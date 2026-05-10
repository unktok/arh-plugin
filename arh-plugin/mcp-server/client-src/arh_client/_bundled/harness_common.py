#!/usr/bin/env python3
"""Shared local harness utilities for ARH agent integrations.

The module is intentionally stdlib-only so plugin scripts can run from Claude
Code, Codex, shell wrappers, and custom agent launchers without installing
extra dependencies.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from hashlib import sha256
from pathlib import Path
from typing import Any


DEFAULT_API_URL = "https://api.airesearcherhub.com"
MAX_OUTPUT_LENGTH = 2000
MAX_THINKING_LENGTH = 5000

_SHADOW_REF_PREFIX = "refs/heads/arh-auto"
_AUTO_CHECKPOINT_THROTTLE_SECONDS = 30
_AUTO_CHECKPOINT_STATE_FILE = ".arh/.auto-checkpoint-state"


def read_json_file(path: Path) -> dict[str, Any]:
    try:
        if path.is_file():
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        if not path.is_file():
            return values
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    except OSError:
        pass
    return values


def _valid_api_key(value: str) -> bool:
    return value.startswith("arh_sk_") and "${" not in value


def load_context(cwd: Path) -> dict[str, str]:
    creds = read_json_file(Path.home() / ".arh" / "credentials")
    project_env = read_env_file(cwd / ".arh" / ".env")
    project_settings = read_json_file(cwd / ".arh" / "settings.json")
    trace_file = read_json_file(cwd / ".arh-trace")

    context = {
        "api_url": DEFAULT_API_URL,
        "api_key": "",
        "project_id": "",
        "trace_id": "",
    }
    if isinstance(creds.get("api_url"), str):
        context["api_url"] = creds["api_url"]
    if isinstance(creds.get("api_key"), str):
        context["api_key"] = creds["api_key"]
    if project_env.get("ARH_API_URL"):
        context["api_url"] = project_env["ARH_API_URL"]
    if project_env.get("ARH_PROJECT_ID"):
        context["project_id"] = project_env["ARH_PROJECT_ID"]
    if project_env.get("ARH_TRACE_ID"):
        context["trace_id"] = project_env["ARH_TRACE_ID"]
    if isinstance(project_settings.get("project_id"), str):
        context["project_id"] = project_settings["project_id"]
    if isinstance(trace_file.get("trace_id"), str):
        context["trace_id"] = trace_file["trace_id"]

    context["api_url"] = os.environ.get("ARH_API_URL", context["api_url"])
    env_api_key = os.environ.get("ARH_API_KEY", "")
    if _valid_api_key(env_api_key):
        context["api_key"] = env_api_key
    context["project_id"] = os.environ.get("ARH_PROJECT_ID", context["project_id"])
    context["trace_id"] = os.environ.get("ARH_TRACE_ID", context["trace_id"])
    return context


def write_project_id(cwd: Path, project_id: str) -> None:
    arh_dir = cwd / ".arh"
    arh_dir.mkdir(exist_ok=True)
    settings_path = arh_dir / "settings.json"
    settings = read_json_file(settings_path)
    if settings.get("project_id") == project_id:
        return
    settings["project_id"] = project_id
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")


def read_settings(cwd: Path) -> dict[str, Any]:
    return read_json_file(cwd / ".arh" / "settings.json")


def write_settings(cwd: Path, updates: dict[str, Any]) -> None:
    arh_dir = cwd / ".arh"
    arh_dir.mkdir(exist_ok=True)
    settings_path = arh_dir / "settings.json"
    settings = read_json_file(settings_path)
    settings.update({key: value for key, value in updates.items() if value is not None})
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")


def truncate(value: str, max_length: int = MAX_OUTPUT_LENGTH) -> str:
    value = redact_text(value)
    if len(value) > max_length:
        return value[:max_length] + "... [truncated]"
    return value


def redact_text(value: str) -> str:
    patterns = [
        (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL), "[REDACTED]"),
        (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE), "Bearer [REDACTED]"),
        (re.compile(r"\b(arh_sk_)[A-Za-z0-9._-]+"), r"\1[REDACTED]"),
        (re.compile(r"\b(sk_live_|sk_test_|rk_live_|rk_test_|sk-or-|sk-)[A-Za-z0-9._-]{12,}"), r"\1[REDACTED]"),
        (re.compile(r"\b(ghp_|gho_|ghu_|ghs_|ghr_|github_pat_)[A-Za-z0-9_]{20,}"), r"\1[REDACTED]"),
        (re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b"), r"\1[REDACTED]"),
        (re.compile(r"\b(xox[baprs]-)[A-Za-z0-9-]{16,}\b"), r"\1[REDACTED]"),
    ]
    redacted = value
    for pattern, replacement in patterns:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def detect_git_info(cwd: Path) -> tuple[str, str]:
    remote = git(cwd, ["remote", "get-url", "origin"])[1].strip()
    branch = git(cwd, ["rev-parse", "--abbrev-ref", "HEAD"])[1].strip()
    return remote, branch


def git(cwd: Path, args: list[str], timeout: int = 15) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout, result.stderr
    except (OSError, subprocess.TimeoutExpired):
        return 1, "", ""


def uncommitted_files(cwd: Path, limit: int = 20) -> list[str]:
    rc, stdout, _ = git(cwd, ["status", "--porcelain"])
    if rc != 0:
        return []
    paths: list[str] = []
    for line in stdout.splitlines():
        if not line:
            continue
        path = line[3:] if len(line) > 3 else line
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path)
        if len(paths) >= limit:
            break
    return paths


def has_git_changes(cwd: Path) -> bool:
    return bool(uncommitted_files(cwd, limit=1))


def secret_scan_required(cwd: Path) -> bool:
    settings = read_settings(cwd)
    if settings.get("secret_scan_required") is False:
        return False
    raw = os.environ.get("ARH_SECRET_SCAN_REQUIRED", "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    return True


def gitleaks_path() -> str:
    configured = os.environ.get("ARH_GITLEAKS_PATH", "").strip()
    if configured:
        return configured
    found = shutil.which("gitleaks")
    if found:
        return found
    go_bin = Path.home() / "go" / "bin" / "gitleaks"
    return str(go_bin) if go_bin.is_file() else ""


def scan_staged_secrets(cwd: Path) -> dict[str, Any]:
    if not secret_scan_required(cwd):
        return {"status": "skipped", "reason": "disabled"}

    binary = gitleaks_path()
    if not binary:
        return {
            "status": "blocked",
            "reason": "gitleaks_missing",
            "error": "gitleaks is required before ARH auto-commit can run.",
        }

    try:
        scan = subprocess.run(
            [
                binary,
                "protect",
                "--staged",
                "--redact",
                "--no-banner",
                "--report-format",
                "json",
                "--report-path",
                "-",
            ],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "status": "blocked",
            "reason": "secret_scan_error",
            "error": truncate(str(exc)),
        }

    output = truncate((scan.stdout or scan.stderr or "").strip())
    if scan.returncode == 0:
        return {"status": "clean", "tool": binary}
    if scan.returncode == 1:
        return {
            "status": "blocked",
            "reason": "secret_scan_findings",
            "error": output or "gitleaks detected staged secrets.",
        }
    return {
        "status": "blocked",
        "reason": "secret_scan_error",
        "error": output or f"gitleaks exited with status {scan.returncode}.",
    }


def generate_commit_message(event_data: dict[str, Any], event_type: str) -> str:
    if event_type == "TaskCompleted":
        subject = str(event_data.get("task_subject") or "").strip()
        if subject:
            return f"research: complete {subject[:60]}"
    last_msg = str(
        event_data.get("last_assistant_message")
        or event_data.get("message")
        or event_data.get("summary")
        or ""
    ).strip()
    if last_msg:
        return f"research: {last_msg.splitlines()[0][:60]}"
    return "research: auto-commit progress"


def auto_commit_handoff(reason: str = "configured_handoff") -> dict[str, Any]:
    return {
        "status": "handoff",
        "reason": reason,
        "message": "Codex handoff captured; no git commit was created.",
    }


def auto_commit_and_push(
    cwd: Path,
    event_data: dict[str, Any],
    event_type: str,
) -> dict[str, Any]:
    if git(cwd, ["rev-parse", "--is-inside-work-tree"])[0] != 0:
        return {"status": "skipped", "reason": "not_git_repo"}
    if not has_git_changes(cwd):
        return {"status": "skipped", "reason": "clean"}

    message = generate_commit_message(event_data, event_type)
    add = subprocess.run(["git", "add", "-A"], cwd=str(cwd), capture_output=True, text=True)
    if add.returncode != 0:
        return {"status": "error", "message": message, "error": truncate(add.stderr or add.stdout)}

    secret_scan = scan_staged_secrets(cwd)
    if secret_scan.get("status") == "blocked":
        return {
            "status": "blocked",
            "message": message,
            "reason": secret_scan.get("reason", "secret_scan_failed"),
            "error": secret_scan.get("error", "Secret scan blocked auto-commit."),
        }

    commit = subprocess.run(
        ["git", "commit", "-m", message],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )
    if commit.returncode != 0:
        return {"status": "error", "message": message, "error": truncate(commit.stderr or commit.stdout)}

    sha_rc, sha, _ = git(cwd, ["rev-parse", "HEAD"])
    sha = sha.strip()
    result: dict[str, Any] = {
        "status": "committed",
        "sha": sha if sha_rc == 0 else "",
        "message": message,
        "push_failed": False,
    }

    remote_rc, remote, _ = git(cwd, ["remote"])
    if remote_rc != 0 or not remote.strip():
        result["push_skipped"] = True
        result["push_reason"] = "no_remote"
        return result

    push = subprocess.run(["git", "push"], cwd=str(cwd), capture_output=True, text=True)
    if push.returncode != 0:
        result["push_failed"] = True
        result["push_error"] = truncate(push.stderr or push.stdout)
    else:
        result["pushed"] = True
    return result


def safe_session_token(session_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", session_id or "unknown")[:64]


def offset_token(session_id: str, path: Path) -> str:
    digest = sha256(str(path.expanduser().resolve()).encode("utf-8")).hexdigest()[:12]
    return safe_session_token(f"{session_id}-{digest}")


def shadow_ref_for(session_id: str) -> str:
    return f"{_SHADOW_REF_PREFIX}/{safe_session_token(session_id)}"


def ensure_shadow_ref(cwd: Path, session_id: str) -> bool:
    ref = shadow_ref_for(session_id)
    if git(cwd, ["rev-parse", "--verify", "--quiet", ref])[0] == 0:
        return True
    rc, head_sha, _ = git(cwd, ["rev-parse", "--verify", "--quiet", "HEAD"])
    head_sha = head_sha.strip()
    if rc != 0 or not head_sha:
        return False
    return git(cwd, ["update-ref", ref, head_sha])[0] == 0


def _auto_checkpoint_throttled(cwd: Path) -> bool:
    state_path = cwd / _AUTO_CHECKPOINT_STATE_FILE
    try:
        return (time.time() - state_path.stat().st_mtime) < _AUTO_CHECKPOINT_THROTTLE_SECONDS
    except OSError:
        return False


def _touch_auto_checkpoint_state(cwd: Path) -> None:
    state_path = cwd / _AUTO_CHECKPOINT_STATE_FILE
    try:
        state_path.parent.mkdir(exist_ok=True)
        state_path.write_text(str(time.time()))
    except OSError:
        pass


def auto_checkpoint(
    cwd: Path,
    session_id: str,
    summary: str,
    bypass_throttle: bool = False,
) -> dict[str, str] | None:
    if git(cwd, ["rev-parse", "--is-inside-work-tree"])[0] != 0:
        return None
    if not bypass_throttle and _auto_checkpoint_throttled(cwd):
        return None
    if git(cwd, ["diff", "--quiet"])[0] == 0 and git(cwd, ["diff", "--cached", "--quiet"])[0] == 0:
        if not uncommitted_files(cwd, limit=1):
            return None

    ref = shadow_ref_for(session_id)
    has_shadow_ref = ensure_shadow_ref(cwd, session_id)
    index_path = (
        Path(tempfile.gettempdir())
        / f"arh_shadow_index_{safe_session_token(str(cwd))}_{safe_session_token(session_id)}"
    )
    try:
        index_path.parent.mkdir(exist_ok=True)
    except OSError:
        return None

    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = str(index_path)
    if has_shadow_ref:
        subprocess.run(
            ["git", "read-tree", ref],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
        )
    else:
        try:
            index_path.unlink()
        except OSError:
            pass
    add = subprocess.run(["git", "add", "-A"], cwd=str(cwd), env=env, capture_output=True, text=True)
    if add.returncode != 0:
        return None
    tree = subprocess.run(["git", "write-tree"], cwd=str(cwd), env=env, capture_output=True, text=True)
    if tree.returncode != 0 or not tree.stdout.strip():
        return None
    tree_sha = tree.stdout.strip()

    parent_rc, parent_sha, _ = git(cwd, ["rev-parse", "--verify", "--quiet", ref])
    if parent_rc == 0 and parent_sha.strip():
        parent_tree = git(cwd, ["show", "-s", "--format=%T", parent_sha.strip()])[1].strip()
        if parent_tree == tree_sha:
            return None
    commit_args = ["commit-tree", tree_sha, "-m", f"arh auto: {summary}"]
    if parent_rc == 0 and parent_sha.strip():
        commit_args[2:2] = ["-p", parent_sha.strip()]
    commit = subprocess.run(["git", *commit_args], cwd=str(cwd), capture_output=True, text=True)
    sha = commit.stdout.strip()
    if commit.returncode != 0 or not re.match(r"^[a-f0-9]{7,64}$", sha):
        return None
    if git(cwd, ["update-ref", ref, sha])[0] != 0:
        return None
    _touch_auto_checkpoint_state(cwd)
    return {"sha": sha, "summary": summary}


def read_new_transcript_entries(
    transcript_path: Path,
    session_id: str,
    max_entries: int = 50,
) -> list[dict[str, str]]:
    if not transcript_path.is_file():
        return []
    offset_file = Path(tempfile.gettempdir()) / f"arh_offset_{offset_token(session_id, transcript_path)}"
    try:
        last_offset = int(offset_file.read_text().strip()) if offset_file.is_file() else 0
    except (OSError, ValueError):
        last_offset = 0

    entries: list[dict[str, str]] = []
    new_offset = last_offset
    try:
        with transcript_path.open("r", encoding="utf-8") as fh:
            fh.seek(last_offset)
            while True:
                line = fh.readline()
                if not line:
                    break
                new_offset = fh.tell()
                entry = parse_transcript_record(line)
                entries.extend(entry)
                if len(entries) >= max_entries:
                    break
    except (OSError, PermissionError):
        return []

    if new_offset > last_offset:
        try:
            offset_file.write_text(str(new_offset))
        except OSError:
            pass
    return entries[:max_entries]


def parse_transcript_record(line: str) -> list[dict[str, str]]:
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return []
    return transcript_entries_from_record(record)


def transcript_entries_from_record(record: dict[str, Any]) -> list[dict[str, str]]:
    record_type = record.get("type", "")
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    if record_type == "response_item":
        item_type = payload.get("type")
        role = payload.get("role")
        content = payload.get("content")
        if item_type == "message" and role in {"assistant", "user"}:
            return _entries_from_content(role, content)
        if item_type == "reasoning":
            summaries = payload.get("summary") or []
            text = "\n".join(
                str(s.get("text", "")) for s in summaries if isinstance(s, dict)
            ).strip()
            if text:
                return [{"role": "assistant", "type": "thinking", "content": truncate(text, MAX_THINKING_LENGTH)}]
        return []
    if record_type in {"assistant", "user", "human"}:
        message = record.get("message", {})
        role = "user" if record_type in {"user", "human"} else "assistant"
        return _entries_from_content(role, message.get("content", []))
    return []


def _entries_from_content(role: str, content: Any) -> list[dict[str, str]]:
    entry_type = "user_input" if role == "user" else "text"
    if isinstance(content, str):
        return [{"role": role, "type": entry_type, "content": truncate(content)}] if content.strip() else []
    if not isinstance(content, list):
        return []
    entries: list[dict[str, str]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        text = block.get("text") or block.get("input_text") or block.get("output_text") or ""
        if text:
            entries.append({"role": role, "type": entry_type, "content": truncate(str(text))})
        elif block_type == "thinking" and block.get("thinking"):
            entries.append({"role": role, "type": "thinking", "content": truncate(str(block["thinking"]), MAX_THINKING_LENGTH)})
    return entries


def send_event(api_url: str, api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not api_key:
        raise RuntimeError("ARH_API_KEY is required")
    url = f"{api_url.rstrip('/')}/v1/hooks/agent-event"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15, context=_ssl_context()) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if exc.code == 401 and os.environ.get("ARH_API_KEY"):
            body = (
                f"{body} If you recently rotated credentials, unset stale "
                "ARH_API_KEY so ~/.arh/credentials can be used."
            )
        raise RuntimeError(f"ARH request failed ({exc.code}): {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"ARH request failed: {exc.reason}") from exc


def _ssl_context() -> ssl.SSLContext:
    """Return a TLS context that works with python.org macOS installs too."""
    try:
        import certifi  # type: ignore[import-not-found]

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()
