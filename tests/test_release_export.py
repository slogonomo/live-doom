# SPDX-License-Identifier: 0BSD
"""Synthetic local checkouts only; no real release copy, network or publish."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_export", ROOT / "scripts/export-release.py")
release = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = release
spec.loader.exec_module(release)


def png() -> bytes:
    def chunk(name, data):
        return struct.pack(">I", len(data)) + name + data + struct.pack(">I", zlib.crc32(name + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\0\0\0\0")) + chunk(b"IEND", b""))


class ReleaseExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="live-doom-export-unit-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "source"
        self.destination = self.base / "candidate"
        self.source.mkdir(mode=0o700)
        self.destination.mkdir(mode=0o700)
        self.git("init", "-q")
        manifest = {"schemaVersion": 1, "id": release.PLUGIN_ID, "name": "Live Doom", "version": "1.0.0",
                    "kinds": ["overlay", "service"],
                    "entryPoints": {"overlay": "menu/Menu.qml", "service": "service/Service.qml"}}
        self.put("manifest.json", json.dumps(manifest).encode())
        self.put("README.md", b"# Live Doom\nhttps://github.com/slogonomo/live-doom\n[Licence](LICENSE)\n")
        self.put("LICENSE", b"0BSD synthetic fixture\n")
        self.put("THIRD-PARTY-NOTICES.md", b"Synthetic notices\n")
        self.put("patches/sdl-mixer-device.patch", b"# Synthetic mixer adapter fixture\n")
        for name in ("0BSD", "CC0-1.0", "GPL-3.0-or-later", "Zlib"):
            self.put("LICENSES/" + name + ".txt", b"Synthetic license fixture\n")
        self.put("preview.png", png())
        self.put("assets/live-doom.webp", b"RIFF" + struct.pack("<I", 12) + b"WEBPVP8L" + b"\0" * 4)
        self.put("menu/Menu.qml", b"import QtQuick\nItem {}\n")
        self.put("service/Service.qml", b"import QtQuick\nItem {}\n")
        self.put("src/example.py", b"print('portable')\n")
        self.put("scripts/doomctl", b"#!/bin/sh\nexit 0\n", 0o755)
        self.put(".gitignore", b"build/\n__pycache__/\n*.pyc\nsrc/ignored.py\n")
        self.git("add", ".")

    def put(self, relative, data, mode=0o644):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)
        return path

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.source), *args], check=True, capture_output=True,
                       timeout=5, env=os.environ | {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})

    def test_default_dry_run_is_read_only_and_accepts_precommit_reviewed_source(self):
        self.put("src/new.py", b"# prepared but not yet committed\n")
        inventory = release.export(self.source, self.destination)
        self.assertIn("src/new.py", {row["path"] for row in inventory["files"]})
        self.assertEqual(list(self.destination.iterdir()), [])
        self.assertFalse(self.destination.with_suffix(".inventory.json").exists())
        self.assertFalse((self.source / ".git/refs/heads/master").exists(), "No source commit is needed")

    def test_export_inventory_matches_bytes_and_executable_flags_without_history(self):
        before = {path.relative_to(self.source).as_posix(): path.read_bytes()
                  for path in self.source.rglob("*") if path.is_file() and ".git" not in path.parts}
        inventory = release.export(self.source, self.destination, write=True)
        rows = {row["path"]: row for row in inventory["files"]}
        exported = {path.relative_to(self.destination).as_posix(): path
                    for path in self.destination.rglob("*") if path.is_file()}
        self.assertEqual(set(rows), set(exported))
        for name, path in exported.items():
            self.assertEqual(rows[name]["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(rows[name]["bytes"], path.stat().st_size)
            self.assertEqual(rows[name]["mode"], f"{path.stat().st_mode & 0o777:04o}")
            self.assertFalse(path.is_symlink())
        self.assertEqual((self.destination / "scripts/doomctl").stat().st_mode & 0o111, 0o111)
        self.assertFalse((self.destination / ".git").exists())
        record = self.destination.with_name("candidate.inventory.json")
        self.assertEqual(json.loads(record.read_bytes()), inventory)
        self.assertEqual(record.stat().st_mode & 0o777, 0o600)
        after = {path.relative_to(self.source).as_posix(): path.read_bytes()
                 for path in self.source.rglob("*") if path.is_file() and ".git" not in path.parts}
        self.assertEqual(before, after)

    def test_ignored_artifacts_historical_probes_and_theme_are_never_copied(self):
        for name in ("docs/runtime/human.png", "theme/preview.png", "design/art/prototype.py",
                     "build/eternity", "src/__pycache__/example.pyc", "src/ignored.py",
                     "tests/installed_menu_probe.py", "tests/private_desktop_probe.py",
                     "scripts/build-virtual-click.sh", "docs/architecture.md",
                     "src/protocols/wlr-virtual-pointer-unstable-v1.xml"):
            self.put(name, b"excluded")
        self.put("tests/test_onboarding.py", b"# portable private fixture\n")
        self.put("docs/design/EXTERNAL-AGENT.md", b"# Public agent protocol\n")
        self.git("add", "-f", "src/ignored.py", "docs/runtime", "theme", "design", "tests", "docs/architecture.md")
        inventory = release.export(self.source, self.destination, write=True)
        names = {row["path"] for row in inventory["files"]}
        self.assertIn("tests/test_onboarding.py", names)
        self.assertIn("docs/design/EXTERNAL-AGENT.md", names)
        self.assertFalse(any(name.startswith(("theme/", "build/", "docs/runtime/", "design/")) for name in names))
        self.assertNotIn("src/ignored.py", names, "Even tracked ignored artifacts must be omitted")
        self.assertNotIn("tests/installed_menu_probe.py", names)
        self.assertNotIn("src/protocols/wlr-virtual-pointer-unstable-v1.xml", names)

    def test_current_controller_and_native_lifecycle_fixtures_are_exported(self):
        expected = {"tests/test_menu_commands.py", "tests/test_marketplace_controller.py",
                    "tests/test_native_lifecycle.py", "tests/test_process_utils.py",
                    "tests/test_mixer_device.py"}
        for name in expected:
            self.put(name, b"# portable private fixture\n")
        names = {file.path for file in release.prepare(self.source).files}
        self.assertTrue(expected <= names)

    def test_tracked_symlinks_outside_whitelist_abort_before_copy(self):
        target = self.put("theme/target", b"private")
        (self.source / "theme/link").symlink_to(target)
        self.git("add", "theme")
        with self.assertRaisesRegex(release.ExportError, "tracked symlink"):
            release.export(self.source, self.destination, write=True)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_current_tracked_symlink_and_untracked_payload_symlinks_are_rejected(self):
        original = self.source / "src/example.py"
        original.unlink()
        original.symlink_to(self.source / "README.md")
        with self.assertRaisesRegex(release.ExportError, "tracked symlink"):
            release.prepare(self.source)
        original.unlink()
        self.put("src/example.py", b"# restored\n")
        (self.source / "menu/new.qml").symlink_to(self.source / "menu/Menu.qml")
        with self.assertRaises(release.ExportError):
            release.prepare(self.source)

    def test_nested_case_manifest_and_missing_entrypoints_fail(self):
        nested = self.put("menu/MANIFEST.JSON", b"{}")
        with self.assertRaisesRegex(release.ExportError, "one root manifest"):
            release.prepare(self.source)
        nested.unlink()
        (self.source / "service/Service.qml").unlink()
        with self.assertRaisesRegex(release.ExportError, "entry point is absent"):
            release.prepare(self.source)

    def test_missing_release_sources_report_actionable_error_without_placeholders(self):
        (self.source / "preview.png").unlink()
        with self.assertRaisesRegex(release.ExportError, "preview.png.*no placeholders"):
            release.export(self.source, self.destination, write=True)
        self.assertEqual(list(self.destination.iterdir()), [])
        self.assertFalse((self.source / "preview.png").exists())

    def test_mixer_adapter_is_required_before_any_release_copy(self):
        (self.source / "patches/sdl-mixer-device.patch").unlink()
        with self.assertRaisesRegex(release.ExportError, "sdl-mixer-device.patch.*no placeholders"):
            release.export(self.source, self.destination, write=True)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_personal_paths_and_links_to_excluded_frames_block_export(self):
        self.put("README.md", ("Private path: /home/" + "private-person/secret\n").encode())
        with self.assertRaisesRegex(release.ExportError, "personal absolute home path"):
            release.prepare(self.source)
        self.put("README.md", b"[Historical screen](docs/runtime/desktop.png)\n")
        self.put("docs/runtime/desktop.png", png())
        with self.assertRaisesRegex(release.ExportError, "link target excluded/missing"):
            release.prepare(self.source)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_legacy_migration_id_is_reported_without_source_rewriting(self):
        data = b'LEGACY_MENU_ID = "live-doom-dev.menu"\n# old live-doom-dev.menu comment\n'
        path = self.put("scripts/legacy.py", data)
        inventory = release.export(self.source, self.destination)
        self.assertEqual(len(inventory["warnings"]), 1)
        self.assertIn("scripts/legacy.py:2", inventory["warnings"][0])
        self.assertEqual(path.read_bytes(), data)

    def test_destination_must_be_empty_external_owned_and_not_symlinked(self):
        (self.destination / ".hidden").write_bytes(b"keep")
        with self.assertRaisesRegex(release.ExportError, "empty"):
            release.export(self.source, self.destination, write=True)
        (self.destination / ".hidden").unlink()
        with self.assertRaisesRegex(release.ExportError, "outside"):
            release.export(self.source, self.source / "new")
        alias = self.base / "alias"
        alias.symlink_to(self.destination, target_is_directory=True)
        with self.assertRaises(OSError):
            release.export(self.source, alias)
        self.destination.chmod(0o777)
        with self.assertRaisesRegex(release.ExportError, "not writable"):
            release.export(self.source, self.destination)

    def test_inventory_name_is_never_overwritten_and_failed_copy_rolls_back(self):
        report = self.destination.with_name("candidate.inventory.json")
        report.write_bytes(b"existing proof")
        with self.assertRaisesRegex(release.ExportError, "Inventory path already exists"):
            release.export(self.source, self.destination, write=True)
        self.assertEqual(report.read_bytes(), b"existing proof")
        report.unlink()
        with patch.object(release.os, "fsync", side_effect=OSError("private injected copy failure")):
            with self.assertRaises(OSError):
                release.export(self.source, self.destination, write=True)
        self.assertEqual(list(self.destination.iterdir()), [])
        self.assertFalse(report.exists())

    def test_file_byte_and_count_limits_are_finite(self):
        with patch.object(release, "MAX_FILES", 1), self.assertRaisesRegex(release.ExportError, "file safety limit"):
            release.prepare(self.source)
        with patch.object(release, "MAX_FILE_BYTES", 1), self.assertRaisesRegex(release.ExportError, "per-file limit"):
            release.prepare(self.source)
        with patch.object(release, "MAX_TOTAL_BYTES", 1), self.assertRaisesRegex(release.ExportError, "byte safety limit"):
            release.prepare(self.source)

    def test_binary_disguised_as_source_and_nonregular_payload_are_rejected(self):
        fake = self.put("src/binary.py", b"\x7fELF\0payload")
        with self.assertRaisesRegex(release.ExportError, "binary/NUL"):
            release.prepare(self.source)
        fake.unlink()
        os.mkfifo(self.source / "src/fifo.py")
        # Git never indexes FIFOs: test the bounded regular-file reader itself.
        with self.assertRaisesRegex(release.ExportError, "regular file"):
            release._read(self.source, "src/fifo.py")


if __name__ == "__main__":
    unittest.main()
