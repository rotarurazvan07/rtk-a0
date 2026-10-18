import shutil
import subprocess

from helpers.extension import Extension

# AC-01/D7/NFR1: single module constant — never an inline literal (architecture pin).
RTK_REWRITE_TIMEOUT = 2.0

# AC-01/FR10/D9: session latch key for the one-time unavailability warning.
RTK_MISSING_WARNED = "rtk_a0_missing_warned"

# Story 3.2 AC-05/FR19/D9: session latch key for the one-time gain-visibility
# notice (emitted at the first applied rewrite, advisory only).
RTK_GAIN_HINTED = "rtk_a0_gain_hinted"


class RtkIntercept(Extension):
    """tool_execute_before extension: terminal-command gating + rtk rewrite
    delegation + full fail-open guard contract (Stories 1.1-1.4) with the
    ADR-006 rc-0/3 acceptance amendment (Story 3.1) and the one-time
    `rtk gain` visibility notice (Story 3.2).

    Four Story 1.2 gates, Story 1.3 delegation with the single mutation
    point, Story 1.4's availability guard with warn-once notification
    (D3 cases 5-7 complete the 10-case contract), Story 3.1's case-8
    amendment (rc 0 and 3 are rewrite-available), and Story 3.2's gain
    hint at the mutation point. `rtk gain` itself is never invoked by
    this plugin (architecture API table).

    Satisfies: AC-01..AC-05 (Story 1.2); AC-01..AC-05 (Story 1.3);
    AC-01..AC-07 (Story 1.4); AC-05 (Story 3.1); AC-05 (Story 3.2).
    """

    def execute(self, tool_name: str = "", tool_args: dict | None = None, **kwargs) -> None:
        """Guard clauses in architecture order (D3 cases 1-5), guarded call
        with explicit timeout + catch-all (cases 6-7), result gates (8-10)."""
        # AC-01 (guard 1, D8, FR3): only code_execution_tool is eligible.
        # Exact equality — no prefix/fuzzy matching on tool names.
        if tool_name != "code_execution_tool":
            return

        if not isinstance(tool_args, dict):
            return

        # AC-02 (guard 2, D8, FR3): only the terminal runtime is eligible;
        # python/nodejs/other payloads pass through untouched.
        if tool_args.get("runtime") != "terminal":
            return

        code = tool_args.get("code")

        # AC-03 (guard 3): payload must be a non-empty string.
        if not isinstance(code, str) or not code.strip():
            return

        # AC-04 (guard 4, FR4, NFR7): idempotence — commands already routed
        # through RTK (`rtk rewrite`, `rtk bypass`, `rtk gain`, ...) are never
        # re-intercepted. Zero subprocess calls (AC-05).
        if code.lstrip().startswith("rtk "):
            return

        # AC-01 (guard 5, FR6, FR10, D3 case 5, D9): availability pre-check —
        # a missing binary skips delegation entirely. First miss per session
        # warns the user once via the agent data latch; self.agent absent
        # (test/headless context) degrades silently. Never a user-facing
        # interruption — the original command always runs.
        if not shutil.which("rtk"):
            if self.agent and not self.agent.data.get(RTK_MISSING_WARNED):
                self.agent.data[RTK_MISSING_WARNED] = True
                self._warn_unavailable()
            return

        # AC-01 (FR1, D2): delegate to `rtk rewrite` via argv-list subprocess.
        # D2: never enable shell mode (injection); timeout is the D7 constant;
        # explicit utf-8 codec keeps decoding locale-independent on the musl
        # target (CR 1.3 defer: C/POSIX-locale containers).
        try:
            result = subprocess.run(
                ["rtk", "rewrite", code],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=RTK_REWRITE_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            # AC-05 (guard 6, D3 case 6, FR8, NFR1): hung binary stalls at
            # most one command — original runs unchanged.
            return
        except Exception:
            # AC-06 (guard 7, D3 case 7, FR9): OSError (e.g. oversized argv)
            # and anything else — original command runs unchanged, no crash.
            return

        # AC-03 (FR5, D3 case 8, amended ADR-006): rc 0 (rules cover
        # everything) AND rc 3 (rewrite printed; allow rules do not cover
        # every command — real rtk 1.0.1 default-container behavior, rtk
        # defers approval to the host) are rewrite-AVAILABLE. rc 1 (no
        # rewrite), rc 2 (deny), every other non-zero rc -> execute exactly
        # as written. Permission approval of the applied rewrite remains
        # Agent Zero's tool-permission layer responsibility (no plugin-side
        # permission logic — Security posture note, architecture.md).
        if result.returncode not in (0, 3):
            return

        # AC-04 (FR5, D3 cases 9-10): empty/stdout-identical reply means rtk
        # decided no compression is needed — compare on equal footing (the
        # original is normalized the same way, CR 1.3 gate fix).
        rewritten = result.stdout.strip()
        if not rewritten or rewritten == code.strip():
            return

        # Story 3.2 AC-05 (FR19, D9): one-time advisory notice on the first
        # applied rewrite — points the user at `rtk gain` for savings
        # visibility. Advisory only: latch-guarded, never re-ordered ahead of
        # any guard, and never blocks the mutation itself. `rtk gain` is NOT
        # invoked by the plugin (architecture API table).
        if self.agent and not self.agent.data.get(RTK_GAIN_HINTED):
            self.agent.data[RTK_GAIN_HINTED] = True
            self._hint_gain()

        # AC-02 (FR1, D1): the single mutation point in the entire plugin —
        # only when rtk produced a different, non-empty command.
        tool_args["code"] = rewritten

    def _warn_unavailable(self) -> None:
        """AC-01/FR10/D9: surface the one-time 'compression inactive' notice
        through the agent's own log (code standard: agent.context.log, never
        print). Guarded: a broken log surface can never break the agent."""
        try:
            self.agent.context.log.log(
                type="warning",
                heading="rtk unavailable — running uncompressed",
                content=(
                    "The rtk binary was not found on PATH, so terminal commands \
 run without compression. Install rtk (or re-run plugin setup) to reactivate; \
 commands continue to work normally either way."
                ),
            )
        except Exception:
            pass  # notification is best-effort; the skip already failed open

    def _hint_gain(self) -> None:
        """Story 3.2 AC-05 (FR19, D9): one-time advisory notice pointing the
        user at RTK's own savings dashboard (`rtk gain`) at the first applied
        rewrite. Discoverability only — the plugin never invokes `rtk gain`
        itself (architecture API table: savings accounting is RTK-owned).
        Uses the agent log surface (never print), latch-guarded by the
        caller, and swallow-broken-surface safe (a failing log can never
        break the agent)."""
        try:
            self.agent.context.log.log(
                type="info",
                heading="rtk compression active — view savings with `rtk gain`",
                content=(
                    "Terminal commands are now compressed by rtk. Ask me to run \
 `rtk gain` to see per-command and per-session token savings as tracked by the \
 rtk CLI itself; it runs like any other command and needs no plugin settings."
                ),
            )
        except Exception:
            pass  # notification is best-effort; the rewrite already applies
