"""
Diagnose MCU Bridge registration by:
1. Restarting arduino-router (triggers MCU GPIO reset)
2. Watching router logs for $/reset and $/register events
3. Checking if methods get registered
"""
import paramiko, time, sys

HOST = "192.168.1.195"
USER = "arduino"
PASS = "piragua827"

def ssh_run(client, cmd, timeout=15):
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode(errors='replace')
    err = stderr.read().decode(errors='replace')
    return out, err

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect(HOST, username=USER, password=PASS, timeout=10)
print("Connected.")

# Step 1: capture current router log (last 50 lines)
print("\n=== CURRENT ROUTER LOG (last 50 lines) ===")
out, _ = ssh_run(client, "journalctl -u arduino-router -n 50 --no-pager 2>/dev/null || sudo journalctl -u arduino-router -n 50 --no-pager")
print(out)

# Step 2: stop hvac-bridge so it doesn't interfere
print("\n=== Stopping hvac-bridge service ===")
out, err = ssh_run(client, "echo piragua827 | sudo -S systemctl stop hvac-bridge 2>&1")
print(out, err)
time.sleep(1)

# Step 3: restart arduino-router
print("\n=== Restarting arduino-router ===")
out, err = ssh_run(client, "echo piragua827 | sudo -S systemctl restart arduino-router 2>&1")
print(out, err)

# Step 4: wait for MCU to boot and register
print("\nWaiting 15 seconds for MCU to boot and register methods...")
time.sleep(15)

# Step 5: capture fresh router log
print("\n=== ROUTER LOG AFTER RESTART (last 80 lines) ===")
out, _ = ssh_run(client, "journalctl -u arduino-router -n 80 --no-pager 2>/dev/null || sudo journalctl -u arduino-router -n 80 --no-pager")
print(out)

# Step 6: check router status
print("\n=== ROUTER STATUS ===")
out, _ = ssh_run(client, "echo piragua827 | sudo -S systemctl status arduino-router --no-pager 2>&1")
print(out)

# Step 7: try to call get_outputs via the router socket directly
print("\n=== TRYING DIRECT RPC CALL TO ROUTER SOCKET ===")
test_script = """
import socket, msgpack, time

SOCK = "/var/run/arduino-router.sock"

def rpc_call(method, args=[]):
    req = msgpack.packb([0, 1, method, args], use_bin_type=True)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(SOCK)
        s.settimeout(5.0)
        s.sendall(req)
        data = b""
        while True:
            try:
                chunk = s.recv(4096)
                if not chunk:
                    break
                data += chunk
                try:
                    resp = msgpack.unpackb(data, raw=False)
                    return resp
                except Exception:
                    continue
            except socket.timeout:
                break
        return data
    finally:
        s.close()

# Try calling get_outputs
print("Calling get_outputs...")
result = rpc_call("get_outputs")
print("Result:", result)

print("Calling get_inputs...")
result = rpc_call("get_inputs")
print("Result:", result)
"""

# Write script to board and run it
sftp = client.open_sftp()
with sftp.open("/tmp/test_rpc.py", "w") as f:
    f.write(test_script)
sftp.close()

out, err = ssh_run(client, "python3 /tmp/test_rpc.py 2>&1", timeout=20)
print(out)
if err:
    print("STDERR:", err)

# Step 8: check /dev/ttyHS1 to see if MCU is outputting anything
print("\n=== CHECKING /dev/ttyHS1 (2 sec capture) ===")
out, err = ssh_run(client, "timeout 2 cat /dev/ttyHS1 2>&1 | cat -v | head -50 || echo '(no output or access denied)'")
print(out)

client.close()
print("\nDone.")
