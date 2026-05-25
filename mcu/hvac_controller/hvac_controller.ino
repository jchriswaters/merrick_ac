/*
 * hvac_controller.ino
 * Arduino Uno Q — STM32U585 MCU sketch
 *
 * Controls a Unico mini-duct HVAC system with:
 *   - 3-zone thermostat inputs (main, downstairs, theater)
 *   - 9 relay outputs (cooling stages, heat, reversing valve,
 *     fan, zone dampers, vent, dehumidifier)
 *   - Arduino Bridge RPC to Linux side (MQTT + web config)
 *
 * All sensor and power monitoring is handled by the Linux side
 * (bridge_daemon.py) via a USB-RS485 adapter connected to the
 * QRB2210 USB port. The Linux side evaluates all temperature and
 * humidity thresholds and pushes five pre-computed SensorFlags to
 * the MCU via Bridge RPC every ~10 seconds.
 *
 * The MCU handles digital I/O and real-time relay safety only.
 *
 * See docs/system-design.md and docs/control-logic.md for full
 * pin map, bus architecture, and control logic rules.
 *
 * Libraries required:
 *   - Arduino Bridge (built-in Uno Q support)
 */

#include <Bridge.h>

// ─────────────────────────────────────────────────────────────
// PIN DEFINITIONS
// ─────────────────────────────────────────────────────────────

// Digital inputs — active-HIGH via 24VAC relay isolation
// Relay coil driven by thermostat 24VAC; contact side = Arduino 3.3V → MCU pin
// Configure as INPUT_PULLDOWN: LOW = relay open (no call), HIGH = relay closed (calling)
const uint8_t PIN_MAIN_LOW_COOL     = 2;   // Main thermostat Y1
const uint8_t PIN_MAIN_HIGH_COOL    = 3;   // Main thermostat Y2
const uint8_t PIN_MAIN_HEAT         = 4;   // Main thermostat W
const uint8_t PIN_THEATER_COOL      = 5;   // Theater thermostat Y
const uint8_t PIN_THEATER_HEAT      = 6;   // Theater thermostat W
const uint8_t PIN_DOWNSTAIRS_COOL   = 7;   // Downstairs thermostat Y
const uint8_t PIN_DOWNSTAIRS_HEAT   = 8;   // Downstairs thermostat W
const uint8_t PIN_HIGH_HUMIDITY     = 9;   // External humidity controller (HIGH = humid)
const uint8_t PIN_VENT_IN           = 10;  // Humidity controller timer output

// Digital outputs — active-HIGH into 8+1 channel optocoupler relay board
// Logic side VCC = Arduino 3.3V; coil side VCC = Arduino 5V
// HIGH = relay energised = 24VAC circuit closed
const uint8_t PIN_HIGH_COOL         = 11;  // Unico Y2 — high-stage compressor
const uint8_t PIN_LOW_COOL          = 12;  // Unico Y1 — low-stage compressor
const uint8_t PIN_HIGH_HEAT         = 13;  // Aux electric heater
const uint8_t PIN_REVERSING_VALVE   = 14;  // Unico O/B  — B-type: HIGH = heating
const uint8_t PIN_THEATER_DAMPER    = 15;  // Theater zone damper open
const uint8_t PIN_DOWNSTAIRS_DAMPER = 16;  // Downstairs zone damper open
const uint8_t PIN_VENT_OUT          = 17;  // Fresh-air vent actuator open
const uint8_t PIN_DEHUMIDIFIER      = 18;  // Dehumidifier on
const uint8_t PIN_FAN               = 19;  // Unico G wire — fan-only / dehumidifier assist

// ─────────────────────────────────────────────────────────────
// CONFIGURATION  (updated by Linux side via Bridge RPC)
// Threshold values are evaluated on the Linux side; results are
// delivered as SensorFlags. These fields are stored on the MCU
// only for reference and display via Bridge.
// ─────────────────────────────────────────────────────────────

struct Config {
  bool    theaterEnabled   = false;  // theater zone damper logic active
  uint8_t ventMinPerHour   = 10;     // fresh-air vent open minutes per hour (0–60)
  bool    modeOverride     = false;  // true = force system off
  int8_t  heatPumpMinTempF = 40;     // outdoor °F below which aux electric heat engages
  int8_t  freeCoolMaxTempF = 60;     // outdoor °F below which free-cooling vent opens
  uint8_t highHumidityPct  = 80;     // outdoor %RH at or above which vent is forced off
  uint8_t dehumMaxMinutes  = 20;     // dehumidifier-only runtime before forced high_cool
};

// ─────────────────────────────────────────────────────────────
// SENSOR FLAGS  (pushed by Linux side via Bridge RPC, ~10 s)
// Linux evaluates all temperature / humidity thresholds and
// pushes pre-computed booleans. MCU uses them directly in logic.
// Defaults are chosen to be safe on first power-up before Linux
// side has had time to push real values.
// ─────────────────────────────────────────────────────────────

struct SensorFlags {
  bool heatPumpOk     = true;   // outdoor >= heatPumpMinTempF  → heat pump alone is fine
  bool auxHeatNeeded  = false;  // outdoor < heatPumpMinTempF   → also run aux electric
  bool tempRisingFast = true;   // indoor rising >= 1°F / 15 min → heat pump keeping up
                                //   default TRUE = start low stage; escalate if Linux says not keeping up
  bool ventOk         = false;  // outdoor < freeCoolMaxTempF AND hum < threshold → open vent
  bool ventBlocked    = false;  // outdoor humidity >= highHumidityPct → vent must stay off
};

// ─────────────────────────────────────────────────────────────
// INPUT STATE
// ─────────────────────────────────────────────────────────────

struct Inputs {
  bool mainLowCool  = false;  // D2
  bool mainHighCool = false;  // D3
  bool mainHeat     = false;  // D4
  bool theaterCool  = false;  // D5
  bool theaterHeat  = false;  // D6
  bool downCool     = false;  // D7
  bool downHeat     = false;  // D8
  bool highHumidity = false;  // D9
  bool ventIn       = false;  // D10
};

// ─────────────────────────────────────────────────────────────
// OUTPUT STATE
// ─────────────────────────────────────────────────────────────

struct Outputs {
  bool highCool      = false;  // D11 — Unico Y2
  bool lowCool       = false;  // D12 — Unico Y1
  bool highHeat      = false;  // D13 — aux electric
  bool revValve      = false;  // D14 — B-type: true = heating, false = cooling
  bool theaterDamper = false;  // D15
  bool downDamper    = false;  // D16
  bool ventOut       = false;  // D17
  bool dehumOn       = false;  // D18
  bool fanOn         = false;  // D19 — Unico G wire
};

// ─────────────────────────────────────────────────────────────
// GLOBAL STATE
// ─────────────────────────────────────────────────────────────

Config      cfg;
Inputs      inp;
Outputs     out;
SensorFlags sf;

// Compressor short-cycle / mode-change interlock (3 minutes)
// Timer starts when compressor turns OFF. Prevents turning back ON,
// or reversing heat↔cool, until INTERLOCK_MS has elapsed.
const unsigned long INTERLOCK_MS = 180000UL;
unsigned long lastCompressorOffMs = 0;

// Dehumidifier runtime timer (rules 7–8 in control-logic.md)
// Dehumidifier runs alone (no compressor) while humidity is high.
// After dehumMaxMinutes it times out and the system switches to
// high_cool until the humidity sensor clears.
unsigned long dehumStartMs  = 0;
bool          dehumTimedOut = false;

// Vent timer — tracks position within the current 1-hour window
// so the vent opens for cfg.ventMinPerHour minutes each hour.
unsigned long ventHourStartMs = 0;

// ─────────────────────────────────────────────────────────────
// HELPERS
// ─────────────────────────────────────────────────────────────

// Returns true if the compressor interlock is still active.
bool interlockActive() {
  return (millis() - lastCompressorOffMs) < INTERLOCK_MS;
}

// Returns true if the vent timer says the vent should be open
// this minute within the current hour window.
bool checkVentTimer() {
  unsigned long now = millis();
  // Roll over to a new hour window when the current one expires
  if ((now - ventHourStartMs) >= 3600000UL) {
    ventHourStartMs = now;
  }
  unsigned long openMs = (unsigned long)cfg.ventMinPerHour * 60000UL;
  return (now - ventHourStartMs) < openMs;
}

// ─────────────────────────────────────────────────────────────
// readDigitalInputs
// Read all nine thermostat / humidity input pins.
// All inputs are active-HIGH (relay contact to 3.3V; INPUT_PULLDOWN).
// ─────────────────────────────────────────────────────────────

void readDigitalInputs() {
  inp.mainLowCool  = (digitalRead(PIN_MAIN_LOW_COOL)    == HIGH);
  inp.mainHighCool = (digitalRead(PIN_MAIN_HIGH_COOL)   == HIGH);
  inp.mainHeat     = (digitalRead(PIN_MAIN_HEAT)        == HIGH);
  inp.theaterCool  = (digitalRead(PIN_THEATER_COOL)     == HIGH);
  inp.theaterHeat  = (digitalRead(PIN_THEATER_HEAT)     == HIGH);
  inp.downCool     = (digitalRead(PIN_DOWNSTAIRS_COOL)  == HIGH);
  inp.downHeat     = (digitalRead(PIN_DOWNSTAIRS_HEAT)  == HIGH);
  inp.highHumidity = (digitalRead(PIN_HIGH_HUMIDITY)    == HIGH);
  inp.ventIn       = (digitalRead(PIN_VENT_IN)          == HIGH);
}

// ─────────────────────────────────────────────────────────────
// runZoneLogic
// Full control-logic implementation (see docs/control-logic.md).
// Computes desired Outputs from Inputs + SensorFlags + Config,
// then enforces safety interlocks before writing to out.
// ─────────────────────────────────────────────────────────────

void runZoneLogic() {
  // Start with all outputs false; build up the desired state.
  Outputs desired;

  // ── Clear dehumidifier timeout when humidity finally drops ──
  if (!inp.highHumidity) {
    dehumTimedOut = false;
  }

  // ── Check dehumidifier timeout while it is running ─────────
  if (out.dehumOn && !dehumTimedOut) {
    unsigned long maxMs = (unsigned long)cfg.dehumMaxMinutes * 60000UL;
    if ((millis() - dehumStartMs) >= maxMs) {
      dehumTimedOut = true;
    }
  }

  // ── MODE OVERRIDE: force compressor/heat off ────────────────
  // Fan and circulation still run; dampers and vent follow normal rules.
  if (cfg.modeOverride) {
    desired.fanOn = true;
    // Dampers: open by default (no conflicting calls during override)
    desired.theaterDamper = true;
    desired.downDamper    = true;
    // Vent: follow humidity/temperature rules normally
    if (sf.ventBlocked) {
      desired.ventOut = false;
    } else if (sf.ventOk) {
      desired.ventOut = true;
    } else {
      desired.ventOut = inp.ventIn || checkVentTimer();
    }
    out = desired;
    return;
  }

  // ── Resolve simultaneous heat+cool calls: heat wins ─────────
  bool mainCoolCall = inp.mainLowCool || inp.mainHighCool;
  bool mainHeatCall = inp.mainHeat;
  if (mainHeatCall && mainCoolCall) {
    mainCoolCall = false;
  }

  // ═══════════════════════════════════════════════════════════
  // COMPRESSOR / HEAT LOGIC
  // ═══════════════════════════════════════════════════════════

  if (mainHeatCall) {
    // ── Rules 2–4: Heat pump heating, optional aux electric ───
    desired.revValve = true;   // B-type: energise reversing valve for heating
    desired.fanOn    = true;

    // Stage: stay on low unless Linux says heat pump is not keeping up
    if (sf.tempRisingFast) {
      desired.lowCool  = true;   // rule 2: low stage — heat pump keeping up
    } else {
      desired.highCool = true;   // rule 3: high stage — indoor temp not rising fast enough
    }

    // Aux electric heat if outdoor temp is below threshold (rule 4)
    if (sf.auxHeatNeeded) {
      desired.highHeat = true;
    }

  } else if (mainCoolCall) {
    // ── Rules 5–6: Cooling ────────────────────────────────────
    desired.revValve = false;  // B-type: de-energise for cooling
    desired.fanOn    = true;

    if (inp.highHumidity) {
      // Rule 6: Mitsubishi variable-speed needs high stage for moisture removal
      desired.highCool = true;
    } else {
      // Rule 5: normal cooling — low stage
      desired.lowCool = true;
    }

  } else {
    // ── No main thermostat call ───────────────────────────────

    if (inp.highHumidity) {
      // Rules 7–8: Dehumidification without a cooling call
      if (dehumTimedOut) {
        // Rule 8: dehumidifier has been running too long → switch to high_cool
        // until humidity clears. Compressor subject to interlock (handled below).
        desired.highCool = true;
        desired.fanOn    = true;
      } else {
        // Rule 7: run dehumidifier + fan; no compressor
        // (dehumidifier exhaust feeds into air handler before condenser coil)
        desired.dehumOn = true;
        desired.fanOn   = true;
      }
    } else {
      // Rule 14: idle — fan-only circulation, no compressor
      desired.fanOn = true;
    }
  }

  // ── Track dehumidifier start time ──────────────────────────
  if (desired.dehumOn && !out.dehumOn) {
    dehumStartMs = millis();
  }

  // ─────────────────────────────────────────────────────────
  // SAFETY: dehumidifier ↔ compressor mutual exclusion (rule 9)
  // Dehumidifier warm exhaust enters the air handler upstream of
  // the condenser coil — running both simultaneously degrades cooling.
  // ─────────────────────────────────────────────────────────

  if (desired.dehumOn) {
    desired.highCool = false;
    desired.lowCool  = false;
    // (high_heat / aux electric does not conflict with dehumidifier)
    desired.fanOn    = true;   // fan always on when dehumidifier is running
  }

  // Compressor stages always imply fan
  if (desired.highCool || desired.lowCool || desired.highHeat) {
    desired.fanOn = true;
  }

  // ─────────────────────────────────────────────────────────
  // COMPRESSOR INTERLOCK (anti-short-cycle + mode-change delay)
  //
  // lastCompressorOffMs is set whenever the compressor turns OFF.
  // While the interlock is active:
  //   • Turning the compressor ON is blocked (from any OFF state)
  //   • Reversing the valve while compressor is on is blocked
  // Turning the compressor OFF is always allowed immediately.
  // Stage changes within the same mode (low↔high within cooling)
  // are allowed without triggering the interlock.
  // ─────────────────────────────────────────────────────────

  bool wasCompOn  = out.highCool  || out.lowCool;   // heat pump compressor was running
  bool wantsCompOn = desired.highCool || desired.lowCool;

  // Start the interlock timer when the heat pump compressor turns off
  if (wasCompOn && !wantsCompOn) {
    lastCompressorOffMs = millis();
  }

  // Block turning compressor ON during lockout (anti-short-cycle)
  if (!wasCompOn && wantsCompOn && interlockActive()) {
    desired.highCool = false;
    desired.lowCool  = false;
    desired.highHeat = false;
    desired.revValve = out.revValve;   // hold current valve position
    desired.fanOn    = out.fanOn;      // hold current fan state
    // Dehumidifier can still run during a compressor lockout period
  }

  // Block reversing valve flip while compressor is running and interlock active
  // (prevents switching heat↔cool with compressor spinning)
  if (wasCompOn && wantsCompOn && (desired.revValve != out.revValve) && interlockActive()) {
    desired.revValve = out.revValve;
    desired.highCool = out.highCool;
    desired.lowCool  = out.lowCool;
    desired.highHeat = out.highHeat;
  }

  // ─────────────────────────────────────────────────────────
  // ZONE DAMPER LOGIC (rules 10–11)
  //
  // Default state: OPEN.
  // A secondary zone damper closes ONLY when its thermostat is
  // calling for the opposite of what the main thermostat is running.
  // When not calling (zone satisfied), damper stays open for circulation.
  // ─────────────────────────────────────────────────────────

  bool mainIsHeating = desired.revValve && (desired.lowCool || desired.highCool);
  bool mainIsCooling = !desired.revValve && (desired.lowCool || desired.highCool);

  // Theater damper (gated by theaterEnabled config flag)
  if (cfg.theaterEnabled) {
    bool conflict = (inp.theaterHeat && mainIsCooling) ||
                    (inp.theaterCool && mainIsHeating);
    desired.theaterDamper = !conflict;   // open unless zone is fighting main mode
  } else {
    desired.theaterDamper = true;        // zone disabled: always open
  }

  // Downstairs damper
  {
    bool conflict = (inp.downHeat && mainIsCooling) ||
                    (inp.downCool && mainIsHeating);
    desired.downDamper = !conflict;
  }

  // ─────────────────────────────────────────────────────────
  // VENT LOGIC (rules 12–13 + timer)
  //
  // Rule 12 (ventBlocked) takes absolute priority.
  // Rule 13 (ventOk) opens for free cooling.
  // Otherwise: hardware input (D10) OR internal timer.
  // ─────────────────────────────────────────────────────────

  if (sf.ventBlocked) {
    desired.ventOut = false;                          // rule 12: outdoor humidity too high
  } else if (sf.ventOk) {
    desired.ventOut = true;                           // rule 13: free cooling conditions
  } else {
    desired.ventOut = inp.ventIn || checkVentTimer(); // hardware timer or internal schedule
  }

  // ── Commit desired state ────────────────────────────────────
  out = desired;
}

// ─────────────────────────────────────────────────────────────
// applyOutputs
// Write the current Outputs state to the relay output pins.
// Called every loop cycle — idempotent.
// ─────────────────────────────────────────────────────────────

void applyOutputs() {
  digitalWrite(PIN_HIGH_COOL,         out.highCool      ? HIGH : LOW);
  digitalWrite(PIN_LOW_COOL,          out.lowCool       ? HIGH : LOW);
  digitalWrite(PIN_HIGH_HEAT,         out.highHeat      ? HIGH : LOW);
  digitalWrite(PIN_REVERSING_VALVE,   out.revValve      ? HIGH : LOW);
  digitalWrite(PIN_FAN,               out.fanOn         ? HIGH : LOW);
  digitalWrite(PIN_THEATER_DAMPER,    out.theaterDamper ? HIGH : LOW);
  digitalWrite(PIN_DOWNSTAIRS_DAMPER, out.downDamper    ? HIGH : LOW);
  digitalWrite(PIN_VENT_OUT,          out.ventOut       ? HIGH : LOW);
  digitalWrite(PIN_DEHUMIDIFIER,      out.dehumOn       ? HIGH : LOW);
}

// ─────────────────────────────────────────────────────────────
// exposeToBridge
// Publish current relay output states and thermostat input states
// to the Arduino Bridge shared data store. bridge_daemon.py reads
// these every ~10 s and assembles the full MQTT payload.
//
// Key naming: "o_" prefix = output (relay), "i_" prefix = input.
// Compact single-char suffixes match the MCU key map in mqtt-payload-spec.md.
// ─────────────────────────────────────────────────────────────

void exposeToBridge() {
  // Relay outputs
  Bridge.put("o_hc", out.highCool      ? "1" : "0");  // high_cool
  Bridge.put("o_lc", out.lowCool       ? "1" : "0");  // low_cool
  Bridge.put("o_hh", out.highHeat      ? "1" : "0");  // high_heat
  Bridge.put("o_rv", out.revValve      ? "1" : "0");  // reversing_valve
  Bridge.put("o_fn", out.fanOn         ? "1" : "0");  // fan_on
  Bridge.put("o_td", out.theaterDamper ? "1" : "0");  // theater_damper
  Bridge.put("o_dd", out.downDamper    ? "1" : "0");  // downstairs_damper
  Bridge.put("o_vo", out.ventOut       ? "1" : "0");  // vent_open
  Bridge.put("o_dh", out.dehumOn       ? "1" : "0");  // dehumidifier_on

  // Thermostat / humidity inputs (for HMI SCADA view and diagnostics)
  Bridge.put("i_mlc", inp.mainLowCool  ? "1" : "0");  // main low cool
  Bridge.put("i_mhc", inp.mainHighCool ? "1" : "0");  // main high cool
  Bridge.put("i_mh",  inp.mainHeat     ? "1" : "0");  // main heat
  Bridge.put("i_tc",  inp.theaterCool  ? "1" : "0");  // theater cool
  Bridge.put("i_th",  inp.theaterHeat  ? "1" : "0");  // theater heat
  Bridge.put("i_dc",  inp.downCool     ? "1" : "0");  // downstairs cool
  Bridge.put("i_dh",  inp.downHeat     ? "1" : "0");  // downstairs heat
  Bridge.put("i_hh",  inp.highHumidity ? "1" : "0");  // high humidity
  Bridge.put("i_vi",  inp.ventIn       ? "1" : "0");  // vent in
}

// ─────────────────────────────────────────────────────────────
// handleBridgeCommands
// Read SensorFlags and Config updates pushed by bridge_daemon.py.
// Called every loop cycle; Bridge.get() returns immediately if
// the key hasn't changed since last read.
// ─────────────────────────────────────────────────────────────

void handleBridgeCommands() {
  char buf[16] = "";

  // ── SensorFlags ────────────────────────────────────────────
  // Keys match what bridge_daemon.py writes via bridge.put()

  if (Bridge.get("sf_hpo", buf, sizeof(buf)) > 0) {   // heatPumpOk
    sf.heatPumpOk = (buf[0] == '1');
    buf[0] = '\0';
  }
  if (Bridge.get("sf_ahn", buf, sizeof(buf)) > 0) {   // auxHeatNeeded
    sf.auxHeatNeeded = (buf[0] == '1');
    buf[0] = '\0';
  }
  if (Bridge.get("sf_trf", buf, sizeof(buf)) > 0) {   // tempRisingFast
    sf.tempRisingFast = (buf[0] == '1');
    buf[0] = '\0';
  }
  if (Bridge.get("sf_vok", buf, sizeof(buf)) > 0) {   // ventOk
    sf.ventOk = (buf[0] == '1');
    buf[0] = '\0';
  }
  if (Bridge.get("sf_vbl", buf, sizeof(buf)) > 0) {   // ventBlocked
    sf.ventBlocked = (buf[0] == '1');
    buf[0] = '\0';
  }

  // ── Config ─────────────────────────────────────────────────

  if (Bridge.get("cfg_th", buf, sizeof(buf)) > 0) {   // theaterEnabled
    cfg.theaterEnabled = (buf[0] == '1');
    buf[0] = '\0';
  }
  if (Bridge.get("cfg_vph", buf, sizeof(buf)) > 0) {  // ventMinPerHour
    uint8_t v = (uint8_t)atoi(buf);
    if (v <= 60) cfg.ventMinPerHour = v;
    buf[0] = '\0';
  }
  if (Bridge.get("cfg_mo", buf, sizeof(buf)) > 0) {   // modeOverride
    cfg.modeOverride = (buf[0] == '1');
    buf[0] = '\0';
  }
  if (Bridge.get("cfg_dmm", buf, sizeof(buf)) > 0) {  // dehumMaxMinutes
    uint8_t v = (uint8_t)atoi(buf);
    if (v >= 5 && v <= 120) cfg.dehumMaxMinutes = v;
    buf[0] = '\0';
  }
}

// ─────────────────────────────────────────────────────────────
// SETUP
// ─────────────────────────────────────────────────────────────

void setup() {
  // ── Drive all outputs LOW before enabling as OUTPUT ─────────
  // This prevents relay chatter on power-up: pin starts LOW,
  // then is switched to OUTPUT — relay stays de-energised.
  digitalWrite(PIN_HIGH_COOL,         LOW);
  digitalWrite(PIN_LOW_COOL,          LOW);
  digitalWrite(PIN_HIGH_HEAT,         LOW);
  digitalWrite(PIN_REVERSING_VALVE,   LOW);
  digitalWrite(PIN_FAN,               LOW);
  digitalWrite(PIN_THEATER_DAMPER,    LOW);
  digitalWrite(PIN_DOWNSTAIRS_DAMPER, LOW);
  digitalWrite(PIN_VENT_OUT,          LOW);
  digitalWrite(PIN_DEHUMIDIFIER,      LOW);

  pinMode(PIN_HIGH_COOL,         OUTPUT);
  pinMode(PIN_LOW_COOL,          OUTPUT);
  pinMode(PIN_HIGH_HEAT,         OUTPUT);
  pinMode(PIN_REVERSING_VALVE,   OUTPUT);
  pinMode(PIN_FAN,               OUTPUT);
  pinMode(PIN_THEATER_DAMPER,    OUTPUT);
  pinMode(PIN_DOWNSTAIRS_DAMPER, OUTPUT);
  pinMode(PIN_VENT_OUT,          OUTPUT);
  pinMode(PIN_DEHUMIDIFIER,      OUTPUT);

  // ── Configure input pins ────────────────────────────────────
  // INPUT_PULLDOWN: pin reads LOW when relay is open (no call).
  // Relay contact closes → 3.3V reaches pin → reads HIGH (active HIGH).
  pinMode(PIN_MAIN_LOW_COOL,    INPUT_PULLDOWN);
  pinMode(PIN_MAIN_HIGH_COOL,   INPUT_PULLDOWN);
  pinMode(PIN_MAIN_HEAT,        INPUT_PULLDOWN);
  pinMode(PIN_THEATER_COOL,     INPUT_PULLDOWN);
  pinMode(PIN_THEATER_HEAT,     INPUT_PULLDOWN);
  pinMode(PIN_DOWNSTAIRS_COOL,  INPUT_PULLDOWN);
  pinMode(PIN_DOWNSTAIRS_HEAT,  INPUT_PULLDOWN);
  pinMode(PIN_HIGH_HUMIDITY,    INPUT_PULLDOWN);
  pinMode(PIN_VENT_IN,          INPUT_PULLDOWN);

  // ── Serial debug (USB-C during commissioning) ───────────────
  Serial.begin(115200);

  // ── STM32U585 ADC (spare analog pins available if needed) ───
  analogReadResolution(12);

  // ── Pre-expire interlock so first compressor start on boot ──
  // is not needlessly delayed by the 3-minute lockout.
  lastCompressorOffMs = millis() - INTERLOCK_MS;
  ventHourStartMs     = millis();

  // ── Start Bridge (blocks until Linux side is ready, ~2-30 s)─
  Bridge.begin();

  Serial.println(F("[HVAC] Controller ready"));
}

// ─────────────────────────────────────────────────────────────
// LOOP  (~10 Hz, 100 ms cycle)
// ─────────────────────────────────────────────────────────────

void loop() {
  readDigitalInputs();
  runZoneLogic();
  applyOutputs();
  exposeToBridge();
  handleBridgeCommands();
  delay(100);
}
