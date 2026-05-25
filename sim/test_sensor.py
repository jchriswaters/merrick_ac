"""
test_sensor.py  —  RS485 Modbus RTU sensor sanity check
========================================================
Usage
-----
    # Scan addresses 0x01-0x0A and test every device found:
    python sim/test_sensor.py COM3

    # Test one specific address (decimal or hex):
    python sim/test_sensor.py COM3 2
    python sim/test_sensor.py COM3 0x02

    # Linux:
    python sim/test_sensor.py /dev/ttyUSB0

Mirrors the exact read logic used by bridge_daemon.py, so a passing
result here confirms sensor wiring and bus settings are correct.
"""

import sys
import logging

# Silence pymodbus's own retry/timeout log noise during scans
logging.getLogger("pymodbus").setLevel(logging.CRITICAL)

# ── dependency check ──────────────────────────────────────────
try:
    from pymodbus.client import ModbusSerialClient
    from pymodbus.exceptions import ModbusIOException
except ImportError:
    print("\nERROR: pymodbus is not installed.  Run:  pip install pymodbus pyserial\n")
    sys.exit(1)

# ── usage ─────────────────────────────────────────────────────
if len(sys.argv) < 2:
    print()
    print("Usage:  python test_sensor.py <port> [address]")
    print("  Test one address:   python test_sensor.py COM3 0x01")
    print("  Scan & test all:    python test_sensor.py COM3")
    print()
    print("Find your port:")
    print("  Windows — Device Manager > Ports (COM & LPT)")
    print("  Linux   — ls /dev/ttyUSB*  or  ls /dev/ttyACM*")
    print()
    sys.exit(1)

PORT        = sys.argv[1]
TARGET_ADDR = None
if len(sys.argv) >= 3:
    try:
        TARGET_ADDR = int(sys.argv[2], 0)   # accepts 2, 0x02, etc.
    except ValueError:
        print(f"ERROR: '{sys.argv[2]}' is not a valid address.")
        sys.exit(1)

# ── Modbus settings ───────────────────────────────────────────
BAUD     = 9600
PARITY   = 'N'
STOPBITS = 1
BYTESIZE = 8
TIMEOUT  = 1

# ── friendly names for our project's addresses ────────────────
ADDR_LABELS = {
    0x01: "Indoor SHT30",
    0x02: "Outdoor SHT30",
    0x03: "SDM120 AC circuit",
    0x04: "SDM120 Dehumidifier",
}

# ── helpers ───────────────────────────────────────────────────

def to_signed(v):
    return v if v < 0x8000 else v - 0x10000


def _mb_read(client, address, count, dev_id):
    """Version-agnostic read_holding_registers (pymodbus 2.x / 3.x / 3.13+)."""
    for kw in ("device_id", "slave", "unit"):
        try:
            return client.read_holding_registers(
                address=address, count=count, **{kw: dev_id}
            )
        except TypeError:
            continue
    return client.read_holding_registers(address, count, dev_id)


def scan_bus(client, start=1, end=10):
    """Return list of addresses that responded."""
    found = []
    print(f"\nScanning addresses 0x{start:02X} – 0x{end:02X} ...")
    for addr in range(start, end + 1):
        print(f"  \r  Trying 0x{addr:02X} …", end="", flush=True)
        try:
            r = _mb_read(client, 0, 1, addr)
            if r and not r.isError():
                label = ADDR_LABELS.get(addr, "unknown device")
                print(f"\r  0x{addr:02X}  found  ({label})              ")
                found.append(addr)
        except (ModbusIOException, Exception):
            pass
    print(f"\r  Scan complete — {len(found)} device(s) found.          ")
    return found


def read_sht30(client, address):
    """
    Read temp + humidity from one SHT30 RS485 module.
    Register map (confirmed against hardware):
      [0] temperature x100, signed int16   (0.01 deg C resolution)
      [1] humidity    x100, unsigned int16  (0.01 %RH resolution)
      [2] status byte — informational only
    """
    label = ADDR_LABELS.get(address, f"device 0x{address:02X}")
    print(f"\n--- {label}  (address 0x{address:02X}) ---")
    try:
        result = _mb_read(client, 0, 3, address)
    except ModbusIOException:
        result = None

    if result is None or (hasattr(result, 'isError') and result.isError()):
        print("  ERROR: no response from sensor")
        print("  Checks:")
        print("    1. Swap A+/B- wires on the adapter (most common cause)")
        print("    2. Verify 12 V supply is connected and on")
        print("    3. Confirm baud rate is 9600")
        print("    4. Confirm sensor address with set_sensor_address.py")
        return False

    raw = result.registers
    temp_c = to_signed(raw[0]) / 100.0
    temp_f = temp_c * 9 / 5 + 32
    hum    = raw[1] / 100.0

    print(f"  Temperature : {temp_c:.2f} C  /  {temp_f:.2f} F")
    print(f"  Humidity    : {hum:.2f} %RH")
    print(f"  Raw regs    : {raw}")
    if len(raw) > 2:
        print(f"  Status reg  : 0x{raw[2]:04X}")

    if not (-40 <= temp_c <= 125):
        print("  WARNING: temperature out of SHT30 range — check wiring")
    if not (0 <= hum <= 100):
        print("  WARNING: humidity out of range — check wiring")
    return True


# ── connect ───────────────────────────────────────────────────

print(f"\nConnecting to {PORT} at {BAUD} 8{PARITY}{STOPBITS} ...")
client = ModbusSerialClient(
    port=PORT, baudrate=BAUD, parity=PARITY,
    stopbits=STOPBITS, bytesize=BYTESIZE, timeout=TIMEOUT,
)
if not client.connect():
    print(f"ERROR: could not open {PORT}")
    print("  Check the port name and that no other program has it open.")
    sys.exit(1)
print("Connected.")

# ── test ──────────────────────────────────────────────────────

if TARGET_ADDR is not None:
    # User specified a single address
    read_sht30(client, TARGET_ADDR)
else:
    # Scan and test everything found
    addresses = scan_bus(client, start=1, end=10)
    if not addresses:
        print("\nNo devices found.")
        print("  - Check power and wiring")
        print("  - Try swapping A+/B- wires")
    else:
        for addr in addresses:
            read_sht30(client, addr)

client.close()
print("\nDone.\n")
