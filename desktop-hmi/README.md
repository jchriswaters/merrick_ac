# HVAC HMI — Desktop Web App

A small web app that runs locally on your laptop and gives you a live
HMI-style dashboard of the HVAC controller.  Opens a single SSH
connection to the Uno Q, polls inputs and outputs over msgpack-RPC,
and pushes updates to your browser over WebSocket every ~1 second.

```
┌─────────────────────────────────────────────────────────────┐
│  ⚙ HVAC Controller HMI         ● Connected 192.168.1.197    │
├─────────────────────────────────────────────────────────────┤
│  CURRENT MODE: FAN   │ COMPRESSOR: STOPPED │ INDOOR — °F    │
│                      │                     │ OUTDOOR 74 °F   │
├──────────────────────┴─────────────────────┴────────────────┤
│  THERMOSTAT INPUTS                  HVAC OUTPUTS            │
│  ┌─────────────┐ ┌─────────────┐    ┌─────────────┐ ┌──┐    │
│  │ Main Y1     │ │ Main Y2     │    │ High Cool   │ │  │ …  │
│  │ OFF         │ │ OFF         │    │ ON          │ │  │    │
│  └─────────────┘ └─────────────┘    └─────────────┘ └──┘    │
│  …                                  …                       │
└─────────────────────────────────────────────────────────────┘
```

## Quick start (Windows)

```cmd
cd desktop-hmi
run.bat
```

First run: creates a Python venv, installs deps, and writes
`config.json` from the example.  Edit `config.json` with your
controller's IP / password, then re-run `run.bat`.

Subsequent runs: just starts the server — open
<http://localhost:8000> in any browser.

## Quick start (Mac / Linux)

```bash
cd desktop-hmi
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.json.example config.json
$EDITOR config.json
python server.py
```

## Configuration

`config.json` (gitignored — contains the controller password):

| Key | What |
|---|---|
| `controller_host` | IP or hostname of the Uno Q (DHCP may change this; check your router) |
| `controller_user` | SSH username (typically `arduino`) |
| `controller_password` | SSH password |
| `ssh_port` | Usually `22` |
| `router_socket` | Path to `arduino-router.sock` on the controller |
| `mqtt_status_topic` | MQTT topic the bridge daemon publishes to (`home/hvac/status`) |
| `poll_interval_s` | How often to poll inputs/outputs (default `1.0`) |
| `ssh_keepalive_s` | TCP keepalive on the SSH transport |

## How it works

```
Browser ── WebSocket ─→ FastAPI server (this laptop)
                                  │
                                  │ paramiko SSH (single transport)
                                  ▼
                          Uno Q (controller)
                            │       │
                            │       └── mosquitto_sub -C 1 -W 3
                            │              (latest retained status,
                            │               for sensor + power data)
                            │
                            └── direct-streamlocal channels →
                                /var/run/arduino-router.sock
                                msgpack-RPC: get_inputs / get_outputs
                                (for real-time input/output state)
```

Every poll cycle:

1. Open a new `direct-streamlocal` channel to the controller's Unix socket.
2. Send `get_inputs` and `get_outputs` msgpack-RPC requests in parallel.
3. Pull the latest retained `home/hvac/status` MQTT message via
   `mosquitto_sub -C 1`.
4. Merge into a single status snapshot.
5. Broadcast to every connected WebSocket client.

If the SSH connection drops, the next poll reconnects automatically.

## Input simulation (implemented)

Each input card has an **AUTO / FORCE ON / FORCE OFF** control. This lets
you simulate thermostat calls without physically triggering a thermostat,
to test the control logic:

- **AUTO** — the input reads the live hardware pin (normal operation).
- **FORCE ON / FORCE OFF** — the MCU ignores the hardware pin and uses
  the forced value instead.

When any input is forced, an amber **SIMULATION MODE** banner appears at
the top with a **Return all inputs to AUTO** button.

**This drives real equipment.**  A forced input flows through the same
`runZoneLogic()` on the MCU as a real thermostat call — so forcing "Main
Y1" actually engages the compressor's low-cool relay.  All firmware
safety interlocks still apply (3-minute compressor lockout, heat/cool
mutual exclusion), so simulation cannot violate equipment protection.
The override state is volatile — it clears on MCU reset/power-cycle.

### How it works

- MCU RPC `set_input_override(mask, value)` — `mask` bit i = override
  input i; `value` bit i = forced state.  `(0, 0)` clears all.
- MCU RPC `get_input_override()` → 9-char string (`-`=auto, `1`=on,
  `0`=off), polled each cycle so the UI stays in sync even if another
  client changes it.
- The browser sends `{type:"override", key, mode}` or
  `{type:"clear_overrides"}` over the WebSocket; the server reads the
  current override state, applies the one change, computes the new
  `(mask, value)`, and calls `set_input_override`.
- A REST fallback exists at `POST /api/override` with the same payload.

## Staleness indicator

Sensor cards (indoor / outdoor / AC power) come from the bridge daemon's
MQTT status, which only refreshes every ~10 s.  If that data is more than
30 s old (e.g. an MQTT hiccup or the bridge stalling), the affected cards
dim and show a **STALE** tag — so a frozen number is never mistaken for a
live-but-unchanged one.

## Troubleshooting

| Symptom | What to check |
|---|---|
| `Controller unreachable` LED red | Wrong IP in `config.json`, or controller off/rebooting |
| `Connected` but inputs/outputs show `···` | RPC calls failing — usually transient; check `arduino-router.service` on the board |
| Sensor data shows `—` | `mosquitto_sub` returning no message — make sure `hvac-bridge.service` is publishing |
| WebSocket disconnects every few seconds | Network instability between laptop and controller |
| Connection refused on port 8000 | Server crashed or didn't start — re-run `run.bat` and look at the console log |
