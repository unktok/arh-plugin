from __future__ import annotations

import json
from pathlib import Path

from arh_client import cli


class _RegisterClient:
    _base_url = "http://localhost:8765"

    def register_agent(self, data):
        assert data["handle"] == "local-agent"
        return {"handle": "local-agent", "api_key": "arh_sk_local_key"}


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
