# SPDX-License-Identifier: 0BSD
"""Marketplace setup/removal in private prefixes; never contact the desktop."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'src'))
import install
import uninstall
import install_support as support
from project_paths import PLUGIN_ID, ProjectPaths


class MarketplaceInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='live-doom-setup-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / 'private home'
        self.home.mkdir()
        self.paths = ProjectPaths.from_env(home=self.home)
        self.root = self.home / '.config/omarchy/plugins' / PLUGIN_ID
        (self.root / 'menu').mkdir(parents=True)
        (self.root / 'src').mkdir()
        (self.root / 'scripts').mkdir()
        (self.root / 'manifest.json').write_text(json.dumps({
            'id': PLUGIN_ID, 'kinds': ['overlay'], 'entryPoints': {'overlay': 'menu/Menu.qml'}}))
        (self.root / 'menu/Menu.qml').write_text('Item {}\n')
        (self.root / 'src/controller.py').write_text('# source fixture\n')
        (self.root / 'scripts/doomctl').write_text('# launcher fixture\n')
        (self.root / 'scripts/doom-desktop.service').write_bytes((ROOT / 'scripts/doom-desktop.service').read_bytes())
        self.omarchy = self.home / 'stock'
        self.omarchy.mkdir()
        self.config = {'version': 1, 'plugins': [{'id': 'omarchy.clock', 'setting': 4}],
                       'idle': {'screensaver': 150, 'lock': 300}, 'custom': {'preserve': True}}
        support.atomic_write(self.home / support.SHELL_REL, support.json_bytes(self.config))
        self.journal = self.paths.state_root / 'install.json'
        self.service = self.paths.config_root.parent / 'systemd/user' / support.SERVICE_NAME
        # Only first-run metadata/config creation runs in a subprocess. It uses
        # explicit fixture home and XDG dirs, no display, engine or host scan.
        initializer = support.initialize_marketplace
        init = patch.object(support, 'initialize_marketplace', side_effect=lambda _, home, paths:
                            initializer(ROOT, home, paths))
        self.initialize = init.start()
        self.addCleanup(init.stop)
        self.ipc = Mock(side_effect=AssertionError('Unexpected service or desktop process'))
        actual_run = support.run
        def commands(args, **options):
            if len(args) > 2 and Path(args[1]).name == 'game_catalog.py' and args[2] == 'scan':
                self.assertEqual(Path(args[args.index('--home') + 1]), self.home)
                self.assertEqual(Path(args[args.index('--state') + 1]), self.paths.data_root)
                return actual_run([args[0], str(ROOT / 'src/game_catalog.py'), *args[2:]], **options)
            return self.ipc(args, **options)
        ipc = patch.object(support, 'run', side_effect=commands)
        ipc.start()
        self.addCleanup(ipc.stop)

    def args(self, **options):
        values = dict(prefix=self.home, dry_run=False, no_activate=True, yes=False,
                      without_design_bundles=False, menu_only=False, replace_modified=False, dev=False,
                      omarchy_path=self.omarchy)
        return SimpleNamespace(**(values | options))

    def setup(self, **options):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return support.marketplace_install(self.args(**options), self.root)

    def remove(self, **options):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return support.marketplace_uninstall(self.args(**options))

    def snapshot(self):
        return {str(p.relative_to(self.home)): (p.read_bytes(), p.stat().st_mode & 0o777, p.stat().st_mtime_ns)
                for p in self.home.rglob('*') if p.is_file() and not p.is_symlink()}

    def snapshot_inputs(self, source):
        for name in ('assets/live-doom.webp', 'README.md', 'LICENSE', 'THIRD-PARTY-NOTICES.md',
                     'LICENSES/0BSD.txt', 'LICENSES/CC0-1.0.txt', 'LICENSES/GPL-3.0-or-later.txt',
                     'docs/licenses/Doom-shareware.txt', 'docs/licenses/Doom-shareware-1.8.txt',
                     'patches/autodoom-desktop.patch', 'patches/sdl-mixer-device.patch',
                     'scripts/build-pins.json'):
            destination = source / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, destination)

    def test_default_enables_checkout_without_menu_copy_theme_or_idle_clone(self):
        self.assertEqual(self.setup(), 0)
        config = support.load_json(self.home / support.SHELL_REL)
        self.assertEqual(config['plugins'], self.config['plugins'] + [{'id': PLUGIN_ID}])
        self.assertEqual(config['idle'], self.config['idle'])
        self.assertEqual(config['custom'], self.config['custom'])
        journal = support.load_json(self.journal)
        self.assertEqual(journal['plugin_id'], PLUGIN_ID)
        self.assertFalse(any('/omarchy/plugins/' in name or '/omarchy/themes/' in name for name in journal['files']))
        self.assertFalse((self.home / '.config/omarchy/plugins' / support.LEGACY_MENU_ID).exists())
        self.assertFalse((self.home / '.config/omarchy/plugins' / support.LEGACY_IDLE_ID).exists())
        self.assertFalse((self.home / '.config/omarchy/themes').exists())
        self.assertTrue(self.paths.config_file.exists())
        self.assertEqual(json.loads(self.paths.config_file.read_text())['iwad'], str(self.paths.default_iwad))
        self.assertTrue(self.paths.catalog_file.exists())
        self.ipc.assert_not_called()

    def test_unit_removal_guard_rate_limit_and_bytecode_setting(self):
        with patch.object(support.sys, 'executable', '/untrusted/python'):
            rendered = support.render_service(self.root).decode()
            self.assertIn('ExecStart=/usr/bin/python3 -E -s -B ', rendered)
            self.assertNotIn('/untrusted/python', rendered)
        self.assertEqual(self.setup(), 0)
        text = self.service.read_text()
        self.assertIn('ExecStart=/usr/bin/python3 -E -s -B ', text)
        self.assertNotIn('/untrusted/python', text)
        self.assertIn('ConditionPathExists=' + str(self.root / 'src/controller.py'), text)
        self.assertIn('StartLimitIntervalSec=60', text)
        self.assertIn('StartLimitBurst=3', text)
        self.assertIn('Environment=PYTHONDONTWRITEBYTECODE=1', text)
        self.assertNotIn('@ROOT@', text)

    def test_launcher_marks_its_origin_without_changing_install_welcome_summon(self):
        self.assertEqual(self.setup(), 0)
        launcher = self.paths.data_root.parent / 'applications/live-doom.desktop'
        text = launcher.read_text()
        self.assertIn(' open-menu --origin=launcher\n', text)
        self.assertIn('Name=Live Doom\n', text)
        self.assertIn('Comment=Configure the live Doom background and screensaver\n', text)

    def test_prefix_ignores_host_xdg_and_dry_run_is_read_only(self):
        before = self.snapshot()
        with patch.dict(os.environ, {'XDG_CONFIG_HOME': '/not-this-test/config', 'XDG_DATA_HOME': '/not-this-test/data',
                                    'XDG_STATE_HOME': '/not-this-test/state', 'DOOM_DESKTOP_STATE': '/not-this-test/old'}):
            self.assertEqual(self.setup(dry_run=True), 0)
            self.assertEqual(self.snapshot(), before)
            self.initialize.assert_not_called()
            self.assertEqual(self.setup(), 0)
        self.assertTrue(self.paths.config_file.exists())
        self.assertTrue(self.paths.catalog_file.exists())

    def test_reinstall_preserves_existing_config_and_unchanged_artifact_mtimes(self):
        self.assertEqual(self.setup(), 0)
        chosen = b'{"iwad":"/my/game.wad","pwads":["/my/mod.wad"],"game_package":null}\n'
        self.paths.config_file.write_bytes(chosen)
        before = self.snapshot()
        self.assertEqual(self.setup(), 0)
        after = self.snapshot()
        self.assertEqual(after[str(self.paths.config_file.relative_to(self.home))], before[str(self.paths.config_file.relative_to(self.home))])
        for p in (self.service, self.home / support.SHELL_REL):
            self.assertEqual(after[str(p.relative_to(self.home))], before[str(p.relative_to(self.home))])

    def test_legacy_config_and_last_good_are_adopted_without_copying_games(self):
        original = b'{"iwad":"/legacy/game.wad","pwads":["/legacy/mod.wad"],"skill":4}\n'
        support.atomic_write(self.paths.legacy_data_root / 'config.json', original, 0o600)
        support.atomic_write(self.paths.legacy_data_root / 'config.last-good.json', original, 0o600)
        checkpoint = self.paths.legacy_data_root / 'games/private/saves/etersav7.dsg'
        support.atomic_write(checkpoint, b'KEEP_NATIVE_BYTES', 0o600)
        self.assertEqual(self.setup(), 0)
        for p in (self.paths.config_file, self.paths.last_good_config):
            self.assertEqual(p.read_bytes(), original)
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        self.assertEqual(checkpoint.read_bytes(), b'KEEP_NATIVE_BYTES')
        self.assertFalse(self.paths.games_root.exists())
        self.assertFalse(json.loads((self.paths.data_root / 'onboarding.json').read_text())['fresh'])

    def test_existing_canonical_choice_wins_over_legacy_config(self):
        self.paths.config_root.mkdir(mode=0o700)
        support.atomic_write(self.paths.config_file, b'{"preserve":"canonical"}\n', 0o600)
        support.atomic_write(self.paths.legacy_data_root / 'config.json', b'{"preserve":"legacy"}\n', 0o600)
        before = self.paths.config_file.read_bytes(), self.paths.config_file.stat().st_mtime_ns
        self.assertEqual(self.setup(), 0)
        self.assertEqual((self.paths.config_file.read_bytes(), self.paths.config_file.stat().st_mtime_ns), before)

    def test_malformed_manifest_duplicate_or_symlink_refuses_before_writes(self):
        for mutation in ('wrong-id', 'duplicate', 'symlink'):
            with self.subTest(mutation=mutation):
                entry = self.root / 'menu/Menu.qml'
                manifest = self.root / 'manifest.json'
                if mutation == 'wrong-id':
                    value = json.loads(manifest.read_text()); value['id'] = 'wrong'; manifest.write_text(json.dumps(value))
                elif mutation == 'duplicate':
                    (self.root / 'menu/manifest.json').write_text('{}')
                else:
                    entry.unlink(); entry.symlink_to(self.root / 'src/controller.py')
                before = self.snapshot()
                self.assertEqual(self.setup(), 1)
                self.assertEqual(self.snapshot(), before)
                self.assertFalse(self.journal.exists())
                manifest.write_text(json.dumps({'id': PLUGIN_ID, 'kinds': ['overlay'], 'entryPoints': {'overlay': 'menu/Menu.qml'}}))
                (self.root / 'menu/manifest.json').unlink(missing_ok=True)
                if entry.is_symlink():
                    entry.unlink(); entry.write_text('Item {}\n')

    def test_real_setup_requires_confirmation_before_any_writes(self):
        before = self.snapshot()
        env = {'XDG_CONFIG_HOME': str(self.home / '.config'), 'XDG_DATA_HOME': str(self.home / '.local/share'),
               'XDG_STATE_HOME': str(self.home / '.local/state')}
        with patch.object(Path, 'home', return_value=self.home), patch.dict(os.environ, env), \
                patch.object(sys.stdin, 'isatty', return_value=False), patch.object(support, 'require_unlocked') as unlocked:
            self.assertEqual(self.setup(prefix=None), 1)
            unlocked.assert_not_called()
        self.assertEqual(self.snapshot(), before)

    def test_yes_stage_initializes_without_services_and_unlocked_guard_is_before_writes(self):
        env = {'XDG_CONFIG_HOME': str(self.home / '.config'), 'XDG_DATA_HOME': str(self.home / '.local/share'),
               'XDG_STATE_HOME': str(self.home / '.local/state')}
        before = self.snapshot()
        with patch.object(Path, 'home', return_value=self.home), patch.dict(os.environ, env), \
                patch.object(support, 'require_unlocked', side_effect=RuntimeError('locked')):
            self.assertEqual(self.setup(prefix=None, yes=True), 1)
        self.assertEqual(self.snapshot(), before)
        with patch.object(Path, 'home', return_value=self.home), patch.dict(os.environ, env), \
                patch.object(support, 'require_unlocked'):
            self.assertEqual(self.setup(prefix=None, yes=True), 0)
        self.ipc.assert_not_called()

    def test_upgrade_checkpoint_refusal_precedes_writes_and_activation(self):
        self.assertEqual(self.setup(), 0)
        for p in (self.paths.engine, self.paths.viewer):
            support.atomic_write(p, b'binary fixture', 0o755)
        before = self.snapshot()
        env = {'XDG_CONFIG_HOME': str(self.home / '.config'), 'XDG_DATA_HOME': str(self.home / '.local/share'),
               'XDG_STATE_HOME': str(self.home / '.local/state')}
        with patch.object(Path, 'home', return_value=self.home), patch.dict(os.environ, env), \
                patch.object(support, 'require_unlocked'), \
                patch.object(support, 'require_upgrade_checkpoint', side_effect=RuntimeError('takeover')):
            self.assertEqual(self.setup(prefix=None, yes=True, no_activate=False), 1)
        self.assertEqual(self.snapshot(), before)
        self.ipc.assert_not_called()

    def test_uninstall_keeps_theme_games_edits_and_checkout(self):
        self.assertEqual(self.setup(), 0)
        assets = [self.home / '.config/omarchy/themes/phobos/colors.toml', self.paths.games_root / 'save/etersav7.dsg',
                  self.paths.default_iwad, self.root / 'menu/Menu.qml']
        for p in assets[:-1]:
            support.atomic_write(p, b'USER_ASSET_KEEP', 0o600)
        original = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in assets}
        self.paths.config_file.write_bytes(b'{"user":"later-choice"}\n')
        self.assertEqual(self.remove(), 0)
        self.assertEqual({p: (p.read_bytes(), p.stat().st_mtime_ns) for p in assets}, original)
        self.assertTrue(self.journal.exists())
        self.assertEqual(self.paths.config_file.read_bytes(), b'{"user":"later-choice"}\n')
        self.assertFalse(self.service.exists())
        self.assertEqual(support.load_json(self.home / support.SHELL_REL)['plugins'], self.config['plugins'])

    def test_uninstall_original_service_and_menu_registry_settings_are_restored(self):
        support.atomic_write(self.service, b'ORIGINAL_SERVICE\n', 0o644)
        self.config['plugins'].append({'id': PLUGIN_ID, 'custom': True})
        self.config['disabledPlugins'] = [PLUGIN_ID, 'other.plugin']
        support.atomic_write(self.home / support.SHELL_REL, support.json_bytes(self.config))
        self.assertEqual(self.setup(), 0)
        self.assertEqual(self.remove(), 0)
        self.assertEqual(self.service.read_bytes(), b'ORIGINAL_SERVICE\n')
        self.assertEqual(support.load_json(self.home / support.SHELL_REL), self.config)

    def test_uninstall_dry_run_is_read_only_and_clean_repeat_is_idempotent(self):
        self.assertEqual(self.setup(), 0)
        before = self.snapshot()
        self.assertEqual(self.remove(dry_run=True), 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.remove(), 0)
        self.assertFalse(self.journal.exists())
        before = self.snapshot()
        self.assertEqual(self.remove(), 0)
        self.assertEqual(self.snapshot(), before)

    def test_marketplace_activation_enables_one_plugin_without_shell_restart(self):
        calls = []
        with patch.object(support, 'run', side_effect=lambda args, **kw: calls.append(args) or SimpleNamespace(returncode=0, stdout='')), \
                patch.object(support, 'require_unlocked'), patch.object(support, 'wait_for_plugin'), \
                patch.object(support, 'require_upgrade_checkpoint', return_value=True):
            support.activate_marketplace(self.root, upgrade=True, show=True)
        self.assertIn(['omarchy', 'plugin', 'enable', PLUGIN_ID], calls)
        self.assertIn(['systemctl', '--user', 'restart', support.SERVICE_NAME], calls)
        self.assertIn([str(self.root / 'scripts/doomctl'), 'open-menu', '--origin=onboarding'], calls)
        self.assertNotIn(['omarchy', 'restart', 'shell'], calls)
        self.assertFalse(any(support.LEGACY_IDLE_ID in c or support.LEGACY_MENU_ID in c for c in calls))

    def test_legacy_menu_migration_keeps_idle_until_uninstall_and_never_removes_theme(self):
        # The IDs come from local journal provenance, not published defaults.
        idle_id, menu_id = 'fixture-v1.idle', 'fixture-v1.menu'
        idle = f'.config/omarchy/plugins/{idle_id}/Service.qml'
        menu = f'.config/omarchy/plugins/{menu_id}/manifest.json'
        art = '.config/omarchy/themes/phobos/backgrounds/1-live-doom.webp'
        files = {idle: (b'KEEP_EXISTING_IDLE\n', 0o644), menu: (b'{}\n', 0o644), art: (b'PHOBOS_OWNED_BY_THEME\n', 0o644)}
        records = support.journal_files(self.home, files)
        for rel, (data, mode) in files.items():
            support.atomic_write(self.home / rel, data, mode)
        legacy = {'schema': 1, 'plugin_id': idle_id, 'files': records,
                  'config': {'previous_clones': [], 'source_explicit_entries': [], 'source_disabled': False,
                             'own_disabled': False, 'own_restore': False,
                             'menu': {'added_entry': True, 'cleared_disabled': False}}}
        support.atomic_write(self.home / support.JOURNAL_REL, support.json_bytes(legacy), 0o600)
        self.config['plugins'] += [{'id': idle_id}, {'id': menu_id}]
        self.config['disabledPlugins'] = ['omarchy.idle']
        self.config['cloneSourceRestores'] = [idle_id]
        support.atomic_write(self.home / support.SHELL_REL, support.json_bytes(self.config))
        before = ((self.home / idle).read_bytes(), (self.home / idle).stat().st_mtime_ns)
        art_before = ((self.home / art).read_bytes(), (self.home / art).stat().st_mtime_ns)
        self.assertEqual(self.setup(), 0)
        current = support.load_json(self.home / support.SHELL_REL)
        self.assertIn({'id': idle_id}, current['plugins'])
        self.assertNotIn({'id': menu_id}, current['plugins'])
        self.assertIn(menu_id, current['disabledPlugins'])
        self.assertEqual(((self.home / idle).read_bytes(), (self.home / idle).stat().st_mtime_ns), before)
        self.assertEqual(current['idle'], self.config['idle'])
        self.assertNotIn(art, support.load_json(self.journal)['files'])
        self.assertEqual(self.remove(), 0)
        current = support.load_json(self.home / support.SHELL_REL)
        self.assertNotIn({'id': idle_id}, current['plugins'])
        self.assertNotIn('omarchy.idle', current.get('disabledPlugins', []))
        self.assertEqual(current['idle'], self.config['idle'])
        self.assertEqual(((self.home / art).read_bytes(), (self.home / art).stat().st_mtime_ns), art_before)

    def test_example_agent_original_and_later_edits_are_preserved(self):
        (self.root / 'sdk/python').mkdir(parents=True)
        (self.root / 'sdk/python/doomagent.py').write_bytes(b'# SDK fixture\n')
        (self.root / 'examples/agents/wander').mkdir(parents=True)
        (self.root / 'examples/agents/wander/agent.json').write_text('{}\n')
        (self.root / 'examples/agents/wander/wander.py').write_bytes(b'# example fixture\n')
        agent = self.paths.agent_config_root / 'example-wander'
        original = agent / 'wander.py'
        support.atomic_write(original, b'# USER ORIGINAL\n', 0o644)
        self.paths.config_root.chmod(0o700)
        self.assertEqual(self.setup(), 0)
        (agent / 'doomagent.py').write_bytes(b'# LATER USER EDIT\n')
        edited = (agent / 'doomagent.py').read_bytes(), (agent / 'doomagent.py').stat().st_mtime_ns
        before = support.load_json(self.journal)['files']
        self.assertEqual(self.setup(), 0)
        self.assertEqual(((agent / 'doomagent.py').read_bytes(), (agent / 'doomagent.py').stat().st_mtime_ns), edited)
        sdk_rel = str((agent / 'doomagent.py').relative_to(self.home))
        self.assertEqual(support.load_json(self.journal)['files'][sdk_rel], before[sdk_rel])
        self.assertEqual(self.remove(), 0)
        self.assertEqual(original.read_bytes(), b'# USER ORIGINAL\n')
        self.assertEqual((agent / 'doomagent.py').read_bytes(), b'# LATER USER EDIT\n')
        self.assertTrue(self.journal.exists())

    def test_modified_example_skip_does_not_relax_other_owned_files_and_explicit_replace_works(self):
        (self.root / 'sdk/python').mkdir(parents=True)
        (self.root / 'sdk/python/doomagent.py').write_bytes(b'# SDK fixture\n')
        (self.root / 'examples/agents/wander').mkdir(parents=True)
        (self.root / 'examples/agents/wander/agent.json').write_text('{}\n')
        (self.root / 'examples/agents/wander/wander.py').write_bytes(b'# original upstream\n')
        self.assertEqual(self.setup(), 0)
        script = self.paths.agent_config_root / 'example-wander/wander.py'
        script.write_bytes(b'# USER CUSTOM AGENT\n')
        script.chmod(0o600)
        self.service.write_bytes(b'USER MODIFIED SERVICE\n')
        journal = self.journal.read_bytes()
        self.assertEqual(self.setup(), 1)
        self.assertEqual(script.read_bytes(), b'# USER CUSTOM AGENT\n')
        self.assertEqual(self.service.read_bytes(), b'USER MODIFIED SERVICE\n')
        self.assertEqual(self.journal.read_bytes(), journal)
        self.assertEqual(self.setup(replace_modified=True), 0)
        self.assertEqual(script.read_bytes(), b'# original upstream\n')
        self.assertEqual(script.stat().st_mode & 0o777, 0o644)
        self.assertNotEqual(self.service.read_bytes(), b'USER MODIFIED SERVICE\n')

    def test_no_journal_uninstall_and_dev_compatibility_option_make_no_changes(self):
        before = self.snapshot()
        for dev in (False, True):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(support.marketplace_uninstall(self.args(dev=dev)), 0)
            self.assertIn('No Live Doom installation journal was found; nothing changed.', output.getvalue())
            self.assertNotIn('legacy', output.getvalue())
            self.assertEqual(self.snapshot(), before)

    def test_owned_onboarding_dismissed_marker_and_lock_are_removed_but_user_extension_is_kept(self):
        self.assertEqual(self.setup(), 0)
        marker = self.paths.data_root / 'onboarding.json'
        import onboarding
        onboarding.set_progress(self.paths.data_root, 'summary', background='skip', screensaver=True)
        onboarding.complete(self.paths.data_root)
        self.assertEqual(self.remove(), 0)
        self.assertFalse(marker.exists())
        self.assertFalse((self.paths.data_root / '.onboarding.lock').exists())
        self.assertFalse(self.journal.exists())
        self.assertEqual(self.setup(), 0)
        value = json.loads(marker.read_text()); value['user-note'] = 'retain this edit'
        support.atomic_write(marker, support.json_bytes(value), 0o600)
        self.assertEqual(self.remove(), 0)
        self.assertEqual(json.loads(marker.read_text()), value)
        self.assertTrue(self.journal.exists())

    def test_owned_legacy_and_resumable_guide_markers_are_removed(self):
        import onboarding
        for legacy in (True, False):
            with self.subTest(legacy=legacy):
                self.assertEqual(self.setup(), 0)
                marker = self.paths.data_root / 'onboarding.json'
                if legacy:
                    value = {'schema': 1, 'show': False, 'fresh': True, 'selected_package': None}
                    support.atomic_write(marker, support.json_bytes(value), 0o600)
                else:
                    onboarding.set_progress(self.paths.data_root, 'screensaver', background='phobos')
                self.assertEqual(self.remove(), 0)
                self.assertFalse(marker.exists())
                self.assertFalse((self.paths.data_root / '.onboarding.lock').exists())
                self.assertFalse(self.journal.exists())

    def test_runtime_cleanup_removes_known_stopped_artifacts_and_preserves_unknown_and_symlinks(self):
        runtime = self.home / 'private-runtime'
        runtime.mkdir(mode=0o700)
        paths = ProjectPaths.from_env(home=self.home, env={'LIVE_DOOM_RUNTIME': str(runtime)})
        for name in ('daemon.lock', 'control.sock', 'engine.sock', 'frame.bin', 'campaign-info.wad', 'user-note'):
            (runtime / name).write_bytes(b'PRIVATE_RUNTIME_FIXTURE')
        outside = self.home / 'user-image'
        outside.write_bytes(b'PRESERVE')
        (runtime / 'frame.bin').unlink(); (runtime / 'frame.bin').symlink_to(outside)
        notes = support.cleanup_owned_runtime(paths, {'files': {}}, self.home)
        self.assertTrue(notes)
        self.assertFalse((runtime / 'control.sock').exists())
        self.assertTrue((runtime / 'frame.bin').is_symlink())
        self.assertEqual(outside.read_bytes(), b'PRESERVE')
        self.assertTrue((runtime / 'user-note').exists())

    def test_runtime_used_by_another_daemon_is_retained(self):
        import fcntl
        runtime = self.home / 'private-runtime'
        runtime.mkdir(mode=0o700)
        paths = ProjectPaths.from_env(home=self.home, env={'LIVE_DOOM_RUNTIME': str(runtime)})
        lock = (runtime / 'daemon.lock').open('w')
        self.addCleanup(lock.close)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        (runtime / 'frame.bin').write_bytes(b'IN_USE')
        self.assertTrue(support.cleanup_owned_runtime(paths, {'files': {}}, self.home))
        self.assertEqual((runtime / 'frame.bin').read_bytes(), b'IN_USE')

    def test_initializer_finishes_before_activation_and_only_first_setup_summons(self):
        for p in (self.paths.engine, self.paths.viewer):
            support.atomic_write(p, b'binary fixture', 0o755)
        self.paths.data_root.chmod(0o700)
        events = []
        initializer = self.initialize.side_effect
        def initialize(*args):
            events.append('initialize')
            return initializer(*args)
        def activate(root, **options):
            self.assertTrue(self.paths.config_file.exists())
            self.assertTrue((self.paths.data_root / 'onboarding.json').exists())
            events.append(('activate', options['show']))
        self.initialize.side_effect = initialize
        env = {'XDG_CONFIG_HOME': str(self.home / '.config'), 'XDG_DATA_HOME': str(self.home / '.local/share'),
               'XDG_STATE_HOME': str(self.home / '.local/state')}
        with patch.object(Path, 'home', return_value=self.home), patch.dict(os.environ, env), \
                patch.object(support, 'require_unlocked'), patch.object(support, 'require_upgrade_checkpoint'), \
                patch.object(support, 'activate_marketplace', side_effect=activate), \
                patch.object(support, 'run', return_value=SimpleNamespace(returncode=1)):
            self.assertEqual(self.setup(prefix=None, yes=True, no_activate=False), 0)
            self.assertEqual(self.setup(prefix=None, yes=True, no_activate=False), 0)
        self.assertEqual(events, ['initialize', ('activate', True), 'initialize', ('activate', False)])

    def test_first_confirmed_setup_with_existing_choice_still_shows_welcome_once(self):
        self.paths.config_root.mkdir(mode=0o700)
        support.atomic_write(self.paths.config_file, b'{"preserve":"existing-choice"}\n', 0o600)
        for p in (self.paths.engine, self.paths.viewer):
            support.atomic_write(p, b'binary fixture', 0o755)
        self.paths.data_root.chmod(0o700)
        env = {'XDG_CONFIG_HOME': str(self.home / '.config'), 'XDG_DATA_HOME': str(self.home / '.local/share'),
               'XDG_STATE_HOME': str(self.home / '.local/state')}
        with patch.object(Path, 'home', return_value=self.home), patch.dict(os.environ, env), \
                patch.object(support, 'require_unlocked'), patch.object(support, 'activate_marketplace') as activate, \
                patch.object(support, 'run', return_value=SimpleNamespace(returncode=1)):
            self.assertEqual(self.setup(prefix=None, yes=True, no_activate=False), 0)
        self.assertTrue(activate.call_args.kwargs['show'])
        self.assertEqual(self.paths.config_file.read_bytes(), b'{"preserve":"existing-choice"}\n')

    def test_interactive_cancel_is_read_only(self):
        before = self.snapshot()
        env = {'XDG_CONFIG_HOME': str(self.home / '.config'), 'XDG_DATA_HOME': str(self.home / '.local/share'),
               'XDG_STATE_HOME': str(self.home / '.local/state')}
        with patch.object(Path, 'home', return_value=self.home), patch.dict(os.environ, env), \
                patch.object(sys.stdin, 'isatty', return_value=True), patch('builtins.input', return_value='n'):
            self.assertEqual(self.setup(prefix=None), 1)
        self.assertEqual(self.snapshot(), before)

    def test_default_cli_dispatches_marketplace_and_accepts_explicit_yes(self):
        with patch.object(install, 'marketplace_install', return_value=0) as setup:
            self.assertEqual(install.main(['--prefix', str(self.home), '--yes']), 0)
        args, root = setup.call_args.args
        self.assertFalse(args.dev)
        self.assertTrue(args.yes)
        self.assertEqual(args.prefix, self.home)

    def test_dev_versions_complete_relative_runtime_without_duplicate_plugin_or_theme(self):
        source = self.home.parent / 'developer checkout'
        shutil.copytree(self.root, source)
        self.root = source
        self.snapshot_inputs(source)
        (source / 'theme/backgrounds').mkdir(parents=True)
        (source / 'theme/backgrounds/unused.webp').write_bytes(b'NOT_A_RUNTIME_ASSET')
        (source / 'service').mkdir()
        (source / 'service/Service.qml').write_text('Item {}\n')
        manifest = support.load_json(source / 'manifest.json')
        manifest['kinds'].append('service')
        manifest['entryPoints']['service'] = 'service/Service.qml'
        (source / 'manifest.json').write_text(json.dumps(manifest))
        (source / 'menu/components').mkdir()
        card = source / 'menu/components/MenuCard.qml'
        card.write_text('Item { property int version: 1 }\n')
        (source / 'src/__pycache__').mkdir()
        (source / 'src/__pycache__/ignored.pyc').write_bytes(b'GENERATED_CACHE')
        self.assertEqual(self.setup(dev=True), 0)
        plugin = self.home / '.config/omarchy/plugins' / PLUGIN_ID
        first = support.load_json(plugin / 'manifest.json')
        first_impl = plugin / Path(first['entryPoints']['overlay']).parts[0]
        self.assertEqual(first['entryPoints']['service'], first_impl.name + '/service/Service.qml')
        self.assertTrue((first_impl / 'menu/Menu.qml').exists())
        self.assertTrue((first_impl / 'scripts/doomctl').exists())
        self.assertTrue((first_impl / 'src/controller.py').exists())
        self.assertFalse((first_impl / 'src/__pycache__').exists())
        self.assertFalse((first_impl / 'backend.json').exists())
        self.assertFalse((first_impl / 'theme').exists())
        self.assertIn(str(first_impl / 'src/controller.py'), self.service.read_text())
        self.assertFalse((self.home / '.config/omarchy/themes').exists())
        self.assertFalse((self.home / '.config/omarchy/plugins' / support.LEGACY_MENU_ID).exists())
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in first_impl.rglob('*') if p.is_file()}
        self.assertEqual(self.setup(dev=True), 0)
        self.assertEqual({p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before}, before)
        card.write_text('Item { property int version: 2 }\n')
        self.assertEqual(self.setup(dev=True), 0)
        second = support.load_json(plugin / 'manifest.json')
        self.assertNotEqual(first['entryPoints']['overlay'], second['entryPoints']['overlay'])
        self.assertEqual({p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before}, before)
        (first_impl / 'menu/Menu.qml').write_text('Item { property bool userEdit: true }\n')
        self.assertEqual(self.remove(), 0)
        self.assertTrue((first_impl / 'menu/Menu.qml').exists())
        second_impl = plugin / Path(second['entryPoints']['overlay']).parts[0]
        self.assertFalse(second_impl.exists())
        self.assertTrue(self.journal.exists())

    def test_dev_staged_onboarding_terms_and_live_still_work_and_removal_preserves_selection(self):
        source = self.home.parent / 'developer runtime'
        shutil.copytree(self.root, source)
        self.root = source
        self.snapshot_inputs(source)
        shutil.copytree(ROOT / 'src', source / 'src', dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo'))
        self.assertEqual(self.setup(dev=True), 0)
        plugin = self.home / '.config/omarchy/plugins' / PLUGIN_ID
        manifest = support.load_json(plugin / 'manifest.json')
        implementation = plugin / Path(manifest['entryPoints']['overlay']).parts[0]
        current = self.home / '.local/state/omarchy/current'
        (current / 'theme/backgrounds').mkdir(parents=True)
        (current / 'theme.name').write_text('independent\n')
        static = current / 'theme/backgrounds/static.webp'
        static.write_bytes(b'USER_STATIC_IMAGE')
        (current / 'background').symlink_to(static)
        runtime = self.home / 'private-runtime'
        runtime.mkdir(mode=0o700)
        program = '''
import json, sys
from pathlib import Path
import onboarding
from live_wallpaper import use_live_wallpaper
home, data, runtime, fallback = map(Path, sys.argv[1:])
def select(image):
    link = home / '.local/state/omarchy/current/background'
    link.unlink()
    link.symlink_to(image)
selected = use_live_wallpaper(home, fallback, select, runtime)
print(json.dumps({'selected': str(selected), 'onboarding': onboarding.settings(data, home=home)}))
'''
        env = os.environ | {'PYTHONPATH': str(implementation / 'src'), 'PYTHONDONTWRITEBYTECODE': '1'}
        result = subprocess.run([sys.executable, '-c', program, str(self.home), str(self.paths.data_root),
                                 str(runtime), str(implementation / 'assets/live-doom.webp')],
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        selected = Path(value['selected'])
        self.assertEqual(selected.read_bytes(), (ROOT / 'assets/live-doom.webp').read_bytes())
        terms = value['onboarding']['shareware_terms']['text']
        for name in ('Doom-shareware.txt', 'Doom-shareware-1.8.txt'):
            self.assertIn((ROOT / 'docs/licenses' / name).read_text(), terms)
        for name in ('LICENSE', 'LICENSES/0BSD.txt', 'LICENSES/CC0-1.0.txt',
                     'LICENSES/GPL-3.0-or-later.txt', 'THIRD-PARTY-NOTICES.md',
                     'patches/autodoom-desktop.patch', 'patches/sdl-mixer-device.patch',
                     'scripts/build-pins.json'):
            self.assertEqual((implementation / name).read_bytes(), (ROOT / name).read_bytes())
        self.assertEqual(value['onboarding']['theme']['active'], False)
        self.assertEqual((current / 'theme.name').read_text(), 'independent\n')
        self.assertFalse((implementation / 'theme').exists())
        self.assertEqual(self.remove(), 0)
        self.assertFalse(implementation.exists())
        self.assertEqual((current / 'background').resolve(), selected)
        self.assertTrue(selected.is_file())
        self.assertEqual(static.read_bytes(), b'USER_STATIC_IMAGE')
        self.assertTrue((self.paths.state_root / 'live-backgrounds/ownership.json').is_file())

    def test_dev_missing_runtime_still_refuses_before_prefix_writes(self):
        self.snapshot_inputs(self.root)
        (self.root / 'assets/live-doom.webp').unlink()
        before = self.snapshot()
        self.assertEqual(self.setup(dev=True), 1)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.journal.exists())

    def test_dev_missing_mixer_patch_refuses_before_prefix_writes(self):
        self.snapshot_inputs(self.root)
        (self.root / 'patches/sdl-mixer-device.patch').unlink()
        before = self.snapshot()
        self.assertEqual(self.setup(dev=True), 1)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.journal.exists())

    def test_oversized_existing_owned_artifact_refuses_before_staging(self):
        self.service.parent.mkdir(parents=True)
        with self.service.open('wb') as output:
            output.truncate(support.MAX_ARTIFACT_BYTES + 1)
        before = self.service.stat()
        shell = (self.home / support.SHELL_REL).read_bytes()
        self.assertEqual(self.setup(), 1)
        self.assertEqual(self.service.stat().st_size, before.st_size)
        self.assertEqual(self.service.stat().st_mtime_ns, before.st_mtime_ns)
        self.assertEqual((self.home / support.SHELL_REL).read_bytes(), shell)
        self.assertFalse(self.journal.exists())

    def test_activated_removal_stops_owned_unit_before_runtime_cleanup_and_restores_original_state(self):
        support.atomic_write(self.service, b'ORIGINAL_USER_SERVICE\n', 0o644)
        self.assertEqual(self.setup(), 0)
        journal = support.load_json(self.journal)
        journal['runtime'] = {'service_enabled': True, 'service_active': True}
        support.atomic_write(self.journal, support.json_bytes(journal), 0o600)
        runtime = self.home / 'owned-runtime'
        runtime.mkdir(mode=0o700)
        (runtime / 'daemon.lock').write_bytes(b'')
        (runtime / 'frame.bin').write_bytes(b'OWNED_FRAME')
        calls = []
        def command(args, **kw):
            if args == ['systemctl', '--user', 'disable', '--now', support.SERVICE_NAME]:
                self.assertTrue((runtime / 'frame.bin').exists())
            if args == ['systemctl', '--user', 'start', support.SERVICE_NAME]:
                self.assertEqual(self.service.read_bytes(), b'ORIGINAL_USER_SERVICE\n')
            calls.append(args)
            return SimpleNamespace(returncode=0, stdout='')
        env = {'XDG_CONFIG_HOME': str(self.home / '.config'), 'XDG_DATA_HOME': str(self.home / '.local/share'),
               'XDG_STATE_HOME': str(self.home / '.local/state'), 'LIVE_DOOM_RUNTIME': str(runtime)}
        with patch.object(Path, 'home', return_value=self.home), patch.dict(os.environ, env), \
                patch.object(support, 'require_unlocked'), patch.object(support, 'run', side_effect=command):
            self.assertEqual(self.remove(prefix=None, yes=True, no_activate=False), 0)
        self.assertEqual(calls[0], ['systemctl', '--user', 'disable', '--now', support.SERVICE_NAME])
        self.assertFalse((runtime / 'frame.bin').exists())
        self.assertIn(['systemctl', '--user', 'enable', support.SERVICE_NAME], calls)
        self.assertIn(['systemctl', '--user', 'start', support.SERVICE_NAME], calls)
        self.assertNotIn(['omarchy', 'restart', 'shell'], calls)

    def test_modified_unit_is_never_stopped_and_its_runtime_is_preserved(self):
        self.assertEqual(self.setup(), 0)
        self.service.write_bytes(b'REPLACED_USER_SERVICE\n')
        runtime = self.home / 'owned-runtime'
        runtime.mkdir(mode=0o700)
        (runtime / 'frame.bin').write_bytes(b'KEEP_REPLACED_SERVICE_RUNTIME')
        calls = []
        env = {'XDG_CONFIG_HOME': str(self.home / '.config'), 'XDG_DATA_HOME': str(self.home / '.local/share'),
               'XDG_STATE_HOME': str(self.home / '.local/state'), 'LIVE_DOOM_RUNTIME': str(runtime)}
        with patch.object(Path, 'home', return_value=self.home), patch.dict(os.environ, env), \
                patch.object(support, 'require_unlocked'), \
                patch.object(support, 'run', side_effect=lambda args, **kw: calls.append(args) or SimpleNamespace(returncode=0)):
            self.assertEqual(self.remove(prefix=None, yes=True, no_activate=False), 0)
        self.assertFalse(any('disable' in args or 'stop' in args for args in calls))
        self.assertEqual(self.service.read_bytes(), b'REPLACED_USER_SERVICE\n')
        self.assertEqual((runtime / 'frame.bin').read_bytes(), b'KEEP_REPLACED_SERVICE_RUNTIME')
        self.assertTrue(self.journal.exists())


class BoundedInstallerIOTests(unittest.TestCase):
    def test_run_resolves_fixed_path_and_uses_closed_environment(self):
        completed = subprocess.CompletedProcess([], 0, 'ok', '')
        with patch.dict(os.environ, {'PATH': '/untrusted', 'PYTHONPATH': '/untrusted',
                                     'LD_PRELOAD': '/untrusted'}), \
                patch.object(support, 'bounded_run', return_value=completed) as process:
            self.assertIs(support.run(['true']), completed)
            self.assertIn(process.call_args.args[0][0], ('/usr/bin/true', '/bin/true'))
            self.assertEqual(process.call_args.kwargs['max_output_bytes'], 1024 * 1024)
            with self.assertRaises(RuntimeError):
                support.run(['relative/command'])
        with patch.dict(os.environ, {'PATH': '/untrusted', 'PYTHONPATH': '/untrusted',
                                     'LD_PRELOAD': '/untrusted'}):
            result = support.run(['/usr/bin/python3', '-c',
                "import os,json; print(json.dumps({k:os.environ.get(k) for k in ['PATH','PYTHONPATH','LD_PRELOAD','PYTHONDONTWRITEBYTECODE']}))"])
        self.assertEqual(json.loads(result.stdout), {'PATH': '/usr/bin:/bin', 'PYTHONPATH': None,
                                                   'LD_PRELOAD': None, 'PYTHONDONTWRITEBYTECODE': '1'})

    def test_process_and_initializer_output_caps(self):
        with self.assertRaisesRegex(RuntimeError, 'byte limit'):
            support.run(['/usr/bin/python3', '-c', "print('x'*8192)"], max_output_bytes=4096)
        with tempfile.TemporaryDirectory(prefix='live-doom-output-cap-') as directory:
            home = Path(directory) / 'home'
            home.mkdir()
            source = Path(directory) / 'source'
            (source / 'src').mkdir(parents=True)
            (source / 'src/onboarding.py').write_text("print('x'*8192)\n")
            paths = ProjectPaths.from_env(home=home)
            with self.assertRaisesRegex(ValueError, 'byte limit'):
                support.initialize_marketplace(source, home, paths)
            self.assertFalse(paths.config_file.exists())
            self.assertFalse(paths.catalog_file.exists())

    def test_journal_refuses_nonregular_and_symlink_backups_without_unbounded_reads(self):
        with tempfile.TemporaryDirectory(prefix='live-doom-backup-cap-') as directory:
            home = Path(directory)
            target = home / 'artifact'
            outside = home / 'unrelated'
            outside.write_bytes(b'PRESERVE_USER_BYTES')
            for kind in ('directory', 'fifo', 'symlink'):
                with self.subTest(kind=kind):
                    if kind == 'directory':
                        target.mkdir()
                    elif kind == 'fifo':
                        os.mkfifo(target)
                    else:
                        target.symlink_to(outside)
                    before = target.lstat()
                    with patch.object(Path, 'read_bytes', side_effect=AssertionError('Unbounded backup read')), \
                            self.assertRaises((OSError, ValueError, RuntimeError)):
                        support.journal_files(home, {'artifact': (b'REPLACEMENT', 0o644)})
                    self.assertEqual(target.lstat().st_ino, before.st_ino)
                    self.assertEqual(target.lstat().st_mtime_ns, before.st_mtime_ns)
                    self.assertEqual(outside.read_bytes(), b'PRESERVE_USER_BYTES')
                    target.rmdir() if kind == 'directory' else target.unlink()

    def test_real_theme_lock_requires_existing_owned_private_runtime(self):
        with tempfile.TemporaryDirectory(prefix='live-doom-theme-lock-') as directory:
            home = Path(directory) / 'home'
            home.mkdir()
            runtime = Path(directory) / 'runtime'
            with patch.object(Path, 'home', return_value=home), patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(RuntimeError, 'XDG_RUNTIME_DIR'):
                    with support.theme_background_lock(home):
                        self.fail('Missing runtime accepted')
            with patch.object(Path, 'home', return_value=home), \
                    patch.dict(os.environ, {'XDG_RUNTIME_DIR': str(runtime)}):
                with self.assertRaises(OSError):
                    with support.theme_background_lock(home):
                        self.fail('Absent runtime accepted')
                self.assertFalse(runtime.exists())
                runtime.mkdir(mode=0o755)
                with self.assertRaises(PermissionError):
                    with support.theme_background_lock(home):
                        self.fail('Non-private runtime accepted')
                runtime.chmod(0o700)
                with support.theme_background_lock(home):
                    self.assertEqual((runtime / 'omarchy-theme-set.lock').stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main(verbosity=2)
