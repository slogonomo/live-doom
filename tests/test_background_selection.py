# SPDX-License-Identifier: 0BSD
"""Actual symlink/theme selections in a temporary HOME; no desktop IPC."""
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from background_selection import BackgroundSelection


class BackgroundSelectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="doom-background-test-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name) / "home with spaces"
        self.state = self.home / ".local/share/doom-desktop"
        self.link = self.home / ".local/state/omarchy/current/background"
        self.link.parent.mkdir(parents=True)
        self.static = self.home / "Pictures/static.webp"
        self.live = self.home / "Pictures/1-Live-Doom.webp"
        self.static.parent.mkdir(parents=True)
        self.static.write_bytes(b"static fixture")
        self.live.write_bytes(b"live fixture")

    def select(self, path):
        next_link = self.link.with_name("next-background")
        next_link.symlink_to(path)
        next_link.replace(self.link)

    def test_initial_static_preserves_existing_wallpaper_then_picker_changes_apply(self):
        self.select(self.static)
        watcher = BackgroundSelection(self.home, self.state)
        self.assertIsNone(watcher.poll())
        self.assertIsNone(watcher.poll())
        self.select(self.live)
        self.assertIs(watcher.poll(), True)
        self.assertIsNone(watcher.poll())
        self.select(self.static)
        self.assertIs(watcher.poll(), False)
        self.assertEqual(self.static.read_bytes(), b"static fixture")
        self.assertEqual(self.live.read_bytes(), b"live fixture")

    def test_first_live_selection_opts_in_and_unchanged_restart_preserves_manual_override(self):
        self.select(self.live)
        watcher = BackgroundSelection(self.home, self.state)
        self.assertIs(watcher.poll(), True)
        saved = watcher.state_path.read_bytes()
        before = watcher.state_path.stat().st_mtime_ns
        self.assertIsNone(BackgroundSelection(self.home, self.state).poll())
        self.assertEqual(watcher.state_path.read_bytes(), saved)
        self.assertEqual(watcher.state_path.stat().st_mtime_ns, before)
        self.assertEqual(watcher.state_path.stat().st_mode & 0o777, 0o600)

    def test_selection_while_stopped_and_same_file_reselection_are_detected(self):
        self.select(self.live)
        self.assertIs(BackgroundSelection(self.home, self.state).poll(), True)
        self.select(self.static)
        watcher = BackgroundSelection(self.home, self.state)
        self.assertIs(watcher.poll(), False)
        # Re-selecting the current static file in the picker must disable a
        # wallpaper enabled manually since the previous selection.
        self.select(self.static)
        self.assertIs(watcher.poll(), False)

    def test_persisted_observation_survives_device_change_without_picker_change(self):
        for selected in (self.static, self.live):
            with self.subTest(selected=selected):
                self.select(selected)
                watcher = BackgroundSelection(self.home, self.state)
                watcher.poll()
                saved = json.loads(watcher.state_path.read_text())
                self.assertEqual(saved["schema"], 2)
                self.assertEqual(set(saved["selection"]["link"]), {"mtime_ns", "size", "mode"})
                actual_lstat = Path.lstat
                def changed_device(path, *args, **kwargs):
                    info = actual_lstat(path, *args, **kwargs)
                    if path != self.link:
                        return info
                    fields = {key: getattr(info, key) for key in
                              ("st_dev", "st_ino", "st_mtime_ns", "st_ctime_ns", "st_mode", "st_size")}
                    fields["st_dev"] += 100
                    return SimpleNamespace(**fields)
                with patch.object(Path, "lstat", changed_device):
                    self.assertIsNone(BackgroundSelection(self.home, self.state).poll())
                self.assertEqual(json.loads(watcher.state_path.read_text()), saved)

    def test_legacy_device_change_migrates_without_overriding_manual_choice(self):
        for selected in (self.static, self.live):
            with self.subTest(selected=selected):
                self.select(selected)
                self.state.mkdir(parents=True, exist_ok=True)
                info = self.link.lstat()
                state_path = self.state / "background-selection.json"
                state_path.write_text(json.dumps({"schema": 1, "selection": {
                    "path": str(selected), "link": [info.st_dev + 100, info.st_ino,
                                                       info.st_mtime_ns, info.st_ctime_ns]}}))
                watcher = BackgroundSelection(self.home, self.state)
                self.assertIsNone(watcher.poll())
                migrated = json.loads(state_path.read_text())
                self.assertEqual(migrated["schema"], 2)
                self.assertEqual(migrated["selection"], watcher.previous)
                self.assertEqual(set(migrated["selection"]["link"]), {"mtime_ns", "size", "mode"})
                self.assertIsNone(watcher.poll())
                self.select(selected)
                self.assertIs(watcher.poll(), selected == self.live)

    def test_legacy_observation_still_detects_real_picker_change_while_stopped(self):
        self.select(self.live)
        self.state.mkdir(parents=True)
        info = self.link.lstat()
        state_path = self.state / "background-selection.json"
        old = {"schema": 1, "selection": {"path": str(self.live),
               "link": [info.st_dev + 100, info.st_ino, info.st_mtime_ns, info.st_ctime_ns]}}
        state_path.write_text(json.dumps(old))
        self.select(self.static)
        self.assertIs(BackgroundSelection(self.home, self.state).poll(), False)
        state_path.write_text(json.dumps(old))
        self.select(self.live)
        # Even a same-file re-selection is a picker event when its mtime has
        # changed; migration only ignores the boot-unstable parts of identity.
        os.utime(self.link, ns=(info.st_mtime_ns + 100, info.st_mtime_ns + 100), follow_symlinks=False)
        self.assertIs(BackgroundSelection(self.home, self.state).poll(), True)

    def test_copied_theme_background_and_any_theme_can_opt_in(self):
        self.select(self.static)
        watcher = BackgroundSelection(self.home, self.state)
        self.assertIsNone(watcher.poll())
        staged = self.link.parent / "theme/backgrounds/1-live-doom.webp"
        staged.parent.mkdir(parents=True)
        staged.write_bytes(b"copied theme fallback")
        self.select(staged)
        self.assertIs(watcher.poll(), True)
        self.select(self.home / "Pictures/ordinary-live-video.webp")
        self.assertIsNone(watcher.poll())  # unreadable choice is not accepted
        (self.home / "Pictures/ordinary-live-video.webp").write_bytes(b"video still")
        self.assertIs(watcher.poll(), False)

    def test_missing_non_symlink_directory_cycle_and_transient_theme_are_ignored(self):
        watcher = BackgroundSelection(self.home, self.state)
        self.assertIsNone(watcher.poll())
        self.select(self.live)
        self.assertIs(watcher.poll(), True)
        saved = watcher.state_path.read_bytes()
        self.live.unlink()
        self.assertIsNone(watcher.poll())
        self.assertEqual(watcher.state_path.read_bytes(), saved)
        self.live.write_bytes(b"replaced during theme staging")
        self.assertIsNone(watcher.poll())
        self.link.unlink()
        self.link.write_bytes(b"not a selected background symlink")
        self.assertIsNone(watcher.poll())
        self.link.unlink()
        self.select(self.home / "Pictures")
        self.assertIsNone(watcher.poll())
        self.select(self.link)
        self.assertIsNone(watcher.poll())
        self.assertEqual(watcher.state_path.read_bytes(), saved)

    def test_invalid_saved_observation_migrates_without_disabling_static(self):
        self.state.mkdir(parents=True)
        for saved in (b"invalid json", b"[]", b'{"schema":1,"selection":null}',
                      b'{"schema":2,"selection":{"path":"x","link":[1,2,3,4]}}'):
            with self.subTest(saved=saved):
                (self.state / "background-selection.json").write_bytes(saved)
                self.select(self.static)
                watcher = BackgroundSelection(self.home, self.state)
                self.assertIsNone(watcher.poll())
                self.assertEqual(json.loads(watcher.state_path.read_text())["schema"], 2)

    def test_symlink_replaced_during_snapshot_is_not_published(self):
        self.select(self.static)
        watcher = BackgroundSelection(self.home, self.state)
        actual_resolve = Path.resolve
        def replacing_resolve(path, **kwargs):
            if path == self.link:
                self.select(self.live)
            return actual_resolve(path, **kwargs)
        with patch.object(Path, "resolve", replacing_resolve):
            self.assertIsNone(watcher.poll())
        self.assertFalse(watcher.state_path.exists())
        self.assertIs(watcher.poll(), True)

    def test_persistence_failure_does_not_repeat_selection_or_touch_settings(self):
        self.select(self.live)
        watcher = BackgroundSelection(self.home, self.state)
        with patch.object(watcher, "_remember", side_effect=OSError("readonly fixture")):
            self.assertIs(watcher.poll(), True)
            self.assertIsNone(watcher.poll())
        self.assertIn("readonly fixture", watcher.error)
        self.assertFalse((self.state / "config.json").exists())


if __name__ == "__main__":
    unittest.main()
