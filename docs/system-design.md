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
│  │  • Read 9 digital inputs │    │  • paho-mqtt client      │   │
│  │  • Drive 8 relay outputs │    │  • Flask web config API  │   │
│  │  • Zone + humidity logic │    │  • JSON config store     │   │
│  │  • Expose state via RPC  │    │  • Poll RS485 bus        │   │
│  │                          │    │    (sensors + power mon) │   │
│  │                          │    │  WiFi 5 dual-band        │   │
│  │                          │    │  (WCBN3536A onboard)    │   │
│  └──────────────────────────┘    └─────────────────────────┘   │
│              ▲                          Arduino Bridge RPC       │
└──────────────┼──────────────────────────────────────────────────┘
               │
     ┌─────────┴──────────┐
     │  Physical I/O      │
     │  9 inputs (24VAC)  │
     │  8 relay outputs   │
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
24VAC thermostat call (e.g. Y1, W) ──► Relay coil terminal A
24VAC common (C wire)               ──► Relay coil terminal B
  → When thermostat calls, relay energizes

Relay contact terminal 1 ──► Arduino 3.3V pin
Relay contact terminal 2 ──► MCU input pin (reads HIGH = 3.3V when relay closed)
MCU pin configured as INPUT_PULLDOWN
```
Use **9× DIN-rail relay modules with 24VAC coils** — one per input channel.
The relay coils are driven directly by the 24VAC thermostat signals. No AC-DC
conversion module is required. The contact side uses Arduino 3.3V (not 5V) —
the STM32U585 input pins are 3.3V maximum.

**Input logic is active-HIGH:** pin reads HIGH (3.3V) when the thermostat is calling,
LOW when not calling. This is the opposite of the original optocoupler design.

---

## 3. Digital Outputs — Pin Assignment

All outputs drive one channel of an **8-channel 5V optocoupler relay board**.
The relay contacts switch 24 VAC control circuits for the HVAC equipment.

| MCU Pin | Signal Name           | Load controlled              | Notes                          |
|---------|-----------------------|------------------------------|--------------------------------|
| D11     | high_cool             | Unico Y2 — high-stage cool   | Relay NC → NO when active      |
| D12     | low_cool              | Unico Y1 — low-stage cool    |                                |
| D13     | high_heat             | Unico W / W2 — heat stage    |                                |
| D14     | reversing_valve       | Unico O/B — heat pump rev.   | B-type: energize in HEAT mode  |
|         |                       |                               | (OFF during cooling, ON during heating) |
| D15     | theater_damper_open   | Theater zone powered damper  |                                |
| D16     | downstairs_damper_open| Downstairs zone powered damper|                               |
| D17     | vent_open_out         | Fresh-air vent actuator       |                                |
| D18     | dehumidifier_on       | Dehumidifier relay            |                                |
| D19     | fan_on                | Unico air handler G wire      | Fan-only mode (no cooling/heat)|

**Relay board wiring note:** Use a board with a VCC/VDD jumper that separates relay
coil power from the logic input. Connect logic input VCC to the Arduino **3.3V pin**;
relay coil VCC to the Arduino **5V pin** (available from the onboard regulator when
powered via VIN). No external 5V supply is required.

**CRITICAL SAFETY INTERLOCKS (enforced in firmware):**
- `high_cool`, `low_cool`, and `high_heat` are **mutually exclusive** with heat outputs.
  Never allow cooling and heating relays active simultaneously.
- Minimum **180 seconds (3 minutes)** must elapse between any mode change to protect
  the compressor from short-cycling.
- `reversing_valve` uses **B-type behavior**: energized during any **heating** call,
  de-energized during cooling. This is confirmed for the Unico/Mitsubishi system in
  this installation (O/B wire is OFF when thermostat calls for cooling, ON for heating).

---

## 3.5. Power Distribution

A single 12V DIN-rail PSU is the only external DC supply needed. The Arduino's
onboard regulator provides 5V and 3.3V from the 12V VIN input. No buck converter
or AC-DC conversion module is required.

```
AC mains ──► 12V DIN-rail PSU (2A)
                │
                ├──► Uno Q VIN pin (via shield screw terminal)
                │      GND ──► Uno Q GND pin
                │
                └──► RS485 sensor VCC (via 4-conductor cable to sensor chain)
                       GND ──► RS485 sensor GND

Uno Q 5V pin  ──► Output relay board coil VCC   (5V relay coils)
Uno Q 3.3V pin ──► Output relay board logic VCC  (3.3V MCU logic)
               ──► Input relay contacts (one terminal per relay → MCU input pin)

HVAC 24VAC thermostat signals ──► Input relay coils (directly — no conversion needed)
SDM120 energy meters ──► powered from AC mains circuits they monitor
```

**Key points:**
- The VIN pin accepts 7–24V DC. 12V is recommended — keeps the onboard regulator cool.
- Do **not** connect both VIN and USB-C simultaneously as primary power sources.
  USB-C can remain connected for serial monitoring during commissioning.
- The 5V pin is shared between the Linux processor and the output relay coils. If
  instability occurs when multiple relays are active, add a dedicated external 5V
  supply for the relay board coil VCC.
- Input relay contacts are galvanically isolated from 24VAC — 3.3V on the contact
  side is the only voltage that reaches the MCU input pins.

---

## 4. Sensor and Power Monitor Bus

### RS485 Modbus RTU Bus — All Field Devices

**Read entirely by the Linux side (QRB2210) via USB-RS485 adapter. The MCU is not
involved in any sensor or power monitoring.**

All four field devices share a single RS485 Modbus RTU bus connected to the QRB2210
via a USB-RS485 adapter. The `bridge_daemon.py` polls all devices and assembles the
full MQTT payload. The MCU only handles digital I/O (thermostat inputs and relay outputs).

```
QRB2210 USB port ──► Waveshare USB-RS485 adapter (/dev/ttyUSB0) ──► RS485 bus (daisy-chain)
                                                │
                                                ├── SDM120 #1, addr 0x03 (AC system)
                                                ├── SDM120 #2, addr 0x04 (dehumidifier)
                                                ├── SHT30 #1,  addr 0x01 (indoor)
                                                └── SHT30 #2,  addr 0x02 (outdoor) ──► 120Ω
```

**Recommended daisy-chain order:** Start at the USB-RS485 adapter, wire through the
SDM120 energy meters (DIN-rail mounted in the enclosure), then exit the enclosure and
continue to the indoor then outdoor SHT30 sensor. Terminate with a 120Ω resistor
across A and B at the outdoor sensor (far end of the bus).

**Cable:** 4-conductor, 22 AWG unshielded twisted pair — fine for a 4m run at 9600 baud.
Use the twisted pair for RS485 A/B. The other two conductors carry 12V and GND for
SHT30 sensor power only. SDM120 meters are self-powered from AC mains.

**Devices on this bus:**

| Device | Modbus Addr | Address set by | Location / Notes |
|--------|-------------|----------------|------------------|
| Eastron SDM120 Modbus | 0x03 | Front panel buttons | In enclosure — AC system (compressor + air handler) |
| Eastron SDM120 Modbus | 0x04 | Front panel buttons | In enclosure — Dehumidifier circuit |
| RS485 SHT30 Temp/Hum xmitter | 0x01 | DIP switch | Indoor — return air location |
| RS485 SHT30 Temp/Hum xmitter | 0x02 | DIP switch | Outdoor — shaded, weatherproof enclosure |

**SDM120 wiring (per unit):**
- **L + N terminals:** connected to the AC mains circuit being monitored (powers the meter)
- **CT clamp:** clamped around the **live wire only** of the circuit being monitored
- **RS485 A/B terminals:** daisy-chained on the bus
- Ships with default address 0x01 — reprogram to 0x03 / 0x04 via front panel before
  installation (hold setup button to enter address programming mode)

**Data available per SDM120:**
- Voltage (V), Current (A), Active Power (W), Apparent Power (VA),
  Power Factor, Frequency (Hz), Import Energy (kWh)

**SHT30 sensor notes:**
- Factory calibrated. No user calibration required.
- Address set by physical DIP switch — set *before* installing.
- Each transmitter provides: temperature (°F), relative humidity (%RH), dew point (°F).

---

## 5. Control Logic

### 5a. Zone Logic

The system has three zones: **main** (always active), **downstairs** (always active),
**theater** (software-gated by `theaterEnabled` configuration flag).

**The main thermostat is the sole authority over the Unico unit.** Secondary zone
thermostats (theater, downstairs) never change what the compressor or fan does —
they only open or close the damper for their room.

**Default damper state is open.** A secondary zone damper closes *only* when the
zone thermostat is actively calling for the opposite of what the main is currently
running (to avoid forcing unwanted hot or cold air into a room that doesn't want it).

| Secondary zone state | Main unit mode | Damper |
|----------------------|----------------|--------|
| Not calling (satisfied) | Any          | **Open** |
| Calling heat         | Heating        | **Open** |
| Calling cool         | Cooling        | **Open** |
| Calling heat         | Cooling        | **Closed** — protect room from unwanted cold |
| Calling cool         | Heating        | **Closed** — protect room from unwanted heat |

### 5b. Mode Arbitration

Priority order when multiple zones call simultaneously:

1. **Conflict detection:** If any zone calls for heat AND any zone calls for cool
   simultaneously, heating takes priority and cooling is suppressed.
2. **High cool:** Active when main_high_cool is asserted (main thermostat Y2 only —
   zone thermostats do not trigger high-stage cooling).
3. **Low cool:** Active when any zone calls for cooling (main Y1, theater Y, downstairs Y).
4. **Heat:** Active when any zone calls for heat.
5. **Fan only:** Fan runs, no compressor — used for dehumidifier assist and idle circulation.
6. **Off:** No zone calling.

**Reversing valve (B-type):** `reversing_valve` (D14) is energized during **heating**,
de-energized during cooling. This is the confirmed behavior for this Unico/Mitsubishi
installation (O/B wire OFF = cooling, ON = heating).

**Fan relay:** `fan_on` (D19) drives the Unico air handler G wire. It is energized
whenever `low_cool`, `high_cool`, or `high_heat` is active, and also independently
for fan-only mode (dehumidifier assist, idle circulation). The compressor stages
always imply fan on, but fan can run without a compressor stage.

**Note on zone dampers in mode arbitration:** Secondary zone dampers are not
determined by the zone arbitration priority list above — they follow the separate
table in §5a. The Unico operating mode is determined solely by the main thermostat.

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

### 5d. Sensor Flags — Linux-to-MCU Bridge

The MCU has no direct access to temperature or humidity sensor data. The Linux side
(`bridge_daemon.py`) evaluates all environmental thresholds and pushes **five pre-computed
boolean flags** to the MCU via Arduino Bridge RPC every polling cycle (~10 s). The MCU
stores these in a `SensorFlags` struct and uses them in `runZoneLogic()`.

| Flag            | Set when                                          | Used by rule(s)          |
|-----------------|---------------------------------------------------|--------------------------|
| `heatPumpOk`    | Outdoor temp ≥ 40 °F                              | 2 — heat pump only       |
| `auxHeatNeeded` | Outdoor temp < 40 °F                              | 4 — add aux electric heat|
| `tempRisingFast`| Indoor temp rising ≥ 1 °F / 15 min               | 3 — stay on low_cool     |
| `ventOk`        | Outdoor temp < 60 °F **AND** outdoor hum < 80 %  | 13 — free cooling vent   |
| `ventBlocked`   | Outdoor humidity ≥ 80 %                           | 12 — vent must stay off  |

Thresholds are configurable via the web API and stored in `/etc/hvac/config.json`.
The `tempRisingFast` flag requires the Linux side to maintain a rolling 15-minute
indoor temperature history from the RS485 SHT30 sensor (addr 0x01).

---

## 6. Linux-Side Services

### 6a. Bridge Daemon (`linux/bridge_daemon.py`)

- Polls MCU relay state every **10 seconds** via Arduino Bridge RPC
- Polls all RS485 devices (2x SHT30, 2x SDM120) via USB-RS485 adapter
- Assembles full MQTT payload: relay states + sensor readings + power data
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
| D11–D19 | Output    | Relay control               | Digital        |
| D0/D1   | I/O       | (spare — Serial1 not used)  | —              |
| D20/SDA | I/O       | (spare)                     | —              |
| Serial2 | UART      | (spare — not accessible on Uno Q shield headers) | — |
| SCL     | I/O       | (spare — available)         | —              |
| A0–A5   | Analog    | All spare                   | ADC            |

---

## 8. Development Notes for Claude Code

- Use the **Arduino Bridge library** for MCU↔Linux communication, not raw Serial
- All MCU output pins should be explicitly set LOW in `setup()` before any logic runs —
  prevents relay chatter on boot
- All sensors and power monitors are read by `bridge_daemon.py` on the Linux side
  via the USB-RS485 adapter — the MCU sketch contains **no sensor code at all**
- D0/D1 (Serial1) are unused and available as spare pins
- Thermostat inputs use **active-HIGH logic** (relay contact closes → pin reads HIGH).
  Configure all input pins as `INPUT_PULLDOWN` so they read LOW when the relay is open
- `analogReadResolution(12)` should be called in setup() to enable 12-bit ADC on STM32
