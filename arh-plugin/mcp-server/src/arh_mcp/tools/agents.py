import json
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from arh_mcp.client import arh_client


DEFAULT_API_URL = "https://api.airesearcherhub.com"
PLUGIN_NAME = "arh"
MARKETPLACE_NAME = "arh-plugin"


def _credentials_dir() -> str:
    global_dir = os.path.expanduser("~/.arh")
    if os.path.islink(global_dir):
        raise OSError(f"Refusing to use symlinked credentials directory: {global_dir}")
    os.makedirs(global_dir, mode=0o700, exist_ok=True)
    if not os.path.isdir(global_dir):
        raise OSError(f"Credentials path is not a directory: {global_dir}")
    try:
        os.chmod(global_dir, 0o700)
    except OSError:
        pass
    return global_dir


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
    global_dir = _credentials_dir()
    creds_path = os.path.join(global_dir, "credentials")
    creds = {"api_key": api_key}
    if api_url:
        creds["api_url"] = api_url
    with _open_creds_for_write(creds_path) as f:
        json.dump(creds, f, indent=2)
        f.write("\n")
    return creds_path


def _safe_read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _runtime_plugin_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _runtime_plugin_version() -> str:
    data = _safe_read_json(_runtime_plugin_root() / ".claude-plugin" / "plugin.json")
    version = data.get("version")
    return version if isinstance(version, str) else ""


def _marketplace_plugin_version() -> str:
    manifest = (
        Path.home()
        / ".claude"
        / "plugins"
        / "marketplaces"
        / MARKETPLACE_NAME
        / ".claude-plugin"
        / "marketplace.json"
    )
    data = _safe_read_json(manifest)
    plugins = data.get("plugins")
    if not isinstance(plugins, list):
        return ""
    for plugin in plugins:
        if isinstance(plugin, dict) and plugin.get("name") == PLUGIN_NAME:
            version = plugin.get("version")
            return version if isinstance(version, str) else ""
    return ""


def _installed_plugin_record() -> dict:
    installed = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
    data = _safe_read_json(installed)
    records = data.get("plugins", {}).get(f"{PLUGIN_NAME}@{MARKETPLACE_NAME}", [])
    if not isinstance(records, list):
        return {}
    runtime_root = _runtime_plugin_root().resolve()
    for record in records:
        if not isinstance(record, dict):
            continue
        install_path = record.get("installPath")
        if isinstance(install_path, str):
            try:
                if Path(install_path).resolve() == runtime_root:
                    return record
            except OSError:
                pass
    return records[0] if records and isinstance(records[0], dict) else {}


def _version_tuple(version: str) -> tuple[int, ...] | None:
    parts = version.split(".")
    if not parts or any(not part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def _version_less(left: str, right: str) -> bool:
    left_parts = _version_tuple(left)
    right_parts = _version_tuple(right)
    if left_parts is None or right_parts is None:
        return False
    width = max(len(left_parts), len(right_parts))
    return left_parts + (0,) * (width - len(left_parts)) < right_parts + (0,) * (
        width - len(right_parts)
    )


def _redact_path(path: str | Path) -> str:
    value = str(path)
    home = str(Path.home())
    if home and (value == home or value.startswith(home + os.sep)):
        return "~" + value[len(home) :]
    return value


def _redact_url(url: str) -> str:
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "[invalid-url]"
    if not parts.scheme or not parts.netloc:
        return url
    hostname = parts.hostname or ""
    if parts.username or parts.password:
        userinfo = "[redacted]@"
    else:
        userinfo = ""
    try:
        port_value = parts.port
    except ValueError:
        return "[invalid-url]"
    port = f":{port_value}" if port_value is not None else ""
    netloc = f"{userinfo}{hostname}{port}"
    path = "/[redacted]" if parts.path and parts.path != "/" else parts.path
    query = "[redacted]" if parts.query else ""
    fragment = "[redacted]" if parts.fragment else ""
    return urlunsplit((parts.scheme, netloc, path, query, fragment))


def _redact_error(value: str) -> str:
    home = str(Path.home())
    if home:
        value = value.replace(home, "~")
    return value


def _credentials_summary() -> dict:
    creds_path = Path.home() / ".arh" / "credentials"
    creds = _safe_read_json(creds_path)
    stored_key = creds.get("api_key")
    stored_url = creds.get("api_url")
    env_key = os.environ.get("ARH_API_KEY", "")
    env_url = os.environ.get("ARH_API_URL", "")
    return {
        "credentials_path": _redact_path(creds_path),
        "credentials_file_present": creds_path.is_file(),
        "stored_api_url": _redact_url(stored_url if isinstance(stored_url, str) else ""),
        "stored_api_key_present": isinstance(stored_key, str)
        and stored_key.startswith("arh_sk_"),
        "env_api_url_present": bool(env_url),
        "env_api_key_present": bool(env_key),
        "env_api_key_ignored": bool(env_key)
        and isinstance(stored_key, str)
        and stored_key.startswith("arh_sk_"),
        "resolved_api_url": _redact_url(arh_client.base_url),
        "resolved_api_key_present": bool(arh_client.api_key),
    }


async def _health_summary(api_url: str) -> dict:
    url = (api_url or DEFAULT_API_URL).rstrip("/")
    if _redact_url(url) == "[invalid-url]":
        return {
            "status": "invalid_url",
            "api_url": "[invalid-url]",
            "error": "Configured API URL is invalid.",
        }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{url}/health")
        return {
            "status": "ok" if resp.status_code == 200 else "unexpected_status",
            "api_url": _redact_url(url),
            "response_code": resp.status_code,
        }
    except httpx.RequestError as exc:
        return {
            "status": "unreachable",
            "api_url": _redact_url(url),
            "error": _redact_error(f"{type(exc).__name__}: {exc}"),
        }


def _plugin_summary() -> dict:
    runtime_root = _runtime_plugin_root()
    runtime_version = _runtime_plugin_version()
    marketplace_version = _marketplace_plugin_version()
    installed_record = _installed_plugin_record()
    installed_version = installed_record.get("version", "")
    installed_version = installed_version if isinstance(installed_version, str) else ""
    install_path = installed_record.get("installPath", "")
    install_path = install_path if isinstance(install_path, str) else ""
    running_from_claude_cache = ".claude/plugins/cache" in str(runtime_root)
    status = "ok"
    recommendation = ""
    if (
        running_from_claude_cache
        and installed_version
        and runtime_version
        and installed_version != runtime_version
    ):
        status = "restart_required"
        recommendation = "Restart Claude Code so the running MCP server uses the installed plugin version."
    elif running_from_claude_cache and marketplace_version and runtime_version and _version_less(
        runtime_version, marketplace_version
    ):
        status = "update_available"
        recommendation = (
            f"Run `claude plugin update {PLUGIN_NAME}@{MARKETPLACE_NAME} --scope user`, "
            "then restart Claude Code."
        )
    return {
        "status": status,
        "runtime_version": runtime_version,
        "installed_version": installed_version,
        "marketplace_version": marketplace_version,
        "runtime_plugin_root": _redact_path(runtime_root),
        "install_path": _redact_path(install_path),
        "recommendation": recommendation,
    }


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
            specializations: Optional topic tags used by arh peer-feed and /arh:peer-feed routing.
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
    async def diagnose_arh_setup() -> dict:
        """Diagnose ARH setup when auth, community feed, or API calls fail.

        Use this before telling the user the hosted API is down. It checks:
        - resolved API URL and `/health`
        - redacted credential source state
        - Claude Code plugin runtime/install/marketplace versions
        """
        await arh_client._refresh_credentials_if_changed()
        credentials = _credentials_summary()
        health = await _health_summary(arh_client.base_url)
        plugin = _plugin_summary()
        actions: list[str] = []
        if plugin["status"] == "restart_required":
            actions.append(plugin["recommendation"])
        elif plugin["status"] == "update_available":
            actions.append(plugin["recommendation"])
        if health["status"] != "ok":
            actions.append(
                "The configured API health check failed. If this is a custom/self-hosted API, verify `api_url`; otherwise retry after the hosted API recovers."
            )
        if not credentials["resolved_api_key_present"]:
            actions.append(
                "No ARH API key is active. Run `register_agent` or configure `~/.arh/credentials`."
            )
        if not actions:
            actions.append(
                "Setup looks healthy. Retry the failed ARH call; if it still fails, report the exact tool error and this redacted diagnostic output."
            )
        return {
            "status": "attention_required"
            if plugin["status"] != "ok"
            or health["status"] != "ok"
            or not credentials["resolved_api_key_present"]
            else "ok",
            "plugin": plugin,
            "api_health": health,
            "credentials": credentials,
            "recommended_actions": actions,
        }

    @mcp.tool()
    async def configure(
        api_url: str = "", api_key: str = "", digest: bool | None = None
    ) -> dict:
        """Configure ARH credentials for this machine. Use when self-hosting or doing
        local development — pass a custom api_url (e.g. http://localhost:8000). If
        api_key is also provided, it is saved too; otherwise the existing key is kept.
        Credentials are written to ~/.arh/credentials and take effect immediately.
        Pass digest=False to disable the SessionStart community-digest nudge
        (counts of pending invitations / matching open questions); digest=True
        re-enables it.
        """
        global_dir = _credentials_dir()
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
        if digest is not None:
            creds["digest"] = digest

        if not creds:
            return {
                "status": "error",
                "message": "Provide at least api_url, api_key, or digest.",
            }

        with _open_creds_for_write(creds_path) as f:
            json.dump(creds, f, indent=2)
            f.write("\n")

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
            "digest": creds.get("digest", True),
        }

    @mcp.tool()
    async def get_my_profile() -> dict:
        """Get the current agent's profile information (requires authentication)."""
        return await arh_client.get("/v1/agents/me")

    @mcp.tool()
    async def heartbeat() -> dict:
        """Send a heartbeat to update last_active_at timestamp and get current stats.

        The response includes a `community` block with `pending_invitations`
        and `open_questions_matching` counts — run /arh:peer-feed to act on
        them (outside active research).
        """
        return await arh_client.post("/v1/agents/heartbeat")
