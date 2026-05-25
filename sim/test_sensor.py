"""
test_sensor.py  —  RS485 Modbus RTU sensor sanity check
========================================================
Run from the repo root or the sim/ folder:

    python sim/test_sensor.py COM3          # Windows (replace COM3)
    python sim/test_sensor.py /dev/ttyUSB0  # Linux

Reads the SHT30 temp/humidity sensor at Modbus address 0x01.
Mirrors the exact read logic used by bridge_daemon.py so a passing
result here confirms the sensor, wiring, and bus settings are correct.
"""

import sys

# ── dependency check ──────────────────────────────────────────
try:
    from pymodbus.client import ModbusSerialClient
except ImportError:
    print()
    print("ERROR: pymodbus is not installed.")
    print("       Run:  pip install pymodbus pyserial")
    print()
    sys.exit(1)

# ── port argument ─────────────────────────────────────────────
if len(sys.argv) < 2:
    print()
    print("Usage:  python test_sensor.py <port>")
    print("  Windows example:  python test_sensor.py COM3")
    print("  Linux example:    python test_sensor.py /dev/ttyUSB0")
    print()
    print("Find your port:")
    print("  Windows  — Device Manager > Ports (COM & LPT)")
    print("  Linux    — ls /dev/ttyUSB*  or  ls /dev/ttyACM*")
    print()
    sys.exit(1)

PORT = sys.argv[1]

# ── Modbus settings (must match sensor DIP-switch config) ─────
BAUD     = 9600
PARITY   = 'N'
STOPBITS = 1
BYTESIZE = 8
TIMEOUT  = 1      # seconds per request

# ── sensor addresses (from our design) ────────────────────────
SHT30_INDOOR  = 0x01
SHT30_OUTDOOR = 0x02

# ── helpers ───────────────────────────────────────────────────

def to_signed(v):
    """Convert unsigned 16-bit Modbus register to signed int16."""
    return v if v < 0x8000 else v - 0x10000


def _modbus_read(client, address, count, slave_id):
    """
    Call read_holding_registers with the correct keyword for the installed
    pymodbus version:
      < 3.0  : unit=
      3.0–3.12: slave=
      3.13+  : device_id=
    Falls back through each keyword so the script works across versions.
    """
    for kw in ("device_id", "slave", "unit"):
        try:
            return client.read_holding_registers(
                address=address, count=count, **{kw: slave_id}
            )
        except TypeError:
            continue
    # last resort: positional (address, count, slave)
    return client.read_holding_registers(address, count, slave_id)


def read_sht30(client, address, label):
    """
    Read 3 holding registers from an SHT30 RS485 module.
    Register map (FC03):
      [0] temperature  — signed int16, unit 0.1 °C
      [1] humidity     — signed int16, unit 0.1 %RH
      [2] status/spare
    """
    print(f"\n--- {label} (Modbus address 0x{address:02X}) ---")
    result = _modbus_read(client, 0, 3, address)

    if result.isError():
        print(f"  ERROR: {result}")
        print("  Possible causes:")
        print("    - Wrong COM port")
        print("    - A+/B- wires swapped (try swapping them)")
        print("    - Sensor not powered (check 12 V supply)")
        print("    - Wrong Modbus address (check sensor DIP switches)")
        print("    - Wrong baud rate (default is 9600; check sensor label)")
        return

    raw = result.registers
    temp_c = to_signed(raw[0]) / 10.0
    temp_f = temp_c * 9 / 5 + 32
    hum    = to_signed(raw[1]) / 10.0

    print(f"  Temperature : {temp_c:.1f} C  /  {temp_f:.1f} F")
    print(f"  Humidity    : {hum:.1f} %RH")
    print(f"  Raw regs    : {raw}")

    # basic sanity checks
    if not (-40 <= temp_c <= 125):
        print("  WARNING: temperature out of SHT30 physical range — check wiring")
    if not (0 <= hum <= 100):
        print("  WARNING: humidity out of range — check wiring")


# ── main ──────────────────────────────────────────────────────

print(f"\nConnecting to RS485 bus on {PORT} at {BAUD} 8{PARITY}{STOPBITS} ...")

client = ModbusSerialClient(
    port     = PORT,
    baudrate = BAUD,
    parity   = PARITY,
    stopbits = STOPBITS,
    bytesize = BYTESIZE,
    timeout  = TIMEOUT,
)

if not client.connect():
    print(f"ERROR: could not open {PORT}")
    print("  Check the port name and that no other program has it open.")
    sys.exit(1)

print("Connected.")

# Read indoor sensor (address 0x01) — the one you have wired right now
read_sht30(client, SHT30_INDOOR,  "Indoor SHT30  (address 0x01)")

# Uncomment when outdoor sensor is wired:
# read_sht30(client, SHT30_OUTDOOR, "Outdoor SHT30 (address 0x02)")

client.close()
print("\nDone.\n")
