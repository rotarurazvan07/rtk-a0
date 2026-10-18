# RTK Compression — Agent Zero Plugin

Thin fail-open adapter that routes Agent Zero terminal commands through the
[RTK](https://github.com/rtk-ai/rtk) CLI for output compression. Every terminal
command is delegated to `rtk rewrite` before execution; if RTK is unavailable or
fails, your command runs unchanged. The plugin downloads and invokes the RTK
binary — it never vendors or reimplements it.

## Install

**Plugin Hub** — search for **rtk** in Agent Zero's Plugin Hub and install.
The plugin is default-on (no settings to toggle).

**Git clone** — clone the plugin into Agent Zero's custom-plugin directory:

```bash
git clone https://github.com/rotarurazvan07/rtk-a0.git usr/plugins/rtk_a0
```

**Restart Agent Zero** after either path so the extension loads.

### What setup does automatically

On install (and on plugin update), `scripts/setup.sh` provisions the RTK binary:

- Downloads the release pinned in `rtk_pin.txt` (currently `v1.0.1`, the x86_64
  Linux musl static build) from the RTK project's GitHub releases.
- Verifies the download's SHA-256 against the release `checksums.txt`, then
  installs it to `/usr/local/bin/rtk`. Skips automatically when the pinned
  version is already installed.
- **Warn-not-fail:** if the download or verification fails (offline container,
  network block, checksum mismatch), setup logs one warning and exits cleanly.
  The plugin keeps working — commands simply run uncompressed until the binary
  is available.

## Verify compression is active

Run this through the agent in any session:

```
rtk gain
```

RTK maintains its own savings ledger; the plugin surfaces its output unchanged.
Read **Tokens saved** and the per-command table to see cumulative and
per-command reductions.

Measured examples on this container (not guarantees):

| Command | Before | After | Reduction |
| --- | --- | --- | --- |
| `git status` | 476 B | 125 B | 73.7% |
| `ls -la` | — | — | 70.6% |

Savings are command-dependent: output-heavy commands compress well, while tiny
outputs can come back **larger** (a plain `ls` in a small directory measured
78 B → 146 B in testing). There is no guaranteed flat percentage.

## Usage

The plugin is **default-on** (manifest `always_enabled: true`) and needs no
configuration. In any session:

- Terminal commands are rewritten by RTK before execution — compressed output
  reaches the agent context when RTK has a rewrite, and the original command
  runs when RTK returns no change.
- Commands you prefix with `rtk` yourself are passed through untouched
  (`rtk gain`, `rtk --version`, ...), so you can always talk to RTK directly.
- Rewritten commands still go through Agent Zero's normal permission layer —
  the plugin never bypasses approvals.

### Escape hatch — raw output on demand

**`rtk bypass <command>`** returns the command's raw, unrewritten output. Use it
whenever you want exactly what the command prints, with no compression in the
way:

```
rtk bypass ls -la
```

### Fail-open guarantee

The plugin is designed to trust by default: no failure mode can strand your
command. RTK missing from `PATH` (one warning per session), rewrite timeout
(2 s), non-zero rewrite exit — except RTK's rc 3 allow-rules notice, which
counts as a rewrite and is applied — empty or identical rewrite output, and
any internal error all result in the **original command running
unchanged**. The command always executes — worst case, uncompressed.

## Troubleshooting

**"rtk unavailable — running uncompressed" warning appears once per
session.** The RTK binary is not on `PATH`. The agent still works — commands
run raw and uncompressed.

Remedy:

1. Re-run setup from the plugin directory (it is idempotent and skips work that
   is already done):

   ```bash
   RTK_INSTALL_DIR=/usr/local/bin bash scripts/setup.sh
   ```

2. Verify the binary is present:

   ```bash
   rtk --version
   ```

3. Restart the agent session and run `rtk gain` again.

**RTK rewrites carry an allow-rules notice on some hosts.** On default
containers RTK exits with rc 3 and defers command approval to the host —
Agent Zero's permission layer applies to the rewritten command exactly as it
does to the original, and the compressed output is valid either way.

**Compression seems not to fire.** The pinned binary is Linux-only. On macOS or
Windows the binary cannot be provisioned — the plugin warns and the agent keeps
working uncompressed.

## Uninstall

Uninstalling the plugin removes the plugin files only. The RTK binary at
`/usr/local/bin/rtk` is treated as user-owned and left in place — remove it
yourself if you want it gone. On plugin update, setup re-provisions the binary
if the installed version has drifted from the pin in `rtk_pin.txt`.

## License

This plugin is licensed **Apache-2.0** (see `LICENSE`). RTK itself is an
**external BUSL-1.1 dependency**, used **orchestration-only**: the plugin
downloads the RTK binary at setup time and invokes it as
`rtk rewrite <command>` — it never vendors, embeds, or reimplements RTK code. Support scope: **Linux containers only** - the
pinned release is the x86_64 musl static Linux build. Compliance boundary: the
RTK engine is downloaded as a pinned, single-source binary (recorded in
`rtk_pin.txt`) and used for orchestration only; no RTK code or RTK-owned
savings accounting is embedded in this repository.

## Links

- RTK project: https://github.com/rtk-ai/rtk
- RTK documentation: https://www.rtk-ai.app/docs/
- This plugin: https://github.com/rotarurazvan07/rtk-a0
