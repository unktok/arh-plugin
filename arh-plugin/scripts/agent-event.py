#!/usr/bin/env python3
"""Send harness-neutral agent events to AI Researcher Hub.

This script is intentionally stdlib-only so Codex, local LLM runners, shell
scripts, and custom agents can use the same ARH event contract without loading
Claude Code hooks.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import harness_common as hc


def _parse_json_arg(value: str | None, label: str) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON for {label}: {exc}") from exc


def _base_payload(args: argparse.Namespace, context: dict[str, str]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "runtime": args.runtime,
        "session_id": args.session_id,
        "event_name": args.event_name,
        "cwd": str(Path(args.cwd).resolve()),
    }
    project_id = args.project_id or context["project_id"]
    trace_id = args.trace_id or context["trace_id"]
    if project_id:
        payload["project_id"] = project_id
    if trace_id:
        payload["trace_id"] = trace_id
    if args.participant_id:
        payload["participant_id"] = args.participant_id
    metadata = _parse_json_arg(args.metadata, "--metadata")
    if metadata is not None:
        if not isinstance(metadata, dict):
            raise SystemExit("--metadata must be a JSON object")
        payload["metadata"] = metadata
    if args.auto_checkpoint_sha:
        payload["auto_checkpoint_sha"] = args.auto_checkpoint_sha
    if args.auto_checkpoint_summary:
        payload["auto_checkpoint_summary"] = args.auto_checkpoint_summary
    if args.transcript_path:
        entries = hc.read_new_transcript_entries(Path(args.transcript_path), args.session_id)
        if entries:
            payload["transcript_entries"] = entries
    return payload


def _build_payload(args: argparse.Namespace, context: dict[str, str]) -> dict[str, Any]:
    payload = _base_payload(args, context)
    command = args.command
    if command == "start":
        if args.title:
            payload["title"] = args.title
        if args.description:
            payload["description"] = args.description
        if args.tags:
            payload["tags"] = args.tags
    elif command == "tool":
        payload["tool_name"] = args.tool_name
        tool_input = _parse_json_arg(args.tool_input, "--tool-input")
        if tool_input is not None:
            if not isinstance(tool_input, dict):
                raise SystemExit("--tool-input must be a JSON object")
            payload["tool_input"] = tool_input
        if args.tool_output is not None:
            payload["tool_output"] = args.tool_output
    elif command == "message":
        payload["message"] = args.message
        payload["message_role"] = args.role
    elif command == "stop":
        if args.message is not None:
            payload["message"] = args.message
        if args.reason:
            payload["stop_reason"] = args.reason
    elif command == "subagent-stop":
        if args.message is not None:
            payload["message"] = args.message
        if args.subagent_type:
            payload["subagent_type"] = args.subagent_type
        if args.subagent_id:
            payload["subagent_id"] = args.subagent_id
    elif command == "notification":
        payload["notification_type"] = args.notification_type
        payload["notification_message"] = args.message
        if args.title:
            payload["notification_title"] = args.title
    elif command == "task-completed":
        payload["message"] = args.message
        if args.commit_sha:
            payload["commit_sha"] = args.commit_sha
        if args.commit_message:
            payload["commit_message"] = args.commit_message
    return {key: value for key, value in payload.items() if value is not None}


def _send_event(api_url: str, api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not api_key:
        raise SystemExit(
            "ARH_API_KEY is required. Set it in the environment or ~/.arh/credentials."
        )
    try:
        return hc.send_event(api_url, api_key, payload)
    except RuntimeError as exc:
        hint = ""
        message = str(exc)
        if "ARH request failed (401)" in message and os.environ.get("ARH_API_KEY"):
            hint = (
                " ARH_API_KEY from the environment overrides ~/.arh/credentials; "
                "unset or update it if it is stale."
            )
        raise SystemExit(f"{message}{hint}") from exc


def _add_common(parser: argparse.ArgumentParser, event_name: str) -> None:
    parser.set_defaults(event_name=event_name)
    parser.add_argument("--runtime", default="custom")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--project-id", default="")
    parser.add_argument("--trace-id", default="")
    parser.add_argument("--participant-id", default="")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--metadata", help="JSON object to attach to metadata")
    parser.add_argument("--transcript-path", default="")
    parser.add_argument("--auto-checkpoint-sha", default="")
    parser.add_argument("--auto-checkpoint-summary", default="")
    parser.add_argument("--dry-run", action="store_true")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="Start or reuse an ARH session")
    _add_common(start, "session_start")
    start.add_argument("--title")
    start.add_argument("--description")
    start.add_argument("--tag", dest="tags", action="append")

    tool = subparsers.add_parser("tool", help="Record a tool call")
    _add_common(tool, "tool_use")
    tool.add_argument("--tool-name", required=True)
    tool.add_argument("--tool-input", help="JSON object")
    tool.add_argument("--tool-output")

    message = subparsers.add_parser("message", help="Record an agent or user message")
    _add_common(message, "message")
    message.add_argument("--role", default="assistant")
    message.add_argument("--message", required=True)

    stop = subparsers.add_parser("stop", help="End an ARH session")
    _add_common(stop, "session_stop")
    stop.add_argument("--message")
    stop.add_argument("--reason")

    subagent = subparsers.add_parser("subagent-stop", help="Record a subagent stop")
    _add_common(subagent, "subagent_stop")
    subagent.add_argument("--message")
    subagent.add_argument("--subagent-type", default="")
    subagent.add_argument("--subagent-id", default="")

    notification = subparsers.add_parser("notification", help="Record a notification")
    _add_common(notification, "notification")
    notification.add_argument("--notification-type", default="info")
    notification.add_argument("--message", required=True)
    notification.add_argument("--title")

    task = subparsers.add_parser("task-completed", help="Record a task completion")
    _add_common(task, "task_completed")
    task.add_argument("--message", required=True)
    task.add_argument("--commit-sha", default="")
    task.add_argument("--commit-message", default="")
    return parser


def main() -> int:
    args = _parser().parse_args()
    cwd = Path(args.cwd).resolve()
    context = hc.load_context(cwd)
    payload = _build_payload(args, context)

    if args.dry_run:
        print(hc.redact_text(json.dumps(payload, indent=2, sort_keys=True)))
        return 0

    result = _send_event(context["api_url"], context["api_key"], payload)
    if args.command == "start" and result.get("project_id"):
        hc.write_project_id(cwd, result["project_id"])
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
