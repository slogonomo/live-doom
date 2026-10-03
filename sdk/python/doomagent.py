#!/usr/bin/env python3
# SPDX-License-Identifier: 0BSD
"""doomagent: drive the Doom live wallpaper from your own code.

An external agent sees what the wallpaper shows (the shared frame.bin: pixels plus game
state) and sends one action per decision; the engine applies the latest action every tic
until the next one. AutoDoom drives whenever no agent is connected, and a human player
always wins. Protocol and semantics: docs/design/EXTERNAL-AGENT.md.

    from doomagent import Agent
    with Agent("Wanderer") as agent:
        while agent.connected:
            obs = agent.next_observation(pixels=False)   # waits for the next world tic
            if obs and agent.driving:
                agent.act(move=1, turn=3 if obs.health < 50 else 0)

Standard library only; numpy is used for Observation.rgb() when installed.
Command line: `doomagent.py info` prints the current game state, and
`doomagent.py snapshot out.ppm` saves the current frame.
"""
from __future__ import annotations

import collections
import ctypes
import math
import mmap
import os
import socket
import stat
import sys
import threading
import time
from pathlib import Path

PROTOCOL = 1
MAGIC = 0x44444F4D
HEADER_AREA = 4096                      # pixels start here (DD_HEADER_SIZE)
MAX_WIDTH, MAX_HEIGHT = 3840, 2160
OWNERS = {0: "autodoom", 1: "agent", 2: "human"}
GS_LEVEL, GS_INTERMISSION, GS_FINALE, GS_DEMOSCREEN = 0, 1, 2, 3


class _HeaderV2(ctypes.Structure):
    """src/bridge.h protocol 2 (the fields every engine since v2 writes)."""
    _fields_ = [(n, ctypes.c_uint32) for n in ("magic", "version", "seq", "width", "height", "stride",
                                               "bot", "paused", "audible", "gamestate")]
    _fields_ += [("frame", ctypes.c_uint64), ("tic", ctypes.c_uint64)]
    _fields_ += [(n, ctypes.c_int32) for n in ("leveltime", "health", "x", "y", "kills", "items", "secrets")]
    _fields_ += [("map", ctypes.c_char * 16)]
    _fields_ += [(n, ctypes.c_uint32) for n in ("pid", "campaign", "audio_peak")]
    _fields_ += [("angle", ctypes.c_int32), ("pixel_aspect_num", ctypes.c_uint32), ("pixel_aspect_den", ctypes.c_uint32)]
    _fields_ += [(n, ctypes.c_int32) for n in ("fov", "view_width", "view_height", "hud_layout")]
    _fields_ += [("bot_replans", ctypes.c_uint32), ("render_fps", ctypes.c_uint32), ("render_lerp", ctypes.c_uint32),
                 ("render_angle", ctypes.c_int32), ("human_quits", ctypes.c_uint32)]


class _HeaderV3(ctypes.Structure):
    """Protocol 3 appends agent ownership and player telemetry; v2 offsets are unchanged."""
    _fields_ = list(_HeaderV2._fields_)
    _fields_ += [("owner", ctypes.c_uint32), ("agent_gen", ctypes.c_uint32)]
    _fields_ += [("armor", ctypes.c_int32), ("ammo", ctypes.c_int32 * 4), ("ready_weapon", ctypes.c_int32)]
    _fields_ += [("weapons_owned", ctypes.c_uint32), ("keys", ctypes.c_uint32)]
    _fields_ += [(n, ctypes.c_int32) for n in ("z", "momx", "momy", "damage_count")]
    _fields_ += [(n, ctypes.c_uint32) for n in ("deaths", "levels_completed", "bot_stuck")]


class AgentError(RuntimeError):
    """The game rejected a request (ERR ...) or the connection is gone."""


_SEQ = _HeaderV2.seq.offset
_HEADERS = {2: _HeaderV2, 3: _HeaderV3}


def runtime_dir() -> Path:
    """Same rule as the controller: $LIVE_DOOM_RUNTIME, else $XDG_RUNTIME_DIR/live-doom.
    There is no /tmp fallback: the game's sockets live only in the per-user runtime dir.
    (DOOM_DESKTOP_RUNTIME, the pre-release name, is still honoured.)"""
    for name in ("LIVE_DOOM_RUNTIME", "DOOM_DESKTOP_RUNTIME"):
        if os.environ.get(name):
            return Path(os.environ[name])
    base = os.environ.get("XDG_RUNTIME_DIR")
    if not base:
        raise AgentError("XDG_RUNTIME_DIR is not set; run inside your desktop session "
                         "(or set LIVE_DOOM_RUNTIME)")
    return Path(base) / "live-doom"


def check_private_dir(path: Path) -> None:
    """The game's runtime directory must be ours and closed to others (as the controller
    creates it: owner-only, mode 0700); refuse to talk to a socket or map a frame elsewhere."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return                                  # no game running: the caller reports that
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise AgentError(f"{path} is not a private directory owned by you (expected mode 0700)")


def default_frame_path() -> Path:
    return Path(os.environ.get("DOOM_AGENT_FRAME") or runtime_dir() / "frame.bin")


def default_socket_path() -> Path:
    return Path(os.environ.get("DOOM_AGENT_SOCKET") or runtime_dir() / "control.sock")


# ------------------------------------------------------------------ observation
class Observation:
    """One consistent snapshot: `header` (dict of every header field) and, unless the
    observation was taken with pixels=False, `data` (XRGB8888 rows: B, G, R, unused).
    `header["frame"]` is the image revision: it only advances when the pixels change, so a
    paused or still view keeps it while `tic` moves on. Key on `tic`, not `frame`."""

    __slots__ = ("header", "data", "width", "height", "stride")

    def __init__(self, header: dict, data: bytes | None):
        self.header, self.data = header, data
        self.width, self.height, self.stride = header["width"], header["height"], header["stride"]

    def __getattr__(self, name):            # obs.health, obs.map, obs.tic ...
        try:
            return self.header[name]
        except KeyError:
            raise AttributeError(name) from None

    @property
    def pos(self) -> tuple[float, float]:
        """Player position in map units (the header stores 16.16 fixed point)."""
        return self.header["x"] / 65536.0, self.header["y"] / 65536.0

    @property
    def z_units(self) -> float:
        return self.header.get("z", 0) / 65536.0

    @property
    def angle_deg(self) -> float:
        """Facing in degrees, 0 = east, counter-clockwise (Doom's binary angle)."""
        return (self.header["angle"] & 0xFFFFFFFF) * 360.0 / 2 ** 32

    @property
    def owner_name(self) -> str:
        return OWNERS.get(self.header.get("owner", 0), "unknown")

    @property
    def in_level(self) -> bool:
        return self.header["gamestate"] == GS_LEVEL

    @property
    def alive(self) -> bool:
        return self.header["health"] > 0

    def rgb(self, downscale: tuple[int, int] | None = None):
        """HxWx3 uint8 numpy array (needs numpy). downscale=(w, h) picks nearest pixels."""
        import numpy as np
        if self.data is None:
            raise ValueError("observation was taken without pixels")
        img = np.frombuffer(self.data, np.uint8).reshape(self.height, self.stride // 4, 4)[:, :self.width, 2::-1]
        if downscale:
            w, h = downscale
            img = img[(np.arange(h) * self.height // h)][:, (np.arange(w) * self.width // w)]
        return img

    def rgb_bytes(self) -> bytes:
        """Packed RGB rows without numpy (C-speed slicing)."""
        if self.data is None:
            raise ValueError("observation was taken without pixels")
        rows = self.data if self.stride == self.width * 4 else b"".join(
            self.data[y * self.stride:y * self.stride + self.width * 4] for y in range(self.height))
        out = bytearray(self.width * self.height * 3)
        out[0::3], out[1::3], out[2::3] = rows[2::4], rows[1::4], rows[0::4]
        return bytes(out)

    def save_ppm(self, path) -> None:
        Path(path).write_bytes(b"P6\n%d %d\n255\n" % (self.width, self.height) + self.rgb_bytes())


class FrameReader:
    """Lock-free reader for frame.bin. The engine brackets every publish with an odd
    sequence number; a copy counts only if the sequence was even and unchanged across it.
    Reopens the file when the engine restarts (new inode), and returns None while no
    game is loaded (the wallpaper is off and the game is unloaded)."""

    def __init__(self, path=None):
        self.path = Path(path) if path else default_frame_path()
        self._mm = None
        self._ino = None

    def close(self) -> None:
        if self._mm is not None:
            self._mm.close()
        self._mm = self._ino = None

    def _map(self) -> bool:
        try:
            st = os.stat(self.path)
        except OSError:
            self.close()
            return False
        if self._mm is not None and st.st_ino == self._ino and len(self._mm) == st.st_size:
            return True
        self.close()
        if st.st_size < HEADER_AREA:
            return False
        check_private_dir(self.path.parent)
        with open(self.path, "rb") as f:
            self._mm = mmap.mmap(f.fileno(), 0, prot=mmap.PROT_READ)
        self._ino = st.st_ino
        return True

    def read(self, pixels: bool = True, attempts: int = 200) -> Observation | None:
        if not self._map():
            return None
        mm = self._mm
        for _ in range(attempts):
            seq = int.from_bytes(mm[_SEQ:_SEQ + 4], "little")
            if seq & 1:
                time.sleep(0.0002)
                continue
            version = int.from_bytes(mm[4:8], "little")
            cls = _HEADERS.get(version)
            if int.from_bytes(mm[0:4], "little") != MAGIC or cls is None:
                return None
            raw = mm[0:ctypes.sizeof(cls)]
            h = cls.from_buffer_copy(raw)
            if not (0 < h.width <= MAX_WIDTH and 0 < h.height <= MAX_HEIGHT and h.stride >= h.width * 4
                    and HEADER_AREA + h.height * h.stride <= len(mm)):
                return None
            data = mm[HEADER_AREA:HEADER_AREA + h.height * h.stride] if pixels else None
            if int.from_bytes(mm[_SEQ:_SEQ + 4], "little") != seq:
                continue                    # a publish overlapped the copy: retry
            header = {}
            for name, _ in cls._fields_:
                value = getattr(h, name)
                header[name] = list(value) if name == "ammo" else value
            header["map"] = h.map.decode("ascii", "replace")
            return Observation(header, data)
        return None


# ------------------------------------------------------------------ control
class Busy(AgentError):
    """Another agent already holds control."""


_ACT_LIMITS = {"move": 1.0, "strafe": 1.0, "turn": 45.0}


def _num(v: float) -> str:
    s = f"{v:.4f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


class Agent:
    """One agent connection. The connection is the lease: while it stays open and
    something is sent at least every `ttl_ms` (a background heartbeat sends `ping`), the
    agent holds control whenever no human is playing. Closing it hands control back to
    AutoDoom.

    State, kept current from the game's EVENT lines:
      owner   "agent" (driving) | "waiting" (connected, someone else drives) |
              "human" | "autodoom" (you yielded) | "closed"
      paused  None, or why the world is paused: "busy", "lock", "asleep", "menu" ...
    """

    def __init__(self, name: str, socket_path=None, frame_path=None, heartbeat: float = 0.25,
                 connect_timeout: float = 5.0, strict: bool = True):
        if not name or any(c.isspace() for c in name):
            raise ValueError("agent name must be one word")
        self.name = name
        self.socket_path = Path(socket_path) if socket_path else default_socket_path()
        self.frames = FrameReader(frame_path)
        self.heartbeat = heartbeat
        self.connect_timeout = connect_timeout
        self.strict = strict
        self.owner, self.paused, self.map = "closed", None, None
        self.gen = self.ttl_ms = self.hold_ms = self.schema = None
        self.last_error = None
        self._sock = None
        self._send_lock = threading.Lock()
        self._state = threading.Condition()
        self._pending = collections.deque()      # one slot per request awaiting OK/ERR, FIFO
        self._events = collections.deque(maxlen=1024)
        self._errors = collections.deque()
        self._last_send = 0.0
        self._last_tic = None
        self._threads = []

    # -- connection ---------------------------------------------------------
    def __enter__(self):
        if self._sock is None:
            self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    @property
    def connected(self) -> bool:
        return self.owner != "closed"

    @property
    def driving(self) -> bool:
        """True while this agent's actions reach the game."""
        return self.owner == "agent" and self.paused is None

    def connect(self) -> "Agent":
        check_private_dir(self.socket_path.parent)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.connect_timeout)
        sock.connect(str(self.socket_path))
        sock.sendall(f"agent hello {self.name} {PROTOCOL}\n".encode())
        reply = self._read_first_line(sock)
        if reply.startswith("ERR"):
            sock.close()
            reason = reply[4:].strip()
            raise (Busy if reason.startswith("busy") else AgentError)(reason or "refused")
        fields = dict(kv.split("=", 1) for kv in reply.split()[1:] if "=" in kv)
        if not reply.startswith("OK") or "owner" not in fields:
            sock.close()
            raise AgentError(f"unexpected hello reply: {reply!r}")
        self.owner = fields["owner"]
        self.gen = int(fields.get("gen", 0))
        self.ttl_ms = int(fields.get("ttl_ms", 1000))
        self.hold_ms = int(fields.get("hold_ms", 500))
        self.schema = int(fields.get("schema", 0))
        self.paused = None if fields.get("paused", "none") == "none" else fields["paused"]
        sock.settimeout(None)
        self._sock = sock
        self._last_send = time.monotonic()
        for target in (self._reader, self._pinger):
            t = threading.Thread(target=target, name=f"doomagent-{target.__name__}", daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def _read_first_line(self, sock) -> str:
        self._buf = b""
        while b"\n" not in self._buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise AgentError("the game closed the connection")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line.decode(errors="replace").strip()

    def close(self) -> None:
        """Release control (`bye`) and disconnect. AutoDoom resumes at once."""
        sock, self._sock = self._sock, None
        if sock is None:
            return
        try:
            with self._send_lock:
                sock.sendall(b"bye\n")
        except OSError:
            pass
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()
        self._closed("closed by agent")

    def _closed(self, why: str) -> None:
        with self._state:
            self.owner = "closed"
            while self._pending:
                slot = self._pending.popleft()
                if slot is not None:
                    slot["reply"] = f"ERR {why}"
            self._events.append(("closed", why))
            self._state.notify_all()

    # -- background threads -------------------------------------------------
    def _reader(self) -> None:
        sock, buf = self._sock, self._buf
        why = "connection lost"
        try:
            while True:
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self._line(line.decode(errors="replace").strip())
                chunk = sock.recv(65536)
                if not chunk:
                    why = "the game closed the connection"
                    break
                buf += chunk
        except OSError:
            pass
        if self._sock is sock:
            self._sock = None
            try:
                sock.close()
            except OSError:
                pass
            self._closed(why)

    def _line(self, line: str) -> None:
        if not line:
            return
        with self._state:
            if line.startswith("EVENT "):
                parts = line.split()
                kind, arg = parts[1], (parts[2] if len(parts) > 2 else None)
                if kind == "owner" and arg:
                    self.owner = arg
                elif kind == "paused":
                    self.paused = None if arg in (None, "none") else arg
                elif kind == "level":
                    self.map = arg
                self._events.append((kind, arg))
            else:
                slot = self._pending.popleft() if self._pending else None
                if slot is not None:
                    slot["reply"] = line
                elif line.startswith("ERR"):
                    self.last_error = line[4:].strip()
                    # An act racing an ownership change (a human took over, a yield ended)
                    # is normal and harmless; only protocol errors surface on the next call.
                    if not self.last_error.startswith("not owner"):
                        self._errors.append(self.last_error)
                if line.startswith("OK"):
                    for kv in line.split()[1:]:
                        if kv.startswith("tic="):
                            self._last_tic = int(kv[4:])
            self._state.notify_all()

    def _pinger(self) -> None:
        while self._sock is not None:
            idle = time.monotonic() - self._last_send
            if idle >= self.heartbeat:
                try:
                    self._send("ping", wait=False, check=False)   # never consumes the agent's errors
                except AgentError:
                    return                                         # disconnected
                idle = 0.0
            time.sleep(max(0.01, self.heartbeat - idle))

    # -- requests -----------------------------------------------------------
    def _send(self, line: str, wait: bool, timeout: float = 2.0, check: bool = True) -> str | None:
        if check and self.strict and self._errors:
            raise AgentError(self._errors.popleft())
        sock = self._sock
        if sock is None:
            raise AgentError("not connected")
        slot = {"reply": None} if wait else None
        with self._send_lock:
            with self._state:
                self._pending.append(slot)
            try:
                sock.sendall(line.encode() + b"\n")
            except OSError as e:
                raise AgentError(f"send failed: {e}") from None
            self._last_send = time.monotonic()
        if not wait:
            return None
        with self._state:
            if not self._state.wait_for(lambda: slot["reply"] is not None, timeout):
                raise AgentError(f"no reply to {line.split()[0]!r} within {timeout} s")
        reply = slot["reply"]
        if reply.startswith("ERR"):
            raise AgentError(reply[4:].strip())
        return reply

    def act(self, move: float = 0, strafe: float = 0, turn: float = 0, fire: bool = False,
            use: bool = False, run: bool = True, weapon: int = 0, wait: bool = False):
        """Replace the held action. move/strafe in -1..1 (+ = forward / right), turn in
        degrees per tic (+ = left, at most 45), weapon 1..9 to switch (0 keeps the current
        one). The engine repeats it every tic until the next act, and stops the player
        hold_ms after the last one. Returns False when not driving (nothing is sent:
        the game would ignore it), else None, or with wait=True the tic at which it applies
        (informational: actions are not a stepping barrier)."""
        if not self.driving:
            return False
        vals = {"move": move, "strafe": strafe, "turn": turn}
        for k, lim in _ACT_LIMITS.items():
            v = float(vals[k])
            if not math.isfinite(v):
                raise ValueError(f"{k} must be finite")
            vals[k] = max(-lim, min(lim, v))
        weapon = int(weapon)
        if not 0 <= weapon <= 9:
            raise ValueError("weapon must be 0..9")
        line = (f"act move={_num(vals['move'])} strafe={_num(vals['strafe'])} turn={_num(vals['turn'])} "
                f"fire={int(bool(fire))} use={int(bool(use))} run={int(bool(run))} weapon={weapon}")
        try:
            reply = self._send(line, wait)
        except AgentError as e:
            if str(e).startswith("not owner"):       # lost control in flight: nothing applied
                return False
            raise
        if reply is None:
            return None
        tic = [kv[4:] for kv in reply.split() if kv.startswith("tic=")]
        return int(tic[0]) if tic else None

    def stop(self):
        """Hold nothing (stand still, release fire/use)."""
        return self.act(run=False)

    def yield_to_bot(self, seconds: float | None = None) -> None:
        """Let AutoDoom drive, for `seconds` or until take(). The lease stays yours."""
        self._send("yield" if seconds is None else f"yield {_num(seconds)}", wait=True)

    def take(self) -> None:
        """Take control back after yield_to_bot (no effect while a human plays)."""
        self._send("take", wait=True)

    def replan(self) -> None:
        """Ask AutoDoom to forget its route and search again (the menu's Reroute bot)."""
        self._send("replan", wait=True)

    def ping(self) -> int | None:
        reply = self._send("ping", wait=True)
        tic = [kv[4:] for kv in reply.split() if kv.startswith("tic=")]
        return int(tic[0]) if tic else None

    # -- events and observations --------------------------------------------
    def poll_events(self) -> list[tuple[str, str | None]]:
        """Events since the last call: ("owner", "agent"), ("paused", "busy"),
        ("level", "E1M2"), ("death", None), ("stuck", None), ("shutdown", None),
        ("closed", reason)."""
        with self._state:
            out = list(self._events)
            self._events.clear()
        return out

    def wait_for(self, predicate, timeout: float | None = None) -> bool:
        """Block until predicate(agent) is true (state changes wake it); False on timeout."""
        with self._state:
            return self._state.wait_for(lambda: predicate(self), timeout)

    def wait_for_control(self, timeout: float | None = None) -> bool:
        return self.wait_for(lambda a: a.driving or not a.connected, timeout) and self.connected

    def observe(self, pixels: bool = True) -> Observation | None:
        return self.frames.read(pixels)

    def next_observation(self, after: Observation | None = None, pixels: bool = True,
                         timeout: float = 1.0) -> Observation | None:
        """The first observation from a later world tic than `after` (default: the last
        one returned). While the world is paused this returns the current one at timeout."""
        last = after.tic if after is not None else getattr(self, "_seen_tic", None)
        deadline = time.monotonic() + timeout
        obs = None
        while True:
            obs = self.frames.read(pixels=False)
            if obs is not None and (last is None or obs.tic != last):
                break
            if time.monotonic() >= deadline or not self.connected:
                break
            time.sleep(0.002)
        if obs is not None and pixels:
            obs = self.frames.read(pixels=True) or obs
        if obs is not None:
            self._seen_tic = obs.tic
        return obs


# ------------------------------------------------------------------ command line
def _main(argv: list[str]) -> int:
    reader = FrameReader()
    if argv[:1] == ["info"]:
        obs = reader.read(pixels=False)
        if obs is None:
            print(f"no game frame at {reader.path} (is the wallpaper on, or the game kept ready?)", file=sys.stderr)
            return 1
        import json
        h = dict(obs.header, position=obs.pos, angle_deg=round(obs.angle_deg, 2), owner_name=obs.owner_name)
        print(json.dumps(h, indent=2))
        return 0
    if argv[:1] == ["snapshot"] and len(argv) == 2:
        obs = reader.read()
        if obs is None:
            print(f"no game frame at {reader.path}", file=sys.stderr)
            return 1
        obs.save_ppm(argv[1])
        print(f"{argv[1]}: {obs.width}x{obs.height} {obs.map} tic {obs.tic}")
        return 0
    print(__doc__.split("\n\n")[0] + "\n\nusage: doomagent.py info | snapshot OUT.ppm", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
