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
| Broker | `broker.hivemq.com` port `1883` |
| Topic | `jchriswaters_merrick_ac_current` *(suggested — confirm in web UI)* |
| Auth | None (HiveMQ public broker) |
| TLS | Optional (HiveMQ public supports TLS on port 8883) |
| Payload format | JSON |

**Published payload format** (NCD standard — confirm field names against
live output from your unit):

```json
{
  "nodeId": "ncd-1ec4",
  "firmware": "1.0.x",
  "ct1_amps": 12.34,
  "timestamp": 1234567890
}
```

> **Note:** NCD does not publicly document the exact JSON field names for this
> model. Capture a live payload using `mosquitto_sub` (see below) to confirm
> field names before writing any downstream parsing logic.

**Capture a live payload:**

```bash
mosquitto_sub -h broker.hivemq.com -p 1883 \
  -t "jchriswaters_merrick_ac_current" -v
```

---

## Power wiring

Power the unit from the 12V DIN-rail supply inside the enclosure via a
dedicated 2-conductor run through a cable gland. The device draws well
under 1A at 12V. The CT clamp attaches directly to the monitored conductor
— no break in the AC wiring.

---

## Integration with the Snowflake pipeline

The NCD sensor publishes **independently** to HiveMQ — the bridge_daemon
does not poll it. To capture its readings in Snowflake:

1. Add its topic to `config.env` in the `mqtt-to-snowflake` repo:
   ```
   MQTT_TOPICS=jchriswaters_merrick_ac,jchriswaters_merrick_ac_current
   ```
2. Redeploy the service (topics are baked into the systemd unit):
   ```bash
   bash deploy.sh --generate
   bash deploy.sh --install
   sudo systemctl restart mqtt_snowflake
   ```
3. Verify subscription in logs:
   ```bash
   sudo journalctl -u mqtt_snowflake -f | grep -i subscrib
   ```

Records will appear in `DBTL.MQTT.MQTT_DATA_RAW` with
`MESSAGE_TYPE = 'jchriswaters_merrick_ac_current'`.

---

## Comparison with SDM120 Modbus meters

| Feature | NCD WiFi current monitor | Eastron SDM120-Modbus |
|---------|--------------------------|----------------------|
| Wiring to controller | None (WiFi) | RS485 bus |
| Measurements | RMS current (A) | V, A, W, VA, PF, Hz, kWh |
| Non-invasive install | Yes (CT clamp) | Yes (CT clamp variant) |
| Configuration | Web browser | Front-panel buttons |
| Data path | MQTT → HiveMQ → Snowflake | bridge_daemon poll → MQTT |
| IP rating | IP65 | IP20 (DIN-rail, indoor) |
| Power supply | 6–32V DC (self-contained) | 80–270VAC (from monitored circuit) |

Use the SDM120 where energy (kWh) accumulation and full power factor data
are needed. Use the NCD WiFi sensor where a simple current reading is
sufficient and running RS485 wiring is impractical.
