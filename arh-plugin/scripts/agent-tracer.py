#!/usr/bin/env python3
"""Trace Codex and custom agents into AI Researcher Hub.

Modes:
  codex-tail  Replay or tail Codex session JSONL.
  jsonl-tail  Replay or tail canonical ARH agent-event JSONL.
  run         Run a subprocess and trace stdout/stderr/exit/git state.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable

import harness_common as hc


def _base_payload(args: argparse.Namespace, context: dict[str, str]) -> dict[str, Any]:
    cwd = Path(args.cwd).resolve()
    payload: dict[str, Any] = {
        "runtime": args.runtime,
        "session_id": args.session_id,
        "cwd": str(cwd),
        "metadata": {"tracer": "agent-tracer.py", "mode": args.mode},
    }
    project_id = args.project_id or context["project_id"]
    trace_id = args.trace_id or context["trace_id"]
    if project_id:
        payload["project_id"] = project_id
    if trace_id:
        payload["trace_id"] = trace_id
    if args.participant_id:
        payload["participant_id"] = args.participant_id
    codex_meta = getattr(args, "_codex_session_meta", {})
    if codex_meta:
        payload["metadata"].update(codex_meta)
    return payload


class Sender:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.cwd = Path(args.cwd).resolve()
        self.context = hc.load_context(self.cwd)

    def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.args.dry_run:
            print(hc.redact_text(json.dumps(payload, sort_keys=True)))
            return {"status": "dry_run", "project_id": payload.get("project_id")}
        result = hc.send_event(self.context["api_url"], self.context["api_key"], payload)
        if result.get("project_id"):
            hc.write_project_id(self.cwd, result["project_id"])
            self.context["project_id"] = result["project_id"]
        if result.get("nudge"):
            self._record_nudge(result)
        return result

    def _record_nudge(self, result: dict[str, Any]) -> None:
        message = result["nudge"]
        print(f"[arh] nudge: {message}", file=sys.stderr)
        try:
            log_path = self.cwd / ".arh" / "tracer-notifications.log"
            log_path.parent.mkdir(exist_ok=True)
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(result, sort_keys=True) + "\n")
        except OSError:
            pass


def _start_payload(args: argparse.Namespace, context: dict[str, str]) -> dict[str, Any]:
    payload = _base_payload(args, context)
    payload["event_name"] = "session_start"
    if args.title:
        payload["title"] = args.title
    if args.description:
        payload["description"] = args.description
    if args.tag:
        payload["tags"] = args.tag
    remote, branch = hc.detect_git_info(Path(args.cwd).resolve())
    if remote:
        payload["git_remote_url"] = remote
    if branch:
        payload["git_branch"] = branch
    return payload


def _stop_payload(
    args: argparse.Namespace,
    context: dict[str, str],
    reason: str,
    message: str = "",
    exit_code: int | None = None,
) -> dict[str, Any]:
    payload = _base_payload(args, context)
    payload["event_name"] = "session_stop"
    payload["stop_reason"] = reason
    if message:
        payload["message"] = message
    if exit_code is not None:
        payload["metadata"]["exit_code"] = exit_code
    files = hc.uncommitted_files(Path(args.cwd).resolve())
    if files:
        payload["uncommitted_files"] = files
    ckpt = None
    if not args.dry_run:
        ckpt = hc.auto_checkpoint(
            Path(args.cwd).resolve(),
            args.session_id,
            "session stop",
            bypass_throttle=True,
        )
    if ckpt:
        payload["auto_checkpoint_sha"] = ckpt["sha"]
        payload["auto_checkpoint_summary"] = ckpt["summary"]
    return payload


def _state_path(args: argparse.Namespace, source: Path) -> Path:
    token = hc.offset_token(f"{args.runtime}-{args.session_id}", source)
    return Path(os.environ.get("TMPDIR", "/tmp")) / f"arh_tracer_offset_{token}"


def _read_from_offset(args: argparse.Namespace, source: Path) -> Iterable[str]:
    offset_path = _state_path(args, source)
    try:
        offset = int(offset_path.read_text().strip()) if offset_path.is_file() else 0
    except (OSError, ValueError):
        offset = 0
    with source.open("r", encoding="utf-8") as fh:
        fh.seek(offset)
        while True:
            line = fh.readline()
            if not line:
                if not args.follow:
                    break
                time.sleep(args.interval)
                continue
            yield line
            if not args.dry_run:
                try:
                    offset_path.write_text(str(fh.tell()))
                except OSError:
                    pass


def _tool_call_input(payload: dict[str, Any]) -> dict[str, Any]:
    arguments = payload.get("arguments")
    if arguments is None:
        arguments = payload.get("input")
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
            return parsed if isinstance(parsed, dict) else {"arguments": parsed}
        except json.JSONDecodeError:
            return {"arguments": arguments}
    if isinstance(arguments, dict):
        return arguments
    return {}


def _tool_call_output(payload: dict[str, Any]) -> str:
    output = payload.get("output")
    if output is None:
        output = payload.get("result")
    return hc.truncate(output if isinstance(output, str) else json.dumps(output))


def _record_tool_call(args: argparse.Namespace, payload: dict[str, Any]) -> None:
    call_id = payload.get("call_id") or payload.get("id")
    if not call_id:
        return
    calls = getattr(args, "_codex_tool_calls", None)
    if calls is None:
        calls = {}
        setattr(args, "_codex_tool_calls", calls)
    calls[call_id] = {
        "name": payload.get("name") or payload.get("tool_name") or payload.get("type") or "tool_call",
        "input": _tool_call_input(payload),
    }


def _lookup_tool_call(args: argparse.Namespace, payload: dict[str, Any]) -> dict[str, Any] | None:
    call_id = payload.get("call_id") or payload.get("id")
    if not call_id:
        return None
    return getattr(args, "_codex_tool_calls", {}).get(call_id)


def _apply_codex_session_meta(args: argparse.Namespace, payload: dict[str, Any]) -> None:
    meta: dict[str, Any] = {}
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        args.cwd = cwd
        meta["codex_cwd"] = cwd
    for key in ("agent_role", "agent_nickname"):
        if payload.get(key):
            meta[key] = payload[key]
    source = payload.get("source")
    if isinstance(source, dict):
        meta["codex_source"] = source
        subagent = source.get("subagent") if isinstance(source.get("subagent"), dict) else None
        if subagent:
            subagent_id = subagent.get("thread_spawn") or subagent.get("id")
            if subagent_id and not args.participant_id:
                args.participant_id = f"session:{args.session_id}:subagent:{subagent_id}"
            meta["subagent_id"] = subagent_id
    if meta:
        current = getattr(args, "_codex_session_meta", {})
        current.update(meta)
        setattr(args, "_codex_session_meta", current)


def _maybe_attach_checkpoint(args: argparse.Namespace, event: dict[str, Any]) -> None:
    if args.dry_run:
        return
    tool_name = str(event.get("tool_name") or "")
    mutating_names = ("apply_patch", "exec_command", "write_stdin", "patch")
    if not any(name in tool_name for name in mutating_names):
        return
    ckpt = hc.auto_checkpoint(Path(args.cwd).resolve(), args.session_id, f"tool: {tool_name}")
    if ckpt:
        event["auto_checkpoint_sha"] = ckpt["sha"]
        event["auto_checkpoint_summary"] = ckpt["summary"]


def _codex_payloads(
    args: argparse.Namespace, context: dict[str, str], line: str
) -> list[dict[str, Any]]:
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return []
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    record_type = record.get("type")
    out: list[dict[str, Any]] = []

    if record_type == "session_meta":
        _apply_codex_session_meta(args, payload)
        return []

    entries = hc.transcript_entries_from_record(record)
    if entries:
        event = _base_payload(args, context)
        event["event_name"] = "message"
        event["transcript_entries"] = entries
        event["metadata"]["codex_record_type"] = record_type
        if payload.get("type") == "message":
            event["message_role"] = payload.get("role", "assistant")
        out.append(event)
        return out

    if record_type == "response_item" and payload.get("type") in {"function_call", "custom_tool_call"}:
        _record_tool_call(args, payload)
        event = _base_payload(args, context)
        event["event_name"] = "tool_use"
        event["tool_name"] = payload.get("name") or payload.get("tool_name") or payload["type"]
        event["tool_input"] = _tool_call_input(payload)
        event["metadata"].update(
            {
                "codex_item_type": payload["type"],
                "call_id": payload.get("call_id") or payload.get("id"),
            }
        )
        _maybe_attach_checkpoint(args, event)
        out.append(event)
        return out

    if record_type == "response_item" and payload.get("type") in {"function_call_output", "custom_tool_call_output"}:
        call = _lookup_tool_call(args, payload) or {}
        event = _base_payload(args, context)
        event["event_name"] = "tool_use"
        event["tool_name"] = call.get("name") or payload.get("name") or payload["type"]
        event["tool_input"] = call.get("input") or {"call_id": payload.get("call_id") or payload.get("id")}
        event["tool_output"] = _tool_call_output(payload)
        event["metadata"].update(
            {
                "codex_item_type": payload["type"],
                "call_id": payload.get("call_id") or payload.get("id"),
            }
        )
        _maybe_attach_checkpoint(args, event)
        out.append(event)
        return out

    if record_type == "event_msg" and payload.get("type") in {"task_started", "task_complete"}:
        event = _base_payload(args, context)
        event["event_name"] = "notification"
        event["notification_type"] = payload["type"]
        event["notification_message"] = payload.get("last_agent_message") or payload["type"]
        event["metadata"].update({k: v for k, v in payload.items() if k != "last_agent_message"})
        out.append(event)
    return out


def _generic_payloads(
    args: argparse.Namespace, context: dict[str, str], line: str
) -> list[dict[str, Any]]:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return []
    if not isinstance(event, dict):
        return []
    payload = _base_payload(args, context)
    payload.update(event)
    payload.setdefault("runtime", args.runtime)
    payload.setdefault("session_id", args.session_id)
    payload.setdefault("cwd", str(Path(args.cwd).resolve()))
    return [payload]


def _apply_context_defaults(payload: dict[str, Any], context: dict[str, str]) -> None:
    if context.get("project_id") and not payload.get("project_id"):
        payload["project_id"] = context["project_id"]
    if context.get("trace_id") and not payload.get("trace_id"):
        payload["trace_id"] = context["trace_id"]


def run_tail(args: argparse.Namespace) -> int:
    sender = Sender(args)
    source = Path(args.path).expanduser()
    adapter = _codex_payloads if args.mode == "codex-tail" else _generic_payloads
    started = False
    for line in _read_from_offset(args, source):
        for payload in adapter(args, sender.context, line):
            if not started:
                sender.cwd = Path(args.cwd).resolve()
                sender.context = hc.load_context(sender.cwd)
                if not args.dry_run:
                    hc.ensure_shadow_ref(sender.cwd, args.session_id)
                sender.send(_start_payload(args, sender.context))
                started = True
            _apply_context_defaults(payload, sender.context)
            result = sender.send(payload)
            if result.get("project_id"):
                sender.context["project_id"] = result["project_id"]
    if not started:
        sender.cwd = Path(args.cwd).resolve()
        sender.context = hc.load_context(sender.cwd)
        if not args.dry_run:
            hc.ensure_shadow_ref(sender.cwd, args.session_id)
        sender.send(_start_payload(args, sender.context))
    sender.send(_stop_payload(args, sender.context, "completed"))
    return 0


def run_subprocess(args: argparse.Namespace) -> int:
    sender = Sender(args)
    if not args.dry_run:
        hc.ensure_shadow_ref(sender.cwd, args.session_id)
    sender.send(_start_payload(args, sender.context))
    proc = subprocess.Popen(
        args.command,
        cwd=str(sender.cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    buffer: list[str] = []
    for line in proc.stdout:
        print(line, end="")
        buffer.append(line)
        if sum(len(x) for x in buffer) >= args.chunk_size:
            _send_output_chunk(args, sender, buffer)
            buffer = []
    if buffer:
        _send_output_chunk(args, sender, buffer)
    rc = proc.wait()
    reason = "completed" if rc == 0 else "failed"
    sender.send(_stop_payload(args, sender.context, reason, exit_code=rc))
    return rc


def _send_output_chunk(args: argparse.Namespace, sender: Sender, lines: list[str]) -> None:
    payload = _base_payload(args, sender.context)
    payload["event_name"] = "tool_use"
    payload["tool_name"] = "subprocess_output"
    payload["tool_input"] = {"command": args.command}
    payload["tool_output"] = hc.truncate("".join(lines))
    sender.send(payload)


def _common(parser: argparse.ArgumentParser, mode: str) -> None:
    parser.set_defaults(mode=mode)
    parser.add_argument("--runtime", default="custom")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--project-id", default="")
    parser.add_argument("--trace-id", default="")
    parser.add_argument("--participant-id", default="")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--title")
    parser.add_argument("--description")
    parser.add_argument("--tag", action="append")
    parser.add_argument("--dry-run", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)

    codex = sub.add_parser("codex-tail")
    _common(codex, "codex-tail")
    codex.add_argument("path")
    codex.add_argument("--follow", action="store_true")
    codex.add_argument("--interval", type=float, default=1.0)
    codex.set_defaults(func=run_tail, runtime="codex")

    generic = sub.add_parser("jsonl-tail")
    _common(generic, "jsonl-tail")
    generic.add_argument("path")
    generic.add_argument("--follow", action="store_true")
    generic.add_argument("--interval", type=float, default=1.0)
    generic.set_defaults(func=run_tail)

    run = sub.add_parser("run")
    _common(run, "run")
    run.add_argument("--chunk-size", type=int, default=2000)
    run.add_argument("command", nargs=argparse.REMAINDER)
    run.set_defaults(func=run_subprocess)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.mode == "run" and (not args.command or args.command[0] != "--"):
        raise SystemExit("run mode requires: agent-tracer.py run ... -- <command>")
    if args.mode == "run":
        args.command = args.command[1:]
    try:
        return args.func(args)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    sys.exit(main())
