# SPDX-License-Identifier: 0BSD
"""Small, bounded subprocess calls with an explicit desktop environment."""
from __future__ import annotations

import math
import os
import selectors
import signal
import subprocess
import time


_SESSION_VARIABLES = frozenset({
    'HOME', 'XDG_RUNTIME_DIR', 'WAYLAND_DISPLAY', 'HYPRLAND_INSTANCE_SIGNATURE',
    'DBUS_SESSION_BUS_ADDRESS', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME',
    'XDG_CACHE_HOME', 'XDG_STATE_HOME', 'XDG_CURRENT_DESKTOP',
    'XDG_SESSION_TYPE', 'LANG', 'LC_ALL',
})


def session_environment() -> dict[str, str]:
    """Retain desktop addresses and XDG locations, without executable overrides."""
    result = {name: value for name, value in os.environ.items() if name in _SESSION_VARIABLES}
    result.update(PATH='/usr/bin:/bin', PYTHONDONTWRITEBYTECODE='1',
                  OMARCHY_PATH='/usr/share/omarchy')
    return result


def _kill_owned_group(process: subprocess.Popen):
    # start_new_session guarantees that this process's pid names its own group,
    # including descendants that kept our pipes open after the parent exited.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        # Keep teardown bounded even if a killed child is stuck in kernel I/O.
        # Popen retains unreaped children for its subsequent cleanup pass.
        pass


def bounded_run(argv, *, timeout=2, max_output_bytes=1024 * 1024,
                text=False, env=None, cwd=None, check=False) -> subprocess.CompletedProcess:
    """Run an absolute command with a deadline and combined stdout/stderr cap.

    Both pipes are drained independently and returned separately. A deadline
    raises TimeoutExpired with partial bytes, matching subprocess.run; an
    output overflow raises ValueError. Either failure kills this call's owned
    process group. env=None uses session_environment(), never an inherited
    executable, Python, Git, or dynamic-loader override. Explicit env is used
    as supplied. text=True decodes completed output as UTF-8 with replacement.
    """
    if isinstance(argv, (str, bytes)):
        raise ValueError('Process arguments must be a sequence')
    command = list(argv)
    if not command or not os.path.isabs(command[0]):
        raise ValueError('Process command must be an absolute path')
    if (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError('Process timeout must be finite and positive')
    if type(max_output_bytes) is not int or max_output_bytes < 0:
        raise ValueError('Process output limit must be a nonnegative integer')
    if type(text) is not bool or type(check) is not bool:
        raise ValueError('Process text and check options must be booleans')

    deadline = time.monotonic() + timeout
    output, error = bytearray(), bytearray()
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               stdin=subprocess.DEVNULL, start_new_session=True,
                               env=session_environment() if env is None else env, cwd=cwd)

    def expired():
        return subprocess.TimeoutExpired(command, timeout, output=bytes(output), stderr=bytes(error))

    try:
        with selectors.DefaultSelector() as selector:
            for stream, buffer in ((process.stdout, output), (process.stderr, error)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, buffer)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise expired()
                for key, _ in selector.select(remaining):
                    try:
                        block = os.read(key.fileobj.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    if len(output) + len(error) + len(block) > max_output_bytes:
                        raise ValueError('Process output exceeds its byte limit')
                    key.data.extend(block)
        try:
            returncode = process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise expired() from None
    except BaseException:
        _kill_owned_group(process)
        raise
    finally:
        process.stdout.close()
        process.stderr.close()

    stdout, stderr = bytes(output), bytes(error)
    if text:
        stdout, stderr = stdout.decode('utf-8', errors='replace'), stderr.decode('utf-8', errors='replace')
    result = subprocess.CompletedProcess(command, returncode, stdout, stderr)
    if check:
        result.check_returncode()
    return result
