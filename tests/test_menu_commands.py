#!/usr/bin/env python3
# SPDX-License-Identifier: 0BSD
"""Menu IPC failures and lease revocation must preserve the idle saver."""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from controller import Controller


class MenuCommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        base = Path(self.directory.name)
        self.ctrl = Controller(base / 'runtime', base / 'state', headless=True)
        self.ctrl.headless = False
        self.home = base / 'home'
        manifest = self.home / '.config/omarchy/plugins/io.github.slogonomo.live-doom/manifest.json'
        manifest.parent.mkdir(parents=True)
        manifest.write_text('{}')
        self.addCleanup(patch.stopall)
        patch('controller.Path.home', return_value=self.home).start()
        self.native = patch('controller.engine_request', return_value='OK').start()
        self.spawn = patch('controller.subprocess.Popen').start()
        self.spawn.return_value.poll.return_value = None
        self.summon = patch('controller.bounded_run').start()

    def test_rejected_summon_opens_one_fallback_and_success_does_not(self):
        self.summon.return_value = subprocess.CompletedProcess([], 0, b'unknown\n', b'')
        self.assertEqual(self.ctrl.command('open-menu TEST'), 'OK')
        self.assertEqual(self.ctrl.command('open-menu TEST'), 'OK')
        self.spawn.assert_called_once()
        self.assertIn('settings-window', self.spawn.call_args.args[0])
        self.assertEqual(self.summon.call_args.args[0][-1], '{"output": "TEST"}')
        self.spawn.reset_mock()
        self.ctrl.settings_process = None
        self.summon.return_value = subprocess.CompletedProcess([], 0, b'ok\n', b'')
        self.assertEqual(self.ctrl.command('open-menu TEST'), 'OK')
        self.spawn.assert_not_called()

    def test_timed_out_summon_returns_error_without_duplicate_menu(self):
        self.summon.side_effect = subprocess.TimeoutExpired('omarchy-shell', 2)
        self.assertEqual(self.ctrl.command('open-menu TEST'), 'ERR omarchy-shell is not responding')
        self.spawn.assert_not_called()
        self.assertTrue(self.ctrl.alive)
        self.assertIn('"engine"', self.ctrl.command('status'))

    def test_missing_shell_falls_back(self):
        self.summon.side_effect = FileNotFoundError('missing shell')
        self.assertEqual(self.ctrl.command('open-menu TEST'), 'OK')
        self.spawn.assert_called_once()

    def test_only_launcher_origin_summons_request_toast_cleanup(self):
        self.summon.return_value = subprocess.CompletedProcess([], 0, b'ok\n', b'')
        for command, output, origin in (
            ('open-menu --origin=launcher', 'TEST', 'launcher'),
            ('open-menu --origin=onboarding', 'TEST', 'onboarding'),
            ('open-menu DP-2 --origin=launcher', 'DP-2', 'launcher'),
            ('open-menu DP-2', 'DP-2', None),
            ('settings DP-2', 'DP-2', None),
            ('open-menu', 'TEST', None),
        ):
            with self.subTest(command=command):
                self.assertEqual(self.ctrl.command(command), 'OK')
                payload = json.loads(self.summon.call_args.args[0][-1])
                self.assertEqual(payload['output'], output)
                self.assertEqual(payload.get('origin'), origin)
        self.spawn.assert_not_called()

    def test_invalid_origins_do_not_summon_a_menu(self):
        for command in ('settings --origin=launcher', 'open-menu --origin=other',
                        'open-menu DP-2 another-output', 'open-menu --origin=launcher --origin=launcher',
                        'open-menu --origin=launcher --origin=onboarding'):
            with self.subTest(command=command):
                self.assertEqual(self.ctrl.command(command), 'ERR invalid menu arguments')
        self.summon.assert_not_called()
        self.spawn.assert_not_called()

    def test_public_cli_forwards_only_explicit_launcher_origin(self):
        from controller import main
        with patch('controller.request', return_value='OK') as request, \
                patch('controller.runtime_dir', return_value=self.ctrl.runtime), \
                patch('controller.sys.argv', ['doomctl', 'open-menu', '--origin=launcher']), \
                patch('builtins.print'):
            self.assertEqual(main(), 0)
        self.assertEqual(request.call_args.args[0], 'open-menu --origin=launcher')

    def test_idle_hides_asynchronously_and_late_renew_cannot_close_saver(self):
        saver = Mock()
        saver.poll.return_value = None
        with patch.object(self.ctrl, 'launch_viewer', return_value=saver) as launch, \
                patch.object(self.ctrl, 'ensure_engine'), \
                patch.object(self.ctrl, 'engine_loaded', return_value=True), \
                patch.object(self.ctrl, 'reconcile_runtime'):
            self.assertEqual(self.ctrl.command('menu opened'), 'OK')
            self.assertEqual(self.ctrl.command('menu renew'), 'OK')
            self.assertEqual(self.ctrl.command('screensaver'), 'OK')
            self.spawn.assert_called_once_with(
                ['/usr/bin/omarchy-shell', 'shell', 'hide', 'io.github.slogonomo.live-doom'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=__import__('process_utils').session_environment())
            self.spawn.return_value.wait.assert_not_called()
            launch.assert_called_once_with(saver=True, preview=False)
            self.assertEqual(self.ctrl.command('menu renew'), 'ERR lease not held')
            self.assertEqual(self.ctrl.command('menu closed'), 'OK')
            self.assertIs(self.ctrl.saver, saver)
            saver.terminate.assert_not_called()
            self.assertEqual(self.ctrl.mode, 'screensaver')

    def test_expired_or_locked_lease_cannot_be_renewed(self):
        with patch('controller.time.monotonic', return_value=100):
            self.assertEqual(self.ctrl.command('menu opened'), 'OK')
        with patch('controller.time.monotonic', return_value=107):
            self.assertEqual(self.ctrl.command('menu renew'), 'ERR lease not held')
            self.assertEqual(self.ctrl.menu_until, 106)
        self.ctrl.locked = True
        with patch('controller.time.monotonic', return_value=102):
            self.assertEqual(self.ctrl.command('menu renew'), 'ERR lease not held')
            self.assertEqual(self.ctrl.command('menu opened'), 'ERR desktop is unavailable')
        self.ctrl.locked = False
        self.ctrl.snapshot_ok = False
        self.assertEqual(self.ctrl.command('menu opened'), 'ERR desktop is unavailable')

    def test_slow_reload_keeps_only_an_existing_menu_pause(self):
        clock = [100.0]
        self.ctrl.menu_until = 106.0
        def reload(candidate):
            clock[0] = 115.0
            self.assertGreater(self.ctrl.menu_until, clock[0])
            return 'OK'
        with patch('controller.time.monotonic', side_effect=lambda: clock[0]), \
                patch.object(self.ctrl, '_reload_config', side_effect=reload):
            self.assertEqual(self.ctrl.reload_config({}), 'OK')
            self.assertEqual(self.ctrl.menu_until, 121)
            self.assertEqual(self.ctrl.command('menu renew'), 'OK')
        for lease in (0, 99):
            with self.subTest(lease=lease):
                self.ctrl.menu_until = lease
                with patch('controller.time.monotonic', return_value=100), \
                        patch.object(self.ctrl, '_reload_config', return_value='OK'):
                    self.ctrl.reload_config({})
                self.assertEqual(self.ctrl.menu_until, lease)

    def test_reload_cannot_restore_a_revoked_or_unavailable_menu_lease(self):
        for cause in ('locked', 'snapshot', 'revoked', 'timeout', 'error'):
            with self.subTest(cause=cause):
                clock = [100.0]
                self.ctrl.menu_until = 106.0
                self.ctrl.locked = False
                self.ctrl.snapshot_ok = True
                def reload(candidate):
                    clock[0] = 115.0
                    if cause == 'locked':
                        self.ctrl.locked = True
                    elif cause == 'snapshot':
                        self.ctrl.snapshot_ok = False
                    elif cause == 'revoked':
                        self.ctrl.menu_until = 0
                    elif cause == 'timeout':
                        clock[0] = 231
                    else:
                        raise RuntimeError('failed reload')
                    return 'OK'
                with patch('controller.time.monotonic', side_effect=lambda: clock[0]), \
                        patch.object(self.ctrl, '_reload_config', side_effect=reload):
                    if cause == 'error':
                        with self.assertRaises(RuntimeError):
                            self.ctrl.reload_config({})
                        self.assertEqual(self.ctrl.menu_until, 121)
                    else:
                        self.assertEqual(self.ctrl.reload_config({}), 'OK')
                        self.assertEqual(self.ctrl.menu_until, 0)
                        self.assertEqual(self.ctrl.command('menu renew'), 'ERR lease not held')

    def test_hide_launch_failure_does_not_start_a_saver_or_release_lease(self):
        self.assertEqual(self.ctrl.command('menu opened'), 'OK')
        lease = self.ctrl.menu_until
        self.spawn.side_effect = FileNotFoundError('missing shell')
        with patch.object(self.ctrl, 'launch_viewer') as launch:
            self.assertTrue(self.ctrl.command('screensaver').startswith('ERR unable to hide menu:'))
            launch.assert_not_called()
        self.assertEqual(self.ctrl.menu_until, lease)
        self.assertIsNone(self.ctrl.saver)


if __name__ == '__main__':
    unittest.main(verbosity=2)
