#!/usr/bin/env python3
# SPDX-License-Identifier: 0BSD
"""Live picker opt-in with private filesystem fixtures, never desktop IPC."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from live_wallpaper import live_wallpaper_metadata, remove_live_wallpaper, use_live_wallpaper


class LiveWallpaperTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="doom-live-choice-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.home = self.base / "home with spaces"
        self.runtime = self.base / "private runtime"
        self.runtime.mkdir()
        self.current = self.home / ".local/state/omarchy/current"
        self.backgrounds = self.current / "theme/backgrounds"
        self.backgrounds.mkdir(parents=True)
        self.theme_name = self.current / "theme.name"
        self.theme_name.write_bytes(b"tokyo-night\n")
        self.palette = self.current / "theme/colors.toml"
        self.palette.write_bytes(b"preserve this active palette\n")
        self.static = self.backgrounds / "0-personal-static.webp"
        self.static.write_bytes(b"preserve selected static image")
        self.link = self.current / "background"
        self.link.symlink_to(self.static)
        self.settings = self.home / ".local/share/doom-desktop/config.json"
        self.settings.parent.mkdir(parents=True)
        self.settings.write_bytes(b'{"wallpaper_enabled":false,"screensaver_enabled":false}\n')
        self.fallback = self.base / "bundled-live-doom.webp"
        self.fallback.write_bytes(b"bundled fallback fixture")
        self.selections = []

    def personal(self):
        return self.home / ".config/omarchy/backgrounds/tokyo-night"

    def select(self, path):
        self.selections.append(path)
        temporary = self.link.with_name("next-background")
        temporary.symlink_to(path)
        temporary.replace(self.link)

    def preserved(self, selection=True):
        paths = [self.theme_name, self.palette, self.static, self.settings]
        if selection:
            paths.append(self.link)
        return {path: (path.lstat().st_mtime_ns,
                       os.readlink(path) if path.is_symlink() else path.read_bytes()) for path in paths}

    def assert_preserved(self, before):
        for path, expected in before.items():
            actual = (path.lstat().st_mtime_ns,
                      os.readlink(path) if path.is_symlink() else path.read_bytes())
            self.assertEqual(actual, expected, str(path))

    def test_existing_current_theme_marker_reused_without_fallback_or_duplicates(self):
        live = self.backgrounds / "2-LIVE-DOOM.png"
        live.write_bytes(b"existing themed live image")
        before = self.preserved(selection=False)
        image_before = (live.read_bytes(), live.stat().st_mtime_ns)
        self.fallback.unlink()
        for _ in range(2):
            self.assertEqual(use_live_wallpaper(self.home, self.fallback, self.select, self.runtime), live.resolve())
        self.assertEqual(self.link.resolve(strict=True), live.resolve())
        self.assertEqual(self.selections, [live.resolve()] * 2)
        self.assertEqual((live.read_bytes(), live.stat().st_mtime_ns), image_before)
        self.assertFalse(self.personal().exists())
        self.assert_preserved(before)

    def test_existing_user_image_symlink_reuses_resolved_live_marker(self):
        personal = self.personal()
        personal.mkdir(parents=True)
        image = self.base / "other-images/1-live-doom.jpeg"
        image.parent.mkdir()
        image.write_bytes(b"existing resolved image")
        alias = personal / "friendly-name.jpeg"
        alias.symlink_to(image)
        before = self.preserved(selection=False)
        alias_before = (os.readlink(alias), alias.lstat().st_mtime_ns)
        selected = use_live_wallpaper(self.home, self.fallback, self.select, self.runtime)
        self.assertEqual(selected, image.resolve())
        self.assertEqual(list(personal.iterdir()), [alias])
        self.assertEqual((os.readlink(alias), alias.lstat().st_mtime_ns), alias_before)
        self.assert_preserved(before)

    def test_arbitrary_theme_gets_user_choice_without_palette_theme_or_settings_changes(self):
        before = self.preserved(selection=False)
        selected = use_live_wallpaper(self.home, self.fallback, self.select, self.runtime)
        self.assertEqual(selected.parent, self.personal())
        self.assertIn("live-doom", selected.name)
        self.assertEqual(selected.read_bytes(), self.fallback.read_bytes())
        self.assertEqual(selected.stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.link.resolve(strict=True), selected)
        self.assertEqual(list(self.backgrounds.iterdir()), [self.static])
        timestamp = selected.stat().st_mtime_ns
        self.assertEqual(use_live_wallpaper(self.home, self.fallback, self.select, self.runtime), selected)
        self.assertEqual(selected.stat().st_mtime_ns, timestamp)
        self.assertEqual(list(self.personal().iterdir()), [selected])
        self.assert_preserved(before)

    def test_collision_preserves_reserved_user_path_and_uses_content_hash_once(self):
        personal = self.personal()
        reserved = personal / "1-live-doom.webp"
        reserved.mkdir(parents=True)
        user_file = reserved / "personal.txt"
        user_file.write_bytes(b"keep this reserved directory and contents")
        before = self.preserved(selection=False)
        reserved_before = (reserved.stat().st_mtime_ns, user_file.read_bytes(), user_file.stat().st_mtime_ns)
        selected = use_live_wallpaper(self.home, self.fallback, self.select, self.runtime)
        expected = personal / ("1-live-doom-" + hashlib.sha256(self.fallback.read_bytes()).hexdigest() + ".webp")
        self.assertEqual(selected, expected)
        self.assertEqual(selected.read_bytes(), self.fallback.read_bytes())
        self.assertEqual(use_live_wallpaper(self.home, self.fallback, self.select, self.runtime), expected)
        self.assertEqual(set(personal.iterdir()), {reserved, expected})
        self.assertEqual((reserved.stat().st_mtime_ns, user_file.read_bytes(), user_file.stat().st_mtime_ns), reserved_before)
        self.assert_preserved(before)

    def test_full_hash_collision_fails_without_overwriting_either_existing_path(self):
        personal = self.personal()
        reserved = personal / "1-live-doom.webp"
        reserved.mkdir(parents=True)
        hashed = personal / ("1-live-doom-" + hashlib.sha256(self.fallback.read_bytes()).hexdigest() + ".webp")
        hashed.mkdir()
        before = self.preserved()
        select = Mock(side_effect=AssertionError("must not select a conflicting path"))
        with self.assertRaises(RuntimeError):
            use_live_wallpaper(self.home, self.fallback, select, self.runtime)
        self.assertEqual(set(personal.iterdir()), {reserved, hashed})
        select.assert_not_called()
        self.assert_preserved(before)

    def test_selection_callback_failure_keeps_existing_theme_and_selection(self):
        before = self.preserved()
        select = Mock(side_effect=RuntimeError("private picker failure"))
        with self.assertRaisesRegex(RuntimeError, "private picker failure"):
            use_live_wallpaper(self.home, self.fallback, select, self.runtime)
        select.assert_called_once()
        self.assert_preserved(before)

    def test_successful_callback_without_changed_background_is_rejected(self):
        before = self.preserved()
        select = Mock()
        with self.assertRaisesRegex(RuntimeError, "not selected"):
            use_live_wallpaper(self.home, self.fallback, select, self.runtime)
        select.assert_called_once()
        self.assert_preserved(before)

    def test_unsafe_theme_names_fail_before_picker_or_background_creation(self):
        select = Mock()
        for slug in ("", ".", "..", "../other", "nested/theme", "/absolute", "bad\0name"):
            with self.subTest(slug=slug):
                self.theme_name.write_text(slug + "\n")
                before = self.preserved()
                with self.assertRaises((RuntimeError, ValueError, OSError)):
                    use_live_wallpaper(self.home, self.fallback, select, self.runtime)
                self.assertFalse((self.home / ".config/omarchy/backgrounds").exists())
                self.assert_preserved(before)
        select.assert_not_called()

    def assert_symlink_directory_rejected(self, current):
        external = self.base / "external-background-directory"
        external.mkdir()
        live = external / "1-live-doom.webp"
        live.write_bytes(b"do not follow this directory alias")
        if current:
            (self.backgrounds / "0-personal-static.webp").unlink()
            self.backgrounds.rmdir()
            self.backgrounds.symlink_to(external, target_is_directory=True)
            # A separately owned static image keeps the selected image readable.
            self.static = self.base / "personal-static.webp"
            self.static.write_bytes(b"preserve selected static image")
            self.link.unlink()
            self.link.symlink_to(self.static)
        else:
            self.personal().parent.mkdir(parents=True)
            self.personal().symlink_to(external, target_is_directory=True)
        before = self.preserved()
        select = Mock()
        with self.assertRaises(RuntimeError):
            use_live_wallpaper(self.home, self.fallback, select, self.runtime)
        select.assert_not_called()
        self.assertEqual(list(external.iterdir()), [live])
        self.assertEqual(live.read_bytes(), b"do not follow this directory alias")
        self.assert_preserved(before)

    def test_current_background_directory_alias_is_rejected_before_reuse(self):
        self.assert_symlink_directory_rejected(current=True)

    def test_user_background_directory_alias_is_rejected_before_reuse(self):
        self.assert_symlink_directory_rejected(current=False)

    def test_missing_empty_or_symlink_fallback_fails_before_picker_or_directory_creation(self):
        self.fallback.unlink()
        select = Mock()
        for kind in ("missing", "empty", "symlink"):
            with self.subTest(kind=kind):
                if kind == "empty":
                    self.fallback.write_bytes(b"")
                elif kind == "symlink":
                    self.fallback.unlink()
                    self.fallback.symlink_to(self.static)
                before = self.preserved()
                with self.assertRaises(RuntimeError):
                    use_live_wallpaper(self.home, self.fallback, select, self.runtime)
                self.assertFalse(self.personal().exists())
                self.assert_preserved(before)
        select.assert_not_called()

    def test_busy_stock_theme_lock_fails_bounded_before_selection_or_copying(self):
        lock = self.runtime / "omarchy-theme-set.lock"
        with lock.open("w") as owner:
            fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = self.preserved()
            select = Mock()
            with patch("live_wallpaper.time.monotonic", side_effect=[0, 3]):
                with self.assertRaisesRegex(RuntimeError, "theme change"):
                    use_live_wallpaper(self.home, self.fallback, select, self.runtime)
            select.assert_not_called()
            self.assertFalse(self.personal().exists())
            self.assert_preserved(before)

    def ownership(self):
        return self.home / '.local/state/live-doom/live-backgrounds/ownership.json'

    def add_owned(self):
        return use_live_wallpaper(self.home, self.fallback, self.select, self.runtime)

    def metadata(self):
        return live_wallpaper_metadata(self.home, self.fallback)

    def legacy_ledger(self):
        """The former schema, with a different device number after a reboot."""
        ledger = json.loads(self.ownership().read_text())
        def legacy(record):
            info = Path(record['path']).lstat()
            value = {key: data for key, data in record.items() if key not in ('size', 'mtime_ns')}
            value['identity'] = [info.st_dev + 100, info.st_ino, info.st_mtime_ns]
            if isinstance(value.get('prior_static'), dict):
                value['prior_static'] = legacy(value['prior_static'])
            return value
        ledger['schema'] = 1
        ledger['images'] = {name: legacy(record) for name, record in ledger['images'].items()}
        self.ownership().write_text(json.dumps(ledger))
        return ledger

    def assert_stable_record(self, record):
        self.assertNotIn('identity', record)
        self.assertNotIn('st_dev', record)
        self.assertNotIn('st_ino', record)
        self.assertNotIn('st_ctime_ns', record)
        for key in ('path', 'sha256', 'size', 'mode', 'mtime_ns'):
            self.assertIn(key, record)
        if isinstance(record.get('prior_static'), dict):
            self.assert_stable_record(record['prior_static'])

    def test_new_live_static_and_recovery_records_have_boot_stable_fingerprints(self):
        owned = self.add_owned()
        ledger = json.loads(self.ownership().read_text())
        self.assertEqual(ledger['schema'], 2)
        self.assert_stable_record(ledger['images'][str(owned)])
        self.assertEqual(ledger['images'][str(owned)]['size'], owned.stat().st_size)
        remove_live_wallpaper(self.home, self.fallback, self.select, self.runtime)
        ledger = json.loads(self.ownership().read_text())
        self.assert_stable_record(ledger['recovered'][-1])

    def test_current_device_change_keeps_modern_choice_removable(self):
        owned = self.add_owned()
        saved = self.ownership().read_bytes()
        real_lstat = Path.lstat
        def changed_device(path, *args, **kwargs):
            info = real_lstat(path, *args, **kwargs)
            if path not in (owned, self.static):
                return info
            fields = {key: getattr(info, key) for key in
                      ('st_dev', 'st_ino', 'st_mtime_ns', 'st_ctime_ns', 'st_mode', 'st_size', 'st_nlink')}
            fields['st_dev'] += 100
            return SimpleNamespace(**fields)
        with patch.object(Path, 'lstat', changed_device):
            self.assertTrue(self.metadata()['can_remove_live'])
        self.assertEqual(self.ownership().read_bytes(), saved)

    def test_legacy_device_change_keeps_active_and_inactive_removal_working(self):
        for active in (True, False):
            with self.subTest(active=active):
                owned = self.add_owned()
                self.legacy_ledger()
                other = self.backgrounds / '00-earlier-sorted.png'
                other.write_bytes(b'keep this earlier static choice')
                if not active:
                    self.select(self.static)
                before = self.preserved(selection=not active)
                saved = self.ownership().read_bytes()
                self.assertTrue(self.metadata()['can_remove_live'])
                self.assertEqual(self.ownership().read_bytes(), saved, 'Metadata must stay read-only')
                picker = self.select if active else Mock(side_effect=AssertionError('No picker while inactive'))
                result = remove_live_wallpaper(self.home, self.fallback, picker, self.runtime)
                self.assertFalse(owned.exists())
                self.assertEqual(self.link.resolve(), self.static, 'Recorded prior static survives migration')
                self.assertEqual(Path(result['recovery']).read_bytes(), self.fallback.read_bytes())
                self.assert_preserved(before)
                ledger = json.loads(self.ownership().read_text())
                self.assertEqual(ledger['schema'], 2)
                self.assert_stable_record(ledger['recovered'][-1])
                self.assertEqual(ledger['recovered'][-1]['size'], self.fallback.stat().st_size)

    def test_legacy_migration_accepts_only_device_mismatch(self):
        owned = self.add_owned()
        ledger = self.legacy_ledger()
        for field in ('inode', 'mtime', 'sha256', 'mode'):
            with self.subTest(field=field):
                altered = json.loads(json.dumps(ledger))
                record = altered['images'][str(owned)]
                if field in ('inode', 'mtime'):
                    record['identity'][1 if field == 'inode' else 2] += 1
                elif field == 'mode':
                    record['mode'] = 0o600
                else:
                    record['sha256'] = '0' * 64
                self.ownership().write_text(json.dumps(altered))
                self.assertFalse(self.metadata()['can_remove_live'])
                with self.assertRaisesRegex(RuntimeError, 'No unchanged'):
                    remove_live_wallpaper(self.home, self.fallback, Mock(), self.runtime)
                self.assertTrue(owned.exists())

    def test_explicit_use_migrates_verified_legacy_and_retains_unverified_provenance(self):
        owned = self.add_owned()
        ledger = self.legacy_ledger()
        old_archive = dict(ledger['images'][str(owned)], path=str(self.personal() / 'old-live-doom.webp'),
                           recovery=str(self.base / 'old-archive.webp'), state='archived')
        ledger['recovered'].append(old_archive)
        self.ownership().write_text(json.dumps(ledger))
        self.select(self.static)
        self.assertEqual(use_live_wallpaper(self.home, self.fallback, self.select, self.runtime), owned)
        migrated = json.loads(self.ownership().read_text())
        self.assertEqual(migrated['schema'], 2)
        self.assert_stable_record(migrated['images'][str(owned)])
        self.assert_stable_record(migrated['recovered'][-1])
        self.assertIsNone(migrated['recovered'][-1]['size'])
        self.assertEqual(migrated['recovered'][-1]['recovery'], old_archive['recovery'])
        self.assertTrue(self.metadata()['can_remove_live'])

    def test_metadata_is_read_only_before_any_addition(self):
        before = self.preserved()
        self.assertFalse(self.ownership().parent.exists())
        value = self.metadata()
        self.assertEqual(value['name'], 'tokyo-night')
        self.assertFalse(value['live_choice_present'])
        self.assertFalse(value['can_remove_live'])
        self.assertFalse(self.ownership().parent.exists())
        self.assert_preserved(before)

    def test_future_copy_records_ownership_without_metadata_writes(self):
        owned = self.add_owned()
        before = self.preserved()
        ledger = (self.ownership().read_bytes(), self.ownership().stat().st_mtime_ns)
        self.assertEqual(self.ownership().stat().st_mode & 0o777, 0o600)
        for _ in range(2):
            value = self.metadata()
            self.assertTrue(value['can_remove_live'])
            self.assertTrue(value['live_choice_present'])
            self.assertEqual(value['owned_path'], str(owned))
            self.assertFalse(value['legacy_adoptable'])
        self.assertEqual((self.ownership().read_bytes(), self.ownership().stat().st_mtime_ns), ledger)
        self.assert_preserved(before)

    def test_active_removal_prefers_recorded_static_and_archives_recoverably(self):
        owned = self.add_owned()
        other = self.backgrounds / '00-earlier-sorted-static.png'
        other.write_bytes(b'keep this other static choice')
        before = self.preserved(selection=False)
        result = remove_live_wallpaper(self.home, self.fallback, self.select, self.runtime)
        self.assertEqual(self.link.resolve(), self.static)
        self.assertEqual(self.selections[-1], self.static)
        self.assertEqual(result['removed'], str(owned))
        self.assertEqual(result['selected'], str(self.static))
        self.assertFalse(owned.exists())
        recovery = Path(result['recovery'])
        self.assertEqual(recovery.read_bytes(), self.fallback.read_bytes())
        self.assertEqual(recovery.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.ownership().parent.stat().st_mode & 0o777, 0o700)
        import json
        ledger = json.loads(self.ownership().read_text())
        self.assertNotIn(str(owned), ledger['images'])
        self.assertEqual(ledger['recovered'][-1]['recovery'], str(recovery))
        self.assertEqual(ledger['recovered'][-1]['state'], 'archived')
        self.assertFalse(self.metadata()['can_remove_live'])
        self.assertEqual(other.read_bytes(), b'keep this other static choice')
        self.assert_preserved(before)

    def test_inactive_choice_removal_does_not_call_picker_or_change_selection(self):
        owned = self.add_owned()
        self.select(self.static)
        before = self.preserved()
        picker = Mock(side_effect=AssertionError('inactive removal must not select anything'))
        self.assertTrue(self.metadata()['can_remove_live'])
        result = remove_live_wallpaper(self.home, self.fallback, picker, self.runtime)
        picker.assert_not_called()
        self.assertEqual(Path(result['recovery']).read_bytes(), self.fallback.read_bytes())
        self.assertFalse(owned.exists())
        self.assert_preserved(before)

    def test_readding_after_removal_keeps_archive_and_reuses_safe_original_filename(self):
        first = self.add_owned()
        result = remove_live_wallpaper(self.home, self.fallback, self.select, self.runtime)
        archive = Path(result['recovery'])
        original_archive = (archive.read_bytes(), archive.stat().st_mtime_ns)
        second = self.add_owned()
        self.assertEqual(first, second)
        self.assertEqual(second.read_bytes(), self.fallback.read_bytes())
        self.assertTrue(self.metadata()['can_remove_live'])
        self.assertEqual((archive.read_bytes(), archive.stat().st_mtime_ns), original_archive)

    def test_user_edit_is_never_removed_or_readopted(self):
        owned = self.add_owned()
        owned.write_bytes(b'user edited this choice')
        before = self.preserved()
        value = self.metadata()
        self.assertFalse(value['can_remove_live'])
        self.assertFalse(value['legacy_adoptable'])
        picker = Mock()
        with self.assertRaisesRegex(RuntimeError, 'No unchanged'):
            remove_live_wallpaper(self.home, self.fallback, picker, self.runtime)
        picker.assert_not_called()
        self.assertEqual(owned.read_bytes(), b'user edited this choice')
        self.assert_preserved(before)

    def test_same_byte_replacement_inode_and_changed_mode_are_protected(self):
        owned = self.add_owned()
        original = owned.read_bytes()
        with self.subTest(change='permissions'):
            owned.chmod(0o600)
            self.assertFalse(self.metadata()['can_remove_live'])
            with self.assertRaises(RuntimeError):
                remove_live_wallpaper(self.home, self.fallback, Mock(), self.runtime)
        owned.chmod(0o644)
        replacement = owned.with_name('user-replacement.tmp')
        replacement.write_bytes(original)
        replacement.replace(owned)
        self.assertFalse(self.metadata()['can_remove_live'])
        self.assertFalse(self.metadata()['legacy_adoptable'])
        with self.assertRaises(RuntimeError):
            remove_live_wallpaper(self.home, self.fallback, Mock(), self.runtime)
        self.assertEqual(owned.read_bytes(), original)

    def test_owned_symlink_or_hardlink_is_preserved(self):
        owned = self.add_owned()
        external = self.base / 'external-live-doom.webp'
        owned.replace(external)
        owned.symlink_to(external)
        before = self.preserved()
        self.assertFalse(self.metadata()['can_remove_live'])
        with self.assertRaises(RuntimeError):
            remove_live_wallpaper(self.home, self.fallback, Mock(), self.runtime)
        self.assertTrue(owned.is_symlink())
        self.assertEqual(external.read_bytes(), self.fallback.read_bytes())
        self.assert_preserved(before)
        owned.unlink()
        external.replace(owned)
        os.link(owned, external)
        self.assertFalse(self.metadata()['can_remove_live'])
        with self.assertRaises(RuntimeError):
            remove_live_wallpaper(self.home, self.fallback, Mock(), self.runtime)
        self.assertTrue(owned.exists() and external.exists())

    def test_noncanonical_exact_bundle_user_image_and_theme_copy_are_protected(self):
        self.personal().mkdir(parents=True)
        user = self.personal() / 'my-live-doom.webp'
        user.write_bytes(self.fallback.read_bytes())
        theme_live = self.backgrounds / '1-live-doom.webp'
        theme_live.write_bytes(self.fallback.read_bytes())
        self.select(theme_live)
        before = self.preserved()
        self.assertTrue(self.metadata()['live_choice_present'])
        self.assertFalse(self.metadata()['can_remove_live'])
        with self.assertRaises(RuntimeError):
            remove_live_wallpaper(self.home, self.fallback, Mock(), self.runtime)
        self.assertEqual(user.read_bytes(), self.fallback.read_bytes())
        self.assertEqual(theme_live.read_bytes(), self.fallback.read_bytes())
        self.assertFalse(self.ownership().exists())
        self.assert_preserved(before)

    def test_exact_legacy_personal_copy_is_adopted_only_when_removing(self):
        self.personal().mkdir(parents=True)
        legacy = self.personal() / '1-live-doom.webp'
        legacy.write_bytes(self.fallback.read_bytes())
        legacy.chmod(0o644)
        self.select(legacy)
        before = self.preserved(selection=False)
        value = self.metadata()
        self.assertTrue(value['can_remove_live'])
        self.assertTrue(value['legacy_adoptable'])
        self.assertFalse(self.ownership().parent.exists())
        result = remove_live_wallpaper(self.home, self.fallback, self.select, self.runtime)
        self.assertTrue(result['legacy_adopted'])
        self.assertFalse(legacy.exists())
        self.assertEqual(Path(result['recovery']).read_bytes(), self.fallback.read_bytes())
        self.assertEqual(self.link.resolve(), self.static)
        self.assert_preserved(before)

    def test_legacy_full_hash_collision_name_can_be_adopted(self):
        self.personal().mkdir(parents=True)
        reserved = self.personal() / '1-live-doom.webp'
        reserved.mkdir()
        legacy = self.personal() / ('1-live-doom-' + hashlib.sha256(self.fallback.read_bytes()).hexdigest() + '.webp')
        legacy.write_bytes(self.fallback.read_bytes())
        legacy.chmod(0o644)
        self.assertTrue(self.metadata()['legacy_adoptable'])
        result = remove_live_wallpaper(self.home, self.fallback, Mock(), self.runtime)
        self.assertTrue(result['legacy_adopted'])
        self.assertTrue(reserved.is_dir())

    def test_legacy_edited_bytes_mode_or_missing_bundle_cannot_be_adopted(self):
        self.personal().mkdir(parents=True)
        legacy = self.personal() / '1-live-doom.webp'
        for kind in ('different-bytes', 'different-mode', 'missing-bundle'):
            with self.subTest(kind=kind):
                legacy.write_bytes(b'user pixels' if kind == 'different-bytes' else self.fallback.read_bytes())
                legacy.chmod(0o600 if kind == 'different-mode' else 0o644)
                if kind == 'missing-bundle':
                    self.fallback.unlink()
                self.assertFalse(self.metadata()['can_remove_live'])
                self.assertFalse(self.metadata()['legacy_adoptable'])
                with self.assertRaises(RuntimeError):
                    remove_live_wallpaper(self.home, self.fallback, Mock(), self.runtime)
                self.assertTrue(legacy.exists())

    def test_recorded_copy_remains_removable_without_original_bundle(self):
        owned = self.add_owned()
        data = owned.read_bytes()
        self.fallback.unlink()
        self.assertTrue(self.metadata()['can_remove_live'])
        result = remove_live_wallpaper(self.home, self.fallback, self.select, self.runtime)
        self.assertEqual(Path(result['recovery']).read_bytes(), data)

    def test_phobos_is_protected_even_for_an_added_personal_choice(self):
        self.theme_name.write_text('phobos\n')
        owned = self.add_owned()
        before = self.preserved()
        self.assertFalse(self.metadata()['can_remove_live'])
        self.assertIn('protected', self.metadata()['remove_reason'])
        picker = Mock()
        with self.assertRaisesRegex(RuntimeError, 'Phobos'):
            remove_live_wallpaper(self.home, self.fallback, picker, self.runtime)
        picker.assert_not_called()
        self.assertTrue(owned.exists())
        self.assert_preserved(before)

    def test_selected_owned_choice_without_static_replacement_is_not_removable(self):
        owned = self.add_owned()
        before = self.preserved(selection=False)
        before.pop(self.static)
        self.static.unlink()
        self.assertFalse(self.metadata()['can_remove_live'])
        picker = Mock()
        with self.assertRaisesRegex(RuntimeError, 'No safe static'):
            remove_live_wallpaper(self.home, self.fallback, picker, self.runtime)
        picker.assert_not_called()
        self.assertTrue(owned.exists())
        self.assertEqual(self.link.resolve(), owned)
        self.assert_preserved(before)

    def test_cancel_noop_or_picker_error_keeps_owned_image_and_original_selection(self):
        owned = self.add_owned()
        before = self.preserved()
        for picker in (Mock(return_value=False), Mock(), Mock(side_effect=RuntimeError('private picker failure'))):
            with self.subTest(picker=picker):
                with self.assertRaises(RuntimeError):
                    remove_live_wallpaper(self.home, self.fallback, picker, self.runtime)
                self.assertTrue(owned.exists())
                self.assertEqual(self.link.resolve(), owned)
                self.assertFalse((self.ownership().parent / 'recovered').exists())
                self.assert_preserved(before)

    def test_picker_failure_after_switch_restores_original_before_any_archive(self):
        owned = self.add_owned()
        before = self.preserved(selection=False)
        def failing_picker(path):
            self.select(path)
            raise RuntimeError('private picker failure after switching')
        with self.assertRaisesRegex(RuntimeError, 'after switching'):
            remove_live_wallpaper(self.home, self.fallback, failing_picker, self.runtime)
        self.assertEqual(self.link.resolve(), owned)
        self.assertTrue(owned.exists())
        self.assertFalse((self.ownership().parent / 'recovered').exists())
        self.assert_preserved(before)

    def test_theme_race_does_not_move_image_or_reselect_new_theme(self):
        owned = self.add_owned()
        new_asset = self.base / 'new-theme-static.png'
        new_asset.write_bytes(b'new selected theme asset')
        def concurrent_theme(path):
            self.select(path)
            self.theme_name.write_text('new-theme\n')
            self.select(new_asset)
        with self.assertRaisesRegex(RuntimeError, 'theme changed'):
            remove_live_wallpaper(self.home, self.fallback, concurrent_theme, self.runtime)
        self.assertTrue(owned.exists())
        self.assertEqual(self.theme_name.read_text(), 'new-theme\n')
        self.assertEqual(self.link.resolve(), new_asset)
        self.assertEqual(new_asset.read_bytes(), b'new selected theme asset')
        self.assertFalse((self.ownership().parent / 'recovered').exists())

    def test_use_theme_race_records_only_old_theme_copy_and_never_removes_images(self):
        def concurrent_theme(path):
            self.theme_name.write_text('new-theme\n')
        with self.assertRaisesRegex(RuntimeError, 'theme changed'):
            use_live_wallpaper(self.home, self.fallback, concurrent_theme, self.runtime)
        self.assertEqual(self.link.resolve(), self.static)
        self.assertEqual(list(self.personal().iterdir())[0].read_bytes(), self.fallback.read_bytes())
        self.assertEqual(self.theme_name.read_text(), 'new-theme\n')
        self.assertFalse(self.metadata()['can_remove_live'])

    def test_image_edit_during_static_selection_is_preserved_and_not_archived(self):
        owned = self.add_owned()
        def concurrent_edit(path):
            self.select(path)
            if path == self.static:
                owned.write_bytes(b'user edited during the picker')
        with self.assertRaisesRegex(RuntimeError, 'image or its selection changed'):
            remove_live_wallpaper(self.home, self.fallback, concurrent_edit, self.runtime)
        self.assertEqual(owned.read_bytes(), b'user edited during the picker')
        self.assertEqual(self.link.resolve(), owned)
        self.assertFalse((self.ownership().parent / 'recovered').exists())

    def test_recovery_collision_preserves_existing_private_images(self):
        self.add_owned()
        folder = self.ownership().parent / 'recovered/tokyo-night'
        folder.mkdir(parents=True)
        existing = folder / '1-live-doom.reserved.webp'
        existing.write_bytes(b'earlier recoverable image')
        before = (existing.read_bytes(), existing.stat().st_mtime_ns)
        result = remove_live_wallpaper(self.home, self.fallback, self.select, self.runtime)
        self.assertNotEqual(Path(result['recovery']), existing)
        self.assertEqual((existing.read_bytes(), existing.stat().st_mtime_ns), before)

    def test_recovery_alias_is_rejected_before_picker_and_no_external_files_touched(self):
        owned = self.add_owned()
        external = self.base / 'do-not-touch-recovery-alias'
        external.mkdir()
        (self.ownership().parent / 'recovered').symlink_to(external, target_is_directory=True)
        before = self.preserved()
        self.assertFalse(self.metadata()['can_remove_live'])
        picker = Mock()
        with self.assertRaisesRegex(RuntimeError, 'symlink'):
            remove_live_wallpaper(self.home, self.fallback, picker, self.runtime)
        picker.assert_not_called()
        self.assertTrue(owned.exists())
        self.assertEqual(list(external.iterdir()), [])
        self.assert_preserved(before)

    def test_corrupt_or_symlink_ownership_data_fails_closed_without_image_changes(self):
        owned = self.add_owned()
        self.ownership().write_text('{invalid json')
        before = self.preserved()
        self.assertFalse(self.metadata()['can_remove_live'])
        picker = Mock()
        with self.assertRaisesRegex(RuntimeError, 'ownership'):
            remove_live_wallpaper(self.home, self.fallback, picker, self.runtime)
        self.ownership().unlink()
        outside = self.base / 'unrelated-ownership.json'
        outside.write_bytes(b'do not overwrite this')
        self.ownership().symlink_to(outside)
        self.assertFalse(self.metadata()['can_remove_live'])
        with self.assertRaisesRegex(RuntimeError, 'symlink'):
            remove_live_wallpaper(self.home, self.fallback, picker, self.runtime)
        self.assertEqual(outside.read_bytes(), b'do not overwrite this')
        self.assertTrue(owned.exists())
        picker.assert_not_called()
        self.assert_preserved(before)

    def test_remove_busy_stock_lock_is_bounded_and_does_not_select_or_archive(self):
        owned = self.add_owned()
        with (self.runtime / 'omarchy-theme-set.lock').open('w') as owner:
            fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = self.preserved()
            picker = Mock()
            with patch('live_wallpaper.time.monotonic', side_effect=[0, 3]):
                with self.assertRaisesRegex(RuntimeError, 'theme change'):
                    remove_live_wallpaper(self.home, self.fallback, picker, self.runtime)
            picker.assert_not_called()
            self.assertTrue(owned.exists())
            self.assertFalse((self.ownership().parent / 'recovered').exists())
            self.assert_preserved(before)

    def test_failed_recovery_journal_write_keeps_original_and_restores_selection(self):
        owned = self.add_owned()
        before = self.preserved(selection=False)
        with patch('live_wallpaper._save_ledger', side_effect=OSError('private journal failure')):
            with self.assertRaisesRegex(OSError, 'journal failure'):
                remove_live_wallpaper(self.home, self.fallback, self.select, self.runtime)
        self.assertTrue(owned.exists())
        self.assertEqual(owned.read_bytes(), self.fallback.read_bytes())
        self.assertEqual(self.link.resolve(), owned)
        self.assertTrue(self.metadata()['can_remove_live'])
        self.assert_preserved(before)

    def test_relative_home_cannot_misclassify_the_current_owned_selection_as_inactive(self):
        relative_home = self.home.relative_to(self.base)
        with patch('os.getcwd', return_value=str(self.base)):
            owned = use_live_wallpaper(relative_home, self.fallback, self.select, self.runtime)
            self.assertTrue(live_wallpaper_metadata(relative_home, self.fallback)['can_remove_live'])
            result = remove_live_wallpaper(relative_home, self.fallback, self.select, self.runtime)
        self.assertEqual(self.link.resolve(), self.static)
        self.assertEqual(result['removed'], str(owned))
        self.assertEqual(self.selections[-1], self.static)


if __name__ == "__main__":
    unittest.main()
