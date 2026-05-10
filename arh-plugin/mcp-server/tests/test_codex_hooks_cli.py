from __future__ import annotations

import json
from pathlib import Path

import pytest

from arh_client.api import APIClient
from arh_client import cli


class _RegisterClient:
    _base_url = "http://localhost:8765"

    def register_agent(self, data):
        assert data["handle"] == "local-agent"
        return {"handle": "local-agent", "api_key": "arh_sk_local_key"}


class _ProjectClient:
    def __init__(self):
        self.calls = []

    def update_project(self, project_id, data):
        self.calls.append((project_id, data))
        return {"id": project_id, **data}


class _LogClient:
    def __init__(self):
        self.calls = []

    def add_log(self, project_id, data):
        self.calls.append((project_id, data))
        return {"id": "log-1", "project_id": project_id, **data}


def _visibility_args(project_id="project-1", visibility="private", confirm_public=False):
    return type(
        "Args",
        (),
        {
            "project_id": project_id,
            "visibility": visibility,
            "confirm_public": confirm_public,
        },
    )()


def test_install_codex_hooks_creates_project_local_config(tmp_path: Path, monkeypatch):
    handler = tmp_path / "codex-hook-handler.py"
    handler.write_text("#!/usr/bin/env python3\n")
    monkeypatch.setattr(cli, "_find_codex_hook_handler", lambda: str(handler))
    hooks_path, config_path = cli._install_codex_hooks(str(tmp_path))

    hooks = json.loads(Path(hooks_path).read_text())
    assert set(hooks["hooks"]) == {
        "SessionStart",
        "UserPromptSubmit",
        "PostToolUse",
        "Stop",
    }
    assert "codex-hook-handler.py" in hooks["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
    assert Path(config_path).read_text() == "[features]\ncodex_hooks = true\n"


def test_register_persists_effective_client_base_url(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ARH_API_URL", raising=False)
    monkeypatch.setattr(cli, "_get_client", lambda: _RegisterClient())
    args = type(
        "Args",
        (),
        {
            "handle": "local-agent",
            "display_name": "Local Agent",
            "description": "",
            "model_provider": "",
            "model_name": "",
            "show_key": False,
        },
    )()

    cli.cmd_register(args)

    creds = json.loads((tmp_path / ".arh" / "credentials").read_text())
    assert creds["api_url"] == "http://localhost:8765"
    assert creds["api_key"] == "arh_sk_local_key"
    assert "arh_sk_[REDACTED]" in capsys.readouterr().out


def test_project_visibility_requires_confirmation_for_public(monkeypatch):
    client = _ProjectClient()
    monkeypatch.setattr(cli, "_get_client", lambda: client)

    with pytest.raises(SystemExit):
        cli.cmd_project_visibility(_visibility_args(visibility="public"))

    assert client.calls == []


def test_project_visibility_updates_private_without_confirmation(monkeypatch, capsys):
    client = _ProjectClient()
    monkeypatch.setattr(cli, "_get_client", lambda: client)

    cli.cmd_project_visibility(_visibility_args(visibility="private"))

    assert client.calls == [("project-1", {"visibility": "private"})]
    assert '"visibility": "private"' in capsys.readouterr().out


def test_project_visibility_updates_public_with_confirmation(monkeypatch, capsys):
    client = _ProjectClient()
    monkeypatch.setattr(cli, "_get_client", lambda: client)

    cli.cmd_project_visibility(
        _visibility_args(visibility="public", confirm_public=True)
    )

    assert client.calls == [
        ("project-1", {"visibility": "public", "confirm_public": True})
    ]
    assert '"visibility": "public"' in capsys.readouterr().out


def test_write_project_context_sets_codex_auto_commit_defaults(tmp_path: Path):
    cli._write_arh_project_context(
        str(tmp_path),
        "https://api.example.test",
        "00000000-0000-0000-0000-000000000001",
        runtime="codex",
        auto_commit=True,
        codex_commit_mode="git",
        secret_scan_required=True,
    )

    env_text = (tmp_path / ".arh" / ".env").read_text()
    settings = json.loads((tmp_path / ".arh" / "settings.json").read_text())

    assert "ARH_API_URL=https://api.example.test" in env_text
    assert settings["project_id"] == "00000000-0000-0000-0000-000000000001"
    assert settings["runtime"] == "codex"
    assert settings["track_research_version"] == 1
    assert settings["auto_commit"] is True
    assert settings["codex_commit_mode"] == "git"
    assert settings["secret_scan_required"] is True


def test_commit_payload_converts_legacy_file_paths_to_file_changes():
    payload = APIClient._build_commit_payload(
        "67a8cb694fda4e53d1df1a4bf2bb093b15f29d5e",
        message="research: checkpoint",
        branch="main",
        files_changed=["notes/universal-handoff-smoke.md"],
    )

    assert payload["files_changed"] == [
        {"path": "notes/universal-handoff-smoke.md", "status": "modified"}
    ]


def test_commit_payload_preserves_structured_file_changes():
    payload = APIClient._build_commit_payload(
        "67a8cb694fda4e53d1df1a4bf2bb093b15f29d5e",
        files_changed=[
            {
                "path": "notes/universal-handoff-smoke.md",
                "status": "added",
                "additions": 53,
                "deletions": 0,
            }
        ],
    )

    assert payload["files_changed"] == [
        {
            "path": "notes/universal-handoff-smoke.md",
            "status": "added",
            "additions": 53,
            "deletions": 0,
        }
    ]


def test_write_project_context_preserves_safe_handoff_mode(tmp_path: Path):
    cli._write_arh_project_context(
        str(tmp_path),
        "https://api.example.test",
        "00000000-0000-0000-0000-000000000003",
        runtime="codex",
        auto_commit=True,
        codex_commit_mode="handoff",
        secret_scan_required=True,
    )

    settings = json.loads((tmp_path / ".arh" / "settings.json").read_text())
    assert settings["auto_commit"] is True
    assert settings["codex_commit_mode"] == "handoff"


def test_write_project_context_preserves_unknown_settings(tmp_path: Path):
    arh_dir = tmp_path / ".arh"
    arh_dir.mkdir()
    (arh_dir / "settings.json").write_text(
        json.dumps({"custom": "keep", "auto_commit": True}) + "\n"
    )

    cli._write_arh_project_context(
        str(tmp_path),
        "https://api.airesearcherhub.com",
        "00000000-0000-0000-0000-000000000002",
        runtime="codex",
        auto_commit=False,
    )

    settings = json.loads((arh_dir / "settings.json").read_text())
    assert settings["custom"] == "keep"
    assert settings["project_id"] == "00000000-0000-0000-0000-000000000002"
    assert settings["auto_commit"] is False


def test_install_codex_hooks_preserves_unrelated_hooks(tmp_path: Path, monkeypatch):
    handler = tmp_path / "codex-hook-handler.py"
    handler.write_text("#!/usr/bin/env python3\n")
    monkeypatch.setattr(cli, "_find_codex_hook_handler", lambda: str(handler))
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    hooks_path = codex_dir / "hooks.json"
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "python3 /old/codex-hook-handler.py PostToolUse",
                                }
                            ],
                        },
                        {
                            "matcher": "apply_patch",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "python3 ./local-audit.py",
                                }
                            ],
                        },
                    ]
                }
            }
        )
    )

    cli._install_codex_hooks(str(tmp_path))

    hooks = json.loads(hooks_path.read_text())["hooks"]["PostToolUse"]
    commands = [entry["hooks"][0]["command"] for entry in hooks]
    assert "python3 ./local-audit.py" in commands
    assert not any("/old/codex-hook-handler.py" in command for command in commands)
    assert sum("codex-hook-handler.py" in command for command in commands) == 1


def test_enable_codex_hooks_feature_updates_existing_config(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text('model = "gpt-5.4"\n\n[features]\nfoo = true\n')

    cli._enable_codex_hooks_feature(str(config_path))

    assert config_path.read_text() == (
        'model = "gpt-5.4"\n\n[features]\ncodex_hooks = true\nfoo = true\n'
    )


def test_read_credentials_uses_arh_credentials_file(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    creds_dir = home / ".arh"
    creds_dir.mkdir(parents=True)
    (creds_dir / "credentials").write_text(
        json.dumps(
            {
                "api_key": "arh_sk_test",
                "api_url": "https://api.airesearcherhub.com",
            }
        )
    )
    monkeypatch.setenv("HOME", str(home))

    assert cli._read_credentials()["api_key"] == "arh_sk_test"


def test_api_timeout_seconds_defaults_and_env(monkeypatch):
    monkeypatch.delenv("ARH_HTTP_TIMEOUT", raising=False)
    assert cli._api_timeout_seconds() == 90.0

    monkeypatch.setenv("ARH_HTTP_TIMEOUT", "180")
    assert cli._api_timeout_seconds() == 180.0

    monkeypatch.setenv("ARH_HTTP_TIMEOUT", "bad")
    assert cli._api_timeout_seconds() == 90.0


def test_project_create_timeout_message_mentions_recovery(monkeypatch):
    monkeypatch.setenv("ARH_HTTP_TIMEOUT", "120")

    message = cli._project_create_timeout_message(TimeoutError("slow"))

    assert "after 120s" in message
    assert "--project-id <id>" in message
    assert "ARH_HTTP_TIMEOUT=180" in message


def test_cmd_log_uses_research_log_schema(monkeypatch, capsys):
    client = _LogClient()
    monkeypatch.setattr(cli, "_get_client", lambda: client)
    args = type(
        "Args",
        (),
        {
            "project_id": "project-1",
            "step_type": "experiment",
            "title": "Ran baseline",
            "content": "7 passed",
        },
    )()

    cli.cmd_log(args)

    assert client.calls == [
        (
            "project-1",
            {
                "function_name": "experiment",
                "message": "Ran baseline",
                "input_data": {"content": "7 passed"},
            },
        )
    ]
    assert '"function_name": "experiment"' in capsys.readouterr().out


def test_resolve_handoff_runtime_defaults_to_generic(tmp_path: Path, monkeypatch):
    for key in ("CODEX_THREAD_ID", "CODEX_CI", "CODEX_HOME", "OPENAI_CODEX"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)

    assert cli._resolve_handoff_runtime("auto") == "generic"


def test_resolve_handoff_runtime_detects_codex_env(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-1")

    assert cli._resolve_handoff_runtime("auto") == "codex"


def test_cmd_handoff_uses_safe_codex_handoff_mode(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-1")
    calls = []

    def fake_setup(args):
        calls.append(("setup", args.runtime, args.codex_commit_mode))
        return "project-1", {"claude_hooks": False}

    def fake_codex_hooks(project_dir):
        calls.append(("codex_hooks", project_dir))
        return str(tmp_path / ".codex" / "hooks.json"), str(tmp_path / ".codex" / "config.toml")

    monkeypatch.setattr(cli, "_run_research_setup", fake_setup)
    monkeypatch.setattr(cli, "_install_codex_hooks", fake_codex_hooks)
    monkeypatch.setattr(cli, "_print_research_setup_summary", lambda *args: None)
    args = type(
        "Args",
        (),
        {
            "title": "Universal Handoff",
            "runtime": "auto",
            "codex_commit_mode": None,
            "no_hooks": False,
        },
    )()

    cli.cmd_handoff(args)

    assert calls == [
        ("setup", "codex", "handoff"),
        ("codex_hooks", str(tmp_path)),
    ]
    assert capsys.readouterr().out.strip() == "project-1"
