"""
HVAC Controller — Desktop HMI

Runs locally on your laptop, opens a single persistent SSH connection
to the Uno Q, and serves a real-time web HMI showing the live state of
all 9 inputs and 9 outputs.  Updates push to the browser over WebSocket.

Usage:
    pip install -r requirements.txt
    python server.py
    # then open http://localhost:8000 in any browser

Architecture:
    Browser  <---WebSocket--->  this server  <---SSH direct-streamlocal--->
        arduino-router.sock on the Uno Q (msgpack-RPC)

    SSH "direct-streamlocal@openssh.com" channels let us connect through
    SSH directly to the controller's Unix socket without uploading any
    helper scripts — every RPC call is a fresh channel on the same SSH
    transport, so it's fast (no new TCP/auth per call).

Inputs and outputs are read via the MCU's get_inputs / get_outputs RPCs.
Sensor temperature/humidity is grabbed from the bridge daemon's MQTT
retained status message via mosquitto_sub (only updates every 10 s).
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import socket
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import msgpack
import paramiko
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ──────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────

HERE = Path(__file__).parent
CONFIG_PATH = HERE / "config.json"
STATIC_DIR = HERE / "static"

DEFAULT_CONFIG = {
    "controller_host": "192.168.1.197",
    "controller_user": "arduino",
    "controller_password": "piragua827",
    "ssh_port": 22,
    "router_socket": "/var/run/arduino-router.sock",
    "mqtt_status_topic": "home/hvac/status",
    "poll_interval_s": 1.0,
    "ssh_keepalive_s": 30,
}

def load_or_init_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
        print(f"Wrote default config to {CONFIG_PATH}.  Edit it and restart.")
    cfg = json.loads(CONFIG_PATH.read_text())
    # Merge defaults for any missing keys
    for k, v in DEFAULT_CONFIG.items():
        cfg.setdefault(k, v)
    return cfg

CONFIG = load_or_init_config()

# ──────────────────────────────────────────────────────────────────────
# Field maps — MUST match the MCU sketch / docs/system-design.md
# ──────────────────────────────────────────────────────────────────────

# Bitmask position (LSB first) and pretty labels for the UI.
# (ASCII-only descriptions to avoid source-encoding surprises on Windows.)
OUTPUTS = [
    ("high_cool",          "High Cool",          "Y2 - high-stage cooling"),
    ("low_cool",           "Low Cool",           "Y1 - low-stage cooling / heat pump"),
    ("high_heat",          "High Heat",          "Auxiliary electric heater"),
    ("reversing_valve",    "Reversing Valve",    "B-type - energized during heating"),
    ("theater_damper",     "Theater Damper",     "Open when energized"),
    ("downstairs_damper",  "Downstairs Damper",  "Open when energized"),
    ("vent_open",          "Fresh-Air Vent",     "Open when energized"),
    ("dehumidifier_on",    "Dehumidifier",       "Standalone dehumidifier"),
    ("fan_on",             "Fan",                "Unico G - air handler fan"),
]
INPUTS = [
    ("input_main_low_cool",      "Main Y1",        "Main thermostat - low cool call"),
    ("input_main_high_cool",     "Main Y2",        "Main thermostat - high cool call"),
    ("input_main_heat",          "Main W",         "Main thermostat - heat call"),
    ("input_theater_cool",       "Theater Y",      "Theater thermostat - cool call"),
    ("input_theater_heat",       "Theater W",      "Theater thermostat - heat call"),
    ("input_downstairs_cool",    "Downstairs Y",   "Downstairs thermostat - cool call"),
    ("input_downstairs_heat",    "Downstairs W",   "Downstairs thermostat - heat call"),
    ("input_high_humidity",      "Humidistat",     "External humidity controller alarm"),
    ("input_vent_in",            "Vent Timer",     "External humidity controller vent timer"),
]

# Editable system settings.  Must match SETCONFIG_SCHEMA in
# linux/bridge_daemon.py — the bridge validates inbound setConfig
# commands against its own schema.  The UI reads min/max/unit from here
# to render the input controls.
CONFIG_FIELDS = [
    # (key,                       label,                                unit, kind,         min, max, choices,        help)
    ("heat_pump_min_temp_f",      "Heat pump min outdoor temp",         "°F", "int",         20,  60, None,           "Auxiliary electric heat engages when outdoor temperature is below this."),
    ("free_cool_max_temp_f",      "Free-cooling max outdoor temp",      "°F", "int",         40,  80, None,           "Fresh-air vent opens for free cooling when outdoor temperature is below this."),
    ("high_humidity_pct",         "Outdoor humidity vent limit",        "%",  "int",         50, 100, None,           "Fresh-air vent is forced off when outdoor humidity is at or above this."),
    ("indoor_humidity_low_pct",   "Indoor humidity (low / soft)",       "%",  "int",         30,  70, None,           "Below this indoor RH, the dehumidifier alone is considered sufficient (humidistat → dehumidifier-only)."),
    ("indoor_humidity_high_pct",  "Indoor humidity (high / emergency)", "%",  "int",         40,  90, None,           "At or above this indoor RH, force the AC into high cool and turn off the dehumidifier, regardless of the humidistat input."),
    ("dehum_max_minutes",         "Dehumidifier max runtime",           "min","int",          5, 120, None,           "After this many minutes of dehumidifier-only operation, switch to high cool until humidity clears."),
    ("vent_minutes_per_hour",     "Fresh-air vent minutes per hour",    "min","int",          0,  60, None,           "How many minutes of every hour the fresh-air vent opens on its internal timer."),
    ("theater_enabled",           "Theater zone enabled",               "",   "bool",      None,None, None,           "When disabled, the theater damper is held open and the theater thermostat is ignored."),
    ("downstairs_enabled",        "Downstairs zone enabled",            "",   "bool",      None,None, None,           "When disabled, the downstairs damper is held open and the downstairs thermostat is ignored."),
    ("mode_override",             "Mode override",                      "",   "enum",      None,None, ["auto","off"], "'off' forces the system into fan-only mode regardless of thermostat calls."),
    ("mcu_hang_threshold_s",      "MCU hang threshold",                 "s",  "int",         10, 600, None,           "How many seconds of no successful MCU RPC reads before the bridge flags it as unhealthy (and, if auto-recover is on, restarts arduino-router)."),
    ("mcu_auto_recover",          "MCU auto-recover",                   "",   "bool",      None,None, None,           "When the MCU is unresponsive past the threshold, run `sudo systemctl restart arduino-router` to cycle SRST and reset the MCU. Requires a sudoers entry — see docs/deployment.md."),
]

# ──────────────────────────────────────────────────────────────────────
# SSH + RPC plumbing
# ──────────────────────────────────────────────────────────────────────

log = logging.getLogger("hmi")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

# Helper script uploaded once to the controller.  Performs one or more
# msgpack-RPC calls against /var/run/arduino-router.sock and prints a
# single JSON object to stdout.  Batching multiple methods into a single
# invocation keeps SSH overhead to one round trip per poll cycle.
#
# Usage on the board:
#   python3 /tmp/hmi_rpc.py {"methods":[["get_outputs",[]],["get_inputs",[]]]}
# Returns:
#   {"get_outputs": [1,1,null,"000011101"], "get_inputs": [1,1,null,"000000000"]}
#
# AllowStreamLocalForwarding is off by default in the Uno Q's sshd_config,
# so we can't use direct-streamlocal channels — exec_command + helper
# script is the portable path.
RPC_HELPER_SCRIPT = r'''
import sys, json, socket, msgpack, time

SOCK = "/var/run/arduino-router.sock"

def rpc(method, args, timeout=3.0):
    req = msgpack.packb([0, 1, method, args], use_bin_type=True)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(SOCK)
        s.sendall(req)
        data = b""
        deadline = time.monotonic() + timeout
        while True:
            try:
                chunk = s.recv(4096)
            except socket.timeout:
                return None
            if not chunk:
                return None
            data += chunk
            try:
                return msgpack.unpackb(data, raw=False)
            except Exception:
                if time.monotonic() > deadline:
                    return None
                continue
    except Exception as e:
        return {"_error": str(e)}
    finally:
        try: s.close()
        except: pass

def main():
    spec = json.loads(sys.stdin.read() or sys.argv[1])
    out = {}
    for entry in spec.get("methods", []):
        name = entry[0]
        args = entry[1] if len(entry) > 1 else []
        out[name] = rpc(name, args)
    print(json.dumps(out, default=str))

if __name__ == "__main__":
    main()
'''


class ControllerSession:
    """Single long-lived SSH connection to the Uno Q.

    All RPC calls go through a small helper script (`/tmp/hmi_rpc.py`)
    uploaded once on first connect.  One exec_command per poll cycle
    handles batched RPC; `mosquitto_sub` runs separately for sensor data.
    """

    HELPER_PATH = "/tmp/hmi_rpc.py"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._ssh: Optional[paramiko.SSHClient] = None
        self._lock = asyncio.Lock()
        self.last_error: Optional[str] = None
        self._helper_installed = False

    # ---- connection management ----

    def _is_alive(self) -> bool:
        if self._ssh is None:
            return False
        t = self._ssh.get_transport()
        return bool(t and t.is_active())

    def _connect_sync(self):
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            self.cfg["controller_host"],
            port=self.cfg["ssh_port"],
            username=self.cfg["controller_user"],
            password=self.cfg["controller_password"],
            timeout=5,
            banner_timeout=10,
            auth_timeout=10,
        )
        t = ssh.get_transport()
        if t:
            t.set_keepalive(self.cfg["ssh_keepalive_s"])
        self._ssh = ssh
        self.last_error = None
        self._helper_installed = False
        log.info("SSH connected to %s", self.cfg["controller_host"])

        # Upload the RPC helper script
        sftp = ssh.open_sftp()
        try:
            with sftp.open(self.HELPER_PATH, "w") as f:
                f.write(RPC_HELPER_SCRIPT)
            self._helper_installed = True
            log.debug("Helper uploaded to %s", self.HELPER_PATH)
        finally:
            sftp.close()

    async def ensure_connected(self) -> bool:
        async with self._lock:
            if self._is_alive() and self._helper_installed:
                return True
            try:
                await asyncio.get_event_loop().run_in_executor(None, self._connect_sync)
                return True
            except Exception as e:
                self.last_error = f"SSH connect failed: {e}"
                log.warning(self.last_error)
                self._ssh = None
                return False

    def close(self):
        if self._ssh:
            try:
                self._ssh.close()
            except Exception:
                pass
            self._ssh = None
            self._helper_installed = False

    # ---- Batched RPC via exec_command ----

    def _rpc_batch_sync(self, methods: list[tuple[str, list]], timeout: float = 4.0) -> dict:
        if not self._is_alive():
            raise RuntimeError("SSH not connected")
        spec = json.dumps({"methods": [list(m) for m in methods]})
        # Pipe the spec via stdin so we don't have to worry about shell quoting.
        cmd = f"python3 {self.HELPER_PATH}"
        stdin, stdout, stderr = self._ssh.exec_command(cmd, timeout=timeout)
        try:
            stdin.write(spec)
            stdin.channel.shutdown_write()
        except Exception:
            pass
        out = stdout.read().decode(errors="replace").strip()
        if not out:
            err = stderr.read().decode(errors="replace").strip()
            raise RuntimeError(f"empty helper output (stderr: {err[:200]})")
        try:
            return json.loads(out)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"helper returned non-JSON: {out[:200]} ({e})")

    async def rpc_batch(self, methods: list[tuple[str, list]], timeout: float = 4.0) -> dict:
        """Run several msgpack-RPC calls in one SSH round trip.

        Returns a dict {method_name: rpc_response} where rpc_response is
        the raw msgpack-RPC response array [1, msgid, error, result] or
        None on transport failure.
        """
        if not await self.ensure_connected():
            return {}
        try:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._rpc_batch_sync, methods, timeout
            )
        except Exception as e:
            self.last_error = f"RPC batch failed: {e}"
            log.warning(self.last_error)
            self.close()
            return {}

    async def rpc(self, method: str, args=None, timeout: float = 4.0):
        """Single-method convenience wrapper."""
        result = await self.rpc_batch([(method, args or [])], timeout)
        return result.get(method)

    # ---- MQTT retained-topic reads ----

    def _mqtt_retained_sync(self, topic: str, timeout: float = 2.0) -> Optional[dict]:
        if not self._is_alive():
            raise RuntimeError("SSH not connected")
        # -C 1: exit after one message; -W <sec>: total timeout
        cmd = (
            f"mosquitto_sub -h localhost -t {topic} "
            f"-C 1 -W {int(timeout) + 1} 2>/dev/null"
        )
        stdin, stdout, stderr = self._ssh.exec_command(cmd, timeout=timeout + 2)
        out = stdout.read().decode(errors="replace").strip()
        if not out:
            return None
        try:
            return json.loads(out)
        except Exception:
            return None

    async def mqtt_status(self, timeout: float = 2.0) -> Optional[dict]:
        if not await self.ensure_connected():
            return None
        try:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._mqtt_retained_sync,
                self.cfg["mqtt_status_topic"], timeout
            )
        except Exception as e:
            log.debug("mosquitto_sub (status) failed: %s", e)
            return None

    async def mqtt_config(self, timeout: float = 2.0) -> Optional[dict]:
        if not await self.ensure_connected():
            return None
        try:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._mqtt_retained_sync,
                self.cfg.get("mqtt_config_topic", "home/hvac/config"), timeout
            )
        except Exception as e:
            log.debug("mosquitto_sub (config) failed: %s", e)
            return None

    # ---- MQTT publish (setConfig commands) ----

    def _mqtt_publish_sync(self, topic: str, payload: dict, timeout: float = 3.0) -> bool:
        if not self._is_alive():
            raise RuntimeError("SSH not connected")
        # Use stdin to avoid shell-quoting any JSON braces / quotes.
        cmd = f"mosquitto_pub -h localhost -t {topic} -q 1 -s"
        stdin, stdout, stderr = self._ssh.exec_command(cmd, timeout=timeout)
        try:
            stdin.write(json.dumps(payload))
            stdin.channel.shutdown_write()
        except Exception:
            return False
        # Wait for the command to finish (mosquitto_pub returns 0 on success)
        return stdout.channel.recv_exit_status() == 0

    async def mqtt_publish(self, topic: str, payload: dict) -> bool:
        if not await self.ensure_connected():
            return False
        try:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._mqtt_publish_sync, topic, payload
            )
        except Exception as e:
            log.warning("mosquitto_pub failed: %s", e)
            return False


# ──────────────────────────────────────────────────────────────────────
# Status aggregation
# ──────────────────────────────────────────────────────────────────────

def parse_bitmask(s: str, fields: list[tuple[str, str, str]]) -> dict[str, bool]:
    """Convert a '01010...' bitmask string from the MCU to a {name: bool} dict.

    The MCU rpc_get_outputs / rpc_get_inputs functions return a 9-character
    string where index 0 corresponds to the first field (LSB-first ordering).
    """
    out = {}
    if not isinstance(s, str):
        for key, _, _ in fields:
            out[key] = False
        return out
    for i, (key, _, _) in enumerate(fields):
        out[key] = (i < len(s) and s[i] == "1")
    return out


def parse_override(s: str, fields: list[tuple[str, str, str]]) -> dict[str, str]:
    """Convert the get_input_override 9-char string to {name: mode}.

    Each char: '-' = auto (live hardware), '1' = forced ON, '0' = forced OFF.
    """
    mode_map = {"-": "auto", "1": "on", "0": "off"}
    out = {}
    for i, (key, _, _) in enumerate(fields):
        ch = s[i] if isinstance(s, str) and i < len(s) else "-"
        out[key] = mode_map.get(ch, "auto")
    return out


def override_to_mask_value(override: dict[str, str]) -> tuple[int, int]:
    """Build the (mask, value) ints the MCU expects from a {key: mode} dict.

    mask bit set  = that input is overridden (ignores hardware)
    value bit set = forced state when overridden
    """
    mask = 0
    value = 0
    for i, (key, _, _) in enumerate(INPUTS):
        mode = override.get(key, "auto")
        if mode == "on":
            mask |= (1 << i)
            value |= (1 << i)
        elif mode == "off":
            mask |= (1 << i)
    return mask, value


async def collect_status(session: ControllerSession) -> dict:
    """Poll the controller for a complete status snapshot."""
    snapshot = {
        "ts": time.time(),
        "connected": False,
        "controller_host": session.cfg["controller_host"],
        "last_error": session.last_error,
        "outputs": {key: None for key, _, _ in OUTPUTS},
        "inputs":  {key: None for key, _, _ in INPUTS},
        "input_override": {key: "auto" for key, _, _ in INPUTS},
        "sim_active": False,
        "sensors": None,
        "mode": None,
        "compressor_on": None,
    }

    # Outputs + inputs + override state via batched RPC (one SSH round
    # trip), and the MQTT retained status in parallel.
    rpc_task = asyncio.create_task(session.rpc_batch([
        ("get_outputs",        []),
        ("get_inputs",         []),
        ("get_input_override", []),
    ]))
    mqtt_task = asyncio.create_task(session.mqtt_status(timeout=2.0))
    cfg_task  = asyncio.create_task(session.mqtt_config(timeout=2.0))

    rpc_resp = await rpc_task

    def _result(resp):
        # msgpack RPC response: [1, msgid, error, result]
        if isinstance(resp, list) and len(resp) >= 4 and resp[2] is None:
            return resp[3]
        return None

    out_val = _result(rpc_resp.get("get_outputs"))
    in_val  = _result(rpc_resp.get("get_inputs"))
    ovr_val = _result(rpc_resp.get("get_input_override"))

    if out_val is not None or in_val is not None:
        snapshot["connected"] = True

    if isinstance(out_val, str):
        snapshot["outputs"] = parse_bitmask(out_val, OUTPUTS)
    if isinstance(in_val, str):
        snapshot["inputs"] = parse_bitmask(in_val, INPUTS)
    if isinstance(ovr_val, str):
        ovr = parse_override(ovr_val, INPUTS)
        snapshot["input_override"] = ovr
        snapshot["sim_active"] = any(v != "auto" for v in ovr.values())

    # ── Derive mode + compressor_on from the LIVE RPC outputs ─────
    # The bridge daemon also publishes these on MQTT, but that message
    # only refreshes every ~10 s.  The HMI polls outputs every ~1 s, so
    # if we waited for the MQTT derivation the dashboard would lag by
    # up to 9 s after the user simulates an input.  Mirror bridge
    # daemon's derive_mode() here so the UI stays snappy.
    if out_val is not None:
        outs = snapshot["outputs"]
        hc, lc = bool(outs.get("high_cool")), bool(outs.get("low_cool"))
        rv     = bool(outs.get("reversing_valve"))
        dh     = bool(outs.get("dehumidifier_on"))
        fn     = bool(outs.get("fan_on"))
        if   (hc or lc) and rv: snapshot["mode"] = "heat"
        elif hc:                snapshot["mode"] = "high_cool"
        elif lc:                snapshot["mode"] = "low_cool"
        elif dh:                snapshot["mode"] = "dehum"
        elif fn:                snapshot["mode"] = "fan"
        else:                   snapshot["mode"] = "off"
        snapshot["compressor_on"] = hc or lc

    # Current system config (retained MQTT topic from the bridge daemon)
    cfg_msg = await cfg_task
    if isinstance(cfg_msg, dict):
        # Keep only the keys we expose to the UI, plus updated_at.
        keep = {f[0] for f in CONFIG_FIELDS} | {"config_updated_at"}
        snapshot["config"] = {k: v for k, v in cfg_msg.items() if k in keep}
    else:
        snapshot["config"] = None

    # Sensors + derived fields from MQTT retained status
    mqtt = await mqtt_task
    if isinstance(mqtt, dict):
        snapshot["sensors"] = {
            "indoor_temp_f":         mqtt.get("indoor_temp_f"),
            "indoor_humidity_pct":   mqtt.get("indoor_humidity_pct"),
            "outdoor_temp_f":        mqtt.get("outdoor_temp_f"),
            "outdoor_humidity_pct":  mqtt.get("outdoor_humidity_pct"),
            "ac_voltage_v":          mqtt.get("ac_voltage_v"),
            "ac_current_a":          mqtt.get("ac_current_a"),
            "ac_power_w":            mqtt.get("ac_power_w"),
            "ac_energy_kwh":         mqtt.get("ac_energy_kwh"),
            "dehum_power_w":         mqtt.get("dehum_power_w"),
        }
        # mode + compressor_on are now derived live from the RPC outputs
        # above (snappier than the 10 s MQTT cycle).  Fall back to MQTT
        # only if the live RPC failed and we have no fresh outputs.
        if out_val is None:
            snapshot["mode"]          = mqtt.get("mode")
            snapshot["compressor_on"] = mqtt.get("compressor_on")
        snapshot["mqtt_ts"]       = mqtt.get("timestamp")
        # MCU health: published by bridge_daemon every cycle.  We surface
        # it on the dashboard so a hung MCU is obvious even if the HMI's
        # own RPC reads happen to look fresh (they don't — but defense in
        # depth — the bridge sees the same RPC channel and decides).
        snapshot["mcu_healthy"]   = mqtt.get("mcu_healthy")
        snapshot["mcu_silence_s"] = mqtt.get("mcu_silence_s")

    return snapshot


# ──────────────────────────────────────────────────────────────────────
# WebSocket broadcasting
# ──────────────────────────────────────────────────────────────────────

class Hub:
    """Tracks connected WebSocket clients and the latest status snapshot."""
    def __init__(self):
        self.clients: set[WebSocket] = set()
        self.latest: Optional[dict] = None
        self._lock = asyncio.Lock()

    async def add(self, ws: WebSocket):
        async with self._lock:
            self.clients.add(ws)
        if self.latest:
            try: await ws.send_json(self.latest)
            except Exception: pass

    async def remove(self, ws: WebSocket):
        async with self._lock:
            self.clients.discard(ws)

    async def broadcast(self, payload: dict):
        self.latest = payload
        async with self._lock:
            dead = []
            for ws in self.clients:
                try:
                    await ws.send_json(payload)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self.clients.discard(ws)


SESSION = ControllerSession(CONFIG)
HUB = Hub()


async def poll_loop():
    interval = float(CONFIG.get("poll_interval_s", 1.0))
    while True:
        try:
            snap = await collect_status(SESSION)
            await HUB.broadcast(snap)
        except Exception as e:
            log.exception("poll_loop iteration failed: %s", e)
        await asyncio.sleep(interval)


# ──────────────────────────────────────────────────────────────────────
# FastAPI app
# ──────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(poll_loop())
    log.info("HMI ready at http://localhost:8000")
    log.info("Polling %s every %.1fs", CONFIG["controller_host"], CONFIG["poll_interval_s"])
    yield
    task.cancel()
    SESSION.close()


app = FastAPI(title="HVAC HMI", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.middleware("http")
async def no_cache_for_html_and_static(request, call_next):
    """Avoid stale UI after server-side edits — the HMI is a small
    single-user dev app, so we don't want any caching on / or /static/."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


@app.get("/")
async def root():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/status")
async def api_status():
    snap = HUB.latest or await collect_status(SESSION)
    return JSONResponse(snap)


@app.get("/api/metadata")
async def api_metadata():
    return {
        "outputs": [{"key": k, "label": label, "desc": d} for k, label, d in OUTPUTS],
        "inputs":  [{"key": k, "label": label, "desc": d} for k, label, d in INPUTS],
        "config_fields": [
            {
                "key": k, "label": label, "unit": unit, "kind": kind,
                "min": mn, "max": mx, "choices": ch, "help": hlp,
            }
            for k, label, unit, kind, mn, mx, ch, hlp in CONFIG_FIELDS
        ],
        "controller_host": CONFIG["controller_host"],
    }


async def apply_set_config(cmd: dict) -> dict:
    """Publish a setConfig command to the bridge daemon via MQTT.

    Accepts {"type": "set_config", "key": "<schema_key>", "value": <val>}.
    The bridge daemon validates against its SETCONFIG_SCHEMA, persists to
    /etc/hvac/config.json, pushes the MCU-relevant subset via set_config,
    and republishes home/hvac/config.  We re-poll afterwards so the UI
    reflects the new value once the retained message refreshes.
    """
    key = cmd.get("key")
    value = cmd.get("value")
    valid_keys = {f[0] for f in CONFIG_FIELDS}
    if key not in valid_keys:
        return {"ok": False, "error": f"unknown config key {key!r}"}

    payload = {"cmd": "setConfig", "key": key, "value": value}
    ok = await SESSION.mqtt_publish(
        SESSION.cfg.get("mqtt_cmd_topic", "home/hvac/cmd"), payload
    )

    # Give the bridge a moment to process + republish, then re-poll.
    await asyncio.sleep(0.4)
    snap = await collect_status(SESSION)
    await HUB.broadcast(snap)
    return {"ok": ok, "key": key, "value": value}


async def apply_override_command(cmd: dict) -> dict:
    """Handle an input-simulation command from the UI.

    Accepts:
      {"type": "override", "key": "<input_key>", "mode": "auto"|"on"|"off"}
      {"type": "clear_overrides"}

    Reads the current override state, applies the change, and pushes the
    new (mask, value) to the MCU via set_input_override.  Returns the
    fresh snapshot (also broadcast to all clients).
    """
    latest = HUB.latest or {}
    current = dict(latest.get("input_override") or {})

    if cmd.get("type") == "clear_overrides":
        mask, value = 0, 0
    else:
        key = cmd.get("key")
        mode = cmd.get("mode", "auto")
        if key not in {k for k, _, _ in INPUTS}:
            return {"error": f"unknown input key {key!r}"}
        if mode not in ("auto", "on", "off"):
            return {"error": f"invalid mode {mode!r}"}
        current[key] = mode
        mask, value = override_to_mask_value(current)

    resp = await SESSION.rpc("set_input_override", [mask, value])
    ok = isinstance(resp, list) and len(resp) >= 4 and resp[2] is None

    # Re-poll immediately so the UI reflects the change without waiting
    # for the next periodic cycle.
    snap = await collect_status(SESSION)
    await HUB.broadcast(snap)
    return {"ok": ok, "mask": mask, "value": value}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    await HUB.add(ws)
    try:
        while True:
            msg = await ws.receive_text()
            try:
                cmd = json.loads(msg)
            except Exception:
                continue
            if cmd.get("type") in ("override", "clear_overrides"):
                result = await apply_override_command(cmd)
            elif cmd.get("type") == "set_config":
                result = await apply_set_config(cmd)
            else:
                log.debug("ws msg (ignored): %s", msg)
                continue
            try:
                await ws.send_json({"type": "ack", **result})
            except Exception:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        await HUB.remove(ws)


@app.post("/api/override")
async def api_override(cmd: dict):
    """REST fallback for input simulation (same payload as the WS command)."""
    return await apply_override_command(cmd)


@app.post("/api/set_config")
async def api_set_config(cmd: dict):
    """REST fallback for changing a system setting."""
    return await apply_set_config(cmd)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "server:app",
        host="127.0.0.1",
        port=8000,
        reload=False,
        log_level="info",
    )
