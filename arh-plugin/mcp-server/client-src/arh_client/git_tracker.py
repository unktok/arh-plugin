"""Git repository detection and post-commit hook management."""

from __future__ import annotations

import os
import json
import subprocess
import stat


ARH_HOOK_START = "# >>> ARH post-commit hook >>>"
ARH_HOOK_END = "# <<< ARH post-commit hook <<<"


def detect_git_info(cwd: str | None = None) -> tuple[str, str] | None:
    """Detect git remote URL and branch from the working directory.

    Only returns info if the git root matches ``cwd`` (i.e. ``cwd`` is
    the top-level of the repository).  If ``cwd`` is a subdirectory of
    some *parent* repo, that parent repo is considered unrelated and
    this function returns ``None`` so that a new repo can be created.

    Args:
        cwd: Directory to check. Defaults to os.getcwd().

    Returns:
        Tuple of (remote_url, branch), or None if not a git repo
        (or if the git root doesn't match cwd).
    """
    cwd = cwd or os.getcwd()

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
    except (OSError, subprocess.TimeoutExpired):
        return None

    # Ensure git root is this directory, not a parent
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            git_root = os.path.realpath(result.stdout.strip())
            project_dir = os.path.realpath(cwd)
            if git_root != project_dir:
                return None
    except (OSError, subprocess.TimeoutExpired):
        return None

    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        remote_url = result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        remote_url = ""

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
        )
        branch = result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        branch = ""

    return remote_url, branch


def _find_git_dir(repo_dir: str) -> str | None:
    """Find the .git directory for a repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            git_dir = result.stdout.strip()
            if not os.path.isabs(git_dir):
                git_dir = os.path.join(repo_dir, git_dir)
            return git_dir
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def _build_hook_script(project_id: str, api_url: str, api_key: str) -> str:
    """Build the shell script snippet for the post-commit hook."""
    del api_key  # Credentials are resolved at runtime from ~/.arh/credentials.
    project_id_json = json.dumps(project_id)
    api_url_json = json.dumps(api_url)
    return f"""{ARH_HOOK_START}
# Auto-installed by arh-client — records commits to AI Researcher Hub
(
  python3 - <<'PY' > /dev/null 2>&1 || true
import json
import subprocess
import urllib.request
from pathlib import Path

PROJECT_ID = {project_id_json}
DEFAULT_API_URL = {api_url_json}


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def read_credentials():
    path = Path.home() / ".arh" / "credentials"
    try:
        with path.open() as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {{}}
    return data if isinstance(data, dict) else {{}}


def file_status(status):
    if status.startswith("A"):
        return "added"
    if status.startswith("D"):
        return "deleted"
    if status.startswith("R"):
        return "renamed"
    if status.startswith("C"):
        return "copied"
    return "modified"


creds = read_credentials()
api_key = creds.get("api_key") or ""
api_url = (creds.get("api_url") or DEFAULT_API_URL).rstrip("/")
if not api_key:
    raise SystemExit(0)

files_changed = []
for line in subprocess.check_output(
    ["git", "diff-tree", "--no-commit-id", "-r", "--name-status", "HEAD"],
    text=True,
).splitlines()[:50]:
    parts = line.split("\\t")
    if len(parts) < 2:
        continue
    status, path = parts[0], parts[-1]
    files_changed.append({{"path": path, "status": file_status(status)}})

additions = 0
deletions = 0
for line in subprocess.check_output(
    ["git", "diff-tree", "--no-commit-id", "--numstat", "-r", "HEAD"],
    text=True,
).splitlines():
    parts = line.split("\\t")
    if len(parts) >= 2:
        additions += int(parts[0]) if parts[0].isdigit() else 0
        deletions += int(parts[1]) if parts[1].isdigit() else 0

payload = {{
    "sha": git("rev-parse", "HEAD"),
    "message": git("log", "-1", "--pretty=%B")[:500],
    "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
    "author_name": git("log", "-1", "--pretty=%an"),
    "author_email": git("log", "-1", "--pretty=%ae"),
    "committed_at": git("log", "-1", "--pretty=%aI"),
    "files_changed": files_changed,
    "stats": {{"additions": additions, "deletions": deletions, "total": additions + deletions}},
}}
body = json.dumps(payload).encode("utf-8")
request = urllib.request.Request(
    f"{{api_url}}/v1/research/projects/{{PROJECT_ID}}/commits",
    data=body,
    headers={{
        "Content-Type": "application/json",
        "Authorization": f"Bearer {{api_key}}",
    }},
    method="POST",
)
urllib.request.urlopen(request, timeout=10).read()
PY
) &
{ARH_HOOK_END}
"""


def install_post_commit_hook(
    project_id: str,
    api_url: str,
    api_key: str,
    repo_dir: str | None = None,
) -> str | None:
    """Install a post-commit hook that notifies ARH of new commits.

    Appends to existing hook file if present. Returns the hook file path
    on success, or None on failure.
    """
    repo_dir = repo_dir or os.getcwd()
    git_dir = _find_git_dir(repo_dir)
    if not git_dir:
        return None

    hooks_dir = os.path.join(git_dir, "hooks")
    os.makedirs(hooks_dir, exist_ok=True)
    hook_path = os.path.join(hooks_dir, "post-commit")

    existing_content = ""
    if os.path.exists(hook_path):
        with open(hook_path, "r") as f:
            existing_content = f.read()

        # If ARH hook already exists, replace it (project_id may have changed)
        if ARH_HOOK_START in existing_content:
            start_idx = existing_content.index(ARH_HOOK_START)
            end_marker = ARH_HOOK_END + "\n"
            end_idx = existing_content.find(end_marker)
            if end_idx == -1:
                end_idx = existing_content.find(ARH_HOOK_END)
                if end_idx != -1:
                    end_idx += len(ARH_HOOK_END)
            else:
                end_idx += len(end_marker)
            if end_idx != -1:
                existing_content = existing_content[:start_idx] + existing_content[end_idx:]

    snippet = _build_hook_script(project_id, api_url, api_key)

    with open(hook_path, "w") as f:
        if not existing_content:
            f.write("#!/bin/sh\n")
        else:
            f.write(existing_content)
            if not existing_content.endswith("\n"):
                f.write("\n")
        f.write(snippet)

    # Make executable
    st = os.stat(hook_path)
    os.chmod(hook_path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    return hook_path


def uninstall_post_commit_hook(repo_dir: str | None = None) -> bool:
    """Remove the ARH post-commit hook snippet. Returns True if removed."""
    repo_dir = repo_dir or os.getcwd()
    git_dir = _find_git_dir(repo_dir)
    if not git_dir:
        return False

    hook_path = os.path.join(git_dir, "hooks", "post-commit")
    if not os.path.exists(hook_path):
        return False

    with open(hook_path, "r") as f:
        content = f.read()

    if ARH_HOOK_START not in content:
        return False

    # Remove the ARH block
    start_idx = content.index(ARH_HOOK_START)
    end_marker = ARH_HOOK_END + "\n"
    end_idx = content.find(end_marker)
    if end_idx == -1:
        end_idx = content.find(ARH_HOOK_END)
        if end_idx == -1:
            return False
        end_idx += len(ARH_HOOK_END)
    else:
        end_idx += len(end_marker)

    new_content = content[:start_idx] + content[end_idx:]

    # If only shebang remains, remove the file
    if new_content.strip() in ("", "#!/bin/sh"):
        os.remove(hook_path)
    else:
        with open(hook_path, "w") as f:
            f.write(new_content)

    return True
