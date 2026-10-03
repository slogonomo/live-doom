# SPDX-License-Identifier: 0BSD
"""Observe Omarchy's selected background without changing the shell or theme.

The stock background picker and theme picker both replace
``~/.local/state/omarchy/current/background``. A normal image with ``live-doom``
in its resolved basename opts into the live wallpaper; it remains a valid
static fallback for Omarchy. Screensaver settings are deliberately outside
this module.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile

from project_paths import atomic_json, read_json


class BackgroundSelection:
    """Return wallpaper enable changes only when a selection changes.

    A first ordinary background establishes a baseline without disabling an
    existing installation. A first live-doom background opts in immediately.
    Persisted observations also detect selections made while the daemon was
    stopped. Repeated polling never overrides an explicit wallpaper command.
    """

    def __init__(self, home: Path, state_dir: Path):
        self.link = Path(home) / ".local/state/omarchy/current/background"
        self.state_path = Path(state_dir) / "background-selection.json"
        self.previous = None
        self.legacy_observation = False
        self.error = None
        try:
            saved = read_json(self.state_path, max_bytes=16384)
            selection = saved.get("selection")
            if (saved.get("schema") == 2 and isinstance(selection, dict)
                    and isinstance(selection.get("path"), str)
                    and isinstance(selection.get("link"), dict)
                    and set(selection["link"]) == {"mtime_ns", "size", "mode"}
                    and all(isinstance(value, int) for value in selection["link"].values())):
                self.previous = selection
            elif (saved.get("schema") == 1 and isinstance(selection, dict)
                    and isinstance(selection.get("path"), str)
                    and isinstance(selection.get("link"), list)
                    and len(selection["link"]) == 4
                    and all(isinstance(value, int) for value in selection["link"])):
                # Old observations stored dev/inode/ctime. Device numbers can
                # change across a reboot, so only path and mtime survive the
                # migration. The next stable sample supplies size and mode.
                self.previous = {"path": selection["path"],
                                 "link": {"mtime_ns": selection["link"][2]}}
                self.legacy_observation = True
        except (OSError, ValueError, AttributeError):
            # A missing/old/corrupt observation is a fresh migration baseline,
            # never a reason to stop an existing wallpaper or screensaver.
            pass

    def _selection(self):
        try:
            before = self.link.lstat()
            if not stat.S_ISLNK(before.st_mode):
                return None
            resolved = self.link.resolve(strict=True)
            if not stat.S_ISREG(resolved.stat().st_mode):
                return None
            after = self.link.lstat()
            # Theme switching can replace this symlink between the reads.
            # Observe the next stable sample rather than publishing a mix.
            identity = lambda info: [info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns]
            if identity(before) != identity(after):
                return None
            return {"path": str(resolved),
                    "link": {"mtime_ns": after.st_mtime_ns, "size": after.st_size,
                             "mode": stat.S_IMODE(after.st_mode)}}
        except (OSError, RuntimeError):
            # A staged theme may be briefly absent, and a broken/cyclic
            # symlink must not erase the last successfully observed choice.
            return None

    def _remember(self, selection):
        atomic_json(self.state_path, {"schema": 2, "selection": selection})

    def poll(self) -> bool | None:
        """True/False means a live/static choice; None means no policy change.

        Persistence errors are exposed as ``error`` but do not retry the same
        enable change every poll. That keeps read-only state directories from
        continuously undoing the user's explicit wallpaper setting.
        """
        selection = self._selection()
        if selection is None:
            return None
        unchanged = selection == self.previous
        if self.legacy_observation:
            unchanged = (selection["path"] == self.previous["path"]
                         and selection["link"]["mtime_ns"] == self.previous["link"]["mtime_ns"])
        if unchanged and not self.legacy_observation:
            return None
        initial = self.previous is None
        live = "live-doom" in Path(selection["path"]).name.lower()
        self.previous = selection
        self.legacy_observation = False
        try:
            self._remember(selection)
            self.error = None
        except OSError as exc:
            self.error = str(exc)
        return None if unchanged else (True if live else (None if initial else False))
