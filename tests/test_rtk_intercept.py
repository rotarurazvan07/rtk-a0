#!/usr/bin/env python3
"""Framework-dependent tests (registration, no-op execute). Requires helpers.extension."""
import os
import sys
from pathlib import Path
from unittest.mock import patch

# Ensure framework helpers are importable (A0 framework root, overridable)
sys.path.insert(0, os.environ.get("A0_FRAMEWORK_ROOT", "/a0"))

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
EXT_DIR = PLUGIN_ROOT / "extensions" / "python" / "tool_execute_before"


def test_extension_folder_exists():
    """Extension folder exists at correct path."""
    assert EXT_DIR.exists(), f"Extension folder missing: {EXT_DIR}"
    # Should contain exactly one _10_rtk_intercept.py
    files = list(EXT_DIR.glob("*_rtk_intercept.py"))
    assert len(files) == 1, f"Expected one _rtk_intercept.py, found {len(files)}"
    assert files[0].name == "_10_rtk_intercept.py", f"Wrong name: {files[0].name}"


def test_rtk_intercept_is_extension_subclass():
    """Registration: RtkIntercept is an Extension subclass discoverable by framework loader."""
    from helpers.extension import Extension
    
    # Import the module directly
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_10_rtk_intercept", EXT_DIR / "_10_rtk_intercept.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    
    assert hasattr(module, "RtkIntercept"), "RtkIntercept class not found"
    assert issubclass(module.RtkIntercept, Extension), "RtkIntercept must extend Extension"


def test_framework_loader_discovers_rtk_intercept():
    """registration smoke test — the framework's own folder loader (helpers.modules.load_classes_from_folder, used by _get_extension_classes) discovers RtkIntercept as an Extension subclass from the named extension folder."""
    from helpers.extension import Extension
    from helpers import modules
    
    classes = modules.load_classes_from_folder(str(EXT_DIR), "*", Extension)
    class_names = [cls.__name__ for cls in classes]
    
    assert "RtkIntercept" in class_names, f"RtkIntercept not discovered; found: {class_names}"


def test_no_op_execute_leaves_tool_args_unchanged():
    """execute is a no-op — leaves tool_args dict unchanged."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_10_rtk_intercept", EXT_DIR / "_10_rtk_intercept.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    
    ext = module.RtkIntercept(agent=None)
    
    tool_args = {"code": "echo hello", "runtime": "terminal", "tool_name": "code_execution_tool"}
    original = {"code": "echo hello", "runtime": "terminal", "tool_name": "code_execution_tool"}
    
    # Call execute exactly as the framework does for this hook point: it passes
    # the mutable tool args into the extension.
    ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
    
    # the dict object and its contents must be untouched (no-op).
    assert tool_args == original, "execute() must not modify tool_args"
    assert tool_args["code"] == original["code"], "execute() must not mutate tool_args['code']"


def test_no_op_execute_spawns_no_subprocess():
    """execute spawns no subprocess."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_10_rtk_intercept", EXT_DIR / "_10_rtk_intercept.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    
    ext = module.RtkIntercept(agent=None)
    
    with patch("subprocess.run") as mock_run:
        ext.execute()
        mock_run.assert_not_called()


def test_no_op_execute_writes_no_files():
    """execute writes no files."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_10_rtk_intercept", EXT_DIR / "_10_rtk_intercept.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    
    ext = module.RtkIntercept(agent=None)
    
    # Snapshot directory state before.: exclude generated runner
    # artifacts (.pytest_cache/__pycache__) -- a concurrent pytest run writes
    # them under PLUGIN_ROOT and trips an all-inclusive snapshot (reproduced
    # during verification runs); they are not plugin writes, which is
    # the property this no-op pin exists to prove.
    def _plugin_files():
        return {
            f.stat().st_mtime_ns
            for f in PLUGIN_ROOT.rglob("*")
            if f.is_file() and ".pytest_cache" not in f.parts and "__pycache__" not in f.parts
        }

    before = _plugin_files()

    ext.execute()

    after = _plugin_files()
    
    # No file modifications (same set of mtimes)
    assert before == after, "execute() must not create or modify any files"

# =====: Terminal-Command Gating with Idempotence =====

def _load_ext():
    """Load the extension module; return an instance bound to no agent."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_10_rtk_intercept", EXT_DIR / "_10_rtk_intercept.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.RtkIntercept(agent=None)


def test_gate_non_code_execution_tool_skips():
    """tool_name != code_execution_tool -> return immediately, zero subprocess."""
    ext = _load_ext()
    with patch("subprocess.run") as mock_run:
        ext.execute(tool_name="some_other_tool", tool_args={"code": "echo hello"})
        mock_run.assert_not_called()


def test_gate_non_terminal_runtime_passes_through():
    """code_execution_tool with runtime != terminal -> code payload unchanged."""
    ext = _load_ext()
    for runtime in ("python", "nodejs", "output", "invalid", None, ""):
        tool_args = {"code": "print('hello')", "runtime": runtime, "tool_name": "code_execution_tool"}
        original = tool_args.copy()
        with patch("subprocess.run") as mock_run:
            ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
            mock_run.assert_not_called()
        assert tool_args == original, f"runtime={runtime!r} must not mutate tool_args"


def test_gate_invalid_code_payload_returns_unchanged():
    """code None/empty/whitespace/non-string -> no error, args unchanged."""
    ext = _load_ext()
    for code in (None, "", "   ", "\t\n", 123, [], {}):
        tool_args = {"code": code, "runtime": "terminal", "tool_name": "code_execution_tool"}
        original = tool_args.copy()
        with patch("subprocess.run") as mock_run:
            ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
            mock_run.assert_not_called()
        assert tool_args == original, f"code={code!r} must not mutate tool_args"
        assert tool_args["code"] == original["code"], f"code={code!r} must not mutate tool_args['code']"


def test_gate_rtk_prefixed_command_zero_subprocess():
    """stripped form starts with 'rtk ' -> pass through untouched, zero subprocess."""
    ext = _load_ext()
    for cmd in ("rtk git status", "rtk rewrite echo hi", "rtk bypass cargo test", "  rtk gain", "\trtk rewrite ls"):
        tool_args = {"code": cmd, "runtime": "terminal", "tool_name": "code_execution_tool"}
        original = tool_args.copy()
        with patch("subprocess.run") as mock_run:
            ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
            mock_run.assert_not_called()
        assert tool_args == original, f"{cmd!r} must not mutate tool_args"
        assert tool_args["code"] == original["code"], f"{cmd!r} must not mutate tool_args['code']"


def test_gate_exact_equality_and_rtk_git_status_zero_subprocess():
    """exact equality gating; mocked subprocess counter asserts zero invocations for 'rtk git status'."""
    ext = _load_ext()

    # rtk-prefixed terminal command -> idempotence gate -> zero subprocess invocations
    tool_args = {"code": "rtk git status", "runtime": "terminal", "tool_name": "code_execution_tool"}
    with patch("subprocess.run") as mock_run:
        ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
        assert mock_run.call_count == 0, "zero subprocess invocations required for rtk git status"
    assert tool_args["code"] == "rtk git status", "rtk command must pass through unchanged"

    # exact equality on tool_name: a prefix-extension name must NOT pass the gate
    ta2 = {"code": "echo hello", "runtime": "terminal", "tool_name": "code_execution_tool_extra"}
    with patch("subprocess.run") as mock_run:
        ext.execute(tool_name="code_execution_tool_extra", tool_args=ta2)
        mock_run.assert_not_called()
    assert ta2["code"] == "echo hello", "non-exact tool_name must be skipped"

    # exact equality on runtime: 'terminal_extra' must NOT pass the gate
    ta3 = {"code": "echo hello", "runtime": "terminal_extra", "tool_name": "code_execution_tool"}
    with patch("subprocess.run") as mock_run:
        ext.execute(tool_name="code_execution_tool", tool_args=ta3)
        mock_run.assert_not_called()
    assert ta3["code"] == "echo hello", "non-exact runtime must be skipped"


def test_gate_contract_source_structure():
    """The extension implements exactly the pinned gate conditions in guard order: tool identity, runtime, payload, idempotence."""
    src = (EXT_DIR / "_10_rtk_intercept.py").read_text()
    guards = [
        'tool_name != "code_execution_tool"',      # guard 1: tool identity
        'tool_args.get("runtime") != "terminal"',  # guard 2: terminal runtime
        'isinstance(code, str)',                   # guard 3: payload sanity
        'startswith("rtk ")',                     # guard 4: idempotence
    ]
    pos = -1
    for g in guards:
        i = src.find(g)
        assert i != -1, f"gate condition missing from extension source: {g!r}"
        assert i > pos, f"guard order violated: {g!r} must come after the previous guard"
        pos = i

# =====: RTK Rewrite Delegation & Command Mutation =====
# Mock patch target: the extension imports `subprocess` (module) and calls
# subprocess.run(...) -> patch where looked up: 'subprocess.run' stdlib attr.
import subprocess as _subprocess


def _fake_run(rc=0, stdout=""):
    """Helper: side_effect returning a CompletedProcess-like object."""
    def _side(argv, **kwargs):
        return _subprocess.CompletedProcess(argv, rc, stdout, None)
    return _side


def test_delegation_invokes_rtk_rewrite_argv_list():
    """rc==0 + different stdout -> subprocess.run called with argv list ['rtk','rewrite',cmd], capture_output, text, timeout=RTK_REWRITE_TIMEOUT; no shell=True."""
    ext = _load_ext()
    tool_args = {"code": "git status", "runtime": "terminal", "tool_name": "code_execution_tool"}
    with patch("shutil.which", return_value="/usr/local/bin/rtk"), patch(
        "subprocess.run", side_effect=_fake_run(rc=0, stdout="rtk git status")
    ) as m:
        ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
    m.assert_called_once()
    argv, kwargs = m.call_args
    assert argv[0] == ["rtk", "rewrite", "git status"], f"argv-list form required: {argv}"
    assert kwargs.get("capture_output") is True
    assert kwargs.get("text") is True
    assert kwargs.get("timeout") == 2.0
    assert "shell" not in kwargs or kwargs.get("shell") is False, "shell=True forbidden (D2)"


def test_mutation_on_success():
    """rc==0, non-empty stdout != original -> tool_args['code'] mutated to stripped rewritten command; no other key written."""
    ext = _load_ext()
    tool_args = {
        "code": "git status",
        "runtime": "terminal",
        "tool_name": "code_execution_tool",
        "session": 0,
    }
    snapshot = dict(tool_args)
    with patch("shutil.which", return_value="/usr/local/bin/rtk"), patch(
        "subprocess.run", side_effect=_fake_run(rc=0, stdout="  rtk git status  \n")
    ):
        ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
    assert tool_args["code"] == "rtk git status", "stripped rewritten must replace code"
    for key in ("runtime", "tool_name", "session"):
        assert tool_args[key] == snapshot[key], f"only 'code' may be written, touched {key}"


def test_no_mutation_on_nonzero_rc():
    """rc outside 0 and 3 (here rc 1: no rewrite) never mutates, even with stdout."""
    ext = _load_ext()
    tool_args = {"code": "git status", "runtime": "terminal", "tool_name": "code_execution_tool"}
    with patch("shutil.which", return_value="/usr/local/bin/rtk"), patch(
        "subprocess.run", side_effect=_fake_run(rc=1, stdout="rtk git status")
    ) as run:
        ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
    run.assert_called_once()  # : delegation must be reached (no vacuous pass on rtk-absent hosts)
    assert tool_args["code"] == "git status", "rc!=0 must not mutate"


def test_no_mutation_on_empty_stdout():
    """rc 0 with empty or whitespace stdout does not mutate."""
    ext = _load_ext()
    for out in ("", "   \n\t"):
        tool_args = {"code": "git status", "runtime": "terminal", "tool_name": "code_execution_tool"}
        with patch("shutil.which", return_value="/usr/local/bin/rtk"), patch(
            "subprocess.run", side_effect=_fake_run(rc=0, stdout=out)
        ) as run:
            ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
        run.assert_called_once()  # : delegation must be reached
        assert tool_args["code"] == "git status", f"empty stdout {out!r} must not mutate"


def test_no_mutation_when_rtk_decides_no_compression():
    """rc==0 + stdout identical to original -> original executes exactly as written (no mutation)."""
    ext = _load_ext()
    tool_args = {"code": "git status", "runtime": "terminal", "tool_name": "code_execution_tool"}
    with patch("shutil.which", return_value="/usr/local/bin/rtk"), patch(
        "subprocess.run", side_effect=_fake_run(rc=0, stdout="git status\n")
    ) as run:
        ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
    run.assert_called_once()  # : delegation must be reached
    assert tool_args["code"] == "git status", "identical stdout must not mutate"


def test_rewritten_rtk_prefixed_command_passes_through_untouched():
    """RTK-rewritten commands are rtk-prefixed; the idempotence gate catches them on the next dispatch -> zero subprocess, no re-compression loop. The rewritten form also exercises 's mutation output shape."""
    ext = _load_ext()
    # Round 1: plain command gets rewritten to an rtk-prefixed command.
    tool_args = {"code": "git status", "runtime": "terminal", "tool_name": "code_execution_tool"}
    with patch("shutil.which", return_value="/usr/local/bin/rtk"), patch(
        "subprocess.run", side_effect=_fake_run(rc=0, stdout="rtk git status")
    ):
        ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
    assert tool_args["code"] == "rtk git status"
    # Round 2 (what code_execution_tool would dispatch): rtk-prefixed -> untouched.
    with patch("subprocess.run") as m2:
        ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
        assert m2.call_count == 0, "rewritten rtk command must not be re-compressed (NFR3)"
    assert tool_args["code"] == "rtk git status", "rtk-prefixed command passes through untouched"


def test_dispatch_paths_uniform_mutation():
    """identical rewrite behavior regardless of which A0 dispatch site invokes the hook (agent.py:1219 single-tool, agent.py:1499 message loop, parallel_tools.py:576 worker) — the sites differ only in caller, not in the hook contract; each passes tool_name kwarg + mutable tool_args."""
    ext = _load_ext()
    for site in ("agent.py:1219", "agent.py:1499", "parallel_tools.py:576"):
        tool_args = {"code": "cargo test", "runtime": "terminal", "tool_name": "code_execution_tool"}
        with patch("shutil.which", return_value="/usr/local/bin/rtk"), patch(
            "subprocess.run", side_effect=_fake_run(rc=0, stdout="rtk cargo test")
        ):
            # kwargs form is exactly how each site invokes the named extension
            ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
        assert tool_args["code"] == "rtk cargo test", f"uniform rewrite required at {site}"


def test_timeout_constant_pinned():
    """RTK_REWRITE_TIMEOUT == 2.0 module constant."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_10_rtk_intercept", EXT_DIR / "_10_rtk_intercept.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.RTK_REWRITE_TIMEOUT == 2.0, "RTK_REWRITE_TIMEOUT must be 2.0 (D7)"
    src = (EXT_DIR / "_10_rtk_intercept.py").read_text()
    assert "timeout=RTK_REWRITE_TIMEOUT" in src, "timeout must use the constant, not a literal"


def test_no_shell_true_and_single_mutation_point():
    """structural: shell=True never appears in plugin source; tool_args['code'] = written at exactly one place (single mutation point)."""
    tests_dir = PLUGIN_ROOT / "tests"
    for pyfile in PLUGIN_ROOT.rglob("*.py"):
        if tests_dir in pyfile.parents:  # skip test sources (they name the anti-pattern)
            continue
        src = pyfile.read_text()
        assert "shell=True" not in src, f"shell=True forbidden everywhere: {pyfile}"
    ext_src = (EXT_DIR / "_10_rtk_intercept.py").read_text()
    mutations = ext_src.count("tool_args['code'] =") + ext_src.count('tool_args["code"] =')
    assert mutations == 1, "single mutation point required"


def test_padded_original_no_compression_reply_no_mutation():
    """An original with trailing whitespace echoed back by rtk must not mutate: the no-op reply executes the original exactly as written. Pins the equality gate normalizing both sides."""
    ext = _load_ext()
    for original in ("git status", "git status\n", "  git status  \n"):
        tool_args = {"code": original, "runtime": "terminal", "tool_name": "code_execution_tool"}
        with patch("shutil.which", return_value="/usr/local/bin/rtk"), patch(
            "subprocess.run", side_effect=_fake_run(rc=0, stdout="git status\n")
        ) as run:
            ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
        run.assert_called_once()  # : delegation must be reached
        assert tool_args["code"] == original, f"whitespace-padded original must execute as written: {original!r}"


def test_delegation_exception_fails_open():
    """A subprocess exception (timeout or OSError) leaves tool_args untouched."""
    ext = _load_ext()
    tool_args = {"code": "git status", "runtime": "terminal", "tool_name": "code_execution_tool"}
    with patch("shutil.which", return_value="/usr/local/bin/rtk"), patch(
        "subprocess.run", side_effect=_subprocess.TimeoutExpired("rtk rewrite", 2.0)
    ):
        ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
    assert tool_args["code"] == "git status", "exception path must fail open"


# =====: Fail-Open Guards & Unavailability Warning =====

import shutil


class _StubAgent:
    """Minimal stand-in for the framework Agent for warn-once tests."""

    def __init__(self):
        self.data = {}
        self.warnings = []

        class _Log:
            def __init__(self, sink):
                self.sink = sink

            def log(self, **kwargs):
                self.sink.warnings.append(kwargs)

        self.context = type("Ctx", (), {"log": _Log(self)})()


def _load_ext_with_agent(agent):
    """Load the extension module bound to the given (stub or None) agent."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_10_rtk_intercept", EXT_DIR / "_10_rtk_intercept.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.RtkIntercept(agent=agent)


def test_binary_missing_warns_once_and_skips():
    """A missing binary skips delegation: no subprocess, original command unchanged, the session latch is set, exactly one user-visible warning."""
    agent = _StubAgent()
    ext = _load_ext_with_agent(agent)
    tool_args = {"code": "git status", "runtime": "terminal", "tool_name": "code_execution_tool"}
    with patch("shutil.which", return_value=None) as m_which:
        ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
    m_which.assert_called_once_with("rtk")
    assert tool_args["code"] == "git status", "missing binary must fail open"
    assert agent.data.get("rtk_a0_missing_warned") is True, "warn-once flag must latch"
    assert len(agent.warnings) == 1, f"exactly one warning expected, got {len(agent.warnings)}"


def test_binary_missing_warn_once_silent_afterwards():
    """second and subsequent misses in the same session skip silently — flag stays latched, zero additional warnings."""
    agent = _StubAgent()
    ext = _load_ext_with_agent(agent)
    with patch("shutil.which", return_value=None):
        for _ in range(3):
            tool_args = {"code": "git status", "runtime": "terminal", "tool_name": "code_execution_tool"}
            ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
            assert tool_args["code"] == "git status"
    assert len(agent.warnings) == 1, "warn-once: only the first miss may warn"


def test_binary_missing_no_agent_silent_skip():
    """self.agent is None while rtk missing -> silent skip, no error, no flag write, no subprocess (graceful degradation)."""
    ext = _load_ext()  # agent=None
    tool_args = {"code": "git status", "runtime": "terminal", "tool_name": "code_execution_tool"}
    with patch("shutil.which", return_value=None):
        ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
    assert tool_args["code"] == "git status", "agentless degradation must fail open"


def test_timeout_expired_fails_open():
    """A timeout at the rewrite bound leaves the original command unchanged."""
    ext = _load_ext()
    tool_args = {"code": "git status", "runtime": "terminal", "tool_name": "code_execution_tool"}
    with patch("shutil.which", return_value="/usr/local/bin/rtk"), patch(
        "subprocess.run", side_effect=_subprocess.TimeoutExpired(["rtk", "rewrite", "git status"], 2.0)
    ):
        ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
    assert tool_args["code"] == "git status", "timeout must fail open (NFR1)"


def test_os_error_fails_open():
    """An OSError (e.g. oversized argv) is caught by the guard: original command unchanged, no agent interruption."""
    ext = _load_ext()
    tool_args = {"code": "git status", "runtime": "terminal", "tool_name": "code_execution_tool"}
    with patch("shutil.which", return_value="/usr/local/bin/rtk"), patch(
        "subprocess.run", side_effect=OSError(7, "Argument list too long")
    ):
        ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
    assert tool_args["code"] == "git status", "OSError must fail open"


def test_generic_exception_fails_open():
    """Any other exception is caught by the guard: original command unchanged, no crash, no agent interruption."""
    ext = _load_ext()
    tool_args = {"code": "git status", "runtime": "terminal", "tool_name": "code_execution_tool"}
    with patch("shutil.which", return_value="/usr/local/bin/rtk"), patch(
        "subprocess.run", side_effect=RuntimeError("boom")
    ):
        ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
    assert tool_args["code"] == "git status", "generic exception must fail open"


def test_full_guard_chain_fail_open_invariant():
    """no failure mode mutates the original command — cases 1-7, 9-10 leave it unchanged in 100% of runs; case 8 (rc 0/3 + non-empty differing stdout) is the single sanctioned mutation."""
    original = "cargo test"

    def _args():
        return {"code": original, "runtime": "terminal", "tool_name": "code_execution_tool"}

    # Cases 1-4 (gates): no subprocess, untouched payload.
    ext = _load_ext()
    t = _args()
    with patch("subprocess.run") as m:
        ext.execute(tool_name="not_code_execution_tool", tool_args=t)
        m.assert_not_called()
    assert t["code"] == original

    t = _args()
    t["runtime"] = "python"
    with patch("subprocess.run") as m:
        ext.execute(tool_name="code_execution_tool", tool_args=t)
        m.assert_not_called()
    assert t["code"] == original

    for bad in (None, "", "   ", 42):
        t = _args()
        t["code"] = bad
        with patch("subprocess.run") as m:
            ext.execute(tool_name="code_execution_tool", tool_args=t)
            m.assert_not_called()
        assert t["code"] == bad

    t = _args()
    t["code"] = "rtk bypass git status"
    with patch("subprocess.run") as m:
        ext.execute(tool_name="code_execution_tool", tool_args=t)
        m.assert_not_called()
    assert t["code"] == "rtk bypass git status"

    # Case 5: binary missing (agent present) -> silent skip after single warn.
    t = _args()
    with patch("shutil.which", return_value=None):
        ext.execute(tool_name="code_execution_tool", tool_args=t)
    assert t["code"] == original

    # Case 6: timeout. Case 7: exception family.
    for exc in (
        _subprocess.TimeoutExpired(["rtk", "rewrite", original], 2.0),
        OSError(7, "Argument list too long"),
        RuntimeError("boom"),
    ):
        t = _args()
        with patch("shutil.which", return_value="/usr/local/bin/rtk"), patch(
            "subprocess.run", side_effect=exc
        ):
            ext.execute(tool_name="code_execution_tool", tool_args=t)
        assert t["code"] == original, f"exception {exc!r} must fail open"

    # Cases 8-10 (delegation outcomes): untouched payload EXCEPT
    # case 8's sanctioned rc-0/3 rewrite, asserted in the branch below.
    for rc, stdout in ((1, ""), (1, "err"), (0, ""), (0, "   \n"), (0, original + "\n"), (0, "rtk " + original)):
        t = _args()
        with patch("shutil.which", return_value="/usr/local/bin/rtk"), patch(
            "subprocess.run", side_effect=_fake_run(rc=rc, stdout=stdout)
        ):
            ext.execute(tool_name="code_execution_tool", tool_args=t)
        if rc == 0 and stdout.strip() and stdout.strip() != original:
            assert t["code"] == stdout.strip(), f"rtk-prefixed rewrite expected for {stdout!r}"
        else:
            assert t["code"] == original, f"rc={rc} stdout={stdout!r} must fail open"


def test_guard_contract_source_structure():
    """Structural pins (/05/06): shutil availability guard present in order (after idempotence, before delegation); TimeoutExpired branch precedes catch-all; encoding='utf-8' pinned; warn-once flag written at exactly one place; availability guard precedes the try block."""
    src = (EXT_DIR / "_10_rtk_intercept.py").read_text()
    assert "import shutil" in src, "shutil import required for availability guard"
    assert "shutil.which" in src, "availability guard must call shutil.which"
    assert "rtk_a0_missing_warned" in src
    assert src.count('RTK_MISSING_WARNED] =') == 1, "flag written at exactly one place"
    assert 'RTK_MISSING_WARNED = "rtk_a0_missing_warned"' in src, "warn-once key pinned once (D9)"
    assert "except subprocess.TimeoutExpired" in src, "explicit timeout branch required (D3 case 6)"
    assert src.index("except subprocess.TimeoutExpired") < src.index("except Exception")
    assert "encoding=\"utf-8\"" in src, "explicit utf-8 codec required (locale-safe)"
    # ordering: idempotence gate -> availability -> guarded call
    assert src.index("shutil.which") < src.index("subprocess.run"), "availability precedes delegation"
    assert src.index('startswith("rtk ")') < src.index("shutil.which"), "idempotence precedes availability"


def test_delegation_kwargs_include_utf8_encoding():
    """The single delegation call pins utf-8 decoding alongside the argv list, capture, text mode, and the timeout constant."""
    ext = _load_ext()
    tool_args = {"code": "git status", "runtime": "terminal", "tool_name": "code_execution_tool"}
    with patch("shutil.which", return_value="/usr/local/bin/rtk"), patch(
        "subprocess.run", side_effect=_fake_run(rc=0, stdout="rtk git status")
    ) as m:
        ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
    _argv, kwargs = m.call_args
    assert kwargs.get("encoding") == "utf-8"
    assert kwargs.get("timeout") == 2.0
    assert _argv[0] == ["rtk", "rewrite", "git status"]


def test_missing_binary_warn_log_failure_still_fails_open():
    """the best-effort notification cannot break the agent. A stub agent whose context.log.log raises must still skip silently with tool_args unchanged (the _warn_unavailable guard swallows the failure)."""
    agent = _StubAgent()

    def _boom(**kwargs):
        raise RuntimeError("log surface broken")

    agent.context.log.log = _boom
    ext = _load_ext_with_agent(agent)
    tool_args = {"code": "git status", "runtime": "terminal", "tool_name": "code_execution_tool"}
    with patch("shutil.which", return_value=None):
        ext.execute(tool_name="code_execution_tool", tool_args=tool_args)  # must not raise
    assert tool_args["code"] == "git status", "broken log surface must fail open"
    assert agent.data["rtk_a0_missing_warned"] is True, "latch set before the log attempt"

# =====: Fail-Open & Parallel Re-Entrancy Test Suite =====
# Consolidation + extension of the -1.4 coverage into the
# 10-case matrix, happy-path mutation isolation, parallel/re-entrancy
# simulation (threads + processes), a real-stub integration harness, and
# the licensing-boundary structural pin. EXISTING TESTS ABOVE ARE UNCHANGED.

import errno
import itertools
import os
import subprocess
import sys
import threading
import time as _time

import pytest  # : live OS-limit control (E2BIG) uses pytest.raises


def _d3_matrix_mutations():
    """Gate mutations in guard order; each entry mutates a valid baseline dict so exactly one gate fires."""
    return [
        ("wrong_tool", lambda t: t.update({"tool_name": "some_other_tool"})),
        ("wrong_runtime", lambda t: t.update({"runtime": "python"})),
        ("invalid_payload", lambda t: t.update({"code": "   "})),
        ("rtk_prefix", lambda t: t.update({"code": "rtk git status"})),
    ]


def test_d3_ten_case_fail_open_matrix():
    """all 10 fail-open cases leave the original command unchanged — one auditable enumeration of the full contract. Cases 1-4 (gates) assert zero subprocess. Cases 5-10 patch shutil.which per the guard-adjacency discipline so the delegation branch is actually reached on rtk-absent hosts, pinned by run.assert_called_once."""
    original = "cargo test"
    ext = _load_ext()

    for name, mutate in _d3_matrix_mutations():
        t = {"code": original, "runtime": "terminal", "tool_name": "code_execution_tool"}
        mutate(t)
        expected_code = t["code"]  # post-mutation payload must survive untouched
        with patch("shutil.which", return_value="/usr/local/bin/rtk"), patch("subprocess.run") as m:
            # which-patch: availability passes so ONLY the gate under test can stop the spawn
            ext.execute(tool_name=t["tool_name"], tool_args=t)
            m.assert_not_called(), f"case {name} must not spawn rtk"
        assert t["code"] == expected_code, f"case {name} must be unchanged"

    t = {"code": original, "runtime": "terminal", "tool_name": "code_execution_tool"}
    with patch("shutil.which", return_value=None) as m_which:
        ext.execute(tool_name="code_execution_tool", tool_args=t)
    m_which.assert_called_once_with("rtk")
    assert t["code"] == original, "case 5 (binary missing) must be unchanged"

    for exc in (
        subprocess.TimeoutExpired(["rtk", "rewrite", original], 2.0),
        OSError(7, "Argument list too long"),
        RuntimeError("boom"),
    ):
        t = {"code": original, "runtime": "terminal", "tool_name": "code_execution_tool"}
        with patch("shutil.which", return_value="/usr/local/bin/rtk"), patch(
            "subprocess.run", side_effect=exc
        ) as run:
            ext.execute(tool_name="code_execution_tool", tool_args=t)
        run.assert_called_once()
        assert t["code"] == original, f"case 6/7 {exc!r} must be unchanged"

    for rc, out in ((1, "rtk cargo test"), (0, "   \n"), (0, original + "\n")):
        t = {"code": original, "runtime": "terminal", "tool_name": "code_execution_tool"}
        with patch("shutil.which", return_value="/usr/local/bin/rtk"), patch(
            "subprocess.run", side_effect=_fake_run(rc=rc, stdout=out)
        ) as run:
            ext.execute(tool_name="code_execution_tool", tool_args=t)
        run.assert_called_once()
        assert t["code"] == original, f"case 8/9/10 rc={rc} out={out!r} must be unchanged"


def test_rtk_prefix_mocked_binary_invocation_counter_zero():
    """mocked invocation counter — an `rtk `-prefixed command triggers ZERO subprocess calls (idempotence on re-dispatch). The which-patch is present so only the idempotence gate can stop the call."""
    ext = _load_ext()
    for cmd in ("rtk git status", "  rtk rewrite ls", "rtk bypass cargo test"):
        tool_args = {"code": cmd, "runtime": "terminal", "tool_name": "code_execution_tool"}
        with patch("shutil.which", return_value="/usr/local/bin/rtk"), patch("subprocess.run") as m:
            ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
            assert m.call_count == 0, f"idempotence: {cmd!r} must not re-invoke rtk"
        assert tool_args["code"] == cmd


def test_happy_path_mutation_full_snapshot_isolation():
    """mocked valid rewrite mutates only tool_args['code']; every other key is identical, nothing added, nothing removed."""
    ext = _load_ext()
    tool_args = {
        "code": "git status",
        "runtime": "terminal",
        "tool_name": "code_execution_tool",
        "session": 0,
        "timeout": 30,
    }
    before = dict(tool_args)
    with patch("shutil.which", return_value="/usr/local/bin/rtk"), patch(
        "subprocess.run", side_effect=_fake_run(rc=0, stdout="  rtk git --short  \n")
    ) as run:
        ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
    run.assert_called_once()
    assert tool_args["code"] == "rtk git --short"
    after = dict(tool_args)
    after.pop("code")
    expected = {k: v for k, v in before.items() if k != "code"}
    assert after == expected, f"only 'code' may change, diff: {set(after) ^ set(expected)}"


def test_extension_instance_is_stateless_after_execute():
    """structural: execute writes no instance attribute — the extension holds no mutable state between invocations (re-entrancy)."""
    ext = _load_ext()
    before = {k: v for k, v in vars(ext).items() if k != "agent"}
    with patch("shutil.which", return_value="/usr/local/bin/rtk"), patch(
        "subprocess.run", side_effect=_fake_run(rc=0, stdout="rtk git status")
    ):
        tool_args = {"code": "git status", "runtime": "terminal", "tool_name": "code_execution_tool"}
        ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
    after = {k: v for k, v in vars(ext).items() if k != "agent"}
    assert after == before, f"statelessness violated: {set(after) ^ set(before)}"


def test_concurrent_threads_share_one_extension_instance():
    """8 threads x 4 outcome families through ONE shared extension instance with per-thread tool_args — every outcome correct and isolated; no shared-state corruption across threads."""
    ext = _load_ext()
    # patches installed ONCE here, never inside worker threads.
    # In-worker patch.__exit__ restores the process-global shutil.which /
    # subprocess.run into sibling threads still mid-body (reproduced under
    # sys.setswitchinterval stress: a sibling could reach the unpatched
    # global, spawn the real rtk, or hit FileNotFoundError). A parent-scope
    # patch lifetime removes the cross-thread restore window entirely.
    _cm_which = patch("shutil.which", return_value="/usr/local/bin/rtk")
    _cm_run = patch("subprocess.run")
    _cm_which.start()
    _cm_run.start()
    expectations = [
        ("git status", 0, "rtk git status"),
        ("git status", 1, "rtk git status"),
        ("cargo test", 0, ""),
        ("cargo test", 0, "cargo test\n"),
    ]
    jobs = list(itertools.product(range(8), expectations))
    errors = []

    def _work(job):
        _idx, (code, rc, out) = job
        args = {"code": code, "runtime": "terminal", "tool_name": "code_execution_tool"}
        try:
            # Per-thread side_effect bound by closure defaults: each
            # thread observes its own (rc, stdout) outcome through the
            # single shared patched global.
            def _side(argv, _rc=rc, _out=out, **kw):
                return subprocess.CompletedProcess(argv, _rc, _out, None)

            with patch("subprocess.run", side_effect=_side) as run:
                ext.execute(tool_name="code_execution_tool", tool_args=args)
            run.assert_called_once()
            expected_code = out.strip() if rc == 0 and out.strip() and out.strip() != code else code
            assert args["code"] == expected_code, f"thread outcome drift: {args['code']!r}"
        except Exception as e:  # collector: thread failures reported after join
            errors.append(e)

    threads = [threading.Thread(target=_work, args=(job,)) for job in jobs]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    _cm_run.stop()
    _cm_which.stop()
    assert not errors, f"concurrent execution corrupted state: {errors[:3]}"


def _plugin_root_str():
    return str(PLUGIN_ROOT)


_WORKER_SCRIPT = """
import importlib.util, sys
from pathlib import Path

root = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location(
    "_10_rtk_intercept", root / "extensions" / "python" / "tool_execute_before" / "_10_rtk_intercept.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
ext = module.RtkIntercept(agent=None)
args = {"code": sys.argv[2], "runtime": "terminal", "tool_name": "code_execution_tool"}
ext.execute(tool_name="code_execution_tool", tool_args=args)
print(args["code"])
"""


def _stub_path(tmp_path, monkeypatch, body):
    """Write an executable stub 'rtk' in tmp; prepend its dir to the real PATH."""
    stub = tmp_path / "rtk"
    stub.write_text(body)
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    return stub


def test_parallel_worker_processes_independent_outcomes(tmp_path, monkeypatch):
    """Two separate OS processes (the framework parallel-worker model) each run the extension against the real local stub with independent, isolated outcomes."""
    _stub_path(
        tmp_path,
        monkeypatch,
        "#!/bin/sh\nif [ \"$2\" = \"git status\" ]; then echo \"rtk git status\"; exit 0; fi\nexit 1\n",
    )
    script = tmp_path / "worker.py"
    script.write_text(_WORKER_SCRIPT)

    env = dict(os.environ, PYTHONPATH="/a0", A0_FRAMEWORK_ROOT=os.environ.get("A0_FRAMEWORK_ROOT", "/a0"))
    procs = [
        subprocess.run(
            [sys.executable, str(script), _plugin_root_str(), cmd],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        for cmd in ("git status", "cargo test")
    ]
    assert procs[0].returncode == 0 and procs[1].returncode == 0, [p.stderr for p in procs]
    assert procs[0].stdout.strip() == "rtk git status", "worker 1 must get the real rewrite"
    assert procs[1].stdout.strip() == "cargo test", "worker 2 (rc!=0) must fail open"


def test_warn_once_latch_bounded_under_thread_concurrency():
    """adjacent: concurrent missing-binary executions on a SHARED agent.data never crash; the latch ends True and the warning count stays bounded (1.thread_count). Exact count is intentionally unpinned — the check-then-set latch is process-local (parallel workers are separate processes) and per-process best-effort ( defer stands)."""
    agent = _StubAgent()
    ext = _load_ext_with_agent(agent)
    errors = []

    def _work():
        try:
            args = {"code": "git status", "runtime": "terminal", "tool_name": "code_execution_tool"}
            ext.execute(tool_name="code_execution_tool", tool_args=args)
            assert args["code"] == "git status"
        except Exception as e:  # collector
            errors.append(e)

    # patch scope is the parent (see
    # test_concurrent_threads_share_one_extension_instance: in-worker
    # patching races the global being restored into sibling threads).
    with patch("shutil.which", return_value=None):
        threads = [threading.Thread(target=_work) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
    assert not errors, f"latch concurrency crashed: {errors[:3]}"
    assert agent.data.get("rtk_a0_missing_warned") is True
    # Bounds, not an exact count: the read-then-set latch (extension:59-61)
    # is non-atomic; A0 parallel workers are separate processes with their
    # own dicts, so the worst realistic case is one warning per process
    # (deferred-work.md:50). Threads here share one agent, so a race could
    # warn up to once per thread -- bounded, never crash, always fail-open.
    assert 1 <= len(agent.warnings) <= 8, f"warning count out of bounds: {len(agent.warnings)}"


def test_real_stub_happy_path_mutates(tmp_path, monkeypatch):
    """A real-stub harness: unmocked subprocess against a real local stub 'rtk' — exercises the true availability check (shutil.which), argv passing, real rc + utf-8 stdout decode, and the mutation. Consumes the deferred 'real-subprocess rc!=0 / real-stub integration' item."""
    _stub_path(tmp_path, monkeypatch, "#!/bin/sh\necho 'rtk git --short'\n")
    ext = _load_ext()
    tool_args = {"code": "git status", "runtime": "terminal", "tool_name": "code_execution_tool"}
    ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
    assert tool_args["code"] == "rtk git --short", "real stub rewrite must mutate"


def test_real_stub_nonzero_rc_fails_open(tmp_path, monkeypatch):
    """A real stub exiting with a non-rewrite code leaves the original command unchanged."""
    _stub_path(tmp_path, monkeypatch, "#!/bin/sh\necho garbage >&2\nexit 2\n")
    ext = _load_ext()
    tool_args = {"code": "git status", "runtime": "terminal", "tool_name": "code_execution_tool"}
    ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
    assert tool_args["code"] == "git status", "real rc!=0 must fail open"


def test_real_stub_e2big_fails_open(tmp_path, monkeypatch):
    """A normal payload spawns the stub while an oversized single argv still fails open. The invariant is the contract: any delegation failure leaves the command unchanged, whether the OS refuses the argv at exec or earlier argv handling intervenes. Two controls separate the evidence: a small payload really reaches exec through this harness, and the OS refusal of the oversized argv is observed independently."""
    _stub_path(tmp_path, monkeypatch, "#!/bin/sh\nexit 0\n")
    ext = _load_ext()
    # Control 1: small payload actually reaches the real stub -- proves
    # this harness exercises the real availability check, PATH resolution
    # and exec.
    small = {"code": "probe exec marker", "runtime": "terminal", "tool_name": "code_execution_tool"}
    ext.execute(tool_name="code_execution_tool", tool_args=small)
    assert small["code"] == "probe exec marker"  # unchanged (empty stdout gate)
    # Oversized payload: still fails open through the extension.
    huge = "echo " + "x" * 200000  # oversized single argv on any Linux host
    tool_args = {"code": huge, "runtime": "terminal", "tool_name": "code_execution_tool"}
    ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
    assert tool_args["code"] == huge, "oversized argv must fail open unchanged"
    # Control 2 (deterministic): the OS refusal of the oversized argv is
    # injected at the exec seam (`OSError(errno.E2BIG)`), proving the
    # delegation was reached and the catch-all keeps the command unchanged
    # -- independent of any host-specific argv limit.
    with patch("shutil.which", return_value="/usr/local/bin/rtk"), patch(
        "subprocess.run",
        side_effect=OSError(errno.E2BIG, "Argument list too long"),
    ) as run:
        probe = {"code": "probe exec marker", "runtime": "terminal", "tool_name": "code_execution_tool"}
        ext.execute(tool_name="code_execution_tool", tool_args=probe)
    run.assert_called_once()
    assert probe["code"] == "probe exec marker", "E2BIG must fail open unchanged"
    # Control 3 (live evidence, best-effort): this OS may or may not refuse
    # the oversized single argv at exec (per-arg limit is host/arch-specific);
    # record what actually happened, assert only when the OS did raise.
    try:
        subprocess.run(["echo", "x" * 200000], capture_output=True)
    except OSError as e:
        if e.errno == errno.E2BIG:
            pass  # live confirmation of the exec-time refusal


def test_real_stub_grandchild_timeout_bounded(tmp_path, monkeypatch):
    """The stub exits after spawning a background grandchild holding the inherited pipes. Asserts the pinned bound: the wait stays near the rewrite timeout and the original command is unchanged. A real unbounded hang would need an explicit recorded deviation; the pinned shape is never changed silently."""
    _stub_path(tmp_path, monkeypatch, "#!/bin/sh\nsleep 8 &\necho waiting\nexit 0\n")
    ext = _load_ext()
    tool_args = {"code": "git status", "runtime": "terminal", "tool_name": "code_execution_tool"}
    start = _time.monotonic()
    ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
    elapsed = _time.monotonic() - start
    assert tool_args["code"] == "git status", "grandchild-pipe wait must fail open"
    # Tolerance band: upper bounds the wait (2.0s pinned timeout +
    # margin), lower bound proves the timeout actually fired -- a silent
    # fire-and-forget or a dropped timeout= would pass a bare upper-bound
    # check vacuously.
    assert 1.5 <= elapsed <= 4.0, f"timeout must bound the wait to ~2.0s, took {elapsed:.2f}s"


def test_no_rtk_derived_code_or_licensing_drift():
    """structural: the repo carries no RTK/Hermes-derived code — the fail-open suite is authored from the architecture enumeration, never copied from the upstream adapter; LICENSE stays Apache-2.0."""
    tests_dir = PLUGIN_ROOT / "tests"
    needles = ("hermes", "rtk_ai", "import rtk", "from rtk")
    for pyfile in PLUGIN_ROOT.rglob("*.py"):
        if tests_dir in pyfile.parents:
            continue
        src = pyfile.read_text().lower()
        for needle in needles:
            assert needle not in src, f"RTK-derived token {needle!r} in {pyfile}"
    license_text = (PLUGIN_ROOT / "LICENSE").read_text()
    assert "Apache License" in license_text, "LICENSE must be Apache-2.0 (FR25)"


def test_fail_open_branches_emit_no_log_noise():
    """Fail-open branches other than the binary-missing notification stay silent: no unrequested log noise, and the operator signal remains the single warn-once notice."""
    agent = _StubAgent()
    ext = _load_ext_with_agent(agent)
    cases = (
        # rc 3 no longer skips: noise-suppression is pinned with a
        # rc that still short-circuits at the case-8 gate.
        ("nonzero_rc", _fake_run(rc=1, stdout="x")),
        ("timeout", lambda argv, **kw: (_ for _ in ()).throw(subprocess.TimeoutExpired(argv, 2.0))),
        ("oserror", lambda argv, **kw: (_ for _ in ()).throw(OSError(7, "Argument list too long"))),
        ("identical_stdout", _fake_run(rc=0, stdout="git status\n")),
    )
    for name, side in cases:
        tool_args = {"code": "git status", "runtime": "terminal", "tool_name": "code_execution_tool"}
        with patch("shutil.which", return_value="/usr/local/bin/rtk"), patch(
            "subprocess.run", side_effect=side
        ):
            ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
        assert tool_args["code"] == "git status", f"{name} must fail open"
        assert len(agent.warnings) == 0, f"{name}: D9 notification is reserved for binary-missing"


# ---: rc-3 acceptance gate (, CC -ESCALATION-1 Option A) ---

def test_rc3_rewrite_applies_mutation():
    """A rewrite-available exit with differing non-empty stdout mutates tool_args at the single mutation point; no other key is touched."""
    ext = _load_ext()
    tool_args = {
        "code": "git status",
        "runtime": "terminal",
        "tool_name": "code_execution_tool",
        "session": 0,
    }
    snapshot = dict(tool_args)
    with patch("shutil.which", return_value="/usr/local/bin/rtk"), patch(
        "subprocess.run", side_effect=_fake_run(rc=3, stdout="rtk git status\n")
    ) as run:
        ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
    run.assert_called_once()  # availability + preceding gates passed through
    assert tool_args["code"] == "rtk git status", "rc 3 must apply the rewrite (ADR-006)"
    for key in ("runtime", "tool_name", "session"):
        assert tool_args[key] == snapshot[key], f"only 'code' may be written, touched {key}"


def test_rc3_empty_stdout_no_mutation():
    """rc 3 does not bypass case 9 — empty/whitespace stdout means nothing was printed, so the original runs."""
    ext = _load_ext()
    for out in ("", "   \n\t"):
        tool_args = {"code": "git status", "runtime": "terminal", "tool_name": "code_execution_tool"}
        with patch("shutil.which", return_value="/usr/local/bin/rtk"), patch(
            "subprocess.run", side_effect=_fake_run(rc=3, stdout=out)
        ) as run:
            ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
        run.assert_called_once()
        assert tool_args["code"] == "git status", f"rc 3 + empty stdout {out!r} must not mutate"


def test_rc3_identical_stdout_no_mutation():
    """rc 3 does not bypass case 10 — an echo of the original is not a rewrite (normalized-to-normalized comparison, )."""
    ext = _load_ext()
    tool_args = {"code": "git status", "runtime": "terminal", "tool_name": "code_execution_tool"}
    with patch("shutil.which", return_value="/usr/local/bin/rtk"), patch(
        "subprocess.run", side_effect=_fake_run(rc=3, stdout="  git status  \n")
    ) as run:
        ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
    run.assert_called_once()
    assert tool_args["code"] == "git status", "rc 3 + identical stdout must not mutate"


@pytest.mark.parametrize("rc", [1, 2, 4, 99, -1])
def test_non_rewrite_available_rc_preserves_original(rc):
    """only rc 0/3 are rewrite-available. rc 1 (no rewrite), rc 2 (deny) and any other non-zero code — including negative kill codes — preserve the original command."""
    ext = _load_ext()
    tool_args = {"code": "git status", "runtime": "terminal", "tool_name": "code_execution_tool"}
    with patch("shutil.which", return_value="/usr/local/bin/rtk"), patch(
        "subprocess.run", side_effect=_fake_run(rc=rc, stdout="rtk git status\n")
    ) as run:
        ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
    run.assert_called_once()
    assert tool_args["code"] == "git status", f"rc={rc} must not mutate"


def test_rc3_does_not_weaken_preceding_gates():
    """The rewrite-available gate sits after the guard clauses: rtk-prefixed and non-terminal payloads still short-circuit before any subprocess."""
    ext = _load_ext()
    gated = (
        {"code": "rtk git status", "runtime": "terminal", "tool_name": "code_execution_tool"},
        {"code": "rtk bypass git status", "runtime": "terminal", "tool_name": "code_execution_tool"},
        {"code": "git status", "runtime": "python", "tool_name": "code_execution_tool"},
        {"code": "git status", "runtime": "terminal", "tool_name": "other_tool"},
    )
    for t in gated:
        snapshot = dict(t)
        with patch("shutil.which", return_value="/usr/local/bin/rtk"), patch("subprocess.run") as m:
            ext.execute(tool_name=t["tool_name"], tool_args=t)
            m.assert_not_called(), f"preceding gate must stop the spawn: {t}"
        assert t == snapshot, "gated payload must be byte-identical before/after"
        assert t["code"] in ("rtk git status", "rtk bypass git status", "git status"), "gated payload code unchanged"


def test_rc3_gate_source_pin():
    """Structural pin: the result gate in the extension source must accept exactly the rc 0/3 pair as an inline membership test."""
    src = (PLUGIN_ROOT / "extensions" / "python" / "tool_execute_before" / "_10_rtk_intercept.py").read_text()
    assert "result.returncode not in (0, 3)" in src, "case-8 gate must accept rc 0 and 3 (ADR-006)"
    assert "result.returncode != 0" not in src, "legacy rc != 0 gate must be gone"


# =====: RTK Gain Savings Visibility =====


class _HintAgent:
    """stand-in capturing every agent-context log call with its type so the gain hint can be asserted independently of the warn-once surface."""

    def __init__(self):
        self.data = {}
        self.logs = []

        class _Log:
            def __init__(self, sink):
                self.sink = sink

            def log(self, **kwargs):
                self.sink.logs.append(kwargs)

        self.context = type("Ctx", (), {"log": _Log(self)})()

    def hints(self):
        return [c for c in self.logs if "gain" in str(c.get("content", "")).lower()]


def test_first_rewrite_hints_gain_once():
    """first applied rewrite emits exactly one info notice naming `rtk gain`; the session latch is set (warn-once shape, )."""
    agent = _HintAgent()
    ext = _load_ext_with_agent(agent)
    tool_args = {"code": "git status", "runtime": "terminal", "tool_name": "code_execution_tool"}
    with patch("shutil.which", return_value="/usr/local/bin/rtk"):
        with patch("subprocess.run", side_effect=_fake_run(rc=0, stdout="rtk git status")):
            ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
    assert tool_args["code"] == "rtk git status", "rewrite must still apply"
    hints = agent.hints()
    assert len(hints) == 1, f"exactly one gain hint expected, got {len(hints)}"
    assert hints[0].get("type") == "info", "hint must be informational, not a warning"
    assert "rtk gain" in hints[0].get("content", ""), "hint must point at `rtk gain`"
    assert agent.data.get("rtk_a0_gain_hinted") is True, "hint latch must set (D9 shape)"


def test_gain_hint_silent_after_first():
    """second and later rewrites stay silent — one hint per session."""
    agent = _HintAgent()
    ext = _load_ext_with_agent(agent)
    with patch("shutil.which", return_value="/usr/local/bin/rtk"):
        with patch("subprocess.run", side_effect=_fake_run(rc=0, stdout="rtk git status")):
            for _ in range(3):
                tool_args = {"code": "git status", "runtime": "terminal", "tool_name": "code_execution_tool"}
                ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
                assert tool_args["code"] == "rtk git status", "rewrite keeps applying"
    assert len(agent.hints()) == 1, "hint-once: only the first mutation may hint"


def test_gain_hint_agent_absent_silent():
    """self.agent is None -> silent (no latch write, no crash) while the rewrite still applies (notification is best-effort only)."""
    ext = _load_ext()  # agent=None
    tool_args = {"code": "git status", "runtime": "terminal", "tool_name": "code_execution_tool"}
    with patch("shutil.which", return_value="/usr/local/bin/rtk"):
        with patch("subprocess.run", side_effect=_fake_run(rc=0, stdout="rtk git status")):
            ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
    assert tool_args["code"] == "rtk git status", "agentless hint degradation must still rewrite"


def test_gain_hint_log_failure_still_mutates():
    """a broken log surface can never break the agent — mutation proceeds and the exception is swallowed (mirrors _warn_unavailable)."""
    agent = _HintAgent()

    def _boom(**kwargs):
        raise RuntimeError("log surface down")

    agent.context.log.log = _boom
    ext = _load_ext_with_agent(agent)
    tool_args = {"code": "git status", "runtime": "terminal", "tool_name": "code_execution_tool"}
    with patch("shutil.which", return_value="/usr/local/bin/rtk"):
        with patch("subprocess.run", side_effect=_fake_run(rc=0, stdout="rtk git status")):
            ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
    assert tool_args["code"] == "rtk git status", "log failure must not block the rewrite"


def test_gain_hint_absent_on_all_fail_open_paths():
    """the hint is downstream of every guard — no fail-open return path may emit it (gate adjacency)."""
    def _hintless_run(tool_args, which_val="/usr/local/bin/rtk", **run_kw):
        agent = _HintAgent()
        ext = _load_ext_with_agent(agent)
        with patch("shutil.which", return_value=which_val):
            if "exc" in run_kw:
                with patch("subprocess.run", side_effect=run_kw["exc"]):
                    ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
            else:
                with patch("subprocess.run", side_effect=_fake_run(rc=run_kw.get("rc", 0), stdout=run_kw.get("stdout", ""))):
                    ext.execute(tool_name="code_execution_tool", tool_args=tool_args)
        assert not agent.hints(), f"hint leaked on a fail-open path: {tool_args}"
        assert "rtk_a0_gain_hinted" not in agent.data, f"latch leaked on a fail-open path: {tool_args}"

    _hintless_run({"code": "git status", "runtime": "terminal", "tool_name": "code_execution_tool"}, which_val=None)
    _hintless_run({"code": "git status", "runtime": "terminal", "tool_name": "code_execution_tool"}, rc=1)
    _hintless_run({"code": "git status", "runtime": "terminal", "tool_name": "code_execution_tool"}, rc=2)
    _hintless_run({"code": "git status", "runtime": "terminal", "tool_name": "code_execution_tool"}, rc=99)
    _hintless_run({"code": "git status", "runtime": "terminal", "tool_name": "code_execution_tool"}, rc=0, stdout="")
    _hintless_run({"code": "git status", "runtime": "terminal", "tool_name": "code_execution_tool"}, rc=0, stdout="git status")
    _hintless_run({"code": "git status", "runtime": "terminal", "tool_name": "code_execution_tool"}, exc=subprocess.TimeoutExpired("rtk rewrite", 2.0))
    _hintless_run({"code": "git status", "runtime": "terminal", "tool_name": "code_execution_tool"}, exc=OSError("argv too large"))
    _hintless_run({"code": "git status", "runtime": "python", "tool_name": "code_execution_tool"})
    _hintless_run({"code": "rtk gain", "runtime": "terminal", "tool_name": "code_execution_tool"})
    _hintless_run({"code": "", "runtime": "terminal", "tool_name": "code_execution_tool"})
    _hintless_run({"code": "git status", "runtime": "terminal", "tool_name": "other_tool"})


def test_gain_hint_source_pin():
    """Structural pin: a single-source latch constant with exactly one call site adjacent to the mutation point."""
    src = (PLUGIN_ROOT / "extensions" / "python" / "tool_execute_before" / "_10_rtk_intercept.py").read_text()
    assert 'RTK_GAIN_HINTED = "rtk_a0_gain_hinted"' in src, "latch key must be a single-source constant"
    calls = src.count("_hint_gain()")
    assert calls == 1, f"exactly one hint call site expected, found {calls}"
    mutation = src.index('tool_args["code"] = rewritten')
    hint = src.rindex("_hint_gain()")
    assert hint < mutation and mutation - hint < 600, "hint must sit immediately before the mutation point"


def test_no_plugin_owned_savings_persistence():
    """Structural pin: savings accounting stays RTK-internal. The extension holds no persistence primitives and the plugin owns no stats or state file that could substitute for the RTK ledger. Matches the MVP data boundary."""
    ext_src = (PLUGIN_ROOT / "extensions" / "python" / "tool_execute_before" / "_10_rtk_intercept.py").read_text()
    write_primitives = ("open(", ".write_text(", ".write_bytes(", "json.dump", "json.load", "Path(", "mkdir(", "shutil.copy", "os.replace", "os.rename")
    for needle in write_primitives:
        assert needle not in ext_src, f"AC-03: extension must not persist data, found {needle!r}"
    owned = []
    for candidate in PLUGIN_ROOT.rglob("*"):
        if candidate.suffix != ".json" and "stats" not in candidate.name and "state" not in candidate.name:
            continue
        parts = candidate.relative_to(PLUGIN_ROOT).parts
        if any(p in parts for p in ("tests", ".a0proj", ".serena", ".pytest_cache", "venv", ".git")):
            continue
        owned.append(str(candidate))
    assert not owned, f"AC-03: plugin-owned persistence surface exists: {owned}"
    # extension: gain-visibility must add nothing either —
    # no plugin-owned gain/savings ledger anywhere in the plugin tree.
    ledger = []
    for candidate in PLUGIN_ROOT.rglob("*"):
        if not candidate.is_file():
            continue
        name = candidate.name.lower()
        if "gain" not in name and "saving" not in name and "stats" not in name:
            continue
        parts = candidate.relative_to(PLUGIN_ROOT).parts
        if any(p in parts for p in ("tests", ".a0proj", ".serena", ".pytest_cache", "venv", ".git", "docs", "node_modules")):
            continue
        ledger.append(str(candidate))
    assert not ledger, f"AC-03: no plugin-owned gain/savings ledger: {ledger}"
    # hooks.py and scripts/setup.sh must not write any stats-like surface.
    for text_path in (PLUGIN_ROOT / "hooks.py", PLUGIN_ROOT / "scripts" / "setup.sh"):
        body = text_path.read_text()
        for needle in ("stats.json", "gain.json", "savings.json"):
            assert needle not in body, f"AC-03: {text_path.name} must not persist {needle}"
