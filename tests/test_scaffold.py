#!/usr/bin/env python3
"""Pure-runtime scaffold tests (manifest, pin, compliance). No framework imports."""
import os
import re
import yaml
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def test_manifest_exists():
    """plugin.yaml exists at root."""
    assert (PLUGIN_ROOT / "plugin.yaml").exists(), "plugin.yaml missing"


def test_manifest_pinned_fields():
    """manifest contains exact pinned fields."""
    manifest_path = PLUGIN_ROOT / "plugin.yaml"
    with open(manifest_path) as f:
        data = yaml.safe_load(f)

    assert data["name"] == "rtk_a0", f"name mismatch: {data.get('name')}"
    assert re.match(r'^[a-z0-9_]+$', data["name"]), "name must match ^[a-z0-9_]+$"
    assert "title" in data and data["title"], "title missing"
    assert "description" in data and data["description"], "description missing"
    assert data["version"] == "1.0.0", f"version mismatch: {data.get('version')}"
    # supersedes the off-by-default assumption: requires
    # zero-config default-on once setup auto-provisions the binary (hooks.install
    # runs scripts/setup.sh on plugin install —).
    assert data.get("per_project_config") is False, "per_project_config must be false"
    assert data.get("per_agent_config") is False, "per_agent_config must be false"
    assert "settings_sections" not in data, "settings_sections must be absent (MVP)"


def test_pin_file():
    """rtk_pin.txt contains exactly v1.0.1."""
    pin_path = PLUGIN_ROOT / "rtk_pin.txt"
    assert pin_path.exists(), "rtk_pin.txt missing"
    content = pin_path.read_text().strip()
    assert content == "v1.0.1", f"pin content mismatch: '{content}'"
    # Ensure single line, no extra bytes
    lines = pin_path.read_text().splitlines()
    assert len(lines) == 1, "pin file must be single line"


def test_license_apache2():
    """LICENSE is Apache-2.0 (not MIT)."""
    license_path = PLUGIN_ROOT / "LICENSE"
    assert license_path.exists(), "LICENSE missing"
    content = license_path.read_text()
    assert "Apache License" in content or "Apache-2.0" in content, "LICENSE must be Apache-2.0"
    assert "MIT License" not in content, "LICENSE must not be MIT"


def test_no_rtk_source_or_binaries():
    """zero RTK source or binaries in repo."""
    # Walk plugin root, ensure no rtk binary blobs or source
    for root, dirs, files in os.walk(PLUGIN_ROOT):
        # Skip .git, __pycache__, .a0proj, .serena
        rel = Path(root).relative_to(PLUGIN_ROOT)
        if any(part in {'.git', '__pycache__', '.a0proj', '.serena'} for part in rel.parts):
            continue
        for f in files:
            if f.lower().startswith('rtk') and f.endswith(('.bin', '.exe', '.so', '.dll', '.tar.gz', '.zip')):
                raise AssertionError(f"RTK binary found: {rel/f}")
            # Also check for RTK source patterns
            if f.endswith(('.rs', '.go', '.c', '.h')) and 'rtk' in f.lower():
                raise AssertionError(f"RTK source file found: {rel/f}")


def test_hooks_stub_exists():
    """hooks.py stub exists."""
    assert (PLUGIN_ROOT / "hooks.py").exists(), "hooks.py missing"


def test_setup_sh_placeholder():
    """scripts/setup.sh placeholder exists."""
    assert (PLUGIN_ROOT / "scripts" / "setup.sh").exists(), "scripts/setup.sh missing"


def test_gitignore_excludes_workspace():
    """gitignore excludes workspace metadata."""
    gitignore = PLUGIN_ROOT / ".gitignore"
    assert gitignore.exists(), ".gitignore missing"
    content = gitignore.read_text()
    for pattern in [".a0proj", ".serena", "__pycache__"]:
        assert pattern in content, f".gitignore missing {pattern}"