# SPDX-License-Identifier: 0BSD
"""Offer a live Doom choice without applying or changing the current theme."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time
from contextlib import contextmanager

from project_paths import atomic_json, ensure_private_dir, read_bytes, read_json, read_text

IMAGE_SUFFIXES = {'.webp', '.png', '.jpg', '.jpeg', '.bmp', '.gif'}


def _safe_folder(path: Path):
    for parent in reversed((path,) + tuple(path.parents)):
        if parent.is_symlink():
            raise RuntimeError('The background folder is a symlink')
        if parent.exists() and not parent.is_dir():
            raise RuntimeError('The background folder is not a directory')


def _directory(path: Path):
    _safe_folder(path)
    path.mkdir(parents=True, exist_ok=True)


def _live_image(folder: Path):
    _safe_folder(folder)
    if not folder.is_dir():
        return None
    for path in sorted(folder.iterdir()):
        try:
            resolved = path.resolve(strict=True)
            if (resolved.is_file() and resolved.stat().st_size > 0
                    and 'live-doom' in resolved.name.lower()
                    and resolved.suffix.lower() in IMAGE_SUFFIXES):
                return resolved
        except (OSError, RuntimeError):
            continue
    return None


@contextmanager
def _theme_lock(runtime):
    _safe_folder(runtime)
    lock_path = runtime / 'omarchy-theme-set.lock'
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + 2
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('A theme change is still running; try again when it finishes')
                time.sleep(0.025)
        yield
    finally:
        os.close(fd)


def _theme(home):
    current = home / '.local/state/omarchy/current'
    _safe_folder(current / 'theme')
    name_file = current / 'theme.name'
    if name_file.is_symlink():
        raise RuntimeError('The current theme name is a symlink')
    name = read_text(name_file, max_bytes=256).strip()
    if not name or name.startswith('.') or Path(name).name != name or '\x00' in name or '\\' in name:
        raise RuntimeError('The current theme name is invalid')
    info = name_file.stat()
    directory = (current / 'theme').stat()
    return {'name': name, 'name_identity': [info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns],
            'directory_identity': [directory.st_dev, directory.st_ino]}


def _assert_theme(home, expected):
    if _theme(home) != expected:
        raise RuntimeError('The current theme changed; no image was removed')


def _personal(home, name):
    return home / '.config/omarchy/backgrounds' / name


def _state(home):
    # Explicit fixture homes must not inherit the desktop's XDG overrides.
    root = (Path(os.environ['XDG_STATE_HOME']) if os.environ.get('XDG_STATE_HOME')
            and os.environ.get('HOME') == str(home) else home / '.local/state')
    return root / 'live-doom/live-backgrounds'


def _ledger(home):
    path = _state(home) / 'ownership.json'
    _safe_folder(path.parent)
    if path.is_symlink():
        raise RuntimeError('Live background ownership data is a symlink')
    if not path.exists():
        # Read legacy ownership in place once; the next explicit edit records
        # it under the new app name without deleting the recovery archive.
        legacy = home / '.local/state/doom-desktop/live-backgrounds/ownership.json'
        if not legacy.exists() and not legacy.is_symlink():
            return {'schema': 2, 'images': {}, 'recovered': []}
        path = legacy
    try:
        value = read_json(path, max_bytes=1024 * 1024)
    except (OSError, ValueError) as exc:
        raise RuntimeError('Live background ownership data is unavailable') from exc
    if (not isinstance(value, dict) or value.get('schema') not in (1, 2) or
            not isinstance(value.get('images'), dict) or not isinstance(value.get('recovered'), list)):
        raise RuntimeError('Live background ownership data is invalid')
    return value


def _save_ledger(home, ledger):
    folder = _state(home)
    ensure_private_dir(folder)
    target = folder / 'ownership.json'
    # Convert old fingerprints only after checking their unchanged content.
    # Invalid/edited historical entries remain recorded but cannot authorize
    # removal. Device numbers and inode identities are never written again.
    value = dict(ledger, schema=2,
                 images={name: _persistent_record(record) for name, record in ledger['images'].items()},
                 recovered=[_persistent_record(record) for record in ledger['recovered']])
    atomic_json(target, value)


def _file_record(path, *, expected_inode=None):
    _safe_folder(path.parent)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size == 0:
        raise RuntimeError('The image is not an independent regular file')
    data = read_bytes(path, max_bytes=32 * 1024 * 1024)
    after = path.lstat()
    identity = lambda info: (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns)
    if identity(before) != identity(after) or before.st_mode != after.st_mode or before.st_size != after.st_size:
        raise RuntimeError('The image changed while being read')
    if expected_inode is not None and after.st_ino != expected_inode:
        raise RuntimeError('The recorded image was replaced')
    return {'path': str(path), 'sha256': hashlib.sha256(data).hexdigest(),
            'mode': stat.S_IMODE(after.st_mode), 'size': after.st_size,
            'mtime_ns': after.st_mtime_ns}


def _matching_record(path, record):
    try:
        if not isinstance(record, dict):
            return None
        old = record.get('identity')
        if 'identity' in record and not (isinstance(old, list) and len(old) == 3
                                        and all(type(value) is int for value in old)):
            return None
        actual = _file_record(path, expected_inode=old[1] if old is not None else None)
        if not all(actual[key] == record.get(key) for key in ('path', 'sha256', 'mode')):
            return None
        if old is not None:
            # Schema 1 included st_dev, which can change across a btrfs mount.
            # Accept only that mismatch: its inode, timestamp, bytes and mode
            # must still match before adopting the boot-stable fingerprint.
            return actual if old[2] == actual['mtime_ns'] else None
        return actual if all(actual[key] == record.get(key) for key in ('size', 'mtime_ns')) else None
    except (OSError, RuntimeError, ValueError, TypeError):
        return None


def _matches(path, record):
    return _matching_record(path, record) is not None


def _persistent_record(record):
    """Retain provenance without publishing boot-local stat identities."""
    if not isinstance(record, dict):
        return record
    value = dict(record)
    old = value.pop('identity', None)
    if old is not None:
        value['size'] = None  # An unverified old entry must never become owned.
        value['mtime_ns'] = old[2] if isinstance(old, list) and len(old) == 3 else None
        if isinstance(record.get('path'), str):
            actual = _matching_record(Path(record['path']), record)
            if actual is not None:
                value.update(actual)
    if isinstance(value.get('prior_static'), dict):
        value['prior_static'] = _persistent_record(value['prior_static'])
    return value


def _selection(home):
    link = home / '.local/state/omarchy/current/background'
    if not link.is_symlink():
        raise RuntimeError('The selected background is unavailable')
    before = link.lstat()
    chosen = link.resolve(strict=True)
    after = link.lstat()
    identity = lambda info: [info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns]
    if identity(before) != identity(after) or not chosen.is_file():
        raise RuntimeError('The selected background changed while being read')
    return {'path': str(chosen), 'link_identity': identity(after)}


def _is_static(path):
    return 'live-doom' not in path.name.lower() and path.suffix.lower() in IMAGE_SUFFIXES


def _prior_static(home, name):
    try:
        path = Path(_selection(home)['path'])
        if _is_static(path):
            return dict(_file_record(path), theme=name)
    except (OSError, RuntimeError):
        pass
    return None


def _static_choice(home, theme, record):
    prior = record.get('prior_static')
    if isinstance(prior, dict) and prior.get('theme') == theme['name'] and isinstance(prior.get('path'), str):
        path = Path(prior['path'])
        if _is_static(path) and _matches(path, prior):
            return path
    for folder in (home / '.local/state/omarchy/current/theme/backgrounds', _personal(home, theme['name'])):
        _safe_folder(folder)
        if not folder.is_dir():
            continue
        for path in sorted(folder.iterdir()):
            if _is_static(path):
                try:
                    _file_record(path)
                    return path
                except (OSError, RuntimeError):
                    pass
    return None


def _legacy_record(home, fallback, theme, path, ledger):
    """Infer only the exact former helper filename/bytes/mode, never an edit."""
    if str(path) in ledger['images'] or any(isinstance(row, dict) and row.get('path') == str(path)
                                          for row in ledger['recovered']):
        return None
    if path.parent != _personal(home, theme['name']) or fallback.is_symlink() or not fallback.is_file():
        return None
    try:
        data = fallback.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if not data or path.name not in ('1-live-doom.webp', f'1-live-doom-{digest}.webp'):
            return None
        record = _file_record(path)
        if record['sha256'] != digest or record['mode'] != 0o644:
            return None
        return dict(record, theme=theme['name'], origin='legacy-exact-bundle', prior_static=None)
    except (OSError, RuntimeError):
        return None


def _owned_choice(home, fallback, theme, ledger, selected):
    personal = _personal(home, theme['name'])
    _safe_folder(personal)
    paths = []
    for text, record in ledger['images'].items():
        if not isinstance(text, str) or not isinstance(record, dict) or record.get('theme') != theme['name']:
            continue
        path = Path(text)
        if (path.parent == personal and 'live-doom' in path.name.lower() and
                path.suffix.lower() in IMAGE_SUFFIXES and _matches(path, record)):
            paths.append((path, record, False))
    if personal.is_dir():
        for path in sorted(personal.iterdir()):
            record = _legacy_record(home, fallback, theme, path, ledger)
            if record is not None:
                paths.append((path, record, True))
    paths.sort(key=lambda row: (str(row[0]) != selected['path'], str(row[0])))
    return paths[0] if paths else None


def live_wallpaper_metadata(home: Path, fallback: Path) -> dict:
    """Read-only removability; legacy adoption is only a narrow content match.

    No ownership directory, record, lock or archive is created by this call.
    A matching legacy personal copy is recoverably adopted on removal. Copies
    in a theme directory, arbitrary live markers, aliases and edits are never
    inferred to be ours; an unavailable original bundle cannot prove a legacy
    copy. Recorded future additions remain removable without that bundle.
    """
    home = Path(home).absolute()  # keep directory aliases visible to safety checks
    result = {'name': '', 'live_choice_present': False, 'can_remove_live': False,
              'remove_reason': '', 'selected_path': None, 'owned_path': None,
              'legacy_adoptable': False}
    try:
        theme = _theme(home)
        result['name'] = theme['name']
        selected = _selection(home)
        result['selected_path'] = selected['path']
        result['live_choice_present'] = bool(_live_image(home / '.local/state/omarchy/current/theme/backgrounds') or
                                             _live_image(_personal(home, theme['name'])))
        if theme['name'].lower() == 'phobos':
            result['remove_reason'] = 'Phobos live backgrounds are bundled and protected'
            return result
        ledger = _ledger(home)
        owned = _owned_choice(home, fallback, theme, ledger, selected)
        if owned is None:
            result['remove_reason'] = 'No unchanged Doom-owned personal live choice; user images and edits are preserved'
            return result
        path, record, legacy = owned
        _safe_folder(_state(home) / 'recovered' / theme['name'])
        result['owned_path'] = str(path)
        result['legacy_adoptable'] = legacy
        if selected['path'] == str(path) and _static_choice(home, theme, record) is None:
            result['remove_reason'] = 'Select or add a static background before removing the selected live choice'
            return result
        result['can_remove_live'] = True
        result['remove_reason'] = 'Exact legacy bundle copy can be archived recoverably' if legacy else 'Doom-owned choice can be archived recoverably'
        _assert_theme(home, theme)
        return result
    except (OSError, ValueError, RuntimeError) as exc:
        result['can_remove_live'] = False
        result['remove_reason'] = str(exc)
        return result


def _pick(home, theme, select, target):
    _assert_theme(home, theme)
    if select(target) is False:
        raise RuntimeError('Background selection was cancelled')
    _assert_theme(home, theme)
    if _selection(home)['path'] != str(target.resolve(strict=True)):
        raise RuntimeError('The background was not selected')


def use_live_wallpaper(home: Path, fallback: Path, select, runtime: Path) -> Path:
    """Reuse/add a live choice, recording only images this helper publishes."""
    home = Path(home).absolute()
    with _theme_lock(runtime):
        theme = _theme(home)
        personal = _personal(home, theme['name'])
        target = _live_image(home / '.local/state/omarchy/current/theme/backgrounds') or _live_image(personal)
        prior = _prior_static(home, theme['name'])
        ledger = _ledger(home)
        created = False
        if target is None:
            if fallback.is_symlink() or not fallback.is_file() or fallback.stat().st_size == 0:
                raise RuntimeError('No live background is bundled')
            data = read_bytes(fallback, max_bytes=32 * 1024 * 1024)
            _directory(personal)
            target = personal / '1-live-doom.webp'
            if target.exists() or target.is_symlink():
                target = personal / ('1-live-doom-' + hashlib.sha256(data).hexdigest() + '.webp')
            if target.exists() or target.is_symlink():
                if target.is_symlink() or not target.is_file() or read_bytes(target, max_bytes=32 * 1024 * 1024) != data:
                    raise RuntimeError('The live background filename is already in use')
            else:
                temp_fd, temporary = tempfile.mkstemp(prefix='.doom-live-', suffix='.tmp', dir=personal)
                try:
                    with os.fdopen(temp_fd, 'wb') as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.chmod(temporary, 0o644)
                    _assert_theme(home, theme)
                    os.link(temporary, target)  # publication never overwrites a user file
                    created = True
                finally:
                    os.unlink(temporary)
        record = ledger['images'].get(str(target))
        if created:
            record = dict(_file_record(target), theme=theme['name'], origin='created', prior_static=prior)
            ledger['images'][str(target)] = record
            _save_ledger(home, ledger)
        elif isinstance(record, dict) and _matches(target, record) and prior is not None:
            record['prior_static'] = prior
            _save_ledger(home, ledger)
        _pick(home, theme, select, target)
        chosen = Path(_selection(home)['path'])
        if 'live-doom' not in chosen.name.lower():
            raise RuntimeError('The live background was not selected')
        return chosen


def remove_live_wallpaper(home: Path, fallback: Path, select, runtime: Path) -> dict:
    """Archive one unchanged owned personal choice, keeping every other image.

    If selected, a verified static choice is applied before archiving. The
    original image remains in place on picker failure; rollback is attempted
    only while the same theme and our attempted static selection remain.
    """
    home = Path(home).absolute()
    with _theme_lock(runtime):
        theme = _theme(home)
        if theme['name'].lower() == 'phobos':
            raise RuntimeError('Phobos live backgrounds are bundled and protected')
        selected = _selection(home)
        ledger = _ledger(home)
        owned = _owned_choice(home, fallback, theme, ledger, selected)
        if owned is None:
            raise RuntimeError('No unchanged Doom-owned personal live choice can be removed')
        path, record, legacy = owned
        # Capture verified legacy provenance while the original still exists;
        # the final archive journal is written after that original is unlinked.
        record = _persistent_record(record)
        if not _matches(path, record):
            raise RuntimeError('The live image changed; no image was removed')
        recovery_folder = _state(home) / 'recovered' / theme['name']
        _safe_folder(recovery_folder)
        replacement = None
        switched = removed = False
        try:
            if selected['path'] == str(path):
                replacement = _static_choice(home, theme, record)
                if replacement is None:
                    raise RuntimeError('No safe static background is available')
                switched = True
                _pick(home, theme, select, replacement)
            expected_selection = _selection(home)
            _assert_theme(home, theme)
            if expected_selection['path'] == str(path) or not _matches(path, record):
                raise RuntimeError('The live image or its selection changed; no image was removed')
            if not switched and expected_selection != selected:
                raise RuntimeError('Background selection changed; no image was removed')

            _directory(recovery_folder)
            os.chmod(_state(home), 0o700)
            data = read_bytes(path, max_bytes=32 * 1024 * 1024)
            if hashlib.sha256(data).hexdigest() != record['sha256']:
                raise RuntimeError('The live image changed; no image was removed')
            fd, archived = tempfile.mkstemp(prefix=path.stem + '.', suffix=path.suffix, dir=recovery_folder)
            recovery = Path(archived)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            # Persist recovery provenance before removing the original. A
            # failed final ledger write still leaves a recorded recovery file.
            archived_record = dict(record, recovery=str(recovery), state='pending-removal')
            ledger['images'][str(path)] = record
            ledger['recovered'].append(archived_record)
            _save_ledger(home, ledger)
            _assert_theme(home, theme)
            if _selection(home) != expected_selection or not _matches(path, record):
                raise RuntimeError('The theme, image or selection changed; no image was removed')
            path.unlink()
            removed = True
            archived_record['state'] = 'archived'
            ledger['images'].pop(str(path), None)
            _save_ledger(home, ledger)
            return {'theme': theme['name'], 'removed': str(path), 'recovery': str(recovery),
                    'selected': _selection(home)['path'], 'legacy_adopted': legacy}
        except Exception:
            if switched and not removed and replacement is not None:
                try:
                    _assert_theme(home, theme)
                    if _selection(home)['path'] == str(replacement.resolve(strict=True)) and path.is_file():
                        _pick(home, theme, select, path)
                except (OSError, RuntimeError):
                    pass
            raise
