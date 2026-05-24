# System Design — Uno Q HVAC Controller

## 1. Architecture Overview

The system is split across the two processors on the Uno Q board:

```
┌─────────────────────────────────────────────────────────────────┐
│                        Arduino Uno Q                            │
│                                                                 │
│  ┌──────────────────────────┐    ┌─────────────────────────┐   │
│  │   STM32U585 MCU          │    │  QRB2210 Linux (Debian) │   │
│  │   (Zephyr / Arduino)     │◄──►│                         │   │
│  │                          │    │  • paho-mqtt client      │   │
│  │  • Read 9 digital inputs │    │  • paho-mqtt client      │   │
│  │  • Drive 8 relay outputs │    │  • Flask web config API  │   │
│  │  • Poll PZEM UART bus    │    │  • JSON config store     │   │
│  │  • Zone + humidity logic │    │  • Poll RS485 sensors    │   │
│  │  • Expose state via RPC  │    │    (USB-RS485 adapter)   │   │
│  │                          │    │  WiFi 5 dual-band        │   │
│  └──────────────────────────┘    └─────────────────────────┘   │
│              ▲                          Arduino Bridge RPC       │
└──────────────┼──────────────────────────────────────────────────┘
               │
     ┌─────────┴──────────┐
     │  Physical I/O      │
     │  9 inputs (24VAC)  │
     │  8 relay outputs   │
     │  UART (PZEM)       │
     └────────────────────┘
```

### Inter-Processor Communication

The STM32 and Linux side communicate via **Arduino Bridge** — an RPC layer built on
internal UART + SPI. The Linux daemon calls MCU functions to read state and push
configuration changes. This is the official Uno Q mechanism; do not use bare Serial
for MCU↔Linux communication.

---

## 2. Digital Inputs — Pin Assignment

All inputs are 24 VAC thermostat signals. Each must pass through an **optocoupler
isolation module** (PC817-based) before reaching the MCU pin. The opto output is
pulled up to 3.3V; when the thermostat closes, the pin reads LOW (active-low logic).

| MCU Pin | Signal Name       | Source                        | Notes                        |
|---------|-------------------|-------------------------------|------------------------------|
| D2      | main_low_cool     | Main thermostat Y1            | Low-stage cooling call       |
| D3      | main_high_cool    | Main thermostat Y2            | High-stage cooling call      |
| D4      | main_heat         | Main thermostat W             | Heating call                 |
| D5      | theater_cool      | Theater thermostat Y          | Gated by theaterEnabled cfg  |
| D6      | theater_heat      | Theater thermostat W          | Gated by theaterEnabled cfg  |
| D7      | downstairs_cool   | Downstairs thermostat Y       |                              |
| D8      | downstairs_heat   | Downstairs thermostat W       |                              |
| D9      | high_humidity     | External humidity controller  | Active HIGH = humidity high  |
| D10     | vent_open_in      | Humidity controller timer out | Timer-driven or always use   |
|         |                   |                               | Arduino-internal vent timer  |

**Input signal conditioning circuit (per channel):**
```
24VAC signal ──► PC817 opto LED (with 1kΩ current-limiting resistor in series)
PC817 collector ──► MCU pin (INPUT_PULLUP, reads LOW when thermostat calls)
PC817 emitter ──► GND
```
Use an **8-channel PC817 optocoupler board** for inputs D2–D9, and a single-channel
board or the 8th spare channel for D10.

Power the opto **input side** from a small **24VAC→5VDC module** tapped off the
existing HVAC 24VAC transformer (the same transformer that powers the thermostats).

---

## 3. Digital Outputs — Pin Assignment

All outputs drive one channel of an **8-channel 5V optocoupler relay board**.
The relay contacts switch 24 VAC control circuits for the HVAC equipment.

| MCU Pin | Signal Name           | Load controlled              | Notes                          |
|---------|-----------------------|------------------------------|--------------------------------|
| D11     | high_cool             | Unico Y2 — high-stage cool   | Relay NC → NO when active      |
| D12     | low_cool              | Unico Y1 — low-stage cool    |                                |
| D13     | high_heat             | Unico W / W2 — heat stage    |                                |
| D14     | reversing_valve       | Unico O/B — heat pump rev.   | Energize in COOL mode (O-type) |
|         |                       |                               | Verify O vs B for your unit    |
| D15     | theater_damper_open   | Theater zone powered damper  |                                |
| D16     | downstairs_damper_open| Downstairs zone powered damper|                               |
| D17     | vent_open_out         | Fresh-air vent actuator       |                                |
| D18     | dehumidifier_on       | Dehumidifier relay            |                                |

**Relay board wiring note:** Use a board with a VCC/VDD jumper that separates relay
coil power from the logic input. Connect logic input to 3.3V; relay coil power to 5V.
This is required because the STM32U585 is a 3.3V device.

**CRITICAL SAFETY INTERLOCKS (enforced in firmware):**
- `high_cool`, `low_cool`, and `high_heat` are **mutually exclusive** with heat outputs.
  Never allow cooling and heating relays active simultaneously.
- Minimum **180 seconds (3 minutes)** must elapse between any mode change to protect
  the compressor from short-cycling.
- `reversing_valve` follows the cooling state: energized during any cooling call,
  de-energized during heating. Verify O vs B wiring for your specific Unico model.

---

## 3.5. Power Distribution

A single 12V DIN-rail PSU is the only AC-DC converter needed for the enclosure.
A buck converter derives 5V for relay coil and PZEM logic power.

```
AC mains ──► 12V DIN-rail PSU (2A)
                │
                ├──► Uno Q VIN pin (via shield screw terminal)
                │      GND ──► Uno Q GND pin (via shield screw terminal)
                │
                ├──► RS485 sensor VCC (4m cable to sensor chain)
                │      GND ──► RS485 sensor GND
                │
                └──► Buck converter IN (12V)
                           │
                           └──► Buck converter OUT (5V)
                                    ├──► Relay board VCC (coil power)
                                    └──► PZEM logic VCC

HVAC 24VAC transformer ──► 24VAC→5VDC module ──► Opto board input (LED) side VCC
```

**Uno Q 3.3V pin** (from onboard regulator, via shield screw terminal):
- → Relay board logic input VCC (keeps relay logic at 3.3V, matching STM32U585 GPIO)

**Key points:**
- The VIN pin accepts 7–24V DC. 12V from the DIN-rail supply is the recommended choice
  — efficient for buck conversion to 5V and runs the onboard regulator cooler than 24V.
- Do **not** connect both VIN and USB-C simultaneously as primary power sources.
  USB-C can remain connected for serial monitoring during commissioning — the Uno Q
  will accept power from whichever source is higher, but avoid this in normal operation.
- Set the buck converter output to exactly 5.0V with a multimeter *before* connecting
  the relay board or PZEM modules.
- The 24VAC opto supply is galvanically isolated from the DC logic supply — this is
  intentional and required for safe 24VAC input signal conditioning.

---

## 4. Sensor Buses

### 4a. RS485 Modbus RTU Bus — Temperature + Humidity

**Read by the Linux side (QRB2210), not the MCU.**

The Arduino Uno Q form factor only exposes one accessible hardware UART (Serial1 on
D0/D1). Rather than use SoftwareSerial (unreliable for Modbus RTU), the RS485
temp/humidity sensors are read directly by the Linux side via a **USB-to-RS485 adapter**
plugged into the QRB2210's USB port. The `bridge_daemon.py` polls both sensors and
includes readings in the MQTT payload. The MCU is not involved.

The MAX3485 module, Serial2, and D20 direction-control pin are **not used** — D20/SDA
and Serial2 pins are free for future use.

```
QRB2210 USB port ──► USB-RS485 adapter ──► shielded twisted-pair ──► sensor chain
```

Terminate the far end of the RS485 cable with a **120Ω resistor** across A and B.
Use **4-conductor shielded 22 AWG cable** (e.g. Belden 9504 or equivalent) — two
conductors carry RS485 A/B (use the twisted pair if available), the other two carry
12V and GND for sensor power. RS485 signal lines carry no power themselves.
Shield drain wire connected to GND at the adapter end only.

**Sensors on this bus:**

| Device                        | Modbus Addr | DIP setting | Location         |
|-------------------------------|-------------|-------------|------------------|
| RS485 SHT30 Temp/Hum xmitter | 0x01        | DIP switch  | Indoor — return air location |
| RS485 SHT30 Temp/Hum xmitter | 0x02        | DIP switch  | Outdoor — shaded, weatherproof enclosure |

Each transmitter provides: temperature (°C or °F), relative humidity (%RH), dew point.
Factory calibrated. No user calibration required. Address set by physical DIP switch.

Both sensors share one daisy-chained cable. Set addresses via DIP switch *before*
installing — no software address programming needed.

### 4b. UART Modbus Bus — Power Monitoring (PZEM-004T)

The two PZEM-004T modules use **UART-level Modbus RTU** (not RS485 differential).
They connect directly to a **separate** MCU hardware UART (Serial1) — do **not**
share the RS485 bus with the temp/humidity sensors.

```
STM32 Serial1 TX ──► PZEM TX (both modules wired in parallel)
STM32 Serial1 RX ◄── PZEM RX (both modules wired in parallel)
```

**IMPORTANT:** The PZEM UART side is not galvanically isolated from AC mains on all
board variants. Verify your specific board includes optocouplers on the UART lines.
If not, add an external optocoupler between PZEM TX/RX and MCU pins.

Each PZEM module also requires:
- **L + N** terminals connected to the AC mains circuit it is monitoring
  (this powers the module's internal MCU — 5V on the UART side is not enough alone)
- **CT clamp** clamped around the **live wire only** (not neutral, not both)

**PZEM modules on this bus:**

| Device          | Modbus Addr | CT clamp location        | Circuit monitored    |
|-----------------|-------------|--------------------------|----------------------|
| PZEM-004T V3.0  | 0x01        | AC system live wire      | Compressor + air handler |
| PZEM-004T V3.0  | 0x02        | Dehumidifier live wire   | Dehumidifier         |

**One-time address programming:** Each PZEM ships with default address 0xF8. Flash
the address-programming sketch with one module connected at a time (AC power connected
to L/N terminals) to burn address 0x01 then 0x02. See code directory for sketch.

**Data available per PZEM module:**
- Voltage (V), Current (A), Power (W), Energy (kWh), Frequency (Hz), Power Factor

---

## 5. Control Logic

### 5a. Zone Logic

The system has three zones: **main** (always active), **downstairs** (always active),
**theater** (software-gated by `theaterEnabled` configuration flag).

Zone dampers follow the calling zone:
- `theater_damper_open` = HIGH when theater_cool OR theater_heat is active
  (and theaterEnabled = true)
- `downstairs_damper_open` = HIGH when downstairs_cool OR downstairs_heat is active

When only the main zone is calling, both dampers remain closed (main zone uses the
full duct capacity).

### 5b. Mode Arbitration

Priority order when multiple zones call simultaneously:

1. **Conflict detection:** If any zone calls for heat AND any zone calls for cool
   simultaneously, heating takes priority and cooling is suppressed.
2. **High cool:** Active when main_high_cool is asserted (main thermostat Y2 only —
   zone thermostats do not trigger high-stage cooling).
3. **Low cool:** Active when any zone calls for cooling (main Y1, theater Y, downstairs Y).
4. **Heat:** Active when any zone calls for heat.
5. **Off:** No zone calling.

Mode change interlock: after any output state change, a **180-second lockout** prevents
any further mode change. This protects the compressor from short-cycling.

### 5c. Humidity and Ventilation Logic

```
high_humidity input HIGH  →  dehumidifier_on = HIGH
                          →  if system is in cooling mode, prefer low_cool over high_cool
                             (low-stage removes more moisture per BTU)

vent_open_in input HIGH   →  vent_open_out = HIGH  (pass-through from humidity controller)
  OR
Arduino internal timer    →  vent_open_out = HIGH  (configurable minutes-per-hour)
```

The fresh-air vent timer is configurable via the web API (`/config/vent`). The
`vent_open_in` hardware input from the humidity controller can override or supplement
the internal timer — the OR logic means either source can open the vent.

---

## 6. Linux-Side Services

### 6a. Bridge Daemon (`linux/bridge_daemon.py`)

- Polls MCU state every **10 seconds** via Arduino Bridge RPC
- Expands compact MCU JSON keys to human-readable field names
- Adds derived fields: `mode` (heat/cool/off), `compressor_on` (ac_current_a > 5A),
  `timestamp` (Unix epoch)
- Publishes full payload to MQTT topic `home/hvac/status` as **retained** message

### 6b. Web Config API (`linux/web_config.py`)

Flask app on port 80. Endpoints:

| Method | Endpoint              | Action                                      |
|--------|-----------------------|---------------------------------------------|
| GET    | /config               | Returns current MCU state + config as JSON  |
| POST   | /config/theater       | `{"enabled": true/false}` — enable/disable theater zone |
| POST   | /config/vent          | `{"minutesPerHour": 10}` — set vent schedule |
| POST   | /config/mode-override | `{"mode": "off"}` — force system off (safety) |

Config changes are:
1. Validated by Flask
2. Written to `/etc/hvac/config.json` for persistence across reboots
3. Pushed to MCU via Arduino Bridge RPC call

### 6c. MQTT

Broker: external (Home Assistant, Mosquitto, or similar) on the local network.
Broker address configured in `/etc/hvac/config.json`.

Topics:
- `home/hvac/status` — published every 10 seconds, retained
- `home/hvac/cmd` — subscribed, accepts JSON commands (same schema as POST endpoints)

---

## 7. Pin Summary Table

| Pin     | Direction | Function                    | Bus / Protocol |
|---------|-----------|-----------------------------|----------------|
| D2–D10  | Input     | Thermostat + humidity signals | Digital (via opto) |
| D11–D18 | Output    | Relay control               | Digital        |
| D19     | I/O       | (spare)                     | —              |
| D20/SDA | I/O       | (spare — RS485 DE pin not needed; sensors read by Linux side) | — |
| Serial1 | UART      | PZEM-004T Modbus            | Modbus RTU     |
| Serial2 | UART      | (spare — not accessible on Uno Q shield headers) | — |
| SCL     | I/O       | (spare — available)         | —              |
| A0–A5   | Analog    | All spare                   | ADC            |

---

## 8. Development Notes for Claude Code

- Use the **Arduino Bridge library** for MCU↔Linux communication, not raw Serial
- All MCU output pins should be explicitly set LOW in `setup()` before any logic runs —
  prevents relay chatter on boot
- The PZEM library (`PZEM004Tv30`) requires a **hardware UART** — SoftwareSerial
  will not work reliably with two devices
- RS485 temp/humidity sensors are read by `bridge_daemon.py` on the Linux side via a
  USB-RS485 adapter — the MCU sketch does not include any RS485 or SHT30 code
- Prefer `INPUT_PULLDOWN` (not INPUT_PULLUP) for thermostat inputs if active-HIGH
  opto outputs are used; verify against your specific opto board's output logic
- `analogReadResolution(12)` should be called in setup() to enable 12-bit ADC on STM32
