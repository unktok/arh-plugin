from pathlib import Path

import pytest

from arh_mcp.tools import workflow


def _git(cwd: Path, *args: str) -> None:
    import subprocess

    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_git_commit_and_push_blocks_when_gitleaks_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(workflow, "_gitleaks_path", lambda: None)
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    (tmp_path / "tracked.txt").write_text("base\n")
    _git(tmp_path, "add", "tracked.txt")
    _git(tmp_path, "commit", "-m", "initial")
    (tmp_path / "tracked.txt").write_text("changed\n")

    result = await workflow._git_commit_and_push(tmp_path, "research: blocked")

    assert result["reason"] == "secret_scan_failed"
    assert "gitleaks is required" in result["error"]
