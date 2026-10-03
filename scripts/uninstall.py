#!/usr/bin/env python3
# SPDX-License-Identifier: 0BSD
"""Remove this project's integration while retaining later user changes."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.dont_write_bytecode = True

from install_support import (
    JOURNAL_REL, SHELL_REL, LEGACY_IDLE_ID, SERVICE_NAME, atomic_write, digest,
    json_bytes, load_json, require_unlocked, restore_config, restore_files, run,
    marketplace_uninstall,
    confirm_changes,
    artifact_matches,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, help="Remove staged files under this HOME without contacting running services")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan without modifying files or processes")
    parser.add_argument("--no-activate", action="store_true", help="Restore files without changing running services")
    parser.add_argument("--dev", action="store_true", help="Compatibility option for a new developer snapshot; removal uses the same Live Doom ownership journal")
    parser.add_argument("--legacy-dev", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--yes", action="store_true", help="Approve the printed changes without an interactive prompt")
    args = parser.parse_args(argv)
    if not args.legacy_dev:
        return marketplace_uninstall(args)
    home = (args.prefix or Path.home()).expanduser().resolve()
    activate = args.prefix is None and not args.no_activate and not args.dry_run
    try:
        journal = load_json(home / JOURNAL_REL)
        if journal is None:
            print("No Doom installation journal was found; nothing changed.")
            return 0
        if journal.get("schema") != 1 or journal.get("plugin_id") != LEGACY_IDLE_ID:
            raise RuntimeError("Install journal format is unsupported")
        if activate:
            require_unlocked()
        if (home / SHELL_REL).is_symlink():
            raise RuntimeError(f"Refusing to replace a symlink: {home / SHELL_REL}")
        config, notes = restore_config(home, journal)
        print(f"{'Would remove' if args.dry_run else 'Removing'} Doom integration from {home}")
        for rel in journal["files"]:
            print(f"  {rel}")
        if not args.dry_run:
            confirm_changes(args)
        service_owned = False
        if activate:
            service_rel = f".config/systemd/user/{SERVICE_NAME}"
            record = journal["files"].get(service_rel)
            service = home / service_rel
            # Never stop a service file replaced with another user's service.
            if record and service.is_file() and not service.is_symlink() and artifact_matches(service, record["installed_sha256"]):
                service_owned = True
                run(["systemctl", "--user", "disable", "--now", SERVICE_NAME])
        if not args.dry_run and config is not None:
            atomic_write(home / SHELL_REL, json_bytes(config))
        notes.extend(restore_files(home, journal, dry_run=args.dry_run))
        for note in notes:
            print(note)
        if args.dry_run:
            return 0
        if activate:
            run(["omarchy-shell", "shell", "rescanPlugins"])
            run(["systemctl", "--user", "daemon-reload"])
            runtime = journal.get("runtime") or {}
            service_record = journal["files"].get(f".config/systemd/user/{SERVICE_NAME}")
            if service_owned and service_record and service_record["original"] is not None:
                if runtime.get("service_enabled"):
                    run(["systemctl", "--user", "enable", SERVICE_NAME])
                if runtime.get("service_active"):
                    run(["systemctl", "--user", "start", SERVICE_NAME])
            if journal["config"].get("previous_clones"):
                print("The previous idle plugin selection was restored.")
        # Retain the journal if edits survive so their originals remain
        # recoverable. Once clean, removing it makes uninstall idempotent.
        if any(note.startswith("Kept modified file:") for note in notes):
            print(f"Backup metadata retained at {home / JOURNAL_REL}")
        else:
            (home / JOURNAL_REL).unlink()
        print("Integration removed. Game progress and WAD files were retained.")
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Uninstall failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
