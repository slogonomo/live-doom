# SPDX-License-Identifier: 0BSD
"""Explicit first-run choices; settings reads never download or select games."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
sys.dont_write_bytecode = True
import tempfile
import tarfile

from game_catalog import read_catalog, scan_steam
from project_paths import atomic_json, atomic_write, ensure_private_dir, open_regular, read_bytes, read_json

MARKER = 'onboarding.json'
STEPS = ('background', 'game', 'screensaver', 'summary')
BACKGROUND_CHOICES = ('current', 'phobos', 'skip')
SHAREWARE_SOURCE = {
    'url': 'https://deb.debian.org/debian/pool/non-free/d/doom-wad-shareware/doom-wad-shareware_1.9.fixed.orig.tar.gz',
    'sha256': 'e02c8b5e01be7373d4c53f82556118e2aaaf8f83fa2af5eee1efadf9c55c4eb1',
    'bytes': 1756095,
    'member': 'doom-wad-shareware-1.9.fixed/doom1.wad',
    'wad_sha256': '1d7d43be501e67d927e415e0b8f3e29c3bf33075e859721816f652a526cac771',
    'wad_bytes': 4196020,
}
# Separate theme repository approved for explicit onboarding installation.
PHOBOS_REPO = 'https://github.com/slogonomo/omarchy-phobos-theme'
LICENSE_ROOT = Path(__file__).resolve().parents[1] / 'docs/licenses'
LICENSE_FILES = ('Doom-shareware.txt', 'Doom-shareware-1.8.txt')
BUILD_PINS = Path(__file__).resolve().parents[1] / 'scripts/build-pins.json'


def bundled_game(data_root, game, *, verify=False):
    """Describe an installed game; explicit selections also verify its pinned bytes."""
    if game == 'freedoom':
        pins = read_json(BUILD_PINS, 65536)['freedoom']
        path = Path(data_root) / 'assets' / ('freedoom-' + pins['version']) / 'freedoom1.wad'
        title, digest, limit = 'Freedoom Phase 1', pins['files']['freedoom1.wad'], pins['max_unpacked_bytes']
        missing = 'Bundled Freedoom is missing; run scripts/bootstrap.sh --fetch-freedoom'
    elif game == 'shareware':
        path = Path(data_root) / 'assets/shareware/doom1.wad'
        title, digest, limit = 'DOOM Shareware', SHAREWARE_SOURCE['wad_sha256'], SHAREWARE_SOURCE['wad_bytes']
        missing = 'Verified shareware is not installed; download it after accepting its terms'
    else:
        raise ValueError('Unknown bundled game')
    if verify:
        try:
            with open_regular(path) as stream:
                size = os.fstat(stream.fileno()).st_size
                if not 0 < size <= limit:
                    raise ValueError(title + ' has an invalid size')
                actual = hashlib.sha256()
                total = 0
                while block := stream.read(min(1024 * 1024, limit + 1 - total)):
                    total += len(block)
                    if total > limit:
                        raise ValueError(title + ' exceeds its size limit')
                    actual.update(block)
                if actual.hexdigest() != digest:
                    raise ValueError(title + ' failed SHA256 verification')
        except FileNotFoundError:
            raise ValueError(missing) from None
    return {'iwad': str(path), 'title': title,
            'available': path.is_file() and not path.is_symlink()}


def selected_bundled_game(config, data_root):
    if config.get('game_package') is not None or config['pwads'] or config.get('start_map') is not None:
        return None
    for game in ('freedoom', 'shareware'):
        choice = bundled_game(data_root, game)
        if choice['available'] and config['iwad'] == choice['iwad']:
            return game
    return None


def shareware_terms():
    summary = ('DOOM Shareware Episode (Knee-Deep in the Dead), copyright id Software. '
               'Proprietary game data, outside Live Doom\'s licence. Free to use and '
               'give unmodified copies to others free of charge; no commercial use; '
               'do not modify. Downloaded from Debian\'s archive.\n\n')
    licenses = [read_bytes(LICENSE_ROOT / name, max_bytes=32768).decode('utf-8', errors='replace')
                for name in LICENSE_FILES]
    return {key: SHAREWARE_SOURCE[key] for key in ('url', 'sha256', 'bytes')} | {
        'text': summary + '\n\n'.join(licenses)}


def verified_shareware(archive):
    """Read only the pinned WAD, without extracting archive paths or programs."""
    source = SHAREWARE_SOURCE
    if len(archive) != source['bytes'] or hashlib.sha256(archive).hexdigest() != source['sha256']:
        raise ValueError('Shareware archive SHA256 or size verification failed')
    # Stream iteration keeps malformed headers from creating an unbounded list.
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r|gz') as package:
        members = iter(package)
        member = next(members, None)
        if (member is None or member.name != source['member'] or not member.isreg()
                or member.size != source['wad_bytes']):
            raise ValueError('Shareware archive does not contain the expected WAD')
        stream = package.extractfile(member)
        if stream is None:
            raise ValueError('Shareware WAD cannot be read')
        with stream:
            wad = stream.read(source['wad_bytes'] + 1)
        if next(members, None) is not None:
            raise ValueError('Shareware archive has unexpected extra members')
    if len(wad) != source['wad_bytes'] or hashlib.sha256(wad).hexdigest() != source['wad_sha256']:
        raise ValueError('Shareware WAD SHA256 or size verification failed')
    return wad


def download_shareware(data_root, *, accept_terms=False):
    if accept_terms is not True:
        raise ValueError('Read id Software\'s terms, then use --accept-terms to download')
    from asset_fetch import fetch_verified
    data_root = Path(data_root)
    with setup_lock(data_root):
        destination = data_root / 'assets/shareware/doom1.wad'
        source = SHAREWARE_SOURCE
        try:
            existing = read_bytes(destination, max_bytes=source['wad_bytes'])
        except FileNotFoundError:
            existing = None
        if existing is not None:
            if hashlib.sha256(existing).hexdigest() != source['wad_sha256']:
                raise ValueError('Existing shareware differs; preserve or remove it before retrying')
            for name in LICENSE_FILES:
                atomic_write(destination.parent / name, read_bytes(LICENSE_ROOT / name, max_bytes=32768))
            return destination
        archive = fetch_verified(source['url'], data_root / 'assets/downloads/doom-shareware-1.9.tar.gz',
                                 source['sha256'], source['bytes'], timeout=15, deadline=120,
                                 same_host=True)
        wad = verified_shareware(read_bytes(archive, max_bytes=source['bytes']))
        ensure_private_dir(destination.parent)
        # Publish complete bytes without replacing a concurrent file choice.
        fd, temporary = tempfile.mkstemp(prefix='.shareware-', dir=destination.parent)
        try:
            with os.fdopen(fd, 'wb') as output:
                output.write(wad)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary, destination, follow_symlinks=False)
            except FileExistsError:
                if hashlib.sha256(read_bytes(destination, max_bytes=source['wad_bytes'])).hexdigest() != source['wad_sha256']:
                    raise ValueError('Shareware destination changed during download')
        finally:
            Path(temporary).unlink(missing_ok=True)
        for name in LICENSE_FILES:
            atomic_write(destination.parent / name, read_bytes(LICENSE_ROOT / name, max_bytes=32768))
        return destination


@contextmanager
def setup_lock(data_root):
    ensure_private_dir(data_root)
    fd = os.open(Path(data_root) / '.onboarding.lock',
                 os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError('Unsafe onboarding lock')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def screensaver_choice(config):
    """Use the existing effective preference when an upgrade has no saved choice."""
    if isinstance(config, (str, Path)):
        path = Path(config)
        try:
            config = read_json(path, 65536)
            if not isinstance(config, dict):
                raise ValueError('Settings must be a JSON object')
        except (OSError, ValueError):
            # Match the controller's recovery path without touching either file.
            try:
                config = read_json(path.with_name('config.last-good.json'), 65536)
            except (OSError, ValueError):
                config = None
    return bool(isinstance(config, dict) and config.get('enabled', True)
                and config.get('screensaver_enabled', True))


def marker(data_root, *, config=None):
    try:
        value = read_json(Path(data_root) / MARKER, max_bytes=16384)
    except FileNotFoundError:
        return None
    if (not isinstance(value, dict) or type(value.get('schema')) is not int
            or value['schema'] not in (1, 2) or type(value.get('show')) is not bool):
        raise ValueError('Invalid onboarding state')
    if value['schema'] == 1:
        # Normalize in memory; a settings read never rewrites legacy state.
        return dict(value, schema=2, step='background',
                    choices={'background': 'current', 'screensaver': screensaver_choice(config)},
                    outcome=None if value['show'] else 'finished')
    choices = value.get('choices')
    if (value.get('step') not in STEPS or not isinstance(choices, dict)
            or choices.get('background') not in BACKGROUND_CHOICES
            or type(choices.get('screensaver')) is not bool
            or value.get('outcome') not in (None, 'finished', 'skipped')
            or (value['show'] and value.get('outcome') is not None)):
        raise ValueError('Invalid onboarding progress or choices')
    return value


def new_marker(*, fresh=False, selected=None, config=None):
    return {'schema': 2, 'show': True, 'fresh': fresh, 'selected_package': selected,
            'step': 'background', 'choices': {'background': 'current',
                'screensaver': True if fresh else screensaver_choice(config)},
            'outcome': None}


def create_config(path, value):
    """Publish a complete new config without replacing a concurrent choice."""
    path = Path(path)
    ensure_private_dir(path.parent)
    fd, temporary = tempfile.mkstemp(prefix='.first-config-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write((json.dumps(value, indent=2) + '\n').encode())
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
            return True
        except FileExistsError:
            return False
    finally:
        Path(temporary).unlink(missing_ok=True)


def initialize(config_path, data_root, *, home, scan=True, defaults=None):
    """Initialize once. Existing config and later user choices always win."""
    config_path, data_root, home = Path(config_path), Path(data_root), Path(home)
    with setup_lock(data_root):
        previous = marker(data_root, config=config_path)
        if previous is not None:
            return {'fresh': False, 'show': previous['show'],
                    'selected_package': previous.get('selected_package')}
        exists = config_path.exists() or config_path.is_symlink()
        catalog = scan_steam(home, data_root) if scan else read_catalog(data_root)
        selected = None
        fresh = False
        if not exists:
            if defaults is None:
                from controller import config_for
                defaults = config_for(config_path)
            config = dict(defaults)
            # A fresh installation opts in through the background picker.
            # Existing configs retain the observer's migration baseline.
            config['wallpaper_enabled'] = False
            config['screensaver_enabled'] = False
            config['iwad'] = bundled_game(data_root, 'freedoom')['iwad']
            playable = [row for row in catalog['packages'] if row.get('compatible') is True
                        and all(Path(path).is_file() for path in [row['iwad']] + row['pwads'])]
            chosen = next((row for row in playable if row['id'] == 'steam-doom'),
                          playable[0] if playable else None)
            if chosen is not None:
                selected = chosen['id']
                config.update(iwad=chosen['iwad'], pwads=chosen['pwads'],
                              start_map=chosen['start_map'], game_package=selected)
            fresh = create_config(config_path, config)
            if not fresh:
                selected = None
        state = new_marker(fresh=fresh, selected=selected, config=config_path)
        atomic_json(data_root / MARKER, state)
        return {'fresh': fresh, 'show': True, 'selected_package': selected}


def set_progress(data_root, step, *, background=None, screensaver=None, config=None):
    """Persist inert guide choices; only explicit game/integration verbs apply them."""
    if step not in STEPS:
        raise ValueError('Unknown onboarding step')
    if background is not None and background not in BACKGROUND_CHOICES:
        raise ValueError('Unknown onboarding background choice')
    if screensaver is not None and type(screensaver) is not bool:
        raise ValueError('Onboarding screensaver choice must be on or off')
    with setup_lock(data_root):
        value = marker(data_root, config=config) or new_marker(config=config)
        if not value['show']:
            raise ValueError('The setup guide is already closed')
        value['step'] = step
        choices = dict(value['choices'])
        if background is not None:
            choices['background'] = background
        if screensaver is not None:
            choices['screensaver'] = screensaver
        value['choices'] = choices
        atomic_json(Path(data_root) / MARKER, value)


def complete(data_root, outcome='finished', *, config=None):
    if outcome not in ('finished', 'skipped'):
        raise ValueError('Unknown onboarding outcome')
    with setup_lock(data_root):
        value = marker(data_root, config=config) or new_marker(config=config)
        if not value['show']:
            return  # Repeated/stale completion must not replace the first outcome.
        value['show'] = False
        value['outcome'] = outcome
        if outcome == 'finished':
            value['step'] = 'summary'
        atomic_json(Path(data_root) / MARKER, value)


def dismiss(data_root, *, config=None):
    complete(data_root, config=config)


def settings(data_root, *, home, downloading=False, config=None):
    value = marker(data_root, config=config)
    rows = read_catalog(data_root)['packages']
    official = any(row.get('compatible') is True and all(Path(path).is_file()
                   for path in [row['iwad']] + row['pwads']) for row in rows)
    steps = [step for step in STEPS if step != 'game' or not official]
    step = value['step'] if value else 'background'
    if step == 'game' and official:
        step = 'screensaver'
    terms = shareware_terms()
    present = Path(data_root) / 'assets/shareware/doom1.wad'
    return {'show': bool(value and value['show']),
            'step': step, 'steps': steps,
            'choices': dict(value['choices']) if value else dict(new_marker(config=config)['choices']),
            'outcome': value['outcome'] if value else None,
            'official_found': official,
            'freedoom': {key: item for key, item in bundled_game(data_root, 'freedoom').items()
                        if key in ('title', 'available')},
            'steam_doom': any(row['id'] == 'steam-doom' and row['compatible'] for row in rows),
            'shareware': 'downloading' if downloading else 'present' if present.is_file() and not present.is_symlink() else 'absent',
            'shareware_terms': terms,
            'theme': theme_settings(home)}


def theme_settings(home):
    home = Path(home)
    try:
        current = read_bytes(home / '.local/state/omarchy/current/theme.name', max_bytes=256).decode().strip()
    except FileNotFoundError:
        current = ''
    return {'name': 'phobos', 'url': PHOBOS_REPO,
            'installed': (home / '.config/omarchy/themes/phobos').is_dir(),
            'active': current == 'phobos'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('initialize',))
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--home', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(initialize(args.config, args.data, home=args.home)))


if __name__ == '__main__':
    main()
