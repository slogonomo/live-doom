#!/usr/bin/env python3
# SPDX-License-Identifier: 0BSD
"""Real selector/socket regressions for request failures without desktop clients."""
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from control_server import ControlServer


class FakeController:
    def __init__(self):
        self.alive = True
        self.agents = SimpleNamespace(tick=lambda: None, lease_wait=lambda timeout: timeout)
        self.human_output = 'TEST'
        self.input_generation = 1
        self.returned = 0
        self.policies = 0

    def command(self, line):
        if line == 'settings':
            raise subprocess.TimeoutExpired(['omarchy-shell', 'plugin', 'open'], 2)
        if line == 'status':
            return 'OK status'
        raise ValueError('unknown command')

    def input_batch(self, lines, output, generation):
        raise subprocess.TimeoutExpired(['native-input'], 1)

    def return_bot(self):
        self.returned += 1
        self.human_output = None
        self.input_generation += 1

    def policy(self):
        self.policies += 1


class SubprocessFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='doom-control-test-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'control.sock'
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(self.listener.close)
        self.listener.bind(str(self.path))
        self.listener.listen()
        self.controller = FakeController()
        self.server = ControlServer(self.controller, self.listener)
        self.addCleanup(self.server.close)

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(sock.close)
        sock.connect(str(self.path))
        sock.setblocking(False)
        return sock

    def responses(self, sock, count):
        data = bytearray()
        deadline = time.monotonic() + 2
        while data.count(b'\n') < count and time.monotonic() < deadline:
            self.server.poll(0.01)
            try:
                chunk = sock.recv(65536)
            except BlockingIOError:
                continue
            if not chunk:
                break
            data.extend(chunk)
        lines = data.decode().splitlines()
        self.assertEqual(len(lines), count, lines)
        return lines

    def request(self, line):
        sock = self.connect()
        sock.sendall((line + '\n').encode())
        return self.responses(sock, 1)[0]

    def test_command_timeout_returns_error_then_status_still_works(self):
        error = self.request('settings')
        self.assertTrue(error.startswith('ERR '), error)
        self.assertIn('timed out', error)
        self.assertEqual(self.request('status'), 'OK status')
        self.assertEqual(self.controller.returned, 0)

    def test_batch_timeout_releases_lease_then_status_still_works(self):
        stream = self.connect()
        stream.sendall(b'input TEST\n')
        self.assertEqual(self.responses(stream, 1), ['OK'])
        stream.sendall(b'key 119 1\nmouse 1 0\n')
        errors = self.responses(stream, 2)
        self.assertTrue(all(line.startswith('ERR input transport: ') for line in errors), errors)
        self.assertTrue(all('timed out' in line for line in errors), errors)
        self.assertIsNone(self.controller.human_output)
        self.assertEqual(self.controller.returned, 1)
        self.assertGreaterEqual(self.controller.policies, 2)
        self.assertEqual(self.request('status'), 'OK status')

    def test_shutdown_drains_human_reply_without_native_policy_callbacks(self):
        stream = self.connect()
        stream.sendall(b'input TEST\n')
        self.assertEqual(self.responses(stream, 1), ['OK'])
        owned_socket = next(iter(self.server.clients))
        self.server.reply(owned_socket, ['OK final'], close=True)
        self.controller.alive = False
        self.controller.policy = Mock(side_effect=OSError('native engine has exited'))
        self.controller.return_bot = Mock(side_effect=OSError('native engine has exited'))
        self.server.prepare_shutdown()
        self.server.poll(0.01, policy=False)
        self.assertEqual(stream.recv(1024), b'OK final\n')
        self.assertEqual(self.server.clients, {})
        self.controller.policy.assert_not_called()
        self.controller.return_bot.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
