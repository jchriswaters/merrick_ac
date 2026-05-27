"""Test arduino-router-cli RPC calls to the MCU."""
import paramiko, sys

PASS = 'piragua827'
client = paramiko.SSHClient()
client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
client.connect('192.168.1.195', username='arduino', password=PASS, timeout=15)

def ru(cmd):
    sin, sout, serr = client.exec_command(cmd)
    o = sout.read().decode('utf-8', 'replace').strip()
    e = serr.read().decode('utf-8', 'replace').strip()
    rc = sout.channel.recv_exit_status()
    sys.stdout.buffer.write(f'CMD: {cmd}\n  OUT: {repr(o)}\n  ERR: {repr(e)}\n  RC:  {rc}\n'.encode('utf-8', 'replace'))
    return rc, o, e

print('=== Test RPC calls ===')
ru('arduino-router-cli get_outputs')
ru('arduino-router-cli get_inputs')
ru('arduino-router-cli set_flags true false true false false')
ru('arduino-router-cli set_config true 12 false 30')

client.close()
