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
│   ├── system-design.md             ← architecture, pin map, bus design, control logic
│   ├── component-list.md            ← full bill of materials with notes
│   └── mqtt-payload-spec.md         ← MQTT topic, field names, types, units
├── mcu/
│   └── hvac_controller/
│       └── hvac_controller.ino      ← STM32 sketch (to be developed in Claude Code)
└── linux/
    ├── bridge_daemon.py             ← IPC bridge + MQTT publisher (to be developed)
    └── web_config.py                ← Flask REST config API (to be developed)
```

## Quick-Start Context for Claude Code

- All design decisions, pin assignments, sensor choices, and control logic rules are in
  **`docs/system-design.md`** — read this first.
- The full bill of materials is in **`docs/component-list.md`**.
- The MQTT schema is in **`docs/mqtt-payload-spec.md`**.
- Code files in `mcu/` and `linux/` are stubs — ready to be fleshed out.
- All sensors and power monitors share a single **RS485 Modbus RTU bus** read by the
  Linux side via a USB-RS485 adapter — the MCU handles digital I/O only (no sensor code).
- The RS485 bus carries: 2× Eastron SDM120 energy meters + 2× SHT30 temp/humidity sensors.
- The STM32 communicates with the Linux side via **Arduino Bridge RPC** (not bare Serial).

## Key Constraints

- STM32U585 GPIO and analog pins are **3.3V only** — never exceed 3.3V on any pin
- All 24 VAC thermostat input signals must pass through **optocoupler isolation** before
  reaching MCU pins
- Heat and cool outputs are **hardware-interlocked** — never active simultaneously
- Compressor protection: **3-minute minimum delay** between mode changes
- For permanent installation, power the Uno Q via the **VIN pin (7–24V DC)** from a
  DIN-rail 12V supply wired to the VIN and GND screw terminals on the shield — more
  robust than USB-C for an enclosure. 12V is recommended (efficient buck conversion
  to 5V, cooler regulator). USB-C accepts standard 5V 3A (no Power Delivery required)
  and is suitable for bench/development use. No barrel jack on the Uno Q.
