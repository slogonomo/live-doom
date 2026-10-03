# SPDX-License-Identifier: 0BSD
"""External-agent leases, strict actions, events and child supervision.

The controller owns native ownership, clock and sound policy. Its integration is:
``agent_native(command) -> str``, ``agent_header() -> dict``, ``engine_loaded()``,
``runtime``, ``config`` and optionally ``log(message)``. Call ``desired_owner``
from policy and send ``owner N generation`` when that pair changes; then call
``notify`` with the applied owner, pause reason and a stable header snapshot.

Protocol replies are returned, never written here. Drain events *after* queuing
each reply. A transport must close a connection when ``close_requested(conn)``
is true and call ``disconnect`` on EOF/error. ``tick`` must run at least every
100 ms; it never waits on a child or reads a socket.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import signal
import socket
import struct
import subprocess
import time
from typing import Any

from project_paths import (ProjectPaths, close_process_log, open_directory, read_json, spawn_logged)

TTL_SECONDS = 1.0
STARTUP_SECONDS = 5.0
HOLD_MS = 500
SCHEMA = 3
PROTOCOL = 1
MAX_EVENTS = 128
SAFE_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z")
INTEGER = re.compile(r"[+-]?[0-9]+\Z")
OWNER_NAMES = {0: "autodoom", 1: "agent", 2: "human"}
PAUSE_REASONS = frozenset(("busy", "lock", "asleep", "menu", "none"))


def valid_id(value: Any) -> bool:
    return isinstance(value, str) and bool(SAFE_ID.fullmatch(value))


def valid_name(value: Any) -> bool:
    return (isinstance(value, str) and 1 <= len(value) <= 64
            and value == value.strip() and all(32 <= ord(c) <= 126 for c in value))


@dataclass(frozen=True)
class Action:
    move: float = 0.0
    strafe: float = 0.0
    turn: float = 0.0
    fire: int = 0
    use: int = 0
    run: int = 0
    weapon: int = 0

    def command(self, generation: int) -> str:
        values = (format(self.move, ".17g"), format(self.strafe, ".17g"),
                  format(self.turn, ".17g"), str(self.fire), str(self.use),
                  str(self.run), str(self.weapon))
        return f"action {generation} " + " ".join(values) + f" {HOLD_MS}"


class ActionError(ValueError):
    def __init__(self, field: str):
        self.field = field
        super().__init__("ERR field " + field)


def parse_action(parts: list[str]) -> Action:
    """Parse an entire replacement action; no partial state is ever retained."""
    values: dict[str, float | int] = {}
    limits = {"move": 1, "strafe": 1, "turn": 45}
    for part in parts:
        name, separator, text = part.partition("=")
        if not separator or name not in (*limits, "fire", "use", "run", "weapon") or name in values:
            raise ActionError(name or "action")
        try:
            if name in limits:
                value = float(text)
                if not math.isfinite(value) or abs(value) > limits[name]:
                    raise ValueError
            else:
                if not INTEGER.fullmatch(text):
                    raise ValueError
                value = int(text)
                if not 0 <= value <= (9 if name == "weapon" else 1):
                    raise ValueError
        except (ValueError, OverflowError):
            raise ActionError(name) from None
        values[name] = value
    return Action(**values)


@dataclass(frozen=True)
class Manifest:
    id: str
    name: str
    command: tuple[str, ...]
    screensaver: bool
    directory: Path


def read_manifest(directory: Path, agent_id: str) -> Manifest:
    if not valid_id(agent_id) or agent_id == "autodoom":
        raise ValueError("Invalid agent id")
    # Bounded reads reject symlink components, including the manifest and its
    # parent folder. An agent cannot redirect discovery outside this root.
    root = directory.absolute()
    folder = root / agent_id
    path = folder / "agent.json"
    data = read_json(path, 65536)
    if not isinstance(data, dict) or set(data) - {"name", "command", "screensaver"}:
        raise ValueError("Invalid agent manifest fields")
    if not valid_name(data.get("name")):
        raise ValueError("Invalid agent name")
    command = data.get("command")
    if (not isinstance(command, list) or not 1 <= len(command) <= 64
            or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in command)
            or sum(len(arg) for arg in command) > 8192):
        raise ValueError("Agent command must be a nonempty argument list")
    screensaver = data.get("screensaver", True)
    if not isinstance(screensaver, bool):
        raise ValueError("Agent screensaver must be a boolean")
    return Manifest(agent_id, data["name"], tuple(command), screensaver, folder)


class AgentControl:
    def __init__(self, controller, *, clock=time.monotonic,
                 config_root: Path | None = None, state_root: Path | None = None):
        self.controller = controller
        self.clock = clock
        paths = ProjectPaths.from_env()
        self.config_root = Path(config_root) if config_root is not None else paths.agent_config_root
        self.state_root = Path(state_root) if state_root is not None else paths.agent_state_root
        self.connection = None
        self.name = ""
        self.generation = 0
        self.last_valid = 0.0
        self.yield_until: float | None = 0.0
        self._human = False
        self._saver = False
        self._running = False
        self._desired = 0
        self._applied = 0
        self._events: dict[Any, deque[str]] = {}
        self._closing: set[Any] = set()
        self._event_owner: str | None = None
        self._event_pause: str | None = None
        self._header_pid: int | None = None
        self._header_map: str | None = None
        self._deaths: int | None = None
        self._stuck: int | None = None
        self._connection_saver = True
        self._connection_process_pid: int | None = None
        self.last_error: str | None = None
        self.manifests: dict[str, Manifest] = {}
        self.discovery_errors: dict[str, str] = {}
        self._selected = self._selection()
        self._loaded = bool(controller.engine_loaded())
        self._closed = False
        self.process: subprocess.Popen | None = None
        self.process_id: str | None = None
        self._started = 0.0
        self._managed_hello = False
        self._stop_deadline: float | None = None
        self._kill_sent = False
        self._restart_at = 0.0
        self._backoff = 1.0
        self._scan_at = 0.0
        self.available()

    @property
    def connected(self) -> bool:
        return self.connection is not None

    @property
    def owner(self) -> str:
        return OWNER_NAMES[self._desired]

    @property
    def selected(self) -> str:
        return self._selection()

    def _selection(self) -> str:
        return self.controller.config.get("agent_selected", "autodoom")

    def _log(self, line: str):
        logger = getattr(self.controller, "log", None)
        if logger is not None:
            logger(line)

    def _header(self) -> dict:
        try:
            return self.controller.agent_header() or {}
        except (OSError, ValueError):
            return {}

    def _tic_reply(self) -> str:
        # Informational next-tic estimate; this is not a synchronous step fence.
        value = self._header().get("tic", 0)
        tic = value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
        return f"OK tic={tic + 1}"

    def _queue(self, conn, line: str):
        if conn is None or conn in self._closing:
            return
        queue = self._events.setdefault(conn, deque())
        if len(queue) >= MAX_EVENTS:
            self._revoke("event backpressure")
            return
        queue.append(line)

    def drain_events(self, conn) -> list[str]:
        queue = self._events.get(conn)
        if queue is None:
            return []
        lines = list(queue)
        queue.clear()
        return lines

    def close_requested(self, conn) -> bool:
        return conn in self._closing

    def next_deadline(self) -> float | None:
        """Monotonic lease deadline, independent of world pause or socket bytes."""
        return self.last_valid + TTL_SECONDS if self.connected else None

    def lease_wait(self, timeout: float, now: float | None = None) -> float:
        """Bound a selector wait so polling cadence cannot extend the lease."""
        now = self.clock() if now is None else now
        timeout = max(0.0, timeout)
        deadline = self.next_deadline()
        return timeout if deadline is None else min(timeout, max(0.0, deadline - now))

    def _revoke(self, reason: str):
        conn = self.connection
        if conn is None:
            return
        managed_pid = self._connection_process_pid
        self._connection_process_pid = None
        self.connection = None
        self.generation += 1
        self.name = ""
        self.yield_until = 0.0
        self._closing.add(conn)
        self._log("Agent disconnected: " + reason)
        if (managed_pid is not None and self.process is not None and self.process.pid == managed_pid
                and reason not in ("selection changed", "game unloaded", "controller shutdown")):
            # A living process without its lease can be hung just as surely as
            # an exited one. Stop its owned session and retry without turning
            # heartbeat recovery into a tight reconnect/restart loop.
            self._retry_process(f"lost its lease ({reason})", self.clock())
        self.desired_owner(self._human, self._saver, self._running)

    def disconnect(self, conn):
        if conn is self.connection:
            self._revoke("EOF or transport error")
        self._closing.discard(conn)
        self._events.pop(conn, None)

    def _expire(self, now: float):
        if self.connected and now - self.last_valid >= TTL_SECONDS:
            self._revoke("heartbeat expired")

    def _managed_peer(self, connection) -> bool:
        """Authenticate the selected child/session using kernel Unix credentials.

        Display names are labels. Descendants may use another process group
        within the launcher's new session, so session membership is accepted
        too. A missing/dead peer or a non-socket test object fails closed.
        """
        process = self.process
        if process is None or process.poll() is not None or self.process_id != self._selection():
            return False
        try:
            raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            pid, uid, _ = struct.unpack("3i", raw)
            if pid <= 0 or uid != os.getuid():
                return False
            return os.getpgid(pid) == process.pid or os.getsid(pid) == process.pid
        except (AttributeError, OSError, TypeError, ValueError, struct.error):
            return False

    def hello(self, name, version, connection) -> str:
        now = self.clock()
        self._expire(now)
        if self._closed:
            return "ERR shutdown"
        if str(version) != str(PROTOCOL) or isinstance(version, bool):
            return "ERR version"
        if not valid_name(name):
            return "ERR field name"
        if self.connected:
            return "ERR busy " + self.name
        manifest = self.manifests.get(self._selection())
        managed = self._managed_peer(connection)
        if (self.process is not None and self.process.poll() is None
                and (not managed or self._stop_deadline is not None)):
            return "ERR busy " + (manifest.name if manifest is not None else self.process_id or "managed agent")
        self.connection = connection
        self.name = name
        self.generation += 1
        self.last_valid = now
        self.yield_until = 0.0
        self._connection_saver = manifest.screensaver if manifest is not None else True
        self._connection_process_pid = self.process.pid if managed else None
        if managed:
            self._managed_hello = True
        self._closing.discard(connection)
        self._events[connection] = deque()
        self._event_owner = self._event_pause = None
        self._reset_header()
        self.desired_owner(self._human, self._saver, self._running)
        self._log("Agent connected: " + name)
        owner = "agent" if self._desired == 1 else "waiting"
        return f"OK owner={owner} gen={self.generation} ttl_ms=1000 hold_ms={HOLD_MS} schema={SCHEMA}"

    def desired_owner(self, human: bool, saver: bool, running: bool) -> int:
        self._expire(self.clock())
        now = self.clock()
        previous_running = self._running
        self._human, self._saver, self._running = bool(human), bool(saver), bool(running)
        yielding = self.yield_until is None or now < self.yield_until
        eligible = (self.connected and not yielding and self.controller.engine_loaded()
                    and (not saver or (self._connection_saver
                         and self.controller.config.get("agent_screensaver", True))))
        owner = 2 if human else 1 if eligible else 0
        if owner != self._desired or ((owner == 1 or self._desired == 1) and previous_running != bool(running)):
            self.generation += 1
        self._desired = owner
        return owner

    def handle(self, line: str, conn) -> str:
        now = self.clock()
        self._expire(now)
        if conn is not self.connection:
            return "ERR not owner"
        if not isinstance(line, str) or len(line) > 4096 or "\n" in line or "\r" in line:
            return "ERR command"
        parts = line.split()
        if not parts:
            return "ERR command"
        name, args = parts[0], parts[1:]
        action = None
        if name == "act":
            try:
                action = parse_action(args)
            except ActionError as exc:
                return str(exc)
            if self._desired != 1:
                return "ERR not owner"
        elif name == "ping" and not args:
            pass
        elif name == "yield" and len(args) <= 1:
            if args:
                try:
                    seconds = float(args[0])
                    if not math.isfinite(seconds) or not 0 < seconds <= 86400:
                        raise ValueError
                except (ValueError, OverflowError):
                    return "ERR field seconds"
                self.yield_until = now + seconds
            else:
                self.yield_until = None
        elif name == "take" and not args:
            self.yield_until = 0.0
        elif name == "replan" and not args:
            if self._human or not self.controller.engine_loaded():
                return "ERR not owner"
            try:
                answer = self.controller.agent_native("replan")
            except (OSError, RuntimeError, ValueError) as exc:
                self.last_error = "Agent replan failed: " + str(exc)
                return "ERR native"
            if not answer.startswith("OK"):
                self.last_error = "Agent replan failed: " + answer
                return answer if answer.startswith("ERR") else "ERR native"
        elif name == "bye" and not args:
            self.last_valid = now
            self._revoke("bye")
            return "OK"
        else:
            return "ERR command"
        self.last_valid = now
        self.desired_owner(self._human, self._saver, self._running)
        if action is not None and self._running:
            try:
                answer = self.controller.agent_native(action.command(self.generation))
            except (OSError, RuntimeError, ValueError) as exc:
                self.last_error = "Agent action failed: " + str(exc)
                self._revoke("native transport failure")
                return "ERR native"
            if not answer.startswith("OK"):
                self.last_error = "Agent action failed: " + answer
                self._revoke("native action rejected")
                return answer if answer.startswith("ERR") else "ERR native"
        return self._tic_reply()

    def _reset_header(self):
        self._header_pid = self._header_map = self._deaths = self._stuck = None

    def notify(self, owner: int, paused: str, header: dict | None = None):
        """Report applied policy and stable telemetry; never infer stuck from replan."""
        if owner not in OWNER_NAMES or paused not in PAUSE_REASONS:
            raise ValueError("Invalid applied agent policy")
        self._applied = owner
        conn = self.connection
        if conn is None:
            return
        name = OWNER_NAMES[owner] if self.controller.engine_loaded() else "waiting"
        if name != self._event_owner:
            self._queue(conn, "EVENT owner " + name)
            self._event_owner = name
        if paused != self._event_pause:
            self._queue(conn, "EVENT paused " + paused)
            self._event_pause = paused
        data = self._header() if header is None else header
        pid = data.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return  # Transient seqlock miss cannot reset a valid baseline.
        level = data.get("map")
        level = level if isinstance(level, str) and level and not any(c.isspace() for c in level) else None
        deaths, stuck = data.get("deaths"), data.get("bot_stuck")
        deaths = deaths if isinstance(deaths, int) and not isinstance(deaths, bool) and deaths >= 0 else None
        stuck = stuck if isinstance(stuck, int) and not isinstance(stuck, bool) and stuck >= 0 else None
        if self._header_pid != pid:
            self._header_pid, self._header_map = pid, level
            self._deaths, self._stuck = deaths, stuck
            return
        if level is not None and self._header_map is not None and level != self._header_map:
            self._queue(conn, "EVENT level " + level)
        if level is not None:
            self._header_map = level
        if deaths is not None:
            if self._deaths is not None and deaths > self._deaths:
                self._queue(conn, "EVENT death")
            self._deaths = deaths
        if stuck is not None:
            if self._stuck is not None and stuck > self._stuck:
                self._queue(conn, "EVENT stuck")
            self._stuck = stuck

    def available(self) -> list[dict[str, str]]:
        manifests, errors = {}, {}
        try:
            # Keep settings/discovery work finite even for an accidental huge
            # directory. No agent is executed during this scan.
            folders = []
            with open_directory(self.config_root) as directory:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if len(folders) == 256:
                            errors["directory"] = "Agent discovery limited to 256 entries"
                            break
                        folders.append(self.config_root / entry.name)
            folders.sort(key=lambda path: path.name)
        except FileNotFoundError:
            folders = []
        except OSError as exc:
            folders = []
            errors["directory"] = str(exc)
        for folder in folders:
            if not valid_id(folder.name) or folder.name == "autodoom" or not folder.is_dir():
                continue
            try:
                manifests[folder.name] = read_manifest(self.config_root, folder.name)
            except (OSError, ValueError, TypeError, RecursionError) as exc:
                errors[folder.name] = str(exc)
        self.manifests, self.discovery_errors = manifests, errors
        return [{"id": item.id, "name": item.name} for item in manifests.values()]

    def settings(self) -> dict:
        return {"selected": self._selection(), "available": self.available(),
                "connected": self.connected, "owner": self.owner,
                "last_error": self.last_error}

    def _owned_groups(self) -> set[int]:
        # Session ids originate from our Popen(start_new_session=True). Workers
        # can create their own process groups inside that session; include
        # those too. Never inspect or signal a manually connected peer's group.
        if self.process is None:
            return set()
        session = self.process.pid
        groups = {session}
        try:
            with os.scandir("/proc") as entries:
                for entry in entries:
                    if not entry.name.isdigit():
                        continue
                    try:
                        pid = int(entry.name)
                        if os.getsid(pid) == session:
                            groups.add(os.getpgid(pid))
                    except (OSError, ValueError):
                        continue
        except OSError:
            pass
        return groups

    def _signal_child(self, sig: int):
        # Only sessions created by this module, even when their leader exited.
        for group in self._owned_groups():
            try:
                os.killpg(group, sig)
            except ProcessLookupError:
                pass

    def _kill_remaining_group(self):
        # A child may spawn workers and then exit before the next tick. Clear
        # its own session's remaining workers before forgetting that group.
        # This is never called for a manually connected agent.
        self._signal_child(signal.SIGKILL)

    def _stop_process(self, now: float):
        if self.process is None or self._stop_deadline is not None:
            return
        self._signal_child(signal.SIGTERM)
        self._stop_deadline = now + 0.5
        self._kill_sent = False

    def _retry_process(self, reason: str, now: float):
        self.last_error = f"Agent {self.process_id} {reason}; retrying"
        self._log(self.last_error)
        self._stop_process(now)
        self._restart_at = now + self._backoff
        self._backoff = min(60.0, self._backoff * 2)

    def _spawn(self, manifest: Manifest, now: float):
        try:
            self.process = spawn_logged(list(manifest.command), self.state_root / (manifest.id + ".log"),
                cwd=manifest.directory,
                env=os.environ | {"DOOM_AGENT_SOCKET": str(self.controller.runtime / "control.sock"),
                                  "DOOM_AGENT_FRAME": str(self.controller.runtime / "frame.bin"),
                                  "PYTHONDONTWRITEBYTECODE": "1"},
                stdin=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        except (OSError, ValueError) as exc:
            self.last_error = f"Unable to start agent {manifest.id}: {exc}"
            self._log(self.last_error)
            self._restart_at = now + self._backoff
            self._backoff = min(60.0, self._backoff * 2)
            return
        self.process_id, self._started = manifest.id, now
        self._managed_hello = False
        self._stop_deadline = None
        self._kill_sent = False
        self.last_error = None
        self._log(f"Started agent {manifest.id}: PID {self.process.pid}")

    def tick(self, now: float | None = None):
        now = self.clock() if now is None else now
        self._expire(now)
        loaded = bool(self.controller.engine_loaded())
        selected = self._selection()
        if selected != self._selected:
            if self.connected:
                self._queue(self.connection, "EVENT shutdown")
                self._revoke("selection changed")
            self._stop_process(now)
            self._selected = selected
            self._backoff, self._restart_at = 1.0, 0.0
            self.available()
        if self._loaded and not loaded:
            if self.connected:
                self._queue(self.connection, "EVENT shutdown")
                self._revoke("game unloaded")
            self._stop_process(now)
            self._backoff, self._restart_at = 1.0, 0.0
            self._reset_header()
        self._loaded = loaded
        if self._closed or not loaded or selected == "autodoom":
            self._stop_process(now)
        if self.process is not None:
            code = self.process.poll()
            if code is not None:
                stopped = self._stop_deadline is not None
                self._kill_remaining_group()
                # EOF normally closes the logging thread itself; a surviving
                # descendant's inherited stdout must not retain this sink.
                close_process_log(self.process, timeout=0.0)
                if not stopped:
                    self.last_error = f"Agent {self.process_id} exited ({code}); retrying"
                    self._log(self.last_error)
                    self._restart_at = now + self._backoff
                    self._backoff = min(60.0, self._backoff * 2)
                self.process = None
                self.process_id = None
                self._stop_deadline = None
                self._kill_sent = False
            elif self._stop_deadline is not None and now >= self._stop_deadline and not self._kill_sent:
                self._signal_child(signal.SIGKILL)
                self._kill_sent = True
            elif self._stop_deadline is None and not self._managed_hello and now - self._started >= STARTUP_SECONDS:
                self._retry_process("did not connect within 5 seconds", now)
            elif self._stop_deadline is None and now - self._started >= 30:
                self._backoff = 1.0
        if not self._closed and loaded and selected != "autodoom" and self.process is None and now >= self._restart_at:
            if now >= self._scan_at:
                self.available()
                self._scan_at = now + 1.0
            manifest = self.manifests.get(selected)
            if manifest is None:
                detail = self.discovery_errors.get(selected, "manifest not found")
                self.last_error = f"Agent {selected} is unavailable: {detail}"
                self._restart_at = now + min(60.0, self._backoff)
                self._backoff = min(60.0, self._backoff * 2)
            else:
                self._spawn(manifest, now)
        self.desired_owner(self._human, self._saver, self._running)

    def shutdown(self):
        """Revoke immediately; tick continues nonblocking child reap/escalation."""
        self._closed = True
        if self.connected:
            self._queue(self.connection, "EVENT shutdown")
            self._revoke("controller shutdown")
        self._stop_process(self.clock())
