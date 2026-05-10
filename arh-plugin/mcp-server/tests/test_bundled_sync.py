"""Guard that arh_client/_bundled/ stays byte-identical to arh-plugin/scripts/.

The bundled copies ship with the wheel so a uvx-from-git install can locate
the hook scripts without a checked-out plugin tree. The canonical sources
live at arh-plugin/scripts/. If the two diverge, hooks installed from a uvx
install run different code than hooks installed from the plugin — silent and
nasty. Run ``tools/sync_bundled.sh`` to resync.
"""

from __future__ import annotations

import filecmp
from pathlib import Path

import pytest

# tests/ → mcp-server/ → arh-plugin/
PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent
CANONICAL_DIR = PLUGIN_ROOT / "scripts"
BUNDLED_DIR = PLUGIN_ROOT / "mcp-server" / "client-src" / "arh_client" / "_bundled"

BUNDLED_FILES = ("hook-handler.py", "codex-hook-handler.py", "harness_common.py")


@pytest.mark.parametrize("filename", BUNDLED_FILES)
def test_bundled_matches_canonical(filename: str) -> None:
    canonical = CANONICAL_DIR / filename
    bundled = BUNDLED_DIR / filename
    assert canonical.is_file(), f"missing canonical source {canonical}"
    assert bundled.is_file(), f"missing bundled copy {bundled}"
    assert filecmp.cmp(canonical, bundled, shallow=False), (
        f"{filename} desynced between {canonical} and {bundled}; "
        f"run arh-plugin/mcp-server/client-src/tools/sync_bundled.sh"
    )
