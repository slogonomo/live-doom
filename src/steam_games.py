# SPDX-License-Identifier: 0BSD
"""Read-only discovery of recognized Doom files in local Steam libraries.

Only four known app manifests, finite install layouts and recognized WAD names
are inspected. There is no recursive game/Proton/workshop search, game launch,
copy, download or cache write. Explicit ``home`` keeps fixture scans isolated.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
import struct

MAX_VDF_BYTES = 1024 * 1024
MAX_VDF_TOKENS = 65536
MAX_VDF_DEPTH = 16
MAX_LIBRARIES = 32
MAX_LIBRARY_PATHS = 64
MAX_DIRECTORY_ENTRIES = 1024
MAX_WAD_LUMPS = 65536
APP_IDS = (2280, 2300, 2290, 9160)
APP_TITLES = {2280: "DOOM", 2300: "DOOM II", 2290: "Final DOOM", 9160: "Master Levels for DOOM II"}
MASTER_LEVELS = {
    "attack": ("Attack", "MAP01"), "blacktwr": ("Black Tower", "MAP25"),
    "bloodsea": ("Bloodsea Keep", "MAP07"), "canyon": ("Canyon", "MAP01"),
    "catwalk": ("The Catwalk", "MAP01"), "combine": ("The Combine", "MAP01"),
    "fistula": ("The Fistula", "MAP01"), "garrison": ("The Garrison", "MAP01"),
    "geryon": ("Geryon", "MAP08"), "manor": ("Titan Manor", "MAP01"),
    "mephisto": ("Mephisto's Maosoleum", "MAP07"), "minos": ("Minos' Judgement", "MAP05"),
    "nessus": ("Nessus", "MAP07"), "paradox": ("Paradox", "MAP01"),
    "subspace": ("Subspace", "MAP01"), "subterra": ("Subterra", "MAP01"),
    "teeth": ("The Express Elevator to Hell", "MAP31"), "ttrap": ("Trapped on Titan", "MAP01"),
    "vesperas": ("Vesperas", "MAP09"), "virgil": ("Virgil's Lead", "MAP03"),
}
TITLES = {"doom": "DOOM", "doom2": "DOOM II", "nerve": "No Rest for the Living",
          "master": "Master Levels for DOOM II", "tnt": "TNT: Evilution",
          "plutonia": "The Plutonia Experiment", "sigil": "SIGIL", "sigil2": "SIGIL II",
          "legacy-rust": "Legacy of Rust"}
START_MAPS = {"doom": "E1M1", "doom2": "MAP01", "nerve": "MAP01", "master": "MAP01",
              "tnt": "MAP01", "plutonia": "MAP01", "sigil": "E5M1", "sigil2": "E6M1",
              "legacy-rust": "MAP01"}
ORDER = ("doom", "doom2", "nerve", "master", "tnt", "plutonia", "sigil", "sigil2", "legacy-rust")
# Supported expansion rows require the controller's original tiny campaign
# metadata adapter, verified with the pinned native engine's map/boundary tests.
COMPATIBILITY = {"nerve": (True, ""),
                 "master": (True, ""),
                 "master-individual": (True, ""),
                 "sigil": (True, ""),
                 "sigil2": (True, ""),
                 "legacy-rust": (False, "Requires unsupported ID24")}


class VDFError(ValueError):
    pass


def parse_vdf(text: str) -> dict:
    """Parse bounded Valve KeyValues text without dependencies or execution."""
    if not isinstance(text, str) or len(text) > MAX_VDF_BYTES:
        raise VDFError("VDF exceeds the size limit")
    tokens: list[tuple[str, str]] = []
    position, length = 0, len(text)
    escapes = {'"': '"', "\\": "\\", "n": "\n", "r": "\r", "t": "\t"}
    while position < length:
        char = text[position]
        if char.isspace() or (position == 0 and char == "\ufeff"):
            position += 1
            continue
        if text.startswith("//", position):
            end = text.find("\n", position + 2)
            position = length if end == -1 else end + 1
            continue
        if char in "{}":
            tokens.append((char, char))
            position += 1
        elif char == '"':
            position += 1
            value = []
            while position < length and text[position] != '"':
                char = text[position]
                if char == "\\":
                    position += 1
                    if position >= length or text[position] not in escapes:
                        raise VDFError("Invalid VDF escape")
                    char = escapes[text[position]]
                if char == "\0":
                    raise VDFError("NUL in VDF string")
                value.append(char)
                position += 1
            if position >= length:
                raise VDFError("Unterminated VDF string")
            position += 1
            tokens.append(("string", "".join(value)))
        else:
            start = position
            while position < length and not text[position].isspace() and text[position] not in '{}"':
                if text.startswith("//", position):
                    break
                position += 1
            if start == position or "\0" in text[start:position]:
                raise VDFError("Invalid VDF token")
            tokens.append(("string", text[start:position]))
        if len(tokens) > MAX_VDF_TOKENS:
            raise VDFError("VDF exceeds the token limit")

    cursor = 0

    def object_value(depth: int, nested: bool) -> dict:
        nonlocal cursor
        if depth > MAX_VDF_DEPTH:
            raise VDFError("VDF exceeds the nesting limit")
        result = {}
        while cursor < len(tokens):
            kind, name = tokens[cursor]
            if kind == "}":
                if not nested:
                    raise VDFError("Unexpected VDF closing brace")
                cursor += 1
                return result
            if kind != "string" or not name or name in result:
                raise VDFError("Invalid or duplicate VDF key")
            cursor += 1
            if cursor >= len(tokens):
                raise VDFError("Missing VDF value")
            kind, value = tokens[cursor]
            cursor += 1
            if kind == "{":
                result[name] = object_value(depth + 1, True)
            elif kind == "string":
                result[name] = value
            else:
                raise VDFError("Missing VDF value")
        if nested:
            raise VDFError("Unterminated VDF object")
        return result

    return object_value(0, False)


def _value(data: dict, key: str, default=None):
    return next((value for name, value in data.items() if name.casefold() == key.casefold()), default)


@dataclass(frozen=True)
class Install:
    directory: Path
    appid: int
    title: str


@dataclass(frozen=True)
class Wad:
    path: Path
    magic: bytes
    maps: frozenset[str]


@dataclass(frozen=True)
class Candidate:
    kind: str
    wad: Wad
    install: Install
    edition: str
    rank: int


class Scanner:
    def __init__(self, home: Path):
        self.home = home
        self.warnings: list[str] = []
        self.scanned: list[str] = []
        self._seen_paths: set[str] = set()
        self._directories: dict[Path, dict[str, Path]] = {}
        self._wads: dict[tuple[int, int], Wad | None] = {}
        self._artwork: dict[int, dict[str, str]] = {}
        self.libraries: list[Path] = []
        self.candidates: dict[str, list[Candidate]] = {}
        self.steam_found = False

    def record(self, path: Path):
        text = str(path)
        if text not in self._seen_paths:
            self._seen_paths.add(text)
            self.scanned.append(text)

    def warn(self, message: str):
        if message not in self.warnings:
            self.warnings.append(message)

    def vdf(self, path: Path) -> dict | None:
        self.record(path)
        try:
            with path.open("rb") as stream:
                data = stream.read(MAX_VDF_BYTES + 1)
            if len(data) > MAX_VDF_BYTES:
                raise VDFError("VDF exceeds the size limit")
            return parse_vdf(data.decode("utf-8-sig"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, VDFError) as exc:
            self.warn(f"Unable to read Steam metadata {path}: {exc}")
            return None

    def roots(self):
        names = (".local/share/Steam", ".steam/steam", ".steam/root", ".steam/debian-installation",
                 ".var/app/com.valvesoftware.Steam/.local/share/Steam",
                 ".var/app/com.valvesoftware.Steam/data/Steam",
                 ".var/app/com.valvesoftware.Steam/.steam/steam",
                 ".var/app/com.valvesoftware.Steam/.steam/root")
        pending = [(self.home / name, False) for name in names]
        seen, attempted = set(), set()
        while pending and len(self.libraries) < MAX_LIBRARIES and len(attempted) < MAX_LIBRARY_PATHS:
            path, referenced = pending.pop(0)
            if path in attempted:
                continue
            attempted.add(path)
            self.record(path)
            try:
                real = path.resolve(strict=True)
                if not real.is_dir():
                    raise OSError("not a directory")
            except (OSError, RuntimeError) as exc:
                if referenced:
                    self.warn(f"Steam library unavailable {path}: {exc}")
                continue
            if real in seen:
                continue
            seen.add(real)
            self.libraries.append(real)
            self.steam_found = True
            for relative in ("steamapps/libraryfolders.vdf", "config/libraryfolders.vdf"):
                data = self.vdf(real / relative)
                libraries = _value(data or {}, "libraryfolders", {})
                if not isinstance(libraries, dict):
                    self.warn(f"Invalid Steam library list: {real / relative}")
                    continue
                for key, entry in libraries.items():
                    if not key.isdecimal():
                        continue
                    value = _value(entry, "path") if isinstance(entry, dict) else entry
                    if not isinstance(value, str) or not value or "\0" in value or not Path(value).is_absolute():
                        self.warn(f"Invalid Steam library path in {real / relative}")
                        continue
                    pending.append((Path(value), True))
        if pending:
            self.warn(f"Steam library scan limited to {MAX_LIBRARIES} roots/{MAX_LIBRARY_PATHS} paths")

    def installs(self) -> list[Install]:
        result, seen = [], set()
        for library in self.libraries:
            for appid in APP_IDS:
                path = library / "steamapps" / f"appmanifest_{appid}.acf"
                data = self.vdf(path)
                if data is None:
                    continue
                app = _value(data, "AppState", {})
                if not isinstance(app, dict) or _value(app, "appid") != str(appid):
                    self.warn(f"Invalid Steam app manifest: {path}")
                    continue
                folder = _value(app, "installdir")
                if (not isinstance(folder, str) or not folder or folder in (".", "..")
                        or any(c in folder for c in ("/", "\\", "\0", "\n", "\r"))):
                    self.warn(f"Invalid Steam install directory: {path}")
                    continue
                directory = library / "steamapps/common" / folder
                self.record(directory)
                try:
                    directory = directory.resolve(strict=True)
                    if not directory.is_dir():
                        raise OSError("not a directory")
                except (OSError, RuntimeError) as exc:
                    self.warn(f"Steam game install unavailable {directory}: {exc}")
                    continue
                key = (directory, appid)
                if key in seen:
                    continue
                seen.add(key)
                title = _value(app, "name", APP_TITLES[appid])
                if not isinstance(title, str) or not title.strip() or any(c in title for c in ("\0", "\n", "\r")):
                    title = APP_TITLES[appid]
                result.append(Install(directory, appid, title))
        return result

    def case_path(self, directory: Path, relative: str) -> Path | None:
        path = directory
        for component in relative.split("/"):
            exact = path / component
            if exact.exists():
                path = exact
                continue
            if path not in self._directories:
                entries = {}
                try:
                    with os.scandir(path) as stream:
                        for index, entry in enumerate(stream):
                            if index >= MAX_DIRECTORY_ENTRIES:
                                self.warn(f"Steam directory scan truncated: {path}")
                                break
                            entries.setdefault(entry.name.casefold(), Path(entry.path))
                except (FileNotFoundError, NotADirectoryError):
                    pass
                except OSError as exc:
                    self.warn(f"Steam directory unavailable {path}: {exc}")
                self._directories[path] = entries
            path = self._directories[path].get(component.casefold())
            if path is None:
                return None
        return path

    def wad(self, path: Path) -> Wad | None:
        self.record(path)
        try:
            path = path.resolve(strict=True)
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("not a regular WAD file")
            identity = info.st_dev, info.st_ino
            if identity in self._wads:
                return self._wads[identity]
            with path.open("rb") as stream:
                header = stream.read(12)
                if len(header) != 12:
                    raise ValueError("truncated WAD header")
                magic, count, offset = struct.unpack("<4sii", header)
                if magic not in (b"IWAD", b"PWAD") or not 1 <= count <= MAX_WAD_LUMPS or offset < 12 or offset + count * 16 > info.st_size:
                    raise ValueError("invalid WAD directory")
                stream.seek(offset)
                entries = stream.read(count * 16)
                if len(entries) != count * 16:
                    raise ValueError("truncated WAD directory")
            maps = set()
            for position, size, raw_name in struct.iter_unpack("<ii8s", entries):
                if position < 0 or size < 0 or position + size > info.st_size:
                    raise ValueError("truncated WAD lump")
                name = raw_name.split(b"\0", 1)[0].decode("ascii", errors="replace").upper()
                if re.fullmatch(r"E[1-9]M[1-9]|MAP[0-9]{2}", name):
                    maps.add(name)
            result = Wad(path, magic, frozenset(maps))
            self._wads[identity] = result
            return result
        except (OSError, RuntimeError, ValueError, struct.error) as exc:
            self.warn(f"Steam WAD unavailable {path}: {exc}")
            return None

    def candidate(self, kind: str, install: Install, relative: str, rank: int):
        self.record(install.directory / relative)
        path = self.case_path(install.directory, relative)
        if path is None:
            return
        wad = self.wad(path)
        if wad is not None:
            edition = "rerelease" if relative.startswith("rerelease/") else "original"
            candidate = Candidate(kind, wad, install, edition, rank)
            bucket = self.candidates.setdefault(kind, [])
            if not any(item.wad.path == wad.path for item in bucket):
                bucket.append(candidate)

    def collect(self):
        for install in self.installs():
            base = ("doom", "doom2", "tnt", "plutonia") if install.appid == 2280 else ("doom2",) if install.appid == 2300 else ("tnt", "plutonia") if install.appid == 2290 else ("doom2",)
            for kind in base:
                nested = f"base/{kind}/{kind}.wad"
                for rank, relative in enumerate((f"base/{kind}.wad", nested, f"{kind}/{kind}.wad", f"{kind}.wad")):
                    self.candidate(kind, install, relative, rank)
                self.candidate(kind, install, f"rerelease/{kind}.wad", 100)
            if install.appid in (2280, 2300):
                for kind in ("nerve", "sigil", "sigil2", "master", "legacy-rust"):
                    filename = "masterlevels" if kind == "master" else "id1" if kind == "legacy-rust" else kind
                    for rank, relative in enumerate((f"base/{filename}.wad", f"{filename}.wad", f"rerelease/{filename}.wad")):
                        self.candidate(kind, install, relative, 100 if relative.startswith("rerelease/") else rank)
            if install.appid in (2280, 9160):
                for level in MASTER_LEVELS:
                    for rank, prefix in enumerate(("base/master/wads", "master/wads", "base/wads", "wads")):
                        self.candidate("master-" + level, install, f"{prefix}/{level}.wad", rank)

    def artwork(self, appid: int) -> dict[str, str]:
        if appid in self._artwork:
            return dict(self._artwork[appid])
        found = {}
        names = {"logo": ("logo.png",), "cover": ("library_600x900.jpg", "library_600x900.png"),
                 "hero": ("library_hero.jpg", "library_hero.png")}
        for library in self.libraries:
            folder = library / "appcache/librarycache" / str(appid)
            self.record(folder)
            directories = [folder]
            try:
                with os.scandir(folder) as stream:
                    # Only recognized hash folders one level below this app.
                    for index, entry in enumerate(stream):
                        if index >= 64:
                            break
                        if re.fullmatch(r"[a-fA-F0-9]{16,64}", entry.name) and entry.is_dir():
                            directories.append(Path(entry.path))
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError as exc:
                self.warn(f"Steam artwork unavailable {folder}: {exc}")
                continue
            for directory in directories:
                for kind, options in names.items():
                    if kind in found:
                        continue
                    for name in options:
                        path = directory / name
                        self.record(path)
                        try:
                            if path.is_file() and path.stat().st_size > 0:
                                found[kind] = str(path.resolve(strict=True))
                                break
                        except (OSError, RuntimeError):
                            continue
            if len(found) == len(names):
                break
        self._artwork[appid] = found
        return dict(found)

    def packages(self) -> list[dict]:
        def quality(item: Candidate):
            iwad = item.kind in ("doom", "doom2", "tnt", "plutonia")
            start = MASTER_LEVELS[item.kind[7:]][1] if item.kind.startswith("master-") else START_MAPS[item.kind]
            invalid = item.wad.magic != (b"IWAD" if iwad else b"PWAD") or start not in item.wad.maps
            return invalid, item.rank, str(item.wad.path)

        selected = {kind: min(items, key=quality) for kind, items in self.candidates.items()}
        result = []
        kinds = list(ORDER[:4]) + ["master-" + level for level in MASTER_LEVELS] + list(ORDER[4:])
        for kind in kinds:
            item = selected.get(kind)
            if item is None:
                continue
            individual = kind.startswith("master-")
            aggregate = selected.get("master")
            if (individual and aggregate is not None and COMPATIBILITY["master"][0]
                    and aggregate.wad.magic == b"PWAD" and "MAP01" in aggregate.wad.maps):
                continue
            is_iwad = kind in ("doom", "doom2", "tnt", "plutonia")
            base = item if is_iwad else selected.get("doom" if kind in ("sigil", "sigil2") else "doom2")
            if not is_iwad:
                base_kind = "doom" if kind in ("sigil", "sigil2") else "doom2"
                required = {START_MAPS[base_kind]} | ({"E2M1"} if base_kind == "doom" else set())
                valid_bases = [candidate for candidate in self.candidates.get(base_kind, [])
                               if candidate.wad.magic == b"IWAD" and required <= candidate.wad.maps]
                if valid_bases:
                    base = min(valid_bases, key=quality)
            title, start = MASTER_LEVELS[kind[7:]] if individual else (TITLES[kind], START_MAPS[kind])
            title = "Master Levels: " + title if individual else title
            compatible, reason = COMPATIBILITY.get("master-individual" if individual else kind, (True, ""))
            if not is_iwad and base is None:
                compatible, reason = False, "Requires DOOM" if kind in ("sigil", "sigil2") else "Requires DOOM II"
            elif base is not None and base.wad.magic != b"IWAD":
                compatible, reason = False, "Invalid base IWAD"
            elif not is_iwad and base is not None and START_MAPS[base.kind] not in base.wad.maps:
                compatible, reason = False, "Missing base start map"
            elif kind in ("sigil", "sigil2") and base is not None and "E2M1" not in base.wad.maps:
                compatible, reason = False, "Requires full DOOM"
            elif item.wad.magic != (b"IWAD" if is_iwad else b"PWAD"):
                compatible, reason = False, "Invalid WAD type"
            elif start not in item.wad.maps:
                compatible, reason = False, "Missing start map"
            source = {"title": item.install.title, "appid": item.install.appid,
                      "artwork": self.artwork(item.install.appid)}
            package_id = "steam-masterlevels" if kind == "master" else "steam-" + kind
            result.append({"id": package_id, "title": title, "appid": item.install.appid,
                           "source": source, "edition": item.edition,
                           "iwad": str(base.wad.path) if base is not None else None,
                           "pwads": [] if is_iwad else [str(item.wad.path)], "start_map": start,
                           "compatible": compatible, "reason": reason})
        return result


def discover(home: Path | str | None = None) -> dict:
    """Return JSON-ready packages, diagnostics and exact paths inspected."""
    scanner = Scanner(Path.home() if home is None else Path(home))
    scanner.roots()
    scanner.collect()
    packages = scanner.packages()
    compatible = sum(package["compatible"] for package in packages)
    summary = (f"Found {compatible} playable Doom packages in Steam" if packages else
               "No Doom WADs found in Steam" if scanner.steam_found else "Steam was not found")
    return {"packages": packages, "warnings": scanner.warnings, "scanned_paths": scanner.scanned,
            "steam_found": scanner.steam_found, "summary": summary}
