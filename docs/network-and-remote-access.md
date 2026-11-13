# Network and Remote Access Design

Status: proposed, 2026-11-12. Nothing in this document is implemented yet. Section 6
lists the decisions that are still open.

This document covers two related changes to how the controller is networked:

- **Part A** moves the HVAC devices (controller, CrowPanel display, NCD current
  monitor, future room sensors) onto a small dedicated Wi-Fi network instead of the
  house Wi-Fi.
- **Part B** describes how an iOS app, or any remote client, could reach the system
  from the internet, and what needs to be fixed in the current cloud publishing
  before that is done.

---

## 1. Current state

Everything below was taken from the repo as of commit `88802d5`.

| Item | Current setup |
|------|---------------|
| Controller network | Uno Q onboard Wi-Fi, client of the house network. Address `192.168.1.197`, hostname `MerrickAC`, pinned by a DHCP reservation on the house router. |
| USB-C port | Used by a powered USB hub that carries the USB-RS485 adapter and the kiosk monitor (see `docs/deployment.md` §1.3). |
| Web HMI | `hvac-hmi.service`, `http://192.168.1.197:8000`, listening on all interfaces. No login was found in `desktop-hmi/server.py`. |
| Local MQTT | Mosquitto on the controller. Carries `home/hvac/cmd`, `home/hvac/config`, and the NCD monitor topic. The LAN listener allows anonymous clients and has no TLS. |
| NCD current monitor | `192.168.1.244`, mDNS name `ncd-1ec4.local`, joined to the house Wi-Fi, publishes to the controller's Mosquitto. |
| CrowPanel display | Not deployed yet. `hmi/crowpanel_hvac/config.h` holds placeholders for the SSID, password, and broker address. |
| Cloud MQTT | `bridge_daemon.py` publishes status to `broker.hivemq.com:1883` on topic `jchriswaters_merrick_ac`. No authentication and no TLS. This feeds the mqtt-to-snowflake pipeline on GCP. |

Wi-Fi is not part of the control path. Thermostat inputs, relay outputs, and the
RS485 sensor reads (through the USB adapter) all work with the network down. Losing
Wi-Fi affects the HMI, the display, the current monitor, and cloud publishing.

---

## 2. Part A: dedicated HVAC Wi-Fi

### 2.1 Why

- The control-related devices stop depending on the house router, so a router
  replacement, password change, or channel change at the house does not affect them.
- The anonymous local broker is no longer reachable from every device on the house
  network.
- Room sensors and wall panels, which `PROJECT-STATUS.md` lists as a future idea,
  would have a network of their own to join.

### 2.2 Constraint: the Uno Q has one Wi-Fi radio

Running the board as an access point while it is also a client of the house Wi-Fi is
possible on Debian but depends on the radio supporting both modes on the same
channel, and it is likely to be fragile. The design therefore uses a separate small
router or access point, such as a GL.iNet travel router or any inexpensive
OpenWrt-capable unit.

### 2.3 Two layouts

**Layout A: the router carries the uplink.** The router joins the house Wi-Fi as a
wireless client (repeater or WISP mode) and provides its own SSID. The controller
joins that SSID with its onboard radio, as do the display and the current monitor.
The controller then has a single network interface and no new USB devices.

**Layout B: the controller is on both networks.** The controller keeps its onboard
Wi-Fi on the house network for its internet connection. A USB Ethernet adapter on the
powered hub connects it by cable to the router, and the router has no uplink. Local
devices join the router's SSID and reach the controller through the cable.

| | Layout A | Layout B |
|---|---|---|
| New hardware | Router only | Router and USB Ethernet adapter |
| Controller interfaces | One | Two, with only the house link as the default route |
| Depends on the USB host role | No | Yes, for the local network |
| Internet path for cloud publishing | Through the router's wireless uplink | Unchanged, house Wi-Fi |
| Local devices if the house Wi-Fi is down | Keep working | Keep working |

Recommendation: start with Layout A. The repo documents that the USB-C data role can
flip to `device` while running (gotcha 13 in `PROJECT-STATUS.md`), which already
interrupts the RS485 adapter until `usb-host-role-monitor.service` restores it.
Layout B would add the local network to the devices affected by that event. Layout B
remains an option if the router's wireless uplink proves unreliable.

In either layout, a phone on the house Wi-Fi cannot reach the HMI directly, because it
is on a different subnet. The remote access options in Part B cover this, since a VPN
reaches the controller from the house as well as from outside. A phone can also join
the dedicated SSID when it is near the panel.

### 2.4 Addressing plan (example values)

| Device | Address | Notes |
|--------|---------|-------|
| Router | `192.168.50.1` | DHCP server for the dedicated network |
| Controller (`MerrickAC`) | `192.168.50.10` | DHCP reservation by MAC (`14:b5:cd:ea:d3:31` for the onboard radio) |
| NCD current monitor | `192.168.50.20` | Reservation by MAC, or static IP set in its web UI |
| CrowPanel display | `192.168.50.21` | Reservation by MAC |
| Future room sensors | `192.168.50.30` and up | |

The subnet differs from the house network on purpose, so that the two are never
confused when the router sits between them.

### 2.5 Radio settings

- 2.4 GHz only for this SSID. The ESP32-S3 in the CrowPanel supports 2.4 GHz only, and
  the NCD monitor is expected to as well.
- A fixed channel, chosen after a look at nearby networks (1, 6, or 11), so the router
  does not change it on its own.
- WPA2-PSK with a long passphrase used only for this network.
- No internet access is needed by the local devices. Only the controller needs one,
  for cloud publishing and later remote access.

### 2.6 Power

Power the router from the enclosure supply so it starts with the controller after an
outage. The enclosure already has a 12 V to 5 V converter for the USB hub; the router
needs either its own converter or confirmation that the existing one has spare
current. A small UPS for the controller and router together is worth considering, as
noted in `docs/component-list.md`.

### 2.7 Mosquitto on the new network

The local listener currently accepts anonymous clients. After the move, bind it to
the dedicated network's interface and to localhost, so the house network cannot
reach it. Whether to add a username and password is a separate choice. The CrowPanel
config already has `MQTT_USER` and `MQTT_PASS` fields. The NCD monitor's web UI lists
broker host, port, and topic as settings, but credentials are not listed in
`docs/ncd-current-monitor.md`, so that device may not support authentication.

### 2.8 Migration steps

1. Set up the router with its own subnet, SSID, fixed channel, and the DHCP
   reservations in §2.4. For Layout A, configure its wireless uplink to the house
   Wi-Fi and confirm it has internet access.
2. On the controller, add the new SSID as a saved Wi-Fi connection. Confirm it
   connects and has internet access through the router before removing the house
   connection.
3. Reconfigure the NCD monitor through its web UI (`http://ncd-1ec4.local/`): Wi-Fi
   credentials, broker host (the controller's new address), and static IP or DHCP.
4. Update `hmi/crowpanel_hvac/config.h` (`WIFI_SSID`, `WIFI_PASS`, `MQTT_HOST`) and
   flash the display.
5. Update `controller_host` in `desktop-hmi/config.json` if the desktop HMI runs in
   remote mode.
6. Bind the Mosquitto listener as described in §2.7 and restart it.
7. Check the full path: status in the HMI, NCD readings in the status payload,
   a setpoint change from the display reaching `home/hvac/cmd`, and cloud publishing
   still arriving at the broker.
8. Update the addresses in `PROJECT-STATUS.md`, `ncd-current-monitor.md`, `README.md`,
   and `deployment.md`.

---

## 3. Part B: remote access from the internet

### 3.1 Problems to resolve first

These exist today and should be dealt with before any remote command path is added.

1. **The cloud status topic is public.** Anyone who learns or guesses the topic name on
   `broker.hivemq.com` can read the status payload, which shows temperatures, power use,
   and when the system runs. They can also publish false readings into the same topic,
   which would flow into the Snowflake pipeline. The defaults at the top of
   `bridge_daemon.py` list only the enabled flag, host, port, and topic for the cloud
   connection, so username, password, and TLS settings would need to be added.
2. **The HMI has no login.** It listens on all interfaces on port 8000 and can change
   configuration. It should not be exposed to the internet directly through port
   forwarding.
3. **Credentials are in the repo.** The controller's SSH password appears in
   `docs/PROJECT-STATUS.md` and as a default in `desktop-hmi/server.py`. If that password is
   still valid, rotate it and remove it from both files. If the repository is or may
   become public, it should also be removed from the git history.

### 3.2 Options

| Option | How it works | Notes |
|--------|--------------|-------|
| Private VPN (Tailscale or WireGuard) | The controller and the phone join the same private network. The app reaches the HMI and broker by their private addresses. | Smallest change and no public exposure. Gives access from inside the house as well. The VPN app must be running on the phone. Package availability for the Uno Q's Debian image should be confirmed. |
| Authenticated cloud MQTT broker | HiveMQ Cloud, EMQX Cloud, or a small VPS with TLS on port 8883, per-client credentials, and topic permissions. The controller connects outbound. The app subscribes for status and publishes commands. | Builds on the existing cloud publishing. Requires adding credentials and TLS to `bridge_daemon.py` and updating the pipeline consumer. |
| HTTPS tunnel with access control (for example Cloudflare Tunnel with Cloudflare Access) | The controller opens an outbound tunnel. A public hostname forwards to the HMI after the user logs in. | No port forwarding. Needs a domain. The app can use the REST API or a web view. |
| Home Assistant | Home Assistant on a separate machine bridges MQTT and provides remote access and push notifications through its own iOS app. | Avoids writing an app. It makes sense only if Home Assistant will be run anyway. |

### 3.3 Recommended sequence

1. **Private VPN on the controller.** This gives remote use of the existing HMI at
   once, with no new code. It also solves the cross-subnet access problem from §2.3.
2. **Authenticated cloud broker.** Move cloud publishing to a broker with TLS and
   credentials, and update the Snowflake consumer to match. Do this regardless of
   whether an app is built. Use separate credentials for the controller, the pipeline
   consumer, and the app, with topic permissions that give each only what it needs.
3. **Native iOS app.** Use SwiftUI with an MQTT client library, or a thin wrapper
   around the HMI over the VPN. Push alerts require a relay that sends notifications
   through Apple's push service, or a service such as ntfy or Pushover.

### 3.4 Rules for remote commands

The command path from the HMI and display already goes through `home/hvac/cmd`, where
`bridge_daemon.py` validates and applies changes. Remote commands should reuse that
path and follow these rules:

- The MCU interlocks stay in charge. The three-minute compressor delay and the
  heat/cool exclusion are enforced in the sketch, and no command may bypass them.
- Remote clients are limited to an allowlist of configuration keys, kept smaller than
  what the local HMI can change. Anything that affects equipment safety limits stays
  local-only.
- Commands carry a timestamp and are rejected if stale, to prevent replay.
- Remote-origin commands are rate limited and logged with their source.
- A remote command to force the system off is allowed, but a remote command that
  turns equipment on is not added until the other rules are in place and tested.

### 3.5 iOS constraints to plan for

- iOS suspends apps in the background, so an app cannot hold an MQTT connection open
  all day. Alerts need push notifications, and live status is fetched when the app opens.
- App Transport Security requires HTTPS by default. Reaching the HMI over plain HTTP
  needs an exception, or HTTPS in front of it. Tailscale can issue certificates for
  tailnet hostnames, which would avoid the exception if Tailscale is used.
- A VPN-based design means the VPN app must be installed and connected on the phone
  in addition to this app.

---

## 4. How the two parts fit together

```
 Phone (remote)                      House Wi-Fi          HVAC Wi-Fi (dedicated)
      │                                   │                      │
      │  VPN or TLS to cloud broker       │                      ├── CrowPanel display
      ▼                                   │                      ├── NCD current monitor
 Cloud broker / VPN ◄───────────────► Router (uplink) ◄──────────┤   (future room sensors)
      ▲                                                          │
      │  outbound connection from controller                     ▼
      └────────────────────────────────────────── Controller (Uno Q)
                                                     MCU: relays, interlocks
                                                     Linux: RS485, Mosquitto, HMI
```

The controller makes only outbound connections to the internet. The local devices
reach it on the dedicated network, and remote clients reach it through either the VPN
or the authenticated broker.

---

## 5. Files affected when this is implemented

| File | Change |
|------|--------|
| `linux/bridge_daemon.py` | Cloud MQTT credentials and TLS settings; handling for remote commands |
| `hmi/crowpanel_hvac/config.h` | SSID, passphrase, broker address |
| `desktop-hmi/config.json.example` | `controller_host` |
| `desktop-hmi/server.py` | Remove the default SSH password |
| `docs/PROJECT-STATUS.md` | New addresses; remove the SSH password |
| `docs/ncd-current-monitor.md` | New broker address and network |
| `docs/deployment.md` | Network setup steps; remote access section |
| `docs/mqtt-payload-spec.md` | Cloud broker, topics, and credentials model |
| `README.md` | Addresses, if listed |

---

## 6. Open decisions

1. Layout A or Layout B (§2.3). The recommendation is A.
2. Router model.
3. VPN product, and whether the controller's Debian image has a package for it.
4. Cloud broker provider, or a self-hosted broker.
5. Whether the iOS app is a native app or a wrapper around the HMI, and whether
   remote control is needed or monitoring and alerts are enough for the first version.
6. How the Snowflake consumer will receive the new credentials.
7. Which configuration keys, if any, the app may change.
