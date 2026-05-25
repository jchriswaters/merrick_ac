"""Check arduino-router version and protocol details on the board."""
import paramiko, sys

PASS = 'piragua827'
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('192.168.1.195', username='arduino', password=PASS, timeout=15)

def ru(cmd):
    sin, sout, serr = client.exec_command(cmd)
    o = sout.read().decode('utf-8', 'replace').strip()
    e = serr.read().decode('utf-8', 'replace').strip()
    if o: sys.stdout.buffer.write((o + '\n').encode('utf-8', 'replace'))
    if e: sys.stdout.buffer.write(('ERR: ' + e + '\n').encode('utf-8', 'replace'))

print('=== arduino-router version ===')
ru('dpkg -l arduino-router arduino-app-cli')
ru('arduino-router --version 2>/dev/null || arduino-router -v 2>/dev/null || echo no --version flag')

print('=== router protocol / config files ===')
ru('cat /var/lib/arduino-router/config/10-imola.conf')
ru('ls /var/lib/arduino-router/')

print('=== arduino-cli on board (zephyr platform version) ===')
ru('arduino-cli core list 2>/dev/null | grep zephyr')

print('=== RouterBridge library version on board ===')
ru('find /home/arduino/.arduino15 -name "library.properties" -path "*RouterBridge*" 2>/dev/null | xargs cat 2>/dev/null')

print('=== ttyHS1 characteristics ===')
ru('stty -F /dev/ttyHS1 2>/dev/null || echo no stty access')

client.close()
