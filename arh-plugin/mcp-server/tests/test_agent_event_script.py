from __future__ import annotations

import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread


REPO_ROOT = Path(__file__).resolve().parents[3]
AGENT_EVENT = REPO_ROOT / "arh-plugin" / "scripts" / "agent-event.py"


def _run_agent_event(tmp_path: Path, *args: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(AGENT_EVENT), *args],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_start_dry_run_builds_codex_payload(tmp_path: Path) -> None:
    payload = _run_agent_event(
        tmp_path,
        "start",
        "--runtime",
        "codex",
        "--session-id",
        "codex-run-1",
        "--title",
        "Codex Trial",
        "--description",
        "Testing ARH from Codex.",
        "--tag",
        "codex",
        "--metadata",
        '{"runner":"cli"}',
        "--dry-run",
    )

    assert payload["runtime"] == "codex"
    assert payload["session_id"] == "codex-run-1"
    assert payload["event_name"] == "session_start"
    assert payload["title"] == "Codex Trial"
    assert payload["description"] == "Testing ARH from Codex."
    assert payload["tags"] == ["codex"]
    assert payload["metadata"] == {"runner": "cli"}
    assert payload["cwd"] == str(tmp_path.resolve())


def test_tool_dry_run_reuses_project_context(tmp_path: Path) -> None:
    arh_dir = tmp_path / ".arh"
    arh_dir.mkdir()
    (arh_dir / "settings.json").write_text(
        '{"project_id":"00000000-0000-0000-0000-000000000001"}\n'
    )

    payload = _run_agent_event(
        tmp_path,
        "tool",
        "--runtime",
        "local-llm",
        "--session-id",
        "local-run-1",
        "--tool-name",
        "shell",
        "--tool-input",
        '{"cmd":"pytest"}',
        "--tool-output",
        "7 passed",
        "--dry-run",
    )

    assert payload["event_name"] == "tool_use"
    assert payload["project_id"] == "00000000-0000-0000-0000-000000000001"
    assert payload["tool_name"] == "shell"
    assert payload["tool_input"] == {"cmd": "pytest"}
    assert payload["tool_output"] == "7 passed"


def test_message_and_stop_dry_run_payloads(tmp_path: Path) -> None:
    message = _run_agent_event(
        tmp_path,
        "message",
        "--runtime",
        "custom",
        "--session-id",
        "custom-run-1",
        "--role",
        "assistant",
        "--message",
        "Found a baseline.",
        "--dry-run",
    )
    stop = _run_agent_event(
        tmp_path,
        "stop",
        "--runtime",
        "custom",
        "--session-id",
        "custom-run-1",
        "--message",
        "Done.",
        "--reason",
        "completed",
        "--dry-run",
    )

    assert message["event_name"] == "message"
    assert message["message_role"] == "assistant"
    assert message["message"] == "Found a baseline."
    assert stop["event_name"] == "session_stop"
    assert stop["message"] == "Done."
    assert stop["stop_reason"] == "completed"


def test_task_completed_dry_run_payload(tmp_path: Path) -> None:
    payload = _run_agent_event(
        tmp_path,
        "task-completed",
        "--runtime",
        "codex",
        "--session-id",
        "codex-run-task",
        "--message",
        "Finished the task.",
        "--commit-sha",
        "abcdef1234567",
        "--commit-message",
        "research: Finished the task.",
        "--dry-run",
    )

    assert payload["event_name"] == "task_completed"
    assert payload["message"] == "Finished the task."
    assert payload["commit_sha"] == "abcdef1234567"
    assert payload["commit_message"] == "research: Finished the task."


def test_401_mentions_stale_env_override(tmp_path: Path) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"detail":"Invalid API key"}')

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        result = subprocess.run(
            [
                sys.executable,
                str(AGENT_EVENT),
                "start",
                "--runtime",
                "codex",
                "--session-id",
                "codex-run-401",
                "--title",
                "401",
            ],
            cwd=tmp_path,
            env={
                **os.environ,
                "ARH_API_URL": f"http://127.0.0.1:{server.server_port}",
                "ARH_API_KEY": "arh_sk_stale",
            },
            capture_output=True,
            text=True,
            timeout=15,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert result.returncode != 0
    assert "ARH request failed (401)" in result.stderr
    assert "environment overrides ~/.arh/credentials" in result.stderr
