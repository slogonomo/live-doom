# SPDX-License-Identifier: 0BSD
"""Token-checked ownership of Omarchy's stock screensaver suppression toggle."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import secrets
import stat
import tempfile

from project_paths import atomic_write, ensure_owned_dir, ensure_private_dir, open_regular


def _token_file(path):
    try:
        with open_regular(path) as stream:
            info = os.fstat(stream.fileno())
            value = stream.read(129)
            if len(value) > 128:
                return None, info
            return value, info
    except FileNotFoundError:
        return None, None


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns)


class SaverOwnership:
    def __init__(self, home, state_root):
        self.toggle = Path(home) / '.local/state/omarchy/toggles/screensaver-off'
        self.marker = Path(state_root) / 'owns-screensaver-off'

    @contextmanager
    def lock(self):
        ensure_private_dir(self.marker.parent)
        fd = os.open(self.marker.parent / '.screensaver-ownership.lock',
                     os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
                raise ValueError('Unsafe screensaver ownership lock')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def status(self, *, retry=True):
        marker, marker_info = _token_file(self.marker)
        try:
            toggle, toggle_info = _token_file(self.toggle)
        except (OSError, ValueError):
            # A user-controlled nonregular or symlink toggle is never ours.
            toggle, toggle_info = None, True
        again, again_info = _token_file(self.marker)
        if (again != marker or (marker_info is not None and again_info is not None
                               and _identity(marker_info) != _identity(again_info))):
            if retry:
                return self.status(retry=False)
            # Destruction/reload can retire a token between the reads. An
            # unstable sample must never persist a user-disabled preference.
            return {'owned': False, 'blocked': toggle_info is not None,
                    'revoked': False, 'token': None,
                    'toggle': str(self.toggle), 'marker': str(self.marker)}
        token = marker.decode('ascii') if marker is not None else None
        if token is not None and (len(token) != 64 or any(c not in '0123456789abcdef' for c in token)):
            raise ValueError('Invalid screensaver ownership token')
        owned = token is not None and toggle == marker
        return {'owned': owned, 'blocked': bool(toggle_info is not None and not owned),
                'revoked': bool(token is not None and not owned), 'token': token,
                'toggle': str(self.toggle), 'marker': str(self.marker)}

    def claim(self):
        with self.lock():
            status = self.status()
            if status['owned'] or status['blocked'] or status['revoked']:
                return status
            ensure_owned_dir(self.toggle.parent)
            token = secrets.token_hex(32).encode('ascii')
            # Write complete bytes first, then journal, then publish without
            # overwriting. A failed write never leaves an empty stock toggle.
            fd, temporary = tempfile.mkstemp(prefix='.live-doom-toggle-', dir=self.toggle.parent)
            try:
                with os.fdopen(fd, 'wb') as output:
                    output.write(token)
                    output.flush()
                    os.fsync(output.fileno())
                atomic_write(self.marker, token)
                try:
                    os.link(temporary, self.toggle, follow_symlinks=False)
                except FileExistsError:
                    if _token_file(self.marker)[0] == token:
                        self.marker.unlink()
                    return self.status()
            finally:
                Path(temporary).unlink(missing_ok=True)
            return self.status()

    def release(self):
        with self.lock():
            marker, marker_info = _token_file(self.marker)
            try:
                toggle, toggle_info = _token_file(self.toggle)
            except (OSError, ValueError):
                toggle, toggle_info = None, None
            released = False
            if marker is not None and marker == toggle and toggle_info is not None:
                if (_identity(self.toggle.lstat()) == _identity(toggle_info)
                        and _identity(self.marker.lstat()) == _identity(marker_info)):
                    self.toggle.unlink()
                    self.marker.unlink()
                    released = True
            # If the user removed/replaced it, retire only our private marker.
            elif marker_info is not None and _identity(self.marker.lstat()) == _identity(marker_info):
                self.marker.unlink()
            return self.status() | {'released': released}


# Passed verbatim to /usr/bin/python3 -I -c by the companion's destruction
# handler. It needs no files from a checkout that may already have been removed.
INLINE_RELEASE = r'''
import os,stat,sys
from pathlib import Path
def read(p):
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_CLOEXEC)
    try:
        for part in p.parts[1:-1]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_CLOEXEC|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=child
        f=os.open(p.name,os.O_RDONLY|os.O_CLOEXEC|os.O_NONBLOCK|os.O_NOFOLLOW,dir_fd=fd)
        try:
            s=os.fstat(f)
            if not stat.S_ISREG(s.st_mode) or s.st_uid!=os.getuid(): raise ValueError()
            return os.read(f,129),s,os.dup(fd)
        finally: os.close(f)
    finally: os.close(fd)
def identity(s): return s.st_dev,s.st_ino,s.st_mtime_ns,s.st_ctime_ns
fds=[]
try:
    toggle,marker=map(Path,sys.argv[1:3]);token=sys.argv[3].encode('ascii')
    if len(token)!=64 or any(c not in b'0123456789abcdef' for c in token): raise ValueError()
    a,sa,fa=read(toggle);fds.append(fa)
    b,sb,fb=read(marker);fds.append(fb)
    if a==b==token and identity(sa)==identity(os.stat(toggle.name,dir_fd=fa,follow_symlinks=False)) and identity(sb)==identity(os.stat(marker.name,dir_fd=fb,follow_symlinks=False)):
        os.unlink(toggle.name,dir_fd=fa);os.unlink(marker.name,dir_fd=fb)
except (OSError,ValueError,IndexError,UnicodeError): pass
finally:
    for fd in fds: os.close(fd)
'''
