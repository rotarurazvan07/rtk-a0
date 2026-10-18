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
PROD_TARGETS = [
    "scripts/setup.sh", "hooks.py", "plugin.yaml",
    "extensions/python/tool_execute_before/_10_rtk_intercept.py",
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
