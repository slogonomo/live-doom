"""Cached discovery of Steam games; scans never change gameplay settings."""
# SPDX-License-Identifier: 0BSD
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from project_paths import read_bytes, atomic_json

STORE_URL = 'https://store.steampowered.com/app/2280/DOOM__DOOM_II/'
CACHE_NAME = 'steam-catalog.json'
MAX_CACHE_BYTES = 2 * 1024 * 1024


def valid_row(row):
    if not isinstance(row, dict):
        return False
    strings = ('id', 'title', 'start_map', 'reason', 'edition')
    if any(not isinstance(row.get(key), str) for key in strings):
        return False
    if not all(row[key] for key in ('id', 'title', 'start_map')):
        return False
    if type(row.get('compatible')) is not bool or row['edition'] not in ('original', 'rerelease'):
        return False
    if 'iwad' not in row or not (isinstance(row['iwad'], str) and row['iwad']
                                or row['iwad'] is None and not row['compatible']):
        return False
    if not isinstance(row.get('pwads'), list) or any(not isinstance(p, str) or not p for p in row['pwads']):
        return False
    source = row.get('source')
    if not isinstance(source, dict) or not isinstance(source.get('title'), str) or type(source.get('appid')) is not int:
        return False
    artwork = source.get('artwork', {})
    return isinstance(artwork, dict) and all(isinstance(value, str) for value in artwork.values())


def empty_catalog():
    return {'schema': 1, 'steam_found': False, 'summary': 'Steam not scanned yet',
            'packages': [], 'warnings': [], 'scanned_paths': [], 'scanned_at': None}


def read_catalog(state: Path):
    path = Path(state) / CACHE_NAME
    try:
        raw = read_bytes(path, MAX_CACHE_BYTES)
        if len(raw) > MAX_CACHE_BYTES:
            raise ValueError('Steam catalog is too large')
        data = json.loads(raw)
        if (not isinstance(data, dict) or data.get('schema') != 1
                or not isinstance(data.get('packages'), list) or len(data['packages']) > 128
                or any(not valid_row(row) for row in data['packages'])
                or type(data.get('steam_found')) is not bool or not isinstance(data.get('summary'), str)
                or not isinstance(data.get('warnings'), list)
                or any(not isinstance(value, str) for value in data['warnings'])):
            raise ValueError('Invalid Steam catalog')
        return data
    except FileNotFoundError:
        return empty_catalog()
    except (OSError, ValueError) as error:
        data = empty_catalog()
        data['summary'] = 'Steam scan needed'
        data['warnings'] = [str(error)]
        return data


def write_catalog(state: Path, catalog: dict):
    state = Path(state)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = state / CACHE_NAME
    raw = (json.dumps(catalog, indent=2) + '\n').encode()
    if len(raw) > MAX_CACHE_BYTES:
        raise ValueError('Steam catalog is too large')
    # Installer and menu scans can overlap; each writer owns its temporary file.
    atomic_json(destination, catalog)


def scan_steam(home: Path, state: Path):
    from steam_games import discover
    catalog = dict(discover(home=Path(home)))
    catalog.update(schema=1, scanned_at=datetime.now(timezone.utc).isoformat())
    write_catalog(state, catalog)
    return catalog


def selected_package(config, catalog):
    selected = config.get('game_package')
    if not selected:
        return None
    for row in catalog['packages']:
        if (row['id'] == selected and row.get('compatible') is True
                and row.get('iwad') == config['iwad'] and row.get('pwads') == config['pwads']
                and row.get('start_map') == config.get('start_map')):
            return selected
    return None


def public_catalog(catalog):
    # Full inspected paths are diagnostic cache data, not menu state. Keeping
    # them out also bounds the IPC response for multiple Steam libraries.
    return {key: catalog.get(key) for key in
            ('steam_found', 'summary', 'packages', 'scanned_at', 'warnings')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('scan',))
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--state', type=Path, required=True)
    options = parser.parse_args()
    try:
        catalog = scan_steam(options.home, options.state)
        print(json.dumps({'steam_found': catalog.get('steam_found', False),
                          'summary': catalog.get('summary', 'Steam scan complete'),
                          'count': len(catalog['packages'])}))
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print('Steam scan failed: ' + str(error))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
