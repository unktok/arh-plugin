"""Guard that arh_client._workspace.WORKFLOW_RULES_MARKDOWN matches the
heredoc the init-research skill writes when running on Claude Code.

The CLI path (`arh track-research --runtime codex` / `--runtime claude_code`)
materializes `.arh/ARH.md` from this Python constant. The skill path
(`/arh:track-research`) writes `.arh/ARH.md` from a markdown heredoc embedded
in `arh-plugin/skills/init-research/SKILL.md` Step 5.5.2. Both surfaces must
emit the same content; otherwise an agent's behavior depends on which
install path the user chose. Run drift fix manually: edit one, copy to the
other.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
SKILL_PATH = REPO_ROOT / "arh-plugin" / "skills" / "init-research" / "SKILL.md"


def _extract_arh_md_heredoc() -> str:
    """Return the markdown content the skill instructs the agent to write to
    `.arh/ARH.md`. Located between the first ```markdown fence and the next
    ``` fence after Step 5.5.2.
    """
    text = SKILL_PATH.read_text(encoding="utf-8")
    section_start = text.find("### 5.5.2:")
    assert section_start >= 0, "Step 5.5.2 anchor missing from SKILL.md"
    after_anchor = text[section_start:]
    match = re.search(r"```markdown\n(.*?)\n```", after_anchor, flags=re.DOTALL)
    assert match, "no ```markdown fence found after Step 5.5.2"
    # Skill heredoc has no trailing newline before the closing fence; the
    # Python constant uses a triple-quoted string ending in '\n', so add one
    # for parity.
    return match.group(1) + "\n"


def test_workspace_markdown_matches_skill_heredoc() -> None:
    from arh_client._workspace import WORKFLOW_RULES_MARKDOWN

    expected = _extract_arh_md_heredoc()
    if WORKFLOW_RULES_MARKDOWN != expected:
        # Find the first divergent line so the failure message is actionable.
        a_lines = WORKFLOW_RULES_MARKDOWN.splitlines()
        b_lines = expected.splitlines()
        for idx, (a, b) in enumerate(zip(a_lines, b_lines)):
            if a != b:
                pytest.fail(
                    f"WORKFLOW_RULES_MARKDOWN diverges from SKILL.md heredoc at line {idx + 1}.\n"
                    f"  python:  {a!r}\n"
                    f"  skill:   {b!r}\n"
                    f"Edit one, copy to the other."
                )
        if len(a_lines) != len(b_lines):
            pytest.fail(
                f"WORKFLOW_RULES_MARKDOWN has {len(a_lines)} lines but SKILL.md heredoc has {len(b_lines)} lines."
            )
