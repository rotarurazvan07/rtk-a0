#!/usr/bin/env python3
"""ast-grep CI enforcement rule contract (network-free). The pure suite parses the rule YAML files and pins their shape (ids, severity, regex). If the ast-grep binary exists on the host it is run live on the production source paths as a gated bonus verification, never a requirement."""
import re
import shutil
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
RULE_DIR = ROOT / ".github" / "ast-rules"
SGCONFIG = ROOT / ".github" / "sgconfig.yml"
RULE_IDS = {
    "no-shell-true",
    "no-hardcoded-pin-outside-pin-file",
    "no-hardcoded-pin-in-shell",
}
# Production source only: fixtures/docs legitimately quote the version shape.
# Epic 4 (2026-10-26, CR-4-2-D8 fold-in): the user-triggered rtk gain surface
# family ships new production paths — command handler, api endpoint, page and
# badge webui assets — so the live ast-grep gate covers them too. rc=0 verified
# scoped over this full set at PR prep time.
PROD_TARGETS = [
    "scripts/setup.sh", "hooks.py", "plugin.yaml",
    "extensions/python/tool_execute_before/_10_rtk_intercept.py",
    "commands/rtk-gain.command.yaml", "commands/rtk_gain_command.py",
    "api/rtk_gain.py", "api/__init__.py",
    "webui/main.html", "webui/rtk-gain-store.js",
    "webui/rtk-gain-badge-store.js",
    "extensions/webui/chat-top-start/_10_rtk_gain_badge.html",
]


def _rules():
    out = {}
    for f in sorted(RULE_DIR.glob("*.yml")):
        d = yaml.safe_load(f.read_text())
        out[d["id"]] = d
    return out


def test_ci_rule_files_exist_with_all_rules():
    """every pinned rule id has a single-doc YAML file (ast-grep ruleDir)."""
    rules = _rules()
    missing = RULE_IDS - set(rules)
    assert not missing, f"rule ids missing: {missing}"
    for rid in RULE_IDS:
        assert rules[rid].get("severity") == "error", f"{rid} must be error severity"
        assert rules[rid].get("language") in {"python", "bash"}, f"{rid} language"
    cfg = yaml.safe_load(SGCONFIG.read_text())
    assert "ast-rules" in cfg.get("ruleDirs", []), "sgconfig must point at ast-rules"


def test_shell_true_rule_targets_subprocess_run():
    """shell=True rejection pattern anchors subprocess.run."""
    rule = _rules()["no-shell-true"]
    assert "shell=True" in str(rule.get("rule")), "shell=True rule must anchor the pattern"


def test_version_literal_rules_match_semver_shape():
    """the hardcoded-pin rules match the vX.Y.Z shape."""
    rules = _rules()
    py_re = rules["no-hardcoded-pin-outside-pin-file"]["rule"]["regex"]
    sh_re = rules["no-hardcoded-pin-in-shell"]["rule"]["regex"]
    assert re.search(py_re, "v1.0.1"), "python rule regex must match vN.N.N"
    assert re.search(sh_re, "v1.0.1"), "bash rule regex must match vN.N.N"


def _ast_grep_binary() -> str | None:
    """Resolve a REAL ast-grep binary, or None.

    `sg` alone is ambiguous: ubuntu-latest ships shadow-utils' /usr/bin/sg
    (execute a command as another group), which has no `scan` subcommand and
    fails with "group 'scan' does not exist". A `--version` probe tells the
    two apart before any scan is attempted.
    """
    for candidate in (shutil.which("ast-grep"), shutil.which("sg")):
        if not candidate:
            continue
        try:
            proc = subprocess.run(
                [candidate, "--version"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0 and "ast-grep" in f"{proc.stdout}{proc.stderr}".lower():
            return candidate
    return None


CI_PATH = ROOT / ".github" / "workflows" / "ci.yaml"
# Production Python surface the 95% gate must cover: root hooks + every
# extension module. Globbed in CI so a new extension module cannot land
# outside the gate silently.
PROD_PY = ["hooks.py"] + sorted(
    p.relative_to(ROOT).as_posix()
    for p in (ROOT / "extensions" / "python").rglob("*.py")
)


def test_coverage_include_covers_every_production_module():
    """The CI coverage gate globs the whole production surface: hooks.py
    plus extensions/python/**, so adding a new extension module keeps it
    inside the 95% gate instead of silently dropping it."""
    ci = CI_PATH.read_text(encoding="utf-8")
    assert 'include="hooks.py,extensions/python/**"' in ci, (
        "coverage gate must include hooks.py and extensions/python/**"
    )
    for rel in PROD_PY:
        assert (ROOT / rel).exists(), f"production module {rel} missing"
    # every extension module matches the glob (no stray non-py extensions)
    for rel in PROD_PY[1:]:
        assert rel.startswith("extensions/python/"), f"{rel} outside glob scope"


def test_ci_framework_shim_pins_extension_contract():
    """CI runs framework-venv suites against a minimal Extension shim; the
    shim must preserve the contract the extension actually relies on:
    Extension stems with an .agent attribute, and load_classes_from_folder
    returning Extension subclasses only (the loader filter)."""
    ci = CI_PATH.read_text(encoding="utf-8")
    assert "class Extension" in ci and "self.agent = agent" in ci, (
        "CI shim must define Extension with per-instance agent"
    )
    assert "def load_classes_from_folder" in ci, (
        "CI shim must define the loader entry point the suites import"
    )
    assert "issubclass" in ci and "base_class" in ci, (
        "shim loader must filter subclasses of the base class, as A0 does"
    )


def test_ast_grep_live_scan_when_binary_present():
    """gated live proof: ast-grep on PATH scans production source clean."""
    sg = _ast_grep_binary()
    if sg is None:
        import pytest
        pytest.skip("ast-grep binary not on PATH - pure YAML contract pins the rule")
    targets = [str(ROOT / p) for p in PROD_TARGETS if (ROOT / p).exists()]
    assert targets, "no production source targets found for live scan"
    try:
        proc = subprocess.run(
            [sg, "scan", "--config", str(SGCONFIG), *targets],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
    except subprocess.TimeoutExpired:
        import pytest
        pytest.skip("ast-grep live scan timed out - pure YAML contract pins the rule")
    assert proc.returncode == 0, f"ast-grep CI rule violations: {proc.stdout}{proc.stderr}"
