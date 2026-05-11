# `_bundled/`

Copies of plugin scripts shipped with `arh-client` so the CLI's hook installer
can run without a full plugin checkout (e.g. from a `uvx --refresh --from git+...`
invocation).

The canonical sources live in `arh-plugin/scripts/`. Keep the two trees in
sync with `tools/sync_bundled.sh` (or `cp` manually) and update both when
modifying hook handler logic.

Files:
- `hook-handler.py` — Claude Code hook handler
- `codex-hook-handler.py` — Codex hook handler
- `harness_common.py` — shared utilities, imported by both handlers
