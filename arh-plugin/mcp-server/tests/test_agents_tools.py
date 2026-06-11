import pytest

from arh_mcp.tools import agents


class _RecordingClient:
    base_url = "http://localhost:0"

    def __init__(self):
        self.payload = None
        self.reset_args = None

    async def post(self, path, json=None):
        assert path == "/v1/agents/register"
        self.payload = json
        return {
            "id": "agent-id",
            "handle": json["handle"],
            "display_name": json["display_name"],
            "api_key": "arh_sk_test_key",
        }

    def reset_auth(self, api_key="", api_url=""):
        self.reset_args = {"api_key": api_key, "api_url": api_url}


@pytest.mark.asyncio
async def test_register_agent_sends_capabilities_and_specializations(
    mcp_register, monkeypatch, tmp_path
):
    client = _RecordingClient()
    monkeypatch.setattr(agents, "arh_client", client)
    monkeypatch.setenv("HOME", str(tmp_path))

    tools = mcp_register(agents.register)
    result = await tools["register_agent"](
        handle="smoke-agent",
        display_name="Smoke Agent",
        capabilities=["replication"],
        specializations=["evaluation", "nlp"],
    )

    assert result["_auth_active"] is True
    assert result["api_key"] == "arh_sk_[REDACTED]"
    assert client.payload["capabilities"] == ["replication"]
    assert client.payload["specializations"] == ["evaluation", "nlp"]
    assert client.reset_args == {"api_key": "arh_sk_test_key", "api_url": ""}


def test_plugin_summary_detects_restart_required(monkeypatch, tmp_path):
    runtime = tmp_path / ".claude" / "plugins" / "cache" / "arh-plugin" / "arh" / "0.3.8"
    install = tmp_path / ".claude" / "plugins" / "installed_plugins.json"
    install.parent.mkdir(parents=True)
    install.write_text(
        """
{
  "version": 2,
  "plugins": {
    "arh@arh-plugin": [
      {
        "scope": "user",
        "installPath": "%s",
        "version": "0.3.9"
      }
    ]
  }
}
"""
        % runtime
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(agents, "_runtime_plugin_root", lambda: runtime)
    monkeypatch.setattr(agents, "_runtime_plugin_version", lambda: "0.3.8")

    summary = agents._plugin_summary()

    assert summary["status"] == "restart_required"
    assert "Restart Claude Code" in summary["recommendation"]


def test_plugin_summary_detects_update_available(monkeypatch, tmp_path):
    runtime = tmp_path / ".claude" / "plugins" / "cache" / "arh-plugin" / "arh" / "0.3.8"
    marketplace = (
        tmp_path
        / ".claude"
        / "plugins"
        / "marketplaces"
        / "arh-plugin"
        / ".claude-plugin"
        / "marketplace.json"
    )
    marketplace.parent.mkdir(parents=True)
    marketplace.write_text(
        """
{
  "plugins": [
    {"name": "other", "version": "9.9.9"},
    {"name": "arh", "version": "0.3.9"}
  ]
}
"""
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(agents, "_runtime_plugin_root", lambda: runtime)
    monkeypatch.setattr(agents, "_runtime_plugin_version", lambda: "0.3.8")

    summary = agents._plugin_summary()

    assert summary["status"] == "update_available"
    assert "claude plugin update arh@arh-plugin --scope user" in summary[
        "recommendation"
    ]


def test_plugin_summary_ignores_installed_update_when_running_from_source(
    monkeypatch, tmp_path
):
    runtime = tmp_path / "workspace" / "ai-researcher-hub" / "arh-plugin"
    marketplace = (
        tmp_path
        / ".claude"
        / "plugins"
        / "marketplaces"
        / "arh-plugin"
        / ".claude-plugin"
        / "marketplace.json"
    )
    marketplace.parent.mkdir(parents=True)
    marketplace.write_text(
        """
{
  "plugins": [
    {"name": "arh", "version": "0.3.9"}
  ]
}
"""
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(agents, "_runtime_plugin_root", lambda: runtime)
    monkeypatch.setattr(agents, "_runtime_plugin_version", lambda: "0.3.8")

    summary = agents._plugin_summary()

    assert summary["status"] == "ok"


def test_diagnostic_redacts_paths_and_urls(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))

    assert agents._redact_path(tmp_path / ".arh" / "credentials").startswith("~/.arh")
    assert (
        agents._redact_url("https://user:secret@example.test/path?token=abc#frag")
        == "https://[redacted]@example.test/[redacted]?[redacted]#[redacted]"
    )
    assert agents._redact_error(f"failed under {tmp_path}/secret") == (
        "failed under ~/secret"
    )


async def test_health_summary_reports_invalid_api_url():
    summary = await agents._health_summary("http://localhost:bad")

    assert summary == {
        "status": "invalid_url",
        "api_url": "[invalid-url]",
        "error": "Configured API URL is invalid.",
    }


@pytest.mark.asyncio
async def test_configure_persists_digest_flag(mcp_register, monkeypatch, tmp_path):
    import json as json_module
    import os

    client = _RecordingClient()
    monkeypatch.setattr(agents, "arh_client", client)
    monkeypatch.setenv("HOME", str(tmp_path))
    tools = mcp_register(agents.register)

    creds_path = os.path.join(str(tmp_path), ".arh", "credentials")
    os.makedirs(os.path.dirname(creds_path), mode=0o700, exist_ok=True)
    with open(creds_path, "w") as f:
        json_module.dump({"api_key": "arh_sk_existing", "api_url": "http://x"}, f)

    result = await tools["configure"](digest=False)
    assert result["status"] == "ok"
    assert result["digest"] is False

    with open(creds_path) as f:
        creds = json_module.load(f)
    assert creds["digest"] is False
    # Existing values are preserved by a digest-only call.
    assert creds["api_key"] == "arh_sk_existing"
    assert creds["api_url"] == "http://x"
