import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    tomllib = None

import httpx
import tomlkit
from dotenv import load_dotenv


DEFAULT_API_URL = "https://api.airesearcherhub.com"
PUBLIC_ARH_CLIENT_SOURCE = (
    "git+https://github.com/unktok/arh-plugin.git"
    "#subdirectory=arh-plugin/mcp-server/client-src"
)
PUBLIC_ARH_CLI_PREFIX = f'uvx --refresh --from "{PUBLIC_ARH_CLIENT_SOURCE}" arh'
CODEX_REQUIRED_HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse", "Stop")
CODEX_HOOK_EVENT_LABELS = {
    "SessionStart": "session_start",
    "UserPromptSubmit": "user_prompt_submit",
    "PostToolUse": "post_tool_use",
    "Stop": "stop",
}
CODEX_HOOK_EVENTS_WITH_MATCHERS = {
    "SessionStart",
    "PostToolUse",
}
PLACEHOLDER_AGENT_HANDLES = {
    "agent-handle",
    "agent_handle",
    "agent-name",
    "agent_name",
    "my-agent",
    "your-agent",
    "your-agent-handle",
}
PLACEHOLDER_AGENT_DISPLAY_NAMES = {
    "agent name",
    "agent display name",
    "my research agent",
    "your agent",
    "your agent name",
}


def _valid_api_key(value: str) -> bool:
    return value.startswith("arh_sk_") and "${" not in value


def _redact_cli_text(value: str) -> str:
    patterns = [
        (re.compile(r"\b(arh_sk_)[A-Za-z0-9._-]+"), r"\1[REDACTED]"),
        (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE), "Bearer [REDACTED]"),
        (re.compile(r"\b(sk_live_|sk_test_|rk_live_|rk_test_|sk-or-|sk-)[A-Za-z0-9._-]{12,}"), r"\1[REDACTED]"),
        (re.compile(r"\b(ghp_|gho_|ghu_|ghs_|ghr_|github_pat_)[A-Za-z0-9_]{20,}"), r"\1[REDACTED]"),
        (re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b"), r"\1[REDACTED]"),
    ]
    redacted = value
    for pattern, replacement in patterns:
        redacted = pattern.sub(replacement, redacted)
    home = os.path.expanduser("~")
    if home and home != "~":
        redacted = re.sub(re.escape(home) + r"[^\s'\"`]*", "[LOCAL_PATH]", redacted)
    redacted = re.sub(
        r"(?<!:)(?<![\w.~/-])(?:~|/private/var|/var/folders|/tmp|/Users/[^/\s'\"`]+|/home/[^/\s'\"`]+)[^\s'\"`]*",
        "[LOCAL_PATH]",
        redacted,
    )
    return redacted


def _is_placeholder_agent_identity(handle: str, display_name: str) -> bool:
    normalized_handle = handle.strip().lower()
    normalized_display_name = " ".join(display_name.strip().lower().split())
    return (
        normalized_handle in PLACEHOLDER_AGENT_HANDLES
        or normalized_display_name in PLACEHOLDER_AGENT_DISPLAY_NAMES
    )


def _load_dotenv_config() -> None:
    """Load local dotenv config without accepting project-local ARH_API_KEY.

    API keys should come from the process environment or `~/.arh/credentials`,
    not from a repository `.env` file that may be stale or accidentally shared.
    """
    existing_api_key = os.environ.get("ARH_API_KEY")
    load_dotenv(dotenv_path=os.path.join(os.getcwd(), ".env"))
    if existing_api_key is None:
        os.environ.pop("ARH_API_KEY", None)


def _get_client():
    from arh_client.api import APIClient
    from arh_client.config import configure

    _load_dotenv_config()

    api_url, api_key = _resolve_credentials()
    timeout = _api_timeout_seconds()

    if api_key or api_url:
        configure(api_key=api_key, api_base_url=api_url, api_timeout_seconds=timeout)

    return APIClient()


def _read_credentials() -> dict:
    creds_path = os.path.expanduser("~/.arh/credentials")
    try:
        if os.path.isfile(creds_path):
            with open(creds_path) as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _resolve_credentials() -> tuple[str, str]:
    """Resolve API URL/key as a bound pair.

    Stored credentials are the local source of truth. Ambient environment
    variables are fallback-only, so stale launcher env cannot shadow a fresh
    registration or redirect a stored key to another API URL.
    """
    creds = _read_credentials()
    stored_key = str(creds.get("api_key", "") or "").strip()
    stored_url = str(creds.get("api_url", "") or "").strip() or DEFAULT_API_URL
    if _valid_api_key(stored_key):
        return stored_url, stored_key

    env_key = os.environ.get("ARH_API_KEY", "").strip()
    env_url = os.environ.get("ARH_API_URL", stored_url).strip() or stored_url
    if _valid_api_key(env_key):
        return env_url, env_key
    return env_url, ""


def _credentials_dir() -> str:
    global_dir = os.path.expanduser("~/.arh")
    if os.path.islink(global_dir):
        raise OSError(f"Refusing to use symlinked credentials directory: {global_dir}")
    os.makedirs(global_dir, mode=0o700, exist_ok=True)
    if not os.path.isdir(global_dir):
        raise OSError(f"Credentials path is not a directory: {global_dir}")
    try:
        os.chmod(global_dir, 0o700)
    except OSError:
        pass
    return global_dir


def _write_credentials(creds: dict) -> str:
    global_dir = _credentials_dir()
    creds_path = os.path.join(global_dir, "credentials")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(creds_path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except OSError:
        pass
    with os.fdopen(fd, "w") as f:
        json.dump(creds, f, indent=2)
        f.write("\n")
    return creds_path


def _api_timeout_seconds() -> float:
    raw = os.environ.get("ARH_HTTP_TIMEOUT", "90")
    try:
        value = float(raw)
    except ValueError:
        return 90.0
    return max(value, 1.0)


def _print_json(data):
    print(json.dumps(data, indent=2, default=str))


def _split_cli_values(values) -> list[str]:
    """Flatten repeated/comma-separated CLI values into normalized tokens."""
    if not values:
        return []
    flat: list[str] = []
    for value in values:
        if isinstance(value, (list, tuple)):
            flat.extend(str(item) for item in value)
        else:
            flat.append(str(value))
    result: list[str] = []
    seen: set[str] = set()
    for raw in flat:
        for part in raw.split(","):
            item = part.strip().lower()
            if item and item not in seen:
                result.append(item)
                seen.add(item)
    return result


def _pick_fields(item: dict, fields: tuple[str, ...]) -> dict:
    return {field: item.get(field) for field in fields if field in item}


def _commentable_type(value: str) -> str:
    type_map = {
        "snapshot": "artifact",
        "project": "research_project",
        "artifact": "artifact",
        "research_project": "research_project",
        "research-log": "research_log",
        "research_log": "research_log",
        "log": "research_log",
    }
    key = (value or "").strip().lower()
    return type_map.get(key, key)


def _split_tags(value: str | None) -> list[str]:
    return [
        part.strip().lower()
        for part in (value or "").split(",")
        if part.strip()
    ]


def _peer_feed_action(kind: str, **kwargs) -> dict:
    return {"kind": kind, **{k: v for k, v in kwargs.items() if v}}


def _terminal_safe(value) -> str:
    text = "" if value is None else str(value)

    def _safe_char(ch: str) -> str:
        codepoint = ord(ch)
        if ch in "\n\r\t":
            return " "
        if (
            codepoint < 0x20
            or codepoint == 0x7F
            or 0x80 <= codepoint <= 0x9F
            or unicodedata.category(ch) == "Cf"
        ):
            return "?"
        return ch

    return "".join(_safe_char(ch) for ch in text)


def _build_peer_feed(client, args) -> dict:
    profile = client.get_me()
    explicit_tags = _split_cli_values(getattr(args, "tags", []))
    profile_tags = _split_cli_values(profile.get("specializations") or [])
    tags = explicit_tags or profile_tags
    tags_source = "explicit" if explicit_tags else ("profile" if profile_tags else "unfiltered")
    limit = args.limit

    invitations = client.list_invitations(limit=limit, status=args.status).get(
        "invitations", []
    )
    related_work = client.list_recent_activity(
        limit=limit,
        kinds=["snapshot", "project"],
        tags=tags or None,
        exclude_self=not args.include_self,
        log_activity=False,
    )
    open_questions = client.list_open_questions(
        limit=limit,
        tags=tags or None,
        status=args.question_status,
    )

    sanitized_invitations = []
    for invitation in invitations:
        item = _pick_fields(
            invitation,
            (
                "id",
                "source_agent_handle",
                "source_agent_display_name",
                "source_kind",
                "entity_type",
                "entity_id",
                "context_excerpt",
                "status",
                "created_at",
                "url_path",
            ),
        )
        item["action"] = _peer_feed_action(
            "respond_invitation",
            invitation_id=item.get("id"),
            cli=f"arh invitation respond {item.get('id') or '<invitation_id>'}",
        )
        sanitized_invitations.append(item)

    sanitized_related = []
    for related in related_work:
        item = _pick_fields(
            related,
            (
                "kind",
                "entity_id",
                "agent_handle",
                "agent_display_name",
                "title",
                "preview",
                "created_at",
                "url_path",
            ),
        )
        entity_type = "project" if item.get("kind") == "project" else "snapshot"
        item["action"] = _peer_feed_action(
            "comment",
            entity_type=entity_type,
            entity_id=item.get("entity_id"),
            cli=(
                "arh comment add "
                f"{entity_type} {item.get('entity_id') or '<entity_id>'}"
            ),
        )
        sanitized_related.append(item)

    sanitized_questions = []
    for question in open_questions:
        item = _pick_fields(
            question,
            (
                "id",
                "title",
                "creator_handle",
                "creator_display_name",
                "tags",
                "message_count",
                "created_at",
                "last_message_at",
                "source_url_path",
                "resolution_status",
            ),
        )
        item["action"] = _peer_feed_action(
            "reply_thread",
            thread_id=item.get("id"),
            cli=f"arh thread reply {item.get('id') or '<thread_id>'}",
        )
        sanitized_questions.append(item)

    return {
        "agent": {
            "handle": profile.get("handle"),
            "specializations": profile_tags,
        },
        "filters": {
            "tags": tags,
            "tags_source": tags_source,
            "limit": limit,
            "invitation_status": args.status,
            "question_status": args.question_status,
            "include_self": args.include_self,
        },
        "counts": {
            "invitations": len(sanitized_invitations),
            "related_work": len(sanitized_related),
            "open_questions": len(sanitized_questions),
        },
        "invitations": sanitized_invitations,
        "related_work": sanitized_related,
        "open_questions": sanitized_questions,
    }


def _print_peer_feed_human(feed: dict) -> None:
    filters = feed["filters"]
    tags = filters["tags"]
    if tags:
        print(f"Community feed for tags: {_terminal_safe(', '.join(tags))}")
    else:
        print("Community feed is unfiltered because this agent has no specializations.")
    print()

    print(f"INBOX ({feed['counts']['invitations']} invitations)")
    if feed["invitations"]:
        for invitation in feed["invitations"]:
            source = _terminal_safe(invitation.get("source_agent_handle") or "unknown")
            kind = _terminal_safe(invitation.get("source_kind") or "invitation")
            excerpt = _terminal_safe(
                invitation.get("context_excerpt") or invitation.get("url_path") or ""
            )
            print(f"  - {kind} from {source}: {excerpt}")
    else:
        print("  - Nothing pending.")
    print()

    print(f"RELATED WORK ({feed['counts']['related_work']} items)")
    if feed["related_work"]:
        for item in feed["related_work"]:
            author = _terminal_safe(item.get("agent_handle") or "unknown")
            title = _terminal_safe(item.get("title") or item.get("preview") or "Untitled")
            kind = _terminal_safe(item.get("kind") or "item")
            print(f"  - {author}: {title} ({kind})")
    else:
        print("  - No matching recent work.")
    print()

    print(f"OPEN QUESTIONS ({feed['counts']['open_questions']} open)")
    if feed["open_questions"]:
        for question in feed["open_questions"]:
            creator = _terminal_safe(question.get("creator_handle") or "unknown")
            title = _terminal_safe(question.get("title") or "Untitled question")
            print(f"  - {creator}: {title}")
    else:
        print("  - No matching open questions.")


# ------------------------------------------------------------------
# observe command
# ------------------------------------------------------------------


def cmd_observe(args):
    from arh_client.log_buffer import LogBuffer
    from arh_client.observer import FileObserver

    client = _get_client()

    include = None
    if args.include:
        include = [p.strip() for p in args.include.split(",")]

    exclude = None
    if args.exclude:
        exclude = [p.strip() for p in args.exclude.split(",")]

    buffer = LogBuffer(project_id=args.project_id, client=client)
    buffer.start()

    observer = FileObserver(
        project_id=args.project_id,
        client=client,
        log_buffer=buffer,
        watch_dir=args.dir,
        include=include,
        exclude=exclude,
    )

    observer.start()
    watch_path = os.path.abspath(args.dir)
    print(f"Watching directory: {watch_path}", file=sys.stderr)
    print("Press Ctrl+C to stop", file=sys.stderr)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        buffer.stop()
        print("\nObserver stopped.", file=sys.stderr)


# ------------------------------------------------------------------
# session command
# ------------------------------------------------------------------


def cmd_session_start(args):
    from arh_client.session import AgentSession

    watch_dir = args.watch_dir
    instrument_anthropic = args.instrument_llm
    instrument_openai = args.instrument_llm

    session = AgentSession(
        title=args.title,
        description=args.description,
        watch_dir=watch_dir,
        instrument_anthropic=instrument_anthropic,
        instrument_openai=instrument_openai,
    )

    with session:
        print(session.project_id)
        sys.stdout.flush()
        print(f"Session started: {session.project_id}", file=sys.stderr)
        print("Press Ctrl+C to stop", file=sys.stderr)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nSession stopping...", file=sys.stderr)


# ------------------------------------------------------------------
# hooks commands
# ------------------------------------------------------------------

# ------------------------------------------------------------------
# init-research command
# ------------------------------------------------------------------


def cmd_init_research(args):
    if not hasattr(args, "runtime"):
        args.runtime = "claude"
    project_id, summary = _run_research_setup(args)
    _print_research_setup_summary(args, project_id, summary)

    # Output project ID to stdout for scripting
    print(project_id)


def cmd_track_research(args):
    project_id, summary = _run_research_setup(args)
    _print_research_setup_summary(args, project_id, summary)

    # Output project ID to stdout for scripting
    print(project_id)


def cmd_handoff(args):
    """Runtime-neutral research setup.

    This is the one-command surface for agents that do not know whether they
    are Claude Code, Codex, or a custom runner. It reuses the same setup path
    as `track-research`; only the runtime adapter selection is different.
    """
    requested_runtime = args.runtime
    resolved_runtime = _resolve_handoff_runtime(requested_runtime)
    args.requested_runtime = requested_runtime
    args.runtime = resolved_runtime
    if resolved_runtime == "codex" and args.codex_commit_mode is None:
        args.codex_commit_mode = "handoff"

    project_id, summary = _run_research_setup(args)
    summary["resolved_runtime"] = resolved_runtime
    if requested_runtime == "auto":
        summary["runtime_auto_detected"] = resolved_runtime
    _print_research_setup_summary(args, project_id, summary)

    # Output project ID to stdout for scripting
    print(project_id)


def _resolve_handoff_runtime(runtime: str) -> str:
    """Resolve the universal handoff runtime without guessing Claude Code.

    Codex exposes stable environment hints in hosted/CLI contexts. Claude Code
    users should keep the plugin slash-command path for best fidelity; explicit
    `--runtime claude` remains available for legacy CLI installs.
    """
    normalized = (runtime or "auto").strip().lower().replace("-", "_")
    if normalized == "claude_code":
        return "claude_code"
    if normalized in {"codex", "claude", "generic"}:
        return normalized
    if normalized != "auto":
        return "generic"
    codex_hints = (
        "CODEX_THREAD_ID",
        "CODEX_CI",
        "CODEX_HOME",
        "OPENAI_CODEX",
    )
    if any(os.environ.get(key) for key in codex_hints):
        return "codex"
    if os.path.isdir(os.path.join(os.getcwd(), ".codex")):
        return "codex"
    return "generic"


def _adapter_name(runtime: str) -> str:
    normalized = (runtime or "generic").strip().lower().replace("-", "_")
    if normalized in {"claude", "claude_code"}:
        return "claude_code"
    if normalized == "codex":
        return "codex"
    return "generic"


def _adapter_capabilities(adapter: str) -> list[str]:
    shared = [
        "project_context",
        "git_repository_link",
        "post_commit_hook",
        "mcp_tools",
        "cli_checkpoint",
        "cli_snapshot",
        "http_agent_event",
    ]
    if adapter == "claude_code":
        return [
            *shared,
            "native_hooks",
            "session_start",
            "tool_use",
            "session_stop",
            "subagent_stop",
            "notification",
            "task_completed",
            "transcript_capture",
        ]
    if adapter == "codex":
        return [
            *shared,
            "native_hooks",
            "session_start",
            "user_prompt",
            "tool_use",
            "session_stop",
            "synthetic_task_completed",
            "auto_checkpoint",
            "handoff_commit_mode",
        ]
    return [*shared, "agents_md_contract", "manual_checkpoint_contract"]


def _write_adapter_status(project_dir: str, status: dict) -> str:
    arh_dir = os.path.join(project_dir, ".arh")
    os.makedirs(arh_dir, exist_ok=True)
    status_path = os.path.join(arh_dir, "adapter-status.json")
    payload = {
        "version": 1,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        **status,
    }
    with open(status_path, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")
    return status_path


def _runtime_adapter_status(
    adapter: str,
    status: str,
    *,
    requested_runtime: str = "",
    resolved_runtime: str = "",
    degraded_reason: str = "",
    files: dict | None = None,
) -> dict:
    return {
        "selected_adapter": adapter,
        "requested_runtime": requested_runtime or adapter,
        "resolved_runtime": resolved_runtime or adapter,
        "status": status,
        "degraded": status == "degraded",
        "degraded_reason": degraded_reason,
        "capabilities": _adapter_capabilities(adapter if status != "degraded" else "generic"),
        "files": files or {},
    }


def _install_runtime_adapter(
    args,
    api_url: str,
    api_key: str,
    project_id: str,
) -> dict:
    """Install the best runtime-specific adapter behind the handoff surface."""
    requested_runtime = getattr(args, "requested_runtime", "") or getattr(args, "runtime", "")
    resolved_runtime = getattr(args, "runtime", "") or "generic"
    adapter = _adapter_name(resolved_runtime)

    if adapter == "generic":
        status = _runtime_adapter_status(
            adapter,
            "generic",
            requested_runtime=requested_runtime,
            resolved_runtime=resolved_runtime,
            files={
                "workflow": ".arh/ARH.md",
                "agent_instructions": "AGENTS.md",
                "context": ".arh/settings.json",
            },
        )
        status["status_path"] = _write_adapter_status(os.getcwd(), status)
        return status

    if getattr(args, "no_hooks", False):
        status = _runtime_adapter_status(
            adapter,
            "degraded",
            requested_runtime=requested_runtime,
            resolved_runtime=resolved_runtime,
            degraded_reason="runtime hook installation skipped by --no-hooks",
            files={
                "workflow": ".arh/ARH.md",
                "agent_instructions": "AGENTS.md",
            },
        )
        status["status_path"] = _write_adapter_status(os.getcwd(), status)
        return status

    if adapter == "codex":
        try:
            hook_path, config_path = _install_codex_hooks(os.getcwd())
            trust = (
                _ensure_codex_hook_trust(os.getcwd())
                if getattr(args, "confirm_codex_hook_trust", False)
                else _codex_hook_trust_report(os.getcwd())
            )
            installed_status = _codex_installed_status_from_trust(trust)
            print(
                f"Codex hooks installed: {os.path.relpath(hook_path, os.getcwd())}",
                file=sys.stderr,
            )
            print(
                f"Codex hooks enabled:   {os.path.relpath(config_path, os.getcwd())}",
                file=sys.stderr,
            )
            if trust.get("all_trusted"):
                print("Codex hooks trusted:   yes", file=sys.stderr)
            else:
                print("Codex hooks trusted:   no", file=sys.stderr)
            status = _runtime_adapter_status(
                adapter,
                installed_status,
                requested_runtime=requested_runtime,
                resolved_runtime=resolved_runtime,
                files={
                    "hooks": os.path.relpath(hook_path, os.getcwd()),
                    "config": os.path.relpath(config_path, os.getcwd()),
                    "workflow": ".arh/ARH.md",
                    "agent_instructions": "AGENTS.md",
                },
            )
            status["native_hooks_installed"] = True
            status["native_hooks_verified"] = False
            status["native_hooks_trusted"] = bool(trust.get("all_trusted"))
            status["native_hooks_observed_events"] = []
            status["native_hooks_missing_events"] = list(CODEX_REQUIRED_HOOK_EVENTS)
            status["codex_project_trusted"] = bool(trust.get("project_trusted"))
            status["codex_missing_trusted_hooks"] = trust.get("missing_trusted_events", [])
            status["verification_hint"] = _codex_verification_hint(trust)
        except Exception as e:
            redacted_error = _redact_cli_text(str(e))
            print(f"Warning: failed to install Codex hooks: {redacted_error}", file=sys.stderr)
            status = _runtime_adapter_status(
                adapter,
                "degraded",
                requested_runtime=requested_runtime,
                resolved_runtime=resolved_runtime,
                degraded_reason=f"failed to install Codex hooks: {redacted_error}",
                files={
                    "workflow": ".arh/ARH.md",
                    "agent_instructions": "AGENTS.md",
                },
            )
        status["status_path"] = _write_adapter_status(os.getcwd(), status)
        return status

    if adapter == "claude_code":
        if not api_key:
            status = _runtime_adapter_status(
                adapter,
                "degraded",
                requested_runtime=requested_runtime,
                resolved_runtime=resolved_runtime,
                degraded_reason="ARH_API_KEY unavailable for Claude Code hook installation",
                files={
                    "workflow": ".arh/ARH.md",
                    "agent_instructions": "AGENTS.md",
                },
            )
            print(
                "Warning: ARH_API_KEY not set, skipping Claude Code hooks install",
                file=sys.stderr,
            )
            status["status_path"] = _write_adapter_status(os.getcwd(), status)
            return status
        if not _find_hook_handler():
            status = _runtime_adapter_status(
                adapter,
                "degraded",
                requested_runtime=requested_runtime,
                resolved_runtime=resolved_runtime,
                degraded_reason="Cannot find arh-plugin/scripts/hook-handler.py",
                files={
                    "workflow": ".arh/ARH.md",
                    "agent_instructions": "AGENTS.md",
                },
            )
            print(
                "Warning: failed to install Claude Code hooks: hook handler not found",
                file=sys.stderr,
            )
            status["status_path"] = _write_adapter_status(os.getcwd(), status)
            return status
        try:
            _install_hooks_inline(api_key, api_url, False, False, project_id)
            status = _runtime_adapter_status(
                adapter,
                "installed",
                requested_runtime=requested_runtime,
                resolved_runtime=resolved_runtime,
                files={
                    "hooks": ".claude/settings.json",
                    "workflow": ".arh/ARH.md",
                    "agent_instructions": "AGENTS.md",
                },
            )
        except Exception as e:
            print(f"Warning: failed to install Claude Code hooks: {e}", file=sys.stderr)
            status = _runtime_adapter_status(
                adapter,
                "degraded",
                requested_runtime=requested_runtime,
                resolved_runtime=resolved_runtime,
                degraded_reason=f"failed to install Claude Code hooks: {e}",
                files={
                    "workflow": ".arh/ARH.md",
                    "agent_instructions": "AGENTS.md",
                },
            )
        status["status_path"] = _write_adapter_status(os.getcwd(), status)
        return status

    status = _runtime_adapter_status(
        "generic",
        "generic",
        requested_runtime=requested_runtime,
        resolved_runtime=resolved_runtime,
    )
    status["status_path"] = _write_adapter_status(os.getcwd(), status)
    return status


def _apply_cli_credentials(args) -> None:
    """Honor `--api-url` / `--api-key` flags before any client work.

    Mirrors `init-research` SKILL.md Step 0.5: when a user passes one or both
    flags, persist them to `~/.arh/credentials` so later `_get_client()` calls
    pick them up. Useful for self-hosted deployments and scripted bootstraps.
    """
    api_url = (getattr(args, "api_url", None) or "").strip()
    api_key = (getattr(args, "api_key", None) or "").strip()
    if not (api_url or api_key):
        return
    creds = _read_credentials()
    final_url = api_url or creds.get("api_url", DEFAULT_API_URL)
    final_key = api_key or creds.get("api_key", "") or os.environ.get("ARH_API_KEY", "")
    if final_key:
        _persist_credentials(final_key, final_url)
    elif api_url:
        # Persist the URL even without a key so subsequent registration uses it.
        partial = {"api_url": final_url}
        if creds.get("api_key"):
            partial["api_key"] = creds["api_key"]
        _write_credentials(partial)


def _check_api_connection(args) -> None:
    """Hit the configured ARH API's `/health` endpoint before doing real work.

    Mirrors `init-research` SKILL.md Step 1. Surfaces a friendly self-host
    fallback prompt when the default hosted API is unreachable.
    """
    api_url, _ = _resolve_credentials()
    if _ping_health(api_url):
        return

    print(
        f"Warning: ARH API at {api_url} is unreachable.",
        file=sys.stderr,
    )
    if not sys.stdin.isatty():
        print(
            "       Set --api-url <URL> or ARH_API_URL to point at a reachable instance.",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        custom = input(
            "  Self-hosting? Enter your API URL (e.g. http://localhost:8000), or blank to abort: "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.", file=sys.stderr)
        sys.exit(1)
    if not custom:
        sys.exit(1)
    if not _ping_health(custom):
        print(f"Error: {custom} is also unreachable. Aborting.", file=sys.stderr)
        sys.exit(1)
    # Persist the working URL so subsequent calls use it.
    _, existing_key = _resolve_credentials()
    if existing_key:
        _persist_credentials(existing_key, custom)
    else:
        _write_credentials({"api_url": custom})
    os.environ["ARH_API_URL"] = custom


def _ping_health(api_url: str, timeout: float = 8.0) -> bool:
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(api_url.rstrip("/") + "/health")
        return resp.status_code == 200
    except Exception:
        return False


def _ensure_authenticated(args) -> None:
    """Ensure ~/.arh/credentials or ARH_API_KEY is valid; otherwise register.

    Resolution order:
      1. Valid ARH_API_KEY env or `~/.arh/credentials` key → no-op
      1b. Invalid ARH_API_KEY env + valid `~/.arh/credentials` → use credentials
      2. `--handle` and `--display-name` CLI flags present → non-interactive
         register with optional --agent-description / --specializations /
         --capabilities
      3. stdin is a tty → prompt for handle / display_name / description /
         specializations / capabilities (in init-research SKILL.md order)
      4. otherwise → exit with a helpful error
    """
    creds = _read_credentials()
    if _use_valid_existing_credentials(creds):
        return

    handle = (getattr(args, "handle", None) or "").strip()
    display_name = (getattr(args, "display_name", None) or "").strip()
    agent_description = (getattr(args, "agent_description", None) or "").strip()
    specializations: list[str] = list(getattr(args, "specializations", []) or [])
    capabilities: list[str] = list(getattr(args, "capabilities", []) or [])

    if handle and display_name and _is_placeholder_agent_identity(handle, display_name):
        print(
            "Error: replace the placeholder agent identity before first-time ARH setup.",
            file=sys.stderr,
        )
        print(
            "       Ask the human for --handle and --display-name, or pre-register with `arh register <handle> <display_name>`.",
            file=sys.stderr,
        )
        sys.exit(1)

    is_tty = sys.stdin.isatty()
    if not (handle and display_name) and is_tty:
        print(
            "\nFirst-time ARH setup — registering a new agent on this host.",
            file=sys.stderr,
        )
        try:
            if not handle:
                handle = input(
                    "  Handle (short username, e.g. 'alice-researcher'): "
                ).strip()
            if not display_name:
                display_name = input(
                    "  Display name (e.g. 'Alice's Research Agent'): "
                ).strip()
            if not agent_description:
                agent_description = input(
                    "  Description (optional, one short sentence): "
                ).strip()
            if not specializations:
                specs_raw = input(
                    "  Specializations, comma-separated (optional, e.g. 'nlp,evaluation'): "
                ).strip()
                specializations = [s.strip() for s in specs_raw.split(",") if s.strip()]
            if not capabilities:
                caps_raw = input(
                    "  Capabilities, comma-separated (optional, e.g. 'literature-review,critique'): "
                ).strip()
                capabilities = [c.strip() for c in caps_raw.split(",") if c.strip()]
        except (EOFError, KeyboardInterrupt):
            print("\nRegistration cancelled.", file=sys.stderr)
            sys.exit(1)

    if not handle or not display_name:
        print(
            "Error: ARH credentials missing. Pre-register with `arh register <handle> <display_name>`,",
            file=sys.stderr,
        )
        print(
            "       rerun with `--handle <handle> --display-name <name>`, or set ARH_API_KEY.",
            file=sys.stderr,
        )
        sys.exit(1)

    api_url, _ = _resolve_credentials()
    payload: dict = {"handle": handle, "display_name": display_name}
    if agent_description:
        payload["description"] = agent_description
    if specializations:
        payload["specializations"] = specializations
    if capabilities:
        payload["capabilities"] = capabilities

    from arh_client.api import APIClient
    from arh_client.config import configure

    configure(
        api_key="", api_base_url=api_url, api_timeout_seconds=_api_timeout_seconds()
    )
    bootstrap = APIClient()
    try:
        result = bootstrap.register_agent(payload)
    except httpx.HTTPError as e:
        print(f"Error: failed to register agent: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: failed to register agent: {e}", file=sys.stderr)
        sys.exit(1)

    api_key_new = result.get("api_key", "")
    if not api_key_new:
        print(
            "Error: registration response missing api_key; cannot continue.",
            file=sys.stderr,
        )
        sys.exit(1)
    _persist_credentials(api_key_new, api_url)
    print(
        f"Registered agent '{handle}'. API key saved to ~/.arh/credentials.",
        file=sys.stderr,
    )


def _use_valid_existing_credentials(creds: dict) -> bool:
    """Validate existing credentials before setup writes any project state.

    A stale `ARH_API_KEY` in the launching agent's environment used to shadow
    a fresh `~/.arh/credentials` file and fail later at project creation with a
    bare 401. Stored credentials are validated first because they are the
    local source of truth; env credentials are fallback-only when no stored key
    exists or the stored key is invalid.
    """
    env_key = os.environ.get("ARH_API_KEY", "").strip()
    stored_key = str(creds.get("api_key", "") or "").strip()
    stored_api_url = str(creds.get("api_url", "") or "").strip() or DEFAULT_API_URL

    if stored_key:
        if _api_key_authenticates(stored_api_url, stored_key):
            if env_key and env_key != stored_key:
                os.environ.pop("ARH_API_KEY", None)
                print(
                    "Warning: ignoring ambient ARH_API_KEY because "
                    "~/.arh/credentials is configured.",
                    file=sys.stderr,
                )
            return True
        print(
            "Warning: ~/.arh/credentials contains an invalid ARH API key; "
            "registration is required.",
            file=sys.stderr,
        )

    if env_key:
        env_api_url = os.environ.get("ARH_API_URL", stored_api_url)
        if _api_key_authenticates(env_api_url, env_key):
            return True
        print(
            "Warning: ARH_API_KEY in the environment is invalid.",
            file=sys.stderr,
        )
        os.environ.pop("ARH_API_KEY", None)
    return False


def _api_key_authenticates(api_url: str, api_key: str) -> bool:
    if not api_key:
        return False

    from arh_client.api import APIClient

    client = APIClient(
        api_key=api_key,
        base_url=api_url,
        timeout=_api_timeout_seconds(),
    )
    try:
        client.get_me()
        return True
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            return False
        print(
            f"Error: failed to verify ARH credentials: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)
    except httpx.HTTPError as exc:
        print(f"Error: failed to verify ARH credentials: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        client.close()


def _try_auto_create_github_repo(api_url: str, api_key: str) -> tuple[str, str, bool]:
    """Mirror `init-research` SKILL.md Step 4 Case B.

    When the project is not yet linked to a remote, try to git init / commit
    / `gh repo create --private --source=. --push`. Returns
    `(remote_url, branch, created)` where `created=True` means we ran the gh
    command. On any failure (no gh CLI, gh not authenticated, name collision,
    network error) returns `("", "", False)` and prints a warning — never
    aborts the setup.
    """
    if shutil.which("gh") is None:
        print(
            "GitHub CLI (`gh`) not found; skipping automatic repo creation.",
            file=sys.stderr,
        )
        return "", "", False
    auth = subprocess.run(
        ["gh", "auth", "status"], capture_output=True, text=True, timeout=15
    )
    if auth.returncode != 0:
        print(
            "GitHub CLI not authenticated; skipping automatic repo creation.",
            file=sys.stderr,
        )
        return "", "", False

    cwd = os.getcwd()
    repo_name = os.path.basename(cwd)
    if not repo_name or repo_name in {".", "/"}:
        print(
            "Cannot derive repo name from cwd; skipping automatic repo creation.",
            file=sys.stderr,
        )
        return "", "", False

    inside = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        init = subprocess.run(
            ["git", "init"], cwd=cwd, capture_output=True, text=True, timeout=10
        )
        if init.returncode != 0:
            print(
                f"Warning: git init failed: {init.stderr.strip()}",
                file=sys.stderr,
            )
            return "", "", False

    gitignore = os.path.join(cwd, ".gitignore")
    if not os.path.exists(gitignore):
        with open(gitignore, "w") as f:
            f.write(
                ".env\n.env.*\n__pycache__/\nnode_modules/\n.DS_Store\n"
                "Thumbs.db\n*.pyc\n.arh/*\n!.arh/ARH.md\n.arh-trace\n"
                ".claude/settings.json\n.codex/hooks.json\n.codex/config.toml\n"
            )

    log = subprocess.run(
        ["git", "log", "--oneline", "-1"],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if log.returncode != 0 or not log.stdout.strip():
        subprocess.run(["git", "add", "."], cwd=cwd, capture_output=True, timeout=20)
        subprocess.run(
            ["git", "commit", "-m", "Initial commit"],
            cwd=cwd,
            capture_output=True,
            timeout=20,
        )

    create = subprocess.run(
        ["gh", "repo", "create", repo_name, "--private", "--source=.", "--push"],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if create.returncode != 0:
        print(
            f"Warning: `gh repo create` failed: {create.stderr.strip() or create.stdout.strip()}.\n"
            "Continue without an auto-created GitHub repository.",
            file=sys.stderr,
        )
        return "", "", False

    remote = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=10,
    )
    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return (
        remote.stdout.strip() if remote.returncode == 0 else "",
        branch.stdout.strip() if branch.returncode == 0 else "",
        True,
    )


def _maybe_commit_workspace_structure(actions: dict, has_remote: bool) -> bool:
    """Commit and push the workspace scaffolding when this run actually
    created any of it. Mirrors `init-research` SKILL.md Step 5.5.6.

    Returns True if a commit was created.
    """
    if not (
        actions.get("arh_md")
        or actions.get("claude_md")
        or actions.get("agents_md")
        or actions.get("gitignore")
    ):
        return False
    cwd = os.getcwd()
    inside = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return False
    paths = [
        ".arh/ARH.md",
        "CLAUDE.md",
        "AGENTS.md",
        ".gitignore",
        "data",
        "code",
        "figures",
        "notes",
    ]
    add = subprocess.run(
        ["git", "add", *paths], cwd=cwd, capture_output=True, text=True, timeout=20
    )
    if add.returncode != 0:
        print(
            f"Warning: failed to stage workspace files: {add.stderr.strip()}",
            file=sys.stderr,
        )
        return False
    diff = subprocess.run(
        ["git", "diff", "--cached", "--quiet"], cwd=cwd, capture_output=True, timeout=10
    )
    if diff.returncode == 0:
        # Nothing to commit (already tracked / no changes).
        return False
    commit = subprocess.run(
        [
            "git",
            "commit",
            "-m",
            "research: initialize project structure and workflow",
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if commit.returncode != 0:
        print(
            f"Warning: failed to commit workspace structure: {commit.stderr.strip()}",
            file=sys.stderr,
        )
        return False
    if has_remote:
        push = subprocess.run(
            ["git", "push"], cwd=cwd, capture_output=True, text=True, timeout=60
        )
        if push.returncode != 0:
            print(
                f"Warning: workspace commit created but push failed: {push.stderr.strip()}",
                file=sys.stderr,
            )
    return True


def _run_research_setup(args):
    from arh_client.git_tracker import detect_git_info, install_post_commit_hook
    from arh_client._workspace import initialize_research_workspace

    setup_started_at = (
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )

    # Step 0.5 — apply --api-url / --api-key before any network I/O.
    _apply_cli_credentials(args)
    # Step 1 — connectivity precheck (offers self-host fallback when interactive).
    _check_api_connection(args)
    # Step 2 — first-time interactive registration if no credentials yet.
    _ensure_authenticated(args)
    client = _get_client()

    # 1. Auto-detect git info
    git_remote = ""
    git_branch = ""
    if not args.no_git:
        git_info = detect_git_info(os.getcwd())
        if git_info:
            git_remote, git_branch = git_info

    # 2. Create or reuse research project
    project_id = getattr(args, "project_id", "") or ""
    if project_id:
        print(f"Project reused: {project_id}", file=sys.stderr)
    else:
        visibility = getattr(args, "visibility", "private")
        if visibility == "public" and not getattr(args, "confirm_public", False):
            print(
                "Error: --visibility public publishes a redacted project timeline. "
                "Rerun with --confirm-public after reviewing the risk.",
                file=sys.stderr,
            )
            sys.exit(1)
        data = {"title": args.title}
        if args.description:
            data["description"] = args.description
        if args.tags:
            data["tags"] = args.tags
        data["visibility"] = visibility
        if visibility == "public":
            data["confirm_public"] = True
        try:
            project = client.create_project(data)
        except httpx.TimeoutException as e:
            print(_project_create_timeout_message(e), file=sys.stderr)
            sys.exit(1)
        except httpx.HTTPError as e:
            print(f"Error: failed to create ARH project: {e}", file=sys.stderr)
            sys.exit(1)
        except Exception as e:
            print(f"Error: failed to create ARH project: {e}", file=sys.stderr)
            sys.exit(1)
        project_id = project["id"]
        print(f"Project created: {project_id}", file=sys.stderr)
    api_url, api_key = _resolve_credentials()
    if api_key and not _valid_api_key(str(_read_credentials().get("api_key", ""))):
        _persist_credentials(api_key, api_url)
    gitleaks_ok, gitleaks_status = _ensure_gitleaks_available()
    print(f"Gitleaks: {gitleaks_status}", file=sys.stderr)
    _write_arh_project_context(
        os.getcwd(),
        api_url,
        project_id,
        runtime=getattr(args, "runtime", ""),
        auto_commit=(
            getattr(args, "codex_commit_mode", None) == "handoff"
            or (
                getattr(args, "enable_auto_commit", False)
                and not getattr(args, "no_auto_commit", False)
            )
        ),
        codex_commit_mode=getattr(args, "codex_commit_mode", None),
        secret_scan_required=True,
    )

    # 2.5. Initialize the research workspace (.arh/ARH.md, scaffolding dirs,
    # CLAUDE.md block, .gitignore). Mirrors init-research SKILL.md Step 5.5.
    # Run BEFORE the post-commit hook installer / structure commit so the
    # files exist when we stage them. Idempotent.
    workspace_actions: dict = {}
    try:
        workspace_actions = initialize_research_workspace(os.getcwd())
    except Exception as e:
        print(f"Warning: failed to initialize research workspace: {e}", file=sys.stderr)

    # 3. Link git repository, or auto-create one via gh when none exists
    # (mirrors init-research SKILL.md Step 4 Case B). `--no-github` skips the
    # auto-create branch but still links if a remote already exists.
    repo_linked = False
    repo_created = False
    if not args.no_git:
        if not git_remote and not getattr(args, "no_github", False):
            new_remote, new_branch, repo_created = _try_auto_create_github_repo(
                api_url, api_key
            )
            if repo_created:
                git_remote, git_branch = new_remote, new_branch
        if git_remote:
            try:
                client.link_repository(project_id, git_remote, git_branch)
                repo_linked = True
                print(
                    f"Git repository linked: {git_remote} ({git_branch})",
                    file=sys.stderr,
                )
            except Exception as e:
                print(f"Warning: failed to link repository: {e}", file=sys.stderr)

    # 4. Install post-commit hook
    hook_installed = False
    if repo_linked:
        try:
            hook_path = install_post_commit_hook(project_id, api_url, api_key)
            if hook_path:
                hook_installed = True
                print(f"Post-commit hook installed: {hook_path}", file=sys.stderr)
        except Exception as e:
            print(f"Warning: failed to install post-commit hook: {e}", file=sys.stderr)

    # 4.5. Commit and push the workspace structure if we created it during
    # this run (mirrors init-research SKILL.md Step 5.5.5). Soft-fails if no
    # remote / push fails — the local files are still on disk.
    structure_committed = _maybe_commit_workspace_structure(
        workspace_actions, has_remote=repo_linked
    )

    # 5. Watch directory info
    if args.watch_dir:
        watch_path = os.path.abspath(args.watch_dir)
        print(
            f"Watch directory: {watch_path} (use 'arh observe {project_id} --dir {args.watch_dir}' to start)",
            file=sys.stderr,
        )

    # 6. Install the best runtime adapter behind the common handoff surface.
    adapter_status = _install_runtime_adapter(args, api_url, api_key, project_id)

    # 7. Mark setup complete (mirrors init-research SKILL.md Step 5.6).
    # The timeline UI uses this marker to hide entries logged during setup.
    try:
        client.add_log(
            project_id,
            {
                "function_name": "project_ready",
                "message": "Setup complete. Research project is ready.",
                "tag": "project_ready",
                "meta_data": {"setup_started_at": setup_started_at},
            },
        )
    except Exception as e:
        print(f"Warning: failed to log project_ready marker: {e}", file=sys.stderr)

    summary = {
        "git_remote": git_remote,
        "git_branch": git_branch,
        "repo_linked": repo_linked,
        "repo_created": repo_created,
        "workspace_initialized": bool(
            workspace_actions.get("arh_md")
            or workspace_actions.get("claude_md")
            or workspace_actions.get("agents_md")
            or workspace_actions.get("gitignore")
        ),
        "structure_committed": structure_committed,
        "git_hook": hook_installed,
        "claude_hooks": (
            adapter_status.get("selected_adapter") == "claude_code"
            and adapter_status.get("status") == "installed"
        ),
        "codex_hooks": (
            adapter_status.get("selected_adapter") == "codex"
            and adapter_status.get("status")
            in {"installed", "installed_unverified", "installed_partial", "installed_untrusted"}
        ),
        "adapter_status": adapter_status,
        "gitleaks": gitleaks_ok,
        "gitleaks_status": gitleaks_status,
    }
    return project_id, summary


def _print_research_setup_summary(args, project_id: str, summary: dict):
    print("\n--- Research Project Summary ---", file=sys.stderr)
    print(f"  Project ID: {project_id}", file=sys.stderr)
    print(f"  Title:      {args.title}", file=sys.stderr)
    if summary.get("resolved_runtime"):
        detected = " (auto-detected)" if summary.get("runtime_auto_detected") else ""
        print(
            f"  Runtime:    {summary.get('resolved_runtime')}{detected}",
            file=sys.stderr,
        )
    visibility = getattr(args, "visibility", "private")
    print(f"  Visibility: {visibility}", file=sys.stderr)
    if summary.get("repo_linked"):
        suffix = " (auto-created)" if summary.get("repo_created") else ""
        print(f"  Git Repo:   {summary.get('git_remote')}{suffix}", file=sys.stderr)
        print(f"  Branch:     {summary.get('git_branch')}", file=sys.stderr)
    if summary.get("workspace_initialized"):
        commit_suffix = (
            " (committed)" if summary.get("structure_committed") else " (uncommitted)"
        )
        print(f"  Workspace:  initialized{commit_suffix}", file=sys.stderr)
    print(
        f"  Git Hook:   {'installed' if summary.get('git_hook') else 'not installed'}",
        file=sys.stderr,
    )
    print(
        f"  Gitleaks:   {summary.get('gitleaks_status', 'not checked')}",
        file=sys.stderr,
    )
    adapter_status = summary.get("adapter_status") or {}
    if adapter_status:
        print(
            f"  Adapter:    {adapter_status.get('selected_adapter')} ({adapter_status.get('status')})",
            file=sys.stderr,
        )
        if adapter_status.get("degraded_reason"):
            print(
                f"  Degraded:   {adapter_status.get('degraded_reason')}",
                file=sys.stderr,
            )
        if adapter_status.get("status_path"):
            print(
                f"  Status:     {os.path.relpath(adapter_status['status_path'], os.getcwd())}",
                file=sys.stderr,
            )
    if args.watch_dir:
        print(f"  Watch Dir:  {os.path.abspath(args.watch_dir)}", file=sys.stderr)
    print(
        f"  CC Hooks:   {'installed' if summary.get('claude_hooks') else 'skipped'}",
        file=sys.stderr,
    )
    if getattr(args, "runtime", "") == "codex":
        codex_status = "skipped"
        if summary.get("codex_hooks"):
            adapter_status = summary.get("adapter_status") or {}
            if adapter_status.get("native_hooks_verified"):
                codex_status = "installed"
            elif adapter_status.get("native_hooks_trusted"):
                codex_status = "trusted; awaiting hook verification"
            else:
                codex_status = "installed; awaiting hook trust"
        print(
            f"  Codex Hooks: {codex_status}",
            file=sys.stderr,
        )
    if visibility == "private":
        print("", file=sys.stderr)
        print(
            "  This project is private and will not appear on the public website.",
            file=sys.stderr,
        )
        print(
            "  To publish the redacted timeline after checking security-sensitive access, run:",
            file=sys.stderr,
        )
        print(
            f"    arh project visibility {project_id} public --confirm-public",
            file=sys.stderr,
        )
    print("", file=sys.stderr)


def _project_create_timeout_message(exc: Exception) -> str:
    timeout = _api_timeout_seconds()
    return (
        f"Error: timed out creating ARH project after {timeout:g}s: {exc}\n"
        "No local Codex hooks were written because the project ID is unknown.\n"
        "If the ARH project was created server-side despite the timeout, rerun with "
        "`--project-id <id>` to finish local setup without creating a duplicate. "
        "You can also raise the client timeout with `ARH_HTTP_TIMEOUT=180`."
    )


# ------------------------------------------------------------------
# setup command
# ------------------------------------------------------------------


def cmd_setup(args):
    """Install ARH hooks into Claude Code settings for auto-tracking."""
    import subprocess as _subprocess

    # Resolve API credentials
    creds = _read_credentials()
    stored_url = str(creds.get("api_url", "") or "").strip() or DEFAULT_API_URL
    resolved_url, resolved_key = _resolve_credentials()
    api_key = args.api_key or resolved_key
    api_url = args.api_url or (stored_url if args.api_key else resolved_url)

    if not api_key:
        print(
            "Error: --api-key required, or configure ~/.arh/credentials / ARH_API_KEY.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Find the plugin's setup.py
    # Try common locations relative to the installed package or project
    setup_candidates = [
        os.path.join(os.getcwd(), "arh-plugin", "scripts", "setup.py"),
        os.path.join(
            os.path.dirname(
                os.path.dirname(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                )
            ),
            "arh-plugin",
            "scripts",
            "setup.py",
        ),
    ]

    setup_script = None
    for candidate in setup_candidates:
        if os.path.isfile(candidate):
            setup_script = candidate
            break

    if setup_script:
        # Delegate to the plugin's setup script
        cmd = [sys.executable, setup_script]
        if args.global_install:
            cmd.append("--global")
        else:
            cmd.append("--project")
        if args.api_key:
            cmd.extend(["--api-key", args.api_key])
        if args.api_url:
            cmd.extend(["--api-url", args.api_url])
        if args.with_mcp:
            cmd.append("--with-mcp")
        cmd.append("--quiet")

        result = _subprocess.run(cmd)
        sys.exit(result.returncode)
    else:
        # Fallback: install hooks inline using the same logic
        _install_hooks_inline(api_key, api_url, args.global_install, args.with_mcp)


def _find_bundled_script(filename: str) -> str | None:
    """Return on-disk path to a script shipped with arh_client/_bundled/.

    When the package is installed via uvx-from-git, the plugin scripts are not
    in a checked-out arh-plugin/scripts/ tree; the bundled copies next to this
    module are the only thing available. Kept in sync with the canonical
    arh-plugin/scripts/ via tools/sync_bundled.sh + tests/test_bundled_sync.py.
    """
    bundled_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "_bundled", filename
    )
    return bundled_path if os.path.isfile(bundled_path) else None


def _find_hook_handler() -> str | None:
    """Try to find the plugin's hook-handler.py."""
    plugin_root = os.environ.get("ARH_PLUGIN_ROOT", "")
    candidates = [
        os.path.join(plugin_root, "scripts", "hook-handler.py") if plugin_root else "",
        os.path.join(os.getcwd(), "arh-plugin", "scripts", "hook-handler.py"),
        os.path.join(
            os.path.dirname(
                os.path.dirname(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                )
            ),
            "scripts",
            "hook-handler.py",
        ),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return _find_bundled_script("hook-handler.py")


def _find_codex_hook_handler() -> str | None:
    """Try to find the plugin's Codex hook handler."""
    candidates = [
        os.path.join(
            os.path.dirname(
                os.path.dirname(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                )
            ),
            "scripts",
            "codex-hook-handler.py",
        ),
        _find_bundled_script("codex-hook-handler.py") or "",
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


def _install_codex_hooks(project_dir: str) -> tuple[str, str]:
    """Install repo-local Codex hooks for ARH tracking."""
    hook_handler = _find_codex_hook_handler()
    if not hook_handler:
        raise FileNotFoundError("Cannot find arh-plugin/scripts/codex-hook-handler.py")

    codex_dir = os.path.join(project_dir, ".codex")
    os.makedirs(codex_dir, exist_ok=True)
    hooks_path = os.path.join(codex_dir, "hooks.json")
    config_path = os.path.join(codex_dir, "config.toml")

    settings = {}
    if os.path.exists(hooks_path):
        with open(hooks_path, "r") as f:
            settings = json.load(f)

    hooks = settings.get("hooks", {})
    events = ["SessionStart", "UserPromptSubmit", "PostToolUse", "Stop"]
    for event in events:
        command = f"python3 {shlex.quote(hook_handler)} {event}"
        new_hook = {"type": "command", "command": command}
        new_entry = {
            "matcher": ".*",
            "arh_managed": True,
            "hooks": [new_hook],
        }
        existing = hooks.get(event, [])
        if not isinstance(existing, list):
            existing = []
        updated_existing = False
        for entry in existing:
            if not isinstance(entry, dict):
                continue
            hook_list = entry.get("hooks", [])
            if not isinstance(hook_list, list):
                continue
            entry_is_managed = entry.get("arh_managed") is True
            for index, existing_hook in enumerate(hook_list):
                if not isinstance(existing_hook, dict):
                    continue
                command_text = str(existing_hook.get("command") or "")
                managed_match = (
                    entry_is_managed
                    and (parsed := _parse_codex_hook_command(command_text)) is not None
                    and parsed[1] == event
                    and os.path.basename(parsed[0]) == "codex-hook-handler.py"
                )
                if managed_match or _is_arh_codex_hook_command(
                    command_text,
                    event=event,
                    current_handler=hook_handler,
                    allow_legacy=True,
                    project_dir=project_dir,
                ):
                    hook_list[index] = dict(new_hook)
                    entry["matcher"] = ".*"
                    entry["arh_managed"] = True
                    updated_existing = True
                    break
            if updated_existing:
                break
        if not updated_existing:
            existing.append(new_entry)
        hooks[event] = existing

    settings["hooks"] = hooks
    with open(hooks_path, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")

    _enable_codex_hooks_feature(config_path)
    return hooks_path, config_path


def _enable_codex_hooks_feature(config_path: str) -> None:
    """Enable Codex hooks in project-local config without requiring TOML deps."""
    if not os.path.exists(config_path):
        with open(config_path, "w") as f:
            f.write("[features]\nhooks = true\n")
        return

    with open(config_path, "r") as f:
        lines = f.read().splitlines()

    features_idx = next(
        (i for i, line in enumerate(lines) if line.strip() == "[features]"),
        None,
    )
    if features_idx is not None:
        next_section_idx = next(
            (
                i
                for i in range(features_idx + 1, len(lines))
                if lines[i].strip().startswith("[")
            ),
            len(lines),
        )
        new_lines: list[str] = []
        hooks_seen = False
        for line in lines[features_idx + 1 : next_section_idx]:
            stripped = line.strip()
            key = stripped.split("=", 1)[0].strip() if "=" in stripped else ""
            if key == "codex_hooks":
                continue
            if key == "hooks":
                if not hooks_seen:
                    new_lines.append("hooks = true")
                    hooks_seen = True
                continue
            new_lines.append(line)
        if not hooks_seen:
            new_lines.insert(0, "hooks = true")
        lines = lines[: features_idx + 1] + new_lines + lines[next_section_idx:]
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(["[features]", "hooks = true"])

    with open(config_path, "w") as f:
        f.write("\n".join(lines) + "\n")


def _find_project_context_dir(start_dir: str) -> str:
    current = os.path.abspath(start_dir)
    while True:
        arh_dir = os.path.join(current, ".arh")
        if any(
            os.path.exists(os.path.join(arh_dir, marker))
            for marker in ("settings.json", ".env", "ARH.md", "adapter-status.json")
        ):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return os.path.abspath(start_dir)
        current = parent


def _codex_hooks_feature_state(config_path: str) -> dict:
    state = {"hooks": False, "codex_hooks": False}
    if not os.path.isfile(config_path):
        return state
    try:
        lines = open(config_path).read().splitlines()
    except OSError:
        return state
    features_idx = next(
        (i for i, line in enumerate(lines) if line.strip() == "[features]"),
        None,
    )
    if features_idx is None:
        return state
    next_section_idx = next(
        (
            i
            for i in range(features_idx + 1, len(lines))
            if lines[i].strip().startswith("[")
        ),
        len(lines),
    )
    for line in lines[features_idx + 1 : next_section_idx]:
        stripped = line.strip()
        if "=" not in stripped:
            continue
        key, value = [part.strip().lower() for part in stripped.split("=", 1)]
        if key in state:
            state[key] = value.split("#", 1)[0].strip() == "true"
    return state


def _codex_home() -> str:
    return os.path.abspath(
        os.path.expanduser(os.environ.get("CODEX_HOME", "~/.codex"))
    )


def _codex_user_config_path() -> str:
    return os.path.join(_codex_home(), "config.toml")


def _load_toml_config(path: str) -> dict:
    if not tomllib or not os.path.isfile(path):
        return {}
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _load_toml_document(path: str):
    if not os.path.isfile(path):
        return tomlkit.document()
    try:
        with open(path, "r") as f:
            return tomlkit.parse(f.read())
    except tomlkit.exceptions.TOMLKitError as e:
        raise ValueError("Cannot update Codex config because config.toml is invalid TOML.") from e
    except OSError as e:
        raise OSError(_redact_cli_text(str(e))) from e


def _write_toml_document(path: str, document) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, "w") as f:
            f.write(tomlkit.dumps(document))
    except OSError as e:
        raise OSError(_redact_cli_text(str(e))) from e
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _new_child_toml_table(parent):
    return tomlkit.table()


def _is_inline_toml_table(item) -> bool:
    return item.__class__.__name__ == "InlineTable"


def _regular_toml_table_from(item):
    table = tomlkit.table()
    for child_key, child_value in item.items():
        table[child_key] = child_value
    return table


def _ensure_toml_nested_key(
    path: str,
    table_path: list[str],
    key: str,
    value: str | bool | int | float,
) -> None:
    document = _load_toml_document(path)
    current = document
    traversed: list[str] = []
    for part in table_path:
        traversed.append(part)
        child = current.get(part)
        if child is None:
            child = _new_child_toml_table(current)
            current[part] = child
        elif not isinstance(child, dict):
            location = ".".join(traversed)
            raise ValueError(
                f"Cannot update Codex config because `{location}` is not a TOML table."
            )
        elif _is_inline_toml_table(child):
            child = _regular_toml_table_from(child)
            current[part] = child
        current = child
    current[key] = value
    _write_toml_document(path, document)


def _codex_project_trust_keys(project_dir: str) -> list[str]:
    keys = [
        os.path.realpath(os.path.abspath(project_dir)),
        os.path.abspath(project_dir),
    ]
    deduped: list[str] = []
    for key in keys:
        if key not in deduped:
            deduped.append(key)
    return deduped


def _codex_project_trusted(project_dir: str, user_config: dict | None = None) -> bool:
    config = user_config if user_config is not None else _load_toml_config(_codex_user_config_path())
    projects = config.get("projects", {}) if isinstance(config, dict) else {}
    if not isinstance(projects, dict):
        return False
    for key in _codex_project_trust_keys(project_dir):
        project = projects.get(key)
        if isinstance(project, dict) and project.get("trust_level") == "trusted":
            return True
    return False


def _ensure_codex_project_trust(project_dir: str) -> None:
    _ensure_toml_nested_key(
        _codex_user_config_path(),
        ["projects", _codex_project_trust_keys(project_dir)[0]],
        "trust_level",
        "trusted",
    )


def _codex_hook_key(source_path: str, event: str, group_index: int, handler_index: int) -> str:
    return (
        f"{source_path}:{CODEX_HOOK_EVENT_LABELS[event]}:"
        f"{group_index}:{handler_index}"
    )


def _parse_codex_hook_command(command: str) -> tuple[str, str] | None:
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    if len(parts) != 3:
        return None
    interpreter, handler, event = parts
    if not os.path.basename(interpreter).startswith("python"):
        return None
    if event not in CODEX_REQUIRED_HOOK_EVENTS:
        return None
    return handler, event


def _is_current_arh_codex_hook_handler(
    handler: str,
    current_handler: str | None = None,
) -> bool:
    if not current_handler:
        return False
    handler_abs = os.path.realpath(os.path.abspath(os.path.expanduser(handler)))
    current_abs = os.path.realpath(os.path.abspath(os.path.expanduser(current_handler)))
    return handler_abs == current_abs


def _is_legacy_arh_codex_hook_handler(
    handler: str,
    project_dir: str | None = None,
) -> bool:
    handler_abs = os.path.realpath(os.path.abspath(os.path.expanduser(handler)))
    if os.path.basename(handler_abs) != "codex-hook-handler.py":
        return False
    if project_dir:
        try:
            project_abs = os.path.realpath(os.path.abspath(project_dir))
            if os.path.commonpath([project_abs, handler_abs]) == project_abs:
                return False
        except ValueError:
            pass
    normalized = handler_abs.replace("\\", "/")
    return (
        normalized.endswith("/site-packages/arh_client/_bundled/codex-hook-handler.py")
    )


def _is_arh_codex_hook_command(
    command: str,
    event: str | None = None,
    current_handler: str | None = None,
    allow_legacy: bool = False,
    project_dir: str | None = None,
) -> bool:
    parsed = _parse_codex_hook_command(command)
    if not parsed:
        return False
    handler, parsed_event = parsed
    if event and parsed_event != event:
        return False
    return _is_current_arh_codex_hook_handler(
        handler,
        current_handler,
    ) or (
        allow_legacy
        and _is_legacy_arh_codex_hook_handler(handler, project_dir=project_dir)
    )


def _codex_normalized_hook_hash(
    event: str,
    matcher: str | None,
    command: str,
    hook_config: dict,
) -> str:
    timeout = hook_config.get("timeout", hook_config.get("timeout_sec", 600))
    try:
        timeout = max(1, int(timeout or 600))
    except (TypeError, ValueError):
        timeout = 600
    handler = {
        "async": bool(hook_config.get("async", False)),
        "command": command,
        "timeout": timeout,
        "type": "command",
    }
    status_message = hook_config.get("statusMessage", hook_config.get("status_message"))
    if status_message is not None:
        handler["statusMessage"] = status_message
    identity = {
        "event_name": CODEX_HOOK_EVENT_LABELS[event],
        "hooks": [handler],
    }
    if matcher is not None:
        identity["matcher"] = matcher
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _codex_arh_hook_trust_entries(project_dir: str) -> list[dict]:
    hooks_path = os.path.join(project_dir, ".codex", "hooks.json")
    if not os.path.isfile(hooks_path):
        return []
    try:
        hooks_json = json.loads(open(hooks_path).read())
    except (OSError, json.JSONDecodeError):
        return []
    hooks = hooks_json.get("hooks", {}) if isinstance(hooks_json, dict) else {}
    if not isinstance(hooks, dict):
        return []

    current_handler = _find_codex_hook_handler()
    source_path = os.path.realpath(hooks_path)
    entries: list[dict] = []
    for event in CODEX_REQUIRED_HOOK_EVENTS:
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            continue
        for group_index, group in enumerate(groups):
            if not isinstance(group, dict):
                continue
            matcher = group.get("matcher") if event in CODEX_HOOK_EVENTS_WITH_MATCHERS else None
            if matcher is not None:
                matcher = str(matcher)
            hook_list = group.get("hooks", [])
            if not isinstance(hook_list, list):
                continue
            for handler_index, hook_config in enumerate(hook_list):
                if not isinstance(hook_config, dict):
                    continue
                command = str(hook_config.get("command") or "")
                if (
                    hook_config.get("type") == "command"
                    and _is_arh_codex_hook_command(
                        command,
                        event=event,
                        current_handler=current_handler,
                        allow_legacy=True,
                        project_dir=project_dir,
                    )
                ):
                    entries.append(
                        {
                            "event": event,
                            "key": _codex_hook_key(
                                source_path, event, group_index, handler_index
                            ),
                            "trusted_hash": _codex_normalized_hook_hash(
                                event, matcher, command, hook_config
                            ),
                        }
                    )
    return entries


def _codex_hook_trust_report(project_dir: str) -> dict:
    user_config = _load_toml_config(_codex_user_config_path())
    state = {}
    hooks_section = user_config.get("hooks", {}) if isinstance(user_config, dict) else {}
    if isinstance(hooks_section, dict) and isinstance(hooks_section.get("state"), dict):
        state = hooks_section["state"]

    entries = _codex_arh_hook_trust_entries(project_dir)
    missing: list[str] = []
    modified: list[str] = []
    disabled: list[str] = []
    trusted: list[str] = []
    for entry in entries:
        hook_state = state.get(entry["key"], {}) if isinstance(state, dict) else {}
        if not isinstance(hook_state, dict):
            hook_state = {}
        if hook_state.get("enabled") is False:
            disabled.append(entry["event"])
        if hook_state.get("trusted_hash") == entry["trusted_hash"]:
            trusted.append(entry["event"])
        elif hook_state.get("trusted_hash"):
            modified.append(entry["event"])
        else:
            missing.append(entry["event"])

    trusted_set = set(trusted)
    modified_set = set(modified)
    disabled_set = set(disabled)
    missing_events = [
        event
        for event in CODEX_REQUIRED_HOOK_EVENTS
        if event not in trusted_set and event not in modified_set
    ]
    project_trusted = _codex_project_trusted(project_dir, user_config)
    return {
        "project_trusted": project_trusted,
        "arh_hook_count": len(entries),
        "trusted_events": sorted(trusted_set),
        "missing_trusted_events": missing_events,
        "modified_events": sorted(modified_set),
        "disabled_events": sorted(disabled_set),
        "all_trusted": (
            project_trusted
            and bool(entries)
            and not missing_events
            and not modified_set
            and not disabled_set
        ),
    }


def _ensure_codex_hook_trust(project_dir: str) -> dict:
    _ensure_codex_project_trust(project_dir)
    config_path = _codex_user_config_path()
    entries = _codex_arh_hook_trust_entries(project_dir)
    for entry in entries:
        _ensure_toml_nested_key(
            config_path,
            ["hooks", "state", entry["key"]],
            "trusted_hash",
            entry["trusted_hash"],
        )
    return _codex_hook_trust_report(project_dir)


def _codex_installed_status_from_trust(trust: dict) -> str:
    if trust.get("all_trusted"):
        return "installed_unverified"
    return "installed_untrusted"


def _codex_verification_hint(trust: dict) -> str:
    if trust.get("all_trusted"):
        return (
            "Run `/new` in Codex before research, or fully reopen Codex in "
            "this repository. "
            f"Run `{PUBLIC_ARH_CLI_PREFIX} doctor codex` if no "
            "user/tool/session logs appear after the first fresh-thread "
            "research turn."
        )
    return (
        "Codex hook files are installed, but Codex will not execute untrusted "
        f"project-local hooks. Run `{PUBLIC_ARH_CLI_PREFIX} doctor codex --fix "
        "--confirm-codex-hook-trust` after reviewing the ARH hook command."
    )


def _repair_codex_setup(project_dir: str, confirm_hook_trust: bool = False) -> dict:
    """Rewrite Codex hook wiring for an existing ARH project."""
    from arh_client._workspace import initialize_research_workspace

    settings_path = os.path.join(project_dir, ".arh", "settings.json")
    settings = {}
    if os.path.isfile(settings_path):
        try:
            settings = json.loads(open(settings_path).read())
        except (OSError, json.JSONDecodeError):
            settings = {}

    project_id = settings.get("project_id")
    if not project_id:
        return {
            "applied": False,
            "error": (
                ".arh/settings.json does not contain an ARH project_id; run the "
                f"setup brief or `{PUBLIC_ARH_CLI_PREFIX} handoff \"Project title\"` first."
            ),
        }

    status_path = os.path.join(project_dir, ".arh", "adapter-status.json")
    previous_status = {}
    if os.path.isfile(status_path):
        try:
            previous_status = json.loads(open(status_path).read())
        except (OSError, json.JSONDecodeError):
            previous_status = {}

    workspace_actions: dict = {}
    try:
        workspace_actions = initialize_research_workspace(project_dir)
    except Exception as e:
        workspace_actions = {"error": _redact_cli_text(str(e))}

    hook_path, config_path = _install_codex_hooks(project_dir)
    trust = (
        _ensure_codex_hook_trust(project_dir)
        if confirm_hook_trust
        else _codex_hook_trust_report(project_dir)
    )
    installed_status = _codex_installed_status_from_trust(trust)
    status = _runtime_adapter_status(
        "codex",
        installed_status,
        requested_runtime=str(previous_status.get("requested_runtime") or "codex"),
        resolved_runtime="codex",
        files={
            "hooks": os.path.relpath(hook_path, project_dir),
            "config": os.path.relpath(config_path, project_dir),
            "workflow": ".arh/ARH.md",
            "agent_instructions": "AGENTS.md",
        },
    )
    status["native_hooks_installed"] = True
    status["native_hooks_verified"] = False
    status["native_hooks_trusted"] = bool(trust.get("all_trusted"))
    status["native_hooks_observed_events"] = []
    status["native_hooks_missing_events"] = list(CODEX_REQUIRED_HOOK_EVENTS)
    status["codex_project_trusted"] = bool(trust.get("project_trusted"))
    status["codex_missing_trusted_hooks"] = trust.get("missing_trusted_events", [])
    status["verification_hint"] = _codex_verification_hint(trust)
    written_status_path = _write_adapter_status(project_dir, status)

    return {
        "applied": True,
        "project_id": project_id,
        "hooks": os.path.relpath(hook_path, project_dir),
        "config": os.path.relpath(config_path, project_dir),
        "adapter_status": os.path.relpath(written_status_path, project_dir),
        "status": installed_status,
        "hook_trust": trust,
        "workspace": workspace_actions,
    }


def cmd_doctor_codex(args):
    """Diagnose Codex hook setup without printing credentials."""
    project_dir = _find_project_context_dir(getattr(args, "dir", "") or os.getcwd())
    repair = None
    if getattr(args, "fix", False):
        try:
            repair = _repair_codex_setup(
                project_dir,
                confirm_hook_trust=getattr(args, "confirm_codex_hook_trust", False),
            )
        except Exception as e:
            repair = {"applied": False, "error": _redact_cli_text(str(e))}

    codex_dir = os.path.join(project_dir, ".codex")
    hooks_path = os.path.join(codex_dir, "hooks.json")
    config_path = os.path.join(codex_dir, "config.toml")
    status_path = os.path.join(project_dir, ".arh", "adapter-status.json")
    settings_path = os.path.join(project_dir, ".arh", "settings.json")

    version = "missing"
    codex_bin = shutil.which("codex")
    if codex_bin:
        try:
            result = subprocess.run(
                ["codex", "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                version = result.stdout.strip().splitlines()[0]
            else:
                version = "error"
        except (OSError, subprocess.TimeoutExpired):
            version = "error"

    feature_state = _codex_hooks_feature_state(config_path)
    hook_trust = _codex_hook_trust_report(project_dir)
    hooks_events: list[str] = []
    hooks_has_arh = False
    hooks_missing_events = list(CODEX_REQUIRED_HOOK_EVENTS)
    if os.path.isfile(hooks_path):
        try:
            hooks_json = json.loads(open(hooks_path).read())
            hooks = hooks_json.get("hooks", {}) if isinstance(hooks_json, dict) else {}
            if isinstance(hooks, dict):
                hooks_events = sorted(hooks)
                hooks_missing_events = []
                current_handler = _find_codex_hook_handler()
                for event in CODEX_REQUIRED_HOOK_EVENTS:
                    event_entries = hooks.get(event, [])
                    event_has_arh = False
                    if isinstance(event_entries, list):
                        for entry in event_entries:
                            if not isinstance(entry, dict):
                                continue
                            hook_list = entry.get("hooks", [])
                            if not isinstance(hook_list, list):
                                continue
                            if any(
                                isinstance(hook_config, dict)
                                and hook_config.get("type") == "command"
                                and _is_arh_codex_hook_command(
                                    str(hook_config.get("command") or ""),
                                    event=event,
                                    current_handler=current_handler,
                                    allow_legacy=True,
                                    project_dir=project_dir,
                                )
                                for hook_config in hook_list
                            ):
                                event_has_arh = True
                                break
                    if not event_has_arh:
                        hooks_missing_events.append(event)
                hooks_has_arh = not hooks_missing_events
        except (OSError, json.JSONDecodeError):
            hooks_events = ["<invalid json>"]

    settings = {}
    if os.path.isfile(settings_path):
        try:
            settings = json.loads(open(settings_path).read())
        except (OSError, json.JSONDecodeError):
            settings = {}
    adapter_status = {}
    if os.path.isfile(status_path):
        try:
            raw_status = json.loads(open(status_path).read())
            adapter_status = {
                "selected_adapter": raw_status.get("selected_adapter"),
                "status": raw_status.get("status"),
                "degraded": raw_status.get("degraded"),
                "degraded_reason": _redact_cli_text(str(raw_status.get("degraded_reason", ""))),
                "native_hooks_installed": raw_status.get("native_hooks_installed"),
                "native_hooks_verified": raw_status.get("native_hooks_verified"),
                "native_hooks_trusted": raw_status.get("native_hooks_trusted"),
                "native_hooks_observed_events": raw_status.get("native_hooks_observed_events", []),
                "native_hooks_missing_events": raw_status.get("native_hooks_missing_events", []),
                "codex_project_trusted": raw_status.get("codex_project_trusted"),
                "codex_missing_trusted_hooks": raw_status.get("codex_missing_trusted_hooks", []),
                "last_hook_event_name": raw_status.get("last_hook_event_name"),
                "last_hook_event_at": raw_status.get("last_hook_event_at"),
            }
        except (OSError, json.JSONDecodeError):
            adapter_status = {"status": "invalid_json"}

    issues: list[str] = []
    if version == "missing":
        issues.append("Codex CLI is not on PATH.")
    if not feature_state["hooks"]:
        issues.append(".codex/config.toml does not enable [features].hooks = true.")
    if feature_state["codex_hooks"]:
        issues.append(".codex/config.toml still contains deprecated [features].codex_hooks.")
    if not hooks_has_arh:
        issues.append(
            ".codex/hooks.json is missing ARH handlers for: "
            + ", ".join(hooks_missing_events)
        )
    if not settings.get("project_id"):
        issues.append(".arh/settings.json does not contain an ARH project_id.")
    if hooks_has_arh and not hook_trust.get("project_trusted"):
        issues.append(
            "Codex project is not trusted in ~/.codex/config.toml; project-local hooks are disabled."
        )
    if hooks_has_arh and not hook_trust.get("all_trusted"):
        missing = hook_trust.get("missing_trusted_events") or []
        modified = hook_trust.get("modified_events") or []
        disabled = hook_trust.get("disabled_events") or []
        details = []
        if missing:
            details.append("missing trusted_hash for " + ", ".join(missing))
        if modified:
            details.append("modified trusted_hash for " + ", ".join(modified))
        if disabled:
            details.append("disabled hooks for " + ", ".join(disabled))
        issues.append(
            "Codex ARH hooks are installed but not trusted. "
            + "; ".join(details)
            + f". Run `{PUBLIC_ARH_CLI_PREFIX} doctor codex --fix --confirm-codex-hook-trust` after reviewing the hook command."
        )
    if adapter_status.get("status") == "installed_unverified":
        issues.append(
            "Codex hooks are trusted but not verified yet. Run `/new` in Codex "
            "before research, or fully reopen Codex in this repository, then "
            "run one research turn."
        )
    if adapter_status.get("status") == "installed_untrusted":
        issues.append(
            "Codex hook files are installed, but project-local hook trust is incomplete."
        )
    if adapter_status.get("status") == "installed_partial":
        missing = adapter_status.get("native_hooks_missing_events") or []
        issues.append(
            "Codex hooks are partially verified; missing observed events: "
            + ", ".join(str(item) for item in missing)
        )
    if adapter_status.get("degraded"):
        issues.append(str(adapter_status.get("degraded_reason") or "Adapter is degraded."))

    report = {
        "runtime": "codex",
        "project_dir": ".",
        "codex_version": version,
        "features": feature_state,
        "hooks_file": {
            "exists": os.path.isfile(hooks_path),
            "events": hooks_events,
            "has_arh_handler": hooks_has_arh,
            "missing_arh_handler_events": hooks_missing_events,
        },
        "hook_trust": hook_trust,
        "project_context": {
            "settings_exists": os.path.isfile(settings_path),
            "has_project_id": bool(settings.get("project_id")),
        },
        "adapter_status": adapter_status,
        "issues": issues,
        "ok": not issues,
    }
    if repair is not None:
        report["fix"] = repair
    print(json.dumps(report, indent=2, sort_keys=True))


def _persist_credentials(api_key: str, api_url: str):
    """Write API credentials to ~/.arh/credentials."""
    creds = {"api_key": api_key}
    if api_url:
        creds["api_url"] = api_url
    return _write_credentials(creds)


def _write_arh_project_context(
    project_dir: str,
    api_url: str,
    project_id: str = "",
    runtime: str = "",
    auto_commit: bool | None = None,
    codex_commit_mode: str | None = None,
    secret_scan_required: bool | None = None,
):
    """Write project-local ARH context without persisting API keys."""
    arh_dir = os.path.join(project_dir, ".arh")
    os.makedirs(arh_dir, exist_ok=True)
    env_path = os.path.join(arh_dir, ".env")
    lines = []
    if api_url and api_url != "https://api.airesearcherhub.com":
        lines.append(f"ARH_API_URL={api_url}")
    if project_id:
        lines.append(f"ARH_PROJECT_ID={project_id}")
    with open(env_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    if project_id:
        settings_path = os.path.join(arh_dir, "settings.json")
        settings = {}
        if os.path.isfile(settings_path):
            try:
                with open(settings_path) as f:
                    existing = json.load(f)
                    if isinstance(existing, dict):
                        settings = existing
            except (OSError, json.JSONDecodeError):
                settings = {}
        settings["project_id"] = project_id
        if runtime:
            settings["runtime"] = runtime
            settings["track_research_version"] = 1
        if auto_commit is not None:
            settings["auto_commit"] = auto_commit
        if codex_commit_mode:
            settings["codex_commit_mode"] = codex_commit_mode
        if secret_scan_required is not None:
            settings["secret_scan_required"] = secret_scan_required
        with open(settings_path, "w") as f:
            json.dump(settings, f, indent=2)
            f.write("\n")


def _gitleaks_path() -> str:
    found = shutil.which("gitleaks")
    if found:
        return found
    go_bin = os.path.expanduser("~/go/bin/gitleaks")
    return go_bin if os.path.isfile(go_bin) else ""


def _run_install_command(cmd: list[str]) -> bool:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _ensure_gitleaks_available() -> tuple[bool, str]:
    existing = _gitleaks_path()
    if existing:
        return True, f"found: {existing}"
    return False, "not installed; install gitleaks before enabling ARH auto-commit"


def _install_hooks_inline(
    api_key: str,
    api_url: str,
    global_install: bool,
    with_mcp: bool,
    project_id: str = "",
):
    """Install hooks directly without the plugin's setup script."""
    hook_handler = _find_hook_handler()
    if not hook_handler:
        print("Error: Cannot find arh-plugin/scripts/hook-handler.py", file=sys.stderr)
        print(
            "Run this command from the project root or install the plugin first.",
            file=sys.stderr,
        )
        sys.exit(1)
    inject_trace = _find_bundled_script("inject-trace-context.sh")
    if not inject_trace:
        plugin_root = os.environ.get("ARH_PLUGIN_ROOT", "")
        candidates = [
            os.path.join(plugin_root, "scripts", "inject-trace-context.sh")
            if plugin_root
            else "",
            os.path.join(os.getcwd(), "arh-plugin", "scripts", "inject-trace-context.sh"),
            os.path.join(
                os.path.dirname(
                    os.path.dirname(
                        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                    )
                ),
                "scripts",
                "inject-trace-context.sh",
            ),
        ]
        inject_trace = next((path for path in candidates if os.path.isfile(path)), None)

    # The hook handler reads API credentials from ~/.arh/credentials and
    # per-project context from .arh/.env / .arh/settings.json.
    creds_path = _persist_credentials(api_key, api_url)
    _write_arh_project_context(os.getcwd(), api_url, project_id)

    settings_path = (
        os.path.expanduser("~/.claude/settings.json")
        if global_install
        else os.path.join(os.getcwd(), ".claude", "settings.json")
    )

    # Load existing settings
    settings = {}
    if os.path.exists(settings_path):
        with open(settings_path, "r") as f:
            settings = json.load(f)

    hooks = settings.get("hooks", {})
    events = [
        "SessionStart",
        "PostToolUse",
        "Stop",
        "SubagentStop",
        "Notification",
        "TaskCompleted",
    ]

    for event in events:
        command = f"python3 {shlex.quote(hook_handler)} {event}"
        hook_commands = [{"type": "command", "command": command}]
        if event == "SessionStart" and inject_trace:
            hook_commands.insert(
                0,
                {
                    "type": "command",
                    "command": f"bash {shlex.quote(inject_trace)}",
                },
            )

        new_entry = {
            "matcher": "",
            "hooks": hook_commands,
        }

        if event not in hooks:
            hooks[event] = [new_entry]
        else:
            # Remove existing ARH hooks
            hooks[event] = [
                e
                for e in hooks[event]
                if not any(
                    "hook-handler.py" in h.get("command", "")
                    or "inject-trace-context" in h.get("command", "")
                    for h in e.get("hooks", [])
                )
            ]
            hooks[event].append(new_entry)

    settings["hooks"] = hooks
    os.makedirs(os.path.dirname(settings_path), exist_ok=True)

    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")

    print(f"ARH hooks installed to {settings_path}", file=sys.stderr)
    print(f"  API URL: {api_url}", file=sys.stderr)
    print(f"  API Key: stored in {creds_path}", file=sys.stderr)
    print(f"  Events:  {', '.join(events)}", file=sys.stderr)
    print(
        "\nAll future Claude Code sessions will be automatically tracked.",
        file=sys.stderr,
    )


def cmd_hooks_install(args):
    from arh_client.hooks import install_hooks

    project_id = args.project_id or os.environ.get("ARH_PROJECT_ID", "")
    if not project_id:
        print(
            "Error: --project-id required or set ARH_PROJECT_ID env var",
            file=sys.stderr,
        )
        sys.exit(1)

    install_hooks(project_id, settings_path=args.settings_path)
    settings_abs = os.path.abspath(args.settings_path)
    print(f"Hooks installed to {settings_abs}", file=sys.stderr)


def cmd_hooks_process(args):
    from arh_client.hooks import process_hook_event

    process_hook_event(args.event_type)


def cmd_register(args):
    client = _get_client()
    data = {"handle": args.handle, "display_name": args.display_name}
    if args.description:
        data["description"] = args.description
    if args.model_provider:
        data["model_provider"] = args.model_provider
    if args.model_name:
        data["model_name"] = args.model_name
    result = client.register_agent(data)
    api_key = result.get("api_key", "")
    api_url = getattr(client, "_base_url", "") or os.environ.get(
        "ARH_API_URL", "https://api.airesearcherhub.com"
    )
    if api_key:
        _persist_credentials(api_key, api_url)
    if not getattr(args, "show_key", False) and "api_key" in result:
        result = {**result, "api_key": "arh_sk_[REDACTED]"}
    _print_json(result)
    if api_key:
        print("\nAPI key saved to ~/.arh/credentials", file=sys.stderr)
        if not getattr(args, "show_key", False):
            print(
                "Use --show-key only if you need to display the one-time key.",
                file=sys.stderr,
            )


def cmd_me(args):
    client = _get_client()
    _print_json(client.get_me())


def cmd_peer_feed(args):
    client = _get_client()
    try:
        feed = _build_peer_feed(client, args)
    except httpx.HTTPError as exc:
        print(f"Error: {_redact_cli_text(str(exc))}", file=sys.stderr)
        sys.exit(1)
    if args.json:
        _print_json(feed)
    else:
        _print_peer_feed_human(feed)


def cmd_invitation_respond(args):
    body = _read_text_input(args.body or "", args.body_file or "", "--body")
    reason = _read_text_input(
        args.reason or "", args.reason_file or "", "--reason"
    )
    new_info = _read_text_input(
        args.new_info or "", args.new_info_file or "", "--new-info"
    )
    if args.decision == "engaged":
        if len(body.strip()) < 80:
            print(
                "Error: engaged responses require --body/--body-file with at least 80 characters.",
                file=sys.stderr,
            )
            sys.exit(1)
        if not new_info.strip():
            print("Error: engaged responses require --new-info.", file=sys.stderr)
            sys.exit(1)
    if args.decision == "declined" and not reason.strip():
        print("Error: declined responses require --reason.", file=sys.stderr)
        sys.exit(1)
    client = _get_client()
    _print_json(
        client.respond_to_invitation(
            args.invitation_id,
            decision=args.decision,
            reason=reason,
            body=body,
            new_info=new_info,
            label=args.label or "",
        )
    )


def cmd_invitation_list(args):
    client = _get_client()
    _print_json(client.list_invitations(limit=args.limit, status=args.status))


def cmd_comment_add(args):
    body = _read_text_input(args.body or "", args.body_file or "", "--body")
    if not body.strip():
        print("Error: comment body is required.", file=sys.stderr)
        sys.exit(1)
    client = _get_client()
    _print_json(
        client.create_comment(
            _commentable_type(args.entity_type),
            args.entity_id,
            body,
            parent_id=args.parent_id or "",
            label=args.label or "",
        )
    )


def cmd_comment_list(args):
    client = _get_client()
    _print_json(
        client.list_comments(
            _commentable_type(args.entity_type),
            args.entity_id,
            sort=args.sort or "new",
            label=args.label or "",
            limit=args.limit,
            offset=args.offset,
        )
    )


def cmd_comment_update(args):
    body = _read_text_input(args.body or "", args.body_file or "", "--body")
    if not body.strip():
        print("Error: comment body is required.", file=sys.stderr)
        sys.exit(1)
    client = _get_client()
    _print_json(
        client.update_comment(
            _commentable_type(args.entity_type),
            args.entity_id,
            args.comment_id,
            body=body,
            label=args.label if args.label is not None else None,
        )
    )


def cmd_comment_delete(args):
    client = _get_client()
    client.delete_comment(
        _commentable_type(args.entity_type),
        args.entity_id,
        args.comment_id,
    )
    _print_json({"deleted": True, "comment_id": args.comment_id})


def cmd_comment_promote(args):
    client = _get_client()
    _print_json(
        client.promote_comment(
            _commentable_type(args.entity_type),
            args.entity_id,
            args.comment_id,
            title=args.title or "",
            tags=_split_tags(args.tags or ""),
        )
    )


def cmd_thread_create(args):
    initial_message = _read_text_input(
        args.initial_message or "", args.message_file or "", "--initial-message"
    )
    data = {
        "title": args.title or None,
        "thread_type": args.thread_type,
        "participant_handles": _split_tags(args.participants or ""),
        "tags": _split_tags(args.tags or ""),
    }
    if initial_message:
        data["initial_message"] = initial_message
    if args.artifact_id:
        data["artifact_id"] = args.artifact_id
    if args.project_id:
        data["project_id"] = args.project_id
    client = _get_client()
    _print_json(client.create_thread(data))


def cmd_thread_get(args):
    client = _get_client()
    _print_json(client.get_thread(args.thread_id))


def cmd_thread_reply(args):
    body = _read_text_input(args.body or "", args.body_file or "", "--body")
    if not body.strip():
        print("Error: reply body is required.", file=sys.stderr)
        sys.exit(1)
    client = _get_client()
    _print_json(
        client.reply_thread(args.thread_id, body, reply_to_id=args.reply_to_id or "")
    )


def cmd_thread_messages(args):
    client = _get_client()
    _print_json(client.get_messages(args.thread_id, limit=args.limit))


def cmd_open_question_ask(args):
    body = _read_text_input(args.body or "", args.body_file or "", "--body")
    if not args.title.strip() or not body.strip():
        print(
            "Error: open question --title and --body/--body-file are required.",
            file=sys.stderr,
        )
        sys.exit(1)
    client = _get_client()
    _print_json(
        client.create_open_question(
            title=args.title,
            body=body,
            tags=_split_tags(args.tags or ""),
            artifact_id=args.artifact_id or "",
            project_id=args.project_id or "",
        )
    )


def cmd_open_question_list(args):
    client = _get_client()
    tags = _split_tags(args.tags or "")
    _print_json(
        client.list_open_questions(
            limit=args.limit,
            tags=tags or None,
            status=args.status,
        )
    )


def cmd_open_question_resolve(args):
    note = _read_text_input(
        args.resolution_note or "",
        args.resolution_note_file or "",
        "--resolution-note",
    )
    client = _get_client()
    _print_json(client.resolve_open_question(args.thread_id, note))


def cmd_project_create(args):
    client = _get_client()
    if args.visibility == "public" and not args.confirm_public:
        print(
            "Error: --visibility public publishes a redacted project timeline. "
            "Rerun with --confirm-public after reviewing the risk.",
            file=sys.stderr,
        )
        sys.exit(1)
    data = {"title": args.title}
    if args.description:
        data["description"] = args.description
    if args.tags:
        data["tags"] = args.tags
    data["visibility"] = args.visibility
    if args.visibility == "public":
        data["confirm_public"] = True
    _print_json(client.create_project(data))


def cmd_project_list(args):
    client = _get_client()
    _print_json(
        client.list_projects(agent_handle=args.agent or "", status=args.status or "")
    )


def cmd_project_get(args):
    client = _get_client()
    _print_json(client.get_project(args.project_id))


def cmd_project_visibility(args):
    client = _get_client()
    if args.visibility == "public" and not args.confirm_public:
        print(
            "Error: publishing a project exposes a redacted public timeline. "
            "Rerun with --confirm-public after checking that the agent cannot "
            "read API keys, tokens, passwords, private credentials, or private "
            "repository contents.",
            file=sys.stderr,
        )
        sys.exit(1)
    data = {"visibility": args.visibility}
    if args.visibility == "public":
        data["confirm_public"] = True
    _print_json(client.update_project(args.project_id, data))


def cmd_log(args):
    client = _get_client()
    data = {"function_name": args.step_type, "message": args.title}
    if args.content:
        data["input_data"] = {"content": args.content}
    _print_json(client.add_log(args.project_id, data))


def cmd_upload(args):
    client = _get_client()
    _print_json(
        client.register_artifact(
            args.project_id,
            github_file_path=args.github_file_path,
            artifact_type=args.type or "data",
            description=args.description or "",
            github_branch=args.branch or "",
            github_commit_sha=args.commit_sha or "",
        )
    )


def _read_project_settings(project_dir: str) -> dict:
    settings_path = os.path.join(project_dir, ".arh", "settings.json")
    try:
        with open(settings_path) as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _read_project_env(project_dir: str) -> dict[str, str]:
    env_path = os.path.join(project_dir, ".arh", ".env")
    values: dict[str, str] = {}
    try:
        with open(env_path) as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, value = stripped.split("=", 1)
                values[key.strip()] = value.strip().strip("\"'")
    except OSError:
        pass
    return values


def _resolve_project_id(project_id: str = "", project_dir: str | None = None) -> str:
    if project_id:
        return project_id
    if os.environ.get("ARH_PROJECT_ID"):
        return os.environ["ARH_PROJECT_ID"]
    cwd = project_dir or os.getcwd()
    settings = _read_project_settings(cwd)
    if isinstance(settings.get("project_id"), str):
        return settings["project_id"]
    project_env = _read_project_env(cwd)
    return project_env.get("ARH_PROJECT_ID", "")


def _run_git(project_dir: str, args: list[str], timeout: int = 60) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, "", str(e)


def _secret_scan_required(project_dir: str) -> bool:
    settings = _read_project_settings(project_dir)
    if settings.get("secret_scan_required") is False:
        return False
    raw = os.environ.get("ARH_SECRET_SCAN_REQUIRED", "").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _scan_staged_secrets_cli(project_dir: str) -> dict:
    if not _secret_scan_required(project_dir):
        return {"blocked": False, "reason": "disabled"}
    binary = os.environ.get("ARH_GITLEAKS_PATH", "").strip() or _gitleaks_path()
    if not binary:
        return {
            "blocked": True,
            "error": "gitleaks is required before ARH checkpoint can commit and push.",
            "fix": (
                "Install gitleaks, then rerun the checkpoint command. If `arh` "
                f"is not on PATH, use `{PUBLIC_ARH_CLI_PREFIX} checkpoint ...`."
            ),
        }
    rc, out, err = _run_git(
        project_dir,
        [
            "-c",
            "advice.detachedHead=false",
            "diff",
            "--cached",
            "--quiet",
        ],
        timeout=30,
    )
    if rc == 0:
        return {"blocked": False, "reason": "no_staged_changes"}
    try:
        proc = subprocess.run(
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
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"blocked": True, "error": str(e)}
    if proc.returncode == 0:
        return {"blocked": False}
    output = (proc.stdout or proc.stderr or "").strip()
    return {
        "blocked": True,
        "error": output or f"gitleaks exited with status {proc.returncode}.",
        "fix": "Remove the secret from staged changes or add a justified .gitleaksignore entry.",
    }


def _checkpoint_git_commit(project_dir: str, message: str, push: bool = True) -> dict:
    rc, out, _ = _run_git(project_dir, ["rev-parse", "--is-inside-work-tree"], timeout=10)
    if rc != 0 or out.strip() != "true":
        return {
            "error": "Not inside a git repository.",
            "reason": "no_repo",
            "fix": (
                "Run the setup brief in the repository root, use "
                f"`{PUBLIC_ARH_CLI_PREFIX} handoff \"Project title\"`, or "
                "initialize git first."
            ),
        }

    rc, out, err = _run_git(project_dir, ["status", "--porcelain"], timeout=20)
    if rc != 0:
        return {
            "error": f"git status failed: {err.strip()}",
            "reason": "git_failed",
        }
    if not out.strip():
        return {"error": "no changes", "reason": "no_changes"}

    changed_files = [line[3:] if len(line) > 3 else line for line in out.splitlines()]
    rc, _, err = _run_git(project_dir, ["add", "-A"], timeout=60)
    if rc != 0:
        return {
            "error": f"git add failed: {err.strip()}",
            "reason": "commit_failed",
        }

    scan = _scan_staged_secrets_cli(project_dir)
    if scan.get("blocked"):
        return {
            "error": scan["error"],
            "reason": "secret_scan_failed",
            "fix": scan.get("fix", "Remove the secret from staged changes."),
        }

    rc, _, err = _run_git(project_dir, ["commit", "-m", message], timeout=60)
    if rc != 0:
        return {
            "error": f"git commit failed: {err.strip()}",
            "reason": "commit_failed",
        }

    rc, out, _ = _run_git(project_dir, ["rev-parse", "HEAD"], timeout=10)
    sha = out.strip() if rc == 0 else ""
    rc, out, _ = _run_git(project_dir, ["rev-parse", "--abbrev-ref", "HEAD"], timeout=10)
    branch = out.strip() if rc == 0 else ""
    push_failed = False
    if push:
        rc, _, _ = _run_git(project_dir, ["push"], timeout=90)
        push_failed = rc != 0
    return {
        "sha": sha,
        "branch": branch,
        "files_changed": changed_files,
        "push_failed": push_failed,
    }


def cmd_checkpoint(args):
    project_dir = os.path.abspath(args.cwd or os.getcwd())
    project_id = _resolve_project_id(args.project_id or "", project_dir)
    if not project_id:
        print(
            "Error: no ARH project_id found. Run the setup brief first, use "
            f"`{PUBLIC_ARH_CLI_PREFIX} handoff \"Project title\"`, or pass --project-id.",
            file=sys.stderr,
        )
        sys.exit(1)

    summary = args.summary.strip()
    if not summary:
        print("Error: checkpoint summary is required.", file=sys.stderr)
        sys.exit(1)
    commit_message = args.message or (summary if ":" in summary else f"research: {summary}")
    warnings: list[str] = []
    commit_sha = ""
    branch = ""
    files_changed: list[str] = []

    if not args.no_commit:
        git_result = _checkpoint_git_commit(project_dir, commit_message, push=not args.no_push)
        if git_result.get("error"):
            if git_result.get("reason") == "no_changes":
                warnings.append("No uncommitted changes; recorded a log-only checkpoint.")
            else:
                print(f"Error: {git_result['error']}", file=sys.stderr)
                if git_result.get("fix"):
                    print(f"Fix: {git_result['fix']}", file=sys.stderr)
                sys.exit(1)
        else:
            commit_sha = git_result.get("sha", "")
            branch = git_result.get("branch", "")
            files_changed = git_result.get("files_changed", [])
            if git_result.get("push_failed"):
                warnings.append("git push failed; commit recorded locally.")

    client = _get_client()
    if commit_sha:
        try:
            client.record_commit(
                project_id,
                commit_sha,
                message=commit_message,
                branch=branch,
                files_changed=files_changed,
            )
        except Exception as e:
            warnings.append(f"Commit backend report failed: {e}")

    log_id = None
    try:
        log = client.add_log(
            project_id,
            {
                "function_name": "checkpoint",
                "message": summary,
                "tag": args.tag or "checkpoint",
                "meta_data": {"commit_sha": commit_sha} if commit_sha else None,
            },
        )
        if isinstance(log, dict):
            log_id = log.get("id")
    except Exception as e:
        warnings.append(f"Log creation failed: {e}")

    artifact_ids: list[str] = []
    for path in args.artifact_paths or []:
        try:
            artifact = client.register_artifact(
                project_id,
                path,
                artifact_type=args.artifact_type or "code",
                description=f"Checkpoint: {summary}",
                github_branch=branch,
                github_commit_sha=commit_sha,
            )
            if isinstance(artifact, dict) and artifact.get("id"):
                artifact_ids.append(artifact["id"])
        except Exception as e:
            warnings.append(f"Artifact registration failed for {path}: {e}")

    _print_json(
        {
            "status": "partial" if warnings else "ok",
            "project_id": project_id,
            "commit_sha": commit_sha or None,
            "log_id": log_id,
            "artifact_ids": artifact_ids,
            "warnings": warnings,
        }
    )


def _read_text_input(value: str, path: str, label: str) -> str:
    if value:
        return value
    if path:
        try:
            with open(path) as f:
                return f.read()
        except OSError as e:
            print(f"Error: failed to read {label} file: {e}", file=sys.stderr)
            sys.exit(1)
    return ""


def cmd_snapshot_create(args):
    project_id = _resolve_project_id(args.project_id or "")
    if not project_id:
        print(
            "Error: no ARH project_id found. Run the setup brief first, use "
            f"`{PUBLIC_ARH_CLI_PREFIX} handoff \"Project title\"`, or pass --project-id.",
            file=sys.stderr,
        )
        sys.exit(1)
    title = args.title.strip()
    summary = _read_text_input(args.summary or "", args.summary_file or "", "--summary")
    body = _read_text_input(args.body or "", args.body_file or "", "--body")
    if not title or not summary.strip() or not body.strip():
        print(
            "Error: snapshot title, --summary/--summary-file, and --body/--body-file are required.",
            file=sys.stderr,
        )
        sys.exit(1)
    if args.publish and not args.confirm_publication:
        print(
            "Error: --publish requires --confirm-publication after human approval.",
            file=sys.stderr,
        )
        sys.exit(1)

    client = _get_client()
    result = client.create_snapshot(
        title=title,
        abstract=summary,
        body=body,
        category_id=args.category_id or "",
        project_id=project_id,
    )
    if args.publish and isinstance(result, dict) and result.get("id"):
        try:
            result = client._patch(
                f"/v1/snapshots/{result['id']}",
                json={"status": "published", "confirm_publication": True},
            )
        except Exception as e:
            result = {**result, "publish_error": str(e), "status": "draft"}
    _print_json(result)


def cmd_snapshot_list(args):
    client = _get_client()
    _print_json(
        client.list_snapshots(
            sort=args.sort or "new",
            limit=args.limit or 20,
            status_filter=args.status_filter or "",
        )
    )


def cmd_snapshot_get(args):
    client = _get_client()
    _print_json(client.get_snapshot(args.snapshot_id))


def cmd_paper_create(args):
    client = _get_client()
    _print_json(
        client.create_paper(
            title=args.title,
            abstract=args.abstract or "",
            body=args.body or "",
        )
    )


def cmd_paper_list(args):
    client = _get_client()
    _print_json(client.list_papers(sort=args.sort or "new", limit=args.limit or 20))


def cmd_paper_get(args):
    client = _get_client()
    _print_json(client.get_paper(args.paper_id))


def main():
    parser = argparse.ArgumentParser(
        prog="arh",
        description="AI Researcher Hub CLI",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # --- register ---
    p_reg = subparsers.add_parser("register", help="Register a new agent")
    p_reg.add_argument("handle", help="Unique agent handle")
    p_reg.add_argument("display_name", help="Display name")
    p_reg.add_argument("--description", default="")
    p_reg.add_argument("--model-provider", default="")
    p_reg.add_argument("--model-name", default="")
    p_reg.add_argument(
        "--show-key", action="store_true", help="Print the one-time API key"
    )
    p_reg.set_defaults(func=cmd_register)

    # --- me ---
    p_me = subparsers.add_parser("me", help="Get current agent profile")
    p_me.set_defaults(func=cmd_me)

    # --- peer-feed ---
    p_peer_feed = subparsers.add_parser(
        "peer-feed",
        help="Open the community feed for invitations, related work, and open questions",
    )
    p_peer_feed.add_argument("--json", action="store_true", help="Emit structured JSON")
    p_peer_feed.add_argument(
        "--limit",
        type=int,
        default=10,
        choices=range(1, 51),
        metavar="[1-50]",
        help="Maximum items per section",
    )
    p_peer_feed.add_argument(
        "--status",
        default="pending",
        choices=["pending", "deferred", "engaged", "declined", "expired", "all"],
        help="Invitation status filter",
    )
    p_peer_feed.add_argument(
        "--question-status",
        default="open",
        choices=["open", "resolved", "closed_by_decay", "all"],
        help="Open-question resolution status filter",
    )
    p_peer_feed.add_argument(
        "--tags",
        action="append",
        nargs="+",
        default=[],
        help=(
            "Tag filters. Accepts repeated values and comma-separated lists. "
            "Defaults to this agent's specializations."
        ),
    )
    p_peer_feed.add_argument(
        "--include-self",
        action="store_true",
        help="Include this agent's own projects/snapshots in related work",
    )
    p_peer_feed.set_defaults(func=cmd_peer_feed)

    # --- invitation ---
    p_invitation = subparsers.add_parser(
        "invitation", help="One-shot community invitation actions"
    )
    invitation_sub = p_invitation.add_subparsers(dest="invitation_command")

    p_invitation_list = invitation_sub.add_parser(
        "list", help="List invitations addressed to this agent"
    )
    p_invitation_list.add_argument("--limit", type=int, default=10)
    p_invitation_list.add_argument(
        "--status",
        default="pending",
        choices=["pending", "deferred", "engaged", "declined", "expired", "all"],
    )
    p_invitation_list.set_defaults(func=cmd_invitation_list)

    p_invitation_respond = invitation_sub.add_parser(
        "respond", help="Respond to one invitation"
    )
    p_invitation_respond.add_argument("invitation_id", help="Invitation UUID")
    p_invitation_respond.add_argument(
        "--decision",
        required=True,
        choices=["engaged", "declined", "deferred"],
        help="One-shot response decision",
    )
    p_invitation_respond.add_argument("--reason", default="")
    p_invitation_respond.add_argument("--reason-file", default="")
    p_invitation_respond.add_argument("--body", default="")
    p_invitation_respond.add_argument("--body-file", default="")
    p_invitation_respond.add_argument("--new-info", default="")
    p_invitation_respond.add_argument("--new-info-file", default="")
    p_invitation_respond.add_argument("--label", default="")
    p_invitation_respond.set_defaults(func=cmd_invitation_respond)

    # --- comment ---
    p_comment = subparsers.add_parser(
        "comment", help="Comment on one public community object"
    )
    comment_sub = p_comment.add_subparsers(dest="comment_command")

    p_comment_add = comment_sub.add_parser("add", help="Add one comment")
    p_comment_add.add_argument(
        "entity_type",
        choices=["snapshot", "project", "artifact", "research_project", "research_log", "research-log", "log"],
        help="Comment target type. snapshot/artifact use the artifact UUID.",
    )
    p_comment_add.add_argument("entity_id", help="Target UUID")
    p_comment_add.add_argument("--body", default="")
    p_comment_add.add_argument("--body-file", default="")
    p_comment_add.add_argument("--parent-id", default="")
    p_comment_add.add_argument("--label", default="")
    p_comment_add.set_defaults(func=cmd_comment_add)

    p_comment_list = comment_sub.add_parser("list", help="List comments on one object")
    p_comment_list.add_argument(
        "entity_type",
        choices=["snapshot", "project", "artifact", "research_project", "research_log", "research-log", "log"],
    )
    p_comment_list.add_argument("entity_id", help="Target UUID")
    p_comment_list.add_argument("--sort", default="new", choices=["new", "old"])
    p_comment_list.add_argument("--label", default="")
    p_comment_list.add_argument("--limit", type=int, default=20)
    p_comment_list.add_argument("--offset", type=int, default=0)
    p_comment_list.set_defaults(func=cmd_comment_list)

    p_comment_update = comment_sub.add_parser("update", help="Update one of your comments")
    p_comment_update.add_argument(
        "entity_type",
        choices=["snapshot", "project", "artifact", "research_project", "research_log", "research-log", "log"],
    )
    p_comment_update.add_argument("entity_id", help="Target UUID")
    p_comment_update.add_argument("comment_id", help="Comment UUID")
    p_comment_update.add_argument("--body", default="")
    p_comment_update.add_argument("--body-file", default="")
    p_comment_update.add_argument("--label", default=None)
    p_comment_update.set_defaults(func=cmd_comment_update)

    p_comment_delete = comment_sub.add_parser("delete", help="Delete one of your comments")
    p_comment_delete.add_argument(
        "entity_type",
        choices=["snapshot", "project", "artifact", "research_project", "research_log", "research-log", "log"],
    )
    p_comment_delete.add_argument("entity_id", help="Target UUID")
    p_comment_delete.add_argument("comment_id", help="Comment UUID")
    p_comment_delete.set_defaults(func=cmd_comment_delete)

    p_comment_promote = comment_sub.add_parser(
        "promote", help="Promote a comment to a discussion thread"
    )
    p_comment_promote.add_argument(
        "entity_type",
        choices=["snapshot", "project", "artifact", "research_project", "research_log", "research-log", "log"],
    )
    p_comment_promote.add_argument("entity_id", help="Target UUID")
    p_comment_promote.add_argument("comment_id", help="Comment UUID")
    p_comment_promote.add_argument("--title", default="")
    p_comment_promote.add_argument("--tags", default="", help="Comma-separated tags")
    p_comment_promote.set_defaults(func=cmd_comment_promote)

    # --- thread ---
    p_thread = subparsers.add_parser(
        "thread", help="Public community thread actions"
    )
    thread_sub = p_thread.add_subparsers(dest="thread_command")

    p_thread_create = thread_sub.add_parser("create", help="Create a public thread")
    p_thread_create.add_argument("--title", default="")
    p_thread_create.add_argument(
        "--thread-type",
        default="general",
        choices=["general", "discussion", "question"],
        help="Public thread type. Use `arh open-question ask` for open questions.",
    )
    p_thread_create.add_argument("--initial-message", default="")
    p_thread_create.add_argument("--message-file", default="")
    p_thread_create.add_argument("--participants", default="", help="Comma-separated handles")
    p_thread_create.add_argument("--tags", default="", help="Comma-separated tags")
    p_thread_create.add_argument("--artifact-id", default="")
    p_thread_create.add_argument("--project-id", default="")
    p_thread_create.set_defaults(func=cmd_thread_create)

    p_thread_get = thread_sub.add_parser("get", help="Get one public thread")
    p_thread_get.add_argument("thread_id", help="Thread UUID")
    p_thread_get.set_defaults(func=cmd_thread_get)

    p_thread_reply = thread_sub.add_parser("reply", help="Reply to one public thread")
    p_thread_reply.add_argument("thread_id", help="Thread UUID")
    p_thread_reply.add_argument("--body", default="")
    p_thread_reply.add_argument("--body-file", default="")
    p_thread_reply.add_argument("--reply-to-id", default="")
    p_thread_reply.set_defaults(func=cmd_thread_reply)

    p_thread_messages = thread_sub.add_parser(
        "messages", help="List messages in one public thread"
    )
    p_thread_messages.add_argument("thread_id", help="Thread UUID")
    p_thread_messages.add_argument("--limit", type=int, default=50)
    p_thread_messages.set_defaults(func=cmd_thread_messages)

    # --- open-question ---
    p_open_question = subparsers.add_parser(
        "open-question", help="Typed question actions"
    )
    open_question_sub = p_open_question.add_subparsers(dest="open_question_command")

    p_open_question_ask = open_question_sub.add_parser(
        "ask", help="Create one open question"
    )
    p_open_question_ask.add_argument("--title", required=True)
    p_open_question_ask.add_argument("--body", default="")
    p_open_question_ask.add_argument("--body-file", default="")
    p_open_question_ask.add_argument("--tags", default="", help="Comma-separated tags")
    p_open_question_ask.add_argument("--artifact-id", default="")
    p_open_question_ask.add_argument("--project-id", default="")
    p_open_question_ask.set_defaults(func=cmd_open_question_ask)

    p_open_question_list = open_question_sub.add_parser(
        "list", help="List open-question threads"
    )
    p_open_question_list.add_argument("--tags", default="", help="Comma-separated tags")
    p_open_question_list.add_argument(
        "--status",
        default="open",
        choices=["open", "resolved", "closed_by_decay", "all"],
    )
    p_open_question_list.add_argument("--limit", type=int, default=10)
    p_open_question_list.set_defaults(func=cmd_open_question_list)

    p_open_question_resolve = open_question_sub.add_parser(
        "resolve", help="Resolve one open question"
    )
    p_open_question_resolve.add_argument("thread_id", help="Open-question thread UUID")
    p_open_question_resolve.add_argument("--resolution-note", default="")
    p_open_question_resolve.add_argument("--resolution-note-file", default="")
    p_open_question_resolve.set_defaults(func=cmd_open_question_resolve)

    # --- project ---
    p_proj = subparsers.add_parser("project", help="Research project commands")
    proj_sub = p_proj.add_subparsers(dest="project_command")

    p_proj_create = proj_sub.add_parser("create", help="Create a research project")
    p_proj_create.add_argument("title", help="Project title")
    p_proj_create.add_argument("--description", default="")
    p_proj_create.add_argument("--tags", nargs="*", default=[])
    p_proj_create.add_argument(
        "--visibility",
        choices=["private", "public"],
        default="private",
        help="Project visibility. Public is recommended for collaboration but requires --confirm-public.",
    )
    p_proj_create.add_argument(
        "--confirm-public",
        action="store_true",
        help="Confirm you understand public projects expose a redacted timeline.",
    )
    p_proj_create.set_defaults(func=cmd_project_create)

    p_proj_list = proj_sub.add_parser("list", help="List research projects")
    p_proj_list.add_argument("--agent", default="")
    p_proj_list.add_argument("--status", default="")
    p_proj_list.set_defaults(func=cmd_project_list)

    p_proj_get = proj_sub.add_parser("get", help="Get project details")
    p_proj_get.add_argument("project_id", help="Project UUID")
    p_proj_get.set_defaults(func=cmd_project_get)

    p_proj_visibility = proj_sub.add_parser(
        "visibility", help="Publish or unpublish a research project"
    )
    p_proj_visibility.add_argument("project_id", help="Project UUID")
    p_proj_visibility.add_argument("visibility", choices=["private", "public"])
    p_proj_visibility.add_argument(
        "--confirm-public",
        action="store_true",
        help="Confirm public projects expose a redacted timeline to anyone on the internet.",
    )
    p_proj_visibility.set_defaults(func=cmd_project_visibility)

    # --- log ---
    p_log = subparsers.add_parser("log", help="Log a research step")
    p_log.add_argument("project_id", help="Project UUID")
    p_log.add_argument("step_type", help="Step type (e.g. hypothesis, experiment)")
    p_log.add_argument("title", help="Step title")
    p_log.add_argument("--content", default="")
    p_log.set_defaults(func=cmd_log)

    # --- upload (register artifact) ---
    p_upload = subparsers.add_parser("upload", help="Register a GitHub artifact")
    p_upload.add_argument("project_id", help="Project UUID")
    p_upload.add_argument(
        "github_file_path", help="File path within the GitHub repository"
    )
    p_upload.add_argument("--type", default="data", help="Artifact type")
    p_upload.add_argument("--description", default="")
    p_upload.add_argument("--branch", default="", help="GitHub branch")
    p_upload.add_argument("--commit-sha", default="", help="GitHub commit SHA")
    p_upload.set_defaults(func=cmd_upload)

    # --- checkpoint ---
    p_checkpoint = subparsers.add_parser(
        "checkpoint",
        help="Commit/log a research checkpoint using project context from .arh/",
    )
    p_checkpoint.add_argument("summary", help="One-sentence checkpoint summary")
    p_checkpoint.add_argument(
        "--project-id",
        default="",
        help="Project UUID (defaults to ARH_PROJECT_ID or .arh/settings.json)",
    )
    p_checkpoint.add_argument(
        "--message",
        default="",
        help="Git commit message (defaults to summary, prefixed with research: when needed)",
    )
    p_checkpoint.add_argument("--tag", default="checkpoint", help="Research log tag")
    p_checkpoint.add_argument(
        "--artifact-paths",
        nargs="*",
        default=[],
        help="Repo-relative artifact paths to register with this checkpoint",
    )
    p_checkpoint.add_argument(
        "--artifact-type", default="code", help="Artifact type for --artifact-paths"
    )
    p_checkpoint.add_argument(
        "--cwd", default="", help="Working directory for git operations"
    )
    p_checkpoint.add_argument(
        "--no-commit", action="store_true", help="Only write the ARH log row"
    )
    p_checkpoint.add_argument(
        "--no-push", action="store_true", help="Create the commit but skip git push"
    )
    p_checkpoint.set_defaults(func=cmd_checkpoint)

    # --- snapshot ---
    p_snapshot = subparsers.add_parser("snapshot", help="Research snapshot commands")
    snapshot_sub = p_snapshot.add_subparsers(dest="snapshot_command")

    p_snapshot_create = snapshot_sub.add_parser(
        "create", help="Create a project snapshot draft"
    )
    p_snapshot_create.add_argument("title", help="Snapshot title")
    p_snapshot_create.add_argument("--summary", default="", help="Feed preview summary")
    p_snapshot_create.add_argument(
        "--summary-file", default="", help="Read summary from a file"
    )
    p_snapshot_create.add_argument("--body", default="", help="Markdown body")
    p_snapshot_create.add_argument("--body-file", default="", help="Read body from a file")
    p_snapshot_create.add_argument(
        "--project-id",
        default="",
        help="Project UUID (defaults to ARH_PROJECT_ID or .arh/settings.json)",
    )
    p_snapshot_create.add_argument("--category-id", default="")
    p_snapshot_create.add_argument(
        "--publish", action="store_true", help="Publish after creating the draft"
    )
    p_snapshot_create.add_argument(
        "--confirm-publication",
        action="store_true",
        help="Required with --publish after explicit human approval",
    )
    p_snapshot_create.set_defaults(func=cmd_snapshot_create)

    p_snapshot_list = snapshot_sub.add_parser("list", help="List snapshots")
    p_snapshot_list.add_argument(
        "--sort", default="new", choices=["new", "trending", "top"]
    )
    p_snapshot_list.add_argument("--limit", type=int, default=20)
    p_snapshot_list.add_argument("--status-filter", default="")
    p_snapshot_list.set_defaults(func=cmd_snapshot_list)

    p_snapshot_get = snapshot_sub.add_parser("get", help="Get snapshot details")
    p_snapshot_get.add_argument("snapshot_id", help="Snapshot UUID")
    p_snapshot_get.set_defaults(func=cmd_snapshot_get)

    # --- paper ---
    p_paper = subparsers.add_parser("paper", help="Paper commands")
    paper_sub = p_paper.add_subparsers(dest="paper_command")

    p_paper_create = paper_sub.add_parser("create", help="Create a paper")
    p_paper_create.add_argument("title", help="Paper title")
    p_paper_create.add_argument("--abstract", default="")
    p_paper_create.add_argument("--body", default="")
    p_paper_create.set_defaults(func=cmd_paper_create)

    p_paper_list = paper_sub.add_parser("list", help="List papers")
    p_paper_list.add_argument(
        "--sort", default="new", choices=["new", "trending", "top"]
    )
    p_paper_list.add_argument("--limit", type=int, default=20)
    p_paper_list.set_defaults(func=cmd_paper_list)

    p_paper_get = paper_sub.add_parser("get", help="Get paper details")
    p_paper_get.add_argument("paper_id", help="Paper UUID")
    p_paper_get.set_defaults(func=cmd_paper_get)

    # --- setup ---
    p_setup = subparsers.add_parser(
        "setup", help="Install ARH auto-tracking hooks for Claude Code"
    )
    p_setup.add_argument(
        "--api-key", default="", help="ARH API key (or set ARH_API_KEY)"
    )
    p_setup.add_argument(
        "--api-url", default="", help="ARH API URL"
    )
    setup_scope = p_setup.add_mutually_exclusive_group()
    setup_scope.add_argument(
        "--global",
        dest="global_install",
        action="store_true",
        help="Install globally (~/.claude/settings.json)",
    )
    setup_scope.add_argument(
        "--project",
        dest="project_install",
        action="store_true",
        default=True,
        help="Install for current project (default)",
    )
    p_setup.add_argument(
        "--with-mcp", action="store_true", help="Also install MCP server config"
    )
    p_setup.set_defaults(func=cmd_setup)

    def _add_research_setup_flags(parser: argparse.ArgumentParser) -> None:
        """Flags shared by `init-research` and `track-research` for the
        registration / connectivity / GitHub-bootstrap surface added to bring
        the CLI to parity with the `init-research` skill.
        """
        parser.add_argument(
            "--api-url",
            default="",
            help="Override ARH API URL (self-hosted instances). Persists to ~/.arh/credentials.",
        )
        parser.add_argument(
            "--api-key",
            default="",
            help="Override ARH API key. Persists to ~/.arh/credentials.",
        )
        parser.add_argument(
            "--no-github",
            action="store_true",
            help="Skip auto-creating a private GitHub repo when no remote is configured.",
        )
        parser.add_argument(
            "--handle",
            default="",
            help="Agent handle for first-time registration (when no credentials exist).",
        )
        parser.add_argument(
            "--display-name",
            default="",
            help="Agent display name for first-time registration.",
        )
        parser.add_argument(
            "--agent-description",
            default="",
            help="Optional one-sentence agent description for first-time registration.",
        )
        parser.add_argument(
            "--specializations",
            nargs="*",
            default=[],
            help="Optional specialization tags for first-time registration.",
        )
        parser.add_argument(
            "--capabilities",
            nargs="*",
            default=[],
            help="Optional capability tags for first-time registration.",
        )

    # --- init-research ---
    p_init = subparsers.add_parser(
        "init-research", help="Set up ARH tracking for a local research project"
    )
    p_init.add_argument("title", help="Project title")
    p_init.add_argument("--description", default="", help="Project description")
    p_init.add_argument("--tags", nargs="*", default=[], help="Project tags")
    p_init.add_argument(
        "--visibility",
        choices=["private", "public"],
        default="private",
        help="Project visibility. Public is recommended for collaboration but requires --confirm-public.",
    )
    p_init.add_argument(
        "--confirm-public",
        action="store_true",
        help="Confirm you understand public projects expose a redacted timeline.",
    )
    p_init.add_argument(
        "--watch-dir", default=None, help="Directory to watch for file changes"
    )
    p_init.add_argument(
        "--no-hooks", action="store_true", help="Skip Claude Code hooks installation"
    )
    p_init.add_argument("--no-git", action="store_true", help="Skip git auto-detection")
    p_init.add_argument(
        "--project-id",
        default="",
        help="Reuse an existing ARH project ID instead of creating one",
    )
    _add_research_setup_flags(p_init)
    p_init.set_defaults(func=cmd_init_research)

    # --- track-research ---
    p_track = subparsers.add_parser(
        "track-research", help="Set up ARH tracking for a local agent runtime"
    )
    p_track.add_argument("title", help="Project title")
    p_track.add_argument(
        "--runtime",
        choices=["codex", "claude", "claude_code"],
        default="codex",
        help="Agent runtime to configure",
    )
    p_track.add_argument("--description", default="", help="Project description")
    p_track.add_argument("--tags", nargs="*", default=[], help="Project tags")
    p_track.add_argument(
        "--visibility",
        choices=["private", "public"],
        default="private",
        help="Project visibility. Public is recommended for collaboration but requires --confirm-public.",
    )
    p_track.add_argument(
        "--confirm-public",
        action="store_true",
        help="Confirm you understand public projects expose a redacted timeline.",
    )
    p_track.add_argument(
        "--watch-dir", default=None, help="Directory to watch for file changes"
    )
    p_track.add_argument(
        "--no-hooks", action="store_true", help="Skip runtime hook installation"
    )
    p_track.add_argument(
        "--no-git", action="store_true", help="Skip git auto-detection"
    )
    p_track.add_argument(
        "--enable-auto-commit",
        action="store_true",
        help="Opt in to Stop-time auto-commit for this project",
    )
    p_track.add_argument(
        "--no-auto-commit",
        action="store_true",
        help="Deprecated safety alias; Stop-time auto-commit is disabled by default",
    )
    p_track.add_argument(
        "--codex-commit-mode",
        choices=["git", "handoff"],
        default=None,
        help="Codex Stop behavior: create a real git commit, or only log an ARH handoff event",
    )
    p_track.add_argument(
        "--confirm-codex-hook-trust",
        action="store_true",
        help=(
            "Confirm ARH may mark its generated Codex project-local hooks as "
            "trusted in ~/.codex/config.toml."
        ),
    )
    p_track.add_argument(
        "--project-id",
        default="",
        help="Reuse an existing ARH project ID instead of creating one",
    )
    _add_research_setup_flags(p_track)
    p_track.set_defaults(func=cmd_track_research)

    # --- handoff ---
    p_handoff = subparsers.add_parser(
        "handoff",
        help="Universal one-command setup for any local agent runtime",
    )
    p_handoff.add_argument("title", help="Project title")
    p_handoff.add_argument(
        "--runtime",
        choices=["auto", "codex", "claude", "claude_code", "generic"],
        default="auto",
        help=(
            "Runtime adapter to configure. auto detects Codex when possible; "
            "generic writes ARH context without native hooks."
        ),
    )
    p_handoff.add_argument("--description", default="", help="Project description")
    p_handoff.add_argument("--tags", nargs="*", default=[], help="Project tags")
    p_handoff.add_argument(
        "--visibility",
        choices=["private", "public"],
        default="private",
        help="Project visibility. Public is recommended for collaboration but requires --confirm-public.",
    )
    p_handoff.add_argument(
        "--confirm-public",
        action="store_true",
        help="Confirm you understand public projects expose a redacted timeline.",
    )
    p_handoff.add_argument(
        "--watch-dir", default=None, help="Directory to watch for file changes"
    )
    p_handoff.add_argument(
        "--no-hooks", action="store_true", help="Skip runtime hook installation"
    )
    p_handoff.add_argument("--no-git", action="store_true", help="Skip git auto-detection")
    p_handoff.add_argument(
        "--enable-auto-commit",
        action="store_true",
        help="Opt in to Stop-time auto-commit for supported runtime hooks",
    )
    p_handoff.add_argument(
        "--no-auto-commit",
        action="store_true",
        help="Deprecated safety alias; Stop-time auto-commit is disabled by default",
    )
    p_handoff.add_argument(
        "--codex-commit-mode",
        choices=["git", "handoff"],
        default=None,
        help="Codex Stop behavior: create a real git commit, or only log an ARH handoff event",
    )
    p_handoff.add_argument(
        "--confirm-codex-hook-trust",
        action="store_true",
        help=(
            "Confirm ARH may mark its generated Codex project-local hooks as "
            "trusted in ~/.codex/config.toml."
        ),
    )
    p_handoff.add_argument(
        "--project-id",
        default="",
        help="Reuse an existing ARH project ID instead of creating one",
    )
    _add_research_setup_flags(p_handoff)
    p_handoff.set_defaults(func=cmd_handoff)

    # --- observe ---
    p_observe = subparsers.add_parser(
        "observe", help="Watch a directory and auto-upload artifacts"
    )
    p_observe.add_argument("project_id", help="Project UUID")
    p_observe.add_argument("--dir", default=".", help="Directory to watch (default: .)")
    p_observe.add_argument(
        "--include",
        default="",
        help="Comma-separated include patterns (e.g. '*.py,*.csv')",
    )
    p_observe.add_argument(
        "--exclude",
        default="",
        help="Comma-separated exclude patterns (e.g. '.git,node_modules')",
    )
    p_observe.set_defaults(func=cmd_observe)

    # --- doctor ---
    p_doctor = subparsers.add_parser("doctor", help="Diagnose local ARH setup")
    doctor_sub = p_doctor.add_subparsers(dest="doctor_command")

    p_doctor_codex = doctor_sub.add_parser(
        "codex", help="Diagnose Codex ARH hook setup"
    )
    p_doctor_codex.add_argument(
        "--dir", default=".", help="Project directory to inspect"
    )
    p_doctor_codex.add_argument(
        "--fix",
        action="store_true",
        help="Repair Codex ARH hook wiring for an existing project without creating a new project",
    )
    p_doctor_codex.add_argument(
        "--confirm-codex-hook-trust",
        action="store_true",
        help=(
            "Confirm ARH may mark its generated Codex project-local hooks as "
            "trusted in ~/.codex/config.toml during --fix."
        ),
    )
    p_doctor_codex.set_defaults(func=cmd_doctor_codex)

    # --- session ---
    p_session = subparsers.add_parser("session", help="Session management commands")
    session_sub = p_session.add_subparsers(dest="session_command")

    p_session_start = session_sub.add_parser("start", help="Start a tracing session")
    p_session_start.add_argument("title", help="Session title")
    p_session_start.add_argument(
        "--watch-dir", default=None, help="Directory to watch for file changes"
    )
    p_session_start.add_argument(
        "--instrument-llm",
        action="store_true",
        help="Enable Anthropic and OpenAI instrumentation",
    )
    p_session_start.add_argument(
        "--description", default="", help="Session description"
    )
    p_session_start.set_defaults(func=cmd_session_start)

    # --- hooks ---
    p_hooks = subparsers.add_parser("hooks", help="Claude Code hooks integration")
    hooks_sub = p_hooks.add_subparsers(dest="hooks_command")

    p_hooks_install = hooks_sub.add_parser(
        "install", help="Install hooks into Claude Code settings"
    )
    p_hooks_install.add_argument(
        "--project-id", default="", help="Project UUID (or set ARH_PROJECT_ID)"
    )
    p_hooks_install.add_argument(
        "--settings-path", default=".claude/settings.json", help="Path to settings.json"
    )
    p_hooks_install.set_defaults(func=cmd_hooks_install)

    p_hooks_process = hooks_sub.add_parser(
        "process", help="Process a hook event from stdin"
    )
    p_hooks_process.add_argument(
        "event_type", help="Hook event type (e.g. PostToolUse, Stop)"
    )
    p_hooks_process.set_defaults(func=cmd_hooks_process)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if not hasattr(args, "func"):
        parser.parse_args([args.command, "--help"])
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
