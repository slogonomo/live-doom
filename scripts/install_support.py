# SPDX-License-Identifier: 0BSD
"""User-owned Omarchy integration, shared by install and uninstall.

The journal records only files we own and their plugin registry changes.
It deliberately never restores a complete shell.json over later user edits.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from process_utils import bounded_run, session_environment
from project_paths import read_bytes as secure_read_bytes

MAX_ARTIFACT_BYTES = 16 * 1024 * 1024


def artifact_bytes(path: Path, *, require_owner=True):
    return secure_read_bytes(path, MAX_ARTIFACT_BYTES, require_owner=require_owner)


def artifact_matches(path: Path, expected):
    try:
        return digest(artifact_bytes(path)) == expected
    except (OSError, ValueError):
        return False


LEGACY_IDLE_ID = "live-doom-dev.idle"
LEGACY_MENU_ID = "live-doom-dev.menu"
THEME_SLUG = "phobos"
SOURCE_ID = "omarchy.idle"
SAVER_CLASS = "org.omarchy.doom.screensaver"
JOURNAL_REL = ".local/state/doom-desktop/install.json"
SHELL_REL = ".config/omarchy/shell.json"
SERVICE_NAME = "doom-desktop.service"


def legacy_ids(journal: dict):
    """Recover local developer IDs from an owner-checked legacy journal."""
    if not isinstance(journal, dict):
        raise RuntimeError("Legacy install journal format is unsupported")
    idle_id = journal.get("plugin_id")
    if (journal.get("schema") != 1 or not isinstance(idle_id, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", idle_id)
            or idle_id in {SOURCE_ID, published_id()}
            or not isinstance(journal.get("files"), dict)):
        raise RuntimeError("Legacy install journal format is unsupported")
    menu_ids = set()
    for rel in journal["files"]:
        parts = Path(rel).parts
        if (len(parts) == 5 and parts[:3] == (".config", "omarchy", "plugins")
                and parts[4] == "manifest.json" and parts[3] != idle_id):
            candidate = parts[3]
            if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", candidate)
                    or candidate in {SOURCE_ID, published_id()}):
                raise RuntimeError("Unsafe legacy menu identity")
            menu_ids.add(candidate)
    if len(menu_ids) > 1:
        raise RuntimeError("Legacy menu identity is ambiguous; preserve it for manual review")
    return idle_id, next(iter(menu_ids), None)


def project_paths(home: Path, *, prefix=False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from project_paths import ProjectPaths
    return ProjectPaths.from_env(home=home, env={} if prefix else os.environ)


def published_id():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from project_paths import PLUGIN_ID
    return PLUGIN_ID


def confirm_changes(args):
    """Consent applies to real writes, including --no-activate staging."""
    if args.prefix is not None or args.dry_run or args.yes:
        return
    if not sys.stdin.isatty():
        raise RuntimeError("No changes were made. Review --dry-run, then use --yes or run interactively to confirm.")
    if input("Apply exactly these changes? [y/N] ").strip().lower() not in {"y", "yes"}:
        raise RuntimeError("Cancelled; no changes were made.")


def validate_marketplace(root: Path):
    if (root / "manifest.json").is_symlink():
        raise RuntimeError("Marketplace manifest must be a regular file")
    manifest = load_json(root / "manifest.json", {})
    if (not isinstance(manifest, dict) or manifest.get("id") != published_id()
            or not isinstance(manifest.get("kinds"), list)
            or not isinstance(manifest.get("entryPoints"), dict)
            or "overlay" not in manifest.get("kinds", [])
            or manifest.get("entryPoints", {}).get("overlay") != "menu/Menu.qml"):
        raise RuntimeError("Marketplace setup requires the root Live Doom manifest and menu/Menu.qml entry point.")
    manifests = [p for p in root.iterdir() if p.name.lower() == "manifest.json"]
    manifests += [p for folder in root.iterdir() if folder.is_dir() and not folder.is_symlink()
                  for p in folder.iterdir() if p.name.lower() == "manifest.json"]
    if len(manifests) != 1:
        raise RuntimeError("Marketplace checkout must contain exactly one manifest.json, at the root.")
    for entry in manifest["entryPoints"].values():
        if not isinstance(entry, str):
            raise RuntimeError("Invalid marketplace entry point")
        validate_relative(entry)
        path = root / entry
        if path.is_symlink() or symlink_ancestor(root, entry) is not None or not path.is_file():
            raise RuntimeError(f"Marketplace entry point must be a regular file: {path}")


def home_relative(home: Path, path: Path):
    try:
        rel = str(path.relative_to(home))
    except ValueError as exc:
        raise RuntimeError("Installer-owned XDG artifacts must be beneath HOME; use --prefix for isolated staging.") from exc
    validate_relative(rel)
    return rel


def render_service(root: Path, *, installed_root=None):
    service = artifact_bytes(root / "scripts/doom-desktop.service").decode()
    target = installed_root or root
    service = service.replace("@PYTHON@ @ROOT@/src/controller.py", "/usr/bin/python3 -E -s -B " + systemd_quote(str(target / "src/controller.py")))
    return service.replace("@ROOT@", str(target).replace("\\", "\\\\").replace("%", "%%")).encode()


def marketplace_artifacts(root: Path, home: Path, paths, *, menu_only=False):
    files = {}
    if not menu_only:
        service = paths.config_root.parent / "systemd/user" / SERVICE_NAME
        files[home_relative(home, service)] = (render_service(root), 0o644)
        for rel, value in agent_example_files(root).items():
            tail = Path(rel).relative_to(".config/doom-desktop")
            files[home_relative(home, paths.config_root / tail)] = value
    launcher = launcher_files(root)[".local/share/applications/doom-wallpaper.desktop"][0]
    files[home_relative(home, paths.data_root.parent / "applications/live-doom.desktop")] = (launcher, 0o644)
    return files


def developer_snapshot(root: Path, home: Path, paths):
    """Version the whole relative runtime tree, never a duplicate menu plugin."""
    from project_paths import read_bytes
    contents = {}

    def add_file(path):
        rel = path.relative_to(root)
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"Developer snapshot requires a regular file: {path}")
        contents[str(rel)] = (read_bytes(path, 16 * 1024 * 1024),
                              0o755 if path.stat().st_mode & 0o111 else 0o644)

    # The live still and terms are runtime inputs, not optional theme art.
    # Retain their licences and the GPL patch needed to rebuild the backend.
    for rel in ("assets/live-doom.webp", "README.md", "LICENSE", "THIRD-PARTY-NOTICES.md",
                "LICENSES/0BSD.txt", "LICENSES/CC0-1.0.txt", "LICENSES/GPL-3.0-or-later.txt",
                "docs/licenses/Doom-shareware.txt", "docs/licenses/Doom-shareware-1.8.txt",
                "patches/autodoom-desktop.patch", "patches/sdl-mixer-device.patch",
                "scripts/build-pins.json"):
        add_file(root / rel)
    for name in ("menu", "service", "src", "scripts", "sdk", "examples", "LICENSES", "docs/licenses", "patches"):
        directory = root / name
        if not directory.exists():
            continue
        if directory.is_symlink():
            raise RuntimeError(f"Refusing developer snapshot symlink: {directory}")
        for path in sorted(directory.rglob("*")):
            rel = path.relative_to(root)
            if is_python_cache(rel):
                continue
            if path.is_symlink():
                raise RuntimeError(f"Refusing developer snapshot symlink: {path}")
            if path.is_file():
                if path.name.lower() == "manifest.json":
                    raise RuntimeError("Only the root marketplace manifest may be staged")
                add_file(path)
    manifest = load_json(root / "manifest.json")
    version = digest(json_bytes({"manifest": manifest, "files": [
        {"path": rel, "mode": mode, "sha256": digest(data)} for rel, (data, mode) in sorted(contents.items())]}))
    plugin = paths.config_root.parent / "omarchy/plugins" / published_id()
    implementation = plugin / ("impl-" + version)
    files = {home_relative(home, implementation / rel): value for rel, value in contents.items()}
    for kind, entry in manifest["entryPoints"].items():
        manifest["entryPoints"][kind] = "impl-" + version + "/" + entry
    # Publish only after all sibling scripts, modules and implicit QML types.
    files[home_relative(home, plugin / "manifest.json")] = (json_bytes(manifest), 0o644)
    return implementation, files


def marketplace_config(home: Path, omarchy: Path, previous=None, legacy=None):
    config = load_json(home / SHELL_REL)
    if config is None:
        config = load_json(omarchy / "config/omarchy/shell.json", {"version": 1, "plugins": []})
    if not isinstance(config, dict) or not isinstance(config.get("plugins", []), list) or not isinstance(config.get("disabledPlugins", []), list):
        raise RuntimeError("shell.json must contain plugin arrays")
    plugin_id = published_id()
    legacy_menu_id = legacy_ids(legacy)[1] if legacy else None
    plugins = config.get("plugins", [])
    disabled = config.get("disabledPlugins", [])
    delta = dict(previous["config"]) if previous else {
        "added_entry": not any(isinstance(e, dict) and e.get("id") == plugin_id for e in plugins),
        "cleared_disabled": plugin_id in disabled,
        "disabled_index": disabled.index(plugin_id) if plugin_id in disabled else 0,
        "disabled_present": "disabledPlugins" in config,
        "legacy_menu_entries": [], "legacy_menu_id": legacy_menu_id,
        "legacy_menu_disabled": legacy_menu_id in disabled,
    }
    if legacy_menu_id and legacy.get("config", {}).get("menu", {}).get("added_entry"):
        removed = [e for e in plugins if isinstance(e, dict) and e.get("id") == legacy_menu_id]
        if removed:
            delta["legacy_menu_entries"] = removed
            plugins = [e for e in plugins if e not in removed]
            if legacy_menu_id not in disabled:
                disabled = disabled + [legacy_menu_id]
    if not any(isinstance(e, dict) and e.get("id") == plugin_id for e in plugins):
        plugins = plugins + [{"id": plugin_id}]
    config["plugins"] = plugins
    config["disabledPlugins"] = [p for p in disabled if p != plugin_id]
    return config, delta


def initialize_marketplace(root: Path, home: Path, paths):
    env = session_environment() | {"XDG_CONFIG_HOME": str(paths.config_root.parent),
                        "XDG_DATA_HOME": str(paths.data_root.parent), "XDG_STATE_HOME": str(paths.state_root.parent),
                        "XDG_CACHE_HOME": str(paths.cache_root.parent)}
    result = bounded_run([sys.executable, str(root / "src/onboarding.py"), "initialize",
                             "--config", str(paths.config_file), "--data", str(paths.data_root), "--home", str(home)],
                            env=env, text=True, timeout=15, max_output_bytes=4096)
    if result.returncode or len(result.stdout) > 4096:
        raise RuntimeError("First-run initialization failed; staged ownership records were retained.")
    value = json.loads(result.stdout)
    if not isinstance(value, dict) or type(value.get("fresh")) is not bool or type(value.get("show")) is not bool:
        raise RuntimeError("Invalid first-run initialization result")
    return value


def activate_marketplace(root: Path, *, upgrade=False, menu_only=False, show=False):
    require_unlocked()
    was_active = require_upgrade_checkpoint(root) if upgrade and not menu_only else False
    if not menu_only:
        run(["systemctl", "--user", "daemon-reload"])
        run(["systemctl", "--user", "enable", "--now", SERVICE_NAME])
        if was_active:
            require_upgrade_checkpoint(root)
            run(["systemctl", "--user", "restart", SERVICE_NAME])
    run(["omarchy-shell", "shell", "rescanPlugins"])
    wait_for_plugin(published_id(), cloned_from=None)
    run(["omarchy", "plugin", "enable", published_id()])
    if show:
        require_unlocked()
        run([str(root / "scripts/doomctl"), "open-menu", "--origin=onboarding"], timeout=10)
    run(["update-desktop-database", str(Path.home() / ".local/share/applications")], check=False)


def cleanup_owned_runtime(paths, journal, home: Path):
    """Called only after stopping an unchanged journal-owned service unit."""
    notes = []
    try:
        runtime = paths.runtime_root(create=False)
    except (OSError, ValueError):
        return notes
    lock = runtime / "daemon.lock"
    fd = None
    try:
        if lock.exists():
            fd = os.open(lock, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                return [f"Kept unsafe runtime lock: {lock}"]
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return [f"Kept runtime used by another daemon: {runtime}"]
        for name in ("daemon.lock", "control.sock", "engine.sock", "frame.bin", "campaign-info.wad"):
            path = runtime / name
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if info.st_uid == os.getuid() and (stat.S_ISREG(info.st_mode) or stat.S_ISSOCK(info.st_mode)):
                path.unlink()
                notes.append(f"Removed stopped service runtime artifact: {path}")
        try:
            runtime.rmdir()
        except OSError:
            pass
    except OSError:
        notes.append(f"Kept unsafe or concurrently changed runtime artifact: {runtime}")
    finally:
        if fd is not None:
            os.close(fd)
    return notes


def cleanup_owned_onboarding(paths, journal, home: Path):
    """Recognize product-owned marker changes such as dismiss, not user data."""
    path = paths.data_root / "onboarding.json"
    rel = home_relative(home, path)
    record = journal["files"].get(rel)
    if not record or record.get("original") is not None:
        return journal
    try:
        from project_paths import read_bytes
        raw = read_bytes(path, 16384)
        value = json.loads(raw)
        base = {"schema", "show", "fresh", "selected_package"}
        valid = (isinstance(value, dict) and base <= value.keys()
                 and type(value["schema"]) is int and type(value["show"]) is bool
                 and type(value["fresh"]) is bool
                 and (value["selected_package"] is None or isinstance(value["selected_package"], str)))
        if valid and value["schema"] == 1:
            valid = set(value) == base
        elif valid and value["schema"] == 2:
            choices = value.get("choices")
            valid = (set(value) == base | {"step", "choices", "outcome"}
                     and value.get("step") in ("background", "game", "screensaver", "summary")
                     and isinstance(choices, dict) and set(choices) == {"background", "screensaver"}
                     and choices["background"] in ("current", "phobos", "skip")
                     and type(choices["screensaver"]) is bool
                     and value.get("outcome") in (None, "finished", "skipped")
                     and (not value["show"] or value["outcome"] is None))
        else:
            valid = False
        if valid and path.stat().st_mode & 0o777 == 0o600:
            files = dict(journal["files"])
            files[rel] = dict(record, installed_sha256=digest(raw))
            return dict(journal, files=files)
    except (OSError, ValueError):
        pass
    return journal


def cleanup_generated_state(paths, journal, home: Path):
    """Only the installer-created onboarding lock and unchanged example logs."""
    notes = []
    candidates = []
    marker_rel = home_relative(home, paths.data_root / "onboarding.json")
    marker_record = journal["files"].get(marker_rel)
    if (marker_record and marker_record.get("original") is None
            and not (paths.data_root / "onboarding.json").exists() and not (paths.data_root / "onboarding.json").is_symlink()):
        candidates.append(paths.data_root / ".onboarding.lock")
    for agent_id in ("example-wander", "example-unstick"):
        manifest = paths.agent_config_root / agent_id / "agent.json"
        record = journal["files"].get(home_relative(home, manifest))
        # restore_files has already removed the unchanged owned manifest;
        # an edited or pre-install manifest keeps its related logs untouched.
        if record and record.get("original") is None and not manifest.exists() and not manifest.is_symlink():
            candidates += [paths.agent_state_root / (agent_id + suffix) for suffix in (".log", ".log.1", ".log.2")]
    for path in candidates:
        rel = home_relative(home, path)
        if symlink_ancestor(home, rel) is not None:
            continue
        fd = None
        try:
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o777 != 0o600:
                continue
            if path.name == ".onboarding.lock":
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            path.unlink()
            notes.append(f"Removed owned generated state: {path}")
        except (OSError, ValueError):
            continue
        finally:
            if fd is not None:
                os.close(fd)
    return notes


def prune_owned_empty_directories(paths, journal, home: Path):
    plugin = paths.config_root.parent / "omarchy/plugins" / published_id()
    candidates = set()
    for rel in journal["files"]:
        path = home / rel
        for base in (paths.agent_config_root, plugin):
            if base not in path.parents:
                continue
            if base == plugin and not re.fullmatch(r"impl-[0-9a-f]{64}", path.relative_to(base).parts[0]):
                continue
            parent = path.parent
            while parent != base:
                candidates.add(parent)
                parent = parent.parent
    for path in sorted(candidates, key=lambda p: len(p.parts), reverse=True):
        if path.is_symlink() or symlink_ancestor(home, home_relative(home, path)) is not None:
            continue
        try:
            path.rmdir()
        except OSError:
            pass


def marketplace_install(args, root: Path):
    try:
        if args.without_design_bundles:
            raise RuntimeError("Marketplace setup always validates its root manifest; --without-design-bundles belongs to legacy staging.")
        home = (args.prefix or Path.home()).expanduser().resolve()
        paths = project_paths(home, prefix=args.prefix is not None)
        journal_path = paths.state_root / "install.json"
        if (journal_path.is_symlink() or (home / SHELL_REL).is_symlink()
                or symlink_ancestor(home, SHELL_REL) is not None):
            raise RuntimeError("Refusing a symlinked install journal or shell.json")
        validate_marketplace(root)
        if args.prefix is None and not getattr(args, "dev", False) and root.resolve() != paths.config_root.parent / "omarchy/plugins" / published_id():
            raise RuntimeError("Run setup from the marketplace plugin checkout, or choose explicit --dev staging.")
        old = load_json(journal_path, require_owner=True)
        if old and (old.get("schema") != 2 or old.get("plugin_id") != published_id()):
            raise RuntimeError("Install journal format is unsupported")
        legacy = old.get("legacy") if old else load_json(home / JOURNAL_REL, require_owner=True)
        if legacy:
            legacy_ids(legacy)
        if args.menu_only and not (old or legacy):
            raise RuntimeError("Menu-only setup requires an existing installation")
        source_root = root
        snapshot = {}
        if getattr(args, "dev", False):
            root, snapshot = developer_snapshot(root, home, paths)
        files = dict(snapshot)
        files.update(marketplace_artifacts(source_root, home, paths, menu_only=args.menu_only))
        if snapshot and not args.menu_only:
            service_rel = home_relative(home, paths.config_root.parent / "systemd/user" / SERVICE_NAME)
            files[service_rel] = (render_service(source_root, installed_root=root), 0o644)
        if snapshot:
            # The launcher resolves the same implementation as the QML menu.
            launcher_rel = home_relative(home, paths.data_root.parent / "applications/live-doom.desktop")
            data, mode = files[launcher_rel]
            files[launcher_rel] = (data.replace(desktop_quote(str(source_root / "scripts/doomctl")).encode(),
                                               desktop_quote(str(root / "scripts/doomctl")).encode()), mode)
        if not args.menu_only:
            # Adopting an existing choice prevents first-run Steam preference
            # from replacing it. The source and all game/checkpoint data stay.
            for destination, name in ((paths.config_file, "config.json"), (paths.last_good_config, "config.last-good.json")):
                source = paths.legacy_data_root / name
                if not destination.exists() and not destination.is_symlink() and source.exists():
                    rel = home_relative(home, source)
                    if source.is_symlink() or symlink_ancestor(home, rel) is not None or not source.is_file():
                        raise RuntimeError(f"Unsafe legacy configuration: {source}")
                    from project_paths import read_bytes
                    files[home_relative(home, destination)] = (read_bytes(source, 1024 * 1024), 0o600)
        config, delta = marketplace_config(home, args.omarchy_path, old, legacy)
        prior_files = dict(old.get("files", {})) if old else {
            rel: rec for rel, rec in (legacy or {}).get("files", {}).items()
            if not rel.startswith(".config/omarchy/themes/")}
        records = journal_files(home, files, {"files": prior_files}, args.replace_modified)
        activate = args.prefix is None and not args.no_activate and not args.dry_run
        if activate and not args.menu_only:
            for path in (paths.engine, paths.viewer, source_root / "src/controller.py"):
                if not path.is_file() or path.is_symlink():
                    raise RuntimeError(f"Build is incomplete: {path}")
        print(f"{'Would set up' if args.dry_run else 'Setup plan for'} Live Doom under {home}")
        for rel in files:
            print(f"  {'replace' if (home / rel).exists() else 'create'}: {home / rel}")
        publish_registry = args.prefix is not None or activate
        print(f"  shell.json: enable {published_id()}; retire only the journal-owned legacy menu entry" if publish_registry
              else "  shell.json: unchanged; plugin enabling deferred by --no-activate")
        print("  idle selection, idle timers, theme and background: unchanged")
        print(f"  journal: {journal_path}")
        print("  first run: initialize settings/catalog before service startup; existing settings win")
        print(f"  config if absent: {paths.config_file}; prefer compatible Steam DOOM, otherwise Freedoom")
        print(f"  generated onboarding/catalog: {paths.data_root / 'onboarding.json'}, {paths.catalog_file}")
        print("  service: " + ("no process changes" if not activate or args.menu_only else "daemon-reload, enable/start; safely checkpoint then restart only an active upgraded game"))
        print("  shell: rescan/enable plugin only; no shell restart" if activate else "  shell: no IPC activation")
        if args.dry_run:
            return 0
        confirm_changes(args)
        if args.prefix is None:
            require_unlocked()
        if activate and (old or legacy) and not args.menu_only:
            require_upgrade_checkpoint(source_root)
        from project_paths import ensure_private_dir
        for directory in (paths.config_root, paths.state_root, paths.data_root):
            ensure_private_dir(directory)
        runtime = (old or legacy or {}).get("runtime")
        if activate and not args.menu_only and runtime is None:
            runtime = {"service_enabled": run(["systemctl", "--user", "is-enabled", "--quiet", SERVICE_NAME], check=False).returncode == 0,
                       "service_active": run(["systemctl", "--user", "is-active", "--quiet", SERVICE_NAME], check=False).returncode == 0}
        journal = {"schema": 2, "plugin_id": published_id(), "root": str(root), "files": records,
                   "config": delta, "legacy": legacy, "runtime": runtime}
        if not old and legacy:
            journal["legacy_journal_sha256"] = digest(artifact_bytes(home / JOURNAL_REL))
        elif old and "legacy_journal_sha256" in old:
            journal["legacy_journal_sha256"] = old["legacy_journal_sha256"]
        atomic_write(journal_path, json_bytes(journal), 0o600)
        manifest_rel = home_relative(home, paths.config_root.parent / "omarchy/plugins" / published_id() / "manifest.json")
        for rel, (data, mode) in files.items():
            if snapshot and rel == manifest_rel:
                continue
            atomic_write(home / rel, data, mode)
        onboarding = {"fresh": False, "show": False}
        if not args.menu_only:
            candidates = (paths.config_file, paths.data_root / "onboarding.json")
            absent = [p for p in candidates if not p.exists()]
            for p in candidates:
                rel = home_relative(home, p)
                if p.is_symlink() or symlink_ancestor(home, rel) is not None:
                    raise RuntimeError(f"Refusing symlinked first-run state: {p}")
            try:
                onboarding = initialize_marketplace(root if snapshot else source_root, home, paths)
            finally:
                for p in absent:
                    if p.is_file() and not p.is_symlink():
                        journal["files"][home_relative(home, p)] = {"original": None, "mode": p.stat().st_mode & 0o777,
                                                                  "installed_sha256": digest(artifact_bytes(p))}
                atomic_write(journal_path, json_bytes(journal), 0o600)
        if snapshot:
            data, mode = files[manifest_rel]
            atomic_write(home / manifest_rel, data, mode)
        if publish_registry:
            atomic_write(home / SHELL_REL, json_bytes(config))
        if not args.menu_only:
            print(scan_steam_catalog(root if snapshot else source_root, home, prefix=args.prefix is not None,
                                     state=paths.data_root))
        if activate:
            activate_marketplace(root, upgrade=old is not None or legacy is not None,
                                 menu_only=args.menu_only, show=bool(old is None and not args.menu_only and onboarding["show"]))
        print("Live Doom setup complete; game files and existing settings were preserved.")
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"Install failed: {error}", file=sys.stderr)
        return 1


def marketplace_uninstall(args):
    try:
        home = (args.prefix or Path.home()).expanduser().resolve()
        paths = project_paths(home, prefix=args.prefix is not None)
        journal_path = paths.state_root / "install.json"
        if journal_path.is_symlink():
            raise RuntimeError("Refusing a symlinked install journal")
        journal = load_json(journal_path, require_owner=True)
        if journal is None:
            print("No Live Doom installation journal was found; nothing changed.")
            return 0
        if journal.get("schema") != 2 or journal.get("plugin_id") != published_id():
            raise RuntimeError("Install journal format is unsupported")
        if (home / SHELL_REL).is_symlink() or symlink_ancestor(home, SHELL_REL) is not None:
            raise RuntimeError("Refusing symlinked shell.json")
        config = load_json(home / SHELL_REL)
        if not isinstance(config, dict):
            raise RuntimeError("shell.json must contain a JSON object")
        delta = journal["config"]
        if delta.get("added_entry"):
            config["plugins"] = [e for e in config.get("plugins", []) if e != {"id": published_id()}]
        # A migration keeps the legacy idle service intact until removal, then
        # uses its original recovery delta to restore stock/custom idle safely.
        legacy = journal.get("legacy")
        if legacy:
            legacy_config, notes = restore_config(home, legacy)
            if legacy_config is not None:
                # restore_config reads the same current config. Reapply only
                # this marketplace entry's ownership delta to that result.
                config = legacy_config
                if delta.get("added_entry"):
                    config["plugins"] = [e for e in config.get("plugins", []) if e != {"id": published_id()}]
        else:
            notes = []
        legacy_menu_id = legacy_ids(legacy)[1] if legacy else None
        if legacy_menu_id and not delta.get("legacy_menu_disabled"):
            config["disabledPlugins"] = [p for p in config.get("disabledPlugins", []) if p != legacy_menu_id]
        if delta.get("cleared_disabled") and published_id() not in config.get("disabledPlugins", []):
            disabled = config.setdefault("disabledPlugins", [])
            disabled.insert(min(delta.get("disabled_index", 0), len(disabled)), published_id())
        if not config.get("disabledPlugins") and not delta.get("disabled_present"):
            config.pop("disabledPlugins", None)
        activate = args.prefix is None and not args.no_activate and not args.dry_run
        print(f"{'Would remove' if args.dry_run else 'Removal plan for'} Live Doom under {home}")
        for rel in journal["files"]:
            print(f"  restore/remove unchanged owned artifact: {home / rel}")
        print("  preserve all themes, WADs, saves and later user edits")
        print("  service: disable/stop only if the installed unit still matches our ownership record" if activate else "  service: no process changes")
        print("  shell: restore only owned registry changes and rescan; no shell restart")
        if args.dry_run:
            return 0
        confirm_changes(args)
        if args.prefix is None:
            require_unlocked()
        service_rel = home_relative(home, paths.config_root.parent / "systemd/user" / SERVICE_NAME)
        service = home / service_rel
        rec = journal["files"].get(service_rel)
        service_owned = bool(rec and service.is_file() and not service.is_symlink()
                             and symlink_ancestor(home, service_rel) is None
                             and service.stat().st_mode & 0o777 == rec.get("mode", 0o644)
                             and artifact_matches(service, rec["installed_sha256"]))
        if activate and service_owned:
            run(["systemctl", "--user", "disable", "--now", SERVICE_NAME])
            notes += cleanup_owned_runtime(paths, journal, home)
        atomic_write(home / SHELL_REL, json_bytes(config))
        # Old bundled artwork belongs to a separately selectable theme now.
        # Never use legacy recovery records to remove it from marketplace undo.
        owned = cleanup_owned_onboarding(paths, journal, home)
        owned = dict(owned, files={rel: rec for rel, rec in owned["files"].items()
                                    if not rel.startswith(".config/omarchy/themes/")})
        notes += restore_files(home, owned)
        if args.prefix is not None or (activate and service_owned):
            notes += cleanup_generated_state(paths, owned, home)
        prune_owned_empty_directories(paths, owned, home)
        if activate:
            run(["omarchy-shell", "shell", "rescanPlugins"])
            run(["systemctl", "--user", "daemon-reload"])
            previous_runtime = journal.get("runtime") or {}
            if service_owned and rec.get("original") is not None:
                if previous_runtime.get("service_enabled"):
                    run(["systemctl", "--user", "enable", SERVICE_NAME])
                if previous_runtime.get("service_active"):
                    run(["systemctl", "--user", "start", SERVICE_NAME])
        for note in notes:
            print(note)
        theme_backups = any(rel.startswith(".config/omarchy/themes/") and rec.get("original") is not None
                            for rel, rec in (legacy or {}).get("files", {}).items())
        if any(note.startswith("Kept modified file:") for note in notes) or theme_backups:
            print(f"Recovery metadata retained: {journal_path}")
        else:
            journal_path.unlink()
            legacy_path = home / JOURNAL_REL
            if (journal.get("legacy_journal_sha256") and legacy_path.is_file() and not legacy_path.is_symlink()
                    and artifact_matches(legacy_path, journal["legacy_journal_sha256"])):
                legacy_path.unlink()
        print("Live Doom integration removed; the checkout, independent themes, WADs and saves remain.")
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Uninstall failed: {error}", file=sys.stderr)
        return 1


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()


def load_json(path: Path, fallback=None, *, require_owner=False):
    if not path.exists():
        return fallback
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from project_paths import read_bytes
    try:
        return json.loads(read_bytes(path, 16 * 1024 * 1024, require_owner=require_owner))
    except RecursionError:
        raise ValueError("JSON nesting is too deep") from None


def atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    # Unchanged config/plugin replacements still notify Omarchy's watchers.
    # Preserve the inode and mtime when this install has nothing to update.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from project_paths import _directory_fd, _check_replace_target, read_bytes
    if not path.is_symlink() and path.is_file() and path.stat().st_mode & 0o777 == mode:
        try:
            if read_bytes(path, len(data)) == data:
                return
        except ValueError:
            pass
    with _directory_fd(path.parent, create=True) as parent:
        _check_replace_target(parent, path.name, path)
        fd, temporary = tempfile.mkstemp(prefix=".doom-install-", dir=f"/proc/self/fd/{parent}")
        name = Path(temporary).name
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), mode)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            _check_replace_target(parent, path.name, path)
            os.replace(name, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(name, dir_fd=parent)
            except FileNotFoundError:
                pass


def run(args: list[str], timeout: int = 15, check: bool = True, *, max_output_bytes=1024 * 1024):
    command = list(args)
    if not command:
        raise RuntimeError("Process command is empty")
    if not Path(command[0]).is_absolute():
        resolved = shutil.which(command[0], path="/usr/bin:/bin") if Path(command[0]).name == command[0] else None
        if resolved is None:
            raise RuntimeError(f"Command is unavailable in /usr/bin:/bin: {command[0]}")
        command[0] = resolved
    try:
        result = bounded_run(command, text=True, timeout=timeout, max_output_bytes=max_output_bytes)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"{' '.join(args)} timed out after {timeout} seconds") from error
    except ValueError as error:
        raise RuntimeError(f"{' '.join(args)} failed: {error}") from error
    if check and result.returncode:
        raise RuntimeError(f"{' '.join(args)} failed: {(result.stderr.strip() or result.stdout.strip())[:4096]}")
    return result


def scan_steam_catalog(root: Path, home: Path, *, prefix: bool = False, state: Path | None = None) -> str:
    """Refresh only the generated game catalog through a bounded private CLI.

    Prefix stages must ignore the host's data/state overrides. Catalogs stay
    outside journal.files: uninstall must not restore an older runtime scan.
    """
    if state is not None:
        state = Path(state)
    elif prefix:
        state = home / ".local/share/doom-desktop"
    else:
        data = Path(os.environ.get("XDG_DATA_HOME", str(home / ".local/share")))
        state = Path(os.environ.get("DOOM_DESKTOP_STATE", str(data / "doom-desktop")))
    command = [sys.executable, str(root / "src/game_catalog.py"), "scan",
               "--home", str(home), "--state", str(state)]
    try:
        result = run(command, timeout=8, check=False, max_output_bytes=4096)
        if result.returncode:
            return f"Steam scan unavailable (exit {result.returncode}); game selection unchanged."
        if len(result.stdout) > 4096:
            raise ValueError("Oversized Steam scan summary")
        summary = json.loads(result.stdout)
        if (not isinstance(summary, dict) or not isinstance(summary.get("summary"), str)
                or type(summary.get("count")) is not int or summary["count"] < 0):
            raise ValueError("Invalid Steam scan summary")
        text = " ".join(summary["summary"].split())[:256]
        return f"Steam scan: {text} ({summary['count']} catalog entries); game selection unchanged."
    except (OSError, ValueError, RuntimeError):
        return "Steam scan unavailable; installation continues with the current game selection."


def require_unlocked() -> None:
    result = run(["omarchy-shell", "lock", "isLocked"], check=False, timeout=5)
    if result.returncode or result.stdout.strip() not in {"false", "true"}:
        raise RuntimeError("Cannot verify Omarchy lock state; use --no-activate to install files without changing running services.")
    if result.stdout.strip() == "true":
        raise RuntimeError("The session is locked. Unlock before activating the Doom integration, or use --no-activate to stage files.")


def source_dir(omarchy: Path) -> Path:
    candidates = [omarchy / "shell/plugins/services/idle", omarchy / "shell/plugins/idle"]
    for candidate in candidates:
        if (candidate / "manifest.json").is_file() and (candidate / "Service.qml").is_file():
            return candidate
    raise RuntimeError(f"Omarchy's idle plugin was not found under {omarchy}")


def clone_files(root: Path, omarchy: Path) -> dict[str, tuple[bytes, int]]:
    source = source_dir(omarchy)
    plugin_prefix = f".config/omarchy/plugins/{LEGACY_IDLE_ID}"
    files = {}
    for path in sorted(source.rglob("*")):
        if path.is_file():
            files[f"{plugin_prefix}/{path.relative_to(source)}"] = (artifact_bytes(path, require_owner=False), 0o644)
    manifest = json.loads(files[f"{plugin_prefix}/manifest.json"][0])
    manifest["id"] = LEGACY_IDLE_ID
    manifest["name"] = "Live Doom Idle"
    manifest["description"] = "Omarchy idle timers with the shared Doom screensaver."
    manifest["omarchy"] = dict(manifest.get("omarchy") or {}, clonedFrom=SOURCE_ID)
    files[f"{plugin_prefix}/manifest.json"] = (json_bytes(manifest), 0o644)
    qml = files[f"{plugin_prefix}/Service.qml"][0].decode()
    qml, class_count = re.subn(
        r'(readonly property string screensaverClass:\s*)"[^"\n]*"',
        lambda match: match[1] + json.dumps(SAVER_CLASS), qml,
    )
    command = '[[ $(omarchy-shell lock isLocked 2>/dev/null) == "true" ]] || ' + shlex.quote(str(root / "scripts/doomctl")) + " screensaver"
    qml, launch_count = re.subn(
        r'(?m)^(\s*)runProcess\(screensaverProcess,\s*"screensaver",\s*".*"\)\s*$',
        lambda match: match[1] + 'runProcess(screensaverProcess, "screensaver", ' + json.dumps(command) + ')', qml,
    )
    # Idle notifications can become active while the session remains locked.
    # A cancellation must not wake a display behind the lock or bypass its
    # blank timer. Real input/unlock still uses Omarchy's own lock service.
    wake_command = ('if doom_lock_state=$(omarchy-shell lock isLocked 2>/dev/null) '
                    '&& [[ "$doom_lock_state" == "false" ]]; then omarchy-system-wake; fi')
    qml, wake_count = re.subn(
        r'runProcess\(wakeProcess,\s*"wake",\s*"omarchy-system-wake"\)',
        lambda match: 'runProcess(wakeProcess, "wake", ' + json.dumps(wake_command) + ')', qml,
    )
    if (class_count, launch_count, wake_count) != (1, 1, 1):
        raise RuntimeError("Omarchy's idle plugin format changed; refusing to install an unverified timer patch.")
    files[f"{plugin_prefix}/Service.qml"] = (qml.encode(), 0o644)
    return files


def systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def desktop_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$").replace("%", "%%") + '"'


def bundle_files(root: Path) -> dict[str, tuple[bytes, int]]:
    """Copy the bundled user theme/menu without selecting a theme or background.

    Missing directories are allowed while developing the native frontend.
    A present bundle must be complete, so partial updates fail before writes.
    Source symlinks are rejected instead of following them out of the bundle.
    """
    files = {}
    for directory, destination in (
        (root / "theme", f".config/omarchy/themes/{THEME_SLUG}"),
        (root / "menu", f".config/omarchy/plugins/{LEGACY_MENU_ID}"),
    ):
        if not directory.exists():
            continue
        if directory.is_symlink() or not directory.is_dir():
            raise RuntimeError(f"Invalid bundled directory: {directory}")
        if directory.name == "theme":
            if not (directory / "colors.toml").is_file():
                raise RuntimeError("Bundled Phobos theme is missing colors.toml")
            if not any("live-doom" in path.name.lower() and path.is_file()
                       for path in (directory / "backgrounds").glob("*")):
                raise RuntimeError("Bundled Phobos theme is missing its live-doom background choice")
        else:
            manifest = load_json(directory / "manifest.json", {})
            if (not isinstance(manifest, dict) or manifest.get("id") != LEGACY_MENU_ID
                    or not isinstance(manifest.get("kinds"), list)
                    or "overlay" not in manifest["kinds"]
                    or not isinstance(manifest.get("entryPoints"), dict)):
                raise RuntimeError("Bundled Doom menu manifest is invalid")
            entry = manifest["entryPoints"].get("overlay")
            if entry != "Menu.qml":
                raise RuntimeError("Bundled Doom menu overlay must be Menu.qml; refusing an unverified entry-point contract")
            if not (directory / entry).is_file():
                raise RuntimeError("Bundled Doom menu overlay entry point is missing")
        source_files = {}
        for path in sorted(directory.rglob("*")):
            relative = path.relative_to(directory)
            if is_python_cache(relative):
                continue
            if path.is_symlink():
                raise RuntimeError(f"Refusing bundled symlink: {path}")
            if path.is_file():
                mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
                source_files[str(relative)] = (artifact_bytes(path), mode)
        if directory.name == "menu":
            # Qt caches implicit QML types by their URL. A new manifest URL
            # must move the whole implementation, including sibling types,
            # rather than only cache-busting the root Menu.qml component.
            implementation = {rel: value for rel, value in source_files.items()
                              if rel not in {"manifest.json", "backend.json"}}
            binding = {
                "doomctl": str(root.resolve() / "scripts/doomctl"),
                "python": "/usr/bin/python3",
            }
            content = {"binding": binding, "files": [
                {"path": rel, "mode": mode, "sha256": digest(data)}
                for rel, (data, mode) in sorted(implementation.items())
            ]}
            impl = "impl-" + digest(json_bytes(content))
            manifest["entryPoints"]["overlay"] = impl + "/Menu.qml"
            backend = (json_bytes(dict(binding, impl=impl)), 0o644)
            for rel, value in implementation.items():
                files[f"{destination}/{impl}/{rel}"] = value
            files[f"{destination}/{impl}/backend.json"] = backend
            files[f"{destination}/backend.json"] = backend
            # Publish the new entry URL only after every sibling and backend
            # exists; Omarchy's file watcher can reload during these writes.
            files[f"{destination}/manifest.json"] = (json_bytes(manifest), 0o644)
        else:
            for rel, value in source_files.items():
                files[f"{destination}/{rel}"] = value
    return files


def is_python_cache(path: Path) -> bool:
    return "__pycache__" in path.parts or path.suffix.lower() in {".pyc", ".pyo"}


def prune_generated_bundle_caches(home: Path, journal: dict) -> list[str]:
    """Remove only unchanged, unbacked caches owned by an earlier installer.

    Ordinary obsolete assets are deliberately untouched. Edited/symlinked
    caches and caches with a pre-install original retain their file and backup
    record, so a later uninstall can still honor the original installation.
    """
    notes = []
    prefixes = (f".config/omarchy/plugins/{LEGACY_MENU_ID}/", f".config/omarchy/themes/{THEME_SLUG}/")
    for rel, record in list(journal["files"].items()):
        if not rel.startswith(prefixes) or not is_python_cache(Path(rel)):
            continue
        validate_relative(rel)
        path = home / rel
        if record.get("original") is not None:
            notes.append(f"Kept backed-up generated cache: {path}")
            continue
        if (symlink_ancestor(home, rel) is not None or path.is_symlink()
                or (path.exists() and (not path.is_file()
                    or path.stat().st_mode & 0o777 != record.get("mode", 0o644)
                    or not artifact_matches(path, record["installed_sha256"])))):
            notes.append(f"Kept modified generated cache: {path}")
            continue
        if path.exists():
            path.unlink()
        del journal["files"][rel]
        notes.append(f"Removed unchanged generated cache: {path}")
        # Only an empty compiler-cache directory is eligible for pruning.
        # Keep all other directories and any files the user added there.
        if path.parent.name == "__pycache__":
            try:
                path.parent.rmdir()
            except OSError:
                pass
    return notes


def selected_background(home: Path) -> tuple[Path | None, bool]:
    """Read a stable picker target; an unreadable selection fails closed."""
    link = home / ".local/state/omarchy/current/background"
    try:
        before = link.lstat()
    except FileNotFoundError:
        return None, True
    except OSError:
        return None, False
    if not stat.S_ISLNK(before.st_mode):
        return None, False
    try:
        target = link.resolve(strict=True)
        after = link.lstat()
    except (OSError, RuntimeError):
        return None, False
    identity = lambda value: (value.st_dev, value.st_ino, value.st_mtime_ns, value.st_ctime_ns)
    return target, identity(before) == identity(after)


def prune_obsolete_theme_backgrounds(home: Path, journal: dict, files: dict) -> list[str]:
    """Prune only retired, unchanged backgrounds introduced by this install.

    A currently selected file, user edits, symlinks, pre-install originals,
    and their recovery records are preserved. Menu implementations and other
    theme assets are outside this migration. No theme is applied or selected.
    """
    theme = f".config/omarchy/themes/{THEME_SLUG}"
    if theme + "/colors.toml" not in files:
        return []  # A menu-only development bundle is not a theme removal.
    prefix = theme + "/backgrounds/"
    notes = []
    for rel, record in list(journal["files"].items()):
        if not rel.startswith(prefix) or rel in files:
            continue
        validate_relative(rel)
        path = home / rel
        if record.get("original") is not None:
            notes.append(f"Kept backed-up obsolete background: {path}")
            continue
        if (symlink_ancestor(home, rel) is not None or path.is_symlink()
                or not path.is_file()
                or path.stat().st_mode & 0o777 != record.get("mode", 0o644)
                or not artifact_matches(path, record["installed_sha256"])):
            notes.append(f"Kept modified or absent obsolete background: {path}")
            continue
        # Recheck for each removal: a picker change during this install must
        # not leave the current background link pointing at a pruned file.
        selected, known = selected_background(home)
        if not known or selected == path.resolve():
            notes.append(f"Kept selected or unverifiable obsolete background: {path}")
            continue
        path.unlink()
        del journal["files"][rel]
        notes.append(f"Removed unchanged obsolete background: {path}")
    return notes


@contextmanager
def theme_background_lock(home: Path):
    # Stage fixtures must never lock the live session's theme operations.
    if home.resolve() == Path.home().resolve():
        value = os.environ.get("XDG_RUNTIME_DIR")
        if not value:
            raise RuntimeError("XDG_RUNTIME_DIR must be an existing owned private directory for theme changes")
        from project_paths import _directory_fd
        runtime = Path(value)
        with _directory_fd(runtime, private=True):
            pass
        path = runtime / "omarchy-theme-set.lock"
    else:
        rel = ".local/state/doom-desktop/omarchy-theme-set.lock"
        if symlink_ancestor(home, rel) is not None:
            raise RuntimeError("Refusing a symlink directory for the staged theme lock")
        path = home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + 5
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Omarchy theme change is still running; retry this install after it finishes")
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def sync_active_theme_backgrounds(home: Path, journal: dict, previous: dict | None, files: dict) -> list[str]:
    """Update only proven, unchanged background copies in the active Phobos.

    Mirror provenance is separate from ``files``: uninstall must not delete
    Omarchy's current theme snapshot. Nothing here applies a theme or touches
    its palette, theme.name, or selected background link.
    """
    source_prefix = f".config/omarchy/themes/{THEME_SLUG}/backgrounds/"
    new = {rel[len(source_prefix):]: value for rel, value in files.items()
           if rel.startswith(source_prefix)}
    if not new:
        return []
    previous = previous or {}
    old = {rel[len(source_prefix):]: record for rel, record in previous.get("files", {}).items()
           if rel.startswith(source_prefix)}
    mirror = dict(previous.get("background_mirror", {}))
    current_rel = ".local/state/omarchy/current/theme/backgrounds"
    current = home / current_rel
    theme_name = home / ".local/state/omarchy/current/theme.name"

    def valid_theme():
        try:
            return (symlink_ancestor(home, current_rel + "/placeholder") is None
                    and current.is_dir() and not current.is_symlink()
                    and theme_name.is_file() and not theme_name.is_symlink()
                    and secure_read_bytes(theme_name, 256).decode().rstrip("\n") == THEME_SLUG)
        except (OSError, ValueError):
            return False

    notes = []
    if not valid_theme():
        return []
    with theme_background_lock(home):
        if not valid_theme():
            return []
        for name in sorted(set(old) | set(mirror) | set(new)):
            validate_relative(name)
            rel = current_rel + "/" + name
            path = home / rel
            source = journal["files"].get(source_prefix + name) or old.get(name, {})
            if source.get("original") is not None:
                notes.append(f"Kept backed-up active background copy: {path}")
                continue
            if symlink_ancestor(home, rel) is not None or path.is_symlink():
                notes.append(f"Kept symlinked active background copy: {path}")
                continue
            known = [record for record in (old.get(name), mirror.get(name))
                     if isinstance(record, dict) and record.get("original") is None]
            matches = None
            if path.exists():
                if not path.is_file():
                    continue
                try:
                    actual = {"installed_sha256": digest(artifact_bytes(path)), "mode": path.stat().st_mode & 0o777}
                except (OSError, ValueError):
                    notes.append(f"Kept unreadable or oversized active background copy: {path}")
                    continue
                matches = next((record for record in known
                                if actual["installed_sha256"] == record.get("installed_sha256")
                                and actual["mode"] == record.get("mode", 0o644)), None)
                if matches is None:
                    notes.append(f"Kept untracked or modified active background copy: {path}")
                    continue
                mirror[name] = actual
            elif name not in new:
                mirror.pop(name, None)
                continue
            # Stock theme changes share this flock. Recheck both pieces of
            # selection state immediately before each copy or removal.
            selected, selection_known = selected_background(home)
            if not valid_theme() or not selection_known:
                notes.append("Kept active background copies: selection changed or could not be verified")
                break
            if selected == path.resolve():
                notes.append(f"Kept selected active background copy: {path}")
                continue
            if name in new:
                data, mode = new[name]
                atomic_write(path, data, mode)
                mirror[name] = {"installed_sha256": digest(data), "mode": mode}
                notes.append(f"Synchronized active background copy: {path}")
            else:
                path.unlink()
                mirror.pop(name, None)
                notes.append(f"Removed unchanged obsolete active background copy: {path}")
        if mirror:
            journal["background_mirror"] = mirror
        else:
            journal.pop("background_mirror", None)
    return notes


def launcher_files(root: Path) -> dict[str, tuple[bytes, int]]:
    desktop = "\n".join([
        "[Desktop Entry]", "Type=Application", "Name=Live Doom",
        "Comment=Configure the live Doom background and screensaver",
        "Icon=applications-games", "Categories=Game;", "Terminal=false",
        "StartupNotify=false", "Exec=" + desktop_quote(str(root / "scripts/doomctl")) + " open-menu --origin=launcher", "",
    ])
    return {".local/share/applications/doom-wallpaper.desktop": (desktop.encode(), 0o644)}


def agent_example_files(root: Path) -> dict[str, tuple[bytes, int]]:
    """Bundle optional original examples with a local stdlib SDK per folder."""
    sdk = root / 'sdk/python/doomagent.py'
    examples = root / 'examples/agents'
    if not sdk.is_file() or not examples.is_dir():
        return {}
    files = {}
    for agent_id in ('wander', 'unstick'):
        folder = examples / agent_id
        if not (folder / 'agent.json').is_file():
            continue
        prefix = f'.config/doom-desktop/agents/example-{agent_id}'
        for name in ('agent.json', f'{agent_id}.py'):
            path = folder / name
            if not path.is_file() or path.is_symlink():
                raise RuntimeError(f'Incomplete example agent: {path}')
            files[f'{prefix}/{name}'] = (artifact_bytes(path), 0o644)
        files[f'{prefix}/doomagent.py'] = (artifact_bytes(sdk), 0o644)
    return files


def artifacts(root: Path, omarchy: Path, include_design=True, menu_only=False) -> dict[str, tuple[bytes, int]]:
    files = {} if menu_only else clone_files(root, omarchy)
    files.update(agent_example_files(root))
    if include_design:
        files.update(bundle_files(root))
    if menu_only:
        if f".config/omarchy/plugins/{LEGACY_MENU_ID}/manifest.json" not in files:
            raise RuntimeError("Menu-only installation requires the bundled Doom menu")
        files.update(launcher_files(root))
        return files
    # Render arguments individually: quoting a substituted ROOT before /src
    # would make paths containing spaces ambiguous in systemd's command parser.
    files[f".config/systemd/user/{SERVICE_NAME}"] = (render_service(root), 0o644)
    files.update(launcher_files(root))
    return files


def active_idle_clones(home: Path, config: dict) -> list[dict]:
    entries = config.get("plugins", [])
    if not isinstance(entries, list):
        raise RuntimeError("shell.json plugins must be an array")
    found = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        plugin_id = entry["id"]
        if plugin_id == LEGACY_IDLE_ID:
            found.append(entry)
            continue
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", plugin_id):
            continue
        manifest = load_json(home / ".config/omarchy/plugins" / plugin_id / "manifest.json", {})
        if manifest.get("omarchy", {}).get("clonedFrom") == SOURCE_ID:
            found.append(entry)
    return found


def prepare_config(home: Path, omarchy: Path, old_journal=None, install_menu=False) -> tuple[dict, dict]:
    config = load_json(home / SHELL_REL)
    if config is None:
        config = load_json(omarchy / "config/omarchy/shell.json", {"version": 1, "plugins": []})
    if not isinstance(config, dict):
        raise RuntimeError("shell.json must contain a JSON object")
    clones = active_idle_clones(home, config)
    disabled = config.get("disabledPlugins", [])
    restores = config.get("cloneSourceRestores", [])
    if not isinstance(disabled, list) or not isinstance(restores, list):
        raise RuntimeError("shell.json disabledPlugins and cloneSourceRestores must be arrays")
    delta = dict(old_journal["config"]) if old_journal else {
        "previous_clones": clones,
        "source_disabled": SOURCE_ID in disabled,
        "source_explicit_entries": [entry for entry in config.get("plugins", []) if isinstance(entry, dict) and entry.get("id") == SOURCE_ID],
        "own_disabled": LEGACY_IDLE_ID in disabled,
        "own_restore": LEGACY_IDLE_ID in restores,
    }
    replaced_ids = {entry["id"] for entry in clones}
    plugins = []
    inserted = False
    for entry in config.get("plugins", []):
        if isinstance(entry, dict) and entry.get("id") in replaced_ids | {SOURCE_ID}:
            if not inserted:
                plugins.append({"id": LEGACY_IDLE_ID})
                inserted = True
        else:
            plugins.append(entry)
    if not inserted:
        plugins.append({"id": LEGACY_IDLE_ID})
    config["plugins"] = plugins
    config["disabledPlugins"] = [item for item in disabled if item != LEGACY_IDLE_ID]
    if SOURCE_ID not in config["disabledPlugins"]:
        config["disabledPlugins"].append(SOURCE_ID)
    # Match Omarchy's supported clone replacement metadata. This lets its own
    # Disabling the developer clone restores the first-party service.
    config["cloneSourceRestores"] = [item for item in restores if item != LEGACY_IDLE_ID]
    if not delta["source_disabled"]:
        config["cloneSourceRestores"].append(LEGACY_IDLE_ID)
    if not config["cloneSourceRestores"]:
        config.pop("cloneSourceRestores")
    if install_menu:
        prepare_menu_entry(config, delta)
    return config, delta


def prepare_menu_entry(config: dict, delta: dict) -> None:
    plugins = config.get("plugins", [])
    disabled = config.get("disabledPlugins", [])
    if not isinstance(plugins, list) or not isinstance(disabled, list):
        raise RuntimeError("shell.json plugins and disabledPlugins must be arrays")
    menu_present = any(isinstance(entry, dict) and entry.get("id") == LEGACY_MENU_ID for entry in plugins)
    if "menu" not in delta:
        delta["menu"] = {"added_entry": not menu_present, "cleared_disabled": LEGACY_MENU_ID in disabled}
    if not menu_present:
        config["plugins"] = plugins + [{"id": LEGACY_MENU_ID}]
    if LEGACY_MENU_ID in disabled:
        config["disabledPlugins"] = [item for item in disabled if item != LEGACY_MENU_ID]


def prepare_menu_config(home: Path, old_journal: dict) -> tuple[dict, dict]:
    """Enable only the overlay, preserving current idle selection verbatim."""
    config = load_json(home / SHELL_REL)
    if not isinstance(config, dict):
        raise RuntimeError("Menu-only installation requires an existing shell.json object")
    delta = dict(old_journal["config"])
    prepare_menu_entry(config, delta)
    return config, delta


def validate_relative(rel: str) -> None:
    if Path(rel).is_absolute() or ".." in Path(rel).parts:
        raise RuntimeError(f"Unsafe path in install journal: {rel}")


def symlink_ancestor(home: Path, rel: str) -> Path | None:
    parent = home
    for component in Path(rel).parts[:-1]:
        parent /= component
        if parent.is_symlink():
            return parent
    return None


def bundled_example(rel):
    parts = Path(rel).parts
    if (len(parts) < 4 or parts[-4] not in {"live-doom", "doom-desktop"}
            or parts[-3] != "agents" or parts[-2] not in {"example-wander", "example-unstick"}):
        return False
    return parts[-1] in {"agent.json", "doomagent.py", parts[-2].removeprefix("example-") + ".py"}


def journal_files(home: Path, files: dict, old_journal=None, replace_modified=False) -> dict:
    previous = old_journal.get("files", {}) if old_journal else {}
    records = {}
    for rel, (data, mode) in list(files.items()):
        validate_relative(rel)
        path = home / rel
        prior = previous.get(rel)
        preserve_example = prior and bundled_example(rel) and not replace_modified

        def keep_example():
            files.pop(rel)
            print(f"Kept modified bundled example: {path}")

        linked_parent = symlink_ancestor(home, rel)
        if linked_parent is not None:
            if preserve_example:
                keep_example()
                continue
            raise RuntimeError(f"Refusing to write through symlink directory: {linked_parent}")
        if path.is_symlink():
            if preserve_example:
                keep_example()
                continue
            raise RuntimeError(f"Refusing to replace symlink: {path}")
        try:
            existing = artifact_bytes(path) if path.exists() else None
        except (OSError, ValueError):
            if preserve_example:
                keep_example()
                continue
            raise
        if prior:
            edited = existing is not None and (digest(existing) != prior["installed_sha256"]
                                               or path.stat().st_mode & 0o777 != prior.get("mode", 0o644))
            if edited and preserve_example:
                keep_example()
                continue
            if edited and not replace_modified:
                raise RuntimeError(f"Installed file was edited: {path}. Use --replace-modified only if you intend to replace that edit.")
            original = prior["original"]
        elif existing is not None:
            original = {"data": base64.b64encode(existing).decode(), "mode": path.stat().st_mode & 0o777}
        else:
            original = None
        records[rel] = {"installed_sha256": digest(data), "original": original, "mode": mode}
    # Keep records for previous-version artifacts so uninstall also handles
    # them; never forget a backup during an upgrade.
    result = dict(previous, **records)
    if len(json_bytes(result)) > MAX_ARTIFACT_BYTES:
        raise RuntimeError("Recovery metadata exceeds the bounded journal size; no files were changed")
    return result


def wait_for_plugin(plugin_id=LEGACY_IDLE_ID, cloned_from=SOURCE_ID) -> None:
    # rescanPlugins returns before the asynchronous catalog scan finishes.
    # The stock clone command uses the same discovery handshake.
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        result = run(["omarchy", "plugin", "list", "--json"], timeout=2, check=False)
        try:
            catalog = json.loads(result.stdout)
        except ValueError:
            catalog = []
        if result.returncode == 0 and isinstance(catalog, list) and any(
            isinstance(item, dict) and item.get("id") == plugin_id
            and (cloned_from is None or item.get("clonedFrom") == cloned_from)
            for item in catalog
        ):
            return
        time.sleep(0.05)
    raise RuntimeError(f"Omarchy did not discover {plugin_id}; staged files and backup metadata were retained.")


def wait_for_idle() -> None:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        result = run(["omarchy-shell", "idle", "status"], timeout=2, check=False)
        try:
            status = json.loads(result.stdout)
        except ValueError:
            status = {}
        if result.returncode == 0 and isinstance(status, dict) and "screensaver" in status and "lock" in status:
            return
        time.sleep(0.05)
    raise RuntimeError("Omarchy idle service did not become ready; staged files and backup metadata were retained.")


def require_upgrade_checkpoint(root: Path) -> bool:
    """Refuse an active native upgrade unless its current world can be saved.

    Never pause a player or advance an intermission to make an upgrade fit.
    The caller can stage files or install only the menu while waiting for a
    naturally paused live level. An active but unloaded game also needs no
    checkpoint; settings corroborates that an empty header means unloaded.
    """
    service = run(["systemctl", "--user", "show", SERVICE_NAME,
                   "--property=ActiveState", "--value"], check=False, timeout=5)
    state = service.stdout.strip()
    if service.returncode or state not in {"active", "inactive", "failed"}:
        raise RuntimeError("Cannot verify Doom service state; refusing a native upgrade. Use --no-activate to stage files.")
    if state != "active":
        return False
    doomctl = str(root.resolve() / "scripts/doomctl")

    def paused_level():
        result = run([doomctl, "status"], check=False, timeout=5)
        try:
            status = json.loads(result.stdout)
        except ValueError:
            status = None
        if result.returncode or not isinstance(status, dict):
            raise RuntimeError("Cannot verify the live Doom world; refusing a native upgrade. Use --no-activate or --menu-only.")
        engine = status.get("engine")
        if status.get("human_output") or status.get("mode") == "human":
            raise RuntimeError("Human takeover is active; refusing to restart Doom. Press F12 to return to the bot, then run doomctl pause before this update and doomctl resume afterwards; or use --menu-only.")
        if engine == {} and status.get("mode") == "paused":
            settings = run([doomctl, "settings"], check=False, timeout=5)
            try:
                value = json.loads(settings.stdout)
            except ValueError:
                value = None
            if (settings.returncode == 0 and isinstance(value, dict)
                    and isinstance(value.get("runtime"), dict) and value["runtime"].get("loaded") is False):
                return None
            raise RuntimeError("Cannot confirm that Doom is unloaded; refusing a native upgrade. Use --no-activate or --menu-only.")
        if (not isinstance(engine, dict) or not engine.get("pid")
                or engine.get("gamestate") != 0 or status.get("mode") != "paused"
                or engine.get("paused") != 1 or engine.get("audible") != 0):
            raise RuntimeError("Native upgrade requires a paused, muted live level that can be checkpointed. For a running bot, including All desktops, run doomctl pause before this update and doomctl resume afterwards. Wait for intermissions/finales to reach a live level, or use --no-activate or --menu-only.")
        return engine

    before = paused_level()
    if before is None:
        if paused_level() is not None:
            raise RuntimeError("Doom loaded during upgrade validation; no restart was attempted. Retry when it is unloaded or safely paused.")
        return True
    saved = run([doomctl, "save"], check=False, timeout=10)
    if saved.returncode or saved.stdout.strip() != "OK":
        reason = getattr(saved, "stderr", "").strip() or saved.stdout.strip() or "no checkpoint confirmation"
        raise RuntimeError(f"Unable to checkpoint the live Doom level; no restart was attempted: {reason}")
    after = paused_level()
    if after is None:
        raise RuntimeError("Doom unloaded during checkpoint validation; no restart was attempted. Retry with a stable game state.")
    preserved = ("pid", "gamestate", "leveltime", "health", "x", "y", "kills", "items", "secrets", "map", "angle")
    if any(before.get(key) != after.get(key) for key in preserved):
        raise RuntimeError("Doom world changed during checkpoint validation; refusing to restart it. Retry when the live level remains paused.")
    return True


def activate_install(upgrade: bool, enable_menu=False, root=None) -> None:
    was_active = False
    if upgrade:
        require_unlocked()
        was_active = require_upgrade_checkpoint(root or Path(__file__).resolve().parents[1])
    run(["systemctl", "--user", "daemon-reload"])
    run(["systemctl", "--user", "enable", "--now", SERVICE_NAME])
    if upgrade:
        # Checkpoint and reload changed Python/native code, then use Omarchy's
        # supported unlocked restart to refresh keepLoaded idle QML reliably.
        if was_active:
            # daemon-reload/enable can take time. Recheck at the restart
            # boundary so activity or a new takeover refuses this upgrade.
            require_upgrade_checkpoint(root or Path(__file__).resolve().parents[1])
            run(["systemctl", "--user", "restart", SERVICE_NAME])
        require_unlocked()
        run(["omarchy", "restart", "shell"], timeout=30)
    else:
        run(["omarchy-shell", "shell", "rescanPlugins"])
    wait_for_plugin()
    run(["omarchy", "plugin", "enable", LEGACY_IDLE_ID])
    if enable_menu:
        wait_for_plugin(LEGACY_MENU_ID, cloned_from=None)
        run(["omarchy", "plugin", "enable", LEGACY_MENU_ID])
        wait_for_menu_code()
    wait_for_idle()
    run(["update-desktop-database", str(Path.home() / ".local/share/applications")], check=False)


def wait_for_menu_code(home: Path | None = None) -> None:
    """Verify the actual QML instance, not merely its registered manifest."""
    plugin = (home or Path.home()) / ".config/omarchy/plugins" / LEGACY_MENU_ID
    backend = load_json(plugin / "backend.json", {})
    impl = backend.get("impl") if isinstance(backend, dict) else None
    if not isinstance(impl, str) or not re.fullmatch(r"impl-[0-9a-f]{64}", impl):
        raise RuntimeError("Installed Doom menu lacks a valid content-versioned implementation")
    expected = plugin / impl
    deadline = time.monotonic() + 8
    last = "unknown"
    while time.monotonic() < deadline:
        try:
            result = run(["omarchy-shell", "shell", "call", LEGACY_MENU_ID, "loadedVersion", ""], check=False, timeout=2)
            last = result.stdout.strip()
            if result.returncode == 0 and last.rstrip("/") == str(expected):
                return
        except (OSError, subprocess.SubprocessError) as exc:
            last = str(exc)
        time.sleep(0.05)
    raise RuntimeError("Omarchy has not loaded the current Doom menu code: " + last)


def activate_menu() -> None:
    """Discover/enable the overlay without replacing kept idle or game state."""
    require_unlocked()
    run(["omarchy-shell", "shell", "rescanPlugins"])
    wait_for_plugin(LEGACY_MENU_ID, cloned_from=None)
    run(["omarchy", "plugin", "enable", LEGACY_MENU_ID])
    wait_for_menu_code()
    run(["update-desktop-database", str(Path.home() / ".local/share/applications")], check=False)


def restore_config(home: Path, journal: dict) -> tuple[dict | None, list[str]]:
    config = load_json(home / SHELL_REL)
    if config is None:
        return None, []
    if not isinstance(config, dict):
        raise RuntimeError("shell.json must contain a JSON object")
    delta = journal["config"]
    idle_id, menu_id = legacy_ids(journal)
    own_was_active = any(isinstance(entry, dict) and entry.get("id") == idle_id for entry in config.get("plugins", []))
    config["plugins"] = [entry for entry in config.get("plugins", []) if not isinstance(entry, dict) or entry.get("id") != idle_id]
    other_clones = active_idle_clones(home, config)
    notes = []
    if own_was_active and not other_clones:
        existing = {entry.get("id") for entry in config["plugins"] if isinstance(entry, dict)}
        for entry in delta["previous_clones"] + delta.get("source_explicit_entries", []):
            if entry["id"] not in existing:
                config["plugins"].append(entry)
                existing.add(entry["id"])
        if not delta["source_disabled"]:
            config["disabledPlugins"] = [item for item in config.get("disabledPlugins", []) if item != SOURCE_ID]
    elif other_clones:
        notes.append("Kept the newer active idle clone; the previous idle selection was not restored.")
    # Only our ID is removed from registry bookkeeping. All later unrelated
    # plugin entries, disabled IDs, timings, bar changes, and extensions survive.
    config["disabledPlugins"] = [item for item in config.get("disabledPlugins", []) if item != idle_id]
    if delta.get("own_disabled"):
        config["disabledPlugins"].append(idle_id)
    config["cloneSourceRestores"] = [item for item in config.get("cloneSourceRestores", []) if item != idle_id]
    if delta.get("own_restore"):
        config["cloneSourceRestores"].append(idle_id)
    menu = delta.get("menu", {})
    if menu_id and menu.get("added_entry"):
        # Preserve later user settings on the menu's entry; only the exact
        # entry added by our installer belongs to this registry delta.
        config["plugins"] = [entry for entry in config["plugins"] if entry != {"id": menu_id}]
    if menu_id and menu.get("cleared_disabled") and menu_id not in config["disabledPlugins"]:
        config["disabledPlugins"].append(menu_id)
    for key in ("disabledPlugins", "cloneSourceRestores"):
        if not config[key]:
            config.pop(key)
    return config, notes


def restore_files(home: Path, journal: dict, dry_run=False) -> list[str]:
    notes = []
    for rel, record in journal["files"].items():
        validate_relative(rel)
        path = home / rel
        if (symlink_ancestor(home, rel) is not None or path.is_symlink()
                or (path.exists() and (not path.is_file()
                    or path.stat().st_mode & 0o777 != record.get("mode", 0o644)
                    or not artifact_matches(path, record["installed_sha256"])))):
            notes.append(f"Kept modified file: {path}")
            continue
        if dry_run:
            continue
        if record["original"] is None:
            if path.exists():
                path.unlink()
        else:
            original = record["original"]
            atomic_write(path, base64.b64decode(original["data"]), original["mode"])
    legacy = journal.get("legacy") if journal.get("schema") != 1 else journal
    plugin_ids = legacy_ids(legacy) if legacy else (LEGACY_IDLE_ID, LEGACY_MENU_ID)
    directories = [f".config/omarchy/plugins/{plugin_id}" for plugin_id in plugin_ids if plugin_id]
    for rel in directories + [f".config/omarchy/themes/{THEME_SLUG}"]:
        owned_dir = home / rel
        if (owned_dir.exists() and not owned_dir.is_symlink()
                and symlink_ancestor(home, rel) is None and not dry_run):
            for directory in sorted((path for path in owned_dir.rglob("*")
                                     if path.is_dir() and not path.is_symlink()), reverse=True):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            try:
                owned_dir.rmdir()
            except OSError:
                pass
    return notes
