import pytest

from arh_mcp.tools import communication


class _RecordingClient:
    def __init__(self):
        self.calls = []

    async def get(self, path, params=None):
        self.calls.append((path, params))
        assert path == "/v1/search"
        return [
            {
                "kind": "snapshot",
                "entity_id": "snapshot-1",
                "title": "Transformer Efficiency",
                "preview": "Latency experiment",
            },
            {
                "kind": "project",
                "entity_id": "project-1",
                "title": "Transformer Cache Study",
                "preview": "Inference work",
            },
        ]


class _RecentActivityClient:
    def __init__(self):
        self.calls = []

    async def get(self, path, params=None):
        self.calls.append((path, params))
        return []


class _CommunicationClient:
    def __init__(self):
        self.calls = []

    async def get(self, path, params=None):
        self.calls.append(("get", path, params))
        if path.endswith("/messages"):
            return []
        if path.startswith("/v1/comments/"):
            return []
        return {"id": path.rsplit("/", 1)[-1]}

    async def post(self, path, json=None):
        self.calls.append(("post", path, json))
        return {"id": "created"}

    async def patch(self, path, json=None):
        self.calls.append(("patch", path, json))
        return {"id": "updated"}

    async def delete(self, path):
        self.calls.append(("delete", path))


@pytest.mark.asyncio
async def test_search_calls_server_side_search_endpoint(mcp_register, monkeypatch):
    client = _RecordingClient()
    monkeypatch.setattr(communication, "arh_client", client)
    tools = mcp_register(communication.register)

    result = await tools["search"](q="transformer", kinds=["snapshot", "project"], limit=5)

    assert client.calls == [
        ("/v1/search", {"q": "transformer", "limit": 5, "kinds": "snapshot,project"})
    ]
    assert result["total"] == 2
    assert [item["entity_id"] for item in result["items"]] == [
        "snapshot-1",
        "project-1",
    ]


@pytest.mark.asyncio
async def test_list_recent_activity_disables_telemetry_by_default(
    mcp_register, monkeypatch
):
    client = _RecentActivityClient()
    monkeypatch.setattr(communication, "arh_client", client)
    tools = mcp_register(communication.register)

    result = await tools["list_recent_activity"](
        limit=5,
        kinds=["snapshot", "project"],
        tags=["smoke"],
    )

    assert result == []
    assert client.calls == [
        (
            "/v1/feed/recent",
            {
                "limit": 5,
                "exclude_self": "true",
                "kinds": "snapshot,project",
                "tags": "smoke",
                "log_activity": "false",
            },
        )
    ]


@pytest.mark.asyncio
async def test_create_thread_uses_public_thread_fields(mcp_register, monkeypatch):
    client = _CommunicationClient()
    monkeypatch.setattr(communication, "arh_client", client)
    tools = mcp_register(communication.register)

    result = await tools["create_thread"](
        title="Discuss",
        participant_handles=["alice"],
        thread_type="discussion",
        artifact_id="artifact-1",
        project_id="project-1",
        initial_message="Initial",
        tags=["nlp"],
    )

    assert result == {"id": "created"}
    assert client.calls == [
        (
            "post",
            "/v1/threads",
            {
                "title": "Discuss",
                "thread_type": "discussion",
                "tags": ["nlp"],
                "participant_handles": ["alice"],
                "artifact_id": "artifact-1",
                "project_id": "project-1",
                "initial_message": "Initial",
            },
        )
    ]


@pytest.mark.asyncio
async def test_create_thread_rejects_private_thread_type(mcp_register, monkeypatch):
    client = _CommunicationClient()
    monkeypatch.setattr(communication, "arh_client", client)
    tools = mcp_register(communication.register)

    result = await tools["create_thread"](title="Private", thread_type="private")

    assert "error" in result
    assert client.calls == []


@pytest.mark.asyncio
async def test_send_message_supports_reply_to_id(mcp_register, monkeypatch):
    client = _CommunicationClient()
    monkeypatch.setattr(communication, "arh_client", client)
    tools = mcp_register(communication.register)

    await tools["send_message"](
        thread_id="thread-1", body="Reply", reply_to_id="message-1"
    )

    assert client.calls == [
        (
            "post",
            "/v1/threads/thread-1/messages",
            {"body": "Reply", "reply_to_id": "message-1"},
        )
    ]


@pytest.mark.asyncio
async def test_comment_accepts_research_project_alias(mcp_register, monkeypatch):
    client = _CommunicationClient()
    monkeypatch.setattr(communication, "arh_client", client)
    tools = mcp_register(communication.register)

    await tools["comment"](
        entity_type="research_project",
        entity_id="project-1",
        body="Project-level note",
    )

    assert client.calls == [
        (
            "post",
            "/v1/comments/research_project/project-1",
            {"body": "Project-level note"},
        )
    ]


@pytest.mark.asyncio
async def test_comment_list_and_promote_map_entity_types(mcp_register, monkeypatch):
    client = _CommunicationClient()
    monkeypatch.setattr(communication, "arh_client", client)
    tools = mcp_register(communication.register)

    await tools["list_comments"](
        entity_type="snapshot",
        entity_id="snapshot-1",
        sort="old",
        label="note",
        limit=3,
        offset=2,
    )
    await tools["promote_comment_to_thread"](
        entity_type="project",
        entity_id="project-1",
        comment_id="comment-1",
        title="Discuss",
        tags=["nlp"],
    )

    assert client.calls == [
        (
            "get",
            "/v1/comments/artifact/snapshot-1",
            {"sort": "old", "limit": 3, "offset": 2, "label": "note"},
        ),
        (
            "post",
            "/v1/comments/research_project/project-1/comment-1/promote",
            {"tags": ["nlp"], "title": "Discuss"},
        ),
    ]


@pytest.mark.asyncio
async def test_update_and_delete_comment_map_entity_types(mcp_register, monkeypatch):
    client = _CommunicationClient()
    monkeypatch.setattr(communication, "arh_client", client)
    tools = mcp_register(communication.register)

    await tools["update_comment"](
        entity_type="snapshot",
        entity_id="snapshot-1",
        comment_id="comment-1",
        body="Updated",
        label="note",
    )
    result = await tools["delete_comment"](
        entity_type="project",
        entity_id="project-1",
        comment_id="comment-2",
    )

    assert result == {"deleted": True, "comment_id": "comment-2"}
    assert client.calls == [
        (
            "patch",
            "/v1/comments/artifact/snapshot-1/comment-1",
            {"body": "Updated", "label": "note"},
        ),
        ("delete", "/v1/comments/research_project/project-1/comment-2"),
    ]
