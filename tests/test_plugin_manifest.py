from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
import yaml

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = PLUGIN_ROOT / "plugin.yaml"

# Real required manifest fields per /a0/helpers/plugins.py:77-85 (PluginMetadata).
# author/license/repo are NOT manifest fields (PluginListItem display attrs, :91-113).
REQUIRED_STR_FIELDS = ("name", "title", "description", "version")
BOOL_FLAGS = ("per_project_config", "per_agent_config")
NAME_RE = re.compile(r"^[a-z0-9_]+$")


def _load_manifest() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


def test_no_default_config_required():
    """activation needs no config file — the loader reads default_config.yaml only when present (helpers/plugins.py:59,:673-688); MVP ships none."""
    assert not (PLUGIN_ROOT / "default_config.yaml").exists(), (
        "no default_config.yaml — compression must activate from the manifest alone"
    )


def test_extension_registered_unconditionally():
    """the extension file exists at the named-extension path — registration is unconditional on module load; no toggle/gate file is needed for it to run."""
    ext = PLUGIN_ROOT / "extensions" / "python" / "tool_execute_before" / "_10_rtk_intercept.py"
    assert ext.is_file(), "tool_execute_before extension must ship in the plugin"
    src = ext.read_text(encoding="utf-8")
    assert "class" in src and "Extension" in src, (
        "extension must subclass/register as an Extension"
    )


# schema + honesty

def test_required_manifest_fields_present_and_typed():
    """name/title/description/version present as non-empty strings — the installer hard-fails on a missing name (install.py:31-34) and model-validates the rest (helpers/plugins.py:77-85,:291)."""
    manifest = _load_manifest()
    for field in REQUIRED_STR_FIELDS:
        assert isinstance(manifest.get(field), str) and manifest[field].strip(), (
            f"required field {field!r} missing/empty in plugin.yaml"
        )
    for flag in BOOL_FLAGS:
        if flag in manifest:
            assert isinstance(manifest[flag], bool), f"{flag!r} must be boolean"


def test_name_matches_hub_pattern():
    """name matches ^[a-z0-9_]+$ and equals the install directory name (installer enforces name == directory — install.py:48-50)."""
    manifest = _load_manifest()
    name = manifest.get("name", "")
    assert NAME_RE.match(name), f"name {name!r} must match ^[a-z0-9_]+$"
    assert name == "rtk_a0", "name must be rtk_a0"
    # The checkout directory follows the repo slug, which is hyphenated
    # (CI: .../rtk-a0); the installer contract is name == directory modulo
    # that separator, so normalise before comparing.
    assert PLUGIN_ROOT.name.replace("-", "_") == "rtk_a0", (
        f"plugin dir {PLUGIN_ROOT.name!r} must be rtk_a0 modulo -/_"
    )


def test_description_states_install_and_uninstall_behavior():
    """The description honestly states that install auto-downloads the pinned RTK binary and that uninstall removes only plugin files, leaving the binary user-owned."""
    desc = _load_manifest().get("description", "").lower()
    assert "rtk" in desc, "description must name RTK"
    assert re.search(r"(download|install)", desc) and "binary" in desc, (
        "description must state that install downloads the RTK binary"
    )
    assert re.search(r"uninstall", desc) or re.search(r"leav", desc), (
        "description must state uninstall behavior (binary left user-owned)"
    )


def test_no_mvp_overclaims():
    """No misleading claims: this MVP ships no settings UI, no per-project toggles, and no stats persistence."""
    manifest = _load_manifest()
    desc = manifest.get("description", "").lower()
    assert not manifest.get("settings_sections"), "MVP manifest has no settings_sections"
    assert manifest.get("per_project_config") is False
    assert manifest.get("per_agent_config") is False
    for needle in ("guaranteed", "settings", "per-project", "dashboard", "stats.json"):
        assert needle not in desc, f"description must not claim {needle!r} (unshipped in MVP)"
    assert not (PLUGIN_ROOT / "stats.json").exists(), "no plugin-owned persistence (MVP scope)"


# install-layout simulation (both Hub-zip and git clone run the same
# validate_plugin_dir contract — plugin.yaml at root + name == directory)

def test_git_clone_layout_is_install_ready(tmp_path):
    """a fresh git-clone-shaped tree contains every path the loader and installer require (install.py:43 hard-requires plugin.yaml at root)."""
    shutil.copytree(
        PLUGIN_ROOT,
        tmp_path / "rtk_a0",
        ignore=shutil.ignore_patterns(
            "venv", "usr", "node_modules", "__pycache__", ".pytest_cache",
            ".coverage", ".git", ".a0proj", "_bmad",".serena", "_bmad-output",
        ),
    )
    layout = tmp_path / "rtk_a0"
    required = [
        "plugin.yaml",
        "hooks.py",
        "rtk_pin.txt",
        "LICENSE",
        "scripts/setup.sh",
        "extensions/python/tool_execute_before/_10_rtk_intercept.py",
    ]
    missing = [rel for rel in required if not (layout / rel).is_file()]
    assert not missing, f"install layout missing required paths: {missing}"
    # name == directory (installer dir-match pin)
    manifest = yaml.safe_load((layout / "plugin.yaml").read_text(encoding="utf-8"))
    assert manifest["name"] == layout.name == "rtk_a0"