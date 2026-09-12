#!/usr/bin/python3
"""Bounded launcher for the Hermes usage collector.

Service.qml never executes anything but a fixed absolute interpreter; this
launcher runs under it, validates the interpreter override
(HERMES_USAGE_PYTHON), drops to a closed environment, puts itself in its own
session (so the service can terminate the whole process group), and
re-executes the collector with `-I`.
"""

from __future__ import annotations

import errno
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
    if not info.st_mode & 0o111:
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


def supervise(command, environment, timeout=TIMEOUT, grace=GRACE):
    """Standalone, single-threaded exclusive child owner; never embed in a host."""
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
        if fd in descriptors:
            descriptors.remove(fd)
            os.close(fd)

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
            return
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
            if cancelled[0] or os.getppid() != parent:
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
                except OSError as error:
                    failure = failure or error
            # Signal/reap in rounds. Escaped and double-forked descendants become
            # direct children as their parents die. Never reopen a reaped PID.
            pending_reported = False
            cleanup_start = time.monotonic()
            while True:
                try:
                    owned = children()
                    if not owned:
                        try:
                            done, st = os.waitpid(-1, os.WNOHANG)
                        except ChildProcessError:
                            break
                        if done:
                            reaped.append(done)
                            if done == pid:
                                status = st
                    for child in owned:
                        try:
                            signal_owned_child(child, signal.SIGKILL)
                        except OSError as error:
                            failure = failure or error
                        done, st = os.waitpid(child, os.WNOHANG)
                        if done:
                            reaped.append(done)
                            if done == pid:
                                status = st
                except OSError as error:
                    failure = failure or error
                if not pending_reported and time.monotonic() - cleanup_start >= 3:
                    pending_reported = True
                    try:
                        os.write(2, b'hermes-usage: cleanup pending; remaining busy\n')
                    except OSError:
                        pass
                time.sleep(0.02)
            # All writers are dead; drain to EOF, including final output emitted
            # just before waitid. Counts remain raw-byte/saturating.
            if poller is not None:
                try:
                    while poller.get_map():
                        for key in list(poller.get_map().values()):
                            consume(key)
                except OSError as error:
                    failure = failure or error
        if poller is not None:
            poller.close()
        for fd in list(descriptors):
            close(fd)
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
    if failure is not None:
        raise failure
    code = os.waitstatus_to_exitcode(status)
    code = {'timeout': 124, 'output-limit': 125, 'cancelled': 130}.get(reason, code)
    return dict(code=code, reason=reason, stdout=bytes(output[0]),
                stderr=bytes(output[1]), reaped=reaped, counts=counts)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    interpreter, warning = select_interpreter(os.environ)
    if warning:
        print(warning, file=sys.stderr)
    if interpreter is None:
        return 1
    collector = os.path.join(os.path.dirname(os.path.realpath(__file__)), "hermes-usage.py")
    # Private session/process group: pid == pgid == sid afterwards, so the
    # service can signal this whole tree and nothing else.
    try:
        os.setsid()
    except OSError as error:
        if error.errno != errno.EPERM or os.getpgrp() != os.getpid():
            print(f"hermes-usage: cannot create a private process group: {error}", file=sys.stderr)
            return 1
    try:
        os.execve(interpreter, [interpreter, "-I", collector, *args], child_environment(os.environ))
    except OSError as error:
        print(f"hermes-usage: cannot execute {interpreter}: {error}", file=sys.stderr)
        return 1
    return 1  # unreachable: execve replaces the process


if __name__ == "__main__":
    raise SystemExit(main())
