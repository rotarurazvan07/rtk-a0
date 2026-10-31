from __future__ import annotations

import sys
import threading
from pathlib import Path

from helpers.extension import Extension

_startup_migration_thread: threading.Thread | None = None
_SPAWN_LOCK = threading.Lock()
_THREAD_NAME = "a0-rtk-boot-reprovisioning"


def _warn(message: str) -> None:
    """Best-effort operator warning; logging must never break startup."""
    try:
        print("rtk boot: " + message, file=sys.stderr)
    except Exception:
        pass


def _resolve_plugin_dir() -> Path | None:
    """Walk up from this module until the directory holding plugin.yaml +
    hooks.py; return None when not found (never guesses a directory name)."""
    current = Path(__file__).resolve().parent
    while True:
        if (current / "plugin.yaml").is_file() and (current / "hooks.py").is_file():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def _reprovision_boot() -> None:
    """Background boot task: resolve this plugin by its directory name and
    let the framework run the plugin's provisioning entrypoint. Never raises."""
    try:
        from helpers.plugins import call_plugin_hook
    except Exception:
        _warn("framework hook dispatch unavailable; skipping re-provisioning")
        return
    try:
        plugin_dir = _resolve_plugin_dir()
    except Exception as exc:
        # Filesystem faults (permission changes, races with a plugin delete)
        # stay inside the background target; boot never propagates them.
        _warn("plugin root resolution failed (" + type(exc).__name__ + ")")
        return
    if plugin_dir is None:
        _warn("plugin root not found; skipping re-provisioning")
        return
    plugin_name = plugin_dir.name
    try:
        call_plugin_hook(plugin_name, "ensure_provisioned", raise_on_error=False)
    except Exception as exc:
        _warn("re-provisioning call failed (" + type(exc).__name__ + ")")


class RtkBootReprovisioningMigration(Extension):
    def execute(self, **kwargs):
        global _startup_migration_thread
        with _SPAWN_LOCK:
            # Check+assign+start under one lock: rapid reloads can never
            # spawn a second live reprovisioning thread.
            if _startup_migration_thread is not None and _startup_migration_thread.is_alive():
                return
            _startup_migration_thread = threading.Thread(
                target=_reprovision_boot,
                name=_THREAD_NAME,
                daemon=True,
            )
            _startup_migration_thread.start()
