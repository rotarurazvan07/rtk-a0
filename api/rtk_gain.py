"""Read-only rtk gain endpoint for the rtk-gain savings surface.

Contract: one auth-gated, POST-only endpoint that invokes the RTK CLI in
argv-list form (`rtk gain --text`), consumes stdout only, strips ANSI
sequences, and always answers with a structured body carrying RTK's own
terminal output. It never raises to the framework, never writes, persists,
or caches anything: RTK remains the sole owner of savings accounting.
"""
import re
import shutil
import subprocess

from helpers.api import ApiHandler, Request, Response

# Single-source timeout for the CLI probe (project constant pattern).
RTK_GAIN_TIMEOUT = 2.0

# Complete ANSI escape stripper: CSI sequences, OSC, and C0 control bytes.
_ANSI = re.compile(r"\u001b\][^\u0007\u001b]*(?:\u0007|\u001b\\)|\u001b\[@-Z\\-_]|\u001b\[[0-?]*[ -/]*[@-~]|[\u0000-\u0008\u000b-\u001f\u007f]")


class RtkGain(ApiHandler):
    """Auth-gated read-only projection of RTK's own savings output."""

    @classmethod
    def requires_auth(cls) -> bool:
        return True

    @classmethod
    def get_methods(cls) -> list:
        return ["POST"]

    async def process(self, input: dict, request: Request) -> dict | Response:
        # Never-raise boundary: every failure mode answers with a structured
        # body; the agent loop and the framework are never touched.
        try:
            return self._collect()
        except Exception as exc:  # hostile payloads, faults, anything
            return self._fail("unexpected-error", f"{type(exc).__name__}")

    # -- helpers ----------------------------------------------------------

    def _collect(self) -> dict:
        if shutil.which("rtk") is None:
            return self._fail("rtk-missing", "rtk binary not found on PATH")
        try:
            proc = subprocess.run(
                # --text: bare `rtk gain` can auto-launch --watch (mutating /
                # interactive); --text forces the plain print form.
                ["rtk", "gain", "--text"],
                capture_output=True,
                timeout=RTK_GAIN_TIMEOUT,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            return self._fail("timeout", f"rtk gain exceeded {RTK_GAIN_TIMEOUT}s")
        except OSError as exc:
            return self._fail("spawn-failed", f"{type(exc).__name__}")
        if proc.returncode != 0:
            return self._fail("nonzero-exit", f"rtk gain exited {proc.returncode}")
        terminal = _ANSI.sub("", proc.stdout or "")
        if not terminal.strip():
            return self._fail("empty-output", "rtk gain produced no output")
        return self._ok(terminal)

    @staticmethod
    def _ok(terminal: str) -> dict:
        first_line = terminal.splitlines()[0].strip() or "RTK savings available"
        return {
            "ok": True,
            "rtk_available": True,
            "message": first_line,
            "error": "",
            "terminal": terminal,
        }

    @staticmethod
    def _fail(reason: str, detail: str) -> dict:
        return {
            "ok": False,
            "rtk_available": reason != "rtk-missing",
            "message": "",
            "error": f"{reason}: {detail}",
            "terminal": "",
        }
