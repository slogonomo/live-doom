#!/usr/bin/env bash
# SPDX-License-Identifier: 0BSD
set -euo pipefail
umask 077
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
export PYTHONDONTWRITEBYTECODE=1
cmake_bin=${CMAKE:-cmake}
if ! command -v "$cmake_bin" >/dev/null; then
  printf 'Install system CMake 3.28 or newer before building Live Doom. No tool is downloaded automatically.\n' >&2
  exit 1
fi
"$cmake_bin" --version | python3 -B -c 'import re,sys; m=re.search(r"cmake version (\d+)\.(\d+)",sys.stdin.read(4096)); sys.exit(0 if m and tuple(map(int,m.groups())) >= (3,28) else "CMake 3.28 or newer is required")'
jobs=${LIVE_DOOM_BUILD_JOBS:-2}
if ! python3 -B -c 'import re,sys; v=sys.argv[1]; sys.exit(0 if re.fullmatch(r"[0-9]{1,2}",v) and 1 <= int(v) <= 32 else 1)' "$jobs"; then
  printf 'LIVE_DOOM_BUILD_JOBS must be an integer from 1 to 32.\n' >&2
  exit 1
fi
vendor=$(python3 -B "$root/src/project_paths.py" --field vendor_root)
build=$(python3 -B "$root/src/project_paths.py" --field build_root)
prefix=$(python3 -B "$root/src/project_paths.py" --field deps_root)
binary=$(python3 -B "$root/src/project_paths.py" --field engine)
for source in autodoom SDL_mixer SDL_net; do
  if [[ ! -f $vendor/$source/CMakeLists.txt ]]; then
    printf 'Pinned sources missing: run scripts/bootstrap.sh --fetch-sources (optionally --fetch-freedoom).\n' >&2
    exit 1
  fi
done
python3 -B "$root/scripts/build_support.py" bootstrap
python3 -B "$root/scripts/build_support.py" header
"$cmake_bin" -S "$vendor/SDL_mixer" -B "$build/sdl-mixer" -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$prefix" -DSDL2MIXER_SAMPLES=OFF -DSDL2MIXER_VENDORED=OFF -DSDL2MIXER_VORBIS=STB -DSDL2MIXER_GME=OFF -DSDL2MIXER_MOD=OFF -DSDL2MIXER_MIDI=OFF -DSDL2MIXER_FLAC=OFF -DSDL2MIXER_WAVPACK=OFF -DSDL2MIXER_OPUS=OFF -DSDL2MIXER_MP3=OFF
"$cmake_bin" --build "$build/sdl-mixer" -j "$jobs"
"$cmake_bin" --install "$build/sdl-mixer"
"$cmake_bin" -S "$vendor/SDL_net" -B "$build/sdl-net" -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$prefix" -DSDL2NET_SAMPLES=OFF
"$cmake_bin" --build "$build/sdl-net" -j "$jobs"
"$cmake_bin" --install "$build/sdl-net"
"$cmake_bin" -S "$vendor/autodoom" -B "$build/engine" -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH="$prefix" -DCMAKE_BUILD_WITH_INSTALL_RPATH=ON -DCMAKE_INSTALL_RPATH='$ORIGIN/../deps/lib' -DCMAKE_INSTALL_RPATH_USE_LINK_PATH=OFF -DCMAKE_POLICY_VERSION_MINIMUM=3.5
"$cmake_bin" --build "$build/engine" -j "$jobs"
python3 -B "$root/scripts/build_support.py" publish "$build/engine/source/eternity" "$binary"
"$root/scripts/build-viewer.sh"
printf 'Live Doom built outside the checkout: %s\n' "$binary"
