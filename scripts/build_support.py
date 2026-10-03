#!/usr/bin/env python3
# SPDX-License-Identifier: 0BSD
"""Pinned source bootstrap and private XDG build publication helpers."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import selectors
import subprocess
import sys
sys.dont_write_bytecode = True
import tempfile
import time
import stat

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from asset_fetch import _parent, _regular
from project_paths import ProjectPaths, atomic_write, open_directory, open_regular, read_bytes, read_json

SOURCE_RECEIPT = '.live-doom-source-state.json'
OWNED_PATCH = '.live-doom-desktop.patch'
OWNED_MIXER_PATCH = '.live-doom-sdl-mixer-device.patch'
SOURCE_NAMES = ('autodoom', 'SDL_mixer', 'SDL_net')


def paths():
    value = ProjectPaths.from_env()
    for path in (value.data_root, value.cache_root):
        if ROOT == path or ROOT in path.parents:
            raise ValueError('XDG build/data paths must be outside the plugin checkout')
    return value


def prepare_layout(value):
    for path in (value.vendor_root, value.deps_root, value.build_root, value.bin_root,
                 value.asset_root, value.data_root / 'src'):
        _parent(path / '.build-owner-check')


def pins():
    value = read_json(ROOT / 'scripts/build-pins.json', 65536)
    for source in value['sources'].values():
        if (not re.fullmatch(r'[0-9a-f]{40}', source['revision'])
                or not source['url'].startswith('https://github.com/')):
            raise ValueError('Every source requires an immutable full Git commit and HTTPS URL')
    return value


def git(directory, *args, capture=False, allowed=(0,)):
    command = ['git', '-C', str(directory), *args]
    options = {'env': os.environ | {'GIT_TERMINAL_PROMPT': '0', 'GIT_OPTIONAL_LOCKS': '0'}}
    if not capture:
        result = subprocess.run(command, timeout=180, **options)
        returncode, output = result.returncode, b''
    else:
        # Captured local diffs/configuration must not become an unbounded pipe.
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options) as child:
            output, error = bytearray(), bytearray()
            stop = time.monotonic() + 180
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ, (output, 4 * 1024 * 1024))
                selector.register(child.stderr, selectors.EVENT_READ, (error, 1024 * 1024))
                try:
                    while selector.get_map():
                        remaining = stop - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError('Pinned source Git command deadline exceeded')
                        for key, _ in selector.select(min(remaining, 1)):
                            data = os.read(key.fileobj.fileno(), 65536)
                            if not data:
                                selector.unregister(key.fileobj)
                                continue
                            buffer, cap = key.data
                            if len(buffer) + len(data) > cap:
                                raise ValueError('Pinned source Git output exceeds its byte limit')
                            buffer.extend(data)
                    returncode = child.wait(timeout=max(0.01, stop - time.monotonic()))
                except BaseException:
                    child.kill()
                    child.wait()
                    raise
            output = bytes(output)
    if returncode not in allowed:
        raise RuntimeError('Pinned source Git command failed: ' + ' '.join(args[:3]))
    return output


def source(directory, pin, fetch=False):
    directory = _parent(directory)
    if fetch and directory.is_dir() and not directory.is_symlink() and not any(directory.iterdir()):
        directory.rmdir()  # checkout creates an empty directory for an uninitialized gitlink
    if not directory.exists():
        if not fetch:
            raise ValueError('Pinned sources missing: run scripts/bootstrap.sh --fetch-sources')
        temporary = Path(tempfile.mkdtemp(prefix='.' + directory.name + '-', dir=directory.parent))
        try:
            print(f"Fetching {directory.name} at {pin['revision']}", flush=True)
            git(temporary, 'init', '--quiet')
            git(temporary, 'remote', 'add', 'origin', pin['url'])
            git(temporary, 'fetch', '--quiet', '--depth', '1', 'origin', pin['revision'])
            git(temporary, 'checkout', '--detach', '--quiet', pin['revision'])
            os.replace(temporary, directory)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('Pinned source is not a regular directory')
    actual = git(directory, 'rev-parse', 'HEAD', capture=True).decode().strip()
    if actual != pin['revision']:
        raise ValueError(f'Wrong revision in {directory}; preserve local edits before replacing it')


def working_patch(directory):
    args = ('--binary', '--no-ext-diff', '--no-textconv', '--src-prefix=a/', '--dst-prefix=b/')
    patch = git(directory, 'diff', *args, '--', 'source', capture=True)
    for name in ('desktop_bridge.cpp', 'desktop_bridge.h'):
        patch += git(directory, 'diff', '--no-index', *args, '--', '/dev/null',
                     'source/' + name, capture=True, allowed=(1,))
    return patch


def working_mixer_patch(directory):
    """The mixer adapter only modifies tracked upstream files."""
    return git(directory, 'diff', '--binary', '--no-ext-diff', '--no-textconv',
               '--src-prefix=a/', '--dst-prefix=b/', 'HEAD', '--', '.', capture=True)


def stage_header(value):
    target = value.data_root / 'src/bridge.h'
    data = read_bytes(ROOT / 'src/bridge.h', 65536)
    try:
        if read_bytes(target, 65536) == data:
            return
    except FileNotFoundError:
        pass
    atomic_write(target, data, max_bytes=65536)


def prior_source_state(value):
    receipt = value.vendor_root / SOURCE_RECEIPT
    previous_patch = value.vendor_root / OWNED_PATCH
    mixer_patch = value.vendor_root / OWNED_MIXER_PATCH
    if all(not path.exists() and not path.is_symlink()
           for path in (receipt, previous_patch, mixer_patch)):
        return None, None
    state = read_json(receipt, 65536)
    data = read_bytes(previous_patch, 4 * 1024 * 1024)
    if (not isinstance(state, dict) or state.get('schema') not in (1, 2)
            or not isinstance(state.get('sources'), dict)
            or set(state['sources']) != set(SOURCE_NAMES) | {'adlmidi'}):
        raise ValueError('Source ownership receipt is invalid; existing sources were preserved')
    if state['schema'] == 1:
        valid = (state.get('patch_sha256') == hashlib.sha256(data).hexdigest()
                 and not mixer_patch.exists() and not mixer_patch.is_symlink())
    else:
        mixer_data = read_bytes(mixer_patch, 4 * 1024 * 1024)
        valid = state.get('patches') == {
            'autodoom': hashlib.sha256(data).hexdigest(),
            'SDL_mixer': hashlib.sha256(mixer_data).hexdigest()}
    if not valid:
        raise ValueError('Source ownership receipt is invalid; existing sources were preserved')
    for item in state['sources'].values():
        if (not isinstance(item, dict) or not isinstance(item.get('url'), str)
                or not isinstance(item.get('revision'), str)
                or not re.fullmatch('[0-9a-f]{40}', item['revision'])):
            raise ValueError('Source ownership receipt is invalid; existing sources were preserved')
    return state, data


def previous_mixer_patch(value, previous):
    """Schema 1 proved a pristine mixer; schema 2 retains its exact adapter."""
    if previous is not None and previous['schema'] == 2:
        return read_bytes(value.vendor_root / OWNED_MIXER_PATCH, 4 * 1024 * 1024)
    return b''


def inspect_sources(value, metadata, desktop_patch, previous=None, previous_patch=None,
                    mixer_patch=b'', previous_mixer=b''):
    """Prove existing worktrees clean or exactly ours, without changing them."""
    snapshots, ready = {}, True
    for name in SOURCE_NAMES + ('adlmidi',):
        directory = (value.vendor_root / 'autodoom/adlmidi' if name == 'adlmidi'
                     else value.vendor_root / name)
        if not directory.exists() and not directory.is_symlink():
            snapshots[name] = None
            ready = False
            continue
        # An uninitialized gitlink is an empty directory, not an existing repo.
        if name == 'adlmidi' and directory.is_dir() and not list(directory.iterdir()):
            snapshots[name] = None
            ready = False
            continue
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError(f'Unsafe source directory: {directory}; existing sources were preserved')
        with open_directory(directory):
            pass
        top = Path(git(directory, 'rev-parse', '--show-toplevel', capture=True).decode().strip())
        gitdir = Path(git(directory, 'rev-parse', '--absolute-git-dir', capture=True).decode().strip())
        if top != directory or value.vendor_root not in gitdir.resolve().parents:
            raise ValueError(f'Source repository points outside its owned tree: {directory}')
        revision = git(directory, 'rev-parse', 'HEAD', capture=True).decode().strip()
        origin = git(directory, 'remote', 'get-url', 'origin', capture=True).decode().strip()
        allowed_urls = {metadata['sources'][name]['url']}
        if previous is not None:
            allowed_urls.add(previous['sources'][name]['url'])
        # GitHub's submodule URL commonly omits .git and ends in a slash.
        canonical = lambda url: url.rstrip('/').removesuffix('.git')
        if canonical(origin) not in {canonical(url) for url in allowed_urls}:
            raise ValueError(f'Unknown source origin: {directory}; existing sources were preserved')
        status = git(directory, 'status', '--porcelain', '-z', '--untracked-files=all', '--ignored=matching', capture=True)
        dirty_message = f'Unknown source edits in {directory}; move the entire vendor directory to a safe backup before retrying --fetch-sources'
        patch_bytes = b''
        if name == 'autodoom':
            if git(directory, 'diff', '--cached', 'HEAD', capture=True).strip():
                raise ValueError(dirty_message)
            if git(directory, 'diff', 'HEAD', '--', '.', ':!source', ':!adlmidi', capture=True).strip():
                raise ValueError(dirty_message)
            for entry in status.split(b'\0'):
                if not entry:
                    continue
                if entry[:2] in (b'??', b'!!') and entry not in (
                        b'?? source/desktop_bridge.cpp', b'?? source/desktop_bridge.h'):
                    raise ValueError(dirty_message)
            bridges = [directory / 'source' / ('desktop_bridge.' + suffix) for suffix in ('cpp', 'h')]
            if all(path.is_file() and not path.is_symlink() for path in bridges):
                patch_bytes = working_patch(directory)
                owned_previous = (previous is not None and revision == previous['sources'][name]['revision']
                                  and patch_bytes == previous_patch)
                if patch_bytes != desktop_patch and not owned_previous:
                    raise ValueError(dirty_message)
            elif any(path.exists() or path.is_symlink() for path in bridges) or git(directory, 'diff', 'HEAD', '--', 'source', capture=True).strip():
                raise ValueError(dirty_message)
        elif name == 'SDL_mixer':
            if (git(directory, 'diff', '--cached', 'HEAD', capture=True).strip()
                    or any(entry[:2] in (b'??', b'!!') for entry in status.split(b'\0') if entry)):
                raise ValueError(dirty_message)
            patch_bytes = working_mixer_patch(directory)
            owned_previous = (previous is not None and revision == previous['sources'][name]['revision']
                              and patch_bytes == previous_mixer)
            if patch_bytes not in (b'', mixer_patch) and not owned_previous:
                raise ValueError(dirty_message)
        elif status.strip():
            raise ValueError(dirty_message)
        info = directory.stat()
        snapshots[name] = (info.st_dev, info.st_ino, revision, origin, status, hashlib.sha256(patch_bytes).hexdigest())
        ready = ready and revision == metadata['sources'][name]['revision']
        if name == 'autodoom':
            ready = ready and patch_bytes == desktop_patch
        elif name == 'SDL_mixer':
            ready = ready and patch_bytes == mixer_patch
    if snapshots.get('autodoom') and snapshots.get('adlmidi'):
        link = git(value.vendor_root / 'autodoom', 'ls-tree', 'HEAD', 'adlmidi', capture=True).decode().split()
        if len(link) < 3 or link[2] != snapshots['adlmidi'][2]:
            raise ValueError('Existing ADLMIDI has an unknown revision; existing sources were preserved')
    return snapshots, ready


@contextmanager
def source_lock(value):
    with open_directory(value.data_root):
        pass
    fd = os.open(value.data_root / '.source-bootstrap.lock',
                 os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError('Unsafe source bootstrap lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another source preparation is running; try again after it finishes') from None
        yield
    finally:
        os.close(fd)


def publish_sources(value, staging, names):
    """Retain old trees and roll back the active paths if a rename fails."""
    backup_parent = _parent(value.data_root / 'source-backups' / '.owner-check').parent
    backup = Path(tempfile.mkdtemp(prefix='sources-', dir=backup_parent))
    moved, published = [], []
    try:
        for name in names:
            target = value.vendor_root / name
            if target.exists() or target.is_symlink():
                os.replace(target, backup / name)
                moved.append(name)
            os.replace(staging / name, target)
            published.append(name)
    except BaseException as failure:
        try:
            for name in reversed(published):
                os.replace(value.vendor_root / name, staging / name)
            for name in reversed(moved):
                os.replace(backup / name, value.vendor_root / name)
        except OSError as rollback:
            raise RuntimeError('Source publication could not roll back; previous source files remain at '
                               + str(backup)) from rollback
        raise failure
    finally:
        if not list(backup.iterdir()):
            backup.rmdir()
    if moved:
        print('Previous sources retained at ' + str(backup), flush=True)


def prepare_sources(value, metadata, desktop_patch, mixer_patch=b''):
    prepare_layout(value)
    with source_lock(value):
        previous, previous_patch = prior_source_state(value)
        previous_mixer = previous_mixer_patch(value, previous)
        before, ready = inspect_sources(value, metadata, desktop_patch, previous, previous_patch,
                                        mixer_patch, previous_mixer)
        desired = {'schema': 2, 'sources': metadata['sources'], 'patches': {
            'autodoom': hashlib.sha256(desktop_patch).hexdigest(),
            'SDL_mixer': hashlib.sha256(mixer_patch).hexdigest()}}
        if ready and previous == desired:
            stage_header(value)
            return
        with tempfile.TemporaryDirectory(prefix='.source-update-', dir=value.data_root) as temporary:
            staging = Path(temporary)
            names = []
            if not ready:
                for name in SOURCE_NAMES:
                    source(staging / name, metadata['sources'][name], True)
                engine = staging / 'autodoom'
                link = git(engine, 'ls-tree', 'HEAD', 'adlmidi', capture=True).decode().split()
                if len(link) < 3 or link[2] != metadata['sources']['adlmidi']['revision']:
                    raise ValueError('New AutoDoom ADLMIDI gitlink differs from the immutable pin')
                source(engine / 'adlmidi', metadata['sources']['adlmidi'], True)
                patch = staging / 'desktop.patch'
                atomic_write(patch, desktop_patch, max_bytes=4 * 1024 * 1024)
                git(engine, 'apply', '--check', str(patch))
                git(engine, 'apply', str(patch))
                if mixer_patch:
                    mixer_path = staging / 'mixer.patch'
                    atomic_write(mixer_path, mixer_patch, max_bytes=4 * 1024 * 1024)
                    git(staging / 'SDL_mixer', 'apply', '--check', str(mixer_path))
                    git(staging / 'SDL_mixer', 'apply', str(mixer_path))
                staged_paths = type('StagedPaths', (), {'vendor_root': staging})()
                _, staged_ready = inspect_sources(staged_paths, metadata, desktop_patch,
                                                  mixer_patch=mixer_patch)
                if not staged_ready:
                    raise ValueError('Staged sources failed exact revision/patch verification')
                names.extend(SOURCE_NAMES)
            atomic_write(staging / OWNED_PATCH, desktop_patch, max_bytes=4 * 1024 * 1024)
            atomic_write(staging / OWNED_MIXER_PATCH, mixer_patch, max_bytes=4 * 1024 * 1024)
            atomic_write(staging / SOURCE_RECEIPT, (json.dumps(desired, sort_keys=True, indent=2) + '\n').encode(), max_bytes=65536)
            names.extend((OWNED_PATCH, OWNED_MIXER_PATCH, SOURCE_RECEIPT))
            after, _ = inspect_sources(value, metadata, desktop_patch, previous, previous_patch,
                                       mixer_patch, previous_mixer)
            current_state = prior_source_state(value)
            if (after != before or current_state != (previous, previous_patch)
                    or previous_mixer_patch(value, current_state[0]) != previous_mixer):
                raise ValueError('Sources changed during preparation; all existing sources were preserved')
            publish_sources(value, staging, names)
        stage_header(value)


def bootstrap(fetch_sources=False, fetch_freedoom=False):
    value, metadata = paths(), pins()
    if not fetch_sources:
        missing = [name for name in ('autodoom', 'SDL_mixer', 'SDL_net')
                   if not (value.vendor_root / name).is_dir()]
        if missing:
            raise ValueError('No sources fetched. Opt in with scripts/bootstrap.sh --fetch-sources '
                             '(add --fetch-freedoom for game data); missing: ' + ', '.join(missing))
    desktop_patch = read_bytes(ROOT / 'patches/autodoom-desktop.patch', 4 * 1024 * 1024)
    mixer_patch = read_bytes(ROOT / 'patches/sdl-mixer-device.patch', 4 * 1024 * 1024)
    if fetch_sources:
        prepare_sources(value, metadata, desktop_patch, mixer_patch)
    else:
        previous, previous_patch = prior_source_state(value)
        _, ready = inspect_sources(value, metadata, desktop_patch, previous, previous_patch,
                                   mixer_patch, previous_mixer_patch(value, previous))
        if not ready:
            raise ValueError('Pinned revision or owned source patch changed: opt in with scripts/bootstrap.sh --fetch-sources')
    if fetch_freedoom:
        subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/fetch-assets.py'),
                        '--fetch-freedoom'], check=True, timeout=180)
    elif not value.default_iwad.is_file():
        print('No game data fetched. Opt in with scripts/bootstrap.sh --fetch-freedoom, '
              'or select an installed compatible Steam IWAD after setup.', flush=True)


def publish(source_path, destination):
    source_path = Path(source_path)
    destination = _parent(Path(destination))
    with open_regular(source_path) as data, open_directory(destination.parent) as parent:
        _regular(destination)
        cap = 128 * 1024 * 1024
        if os.fstat(data.fileno()).st_size > cap:
            raise ValueError('Build executable exceeds its byte limit')
        fd, temporary = tempfile.mkstemp(prefix='.' + destination.name + '-', dir=f'/proc/self/fd/{parent}')
        name = Path(temporary).name
        try:
            with os.fdopen(fd, 'wb') as output:
                count = 0
                while block := data.read(65536):
                    count += len(block)
                    if count > cap:
                        raise ValueError('Build executable exceeds its byte limit')
                    output.write(block)
                output.flush()
                os.fchmod(output.fileno(), 0o700)
                os.fsync(output.fileno())
            _regular(destination)
            os.replace(name, destination.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(name, dir_fd=parent)
            except FileNotFoundError:
                pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    boot = sub.add_parser('bootstrap', help='Explicitly fetch pinned build sources outside the checkout')
    boot.add_argument('--fetch-sources', action='store_true', help='Explicitly fetch/prepare immutable pinned build sources')
    boot.add_argument('--fetch-freedoom', action='store_true', help='Explicitly opt in to verified Freedoom data')
    sub.add_parser('layout')
    sub.add_parser('header')
    publication = sub.add_parser('publish')
    publication.add_argument('source', type=Path)
    publication.add_argument('destination', type=Path)
    arguments = parser.parse_args()
    try:
        if arguments.command == 'bootstrap':
            bootstrap(arguments.fetch_sources, arguments.fetch_freedoom)
        elif arguments.command == 'publish':
            publish(arguments.source, arguments.destination)
        else:
            value = paths()
            prepare_layout(value)
            if arguments.command == 'header':
                stage_header(value)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, 'Live Doom build: ' + str(error) + '\n')


if __name__ == '__main__':
    main()
