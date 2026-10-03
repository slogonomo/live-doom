#!/usr/bin/env bash
# SPDX-License-Identifier: 0BSD
set -euo pipefail
umask 077
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
export PYTHONDONTWRITEBYTECODE=1
build=$(python3 -B "$root/src/project_paths.py" --field build_root)
binary=$(python3 -B "$root/src/project_paths.py" --field viewer)
python3 -B "$root/scripts/build_support.py" layout
protocols="$build/viewer-protocols"
mkdir -p -- "$protocols"
for protocol in xdg-shell xdg-output-unstable-v1 wlr-layer-shell-unstable-v1 relative-pointer-unstable-v1 pointer-constraints-unstable-v1; do
  wayland-scanner client-header "$root/src/protocols/$protocol.xml" "$protocols/$protocol-client-protocol.h"
  wayland-scanner private-code "$root/src/protocols/$protocol.xml" "$protocols/$protocol-protocol.c"
done
temporary=$(mktemp "$build/.doom-viewer-XXXXXX")
trap 'rm -f -- "$temporary"' EXIT
cc -std=c11 -O2 -g -Wall -Wextra -Wpedantic \
  $(pkg-config --cflags wayland-client wayland-cursor xkbcommon cairo) \
  -I"$root/src" -I"$protocols" "$root/src/viewer.c" "$protocols"/*-protocol.c \
  $(pkg-config --libs wayland-client wayland-cursor xkbcommon cairo) -lm -o "$temporary"
python3 -B "$root/scripts/build_support.py" publish "$temporary" "$binary"
