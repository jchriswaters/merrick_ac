"""
Use strace on the router process to see if it receives any bytes from /dev/ttyHS1.
Also try to see what the MCU's UART is doing.
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

# Kill any stale gpioset processes from previous tests
print("\n=== Killing stale gpioset processes ===")
out, _ = ssh_run(client, "echo piragua827 | sudo -S pkill -f 'gpioset' 2>&1 | head -5; sleep 1")
print(out.strip())

# Get router PID
print("\n=== Router PID ===")
out, _ = ssh_run(client, "pgrep -x arduino-router 2>&1")
print("PID:", out.strip())
router_pid = out.strip()

# Restart router to get clean state
print("\n=== Restarting router (clean state) ===")
out, _ = ssh_run(client, "echo piragua827 | sudo -S systemctl restart arduino-router 2>&1; sleep 1; pgrep -x arduino-router 2>&1")
print(out.strip())
router_pid = out.strip().split('\n')[-1].strip()
print("New PID:", router_pid)

# Now run strace for 10 seconds to capture serial reads/writes
print(f"\n=== strace on router (PID {router_pid}) for 10 seconds ===")
strace_cmd = (f"echo piragua827 | sudo -S strace -p {router_pid} "
              f"-e trace=read,write -s 256 -f -T 2>&1 | head -200")
out, _ = ssh_run(client, strace_cmd, timeout=15)
print(out)

# Check current gpio state
print("\n=== Current GPIO state ===")
out, _ = ssh_run(client, "cat /sys/kernel/debug/gpio 2>/dev/null | grep -E '(37|38|70|71)' || "
                          "echo piragua827 | sudo -S cat /sys/kernel/debug/gpio 2>/dev/null | grep -E '(37|38|70|71)'")
print(out)

# Get fd info for the router to find which fd is /dev/ttyHS1
print("\n=== Router file descriptors ===")
out, _ = ssh_run(client, f"echo piragua827 | sudo -S ls -la /proc/{router_pid}/fd 2>/dev/null | grep -E '(tty|serial|HS1)'")
print(out)

# Find /dev/ttyHS1's file descriptor number
out, _ = ssh_run(client, f"echo piragua827 | sudo -S ls -la /proc/{router_pid}/fd 2>/dev/null | head -30")
print(out)

# Try strace just for the serial fd reads
print("\n=== Router log since last restart ===")
out, _ = ssh_run(client, "journalctl -u arduino-router -n 20 --no-pager --since '1 minute ago' 2>/dev/null")
print(out)

client.close()
print("\nDone.")
