"""Git repository detection and ARH git hook management."""

from __future__ import annotations

import json
import os
import stat
import subprocess


ARH_HOOK_START = "# >>> ARH post-commit hook >>>"
ARH_HOOK_END = "# <<< ARH post-commit hook <<<"
ARH_PUSH_HOOK_START = "# >>> ARH pre-push hook >>>"
ARH_PUSH_HOOK_END = "# <<< ARH pre-push hook <<<"


def detect_git_info(cwd: str | None = None) -> tuple[str, str] | None:
    """Detect git remote URL and branch only when cwd is the git repo root."""
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
    """Find the .git directory for a repo, refusing unrelated parent repos."""
    try:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if root.returncode != 0:
            return None
        if os.path.realpath(root.stdout.strip()) != os.path.realpath(repo_dir):
            return None

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


def _strip_managed_block(content: str, start_marker: str, end_marker: str) -> tuple[str, bool]:
    if start_marker not in content:
        return content, False
    start_idx = content.index(start_marker)
    full_end_marker = end_marker + "\n"
    end_idx = content.find(full_end_marker, start_idx)
    if end_idx == -1:
        end_idx = content.find(end_marker, start_idx)
        if end_idx == -1:
            return content, False
        end_idx += len(end_marker)
    else:
        end_idx += len(full_end_marker)
    return content[:start_idx] + content[end_idx:], True


def _build_hook_script(
    project_id: str, api_url: str, api_key: str, repo_dir: str | None = None
) -> str:
    """Build the pre-push hook snippet.

    The legacy function name is kept for callers/tests. Credentials are resolved
    at runtime from ~/.arh/credentials and are never embedded in the hook file.
    """
    del api_key
    project_id_json = json.dumps(project_id)
    api_url_json = json.dumps(api_url)
    project_dir_json = json.dumps(os.path.realpath(repo_dir or os.getcwd()))
    return f"""{ARH_PUSH_HOOK_START}
# Auto-installed by arh-client — attaches GitHub metadata after push
remote_name="$1"
tmp_file="$(mktemp "${{TMPDIR:-/tmp}}/arh-pre-push.XXXXXX")" || exit 0
{{
  printf '%s\\n' "$remote_name"
  cat
}} > "$tmp_file"
(
  sleep 2
  python3 - "$tmp_file" <<'PY' > /dev/null 2>&1 || true
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ID = {project_id_json}
DEFAULT_API_URL = {api_url_json}
PROJECT_DIR = {project_dir_json}
MAX_REFS = 20
MAX_COMMITS = 200
MAX_FILES_PER_COMMIT = 50
ZERO_SHA = "0" * 40


def git(*args, timeout=20):
    return subprocess.check_output(["git", *args], cwd=PROJECT_DIR, text=True, timeout=timeout).strip()


def git_ok(*args, timeout=20):
    return subprocess.run(["git", *args], cwd=PROJECT_DIR, capture_output=True, text=True, timeout=timeout)


def read_credentials():
    path = Path.home() / ".arh" / "credentials"
    try:
        with path.open() as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {{}}
    return data if isinstance(data, dict) else {{}}


def parse_github_remote(value):
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme == "https" and parsed.hostname == "github.com":
        parts = [p for p in parsed.path.strip("/").split("/") if p]
        if len(parts) == 2:
            owner, name = parts
            if name.endswith(".git"):
                name = name[:-4]
            return owner, name
    match = re.match(r"^git@github\\.com:([^/\\s]+)/([^/\\s]+?)(?:\\.git)?$", value)
    if match:
        return match.group(1), match.group(2)
    return None


def canonical_remote(owner, name):
    return f"https://github.com/{{owner}}/{{name}}.git"


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


def commit_payload(sha, branch):
    files_changed = []
    for line in git("diff-tree", "--no-commit-id", "-r", "--name-status", sha).splitlines()[:MAX_FILES_PER_COMMIT]:
        parts = line.split("\\t")
        if len(parts) >= 2:
            files_changed.append({{"path": parts[-1], "status": file_status(parts[0])}})
    additions = 0
    deletions = 0
    for line in git("diff-tree", "--no-commit-id", "--numstat", "-r", sha).splitlines()[:MAX_FILES_PER_COMMIT]:
        parts = line.split("\\t")
        if len(parts) >= 2:
            additions += int(parts[0]) if parts[0].isdigit() else 0
            deletions += int(parts[1]) if parts[1].isdigit() else 0
    return {{
        "sha": sha,
        "message": git("log", "-1", "--pretty=%B", sha)[:500],
        "branch": branch,
        "author_name": git("log", "-1", "--pretty=%an", sha)[:200],
        "author_email": git("log", "-1", "--pretty=%ae", sha)[:320],
        "committed_at": git("log", "-1", "--pretty=%aI", sha),
        "files_changed": files_changed,
        "stats": {{"additions": additions, "deletions": deletions, "total": additions + deletions}},
    }}


def post_json(api_url, api_key, path, payload):
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{{api_url}}{{path}}",
        data=data,
        headers={{"Content-Type": "application/json", "Authorization": f"Bearer {{api_key}}"}},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8") or "{{}}")


def remote_has_sha(remote, remote_ref, sha):
    for _ in range(10):
        proc = git_ok("ls-remote", remote, remote_ref, timeout=15)
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0] == sha and parts[1] == remote_ref:
                    return True
        time.sleep(2)
    return False


def main():
    refs_file = Path(sys.argv[1])
    try:
        root = git("rev-parse", "--show-toplevel")
    except Exception:
        return
    if os.path.realpath(root) != os.path.realpath(PROJECT_DIR):
        return

    creds = read_credentials()
    api_key = creds.get("api_key") or ""
    api_url = (creds.get("api_url") or DEFAULT_API_URL).rstrip("/")
    if not api_key:
        return

    try:
        raw_lines = refs_file.read_text().splitlines()
    except OSError:
        raw_lines = []
    finally:
        try:
            refs_file.unlink()
        except OSError:
            pass
    if not raw_lines:
        return
    remote_name = raw_lines[0].strip()
    raw_lines = raw_lines[1:]
    if (
        not remote_name
        or remote_name.startswith("-")
        or "://" in remote_name
        or "@" in remote_name
        or not re.fullmatch(r"[A-Za-z0-9._/-]+", remote_name)
    ):
        return
    try:
        remote_url = git("remote", "get-url", remote_name, timeout=10)
    except Exception:
        return
    parsed_remote = parse_github_remote(remote_url)
    if not parsed_remote:
        return
    owner, name = parsed_remote
    clean_remote = canonical_remote(owner, name)

    pushed = []
    for line in raw_lines[:MAX_REFS]:
        parts = line.split()
        if len(parts) != 4:
            continue
        local_ref, local_sha, remote_ref, remote_sha = parts
        if local_sha == ZERO_SHA or not remote_ref.startswith("refs/heads/"):
            continue
        if not re.fullmatch(r"[0-9a-f]{{7,40}}", local_sha):
            continue
        if not remote_has_sha(remote_name, remote_ref, local_sha):
            continue
        branch = remote_ref.removeprefix("refs/heads/")
        start = remote_sha if re.fullmatch(r"[0-9a-f]{{7,40}}", remote_sha) and remote_sha != ZERO_SHA else ""
        rev_range = f"{{start}}..{{local_sha}}" if start else local_sha
        proc = git_ok("rev-list", "--reverse", "--max-count", str(MAX_COMMITS), rev_range, timeout=20)
        if proc.returncode != 0:
            continue
        for sha in proc.stdout.splitlines():
            if re.fullmatch(r"[0-9a-f]{{7,40}}", sha):
                pushed.append(commit_payload(sha, branch))
        if len(pushed) >= MAX_COMMITS:
            pushed = pushed[:MAX_COMMITS]
            break

    if not pushed:
        return

    link_payload = {{"remote_url": clean_remote, "branch": pushed[-1].get("branch") or ""}}
    try:
        post_json(api_url, api_key, f"/v1/research/projects/{{PROJECT_ID}}/link-repo", link_payload)
    except urllib.error.HTTPError:
        return
    post_json(api_url, api_key, f"/v1/research/projects/{{PROJECT_ID}}/commits/batch", {{"commits": pushed}})


if __name__ == "__main__":
    main()
PY
) &
{ARH_PUSH_HOOK_END}
"""


def _build_post_commit_hook_script(
    project_id: str, api_url: str, api_key: str, repo_dir: str | None = None
) -> str:
    """Build the post-commit hook snippet that records local commits."""
    del api_key
    project_id_json = json.dumps(project_id)
    api_url_json = json.dumps(api_url)
    project_dir_json = json.dumps(os.path.realpath(repo_dir or os.getcwd()))
    return f"""{ARH_HOOK_START}
# Auto-installed by arh-client — records local commits to AI Researcher Hub
(
  python3 - <<'PY' > /dev/null 2>&1 || true
import json
import os
import subprocess
import urllib.request
from pathlib import Path

PROJECT_ID = {project_id_json}
DEFAULT_API_URL = {api_url_json}
PROJECT_DIR = {project_dir_json}
MAX_FILES_PER_COMMIT = 50


def git(*args, timeout=20):
    return subprocess.check_output(["git", *args], cwd=PROJECT_DIR, text=True, timeout=timeout).strip()


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


def main():
    try:
        root = git("rev-parse", "--show-toplevel")
        sha = git("rev-parse", "HEAD")
    except Exception:
        return
    if os.path.realpath(root) != os.path.realpath(PROJECT_DIR):
        return

    creds = read_credentials()
    api_key = creds.get("api_key") or ""
    api_url = (creds.get("api_url") or DEFAULT_API_URL).rstrip("/")
    if not api_key:
        return

    files_changed = []
    for line in git("diff-tree", "--no-commit-id", "-r", "--name-status", sha).splitlines()[:MAX_FILES_PER_COMMIT]:
        parts = line.split("\\t")
        if len(parts) >= 2:
            files_changed.append({{"path": parts[-1], "status": file_status(parts[0])}})

    additions = 0
    deletions = 0
    for line in git("diff-tree", "--no-commit-id", "--numstat", "-r", sha).splitlines()[:MAX_FILES_PER_COMMIT]:
        parts = line.split("\\t")
        if len(parts) >= 2:
            additions += int(parts[0]) if parts[0].isdigit() else 0
            deletions += int(parts[1]) if parts[1].isdigit() else 0

    payload = {{
        "sha": sha,
        "message": git("log", "-1", "--pretty=%B", sha)[:500],
        "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "author_name": git("log", "-1", "--pretty=%an", sha)[:200],
        "author_email": git("log", "-1", "--pretty=%ae", sha)[:320],
        "committed_at": git("log", "-1", "--pretty=%aI", sha),
        "files_changed": files_changed,
        "stats": {{"additions": additions, "deletions": deletions, "total": additions + deletions}},
    }}
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{{api_url}}/v1/research/projects/{{PROJECT_ID}}/commits",
        data=data,
        headers={{"Content-Type": "application/json", "Authorization": f"Bearer {{api_key}}"}},
        method="POST",
    )
    urllib.request.urlopen(request, timeout=10).read()


if __name__ == "__main__":
    main()
PY
) &
{ARH_HOOK_END}
"""


def install_local_commit_hook(
    project_id: str,
    api_url: str,
    api_key: str,
    repo_dir: str | None = None,
) -> str | None:
    """Install a post-commit hook that records local commits immediately."""
    repo_dir = repo_dir or os.getcwd()
    git_dir = _find_git_dir(repo_dir)
    if not git_dir:
        return None

    hooks_dir = os.path.join(git_dir, "hooks")
    os.makedirs(hooks_dir, exist_ok=True)
    hook_path = os.path.join(hooks_dir, "post-commit")

    existing_content = ""
    if os.path.exists(hook_path):
        with open(hook_path) as f:
            existing_content = f.read()
        existing_content, _ = _strip_managed_block(
            existing_content, ARH_HOOK_START, ARH_HOOK_END
        )

    snippet = _build_post_commit_hook_script(project_id, api_url, api_key, repo_dir)
    with open(hook_path, "w") as f:
        if not existing_content:
            f.write("#!/bin/sh\n")
        else:
            f.write(existing_content)
            if not existing_content.endswith("\n"):
                f.write("\n")
        f.write(snippet)

    st = os.stat(hook_path)
    os.chmod(hook_path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return hook_path


def install_push_tracking_hook(
    project_id: str,
    api_url: str,
    api_key: str,
    repo_dir: str | None = None,
) -> str | None:
    """Install a pre-push hook that links GitHub metadata after pushed commits."""
    repo_dir = repo_dir or os.getcwd()
    git_dir = _find_git_dir(repo_dir)
    if not git_dir:
        return None

    hooks_dir = os.path.join(git_dir, "hooks")
    os.makedirs(hooks_dir, exist_ok=True)
    hook_path = os.path.join(hooks_dir, "pre-push")

    existing_content = ""
    if os.path.exists(hook_path):
        with open(hook_path) as f:
            existing_content = f.read()
        existing_content, _ = _strip_managed_block(
            existing_content, ARH_PUSH_HOOK_START, ARH_PUSH_HOOK_END
        )

    snippet = _build_hook_script(project_id, api_url, api_key, repo_dir)
    with open(hook_path, "w") as f:
        if not existing_content:
            f.write("#!/bin/sh\n")
        else:
            f.write(existing_content)
            if not existing_content.endswith("\n"):
                f.write("\n")
        f.write(snippet)

    st = os.stat(hook_path)
    os.chmod(hook_path, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    install_local_commit_hook(project_id, api_url, api_key, repo_dir)
    return hook_path


def install_post_commit_hook(
    project_id: str,
    api_url: str,
    api_key: str,
    repo_dir: str | None = None,
) -> str | None:
    """Backward-compatible wrapper that installs both local and push-time hooks."""
    return install_push_tracking_hook(project_id, api_url, api_key, repo_dir)


def uninstall_push_tracking_hook(repo_dir: str | None = None) -> bool:
    """Remove the ARH pre-push hook snippet. Returns True if removed."""
    repo_dir = repo_dir or os.getcwd()
    git_dir = _find_git_dir(repo_dir)
    if not git_dir:
        return False

    hook_path = os.path.join(git_dir, "hooks", "pre-push")
    if not os.path.exists(hook_path):
        return False

    with open(hook_path) as f:
        content = f.read()

    new_content, removed = _strip_managed_block(
        content, ARH_PUSH_HOOK_START, ARH_PUSH_HOOK_END
    )
    if not removed:
        return False

    if new_content.strip() in ("", "#!/bin/sh"):
        os.remove(hook_path)
    else:
        with open(hook_path, "w") as f:
            f.write(new_content)

    return True


def uninstall_post_commit_hook(repo_dir: str | None = None) -> bool:
    """Remove the ARH post-commit hook snippet. Returns True if removed."""
    repo_dir = repo_dir or os.getcwd()
    git_dir = _find_git_dir(repo_dir)
    if not git_dir:
        return False

    hook_path = os.path.join(git_dir, "hooks", "post-commit")
    if not os.path.exists(hook_path):
        return False

    with open(hook_path) as f:
        content = f.read()

    new_content, removed = _strip_managed_block(content, ARH_HOOK_START, ARH_HOOK_END)
    if not removed:
        return False

    if new_content.strip() in ("", "#!/bin/sh"):
        os.remove(hook_path)
    else:
        with open(hook_path, "w") as f:
            f.write(new_content)

    return True
