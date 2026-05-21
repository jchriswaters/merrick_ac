/*
 * hvac_controller.ino
 * Arduino Uno Q — STM32U585 MCU sketch
 *
 * Controls a Unico mini-duct HVAC system with:
 *   - 3-zone thermostat inputs (main, downstairs, theater)
 *   - 8 relay outputs (cooling stages, heat, reversing valve,
 *     zone dampers, vent, dehumidifier)
 *   - RS485 Modbus temp/humidity sensors (2× SHT30 transmitters)
 *   - UART Modbus power monitors (2× PZEM-004T)
 *   - Arduino Bridge RPC to Linux side (MQTT + web config)
 *
 * See docs/system-design.md for full pin map, bus architecture,
 * control logic rules, and safety interlock requirements.
 *
 * Libraries required:
 *   - Arduino Bridge (built-in Uno Q support)
 *   - PZEM004Tv30  (Library Manager: "PZEM-004T")
 *   - ModbusMaster  (Library Manager: "ModbusMaster") -- for RS485 sensors
 *
 * TODO (implement in Claude Code session):
 *   [ ] setup() — pin modes, serial init, sensor init, Bridge init
 *   [ ] loop() — read inputs, run zone logic, read sensors, expose via Bridge
 *   [ ] readDigitalInputs()
 *   [ ] runZoneLogic() — mode arbitration, interlock timer
 *   [ ] applyOutputs()
 *   [ ] readRS485Sensors() — SHT30 temp/hum via Modbus
 *   [ ] readPZEM() — power monitors via UART Modbus
 *   [ ] exposeToBridge() — publish state for Linux side to read
 *   [ ] handleBridgeCommands() — receive config from Linux side
 *   [ ] Address-programming sketch for PZEM (separate .ino)
 */

#include <Bridge.h>         // Arduino Uno Q Bridge RPC
#include <PZEM004Tv30.h>    // PZEM power monitors on Serial1
#include <ModbusMaster.h>   // RS485 SHT30 sensors on Serial2

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

// RS485 direction control for MAX3485 transceiver
const uint8_t PIN_RS485_DE          = 20;  // DE+RE tied together

// ─────────────────────────────────────────────────────────────
// CONFIGURATION (updated by Linux side via Bridge RPC)
// ─────────────────────────────────────────────────────────────

struct Config {
  bool  theaterEnabled      = false;  // theater zone active?
  uint8_t ventMinPerHour    = 10;     // minutes per hour to open vent
  bool  modeOverride        = false;  // force system off
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
// SENSOR DATA
// ─────────────────────────────────────────────────────────────

struct SensorData {
  // RS485 SHT30 transmitters
  float indoorTempF       = 0;
  float indoorHumPct      = 0;
  float indoorDewpointF   = 0;
  float outdoorTempF      = 0;
  float outdoorHumPct     = 0;
  float outdoorDewpointF  = 0;
  // PZEM-004T — AC system (addr 0x01)
  float acVoltage     = 0;
  float acCurrent     = 0;
  float acPower       = 0;
  float acEnergy      = 0;
  float acFrequency   = 0;
  float acPowerFactor = 0;
  // PZEM-004T — dehumidifier (addr 0x02)
  float dhVoltage     = 0;
  float dhCurrent     = 0;
  float dhPower       = 0;
  float dhEnergy      = 0;
  float dhFrequency   = 0;
  float dhPowerFactor = 0;
};

// ─────────────────────────────────────────────────────────────
// GLOBAL STATE
// ─────────────────────────────────────────────────────────────

Config     cfg;
Inputs     inp;
Outputs    out;
SensorData sens;

// Compressor interlock timer
const unsigned long INTERLOCK_MS = 180000UL;  // 3 minutes
unsigned long lastModeChangeMs   = 0;

// RS485 Modbus master instance (for SHT30 sensors on Serial2)
ModbusMaster  rs485;

// PZEM instances on Serial1
// TODO: confirm correct Serial1 constructor for Uno Q / STM32U585
PZEM004Tv30 pzemAC   (&Serial1, 0x01);
PZEM004Tv30 pzemDehum(&Serial1, 0x02);

// ─────────────────────────────────────────────────────────────
// FUNCTION STUBS — implement these in Claude Code session
// ─────────────────────────────────────────────────────────────

void readDigitalInputs();
void runZoneLogic();
void applyOutputs();
void readRS485Sensors();
void readPZEM();
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
  // - Serial1.begin(9600) for PZEM
  // - Serial2.begin(9600) for RS485
  // - rs485.begin(1, Serial2) — start with sensor addr 1
  // - Bridge.begin()
  // - analogReadResolution(12)
}

void loop() {
  // TODO: implement main control loop (~100ms cycle)
  // readDigitalInputs();
  // runZoneLogic();
  // applyOutputs();
  // readRS485Sensors();   // poll less frequently, e.g. every 10s
  // readPZEM();           // poll less frequently, e.g. every 10s
  // exposeToBridge();
  // handleBridgeCommands();
  // delay(100);
}
