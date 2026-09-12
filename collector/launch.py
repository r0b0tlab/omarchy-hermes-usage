#!/usr/bin/python3
"""Direct-parent Linux supervisor for one bounded usage refresh."""

from __future__ import annotations

import errno
import importlib.util
import select
import selectors
import ctypes
import time
import signal
import os
import stat
import sys

DEFAULT_INTERPRETER = "/usr/bin/python3"
MAX_PATH_LENGTH = 1024
CHILD_ENV_ALLOWLIST = ("HOME", "XDG_STATE_HOME", "HERMES_HOME", "TZ")


def validate_interpreter(candidate: str) -> str | None:
    """Resolve `candidate` to a trusted interpreter path, or None.

    Trusted means: absolute, length-bounded, every resolved path component
    root-owned and not group/other writable, and the final component a
    regular file with at least one execute bit. Symlinks are resolved first,
    so a chain into a user-writable directory fails closed.
    """
    if not candidate or not candidate.startswith("/") or len(candidate) > MAX_PATH_LENGTH:
        return None
    if "\x00" in candidate:
        return None
    try:
        real = os.path.realpath(candidate)
    except OSError:
        return None
    info = None
    current = "/"
    for part in real.split("/")[1:]:
        current = os.path.join(current, part)
        try:
            info = os.lstat(current)
        except OSError:
            return None
        if stat.S_ISLNK(info.st_mode):  # realpath() already resolved these
            return None
        if info.st_uid != 0:
            return None
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return None
    if info is None or not stat.S_ISREG(info.st_mode):
        return None
    if not info.st_mode & 0o111 or info.st_mode & (stat.S_ISUID | stat.S_ISGID):
        return None
    return real


def child_environment(environ) -> dict[str, str]:
    """The exact environment the collector is allowed to see."""
    return {name: environ[name] for name in CHILD_ENV_ALLOWLIST if name in environ}


def select_interpreter(environ) -> tuple:
    """(interpreter, warning). An invalid override is refused, never run."""
    override = environ.get("HERMES_USAGE_PYTHON") or ""
    warning = None
    if override:
        validated = validate_interpreter(override)
        if validated:
            return validated, None
        warning = (
            f"hermes-usage: refused HERMES_USAGE_PYTHON={override!r} (must be an "
            "absolute root-owned, non-group/world-writable, regular executable); "
            f"using {DEFAULT_INTERPRETER}"
        )
    validated = validate_interpreter(DEFAULT_INTERPRETER)
    if not validated:
        return None, (warning + "; " if warning else "") + (
            f"hermes-usage: {DEFAULT_INTERPRETER} failed trust validation"
        )
    return validated, warning


TIMEOUT = 60.0
GRACE = 3.0
OUT_CAP = 262144
ERR_CAP = 8192


def subreaper():
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), 'cannot enable child subreaper')


def direct_children():
    # This supervisor is single-threaded. Do not generalize to another PID.
    with open('/proc/self/task/%d/children' % os.getpid(), encoding='ascii') as f:
        return [int(x) for x in f.read().split()]


def signal_owned_child(pid, sig):
    # Caller owns this direct child and has NOT reaped it. No SIGCHLD handler
    # or Popen.poll()/wait() may independently reap children in this process.
    try:
        fd = os.pidfd_open(pid)
    except OSError as error:
        if error.errno not in (errno.EMFILE, errno.ENFILE, errno.ENOMEM):
            raise
        # Exclusive, unreaped DIRECT child only: ownership reserves this PID.
        # This is not a lookup fallback for arbitrary numeric process IDs.
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass
        return
    try:
        signal.pidfd_send_signal(fd, sig)
    except ProcessLookupError:
        pass
    finally:
        os.close(fd)


def signal_reserved_group(pid, sig):
    # Valid ONLY while the group's leader is our unreaped child. ESRCH means
    # no signalable members remain; it does not excuse skipping wait/reaping.
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def require_unprivileged():
    if (os.getuid() == 0 or os.geteuid() != os.getuid()
            or os.getegid() != os.getgid() or os.geteuid() == 0):
        raise RuntimeError('supervisor requires an unprivileged equal-ID context')
    with open('/proc/self/status', encoding='ascii') as stream:
        status = dict(line.split(':', 1) for line in stream if ':' in line)
    if any(int(status[key].strip(), 16) for key in ('CapPrm', 'CapEff', 'CapAmb')):
        raise RuntimeError('supervisor must not carry capabilities')


def supervise(command, environment, timeout=TIMEOUT, grace=GRACE, lease_fd=None, lock_fd=None):
    """Standalone, single-threaded exclusive child owner; never embed in a host."""
    require_unprivileged()
    parent = os.getppid()
    if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        raise RuntimeError('Linux pidfd support is required')
    subreaper()
    cancelled = [False]
    handlers = {}
    descriptors = set()
    poller = None
    pid = None
    ready = False
    output = [bytearray(), bytearray()]
    counts = [0, 0]
    reaped = []
    reason = None
    status = None
    failure = None
    children_fd = None

    def close(fd):
        nonlocal failure
        if fd in descriptors:
            descriptors.remove(fd)
            try:
                os.close(fd)
            except BaseException as error:
                # On Linux a failed close may already release the FD; retrying
                # it could close a reused descriptor. Still attempt all others.
                failure = failure or error

    def children():
        os.lseek(children_fd, 0, os.SEEK_SET)
        data = bytearray()
        while True:
            chunk = os.read(children_fd, 4096)
            if not chunk:
                return [int(x) for x in data.split()]
            data.extend(chunk)

    def consume(key):
        nonlocal ready, reason
        try:
            chunk = os.read(key.fd, 4096)
        except BlockingIOError:
            return False
        if not chunk:
            poller.unregister(key.fd)
            close(key.fd)
        elif key.data == 2:
            ready = ready or b'R' in chunk
        else:
            index = key.data
            cap = (OUT_CAP, ERR_CAP)[index]
            counts[index] = min(cap + 1, counts[index] + len(chunk))
            output[index].extend(chunk[:max(0, cap - len(output[index]))])
            if counts[index] > cap:
                reason = reason or 'output-limit'
        return bool(chunk)

    try:
        # Reserve enumeration before fork: cleanup must work under EMFILE.
        children_fd = os.open('/proc/self/task/%d/children' % os.getpid(), os.O_RDONLY | os.O_CLOEXEC)
        descriptors.add(children_fd)
        if children():
            raise RuntimeError('supervisor must not have pre-existing children')
        handlers[signal.SIGCHLD] = signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            handlers[sig] = signal.signal(sig, lambda *_: cancelled.__setitem__(0, True))
        pipes = []
        for _ in range(3):
            pair = os.pipe()
            descriptors.update(pair)
            pipes.append(pair)
        if lease_fd is not None and select.select([lease_fd], [], [], 0)[0]:
            return dict(code=130, reason='cancelled', stdout=b'', stderr=b'', reaped=[], counts=[0, 0])
        pid = os.fork()
        if pid == 0:
            try:
                for sig in handlers:
                    signal.signal(sig, signal.SIG_DFL)
                # No privilege-gaining exec may make owned children unsignalable.
                libc = ctypes.CDLL(None, use_errno=True)
                if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
                    os._exit(126)
                os.setsid()
                os.dup2(pipes[0][1], 1)
                os.dup2(pipes[1][1], 2)
                for fd in (lease_fd, lock_fd):
                    if fd is not None:
                        os.close(fd)
                for fd in descriptors:
                    if fd not in (1, 2, pipes[2][1]):
                        os.close(fd)
                os.write(pipes[2][1], b'R')
                os.close(pipes[2][1])
                os.execve(command[0], command, environment)
            except BaseException:
                os._exit(126)
        for _, fd in pipes:
            close(fd)
        leader_fd = os.pidfd_open(pid)
        descriptors.add(leader_fd)
        poller = selectors.DefaultSelector()
        for index, (fd, _) in enumerate(pipes):
            os.set_blocking(fd, False)
            poller.register(fd, selectors.EVENT_READ, index)
        deadline = time.monotonic() + timeout
        term_at = None
        while True:
            now = time.monotonic()
            lost_lease = lease_fd is not None and select.select([lease_fd], [], [], 0)[0]
            if cancelled[0] or lost_lease or (lease_fd is None and os.getppid() != parent):
                reason = reason or 'cancelled'
            if now >= deadline:
                reason = reason or 'timeout'
            for key, _ in poller.select(0.02):
                consume(key)
            if reason is not None and term_at is None:
                if ready:
                    signal_reserved_group(pid, signal.SIGTERM)
                signal_owned_child(pid, signal.SIGTERM)
                term_at = now
            observed = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if observed is not None or (term_at is not None and now - term_at >= grace):
                break
    except BaseException as error:
        failure = error
    finally:
        if pid is not None and pid > 0:
            # This is the LAST group signal. PID is reserved by unreaped leader.
            if ready:
                try:
                    signal_reserved_group(pid, signal.SIGKILL)
                except BaseException as error:
                    failure = failure or error
            # Signal/reap in rounds. Escaped and double-forked descendants become
            # direct children as their parents die. Never reopen a reaped PID.
            pending_reported = False
            cleanup_start = time.monotonic()
            known = {pid}
            while True:
                owned = set(known)
                try:
                    owned.update(children())
                except BaseException as error:
                    failure = failure or error
                try:
                    for child in owned:
                        try:
                            signal_owned_child(child, signal.SIGKILL)
                        except BaseException as error:
                            failure = failure or error
                        try:
                            done, st = os.waitpid(child, os.WNOHANG)
                        except ChildProcessError as error:
                            failure = failure or error
                            known.discard(child)
                            continue
                        if done:
                            known.discard(done)
                            reaped.append(done)
                            if done == pid:
                                status = st
                except BaseException as error:
                    failure = failure or error
                # /proc listings can be incomplete. Always wait for any exited
                # child; only ECHILD from this global wait proves completion.
                try:
                    done, st = os.waitpid(-1, os.WNOHANG)
                    if done:
                        known.discard(done)
                        reaped.append(done)
                        if done == pid:
                            status = st
                except ChildProcessError:
                    break
                except BaseException as error:
                    failure = failure or error
                if not pending_reported and time.monotonic() - cleanup_start >= 3:
                    pending_reported = True
                    try:
                        os.write(2, b'hermes-usage: cleanup pending; remaining busy\n')
                    except OSError:
                        pass
                time.sleep(0.02)
            # Owned children are reaped, but an external process might hold a
            # transferred pipe FD. Drain available bounded bytes, never wait
            # for that external writer and never spin on EAGAIN.
            if poller is not None:
                try:
                    for key in list(poller.get_map().values()):
                        for _ in range(OUT_CAP // 4096 + 2):
                            if not consume(key):
                                break
                except BaseException as error:
                    failure = failure or error
        if poller is not None:
            try:
                poller.close()
            except BaseException as error:
                failure = failure or error
        for fd in list(descriptors):
            close(fd)
        for sig, handler in handlers.items():
            try:
                signal.signal(sig, handler)
            except BaseException as error:
                failure = failure or error
    if failure is not None:
        raise failure
    code = os.waitstatus_to_exitcode(status)
    code = {'timeout': 124, 'output-limit': 125, 'cancelled': 130}.get(reason, code)
    return dict(code=code, reason=reason, stdout=bytes(output[0]),
                stderr=bytes(output[1]), reaped=reaped, counts=counts)


def run_main(argv=None, lease_fd=None, lock_fd=None):
    args = list(sys.argv[1:] if argv is None else argv)
    interpreter, warning = select_interpreter(os.environ)
    if warning:
        # Do not echo attacker-sized override values to logs.
        print('hermes-usage: interpreter override refused; using trusted default', file=sys.stderr)
    if interpreter is None:
        return 126
    collector = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'hermes-usage.py')
    try:
        result = supervise([interpreter, '-I', collector, *args], child_environment(os.environ),
                           lease_fd=lease_fd, lock_fd=lock_fd)
    except Exception:
        print('hermes-usage: supervisor failed after cleanup', file=sys.stderr)
        return 126
    if result['stdout']:
        sys.stdout.buffer.write(result['stdout'])
    if result['stderr']:
        sys.stderr.buffer.write(result['stderr'][:2048])
    if result['reason']:
        print('\nhermes-usage: ' + result['reason'], file=sys.stderr)
    return result['code'] if result['code'] >= 0 else 128 - result['code']


def main(argv=None, lease_fd=None, lock_fd=None):
    # QML's forked supervisor already holds the lock. Direct CLI invocations
    # participate in the same exclusion rather than bypassing pending cleanup.
    if lock_fd is not None:
        return run_main(argv, lease_fd, lock_fd)
    acquired = None
    try:
        require_unprivileged()
        path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'bootstrap.py')
        spec = importlib.util.spec_from_file_location('usage_bootstrap', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        acquired = module.acquire_lock(os.environ)
        if acquired is None:
            return 0
        return run_main(argv, lease_fd, acquired)
    except Exception:
        print('hermes-usage: supervisor refused or failed after cleanup', file=sys.stderr)
        return 126
    finally:
        if acquired is not None:
            try:
                os.close(acquired)
            except OSError:
                pass


if __name__ == '__main__':
    raise SystemExit(main())
