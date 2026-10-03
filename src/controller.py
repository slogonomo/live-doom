#!/usr/bin/env python3
# SPDX-License-Identifier: 0BSD
"""One live Doom session, shared by the desktop and Omarchy screensaver."""
from __future__ import annotations
import argparse
import ctypes
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import stat
import struct
import subprocess
import sys
sys.dont_write_bytecode = True
import threading
import time
from contextlib import contextmanager

from control_server import ControlServer, MAX_REPLY_BYTES
from agent_control import AgentControl, valid_id
from background_selection import BackgroundSelection
from live_wallpaper import use_live_wallpaper, live_wallpaper_metadata, remove_live_wallpaper
from game_catalog import STORE_URL, read_catalog, scan_steam, selected_package, public_catalog
from project_paths import (PLUGIN_ID, ProjectPaths, ensure_private_dir, ensure_owned_dir,
                           open_regular, read_bytes, read_json, atomic_json, atomic_write, spawn_logged, close_process_log)
import onboarding
from saver_ownership import SaverOwnership
from process_utils import bounded_run, session_environment

ROOT = Path(__file__).resolve().parents[1]
GAME_KEYS = ('iwad', 'pwads', 'skill', 'start_map', 'game_package')
MAP_LABEL = re.compile(r'(?:E[1-9]M[1-9]|MAP(?:0[1-9]|[1-9][0-9]))\Z')
ACTIVE_RENDER_FPS = 35


def prepare_native_config(path, forced, defaults=None):
    """Preserve native preferences while enforcing the desktop backend.

    Eternity saves these files on clean exit. Keep its values and comments in
    place, including valid zero volumes; only integration-owned keys override
    existing entries. Seed optional defaults once, rather than at every launch.
    """
    limit = 1024 * 1024
    try:
        original = read_bytes(path, limit)
    except FileNotFoundError:
        original = b''
    lines = original.splitlines(keepends=True)
    names = {line.split(None, 1)[0] for line in lines if line.split(None, 1)}
    managed = {name.encode('ascii') for name in forced}
    data = b''.join(line for line in lines if not line.split(None, 1)
                    or line.split(None, 1)[0] not in managed)
    if data and not data.endswith(b'\n'):
        data += b'\n'
    values = {key: value for key, value in (defaults or {}).items()
              if key.encode('ascii') not in names}
    values.update(forced)
    data += b''.join(f'{key} {value}\n'.encode('ascii') for key, value in values.items())
    if data != original:
        atomic_write(path, data, max_bytes=limit)


def manual_game_changes(changes):
    changes = dict(changes)
    if any(key in changes for key in ('iwad', 'pwads', 'start_map')):
        changes['game_package'] = None
        if any(key in changes for key in ('iwad', 'pwads')):
            changes.setdefault('start_map', None)
    return changes


class Header(ctypes.LittleEndianStructure):
    _fields_ = [(n, ctypes.c_uint32) for n in
                ('magic', 'version', 'seq', 'width', 'height', 'stride',
                 'bot', 'paused', 'audible', 'gamestate')]
    _fields_ += [('frame', ctypes.c_uint64), ('tic', ctypes.c_uint64)]
    _fields_ += [(n, ctypes.c_int32) for n in
                ('leveltime', 'health', 'x', 'y', 'kills', 'items', 'secrets')]
    _fields_ += [('map', ctypes.c_char * 16)]
    _fields_ += [(n, ctypes.c_uint32) for n in ('pid', 'campaign', 'audio_peak')]
    _fields_ += [('angle', ctypes.c_int32)]
    _fields_ += [(n, ctypes.c_uint32) for n in ('pixel_aspect_num', 'pixel_aspect_den')]
    _fields_ += [(n, ctypes.c_int32) for n in ('fov', 'view_width', 'view_height', 'hud_layout')]
    _fields_ += [('bot_replans', ctypes.c_uint32)]
    _fields_ += [('render_fps', ctypes.c_uint32), ('render_lerp', ctypes.c_uint32),
                ('render_angle', ctypes.c_int32), ('human_quits', ctypes.c_uint32)]
    _fields_ += [('owner', ctypes.c_uint32), ('agent_gen', ctypes.c_uint32),
                 ('armor', ctypes.c_int32), ('ammo', ctypes.c_int32 * 4),
                 ('ready_weapon', ctypes.c_int32), ('weapons_owned', ctypes.c_uint32),
                 ('keys', ctypes.c_uint32), ('z', ctypes.c_int32), ('momx', ctypes.c_int32),
                 ('momy', ctypes.c_int32), ('damage_count', ctypes.c_int32),
                 ('deaths', ctypes.c_uint32), ('levels_completed', ctypes.c_uint32),
                 ('bot_stuck', ctypes.c_uint32)]


def runtime_dir() -> Path:
    return ProjectPaths.from_env().runtime_root()


def theme_runtime_dir() -> Path:
    value = os.environ.get('XDG_RUNTIME_DIR')
    if not value:
        raise ValueError('XDG_RUNTIME_DIR is required for Omarchy theme changes')
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts or not path.exists():
        raise ValueError('XDG_RUNTIME_DIR must be an existing absolute private directory')
    ensure_private_dir(path)
    return path


def state_dir() -> Path:
    return Path(os.environ.get('LIVE_DOOM_DATA') or os.environ.get('DOOM_DESKTOP_STATE')
                or ProjectPaths.from_env().data_root)


def settings_path() -> Path:
    override = os.environ.get('LIVE_DOOM_DATA') or os.environ.get('DOOM_DESKTOP_STATE')
    return Path(override) / 'config.json' if override else ProjectPaths.from_env().config_file


def config_for(path: Path):
    default = {'iwad': str(ProjectPaths.from_env().default_iwad), 'pwads': [],
               'watch_sound': False, 'skill': 2, 'enabled': True,
               'render_width': 0, 'render_height': 0,
               'wallpaper_enabled': True, 'wallpaper_mode': 'empty',
               'wallpaper_behind_windows': 'light',
               'screensaver_enabled': True, 'playing_sound': True,
               'desktop_sound': False, 'screensaver_sound': False,
               'pause_blur': 20, 'mouse_sensitivity': 1.0, 'mouselook': False,
               'keep_game_loaded': False, 'agent_selected': 'autodoom', 'agent_screensaver': True,
               'start_map': None, 'game_package': None}
    if path.exists():
        stored = read_json(path, max_bytes=65536)
        if not isinstance(stored, dict):
            raise ValueError('Settings must be a JSON object')
        default.update(stored)
        # Preserve the former shared watching-sound preference on upgrade.
        for key in ('desktop_sound', 'screensaver_sound'):
            if key not in stored:
                default[key] = bool(stored.get('watch_sound', False))
    # Retain the retired key for old full-config clients, without keeping a
    # deselected game resident or rewriting settings merely by reading them.
    default['keep_game_loaded'] = False
    return default


def validate_wad(path, expected=None, names=None):
    """Reject incomplete/corrupt WAD containers before replacing live settings."""
    path = Path(path).expanduser().resolve()
    with open_regular(path, require_owner=False) as stream:
        size = os.fstat(stream.fileno()).st_size
        raw = stream.read(12)
        if len(raw) != 12:
            raise ValueError(f'Incomplete Doom WAD: {path.name}')
        tag, count, offset = struct.unpack('<4sii', raw)
        if tag not in (b'IWAD', b'PWAD') or (expected and tag != expected):
            if expected == b'IWAD':
                raise ValueError('Choose an IWAD containing a complete game.')
            raise ValueError(f'Choose a valid Doom WAD: {path.name}')
        if count <= 0 or offset < 12 or offset > size or count > (size - offset) // 16:
            raise ValueError(f'Invalid WAD directory: {path.name}')
        stream.seek(offset)
        for _ in range(count):
            position, length, name = struct.unpack('<ii8s', stream.read(16))
            if position < 0 or length < 0 or position > size or length > size - position:
                raise ValueError(f'Invalid WAD lump: {path.name}')
            if names is not None:
                names.add(name.rstrip(b'\0').upper())
    return path


def render_dimensions(config, headless=False, monitors=None):
    width, height = config.get('render_width', 0), config.get('render_height', 0)
    if any(type(value) is not int or not 0 <= value <= 16384 for value in (width, height)):
        raise ValueError('Render dimensions must be whole pixels between 0 and 16384.')
    if not width or not height:
        width, height = 640, 400
        if not headless:
            try:
                if monitors is None:
                    monitors = json.loads(bounded_run(['/usr/bin/hyprctl', '-j', 'monitors'],
                                                      timeout=2, check=True).stdout)
                available = [m for m in monitors if not m.get('disabled')]
                monitor = next((m for m in available if m.get('focused')), available[0] if available else {})
                width, height = int(monitor.get('width', width)), int(monitor.get('height', height))
                if int(monitor.get('transform', 0)) % 2:
                    width, height = height, width
            except (OSError, ValueError, KeyError, subprocess.SubprocessError):
                pass
    if width < 320 or height < 200:
        raise ValueError('Render dimensions must be at least 320 × 200.')
    factor = min(1, 3840 / width, 2160 / height)
    return max(320, int(width * factor)), max(200, int(height * factor))


def validate_config(config):
    base_names = set()
    validate_wad(config['iwad'], b'IWAD', names=base_names)
    if not isinstance(config['pwads'], list):
        raise ValueError('Additional WADs must be a list of file paths.')
    all_names = set(base_names)
    for path in config['pwads']:
        validate_wad(path, names=all_names)
    start = config['start_map']
    if start is not None and (not isinstance(start, str) or not MAP_LABEL.fullmatch(start)):
        raise ValueError('Choose a Doom map such as E1M1 or MAP01.')
    if start is not None and start.encode() not in all_names:
        raise ValueError('The selected starting map is not present in these game files.')
    if config['game_package'] is not None and not valid_id(config['game_package']):
        raise ValueError('Choose a valid installed game package.')
    if config['pwads'] and b'E1M1' in base_names and b'E2M1' not in base_names and b'MAP01' not in base_names:
        raise ValueError('Doom shareware cannot load custom WADs. Choose a complete Doom or Doom II IWAD, or Freedoom.')
    if type(config['skill']) is not int or not 1 <= config['skill'] <= 5:
        raise ValueError('Doom skill must be between 1 and 5.')
    render_dimensions(config, headless=True)
    if config['wallpaper_mode'] not in ('empty', 'all'):
        raise ValueError('Live background must use empty desktops or all desktops.')
    if config.get('wallpaper_behind_windows', 'light') not in ('light', 'full'):
        raise ValueError('Behind windows must use Light or Full speed.')
    for key in ('enabled', 'wallpaper_enabled', 'screensaver_enabled',
                'playing_sound', 'desktop_sound', 'screensaver_sound', 'mouselook', 'keep_game_loaded', 'agent_screensaver'):
        if not isinstance(config[key], bool):
            raise ValueError(f'{key} must be on or off.')
    if isinstance(config['pause_blur'], bool) or not isinstance(config['pause_blur'], int) or not 0 <= config['pause_blur'] <= 100:
        raise ValueError('Pause blur must be between 0 and 100%.')
    if not valid_id(config['agent_selected']):
        raise ValueError('Choose a valid external agent id or autodoom.')
    sensitivity = config['mouse_sensitivity']
    if isinstance(sensitivity, bool) or not isinstance(sensitivity, (int, float)) or not 0.1 <= sensitivity <= 4:
        raise ValueError('Mouse sensitivity must be between 0.1 and 4.')


def request(command: str, directory: Path | None = None, timeout=2.0) -> str:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str((directory or runtime_dir()) / 'control.sock'))
        sock.sendall((command + '\n').encode())
        answer = bytearray()
        while not answer.endswith(b'\n') and len(answer) <= MAX_REPLY_BYTES:
            chunk = sock.recv(4096)
            if not chunk:
                break
            answer.extend(chunk)
        if len(answer) > MAX_REPLY_BYTES:
            raise ValueError('Controller response is too large')
        return answer.decode().strip()


def engine_request(command: str, directory: Path) -> str:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.7)
        sock.connect(str(directory / 'engine.sock'))
        sock.sendall((command + '\n').encode())
        return sock.recv(1024).decode().strip()


def engine_request_batch(commands, directory: Path):
    """Send ordered input together, avoiding a game-tic wait for every event."""
    if not commands:
        return []
    payload = ('\n'.join(commands) + '\n').encode()
    if len(commands) > 128 or len(payload) > 4096:
        raise ValueError('Input batch is too large')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.7)
        sock.connect(str(directory / 'engine.sock'))
        sock.sendall(payload)
        sock.shutdown(socket.SHUT_WR)
        answer = bytearray()
        while answer.count(b'\n') < len(commands):
            chunk = sock.recv(4096)
            if not chunk:
                raise OSError('Engine input connection closed before acknowledging every event')
            answer.extend(chunk)
            if len(answer) > 16384:
                raise OSError('Engine input reply is too large')
        return answer.decode().splitlines()[:len(commands)]


def read_header(path: Path):
    try:
        with open_regular(path) as stream:
            raw = stream.read(ctypes.sizeof(Header))
            stream.seek(Header.seq.offset)
            sequence = int.from_bytes(stream.read(4), 'little')
        if len(raw) != ctypes.sizeof(Header):
            return {}
        header = Header.from_buffer_copy(raw)
        if header.magic != 0x44444f4d or header.version not in (2, 3) or header.seq & 1 or header.seq != sequence:
            return {}
        result = {name: getattr(header, name) for name, _ in Header._fields_}
        result['map'] = result['map'].decode(errors='replace')
        result['ammo'] = list(result['ammo'])
        if header.version == 2:
            for name, _ in Header._fields_[Header._fields_.index(('owner', ctypes.c_uint32)):]:
                result[name] = [0] * 4 if name == 'ammo' else 0
            result['owner'] = 0 if header.bot else 2
        return result
    except OSError:
        return {}


def workspace_views(monitors, clients):
    """Mapped windows on active or visible special workspaces block that output."""
    busy = {c.get('workspace', {}).get('id') for c in clients
            if c.get('mapped', True) and not c.get('hidden', False)}
    views = {}
    for monitor in monitors:
        if monitor.get('disabled'):
            continue
        active = monitor.get('activeWorkspace', {}).get('id')
        special = monitor.get('specialWorkspace', {}).get('id', 0)
        reserved = monitor.get('reserved', [0, 0, 0, 0])
        if (not isinstance(reserved, (list, tuple)) or len(reserved) != 4
                or any(isinstance(value, bool) or not isinstance(value, (int, float))
                       or (isinstance(value, float) and not math.isfinite(value)) for value in reserved)):
            reserved = [0, 0, 0, 0]
        reserved = tuple(max(0, min(16384, math.ceil(value))) for value in reserved)
        views[monitor['name']] = {
            'empty': active not in busy and (not special or special not in busy),
            'workspace': (active, special),
            'awake': monitor.get('dpmsStatus', True),
            'focused': monitor.get('focused', False),
            # Hyprland reports panel reservations in output logical units.
            # The image stays full-output; only viewer text uses these insets.
            'reserved': reserved,
        }
    return views


def output_render_rates(monitors):
    """Use native Doom cadence on every output, independent of refresh rate."""
    rates = {}
    for monitor in monitors:
        name = monitor.get('name')
        if not isinstance(name, str) or not name or monitor.get('disabled'):
            continue
        rates[name] = ACTIVE_RENDER_FPS
    return rates


class Controller:
    def __init__(self, runtime: Path, state: Path, headless=False, *, config_path=None):
        self.runtime, self.state, self.headless = runtime, state, headless
        self.paths = ProjectPaths.from_env()
        self.config_path = Path(config_path) if config_path is not None else state / 'config.json'
        self.accepted_path = self.config_path.with_name('config.last-good.json')
        self.log_root = state if self.config_path.parent == state else self.paths.state_root
        self.saver_ownership = None if headless else SaverOwnership(Path.home(), self.log_root)
        self.manage_plugin = not headless and (ROOT.name == PLUGIN_ID or
                             (ROOT.parent.name == PLUGIN_ID and ROOT.name.startswith('impl-')))
        self.onboarding_downloading = False
        if not headless:
            onboarding.initialize(self.config_path, state, home=Path.home())
        self.last_error = None
        try:
            self.config = config_for(self.config_path)
        except (OSError, ValueError) as exc:
            if not self.accepted_path.exists():
                raise
            self.config = config_for(self.accepted_path)
            self.last_error = f'Invalid settings restored from the last working configuration: {exc}'
        self.views = {'TEST': {'empty': True, 'workspace': (1, 0), 'awake': True, 'focused': True}} if headless else {}
        self.locked = False
        self.snapshot_ok = headless
        self.human_output = None
        self.human_workspace = None
        self.input_generation = 0
        self.saver = None
        self.engine = None
        self.viewer = None
        self.settings_process = None
        self.menu_hide_process = None
        self.last_policy = None
        self.output_rates = {}
        self.last_render_rate = None
        self.native_schema = 2
        self.last_human_quits = 0
        self.last_controls = None
        self.menu_until = 0.0
        self.mode = 'paused'
        self.alive = True
        self.sync = threading.Lock()
        self.last_save = time.monotonic()
        self.last_save_attempt = self.last_save
        self.snapshot_thread = None
        self.render_size = None
        self.pending_render_size = None
        self.background = None if headless else BackgroundSelection(Path.home(), state)
        self.steam_catalog = read_catalog(state)
        self.last_background_poll = 0.0
        self.last_unload_attempt = 0.0
        self.agents = AgentControl(self)

    def agent_native(self, command):
        return engine_request(command, self.runtime)

    def agent_header(self):
        return read_header(self.runtime / 'frame.bin') if self.engine_loaded() else {}

    def log(self, message):
        print(time.strftime('%Y-%m-%d %H:%M:%S'), message, flush=True)

    def plugin_enabled(self):
        plugin = ROOT.parent if ROOT.parent.name == PLUGIN_ID else ROOT
        if (not (ROOT / 'src/controller.py').is_file()
                or not (plugin / 'manifest.json').is_file() or (plugin / 'manifest.json').is_symlink()):
            return False
        try:
            config = read_json(Path.home() / '.config/omarchy/shell.json', max_bytes=MAX_REPLY_BYTES)
        except (OSError, ValueError):
            return False
        return (isinstance(config, dict)
                and PLUGIN_ID not in config.get('disabledPlugins', [])
                and any(isinstance(entry, dict) and entry.get('id') == PLUGIN_ID
                        and entry.get('enabled', True) is not False
                        for entry in config.get('plugins', [])))

    def check_saver_ownership(self):
        if self.saver_ownership is None:
            return
        status = self.saver_ownership.status()
        if status['revoked'] and self.config['screensaver_enabled']:
            self.config['screensaver_enabled'] = False
            atomic_json(self.config_path, self.config)
            self.stop_saver()
            self.saver_ownership.release()
            self.log('Screensaver toggle changed by the user; Doom screensaver disabled')
        elif not (self.config['enabled'] and self.config['screensaver_enabled']) and status['owned']:
            self.saver_ownership.release()

    def start_engine(self, recover_checkpoint=True):
        from campaign_metadata import prepare_campaign
        self.last_render_rate = None
        self.last_human_quits = 0
        iwad = validate_wad(self.config['iwad'], b'IWAD')
        files = [iwad] + [validate_wad(p) for p in self.config['pwads']]
        digest = hashlib.sha256()
        for wad in files:
            with wad.open('rb') as stream:
                while data := stream.read(1024 * 1024):
                    digest.update(data)
        adapter = prepare_campaign(self.config['game_package'], self.config['start_map'],
                                   self.config['pwads'], self.runtime / 'campaign-info.wad')
        if self.config['start_map'] is not None:
            digest.update(b'\0start-map\0' + self.config['start_map'].encode())
        if adapter is not None:
            digest.update(b'\0campaign-metadata\0' + adapter.read_bytes())
        game_dir = self.state / 'games' / digest.hexdigest()[:20]
        # Existing installs retain their exact save/user directory in place.
        # The legacy path is derived locally, never supplied by a manifest.
        legacy = self.paths.legacy_data_root / 'games' / game_dir.name
        if (not self.headless and self.state == self.paths.data_root
                and self.config_path == self.paths.config_file
                and not game_dir.exists() and legacy.is_dir()):
            ensure_owned_dir(legacy)
            game_dir = legacy
        saves = game_dir / 'saves'
        user = game_dir / 'user'
        for path in (saves,) + tuple(user / name for name in ('doom', 'doom2', 'tnt', 'plutonia', 'hacx', 'heretic', 'shots')):
            ensure_owned_dir(path)
        # Keep the rendering backend deterministic and unobtrusive. Sound is
        # generated normally and gated by the engine bridge's audio command.
        engine_config = game_dir / 'desktop.cfg'
        width, height = render_dimensions(self.config, self.headless)
        self.render_size = width, height
        prepare_native_config(user / 'system.cfg',
                              {'i_videodriverid': 0, 'i_videomode': f'"{width}x{height}w"'})
        prepare_native_config(engine_config,
                              {'use_vsync': 0, 'wipewait': 0, 'i_showendoom': 0, 'use_mouse': 1,
                               'screensize': 8, 'hud_enabled': 1, 'hud_overlaylayout': 4},
                              {'snd_mididevice': 0})
        env = os.environ.copy()
        env.update(SDL_VIDEODRIVER='dummy', SDL_RENDER_DRIVER='software',
                   DOOM_DESKTOP_FRAME=str(self.runtime / 'frame.bin'),
                   DOOM_DESKTOP_ENGINE_SOCKET=str(self.runtime / 'engine.sock'))
        if self.headless:
            env['SDL_AUDIODRIVER'] = 'dummy'
        args = [str(self.paths.engine), '-base', str(self.paths.engine_base),
                '-user', str(user), '-save', str(saves), '-config', str(engine_config),
                '-iwad', str(iwad), '-skill', str(int(self.config['skill'])),
                '-warp', self.config['start_map'] or '1']
        addons = [str(w) for w in files[1:]] + ([str(adapter)] if adapter is not None else [])
        if addons:
            args += ['-file'] + addons
        self.engine = spawn_logged(args, self.log_root / 'engine.log', env=env, cwd=self.state)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.engine.poll() is not None:
                raise RuntimeError(f'Engine exited ({self.engine.returncode}); see {self.log_root / "engine.log"}')
            header = read_header(self.runtime / 'frame.bin')
            if header.get('frame', 0) and header.get('pid') == self.engine.pid:
                self.native_schema = header['version']
                break
            time.sleep(0.05)
        else:
            raise RuntimeError('Engine did not produce a frame within 60 seconds')
        checkpoint = saves / 'etersav7.dsg'
        if checkpoint.exists():
            try:
                answer = engine_request('load', self.runtime)
                if answer != 'OK':
                    raise RuntimeError(answer)
                if self.engine is not None and self.engine.poll() is not None:
                    raise RuntimeError('Engine exited while restoring the checkpoint')
                self.log('Checkpoint: OK')
            except (OSError, RuntimeError) as exc:
                if not recover_checkpoint:
                    raise
                rejected = checkpoint.with_name(f'etersav7.rejected-{time.time_ns()}.dsg')
                checkpoint.replace(rejected)
                self.shutdown_engine(checkpoint=False)
                self.last_error = f'Unable to restore checkpoint; kept it at {rejected}. Starting this game fresh: {exc}'
                self.log(self.last_error)
                return self.start_engine(recover_checkpoint=False)
        self.last_policy = None
        self.last_controls = None
        if adapter is not None:
            answer = engine_request('campaign ' + self.config['start_map'], self.runtime)
            if answer != 'OK':
                raise RuntimeError('Unable to set the selected campaign restart: ' + answer)
        self.apply_controls()
        self.log(f'Engine ready: PID {self.engine.pid}, IWAD {iwad.name}')

    def apply_controls(self):
        controls = self.config['mouse_sensitivity'], self.config['mouselook']
        if controls != self.last_controls:
            answer = engine_request(f'controls {controls[0]} {int(controls[1])}', self.runtime)
            if answer != 'OK':
                raise RuntimeError('Unable to apply mouse settings: ' + answer)
            self.last_controls = controls

    def engine_loaded(self):
        return self.engine is not None and self.engine.poll() is None

    def record_preferences(self):
        accepted = config_for(self.accepted_path) if self.accepted_path.exists() else self.config
        value = self.config if self.engine_loaded() else self.config | {
            key: accepted[key] for key in GAME_KEYS}
        atomic_json(self.accepted_path, value)

    def ensure_engine(self):
        if self.engine_loaded():
            return
        try:
            self.start_engine()
        except (OSError, RuntimeError, ValueError) as exc:
            self.shutdown_engine(checkpoint=False)
            accepted = config_for(self.accepted_path) if self.accepted_path.exists() else None
            if accepted is None or all(accepted[k] == self.config[k] for k in GAME_KEYS):
                raise
            # Preferences may be edited while the game is unloaded. Restore
            # only a failed game selection, keeping the current preferences.
            validate_config(accepted)
            self.config.update({k: accepted[k] for k in GAME_KEYS})
            atomic_json(self.config_path, self.config)
            self.start_engine()
            self.last_error = f'Unable to load the selected game; restored the last working game: {exc}'
            self.log(self.last_error)
        self.record_preferences()

    def stop_desktop_viewer(self):
        viewer, self.viewer = self.viewer, None
        if viewer is not None:
            if viewer.poll() is None:
                viewer.terminate()
            try:
                viewer.wait(timeout=1)
            except subprocess.TimeoutExpired:
                viewer.kill()
                viewer.wait(timeout=1)
            close_process_log(viewer)

    def unload_engine(self):
        if self.engine is None:
            return True
        self.return_bot()
        self.stop_desktop_viewer()
        if self.engine_loaded():
            # A genuine checkpoint error must not silently throw away the
            # world. Keep the muted paused engine and retry at a bounded rate.
            if time.monotonic() - self.last_unload_attempt < 5:
                return False
            self.last_unload_attempt = time.monotonic()
            self.last_policy = None
            try:
                engine_request('audio 0', self.runtime)
                engine_request('pause 1', self.runtime)
                answer = self.save_checkpoint()
            except OSError as exc:
                self.last_error = 'Checkpoint failed: ' + str(exc)
                return False
            if answer not in ('OK', 'ERR no live level to save'):
                return False
            if answer != 'OK':
                self.log('No live level to save; next load restores the last level checkpoint')
        self.shutdown_engine(checkpoint=False)
        self.agents.tick()
        # All consumers are gone. Release the tmpfs framebuffer too rather
        # than keeping its pages charged to the otherwise idle service.
        (self.runtime / 'frame.bin').unlink(missing_ok=True)
        (self.runtime / 'engine.sock').unlink(missing_ok=True)
        self.log('Game unloaded; screensaver can reload its checkpoint')
        return True

    def reconcile_runtime(self):
        # Reap/dismiss before deciding whether the screensaver still needs
        # the shared engine. Locking always revokes a saver first.
        saver = self.saver is not None and (self.headless or self.saver.poll() is None)
        if self.saver is not None and (self.locked or not saver):
            self.stop_saver()
            saver = False
        wanted = self.config['wallpaper_enabled'] or saver
        if not self.alive:
            return
        if not wanted:
            self.stop_desktop_viewer()
            self.unload_engine()
            self.agents.tick()
            return
        self.ensure_engine()
        desktop = self.config['wallpaper_enabled'] and not self.headless and self.snapshot_ok and not self.locked
        if desktop:
            if self.viewer is not None and self.viewer.poll() is not None:
                self.stop_desktop_viewer()
                self.return_bot()
            if self.viewer is None:
                self.viewer = self.launch_viewer()
        else:
            self.stop_desktop_viewer()
        self.agents.tick()

    def reload_config(self, candidate=None):
        with self.menu_operation(130):
            return self._reload_config(candidate)

    @contextmanager
    def menu_operation(self, timeout):
        # Reloads may cold-start a different game. Preserve an already-held
        # pause while that bounded synchronous work is in progress; queued
        # renewals can then extend it when command dispatch resumes. Never
        # acquire a lease here, or restore one revoked by lock/idle handling.
        started = time.monotonic()
        with self.sync:
            lease = self.menu_until
            protected = started + timeout if lease > started and not self.locked and self.snapshot_ok else None
            if protected is not None:
                self.menu_until = protected
        try:
            yield
        finally:
            with self.sync:
                now = time.monotonic()
                if protected is not None and self.menu_until == protected:
                    self.menu_until = (now + min(6, lease - started)
                                       if now < protected and not self.locked and self.snapshot_ok else 0)

    def _reload_config(self, candidate=None):
        previous = dict(self.config)
        try:
            candidate = config_for(self.config_path) if candidate is None else candidate
            candidate = {'wallpaper_behind_windows': 'light'} | dict(candidate) | {'keep_game_loaded': False}
            validate_config(candidate)
            if candidate['agent_selected'] != previous['agent_selected'] and candidate['agent_selected'] != 'autodoom':
                self.agents.available()
                if candidate['agent_selected'] not in self.agents.manifests:
                    raise ValueError('External agent is unavailable: ' + candidate['agent_selected'])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            atomic_json(self.config_path, previous)
            self.last_error = str(exc)
            return 'ERR ' + self.last_error
        game_changed = any(candidate[k] != previous[k] for k in GAME_KEYS)
        loaded = self.engine_loaded()
        if not (self.last_error or '').startswith('Checkpoint failed: '):
            self.last_error = None
        # This choice affects only the viewer's per-output presentation stride.
        # Preserve the native world, audio/input policy and render-rate cache.
        if (candidate['wallpaper_behind_windows'] != previous['wallpaper_behind_windows']
                and candidate == previous | {'wallpaper_behind_windows': candidate['wallpaper_behind_windows']}):
            self.config = candidate
            atomic_json(self.config_path, self.config)
            self.record_preferences()
            return 'OK'
        old_size = self.render_size
        if game_changed and not self.accepted_path.exists():
            atomic_json(self.accepted_path, previous)
        if game_changed and loaded:
            self.return_bot()
            self.stop_saver()
            self.stop_desktop_viewer()
            if not self.unload_engine():
                self.reconcile_runtime()
                self.policy()
                return 'ERR ' + (self.last_error or 'Unable to checkpoint the current game')
        self.config = candidate
        self.last_render_rate = None
        try:
            if self.saver_ownership is not None and any(candidate[k] != previous[k]
                                                        for k in ('enabled', 'screensaver_enabled')):
                # An explicit re-enable can retire a stale private token,
                # while a user-owned suppression toggle remains untouched.
                self.saver_ownership.release()
            if game_changed and loaded:
                self.start_engine()
            elif loaded:
                dimensions = render_dimensions(candidate, self.headless)
                if dimensions != self.render_size:
                    answer = engine_request(f'video {dimensions[0]} {dimensions[1]}', self.runtime)
                    if answer != 'OK':
                        raise RuntimeError('Unable to change render dimensions: ' + answer)
                    self.render_size = dimensions
                self.apply_controls()
            if not (self.config['enabled'] and self.config['screensaver_enabled']):
                self.stop_saver()
            self.reconcile_runtime()
        except (OSError, RuntimeError, ValueError) as exc:
            self.config = previous
            atomic_json(self.config_path, previous)
            if not game_changed and loaded and self.engine_loaded():
                # Recover native preferences in place without discarding the
                # live world for a rejected video/control setting.
                if old_size and self.render_size != old_size:
                    engine_request(f'video {old_size[0]} {old_size[1]}', self.runtime)
                    self.render_size = old_size
                self.last_controls = None
                self.apply_controls()
            else:
                self.shutdown_engine(checkpoint=False)
                self.reconcile_runtime()
            self.last_error = f'Unable to apply settings; restored the previous settings: {exc}'
            self.log(self.last_error)
            self.policy()
            return 'ERR ' + self.last_error
        atomic_json(self.config_path, self.config)
        # An unloaded game-file change is validated as a container now and
        # proven by the native engine on the next load. Preserve the previous
        # working game for recovery if a well-formed IWAD is unsupported.
        if self.engine_loaded() or not game_changed:
            self.record_preferences()
        self.policy()
        return 'OK'

    def launch_viewer(self, saver=False, preview=False):
        if preview and not saver:
            raise ValueError('Preview requires a screensaver surface')
        args = [str(self.paths.viewer), '--frame', str(self.runtime / 'frame.bin'),
                '--socket', str(self.runtime / 'control.sock')]
        if saver:
            args += ['--screensaver-layer', '--omarchy-marker', 'org.omarchy.screensaver']
            if preview:
                args.append('--preview')
        return spawn_logged(args, self.log_root / 'viewer.log', cwd=self.state)

    def stop_saver(self):
        saver, self.saver = self.saver, None
        if saver is not None and not isinstance(saver, bool):
            if saver.poll() is None:
                saver.terminate()
            try:
                saver.wait(timeout=1)
            except subprocess.TimeoutExpired:
                saver.kill()
                saver.wait(timeout=1)
            close_process_log(saver)

    def snapshots(self):
        while self.alive:
            try:
                def fetch(what):
                    result = bounded_run(['/usr/bin/hyprctl', '-j', what], timeout=2, check=True)
                    return json.loads(result.stdout)
                monitors, clients = fetch('monitors'), fetch('clients')
                lock = bounded_run(['/usr/bin/omarchy-shell', 'lock', 'isLocked'], timeout=2,
                                   max_output_bytes=16384, check=True).stdout.decode().strip()
                if lock not in ('true', 'false'):
                    raise ValueError('Unknown lock state')
                with self.sync:
                    self.views = workspace_views(monitors, clients)
                    self.output_rates = output_render_rates(monitors)
                    self.locked = lock == 'true'
                    self.snapshot_ok = True
                    self.pending_render_size = render_dimensions(self.config, monitors=monitors)
            except (OSError, ValueError, subprocess.SubprocessError):
                with self.sync:
                    self.snapshot_ok = False
            time.sleep(0.75 if self.locked and self.snapshot_ok else 0.25)

    def return_bot(self):
        self.human_output = self.human_workspace = None
        self.input_generation += 1
        try:
            engine_request('release', self.runtime)
        except OSError:
            pass

    def policy(self):
        self.agents.tick()
        with self.sync:
            views = dict(self.views)
            locked, good = self.locked, self.snapshot_ok
        if self.human_output:
            quits = read_header(self.runtime / 'frame.bin').get('human_quits')
            if quits is not None and quits != self.last_human_quits:
                self.last_human_quits = quits
                self.log('Native Quit returned control to the bot')
                self.return_bot()
        if self.human_output:
            own = views.get(self.human_output, {})
            if (locked or not good or not self.config['wallpaper_enabled'] or not own.get('empty') or not own.get('awake') or
                    own.get('workspace') != self.human_workspace or not own.get('focused')):
                self.log(f'Releasing human control: locked={locked}, snapshot_ok={good}, view={own}, session_workspace={self.human_workspace}')
                self.return_bot()
        saver = self.saver is not None and (self.headless or self.saver.poll() is None)
        if self.saver is not None and not saver:
            self.saver = None
            self.log('Screensaver dismissed; same game returned to desktop')
        if locked and self.saver:
            self.stop_saver()
            saver = False
        enabled = bool(self.config['enabled'])
        wallpaper = bool(self.config['wallpaper_enabled'])
        menu = time.monotonic() < self.menu_until
        human = bool(self.human_output) and not saver
        running = self.alive and enabled and good and not locked and not menu and any(v['awake'] for v in views.values()) and (saver or human or (wallpaper and any(
            (v['empty'] or self.config['wallpaper_mode'] == 'all') and v['awake'] for v in views.values())))
        audible = running and bool(self.config['playing_sound'] if human else self.config['screensaver_sound'] if saver else self.config['desktop_sound'])
        owner = self.agents.desired_owner(human, saver, running)
        pause_reason = 'lock' if locked or not good else 'menu' if menu else 'asleep' if not any(v['awake'] for v in views.values()) else 'busy' if not running else 'none'
        self.mode = 'screensaver' if saver and running else 'human' if human and running else 'agent' if owner == 1 and running else 'bot' if running else 'paused'
        if not self.engine_loaded():
            self.mode = 'paused'
            self.last_policy = None
            self.agents.notify(0, pause_reason, {})
            return (0, True, False, self.agents.generation)
        if running and (human or owner == 1):
            rate = ACTIVE_RENDER_FPS
            if rate != self.last_render_rate:
                answer = engine_request(f'render {rate}', self.runtime)
                if answer == 'OK':
                    self.last_render_rate = rate
                else:
                    self.last_error = 'Unable to set active-play rendering rate: ' + answer
        next_policy = (owner, not running, audible, self.agents.generation)
        if next_policy != self.last_policy:
            # Gate audio before changing input/clock ownership.
            ownership = f'owner {owner} {self.agents.generation}' if self.native_schema >= 3 else f'bot {int(not human)}'
            commands = (f'audio {int(audible)}', ownership, f'pause {int(not running)}')
            answers = (engine_request_batch(commands, self.runtime) if self.native_schema >= 3
                       else [engine_request(command, self.runtime) for command in commands])
            if any(answer != 'OK' for answer in answers):
                raise RuntimeError('Unable to apply game policy: ' + '; '.join(answers))
            self.last_policy = next_policy
            self.log(f'Mode {self.mode}; sound {"on" if audible else "muted"}')
        self.agents.notify(owner, pause_reason, self.agent_header() if self.agents.connected else {})
        return next_policy

    def command(self, line):
        parts = line.split()
        if not parts:
            return 'ERR empty command'
        name = parts[0]
        if name in ('saver-claim', 'saver-release', 'saver-status') and len(parts) == 1:
            if self.saver_ownership is None:
                return 'ERR screensaver ownership requires an Omarchy session'
            method = {'saver-claim': 'claim', 'saver-release': 'release', 'saver-status': 'status'}[name]
            if name == 'saver-claim' and not (self.config['enabled'] and self.config['screensaver_enabled']):
                return 'ERR Doom screensaver is disabled'
            return json.dumps(getattr(self.saver_ownership, method)())
        if name == 'onboarding':
            try:
                if len(parts) == 2 and parts[1] in ('finish', 'skip', 'dismiss'):
                    onboarding.complete(self.state, 'skipped' if parts[1] == 'skip' else 'finished',
                                        config=self.config)
                elif len(parts) >= 3 and parts[1] == 'step':
                    flags = parts[3:]
                    if len(flags) % 2:
                        raise ValueError('Onboarding choices need a flag and value')
                    choices = {}
                    for flag, value in zip(flags[::2], flags[1::2]):
                        if flag not in ('--background', '--screensaver') or flag in choices:
                            raise ValueError('Unknown or repeated onboarding choice')
                        choices[flag] = value
                    saver = choices.get('--screensaver')
                    if saver is not None and saver not in ('on', 'off'):
                        raise ValueError('Onboarding screensaver choice must be on or off')
                    onboarding.set_progress(self.state, parts[2], background=choices.get('--background'),
                                            screensaver=None if saver is None else saver == 'on', config=self.config)
                else:
                    return 'ERR invalid onboarding command'
                return json.dumps(self.settings_json())
            except (OSError, ValueError, RuntimeError) as error:
                return 'ERR ' + str(error)
        if name == 'download-shareware':
            if parts[1:] != ['--accept-terms']:
                return 'ERR review the shareware terms and pass --accept-terms to download'
            if onboarding.SHAREWARE_SOURCE is None:
                return 'ERR verified shareware source is not configured'
            with self.menu_operation(300):
                self.onboarding_downloading = True
                try:
                    onboarding.download_shareware(self.state, accept_terms=True)
                    self.onboarding_downloading = False
                    return self.command('use-game shareware')
                finally:
                    self.onboarding_downloading = False
        if name == 'scan-steam' and len(parts) == 1:
            try:
                self.steam_catalog = scan_steam(Path.home(), self.state)
            except (OSError, ValueError, RuntimeError) as error:
                return 'ERR Steam scan failed: ' + str(error)
            return json.dumps(self.settings_json())
        if name == 'use-game' and len(parts) == 2:
            if parts[1] in ('freedoom', 'shareware'):
                try:
                    choice = onboarding.bundled_game(self.state, parts[1], verify=True)
                except (OSError, ValueError) as error:
                    return 'ERR ' + str(error)
                answer = self.reload_config(self.config | {
                    'iwad': choice['iwad'], 'pwads': [], 'start_map': None, 'game_package': None})
                return json.dumps(self.settings_json()) if answer == 'OK' else answer
            self.steam_catalog = read_catalog(self.state)
            package = next((row for row in self.steam_catalog['packages'] if row['id'] == parts[1]), None)
            if package is None:
                return 'ERR Steam game is not available; use Find Steam games first'
            if package.get('compatible') is not True:
                return 'ERR ' + (package.get('reason') or 'This campaign needs a different engine')
            if any(not Path(path).is_file() for path in [package['iwad']] + package['pwads']):
                return 'ERR Steam game files are missing; install the game and scan again'
            answer = self.reload_config(self.config | {
                'iwad': package['iwad'], 'pwads': package['pwads'],
                'start_map': package['start_map'], 'game_package': package['id']})
            return json.dumps(self.settings_json()) if answer == 'OK' else answer
        if name in ('use-live-wallpaper', 'remove-live-wallpaper') and len(parts) == 1:
            if self.headless:
                return 'ERR live background selection requires an Omarchy desktop'
            self.log(name + ': requested')
            try:
                def select(path):
                    result = bounded_run(['/usr/bin/omarchy', 'theme', 'bg', 'set', str(path)],
                                         text=True, timeout=2, max_output_bytes=16384)
                    if result.returncode:
                        raise RuntimeError(result.stderr.strip() or 'Omarchy could not select the background')
                operation = use_live_wallpaper if name == 'use-live-wallpaper' else remove_live_wallpaper
                operation(Path.home(), ROOT / 'assets/live-doom.webp', select, theme_runtime_dir())
                if self.background is not None:
                    self.background.poll()
                selected = (Path.home() / '.local/state/omarchy/current/background').resolve(strict=True)
                self.config['wallpaper_enabled'] = 'live-doom' in selected.name.lower()
                atomic_json(self.config_path, self.config)
                self.record_preferences()
                self.reconcile_runtime()
                self.policy()
                self.log(name + ': completed')
                return 'OK'
            except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
                lines = str(exc).splitlines()
                reason = (lines[-1] if lines else 'Unable to select the live background')[:1024]
                self.log(name + ': failed: ' + reason)
                return 'ERR ' + reason
        if name == 'view' and len(parts) == 2:
            output = parts[1]
            with self.sync:
                view = self.views.get(output)
                hidden = self.locked or not self.snapshot_ok or self.saver is not None or not self.config['wallpaper_enabled']
            paused = view and (not view['awake'] or not self.config['enabled'] or time.monotonic() < self.menu_until or (not view['empty'] and self.config['wallpaper_mode'] == 'empty'))
            mode = 'hidden' if hidden or not view else 'paused' if paused else 'human' if output == self.human_output else 'bot'
            reserved = (view or {}).get('reserved', (0, 0, 0, 0))
            insets = ' '.join(str(value) for value in reserved)
            # Simulation and exported observations retain their native rate.
            # Only a busy output showing the background everywhere needs fewer
            # viewer presentations; older viewers ignore this optional tail.
            stride = ' 4' if (mode == 'bot' and self.config['wallpaper_mode'] == 'all'
                              and self.config['wallpaper_behind_windows'] == 'light'
                              and not view['empty']) else ''
            return f'STATE {mode} {int(bool(self.last_policy and self.last_policy[2]))} {self.config["pause_blur"]} {insets}{stride}'
        if name == 'takeover' and len(parts) == 2:
            with self.sync:
                view = self.views.get(parts[1], {})
                allowed = self.snapshot_ok and not self.locked and view.get('empty') and view.get('awake') and view.get('focused')
            if not allowed or self.saver is not None or not self.config['enabled'] or not self.config['wallpaper_enabled'] or time.monotonic() < self.menu_until:
                return 'ERR desktop is unavailable'
            # A publish can temporarily make the seqlock unreadable. Never
            # turn an unknown Quit baseline into zero and revoke a new lease
            # for a Quit that happened before this takeover.
            baseline = None
            for _ in range(20):
                baseline = read_header(self.runtime / 'frame.bin').get('human_quits')
                if baseline is not None:
                    break
                time.sleep(0.002)
            if baseline is None:
                return 'ERR game state is unavailable'
            self.return_bot()
            self.last_human_quits = baseline
            self.human_output, self.human_workspace = parts[1], view['workspace']
            self.policy()
            return 'OK'
        if name == 'bot':
            if self.human_output:
                self.log('Human control released by desktop viewer or control command')
            self.return_bot()
        elif name in ('screensaver', 'screensaver-preview', 'preview-screensaver'):
            if name == 'screensaver' and (self.locked or self.saver is not None):
                return 'OK'
            if self.locked or not self.snapshot_ok or not self.config['enabled'] or (name == 'screensaver' and not self.config['screensaver_enabled']):
                return 'ERR screensaver unavailable'
            if name == 'screensaver' and self.saver_ownership is not None and not self.saver_ownership.status()['owned']:
                return 'ERR stock screensaver suppression is not owned'
            if name == 'screensaver' and time.monotonic() < self.menu_until and not self.headless:
                # Shell hide may call "menu closed" back into this command loop.
                # Never wait for it here: revoke the lease before starting the saver,
                # so an in-flight renewal cannot dismiss the saver or cancel locking.
                if self.menu_hide_process is None or self.menu_hide_process.poll() is not None:
                    try:
                        self.menu_hide_process = subprocess.Popen(
                            ['/usr/bin/omarchy-shell', 'shell', 'hide', PLUGIN_ID],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            env=session_environment())
                    except OSError as exc:
                        return 'ERR unable to hide menu: ' + str(exc)
            self.menu_until = 0
            if self.saver is None:
                self.return_bot()
                # Map immediately, before a cold engine/checkpoint load. Stock
                # Omarchy idle gives its saver window three seconds to appear.
                self.saver = True if self.headless else self.launch_viewer(saver=True, preview=name != 'screensaver')
                try:
                    self.ensure_engine()
                except Exception:
                    self.stop_saver()
                    raise
                # Native startup can outlive a desktop-state snapshot. The
                # snapshot thread remains active; never map over a new lock.
                if self.locked or not self.snapshot_ok:
                    self.stop_saver()
                    self.reconcile_runtime()
                    self.policy()
                    return 'ERR screensaver unavailable'
        elif name == 'dismiss':
            self.stop_saver()
        elif name == 'watch-sound' and len(parts) == 2 and parts[1] in ('on', 'off', 'toggle'):
            self.config['watch_sound'] = not self.config['watch_sound'] if parts[1] == 'toggle' else parts[1] == 'on'
            self.config['desktop_sound'] = self.config['screensaver_sound'] = self.config['watch_sound']
            atomic_json(self.config_path, self.config)
        elif name in ('pause', 'resume'):
            self.config['enabled'] = name == 'resume'
            if name == 'pause':
                self.return_bot()
                self.stop_saver()
                if self.saver_ownership is not None:
                    self.saver_ownership.release()
            atomic_json(self.config_path, self.config)
        elif name in ('key', 'mouse', 'text'):
            error = self.input_error(line)
            if error:
                return error
            return engine_request(line, self.runtime)
        elif name == 'status':
            return json.dumps({'mode': self.mode, 'locked': self.locked, 'snapshot_ok': self.snapshot_ok,
                               'human_output': self.human_output, 'views': self.views,
                               'config': self.config, 'last_error': self.last_error,
                               'agent': self.agents.settings(),
                               'engine': read_header(self.runtime / 'frame.bin') if self.engine_loaded() else {}})
        elif name == 'save':
            return self.save_checkpoint()
        elif name in ('replan', 'hint-bot'):
            self.policy()
            if self.human_output:
                return 'ERR the bot must have control first'
            if not self.engine_loaded():
                return 'ERR game is not loaded; select Live Background or preview the screensaver'
            return engine_request('replan', self.runtime)
        elif name == 'reload':
            return self.reload_config()
        elif name == 'configure':
            changes = json.loads(line.partition(' ')[2])
            if not isinstance(changes, dict) or any(k not in self.config for k in changes):
                return 'ERR unknown settings'
            echoed_package = 'game_package' in changes
            if echoed_package:
                # Preserve clients that round-trip the full config, while
                # refusing to forge or change an official selection.
                if changes.pop('game_package') != self.config['game_package']:
                    return 'ERR game package is read-only; use use-game'
            # Full-config clients may echo game fields while changing only a
            # preference. Keep the official adapter in that case. Explicit
            # set/chooser edits still enter custom play, even for equal values.
            if echoed_package:
                changes = {key: value for key, value in changes.items()
                           if key not in ('iwad', 'pwads', 'start_map') or value != self.config[key]}
            return self.reload_config(self.config | manual_game_changes(changes))
        elif name == 'wallpaper' and len(parts) == 2 and parts[1] in ('on', 'off'):
            return self.reload_config(self.config | {'wallpaper_enabled': parts[1] == 'on'})
        elif name == 'settings-json':
            return json.dumps(self.settings_json())
        elif name == 'set' and len(parts) >= 3:
            aliases = {'sound.playing': 'playing_sound', 'sound.screensaver': 'screensaver_sound',
                       'sound.idle': 'desktop_sound', 'wallpaper.mode': 'wallpaper_mode',
                       'wallpaper.behind_windows': 'wallpaper_behind_windows',
                       'screensaver.enabled': 'screensaver_enabled', 'blur.percent': 'pause_blur',
                       'mouse.sensitivity': 'mouse_sensitivity', 'mouse.mouselook': 'mouselook',
                       'game.iwad': 'iwad', 'game.pwads': 'pwads', 'game.skill': 'skill',
                       'game.start_map': 'start_map',
                       'agent.selected': 'agent_selected',
                       'agent.screensaver': 'agent_screensaver'}
            key = aliases.get(parts[1])
            if not key:
                return 'ERR unknown or read-only setting'
            value = json.loads(line.split(None, 2)[2])
            answer = self.reload_config(self.config | manual_game_changes({key: value}))
            return json.dumps(self.settings_json()) if answer == 'OK' else answer
        elif name == 'menu' and len(parts) == 2 and parts[1] in ('opened', 'renew', 'closed'):
            now = time.monotonic()
            if parts[1] == 'opened' and (self.locked or not self.snapshot_ok):
                return 'ERR desktop is unavailable'
            if parts[1] == 'renew' and (now >= self.menu_until or self.locked or not self.snapshot_ok):
                return 'ERR lease not held'
            if parts[1] == 'opened' and now >= self.menu_until:
                self.return_bot()
                self.stop_saver()
            self.menu_until = now + 6 if parts[1] != 'closed' else 0
        elif name in ('settings', 'open-menu'):
            menu_args = parts[1:]
            payload = {}
            origins = [arg for arg in menu_args if arg.startswith('--origin=')]
            if name == 'open-menu' and len(origins) == 1 and origins[0] in ('--origin=launcher', '--origin=onboarding'):
                menu_args.remove(origins[0])
                payload['origin'] = origins[0].partition('=')[2]
            if len(menu_args) > 1 or any(arg.startswith('--') for arg in menu_args):
                return 'ERR invalid menu arguments'
            payload['output'] = menu_args[0] if menu_args else next(
                (name for name, view in self.views.items() if view.get('focused')), '')
            if not self.headless and (Path.home() / '.config/omarchy/plugins' / PLUGIN_ID / 'manifest.json').exists():
                try:
                    result = bounded_run(['/usr/bin/omarchy-shell', 'shell', 'summon', PLUGIN_ID,
                                          json.dumps(payload)], timeout=2, max_output_bytes=16384)
                except subprocess.TimeoutExpired:
                    # The summon may still arrive. Starting a second menu would
                    # create competing keyboard grabs and duplicate UI actions.
                    return 'ERR omarchy-shell is not responding'
                except OSError:
                    pass
                else:
                    if result.returncode == 0 and result.stdout.strip() == b'ok':
                        return 'OK'
            if self.settings_process is None or self.settings_process.poll() is not None:
                env = os.environ | {'LIVE_DOOM_RUNTIME': str(self.runtime), 'LIVE_DOOM_DATA': str(self.state)}
                if self.config_path.parent == self.state:
                    env['DOOM_DESKTOP_STATE'] = str(self.state)
                self.settings_process = subprocess.Popen([sys.executable, str(__file__), 'settings-window'], env=env)
        elif name == 'quit':
            self.alive = False
        elif name == 'test-state' and self.headless:
            value = json.loads(line.partition(' ')[2])
            with self.sync:
                self.views = workspace_views(value['monitors'], value['clients'])
                self.locked = bool(value.get('locked', False))
        else:
            return 'ERR unknown command'
        if name in ('screensaver', 'screensaver-preview', 'preview-screensaver', 'dismiss', 'test-state'):
            self.reconcile_runtime()
        self.policy()
        if name in ('watch-sound', 'pause', 'resume'):
            self.record_preferences()
        return 'OK'

    def input_error(self, line, refresh=True):
        if refresh:
            self.policy()
        if not self.human_output or self.saver is not None or self.locked or time.monotonic() < self.menu_until:
            return 'ERR input requires desktop takeover'
        parts = line.split()
        if not parts:
            return 'ERR invalid input command'
        try:
            values = [int(p) for p in parts[1:]]
        except ValueError:
            return 'ERR invalid input'
        if parts[0] == 'key':
            if not 2 <= len(values) <= 4 or not (0 < values[0] <= 511 and values[1] in (0, 1)):
                return 'ERR invalid key'
            if len(values) >= 3 and values[2] != 0 and not 32 <= values[2] <= 126:
                return 'ERR invalid character'
            if len(values) == 4 and values[3] not in (0, 1):
                return 'ERR invalid repeat'
            if values[1] == 0 and any(values[2:]):
                return 'ERR key release cannot type or repeat'
        elif parts[0] == 'mouse':
            if len(values) != 2 or any(abs(v) > 16384 for v in values):
                return 'ERR invalid mouse movement'
        elif parts[0] == 'text':
            if len(values) != 1 or not 32 <= values[0] <= 126:
                return 'ERR invalid character'
        else:
            return 'ERR invalid input command'
        return None

    def input_batch(self, lines, output, generation):
        self.policy()
        if output != self.human_output or generation != self.input_generation:
            return ['ERR input lease expired'] * len(lines)
        errors = [self.input_error(line, refresh=False) for line in lines]
        good = [line for line, error in zip(lines, errors) if error is None]
        try:
            answers = iter(engine_request_batch(good, self.runtime))
        except (OSError, ValueError) as exc:
            self.return_bot()
            self.policy()
            return ['ERR input transport: ' + str(exc)] * len(lines)
        return [error if error else next(answers) for error in errors]

    def settings_json(self):
        self.steam_catalog = read_catalog(self.state)
        cfg = self.config
        name = Path(cfg['iwad']).name.lower()
        titles = {'doom1.wad': 'Doom Shareware', 'doom.wad': 'Doom', 'doom2.wad': 'Doom II',
                  'freedoom1.wad': 'Freedoom Phase 1', 'freedoom2.wad': 'Freedoom Phase 2',
                  'tnt.wad': 'TNT: Evilution', 'plutonia.wad': 'The Plutonia Experiment'}
        metadata = {} if self.headless else live_wallpaper_metadata(Path.home(), ROOT / 'assets/live-doom.webp')
        return {'agent': self.agents.settings(),
                'onboarding': onboarding.settings(self.state, home=Path.home(),
                    downloading=self.onboarding_downloading, config=cfg),
                'runtime': {'loaded': self.engine_loaded()},
                'sound': {'playing': cfg['playing_sound'], 'screensaver': cfg['screensaver_sound'], 'idle': cfg['desktop_sound']},
                'wallpaper': {'mode': cfg['wallpaper_mode'], 'behind_windows': cfg['wallpaper_behind_windows'],
                              'active': cfg['wallpaper_enabled'], 'active_reason': 'background',
                              'theme': metadata.get('name', ''), 'can_remove': metadata.get('can_remove_live', False),
                              'live_choice_present': metadata.get('live_choice_present', False),
                              'remove_reason': metadata.get('remove_reason', '')},
                'screensaver': {'enabled': bool(cfg['enabled'] and cfg['screensaver_enabled']),
                               'claim': self.saver_ownership.status() if self.saver_ownership is not None else None},
                'blur': {'percent': cfg['pause_blur']},
                'mouse': {'mouselook': cfg['mouselook'], 'sensitivity': cfg['mouse_sensitivity'], 'mouselook_supported': True},
                'game': {'iwad': cfg['iwad'], 'iwad_title': titles.get(name, Path(cfg['iwad']).stem),
                         'pwads': cfg['pwads'], 'skill': cfg['skill'], 'start_map': cfg['start_map'],
                         'selected_package': selected_package(cfg, self.steam_catalog) or
                                             onboarding.selected_bundled_game(cfg, self.state),
                         'catalog': public_catalog(self.steam_catalog), 'store_url': STORE_URL},
                'status': {'mode': self.mode, 'map': read_header(self.runtime / 'frame.bin').get('map', '') if self.engine_loaded() else '',
                           'outputs': list(self.views), 'menu_renew_supported': True, 'last_error': self.last_error}}

    def save_checkpoint(self):
        if not self.engine_loaded():
            return 'ERR game is not loaded'
        self.last_save_attempt = time.monotonic()
        answer = engine_request('save', self.runtime)
        if answer == 'OK':
            self.last_save = self.last_save_attempt
            if self.last_error and self.last_error.startswith('Checkpoint failed: '):
                self.last_error = None
        elif answer != 'ERR no live level to save':
            self.last_error = 'Checkpoint failed: ' + answer
            self.log(self.last_error)
        return answer

    def shutdown_engine(self, checkpoint=True):
        if self.engine and self.engine.poll() is None:
            try:
                engine_request('audio 0', self.runtime)
                engine_request('pause 1', self.runtime)
                if checkpoint:
                    self.save_checkpoint()
                engine_request('quit' if checkpoint else 'quit-nosave', self.runtime)
                self.engine.wait(timeout=4)
            except (OSError, subprocess.TimeoutExpired):
                if not checkpoint:
                    self.engine.kill()
                    self.engine.wait(timeout=5)
                    close_process_log(self.engine)
                    self.engine = None
                    return
                self.engine.terminate()
                try:
                    self.engine.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.engine.kill()
                    self.engine.wait()

        close_process_log(self.engine)
        self.engine = None
        self.last_policy = self.last_controls = self.last_render_rate = None
        self.render_size = None
        self.last_unload_attempt = 0.0

    def run(self):
        ensure_private_dir(self.runtime)
        ensure_private_dir(self.state)
        fd = os.open(self.runtime / 'daemon.lock', os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        info = os.fstat(fd)
        if (info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            os.close(fd)
            raise ValueError('Unsafe daemon lock')
        lock = os.fdopen(fd, 'a')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        atomic_json(self.config_path, self.config)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        control = self.runtime / 'control.sock'
        control.unlink(missing_ok=True)
        server.bind(str(control))
        os.chmod(control, 0o600)
        server.listen(16)
        connections = ControlServer(self, server)
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, 'alive', False))
        signal.signal(signal.SIGINT, lambda *_: setattr(self, 'alive', False))
        try:
            try:
                validate_config(self.config)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                if not self.accepted_path.exists():
                    raise
                self.config = config_for(self.accepted_path)
                validate_config(self.config)
                atomic_json(self.config_path, self.config)
                self.last_error = f'Invalid settings restored from the last working configuration: {exc}'
                self.log(self.last_error)
            if self.background is not None:
                selected = self.background.poll()
                if selected is not None:
                    self.config['wallpaper_enabled'] = selected
                    atomic_json(self.config_path, self.config)
            if not self.headless:
                self.snapshot_thread = threading.Thread(target=self.snapshots, daemon=True)
                self.snapshot_thread.start()
            self.reconcile_runtime()
            while self.alive:
                if self.engine is not None and self.engine.poll() is not None:
                    raise RuntimeError(f'Engine exited ({self.engine.returncode}); see engine.log')
                if self.settings_process is not None and self.settings_process.poll() is not None:
                    self.settings_process = None
                if self.menu_hide_process is not None and self.menu_hide_process.poll() is not None:
                    self.menu_hide_process = None
                self.policy()
                now = time.monotonic()
                if self.background is not None and now - self.last_background_poll >= 1:
                    self.last_background_poll = now
                    if self.manage_plugin and not self.plugin_enabled():
                        self.log('Plugin removed or disabled; stopping owned processes')
                        break
                    self.check_saver_ownership()
                    active = self.background.poll()
                    if active is not None and active != self.config['wallpaper_enabled']:
                        self.config['wallpaper_enabled'] = active
                        atomic_json(self.config_path, self.config)
                        self.record_preferences()
                    if self.background.error:
                        self.last_error = self.background.error
                self.reconcile_runtime()
                self.policy()
                with self.sync:
                    dimensions = self.pending_render_size
                if self.engine_loaded() and dimensions and dimensions != self.render_size:
                    answer = engine_request(f'video {dimensions[0]} {dimensions[1]}', self.runtime)
                    if answer == 'OK':
                        self.render_size = dimensions
                    else:
                        self.last_error = 'Unable to resize the live renderer: ' + answer
                        self.log(self.last_error)
                        with self.sync:
                            self.pending_render_size = None
                now = time.monotonic()
                if self.engine_loaded() and now - self.last_save > 30 and now - self.last_save_attempt > 5:
                    self.save_checkpoint()
                connections.poll()
        finally:
            self.alive = False
            if self.snapshot_thread is not None:
                self.snapshot_thread.join(timeout=6.5)
            self.agents.shutdown()
            connections.prepare_shutdown()
            # Give queued shutdown replies and owned children a bounded exit.
            deadline = time.monotonic() + 1.2
            while time.monotonic() < deadline:
                self.agents.tick()
                connections.poll(timeout=0.02, policy=False)
                if self.agents.process is None and not any(client['out'] for client in connections.clients.values()):
                    break
            connections.close()
            self.return_bot()
            self.shutdown_engine()
            if self.saver_ownership is not None:
                try:
                    self.saver_ownership.release()
                except (OSError, ValueError) as exc:
                    # A replaced or inaccessible ownership marker must not
                    # prevent the daemon from stopping its other children.
                    self.log('Unable to release the screensaver claim: ' + str(exc))
            for child in (self.viewer, self.saver, self.settings_process, self.menu_hide_process):
                if child is not None and not isinstance(child, bool) and child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
                if child is not None and not isinstance(child, bool):
                    close_process_log(child)
            server.close()
            control.unlink(missing_ok=True)
            lock.close()


def settings():
    from settings_window import show
    show()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', default='status')
    parser.add_argument('args', nargs='*')
    parser.add_argument('--runtime-dir', type=Path)
    parser.add_argument('--state-dir', type=Path)
    parser.add_argument('--headless', action='store_true')
    parser.add_argument('--accept-terms', action='store_true')
    parser.add_argument('--origin', choices=('launcher', 'onboarding'), action='append')
    parser.add_argument('--background', choices=onboarding.BACKGROUND_CHOICES, action='append')
    parser.add_argument('--screensaver', choices=('on', 'off'), action='append')
    options = parser.parse_args()
    for key in ('origin', 'background', 'screensaver'):
        values = getattr(options, key)
        if values is not None:
            if len(values) != 1:
                parser.error('--' + key + ' may only be passed once')
            setattr(options, key, values[0])
    if options.accept_terms and options.command != 'download-shareware':
        parser.error('--accept-terms is only for download-shareware')
    if options.origin and options.command != 'open-menu':
        parser.error('--origin is only for open-menu')
    if ((options.background is not None or options.screensaver is not None)
            and (options.command != 'onboarding' or options.args[:1] != ['step'])):
        parser.error('--background and --screensaver are only for onboarding step')
    if options.command == 'daemon':
        config = options.state_dir / 'config.json' if options.state_dir is not None else settings_path()
        Controller(options.runtime_dir or runtime_dir(), options.state_dir or state_dir(),
                   options.headless, config_path=config).run()
    elif options.command == 'settings-window':
        settings()
    elif options.command in ('choose-iwad', 'add-wads'):
        return choose_wads(options.command, options.runtime_dir)
    else:
        command = 'settings-json' if options.command == 'settings' else options.command
        if options.accept_terms:
            options.args.append('--accept-terms')
        if options.origin:
            options.args.append('--origin=' + options.origin)
        if options.background is not None:
            options.args.extend(('--background', options.background))
        if options.screensaver is not None:
            options.args.extend(('--screensaver', options.screensaver))
        answer = request(' '.join([command] + options.args), options.runtime_dir,
                         timeout=300 if command in ('download-shareware', 'install-theme') or
                                    (command == 'menu' and options.args == ['renew']) else
                                 130 if command in ('reload', 'set', 'configure', 'wallpaper', 'screensaver',
                                                   'preview-screensaver', 'screensaver-preview', 'dismiss',
                                                   'use-live-wallpaper', 'remove-live-wallpaper',
                                                   'scan-steam', 'use-game') else 2)
        if answer.startswith('ERR'):
            print(answer[:4096], file=sys.stderr)
            return 1
        if options.command in ('status', 'settings', 'set', 'scan-steam', 'use-game', 'onboarding'):
            output = json.dumps(json.loads(answer), indent=2)
            if len(output.encode()) > MAX_REPLY_BYTES:
                raise ValueError('Formatted controller response is too large')
            print(output)
        else:
            print(answer)
        return 1 if answer.startswith('ERR') else 0
    return 0


def choose_wads(command, directory):
    import gi
    gi.require_version('Gtk', '3.0')
    from gi.repository import Gtk
    cfg = json.loads(request('settings-json', directory))
    dialog = Gtk.FileChooserDialog(title='Choose a Doom or Doom II IWAD' if command == 'choose-iwad' else 'Add Doom level or mod WADs',
                                   action=Gtk.FileChooserAction.OPEN)
    dialog.add_buttons('Cancel', Gtk.ResponseType.CANCEL, 'Choose' if command == 'choose-iwad' else 'Add', Gtk.ResponseType.ACCEPT)
    dialog.set_select_multiple(command == 'add-wads')
    wad_filter = Gtk.FileFilter()
    wad_filter.set_name('Doom WAD files')
    for pattern in ('*.wad', '*.WAD'):
        wad_filter.add_pattern(pattern)
    dialog.add_filter(wad_filter)
    if command == 'choose-iwad':
        dialog.set_filename(cfg['game']['iwad'])
    try:
        if dialog.run() == Gtk.ResponseType.ACCEPT:
            paths = [str(validate_wad(path, b'IWAD' if command == 'choose-iwad' else None)) for path in dialog.get_filenames()]
            if paths:
                # Read the latest list after the picker, preserving any changes
                # made elsewhere while this dialog was open.
                cfg = json.loads(request('settings-json', directory))
                value = paths[0] if command == 'choose-iwad' else list(dict.fromkeys(cfg['game']['pwads'] + paths))
                key = 'game.iwad' if command == 'choose-iwad' else 'game.pwads'
                answer = request('set ' + key + ' ' + json.dumps(value), directory, timeout=130)
                if answer.startswith('ERR'):
                    print(answer, file=sys.stderr)
                    return 1
        print(request('settings-json', directory))
        return 0
    finally:
        dialog.destroy()


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        print(f'Live Doom: {exc}', file=sys.stderr)
        sys.exit(1)
