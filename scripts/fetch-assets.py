#!/usr/bin/env python3
# SPDX-License-Identifier: 0BSD
"""Explicit opt-in download of pinned Freedoom; no shareware download policy."""
import argparse
from pathlib import Path
import sys
sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from asset_fetch import fetch_verified, install_verified_zip
from project_paths import ProjectPaths, read_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fetch-freedoom', action='store_true',
                        help='Explicitly download and install pinned Freedoom 0.13.0 data')
    arguments = parser.parse_args()
    if not arguments.fetch_freedoom:
        parser.error('No assets fetched: --fetch-freedoom is an explicit required opt-in')
    pins = read_json(ROOT / 'scripts/build-pins.json', 65536)['freedoom']
    paths = ProjectPaths.from_env()
    for path in (paths.data_root, paths.cache_root):
        if path == ROOT or ROOT in path.parents:
            parser.error('XDG data/cache locations must be outside the checkout')
    name = 'freedoom-' + pins['version']
    try:
        archive = fetch_verified(pins['url'], paths.cache_root / 'downloads' / (name + '.zip'),
                                 pins['sha256'], pins['max_bytes'])
        directory = install_verified_zip(archive, paths.asset_root / name, pins['sha256'],
                                         pins['max_bytes'], pins['max_unpacked_bytes'], pins['files'])
    except (OSError, ValueError, TimeoutError) as error:
        parser.exit(1, 'Freedoom download: ' + str(error) + '\n')
    print('Verified Freedoom installed at ' + str(directory))


if __name__ == '__main__':
    main()
