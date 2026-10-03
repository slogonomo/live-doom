# External agents: protocol v1

How a program of your own drives Live Doom's game in place of the built-in AutoDoom bot.
Reinforcement-learning stepping (seeded reset, exact `step(action, tics)`, rewards) is **out of
scope**. For training, use ViZDoom and run the resulting policy here through this interface.

## Goal

Any program, in any language, can drive the live game in place of AutoDoom. It sees the frames
and game state the viewers already use and sends actions. AutoDoom stays the fallback, and a
human always wins. A useful bot fits in about 50 lines of Python.

Non-goals: a plugin framework inside the engine, network exposure, console/menu/save access for
agents, and structured world state (enemy lists, map geometry) beyond a few header fields.

## Ownership

`human > agent > autodoom`, one agent at a time.

- The agent drives wherever AutoDoom would: empty desktops, and the screensaver unless
  `agent.screensaver` is false. Normal policy still applies. Busy, locked or asleep outputs pause
  the world, and the agent's actions are ignored while paused, but its lease survives.
- Human takeover preempts the agent: the agent's held action is cleared and it gets an event. When
  the human releases, a connected agent gets control back automatically.
- Losing the agent (EOF, a missed heartbeat, a crash) hands control back to AutoDoom with no held
  input left behind: at EOF, or at the one-second lease deadline plus a bounded native dispatch
  (about 1.03 s observed), not a strict wall-clock guarantee. The controller handles synchronous commands one at a time,
  so `use-live-wallpaper`, `remove-live-wallpaper`, `open-menu` and `settings` can delay lease and
  EOF handling while they run. There's no hard wall-clock fallback during those transitions. The
  engine's own watchdog doesn't depend on the controller: 500 ms of world tics after the last act,
  the held command goes neutral anyway.

## Action seam: per-tic commands, not keys

Each tic, `g_game.cpp` asks the bot to fill the player's `ticcmd_t` (`bots[i].doCommand()`,
`b_think.cpp`). An agent supplies that command instead. The bridge keeps the agent's latest
action and copies it into `pl->cmd` on every tic while the agent owns control (sample and hold).
This avoids key bindings, mouse sensitivity and held-key bookkeeping entirely.

Engine watchdog: if no new action arrives within `hold_ms` (default 500 ms, measured in world
tics so a pause doesn't count), the command becomes neutral. A hung agent then stands still and
holds nothing, and the controller's lease expiry soon returns control to AutoDoom.

Death respawn, intermission advance and finale continuation stay with the bridge in agent mode,
as they are for AutoDoom today. They're split from AutoDoom navigation, so an agent only has to
play levels.

## Protocol

Everything goes over the existing private `control.sock` (0700 runtime dir, 0600 socket),
newline-delimited, on **one persistent connection**. The connection *is* the lease, so there are
no tokens.

```text
→ agent hello NAME 1                      # protocol version 1
← OK owner=agent|waiting gen=7 ttl_ms=1000 hold_ms=500 schema=3
← EVENT owner agent                       # the current owner and pause state follow at once
← EVENT paused none
→ act move=1 strafe=0 turn=-4.5 fire=1 use=0 run=1 weapon=0
← OK tic=123456                           # next eligible tic at acceptance (informational)
→ ping                                    # each accepted verb renews the lease; send ≥ every 250 ms
← OK tic=123460
→ yield 30                                # let AutoDoom drive for 30 s (or "yield" = until take)
→ take                                    # resume control early
→ replan                                  # same as Reroute bot, useful just before yielding
→ bye                                     # release now; AutoDoom resumes
```

`act` fields; anything omitted is 0, and each `act` replaces the whole held action:

| field | range | maps to |
| --- | --- | --- |
| `move` | −1…1 | `forwardmove` (±1 = run speed if `run=1`, else walk) |
| `strafe` | −1…1 | `sidemove` (+ = right) |
| `turn` | −45…45 | degrees **per tic**, + = left (`angleturn`) |
| `fire`, `use` | 0/1 | `BT_ATTACK`, `BT_USE` |
| `run` | 0/1 | speed class for move/strafe |
| `weapon` | 0…9 | 0 = keep; else `BT_CHANGE` to that slot |

`OK tic=N` reports the next eligible world tic when the action was accepted. It is
informational, not a stepping barrier: the world keeps its 35 Hz clock. While the world is
paused, actions are ignored (the held command is neutral) and are **not** replayed on resume.
The starting owner and pause state arrive as the first `EVENT owner` / `EVENT paused` lines after
hello (a `paused=` field in the hello reply is also accepted by the SDK).

Asynchronous events arrive on the same connection as lines starting with `EVENT`, in order with
the replies:
`EVENT owner agent|human|autodoom|waiting`, `EVENT paused busy|lock|asleep|menu|none`,
`EVENT level MAP`, `EVENT death`, `EVENT stuck` (header `bot_stuck` rose: the native no-progress
watchdog, 25 s of world time without new ground or pickups, not explicit reroutes),
`EVENT shutdown`.

Errors are `ERR <reason>`: `busy <other-agent>` when an agent already holds control,
`version`, `field <name>`, `not owner`. Nothing else is accepted on an agent connection: no
`key`, `text`, `mouse`, console, menu, quit, save/load or `set`.

## Observation

Reading `frame.bin` is unchanged: the seqlock copy, header plus pixels, as in `viewer.c`. Header
protocol **v3** appends fields only (existing offsets stay):
`owner` (0 autodoom, 1 agent, 2 human), `agent_gen`, `armor`, `ammo[4]`, `ready_weapon`,
`weapons_owned` (bitmask), `keys` (bitmask), `z`, `momx`, `momy`, `damage_count`, `deaths`,
`levels_completed`, `bot_stuck` (in that order; owner/agent_gen/weapons_owned/keys/deaths/
levels_completed/bot_stuck are uint32, the rest int32). `bot_stuck` counts only the native watchdog's
no-progress detections (25 s of world time without new ground or pickups); its baseline resets with a new engine `pid`. `tic` identifies the world
step; agents act on the newest observation and must not assume one image per tic.

`frame` is the **image revision**: it advances only when the published pixels change. A paused
world, an open menu or a still view keeps the same image and the same `frame` while `tic` and the
rest of the header keep updating, so wait on `tic` (as the SDK's `next_observation()` does) or on
the seqlock, never on `frame`. Active play renders at 35 frames a second, Doom's tic rate.

Agents downscale images themselves (the SDK does this). The engine renders at the output's size
for the live background and won't change for agents.

## Launching agents ("plugins")

`~/.config/live-doom/agents/<id>/agent.json`:

```json
{ "name": "Wanderer", "command": ["python3", "wander.py"], "screensaver": true }
```

The installer bundles the two examples as `example-wander` and `example-unstick`, each with a
local copy of `doomagent.py` so the installed scripts import it directly. Updates keep user edits
to those files (`install.py --replace-modified` replaces them); copying an example to a new id keeps
a custom bot separate. The default driver stays `autodoom`.

The setting `agent.selected` (`"autodoom"` or an id) picks the driver. The controller runs the
selected agent as a child process: working directory is its folder, logs go to
`~/.local/state/live-doom/agents/<id>.log`, restarts use backoff (1, 2, 4 … 60 s), and it's
stopped when the game unloads. It gets `DOOM_AGENT_SOCKET` and `DOOM_AGENT_FRAME` in its
environment. A supervised agent has 5 s after launch to send `hello`. Connect first (`Agent(...)`
starts the heartbeat) and load models afterwards, so a slow start doesn't cost the slot. The
controller recognises the agent it launched by the connecting peer's PID, not by
the name sent in `hello`, so an agent may call itself anything. Agents started by hand (for
development) connect the same way; they get `busy` while the selected agent is running.

The settings JSON gains `agent: { selected, available: [{id, name}], connected, owner, last_error }`.
The menu adds a **Driver** choice (AutoDoom, then each available agent) in the Game section, with
a connection note under it. Trust is the user's own: the agent folder is theirs, the same as
their shell.

## SDK and examples (in the repo, stdlib-only Python, numpy optional)

`sdk/python/doomagent.py` (`doomagent_fake.py` is a stand-in game for development and tests):

- `with Agent("name") as a:` connects, sends `hello`, runs a heartbeat thread and releases on exit.
- `a.owner`, `a.paused`, `a.driving` follow the events; `act()` returns False without sending
  while the agent is not driving.
- `a.observe(pixels=True)` returns an `Observation` (or None while no game is loaded): `.header`
  (every field, also as attributes, plus `pos`, `angle_deg`, `owner_name`, `in_level`, `alive`),
  `.data` (raw XRGB rows), `.rgb(downscale=(w, h))` (HxWx3 numpy array), `.rgb_bytes()` and
  `.save_ppm()`. `a.next_observation()` waits for the next world tic.
- `a.act(**fields)`, `a.yield_to_bot(seconds)`, `a.take()`, `a.replan()`.
- `a.poll_events()` returns the events since the last call as `(kind, arg)` pairs;
  `a.wait_for(predicate)` and `a.wait_for_control()` block on state changes.

Examples:

1. `examples/agents/wander/`: a reactive pixel-and-state walker. It's the minimal template.
2. `examples/agents/unstick/`: the hybrid. AutoDoom drives (`yield`). On `EVENT stuck`, or when x/y
   hasn't moved, it takes control: turn toward the least-visited direction, strafe and use, then
   replan and yield back. This targets the "bot gets stuck" problem directly.

## Tests

The tests that ship with the plugin cover this behaviour against stand-ins, not the engine:
- `sdk/python/tests` runs the SDK and both examples against `doomagent_fake`: lease and heartbeat, hold watchdog, busy, human preemption, paused actions dropped, ownership races, and unstick's detect, escape and hand-back at a dead end.
- `tests/test_agent_control.py` covers the controller's agent lease, supervision and events.
- `tests/test_native_lifecycle.py` exercises the real engine's checkpoint, defaults and driver seam.

Unstick *detects* a no-progress window well before the native 25 s watchdog, *intervenes* (takes control, probes, presses use) and *replans* before yielding. It can't leave a room that has no exit.
