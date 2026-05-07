import pytest

from arh_mcp.tools import research


class _RecordingClient:
    async def post(self, path, json=None):
        assert path == "/v1/research/projects/project-1/logs/batch"
        assert json["logs"][0]["function_name"] == "hypothesis"
        return [{"id": "log-1"}, {"id": "log-2"}]


class _VisibilityClient:
    def __init__(self):
        self.patch_calls = []

    async def patch(self, path, json=None):
        self.patch_calls.append((path, json))
        return {"id": "project-1", **(json or {})}


class _SnapshotClient:
    def __init__(self):
        self.post_calls = []

    async def post(self, path, json=None, params=None):
        self.post_calls.append((path, json, params))
        return {"id": "snapshot-1", "status": "draft"}

    async def get(self, path):
        assert path == "/v1/research/projects/project-1"
        return {"id": "project-1", "visibility": "private"}


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


@pytest.mark.asyncio
async def test_update_visibility_requires_public_confirmation(mcp_register, monkeypatch):
    client = _VisibilityClient()
    monkeypatch.setattr(research, "arh_client", client)
    tools = mcp_register(research.register)

    blocked = await tools["update_research_project_visibility"](
        project_id="project-1",
        visibility="public",
    )
    assert blocked["error"] == "Publishing requires confirm_public=True."
    assert client.patch_calls == []

    result = await tools["update_research_project_visibility"](
        project_id="project-1",
        visibility="public",
        confirm_public=True,
    )
    assert result["visibility"] == "public"
    assert client.patch_calls == [
        ("/v1/research/projects/project-1", {"visibility": "public", "confirm_public": True})
    ]


@pytest.mark.asyncio
async def test_update_visibility_allows_private_without_confirmation(
    mcp_register, monkeypatch
):
    client = _VisibilityClient()
    monkeypatch.setattr(research, "arh_client", client)
    tools = mcp_register(research.register)

    result = await tools["update_research_project_visibility"](
        project_id="project-1",
        visibility="private",
    )

    assert result["visibility"] == "private"
    assert client.patch_calls == [
        ("/v1/research/projects/project-1", {"visibility": "private"})
    ]


@pytest.mark.asyncio
async def test_create_snapshot_returns_private_project_visibility_hint(
    mcp_register, monkeypatch
):
    monkeypatch.setattr(research, "arh_client", _SnapshotClient())
    tools = mcp_register(research.register)

    result = await tools["create_snapshot"](
        project_id="project-1",
        title="Finding",
        summary="A short summary.",
        body="Full body.",
    )

    assert result["id"] == "snapshot-1"
    assert "public_visibility_hint" in result
    assert "arh project visibility project-1 public --confirm-public" in result[
        "public_visibility_hint"
    ]
