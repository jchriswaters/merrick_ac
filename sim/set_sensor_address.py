"""
set_sensor_address.py  —  RS485 SHT30 address commissioning tool
=================================================================
Use this script to discover and reassign the Modbus address of an
SHT30 RS485 sensor that has no DIP switches.

IMPORTANT: connect only ONE sensor to the bus while running this
script.  If two sensors share the same address they will collide
and corrupt each other's responses.

Usage
-----
    python sim/set_sensor_address.py COM3          # Windows
    python sim/set_sensor_address.py /dev/ttyUSB0  # Linux

Workflow
--------
    1. Connect one sensor, power it on.
    2. Run this script — it scans 0x01-0x0A and finds the sensor.
    3. Choose the new address (1 = indoor, 2 = outdoor per our design).
    4. Script writes the address and verifies the sensor responds.
    5. Power-cycle the sensor, run test_sensor.py to confirm.
    6. Disconnect, connect the next sensor, repeat from step 1.

Address register locations tried (most common Chinese modules)
--------------------------------------------------------------
    0x0101  — most common (JXBS-3001 and compatible)
    0x0200  — second most common variant
    0x07D0  — some Modbus configurator variants
    0x0100  — occasionally used
If none of those work the script reports which registers it tried
and you can supply a custom register number.
"""

import sys
import time

try:
    from pymodbus.client import ModbusSerialClient
    from pymodbus.exceptions import ModbusIOException
except ImportError:
    print("\nERROR: pymodbus is not installed.  Run:  pip install pymodbus pyserial\n")
    sys.exit(1)

# ── version-agnostic Modbus helpers ──────────────────────────

def _mb_read(client, address, count, dev_id):
    for kw in ("device_id", "slave", "unit"):
        try:
            return client.read_holding_registers(
                address=address, count=count, **{kw: dev_id}
            )
        except TypeError:
            continue
    return client.read_holding_registers(address, count, dev_id)


def _mb_write(client, address, value, dev_id):
    for kw in ("device_id", "slave", "unit"):
        try:
            return client.write_register(
                address=address, value=value, **{kw: dev_id}
            )
        except TypeError:
            continue
    return client.write_register(address, value, dev_id)


# ── address register candidates (most-likely first) ──────────

ADDR_REGS = [0x0066,   # confirmed for this sensor (decoded from raw frame 01 06 00 66 ...)
             0x0101, 0x0200, 0x07D0, 0x0100]   # other common variants

# ── scan ─────────────────────────────────────────────────────

def scan_bus(client, start=1, end=247):
    """
    Scan Modbus addresses start..end.  Returns list of responding addresses.
    Uses a short timeout so the full scan completes in ~30 s.
    """
    found = []
    print(f"\nScanning addresses 0x{start:02X} – 0x{end:02X} "
          f"(this takes up to {end - start + 1} seconds) ...")

    original_timeout = client.comm_params.timeout_connect
    client.comm_params.timeout_connect = 0.3   # fast scan

    for addr in range(start, end + 1):
        print(f"  \r  Trying 0x{addr:02X} …", end="", flush=True)
        try:
            resp = _mb_read(client, 0, 1, addr)
            if resp and not resp.isError():
                found.append(addr)
                print(f"\r  Found device at 0x{addr:02X}              ")
        except (ModbusIOException, Exception):
            pass

    client.comm_params.timeout_connect = original_timeout
    print()
    return found


# ── address change ────────────────────────────────────────────

def set_address(client, current_addr, new_addr):
    """
    Try each known address-register location in order.
    Returns (success: bool, register_used: int | None).
    """
    print(f"\nAttempting to change 0x{current_addr:02X} → 0x{new_addr:02X} ...")

    for reg in ADDR_REGS:
        print(f"  Writing new address to register 0x{reg:04X} ... ", end="", flush=True)
        try:
            resp = _mb_write(client, reg, new_addr, current_addr)
            if resp and not resp.isError():
                print("write accepted.")
            else:
                print("write error (trying next register).")
                continue
        except (ModbusIOException, Exception) as exc:
            print(f"no response ({exc}) — trying next register.")
            continue

        # Give the sensor time to save to EEPROM and reinitialise
        print(f"  Waiting 2 s for sensor to reinitialise ...")
        time.sleep(2)

        # Verify: try reading from the new address
        print(f"  Verifying response at new address 0x{new_addr:02X} ... ", end="", flush=True)
        try:
            verify = _mb_read(client, 0, 2, new_addr)
            if verify and not verify.isError():
                temp_c = (verify.registers[0] if verify.registers[0] < 0x8000
                          else verify.registers[0] - 0x10000) / 100.0
                hum    = verify.registers[1] / 100.0
                print(f"SUCCESS  ({temp_c:.1f} C, {hum:.1f} %RH)")
                return True, reg
            else:
                print("no valid response — register may be wrong.")
        except (ModbusIOException, Exception):
            print("no response — register may be wrong.")

    return False, None


# ── main ─────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    port = sys.argv[1]

    print(f"\nConnecting to {port} at 9600 8N1 ...")
    client = ModbusSerialClient(
        port=port, baudrate=9600, parity="N",
        stopbits=1, bytesize=8, timeout=1,
    )
    if not client.connect():
        print(f"ERROR: could not open {port}")
        sys.exit(1)
    print("Connected.\n")

    # ── step 1: scan ─────────────────────────────────────────
    found = scan_bus(client, start=1, end=10)

    if not found:
        print("No devices found on the bus.")
        print("Possible causes:")
        print("  - Sensor not powered (check 12 V supply)")
        print("  - A+/B- wires swapped — try swapping them")
        print("  - Wrong COM port")
        client.close()
        sys.exit(1)

    print(f"Found {len(found)} device(s): "
          + ", ".join(f"0x{a:02X}" for a in found))

    if len(found) > 1:
        print("\nWARNING: more than one device found.")
        print("Disconnect all but the one you want to re-address, then re-run.")
        client.close()
        sys.exit(1)

    current_addr = found[0]

    # ── step 2: choose target address ────────────────────────
    print(f"\nDevice is currently at address 0x{current_addr:02X}.")
    print("Target addresses for this project:")
    print("  1  (0x01) = indoor  SHT30")
    print("  2  (0x02) = outdoor SHT30")
    print("  3  (0x03) = SDM120 AC circuit")
    print("  4  (0x04) = SDM120 dehumidifier circuit")

    while True:
        raw = input("\nEnter new address (1-247, or hex like 0x02): ").strip()
        try:
            new_addr = int(raw, 0)   # handles decimal and 0x hex
        except ValueError:
            print("Not a valid number — try again.")
            continue
        if not 1 <= new_addr <= 247:
            print("Address must be 1–247.")
            continue
        if new_addr == current_addr:
            print(f"Device is already at 0x{current_addr:02X} — nothing to do.")
            client.close()
            sys.exit(0)
        break

    # ── step 3: write new address ─────────────────────────────
    success, reg_used = set_address(client, current_addr, new_addr)

    if success:
        print(f"\n✓ Address changed successfully using register 0x{reg_used:04X}.")
        print(f"  Power-cycle the sensor then run:")
        print(f"    python sim\\test_sensor.py {port}")
        print(f"  to confirm it responds at address 0x{new_addr:02X}.\n")
    else:
        print(f"\nCould not change address automatically.")
        print(f"Registers tried: " + ", ".join(f"0x{r:04X}" for r in ADDR_REGS))
        print()
        print("Options:")
        print("  a) Check your sensor's datasheet for the address register number,")
        print("     then re-run and enter it when prompted (coming soon).")
        print("  b) Share the sensor model/brand and we can look up the register.")
        print("  c) Use the manufacturer's PC configuration tool if one exists.\n")

    client.close()


if __name__ == "__main__":
    main()
