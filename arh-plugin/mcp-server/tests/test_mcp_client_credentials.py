import json
import os
import stat
from pathlib import Path

import httpx
import pytest

from arh_mcp.client import ARHClient
from arh_mcp.tools import agents


def _write_creds(home: Path, data: dict) -> Path:
    creds_dir = home / ".arh"
    creds_dir.mkdir(parents=True)
    path = creds_dir / "credentials"
    path.write_text(json.dumps(data) + "\n")
    return path


def test_mcp_client_prefers_stored_credentials_over_env_pair(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _write_creds(
        home,
        {
            "api_key": "arh_sk_stored",
            "api_url": "https://stored.example.test",
        },
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ARH_API_KEY", "arh_sk_env")
    monkeypatch.setenv("ARH_API_URL", "https://env.example.test")

    client = ARHClient()

    assert client.api_key == "arh_sk_stored"
    assert client.base_url == "https://stored.example.test"


def test_mcp_client_uses_env_when_no_stored_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ARH_API_KEY", "arh_sk_env")
    monkeypatch.setenv("ARH_API_URL", "https://env.example.test")

    client = ARHClient()

    assert client.api_key == "arh_sk_env"
    assert client.base_url == "https://env.example.test"


def test_mcp_client_reset_auth_reloads_stored_credentials_without_env_shadow(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    _write_creds(
        home,
        {
            "api_key": "arh_sk_stored",
            "api_url": "https://stored.example.test",
        },
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ARH_API_KEY", "arh_sk_env")
    monkeypatch.setenv("ARH_API_URL", "https://env.example.test")

    client = ARHClient()
    client.api_key = "arh_sk_other"
    client.base_url = "https://other.example.test"
    client.reset_auth()

    assert client.api_key == "arh_sk_stored"
    assert client.base_url == "https://stored.example.test"


@pytest.mark.asyncio
async def test_mcp_client_refreshes_credentials_changed_after_startup(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ARH_API_KEY", "arh_sk_stale")
    monkeypatch.setenv("ARH_API_URL", "https://env.example.test")

    client = ARHClient()
    assert client.api_key == "arh_sk_stale"
    assert client.base_url == "https://env.example.test"

    _write_creds(
        home,
        {
            "api_key": "arh_sk_fresh",
            "api_url": "https://stored.example.test",
        },
    )

    client._client = httpx.AsyncClient(
        base_url=client.base_url,
        headers={"Authorization": f"Bearer {client.api_key}"},
    )
    old_http_client = client._client

    await client._refresh_credentials_if_changed()

    assert old_http_client.is_closed
    assert client._client is None
    assert client.api_key == "arh_sk_fresh"
    assert client.base_url == "https://stored.example.test"


def test_agent_credentials_writer_sets_private_modes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    path = Path(agents._persist_api_key("arh_sk_test", "https://api.example.test"))

    assert stat.S_IMODE((home / ".arh").stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == {
        "api_key": "arh_sk_test",
        "api_url": "https://api.example.test",
    }


@pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW"),
    reason="symlink refusal relies on O_NOFOLLOW",
)
def test_agent_credentials_writer_refuses_symlink_file(tmp_path, monkeypatch):
    home = tmp_path / "home"
    creds_dir = home / ".arh"
    creds_dir.mkdir(parents=True)
    target = tmp_path / "target"
    (creds_dir / "credentials").symlink_to(target)
    monkeypatch.setenv("HOME", str(home))

    with pytest.raises(OSError):
        agents._persist_api_key("arh_sk_test", "https://api.example.test")

    assert not target.exists()
