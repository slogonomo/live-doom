# Live Doom settings console

The overlay entry point of the Live Doom plugin (`io.github.slogonomo.live-doom`; the root
`manifest.json` points at `menu/Menu.qml`). The controller summons it
(`omarchy-shell shell summon io.github.slogonomo.live-doom '{"output":"<name>"[,"origin":…]}'`) and the
origin picks the host:
- a right-click on the live background (no origin) gets the **overlay** (`MenuWindow.qml`): it
  belongs to the desktop, covers the right-clicked output, and holds the pause lease;
- the **Live Doom** entry in Apps (`origin: "launcher"`) and the first-run guide the installer opens
  (`origin: "onboarding"`) get a real **window** (`MenuFloat.qml`). Its minimum and maximum sizes
  are equal, which is what makes Hyprland float and centre it; Super+W closes it like any app, and
  it ends Omarchy's "Launching…" toast by appearing. It takes no lease: like any window it makes
  its workspace busy, so with **Show on: Empty desktops** the game pauses there, and with **All
  desktops** it keeps playing behind the window.

The host changes only while the menu is closed. It is styled like the game's own options menu: the IWAD's logo,
the red HUD lettering, the blinking skull cursor and thermometer sliders. All of it is read at
runtime from **your** IWAD by `wadfont.py` and cached in `~/.cache/live-doom/menu-font/`.
Nothing from the game ships here. With no IWAD it falls back to the mono UI font.

| File | Role |
|---|---|
| `Menu.qml` | state and behaviour: doomctl jobs, menu lease, settings model, actions |
| `MenuWindow.qml` | the layer-shell overlay host, placed on the right-clicked output |
| `MenuFloat.qml` | the floating window host, for Apps and the first-run guide |
| `MenuCard.qml` | the console card (pure view, so it can render offscreen) |
| `DoomText.qml`, `Thermo.qml` | IWAD-glyph text and thermometer slider |
| `wadfont.py` | extracts STCFN glyphs and menu patches from an IWAD (standard library only) |
| `artbox.py` | visible-pixel box of a Steam logo PNG, read in place (standard library only) |

Backend contract: in the overlay, `doomctl menu opened` once, from an
explicit open (it takes the lease and returns the game to the bot); `doomctl menu renew` every
2 s while open, which only extends a lease that is still held (the controller expires it after
6 s) and never stops a screensaver. The menu fails closed: any renew error (lease taken by the
idle screensaver or a preview, an older backend, doomctl unavailable) closes it, and it never
re-acquires from a heartbeat. `menu closed` on close. The window host sends none of these. Both use
`settings`, `set <key> <json>` (serialised; sliders and the WAD list are debounced),
`preview-screensaver`, `hint-bot`, `choose-iwad`, `add-wads`, `use-live-wallpaper`,
`remove-live-wallpaper`, `use-game`, `download-shareware --accept-terms` and
`onboarding step|finish|skip`. While a native file chooser is open the menu steps aside (no
keyboard grab) and reappears with fresh settings afterwards. **Preview** in the overlay closes
the menu first; in the window it leaves the window up and the saver covers it.

Live background with any theme: the game runs while the current background is a `live-doom`
one. When it is off, the menu offers **Use Doom with this theme**, which runs `doomctl
use-live-wallpaper`: the backend adds the bundled live background to the current theme's
background collection (or reuses one the theme already has) and selects it, returning only once
`settings` reports `wallpaper.active`. The theme, its colours and its other backgrounds stay as
they are; nothing switches themes. Picking another background turns it off again.
When `wallpaper.can_remove` is set (a live background the user added, never Phobos's own), the
menu also offers **Remove Doom from this theme** (`doomctl remove-live-wallpaper`, synchronous;
the backend switches to another background first if that one is selected). Both actions can
show at once for an added but unselected choice, and the cursor stays on the action or its
section, never on the opposite action, when the rows change.

Doom closes while no live background is selected and the screensaver starts it from the last
save. **Reroute bot** is hidden while `runtime.loaded` is false.

**First-run guide** (while `settings.onboarding.show`): the guide is the whole menu until it is
finished or skipped. Its steps come from `onboarding.steps`, in order:
1. **Live Background**: add to the current theme (`wallpaper.theme`), the Phobos theme, or skip.
   Phobos is offered from `onboarding.theme` (`{name, url, installed, active}`): installed but not
   active, it switches to it (`omarchy-theme-set phobos`); missing, with a trusted
   `https://github.com/...` URL, it installs it (`omarchy-theme-install <url>`).
2. **Choose your DOOM**, only when `onboarding.official_found` is false: Freedoom
   (`use-game freedoom`) or the shareware episode. Its first press shows
   `onboarding.shareware_terms` (text, source URL, sha256) followed by an **Accept and download**
   button at the end of the text; only that button runs `download-shareware --accept-terms`
   (refused in the first 800 ms, so a double-click can't accept unread terms), which also selects it.
   Pressing the episode's row again moves to the button. Once present, `use-game shareware`.
3. **Live Screensaver**: **Enable** (chosen by default on a fresh install, applied only at Finish) and **Preview**.
4. **Your setup**: what Finish will do, then **Finish**.

Only the game step acts at once. The background and screensaver choices are inert:
`onboarding step <id> --background current|phobos|skip --screensaver on|off` saves them with the
step after every change, so closing the menu (Esc) and reopening resumes the same place, and
**Back** never undoes anything. **Finish** applies them in order with the ordinary verbs (the theme
command, `use-live-wallpaper`, `set screensaver.enabled` when it differs), then
`onboarding finish`; a failure stops there and stays on the summary. **Skip setup and show all
settings** (`onboarding skip`) changes nothing else. Each step opens with the cursor on **Next**
(← reaches **Back**). Theme commands run from the menu's own session process: absolute `/usr/bin`
paths, a closed session environment, and a 190 s deadline. The game service never runs them.

Closing the menu declines open terms.

**Official games** lists the official releases the backend found in the Steam libraries
(`settings.game.catalog`; `doomctl scan-steam` looks again, and install time scans once). Each
Steam package gets a banner with its logo, read from Steam's local cache at runtime.
`artbox.py` measures the logo's visible pixels so the banner can clip away its transparent
padding, and nothing is copied. Playable games are actions: Enter runs `doomctl use-game ID`
(IWAD, PWADs and start map in one synchronous step) and the current one reads **Playing**.
Games that were found but can't run on this engine stay visible, dimmed and unselectable, marked
"(not supported)". **Find Steam games** rescans without changing the game.
**Get DOOM + DOOM II on Steam** opens `game.store_url` (a Steam store link only), and only when
chosen; the menu closes first so the browser gets the keyboard. With nothing found, a note
explains the store package. Editing the IWAD or WAD list by hand makes it custom play
(`selected_package` becomes null). **Live background** also offers **Install our Phobos theme** (or **Use our Phobos theme** when it is
installed but not active) whenever `onboarding.theme` allows it, so a skipped setup step stays reachable:
the theme command, then `use-live-wallpaper`. **Free games** lists Freedoom and the DOOM shareware episode
(`use-game freedoom|shareware`; `selected_package` reports `freedoom`/`shareware`), with the same
two-press terms flow when the episode isn't downloaded yet. Live-background changes, game switches
and scans run one at a time after any queued edits.

Keys: ↑/↓ (or j/k, Tab) select · ←/→ adjust (in the guide: Back / Next) · Enter/Space toggle or activate · Shift+↑/↓ or ←/→
reorder a WAD · Del/x remove a WAD · Esc or F12 close. The mouse works throughout, and the wheel
adjusts sliders.

The backend is the plugin's own `scripts/doomctl`, found relative to this folder. The menu reads no
config files and takes no command paths from the environment.

Every process the menu starts has a deadline above doomctl's own client budget for that command:
- 140 s for ordinary commands (budget 130 s);
- 190 s for a Phobos theme install or switch (Omarchy's own command, run by the menu);
- 310 s for `download-shareware` (300 s);
- 310 s for the lease calls, since a renew can wait out a bounded operation;
- 60 s for the font export;
- 15 s for logo measuring;
- 30 min for a file chooser a person is using. A hit deadline stops the process and fails the job, so a hung command cannot stall
the queues. Every `Text` renders `PlainText`, because labels, titles and backend reasons are data.

Every process the menu starts runs with a closed session environment: a fixed `PATH`, plus only
the session variables it needs.

Tests: `DOOM_MENU_TEST=1` skips both window hosts, and in that mode `DOOM_MENU_DEADLINE_MS`
shortens the doomctl deadline. Those two only change timing and hosting. No executable is ever
taken from the environment. The maintainers' offscreen scenario harness replaces the backend and
theme commands by setting the menu's `doomctl` and `themeCommand` properties when it loads the
component, so it can drive and snapshot the card without a desktop. The harness and its fixtures
live in the development tree and aren't part of the plugin.

Updates: Qt caches compiled QML by URL (including same-directory types), and the Omarchy shell
reloads changed plugins without clearing that cache. After `omarchy plugin update`, run
`omarchy restart shell` so the new menu code loads. The pure `loadedVersion()` function reports
the directory the running code was compiled from
(`omarchy-shell shell call io.github.slogonomo.live-doom loadedVersion`).
