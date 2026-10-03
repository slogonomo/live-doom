# SPDX-License-Identifier: 0BSD
"""Private XDG fixtures: no production services, inputs or home mutations."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from project_paths import (APP_NAME, PLUGIN_ID, ProjectPaths, RotatingProcessLog,
                           atomic_json, atomic_write, close_process_log, ensure_private_dir,
                           flush_process_log, open_regular, read_bytes, read_json,
                           spawn_logged)


class ProjectPathsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="live-doom-paths-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "fixture home"
        self.home.mkdir(mode=0o700)

    def test_all_artifacts_are_outside_checkout_and_explicit_home_ignores_host_xdg(self):
        with patch.dict(os.environ, {"XDG_DATA_HOME": "/ignored-host-data"}):
            paths = ProjectPaths.from_env(home=self.home)
        self.assertEqual(PLUGIN_ID, "io.github.slogonomo.live-doom")
        self.assertEqual(paths.data_root, self.home / ".local/share/live-doom")
        self.assertEqual(paths.cache_root, self.home / ".cache/live-doom")
        self.assertEqual(paths.config_file, self.home / ".config/live-doom/config.json")
        self.assertEqual(paths.last_good_config, self.home / ".config/live-doom/config.last-good.json")
        self.assertEqual(paths.state_root, self.home / ".local/state/live-doom")
        self.assertEqual(paths.engine, paths.data_root / "bin/eternity")
        self.assertEqual(paths.viewer, paths.data_root / "bin/doom-viewer")
        self.assertEqual(paths.engine_base, paths.data_root / "vendor/autodoom/base")
        self.assertEqual(paths.build_root, paths.cache_root / "build")
        self.assertEqual(paths.deps_root, paths.data_root / "deps")
        self.assertEqual(paths.default_iwad, paths.data_root / "assets/freedoom-0.13.0/freedoom1.wad")
        self.assertEqual(paths.games_root, paths.data_root / "games")
        self.assertEqual(paths.catalog_file, paths.data_root / "steam-catalog.json")
        self.assertEqual(paths.agent_config_root, paths.config_root / "agents")
        self.assertEqual(paths.agent_state_root, paths.state_root / "agents")
        self.assertEqual(paths.legacy_data_root, self.home / ".local/share/doom-desktop")
        self.assertEqual(list(self.home.iterdir()), [], "Pure path lookup must not create/migrate files")

    def test_xdg_overrides_are_absolute_and_never_escape_via_dotdot(self):
        env = {name: str(self.root / name) for name in
               ("XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME")}
        paths = ProjectPaths.from_env(home=self.home, env=env)
        self.assertEqual(paths.data_root, self.root / "XDG_DATA_HOME" / APP_NAME)
        for value in ("relative", str(self.root / "x/../outside")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ProjectPaths.from_env(home=self.home, env={"XDG_DATA_HOME": value})

    def test_runtime_missing_does_not_fall_back_to_tmp_or_legacy(self):
        paths = ProjectPaths.from_env(home=self.home)
        with self.assertRaisesRegex(ValueError, "XDG_RUNTIME_DIR is required"):
            paths.runtime_root()
        self.assertFalse((self.home / ".local/share/live-doom").exists())

    def test_runtime_parent_must_be_existing_owned_0700_without_symlinks(self):
        runtime = self.root / "runtime"
        runtime.mkdir(mode=0o700)
        paths = ProjectPaths.from_env(home=self.home, env={"XDG_RUNTIME_DIR": str(runtime)})
        leaf = paths.runtime_root()
        self.assertEqual(leaf, runtime / "live-doom")
        self.assertEqual(leaf.stat().st_mode & 0o777, 0o700)
        self.assertEqual(paths.runtime_root(create=False), leaf)
        runtime.chmod(0o755)
        with self.assertRaises(PermissionError):
            paths.runtime_root()
        runtime.chmod(0o700)
        with patch("project_paths.os.getuid", return_value=os.getuid() + 1), self.assertRaises(PermissionError):
            paths.runtime_root()
        alias = self.root / "runtime-alias"
        alias.symlink_to(runtime, target_is_directory=True)
        with self.assertRaises(OSError):
            ProjectPaths.from_env(home=self.home, env={"XDG_RUNTIME_DIR": str(alias)}).runtime_root()
        missing = self.root / "missing"
        with self.assertRaises(FileNotFoundError):
            ProjectPaths.from_env(home=self.home, env={"XDG_RUNTIME_DIR": str(missing)}).runtime_root()

    def test_runtime_explicit_overrides_are_checked_and_primary_wins(self):
        primary, legacy = self.root / "explicit", self.root / "old-explicit"
        paths = ProjectPaths.from_env(home=self.home, env={"LIVE_DOOM_RUNTIME": str(primary),
                                                       "DOOM_DESKTOP_RUNTIME": str(legacy)})
        self.assertEqual(paths.runtime_root(), primary)
        self.assertFalse(legacy.exists())
        old = ProjectPaths.from_env(home=self.home, env={"DOOM_DESKTOP_RUNTIME": str(legacy)})
        self.assertEqual(old.runtime_root(), legacy)
        legacy.chmod(0o777)
        with self.assertRaises(PermissionError):
            old.runtime_root()

    def test_runtime_child_symlink_and_wrong_permissions_are_rejected(self):
        parent = self.root / "runtime"
        parent.mkdir(mode=0o700)
        paths = ProjectPaths.from_env(home=self.home, env={"XDG_RUNTIME_DIR": str(parent)})
        (parent / "live-doom").symlink_to(self.home, target_is_directory=True)
        with self.assertRaises(OSError):
            paths.runtime_root()
        (parent / "live-doom").unlink()
        (parent / "live-doom").mkdir(mode=0o755)
        with self.assertRaises(PermissionError):
            paths.runtime_root()

    def test_shell_paths_are_quoted_and_queries_do_not_require_runtime(self):
        result = subprocess.run([sys.executable, "-B", str(ROOT / "src/project_paths.py"),
                                 "--home", str(self.home), "--shell"], capture_output=True,
                                text=True, timeout=5, check=True)
        values = {}
        for line in result.stdout.splitlines():
            name, value = line.split("=", 1)
            values[name] = shlex.split(value)[0]
        self.assertEqual(values["LIVE_DOOM_DEFAULT_IWAD"], str(ProjectPaths.from_env(home=self.home).default_iwad))
        self.assertEqual(values["LIVE_DOOM_PLUGIN_ID"], PLUGIN_ID)
        self.assertEqual(list(self.home.iterdir()), [])


class SafeFileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="live-doom-files-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_bounded_reads_reject_size_growth_nonregular_symlinks_and_wrong_owner(self):
        file = self.root / "small"
        file.write_bytes(b"abc")
        self.assertEqual(read_bytes(file, 3), b"abc")
        with self.assertRaises(ValueError):
            read_bytes(file, 2)
        with patch("project_paths.os.getuid", return_value=os.getuid() + 1), self.assertRaises(PermissionError):
            read_bytes(file, 3)
        self.assertEqual(read_bytes(file, 3, require_owner=False), b"abc")
        alias = self.root / "alias"
        alias.symlink_to(file)
        with self.assertRaises(OSError):
            read_bytes(alias, 3)
        directory = self.root / "folder"
        directory.mkdir()
        with self.assertRaises(ValueError):
            read_bytes(directory, 3)
        fifo = self.root / "fifo"
        os.mkfifo(fifo)
        started = time.monotonic()
        with self.assertRaises(ValueError):
            read_bytes(fifo, 3)
        self.assertLess(time.monotonic() - started, 1, "FIFO validation must not wait for a writer")
        for limit in (-1, None, float("inf"), True):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                read_bytes(file, limit)

    def test_ancestor_symlinks_do_not_bypass_nofollow(self):
        directory = self.root / "real"
        directory.mkdir(mode=0o700)
        (directory / "file").write_bytes(b"a")
        alias = self.root / "alias"
        alias.symlink_to(directory, target_is_directory=True)
        for action in (lambda: read_bytes(alias / "file", 1),
                       lambda: atomic_write(alias / "output", b"a"),
                       lambda: ensure_private_dir(alias / "child")):
            with self.assertRaises(OSError):
                action()
        self.assertFalse((directory / "output").exists())
        self.assertFalse((directory / "child").exists())

    def test_file_growth_after_stat_cannot_exceed_read_cap(self):
        path = self.root / "growing"
        path.write_bytes(b"abc")

        @contextmanager
        def growing_file(*args, **kwargs):
            with open_regular(*args, **kwargs) as handle:
                class GrowingReader:
                    def fileno(self):
                        return handle.fileno()

                    def read(self, count):
                        with path.open("ab") as writer:
                            writer.write(b"d")
                        return handle.read(count)

                yield GrowingReader()

        with patch("project_paths.open_regular", growing_file), self.assertRaisesRegex(ValueError, "grew past"):
            read_bytes(path, 3)

    def test_atomic_json_is_0600_unpredictable_and_replaces_without_temp_leftovers(self):
        path = self.root / "state" / "config.json"
        with patch("project_paths.tempfile.mkstemp", wraps=tempfile.mkstemp) as create:
            atomic_json(path, {"name": "Doom", "value": 1})
            self.assertEqual(create.call_count, 1)
        self.assertEqual(read_json(path), {"name": "Doom", "value": 1})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        old_inode = path.stat().st_ino
        atomic_json(path, {"value": 2})
        self.assertNotEqual(path.stat().st_ino, old_inode)
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_atomic_output_rejects_symlink_hardlink_unsafe_parent_and_oversize(self):
        target = self.root / "target"
        target.write_bytes(b"untouched")
        alias = self.root / "alias"
        alias.symlink_to(target)
        with self.assertRaises(ValueError):
            atomic_write(alias, b"replacement")
        linked = self.root / "linked"
        os.link(target, linked)
        with self.assertRaises(PermissionError):
            atomic_write(target, b"replacement")
        self.assertEqual(target.read_bytes(), b"untouched")
        public = self.root / "unsafe"
        public.mkdir(mode=0o777)
        public.chmod(0o777)
        with self.assertRaises(PermissionError):
            atomic_write(public / "new", b"a")
        with self.assertRaises(ValueError):
            atomic_write(self.root / "large", b"ab", max_bytes=1)
        with self.assertRaises(ValueError):
            atomic_json(self.root / "nan", {"number": float("nan")})
        self.assertFalse((self.root / "large").exists())

    def test_failed_replace_cleans_temp_and_preserves_previous_file(self):
        path = self.root / "config.json"
        atomic_write(path, b"previous")
        with patch("project_paths.os.replace", side_effect=OSError("private injected failure")):
            with self.assertRaises(OSError):
                atomic_write(path, b"next")
        self.assertEqual(path.read_bytes(), b"previous")
        self.assertEqual(list(self.root.iterdir()), [path])


class ProcessLogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="live-doom-logs-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_live_stdout_and_stderr_flood_remain_bounded_and_final_lines_survive(self):
        path = self.root / "engine.log"
        marker = self.root / "flood-complete"
        program = ("import os,time,sys; "
                   "[os.write(1, b'x'*1024) for _ in range(200)]; "
                   "os.write(2,b'FINAL STDERR\\n'); open(sys.argv[1],'w').close(); time.sleep(.2)")
        process = spawn_logged([sys.executable, "-B", "-c", program, str(marker)], path,
                               log_max_bytes=2048, log_backups=2, stdin=subprocess.DEVNULL)
        self.addCleanup(close_process_log, process)
        try:
            deadline = time.monotonic() + 2
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertTrue(marker.exists())
            self.assertIsNone(process.poll(), "Size bounds must hold before child exit")
            for file in self.root.glob("engine.log*"):
                self.assertLessEqual(file.stat().st_size, 2048)
            process.wait(timeout=5)
            self.assertTrue(flush_process_log(process, timeout=1))
            self.assertIsNone(process._live_doom_log.error)
            files = sorted(self.root.glob("engine.log*"))
            self.assertEqual(len(files), 3)
            for file in files:
                self.assertLessEqual(file.stat().st_size, 2048)
                self.assertEqual(file.stat().st_mode & 0o777, 0o600)
            self.assertIn(b"FINAL STDERR\n", path.read_bytes())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)

    def test_existing_logs_are_capped_and_single_file_mode_rotates_in_place(self):
        path = self.root / "agent.log"
        path.write_bytes(b"old" * 1000)
        backup = self.root / "agent.log.1"
        backup.write_bytes(b"backup" * 1000)
        sink = RotatingProcessLog(path, max_bytes=64, backups=1)
        try:
            os.write(sink.writer, b"n" * 130 + b"END")
            self.assertTrue(sink.finish(1))
            self.assertEqual(path.read_bytes(), b"nnEND")
            self.assertEqual(backup.stat().st_size, 64)
        finally:
            sink.close()
        one = RotatingProcessLog(self.root / "one.log", max_bytes=4, backups=0)
        try:
            os.write(one.writer, b"0123456789")
            self.assertTrue(one.finish(1))
            self.assertEqual((self.root / "one.log").read_bytes(), b"89")
            self.assertFalse((self.root / "one.log.1").exists())
        finally:
            one.close()

    def test_log_symlinks_fifos_and_failed_spawn_fail_cleanly(self):
        target = self.root / "target"
        target.write_bytes(b"untouched")
        path = self.root / "engine.log"
        path.symlink_to(target)
        with self.assertRaises(ValueError):
            RotatingProcessLog(path)
        self.assertEqual(target.read_bytes(), b"untouched")
        path.unlink()
        os.mkfifo(path)
        with self.assertRaises(ValueError):
            RotatingProcessLog(path)
        path.unlink()
        with self.assertRaises(FileNotFoundError):
            spawn_logged([str(self.root / "nonexistent")], path)
        self.assertTrue(path.is_file())
        with self.assertRaises(ValueError):
            spawn_logged(["false"], path, stdout=subprocess.DEVNULL)

    def test_log_disk_error_keeps_draining_without_deadlocking_child(self):
        program = "import os; [os.write(1,b'x'*65536) for _ in range(20)]"
        with patch.object(RotatingProcessLog, "_append", side_effect=OSError("private disk failure")):
            process = spawn_logged([sys.executable, "-B", "-c", program], self.root / "failed.log",
                                   log_max_bytes=1024, log_backups=1)
            try:
                self.assertEqual(process.wait(timeout=5), 0)
                self.assertTrue(flush_process_log(process, timeout=1))
                self.assertIn("private disk failure", process._live_doom_log.error)
                self.assertLessEqual((self.root / "failed.log").stat().st_size, 1024)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=3)
                close_process_log(process)


if __name__ == "__main__":
    unittest.main()
