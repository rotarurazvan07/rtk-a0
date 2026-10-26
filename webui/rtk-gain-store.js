// RTK savings page store (slash-command surface, zero-load contract).
// Zero-load invariant: the ONLY entry point is onOpen (page open). No timers,
// no watchers, no polling, no background refresh - the single fetch below is
// issued exactly once per open and nowhere else.

// NOTE: top-level `var` (not `const`) is deliberate: A0 re-creates classic
// script nodes on every modal open (webui/js/components.js non-module branch,
// uncached) while module scripts are URL-cached there - a top-level const would
// throw a redeclaration SyntaxError on the second open. Bundled precedents
// (_whats_new, _kokoro_tts) use module scripts instead; this shell keeps the
// classic-script shape its node test harness drives.

// Single-source projection endpoint (POST, auth-gated, read-only; see api/).
var RTK_GAIN_ENDPOINT = "/api/plugins/rtk_a0/rtk_gain";

// Bounded lifecycle log: repeated open/destroy mounts must not grow it forever
// (events[] discipline). 64 entries is far beyond any diagnostic need.
var RTK_GAIN_EVENTS_MAX = 64;

// Friendly copy for the notice states (the endpoint answers structured bodies;
// raw reason strings never reach the user - consumed at this layer).
var RTK_GAIN_NOTICES = {
  "rtk-missing":
    "The RTK binary was not found on PATH, so there are no savings to show " +
    "yet. Re-run setup from the plugin directory to provision it - see the " +
    "Troubleshooting section of the README.",
  "timeout":
    "RTK did not answer in time, so the summary could not be read. RTK's own " +
    "accounting is unchanged - try opening the page again.",
  "spawn-failed":
    "RTK could not be started, so the summary could not be read. Try opening " +
    "the page again - see the README Troubleshooting section if it repeats.",
  "nonzero-exit":
    "RTK exited with an error this time, so the summary could not be read. " +
    "RTK's own accounting is unchanged - try opening the page again.",
  "unparseable-output":
    "RTK's output could not be read in a known format. RTK's own accounting " +
    "is unchanged - try opening the page again.",
  "unexpected-error":
    "The savings summary could not be read this time. Agent Zero and RTK are " +
    "unaffected - try opening the page again.",
};

var createRtkGainStore = () => {
  const store = {
    status: "idle", // idle | loading | ready | error
    notice: "", // friendly copy for the ready-but-nothing-to-show states
    message: "", // RTK's own headline string (endpoint-provided, verbatim)
    error: "", // network-level failure text (error state, not a notice)
    view: { view: "loading" }, // render projection for the page
    summary: null, // endpoint summary (rendered as-is, never recomputed)
    source: "none", // json | text (which parse produced the summary)
    events: [], // bounded lifecycle log
    _mount: 0, // monotonic mount generation (stale-response guard)

    onOpen() {
      // AC-01: exactly one open per mount; re-entry is a no-op
      if (store.status !== "idle") return;
      store.status = "loading";
      store._emit("open");
      store._emit("status", "idle", "loading");
      if (typeof fetch !== "function") {
        store.fail("endpoint client unavailable in this runtime");
        return;
      }
      // CR 4-3 E1/E2: mount generation - cleanup() bumps this token so a
      // response from a torn-down mount can never write into a fresh one
      const mount = ++store._mount;
      store._inflight = fetch(RTK_GAIN_ENDPOINT, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      })
        .then((response) => response.json())
        .then((data) => {
          // stale-write guard: only this mount's response may drive state
          if (mount !== store._mount) return;
          store.handle(data);
        })
        .catch(() => {
          // network-level failure - distinct from endpoint notice states
          if (mount !== store._mount) return;
          store.fail("savings summary could not be fetched");
        });
      // the chain itself: awaited by the open lifecycle so the page renders
      // after the response lands (node harness scenario contract)
      return store._inflight;
    },

    handle(data) {
      // consume the endpoint contract: ok+summary -> headline/empty-ledger,
      // anything else -> a friendly notice (never raw error chrome).
      // CR 4-3 E1: a response from a torn-down mount (or any non-loading
      // state) is dropped here - the machine only advances from loading
      if (store.status !== "loading") return;
      if (data && data.ok === true && data.summary) {
        store.summary = data.summary;
        store.source = String(data.source || "json");
        store.message = String(data.message || "RTK savings available");
        store.notice = "";
        store.status = "ready";
        store._emit("status", "loading", "ready");
        return "ready";
      }
      const reason = String((data && data.error) || "").split(":")[0].trim();
      if (data && data.rtk_available === false) {
        store.notice = RTK_GAIN_NOTICES["rtk-missing"];
      } else if (RTK_GAIN_NOTICES[reason]) {
        store.notice = RTK_GAIN_NOTICES[reason];
      } else {
        store.notice = RTK_GAIN_NOTICES["unexpected-error"];
      }
      store.summary = null;
      store.status = "ready";
      store._emit("status", "loading", "ready");
      return "notice";
    },

    complete() {
      // test/transition seam: fetch-less resolution of the loading state
      if (store.status !== "loading") return;
      store.status = "ready";
      store._emit("status", "loading", "ready");
    },

    fail(message) {
      if (store.status !== "loading") return;
      store.status = "error";
      store.error = String(message || "unknown error");
      store._emit("status", "loading", "error");
    },

    renderView() {
      // pure projection of state -> what the page renders. The headline is the
      // endpoint's own string verbatim; pct gets ONE display format and is
      // never recomputed from counts; supporting fields are display-level
      // humanization only. Non-finite/non-numeric values render as "n/a".
      if (store.status !== "ready") {
        if (store.status === "error") return { view: "error", error: store.error };
        return { view: "loading" };
      }
      const summary = store.summary || {};
      // CR 4-3 E4: n/a for anything that is not a finite number; a string
      // keeps its verbatim value (RTK's own text formatting) but
      // NaN/Infinity/booleans never surface as page literals
      const num = (v) => typeof v === "number" && isFinite(v);
      const group = (v) => {
        if (typeof v === "number") return isFinite(v) ? v.toLocaleString("en-US") : "n/a";
        if (typeof v === "boolean" || v == null) return "n/a";
        return String(v);
      };
      // a specific fail-state notice always wins over any data-driven state
      if (store.notice) return { view: "notice", notice: store.notice };
      // D6 + CR 4-3 E3: no numeric activity at all (empty object, missing
      // counters, or all-zero) is a distinct friendly state, not a headline
      // of n/a fields
      if (
        !summary ||
        typeof summary.total_commands !== "number" ||
        Number(summary.total_commands) === 0 ||
        Number(summary.total_input) === 0
      ) {
        return {
          view: "notice",
          notice:
            "RTK has no tracked commands yet. Savings appear here once " +
            "commands run through the plugin - the numbers come from RTK's " +
            "own accounting.",
        };
      }
      const fields = [];
      if (store.source === "text") {
        // AC-03: text-fallback fields render RTK's parsed strings as-is
        fields.push({ label: "Tokens saved", value: String(summary.saved_text || "n/a") });
        fields.push({
          label: "Avg savings",
          value: num(summary.savings_pct_text) ? summary.savings_pct_text.toFixed(1) + "%" : "n/a",
        });
        fields.push({ label: "Total commands", value: group(summary.total_commands) });
        if (summary.input_text != null)
          fields.push({ label: "Input tokens", value: String(summary.input_text) });
        if (summary.output_text != null)
          fields.push({ label: "Output tokens", value: String(summary.output_text) });
        if (summary.exec_time_text != null)
          fields.push({ label: "Exec time", value: String(summary.exec_time_text) });
        if (summary.oversized_excluded != null)
          fields.push({ label: "Excluded (oversized)", value: group(summary.oversized_excluded) });
      } else {
        // json source: raw numeric fields, display-level humanization only
        fields.push({ label: "Tokens saved", value: group(summary.total_saved) });
        fields.push({
          label: "Avg savings",
          value: num(summary.avg_savings_pct) ? summary.avg_savings_pct.toFixed(1) + "%" : "n/a",
        });
        fields.push({ label: "Total commands", value: group(summary.total_commands) });
        fields.push({ label: "Input tokens", value: group(summary.total_input) });
        fields.push({ label: "Output tokens", value: group(summary.total_output) });
        fields.push({
          label: "Exec time",
          value: num(summary.total_time_ms) ? (summary.total_time_ms / 1000).toFixed(1) + " s" : "n/a",
        });
        fields.push({ label: "Excluded (oversized)", value: group(summary.oversized_excluded) });
      }
      return { view: "headline", headline: store.message || "RTK savings available", fields };
    },

    _emit(type, from, to) {
      const entry = to === undefined ? { type } : { type, from, to };
      store.events.push(entry);
      if (store.events.length > RTK_GAIN_EVENTS_MAX) store.events.shift();
    },

    cleanup() {
      // x-destroy teardown: a fresh mount starts from a clean machine
      store.status = "idle";
      store.notice = "";
      store.message = "";
      store.error = "";
      store.view = { view: "loading" };
      store.summary = null;
      store.source = "none";
      store.events = [];
    },
  };
  return store;
};

globalThis.createRtkGainStore = createRtkGainStore;

var _rtkGainStore = createRtkGainStore();
if (typeof Alpine !== "undefined" && Alpine.store) {
  Alpine.store("rtkGain", _rtkGainStore);
}

