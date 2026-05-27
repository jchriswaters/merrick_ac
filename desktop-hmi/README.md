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

## Phase 2 — input simulation (future)

The WebSocket already accepts inbound messages (currently logged and
ignored).  To add input simulation:

1. Add an MCU-side RPC method (e.g. `set_input_override(bitmask,
   force_value_mask)`) that overrides physical pin reads.
2. Render a small toggle on each input card in the UI.
3. On toggle, send `{type: "override", key: "input_main_low_cool",
   force: true}` over the WebSocket.
4. Server handler calls the new RPC.

The CSS already has `.iocard` styling that will work as a clickable
button — the framework is wired up, just the MCU-side RPC and the
toggle UI are missing.

## Troubleshooting

| Symptom | What to check |
|---|---|
| `Controller unreachable` LED red | Wrong IP in `config.json`, or controller off/rebooting |
| `Connected` but inputs/outputs show `···` | RPC calls failing — usually transient; check `arduino-router.service` on the board |
| Sensor data shows `—` | `mosquitto_sub` returning no message — make sure `hvac-bridge.service` is publishing |
| WebSocket disconnects every few seconds | Network instability between laptop and controller |
| Connection refused on port 8000 | Server crashed or didn't start — re-run `run.bat` and look at the console log |
