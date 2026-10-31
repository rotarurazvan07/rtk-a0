#!/usr/bin/env python3
"""Pure-venv tests: boot-time RTK re-provisioning extension surface.

Network-free: the extension never touches subprocess and setup.sh is never
executed. The module imports only helpers.extension at module level (the same
minimal contract the CI framework shim provides); the hook dispatch import
stays lazy, so the module imports cleanly under that shim too.
"""
import ast
import importlib.util
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
BOOTSTRAP_PATH = PLUGIN_ROOT / "extensions" / "python" / "startup_migration" / "_10_rtk_bootstrap.py"

try:  # the real framework, when available
    import helpers.extension  # noqa: F401
    _HELPERS_STUBBED = False
except ImportError:  # minimal local shim, same shape as the CI shim
    _helpers = types.ModuleType("helpers")
    _helpers.__path__ = []
    _extension = types.ModuleType("helpers.extension")

    class _Extension:
        def __init__(self, agent=None):
            self.agent = agent

    _extension.Extension = _Extension
    _helpers.extension = _extension
    sys.modules["helpers"] = _helpers
    sys.modules["helpers.extension"] = _extension
    _HELPERS_STUBBED = True


def _load_bootstrap(name="rtk_a0_bootstrap", path=None):
    target = Path(path) if path else BOOTSTRAP_PATH
    spec = importlib.util.spec_from_file_location(name, target)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_plugins_module(calls):
    module = types.ModuleType("helpers.plugins")

    def call_plugin_hook(*args, **kwargs):
        calls.append((args, kwargs))

    module.call_plugin_hook = call_plugin_hook
    return module


def test_extension_exists_subclasses_extension():
    from helpers.extension import Extension

    mod = _load_bootstrap()
    assert hasattr(mod, "RtkBootReprovisioningMigration")
    assert issubclass(mod.RtkBootReprovisioningMigration, Extension)
    inst = mod.RtkBootReprovisioningMigration(agent=None)
    assert callable(getattr(inst, "execute", None))


def test_module_import_has_no_side_effects(monkeypatch):
    """Import must not probe, run setup, print, or start a thread."""
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        raise AssertionError("import-time setup invocation must never happen")

    monkeypatch.setattr("subprocess.run", fake_run)
    printed = []
    monkeypatch.setattr("builtins.print", lambda *a: printed.append(a))
    mod = _load_bootstrap()
    assert calls == [], calls
    assert printed == [], printed
    assert mod._startup_migration_thread is None


def test_execute_returns_immediately_non_blocking(monkeypatch):
    """execute() spawns a daemon thread and returns promptly: no synchronous
    setup or network call inside execute (boot is never blocked)."""
    mod = _load_bootstrap()
    seen = {}

    def fake_target():
        seen["thread"] = threading.current_thread()
        time.sleep(0.2)

    monkeypatch.setattr(mod, "_reprovision_boot", fake_target)
    inst = mod.RtkBootReprovisioningMigration(agent=None)
    t0 = time.time()
    inst.execute()
    elapsed = time.time() - t0
    assert elapsed < 0.1, f"execute blocked {elapsed:.2f}s"
    assert seen.get("thread") is not None, "thread target never ran"
    assert seen["thread"].daemon is True
    assert seen["thread"].name == "a0-rtk-boot-reprovisioning"
    seen["thread"].join(timeout=1.0)


def test_execute_spawns_single_thread_guard(monkeypatch):
    """Singleton plus liveness guard: a second execute() while the first
    thread is alive spawns nothing (one background task per boot)."""
    mod = _load_bootstrap()
    starts = {"n": 0}

    def slow_target():
        starts["n"] += 1
        time.sleep(0.3)

    monkeypatch.setattr(mod, "_reprovision_boot", slow_target)
    inst = mod.RtkBootReprovisioningMigration(agent=None)
    inst.execute()
    inst.execute()
    assert starts["n"] == 1, f"expected one thread start, got {starts['n']}"


def _copy_tree(tmp_path, name):
    tree = tmp_path / name
    (tree / "extensions" / "python" / "startup_migration").mkdir(parents=True)
    (tree / "plugin.yaml").write_text("name: " + name + "\n", encoding="utf-8")
    (tree / "hooks.py").write_text(
        "def ensure_provisioned(raise_on_error=False):\n    return None\n", encoding="utf-8"
    )
    copied = tree / "extensions" / "python" / "startup_migration" / "_10_rtk_bootstrap.py"
    copied.write_text(BOOTSTRAP_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    return copied


def test_resolve_plugin_dir_walks_up_name_agnostically(tmp_path, monkeypatch):
    """The plugin root is found by walking up to the directory holding
    plugin.yaml and hooks.py, under any install name; the resolved directory
    name is what the framework is asked to dispatch to."""
    for name in ("rtk_compression", "rtk_a0", "zz-arbitrary-plugin-dir"):
        copied = _copy_tree(tmp_path, name)
        mod = _load_bootstrap("bootstrap_" + name.replace("-", "_"), copied)
        calls = []
        monkeypatch.setitem(sys.modules, "helpers.plugins", _fake_plugins_module(calls))
        inst = mod.RtkBootReprovisioningMigration(agent=None)
        inst.execute()
        thread = mod._startup_migration_thread
        assert thread is not None
        thread.join(timeout=2.0)
        assert len(calls) == 1, calls
        args, kwargs = calls[0]
        assert args == (name, "ensure_provisioned"), args
        assert kwargs.get("raise_on_error") is False, kwargs


def test_resolve_plugin_dir_degrades_when_unresolvable(tmp_path, monkeypatch):
    """No plugin.yaml and hooks.py anywhere up the tree: no dispatch, no
    crash, the boot path just warns and returns."""
    tree = tmp_path / "deep" / "nested"
    (tree / "extensions" / "python" / "startup_migration").mkdir(parents=True)
    copied = tree / "extensions" / "python" / "startup_migration" / "_10_rtk_bootstrap.py"
    copied.write_text(BOOTSTRAP_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    mod = _load_bootstrap("bootstrap_unresolved", copied)
    calls = []
    monkeypatch.setitem(sys.modules, "helpers.plugins", _fake_plugins_module(calls))
    inst = mod.RtkBootReprovisioningMigration(agent=None)
    inst.execute()
    thread = mod._startup_migration_thread
    assert thread is not None
    thread.join(timeout=2.0)
    assert calls == [], calls


def test_dispatch_failure_degrades_to_warning(tmp_path, monkeypatch):
    """A raising dispatch degrades to a warning: the background task never
    propagates anything to the boot path."""
    mod = _load_bootstrap()
    calls = []

    def boom(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError("dispatch exploded")

    module = types.ModuleType("helpers.plugins")
    module.call_plugin_hook = boom
    monkeypatch.setitem(sys.modules, "helpers.plugins", module)
    warnings = []
    monkeypatch.setattr(mod, "_warn", lambda msg: warnings.append(msg))
    mod._reprovision_boot()
    assert calls, "dispatch was never attempted"
    assert warnings, "a failed dispatch must warn"


def test_no_hardcoded_plugin_directory_name():
    """Structural pin: no literal plugin directory name and no fixed
    parents[] depth survive in the source (installed-name agnosticism)."""
    code = BOOTSTRAP_PATH.read_text(encoding="utf-8")
    for banned in ("rtk_a0", "rtk_compression", "parents[2]", "parents[0]"):
        assert banned not in code, f"hardcoded name or depth found: {banned}"


def test_module_level_imports_are_limited_to_the_shim_contract():
    """The CI shim provides only helpers.extension and helpers.modules, so a
    module-level import of any other framework module breaks CI."""
    tree = ast.parse(BOOTSTRAP_PATH.read_text(encoding="utf-8"))
    allowed = {"__future__", "sys", "threading", "pathlib", "helpers.extension"}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name in allowed, f"module-level import not shim-safe: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert module in allowed, f"module-level import not shim-safe: {module}"
            assert node.level == 0, "relative module-level import is not shim-safe"


def test_hook_dispatch_import_is_lazy():
    """The framework dispatch import happens inside the task body, never at
    module import time."""
    tree = ast.parse(BOOTSTRAP_PATH.read_text(encoding="utf-8"))
    body_nodes = set(tree.body)
    lazy = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "") == "helpers.plugins"
    ]
    assert lazy, "dispatch import not found"
    for node in lazy:
        assert node not in body_nodes, "dispatch import must not be module level"


def test_extension_imports_cleanly_under_a_minimal_framework_shim(tmp_path):
    """The CI shim exposes only helpers.extension and helpers.modules: the
    extension module must import under exactly that surface."""
    shim = tmp_path / "framework"
    (shim / "helpers").mkdir(parents=True)
    (shim / "helpers" / "__init__.py").write_text("", encoding="utf-8")
    (shim / "helpers" / "extension.py").write_text(
        "class Extension:\n"
        "    def __init__(self, agent=None):\n"
        "        self.agent = agent\n",
        encoding="utf-8",
    )
    (shim / "helpers" / "modules.py").write_text(
        "def load_classes_from_folder(folder, _pattern, base_class):\n"
        "    return []\n",
        encoding="utf-8",
    )
    script = (
        "import importlib.util\n"
        "spec = importlib.util.spec_from_file_location('boot', %r)\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "print('IMPORT_OK')\n" % str(BOOTSTRAP_PATH)
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(shim)},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "IMPORT_OK" in proc.stdout, proc.stdout + proc.stderr


def test_path_load_and_second_load_expose_identical_code():
    """The framework loads extension modules by file path; two loads must
    expose the same implementation (byte-identical code objects)."""
    first = _load_bootstrap("boot_first")
    second = _load_bootstrap("boot_second")
    assert (
        first.RtkBootReprovisioningMigration.execute.__code__.co_code
        == second.RtkBootReprovisioningMigration.execute.__code__.co_code
    )
    assert first._THREAD_NAME == second._THREAD_NAME


def test_dispatch_import_unavailable_warns_and_returns(tmp_path, monkeypatch):
    """Fault branch: when the framework dispatch module cannot be imported the
    background task warns and returns; startup is never broken."""
    mod = _load_bootstrap()
    monkeypatch.setitem(sys.modules, "helpers.plugins", None)
    warnings = []
    monkeypatch.setattr(mod, "_warn", lambda msg: warnings.append(msg))
    mod._reprovision_boot()
    assert warnings, "an unavailable dispatch must warn"


def test_unresolvable_plugin_root_warns(tmp_path, monkeypatch):
    """Fault branch: no plugin root found -> the task warns instead of
    silently doing nothing."""
    tree = tmp_path / "deep" / "nested"
    (tree / "extensions" / "python" / "startup_migration").mkdir(parents=True)
    copied = tree / "extensions" / "python" / "startup_migration" / "_10_rtk_bootstrap.py"
    copied.write_text(BOOTSTRAP_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    mod = _load_bootstrap("bootstrap_warn_unresolved", copied)
    calls = []
    monkeypatch.setitem(sys.modules, "helpers.plugins", _fake_plugins_module(calls))
    warnings = []
    monkeypatch.setattr(mod, "_warn", lambda msg: warnings.append(msg))
    mod._reprovision_boot()
    assert calls == [], calls
    assert warnings, "an unresolvable plugin root must warn"


def test_warning_sink_failure_is_swallowed(monkeypatch):
    """Fault branch: a broken stderr must not turn the warning into a raise
    (the boot path can never break startup)."""
    mod = _load_bootstrap()

    class _Broken:
        def write(self, *args, **kwargs):
            raise ValueError("closed file")

        def flush(self):
            pass

    monkeypatch.setattr(mod.sys, "stderr", _Broken())
    assert mod._warn("broken sink") is None


def test_resolution_fault_warns_and_returns(tmp_path, monkeypatch):
    """Fault branch (review-added): a filesystem fault inside plugin-root
    resolution (permission change, plugin dir deleted mid-boot) must degrade
    to a warning — never escape the background target."""
    mod = _load_bootstrap()
    calls = []
    monkeypatch.setitem(sys.modules, "helpers.plugins", _fake_plugins_module(calls))

    def boom():
        raise PermissionError("plugin tree unreadable")

    monkeypatch.setattr(mod, "_resolve_plugin_dir", boom)
    warnings = []
    monkeypatch.setattr(mod, "_warn", lambda msg: warnings.append(msg))
    mod._reprovision_boot()  # must not raise
    assert calls == [], calls
    assert warnings and "resolution failed" in warnings[0], warnings


def test_execute_singleton_check_runs_under_spawn_lock():
    """Structural pin (review-added): the liveness check and thread spawn in
    execute() run under the module spawn lock, so concurrent execute() calls
    (boot + reload) can never spawn two live reprovisioning threads (TOCTOU)."""
    tree = ast.parse(BOOTSTRAP_PATH.read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "RtkBootReprovisioningMigration")
    fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "execute")
    with_locks = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.With)
        and any(isinstance(i.context_expr, ast.Name) and i.context_expr.id == "_SPAWN_LOCK" for i in n.items)
    ]
    assert with_locks, "execute() must hold _SPAWN_LOCK around the singleton check and spawn"
    lock_body = with_locks[0]
    has_check = any(isinstance(n, ast.If) and "is_alive" in ast.dump(n.test) for n in ast.walk(lock_body))
    has_spawn = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "Thread" for n in ast.walk(lock_body))
    assert has_check and has_spawn, "lock must cover both the liveness check and the Thread spawn"
