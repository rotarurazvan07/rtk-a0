import shutil
import subprocess

from helpers.extension import Extension

# Single module constant, never an inline literal.
RTK_REWRITE_TIMEOUT = 2.0

# Session latch key for the one-time unavailability warning.
RTK_MISSING_WARNED = "rtk_a0_missing_warned"

# Session latch key for the one-time gain-visibility notice (emitted at the
# first applied rewrite, advisory only).
RTK_GAIN_HINTED = "rtk_a0_gain_hinted"


class RtkIntercept(Extension):
    """tool_execute_before extension: terminal-command gating + rtk rewrite
    delegation + full fail-open guard contract with the rc-0/3
    rewrite-available amendment and the one-time `rtk gain` visibility
    notice.

    Guard clauses, the delegation call with its single mutation point, the
    availability guard with warn-once notification, the rc-0/3 acceptance
    amendment, and the gain hint at the mutation point. `rtk gain` itself is
    never invoked by this plugin: savings accounting belongs to the RTK
    CLI.

    """

    def execute(self, tool_name: str = "", tool_args: dict | None = None, **kwargs) -> None:
        """Guard clauses in order, the guarded delegation call, then the
        result gates."""
        # Only code_execution_tool is eligible; exact equality, no
        # prefix/fuzzy matching on tool names.
        if tool_name != "code_execution_tool":
            return

        if not isinstance(tool_args, dict):
            return

        # Only the terminal runtime is eligible; python/nodejs/other
        # payloads pass through untouched.
        if tool_args.get("runtime") != "terminal":
            return

        code = tool_args.get("code")

        # Payload must be a non-empty string.
        if not isinstance(code, str) or not code.strip():
            return

        # Idempotence: commands already routed through rtk (`rtk rewrite`,
        # `rtk bypass`, `rtk gain`, ...) are never re-intercepted and cost
        # zero subprocess calls.
        if code.lstrip().startswith("rtk "):
            return

        # Availability pre-check: a missing binary skips delegation
        # entirely. The first miss per session warns the user once via the
        # agent data latch; self.agent absent (test/headless context) degrades
        # silently. The original command always runs.
        if not shutil.which("rtk"):
            if self.agent and not self.agent.data.get(RTK_MISSING_WARNED):
                self.agent.data[RTK_MISSING_WARNED] = True
                self._warn_unavailable()
            return

        # Delegate to `rtk rewrite` via an argv-list subprocess: never shell
        # mode (injection); timeout is the module constant; explicit utf-8
        # codec keeps decoding locale-independent on the musl target.
        try:
            result = subprocess.run(
                ["rtk", "rewrite", code],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=RTK_REWRITE_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            # A hung binary stalls at most one command; the original runs
            # unchanged.
            return
        except Exception:
            # OSError (e.g. oversized argv) and anything else: the original
            # command runs unchanged, no crash.
            return

        # rc 0 (rules cover everything) and rc 3 (rtk printed a rewrite but
        # its allow rules do not cover every command - real rtk 1.0.1
        # default-container behavior) are rewrite-available. rc 1 (no
        # rewrite), rc 2 (deny), and every other non-zero rc execute the
        # command exactly as written. Permission approval of an applied
        # rewrite remains the host tool-permission layer's responsibility.
        if result.returncode not in (0, 3):
            return

        # An empty or stdout-identical reply means rtk decided no
        # compression is needed; compare on equal footing (the original is
        # normalized the same way).
        rewritten = result.stdout.strip()
        if not rewritten or rewritten == code.strip():
            return

        # One-time advisory notice on the first applied rewrite, pointing
        # the user at `rtk gain` for savings visibility. Advisory only:
        # latch-guarded, never re-ordered ahead of any guard, never blocks
        # the mutation. `rtk gain` is not invoked by the plugin.
        if self.agent and not self.agent.data.get(RTK_GAIN_HINTED):
            self.agent.data[RTK_GAIN_HINTED] = True
            self._hint_gain()

        # The single mutation point in the entire plugin: only when rtk
        # produced a different, non-empty command.
        tool_args["code"] = rewritten

    def _warn_unavailable(self) -> None:
        """Surface the one-time 'compression inactive' notice through the
        agent's own log surface. Guarded: a broken log can never break the
        agent."""
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
        """One-time advisory notice pointing the user at the rtk CLI's own
        savings report (`rtk gain`) at the first applied rewrite.
        Discoverability only: the plugin never invokes `rtk gain` itself.
        Uses the agent log surface, is latch-guarded by the caller, and is
        broken-surface safe (a failing log can never break the agent)."""
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
