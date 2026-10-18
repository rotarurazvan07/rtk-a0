#!/usr/bin/env python3
"""Story 2.4 tests: plugin lifecycle wiring via hooks.py (install() warns-not-fails).

Pure runtime — no framework imports; hooks.py is loaded by path the same way
helpers/plugins.py:918-921 imports it during plugin lifecycle dispatch.
Network-free by design: subprocess.run is mocked; setup.sh is never executed.
"""
import importlib.util
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
HOOKS_PATH = PLUGIN_ROOT / "hooks.py"


def _load_hooks():
    """Import the plugin hooks module by file path (A0 lifecycle-style load)."""
    spec = importlib.util.spec_from_file_location("rtk_a0_hooks", HOOKS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def hooks():
    return _load_hooks()


class _Rec:
    """Records PrintStyle-style calls made by hooks under test."""

    def __init__(self):
        self.calls = []

    def _add(self, level, *args, **kwargs):
        self.calls.append((level, " ".join(str(a) for a in args)))

    def info(self, *args, **kwargs):
        self._add("info", *args, **kwargs)

    def warning(self, *args, **kwargs):
        self._add("warning", *args, **kwargs)

    def error(self, *args, **kwargs):
        self._add("error", *args, **kwargs)

    @property
    def texts(self):
        return [t for _, t in self.calls]

    def levels(self, level):
        return [t for lv, t in self.calls if lv == level]


class _Result:
    def __init__(self, rc, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


# ---------------------------------------------------------------------------
# Task 1 — real A0 lifecycle contract pins
# ---------------------------------------------------------------------------

def test_lifecycle_hooks_are_zero_arg_callables(hooks, monkeypatch):
    """Task 1: call_plugin_hook (helpers/plugins.py:929) dispatches by getattr
    with NO arguments (install.py:298-302; plugins.py:414) — every lifecycle
    hook must be callable with zero args."""
    # P6 (CR 2.4): stub the setup invocation so the zero-arg probe never
    # executes the real setup.sh (network-free contract; no host mutation).
    monkeypatch.setattr(
        hooks.subprocess,
        "run",
        lambda *a, **k: _Result(0, "stub", ""),
    )
    for name in ("install", "pre_update", "uninstall"):
        fn = getattr(hooks, name, None)
        assert callable(fn), f"hooks.{name} missing or not callable"
        fn()  # must not raise


def test_hooks_never_raise_on_setup_exception(hooks, monkeypatch):
    """Task 1 / AC-03: safe_call (helpers/functions.py:3-32) does NOT catch
    exceptions and the installer re-raises (install.py:246-250) — hooks MUST
    self-guard. Missing script path must not propagate."""
    monkeypatch.setattr(hooks, "_SETUP_SCRIPT", hooks._SETUP_SCRIPT / "missing.sh")

    def _raise_missing(argv, **kwargs):
        raise FileNotFoundError("setup script missing")

    monkeypatch.setattr(hooks.subprocess, "run", _raise_missing)
    rec = _Rec()
    monkeypatch.setattr(hooks, "_style", rec)
    hooks.install()
    assert any("could not run" in t for t in rec.texts), rec.texts


# ---------------------------------------------------------------------------
# Task 2 — install(): invocation, exit-code mapping, output surfacing
# ---------------------------------------------------------------------------

def _patch_run(hooks, monkeypatch, result=None, exc=None, recorder=None):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if recorder is not None:
            recorder(hooks, argv, kwargs)
        if exc is not None:
            raise exc
        return result

    monkeypatch.setattr(hooks.subprocess, "run", fake_run)
    return calls


def test_install_invokes_setup_sh_by_argv_without_shell(hooks, monkeypatch):
    """Task 2 / D2: argv-list invocation of scripts/setup.sh, never shell=True."""
    calls = _patch_run(hooks, monkeypatch, result=_Result(0, "ok", ""))
    hooks.install()
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert isinstance(argv, list) and argv[0] == "bash"
    assert argv[1] == str(hooks._SETUP_SCRIPT)
    assert kwargs.get("shell") in (None, False)


@pytest.mark.parametrize("rc", [0, 2, 1, 3, 127])
def test_install_returns_without_raising_for_every_exit_class(hooks, monkeypatch, rc):
    """Task 2 / AC-01: setup.sh exit contract (setup.sh:20-21) — 0 provisioned-or-warned,
    2 usage, other integrity — ALL classes complete the hook without raising."""
    _patch_run(hooks, monkeypatch, result=_Result(rc, f"out{rc}", f"err{rc}"))
    assert hooks.install() is None


def test_install_surfaces_setup_output_on_success(hooks, monkeypatch):
    """Task 2 / AC-01: setup output is surfaced to the operator (epics: 'surfaces its output')."""
    rec = _Rec()
    monkeypatch.setattr(hooks, "_style", rec)
    _patch_run(hooks, monkeypatch, result=_Result(0, "rtk setup: installed rtk v1.0.1", ""))
    hooks.install()
    assert any("installed rtk" in t for t in rec.texts), rec.texts


def test_install_logs_warning_for_usage_exit(hooks, monkeypatch):
    """Task 2: exit 2 (usage error, setup.sh:91-93) is surfaced as a warning."""
    rec = _Rec()
    monkeypatch.setattr(hooks, "_style", rec)
    _patch_run(hooks, monkeypatch, result=_Result(2, "", "rtk setup: unknown argument"))
    hooks.install()
    assert any("unknown argument" in t for t in rec.texts)


def test_install_logs_warning_for_integrity_exit(hooks, monkeypatch):
    """Task 2: non-zero non-2 exit (integrity/format corruption) warns and returns."""
    rec = _Rec()
    monkeypatch.setattr(hooks, "_style", rec)
    _patch_run(hooks, monkeypatch, result=_Result(1, "", "rtk setup: malformed pin"))
    hooks.install()
    assert any("malformed pin" in t for t in rec.texts)


def test_install_surfaces_warning_output_on_exit_zero_warn_path(hooks, monkeypatch):
    """Task 2 / AC-02 adjacency: warned-unavailable runs also exit 0 with stderr text
    (setup.sh rtk_unavailable, Story 2.3) — the skip/warn messages must surface."""
    rec = _Rec()
    monkeypatch.setattr(hooks, "_style", rec)
    _patch_run(
        hooks,
        monkeypatch,
        result=_Result(0, "rtk setup: rtk 1.0.1 already on PATH — skipping", ""),
    )
    hooks.install()
    assert any("already on PATH" in t for t in rec.texts), rec.texts


@pytest.mark.parametrize("exc", [FileNotFoundError("no bash"), OSError("boom"), subprocess.TimeoutExpired("bash", 120)])
def test_install_swallows_exceptions_with_warning(hooks, monkeypatch, exc):
    """Task 2 / AC-03: any subprocess-layer exception warns and returns."""
    rec = _Rec()
    monkeypatch.setattr(hooks, "_style", rec)
    _patch_run(hooks, monkeypatch, exc=exc)
    assert hooks.install() is None
    assert rec.levels("warning") or rec.levels("error"), "expected a warning/error log"


def test_install_exception_path_logs_reason(hooks, monkeypatch):
    """AC-03: the warning must carry the failure reason for the operator."""
    rec = _Rec()
    monkeypatch.setattr(hooks, "_style", rec)
    _patch_run(hooks, monkeypatch, exc=OSError("disk-on-fire"))
    hooks.install()
    assert any("disk-on-fire" in t for t in rec.texts), rec.texts


# ---------------------------------------------------------------------------
# Task 2 — CR-2.3-D2: subprocess PATH must include the install dir
# ---------------------------------------------------------------------------

def test_setup_env_appends_install_dir_when_missing(hooks, monkeypatch):
    """CR-2.3-D2: when caller PATH lacks /usr/local/bin, the setup subprocess PATH
    gains it so setup.sh's PATH-resolved self-check (setup.sh:217-219) resolves
    the freshly installed binary."""
    monkeypatch.setattr(hooks.os, "environ", {"PATH": "/opt/venv/bin"})
    env = hooks._setup_env()
    assert hooks.INSTALL_DIR in env["PATH"].split(os.pathsep)


def test_setup_env_preserves_path_when_install_dir_present(hooks, monkeypatch):
    """CR-2.3-D2: an existing /usr/local/bin entry is not duplicated."""
    monkeypatch.setattr(
        hooks.os, "environ", {"PATH": os.pathsep.join(["/usr/local/bin", "/opt/venv/bin"])}
    )
    env = hooks._setup_env()
    assert env["PATH"].count(hooks.INSTALL_DIR) == 1
    assert env["PATH"].split(os.pathsep)[0] == "/usr/local/bin"


def test_install_passes_env_with_install_dir_to_subprocess(hooks, monkeypatch):
    """CR-2.3-D2 end-to-end: the env handed to subprocess.run contains INSTALL_DIR."""
    seen = {}

    def recorder(mod, argv, kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(hooks.os, "environ", {"PATH": "/opt/venv/bin"})
    _patch_run(hooks, monkeypatch, result=_Result(0, "", ""), recorder=recorder)
    hooks.install()
    assert hooks.INSTALL_DIR in seen.get("env", {}).get("PATH", "").split(os.pathsep)


# ---------------------------------------------------------------------------
# Task 3 — concurrent setup invocations are serialized
# ---------------------------------------------------------------------------

def test_setup_calls_are_serialized_across_threads(hooks, monkeypatch):
    """Task 3 / CR-2.1-D2: concurrent install() callers must not interleave inside
    setup.sh's fixed staging path — the module lock serializes them."""
    order = []
    lock = threading.Lock()
    active = {"n": 0}

    def slow(mod, argv, kwargs):
        with lock:
            active["n"] += 1
            assert active["n"] == 1, "setup invocations overlapped"
            order.append("enter")
        import time as _t

        _t.sleep(0.05)
        with lock:
            active["n"] -= 1
            order.append("exit")
        return _Result(0, "", "")

    _patch_run(hooks, monkeypatch, result=_Result(0, "", ""), recorder=slow)
    threads = [threading.Thread(target=hooks.install) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert order == ["enter", "exit", "enter", "exit"]


def test_module_holds_setup_lock(hooks):
    """Task 3 structural pin: a lock guards the setup invocation."""
    lock = getattr(hooks, "_SETUP_LOCK", None)
    assert lock is not None and hasattr(lock, "acquire") and hasattr(lock, "release")


# ---------------------------------------------------------------------------
# Task 4 — stub safety (Story 3.4 owns full pre_update/uninstall)
# ---------------------------------------------------------------------------

def test_pre_update_and_uninstall_never_raise(hooks, monkeypatch):
    """Story 3.4 SUPERSESSION of the Story-2.4 stub pin (was
    test_pre_update_and_uninstall_are_safe_noops): the hooks are real now
    (FR23/FR24). Never-raise stays; the no-op/source-ban assertions are
    narrowed to FR23 invariant - no deletion primitive may target the
    user-owned binary (INSTALL_DIR). Host isolation: subprocess mocked so
    the probe/setup never touch this host (CR 2.4 P6)."""
    calls = _patch_version_then_setup(
        hooks, monkeypatch, probe=_probe_result("rtk 1.0.1"), setup=_Result(0, "", "")
    )
    monkeypatch.setattr(hooks, "_style", _Rec())
    assert hooks.pre_update() is None
    assert hooks.uninstall() is None
    code = "\n".join(
        ln for ln in HOOKS_PATH.read_text(encoding="utf-8").splitlines()
        if not ln.lstrip().startswith("#")
    )
    for banned in ("shutil.rmtree", "os.remove", "os.unlink", "unlink("):
        assert banned not in code, f"FR23: no file removal in hooks: found {banned}"


def test_hooks_source_pins(hooks):
    """Task 5 structural pins: no shell=True, no hardcoded pin (NFR12 single-source)."""
    code = "\n".join(
        ln for ln in HOOKS_PATH.read_text(encoding="utf-8").splitlines()
        if not ln.lstrip().startswith("#")
    )
    assert "shell=True" not in code
    assert "v1.0.1" not in code, "pin must stay single-source in rtk_pin.txt (NFR12)"
    assert "/usr/local/bin" in code, "install dir constant required (D4/FR16)"


def test_setup_script_referenced_exists(hooks):
    """Task 2: the hooked script path resolves inside the plugin."""
    assert hooks._SETUP_SCRIPT == PLUGIN_ROOT / "scripts" / "setup.sh"
    assert hooks._SETUP_SCRIPT.is_file()



# ---------------------------------------------------------------------------
# CR 2.4 regression tests — defects found by the review layers
# ---------------------------------------------------------------------------

def test_install_survives_unicode_decode_error(hooks, monkeypatch):
    """CR 2.4 P1 (blind HIGH + edge HIGH, PROBE-verified): text=True decodes
    strictly in the interpreter locale; a non-ASCII byte from setup.sh raises
    UnicodeDecodeError (a ValueError, NOT an OSError) — it must not escape
    install() and fail the plugin lifecycle (NFR6/D10)."""
    rec = _Rec()
    monkeypatch.setattr(hooks, "_style", rec)

    def _raise_decode(argv, **kwargs):
        raise UnicodeDecodeError("ascii", b"\xe2", 0, 1, "invalid byte")

    monkeypatch.setattr(hooks.subprocess, "run", _raise_decode)
    assert hooks.install() is None
    assert any("uncompressed" in t for t in rec.texts), rec.texts


@pytest.mark.parametrize("rc", [126, 127, -9, -11])
def test_install_classifies_bash_and_signal_rcs_as_runtime_failures(hooks, monkeypatch, rc):
    """CR 2.4 P2 (blind MEDIUM + edge MEDIUM, PROBE-verified): rc 126/127
    (missing/non-executable setup script) and negative rc (signal-killed
    child) are availability-shaped, NOT integrity corruption — the operator
    must not be steered to sha256/pin inspection for them."""
    rec = _Rec()
    monkeypatch.setattr(hooks, "_style", rec)
    _patch_run(hooks, monkeypatch, result=_Result(rc, "", "bash: setup.sh: Permission denied"))
    hooks.install()
    texts = rec.texts
    assert any("could not run" in t for t in texts), (rc, texts)
    assert not any("integrity" in t for t in texts), (rc, texts)


def test_install_guards_logging_failures(hooks, monkeypatch):
    """CR 2.4 P4 (blind LOW): a broken stderr pipe must not turn a handled
    failure into an unhandled one (NFR6 is absolute)."""
    class _Broken(_Rec):
        def _emit(self, level, *args):
            raise BrokenPipeError("stderr closed")

    rec = _Broken()
    monkeypatch.setattr(hooks, "_style", rec)

    def _raise_os(argv, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(hooks.subprocess, "run", _raise_os)
    assert hooks.install() is None


# ---------------------------------------------------------------------------
# Story 3.4 — Lifecycle Uninstall & Update Safety (FR23/FR24)
# Red phase: these tests are written against the Story-2.4 stub hooks and
# MUST fail until real uninstall()/pre_update() land (Task 1/2).
# ---------------------------------------------------------------------------

class _StyleBoom:
    """Style whose every logging call raises — proves hooks self-guard."""

    def info(self, *args, **kwargs):
        raise AssertionError("style logging must never leak a raise")

    def warning(self, *args, **kwargs):
        raise AssertionError("style logging must never leak a raise")

    def error(self, *args, **kwargs):
        raise AssertionError("style logging must never leak a raise")


class _DeletionSpy:
    """Records any filesystem-deletion call; explode on INSTALL_DIR targets."""

    def __init__(self):
        self.calls = []

    def _record(self, name, target):
        self.calls.append((name, str(target)))
        if "usr/local/bin/rtk" in str(target) or str(target).endswith("/rtk"):
            raise AssertionError(
                f"deletion targeted the user-owned RTK binary: {name} {target}"
            )

    def remove(self, path, *a, **k):
        self._record("os.remove", path)

    def unlink(self, path, *a, **k):
        self._record("os.unlink", path)

    def rmtree(self, path, *a, **k):
        self._record("shutil.rmtree", path)


def test_uninstall_never_touches_rtk_binary(hooks, monkeypatch):
    """AC-01/AC-05 (FR23/NFR11): uninstall() must not delete /usr/local/bin/rtk
    — spy every deletion primitive (os.remove/os.unlink/shutil.rmtree)."""
    spy = _DeletionSpy()
    monkeypatch.setattr(hooks.os, "remove", spy.remove)
    monkeypatch.setattr(hooks.os, "unlink", spy.unlink)
    import shutil as _shutil

    monkeypatch.setattr(_shutil, "rmtree", spy.rmtree)
    monkeypatch.setattr(hooks, "_style", _Rec())
    assert hooks.uninstall() is None
    assert spy.calls == [], spy.calls


def test_uninstall_never_raises_when_style_breaks(hooks, monkeypatch):
    """AC-04 (NFR6/D10): uninstall degrades to return when its own logging
    raises — the installer re-raises hook exceptions (install.py re-raise)."""
    monkeypatch.setattr(hooks, "_style", _StyleBoom())
    assert hooks.uninstall() is None


def _pin_version():
    """Pin exactly as hooks will parse it: rtk_pin.txt, stripped, v-prefix dropped."""
    raw = (PLUGIN_ROOT / "rtk_pin.txt").read_text(encoding="utf-8").strip()
    return raw[1:] if raw.startswith("v") else raw


def _hook_run_setup_count(calls):
    """Count setup.sh invocations (argv-list starting with bash + setup path)."""
    return sum(
        1
        for argv, _kw in calls
        if isinstance(argv, list)
        and len(argv) >= 2
        and argv[0] == "bash"
        and str(hooks_path_marker()) in argv[1]
    )


def hooks_path_marker():
    return "scripts" + "/" + "setup.sh"


def _probe_result(out, rc=0, err=""):
    return _Result(rc, out, err)


def _patch_version_then_setup(hooks, monkeypatch, probe, setup=None, exc=None):
    """Mock subprocess.run: first call = rtk --version probe, second = setup.sh."""
    calls = []
    state = {"probe_done": False}

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        is_probe = isinstance(argv, list) and len(argv) >= 2 and argv[-1] == "--version"
        if is_probe and not state["probe_done"]:
            state["probe_done"] = True
            if isinstance(probe, BaseException):
                raise probe
            return probe
        if exc is not None:
            raise exc
        return setup if setup is not None else _Result(0, "", "")

    monkeypatch.setattr(hooks.subprocess, "run", fake_run)
    return calls


def test_pre_update_provisions_on_version_drift(hooks, monkeypatch):
    """AC-02 (FR24): installed 'rtk 9.9.9' != pin → setup.sh re-provisions once."""
    calls = _patch_version_then_setup(
        hooks, monkeypatch, probe=_probe_result("rtk 9.9.9"), setup=_Result(0, "warned", "")
    )
    monkeypatch.setattr(hooks, "_style", _Rec())
    assert hooks.pre_update() is None
    assert _hook_run_setup_count(calls) == 1, calls


def test_pre_update_drift_substring_trap(hooks, monkeypatch):
    """AC-02: 'rtk 1.0.10' must NOT match pin 1.0.1 (token-exact, CR-2.2 lesson)."""
    calls = _patch_version_then_setup(
        hooks, monkeypatch, probe=_probe_result("rtk 1.0.10"), setup=_Result(0, "", "")
    )
    monkeypatch.setattr(hooks, "_style", _Rec())
    assert hooks.pre_update() is None
    assert _hook_run_setup_count(calls) == 1, calls


def test_pre_update_drift_on_empty_version_output(hooks, monkeypatch):
    """AC-02: unreadable/empty version output is drift → provision once."""
    calls = _patch_version_then_setup(
        hooks, monkeypatch, probe=_probe_result("   \n"), setup=_Result(0, "", "")
    )
    monkeypatch.setattr(hooks, "_style", _Rec())
    assert hooks.pre_update() is None
    assert _hook_run_setup_count(calls) == 1, calls


def test_pre_update_noop_when_version_matches_pin(hooks, monkeypatch):
    """AC-03 (FR24): 'rtk 1.0.1' == pin 'v1.0.1' → ZERO setup invocations."""
    pin = _pin_version()
    calls = _patch_version_then_setup(
        hooks, monkeypatch, probe=_probe_result(f"rtk {pin}"), setup=_Result(0, "", "")
    )
    rec = _Rec()
    monkeypatch.setattr(hooks, "_style", rec)
    assert hooks.pre_update() is None
    assert _hook_run_setup_count(calls) == 0, calls
    assert any("rtk" in t or pin in t for t in rec.texts), rec.texts


@pytest.mark.parametrize("exc", [FileNotFoundError("no rtk"), OSError("boom"), subprocess.TimeoutExpired("rtk", 10)])
def test_pre_update_provisions_when_probe_unavailable(hooks, monkeypatch, exc):
    """AC-02: probe failure (absent binary / OSError / timeout) = drift → setup once."""
    calls = _patch_version_then_setup(
        hooks, monkeypatch, probe=exc, setup=_Result(0, "", "")
    )
    monkeypatch.setattr(hooks, "_style", _Rec())
    assert hooks.pre_update() is None
    assert _hook_run_setup_count(calls) == 1, calls


def test_pre_update_never_raises_on_setup_failure(hooks, monkeypatch):
    """AC-04 (NFR6): a failing setup.sh during re-provisioning never escapes."""
    calls = _patch_version_then_setup(
        hooks, monkeypatch, probe=_probe_result("rtk 9.9.9"), exc=OSError("setup exploded")
    )
    monkeypatch.setattr(hooks, "_style", _Rec())
    assert hooks.pre_update() is None


def test_pre_update_never_raises_when_style_breaks(hooks, monkeypatch):
    """AC-04: even a raising logger cannot fail the update lifecycle."""
    _patch_version_then_setup(
        hooks, monkeypatch, probe=_probe_result("rtk 9.9.9"), setup=_Result(0, "", "")
    )
    monkeypatch.setattr(hooks, "_style", _StyleBoom())
    assert hooks.pre_update() is None


# ---------------------------------------------------------------------------
# CR 3.4 regression tests — defects found by the review layers
# (all reproduced by parent fault-injection probes before patching)
# ---------------------------------------------------------------------------


class _EvilStr(Exception):
    """Exception whose __str__ itself raises (double-fault class)."""

    def __str__(self):
        raise RuntimeError("str broken")


class _ClosedWriter(_Rec):
    """Writer that raises ValueError like a closed stdio stream."""

    def _emit(self, level, *args):
        raise ValueError("I/O operation on closed file")


def test_install_swallows_closed_stdio_valueerror(hooks, monkeypatch):
    """CR 3.4 F1 (edge HIGH): a closed stderr raises ValueError (NOT OSError);
    _emit must swallow EVERY writer failure or install() escapes (NFR6)."""
    monkeypatch.setattr(hooks, "_style", _ClosedWriter())

    def _raise_os(argv, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(hooks.subprocess, "run", _raise_os)
    assert hooks.install() is None


def test_install_swallows_unprintable_exception(hooks, monkeypatch):
    """CR 3.4 F2 (edge MEDIUM): an exception whose __str__ raises must not
    escape install() through its own warning message construction."""
    rec = _Rec()
    monkeypatch.setattr(hooks, "_style", rec)

    def _raise_evil(argv, **kwargs):
        raise _EvilStr()

    monkeypatch.setattr(hooks.subprocess, "run", _raise_evil)
    assert hooks.install() is None


def test_pre_update_swallows_unprintable_pin_fault(hooks, monkeypatch):
    """CR 3.4 F2+F4 (edge MEDIUM + blind + auditor): an unprintable OR missing
    pin is drift, not a silent skip - warn and still attempt re-provision."""
    def _raise_evil(*a, **k):
        raise _EvilStr()

    monkeypatch.setattr(hooks, "_pin_version", _raise_evil)
    calls = _patch_version_then_setup(
        hooks, monkeypatch, probe=_probe_result("rtk 1.0.1"), setup=_Result(0, "", "")
    )
    rec = _Rec()
    monkeypatch.setattr(hooks, "_style", rec)
    assert hooks.pre_update() is None
    assert _hook_run_setup_count(calls) == 1, calls
    assert any("pin unreadable" in t for t in rec.texts), rec.texts


def test_pre_update_attempts_setup_when_pin_file_missing(hooks, monkeypatch):
    """CR 3.4 F4 (edge MEDIUM, probe-confirmed): rtk_pin.txt gone -> warn +
    re-provision attempt (exactly one setup invocation)."""
    orig_read = Path.read_text

    def deny(self, *a, **k):
        if self.name == "rtk_pin.txt":
            raise FileNotFoundError("pin gone")
        return orig_read(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", deny)
    calls = _patch_version_then_setup(
        hooks, monkeypatch, probe=_probe_result("rtk 9.9.9"), setup=_Result(0, "", "")
    )
    monkeypatch.setattr(hooks, "_style", _Rec())
    assert hooks.pre_update() is None
    assert _hook_run_setup_count(calls) == 1, calls


def test_uninstall_swallows_unprintable_style_fault(hooks, monkeypatch):
    """CR 3.4 F3 (edge MEDIUM): uninstall's except-branch message construction
    must not let an unprintable exception abort the plugin uninstall flow."""

    def _raise_evil(*a, **k):
        raise _EvilStr()

    monkeypatch.setattr(hooks, "_style", type("B",(),{"info":_raise_evil,"warning":_raise_evil,"error":_raise_evil})())
    assert hooks.uninstall() is None


@pytest.mark.parametrize("exc", [ValueError("weird"), TypeError("weird"), RuntimeError("weird")])
def test_pre_update_provisions_on_any_probe_fault(hooks, monkeypatch, exc):
    """CR 3.4 F5 (edge MEDIUM + blind MEDIUM): ANY probe fault beyond the
    OSError family is still 'unreadable version' = drift -> setup once."""
    calls = _patch_version_then_setup(
        hooks, monkeypatch, probe=exc, setup=_Result(0, "", "")
    )
    monkeypatch.setattr(hooks, "_style", _Rec())
    assert hooks.pre_update() is None
    assert _hook_run_setup_count(calls) == 1, calls


def test_pre_update_provisions_on_nonstring_probe_stdout(hooks, monkeypatch):
    """CR 3.4 F5 (edge MEDIUM): a probe result whose stdout is not text
    degrades to drift (parse fault) and still re-provisions once."""
    calls = _patch_version_then_setup(
        hooks, monkeypatch, probe=_Result(0, 12345, ""), setup=_Result(0, "", "")
    )
    monkeypatch.setattr(hooks, "_style", _Rec())
    assert hooks.pre_update() is None
    assert _hook_run_setup_count(calls) == 1, calls


def test_pre_update_provisions_even_when_style_breaks(hooks, monkeypatch):
    """CR 3.4 F6 (blind MEDIUM, probe-confirmed): a broken status logger must
    not abort the re-provisioning it announces (status lines are best-effort)."""
    calls = _patch_version_then_setup(
        hooks, monkeypatch, probe=_probe_result("rtk 9.9.9"), setup=_Result(0, "", "")
    )
    monkeypatch.setattr(hooks, "_style", _StyleBoom())
    assert hooks.pre_update() is None
    assert _hook_run_setup_count(calls) == 1, calls


def test_pre_update_v_prefixed_installed_matches_pin(hooks, monkeypatch):
    """CR 3.4 E7 coverage gap: installed output 'rtk v1.0.1' (v-stripped)
    equals the pin -> ZERO setup invocations (parse branch :179 pinned)."""
    pin = _pin_version()
    calls = _patch_version_then_setup(
        hooks, monkeypatch, probe=_probe_result(f"rtk v{pin}"), setup=_Result(0, "", "")
    )
    monkeypatch.setattr(hooks, "_style", _Rec())
    assert hooks.pre_update() is None
    assert _hook_run_setup_count(calls) == 0, calls


@pytest.mark.parametrize(
    "rc,needle",
    [
        (2, "usage error"),
        (126, "could not run"),
        (127, "could not run"),
        (1, "integrity/format failure"),
    ],
)
def test_pre_update_classifies_setup_exit_codes(hooks, monkeypatch, rc, needle):
    """CR 3.4 F7 (auditor D1): update-path re-provision classifies setup
    exits like install() does - AC-04 'surface distinctly' parity."""
    calls = _patch_version_then_setup(
        hooks, monkeypatch, probe=_probe_result("rtk 9.9.9"), setup=_Result(rc, "detail", "")
    )
    rec = _Rec()
    monkeypatch.setattr(hooks, "_style", rec)
    assert hooks.pre_update() is None
    assert _hook_run_setup_count(calls) == 1, calls
    assert any(needle in t for t in rec.texts), (needle, rec.texts)
