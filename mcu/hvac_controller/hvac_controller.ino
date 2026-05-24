/*
 * hvac_controller.ino
 * Arduino Uno Q — STM32U585 MCU sketch
 *
 * Controls a Unico mini-duct HVAC system with:
 *   - 3-zone thermostat inputs (main, downstairs, theater)
 *   - 8 relay outputs (cooling stages, heat, reversing valve,
 *     zone dampers, vent, dehumidifier)
 *   - Arduino Bridge RPC to Linux side (MQTT + web config)
 *
 * All sensor and power monitoring is handled by the Linux side
 * (bridge_daemon.py) via a USB-RS485 adapter connected to the
 * QRB2210 USB port. The MCU handles digital I/O only.
 *
 * See docs/system-design.md for full pin map, bus architecture,
 * control logic rules, and safety interlock requirements.
 *
 * Libraries required:
 *   - Arduino Bridge (built-in Uno Q support)
 *
 * TODO (implement in Claude Code session):
 *   [ ] setup() — pin modes, Bridge init
 *   [ ] loop() — read inputs, run zone logic, apply outputs, expose via Bridge
 *   [ ] readDigitalInputs()
 *   [ ] runZoneLogic() — mode arbitration, interlock timer
 *   [ ] applyOutputs()
 *   [ ] exposeToBridge() — publish relay state for Linux side to read
 *   [ ] handleBridgeCommands() — receive config from Linux side
 */

#include <Bridge.h>         // Arduino Uno Q Bridge RPC

// ─────────────────────────────────────────────────────────────
// PIN DEFINITIONS — see docs/system-design.md for full table
// ─────────────────────────────────────────────────────────────

// Digital inputs (active-low via optocoupler isolation)
const uint8_t PIN_MAIN_LOW_COOL     = 2;
const uint8_t PIN_MAIN_HIGH_COOL    = 3;
const uint8_t PIN_MAIN_HEAT         = 4;
const uint8_t PIN_THEATER_COOL      = 5;
const uint8_t PIN_THEATER_HEAT      = 6;
const uint8_t PIN_DOWNSTAIRS_COOL   = 7;
const uint8_t PIN_DOWNSTAIRS_HEAT   = 8;
const uint8_t PIN_HIGH_HUMIDITY     = 9;
const uint8_t PIN_VENT_IN           = 10;

// Digital outputs (relay board, active-low)
const uint8_t PIN_HIGH_COOL         = 11;
const uint8_t PIN_LOW_COOL          = 12;
const uint8_t PIN_HIGH_HEAT         = 13;
const uint8_t PIN_REVERSING_VALVE   = 14;
const uint8_t PIN_THEATER_DAMPER    = 15;
const uint8_t PIN_DOWNSTAIRS_DAMPER = 16;
const uint8_t PIN_VENT_OUT          = 17;
const uint8_t PIN_DEHUMIDIFIER      = 18;

// ─────────────────────────────────────────────────────────────
// CONFIGURATION (updated by Linux side via Bridge RPC)
// ─────────────────────────────────────────────────────────────

struct Config {
  bool    theaterEnabled    = false;  // theater zone active?
  uint8_t ventMinPerHour    = 10;     // minutes per hour to open vent
  bool    modeOverride      = false;  // force system off
};

// ─────────────────────────────────────────────────────────────
// INPUT STATE
// ─────────────────────────────────────────────────────────────

struct Inputs {
  bool mainLowCool    = false;
  bool mainHighCool   = false;
  bool mainHeat       = false;
  bool theaterCool    = false;
  bool theaterHeat    = false;
  bool downCool       = false;
  bool downHeat       = false;
  bool highHumidity   = false;
  bool ventIn         = false;
};

// ─────────────────────────────────────────────────────────────
// OUTPUT STATE
// ─────────────────────────────────────────────────────────────

struct Outputs {
  bool highCool       = false;
  bool lowCool        = false;
  bool highHeat       = false;
  bool revValve       = false;
  bool theaterDamper  = false;
  bool downDamper     = false;
  bool ventOut        = false;
  bool dehumOn        = false;
};

// ─────────────────────────────────────────────────────────────
// GLOBAL STATE
// ─────────────────────────────────────────────────────────────

Config  cfg;
Inputs  inp;
Outputs out;

// Compressor interlock timer
const unsigned long INTERLOCK_MS = 180000UL;  // 3 minutes
unsigned long lastModeChangeMs   = 0;

// ─────────────────────────────────────────────────────────────
// FUNCTION STUBS — implement these in Claude Code session
// ─────────────────────────────────────────────────────────────

void readDigitalInputs();
void runZoneLogic();
void applyOutputs();
void exposeToBridge();
void handleBridgeCommands();

// ─────────────────────────────────────────────────────────────
// SETUP + LOOP
// ─────────────────────────────────────────────────────────────

void setup() {
  // TODO: implement full setup
  // - Set all output pins LOW before setting as OUTPUT
  // - Configure input pins with appropriate pull mode
  // - Serial.begin() for debug
  // - Bridge.begin()
  // - analogReadResolution(12)
}

void loop() {
  // TODO: implement main control loop (~100ms cycle)
  // readDigitalInputs();
  // runZoneLogic();
  // applyOutputs();
  // exposeToBridge();
  // handleBridgeCommands();
  // delay(100);
}
