"""
After firmware re-flash, check router logs and test RPC calls.
"""
import paramiko, time, sys

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

# Step 1: restart arduino-router so it issues a fresh MCU GPIO reset
print("\n=== Restarting arduino-router ===")
out, err = ssh_run(client, "echo piragua827 | sudo -S systemctl restart arduino-router 2>&1")
print(out.strip(), err.strip())

print("Waiting 12 seconds for MCU to boot and register methods...")
time.sleep(12)

# Step 2: capture router log
print("\n=== ROUTER LOG (last 60 lines after restart) ===")
out, _ = ssh_run(client, "journalctl -u arduino-router -n 60 --no-pager 2>/dev/null")
print(out)

# Step 3: try RPC calls
test_script = r"""
import socket, msgpack

SOCK = "/var/run/arduino-router.sock"

def rpc_call(method, args=[], msgid=1):
    req = msgpack.packb([0, msgid, method, args], use_bin_type=True)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(SOCK)
        s.settimeout(6.0)
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
        return {"timeout": True, "raw": data.hex()}
    finally:
        s.close()

for m in ["get_outputs", "get_inputs"]:
    print(f"Calling {m}...")
    try:
        result = rpc_call(m)
        print(f"  Response: {result}")
    except Exception as e:
        print(f"  Error: {e}")
"""

sftp = client.open_sftp()
with sftp.open("/tmp/check_rpc.py", "w") as f:
    f.write(test_script)
sftp.close()

print("\n=== RPC CALL TEST ===")
out, err = ssh_run(client, "python3 /tmp/check_rpc.py 2>&1", timeout=30)
print(out)
if err.strip():
    print("STDERR:", err)

# Step 4: check for any 'invalid packet' errors this time
print("\n=== ERRORS IN THIS BOOT? (grep for int8 errors) ===")
out, _ = ssh_run(client, "journalctl -u arduino-router --since '1 minute ago' --no-pager 2>/dev/null | grep -i 'invalid\\|error\\|register\\|reset' | head -30")
print(out if out.strip() else "(none found)")

client.close()
print("\nDone.")
