# SPDX-License-Identifier: 0BSD
"""First-run selection and consent use only temporary homes and game files."""
from pathlib import Path
import hashlib
import io
import json
import os
import struct
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import onboarding
from background_selection import BackgroundSelection
from controller import Controller, config_for
from project_paths import read_json


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='live-doom-onboarding-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.data = self.home / 'data'
        self.config = self.home / 'config/config.json'
        self.catalog = {'packages': []}
        self.defaults = {'iwad': 'unused', 'pwads': [], 'start_map': None, 'game_package': None}

    def select_background(self, name):
        image = self.home / 'Pictures' / name
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(b'private background fixture')
        link = self.home / '.local/state/omarchy/current/background'
        link.parent.mkdir(parents=True, exist_ok=True)
        link.unlink(missing_ok=True)
        link.symlink_to(image)

    def startup_wallpaper_enabled(self):
        """Inspect the actual startup policy before any native process runs."""
        ctrl = Controller(self.home / 'runtime', self.data, headless=True,
                          config_path=self.config)
        ctrl.background = BackgroundSelection(self.home, self.data)
        seen = []
        def initial_runtime():
            seen.append(ctrl.config['wallpaper_enabled'])
            ctrl.alive = False
        with patch.object(ctrl, 'reconcile_runtime', side_effect=initial_runtime), \
                patch('controller.validate_config'), patch('controller.signal.signal'), \
                patch('controller.subprocess.Popen', side_effect=AssertionError('native startup')):
            ctrl.run()
        self.assertEqual(len(seen), 1)
        return seen[0]

    def test_fresh_static_or_missing_background_starts_with_wallpaper_off(self):
        for background in (None, 'static.webp'):
            with self.subTest(background=background):
                self.config.unlink(missing_ok=True)
                (self.data / onboarding.MARKER).unlink(missing_ok=True)
                if background:
                    self.select_background(background)
                self.assertTrue(self.initialize()['fresh'])
                self.assertIs(read_json(self.config)['wallpaper_enabled'], False)
                self.assertIs(self.startup_wallpaper_enabled(), False)

    def test_fresh_live_background_opts_in_at_controller_startup(self):
        self.select_background('1-live-doom.webp')
        self.initialize()
        self.assertIs(read_json(self.config)['wallpaper_enabled'], False)
        self.assertIs(self.startup_wallpaper_enabled(), True)
        self.assertIs(read_json(self.config)['wallpaper_enabled'], True)

    def test_legacy_static_baseline_keeps_existing_wallpaper_preference(self):
        self.select_background('static.webp')
        self.config.parent.mkdir(mode=0o700)
        self.config.write_text(json.dumps(self.defaults | {'wallpaper_enabled': True}))
        self.assertFalse(self.initialize()['fresh'])
        self.assertIs(self.startup_wallpaper_enabled(), True)

    def test_reinstall_keeps_edited_wallpaper_preference_with_same_background(self):
        self.select_background('1-live-doom.webp')
        self.initialize()
        self.assertIs(self.startup_wallpaper_enabled(), True)
        edited = read_json(self.config) | {'wallpaper_enabled': False, 'pause_blur': 37}
        self.config.write_text(json.dumps(edited))
        before = self.config.read_bytes()
        self.assertFalse(self.initialize()['fresh'])
        self.assertEqual(self.config.read_bytes(), before)
        self.assertIs(self.startup_wallpaper_enabled(), False)
        self.assertEqual(read_json(self.config)['pause_blur'], 37)

    def initialize(self):
        with patch.object(onboarding, 'scan_steam', return_value=self.catalog):
            return onboarding.initialize(self.config, self.data, home=self.home, defaults=self.defaults)

    def game(self, game_id, compatible=True):
        wad = self.home / (game_id + '.wad')
        wad.write_bytes(b'fixture')
        return {'id': game_id, 'compatible': compatible, 'iwad': str(wad), 'pwads': [], 'start_map': 'E1M1'}

    def test_fresh_default_is_freedoom_without_fetching(self):
        result = self.initialize()
        self.assertTrue(result['fresh'])
        self.assertTrue(result['show'])
        self.assertEqual(read_json(self.config)['iwad'], str(self.data / 'assets/freedoom-0.13.0/freedoom1.wad'))
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertIs(read_json(self.config)['screensaver_enabled'], False)
        state = onboarding.settings(self.data, home=self.home)
        self.assertEqual(state['step'], 'background')
        self.assertEqual(state['steps'], ['background', 'game', 'screensaver', 'summary'])
        self.assertEqual(state['choices'], {'background': 'current', 'screensaver': True})
        self.assertIsNone(state['outcome'])

    def test_fresh_saver_suggestion_on_is_inert_and_skip_keeps_it_off(self):
        self.initialize()
        before = self.config.read_bytes()
        self.assertTrue(onboarding.settings(self.data, home=self.home)['choices']['screensaver'])
        onboarding.complete(self.data, 'skipped', config=self.config)
        state = onboarding.settings(self.data, home=self.home, config=self.config)
        self.assertEqual(state['outcome'], 'skipped')
        self.assertTrue(state['choices']['screensaver'])
        self.assertFalse(read_json(self.config)['screensaver_enabled'])
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse((self.home / '.local/state/omarchy/toggles/screensaver-off').exists())

    def test_existing_phase_two_selection_is_preserved_when_default_changes(self):
        self.config.parent.mkdir(mode=0o700)
        existing = self.defaults | {
            'iwad': str(self.data / 'assets/freedoom-0.13.0/freedoom2.wad')}
        self.config.write_text(json.dumps(existing))
        before = self.config.read_bytes()
        self.assertFalse(self.initialize()['fresh'])
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(read_json(self.config)['iwad'], existing['iwad'])

    def test_existing_screensaver_choice_is_preserved_even_without_a_marker(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                self.config.parent.mkdir(mode=0o700, exist_ok=True)
                before = json.dumps(self.defaults | {'screensaver_enabled': enabled}).encode()
                self.config.write_bytes(before)
                (self.data / onboarding.MARKER).unlink(missing_ok=True)
                self.assertFalse(self.initialize()['fresh'])
                self.assertEqual(self.config.read_bytes(), before)
                self.assertEqual(onboarding.settings(self.data, home=self.home)['choices']['screensaver'], enabled)

    def test_nonfresh_disabled_plugin_seeds_effective_screensaver_off(self):
        self.config.parent.mkdir(mode=0o700)
        before = json.dumps(self.defaults | {'enabled': False, 'screensaver_enabled': True}).encode()
        self.config.write_bytes(before)
        self.initialize()
        self.assertIs(onboarding.settings(self.data, home=self.home)['choices']['screensaver'], False)
        self.assertEqual(self.config.read_bytes(), before)

    def test_schema_one_guide_inherits_existing_preference_without_read_time_writes(self):
        self.config.parent.mkdir(mode=0o700)
        self.data.mkdir(mode=0o700)
        path = self.data / onboarding.MARKER
        for original_fresh in (True, False):
            for enabled in (True, False):
                with self.subTest(original_fresh=original_fresh, enabled=enabled):
                    self.config.write_text(json.dumps(self.defaults | {
                        'enabled': True, 'screensaver_enabled': enabled}))
                    path.write_text(json.dumps({'schema': 1, 'show': True, 'fresh': original_fresh,
                                                'selected_package': None}))
                    before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in (self.config, path)]
                    self.assertFalse(self.initialize()['fresh'])
                    guide = onboarding.settings(self.data, home=self.home, config=self.config)
                    self.assertIs(guide['choices']['screensaver'], enabled)
                    self.assertEqual([(p.read_bytes(), p.stat().st_mtime_ns)
                                      for p in (self.config, path)], before)
                    onboarding.set_progress(self.data, 'summary', config=self.config)
                    self.assertIs(read_json(path)['choices']['screensaver'], enabled)
                    self.assertEqual(self.config.read_bytes(), before[0][0])

    def test_existing_explicit_guide_choice_wins_over_later_preferences(self):
        self.initialize()
        for choice in (True, False):
            with self.subTest(choice=choice):
                onboarding.set_progress(self.data, 'summary', screensaver=choice)
                self.config.write_text(json.dumps(self.defaults | {'screensaver_enabled': not choice}))
                path = self.data / onboarding.MARKER
                before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in (self.config, path)]
                self.initialize()
                guide = onboarding.settings(self.data, home=self.home, config=self.config)
                self.assertIs(guide['choices']['screensaver'], choice)
                self.assertEqual([(p.read_bytes(), p.stat().st_mtime_ns)
                                  for p in (self.config, path)], before)

    def test_racing_config_creation_seeds_the_concurrent_existing_choice(self):
        def concurrent(path, value):
            path.parent.mkdir(mode=0o700)
            path.write_text(json.dumps(self.defaults | {'enabled': True, 'screensaver_enabled': True}))
            return False
        with patch.object(onboarding, 'create_config', side_effect=concurrent):
            self.assertFalse(self.initialize()['fresh'])
        self.assertTrue(onboarding.settings(self.data, home=self.home)['choices']['screensaver'])
        self.assertTrue(read_json(self.config)['screensaver_enabled'])

    def test_recoverable_invalid_config_seeds_from_last_good_without_overwriting(self):
        self.config.parent.mkdir(mode=0o700)
        accepted = self.config.with_name('config.last-good.json')
        accepted.write_text(json.dumps(self.defaults | {'enabled': True, 'screensaver_enabled': True}))
        for invalid in ('{incomplete', '[]'):
            with self.subTest(invalid=invalid):
                self.config.write_text(invalid)
                (self.data / onboarding.MARKER).unlink(missing_ok=True)
                before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in (self.config, accepted)]
                self.assertFalse(self.initialize()['fresh'])
                self.assertTrue(onboarding.settings(self.data, home=self.home)['choices']['screensaver'])
                self.assertEqual([(p.read_bytes(), p.stat().st_mtime_ns)
                                  for p in (self.config, accepted)], before)

    def test_progress_and_partial_choices_resume_without_applying_preferences(self):
        self.initialize()
        before = self.config.read_bytes()
        onboarding.set_progress(self.data, 'game', background='phobos')
        onboarding.set_progress(self.data, 'screensaver', screensaver=True)
        self.assertFalse(self.initialize()['fresh'])
        state = onboarding.settings(self.data, home=self.home)
        self.assertEqual(state['step'], 'screensaver')
        self.assertEqual(state['choices'], {'background': 'phobos', 'screensaver': True})
        onboarding.set_progress(self.data, 'background')
        self.assertEqual(onboarding.settings(self.data, home=self.home)['choices'], state['choices'])
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse((self.home / '.local/state/omarchy/toggles/screensaver-off').exists())
        self.assertFalse((self.home / '.config/omarchy/backgrounds').exists())

    def test_invalid_progress_or_choices_leave_state_byte_identical(self):
        self.initialize()
        path = self.data / onboarding.MARKER
        before = path.read_bytes(), path.stat().st_mtime_ns
        for step, choices in (('unknown', {}), ('game', {'background': 'other'}),
                              ('summary', {'screensaver': 'on'}), ('summary', {'screensaver': 1})):
            with self.subTest(step=step, choices=choices), self.assertRaises(ValueError):
                onboarding.set_progress(self.data, step, **choices)
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_completion_preserves_choices_and_first_outcome_without_applying_them(self):
        self.initialize()
        onboarding.set_progress(self.data, 'summary', background='phobos', screensaver=True)
        config = self.config.read_bytes()
        onboarding.complete(self.data, 'skipped')
        state = onboarding.settings(self.data, home=self.home)
        self.assertFalse(state['show'])
        self.assertEqual(state['outcome'], 'skipped')
        self.assertEqual(state['choices'], {'background': 'phobos', 'screensaver': True})
        marker = (self.data / onboarding.MARKER).read_bytes()
        onboarding.complete(self.data, 'finished')
        onboarding.dismiss(self.data)
        self.assertEqual((self.data / onboarding.MARKER).read_bytes(), marker)
        self.assertEqual(self.config.read_bytes(), config)
        with self.assertRaisesRegex(ValueError, 'closed'):
            onboarding.set_progress(self.data, 'background')

    def test_finish_and_legacy_dismiss_have_finished_outcome(self):
        self.initialize()
        onboarding.dismiss(self.data)
        state = onboarding.settings(self.data, home=self.home)
        self.assertFalse(state['show'])
        self.assertEqual((state['step'], state['outcome']), ('summary', 'finished'))

    def test_legacy_marker_reads_are_pure_and_progress_write_upgrades_once(self):
        self.data.mkdir(mode=0o700)
        path = self.data / onboarding.MARKER
        for show in (True, False):
            before = json.dumps({'schema': 1, 'show': show, 'fresh': False,
                                 'selected_package': None}).encode()
            path.write_bytes(before)
            state = onboarding.settings(self.data, home=self.home)
            self.assertEqual(state['show'], show)
            self.assertEqual(state['choices'], {'background': 'current', 'screensaver': False})
            self.assertEqual(path.read_bytes(), before)
        path.write_bytes(json.dumps({'schema': 1, 'show': True}).encode())
        onboarding.set_progress(self.data, 'summary', background='skip')
        self.assertEqual(read_json(path)['schema'], 2)
        self.assertEqual(read_json(path)['choices']['background'], 'skip')

    def test_any_playable_official_package_skips_game_step_without_read_time_writes(self):
        self.catalog['packages'] = [self.game('steam-doom2')]
        self.initialize()
        onboarding.set_progress(self.data, 'game')
        path = self.data / onboarding.MARKER
        before = path.read_bytes(), path.stat().st_mtime_ns
        with patch.object(onboarding, 'read_catalog', return_value=self.catalog):
            state = onboarding.settings(self.data, home=self.home)
            self.assertTrue(state['official_found'])
            self.assertFalse(state['steam_doom'])
            self.assertEqual(state['step'], 'screensaver')
            self.assertEqual(state['steps'], ['background', 'screensaver', 'summary'])
            Path(self.catalog['packages'][0]['iwad']).unlink()
            missing = onboarding.settings(self.data, home=self.home)
            self.assertFalse(missing['official_found'])
            self.assertEqual(missing['step'], 'game')
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_fresh_prefers_steam_doom_and_selects_only_once(self):
        self.catalog['packages'] = [self.game('steam-doom2'), self.game('steam-doom')]
        self.assertEqual(self.initialize()['selected_package'], 'steam-doom')
        changed = self.defaults | {'iwad': 'later-user-choice'}
        self.config.write_text(json.dumps(changed))
        with patch.object(onboarding, 'scan_steam', side_effect=AssertionError('second scan')):
            onboarding.initialize(self.config, self.data, home=self.home)
        self.assertEqual(read_json(self.config), changed)

    def test_fresh_skips_incompatible_and_missing_packages(self):
        missing = self.game('missing')
        Path(missing['iwad']).unlink()
        self.catalog['packages'] = [self.game('unsupported', False), missing, self.game('steam-doom2')]
        self.assertEqual(self.initialize()['selected_package'], 'steam-doom2')

    def test_existing_config_is_byte_identical_even_when_steam_is_found(self):
        self.config.parent.mkdir(mode=0o700)
        original = b'{ "iwad": "my game", "custom": 123 }\n'
        self.config.write_bytes(original)
        self.catalog['packages'] = [self.game('steam-doom')]
        self.assertFalse(self.initialize()['fresh'])
        self.assertEqual(self.config.read_bytes(), original)

    def test_config_publication_never_overwrites_a_file_or_symlink(self):
        self.config.parent.mkdir(mode=0o700)
        self.config.write_bytes(b'original')
        self.assertFalse(onboarding.create_config(self.config, self.defaults))
        self.assertEqual(self.config.read_bytes(), b'original')
        self.config.unlink()
        outside = self.home / 'target'
        outside.write_bytes(b'target')
        self.config.symlink_to(outside)
        self.assertFalse(onboarding.create_config(self.config, self.defaults))
        self.assertEqual(outside.read_bytes(), b'target')

    def test_dismiss_persists_and_settings_do_not_scan_or_download(self):
        self.initialize()
        onboarding.dismiss(self.data)
        with patch.object(onboarding, 'scan_steam', side_effect=AssertionError('scan')), \
                patch.object(onboarding, 'download_shareware', side_effect=AssertionError('download')):
            state = onboarding.settings(self.data, home=self.home)
        self.assertFalse(state['show'])
        self.assertEqual(state['shareware'], 'absent')
        self.assertEqual(state['theme']['url'], 'https://github.com/slogonomo/omarchy-phobos-theme')
        self.assertIn('electronic', state['shareware_terms']['text'].lower())

    def test_marker_symlink_and_invalid_schema_are_rejected(self):
        self.data.mkdir(mode=0o700)
        target = self.home / 'marker-target'
        target.write_text('{}')
        marker = self.data / onboarding.MARKER
        marker.symlink_to(target)
        with self.assertRaises(OSError):
            onboarding.marker(self.data)
        marker.unlink()
        marker.write_text('{"schema": 1, "show": "yes"}')
        with self.assertRaises(ValueError):
            onboarding.marker(self.data)

    def test_download_needs_explicit_terms_acceptance(self):
        with patch('asset_fetch.fetch_verified', side_effect=AssertionError('network')):
            with self.assertRaisesRegex(ValueError, 'accept-terms'):
                onboarding.download_shareware(self.data)
        self.assertFalse(self.data.exists())

    def tar(self, *, extra=False, symlink=False):
        wad = b'IWAD-fixture'
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
            info = tarfile.TarInfo('shareware/doom1.wad')
            if symlink:
                info.type = tarfile.SYMTYPE
                info.linkname = '../outside'
            else:
                info.size = len(wad)
            archive.addfile(info, None if symlink else io.BytesIO(wad))
            if extra:
                archive.addfile(tarfile.TarInfo('extra.exe'))
        data = buffer.getvalue()
        source = {'url': 'https://example.invalid/shareware', 'bytes': len(data),
                  'sha256': hashlib.sha256(data).hexdigest(), 'member': 'shareware/doom1.wad',
                  'wad_bytes': len(wad), 'wad_sha256': hashlib.sha256(wad).hexdigest()}
        return wad, data, source

    def test_tar_reads_exact_wad_and_rejects_extra_paths_links_and_bad_hash(self):
        wad, archive, source = self.tar()
        with patch.object(onboarding, 'SHAREWARE_SOURCE', source):
            self.assertEqual(onboarding.verified_shareware(archive), wad)
            with self.assertRaises(ValueError):
                onboarding.verified_shareware(archive + b'changed')
        for args in ({'extra': True}, {'symlink': True}):
            _, archive, source = self.tar(**args)
            with self.subTest(args=args), patch.object(onboarding, 'SHAREWARE_SOURCE', source), self.assertRaises(ValueError):
                onboarding.verified_shareware(archive)

    def test_download_verifies_before_publication_and_preserves_existing_data(self):
        wad, archive, source = self.tar()
        cached = self.home / 'archive.tar.gz'
        cached.write_bytes(archive)
        with patch.object(onboarding, 'SHAREWARE_SOURCE', source), \
                patch('asset_fetch.fetch_verified', return_value=cached) as fetch:
            destination = onboarding.download_shareware(self.data, accept_terms=True)
            self.assertEqual(destination.read_bytes(), wad)
            self.assertTrue(fetch.call_args.kwargs['same_host'])
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            destination.write_bytes(b'personal-file')
            with self.assertRaises(ValueError):
                onboarding.download_shareware(self.data, accept_terms=True)
            self.assertEqual(destination.read_bytes(), b'personal-file')


class BundledGameSelectionTests(unittest.TestCase):
    """Switch synthetic, pinned WADs through the real unloaded Controller."""
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='live-doom-bundled-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.data = self.home / 'data'
        self.runtime = self.home / 'runtime'
        self.freedoom = self.wad('assets/freedoom-0.13.0/freedoom1.wad', 'MAP01')
        self.shareware = self.wad('assets/shareware/doom1.wad', 'E1M1')
        self.addon = self.wad('custom/addon.wad', 'MAP02', b'PWAD')
        pins = self.home / 'pins.json'
        pins.write_text(json.dumps({'freedoom': {'version': '0.13.0', 'max_unpacked_bytes': 4096,
                    'files': {'freedoom1.wad': hashlib.sha256(self.freedoom.read_bytes()).hexdigest()}}}))
        for replacement in (patch.dict(os.environ, {'HOME': str(self.home),
                                'XDG_CONFIG_HOME': str(self.home / '.config'),
                                'XDG_STATE_HOME': str(self.home / '.local/state'),
                                'XDG_DATA_HOME': str(self.home / '.local/share')}),
                            patch.object(onboarding, 'BUILD_PINS', pins),
                            patch.object(onboarding, 'SHAREWARE_SOURCE', dict(onboarding.SHAREWARE_SOURCE,
                                wad_sha256=hashlib.sha256(self.shareware.read_bytes()).hexdigest(),
                                wad_bytes=self.shareware.stat().st_size)),
                            patch('controller.Path.home', return_value=self.home)):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.config = self.data / 'config.json'
        self.config.write_text(json.dumps(config_for(self.config) | {
            'iwad': str(self.freedoom), 'pwads': [str(self.addon)], 'start_map': 'MAP02',
            'game_package': 'steam-fixture', 'wallpaper_enabled': False, 'screensaver_enabled': False}))
        self.ctrl = Controller(self.runtime, self.data, headless=True)
        self.ctrl.reconcile_runtime = Mock()
        self.ctrl.policy = Mock()
        native = patch('controller.engine_request', side_effect=AssertionError('native launch'))
        self.native = native.start()
        self.addCleanup(native.stop)

    def wad(self, relative, map_name, magic=b'IWAD'):
        path = self.data / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(struct.pack('<4sii', magic, 1, 12) +
                         struct.pack('<ii8s', 12, 0, map_name.encode()))
        return path

    def test_switching_bundled_games_clears_campaign_and_mods_atomically(self):
        for game, wad in (('shareware', self.shareware), ('freedoom', self.freedoom),
                          ('shareware', self.shareware)):
            with self.subTest(game=game):
                answer = json.loads(self.ctrl.command('use-game ' + game))
                self.assertEqual(answer['game']['selected_package'], game)
                self.assertEqual(answer['game']['iwad'], str(wad))
                self.assertEqual(answer['game']['pwads'], [])
                self.assertIsNone(answer['game']['start_map'])
                saved = json.loads(self.config.read_text())
                self.assertEqual(saved, self.ctrl.config)
                self.assertIsNone(saved['game_package'])
                self.assertFalse(saved['screensaver_enabled'])
        self.native.assert_not_called()

    def test_missing_changed_and_linked_bundles_cannot_replace_the_game(self):
        before = self.config.read_bytes(), self.config.stat().st_mtime_ns, dict(self.ctrl.config)
        for game, wad in (('freedoom', self.freedoom), ('shareware', self.shareware)):
            content = wad.read_bytes()
            wad.unlink()
            for state in ('missing', 'changed', 'link'):
                with self.subTest(game=game, state=state):
                    if state == 'changed':
                        wad.write_bytes(content + b'changed')
                    elif state == 'link':
                        wad.unlink()
                        target = self.home / 'external.wad'
                        target.write_bytes(content)
                        wad.symlink_to(target)
                    self.assertTrue(self.ctrl.command('use-game ' + game).startswith('ERR'))
                    self.assertEqual((self.config.read_bytes(), self.config.stat().st_mtime_ns,
                                      dict(self.ctrl.config)), before)
            wad.unlink()
            wad.write_bytes(content)
        self.ctrl.reconcile_runtime.assert_not_called()
        self.native.assert_not_called()

    def test_download_selects_verified_shareware_only_at_explicit_consent(self):
        with patch.object(onboarding, 'download_shareware', return_value=self.shareware) as download:
            self.assertTrue(self.ctrl.command('download-shareware').startswith('ERR'))
            download.assert_not_called()
            answer = json.loads(self.ctrl.command('download-shareware --accept-terms'))
            download.assert_called_once_with(self.data, accept_terms=True)
            self.assertEqual(answer['game']['selected_package'], 'shareware')
            self.assertEqual(answer['game']['pwads'], [])
            self.assertIsNone(answer['game']['start_map'])
            self.assertFalse(self.ctrl.config['screensaver_enabled'])
            self.assertFalse(self.ctrl.onboarding_downloading)
            self.assertEqual(answer['onboarding']['shareware'], 'present')


if __name__ == '__main__':
    unittest.main()
