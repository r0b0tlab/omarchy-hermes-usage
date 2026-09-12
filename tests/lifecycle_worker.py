"""Copied ONLY as the disposable fixture's sibling hermes-usage.py."""
import json
import os
from pathlib import Path
import signal
import socket
import time

os.chdir(os.environ['HERMES_HOME'])
mode = Path('mode').read_text().strip()
channel = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
channel.connect('events.sock')
signal.signal(signal.SIGTERM, signal.SIG_IGN)
signal.alarm(15)
supervisor = os.getppid()

def event(kind):
    fields = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    descriptors = {}
    for fd in os.listdir('/proc/self/fd'):
        try:
            descriptors[fd] = os.readlink('/proc/self/fd/' + fd)
        except FileNotFoundError:
            pass
    channel.send(json.dumps(dict(kind=kind, pid=os.getpid(), supervisor=supervisor,
        nnp=fields['NoNewPrivs'].strip(), fds=descriptors, sid=os.getsid(0), at=time.monotonic())).encode())

event('leader')
assert channel.recv(16) == b'go'
if mode == 'normal':
    print('fixture complete', flush=True)
    os._exit(0)
read_fd, write_fd = os.pipe()
child = os.fork()
if child == 0:
    os.close(read_fd)
    os.setsid()
    event('intermediate')
    grandchild = os.fork()
    if grandchild == 0:
        signal.alarm(15)
        event('detached')
        os.write(write_fd, b'R')
        os.close(write_fd)
        while True:
            signal.pause()
    os.close(write_fd)
    os._exit(0)
os.close(write_fd)
assert os.read(read_fd, 1) == b'R'
os.close(read_fd)
event('ready')
if mode == 'leader-first':
    os._exit(0)
while True:
    signal.pause()
