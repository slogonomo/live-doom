#!/usr/bin/env bash
# SPDX-License-Identifier: 0BSD
set -euo pipefail
umask 077
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
export PYTHONDONTWRITEBYTECODE=1
exec python3 -B "$root/scripts/build_support.py" bootstrap "$@"
