#!/usr/bin/env python3
# SPDX-License-Identifier: 0BSD
"""Extract Doom-style glyphs from the user's own IWAD for the Live Doom menu.

Nothing from id Software is redistributed: the menu ships no font. At runtime this
reads the STCFN (status/console font) and a few menu patches from whichever IWAD the
game is configured to use, and caches them as PNGs keyed by the IWAD's checksum.
Freedoom's equivalents are BSD-licensed, so a Freedoom IWAD works the same way.

Usage:
    wadfont.py export <iwad> <cache-root>   # prints the cache directory as JSON
    wadfont.py palette <iwad>               # prints PLAYPAL[0] as JSON hex list
Pure standard library (zlib PNG writer), so it runs anywhere python3 does.
"""
from __future__ import annotations

import hashlib
import json
import mmap
import os
import re
import stat
import struct
import sys
import tempfile
import zlib
from pathlib import Path

GLYPHS = range(33, 96)          # STCFN033 '!' .. STCFN095 '_'
MENU_PATCHES = ("M_SKULL1", "M_SKULL2", "M_DOOM", "M_THERMM", "M_THERML", "M_THERMR", "M_THERMO")
# The shell keeps every exported image decoded, so each one is capped by what it is.
# Shipped IWADs: glyphs at most 9x8, M_DOOM at most 159x60, skull and thermometer 20x19.
GLYPH_MAX = (32, 32)
PATCH_MAX = {"M_DOOM": (320, 200)}  # the title logo fits the screen it was drawn for
PATCH_DEFAULT_MAX = (64, 64)
MAX_TOTAL_PIXELS = 1 << 18      # the whole set stays under 1 MiB decoded, whatever the WAD says
WANTED = {"PLAYPAL", *(f"STCFN{code:03d}" for code in GLYPHS), *MENU_PATCHES}
CACHE_VERSION = 3
MAX_INDEX_BYTES = 1 << 20                       # the cached font.json is a few KiB
CACHED_NAME = re.compile(r"[a-z0-9_]{1,16}\.png")    # used with fullmatch


class Wad:
    def __init__(self, path: Path):
        self.path = Path(path)
        # Map the IWAD instead of reading it whole: only a few lumps are ever touched.
        with open(self.path, "rb") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                raise ValueError(f"{path} is not a regular file")
            data = mmap.mmap(f.fileno(), 0, prot=mmap.PROT_READ)
        magic, count, offset = struct.unpack_from("<4sii", data, 0)
        if magic not in (b"IWAD", b"PWAD"):
            raise ValueError(f"{path} is not a WAD")
        if count < 0 or offset < 12 or offset + 16 * count > len(data):
            raise ValueError(f"{path} has a bad lump directory")
        self.data = data
        self.lumps: dict[str, tuple[int, int]] = {}
        self._palette = None
        for i in range(count):
            pos, size, raw = struct.unpack_from("<ii8s", data, offset + 16 * i)
            name = raw.split(b"\0", 1)[0].decode("ascii", "replace").upper()
            if name in WANTED:               # only what the menu reads, however long the directory is
                self.lumps[name] = (pos, size)   # later lumps win, like the engine

    def lump(self, name: str, limit: int | None = None) -> bytes | None:
        """A lump's bytes, or only its first `limit` bytes."""
        entry = self.lumps.get(name.upper())
        if not entry:
            return None
        pos, size = entry
        if pos < 0 or size < 0 or pos + size > len(self.data):
            return None
        return self.data[pos:pos + (size if limit is None else min(size, limit))]

    def palette(self) -> list[tuple[int, int, int]]:
        if self._palette is None:
            raw = self.lump("PLAYPAL", 768)
            if not raw or len(raw) < 768:
                raise ValueError("IWAD has no PLAYPAL")
            self._palette = [tuple(raw[i:i + 3]) for i in range(0, 768, 3)]
        return self._palette

    def picture(self, name: str, max_size: tuple[int, int]):
        """Decode a Doom patch no larger than max_size → (width, height, left, top, rgba bytes) or None."""
        head = self.lump(name, 8)
        if not head or len(head) < 8:
            return None
        width, height, left, top = struct.unpack_from("<HHhh", head, 0)
        if not (0 < width <= max_size[0] and 0 < height <= max_size[1]):
            return None
        # At most a post per pixel (4 bytes of framing each) and an end marker per column, plus slack.
        limit = 8 + 4 * width + width * (5 * height + 1) + 1024
        if self.lumps[name.upper()][1] > limit:
            return None
        raw = self.lump(name, limit)
        if len(raw) < 8 + 4 * width:
            return None
        pal = self.palette()
        rgba = bytearray(width * height * 4)
        for x in range(width):
            (col,) = struct.unpack_from("<I", raw, 8 + 4 * x)
            for _ in range(height + 1):          # a column has no more posts than pixels
                if col >= len(raw):
                    break
                topdelta = raw[col]
                if topdelta == 0xFF:
                    break
                length = raw[col + 1]
                pixels = raw[col + 3:col + 3 + length]
                for i, index in enumerate(pixels):
                    y = topdelta + i
                    if 0 <= y < height:
                        r, g, b = pal[index]
                        o = (y * width + x) * 4
                        rgba[o:o + 4] = bytes((r, g, b, 255))
                col += length + 4
        return width, height, left, top, bytes(rgba)


def write_png(path: Path, width: int, height: int, rgba: bytes) -> None:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))
    rows = b"".join(b"\0" + rgba[y * width * 4:(y + 1) * width * 4] for y in range(height))
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b"")
    atomic_write(path, png)


def atomic_write(path: Path, data: bytes) -> None:
    """Unique temp file in the same directory, then rename: concurrent exporters
    and interrupted writes never leave a truncated cache file behind."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def patch_max(name: str) -> tuple[int, int]:
    return PATCH_MAX.get(name, PATCH_DEFAULT_MAX)


def entry_fits(entry, max_size: tuple[int, int]) -> bool:
    w, h = entry.get("w"), entry.get("h")
    return (type(w) is int and type(h) is int and 0 < w <= max_size[0] and 0 < h <= max_size[1]
            and CACHED_NAME.fullmatch(str(entry.get("file", ""))) is not None)


def read_index(index_path: Path, out: Path):
    """The cached index, read without following symlinks and at most MAX_INDEX_BYTES, accepted
    only if it belongs to `out`, every file it names is a plain cache file name, and every image
    stays within the export caps."""
    try:
        fd = os.open(index_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_size > MAX_INDEX_BYTES:
            return None
        raw = os.read(fd, MAX_INDEX_BYTES + 1)
    finally:
        os.close(fd)
    try:
        cached = json.loads(raw)
    except ValueError:
        return None
    if not (isinstance(cached, dict) and cached.get("dir") == str(out) and isinstance(cached.get("glyphs"), dict)
            and isinstance(cached.get("patches", {}), dict)):
        return None
    sized = [(entry, GLYPH_MAX) for entry in cached["glyphs"].values()]
    sized += [(entry, patch_max(str(name))) for name, entry in cached.get("patches", {}).items()]
    if not all(isinstance(entry, dict) and entry_fits(entry, limit) for entry, limit in sized):
        return None
    if sum(entry["w"] * entry["h"] for entry, _ in sized) > MAX_TOTAL_PIXELS:
        return None
    line, space = cached.get("lineHeight", 8), cached.get("spaceWidth", 4)
    if not (type(line) is int and 0 < line <= GLYPH_MAX[1] and type(space) is int and 0 < space <= GLYPH_MAX[0]):
        return None
    return cached


def iwad_key(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()[:16]


def export(iwad: Path, cache_root: Path) -> dict:
    iwad = Path(iwad).expanduser().resolve()
    out = Path(cache_root).expanduser() / f"{iwad_key(iwad)}-v{CACHE_VERSION}"
    index_path = out / "font.json"
    cached = read_index(index_path, out)        # trust a cached index only if it is whole and ours
    if cached is not None:
        return cached
    wad = Wad(iwad)
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    budget = MAX_TOTAL_PIXELS
    glyphs = {}
    for code in GLYPHS:
        pic = wad.picture(f"STCFN{code:03d}", GLYPH_MAX)
        if not pic or pic[0] * pic[1] > budget:
            continue
        w, h, left, top, rgba = pic
        budget -= w * h
        write_png(out / f"g{code:03d}.png", w, h, rgba)
        glyphs[chr(code)] = {"file": f"g{code:03d}.png", "w": w, "h": h, "top": top}
    patches = {}
    for name in MENU_PATCHES:
        pic = wad.picture(name, patch_max(name))
        if pic and pic[0] * pic[1] <= budget:
            w, h, left, top, rgba = pic
            budget -= w * h
            write_png(out / f"{name.lower()}.png", w, h, rgba)
            patches[name] = {"file": f"{name.lower()}.png", "w": w, "h": h}
    index = {
        "dir": str(out),
        "iwad": str(iwad),
        "lineHeight": max((g["h"] for g in glyphs.values()), default=8),
        "spaceWidth": 4,
        "glyphs": glyphs,
        "patches": patches,
    }
    atomic_write(index_path, json.dumps(index).encode())   # written last: marks the cache complete
    return index


def main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[0] == "export":
        print(json.dumps(export(Path(argv[1]), Path(argv[2]))))
        return 0
    if len(argv) >= 2 and argv[0] == "palette":
        print(json.dumps(["#%02x%02x%02x" % c for c in Wad(Path(argv[1])).palette()]))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
