# Licences and third-party notices

## This project

| What | Licence |
| --- | --- |
| Original source, scripts, tests, the menu, the SDK and the example agents, documentation | **0BSD** ([LICENSE](LICENSE)). Use it for anything, no attribution required. |
| Original art: [`assets/live-doom.webp`](assets/live-doom.webp) (procedural, generated for this project) | **CC0-1.0** ([LICENSES/CC0-1.0.txt](LICENSES/CC0-1.0.txt)) |
| [`preview.png`](preview.png): a screenshot of Live Doom playing Freedoom as the background, with its menu and Omarchy's background picker open | Our parts CC0-1.0: the menu and the LIVE DOOM still. The game imagery and the lettering the menu reads from the IWAD are Freedoom's, under BSD-3-Clause (see *Game data* below). The picker's other tiles are backgrounds from the separate Phobos theme (CC0-1.0, or BSD-3-Clause where derived from Freedoom). The background picker itself is Omarchy's interface (MIT) |
| [`patches/autodoom-desktop.patch`](patches/autodoom-desktop.patch): the desktop bridge applied to AutoDoom/Eternity, and any engine built from it | **GPL-3.0-or-later** ([LICENSES/GPL-3.0-or-later.txt](LICENSES/GPL-3.0-or-later.txt)), because it modifies GPL code |
| [`patches/sdl-mixer-device.patch`](patches/sdl-mixer-device.patch): lets the bundled SDL2_mixer release its audio device while muted and reopen it without restarting music, and the SDL2_mixer library built from it | **Zlib** ([LICENSES/Zlib.txt](LICENSES/Zlib.txt)) for the SDL2_mixer code it alters. Our additions are 0BSD. The altered files say so, as the zlib licence requires, and keep the original notice |

The SDK and example agents are 0BSD, so agents written against them can use any licence; the socket protocol carries no licence obligations.

The engine patch makes AutoDoom/Eternity publish live frames, accept controlled desktop input, suspend its simulation exactly, gate audio and checkpoint its native game state. The patched engine is not an unmodified upstream release. When you distribute a built engine, include the corresponding source: the pinned upstream revisions below, this patch, and [`src/bridge.h`](src/bridge.h) (0BSD), the shared header the patch includes.

Third-party source, protocol definitions and game data keep their own copyright notices and licences. Nothing here relicenses Doom or Freedoom data.

## Engine sources, built on the user's machine

The setup scripts fetch these exact revisions and apply the desktop patch to AutoDoom and the device patch to SDL2_mixer, so the SDL2_mixer that Live Doom builds is an altered version, used only by its own engine. Nothing from them is committed to this repository.

| Component | Upstream | Revision | Licence and notices |
| --- | --- | --- | --- |
| AutoDoom (Eternity) | [ioan-chera/AutoDoom](https://github.com/ioan-chera/AutoDoom) | `6a1d1839ee485b753c73e3405834472aca0b4c8f` | [GNU GPL v3](https://github.com/ioan-chera/AutoDoom/blob/6a1d1839ee485b753c73e3405834472aca0b4c8f/COPYING); [Eternity additional terms](docs/licenses/Eternity-additional-terms.txt); [Eternity authors](https://github.com/ioan-chera/AutoDoom/blob/6a1d1839ee485b753c73e3405834472aca0b4c8f/AUTHORS), [AutoDoom authors](https://github.com/ioan-chera/AutoDoom/blob/6a1d1839ee485b753c73e3405834472aca0b4c8f/AUTHORS-AutoDoom) |
| SDL2_mixer | [libsdl-org/SDL_mixer](https://github.com/libsdl-org/SDL_mixer) | `8f0c805d54dbd1ff6b4b784d351ca884b8fe5ee9` | [zlib-style licence; Copyright 1997–2025 Sam Lantinga](docs/licenses/SDL_mixer.txt) |
| SDL2_net | [libsdl-org/SDL_net](https://github.com/libsdl-org/SDL_net) | `bec0e57de5f188c97b4762a52b59994ceb931ce1` | [zlib-style licence; Copyright 1997–2026 Sam Lantinga](docs/licenses/SDL_net.txt) |
| libADLMIDI (AutoDoom submodule) | [Wohlstand/libADLMIDI](https://github.com/Wohlstand/libADLMIDI) | `e721728ef11dd97395853514c6ae44b05bd6e71c` | Mixed LGPL/GPL/MIT components: [README licence section](https://github.com/Wohlstand/libADLMIDI/blob/e721728ef11dd97395853514c6ae44b05bd6e71c/README.md), [LICENSE](https://github.com/Wohlstand/libADLMIDI/blob/e721728ef11dd97395853514c6ae44b05bd6e71c/LICENSE), [GPL v3](https://github.com/Wohlstand/libADLMIDI/blob/e721728ef11dd97395853514c6ae44b05bd6e71c/LICENSE.GPL-3.txt), [LGPL v2.1](https://github.com/Wohlstand/libADLMIDI/blob/e721728ef11dd97395853514c6ae44b05bd6e71c/LICENSE.LGPL-2.1.txt), and the individual source notices |

AutoDoom is Ioan Chera's modification of the Eternity Engine. Eternity keeps the work of its upstream contributors and of the original Doom engine authors. Its GLBSP-derived node builder credits Andrew Apted, and the earlier BSP work of Colin Reed, Lee Killough and others.

Further engine components keep their in-tree notices (paths are in the AutoDoom revision above):

| Component | Notice |
| --- | --- |
| ACSVM, David Hill | [LGPL v2.1](https://github.com/ioan-chera/AutoDoom/blob/6a1d1839ee485b753c73e3405834472aca0b4c8f/acsvm/COPYING) and source headers |
| snes_spc, Shay Green | [LGPL v2.1](https://github.com/ioan-chera/AutoDoom/blob/6a1d1839ee485b753c73e3405834472aca0b4c8f/snes_spc/license.txt), [readme](https://github.com/ioan-chera/AutoDoom/blob/6a1d1839ee485b753c73e3405834472aca0b4c8f/snes_spc/readme.txt) |
| libpng, Glenn Randers-Pehrson and contributing authors | [libpng licence](https://github.com/ioan-chera/AutoDoom/blob/6a1d1839ee485b753c73e3405834472aca0b4c8f/libpng/LICENSE); the notices in `png.h` govern where upstream says so |
| zlib, Jean-loup Gailly and Mark Adler | [notice in zlib.h](https://github.com/ioan-chera/AutoDoom/blob/6a1d1839ee485b753c73e3405834472aca0b4c8f/zlib/zlib.h) |
| GLBSP-derived bot node builder, Andrew Apted and contributors | GPL-3.0-or-later notices in [system.h](https://github.com/ioan-chera/AutoDoom/blob/6a1d1839ee485b753c73e3405834472aca0b4c8f/source/autodoom/glbsp/system.h) and the neighbouring files |

SDL2_mixer's optional codecs keep their own notices, including the [dr_libs licence](https://github.com/libsdl-org/SDL_mixer/blob/8f0c805d54dbd1ff6b4b784d351ca884b8fe5ee9/src/codecs/dr_libs/LICENSE), the [minimp3 licence](https://github.com/libsdl-org/SDL_mixer/blob/8f0c805d54dbd1ff6b4b784d351ca884b8fe5ee9/src/codecs/minimp3/LICENSE), and TiMidity's [COPYING](https://github.com/libsdl-org/SDL_mixer/blob/8f0c805d54dbd1ff6b4b784d351ca884b8fe5ee9/src/codecs/timidity/COPYING). The local build disables several optional codecs; their source licences still apply to their source files.

## Game data

Live Doom ships no game data. Nothing is downloaded without an explicit flag or click.

- **Your own games.** Official games found in your Steam libraries are read where Steam installed them, and never copied. DOOM, DOOM II, Final DOOM, Master Levels and No Rest for the Living are © id Software / ZeniMax Media; SIGIL and SIGIL II are © Romero Games. None are covered by this project's licences. Steam package artwork is shown from Steam's local cache at runtime, and also never copied.
- **DOOM shareware episode** (Knee-Deep in the Dead, v1.9) is proprietary game data © id Software, and is not covered by this project's licences.
  - It is downloaded only after you accept id's terms in the Welcome card (or `doomctl download-shareware --accept-terms`).
  - The source is Debian's unmodified `doom-wad-shareware_1.9.fixed.orig.tar.gz`. The archive is checked against a pinned SHA-256, only its single `doom1.wad` member is read, that WAD is checked against its own pinned SHA-256, and it is stored unmodified under your data directory.
  - id's terms are in [docs/licenses/Doom-shareware.txt](docs/licenses/Doom-shareware.txt) (v1.2 `LICENSE.DOC`, with John Carmack's note on electronic distribution) and [docs/licenses/Doom-shareware-1.8.txt](docs/licenses/Doom-shareware-1.8.txt) (the last `LICENSE.DOC` id shipped).
  - The shareware may be given away free of charge, unmodified; it may not be sold, modified, or extended with new levels.
- **Freedoom 0.13.0** is fetched only by the setup flag the README's install command passes, from the [Freedoom release](https://github.com/freedoom/freedoom/releases/tag/v0.13.0). It is © 2001–2024 contributors to the Freedoom project, under the BSD-3-Clause terms in [docs/licenses/Freedoom.txt](docs/licenses/Freedoom.txt); the release's `CREDITS.txt` and `CREDITS-MUSIC.txt` carry the attributions.

| Download | SHA-256 |
| --- | --- |
| `doom-wad-shareware_1.9.fixed.orig.tar.gz` (Debian) | `e02c8b5e01be7373d4c53f82556118e2aaaf8f83fa2af5eee1efadf9c55c4eb1` |
| `doom1.wad` (read from the Debian archive) | `1d7d43be501e67d927e415e0b8f3e29c3bf33075e859721816f652a526cac771` |
| `freedoom-0.13.0.zip` | `3f9b264f3e3ce503b4fb7f6bdcb1f419d93c7b546f4df3e874dd878db9688f59` |
| `freedoom1.wad` (extracted) | `7323bcc168c5a45ff10749b339960e98314740a734c30d4b9f3337001f9e703d` |
| `freedoom2.wad` (extracted) | `a8772e088847032510d97ba2312406a6998f21cbab44d4ff10696faa9c0ecd4b` |

"DOOM" is a trademark of id Software / ZeniMax Media. Live Doom is an independent fan project. It is not affiliated with or endorsed by them, and uses the name only to say which game it plays.

## Wayland definitions and system dependencies

The protocol XML files under [src/protocols](src/protocols) keep their full embedded copyright and permission notices, and the generated protocol code derives from them. `xdg-shell`, `xdg-output`, relative-pointer and pointer-constraints definitions carry MIT-style notices from their authors. The layer-shell definition keeps Drew DeVault's permission and disclaimer text.

Wayland, Cairo, libxkbcommon, SDL2/SDL2-compat, GTK, PyGObject, Python, systemd, Hyprland, Quickshell and Omarchy ([omacom/omarchy](https://github.com/omacom/omarchy), MIT) come from the installed system and keep their package licences. Live Doom uses Omarchy's documented commands and does not copy Omarchy code into this repository.

The upstream source trees, their source headers and their licence texts remain authoritative for their components. Keep those notices with source distributions, and with the corresponding source of any distributed engine binary.
