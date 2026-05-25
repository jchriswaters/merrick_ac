"""
Read the generator script and arduino-reset.sh to understand proper MCU reset.
"""
import paramiko

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

# Read the generator script
print("\n=== /lib/systemd/system-generators/systemd-arduino-router.sh ===")
out, _ = ssh_run(client, "cat /lib/systemd/system-generators/systemd-arduino-router.sh 2>&1")
print(out)

# Read the arduino reset script
print("\n=== /opt/openocd/bin/arduino-reset.sh ===")
out, _ = ssh_run(client, "cat /opt/openocd/bin/arduino-reset.sh 2>&1")
print(out)

# Read the arduino debug script for context
print("\n=== /opt/openocd/bin/arduino-debug.sh ===")
out, _ = ssh_run(client, "cat /opt/openocd/bin/arduino-debug.sh 2>&1")
print(out)

# Check what other scripts are in /opt/openocd/bin/
print("\n=== /opt/openocd/bin/ contents ===")
out, _ = ssh_run(client, "ls -la /opt/openocd/bin/ 2>&1")
print(out)

# Check /opt/arduino/scripts
print("\n=== /opt/arduino/scripts/ ===")
out, _ = ssh_run(client, "ls -la /opt/arduino/scripts/ 2>&1 && cat /opt/arduino/scripts/* 2>&1")
print(out)

# Also check gpioinfo for ALL lines to understand the mapping
print("\n=== Full gpioinfo chip 1 (first 80 lines) ===")
out, _ = ssh_run(client, "gpioinfo /dev/gpiochip1 2>&1 | head -80")
print(out)

# Check gpio chip 1 lines 37, 38, 70 with labels
print("\n=== GPIO lines 37, 38, 70 details ===")
out, _ = ssh_run(client, "gpioinfo /dev/gpiochip1 2>&1 | grep -E '^\\s+(37|38|70|71):'")
print(out)

# Try connecting to monitor port after fresh reconnect
print("\n=== Monitor port test (direct ssh exec) ===")
out, _ = ssh_run(client, """python3 -c "
import socket, time
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    s.connect(('127.0.0.1', 7500))
    s.settimeout(3.0)
    data = b''
    for _ in range(10):
        try:
            chunk = s.recv(256)
            if not chunk: break
            data += chunk
        except: break
    print(f'bytes={len(data)} hex={data.hex()} repr={repr(data)}')
except Exception as e:
    print(f'err={e}')
finally:
    s.close()
" 2>&1""", timeout=10)
print(out)

client.close()
print("\nDone.")
