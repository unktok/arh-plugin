#!/usr/bin/env bash
# Copy canonical plugin scripts from arh-plugin/scripts/ into the
# arh_client._bundled package directory, keeping the uvx-installable CLI in
# sync with the plugin's hook handlers.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
src="$here/../../../scripts"
dst="$here/../arh_client/_bundled"
for f in hook-handler.py codex-hook-handler.py harness_common.py; do
  cp "$src/$f" "$dst/$f"
  echo "synced $dst/$f"
done
