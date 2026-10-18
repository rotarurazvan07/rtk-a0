#!/usr/bin/env bash
# RTK binary provisioning: pinned download, integrity verification, PATH
# install.
#
# The release tag lives only in rtk_pin.txt at the plugin root; this script
# reads it at runtime and never hardcodes it. A pin bump edits that one file.
#
# Availability failures (network, missing download, corrupted transfer, failing
# self-check) warn and exit 0: the plugin stays installed and the agent fails
# open. Integrity/format failures still fail hard - never install an
# unverified binary.
# Exit contract: 0 = provisioned or warned-unavailable; 2 = usage error;
# other non-zero = integrity/format corruption (operator must inspect).
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# RTK_PROJECT_ROOT keeps the script CWD-independent and testable; normally
# it is the plugin root, one level above scripts/.
PROJECT_ROOT="${RTK_PROJECT_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
# Normalize the override the same way the default branch is normalized:
# relative overrides must not resolve against the caller cwd.
if [ -n "${RTK_PROJECT_ROOT:-}" ]; then
  PROJECT_ROOT="$(cd -P -- "$PROJECT_ROOT" && pwd)"
fi

# The single place the release tag enters the program.
PIN=$(cat "${PROJECT_ROOT}/rtk_pin.txt")

BASE_URL=https://github.com/rtk-ai/rtk/releases/download
ASSET_TARBALL=rtk-x86_64-unknown-linux-musl.tar.gz
ASSET_CHECKSUMS=checksums.txt
STAGING_DIR="${RTK_STAGING_DIR:-${TMPDIR:-/tmp}/rtk-a0-staging}"
# RTK_INSTALL_DIR keeps the install step testable; production default is
# the container OS bin dir - never the framework venv.
INSTALL_DIR="${RTK_INSTALL_DIR:-/usr/local/bin}"

# Pin-content validation: a malformed pin (garbage, empty, multi-line,
# suffix) must fail before any URL is built or download attempted. Bash regex
# anchors apply to the whole string, unlike line-wise grep. The pin carries
# the release-tag form (vN.N.N) because that is the exact GitHub release tag;
# runtime version output is matched below without the v (the real binary
# prints e.g. "rtk 1.0.1").
if [[ ! "$PIN" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "rtk setup: malformed pin '${PIN}' in ${PROJECT_ROOT}/rtk_pin.txt (expected vN.N.N)" >&2
  exit 1
fi
# Runtime version token for output matching: the tag minus its leading v.
VERSION="${PIN#v}"
# Exact-token version comparison: split the version output on whitespace and
# require a whole token equal to VERSION - a substring grep would let
# "1.0.1" match "1.0.10".
rtk_version_matches() {
  rtk --version 2>/dev/null | tr -s '[:space:]' '\n' | grep -Fxq -- "$VERSION"
}

# Warn-not-fail operators. Every availability failure (network, missing
# download) ends in a clear operator warning and exit 0: the plugin stays
# installed and fails open at runtime. Integrity failures (malformed pins,
# missing checksums entries, corrupt archives, staging guards) keep
# fail-hard below.
rtk_warn() { echo "rtk setup: WARNING: $*" >&2; }
rtk_unavailable() {
  rtk_warn "$*"
  rtk_warn "rtk unavailable; the plugin stays installed and the agent will run terminal commands uncompressed (failing open, FR15/NFR6)"
  exit 0
}

# --print-download-plan: resolve the pinned URLs and exit without any network
# access (testability seam; keeps the test suite network-free). Placed before
# the idempotency/symlink gates so the seam stays a pure read-only query even
# on hosts where rtk is installed. An unknown argument is a usage error,
# never a silent full install. Output: two lines, "<asset-name> <https-url>".
case "${1:-}" in
  --print-download-plan)
    printf '%s %s\n' "$ASSET_TARBALL" "$BASE_URL/$PIN/$ASSET_TARBALL"
    printf '%s %s\n' "$ASSET_CHECKSUMS" "$BASE_URL/$PIN/$ASSET_CHECKSUMS"
    exit 0
    ;;
  "") ;;
  *)
    echo "rtk setup: unknown argument '${1}' (expected --print-download-plan)" >&2
    exit 2
    ;;
esac

# Idempotent skip: a version-matching binary already on PATH means there is
# nothing to do - no download, no rewrite, no staging writes.
if command -v rtk >/dev/null 2>&1 && rtk_version_matches; then
  echo "rtk setup: rtk $VERSION already on PATH — skipping download and install"
  exit 0
fi

# Symlink hardening: reject a symlinked staging path before any normalization
# resolves it away (cd -P would hide the link). The trailing slash is
# stripped for the test - bash `[ -L x/ ]` dereferences the link and would
# otherwise bypass this guard.
if [ -L "${STAGING_DIR%/}" ]; then
  echo "rtk setup: staging path is a symlink, refusing: $STAGING_DIR" >&2
  exit 1
fi

# Normalize relative overrides to absolute, but only when the directory
# already exists: plan mode and first-run staging point at not-yet-created
# dirs, where cd would fail under set -e; mkdir -p below creates them either
# way.
if [ -n "${RTK_STAGING_DIR:-}" ] && [ -d "$STAGING_DIR" ]; then
  STAGING_DIR="$(cd -P -- "$STAGING_DIR" && pwd)"
fi
mkdir -p "$STAGING_DIR"

# Full edition (the rtk-oss edition lacks the engine) plus the checksums
# manifest used by verify/install. Redirects are constrained to https; sha256
# verification below is the designed integrity mitigation for cross-host
# release-asset redirects.
# Cleanup on every exit path: the trap is armed before any download so a
# mismatch or failed self-check cannot leak the staged assets or extract dir.
cleanup() {
  [ -n "${EXTRACT_DIR:-}" ] && rm -rf "$EXTRACT_DIR"
  rm -f "$STAGING_DIR/$ASSET_TARBALL" "$STAGING_DIR/$ASSET_CHECKSUMS" "$STAGING_DIR/rtk-preexisting.bak"
}
trap cleanup EXIT

for asset in "$ASSET_TARBALL" "$ASSET_CHECKSUMS"; do
  # Refuse a pre-existing symlink/regular target: curl -o follows symlinks,
  # so a planted link would write through to its target.
  if [ -L "$STAGING_DIR/$asset" ] || [ -e "$STAGING_DIR/$asset" ]; then
    echo "rtk setup: staging target already exists, refusing: $STAGING_DIR/$asset" >&2
    exit 1
  fi
  # Availability conversion: any curl non-zero exit (connection refused 7,
  # timeout 28, HTTP error 22, DNS 6, unsupported redirect 3, partial
  # transfer 18/35) is a network failure - warn and exit 0 instead of
  # aborting plugin provisioning.
  if ! curl -fsSL --fail --retry 2 --proto-redir =https --max-redirs 5 \
      -o "$STAGING_DIR/$asset" "$BASE_URL/$PIN/$asset"; then
    rtk_unavailable "download of $asset failed from $BASE_URL/$PIN (network unavailable or release unreachable)"
  fi
done

# A missing checksums file on the download side is an availability failure
# (the manifest did not arrive), not a manifest-content error - a present
# but unparseable manifest stays fail-hard below.
if [ ! -f "$STAGING_DIR/$ASSET_CHECKSUMS" ]; then
  rtk_unavailable "checksums file missing after download: $STAGING_DIR/$ASSET_CHECKSUMS"
fi
expected_sha=$(awk -v n="$ASSET_TARBALL" '$2 == n { print $1 }' "$STAGING_DIR/$ASSET_CHECKSUMS")
if [ -z "$expected_sha" ]; then
  echo "rtk setup: no $ASSET_TARBALL entry in checksums.txt" >&2
  exit 1
fi
# The staged tarball can only be absent here if it was removed after
# download (or a partial write left nothing); classify as availability - a
# set -e exit would read as integrity corruption in the exit contract.
if [ ! -f "$STAGING_DIR/$ASSET_TARBALL" ]; then
  rtk_unavailable "staged tarball missing before verification: $STAGING_DIR/$ASSET_TARBALL"
fi
actual_sha=$(sha256sum "$STAGING_DIR/$ASSET_TARBALL" | awk '{ print $1 }')
if [ "$actual_sha" != "$expected_sha" ]; then
  # A hash mismatch on the download is a corrupted-transfer class: warn,
  # discard, exit 0. The exit trap discards the staged pair; any previously
  # installed binary was never touched (verify before extract).
  rtk_unavailable "sha256 mismatch for $ASSET_TARBALL (corrupted download; expected $expected_sha, got $actual_sha); discarded, previously installed binary unchanged"
fi

# Extract after verification; tolerate flat or nested upstream layouts
# (locate the single rtk binary inside the archive). -print -quit avoids a
# pipe - pipefail with SIGPIPE would abort under `find | head`.
EXTRACT_DIR=$(mktemp -d "$STAGING_DIR/extract.XXXXXX")
tar -xzf "$STAGING_DIR/$ASSET_TARBALL" -C "$EXTRACT_DIR"
rtk_bin=$(find "$EXTRACT_DIR" -type f -name rtk -print -quit)
if [ -z "$rtk_bin" ]; then
  echo "rtk setup: no rtk binary found inside $ASSET_TARBALL" >&2
  exit 1
fi

# PATH installation: container OS bin dir, never the framework venv. Refuse a
# pre-planted symlink/directory at the install path (the staging loop guards
# its targets the same way; cp follows symlinks and nesting inside a
# directory breaks the rollback rm -f).
if [ -L "$INSTALL_DIR/rtk" ] || [ -d "$INSTALL_DIR/rtk" ]; then
  echo "rtk setup: install target is a symlink or directory, refusing: $INSTALL_DIR/rtk" >&2
  exit 1
fi
# Snapshot any pre-existing binary so a failed install or self-check can
# restore it - a bare rollback would destroy the previous working binary.
RTK_PREEXISTING_BACKUP="$STAGING_DIR/rtk-preexisting.bak"
if [ -f "$INSTALL_DIR/rtk" ]; then
  cp "$INSTALL_DIR/rtk" "$RTK_PREEXISTING_BACKUP"
fi
# Classify install-surface failure (permission denied, disk full) as
# availability: it is an environment problem, not a corrupted download. A
# bare set -e exit would surface as exit 1, which the exit contract reserves
# for integrity corruption.
if ! mkdir -p "$INSTALL_DIR" || ! cp "$rtk_bin" "$INSTALL_DIR/rtk" || ! chmod 755 "$INSTALL_DIR/rtk"; then
  if [ -s "$RTK_PREEXISTING_BACKUP" ]; then
    cat "$RTK_PREEXISTING_BACKUP" > "$INSTALL_DIR/rtk" && chmod 755 "$INSTALL_DIR/rtk" || rm -f "$INSTALL_DIR/rtk"
  else
    rm -f "$INSTALL_DIR/rtk"
  fi
  rtk_unavailable "could not install rtk to $INSTALL_DIR (permission denied or disk full); agent runs uncompressed"
fi

# Self-check: resolve rtk through PATH, exactly as terminal commands will,
# and require the pinned version token in its output (exact-token match, no
# substring family - e.g. 1.0.1 must not match 1.0.10).
if ! rtk_version_matches; then
  # Fail-safe rollback: a wrong-version binary must not stay on PATH. If a
  # pre-existing binary was snapshotted, restore it - deleting it outright
  # would leave the host worse than before.
  if [ -f "$RTK_PREEXISTING_BACKUP" ]; then
    cp "$RTK_PREEXISTING_BACKUP" "$INSTALL_DIR/rtk" 2>/dev/null || rm -f "$INSTALL_DIR/rtk"
  else
    rm -f "$INSTALL_DIR/rtk"
  fi
  # A corrupted, non-executable, or wrong-version binary after install is
  # graceful degradation at setup time: warn and exit 0.
  rtk_unavailable "installed rtk failed the version self-check for $VERSION (corrupt or non-executable binary); rolled back, agent runs uncompressed"
fi

# Staging lifecycle cleanup is handled by the exit trap above - no orphan
# tarball, checksums, or extract dir remains on any exit path.
echo "rtk setup: verified and installed rtk $PIN to $INSTALL_DIR/rtk"
