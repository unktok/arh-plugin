from __future__ import annotations

import json
import os
import stat
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
    config_text = Path(config_path).read_text()
    hooks_text = Path(hooks_path).read_text()
    assert config_text == "[features]\nhooks = true\n"
    assert "ARH_API_KEY" not in hooks_text
    assert "arh_sk_" not in hooks_text


def test_codex_handler_lookup_ignores_repo_local_handler(
    tmp_path: Path, monkeypatch
):
    repo_handler = tmp_path / "arh-plugin" / "scripts" / "codex-hook-handler.py"
    repo_handler.parent.mkdir(parents=True)
    repo_handler.write_text("#!/usr/bin/env python3\nraise SystemExit('malicious')\n")
    env_handler = tmp_path / "env-plugin" / "scripts" / "codex-hook-handler.py"
    env_handler.parent.mkdir(parents=True)
    env_handler.write_text("#!/usr/bin/env python3\nraise SystemExit('malicious')\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ARH_PLUGIN_ROOT", str(env_handler.parent.parent))

    selected = cli._find_codex_hook_handler()

    assert selected
    assert Path(selected).resolve() != repo_handler.resolve()
    assert Path(selected).resolve() != env_handler.resolve()


def test_redact_cli_text_removes_credentials_and_local_paths():
    text = (
        "failed at /Users/alice/project/.codex/config.toml and "
        "/private/var/folders/example with arh_sk_secret_value"
    )

    redacted = cli._redact_cli_text(text)

    assert "arh_sk_secret_value" not in redacted
    assert "/Users/alice" not in redacted
    assert "/private/var" not in redacted
    assert "~/" not in redacted


def test_codex_hook_hash_matches_codex_discovery_shape():
    command = (
        "python3 /opt/arh/arh_client/_bundled/codex-hook-handler.py PostToolUse"
    )

    assert cli._codex_normalized_hook_hash(
        "PostToolUse",
        ".*",
        command,
        {"type": "command", "command": command},
    ) == "sha256:b640052d9c1269e2f1ab722b3b8660ce1ac48e63713ea4396ac974e1ed434145"


def test_confirm_codex_hook_trust_writes_user_config(
    tmp_path: Path, monkeypatch
):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    handler = tmp_path / "codex-hook-handler.py"
    handler.write_text("#!/usr/bin/env python3\n")
    monkeypatch.setattr(cli, "_find_codex_hook_handler", lambda: str(handler))

    cli._install_codex_hooks(str(tmp_path))
    report = cli._ensure_codex_hook_trust(str(tmp_path))

    assert report["project_trusted"] is True
    assert report["all_trusted"] is True
    assert report["missing_trusted_events"] == []
    config_text = (home / ".codex" / "config.toml").read_text()
    assert f'[projects."{tmp_path}"]' in config_text
    assert "trust_level = \"trusted\"" in config_text
    assert "hooks.state" in config_text
    assert "trusted_hash = \"sha256:" in config_text
    assert "ARH_API_KEY" not in config_text
    assert "arh_sk_" not in config_text


def test_confirm_codex_hook_trust_updates_inline_toml_config(
    tmp_path: Path, monkeypatch
):
    home = tmp_path / "home"
    config_path = home / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        'model = "gpt-5.4"\n'
        'projects = { "/existing" = { trust_level = "trusted" } }\n'
        'hooks = { state = { "old:key" = { trusted_hash = "sha256:old" } } }\n'
    )
    monkeypatch.setenv("HOME", str(home))
    handler = tmp_path / "codex-hook-handler.py"
    handler.write_text("#!/usr/bin/env python3\n")
    monkeypatch.setattr(cli, "_find_codex_hook_handler", lambda: str(handler))

    cli._install_codex_hooks(str(tmp_path))
    report = cli._ensure_codex_hook_trust(str(tmp_path))

    assert report["all_trusted"] is True
    parsed = cli._load_toml_config(str(config_path))
    assert parsed["model"] == "gpt-5.4"
    assert parsed["projects"]["/existing"]["trust_level"] == "trusted"
    assert parsed["projects"][str(tmp_path)]["trust_level"] == "trusted"
    assert parsed["hooks"]["state"]["old:key"]["trusted_hash"] == "sha256:old"
    assert len(report["trusted_events"]) == len(cli.CODEX_REQUIRED_HOOK_EVENTS)


def test_confirm_codex_hook_trust_rejects_scalar_toml_conflict(
    tmp_path: Path, monkeypatch
):
    home = tmp_path / "home"
    config_path = home / ".codex" / "config.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text('hooks = "/tmp/not-a-table"\n')
    monkeypatch.setenv("HOME", str(home))
    handler = tmp_path / "codex-hook-handler.py"
    handler.write_text("#!/usr/bin/env python3\n")
    monkeypatch.setattr(cli, "_find_codex_hook_handler", lambda: str(handler))

    cli._install_codex_hooks(str(tmp_path))
    with pytest.raises(ValueError, match="hooks"):
        cli._ensure_codex_hook_trust(str(tmp_path))

    parsed = cli._load_toml_config(str(config_path))
    assert parsed["hooks"] == "/tmp/not-a-table"


def test_codex_hook_trust_report_detects_untrusted_hooks(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    handler = tmp_path / "codex-hook-handler.py"
    handler.write_text("#!/usr/bin/env python3\n")
    monkeypatch.setattr(cli, "_find_codex_hook_handler", lambda: str(handler))

    cli._install_codex_hooks(str(tmp_path))
    report = cli._codex_hook_trust_report(str(tmp_path))

    assert report["project_trusted"] is False
    assert report["all_trusted"] is False
    assert report["missing_trusted_events"] == list(cli.CODEX_REQUIRED_HOOK_EVENTS)


def test_codex_hook_trust_entries_ignore_suffix_impostor(
    tmp_path: Path, monkeypatch
):
    current_handler = tmp_path / "package" / "codex-hook-handler.py"
    current_handler.parent.mkdir()
    current_handler.write_text("#!/usr/bin/env python3\n")
    impostor = tmp_path / "repo" / "arh-plugin" / "scripts" / "codex-hook-handler.py"
    impostor.parent.mkdir(parents=True)
    impostor.write_text("#!/usr/bin/env python3\nraise SystemExit('malicious')\n")
    monkeypatch.setattr(cli, "_find_codex_hook_handler", lambda: str(current_handler))
    hooks_dir = tmp_path / ".codex"
    hooks_dir.mkdir()
    (hooks_dir / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": ".*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f"python3 {impostor} PostToolUse",
                                }
                            ],
                        }
                    ]
                }
            }
        )
    )

    assert cli._codex_arh_hook_trust_entries(str(tmp_path)) == []


def test_codex_hook_trust_entries_accept_uvx_cached_bundled_handler(
    tmp_path: Path, monkeypatch
):
    current_handler = (
        tmp_path
        / "fresh"
        / "lib"
        / "python3.13"
        / "site-packages"
        / "arh_client"
        / "_bundled"
        / "codex-hook-handler.py"
    )
    current_handler.parent.mkdir(parents=True)
    current_handler.write_text("#!/usr/bin/env python3\n")
    stale_handler = (
        tmp_path.parent
        / f"{tmp_path.name}-uv-cache"
        / "uv-cache"
        / "lib"
        / "python3.13"
        / "site-packages"
        / "arh_client"
        / "_bundled"
        / "codex-hook-handler.py"
    )
    stale_handler.parent.mkdir(parents=True)
    stale_handler.write_text("#!/usr/bin/env python3\n")
    monkeypatch.setattr(cli, "_find_codex_hook_handler", lambda: str(current_handler))
    hooks_dir = tmp_path / ".codex"
    hooks_dir.mkdir()
    (hooks_dir / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    event: [
                        {
                            "matcher": ".*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f"python3 {stale_handler} {event}",
                                }
                            ],
                        }
                    ]
                    for event in cli.CODEX_REQUIRED_HOOK_EVENTS
                }
            }
        )
    )

    entries = cli._codex_arh_hook_trust_entries(str(tmp_path))

    assert {entry["event"] for entry in entries} == set(cli.CODEX_REQUIRED_HOOK_EVENTS)


def test_codex_hook_trust_entries_reject_non_package_bundled_impostor(
    tmp_path: Path, monkeypatch
):
    current_handler = tmp_path / "package" / "codex-hook-handler.py"
    current_handler.parent.mkdir()
    current_handler.write_text("#!/usr/bin/env python3\n")
    impostor = tmp_path / "outside" / "arh_client" / "_bundled" / "codex-hook-handler.py"
    impostor.parent.mkdir(parents=True)
    impostor.write_text("#!/usr/bin/env python3\n")
    monkeypatch.setattr(cli, "_find_codex_hook_handler", lambda: str(current_handler))
    hooks_dir = tmp_path / ".codex"
    hooks_dir.mkdir()
    (hooks_dir / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": ".*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f"python3 {impostor} PostToolUse",
                                }
                            ],
                        }
                    ]
                }
            }
        )
    )

    assert cli._codex_arh_hook_trust_entries(str(tmp_path)) == []


def test_codex_hook_trust_report_requires_project_trust(
    tmp_path: Path, monkeypatch
):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    handler = tmp_path / "codex-hook-handler.py"
    handler.write_text("#!/usr/bin/env python3\n")
    monkeypatch.setattr(cli, "_find_codex_hook_handler", lambda: str(handler))

    cli._install_codex_hooks(str(tmp_path))
    for entry in cli._codex_arh_hook_trust_entries(str(tmp_path)):
        cli._ensure_toml_nested_key(
            str(home / ".codex" / "config.toml"),
            ["hooks", "state", entry["key"]],
            "trusted_hash",
            entry["trusted_hash"],
        )

    report = cli._codex_hook_trust_report(str(tmp_path))
    assert report["project_trusted"] is False
    assert report["missing_trusted_events"] == []
    assert report["all_trusted"] is False
    assert cli._codex_installed_status_from_trust(report) == "installed_untrusted"


def test_codex_hook_trust_report_separates_modified_hashes(
    tmp_path: Path, monkeypatch
):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    handler = tmp_path / "codex-hook-handler.py"
    handler.write_text("#!/usr/bin/env python3\n")
    monkeypatch.setattr(cli, "_find_codex_hook_handler", lambda: str(handler))

    cli._install_codex_hooks(str(tmp_path))
    cli._ensure_codex_project_trust(str(tmp_path))
    post_tool_entry = next(
        entry
        for entry in cli._codex_arh_hook_trust_entries(str(tmp_path))
        if entry["event"] == "PostToolUse"
    )
    cli._ensure_toml_nested_key(
        str(home / ".codex" / "config.toml"),
        ["hooks", "state", post_tool_entry["key"]],
        "trusted_hash",
        "sha256:stale",
    )

    report = cli._codex_hook_trust_report(str(tmp_path))
    assert "PostToolUse" in report["modified_events"]
    assert "PostToolUse" not in report["missing_trusted_events"]
    assert report["all_trusted"] is False


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
    stale_arh_handler = (
        tmp_path.parent
        / f"{tmp_path.name}-uv-cache"
        / "lib"
        / "python3.13"
        / "site-packages"
        / "arh_client"
        / "_bundled"
        / "codex-hook-handler.py"
    )
    stale_arh_handler.parent.mkdir(parents=True)
    stale_arh_handler.write_text("#!/usr/bin/env python3\n")
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
                            "matcher": ".*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f"python3 {stale_arh_handler} PostToolUse",
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
    assert len(hooks) == 3
    assert "python3 ./local-audit.py" in commands
    assert any("/old/codex-hook-handler.py" in command for command in commands)
    assert not any(str(stale_arh_handler) in command for command in commands)
    assert any(str(handler) in command for command in commands)
    assert commands.index("python3 ./local-audit.py") == 2


def test_enable_codex_hooks_feature_updates_existing_config(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text('model = "gpt-5.4"\n\n[features]\nfoo = true\n')

    cli._enable_codex_hooks_feature(str(config_path))

    assert config_path.read_text() == (
        'model = "gpt-5.4"\n\n[features]\nhooks = true\nfoo = true\n'
    )


def test_enable_codex_hooks_feature_migrates_deprecated_flag(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'hooks = "/tmp/not-a-feature-hooks-path"\n\n'
        "[features]\n"
        "codex_hooks = true\n"
        "foo = true\n\n"
        "[hooks]\n"
        'example = "preserve"\n'
    )

    cli._enable_codex_hooks_feature(str(config_path))
    cli._enable_codex_hooks_feature(str(config_path))

    assert config_path.read_text() == (
        'hooks = "/tmp/not-a-feature-hooks-path"\n\n'
        "[features]\n"
        "hooks = true\n"
        "foo = true\n\n"
        "[hooks]\n"
        'example = "preserve"\n'
    )


def test_doctor_codex_reports_unverified_setup_without_secrets(
    tmp_path: Path, monkeypatch, capsys
):
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        cli, "_find_codex_hook_handler", lambda: "/tmp/codex-hook-handler.py"
    )
    (tmp_path / ".arh").mkdir()
    (tmp_path / ".arh" / "settings.json").write_text(
        json.dumps({"project_id": "00000000-0000-0000-0000-000000000001"})
    )
    (tmp_path / ".arh" / "adapter-status.json").write_text(
        json.dumps(
            {
                "selected_adapter": "codex",
                "status": "installed_unverified",
                "degraded": False,
                "native_hooks_installed": True,
                "native_hooks_verified": False,
                "degraded_reason": "arh_sk_should_not_be_here",
            }
        )
    )
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text("[features]\nhooks = true\n")
    (tmp_path / ".codex" / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "python3 /tmp/codex-hook-handler.py PostToolUse",
                                }
                            ]
                        }
                    ]
                }
            }
        )
    )

    args = type("Args", (), {"dir": str(tmp_path)})()
    cli.cmd_doctor_codex(args)

    report = json.loads(capsys.readouterr().out)
    assert report["features"]["hooks"] is True
    assert report["adapter_status"]["status"] == "installed_unverified"
    report_text = json.dumps(report)
    assert "arh_sk_should_not_be_here" not in report_text
    assert str(tmp_path) not in report_text
    assert "user_config" not in report["hook_trust"]


def test_doctor_codex_fix_repairs_deprecated_hook_config(
    tmp_path: Path, monkeypatch, capsys
):
    handler = tmp_path / "fresh-codex-hook-handler.py"
    handler.write_text("#!/usr/bin/env python3\n")
    stale_arh_handler = (
        tmp_path.parent
        / f"{tmp_path.name}-uv-cache"
        / "lib"
        / "python3.13"
        / "site-packages"
        / "arh_client"
        / "_bundled"
        / "codex-hook-handler.py"
    )
    stale_arh_handler.parent.mkdir(parents=True)
    stale_arh_handler.write_text("#!/usr/bin/env python3\n")
    monkeypatch.setattr(cli, "_find_codex_hook_handler", lambda: str(handler))
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)

    (tmp_path / ".arh").mkdir()
    (tmp_path / ".arh" / "settings.json").write_text(
        json.dumps({"project_id": "00000000-0000-0000-0000-000000000001"})
    )
    (tmp_path / ".arh" / "adapter-status.json").write_text(
        json.dumps(
            {
                "selected_adapter": "codex",
                "requested_runtime": "auto",
                "status": "installed",
                "degraded": False,
            }
        )
    )
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text(
        "[features]\ncodex_hooks = true\nfoo = true\n"
    )
    (tmp_path / ".codex" / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f"python3 {stale_arh_handler} PostToolUse",
                                }
                            ]
                        }
                    ]
                }
            }
        )
    )

    args = type(
        "Args",
        (),
        {"dir": str(tmp_path), "fix": True, "confirm_codex_hook_trust": False},
    )()
    cli.cmd_doctor_codex(args)

    report = json.loads(capsys.readouterr().out)
    assert report["fix"]["applied"] is True
    assert report["features"] == {"codex_hooks": False, "hooks": True}
    assert report["hooks_file"]["has_arh_handler"] is True
    assert report["adapter_status"]["status"] == "installed_untrusted"
    assert report["adapter_status"]["native_hooks_missing_events"] == list(
        cli.CODEX_REQUIRED_HOOK_EVENTS
    )

    config_text = (tmp_path / ".codex" / "config.toml").read_text()
    assert "hooks = true" in config_text
    assert "codex_hooks" not in config_text
    hooks_text = (tmp_path / ".codex" / "hooks.json").read_text()
    assert "fresh-codex-hook-handler.py" in hooks_text
    assert str(stale_arh_handler) not in hooks_text


def test_doctor_codex_fix_can_trust_generated_hooks(
    tmp_path: Path, monkeypatch, capsys
):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    handler = tmp_path / "fresh-codex-hook-handler.py"
    handler.write_text("#!/usr/bin/env python3\n")
    monkeypatch.setattr(cli, "_find_codex_hook_handler", lambda: str(handler))
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)

    (tmp_path / ".arh").mkdir()
    (tmp_path / ".arh" / "settings.json").write_text(
        json.dumps({"project_id": "00000000-0000-0000-0000-000000000001"})
    )

    args = type(
        "Args",
        (),
        {"dir": str(tmp_path), "fix": True, "confirm_codex_hook_trust": True},
    )()
    cli.cmd_doctor_codex(args)

    report = json.loads(capsys.readouterr().out)
    assert report["fix"]["applied"] is True
    assert report["fix"]["status"] == "installed_unverified"
    assert report["hook_trust"]["all_trusted"] is True
    assert (
        "Codex hooks are trusted but not verified yet. Run `/new` in Codex before research, or fully reopen Codex in this repository, then run one research turn."
        in report["issues"]
    )
    assert "trusted_hash" in (home / ".codex" / "config.toml").read_text()


def test_doctor_codex_fix_requires_existing_project_id(tmp_path: Path, capsys):
    (tmp_path / ".arh").mkdir()
    args = type(
        "Args",
        (),
        {"dir": str(tmp_path), "fix": True, "confirm_codex_hook_trust": True},
    )()

    cli.cmd_doctor_codex(args)

    report = json.loads(capsys.readouterr().out)
    assert report["fix"]["applied"] is False
    assert "project_id" in report["fix"]["error"]
    assert not (tmp_path / ".codex" / "config.toml").exists()


def test_find_project_context_dir_ignores_global_credentials_dir(tmp_path: Path):
    home = tmp_path / "home"
    workspace = home / "work"
    workspace.mkdir(parents=True)
    (home / ".arh").mkdir()
    (home / ".arh" / "credentials").write_text("{}")

    assert cli._find_project_context_dir(str(workspace)) == str(workspace)


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


def test_ensure_authenticated_falls_back_to_credentials_when_env_key_is_stale(
    tmp_path: Path, monkeypatch, capsys
):
    home = tmp_path / "home"
    creds_dir = home / ".arh"
    creds_dir.mkdir(parents=True)
    (creds_dir / "credentials").write_text(
        json.dumps(
            {
                "api_key": "arh_sk_fresh",
                "api_url": "https://api.airesearcherhub.com",
            }
        )
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ARH_API_KEY", "arh_sk_stale")

    seen_keys: list[str] = []

    def fake_authenticates(_api_url: str, api_key: str) -> bool:
        seen_keys.append(api_key)
        return api_key == "arh_sk_fresh"

    monkeypatch.setattr(cli, "_api_key_authenticates", fake_authenticates)

    args = type(
        "Args",
        (),
        {
            "handle": "",
            "display_name": "",
            "agent_description": "",
            "specializations": [],
            "capabilities": [],
        },
    )()

    cli._ensure_authenticated(args)

    assert seen_keys == ["arh_sk_fresh"]
    assert "ARH_API_KEY" not in os.environ
    assert "ignoring ambient ARH_API_KEY" in capsys.readouterr().err


def test_ensure_authenticated_rejects_placeholder_agent_identity(
    tmp_path: Path, monkeypatch, capsys
):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ARH_API_KEY", raising=False)

    args = type(
        "Args",
        (),
        {
            "handle": "agent-handle",
            "display_name": "Agent name",
            "agent_description": "",
            "specializations": [],
            "capabilities": [],
        },
    )()

    with pytest.raises(SystemExit):
        cli._ensure_authenticated(args)

    assert "replace the placeholder agent identity" in capsys.readouterr().err


def test_load_dotenv_config_ignores_project_local_api_key(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ARH_API_KEY", raising=False)
    monkeypatch.delenv("ARH_API_URL", raising=False)
    (tmp_path / ".env").write_text(
        "ARH_API_KEY=arh_sk_project_local_stale\n"
        "ARH_API_URL=https://api.example.test\n"
    )

    cli._load_dotenv_config()

    assert "ARH_API_KEY" not in os.environ
    assert os.environ["ARH_API_URL"] == "https://api.example.test"


def test_resolve_credentials_keeps_stored_key_bound_to_stored_url(
    tmp_path: Path, monkeypatch
):
    home = tmp_path / "home"
    creds_dir = home / ".arh"
    creds_dir.mkdir(parents=True)
    (creds_dir / "credentials").write_text(
        json.dumps(
            {
                "api_key": "arh_sk_fresh",
                "api_url": "https://stored.example.test",
            }
        )
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ARH_API_KEY", "arh_sk_stale")
    monkeypatch.setenv("ARH_API_URL", "https://env.example.test")

    assert cli._resolve_credentials() == (
        "https://stored.example.test",
        "arh_sk_fresh",
    )


def test_resolve_credentials_uses_env_without_stored_key(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ARH_API_KEY", "arh_sk_env")
    monkeypatch.setenv("ARH_API_URL", "https://env.example.test")

    assert cli._resolve_credentials() == ("https://env.example.test", "arh_sk_env")


def test_apply_cli_credentials_does_not_overwrite_stored_key_with_stale_env(
    tmp_path: Path, monkeypatch
):
    home = tmp_path / "home"
    creds_dir = home / ".arh"
    creds_dir.mkdir(parents=True)
    creds_path = creds_dir / "credentials"
    creds_path.write_text(
        json.dumps(
            {
                "api_key": "arh_sk_fresh",
                "api_url": "https://api.airesearcherhub.com",
            }
        )
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ARH_API_KEY", "arh_sk_stale")
    args = type(
        "Args",
        (),
        {"api_url": "https://api.example.test", "api_key": ""},
    )()

    cli._apply_cli_credentials(args)

    creds = json.loads(creds_path.read_text())
    assert creds["api_key"] == "arh_sk_fresh"
    assert creds["api_url"] == "https://api.example.test"


def test_apply_cli_credentials_does_not_pair_explicit_key_with_stale_env_url(
    tmp_path: Path, monkeypatch
):
    home = tmp_path / "home"
    creds_dir = home / ".arh"
    creds_dir.mkdir(parents=True)
    creds_path = creds_dir / "credentials"
    creds_path.write_text(
        json.dumps(
            {
                "api_key": "arh_sk_old",
                "api_url": "https://stored.example.test",
            }
        )
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ARH_API_URL", "https://env.example.test")
    args = type("Args", (), {"api_url": "", "api_key": "arh_sk_new"})()

    cli._apply_cli_credentials(args)

    creds = json.loads(creds_path.read_text())
    assert creds["api_key"] == "arh_sk_new"
    assert creds["api_url"] == "https://stored.example.test"


def test_apply_cli_credentials_explicit_key_uses_default_url_without_stored_url(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ARH_API_URL", "https://env.example.test")
    args = type("Args", (), {"api_url": "", "api_key": "arh_sk_new"})()

    cli._apply_cli_credentials(args)

    creds = json.loads((tmp_path / "home" / ".arh" / "credentials").read_text())
    assert creds["api_key"] == "arh_sk_new"
    assert creds["api_url"] == cli.DEFAULT_API_URL


def test_persist_credentials_sets_private_modes(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    creds_path = Path(cli._persist_credentials("arh_sk_test", "https://api.example.test"))

    assert stat.S_IMODE((home / ".arh").stat().st_mode) == 0o700
    assert stat.S_IMODE(creds_path.stat().st_mode) == 0o600


@pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW"),
    reason="symlink refusal relies on O_NOFOLLOW",
)
def test_persist_credentials_refuses_symlink_file(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    creds_dir = home / ".arh"
    creds_dir.mkdir(parents=True)
    target = tmp_path / "target"
    (creds_dir / "credentials").symlink_to(target)
    monkeypatch.setenv("HOME", str(home))

    with pytest.raises(OSError):
        cli._persist_credentials("arh_sk_test", "https://api.example.test")

    assert not target.exists()


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
        return "project-1", {"claude_hooks": False, "codex_hooks": True}

    monkeypatch.setattr(cli, "_run_research_setup", fake_setup)
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
    ]
    assert capsys.readouterr().out.strip() == "project-1"
