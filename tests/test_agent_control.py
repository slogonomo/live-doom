#!/usr/bin/env python3
# SPDX-License-Identifier: 0BSD
"""Private protocol/supervisor tests: no engine, compositor or production IPC."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from agent_control import AgentControl, Action, MAX_EVENTS, parse_action, valid_id
from project_paths import ProjectPaths, flush_process_log


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Controller:
    def __init__(self, runtime):
        self.runtime = runtime
        self.config = {"agent_selected": "autodoom", "agent_screensaver": True}
        self.loaded = True
        self.header = {"pid": 42, "tic": 123, "map": "E1M1", "deaths": 0,
                       "bot_stuck": 0, "bot_replans": 0}
        self.commands = []
        self.logs = []
        self.answer = "OK"

    def engine_loaded(self):
        return self.loaded

    def agent_native(self, command):
        self.commands.append(command)
        return self.answer

    def agent_header(self):
        return dict(self.header)

    def log(self, line):
        self.logs.append(line)


class Process:
    next_pid = 8000

    def __init__(self):
        self.pid = Process.next_pid
        Process.next_pid += 1
        self.code = None

    def poll(self):
        return self.code


class Peer:
    def __init__(self, pid, uid=None):
        self.pid, self.uid = pid, os.getuid() if uid is None else uid

    def getsockopt(self, level, option, size):
        if (level, option, size) != (socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")):
            raise AssertionError("Only kernel Unix peer credentials should be queried")
        return struct.pack("3i", self.pid, self.uid, os.getgid())


class AgentControlTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="doom-agent-unit-")
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.agents = self.base / "agents"
        self.logs = self.base / "logs"
        self.agents.mkdir()
        self.clock = Clock()
        self.controller = Controller(self.base / "runtime")
        self.module = AgentControl(self.controller, clock=self.clock,
                                   config_root=self.agents, state_root=self.logs)
        self.conn = object()
        self.module.desired_owner(False, False, True)

    def hello(self, name="Test agent"):
        answer = self.module.hello(name, 1, self.conn)
        self.assertTrue(answer.startswith("OK owner=agent "), answer)
        return answer

    def manifest(self, agent_id="wander", **overrides):
        folder = self.agents / agent_id
        folder.mkdir(exist_ok=True)
        value = {"name": "Wanderer", "command": ["python3", "wander.py"], "screensaver": True}
        value.update(overrides)
        (folder / "agent.json").write_text(json.dumps(value))
        self.module.available()
        return folder

    def test_atomic_actions_defaults_ranges_and_finite_validation(self):
        self.hello()
        self.assertEqual(self.module.handle(
            "act move=1 strafe=-0.5 turn=-4.5 fire=1 use=0 run=1 weapon=9", self.conn), "OK tic=124")
        generation = self.module.generation
        self.assertEqual(self.controller.commands, [f"action {generation} 1 -0.5 -4.5 1 0 1 9 500"])
        self.module.handle("act fire=1", self.conn)
        self.assertEqual(self.controller.commands[-1], f"action {generation} 0 0 0 1 0 0 0 500")
        self.module.handle("act", self.conn)
        self.assertEqual(self.controller.commands[-1], f"action {generation} 0 0 0 0 0 0 0 500")
        self.assertEqual(parse_action(["move=-1", "turn=45"]), Action(move=-1, turn=45))

    def test_rejected_actions_and_verbs_never_renew_or_forward(self):
        self.hello()
        timestamp = self.module.last_valid
        self.clock.advance(0.5)
        invalid = {"move=nan": "move", "move=inf": "move", "move=-inf": "move",
                   "turn=45.01": "turn", "strafe=1.01": "strafe", "fire=2": "fire",
                   "run=1.0": "run", "weapon=10": "weapon", "use=-1": "use",
                   "move=0 move=1": "move", "look=1": "look", "move": "move"}
        for fields, error in invalid.items():
            with self.subTest(fields=fields):
                self.assertEqual(self.module.handle("act " + fields, self.conn), "ERR field " + error)
        for line in ("key 119 1", "text 65", "mouse 2 3", "set x 0", "menu opened",
                     "save", "load x", "quit", "ping junk", "take junk", "", "ping\nbye"):
            with self.subTest(line=line):
                self.assertEqual(self.module.handle(line, self.conn), "ERR command")
        self.assertEqual(self.module.last_valid, timestamp)
        self.assertEqual(self.controller.commands, [])
        self.clock.advance(0.5)
        self.module.tick()
        self.assertFalse(self.module.connected)
        self.assertTrue(self.module.close_requested(self.conn))
        self.assertEqual(self.module.desired_owner(False, False, True), 0)

    def test_version_single_connection_and_expired_connection_cannot_act(self):
        other = object()
        self.assertEqual(self.module.hello("Test", 2, self.conn), "ERR version")
        self.assertEqual(self.module.hello("Bad\nname", 1, self.conn), "ERR field name")
        self.hello()
        self.assertEqual(self.module.hello("Other", 1, other), "ERR busy Test agent")
        self.assertEqual(self.module.handle("act fire=1", other), "ERR not owner")
        self.clock.advance(1.001)
        self.assertEqual(self.module.handle("ping", self.conn), "ERR not owner")
        self.assertTrue(self.module.hello("Other", 1, other).startswith("OK"))
        self.module.disconnect(self.conn)  # Delayed EOF must not revoke its replacement.
        self.assertIs(self.module.connection, other)

    def test_selector_wait_reaches_lease_deadline_without_poll_cadence_extension(self):
        self.assertIsNone(self.module.next_deadline())
        self.assertEqual(self.module.lease_wait(0.1), 0.1)
        self.hello()
        self.assertEqual(self.module.next_deadline(), 101.0)
        self.clock.advance(0.97)
        self.assertAlmostEqual(self.module.lease_wait(0.1), 0.03)
        self.assertEqual(self.module.lease_wait(0.01), 0.01)
        # Valid activity moves the deadline; a rejected line never does.
        self.module.handle("ping", self.conn)
        renewed = self.module.next_deadline()
        self.clock.advance(0.98)
        self.module.handle("act move=nan", self.conn)
        self.assertEqual(self.module.next_deadline(), renewed)
        self.assertAlmostEqual(self.module.lease_wait(0.1), 0.02)
        self.assertEqual(self.module.lease_wait(0.1, now=renewed), 0.0)
        self.clock.now = renewed
        self.module.tick()
        self.assertFalse(self.module.connected)
        self.assertEqual(self.module.lease_wait(0.1), 0.1)

    def test_human_preemption_and_pause_clear_generation_without_losing_lease(self):
        self.hello()
        generation = self.module.generation
        self.assertEqual(self.module.desired_owner(True, False, True), 2)
        self.assertGreater(self.module.generation, generation)
        self.assertEqual(self.module.handle("act fire=1", self.conn), "ERR not owner")
        self.clock.advance(0.8)
        self.assertEqual(self.module.handle("ping", self.conn), "OK tic=124")
        self.module.notify(2, "none", self.controller.header)
        self.assertEqual(self.module.drain_events(self.conn), ["EVENT owner human", "EVENT paused none"])
        self.assertEqual(self.module.desired_owner(False, False, True), 1)
        generation = self.module.generation
        self.assertEqual(self.module.desired_owner(False, False, False), 1)
        self.assertGreater(self.module.generation, generation)
        self.clock.advance(0.8)
        self.assertEqual(self.module.handle("act fire=1", self.conn), "OK tic=124")
        self.assertEqual(self.controller.commands, [], "Paused actions must be discarded")
        self.clock.advance(0.8)
        self.module.handle("ping", self.conn)
        self.assertEqual(self.module.desired_owner(False, False, True), 1)
        self.assertTrue(self.module.connected)
        self.module.handle("act move=1", self.conn)
        self.assertEqual(len(self.controller.commands), 1)

    def test_yield_deadline_take_replan_bye_and_eof(self):
        self.hello()
        self.assertEqual(self.module.handle("replan", self.conn), "OK tic=124")
        self.assertEqual(self.controller.commands, ["replan"])
        self.assertEqual(self.module.handle("yield 0.5", self.conn), "OK tic=124")
        self.assertEqual(self.module.desired_owner(False, False, True), 0)
        self.assertEqual(self.module.handle("act move=1", self.conn), "ERR not owner")
        self.clock.advance(0.5)
        self.assertEqual(self.module.desired_owner(False, False, True), 1)
        self.module.handle("yield", self.conn)
        self.assertEqual(self.module.desired_owner(False, False, True), 0)
        self.module.handle("take", self.conn)
        self.assertEqual(self.module.desired_owner(False, False, True), 1)
        stamp = self.module.last_valid
        self.clock.advance(0.1)
        for line in ("yield nan", "yield inf", "yield 0", "yield -1", "yield 100000", "yield 1 2"):
            self.assertTrue(self.module.handle(line, self.conn).startswith("ERR"))
        self.assertEqual(self.module.last_valid, stamp)
        self.assertEqual(self.module.handle("bye", self.conn), "OK")
        self.assertFalse(self.module.connected)
        self.assertTrue(self.module.close_requested(self.conn))
        self.assertEqual(self.module.desired_owner(False, False, True), 0)
        other = object()
        self.assertTrue(self.module.hello("Again", 1, other).startswith("OK"))
        self.module.disconnect(other)
        self.assertEqual(self.module.desired_owner(False, False, True), 0)

    def test_screensaver_global_and_manifest_opt_out(self):
        self.manifest(screensaver=False)
        self.controller.config["agent_selected"] = "wander"
        self.hello("Wanderer")
        self.assertEqual(self.module.desired_owner(False, True, True), 0)
        self.assertEqual(self.module.handle("act move=1", self.conn), "ERR not owner")
        self.assertEqual(self.module.desired_owner(False, False, True), 1)
        self.module.disconnect(self.conn)
        self.controller.config["agent_selected"] = "autodoom"
        self.hello()
        self.assertEqual(self.module.desired_owner(False, True, True), 1)
        self.controller.config["agent_screensaver"] = False
        self.assertEqual(self.module.desired_owner(False, True, True), 0)
        self.assertTrue(self.module.connected)

    def test_ordered_events_dedup_transient_reads_and_new_pid_baselines(self):
        self.hello()
        self.module.notify(1, "none", self.controller.header)
        self.assertEqual(self.module.drain_events(self.conn), ["EVENT owner agent", "EVENT paused none"])
        self.module.notify(1, "none", {})
        self.module.notify(1, "none", self.controller.header)
        self.assertEqual(self.module.drain_events(self.conn), [])
        self.controller.header.update(map="E1M2", deaths=1, bot_stuck=1, bot_replans=20)
        self.module.notify(1, "busy", self.controller.header)
        self.assertEqual(self.module.drain_events(self.conn), ["EVENT paused busy", "EVENT level E1M2",
                                                             "EVENT death", "EVENT stuck"])
        self.controller.header.update(pid=99, map="MAP01", deaths=100, bot_stuck=100)
        self.module.notify(1, "busy", self.controller.header)
        self.assertEqual(self.module.drain_events(self.conn), [])
        self.controller.header.update(bot_replans=40)
        self.module.notify(1, "busy", self.controller.header)
        self.assertEqual(self.module.drain_events(self.conn), [], "Explicit replan is not a stuck event")
        self.controller.header.update(deaths=101, bot_stuck=101)
        self.module.notify(1, "busy", self.controller.header)
        self.assertEqual(self.module.drain_events(self.conn), ["EVENT death", "EVENT stuck"])

    def test_selection_unload_shutdown_and_event_backpressure_revoke(self):
        self.hello()
        self.controller.config["agent_selected"] = "new"
        self.module.tick()
        self.assertFalse(self.module.connected)
        self.assertEqual(self.module.drain_events(self.conn), ["EVENT shutdown"])
        self.assertTrue(self.module.close_requested(self.conn))
        self.controller.config["agent_selected"] = "autodoom"
        self.module.tick()
        self.hello()
        self.controller.loaded = False
        self.module.tick()
        self.assertFalse(self.module.connected)
        self.assertEqual(self.module.drain_events(self.conn), ["EVENT shutdown"])
        self.controller.loaded = True
        self.module.tick()
        self.hello()
        self.module.shutdown()
        self.assertEqual(self.module.drain_events(self.conn), ["EVENT shutdown"])
        self.assertEqual(self.module.hello("new", 1, object()), "ERR shutdown")

    def test_event_backpressure_revokes_instead_of_growing_unbounded(self):
        self.hello()
        self.module.notify(1, "none", self.controller.header)
        for index in range(MAX_EVENTS):
            self.module.notify(1, "menu" if index % 2 else "busy", self.controller.header)
        self.assertFalse(self.module.connected)
        self.assertTrue(self.module.close_requested(self.conn))
        self.assertLessEqual(len(self.module.drain_events(self.conn)), MAX_EVENTS)
        self.assertEqual(self.module.desired_owner(False, False, True), 0)

    def test_native_failure_releases_lease(self):
        self.hello()
        with patch.object(self.controller, "agent_native", side_effect=OSError("private transport closed")):
            self.assertEqual(self.module.handle("act fire=1", self.conn), "ERR native")
        self.assertFalse(self.module.connected)
        self.assertTrue(self.module.close_requested(self.conn))
        self.assertIn("transport closed", self.module.last_error)

    def test_native_rejection_releases_previous_held_action(self):
        self.hello()
        self.module.handle("act fire=1", self.conn)
        self.controller.answer = "ERR not owner"
        self.assertEqual(self.module.handle("act move=1", self.conn), "ERR not owner")
        self.assertFalse(self.module.connected)
        self.assertTrue(self.module.close_requested(self.conn))
        self.assertEqual(self.module.desired_owner(False, False, True), 0)

    def test_manifests_are_strict_discovered_and_do_not_execute_shell(self):
        self.manifest()
        self.manifest("bad-command", command="python3 wander.py")
        self.manifest("bad-saver", screensaver="yes")
        self.manifest("bad-name", name="bad\nname")
        self.manifest("bad-extra", shell=True)
        self.assertEqual(self.module.available(), [{"id": "wander", "name": "Wanderer"}])
        self.assertEqual(len(self.module.discovery_errors), 4)
        for value in ("../x", "/tmp/x", ".hidden", "foo.bar", "x\n", "", None):
            self.assertFalse(valid_id(value))
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "agent.json").write_text(json.dumps({"name": "Outside", "command": ["false"]}))
        (self.agents / "escape").symlink_to(outside, target_is_directory=True)
        self.assertEqual(self.module.available(), [{"id": "wander", "name": "Wanderer"}])
        self.assertIn("escape", self.module.discovery_errors)

    def test_default_agent_paths_use_live_doom_xdg_and_do_not_adopt_legacy(self):
        env = {"XDG_CONFIG_HOME": str(self.base / "xdg-config"),
               "XDG_STATE_HOME": str(self.base / "xdg-state")}
        legacy = self.base / "xdg-config/doom-desktop/agents/old"
        legacy.mkdir(parents=True)
        (legacy / "agent.json").write_text(json.dumps({"name": "Old", "command": ["false"]}))
        with patch.dict(os.environ, env):
            module = AgentControl(self.controller, clock=self.clock)
            paths = ProjectPaths.from_env()
        self.assertEqual(module.config_root, paths.agent_config_root)
        self.assertEqual(module.state_root, paths.agent_state_root)
        self.assertEqual(module.available(), [])
        self.assertFalse(module.config_root.exists())
        self.assertTrue((legacy / "agent.json").is_file())

    def test_manifest_reads_are_bounded_and_nofollow_without_fifo_wait(self):
        safe = self.manifest()
        for name in ("oversize", "linked", "fifo"):
            folder = self.agents / name
            folder.mkdir()
            if name == "oversize":
                (folder / "agent.json").write_bytes(b" " * 65537)
            elif name == "linked":
                (folder / "agent.json").symlink_to(safe / "agent.json")
            else:
                os.mkfifo(folder / "agent.json")
        started = time.monotonic()
        self.assertEqual(self.module.available(), [{"id": "wander", "name": "Wanderer"}])
        self.assertEqual(set(self.module.discovery_errors), {"oversize", "linked", "fifo"})
        self.assertLess(time.monotonic() - started, 1)
        alias = self.base / "agents-alias"
        alias.symlink_to(self.agents, target_is_directory=True)
        self.module.config_root = alias
        self.assertEqual(self.module.available(), [])
        self.assertIn("directory", self.module.discovery_errors)

    def test_real_supervised_agent_output_is_rotated_without_changing_lease_policy(self):
        program = "import os; [os.write(1,b'x'*65536) for _ in range(100)]; os.write(2,b'AGENT-END\\n')"
        self.manifest(command=[sys.executable, "-B", "-c", program])
        self.controller.config["agent_selected"] = "wander"
        self.module.tick()
        process = self.module.process
        self.assertIsNotNone(process)
        try:
            process.wait(timeout=5)
            self.assertTrue(flush_process_log(process, timeout=1))
            self.assertIsNone(process._live_doom_log.error)
            logs = list(self.logs.iterdir())
            self.assertEqual(len(logs), 3)
            for log in logs:
                self.assertLessEqual(log.stat().st_size, 2 * 1024 * 1024)
                self.assertEqual(log.stat().st_mode & 0o777, 0o600)
            self.assertIn(b"AGENT-END\n", (self.logs / "wander.log").read_bytes())
            self.module.tick()
            self.assertIsNone(self.module.process)
            self.assertEqual(self.module._restart_at, self.clock() + 1)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
            self.controller.loaded = False
            self.module.tick()

    def test_supervisor_loaded_selection_environment_and_nonblocking_stop(self):
        folder = self.manifest()
        self.controller.loaded = False
        self.controller.config["agent_selected"] = "wander"
        process = Process()
        with patch("agent_control.subprocess.Popen", return_value=process) as spawn, \
                patch("agent_control.os.killpg") as send:
            self.module.tick()
            spawn.assert_not_called()
            self.controller.loaded = True
            self.module.tick()
            args, options = spawn.call_args
            self.assertEqual(args[0], ["python3", "wander.py"])
            self.assertEqual(options["cwd"], folder)
            self.assertTrue(options["start_new_session"])
            self.assertTrue(options["close_fds"])
            self.assertNotIn("shell", options)
            self.assertEqual(options["env"]["DOOM_AGENT_SOCKET"], str(self.controller.runtime / "control.sock"))
            self.assertEqual(options["env"]["DOOM_AGENT_FRAME"], str(self.controller.runtime / "frame.bin"))
            self.assertTrue((self.logs / "wander.log").is_file())
            self.assertEqual((self.logs / "wander.log").stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.module.hello("manual", 1, object()), "ERR busy Wanderer")
            self.conn = Peer(process.pid)
            with patch("agent_control.os.getpgid", return_value=process.pid):
                self.hello("Different protocol label")
            self.controller.loaded = False
            self.module.tick()
            send.assert_called_once_with(process.pid, signal.SIGTERM)
            self.assertIs(self.module.process, process)
            self.clock.advance(0.5)
            self.module.tick()
            self.assertEqual(send.call_args.args, (process.pid, signal.SIGKILL))
            process.code = -9
            self.module.tick()
            self.assertIsNone(self.module.process)
            self.assertEqual(spawn.call_count, 1)

    def test_crash_restart_backoff_is_bounded_and_stable_run_resets_it(self):
        self.manifest()
        self.controller.config["agent_selected"] = "wander"
        processes = []

        def spawn(*args, **kwargs):
            process = Process()
            processes.append(process)
            return process

        with patch("agent_control.subprocess.Popen", side_effect=spawn), patch("agent_control.os.killpg"):
            self.module.tick()
            for expected in (1, 2, 4, 8, 16, 32, 60, 60):
                processes[-1].code = 1
                self.module.tick()
                self.assertAlmostEqual(self.module._restart_at - self.clock(), expected)
                previous = len(processes)
                self.clock.advance(expected - 0.01)
                self.module.tick()
                self.assertEqual(len(processes), previous)
                self.clock.advance(0.02)
                self.module.tick()
                self.assertEqual(len(processes), previous + 1)
            self.clock.advance(30)
            self.module._managed_hello = True  # This stable run has completed its handshake.
            self.module.tick()
            processes[-1].code = 1
            self.module.tick()
            self.assertAlmostEqual(self.module._restart_at - self.clock(), 1)

    def test_worker_group_is_killed_when_supervised_parent_exits(self):
        self.manifest()
        self.controller.config["agent_selected"] = "wander"
        process = Process()
        with patch("agent_control.subprocess.Popen", return_value=process), \
                patch("agent_control.os.killpg") as send:
            self.module.tick()
            process.code = 0
            self.module.tick()
            send.assert_called_once_with(process.pid, signal.SIGKILL)
            self.assertIsNone(self.module.process)
            self.assertEqual(self.module._restart_at, self.clock() + 1)

    def test_managed_ttl_and_eof_stop_living_child_and_backoff_but_yield_does_not(self):
        self.manifest()
        self.controller.config["agent_selected"] = "wander"
        processes = []

        def spawn(*args, **kwargs):
            process = Process()
            processes.append(process)
            return process

        with patch("agent_control.subprocess.Popen", side_effect=spawn), \
                patch("agent_control.os.killpg") as send, \
                patch("agent_control.os.getpgid", side_effect=lambda pid: pid):
            self.module.tick()
            first = processes[-1]
            self.conn = Peer(first.pid)
            self.hello("Arbitrary client label")
            self.module.handle("yield", self.conn)
            self.clock.advance(0.8)
            self.module.handle("ping", self.conn)
            self.module.tick()
            send.assert_not_called()
            self.assertTrue(self.module.connected)
            self.module.handle("take", self.conn)
            self.clock.advance(1.0)
            self.module.tick()
            self.assertFalse(self.module.connected)
            send.assert_called_once_with(first.pid, signal.SIGTERM)
            self.assertEqual(self.module._restart_at, self.clock() + 1)
            first.code = -15
            self.module.tick()
            self.assertIsNone(self.module.process)
            self.clock.advance(0.99)
            self.module.tick()
            self.assertEqual(len(processes), 1)
            self.clock.advance(0.02)
            self.module.tick()
            second = processes[-1]
            self.assertIsNot(second, first)
            second_conn = Peer(second.pid)
            self.assertTrue(self.module.hello("Another label", 1, second_conn).startswith("OK"))
            self.module.disconnect(second_conn)
            self.assertEqual(send.call_args.args, (second.pid, signal.SIGTERM))
            self.assertEqual(self.module._restart_at, self.clock() + 2)
            self.assertFalse(self.module.connected)

    def test_managed_startup_without_hello_is_bounded_and_retried(self):
        self.manifest()
        self.controller.config["agent_selected"] = "wander"
        process = Process()
        with patch("agent_control.subprocess.Popen", return_value=process) as spawn, \
                patch("agent_control.os.killpg") as send:
            self.module.tick()
            self.clock.advance(4.99)
            self.module.tick()
            send.assert_not_called()
            self.clock.advance(0.01)
            self.module.tick()
            send.assert_called_once_with(process.pid, signal.SIGTERM)
            self.assertIn("did not connect", self.module.last_error)
            self.assertEqual(self.module._restart_at, self.clock() + 1)
            self.clock.advance(0.5)
            self.module.tick()
            self.assertEqual(send.call_args.args, (process.pid, signal.SIGKILL))
            process.code = -9
            self.module.tick()
            self.assertIsNone(self.module.process)
            self.assertEqual(spawn.call_count, 1)

    def test_real_supervised_launcher_and_descendant_use_kernel_peer_identity(self):
        self.controller.runtime.mkdir()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(str(self.controller.runtime / "control.sock"))
        listener.listen(4)
        listener.settimeout(3)
        worker = ("import os,socket,time\n"
                  "if os.getsid(0) != os.getpid(): os.setpgrp()\n"
                  "s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); "
                  "s.connect(os.environ['DOOM_AGENT_SOCKET']); time.sleep(60)")
        for descendant in (False, True):
            with self.subTest(descendant=descendant):
                command = ([sys.executable, "-c", "import subprocess,sys; subprocess.Popen([sys.executable,'-c',sys.argv[1]]).wait()", worker]
                           if descendant else [sys.executable, "-c", worker])
                self.manifest(command=command)
                # Reserve a manual peer before spawning, keeping identity
                # independent of its claimed protocol/display name.
                manual = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.addCleanup(manual.close)
                manual.connect(str(self.controller.runtime / "control.sock"))
                manual_server, _ = listener.accept()
                self.addCleanup(manual_server.close)
                self.controller.config["agent_selected"] = "wander"
                self.module.tick()
                process = self.module.process
                self.assertIsNotNone(process)
                peer_pid = None
                try:
                    self.assertEqual(self.module.hello("Wanderer", 1, manual_server), "ERR busy Wanderer")
                    server, _ = listener.accept()
                    self.addCleanup(server.close)
                    pid, uid, _ = struct.unpack("3i", server.getsockopt(
                        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
                    peer_pid = pid
                    self.assertEqual(uid, os.getuid())
                    self.assertEqual(os.getsid(pid), process.pid)
                    if descendant:
                        self.assertNotEqual(pid, process.pid)
                        self.assertNotEqual(os.getpgid(pid), process.pid)
                    else:
                        self.assertEqual(pid, process.pid)
                    answer = self.module.hello("Custom SDK name", 1, server)
                    self.assertTrue(answer.startswith("OK owner=agent "), answer)
                    self.assertEqual(self.module._connection_process_pid, process.pid)
                    self.module.handle("yield", server)
                    self.clock.advance(0.8)
                    self.module.handle("ping", server)
                    self.module.tick()
                    self.assertIsNone(process.poll(), "Yield must not stop the managed child")
                    self.module.disconnect(server)
                    self.assertIn("lost its lease", self.module.last_error)
                finally:
                    self.controller.loaded = False
                    self.module.tick()
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline and self.module.process is not None:
                        self.clock.advance(0.01)
                        self.module.tick()
                        time.sleep(0.01)
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=3)
                    self.assertIsNone(self.module.process, "Private launcher was not reaped")
                    if peer_pid is not None and peer_pid != process.pid:
                        try:
                            status = (Path("/proc") / str(peer_pid) / "stat").read_text()
                        except FileNotFoundError:
                            status = ""
                        self.assertTrue(not status or status.rsplit(")", 1)[1].split()[0] == "Z",
                                        "Private worker survived its launcher's session cleanup")
                    self.controller.config["agent_selected"] = "autodoom"
                    self.module.tick()
                    self.controller.loaded = True
                    self.module.tick()

    def test_real_private_child_receives_paths_and_is_stopped_on_unload(self):
        folder = self.manifest(command=[sys.executable, "agent.py"])
        marker = folder / "started.json"
        (folder / "agent.py").write_text(
            "import json, os, pathlib, time\n"
            "pathlib.Path('started.json').write_text(json.dumps({'cwd':os.getcwd(),"
            "'socket':os.environ['DOOM_AGENT_SOCKET'],'frame':os.environ['DOOM_AGENT_FRAME']}))\n"
            "print('private agent ready', flush=True)\n"
            "time.sleep(60)\n")
        self.controller.config["agent_selected"] = "wander"
        self.module.tick()
        process = self.module.process
        self.assertIsNotNone(process)
        try:
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not marker.exists():
                if process.poll() is not None:
                    self.fail((self.logs / "wander.log").read_text())
                time.sleep(0.01)
            self.assertTrue(marker.exists(), "Private child startup timed out")
            value = json.loads(marker.read_text())
            self.assertEqual(value, {"cwd": str(folder), "socket": str(self.controller.runtime / "control.sock"),
                                     "frame": str(self.controller.runtime / "frame.bin")})
            self.controller.loaded = False
            self.module.tick()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and self.module.process is not None:
                self.clock.advance(0.01)
                self.module.tick()
                time.sleep(0.01)
            self.assertIsNone(self.module.process, "Private child was not reaped")
            self.assertIsNotNone(process.poll())
            self.assertIn("private agent ready", (self.logs / "wander.log").read_text())
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
