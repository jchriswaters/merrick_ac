"""
Deep diagnostics: read arduino-router service file, check /dev/ttyHS1 state,
look at what $/reset does, and whether MCU is actually communicating.
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

# 1. Read arduino-router service file
print("\n=== arduino-router.service ===")
out, _ = ssh_run(client, "cat /etc/systemd/system/arduino-router.service 2>/dev/null || cat /lib/systemd/system/arduino-router.service 2>/dev/null || find /etc /lib /usr/lib -name 'arduino-router*' 2>/dev/null")
print(out)

# 2. Check router binary location and any config
print("\n=== Router binary / config ===")
out, _ = ssh_run(client, "which arduino-router; arduino-router --help 2>&1 | head -30; ls /etc/arduino* /usr/share/arduino* /opt/arduino* 2>/dev/null | head -20")
print(out)

# 3. Try to run arduino-router with debug/verbose flags
print("\n=== Router version info ===")
out, _ = ssh_run(client, "arduino-router --version 2>&1; arduino-router version 2>&1; echo piragua827 | sudo -S arduino-router --version 2>&1")
print(out)

# 4. Check ExecStartPre for arduino-router
print("\n=== systemctl show arduino-router ===")
out, _ = ssh_run(client, "echo piragua827 | sudo -S systemctl show arduino-router --no-pager 2>&1 | grep -E 'ExecStart|Exec|reset|gpio|serial|tty'")
print(out)

# 5. Check what GPIO the MCU reset is on
print("\n=== GPIO chip info ===")
out, _ = ssh_run(client, "gpioinfo /dev/gpiochip1 2>&1 | grep -i 'reset\\|stm32\\|mcu\\|nrst\\|70\\|69\\|71' | head -20")
print(out)
out2, _ = ssh_run(client, "gpioinfo 2>&1 | grep -E '(chip|70|71|69|reset|mcu|stm)' | head -30")
print(out2)

# 6. What does the full service look like (systemctl cat)
print("\n=== systemctl cat arduino-router ===")
out, _ = ssh_run(client, "echo piragua827 | sudo -S systemctl cat arduino-router 2>&1")
print(out)

# 7. Check the FULL router log since last boot (look for any $/reset or register)
print("\n=== FULL router log since service start ===")
out, _ = ssh_run(client, "journalctl -u arduino-router --no-pager -n 200 2>/dev/null | tail -100")
print(out)

# 8. Look for any arduino-router config file
print("\n=== Router config file ===")
out, _ = ssh_run(client, "find / -name 'arduino-router*.json' -o -name 'arduino-router*.yaml' -o -name 'arduino-router*.conf' 2>/dev/null | head -5")
print(out)

client.close()
print("\nDone.")
