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
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_API_URL = "https://api.airesearcherhub.com"


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        if path.is_file():
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        if not path.is_file():
            return values
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    except OSError:
        pass
    return values


def _load_context(cwd: Path) -> dict[str, str]:
    creds = _read_json_file(Path.home() / ".arh" / "credentials")
    project_env = _read_env_file(cwd / ".arh" / ".env")
    project_settings = _read_json_file(cwd / ".arh" / "settings.json")

    context = {
        "api_url": DEFAULT_API_URL,
        "api_key": "",
        "project_id": "",
        "trace_id": "",
    }
    if isinstance(creds.get("api_url"), str):
        context["api_url"] = creds["api_url"]
    if isinstance(creds.get("api_key"), str):
        context["api_key"] = creds["api_key"]
    if project_env.get("ARH_API_URL"):
        context["api_url"] = project_env["ARH_API_URL"]
    if project_env.get("ARH_PROJECT_ID"):
        context["project_id"] = project_env["ARH_PROJECT_ID"]
    if project_env.get("ARH_TRACE_ID"):
        context["trace_id"] = project_env["ARH_TRACE_ID"]
    if isinstance(project_settings.get("project_id"), str):
        context["project_id"] = project_settings["project_id"]

    context["api_url"] = os.environ.get("ARH_API_URL", context["api_url"])
    context["api_key"] = os.environ.get("ARH_API_KEY", context["api_key"])
    context["project_id"] = os.environ.get("ARH_PROJECT_ID", context["project_id"])
    context["trace_id"] = os.environ.get("ARH_TRACE_ID", context["trace_id"])
    return context


def _write_project_id(cwd: Path, project_id: str) -> None:
    arh_dir = cwd / ".arh"
    arh_dir.mkdir(exist_ok=True)
    settings_path = arh_dir / "settings.json"
    settings = _read_json_file(settings_path)
    if settings.get("project_id") == project_id:
        return
    settings["project_id"] = project_id
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")


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
    metadata = _parse_json_arg(args.metadata, "--metadata")
    if metadata is not None:
        if not isinstance(metadata, dict):
            raise SystemExit("--metadata must be a JSON object")
        payload["metadata"] = metadata
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
    elif command == "notification":
        payload["notification_type"] = args.notification_type
        payload["notification_message"] = args.message
        if args.title:
            payload["notification_title"] = args.title
    return {key: value for key, value in payload.items() if value is not None}


def _send_event(api_url: str, api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not api_key:
        raise SystemExit(
            "ARH_API_KEY is required. Set it in the environment or ~/.arh/credentials."
        )
    url = f"{api_url.rstrip('/')}/v1/hooks/agent-event"
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"ARH request failed ({exc.code}): {body}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"ARH request failed: {exc.reason}") from exc


def _add_common(parser: argparse.ArgumentParser, event_name: str) -> None:
    parser.set_defaults(event_name=event_name)
    parser.add_argument("--runtime", default="custom")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--project-id", default="")
    parser.add_argument("--trace-id", default="")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--metadata", help="JSON object to attach to metadata")
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

    notification = subparsers.add_parser("notification", help="Record a notification")
    _add_common(notification, "notification")
    notification.add_argument("--notification-type", default="info")
    notification.add_argument("--message", required=True)
    notification.add_argument("--title")
    return parser


def main() -> int:
    args = _parser().parse_args()
    cwd = Path(args.cwd).resolve()
    context = _load_context(cwd)
    payload = _build_payload(args, context)

    if args.dry_run:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    result = _send_event(context["api_url"], context["api_key"], payload)
    if args.command == "start" and result.get("project_id"):
        _write_project_id(cwd, result["project_id"])
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
