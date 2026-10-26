"""Read-only rtk gain endpoint for the rtk-gain savings surface.

Contract: one auth-gated, POST-only endpoint that invokes the RTK CLI in
argv-list form, consumes stdout only, and always answers with a structured
body. It never raises to the framework, never writes, persists, or caches
anything: RTK remains the sole owner of savings accounting.
"""
import json
import re
import shutil
import subprocess

from helpers.api import ApiHandler, Request, Response

# Single-source timeout for the CLI probe (project constant pattern).
RTK_GAIN_TIMEOUT = 2.0

# Fields of the live `rtk gain --format json` summary object, captured from a
# live rtk 1.0.1 run (2026-10-25) and recorded in the story Dev Agent Record.
_REQUIRED_SUMMARY_KEYS = (
    "total_commands",
    "total_input",
    "total_output",
    "total_saved",
    "avg_savings_pct",
    "total_time_ms",
    "avg_time_ms",
    "oversized_excluded",
)

# Anchors of the live text summary (`rtk gain --text`), same capture session.
_TEXT_HEADLINE = re.compile(
    r"^Tokens saved:\s+(?P<saved>\S+) \((?P<pct>\d+(?:\.\d+)?)%\)\s*$", re.MULTILINE
)
_TEXT_COMMANDS = re.compile(r"^Total commands:\s+(?P<count>\d+)\s*$", re.MULTILINE)
_TEXT_INPUT = re.compile(r"^Input tokens:\s+(?P<value>\S+)\s*$", re.MULTILINE)
_TEXT_OUTPUT = re.compile(r"^Output tokens:\s+(?P<value>\S+)\s*$", re.MULTILINE)
_TEXT_EXEC = re.compile(r"^Total exec time:\s+(?P<value>.+?)\s*$", re.MULTILINE)
_TEXT_EXCLUDED = re.compile(r"^Excluded:\s+(?P<count>\d+)\s+oversized", re.MULTILINE)

class RtkGain(ApiHandler):
    """Auth-gated read-only projection of RTK's own savings accounting."""

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
                ["rtk", "gain", "--format", "json", "--text"],
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
        parsed = _parse_summary(proc.stdout)
        if parsed is None:
            return self._fail("unparseable-output", "rtk gain output could not be parsed")
        return self._ok(parsed)

    @staticmethod
    def _ok(summary: dict) -> dict:
        headline = _format_headline(summary)
        return {
            "ok": True,
            "rtk_available": True,
            "placeholder": False,
            "message": headline,
            "error": "",
            "summary": summary,
            "source": "json" if "avg_savings_pct" in summary else "text",
        }

    @staticmethod
    def _fail(reason: str, detail: str) -> dict:
        return {
            "ok": False,
            "rtk_available": reason != "rtk-missing",
            "placeholder": False,
            "message": "",
            "error": f"{reason}: {detail}",
            "summary": {},
            "source": "none",
        }


def _validate_summary_keys(data: dict) -> dict | None:
    """Return the summary object when every required key is present, else None."""
    summary = data.get("summary")
    if not isinstance(summary, dict):
        return None
    for key in _REQUIRED_SUMMARY_KEYS:
        if key not in summary:
            return None
    return {key: summary[key] for key in _REQUIRED_SUMMARY_KEYS}


def _format_headline(summary: dict) -> str:
    """Build the one-line message using the live payload values (no double rounding)."""
    saved = summary.get("total_saved")
    pct = summary.get("avg_savings_pct")
    if isinstance(saved, (int, float)) and isinstance(pct, (int, float)):
        return f"Tokens saved: {saved} ({pct:.1f}%)"
    return "RTK savings available"


def _parse_text_summary(text: str) -> dict | None:
    """Anchored-text fallback against the captured live text shape."""
    headline = _TEXT_HEADLINE.search(text)
    if not headline:
        return None
    commands = _TEXT_COMMANDS.search(text)
    if not commands:
        return None
    summary = {
        "total_commands": int(commands.group("count")),
        "headline": headline.group(0).strip(),
        "saved_text": headline.group("saved"),
        "savings_pct_text": float(headline.group("pct")),
    }
    for pattern, key in (
        (_TEXT_INPUT, "input_text"),
        (_TEXT_OUTPUT, "output_text"),
        (_TEXT_EXEC, "exec_time_text"),
        (_TEXT_EXCLUDED, "oversized_excluded"),
    ):
        match = pattern.search(text)
        if match:
            value = match.group("count") if key == "oversized_excluded" else match.group("value")
            summary[key] = int(value) if key == "oversized_excluded" else value
    return summary


def _parse_summary(stdout: str) -> dict | None:
    """Tolerant parse: json first (unknown keys ignored), then text fallback."""
    text = (stdout or "").strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict):
            validated = _validate_summary_keys(data)
            if validated is not None:
                return validated
    return _parse_text_summary(text)
