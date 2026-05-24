
# Control Logic — Uno Q HVAC Controller

## Overview

The Unico mini-duct system is a heat pump (Mitsubishi variable speed compressor)
with auxiliary electric resistance heat. Control is split:
- **Main thermostat** drives the Unico compressor and air handler.
- **Theater and downstairs thermostats** only open/close their zone dampers.
- All temperature/humidity threshold decisions are made on the Linux side and
  delivered to the MCU as pre-computed boolean SensorFlags (see system-design.md §5d).

---

## Relay reference

| Output        | Pin | Notes                                              |
|---------------|-----|----------------------------------------------------|
| high_cool     | D11 | Unico Y2 — high-stage cooling                      |
| low_cool      | D12 | Unico Y1 — low-stage cooling                       |
| high_heat     | D13 | Aux electric heater                                |
| reversing_valve | D14 | **B-type** — HIGH during heating, LOW during cooling |
| theater_damper | D15 | Theater zone damper open                          |
| downstairs_damper | D16 | Downstairs zone damper open                   |
| vent_out      | D17 | Fresh-air vent actuator open                       |
| dehumidifier  | D18 | Dehumidifier on                                    |
| fan           | D19 | Unico G wire — fan-only (no compressor)            |

---

## Rules

### 1. Zone authority
The main thermostat controls the Unico unit (compressor stages, heat, fan).
The theater and downstairs thermostats **only** modulate the damper position
for their respective rooms — they do not call the Unico directly.

### 2. Heat call — heat pump (outdoor temp ≥ 40 °F)
When the main thermostat calls for heat **and** `SensorFlags.heatPumpOk` is true
(outdoor ≥ configurable 40 °F threshold):
- Turn on `reversing_valve` (B-type — energize for heating)
- Call `low_cool` (low-stage heat pump in heating mode)
- Turn on `fan`

### 3. Heat call — heat pump not keeping up
If the main thermostat is calling for heat and `SensorFlags.tempRisingFast` is
**false** (indoor temperature not rising ≥ 1 °F / 15 min as measured by Linux):
- Escalate from `low_cool` to `high_cool` (high-stage heat pump)
- Keep `reversing_valve` on, keep `fan` on

### 4. Heat call — outdoor below aux heat threshold (outdoor temp < 40 °F)
When `SensorFlags.auxHeatNeeded` is true (outdoor < configurable 40 °F threshold):
- Apply the same heat pump logic as rules 2–3 **plus**
- Also energize `high_heat` (aux electric heater runs simultaneously)
- The compressor breaker and the aux heater breaker are separately sized to
  handle simultaneous operation.

### 5. Cool call — no humidity alert
When the main thermostat calls for cooling and `high_humidity` input is LOW:
- Call `low_cool`
- Turn on `fan`
- `reversing_valve` remains de-energized (B-type = cooling)

### 6. Cool call — humidity alert active
When the main thermostat calls for cooling and `high_humidity` input is HIGH:
- Call `high_cool` (the Mitsubishi variable-speed compressor requires high stage
  to achieve meaningful moisture removal; low stage is ineffective for dehumidification)
- Turn on `fan`
- `reversing_valve` remains de-energized

### 7. Humidity alert — no cooling call (dehumidifier phase)
When `high_humidity` input is HIGH but **no** cooling call is active:
- Turn on `dehumidifier`
- Turn on `fan` (air handler moves conditioned air past dehumidifier outlet)
- Do **not** run `low_cool` or `high_cool` (dehumidifier warm-air outlet feeds
  into the air handler before the condenser coil — running the compressor
  simultaneously would heat the coils)
- Record the time the dehumidifier turned on (`dehumStartMs`)

### 8. Humidity alert — dehumidifier timeout
If the dehumidifier has been running (per rule 7) for at least
`cfg.dehumMaxMinutes` (default 20 minutes) and `high_humidity` is still HIGH:
- Turn off `dehumidifier`
- Call `high_cool` until `high_humidity` clears
- Once `high_humidity` goes LOW, return to idle (rule 14)

### 9. Mutual exclusion — dehumidifier and compressor
- When `dehumidifier` is ON: `low_cool` and `high_cool` must both be OFF.
  `fan` must be ON.
- When `low_cool` or `high_cool` is ON: `dehumidifier` must be OFF.
  (Dehumidifier exhaust is warm air fed into the air handler upstream of the
  condenser coil. Running both simultaneously degrades cooling efficiency.)

### 10. Zone dampers — general authority
The main thermostat is the sole authority over the Unico unit. Secondary zone
thermostats (theater, downstairs) **only** open or close their room damper —
they never change what the compressor or fan does.

The **default damper state is open**. A secondary zone damper closes only when
that zone is actively requesting the opposite of what the main thermostat is
currently running. This prevents forcing unwanted hot or cold air into a room
while still allowing conditioned air to circulate freely to satisfied rooms.

| Secondary zone state | Main unit mode | Damper action |
|----------------------|----------------|---------------|
| Not calling (satisfied) | Any          | **Open** |
| Calling heat         | Heating        | **Open** |
| Calling cool         | Cooling        | **Open** |
| Calling heat         | Cooling        | **Closed** |
| Calling cool         | Heating        | **Closed** |

Theater damper is also gated by `cfg.theaterEnabled` — if the theater zone is
disabled in config, its damper stays open unconditionally.

### 11. Zone dampers — no Unico call (idle)
When the main thermostat is not calling for heat or cool (idle / fan-only mode),
both secondary zone dampers remain **open** for whole-house circulation.

### 12. Ventilation — outdoor humidity block
If outdoor humidity ≥ 80 % (`SensorFlags.ventBlocked` is true), `vent_out` must
remain OFF regardless of all other conditions.

### 13. Ventilation — free cooling
If `SensorFlags.ventOk` is true (outdoor temp < 60 °F **and** outdoor humidity
< 80 %) and rule 12 is not blocking, open `vent_out` to bring in free cool air.

### 14. Idle — no heat or cool call
When no thermostat is calling for heat or cooling:
- Run `fan` (continuous air circulation)
- Both zone dampers are open by default (rule 10 — no conflicting call, so no reason
  to close them); conditioned air circulates to all rooms
- Apply vent rules 12–13 (open vent if conditions allow fresh air)
- All compressor outputs (`low_cool`, `high_cool`, `high_heat`) remain OFF

### 15. Fresh air guidance
The house is approximately 4,200 sq ft with 10 ft ceilings (~42,000 cu ft of
space). Maximize fresh air intake subject to constraints:
- Rule 12 always blocks vent when outdoor humidity ≥ 80 %
- Rule 13 opens vent for free cooling when conditions are favourable
- The vent can also be opened on a configurable timer (`cfg.ventMinPerHour`)
  independently of temperature/humidity conditions
- The `vent_open_in` hardware input from an external humidity controller timer
  (D10) can also open the vent as an OR with the internal timer

---

## Safety interlocks (enforced in firmware regardless of logic above)

1. `low_cool`, `high_cool`, and `high_heat` are **mutually exclusive** with each
   other — only one may be active at a time. `high_heat` (aux electric) may run
   simultaneously with heat pump stages during cold-weather heating (rule 4).
2. A **180-second lockout** follows any compressor mode change. No further mode
   change is permitted during this window.
3. `reversing_valve` is de-energized whenever the system is in cooling mode or
   fully off (B-type wiring confirmed for this installation).
4. All output pins are driven LOW in `setup()` before any logic runs to prevent
   relay chatter on power-up.
