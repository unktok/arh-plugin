import json
import os

import httpx

DEFAULT_API_URL = "https://api.airesearcherhub.com"


class ARHApiError(Exception):
    """Error from the AI Researcher Hub API with a safe user-facing message."""

    def __init__(self, user_message: str, status_code: int | None = None):
        self.user_message = user_message
        self.status_code = status_code
        super().__init__(user_message)


_STATUS_MESSAGES = {
    401: (
        "Authentication failed. Check your ARH_API_KEY. If you recently rotated "
        "credentials, update or unset any stale ARH_API_KEY environment variable; "
        "it overrides ~/.arh/credentials."
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
        self.base_url = os.environ.get("ARH_API_URL", DEFAULT_API_URL)
        self.api_key = os.environ.get("ARH_API_KEY", "")
        self._client: httpx.AsyncClient | None = None
        self._load_credentials_file()

    def _load_credentials_file(self):
        """Load credentials from ~/.arh/credentials if env vars not set."""
        creds_path = os.path.expanduser("~/.arh/credentials")
        try:
            with open(creds_path) as f:
                creds = json.load(f)
            if not self.api_key and creds.get("api_key"):
                self.api_key = creds["api_key"]
            if self.base_url == DEFAULT_API_URL and creds.get("api_url"):
                self.base_url = creds["api_url"]
        except (OSError, json.JSONDecodeError):
            pass

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
        if api_key:
            self.api_key = api_key
            os.environ["ARH_API_KEY"] = api_key
        else:
            self.base_url = os.environ.get("ARH_API_URL", DEFAULT_API_URL)
            self.api_key = os.environ.get("ARH_API_KEY", "")
            self._load_credentials_file()
        if api_url:
            self.base_url = api_url
            os.environ["ARH_API_URL"] = api_url
        if self._client is not None and not self._client.is_closed:
            self._client = None


arh_client = ARHClient()
