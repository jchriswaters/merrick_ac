"""
Deploy and run GPIO 70 hold test on the board.
"""
import paramiko, time

HOST = "192.168.1.195"
USER = "arduino"
PASS = "piragua827"

# Script to run on board - holds GPIO 70=1 for 30 sec then reads router log
board_script = r"""#!/bin/bash
set -e

echo "=== Starting GPIO 70 hold test ==="
echo "Current GPIO 70: $(gpioget -c /dev/gpiochip1 70 2>&1)"

# Hold GPIO 70=1 for 30 seconds in background
# Use timeout so it definitely terminates
echo "piragua827" | sudo -S timeout 30 gpioset -c /dev/gpiochip1 70=1 &
GPIOSET_PID=$!
echo "gpioset PID: $GPIOSET_PID"

sleep 1
echo "GPIO 70 while held: $(gpioget -c /dev/gpiochip1 70 2>&1)"

echo ""
echo "Waiting 20 seconds for MCU to respond..."
sleep 20

echo ""
echo "=== Router log (last 40 lines) ==="
journalctl -u arduino-router -n 40 --no-pager

echo ""
echo "=== RPC test ==="
python3 - << 'PYEOF'
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
PYEOF

echo ""
echo "GPIO 70 state at end: $(gpioget -c /dev/gpiochip1 70 2>&1)"

wait $GPIOSET_PID 2>/dev/null || true
echo "GPIO 70 after release: $(gpioget -c /dev/gpiochip1 70 2>&1)"
echo "=== Test complete ==="
"""

def ssh_run(client, cmd, timeout=60):
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode(errors='replace')
    err = stderr.read().decode(errors='replace')
    return out, err

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, username=USER, password=PASS, timeout=10)
print("Connected.")

# Upload the script
sftp = client.open_sftp()
with sftp.open("/tmp/gpio_test.sh", "w") as f:
    f.write(board_script)
sftp.close()

# Make executable and run
ssh_run(client, "chmod +x /tmp/gpio_test.sh")

# First restart the router to get a clean state, then run the test
print("Restarting router and starting GPIO test...")
out, _ = ssh_run(client, "echo piragua827 | sudo -S systemctl restart arduino-router 2>&1; sleep 2")
print("Router restarted:", out.strip())

# Run the test script
print("\nRunning GPIO hold test (35 sec)...")
out, err = ssh_run(client, "bash /tmp/gpio_test.sh 2>&1", timeout=60)
print(out)
if err.strip():
    print("STDERR:", err[:500])

client.close()
print("\nDone.")
