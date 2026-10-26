#!/usr/bin/env python3
"""Story 4.1 tests: slash command + page shell + zero-load invariant (wiring only).

Pure runtime. No framework imports, no network, no rtk invocation. The actual
endpoint arrives in the next story; this suite pins the wiring contract and the
zero-load invariant so the surface cannot silently grow background load.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
COMMANDS_DIR = PLUGIN_ROOT / "commands"
WEBUI_DIR = PLUGIN_ROOT / "webui"
COMMAND_YAML = COMMANDS_DIR / "rtk-gain.command.yaml"
HANDLER_PY = COMMANDS_DIR / "rtk_gain_command.py"
MAIN_HTML = WEBUI_DIR / "main.html"
STORE_JS = WEBUI_DIR / "rtk-gain-store.js"
PAGE_URL = "/plugins/rtk_a0/webui/main.html"


def _load_handler():
    """Load the command handler the same way the A0 loader does (runpy.run_path)."""
    import runpy

    return runpy.run_path(str(HANDLER_PY))


def _handler_source() -> str:
    return HANDLER_PY.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# AC-01: command discoverability contract (schema mirrors _commands exemplars)
# ---------------------------------------------------------------------------

def test_command_yaml_schema_matches_exemplar():
    """commands/rtk-gain.command.yaml: exact 4-key schema, values pinned."""
    data = yaml.safe_load(COMMAND_YAML.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "command yaml must be a mapping"
    assert set(data.keys()) == {"name", "description", "type", "script_path"}, (
        f"schema must be exactly name/description/type/script_path, got {sorted(data.keys())}"
    )
    assert data["name"] == "rtk-gain", f"name mismatch: {data.get('name')!r}"
    assert data["type"] == "script", f"type must be script, got {data.get('type')!r}"
    assert data["script_path"] == "rtk_gain_command.py", (
        f"script_path must name the sibling handler, got {data.get('script_path')!r}"
    )


def test_command_description_names_the_savings_view():
    """description is user-facing copy naming the savings view (palette-visible)."""
    data = yaml.safe_load(COMMAND_YAML.read_text(encoding="utf-8"))
    desc = str(data.get("description", ""))
    assert desc.strip(), "description must not be empty"
    lowered = desc.lower()
    assert "saving" in lowered or "rtk gain" in lowered, (
        f"description must name the savings view, got {desc!r}"
    )


# ---------------------------------------------------------------------------
# AC-02: handler returns exactly one open_modal effect and nothing else
# ---------------------------------------------------------------------------

def test_handler_returns_exactly_one_open_modal_effect():
    """run(payload) -> {'text': '', 'effects': [one open_modal effect]} (loader contract)."""
    module = _load_handler()
    run = module.get("run")
    assert callable(run), "handler must expose callable run(payload)"
    result = run({})
    assert isinstance(result, dict), f"run must return a dict, got {type(result)!r}"
    effects = result.get("effects")
    assert isinstance(effects, list), f"result must carry an effects list, got {effects!r}"
    assert len(effects) == 1, f"exactly one effect expected, got {len(effects)}"
    effect = effects[0]
    assert effect.get("type") == "open_modal", f"effect type must be open_modal, got {effect!r}"
    assert effect.get("path") == PAGE_URL, f"effect path must be {PAGE_URL}, got {effect!r}"


def test_handler_is_stateless_and_idempotent():
    """Repeated calls return identical results: no state, no payload dependence."""
    module = _load_handler()
    run = module["run"]
    first = run({})
    second = run({"invocation": {"command_name": "rtk-gain", "arguments": {}}})
    assert first == second, "handler must not depend on payload or retain state"


def test_handler_source_has_no_side_effect_surface():
    """Handler is wiring only: no subprocess, no os/system, no file writes (AC-02)."""
    source = _handler_source()
    # the page URL legitimately contains the plugin name; ban the invocation
    # surface, not the substring (the page path is the handler's contract)
    for needle in (
        "subprocess", "Popen", "os.system", "os.popen", "shutil.which",
        "rtk gain", "rtk rewrite", "import os",
    ):
        assert needle not in source, f"handler must not reference {needle!r}: found {needle!r}"
    assert "open(" not in source, "handler must not open files"


# ---------------------------------------------------------------------------
# AC-03: page shell anchors (html + store registration contract)
# ---------------------------------------------------------------------------

def test_main_html_has_xinit_xdestroy_and_store_script():
    """Page mounts the store: x-init -> onOpen, x-destroy -> cleanup, store script tag."""
    html = MAIN_HTML.read_text(encoding="utf-8")
    assert 'x-init' in html, "page must declare x-init"
    assert 'x-destroy' in html, "page must declare x-destroy"
    assert "rtk-gain-store.js" in html, "page must load the Alpine store"
    assert "onOpen" in html, "x-init must call the store onOpen handler"
    assert "cleanup" in html, "x-destroy must call the store cleanup"


def test_store_exposes_transition_api_for_node_harness():
    """Store file defines createRtkGainStore factory + Alpine registration."""
    js = STORE_JS.read_text(encoding="utf-8")
    assert "createRtkGainStore" in js, "store must define a testable factory"
    assert "Alpine.store" in js, "store must register with Alpine"


# ---------------------------------------------------------------------------
# AC-03 behavioral: status machine + lifecycle (node harness drives real JS)
# ---------------------------------------------------------------------------

_HARNESS_JS = r"""
const fs = require('fs');
const vm = require('vm');
const storeFile = process.argv[2];
const scenario = process.argv[3];
let registered = null;
const sandbox = {
  console,
  window: {},
  Alpine: { store: (name, factory) => { registered = { name, factory }; } },
  // fetch stub: scenarios set __nextResponse before awaiting store.onOpen();
  // undefined default throws inside the store (network-fail path coverage)
  fetch: async () => {
    sandbox.__fetchCalls = (sandbox.__fetchCalls || 0) + 1;
    if (!sandbox.__nextResponse) { throw new Error('no stubbed response'); }
    return sandbox.__nextResponse;
  },
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(storeFile, 'utf8'), sandbox);
const factory = sandbox.createRtkGainStore || (registered && registered.factory);
if (!factory) { console.log(JSON.stringify({ error: 'store factory not found' })); process.exit(0); }
const store = factory();
const log = [];
const events = [];
function out(extra) { console.log(JSON.stringify(Object.assign({ name: registered.name, status: store.status, events: store.events }, extra))); }
(async () => {
if (scenario === 'transition-ready') {
  log.push(store.status);
  store.onOpen();
  log.push(store.status);
  store.complete();
  log.push(store.status);
  out({ log });
} else if (scenario === 'transition-error') {
  store.onOpen();
  store.fail('boom');
  out({ log: [store.status] });
} else if (scenario === 'one-open-per-mount') {
  store.onOpen();
  store.onOpen();
  store.onOpen();
  out({ log: [store.status] });
} else if (scenario === 'cleanup-resets') {
  store.onOpen();
  store.complete();
  store.cleanup();
  const afterCleanup = store.status;
  store.onOpen();
  out({ log: [afterCleanup, store.status] });
} else if (scenario === 'no-refetch-on-unrelated-change') {
  store.onOpen();
  const before = JSON.stringify(store.events || []);
  store.note = 'unrelated-state-change';
  const after = JSON.stringify(store.events || []);
  out({ log: [before === after] });
} else if (scenario === 'open-renders-json') {
  sandbox.__nextResponse = { ok: true, json: async () => ({ ok: true, rtk_available: true, message: 'Tokens saved: 225.2K (30.9%)', error: '', summary: { total_commands: 182, total_input: 4203109, total_output: 1512301, total_saved: 225200, avg_savings_pct: 30.94, total_time_ms: 151843, avg_time_ms: 834.3, oversized_excluded: 2 }, source: 'json' }) };
  await store.onOpen();
  out({ view: typeof store.renderView === 'function' ? store.renderView() : null, message: store.message });
} else if (scenario === 'open-renders-empty-ledger') {
  sandbox.__nextResponse = { ok: true, json: async () => ({ ok: true, rtk_available: true, message: 'Tokens saved: 0 (0.0%)', error: '', summary: { total_commands: 0, total_input: 0, total_output: 0, total_saved: 0, avg_savings_pct: 0, total_time_ms: 0, avg_time_ms: 0, oversized_excluded: 0 }, source: 'json' }) };
  await store.onOpen();
  out({ view: typeof store.renderView === 'function' ? store.renderView() : null });
} else if (scenario === 'open-renders-text-fallback') {
  sandbox.__nextResponse = { ok: true, json: async () => ({ ok: true, rtk_available: true, message: 'Tokens saved: 225.2K (30.9%)', error: '', summary: { total_commands: 182, headline: 'Tokens saved: 225.2K (30.9%)', saved_text: '225.2K', savings_pct_text: 30.9, input_text: '4.2M', output_text: '1.5M', exec_time_text: '2m 31s', oversized_excluded: 2 }, source: 'text' }) };
  await store.onOpen();
  out({ view: typeof store.renderView === 'function' ? store.renderView() : null });
} else if (scenario === 'open-renders-notice-missing-rtk') {
  sandbox.__nextResponse = { ok: true, json: async () => ({ ok: false, rtk_available: false, placeholder: false, message: '', error: 'rtk-missing: rtk binary not found on PATH', summary: {}, source: 'none' }) };
  await store.onOpen();
  out({ view: typeof store.renderView === 'function' ? store.renderView() : null });
} else if (scenario === 'open-renders-notice-endpoint-fail') {
  sandbox.__nextResponse = { ok: true, json: async () => ({ ok: false, rtk_available: true, placeholder: false, message: '', error: 'nonzero-exit: rtk gain exited 1', summary: {}, source: 'none' }) };
  await store.onOpen();
  out({ view: typeof store.renderView === 'function' ? store.renderView() : null });
} else if (scenario === 'open-network-fail') {
  sandbox.__nextResponse = null;
  await store.onOpen();
  out({ view: typeof store.renderView === 'function' ? store.renderView() : null });
} else if (scenario === 'render-then-unrelated') {
  sandbox.__nextResponse = { ok: true, json: async () => JSON.parse(process.argv[4]) };
  await store.onOpen();
  const before = JSON.stringify(store.events || []);
  store.note = 'unrelated-state-change';
  const after = JSON.stringify(store.events || []);
  out({ fetchCalls: sandbox.__fetchCalls || 0, status: store.status, events: store.events.length, unchanged: before === after });
} else if (scenario === 'render-payload') {
  const payload = JSON.parse(process.argv[4]);
  // CR 4-3: the real lifecycle only ever calls handle() from the loading
  // state (onOpen -> fetch -> response); a direct call from any other
  // state is a no-op by design (stale-response guard). Enter loading first
  // so this pure projection scenario mirrors that contract.
  store.status = 'loading';
  const r = store.handle(payload);
  out({ result: r, view: typeof store.renderView === 'function' ? store.renderView() : null });
} else if (scenario === 'events-unbounded-within-mount') {
  // CR 4-3 T5: growth WITHOUT cleanup must hit the 64-entry bound (the
  // old scenario reset events on every iteration and could never fail)
  const payload = { ok: false, rtk_available: true, message: '', error: 'unexpected-error: x', summary: {}, source: 'none' };
  store.status = 'loading';
  for (let i = 0; i < 100; i++) { store.handle(payload); store.status = 'loading'; }
  out({ events: store.events.length });
} else if (scenario === 'stale-write-after-cleanup') {
  // CR 4-3 E1: a fetch that resolves AFTER cleanup() must not write state
  let resolveFetch;
  sandbox.fetch = () => new Promise((res) => { resolveFetch = res; });
  const chain = store.onOpen();
  const inflightStatus = store.status;
  store.cleanup();
  const afterCleanup = store.status;
  resolveFetch({ ok: true, json: async () => ({
    ok: true, rtk_available: true, message: 'STALE-HEADLINE', error: '',
    summary: { total_commands: 5, total_input: 10, total_output: 5, total_saved: 5,
      avg_savings_pct: 50, total_time_ms: 1, avg_time_ms: 1, oversized_excluded: 0 },
    source: 'json' }) });
  await chain.catch(() => {});
  await new Promise((r) => setTimeout(r, 30));
  out({ inflightStatus, afterCleanup, finalStatus: store.status,
    finalMessage: store.message, summarySet: store.summary !== null, events: store.events.length });
} else if (scenario === 'reopen-after-stale-lands-fresh-fetch') {
  // CR 4-3 E2: after a stale write is blocked, reopening must fetch again
  let resolveFetch;
  let resolveCount = 0;
  sandbox.fetch = () => new Promise((res) => { resolveCount += 1; resolveFetch = res; });
  store.onOpen();
  store.cleanup();
  resolveFetch({ ok: true, json: async () => ({ ok: true, rtk_available: true,
    message: 'STALE', error: '', summary: { total_commands: 1, total_input: 1, total_output: 1,
      total_saved: 1, avg_savings_pct: 1, total_time_ms: 1, avg_time_ms: 1, oversized_excluded: 0 },
    source: 'json' }) });
  await new Promise((r) => setTimeout(r, 30));
  const afterCleanupStatus = store.status;
  store.onOpen(); // fresh mount: must re-enter loading and issue a second fetch
  out({ afterCleanupStatus, reopenStatus: store.status, fetchCount: resolveCount });
} else if (scenario === 'events-bounded') {
  for (let i = 0; i < 50; i++) { store.onOpen(); store.cleanup(); }
  out({ events: store.events.length });
} else {
  out({ error: 'unknown-scenario' });
}
})();
"""


def _run_store_scenario(scenario: str) -> dict:
    """Drive the real store file in node and return the reported JSON state."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node runtime not available for store behavioral harness")
    with tempfile.TemporaryDirectory() as tmp:
        harness = Path(tmp) / "harness.js"
        harness.write_text(_HARNESS_JS, encoding="utf-8")
        proc = subprocess.run(
            [node, str(harness), str(STORE_JS), scenario],
            capture_output=True,
            text=True,
            timeout=30,
        )
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_store_transitions_idle_loading_ready():
    """AC-03: idle -> loading -> ready along the onOpen-driven path."""
    report = _run_store_scenario("transition-ready")
    assert "error" not in report, f"harness error: {report}"
    assert report["log"] == ["idle", "loading", "ready"], f"bad transitions: {report}"


def test_store_transitions_loading_error_on_fail():
    """AC-03: loading -> error on fail() (the seam the next story's fetch will use)."""
    report = _run_store_scenario("transition-error")
    assert "error" not in report, f"harness error: {report}"
    assert report["status"] == "error", f"expected error status, got {report}"


def test_store_fires_exactly_one_open_per_mount():
    """AC-03: repeated onOpen() while open must not fire a second open event."""
    report = _run_store_scenario("one-open-per-mount")
    assert "error" not in report, f"harness error: {report}"
    opens = [e for e in report.get("events", []) if e.get("type") == "open"]
    assert len(opens) == 1, f"expected exactly one open event, got {report.get('events')}"


def test_store_cleanup_resets_and_remount_can_open_again():
    """AC-03: x-destroy cleanup returns to idle; a fresh mount may open again."""
    report = _run_store_scenario("cleanup-resets")
    assert "error" not in report, f"harness error: {report}"
    assert report["log"] == ["idle", "loading"], f"cleanup must reset then allow reopen: {report}"


def test_store_unrelated_state_change_triggers_no_events():
    """AC-03: unrelated store state changes must not emit lifecycle events."""
    report = _run_store_scenario("no-refetch-on-unrelated-change")
    assert "error" not in report, f"harness error: {report}"
    assert report["log"] == [True], f"unrelated change emitted events: {report}"


def test_notice_states_are_labeled_not_fake_numbers():
    """AC-02 (4.3): notice copy is present; no static fake savings figure ships.

    Supersedes 4.1's placeholder-label pin: 4.3 renders real data and honest
    notice states. Anchored both ways: notice markers must exist, and no
    literal percentage may ship in static markup (nothing is fabricated).
    """
    html = MAIN_HTML.read_text(encoding="utf-8")
    lowered = html.lower()
    assert any(k in lowered for k in ("no tracking", "no tracked", "not available", "no savings", "could not")), (
        "page must carry honest notice copy for the empty/fail states"
    )
    assert not re.search(r"\d+\.\d+\s*%", html), (
        "static markup must not ship a fabricated savings percentage"
    )
    js = STORE_JS.read_text(encoding="utf-8")
    assert "renderView" in js, "store must expose the render view for notice + headline"


# ---------------------------------------------------------------------------
# AC-04: structural half of the zero-load invariant
# ---------------------------------------------------------------------------

# recursive globs: 4.2/4.3/4-4 may land assets in subdirectories (edge F4 scope gap)
# 4-4: the badge surface family = commands/ + webui/ stores + extensions/webui/
# slot assets - the zero-load scans below run over ALL of them together (Task 8.2).
_SURFACE_SOURCES = (
    sorted(p for p in COMMANDS_DIR.rglob("*") if p.is_file())
    + sorted(p for p in WEBUI_DIR.rglob("*") if p.is_file())
    + sorted(p for p in (PLUGIN_ROOT / "extensions" / "webui").rglob("*") if p.is_file())
)
# NOTE: _SURFACE_STORES + _open_handler_span are defined in the 4-4 badge
# section below (they reference BADGE_STORE_JS, defined there)


def test_no_timer_or_watch_constructs_in_surface():
    """AC-04: zero setInterval / setTimeout / $watch / requestAnimationFrame."""
    banned = ("setInterval", "setTimeout", "$watch", "requestAnimationFrame", "setImmediate")
    for path in _SURFACE_SOURCES:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle in banned:
            assert needle not in text, f"{path.name}: banned timer/watch construct {needle!r}"


def test_network_primitives_confined_to_open_handler():
    """AC-01/AC-04 (4.3): the ONE fetch lives inside onOpen; no other request path.

    Supersedes 4.1's total no-network pin: the endpoint wiring lands in this
    story, so the invariant narrows from 'no network' to 'network only inside
    the open handler'. Anchored non-vacuously by the endpoint-path presence
    assertion (fails until the wiring exists).
    """
    # 4-4: each surface store carries its own single fetch inside its own
    # onOpen span (page store + badge store together, Task 8.2)
    for store_path in _SURFACE_STORES:
        js = store_path.read_text(encoding="utf-8")
        endpoint = "/api/plugins/rtk_a0/rtk_gain"
        assert endpoint in js, f"{store_path.name} must carry the endpoint path constant (wiring)"
        opens = [m.start() for m in re.finditer(r"fetch\(", js)]
        assert opens, f"{store_path.name} must issue exactly one fetch (page-open projection)"
        span = _open_handler_span(js)
        span_start = js.find(span)
        for hit in opens:
            assert span_start <= hit <= span_start + len(span), (
                f"every fetch( in {store_path.name} must sit inside the onOpen handler span"
            )
    for path in _SURFACE_SOURCES:
        if path in _SURFACE_STORES or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle in ("fetch(", "XMLHttpRequest", "navigator.sendBeacon", "EventSource", "WebSocket"):
            assert needle not in text, f"{path.name}: {needle!r} outside the open handler"


def test_only_open_handler_can_reference_the_endpoint_path():
    """AC-04 (4.3): the endpoint path is named once, inside onOpen, nowhere else."""
    for path in _SURFACE_SOURCES:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        hits = re.findall(r"/api/plugins/[A-Za-z0-9_/-]+", text)
        if path in _SURFACE_STORES:
            assert len(hits) == 1, f"{path.name} must name the endpoint exactly once, got {hits}"
            onopen_span = _open_handler_span(text)
            # single-source constant: the literal may live at module level and
            # be referenced inside onOpen (project constant pattern)
            assert hits[0] in onopen_span or "RTK_GAIN_ENDPOINT" in onopen_span, (
                f"the endpoint path must sit inside {path.name} onOpen (or its constant reference)"
            )
        else:
            for hit in hits:
                raise AssertionError(f"{path.name}: unexpected api path {hit!r}")


# ---------------------------------------------------------------------------
# AC-05: behavioral half of the zero-load invariant (absence audit)
# ---------------------------------------------------------------------------

def _git(args: list[str]) -> str | None:
    """Run git in the plugin repo; None when git is unavailable (mirrors licensing tests)."""
    if shutil.which("git") is None:
        return None
    proc = subprocess.run(
        ["git", "-C", str(PLUGIN_ROOT), *args], capture_output=True, text=True, timeout=30
    )
    return proc.stdout if proc.returncode == 0 else None


def _background_baseline() -> str:
    """Diff base that isolates THIS feature from upstream history.

    Pre-merge this was the epic branch point (HEAD at 5a04c91). After main
    advanced upstream (legitimate Hub/desloppify PRs touched hooks/setup.sh/
    plugin.yaml/extension), diffing against HEAD would misattribute those
    upstream changes to this feature. The merge baseline (origin/main at merge
    time) isolates the feature's own footprint; fall back to HEAD only when
    origin/main is unavailable (air-gapped clone).
    """
    base = _git(["rev-parse", "origin/main"])
    return base.strip() if base else "HEAD"


def test_no_agent_loop_or_lifecycle_changes_vs_baseline():
    """AC-05: zero agent-loop/extension and hooks changes; zero new background surfaces.

    Scoped to extensions/python (the agent-loop surface): Epic 4's second
    surface (Story 4-4 chat-header badge) ships a sanctioned plugin-owned
    frontend asset under extensions/webui/chat-top-start/ per the 2026-10-25
    ADR-007 addendum — the badge-story pin below carries that half; this pin
    guards the agent loop itself.
    """
    diff = _git(["diff", "--stat", _background_baseline(), "--", "extensions/python", "hooks.py", "scripts", "plugin.yaml", "rtk_pin.txt"])
    if diff is None:
        pytest.skip("git unavailable: absence audit via diff not possible")
    assert diff.strip() == "", f"story 4.1 must not touch background surfaces; diff:\n{diff}"
    manifest = yaml.safe_load((PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    for banned_key in ("mcp_servers", "mcpServers", "scheduled_tasks", "cron", "hooks_extra"):
        assert banned_key not in manifest, f"manifest must not gain background surface {banned_key!r}"
    assert not (PLUGIN_ROOT / "mcp_servers.json").exists(), "plugin must not ship an MCP server config"
    # production sources only: the suite that pins this audit legitimately names
    # the markers it forbids, so scanning tests/ would trip on itself
    schedule_markers = ("scheduled_tasks", "SCHEDULED_TASK", "register_scheduled")
    for rel_dir in ("extensions", "commands", "webui", "scripts"):
        for path in sorted((PLUGIN_ROOT / rel_dir).glob("**/*")):
            if not path.is_file() or ".venv" in path.parts or ".git" in path.parts:
                continue
            if path.suffix not in {".py", ".yaml", ".yml", ".json", ".sh", ".js", ".html"}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for needle in schedule_markers:
                assert needle not in text, f"{path.relative_to(PLUGIN_ROOT)}: scheduled-task marker {needle!r}"


def test_closed_page_issues_no_requests_outside_open():
    """AC-05 closed-page proof (4.3): no request code outside the open handler.

    Supersedes 4.1's total no-request pin: the page-open fetch is the single
    sanctioned request. Closed-page behavior = zero requests, proven by the
    handler-span scan (bites if any request primitive appears in cleanup, at
    load time, or anywhere but onOpen).
    """
    html = MAIN_HTML.read_text(encoding="utf-8")
    assert "fetch(" not in html, "main.html must carry no request code of its own"
    js = STORE_JS.read_text(encoding="utf-8")
    for primitive in ("XMLHttpRequest", "sendBeacon", "EventSource", "WebSocket"):
        assert primitive not in js, f"store could issue requests via {primitive!r}"
    onopen = js.find("onOpen()")
    span_end = re.search(r"\n    \w+\(", js[onopen + 1 :])
    onopen_span = js[onopen : onopen + 1 + (span_end.start() if span_end else len(js))]
    outside = js.replace(onopen_span, "")
    assert "fetch(" not in outside, "request primitive outside the open handler span"

# ===========================================================================
# Story 4.2: read-only rtk gain endpoint (api/rtk_gain.py)
# Auth-gated, POST-only, fail-open, flag-safe. TDD: red phase recorded
# against the absent endpoint; see the story Dev Agent Record.
# ===========================================================================

import asyncio
import importlib.util

API_DIR = PLUGIN_ROOT / "api"
API_HANDLER_PY = API_DIR / "rtk_gain.py"
RTK_GAIN_ENDPOINT = "/api/plugins/rtk_a0/rtk_gain"


def _ensure_framework_module():
    """Make helpers.api importable under the project venv.

    Prefers the real framework module (PYTHONPATH=/a0, framework env with
    flask). The project venv carries no flask, so a minimal ApiHandler shim
    is injected - the CI shim precedent (static-import equivalence). The
    handler subclasses only the three names below.
    """
    import sys
    import types

    if "helpers.api" in sys.modules:
        return
    try:
        import helpers.api  # noqa: F401
        return
    except Exception:
        pass
    stub = types.ModuleType("helpers.api")

    class ApiHandler:
        def __init__(self, app, thread_lock):
            self.app = app
            self.thread_lock = thread_lock

        @classmethod
        def requires_auth(cls) -> bool:
            return True

        @classmethod
        def get_methods(cls) -> list:
            return ["POST"]

    stub.ApiHandler = ApiHandler
    stub.Request = object
    stub.Response = dict
    sys.modules["helpers.api"] = stub


def _load_api_handler():
    """Load api/rtk_gain.py directly (framework import only for the tests)."""
    if not API_HANDLER_PY.is_file():
        pytest.fail(f"api handler must exist: {API_HANDLER_PY}")
    _ensure_framework_module()
    spec = importlib.util.spec_from_file_location("rtk_gain_api", str(API_HANDLER_PY))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _api_source() -> str:
    if not API_HANDLER_PY.is_file():
        pytest.fail(f"api handler must exist: {API_HANDLER_PY}")
    return API_HANDLER_PY.read_text(encoding="utf-8")


def _invoke(module, argv, *, stdout=b"", stderr=b"", returncode=0, side_effect=None):
    """Stub subprocess.run and drive the handler's async process() once."""
    calls = []

    class _Completed:
        def __init__(self):
            self.returncode = returncode
            self.stdout = stdout if isinstance(stdout, str) else stdout.decode("utf-8", "replace")
            self.stderr = stderr if isinstance(stderr, str) else stderr.decode("utf-8", "replace")

    def _fake_run(cmd, *args, **kwargs):
        calls.append({"cmd": cmd, "args": args, "kwargs": kwargs})
        if side_effect is not None:
            raise side_effect
        return _Completed()

    class _WhichPatch:
        def __init__(self, path):
            self.path = path
        def __enter__(self):
            self._saved = module.shutil.which
            module.shutil.which = lambda name: self.path
        def __exit__(self, *exc):
            module.shutil.which = self._saved

    saved_run = module.subprocess.run
    module.subprocess.run = _fake_run
    try:
        with _WhichPatch("/stub/rtk"):
            handler = module.RtkGain(app=None, thread_lock=None)
            result = asyncio.run(handler.process({}, None))
    finally:
        module.subprocess.run = saved_run
    return result, calls


def _tokens(argv):
    return {str(tok) for tok in argv}


# ---- RED core (validates sensitivity; full matrix added during implementation) ----

def test_red_handler_class_exists_with_auth_and_post_only():
    module = _load_api_handler()
    handler = module.RtkGain(app=None, thread_lock=None)
    assert handler.requires_auth() is True, "endpoint must declare auth (AC-01)"
    assert handler.get_methods() == ["POST"], "endpoint must be POST-only (AC-01)"


def test_red_invocation_argv_is_exact_and_flag_safe():
    module = _load_api_handler()
    result, calls = _invoke(module, [])
    assert len(calls) == 1, f"exactly one invocation expected, got {len(calls)}"
    argv = calls[0]["cmd"]
    assert isinstance(argv, list), "invocation must be argv-list, not a shell string (AC-03)"
    assert argv == ["rtk", "gain", "--format", "json", "--text"], f"argv mismatch: {argv!r}"
    tokens = _tokens(argv)
    for forbidden in ("--reset", "--yes", "--watch"):
        assert forbidden not in tokens, f"forbidden flag {forbidden!r} in argv (AC-06)"
    assert "--text" in tokens, "--text must always be passed (AC-06)"


def test_red_absent_binary_returns_structured_fail_state():
    module = _load_api_handler()
    saved_which = module.shutil.which
    # which-probe: simulate absence by patching which -> None in a local run
    module.shutil.which = lambda name: None
    try:
        handler = module.RtkGain(app=None, thread_lock=None)
        result = asyncio.run(handler.process({}, None))
    finally:
        module.shutil.which = saved_which
    assert isinstance(result, dict), "handler must return a dict (never raise, AC-07)"
    assert result.get("rtk_available") is False, f"absent binary must yield rtk_available False: {result!r}"


def test_red_fail_state_is_structured_on_nonzero_rc():
    module = _load_api_handler()
    good = json.dumps({"summary": {
        "total_commands": 2752, "total_input": 772465, "total_output": 533869,
        "total_saved": 239773, "avg_savings_pct": 31.039982394024324,
        "total_time_ms": 3294702, "avg_time_ms": 1197, "oversized_excluded": 16,
    }}).encode()
    result, _ = _invoke(module, [], returncode=3, stdout=good, stderr="warn noise")
    assert isinstance(result, dict)
    assert result.get("rtk_available") is True, "binary exists but failed: availability True (AC-05)"
    assert result.get("ok") is False, f"non-zero rc must be a structured fail state: {result!r}"
    assert result.get("error"), "fail state must carry a user-facing reason (AC-05)"


def test_red_json_summary_parsed_from_live_schema():
    module = _load_api_handler()
    payload = json.dumps({"summary": {
        "total_commands": 2752, "total_input": 772465, "total_output": 533869,
        "total_saved": 239773, "avg_savings_pct": 31.039982394024324,
        "total_time_ms": 3294702, "avg_time_ms": 1197, "oversized_excluded": 16,
    }}).encode()
    result, _ = _invoke(module, [], stdout=payload)
    assert result.get("ok") is True, f"json happy path must be ok: {result!r}"
    summary = result.get("summary", {})
    assert summary.get("total_commands") == 2752
    assert abs(summary.get("avg_savings_pct") - 31.039982394024324) < 1e-9


# ---- AC-01: handler protocol contract ----

def test_handler_declares_auth_and_post_only_distinct_from_defaults():
    module = _load_api_handler()
    source = _api_source()
    assert "def requires_auth" in source, "requires_auth must be declared on the handler (AC-01)"
    assert "def get_methods" in source, "get_methods must be declared on the handler (AC-01)"
    handler = module.RtkGain(app=None, thread_lock=None)
    assert handler.requires_auth() is True, "requires_auth must be explicitly True"
    assert handler.get_methods() == ["POST"], "get_methods must be exactly [POST]"


# ---- AC-03: availability probe, argv-list shape, stdout-only ----

def test_handler_subclasses_real_framework_apihandler():
    """Real-contract proof: subclasses the genuine helpers.api.ApiHandler.

    Env-gated: the project venv ships no flask, so helpers.api imports only
    in the framework environment (PYTHONPATH=/a0 + framework deps) and CI.
    """
    pytest.importorskip("flask", reason="project venv ships no flask; real helpers.api needs the framework env")
    import helpers.api as real_api

    _ensure_framework_module()
    module = _load_api_handler()
    assert issubclass(module.RtkGain, real_api.ApiHandler), "must subclass the real ApiHandler"
    assert module.RtkGain.requires_csrf() is True, "csrf follows auth on the real base"


def test_which_probe_precedes_any_invocation():
    module = _load_api_handler()
    saved_which = module.shutil.which
    module.shutil.which = lambda name: None
    saved_run = module.subprocess.run
    calls = []
    module.subprocess.run = lambda *a, **k: calls.append(a)
    try:
        handler = module.RtkGain(app=None, thread_lock=None)
        result = asyncio.run(handler.process({}, None))
    finally:
        module.shutil.which = saved_which
        module.subprocess.run = saved_run
    assert result.get("rtk_available") is False, f"absent binary: {result!r}"
    assert calls == [], "no subprocess may be attempted when the binary is absent"


def test_invocation_is_argv_list_with_project_timeout():
    module = _load_api_handler()
    _, calls = _invoke(module, [])
    assert len(calls) == 1, f"exactly one invocation expected: {calls!r}"
    cmd, kwargs = calls[0]["cmd"], calls[0]["kwargs"]
    assert isinstance(cmd, list) and all(isinstance(t, str) for t in cmd), "argv-list only"
    assert kwargs.get("timeout") == module.RTK_GAIN_TIMEOUT, "project timeout constant must be wired"
    assert kwargs.get("capture_output") is True, "stdout/stderr must be captured separately"
    assert kwargs.get("encoding") == "utf-8" and kwargs.get("errors") == "replace", "locale independence"
    assert not kwargs.get("shell", False), "shell mode must never be enabled"


def test_stderr_banner_noise_is_tolerated():
    module = _load_api_handler()
    payload = json.dumps({"summary": {
        "total_commands": 2752, "total_input": 772465, "total_output": 533869,
        "total_saved": 239773, "avg_savings_pct": 31.039982394024324,
        "total_time_ms": 3294702, "avg_time_ms": 1197, "oversized_excluded": 16,
    }}).encode()
    result, _ = _invoke(module, [], stdout=payload,
                        stderr=b"[warn] No hook installed - run `rtk init -g`")
    assert result.get("ok") is True, f"stderr noise must not corrupt stdout parse: {result!r}"


# ---- AC-04: tolerant parse + anchored-text fallback ----

def test_json_unknown_keys_are_ignored():
    module = _load_api_handler()
    payload = json.dumps({"summary": {
        "total_commands": 10, "total_input": 100, "total_output": 50,
        "total_saved": 50, "avg_savings_pct": 50.0, "total_time_ms": 10,
        "avg_time_ms": 1, "oversized_excluded": 0, "future_field": "ignored",
    }}).encode()
    result, _ = _invoke(module, [], stdout=payload)
    assert result.get("ok") is True
    assert "future_field" not in result.get("summary", {}), "unknown keys must be dropped"
    assert set(result["summary"]) == set(module._REQUIRED_SUMMARY_KEYS), "only schema keys survive"


def test_json_missing_required_key_falls_back_structured():
    """AC-04: a summary missing one required key fails structured (branch pin).

    CR 4-2 gap: the per-key presence loop had no dedicated test; the
    'unexpected-shape' matrix case only exercised the non-dict branch.
    """
    module = _load_api_handler()
    for missing_key in ("total_commands", "avg_savings_pct", "oversized_excluded"):
        summary = {
            "total_commands": 2752, "total_input": 772465, "total_output": 533869,
            "total_saved": 239773, "avg_savings_pct": 31.039982394024324,
            "total_time_ms": 3294702, "avg_time_ms": 1197, "oversized_excluded": 16,
        }
        del summary[missing_key]
        payload = json.dumps({"summary": summary}).encode()
        result, _ = _invoke(module, [], stdout=payload)
        assert result.get("ok") is False, f"missing {missing_key!r} must fail structured: {result!r}"
        assert "unparseable-output" in result.get("error", ""), f"missing {missing_key!r} classify: {result!r}"
        assert result.get("summary") == {}, f"missing {missing_key!r}: no partial summary may leak"


_CAPTURED_TEXT = (
    "RTK Token Savings (Global Scope)\n"
    "\n"
    "Total commands:    2752\n"
    "Input tokens:      772.5K\n"
    "Output tokens:     533.9K\n"
    "Tokens saved:      239.8K (31.0%)\n"
    "Total exec time:   54m54s (avg 1.2s)\n"
    "Efficiency meter: \u2588\u2588\u2588\u2588\u2588\u2588\u2588\u2591\u2591\u2591 31.0%\n"
    "Excluded:          16 oversized outputs (input past the ~30k-char agent cap)\n"
    "\n"
    "By Command\n"
)


def test_text_fallback_parses_the_captured_shape():
    module = _load_api_handler()
    result, _ = _invoke(module, [], stdout=_CAPTURED_TEXT.encode())
    assert result.get("ok") is True, f"text fallback must succeed: {result!r}"
    assert result.get("source") == "text", "degraded path must be marked"
    summary = result.get("summary", {})
    assert summary.get("total_commands") == 2752
    assert summary.get("saved_text") == "239.8K"
    assert summary.get("savings_pct_text") == 31.0
    assert summary.get("oversized_excluded") == 16


def test_empty_or_garbage_stdout_fails_structured():
    module = _load_api_handler()
    for garbage in ("", "\n\n", "error: not json at all", "{not really json"):
        result, _ = _invoke(module, [], stdout=garbage)
        assert isinstance(result, dict), "must never raise"
        assert result.get("ok") is False, f"garbage must fail structured: {garbage!r} -> {result!r}"
        assert result.get("error"), "fail state must carry a reason"


# ---- AC-05: failure matrix (each state structured, endpoint contract kept) ----

_VALID_PAYLOAD = json.dumps({"summary": {
    "total_commands": 2752, "total_input": 772465, "total_output": 533869,
    "total_saved": 239773, "avg_savings_pct": 31.039982394024324,
    "total_time_ms": 3294702, "avg_time_ms": 1197, "oversized_excluded": 16,
}}).encode()

_FAIL_CASES = {
    # valid payload + non-zero rc: ONLY the exit-code branch can fail this case
    "nonzero-exit": dict(returncode=3, stdout=_VALID_PAYLOAD, stderr="warn"),
    "timeout": dict(side_effect=subprocess.TimeoutExpired(cmd="rtk", timeout=2.0)),
    "malformed-json": dict(returncode=0, stdout="{summary: broken"),
    "unexpected-shape": dict(returncode=0, stdout=json.dumps({"unexpected": []}).encode()),
    "empty-stdout": dict(returncode=0, stdout=""),
}


def test_failure_matrix_returns_structured_states():
    module = _load_api_handler()
    for name, kwargs in _FAIL_CASES.items():
        result, _ = _invoke(module, [], **kwargs)
        assert isinstance(result, dict), f"{name}: never raises"
        assert result.get("ok") is False, f"{name}: must be a fail state, got {result!r}"
        assert isinstance(result.get("error"), str) and result["error"], f"{name}: needs a reason"
        assert result.get("summary") == {}, f"{name}: no fake summary"
        assert result.get("placeholder") is False, f"{name}: endpoint answered, not a placeholder"


def test_stderr_only_noise_is_a_fail_state():
    module = _load_api_handler()
    result, _ = _invoke(module, [], stdout="", stderr=b"[warn] hook banner only\n")
    assert result.get("ok") is False and result.get("error"), "stderr-only noise cannot masquerade as data"


# ---- AC-06: flag safety, both directions + structural pin ----

def test_flag_safety_never_passes_mutating_or_interactive_flags():
    module = _load_api_handler()
    _, calls = _invoke(module, [])
    tokens = _tokens(calls[0]["cmd"])
    assert "--text" in tokens, "--text is mandatory (bare rtk gain can auto-launch --watch)"
    for forbidden in ("--reset", "--yes", "--watch"):
        assert forbidden not in tokens, f"forbidden flag {forbidden} must never be passed"


def test_forbidden_flags_absent_from_handler_source():
    source = _api_source()
    for needle in ("--reset", "--yes", "--watch"):
        assert needle not in source, f"handler source must not reference {needle!r} at all"


# ---- AC-07: never-raise + no-persistence structural pin ----

def test_never_raises_under_injected_faults():
    module = _load_api_handler()
    saved_which = module.shutil.which
    # RuntimeError-style faults must degrade to a structured body (the
    # KeyboardInterrupt/BaseException path is intentionally NOT swallowed:
    # the endpoint's `except Exception` contract deliberately lets
    # BaseException propagate - there is nothing to fail open into)
    module.shutil.which = lambda name: (_ for _ in ()).throw(RuntimeError("which boom"))
    try:
        handler = module.RtkGain(app=None, thread_lock=None)
        result = asyncio.run(handler.process({}, None))
    finally:
        module.shutil.which = saved_which
    assert isinstance(result, dict) and result.get("ok") is False, f"which-fault: {result!r}"
    # unprintable exception inside the handler must still answer structured
    class _Evil:
        def __str__(self):
            raise ValueError("unprintable")
    saved_run = module.subprocess.run
    module.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(_Evil())
    try:
        with_unprintable = _invoke_guarded(module)
    finally:
        module.subprocess.run = saved_run
    assert with_unprintable.get("ok") is False, "unprintable fault must not escape"


def _invoke_guarded(module):
    saved_which = module.shutil.which
    module.shutil.which = lambda name: "/stub/rtk"
    try:
        handler = module.RtkGain(app=None, thread_lock=None)
        return asyncio.run(handler.process({}, None))
    finally:
        module.shutil.which = saved_which


def test_handler_performs_no_persistence_or_writes():
    source = _api_source()
    # scan code only: docstrings/comments legitimately name the audit itself
    code = re.sub(r'""".*?"""', '', source, flags=re.DOTALL)
    code = re.sub(r"#.*", "", code)
    for needle in (
        "open(", "json.dump", "write_text", "write_bytes", "sqlite3",
        "Path(", "cache", "shelve", "os.environ[", "putenv",
    ):
        assert needle not in code, f"handler must not persist: found {needle!r}"


def test_api_dir_ships_no_framework_symbols():
    """Licensing exemption scope: plugin-owned api/ holds handler code only."""
    names = sorted(
        p.name for p in API_DIR.rglob("*") if p.is_file()
        and "__pycache__" not in p.parts and p.suffix != ".pyc"
    ) if API_DIR.exists() else []
    assert names == ["__init__.py", "rtk_gain.py"], f"api/ must contain only the handler: {names}"
    for banned in ("agent.py", "initialize.py", "models.py", "helpers", "api", "webui", "plugins"):
        assert banned not in names, f"framework symbol {banned!r} must not live under plugin api/"


# ---- D1 seam: endpoint response feeds the shipped store contract ----

_LIVE_JSON = json.dumps({"summary": {
    "total_commands": 2752, "total_input": 772465, "total_output": 533869,
    "total_saved": 239773, "avg_savings_pct": 31.039982394024324,
    "total_time_ms": 3294702, "avg_time_ms": 1197, "oversized_excluded": 16,
}}).encode()


def test_response_fields_are_consumable_by_the_store():
    module = _load_api_handler()
    result, _ = _invoke(module, [], stdout=_LIVE_JSON)
    # the store consumes placeholder/message/error and treats summary as data
    assert result.get("placeholder") is False, "endpoint answer lifts the placeholder"
    assert isinstance(result.get("message"), str) and result["message"], "needs a message string"
    assert result.get("error") == "", "success carries no error"
    assert isinstance(result.get("summary"), dict) and result["summary"], "carries the numbers"
    # store fields that must stay untouched by the endpoint response
    for store_only in ("status", "events"):
        assert store_only not in result, f"endpoint must not set store-owned field {store_only!r}"


_F2_HARNESS_JS = r"""
const fs = require('fs');
const vm = require('vm');
const sandbox = { console, window: {}, Alpine: { store: (n, f) => {} } };
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), sandbox);
const store = sandbox.createRtkGainStore();
store.status = 'ready'; // wrong state for fail(): already completed
store.fail('late failure');
console.log(JSON.stringify({ status: store.status, error: store.error,
  transitions: store.events.filter(e => e.type === 'status').map(e => e.to) }));
"""


def test_store_fail_from_non_loading_state_does_not_corrupt_machine():
    """F2 seam: fail() outside loading must be a no-op, not a corrupted machine."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node runtime not available")
    with tempfile.TemporaryDirectory() as tmp:
        harness = Path(tmp) / "f2.js"
        harness.write_text(_F2_HARNESS_JS, encoding="utf-8")
        proc = subprocess.run([node, str(harness), str(STORE_JS)],
                              capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"harness failed: {proc.stderr}"
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    assert report["status"] == "ready", f"fail() must not alter a non-loading state: {report}"
    assert report["error"] == "", f"fail() must not write an error outside loading: {report}"
    assert "error" not in report["transitions"], f"no error transition may be emitted: {report}"


# ===========================================================================
# Story 4.3: page rendering, honesty, end-to-end verification
# Rendering + notice/empty-ledger + text-fallback render proof + zero-load.
# TDD: red phase recorded against the 4.1 shell (no render seam, no fetch).
# ===========================================================================


def _run_store_render(scenario: str, payload: dict) -> dict:
    """Drive the real store in node, passing a payload through the render seam."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node runtime not available for store behavioral harness")
    with tempfile.TemporaryDirectory() as tmp:
        harness = Path(tmp) / "harness.js"
        harness.write_text(_HARNESS_JS, encoding="utf-8")
        proc = subprocess.run(
            [node, str(harness), str(STORE_JS), scenario, json.dumps(payload)],
            capture_output=True,
            text=True,
            timeout=30,
        )
    assert proc.returncode == 0, f"node harness failed: {proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


# Endpoint payloads mirroring api/rtk_gain.py's contract (4.2): the live
# json summary shape captured 2026-10-25, the all-zero empty-ledger shape,
# structured fail bodies, and the anchored-text fallback shape.
_JSON_OK_PAYLOAD = {
    "ok": True,
    "rtk_available": True,
    "message": "Tokens saved: 225.2K (30.9%)",
    "error": "",
    "summary": {
        "total_commands": 182,
        "total_input": 4203109,
        "total_output": 1512301,
        "total_saved": 225200,
        "avg_savings_pct": 30.94,
        "total_time_ms": 151843,
        "avg_time_ms": 834.3,
        "oversized_excluded": 2,
    },
    "source": "json",
}

_EMPTY_LEDGER_PAYLOAD = {
    "ok": True,
    "rtk_available": True,
    "message": "Tokens saved: 0 (0.0%)",
    "error": "",
    "summary": {
        "total_commands": 0,
        "total_input": 0,
        "total_output": 0,
        "total_saved": 0,
        "avg_savings_pct": 0,
        "total_time_ms": 0,
        "avg_time_ms": 0,
        "oversized_excluded": 0,
    },
    "source": "json",
}

_MISSING_RTK_PAYLOAD = {
    "ok": False,
    "rtk_available": False,
    "message": "",
    "error": "rtk-missing: rtk binary not found on PATH",
    "summary": {},
    "source": "none",
}

_ENDPOINT_FAIL_PAYLOAD = {
    "ok": False,
    "rtk_available": True,
    "message": "",
    "error": "nonzero-exit: rtk gain exited 1",
    "summary": {},
    "source": "none",
}

_TEXT_FALLBACK_PAYLOAD = {
    "ok": True,
    "rtk_available": True,
    "message": "Tokens saved: 225.2K (30.9%)",
    "error": "",
    "summary": {
        "total_commands": 182,
        "headline": "Tokens saved: 225.2K (30.9%)",
        "saved_text": "225.2K",
        "savings_pct_text": 30.9,
        "input_text": "4.2M",
        "output_text": "1.5M",
        "exec_time_text": "2m 31s",
        "oversized_excluded": 2,
    },
    "source": "text",
}


def test_open_renders_json_headline_verbatim():
    """AC-01: headline string ships verbatim; pct one display format, never recomputed."""
    report = _run_store_scenario("open-renders-json")
    assert "error" not in report, f"harness error: {report}"
    view = report.get("view") or {}
    assert view.get("view") == "headline", f"expected headline view, got {view}"
    assert view.get("headline") == "Tokens saved: 225.2K (30.9%)", (
        f"headline must be RTK's own string verbatim, got {view.get('headline')!r}"
    )
    values = [f.get("value") for f in view.get("fields", [])]
    assert "182" in values, f"total commands must render, got {values}"
    assert "30.9%" in values, f"pct renders with one display format, got {values}"
    assert "30.94%" not in values, "no double-rounding of the raw float"
    assert "225,200" in values, f"saved amount humanized for display, got {values}"


def test_open_renders_empty_ledger_notice():
    """AC-02/D6: all-zero summary is a distinct friendly notice, never error chrome."""
    report = _run_store_scenario("open-renders-empty-ledger")
    assert "error" not in report, f"harness error: {report}"
    view = report.get("view") or {}
    assert view.get("view") == "notice", f"empty ledger must be a notice, got {view}"
    notice = str(view.get("notice", "")).lower()
    assert "no tracked" in notice or "no savings" in notice or "empty" in notice, (
        f"notice must explain the empty-ledger state, got {notice!r}"
    )


def test_open_renders_missing_rtk_notice_with_remedy():
    """AC-02: rtk-missing renders friendly copy with a remedy pointer, not an error."""
    report = _run_store_scenario("open-renders-notice-missing-rtk")
    assert "error" not in report, f"harness error: {report}"
    view = report.get("view") or {}
    assert view.get("view") == "notice", f"missing rtk must be a notice, got {view}"
    notice = str(view.get("notice", "")).lower()
    assert "binary" in notice or "path" in notice, f"notice must name the cause, got {notice!r}"
    assert "setup" in notice or "readme" in notice or "install" in notice, (
        f"notice must point at the remedy, got {notice!r}"
    )


def test_open_renders_endpoint_fail_notice_without_raw_error():
    """AC-02: endpoint fail states stay friendly; raw reason strings do not leak."""
    report = _run_store_scenario("open-renders-notice-endpoint-fail")
    assert "error" not in report, f"harness error: {report}"
    view = report.get("view") or {}
    assert view.get("view") == "notice", f"endpoint fail must be a notice, got {view}"
    notice = str(view.get("notice", ""))
    assert "nonzero-exit" not in notice, f"raw fail reason must not leak, got {notice!r}"


def test_open_renders_text_fallback_headline():
    """AC-03: text-mode payload renders the same headline from RTK's own strings."""
    report = _run_store_render("render-payload", _TEXT_FALLBACK_PAYLOAD)
    assert "error" not in report, f"harness error: {report}"
    view = report.get("view") or {}
    assert view.get("view") == "headline", f"text fallback must render headline, got {view}"
    assert view.get("headline") == "Tokens saved: 225.2K (30.9%)", (
        f"text headline must be verbatim, got {view.get('headline')!r}"
    )
    values = [f.get("value") for f in view.get("fields", [])]
    assert "225.2K" in values, f"saved_text renders as-is, got {values}"
    assert "4.2M" in values and "1.5M" in values, f"input/output text renders as-is, got {values}"


def test_open_network_failure_sets_error_not_notice():
    """AC-02: a network-level failure is an error state (distinct from notices)."""
    report = _run_store_scenario("open-network-fail")
    assert "error" not in report, f"harness error: {report}"
    view = report.get("view") or {}
    assert view.get("view") == "error", (
        f"network failure must surface as error, got {view}"
    )


def test_events_log_bounded_under_repeated_mounts():
    """CR 4.1: events[] is bounded; repeated mounts cannot grow it without limit."""
    report = _run_store_scenario("events-bounded")
    assert "error" not in report, f"harness error: {report}"
    assert report.get("events", 0) <= 64, f"events must stay bounded, got {report.get('events')}"


def test_stale_response_after_cleanup_cannot_write_state():
    """"CR 4-3 E1 REGRESSION: in-flight fetch resolving after cleanup() is

    dropped (mount-generation guard). Before the fix the stale payload wrote
    status ready + headline + summary into the freshly-idle store."""
    report = _run_store_scenario("stale-write-after-cleanup")
    assert "error" not in report, f"harness error: {report}"
    assert report.get("inflightStatus") == "loading", f"setup: {report}"
    assert report.get("afterCleanup") == "idle", f"cleanup must reset: {report}"
    assert report.get("finalStatus") == "idle", (
        f"stale response must not revive the store, got {report}"
    )
    assert report.get("summarySet") is False, f"stale summary must not land: {report}"
    assert report.get("events") == 0, f"stale write must emit no events: {report}"


def test_reopen_after_stale_lands_fresh_fetch():
    """"CR 4-3 E2 REGRESSION: after a stale write is dropped, reopening the

    page re-enters loading and issues a fresh fetch (not a stuck value)."""
    report = _run_store_scenario("reopen-after-stale-lands-fresh-fetch")
    assert "error" not in report, f"harness error: {report}"
    assert report.get("afterCleanupStatus") == "idle", f"cleanup reset: {report}"
    assert report.get("reopenStatus") == "loading", f"reopen must open: {report}"
    assert report.get("fetchCount") == 2, (
        f"stale mount + fresh mount = two fetch calls, got {report}"
    )


def test_events_growth_within_one_mount_stays_bounded():
    """"CR 4-3 T5: events[] growth WITHOUT cleanup must hit the 64 bound.

    The mount-cycle scenario resets events on every iteration (cleanup()
    empties the log), so it cannot fail - this within-mount growth probe
    is the non-vacuous half of the bound pin (edge-layer F2 evidence)."""
    report = _run_store_scenario("events-unbounded-within-mount")
    assert "error" not in report, f"harness error: {report}"
    assert report.get("events", 0) == 64, f"expected the 64-entry bound, got {report.get('events')}"


def test_no_refetch_after_ready_on_unrelated_change():
    """AC-05: once rendered, unrelated state changes issue no further requests."""
    report = _run_store_render("render-then-unrelated", _JSON_OK_PAYLOAD)
    assert "error" not in report, f"harness error: {report}"
    assert report.get("fetchCalls") == 1, f"exactly one fetch total, got {report}"
    assert report.get("status") == "ready", f"status stays ready, got {report}"
    assert report.get("unchanged") is True, f"unrelated change emitted events, got {report}"


def test_html_renders_headline_notice_and_error_slots():
    """AC-01/AC-02: the page wires the render view into headline/notice/error slots."""
    html = MAIN_HTML.read_text(encoding="utf-8")
    assert "renderView" in html, "page must project the render view"
    assert "rtk-gain-headline" in html, "headline slot must exist"
    assert "rtk-gain-notice" in html, "notice slot must exist"
    assert "status === 'error'" in html, "error slot must exist"
    assert "rtk-gain-placeholder" not in html, "4.1 placeholder paragraph must be gone"


def test_store_never_recomputes_percentage_from_counts():
    """AC-01 honesty pin: no arithmetic on summary counts in the store source."""
    js = STORE_JS.read_text(encoding="utf-8")
    # the only arithmetic allowed: display ms->s conversion and pct one-decimal
    # format on numbers already provided (no saved/input-derived percentage)
    for banned in ("total_saved *", "total_saved /", "* 100;", "* 100 ", "/ total_input", "* total_input"):
        assert banned not in js, f"store must not recompute savings from counts: {banned!r}"


# ---------------------------------------------------------------------------
# AC-04: real-binary end-to-end through the page (controlled PATH states)
# Endpoint -> page render seam with the real rtk binary resolved only via
# the controlled PATH (host-state isolation pattern from Story 2.5/3.2);
# the ambient host state is never depended upon.
# ---------------------------------------------------------------------------


def _endpoint_payload(monkeypatch, path_value) -> dict:
    """Drive api/rtk_gain.py once under a controlled PATH; return its body."""
    module = _load_api_handler()
    monkeypatch.setenv("PATH", path_value)
    handler = module.RtkGain(app=None, thread_lock=None)
    return asyncio.run(handler.process({}, None))


_REAL_RTK_PATH = os.environ.get("PATH", "")
_NO_RTK_PATH = "/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin"


def test_real_binary_e2e_renders_headline_through_page(monkeypatch):
    """AC-04: real rtk on controlled PATH -> endpoint payload -> page headline."""
    if shutil.which("rtk") is None:
        pytest.skip("real rtk binary not on host PATH (host-state isolation)")
    payload = _endpoint_payload(monkeypatch, _REAL_RTK_PATH)
    print(f"LIVE-E2E payload: {json.dumps(payload)}")
    assert payload.get("ok") is True, f"real binary must yield ok payload, got {payload}"
    assert payload.get("source") in ("json", "text"), f"unexpected source: {payload}"
    report = _run_store_render("render-payload", payload)
    assert "error" not in report, f"harness error: {report}"
    view = report.get("view") or {}
    if view.get("view") == "notice":
        # honest empty-ledger rendering: real ledger with zero tracked commands
        assert "no tracked" in str(view.get("notice", "")).lower()
        return
    assert view.get("view") == "headline", f"real payload must render, got {view}"
    assert view.get("headline") == payload.get("message"), (
        "page headline must be the endpoint's own string verbatim"
    )
    values = [f.get("value") for f in view.get("fields", [])]
    assert any(str(v).endswith("%") and v != "n/a" for v in values), (
        f"pct must render from RTK's own number, got {values}"
    )


def test_absent_binary_e2e_renders_missing_rtk_notice(monkeypatch):
    """AC-04: rtk absent on controlled PATH -> structured fail -> notice state."""
    payload = _endpoint_payload(monkeypatch, _NO_RTK_PATH)
    assert payload.get("ok") is False, f"absent binary must fail-open, got {payload}"
    assert payload.get("rtk_available") is False, f"rtk_available must be false: {payload}"
    assert payload.get("summary") == {}, "fail body must carry an empty summary"
    report = _run_store_render("render-payload", payload)
    assert "error" not in report, f"harness error: {report}"
    view = report.get("view") or {}
    assert view.get("view") == "notice", f"absent binary must render a notice, got {view}"
    notice = str(view.get("notice", "")).lower()
    assert "binary" in notice and "setup" in notice, f"notice must name cause + remedy: {notice!r}"




# ===========================================================================
# Story 4-4: chat-header savings badge (second ADR-007 surface)
# Plugin-owned extensions/webui/chat-top-start/ asset + webui/ badge store,
# exactly one page-open fetch of the 4-2 endpoint, degrade-to-hidden.
# ===========================================================================

EXT_WEBUI_DIR = PLUGIN_ROOT / "extensions" / "webui"
BADGE_HTML = EXT_WEBUI_DIR / "chat-top-start" / "_10_rtk_gain_badge.html"
BADGE_STORE_JS = WEBUI_DIR / "rtk-gain-badge-store.js"
BADGE_STORE_URL = "/plugins/rtk_a0/webui/rtk-gain-badge-store.js"

# stores that legitimately carry the endpoint constant + the open-handler fetch
_SURFACE_STORES = (STORE_JS, BADGE_STORE_JS)


def _open_handler_span(text: str) -> str:
    """The onOpen method body: from its declaration to the next method in the object."""
    onopen = text.find("onOpen()")
    assert onopen != -1, "store must define onOpen"
    span_end = re.search(r"\n    \w+\(", text[onopen + 1 :])
    return text[onopen : onopen + 1 + (span_end.start() if span_end else len(text))]

# Story 4.2 fail reasons (endpoint error prefixes) - degrade-to-hidden matrix
BADGE_FAIL_REASONS = (
    "rtk-missing",
    "timeout",
    "nonzero-exit",
    "unparseable-output",
    "spawn-failed",
    "unexpected-error",
)


def _run_badge_scenario(scenario: str, payload: dict | None = None) -> dict:
    """Drive the real badge store in node; returns the reported JSON state."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node runtime not available for badge behavioral harness")
    with tempfile.TemporaryDirectory() as tmp:
        harness = Path(tmp) / "badge_harness.js"
        harness.write_text(_BADGE_HARNESS_JS, encoding="utf-8")
        args = [node, str(harness), str(BADGE_STORE_JS), scenario]
        if payload is not None:
            args.append(json.dumps(payload))
        proc = subprocess.run(args, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"badge node harness failed: {proc.stderr}"
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------------------
# AC-01 (4-4): badge asset at the framework slot path
# ---------------------------------------------------------------------------

def test_badge_asset_exists_at_slot_path():
    """AC-01: the badge ships at extensions/webui/chat-top-start/ (directory-scan mount)."""
    assert BADGE_HTML.is_file(), (
        f"badge asset must exist at the slot path: {BADGE_HTML}"
    )


def test_badge_asset_loads_the_badge_store_from_the_plugin_webui_path():
    """AC-01: the asset references the plugin badge store by absolute webui URL."""
    html = BADGE_HTML.read_text(encoding="utf-8")
    assert BADGE_STORE_URL in html, (
        f"badge asset must load {BADGE_STORE_URL} (page-store precedent pattern)"
    )


def test_badge_asset_declares_page_open_lifecycle_hooks():
    """AC-01/AC-04: x-init fires onOpen; x-destroy runs cleanup (teardown)."""
    html = BADGE_HTML.read_text(encoding="utf-8")
    assert "onOpen" in html, "badge asset must call the store onOpen handler"
    assert "cleanup" in html, "badge asset must call the store cleanup on destroy"
    assert "rtkGainBadge" in html, "badge asset must reference the badge store"


def test_badge_asset_hidden_by_default_no_static_percentage():
    """AC-03: hidden-by-default root, never a blank box, no fabricated pct literal."""
    html = BADGE_HTML.read_text(encoding="utf-8")
    assert "display:none" in html.replace(" ", "") or 'style="display: none"' in html or "x-show" in html, (
        "badge root must start hidden (x-show guard or display:none)"
    )
    assert not re.search(r"\d+\.\d+\s*%", html), (
        "static markup must not ship a fabricated savings percentage"
    )


_BADGE_HARNESS_JS = r"""
const fs = require('fs');
const vm = require('vm');
const storeFile = process.argv[2];
const scenario = process.argv[3];
const payloadArg = process.argv[4];
let registered = null;
const sandbox = {
  console,
  window: {},
  Alpine: { store: (name, factory) => { registered = { name, factory }; } },
  fetch: async () => {
    sandbox.__fetchCalls = (sandbox.__fetchCalls || 0) + 1;
    if (!sandbox.__nextResponse) { throw new Error('no stubbed response'); }
    return sandbox.__nextResponse;
  },
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(storeFile, 'utf8'), sandbox);
const factory = sandbox.createRtkGainBadgeStore || (registered && registered.factory);
if (!factory) { console.log(JSON.stringify({ error: 'badge store factory not found' })); process.exit(0); }
const store = factory();
function out(extra) { console.log(JSON.stringify(Object.assign({ status: store.status, events: store.events }, extra))); }
function payload(extra) { return Object.assign({ ok: true, rtk_available: true, placeholder: false, message: 'Tokens saved: 254351 (30.9%)', error: '', source: 'json', summary: { total_commands: 2915, total_input: 823183, total_output: 570010, total_saved: 254351, avg_savings_pct: 30.89847579456816, total_time_ms: 3305575, avg_time_ms: 1133.7, oversized_excluded: 20 } }, extra); }
(async () => {
if (scenario === 'badge-one-fetch-per-mount') {
  sandbox.__nextResponse = { ok: true, json: async () => payload() };
  await store.onOpen();
  store.onOpen();
  store.onOpen();
  out({ fetchCalls: sandbox.__fetchCalls || 0 });
} else if (scenario === 'badge-cleanup-resets') {
  sandbox.__nextResponse = { ok: true, json: async () => payload() };
  await store.onOpen();
  const readyStatus = store.status;
  store.cleanup();
  const afterCleanup = store.status;
  store.onOpen();
  out({ log: [readyStatus, afterCleanup, store.status], fetchCalls: sandbox.__fetchCalls || 0 });
} else if (scenario === 'badge-render-pct') {
  const body = payloadArg ? JSON.parse(payloadArg) : payload();
  sandbox.__nextResponse = { ok: true, json: async () => body };
  await store.onOpen();
  out({ pct: typeof store.badgePct === 'function' ? store.badgePct() : null, visible: typeof store.visible === 'function' ? store.visible() : null });
} else if (scenario === 'badge-fail-matrix') {
  sandbox.__nextResponse = { ok: true, json: async () => JSON.parse(payloadArg) };
  await store.onOpen();
  out({ visible: typeof store.visible === 'function' ? store.visible() : null, pct: typeof store.badgePct === 'function' ? store.badgePct() : null });
} else if (scenario === 'badge-network-fail') {
  sandbox.__nextResponse = null;
  await store.onOpen();
  out({ visible: typeof store.visible === 'function' ? store.visible() : null });
} else if (scenario === 'badge-closed-page-zero-traffic') {
  let resolveFetch;
  sandbox.fetch = () => {
    sandbox.__fetchCalls = (sandbox.__fetchCalls || 0) + 1;
    return new Promise((res) => { resolveFetch = res; });
  };
  const chain = store.onOpen(); // pending: no await (would deadlock pre-resolve)
  store.cleanup(); // user closes the chat page -> x-destroy teardown
  resolveFetch({ ok: true, json: async () => payload() });
  await chain.catch(() => {});
  await new Promise((r) => setTimeout(r, 30));
  out({ fetchCalls: sandbox.__fetchCalls || 0, visible: typeof store.visible === 'function' ? store.visible() : null });
} else if (scenario === 'badge-load-absent-alpine') {
  out({ ok: true });
} else {
  out({ error: 'unknown-scenario' });
}
})();
"""


# ---------------------------------------------------------------------------
# AC-01/AC-04 (4-4): badge store single-fetch lifecycle (node harness)
# ---------------------------------------------------------------------------

def test_badge_store_exposes_factory_and_alpine_registration():
    """Store file defines createRtkGainBadgeStore factory + Alpine registration."""
    js = BADGE_STORE_JS.read_text(encoding="utf-8")
    assert "createRtkGainBadgeStore" in js, "badge store must define a testable factory"
    assert "Alpine.store" in js, "badge store must register with Alpine"


def test_badge_store_uses_top_level_var_not_const():
    """CR 4-1 F1: top-level var (never const) - A0 re-executes classic scripts per mount."""
    js = BADGE_STORE_JS.read_text(encoding="utf-8")
    for line in js.splitlines():
        # CR 4-1 F1 scope: TOP-LEVEL const only - function-body const is fine
        # (the page store uses the same in-factory const shape).
        assert not line.startswith("const "), (
            f"top-level const redeclaration hazard (CR 4-1 F1): {line[:60]!r}"
        )


def test_badge_fires_exactly_one_fetch_per_mount():
    """AC-01: exactly one endpoint fetch per mount; re-entry guard blocks more."""
    report = _run_badge_scenario("badge-one-fetch-per-mount")
    assert "error" not in report, f"harness error: {report}"
    assert report.get("fetchCalls") == 1, f"expected exactly one fetch, got {report}"


def test_badge_cleanup_resets_and_remount_fetches_again():
    """AC-04: x-destroy cleanup resets the machine; a fresh mount starts clean."""
    report = _run_badge_scenario("badge-cleanup-resets")
    assert "error" not in report, f"harness error: {report}"
    log = report.get("log", [])
    assert len(log) == 3 and log[1] == "idle", f"cleanup must reset to idle: {report}"
    assert report.get("fetchCalls") == 2, f"fresh mount must fetch again: {report}"


def test_badge_network_failure_degrades_to_hidden():
    """AC-03: network-level failure (fetch rejects) degrades to hidden, no chrome."""
    report = _run_badge_scenario("badge-network-fail")
    assert "error" not in report, f"harness error: {report}"
    assert report.get("visible") is False, f"network failure must hide the badge: {report}"


def test_badge_closed_page_issues_zero_further_requests():
    """AC-04: after cleanup (page closed) no further endpoint requests are issued."""
    report = _run_badge_scenario("badge-closed-page-zero-traffic")
    assert "error" not in report, f"harness error: {report}"
    assert report.get("fetchCalls") == 1, f"closed page must issue zero further requests: {report}"
    assert report.get("visible") is False, f"closed page must not surface state: {report}"


def test_badge_asset_loads_without_alpine_present():
    """Alpine-absent load must not throw (store-absent tolerance)."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node runtime not available")
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "no_alpine.js"
        probe.write_text(
            "const vm=require('vm'),fs=require('fs');"
            "const s={console,window:{}};s.globalThis=s;"
            "vm.createContext(s);"
            "vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),s);"
            "console.log(JSON.stringify({ok:true}));",
            encoding="utf-8",
        )
        proc = subprocess.run(
            [node, str(probe), str(BADGE_STORE_JS)], capture_output=True, text=True, timeout=30
        )
    assert proc.returncode == 0, f"store must load with Alpine absent: {proc.stderr}"
    assert json.loads(proc.stdout.strip())["ok"] is True


# ---------------------------------------------------------------------------
# AC-02 (4-4): render the endpoint's own values (no recompute, no double-round)
# ---------------------------------------------------------------------------

def test_badge_renders_pct_verbatim_from_endpoint_json_summary():
    """AC-02: json-summary pct renders with one display format (30.89847... -> 30.9%)."""
    report = _run_badge_scenario("badge-render-pct")
    assert "error" not in report, f"harness error: {report}"
    assert report.get("visible") is True, f"ready payload must show the badge: {report}"
    assert report.get("pct") == "30.9%", f"pct must render as provided: {report}"


def test_badge_renders_pct_from_text_fallback_summary():
    """AC-02: text-fallback summary renders pct with the same single format."""
    payload = {
        "ok": True,
        "rtk_available": True,
        "message": "Tokens saved: 225.2K (30.9%)",
        "source": "text",
        "summary": {
            "total_commands": 182,
            "headline": "Tokens saved: 225.2K (30.9%)",
            "savings_pct_text": 30.9,
        },
    }
    report = _run_badge_scenario("badge-render-pct", payload)
    assert "error" not in report, f"harness error: {report}"
    assert report.get("pct") == "30.9%", f"text pct must render as provided: {report}"


def test_badge_store_never_recomputes_percentage_from_counts():
    """AC-02: the pct display value comes from the endpoint field, never counts math."""
    js = BADGE_STORE_JS.read_text(encoding="utf-8")
    fn_start = js.find("badgePct")
    assert fn_start != -1, "badge store must expose badgePct"
    # the pct display logic lives from badgePct through the end of the store
    body = js[fn_start:]
    assert "avg_savings_pct" in body or "savings_pct_text" in body, (
        "badgePct must read the endpoint's own pct field"
    )
    for banned in ("/ 100", "* 100", "total_saved /", "saved /", "/ summary", "Math.round"):
        assert banned not in body, f"badgePct must not recompute pct (found {banned!r})"


# ---------------------------------------------------------------------------
# AC-03 (4-4): degrade-to-hidden fail matrix
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("reason", BADGE_FAIL_REASONS)
def test_badge_hidden_on_endpoint_fail_reason(reason):
    """AC-03: every endpoint fail reason degrades to hidden (no chrome, no retry)."""
    payload = {
        "ok": False,
        "rtk_available": reason != "rtk-missing",
        "placeholder": False,
        "message": "",
        "error": f"{reason}: simulated failure",
        "summary": {},
        "source": "none",
    }
    report = _run_badge_scenario("badge-fail-matrix", payload)
    assert "error" not in report, f"harness error: {report}"
    assert report.get("visible") is False, f"{reason} must hide the badge: {report}"
    assert report.get("pct") is None, f"{reason} must render no pct: {report}"


def test_badge_hidden_when_rtk_unavailable():
    """AC-03: rtk_available:false hides the badge."""
    payload = {
        "ok": False,
        "rtk_available": False,
        "message": "",
        "error": "rtk-missing: rtk binary not found on PATH",
        "summary": {},
        "source": "none",
    }
    report = _run_badge_scenario("badge-fail-matrix", payload)
    assert report.get("visible") is False, f"unavailable rtk must hide: {report}"


def test_badge_hidden_on_empty_ledger():
    """AC-03: ok payload with zero activity renders nothing (no blank badge)."""
    payload = {
        "ok": True,
        "rtk_available": True,
        "message": "Tokens saved: 0 (0.0%)",
        "source": "json",
        "summary": {
            "total_commands": 0,
            "total_input": 0,
            "total_output": 0,
            "total_saved": 0,
            "avg_savings_pct": 0,
            "total_time_ms": 0,
            "avg_time_ms": 0,
            "oversized_excluded": 0,
        },
    }
    report = _run_badge_scenario("badge-fail-matrix", payload)
    assert report.get("visible") is False, f"empty ledger must hide: {report}"
    assert report.get("pct") is None, f"empty ledger must render no pct: {report}"


def test_badge_hidden_on_malformed_payload_shapes():
    """AC-03: unrenderable payloads (missing summary / wrong types) hide the badge."""
    shapes = [
        {},
        {"ok": True},
        {"ok": True, "summary": "not-an-object"},
        {"ok": True, "summary": {"total_commands": "x", "avg_savings_pct": "y"}},
        {"ok": True, "summary": {"avg_savings_pct": None}},
        {"ok": "truthy-string", "summary": {"avg_savings_pct": 30.9}},
        {"ok": True, "summary": {"avg_savings_pct": True}},
    ]
    for shape in shapes:
        report = _run_badge_scenario("badge-fail-matrix", shape)
        assert "error" not in report, f"harness error: {report}"
        assert report.get("visible") is False, f"unrenderable payload must hide: {shape} -> {report}"
        assert report.get("pct") is None, f"unrenderable payload must render no pct: {report}"


# ---------------------------------------------------------------------------
# AC-05 (4-4): zero-load invariant - structural half over extensions/webui/**
# ---------------------------------------------------------------------------

_BADGE_SURFACE_SOURCES = tuple(
    sorted(p for p in EXT_WEBUI_DIR.rglob("*") if p.is_file())
)


def test_badge_surface_ships_only_plugin_owned_files():
    """AC-07: the badge surface is plugin-owned files under extensions/webui/ only."""
    assert _BADGE_SURFACE_SOURCES, "badge surface must ship files under extensions/webui/"
    for path in _BADGE_SURFACE_SOURCES:
        parts = path.relative_to(PLUGIN_ROOT).parts
        assert parts[0] == "extensions" and parts[1] == "webui", (
            f"badge surface file outside plugin-owned extensions/webui/: {path}"
        )


def test_no_timer_or_watch_constructs_in_badge_surface():
    """AC-05: zero setInterval / setTimeout / $watch / requestAnimationFrame."""
    banned = ("setInterval", "setTimeout", "$watch", "requestAnimationFrame", "setImmediate")
    for path in _BADGE_SURFACE_SOURCES:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle in banned:
            assert needle not in text, f"{path.name}: banned timer/watch construct {needle!r}"


def test_badge_network_primitives_confined_to_open_handler():
    """AC-05: the one fetch lives inside onOpen; no other request primitive anywhere."""
    store_js = BADGE_STORE_JS.read_text(encoding="utf-8")
    opens = [m.start() for m in re.finditer(r"fetch\(", store_js)]
    assert opens, "badge store must issue exactly one fetch (page-open projection)"
    onopen = store_js.find("onOpen()")
    assert onopen != -1, "badge store must define onOpen"
    span_end = re.search(r"\n    \w+\(", store_js[onopen + 1 :])
    span = store_js[onopen : onopen + 1 + (span_end.start() if span_end else len(store_js))]
    for hit in opens:
        assert onopen <= hit <= onopen + len(span), (
            "every fetch( must sit inside the onOpen handler span"
        )
    for path in _BADGE_SURFACE_SOURCES:
        if path == BADGE_STORE_JS or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle in ("fetch(", "XMLHttpRequest", "navigator.sendBeacon", "EventSource", "WebSocket"):
            assert needle not in text, f"{path.name}: {needle!r} outside the open handler"


def test_badge_endpoint_path_named_once_inside_open_handler():
    """AC-01/AC-05: the endpoint path is named once, inside onOpen, nowhere else."""
    for path in _BADGE_SURFACE_SOURCES:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        hits = re.findall(r"/api/plugins/[A-Za-z0-9_/-]+", text)
        if path == BADGE_STORE_JS:
            assert len(hits) == 1, f"badge store must name the endpoint exactly once, got {hits}"
            onopen = text.find("onOpen()")
            span_end = re.search(r"\n    \w+\(", text[onopen + 1 :])
            onopen_span = text[onopen : onopen + 1 + (span_end.start() if span_end else len(text))]
            assert hits[0] in onopen_span or "RTK_GAIN_ENDPOINT" in onopen_span, (
                "the endpoint path must sit inside onOpen (or its constant reference)"
            )
        else:
            for hit in hits:
                raise AssertionError(f"{path.name}: unexpected api path {hit!r}")


def test_badge_root_stylesheet_cannot_defeat_x_show():
    """AC-02/AC-03 render reality: the badge root class rule must not set display.

    Alpine x-show removes the inline display property when showing; a
    class-level display:none would keep the root hidden in the ready state
    (CR 4-4 finding, verified against the vendored Alpine build). The hidden
    default must come from the inline pre-Alpine guard only.
    """
    html = BADGE_HTML.read_text(encoding="utf-8")
    rule = re.search(r"\.rtk-gain-badge\s*\{([^}]*)\}", html)
    assert rule, "badge root class rule must exist"
    assert "display" not in rule.group(1), (
        "badge root class rule must not set display (it would defeat x-show)"
    )
    assert 'style="display:none"' in html.replace(" ", ""), (
        "badge root must keep the inline pre-Alpine display:none guard"
    )


# ---------------------------------------------------------------------------
# AC-06 (4-4): zero-load invariant - behavioral half (absence audit)
# ---------------------------------------------------------------------------

def test_badge_adds_no_agent_loop_or_lifecycle_changes():
    """AC-06: zero extensions/python, hooks, scheduled-task, MCP additions."""
    diff = _git(["diff", "--stat", _background_baseline(), "--", "extensions/python", "hooks.py", "scripts", "plugin.yaml", "rtk_pin.txt"])
    if diff is None:
        pytest.skip("git unavailable: absence audit via diff not possible")
    assert diff.strip() == "", f"badge story must not touch background surfaces; diff:\n{diff}"
    manifest = yaml.safe_load((PLUGIN_ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    for banned_key in ("mcp_servers", "mcpServers", "scheduled_tasks", "cron", "hooks_extra"):
        assert banned_key not in manifest, f"manifest must not gain background surface {banned_key!r}"
    assert not (PLUGIN_ROOT / "mcp_servers.json").exists(), "plugin must not ship an MCP server config"
    schedule_markers = ("scheduled_tasks", "SCHEDULED_TASK", "register_scheduled")
    for path in _BADGE_SURFACE_SOURCES:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle in schedule_markers:
            assert needle not in text, f"{path.name}: scheduled-task marker {needle!r}"


# ---------------------------------------------------------------------------
# AC-07 (4-4): no core files touched (scope pin)
# ---------------------------------------------------------------------------

def test_badge_story_touches_no_framework_files():
    """AC-07: the plugin repo cannot contain framework root files (scope freeze)."""
    framework_roots = ("helpers", "agent.py", "initialize.py", "models.py", "run_ui.py", "python")
    for root in framework_roots:
        assert not (PLUGIN_ROOT / root).exists(), f"plugin repo must not contain framework root {root!r}"
    toplevel = _git(["rev-parse", "--show-toplevel"])
    if toplevel is not None:
        assert toplevel.strip() == str(PLUGIN_ROOT), (
            f"git toplevel must be the plugin repo, got {toplevel.strip()!r}"
        )


def test_badge_asset_carries_no_framework_symbols():
    """AC-07/licensing: the badge surface imports no framework symbols."""
    for path in _BADGE_SURFACE_SOURCES:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle in ("helpers.", "from agent", "import agent", "AgentContext", "parallel_tools"):
            assert needle not in text, f"{path.name}: framework symbol {needle!r}"


# ---------------------------------------------------------------------------
# AC-08 (4-4): real-binary E2E, both host states (controlled PATH)
# ---------------------------------------------------------------------------

def test_real_binary_e2e_badge_shows_live_pct(monkeypatch):
    """AC-08: real rtk through the endpoint -> badge renders the live pct."""
    if shutil.which("rtk") is None:
        pytest.skip("real rtk binary not on host PATH (host-state isolation)")
    payload = _endpoint_payload(monkeypatch, _REAL_RTK_PATH)
    print(f"LIVE-E2E badge payload: {json.dumps(payload)}")
    assert payload.get("ok") is True, f"real binary must yield ok payload, got {payload}"
    report = _run_badge_scenario("badge-render-pct", payload)
    assert "error" not in report, f"harness error: {report}"
    assert report.get("visible") is True, f"real payload must show the badge: {report}"
    assert report.get("pct") is not None and report.get("pct") != "n/a", (
        f"badge must render the live pct: {report}"
    )


def test_absent_binary_e2e_badge_hidden(monkeypatch):
    """AC-08: rtk absent on controlled PATH -> badge hidden, zero failures."""
    payload = _endpoint_payload(monkeypatch, _NO_RTK_PATH)
    assert payload.get("ok") is False, f"absent binary must fail-open, got {payload}"
    assert payload.get("rtk_available") is False, f"rtk_available must be false: {payload}"
    report = _run_badge_scenario("badge-fail-matrix", payload)
    assert "error" not in report, f"harness error: {report}"
    assert report.get("visible") is False, f"absent binary must hide the badge: {report}"
    assert report.get("pct") is None, f"absent binary must render no pct: {report}"
