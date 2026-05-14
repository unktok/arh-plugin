import json
import os

import httpx

DEFAULT_API_URL = "https://api.airesearcherhub.com"


def _valid_api_key(value: str) -> bool:
    return value.startswith("arh_sk_") and "${" not in value


class ARHApiError(Exception):
    """Error from the AI Researcher Hub API with a safe user-facing message."""

    def __init__(self, user_message: str, status_code: int | None = None):
        self.user_message = user_message
        self.status_code = status_code
        super().__init__(user_message)


_STATUS_MESSAGES = {
    401: (
        "Authentication failed. Check ~/.arh/credentials, or ARH_API_KEY when "
        "running in an environment-only setup."
    ),
    403: "Permission denied for this operation.",
    404: "Resource not found.",
    409: "Conflict — the resource may have been modified.",
    422: "Invalid request data.",
    429: "Rate limited. Please try again later.",
}


def _safe_message(status_code: int) -> str:
    if status_code in _STATUS_MESSAGES:
        return _STATUS_MESSAGES[status_code]
    if 400 <= status_code < 500:
        return f"Request failed ({status_code})."
    return "AI Researcher Hub API returned an internal error."


class ARHClient:
    """HTTP client wrapper for the AI Researcher Hub REST API."""

    def __init__(self):
        self.base_url = DEFAULT_API_URL
        self.api_key = ""
        self._client: httpx.AsyncClient | None = None
        self._load_credentials()

    def _read_credentials_file(self) -> dict:
        creds_path = os.path.expanduser("~/.arh/credentials")
        try:
            with open(creds_path) as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _resolve_credentials(self) -> tuple[str, str]:
        """Resolve API URL/key as a bound pair.

        Stored credentials are the local source of truth. Ambient environment
        variables are fallback-only so stale launcher env cannot shadow a fresh
        registration or redirect a stored key to a different API URL.
        """
        creds = self._read_credentials_file()
        stored_key = creds.get("api_key", "")
        stored_url = creds.get("api_url", DEFAULT_API_URL) or DEFAULT_API_URL
        if isinstance(stored_key, str) and _valid_api_key(stored_key):
            return str(stored_url), stored_key

        env_key = os.environ.get("ARH_API_KEY", "")
        env_url = os.environ.get("ARH_API_URL", stored_url) or stored_url
        if _valid_api_key(env_key):
            return env_url, env_key
        return env_url, ""

    def _load_credentials(self) -> None:
        self.base_url, self.api_key = self._resolve_credentials()

    async def _refresh_credentials_if_changed(self) -> None:
        """Refresh credentials that may have changed after MCP server startup.

        Claude Code can keep the MCP process alive while setup/CLI commands
        update ~/.arh/credentials in a separate process. Re-resolving before
        requests keeps MCP behavior aligned with the CLI and prevents a stale
        ARH_API_KEY inherited at server startup from shadowing fresh stored
        credentials.
        """
        base_url, api_key = self._resolve_credentials()
        if base_url == self.base_url and api_key == self.api_key:
            return

        self.base_url = base_url
        self.api_key = api_key
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            headers = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
                timeout=30.0,
            )
        return self._client

    async def get(self, path: str, params: dict | None = None) -> dict:
        try:
            await self._refresh_credentials_if_changed()
            resp = await self.client.get(path, params=params)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            raise ARHApiError(
                _safe_message(exc.response.status_code),
                status_code=exc.response.status_code,
            ) from None
        except httpx.RequestError:
            raise ARHApiError(
                "Failed to connect to AI Researcher Hub API."
            ) from None

    async def post(
        self,
        path: str,
        json: dict | None = None,
        data: dict | None = None,
        files: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        try:
            await self._refresh_credentials_if_changed()
            resp = await self.client.post(path, json=json, data=data, files=files, params=params)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            raise ARHApiError(
                _safe_message(exc.response.status_code),
                status_code=exc.response.status_code,
            ) from None
        except httpx.RequestError:
            raise ARHApiError(
                "Failed to connect to AI Researcher Hub API."
            ) from None

    async def patch(self, path: str, json: dict | None = None) -> dict:
        try:
            await self._refresh_credentials_if_changed()
            resp = await self.client.patch(path, json=json)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            raise ARHApiError(
                _safe_message(exc.response.status_code),
                status_code=exc.response.status_code,
            ) from None
        except httpx.RequestError:
            raise ARHApiError(
                "Failed to connect to AI Researcher Hub API."
            ) from None

    async def delete(self, path: str) -> None:
        try:
            await self._refresh_credentials_if_changed()
            resp = await self.client.delete(path)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ARHApiError(
                _safe_message(exc.response.status_code),
                status_code=exc.response.status_code,
            ) from None
        except httpx.RequestError:
            raise ARHApiError(
                "Failed to connect to AI Researcher Hub API."
            ) from None

    def reset_auth(self, api_key: str = "", api_url: str = "") -> None:
        """Update credentials and reset the HTTP client."""
        if api_key or api_url:
            if not api_key:
                _, api_key = self._resolve_credentials()
            if not api_url:
                api_url = self.base_url
            self.api_key = api_key
            self.base_url = api_url
        else:
            self._load_credentials()
        if self._client is not None and not self._client.is_closed:
            self._client = None


arh_client = ARHClient()
