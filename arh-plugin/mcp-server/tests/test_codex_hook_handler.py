from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
HANDLER_PATH = REPO_ROOT / "arh-plugin" / "scripts" / "codex-hook-handler.py"


def _load_handler():
    scripts_dir = str(REPO_ROOT / "arh-plugin" / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location("codex_hook_handler_under_test", HANDLER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_notification_failure_does_not_make_primary_stop_fail(tmp_path: Path, monkeypatch):
    handler = _load_handler()
    context = {"api_url": "https://api.example.test", "api_key": "arh_sk_test"}
    payloads = [
        {"event_name": "session_stop", "session_id": "s1"},
        {"event_name": "task_completed", "session_id": "s1"},
        {"event_name": "notification", "session_id": "s1"},
    ]
    calls: list[str] = []

    def fake_send_event(_api_url: str, _api_key: str, payload: dict):
        calls.append(payload["event_name"])
        if payload["event_name"] == "notification":
            raise RuntimeError("notification failed")
        return {}

    monkeypatch.setattr(handler.hc, "send_event", fake_send_event)

    assert handler.send_payloads(payloads, context, tmp_path, "Stop") is True
    assert calls == ["session_stop", "task_completed", "notification"]
    assert "notification failed" in (tmp_path / ".arh" / "hook-errors.log").read_text()


def test_primary_failure_is_logged_but_hook_can_exit_zero(tmp_path: Path, monkeypatch, capsys):
    handler = _load_handler()
    context = {"api_url": "https://api.example.test", "api_key": "arh_sk_test"}
    payloads = [{"event_name": "session_stop", "session_id": "s1"}]

    def fake_send_event(_api_url: str, _api_key: str, _payload: dict):
        raise RuntimeError("primary failed")

    monkeypatch.setattr(handler.hc, "send_event", fake_send_event)

    assert handler.send_payloads(payloads, context, tmp_path, "Stop") is False
    assert "primary failed" in capsys.readouterr().err
    assert "primary failed" in (tmp_path / ".arh" / "hook-errors.log").read_text()
