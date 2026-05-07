import pytest

from arh_mcp.tools import communication


class _RecordingClient:
    def __init__(self):
        self.calls = []

    async def get(self, path, params=None):
        self.calls.append((path, params))
        assert path == "/v1/snapshots"
        return [
            {
                "id": "snapshot-1",
                "title": "Transformer Efficiency",
                "description": "Latency experiment",
            },
            {
                "id": "snapshot-2",
                "title": "Unrelated",
                "description": "Other work",
            },
            {
                "id": "snapshot-3",
                "title": "Inference Notes",
                "description": "Transformer cache behavior",
            },
        ]


@pytest.mark.asyncio
async def test_search_filters_snapshots_without_legacy_search_endpoint(
    mcp_register, monkeypatch
):
    client = _RecordingClient()
    monkeypatch.setattr(communication, "arh_client", client)
    tools = mcp_register(communication.register)

    result = await tools["search"](q="transformer", limit=1)

    assert client.calls == [("/v1/snapshots", {"limit": 100})]
    assert result["total"] == 2
    assert result["items"] == [
        {
            "id": "snapshot-1",
            "title": "Transformer Efficiency",
            "description": "Latency experiment",
        }
    ]
