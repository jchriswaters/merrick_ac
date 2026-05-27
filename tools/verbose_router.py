"""
Restart arduino-router in verbose mode and capture what happens when MCU boots.
We override ExecStart to add -v flag temporarily.
"""
import paramiko, time

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

# Check GPIO states before touching anything
print("\n=== GPIO states (chip 1, lines 37,38,69,70,71) ===")
out, _ = ssh_run(client, "gpioget -c /dev/gpiochip1 37 2>&1; gpioget -c /dev/gpiochip1 38 2>&1; gpioget -c /dev/gpiochip1 70 2>&1; gpioget -c /dev/gpiochip1 71 2>&1")
print(out)

# Create a drop-in override for the service to add verbose
override_dir = "/etc/systemd/system/arduino-router.service.d"
override_content = """[Service]
ExecStart=
ExecStart=/usr/bin/arduino-router --unix-port /var/run/arduino-router.sock --serial-port /dev/ttyHS1 --serial-baudrate 115200 --after-ready '/usr/bin/gpioset -c /dev/gpiochip1 -t0 70=1' --verbose
"""

print("\n=== Creating verbose override ===")
cmd = f"""echo piragua827 | sudo -S bash -c '
mkdir -p {override_dir}
cat > {override_dir}/verbose.conf << '"'"'EOF'"'"'
{override_content}
EOF
systemctl daemon-reload
echo "Override created"
'"""
out, err = ssh_run(client, cmd)
print(out.strip(), err.strip())

# Restart with verbose
print("\n=== Restarting router with verbose mode ===")
out, err = ssh_run(client, "echo piragua827 | sudo -S systemctl restart arduino-router 2>&1")
print(out.strip(), err.strip())

print("Waiting 15 seconds for MCU to boot...")
time.sleep(15)

# Get verbose logs
print("\n=== VERBOSE ROUTER LOG (all since restart) ===")
out, _ = ssh_run(client, "journalctl -u arduino-router -n 200 --no-pager --since '1 minute ago' 2>/dev/null")
print(out)

# Check GPIO states after boot
print("\n=== GPIO states after MCU boot ===")
out, _ = ssh_run(client, "gpioget -c /dev/gpiochip1 70 2>&1; gpioinfo /dev/gpiochip1 2>&1 | grep -E '(37|38|70|71):'")
print(out)

# Remove the verbose override when done
print("\n=== Cleaning up verbose override ===")
out, err = ssh_run(client, f"echo piragua827 | sudo -S bash -c 'rm -f {override_dir}/verbose.conf; systemctl daemon-reload; systemctl restart arduino-router; echo done'")
print(out.strip(), err.strip())

client.close()
print("\nDone.")
