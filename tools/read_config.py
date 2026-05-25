"""
Read the source config and OpenOCD config to understand GPIO wiring.
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

# Read the source config
print("\n=== /var/lib/arduino-router/config/10-imola.conf ===")
out, _ = ssh_run(client, "echo piragua827 | sudo -S cat /var/lib/arduino-router/config/10-imola.conf 2>&1")
print(out)

# List all config files
print("\n=== Config directory ===")
out, _ = ssh_run(client, "echo piragua827 | sudo -S ls -la /var/lib/arduino-router/config/ 2>&1")
print(out)

# Read openocd config
print("\n=== OpenOCD gpiod config ===")
out, _ = ssh_run(client, "find /opt/openocd -name 'openocd_gpiod.cfg' 2>/dev/null | xargs cat 2>&1")
print(out)

# List openocd directory
print("\n=== /opt/openocd/ contents ===")
out, _ = ssh_run(client, "ls -la /opt/openocd/ 2>&1; ls -la /opt/openocd/share/ 2>&1 | head -20")
print(out)

# Check if there's a gpio config
print("\n=== Find GPIO config in openocd ===")
out, _ = ssh_run(client, "find /opt/openocd -name '*.cfg' 2>/dev/null | head -20; find /opt/openocd -name '*.cfg' | xargs grep -l 'gpio\\|gpiod\\|reset\\|srst' 2>/dev/null | head -5")
print(out)

# Try gpioinfo with proper syntax
print("\n=== gpioinfo (correct syntax) ===")
out, _ = ssh_run(client, "gpioinfo 2>&1 | head -100")
print(out)

# Also check what processes are currently holding GPIO lines
print("\n=== lsof for gpio devices ===")
out, _ = ssh_run(client, "echo piragua827 | sudo -S lsof /dev/gpiochip* 2>/dev/null | head -20")
print(out)

# Check /sys/kernel/debug for GPIO
print("\n=== debugfs GPIO ===")
out, _ = ssh_run(client, "echo piragua827 | sudo -S mount -t debugfs none /sys/kernel/debug 2>/dev/null; echo piragua827 | sudo -S cat /sys/kernel/debug/gpio 2>/dev/null | grep -E '(70|37|38|71|stm|mcu|reset|nrst)' | head -20 || echo 'debugfs not available'")
print(out)

client.close()
print("\nDone.")
