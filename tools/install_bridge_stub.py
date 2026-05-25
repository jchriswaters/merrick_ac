"""
Install a minimal arduino.app_utils stub on the Uno Q.
Writes the package to the project directory and sets PYTHONPATH in the service.
"""
import paramiko, sys, time

PASS = 'piragua827'
PROJ = '/home/arduino/hvac-controller/linux'

APP_UTILS_PY = r'''"""
Minimal arduino.app_utils stub for the Arduino Uno Q.
Implements Bridge.call() via MsgPack RPC over /var/run/arduino-router.sock.
"""
import socket, threading
import msgpack

ROUTER_SOCK = "/var/run/arduino-router.sock"

class _Bridge:
    _lock = threading.Lock()
    _msgid = 0

    def call(self, method, *args):
        with self._lock:
            self._msgid = (self._msgid + 1) & 0xFFFFFFFF
            msgid = self._msgid

        # MsgPack RPC REQUEST: [type=0, msgid, method, [params]]
        request = msgpack.packb([0, msgid, method, list(args)], use_bin_type=True)

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(ROUTER_SOCK)
            sock.settimeout(5.0)
            sock.sendall(request)

            # Read response bytes
            data = b""
            while True:
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    break
                if not chunk:
                    break
                data += chunk
                try:
                    # MsgPack RPC RESPONSE: [type=1, msgid, error, result]
                    resp = msgpack.unpackb(data, raw=False)
                    if isinstance(resp, list) and len(resp) == 4 and resp[0] == 1:
                        error, result = resp[2], resp[3]
                        if error is not None:
                            raise RuntimeError(f"RPC error from MCU: {error}")
                        return result
                except msgpack.UnpackValueError:
                    continue
                break
            return None
        finally:
            sock.close()


Bridge = _Bridge()
App = None  # Not used by bridge_daemon

__all__ = ['Bridge', 'App']
'''

INIT_PY = "# arduino package — MsgPack RPC stub for Uno Q\n"

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
    rc = sout.channel.recv_exit_status()
    if o: sys.stdout.buffer.write((o + '\n').encode('utf-8', 'replace'))
    if ec: sys.stdout.buffer.write(('ERR: ' + ec + '\n').encode('utf-8', 'replace'))
    return rc

# 1. Write the stub to the project directory (user-writable)
print('Writing arduino stub package to project dir...')
sftp = client.open_sftp()
sftp.mkdir(f'{PROJ}/arduino')
with sftp.open(f'{PROJ}/arduino/__init__.py', 'w') as f:
    f.write(INIT_PY)
with sftp.open(f'{PROJ}/arduino/app_utils.py', 'w') as f:
    f.write(APP_UTILS_PY)
sftp.close()
print(f'  Written to {PROJ}/arduino/')

# 2. Install msgpack on the board (needed by the stub)
print('Ensuring msgpack is installed on board...')
rsudo('pip3 install --break-system-packages msgpack')

# 3. Update the service file to add PYTHONPATH
print('Updating service PYTHONPATH...')
SERVICE_PATCH = f'''Environment=PYTHONUNBUFFERED=1
Environment=PYTHONPATH={PROJ}'''
# Read current service file, replace the Environment line
sin, sout, serr = client.exec_command('cat /etc/systemd/system/hvac-bridge.service')
svc_content = sout.read().decode('utf-8', 'replace')

if f'PYTHONPATH={PROJ}' not in svc_content:
    new_content = svc_content.replace(
        'Environment=PYTHONUNBUFFERED=1',
        SERVICE_PATCH
    )
    sftp = client.open_sftp()
    with sftp.open('/tmp/hvac-bridge-new.service', 'w') as f:
        f.write(new_content)
    sftp.close()
    rsudo('cp /tmp/hvac-bridge-new.service /etc/systemd/system/hvac-bridge.service')
    rsudo('systemctl daemon-reload')
    print('  Service updated with PYTHONPATH')
else:
    print('  PYTHONPATH already set')

# 4. Test import works
print('Testing arduino.app_utils import...')
test_cmd = f'PYTHONPATH={PROJ} python3 -c "from arduino.app_utils import Bridge; print(type(Bridge))"'
ru(test_cmd)

# 5. Test actual RPC call
print('Testing Bridge.call(get_outputs)...')
test_cmd2 = f'PYTHONPATH={PROJ} python3 -c "from arduino.app_utils import Bridge; r=Bridge.call(chr(103)+chr(101)+chr(116)+chr(95)+chr(111)+chr(117)+chr(116)+chr(112)+chr(117)+chr(116)+chr(115)); print(repr(r))"'
ru(test_cmd2)

# 6. Restart service
print('Restarting hvac-bridge...')
rsudo('systemctl restart hvac-bridge')
time.sleep(4)
ru('systemctl is-active hvac-bridge')

# 7. Show logs
print('Recent logs:')
sin, sout, serr = client.exec_command(
    f'echo {PASS} | sudo -S journalctl -u hvac-bridge -n 20 --no-pager 2>/dev/null'
)
logs = sout.read().decode('utf-8', 'replace').strip()
sys.stdout.buffer.write((logs + '\n').encode('utf-8', 'replace'))

client.close()
print('Done.')
