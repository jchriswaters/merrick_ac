# Deployment — Uno Q HVAC Controller

This doc covers how to actually get the firmware + Linux services running on the
Arduino Uno Q. It assumes the hardware is built per `docs/system-design.md` and
`docs/component-list.md`.

If you only want to update the sketch after a code change, jump to §2.

---

## 1. First-time provisioning

### 1.1 Board prerequisites

- Arduino Uno Q running its factory Debian image on the QRB2210 side
- Network connectivity (WiFi or Ethernet) so you can `ssh arduino@<board-ip>`
  — the default user is `arduino`
- `arduino-router.service` is the on-board Linux daemon that bridges Unix-domain
  RPC clients to the STM32U585 MCU over `/dev/ttyHS1`. It ships pre-installed
  on the Uno Q's Debian image.

### 1.2 Power and wiring

Wire the enclosure per `docs/system-design.md` §3.5 (power distribution) and §4
(sensor bus). Specifically:

- 12 V DIN-rail PSU feeds the Uno Q VIN, the powered USB hub (via 12 V→5 V buck),
  and the two SHT30 sensors via the in-enclosure power terminal block.
- USB hub plugs into the Uno Q USB-C port (USB-A → USB-C adapter or USB-C-to-USB-A
  cable, depending on hub upstream connector).
- FTDI USB-RS485 dongle plugs into one of the hub's downstream USB-A ports.
- RS485 dongle A+/B-/GND terminals wire to the in-enclosure RS485 distribution
  terminal block.
- All four sensors (2 × SDM120, 2 × SHT30) wire in parallel to the RS485 terminal
  block. SHT30s additionally take +12 V / GND from the power terminal block.

### 1.3 Why a powered USB hub is required (background)

When the Uno Q is powered via VIN, the Type-C port stays in **power-sink** role.
The CC pin/PD state machine is governed by an on-board ANX730x chip at I²C
address 0x58 and **cannot be overridden from Linux** — writes to
`/sys/class/typec/port0/power_role` are rejected with `Permission denied` even
as root, and the PMIC's `usb_vbus` regulator stays in the `disabled` state with
`num_users = 0`.

Symptom of plugging a USB-RS485 dongle directly into the Uno Q: nothing appears
in `lsusb`, no `/dev/ttyUSB*` device is created, and `dmesg` shows
`usb_vbus: disabling`.

A powered USB hub solves this because the hub supplies VBUS upstream (to the
Uno Q port, which happily *sinks* the 5 V) and downstream (to whatever's
plugged into the hub's USB-A ports). The Uno Q sees a self-powered USB device
on a port that now has VBUS, enumerates it normally, and `/dev/ttyUSB0` appears.

**Second-order issue:** the same VBUS-from-hub trick that enables enumeration
also tricks the Uno Q's Type-C controller into thinking it's plugged into a
host PC (the "I'm receiving power, I must be a peripheral" inference). The
**data role** auto-switches to `device`, the USB host controller never comes
up, and even though VBUS is present nothing enumerates downstream. The cure is
to force the role back to `host` in software:

```bash
# Try the /sys/class symlink first (works if xhci is still registered):
sudo sh -c 'echo host > /sys/class/usb_role/4e00000.usb-role-switch/role'

# If that returns "No such file or directory" (xhci deregistered), use the
# absolute device node — this path survives xhci teardown:
sudo sh -c 'echo host > /sys/devices/platform/soc@0/4ef8800.usb/4e00000.usb/usb_role/4e00000.usb-role-switch/role'
```

That write **does** succeed (unlike writes to `power_role`). After the role
switch, `xhci-hcd` initialises the host controller and the hub + downstream
devices enumerate normally within a few seconds.

Because the role detection can re-run on every boot **and** mid-operation (a
VBUS glitch from the hub is enough to cause the PD controller to re-negotiate
and flip to device), this fix needs to be applied persistently. The repo ships
two systemd units that do it automatically:

```
linux/uno-q-usb-host-role.service   — oneshot at boot
linux/usb-host-role-monitor.service — persistent 10-second poll
```

Install both with the rest of the units in §1.7 below.

### 1.4 Clone the repo on the board

```bash
ssh arduino@<board-ip>
cd ~
git clone https://github.com/jchriswaters/merrick_ac.git hvac-controller
```

The repo's `linux/` and `tools/` directories will live under
`/home/arduino/hvac-controller/`.

### 1.5 Install Python dependencies

```bash
sudo apt update
sudo apt install -y python3-pip python3-paramiko python3-paho-mqtt
pip3 install --user "pymodbus==3.6.*" msgpack
```

`paramiko` is only needed on a host that drives `tools/flash_sketch.py` — not
on the Uno Q itself. The bridge daemon only needs `paho-mqtt`, `pymodbus`, and
`msgpack`.

### 1.6 Apply the GPIO timing fix to `arduino-router`

The factory `arduino-router.service` configuration races the MCU's `$/reset`
handshake against its own startup: it asserts SRST in `ExecStopPost` and then
immediately releases it, so the MCU boots before the router is listening on
`/dev/ttyHS1`. The MCU's `Bridge.begin()` sends `$/reset` into the void, never
gets a response, and hangs forever in the wait loop. Methods never register.

Fix from any host that can SSH to the board:

```bash
# from your workstation, with the repo checked out:
python tools/fix_service.py
```

This rewrites `/var/lib/arduino-router/config/10-imola.conf` to assert SRST in
`ExecStartPre` (and *not* release it) and release it only via `--after-ready`,
ensuring the router is fully open on `/dev/ttyHS1` before the MCU boots.

### 1.7 Install and enable the systemd services

```bash
# on the board
sudo cp ~/hvac-controller/linux/hvac-bridge.service /etc/systemd/system/
sudo cp ~/hvac-controller/linux/uno-q-usb-host-role.service /etc/systemd/system/
sudo cp ~/hvac-controller/linux/usb-host-role-monitor.service /etc/systemd/system/
sudo cp ~/hvac-controller/desktop-hmi/hvac-hmi.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable uno-q-usb-host-role.service
sudo systemctl enable usb-host-role-monitor.service
sudo systemctl enable hvac-bridge.service
sudo systemctl enable hvac-hmi.service
sudo systemctl start uno-q-usb-host-role.service   # apply role fix now
sudo systemctl start usb-host-role-monitor.service
# leave hvac-bridge and hvac-hmi stopped for now — still need to flash the MCU
```

- `uno-q-usb-host-role.service` — on every boot, forces the Type-C controller's
  data role back to `host` so the USB hub + FTDI dongle enumerate (see §1.3).
  Tries the `/sys/class/usb_role/` symlink first, falls back to the absolute
  device node path (the class symlink disappears when xhci deregisters mid-run).
- `usb-host-role-monitor.service` — persistent service that polls the USB-C
  data role every 10 s and re-asserts `host` if it ever flips to `device` during
  normal operation (e.g. a VBUS glitch from the powered hub causes the PD
  controller to re-negotiate). Logs recoveries via `logger(1)` to journald.
  Required in addition to the oneshot above.
- `hvac-bridge.service` — the Python bridge daemon. Runs as user `arduino` from
  `/home/arduino/hvac-controller/linux/`. Uses `Type=notify` + `WatchdogSec=60`
  so systemd kills + restarts it if the main polling loop ever hangs.
- `hvac-hmi.service` — the web-based HMI server. Runs as user `arduino` from
  `/home/arduino/hvac-controller/desktop-hmi/`. Serves on port 8000 — see §5
  for how to access it.

### 1.7a Optional — MCU auto-recovery via sudoers

The bridge daemon tracks how long since the last successful MCU RPC read.
If it exceeds `mcu_hang_threshold_s` (default 60 s), the bridge sets
`mcu_healthy: false` in the MQTT status payload (the desktop HMI shows
this as an amber connection LED).

If you also enable `mcu_auto_recover` in the System Settings, the bridge
will additionally attempt to restart `arduino-router.service` when the
threshold is exceeded — its `ExecStartPre` cycles SRST (GPIO 38) which
resets the STM32, and the `--after-ready` hook releases it once the
router is back listening on `/dev/ttyHS1`.

That needs a one-time sudoers entry so the `arduino` user can run the
restart without a password:

```bash
sudo visudo -f /etc/sudoers.d/hvac-bridge
```

Add:

```
arduino ALL=(root) NOPASSWD: /bin/systemctl restart arduino-router.service
```

Save and exit. Without this line, `mcu_auto_recover` falls back to just
logging the failure (detection / reporting still works).

### 1.8 Flash the MCU sketch — see §2

---

## 2. Flashing the MCU sketch

**Important:** The Uno Q's STM32U585 runs Zephyr OS, and your sketch is loaded
as a Zephyr LLEXT (loadable extension), **not** as a standalone firmware. The
Zephyr core (always at flash 0x08000000) parses the sketch ELF at flash
0x08100000 on every boot — but only if a magic boot marker is set in TAMP_CR1.

### 2.1 ⚠️ Do not use `/opt/openocd/bin/arduino-flash.sh` on the board

The on-board flash script is **broken** for sketch upload on the Uno Q:

- It writes the raw `.bin-zsk.bin` to address **0x80F0000** (wrong — the LLEXT
  slot is at 0x8100000).
- It uses the `.bin-zsk.bin` file (which starts with a 16-byte custom Arduino
  header, not the ELF magic `\x7fELF`), so the Zephyr loader rejects it.
- It never writes the `0xCAFFEEEE` marker to TAMP_CR1 (`0x40036400`) that tells
  Zephyr to load the sketch on next boot.

If you have used this script, your symptoms are: the firmware appears to flash
successfully, all services come up, but every RPC call to the MCU returns
`'method get_outputs not available'`. The MCU is sitting in Zephyr's idle
thread because the sketch LLEXT never loaded.

### 2.2 Use `tools/flash_sketch.py` instead

From any host with the repo checked out and Python + `paramiko` installed:

```bash
# 1. Compile the sketch (or use Arduino IDE — output goes into AppData/sketches
#    on Windows or ~/.arduino/sketches on Linux/Mac):
arduino-cli compile -b arduino:zephyr:unoq mcu/hvac_controller \
    --build-path tools/build

# 2. Flash to the board over SSH+SWD:
python tools/flash_sketch.py tools/build/hvac_controller.ino.elf-zsk.bin
```

The script:

1. Pushes the `.elf-zsk.bin` to `/tmp/` on the board via SFTP
2. Stops `arduino-router.service` (so it doesn't hold `/dev/ttyHS1` during flash)
3. Runs OpenOCD over the on-board SWD adapter (GPIO chip 1 lines 25/26/38) with
   commands equivalent to `flash_sketch.cfg` from the Arduino package variant:
   - `flash write_image erase /tmp/<bin> 0x8100000 bin`
   - `mww 0x40036400 0xCAFFEEEE`
4. Restarts `arduino-router.service` (which performs the final MCU reset via
   SRST and the magic marker now causes Zephyr to load the freshly-flashed sketch)
5. Verifies via msgpack-RPC that `get_outputs`, `get_inputs`, `set_flags`, and
   `set_config` are all registered

If the verification step reports `'method ... not available'`, the flash didn't
take or the LLEXT image was rejected. Re-check the file format (it must be
`.elf-zsk.bin`, not `.bin-zsk.bin`).

### 2.3 Required Arduino_RouterBridge library modification

The stock `Arduino_RouterBridge/src/bridge.h` from the Arduino library manager
contains an ASCII flush in `BridgeClass::begin()` (under
`#if defined(ARDUINO_UNO_Q)`) that writes a literal ASCII string to the bridge
serial port before the first RPC. On the Uno Q the Go-based router parses each
ASCII byte as a malformed MsgPack `int8` and closes the session before the
`$/reset` handshake completes.

Locate the library on your build machine — typically:

- Windows: `C:\Users\<you>\Documents\Arduino\libraries\Arduino_RouterBridge\src\bridge.h`
  (or under OneDrive if `Documents` is redirected)
- macOS: `~/Documents/Arduino/libraries/Arduino_RouterBridge/src/bridge.h`
- Linux: `~/Arduino/libraries/Arduino_RouterBridge/src/bridge.h`

Remove (or comment out) the `serial_ptr->write("MCU starting RPC Bridge communication");`
line inside `BridgeClass::begin()`. The buffer is already clean on a fresh
MCU reset triggered by the router's `ExecStartPre` SRST pulse, so the flush is
unnecessary — and on the Uno Q it actively breaks the handshake.

### 2.4 Optional: 5-second timeout in `RpcCall::result()` (defensive)

The stock `result()` template loops forever waiting for an RPC response. If the
router is restarted (or any other transient comms issue happens) while the MCU
is mid-call, the MCU thread blocks indefinitely. Adding a 5-second timeout makes
the system self-recovering at the cost of an `RPC timeout` error code on
genuinely stuck calls. Recommended for production deployments. The patch is
sketched in `tools/flash_sketch.py`'s docstring; apply by editing
`Arduino_RouterBridge/src/bridge.h` to track elapsed time via `k_uptime_get()`
inside the receive `while(true)` loop.

This change is *optional* — the system works without it in normal steady-state
operation.

---

## 3. Verifying the deployment

After flashing, SSH to the board and run:

```bash
# Are both services up?
systemctl is-active arduino-router hvac-bridge        # expect: active, active

# Did the FTDI adapter enumerate via the powered hub?
ls /dev/ttyUSB*                                       # expect: /dev/ttyUSB0
dmesg | tail -20                                      # look for FTDI/CH340 attach

# Are the MCU RPC methods registered?
python3 -c "
import socket, msgpack
def rpc(m, args=[]):
    req = msgpack.packb([0,1,m,args], use_bin_type=True)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect('/var/run/arduino-router.sock'); s.settimeout(3); s.sendall(req)
    data = b''
    while True:
        chunk = s.recv(4096)
        if not chunk: break
        data += chunk
        try: print(m, msgpack.unpackb(data, raw=False)); break
        except: continue
    s.close()
for m in ['get_outputs','get_inputs','set_flags','set_config']:
    rpc(m)
"
# expect: get_outputs / get_inputs return strings like '000000000'
#         set_flags / set_config return 'Missing call parameters' (registered, just need args)
```

If `get_outputs` returns `'000011101'`-style binary strings, the MCU sketch is
running and registered with the router. If it returns
`'method get_outputs not available'`, see §2 — the LLEXT didn't load.

Watch the bridge daemon log to confirm it's polling sensors:

```bash
journalctl -u hvac-bridge -n 30 --no-pager -f
```

Expected good lines:

```
INFO   Arduino RouterBridge available — MCU RPC enabled
INFO   RS485 Modbus connected on /dev/ttyUSB0 @ 9600 baud
INFO   MQTT connected to <broker>:1883
INFO   Entering polling loop (interval=10 s)
INFO   Status → mode=...    indoor=72 °F   outdoor=85 °F   hum=54 %
```

If you see `Outdoor temp unavailable — defaulting heatPumpOk=True`, the bridge
is up but RS485 polling is failing. Common causes:

| Symptom | Likely cause |
|---|---|
| `/dev/ttyUSB0` missing | USB hub not powered, or upstream cable not plugged in |
| `/dev/ttyUSB0` present but Modbus timeouts | Bus wiring (A+/B- swapped), wrong baud, sensor not powered, missing termination |
| All sensors fail | Adapter dead, missing GND reference, or RS485 wires reversed |
| One sensor fails | DIP-switch address mismatch, or that sensor's V+ wire loose |

---

## 5. Accessing the web HMI

The HVAC controller hosts a browser-based dashboard (`hvac-hmi.service`) on
port 8000. No software to install — open any browser on the local WiFi network.

### 5.1 Normal access

| URL | `http://192.168.1.197:8000` |
|---|---|
| Works from | Any phone, tablet, or laptop on the same WiFi / LAN |
| Login required | No |
| Refresh | Live — WebSocket updates every 10 s |

Just open the URL and the dashboard loads. If you bookmark it on your phone's
home screen it works like a native app.

### 5.2 What the HMI shows

- **Heat Pump card** — current operating mode (off / fan / low_cool / high_cool /
  dehum / heat), compressor state, indoor and outdoor temperature and humidity
- **Zone cards** — live thermostat input states for main, downstairs, and theater
  zones; zone enable/disable toggles
- **Input simulation** — force any thermostat input ON / OFF / AUTO for testing
  without physically calling from the thermostat
- **System Settings panel** — all 12 configurable parameters (temperature
  thresholds, humidity limits, vent schedule, zone enables, MCU watchdog settings)
- **Connection indicator** — green (controller reachable, MCU healthy), amber
  (controller reachable but MCU unresponsive), red (controller unreachable)

The HMI is touch-optimised for the kiosk display:
- All action buttons are 44–48 px tall (meets touch target guidelines)
- Tapping any numeric setting field opens a **large numeric keypad modal** —
  no system keyboard required.  The keypad shows the setting name, valid range,
  and a backspace key.  OK is disabled if the entered value is out of range.
- Boolean settings use an oversized toggle switch (58×32 px).

### 5.3 Troubleshooting the HMI

**"Cannot connect" / page not loading:**

```bash
# On the controller — is the service running?
systemctl status hvac-hmi --no-pager

# If stopped:
sudo systemctl start hvac-hmi

# Watch startup logs:
journalctl -u hvac-hmi -n 30 --no-pager
```

**HMI loads but shows no sensor data / all readings are `?`:**

The HMI reads `/tmp/hvac_status.json`, written by `hvac-bridge.service` every
10 s.  Check that the bridge is running and sensors are reading:

```bash
systemctl status hvac-bridge --no-pager
journalctl -u hvac-bridge -n 10 --no-pager
cat /tmp/hvac_status.json
```

**Installing the HMI service for the first time:**

```bash
# On the board — install Python dependencies for the HMI server
pip3 install --break-system-packages fastapi "uvicorn[standard]" msgpack websockets

# Deploy the service unit
sudo cp ~/hvac-controller/desktop-hmi/hvac-hmi.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hvac-hmi.service

# Verify it's up
curl -s http://localhost:8000/ | head -5
```

**`config.json` on the board** (not committed — board-specific):

```json
{
  "local_mode": true,
  "controller_host": "192.168.1.197",
  "router_socket": "/var/run/arduino-router.sock",
  "mqtt_cmd_topic": "home/hvac/cmd",
  "poll_interval_s": 1.0
}
```

`local_mode: true` makes the HMI talk directly to the arduino-router Unix socket
for RPC and read `/tmp/hvac_status.json` for sensor data — no SSH needed.
If this file is missing on the board, create it at
`/home/arduino/hvac-controller/desktop-hmi/config.json`.

---

## 6. Kiosk display on an attached monitor

The controller can drive a monitor connected via USB-C → HDMI on the USB hub,
showing the HMI full-screen with no keyboard or browser chrome.  A USB mouse
plugged into the hub works for navigation.

### 6.1 Hardware requirements

- USB hub with an HDMI output port (e.g. the Battony 3-in-1 USB-C hub or
  equivalent multiport adapter with HDMI + USB-A ports)
- HDMI cable + monitor
- Optionally a USB mouse (no keyboard needed)

The Uno Q's USB-C port outputs DisplayPort Alt Mode, which the hub converts to
HDMI.  The same USB-C connection also carries USB host data for the RS485
dongle, so both video and sensor data run through the single USB-C port.

### 6.2 Software install (first time only)

```bash
# SSH to the controller
ssh arduino@192.168.1.197

# Install Xorg modesetting driver and openbox window manager
# (fbdev is pre-installed; openbox is required for --kiosk fullscreen to work —
# without a WM, Chromium has no one to honor the fullscreen request)
sudo apt-get install -y xserver-xorg-video-modesetting openbox

# Install the kiosk launcher script
sudo cp ~/hvac-controller/linux/hvac-kiosk /usr/local/bin/hvac-kiosk
sudo chmod +x /usr/local/bin/hvac-kiosk

# Register it as an X session type
sudo cp ~/hvac-controller/linux/hvac-kiosk.desktop /usr/share/xsessions/hvac-kiosk.desktop

# Configure lightdm to auto-login arduino into the kiosk session
sudo mkdir -p /etc/lightdm/lightdm.conf.d
sudo cp ~/hvac-controller/linux/99-hvac-kiosk.conf /etc/lightdm/lightdm.conf.d/99-hvac-kiosk.conf

# Restart the display manager — the monitor should show the HMI within ~10 s
sudo systemctl restart lightdm
```

### 6.3 Self-healing behaviour

The kiosk script (`linux/hvac-kiosk`) is designed to recover from every
failure mode without manual intervention:

| Failure | Recovery |
|---|---|
| Power outage / reboot | lightdm auto-logins on boot; script polls port 8000 before launching Chromium |
| Chromium crash / OOM | `while` loop restarts it after 3 s |
| hvac-hmi service crash | Watchdog detects port 8000 going away, waits for recovery, kills and relaunches Chromium |
| Hub unplug / replug (DP signal lost) | Watchdog detects DRM connector transition, runs `xrandr --output DP-1 --off/--auto`, kills Chromium to relaunch at new resolution |
| Monitor swap (different resolution) | Same as hub replug — xrandr picks up new EDID, Chromium relaunches at native resolution |
| lightdm crash | systemd restarts it (`Restart=always`) |

### 6.4 Verify

```bash
# Chromium running fullscreen (expect 1920x1080+0+0 or similar)
DISPLAY=:0 XAUTHORITY=/home/arduino/.Xauthority xwininfo -root -tree | grep -i chromium

# Openbox and watchdog processes
pgrep -a openbox
pgrep -af hvac-kiosk

# Any startup errors (mostly benign DBus/UPower noise)
tail -20 ~/.xsession-errors
```

### 6.5 Troubleshooting

**Monitor shows nothing:**
- Run `cat /sys/class/drm/card0-DP-1/status` — must say `connected`.
  If `disconnected`, the hub or cable isn't seated.
- Run `ls /dev/fb0` — must exist.  If missing, reseat the USB-C hub cable.
- The DP watchdog runs every 5 s and will auto-recover once the connector
  status transitions back to `connected`.

**HMI only fills part of the screen:**
- Chromium window is not fullscreen — likely `openbox` is not installed or
  not running.  Install it (`sudo apt-get install -y openbox`) and restart
  lightdm.
- Verify with `xwininfo` (see §6.4) — the window geometry should match the
  screen resolution exactly (e.g. `1920x1080+0+0`).

**"This site can't be reached" in Chromium:**
- The kiosk script polls port 8000 before launching Chromium, so this should
  not appear at boot.  If it does, check `systemctl is-active hvac-hmi`.
- The script uses `http://127.0.0.1:8000` explicitly — `localhost` resolves
  to IPv6 `::1` first but the HMI only binds IPv4.

**Display goes black / screen saver:**
- `xset s off; xset -dpms` in the kiosk script disables blanking.
  The DPMS watchdog also re-wakes the display if it goes to sleep while
  physically connected.

**Session reverts to login greeter:**
- Verify `/etc/lightdm/lightdm.conf.d/99-hvac-kiosk.conf` exists and
  contains `autologin-user=arduino`.

**Updating the kiosk script after a repo change:**

```bash
sudo cp ~/hvac-controller/linux/hvac-kiosk /usr/local/bin/hvac-kiosk
sudo systemctl restart lightdm
```

---

## 4. Updating the sketch later

After the initial provisioning, an iterative update is just:

```bash
# 1. Edit the sketch in mcu/hvac_controller/
# 2. Recompile
arduino-cli compile -b arduino:zephyr:unoq mcu/hvac_controller \
    --build-path tools/build

# 3. Reflash
python tools/flash_sketch.py tools/build/hvac_controller.ino.elf-zsk.bin

# 4. Confirm
ssh arduino@<board-ip> "systemctl is-active hvac-bridge && \
    python3 -c \"import socket,msgpack; \
    req=msgpack.packb([0,1,'get_outputs',[]], use_bin_type=True); \
    s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); \
    s.connect('/var/run/arduino-router.sock'); s.settimeout(3); \
    s.sendall(req); print(msgpack.unpackb(s.recv(4096), raw=False))\""
```

The flash script automatically stops/restarts `arduino-router.service`, so the
bridge daemon will briefly lose its MCU connection during flash (~15 s of
`MCU Bridge read returned no data` warnings) and then recover automatically.

---

## 5. References

- `tools/flash_sketch.py` — corrected MCU flash workflow (this is the canonical
  way to flash the Uno Q sketch; the on-board `arduino-flash.sh` is broken)
- `tools/fix_service.py` — applies the `arduino-router` GPIO timing fix
- `linux/hvac-bridge.service` — systemd unit for the bridge daemon
- `docs/system-design.md` — full system architecture, pin map, sensor bus
- `docs/component-list.md` — bill of materials
- `docs/control-logic.md` — HVAC control rules
- `docs/mqtt-payload-spec.md` — MQTT topic schema
