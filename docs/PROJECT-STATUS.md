# Project Status & Decision Log

**Read this first when resuming work.**  It's the current-state snapshot
and the "why" behind non-obvious decisions.  Keep it short — detailed
material lives in the other docs (linked below).  Update the "Current
status" and "Open items" sections at the end of each work session.

Last updated: 2026-06-01

---

## How to resume a session efficiently

Point Claude at the relevant doc(s) instead of pasting chat history:

- Design / architecture questions → `docs/system-design.md`
- Flashing / provisioning / runtime → `docs/deployment.md`
- HVAC control rules → `docs/control-logic.md`
- Bill of materials / wiring → `docs/component-list.md`, `docs/system-design.md` §4
- MQTT schema → `docs/mqtt-payload-spec.md`
- Desktop HMI app → `desktop-hmi/README.md`
- This file → current state + decisions

A focused doc read is far cheaper (time + tokens) than re-exploring the
codebase or pasting a transcript.

---

## Access / environment

| Thing | Value | Notes |
|---|---|---|
| Controller SSH | `arduino@192.168.1.197` / pass `piragua827` | IP pinned by DHCP reservation on the router. MAC `14:b5:cd:ea:d3:31`, hostname `MerrickAC`. |
| HMI URL | `http://192.168.1.197:8000` | Any browser on the LAN — phone, laptop, tablet. |
| Linux side | Debian on QRB2210 | runs arduino-router, hvac-bridge, mosquitto, hvac-hmi |
| MCU | STM32U585, Zephyr + Arduino LLEXT sketch | flashed via `tools/flash_sketch.py` |
| Dev machine | Windows laptop, Python 3.14 | Arduino CLI at `C:\Program Files\Arduino CLI\` |

---

## Current status (2026-05-31)

**Working:**
- MCU sketch runs; RPC methods `get_outputs` / `get_inputs` / `set_flags` /
  `set_config` registered and responding.
- `arduino-router.service` + `hvac-bridge.service` both active and enabled.
- `uno-q-usb-host-role.service` forces USB-C host role on boot.
- Powered USB hub → FTDI USB-RS485 → `/dev/ttyUSB0` enumerating.
- RS485 sensors: **indoor SHT30 (0x01) and outdoor SHT30 (0x02) both
  reading** live temp/humidity.
- MQTT status published to **HiveMQ cloud broker** (`broker.hivemq.com`,
  topic `jchriswaters_merrick_ac`) — feeds the mqtt-to-snowflake Snowflake
  pipeline on GCP.  Local mosquitto retained for command/config channels
  only (`home/hvac/cmd`, `home/hvac/config`).
- **HMI deployed on the controller** (`hvac-hmi.service`) — accessible at
  `http://192.168.1.197:8000` from any browser on the LAN.  No laptop
  needed.  Running in `local_mode=true`: RPC goes direct to the
  arduino-router Unix socket; sensor data reads `/tmp/hvac_status.json`
  written by bridge_daemon each cycle; commands publish to local mosquitto.

- Desktop HMI **input simulation** (Phase 2) — DONE.  AUTO / FORCE ON /
  FORCE OFF per input card; verified forcing Main Y1 engages low_cool and
  adding the humidistat switches to high_cool.  MCU RPCs
  `set_input_override` / `get_input_override` added.
- Desktop HMI **staleness indicator** on sensor cards — DONE.
- Desktop HMI **System Settings panel** — DONE.  All 12 configurable
  parameters editable from the HMI (see full list in bridge_daemon
  `SETCONFIG_SCHEMA`); writes via WebSocket → mosquitto_pub to
  `home/hvac/cmd`; bridge daemon validates, persists, and republishes
  to `home/hvac/config`.  Persistence now writes to
  `~/.config/hvac/config.json` (the previous `/etc/hvac/...` path failed
  silently — the service runs as `arduino`, not root).
- Damper logic rewritten (rule 10): secondary zone dampers close when
  their thermostat is calling the opposite of the main mode.  Open
  during idle/fan-only for whole-house circulation.  Verified on hardware.
- Compressor interlock: 3-minute anti-short-cycle timer starts whenever
  the compressor turns off.  Any off→on restart is blocked during
  the lockout.  A separate **mode-reversal guard** cuts the compressor
  immediately when cool↔heat direction would change while running, then
  the anti-short-cycle timer protects the restart in the new direction.
  Live stage changes (low_cool ↔ high_cool) never stop the compressor
  and are not subject to the timer.
- "Heat Pump" hero card derives `compressor_on` from live RPC outputs
  (low_cool ∥ high_cool) instead of the 10 s-stale SDM120 current
  reading.  Indoor / outdoor humidity rendered at the same size as the
  temperature value.
- **Indoor-RH-driven humidity control** — DONE.  Three-mode logic
  (verified on hardware):
    1. Y2 high-cool call → `high_cool`, dehumidifier off (cool takes over).
    2. Humidistat ON + indoor RH below low threshold → dehumidifier + fan
       only (no compressor).
    3. Indoor RH ≥ high threshold → `high_cool` regardless of humidistat
       or thermostat (emergency dehumidification via cooling coil).
  Two new indoor-RH SensorFlags push-computed by Linux side:
  `humidityModerate` and `humidityHigh`.  `set_flags` RPC now takes
  **7 bools** (was 5).  Two new config params in Settings panel:
  `indoor_humidity_low_pct` (default 55 %) and
  `indoor_humidity_high_pct` (default 65 %).  See `docs/control-logic.md`
  rules 7, 7b, 8, 8b for full detail.
- **Downstairs zone toggle** — DONE.  `downstairs_enabled` config flag
  (default `true`, mirrors `theater_enabled`) added to Settings panel.
  When disabled, the downstairs damper is held permanently open regardless
  of thermostat calls.  `rpc_set_config` now takes **5 args** (was 4);
  bridge, MCU, and HMI all updated.  Verified on hardware: cooling +
  downstairs enabled closes the downstairs damper; flipping to false holds
  it open.
- **Linux-side watchdog** — DONE.  `hvac-bridge.service` now uses
  `Type=notify` + `WatchdogSec=60`; bridge daemon calls `sd_notify
  READY=1` on entry and `WATCHDOG=1` every polling cycle.  If the loop
  ever hangs, systemd kills and restarts the service automatically.
- **MCU hang detection** — DONE.  Bridge daemon tracks the timestamp of
  the last successful MCU RPC read and publishes `mcu_healthy` (bool) and
  `mcu_silence_s` (int) in every MQTT status payload.  Two new config
  knobs in Settings panel: `mcu_hang_threshold_s` (default 60 s) and
  `mcu_auto_recover` (default false).  When silence exceeds the threshold,
  an ERROR is logged; if auto-recover is enabled, the bridge attempts
  `systemctl restart arduino-router` (throttled to one attempt per 2 min)
  to cycle the MCU SRST line.  Requires a sudoers entry — see
  `docs/deployment.md §1.7a`.  HMI connection indicator gains an **amber**
  state ("MCU unresponsive") distinct from the red "controller unreachable"
  state.

**Pending / not yet verified:**
- **NCD WiFi AC Current Monitor** — ordered/installed but not yet integrated
  into the data pipeline.  Web config at `http://ncd-1ec4.local/`; configure
  to publish JSON to `broker.hivemq.com:1883` on a dedicated topic (e.g.
  `jchriswaters_merrick_ac_current`).  See `docs/component-list.md` §WiFi
  Current Monitoring for full specs and wiring notes.
- SDM120 AC meter (0x03) — needs L/N wired to live AC + address set; not
  yet returning data.
- SDM120 dehumidifier meter (0x04) — same.
- `web_config.py` Flask config API — present in repo, superseded by the
  HMI settings panel; not re-verified and likely unused.
- HMI CrowPanel ESP32 (`hmi/crowpanel_hvac/`) — not deployed/verified.
- RS485 auto-reconnect deployed but not yet tested through a real power-cycle
  (verified manually: /dev/ttyUSB0 present, sensors reading).

---

## Key decisions & hard-won gotchas

These are the things that are NOT obvious from reading the code, and that
cost real debugging time to discover.  Detail in `docs/deployment.md`.

1. **MCU sketch is a Zephyr LLEXT, flashed to `0x8100000` as an ELF.**
   The on-board `/opt/openocd/bin/arduino-flash.sh` is **broken** — wrong
   address (`0x80F0000`), wrong file (`.bin-zsk.bin` not `.elf-zsk.bin`),
   and misses the `0xCAFFEEEE` write to `TAMP_CR1` (`0x40036400`) that
   tells Zephyr to load the sketch.  Use `tools/flash_sketch.py`.

2. **`Arduino_RouterBridge` library needs its ASCII flush removed** from
   `BridgeClass::begin()` — the Go router parses each ASCII byte as a
   malformed msgpack int8 and drops the session before `$/reset`.

3. **`arduino-router` had a GPIO reset race** — fixed by `tools/fix_service.py`
   (assert SRST during startup, release only via `--after-ready`).

4. **USB-C is sink-mode when VIN-powered** → won't power a USB dongle.
   A powered USB hub supplies VBUS.  **But** the incoming VBUS then makes
   the Type-C controller flip to *device* data role, so the host
   controller never comes up — fixed by forcing host role in
   `linux/uno-q-usb-host-role.service`.  `power_role` writes are rejected
   by the PD chip (EACCES); `usb_role_switch/role` writes succeed.

5. **HMI has two modes: local (board) and remote (laptop).**  Set
   `"local_mode": true` in `desktop-hmi/config.json` when running on the
   controller — RPC goes direct to the Unix socket, sensor data comes from
   `/tmp/hvac_status.json`, no SSH/paramiko needed.  Set `false` on a
   laptop: uses SSH + exec_command + `/tmp/hmi_rpc.py` helper (the Uno Q's
   sshd has `AllowStreamLocalForwarding=no` so direct-streamlocal fails).

6. **Bitmask field order matters** — `get_outputs` / `get_inputs` /
   `get_input_override` return a 9-char string; index order must match the
   MCU sketch and is duplicated in `linux/bridge_daemon.py` and
   `desktop-hmi/server.py`.  If you add/reorder I/O, update all three.

7. **Output pins 14–19 are the A0–A5 header** on the Uno Q shield, not a
   "D14–D19" header.  D14=A0, D15=A1, … D19=A5.  (Confirmed from the
   board device-tree `digital-pin-gpios` array.)

8. **Input simulation drives real equipment.**  `set_input_override`
   forces inputs through the same control logic as real thermostat calls,
   so a forced cool call energizes the compressor.  Safety interlocks
   still apply.  Override state is volatile (cleared on MCU reset).

9. **`set_flags` takes 7 bools, not 5.**  The indoor-RH humidity rewrite
   added `humidityModerate` and `humidityHigh` as args 6–7.  If you flash
   a pre-rewrite MCU sketch, the bridge will silently push 7 args to an
   RPC that only accepts 5, and the last two flags will be ignored — the
   controller will fall back to its humidistat-only logic.  Always flash
   the sketch from the same commit as the bridge daemon.

10. **`rpc_set_config` takes 5 args, not 4.**  The downstairs zone toggle
    added `downstairsEnabled` as arg 5.  Same cross-version mismatch risk
    as above — keep MCU sketch and bridge_daemon in sync.

11. **bridge_daemon no longer publishes to local mosquitto status topic.**
    `home/hvac/status` is gone from the local broker — status goes to
    HiveMQ only.  Any tool that previously subscribed to the local
    `home/hvac/status` (e.g. Home Assistant, Node-RED) will see no data.
    `home/hvac/cmd` and `home/hvac/config` still live on local mosquitto.

12. **RS485 Modbus reconnects automatically** — if `/dev/ttyUSB0` is
    missing at startup (USB hub not yet enumerated) or disappears mid-run,
    bridge_daemon retries the connection every poll cycle.  No service
    restart needed.  Trigger for reset: both SHT30 reads fail in the same
    cycle → client closed → reconnect attempted next cycle.

---

## Repo map

```
docs/        design, deployment, control logic, MQTT spec, this file
mcu/         STM32 Arduino sketch (the LLEXT)
linux/       bridge_daemon.py, web_config.py, systemd units
desktop-hmi/ HMI server (FastAPI + WebSocket) — runs on controller or laptop
             config.json        board config (local_mode=true, not committed)
             hvac-hmi.service   systemd unit for board deployment
hmi/         CrowPanel ESP32 HMI sketch (not deployed)
tools/       flash_sketch.py, fix_service.py, diagnostics
sim/         offline simulator + sensor bench tools
```

---

## Open questions / future work

- **NCD WiFi current monitor** — configure via `http://ncd-1ec4.local/` to
  publish to HiveMQ; verify messages arriving in Snowflake under the new topic.
- Wire + verify the two SDM120 power meters.
- Add `NOPASSWD` sudoers entry for `arduino` user to allow remote service
  restarts (needed for MCU auto-recover and remote `systemctl restart`):
  `arduino ALL=(ALL) NOPASSWD: /bin/systemctl restart hvac-bridge, /bin/systemctl restart arduino-router`
- Verify Snowflake pipeline is receiving `jchriswaters_merrick_ac` messages
  from HiveMQ now that bridge_daemon publishes there.
  See `mqtt-to-snowflake/TROUBLESHOOTING.md` for how to trigger a manual flush
  and query Snowflake to confirm records landed.
