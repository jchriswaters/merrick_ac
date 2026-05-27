"""
Test hypothesis: GPIO 70 = 0 holds MCU in reset.
Hold GPIO 70 = 1 persistently and watch if MCU starts communicating.
"""
import paramiko, time, threading

HOST = "192.168.1.195"
USER = "arduino"
PASS = "piragua827"

def ssh_run(client, cmd, timeout=30):
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode(errors='replace')
    err = stderr.read().decode(errors='replace')
    return out, err

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, username=USER, password=PASS, timeout=10)
print("Connected.")

# Check current state
print("\n=== Current GPIO 70 state ===")
out, _ = ssh_run(client, "gpioget -c /dev/gpiochip1 70 2>&1")
print("GPIO 70:", out.strip())

# Restart router to get clean state (MCU reset via GPIO 37)
print("\n=== Restarting router (fresh MCU reset) ===")
out, _ = ssh_run(client, "echo piragua827 | sudo -S systemctl restart arduino-router 2>&1")
print(out.strip())
time.sleep(2)

# Now hold GPIO 70=1 persistently (without -t0)
# This command will hold until killed; we'll run it in background with &
# But we can't do that easily via SSH... Instead use nohup + sleep
print("\n=== Holding GPIO 70=1 persistently for 20 seconds ===")
# Launch gpioset in background to hold GPIO 70=1
# gpioset without -t will hold the line until the process exits
out, _ = ssh_run(client, "echo piragua827 | sudo -S bash -c 'nohup gpioset -c /dev/gpiochip1 70=1 &' 2>&1; sleep 1; gpioget -c /dev/gpiochip1 70 2>&1")
print("GPIO hold result:", out.strip())

print("\nWaiting 15 seconds to see if MCU sends $/reset...")
time.sleep(15)

# Check the router log
print("\n=== ROUTER LOG (last 30 lines) ===")
out, _ = ssh_run(client, "journalctl -u arduino-router -n 30 --no-pager --since '1 minute ago' 2>/dev/null")
print(out)

# GPIO state now
print("\n=== GPIO 70 state during hold ===")
out, _ = ssh_run(client, "gpioget -c /dev/gpiochip1 70 2>&1")
print("GPIO 70:", out.strip())

# Kill the gpioset process
print("\n=== Killing gpioset (releasing GPIO 70) ===")
out, _ = ssh_run(client, "echo piragua827 | sudo -S pkill -f 'gpioset.*70=1' 2>&1; sleep 1; gpioget -c /dev/gpiochip1 70 2>&1")
print(out.strip())

# Also try calling the RPC methods now to see if they registered
test_script = """
import socket, msgpack

def rpc_call(method, msgid=1):
    req = msgpack.packb([0, msgid, method, []], use_bin_type=True)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect("/var/run/arduino-router.sock")
        s.settimeout(5.0)
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

for m in ["get_outputs", "get_inputs"]:
    print(f"{m}: {rpc_call(m)}")
"""

sftp = client.open_sftp()
with sftp.open("/tmp/t.py", "w") as f:
    f.write(test_script)
sftp.close()

print("\n=== RPC test during GPIO hold ===")
out, _ = ssh_run(client, "python3 /tmp/t.py 2>&1")
print(out)

client.close()
print("\nDone.")
