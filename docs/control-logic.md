
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

### 5. Cool call — stage 1 (Y1 only, no humidity alert)
When the main thermostat asserts only **Y1** (`input_main_low_cool`) and
`input_high_humidity` is LOW:
- Call `low_cool`
- Turn on `fan`
- `reversing_valve` remains de-energized (B-type = cooling)

### 6. Cool call — stage 2 (Y2 asserted, OR humidity alert)
When the main thermostat asserts **Y2** (`input_main_high_cool`), high-stage
cooling engages regardless of humidity.  A standard 2-stage thermostat
asserts Y1 first and then adds Y2 when stage 1 isn't keeping up — so Y2
implies "I need more cooling than Y1 alone provides."

Stage 2 also engages on `high_humidity` even with only Y1 active — the
Mitsubishi variable-speed compressor needs the high stage to achieve
meaningful moisture removal; low stage is ineffective for dehumidification.

In either case:
- Call `high_cool` (high-stage compressor)
- Turn on `fan`
- `reversing_valve` remains de-energized

### 7. Humidity alert — soft dehumidification (humidistat + indoor RH below low threshold)
When `high_humidity` input is HIGH, **no** cool/heat call is active, AND
indoor RH is below the first-level (low) threshold
(`SensorFlags.humidityModerate` is FALSE):
- Turn on `dehumidifier`
- Turn on `fan` (air handler moves conditioned air past dehumidifier outlet)
- Do **not** run `low_cool` or `high_cool` (dehumidifier warm-air outlet feeds
  into the air handler before the condenser coil — running the compressor
  simultaneously would heat the coils)
- Record the time the dehumidifier turned on (`dehumStartMs`)

### 7b. Humidity alert — moderate band (humidistat + indoor RH between thresholds)
When `high_humidity` input is HIGH, **no** cool/heat call is active, and
indoor RH is in the moderate band (≥ low threshold, < high threshold —
`SensorFlags.humidityModerate` TRUE, `humidityHigh` FALSE):
- Same outputs as rule 7 (dehumidifier + fan only).  The dehumidifier
  might not keep up, but the emergency rule (rule 8b) hasn't tripped yet.
  The timeout safety (rule 8) will catch a real failure.

### 8. Humidity alert — dehumidifier timeout
If the dehumidifier has been running (per rule 7 / 7b) for at least
`cfg.dehumMaxMinutes` (default 20 minutes) and `high_humidity` is still HIGH:
- Turn off `dehumidifier`
- Call `high_cool` until `high_humidity` clears
- Once `high_humidity` goes LOW, return to idle (rule 14)

### 8b. Indoor humidity emergency (indoor RH at or above high threshold)
When `SensorFlags.humidityHigh` is TRUE (indoor RH ≥
`cfg.indoor_humidity_high_pct`, default 65%) — regardless of the humidistat
input and regardless of any cooling thermostat call — the system forces
high cool:
- Turn on `high_cool`
- Turn on `fan`
- Turn off `dehumidifier` (mutual exclusion — rule 9)
- `reversing_valve` de-energized (cooling)

This is the emergency dehumidification path.  Indoor RH high enough to
trip this threshold means the dehumidifier alone is no longer sufficient,
and the AC's condenser coil is the most effective moisture-removal
device available.  Heat calls still take precedence over this rule
(unusual to have very high indoor humidity while heating).

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

While the Unico is actively heating or cooling, a secondary zone damper
opens **only** if that zone is actively calling for the SAME mode the main
thermostat is driving. Any zone that isn't asking for the current mode —
whether it's silent (room already at temperature) or asking for the
opposite direction — gets its damper closed. This concentrates the
conditioned airflow into the rooms that actually want it.

| Secondary zone state | Main unit mode | Damper action |
|----------------------|----------------|---------------|
| Calling heat            | Heating       | **Open** |
| Calling cool            | Cooling       | **Open** |
| Calling heat            | Cooling       | **Closed** |
| Calling cool            | Heating       | **Closed** |
| Not calling (satisfied) | Heating       | **Closed** |
| Not calling (satisfied) | Cooling       | **Closed** |
| Any                     | Idle / fan-only | **Open** (whole-house circulation) |

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
2. **Mode-reversal interlock (180 s).**  Restarting the heat-pump compressor
   in the **opposite direction** (cooling↔heating) within 180 s of the last
   stop is blocked, to give the reversing valve and refrigerant pressures
   time to equalize.  Restarting in the **same direction** is allowed
   immediately — the silent-then-on-again case doesn't risk the compressor.
   Live stage changes (low_cool ↔ high_cool while running) do not stop the
   compressor and are therefore never subject to the timer.
3. **Mode-reversal-while-running guard.**  If the reversing valve must
   change direction while the compressor is still spinning, the firmware
   forces the compressor off immediately and holds the valve in its current
   position.  The mode-reversal interlock above then gates the restart in
   the new direction.
4. `reversing_valve` is de-energized whenever the system is in cooling mode or
   fully off (B-type wiring confirmed for this installation).
5. All output pins are driven LOW in `setup()` before any logic runs to prevent
   relay chatter on power-up.
