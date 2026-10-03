# SPDX-License-Identifier: 0BSD
"""Real Freedoom lifecycle with private sockets/state and SDL dummy drivers.

Run after the explicit XDG build. No compositor, session IPC, production
configuration, network, retail game data or input devices are used.
"""
from pathlib import Path
import os
import re
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from controller import Controller, engine_request, engine_request_batch, read_header
from project_paths import ProjectPaths, atomic_json

PATHS = ProjectPaths.from_env()


@unittest.skipUnless(PATHS.engine.is_file() and PATHS.default_iwad.is_file(),
                     'Run the explicit bootstrap and native build first')
class NativeLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='live-doom-native-')
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.runtime, self.state = base / 'runtime', base / 'state'
        self.runtime.mkdir(mode=0o700)
        self.state.mkdir(mode=0o700)
        atomic_json(self.state / 'config.json', {'iwad': str(PATHS.default_iwad),
                    'wallpaper_enabled': True,
                    'render_width': 320, 'render_height': 200})
        self.ctrl = Controller(self.runtime, self.state, headless=True)
        self.ctrl.locked = True
        self.addCleanup(self.ctrl.shutdown_engine, checkpoint=False)
        self.ctrl.start_engine()
        self.ctrl.policy()
        self.console_sequence = 0

    def wait(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            value = read_header(self.runtime / 'frame.bin')
            if predicate(value):
                return value
            time.sleep(0.01)
        self.fail('Native frame condition timed out')

    def native_preferences(self, *commands):
        """Use the real console cvars also used by native Sound sliders."""
        self.assertEqual(engine_request_batch(('owner 2 1', 'pause 0', 'audio 1'), self.runtime),
                         ['OK'] * 3)
        self.wait(lambda h: h.get('owner') == 2 and h.get('paused') == 0)
        self.assertEqual(read_header(self.runtime / 'frame.bin')['render_fps'], 35)
        self.assertEqual(engine_request_batch(('key 96 1', 'key 96 0'), self.runtime), ['OK'] * 2)
        # Console animation must start before the first text event is accepted.
        time.sleep(0.12)
        self.console_sequence += 1
        log = self.state / f'console-{self.console_sequence}.txt'

        def type_command(command):
            events = [f'text {ord(character)}' for character in command]
            events += ['key 13 1', 'key 13 0']
            self.assertEqual(engine_request_batch(events, self.runtime), ['OK'] * len(events))

        type_command(f'openlog {log}')
        self.wait(lambda h: log.exists())
        for command in commands:
            type_command(command)
        type_command('echo PREFS %sfx_volume %music_volume %hu_messages %snd_channels')
        pattern = re.compile(r'^PREFS (\d+) (\d+) (\d+) (\d+)\s*$', re.MULTILINE)
        self.wait(lambda h: pattern.search(log.read_text()))
        result = tuple(map(int, pattern.search(log.read_text()).groups()))
        type_command('closelog')
        self.assertEqual(engine_request_batch(('key 96 1', 'key 96 0'), self.runtime), ['OK'] * 2)
        return result

    def native_config(self):
        args = self.ctrl.engine.args
        return Path(args[args.index('-config') + 1])

    def saved_preferences(self, config):
        values = {}
        for line in config.read_text().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] in ('sfx_volume', 'music_volume', 'show_messages'):
                values[parts[0]] = int(parts[1])
        return values

    def test_paused_freedoom_has_zero_tics_muted_sound_and_restores_checkpoint(self):
        before = self.wait(lambda h: h.get('paused') == 1 and h.get('map') == 'E1M1')
        self.assertEqual(before['tic'], 0)
        self.assertEqual(before['audible'], 0)
        fields = ('map', 'gamestate', 'leveltime', 'health', 'x', 'y', 'kills', 'items', 'secrets', 'angle')
        old_pid = self.ctrl.engine.pid
        self.assertTrue(self.ctrl.unload_engine())
        self.assertIsNone(self.ctrl.engine)
        self.assertFalse((self.runtime / 'frame.bin').exists())
        self.assertEqual(len(list((self.state / 'games').glob('*/saves/etersav7.dsg'))), 1)
        self.ctrl.ensure_engine()
        self.ctrl.policy()
        after = self.wait(lambda h: h.get('paused') == 1 and h.get('pid') == self.ctrl.engine.pid)
        self.assertNotEqual(self.ctrl.engine.pid, old_pid)
        self.assertEqual(tuple(before[k] for k in fields), tuple(after[k] for k in fields))

    def test_shareware_defaults_are_not_needed_for_controls_and_driver_seam(self):
        self.assertEqual(engine_request('controls 1.2 1', self.runtime), 'OK')
        self.assertEqual(engine_request('owner 1 7', self.runtime), 'OK')
        self.assertEqual(engine_request('action 7 0 0 0 0 0 0 0 100', self.runtime), 'OK')
        self.assertEqual(engine_request('owner 0 0', self.runtime), 'OK')
        self.assertEqual(self.ctrl.save_checkpoint(), 'OK')
        self.assertTrue(self.ctrl.engine_loaded())

    def test_fresh_free_default_bot_turns_and_explores(self):
        self.assertEqual(PATHS.default_iwad.name, 'freedoom1.wad')
        before = self.wait(lambda h: h.get('paused') == 1 and h.get('map') == 'E1M1')
        self.assertEqual(engine_request_batch(('owner 0 0', 'pause 0'), self.runtime),
                         ['OK', 'OK'])
        turning = self.wait(lambda h: h.get('angle') != before['angle'])
        self.assertEqual(turning['owner'], 0)
        self.assertGreater(turning['leveltime'], before['leveltime'])
        self.wait(lambda h: (h.get('x'), h.get('y')) != (before['x'], before['y']))

    def test_native_sound_preferences_survive_gate_pause_unload_and_restart(self):
        expected = (0, 7, 0, 16)
        self.assertEqual(self.native_preferences('sfx_volume 0', 'music_volume 7',
                                                'hu_messages 0', 'snd_channels 16'), expected)
        config = self.native_config()
        old_pid = self.ctrl.engine.pid
        for command in ('audio 0', 'pause 1', 'audio 1', 'pause 0'):
            self.assertEqual(engine_request(command, self.runtime), 'OK')
        self.wait(lambda h: h.get('paused') == 0 and h.get('audible') == 1)
        self.assertEqual(self.native_preferences(), expected)
        self.assertTrue(self.ctrl.unload_engine())
        self.assertEqual(self.saved_preferences(config),
                         {'sfx_volume': 0, 'music_volume': 7, 'show_messages': 0})
        system = config.parent / 'user/system.cfg'
        self.assertRegex(system.read_text(), r'(?m)^snd_channels\s+16\s*$')
        self.ctrl.ensure_engine()
        self.assertNotEqual(self.ctrl.engine.pid, old_pid)
        self.assertEqual(self.native_config(), config)
        self.assertEqual(self.native_preferences(), expected)

    @unittest.skipUnless((PATHS.freedoom_root / 'freedoom2.wad').is_file(),
                         'Install the bundled Freedoom Phase 2 IWAD first')
    def test_native_preferences_are_isolated_between_games_and_restored_on_return(self):
        second_game = str(PATHS.freedoom_root / 'freedoom2.wad')
        first_game = self.ctrl.config['iwad']
        expected_first = (0, 7, 0, 16)
        expected_second = (5, 0, 1, 24)
        self.assertEqual(self.native_preferences('sfx_volume 0', 'music_volume 7',
                                                'hu_messages 0', 'snd_channels 16'), expected_first)
        first_config = self.native_config()
        self.assertEqual(self.ctrl.reload_config(self.ctrl.config | {'iwad': second_game}), 'OK')
        second_config = self.native_config()
        self.assertNotEqual(second_config, first_config)
        self.assertEqual(self.saved_preferences(first_config),
                         {'sfx_volume': 0, 'music_volume': 7, 'show_messages': 0})
        self.assertEqual(self.native_preferences(), (8, 8, 1, 32))
        self.assertEqual(self.native_preferences('sfx_volume 5', 'music_volume 0',
                                                'hu_messages 1', 'snd_channels 24'), expected_second)
        self.assertEqual(self.ctrl.reload_config(self.ctrl.config | {'iwad': first_game}), 'OK')
        self.assertEqual(self.native_config(), first_config)
        self.assertEqual(self.saved_preferences(second_config),
                         {'sfx_volume': 5, 'music_volume': 0, 'show_messages': 1})
        self.assertEqual(self.native_preferences(), expected_first)
        self.assertEqual(self.ctrl.reload_config(self.ctrl.config | {'iwad': second_game}), 'OK')
        self.assertEqual(self.native_config(), second_config)
        self.assertEqual(self.native_preferences(), expected_second)

    def test_widescreen_resize_preserves_pid_and_exact_paused_world(self):
        before = self.wait(lambda h: h.get('paused') == 1)
        fields = ('tic', 'map', 'leveltime', 'health', 'x', 'y', 'kills', 'items', 'secrets', 'angle')
        for width, height in ((3440, 1440), (640, 480)):
            result = self.ctrl.reload_config(self.ctrl.config | {'render_width': width, 'render_height': height})
            self.assertEqual(result, 'OK')
            after = self.wait(lambda h: (h.get('width'), h.get('height')) == (width, height))
            self.assertEqual(after['pid'], before['pid'])
            self.assertEqual(tuple(after[k] for k in fields), tuple(before[k] for k in fields))
            self.assertEqual(after['hud_layout'], 4)
            self.assertEqual((after['pixel_aspect_num'], after['pixel_aspect_den']), (1, 1))

    def test_native_menu_suppresses_images_without_suppressing_world_telemetry(self):
        self.assertEqual(engine_request_batch(('owner 2 1', 'pause 0', 'audio 1',
                                              'key 27 1', 'key 27 0'), self.runtime), ['OK'] * 5)
        opened = self.wait(lambda h: h.get('owner') == 2 and h.get('tic', 0) >= 5)
        time.sleep(1.2)
        settled = read_header(self.runtime / 'frame.bin')
        self.assertEqual(settled['render_fps'], 35)
        self.assertEqual(settled['leveltime'], opened['leveltime'])
        self.assertGreaterEqual(settled['tic'] - opened['tic'], 30)
        self.assertGreater(settled['seq'], opened['seq'])
        image_changes = settled['frame'] - opened['frame']
        self.assertGreater(image_changes, 0, 'Menu skull animation must still publish images')
        self.assertLess(image_changes, (settled['tic'] - opened['tic']) // 2)
        self.assertEqual(engine_request('pause 1', self.runtime), 'OK')
        paused = self.wait(lambda h: h.get('paused') == 1)
        # Paused telemetry has a one-second heartbeat rather than render ticks.
        after_pause = self.wait(lambda h: h.get('seq', 0) > paused['seq'])
        self.assertEqual(after_pause['frame'], paused['frame'])
        self.assertEqual(after_pause['tic'], paused['tic'])
        self.assertGreater(after_pause['seq'], paused['seq'])


    def endgame_human(self):
        self.assertEqual(engine_request_batch(('owner 2 1', 'pause 0'), self.runtime),
                         ['OK', 'OK'])
        return self.wait(lambda h: h.get('owner') == 2 and not h.get('paused'))

    def endgame_console(self, command, trailing=()):
        self.assertEqual(engine_request_batch(('key 96 1', 'key 96 0'), self.runtime),
                         ['OK', 'OK'])
        time.sleep(0.12)  # real console animation before its first text event
        commands = tuple(f'text {ord(character)}' for character in command)
        commands += ('key 13 1', 'key 13 0') + tuple(trailing)
        return engine_request_batch(commands, self.runtime)

    def endgame_world(self, header):
        fields = ('map', 'gamestate', 'leveltime', 'health', 'x', 'y', 'angle',
                  'kills', 'items', 'secrets', 'armor', 'ammo', 'keys', 'weapons_owned')
        return tuple(header[key] for key in fields)

    def test_f7_end_game_can_cancel_then_confirm_without_losing_world(self):
        human = self.endgame_human()
        quits = human['human_quits']
        self.assertEqual(engine_request_batch(('key 193 1', 'key 193 0'), self.runtime),
                         ['OK', 'OK'])  # native KEYD_F7
        time.sleep(0.10)
        question = read_header(self.runtime / 'frame.bin')
        time.sleep(0.10)
        self.assertEqual(read_header(self.runtime / 'frame.bin')['leveltime'], question['leveltime'])
        self.assertEqual(question['human_quits'], quits)
        self.assertEqual(engine_request_batch(('key 27 1', 'key 27 0'), self.runtime), ['OK', 'OK'])
        continued = self.wait(lambda h: h.get('leveltime', -1) > question['leveltime'])
        self.assertEqual(continued['owner'], 2)
        self.assertEqual(continued['human_quits'], quits)
        self.assertEqual(engine_request_batch(('key 193 1', 'key 193 0'), self.runtime), ['OK', 'OK'])
        time.sleep(0.08)
        before = read_header(self.runtime / 'frame.bin')
        self.assertEqual(engine_request_batch(('key 13 1', 'pause 1'), self.runtime), ['OK', 'OK'])
        after = self.wait(lambda h: h.get('owner') == 0 and h.get('paused') == 1)
        self.assertEqual(self.endgame_world(after), self.endgame_world(before))
        self.assertEqual(after['human_quits'], quits + 1)
        self.endgame_human()
        self.wait(lambda h: h.get('leveltime', -1) > after['leveltime'])

    def assert_console_endgame_question(self, command):
        human = self.endgame_human()
        self.assertTrue(all(answer == 'OK' for answer in self.endgame_console(command)))
        time.sleep(0.10)
        before = read_header(self.runtime / 'frame.bin')
        self.assertEqual(before['owner'], 2, 'End Game must wait for the Doom confirmation')
        self.assertEqual(before['human_quits'], human['human_quits'])
        self.assertEqual(engine_request_batch(('key 13 1', 'pause 1'), self.runtime), ['OK', 'OK'])
        after = self.wait(lambda h: h.get('owner') == 0 and h.get('paused') == 1)
        self.assertEqual(self.endgame_world(after), self.endgame_world(before))
        self.assertEqual(after['human_quits'], human['human_quits'] + 1)

    def test_native_menu_end_game_command_confirms_and_hands_back(self):
        self.assert_console_endgame_question('mn_endgame')

    def test_console_endgame_confirms_and_hands_back(self):
        self.assert_console_endgame_question('endgame')

    def test_direct_starttitle_hands_back_without_entering_attract_loop(self):
        human = self.endgame_human()
        answers = self.endgame_console('starttitle', ('pause 1',))
        self.assertEqual(answers[-1], 'OK')
        after = self.wait(lambda h: h.get('owner') == 0 and h.get('paused') == 1)
        self.assertEqual(after['human_quits'], human['human_quits'] + 1)
        self.assertEqual((after['map'], after['gamestate']), ('E1M1', 0))

    def endgame_save_path(self):
        args = self.ctrl.engine.args
        return Path(args[args.index('-save') + 1]) / 'etersav7.dsg'

    def test_demo_recovers_checkpoint_and_cannot_save_pending_replay(self):
        self.assertEqual(engine_request('save', self.runtime), 'OK')
        checkpoint = self.endgame_save_path()
        saved = checkpoint.read_bytes()
        self.endgame_human()
        answers = self.endgame_console('playdemo DEMO1', ('save', 'pause 1'))
        self.assertEqual(answers[-2:], ['ERR no live level to save', 'OK'])
        self.assertEqual(engine_request('pause 0', self.runtime), 'OK')
        log = self.ctrl.log_root / 'engine.log'
        self.wait(lambda h: log.exists() and 'Live Doom recovered checkpoint' in log.read_text())
        self.assertEqual(engine_request('pause 1', self.runtime), 'OK')
        restored = self.wait(lambda h: h.get('paused') == 1)
        self.assertEqual((restored['map'], restored['gamestate'], restored['owner']), ('E1M1', 0, 2))
        self.assertEqual(checkpoint.read_bytes(), saved)
        time.sleep(0.15)
        self.assertEqual(log.read_text().count('Live Doom recovered'), 1)

    def assert_demo_campaign_fallback(self, invalid_checkpoint=False):
        checkpoint = self.endgame_save_path()
        if invalid_checkpoint:
            self.assertEqual(engine_request('save', self.runtime), 'OK')
            data = bytearray(checkpoint.read_bytes())
            data[24:40] = b'UNSUPPORTED-SAVE\0'  # safe native-version rejection
            checkpoint.write_bytes(data)
            rejected = checkpoint.read_bytes()
        else:
            self.assertFalse(checkpoint.exists())
        self.assertEqual(engine_request('campaign E1M2', self.runtime), 'OK')
        self.endgame_human()
        self.assertTrue(all(answer == 'OK' for answer in self.endgame_console('playdemo DEMO1')))
        log = self.ctrl.log_root / 'engine.log'
        self.wait(lambda h: log.exists() and 'Live Doom recovered campaign start' in log.read_text())
        restored = self.wait(lambda h: h.get('map') == 'E1M2' and h.get('leveltime', 0) > 1)
        self.assertEqual((restored['gamestate'], restored['owner']), (0, 2))
        time.sleep(0.15)
        self.assertEqual(log.read_text().count('Live Doom recovered'), 1)
        if invalid_checkpoint:
            self.assertEqual(checkpoint.read_bytes(), rejected)
        else:
            self.assertFalse(checkpoint.exists())

    def test_demo_without_checkpoint_recovers_validated_campaign(self):
        self.assert_demo_campaign_fallback()

    def test_demo_rejected_checkpoint_falls_back_once(self):
        self.assert_demo_campaign_fallback(invalid_checkpoint=True)

if __name__ == '__main__':
    unittest.main()
