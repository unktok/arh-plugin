import pytest

from arh_mcp.tools import research


class _RecordingClient:
    async def post(self, path, json=None):
        assert path == "/v1/research/projects/project-1/logs/batch"
        assert json["logs"][0]["function_name"] == "hypothesis"
        return [{"id": "log-1"}, {"id": "log-2"}]


@pytest.mark.asyncio
async def test_log_research_steps_batch_wraps_list_response(mcp_register, monkeypatch):
    monkeypatch.setattr(research, "arh_client", _RecordingClient())
    tools = mcp_register(research.register)

    result = await tools["log_research_steps_batch"](
        project_id="project-1",
        steps=[
            {
                "step_type": "hypothesis",
                "title": "Working hypothesis",
                "content": "A useful agent needs a shareable trajectory.",
                "metadata": {"demo": True},
            }
        ],
    )

    assert result == {"logs": [{"id": "log-1"}, {"id": "log-2"}], "count": 2}
