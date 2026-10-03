#!/usr/bin/env python3
# SPDX-License-Identifier: 0BSD
"""Wanderer: the smallest useful agent, and a template for your own.

It walks, turns away when it stops making progress (a wall), presses use as it goes
(doors, lifts, switches) and fires back while it is being hurt. It reads game state only;
Observation.rgb() gives you the picture when you want vision. For a hybrid that leaves the
playing to AutoDoom and only steps in when it looks stuck, see ../unstick.
"""
import math
import os
import random
import sys
from collections import deque


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

WINDOW = 12            # tics of movement history used to notice a wall
MIN_PROGRESS = 24.0    # map units over that window


def main() -> int:
    try:
        agent = doomagent.Agent("Wanderer").connect()
    except doomagent.Busy as e:
        print(f"another agent is driving: {e}", file=sys.stderr)
        return 3
    except (OSError, doomagent.AgentError) as e:
        print(f"cannot reach the game: {e}", file=sys.stderr)
        return 1
    history = deque(maxlen=WINDOW)
    turn_left = 0          # tics of turning still to do
    turn_dir = 1
    fire_left = 0
    last_damage = 0
    with agent:
        while agent.connected:
            obs = agent.next_observation(pixels=False)
            if obs is None or not agent.driving:
                history.clear()
                continue
            if not obs.in_level or not obs.alive:
                agent.stop()                      # the game handles respawn and intermissions
                history.clear()
                continue
            history.append(obs.pos)
            damage = obs.header.get("damage_count", 0)
            if damage > last_damage:
                fire_left = 25                    # hurt: fire and sweep for a while
            last_damage = damage
            if turn_left == 0 and len(history) == WINDOW and math.dist(history[0], history[-1]) < MIN_PROGRESS:
                turn_left, turn_dir = random.randint(6, 18), random.choice((-1, 1))
                history.clear()
            turning = turn_left > 0
            sweep = 3 * math.sin(obs.tic / 6) if fire_left else 0
            agent.act(move=0.3 if turning else 1.0,
                      turn=9 * turn_dir if turning else sweep,
                      use=obs.tic % 20 < 2,
                      fire=fire_left > 0)
            turn_left = max(0, turn_left - 1)
            fire_left = max(0, fire_left - 1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
