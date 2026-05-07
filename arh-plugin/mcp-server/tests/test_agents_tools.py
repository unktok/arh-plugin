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
