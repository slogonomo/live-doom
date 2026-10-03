#!/usr/bin/env python3
# SPDX-License-Identifier: 0BSD
"""Native-rate rendering and Quit ownership policy, without host actions."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import controller


class OutputRenderRateTests(unittest.TestCase):
    def test_all_enabled_outputs_use35_regardless_of_refresh(self):
        cases = (60, 100, 175, 24, 59.94, 100.49, 100.51, None, 0, -60,
                 True, False, "malformed", {}, [], float("nan"), float("inf"), -float("inf"))
        monitors = [{"name": str(i), "refreshRate": rate} for i, rate in enumerate(cases)]
        monitors += [{"name": "missing"}, {"name": "disabled", "refreshRate": 100, "disabled": True}]
        expected = {str(i): 35 for i in range(len(cases))} | {"missing": 35}
        self.assertEqual(controller.output_render_rates(monitors), expected)


class BehindWindowsPreferenceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='doom-behind-windows-')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.config_path = self.base / 'config.json'

    def test_missing_config_defaults_to_light_without_creating_a_file(self):
        self.assertEqual(controller.config_for(self.config_path)['wallpaper_behind_windows'], 'light')
        self.assertFalse(self.config_path.exists())

    def test_fresh_onboarding_config_inherits_light_without_enabling_background(self):
        home = self.base / 'home'
        home.mkdir()
        state = self.base / 'state'
        result = controller.onboarding.initialize(self.config_path, state, home=home, scan=False)
        self.assertTrue(result['fresh'])
        stored = json.loads(self.config_path.read_text())
        self.assertEqual(stored['wallpaper_behind_windows'], 'light')
        self.assertFalse(stored['wallpaper_enabled'])
        self.assertFalse(stored['screensaver_enabled'])

    def test_legacy_config_defaults_to_light_and_explicit_full_is_preserved_read_only(self):
        legacy = {'wallpaper_mode': 'all', 'desktop_sound': True, 'skill': 4}
        for changes, expected in (({}, 'light'), ({'wallpaper_behind_windows': 'full'}, 'full')):
            with self.subTest(changes=changes):
                data = (json.dumps(legacy | changes, indent=2) + '\n').encode()
                self.config_path.write_bytes(data)
                config = controller.config_for(self.config_path)
                self.assertEqual(config['wallpaper_behind_windows'], expected)
                self.assertEqual(config['wallpaper_mode'], 'all')
                self.assertTrue(config['desktop_sound'])
                self.assertEqual(config['skill'], 4)
                self.assertEqual(self.config_path.read_bytes(), data)

    def test_only_light_and_full_are_valid_preference_values(self):
        config = controller.config_for(self.config_path)
        with patch('controller.validate_wad'):
            for value in ('light', 'full'):
                controller.validate_config(config | {'wallpaper_behind_windows': value})
            for value in (None, True, False, 0, 1, 4, 1.0, [], {}, ['light'], ('full',),
                          '', 'Light', 'Full', 'all', 'fast', ' light ', '4'):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    controller.validate_config(config | {'wallpaper_behind_windows': value})


class RenderOwnershipPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="doom-render-policy-")
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.ctrl = controller.Controller(base / "runtime", base / "state", headless=True)
        self.ctrl.log = Mock()
        self.ctrl.engine = Mock()
        self.ctrl.engine.poll.return_value = None
        self.header = {"human_quits": 7}
        native_patch = patch("controller.engine_request", return_value="OK")
        self.native = native_patch.start()
        self.addCleanup(native_patch.stop)
        frame_patch = patch("controller.read_header", side_effect=lambda _: dict(self.header))
        frame_patch.start()
        self.addCleanup(frame_patch.stop)
        batch_patch = patch("controller.engine_request_batch", side_effect=lambda lines, _: ["OK"] * len(lines))
        self.batch = batch_patch.start()
        self.addCleanup(batch_patch.stop)

    def native_commands(self):
        return [call.args[0] for call in self.native.call_args_list]

    def render_commands(self):
        return [line for line in self.native_commands() if line.startswith("render ")]

    def test_busy_all_desktops_present_stride_is_per_output_and_does_not_change_engine(self):
        self.ctrl.config['wallpaper_mode'] = 'all'
        self.ctrl.views = {'BUSY': {'empty': False, 'awake': True, 'reserved': (0, 30, 0, 0)},
                           'EMPTY': {'empty': True, 'awake': True, 'reserved': (0, 0, 0, 0)}}
        self.assertEqual(self.ctrl.command('view BUSY'), 'STATE bot 0 20 0 30 0 0 4')
        self.assertEqual(self.ctrl.command('view EMPTY'), 'STATE bot 0 20 0 0 0 0')
        self.ctrl.views['BUSY']['empty'] = True
        self.assertEqual(self.ctrl.command('view BUSY'), 'STATE bot 0 20 0 30 0 0')
        self.ctrl.human_output = 'BUSY'
        self.assertEqual(self.ctrl.command('view BUSY'), 'STATE human 0 20 0 30 0 0')
        self.ctrl.views['BUSY']['empty'] = False
        self.assertEqual(self.ctrl.command('view BUSY'), 'STATE human 0 20 0 30 0 0')
        self.ctrl.saver = True
        self.assertEqual(self.ctrl.command('view BUSY'), 'STATE hidden 0 20 0 30 0 0')
        self.native.assert_not_called()
        self.batch.assert_not_called()
        self.assertEqual(self.header, {'human_quits': 7})

    def test_only_running_all_desktops_busy_views_request_stride_four(self):
        self.ctrl.views = {'TEST': {'empty': False, 'awake': True}}
        self.assertEqual(self.ctrl.command('view TEST'), 'STATE paused 0 20 0 0 0 0')
        self.ctrl.config['wallpaper_mode'] = 'all'
        self.assertEqual(self.ctrl.command('view TEST'), 'STATE bot 0 20 0 0 0 0 4')
        self.ctrl.views['TEST']['awake'] = False
        self.assertEqual(self.ctrl.command('view TEST'), 'STATE paused 0 20 0 0 0 0')
        self.ctrl.views['TEST']['awake'] = True
        self.ctrl.locked = True
        self.assertEqual(self.ctrl.command('view TEST'), 'STATE hidden 0 20 0 0 0 0')
        self.ctrl.locked = False
        self.ctrl.snapshot_ok = False
        self.assertEqual(self.ctrl.command('view TEST'), 'STATE hidden 0 20 0 0 0 0')
        self.native.assert_not_called()

    def test_visible_special_workspace_is_busy_for_presentation_policy(self):
        monitors = [{'name': 'A', 'activeWorkspace': {'id': 1}, 'specialWorkspace': {'id': -99}},
                    {'name': 'B', 'activeWorkspace': {'id': 2}, 'specialWorkspace': {'id': 0}}]
        clients = [{'workspace': {'id': -99}, 'mapped': True, 'hidden': False},
                   {'workspace': {'id': 2}, 'mapped': False},
                   {'workspace': {'id': 2}, 'hidden': True}]
        self.ctrl.views = controller.workspace_views(monitors, clients)
        self.ctrl.config['wallpaper_mode'] = 'all'
        self.assertEqual(self.ctrl.command('view A'), 'STATE bot 0 20 0 0 0 0 4')
        self.assertEqual(self.ctrl.command('view B'), 'STATE bot 0 20 0 0 0 0')
        self.native.assert_not_called()

    def test_behind_windows_setting_roundtrip_applies_only_viewer_policy_in_place(self):
        self.ctrl.config['wallpaper_mode'] = 'all'
        self.ctrl.views = {'BUSY': {'empty': False, 'awake': True, 'reserved': (0, 30, 0, 0)},
                           'EMPTY': {'empty': True, 'awake': True}}
        self.ctrl.render_size = controller.render_dimensions(self.ctrl.config, headless=True)
        self.ctrl.last_controls = (self.ctrl.config['mouse_sensitivity'], self.ctrl.config['mouselook'])
        self.ctrl.policy()
        self.native.reset_mock()
        self.batch.reset_mock()
        engine = self.ctrl.engine
        with patch('controller.validate_wad'), patch.object(self.ctrl, 'start_engine') as start, \
                patch.object(self.ctrl, 'unload_engine') as unload, \
                patch.object(self.ctrl, 'stop_desktop_viewer') as stop:
            for preference, busy_state in (('full', 'STATE bot 0 20 0 30 0 0'),
                                           ('light', 'STATE bot 0 20 0 30 0 0 4')):
                with self.subTest(preference=preference):
                    answer = json.loads(self.ctrl.command('set wallpaper.behind_windows ' + json.dumps(preference)))
                    self.assertEqual(answer['wallpaper']['behind_windows'], preference)
                    self.assertEqual(json.loads(self.ctrl.command('settings-json'))['wallpaper']['behind_windows'], preference)
                    self.assertEqual(json.loads(self.ctrl.config_path.read_text())['wallpaper_behind_windows'], preference)
                    self.assertEqual(controller.config_for(self.ctrl.config_path)['wallpaper_behind_windows'], preference)
                    self.assertEqual(self.ctrl.command('view BUSY'), busy_state)
                    self.assertEqual(self.ctrl.command('view EMPTY'), 'STATE bot 0 20 0 0 0 0')
                    self.assertIs(self.ctrl.engine, engine)
                    self.assertEqual(self.ctrl.mode, 'bot')
                    self.assertEqual(self.ctrl.last_controls, (1.0, False))
                    self.native.assert_not_called()
                    self.batch.assert_not_called()
                    start.assert_not_called()
                    unload.assert_not_called()
                    stop.assert_not_called()
        self.assertEqual(self.header, {'human_quits': 7})

    def test_behind_windows_setting_preserves_active_human_and_all_native_caches(self):
        self.ctrl.config['wallpaper_mode'] = 'all'
        self.ctrl.views['OTHER'] = {'empty': False, 'awake': True}
        self.ctrl.render_size = controller.render_dimensions(self.ctrl.config, headless=True)
        self.ctrl.last_controls = (1.0, False)
        self.assertEqual(self.ctrl.command('takeover TEST'), 'OK')
        self.ctrl.viewer = Mock()
        self.ctrl.viewer.poll.return_value = None
        baseline = (self.ctrl.engine, self.ctrl.viewer, self.ctrl.last_policy,
                    self.ctrl.last_controls, self.ctrl.last_render_rate,
                    self.ctrl.human_output, self.ctrl.human_workspace, self.ctrl.input_generation)
        self.native.reset_mock()
        self.batch.reset_mock()
        with patch('controller.validate_wad'), patch.object(self.ctrl, 'start_engine') as start, \
                patch.object(self.ctrl, 'unload_engine') as unload, \
                patch.object(self.ctrl, 'stop_desktop_viewer') as stop, \
                patch.object(self.ctrl, 'stop_saver') as stop_saver, \
                patch.object(self.ctrl, 'return_bot') as handback:
            for preference, other_state in (('full', 'STATE bot 1 20 0 0 0 0'),
                                            ('light', 'STATE bot 1 20 0 0 0 0 4')):
                with self.subTest(preference=preference):
                    answer = json.loads(self.ctrl.command('set wallpaper.behind_windows ' + json.dumps(preference)))
                    self.assertEqual(answer['wallpaper']['behind_windows'], preference)
                    self.assertEqual(self.ctrl.command('view TEST'), 'STATE human 1 20 0 0 0 0')
                    self.assertEqual(self.ctrl.command('view OTHER'), other_state)
                    self.assertEqual((self.ctrl.engine, self.ctrl.viewer, self.ctrl.last_policy,
                                      self.ctrl.last_controls, self.ctrl.last_render_rate,
                                      self.ctrl.human_output, self.ctrl.human_workspace,
                                      self.ctrl.input_generation), baseline)
                    self.assertEqual(self.ctrl.mode, 'human')
                    self.native.assert_not_called()
                    self.batch.assert_not_called()
                    for operation in (start, unload, stop, stop_saver, handback):
                        operation.assert_not_called()

    def test_legacy_full_config_candidate_gets_light_default_without_engine_changes(self):
        candidate = dict(self.ctrl.config)
        candidate.pop('wallpaper_behind_windows')
        self.ctrl.render_size = controller.render_dimensions(self.ctrl.config, headless=True)
        self.ctrl.last_controls = (1.0, False)
        self.ctrl.policy()
        self.native.reset_mock()
        self.batch.reset_mock()
        with patch('controller.validate_wad'), patch.object(self.ctrl, 'stop_desktop_viewer'):
            self.assertEqual(self.ctrl.reload_config(candidate), 'OK')
        self.assertEqual(self.ctrl.config['wallpaper_behind_windows'], 'light')
        self.assertEqual(controller.config_for(self.ctrl.config_path)['wallpaper_behind_windows'], 'light')
        self.native.assert_not_called()
        self.batch.assert_not_called()

    def test_rejected_behind_windows_setting_keeps_previous_preference_and_cadence(self):
        self.ctrl.config['wallpaper_mode'] = 'all'
        self.ctrl.views = {'TEST': {'empty': False, 'awake': True}}
        with patch('controller.validate_wad'):
            for value in (None, False, 4, [], {}, 'fast', 'FULL'):
                with self.subTest(value=value):
                    answer = self.ctrl.command('set wallpaper.behind_windows ' + json.dumps(value))
                    self.assertTrue(answer.startswith('ERR '), answer)
                    self.assertEqual(self.ctrl.config['wallpaper_behind_windows'], 'light')
                    self.assertEqual(json.loads(self.ctrl.config_path.read_text())['wallpaper_behind_windows'], 'light')
                    self.assertEqual(self.ctrl.command('view TEST'), 'STATE bot 0 20 0 0 0 0 4')
        self.native.assert_not_called()
        self.batch.assert_not_called()

    def test_snapshot_publishes_output_refresh_rates_with_workspace_views(self):
        monitors = [{"name": "TEST", "refreshRate": 100.2, "width": 800, "height": 600,
                     "activeWorkspace": {"id": 1}, "specialWorkspace": {"id": 0},
                     "focused": True, "dpmsStatus": True},
                    {"name": "SECOND", "refreshRate": 175, "width": 800, "height": 600,
                     "activeWorkspace": {"id": 2}, "specialWorkspace": {"id": 0},
                     "focused": False, "dpmsStatus": True}]
        answers = {"monitors": json.dumps(monitors).encode(), "clients": b"[]", "isLocked": b"false"}

        def single_cycle(_):
            self.ctrl.alive = False

        with patch("controller.bounded_run", side_effect=lambda args, **_: Mock(stdout=answers[args[-1]])), \
             patch("controller.time.sleep", side_effect=single_cycle):
            self.ctrl.snapshots()
        self.assertTrue(self.ctrl.snapshot_ok)
        self.assertFalse(self.ctrl.locked)
        self.assertEqual(self.ctrl.output_rates, {"TEST": 35, "SECOND": 35})
        self.assertEqual(set(self.ctrl.views), {"TEST", "SECOND"})
        self.assertTrue(self.ctrl.views["TEST"]["focused"])
        self.native.assert_not_called()

    def test_human_rate_applies_once_and_ignores_monitor_refresh_changes(self):
        self.ctrl.output_rates = {"TEST": 100}
        self.assertEqual(self.ctrl.command("takeover TEST"), "OK")
        self.assertEqual(self.render_commands(), ["render 35"])
        for _ in range(4):
            self.ctrl.policy()
        self.assertEqual(self.render_commands(), ["render 35"])
        with self.ctrl.sync:
            self.ctrl.output_rates["TEST"] = 120
        self.ctrl.policy()
        self.ctrl.policy()
        self.assertEqual(self.render_commands(), ["render 35"])
        self.assertEqual(self.ctrl.mode, "human")
        self.assertEqual(self.ctrl.human_output, "TEST")

    def test_bot_and_saver_do_not_request_output_refresh_rendering(self):
        self.ctrl.output_rates = {"TEST": 120}
        self.ctrl.policy()
        self.assertEqual(self.ctrl.mode, "bot")
        self.ctrl.saver = True
        self.ctrl.policy()
        self.assertEqual(self.ctrl.mode, "screensaver")
        self.assertEqual(self.render_commands(), [])

    def test_missing_rate_defaults_to35_and_lock_revocation_sends_no_new_render_rate(self):
        self.ctrl.output_rates = {}
        self.assertEqual(self.ctrl.command("takeover TEST"), "OK")
        self.assertEqual(self.render_commands(), ["render 35"])
        with self.ctrl.sync:
            self.ctrl.output_rates["TEST"] = 120
            self.ctrl.locked = True
        self.ctrl.policy()
        self.assertEqual(self.render_commands(), ["render 35"])
        self.assertIsNone(self.ctrl.human_output)
        self.assertEqual(self.ctrl.mode, "paused")

    def test_external_agent_uses_same_native_rate(self):
        self.ctrl.native_schema = 3
        with patch.object(self.ctrl.agents, 'desired_owner', return_value=1):
            self.ctrl.policy()
            self.ctrl.policy()
        self.assertEqual(self.ctrl.mode, 'agent')
        self.assertEqual(self.render_commands(), ['render 35'])

    def test_reload_reapplies_rate_without_changing_output(self):
        self.assertEqual(self.ctrl.command('takeover TEST'), 'OK')
        with patch('controller.validate_config'), patch.object(self.ctrl, 'record_preferences'):
            self.assertEqual(self.ctrl.reload_config(self.ctrl.config | {'pause_blur': 30}), 'OK')
        self.assertEqual(self.render_commands(), ['render 35', 'render 35'])
        self.assertEqual(self.ctrl.last_render_rate, 35)

    def test_failed_rate_request_is_retried_instead_of_cached(self):
        attempts = []
        def response(command, _):
            if command.startswith('render '):
                attempts.append(command)
                return 'ERR private rate failure' if len(attempts) == 1 else 'OK'
            return 'OK'
        self.native.side_effect = response
        self.assertEqual(self.ctrl.command('takeover TEST'), 'OK')
        self.assertIsNone(self.ctrl.last_render_rate)
        self.ctrl.policy()
        self.ctrl.policy()
        self.assertEqual(attempts, ['render 35', 'render 35'])
        self.assertEqual(self.ctrl.last_render_rate, 35)

    def test_native_quit_counter_revokes_view_and_input_without_revoking_a_new_lease(self):
        self.ctrl.output_rates = {"TEST": 100}
        self.assertEqual(self.ctrl.command("takeover TEST"), "OK")
        self.assertEqual(self.ctrl.last_human_quits, 7)
        lease = self.ctrl.input_generation
        self.assertEqual(self.ctrl.input_batch(["key 119 1"], "TEST", lease), ["OK"])
        self.ctrl.policy()
        self.assertEqual(self.ctrl.human_output, "TEST")
        self.assertEqual(self.ctrl.input_generation, lease)

        self.header["human_quits"] = 8
        self.native.reset_mock()
        self.batch.reset_mock()
        self.ctrl.policy()
        self.assertIsNone(self.ctrl.human_output)
        self.assertIsNone(self.ctrl.human_workspace)
        self.assertGreater(self.ctrl.input_generation, lease)
        self.assertEqual(self.ctrl.mode, "bot")
        self.assertEqual(self.ctrl.command("view TEST"), "STATE bot 0 20 0 0 0 0")
        self.assertEqual(self.native_commands().count("release"), 1)
        self.assertIn("bot 1", self.native_commands())
        self.assertIn("audio 0", self.native_commands())
        self.assertEqual(self.ctrl.input_batch(["key 119 1", "mouse 3 0"], "TEST", lease),
                         ["ERR input lease expired"] * 2)
        self.assertEqual(self.ctrl.command("key 119 1"), "ERR input requires desktop takeover")
        self.batch.assert_not_called()

        self.assertEqual(self.ctrl.command("takeover TEST"), "OK")
        new_lease = self.ctrl.input_generation
        self.assertEqual(self.ctrl.last_human_quits, 8)
        self.native.reset_mock()
        self.assertEqual(self.ctrl.input_batch(["key 119 0"], "TEST", lease), ["ERR input lease expired"])
        self.assertEqual(self.ctrl.human_output, "TEST")
        self.assertEqual(self.ctrl.input_generation, new_lease)
        self.assertNotIn("release", self.native_commands())

    def test_transient_header_read_does_not_revoke_new_takeover_for_an_earlier_quit(self):
        with patch("controller.read_header", side_effect=[{}, {}, {"human_quits": 7}, {"human_quits": 7}]), \
             patch("controller.time.sleep"):
            self.assertEqual(self.ctrl.command("takeover TEST"), "OK")
        self.assertEqual(self.ctrl.last_human_quits, 7)
        self.assertEqual(self.ctrl.human_output, "TEST")
        self.ctrl.policy()
        self.assertEqual(self.ctrl.human_output, "TEST")
        self.header["human_quits"] = 8
        self.ctrl.policy()
        self.assertIsNone(self.ctrl.human_output)

    def test_unavailable_header_refuses_takeover_without_claiming_input(self):
        with patch("controller.read_header", return_value={}), patch("controller.time.sleep"):
            self.assertEqual(self.ctrl.command("takeover TEST"), "ERR game state is unavailable")
        self.assertIsNone(self.ctrl.human_output)
        self.native.assert_not_called()


if __name__ == "__main__":
    unittest.main()
