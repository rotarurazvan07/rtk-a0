#!/usr/bin/env python3
"""tests: plugin lifecycle wiring via hooks.py (install warns-not-fails). Pure runtime — no framework imports; hooks.py is loaded by path the same way helpers/plugins.py:918-921 imports it during plugin lifecycle dispatch. Network-free by design: subprocess.run is mocked; setup.sh is never executed."""
import importlib.util
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
HOOKS_PATH = PLUGIN_ROOT / "hooks.py"

# Two loading paths, one behavior: A0 path-loads hooks.py at runtime
# (fixture below, mirroring helpers/plugins.py) while a normal static import
# (`import hooks`) documents the ordinary-package view. The equivalence test
# pins that both surfaces are identical.
sys.path.insert(0, str(PLUGIN_ROOT))
import hooks as _static_hooks  # noqa: E402


def _load_hooks():
    """Import the plugin hooks module the way A0 loads it (by file path)."""
    spec = importlib.util.spec_from_file_location("rtk_a0_hooks", HOOKS_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_path_load_and_static_import_expose_identical_surface(hooks):
    """The A0 path-loaded module and a static `import hooks` expose the same
    public surface: the framework dispatches getattr(hooks, name), so both
    loads must serve identical callables and constants."""
    for name in ("install", "pre_update", "uninstall"):
        assert callable(getattr(_static_hooks, name)), f"static import missing {name}"
        assert getattr(hooks, name).__code__.co_code == getattr(_static_hooks, name).__code__.co_code, (
            f"{name} differs between path-load and static import"
        )
    for const in ("INSTALL_DIR", "_SETUP_SCRIPT", "_SETUP_TIMEOUT"):
        assert getattr(hooks, const) == getattr(_static_hooks, const), (
            f"constant {const} differs between loads"
        )


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


def _install_style(hooks, monkeypatch, rec=None):
    """Single point of the test<->module coupling: install a recording sink on
    hooks._style and return it. Tests must patch this module global to observe
    operator messages; the loaded module is passed explicitly so the helper
    never resolves the pytest fixture object at module scope."""
    rec = rec if rec is not None else _Rec()
    monkeypatch.setattr(hooks, "_style", rec)
    return rec


# ---------------------------------------------------------------------------
# — real A0 lifecycle contract pins
# ---------------------------------------------------------------------------

def test_lifecycle_hooks_are_zero_arg_callables(hooks, monkeypatch):
    """The framework lifecycle dispatcher calls every hook by name with NO arguments (getattr, zero args), so every hook must be callable with zero args."""
    # stub the setup invocation so the zero-arg probe never
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
    """The framework safe_call helper does not catch exceptions and the installer re-raises: hooks must self-guard and a missing script path must not propagate."""
    monkeypatch.setattr(hooks, "_SETUP_SCRIPT", hooks._SETUP_SCRIPT / "missing.sh")

    def _raise_missing(argv, **kwargs):
        raise FileNotFoundError("setup script missing")

    monkeypatch.setattr(hooks.subprocess, "run", _raise_missing)
    rec = _install_style(hooks, monkeypatch)
    hooks.install()
    assert any("could not run" in t for t in rec.texts), rec.texts


# ---------------------------------------------------------------------------
# — install: invocation, exit-code mapping, output surfacing
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
    """Setup.sh is invoked argv-list style, never through a shell."""
    calls = _patch_run(hooks, monkeypatch, result=_Result(0, "ok", ""))
    hooks.install()
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert isinstance(argv, list) and argv[0] == "bash"
    assert argv[1] == str(hooks._SETUP_SCRIPT)
    assert kwargs.get("shell") in (None, False)


@pytest.mark.parametrize("rc", [0, 2, 1, 3, 127])
def test_install_returns_without_raising_for_every_exit_class(hooks, monkeypatch, rc):
    """The setup.sh exit contract (0 provisioned-or-warned, 2 usage, other integrity) completes the hook without raising in every class."""
    _patch_run(hooks, monkeypatch, result=_Result(rc, f"out{rc}", f"err{rc}"))
    assert hooks.install() is None


def test_install_surfaces_setup_output_on_success(hooks, monkeypatch):
    """Setup output is surfaced to the operator."""
    rec = _install_style(hooks, monkeypatch)
    _patch_run(hooks, monkeypatch, result=_Result(0, "rtk setup: installed rtk v1.0.1", ""))
    hooks.install()
    assert any("installed rtk" in t for t in rec.texts), rec.texts


def test_install_logs_warning_for_usage_exit(hooks, monkeypatch):
    """Exit 2 (usage error) is surfaced as a warning."""
    rec = _install_style(hooks, monkeypatch)
    _patch_run(hooks, monkeypatch, result=_Result(2, "", "rtk setup: unknown argument"))
    hooks.install()
    assert any("unknown argument" in t for t in rec.texts)


def test_install_logs_warning_for_integrity_exit(hooks, monkeypatch):
    """non-zero non-2 exit (integrity/format corruption) warns and returns."""
    rec = _install_style(hooks, monkeypatch)
    _patch_run(hooks, monkeypatch, result=_Result(1, "", "rtk setup: malformed pin"))
    hooks.install()
    assert any("malformed pin" in t for t in rec.texts)


def test_install_surfaces_warning_output_on_exit_zero_warn_path(hooks, monkeypatch):
    """Warned-unavailable runs also exit 0 with stderr text; the skip and warn messages must surface."""
    rec = _install_style(hooks, monkeypatch)
    _patch_run(
        hooks,
        monkeypatch,
        result=_Result(0, "rtk setup: rtk 1.0.1 already on PATH — skipping", ""),
    )
    hooks.install()
    assert any("already on PATH" in t for t in rec.texts), rec.texts


@pytest.mark.parametrize("exc", [FileNotFoundError("no bash"), OSError("boom"), subprocess.TimeoutExpired("bash", 120)])
def test_install_swallows_exceptions_with_warning(hooks, monkeypatch, exc):
    """Any subprocess-layer exception warns and returns."""
    rec = _install_style(hooks, monkeypatch)
    _patch_run(hooks, monkeypatch, exc=exc)
    assert hooks.install() is None
    assert rec.levels("warning") or rec.levels("error"), "expected a warning/error log"


def test_install_exception_path_logs_reason(hooks, monkeypatch):
    """the warning must carry the failure reason for the operator."""
    rec = _install_style(hooks, monkeypatch)
    _patch_run(hooks, monkeypatch, exc=OSError("disk-on-fire"))
    hooks.install()
    assert any("disk-on-fire" in t for t in rec.texts), rec.texts


# ---------------------------------------------------------------------------
# —: subprocess PATH must include the install dir
# ---------------------------------------------------------------------------

def test_setup_env_appends_install_dir_when_missing(hooks, monkeypatch):
    """when caller PATH lacks /usr/local/bin, the setup subprocess PATH gains it so setup.sh's PATH-resolved self-check (setup.sh:217-219) resolves the freshly installed binary."""
    monkeypatch.setattr(hooks.os, "environ", {"PATH": "/opt/venv/bin"})
    env = hooks._setup_env()
    assert hooks.INSTALL_DIR in env["PATH"].split(os.pathsep)


def test_setup_env_preserves_path_when_install_dir_present(hooks, monkeypatch):
    """an existing /usr/local/bin entry is not duplicated."""
    monkeypatch.setattr(
        hooks.os, "environ", {"PATH": os.pathsep.join(["/usr/local/bin", "/opt/venv/bin"])}
    )
    env = hooks._setup_env()
    assert env["PATH"].count(hooks.INSTALL_DIR) == 1
    assert env["PATH"].split(os.pathsep)[0] == "/usr/local/bin"


def test_install_passes_env_with_install_dir_to_subprocess(hooks, monkeypatch):
    """end-to-end: the env handed to subprocess.run contains INSTALL_DIR."""
    seen = {}

    def recorder(mod, argv, kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(hooks.os, "environ", {"PATH": "/opt/venv/bin"})
    _patch_run(hooks, monkeypatch, result=_Result(0, "", ""), recorder=recorder)
    hooks.install()
    assert hooks.INSTALL_DIR in seen.get("env", {}).get("PATH", "").split(os.pathsep)


# ---------------------------------------------------------------------------
# — concurrent setup invocations are serialized
# ---------------------------------------------------------------------------

def test_setup_calls_are_serialized_across_threads(hooks, monkeypatch):
    """Concurrent install callers must not interleave inside the fixed staging path; the module lock serializes them."""
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
    """structural pin: a lock guards the setup invocation."""
    lock = getattr(hooks, "_SETUP_LOCK", None)
    assert lock is not None and hasattr(lock, "acquire") and hasattr(lock, "release")


# ---------------------------------------------------------------------------
# — stub safety (owns full pre_update/uninstall)
# ---------------------------------------------------------------------------

def test_pre_update_and_uninstall_never_raise(hooks, monkeypatch):
    """The lifecycle hooks are real implementations now, replacing the earlier safe-no-op test. The never-raise invariant is pinned: no deletion primitive may target the user-owned binary under the install dir. Subprocess is mocked so the probe and setup never touch this host."""
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
        assert banned not in code, f"no file removal permitted in hooks: found {banned}"


def test_hooks_source_pins(hooks):
    """Structural pins: no shell invocation, no hardcoded version (single-source pin file)."""
    code = "\n".join(
        ln for ln in HOOKS_PATH.read_text(encoding="utf-8").splitlines()
        if not ln.lstrip().startswith("#")
    )
    assert "shell=True" not in code
    assert "v1.0.1" not in code, "pin must stay single-source in rtk_pin.txt"
    assert "/usr/local/bin" in code, "install dir constant required"


def test_setup_script_referenced_exists(hooks):
    """the hooked script path resolves inside the plugin."""
    assert hooks._SETUP_SCRIPT == PLUGIN_ROOT / "scripts" / "setup.sh"
    assert hooks._SETUP_SCRIPT.is_file()



# ---------------------------------------------------------------------------
# regression tests — defects found by the review layers
# ---------------------------------------------------------------------------

def test_install_survives_unicode_decode_error(hooks, monkeypatch):
    """Text-mode subprocess output decodes strictly in the interpreter locale; a non-ASCII byte from setup.sh raises UnicodeDecodeError (a ValueError, not an OSError), so it must not escape install and fail the plugin lifecycle."""
    rec = _install_style(hooks, monkeypatch)

    def _raise_decode(argv, **kwargs):
        raise UnicodeDecodeError("ascii", b"\xe2", 0, 1, "invalid byte")

    monkeypatch.setattr(hooks.subprocess, "run", _raise_decode)
    assert hooks.install() is None
    assert any("uncompressed" in t for t in rec.texts), rec.texts


@pytest.mark.parametrize("rc", [126, 127, -9, -11])
def test_install_classifies_bash_and_signal_rcs_as_runtime_failures(hooks, monkeypatch, rc):
    """Missing or non-executable script (126/127) and signal-killed children (negative rc) are availability-shaped, not integrity corruption — the operator must not be steered to checksum or pin inspection for them."""
    rec = _install_style(hooks, monkeypatch)
    _patch_run(hooks, monkeypatch, result=_Result(rc, "", "bash: setup.sh: Permission denied"))
    hooks.install()
    texts = rec.texts
    assert any("could not run" in t for t in texts), (rc, texts)
    assert not any("integrity" in t for t in texts), (rc, texts)


def test_install_guards_logging_failures(hooks, monkeypatch):
    """A broken stderr pipe must not turn a handled failure into an unhandled one."""
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
# — Lifecycle Uninstall & Update Safety
# Red phase: these tests are written against the Story-2.4 stub hooks and
# MUST fail until real uninstall/pre_update land (/2).
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
    """ uninstall must not delete /usr/local/bin/rtk — spy every deletion primitive (os.remove/os.unlink/shutil.rmtree)."""
    spy = _DeletionSpy()
    monkeypatch.setattr(hooks.os, "remove", spy.remove)
    monkeypatch.setattr(hooks.os, "unlink", spy.unlink)
    import shutil as _shutil

    monkeypatch.setattr(_shutil, "rmtree", spy.rmtree)
    monkeypatch.setattr(hooks, "_style", _Rec())
    assert hooks.uninstall() is None
    assert spy.calls == [], spy.calls


def test_uninstall_never_raises_when_style_breaks(hooks, monkeypatch):
    """Uninstall degrades to return when its own logging raises: the installer re-raises hook exceptions."""
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
    """installed 'rtk 9.9.9' != pin → setup.sh re-provisions once."""
    calls = _patch_version_then_setup(
        hooks, monkeypatch, probe=_probe_result("rtk 9.9.9"), setup=_Result(0, "warned", "")
    )
    monkeypatch.setattr(hooks, "_style", _Rec())
    assert hooks.pre_update() is None
    assert _hook_run_setup_count(calls) == 1, calls


def test_pre_update_drift_substring_trap(hooks, monkeypatch):
    """'rtk 1.0.10' must NOT match pin 1.0.1 (token-exact, lesson)."""
    calls = _patch_version_then_setup(
        hooks, monkeypatch, probe=_probe_result("rtk 1.0.10"), setup=_Result(0, "", "")
    )
    monkeypatch.setattr(hooks, "_style", _Rec())
    assert hooks.pre_update() is None
    assert _hook_run_setup_count(calls) == 1, calls


def test_pre_update_drift_on_empty_version_output(hooks, monkeypatch):
    """unreadable/empty version output is drift → provision once."""
    calls = _patch_version_then_setup(
        hooks, monkeypatch, probe=_probe_result("   \n"), setup=_Result(0, "", "")
    )
    monkeypatch.setattr(hooks, "_style", _Rec())
    assert hooks.pre_update() is None
    assert _hook_run_setup_count(calls) == 1, calls


def test_pre_update_noop_when_version_matches_pin(hooks, monkeypatch):
    """'rtk 1.0.1' == pin 'v1.0.1' → ZERO setup invocations."""
    pin = _pin_version()
    calls = _patch_version_then_setup(
        hooks, monkeypatch, probe=_probe_result(f"rtk {pin}"), setup=_Result(0, "", "")
    )
    rec = _install_style(hooks, monkeypatch)
    assert hooks.pre_update() is None
    assert _hook_run_setup_count(calls) == 0, calls
    assert any("rtk" in t or pin in t for t in rec.texts), rec.texts


@pytest.mark.parametrize("exc", [FileNotFoundError("no rtk"), OSError("boom"), subprocess.TimeoutExpired("rtk", 10)])
def test_pre_update_provisions_when_probe_unavailable(hooks, monkeypatch, exc):
    """A probe failure (absent binary, OSError, timeout) is drift and triggers one setup run."""
    calls = _patch_version_then_setup(
        hooks, monkeypatch, probe=exc, setup=_Result(0, "", "")
    )
    monkeypatch.setattr(hooks, "_style", _Rec())
    assert hooks.pre_update() is None
    assert _hook_run_setup_count(calls) == 1, calls


def test_pre_update_never_raises_on_setup_failure(hooks, monkeypatch):
    """a failing setup.sh during re-provisioning never escapes."""
    calls = _patch_version_then_setup(
        hooks, monkeypatch, probe=_probe_result("rtk 9.9.9"), exc=OSError("setup exploded")
    )
    monkeypatch.setattr(hooks, "_style", _Rec())
    assert hooks.pre_update() is None


def test_pre_update_never_raises_when_style_breaks(hooks, monkeypatch):
    """even a raising logger cannot fail the update lifecycle."""
    _patch_version_then_setup(
        hooks, monkeypatch, probe=_probe_result("rtk 9.9.9"), setup=_Result(0, "", "")
    )
    monkeypatch.setattr(hooks, "_style", _StyleBoom())
    assert hooks.pre_update() is None


# ---------------------------------------------------------------------------
# regression tests — defects found by the review layers
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
    """A closed stderr raises ValueError (not OSError), so the emit path must swallow every writer failure or install escapes."""
    monkeypatch.setattr(hooks, "_style", _ClosedWriter())

    def _raise_os(argv, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(hooks.subprocess, "run", _raise_os)
    assert hooks.install() is None


def test_install_swallows_unprintable_exception(hooks, monkeypatch):
    """An exception whose __str__ raises must not escape install through its own warning message construction."""
    rec = _install_style(hooks, monkeypatch)

    def _raise_evil(argv, **kwargs):
        raise _EvilStr()

    monkeypatch.setattr(hooks.subprocess, "run", _raise_evil)
    assert hooks.install() is None


def test_pre_update_swallows_unprintable_pin_fault(hooks, monkeypatch):
    """An unprintable or missing pin is drift, not a silent skip: warn and still attempt re-provisioning."""
    def _raise_evil(*a, **k):
        raise _EvilStr()

    monkeypatch.setattr(hooks, "_pin_version", _raise_evil)
    calls = _patch_version_then_setup(
        hooks, monkeypatch, probe=_probe_result("rtk 1.0.1"), setup=_Result(0, "", "")
    )
    rec = _install_style(hooks, monkeypatch)
    assert hooks.pre_update() is None
    assert _hook_run_setup_count(calls) == 1, calls
    assert any("pin unreadable" in t for t in rec.texts), rec.texts


def test_pre_update_attempts_setup_when_pin_file_missing(hooks, monkeypatch):
    """A missing pin file warns and still attempts re-provisioning exactly once."""
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
    """The uninstall except-branch message construction must not let an unprintable exception abort the uninstall flow."""

    def _raise_evil(*a, **k):
        raise _EvilStr()

    monkeypatch.setattr(hooks, "_style", type("B",(),{"info":_raise_evil,"warning":_raise_evil,"error":_raise_evil})())
    assert hooks.uninstall() is None


@pytest.mark.parametrize("exc", [ValueError("weird"), TypeError("weird"), RuntimeError("weird")])
def test_pre_update_provisions_on_any_probe_fault(hooks, monkeypatch, exc):
    """Any probe fault beyond the OSError family still reads as unreadable-version drift and triggers one setup run."""
    calls = _patch_version_then_setup(
        hooks, monkeypatch, probe=exc, setup=_Result(0, "", "")
    )
    monkeypatch.setattr(hooks, "_style", _Rec())
    assert hooks.pre_update() is None
    assert _hook_run_setup_count(calls) == 1, calls


def test_pre_update_provisions_on_nonstring_probe_stdout(hooks, monkeypatch):
    """A probe result whose stdout is not text degrades to drift and still re-provisions once."""
    calls = _patch_version_then_setup(
        hooks, monkeypatch, probe=_Result(0, 12345, ""), setup=_Result(0, "", "")
    )
    monkeypatch.setattr(hooks, "_style", _Rec())
    assert hooks.pre_update() is None
    assert _hook_run_setup_count(calls) == 1, calls


def test_pre_update_provisions_even_when_style_breaks(hooks, monkeypatch):
    """A broken status logger must not abort the re-provisioning it announces: status lines are best-effort."""
    calls = _patch_version_then_setup(
        hooks, monkeypatch, probe=_probe_result("rtk 9.9.9"), setup=_Result(0, "", "")
    )
    monkeypatch.setattr(hooks, "_style", _StyleBoom())
    assert hooks.pre_update() is None
    assert _hook_run_setup_count(calls) == 1, calls


def test_pre_update_v_prefixed_installed_matches_pin(hooks, monkeypatch):
    """E7 coverage gap: installed output 'rtk v1.0.1' (v-stripped) equals the pin -> ZERO setup invocations (parse branch:179 pinned)."""
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
    """The update path classifies setup exits the same way install does, surfacing each class distinctly."""
    calls = _patch_version_then_setup(
        hooks, monkeypatch, probe=_probe_result("rtk 9.9.9"), setup=_Result(rc, "detail", "")
    )
    rec = _install_style(hooks, monkeypatch)
    assert hooks.pre_update() is None
    assert _hook_run_setup_count(calls) == 1, calls
    assert any(needle in t for t in rec.texts), (needle, rec.texts)
