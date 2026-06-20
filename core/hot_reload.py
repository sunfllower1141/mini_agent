"""
hot_reload.py -- Live code reload for the Electron backend.

Without this, every code change requires restarting the Electron app because
the Python backend process keeps all modules cached in sys.modules.

This module snapshots file modification times at session init and checks
for changes before each agent turn.  When a source file has changed, it
reloads the affected module(s) via importlib.reload().

For tool modules (file_ops.py, shell_ops.py, etc.) this works cleanly
because the @_register decorator overwrites the same keys in _TOOL_DISPATCH.
For core modules, reload is best-effort -- some module-level state may
not transfer to existing instances.  If a core module changes in a way
that requires a full restart, exit the process and Electron will restart it.
"""

from __future__ import annotations

import importlib
import os
import sys
import threading
from typing import Dict, List, Set


# ---------------------------------------------------------------------------
# Module tracking
# ---------------------------------------------------------------------------

# module name -> (path, mtime) recorded at init time
_module_snapshots: Dict[str, tuple[str, float]] = {}
_snapshot_lock = threading.Lock()

# Track which modules we've already reloaded this session to avoid loops
_reloaded_this_session: Set[str] = set()

# The workspace root -- modules outside this are never reloaded
_workspace_root: str = ""

# Entry-point tracking (server.py when launched as __main__).
# This file cannot be hot-reloaded; changes require an Electron restart.
_entry_point_path: str = ""
_entry_point_mtime: float = 0.0

# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

def _workspace_filter(filepath: str) -> bool:
    """Return True if *filepath* is a workspace source file (not a dependency).

    Excludes virtualenvs, site-packages, caches, and VCS directories.
    """
    if not _workspace_root:
        return False
    try:
        fp = os.path.abspath(filepath)
        wr = os.path.abspath(_workspace_root)
        if not fp.startswith(wr):
            return False
        # Exclude venvs, site-packages, caches, and VCS dirs
        _EXCLUDED = {'site-packages', 'venv', '.venv', '__pycache__', '.git',
                      'node_modules', 'dist', 'build', 'egg-info', '.tox',
                      '.mypy_cache', '.pytest_cache', '.ruff_cache'}
        parts = set(os.path.normpath(fp).split(os.sep))
        if parts & _EXCLUDED:
            return False
        # Also exclude test helper scripts at workspace root (e.g., _test_*.py)
        rel = os.path.relpath(fp, wr)
        if os.path.basename(fp).startswith('_test_') and not os.sep in rel.lstrip(os.sep):
            return False
        return True
    except Exception:
        return False


def snapshot_modules(workspace_root: str) -> int:
    """Record mtimes of all loaded modules in *workspace_root*.

    Called once at session init.  Returns the number of modules snapshotted.
    """
    global _workspace_root
    _workspace_root = os.path.abspath(workspace_root)

    count = 0
    with _snapshot_lock:
        _module_snapshots.clear()
        for name, mod in sorted(sys.modules.items()):
            if name in ("__main__", "builtins"):
                continue
            filepath = getattr(mod, "__file__", None)
            if not filepath or not filepath.endswith(".py"):
                continue
            if not _workspace_filter(filepath):
                continue
            try:
                mtime = os.path.getmtime(filepath)
                _module_snapshots[name] = (filepath, mtime)
                count += 1
            except OSError:
                pass

    # Also snapshot __main__.__file__ (server.py) separately -- it's excluded
    # from the main loop above but we need to detect when it changes on disk
    # so we can tell the user to restart.
    _snapshot_entry_point()

    return count


# ---------------------------------------------------------------------------
# Changed-module detection
# ---------------------------------------------------------------------------

def _get_changed_modules() -> List[tuple[str, str]]:
    """Return list of (module_name, filepath) that have changed since snapshot.

    Also picks up *new* .py files in the workspace that weren't in the snapshot.
    """
    changed: List[tuple[str, str]] = []
    seen_paths: Set[str] = set()

    with _snapshot_lock:
        for name, (path, snap_mtime) in _module_snapshots.items():
            try:
                cur_mtime = os.path.getmtime(path)
            except OSError:
                # File deleted or inaccessible -- skip
                continue
            if cur_mtime > snap_mtime:
                changed.append((name, path))

            seen_paths.add(os.path.abspath(path))

    # Also detect new .py files that were added after snapshot.
    # This handles modules imported after session init (lazy imports).
    for name, mod in sorted(sys.modules.items()):
        if name in _module_snapshots:
            continue
        if name in ("__main__", "builtins"):
            continue
        filepath = getattr(mod, "__file__", None)
        if not filepath or not filepath.endswith(".py"):
            continue
        if not _workspace_filter(filepath):
            continue
        abspath = os.path.abspath(filepath)
        if abspath in seen_paths:
            continue
        try:
            cur_mtime = os.path.getmtime(filepath)
        except OSError:
            continue
        # Check if this path was in the snapshot under a different name
        with _snapshot_lock:
            snap_entry = _module_snapshots.get(name)
        if snap_entry is None:
            # New module -- add to snapshot and mark as changed
            with _snapshot_lock:
                _module_snapshots[name] = (filepath, cur_mtime)
            changed.append((name, filepath))
            seen_paths.add(abspath)

    return changed


# ---------------------------------------------------------------------------
# Dependency-aware reload
# ---------------------------------------------------------------------------

def _reload_order(modules: List[str]) -> List[str]:
    """Sort *modules* so dependents are reloaded AFTER their dependencies.

    Simple topological: modules with fewer submodules (closer to leaf) reload first.
    For the workspace, tool modules (tools.X) should reload before tools.__init__.
    """
    def _depth(name: str) -> int:
        return name.count(".")

    # Also: tools/__init__.py imports from tools.file_ops etc., so reload
    # leaf modules first, then the package __init__ last.
    return sorted(modules, key=_depth, reverse=True)


def reload_changed_modules() -> List[str]:
    """Reload workspace modules whose source files changed since snapshot.

    Returns list of module names that were reloaded.  Empty list if nothing changed.
    Safe to call from any thread; only reloads each module once per change cycle.
    """
    global _workspace_root, _module_snapshots, _reloaded_this_session

    changed = _get_changed_modules()
    if not changed:
        return []

    # Update snapshot mtimes BEFORE reloading, so if reload triggers
    # another check we won't loop.
    now = time_module()
    with _snapshot_lock:
        for name, path in changed:
            try:
                mtime = os.path.getmtime(path)
                _module_snapshots[name] = (path, mtime)
            except OSError:
                pass

    module_names = [name for name, _ in changed]
    ordered = _reload_order(module_names)

    # Save critical state before reloading, in case we reload ourselves.
    _saved_root = _workspace_root
    _saved_snapshots = dict(_module_snapshots)
    _saved_reloaded = set(_reloaded_this_session)

    reloaded: List[str] = []
    errors: List[str] = []

    for name in ordered:
        if name in _reloaded_this_session:
            continue
        mod = sys.modules.get(name)
        if mod is None:
            continue
        try:
            importlib.reload(mod)
            _reloaded_this_session.add(name)
            reloaded.append(name)
        except Exception as e:
            errors.append(f"{name}: {e}")

    # Restore state if we just reloaded ourselves and globals were wiped.
    if not _workspace_root and _saved_root:
        _workspace_root = _saved_root  # type: ignore[assignment]
    if not _module_snapshots and _saved_snapshots:
        _module_snapshots.update(_saved_snapshots)
    if not _reloaded_this_session and _saved_reloaded:
        _reloaded_this_session.update(_saved_reloaded)

    if reloaded:
        _log_reload(reloaded)
    if errors:
        _log_errors(errors)

    return reloaded


# ---------------------------------------------------------------------------
# Restart advisory
# ---------------------------------------------------------------------------

def get_restart_advisory() -> list[str]:
    """Return advisory messages for files that changed but cannot be hot-reloaded.

    Specifically checks the entry-point script (server.py).  When it changes,
    the user must restart the Electron app for changes to take effect.

    Returns an empty list if no restart is needed.
    """
    global _entry_point_path, _entry_point_mtime

    advisories: list[str] = []

    if not _entry_point_path:
        return advisories

    try:
        cur_mtime = os.path.getmtime(_entry_point_path)
    except OSError:
        return advisories

    if cur_mtime > _entry_point_mtime:
        _entry_point_mtime = cur_mtime
        rel = os.path.relpath(_entry_point_path, _workspace_root) if _workspace_root else _entry_point_path
        advisories.append(
            f"⚠️  {rel} changed on disk. This file cannot be hot-reloaded — "
            f"please restart the Electron app for changes to take effect."
        )

    return advisories


def _snapshot_entry_point() -> None:
    """Record the entry-point .py file and its mtime."""
    global _entry_point_path, _entry_point_mtime

    main_mod = sys.modules.get("__main__")
    if main_mod is None:
        return

    filepath = getattr(main_mod, "__file__", None)
    if not filepath or not filepath.endswith(".py"):
        return

    if not _workspace_filter(filepath):
        return

    try:
        _entry_point_path = os.path.abspath(filepath)
        _entry_point_mtime = os.path.getmtime(_entry_point_path)
    except OSError:
        pass


def _log_reload(reloaded: List[str]) -> None:
    """Log to stderr so it's visible in Electron's backend console."""
    names = ", ".join(reloaded)
    print(f"[hot_reload] Reloaded {len(reloaded)} module(s): {names}",
          file=sys.stderr, flush=True)


def _log_errors(errors: List[str]) -> None:
    for err in errors:
        print(f"[hot_reload] ERROR reloading {err}",
              file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Time helper (mockable in tests)
# ---------------------------------------------------------------------------

def time_module():
    """Return current time.  Imported lazily to avoid circular deps."""
    import time
    return time.time()


# ---------------------------------------------------------------------------
# Force reload of a specific file path (called after write_file / edit_file)
# ---------------------------------------------------------------------------

def reload_by_path(filepath: str) -> bool:
    """Reload the module(s) corresponding to *filepath*.

    Called by write_file / edit_file after a successful write so the
    in-process cache is updated immediately, without waiting for the
    next turn's auto-check.

    Returns True if at least one module was reloaded.
    """
    abspath = os.path.abspath(filepath)
    global _workspace_root, _module_snapshots, _reloaded_this_session

    if not _workspace_filter(abspath):
        return False

    to_reload: List[str] = []
    with _snapshot_lock:
        for name, (path, _) in _module_snapshots.items():
            if os.path.abspath(path) == abspath:
                to_reload.append(name)

    if not to_reload:
        # Not in snapshot yet -- search sys.modules for already-imported
        # modules whose __file__ matches this path (handles lazy imports
        # that happened after snapshot_modules() ran).
        try:
            new_mtime = os.path.getmtime(abspath)
        except OSError:
            return False
        for name, mod in sys.modules.items():
            filepath_mod = getattr(mod, "__file__", None)
            if filepath_mod and os.path.abspath(filepath_mod) == abspath:
                with _snapshot_lock:
                    _module_snapshots[name] = (abspath, new_mtime)
                to_reload.append(name)
        if not to_reload:
            # Truly a new file not yet imported at all.
            return False

    # Update mtime for modules already in snapshot
    if not to_reload:
        return False
    try:
        new_mtime = os.path.getmtime(abspath)
    except OSError:
        return False

    with _snapshot_lock:
        for name in to_reload:
            _module_snapshots[name] = (abspath, new_mtime)



    # Reload in dependency order
    # Save critical state before reloading, in case we reload ourselves.
    _saved_root = _workspace_root
    _saved_snapshots = dict(_module_snapshots)
    _saved_reloaded = set(_reloaded_this_session)

    ordered = _reload_order(to_reload)
    reloaded = []
    for name in ordered:
        mod = sys.modules.get(name)
        if mod is None:
            continue
        try:
            importlib.reload(mod)
            _reloaded_this_session.add(name)
            reloaded.append(name)
        except Exception as e:
            print(f"[hot_reload] ERROR reloading {name} after write: {e}",
                  file=sys.stderr, flush=True)

    # Restore state if we just reloaded ourselves and globals were wiped.
    if not _workspace_root and _saved_root:
        # We were reloaded; restore critical state.
        _workspace_root = _saved_root  # type: ignore[assignment]
    if not _module_snapshots and _saved_snapshots:
        _module_snapshots.update(_saved_snapshots)
    if not _reloaded_this_session and _saved_reloaded:
        _reloaded_this_session.update(_saved_reloaded)

    if reloaded:
        _log_reload(reloaded)
    # If the edited path is the entry point, warn immediately.
    if abspath == _entry_point_path:
        try:
            _entry_point_mtime = os.path.getmtime(abspath)
        except OSError:
            pass
        rel = os.path.relpath(abspath, _workspace_root) if _workspace_root else abspath
        print(f"[hot_reload] ⚠️  {rel} edited — restart required for changes to take effect",
              file=sys.stderr, flush=True)

    return bool(reloaded)
