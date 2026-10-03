#!/usr/bin/env python3
# SPDX-License-Identifier: 0BSD
"""Unstick: AutoDoom plays; this agent steps in only when it is stuck.

AutoDoom sometimes pins itself against a wall, a ledge or a door it cannot open, and the
native watchdog waits 25 s of world time before rerouting. Unstick watches the same game state and reacts
after --window seconds of world time (default 10) without progress,
or at once when AutoDoom's own detector fires (header bot_stuck / EVENT stuck). It takes
control, probes eight headings by walking a few steps each (pressing use as it goes, for
doors and switches), commits to the one that got furthest towards the least-visited ground,
asks AutoDoom to replan and hands control back. It is an example of hinting, not a
guarantee: it cannot leave a room that has no way out, and it has not been tested over a
whole campaign.

    python3 unstick.py [--window SECONDS] [--progress MAP_UNITS]
"""
import argparse
import math
import os
import sys
import time
from collections import Counter, deque


def _import_sdk():
    try:
        import doomagent
        return doomagent
    except ImportError:
        here = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, os.environ.get("DOOM_AGENT_SDK") or os.path.join(here, "..", "..", "..", "sdk", "python"))
        import doomagent
        return doomagent


doomagent = _import_sdk()

TICRATE = 35
CELL = 128.0              # visit-map resolution, map units
PROBE_TICS = 12           # steps walked per probed heading
COMMIT_TICS = 45          # steps walked along the chosen heading
CLEAR = 96.0              # a probe this long on fresh ground is taken at once
COOLDOWN_S = 5            # world seconds before another escape


def log(msg):
    print(f"unstick: {msg}", flush=True)


def cell(x, y):
    return (math.floor(x / CELL), math.floor(y / CELL))


def wrap(deg):
    return (deg + 540.0) % 360.0 - 180.0


class Unstick:
    def __init__(self, agent, window_s, progress):
        self.agent = agent
        self.window = int(window_s * TICRATE)
        self.progress = progress
        self.visits = Counter()
        self.history = deque()             # (tic, x, y, kills + items + secrets)
        self.baseline = None               # (engine pid, bot_stuck)
        self.cooldown = 0
        self.map = None

    # -- watching AutoDoom ----------------------------------------------------
    def reset(self):
        self.history.clear()

    def stuck_reason(self, obs, events):
        h = obs.header
        pid, stuck = h.get("pid"), h.get("bot_stuck")
        native = None
        if stuck is not None:
            if self.baseline is None or self.baseline[0] != pid:
                self.baseline = (pid, stuck)          # new engine: new counter
            elif stuck > self.baseline[1]:
                self.baseline = (pid, stuck)
                native = "AutoDoom's detector fired"
        if any(kind == "stuck" for kind, _ in events):
            native = native or "AutoDoom's detector fired"
        if obs.map != self.map:
            self.map, self.visits = obs.map, Counter()
            self.reset()
        if not obs.in_level or not obs.alive or self.agent.paused is not None:
            self.reset()
            return None
        x, y = obs.pos
        self.visits[cell(x, y)] += 1
        score = h["kills"] + h["items"] + h["secrets"]
        self.history.append((obs.tic, x, y, score))
        while self.history and obs.tic - self.history[0][0] > self.window:
            self.history.popleft()
        if obs.tic < self.cooldown:
            return None
        if native:
            return native
        first = self.history[0]
        if obs.tic - first[0] >= self.window * 0.95 and first[3] == score:
            spread = max(math.dist((x, y), (t[1], t[2])) for t in self.history)
            if spread < self.progress:
                return f"under {self.progress:.0f} units in {self.window / TICRATE:.0f} s"
        return None

    # -- driving ---------------------------------------------------------------
    def step(self, **act):
        """Hold `act` for one world tic; None when control was lost."""
        if not self.agent.driving or self.agent.act(**act) is False:
            return None
        return self.agent.next_observation(pixels=False)

    def face(self, heading, obs):
        for _ in range(40):
            delta = wrap(heading - obs.angle_deg)
            if abs(delta) < 4:
                break
            turn = max(-30.0, min(30.0, delta if abs(delta) > 30 else delta / 2))   # damp: one tic of lag
            obs = self.step(turn=turn, run=False)
            if obs is None:
                return None
        return obs

    def walk(self, heading, obs, tics):
        obs = self.face(heading, obs)
        if obs is None:
            return None, 0.0
        start = obs.pos
        for i in range(tics):
            wiggle = 0.3 * math.sin(i / 3) if tics > PROBE_TICS else 0.0
            obs = self.step(move=1, strafe=wiggle, use=i % 8 < 2)
            if obs is None:
                return None, 0.0
            self.visits[cell(*obs.pos)] += 1
        return obs, math.dist(start, obs.pos)

    def unvisited_ahead(self, obs, heading):
        x, y = obs.pos
        r = math.radians(heading)
        return self.visits[cell(x + 2 * CELL * math.cos(r), y + 2 * CELL * math.sin(r))]

    def escape(self, obs, reason):
        a = self.agent
        x0, y0 = obs.pos
        log(f"stuck on {obs.map} at ({x0:.0f}, {y0:.0f}): {reason}")
        try:
            a.take()
        except doomagent.AgentError as e:
            log(f"could not take control: {e}")
            return
        if not a.wait_for_control(timeout=1.0):
            log("control did not arrive; leaving it to AutoDoom")
            self.yield_back(obs, replan=True)
            return
        base = obs.angle_deg
        headings = sorted((base + k * 45.0) % 360 for k in range(8))
        headings.sort(key=lambda hd: (self.unvisited_ahead(obs, hd), abs(wrap(hd - base - 180))))
        best, best_score = None, -1.0
        for hd in headings:
            ahead = self.unvisited_ahead(obs, hd)
            obs, moved = self.walk(hd, obs, PROBE_TICS)
            if obs is None:
                log("lost control mid-escape")
                return self.yield_back(None, replan=False)
            score = moved / (1 + 0.25 * ahead)
            if score > best_score:
                best, best_score = hd, score
            if moved >= CLEAR and ahead == 0:
                break
        if best is not None and best_score > 0:
            obs, _ = self.walk(best, obs, COMMIT_TICS)
        x1, y1 = obs.pos if obs is not None else (x0, y0)
        log(f"escaped towards {best:.0f}°, {math.dist((x0, y0), (x1, y1)):.0f} units from where it stuck")
        self.yield_back(obs, replan=True)

    def yield_back(self, obs, replan):
        a = self.agent
        try:
            if a.driving:
                a.stop()
            if replan:
                a.replan()
            a.yield_to_bot()
        except doomagent.AgentError as e:
            log(f"could not hand back: {e}")
        self.reset()
        if obs is not None:
            self.cooldown = obs.tic + COOLDOWN_S * TICRATE

    # -- main loop ---------------------------------------------------------------
    def run(self):
        a = self.agent
        log(f"watching AutoDoom (reacts after {self.window / TICRATE:.0f} s without progress)")
        while a.connected:
            obs = a.next_observation(pixels=False)
            events = a.poll_events()
            if obs is None:
                continue
            if a.owner == "agent":
                # Control came to us (connect, or a human finished playing): AutoDoom drives.
                self.yield_back(obs, replan=False)
                continue
            if a.owner != "autodoom":            # a human plays, or someone else drives
                self.reset()
                continue
            reason = self.stuck_reason(obs, events)
            if reason:
                self.escape(obs, reason)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--window", type=float, default=float(os.environ.get("UNSTICK_WINDOW", 10)),
                   help="seconds of world time without progress before stepping in (default 10)")
    p.add_argument("--progress", type=float, default=96.0, help="map units that count as progress (default 96)")
    args = p.parse_args()
    try:
        agent = doomagent.Agent("Unstick").connect()
    except doomagent.Busy as e:
        print(f"another agent is driving: {e}", file=sys.stderr)
        return 3
    except (OSError, doomagent.AgentError) as e:
        print(f"cannot reach the game: {e}", file=sys.stderr)
        return 1
    with agent:
        Unstick(agent, args.window, args.progress).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
