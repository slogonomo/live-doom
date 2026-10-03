# SPDX-License-Identifier: 0BSD
"""Bounded, nonblocking clients for control requests and ordered input leases."""
from __future__ import annotations

import selectors
import socket
import subprocess
import time

MAX_REPLY_BYTES = 1024 * 1024


class ControlServer:
    def __init__(self, controller, listener):
        self.controller = controller
        self.listener = listener
        self.selector = selectors.DefaultSelector()
        self.clients = {}
        listener.setblocking(False)
        self.selector.register(listener, selectors.EVENT_READ)

    def close_client(self, sock, revoke=True):
        client = self.clients.pop(sock, None)
        if client is None:
            return
        self.selector.unregister(sock)
        sock.close()
        if client.get('agent'):
            self.controller.agents.disconnect(sock)
            if self.controller.alive:
                self.controller.policy()
        if (revoke and self.controller.alive and client['lease'] is not None and
                client['lease'] == (self.controller.human_output, self.controller.input_generation)):
            self.controller.return_bot()
            self.controller.policy()

    def reply(self, sock, lines, close=False):
        client = self.clients[sock]
        # Game changes can legitimately take longer than the idle-client
        # deadline. Start reply delivery's lifetime after work completes.
        client['last'] = time.monotonic()
        client['out'].extend(('\n'.join(lines) + '\n').encode())
        client['closing'] |= close
        if len(client['out']) > MAX_REPLY_BYTES:
            self.close_client(sock)
            return
        events = selectors.EVENT_WRITE | (0 if client['closing'] else selectors.EVENT_READ)
        self.selector.modify(sock, events)

    def read(self, sock):
        client = self.clients[sock]
        try:
            chunk = sock.recv(4096)
        except BlockingIOError:
            return
        except OSError:
            self.close_client(sock)
            return
        if not chunk:
            self.close_client(sock)
            return
        client['last'] = time.monotonic()
        client['in'].extend(chunk)
        if len(client['in']) > 8192:
            self.close_client(sock)
            return
        lines = []
        while b'\n' in client['in']:
            raw, _, rest = client['in'].partition(b'\n')
            client['in'] = bytearray(rest)
            try:
                lines.append(raw.decode())
            except UnicodeDecodeError:
                self.close_client(sock)
                return
        if not lines:
            return
        if client.get('agent'):
            self.read_agent(sock, lines)
            return
        if client['lease'] is None:
            first = lines.pop(0)
            parts = first.split()
            if len(parts) >= 4 and parts[:2] == ['agent', 'hello']:
                self.controller.policy()
                name = first.split(None, 2)[2].rsplit(None, 1)[0]
                result = ('ERR native schema' if self.controller.engine_loaded() and self.controller.native_schema < 3
                          else self.controller.agents.hello(name, parts[-1], sock))
                success = result.startswith('OK')
                client['agent'] = success
                self.reply(sock, [result], close=not success)
                if success:
                    self.controller.policy()
                    self.flush_agent(sock)
                    if lines and sock in self.clients:
                        self.read_agent(sock, lines)
                return
            if len(parts) == 2 and parts[0] == 'input':
                self.controller.policy()
                if parts[1] != self.controller.human_output:
                    self.reply(sock, ['ERR input requires desktop takeover'], close=True)
                    return
                if any(old != sock and state['lease'] == (parts[1], self.controller.input_generation)
                       for old, state in self.clients.items()):
                    self.reply(sock, ['ERR input stream already active'], close=True)
                    return
                client['lease'] = (parts[1], self.controller.input_generation)
                self.reply(sock, ['OK'])
            else:
                try:
                    result = self.controller.command(first)
                except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
                    result = 'ERR ' + str(exc)
                self.reply(sock, [result], close=True)
                return
        while lines and sock in self.clients:
            batch, lines = lines[:128], lines[128:]
            output, generation = client['lease']
            try:
                results = self.controller.input_batch(batch, output, generation)
            except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
                self.controller.return_bot()
                self.controller.policy()
                results = ['ERR input transport: ' + str(exc)] * len(batch)
            expired = (output, generation) != (self.controller.human_output, self.controller.input_generation)
            self.reply(sock, results, close=expired)
            if expired:
                break

    def flush_agent(self, sock):
        if sock not in self.clients or not self.clients[sock].get('agent'):
            return
        events = self.controller.agents.drain_events(sock)
        closing = self.controller.agents.close_requested(sock)
        if events:
            self.reply(sock, events, close=closing)
        elif closing:
            if self.clients[sock]['out']:
                self.clients[sock]['closing'] = True
                self.selector.modify(sock, selectors.EVENT_WRITE)
            else:
                self.close_client(sock)

    def read_agent(self, sock, lines):
        for line in lines:
            if sock not in self.clients or self.clients[sock]['closing']:
                return
            self.controller.policy()
            try:
                result = self.controller.agents.handle(line, sock)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                self.controller.agents.disconnect(sock)
                self.reply(sock, ['ERR native'], close=True)
                self.controller.policy()
                return
            self.reply(sock, [result])
            self.controller.policy()
            self.flush_agent(sock)

    def prepare_shutdown(self):
        # Pump only already-queued replies/events. Do not accept or execute
        # fresh work while the engine and supervised children are stopping.
        self.selector.unregister(self.listener)
        for sock in list(self.clients):
            self.flush_agent(sock)
            if sock not in self.clients:
                continue
            if not self.clients[sock]['out']:
                self.close_client(sock, revoke=False)
            else:
                self.clients[sock]['closing'] = True
                self.selector.modify(sock, selectors.EVENT_WRITE)

    def poll(self, timeout=0.1, policy=True):
        if policy:
            self.controller.agents.tick()
            self.controller.policy()
        for sock in list(self.clients):
            self.flush_agent(sock)
        if policy:
            timeout = self.controller.agents.lease_wait(timeout)
        for key, events in self.selector.select(timeout):
            sock = key.fileobj
            if sock == self.listener:
                for _ in range(32):
                    try:
                        conn, _ = self.listener.accept()
                    except BlockingIOError:
                        break
                    if len(self.clients) >= 64:
                        conn.close()
                        continue
                    conn.setblocking(False)
                    self.clients[conn] = {'in': bytearray(), 'out': bytearray(), 'last': time.monotonic(),
                                          'lease': None, 'agent': False, 'closing': False}
                    self.selector.register(conn, selectors.EVENT_READ)
                continue
            if sock not in self.clients:
                continue
            if events & selectors.EVENT_READ:
                self.read(sock)
            if sock in self.clients and events & selectors.EVENT_WRITE:
                client = self.clients[sock]
                try:
                    count = sock.send(client['out'])
                    del client['out'][:count]
                except BlockingIOError:
                    pass
                except OSError:
                    self.close_client(sock)
                    continue
                if not client['out']:
                    if client['closing']:
                        self.close_client(sock)
                    else:
                        self.selector.modify(sock, selectors.EVENT_READ)
        now = time.monotonic()
        for sock, client in list(self.clients.items()):
            # A revoked batch can have a final error queued for delivery. Its
            # closing socket accepts no more input and remains bounded by the
            # stalled-reply deadline; do not discard that reply here.
            expired = (not client['closing'] and client['lease'] is not None and
                       client['lease'] != (self.controller.human_output, self.controller.input_generation))
            partial_timeout = not client['closing'] and client['in'] and now - client['last'] > 0.5
            unused_timeout = client['lease'] is None and not client.get('agent') and not client['closing'] and not client['out'] and now - client['last'] > 1
            stalled_reply = client['out'] and now - client['last'] > 2
            if expired or partial_timeout or unused_timeout or stalled_reply:
                self.close_client(sock)

    def close(self):
        for sock in list(self.clients):
            self.close_client(sock, revoke=False)
        self.selector.close()
