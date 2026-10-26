// RTK chat-header savings badge store (second savings surface, zero-load contract).
// Zero-load invariant: the ONLY entry point is onOpen (chat page open via the
// framework chat-top-start slot). No timers, no watchers, no polling, no
// background refresh - the single fetch below is issued exactly once per open
// and nowhere else; closing the page (x-destroy -> cleanup) issues nothing.

// NOTE: top-level `var` (not `const`) is deliberate: A0 re-creates classic
// script nodes on every mount (webui/js/components.js non-module branch,
// uncached) while module scripts are URL-cached there - a top-level const would
// throw a redeclaration SyntaxError on the second mount. Same shape as the
// page store (webui/rtk-gain-store.js).

// Single-source projection endpoint (POST, auth-gated, read-only; see api/).
// Consumed exactly as Story 4-2 built it - this surface never changes it.
var RTK_GAIN_ENDPOINT = "/api/plugins/rtk_a0/rtk_gain";

// Bounded lifecycle log: repeated open/destroy mounts must not grow it forever.
var RTK_GAIN_EVENTS_MAX = 64;

var createRtkGainBadgeStore = () => {
  const store = {
    status: "idle", // idle | loading | ready | hidden
    summary: null, // endpoint summary (read-only; never recomputed)
    source: "none", // json | text (which parse produced the summary)
    message: "", // RTK's own headline string (endpoint-provided, verbatim)
    pct: null, // display pct string, one format, from the endpoint's own value
    events: [], // bounded lifecycle log
    _mount: 0, // monotonic mount generation (stale-response guard)

    onOpen() {
      // AC-01: exactly one open per mount; re-entry is a no-op
      if (store.status !== "idle") return;
      store.status = "loading";
      store._emit("open");
      store._emit("status", "idle", "loading");
      if (typeof fetch !== "function") {
        // no client in this runtime: degrade to hidden (never error chrome)
        store._hide();
        return;
      }
      // mount generation: cleanup() bumps this token so a response from a
      // torn-down mount can never write into a fresh one (CR 4-3 lesson)
      const mount = ++store._mount;
      store._inflight = fetch(RTK_GAIN_ENDPOINT, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      })
        .then((response) => response.json())
        .then((data) => {
          if (mount !== store._mount) return; // stale-response guard
          store.handle(data);
        })
        .catch(() => {
          // network-level failure: degrade to hidden, no chrome, no retry
          if (mount !== store._mount) return;
          store._hide();
        });
      return store._inflight;
    },

    handle(data) {
      // consume the endpoint contract unchanged; advance only from loading
      if (store.status !== "loading") return;
      const pct = store._renderablePct(data);
      if (pct === null) {
        // fail / unavailable / empty / unrenderable -> hidden (AC-03)
        store._hide();
        return "hidden";
      }
      store.summary = (data && data.summary) || {};
      store.source = String((data && data.source) || "json");
      store.message = String((data && data.message) || "RTK savings available");
      store.pct = pct;
      store.status = "ready";
      store._emit("status", "loading", "ready");
      return "ready";
    },

    complete() {
      // test/transition seam: fetch-less resolution of the loading state
      // (mirrors the page store seam; renders via the last handle() verdict)
      if (store.status !== "loading") return;
      store._hide();
    },

    badgePct() {
      // AC-02: the display pct, one format, straight from the endpoint's own
      // value - never recomputed from counts, never double-rounded.
      return store.status === "ready" ? store.pct : null;
    },

    visible() {
      // AC-03: the badge renders only for a ready, renderable payload.
      return store.status === "ready" && store.pct !== null;
    },

    _renderablePct(data) {
      // null unless the payload carries a finite pct with real activity:
      // ok must be exactly true, summary an object, commands a positive
      // number (empty ledger -> hidden), pct a finite number (bool/NaN/
      // Infinity/string/null -> hidden). No re-computation of any value.
      if (!data || data.ok !== true) return null;
      const summary = data.summary;
      if (!summary || typeof summary !== "object") return null;
      if (typeof summary.total_commands !== "number") return null;
      if (!isFinite(summary.total_commands) || summary.total_commands <= 0) return null;
      const num = (v) => typeof v === "number" && isFinite(v);
      if (store.source === "text" || (data.source && String(data.source) === "text")) {
        return num(summary.savings_pct_text) ? summary.savings_pct_text.toFixed(1) + "%" : null;
      }
      return num(summary.avg_savings_pct) ? summary.avg_savings_pct.toFixed(1) + "%" : null;
    },

    _hide() {
      // degrade-to-hidden: no error chrome, no blank box, no retry state
      store.summary = null;
      store.source = "none";
      store.message = "";
      store.pct = null;
      store.status = "hidden";
      store._emit("hidden");
    },

    _emit(type, from, to) {
      const entry = to === undefined ? { type } : { type, from, to };
      store.events.push(entry);
      if (store.events.length > RTK_GAIN_EVENTS_MAX) store.events.shift();
    },

    cleanup() {
      // x-destroy teardown: bump the mount generation (stale responses die)
      // and reset the machine so a fresh mount starts clean.
      store._mount += 1;
      store.status = "idle";
      store.summary = null;
      store.source = "none";
      store.message = "";
      store.pct = null;
      store.events = [];
    },
  };
  return store;
};

globalThis.createRtkGainBadgeStore = createRtkGainBadgeStore;

var _rtkGainBadgeStore = createRtkGainBadgeStore();
if (typeof Alpine !== "undefined" && Alpine.store) {
  Alpine.store("rtkGainBadge", _rtkGainBadgeStore);
}
