#!/usr/bin/env python3
# SPDX-License-Identifier: 0BSD
"""Actual menu slider handlers through private controller preference plumbing."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import controller


@unittest.skipUnless(shutil.which('node'), 'Node is required to exercise the real QML handlers')
class BotSpeedTests(unittest.TestCase):
    def test_menu_edits_preserve_live_game_and_persist_across_ownership_and_restart(self):
        with tempfile.TemporaryDirectory(prefix='doom-bot-speed-') as directory:
            base = Path(directory)
            home = base / 'home'
            home.mkdir()
            environment = {'HOME': str(home), 'XDG_CONFIG_HOME': str(home / '.config'),
                           'XDG_DATA_HOME': str(home / '.local/share'),
                           'XDG_STATE_HOME': str(home / '.local/state')}
            with patch.dict(os.environ, environment), patch('controller.Path.home', return_value=home):
                ctrl = controller.Controller(base / 'runtime', base / 'state', headless=True)
                self.assertEqual(ctrl.settings_json()['bot']['speed'], 100)
                # Execute unchanged production row construction and input
                # handlers. Only unrelated menu sections and transport are
                # replaced; their emitted dotted-key edits go to Controller.
                script = r'''
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const context = {root: {settings: JSON.parse(process.argv[2]), busy: false,
  phobosOffer() { return null; }, officialRows() {}, freeRows() {},
  basename(value) { return String(value).split('/').pop(); },
  getv(value, key) { return key.split('.').reduce((item, part) => item[part], value); },
  edits: [], setValue(key, value) { this.edits.push('set ' + key + ' ' + JSON.stringify(value)); }}};
vm.createContext(context);
for (const name of ['buildRows', 'adjust', 'sliderPick']) {
  const start = source.indexOf('  function ' + name + '(');
  const end = source.indexOf('\n  }', start);
  assert(start >= 0 && end > start, 'Missing production handler: ' + name);
  vm.runInContext(source.slice(start, end + 4), context);
}
const row = context.buildRows(context.root.settings).find(row => row.key === 'bot.speed');
assert(row && row.label === 'Bot speed' && row.min === 25 && row.max === 100 && row.step === 5);
context.adjust(row, -1);
context.sliderPick(row, 1 / 3);
console.log(JSON.stringify(context.root.edits));
'''
                result = subprocess.run(['node', '-e', script, str(ROOT / 'menu/Menu.qml'),
                                         json.dumps(ctrl.settings_json())],
                                        capture_output=True, text=True, timeout=5, check=True)
                edits = json.loads(result.stdout)
                self.assertEqual(edits, ['set bot.speed 95', 'set bot.speed 50'])
                ctrl.engine = Mock()
                ctrl.engine.poll.return_value = None
                ctrl.log = Mock()
                ctrl.native_schema = 3
                ctrl.render_size = controller.render_dimensions(ctrl.config, headless=True)
                ctrl.last_controls = (1.0, False)
                engine = ctrl.engine
                with patch('controller.validate_wad'), \
                        patch('controller.read_header', return_value={'human_quits': 0}), \
                        patch('controller.engine_request', return_value='OK') as native, \
                        patch('controller.engine_request_batch', side_effect=lambda lines, _: ['OK'] * len(lines)), \
                        patch.object(ctrl.agents, 'tick'), \
                        patch.object(ctrl, 'start_engine') as start, \
                        patch.object(ctrl, 'unload_engine') as unload:
                    ctrl.policy()
                    for edit in edits:
                        settings = json.loads(ctrl.command(edit))
                        self.assertEqual(settings['bot']['speed'], json.loads(edit.split()[-1]))
                        self.assertIs(ctrl.engine, engine)
                    self.assertEqual(ctrl.command('takeover TEST'), 'OK')
                    self.assertEqual(ctrl.mode, 'human')
                    # GTK's raw configure API also retains the preference
                    # while the human keeps ownership of the same game.
                    self.assertEqual(ctrl.command('configure {"bot_speed": 40}'), 'OK')
                    self.assertEqual(ctrl.mode, 'human')
                    self.assertEqual(ctrl.command('bot'), 'OK')
                    self.assertEqual(ctrl.mode, 'bot')
                    ctrl.saver = True
                    ctrl.policy()
                    self.assertEqual(ctrl.mode, 'screensaver')
                    self.assertEqual(ctrl.command('set bot.speed 27'),
                                     'ERR Bot speed must be between 25 and 100% in steps of 5%.')
                    speeds = [call.args[0] for call in native.call_args_list if call.args[0].startswith('bot-speed ')]
                    self.assertEqual(speeds, ['bot-speed 95', 'bot-speed 50', 'bot-speed 40'])
                    start.assert_not_called()
                    unload.assert_not_called()
                for path in (ctrl.config_path, ctrl.accepted_path):
                    self.assertEqual(json.loads(path.read_text())['bot_speed'], 40)
                restarted = controller.Controller(base / 'runtime', base / 'state', headless=True)
                self.assertEqual(restarted.settings_json()['bot']['speed'], 40)
                self.assertIsNone(restarted.human_output)


if __name__ == '__main__':
    unittest.main(verbosity=2)
