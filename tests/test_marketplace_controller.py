# SPDX-License-Identifier: 0BSD
"""Controller ownership and cold saver lifecycle use private state and mocks."""
from pathlib import Path
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
import socket
import struct
import sys
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import controller
import onboarding
from control_server import ControlServer
from controller import Controller
from project_paths import PLUGIN_ID, atomic_json
from saver_ownership import SaverOwnership
from live_wallpaper import use_live_wallpaper


class MarketplaceControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='live-doom-controller-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.ctrl = Controller(self.home / 'runtime', self.home / 'data', headless=True)
        self.ctrl.saver_ownership = SaverOwnership(self.home, self.home / 'state')

    def test_user_toggle_revocation_persists_disabled_and_stops_saver(self):
        self.ctrl.saver_ownership.claim()
        self.ctrl.saver_ownership.toggle.unlink()
        with patch.object(self.ctrl, 'stop_saver') as stop:
            self.ctrl.check_saver_ownership()
        stop.assert_called_once()
        self.assertFalse(self.ctrl.config['screensaver_enabled'])
        self.assertFalse(json.loads(self.ctrl.config_path.read_text())['screensaver_enabled'])
        self.assertFalse(self.ctrl.saver_ownership.toggle.exists())

    def test_replaced_user_toggle_is_preserved_when_disabled(self):
        self.ctrl.saver_ownership.claim()
        self.ctrl.saver_ownership.toggle.write_bytes(b'personal')
        with patch.object(self.ctrl, 'stop_saver'):
            self.ctrl.check_saver_ownership()
        self.assertEqual(self.ctrl.saver_ownership.toggle.read_bytes(), b'personal')

    def test_pausing_plugin_releases_suppression_and_preserves_saver_preference(self):
        self.ctrl.saver_ownership.claim()
        with patch.object(self.ctrl, 'policy'), patch.object(self.ctrl, 'stop_saver') as stop, \
                patch.object(self.ctrl, 'record_preferences'), patch('controller.onboarding.settings', return_value={}):
            self.assertEqual(self.ctrl.command('pause'), 'OK')
            stop.assert_called_once()
            self.assertFalse(self.ctrl.settings_json()['screensaver']['enabled'])
            self.assertTrue(self.ctrl.config['screensaver_enabled'])
            self.assertIn('disabled', self.ctrl.command('saver-claim'))
            self.assertFalse(self.ctrl.saver_ownership.toggle.exists())
            self.assertEqual(self.ctrl.command('resume'), 'OK')
            self.assertTrue(self.ctrl.settings_json()['screensaver']['enabled'])

    def test_claim_release_failure_does_not_leave_an_owned_viewer_running(self):
        viewer = subprocess.Popen(['/usr/bin/python3', '-I', '-c', 'import time; time.sleep(30)'])
        def cleanup():
            if viewer.poll() is None:
                viewer.kill()
            viewer.wait(timeout=2)
        self.addCleanup(cleanup)
        self.ctrl.viewer = viewer
        self.ctrl.alive = False
        with patch.object(self.ctrl, 'reconcile_runtime'), patch('controller.validate_config'), \
                patch.object(self.ctrl.saver_ownership, 'release', side_effect=ValueError('unsafe marker')):
            self.ctrl.run()
        self.assertIsNotNone(viewer.poll())
        self.assertFalse((self.ctrl.runtime / 'control.sock').exists())

    def test_idle_saver_maps_before_slow_engine_load_and_does_not_relaunch(self):
        self.ctrl.saver_ownership.claim()
        self.ctrl.headless = False
        saver = Mock()
        saver.poll.return_value = None
        def cold_start():
            self.assertIs(self.ctrl.saver, saver)
        with patch.object(self.ctrl, 'launch_viewer', return_value=saver) as launch, \
                patch.object(self.ctrl, 'ensure_engine', side_effect=cold_start), \
                patch.object(self.ctrl, 'reconcile_runtime'), patch.object(self.ctrl, 'policy'):
            self.assertEqual(self.ctrl.command('screensaver'), 'OK')
            self.assertEqual(self.ctrl.command('screensaver'), 'OK')
        launch.assert_called_once_with(saver=True, preview=False)

    def test_failed_cold_start_dismisses_the_waiting_surface(self):
        self.ctrl.saver_ownership.claim()
        self.ctrl.headless = False
        saver = Mock()
        saver.poll.return_value = None
        with patch.object(self.ctrl, 'launch_viewer', return_value=saver), \
                patch.object(self.ctrl, 'ensure_engine', side_effect=RuntimeError('failed startup')):
            with self.assertRaisesRegex(RuntimeError, 'failed startup'):
                self.ctrl.command('screensaver')
        saver.terminate.assert_called_once()
        self.assertIsNone(self.ctrl.saver)

    def test_locked_idle_trigger_is_noop_and_unowned_toggle_refuses_launch(self):
        self.ctrl.locked = True
        with patch.object(self.ctrl, 'launch_viewer') as launch:
            self.assertEqual(self.ctrl.command('screensaver'), 'OK')
            launch.assert_not_called()
        self.ctrl.locked = False
        self.assertIn('not owned', self.ctrl.command('screensaver'))

    def test_remove_on_static_background_keeps_game_unloaded_and_selection_unchanged(self):
        current = self.home / '.local/state/omarchy/current'
        backgrounds = current / 'theme/backgrounds'
        backgrounds.mkdir(parents=True)
        theme = current / 'theme.name'
        theme.write_bytes(b'osaka-jade\n')
        palette = current / 'theme/colors.toml'
        palette.write_bytes(b'keep this palette\n')
        image = backgrounds / '2-static.webp'
        image.write_bytes(b'keep this static image')
        link = current / 'background'
        link.symlink_to(image)
        root = self.home / 'source'
        fallback = root / 'assets/live-doom.webp'
        fallback.parent.mkdir(parents=True)
        fallback.write_bytes(b'private live marker')
        runtime = self.home / 'theme-runtime'
        runtime.mkdir(mode=0o700)
        def select(path):
            replacement = link.with_name('next-background')
            replacement.symlink_to(path)
            replacement.replace(link)
        owned = use_live_wallpaper(self.home, fallback, select, runtime)
        select(image)
        preserved = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                     for path in (theme, palette, image)}
        selection = (link.readlink(), link.lstat().st_mtime_ns)
        self.ctrl.headless = False
        self.ctrl.config['wallpaper_enabled'] = False
        with patch('controller.Path.home', return_value=self.home), \
                patch('controller.ROOT', root), patch('controller.theme_runtime_dir', return_value=runtime), \
                patch('controller.bounded_run', side_effect=AssertionError('background picker')), \
                patch('controller.subprocess.Popen', side_effect=AssertionError('native or viewer startup')), \
                patch('controller.engine_request', side_effect=AssertionError('native command')), \
                patch.object(self.ctrl, 'record_preferences'), patch.object(self.ctrl, 'log') as log:
            self.assertEqual(self.ctrl.command('remove-live-wallpaper'), 'OK')
        self.assertEqual([call.args[0] for call in log.call_args_list],
                         ['remove-live-wallpaper: requested', 'remove-live-wallpaper: completed'])
        self.assertFalse(owned.exists())
        self.assertFalse(self.ctrl.config['wallpaper_enabled'])
        self.assertIsNone(self.ctrl.engine)
        self.assertIsNone(self.ctrl.viewer)
        self.assertEqual((link.readlink(), link.lstat().st_mtime_ns), selection)
        for path, before in preserved.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_live_selection_error_is_logged_as_a_bounded_last_line(self):
        self.ctrl.headless = False
        with patch('controller.remove_live_wallpaper', side_effect=RuntimeError('context\n' + 'x' * 2000)), \
                patch('controller.theme_runtime_dir', return_value=self.home), \
                patch.object(self.ctrl, 'log') as log:
            self.assertEqual(self.ctrl.command('remove-live-wallpaper'), 'ERR ' + 'x' * 1024)
        log.assert_any_call('remove-live-wallpaper: requested')
        log.assert_any_call('remove-live-wallpaper: failed: ' + 'x' * 1024)

    def test_removal_and_disable_detection_follow_the_actual_registry(self):
        plugin = self.home / 'plugin'
        plugin.mkdir()
        (plugin / 'manifest.json').write_text('{}')
        (plugin / 'src').mkdir()
        (plugin / 'src/controller.py').write_text('# fixture')
        config = self.home / '.config/omarchy/shell.json'
        atomic_json(config, {'plugins': [{'id': PLUGIN_ID}]})
        with patch('controller.ROOT', plugin), patch('controller.Path.home', return_value=self.home):
            self.assertTrue(self.ctrl.plugin_enabled())
            atomic_json(config, {'plugins': []})
            self.assertFalse(self.ctrl.plugin_enabled())
            atomic_json(config, {'plugins': [{'id': PLUGIN_ID}], 'disabledPlugins': [PLUGIN_ID]})
            self.assertFalse(self.ctrl.plugin_enabled())
            atomic_json(config, {'plugins': [{'id': PLUGIN_ID}]})
            (plugin / 'manifest.json').unlink()
            self.assertFalse(self.ctrl.plugin_enabled())


class OnboardingCommandTests(unittest.TestCase):
    """Guide navigation is inert; saver ownership requires explicit opt-in."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='live-doom-guide-controller-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.data = self.home / 'data'
        self.runtime = self.home / 'runtime'
        self.data.mkdir(mode=0o700)
        self.runtime.mkdir(mode=0o700)
        self.config = self.home / 'config/config.json'
        self.marker = self.data / onboarding.MARKER
        environment = {'HOME': str(self.home), 'PATH': '/usr/bin:/bin',
                       'XDG_RUNTIME_DIR': str(self.runtime)}
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, environment, clear=True).start()
        patch('controller.Path.home', return_value=self.home).start()
        # One zero-length map lump is enough for container validation. Native
        # startup is forbidden below: these tests exercise setup and policy.
        wad = self.data / 'assets/freedoom-0.13.0/freedoom1.wad'
        wad.parent.mkdir(parents=True)
        wad.write_bytes(struct.pack('<4sii', b'IWAD', 1, 12)
                        + struct.pack('<ii8s', 12, 0, b'MAP01'))
        initialized = onboarding.initialize(self.config, self.data, home=self.home, scan=False)
        self.assertTrue(initialized['fresh'])
        self.ctrl = self.new_controller()
        self.spawn = patch('controller.subprocess.Popen',
                           side_effect=AssertionError('No native, viewer or shell process')).start()
        self.native = patch('controller.engine_request',
                            side_effect=AssertionError('No native command')).start()
        self.picker = patch('controller.bounded_run',
                            side_effect=AssertionError('No background/theme command')).start()

    def new_controller(self):
        ctrl = Controller(self.runtime, self.data, headless=True, config_path=self.config)
        ctrl.saver_ownership = SaverOwnership(self.home, self.home / 'state')
        return ctrl

    def fingerprint(self, path):
        return path.read_bytes(), path.stat().st_mtime_ns

    def assert_no_saver_token(self):
        self.assertFalse(self.ctrl.saver_ownership.toggle.exists())
        self.assertFalse(self.ctrl.saver_ownership.marker.exists())

    def cli(self, args, *, forward=True):
        output, error = io.StringIO(), io.StringIO()
        with patch('controller.sys.argv', ['doomctl', '--runtime-dir', str(self.runtime), *args]), \
                redirect_stdout(output), redirect_stderr(error):
            if forward:
                with patch('controller.request', side_effect=lambda line, *_args, **_kw: self.ctrl.command(line)) as request:
                    try:
                        code = controller.main()
                    except SystemExit as exc:
                        code = exc.code
                calls = request.call_args_list
            else:
                code = controller.main()
                calls = []
        return code, output.getvalue(), error.getvalue(), calls

    def test_cli_progress_persists_inert_choices_and_reopens_at_saved_step(self):
        before = self.fingerprint(self.config)
        code, output, error, calls = self.cli(
            ['onboarding', 'step', 'screensaver', '--screensaver', 'on', '--background', 'phobos'])
        self.assertEqual((code, error), (0, ''))
        self.assertEqual(calls[0].args[0],
                         'onboarding step screensaver --background phobos --screensaver on')
        state = json.loads(output)
        self.assertEqual(state['onboarding']['step'], 'screensaver')
        self.assertEqual(state['onboarding']['choices'], {'background': 'phobos', 'screensaver': True})
        self.assertFalse(state['screensaver']['enabled'])
        self.assertFalse(state['wallpaper']['active'])
        self.assertEqual(self.fingerprint(self.config), before)
        reopened = self.new_controller().settings_json()['onboarding']
        self.assertEqual(reopened['step'], 'screensaver')
        self.assertEqual(reopened['choices'], state['onboarding']['choices'])
        self.assertEqual(json.loads(self.marker.read_text())['schema'], 2)
        self.assert_no_saver_token()
        self.spawn.assert_not_called()
        self.picker.assert_not_called()

    def test_partial_progress_preserves_other_choices_and_supports_back_navigation(self):
        before = self.fingerprint(self.config)
        for command in ('onboarding step game --background skip',
                        'onboarding step summary --screensaver on',
                        'onboarding step background'):
            state = json.loads(self.ctrl.command(command))
        self.assertEqual(state['onboarding']['step'], 'background')
        self.assertEqual(state['onboarding']['choices'], {'background': 'skip', 'screensaver': True})
        self.assertEqual(self.fingerprint(self.config), before)
        self.assert_no_saver_token()

    def test_protocol_rejects_unknown_duplicate_and_unpaired_choices_without_mutation(self):
        before = self.fingerprint(self.config), self.fingerprint(self.marker)
        invalid = ('onboarding', 'onboarding step', 'onboarding step unknown',
                   'onboarding step game --background',
                   'onboarding step game --unknown current',
                   'onboarding step game --background other',
                   'onboarding step game --screensaver yes',
                   'onboarding step game --screensaver on --screensaver off',
                   'onboarding step game --background current --background phobos',
                   'onboarding finish --screensaver on', 'onboarding skip extra')
        for command in invalid:
            with self.subTest(command=command):
                self.assertTrue(self.ctrl.command(command).startswith('ERR '))
                self.assertEqual((self.fingerprint(self.config), self.fingerprint(self.marker)), before)
                self.assert_no_saver_token()

    def test_cli_rejects_duplicate_invalid_or_misplaced_flags_before_request(self):
        before = self.fingerprint(self.config), self.fingerprint(self.marker)
        invalid = (
            ['onboarding', 'step', 'game', '--background', 'current', '--background', 'phobos'],
            ['onboarding', 'step', 'game', '--screensaver', 'on', '--screensaver', 'on'],
            ['onboarding', 'step', 'game', '--background', 'other'],
            ['onboarding', 'step', 'game', '--screensaver', 'yes'],
            ['onboarding', 'step', 'game', '--background'],
            ['onboarding', 'step', 'game', '--unknown', 'current'],
            ['onboarding', 'finish', '--background', 'skip'],
            ['onboarding', 'skip', '--screensaver', 'off'],
            ['status', '--background', 'current'],
        )
        for args in invalid:
            with self.subTest(args=args):
                code, _, error, calls = self.cli(args)
                self.assertEqual(code, 2)
                self.assertTrue(error)
                self.assertEqual(calls, [])
                self.assertEqual((self.fingerprint(self.config), self.fingerprint(self.marker)), before)
                self.assert_no_saver_token()

    def test_cli_returns_backend_rejection_for_unknown_step_without_mutation(self):
        before = self.fingerprint(self.config), self.fingerprint(self.marker)
        code, output, error, calls = self.cli(['onboarding', 'step', 'unknown'])
        self.assertEqual((code, output), (1, ''))
        self.assertIn('ERR Unknown onboarding step', error)
        self.assertEqual(len(calls), 1)
        self.assertEqual((self.fingerprint(self.config), self.fingerprint(self.marker)), before)

    def test_finish_skip_and_dismiss_close_guide_without_applying_choices(self):
        before = self.fingerprint(self.config)
        for verb, outcome in (('finish', 'finished'), ('skip', 'skipped'), ('dismiss', 'finished')):
            with self.subTest(verb=verb):
                atomic_json(self.marker, onboarding.new_marker(fresh=True))
                self.ctrl.command('onboarding step screensaver --background phobos --screensaver on')
                code, output, error, _ = self.cli(['onboarding', verb])
                self.assertEqual((code, error), (0, ''))
                state = json.loads(output)
                self.assertFalse(state['onboarding']['show'])
                self.assertEqual(state['onboarding']['outcome'], outcome)
                self.assertEqual(state['onboarding']['choices'], {'background': 'phobos', 'screensaver': True})
                self.assertFalse(state['screensaver']['enabled'])
                self.assertFalse(state['wallpaper']['active'])
                marker = self.fingerprint(self.marker)
                self.assertFalse(json.loads(self.ctrl.command('onboarding finish'))['onboarding']['show'])
                self.assertTrue(self.ctrl.command('onboarding step background').startswith('ERR '))
                self.assertEqual(self.fingerprint(self.marker), marker)
                self.assertEqual(self.fingerprint(self.config), before)
                self.assert_no_saver_token()

    def test_schema_one_settings_read_is_pure_and_progress_upgrades_marker(self):
        legacy = {'schema': 1, 'show': True, 'fresh': False, 'selected_package': None}
        atomic_json(self.marker, legacy)
        before = self.fingerprint(self.marker)
        state = json.loads(self.ctrl.command('settings-json'))['onboarding']
        self.assertEqual(state['step'], 'background')
        self.assertEqual(state['choices'], {'background': 'current', 'screensaver': False})
        self.assertEqual(self.fingerprint(self.marker), before)
        state = json.loads(self.ctrl.command('onboarding step summary --background skip'))['onboarding']
        self.assertEqual((state['step'], state['choices']['background']), ('summary', 'skip'))
        self.assertEqual(json.loads(self.marker.read_text())['schema'], 2)
        self.assertEqual(self.new_controller().settings_json()['onboarding']['choices'], state['choices'])

    def test_legacy_guide_inherits_effective_saver_and_matched_finish_does_not_write_settings(self):
        for enabled, saver_enabled in ((True, True), (True, False), (False, True), (False, False)):
            with self.subTest(enabled=enabled, screensaver_enabled=saver_enabled):
                atomic_json(self.config, self.ctrl.config | {
                    'enabled': enabled, 'screensaver_enabled': saver_enabled})
                self.ctrl = self.new_controller()
                atomic_json(self.marker, {'schema': 1, 'show': True, 'fresh': False,
                                          'selected_package': None})
                config_before, marker_before = self.fingerprint(self.config), self.fingerprint(self.marker)
                settings = self.ctrl.settings_json()
                expected = enabled and saver_enabled
                self.assertIs(settings['onboarding']['choices']['screensaver'], expected)
                self.assertIs(settings['screensaver']['enabled'], expected)
                self.assertEqual(self.fingerprint(self.marker), marker_before)
                summary = json.loads(self.ctrl.command('onboarding step summary'))
                self.assertIs(summary['onboarding']['choices']['screensaver'], expected)
                self.assertEqual(json.loads(self.marker.read_text())['schema'], 2)
                # Summary Finish only sends a setting when the guide choice
                # differs from the effective setting. Matching legacy values
                # must complete without accidentally disabling an opted-in saver.
                with patch.object(self.ctrl, 'reload_config',
                                  side_effect=AssertionError('Matched Finish must not apply settings')) as reload:
                    if summary['onboarding']['choices']['screensaver'] != summary['screensaver']['enabled']:
                        self.ctrl.command('set screensaver.enabled '
                                          + json.dumps(summary['onboarding']['choices']['screensaver']))
                    finished = json.loads(self.ctrl.command('onboarding finish'))
                    reload.assert_not_called()
                self.assertEqual(finished['onboarding']['outcome'], 'finished')
                self.assertFalse(finished['onboarding']['show'])
                self.assertEqual(self.fingerprint(self.config), config_before)
                self.assertEqual(self.ctrl.config['screensaver_enabled'], saver_enabled)
                self.assert_no_saver_token()

    def test_explicit_schema_two_saver_choice_wins_over_opposite_configuration(self):
        for choice in (False, True):
            with self.subTest(choice=choice):
                atomic_json(self.config, self.ctrl.config | {
                    'enabled': True, 'screensaver_enabled': not choice})
                self.ctrl = self.new_controller()
                marker = onboarding.new_marker(fresh=False)
                marker['choices']['screensaver'] = choice
                atomic_json(self.marker, marker)
                config_before, marker_before = self.fingerprint(self.config), self.fingerprint(self.marker)
                settings = self.ctrl.settings_json()
                self.assertIs(settings['onboarding']['choices']['screensaver'], choice)
                self.assertIs(settings['screensaver']['enabled'], not choice)
                self.assertEqual(self.fingerprint(self.marker), marker_before)
                summary = json.loads(self.ctrl.command('onboarding step summary'))
                self.assertIs(summary['onboarding']['choices']['screensaver'], choice)
                finished = json.loads(self.ctrl.command('onboarding finish'))
                self.assertIs(finished['onboarding']['choices']['screensaver'], choice)
                self.assertIs(finished['screensaver']['enabled'], not choice)
                self.assertEqual(self.fingerprint(self.config), config_before)
                self.assert_no_saver_token()

    def test_missing_non_fresh_guide_inherits_saver_when_progress_is_created(self):
        atomic_json(self.config, self.ctrl.config | {'enabled': True, 'screensaver_enabled': True})
        self.ctrl = self.new_controller()
        self.marker.unlink()
        before = self.fingerprint(self.config)
        summary = json.loads(self.ctrl.command('onboarding step summary'))
        self.assertIs(summary['onboarding']['choices']['screensaver'], True)
        self.assertIs(json.loads(self.marker.read_text())['choices']['screensaver'], True)
        self.assertEqual(self.fingerprint(self.config), before)
        self.assert_no_saver_token()

    def test_fresh_saver_stays_unclaimed_until_explicit_enable(self):
        self.assertFalse(self.ctrl.config['screensaver_enabled'])
        self.assertEqual(self.ctrl.command('saver-claim'), 'ERR Doom screensaver is disabled')
        self.assert_no_saver_token()
        self.ctrl.command('onboarding step summary --screensaver on')
        self.ctrl.command('onboarding finish')
        self.assertEqual(self.ctrl.command('saver-claim'), 'ERR Doom screensaver is disabled')
        self.assert_no_saver_token()
        settings = json.loads(self.ctrl.command('set screensaver.enabled true'))
        self.assertTrue(settings['screensaver']['enabled'])
        self.assertTrue(json.loads(self.config.read_text())['screensaver_enabled'])
        claimed = json.loads(self.ctrl.command('saver-claim'))
        self.assertTrue(claimed['owned'])
        self.assertEqual(len(claimed['token']), 64)
        self.assertEqual(self.ctrl.saver_ownership.toggle.read_bytes(), self.ctrl.saver_ownership.marker.read_bytes())
        self.assertFalse(settings['runtime']['loaded'])
        self.spawn.assert_not_called()

    def test_disabled_preview_maps_without_enabling_or_claiming_saver(self):
        before = self.fingerprint(self.config)
        self.ctrl.headless = False
        saver = Mock()
        saver.poll.return_value = None
        with patch.object(self.ctrl, 'launch_viewer', return_value=saver) as launch, \
                patch.object(self.ctrl, 'ensure_engine') as engine, \
                patch.object(self.ctrl, 'reconcile_runtime'), patch.object(self.ctrl, 'policy'), \
                patch.object(self.ctrl, 'return_bot'), \
                patch.object(self.ctrl.saver_ownership, 'claim',
                             side_effect=AssertionError('Preview must not suppress the stock saver')):
            self.assertEqual(self.ctrl.command('screensaver'), 'ERR screensaver unavailable')
            launch.assert_not_called()
            for alias in ('preview-screensaver', 'screensaver-preview'):
                self.ctrl.saver = None
                self.assertEqual(self.ctrl.command(alias), 'OK')
                self.assertIs(self.ctrl.saver, saver)
            self.assertEqual(launch.call_count, 2)
            self.assertEqual(engine.call_count, 2)
            launch.assert_called_with(saver=True, preview=True)
        self.assertFalse(self.ctrl.config['screensaver_enabled'])
        self.assertEqual(self.fingerprint(self.config), before)
        self.assert_no_saver_token()

    def test_preview_flag_is_not_applied_to_real_idle_saver(self):
        with patch('controller.spawn_logged', return_value=Mock()) as spawn:
            self.ctrl.launch_viewer(saver=True)
            idle_args = spawn.call_args.args[0]
            self.assertIn('--screensaver-layer', idle_args)
            self.assertNotIn('--preview', idle_args)
            self.ctrl.launch_viewer(saver=True, preview=True)
            self.assertIn('--preview', spawn.call_args.args[0])
            with self.assertRaisesRegex(ValueError, 'requires'):
                self.ctrl.launch_viewer(preview=True)
            self.assertEqual(spawn.call_count, 2)

    def test_public_cli_and_real_private_transport_round_trip_guide_settings(self):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(str(self.runtime / 'control.sock'))
        listener.listen(8)
        server = ControlServer(self.ctrl, listener)
        done = threading.Event()
        errors = []
        def serve():
            try:
                while not done.is_set():
                    server.poll(timeout=0.02, policy=False)
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        before = self.fingerprint(self.config)
        try:
            code, output, error, _ = self.cli(
                ['onboarding', 'step', 'summary', '--background', 'current', '--screensaver', 'off'],
                forward=False)
            self.assertEqual((code, error), (0, ''))
            self.assertEqual(json.loads(output)['onboarding']['step'], 'summary')
            code, output, error, _ = self.cli(['onboarding', 'skip'], forward=False)
            self.assertEqual((code, error), (0, ''))
            self.assertEqual(json.loads(output)['onboarding']['outcome'], 'skipped')
            self.assertEqual(self.fingerprint(self.config), before)
            self.assert_no_saver_token()
        finally:
            done.set()
            thread.join(timeout=2)
            server.close()
            listener.close()
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
