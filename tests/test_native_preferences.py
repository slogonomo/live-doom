# SPDX-License-Identifier: 0BSD
"""Preserve native game preferences across managed engine launches."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from controller import prepare_native_config


class NativePreferenceTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory(prefix='live-doom-preferences-')
        self.addCleanup(self.work.cleanup)
        self.path = Path(self.work.name) / 'desktop.cfg'

    def test_fresh_config_seeds_defaults_and_forces_backend(self):
        prepare_native_config(self.path, {'use_vsync': 0}, {'snd_mididevice': 0})
        self.assertEqual(self.path.read_bytes(), b'snd_mididevice 0\nuse_vsync 0\n')

    def test_native_zero_and_nonzero_volumes_and_unknown_preferences_survive(self):
        original = (b'# Native saved preferences\nsfx_volume 0\nmusic_volume 7\n'
                    b'show_messages 0\nsnd_mididevice -1\nuse_vsync 1\n')
        self.path.write_bytes(original)
        prepare_native_config(self.path, {'use_vsync': 0}, {'snd_mididevice': 0})
        self.assertEqual(self.path.read_bytes(), original.replace(b'use_vsync 1', b'use_vsync 0'))

    def test_duplicate_managed_keys_cannot_override_new_output_dimensions(self):
        self.path.write_bytes(b'i_videomode "320x200w"\n# user preference\n'
                              b'i_videomode "640x480w"\nsfx_volume 0\n')
        prepare_native_config(self.path, {'i_videomode': '"3440x1440w"'})
        self.assertEqual(self.path.read_bytes(), b'# user preference\nsfx_volume 0\n'
                         b'i_videomode "3440x1440w"\n')

    def test_repeat_launch_does_not_rewrite_unchanged_file(self):
        prepare_native_config(self.path, {'use_vsync': 0}, {'snd_mididevice': 0})
        before = self.path.stat()
        prepare_native_config(self.path, {'use_vsync': 0}, {'snd_mididevice': 0})
        after = self.path.stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))

    def test_existing_non_utf8_comments_and_missing_final_newline_survive(self):
        self.path.write_bytes(b'# old comment \xff\nsfx_volume 0')
        prepare_native_config(self.path, {'use_vsync': 0})
        self.assertEqual(self.path.read_bytes(), b'# old comment \xff\nsfx_volume 0\nuse_vsync 0\n')

    def test_symlinked_config_is_refused_without_changing_target(self):
        target = self.path.with_name('other.cfg')
        target.write_bytes(b'sfx_volume 3\n')
        self.path.symlink_to(target)
        with self.assertRaises((OSError, ValueError)):
            prepare_native_config(self.path, {'use_vsync': 0})
        self.assertEqual(target.read_bytes(), b'sfx_volume 3\n')

    def test_oversized_config_is_refused_without_truncation(self):
        self.path.write_bytes(b'#' * (1024 * 1024 + 1))
        with self.assertRaises((OSError, ValueError)):
            prepare_native_config(self.path, {'use_vsync': 0})
        self.assertEqual(self.path.stat().st_size, 1024 * 1024 + 1)


if __name__ == '__main__':
    unittest.main()
