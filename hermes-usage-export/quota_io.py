"""Shared, stdlib-only snapshot schema and descriptor-relative private IO.

This file lives inside the separately installable companion. The collector loads
this exact local source by absolute path under python -I, not via sys.path.
"""
import json
import math
import os
import secrets
import stat
import unicodedata
from contextlib import contextmanager
from pathlib import Path

PROVIDERS = ('openai-codex', 'anthropic', 'openrouter', 'nous')
SELECTION = 'Hermes-resolved credential; may differ from this conversation; not a pool total'
CAP = 16384
TTL = 600
ACCESS = ('unknown', 'allowed', 'denied', 'member-cap-exceeded')


def number(value, low=0, high=1e12):
    if type(value) not in (int, float): return False
    try: return math.isfinite(value) and low <= value <= high
    except OverflowError: return False


def plain(value, limit=120):
    return (type(value) is str and len(value) <= limit
            and not any(unicodedata.category(c).startswith('C') for c in value))


def unavailable(provider, now):
    return dict(schemaVersion=1, provider=provider, scope='account', accountSelection=SELECTION,
                fetchedAt=now, expiresAt=now + TTL, source='hermes-account-usage',
                plan='', windows=[], available=False, status='unavailable', accessStatus='unknown')


def validate(value, provider, now):
    if type(value) is not dict or provider not in PROVIDERS: return None
    allowed = set(unavailable(provider, now)) | {'remainingUsd', 'currency'}
    if set(value) - allowed: return None
    if type(value.get('schemaVersion')) is not int or value['schemaVersion'] != 1: return None
    if value.get('provider') != provider or value.get('scope') != 'account': return None
    if value.get('accountSelection') != SELECTION or value.get('source') != 'hermes-account-usage': return None
    if not plain(value.get('plan')) or value.get('accessStatus') not in ACCESS: return None
    fetched, expiry = value.get('fetchedAt'), value.get('expiresAt')
    if not all(number(x) for x in (fetched, expiry, now)): return None
    if not fetched <= now < expiry <= fetched + TTL: return None
    windows = value.get('windows')
    if type(windows) is not list or len(windows) > 8: return None
    clean = []
    for w in windows:
        if type(w) is not dict or set(w) != {'label', 'usedPercent', 'remainingPercent', 'resetAt'}: return None
        p, reset = w['usedPercent'], w['resetAt']
        if not plain(w['label'], 80) or not number(p, high=100): return None
        if not number(w['remainingPercent'], high=100) or abs(w['remainingPercent'] - (100-p)) > 1e-8: return None
        if reset is not None and not number(reset): return None
        if reset is None or reset > now: clean.append(dict(w))
    money = 'remainingUsd' in value
    if money and (provider != 'nous' or value.get('currency') != 'USD' or not number(value['remainingUsd'])): return None
    if not money and 'currency' in value: return None
    observed = bool(windows or money)
    if type(value.get('available')) is not bool or value['available'] != observed: return None
    if value.get('status') != ('observed' if observed else 'unavailable'): return None
    result = dict(value, windows=clean)
    result['available'] = bool(clean or money)
    result['status'] = 'observed' if result['available'] else 'unavailable'
    return result


@contextmanager
def directory(path, create=False):
    """Retain each nofollow component handle; never traverse a writable parent.

    Root-owned ancestors are allowed; the final directory must be user-owned.
    No /tmp exception: use a private directory beneath a trusted home.
    """
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts: raise OSError('unsafe path')
    fds = []; edges = []
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    def check(fd, final=False):
        s = os.fstat(fd)
        if not stat.S_ISDIR(s.st_mode) or s.st_nlink < 1 or s.st_mode & 0o022: raise OSError('unsafe directory')
        if s.st_uid not in ((os.geteuid(),) if final else (0, os.geteuid())): raise OSError('foreign directory')
        return s
    try:
        fds.append(os.open('/', flags)); check(fds[-1])
        for name in path.parts[1:]:
            parent = fds[-1]
            if create:
                try: os.mkdir(name, 0o700, dir_fd=parent)
                except FileExistsError: pass
            fd = os.open(name, flags, dir_fd=parent); fds.append(fd)
            check(fd); edges.append((parent, name, fd))
        check(fds[-1], True)
        def verify():
            for parent, name, fd in edges:
                s = check(fd)
                linked = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (s.st_dev, s.st_ino) != (linked.st_dev, linked.st_ino): raise OSError('directory changed')
            check(fds[-1], True)
        verify()
        yield fds[-1], verify
    finally:
        for fd in reversed(fds): os.close(fd)


def atomic_write(folder, name, payload):
    if '/' in name or name in ('.', '..'): raise OSError('unsafe filename')
    with directory(folder, create=True) as (dfd, verify):
        temp = '.quota-' + secrets.token_hex(12)
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dfd)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(payload); stream.flush(); os.fsync(stream.fileno())
            verify()
            os.replace(temp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
            os.fsync(dfd)
            verify()
        finally:
            try: os.unlink(temp, dir_fd=dfd)
            except FileNotFoundError: pass


def write_snapshot(folder, provider, record):
    if provider not in PROVIDERS: raise ValueError('unsupported provider')
    if validate(record, provider, record.get('fetchedAt')) is None: raise ValueError('invalid snapshot')
    payload = json.dumps(record, allow_nan=False, separators=(',', ':')).encode()
    if len(payload) > CAP: raise ValueError('snapshot ceiling')
    atomic_write(folder, provider + '.json', payload)


def read_snapshot(folder, provider, now):
    if provider not in PROVIDERS: return None
    try:
        with directory(folder) as (dfd, verify):
            fd = os.open(provider + '.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dfd)
            try:
                s = os.fstat(fd)
                if not stat.S_ISREG(s.st_mode) or s.st_uid != os.geteuid() or s.st_mode & 0o022 or s.st_size > CAP: return None
                raw = os.read(fd, CAP + 1)
            finally: os.close(fd)
            verify()
        if len(raw) > CAP: return None
        # Limit nesting before JSON decoding (even malformed quoted data is bounded).
        depth = 0; quoted = False; escaped = False
        for c in raw:
            if quoted:
                if escaped: escaped = False
                elif c == 92: escaped = True
                elif c == 34: quoted = False
            elif c == 34: quoted = True
            elif c in (91, 123):
                depth += 1
                if depth > 6: return None
            elif c in (93, 125): depth -= 1
        def integer(s):
            if len(s) > 16: raise ValueError('integer ceiling')
            return int(s)
        def constant(s): raise ValueError('nonfinite')
        return validate(json.loads(raw, parse_int=integer, parse_constant=constant), provider, now)
    except (OSError, ValueError, TypeError, OverflowError, RecursionError): return None
