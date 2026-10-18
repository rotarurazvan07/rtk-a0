# Licensing Boundary & Compliance Note

## The boundary, in one paragraph

The rtk_a0 plugin is **orchestration-only**. It ships a thin adapter (a named
A0 extension), a pin file, a setup script, and this documentation. **All
compression intelligence lives in the RTK Rust binary**, which is downloaded
at install time and executed as an external process. RTK is licensed
**BUSL-1.1**; the rtk_a0 plugin makes it available to Agent Zero users
**not vendored** — it is downloaded to `/usr/local/bin/rtk` by
`scripts/setup.sh`, pinned by `rtk_pin.txt`, and invoked only as
`rtk rewrite <command>`. The plugin's own code is licensed
**Apache-2.0** (root `LICENSE`), so the repository and the Plugin Hub artifact
convey an *installer*, never the engine.

## Where the boundary lives

| Concern | Owner | Artifact |
| --- | --- | --- |
| Compression / rewrite logic | RTK binary (BUSL-1.1) | downloaded, never stored in this repo |
| Which RTK version | this repo | `rtk_pin.txt` (single source) |
| Download + verify + install | this repo | `scripts/setup.sh` |
| Lifecycle wiring (install/update/uninstall) | this repo | `hooks.py` |
| Command interception + delegation | this repo | `extensions/python/tool_execute_before/_10_rtk_intercept.py` |
| License of everything above (ours) | this repo | `LICENSE` (Apache-2.0) |

The plugin **never** reimplements filter logic, embeds RTK source, or ships a
precompiled binary. Uninstall removes plugin files only; the downloaded RTK
binary is user-owned and left in place.

## Framework boundary

The plugin integrates through the official named extension point
tool_execute_before only: no Agent Zero framework file is modified, no
framework module is monkeypatched, and no file outside the plugin directory is
written except the user-owned RTK binary install target.

## Enforcement (this story's CI)

- `tests/test_licensing_boundary.py` — structural scans: no RTK/Hermes-derived
tokens in plugin code (comments/docstrings stripped + structural import-node
audit; same needle set over shell/manifest text sources with comment lines
stripped), no vendored binaries, no reimplemented filter tables (dicts
with ≥4 command-shaped keys are flagged as reimplementation), every
process-spawning call (`subprocess.run/call/check_call/check_output/Popen`
+ `os.system`) delegates to the `rtk` binary or the plugin's own
`scripts/setup.sh`, official-registration-only, no framework files or
framework directory names at any depth in-tree, Apache-2.0 LICENSE, and the
plugin git toplevel is the plugin root.
- `.github/ast-rules/` + `.github/sgconfig.yml` — ast-grep CI rules rejecting
`shell=True` and hardcoded `vX.Y.Z` literals outside `rtk_pin.txt`
(subprocess invocation without a shell, plus the single-source pin check).

Run locally:

```bash
pytest tests/test_licensing_boundary.py tests/test_ci_enforcement.py
ast-grep scan --config .github/sgconfig.yml scripts/ hooks.py plugin.yaml extensions/
```
