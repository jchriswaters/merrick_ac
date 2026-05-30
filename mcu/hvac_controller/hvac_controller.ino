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
 *   - Arduino_LED_Matrix (built-in 12x8 LED matrix)
 */

#include <Arduino_RouterBridge.h>
#include <Arduino_LED_Matrix.h>

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
  bool heatPumpOk        = true;   // outdoor >= heatPumpMinTempF  → heat pump alone is fine
  bool auxHeatNeeded     = false;  // outdoor < heatPumpMinTempF   → also run aux electric
  bool tempRisingFast    = true;   // indoor rising >= 1°F / 15 min → heat pump keeping up
                                   //   default TRUE = start low stage; escalate if Linux says not keeping up
  bool ventOk            = false;  // outdoor < freeCoolMaxTempF AND hum < threshold → open vent
  bool ventBlocked       = false;  // outdoor humidity >= highHumidityPct → vent must stay off
  bool humidityModerate  = false;  // indoor RH >= indoorHumidityLowPct  → dehumidifier-only no longer "safe enough"
  bool humidityHigh      = false;  // indoor RH >= indoorHumidityHighPct → emergency: force high_cool, kill dehumidifier
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

// Pixel glyph type — defined here (top of globals) so the Arduino
// preprocessor places the typedef before any auto-generated prototypes.
typedef uint8_t Glyph[5][3];

Config      cfg;
Inputs      inp;
Outputs     out;
SensorFlags sf;

// Compressor mode-reversal interlock (3 minutes).
//
// Timer starts whenever the compressor stops.  Within INTERLOCK_MS of
// that, restarting the compressor in the OPPOSITE direction (heat<->cool)
// is blocked — the mechanical reversal protection.  Restarting in the
// SAME direction is allowed immediately (no short-cycle penalty when the
// system is just satisfying the same kind of call again), and live stage
// changes within a direction (low_cool <-> high_cool) never trigger the
// timer at all because they don't stop the compressor.
const unsigned long INTERLOCK_MS = 180000UL;
unsigned long lastCompressorOffMs = 0;

enum CompMode : uint8_t { CM_NONE = 0, CM_COOL = 1, CM_HEAT = 2 };
// The direction the heat-pump compressor was in last time it ran.
// Used to decide whether a fresh start is in the SAME direction (allowed
// immediately) or the OPPOSITE direction (subject to INTERLOCK_MS).
CompMode lastCompressorMode = CM_NONE;

// Dehumidifier runtime timer (rules 7–8 in control-logic.md)
// Dehumidifier runs alone (no compressor) while humidity is high.
// After dehumMaxMinutes it times out and the system switches to
// high_cool until the humidity sensor clears.
unsigned long dehumStartMs  = 0;
bool          dehumTimedOut = false;

// Vent timer — tracks position within the current 1-hour window
// so the vent opens for cfg.ventMinPerHour minutes each hour.
unsigned long ventHourStartMs = 0;

// ── Input override (HMI simulation) ─────────────────────────────
// Lets the desktop HMI force thermostat inputs on/off for testing,
// without physically triggering a thermostat.  Bit order matches the
// get_inputs bitmask (index 0-8):
//   0 mainLowCool 1 mainHighCool 2 mainHeat 3 theaterCool 4 theaterHeat
//   5 downCool 6 downHeat 7 highHumidity 8 ventIn
//   inputOverrideMask  bit i = 1 → input i is forced (ignores hardware)
//   inputOverrideValue bit i     → forced state for input i
// Volatile: cleared on MCU reset/power cycle.  Overridden inputs still
// pass through runZoneLogic(), so all equipment safety interlocks
// (compressor 3-min lockout, heat/cool mutual exclusion) remain enforced.
uint16_t inputOverrideMask  = 0;
uint16_t inputOverrideValue = 0;

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
  // Read the physical pins (active-HIGH).
  bool phys[9];
  phys[0] = (digitalRead(PIN_MAIN_LOW_COOL)    == HIGH);
  phys[1] = (digitalRead(PIN_MAIN_HIGH_COOL)   == HIGH);
  phys[2] = (digitalRead(PIN_MAIN_HEAT)        == HIGH);
  phys[3] = (digitalRead(PIN_THEATER_COOL)     == HIGH);
  phys[4] = (digitalRead(PIN_THEATER_HEAT)     == HIGH);
  phys[5] = (digitalRead(PIN_DOWNSTAIRS_COOL)  == HIGH);
  phys[6] = (digitalRead(PIN_DOWNSTAIRS_HEAT)  == HIGH);
  phys[7] = (digitalRead(PIN_HIGH_HUMIDITY)    == HIGH);
  phys[8] = (digitalRead(PIN_VENT_IN)          == HIGH);

  // Apply HMI overrides: where a mask bit is set, substitute the forced
  // value; otherwise use the live hardware reading.
  bool eff[9];
  for (int i = 0; i < 9; i++) {
    if (inputOverrideMask & (1u << i)) {
      eff[i] = ((inputOverrideValue >> i) & 1u) != 0;
    } else {
      eff[i] = phys[i];
    }
  }

  inp.mainLowCool  = eff[0];
  inp.mainHighCool = eff[1];
  inp.mainHeat     = eff[2];
  inp.theaterCool  = eff[3];
  inp.theaterHeat  = eff[4];
  inp.downCool     = eff[5];
  inp.downHeat     = eff[6];
  inp.highHumidity = eff[7];
  inp.ventIn       = eff[8];
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

  } else if (mainCoolCall || sf.humidityHigh) {
    // ── Cooling (main thermostat OR emergency dehumidification) ──
    //
    // Rule 3 (humidityHigh): if indoor humidity is above the second-level
    // threshold, force high_cool regardless of the humidistat input.
    // This routes through the same cooling branch as a thermostat cool
    // call (so the mutual-exclusion-with-dehumidifier logic + interlocks
    // all still apply).
    desired.revValve = false;
    desired.fanOn    = true;

    // Stage selection priority:
    //   • humidityHigh      — rule 3, emergency dehumidification → high
    //   • Y2 (mainHighCool) — rule 1 / standard 2-stage thermostat → high
    //   • highHumidity      — rule 6, humidistat moisture removal → high
    //   • otherwise         — rule 5, low stage on Y1 alone
    if (sf.humidityHigh || inp.mainHighCool || inp.highHumidity) {
      desired.highCool = true;
    } else {
      desired.lowCool = true;
    }

  } else {
    // ── No main cool / heat call, indoor humidity not yet "high" ──

    if (inp.highHumidity) {
      // Rules 2 / 7 / 8: humidistat is asking for dehumidification.
      if (dehumTimedOut) {
        // Rule 8 safety: dehumidifier has been running too long without
        // clearing the humidistat — escalate to high_cool until it does.
        desired.highCool = true;
        desired.fanOn    = true;
      } else if (sf.humidityModerate) {
        // Indoor humidity is in the "moderate" band (>= low threshold,
        // < high threshold).  The dehumidifier may not be keeping up but
        // we haven't tripped the emergency threshold yet — keep running
        // the dehumidifier and let the timeout safety catch a real failure.
        desired.dehumOn = true;
        desired.fanOn   = true;
      } else {
        // Rule 2: humidistat is on but indoor humidity is below the
        // first-level threshold (dehumidifier alone is comfortably
        // handling moisture).  Run dehumidifier + fan only — no compressor.
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
  // COMPRESSOR INTERLOCK (mode-reversal protection only)
  //
  // Restarting the compressor in the SAME direction (cool→cool or
  // heat→heat) is allowed immediately — the silent-then-on-again case
  // doesn't risk the compressor mechanically.
  // Restarting in the OPPOSITE direction within INTERLOCK_MS of the
  // last stop is blocked, to give the reversing valve and refrigerant
  // pressures time to equalize.
  // Live stage changes (low↔high) within a single direction never stop
  // the compressor, so they're not subject to the timer at all.
  // ─────────────────────────────────────────────────────────

  bool wasCompOn   = out.highCool  || out.lowCool;   // heat pump compressor was running
  bool wantsCompOn = desired.highCool || desired.lowCool;
  CompMode wantedMode = desired.revValve ? CM_HEAT : CM_COOL;

  // ── Mode-reversal-while-running guard ──────────────────────
  // If the reversing valve must change direction while the compressor
  // is still spinning, the timer-based interlock can't help (it only
  // starts when the compressor stops).  Force the compressor off
  // immediately, hold the valve in its current position, and let the
  // restart-protection block below gate the new direction.
  if (wasCompOn && wantsCompOn && (desired.revValve != out.revValve)) {
    desired.highCool = false;
    desired.lowCool  = false;
    desired.highHeat = false;
    desired.revValve = out.revValve;   // hold valve; new direction starts after lockout
    wantsCompOn      = false;          // compressor is now off — let timer start below
  }

  // Start the interlock timer when the compressor turns off, and
  // remember which direction it was running in so we can tell a same-
  // direction restart from a reversal.
  if (wasCompOn && !wantsCompOn) {
    lastCompressorOffMs = millis();
    lastCompressorMode  = out.revValve ? CM_HEAT : CM_COOL;
  }

  // Block ONLY a direction reversal within the lockout window.
  // Same-direction restarts go through immediately (no penalty).
  bool isReversal = (lastCompressorMode != CM_NONE) &&
                    (wantedMode != lastCompressorMode);
  if (!wasCompOn && wantsCompOn && isReversal && interlockActive()) {
    desired.highCool = false;
    desired.lowCool  = false;
    desired.highHeat = false;
    desired.revValve = out.revValve;   // hold current valve position
    desired.fanOn    = out.fanOn;      // hold current fan state
    // Dehumidifier can still run during a compressor lockout period
  }

  // ─────────────────────────────────────────────────────────
  // ZONE DAMPER LOGIC (rules 10–11)
  //
  // While the system is ACTIVELY heating or cooling, a secondary zone's
  // damper opens only if that zone is actively calling for the SAME
  // mode the main thermostat is driving.  Any zone that isn't asking
  // for the current mode (silent or wrong direction) → damper closes.
  //
  // While the system is idle / fan-only / dehumidifier-only, all zone
  // dampers open for whole-house circulation.
  // ─────────────────────────────────────────────────────────

  bool mainIsHeating = desired.revValve && (desired.lowCool || desired.highCool);
  bool mainIsCooling = !desired.revValve && (desired.lowCool || desired.highCool);

  // Theater damper (gated by theaterEnabled config flag)
  if (!cfg.theaterEnabled) {
    desired.theaterDamper = true;            // zone disabled: always open
  } else if (mainIsCooling) {
    desired.theaterDamper = inp.theaterCool; // open only if zone calling cool too
  } else if (mainIsHeating) {
    desired.theaterDamper = inp.theaterHeat; // open only if zone calling heat too
  } else {
    desired.theaterDamper = true;            // idle / fan-only: open for circulation
  }

  // Downstairs damper (always enabled)
  if (mainIsCooling) {
    desired.downDamper = inp.downCool;
  } else if (mainIsHeating) {
    desired.downDamper = inp.downHeat;
  } else {
    desired.downDamper = true;
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
// BRIDGE RPC HANDLERS  (Arduino_RouterBridge / Uno Q)
//
// Registered in setup() with Bridge.provide_safe().
// All callbacks run in the main Zephyr thread (via __loopHook)
// so they may safely read/write MCU globals without a mutex.
//
// Python bridge_daemon.py calls these via Bridge.call() every ~10 s.
//
// Output bitmask order (9 chars, index 0-8):
//   highCool lowCool highHeat revValve theaterDamper downDamper
//   ventOut dehumOn fanOn
//
// Input bitmask order (9 chars, index 0-8):
//   mainLowCool mainHighCool mainHeat theaterCool theaterHeat
//   downCool downHeat highHumidity ventIn
// ─────────────────────────────────────────────────────────────

// Linux calls get_outputs() → 9-char relay bitmask ("0"/"1" per relay)
static String rpc_get_outputs() {
  char buf[10];
  buf[0] = out.highCool      ? '1' : '0';
  buf[1] = out.lowCool       ? '1' : '0';
  buf[2] = out.highHeat      ? '1' : '0';
  buf[3] = out.revValve      ? '1' : '0';
  buf[4] = out.theaterDamper ? '1' : '0';
  buf[5] = out.downDamper    ? '1' : '0';
  buf[6] = out.ventOut       ? '1' : '0';
  buf[7] = out.dehumOn       ? '1' : '0';
  buf[8] = out.fanOn         ? '1' : '0';
  buf[9] = '\0';
  return String(buf);
}

// Linux calls get_inputs() → 9-char thermostat/humidity input bitmask
static String rpc_get_inputs() {
  char buf[10];
  buf[0] = inp.mainLowCool  ? '1' : '0';
  buf[1] = inp.mainHighCool ? '1' : '0';
  buf[2] = inp.mainHeat     ? '1' : '0';
  buf[3] = inp.theaterCool  ? '1' : '0';
  buf[4] = inp.theaterHeat  ? '1' : '0';
  buf[5] = inp.downCool     ? '1' : '0';
  buf[6] = inp.downHeat     ? '1' : '0';
  buf[7] = inp.highHumidity ? '1' : '0';
  buf[8] = inp.ventIn       ? '1' : '0';
  buf[9] = '\0';
  return String(buf);
}

// Linux calls set_flags(hpo, ahn, trf, vok, vbl, hmod, hhi) → pushes SensorFlags.
// Arg order MUST match push_sensor_flags_to_mcu() in linux/bridge_daemon.py.
static bool rpc_set_flags(bool hpo, bool ahn, bool trf, bool vok, bool vbl,
                          bool hmod, bool hhi) {
  sf.heatPumpOk       = hpo;
  sf.auxHeatNeeded    = ahn;
  sf.tempRisingFast   = trf;
  sf.ventOk           = vok;
  sf.ventBlocked      = vbl;
  sf.humidityModerate = hmod;
  sf.humidityHigh     = hhi;
  return true;
}

// Linux calls set_config(te, vph, mo, dmm) → pushes Config
static bool rpc_set_config(bool te, int vph, bool mo, int dmm) {
  cfg.theaterEnabled  = te;
  cfg.ventMinPerHour  = (uint8_t)constrain(vph, 0, 60);
  cfg.modeOverride    = mo;
  cfg.dehumMaxMinutes = (uint8_t)constrain(dmm, 5, 120);
  return true;
}

// HMI calls set_input_override(mask, value) → force thermostat inputs.
//   mask:  bit i = 1 means input i is overridden (ignores hardware pin)
//   value: bit i     forced state for input i when overridden
// Bit order matches get_inputs (index 0-8).  Call (0, 0) to clear all
// overrides and return every input to live hardware.
static bool rpc_set_input_override(int mask, int value) {
  inputOverrideMask  = (uint16_t)(mask  & 0x1FF);   // 9 valid bits
  inputOverrideValue = (uint16_t)(value & 0x1FF);
  return true;
}

// HMI calls get_input_override() → 9-char string, one char per input:
//   '-' = auto (live hardware), '1' = forced ON, '0' = forced OFF
static String rpc_get_input_override() {
  char buf[10];
  for (int i = 0; i < 9; i++) {
    if (inputOverrideMask & (1u << i)) {
      buf[i] = ((inputOverrideValue >> i) & 1u) ? '1' : '0';
    } else {
      buf[i] = '-';
    }
  }
  buf[9] = '\0';
  return String(buf);
}

// ─────────────────────────────────────────────────────────────
// LED MATRIX DIAGNOSTICS
//
// The built-in 12-column × 8-row LED matrix displays a rotating
// two-page diagnostic readout:
//
//   Page 0 — MODE (3 s)
//   ┌─────────────────────────────────────────┐
//   │ col:  0 1 2   4   5   7   9  11         │
//   │ r 0:  [glyph ]   ·   ·   ·  ♥  ← heartbeat (blinks 1 Hz)
//   │ r 1:  [glyph ]   ·   ·   ·              │
//   │ r 2:  [glyph ]                          │
//   │ r 3:  [glyph ]   ·   ·   ·  ⚡ ← interlock blink
//   │ r 4:  [glyph ]                          │
//   │ r 5:                             ◌ ← vent open
//   │ r 7:                             ◌ ← high humidity
//   └─────────────────────────────────────────┘
//
//   Glyph (cols 0-2, rows 1-5, 3×5 pixel font):
//     H = heating   C = cooling   d = dehumidify
//     I = idle      O = override  L = locked (interlock)
//
//   Col 5 dots  (rows 1/3): high-stage / low-stage compressor
//   Col 7 dots  (rows 1/3): reversing valve energised / aux heat on
//   Col 9 dots  (rows 1/3): fan on / dehumidifier on
//
//   Page 1 — RELAY GRID (3 s)
//   ┌──────────────────────────────────────────┐
//   │      col 1   col 5   col 9   col 11      │
//   │ r 0:  HC      LC      HH       ♥          │
//   │ r 3:  RV      TD      DD                  │
//   │ r 6:  FN      VO      DH                  │
//   └──────────────────────────────────────────┘
//   HC=high_cool  LC=low_cool   HH=high_heat
//   RV=rev_valve  TD=theater    DD=downstairs
//   FN=fan        VO=vent_out   DH=dehumidifier
//   Lit = relay energised, dark = off.
// ─────────────────────────────────────────────────────────────

ArduinoLEDMatrix ledMatrix;

// Display timing
static unsigned long matPageMs  = 0;        // last page flip
static unsigned long matHeartMs = 0;        // last heartbeat edge
static uint8_t       matPage    = 0;        // 0=mode, 1=relays
static bool          matHeart   = false;    // heartbeat state

const unsigned long MAT_PAGE_MS  = 3000UL; // page hold time
const unsigned long MAT_HEART_MS =  500UL; // heartbeat half-period (1 Hz)

// 3-wide x 5-tall pixel glyphs (row-major, top-to-bottom)
// (Glyph typedef is defined earlier in the globals section)

static const Glyph GL_HEAT = {             // H
  {1,0,1},{1,0,1},{1,1,1},{1,0,1},{1,0,1}
};
static const Glyph GL_COOL = {             // C
  {1,1,1},{1,0,0},{1,0,0},{1,0,0},{1,1,1}
};
static const Glyph GL_DEHUM = {            // d
  {1,1,0},{1,0,1},{1,0,1},{1,0,1},{1,1,0}
};
static const Glyph GL_IDLE = {             // I
  {1,1,1},{0,1,0},{0,1,0},{0,1,0},{1,1,1}
};
static const Glyph GL_OVRD = {             // O
  {0,1,0},{1,0,1},{1,0,1},{1,0,1},{0,1,0}
};
static const Glyph GL_LOCK = {             // L
  {1,0,0},{1,0,0},{1,0,0},{1,0,0},{1,1,1}
};

// Draw a 3×5 glyph into frame[] starting at (r0, c0)
static void matGlyph(uint8_t fr[][12], const Glyph &g, uint8_t r0, uint8_t c0) {
  for (uint8_t r = 0; r < 5; r++)
    for (uint8_t c = 0; c < 3; c++)
      fr[r0 + r][c0 + c] = g[r][c];
}

// Page 0: mode letter + key relay indicator dots
static void matDrawMode(uint8_t fr[][12]) {
  bool isHeating = out.revValve && (out.lowCool || out.highCool);
  bool isCooling = (!out.revValve) && (out.lowCool || out.highCool);

  const Glyph *g;
  if      (cfg.modeOverride)                              g = &GL_OVRD;
  else if (interlockActive() && !isHeating && !isCooling) g = &GL_LOCK;
  else if (isHeating)                                     g = &GL_HEAT;
  else if (isCooling)                                     g = &GL_COOL;
  else if (out.dehumOn)                                   g = &GL_DEHUM;
  else                                                    g = &GL_IDLE;

  matGlyph(fr, *g, 1, 0);   // centred vertically (rows 1-5, cols 0-2)

  // col 5: compressor stage (high=row1, low=row3)
  if (out.highCool)  fr[1][5] = 1;
  if (out.lowCool)   fr[3][5] = 1;

  // col 7: reversing valve (heating mode) + aux electric heat
  if (out.revValve)  fr[1][7] = 1;
  if (out.highHeat)  fr[3][7] = 1;

  // col 9: fan + dehumidifier
  if (out.fanOn)     fr[1][9] = 1;
  if (out.dehumOn)   fr[3][9] = 1;

  // col 11: interlock blink / vent open / high humidity flag
  if (interlockActive() && matHeart) fr[3][11] = 1;
  if (out.ventOut)                   fr[5][11] = 1;
  if (inp.highHumidity)              fr[7][11] = 1;
}

// Page 1: 3×3 relay state grid (one dot per relay)
static void matDrawRelays(uint8_t fr[][12]) {
  const uint8_t C[3] = {1, 5, 9};   // three output columns

  fr[0][C[0]] = out.highCool      ? 1 : 0;  // HC
  fr[0][C[1]] = out.lowCool       ? 1 : 0;  // LC
  fr[0][C[2]] = out.highHeat      ? 1 : 0;  // HH

  fr[3][C[0]] = out.revValve      ? 1 : 0;  // RV
  fr[3][C[1]] = out.theaterDamper ? 1 : 0;  // TD
  fr[3][C[2]] = out.downDamper    ? 1 : 0;  // DD

  fr[6][C[0]] = out.fanOn         ? 1 : 0;  // FN
  fr[6][C[1]] = out.ventOut       ? 1 : 0;  // VO
  fr[6][C[2]] = out.dehumOn       ? 1 : 0;  // DH
}

// Call from loop() — draws the current page and ticks heartbeat/flip
void updateLEDMatrix() {
  unsigned long now = millis();

  if (now - matHeartMs >= MAT_HEART_MS) { matHeart = !matHeart; matHeartMs = now; }
  if (now - matPageMs  >= MAT_PAGE_MS)  { matPage  = (matPage + 1) % 2; matPageMs = now; }

  uint8_t fr[8][12] = {};            // zero = all LEDs off

  if (matPage == 0) matDrawMode(fr);
  else              matDrawRelays(fr);

  fr[0][11] = matHeart ? 1 : 0;     // heartbeat: top-right corner, both pages

  ledMatrix.renderBitmap(fr, 8, 12);
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

  // ── LED matrix ─────────────────────────────────────────────
  ledMatrix.begin();

  // ── Start Bridge (blocks until Linux side is ready, ~2-30 s)─
  // Show "L" (locked/loading) on the matrix while Bridge starts up.
  {
    uint8_t fr[8][12] = {};
    matGlyph(fr, GL_LOCK, 1, 0);
    ledMatrix.renderBitmap(fr, 8, 12);
  }
  Bridge.begin();

  // Register RPC methods callable by bridge_daemon.py.
  // provide_safe() guarantees callbacks run in the main Zephyr thread.
  Bridge.provide_safe("get_outputs", rpc_get_outputs);
  Bridge.provide_safe("get_inputs",  rpc_get_inputs);
  Bridge.provide_safe("set_flags",   rpc_set_flags);
  Bridge.provide_safe("set_config",  rpc_set_config);
  Bridge.provide_safe("set_input_override", rpc_set_input_override);
  Bridge.provide_safe("get_input_override", rpc_get_input_override);

  Serial.println(F("[HVAC] Controller ready"));
}

// ─────────────────────────────────────────────────────────────
// LOOP  (~10 Hz, 100 ms cycle)
// ─────────────────────────────────────────────────────────────

void loop() {
  readDigitalInputs();
  runZoneLogic();
  applyOutputs();
  updateLEDMatrix();   // refresh LED matrix diagnostic display
  delay(100);
}
