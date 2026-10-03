# SPDX-License-Identifier: 0BSD
"""SDK and example-agent tests against doomagent_fake (no engine, no desktop).

    python3 -m unittest discover -s sdk/python/tests -v
numpy-only checks are skipped when numpy is missing.
"""
import math
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SDK = HERE.parent
ROOT = SDK.parent.parent
sys.path.insert(0, str(SDK))

import doomagent  # noqa: E402
from doomagent import Agent, AgentError, Busy, FrameReader  # noqa: E402
from doomagent_fake import FakeGame  # noqa: E402

try:
    import numpy  # noqa: F401
    HAVE_NUMPY = True
except ImportError:
    HAVE_NUMPY = False


def wait_until(pred, timeout=3.0, step=0.02):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


class HeaderLayout(unittest.TestCase):
    @unittest.skipUnless(shutil.which("cc"), "needs a C compiler")
    def test_matches_bridge_h(self):
        """Every field the SDK reads sits at the C offset in src/bridge.h."""
        fields = [n for n, _ in doomagent._HeaderV3._fields_]
        body = "".join(f'printf("{n} %zu\\n", offsetof(DDHeader, {n}));' for n in fields)
        src = ("#include <stdio.h>\n#include <stddef.h>\n#include \"bridge.h\"\n"
               "int main(void){" + body + 'printf("sizeof %zu\\n", sizeof(DDHeader)); return 0;}')
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "o.c").write_text(src)
            subprocess.run(["cc", "-I", str(ROOT / "src"), "-o", f"{d}/o", f"{d}/o.c"], check=True)
            out = dict(line.split() for line in subprocess.run([f"{d}/o"], capture_output=True, text=True,
                                                                 check=True).stdout.splitlines())
        for name in fields:
            self.assertEqual(getattr(doomagent._HeaderV3, name).offset, int(out[name]), name)
        self.assertEqual(ctypes_size(doomagent._HeaderV3), int(out["sizeof"]))
        for name, _ in doomagent._HeaderV2._fields_:   # v3 appends only
            self.assertEqual(getattr(doomagent._HeaderV2, name).offset, getattr(doomagent._HeaderV3, name).offset)


def ctypes_size(cls):
    import ctypes
    return ctypes.sizeof(cls)


class RuntimeDir(unittest.TestCase):
    def env(self, **values):
        keys = ("LIVE_DOOM_RUNTIME", "DOOM_DESKTOP_RUNTIME", "XDG_RUNTIME_DIR")
        saved = {k: os.environ.get(k) for k in keys}
        for k in keys:
            os.environ.pop(k, None)
        os.environ.update(values)
        self.addCleanup(lambda: [os.environ.pop(k, None) or (v is not None and os.environ.__setitem__(k, v))
                                 for k, v in saved.items()])

    def test_xdg_runtime_dir(self):
        self.env(XDG_RUNTIME_DIR="/run/user/4242")
        self.assertEqual(doomagent.runtime_dir(), Path("/run/user/4242/live-doom"))

    def test_override_and_legacy_name(self):
        self.env(XDG_RUNTIME_DIR="/run/user/4242", DOOM_DESKTOP_RUNTIME="/x/old")
        self.assertEqual(doomagent.runtime_dir(), Path("/x/old"))
        os.environ["LIVE_DOOM_RUNTIME"] = "/x/new"
        self.assertEqual(doomagent.runtime_dir(), Path("/x/new"))

    def test_refuses_shared_or_foreign_dir(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d)
        os.chmod(d, 0o700)
        doomagent.check_private_dir(d)                       # ours and private: fine
        os.chmod(d, 0o755)
        with self.assertRaises(AgentError):
            doomagent.check_private_dir(d)
        with self.assertRaises(AgentError):
            Agent("t", socket_path=d / "control.sock", frame_path=d / "frame.bin").connect()

    def test_no_tmp_fallback(self):
        self.env()
        with self.assertRaises(AgentError):
            doomagent.runtime_dir()


class FrameReading(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d)

    def _write(self, path, seq, version=3, w=4, h=2, magic=doomagent.MAGIC, fill=b"\x01\x02\x03\x00"):
        hdr = doomagent._HeaderV3()
        hdr.magic, hdr.version, hdr.seq, hdr.width, hdr.height, hdr.stride = magic, version, seq, w, h, w * 4
        hdr.map, hdr.tic, hdr.x, hdr.angle = b"E1M2", 77, 3 * 65536, 1 << 30
        raw = bytes(hdr).ljust(doomagent.HEADER_AREA, b"\0") + fill * (w * h)
        # Replacing the inode keeps an already-mapped reader valid until it
        # reopens. Truncating the mapped inode can SIGBUS concurrent readers.
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".frame-", delete=False) as f:
            temporary = Path(f.name)
            f.write(raw)
        try:
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def test_missing_bad_and_good(self):
        r = FrameReader(self.d / "frame.bin")
        self.addCleanup(r.close)
        self.assertIsNone(r.read())
        self._write(r.path, 2, magic=0)
        self.assertIsNone(r.read())
        self._write(r.path, 2)
        obs = r.read()
        self.assertEqual((obs.map, obs.tic, obs.pos, round(obs.angle_deg)), ("E1M2", 77, (3.0, 0.0), 90))
        self.assertEqual(obs.rgb_bytes()[:3], b"\x03\x02\x01")       # B,G,R,X -> R,G,B

    def test_waits_out_a_publish(self):
        r = FrameReader(self.d / "frame.bin")
        self.addCleanup(r.close)
        self._write(r.path, 3)                                  # odd: mid-publish
        publish = threading.Timer(0.02, self._write, (r.path, 4))
        self.addCleanup(publish.join)
        publish.start()                                       # atomically replaced when done
        obs = None
        end = time.monotonic() + 1
        while obs is None and time.monotonic() < end:
            obs = r.read()
        self.assertIsNotNone(obs)
        self.assertEqual(obs.seq, 4)

    def test_reopens_after_engine_restart(self):
        r = FrameReader(self.d / "frame.bin")
        self.addCleanup(r.close)
        self._write(r.path, 2)
        self.assertEqual(r.read().tic, 77)
        r.path.unlink()
        self.assertIsNone(r.read())
        self._write(r.path, 2, w=8, h=1)
        self.assertEqual(r.read().width, 8)

    def test_v2_frames_still_read(self):
        r = FrameReader(self.d / "frame.bin")
        self.addCleanup(r.close)
        self._write(r.path, 2, version=2)
        obs = r.read()
        self.assertEqual(obs.version, 2)
        self.assertNotIn("owner", obs.header)

    @unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
    def test_rgb_numpy(self):
        r = FrameReader(self.d / "frame.bin")
        self.addCleanup(r.close)
        self._write(r.path, 2, w=8, h=4)
        img = r.read().rgb()
        self.assertEqual(img.shape, (4, 8, 3))
        self.assertEqual(tuple(img[0, 0]), (3, 2, 1))
        self.assertEqual(r.read().rgb(downscale=(4, 2)).shape, (2, 4, 3))


class Protocol(unittest.TestCase):
    def setUp(self):
        self.g = FakeGame().start()
        self.addCleanup(self.g.stop)
        self.addCleanup(shutil.rmtree, self.g.dir, True)

    def agent(self, name="t", **kw):
        a = Agent(name, socket_path=self.g.socket_path, frame_path=self.g.frame_path, **kw).connect()
        self.addCleanup(a.close)
        return a

    def test_hello_act_and_hold_watchdog(self):
        a = self.agent()
        self.assertEqual((a.owner, a.schema, a.paused), ("agent", 3, None))
        tic = a.act(turn=10, wait=True)
        self.assertIsInstance(tic, int)
        time.sleep(0.9)                                   # > hold_ms: the turn stops by itself
        held = a.observe(pixels=False).angle_deg
        time.sleep(0.3)
        self.assertAlmostEqual(a.observe(pixels=False).angle_deg, held, places=3)
        self.assertGreater(held, 100)                     # ~17 tics of 10 degrees were applied

    def test_second_agent_is_busy(self):
        self.agent("first")
        with self.assertRaises(Busy):
            self.agent("second")

    def test_heartbeat_keeps_lease_and_silence_loses_it(self):
        a = self.agent()
        time.sleep(2.5)                                   # 2.5 x ttl with only heartbeats
        self.assertTrue(a.connected)
        self.assertEqual(self.g.owner, "agent")
        raw = socket.socket(socket.AF_UNIX)
        a.close()
        # the game releases the old lease asynchronously; a new hello before that is (correctly) busy
        self.assertTrue(wait_until(lambda: self.g.owner == "autodoom", timeout=1.0))
        raw.connect(str(self.g.socket_path))
        raw.sendall(b"agent hello silent 1\n")
        self.assertTrue(raw.recv(256).startswith(b"OK owner=agent"))
        self.assertTrue(wait_until(lambda: self.g.owner == "autodoom", timeout=2.5))   # no line within ttl
        raw.close()

    def test_close_returns_control_at_once(self):
        a = self.agent()
        a.close()
        self.assertTrue(wait_until(lambda: self.g.owner == "autodoom", timeout=0.5))
        self.assertFalse(a.connected)
        with self.assertRaises(AgentError):
            a.ping()

    def test_human_preempts_and_hands_back(self):
        a = self.agent()
        a.act(move=1)
        self.g.human_takeover()
        self.assertTrue(a.wait_for(lambda x: x.owner == "human", 1))
        self.assertIs(a.act(move=1), False)               # not sent while someone else drives
        self.assertIsNone(self.g.action)                  # held action released to neutral
        self.g.human_release()
        self.assertTrue(a.wait_for_control(1))

    def test_paused_actions_are_dropped_not_replayed(self):
        a = self.agent()
        self.g.pause("busy")
        self.assertTrue(a.wait_for(lambda x: x.paused == "busy", 1))
        self.assertIs(a.act(move=1), False)
        a._send("act move=1 strafe=0 turn=20 fire=0 use=0 run=1 weapon=0", wait=True)   # straight to the game
        before = self.g.angle
        self.g.resume()
        self.assertTrue(a.wait_for(lambda x: x.paused is None, 1))
        time.sleep(0.3)
        self.assertEqual(self.g.angle, before)            # the paused act was never applied

    def test_hello_reports_initial_pause(self):
        self.g.pause("lock")
        a = self.agent()
        self.assertEqual(a.paused, "lock")
        self.assertFalse(a.driving)

    def test_yield_take_replan_and_events(self):
        a = self.agent()
        a.yield_to_bot()
        self.assertTrue(a.wait_for(lambda x: x.owner == "autodoom", 1))
        a.take()
        self.assertTrue(a.wait_for_control(1))
        a.yield_to_bot(0.3)
        self.assertTrue(a.wait_for(lambda x: x.owner == "autodoom", 1))
        self.assertTrue(a.wait_for(lambda x: x.owner == "agent", 2))   # timed yield ends by itself
        a.replan()
        self.assertEqual(self.g.replans, 1)
        self.g.new_level("E1M2")
        self.g.kill_player()
        self.assertTrue(wait_until(lambda: any(k == "death" for k, _ in a._events)))
        kinds = [k for k, _ in a.poll_events()]
        self.assertIn("level", kinds)
        self.assertEqual(a.map, "E1M2")

    def test_errors_surface_on_next_call(self):
        a = self.agent()
        a._send("act bogus=1", wait=False)
        self.assertTrue(wait_until(lambda: a.last_error is not None))
        time.sleep(0.6)                                   # heartbeats must neither eat it nor stop
        with self.assertRaises(AgentError):
            a.act(move=1)
        a.act(move=1)                                     # reported once, then clear
        time.sleep(1.5)
        self.assertTrue(a.connected)                      # the heartbeat kept the lease

    def test_ownership_race_is_not_an_error(self):
        """An act already in flight when a human takes over gets "ERR not owner"; that must
        neither raise on the next call nor count as a protocol error."""
        a = self.agent()
        self.g.human_takeover()
        a._send("act move=1 strafe=0 turn=0 fire=0 use=0 run=1 weapon=0", wait=False)   # raced
        self.assertTrue(wait_until(lambda: a.last_error == "not owner"))
        self.assertTrue(a.wait_for(lambda x: x.owner == "human", 1))
        a.ping()                                          # no stale error raised
        self.g.human_release()
        self.assertTrue(a.wait_for_control(1))
        a.owner = "agent"
        self.g.human_takeover()                           # SDK still thinks it drives
        self.assertIs(a.act(move=1, wait=True), False)

    def test_act_validates_and_clamps(self):
        a = self.agent()
        with self.assertRaises(ValueError):
            a.act(weapon=12)
        with self.assertRaises(ValueError):
            a.act(turn=float("nan"))
        a.act(move=5, strafe=-5, turn=900, wait=True)
        self.assertEqual(self.g.log[-1], "act move=1 strafe=-1 turn=45 fire=0 use=0 run=1 weapon=0")

    def test_game_shutdown_closes_agent(self):
        a = self.agent()
        self.g.stop()
        self.assertTrue(a.wait_for(lambda x: not x.connected, 2))
        self.assertIn(("shutdown", None), a.poll_events())
        self.g.stop = lambda: None                         # already stopped


class Examples(unittest.TestCase):
    def run_agent(self, script, game, args=(), seconds=6.0):
        env = dict(os.environ, DOOM_DESKTOP_RUNTIME=str(game.dir), PYTHONPATH=str(SDK))
        p = subprocess.Popen([sys.executable, str(ROOT / "examples/agents" / script), *args], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return p

    def test_unstick_escapes_dead_end_quickly(self):
        """AutoDoom (fake) walks into a dead end; with a 2 s window Unstick takes control,
        escapes, replans and yields back well before the native 60 s detector would."""
        with FakeGame(stuck_after=60) as g:
            p = self.run_agent("unstick/unstick.py", g, ["--window", "2"])
            try:
                self.assertTrue(wait_until(lambda: g.replans >= 1, timeout=9), "no escape")
                self.assertTrue(wait_until(lambda: g.owner == "autodoom", timeout=2))
                self.assertGreater(math.dist((g.x, g.y), (512, 0)), 150)
                verbs = [line.split()[0] for line in g.log if not line.startswith(("ping", "act"))]
                self.assertEqual(verbs[:5], ["agent", "yield", "take", "replan", "yield"])
            finally:
                p.terminate()
                out = p.communicate(timeout=5)[0]
            self.assertIn("escaped towards", out)
            self.assertEqual(g.bot_stuck, 0)                 # the native detector never fired

    def test_unstick_reacts_to_native_detector(self):
        with FakeGame(stuck_after=1.0) as g:
            p = self.run_agent("unstick/unstick.py", g, ["--window", "30"])
            try:
                self.assertTrue(wait_until(lambda: g.replans >= 1, timeout=8), "no escape")
            finally:
                p.terminate()
                out = p.communicate(timeout=5)[0]
            self.assertIn("AutoDoom's detector fired", out)

    def test_wander_drives_and_turns_at_walls(self):
        with FakeGame(start=(900.0, 0.0, 0.0)) as g:           # facing the east wall
            p = self.run_agent("wander/wander.py", g)
            try:
                self.assertTrue(wait_until(lambda: g.owner == "agent", timeout=3))
                self.assertTrue(wait_until(lambda: abs(g.angle) > 1 and abs(g.angle - 360) > 1, timeout=4))
                time.sleep(1.0)
                self.assertTrue(any(line.startswith("act") and "move=1 " in line for line in g.log))
            finally:
                p.terminate()
                p.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
