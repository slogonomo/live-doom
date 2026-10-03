# SPDX-License-Identifier: 0BSD
"""Bounded, verified asset downloads; callers must obtain explicit consent."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import math
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile

from project_paths import ensure_owned_dir, open_regular


def _parent(path: Path) -> Path:
    path = Path(path).absolute()
    if '..' in path.parts:
        raise ValueError('Asset path may not contain parent traversal')
    ensure_owned_dir(path.parent)
    return path


def _regular(path: Path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError('Asset destination is not an owned regular file')
    return info


def _digest(path: Path, limit: int) -> str:
    with open_regular(path) as stream:
        if os.fstat(stream.fileno()).st_size > limit:
            raise ValueError('Asset file exceeds its size limit')
        digest, size = hashlib.sha256(), 0
        for block in iter(lambda: stream.read(65536), b''):
            size += len(block)
            if size > limit:
                raise ValueError('Asset file exceeds its size limit')
            digest.update(block)
        return digest.hexdigest()


def _https(url: str):
    if not isinstance(url, str) or len(url) > 4096:
        raise ValueError('Invalid asset URL')
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Asset downloads require an HTTPS URL without credentials')
    if parsed.port is not None and not 0 < parsed.port <= 65535:
        raise ValueError('Invalid asset URL port')


def _origin(url: str):
    parsed = urlsplit(url)
    return parsed.hostname.lower(), parsed.port or 443


class HTTPSRedirects(HTTPRedirectHandler):
    def __init__(self, origin_url=None):
        self.origin = _origin(origin_url) if origin_url else None

    def redirect_request(self, request, file, code, message, headers, newurl):
        _https(newurl)
        if self.origin is not None and _origin(newurl) != self.origin:
            raise ValueError('Asset redirect must remain on the same host')
        return super().redirect_request(request, file, code, message, headers, newurl)


def _fetch_stream(url, temporary, sha256, max_bytes, timeout, deadline, same_host=False):
    """Isolated worker: stream to a supplied private staging file, never publish."""
    stop = time.monotonic() + deadline
    request = Request(url, headers={'User-Agent': 'Live-Doom/1', 'Accept-Encoding': 'identity'})
    fd = os.open(temporary, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, 'wb') as output:
        with build_opener(HTTPSRedirects(url if same_host else None)).open(request, timeout=min(timeout, deadline)) as response:
            _https(response.geturl())
            if same_host and _origin(response.geturl()) != _origin(url):
                raise ValueError('Asset redirect must remain on the same host')
            declared = response.headers.get('Content-Length')
            if declared is not None and (not declared.isdecimal() or int(declared) > max_bytes):
                raise ValueError('Asset Content-Length exceeds its byte limit')
            size, digest = 0, hashlib.sha256()
            read = getattr(response, 'read1', response.read)
            while True:
                if time.monotonic() >= stop:
                    raise TimeoutError('Asset download deadline exceeded')
                block = read(min(65536, max_bytes + 1 - size))
                if not block:
                    break
                size += len(block)
                if size > max_bytes:
                    raise ValueError('Asset download exceeds its byte limit')
                digest.update(block)
                output.write(block)
            if size == 0 or digest.hexdigest() != sha256:
                raise ValueError('Asset SHA256 verification failed')
        output.flush()
        os.fsync(output.fileno())


def fetch_verified(url: str, destination: Path, sha256: str, max_bytes: int,
                   timeout: float = 10, deadline: float = 90, *, same_host: bool = False) -> Path:
    """Publish a hash-verified HTTPS download atomically, reusing verified cache.

    Network reads have a socket timeout and an overall wall-clock deadline.
    Files remain 0600, and failures leave an existing destination untouched.
    Nothing invokes this function automatically at startup or installation.
    """
    _https(url)
    if type(same_host) is not bool:
        raise ValueError('same_host must be a boolean')
    if not re.fullmatch(r'[0-9a-f]{64}', sha256):
        raise ValueError('Asset SHA256 must be a full lowercase digest')
    if type(max_bytes) is not int or not 0 < max_bytes <= 512 * 1024 * 1024:
        raise ValueError('Invalid asset byte limit')
    if (type(timeout) not in (int, float) or type(deadline) not in (int, float)
            or not math.isfinite(timeout) or not math.isfinite(deadline)
            or not 0 < timeout <= 30 or not 0 < deadline <= 300):
        raise ValueError('Invalid download deadline')
    destination = _parent(Path(destination))
    if _regular(destination):
        if _digest(destination, max_bytes) != sha256:
            raise ValueError('Existing asset cache has a different SHA256; preserve or remove it before retrying')
        return destination
    fd, temporary = tempfile.mkstemp(prefix='.download-', suffix='.tmp', dir=destination.parent)
    os.close(fd)
    try:
        # DNS, header reads and a slow-dripping body are all bounded by this
        # parent-owned deadline; socket timeouts alone do not bound those.
        try:
            worker = subprocess.run([sys.executable, '-B', str(Path(__file__).absolute()),
                '--worker', url, temporary, sha256, str(max_bytes), str(timeout), str(deadline), str(int(same_host))],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=deadline, check=False)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError('Asset download deadline exceeded') from exc
        if worker.returncode:
            raise ValueError(worker.stderr[:512].decode(errors='replace').strip() or 'Asset download failed')
        if _digest(Path(temporary), max_bytes) != sha256:
            raise ValueError('Asset SHA256 verification failed')
        # Reject a new symlink/nonregular destination before publication too.
        if _regular(destination) is not None:
            if _digest(destination, max_bytes) != sha256:
                raise ValueError('Asset destination changed during download')
            return destination
        os.replace(temporary, destination)
        return destination
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install_verified_zip(archive: Path, destination: Path, sha256: str,
                         max_archive_bytes: int, max_unpacked_bytes: int,
                         expected_files: dict[str, str]) -> Path:
    """Verify first, then extract one rooted ZIP into a new atomic directory."""
    archive = Path(archive)
    destination = _parent(Path(destination))
    if (type(max_archive_bytes) is not int or not 0 < max_archive_bytes <= 512 * 1024 * 1024
            or type(max_unpacked_bytes) is not int or not 0 < max_unpacked_bytes <= 256 * 1024 * 1024):
        raise ValueError('Invalid ZIP extraction byte limit')
    if (not isinstance(expected_files, dict) or not 1 <= len(expected_files) <= 64
            or any(not isinstance(name, str) or name in ('', '.', '..') or Path(name).name != name
                   or '\\' in name or ':' in name or not isinstance(digest, str)
                   or not re.fullmatch(r'[0-9a-f]{64}', digest)
                   for name, digest in expected_files.items())):
        raise ValueError('Invalid expected ZIP file hashes')
    if _digest(archive, max_archive_bytes) != sha256:
        raise ValueError('ZIP SHA256 verification failed')
    if destination.exists() or destination.is_symlink():
        info = destination.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError('Existing asset directory is not an owned directory')
        for name, digest in expected_files.items():
            if _digest(destination / name, max_unpacked_bytes) != digest:
                raise ValueError('Existing extracted data differs; preserve or remove it before retrying')
        return destination
    temporary = Path(tempfile.mkdtemp(prefix='.extract-', dir=destination.parent))
    try:
        with zipfile.ZipFile(archive) as zipped:
            members = zipped.infolist()
            if not members or len(members) > 64:
                raise ValueError('ZIP member count exceeds its limit')
            seen, total = set(), 0
            for member in members:
                name = PurePosixPath(member.filename)
                mode = member.external_attr >> 16
                if (name.is_absolute() or '..' in name.parts or '\\' in member.filename
                        or ':' in member.filename or not name.parts or name.parts[0] != destination.name
                        or member.filename in seen or member.flag_bits & 1
                        or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR))):
                    raise ValueError('Unsafe ZIP member')
                seen.add(member.filename)
                total += member.file_size
                if member.file_size > max_unpacked_bytes or total > max_unpacked_bytes:
                    raise ValueError('ZIP uncompressed data exceeds its limit')
            for member in members:
                relative = PurePosixPath(member.filename).parts[1:]
                if not relative:
                    continue
                target = temporary.joinpath(*relative)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True, mode=0o700)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'wb') as output, zipped.open(member) as data:
                    remaining = member.file_size
                    while block := data.read(min(65536, remaining + 1)):
                        remaining -= len(block)
                        if remaining < 0:
                            raise ValueError('ZIP member exceeds its declared size')
                        output.write(block)
                    if remaining:
                        raise ValueError('Truncated ZIP member')
                    output.flush()
                    os.fsync(output.fileno())
        for name, digest in expected_files.items():
            if _digest(temporary / name, max_unpacked_bytes) != digest:
                raise ValueError('Extracted asset SHA256 verification failed')
        os.replace(temporary, destination)
        return destination
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


if __name__ == '__main__':
    try:
        if len(sys.argv) != 9 or sys.argv[1] != '--worker' or sys.argv[8] not in ('0', '1'):
            raise ValueError('This module is an internal download worker')
        _fetch_stream(sys.argv[2], sys.argv[3], sys.argv[4], int(sys.argv[5]),
                      float(sys.argv[6]), float(sys.argv[7]), sys.argv[8] == '1')
    except Exception as error:
        print(str(error)[:512], file=sys.stderr)
        sys.exit(1)
