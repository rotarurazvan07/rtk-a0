#!/usr/bin/env python3
"""Publishable-surface hygiene: no internal process/criteria references.

The shipped plugin surface (commands, api, webui, extensions, scripts, tests,
README) must not leak internal planning vocabulary: acceptance-criteria ids,
story ids, requirement ids, architecture-decision ids, epic or sprint
identifiers, retrospective/dev-record vocabulary, or workflow-process names.
The rule and its verification grep live in the root AGENTS.md; this pin
enforces it in CI. The rule file itself and this pin are excluded from the
scan (they legitimately name the banned patterns).
"""
import re
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent

# pattern classes that must never appear in publishable surface text
# (re.VERBOSE: comments live on their own lines, never inside the pattern)
BANNED_PATTERN = re.compile(
    r"""
    AC-\d                        # acceptance-criteria ids
    |\b(?:FR|NFR)\d+               # requirement ids
    |ADR-\d+                      # architecture-decision ids
    |\bStory[\s-]\d                # story ids
    |\b(?:CR|DS|ER)[\s-]\d         # review / dev-record / retro ids
    |\bEpic\s+\d                  # epic identifiers
    |\bsprint\b                  # sprint identifiers
    |\bretrospective\b           # retrospective vocabulary
    |\bBMAD\b                     # framework-process names
    |\bmutation-kill\b           # dev-record vocabulary
    |\bescalation\b              # dev-record vocabulary
    |\bD-\d{1,2}\b                # decision/defer ids
    """,
    re.VERBOSE | re.IGNORECASE,
)

SCAN_EXCLUDED_PARTS = (
    ".venv",
    ".git",
    ".serena",
    ".neuralmind",
    ".desloppify",
    ".github",
    ".a0proj",
    "__pycache__",
)
SCAN_DIRS = ("commands", "api", "webui", "extensions", "scripts", "tests")
# CR-4-5 review: root-level publishables are scanned too (the ban
# missed hooks.py/plugin.yaml/rtk_pin.txt/LICENSE before this patch)
ROOT_PUBLISHABLE_FILES = ("hooks.py", "plugin.yaml", "rtk_pin.txt", "LICENSE", "README.md")
# the rule file and this pin itself legitimately name the banned classes
SELF_FILES = {Path(__file__).resolve(), PLUGIN_ROOT / "AGENTS.md"}


def _publishable_files():
    for rel_dir in SCAN_DIRS:
        base = PLUGIN_ROOT / rel_dir
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if set(SCAN_EXCLUDED_PARTS) & set(path.parts):
                continue
            yield path
    for name in ROOT_PUBLISHABLE_FILES:
        candidate = PLUGIN_ROOT / name
        if candidate.is_file():
            yield candidate


def test_publishable_surface_carries_no_internal_references():
    """No publishable file mentions internal process/criteria identifiers."""
    offenders = []
    for path in _publishable_files():
        if path.resolve() in SELF_FILES:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), start=1):
            match = BANNED_PATTERN.search(line)
            if match:
                offenders.append(f"{path.relative_to(PLUGIN_ROOT)}:{lineno}: {match.group(0)!r}")
    assert not offenders, "internal references in publishable surface:\n" + "\n".join(offenders)


def test_ban_pattern_is_live_not_vacuous():
    """The pin itself must bite: a planted internal id must be detected."""
    assert BANNED_PATTERN.search("driven by AC-07 requirements")
    assert BANNED_PATTERN.search("superseded by Story 4.4")
    assert BANNED_PATTERN.search("recorded in ADR-006")
    assert BANNED_PATTERN.search("planned in sprint 3")
    assert BANNED_PATTERN.search("decided at the Epic 4 review")
    assert not BANNED_PATTERN.search("plain engineering prose with no ids")


if __name__ == "__main__":  # copy-pasteable local verification
    raise SystemExit(pytest.main([__file__, "-q"]))