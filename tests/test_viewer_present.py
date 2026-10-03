#!/usr/bin/python3
# SPDX-License-Identifier: 0BSD
"""Compile the real viewer's framebuffer and Wayland wire regressions privately."""
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PROTOCOLS = ('xdg-shell', 'xdg-output-unstable-v1', 'wlr-layer-shell-unstable-v1',
             'relative-pointer-unstable-v1', 'pointer-constraints-unstable-v1')
PACKAGES = ('wayland-client', 'wayland-cursor', 'xkbcommon', 'cairo')


class ViewerPresentationTests(unittest.TestCase):
    def test_real_viewer_snapshot_and_wire_policy(self):
        required = ('cc', 'pkg-config', 'wayland-scanner')
        if any(shutil.which(command) is None for command in required):
            self.skipTest('Viewer build dependencies are not installed')
        probe = subprocess.run(['pkg-config', '--exists', *PACKAGES], timeout=5)
        if probe.returncode:
            self.skipTest('Viewer development packages are not installed')
        with tempfile.TemporaryDirectory(prefix='live-doom-viewer-present-') as temporary:
            work = Path(temporary)
            for name in PROTOCOLS:
                definition = ROOT / 'src/protocols' / (name + '.xml')
                for output, kind in ((name + '-client-protocol.h', 'client-header'),
                                     (name + '-protocol.c', 'private-code')):
                    result = subprocess.run(['wayland-scanner', kind, str(definition), str(work / output)],
                                            capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
            flags = subprocess.run(['pkg-config', '--cflags', '--libs', *PACKAGES], capture_output=True,
                                   text=True, check=True, timeout=5).stdout
            executable = work / 'viewer-present-test'
            command = ['cc', '-std=c11', '-O2', '-Wall', '-Wextra', '-Wpedantic',
                       '-I' + str(ROOT / 'src'), '-I' + str(work), str(ROOT / 'tests/viewer-snapshot.c'),
                       *(str(work / (name + '-protocol.c')) for name in PROTOCOLS),
                       *shlex.split(flags), '-lm', '-o', str(executable)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('skipped tics copy no pixels and attach/damage/commit nothing', result.stdout)


if __name__ == '__main__':
    unittest.main()
