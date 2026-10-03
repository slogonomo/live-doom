#!/usr/bin/env python3
# SPDX-License-Identifier: 0BSD
"""Steam discovery fixtures, isolated from the user's Steam and game settings."""
from __future__ import annotations

import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import steam_games
from steam_games import discover, parse_vdf, VDFError


def vdf_string(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"') + '"'


def wad(path, maps=("MAP01",), magic=b"IWAD"):
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = b"".join(struct.pack("<ii8s", 12, 0, name.encode()) for name in maps)
    path.write_bytes(struct.pack("<4sii", magic, len(maps), 12) + entries)
    return path


class VDFTests(unittest.TestCase):
    def test_modern_legacy_comments_escapes_bare_tokens(self):
        text = '// comment\nlibraryfolders { "0" { "path" "/home/user/Steam" "apps" {2280 0} } "1" "D:\\\\Steam Library" }'
        data = parse_vdf(text)
        self.assertEqual(data["libraryfolders"]["0"]["path"], "/home/user/Steam")
        self.assertEqual(data["libraryfolders"]["1"], "D:\\Steam Library")
        self.assertEqual(parse_vdf('"key" "value \\"quote\\""'), {"key": 'value "quote"'})

    def test_malformed_duplicate_and_bounded_documents(self):
        for text in ('"AppState" {"appid" "2280"', '"key"', '"key" }', '}',
                     '"a" "b" "a" "c"', '"key" "unfinished', '"x" "\\q"'):
            with self.subTest(text=text), self.assertRaises(VDFError):
                parse_vdf(text)
        with patch.object(steam_games, "MAX_VDF_BYTES", 20), self.assertRaises(VDFError):
            parse_vdf("x" * 21)
        with patch.object(steam_games, "MAX_VDF_TOKENS", 2), self.assertRaises(VDFError):
            parse_vdf('"a" "b" "c" "d"')
        with patch.object(steam_games, "MAX_VDF_DEPTH", 2), self.assertRaises(VDFError):
            parse_vdf('"a" {"b" {"c" {"d" "e"}}}')


class SteamDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="doom-steam-discovery-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.home.mkdir()
        self.steam = self.home / ".local/share/Steam"

    def install(self, appid, folder, library=None, title=None):
        library = self.steam if library is None else library
        apps = library / "steamapps"
        apps.mkdir(parents=True, exist_ok=True)
        path = apps / f"appmanifest_{appid}.acf"
        path.write_text('"AppState" {"appid" ' + vdf_string(appid)
                        + ' "name" ' + vdf_string(title or steam_games.APP_TITLES[appid])
                        + ' "installdir" ' + vdf_string(folder) + ' "StateFlags" "4"}')
        directory = apps / "common" / folder
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def libraries(self, paths, modern=True):
        folder = self.steam / "steamapps"
        folder.mkdir(parents=True, exist_ok=True)
        rows = []
        for index, path in enumerate(paths):
            value = '{"path" ' + vdf_string(path) + ' "apps" {"2280" "0"}}' if modern else vdf_string(path)
            rows.append(vdf_string(index) + " " + value)
        (folder / "libraryfolders.vdf").write_text('"libraryfolders" {' + " ".join(rows) + '}')

    def result(self):
        return discover(home=self.home)

    def indexed(self, result=None):
        return {item["id"]: item for item in (self.result() if result is None else result)["packages"]}

    def test_modern_bundle_original_files_preferred_and_known_addons_only(self):
        install = self.install(2280, "Ultimate Doom", title="DOOM + DOOM II")
        paths = {
            "steam-doom": wad(install / "base/DOOM.WAD", ("E1M1", "E2M1")),
            "steam-doom2": wad(install / "base/doom2/DOOM2.WAD"),
            "steam-tnt": wad(install / "base/tnt/TNT.WAD"),
            "steam-plutonia": wad(install / "base/plutonia/PLUTONIA.WAD"),
        }
        for kind, filename in (("doom", "doom"), ("doom2", "doom2"), ("tnt", "tnt"), ("plutonia", "plutonia")):
            wad(install / f"rerelease/{filename}.wad", ("E1M1",) if kind == "doom" else ("MAP01",))
        wad(install / "rerelease/nerve.wad", magic=b"PWAD")
        wad(install / "rerelease/sigil.wad", ("E5M1",), b"PWAD")
        wad(install / "rerelease/sigil2.wad", ("E6M1",), b"PWAD")
        wad(install / "rerelease/masterlevels.wad", magic=b"PWAD")
        wad(install / "base/master/wads/ATTACK.WAD", magic=b"PWAD")
        wad(install / "rerelease/id1.wad", ("MAP01",), b"PWAD")
        wad(install / "rerelease/extras.wad", magic=b"PWAD")
        wad(self.steam / "steamapps/workshop/content/2280/123/arbitrary.wad")
        wad(install / "unrelated/mods/sneaky.wad")
        result = self.result()
        packages = self.indexed(result)
        self.assertTrue(result["steam_found"])
        self.assertEqual(len(packages), 9)
        self.assertEqual([item["id"] for item in result["packages"]],
                         ["steam-masterlevels" if kind == "master" else "steam-" + kind
                          for kind in steam_games.ORDER])
        for package_id, path in paths.items():
            self.assertEqual(packages[package_id]["iwad"], str(path))
            self.assertTrue(packages[package_id]["compatible"])
            self.assertEqual(packages[package_id]["edition"], "original")
        self.assertEqual(packages["steam-sigil"]["start_map"], "E5M1")
        self.assertEqual(packages["steam-sigil2"]["start_map"], "E6M1")
        for package_id in ("steam-nerve", "steam-masterlevels", "steam-sigil", "steam-sigil2"):
            self.assertTrue(packages[package_id]["compatible"])
        self.assertEqual(packages["steam-legacy-rust"]["reason"], "Requires unsupported ID24")
        self.assertFalse(packages["steam-legacy-rust"]["compatible"])
        self.assertEqual(packages["steam-doom"]["source"]["title"], "DOOM + DOOM II")
        self.assertEqual(packages["steam-doom"]["source"]["appid"], 2280)
        self.assertFalse(any("workshop" in path or "unrelated/mods" in path for path in result["scanned_paths"]))

    def test_legacy_app_manifests_custom_library_and_original_across_libraries(self):
        release = self.install(2280, "Ultimate Doom")
        wad(release / "rerelease/doom.wad", ("E1M1",))
        extra = self.home.parent / "external library"
        doom = self.install(2280, "Ultimate Doom", extra)
        original = wad(doom / "base/DOOM.WAD", ("E1M1",))
        doom2 = self.install(2300, "Doom 2", extra)
        doom2_wad = wad(doom2 / "base/DOOM2.WAD")
        final = self.install(2290, "Final Doom", extra)
        wad(final / "base/TNT.WAD")
        wad(final / "base/PLUTONIA.WAD")
        master = self.install(9160, "Master Levels of Doom", extra)
        attack = wad(master / "master/wads/ATTACK.WAD", magic=b"PWAD")
        tower = wad(master / "master/wads/BLACKTWR.WAD", ("MAP25",), b"PWAD")
        self.libraries([self.steam, extra], modern=False)
        packages = self.indexed()
        self.assertEqual(packages["steam-doom"]["iwad"], str(original))
        self.assertEqual(packages["steam-doom2"]["appid"], 2300)
        self.assertEqual(packages["steam-master-attack"]["iwad"], str(doom2_wad))
        self.assertEqual(packages["steam-master-attack"]["pwads"], [str(attack)])
        self.assertEqual(packages["steam-master-blacktwr"]["start_map"], "MAP25")
        self.assertEqual(packages["steam-master-blacktwr"]["pwads"], [str(tower)])

    def test_symlink_root_file_aliases_and_flatpak_are_deduplicated(self):
        install = self.install(2280, "Ultimate Doom")
        original = wad(install / "base/DOOM.WAD", ("E1M1",))
        (install / "DOOM.WAD").symlink_to(original)
        steam_alias = self.home / ".steam"
        steam_alias.mkdir()
        (steam_alias / "steam").symlink_to(self.steam, target_is_directory=True)
        (steam_alias / "root").symlink_to(self.steam, target_is_directory=True)
        self.libraries([self.steam, steam_alias / "steam", steam_alias / "root"])
        result = self.result()
        self.assertEqual(len(result["packages"]), 1)
        self.assertEqual(result["packages"][0]["iwad"], str(original))
        self.assertEqual(result["scanned_paths"].count(str(self.steam / "steamapps/appmanifest_2280.acf")), 1)
        flatpak = self.home / ".var/app/com.valvesoftware.Steam/.local/share/Steam"
        second = self.install(2300, "Doom II", flatpak)
        wad(second / "base/doom2.wad")
        self.assertEqual(set(self.indexed()), {"steam-doom", "steam-doom2"})

    def test_missing_base_incomplete_and_malformed_installs_warn_cleanly(self):
        addon = self.install(9160, "Master")
        wad(addon / "master/wads/attack.wad", magic=b"PWAD")
        partial = self.install(2280, "Ultimate Doom")
        (partial / "base").mkdir()
        (partial / "base/doom.wad").write_bytes(b"IWAD\x01")
        manifests = self.steam / "steamapps"
        (manifests / "appmanifest_2290.acf").write_text('"AppState" {"appid" "2290"')
        (manifests / "appmanifest_2300.acf").write_text('"AppState" {"appid" "2300" "installdir" "Not installed"}')
        missing = self.home.parent / "disconnected-drive"
        self.libraries([self.steam, missing])
        result = self.result()
        row = self.indexed(result)["steam-master-attack"]
        self.assertFalse(row["compatible"])
        self.assertIsNone(row["iwad"])
        self.assertEqual(row["reason"], "Requires DOOM II")
        self.assertTrue(any("truncated WAD" in text for text in result["warnings"]))
        self.assertTrue(any("Unterminated" in text for text in result["warnings"]))
        self.assertTrue(any("Not installed" in text for text in result["warnings"]))
        self.assertTrue(any("disconnected-drive" in text for text in result["warnings"]))

    def test_wrong_type_or_missing_start_falls_back_and_truncated_lumps_rejected(self):
        install = self.install(2280, "Doom")
        wad(install / "base/doom.wad", ("MAP01",))
        fallback = wad(install / "rerelease/doom.wad", ("E1M1",))
        invalid = install / "base/doom2/doom2.wad"
        invalid.parent.mkdir(parents=True)
        invalid.write_bytes(struct.pack("<4sii", b"IWAD", 1, 12)
                            + struct.pack("<ii8s", 100, 50, b"MAP01"))
        row = self.indexed()["steam-doom"]
        self.assertEqual(row["iwad"], str(fallback))
        self.assertEqual(row["edition"], "rerelease")
        self.assertNotIn("steam-doom2", self.indexed())

    def test_addons_require_valid_base_marker_and_registered_doom(self):
        install = self.install(2280, "Doom")
        original = wad(install / "base/doom.wad", ("E1M1",))
        wad(install / "rerelease/sigil.wad", ("E5M1",), b"PWAD")
        wad(install / "rerelease/sigil2.wad", ("E6M1",), b"PWAD")
        doom2 = wad(install / "base/doom2/doom2.wad", ("MAP02",))
        wad(install / "rerelease/nerve.wad", magic=b"PWAD")
        with patch.dict(steam_games.COMPATIBILITY, {"sigil": (True, ""), "sigil2": (True, ""), "nerve": (True, "")}):
            rows = self.indexed()
            for package_id in ("steam-sigil", "steam-sigil2"):
                self.assertFalse(rows[package_id]["compatible"])
                self.assertEqual(rows[package_id]["reason"], "Requires full DOOM")
            self.assertFalse(rows["steam-nerve"]["compatible"])
            self.assertEqual(rows["steam-nerve"]["reason"], "Missing base start map")
            registered = wad(install / "rerelease/doom.wad", ("E1M1", "E2M1"))
            rows = self.indexed()
            self.assertTrue(rows["steam-sigil"]["compatible"])
            self.assertEqual(rows["steam-sigil"]["iwad"], str(registered))
            self.assertEqual(rows["steam-doom"]["iwad"], str(original))
            wad(doom2)
            self.assertTrue(self.indexed()["steam-nerve"]["compatible"])

    def test_invalid_aggregate_preserves_compatible_individual_master_fallback(self):
        install = self.install(2280, "Doom")
        wad(install / "base/doom2/doom2.wad")
        wad(install / "rerelease/masterlevels.wad", ("MAP02",), b"PWAD")
        attack = wad(install / "base/master/wads/attack.wad", magic=b"PWAD")
        rows = self.indexed()
        self.assertFalse(rows["steam-masterlevels"]["compatible"])
        self.assertEqual(rows["steam-masterlevels"]["reason"], "Missing start map")
        self.assertTrue(rows["steam-master-attack"]["compatible"])
        self.assertEqual(rows["steam-master-attack"]["pwads"], [str(attack)])

    def test_bounded_local_artwork_direct_and_hash_layouts(self):
        install = self.install(2280, "Doom")
        wad(install / "base/doom.wad", ("E1M1",))
        cache = self.steam / "appcache/librarycache/2280"
        hashed = cache / ("a" * 40)
        hashed.mkdir(parents=True)
        (cache / "logo.png").write_bytes(b"installed local logo")
        (hashed / "library_600x900.png").write_bytes(b"installed local cover")
        (hashed / "library_hero.jpg").write_bytes(b"installed local hero")
        (cache / "unrecognized").mkdir()
        (cache / "unrecognized/logo.png").write_bytes(b"not recognized")
        art = self.indexed()["steam-doom"]["source"]["artwork"]
        self.assertEqual(art, {"logo": str(cache / "logo.png"),
                               "cover": str(hashed / "library_600x900.png"),
                               "hero": str(hashed / "library_hero.jpg")})

    def test_invalid_manifest_paths_and_missing_steam_do_not_scan_host(self):
        result = self.result()
        self.assertEqual(result["packages"], [])
        self.assertFalse(result["steam_found"])
        self.assertEqual(result["summary"], "Steam was not found")
        self.install(2280, "Doom")
        path = self.steam / "steamapps/appmanifest_2280.acf"
        path.write_text('"AppState" {"appid" "2280" "installdir" "../../outside"}')
        self.libraries(["relative/library", "D:\\Windows Library"])
        result = self.result()
        self.assertEqual(result["packages"], [])
        self.assertTrue(any("Invalid Steam install" in text for text in result["warnings"]))
        self.assertTrue(any("Invalid Steam library path" in text for text in result["warnings"]))
        self.assertTrue(all(path.startswith(str(self.home)) for path in result["scanned_paths"]))
        json.dumps(result)  # The public result contains no Path or bytes values.

    def test_library_probe_budget_also_bounds_missing_and_duplicate_paths(self):
        self.install(2280, "Doom")
        missing = [self.home.parent / f"missing-{index}" for index in range(30)]
        self.libraries([self.steam] * 20 + missing)
        with patch.object(steam_games, "MAX_LIBRARY_PATHS", 12):
            result = self.result()
        self.assertTrue(any("scan limited" in text for text in result["warnings"]))
        missing_warnings = [text for text in result["warnings"] if "Steam library unavailable" in text]
        self.assertLessEqual(len(missing_warnings), 4)  # Eight standard root probes plus four new paths.


if __name__ == "__main__":
    unittest.main()
