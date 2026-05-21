#!/usr/bin/env python3
"""
web_config.py
Uno Q Linux side — HVAC web configuration API

Serves a Flask REST API on port 80 for making configuration changes
to the HVAC controller. Config changes are:
  1. Validated here
  2. Written to /etc/hvac/config.json for persistence
  3. Pushed to MCU via Arduino Bridge RPC

Endpoints:
  GET  /config                 → current state + config as JSON
  POST /config/theater         → {"enabled": true/false}
  POST /config/vent            → {"minutesPerHour": 0-60}
  POST /config/mode-override   → {"mode": "auto" | "off"}

See docs/mqtt-payload-spec.md for response field reference.
See docs/system-design.md for Bridge RPC conventions.

TODO (implement in Claude Code session):
  [ ] GET /config — read live state from Bridge + config from disk
  [ ] POST endpoints — validate, persist, push to MCU
  [ ] Simple HTML dashboard at GET / (optional)
  [ ] Systemd service unit (linux/hvac-web.service)
  [ ] Consider running both bridge_daemon and web_config under
      supervisord or as two systemd units

Dependencies:
  pip install flask
"""

from flask import Flask, request, jsonify, abort
from pathlib import Path
import json
import logging

app = Flask(__name__)

CONFIG_PATH = Path("/etc/hvac/config.json")

DEFAULT_CONFIG = {
    "mqtt_host":             "192.168.1.x",
    "mqtt_port":             1883,
    "theater_enabled":       False,
    "vent_minutes_per_hour": 10,
    "mode_override":         "auto",
}


# ──────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────

def load_config() -> dict:
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return {**DEFAULT_CONFIG, **json.load(f)}
    return DEFAULT_CONFIG.copy()


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def push_to_mcu(cfg: dict) -> None:
    """
    TODO: Push updated config to MCU via Arduino Bridge RPC.
    """
    logging.warning("push_to_mcu: Bridge RPC not yet implemented")


def read_live_state() -> dict:
    """
    TODO: Read current MCU output state via Arduino Bridge RPC.
    Returns empty dict until Bridge is implemented.
    """
    logging.warning("read_live_state: Bridge RPC not yet implemented")
    return {}


# ──────────────────────────────────────────────────────────────
# ROUTES
# ──────────────────────────────────────────────────────────────

@app.route("/config", methods=["GET"])
def get_config():
    """Return current system state and configuration."""
    cfg   = load_config()
    state = read_live_state()
    return jsonify({**state, "config": cfg})


@app.route("/config/theater", methods=["POST"])
def set_theater():
    """Enable or disable the theater zone."""
    body = request.get_json(silent=True)
    if body is None or "enabled" not in body:
        abort(400, "Request body must be JSON with 'enabled' field")

    cfg = load_config()
    cfg["theater_enabled"] = bool(body["enabled"])
    save_config(cfg)
    push_to_mcu(cfg)

    return jsonify({"ok": True, "theater_enabled": cfg["theater_enabled"]})


@app.route("/config/vent", methods=["POST"])
def set_vent():
    """Set ventilation schedule (minutes per hour the fresh-air vent opens)."""
    body = request.get_json(silent=True)
    if body is None or "minutesPerHour" not in body:
        abort(400, "Request body must be JSON with 'minutesPerHour' field")

    minutes = int(body["minutesPerHour"])
    if not (0 <= minutes <= 60):
        abort(400, "minutesPerHour must be 0-60")

    cfg = load_config()
    cfg["vent_minutes_per_hour"] = minutes
    save_config(cfg)
    push_to_mcu(cfg)

    return jsonify({"ok": True, "vent_minutes_per_hour": minutes})


@app.route("/config/mode-override", methods=["POST"])
def set_mode_override():
    """Force system off or return to automatic control."""
    body = request.get_json(silent=True)
    if body is None or "mode" not in body:
        abort(400, "Request body must be JSON with 'mode' field")

    mode = body["mode"]
    if mode not in ("auto", "off"):
        abort(400, "mode must be 'auto' or 'off'")

    cfg = load_config()
    cfg["mode_override"] = mode
    save_config(cfg)
    push_to_mcu(cfg)

    return jsonify({"ok": True, "mode_override": mode})


# ──────────────────────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # Run on all interfaces, port 80
    # In production use gunicorn or uwsgi behind nginx instead
    app.run(host="0.0.0.0", port=80, debug=False)
