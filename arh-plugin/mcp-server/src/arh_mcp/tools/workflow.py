import asyncio
import subprocess
from pathlib import Path

from arh_mcp.client import arh_client


def register(mcp):
    @mcp.tool()
    async def checkpoint(
        project_id: str,
        summary: str,
        commit: bool = True,
        commit_message: str | None = None,
        tag: str = "checkpoint",
        artifact_paths: list[str] | None = None,
        artifact_type: str = "code",
        cwd: str | None = None,
    ) -> dict:
        """Mark a progress checkpoint: commit current work, push, log it, and optionally curate artifacts.

        Call this after any tool-chain that produced a tracked file change. This is the
        preferred way to commit research work — it keeps the project timeline coherent by
        recording a log entry alongside the commit. Do NOT use bare `git commit` for research.

        Args:
            project_id: UUID of the active research project.
            summary: One short sentence — what just got done. Used as the log title
                     and (if commit_message is None) as the git commit message.
            commit: If True (default), runs `git add -A && git commit -m ... && git push`
                    in `cwd`. Set False if the caller has already committed.
            commit_message: Override for the git commit message. Defaults to `summary`
                            prefixed with `research: ` if no type prefix is present.
            tag: Research log tag. Default "checkpoint".
            artifact_paths: Optional list of repo-relative paths to register as curated
                            artifacts (e.g. ["code/train.py", "figures/loss.png"]).
                            Use only when the files are notable research outputs —
                            checkpoints do not require artifacts.
            artifact_type: Type applied to every artifact in `artifact_paths`
                           (one of: code, data, figure). Default "code".
            cwd: Working directory for git operations. Defaults to current process cwd.

        Returns:
            dict with:
              - status: "ok" | "partial" | "error"
              - commit_sha: str | None
              - log_id: str | None
              - artifact_ids: list[str]
              - warnings: list[str]  (non-fatal issues — e.g., push failed but commit succeeded)
              - error / fix: present only when status == "error", with an actionable hint.
        """
        warnings: list[str] = []
        commit_sha: str | None = None
        msg: str | None = None

        work_dir = Path(cwd) if cwd else Path.cwd()

        # 1. git commit + push
        if commit:
            msg = commit_message or (
                summary if ":" in summary else f"research: {summary}"
            )
            git_result = await _git_commit_and_push(work_dir, msg)
            if git_result.get("error"):
                if git_result.get("reason") == "no_changes":
                    warnings.append(
                        "No uncommitted changes — recording log-only checkpoint."
                    )
                else:
                    return {
                        "status": "error",
                        "error": git_result["error"],
                        "fix": git_result.get(
                            "fix",
                            "Check git status in the working directory.",
                        ),
                    }
            else:
                commit_sha = git_result.get("sha")
                if git_result.get("push_failed"):
                    warnings.append(
                        "git push failed — commit recorded locally. "
                        "Run `git push` manually or check remote."
                    )

        # 2. report commit to backend (best-effort)
        if commit_sha:
            try:
                await arh_client.post(
                    f"/v1/research/projects/{project_id}/commits",
                    json={"sha": commit_sha, "message": msg},
                )
            except Exception as e:  # noqa: BLE001
                warnings.append(
                    f"Commit {commit_sha[:8]} recorded locally but "
                    f"backend report failed: {e}"
                )

        # 3. log research step
        log_id: str | None = None
        try:
            log_resp = await arh_client.post(
                f"/v1/research/projects/{project_id}/logs",
                json={
                    "function_name": "checkpoint",
                    "message": summary,
                    "meta_data": ({"commit_sha": commit_sha} if commit_sha else None),
                    "tag": tag,
                },
            )
            log_id = log_resp.get("id") if isinstance(log_resp, dict) else None
        except Exception as e:  # noqa: BLE001
            warnings.append(f"Log creation failed: {e}")

        # 4. register artifacts (optional)
        artifact_ids: list[str] = []
        for path in artifact_paths or []:
            try:
                art_resp = await arh_client.post(
                    f"/v1/research/projects/{project_id}/artifacts",
                    json={
                        "github_file_path": path,
                        "artifact_type": artifact_type,
                        "title": f"Checkpoint: {path}",
                    },
                )
                if isinstance(art_resp, dict) and art_resp.get("id"):
                    artifact_ids.append(art_resp["id"])
            except Exception as e:  # noqa: BLE001
                warnings.append(f"Artifact registration failed for {path}: {e}")

        status = "partial" if warnings else "ok"

        return {
            "status": status,
            "commit_sha": commit_sha,
            "log_id": log_id,
            "artifact_ids": artifact_ids,
            "warnings": warnings,
        }


async def _git_commit_and_push(work_dir, message: str) -> dict:
    """Run `git add -A && git commit -m <message> && git push` in work_dir.

    Returns:
        {"sha": "...", "push_failed": bool}  on success
        {"error": "...", "reason": "no_changes" | "no_repo" | "commit_failed", "fix": "..."} on failure
    """

    def _run(cmd: list[str]) -> tuple[int, str, str]:
        proc = subprocess.run(
            cmd, cwd=work_dir, capture_output=True, text=True, timeout=60
        )
        return proc.returncode, proc.stdout, proc.stderr

    async def run(cmd: list[str]) -> tuple[int, str, str]:
        return await asyncio.to_thread(_run, cmd)

    rc, _, _ = await run(["git", "rev-parse", "--is-inside-work-tree"])
    if rc != 0:
        return {
            "error": "Not inside a git repository.",
            "reason": "no_repo",
            "fix": (
                "Run /arh:init-research first or set up a git repo "
                "before calling checkpoint."
            ),
        }

    rc, out, _ = await run(["git", "status", "--porcelain"])
    if rc == 0 and not out.strip():
        return {"error": "no changes", "reason": "no_changes"}

    rc, _, err = await run(["git", "add", "-A"])
    if rc != 0:
        return {
            "error": f"git add failed: {err.strip()}",
            "reason": "commit_failed",
            "fix": "Inspect working tree.",
        }

    rc, _, err = await run(["git", "commit", "-m", message])
    if rc != 0:
        return {
            "error": f"git commit failed: {err.strip()}",
            "reason": "commit_failed",
            "fix": "Check staged changes and commit hooks.",
        }

    rc, out, _ = await run(["git", "rev-parse", "HEAD"])
    sha = out.strip() if rc == 0 else None

    rc, _, _ = await run(["git", "push"])
    return {"sha": sha, "push_failed": rc != 0}
