# NCD WiFi AC Current Monitor — Device Documentation

Product: [AC Current Monitor Sensor for MQTT over WiFi](https://store.ncd.io/product/ac-current-monitor-sensor-for-mqtt-over-wifi/)
Installed unit web interface: **`http://ncd-1ec4.local/`** (mDNS on local WiFi)

---

## What it does

Clamps non-invasively around a single AC conductor and publishes RMS current
readings as JSON to any MQTT broker over WiFi. No wiring into the controller
enclosure is required — only DC power and WiFi. Eliminates the need for the
SDM120 Modbus meters on circuits where energy (kWh) data is not needed and
current (A) alone is sufficient.

---

## Electrical specifications

| Parameter | Value |
|-----------|-------|
| Supply voltage | 6–32 V DC (included supply: 12V 1.5A) |
| WiFi | IEEE 802.11 b/g/n, 2.4 GHz |
| WiFi security | WPA / WPA2 / WPA2-Enterprise |
| IP addressing | DHCP or static IP |
| Enclosure rating | IP65 |
| Mounting | Wall-mount or magnetic mount (accessory) |

**CT clamp current ranges** (select at time of order):

| CT model | Full-scale | Resolution |
|----------|-----------|------------|
| 100A CT | 100 A | 50 mA |
| 200A CT | 200 A | 100 mA |
| 600A CT | 600 A | 300 mA |
| 1000A CT | 1000 A | 500 mA |

Install the CT clamp around the **live (hot) wire only** — never around both
live and neutral together, or the fields cancel and the reading is zero.

---

## Configuration

The device exposes a **SoftAP web interface** for initial setup — no app or
software download needed.

| Interface | URL / method |
|-----------|-------------|
| Web config UI | `http://ncd-1ec4.local/` (mDNS) or by IP address |
| Initial pairing | Device creates its own WiFi AP on first boot; connect to it, enter home WiFi credentials + MQTT broker details |

**Parameters configurable via the web UI:**

- Home WiFi SSID and password
- MQTT broker host and port
- MQTT topic name (publish topic)
- Data transmission interval (time-based, e.g. every N seconds)
- Change-detection threshold: publish only when current changes by more than N mA
  (useful for avoiding constant publishes when load is stable)
- Static IP / DHCP selection
- Sensor calibration offset

---

## MQTT integration

| Parameter | Configured value for this installation |
|-----------|----------------------------------------|
| Broker | Local mosquitto on controller (`192.168.1.197:1883`) |
| Topic | `jchriswaters_current_sensor` |
| Auth | None (anonymous allowed on LAN listener) |
| TLS | Disabled |
| Payload format | JSON |
| Device IP | `192.168.1.244` (DHCP — pin via router reservation if needed) |

**Published payload format** (confirmed from live unit):

```json
{
  "transmission_count": 12345,
  "rms_current_ma": 24160
}
```

- `rms_current_ma` — RMS current in **milliamps**; divide by 1000 for amps.
- `transmission_count` — monotonically increasing counter; can be used to detect missed messages.
- The device publishes every few seconds (change-detection threshold also applies).

**Capture a live payload:**

```bash
ssh arduino@192.168.1.197
mosquitto_sub -h localhost -t "jchriswaters_current_sensor" -v
```

---

## Power wiring

Power the unit from the 12V DIN-rail supply inside the enclosure via a
dedicated 2-conductor run through a cable gland. The device draws well
under 1A at 12V. The CT clamp attaches directly to the monitored conductor
— no break in the AC wiring.

---

## Integration with bridge_daemon

The NCD sensor publishes to local mosquitto; `bridge_daemon.py` subscribes and
incorporates the reading into every status payload and history DB write.

**bridge_daemon behaviour:**

- Subscribes to `jchriswaters_current_sensor` on startup (alongside the command topic).
- Parses `rms_current_ma` and converts to amps (÷ 1000) → `main_current_a`.
- Marks `main_current_stale = True` if no message received within 120 s.
- Writes `main_current_a` to the SQLite history DB (`/home/arduino/hvac-controller/data/history.db`) every 20 s alongside temp/humidity.

**Derived value — House Main kW:**

The HMI dashboard card derives total power from current using a fixed 230V assumption:

```
kW = main_current_a × 230 / 1000
```

This is displayed alongside the raw amp reading on the "House Main" hero card.

---

## Comparison with SDM120 Modbus meters

| Feature | NCD WiFi current monitor | Eastron SDM120-Modbus |
|---------|--------------------------|----------------------|
| Wiring to controller | None (WiFi) | RS485 bus |
| Measurements | RMS current (A) | V, A, W, VA, PF, Hz, kWh |
| Non-invasive install | Yes (CT clamp) | Yes (CT clamp variant) |
| Configuration | Web browser | Front-panel buttons |
| Data path | MQTT → local mosquitto → bridge_daemon | bridge_daemon poll → MQTT |
| IP rating | IP65 | IP20 (DIN-rail, indoor) |
| Power supply | 6–32V DC (self-contained) | 80–270VAC (from monitored circuit) |

Use the SDM120 where energy (kWh) accumulation and full power factor data
are needed. Use the NCD WiFi sensor where a simple current reading is
sufficient and running RS485 wiring is impractical.
