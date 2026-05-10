from __future__ import annotations

from pathlib import Path

from arh_client._workspace import initialize_research_workspace


def test_initialize_workspace_writes_agents_md_and_codex_gitignore(tmp_path: Path):
    actions = initialize_research_workspace(str(tmp_path))

    agents_md = (tmp_path / "AGENTS.md").read_text()
    gitignore = (tmp_path / ".gitignore").read_text()

    assert actions["agents_md"] is True
    assert "## AI Researcher Hub" in agents_md
    assert ".codex/hooks.json" in gitignore
    assert ".codex/config.toml" in gitignore


def test_initialize_workspace_preserves_existing_agents_md(tmp_path: Path):
    agents_path = tmp_path / "AGENTS.md"
    agents_path.write_text("# Existing\n")

    actions = initialize_research_workspace(str(tmp_path))
    actions_again = initialize_research_workspace(str(tmp_path))

    assert actions["agents_md"] is True
    assert actions_again["agents_md"] is False
    text = agents_path.read_text()
    assert text.count("## AI Researcher Hub") == 1
    assert text.startswith("# Existing")
