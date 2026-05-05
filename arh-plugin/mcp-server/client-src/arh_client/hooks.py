"""Claude Code hooks integration for auto-capturing tool calls and stop events."""

from __future__ import annotations

import json
import os
import sys


MAX_OUTPUT_LENGTH = 2000


def generate_hooks_config(project_id: str) -> dict:
    """Generate Claude Code hooks config JSON.

    Args:
        project_id: Research project UUID to log tool calls to.

    Returns:
        Dict suitable for merging into .claude/settings.json.
    """
    env_prefix = f"ARH_PROJECT_ID={project_id} "
    return {
        "hooks": {
            "PostToolUse": [
                {
                    "matcher": {},
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{env_prefix}arh hooks process PostToolUse",
                        }
                    ],
                }
            ],
            "Stop": [
                {
                    "matcher": {},
                    "hooks": [
                        {
                            "type": "command",
                            "command": f"{env_prefix}arh hooks process Stop",
                        }
                    ],
                }
            ],
        }
    }


def install_hooks(project_id: str, settings_path: str = ".claude/settings.json"):
    """Install hooks config into Claude Code settings.json.

    Reads existing settings, merges hooks config, and writes back.
    Creates the .claude directory if needed. Preserves existing hooks/settings.

    Args:
        project_id: Research project UUID.
        settings_path: Path to the Claude Code settings file.
    """
    settings_dir = os.path.dirname(settings_path)
    if settings_dir:
        os.makedirs(settings_dir, exist_ok=True)

    existing: dict = {}
    if os.path.exists(settings_path):
        with open(settings_path) as f:
            existing = json.load(f)

    new_hooks = generate_hooks_config(project_id)

    # Merge hooks: append to existing event lists without duplicating
    existing_hooks = existing.get("hooks", {})
    for event_type, hook_entries in new_hooks["hooks"].items():
        if event_type not in existing_hooks:
            existing_hooks[event_type] = hook_entries
        else:
            # Check if an ARH hook already exists for this event
            existing_commands = {
                h.get("command", "")
                for entry in existing_hooks[event_type]
                for h in entry.get("hooks", [])
            }
            for entry in hook_entries:
                entry_command = entry["hooks"][0]["command"]
                if entry_command not in existing_commands:
                    existing_hooks[event_type].append(entry)

    existing["hooks"] = existing_hooks

    with open(settings_path, "w") as f:
        json.dump(existing, f, indent=2)
        f.write("\n")


def process_hook_event(event_type: str, project_id: str | None = None) -> None:
    """Process a hook event from stdin. Called by the hook command.

    Args:
        event_type: The hook event type (e.g. "PostToolUse", "Stop").
        project_id: Research project UUID. Falls back to ARH_PROJECT_ID env var.
    """
    project_id = project_id or os.environ.get("ARH_PROJECT_ID", "")
    if not project_id:
        return

    # Read JSON from stdin
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return
        event_data = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        return

    if event_type == "PostToolUse":
        _handle_post_tool_use(project_id, event_data)
    elif event_type == "Stop":
        _handle_stop(project_id, event_data)


def _handle_post_tool_use(project_id: str, event_data: dict):
    """Log a tool call event to the research project."""
    tool_name = event_data.get("tool_name", "unknown")
    tool_input = event_data.get("tool_input", {})
    tool_output = event_data.get("tool_output", "")
    session_id = event_data.get("session_id", "")

    # Truncate large outputs
    if isinstance(tool_output, str) and len(tool_output) > MAX_OUTPUT_LENGTH:
        tool_output = tool_output[:MAX_OUTPUT_LENGTH] + "... [truncated]"

    log_data = {
        "function_name": tool_name,
        "input_data": tool_input if isinstance(tool_input, dict) else {"raw": tool_input},
        "output_data": {"result": tool_output},
        "span_type": "tool_call",
        "tag": "claude_code_hook",
        "level": "info",
        "message": f"Tool call: {tool_name}",
        "session_id": session_id or None,
        "meta_data": {"source": "claude_code_hook"},
    }

    _send_hook_log(project_id, log_data)


def _handle_stop(project_id: str, event_data: dict):
    """Handle a Stop event — log it and optionally update project status."""
    session_id = event_data.get("session_id", "")

    log_data = {
        "function_name": "session_stop",
        "input_data": {},
        "output_data": {},
        "span_type": "observation",
        "tag": "claude_code_hook",
        "level": "info",
        "message": "Claude Code session stopped",
        "session_id": session_id or None,
        "meta_data": {"source": "claude_code_hook"},
    }

    _send_hook_log(project_id, log_data)


def _send_hook_log(project_id: str, log_data: dict):
    """Send a log entry via APIClient. Falls back to local logging on failure."""
    try:
        from arh_client.api import APIClient

        client = APIClient()
        client.add_log(project_id, log_data)
    except Exception:
        # Fall back to local log
        try:
            from arh_client.tracker import _save_local_log

            _save_local_log(
                project_id,
                log_data.get("function_name", "unknown"),
                log_data.get("input_data", {}),
                log_data.get("output_data", {}),
                log_data.get("tag", ""),
                log_data.get("execution_time", 0.0),
                log_data.get("level", "info"),
            )
        except Exception:
            pass
