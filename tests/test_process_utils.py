#!/usr/bin/env python3
# SPDX-License-Identifier: 0BSD
"""Actual private subprocess checks for finite calls and session isolation."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from process_utils import bounded_run, session_environment


class ProcessUtilsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='live-doom-process-test-')
        self.addCleanup(self.temporary.cleanup)
        self.work = Path(self.temporary.name)

    def run_python(self, code, **options):
        return bounded_run([sys.executable, '-B', '-c', code], cwd=self.work, **options)

    def assert_descendant_stopped(self, marker):
        pid = int(marker.read_text())
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            try:
                # Orphans may briefly remain zombies until the system reaper
                # collects them. A zombie has no running process or open pipes.
                status = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[0]
            except FileNotFoundError:
                return
            if status == 'Z':
                return
            time.sleep(0.01)
        self.fail('The owned descendant continued running after teardown')

    def child_code(self, marker, ending):
        # The child inherits the new group and both output pipes. Its PID is
        # written only to this test's private directory for teardown assertions.
        child = 'import os,time; from pathlib import Path; Path(' + repr(str(marker)) + ').write_text(str(os.getpid())); time.sleep(30)'
        return ('import subprocess,sys,time; subprocess.Popen([sys.executable,"-B","-c",' + repr(child) + ']); '
                'print("parent-started",flush=True); ' + ending)

    def test_completed_outputs_text_and_nonzero_check(self):
        result = self.run_python('import sys; sys.stdout.buffer.write(b"a\\xff"); sys.stderr.write("separate"); sys.exit(7)')
        self.assertIsInstance(result, subprocess.CompletedProcess)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (7, b'a\xff', b'separate'))
        text = self.run_python('import sys; sys.stdout.buffer.write(b"a\\xff"); sys.stderr.write("separate")', text=True)
        self.assertEqual((text.stdout, text.stderr), ('a\ufffd', 'separate'))
        with self.assertRaises(subprocess.CalledProcessError) as error:
            self.run_python('import sys; print("out"); print("err",file=sys.stderr); sys.exit(3)', text=True, check=True)
        self.assertEqual((error.exception.returncode, error.exception.stdout, error.exception.stderr), (3, 'out\n', 'err\n'))

    def test_combined_flood_cap_and_owned_descendant_cleanup(self):
        marker = self.work / 'flood-child'
        code = self.child_code(marker,
            'time.sleep(.08); sys.stdout.buffer.write(b"x"*32768); sys.stdout.flush(); '
            'sys.stderr.buffer.write(b"y"*32768); sys.stderr.flush(); time.sleep(30)')
        started = time.monotonic()
        with self.assertRaisesRegex(ValueError, '^Process output exceeds its byte limit$'):
            self.run_python(code, timeout=2, max_output_bytes=40000)
        self.assertLess(time.monotonic() - started, 1)
        self.assert_descendant_stopped(marker)

    def test_deadline_kills_parent_and_child_with_partial_output(self):
        marker = self.work / 'deadline-child'
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired) as error:
            self.run_python(self.child_code(marker, 'time.sleep(30)'), timeout=.2)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(error.exception.timeout, .2)
        self.assertIn(b'parent-started', error.exception.stdout)
        self.assertEqual(error.exception.stderr, b'')
        self.assert_descendant_stopped(marker)

    def test_exited_parent_does_not_hide_descendant_pipe_deadline(self):
        marker = self.work / 'orphan-child'
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_python(self.child_code(marker, 'sys.exit(0)'), timeout=.2)
        self.assertLess(time.monotonic() - started, 1)
        self.assert_descendant_stopped(marker)

    def test_closed_pipes_still_obey_process_deadline(self):
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_python('import os,time; os.close(1); os.close(2); time.sleep(30)', timeout=.1)
        self.assertLess(time.monotonic() - started, 1)

    def test_environment_is_closed_and_explicit_environment_replaces_it(self):
        supplied = {'HOME': str(self.work), 'XDG_RUNTIME_DIR': str(self.work / 'runtime'),
                    'WAYLAND_DISPLAY': 'wayland-private', 'HYPRLAND_INSTANCE_SIGNATURE': 'private',
                    'DBUS_SESSION_BUS_ADDRESS': 'unix:path=private', 'XDG_CONFIG_HOME': str(self.work / 'config'),
                    'XDG_DATA_HOME': str(self.work / 'data'), 'XDG_CACHE_HOME': str(self.work / 'cache'),
                    'XDG_STATE_HOME': str(self.work / 'state'), 'XDG_CURRENT_DESKTOP': 'Hyprland',
                    'XDG_SESSION_TYPE': 'wayland', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8',
                    'LD_PRELOAD': 'must-not-inherit', 'LD_LIBRARY_PATH': '/private/override',
                    'PYTHONPATH': '/private/python', 'PYTHONHOME': '/private/python',
                    'GIT_CONFIG_GLOBAL': '/private/git', 'OMARCHY_PATH': '/private/override',
                    'PATH': '/private/bin', 'SECRET_TEST': 'hidden'}
        with patch.dict(os.environ, supplied, clear=True):
            expected = {key: value for key, value in supplied.items()
                        if key not in ('LD_PRELOAD', 'LD_LIBRARY_PATH', 'PYTHONPATH', 'PYTHONHOME',
                                       'GIT_CONFIG_GLOBAL', 'OMARCHY_PATH', 'PATH', 'SECRET_TEST')}
            expected.update(PATH='/usr/bin:/bin', PYTHONDONTWRITEBYTECODE='1',
                            OMARCHY_PATH='/usr/share/omarchy')
            self.assertEqual(session_environment(), expected)
            result = self.run_python('import json,os; print(json.dumps(dict(os.environ)))', text=True)
            self.assertEqual(json.loads(result.stdout), expected)
            explicit = self.run_python('import os; print(os.getenv("ONLY_PRIVATE")); print(os.getenv("HOME"))',
                                       text=True, env={'ONLY_PRIVATE': 'yes', 'LANG': 'C.UTF-8'})
            self.assertEqual(explicit.stdout, 'yes\nNone\n')

    def test_invalid_arguments_rejected_before_spawn_and_zero_cap(self):
        for arguments in ([], ['python3', '-c', 'pass'], sys.executable):
            with self.assertRaises(ValueError):
                bounded_run(arguments)
        for timeout in (0, -1, True, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                self.run_python('pass', timeout=timeout)
        for cap in (-1, True, 1.5):
            with self.assertRaises(ValueError):
                self.run_python('pass', max_output_bytes=cap)
        result = self.run_python('pass', max_output_bytes=0)
        self.assertEqual((result.stdout, result.stderr), (b'', b''))
        with self.assertRaisesRegex(ValueError, 'byte limit'):
            self.run_python('print("one")', max_output_bytes=0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
