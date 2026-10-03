#!/usr/bin/env python3
# SPDX-License-Identifier: 0BSD
"""Export a clean, reviewable Live Doom tree; never initialize Git or publish.

Dry-run is the default. --write requires an existing empty destination outside
the source checkout. A sibling .inventory.json records exact payload SHA256s
without adding host metadata or a self-referential hash to the public tree.
Limits below are conservative exporter limits; the installed Omarchy validator
has no published byte/file-count ceiling. Release materials must be ready: this
tool never repairs source, rewrites documentation, or creates placeholders.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import selectors
import stat
import struct
import subprocess
import tempfile
import time
from urllib.parse import unquote, urlsplit

PLUGIN_ID = "io.github.slogonomo.live-doom"
MAX_FILES = 512
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_GIT_BYTES = 4 * 1024 * 1024
MAX_INDEX_ENTRIES = 10000
GIT_TIMEOUT = 10.0
REQUIRED = frozenset(("manifest.json", "README.md", "LICENSE", "THIRD-PARTY-NOTICES.md",
                      "preview.png", "assets/live-doom.webp", "LICENSES/0BSD.txt",
                      "LICENSES/CC0-1.0.txt", "LICENSES/GPL-3.0-or-later.txt",
                      "LICENSES/Zlib.txt",
                      "patches/sdl-mixer-device.patch"))
ROOT_FILES = REQUIRED | {".gitignore"}
SOURCE_TREES = frozenset(("src", "scripts", "patches", "sdk", "examples", "menu", "service"))
TEXT_SUFFIXES = frozenset((".py", ".sh", ".c", ".h", ".xml", ".qml", ".js", ".json",
                           ".md", ".txt", ".patch", ".service"))
OMIT_SCRIPTS = frozenset(("scripts/build-virtual-click.sh", "scripts/test-viewer.sh",
                          "scripts/refresh-engine-patch.py"))
OMIT_PROTOCOLS = frozenset(("src/protocols/wlr-virtual-pointer-unstable-v1.xml",))
PORTABLE_TESTS = frozenset(("tests/test_agent_control.py", "tests/test_background_selection.py",
                           "tests/test_bot_progress.py",
                           "tests/test_dimensions.py", "tests/test_live_wallpaper.py",
                           "tests/test_steam_games.py", "tests/test_control_server.py",
                           "tests/test_project_paths.py", "tests/test_marketplace_install.py",
                           "tests/test_build_downloads.py", "tests/test_onboarding.py",
                           "tests/test_saver_ownership.py", "tests/test_process_utils.py",
                           "tests/test_menu_commands.py", "tests/test_marketplace_controller.py",
                           "tests/test_native_lifecycle.py",
                           "tests/test_native_preferences.py",
                           "tests/test_native_audio_policy.py",
                           "tests/test_mixer_device.py",
                           "tests/test_render_policy.py",
                           "tests/test_viewer_present.py", "tests/viewer-snapshot.c",
                           "tests/test_release_export.py"))
PORTABLE_DOCS = frozenset(("docs/design/EXTERNAL-AGENT.md",))
FORBIDDEN_PARTS = frozenset((".git", "__pycache__", ".pytest_cache", ".cache", "node_modules"))
LINK = re.compile(r"(?<!!)\[[^\]\n]+\]\(([^)\n]+)\)")
HOME_PATH = re.compile(r"/(?:home|Users)/(?!user(?:[/\s\"']|$)|example(?:[/\s\"']|$))"
                       r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]")
OLD_ID = re.compile(r"live-doom-dev\.(?:menu|idle)")


class ExportError(ValueError):
    pass


@dataclass(frozen=True)
class Payload:
    path: str
    data: bytes
    mode: int

    def record(self) -> dict:
        return {"path": self.path, "bytes": len(self.data),
                "sha256": hashlib.sha256(self.data).hexdigest(), "mode": f"{self.mode:04o}"}


@dataclass
class Plan:
    files: list[Payload]
    warnings: list[str]
    omitted: list[str]

    def inventory(self) -> dict:
        rows = [file.record() for file in self.files]
        encoded = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return {"format": "live-doom-release-v1", "plugin_id": PLUGIN_ID, "files": rows,
                "file_count": len(rows), "total_bytes": sum(row["bytes"] for row in rows),
                "payload_sha256": hashlib.sha256(encoded).hexdigest(), "warnings": self.warnings,
                "limits": {"files": MAX_FILES, "file_bytes": MAX_FILE_BYTES,
                           "total_bytes": MAX_TOTAL_BYTES, "kind": "exporter safety limits"}}


def _absolute(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    return path


def _relative(text: str) -> str:
    path = PurePosixPath(text)
    if (not text or path.is_absolute() or any(part in ("", ".", "..") for part in text.split("/"))
            or any(ord(char) < 32 for char in text) or "\\" in text
            or len(os.fsencode(text)) > 1024 or len(path.parts) > 16):
        raise ExportError("Unsafe relative source path")
    return path.as_posix()


def _directory(path: Path) -> int:
    """Open every absolute component without following a symlink."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    fd = os.open("/", flags)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _git(source: Path, arguments: list[str], *, input_bytes: bytes = b"", codes=(0,)) -> bytes:
    """Local index queries only: bounded output, input and wall-clock time."""
    if len(input_bytes) > MAX_GIT_BYTES:
        raise ExportError("Local Git input exceeds the export query limit")
    process = subprocess.Popen(["git", "-C", str(source), *arguments], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               env=os.environ | {"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"})
    deadline = time.monotonic() + GIT_TIMEOUT
    output = bytearray()
    offset = 0
    try:
        with selectors.DefaultSelector() as selector:
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ, "read")
            if input_bytes:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, "write")
            else:
                process.stdin.close()
            while selector.get_map():
                left = deadline - time.monotonic()
                if left <= 0:
                    raise ExportError("Local Git index query timed out")
                for key, _ in selector.select(min(left, 0.1)):
                    if key.data == "read":
                        chunk = os.read(process.stdout.fileno(), 16384)
                        if not chunk:
                            selector.unregister(process.stdout)
                            continue
                        output.extend(chunk)
                        if len(output) > MAX_GIT_BYTES:
                            raise ExportError("Local Git index exceeds the export query limit")
                    else:
                        try:
                            written = os.write(process.stdin.fileno(), input_bytes[offset:offset + 16384])
                        except BrokenPipeError:
                            selector.unregister(process.stdin)
                            process.stdin.close()
                            continue
                        offset += written
                        if offset == len(input_bytes):
                            selector.unregister(process.stdin)
                            process.stdin.close()
        code = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        if code not in codes:
            raise ExportError("Local Git query failed; source must be a readable checkout")
        return bytes(output)
    except subprocess.TimeoutExpired:
        raise ExportError("Local Git index query timed out") from None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        process.stdout.close()
        if not process.stdin.closed:
            process.stdin.close()


def _reject_links(source: Path, relative: str):
    current = source
    for part in PurePosixPath(relative).parts:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return  # Deleted working files are omitted, never resurrected.
        if stat.S_ISLNK(info.st_mode):
            raise ExportError("Source contains a tracked symlink: " + relative)


def _selected(relative: str) -> bool:
    path = PurePosixPath(relative)
    if any(part in FORBIDDEN_PARTS for part in path.parts):
        return False
    if path.name.lower() == "manifest.json" and (len(path.parts) == 1 or path.parts[0] in SOURCE_TREES):
        return True
    if relative in ROOT_FILES or relative == "scripts/doomctl":
        return True
    if relative in OMIT_SCRIPTS or relative in OMIT_PROTOCOLS:
        return False
    if relative in PORTABLE_TESTS or relative in PORTABLE_DOCS:
        return True
    if path.parts[:2] == ("docs", "licenses"):
        return path.suffix == ".txt"
    if len(path.parts) == 2 and path.parts[0] == "LICENSES":
        return path.name in ("0BSD.txt", "CC0-1.0.txt", "GPL-3.0-or-later.txt")
    return len(path.parts) > 1 and path.parts[0] in SOURCE_TREES and path.suffix in TEXT_SUFFIXES


def _read(source: Path, relative: str) -> Payload:
    parent = _directory(source / PurePosixPath(relative).parent)
    try:
        fd = os.open(PurePosixPath(relative).name,
                     os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ExportError("Source payload must be a regular file: " + relative)
        if info.st_size > MAX_FILE_BYTES:
            raise ExportError("Source payload exceeds per-file limit: " + relative)
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            data = handle.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise ExportError("Source payload grew past per-file limit: " + relative)
        return Payload(relative, data, 0o644 | (stat.S_IMODE(info.st_mode) & 0o111))
    finally:
        if fd >= 0:
            os.close(fd)


def _validate(files: list[Payload], warnings: list[str]):
    names = {file.path for file in files}
    manifests = [name for name in names if PurePosixPath(name).name.lower() == "manifest.json"]
    if manifests != ["manifest.json"]:
        raise ExportError("Exactly one root manifest.json is required; remove nested/case variants")
    manifest_file = next(file for file in files if file.path == "manifest.json")
    if len(manifest_file.data) > 65536:
        raise ExportError("manifest.json exceeds 64KiB")
    try:
        manifest = json.loads(manifest_file.data)
    except (ValueError, UnicodeError, RecursionError):
        raise ExportError("manifest.json must be valid bounded JSON") from None
    if (not isinstance(manifest, dict) or manifest.get("schemaVersion") != 1
            or isinstance(manifest.get("schemaVersion"), bool) or manifest.get("id") != PLUGIN_ID
            or not isinstance(manifest.get("name"), str) or not manifest["name"]
            or not isinstance(manifest.get("version"), str) or not manifest["version"]
            or not isinstance(manifest.get("kinds"), list) or not manifest["kinds"]
            or not isinstance(manifest.get("entryPoints"), dict)):
        raise ExportError("Root manifest must use schemaVersion1 and the canonical Live Doom id/kinds/entryPoints")
    kind_entries = {"bar": "bar", "bar-widget": "barWidget", "menu": "menu", "overlay": "overlay",
                    "panel": "panel", "service": "service"}
    for kind in manifest["kinds"]:
        if not isinstance(kind, str) or (kind in kind_entries and kind_entries[kind] not in manifest["entryPoints"]):
            raise ExportError("Manifest kind lacks its required entry point")
    for entry in manifest["entryPoints"].values():
        if not isinstance(entry, str) or _relative(entry) not in names:
            raise ExportError("Manifest entry point is absent from export: " + str(entry))
    directories = {str(parent) for name in names for parent in PurePosixPath(name).parents}
    errors = []
    for file in files:
        if file.path == "preview.png":
            if (len(file.data) < 33 or not file.data.startswith(b"\x89PNG\r\n\x1a\n")
                    or file.data[12:16] != b"IHDR" or struct.unpack(">I", file.data[8:12])[0] != 13
                    or any(not 1 <= dim <= 8192 for dim in struct.unpack(">II", file.data[16:24]))):
                errors.append("preview.png: expected PNG release artwork, not a placeholder")
            continue
        if file.path == "assets/live-doom.webp":
            if (len(file.data) < 20 or file.data[:4] != b"RIFF" or file.data[8:12] != b"WEBP"
                    or struct.unpack("<I", file.data[4:8])[0] != len(file.data) - 8
                    or file.data[12:16] not in (b"VP8 ", b"VP8L", b"VP8X")):
                errors.append("assets/live-doom.webp: expected WebP release artwork")
            continue
        try:
            text = file.data.decode("utf-8")
        except UnicodeError:
            errors.append(file.path + ": binary/non-UTF8 content is not release source")
            continue
        if "\0" in text:
            errors.append(file.path + ": binary/NUL content is not release source")
        for number, line in enumerate(text.splitlines(), 1):
            if HOME_PATH.search(line):
                errors.append(f"{file.path}:{number}: personal absolute home path must be removed")
            if OLD_ID.search(line) and "LEGACY" not in line and not file.path.startswith("tests/"):
                warnings.append(f"{file.path}:{number}: legacy plugin id reference; review migration/comment context")
        if file.path.endswith(".md"):
            for match in LINK.finditer(text):
                target = match.group(1).strip()
                target = target[1:target.find(">")] if target.startswith("<") else target.split()[0]
                if not target or target.startswith("#") or urlsplit(target).scheme:
                    continue
                target = unquote(target.partition("#")[0].partition("?")[0])
                normalized = posixpath.normpath(posixpath.join(str(PurePosixPath(file.path).parent), target))
                if normalized not in names and normalized not in directories:
                    errors.append(file.path + ": link target excluded/missing: " + target)
    if errors:
        raise ExportError("Release source needs review:\n" + "\n".join(errors[:80])
                          + (f"\n... {len(errors) - 80} more findings" if len(errors) > 80 else ""))


def prepare(source: Path) -> Plan:
    source = _absolute(source)
    fd = _directory(source)
    os.close(fd)
    tracked = _git(source, ["ls-files", "--stage", "-z"]).split(b"\0")
    if len(tracked) - 1 > MAX_INDEX_ENTRIES:
        raise ExportError("Source index contains too many paths for a bounded release review")
    for raw in tracked:
        if not raw:
            continue
        metadata, name = raw.split(b"\t", 1)
        relative = _relative(os.fsdecode(name))
        mode, _, stage = metadata.split()
        if mode == b"120000":
            raise ExportError("Source contains a tracked symlink: " + relative)
        if mode == b"160000" or stage != b"0":
            raise ExportError("Source contains a submodule/unmerged path: " + relative)
        _reject_links(source, relative)
    listed = _git(source, ["ls-files", "--cached", "--others", "--exclude-standard", "-z"]).split(b"\0")
    if len(listed) - 1 > MAX_INDEX_ENTRIES:
        raise ExportError("Source working tree contains too many paths for a bounded release review")
    names = sorted({_relative(os.fsdecode(name)) for name in listed if name})
    candidates = [name for name in names if _selected(name)]
    if len(candidates) > MAX_FILES:
        raise ExportError(f"Export exceeds {MAX_FILES} file safety limit")
    ignored_raw = _git(source, ["check-ignore", "--no-index", "--stdin", "-z"],
                       input_bytes=b"\0".join(os.fsencode(name) for name in candidates) + b"\0",
                       codes=(0, 1)) if candidates else b""
    ignored = {os.fsdecode(name) for name in ignored_raw.split(b"\0") if name}
    files, omitted, warnings = [], [], []
    total = 0
    for name in names:
        if not _selected(name) or name in ignored:
            omitted.append(name)
            continue
        try:
            file = _read(source, name)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ExportError("Cannot read regular non-symlink source payload: " + name) from exc
        total += len(file.data)
        if total > MAX_TOTAL_BYTES:
            raise ExportError(f"Export exceeds {MAX_TOTAL_BYTES} byte safety limit")
        files.append(file)
    missing = sorted(REQUIRED - {file.path for file in files})
    if missing:
        raise ExportError("Required release sources are missing/ignored: " + ", ".join(missing)
                          + "; finish the release materials before exporting (no placeholders)")
    _validate(files, warnings)
    return Plan(files, sorted(set(warnings)), omitted)


def _destination(source: Path, destination: Path) -> tuple[Path, Path]:
    source, destination = _absolute(source), _absolute(destination)
    if destination == source or source in destination.parents or destination in source.parents:
        raise ExportError("Destination must be outside the source checkout and cannot contain it")
    fd = _directory(destination)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise ExportError("Destination must be owned by the current user and not writable by others")
        if os.listdir(fd):
            raise ExportError("Destination must be empty, including hidden files")
    finally:
        os.close(fd)
    inventory = destination.with_name(destination.name + ".inventory.json")
    if inventory.exists() or inventory.is_symlink():
        raise ExportError("Inventory path already exists; choose a fresh destination: " + inventory.name)
    return destination, inventory


def export(source: Path, destination: Path, *, write: bool = False) -> dict:
    source = _absolute(source)
    destination, inventory_path = _destination(source, destination)
    plan = prepare(source)
    inventory = plan.inventory()
    if not write:
        return inventory
    root_fd = _directory(destination)
    report_fd = _directory(inventory_path.parent)
    created_files, created_directories = [], []

    def parent_for(parts: tuple[str, ...], *, create: bool = False) -> int:
        fd = os.dup(root_fd)
        try:
            prefix = []
            for part in parts:
                prefix.append(part)
                if create:
                    try:
                        os.mkdir(part, 0o755, dir_fd=fd)
                        created_directories.append(tuple(prefix))
                    except FileExistsError:
                        pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            return fd
        except BaseException:
            os.close(fd)
            raise

    try:
        if os.listdir(root_fd):
            raise ExportError("Destination changed after preflight; it must remain empty")
        for file in plan.files:
            parts = PurePosixPath(file.path).parts
            parent_fd = parent_for(parts[:-1], create=True)
            try:
                fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             file.mode, dir_fd=parent_fd)
                created_files.append(parts)
            finally:
                os.close(parent_fd)
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), file.mode)
                handle.write(file.data)
                handle.flush()
                os.fsync(handle.fileno())
        check_fd = _directory(destination)
        try:
            current, original = os.fstat(check_fd), os.fstat(root_fd)
            if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
                raise ExportError("Destination was replaced during export; refusing a misleading inventory")
        finally:
            os.close(check_fd)
        encoded = (json.dumps(inventory, indent=2, sort_keys=True) + "\n").encode("utf-8")
        fd, temporary = tempfile.mkstemp(prefix="." + inventory_path.name + ".", dir=f"/proc/self/fd/{report_fd}")
        temporary_name = Path(temporary).name
        try:
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            # Hard-link publication is atomic and fails if someone reserved
            # the sibling name; it never replaces another user's report.
            os.link(temporary_name, inventory_path.name, src_dir_fd=report_fd, dst_dir_fd=report_fd,
                    follow_symlinks=False)
        finally:
            os.unlink(temporary_name, dir_fd=report_fd)
    except BaseException:
        for parts in reversed(created_files):
            fd = parent_for(parts[:-1])
            try:
                os.unlink(parts[-1], dir_fd=fd)
            finally:
                os.close(fd)
        for parts in reversed(created_directories):
            fd = parent_for(parts[:-1])
            try:
                os.rmdir(parts[-1], dir_fd=fd)
            finally:
                os.close(fd)
        raise
    finally:
        os.close(root_fd)
        os.close(report_fd)
    return inventory


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).absolute().parents[1])
    parser.add_argument("--destination", type=Path, required=True, help="existing empty external directory")
    parser.add_argument("--write", action="store_true", help="copy reviewed payload; default is read-only dry-run")
    options = parser.parse_args()
    try:
        inventory = export(options.source, options.destination, write=options.write)
    except (OSError, ValueError) as exc:
        parser.exit(1, "Release export: " + str(exc) + "\n")
    print(f"{'EXPORTED' if options.write else 'READY (dry-run)'}: {inventory['file_count']} files, "
          f"{inventory['total_bytes']} bytes, payload SHA256 {inventory['payload_sha256']}")
    for warning in inventory["warnings"]:
        print("Review: " + warning)
    if options.write:
        print("Inventory: " + str(options.destination.with_name(options.destination.name + ".inventory.json")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
