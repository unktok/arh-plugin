"""Git repository detection and post-commit hook management."""

from __future__ import annotations

import os
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
    return f"""{ARH_HOOK_START}
# Auto-installed by arh-client — records commits to AI Researcher Hub
(
  SHA=$(git rev-parse HEAD)
  SHORT_SHA=$(git rev-parse --short HEAD)
  MSG=$(git log -1 --pretty=%B | head -1)
  BRANCH=$(git rev-parse --abbrev-ref HEAD)
  AUTHOR_NAME=$(git log -1 --pretty=%an)
  AUTHOR_EMAIL=$(git log -1 --pretty=%ae)
  COMMITTED_AT=$(git log -1 --pretty=%aI)

  # Build files_changed as JSON array of FileChange objects
  FILES_JSON="["
  FIRST=1
  while IFS=$'\\t' read -r status filepath; do
    [ -z "$filepath" ] && continue
    case "$status" in
      A*) st="added" ;;
      D*) st="deleted" ;;
      R*) st="renamed" ;;
      C*) st="copied" ;;
      *)  st="modified" ;;
    esac
    if [ "$FIRST" -eq 1 ]; then FIRST=0; else FILES_JSON="$FILES_JSON,"; fi
    FILES_JSON="$FILES_JSON{{\\\"path\\\":\\\"$filepath\\\",\\\"status\\\":\\\"$st\\\"}}"
  done <<DIFFEOF
$(git diff-tree --no-commit-id -r --name-status HEAD | head -50)
DIFFEOF
  FILES_JSON="$FILES_JSON]"

  # Build stats as JSON object
  ADDS=$(git diff-tree --no-commit-id --numstat -r HEAD | awk '{{a+=$1; d+=$2}} END {{print a+0}}')
  DELS=$(git diff-tree --no-commit-id --numstat -r HEAD | awk '{{a+=$1; d+=$2}} END {{print d+0}}')
  TOTAL=$((ADDS + DELS))
  STATS_JSON="{{\\\"additions\\\":$ADDS,\\\"deletions\\\":$DELS,\\\"total\\\":$TOTAL}}"

  # Escape message for JSON (newlines, quotes, backslashes)
  MSG_ESCAPED=$(printf '%s' "$MSG" | sed 's/\\\\/\\\\\\\\/g; s/"/\\\\"/g' | head -c 500)

  PAYLOAD=$(cat <<PAYLOAD_EOF
{{
  "sha": "$SHA",
  "message": "$MSG_ESCAPED",
  "branch": "$BRANCH",
  "author_name": "$AUTHOR_NAME",
  "author_email": "$AUTHOR_EMAIL",
  "committed_at": "$COMMITTED_AT",
  "files_changed": $FILES_JSON,
  "stats": $STATS_JSON
}}
PAYLOAD_EOF
  )

  curl -s -X POST "{api_url}/v1/research/projects/{project_id}/commits" \\
    -H "Content-Type: application/json" \\
    -H "Authorization: Bearer {api_key}" \\
    -d "$PAYLOAD" > /dev/null 2>&1 || true
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
