"""
Fix arduino-router service config:
- Problem: ExecStopPost resets MCU (38=0 then 38=1), so MCU boots while router is
  stopped/restarting. MCU sends $/reset to a dead serial port → lost → MCU hangs forever.
- Fix:
  1. ExecStartPre: assert SRST (38=0) to hold MCU in reset during router startup
  2. ExecStopPost: assert SRST (38=0) but DON'T release it (remove the 38=1 line)
  3. after-ready: release SRST (38=1) AFTER router is fully ready, then set 70=1
     → MCU boots fresh with router already listening
"""
import paramiko, time

HOST = "192.168.1.195"
USER = "arduino"
PASS = "piragua827"

def ssh_run(client, cmd, timeout=20):
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode(errors='replace')
    err = stderr.read().decode(errors='replace')
    return out, err

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, username=USER, password=PASS, timeout=10)
print("Connected.")

# Read current config
print("\n=== Current config ===")
out, _ = ssh_run(client, "echo piragua827 | sudo -S cat /var/lib/arduino-router/config/10-imola.conf 2>&1")
print(out)

# New config content:
# - ExecStartPre: assert SRST (38=0) BEFORE router starts (keeps MCU in reset)
# - ExecStartPre: set BOOT0=0 (37=0) for normal flash boot
# - ExecStart: after-ready now releases SRST (38=1) then signals ready (70=1)
# - ExecStopPost: assert SRST (38=0) to hold MCU in reset, signal 70=0
#   NOTE: NO 38=1 release in StopPost — MCU stays in reset until next after-ready
new_config = """[Service]
# Hold MCU in reset during router startup (SRST=GPIO38, active-low=reset when 0)
ExecStartPre=-/usr/bin/gpioset -c /dev/gpiochip1 -t0 38=0
# Set BOOT0=0 so MCU boots from main flash (not bootloader)
ExecStartPre=-/usr/bin/gpioset -c /dev/gpiochip1 -t0 37=0
ExecStart=
ExecStart=/usr/bin/arduino-router --unix-port /var/run/arduino-router.sock --serial-port /dev/ttyHS1 --serial-baudrate 115200 --after-ready '/bin/sh -c "gpioset -c /dev/gpiochip1 -t0 38=1 && gpioset -c /dev/gpiochip1 -t0 70=1"'
# On stop: assert SRST to hold MCU in reset (router will release it via after-ready on restart)
ExecStopPost=/usr/bin/gpioset -c /dev/gpiochip1 -t0 38=0
ExecStopPost=/usr/bin/gpioset -c /dev/gpiochip1 -t0 70=0
"""

# Backup original
print("\n=== Backing up original config ===")
out, _ = ssh_run(client, "echo piragua827 | sudo -S cp /var/lib/arduino-router/config/10-imola.conf /var/lib/arduino-router/config/10-imola.conf.bak 2>&1")
print(out.strip())

# Write new config
print("\n=== Writing new config ===")
sftp = client.open_sftp()
with sftp.open("/tmp/10-imola-new.conf", "w") as f:
    f.write(new_config)
sftp.close()

out, _ = ssh_run(client, "echo piragua827 | sudo -S cp /tmp/10-imola-new.conf /var/lib/arduino-router/config/10-imola.conf 2>&1")
print(out.strip())

# Verify
print("\n=== New config content ===")
out, _ = ssh_run(client, "echo piragua827 | sudo -S cat /var/lib/arduino-router/config/10-imola.conf 2>&1")
print(out)

# Force systemd to regenerate and restart
print("\n=== Regenerating systemd and restarting arduino-router ===")
out, _ = ssh_run(client, "echo piragua827 | sudo -S systemctl daemon-reload 2>&1")
print("daemon-reload:", out.strip())

# Kill any remaining gpioset processes
out, _ = ssh_run(client, "echo piragua827 | sudo -S pkill -f 'gpioset' 2>&1")
print("pkill gpioset:", out.strip())
time.sleep(0.5)

# Verify the generated drop-in now has the new content
print("\n=== Verifying generated drop-in ===")
out, _ = ssh_run(client, "cat /run/systemd/generator/arduino-router.service.d/10-imola.conf 2>&1")
print(out)

# Restart router with new config
print("\n=== Restarting arduino-router with fixed config ===")
out, _ = ssh_run(client, "echo piragua827 | sudo -S systemctl restart arduino-router 2>&1")
print(out.strip())

print("\nWaiting 15 seconds for MCU to boot and register...")
time.sleep(15)

# Check results
print("\n=== Router log (since restart) ===")
out, _ = ssh_run(client, "journalctl -u arduino-router -n 60 --no-pager --since '1 minute ago' 2>/dev/null")
print(out)

# Test RPC calls
test_script = """
import socket, msgpack

SOCK = "/var/run/arduino-router.sock"

def rpc_call(method, args=[], msgid=1):
    req = msgpack.packb([0, msgid, method, args], use_bin_type=True)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(SOCK)
        s.settimeout(8.0)
        s.sendall(req)
        data = b""
        while True:
            try:
                chunk = s.recv(4096)
                if not chunk: break
                data += chunk
                try:
                    resp = msgpack.unpackb(data, raw=False)
                    return resp
                except: continue
            except socket.timeout: break
        return None
    finally: s.close()

for m in ["get_outputs", "get_inputs", "set_flags", "set_config", "$/version"]:
    try:
        r = rpc_call(m)
        print(f"{m}: {r}")
    except Exception as e:
        print(f"{m}: ERROR {e}")
"""

sftp2 = client.open_sftp()
with sftp2.open("/tmp/rpc_final.py", "w") as f:
    f.write(test_script)
sftp2.close()

print("\n=== RPC TEST (get_outputs, get_inputs, set_flags, set_config) ===")
out, _ = ssh_run(client, "python3 /tmp/rpc_final.py 2>&1", timeout=30)
print(out)

# Check GPIO state
print("\n=== GPIO state after fix ===")
out, _ = ssh_run(client, "echo piragua827 | sudo -S cat /sys/kernel/debug/gpio 2>/dev/null | grep -E '(37|38|70|71)'")
print(out)

client.close()
print("\nDone.")
