"""RTK plugin lifecycle hooks (Agent Zero plugin contract).

Real interface verified against the framework (cite if changed):
- helpers/plugins.py:908-936 call_plugin_hook loads this file by path and
  dispatches getattr(hooks, hook_name) with NO arguments; a missing hook
  falls back to its default.
- plugins/_plugin_installer/helpers/install.py:298-302 runs install/pre_update;
  helpers/plugins.py:411-414 runs uninstall before deleting the plugin dir.
- helpers/functions.py safe_call adapts arguments but never catches
  exceptions, and the installer re-raises them - so every hook below
  self-guards and warns instead of failing the plugin lifecycle.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent
# Provisioning lives in scripts/setup.sh; hooks only wire it.
_SETUP_SCRIPT = _PLUGIN_DIR / "scripts" / "setup.sh"
# Container OS bin dir - never the framework venv. Overridable via
# RTK_INSTALL_DIR (the same knob setup.sh honors); the fallback keeps the
# lifecycle PATH-append and the uninstall message working by default.
INSTALL_DIR = "/usr/local/bin"
# Pin is single-source in rtk_pin.txt; hooks never read versions.
_SETUP_TIMEOUT = 120  # seconds; download+verify+install worst case on slow links
# Version probe is trivially fast (a --version print); 10s is generous.
_VERSION_TIMEOUT = 10
# Concurrent lifecycle callers share the setup.sh fixed staging path -
# serialize in-process callers. The update path calls pre_update then
# install sequentially.
_SETUP_LOCK = threading.RLock()


class _Style:
    """Minimal operator-visible logging (stderr; A0 surfaces hook output)."""

    @staticmethod
    def _emit(level: str, *args) -> None:
        try:
            print("rtk hooks: " + level + ": " + " ".join(str(a) for a in args), file=sys.stderr)
        except Exception:
            # A closed stderr raises ValueError, not OSError; swallow every
            # writer failure so logging can never break a hook.
            pass

    def info(self, *args) -> None:
        self._emit("INFO", *args)

    def warning(self, *args) -> None:
        self._emit("WARNING", *args)

    def error(self, *args) -> None:
        self._emit("ERROR", *args)


_style = _Style()


def _desc(exc: BaseException) -> str:
    """Fault-proof exception text: an exception whose __str__ raises must
    not turn a handled degradation into an escape."""
    try:
        return str(exc)
    except Exception:
        try:
            return type(exc).__name__ + " (unprintable)"
        except Exception:
            return "<unprintable exception>"


def _install_dir() -> str:
    """Effective install dir: RTK_INSTALL_DIR when set, else the default.

    setup.sh honors the same override; resolving it here keeps the lifecycle
    PATH-append and the uninstall message in sync with the script.
    """
    return os.environ.get("RTK_INSTALL_DIR") or INSTALL_DIR


def _setup_env() -> dict[str, str]:
    """Environment for setup.sh; guarantees the install dir on PATH.

    The setup self-check resolves rtk through PATH; if the
    lifecycle process PATH lacks the install dir the check would fail after a
    successful install. Append the dir when absent; never duplicate an
    existing entry.
    """
    env = dict(os.environ)
    path = env.get("PATH", os.defpath)
    parts = path.split(os.pathsep) if path else []
    install_dir = _install_dir()
    if install_dir not in parts:
        parts.append(install_dir)
    env["PATH"] = os.pathsep.join(parts)
    return env


def _run_setup():
    with _SETUP_LOCK:
        return subprocess.run(
            ["bash", str(_SETUP_SCRIPT)],
            capture_output=True,
            text=True,
            # Explicit encoding: text=True decodes strictly in the interpreter
            # locale and a non-ASCII setup byte would raise UnicodeDecodeError
            # (a ValueError, not an OSError) from inside subprocess.run;
            # utf-8+replace keeps the hook raise-free.
            encoding="utf-8",
            errors="replace",
            timeout=_SETUP_TIMEOUT,
            env=_setup_env(),
        )


def _setup_output(result) -> str:
    """Combine setup output so the operator sees the provisioning verdict."""
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    return " | ".join(part for part in (out, err) if part)


def _tail(output: str) -> str:
    """Suffix with the child output when non-empty."""
    return (": " + output) if output else ""


def _emergency(msg: str) -> None:
    """Last-resort stderr write used when the logging surface itself fails.

    If _style is broken (e.g. an injected raising sink), this still cannot
    raise, so no hook can ever escape and no announced action is aborted by
    its own status line. Bypasses _style deliberately - it is the single
    place where the swallow must not depend on the injected sink.
    """
    try:
        print("rtk hooks: " + msg, file=sys.stderr)
    except Exception:
        pass


def _say(level: str, msg: str) -> None:
    """Operator message that cannot fail or abort the action it announces.

    Primary path is the _style surface (whose _emit swallows internally);
    if that surface is itself broken, fall back to the guarded stderr write.
    """
    try:
        getattr(_style, level)(msg)
    except Exception:
        _emergency(msg)


def _report_setup(rc: int, output: str, verb: str) -> None:
    """Map a setup.sh exit code to the operator message (one taxonomy).

    Mirrors the exit contract documented in scripts/setup.sh: usage (2),
    availability (126/127/negative), and everything else integrity.
    """
    if rc == 0:
        # Provisioned, idempotent skip, or warned-unavailable.
        _say("info", "rtk " + verb + " complete" + _tail(output))
    elif rc == 2:
        _say("warning", "rtk " + verb + " usage error (rc=2)" + _tail(output))
    elif rc in (126, 127) or rc < 0:
        # Missing/non-executable script (126/127) or a signal-killed child
        # (negative rc) is availability-shaped, not integrity corruption.
        _say("warning", "rtk " + verb + " could not run (rc=" + str(rc) + ")" + _tail(output))
    else:
        _say(
            "warning",
            "rtk " + verb + " reported integrity/format failure (rc=" + str(rc) + ") - inspect manually"
            + _tail(output),
        )


def install() -> None:
    """A0 install hook: provision RTK via scripts/setup.sh, warn-not-fail.

    Every outcome (0 provisioned-or-warned, 2 usage, other
    integrity-corrupt, or a raised exception) returns without failing the
    plugin installation.
    """
    try:
        result = _run_setup()
    except subprocess.TimeoutExpired:
        _say("warning", "rtk setup exceeded " + str(_SETUP_TIMEOUT) + "s; agent runs uncompressed")
        return
    except Exception as exc:
        # Nothing may escape install(): the installer re-raises hook
        # exceptions, so any failure (missing bash/script, spawn fault, or
        # unexpected error) degrades to a warning.
        _say("warning", "rtk setup could not run (" + _desc(exc) + "); agent runs uncompressed")
        return
    _report_setup(result.returncode, _setup_output(result), "setup")


def _installed_version() -> str | None:
    """Probe the installed rtk version token; raises when rtk is unusable.

    Probes via argv list (never shell mode) with the same PATH the setup
    self-check uses (install dir appended); explicit utf-8 decoding keeps a
    non-ASCII byte from raising a decode error. Real output is ``rtk 1.0.1``
    (no v-prefix); the version is the last whitespace token of the last
    non-empty line. Raises on absent binary / spawn fault / timeout; returns
    None only for empty output.
    """
    result = subprocess.run(
        ["rtk", "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_VERSION_TIMEOUT,
        env=_setup_env(),
    )
    lines = [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]
    if not lines:
        return None
    # Parse FIRST non-empty line for consistency with setup.sh exact-token grep
    # (setup.sh: grep -Fxq over full output; rtk prints "rtk X.Y.Z" on first line)
    first = lines[0]
    token = first.split()[-1]
    if token.startswith("v"):
        token = token[1:]
    # Validate semver-ish shape; fall back to None to trigger drift re-provision
    if not token or not any(c.isdigit() for c in token):
        return None
    return token


def _pin_version() -> str:
    """Read the pin file (rtk_pin.txt); v-prefix stripped. Raises when the
    file is missing or unreadable (plugin damage, not a version match)."""
    raw = (_PLUGIN_DIR / "rtk_pin.txt").read_text(encoding="utf-8").strip()
    return raw[1:] if raw.startswith("v") else raw


def _ensure_pinned_rtk(context: str) -> None:
    """Shared drift-check + re-provision path (single source of truth).

    Probes the installed rtk token against rtk_pin.txt and runs setup.sh
    only when the binary is absent, the pin is unreadable, or the versions
    differ. Both the update lifecycle (pre_update) and the boot-time
    entrypoint (ensure_provisioned) delegate here, so the probe/pin logic
    exists exactly once. Every message path is fault-proof (_desc/_say);
    a comparison fault propagates to the caller's outer guard by design.
    """
    try:
        installed = _installed_version()
    except Exception as exc:
        # Any probe failure (absent binary, OSError, timeout, or a
        # ValueError/TypeError-family fault) is unreadable = drift.
        installed = None
        _say("warning", "rtk version probe failed (" + _desc(exc) + "); re-checking via setup")
    try:
        pin = _pin_version()
    except Exception as exc:
        # An unreadable pin is plugin damage, not a version match; warn
        # distinctly and still attempt re-provisioning.
        pin = None
        _say("warning", "rtk pin unreadable (" + _desc(exc) + "); re-checking via setup")
    if installed is not None and pin is not None and installed == pin:
        _say("info", "rtk " + installed + " matches pin; " + context + " leaves binary untouched")
        return
    if installed is None:
        _say("info", "rtk binary unavailable or unreadable; running setup to re-provision")
    elif pin is None:
        _say("info", "rtk pin unreadable; running setup to re-provision")
    else:
        _say("info", "rtk " + installed + " != pin " + pin + "; running setup to re-provision")
    try:
        result = _run_setup()
    except Exception as exc:
        # A failing re-provision must not fail the lifecycle that called it.
        _say("warning", "rtk re-provisioning failed (" + _desc(exc) + "); agent runs as-is")
        return
    _report_setup(result.returncode, _setup_output(result), "re-provision")


def pre_update() -> None:
    """A0 pre_update hook: re-provision only on version drift.

    Installed rtk --version vs rtk_pin.txt, token-exact: equal -> keep the
    binary (no setup invocation); absent/unreadable/different -> drift ->
    re-run setup.sh (which itself warns-not-fails, and idempotently skips
    when already correct). Never raises: the framework never catches hook
    exceptions and the installer re-raises them.
    """
    # The outer guard is fault-proof: every inner handler builds its message
    # via _desc(), so nothing can escape this hook.
    try:
        _ensure_pinned_rtk("update")
    except Exception as exc:
        _say("warning", "rtk pre_update degraded (" + _desc(exc) + "); update continues")


def ensure_provisioned(raise_on_error: bool = False) -> None:
    """Boot-time re-provisioning entrypoint (framework startup migration).

    Called at framework boot by the plugin's startup_migration extension
    (extensions/python/startup_migration/) through call_plugin_hook, so a
    container restart or image rebuild that lost the user-owned binary is
    repaired without human action. Shares the probe/pin/drift classification
    and the setup invocation with pre_update (single source of truth); the
    same never-raise, warn-not-fail, timeout, and exit-code taxonomy apply.
    The correct-match path is a single cheap version probe: no writes, no
    downloads, zero setup invocations. The framework caller passes NO args;
    safe_call drops undeclared kwargs, so raise_on_error only reaches the
    hook because it is declared. This hook never raises regardless. install/
    uninstall/pre_update semantics are unchanged by this entrypoint.
    """
    try:
        _ensure_pinned_rtk("boot check")
    except Exception as exc:
        _say("warning", "rtk boot re-provisioning degraded (" + _desc(exc) + "); startup continues")


def uninstall() -> None:
    """A0 uninstall hook: never touches the user-owned RTK binary.

    A0 deletes the plugin directory itself after this hook returns:
    uninstall_plugin -> call_plugin_hook("uninstall") then delete_plugin
    (helpers/plugins.py:409-414 -> :415-444). This plugin owns no files
    outside its directory except the user-owned binary, so the correct hook
    is a guarded, informative no-op - it removes nothing and never raises.
    """
    try:
        _say(
            "info",
            "rtk uninstall: plugin files removed by A0; user-owned rtk left at "
            + _install_dir() + "/rtk",
        )
    except Exception as exc:
        _say("warning", "rtk uninstall degraded (" + _desc(exc) + ")")
