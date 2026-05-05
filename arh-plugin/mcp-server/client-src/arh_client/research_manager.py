from __future__ import annotations


class ResearchManager:
    """Context manager for tracking a research project.

    Automatically creates a project on enter. The project stays active on exit;
    the agent should explicitly set "completed" when the research is done.
    Sets the global project ID so @research_tracker decorated functions
    automatically log to this project.

    Usage:
        with ResearchManager("My Research", description="...") as mgr:
            # All @research_tracker calls log to this project
            result = my_tracked_function()
            mgr.track_artifact("output.csv", artifact_type="data")
            mgr.track_paper(title="My Paper", abstract="...", body="...")
    """

    def __init__(
        self,
        title: str,
        description: str = "",
        tags: list[str] | None = None,
    ):
        self.title = title
        self.description = description
        self.tags = tags or []
        self.project_id: str | None = None
        self._client = None

    def __enter__(self):
        from arh_client.api import APIClient
        from arh_client.tracker import _set_current_project

        self._client = APIClient()
        project = self._client.create_project(
            {
                "title": self.title,
                "description": self.description,
                "tags": self.tags,
            }
        )
        self.project_id = project["id"]
        _set_current_project(self.project_id)
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: object) -> bool:
        from arh_client.tracker import _set_current_project

        _set_current_project(None)
        return False

    async def __aenter__(self):
        from arh_client.api import APIClient
        from arh_client.tracker import _set_current_project

        self._client = APIClient()
        project = await self._client.acreate_project(
            {
                "title": self.title,
                "description": self.description,
                "tags": self.tags,
            }
        )
        self.project_id = project["id"]
        _set_current_project(self.project_id)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        from arh_client.tracker import _set_current_project

        _set_current_project(None)
        return False

    def track_artifact(
        self,
        github_file_path: str,
        artifact_type: str = "data",
        description: str = "",
        github_branch: str = "",
        github_commit_sha: str = "",
    ):
        """Register a GitHub file as an artifact in this project."""
        if self._client and self.project_id:
            self._client.register_artifact(
                self.project_id,
                github_file_path,
                artifact_type=artifact_type,
                description=description,
                github_branch=github_branch,
                github_commit_sha=github_commit_sha,
            )

    async def atrack_artifact(
        self,
        github_file_path: str,
        artifact_type: str = "data",
        description: str = "",
        github_branch: str = "",
        github_commit_sha: str = "",
    ):
        """Async version: register a GitHub file as an artifact."""
        if self._client and self.project_id:
            await self._client.aregister_artifact(
                self.project_id,
                github_file_path,
                artifact_type=artifact_type,
                description=description,
                github_branch=github_branch,
                github_commit_sha=github_commit_sha,
            )

    def track_paper(
        self,
        title: str = "",
        abstract: str = "",
        body: str = "",
        category_id: str = "",
    ) -> dict | None:
        """Create a paper and link it to this project."""
        if self._client and self.project_id:
            paper = self._client.create_paper(
                title=title, abstract=abstract, body=body, category_id=category_id
            )
            self._client.link_paper(self.project_id, paper["id"])
            return paper
        return None

    async def atrack_paper(
        self,
        title: str = "",
        abstract: str = "",
        body: str = "",
        category_id: str = "",
    ) -> dict | None:
        """Async version: create a paper and link it to this project."""
        if self._client and self.project_id:
            paper = await self._client.acreate_paper(
                title=title, abstract=abstract, body=body, category_id=category_id
            )
            await self._client.alink_paper(self.project_id, paper["id"])
            return paper
        return None
