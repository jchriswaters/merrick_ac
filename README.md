# HVAC Controller — Arduino Uno Q

A custom HVAC zone controller for a **Unico mini-duct system** with multi-zone damper control,
dehumidification, fresh-air ventilation, current/power monitoring, environmental sensing,
MQTT telemetry, and a web-based configuration API.

## Platform

**Arduino Uno Q** — dual-brain board:
- **STM32U585 MCU** (Arm Cortex-M33, 160 MHz) running Arduino Core on Zephyr OS
  — owns all GPIO, runs real-time control loop, reads sensors, drives relays
- **Qualcomm QRB2210 MPU** running full Debian Linux
  — runs MQTT client, Flask web config API, bridges state to/from MCU via Arduino Bridge RPC

## Repository Structure

```
hvac-controller/
├── README.md                        ← this file
├── docs/
│   ├── system-design.md             ← architecture, pin map, sensor bus, wiring diagram
│   ├── component-list.md            ← full bill of materials with notes
│   ├── control-logic.md             ← HVAC control rules (zone arbitration, interlocks)
│   ├── mqtt-payload-spec.md         ← MQTT topic, field names, types, units
│   └── deployment.md                ← MCU flash procedure + runtime setup
├── mcu/
│   └── hvac_controller/
│       └── hvac_controller.ino      ← STM32 sketch — production-ready
├── linux/
│   ├── bridge_daemon.py             ← Modbus poller + MQTT publisher + MCU RPC client
│   ├── hvac-bridge.service          ← systemd unit (User=arduino)
│   ├── web_config.py                ← Flask REST config API + web UI
│   ├── hvac-web.service             ← systemd unit for the web config API
│   ├── static/                      ← Flask static assets (web UI)
│   └── templates/                   ← Flask Jinja2 templates (web UI)
├── tools/
│   ├── flash_sketch.py              ← MCU flash via SSH+SWD (USE THIS — see below)
│   ├── fix_service.py               ← Applies arduino-router GPIO timing fix
│   └── *.py                          ← Assorted diagnostic scripts
├── hmi/
│   └── crowpanel_hvac/              ← ESP32 HMI display sketch (LVGL + MQTT)
└── sim/
    ├── hvac_sim.py                  ← Simulator for offline testing
    ├── set_sensor_address.py        ← One-shot tool to set RS485 sensor address
    └── test_sensor.py               ← Bench tool to read a single Modbus device
```

## Quick-Start Context for Claude Code

- All design decisions, pin assignments, sensor choices, and control logic rules are in
  **`docs/system-design.md`** — read this first.
- The full bill of materials is in **`docs/component-list.md`**.
- HVAC control logic rules are in **`docs/control-logic.md`**.
- The MQTT schema is in **`docs/mqtt-payload-spec.md`**.
- The MCU flash procedure (with all the Uno Q gotchas) and the systemd-side runtime
  setup are in **`docs/deployment.md`** — read before flashing.
- All sensors and power monitors share a single **RS485 Modbus RTU bus** read by the
  Linux side via a USB-RS485 adapter — the MCU handles digital I/O only (no sensor code).
- The RS485 bus carries: 2× Eastron SDM120 energy meters + 2× SHT30 temp/humidity sensors.
- The STM32 communicates with the Linux side via **Arduino_RouterBridge RPC** (msgpack
  over `/dev/ttyHS1`) — not the deprecated Yún `Bridge` library.

## Critical deployment gotchas

These were discovered the hard way and are documented in `docs/deployment.md`:

1. **The on-board `/opt/openocd/bin/arduino-flash.sh` is broken** for sketch upload —
   flashes to wrong address (`0x80F0000` vs. `0x8100000`), uses wrong file format
   (`.bin-zsk.bin` vs `.elf-zsk.bin`), and misses the `0xCAFFEEEE` magic write to
   `TAMP_CR1`. Symptom: flash appears to succeed but RPC calls return `'method get_outputs
   not available'` because the LLEXT never loads. **Use `tools/flash_sketch.py` instead.**

2. **The stock `Arduino_RouterBridge` library has an ASCII flush** in `BridgeClass::begin()`
   that breaks the msgpack handshake on the Uno Q's Go-based router. Must be removed
   before compile (see `docs/deployment.md` §2.3).

3. **A powered USB hub is required** between the Uno Q USB-C port and the USB-RS485 dongle.
   The Uno Q's Type-C port is locked in `sink` power role when VIN-powered, so it doesn't
   source VBUS to peripherals. The hub supplies its own VBUS to both ends. See
   `docs/deployment.md` §1.3.

4. **The factory `arduino-router.service` config has a GPIO reset race condition** —
   `tools/fix_service.py` patches it to assert SRST during router startup and release
   only via `--after-ready`.

## Key Constraints

- STM32U585 GPIO and analog pins are **3.3V only** — never exceed 3.3V on any pin
- All 24 VAC thermostat input signals are isolated via **24VAC relay coils** — contact
  side uses **3.3V only** (never 5V) connected to MCU input pins
- Heat and cool outputs are **hardware-interlocked** — never active simultaneously
- Compressor protection: **3-minute minimum delay** between mode changes
- For permanent installation, power the Uno Q via the **VIN pin (7–24V DC)** from a
  DIN-rail 12V supply. The onboard regulator provides 5V (output relay coils) and 3.3V
  (logic + input relay contacts) — no external buck converter needed. USB-C (5V 3A,
  no PD required) is suitable for bench use only. No barrel jack on the Uno Q.
