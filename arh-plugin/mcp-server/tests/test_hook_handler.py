"""Regression tests for arh-plugin/scripts/hook-handler.py.

These tests confirm the PR #20 invariant: the per-session shadow ref
(``refs/heads/arh-auto/<safe_session_id>``) advances on PostToolUse for
file-mutating tools, and ``main`` is never touched. The hook is invoked
via ``subprocess`` against a real git repo built in ``tmp_path``.

Note: these are colocated with mcp-server tests because the plugin only
has one ``uv`` project (mcp-server) and one test runner. The script
under test lives at ``arh-plugin/scripts/hook-handler.py``.

The hook will fail to POST to a backend (no server) — that's fine. We
only assert on git side effects.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
HOOK_HANDLER = REPO_ROOT / "arh-plugin" / "scripts" / "hook-handler.py"


def _run_git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"git {args} failed in {cwd}:\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
    return result


def _scaffold_repo(tmp_path: Path) -> str:
    """Create a fresh git repo with one initial commit on ``main``.

    Returns the SHA of the initial commit.
    """
    _run_git(tmp_path, "init", "-q", "-b", "main")
    _run_git(tmp_path, "config", "user.email", "test@example.invalid")
    _run_git(tmp_path, "config", "user.name", "Test")
    readme = tmp_path / "README.md"
    readme.write_text("initial\n")
    _run_git(tmp_path, "add", "README.md")
    _run_git(tmp_path, "commit", "-q", "-m", "initial")
    rev = _run_git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    return rev


def _seed_arh_settings(tmp_path: Path) -> None:
    """Pre-seed .arh/settings.json so hook-handler doesn't early-exit on missing project_id."""
    arh_dir = tmp_path / ".arh"
    arh_dir.mkdir()
    (arh_dir / "settings.json").write_text(
        json.dumps(
            {
                "project_id": "00000000-0000-0000-0000-000000000000",
                # Keep auto_commit off so the legacy auto-commit path doesn't
                # write to main during these tests.
                "auto_commit": False,
            }
        )
    )


def _invoke_hook(
    tmp_path: Path,
    event_type: str,
    event_payload: dict,
) -> subprocess.CompletedProcess:
    """Run hook-handler.py via subprocess, feeding event JSON on stdin.

    The hook will likely fail to POST to a backend; that's expected. We
    suppress its stderr/stdout but don't assert on the return code beyond
    "the process exited normally" (rc == 0 since hook-handler always
    exits cleanly to avoid blocking Claude Code).
    """
    env = os.environ.copy()
    # Don't let any real backend creds leak into the test.
    env.pop("ARH_API_KEY", None)
    env.pop("ARH_API_URL", None)
    env.pop("ARH_TRACE_ID", None)
    # Set a fake API key so the hook actually attempts the (doomed) POST,
    # which is the path that exercises auto-checkpoint side effects.
    env["ARH_API_KEY"] = "arh_sk_test"
    env["ARH_API_URL"] = "http://localhost:0"

    return subprocess.run(
        [sys.executable, str(HOOK_HANDLER), event_type],
        cwd=tmp_path,
        input=json.dumps(event_payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def test_shadow_ref_advances_on_post_tool_use(tmp_path: Path) -> None:
    """PostToolUse for an Edit creates a shadow ref one commit beyond main."""
    initial_sha = _scaffold_repo(tmp_path)
    _seed_arh_settings(tmp_path)

    # Modify a tracked file so auto_checkpoint sees changes.
    readme = tmp_path / "README.md"
    readme.write_text("initial\n\nedited line\n")

    session_id = "test-sess-1"
    payload = {
        "session_id": session_id,
        "cwd": str(tmp_path),
        "tool_name": "Edit",
        "tool_input": {"file_path": str(readme)},
        "tool_response": "",
    }

    _invoke_hook(tmp_path, "PostToolUse", payload)

    # The shadow ref should now exist with the same safe-token transformation
    # hook-handler uses (alphanum/_- preserved, others → '-').
    shadow_ref = f"refs/heads/arh-auto/{session_id}"

    rc = _run_git(tmp_path, "rev-parse", "--verify", "--quiet", shadow_ref, check=False)
    assert rc.returncode == 0, (
        f"shadow ref {shadow_ref} not created. git refs:\n"
        f"{_run_git(tmp_path, 'for-each-ref', check=False).stdout}"
    )
    shadow_sha = rc.stdout.strip()

    # Shadow HEAD should differ from main.
    main_sha = _run_git(tmp_path, "rev-parse", "main").stdout.strip()
    assert shadow_sha != main_sha, "shadow ref must advance past main after Edit"
    assert main_sha == initial_sha, "main must not move during PostToolUse"

    # git log of the shadow ref shows exactly one commit beyond initial.
    log = (
        _run_git(tmp_path, "log", "--format=%H", shadow_ref).stdout.strip().splitlines()
    )
    assert len(log) == 2, (
        f"expected 2 commits on shadow ref (initial + auto), got: {log}"
    )
    assert log[-1] == initial_sha, (
        f"shadow ref's root commit should match initial; got log={log}, "
        f"initial={initial_sha}"
    )


def test_manual_checkpoint_tool_skipped(tmp_path: Path) -> None:
    """When the tool is the manual-checkpoint MCP tool, no shadow ref is created."""
    _scaffold_repo(tmp_path)
    _seed_arh_settings(tmp_path)

    # Working-tree change: present but should be ignored.
    (tmp_path / "README.md").write_text("initial\n\nedited line\n")

    session_id = "test-sess-2"
    payload = {
        "session_id": session_id,
        "cwd": str(tmp_path),
        "tool_name": "mcp__plugin_arh_ai-researcher-hub__checkpoint",
        "tool_input": {},
        "tool_response": "",
    }

    _invoke_hook(tmp_path, "PostToolUse", payload)

    shadow_ref = f"refs/heads/arh-auto/{session_id}"
    rc = _run_git(tmp_path, "rev-parse", "--verify", "--quiet", shadow_ref, check=False)
    assert rc.returncode != 0, (
        f"shadow ref {shadow_ref} should NOT exist for manual checkpoint tool, "
        f"but found {rc.stdout.strip()}"
    )


def test_load_arh_env_ignores_legacy_api_key_in_dotenv(tmp_path: Path) -> None:
    """Single source of truth fix (2026-04): hook-handler must NOT honor an
    ``ARH_API_KEY`` entry in project-local ``.arh/.env``. The key comes only
    from ``~/.arh/credentials``; ``.arh/.env`` carries project context only.
    """
    # Build a project dir with a legacy .arh/.env containing a dead API key
    # AND a project_id (legitimate).
    arh_dir = tmp_path / ".arh"
    arh_dir.mkdir()
    (arh_dir / ".env").write_text(
        "ARH_API_KEY=arh_sk_dead_legacy\n"
        "ARH_API_URL=https://project-local.example.test\n"
        "ARH_PROJECT_ID=33333333-3333-3333-3333-333333333333\n"
    )

    # Run a tiny inline script that imports hook-handler.py's load_arh_env
    # via importlib (the script isn't a normal module, so importlib.util is
    # the cleanest path) and prints the resolved env.
    runner = (
        "import importlib.util, json, os, sys\n"
        f"sys.path.insert(0, os.path.dirname(r'{HOOK_HANDLER}'))\n"
        f"spec = importlib.util.spec_from_file_location('hh', r'{HOOK_HANDLER}')\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        # Pretend the user has a fresh, valid key in ~/.arh/credentials. We
        # can't write to the real ~/.arh/credentials in a test, so seed a
        # temporary HOME and a fake credentials file there.
        "mod.load_arh_env()\n"
        "print(json.dumps({'ARH_API_KEY': os.environ.get('ARH_API_KEY', ''),"
        "                   'ARH_API_URL': os.environ.get('ARH_API_URL', ''),"
        "                   'ARH_PROJECT_ID': os.environ.get('ARH_PROJECT_ID', '')}))\n"
    )

    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    (fake_home / ".arh").mkdir()
    (fake_home / ".arh" / "credentials").write_text(
        json.dumps(
            {
                "api_key": "arh_sk_fresh_from_creds",
                "api_url": "https://stored.example.test",
            }
        )
    )

    env = os.environ.copy()
    # Strip any real ARH_API_KEY from the test process so we observe only
    # what load_arh_env resolves.
    env.pop("ARH_API_KEY", None)
    env.pop("ARH_PROJECT_ID", None)
    env["HOME"] = str(fake_home)

    result = subprocess.run(
        [sys.executable, "-c", runner],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert result.returncode == 0, (
        f"load_arh_env runner failed: stdout={result.stdout} stderr={result.stderr}"
    )

    # The runner prints a JSON object; the legacy stderr-warn line may be
    # interleaved on stderr, not stdout, so we parse the last non-empty stdout
    # line.
    out_lines = [line for line in result.stdout.strip().splitlines() if line]
    parsed = json.loads(out_lines[-1])
    assert parsed["ARH_API_KEY"] == "arh_sk_fresh_from_creds", (
        f"hook-handler must resolve ARH_API_KEY from ~/.arh/credentials, "
        f"NOT from .arh/.env. Got: {parsed}"
    )
    assert parsed["ARH_API_URL"] == "https://stored.example.test", (
        f"hook-handler must keep stored credentials bound to their stored URL. Got: {parsed}"
    )
    assert parsed["ARH_PROJECT_ID"] == "33333333-3333-3333-3333-333333333333", (
        f"ARH_PROJECT_ID must come from .arh/.env. Got: {parsed}"
    )

    # Stderr should carry a deprecation warning since .arh/.env had ARH_API_KEY.
    assert "ARH_API_KEY" in result.stderr and "legacy" in result.stderr.lower(), (
        f"expected deprecation warning on stderr; got: {result.stderr!r}"
    )


def test_load_arh_env_key_only_credentials_use_default_url_not_ambient_url(
    tmp_path: Path,
) -> None:
    arh_dir = tmp_path / ".arh"
    arh_dir.mkdir()
    (arh_dir / ".env").write_text("ARH_API_URL=https://project-local.example.test\n")

    runner = (
        "import importlib.util, json, os, sys\n"
        f"sys.path.insert(0, os.path.dirname(r'{HOOK_HANDLER}'))\n"
        f"spec = importlib.util.spec_from_file_location('hh', r'{HOOK_HANDLER}')\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "mod.load_arh_env()\n"
        "print(json.dumps({'ARH_API_KEY': os.environ.get('ARH_API_KEY', ''),"
        "                   'ARH_API_URL': os.environ.get('ARH_API_URL', '')}))\n"
    )

    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    (fake_home / ".arh").mkdir()
    (fake_home / ".arh" / "credentials").write_text(
        json.dumps({"api_key": "arh_sk_key_only"})
    )
    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    env["ARH_API_URL"] = "https://ambient.example.test"
    env.pop("ARH_API_KEY", None)

    result = subprocess.run(
        [sys.executable, "-c", runner],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout.strip().splitlines()[-1])
    assert parsed["ARH_API_KEY"] == "arh_sk_key_only"
    assert parsed["ARH_API_URL"] == "https://api.airesearcherhub.com"


def test_load_arh_env_drops_ambient_project_context_without_local_setup(
    tmp_path: Path,
) -> None:
    """Community mode can run from an untracked directory. A stale shell-level
    ARH_PROJECT_ID must not make the Claude hook report Stop events against an
    unrelated tracking project.
    """
    runner = (
        "import importlib.util, json, os, sys\n"
        f"sys.path.insert(0, os.path.dirname(r'{HOOK_HANDLER}'))\n"
        f"spec = importlib.util.spec_from_file_location('hh', r'{HOOK_HANDLER}')\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "mod.load_arh_env()\n"
        "print(json.dumps({'ARH_PROJECT_ID': os.environ.get('ARH_PROJECT_ID', ''),"
        "                   'ARH_TRACE_ID': os.environ.get('ARH_TRACE_ID', '')}))\n"
    )

    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    (fake_home / ".arh").mkdir()
    (fake_home / ".arh" / "credentials").write_text(
        json.dumps({"api_key": "arh_sk_fresh"})
    )
    env = os.environ.copy()
    env["HOME"] = str(fake_home)
    env["ARH_PROJECT_ID"] = "11111111-1111-1111-1111-111111111111"
    env["ARH_TRACE_ID"] = "22222222-2222-2222-2222-222222222222"

    result = subprocess.run(
        [sys.executable, "-c", runner],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )

    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout.strip().splitlines()[-1])
    assert parsed == {"ARH_PROJECT_ID": "", "ARH_TRACE_ID": ""}


@pytest.fixture(autouse=True)
def _hook_handler_must_exist():
    """Sanity check — fail loudly if the hook-handler script is missing."""
    assert HOOK_HANDLER.is_file(), f"hook-handler.py not found at {HOOK_HANDLER}"
