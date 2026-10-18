#!/usr/bin/env python3
"""framework E2E: intercept chain with the REAL rtk binary. Framework runtime (needs helpers.extension) — run under /opt/venv-a0 with PYTHONPATH=/a0. HOST-STATE ISOLATED: PATH is built explicitly per test, so the suite behaves identically with or without a real rtk installed on the container."""
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.environ.get("A0_FRAMEWORK_ROOT", "/a0"))

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
EXT_DIR = PLUGIN_ROOT / "extensions" / "python" / "tool_execute_before"
EXT_PATH = EXT_DIR / "_10_rtk_intercept.py"
PIN = (PLUGIN_ROOT / "rtk_pin.txt").read_text().strip()
VERSION = PIN[1:] if PIN.startswith("v") else PIN


def _extension():
    spec = importlib.util.spec_from_file_location("_10_rtk_intercept_e2e", EXT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_real_delegation_compresses(tmp_path):
    """+ ( inversion): with the REAL rtk resolvable, the gate delegates and mutates tool_args['code'] when rtk returns a usable rewrite under BOTH rewrite-available codes: rc 3 = default-container behavior ('rewrite printed, allow rules do not cover every command' — previously OBSERVED as a drop, now ACCEPTED per ) and rc 0 with RTK_REWRITE_HOST=openclaw (rtk's documented delegate mode). Mutation in both modes is the rc-3-accept pin."""
    ext = _extension()
    bin_dir = tmp_path / "bins"
    bin_dir.mkdir()
    real = shutil.copy2("/usr/local/bin/rtk", bin_dir / "rtk") if Path("/usr/local/bin/rtk").exists() else None
    if real is None:
        pytest.skip("real rtk not present on this container")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PATH", str(bin_dir))
        mp.delenv("RTK_REWRITE_HOST", raising=False)
        agent = type("A", (), {"data": {}})()
        args = {"runtime": "terminal", "code": "git status"}
        ext.RtkIntercept(agent).execute("code_execution_tool", args)
        # rc 3 on default container -> rewrite now applied
        assert args["code"] == "rtk git status"
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PATH", str(bin_dir))
        mp.setenv("RTK_REWRITE_HOST", "openclaw")
        agent = type("A", (), {"data": {}})()
        args = {"runtime": "terminal", "code": "git status"}
        ext.RtkIntercept(agent).execute("code_execution_tool", args)
        # rc 0 delegate mode -> rewrite applied
        assert args["code"] == "rtk git status"


def test_real_bypass_escape_hatch_passes_through(tmp_path):
    """ `rtk bypass <cmd>` matches the `rtk ` idempotence gate and reaches the executor unchanged — even with the real binary on a controlled PATH. The plugin never strips or rewrites the prefix; RTK itself runs <cmd> uncompressed and keeps it in its own savings accounting (, RTK-internal). Host-state isolated: skip when the real binary is absent."""
    ext = _extension()
    bin_dir = tmp_path / "bins"
    bin_dir.mkdir()
    if not Path("/usr/local/bin/rtk").exists():
        pytest.skip("real rtk not present on this container")
    shutil.copy2("/usr/local/bin/rtk", bin_dir / "rtk")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PATH", str(bin_dir))
        agent = type("A", (), {"data": {}})()
        args = {"runtime": "terminal", "code": "rtk bypass git status"}
        ext.RtkIntercept(agent).execute("code_execution_tool", args)
        assert args["code"] == "rtk bypass git status"


def test_real_fail_open_without_rtk(tmp_path):
    """fail-open with a real availability check (no rtk on the controlled PATH): command passes through untouched, no subprocess attempted by the extension."""
    ext = _extension()
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PATH", str(empty))
        agent = type("A", (), {"data": {}})()
        args = {"runtime": "terminal", "code": "ls -la"}
        assert shutil.which("rtk", path=str(empty)) is None
        ext.RtkIntercept(agent).execute("code_execution_tool", args)
        assert args["code"] == "ls -la"


def test_real_idempotence_prefix(tmp_path):
    """`rtk `-prefixed commands pass through untouched even with the real binary present (no delegation — no rtk rewrite of rtk)."""
    ext = _extension()
    bin_dir = tmp_path / "bins"
    bin_dir.mkdir()
    if Path("/usr/local/bin/rtk").exists():
        shutil.copy2("/usr/local/bin/rtk", bin_dir / "rtk")
    else:
        pytest.skip("real rtk not present on this container")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PATH", str(bin_dir))
        agent = type("A", (), {"data": {}})()
        args = {"runtime": "terminal", "code": "rtk git status"}
        ext.RtkIntercept(agent).execute("code_execution_tool", args)
        assert args["code"] == "rtk git status"


def test_real_rtk_gain_passes_through(tmp_path):
    """rtk gain matches the idempotence gate and reaches the executor unchanged with the real binary on a controlled PATH; running it returns the RTK savings report into the conversation. The plugin never invokes rtk gain itself - it proves the passthrough only. Skips when the real binary is absent."""
    ext = _extension()
    bin_dir = tmp_path / "bins"
    bin_dir.mkdir()
    if not Path("/usr/local/bin/rtk").exists():
        pytest.skip("real rtk not present on this container")
    shutil.copy2("/usr/local/bin/rtk", bin_dir / "rtk")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PATH", str(bin_dir))
        agent = type("A", (), {"data": {}})()
        args = {"runtime": "terminal", "code": "rtk gain"}
        # T3: pin the idempotence gate itself, not just the outcome —
        # with rc 3 accepted rtk rewrite echoes `rtk gain`
        # unchanged, so the passthrough assert alone is vacuous. Sentinel is
        # scoped to the extension call only; the dashboard run below uses
        # the real subprocess.run again.
        _real_run = ext.subprocess.run

        def _no_delegation(*a, **k):
            raise AssertionError("rtk-prefixed command must never be delegated to rtk rewrite")

        ext.subprocess.run = _no_delegation
        try:
            ext.RtkIntercept(agent).execute("code_execution_tool", args)
        finally:
            ext.subprocess.run = _real_run
        # The exact command string reaches execution unchanged -
        # identical to running `rtk gain` on the host shell.
        assert args["code"] == "rtk gain", "rtk gain must pass through untouched"
        # The dashboard output returns inside the conversation.
        proc = subprocess.run(
            ["rtk", "gain"], capture_output=True, text=True, encoding="utf-8", timeout=10
        )
        assert proc.returncode == 0, f"rtk gain failed: {proc.stderr}"
        if "Tokens saved" not in proc.stdout:
            # T1: fresh-container/empty-ledger host state — rtk gain
            # prints 'No tracking data yet.' (rc 0); skip, never red.
            pytest.skip("rtk ledger empty on this host (no accumulated savings yet)")
        assert "RTK Token Savings" in proc.stdout, "dashboard header must be present"
        assert "Tokens saved" in proc.stdout, "session savings line must be present"
        assert "By Command" in proc.stdout, "per-command table must be present"


def test_real_rtk_gain_session_reduction_observable(tmp_path):
    """A real session shows a parseable session-token reduction and a per-command table in the rtk gain output. Assert the observable exists and parses; never pin an exact percentage, since the RTK-owned ledger accumulates from all container usage. Host-state isolated."""
    import re

    bin_dir = tmp_path / "bins"
    bin_dir.mkdir()
    if not Path("/usr/local/bin/rtk").exists():
        pytest.skip("real rtk not present on this container")
    shutil.copy2("/usr/local/bin/rtk", bin_dir / "rtk")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PATH", str(bin_dir))
        proc = subprocess.run(
            ["rtk", "gain"], capture_output=True, text=True, encoding="utf-8", timeout=10
        )
    assert proc.returncode == 0, f"rtk gain failed: {proc.stderr}"
    m = re.search(r"Tokens saved:\s*\S+\s*\((\d+(?:\.\d+)?)%\)", proc.stdout)
    if not m:
        # T1/T2: fresh-container/empty-ledger host state — rtk gain
        # prints 'No tracking data yet.' (rc 0). The RTK-owned ledger is
        # container-global and varies with usage; skip, never red.
        pytest.skip("rtk ledger empty on this host (no session reduction observable)")
    pct = float(m.group(1))
    # T2: the >=40% target is asserted only where a session reduction
    # is observable (ledger gate above); fresh hosts skip instead of failing
# on a hardcoded threshold (: ledger state varies).
    assert pct >= 40.0, f"session-token reduction must meet the >=40% target, got {pct}%"
    # Per-command statistics section with at least one row.
    assert "By Command" in proc.stdout and re.search(r"\d+\.\s+\S+", proc.stdout), \
        "per-command reduction statistics must be observable"
