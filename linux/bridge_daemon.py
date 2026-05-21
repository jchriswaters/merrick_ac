#!/usr/bin/env python3
"""
bridge_daemon.py
Uno Q Linux side — HVAC controller bridge daemon

Responsibilities:
  1. Poll MCU state every 10 seconds via Arduino Bridge RPC
  2. Expand compact MCU keys to human-readable field names
  3. Add derived fields (mode, compressor_on, timestamp)
  4. Publish full JSON payload to MQTT topic home/hvac/status (retained)
  5. Subscribe to home/hvac/cmd and relay commands to MCU via Bridge

See docs/mqtt-payload-spec.md for full payload schema and field reference.
See docs/system-design.md for Bridge RPC conventions.

TODO (implement in Claude Code session):
  [ ] Arduino Bridge RPC calls to read MCU state
  [ ] Arduino Bridge RPC calls to push config to MCU
  [ ] MQTT connect with reconnect logic
  [ ] Config persistence to /etc/hvac/config.json
  [ ] Systemd service unit file (linux/hvac-bridge.service)

Dependencies:
  pip install paho-mqtt arduino-iot-cloud  # or arduino-bridge if available
"""

import json
import time
import logging
import threading
from pathlib import Path

import paho.mqtt.client as mqtt

# ──────────────────────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────────────────────

CONFIG_PATH   = Path("/etc/hvac/config.json")
MQTT_TOPIC_STATUS = "home/hvac/status"
MQTT_TOPIC_CMD    = "home/hvac/cmd"
MQTT_TOPIC_CONFIG = "home/hvac/config"
POLL_INTERVAL_S   = 10

# Default config — overridden by CONFIG_PATH if it exists
DEFAULT_CONFIG = {
    "mqtt_host":           "192.168.1.x",   # UPDATE before deploying
    "mqtt_port":           1883,
    "theater_enabled":     False,
    "vent_minutes_per_hour": 10,
    "mode_override":       "auto",
}

# ──────────────────────────────────────────────────────────────
# KEY MAP — MCU compact keys → human-readable MQTT field names
# See docs/mqtt-payload-spec.md for full reference
# ──────────────────────────────────────────────────────────────

KEY_MAP = {
    "hc": "high_cool",          "lc": "low_cool",
    "hh": "high_heat",          "rv": "reversing_valve",
    "td": "theater_damper",     "dd": "downstairs_damper",
    "vo": "vent_open",          "dh": "dehumidifier_on",
    "it": "indoor_temp_f",      "ih": "indoor_humidity_pct",
    "id": "indoor_dewpoint_f",
    "ot": "outdoor_temp_f",     "oh": "outdoor_humidity_pct",
    "od": "outdoor_dewpoint_f",
    "av": "ac_voltage_v",       "aa": "ac_current_a",
    "aw": "ac_power_w",         "ak": "ac_energy_kwh",
    "af": "ac_frequency_hz",    "ap": "ac_power_factor",
    "dv": "dehum_voltage_v",    "da": "dehum_current_a",
    "dw": "dehum_power_w",      "dk": "dehum_energy_kwh",
    "df": "dehum_frequency_hz", "dp": "dehum_power_factor",
}


# ──────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────

def load_config() -> dict:
    """Load config from disk, falling back to defaults."""
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            stored = json.load(f)
        return {**DEFAULT_CONFIG, **stored}
    return DEFAULT_CONFIG.copy()


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def derive_mode(payload: dict) -> str:
    if payload.get("high_heat"):    return "heat"
    if payload.get("high_cool"):    return "high_cool"
    if payload.get("low_cool"):     return "low_cool"
    return "off"


def expand_keys(compact: dict) -> dict:
    """Expand MCU short keys to full MQTT field names."""
    return {KEY_MAP.get(k, k): v for k, v in compact.items()}


# ──────────────────────────────────────────────────────────────
# MCU BRIDGE (TODO: implement with Arduino Bridge RPC)
# ──────────────────────────────────────────────────────────────

def read_mcu_state() -> dict | None:
    """
    Poll current state from MCU via Arduino Bridge RPC.
    Returns expanded dict or None on failure.

    TODO: Replace stub with actual Bridge.get() or equivalent RPC call.
    The MCU exposes state as a compact JSON string via Bridge key "hvac_state".
    """
    # STUB — replace with:
    # from arduino import Bridge
    # raw = Bridge.get("hvac_state")
    # return expand_keys(json.loads(raw)) if raw else None
    logging.warning("read_mcu_state: Bridge RPC not yet implemented")
    return None


def push_config_to_mcu(cfg: dict) -> None:
    """
    Push configuration to MCU via Arduino Bridge RPC.

    TODO: Replace stub with Bridge.put() calls for each config key.
    """
    logging.warning("push_config_to_mcu: Bridge RPC not yet implemented")


# ──────────────────────────────────────────────────────────────
# MQTT COMMAND HANDLER
# ──────────────────────────────────────────────────────────────

def handle_command(cfg: dict, payload_str: str) -> dict:
    """
    Parse a command from home/hvac/cmd and apply it.
    Returns updated config dict.
    """
    try:
        cmd = json.loads(payload_str)
    except json.JSONDecodeError:
        logging.error("Invalid command JSON: %s", payload_str)
        return cfg

    name = cmd.get("cmd")

    if name == "theater":
        cfg["theater_enabled"] = bool(cmd.get("enabled", False))
        push_config_to_mcu(cfg)

    elif name == "vent":
        minutes = int(cmd.get("minutesPerHour", 10))
        cfg["vent_minutes_per_hour"] = max(0, min(60, minutes))
        push_config_to_mcu(cfg)

    elif name == "mode_override":
        cfg["mode_override"] = cmd.get("mode", "auto")
        push_config_to_mcu(cfg)

    elif name == "reset_energy":
        # TODO: send PZEM energy reset command via MCU Bridge
        logging.info("Energy reset requested for circuit: %s", cmd.get("circuit"))

    else:
        logging.warning("Unknown command: %s", name)

    save_config(cfg)
    return cfg


# ──────────────────────────────────────────────────────────────
# MAIN LOOP
# ──────────────────────────────────────────────────────────────

def run():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    cfg = load_config()

    client = mqtt.Client()
    # TODO: add client.username_pw_set() if broker requires auth

    def on_connect(c, userdata, flags, rc):
        logging.info("MQTT connected (rc=%d)", rc)
        c.subscribe(MQTT_TOPIC_CMD, qos=1)

    def on_message(c, userdata, msg):
        nonlocal cfg
        logging.info("CMD received: %s", msg.payload.decode())
        cfg = handle_command(cfg, msg.payload.decode())
        # Publish updated config state
        config_payload = {
            "theater_enabled":       cfg["theater_enabled"],
            "vent_minutes_per_hour": cfg["vent_minutes_per_hour"],
            "mode_override":         cfg["mode_override"],
            "config_updated_at":     int(time.time()),
        }
        c.publish(MQTT_TOPIC_CONFIG, json.dumps(config_payload), retain=True)

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(cfg["mqtt_host"], cfg["mqtt_port"], keepalive=60)
    client.loop_start()

    while True:
        state = read_mcu_state()
        if state:
            state["timestamp"]     = int(time.time())
            state["mode"]          = derive_mode(state)
            state["compressor_on"] = state.get("ac_current_a", 0) > 5.0
            client.publish(MQTT_TOPIC_STATUS, json.dumps(state), retain=True)
            logging.info("Published status: mode=%s", state["mode"])
        else:
            logging.warning("MCU state read failed — skipping publish")

        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    run()
