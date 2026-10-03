#!/usr/bin/env python3
# SPDX-License-Identifier: 0BSD
"""A stand-in for the game's agent interface, for developing and testing agents
without the engine: a real Unix socket speaking the v1 agent protocol and a real
seqlocked frame.bin (header v3) over a tiny 2-D world.

The world is a square room (walls at ±1024 map units) with one internal wall: a
corridor dead end in which the fake AutoDoom gets stuck. It follows the same rules as
the game: human > agent > AutoDoom, the connection is the lease (ttl_ms), the held
action goes neutral hold_ms of world time after the last act, actions while paused are
ignored, and bot_stuck counts AutoDoom no-progress detections (here after
`stuck_after` seconds instead of 60).

    python3 doomagent_fake.py              # serve until Ctrl+C from a private temp dir; it
                                           # prints the LIVE_DOOM_RUNTIME=... line for agents
Test hooks: human_takeover()/human_release(), pause(reason)/resume(), kill_player(),
new_level(map), emit(kind, arg), and the `log` of every request line.
"""
from __future__ import annotations

import ctypes
import math
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from doomagent import HEADER_AREA, MAGIC, _HeaderV3  # noqa: E402

FRAC = 65536
RUN, WALK = 16.0, 8.0                     # map units per tic at full move
WALL = 1024.0
# The dead end: a wall segment x = 512 from y = -256 to 256; AutoDoom faces it from the west.
DEAD_END = (512.0, -256.0, 256.0)


class FakeGame:
    def __init__(self, directory=None, width=160, height=100, tic_rate=35.0, ttl_ms=1000, hold_ms=500,
                 stuck_after=3.0, start=(400.0, 0.0, 0.0)):
        self.dir = Path(directory) if directory else Path(tempfile.mkdtemp(prefix="doomagent-fake-"))
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)                    # agents only talk to a private runtime dir
        self.frame_path, self.socket_path = self.dir / "frame.bin", self.dir / "control.sock"
        self.width, self.height = width, height
        self.tic_rate, self.ttl_ms, self.hold_ms, self.stuck_after = tic_rate, ttl_ms, hold_ms, stuck_after
        self.hold_tics = max(1, round(hold_ms * tic_rate / 1000))
        self.lock = threading.RLock()
        self.x, self.y, self.angle = start          # angle: degrees, counter-clockwise from east
        self.tic = self.frame = 0
        self.health, self.deaths, self.levels, self.bot_stuck, self.replans = 100, 0, 0, 0, 0
        self.map = "E1M1"
        self.human = False
        self.paused_reason = None
        self.agent = None                            # the connected agent's session
        self.yield_until = None                      # monotonic deadline, math.inf = until take
        self.action = None                           # (dict, tic it arrived)
        self.agent_gen = 0
        self.log: list[str] = []
        self._stuck_since = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._fd = None

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> "FakeGame":
        self._fd = open(self.frame_path, "w+b")
        self._fd.truncate(HEADER_AREA + self.width * self.height * 4)
        self._publish()
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass
        self.server.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        self.server.listen(8)
        self.server.settimeout(0.1)
        for target in (self._world, self._accept):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)
        return self

    def stop(self) -> None:
        with self.lock:
            if self.agent:
                self.agent.send("EVENT shutdown")
                self.agent.close()
        self._stop.set()
        for t in self._threads:
            t.join(2)
        self.server.close()
        self._fd.close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # -- ownership ------------------------------------------------------------
    @property
    def owner(self) -> str:
        if self.human:
            return "human"
        if self.agent and not (self.yield_until and time.monotonic() < self.yield_until):
            return "agent"
        return "autodoom"

    def _owner_changed(self, before: str) -> None:
        if self.agent and self.owner != before:
            if self.owner != "agent":
                self.action = None                  # preemption/yield releases to neutral
            self.agent.send(f"EVENT owner {self.owner}")

    def human_takeover(self):
        with self.lock:
            before, self.human = self.owner, True
            self._owner_changed(before)

    def human_release(self):
        with self.lock:
            before, self.human = self.owner, False
            self._owner_changed(before)

    def pause(self, reason="busy"):
        with self.lock:
            self.paused_reason = reason
            self.action = None
            if self.agent:
                self.agent.send(f"EVENT paused {reason}")

    def resume(self):
        with self.lock:
            self.paused_reason = None
            if self.agent:
                self.agent.send("EVENT paused none")

    def kill_player(self):
        with self.lock:
            self.health, self.deaths = 0, self.deaths + 1
            self.emit("death")

    def new_level(self, name):
        with self.lock:
            self.map, self.levels, self.health = name, self.levels + 1, 100
            self.emit("level", name)

    def emit(self, kind, arg=None):
        with self.lock:
            if self.agent:
                self.agent.send(f"EVENT {kind}" + (f" {arg}" if arg else ""))

    # -- world ------------------------------------------------------------------
    def _blocked(self, x0, y0, x1, y1) -> bool:
        if not (-WALL < x1 < WALL and -WALL < y1 < WALL):
            return True
        wx, ylo, yhi = DEAD_END
        if (x0 - wx) * (x1 - wx) <= 0 and x0 != x1:
            t = (wx - x0) / (x1 - x0)
            yc = y0 + t * (y1 - y0)
            return ylo <= yc <= yhi
        return abs(x1 - wx) < 1 and ylo <= y1 <= yhi

    def _step(self) -> None:
        owner = self.owner
        if self.yield_until and self.yield_until is not math.inf and time.monotonic() >= self.yield_until:
            self.yield_until = None
            self._owner_changed("autodoom")
            owner = self.owner
        move = strafe = turn = 0.0
        run = True
        if owner == "agent" and self.action and self.tic - self.action[1] < self.hold_tics:
            a = self.action[0]
            move, strafe, turn, run = a["move"], a["strafe"], a["turn"], a["run"]
        elif owner == "autodoom" and self.health > 0:
            move = 1.0                                # fake AutoDoom: walks straight on
        self.angle = (self.angle + turn) % 360
        speed = RUN if run else WALK
        rad = math.radians(self.angle)
        dx = speed * (move * math.cos(rad) + strafe * math.sin(rad))
        dy = speed * (move * math.sin(rad) - strafe * math.cos(rad))
        nx, ny = self.x + dx, self.y + dy
        moved = (dx or dy) and not self._blocked(self.x, self.y, nx, ny)
        if moved:
            self.x, self.y = nx, ny
        if owner == "autodoom" and self.health > 0:
            if moved:
                self._stuck_since = None
            elif self._stuck_since is None:
                self._stuck_since = self.tic
            elif (self.tic - self._stuck_since) >= self.stuck_after * self.tic_rate:
                self.bot_stuck += 1
                self._stuck_since = self.tic
                self.emit("stuck")
        else:
            self._stuck_since = None
        self.tic += 1

    def _publish(self) -> None:
        h = _HeaderV3()
        h.magic, h.version, h.width, h.height, h.stride = MAGIC, 3, self.width, self.height, self.width * 4
        h.bot = int(self.owner == "autodoom")
        h.paused = int(self.paused_reason is not None)
        h.frame, h.tic = self.frame, self.tic
        h.leveltime, h.health = self.tic, self.health
        h.x, h.y = round(self.x * FRAC), round(self.y * FRAC)
        h.map = self.map.encode()
        h.pid = os.getpid()
        h.angle = ctypes.c_int32(round(self.angle / 360 * 2 ** 32) & 0xFFFFFFFF).value
        h.owner = {"autodoom": 0, "agent": 1, "human": 2}[self.owner]
        h.agent_gen, h.deaths, h.levels_completed, h.bot_stuck = self.agent_gen, self.deaths, self.levels, self.bot_stuck
        h.bot_replans, h.render_fps = self.replans, 35
        shade = int(self.angle / 360 * 255)
        row = bytes((shade, 64, 255 - shade, 0)) * self.width
        f = self._fd
        f.seek(8)
        seq = int.from_bytes(f.read(4), "little")
        f.seek(8); f.write((seq + 1).to_bytes(4, "little")); f.flush()        # odd: writing
        h.seq = seq + 1
        f.seek(0); f.write(bytes(h)[:8]); f.seek(12); f.write(bytes(h)[12:])
        f.seek(HEADER_AREA); f.write(row * self.height); f.flush()
        f.seek(8); f.write((seq + 2).to_bytes(4, "little")); f.flush()        # even: done

    def _world(self) -> None:
        period = 1.0 / self.tic_rate
        nxt = time.monotonic()
        while not self._stop.is_set():
            with self.lock:
                if self.paused_reason is None:
                    self._step()
                self.frame += 1
                self._publish()
            nxt += period
            time.sleep(max(0.0, nxt - time.monotonic()))

    # -- protocol -------------------------------------------------------------------
    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.server.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=_Session(self, conn).run, daemon=True).start()

    def handle(self, session: "_Session", line: str) -> str:
        self.log.append(line)
        parts = line.split()
        verb = parts[0] if parts else ""
        with self.lock:
            if session is not self.agent:
                if verb != "agent" or len(parts) != 4 or parts[1] != "hello":
                    return "ERR not owner"
                if parts[3] != "1":
                    return "ERR version"
                if self.agent is not None:
                    return f"ERR busy {self.agent.name}"
                before = self.owner
                self.agent, session.name = session, parts[2]
                self.agent_gen += 1
                self.yield_until = None
                if self.owner != before:
                    self.action = None
                paused = self.paused_reason or "none"
                return (f"OK owner={'agent' if self.owner == 'agent' else 'waiting'} gen={self.agent_gen} ttl_ms={self.ttl_ms} "
                        f"hold_ms={self.hold_ms} schema=3 paused={paused}")
            if verb == "ping":
                return f"OK tic={self.tic}"
            if verb == "act":
                a = {"move": 0.0, "strafe": 0.0, "turn": 0.0, "fire": 0, "use": 0, "run": 0, "weapon": 0}
                for kv in parts[1:]:
                    k, _, v = kv.partition("=")
                    if k not in a:
                        return f"ERR field {k}"
                    try:
                        a[k] = float(v) if k in ("move", "strafe", "turn") else int(v)
                    except ValueError:
                        return f"ERR field {k}"
                if not (-1 <= a["move"] <= 1 and -1 <= a["strafe"] <= 1 and -45 <= a["turn"] <= 45
                        and 0 <= a["weapon"] <= 9 and all(a[k] in (0, 1) for k in ("fire", "use", "run"))):
                    return "ERR field range"
                if self.owner != "agent":
                    return "ERR not owner"
                if self.paused_reason is None:
                    self.action = (a, self.tic)       # ignored (not replayed) while paused
                return f"OK tic={self.tic + 1}"
            if verb == "yield":
                seconds = float(parts[1]) if len(parts) > 1 else None
                before = self.owner
                self.yield_until = math.inf if seconds is None else time.monotonic() + seconds
                self._owner_changed(before)
                return "OK"
            if verb == "take":
                before, self.yield_until = self.owner, None
                self._owner_changed(before)
                return "OK"
            if verb == "replan":
                self.replans += 1
                return "OK"
            if verb == "bye":
                self.release(session)
                return "OK"
            return f"ERR field {verb or 'empty'}"

    def release(self, session: "_Session") -> None:
        with self.lock:
            if self.agent is session:
                self.agent, self.action, self.yield_until = None, None, None


class _Session:
    def __init__(self, game: FakeGame, conn: socket.socket):
        self.game, self.conn, self.name = game, conn, None
        self.wlock = threading.Lock()

    def send(self, line: str) -> None:
        try:
            with self.wlock:
                self.conn.sendall(line.encode() + b"\n")
        except OSError:
            pass

    def close(self) -> None:
        try:
            self.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def run(self) -> None:
        g, buf = self.game, b""
        ttl = g.ttl_ms / 1000
        self.conn.settimeout(ttl)
        try:
            while True:
                try:
                    chunk = self.conn.recv(4096)
                except socket.timeout:
                    break                                   # lease expired: no line within ttl
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = line.decode(errors="replace").strip()
                    reply = g.handle(self, text)
                    self.send(reply)
                    if reply.startswith("ERR") and g.agent is not self:
                        return                              # refused hello: hang up
                    if text == "bye":
                        return
        except OSError:
            pass
        finally:
            g.release(self)
            self.conn.close()


if __name__ == "__main__":
    game = FakeGame(sys.argv[1] if len(sys.argv) > 1 else None).start()
    print(f"fake game at {game.dir}\n  LIVE_DOOM_RUNTIME={game.dir} python3 your_agent.py\nCtrl+C stops it.")
    try:
        last = None
        while True:
            time.sleep(1)
            line = f"{game.owner:8} pos=({game.x:.0f},{game.y:.0f}) angle={game.angle:.0f} stuck={game.bot_stuck}"
            if line != last:
                print(line, flush=True)
                last = line
    except KeyboardInterrupt:
        game.stop()
