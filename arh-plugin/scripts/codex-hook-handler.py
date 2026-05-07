#!/usr/bin/env python3
"""Send Codex native hook events to AI Researcher Hub.

Codex hooks pass a JSON object on stdin. This handler maps the native lifecycle
events into ARH's harness-neutral `/v1/hooks/agent-event` contract.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import harness_common as hc


def _event_name(args_event: str, hook: dict[str, Any]) -> str:
    return str(hook.get("hook_event_name") or hook.get("hookEventName") or args_event)


def _session_id(hook: dict[str, Any]) -> str:
    for key in ("session_id", "conversation_id", "thread_id"):
        value = hook.get(key)
        if isinstance(value, str) and value:
            return value
    turn_id = hook.get("turn_id")
    if isinstance(turn_id, str) and turn_id:
        return f"codex-turn-{turn_id}"
    return "codex-session"


def _cwd(hook: dict[str, Any]) -> Path:
    value = hook.get("cwd") or hook.get("workspace_root")
    if isinstance(value, str) and value:
        return Path(value).expanduser().resolve()
    return Path.cwd().resolve()


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {"value": value}


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, default=str)


def _base_payload(
    event_name: str,
    hook: dict[str, Any],
    context: dict[str, str],
    cwd: Path,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "runtime": "codex",
        "session_id": _session_id(hook),
        "cwd": str(cwd),
        "metadata": {
            "tracer": "codex-hook-handler.py",
            "codex_hook_event_name": event_name,
        },
    }
    for key in ("turn_id", "tool_use_id", "source"):
        if hook.get(key) is not None:
            payload["metadata"][key] = hook[key]
    if context.get("project_id"):
        payload["project_id"] = context["project_id"]
    if context.get("trace_id"):
        payload["trace_id"] = context["trace_id"]
    return payload


def build_payload(
    args_event: str,
    hook: dict[str, Any],
    context: dict[str, str],
    checkpoint: bool = True,
    commit_result: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    event_name = _event_name(args_event, hook)
    cwd = _cwd(hook)
    payload = _base_payload(event_name, hook, context, cwd)

    if event_name == "SessionStart":
        payload["event_name"] = "session_start"
        payload["title"] = hook.get("title") or f"Codex: {cwd.name}"
        remote, branch = hc.detect_git_info(cwd)
        if remote:
            payload["git_remote_url"] = remote
        if branch:
            payload["git_branch"] = branch
    elif event_name == "UserPromptSubmit":
        prompt = hook.get("prompt") or hook.get("user_prompt") or hook.get("message")
        payload["event_name"] = "message"
        payload["message_role"] = "user"
        payload["message"] = _as_text(prompt)
    elif event_name == "PostToolUse":
        payload["event_name"] = "tool_use"
        payload["tool_name"] = str(hook.get("tool_name") or "unknown")
        payload["tool_input"] = _as_dict(hook.get("tool_input") or {})
        payload["tool_output"] = hc.truncate(_as_text(hook.get("tool_response")))
        if hook.get("transcript_path"):
            entries = hc.read_new_transcript_entries(
                Path(str(hook["transcript_path"])).expanduser(),
                payload["session_id"],
            )
            if entries:
                payload["transcript_entries"] = entries
        ckpt = None
        if checkpoint:
            ckpt = hc.auto_checkpoint(cwd, payload["session_id"], f"tool: {payload['tool_name']}")
        if ckpt:
            payload["auto_checkpoint_sha"] = ckpt["sha"]
            payload["auto_checkpoint_summary"] = ckpt["summary"]
    elif event_name == "Stop":
        payload["event_name"] = "session_stop"
        payload["stop_reason"] = str(hook.get("stop_reason") or "completed")
        if hook.get("last_assistant_message"):
            payload["message"] = _as_text(hook["last_assistant_message"])
        if commit_result and commit_result.get("sha"):
            payload["commit_sha"] = commit_result["sha"]
            payload["commit_message"] = commit_result.get("message", "")
        if hook.get("transcript_path"):
            payload["transcript_entries"] = hc.read_new_transcript_entries(
                Path(str(hook["transcript_path"])).expanduser(),
                payload["session_id"],
            )
        files = hc.uncommitted_files(cwd)
        if files:
            payload["uncommitted_files"] = files
        ckpt = None
        if checkpoint:
            ckpt = hc.auto_checkpoint(cwd, payload["session_id"], "session stop", bypass_throttle=True)
        if ckpt:
            payload["auto_checkpoint_sha"] = ckpt["sha"]
            payload["auto_checkpoint_summary"] = ckpt["summary"]
    else:
        return None
    return payload


def _completion_message(hook: dict[str, Any], commit_result: dict[str, Any] | None) -> str:
    if hook.get("last_assistant_message"):
        return _as_text(hook["last_assistant_message"])
    if commit_result and commit_result.get("message"):
        return str(commit_result["message"])
    return "Codex session completed."


def _notification_payload(
    base: dict[str, Any],
    notification_type: str,
    message: str,
    title: str = "",
) -> dict[str, Any]:
    payload = {
        key: base[key]
        for key in ("runtime", "session_id", "cwd", "project_id", "trace_id", "metadata")
        if key in base
    }
    payload["event_name"] = "notification"
    payload["notification_type"] = notification_type
    payload["notification_message"] = message
    if title:
        payload["notification_title"] = title
    return payload


def build_payloads(
    args_event: str,
    hook: dict[str, Any],
    context: dict[str, str],
    checkpoint: bool = True,
    commit_result: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    primary = build_payload(
        args_event,
        hook,
        context,
        checkpoint=checkpoint,
        commit_result=commit_result,
    )
    if primary is None:
        return []

    payloads = [primary]
    if primary.get("event_name") != "session_stop":
        return payloads

    if commit_result:
        primary["metadata"]["codex_commit_status"] = str(commit_result.get("status") or "")
        if commit_result.get("reason"):
            primary["metadata"]["codex_commit_reason"] = str(commit_result["reason"])

    task_completed = {
        key: primary[key]
        for key in ("runtime", "session_id", "cwd", "project_id", "trace_id", "metadata")
        if key in primary
    }
    task_completed["event_name"] = "task_completed"
    task_completed["message"] = _completion_message(hook, commit_result)
    if commit_result and commit_result.get("sha"):
        task_completed["commit_sha"] = commit_result["sha"]
        task_completed["commit_message"] = commit_result.get("message", "")
    payloads.append(task_completed)

    if commit_result:
        status = commit_result.get("status")
        if status == "committed":
            msg = f"Codex auto-committed {commit_result.get('sha', '')[:8]}."
            if commit_result.get("push_failed"):
                msg += " Push failed; the commit remains local."
            elif commit_result.get("push_skipped"):
                msg += " Push skipped because no remote is configured."
            elif commit_result.get("pushed"):
                msg += " Push completed."
            payloads.append(_notification_payload(primary, "auto_commit", msg, "Codex auto-commit"))
        elif status == "error":
            payloads.append(
                _notification_payload(
                    primary,
                    "auto_commit_failed",
                    f"Codex auto-commit failed: {commit_result.get('error', 'unknown error')}",
                    "Codex auto-commit failed",
                )
            )
        elif status == "blocked":
            payloads.append(
                _notification_payload(
                    primary,
                    "auto_commit_blocked_secret_scan",
                    f"Codex auto-commit blocked before git commit: {commit_result.get('error', 'secret scan failed')}",
                    "Codex auto-commit blocked",
                )
            )
        elif status == "handoff":
            payloads.append(
                _notification_payload(
                    primary,
                    "codex_handoff",
                    "Codex handoff captured; no git commit was created in this environment.",
                    "Codex handoff captured",
                )
            )
    elif primary.get("uncommitted_files"):
        payloads.append(
            _notification_payload(
                primary,
                "uncommitted_files",
                "Codex session ended with uncommitted files.",
                "Codex uncommitted files",
            )
        )
    return payloads


def _is_primary_payload(payload: dict[str, Any]) -> bool:
    return payload.get("event_name") in {
        "session_start",
        "message",
        "tool_use",
        "session_stop",
    }


def _codex_commit_mode(settings: dict[str, Any]) -> str:
    mode = str(
        os.environ.get("ARH_CODEX_COMMIT_MODE")
        or settings.get("codex_commit_mode")
        or settings.get("auto_commit_mode")
        or ""
    ).strip().lower()
    if mode in {"handoff", "checkpoint", "checkpoint_only", "none"}:
        return "handoff"
    if mode in {"git", "commit", "auto"}:
        return "git"
    if os.environ.get("CODEX_CLOUD") or os.environ.get("CODEX_WEB"):
        return "handoff"
    return "git"


def _maybe_auto_commit(cwd: Path, hook: dict[str, Any], event_name: str) -> dict[str, Any] | None:
    if event_name != "Stop":
        return None
    settings = hc.read_settings(cwd)
    if not settings.get("project_id"):
        return None
    if settings.get("auto_commit", True) is False:
        return None
    if _codex_commit_mode(settings) == "handoff":
        return hc.auto_commit_handoff("codex_handoff_mode")
    return hc.auto_commit_and_push(cwd, hook, "Stop")


def _emit_nudge(event_name: str, result: dict[str, Any]) -> None:
    nudge = result.get("nudge")
    if not nudge:
        return
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": event_name,
                    "additionalContext": nudge,
                }
            }
        )
    )


def _write_hook_error(
    cwd: Path,
    event_name: str,
    payload: dict[str, Any],
    exc: RuntimeError,
) -> None:
    try:
        arh_dir = cwd / ".arh"
        arh_dir.mkdir(exist_ok=True)
        record = {
            "event_name": event_name,
            "payload_event_name": payload.get("event_name"),
            "error": str(exc),
        }
        with (arh_dir / "hook-errors.log").open("a") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        pass


def send_payloads(
    payloads: list[dict[str, Any]],
    context: dict[str, str],
    cwd: Path,
    event_name: str,
) -> bool:
    sent_primary = False
    last_error: RuntimeError | None = None
    for payload in payloads:
        try:
            result = hc.send_event(context["api_url"], context["api_key"], payload)
            if _is_primary_payload(payload):
                sent_primary = True
            if result.get("project_id"):
                hc.write_project_id(cwd, result["project_id"])
            _emit_nudge(event_name, result)
        except RuntimeError as exc:
            last_error = exc
            if _is_primary_payload(payload):
                print(f"[arh] Codex hook event failed: {exc}", file=sys.stderr)
            else:
                _write_hook_error(cwd, event_name, payload, exc)
    if last_error and not sent_primary:
        _write_hook_error(cwd, event_name, payloads[0], last_error)
    return sent_primary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("event_name")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        hook = json.loads(sys.stdin.read() or "{}")
        if not isinstance(hook, dict):
            hook = {}
    except json.JSONDecodeError as exc:
        print(f"[arh] Codex hook ignored invalid JSON: {exc}", file=sys.stderr)
        return 0

    cwd = _cwd(hook)
    context = hc.load_context(cwd)
    event_name = _event_name(args.event_name, hook)
    commit_result = None if args.dry_run else _maybe_auto_commit(cwd, hook, event_name)
    payloads = build_payloads(
        args.event_name,
        hook,
        context,
        checkpoint=not args.dry_run,
        commit_result=commit_result,
    )
    if not payloads:
        return 0

    if args.dry_run:
        print(json.dumps(payloads[0] if len(payloads) == 1 else payloads, indent=2, sort_keys=True))
        return 0

    send_payloads(payloads, context, cwd, event_name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
