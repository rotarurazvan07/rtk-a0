"""RTK plugin lifecycle hooks (A0 plugin contract, Story 2.4).

Real interface verified against the framework (cite if changed):
- helpers/plugins.py:908-936 call_plugin_hook loads this file by path and
  dispatches getattr(hooks, hook_name) with NO arguments; a missing hook
  falls back to its default.
- plugins/_plugin_installer/helpers/install.py:298-302 runs install/pre_update;
  helpers/plugins.py:411-414 runs uninstall before deleting the plugin dir.
- helpers/functions.py safe_call adapts arguments but never catches
  exceptions, and the installer re-raises - so every hook below self-guards
  and warns instead of failing the plugin lifecycle (NFR6, D10).
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent
# Provisioning lives in scripts/setup.sh (Stories 2.1-2.3); hooks only wire it.
_SETUP_SCRIPT = _PLUGIN_DIR / "scripts" / "setup.sh"
# Container OS bin dir (architecture D4/FR16) - never the framework venv.
INSTALL_DIR = "/usr/local/bin"
# Pin stays single-source in rtk_pin.txt (NFR12/D5); hooks never read versions.
_SETUP_TIMEOUT = 120  # seconds; download+verify+install worst case on slow links
# Concurrent lifecycle callers share setup.sh fixed staging path
# (CR-2.1-D2/D3) - serialize in-process callers. A0 update path calls
# pre_update then install sequentially (install.py:245,270).
_SETUP_LOCK = threading.RLock()


class _Style:
    """Minimal operator-visible logging (stderr; A0 surfaces hook output)."""

    @staticmethod
    def _emit(level, *args):
        try:
            print("rtk hooks: " + level + ": " + " ".join(str(a) for a in args), file=sys.stderr)
        except Exception:
            # CR 3.4 F1: a closed stderr raises ValueError, not an OSError;
            # every writer failure must be swallowed (NFR6 is absolute).
            pass

    def info(self, *args, **kwargs):
        self._emit("INFO", *args)

    def warning(self, *args, **kwargs):
        self._emit("WARNING", *args)

    def error(self, *args, **kwargs):
        self._emit("ERROR", *args)


_style = _Style()


def _warn(msg):
    """Warning that cannot itself fail the hook (NFR6 is absolute: CR-2.4 P4)."""
    try:
        _style.warning(msg)
    except Exception:
        pass


def _desc(exc):
    """Fault-proof exception text (CR 3.4 F2: an exception whose __str__
    raises must not turn a handled degradation into an escape)."""
    try:
        return str(exc)
    except Exception:
        try:
            return type(exc).__name__ + " (unprintable)"
        except Exception:
            return "<unprintable exception>"


def _info(msg):
    """Status line that cannot abort the action it announces (CR 3.4 F6)."""
    try:
        _style.info(msg)
    except Exception:
        pass

def _setup_env():
    """Environment for setup.sh; guarantees INSTALL_DIR on PATH (CR-2.3-D2).

    setup.sh self-check resolves rtk through PATH (setup.sh:217-219); if the
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
            # CR 2.4 P1: explicit encoding - text=True decodes strictly in the
            # interpreter locale and a non-ASCII setup byte would raise
            # UnicodeDecodeError (a ValueError, not an OSError) from inside
            # subprocess.run; utf-8+replace keeps the hook raise-free.
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

    Satisfies FR11/D10/NFR6: every outcome (0 provisioned-or-warned,
    2 usage, other integrity-corrupt, or a raised exception) returns
    without failing the plugin installation.
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
        # CR 2.4 P3: nothing may escape install() - the installer re-raises
        # hook exceptions (install.py:246-250), so any unexpected failure
        # (ValueError family, MemoryError, ...) degrades to a warning.
        _style.warning("rtk setup could not run (" + _desc(exc) + "); agent runs uncompressed")
        return

    rc = result.returncode
    output = _surface(result)
    if rc == 0:
        # Provisioned, idempotent skip, or warned-unavailable (Story 2.3).
        _style.info("rtk setup complete" + ((": " + output) if output else ""))
        return
    if rc == 2:
        _style.warning("rtk setup usage error (rc=2)" + ((": " + output) if output else ""))
        return
    if rc in (126, 127) or rc < 0:
        # CR 2.4 P2: missing/non-executable script (126/127) or a signal-killed
        # child (negative rc) is availability-shaped, NOT integrity corruption.
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

    Probes via argv list - no shell mode - (D2 pin) with the same PATH the
    setup self-check uses (INSTALL_DIR appended); CR-2.4 P1 encoding pins keep
    a non-ASCII byte from raising UnicodeDecodeError. Real output is
    ``rtk 1.0.1`` (CR-2.2 live-verified, no v-prefix); the version is the
    last whitespace token of the last non-empty line.
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
    """Read the pin file (rtk_pin.txt, NFR12 single source); v-prefix stripped."""
    raw = (_PLUGIN_DIR / "rtk_pin.txt").read_text(encoding="utf-8").strip()
    return raw[1:] if raw.startswith("v") else raw


def pre_update():
    """A0 pre_update hook (FR24/D10): re-provision only on version drift.

    Installed rtk --version vs rtk_pin.txt, token-exact: equal -> keep the
    binary (no setup invocation); absent/unreadable/different -> drift ->
    re-run setup.sh (which itself warn-not-fails, Story 2.3, and idempotent-
    skips when already correct, Story 2.2). Never raises (NFR6: safe_call
    never catches; the installer re-raises hook exceptions).
    """
    # CR 3.4 F2: the outer guard is fault-proof - every inner handler builds
    # its message via _desc() and _warn(), so nothing can escape this hook.
    try:
        try:
            installed = _installed_version()
        except Exception as exc:
            # CR 3.4 F5: ANY probe failure (absent binary, OSError, timeout,
            # or a ValueError/TypeError-family fault) = unreadable = drift.
            installed = None
            _warn("rtk version probe failed (" + _desc(exc) + "); re-checking via setup")
        try:
            pin = _pin_version()
        except Exception as exc:
            # CR 3.4 F4: an unreadable pin is plugin damage, not a version match;
            # warn distinctly and still attempt re-provisioning (FR24).
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
            # Provisioned, idempotent skip, or warned-unavailable (Story 2.3).
            _info("rtk re-provision complete" + ((": " + output) if output else ""))
            return
        # CR 3.4 F7: exit-class parity with install() (AC-04): usage (2),
        # availability (126/127/signal), integrity (other) surface distinctly.
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
    """A0 uninstall hook (FR23/D10): never touches the user-owned RTK binary.

    A0 deletes the plugin directory itself after this hook returns:
    uninstall_plugin -> call_plugin_hook("uninstall") then delete_plugin
    (helpers/plugins.py:409-414 -> :415-444). This plugin owns no files
    outside its directory except the user-owned binary (NFR11), so the
    correct hook is a guarded, informative no-op - it removes nothing and
    never raises (NFR6/D10).
    """
    try:
        _style.info(
            "rtk uninstall: plugin files removed by A0; user-owned rtk left at "
            + INSTALL_DIR + "/rtk"
        )
    except Exception as exc:
        _warn("rtk uninstall degraded (" + _desc(exc) + ")")
