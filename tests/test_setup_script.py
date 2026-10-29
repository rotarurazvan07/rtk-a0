#!/usr/bin/env python3
"""Tests for the pinned binary download script (bash). Pure runtime, no framework imports. Network-free by design: every network surface is either a resolved-URL read or a mocked curl stub on PATH."""
import hashlib
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SETUP = PLUGIN_ROOT / "scripts" / "setup.sh"
PIN_FILE = PLUGIN_ROOT / "rtk_pin.txt"

EXPECTED_BASE = "https://github.com/rtk-ai/rtk/releases/download"
EXPECTED_TARBALL = "rtk-x86_64-unknown-linux-musl.tar.gz"
EXPECTED_CHECKSUMS = "checksums.txt"

# GitHub Releases API verification (recorded 2026-10-16, one-time, manual):
# release v1.0.1 (published 2026-10-14T13:31:42Z, not draft/prerelease);
# asset rtk-x86_64-unknown-linux-musl.tar.gz — sha256 5b919c3bccb799b67a61866d0e617410a4fcdf173e2614440e59de2fa2003436,
# size 6,778,173 bytes, download URL returned HTTP 200 (HEAD). Recorded as a
# fact here; this test never opens the network itself.
ASSET_SHA256 = "5b919c3bccb799b67a61866d0e617410a4fcdf173e2614440e59de2fa2003436"


def _strip_comments(src: str) -> str:
    """Drop comment-only lines so audits see code, not prose."""
    return "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#")
    )


def _source() -> str:
    return SETUP.read_text(encoding="utf-8")


def _code_source() -> str:
    return _strip_comments(_source())



# Host-state isolation: a minimal controlled base PATH keeps the
# suite deterministic on hosts that have a real rtk on PATH - inherited PATH
# let host binaries shadow the stubs, making download/verify branches
# unreachable (12 failures on rtk-equipped containers).
_CONTROLLED_PATH = "/usr/bin:/bin"


def _plan_env(root: Path) -> dict:
    env = dict(os.environ)
    env["PATH"] = _CONTROLLED_PATH
    env["RTK_PROJECT_ROOT"] = str(root)
    return env


def _run_plan(root: Path, cwd: Path | None = None) -> list[tuple[str, str]]:
    """Run `setup.sh --print-download-plan` against a project root; return (name, url) pairs. Two-sided normalization discipline ( two-sided comparison discipline): both sides of every comparison are stripped. Optional cwd proves CWD-independence."""
    proc = subprocess.run(
        ["bash", str(SETUP), "--print-download-plan"],
        cwd=str(cwd) if cwd else None,
        env=_plan_env(root),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"plan mode failed: {proc.stderr}"
    pairs = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        name, _, url = line.partition(" ")
        assert url, f"plan line missing url component: {line!r}"
        pairs.append((name.strip(), url.strip()))
    return pairs


def _curl_stub(tmp_path: Path, argv_log: Path) -> Path:
    """Write a PATH-first curl stub that records argv and creates the -o target."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "curl"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$RTK_ARGV_LOG\"\n"
        "out=\"\"\n"
        "while [ $# -gt 0 ]; do\n"
        "  if [ \"$1\" = \"-o\" ]; then out=\"$2\"; fi\n"
        "  shift\n"
        "done\n"
        "if [ -n \"$out\" ]; then printf 'stub-bytes' > \"$out\"; fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    argv_log.write_text("", encoding="utf-8")
    return bin_dir


def _which_patched_env(bin_dir: Path, staging: Path, argv_log: Path) -> dict:
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{_CONTROLLED_PATH}"
    env["RTK_STAGING_DIR"] = str(staging)
    env["RTK_ARGV_LOG"] = str(argv_log)
    return env


def _argv_calls(argv_log: Path) -> list[str]:
    return [ln.strip() for ln in argv_log.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ---------------------------------------------------------------------------
# pin-read + HTTPS download of both pinned assets
# ---------------------------------------------------------------------------

def test_plan_emits_exactly_two_pinned_assets(tmp_path):
    """plan mode prints exactly the tarball and checksums.txt for the real pin."""
    tag = PIN_FILE.read_text(encoding="utf-8").strip()
    pairs = _run_plan(PLUGIN_ROOT)
    assert len(pairs) == 2, f"expected exactly 2 assets, got {pairs}"
    by_name = dict(pairs)
    assert set(by_name) == {EXPECTED_TARBALL, EXPECTED_CHECKSUMS}
    assert by_name[EXPECTED_TARBALL] == f"{EXPECTED_BASE}/{tag}/{EXPECTED_TARBALL}"
    assert by_name[EXPECTED_CHECKSUMS] == f"{EXPECTED_BASE}/{tag}/{EXPECTED_CHECKSUMS}"
    for name, url in pairs:
        assert url.startswith("https://"), f"{name}: non-https url {url}"



def test_plan_no_network_side_effects(tmp_path):
    """plan mode is read-only — the real staging location stays absent."""
    staging = tmp_path / "real-staging-location"
    env = _plan_env(PLUGIN_ROOT)
    env["RTK_STAGING_DIR"] = str(staging)
    proc = subprocess.run(
        ["bash", str(SETUP), "--print-download-plan"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    assert not staging.exists(), "plan mode must not create staging output"


def test_plan_is_cwd_independent(tmp_path):
    """plan mode resolves the pin via RTK_PROJECT_ROOT, not the cwd."""
    pairs = _run_plan(PLUGIN_ROOT, cwd=tmp_path)
    assert EXPECTED_TARBALL in [name for name, _ in pairs]


def test_mocked_curl_downloads_both_assets_to_staging(tmp_path):
    """End-to-end execution with a curl stub on PATH makes exactly two curl calls. Guard-adjacency: the which probe is pre-patched to the stub before PATH mutation, so the stub is what the script resolves."""
    argv_log = tmp_path / "argv.log"
    bin_dir = _curl_stub(tmp_path, argv_log)
    staging = tmp_path / "staging"
    # Isolation mechanism is the PATH prepend in _which_patched_env — a Python
    # shutil.which patch would be inert (the subprocess is bash and resolves
    # curl via its own PATH, never via Python).
    proc = subprocess.run(
        ["bash", str(SETUP)],
        env=_which_patched_env(bin_dir, staging, argv_log),
        capture_output=True,
        text=True,
        timeout=60,
    )
    calls = _argv_calls(argv_log)

    # The stub stages 'stub-bytes' placeholders, so the integrity
    # gate must REJECT them: fail-safe direction (pins this — the
    # download-surface test no longer expects a successful install).
    assert proc.returncode != 0, "junk downloads must fail the integrity gate"
    assert len(calls) == 2, f"expected exactly 2 curl invocations, got {calls}"

    tag = PIN_FILE.read_text(encoding="utf-8").strip()
    expected = {
        EXPECTED_TARBALL: f"{EXPECTED_BASE}/{tag}/{EXPECTED_TARBALL}",
        EXPECTED_CHECKSUMS: f"{EXPECTED_BASE}/{tag}/{EXPECTED_CHECKSUMS}",
    }
    seen_urls = []
    for call in calls:
        seen_urls.append(call)
        assert " -o " in f" {call} ", f"curl call missing -o: {call}"
        # Fail-fast flags present (HTTPS + explicit failure)
        for flag in ("-fsSL", "--fail"):
            assert flag in call.split(), f"curl call missing {flag}: {call}"
        for name, url in expected.items():
            if url in call:
                assert f"-o {staging / name}" in call, f"bad -o target for {name}: {call}"
                break
        else:
            raise AssertionError(f"curl call for unexpected url: {call}")
    assert any(expected[EXPECTED_TARBALL] in c for c in seen_urls)
    assert any(expected[EXPECTED_CHECKSUMS] in c for c in seen_urls)

    # Absence allowlist: the -fsSL/--fail checks above are presence-shaped and
# cannot see TLS-weakening or proxy-redirect drift.
    for call in calls:
        tokens = call.split()
        for flag in (
            "-k",
            "--insecure",
            "-x",
            "--proxy",
            "--noproxy",
            "--proto",
            "--location-trusted",
        ):
            assert flag not in tokens, f"TLS/proxy-weakening shape {flag!r}: {call}"

    # the EXIT trap (armed before download) removes the staged pair
    # even on the integrity-abort path, so post-run file-existence is no longer
    # assertable here — integrity rejection is observable from the exit code
    # (fail-safe direction), which this test already pins above.
    combined = (proc.stdout + proc.stderr).lower()
    assert "integrity" in combined or "checksum" in combined, f"no integrity error surfaced: {combined}"


# ---------------------------------------------------------------------------
# tag single-source + HTTPS-only audit
# ---------------------------------------------------------------------------

def test_setup_script_contains_no_version_literal():
    """no version-like token in code outside the pin-file read line."""
    code = _code_source()
    pin_lines = [ln for ln in code.splitlines() if "rtk_pin.txt" in ln]
    assert pin_lines, "setup.sh must read the pin file"
    offenders = []
    for line in code.splitlines():
        if "rtk_pin.txt" in line:
            continue  # the sanctioned single-source read
        if re.search(r"v\d+\.\d+\.\d+", line):
            offenders.append(line)
    assert not offenders, f"version literal leaked outside pin read: {offenders}"


def test_setup_script_https_only():
    """every URL literal in the script is https."""
    code = _code_source()
    http_urls = re.findall(r"http://[^\s'\"]+", code)
    assert not http_urls, f"insecure http url in script: {http_urls}"
    urls = re.findall(r"https://[^\s'\"]+", code)
    assert urls, "expected at least one https URL constant"


def test_pin_token_appears_once_as_code():
    """the pin-file read is the single place the tag enters."""
    code = _code_source()
    reads = [ln for ln in code.splitlines() if re.search(r"PIN=\$\(\s*cat\s+", ln) and "rtk_pin.txt" in ln]
    assert len(reads) == 1, f"tag must be read exactly once from the pin file: {reads}"
    # The 2.2 malformed-pin ERROR MESSAGE cites rtk_pin.txt — an error string
    # never introduces the tag into the program, so only reads count.


def test_setup_script_fail_fast_curl_flags():
    """curl invocations use -fsSL and --fail."""
    code = _code_source()
    curl_calls = [ln for ln in code.splitlines() if "curl " in ln]
    assert curl_calls, "expected curl invocation(s) in script"
    for call in curl_calls:
        assert "-fsSL" in call, f"curl call missing -fsSL: {call}"
        assert "--fail" in call, f"curl call missing --fail: {call}"


def test_setup_script_bash_shebang():
    """bash shebang (never POSIX sh)."""
    first = _source().splitlines()[0]
    assert first.strip() == "#!/usr/bin/env bash", f"bad shebang: {first}"


# ---------------------------------------------------------------------------
# one-line-PR bump property (pin edit alone changes every URL)
# ---------------------------------------------------------------------------

def test_bump_property_pin_only(tmp_path):
    """rewriting ONLY rtk_pin.txt re-points every download URL."""
    repo = tmp_path / "repo"
    shutil.copytree(
        PLUGIN_ROOT,
        repo,
        ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", ".a0proj", ".serena", "venv", ".venv", "usr", "node_modules", ".coverage"),
    )
    (repo / "rtk_pin.txt").write_text("v9.9.9\n", encoding="utf-8")
    by_name = dict(_run_plan(repo))
    assert by_name[EXPECTED_TARBALL].endswith("/v9.9.9/rtk-x86_64-unknown-linux-musl.tar.gz")
    assert by_name[EXPECTED_CHECKSUMS].endswith("/v9.9.9/checksums.txt")
    # the script copy itself was NOT edited
    assert (repo / "scripts" / "setup.sh").read_text(encoding="utf-8") == SETUP.read_text(encoding="utf-8")


def test_bump_property_matrix(tmp_path):
    """tag flows through every URL for two hypothetical pins."""
    for tag in ("v2.3.4", "v10.20.30"):
        repo = tmp_path / f"repo-{tag}"
        shutil.copytree(
            PLUGIN_ROOT,
            repo,
            ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", ".a0proj", ".serena", "venv", ".venv", "usr", "node_modules", ".coverage"),
        )
        (repo / "rtk_pin.txt").write_text(f"{tag}\n", encoding="utf-8")
        urls = [url for _, url in _run_plan(repo)]
        assert len(urls) == 2
        for url in urls:
            assert f"/{tag}/" in url, f"pin {tag} not reflected in {url}"


# ---------------------------------------------------------------------------
# no pip packages, no build tooling, no interactive input
# ---------------------------------------------------------------------------

def test_no_package_manager_or_build_tooling_in_script():
    """the script never invokes tooling runtimes."""
    code = _code_source()
    banned = ["pip", "pip3", "apt", "apt-get", "cargo", "rustup", "make", "cmake", "gcc", "npm", "node", "wget", "perl"]
    offenders = []
    for line in code.splitlines():
        for tok in line.replace(";", " ").replace("|", " ").replace("&", " ").split():
            base = tok.rsplit("/", 1)[-1]
            if base in banned:
                offenders.append(line.strip())
                break
    assert not offenders, f"tooling token found: {offenders}"


def test_no_interactive_input_in_script():
    """nothing prompts the operator."""
    code = _code_source()
    patterns = [
        r"\bread\s+-?\w*\s*\w*\s*\$",  # read var / read -r var
        r"\bread\s+(-[a-zA-Z]+\s+)*\w+\s*$",
        r"\bselect\s+\w+\s+in\b",
        r"whiptail",
        r"\bdialog\b",
        r"\binput\s*\(",
        r"\bpassword\s*=",
    ]
    offenders = []
    for line in code.splitlines():
        for pat in patterns:
            if re.search(pat, line):
                offenders.append((pat, line.strip()))
    assert not offenders, f"interactive surface found: {offenders}"



# ---------------------------------------------------------------------------
# Structural pins / recorded live-URL verification
# ---------------------------------------------------------------------------

def test_recorded_asset_sha256_is_pinned_in_tests():
    """Recorded GitHub-API fact: expected tarball digest ( verifies for real)."""
    assert re.fullmatch(r"[0-9a-f]{64}", ASSET_SHA256)


def test_setup_script_exists_and_executable_bit():
    """Sanity: the deliverable exists and is directly executable ( wiring)."""
    assert SETUP.is_file()
    assert os.access(SETUP, os.X_OK), "setup.sh must carry the executable bit"


def test_full_edition_asset_not_oss(tmp_path):
    """the full edition tarball is selected, never rtk-oss-*."""
    by_name = dict(_run_plan(PLUGIN_ROOT))
    assert EXPECTED_TARBALL in by_name
    assert not any(name.startswith("rtk-oss") for name in by_name)


# ---------------------------------------------------------------------------
# Integrity Verification & PATH Installation (.)
# ---------------------------------------------------------------------------
# contract decision (recorded in story Dev Notes): RTK publishes
# sha256 over the RELEASE ASSET FILES (the tarball itself -- the recorded
# 5b919c3b digest is the tarball sha, GitHub-API-verified in).
# Verification compares sha256(staged tarball) against the pinned
# checksums.txt entry; extraction happens only AFTER verification passes.


def _fixture_repo(tmp_path):
    """Copy only what setup.sh needs into an isolated root (skip .a0proj etc.)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copytree(PLUGIN_ROOT / "scripts", repo / "scripts")
    shutil.copy2(PIN_FILE, repo / "rtk_pin.txt")
    return repo


def _make_fake_tarball(tmp_path, nested=False):
    """Build a FAKE rtk tarball; return (path, sha256). Dual layout: nested dir vs flat."""
    work = tmp_path / "tar-work"
    work.mkdir(exist_ok=True)
    if nested:
        (work / "rtk-v1.0.1").mkdir()
        stub_path = work / "rtk-v1.0.1" / "rtk"
    else:
        stub_path = work / "rtk"
    # the REAL rtk v1.0.1 binary prints "rtk 1.0.1" (no v prefix) —
    # byte-verified during against the actual release artifact. The
    # fixture must encode the real output format or the suite cannot catch
    # pin/version mismatches.
    stub_path.write_text("#!/usr/bin/env bash\necho 'rtk 1.0.1 (fake fixture)'\n", encoding="utf-8")
    stub_path.chmod(0o755)
    tarball = tmp_path / EXPECTED_TARBALL
    arc = stub_path.relative_to(work)
    with tarfile.open(tarball, "w:gz") as tf:
        tf.add(stub_path, arcname=str(arc))
    digest = hashlib.sha256(tarball.read_bytes()).hexdigest()
    return tarball, digest


def _make_fake_checksums(tmp_path, tarball, digest):
    """Write checksums.txt in the upstream shape: '<sha256>  <asset-name>'."""
    path = tmp_path / EXPECTED_CHECKSUMS
    path.write_text(f"{digest}  {tarball.name}\n", encoding="utf-8")
    return path


# bash stub bodies use unquoted expansions deliberately: pytest tmp paths never
# contain whitespace, and the stubs are test-fixture-only surface.
CURL_STUB_BODY = '''#!/usr/bin/env bash
printf '%s\n' "$*" >> "$RTK_ARGV_LOG"
out=
while [ $# -gt 0 ]; do
  if [ "$1" = "-o" ]; then out="$2"; fi
  shift
done
if [ -n "$out" ]; then
  base=$(basename "$out")
  if [ -f "${RTK_STUB_ASSETS:-/nonexistent}/$base" ]; then
    cp "${RTK_STUB_ASSETS}/$base" "$out"
  else
    printf 'stub-bytes' > "$out"
  fi
fi
exit 0
'''

RTK_STUB_FLIP = '''#!/usr/bin/env bash
printf 'rtk-version-probe: %s\n' "$*" >> "$RTK_VERSION_LOG"
if [ -f "$RTK_STUB_MARKER" ]; then
  printf '%s\n' '__VERSION_STRING__'
else
  : > "$RTK_STUB_MARKER"
  printf '%s\n' 'rtk 0.0.0-not-installed'
fi'''

RTK_STUB_STATIC = '''#!/usr/bin/env bash
printf 'rtk-version-probe: %s\n' "$*" >> "$RTK_VERSION_LOG"
printf '%s\n' '__VERSION_STRING__'
'''


def _stub_bin_dir(tmp_path):
    """PATH-first curl stub recording argv; copies RTK_STUB_ASSETS/<name> when present."""
    bin_dir = tmp_path / "stubbin"
    bin_dir.mkdir(exist_ok=True)
    argv_log = tmp_path / "curl-argv.log"
    argv_log.write_text("", encoding="utf-8")
    curl = bin_dir / "curl"
    curl.write_text(CURL_STUB_BODY, encoding="utf-8")
    curl.chmod(0o755)
    return bin_dir, argv_log


def _rtk_stub(bin_dir, version_text, flip=True):
    """PATH-first fake rtk: records probes in $RTK_VERSION_LOG.

    flip=True starts on a non-matching version and reports the real one only
    after the first probe (install tests must survive the idempotency gate);
    flip=False always reports version_text (idempotency/mismatch tests).
    """
    version_log = bin_dir.parent / "rtk-version.log"
    version_log.write_text("", encoding="utf-8")
    stub = bin_dir / "rtk"
    template = RTK_STUB_FLIP if flip else RTK_STUB_STATIC
    body = template.replace("__VERSION_STRING__", version_text)
    stub.write_text(body, encoding="utf-8")
    stub.chmod(0o755)


def _setup_env(repo, staging, install_dir, extra=None):
    """Environment for a full setup run: env overrides; PATH stubs injected by caller."""
    env = dict(os.environ)
    env["RTK_PROJECT_ROOT"] = str(repo)
    env["RTK_STAGING_DIR"] = str(staging)
    env["RTK_INSTALL_DIR"] = str(install_dir)
    env.pop("RTK_STUB_ASSETS", None)
    for k, v in (extra or {}).items():
        env[k] = str(v)
    return env


def _run_setup(env, args=None, timeout=60):
    """Execute setup.sh with the given environment; callers strip outputs two-sided."""
    return subprocess.run(
        ["bash", str(SETUP), *(args or [])],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _curl_calls(argv_log):
    return [ln.strip() for ln in argv_log.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _stage_fixture_assets(tmp_path):
    """Create fixture tarball+checksums in an asset dir for the curl stub to copy."""
    tarball, digest = _make_fake_tarball(tmp_path / "assets-src")
    assets = tmp_path / "assets"
    assets.mkdir()
    shutil.copy2(tarball, assets / EXPECTED_TARBALL)
    _make_fake_checksums(tmp_path, tarball, digest)  # at tmp root as source
    shutil.copy2(tmp_path / EXPECTED_CHECKSUMS, assets / EXPECTED_CHECKSUMS)
    return assets


def _run_full(tmp_path, nested=False, version_text=None, corrupt_checksum=False, skip_checksums=False, pin_text=None):
    """Full setup run with fixtures. Returns (proc, install_dir, bin_dir, version_log, argv_log)."""
    tmp_path.joinpath("assets-src").mkdir(exist_ok=True)
    tarball, digest = _make_fake_tarball(tmp_path / "assets-src", nested=nested)
    assets = tmp_path / "assets"
    assets.mkdir()
    shutil.copy2(tarball, assets / EXPECTED_TARBALL)
    if not skip_checksums:
        cs = tmp_path / EXPECTED_CHECKSUMS
        cs_text = f"{digest}  {EXPECTED_TARBALL}\n"
        if corrupt_checksum:
            flipped = "0" if digest[0] != "0" else "1"
            cs_text = f"{flipped}{digest[1:]}  {EXPECTED_TARBALL}\n"
        cs.write_text(cs_text, encoding="utf-8")
        shutil.copy2(cs, assets / EXPECTED_CHECKSUMS)
    repo = _fixture_repo(tmp_path)
    if pin_text is not None:
        (repo / "rtk_pin.txt").write_text(pin_text, encoding="utf-8")
    bin_dir, argv_log = _stub_bin_dir(tmp_path)
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    version_log = tmp_path / "rtk-version.log"
    version_log.write_text("", encoding="utf-8")
    if version_text is not None:
        _rtk_stub(bin_dir, version_text)
    env = _setup_env(repo, tmp_path / "staging", install_dir, extra={
        "PATH": f"{bin_dir}{os.pathsep}{_CONTROLLED_PATH}",
        "RTK_ARGV_LOG": str(argv_log),
        "RTK_VERSION_LOG": str(version_log),
        "RTK_STUB_ASSETS": str(assets),
        "RTK_STUB_MARKER": str(tmp_path / "rtk-flip.marker"),
    })
    proc = _run_setup(env)
    return proc, install_dir, bin_dir, version_log, argv_log


def test_verify_and_install_happy_path(tmp_path):
    """03: valid checksum -> install to RTK_INSTALL_DIR -> rtk --version self-check."""
    pin = PIN_FILE.read_text(encoding="utf-8").strip()
    proc, install_dir, _bin, version_log, _log = _run_full(tmp_path, version_text=f"rtk {pin[1:]}")
    assert proc.returncode == 0, f"setup failed: {proc.stderr}"
    installed = install_dir / "rtk"
    assert installed.is_file(), "rtk binary missing from install dir"
    assert os.access(installed, os.X_OK), "installed binary is not executable"
    probes = version_log.read_text(encoding="utf-8").strip().splitlines()
    assert any("--version" in p for p in probes), f"self-check not run via PATH: {probes}"


# SUPERSEDES the 2.2-era hard abort on checksum mismatch:
# a corrupted download is availability-shaped -> warn + exit 0 (never install).
# Pinned by test_checksum_mismatch_warns_not_fails; this placeholder keeps the
# lineage traceable without duplicating that pin.


def test_missing_checksums_aborts(tmp_path):
    """missing/unparseable checksums.txt aborts before install."""
    proc, install_dir, _bin, _vlog, _log = _run_full(tmp_path, skip_checksums=True)
    assert proc.returncode != 0, "missing checksums must abort setup"
    assert not (install_dir / "rtk").exists(), "no install without checksums"


def test_nested_tarball_layout_installs_flat(tmp_path):
    """nested upstream layout (rtk-vX/rtk) still installs <dir>/rtk flat."""
    pin = PIN_FILE.read_text(encoding="utf-8").strip()
    proc, install_dir, _bin, _vlog, _log = _run_full(tmp_path, nested=True, version_text=f"rtk {pin[1:]}")
    assert proc.returncode == 0, f"nested layout failed: {proc.stderr}"
    installed = install_dir / "rtk"
    assert installed.is_file(), "nested tarball must install a flat rtk binary"
    assert os.access(installed, os.X_OK)


# SUPERSEDES the hard-fail self-check: a corrupt or
# wrong-version binary warns + rolls back + exit 0. Pinned by
# test_self_check_failure_warns_and_rolls_back.


def test_no_venv_path_or_package_manager_in_install_path():
    """structural): install path never touches a venv or package manager."""
    code = _code_source()
    for token in ("site-packages", "/venv/", ".venv", "activate"):
        assert token not in code, f"install path references venv surface: {token}"
    banned = ("pip", "pip3", "apt", "apt-get", "apk", "dnf", "cargo", "make", "cmake", "gcc", "npm", "node")
    for line in code.splitlines():
        for tok in line.replace(";", " ").replace("|", " ").replace("&", " ").split():
            base = tok.rsplit("/", 1)[-1]
            assert base not in banned, f"install path invokes tooling: {line.strip()}"


def test_self_check_uses_shell_path_resolution():
    """the self-check invokes rtk by NAME (PATH-resolved), as A0 terminal commands do — not a direct path to the install dir."""
    code = _code_source()
    probes = [ln.strip() for ln in code.splitlines() if "rtk --version" in ln or "rtk" in ln and "--version" in ln]
    assert probes, "expected a version self-check in the script"
    path_probe = [ln for ln in probes if "$RTK_INSTALL_DIR" in ln or "/usr/local/bin" in ln]
    assert not path_probe, f"version check must resolve via PATH, not install dir: {path_probe}"


def test_script_installs_with_chmod_x():
    """the install action is extract + chmod +x — no execution happens at install time."""
    code = _code_source()
    assert "chmod" in code, "install step must set the executable bit"


def test_second_run_skips_download_when_version_matches(tmp_path):
    """version-matching rtk already on PATH -> idempotent skip (no curl, no write)."""
    pin = PIN_FILE.read_text(encoding="utf-8").strip()
    repo = _fixture_repo(tmp_path)
    bin_dir, argv_log = _stub_bin_dir(tmp_path)
    _rtk_stub(bin_dir, f"rtk {pin[1:]}", flip=False)
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    existing = install_dir / "rtk"
    existing.write_text("#!/usr/bin/env bash\necho pre-existing\n", encoding="utf-8")
    existing.chmod(0o755)
    before_stat = existing.stat()
    env = _setup_env(repo, tmp_path / "staging", install_dir, extra={
        "PATH": f"{bin_dir}{os.pathsep}{_CONTROLLED_PATH}",
        "RTK_ARGV_LOG": str(argv_log),
    })
    proc = _run_setup(env)
    assert proc.returncode == 0, f"idempotent second run failed: {proc.stderr}"
    assert _curl_calls(argv_log) == [], "idempotent skip must not download"
    after_stat = existing.stat()
    assert after_stat.st_ino == before_stat.st_ino, "existing binary must not be replaced"
    assert after_stat.st_mtime == before_stat.st_mtime, "existing binary must not be rewritten"
    assert existing.read_text(encoding="utf-8").strip().endswith("pre-existing")


def test_malformed_pin_aborts_before_download(tmp_path):
    """malformed pin (garbage/empty/multiline) never builds a URL or downloads."""
    for bad_pin in ("garbage", "", "v1.0", "v1.0.1\nextra-line"):
        tmp2 = tmp_path / f"case-{abs(hash(bad_pin))}"
        tmp2.mkdir()
        repo = _fixture_repo(tmp2)
        (repo / "rtk_pin.txt").write_text(bad_pin, encoding="utf-8")
        bin_dir, argv_log = _stub_bin_dir(tmp2)
        env = _setup_env(repo, tmp2 / "staging", tmp2 / "install", extra={
            "PATH": f"{bin_dir}{os.pathsep}{_CONTROLLED_PATH}",
            "RTK_ARGV_LOG": str(argv_log),
        })
        proc = _run_setup(env)
        assert proc.returncode != 0, f"malformed pin must abort: {bad_pin!r}"
        assert _curl_calls(argv_log) == [], f"malformed pin must never download: {bad_pin!r}"


def test_staged_assets_cleaned_after_successful_install(tmp_path):
    """successful install cleans the staged pair; no orphan tarball remains."""
    pin = PIN_FILE.read_text(encoding="utf-8").strip()
    proc, install_dir, staging, _vlog, _log = _run_full(tmp_path, version_text=f"rtk {pin[1:]}")
    assert proc.returncode == 0, f"setup failed: {proc.stderr}"
    assert (install_dir / "rtk").is_file()
    assert not (staging / EXPECTED_TARBALL).exists(), "staged tarball must be cleaned after install"
    assert not (staging / EXPECTED_CHECKSUMS).exists(), "staged checksums must be cleaned after install"


def test_curl_uses_proto_redir_constraints():
    """redirects are constrained to https via --proto-redir (+ max-redirs)."""
    code = _code_source()
    curl_lines = [ln for ln in code.splitlines() if "curl " in ln]
    assert curl_lines, "expected curl invocation(s)"
    for line in curl_lines:
        assert "--proto-redir" in line, f"curl line missing --proto-redir: {line.strip()}"
        assert "--max-redirs" in line, f"curl line missing --max-redirs: {line.strip()}"


def test_staging_symlink_is_rejected(tmp_path):
    """a symlinked staging path is rejected, not silently followed."""
    real = tmp_path / "real-staging"
    real.mkdir()
    link = tmp_path / "staging-link"
    link.symlink_to(real, target_is_directory=True)
    repo = _fixture_repo(tmp_path)
    bin_dir, argv_log = _stub_bin_dir(tmp_path)
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    env = _setup_env(repo, link, install_dir, extra={
        "PATH": f"{bin_dir}{os.pathsep}{_CONTROLLED_PATH}",
        "RTK_ARGV_LOG": str(argv_log),
    })
    proc = _run_setup(env)
    assert proc.returncode != 0, "symlinked staging path must be rejected"
    assert _curl_calls(argv_log) == [], "symlinked staging must abort before any download"


# ---------------------------------------------------------------------------
# Regression pins for the in-review patches (convergent layers)
# ---------------------------------------------------------------------------


def test_real_version_output_format_no_v_prefix():
    """The real rtk 1.0.1 binary prints 'rtk 1.0.1' (no v). Byte-verified during against the actual release artifact (github.com/rtk-ai/rtk v1.0.1): `rtk --version` -> "rtk 1.0.1". The fixture stub must encode that exact shape or no test can catch a pin/version-format mismatch."""
    pin = PIN_FILE.read_text(encoding="utf-8").strip()
    assert pin.startswith("v") and pin[1:] == "1.0.1", pin
    src = SETUP.read_text(encoding="utf-8")
    # script derives the bare version token from the tag
    assert 'VERSION="${PIN#v}"' in src, "script must strip the tag's v for output matching"


def test_staging_symlink_with_trailing_slash_is_rejected(tmp_path):
    """[ -L x/ ] dereferences the link — guard must strip the slash."""
    real = tmp_path / "real-staging"
    real.mkdir()
    link = tmp_path / "staging-link"
    link.symlink_to(real, target_is_directory=True)
    repo = _fixture_repo(tmp_path)
    bin_dir, argv_log = _stub_bin_dir(tmp_path)
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    env = _setup_env(repo, f"{link}/", install_dir, extra={
        "PATH": f"{bin_dir}{os.pathsep}{_CONTROLLED_PATH}",
        "RTK_ARGV_LOG": str(argv_log),
    })
    proc = _run_setup(env)
    assert proc.returncode != 0, "trailing-slash symlink staging must be rejected"
    assert _curl_calls(argv_log) == [], "no download through a dereferenced staging link"


def test_pre_planted_staging_target_symlink_is_refused(tmp_path):
    """curl -o follows a pre-planted staging-file symlink; refuse it."""
    staging = tmp_path / "staging"
    staging.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("original", encoding="utf-8")
    (staging / EXPECTED_TARBALL).symlink_to(victim)
    repo = _fixture_repo(tmp_path)
    bin_dir, argv_log = _stub_bin_dir(tmp_path)
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    env = _setup_env(repo, staging, install_dir, extra={
        "PATH": f"{bin_dir}{os.pathsep}{_CONTROLLED_PATH}",
        "RTK_ARGV_LOG": str(argv_log),
    })
    proc = _run_setup(env)
    assert proc.returncode != 0, "pre-planted staging symlink must abort setup"
    assert _curl_calls(argv_log) == [], "no download when staging target is pre-planted"
    assert victim.read_text(encoding="utf-8") == "original", "victim file untouched"


def test_idempotency_gate_exact_token_not_substring_family(tmp_path):
    """'1.0.1' must not match a longer version token (1.0.10). The old substring grep (grep -Fq "$PIN") let v1.0.1 match output 'rtk v1.0.10' and silently skip provisioning a different version."""
    repo = _fixture_repo(tmp_path)
    bin_dir, argv_log = _stub_bin_dir(tmp_path)
    _rtk_stub(bin_dir, "rtk 1.0.10", flip=False)
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    env = _setup_env(repo, tmp_path / "staging", install_dir, extra={
        "PATH": f"{bin_dir}{os.pathsep}{_CONTROLLED_PATH}",
        "RTK_ARGV_LOG": str(argv_log),
    })
    proc = _run_setup(env)
    # stubbed curl writes junk bytes -> integrity gate must be REACHED (not skipped)
    assert _curl_calls(argv_log) != [], "a longer version must NOT trip the idempotency skip"
    assert proc.returncode != 0


def test_multi_rtk_archive_does_not_abort_under_pipefail(tmp_path):
    """+: find|head SIGPIPE-aborts under set -o pipefail. An archive containing many 'rtk'-named files killed the old `find. | head -n 1` pipeline with rc=141; -print -quit keeps rc=0."""
    procs = []
    # 400 rtk entries: empirically SIGPIPE-triggering for the old pipeline
    work = tmp_path / "decoy-work"
    work.mkdir()
    for i in range(400):
        d = work / f"d{i}"
        d.mkdir()
        (d / "rtk").write_text("#!/usr/bin/env bash\necho rtk 1.0.1\n", encoding="utf-8")
    tarball = tmp_path / EXPECTED_TARBALL
    with tarfile.open(tarball, "w:gz") as tf:
        for p in sorted(work.rglob("rtk")):
            tf.add(p, arcname=str(p.relative_to(work)))
    digest = hashlib.sha256(tarball.read_bytes()).hexdigest()

    assets = tmp_path / "assets"
    assets.mkdir()
    shutil.copy2(tarball, assets / EXPECTED_TARBALL)
    (tmp_path / EXPECTED_CHECKSUMS).write_text(f"{digest}  {EXPECTED_TARBALL}\n", encoding="utf-8")
    shutil.copy2(tmp_path / EXPECTED_CHECKSUMS, assets / EXPECTED_CHECKSUMS)
    repo = _fixture_repo(tmp_path)
    bin_dir, argv_log = _stub_bin_dir(tmp_path)
    # flip=True: the idempotency probe first reports a non-matching version so
    # the install path (extract -> self-check) actually runs and the multi-rtk
    # extraction is exercised; afterwards the stub reports the real version.
    _rtk_stub(bin_dir, "rtk 1.0.1", flip=True)
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    env = _setup_env(repo, tmp_path / "staging", install_dir, extra={
        "PATH": f"{bin_dir}{os.pathsep}{_CONTROLLED_PATH}",
        "RTK_ARGV_LOG": str(argv_log),
        "RTK_STUB_ASSETS": str(assets),
        "RTK_VERSION_LOG": str(tmp_path / "v.log"),
        "RTK_STUB_MARKER": str(tmp_path / "rtk-flip.marker"),
    })
    proc = _run_setup(env)
    procs.append(proc)
    assert procs[0].returncode == 0, f"multi-rtk archive must not abort: {procs[0].stderr}"
    assert (install_dir / "rtk").is_file(), "a binary must still be installed"


def test_plan_mode_remains_pure_query_when_rtk_installed(tmp_path):
    """plan seam must print URLs even when the pinned rtk is on PATH."""
    pin = PIN_FILE.read_text(encoding="utf-8").strip()
    repo = _fixture_repo(tmp_path)
    bin_dir, argv_log = _stub_bin_dir(tmp_path)
    _rtk_stub(bin_dir, f"rtk {pin[1:]}", flip=False)
    env = _plan_env(repo)
    env["PATH"] = f"{bin_dir}{os.pathsep}{_CONTROLLED_PATH}"
    env["RTK_ARGV_LOG"] = str(argv_log)
    proc = subprocess.run(
        ["bash", str(SETUP), "--print-download-plan"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    urls = [ln.split(" ", 1)[1] for ln in proc.stdout.splitlines() if ln.strip()]
    assert len(urls) == 2 and all(u.startswith("https://") for u in urls), urls
    assert not argv_log.exists() or not _curl_calls(argv_log), "plan mode must not download"


def test_unknown_argument_is_usage_error_not_silent_install(tmp_path):
    """an unrecognized flag must not trigger a full install run."""
    repo = _fixture_repo(tmp_path)
    bin_dir, argv_log = _stub_bin_dir(tmp_path)
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    env = _setup_env(repo, tmp_path / "staging", install_dir, extra={
        "PATH": f"{bin_dir}{os.pathsep}{_CONTROLLED_PATH}",
        "RTK_ARGV_LOG": str(argv_log),
    })
    proc = _run_setup(env, args=["--plan"])
    assert proc.returncode == 2, f"unknown arg must be a usage error: rc={proc.returncode}"
    assert _curl_calls(argv_log) == [], "unknown arg must never start a download"


def test_failed_run_cleans_staged_assets(tmp_path):
    """An aborted run must not leak the staged tarball or checksums. Before the exit trap, every failure path leaked the staged pair."""
    staging = tmp_path / "staging"
    # corrupt_checksum is now WARN+exit 0 (availability class), so
    # the hard-abort path is driven by a checksums file that lacks the pinned
    # entry (integrity/format class — still fail-hard per).
    bin_dir, argv_log = _stub_bin_dir(tmp_path)
    tmp_path.joinpath("assets-src").mkdir(exist_ok=True)
    assets = _stage_fixture_assets(tmp_path)
    (assets / EXPECTED_CHECKSUMS).write_text("deadbeef  unrelated.txt\n", encoding="utf-8")
    repo = _fixture_repo(tmp_path)
    pin = PIN_FILE.read_text(encoding="utf-8").strip()
    _rtk_stub(bin_dir, f"rtk {pin[1:]}")
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    env = _setup_env(repo, staging, install_dir, extra={
        "PATH": f"{bin_dir}{os.pathsep}{_CONTROLLED_PATH}",
        "RTK_ARGV_LOG": str(argv_log),
        "RTK_VERSION_LOG": str(tmp_path / "rtk-version.log"),
        "RTK_STUB_ASSETS": str(assets),
        "RTK_STUB_MARKER": str(tmp_path / "rtk-flip.marker"),
    })
    (tmp_path / "rtk-version.log").write_text("", encoding="utf-8")
    proc = _run_setup(env)
    assert proc.returncode != 0
    left = sorted(p.name for p in staging.iterdir()) if staging.exists() else []
    assert not left, f"failed run must not leave staged artifacts: {left}"
    assert not (install_dir / "rtk").exists()


# ---------------------------------------------------------------------------
# Warn-Not-Fail Setup Resilience (.)
# Network/availability failures must exit 0 with a clear operator warning and
# never install anything; integrity/format failures stay fail-hard (2.2
# contract — resilience must not weaken security). Network-free: curl is a
# PATH stub whose exit code is configurable via RTK_CURL_RC.
# ---------------------------------------------------------------------------

# Warn-class network failures: curl non-zero exit on the download loop.
CURL_NETWORK_RCS = {
    "connection_refused": 7,
    "timeout": 28,
    "http_404_via_fail": 22,
    "dns_failure": 6,
    "redirect_unsupported": 3,
}

CURL_FAIL_STUB_BODY = '''#!/usr/bin/env bash
printf '%s\n' "$*" >> "$RTK_ARGV_LOG"
out=
while [ $# -gt 0 ]; do
  if [ "$1" = "-o" ]; then out="$2"; fi
  shift
done
# RTK_CURL_PARTIAL>0 plants N bytes before failing (partial-download sim).
if [ -n "$out" ] && [ "${RTK_CURL_PARTIAL:-0}" != "0" ]; then
  head -c "${RTK_CURL_PARTIAL}" /dev/zero | tr '\0' 'x' > "$out"
fi
exit "${RTK_CURL_RC:-7}"
'''


def _fail_curl_stub(tmp_path: Path, rc: int, partial_bytes: int = 0) -> Path:
    """PATH-first curl stub that fails with rc (optionally after planting bytes)."""
    bin_dir = tmp_path / "stubbin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "curl"
    stub.write_text(CURL_FAIL_STUB_BODY, encoding="utf-8")
    stub.chmod(0o755)
    # rc/partial are injected by callers as RTK_CURL_RC / RTK_CURL_PARTIAL
    # env vars, read by the stub at exec time.
    return bin_dir


def _run_network_failure(tmp_path: Path, rc: int, partial_bytes: int = 0, pin_text: str | None = None):
    """Full setup run where the curl stub fails with rc. Returns (proc, install_dir, argv_log, staging)."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    bin_dir = _fail_curl_stub(tmp_path, rc, partial_bytes=partial_bytes)
    repo = _fixture_repo(tmp_path)
    if pin_text is not None:
        (repo / "rtk_pin.txt").write_text(pin_text, encoding="utf-8")
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    staging = tmp_path / "staging"
    argv_log = tmp_path / "curl-argv.log"
    # isolation (guard-adjacency lesson): no host rtk on PATH
    # can satisfy the idempotency gate — the stub bin dir carries only curl.
    env = _setup_env(repo, staging, install_dir, extra={
        # prepend (not replace) so bash/coreutils still resolve; the stub
        # dir carries only the failing curl.
        "PATH": f"{bin_dir}{os.pathsep}{_CONTROLLED_PATH}",
        "RTK_ARGV_LOG": str(argv_log),
        "RTK_CURL_RC": str(rc),
        "RTK_CURL_PARTIAL": str(partial_bytes),
    })
    proc = _run_setup(env)
    return proc, install_dir, argv_log, staging


def _combined(proc) -> str:
    """Two-sided normalized combined output ( two-sided comparison discipline)."""
    return (proc.stdout + "\n" + proc.stderr).strip()


import pytest  # noqa: E402  (test dependency imported near its first parametrize use)


@pytest.mark.parametrize("name,rc", sorted(CURL_NETWORK_RCS.items()))
def test_network_failure_warns_and_exits_zero(tmp_path, name, rc):
    """every curl network failure exit code -> exit 0 + warning, no install. Parametrized over the canonical curl exit codes: connection refused (7), operation timeout (28), HTTP error via --fail (22), DNS failure (6), unsupported redirect (3). /: agent-usable plugin, never blocked."""
    proc, install_dir, argv_log, staging = _run_network_failure(tmp_path, rc)
    out = _combined(proc)
    assert proc.returncode == 0, f"{name}: network failure must exit 0, got {proc.returncode}: {out}"
    low = out.lower()
    assert "warn" in low, f"{name}: no warning emitted: {out}"
    assert "fail open" in low or "rtk unavailable" in low, f"{name}: warning lacks consequence: {out}"
    assert not (install_dir / "rtk").exists(), f"{name}: no install on network failure"
    assert _curl_calls(argv_log), f"{name}: download was attempted (expected)"
    assert not (staging / EXPECTED_TARBALL).exists(), f"{name}: staged tarball must be cleaned"


def test_partial_download_warns_not_fails(tmp_path):
    """curl writes some bytes then fails (rc 18) -> warn + exit 0 + no install."""
    proc, install_dir, argv_log, staging = _run_network_failure(tmp_path, rc=18, partial_bytes=512)
    out = _combined(proc)
    assert proc.returncode == 0, f"partial download must exit 0: {out}"
    assert "warn" in out.lower(), f"no warning on partial download: {out}"
    assert not (install_dir / "rtk").exists(), "no install on partial download"
    assert _curl_calls(argv_log), "download was attempted"
    # cleanup trap must still remove the partial staged tarball
    assert not (staging / EXPECTED_TARBALL).exists(), "partial staging must be cleaned"


def test_checksum_mismatch_warns_not_fails(tmp_path):
    """A corrupt download (sha mismatch) warns and exits 0 without installing: nothing installs, staging is discarded, and a pre-existing binary is untouched."""
    pin = PIN_FILE.read_text(encoding="utf-8").strip()
    # Pre-existing installed binary must survive the warned run untouched.
    pre_install = tmp_path / "preinstall"
    pre_install.mkdir()
    victim = pre_install / "rtk"
    victim.write_text("PRE-EXISTING BINARY\n", encoding="utf-8")
    victim_stat_before = victim.stat().st_mtime_ns
    proc, install_dir, _bin, _vlog, _log = _run_full(
        tmp_path,
        corrupt_checksum=True,
        version_text=f"rtk {pin[1:]}",
    )
    out = _combined(proc)
    assert proc.returncode == 0, f"mismatch must warn-not-fail now, got {proc.returncode}: {out}"
    assert "warn" in out.lower(), f"no warning on mismatch: {out}"
    assert not (install_dir / "rtk").exists(), "corrupt download must not install"
    assert not (tmp_path / "staging" / EXPECTED_TARBALL).exists(), "staging discarded after warn"
    assert victim.read_text(encoding="utf-8") == "PRE-EXISTING BINARY\n", "pre-existing binary untouched"
    assert victim.stat().st_mtime_ns == victim_stat_before, "pre-existing binary mtime unchanged"


def test_self_check_failure_warns_and_rolls_back(tmp_path):
    """installed binary failing the version self-check -> warn + exit 0. The static stub always reports a foreign version: the idempotency gate passes (not matching), install proceeds, self-check fails, rollback fires, and setup exits 0 with a warning (never crashes)."""
    proc, install_dir, _bin, _vlog, _log = _run_full(
        tmp_path,
        version_text="rtk 9.9.9-fake-corrupt",
    )
    out = _combined(proc)
    assert proc.returncode == 0, f"self-check failure must warn-not-fail, got {proc.returncode}: {out}"
    assert "warn" in out.lower(), f"no warning on self-check failure: {out}"
    assert not (install_dir / "rtk").exists(), "wrong-version binary rolled back"


def test_warned_setup_leaves_snapshot_clean(tmp_path):
    """after warned exit-0 runs the plugin tree has no new files. Captures the full plugin-root snapshot before/after and asserts no stray state was written into the repo (warn paths must stay read-only w.r.t. the project tree)."""
    before = {p.relative_to(PLUGIN_ROOT): p.stat().st_mtime_ns
              for p in PLUGIN_ROOT.rglob("*") if ".pytest_cache" not in p.parts and "__pycache__" not in p.parts and "graphify-out" not in p.parts}
    proc, install_dir, _log, _staging = _run_network_failure(tmp_path, rc=7)
    assert proc.returncode == 0, _combined(proc)
    after = {p.relative_to(PLUGIN_ROOT): p.stat().st_mtime_ns
             for p in PLUGIN_ROOT.rglob("*") if ".pytest_cache" not in p.parts and "__pycache__" not in p.parts and "graphify-out" not in p.parts}
    assert before == after, "warned run must not modify the plugin tree"


def test_plan_seam_still_pure_after_warned_run(tmp_path):
    """a warned network run leaves --print-download-plan fully functional. Bridge guard for: hooks may call setup.sh, then the plan seam must still resolve the pin (no environment/state damage from warnings)."""
    proc, _install_dir, _log, _staging = _run_network_failure(tmp_path, rc=28)
    assert proc.returncode == 0, _combined(proc)
    tag = PIN_FILE.read_text(encoding="utf-8").strip()
    pairs = _run_plan(PLUGIN_ROOT)
    by_name = dict(pairs)
    assert by_name[EXPECTED_TARBALL] == f"{EXPECTED_BASE}/{tag}/{EXPECTED_TARBALL}"


def test_classification_separation_network_vs_integrity(tmp_path):
    """the two failure classes must never invert. (full fail-fast parametrization) consumed here: the same run shape, one knob switched — network failure exits 0+warning, integrity failure exits non-zero+error. A single test pinning both directions in one place is the cheapest guard against an over-broad resilience conversion (e.g. converting sha256 mismatch by accident)."""
    # (a) network failure -> exit 0 + warning (own sub-root)
    net_proc, net_install, _log, _st = _run_network_failure(
        tmp_path / "net", rc=22)
    assert net_proc.returncode == 0, f"network class must exit 0: {_combined(net_proc)}"
    assert "warn" in _combined(net_proc).lower()
    assert not (net_install / "rtk").exists()
    # (b) integrity failure (malformed pin) -> non-zero + error, no install
    repo = _fixture_repo(tmp_path)
    (repo / "rtk_pin.txt").write_text("garbage-pin\n", encoding="utf-8")
    bin_dir = tmp_path / "stubbin2"
    bin_dir.mkdir()
    install_dir = tmp_path / "install2"
    install_dir.mkdir()
    env = _setup_env(repo, tmp_path / "staging2", install_dir, extra={
        "PATH": f"{bin_dir}{os.pathsep}{_CONTROLLED_PATH}",
    })
    int_proc = _run_setup(env)
    assert int_proc.returncode != 0, f"integrity class must fail hard: {_combined(int_proc)}"
    assert not (install_dir / "rtk").exists()


def test_sha_mismatch_never_converts(tmp_path):
    """explicit separation pin: SHA-256 mismatch stays in the WARN class (corrupted download is availability-shaped, ) while NO-ENTRY stays fail-hard (integrity manifest corruption is a format/integrity error). This pins the exact boundary of the conversion against accidental whole-script resilience (e.g. wrapping everything in `|| warn`)."""
    pin = PIN_FILE.read_text(encoding="utf-8").strip()
    # mismatch -> warn + exit 0
    proc, install_dir, _bin, _vlog, _log = _run_full(
        tmp_path, corrupt_checksum=True, version_text=f"rtk {pin[1:]}")
    assert proc.returncode == 0, _combined(proc)
    assert "warn" in _combined(proc).lower()
    assert not (install_dir / "rtk").exists()


def test_no_checksums_entry_stays_fail_hard(tmp_path):
    """checksums.txt without the tarball entry is a format error (hard). Complements test_sha_mismatch_never_converts: a checksums file that is present but does not describe the pinned asset is NOT a network failure."""
    bin_dir, argv_log = _stub_bin_dir(tmp_path)
    tmp_path.joinpath("assets-src").mkdir(exist_ok=True)
    assets = _stage_fixture_assets(tmp_path)
    # Remove the tarball entry, keep an unrelated line.
    cs = assets / EXPECTED_CHECKSUMS
    cs.write_text("deadbeef  unrelated-asset.txt\n", encoding="utf-8")
    repo = _fixture_repo(tmp_path)
    pin = PIN_FILE.read_text(encoding="utf-8").strip()
    _rtk_stub(bin_dir, f"rtk {pin[1:]}")
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    env = _setup_env(repo, tmp_path / "staging", install_dir, extra={
        "PATH": f"{bin_dir}{os.pathsep}{_CONTROLLED_PATH}",
        "RTK_ARGV_LOG": str(argv_log),
        "RTK_VERSION_LOG": str(tmp_path / "rtk-version.log"),
        "RTK_STUB_ASSETS": str(assets),
        "RTK_STUB_MARKER": str(tmp_path / "rtk-flip.marker"),
    })
    (tmp_path / "rtk-version.log").write_text("", encoding="utf-8")
    proc = _run_setup(env)
    assert proc.returncode != 0, f"no-entry checksums must stay fail-hard: {_combined(proc)}"
    assert not (install_dir / "rtk").exists()


def test_agent_fail_open_bridge_pointer(tmp_path):
    """Setup.sh owns provisioning only. The runtime contract - the agent stays fully functional with uncompressed commands when rtk is absent - is proven by the intercept test suite in the framework runtime; this guard fails if that suite disappears or is renamed."""
    intercept_suite = PLUGIN_ROOT / "tests" / "test_rtk_intercept.py"
    assert intercept_suite.is_file(), "bridge: intercept test suite must exist"
    src = intercept_suite.read_text(encoding="utf-8")
    assert "which" in src, "bridge: suite must exercise the availability guard"


# ---------------------------------------------------------------------------
# regression tests (/T18v2/-#4):
# install-surface and classification boundaries pinned by the review.
# Each test below fails if its corresponding in-review patch is reverted
# (mutation-validated).
# ---------------------------------------------------------------------------




def test_install_target_symlink_is_refused(tmp_path):
    """a pre-planted symlink at the install path must be refused before cp writes through it."""
    tmp_path.joinpath("assets-src").mkdir(exist_ok=True)
    assets = _stage_fixture_assets(tmp_path)
    repo = _fixture_repo(tmp_path)
    bin_dir, argv_log = _stub_bin_dir(tmp_path)
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    outside = tmp_path / "outside-victim"
    outside.write_text("OUTSIDE-VICTIM\n", encoding="utf-8")
    (install_dir / "rtk").symlink_to(outside)
    pin = PIN_FILE.read_text(encoding="utf-8").strip()
    _rtk_stub(bin_dir, f"rtk {pin[1:]}")
    env = _setup_env(repo, tmp_path / "staging", install_dir, extra={
        "PATH": f"{bin_dir}{os.pathsep}{_CONTROLLED_PATH}",
        "RTK_ARGV_LOG": str(argv_log),
        "RTK_STUB_ASSETS": str(assets),
    })
    proc = _run_setup(env)
    assert proc.returncode != 0, f"symlinked install target must be refused: {_combined(proc)}"
    assert outside.read_text(encoding="utf-8") == "OUTSIDE-VICTIM\n", "victim must not be written through the link"


def test_install_target_directory_is_refused(tmp_path):
    """v2: a directory at the install path must be refused (cp would nest the binary inside it and rollback rm -f would fail)."""
    tmp_path.joinpath("assets-src").mkdir(exist_ok=True)
    assets = _stage_fixture_assets(tmp_path)
    repo = _fixture_repo(tmp_path)
    bin_dir, argv_log = _stub_bin_dir(tmp_path)
    install_dir = tmp_path / "install"
    (install_dir / "rtk").mkdir(parents=True)
    pin = PIN_FILE.read_text(encoding="utf-8").strip()
    _rtk_stub(bin_dir, f"rtk {pin[1:]}")
    env = _setup_env(repo, tmp_path / "staging", install_dir, extra={
        "PATH": f"{bin_dir}{os.pathsep}{_CONTROLLED_PATH}",
        "RTK_ARGV_LOG": str(argv_log),
        "RTK_STUB_ASSETS": str(assets),
    })
    proc = _run_setup(env)
    assert proc.returncode != 0, f"directory install target must be refused: {_combined(proc)}"
    assert not (install_dir / "rtk" / "rtk").exists(), "binary must not be nested inside the directory"


def test_install_surface_failure_warns_not_fails(tmp_path):
    """An install-surface failure (mid-write copy failure or unwritable install dir) must classify as availability: warn and exit 0, leave no partial binary on PATH, and restore any pre-existing binary. Previously this exited 1, which the exit contract reserves for integrity corruption."""
    tmp_path.joinpath("assets-src").mkdir(exist_ok=True)
    assets = _stage_fixture_assets(tmp_path)
    repo = _fixture_repo(tmp_path)
    bin_dir, argv_log = _stub_bin_dir(tmp_path)
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    victim = install_dir / "rtk"
    victim.write_text("PRE-EXISTING-BINARY-2-3-CR\n", encoding="utf-8")
    # Failing cp wrapper (ENOSPC/EIO shape): fails ONLY when the destination
    # is the install target; every other cp (curl asset staging, snapshot)
    # runs the real binary — a global shadow would break the download stub.
    # One-shot failing cp: the FIRST write to the install target plants
    # partial garbage and exits 1 (ENOSPC); every later write (the restore)
    # passes through to the real cp. Modeled with a marker file so the retry
    # semantics of the script can be exercised.
    (bin_dir / "cp").write_text(
        "#!/usr/bin/env bash\n"
        "args=(); for a in \"$@\"; do case \"$a\" in -*) ;; *) args+=(\"$a\") ;; esac; done\n"
        "if [ \"${args[1]}\" = \"$RTK_INSTALL_TARGET\" ] && [ ! -f \"$RTK_FAIL_MARKER\" ]; then\n"
        "  : > \"$RTK_FAIL_MARKER\"\n"
        "  printf 'PARTIAL-GARBAGE' > \"${args[1]}\"\n"
        "  exit 1\n"
        "fi\n"
        "exec /usr/bin/cp \"$@\"\n",
        encoding="utf-8")
    (bin_dir / "cp").chmod(0o755)
    pin = PIN_FILE.read_text(encoding="utf-8").strip()
    _rtk_stub(bin_dir, f"rtk {pin[1:]}")
    env = _setup_env(repo, tmp_path / "staging", install_dir, extra={
        "PATH": f"{bin_dir}{os.pathsep}{_CONTROLLED_PATH}",
        "RTK_ARGV_LOG": str(argv_log),
        "RTK_STUB_ASSETS": str(assets),
        "RTK_STUB_MARKER": str(tmp_path / "rtk-flip.marker"),
        "RTK_VERSION_LOG": str(tmp_path / "rtk-version.log"),
        "RTK_VICTIM": str(victim),
        "RTK_INSTALL_TARGET": str(install_dir / "rtk"),
        "RTK_FAIL_MARKER": str(tmp_path / "cp-fail.marker"),
    })
    proc = _run_setup(env)
    out = _combined(proc)
    assert proc.returncode == 0, f"install-surface failure must warn-not-fail (exit 0), got {proc.returncode}: {out}"
    assert "warn" in out.lower(), f"no operator warning on install failure: {out}"
    assert "rtk unavailable" in out.lower() or "fail open" in out.lower(), f"warning lacks consequence: {out}"
    assert not (victim.read_text(encoding="utf-8") if victim.exists() else "").startswith("PARTIAL-GARBAGE"), \
        "partial garbage binary left on PATH"
    assert victim.exists(), "pre-existing binary must survive the failed install"
    assert victim.read_text(encoding="utf-8") == "PRE-EXISTING-BINARY-2-3-CR\n", \
        f"pre-existing binary must be restored, found: {victim.read_text(encoding='utf-8')!r}"


def test_install_surface_failure_without_pre_existing_removes_partial(tmp_path):
    """control for the install-surface patch: with NO pre-existing binary a failed install must remove the partial copy, leaving nothing."""
    tmp_path.joinpath("assets-src").mkdir(exist_ok=True)
    assets = _stage_fixture_assets(tmp_path)
    repo = _fixture_repo(tmp_path)
    bin_dir, argv_log = _stub_bin_dir(tmp_path)
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    # One-shot failing cp: the FIRST write to the install target plants
    # partial garbage and exits 1 (ENOSPC); every later write (the restore)
    # passes through to the real cp. Modeled with a marker file so the retry
    # semantics of the script can be exercised.
    (bin_dir / "cp").write_text(
        "#!/usr/bin/env bash\n"
        "args=(); for a in \"$@\"; do case \"$a\" in -*) ;; *) args+=(\"$a\") ;; esac; done\n"
        "if [ \"${args[1]}\" = \"$RTK_INSTALL_TARGET\" ] && [ ! -f \"$RTK_FAIL_MARKER\" ]; then\n"
        "  : > \"$RTK_FAIL_MARKER\"\n"
        "  printf 'PARTIAL-GARBAGE' > \"${args[1]}\"\n"
        "  exit 1\n"
        "fi\n"
        "exec /usr/bin/cp \"$@\"\n",
        encoding="utf-8")
    (bin_dir / "cp").chmod(0o755)
    pin = PIN_FILE.read_text(encoding="utf-8").strip()
    _rtk_stub(bin_dir, f"rtk {pin[1:]}")
    env = _setup_env(repo, tmp_path / "staging", install_dir, extra={
        "PATH": f"{bin_dir}{os.pathsep}{_CONTROLLED_PATH}",
        "RTK_ARGV_LOG": str(argv_log),
        "RTK_STUB_ASSETS": str(assets),
        "RTK_STUB_MARKER": str(tmp_path / "rtk-flip.marker"),
        "RTK_VERSION_LOG": str(tmp_path / "rtk-version.log"),
        "RTK_INSTALL_TARGET": str(install_dir / "rtk"),
    })
    proc = _run_setup(env)
    out = _combined(proc)
    assert proc.returncode == 0, f"install failure must warn-not-fail: {out}"
    assert not (install_dir / "rtk").exists(), "partial binary must be removed when nothing pre-existed"


def test_pre_existing_binary_restored_on_self_check_failure(tmp_path):
    """When the self-check fails after install, rollback restores the pre-existing working binary rather than deleting it, so the host never ends up worse than before."""
    tmp_path.joinpath("assets-src").mkdir(exist_ok=True)
    assets = _stage_fixture_assets(tmp_path)
    repo = _fixture_repo(tmp_path)
    bin_dir, argv_log = _stub_bin_dir(tmp_path)
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    victim = install_dir / "rtk"
    victim.write_text("PRE-EXISTING-WORKING-BINARY\n", encoding="utf-8")
    # Foreign version always: idempotency gate passes (no match), install
    # proceeds, self-check fails, rollback must restore the victim.
    _rtk_stub(bin_dir, "rtk 9.9.9-not-installed", flip=False)
    env = _setup_env(repo, tmp_path / "staging", install_dir, extra={
        "PATH": f"{bin_dir}{os.pathsep}{_CONTROLLED_PATH}",
        "RTK_ARGV_LOG": str(argv_log),
        "RTK_STUB_ASSETS": str(assets),
    })
    proc = _run_setup(env)
    out = _combined(proc)
    assert proc.returncode == 0, f"self-check failure must warn-not-fail: {out}"
    assert "warn" in out.lower(), f"no warning on rollback: {out}"
    assert victim.exists(), "pre-existing binary must be restored, not deleted"
    assert victim.read_text(encoding="utf-8") == "PRE-EXISTING-WORKING-BINARY\n", \
        f"restored content must equal the original, found {victim.read_text(encoding='utf-8')!r}"


def test_self_check_failure_without_pre_existing_rolls_back_clean(tmp_path):
    """control for the restore patch: with NO pre-existing binary the rollback must still remove the wrong-version install (old behavior)."""
    proc, install_dir, _bin, _vlog, _log = _run_full(
        tmp_path, version_text="rtk 9.9.9-fake-corrupt")
    assert proc.returncode == 0, _combined(proc)
    assert not (install_dir / "rtk").exists(), "wrong-version install must be removed when nothing pre-existed"


def test_missing_checksums_warns_not_fails(tmp_path):
    """The missing-checksums-file availability branch (checksums never downloaded) must be pinned; the legacy no-entry integrity test never reaches this branch."""
    tmp_path.joinpath("assets-src").mkdir(exist_ok=True)
    tarball, digest = _make_fake_tarball(tmp_path / "assets-src")
    assets = tmp_path / "assets"
    assets.mkdir()
    shutil.copy2(tarball, assets / EXPECTED_TARBALL)
    repo = _fixture_repo(tmp_path)
    bin_dir, argv_log = _stub_bin_dir(tmp_path)
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    pin = PIN_FILE.read_text(encoding="utf-8").strip()
    _rtk_stub(bin_dir, f"rtk {pin[1:]}")
    # Stub curl: copies the tarball from the asset dir; for checksums it
    # exits 0 WITHOUT writing the -o target (the landed-nothing shape).
    (bin_dir / "curl").write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$RTK_ARGV_LOG\"\n"
        "out=\n"
        "while [ $# -gt 0 ]; do if [ \"$1\" = \"-o\" ]; then out=\"$2\"; fi; shift; done\n"
        "base=$(basename \"$out\")\n"
        "if [ -f \"${RTK_STUB_ASSETS}/$base\" ]; then cp \"${RTK_STUB_ASSETS}/$base\" \"$out\"; fi\n"
        "exit 0\n",
        encoding="utf-8")
    (bin_dir / "curl").chmod(0o755)
    env = _setup_env(repo, tmp_path / "staging", install_dir, extra={
        "PATH": f"{bin_dir}{os.pathsep}{_CONTROLLED_PATH}",
        "RTK_ARGV_LOG": str(argv_log),
        "RTK_STUB_ASSETS": str(assets),
        "RTK_STUB_MARKER": str(tmp_path / "rtk-flip.marker"),
        "RTK_VERSION_LOG": str(tmp_path / "rtk-version.log"),
    })
    proc = _run_setup(env)
    out = _combined(proc)
    assert proc.returncode == 0, f"missing checksums FILE must warn-not-fail, got {proc.returncode}: {out}"
    assert "warn" in out.lower(), f"no warning on missing checksums file: {out}"
    assert not (install_dir / "rtk").exists(), "no install without checksums"


def test_failure_classification_structural_audit():
    """A structural audit of every exit site in setup.sh: availability branches route through the warn-not-fail helper (exit 0), while integrity and usage branches are raw nonzero exits after an error line. Guards against over-broad conversion."""
    src = SETUP.read_text(encoding="utf-8")
    lines = src.splitlines()
    # Availability call sites: rtk_unavailable invocations (not its definition).
    availability_calls = [i for i, ln in enumerate(lines, 1)
                          if ln.strip().startswith("rtk_unavailable ")]
    assert len(availability_calls) >= 5, \
        f"expected >=5 availability conversion call sites (download, missing-checksums, vanished-tarball, sha-mismatch, self-check), found {len(availability_calls)}"
    # Fail-hard/usage sites: raw exits with non-zero codes. tar extraction
    # is handled by set -e (no explicit exit), so the raw-exit floor is 7:
    # malformed pin, no-entry, no-rtk-in-archive, staging symlink, staging
    # target, install target, unknown-arg.
    hard_exits = [i for i, ln in enumerate(lines, 1)
                  if "exit 1" in ln or "exit 2" in ln]
    assert len(hard_exits) >= 7, \
        f"expected >=7 fail-hard/usage exit sites, found {len(hard_exits)}"
    # Exactly one definition of rtk_unavailable.
    unavailable_def = [i for i, ln in enumerate(lines, 1) if ln.strip() == "rtk_unavailable() {"]
    assert len(unavailable_def) == 1, f"rtk_unavailable must be defined exactly once, found {len(unavailable_def)}"


def test_no_broad_resilience_conversion(tmp_path):
    """boundary: the malformed-pin class must stay fail-hard even while every availability class warns — pins against a future patch that wraps everything in `|| rtk_unavailable`."""
    repo = _fixture_repo(tmp_path)
    (repo / "rtk_pin.txt").write_text("garbage-pin\n", encoding="utf-8")
    bin_dir, argv_log = _stub_bin_dir(tmp_path)
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    env = _setup_env(repo, tmp_path / "staging", install_dir, extra={
        "PATH": f"{bin_dir}{os.pathsep}{_CONTROLLED_PATH}",
    })
    proc = _run_setup(env)
    assert proc.returncode != 0, f"malformed pin must stay fail-hard: {_combined(proc)}"
    assert not (install_dir / "rtk").exists(), "no install on malformed pin"
    assert "is: command not found" not in proc.stderr, "script must not hit bash syntax errors"


def test_staged_tarball_missing_before_verify_warns(tmp_path):
    """a tarball that vanished before verification must classify as availability (a set -e exit reads as integrity corruption)."""
    tmp_path.joinpath("assets-src").mkdir(exist_ok=True)
    assets = _stage_fixture_assets(tmp_path)
    repo = _fixture_repo(tmp_path)
    bin_dir, argv_log = _stub_bin_dir(tmp_path)
    install_dir = tmp_path / "install"
    install_dir.mkdir()
    pin = PIN_FILE.read_text(encoding="utf-8").strip()
    _rtk_stub(bin_dir, f"rtk {pin[1:]}")
    # Deterministic stub: copies checksums normally but never lands the
    # tarball (exit 0 without the -o write) — the vanished-asset shape.
    (bin_dir / "curl").write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s\\n' \"$*\" >> \"$RTK_ARGV_LOG\"\n"
        "out=\n"
        "while [ $# -gt 0 ]; do if [ \"$1\" = \"-o\" ]; then out=\"$2\"; fi; shift; done\n"
        "base=$(basename \"$out\")\n"
        "if [ \"$base\" = \"$RTK_STUB_CHECKSUMS_ONLY\" ]; then cp \"${RTK_STUB_ASSETS}/$base\" \"$out\"; fi\n"
        "exit 0\n",
        encoding="utf-8")
    (bin_dir / "curl").chmod(0o755)
    env = _setup_env(repo, tmp_path / "staging", install_dir, extra={
        "PATH": f"{bin_dir}{os.pathsep}{_CONTROLLED_PATH}",
        "RTK_ARGV_LOG": str(argv_log),
        "RTK_STUB_ASSETS": str(assets),
        "RTK_STUB_MARKER": str(tmp_path / "rtk-flip.marker"),
        "RTK_VERSION_LOG": str(tmp_path / "rtk-version.log"),
        "RTK_STUB_CHECKSUMS_ONLY": EXPECTED_CHECKSUMS,
    })
    proc = _run_setup(env)
    out = _combined(proc)
    assert proc.returncode == 0, f"vanished tarball must classify as availability: {out}"
    assert "warn" in out.lower(), f"no warning on vanished tarball: {out}"
    assert not (install_dir / "rtk").exists(), "no install without the tarball"
