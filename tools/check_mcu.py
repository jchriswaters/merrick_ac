"""Check MCU bridge connection status and restart router if needed."""
import paramiko, sys, time

PASS = 'piragua827'
PROJ = '/home/arduino/hvac-controller/linux'

client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('192.168.1.195', username='arduino', password=PASS, timeout=15)

def ru(cmd):
    sin, sout, serr = client.exec_command(cmd)
    o = sout.read().decode('utf-8', 'replace').strip()
    e = serr.read().decode('utf-8', 'replace').strip()
    rc = sout.channel.recv_exit_status()
    if o: sys.stdout.buffer.write((o + '\n').encode('utf-8', 'replace'))
    if e: sys.stdout.buffer.write(('ERR: ' + e + '\n').encode('utf-8', 'replace'))
    return rc

def rsudo(cmd):
    full = f'echo {PASS} | sudo -S bash -c \'{cmd}\''
    sin, sout, serr = client.exec_command(full)
    o = sout.read().decode('utf-8', 'replace').strip()
    e = serr.read().decode('utf-8', 'replace').strip()
    ec = '\n'.join(l for l in e.splitlines() if '[sudo]' not in l and 'password for' not in l)
    if o: sys.stdout.buffer.write((o + '\n').encode('utf-8', 'replace'))
    if ec: sys.stdout.buffer.write(('ERR: ' + ec + '\n').encode('utf-8', 'replace'))

print('=== Arduino router status ===')
ru('systemctl is-active arduino-router')

print('\n=== Router logs (last 20) ===')
sin, sout, serr = client.exec_command(
    f'echo {PASS} | sudo -S journalctl -u arduino-router -n 20 --no-pager 2>/dev/null'
)
logs = sout.read().decode('utf-8', 'replace')
sys.stdout.buffer.write(logs.encode('utf-8', 'replace'))

print('\n=== Restarting arduino-router ===')
rsudo('systemctl restart arduino-router')
time.sleep(3)
ru('systemctl is-active arduino-router')

print('\n=== Router logs after restart ===')
sin, sout, serr = client.exec_command(
    f'echo {PASS} | sudo -S journalctl -u arduino-router -n 15 --no-pager 2>/dev/null'
)
logs = sout.read().decode('utf-8', 'replace')
sys.stdout.buffer.write(logs.encode('utf-8', 'replace'))

print('\n=== Testing RPC after router restart ===')
test = f'PYTHONPATH={PROJ} python3 -c "from arduino.app_utils import Bridge; r=Bridge.call(\'get_outputs\'); print(repr(r))"'
ru(test)

print('\n=== Restarting hvac-bridge ===')
rsudo('systemctl restart hvac-bridge')
time.sleep(4)

print('\n=== Final hvac-bridge logs ===')
sin, sout, serr = client.exec_command(
    f'echo {PASS} | sudo -S journalctl -u hvac-bridge -n 20 --no-pager 2>/dev/null'
)
logs = sout.read().decode('utf-8', 'replace')
sys.stdout.buffer.write(logs.encode('utf-8', 'replace'))

client.close()
