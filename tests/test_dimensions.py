#!/usr/bin/env python3
# SPDX-License-Identifier: 0BSD
"""Native viewport selection and live resize without restarting the game."""
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from controller import render_dimensions, workspace_views


class DisplaySelectionTests(unittest.TestCase):
    def test_native_focused_monitor_and_rotation(self):
        monitors = [{'width': 1920, 'height': 1080},
                    {'width': 3440, 'height': 1440, 'focused': True, 'dpmsStatus': False}]
        self.assertEqual(render_dimensions({}, monitors=monitors), (3440, 1440))
        monitors[1].update(width=1920, height=1080, transform=1)
        self.assertEqual(render_dimensions({}, monitors=monitors), (1080, 1920))

    def test_large_display_preserves_aspect_within_transport_capacity(self):
        width, height = render_dimensions({}, monitors=[{'width': 7680, 'height': 2160}])
        self.assertEqual((width, height), (3840, 1080))
        self.assertEqual(render_dimensions({}, headless=True), (640, 400))

    def test_reserved_panels_are_per_output_logical_insets(self):
        monitors = [{'name': 'WIDE', 'width': 3440, 'height': 1440,
                     'scale': 2, 'reserved': [12, 30, 18, 8]},
                    {'name': 'PORTRAIT', 'reserved': [0, 42.25, 80, 0]}]
        views = workspace_views(monitors, [])
        self.assertEqual(views['WIDE']['reserved'], (12, 30, 18, 8))
        self.assertEqual(views['PORTRAIT']['reserved'], (0, 43, 80, 0))
        monitors[0]['reserved'] = [0, 0, 0, 0]
        self.assertEqual(workspace_views(monitors, [])['WIDE']['reserved'], (0, 0, 0, 0))

    def test_malformed_reserved_geometry_is_bounded_or_ignored(self):
        malformed = (None, '30', {}, [1, 2, 3], [0, True, 0, 0],
                     [0, float('nan'), 0, 0], [0, float('inf'), 0, 0])
        for value in malformed:
            with self.subTest(value=value):
                self.assertEqual(workspace_views([{'name': 'TEST', 'reserved': value}], [])['TEST']['reserved'],
                                 (0, 0, 0, 0))
        self.assertEqual(workspace_views([{'name': 'TEST', 'reserved': [-1, 99999, 2, 3]}], [])['TEST']['reserved'],
                         (0, 16384, 2, 3))
        self.assertEqual(workspace_views([{'name': 'TEST', 'reserved': [0, 10 ** 400, 0, 0]}], [])['TEST']['reserved'],
                         (0, 16384, 0, 0))



if __name__ == '__main__':
    unittest.main(verbosity=2)
