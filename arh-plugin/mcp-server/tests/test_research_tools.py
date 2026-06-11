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
async def test_update_visibility_requires_public_confirmation(
    mcp_register, monkeypatch
):
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
        (
            "/v1/research/projects/project-1",
            {"visibility": "public", "confirm_public": True},
        )
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
    hint = result["public_visibility_hint"]
    assert "update_research_project_visibility" in hint
    assert 'project_id="project-1"' in hint
    assert 'visibility="public"' in hint
    assert "confirm_public=True" in hint
    assert "arh project visibility project-1 public --confirm-public" in hint


@pytest.mark.asyncio
async def test_create_snapshot_passes_supersedes_id(mcp_register, monkeypatch):
    client = _SnapshotClient()
    monkeypatch.setattr(research, "arh_client", client)
    tools = mcp_register(research.register)

    await tools["create_snapshot"](
        project_id="project-1",
        title="Finding v2",
        summary="A revised summary.",
        body="Revised body.",
        supersedes_id="snapshot-0",
    )

    _path, json_body, _params = client.post_calls[0]
    assert json_body["supersedes_id"] == "snapshot-0"

    client.post_calls.clear()
    await tools["create_snapshot"](
        project_id="project-1",
        title="Finding",
        summary="A short summary.",
        body="Full body.",
    )
    _path, json_body, _params = client.post_calls[0]
    assert "supersedes_id" not in json_body


class _LogRecordingClient:
    def __init__(self):
        self.calls = []

    async def post(self, path, json=None):
        self.calls.append((path, json))
        if path.endswith("/logs/batch"):
            return [{"id": "log-1"}]
        return {"id": "log-1"}


@pytest.mark.asyncio
async def test_log_research_step_passes_span_fields(mcp_register, monkeypatch):
    client = _LogRecordingClient()
    monkeypatch.setattr(research, "arh_client", client)
    tools = mcp_register(research.register)

    await tools["log_research_step"](
        project_id="project-1",
        step_type="decision",
        title="Chose A over B",
        span_type="decision",
    )
    _path, json_body = client.calls[0]
    assert json_body["span_type"] == "decision"
    assert "parent_id" not in json_body

    client.calls.clear()
    await tools["log_research_step"](
        project_id="project-1",
        step_type="experiment",
        title="Follow-up",
        parent_id="log-0",
    )
    _path, json_body = client.calls[0]
    assert json_body["parent_id"] == "log-0"
    assert "span_type" not in json_body


@pytest.mark.asyncio
async def test_log_research_steps_batch_passes_span_fields(mcp_register, monkeypatch):
    client = _LogRecordingClient()
    monkeypatch.setattr(research, "arh_client", client)
    tools = mcp_register(research.register)

    await tools["log_research_steps_batch"](
        project_id="project-1",
        steps=[
            {
                "step_type": "decision",
                "title": "Decision step",
                "span_type": "decision",
            },
            {
                "step_type": "experiment",
                "title": "Child step",
                "parent_id": "log-0",
            },
            {"step_type": "analysis", "title": "Plain step"},
        ],
    )
    _path, json_body = client.calls[0]
    logs = json_body["logs"]
    assert logs[0]["span_type"] == "decision" and "parent_id" not in logs[0]
    assert logs[1]["parent_id"] == "log-0" and "span_type" not in logs[1]
    assert "span_type" not in logs[2] and "parent_id" not in logs[2]
