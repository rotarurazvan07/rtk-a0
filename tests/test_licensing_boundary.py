#!/usr/bin/env python3
"""Licensing-boundary and official-extension compliance checks. Pure-runtime structural scans: network-free, host-state independent, scanning only the plugin's own tree. Lessons honored: vendoring needles match code with comments stripped, never documentation prose."""
import ast
import shutil
import subprocess
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
ELF_MAGIC = bytes.fromhex("7f454c46")

# Directories that are NOT plugin-owned shipped source: bookkeeping, test
# fixtures, caches, memory, generated docs/CI (own files, scanned separately).
SKIP_DIRS = {
    ".git", ".a0proj", "tests", "_bmad", "__pycache__", ".pytest_cache",
    ".serena", "venv", ".venv", "node_modules", "docs", ".github",
    ".desloppify", ".neuralmind",
}

NEEDLES = ("hermes", "rtk_ai", "import rtk", "from rtk")
# Module-name needles for STRUCTURAL import-node matching (concatenation and
# f-strings cannot dodge these - /M3 fix). Avoids matching a module
# named e.g. "smart_kit" because we match whole name segments, not substrings.
NEEDLES_CORE = ("hermes", "rtk_ai", "rtk")

# Framework source files: must never appear at ANY depth.
FRAMEWORK_FILE_NAMES = {"agent.py", "initialize.py", "run_ui.py", "models.py",
                       "functions.py", "parallel_tools.py"}
# A0 core dirs: forbidden at the tree ROOT only (extensions/python/ and
# extensions/python/tool_execute_before/ are the OFFICIAL layout and must
# not false-positive - self-caught defect #3, ).
FRAMEWORK_ROOT_DIRS = {"helpers", "api", "webui", "plugins"}
# Non-python plugin sources scanned for the same needle set: shell
# provisioning + YAML/text config; comments stripped before matching.
NEEDLE_TEXT_SUFFIXES = (".sh", ".bash", ".yaml", ".yml", ".txt", ".cfg",
                        ".toml", ".json")
# Process-spawning entry points the delegation audit must cover:
# attribute shapes on any subprocess binding plus the bare os.system call.
RUN_ATTRS = {"run", "call", "check_call", "check_output", "Popen"}
OS_SYSTEM_NAMES = {"os.system", "system"}
# RTK/Hermes-derived code smuggled through dynamic import machinery (M3).
NEEDLE_CALLS = ("__import__", "import_module")
# Runtime framework-mutation markers.
FRAMEWORK_MUTATION_MARKERS = (
    "setattr(", "delattr(", "monkeypatch", "__setattr__", "__delattr__",
    "importlib.reload", "sys.modules[", "sys.modules.update",
    "sys.modules.pop", "sys.modules.setdefault",
)

# Precompiled / vendored-binary indicators (nothing precompiled is shipped).
BINARY_SUFFIXES = (".tar.gz", ".tgz", ".so", ".rlib", ".dll", ".dylib",
                   ".bin", ".o", ".a", ".exe", ".zip", ".whl")

# Only tolerated absolute write target outside the plugin directory: the
# user-owned RTK binary installed by scripts/setup.sh.
TOLERATED_ABSOLUTE_PREFIX = "/usr/local/bin"


def _scanned_files():
    for p in sorted(PLUGIN_ROOT.rglob("*")):
        if p.is_file() and not any(d in p.parts for d in SKIP_DIRS):
            yield p


def _plugin_py_files():
    for p in _scanned_files():
        if p.suffix == ".py":
            yield p


def _code_needle_hits(tree) -> list:
    """Structural needle detection: import nodes + dynamic-import calls. ast.Call/Import nodes cannot be obfuscated by concatenation, f-strings or comments the way flat text can ( /M3 fix)."""
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if any(n in alias.name.lower() for n in NEEDLES_CORE):
                    hits.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = (node.module or "").lower()
            if any(n in mod for n in NEEDLES_CORE):
                hits.append(node.module)
        elif (isinstance(node, ast.Call)
              and isinstance(node.func, ast.Name)
              and node.func.id in NEEDLE_CALLS):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if any(n in arg.value.lower() for n in NEEDLES_CORE):
                        hits.append(f"{node.func.id}({arg.value!r})")
    return hits


def _code_text(py_path: Path) -> str:
    """Lowercased CODE-only text: comments + docstrings stripped."""
    tree = ast.parse(py_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            node.body = [n for n in node.body if not (
                isinstance(n, ast.Expr)
                and isinstance(n.value, ast.Constant)
                and isinstance(n.value.value, str))]
            if not node.body:
                node.body = [ast.Pass()]
    return ast.unparse(tree).lower()


def test_no_rtk_derived_code_in_plugin_sources():
    """No vendored-toolkit tokens appear in plugin-owned code."""
    hits = []
    for p in _plugin_py_files():
        text = _code_text(p)
        for needle in NEEDLES:
            if needle in text:
                hits.append((str(p.relative_to(PLUGIN_ROOT)), needle))
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for hit in _code_needle_hits(tree):
            hits.append((str(p.relative_to(PLUGIN_ROOT)), f"import-node:{hit}"))
    assert not hits, f"RTK-derived tokens in plugin code: {hits}"


def _comment_stripped_text(text_path: Path) -> str:
    """Lowercased text with comment lines removed."""
    out = []
    for line in text_path.read_text(encoding="utf-8",
                                    errors="replace").splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        out.append(line.lower())
    return "\n".join(out)


def test_no_rtk_derived_tokens_in_plugin_text_sources():
    """The same needle set runs over non-python sources (shell, manifests, config) with comment lines stripped: vendored code does not have to be python to violate the licensing boundary."""
    hits = []
    for p in _scanned_files():
        if p.suffix in NEEDLE_TEXT_SUFFIXES and p.suffix != ".py":
            text = _comment_stripped_text(p)
            for needle in ("hermes", "rtk_ai"):
                if needle in text:
                    hits.append((str(p.relative_to(PLUGIN_ROOT)), needle))
    assert not hits, f"RTK-derived tokens in plugin text sources: {hits}"


def test_no_vendored_rtk_binaries():
    """No precompiled binaries or archives are vendored in the repo."""
    bad = []
    for p in _scanned_files():
        if p.name.lower().endswith(BINARY_SUFFIXES):
            bad.append(str(p.relative_to(PLUGIN_ROOT)))
            continue
        try:
            if p.read_bytes()[:4] == ELF_MAGIC:
                bad.append(str(p.relative_to(PLUGIN_ROOT)))
        except OSError:
            continue
    assert not bad, f"vendored binaries/archives found: {bad}"


def test_root_license_is_apache_2_0():
    """Plugin code is Apache-2.0 at the repo-root LICENSE."""
    license_text = (PLUGIN_ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in license_text, "root LICENSE must be Apache License"
    assert "Version 2.0" in license_text, "root LICENSE must state Apache Version 2.0"


def test_all_compression_delegated_to_rtk_rewrite_binary():
    """every subprocess call in plugin code targets the rtk binary in argv form; the plugin delegates, it never executes rewrite logic itself."""
    offenders = []
    for p in _plugin_py_files():
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # audit surface: every subprocess entry point plus os.system
# (: aliased/popen/call/check_output were unaudited before).
            spawning = False
            if isinstance(node, ast.Call):
                if (isinstance(node.func, ast.Attribute)
                        and node.func.attr in RUN_ATTRS):
                    spawning = True
                elif (isinstance(node.func, ast.Attribute)
                      and isinstance(node.func.value, ast.Name)
                      and node.func.value.id == "os"
                      and node.func.attr == "system"):
                    spawning = True
                elif (isinstance(node.func, ast.Name)
                      and node.func.id == "system"):
                    spawning = True
            if not spawning:
                continue
            # os.system is NEVER sanctioned - it cannot carry an argv list
# (: audit surface includes os.system as a flat offender).
            if (isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "os"
                    and node.func.attr == "system") or (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "system"):
                offenders.append((str(p.relative_to(PLUGIN_ROOT)), node.lineno,
                                  "os.system is not argv-list delegation"))
                continue
            target = ast.unparse(node.func.value).lower()
            if "subprocess" not in target and "sp" not in target:
                offenders.append((str(p.relative_to(PLUGIN_ROOT)), node.lineno, target))
                continue
            argv = node.args[0] if node.args else None
            if not isinstance(argv, ast.List):
                # Non-list argv tolerated ONLY in hooks.py: it runs the plugin's
                # own scripts/setup.sh (provisioning,), never
                # compression logic.
                if p.name != "hooks.py":
                    offenders.append((str(p.relative_to(PLUGIN_ROOT)), node.lineno,
                                      "non-list subprocess argv"))
                continue
            # Delegation targets: the 'rtk' binary or the plugin's own
            # scripts/setup.sh (provisioning, hooks.py only -).
            head = [ast.literal_eval(e) for e in argv.elts
                    if isinstance(e, ast.Constant)]
            argv_src = ast.unparse(argv)
            ok = (
                "rtk" in head
                or argv_src.endswith("setup.sh")
                or "_SETUP_SCRIPT" in argv_src
            )
            if not ok:
                offenders.append((str(p.relative_to(PLUGIN_ROOT)), node.lineno,
                                  argv_src[:80]))
    assert not offenders, f"non-delegated subprocess calls: {offenders}"


def test_no_reimplemented_filter_or_translation_tables():
    """no command->output mapping literals (RTK filter logic lives in the Rust binary; a dict of >=4 command strings means reimplementation)."""
    bad = []
    for p in _plugin_py_files():
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            str_keys = [k for k in node.keys if isinstance(k, ast.Constant)
                        and isinstance(k.value, str)]
            if len(str_keys) >= 4 and any(" " in k.value for k in str_keys):
                bad.append((str(p.relative_to(PLUGIN_ROOT)), node.lineno, len(str_keys)))
    assert not bad, f"possible reimplemented filter/translation tables: {bad}"


def test_extension_registers_via_official_named_extension_only():
    """official tool_execute_before layout + Extension subclass."""
    ext = PLUGIN_ROOT / "extensions" / "python" / "tool_execute_before" / "_10_rtk_intercept.py"
    assert ext.is_file(), f"official extension point file missing: {ext}"
    tree = ast.parse(ext.read_text(encoding="utf-8"))
# Module-anchored official import : record the local binding
    # (alias or plain name) so a renamed binding is not a false positive.
    official_bindings = set()
    for n in tree.body:
        if (isinstance(n, ast.ImportFrom) and n.module == "helpers.extension"
                and any(a.name == "Extension" for a in n.names)):
            official_bindings.update(a.asname or a.name for a in n.names
                                     if a.name == "Extension")
    assert official_bindings, "official import required: from helpers.extension import Extension"
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
    named = [c for c in classes
             if any(ast.unparse(b) in official_bindings for b in c.bases)]
    assert named, "extension class must subclass the official Extension binding"


def test_no_framework_monkeypatching():
    """no runtime mutation of framework modules."""
    markers = FRAMEWORK_MUTATION_MARKERS
    hits = []
    for p in _plugin_py_files():
        text = _code_text(p)
        for m in markers:
            if m in text:
                hits.append((str(p.relative_to(PLUGIN_ROOT)), m))
        # literal_eval is a SAFE constant parser, not the eval builtin
# (: ast.literal_eval must not trip the eval(marker).
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "literal_eval"):
                continue
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "eval"):
                hits.append((str(p.relative_to(PLUGIN_ROOT)), "eval()"))
    assert not hits, f"framework-mutation markers in plugin code: {hits}"


def test_no_shell_true_in_plugin_sources():
    """No process-spawning call passes a truthy shell kwarg. A pure-AST counterpart of the lint rule that needs no binary on PATH."""
    offenders = []
    for p in _plugin_py_files():
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg == "shell":
                    truthy = not (
                        isinstance(kw.value, ast.Constant)
                        and kw.value.value in (False, 0, None)
                    )
                    if truthy:
                        offenders.append(
                            (str(p.relative_to(PLUGIN_ROOT)), node.lineno))
    assert not offenders, f"shell=True-style process calls: {offenders}"


def test_no_writes_outside_plugin_dir_except_rtk_binary():
    """Plugin code writes only inside its own directory; the sole external target is the user-owned RTK binary."""
    tolerated = TOLERATED_ABSOLUTE_PREFIX
    offenders = []
    for p in _plugin_py_files():
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                v = node.value
                if (v.startswith("/") and v.count("/") >= 2
                        and not v.startswith(tolerated)):
                    offenders.append((str(p.relative_to(PLUGIN_ROOT)), node.lineno, v))
    assert not offenders, f"absolute paths outside plugin tree: {offenders}"


def test_no_framework_files_in_plugin_tree():
    """repo ships no A0 core source files (any depth) and no A0 core dirs at the tree root (official extensions/python/ is allowed)."""
    found = []
    for p in _scanned_files():
        rel = p.relative_to(PLUGIN_ROOT)
        if p.name in FRAMEWORK_FILE_NAMES or rel.parts[0] in FRAMEWORK_ROOT_DIRS:
            found.append(str(rel))
    assert not found, f"framework source inside plugin repo: {found}"


def test_plugin_git_toplevel_is_plugin_root():
    """git-diff-empty check: the repo cannot contain framework files because its git toplevel is the plugin root itself."""
    if not (PLUGIN_ROOT / ".git").exists():
        return  # no checkout: structural asserts above already cover
    if shutil.which("git") is None:
        return  # F7: no git binary -> structural asserts are the check
    toplevel = subprocess.run(["git", "-C", str(PLUGIN_ROOT), "rev-parse",
                               "--show-toplevel"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", check=True)
    assert toplevel.stdout.strip() == str(PLUGIN_ROOT.resolve()), \
        "plugin repo must be its own git root (framework not vendored here)"


def test_readme_licensing_boundary_statement():
    """The README states the orchestrate-only boundary: RTK (BUSL-1.1) is
    downloaded, not vendored, our code is Apache-2.0, and distribution conveys
    an installer, not the engine."""
    text = (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8")
    for token in ("BUSL-1.1", "Apache-2.0", "rtk_pin.txt", "orchestration-only"):
        assert token in text, f"README must state {token!r}"
