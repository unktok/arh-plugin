#!/usr/bin/env python3
"""Hook handler for AI Researcher Hub plugin.

Reads Claude Code hook events from stdin and sends them to the ARH backend.
Handles: SessionStart, PostToolUse, Stop, SubagentStop, Notification, TaskCompleted.
Parses transcript JSONL for thinking/reasoning extraction.
Uses only Python standard library (no external dependencies).
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
from pathlib import Path

import harness_common as hc

MAX_OUTPUT_LENGTH = 2000
MAX_THINKING_LENGTH = 5000
MAX_TRANSCRIPT_ENTRIES = 50


def load_arh_env():
    """Populate ARH_* env vars from user-global credentials and project config.

    Single source of truth model (introduced 2026-04):
      - **API key** comes ONLY from `~/.arh/credentials`. The project-local
        `.arh/.env` is NOT consulted for `ARH_API_KEY`. This prevents the
        round-5 silent-override bug where a project's stale `.arh/.env` kept
        re-asserting an old key after `register_agent` rewrote `~/.arh/credentials`.
      - **Project / trace context** still comes from project-local `.arh/.env`
        (`ARH_PROJECT_ID`, `ARH_API_URL`, `ARH_TRACE_ID`), because those are
        legitimately per-project values.

    Resolution order:
      1. `~/.arh/credentials` → ARH_API_KEY, ARH_API_URL
      2. Project `.arh/.env` → ARH_API_URL, ARH_PROJECT_ID, ARH_TRACE_ID
         (overrides API_URL if present; ARH_API_KEY in this file is IGNORED
          and a deprecation warning is printed to stderr)
      3. Existing shell env vars (preserved when no source above sets the key)
    """
    ARH_PROJECT_KEYS = ("ARH_API_URL", "ARH_PROJECT_ID", "ARH_TRACE_ID")

    # --- Collect values from each source ---
    env_file: dict[str, str] = {}
    env_path = os.path.join(os.getcwd(), ".arh", ".env")
    if os.path.isfile(env_path):
        try:
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        k, v = line.split("=", 1)
                        env_file[k.strip()] = v.strip()
        except OSError:
            pass

    creds: dict = {}
    creds_path = os.path.expanduser("~/.arh/credentials")
    if os.path.isfile(creds_path):
        try:
            with open(creds_path) as f:
                creds = json.loads(f.read())
        except (OSError, ValueError):
            creds = {}

    resolved: dict[str, str] = {}

    # API key: ONLY from user-global credentials.
    if creds.get("api_key"):
        resolved["ARH_API_KEY"] = creds["api_key"]
    if creds.get("api_url"):
        resolved["ARH_API_URL"] = creds["api_url"]

    # Project-local .arh/.env contributes project / trace context only.
    for k in ARH_PROJECT_KEYS:
        if k in env_file:
            resolved[k] = env_file[k]

    # Detect and warn about a stale ARH_API_KEY entry in .arh/.env (legacy
    # layout pre-2026-04). Such entries used to override credentials and
    # caused silent auth drift; we now ignore them. Strip-on-write happens
    # via setup_auto_tracking, but warn here for any out-of-band copy.
    if "ARH_API_KEY" in env_file:
        try:
            sys.stderr.write(
                f"[arh] note: {env_path} has ARH_API_KEY (legacy). "
                "Ignoring; run /arh:init-research to rewrite without it.\n"
            )
        except OSError:
            pass

    for k, v in resolved.items():
        os.environ[k] = v


def _arh_settings_path(cwd: str) -> str:
    """Return path to .arh/settings.json in the project directory."""
    return os.path.join(cwd, ".arh", "settings.json")


def _read_arh_settings(cwd: str) -> dict:
    """Read .arh/settings.json from the project directory."""
    path = _arh_settings_path(cwd)
    try:
        if os.path.isfile(path):
            with open(path, "r") as f:
                return json.loads(f.read().strip())
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _write_arh_settings(cwd: str, settings: dict) -> None:
    """Write settings to .arh/settings.json in the project directory."""
    arh_dir = os.path.join(cwd, ".arh")
    try:
        os.makedirs(arh_dir, exist_ok=True)
        path = _arh_settings_path(cwd)
        with open(path, "w") as f:
            json.dump(settings, f, indent=2)
    except OSError:
        pass


def parse_transcript(
    transcript_path: str, max_entries: int = MAX_TRANSCRIPT_ENTRIES
) -> list[dict]:
    """Parse transcript JSONL file and extract recent assistant messages with thinking.

    Returns a list of structured entries with thinking blocks and text blocks
    from the most recent assistant messages.
    """
    if not transcript_path or not os.path.isfile(transcript_path):
        return []

    entries = []
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        # Process from the end to get recent entries first
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = record.get("type", "")

            # Extract user messages (Claude Code uses "user", older format uses "human")
            if msg_type in ("user", "human"):
                message = record.get("message", {})
                content = message.get("content", [])
                if isinstance(content, str):
                    if content.strip():
                        entries.append(
                            {
                                "role": "user",
                                "type": "user_input",
                                "content": content[:MAX_OUTPUT_LENGTH],
                            }
                        )
                elif isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "text":
                            text = block.get("text", "")
                            if text.strip():
                                entries.append(
                                    {
                                        "role": "user",
                                        "type": "user_input",
                                        "content": text[:MAX_OUTPUT_LENGTH],
                                    }
                                )

            # Extract assistant messages for thinking/text output
            elif msg_type == "assistant":
                message = record.get("message", {})
                content = message.get("content", [])
                if isinstance(content, str):
                    # Simple text content
                    entries.append(
                        {
                            "role": "assistant",
                            "type": "text",
                            "content": content[:MAX_OUTPUT_LENGTH],
                        }
                    )
                elif isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        block_type = block.get("type", "")

                        if block_type == "thinking":
                            thinking_text = block.get("thinking", "")
                            if thinking_text:
                                entries.append(
                                    {
                                        "role": "assistant",
                                        "type": "thinking",
                                        "content": thinking_text[:MAX_THINKING_LENGTH],
                                    }
                                )
                        elif block_type == "text":
                            text = block.get("text", "")
                            if text:
                                entries.append(
                                    {
                                        "role": "assistant",
                                        "type": "text",
                                        "content": text[:MAX_OUTPUT_LENGTH],
                                    }
                                )

                if len(entries) >= max_entries:
                    break

    except (OSError, PermissionError):
        return []

    # Reverse to chronological order
    entries.reverse()
    return entries


def _detect_git_info(cwd: str) -> tuple[str, str]:
    """Detect git remote URL and branch from the working directory.

    Returns (remote_url, branch) or ("", "") if not a git repo.
    """
    if not cwd or not os.path.isdir(cwd):
        return "", ""

    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        remote_url = result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        remote_url = ""

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        branch = result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        branch = ""

    return remote_url, branch


def _read_new_transcript_entries(transcript_path: str, session_id: str) -> list[dict]:
    """Read new transcript entries since last read using byte offset tracking.

    Stores offset in /tmp/arh_offset_{session_id} to avoid re-reading.
    Returns new thinking/text entries found.
    """
    if not transcript_path or not os.path.isfile(transcript_path):
        return []

    offset_file = os.path.join(tempfile.gettempdir(), f"arh_offset_{session_id}")

    last_offset = 0
    try:
        if os.path.isfile(offset_file):
            with open(offset_file, "r") as f:
                last_offset = int(f.read().strip())
    except (OSError, ValueError):
        last_offset = 0

    entries = []
    new_offset = last_offset
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            f.seek(last_offset)
            while True:
                line = f.readline()
                if not line:
                    break
                new_offset = f.tell()
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                record_type = record.get("type", "")

                if record_type in ("user", "human"):
                    message = record.get("message", {})
                    content = message.get("content", [])
                    if isinstance(content, str):
                        if content.strip():
                            entries.append(
                                {
                                    "role": "user",
                                    "type": "user_input",
                                    "content": content[:MAX_OUTPUT_LENGTH],
                                }
                            )
                    elif isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") == "text":
                                text = block.get("text", "")
                                if text.strip():
                                    entries.append(
                                        {
                                            "role": "user",
                                            "type": "user_input",
                                            "content": text[:MAX_OUTPUT_LENGTH],
                                        }
                                    )
                elif record_type == "assistant":
                    message = record.get("message", {})
                    content = message.get("content", [])
                    if isinstance(content, str):
                        entries.append(
                            {
                                "role": "assistant",
                                "type": "text",
                                "content": content[:MAX_OUTPUT_LENGTH],
                            }
                        )
                    elif isinstance(content, list):
                        for block in content:
                            if not isinstance(block, dict):
                                continue
                            block_type = block.get("type", "")
                            if block_type == "thinking":
                                thinking_text = block.get("thinking", "")
                                if thinking_text:
                                    entries.append(
                                        {
                                            "role": "assistant",
                                            "type": "thinking",
                                            "content": thinking_text[
                                                :MAX_THINKING_LENGTH
                                            ],
                                        }
                                    )
                            elif block_type == "text":
                                text = block.get("text", "")
                                if text:
                                    entries.append(
                                        {
                                            "role": "assistant",
                                            "type": "text",
                                            "content": text[:MAX_OUTPUT_LENGTH],
                                        }
                                    )
    except (OSError, PermissionError):
        return []

    # Save new offset
    if new_offset > last_offset:
        try:
            with open(offset_file, "w") as f:
                f.write(str(new_offset))
        except OSError:
            pass

    return entries


def truncate(value: str, max_length: int = MAX_OUTPUT_LENGTH) -> str:
    """Truncate string with indicator."""
    if len(value) > max_length:
        return value[:max_length] + "... [truncated]"
    return value


_SHADOW_REF_PREFIX = "refs/heads/arh-auto"
_AUTO_CHECKPOINT_THROTTLE_SECONDS = 30
_AUTO_CHECKPOINT_STATE_FILE = ".arh/.auto-checkpoint-state"
_SHADOW_INDEX_FILE = ".arh/.shadow-index"
_MANUAL_CHECKPOINT_TOOL_NAMES = {
    "checkpoint",
    "mcp__plugin_arh_ai-researcher-hub__checkpoint",
}


def build_payload(event_type: str, event_data: dict) -> dict | None:
    """Build API payload based on event type. Returns None if no project_id is available."""
    session_id = event_data.get("session_id", "")
    payload = {
        "session_id": session_id,
        "hook_event_name": event_type,
        "cwd": event_data.get("cwd") or os.getcwd(),
    }

    # Include project_id: env var takes priority, then .arh/settings.json
    project_id = os.environ.get("ARH_PROJECT_ID", "")
    if not project_id:
        arh_settings = _read_arh_settings(payload["cwd"])
        project_id = arh_settings.get("project_id", "")
    if project_id:
        payload["project_id"] = project_id
    else:
        # No project_id means init-research hasn't been run yet.
        # Skip sending — but do NOT read transcript (would advance offset,
        # causing user input to be permanently skipped).
        return None

    # Include trace_id if set via environment variable
    trace_id = os.environ.get("ARH_TRACE_ID", "")
    if trace_id:
        payload["trace_id"] = trace_id

    if event_type == "SessionStart":
        # Auto-detect git info from working directory
        cwd = payload.get("cwd", "")
        git_remote, git_branch = _detect_git_info(cwd)
        if git_remote:
            payload["git_remote_url"] = git_remote
        if git_branch:
            payload["git_branch"] = git_branch
        # Seed the per-session shadow ref so PostToolUse / Stop auto-checkpoints
        # have a parent to commit against. Idempotent.
        if session_id:
            _ensure_shadow_ref(cwd, session_id)

    elif event_type == "PostToolUse":
        payload["tool_name"] = event_data.get("tool_name", "unknown")
        tool_input = event_data.get("tool_input", {})
        if isinstance(tool_input, dict):
            payload["tool_input"] = tool_input
        else:
            payload["tool_input"] = {"raw": str(tool_input)}

        tool_output = event_data.get("tool_response") or event_data.get(
            "tool_output", ""
        )
        if not isinstance(tool_output, str):
            tool_output = (
                json.dumps(tool_output)
                if isinstance(tool_output, (dict, list))
                else str(tool_output)
            )
        payload["tool_output"] = truncate(tool_output)

        # Incremental transcript capture
        transcript_path = event_data.get("transcript_path", "")
        if transcript_path:
            new_entries = _read_new_transcript_entries(transcript_path, session_id)
            if new_entries:
                payload["transcript_entries"] = new_entries

        # Auto-checkpoint: capture every file-mutating tool's output to a
        # session-private shadow ref. The agent never sees this — it's a
        # harness audit trail. Skip when the agent JUST called the manual
        # checkpoint MCP tool to avoid duplicate work.
        tool_name = payload["tool_name"]
        if (
            tool_name in ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash")
            and tool_name not in _MANUAL_CHECKPOINT_TOOL_NAMES
        ):
            ckpt = _auto_checkpoint(
                payload["cwd"], session_id, tool_name, tool_input or {}
            )
            if ckpt:
                payload["auto_checkpoint_sha"] = ckpt["sha"]
                payload["auto_checkpoint_summary"] = ckpt["summary"]

    elif event_type == "Stop":
        commit_result = event_data.get("_arh_auto_commit_result")
        if isinstance(commit_result, dict):
            payload["auto_commit_status"] = commit_result.get("status", "")
            payload["auto_commit_reason"] = commit_result.get("reason", "")
            if commit_result.get("sha"):
                payload["commit_sha"] = commit_result["sha"]
                payload["commit_message"] = commit_result.get("message", "")
        last_msg = event_data.get("last_assistant_message", "")
        if last_msg:
            payload["last_assistant_message"] = truncate(last_msg, MAX_OUTPUT_LENGTH)
        payload["stop_reason"] = event_data.get("stop_reason", "")

        # Read only NEW transcript entries since last PostToolUse to avoid
        # duplicating entries that were already sent incrementally.
        transcript_path = event_data.get("transcript_path", "")
        if transcript_path:
            new_entries = _read_new_transcript_entries(transcript_path, session_id)
            if new_entries:
                payload["transcript_entries"] = new_entries

        # uncommitted_files still drives the user-facing Stop systemMessage.
        uncommitted = _git_uncommitted_files(payload["cwd"])
        if uncommitted:
            payload["uncommitted_files"] = uncommitted

        # Final auto-checkpoint flush — captures any work that didn't make it
        # into a PostToolUse-triggered checkpoint. Bypasses the 30-second
        # throttle because end-of-session is the last chance to record state.
        # Note: when `.arh/settings.json` has `auto_commit=true`, the legacy
        # `_auto_commit_and_push` path may have already committed everything to
        # the active branch by the time we get here — in that case
        # `_git_has_changes` returns False and this flush no-ops. PostToolUse
        # captures the granular per-tool history in real usage.
        if session_id:
            ckpt = _auto_checkpoint(
                payload["cwd"], session_id, "Stop", {}, bypass_throttle=True
            )
            if ckpt:
                payload["auto_checkpoint_sha"] = ckpt["sha"]
                payload["auto_checkpoint_summary"] = ckpt["summary"]

    elif event_type == "SubagentStop":
        last_msg = event_data.get("last_assistant_message", "")
        if last_msg:
            payload["last_assistant_message"] = truncate(last_msg, MAX_OUTPUT_LENGTH)
        payload["subagent_type"] = event_data.get("agent_type", "")
        payload["subagent_id"] = event_data.get("agent_id", "")

        # Parse subagent transcript
        transcript_path = event_data.get("agent_transcript_path") or event_data.get(
            "transcript_path", ""
        )
        if transcript_path:
            transcript_entries = parse_transcript(transcript_path, max_entries=20)
            if transcript_entries:
                payload["transcript_entries"] = transcript_entries

        uncommitted = _git_uncommitted_files(payload["cwd"])
        if uncommitted:
            payload["uncommitted_files"] = uncommitted

    elif event_type == "Notification":
        payload["notification_type"] = event_data.get("notification_type", "")
        payload["notification_message"] = event_data.get("message", "")
        payload["notification_title"] = event_data.get("title", "")

    return payload


def _safe_session_token(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", session_id or "unknown")[:64]


def _shadow_ref_for(session_id: str) -> str:
    return f"{_SHADOW_REF_PREFIX}/{_safe_session_token(session_id)}"


def _git(
    cwd: str, args: list[str], env: dict | None = None, timeout: int = 15
) -> tuple[int, str, str]:
    """Run a git command; return (rc, stdout, stderr)."""
    proc_env = os.environ.copy()
    if env:
        proc_env.update(env)
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=proc_env,
        )
        return result.returncode, result.stdout, result.stderr
    except (OSError, subprocess.TimeoutExpired):
        return 1, "", ""


def _ensure_shadow_ref(cwd: str, session_id: str) -> bool:
    """Create the per-session shadow ref from HEAD if it doesn't exist yet.

    Idempotent. Returns True on success (ref exists at the end), False otherwise.
    """
    ref = _shadow_ref_for(session_id)
    rc, _, _ = _git(cwd, ["rev-parse", "--verify", "--quiet", ref])
    if rc == 0:
        return True
    rc, head_sha, _ = _git(cwd, ["rev-parse", "--verify", "--quiet", "HEAD"])
    head_sha = head_sha.strip()
    if rc != 0 or not head_sha:
        # No HEAD yet (fresh repo without commits) — skip; auto-checkpoint
        # will create the ref when the first commit lands via commit-tree
        # without a parent.
        return False
    rc, _, _ = _git(cwd, ["update-ref", ref, head_sha])
    return rc == 0


def _auto_checkpoint_throttled(cwd: str) -> bool:
    state_path = os.path.join(cwd, _AUTO_CHECKPOINT_STATE_FILE)
    try:
        mtime = os.path.getmtime(state_path)
    except OSError:
        return False
    return (time.time() - mtime) < _AUTO_CHECKPOINT_THROTTLE_SECONDS


def _touch_auto_checkpoint_state(cwd: str) -> None:
    state_path = os.path.join(cwd, _AUTO_CHECKPOINT_STATE_FILE)
    try:
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        with open(state_path, "w", encoding="utf-8") as fh:
            fh.write(str(time.time()))
    except OSError:
        pass


def _auto_summary(tool_name: str, tool_input: dict) -> str:
    """Generate a short, deterministic summary for an auto-checkpoint commit."""
    action_map = {
        "Edit": "edit",
        "Write": "write",
        "MultiEdit": "multi-edit",
        "NotebookEdit": "notebook-edit",
        "Bash": "bash",
    }
    action = action_map.get(tool_name, tool_name.lower() if tool_name else "tool")
    target = ""
    if isinstance(tool_input, dict):
        for key in ("file_path", "path", "notebook_path"):
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                target = os.path.basename(value)[:40]
                break
        if not target:
            cmd = tool_input.get("command")
            if isinstance(cmd, str) and cmd:
                target = cmd.strip().split("\n")[0][:40]
    # Strip control chars so a Bash command with embedded NUL/escape sequences
    # can't sneak through into the commit message audit trail.
    target = re.sub(r"[\x00-\x1f\x7f]", " ", target)
    return f"auto: {action} {target}".rstrip()


def _auto_checkpoint(
    cwd: str,
    session_id: str,
    tool_name: str,
    tool_input: dict,
    bypass_throttle: bool = False,
) -> dict | None:
    """Stage current working-tree changes into a shadow ref commit.

    Never touches HEAD or pushes. Returns {"sha": ..., "summary": ...} on a
    successful commit, or None when there's nothing to do (no repo, no
    changes, throttled, error).
    """
    if not _git_has_changes(cwd):
        return None
    if not bypass_throttle and _auto_checkpoint_throttled(cwd):
        return None
    if not _ensure_shadow_ref(cwd, session_id):
        return None

    ref = _shadow_ref_for(session_id)
    index_path = os.path.join(cwd, _SHADOW_INDEX_FILE)
    try:
        os.makedirs(os.path.dirname(index_path), exist_ok=True)
        # Best-effort cleanup of any stale index from a prior run.
        if os.path.exists(index_path):
            os.remove(index_path)
    except OSError:
        pass

    env = {"GIT_INDEX_FILE": index_path}

    rc, _, _ = _git(cwd, ["read-tree", ref], env=env)
    if rc != 0:
        return None
    rc, _, _ = _git(cwd, ["add", "-A"], env=env)
    if rc != 0:
        return None
    rc, tree_sha, _ = _git(cwd, ["write-tree"], env=env)
    tree_sha = tree_sha.strip()
    if rc != 0 or not tree_sha:
        return None

    summary = _auto_summary(tool_name, tool_input)
    rc, parent_sha, _ = _git(cwd, ["rev-parse", ref])
    parent_sha = parent_sha.strip()
    commit_args = ["commit-tree", tree_sha, "-m", summary]
    if rc == 0 and parent_sha:
        # If the working tree matches the parent (no real change vs shadow ref),
        # skip — happens when only manual checkpoint already captured everything.
        rc, _, _ = _git(cwd, ["diff-tree", "--quiet", parent_sha, tree_sha])
        if rc == 0:
            try:
                os.remove(index_path)
            except OSError:
                pass
            return None
        commit_args = ["commit-tree", tree_sha, "-p", parent_sha, "-m", summary]

    rc, commit_sha, _ = _git(cwd, commit_args)
    commit_sha = commit_sha.strip()
    if rc != 0 or not commit_sha:
        return None
    rc, _, _ = _git(cwd, ["update-ref", ref, commit_sha])
    try:
        os.remove(index_path)
    except OSError:
        pass
    if rc != 0:
        return None

    _touch_auto_checkpoint_state(cwd)
    return {"sha": commit_sha, "summary": summary, "ref": ref}


def _git_has_changes(cwd: str) -> bool:
    """Return True if the working tree has uncommitted changes."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


def _git_uncommitted_files(cwd: str, limit: int = 20) -> list[str]:
    """Return a bounded list of uncommitted file paths, or [] on error."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return []
        paths: list[str] = []
        for line in result.stdout.splitlines():
            if not line.strip():
                continue
            # Porcelain lines are "XY <path>" or "XY <old> -> <new>" for renames.
            entry = line[3:].strip()
            if " -> " in entry:
                entry = entry.split(" -> ", 1)[1]
            paths.append(entry)
            if len(paths) >= limit:
                break
        return paths
    except (OSError, subprocess.TimeoutExpired):
        return []


def _generate_commit_message(event_data: dict, event_type: str) -> str:
    """Generate an English commit message based on event type."""
    if event_type == "TaskCompleted":
        subject = event_data.get("task_subject", "")
        if subject:
            return f"research: complete {subject[:60]}"
    last_msg = event_data.get("last_assistant_message", "")
    if last_msg:
        # Take first line, keep it concise
        first_line = last_msg.strip().split("\n")[0][:60]
        return f"research: {first_line}"
    return "research: auto-commit progress"


def _auto_commit_and_push(cwd: str, event_data: dict, event_type: str) -> None:
    """Auto-commit all changes and push if a remote exists."""
    try:
        if not _git_has_changes(cwd):
            return

        message = _generate_commit_message(event_data, event_type)

        subprocess.run(
            ["git", "add", "."],
            cwd=cwd,
            capture_output=True,
            timeout=30,
        )
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=cwd,
            capture_output=True,
            timeout=30,
        )
        # Push — ignore failure if no remote configured
        subprocess.run(
            ["git", "push"],
            cwd=cwd,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _auto_commit_notification_payload(base_payload: dict, commit_result: dict) -> dict | None:
    if commit_result.get("status") != "blocked":
        return None
    return {
        "session_id": base_payload.get("session_id", ""),
        "hook_event_name": "Notification",
        "cwd": base_payload.get("cwd") or os.getcwd(),
        "project_id": base_payload.get("project_id", ""),
        "trace_id": base_payload.get("trace_id", ""),
        "notification_type": "auto_commit_blocked_secret_scan",
        "notification_title": "Claude auto-commit blocked",
        "notification_message": (
            "Claude Code auto-commit blocked before git commit: "
            f"{commit_result.get('error', 'secret scan failed')}"
        ),
    }


def _send_payload(api_url: str, api_key: str, payload: dict) -> dict:
    url = f"{api_url}/v1/hooks/claude-code"
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _exit_ok():
    """Exit cleanly with valid empty JSON so Claude Code doesn't report validation errors."""
    print("{}")
    sys.exit(0)


def main():
    if len(sys.argv) < 2:
        _exit_ok()

    event_type = sys.argv[1]

    # Load API key from ~/.arh/credentials and project context from .arh/.env.
    load_arh_env()

    # Read event JSON from stdin
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            _exit_ok()
        event_data = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        _exit_ok()

    cwd = event_data.get("cwd") or os.getcwd()
    auto_commit_result = None

    # --- Auto-commit (no API key needed, pure git) ---
    if event_type in ("Stop", "SubagentStop", "TaskCompleted"):
        if not event_data.get("stop_hook_active", False):
            arh_settings = _read_arh_settings(cwd)
            if arh_settings.get("project_id") and arh_settings.get("auto_commit", True):
                auto_commit_result = hc.auto_commit_and_push(Path(cwd), event_data, event_type)
                event_data["_arh_auto_commit_result"] = auto_commit_result

    # --- API reporting (requires API key) ---
    api_url = os.environ.get("ARH_API_URL", "https://api.airesearcherhub.com")
    api_key = os.environ.get("ARH_API_KEY", "")
    if not api_key:
        _exit_ok()

    session_id = event_data.get("session_id", "")
    if not session_id:
        _exit_ok()

    payload = build_payload(event_type, event_data)
    if payload is None:
        # No project_id found — init-research hasn't been run yet; skip.
        _exit_ok()

    # Send to backend
    try:
        result = _send_payload(api_url, api_key, payload)

        if auto_commit_result:
            notification = _auto_commit_notification_payload(payload, auto_commit_result)
            if notification:
                try:
                    _send_payload(api_url, api_key, notification)
                except (urllib.error.URLError, OSError, json.JSONDecodeError):
                    pass

        # Persist project_id for SessionStart if newly known.
        if event_type == "SessionStart" and result.get("project_id"):
            arh_settings = _read_arh_settings(cwd)
            if not arh_settings.get("project_id"):
                arh_settings["project_id"] = result["project_id"]
                _write_arh_settings(cwd, arh_settings)

        # Emit agent-visible context based on what the event schema allows.
        lines: list[str] = []
        if event_type == "SessionStart" and result.get("project_id"):
            status = result.get("status", "")
            label = "reused" if "reused" in status else "created"
            lines.append(
                f"Research project {label}: {result['project_id']} — "
                f"{result.get('title', 'Untitled')}"
            )
        nudge = result.get("nudge")
        if nudge:
            lines.append(nudge)

        if lines:
            text = "\n".join(lines)
            # SessionStart / UserPromptSubmit / PostToolUse accept
            # hookSpecificOutput.additionalContext (injected into agent
            # context). Stop / SubagentStop do not — use top-level
            # systemMessage (surfaced to the user) instead. This keeps
            # the handler schema-compliant for every event type.
            if event_type in ("SessionStart", "UserPromptSubmit", "PostToolUse"):
                output = {
                    "hookSpecificOutput": {
                        "hookEventName": event_type,
                        "additionalContext": text,
                    }
                }
            else:
                output = {"systemMessage": text}
            print(json.dumps(output))
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        # Silently fail — don't block Claude
        pass


if __name__ == "__main__":
    main()
