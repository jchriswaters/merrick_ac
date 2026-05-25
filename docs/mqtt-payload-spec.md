# MQTT Payload Specification — Uno Q HVAC Controller

## Broker Configuration

| Parameter | Value |
|-----------|-------|
| Broker address | Configured in `/etc/hvac/config.json` on Linux side |
| Default port | 1883 (standard MQTT) |
| Authentication | Optional — configure in `/etc/hvac/config.json` |
| QoS | 0 (fire-and-forget) for status; 1 for commands |

---

## Topics Overview

| Topic | Direction | Retained | Purpose |
|-------|-----------|----------|---------|
| `home/hvac/status` | Uno Q → broker | Yes | Live system state, sensors, power — published every 10 s |
| `home/hvac/config` | Uno Q → broker | Yes | Current configuration / setpoints — published on any change |
| `home/hvac/cmd` | broker → Uno Q | No | Commands: config changes, mode override, energy reset |

---

## `home/hvac/status` — System Status (Published)

**Direction:** Uno Q → broker
**Interval:** Every 10 seconds
**Retained:** Yes
**Format:** JSON, single flat object

### Full Payload Example

```json
{
  "timestamp": 1748123456,
  "mode": "high_cool",
  "compressor_on": true,

  "high_cool": true,
  "low_cool": false,
  "high_heat": false,
  "reversing_valve": false,
  "fan_on": true,
  "theater_damper": false,
  "downstairs_damper": true,
  "vent_open": false,
  "dehumidifier_on": false,

  "indoor_temp_f": 72.4,
  "indoor_humidity_pct": 54.1,
  "indoor_dewpoint_f": 53.8,
  "outdoor_temp_f": 91.2,
  "outdoor_humidity_pct": 68.5,
  "outdoor_dewpoint_f": 79.3,

  "ac_voltage_v": 120.3,
  "ac_current_a": 18.3,
  "ac_power_w": 2198.0,
  "ac_energy_kwh": 142.731,
  "ac_frequency_hz": 60.0,
  "ac_power_factor": 0.99,

  "dehum_voltage_v": 120.1,
  "dehum_current_a": 6.1,
  "dehum_power_w": 732.0,
  "dehum_energy_kwh": 28.452,
  "dehum_frequency_hz": 60.0,
  "dehum_power_factor": 0.98
}
```

### Field Reference

**Control / metadata:**

| Field | Type | Units | Description |
|-------|------|-------|-------------|
| `timestamp` | integer | Unix epoch (s) | Time of reading on Linux side |
| `mode` | string | — | Derived: `"high_cool"`, `"low_cool"`, `"heat"`, `"fan"`, `"off"` |
| `compressor_on` | boolean | — | Derived: `ac_current_a > 5.0` |

**Digital output states (relay positions):**

| Field | Type | Description |
|-------|------|-------------|
| `high_cool` | boolean | Unico Y2 relay active |
| `low_cool` | boolean | Unico Y1 relay active |
| `high_heat` | boolean | Aux electric heater relay active |
| `reversing_valve` | boolean | O/B relay — **B-type**: true = heating, false = cooling |
| `fan_on` | boolean | Unico G wire relay active (fan-only or with compressor) |
| `theater_damper` | boolean | Theater zone damper open |
| `downstairs_damper` | boolean | Downstairs zone damper open |
| `vent_open` | boolean | Fresh-air vent actuator open |
| `dehumidifier_on` | boolean | Dehumidifier relay active |

**Environmental — indoor (RS485 SHT30 transmitter, addr 0x01):**

| Field | Type | Units | Notes |
|-------|------|-------|-------|
| `indoor_temp_f` | float | °F | 1 decimal place |
| `indoor_humidity_pct` | float | %RH | 1 decimal place |
| `indoor_dewpoint_f` | float | °F | Reported by transmitter |

**Environmental — outdoor (RS485 SHT30 transmitter, addr 0x02):**

| Field | Type | Units | Notes |
|-------|------|-------|-------|
| `outdoor_temp_f` | float | °F | 1 decimal place |
| `outdoor_humidity_pct` | float | %RH | 1 decimal place |
| `outdoor_dewpoint_f` | float | °F | Reported by transmitter |

**Power — AC system (Eastron SDM120, Modbus addr 0x03):**

| Field | Type | Units | Notes |
|-------|------|-------|-------|
| `ac_voltage_v` | float | V | 1 decimal place |
| `ac_current_a` | float | A | 2 decimal places |
| `ac_power_w` | float | W | 0 decimal places |
| `ac_energy_kwh` | float | kWh | 3 decimal places (accumulates — never resets in normal operation) |
| `ac_frequency_hz` | float | Hz | 1 decimal place |
| `ac_power_factor` | float | 0.00–1.00 | 2 decimal places |

**Power — dehumidifier (Eastron SDM120, Modbus addr 0x04):**

| Field | Type | Units | Notes |
|-------|------|-------|-------|
| `dehum_voltage_v` | float | V | 1 decimal place |
| `dehum_current_a` | float | A | 2 decimal places |
| `dehum_power_w` | float | W | 0 decimal places |
| `dehum_energy_kwh` | float | kWh | 3 decimal places |
| `dehum_frequency_hz` | float | Hz | 1 decimal place |
| `dehum_power_factor` | float | 0.00–1.00 | 2 decimal places |

### Null / Error Values

If a sensor read fails, the corresponding fields are omitted from the payload
(not published as null) to prevent downstream consumers from treating stale
readings as current. The `timestamp` field always appears.

---

## `home/hvac/config` — Configuration State (Published on change)

**Direction:** Uno Q → broker
**Retained:** Yes
**Trigger:** Published whenever any configuration value changes (via `home/hvac/cmd`
topic or web API). Also published once at startup so HMI and Home Assistant
always have current setpoints without needing to poll.

### Full Payload Example

```json
{
  "theater_enabled": false,
  "vent_minutes_per_hour": 10,
  "mode_override": "auto",
  "heat_pump_min_temp_f": 40,
  "free_cool_max_temp_f": 60,
  "high_humidity_pct": 80,
  "dehum_max_minutes": 20,
  "config_updated_at": 1748123000
}
```

### Field Reference

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `theater_enabled` | boolean | `false` | Theater zone damper logic active |
| `vent_minutes_per_hour` | integer | `10` | Fresh-air vent open time per hour (0 = disabled, 60 = always open) |
| `mode_override` | string | `"auto"` | `"auto"` = normal operation; `"off"` = force system off |
| `heat_pump_min_temp_f` | integer | `40` | Outdoor temp (°F) below which aux electric heat is also engaged |
| `free_cool_max_temp_f` | integer | `60` | Outdoor temp (°F) below which free-cooling vent is allowed to open |
| `high_humidity_pct` | integer | `80` | Outdoor humidity (%RH) at or above which fresh-air vent is forced closed |
| `dehum_max_minutes` | integer | `20` | Minutes dehumidifier runs alone before system switches to high_cool |
| `config_updated_at` | integer | — | Unix epoch (s) of last config change |

---

## `home/hvac/cmd` — Remote Commands (Subscribed)

**Direction:** broker → Uno Q
**QoS:** 1
**Format:** JSON

The Linux bridge daemon subscribes to this topic and translates commands into
config updates (written to `/etc/hvac/config.json`, pushed to MCU via Bridge RPC)
or direct MCU Bridge RPC calls. All commands are acknowledged by an immediate
republish of `home/hvac/config` with the updated values.

### Set a configuration parameter

The general-purpose setpoint command. Covers all fields in the config topic.

```json
{ "cmd": "setConfig", "key": "heat_pump_min_temp_f", "value": 38 }
{ "cmd": "setConfig", "key": "free_cool_max_temp_f",  "value": 55 }
{ "cmd": "setConfig", "key": "high_humidity_pct",      "value": 75 }
{ "cmd": "setConfig", "key": "dehum_max_minutes",      "value": 15 }
{ "cmd": "setConfig", "key": "vent_minutes_per_hour",  "value": 12 }
{ "cmd": "setConfig", "key": "theater_enabled",        "value": true }
```

Accepted keys and validation ranges:

| Key | Type | Min | Max |
|-----|------|-----|-----|
| `heat_pump_min_temp_f` | integer | 20 | 60 |
| `free_cool_max_temp_f` | integer | 40 | 80 |
| `high_humidity_pct` | integer | 50 | 100 |
| `dehum_max_minutes` | integer | 5 | 120 |
| `vent_minutes_per_hour` | integer | 0 | 60 |
| `theater_enabled` | boolean | — | — |

### Force system off / restore auto

```json
{ "cmd": "setConfig", "key": "mode_override", "value": "off" }
{ "cmd": "setConfig", "key": "mode_override", "value": "auto" }
```

### Reset SDM120 energy counters

```json
{ "cmd": "reset_energy", "circuit": "ac" }
{ "cmd": "reset_energy", "circuit": "dehum" }
{ "cmd": "reset_energy", "circuit": "all" }
```

Note: SDM120 energy registers are reset via a Modbus write command to the meter
directly from `bridge_daemon.py` — no MCU involvement.

---

## Integration Notes

### Elecrow CrowPanel HMI (ESP32-S3, local WiFi)

The round 2.1-inch touchscreen HMI connects to the same WiFi network as the
Uno Q and communicates **exclusively via MQTT** — no direct connection to the
Arduino controller.

- Subscribes to `home/hvac/status` — refreshes display every 10 s
- Subscribes to `home/hvac/config` — displays current setpoints; retained message
  ensures the HMI always has values even after a power cycle
- Publishes `setConfig` commands to `home/hvac/cmd` when the user adjusts a
  setpoint via the rotary knob

The HMI requires no firmware changes to the Uno Q when a setpoint is changed —
the MQTT command flows through the broker to `bridge_daemon.py` which handles
validation, persistence, and MCU notification.

### Home Assistant

Add to `configuration.yaml` to consume the status topic:

```yaml
mqtt:
  sensor:
    - name: "HVAC Indoor Temp"
      state_topic: "home/hvac/status"
      value_template: "{{ value_json.indoor_temp_f }}"
      unit_of_measurement: "°F"
      device_class: temperature

    - name: "HVAC Mode"
      state_topic: "home/hvac/status"
      value_template: "{{ value_json.mode }}"

    - name: "AC Power"
      state_topic: "home/hvac/status"
      value_template: "{{ value_json.ac_power_w }}"
      unit_of_measurement: "W"
      device_class: power

  number:
    - name: "Heat Pump Min Temp"
      state_topic: "home/hvac/config"
      value_template: "{{ value_json.heat_pump_min_temp_f }}"
      command_topic: "home/hvac/cmd"
      command_template: >
        {"cmd":"setConfig","key":"heat_pump_min_temp_f","value":{{ value | int }}}
      min: 20
      max: 60
      step: 1
      unit_of_measurement: "°F"
  # ... add remaining fields as needed
```

### Node-RED

Subscribe to `home/hvac/status` with a MQTT-in node.
Parse `msg.payload` as JSON. All fields are at the top level.
Use a separate MQTT-in node on `home/hvac/config` to display/edit setpoints.

### Grafana / InfluxDB

Use Telegraf with the MQTT consumer plugin to ingest `home/hvac/status` into
InfluxDB. Tag by `mode` for easy filtering of power consumption by operating mode.

---

## MCU → Linux Compact Key Map

The MCU exposes only relay output states via Bridge RPC (compact JSON to reduce
payload size). The Linux bridge daemon expands these short keys to human-readable
field names and merges them with sensor and power data read directly from the RS485
bus before publishing to MQTT.

| MCU key | Full field name        | Source        |
|---------|------------------------|---------------|
| `hc`    | `high_cool`            | MCU via Bridge |
| `lc`    | `low_cool`             | MCU via Bridge |
| `hh`    | `high_heat`            | MCU via Bridge |
| `rv`    | `reversing_valve`      | MCU via Bridge |
| `fn`    | `fan_on`               | MCU via Bridge |
| `td`    | `theater_damper`       | MCU via Bridge |
| `dd`    | `downstairs_damper`    | MCU via Bridge |
| `vo`    | `vent_open`            | MCU via Bridge |
| `dh`    | `dehumidifier_on`      | MCU via Bridge |

All sensor and power fields (`indoor_*`, `outdoor_*`, `ac_*`, `dehum_*`) are added
directly by `bridge_daemon.py` from RS485 reads — they are not sourced from the MCU.
