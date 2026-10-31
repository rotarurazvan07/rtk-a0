import shutil
import subprocess

from helpers.extension import Extension

# Single module constant, never an inline literal.
RTK_REWRITE_TIMEOUT = 2.0

# Latches live on the per-session agent data dict, not module state.
RTK_MISSING_WARNED = "rtk_a0_missing_warned"
RTK_GAIN_HINTED = "rtk_a0_gain_hinted"


class RtkIntercept(Extension):
    """Terminal-command gating, rtk rewrite delegation, and fail-open guards.

    `rtk gain` is never invoked by this plugin: savings accounting belongs to
    the RTK CLI.
    """

    def execute(self, tool_name: str = "", tool_args: dict | None = None, **kwargs) -> None:
        # Guard 1: tool identity
        if tool_name != "code_execution_tool":
            return

        # Guard 2: terminal runtime
        if not isinstance(tool_args, dict) or tool_args.get("runtime") != "terminal":
            return

        code = tool_args.get("code")

        # Guard 3: payload sanity
        if not isinstance(code, str) or not code.strip():
            return

        # Guard 4: idempotence
        if code.lstrip().startswith("rtk "):
            return

        # Availability pre-check with once-per-session warning
        if not shutil.which("rtk"):
            if self.agent and not self.agent.data.get(RTK_MISSING_WARNED):
                self.agent.data[RTK_MISSING_WARNED] = True
                self._warn_unavailable()
            return

        # Delegate to `rtk rewrite` via argv-list subprocess
        try:
            result = subprocess.run(
                ["rtk", "rewrite", code],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=RTK_REWRITE_TIMEOUT,
            )
        except Exception:
            # TimeoutExpired and every spawn fault (OSError, oversized argv):
            # original command runs unchanged, no crash.
            return

        # Structural pin: result gate must accept exactly rc 0 and 3
        if result.returncode not in (0, 3):
            return

        rewritten = result.stdout.strip()
        if not rewritten or rewritten == code.strip():
            return

        # Single-source latch with exactly one call site adjacent to mutation
        if self.agent and not self.agent.data.get(RTK_GAIN_HINTED):
            self.agent.data[RTK_GAIN_HINTED] = True
            self._hint_gain()

        # The single mutation point in the entire plugin
        tool_args["code"] = rewritten

    def _warn_unavailable(self) -> None:
        """One-time 'compression inactive' notice after the latched miss."""
        self._notify(
            "warning",
            "rtk unavailable — running uncompressed",
            (
                "The rtk binary was not found on PATH, so terminal commands "
                "run without compression. Install rtk (or re-run plugin setup) "
                "to reactivate; commands continue to work normally either way."
            ),
        )

    def _hint_gain(self) -> None:
        """One-time advisory pointing the user at the rtk CLI's own savings
        report (`rtk gain`) at the first applied rewrite."""
        self._notify(
            "info",
            "rtk compression active — view savings with `rtk gain`",
            (
                "Terminal commands are now compressed by rtk. Ask me to run "
                "`rtk gain` to see per-command and per-session token savings "
                "as tracked by the rtk CLI itself; it runs like any other "
                "command and needs no plugin settings."
            ),
        )

    def _notify(self, kind: str, heading: str, content: str) -> None:
        """One operator notice on the agent log surface; best-effort - a
        broken surface can never break the agent."""
        try:
            self.agent.context.log.log(type=kind, heading=heading, content=content)
        except Exception:
            pass
