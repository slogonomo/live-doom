# SPDX-License-Identifier: 0BSD
"""Token ownership never changes a user's existing screensaver toggle."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from saver_ownership import SaverOwnership, INLINE_RELEASE
import saver_ownership


class SaverOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='live-doom-saver-')
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.owner = SaverOwnership(self.home, self.home / 'state')

    def test_claim_is_private_idempotent_and_release_checks_tokens(self):
        first = self.owner.claim()
        self.assertTrue(first['owned'])
        self.assertEqual(self.owner.claim(), first)
        for file in (self.owner.toggle, self.owner.marker):
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.owner.release()['released'])
        self.assertFalse(self.owner.toggle.exists())
        self.assertFalse(self.owner.marker.exists())

    def test_failed_token_write_does_not_suppress_the_stock_saver(self):
        with patch('saver_ownership.os.fsync', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                self.owner.claim()
        self.assertFalse(self.owner.toggle.exists())
        self.assertFalse(self.owner.marker.exists())
        self.assertFalse(self.owner.status()['blocked'])

    def test_preexisting_user_toggle_is_never_claimed_or_removed(self):
        self.owner.toggle.parent.mkdir(parents=True)
        self.owner.toggle.write_bytes(b'user preference')
        state = self.owner.claim()
        self.assertFalse(state['owned'])
        self.assertTrue(state['blocked'])
        self.assertFalse(self.owner.release()['released'])
        self.assertEqual(self.owner.toggle.read_bytes(), b'user preference')

    def test_deleted_toggle_is_revoked_and_never_automatically_reclaimed(self):
        self.owner.claim()
        self.owner.toggle.unlink()
        self.assertTrue(self.owner.status()['revoked'])
        self.assertTrue(self.owner.claim()['revoked'])
        self.assertFalse(self.owner.toggle.exists())
        self.owner.release()
        self.assertTrue(self.owner.claim()['owned'])

    def test_inline_release_during_status_is_not_mistaken_for_user_revocation(self):
        state = self.owner.claim()
        original = saver_ownership._token_file
        released = False
        def racing_read(path):
            nonlocal released
            value = original(path)
            if path == self.owner.marker and not released:
                released = True
                self.inline(state)
            return value
        with patch('saver_ownership._token_file', side_effect=racing_read):
            sample = self.owner.status()
        self.assertFalse(sample['revoked'])
        self.assertFalse(sample['owned'])

    def test_replaced_toggle_is_preserved_on_release(self):
        self.owner.claim()
        self.owner.toggle.write_bytes(b'user replacement')
        self.assertTrue(self.owner.status()['revoked'])
        self.assertTrue(self.owner.status()['blocked'])
        self.assertFalse(self.owner.release()['released'])
        self.assertEqual(self.owner.toggle.read_bytes(), b'user replacement')

    def test_symlink_toggle_is_blocked_and_target_unchanged(self):
        self.owner.toggle.parent.mkdir(parents=True)
        target = self.home / 'personal'
        target.write_bytes(b'keep')
        self.owner.toggle.symlink_to(target)
        self.assertTrue(self.owner.claim()['blocked'])
        self.assertFalse(self.owner.release()['released'])
        self.assertTrue(self.owner.toggle.is_symlink())
        self.assertEqual(target.read_bytes(), b'keep')

    def inline(self, state):
        subprocess.run(['/usr/bin/python3', '-I', '-c', INLINE_RELEASE,
                        state['toggle'], state['marker'], state['token']],
                       cwd='/', env={'PATH': '/usr/bin:/bin'}, check=True, timeout=2)

    def test_inline_release_works_without_imports_or_checkout(self):
        state = self.owner.claim()
        self.inline(state)
        self.assertFalse(self.owner.toggle.exists())
        self.assertFalse(self.owner.marker.exists())

    def test_inline_old_token_cannot_remove_a_new_claim_or_user_file(self):
        first = self.owner.claim()
        self.owner.release()
        second = self.owner.claim()
        self.inline(first)
        self.assertTrue(self.owner.status()['owned'])
        self.owner.toggle.write_bytes(b'personal')
        self.inline(second)
        self.assertEqual(self.owner.toggle.read_bytes(), b'personal')


if __name__ == '__main__':
    unittest.main()
