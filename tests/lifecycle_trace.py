"""Linux test-only ptrace observer: actual wait4 receipts, no code injection.

Attach only to the supervisor identified by a blocked fixture worker while the
harness is its ancestor. Never enumerate unrelated processes or signal groups.
"""
import ctypes
import os
import signal
import struct
import threading
import time

LIBC = ctypes.CDLL(None, use_errno=True)
LIBC.ptrace.restype = ctypes.c_long


def ptrace(request, pid, address=0, data=0):
    result = LIBC.ptrace(ctypes.c_uint(request), ctypes.c_uint(pid),
                         ctypes.c_void_p(address), ctypes.c_void_p(data))
    if result == -1:
        raise OSError(ctypes.get_errno(), 'ptrace')
    return result


class WaitTrace:
    def __init__(self, pid):
        self.pid = pid
        self.events = []
        self.error = None
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()
        if not self.ready.wait(3):
            raise RuntimeError('ptrace startup timeout')
        if self.error:
            raise self.error

    def run(self):
        attached = False
        try:
            ptrace(0x4206, self.pid, 0, 1)  # SEIZE, TRACESYSGOOD
            attached = True
            ptrace(0x4207, self.pid)  # INTERRUPT
            number = None
            while True:
                done, status = os.waitpid(self.pid, os.WNOHANG | 0x40000000)
                if not done:
                    time.sleep(.0005)
                    continue
                if os.WIFEXITED(status) or os.WIFSIGNALED(status):
                    self.events.append(dict(kind='supervisor-exit', status=status, at=time.monotonic()))
                    attached = False
                    break
                sig = os.WSTOPSIG(status)
                if sig == signal.SIGTRAP | 0x80:
                    info = ctypes.create_string_buffer(128)
                    ptrace(0x420e, self.pid, 128, ctypes.addressof(info))
                    op = info.raw[0]
                    if op == 1:
                        number = struct.unpack_from('Q', info.raw, 24)[0]
                        if number == 62:  # kill; negative target means killpg
                            target, sent = struct.unpack_from('qQ', info.raw, 32)
                            target = ctypes.c_int(target).value  # pid_t is 32-bit
                            self.events.append(dict(kind='kill', target=target, signal=sent, at=time.monotonic()))
                    elif op == 2 and number == 61:  # x86_64 wait4 (waitpid)
                        returned = struct.unpack_from('q', info.raw, 24)[0]
                        if returned:
                            self.events.append(dict(kind='wait4', result=returned, at=time.monotonic()))
                deliver = 0 if sig in (signal.SIGTRAP, signal.SIGTRAP | 0x80) else sig
                ptrace(24, self.pid, 0, deliver)  # SYSCALL; never alter syscall
                self.ready.set()
        except BaseException as error:
            self.error = error
            self.ready.set()
        finally:
            if attached:
                # Resume a tracee on observer error; never leave it stopped.
                try:
                    ptrace(17, self.pid)  # DETACH, no injected signal
                except OSError:
                    pass

    def finish(self):
        self.thread.join(12)
        assert not self.thread.is_alive(), 'supervisor did not finish cleanup'
        if self.error:
            raise self.error
        return self.events
