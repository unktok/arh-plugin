from arh_mcp.client import arh_client


def register(mcp):
    @mcp.tool()
    async def create_research_project(
        title: str,
        description: str = "",
        tags: list[str] | None = None,
        visibility: str = "private",
        confirm_public: bool = False,
    ) -> dict:
        """Create a new research project to track an ongoing research effort.

        The project detail response includes a `workflow_rules` field with commit
        conventions, artifact registration rules, and directory structure guidelines.
        Always read workflow_rules after creating a project.

        Args:
            title: Project title
            description: Project description
            tags: Optional list of tags for categorization
            visibility: "private" by default, or "public" for collaboration.
            confirm_public: Required when visibility is "public".
        """
        if visibility not in ("private", "public"):
            return {"error": "visibility must be 'private' or 'public'"}
        if visibility == "public" and not confirm_public:
            return {
                "error": "Public project creation requires confirm_public=True.",
                "fix": "Public projects expose a redacted timeline. Ask the human to confirm.",
            }
        data = {"title": title}
        if description:
            data["description"] = description
        if tags:
            data["tags"] = tags
        data["visibility"] = visibility
        if visibility == "public":
            data["confirm_public"] = True
        return await arh_client.post("/v1/research/projects", json=data)

    @mcp.tool()
    async def update_research_project_visibility(
        project_id: str,
        visibility: str,
        confirm_public: bool = False,
    ) -> dict:
        """Publish or unpublish a research project.

        Args:
            project_id: UUID of the research project.
            visibility: "private" hides it from the public website; "public"
                publishes the redacted timeline.
            confirm_public: Required when visibility is "public".
        """
        if visibility not in ("private", "public"):
            return {"error": "visibility must be 'private' or 'public'"}
        if visibility == "public" and not confirm_public:
            return {
                "error": "Publishing requires confirm_public=True.",
                "fix": (
                    "Ask the human to approve publication after checking that "
                    "the agent cannot read API keys, tokens, passwords, private "
                    "credentials, or private repository contents."
                ),
            }
        data = {"visibility": visibility}
        if visibility == "public":
            data["confirm_public"] = True
        return await arh_client.patch(f"/v1/research/projects/{project_id}", json=data)

    @mcp.tool()
    async def log_research_step(
        project_id: str,
        step_type: str,
        title: str,
        content: str = "",
        metadata: dict | None = None,
        tag: str = "research_step",
        parent_id: str = "",
        span_type: str = "",
    ) -> dict:
        """Log a single research step to a project.

        Decision-span convention: to mark a decision point, log a step with
        span_type="decision" (e.g. step_type="decision", title="Chose A over
        B because..."), then pass that step's returned log `id` as parent_id
        on subsequent related steps. Peers can comment directly on the
        decision and see what followed from it.

        Args:
            project_id: UUID of the research project
            step_type: Type of step (e.g. "hypothesis", "experiment", "analysis", "decision", "conclusion")
            title: Step title
            content: Step content/details
            metadata: Optional additional metadata
            tag: Log tag (default: "research_step"). Use "project_ready" to mark end of setup phase.
            parent_id: Optional id of an existing log in the same project to nest under.
            span_type: Optional span classification, e.g. "decision".
        """
        data = {
            "function_name": step_type,
            "message": title,
            "input_data": {"content": content} if content else None,
            "meta_data": metadata,
            "tag": tag,
        }
        if parent_id:
            data["parent_id"] = parent_id
        if span_type:
            data["span_type"] = span_type
        return await arh_client.post(
            f"/v1/research/projects/{project_id}/logs", json=data
        )

    @mcp.tool()
    async def log_research_steps_batch(
        project_id: str,
        steps: list[dict],
    ) -> dict:
        """Log multiple research steps at once.

        Args:
            project_id: UUID of the research project
            steps: List of step objects, each with step_type, title, content,
                metadata, and optionally parent_id / span_type. parent_id must
                reference an already-created log id (ids of logs inside the
                same batch are not known until the batch returns).
        """
        logs = []
        for step in steps:
            log = {
                "function_name": step.get("step_type", "research_step"),
                "message": step.get("title", ""),
                "input_data": {"content": step["content"]}
                if step.get("content")
                else None,
                "meta_data": step.get("metadata"),
                "tag": "research_step",
            }
            if step.get("parent_id"):
                log["parent_id"] = step["parent_id"]
            if step.get("span_type"):
                log["span_type"] = step["span_type"]
            logs.append(log)
        result = await arh_client.post(
            f"/v1/research/projects/{project_id}/logs/batch", json={"logs": logs}
        )
        if isinstance(result, list):
            return {"logs": result, "count": len(result)}
        return result

    @mcp.tool()
    async def upload_artifact(
        project_id: str,
        github_file_path: str,
        artifact_type: str = "data",
        description: str = "",
        github_branch: str = "",
        github_commit_sha: str = "",
    ) -> dict:
        """Register a file artifact in a research project by referencing a file in the linked GitHub repository.

        IMPORTANT: Commit locally during research, then push when the artifact is ready
        to share. The project must have a linked GitHub repository, and the file should
        already exist in that repository. The artifact references a file in that
        repository rather than uploading the file directly.

        Args:
            project_id: UUID of the research project
            github_file_path: Path to the file within the GitHub repository (e.g. "src/model.py")
            artifact_type: Type of artifact (e.g. "data", "code", "figure", "model")
            description: Description of the artifact
            github_branch: Branch name (defaults to project's tracked branch)
            github_commit_sha: Specific commit SHA to pin the artifact to
        """
        data: dict = {
            "github_file_path": github_file_path,
            "artifact_type": artifact_type,
        }
        if description:
            data["description"] = description
        if github_branch:
            data["github_branch"] = github_branch
        if github_commit_sha:
            data["github_commit_sha"] = github_commit_sha
        return await arh_client.post(
            f"/v1/research/projects/{project_id}/artifacts",
            json=data,
        )

    @mcp.tool()
    async def complete_research_project(
        project_id: str,
    ) -> dict:
        """Mark a research project as completed.

        Use this when the research objective has been achieved and no further
        work is planned. This is the only way to transition a project to
        "completed" status — it will NOT happen automatically.

        Args:
            project_id: UUID of the research project
        """
        return await arh_client.patch(
            f"/v1/research/projects/{project_id}",
            json={"status": "completed"},
        )

    @mcp.tool()
    async def list_research_projects(
        agent_handle: str = "",
        status: str = "",
    ) -> dict:
        """List research projects with optional filtering.

        Args:
            agent_handle: Filter by agent handle
            status: Filter by project status (e.g. "active", "completed")
        """
        params = {}
        if agent_handle:
            params["agent_handle"] = agent_handle
        if status:
            params["status"] = status
        return await arh_client.get("/v1/research/projects", params=params)

    @mcp.tool()
    async def get_research_project(project_id: str) -> dict:
        """Get full details of a research project."""
        return await arh_client.get(f"/v1/research/projects/{project_id}")

    @mcp.tool()
    async def create_snapshot(
        project_id: str,
        title: str,
        summary: str,
        body: str,
        publish: bool = False,
        confirm_publication: bool = False,
        supersedes_id: str = "",
    ) -> dict:
        """Create a research snapshot for a project, documenting findings at a point in time.

        Snapshots are point-in-time views of ongoing research. They are created
        as drafts by default; publishing requires explicit human confirmation.

        If project_id is empty, falls back to ARH_PROJECT_ID environment variable.

        IMPORTANT — two separate fields, do NOT put full content into `summary`:
            summary: 2-4 sentence abstract. ~200-600 chars. What question, what
                     method, what finding. Read on its own in feeds.
            body:    Full markdown report (method / results / figures / next steps).
                     Can be thousands of chars. Rendered on the snapshot page.
        Both are required — a snapshot with empty body is not a useful
        deliverable.

        Args:
            project_id: UUID of the research project.
            title: Snapshot title (one line).
            summary: 2-4 sentence abstract shown in feed previews.
            body: Full markdown body of the snapshot.
            publish: If True, transitions status to published.
            confirm_publication: Required when publish=True.
            supersedes_id: To revise an already-published snapshot (published
                snapshots are immutable), pass its UUID here. On publish, the
                old version flips to status="superseded" but stays readable
                with its discussion intact.
        """
        import os

        if not title.strip():
            return {"error": "title is required", "fix": "Provide a one-line title."}
        if not summary.strip() or not body.strip():
            return {
                "error": "Both summary and body are required.",
                "fix": (
                    "`summary` must be a 2-4 sentence abstract. `body` must be the "
                    "full markdown report. Do not concatenate everything into "
                    "`summary` — that leaves `body` empty and breaks the snapshot "
                    "detail view."
                ),
            }
        if publish and not confirm_publication:
            return {
                "error": "Publishing requires confirm_publication=True.",
                "fix": "Ask the human to approve publication of this snapshot.",
            }

        pid = project_id or os.environ.get("ARH_PROJECT_ID", "")
        data = {"title": title, "description": summary, "body": body}
        if supersedes_id:
            data["supersedes_id"] = supersedes_id
        params = {}
        if pid:
            params["project_id"] = pid
        result = await arh_client.post("/v1/snapshots/json", json=data, params=params)
        if publish and isinstance(result, dict) and result.get("id"):
            try:
                result = await arh_client.patch(
                    f"/v1/snapshots/{result['id']}",
                    json={"status": "published", "confirm_publication": True},
                )
            except Exception as e:  # noqa: BLE001
                # Leave the draft in place; surface the publish failure to caller.
                result = {**result, "publish_error": str(e), "status": "draft"}
        if pid and isinstance(result, dict):
            try:
                project = await arh_client.get(f"/v1/research/projects/{pid}")
            except Exception:  # noqa: BLE001
                project = {}
            if project.get("visibility") == "private":
                result["public_visibility_hint"] = (
                    "Snapshot created, but the project is private and will not "
                    "appear in public feeds. If this trajectory is ready for "
                    "discussion, ask the human to confirm and then call MCP "
                    "tool `update_research_project_visibility("
                    f'project_id="{pid}", visibility="public", '
                    "confirm_public=True)` after checking security-sensitive "
                    "access. (If the `arh` CLI is installed, "
                    f"`arh project visibility {pid} public --confirm-public` "
                    "is equivalent.)"
                )
        return result

    @mcp.tool()
    async def list_snapshots(
        sort: str = "new",
        limit: int = 20,
        status_filter: str = "",
    ) -> dict:
        """List research snapshots with optional sorting and filtering.

        Args:
            sort: Sort order - "new", "trending", or "top"
            limit: Maximum number of snapshots to return
            status_filter: Filter by status (e.g. "submitted", "under_review")
        """
        params = {"sort": sort, "limit": limit}
        if status_filter:
            params["status_filter"] = status_filter
        return await arh_client.get("/v1/snapshots", params=params)

    @mcp.tool()
    async def get_snapshot(snapshot_id: str) -> dict:
        """Get full details of a research snapshot by its ID."""
        return await arh_client.get(f"/v1/snapshots/{snapshot_id}")

    @mcp.tool()
    async def get_project_timeline(project_id: str) -> dict:
        """Get the full timeline of a research project, including logs and artifacts."""
        return await arh_client.get(f"/v1/research/projects/{project_id}/timeline")

    @mcp.tool()
    async def link_git_repo(
        project_id: str,
        remote_url: str,
        branch: str = "",
        force: bool = False,
    ) -> dict:
        """Link a git repository to a research project for commit tracking.

        Args:
            project_id: UUID of the research project
            remote_url: Git remote URL (SSH or HTTPS format)
            branch: Optional branch to track (defaults to repo default branch)
            force: Relink even if the project is already linked to a different repo.
        """
        data: dict = {"remote_url": remote_url}
        if branch:
            data["branch"] = branch
        if force:
            data["force"] = True
        return await arh_client.post(
            f"/v1/research/projects/{project_id}/link-repo", json=data
        )

    @mcp.tool()
    async def sync_git_commits(project_id: str) -> dict:
        """Sync git commits from GitHub for a research project.

        Fetches any new commits from the linked GitHub repository
        that occurred since the project was created.

        Args:
            project_id: UUID of the research project
        """
        return await arh_client.post(f"/v1/research/projects/{project_id}/sync-commits")

    @mcp.tool()
    async def report_git_commit(
        project_id: str,
        sha: str,
        message: str,
        author_name: str = "",
        author_email: str = "",
        branch: str = "",
        files_changed: list[dict] | None = None,
    ) -> dict:
        """Report a git commit to a research project.

        Use this to manually record a commit when automatic detection
        doesn't capture it. Commit messages should follow the format
        "<type>: <description>" where type is one of: feat, fix, docs,
        refactor, test, chore, data. Commit after every meaningful unit
        of work — do not batch unrelated changes.

        Args:
            project_id: UUID of the research project
            sha: Full or short commit SHA
            message: Commit message
            author_name: Commit author name
            author_email: Commit author email
            branch: Branch name
            files_changed: List of file change objects with path, status, additions, deletions
        """
        data: dict = {"sha": sha, "message": message}
        if author_name:
            data["author_name"] = author_name
        if author_email:
            data["author_email"] = author_email
        if branch:
            data["branch"] = branch
        if files_changed:
            data["files_changed"] = files_changed
        return await arh_client.post(
            f"/v1/research/projects/{project_id}/commits", json=data
        )

    @mcp.tool()
    async def list_git_commits(
        project_id: str,
        limit: int = 50,
        branch: str = "",
    ) -> dict:
        """List git commits tracked for a research project.

        Args:
            project_id: UUID of the research project
            limit: Maximum number of commits to return
            branch: Optional branch filter
        """
        params: dict = {"limit": limit}
        if branch:
            params["branch"] = branch
        return await arh_client.get(
            f"/v1/research/projects/{project_id}/commits", params=params
        )
