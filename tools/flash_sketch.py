"""
Flash a compiled sketch to the Arduino Uno Q over SSH+SWD.

CRITICAL: Use the .elf-zsk.bin file (the ELF), NOT the .bin-zsk.bin (raw).
The Zephyr LLEXT loader at the bootloader stage validates the ELF magic
(\\x7fELF) at flash address 0x8100000.  If you flash the raw .bin-zsk.bin
(which starts with \\x00\\x00\\x00\\x00 — a custom Arduino header, not ELF),
the loader silently rejects it and your sketch never runs.

The on-board /opt/openocd/bin/arduino-flash.sh script is BROKEN for this
board — it writes to 0x80F0000 (wrong address) and misses the magic write
to TAMP_CR1.  This script implements the correct procedure from the
Arduino package's variant flash_sketch.cfg.

Usage:
    python flash_sketch.py [path/to/sketch.elf-zsk.bin]

If no path given, uses the most recent build output in tools/build/.
"""
import paramiko, sys, os, time

HOST = "192.168.1.195"
USER = "arduino"
PASS = "piragua827"

# Default build output (adjust if your build_path differs)
DEFAULT_BIN = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "build", "hvac_controller.ino.elf-zsk.bin"
)

def ssh_run(c, cmd, timeout=120):
    s, o, e = c.exec_command(cmd, timeout=timeout)
    return o.read().decode(errors="replace"), e.read().decode(errors="replace")

def main():
    local_bin = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BIN
    if not os.path.exists(local_bin):
        print(f"ERROR: file not found: {local_bin}")
        sys.exit(1)
    if not local_bin.endswith(".elf-zsk.bin"):
        print(f"WARNING: file does not end in .elf-zsk.bin — wrong format will silently fail to boot")

    # Verify ELF magic
    with open(local_bin, "rb") as f:
        if f.read(4) != b"\x7fELF":
            print(f"ERROR: {local_bin} is not a valid ELF (missing \\x7fELF magic)")
            sys.exit(1)

    size = os.path.getsize(local_bin)
    print(f"Flashing {local_bin} ({size} bytes)")

    remote_bin = "/tmp/sketch_upload.elf-zsk.bin"
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username=USER, password=PASS, timeout=10)

    # Upload via SFTP
    print(f"Uploading to {remote_bin}...")
    sftp = c.open_sftp(); sftp.put(local_bin, remote_bin); sftp.close()
    out, _ = ssh_run(c, f"md5sum {remote_bin}")
    print(out.strip())

    # Stop router (holds /dev/ttyHS1) so flashing has clean access
    print("\nStopping arduino-router...")
    ssh_run(c, "echo piragua827 | sudo -S systemctl stop arduino-router 2>&1")
    time.sleep(1)

    # Build and upload an OpenOCD config that does the correct flash sequence:
    #   1. Connect with SRST asserted
    #   2. Erase + write the ELF as raw bytes to 0x8100000
    #   3. Reset and let it run briefly
    #   4. Write magic 0xCAFFEEEE to TAMP_CR1 (0x40036400)
    #      — this tells the Zephyr core to boot the new sketch
    #   5. Shutdown (releases SRST, MCU boots and loads the LLEXT)
    flash_cfg = f"""reset_config srst_only srst_nogate srst_push_pull connect_assert_srst
init
reset
halt
flash info 0
flash write_image erase {remote_bin} 0x8100000 bin
reset
sleep 100
mww 0x40036400 0xCAFFEEEE
shutdown
"""
    sftp = c.open_sftp()
    with sftp.open("/tmp/flash.cfg", "w") as f: f.write(flash_cfg)
    sftp.close()

    print("\nFlashing via OpenOCD over SWD...")
    out, _ = ssh_run(c,
        "cd /opt/openocd && echo piragua827 | sudo -S "
        "timeout 90 bin/openocd -d1 -s /opt/openocd "
        "-f openocd_gpiod.cfg -f /tmp/flash.cfg 2>&1",
        timeout=120)
    # Show useful output lines
    for line in out.split("\n"):
        if any(k in line for k in ["Error", "Warn", "wrote", "erased", "shutdown", "Padding"]):
            print("  " + line)

    # Restart router — its ExecStartPre asserts SRST, after-ready releases.
    # That triggers a final MCU reset, at which point Zephyr sees the magic
    # in TAMP_CR1 and loads our sketch.
    print("\nRestarting arduino-router (triggers final MCU reset)...")
    ssh_run(c, "echo piragua827 | sudo -S systemctl start arduino-router 2>&1")

    print("Waiting 12s for MCU boot + Bridge registration...")
    time.sleep(12)

    # Verify methods are registered
    test = """import socket, msgpack
def rpc(m, args=[]):
    req = msgpack.packb([0, 1, m, args], use_bin_type=True)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect("/var/run/arduino-router.sock"); s.settimeout(5)
        s.sendall(req)
        data = b""
        while True:
            try:
                chunk = s.recv(4096)
                if not chunk: break
                data += chunk
                try: return msgpack.unpackb(data, raw=False)
                except: continue
            except socket.timeout: return "TIMEOUT"
        return None
    finally: s.close()
for m in ["get_outputs", "get_inputs", "set_flags", "set_config"]:
    print(f"  {m}: {rpc(m)}")
"""
    sftp = c.open_sftp()
    with sftp.open("/tmp/rpc_verify.py", "w") as f: f.write(test)
    sftp.close()

    out, _ = ssh_run(c, "python3 /tmp/rpc_verify.py 2>&1")
    print("\nMCU RPC verification:")
    print(out)

    if "method get_outputs not available" in out:
        print("\n!!! FLASH MAY HAVE FAILED — methods not registered !!!")
        print("Check that:")
        print("  1. You used .elf-zsk.bin (NOT .bin-zsk.bin)")
        print("  2. The TAMP magic write completed")
        print("  3. The router's GPIO reset timing is correct in /var/lib/arduino-router/config/10-imola.conf")
        sys.exit(1)
    else:
        print("\n✓ Flash successful — MCU is running the new sketch.")

    c.close()

if __name__ == "__main__":
    main()
