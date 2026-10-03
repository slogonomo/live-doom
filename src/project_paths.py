# SPDX-License-Identifier: 0BSD
"""Shared Live Doom paths and bounded, owner-checked local file helpers.

Path discovery does not create directories or migrate legacy installations.
Only ``runtime_root`` requires a live, private runtime directory. Build and
installer callers can query every artifact path without a desktop session.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import errno
import json
import os
from pathlib import Path
import select
import shlex
import stat
import subprocess
import tempfile
import threading
from typing import Any, Iterator, Mapping

PLUGIN_ID = "io.github.slogonomo.live-doom"
APP_NAME = "live-doom"
SERVICE_NAME = "doom-desktop.service"
FREEDOOM_VERSION = "0.13.0"
MAX_JSON_BYTES = 1024 * 1024
LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUPS = 2


def _absolute(value: str | Path, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{label} must be an absolute path without '..'")
    return path


@dataclass(frozen=True)
class ProjectPaths:
    home: Path
    data_root: Path
    cache_root: Path
    config_root: Path
    state_root: Path
    _runtime_parent: Path | None = None
    _runtime_override: Path | None = None

    @classmethod
    def from_env(cls, home: str | Path | None = None,
                 env: Mapping[str, str] | None = None) -> "ProjectPaths":
        # An explicit fixture home must not inherit the user's real XDG paths.
        values = dict(os.environ if env is None and home is None else env or {})
        home_path = _absolute(home if home is not None else values.get("HOME", str(Path.home())), "HOME")

        def xdg(name: str, fallback: Path) -> Path:
            return _absolute(values.get(name) or fallback, name) / APP_NAME

        parent = values.get("XDG_RUNTIME_DIR")
        override = values.get("LIVE_DOOM_RUNTIME") or values.get("DOOM_DESKTOP_RUNTIME")
        return cls(home_path, xdg("XDG_DATA_HOME", home_path / ".local/share"),
                   xdg("XDG_CACHE_HOME", home_path / ".cache"),
                   xdg("XDG_CONFIG_HOME", home_path / ".config"),
                   xdg("XDG_STATE_HOME", home_path / ".local/state"),
                   _absolute(parent, "XDG_RUNTIME_DIR") if parent else None,
                   _absolute(override, "LIVE_DOOM_RUNTIME") if override else None)

    @property
    def vendor_root(self) -> Path:
        return self.data_root / "vendor"

    @property
    def deps_root(self) -> Path:
        return self.data_root / "deps"

    @property
    def build_root(self) -> Path:
        return self.cache_root / "build"

    @property
    def bin_root(self) -> Path:
        return self.data_root / "bin"

    @property
    def asset_root(self) -> Path:
        return self.data_root / "assets"

    @property
    def engine(self) -> Path:
        return self.bin_root / "eternity"

    @property
    def viewer(self) -> Path:
        return self.bin_root / "doom-viewer"

    @property
    def engine_base(self) -> Path:
        return self.vendor_root / "autodoom/base"

    @property
    def freedoom_root(self) -> Path:
        return self.asset_root / ("freedoom-" + FREEDOOM_VERSION)

    @property
    def default_iwad(self) -> Path:
        return self.freedoom_root / "freedoom1.wad"

    @property
    def config_file(self) -> Path:
        return self.config_root / "config.json"

    @property
    def last_good_config(self) -> Path:
        return self.config_root / "config.last-good.json"

    @property
    def games_root(self) -> Path:
        return self.data_root / "games"

    @property
    def catalog_file(self) -> Path:
        return self.data_root / "steam-catalog.json"

    @property
    def agent_config_root(self) -> Path:
        return self.config_root / "agents"

    @property
    def agent_state_root(self) -> Path:
        return self.state_root / "agents"

    @property
    def legacy_data_root(self) -> Path:
        return self.data_root.parent / "doom-desktop"

    @property
    def legacy_state_root(self) -> Path:
        return self.state_root.parent / "doom-desktop"

    def runtime_root(self, *, create: bool = True) -> Path:
        if self._runtime_override is not None:
            path = self._runtime_override
            if create:
                ensure_private_dir(path)
            else:
                with _directory_fd(path, private=True):
                    pass
            return path
        if self._runtime_parent is None:
            raise ValueError("XDG_RUNTIME_DIR is required (or an explicit private LIVE_DOOM_RUNTIME)")
        with _directory_fd(self._runtime_parent, private=True):
            pass
        path = self._runtime_parent / APP_NAME
        if create:
            ensure_private_dir(path)
        else:
            with _directory_fd(path, private=True):
                pass
        return path


def _check_directory(info: os.stat_result, path: Path, *, private: bool):
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise PermissionError(f"Directory must be owned by the current user: {path}")
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o022 or (private and mode != 0o700):
        raise PermissionError(f"Unsafe directory permissions ({mode:o}): {path}")


@contextmanager
def _directory_fd(path: str | Path, *, create: bool = False,
                  private: bool = False, require_owner: bool = True) -> Iterator[int]:
    """Open every component without following symlinks, retaining the leaf fd."""
    target = _absolute(path, "Directory")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    fd = os.open("/", flags)
    try:
        for component in target.parts[1:]:
            try:
                child = os.open(component, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        if require_owner:
            _check_directory(os.fstat(fd), target, private=private)
        yield fd
    finally:
        os.close(fd)


def ensure_private_dir(path: str | Path) -> Path:
    target = _absolute(path, "Private directory")
    with _directory_fd(target, create=True, private=True):
        pass
    return target


def ensure_owned_dir(path: str | Path) -> Path:
    """Create 0700; an existing owned, non-writable-by-others directory is safe."""
    target = _absolute(path, "Owned directory")
    with _directory_fd(target, create=True):
        pass
    return target


@contextmanager
def open_directory(path: str | Path, *, private: bool = False) -> Iterator[int]:
    """Hold an existing owned directory for safe descriptor-relative scanning."""
    with _directory_fd(path, private=private) as fd:
        yield fd


def _check_regular(info: os.stat_result, path: Path, *, require_owner: bool):
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"Expected a regular file: {path}")
    if require_owner and info.st_uid != os.getuid():
        raise PermissionError(f"File must be owned by the current user: {path}")


@contextmanager
def open_regular(path: str | Path, *, require_owner: bool = True):
    """Read-only binary file; rejects symlink components, FIFOs and devices.

    Streaming callers must supply finite read lengths. ``read_bytes`` enforces
    both an initial size check and a read cap if the file grows concurrently.
    """
    target = _absolute(path, "File")
    with _directory_fd(target.parent, require_owner=False) as parent:
        fd = os.open(target.name, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=parent)
    try:
        _check_regular(os.fstat(fd), target, require_owner=require_owner)
        handle = os.fdopen(fd, "rb")
        fd = -1
        with handle:
            yield handle
    finally:
        if fd >= 0:
            os.close(fd)


def read_bytes(path: str | Path, max_bytes: int, *, require_owner: bool = True) -> bytes:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("A finite, nonnegative read limit is required")
    with open_regular(path, require_owner=require_owner) as handle:
        if os.fstat(handle.fileno()).st_size > max_bytes:
            raise ValueError(f"File exceeds {max_bytes} byte limit: {path}")
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"File grew past {max_bytes} byte limit: {path}")
    return data


def read_text(path: str | Path, max_bytes: int = MAX_JSON_BYTES, *, require_owner: bool = True) -> str:
    return read_bytes(path, max_bytes, require_owner=require_owner).decode("utf-8")


def read_json(path: str | Path, max_bytes: int = MAX_JSON_BYTES, *, require_owner: bool = True) -> Any:
    try:
        return json.loads(read_text(path, max_bytes, require_owner=require_owner))
    except RecursionError:
        raise ValueError("JSON nesting is too deep") from None


def _check_replace_target(parent: int, name: str, path: Path):
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    _check_regular(info, path, require_owner=True)
    if info.st_nlink != 1:
        raise PermissionError(f"Refusing a multiply linked output file: {path}")


def atomic_write(path: str | Path, data: bytes, *, max_bytes: int = MAX_JSON_BYTES):
    """Replace a regular owned file with an unpredictable, fsynced 0600 file."""
    if (isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0
            or not isinstance(data, bytes) or len(data) > max_bytes):
        raise ValueError(f"Atomic output must be bytes within {max_bytes} byte limit")
    target = _absolute(path, "Output")
    with _directory_fd(target.parent, create=True) as parent:
        _check_replace_target(parent, target.name, target)
        # The held directory fd prevents parent-path replacement between
        # mkstemp and rename. mkstemp uses O_CREAT|O_EXCL for the random leaf.
        fd, temporary = tempfile.mkstemp(prefix="." + target.name + ".", dir=f"/proc/self/fd/{parent}")
        name = Path(temporary).name
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            _check_replace_target(parent, target.name, target)
            os.replace(name, target.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(name, dir_fd=parent)
            except FileNotFoundError:
                pass


def atomic_json(path: str | Path, value: Any, *, max_bytes: int = MAX_JSON_BYTES):
    atomic_write(path, (json.dumps(value, indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8"),
                 max_bytes=max_bytes)


class RotatingProcessLog:
    """Continuously drain a child pipe into a bounded set of private log files.

    A direct child stdout file cannot be safely rotated: its inherited fd
    keeps writing the retired inode. The reader owns the only file writer.
    On a disk error it records ``error`` and keeps draining, avoiding child
    deadlock or an unbounded in-memory queue.
    """
    def __init__(self, path: str | Path, *, max_bytes: int = LOG_MAX_BYTES, backups: int = LOG_BACKUPS):
        if (isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1 <= max_bytes <= 64 * 1024 * 1024
                or isinstance(backups, bool) or not isinstance(backups, int) or not 0 <= backups <= 8):
            raise ValueError("Log limits must be 1..64MiB and 0..8 backups")
        self.path = _absolute(path, "Log")
        self.max_bytes, self.backups = max_bytes, backups
        self.error: str | None = None
        self._stop = threading.Event()
        self._parent_context = _directory_fd(self.path.parent, create=True)
        self._parent = self._parent_context.__enter__()
        self._file = -1
        self._reader = self._writer = -1
        self._thread: threading.Thread | None = None
        try:
            for index in range(backups + 1):
                name = self._name(index)
                _check_replace_target(self._parent, name, self.path.parent / name)
                try:
                    fd = os.open(name, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=self._parent)
                except FileNotFoundError:
                    continue
                try:
                    self._secure_file(fd, name)
                    if os.fstat(fd).st_size > max_bytes:
                        os.lseek(fd, -max_bytes, os.SEEK_END)
                        tail = os.read(fd, max_bytes)
                        os.ftruncate(fd, 0)
                        os.lseek(fd, 0, os.SEEK_SET)
                        self._write_all(fd, tail)
                finally:
                    os.close(fd)
            self._file = self._open_log()
            self._size = os.fstat(self._file).st_size
            os.lseek(self._file, 0, os.SEEK_END)
            self._reader, self._writer = os.pipe2(os.O_CLOEXEC)
            os.set_blocking(self._reader, False)
            self._thread = threading.Thread(target=self._run, name="live-doom-log", daemon=True)
            self._thread.start()
        except BaseException:
            for fd in (self._file, self._reader, self._writer):
                if fd >= 0:
                    os.close(fd)
            self._parent_context.__exit__(None, None, None)
            raise

    def _name(self, index: int) -> str:
        return self.path.name + (f".{index}" if index else "")

    def _secure_file(self, fd: int, name: str):
        info = os.fstat(fd)
        _check_regular(info, self.path.parent / name, require_owner=True)
        if info.st_nlink != 1:
            raise PermissionError("Refusing a multiply linked log file")
        os.fchmod(fd, 0o600)

    def _open_log(self) -> int:
        fd = os.open(self.path.name, os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
                     0o600, dir_fd=self._parent)
        try:
            self._secure_file(fd, self.path.name)
        except BaseException:
            os.close(fd)
            raise
        return fd

    @staticmethod
    def _write_all(fd: int, data: bytes):
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError(errno.EIO, "Unable to write process log")
            view = view[written:]

    def _rotate(self):
        if not self.backups:
            os.ftruncate(self._file, 0)
            os.lseek(self._file, 0, os.SEEK_SET)
        else:
            for index in range(self.backups, 0, -1):
                source, destination = self._name(index - 1), self._name(index)
                _check_replace_target(self._parent, source, self.path.parent / source)
                _check_replace_target(self._parent, destination, self.path.parent / destination)
                try:
                    os.replace(source, destination, src_dir_fd=self._parent, dst_dir_fd=self._parent)
                except FileNotFoundError:
                    pass
            os.close(self._file)
            self._file = -1
            self._file = self._open_log()
        self._size = 0

    def _append(self, data: bytes):
        while data:
            if self._size == self.max_bytes:
                self._rotate()
            count = min(len(data), self.max_bytes - self._size)
            self._write_all(self._file, data[:count])
            self._size += count
            data = data[count:]

    def _run(self):
        try:
            while not self._stop.is_set():
                if not select.select([self._reader], [], [], 0.1)[0]:
                    continue
                try:
                    data = os.read(self._reader, 16384)
                except BlockingIOError:
                    continue
                if not data:
                    break
                if self.error is None:
                    try:
                        self._append(data)
                    except (OSError, ValueError) as exc:
                        self.error = str(exc)
        finally:
            os.close(self._reader)
            if self._file >= 0:
                os.close(self._file)
            self._parent_context.__exit__(None, None, None)

    @property
    def writer(self) -> int:
        return self._writer

    def close_writer(self):
        if self._writer >= 0:
            os.close(self._writer)
            self._writer = -1

    def finish(self, timeout: float = 0.5) -> bool:
        """After child exit, wait briefly for EOF and pending output; no kill."""
        self.close_writer()
        self._thread.join(max(0.0, min(5.0, timeout)))
        return not self._thread.is_alive()

    def close(self, timeout: float = 0.5) -> bool:
        """Abandon a sink after child teardown; never signal a process."""
        self.close_writer()
        self._stop.set()
        self._thread.join(max(0.0, min(5.0, timeout)))
        return not self._thread.is_alive()


def spawn_logged(argv, log_path: str | Path, *, log_max_bytes: int = LOG_MAX_BYTES,
                 log_backups: int = LOG_BACKUPS, **kwargs) -> subprocess.Popen:
    if "stdout" in kwargs or "stderr" in kwargs:
        raise ValueError("spawn_logged owns stdout and stderr")
    sink = RotatingProcessLog(log_path, max_bytes=log_max_bytes, backups=log_backups)
    try:
        process = subprocess.Popen(argv, stdout=sink.writer, stderr=subprocess.STDOUT, **kwargs)
    except BaseException:
        sink.close()
        raise
    finally:
        sink.close_writer()
    process._live_doom_log = sink
    return process


def flush_process_log(process, timeout: float = 0.5) -> bool:
    sink = getattr(process, "_live_doom_log", None)
    return True if sink is None else sink.finish(timeout)


def close_process_log(process, timeout: float = 0.5) -> bool:
    sink = getattr(process, "_live_doom_log", None)
    return True if sink is None else sink.close(timeout)


CLI_FIELDS = ("data_root", "cache_root", "config_root", "state_root", "vendor_root", "deps_root",
              "build_root", "bin_root", "asset_root", "engine", "viewer", "engine_base", "freedoom_root",
              "default_iwad", "config_file", "last_good_config", "games_root", "catalog_file",
              "agent_config_root", "agent_state_root", "legacy_data_root", "legacy_state_root")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--field", choices=(*CLI_FIELDS, "runtime_root", "plugin_id", "service_name"))
    group.add_argument("--shell", action="store_true", help="quoted LIVE_DOOM_* assignments; no runtime creation")
    parser.add_argument("--home", type=Path)
    options = parser.parse_args()
    try:
        paths = ProjectPaths.from_env(home=options.home)
        if options.shell:
            print("LIVE_DOOM_PLUGIN_ID=" + shlex.quote(PLUGIN_ID))
            print("LIVE_DOOM_SERVICE_NAME=" + shlex.quote(SERVICE_NAME))
            for field in CLI_FIELDS:
                print("LIVE_DOOM_" + field.upper() + "=" + shlex.quote(str(getattr(paths, field))))
        elif options.field == "plugin_id":
            print(PLUGIN_ID)
        elif options.field == "service_name":
            print(SERVICE_NAME)
        elif options.field == "runtime_root":
            print(paths.runtime_root())
        else:
            print(getattr(paths, options.field))
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Live Doom paths: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
