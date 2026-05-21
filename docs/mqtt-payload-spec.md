# MQTT Payload Specification — Uno Q HVAC Controller

## Broker Configuration

| Parameter | Value |
|-----------|-------|
| Broker address | Configured in `/etc/hvac/config.json` on Linux side |
| Default port | 1883 (standard MQTT) |
| Authentication | Optional — configure in `/etc/hvac/config.json` |
| QoS | 0 (fire-and-forget) for status; 1 for commands |

---

## Topics

### `home/hvac/status` — System Status (Published)

**Direction:** Uno Q → broker  
**Interval:** Every 10 seconds  
**Retained:** Yes (broker holds last value for new subscribers)  
**Format:** JSON, single flat object

#### Full Payload Example

```json
{
  "timestamp": 1748123456,
  "mode": "high_cool",
  "compressor_on": true,

  "high_cool": true,
  "low_cool": false,
  "high_heat": false,
  "reversing_valve": true,
  "theater_damper": false,
  "downstairs_damper": true,
  "vent_open": false,
  "dehumidifier_on": true,

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

#### Field Reference

**Control / metadata:**

| Field | Type | Units | Description |
|-------|------|-------|-------------|
| `timestamp` | integer | Unix epoch (s) | Time of reading on Linux side |
| `mode` | string | — | Derived: `"high_cool"`, `"low_cool"`, `"heat"`, `"off"` |
| `compressor_on` | boolean | — | Derived: `ac_current_a > 5.0` |

**Digital output states (relay positions):**

| Field | Type | Description |
|-------|------|-------------|
| `high_cool` | boolean | Unico Y2 relay active |
| `low_cool` | boolean | Unico Y1 relay active |
| `high_heat` | boolean | Unico W/W2 relay active |
| `reversing_valve` | boolean | O/B relay active (energized in cool) |
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

**Power — AC system (PZEM-004T addr 0x01):**

| Field | Type | Units | Notes |
|-------|------|-------|-------|
| `ac_voltage_v` | float | V | 1 decimal place |
| `ac_current_a` | float | A | 2 decimal places |
| `ac_power_w` | float | W | 0 decimal places |
| `ac_energy_kwh` | float | kWh | 3 decimal places (accumulates — never resets in normal operation) |
| `ac_frequency_hz` | float | Hz | 1 decimal place |
| `ac_power_factor` | float | 0.00–1.00 | 2 decimal places |

**Power — dehumidifier (PZEM-004T addr 0x02):**

| Field | Type | Units | Notes |
|-------|------|-------|-------|
| `dehum_voltage_v` | float | V | 1 decimal place |
| `dehum_current_a` | float | A | 2 decimal places |
| `dehum_power_w` | float | W | 0 decimal places |
| `dehum_energy_kwh` | float | kWh | 3 decimal places |
| `dehum_frequency_hz` | float | Hz | 1 decimal place |
| `dehum_power_factor` | float | 0.00–1.00 | 2 decimal places |

#### Null / Error Values

If a sensor read fails, the corresponding fields are omitted from the payload
(not published as null) to prevent downstream consumers from treating stale
readings as current. The `timestamp` field always appears.

---

### `home/hvac/cmd` — Remote Commands (Subscribed)

**Direction:** broker → Uno Q  
**QoS:** 1  
**Format:** JSON

The Linux bridge daemon subscribes to this topic and translates commands into
Arduino Bridge RPC calls to the MCU. Accepted command payloads:

#### Enable/disable theater zone

```json
{ "cmd": "theater", "enabled": true }
{ "cmd": "theater", "enabled": false }
```

#### Set ventilation schedule

```json
{ "cmd": "vent", "minutesPerHour": 10 }
```
Range: 0 (disabled) – 60 (always open).

#### Force system off

```json
{ "cmd": "mode_override", "mode": "off" }
```
Clears override with `"mode": "auto"`.

#### Reset PZEM energy counters

```json
{ "cmd": "reset_energy", "circuit": "ac" }
{ "cmd": "reset_energy", "circuit": "dehum" }
{ "cmd": "reset_energy", "circuit": "all" }
```

---

### `home/hvac/config` — Configuration State (Published on change)

**Direction:** Uno Q → broker  
**Retained:** Yes  
**Trigger:** Published whenever a config change is applied (via cmd topic or web API)

```json
{
  "theater_enabled": false,
  "vent_minutes_per_hour": 10,
  "mode_override": "auto",
  "config_updated_at": 1748123000
}
```

---

## Integration Notes

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
  # ... add remaining fields as needed
```

### Node-RED

Subscribe to `home/hvac/status` with a MQTT-in node.
Parse `msg.payload` as JSON. All fields are at the top level.

### Grafana / InfluxDB

Use Telegraf with the MQTT consumer plugin to ingest `home/hvac/status` into
InfluxDB. Tag by `mode` for easy filtering of power consumption by operating mode.

---

## MCU → Linux Compact Key Map

The MCU sends a compact JSON to reduce Bridge RPC payload size. The Linux bridge
daemon expands these short keys to the human-readable field names above:

| MCU key | Full field name        |
|---------|------------------------|
| `hc`    | `high_cool`            |
| `lc`    | `low_cool`             |
| `hh`    | `high_heat`            |
| `rv`    | `reversing_valve`      |
| `td`    | `theater_damper`       |
| `dd`    | `downstairs_damper`    |
| `vo`    | `vent_open`            |
| `dh`    | `dehumidifier_on`      |
| `it`    | `indoor_temp_f`        |
| `ih`    | `indoor_humidity_pct`  |
| `id`    | `indoor_dewpoint_f`    |
| `ot`    | `outdoor_temp_f`       |
| `oh`    | `outdoor_humidity_pct` |
| `od`    | `outdoor_dewpoint_f`   |
| `av`    | `ac_voltage_v`         |
| `aa`    | `ac_current_a`         |
| `aw`    | `ac_power_w`           |
| `ak`    | `ac_energy_kwh`        |
| `af`    | `ac_frequency_hz`      |
| `ap`    | `ac_power_factor`      |
| `dv`    | `dehum_voltage_v`      |
| `da`    | `dehum_current_a`      |
| `dw`    | `dehum_power_w`        |
| `dk`    | `dehum_energy_kwh`     |
| `df`    | `dehum_frequency_hz`   |
| `dp`    | `dehum_power_factor`   |
