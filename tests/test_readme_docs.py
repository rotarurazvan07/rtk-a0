"""Story 3.6 — README documentation validation (AC-01..AC-05).

Pure venv, network-free: every check reads local files only. The README is the
deliverable; these tests are the executable acceptance contract.
"""

import re
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
README = PLUGIN_ROOT / "README.md"


def _readme_text() -> str:
    # AC-01: the root README.md is the story deliverable and must exist
    assert README.exists(), "root README.md missing — story deliverable absent"
    return README.read_text(encoding="utf-8")


def _headings(text: str) -> str:
    """Markdown ATX heading lines only (structure pins, not body prose)."""
    return "\n".join(line for line in text.splitlines() if line.lstrip().startswith("#"))


# --- AC-01: install paths, automatic setup, restart, auto-download, warn-not-fail ---

def test_ac01_readme_exists_at_root():
    assert README.exists()


def test_ac01_documents_plugin_hub_install_path():
    text = _readme_text().lower()
    assert "plugin hub" in text, "README must document the Plugin Hub install path"


def test_ac01_documents_git_clone_install_path():
    text = _readme_text()
    assert "git clone" in text.lower()
    assert "usr/plugins" in text


def test_ac01_documents_restart_step():
    assert "restart" in _readme_text().lower()


def test_ac01_documents_auto_download_of_pinned_binary():
    text = _readme_text()
    assert "setup.sh" in text
    # pinned binary provenance must be stated
    low = text.lower()
    assert "rtk_pin.txt" in low or "pinned" in low
    assert "/usr/local/bin/rtk" in text


def test_ac01_documents_warn_not_fail():
    low = _readme_text().lower()
    assert "warn" in low and "fail" in low
    # warn-not-fail semantics: agent keeps working without the binary
    assert "without" in low or "agent still works" in low or "keeps working" in low


# --- AC-02: verify savings via rtk gain ---

def test_ac02_documents_rtk_gain_verification():
    text = _readme_text()
    assert "rtk gain" in text
    assert "token" in text.lower() or "saved" in text.lower()


# --- AC-03: bypass escape hatch + fail-open guarantee prominence ---

def test_ac03_documents_rtk_bypass_escape_hatch():
    assert "rtk bypass" in _readme_text()


def test_ac03_documents_fail_open_guarantee():
    low = _readme_text().lower()
    assert "fail-open" in low or "fail open" in low


def test_ac03_bypass_is_prominent_heading_or_bold():
    text = _readme_text()
    prominent = any(
        ("bypass" in ln.lower()) and (ln.lstrip().startswith("#") or ln.lstrip().startswith("**"))
        for ln in text.splitlines()
    )
    assert prominent, "rtk bypass must be a prominent heading or bold callout"


# --- AC-04: licensing + support statement ---

def test_ac04_states_plugin_apache_license():
    low = _readme_text().lower()
    assert "apache" in low


def test_ac04_declares_rtk_busl_dependency():
    low = _readme_text().lower()
    assert "busl" in low


def test_ac04_states_orchestrate_only_posture():
    low = _readme_text().lower()
    assert "orchestrat" in low


def test_ac04_states_linux_container_only_support():
    low = _readme_text().lower()
    assert "linux" in low
    assert "fr28" in low or "linux-container-only" in low or "linux container" in low


def test_ac04_references_licensing_doc():
    text = _readme_text()
    assert "docs/licensing-boundary.md" in text
    assert (PLUGIN_ROOT / "docs" / "licensing-boundary.md").exists()


# --- AC-05: troubleshooting section — warning, agent-works, remedy ---

def test_ac05_has_troubleshooting_section():
    assert "troubleshoot" in _headings(_readme_text()).lower()


def test_ac05_explains_unavailable_warning():
    low = _readme_text().lower()
    assert "warning" in low or "not available" in low or "unavailable" in low


def test_ac05_confirms_agent_still_works():
    low = _readme_text().lower()
    assert "still works" in low or "keeps working" in low or "agent still" in low or "unchanged" in low


def test_ac05_lists_rerun_setup_remedy():
    text = _readme_text()
    assert "setup.sh" in text
    # the remedy must be a form setup.sh actually accepts (no invented flags —
    # CR 3.6 H-1: README previously documented an invalid --install-dir flag)
    assert "--install-dir" not in text, "invalid setup.sh flag documented"
    assert re.search(r"(re-run|rerun|bash scripts/setup\.sh|scripts/setup\.sh)", text)
    env_form = re.search(r"RTK_INSTALL_DIR=/[^\s]+ bash scripts/setup\.sh", text)
    plain_form = re.search(r"bash scripts/setup\.sh(\s|$)", text)
    assert env_form or plain_form, "remedy must be a no-arg or env-var setup.sh run"


# --- Honesty constraints (Story 3.2/3.3 lessons — enforced across surfaces) ---

def test_honesty_no_flat_compression_range_claim():
    text = _readme_text()
    assert "60-90%" not in text and "60–90%" not in text and "60-90 %" not in text


def test_honesty_includes_plain_ls_expansion_counterexample():
    low = _readme_text().lower()
    assert ("expand" in low or "larger" in low or "grow" in low or "increas" in low)


def test_honesty_frames_savings_as_measured_not_guaranteed():
    low = _readme_text().lower()
    assert "measured" in low or "typical" in low or "example" in low or "observed" in low


def test_fail_open_acknowledges_rc3_acceptance():
    # ADR-006 / Story 3.1: rtk rc 3 is applied as a rewrite; the fail-open
    # enumeration must not claim every non-zero exit falls back to original
    low = _readme_text().lower()
    assert ("rc 3" in low) or ("allow-rules" in low), (
        "fail-open section must acknowledge the sanctioned rc-3 acceptance"
    )


# --- Link-shape check: repo-relative markdown links resolve (no network) ---

def test_repo_relative_links_exist():
    text = _readme_text()
    # strip fenced code blocks so example commands are not parsed as links
    stripped = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    missing = []
    for m in re.finditer(r"\[[^\]]+\]\(([^)]+)\)", stripped):
        target = m.group(1).strip()
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        target = target.split("#")[0]
        if not target:
            continue
        if not (PLUGIN_ROOT / target).exists():
            missing.append(target)
    assert not missing, f"README links to non-existent repo paths: {missing}"


# --- Structural: headings hierarchy sane (h1 present, no skipped level from h1) ---

def test_structure_has_single_h1_title():
    text = _readme_text()
    h1 = [ln for ln in text.splitlines() if ln.startswith("# ")]
    assert len(h1) == 1


@pytest.mark.parametrize("section", ["install", "verify", "troubleshoot", "license"])
def test_core_sections_present(section):
    headings = _headings(_readme_text()).lower()
    assert section in headings, f"README missing a '{section}' heading"
