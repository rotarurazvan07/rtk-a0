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
# Container OS bin dir - never the framework venv.
INSTALL_DIR = "/usr/local/bin"
# Pin is single-source in rtk_pin.txt; hooks never read versions.
_SETUP_TIMEOUT = 120  # seconds; download+verify+install worst case on slow links
# Concurrent lifecycle callers share the setup.sh fixed staging path -
# serialize in-process callers. The update path calls pre_update then
# install sequentially.
_SETUP_LOCK = threading.RLock()


class _Style:
    """Minimal operator-visible logging (stderr; A0 surfaces hook output)."""

    @staticmethod
    def _emit(level, *args):
        try:
            print("rtk hooks: " + level + ": " + " ".join(str(a) for a in args), file=sys.stderr)
        except Exception:
            # A closed stderr raises ValueError, not OSError; swallow every
            # writer failure so logging can never break a hook.
            pass

    def info(self, *args, **kwargs):
        self._emit("INFO", *args)

    def warning(self, *args, **kwargs):
        self._emit("WARNING", *args)

    def error(self, *args, **kwargs):
        self._emit("ERROR", *args)


_style = _Style()


def _warn(msg):
    """Warning that cannot itself fail the hook."""
    try:
        _style.warning(msg)
    except Exception:
        pass


def _desc(exc):
    """Fault-proof exception text: an exception whose __str__ raises must
    not turn a handled degradation into an escape."""
    try:
        return str(exc)
    except Exception:
        try:
            return type(exc).__name__ + " (unprintable)"
        except Exception:
            return "<unprintable exception>"


def _info(msg):
    """Status line that cannot abort the action it announces."""
    try:
        _style.info(msg)
    except Exception:
        pass

def _setup_env():
    """Environment for setup.sh; guarantees INSTALL_DIR on PATH.

    The setup self-check resolves rtk through PATH; if the
    lifecycle process PATH lacks /usr/local/bin the check would fail after a
    successful install. Append the pinned install dir when absent; never
    duplicate an existing entry.
    """
    env = dict(os.environ)
    path = env.get("PATH", os.defpath)
    parts = path.split(os.pathsep) if path else []
    if INSTALL_DIR not in parts:
        parts.append(INSTALL_DIR)
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


def _surface(result):
    """Combine setup output so the operator sees the provisioning verdict."""
    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    return " | ".join(part for part in (out, err) if part)


def install():
    """A0 install hook: provision RTK via scripts/setup.sh, warn-not-fail.

    Every outcome (0 provisioned-or-warned, 2 usage, other
    integrity-corrupt, or a raised exception) returns without failing the
    plugin installation.
    """
    try:
        result = _run_setup()
    except FileNotFoundError as exc:
        # bash or the setup script itself is unavailable on this host.
        _style.warning("rtk setup could not run (" + _desc(exc) + "); agent runs uncompressed")
        return
    except subprocess.TimeoutExpired:
        _style.warning("rtk setup exceeded " + str(_SETUP_TIMEOUT) + "s; agent runs uncompressed")
        return
    except OSError as exc:
        _style.warning("rtk setup failed (" + _desc(exc) + "); agent runs uncompressed")
        return
    except Exception as exc:
        # Nothing may escape install(): the installer re-raises hook
        # exceptions, so any unexpected failure degrades to a warning.
        _style.warning("rtk setup could not run (" + _desc(exc) + "); agent runs uncompressed")
        return

    rc = result.returncode
    output = _surface(result)
    if rc == 0:
        # Provisioned, idempotent skip, or warned-unavailable.
        _style.info("rtk setup complete" + ((": " + output) if output else ""))
        return
    if rc == 2:
        _style.warning("rtk setup usage error (rc=2)" + ((": " + output) if output else ""))
        return
    if rc in (126, 127) or rc < 0:
        # Missing/non-executable script (126/127) or a signal-killed child
        # (negative rc) is availability-shaped, not integrity corruption.
        _style.warning("rtk setup could not run (rc=" + str(rc) + ")" + ((": " + output) if output else ""))
        return
    _style.warning(
        "rtk setup reported integrity/format failure (rc=" + str(rc) + ") - inspect manually"
        + ((": " + output) if output else "")
    )


# Version probe is trivially fast (a --version print); 10s is generous.
_VERSION_TIMEOUT = 10


def _installed_version():
    """Return the installed rtk version token, or None when unavailable.

    Probes via argv list (never shell mode) with the same PATH the setup
    self-check uses (INSTALL_DIR appended); explicit utf-8 decoding keeps a
    non-ASCII byte from raising. Real output is ``rtk 1.0.1`` (no v-prefix);
    the version is the last whitespace token of the last non-empty line.
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
    token = lines[-1].split()[-1]
    if token.startswith("v"):
        token = token[1:]
    return token or None


def _pin_version():
    """Read the pin file (rtk_pin.txt); v-prefix stripped."""
    raw = (_PLUGIN_DIR / "rtk_pin.txt").read_text(encoding="utf-8").strip()
    return raw[1:] if raw.startswith("v") else raw


def pre_update():
    """A0 pre_update hook: re-provision only on version drift.

    Installed rtk --version vs rtk_pin.txt, token-exact: equal -> keep the
    binary (no setup invocation); absent/unreadable/different -> drift ->
    re-run setup.sh (which itself warns-not-fails, and idempotently skips
    when already correct). Never raises: the framework never catches hook
    exceptions and the installer re-raises them.
    """
    # The outer guard is fault-proof: every inner handler builds its message
    # via _desc() and _warn(), so nothing can escape this hook.
    try:
        try:
            installed = _installed_version()
        except Exception as exc:
            # Any probe failure (absent binary, OSError, timeout, or a
            # ValueError/TypeError-family fault) is unreadable = drift.
            installed = None
            _warn("rtk version probe failed (" + _desc(exc) + "); re-checking via setup")
        try:
            pin = _pin_version()
        except Exception as exc:
            # An unreadable pin is plugin damage, not a version match; warn
            # distinctly and still attempt re-provisioning.
            pin = None
            _warn("rtk pin unreadable (" + _desc(exc) + "); re-checking via setup")
        if installed is not None and pin is not None and installed == pin:
            _info("rtk " + installed + " matches pin; update leaves binary untouched")
            return
        if installed is None:
            _info("rtk binary unavailable or unreadable; running setup for FR24 re-provision")
        elif pin is None:
            _info("rtk pin unreadable; running setup to re-provision")
        else:
            _info("rtk " + installed + " != pin " + pin + "; running setup to re-provision")
        try:
            result = _run_setup()
        except Exception as exc:
            # A failing re-provision must not fail the update lifecycle.
            _warn("rtk re-provisioning failed (" + _desc(exc) + "); agent runs as-is")
            return
        rc = result.returncode
        output = _surface(result)
        if rc == 0:
            # Provisioned, idempotent skip, or warned-unavailable.
            _info("rtk re-provision complete" + ((": " + output) if output else ""))
            return
        # Exit-class parity with install(): usage, availability, and
        # integrity failures surface distinctly.
        if rc == 2:
            _warn("rtk re-provision usage error (rc=2)" + ((": " + output) if output else ""))
        elif rc in (126, 127) or rc < 0:
            _warn("rtk re-provision could not run (rc=" + str(rc) + ")" + ((": " + output) if output else ""))
        else:
            _warn(
                "rtk re-provision reported integrity/format failure (rc=" + str(rc) + ") - inspect manually"
                + ((": " + output) if output else "")
            )
    except Exception as exc:
        _warn("rtk pre_update degraded (" + _desc(exc) + "); update continues")


def uninstall():
    """A0 uninstall hook: never touches the user-owned RTK binary.

    A0 deletes the plugin directory itself after this hook returns:
    uninstall_plugin -> call_plugin_hook("uninstall") then delete_plugin
    (helpers/plugins.py:409-414 -> :415-444). This plugin owns no files
    outside its directory except the user-owned binary, so the correct hook
    is a guarded, informative no-op - it removes nothing and never raises.
    """
    try:
        _style.info(
            "rtk uninstall: plugin files removed by A0; user-owned rtk left at "
            + INSTALL_DIR + "/rtk"
        )
    except Exception as exc:
        _warn("rtk uninstall degraded (" + _desc(exc) + ")")
