"""
Connect to arduino-router monitor port (127.0.0.1:7500) via SSH tunnel
to see what the MCU is actually transmitting on /dev/ttyHS1.
Also check GPIO state via sysfs and look at generator script.
"""
import paramiko, socket, time, threading

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

# 1. Check the systemd generator script
print("\n=== Systemd generator script ===")
out, _ = ssh_run(client, "find /usr/lib/systemd/system-generators /lib/systemd/system-generators -type f 2>/dev/null | xargs ls -la 2>/dev/null; ls /run/systemd/generator/ 2>/dev/null")
print(out)
out, _ = ssh_run(client, "cat /run/systemd/generator/arduino-router.service.d/10-imola.conf 2>/dev/null")
print(out)

# 2. Find the generator binary/script
print("\n=== Find generator for arduino-router ===")
out, _ = ssh_run(client, "find /usr/lib /opt /usr/share -name '*arduino*' -o -name '*imola*' 2>/dev/null | grep -v '\\.bin$' | head -20")
print(out)

# 3. Check sysfs GPIO
print("\n=== GPIO sysfs ===")
out, _ = ssh_run(client, "ls /sys/class/gpio/ 2>/dev/null | head -20")
print(out)
out, _ = ssh_run(client, "cat /sys/class/gpio/gpiochip*/base 2>/dev/null; cat /sys/class/gpio/gpiochip*/ngpio 2>/dev/null")
print("chips:", out)

# 4. Try to read GPIO 70 via sysfs (find base of gpiochip1)
print("\n=== Compute GPIO 70 global number ===")
get_gpio_num = """
import os
chips = []
for d in os.listdir('/sys/class/gpio'):
    if d.startswith('gpiochip'):
        try:
            base = int(open(f'/sys/class/gpio/{d}/base').read().strip())
            ngpio = int(open(f'/sys/class/gpio/{d}/ngpio').read().strip())
            label = open(f'/sys/class/gpio/{d}/label').read().strip()
            chips.append((base, ngpio, label, d))
        except: pass
chips.sort()
for base, ngpio, label, d in chips:
    print(f"  {d}: base={base}, ngpio={ngpio}, label={label}")
    if 'gpiochip1' in d or 'gpio@' in label:
        print(f"  -> GPIO 70 global number: {base + 70}")
        print(f"  -> GPIO 37 global number: {base + 37}")
"""
sftp = client.open_sftp()
with sftp.open("/tmp/gpio_sysfs.py", "w") as f:
    f.write(get_gpio_num)
sftp.close()
out, _ = ssh_run(client, "python3 /tmp/gpio_sysfs.py 2>&1")
print(out)

# 5. Connect to monitor port to see MCU data
print("\n=== Monitor port raw data (5 sec capture) ===")
monitor_script = """
import socket, time, sys

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    s.connect(('127.0.0.1', 7500))
    s.settimeout(5.0)
    data = b""
    start = time.time()
    while time.time() - start < 5:
        try:
            chunk = s.recv(1024)
            if not chunk: break
            data += chunk
        except socket.timeout:
            break
    print(f"Received {len(data)} bytes")
    print(f"Hex: {data.hex()}")
    print(f"ASCII: {repr(data)}")
except Exception as e:
    print(f"Error: {e}")
finally:
    s.close()
"""
with sftp.open("/tmp/monitor.py", "w") as f:
    f.write(monitor_script)

out, _ = ssh_run(client, "python3 /tmp/monitor.py 2>&1", timeout=10)
print(out)

# 6. Look at the arduino-router flags for monitor
print("\n=== Monitor proxy info ===")
out, _ = ssh_run(client, "ss -tlnp | grep '7500\\|5000\\|arduino' 2>/dev/null; netstat -tlnp 2>/dev/null | grep '7500\\|arduino'")
print(out)

# 7. Check what the MCU serial monitor shows
print("\n=== MCU via mon/read RPC call ===")
mon_rpc = """
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

# Try mon/connected
print("mon/connected:", rpc_call("mon/connected"))
# Try mon/read (read from MCU monitor)
for i in range(3):
    result = rpc_call("mon/read", [100], msgid=i+2)
    print(f"mon/read attempt {i+1}: {result}")
    time.sleep(0.5)
"""
with sftp.open("/tmp/mon_rpc.py", "w") as f:
    f.write(mon_rpc)

out, _ = ssh_run(client, "python3 /tmp/mon_rpc.py 2>&1", timeout=20)
print(out)

client.close()
print("\nDone.")
