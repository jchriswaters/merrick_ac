#!/usr/bin/env python3
"""
bridge_daemon.py  —  Uno Q Linux-side HVAC bridge daemon

Responsibilities:
  1. Poll MCU relay output + thermostat input states via Arduino Bridge RPC
  2. Poll all RS485 sensors (2× SHT30 temp/hum, 2× SDM120 energy meters)
  3. Compute SensorFlags from environmental readings and push to MCU via Bridge
  4. Assemble full MQTT status payload and publish every 10 s (retained)
  5. Publish home/hvac/config retained topic on startup and on every change
  6. Subscribe to home/hvac/cmd and apply configuration changes

See docs/mqtt-payload-spec.md  — full payload schema and field reference
See docs/system-design.md §5d  — SensorFlags architecture and Bridge RPC keys

Dependencies (install on Uno Q Debian Linux):
  pip3 install paho-mqtt pymodbus==3.6.*
  bridgeclient is pre-installed as part of the Arduino Bridge package

Run as a systemd service: see linux/hvac-bridge.service
"""

import json
import logging
import os
import socket
import struct
import threading
import time
import collections
from pathlib import Path
from typing import Optional

import paho.mqtt.client as mqtt

# Arduino RouterBridge Python client — pre-installed on Uno Q Debian Linux.
# Uses RPC (Bridge.call / Bridge.provide) not the old Yún key-value put/get.
# The Bridge singleton communicates with the Go router process over IPC.
try:
    from arduino.app_utils import Bridge as _MCUBridge  # type: ignore
    _bridge_available = True
except ImportError:
    _MCUBridge = None
    _bridge_available = False   # allows the file to parse on dev machines

# pymodbus 3.x (pip3 install pymodbus)
try:
    from pymodbus.client import ModbusSerialClient
    from pymodbus.exceptions import ModbusException
except ImportError:
    ModbusSerialClient = None
    ModbusException = Exception

# ──────────────────────────────────────────────────────────────
# CONSTANTS
# ──────────────────────────────────────────────────────────────

# Resolution order:
#   1. HVAC_CONFIG_PATH env var (override for tests / unusual installs)
#   2. ~/.config/hvac/config.json  — writable by the service user (arduino)
#   3. /etc/hvac/config.json       — read-only fallback (legacy path)
#
# Previously this was hard-coded to /etc/hvac/config.json, but the service
# now runs as the `arduino` user (not root), so writes there fail with
# EACCES and setConfig changes don't persist across reboots.  Falling back
# to the legacy /etc/hvac path on read keeps existing installs working.
_xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
_legacy = Path("/etc/hvac/config.json")
_user_default = Path(_xdg) / "hvac" / "config.json"
CONFIG_PATH = Path(os.environ.get("HVAC_CONFIG_PATH") or (
    _legacy if _legacy.exists() and not _user_default.exists() else _user_default
))
RS485_PORT     = "/dev/ttyUSB0"   # Waveshare USB-RS485 adapter
RS485_BAUD     = 9600
RS485_TIMEOUT  = 1.0              # seconds per Modbus request

POLL_INTERVAL_S = 10

MQTT_STATUS  = "home/hvac/status"
MQTT_CONFIG  = "home/hvac/config"
MQTT_CMD     = "home/hvac/cmd"

# Modbus slave addresses
ADDR_SHT30_INDOOR  = 0x01   # RS485 SHT30 — indoor, return-air location
ADDR_SHT30_OUTDOOR = 0x02   # RS485 SHT30 — outdoor, shaded enclosure
ADDR_SDM120_AC     = 0x03   # Eastron SDM120 — AC system (compressor + air handler)
ADDR_SDM120_DEHUM  = 0x04   # Eastron SDM120 — dehumidifier circuit

# SDM120 input register base addresses (pairs of 16-bit regs = one IEEE 754 float)
# Function code 04 (read input registers)
SDM120_VOLTAGE    = 0x0000
SDM120_CURRENT    = 0x0006
SDM120_POWER      = 0x000C
SDM120_APPARENT   = 0x0012
SDM120_PF         = 0x001E
SDM120_FREQ       = 0x0046
SDM120_ENERGY_IMP = 0x0156

# SHT30 RS485 transmitter register map (confirmed against hardware 2026-05-25)
# Function code 03 (read holding registers), start = 0x0000, count = 2
# Register 0: temperature × 100, signed int16, unit = 0.01 °C
# Register 1: humidity    × 100, unsigned int16, unit = 0.01 %RH
# Register 2: status/flags byte — NOT dew point; do not read as temperature
# Verified: raw 2474 → 24.74 °C, raw 4640 → 46.40 %RH
SHT30_START = 0x0000
SHT30_COUNT = 2   # read only the two data registers; skip register 2 (status)

# SensorFlags: rolling indoor temperature history for tempRisingFast check
TEMP_HISTORY_MAXLEN  = 120         # 120 samples × 10 s = 20 min of history
TEMP_RISE_WINDOW_S   = 900.0       # 15-minute evaluation window
TEMP_RISE_THRESH_F   = 1.0         # indoor must rise ≥ 1 °F in window → "keeping up"

# MCU RPC bitmask field order — must match rpc_get_outputs / rpc_get_inputs
# in hvac_controller.ino (positional, index 0-8).
MCU_OUTPUT_FIELDS = [
    "high_cool", "low_cool", "high_heat", "reversing_valve",
    "theater_damper", "downstairs_damper", "vent_open", "dehumidifier_on", "fan_on",
]
MCU_INPUT_FIELDS = [
    "input_main_low_cool", "input_main_high_cool", "input_main_heat",
    "input_theater_cool",  "input_theater_heat",
    "input_downstairs_cool", "input_downstairs_heat",
    "input_high_humidity", "input_vent_in",
]

# ──────────────────────────────────────────────────────────────
# DEFAULT CONFIG  (overridden by CONFIG_PATH when it exists)
# ──────────────────────────────────────────────────────────────

DEFAULT_CONFIG: dict = {
    "mqtt_host":                "localhost",   # UPDATE to your broker IP
    "mqtt_port":                1883,
    "mqtt_username":            None,          # None = no auth
    "mqtt_password":            None,
    "theater_enabled":          False,
    "downstairs_enabled":       True,
    "vent_minutes_per_hour":    10,
    "mode_override":            "auto",        # "auto" | "off"
    "heat_pump_min_temp_f":     40,            # aux heat engages below this °F
    "free_cool_max_temp_f":     60,            # free-cooling vent opens below this °F
    "high_humidity_pct":        80,            # OUTDOOR humidity — vent forced off at or above this %RH
    "indoor_humidity_low_pct":  55,            # INDOOR humidity — dehumidifier-only is sufficient below this %RH
    "indoor_humidity_high_pct": 65,            # INDOOR humidity — force high_cool emergency dehum at or above this %RH
    "dehum_max_minutes":        20,            # dehumidifier timeout before forced high_cool
    "mcu_hang_threshold_s":     60,            # seconds of MCU silence before publishing mcu_healthy=False
    "mcu_auto_recover":         False,         # if True (and sudoers allows), restart arduino-router after threshold
}

# Allowed keys, (min, max, type) — None = no range check
SETCONFIG_SCHEMA: dict = {
    "heat_pump_min_temp_f":     (20,   60, int),
    "free_cool_max_temp_f":     (40,   80, int),
    "high_humidity_pct":        (50,  100, int),
    "indoor_humidity_low_pct":  (30,   70, int),
    "indoor_humidity_high_pct": (40,   90, int),
    "dehum_max_minutes":        ( 5,  120, int),
    "vent_minutes_per_hour":    ( 0,   60, int),
    "theater_enabled":          (None, None, bool),
    "downstairs_enabled":       (None, None, bool),
    "mode_override":            (None, None, str),
    "mcu_hang_threshold_s":     (10,  600, int),
    "mcu_auto_recover":         (None, None, bool),
}

log = logging.getLogger("hvac")

# ──────────────────────────────────────────────────────────────
# CONFIG PERSISTENCE
# ──────────────────────────────────────────────────────────────

def load_config() -> dict:
    """Load config from disk, merging with defaults for any missing keys."""
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as fh:
                stored = json.load(fh)
            return {**DEFAULT_CONFIG, **stored}
        except Exception as exc:
            log.error("Failed to load config from %s: %s — using defaults", CONFIG_PATH, exc)
    return DEFAULT_CONFIG.copy()


def save_config(cfg: dict) -> None:
    """Persist config to disk atomically."""
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".tmp")
        with open(tmp, "w") as fh:
            json.dump(cfg, fh, indent=2)
        tmp.replace(CONFIG_PATH)
    except Exception as exc:
        log.error("Failed to save config: %s", exc)

# ──────────────────────────────────────────────────────────────
# RS485 MODBUS — SHT30 TEMPERATURE / HUMIDITY SENSORS
# ──────────────────────────────────────────────────────────────

def _raw_to_signed(val: int) -> int:
    """Convert unsigned 16-bit Modbus register value to signed int16."""
    return val - 65536 if val > 32767 else val


def _mb_read_holding(modbus, address: int, count: int, dev_addr: int):
    """
    Version-agnostic read_holding_registers call.

    pymodbus renamed the slave-address keyword across major versions:
      2.x        : unit=
      3.0–3.12   : slave=
      3.13+      : device_id=
    Try each in order so the daemon runs on whatever version pip installs.
    """
    for kw in ("device_id", "slave", "unit"):
        try:
            return modbus.read_holding_registers(
                address=address, count=count, **{kw: dev_addr}
            )
        except TypeError:
            continue
    return modbus.read_holding_registers(address, count, dev_addr)


def _mb_read_input(modbus, address: int, count: int, dev_addr: int):
    """Version-agnostic read_input_registers call (same keyword evolution)."""
    for kw in ("device_id", "slave", "unit"):
        try:
            return modbus.read_input_registers(
                address=address, count=count, **{kw: dev_addr}
            )
        except TypeError:
            continue
    return modbus.read_input_registers(address, count, dev_addr)


def _mb_write_register(modbus, address: int, value: int, dev_addr: int):
    """Version-agnostic write_register call."""
    for kw in ("device_id", "slave", "unit"):
        try:
            return modbus.write_register(
                address=address, value=value, **{kw: dev_addr}
            )
        except TypeError:
            continue
    return modbus.write_register(address, value, dev_addr)


def read_sht30(modbus, addr: int, label: str) -> Optional[dict]:
    """
    Read temperature and humidity from one SHT30 RS485 transmitter.

    Register map (confirmed against hardware):
      [0] temperature × 100, signed int16  → divide by 100 for °C
      [1] humidity    × 100, unsigned int16 → divide by 100 for %RH
      [2] status byte — ignored

    Returns a dict with keys {label}_temp_f and {label}_humidity_pct,
    or None if the read fails.
    """
    try:
        resp = _mb_read_holding(modbus, SHT30_START, SHT30_COUNT, addr)
        if resp.isError():
            log.warning("SHT30 addr=0x%02X (%s) read error", addr, label)
            return None

        temp_c = _raw_to_signed(resp.registers[0]) / 100.0
        hum    = resp.registers[1] / 100.0
        temp_f = temp_c * 9.0 / 5.0 + 32.0

        return {
            f"{label}_temp_f":       round(temp_f, 1),
            f"{label}_humidity_pct": round(hum, 1),
        }

    except ModbusException as exc:
        log.warning("SHT30 addr=0x%02X (%s) exception: %s", addr, label, exc)
        return None

# ──────────────────────────────────────────────────────────────
# RS485 MODBUS — SDM120 ENERGY METERS
# ──────────────────────────────────────────────────────────────

def _read_sdm120_float(modbus, addr: int, reg: int) -> Optional[float]:
    """Read one IEEE 754 float from two consecutive SDM120 input registers."""
    try:
        resp = _mb_read_input(modbus, reg, 2, addr)
        if resp.isError():
            return None
        packed = struct.pack(">HH", resp.registers[0], resp.registers[1])
        return struct.unpack(">f", packed)[0]
    except (ModbusException, struct.error):
        return None


def read_sdm120(modbus: "ModbusSerialClient", addr: int, prefix: str) -> Optional[dict]:
    """
    Read voltage, current, power, apparent power, power factor, frequency,
    and import energy from one Eastron SDM120 Modbus energy meter.

    prefix is "ac" or "dehum" — used as field name prefix in the MQTT payload.
    Returns a dict or None if all reads fail.
    """
    reads = {
        SDM120_VOLTAGE:    (f"{prefix}_voltage_v",    1),
        SDM120_CURRENT:    (f"{prefix}_current_a",    2),
        SDM120_POWER:      (f"{prefix}_power_w",      0),
        SDM120_APPARENT:   (f"{prefix}_apparent_va",  0),
        SDM120_PF:         (f"{prefix}_power_factor", 2),
        SDM120_FREQ:       (f"{prefix}_frequency_hz", 1),
        SDM120_ENERGY_IMP: (f"{prefix}_energy_kwh",   3),
    }
    result = {}
    for reg, (field, decimals) in reads.items():
        val = _read_sdm120_float(modbus, addr, reg)
        if val is not None:
            result[field] = round(val, decimals)

    if not result:
        log.warning("SDM120 addr=0x%02X (%s) — all reads failed", addr, prefix)
        return None
    return result


def reset_sdm120_energy(modbus: "ModbusSerialClient", addr: int) -> bool:
    """
    Reset the SDM120 energy accumulator for the given meter address.

    NOTE: The SDM120 does not support a Modbus energy-reset command on all
    firmware versions. If this fails, the meter must be reset via the front-panel
    button sequence (hold SET until 'rSt' appears). Returns True on success.
    """
    try:
        # Write 0x0003 to holding register 0xF010 — SDM120 reset command
        resp = _mb_write_register(modbus, 0xF010, 0x0003, addr)
        if resp.isError():
            log.warning("SDM120 energy reset: Modbus error for addr=0x%02X", addr)
            return False
        log.info("SDM120 energy reset sent to addr=0x%02X", addr)
        return True
    except ModbusException as exc:
        log.warning("SDM120 energy reset exception (addr=0x%02X): %s — use front-panel reset", addr, exc)
        return False

# ──────────────────────────────────────────────────────────────
# SENSOR FLAGS COMPUTATION
# ──────────────────────────────────────────────────────────────

# Rolling indoor temperature history: deque of (timestamp_s, temp_f) tuples
# Oldest entries at the left; newest at the right.
_temp_history: collections.deque = collections.deque(maxlen=TEMP_HISTORY_MAXLEN)


def _update_temp_history(indoor_temp_f: float) -> None:
    _temp_history.append((time.time(), indoor_temp_f))


def _compute_temp_rising_fast(current_temp_f: float) -> bool:
    """
    Return True if the indoor temperature has risen >= TEMP_RISE_THRESH_F in
    the last TEMP_RISE_WINDOW_S seconds.

    Defaults to True when there is insufficient history — this keeps the heat
    pump on its low stage until 15 minutes of data is available.
    """
    if len(_temp_history) < 3:
        return True   # not enough data yet — default: assume keeping up

    window_start = time.time() - TEMP_RISE_WINDOW_S
    # Find the oldest sample still within the evaluation window
    oldest_in_window = None
    for ts, temp in _temp_history:          # iterates oldest → newest (left → right)
        if ts >= window_start:
            oldest_in_window = (ts, temp)
            break

    if oldest_in_window is None:
        # All history is older than the window — use the most recent entry
        oldest_in_window = _temp_history[-1]

    delta = current_temp_f - oldest_in_window[1]
    return delta >= TEMP_RISE_THRESH_F


def compute_sensor_flags(
    outdoor_temp_f:       Optional[float],
    outdoor_humidity_pct: Optional[float],
    indoor_temp_f:        Optional[float],
    indoor_humidity_pct:  Optional[float],
    cfg:                  dict,
) -> dict:
    """
    Evaluate environmental thresholds and return a dict of SensorFlag booleans
    ready to push to the MCU via Bridge.

    Returns cautious defaults if sensor readings are unavailable.
    """
    heat_min      = cfg["heat_pump_min_temp_f"]
    cool_max      = cfg["free_cool_max_temp_f"]
    hum_limit     = cfg["high_humidity_pct"]
    in_hum_low    = cfg["indoor_humidity_low_pct"]
    in_hum_high   = cfg["indoor_humidity_high_pct"]

    # Default to safe values when sensor data is absent
    if outdoor_temp_f is not None:
        heat_pump_ok    = outdoor_temp_f >= heat_min
        aux_heat_needed = outdoor_temp_f < heat_min
        vent_temp_ok    = outdoor_temp_f < cool_max
    else:
        log.warning("Outdoor temp unavailable — defaulting heatPumpOk=True, ventTempOk=False")
        heat_pump_ok    = True
        aux_heat_needed = False
        vent_temp_ok    = False

    if outdoor_humidity_pct is not None:
        vent_blocked = outdoor_humidity_pct >= hum_limit
        vent_hum_ok  = outdoor_humidity_pct < hum_limit
    else:
        log.warning("Outdoor humidity unavailable — defaulting ventBlocked=True")
        vent_blocked = True
        vent_hum_ok  = False

    if indoor_temp_f is not None:
        _update_temp_history(indoor_temp_f)
        temp_rising_fast = _compute_temp_rising_fast(indoor_temp_f)
    else:
        log.warning("Indoor temp unavailable — defaulting tempRisingFast=True")
        temp_rising_fast = True   # safe default: stay on low stage

    # Indoor humidity drives two thresholds:
    #   • humidityModerate — RH >= low threshold (dehumidifier may not keep up)
    #   • humidityHigh     — RH >= high threshold (emergency: force high_cool)
    # If the reading is missing, default both to FALSE so we don't trigger
    # emergency cooling on a sensor outage.
    if indoor_humidity_pct is not None:
        humidity_moderate = indoor_humidity_pct >= in_hum_low
        humidity_high     = indoor_humidity_pct >= in_hum_high
    else:
        log.warning("Indoor humidity unavailable — defaulting humidityModerate/High=False")
        humidity_moderate = False
        humidity_high     = False

    return {
        "heatPumpOk":        heat_pump_ok,
        "auxHeatNeeded":     aux_heat_needed,
        "tempRisingFast":    temp_rising_fast,
        "ventOk":            vent_temp_ok and vent_hum_ok,
        "ventBlocked":       vent_blocked,
        "humidityModerate":  humidity_moderate,
        "humidityHigh":      humidity_high,
    }

# ──────────────────────────────────────────────────────────────
# ARDUINO BRIDGE — MCU STATE READ AND CONFIG/FLAGS PUSH
# Uses Arduino_RouterBridge RPC calls (not old Yún put/get).
# Bridge.call() is synchronous: blocks until MCU responds (~1-10 ms).
# ──────────────────────────────────────────────────────────────

def read_mcu_state() -> dict:
    """
    Read relay output and thermostat input states from the MCU via RPC.

    Calls get_outputs() and get_inputs() on the MCU, which return 9-char
    bitmask strings ('0'/'1' per channel, order defined by MCU_OUTPUT_FIELDS
    and MCU_INPUT_FIELDS).  Returns a dict with human-readable field names.
    """
    if not _bridge_available:
        return {}
    try:
        out_str = _MCUBridge.call("get_outputs") or ""
        inp_str = _MCUBridge.call("get_inputs")  or ""
    except Exception as exc:
        log.warning("MCU Bridge read failed: %s", exc)
        return {}

    result: dict = {}
    for i, field in enumerate(MCU_OUTPUT_FIELDS):
        if i < len(out_str):
            result[field] = out_str[i] == "1"
    for i, field in enumerate(MCU_INPUT_FIELDS):
        if i < len(inp_str):
            result[field] = inp_str[i] == "1"
    return result


def push_sensor_flags_to_mcu(flags: dict) -> None:
    """
    Push pre-computed SensorFlags to the MCU via RPC set_flags().
    Argument order must match rpc_set_flags() in hvac_controller.ino.
    """
    if not _bridge_available:
        return
    try:
        _MCUBridge.call("set_flags",
            bool(flags.get("heatPumpOk",       True)),
            bool(flags.get("auxHeatNeeded",    False)),
            bool(flags.get("tempRisingFast",   True)),
            bool(flags.get("ventOk",           False)),
            bool(flags.get("ventBlocked",      False)),
            bool(flags.get("humidityModerate", False)),
            bool(flags.get("humidityHigh",     False)),
        )
    except Exception as exc:
        log.warning("MCU Bridge set_flags failed: %s", exc)


# ──────────────────────────────────────────────────────────────
# WATCHDOG / HEALTH MONITORING
# ──────────────────────────────────────────────────────────────

def _sd_notify(message: str) -> bool:
    """
    Send a notification message to systemd (sd_notify protocol).

    Used for two things:
      • READY=1 once we've finished startup (required by Type=notify)
      • WATCHDOG=1 each polling cycle, so systemd kills+restarts us if
        the main loop hangs (configured via WatchdogSec= in the unit)

    Returns True if the message was sent, False if NOTIFY_SOCKET wasn't
    set (running outside systemd — fine, just a no-op).
    """
    sock_path = os.environ.get("NOTIFY_SOCKET")
    if not sock_path:
        return False
    # Abstract socket starts with '@' on the env var, '\0' on the wire
    if sock_path.startswith("@"):
        sock_path = "\0" + sock_path[1:]
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sock.connect(sock_path)
            sock.sendall(message.encode())
        finally:
            sock.close()
        return True
    except Exception as exc:
        log.debug("sd_notify(%r) failed: %s", message, exc)
        return False


# MCU health state.  We track the timestamp of the last successful
# MCU RPC read; bridge_daemon publishes a derived `mcu_healthy` field
# in the status payload so the HMI can show it, and (optionally) tries
# to recover by restarting arduino-router when the hang threshold is hit.
_mcu_last_ok_ts: float = 0.0
_mcu_last_recover_ts: float = 0.0
_mcu_recover_cooldown_s: float = 120.0   # don't attempt more than once per 2 min


def _try_recover_mcu() -> None:
    """
    Best-effort MCU recovery: restart arduino-router via systemd.
    The router unit's ExecStartPre cycles SRST (GPIO 38) which resets
    the MCU; the after-ready hook then releases it once the router is
    listening on /dev/ttyHS1 again.

    Requires sudoers to allow:
        arduino ALL=(root) NOPASSWD: /bin/systemctl restart arduino-router.service
    If sudoers isn't configured, the call fails silently and only the
    detection / reporting half of the watchdog runs.
    """
    global _mcu_last_recover_ts
    now = time.monotonic()
    if now - _mcu_last_recover_ts < _mcu_recover_cooldown_s:
        return
    _mcu_last_recover_ts = now
    log.error("MCU unresponsive — attempting recovery via "
              "`sudo -n systemctl restart arduino-router.service`")
    try:
        import subprocess
        rc = subprocess.run(
            ["sudo", "-n", "systemctl", "restart", "arduino-router.service"],
            capture_output=True, timeout=15,
        )
        if rc.returncode == 0:
            log.warning("MCU recovery: arduino-router restarted OK")
        else:
            log.error("MCU recovery failed (rc=%d, stderr=%r). "
                      "Check sudoers — see docs/PROJECT-STATUS.md.",
                      rc.returncode, rc.stderr.decode(errors="replace")[:200])
    except Exception as exc:
        log.error("MCU recovery subprocess raised: %s", exc)


def push_config_to_mcu(cfg: dict) -> None:
    """
    Push MCU-relevant config to the MCU via RPC set_config().
    Argument order must match rpc_set_config() in hvac_controller.ino.
    """
    if not _bridge_available:
        return
    try:
        _MCUBridge.call("set_config",
            bool(cfg.get("theater_enabled",       False)),
            int(cfg.get("vent_minutes_per_hour",  10)),
            cfg.get("mode_override", "auto") == "off",
            int(cfg.get("dehum_max_minutes",       20)),
            bool(cfg.get("downstairs_enabled",    True)),
        )
    except Exception as exc:
        log.warning("MCU Bridge set_config failed: %s", exc)

# ──────────────────────────────────────────────────────────────
# PAYLOAD ASSEMBLY
# ──────────────────────────────────────────────────────────────

def derive_mode(s: dict) -> str:
    """
    Derive a human-readable operating mode string from relay output states.
    The reversing valve distinguishes heat-pump heating from compressor cooling.
    """
    rv = s.get("reversing_valve", False)
    hc = s.get("high_cool",       False)
    lc = s.get("low_cool",        False)
    fn = s.get("fan_on",          False)
    dh = s.get("dehumidifier_on", False)

    if (hc or lc) and rv:  return "heat"       # heat pump: revValve energised
    if hc:                 return "high_cool"
    if lc:                 return "low_cool"
    if dh:                 return "dehum"
    if fn:                 return "fan"
    return "off"


def build_config_payload(cfg: dict) -> dict:
    """Build the home/hvac/config MQTT payload from the current config dict."""
    return {
        "theater_enabled":          cfg.get("theater_enabled", False),
        "downstairs_enabled":       cfg.get("downstairs_enabled", True),
        "vent_minutes_per_hour":    cfg.get("vent_minutes_per_hour", 10),
        "mode_override":            cfg.get("mode_override", "auto"),
        "heat_pump_min_temp_f":     cfg.get("heat_pump_min_temp_f", 40),
        "free_cool_max_temp_f":     cfg.get("free_cool_max_temp_f", 60),
        "high_humidity_pct":        cfg.get("high_humidity_pct", 80),
        "indoor_humidity_low_pct":  cfg.get("indoor_humidity_low_pct", 55),
        "indoor_humidity_high_pct": cfg.get("indoor_humidity_high_pct", 65),
        "dehum_max_minutes":        cfg.get("dehum_max_minutes", 20),
        "mcu_hang_threshold_s":     cfg.get("mcu_hang_threshold_s", 60),
        "mcu_auto_recover":         cfg.get("mcu_auto_recover", False),
        "config_updated_at":        int(time.time()),
    }

# ──────────────────────────────────────────────────────────────
# MQTT COMMAND HANDLER
# ──────────────────────────────────────────────────────────────

def validate_setconfig(key: str, value) -> Optional[object]:
    """
    Validate a setConfig key/value pair against SETCONFIG_SCHEMA.
    Returns the coerced value, or None if validation fails (logs the reason).
    """
    if key not in SETCONFIG_SCHEMA:
        log.warning("setConfig: unknown key '%s'", key)
        return None

    lo, hi, typ = SETCONFIG_SCHEMA[key]

    # mode_override: only "auto" or "off" accepted
    if key == "mode_override":
        if value not in ("auto", "off"):
            log.warning("setConfig: mode_override must be 'auto' or 'off', got '%s'", value)
            return None
        return value

    # boolean keys
    if typ is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, str)):
            return bool(value)
        log.warning("setConfig: expected bool for '%s', got %r", key, value)
        return None

    # integer keys with range check
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        log.warning("setConfig: expected int for '%s', got %r", key, value)
        return None
    if lo is not None and coerced < lo:
        log.warning("setConfig: '%s' value %d below minimum %d", key, coerced, lo)
        return None
    if hi is not None and coerced > hi:
        log.warning("setConfig: '%s' value %d above maximum %d", key, coerced, hi)
        return None
    return coerced


def handle_command(cfg: dict, mqtt_client: mqtt.Client, payload_str: str) -> dict:
    """
    Parse a home/hvac/cmd message, apply changes, persist config, push to MCU,
    and republish home/hvac/config. Returns the (possibly updated) config dict.
    """
    try:
        cmd = json.loads(payload_str)
    except json.JSONDecodeError as exc:
        log.error("Invalid command JSON: %s — %s", payload_str[:120], exc)
        return cfg

    name = cmd.get("cmd")
    changed = False

    if name == "setConfig":
        key = cmd.get("key")
        raw_value = cmd.get("value")
        if key is None or raw_value is None:
            log.warning("setConfig: missing 'key' or 'value'")
            return cfg
        coerced = validate_setconfig(key, raw_value)
        if coerced is not None:
            cfg[key] = coerced
            changed = True
            log.info("setConfig: %s = %r", key, coerced)

    elif name == "reset_energy":
        circuit = cmd.get("circuit", "all").lower()
        log.info("reset_energy requested for circuit: %s (Modbus reset may not be supported — use SDM120 front panel if this fails)", circuit)
        # Actual Modbus reset attempted in the main polling loop via reset_sdm120_energy()
        # Stash the request in config so the main loop sees it on the next cycle
        cfg["_pending_energy_reset"] = circuit
        changed = False   # don't republish config for this

    else:
        log.warning("Unknown command: %s", name)
        return cfg

    if changed:
        save_config(cfg)
        push_config_to_mcu(cfg)
        config_payload = build_config_payload(cfg)
        mqtt_client.publish(MQTT_CONFIG, json.dumps(config_payload), retain=True, qos=1)
        log.info("Config updated and republished")

    return cfg

# ──────────────────────────────────────────────────────────────
# MAIN RUN LOOP
# ──────────────────────────────────────────────────────────────

def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    cfg = load_config()
    cfg_lock = threading.Lock()   # protects cfg in on_message callback

    # ── Arduino Bridge (RouterBridge RPC) ─────────────────────
    if _bridge_available:
        log.info("Arduino RouterBridge available — MCU RPC enabled")
    else:
        log.warning("arduino.app_utils not available — MCU Bridge disabled")

    # ── RS485 Modbus client ────────────────────────────────────
    modbus: Optional[ModbusSerialClient] = None
    if ModbusSerialClient is not None:
        modbus = ModbusSerialClient(
            port=RS485_PORT,
            baudrate=RS485_BAUD,
            stopbits=1,
            bytesize=8,
            parity="N",
            timeout=RS485_TIMEOUT,
        )
        if modbus.connect():
            log.info("RS485 Modbus connected on %s @ %d baud", RS485_PORT, RS485_BAUD)
        else:
            log.error("RS485 Modbus failed to connect on %s", RS485_PORT)
            modbus = None
    else:
        log.warning("pymodbus not available — RS485 sensor reads disabled")

    # ── MQTT client ────────────────────────────────────────────
    mqtt_client = mqtt.Client(client_id="hvac-bridge", clean_session=True)
    mqtt_client.reconnect_delay_set(min_delay=2, max_delay=60)

    if cfg.get("mqtt_username"):
        mqtt_client.username_pw_set(cfg["mqtt_username"], cfg.get("mqtt_password"))

    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            log.info("MQTT connected to %s:%d", cfg["mqtt_host"], cfg["mqtt_port"])
            client.subscribe(MQTT_CMD, qos=1)
            # Publish current config immediately so HMI and HA have it on reconnect
            with cfg_lock:
                client.publish(MQTT_CONFIG, json.dumps(build_config_payload(cfg)),
                               retain=True, qos=1)
        else:
            log.warning("MQTT connect failed (rc=%d)", rc)

    def on_disconnect(client, userdata, rc):
        if rc != 0:
            log.warning("MQTT unexpectedly disconnected (rc=%d) — will reconnect", rc)

    def on_message(client, userdata, msg):
        nonlocal cfg
        payload_str = msg.payload.decode(errors="replace")
        log.info("CMD  ← %s", payload_str[:200])
        with cfg_lock:
            cfg = handle_command(cfg, client, payload_str)

    mqtt_client.on_connect    = on_connect
    mqtt_client.on_disconnect = on_disconnect
    mqtt_client.on_message    = on_message

    try:
        mqtt_client.connect(cfg["mqtt_host"], int(cfg["mqtt_port"]), keepalive=60)
    except Exception as exc:
        log.error("Initial MQTT connect failed: %s — will retry in background", exc)

    mqtt_client.loop_start()   # handles reconnection automatically in a background thread

    # Push initial config to MCU before entering the polling loop
    with cfg_lock:
        push_config_to_mcu(cfg)

    log.info("Entering polling loop (interval=%d s)", POLL_INTERVAL_S)
    # Tell systemd we're ready (no-op outside systemd).  This is required
    # by Type=notify, so the unit stays "active" instead of "activating".
    _sd_notify("READY=1")
    global _mcu_last_ok_ts
    _mcu_last_ok_ts = time.monotonic()   # assume healthy at startup

    while True:
        loop_start = time.monotonic()
        # Pet the systemd watchdog every iteration.  WatchdogSec= in the
        # unit governs the timeout; if we ever block here for too long
        # (deadlock, RS485 lockup, etc.) systemd kills+restarts us.
        _sd_notify("WATCHDOG=1")

        # ── Snapshot config under lock ─────────────────────────
        with cfg_lock:
            cfg_snap = cfg.copy()
            pending_reset = cfg_snap.pop("_pending_energy_reset", None)
            if pending_reset:
                cfg.pop("_pending_energy_reset", None)

        payload: dict = {}

        # ── Handle pending energy reset ────────────────────────
        if pending_reset and modbus:
            circuits = (
                [(ADDR_SDM120_AC, "ac"), (ADDR_SDM120_DEHUM, "dehum")]
                if pending_reset == "all"
                else [(ADDR_SDM120_AC, "ac") if pending_reset == "ac"
                      else (ADDR_SDM120_DEHUM, "dehum")]
            )
            for addr, label in circuits:
                reset_sdm120_energy(modbus, addr)

        # ── Read MCU relay outputs + thermostat inputs ─────────
        mcu_state = read_mcu_state()
        if mcu_state:
            payload.update(mcu_state)
            _mcu_last_ok_ts = time.monotonic()
        elif _bridge_available:
            log.warning("MCU Bridge read returned no data")

        # MCU health: how long since the last successful MCU RPC read.
        mcu_silence_s = time.monotonic() - _mcu_last_ok_ts
        hang_threshold = float(cfg_snap.get("mcu_hang_threshold_s", 60))
        mcu_healthy   = mcu_silence_s < hang_threshold
        payload["mcu_healthy"]      = mcu_healthy
        payload["mcu_silence_s"]    = round(mcu_silence_s, 1)
        if not mcu_healthy:
            log.error("MCU unresponsive for %.0f s (threshold %.0f s)",
                      mcu_silence_s, hang_threshold)
            if cfg_snap.get("mcu_auto_recover", False):
                _try_recover_mcu()

        # ── Read RS485 sensors ─────────────────────────────────
        indoor_temp_f       = None
        indoor_humidity     = None
        outdoor_temp_f      = None
        outdoor_humidity    = None

        if modbus:
            indoor = read_sht30(modbus, ADDR_SHT30_INDOOR, "indoor")
            if indoor:
                payload.update(indoor)
                indoor_temp_f   = indoor.get("indoor_temp_f")
                indoor_humidity = indoor.get("indoor_humidity_pct")

            outdoor = read_sht30(modbus, ADDR_SHT30_OUTDOOR, "outdoor")
            if outdoor:
                payload.update(outdoor)
                outdoor_temp_f   = outdoor.get("outdoor_temp_f")
                outdoor_humidity = outdoor.get("outdoor_humidity_pct")

            ac_power = read_sdm120(modbus, ADDR_SDM120_AC, "ac")
            if ac_power:
                payload.update(ac_power)

            dehum_power = read_sdm120(modbus, ADDR_SDM120_DEHUM, "dehum")
            if dehum_power:
                payload.update(dehum_power)

        # ── Compute and push SensorFlags to MCU ───────────────
        flags = compute_sensor_flags(
            outdoor_temp_f=outdoor_temp_f,
            outdoor_humidity_pct=outdoor_humidity,
            indoor_temp_f=indoor_temp_f,
            indoor_humidity_pct=indoor_humidity,
            cfg=cfg_snap,
        )
        push_sensor_flags_to_mcu(flags)

        # ── Derive compound fields ─────────────────────────────
        # compressor_on = heat-pump compressor is currently energized.
        # Derived from the MCU's relay state so it remains accurate even
        # while the SDM120 AC current meter isn't wired/reporting.  Both
        # cooling (low_cool / high_cool) and heating (low_cool / high_cool
        # with reversing_valve) run the same compressor; high_heat is
        # auxiliary electric resistance, not the compressor.
        payload["mode"]         = derive_mode(payload)
        payload["compressor_on"] = bool(payload.get("low_cool")) or bool(payload.get("high_cool"))
        payload["timestamp"]    = int(time.time())

        # ── Write status cache for web_config.py ───────────────
        # web_config.py reads /tmp/hvac_status.json for the /api/status endpoint
        try:
            _tmp = Path("/tmp/hvac_status.json.tmp")
            _tmp.write_text(json.dumps(payload))
            _tmp.replace(Path("/tmp/hvac_status.json"))
        except Exception as exc:
            log.debug("Status cache write failed: %s", exc)

        # ── Publish MQTT status (retained) ─────────────────────
        try:
            mqtt_client.publish(MQTT_STATUS, json.dumps(payload), retain=True, qos=0)
            log.info("Status → mode=%-10s  indoor=%-5s°F  outdoor=%-5s°F  hum=%-4s%%",
                     payload["mode"],
                     f"{indoor_temp_f:.1f}"  if indoor_temp_f   is not None else "?",
                     f"{outdoor_temp_f:.1f}" if outdoor_temp_f  is not None else "?",
                     f"{outdoor_humidity:.0f}" if outdoor_humidity is not None else "?")
        except Exception as exc:
            log.error("MQTT publish failed: %s", exc)

        # ── Sleep for the remainder of the poll interval ───────
        elapsed = time.monotonic() - loop_start
        sleep_s = max(0.1, POLL_INTERVAL_S - elapsed)
        time.sleep(sleep_s)


# ──────────────────────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        log.info("Shutdown requested")
