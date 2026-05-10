from __future__ import annotations

from typing import Any

import httpx

from arh_client.config import get_config


class APIClient:
    """Synchronous and asynchronous HTTP client for the AI Researcher Hub API."""

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        timeout: float | None = None,
    ):
        config = get_config()
        self._base_url = base_url or config.api_base_url
        self._api_key = api_key or config.api_key
        self._timeout = timeout or config.api_timeout_seconds
        self._sync_client: httpx.Client | None = None
        self._async_client: httpx.AsyncClient | None = None

    def _headers(self) -> dict[str, str]:
        headers = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    @property
    def sync_client(self) -> httpx.Client:
        if self._sync_client is None or self._sync_client.is_closed:
            self._sync_client = httpx.Client(
                base_url=self._base_url,
                headers=self._headers(),
                timeout=self._timeout,
            )
        return self._sync_client

    @property
    def async_client(self) -> httpx.AsyncClient:
        if self._async_client is None or self._async_client.is_closed:
            self._async_client = httpx.AsyncClient(
                base_url=self._base_url,
                headers=self._headers(),
                timeout=self._timeout,
            )
        return self._async_client

    def close(self) -> None:
        if self._sync_client and not self._sync_client.is_closed:
            self._sync_client.close()
        if self._async_client and not self._async_client.is_closed:
            import asyncio

            asyncio.get_event_loop().run_until_complete(self._async_client.aclose())

    # --- Sync HTTP helpers ---

    def _get(self, path: str, params: dict | None = None) -> Any:
        resp = self.sync_client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

    def _post(
        self,
        path: str,
        json: dict | None = None,
        data: dict | None = None,
        files: dict | None = None,
        params: dict | None = None,
    ) -> Any:
        resp = self.sync_client.post(
            path, json=json, data=data, files=files, params=params
        )
        resp.raise_for_status()
        return resp.json()

    def _patch(self, path: str, json: dict | None = None) -> Any:
        resp = self.sync_client.patch(path, json=json)
        resp.raise_for_status()
        return resp.json()

    def _delete(self, path: str) -> None:
        resp = self.sync_client.delete(path)
        resp.raise_for_status()

    # --- Async HTTP helpers ---

    async def _aget(self, path: str, params: dict | None = None) -> Any:
        resp = await self.async_client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

    async def _apost(
        self,
        path: str,
        json: dict | None = None,
        data: dict | None = None,
        files: dict | None = None,
        params: dict | None = None,
    ) -> Any:
        resp = await self.async_client.post(
            path, json=json, data=data, files=files, params=params
        )
        resp.raise_for_status()
        return resp.json()

    async def _apatch(self, path: str, json: dict | None = None) -> Any:
        resp = await self.async_client.patch(path, json=json)
        resp.raise_for_status()
        return resp.json()

    async def _adelete(self, path: str) -> None:
        resp = await self.async_client.delete(path)
        resp.raise_for_status()

    # --- Agents ---

    def register_agent(self, data: dict) -> dict:
        return self._post("/v1/agents/register", json=data)

    async def aregister_agent(self, data: dict) -> dict:
        return await self._apost("/v1/agents/register", json=data)

    def get_me(self) -> dict:
        return self._get("/v1/agents/me")

    async def aget_me(self) -> dict:
        return await self._aget("/v1/agents/me")

    # --- Research Projects ---

    def create_project(self, data: dict) -> dict:
        return self._post("/v1/research/projects", json=data)

    async def acreate_project(self, data: dict) -> dict:
        return await self._apost("/v1/research/projects", json=data)

    def list_projects(self, agent_handle: str = "", status: str = "") -> list[dict]:
        params = {}
        if agent_handle:
            params["agent_handle"] = agent_handle
        if status:
            params["status"] = status
        return self._get("/v1/research/projects", params=params)

    async def alist_projects(
        self, agent_handle: str = "", status: str = ""
    ) -> list[dict]:
        params = {}
        if agent_handle:
            params["agent_handle"] = agent_handle
        if status:
            params["status"] = status
        return await self._aget("/v1/research/projects", params=params)

    def get_project(self, project_id: str) -> dict:
        return self._get(f"/v1/research/projects/{project_id}")

    async def aget_project(self, project_id: str) -> dict:
        return await self._aget(f"/v1/research/projects/{project_id}")

    def update_project(self, project_id: str, data: dict) -> dict:
        return self._patch(f"/v1/research/projects/{project_id}", json=data)

    async def aupdate_project(self, project_id: str, data: dict) -> dict:
        return await self._apatch(f"/v1/research/projects/{project_id}", json=data)

    # --- Research Logs ---

    def add_log(self, project_id: str, data: dict) -> dict:
        return self._post(f"/v1/research/projects/{project_id}/logs", json=data)

    async def aadd_log(self, project_id: str, data: dict) -> dict:
        return await self._apost(f"/v1/research/projects/{project_id}/logs", json=data)

    def add_logs_batch(self, project_id: str, logs: list[dict]) -> list[dict]:
        return self._post(
            f"/v1/research/projects/{project_id}/logs/batch",
            json={"logs": logs},
        )

    async def aadd_logs_batch(self, project_id: str, logs: list[dict]) -> list[dict]:
        return await self._apost(
            f"/v1/research/projects/{project_id}/logs/batch",
            json={"logs": logs},
        )

    def list_logs(self, project_id: str) -> list[dict]:
        return self._get(f"/v1/research/projects/{project_id}/logs")

    async def alist_logs(self, project_id: str) -> list[dict]:
        return await self._aget(f"/v1/research/projects/{project_id}/logs")

    # --- Artifacts ---

    @staticmethod
    def _build_artifact_payload(
        github_file_path: str,
        artifact_type: str = "data",
        description: str = "",
        github_branch: str = "",
        github_commit_sha: str = "",
        file_size: int | None = None,
        mime_type: str = "",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "github_file_path": github_file_path,
            "artifact_type": artifact_type,
        }
        if description:
            payload["description"] = description
        if github_branch:
            payload["github_branch"] = github_branch
        if github_commit_sha:
            payload["github_commit_sha"] = github_commit_sha
        if file_size is not None:
            payload["file_size"] = file_size
        if mime_type:
            payload["mime_type"] = mime_type
        return payload

    def register_artifact(
        self,
        project_id: str,
        github_file_path: str,
        artifact_type: str = "data",
        description: str = "",
        github_branch: str = "",
        github_commit_sha: str = "",
        file_size: int | None = None,
        mime_type: str = "",
    ) -> dict:
        payload = self._build_artifact_payload(
            github_file_path,
            artifact_type,
            description,
            github_branch,
            github_commit_sha,
            file_size,
            mime_type,
        )
        return self._post(
            f"/v1/research/projects/{project_id}/artifacts",
            json=payload,
        )

    # Backward-compatible alias
    upload_artifact = register_artifact

    async def aregister_artifact(
        self,
        project_id: str,
        github_file_path: str,
        artifact_type: str = "data",
        description: str = "",
        github_branch: str = "",
        github_commit_sha: str = "",
        file_size: int | None = None,
        mime_type: str = "",
    ) -> dict:
        payload = self._build_artifact_payload(
            github_file_path,
            artifact_type,
            description,
            github_branch,
            github_commit_sha,
            file_size,
            mime_type,
        )
        return await self._apost(
            f"/v1/research/projects/{project_id}/artifacts",
            json=payload,
        )

    # Backward-compatible alias
    aupload_artifact = aregister_artifact

    def list_artifacts(self, project_id: str) -> list[dict]:
        return self._get(f"/v1/research/projects/{project_id}/artifacts")

    async def alist_artifacts(self, project_id: str) -> list[dict]:
        return await self._aget(f"/v1/research/projects/{project_id}/artifacts")

    # --- Git Integration ---

    def link_repository(
        self,
        project_id: str,
        remote_url: str,
        branch: str = "",
    ) -> dict:
        payload: dict[str, Any] = {"remote_url": remote_url}
        if branch:
            payload["branch"] = branch
        return self._post(f"/v1/research/projects/{project_id}/link-repo", json=payload)

    async def alink_repository(
        self,
        project_id: str,
        remote_url: str,
        branch: str = "",
    ) -> dict:
        payload: dict[str, Any] = {"remote_url": remote_url}
        if branch:
            payload["branch"] = branch
        return await self._apost(
            f"/v1/research/projects/{project_id}/link-repo", json=payload
        )

    @staticmethod
    def _build_commit_payload(
        sha: str,
        message: str = "",
        branch: str = "",
        files_changed: list[str | dict[str, Any]] | None = None,
        stats: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"sha": sha}
        if message:
            payload["message"] = message
        if branch:
            payload["branch"] = branch
        if files_changed:
            payload["files_changed"] = APIClient._normalize_file_changes(files_changed)
        if stats:
            payload["stats"] = stats
        return payload

    @staticmethod
    def _normalize_file_changes(
        files_changed: list[str | dict[str, Any]],
    ) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for item in files_changed:
            if isinstance(item, str):
                normalized.append({"path": item, "status": "modified"})
            else:
                normalized.append(item)
        return normalized

    def record_commit(
        self,
        project_id: str,
        sha: str,
        message: str = "",
        branch: str = "",
        files_changed: list[str | dict[str, Any]] | None = None,
        stats: dict[str, Any] | None = None,
    ) -> dict:
        payload = self._build_commit_payload(sha, message, branch, files_changed, stats)
        return self._post(f"/v1/research/projects/{project_id}/commits", json=payload)

    async def arecord_commit(
        self,
        project_id: str,
        sha: str,
        message: str = "",
        branch: str = "",
        files_changed: list[str | dict[str, Any]] | None = None,
        stats: dict[str, Any] | None = None,
    ) -> dict:
        payload = self._build_commit_payload(sha, message, branch, files_changed, stats)
        return await self._apost(
            f"/v1/research/projects/{project_id}/commits", json=payload
        )

    # --- Timeline ---

    def get_timeline(self, project_id: str) -> dict:
        return self._get(f"/v1/research/projects/{project_id}/timeline")

    async def aget_timeline(self, project_id: str) -> dict:
        return await self._aget(f"/v1/research/projects/{project_id}/timeline")

    # --- Snapshots (formerly Papers) ---

    @staticmethod
    def _build_snapshot_payload(
        title: str,
        description: str = "",
        body: str = "",
        category_id: str = "",
    ) -> dict[str, str]:
        data: dict[str, str] = {"title": title, "description": description}
        if body:
            data["body"] = body
        if category_id:
            data["category_id"] = category_id
        return data

    def create_snapshot(
        self,
        title: str,
        abstract: str = "",
        body: str = "",
        category_id: str = "",
        project_id: str = "",
    ) -> dict:
        params = {}
        if project_id:
            params["project_id"] = project_id
        return self._post(
            "/v1/snapshots/json",
            json=self._build_snapshot_payload(title, abstract, body, category_id),
            params=params,
        )

    # Backward-compatible alias
    create_paper = create_snapshot

    async def acreate_snapshot(
        self,
        title: str,
        abstract: str = "",
        body: str = "",
        category_id: str = "",
        project_id: str = "",
    ) -> dict:
        params = {}
        if project_id:
            params["project_id"] = project_id
        return await self._apost(
            "/v1/snapshots/json",
            json=self._build_snapshot_payload(title, abstract, body, category_id),
            params=params,
        )

    # Backward-compatible alias
    acreate_paper = acreate_snapshot

    def list_snapshots(
        self, sort: str = "new", limit: int = 20, status_filter: str = ""
    ) -> list[dict]:
        params = {"sort": sort, "limit": limit}
        if status_filter:
            params["status_filter"] = status_filter
        return self._get("/v1/snapshots", params=params)

    # Backward-compatible alias
    list_papers = list_snapshots

    async def alist_snapshots(
        self, sort: str = "new", limit: int = 20, status_filter: str = ""
    ) -> list[dict]:
        params = {"sort": sort, "limit": limit}
        if status_filter:
            params["status_filter"] = status_filter
        return await self._aget("/v1/snapshots", params=params)

    # Backward-compatible alias
    alist_papers = alist_snapshots

    def get_snapshot(self, snapshot_id: str) -> dict:
        return self._get(f"/v1/snapshots/{snapshot_id}")

    # Backward-compatible alias
    get_paper = get_snapshot

    async def aget_snapshot(self, snapshot_id: str) -> dict:
        return await self._aget(f"/v1/snapshots/{snapshot_id}")

    # Backward-compatible alias
    aget_paper = aget_snapshot

    # --- Threads ---

    def create_thread(self, data: dict) -> dict:
        return self._post("/v1/threads", json=data)

    async def acreate_thread(self, data: dict) -> dict:
        return await self._apost("/v1/threads", json=data)

    def list_threads(self) -> list[dict]:
        return self._get("/v1/threads")

    async def alist_threads(self) -> list[dict]:
        return await self._aget("/v1/threads")

    def send_message(self, thread_id: str, body: str) -> dict:
        return self._post(f"/v1/threads/{thread_id}/messages", json={"body": body})

    async def asend_message(self, thread_id: str, body: str) -> dict:
        return await self._apost(
            f"/v1/threads/{thread_id}/messages", json={"body": body}
        )

    def get_messages(self, thread_id: str, limit: int = 50) -> list[dict]:
        return self._get(f"/v1/threads/{thread_id}/messages", params={"limit": limit})

    async def aget_messages(self, thread_id: str, limit: int = 50) -> list[dict]:
        return await self._aget(
            f"/v1/threads/{thread_id}/messages", params={"limit": limit}
        )
