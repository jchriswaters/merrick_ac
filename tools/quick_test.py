"""
Quick test: check if methods are registered after current router boot,
check if any data arrived on the serial fd, and look at router's open fds.
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

# Get router PID
out, _ = ssh_run(client, "pgrep -x arduino-router")
router_pid = out.strip()
print(f"Router PID: {router_pid}")

# Check open fds
print("\n=== Router open file descriptors ===")
out, _ = ssh_run(client, f"echo piragua827 | sudo -S ls -la /proc/{router_pid}/fd 2>&1")
print(out)

# Check IO stats for the router (bytes read from serial)
print("\n=== Router IO stats ===")
out, _ = ssh_run(client, f"echo piragua827 | sudo -S cat /proc/{router_pid}/io 2>&1")
print(out)

# RPC test
test_script = """
import socket, msgpack, time

SOCK = "/var/run/arduino-router.sock"

def rpc_call(method, args=[], msgid=1):
    req = msgpack.packb([0, msgid, method, args], use_bin_type=True)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(SOCK)
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

for m in ["get_outputs", "get_inputs", "$/version"]:
    try:
        r = rpc_call(m)
        print(f"{m}: {r}")
    except Exception as e:
        print(f"{m}: ERROR {e}")
"""

sftp = client.open_sftp()
with sftp.open("/tmp/quick.py", "w") as f:
    f.write(test_script)
sftp.close()

print("\n=== RPC test ===")
out, _ = ssh_run(client, "python3 /tmp/quick.py 2>&1")
print(out)

# Wait and test again (maybe MCU is still booting)
print("\nWaiting 10 more seconds...")
time.sleep(10)

print("\n=== RPC test (after additional wait) ===")
out, _ = ssh_run(client, "python3 /tmp/quick.py 2>&1")
print(out)

# Check how long the router has been up
print("\n=== Router uptime ===")
out, _ = ssh_run(client, f"ps -o pid,etime,comm -p {router_pid} 2>&1")
print(out)

# Check router log for anything new
print("\n=== Router log (all since last restart) ===")
out, _ = ssh_run(client, "journalctl -u arduino-router --since '2 minutes ago' --no-pager -n 50 2>/dev/null")
print(out)

client.close()
print("\nDone.")
