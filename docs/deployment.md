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
sudo sh -c 'echo host > /sys/class/usb_role/4e00000.usb-role-switch/role'
```

That write **does** succeed (unlike writes to `power_role`). After the role
switch, `xhci-hcd` initialises the host controller and the hub + downstream
devices enumerate normally.

Because the role detection re-runs on every boot, this fix has to be applied
on every boot. The repo ships a systemd unit that does it automatically:

```
linux/uno-q-usb-host-role.service
```

Install it with the rest of the units in §1.7 below.

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
sudo systemctl daemon-reload
sudo systemctl enable uno-q-usb-host-role.service
sudo systemctl enable hvac-bridge.service
sudo systemctl start uno-q-usb-host-role.service   # apply role fix now
# leave hvac-bridge stopped for now — we still need to flash the MCU
```

- `uno-q-usb-host-role.service` — on every boot, forces the Type-C controller's
  data role back to `host` so the USB hub + FTDI dongle enumerate (see §1.3).
  Without this, `/dev/ttyUSB0` will not appear after reboot.
- `hvac-bridge.service` — the Python bridge daemon. Runs as user `arduino` from
  `/home/arduino/hvac-controller/linux/`.

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
