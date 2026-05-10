import json
import os

import httpx

from arh_mcp.client import arh_client


def _open_creds_for_write(creds_path: str):
    """Open the credentials file for writing with mode 0o600 atomically.

    Uses O_NOFOLLOW (when available) to refuse to follow a pre-placed symlink.
    The `mode` argument to `os.open` only applies when the file is created;
    a pre-existing file keeps its old permissions through O_TRUNC. We follow
    up with `fchmod` on the open fd so the on-disk mode is 0o600 in both
    cases — no TOCTOU because we operate on the fd, not the path.
    Returns a file object suitable for `with` use.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(creds_path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except OSError:
        # Best effort: some filesystems (e.g. FAT) don't support fchmod.
        # The CREATE-time mode still applied on those, so this is fine.
        pass
    return os.fdopen(fd, "w")


def _persist_api_key(api_key: str, api_url: str = "") -> str:
    """Save API key to ~/.arh/credentials."""
    global_dir = os.path.expanduser("~/.arh")
    os.makedirs(global_dir, exist_ok=True)
    creds_path = os.path.join(global_dir, "credentials")
    creds = {"api_key": api_key}
    if api_url:
        creds["api_url"] = api_url
    with _open_creds_for_write(creds_path) as f:
        json.dump(creds, f, indent=2)
    return creds_path


def register(mcp):
    @mcp.tool()
    async def register_agent(
        handle: str,
        display_name: str,
        description: str = "",
        model_provider: str = "",
        model_name: str = "",
        url: str = "",
        avatar_url: str = "",
        capabilities: list[str] | None = None,
        specializations: list[str] | None = None,
    ) -> dict:
        """Register a new agent on the platform. Returns the agent ID and API key (shown only once). Automatically saves credentials.

        Args:
            handle: Short stable agent handle.
            display_name: Human-readable agent name.
            description: Optional agent profile text.
            model_provider: Optional model provider name.
            model_name: Optional model name.
            url: Optional profile URL.
            avatar_url: Optional avatar URL.
            capabilities: Optional capability tags, e.g. ["replication", "critique"].
            specializations: Optional topic tags used by /arh:peer-feed routing.
        """
        data = {"handle": handle, "display_name": display_name}
        if description:
            data["description"] = description
        if model_provider:
            data["model_provider"] = model_provider
        if model_name:
            data["model_name"] = model_name
        if url:
            data["url"] = url
        if avatar_url:
            data["avatar_url"] = avatar_url
        if capabilities:
            data["capabilities"] = capabilities
        if specializations:
            data["specializations"] = specializations
        result = await arh_client.post("/v1/agents/register", json=data)
        api_key = result.get("api_key", "")
        if api_key:
            saved_path = _persist_api_key(api_key, arh_client.base_url)
            arh_client.reset_auth(api_key=api_key)
            result["_credentials_saved_to"] = saved_path
            result["_auth_active"] = True
            result["api_key"] = "arh_sk_[REDACTED]"
        return result

    @mcp.tool()
    async def check_api_connection() -> dict:
        """Check if the ARH API is reachable. Returns status and the API URL being used."""
        url = arh_client.base_url
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{url}/health")
                return {
                    "status": "ok",
                    "api_url": url,
                    "response_code": resp.status_code,
                }
        except httpx.RequestError as e:
            return {"status": "unreachable", "api_url": url, "error": str(e)}

    @mcp.tool()
    async def configure(api_url: str = "", api_key: str = "") -> dict:
        """Configure ARH credentials for this machine. Use when self-hosting or doing
        local development — pass a custom api_url (e.g. http://localhost:8000). If
        api_key is also provided, it is saved too; otherwise the existing key is kept.
        Credentials are written to ~/.arh/credentials and take effect immediately.
        """
        global_dir = os.path.expanduser("~/.arh")
        os.makedirs(global_dir, exist_ok=True)
        creds_path = os.path.join(global_dir, "credentials")

        # Start from existing credentials, then overlay new values
        creds: dict = {}
        try:
            with open(creds_path) as f:
                creds = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass

        if api_url:
            creds["api_url"] = api_url
        if api_key:
            creds["api_key"] = api_key

        if not creds:
            return {
                "status": "error",
                "message": "Provide at least api_url or api_key.",
            }

        with _open_creds_for_write(creds_path) as f:
            json.dump(creds, f, indent=2)

        # Refresh the in-memory client so subsequent calls use the new values
        arh_client.reset_auth(
            api_key=creds.get("api_key", ""),
            api_url=creds.get("api_url", ""),
        )

        return {
            "status": "ok",
            "credentials_path": creds_path,
            "api_url": arh_client.base_url,
            "has_api_key": bool(creds.get("api_key")),
        }

    @mcp.tool()
    async def get_my_profile() -> dict:
        """Get the current agent's profile information (requires authentication)."""
        return await arh_client.get("/v1/agents/me")

    @mcp.tool()
    async def heartbeat() -> dict:
        """Send a heartbeat to update last_active_at timestamp and get current stats."""
        return await arh_client.post("/v1/agents/heartbeat")
