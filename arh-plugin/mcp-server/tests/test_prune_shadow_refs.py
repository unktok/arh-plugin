"""Tests for the prune_shadow_refs MCP tool.

Builds a real git repo in tmp_path, manufactures shadow refs at
``refs/heads/arh-auto/<session_id>`` with controlled committer dates via
``GIT_COMMITTER_DATE``, then exercises the tool's age-threshold and
dry-run behavior.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from arh_mcp.tools import tracing


def _load_prune(mcp_register):
    tools = mcp_register(tracing.register)
    assert "prune_shadow_refs" in tools, (
        f"prune_shadow_refs not registered; got: {list(tools)}"
    )
    return tools["prune_shadow_refs"]


def _git(cwd: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=15,
        env={**os.environ, **(env or {})},
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {args} failed: {proc.stderr}")
    return proc


def _make_shadow_ref(repo: Path, session: str, days_ago: int) -> str:
    """Create a shadow ref with a synthetic commit dated `days_ago` days back.

    Returns the ref name.
    """
    # Stage a synthetic file change so the commit isn't empty.
    fname = repo / f"shadow_{session}.txt"
    fname.write_text(f"shadow {session} content\n")

    # Use isolated index so we don't disturb main's index.
    index = repo / f".git/index-{session}"
    env = {
        "GIT_INDEX_FILE": str(index),
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    # Read main's tree into the isolated index, then add the new file.
    _git(repo, "read-tree", "main", env=env)
    _git(repo, "add", "-A", env=env)
    tree_sha = _git(repo, "write-tree", env=env).stdout.strip()

    # Backdate the commit by setting committer date.
    backdate_ts = int(time.time()) - (days_ago * 24 * 3600)
    backdate = f"{backdate_ts} +0000"
    main_sha = _git(repo, "rev-parse", "main").stdout.strip()
    commit_env = {
        **env,
        "GIT_AUTHOR_DATE": backdate,
        "GIT_COMMITTER_DATE": backdate,
    }
    commit_sha = _git(
        repo,
        "commit-tree",
        tree_sha,
        "-p",
        main_sha,
        "-m",
        f"shadow {session} ({days_ago}d ago)",
        env=commit_env,
    ).stdout.strip()

    ref = f"refs/heads/arh-auto/{session}"
    _git(repo, "update-ref", ref, commit_sha)

    if index.exists():
        index.unlink()
    if fname.exists():
        fname.unlink()
    return ref


def _scaffold_repo(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "README.md").write_text("initial\n")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-q", "-m", "initial")


def _list_shadow_refs(repo: Path) -> list[str]:
    proc = _git(repo, "for-each-ref", "--format=%(refname)", "refs/heads/arh-auto/")
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_prune_deletes_old_refs_keeps_recent(
    tmp_path, fake_arh_client, mcp_register
):
    """Refs older than the threshold are deleted; recent refs survive."""
    _scaffold_repo(tmp_path)
    _make_shadow_ref(tmp_path, "old-session-1", days_ago=30)
    _make_shadow_ref(tmp_path, "old-session-2", days_ago=20)
    _make_shadow_ref(tmp_path, "fresh-session", days_ago=3)

    prune = _load_prune(mcp_register)
    result = await prune(repo_dir=str(tmp_path), older_than_days=14)

    refs = _list_shadow_refs(tmp_path)
    assert "refs/heads/arh-auto/fresh-session" in refs, (
        f"fresh ref deleted incorrectly: {refs}"
    )
    assert "refs/heads/arh-auto/old-session-1" not in refs, (
        f"old ref not pruned: {refs}"
    )
    assert "refs/heads/arh-auto/old-session-2" not in refs, (
        f"old ref not pruned: {refs}"
    )
    assert "pruned 2" in result, f"summary should report 2 pruned: {result!r}"


@pytest.mark.asyncio
async def test_prune_dry_run_keeps_everything(tmp_path, fake_arh_client, mcp_register):
    """dry_run=True reports candidates but does not delete."""
    _scaffold_repo(tmp_path)
    _make_shadow_ref(tmp_path, "ancient", days_ago=60)
    _make_shadow_ref(tmp_path, "older", days_ago=30)

    prune = _load_prune(mcp_register)
    result = await prune(repo_dir=str(tmp_path), older_than_days=14, dry_run=True)

    refs = _list_shadow_refs(tmp_path)
    assert "refs/heads/arh-auto/ancient" in refs, "dry_run must not delete"
    assert "refs/heads/arh-auto/older" in refs, "dry_run must not delete"
    assert "would prune 2" in result, f"summary mismatch: {result!r}"


@pytest.mark.asyncio
async def test_prune_no_candidates(tmp_path, fake_arh_client, mcp_register):
    """Empty repo (no shadow refs) reports 'no refs found' cleanly."""
    _scaffold_repo(tmp_path)

    prune = _load_prune(mcp_register)
    result = await prune(repo_dir=str(tmp_path), older_than_days=14)

    assert "No shadow refs older than" in result, f"unexpected: {result!r}"


@pytest.mark.asyncio
async def test_prune_rejects_non_git_dir(tmp_path, fake_arh_client, mcp_register):
    """Non-git directory yields a clear error."""
    prune = _load_prune(mcp_register)
    result = await prune(repo_dir=str(tmp_path), older_than_days=14)

    assert "Error" in result and "not a git repository" in result, (
        f"unexpected: {result!r}"
    )


@pytest.mark.asyncio
async def test_prune_rejects_invalid_threshold(tmp_path, fake_arh_client, mcp_register):
    """older_than_days < 1 is rejected."""
    _scaffold_repo(tmp_path)
    prune = _load_prune(mcp_register)
    result = await prune(repo_dir=str(tmp_path), older_than_days=0)

    assert "Error" in result and "older_than_days" in result, f"unexpected: {result!r}"
