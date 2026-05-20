from pathlib import Path

import pytest

from arh_mcp.tools import workflow


def _git(cwd: Path, *args: str) -> None:
    import subprocess

    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


class _DummyMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorator(func):
            self.tools[func.__name__] = func
            return func

        return decorator


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


@pytest.mark.asyncio
async def test_git_commit_defaults_to_local_commit_without_push(tmp_path, monkeypatch):
    async def clean_scan(*args, **kwargs):
        return {"blocked": False}

    monkeypatch.setattr(workflow, "_scan_staged_secrets", clean_scan)
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    _git(tmp_path, "remote", "add", "origin", "https://github.com/test-owner/test-repo.git")
    (tmp_path / "tracked.txt").write_text("base\n")

    result = await workflow._git_commit_and_push(tmp_path, "research: local")

    assert result["sha"]
    assert result["push_failed"] is False


@pytest.mark.asyncio
async def test_checkpoint_reports_local_commit_without_push(monkeypatch):
    async def fake_git_commit(*args, **kwargs):
        assert kwargs["push"] is False
        return {"sha": "a" * 40, "push_failed": False}

    posts = []

    async def fake_post(path, json):
        posts.append((path, json))
        if path.endswith("/logs"):
            return {"id": "log-1"}
        return {}

    dummy = _DummyMCP()
    workflow.register(dummy)
    monkeypatch.setattr(workflow, "_git_commit_and_push", fake_git_commit)
    monkeypatch.setattr(workflow.arh_client, "post", fake_post)

    result = await dummy.tools["checkpoint"](
        project_id="project-1",
        summary="local progress",
        cwd="/tmp",
    )

    assert result["status"] == "ok"
    assert result["commit_sha"] == "a" * 40
    assert [path for path, _ in posts] == [
        "/v1/research/projects/project-1/commits",
        "/v1/research/projects/project-1/logs",
    ]
    assert posts[0][1] == {"sha": "a" * 40, "message": "research: local progress"}
