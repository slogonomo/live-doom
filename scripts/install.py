#!/usr/bin/env python3
# SPDX-License-Identifier: 0BSD
"""Install this checkout's user-owned Omarchy integration."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True

from install_support import (
    JOURNAL_REL, SHELL_REL, LEGACY_IDLE_ID, activate_install, activate_menu, artifacts, atomic_write,
    journal_files, json_bytes, load_json, prepare_config, prepare_menu_config, require_unlocked, run,
    require_upgrade_checkpoint,
    prune_generated_bundle_caches, prune_obsolete_theme_backgrounds, sync_active_theme_backgrounds,
    SERVICE_NAME,
    LEGACY_MENU_ID,
    scan_steam_catalog,
    marketplace_install,
    confirm_changes,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, help="Stage under this HOME directory without contacting running services")
    parser.add_argument("--omarchy-path", type=Path, default=Path(os.environ.get("OMARCHY_PATH", "/usr/share/omarchy")))
    parser.add_argument("--dry-run", action="store_true", help="Print the plan without writing files or starting processes")
    parser.add_argument("--no-activate", action="store_true", help="Write files without enabling or starting running services")
    design_mode = parser.add_mutually_exclusive_group()
    design_mode.add_argument("--without-design-bundles", action="store_true", help=argparse.SUPPRESS)
    design_mode.add_argument("--menu-only", action="store_true", help="Update plugin registration and launcher for an existing install without restarting the game or changing idle integration")
    parser.add_argument("--replace-modified", action="store_true", help="Replace edits in files previously installed by this project")
    parser.add_argument("--dev", action="store_true", help="Stage a complete content-versioned plugin snapshot from this developer checkout")
    parser.add_argument("--legacy-dev", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--yes", action="store_true", help="Approve the printed changes without an interactive prompt")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    if not args.legacy_dev:
        return marketplace_install(args, root)
    home = (args.prefix or Path.home()).expanduser().resolve()
    activate = args.prefix is None and not args.no_activate and not args.dry_run
    try:
        old = load_json(home / JOURNAL_REL)
        if old and (old.get("schema") != 1 or old.get("plugin_id") != LEGACY_IDLE_ID):
            raise RuntimeError("Install journal format is unsupported")
        if args.menu_only and old is None:
            raise RuntimeError("Menu-only installation requires an existing Doom native installation journal")
        if activate:
            required = ("scripts/doomctl", "src/controller.py") if args.menu_only else (
                "scripts/doomctl", "src/controller.py", "build/eternity", "build/doom-viewer")
            for rel in required:
                if not (root / rel).is_file():
                    raise RuntimeError(f"Build is incomplete: {root / rel} is missing")
        if activate or (args.menu_only and args.prefix is None and not args.dry_run):
            # keepLoaded menu QML can mount its parked surface through file
            # watchers even with --no-activate. Verify before real-HOME writes.
            require_unlocked()
        for rel in (JOURNAL_REL, SHELL_REL):
            if (home / rel).is_symlink():
                raise RuntimeError(f"Refusing to replace a symlink: {home / rel}")
        files = artifacts(root, args.omarchy_path, include_design=not args.without_design_bundles,
                          menu_only=args.menu_only)
        install_menu = f".config/omarchy/plugins/{LEGACY_MENU_ID}/manifest.json" in files
        if args.menu_only:
            config, delta = prepare_menu_config(home, old)
        else:
            config, delta = prepare_config(home, args.omarchy_path, old, install_menu=install_menu)
        records = journal_files(home, files, old, args.replace_modified)
        if activate and old is not None and not args.menu_only:
            # Reject takeover/unsaveable phases before file watchers can load
            # changed native integration. Activation rechecks after staging.
            require_upgrade_checkpoint(root)
        runtime = old.get("runtime") if old else None
        if activate and not args.menu_only and runtime is None:
            runtime = {
                "service_enabled": run(["systemctl", "--user", "is-enabled", "--quiet", SERVICE_NAME], check=False).returncode == 0,
                "service_active": run(["systemctl", "--user", "is-active", "--quiet", SERVICE_NAME], check=False).returncode == 0,
            }
        journal = {"schema": 1, "root": str(root), "plugin_id": LEGACY_IDLE_ID, "config": delta, "files": records, "runtime": runtime}
        if old and "background_mirror" in old:
            journal["background_mirror"] = old["background_mirror"]
        print(f"{'Would install' if args.dry_run else 'Installing'} Doom integration under {home}")
        for rel in files:
            print(f"  {rel}")
        print("  shell.json: " + ("enable only the Doom menu; preserve idle selection and timings" if args.menu_only
              else "replace the idle plugin selection; preserve idle timings and other settings"))
        if args.without_design_bundles:
            print("  design bundles skipped; existing theme/menu files and selection unchanged")
        else:
            print("  bundled menu enabled when present; current theme/background unchanged")
            print("  select Phobos and its live-doom choice in Omarchy when ready")
        if args.dry_run:
            return 0
        confirm_changes(args)
        # Persist recovery data before changing any existing artifact.
        atomic_write(home / JOURNAL_REL, json_bytes(journal), 0o600)
        for rel, (data, mode) in files.items():
            atomic_write(home / rel, data, mode)
        atomic_write(home / SHELL_REL, json_bytes(config))
        if not args.without_design_bundles:
            cleanup_notes = prune_generated_bundle_caches(home, journal)
            cleanup_notes += prune_obsolete_theme_backgrounds(home, journal, files)
            cleanup_notes += sync_active_theme_backgrounds(home, journal, old, files)
            if cleanup_notes:
                # The recovery journal was saved before removals. Drop only
                # records for successfully pruned, unbacked owned files.
                atomic_write(home / JOURNAL_REL, json_bytes(journal), 0o600)
                for note in cleanup_notes:
                    print(note)
        # Generated catalog state is independent of owned install artifacts.
        # A missing/inaccessible Steam library must not prevent installation.
        print(scan_steam_catalog(root, home, prefix=args.prefix is not None))
        if activate:
            if args.menu_only:
                activate_menu()
                print("Doom menu installed; the live game and kept idle service were not restarted.")
            else:
                activate_install(upgrade=old is not None, enable_menu=install_menu, root=root)
                print("Live Doom is installed. Open Live Doom in the application menu for settings.")
        else:
            print("Files staged; no running services were changed.")
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Install failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
