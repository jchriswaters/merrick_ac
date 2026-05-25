#!/usr/bin/env python3
"""
web_config.py  —  HVAC web UI and REST configuration API

Serves a browser-based control panel at http://<controller-ip>/
and a JSON REST API at /api/*. The web UI is a Progressive Web App (PWA):
on iPhone Safari use "Share → Add to Home Screen" to install it as a
full-screen icon that behaves like a native app.

All configuration changes are published to the home/hvac/cmd MQTT topic
(same path as the HMI and Home Assistant). The bridge_daemon.py subscriber
handles validation, MCU push, and config persistence.

Live status data is read from /tmp/hvac_status.json, which bridge_daemon
writes on every polling cycle.

REST API:
  GET  /                    → PWA web UI
  GET  /manifest.json       → PWA manifest
  GET  /api/status          → last status payload (from bridge_daemon cache)
  GET  /api/config          → current configuration
  POST /api/config          → {"key": "...", "value": ...}  — set a parameter
  POST /api/cmd             → raw command object (same schema as home/hvac/cmd)

Dependencies:
  pip3 install flask paho-mqtt

Run as a systemd service: see linux/hvac-web.service
"""

import json
import logging
import threading
import time
from pathlib import Path

import paho.mqtt.client as mqtt
from flask import Flask, jsonify, render_template, request, abort, send_from_directory

# ──────────────────────────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────────────────────────

CONFIG_PATH      = Path("/etc/hvac/config.json")
STATUS_CACHE     = Path("/tmp/hvac_status.json")   # written by bridge_daemon.py
STATIC_DIR       = Path(__file__).parent / "static"
TEMPLATES_DIR    = Path(__file__).parent / "templates"

MQTT_CMD_TOPIC   = "home/hvac/cmd"

DEFAULT_CONFIG: dict = {
    "mqtt_host":             "localhost",
    "mqtt_port":             1883,
    "mqtt_username":         None,
    "mqtt_password":         None,
    "theater_enabled":       False,
    "vent_minutes_per_hour": 10,
    "mode_override":         "auto",
    "heat_pump_min_temp_f":  40,
    "free_cool_max_temp_f":  60,
    "high_humidity_pct":     80,
    "dehum_max_minutes":     20,
}

# Setpoint validation (min, max, type) — mirrors SETCONFIG_SCHEMA in bridge_daemon.py
SETCONFIG_SCHEMA: dict = {
    "heat_pump_min_temp_f":  (20,   60, int),
    "free_cool_max_temp_f":  (40,   80, int),
    "high_humidity_pct":     (50,  100, int),
    "dehum_max_minutes":     ( 5,  120, int),
    "vent_minutes_per_hour": ( 0,   60, int),
    "theater_enabled":       (None, None, bool),
    "mode_override":         (None, None, str),
}

log = logging.getLogger("hvac.web")

# ──────────────────────────────────────────────────────────────
# FLASK APP
# ──────────────────────────────────────────────────────────────

app = Flask(
    __name__,
    template_folder=str(TEMPLATES_DIR),
    static_folder=str(STATIC_DIR),
)

# ──────────────────────────────────────────────────────────────
# CONFIG HELPERS
# ──────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as fh:
                return {**DEFAULT_CONFIG, **json.load(fh)}
        except Exception as exc:
            log.error("Failed to load config: %s", exc)
    return DEFAULT_CONFIG.copy()


def read_status_cache() -> dict:
    """Read the last status payload written by bridge_daemon.py."""
    if STATUS_CACHE.exists():
        try:
            with open(STATUS_CACHE) as fh:
                return json.load(fh)
        except Exception as exc:
            log.warning("Status cache read failed: %s", exc)
    return {"error": "status_unavailable", "timestamp": None}


# ──────────────────────────────────────────────────────────────
# MQTT PUBLISHING
# The web app publishes commands to home/hvac/cmd; bridge_daemon
# processes them (validates, persists, pushes to MCU).
# ──────────────────────────────────────────────────────────────

_mqtt_client: mqtt.Client | None = None
_mqtt_connected = False
_mqtt_lock = threading.Lock()


def _init_mqtt(cfg: dict) -> None:
    global _mqtt_client, _mqtt_connected

    def on_connect(client, userdata, flags, rc):
        global _mqtt_connected
        _mqtt_connected = (rc == 0)
        if rc == 0:
            log.info("Web MQTT client connected")
        else:
            log.warning("Web MQTT connect failed rc=%d", rc)

    def on_disconnect(client, userdata, rc):
        global _mqtt_connected
        _mqtt_connected = False

    client = mqtt.Client(client_id="hvac-web", clean_session=True)
    client.reconnect_delay_set(min_delay=2, max_delay=30)
    client.on_connect    = on_connect
    client.on_disconnect = on_disconnect
    if cfg.get("mqtt_username"):
        client.username_pw_set(cfg["mqtt_username"], cfg.get("mqtt_password"))
    try:
        client.connect(cfg["mqtt_host"], int(cfg["mqtt_port"]), keepalive=60)
        client.loop_start()
        _mqtt_client = client
    except Exception as exc:
        log.error("Web MQTT initial connect failed: %s — will retry", exc)


def _publish_cmd(cmd: dict) -> bool:
    """Publish a JSON command to the MQTT cmd topic. Returns True on success."""
    with _mqtt_lock:
        if _mqtt_client is None or not _mqtt_connected:
            log.warning("MQTT not connected — command not sent: %s", cmd)
            return False
        try:
            info = _mqtt_client.publish(MQTT_CMD_TOPIC, json.dumps(cmd), qos=1)
            info.wait_for_publish(timeout=3.0)
            return True
        except Exception as exc:
            log.error("MQTT publish error: %s", exc)
            return False


# ──────────────────────────────────────────────────────────────
# VALIDATION
# ──────────────────────────────────────────────────────────────

def _validate_setconfig(key: str, value) -> tuple[bool, object, str]:
    """
    Validate a setConfig key/value pair.
    Returns (valid: bool, coerced_value, error_message).
    """
    if key not in SETCONFIG_SCHEMA:
        return False, None, f"Unknown config key '{key}'"

    lo, hi, typ = SETCONFIG_SCHEMA[key]

    if key == "mode_override":
        if value not in ("auto", "off"):
            return False, None, "mode_override must be 'auto' or 'off'"
        return True, value, ""

    if typ is bool:
        if isinstance(value, bool):
            return True, value, ""
        if isinstance(value, (int, str)):
            return True, bool(value), ""
        return False, None, f"Expected boolean for '{key}'"

    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return False, None, f"Expected integer for '{key}', got {value!r}"

    if lo is not None and coerced < lo:
        return False, None, f"'{key}' minimum is {lo}, got {coerced}"
    if hi is not None and coerced > hi:
        return False, None, f"'{key}' maximum is {hi}, got {coerced}"

    return True, coerced, ""


# ──────────────────────────────────────────────────────────────
# ROUTES — WEB UI
# ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Serve the PWA web UI."""
    return render_template("index.html")


@app.route("/manifest.json")
def manifest():
    """PWA web app manifest — enables 'Add to Home Screen' on iPhone."""
    return jsonify({
        "name": "HVAC Controller",
        "short_name": "HVAC",
        "description": "Unico mini-duct HVAC zone controller",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#1a1a2e",
        "theme_color": "#0f3460",
        "orientation": "portrait-primary",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    })


# ──────────────────────────────────────────────────────────────
# ROUTES — REST API
# ──────────────────────────────────────────────────────────────

@app.route("/api/status")
def api_status():
    """Return the latest system status (relay states, sensors, power, mode)."""
    return jsonify(read_status_cache())


@app.route("/api/config", methods=["GET"])
def api_config_get():
    """Return the current configuration / setpoints."""
    cfg = load_config()
    # Strip internal / sensitive fields before returning
    public_keys = [
        "theater_enabled", "vent_minutes_per_hour", "mode_override",
        "heat_pump_min_temp_f", "free_cool_max_temp_f",
        "high_humidity_pct", "dehum_max_minutes",
    ]
    return jsonify({k: cfg[k] for k in public_keys if k in cfg})


@app.route("/api/config", methods=["POST"])
def api_config_post():
    """
    Set a single configuration parameter.
    Body: {"key": "<param_name>", "value": <new_value>}
    Publishes a setConfig command to home/hvac/cmd.
    """
    body = request.get_json(silent=True)
    if not body or "key" not in body or "value" not in body:
        abort(400, "JSON body must contain 'key' and 'value'")

    key   = body["key"]
    value = body["value"]

    valid, coerced, err = _validate_setconfig(key, value)
    if not valid:
        abort(400, err)

    cmd = {"cmd": "setConfig", "key": key, "value": coerced}
    ok  = _publish_cmd(cmd)

    if not ok:
        return jsonify({"ok": False, "error": "MQTT publish failed — bridge_daemon may be offline"}), 503

    return jsonify({"ok": True, "key": key, "value": coerced})


@app.route("/api/cmd", methods=["POST"])
def api_cmd():
    """
    Forward a raw command to the MQTT cmd topic.
    Body must be a valid JSON object with a 'cmd' field.
    """
    body = request.get_json(silent=True)
    if not body or "cmd" not in body:
        abort(400, "JSON body must contain 'cmd' field")

    allowed = {"setConfig", "reset_energy"}
    if body["cmd"] not in allowed:
        abort(400, f"cmd must be one of {sorted(allowed)}")

    ok = _publish_cmd(body)
    if not ok:
        return jsonify({"ok": False, "error": "MQTT publish failed"}), 503

    return jsonify({"ok": True})


# ──────────────────────────────────────────────────────────────
# LEGACY ENDPOINTS (backward-compatible with original spec)
# ──────────────────────────────────────────────────────────────

@app.route("/config", methods=["GET"])
def legacy_config_get():
    cfg    = load_config()
    status = read_status_cache()
    return jsonify({**status, "config": cfg})


@app.route("/config/theater", methods=["POST"])
def legacy_theater():
    body = request.get_json(silent=True)
    if not body or "enabled" not in body:
        abort(400, "missing 'enabled'")
    ok = _publish_cmd({"cmd": "setConfig", "key": "theater_enabled",
                        "value": bool(body["enabled"])})
    return jsonify({"ok": ok})


@app.route("/config/vent", methods=["POST"])
def legacy_vent():
    body = request.get_json(silent=True)
    if not body or "minutesPerHour" not in body:
        abort(400, "missing 'minutesPerHour'")
    v, coerced, err = _validate_setconfig("vent_minutes_per_hour", body["minutesPerHour"])
    if not v:
        abort(400, err)
    ok = _publish_cmd({"cmd": "setConfig", "key": "vent_minutes_per_hour", "value": coerced})
    return jsonify({"ok": ok})


@app.route("/config/mode-override", methods=["POST"])
def legacy_mode_override():
    body = request.get_json(silent=True)
    if not body or "mode" not in body:
        abort(400, "missing 'mode'")
    v, coerced, err = _validate_setconfig("mode_override", body["mode"])
    if not v:
        abort(400, err)
    ok = _publish_cmd({"cmd": "setConfig", "key": "mode_override", "value": coerced})
    return jsonify({"ok": ok})


# ──────────────────────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )
    cfg = load_config()
    _init_mqtt(cfg)
    # Use port 8080 to avoid needing root; put nginx or a simple proxy in front
    # to serve on port 80. Alternatively run with sudo for port 80 directly.
    app.run(host="0.0.0.0", port=8080, debug=False)
