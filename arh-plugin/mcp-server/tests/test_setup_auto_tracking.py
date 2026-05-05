"""Regression tests for setup_auto_tracking.

These tests cover the fix in PR #23 / commit 0842dd9, which stopped
``setup_auto_tracking`` from double-installing Claude Code hooks when the
ARH plugin path was already routing events to ``hook-handler.py`` via
``hooks/hooks.json``.

The plugin layout under test is the real one in this repo, so the
auto-detection branch (``plugin_active = True``) is exercised against the
actual ``.claude-plugin/plugin.json`` and ``hooks/hooks.json`` files. The
``install_claude_hooks`` override is exercised separately to confirm the
legacy behavior still works.
"""

from __future__ import annotations

import json
import os

import pytest

from arh_mcp.tools import tracing


def _load_setup_auto_tracking(mcp_register):
    """Register the tracing module's tools and return setup_auto_tracking."""
    tools = mcp_register(tracing.register)
    assert "setup_auto_tracking" in tools, (
        f"setup_auto_tracking not registered; got: {list(tools)}"
    )
    return tools["setup_auto_tracking"]


@pytest.mark.asyncio
async def test_skips_install_when_plugin_active(
    tmp_path, fake_arh_client, mcp_register
):
    """When the plugin manifest + hooks.json exist, .claude/settings.json is not created."""
    setup_auto_tracking = _load_setup_auto_tracking(mcp_register)

    project_dir = tmp_path
    result = await setup_auto_tracking(project_dir=str(project_dir), project_id=None)

    settings_path = project_dir / ".claude" / "settings.json"
    assert not settings_path.exists(), (
        "Plugin path is active, so .claude/settings.json should not be written. "
        f"Found content: {settings_path.read_text() if settings_path.exists() else None}"
    )
    # Sanity: the tool should still report success.
    assert "Auto-tracking configured" in result


@pytest.mark.asyncio
async def test_strips_stale_arh_entries(tmp_path, fake_arh_client, mcp_register):
    """Pre-existing ARH hook entries get cleaned; unrelated entries survive."""
    setup_auto_tracking = _load_setup_auto_tracking(mcp_register)

    # Mirror round-4's polluted state: ARH entries already live in
    # .claude/settings.json AND there is an unrelated integrity-check entry
    # under a different matcher that must NOT be stripped.
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings_path = claude_dir / "settings.json"
    polluted = {
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "python3 /old/path/hook-handler.py PostToolUse",
                        }
                    ],
                },
                {
                    "matcher": "Edit|Write",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "bash .claude/hooks/integrity-check.sh",
                        }
                    ],
                },
            ],
            "Stop": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "python3 /old/path/hook-handler.py Stop",
                        }
                    ],
                }
            ],
            "SessionStart": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": "bash /old/path/inject-trace-context.sh",
                        },
                        {
                            "type": "command",
                            "command": "python3 /old/path/hook-handler.py SessionStart",
                        },
                    ],
                }
            ],
        }
    }
    settings_path.write_text(json.dumps(polluted, indent=2))

    await setup_auto_tracking(project_dir=str(tmp_path), project_id=None)

    cleaned = json.loads(settings_path.read_text())
    hooks = cleaned.get("hooks", {})

    # Stale ARH PostToolUse entry removed; integrity-check entry preserved.
    post_tool_use = hooks.get("PostToolUse", [])
    assert len(post_tool_use) == 1, (
        f"Expected 1 PostToolUse entry (integrity-check), got: {post_tool_use}"
    )
    surviving = post_tool_use[0]
    assert surviving.get("matcher") == "Edit|Write"
    assert any(
        "integrity-check.sh" in h.get("command", "") for h in surviving.get("hooks", [])
    ), f"integrity-check entry not preserved: {surviving}"

    # Stop event had only an ARH entry; it should now be empty / absent.
    assert "Stop" not in hooks or hooks["Stop"] == [], (
        f"Stop should have been removed (only contained ARH entries): {hooks.get('Stop')}"
    )

    # SessionStart had inject-trace + hook-handler — both ARH-managed and
    # should be stripped together.
    assert "SessionStart" not in hooks or hooks["SessionStart"] == [], (
        f"SessionStart entries should be stripped: {hooks.get('SessionStart')}"
    )


@pytest.mark.asyncio
async def test_force_install_via_override(tmp_path, fake_arh_client, mcp_register):
    """install_claude_hooks=True forces hook entries to be written even when plugin is active."""
    setup_auto_tracking = _load_setup_auto_tracking(mcp_register)

    await setup_auto_tracking(
        project_dir=str(tmp_path),
        project_id=None,
        install_claude_hooks=True,
    )

    settings_path = tmp_path / ".claude" / "settings.json"
    assert settings_path.exists(), (
        "install_claude_hooks=True must write .claude/settings.json"
    )
    settings = json.loads(settings_path.read_text())
    hooks = settings.get("hooks", {})

    expected_events = {
        "SessionStart",
        "PostToolUse",
        "Stop",
        "SubagentStop",
        "Notification",
        "TaskCompleted",
    }
    assert expected_events.issubset(hooks.keys()), (
        f"Missing events; got {sorted(hooks)}"
    )

    # Each event has exactly one entry that points at hook-handler.py.
    for event in expected_events:
        entries = hooks[event]
        assert len(entries) == 1, f"{event}: expected 1 entry, got {entries}"
        commands = [h.get("command", "") for h in entries[0].get("hooks", [])]
        assert any("hook-handler.py" in c for c in commands), (
            f"{event} entry missing hook-handler.py: {commands}"
        )


@pytest.mark.asyncio
async def test_arh_env_does_not_persist_api_key(
    tmp_path, fake_arh_client, mcp_register
):
    """Single source of truth (2026-04 fix): the API key is never persisted
    to project-local ``.arh/.env``. Only project / trace context goes there;
    the hook handler reads ``~/.arh/credentials`` for the key.
    """
    setup_auto_tracking = _load_setup_auto_tracking(mcp_register)

    await setup_auto_tracking(
        project_dir=str(tmp_path),
        project_id="00000000-0000-0000-0000-000000000001",
    )

    env_path = tmp_path / ".arh" / ".env"
    # If api_url is the default and project_id is set, the file should exist
    # with only ARH_PROJECT_ID. Otherwise it may be empty but should still not
    # carry ARH_API_KEY.
    if env_path.exists():
        content = env_path.read_text()
        assert "ARH_API_KEY" not in content, (
            f"ARH_API_KEY must not be persisted to .arh/.env after the "
            f"single-source-of-truth fix:\n{content}"
        )
        # project_id should be there since we passed one.
        assert "ARH_PROJECT_ID=00000000-0000-0000-0000-000000000001" in content


@pytest.mark.asyncio
async def test_arh_env_strips_legacy_api_key_entry(
    tmp_path, fake_arh_client, mcp_register
):
    """A pre-existing ``.arh/.env`` with a legacy ``ARH_API_KEY`` entry gets
    rewritten without it. Unrelated entries are preserved.
    """
    setup_auto_tracking = _load_setup_auto_tracking(mcp_register)

    arh_dir = tmp_path / ".arh"
    arh_dir.mkdir()
    env_path = arh_dir / ".env"
    env_path.write_text(
        "# user-added comment\n"
        "ARH_API_KEY=arh_sk_legacy_dead_key\n"
        "ARH_API_URL=http://stale.example.com\n"
        "ARH_PROJECT_ID=11111111-1111-1111-1111-111111111111\n"
        "MY_OWN_VAR=keep_me\n"
    )

    await setup_auto_tracking(
        project_dir=str(tmp_path),
        project_id="22222222-2222-2222-2222-222222222222",
    )

    rewritten = env_path.read_text()
    assert "ARH_API_KEY" not in rewritten, (
        "Legacy ARH_API_KEY must be stripped from .arh/.env"
    )
    assert "MY_OWN_VAR=keep_me" in rewritten, (
        "Unrelated user-added entries must survive the rewrite"
    )
    assert "# user-added comment" in rewritten, "Comments must be preserved"
    assert "ARH_PROJECT_ID=22222222-2222-2222-2222-222222222222" in rewritten, (
        "Fresh project_id should replace the stale one"
    )
    assert "ARH_API_URL=http://stale.example.com" not in rewritten, (
        "Stale API URL should be replaced (or dropped to default)"
    )
