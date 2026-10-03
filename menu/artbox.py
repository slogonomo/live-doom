#!/usr/bin/env python3
# SPDX-License-Identifier: 0BSD
"""Bounding box of the visible pixels in a PNG, for showing a Steam library logo without
its transparent padding (the menu clips to it with Image.sourceClipRect).

Reads the image in place; writes nothing and copies nothing. Standard library only.

Usage: artbox.py <png>   ->  {"width": W, "height": H, "x": X, "y": Y, "w": BW, "h": BH}
Images without alpha (or that cannot be read here) report the whole image or fail cleanly.
"""
from __future__ import annotations

import json
import os
import stat
import struct
import sys
import zlib
from pathlib import Path

MAX_BYTES = 16 << 20
MAX_SIDE = 4096
ALPHA_MIN = 24              # ignore faint anti-alias haze at the edges


def _unfilter(raw: bytes, width: int, height: int, bpp: int) -> list[bytearray]:
    stride = width * bpp
    rows, prev = [], bytearray(stride)
    pos = 0
    for _ in range(height):
        kind = raw[pos]
        line = bytearray(raw[pos + 1:pos + 1 + stride])
        pos += 1 + stride
        if kind == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif kind == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif kind == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif kind == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 0xFF
        elif kind != 0:
            raise ValueError("bad PNG filter")
        rows.append(line)
        prev = line
    return rows


def read_bounded(path: Path) -> bytes:
    """The file itself (never a symlink, FIFO or device), and at most MAX_BYTES of it."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_BYTES:
            raise ValueError("not a regular file of at most 16 MiB")
        chunks, left = [], MAX_BYTES + 1
        while left > 0:
            chunk = os.read(fd, min(left, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        data = b"".join(chunks)
    finally:
        os.close(fd)
    if len(data) > MAX_BYTES:
        raise ValueError("file grew past 16 MiB")
    return data


def box(path: Path) -> dict:
    data = read_bounded(path)
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat, ihdr = 8, [], None
    while pos + 8 <= len(data):
        length, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + length]
        if kind == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
        pos += 12 + length
    if ihdr is None:
        raise ValueError("no IHDR")
    width, height, depth, color, _, _, interlace = ihdr
    if not (0 < width <= MAX_SIDE and 0 < height <= MAX_SIDE):
        raise ValueError("image too large")
    whole = {"width": width, "height": height, "x": 0, "y": 0, "w": width, "h": height}
    channels = {6: 4, 4: 2}.get(color)
    if channels is None or depth != 8 or interlace:
        return whole                          # no alpha channel (or unusual format): use it all
    # Inflate no more than the image can need: a small file cannot expand into gigabytes.
    need = height * (width * channels + 1)
    z = zlib.decompressobj()
    raw = z.decompress(b"".join(idat), need)
    if len(raw) < need or z.unconsumed_tail:
        raise ValueError("image data does not match its size")
    rows = _unfilter(raw, width, height, channels)
    a = channels - 1
    x0, y0, x1, y1 = width, height, -1, -1
    for y, line in enumerate(rows):
        alpha = line[a::channels]
        hits = [x for x, v in enumerate(alpha) if v >= ALPHA_MIN]
        if hits:
            y0, y1 = min(y0, y), y
            x0, x1 = min(x0, hits[0]), max(x1, hits[-1])
    if x1 < 0:
        return whole
    return {"width": width, "height": height, "x": x0, "y": y0, "w": x1 - x0 + 1, "h": y1 - y0 + 1}


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        print(json.dumps(box(Path(argv[0]))))
        return 0
    except (OSError, ValueError, zlib.error, struct.error, MemoryError) as e:
        print(f"artbox: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
