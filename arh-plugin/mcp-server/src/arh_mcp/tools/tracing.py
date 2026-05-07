from __future__ import annotations

import json
import os
import shlex
import subprocess
import time

from arh_client import APIClient, LogBuffer
from arh_client.observer import FileObserver

from arh_mcp.client import arh_client

_SHADOW_REF_PREFIX = "refs/heads/arh-auto/"

# Active file observers, keyed by project_id
_observers: dict[str, FileObserver] = {}
# Supporting objects kept alive alongside each observer
_log_buffers: dict[str, LogBuffer] = {}
_api_clients: dict[str, APIClient] = {}


def _start_observer(
    project_id: str,
    watch_dir: str = ".",
    include: str | None = None,
    exclude: str | None = None,
) -> str:
    """Start a file observer for a project. Returns a status message."""
    if project_id in _observers:
        return f"File observer already running for project {project_id}"

    # Create a sync APIClient (used by FileObserver/LogBuffer in background threads)
    client = APIClient(
        api_key=arh_client.api_key,
        base_url=arh_client.base_url,
    )
    log_buffer = LogBuffer(project_id=project_id, client=client)

    include_list = [p.strip() for p in include.split(",")] if include else None
    exclude_list = [p.strip() for p in exclude.split(",")] if exclude else None

    observer = FileObserver(
        project_id=project_id,
        client=client,
        log_buffer=log_buffer,
        watch_dir=watch_dir,
        include=include_list,
        exclude=exclude_list,
    )

    log_buffer.start()
    observer.start()

    _observers[project_id] = observer
    _log_buffers[project_id] = log_buffer
    _api_clients[project_id] = client

    msg = f"File observer started for project {project_id}, watching: {watch_dir}"
    if include_list:
        msg += f", include: {include_list}"
    if exclude_list:
        msg += f", exclude: {exclude_list}"
    return msg


def _stop_observer(project_id: str) -> str:
    """Stop a file observer for a project. Returns a status message."""
    observer = _observers.pop(project_id, None)
    if observer is None:
        return f"No file observer running for project {project_id}"

    observer.stop()

    log_buffer = _log_buffers.pop(project_id, None)
    if log_buffer is not None:
        log_buffer.stop()

    api_client = _api_clients.pop(project_id, None)
    if api_client is not None:
        api_client.close()

    return f"File observer stopped for project {project_id}"


def register(mcp):
    @mcp.tool()
    async def start_session(
        title: str = "Untitled Research",
        description: str | None = None,
        tags: list[str] | None = None,
        visibility: str = "private",
        confirm_public: bool = False,
        watch_dir: str | None = None,
        include: str | None = None,
        exclude: str | None = None,
    ) -> str:
        """Start a new research session: create project and optionally start file observer.

        Args:
            title: Project title
            description: Optional project description
            tags: Optional list of tags
            visibility: "private" by default, or "public" for collaboration.
            confirm_public: Required when visibility is "public".
            watch_dir: If provided, start file observer on this directory
            include: Comma-separated glob patterns to include
            exclude: Comma-separated glob patterns to exclude
        """
        if visibility not in ("private", "public"):
            return "Error: visibility must be 'private' or 'public'."
        if visibility == "public" and not confirm_public:
            return (
                "Error: public tracking requires confirm_public=True after the "
                "human approves publication of the redacted timeline."
            )
        # Create project via API
        result = await arh_client.post(
            "/v1/research/projects",
            json={
                "title": title,
                "description": description,
                "tags": tags or [],
                "metadata": {"source": "mcp_session"},
                "visibility": visibility,
                **({"confirm_public": True} if visibility == "public" else {}),
            },
        )
        project_id = result["id"]

        msg = f"Research session started. Project: {project_id} — {title}"

        # Optionally start file observer
        if watch_dir:
            obs_msg = _start_observer(project_id, watch_dir, include, exclude)
            msg += f"\n{obs_msg}"

        return msg

    @mcp.tool()
    async def end_session(project_id: str) -> str:
        """End a research session: stop file observer.

        Args:
            project_id: Research project ID to end
        """
        msgs = []

        # Stop observer if running
        if project_id in _observers:
            obs_msg = _stop_observer(project_id)
            msgs.append(obs_msg)

        return "\n".join(msgs) if msgs else f"Session {project_id} ended."

    @mcp.tool()
    async def setup_auto_tracking(
        project_dir: str,
        project_id: str | None = None,
        scope: str = "project",
        install_claude_hooks: bool | None = None,
    ) -> str:
        """Configure auto-tracking for a research project.

        Writes project context to `<project_dir>/.arh/`, installs a git
        post-commit hook for ARH commit telemetry, and optionally installs
        Claude Code hook entries in `.claude/settings.json`. Credentials are
        read from `~/.arh/credentials`.

        Args:
            project_dir: Absolute path to the user's project directory.
            project_id: Optional research project ID to associate with hooks.
            scope: "project" → `<project_dir>/.claude/settings.json`,
                   "global" → `~/.claude/settings.json`.
            install_claude_hooks:
                None (default) → auto-detect. When this MCP server is loaded
                from inside the ARH plugin (i.e. plugin's `hooks/hooks.json`
                is already routing events to hook-handler.py), installation
                is SKIPPED to avoid double-firing every tool call. Otherwise
                installation proceeds (legacy non-plugin path).
                True → force install regardless.
                False → never install; only write config + git hook.
        """
        api_key = arh_client.api_key
        api_url = arh_client.base_url
        if not api_key:
            return "Error: ARH_API_KEY not configured. Cannot configure tracking."

        # Find hook-handler.py relative to the plugin
        # __file__ is at mcp-server/src/arh_mcp/tools/tracing.py
        # plugin root is 4 levels up: tools/ -> arh_mcp/ -> src/ -> mcp-server/ -> plugin_root/
        mcp_server_dir = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        )
        plugin_root = os.path.dirname(mcp_server_dir)
        hook_handler = os.path.join(plugin_root, "scripts", "hook-handler.py")
        inject_trace = os.path.join(plugin_root, "scripts", "inject-trace-context.sh")

        if not os.path.isfile(hook_handler):
            return f"Error: hook-handler.py not found at {hook_handler}"

        # Auto-detect: when the plugin ships hooks/hooks.json AND the plugin
        # manifest exists, this MCP server is part of a loaded plugin, so
        # Claude Code is already routing events through hook-handler.py via
        # the plugin path. Installing again into .claude/settings.json would
        # double every tool call.
        plugin_manifest = os.path.join(plugin_root, ".claude-plugin", "plugin.json")
        plugin_hooks_json = os.path.join(plugin_root, "hooks", "hooks.json")
        plugin_active = os.path.isfile(plugin_manifest) and os.path.isfile(
            plugin_hooks_json
        )
        if install_claude_hooks is None:
            install_claude_hooks = not plugin_active

        # Write project-local config to .arh/.env. Single source of truth model:
        # the API key is NOT persisted here — the hook handler reads
        # ~/.arh/credentials directly. This prevents the round-5 silent-override
        # bug where a stale .arh/.env kept re-asserting an old key after
        # `register_agent` rewrote ~/.arh/credentials.
        arh_dir = os.path.join(project_dir, ".arh")
        os.makedirs(arh_dir, exist_ok=True)
        env_path = os.path.join(arh_dir, ".env")
        env_lines: list[str] = []
        if api_url and api_url != "https://api.airesearcherhub.com":
            env_lines.append(f"ARH_API_URL={api_url}")
        if project_id:
            env_lines.append(f"ARH_PROJECT_ID={project_id}")

        # Preserve any unrelated keys the user may have added by hand, but
        # strip legacy ARH_API_KEY entries — those are the ones that caused
        # the override bug.
        existing_extra: list[str] = []
        if os.path.isfile(env_path):
            try:
                with open(env_path) as f:
                    for line in f:
                        stripped = line.strip()
                        if not stripped or stripped.startswith("#"):
                            existing_extra.append(line.rstrip("\n"))
                            continue
                        if "=" not in stripped:
                            existing_extra.append(line.rstrip("\n"))
                            continue
                        key = stripped.split("=", 1)[0].strip()
                        if key in ("ARH_API_KEY", "ARH_API_URL", "ARH_PROJECT_ID"):
                            continue  # rewritten below or intentionally dropped
                        existing_extra.append(line.rstrip("\n"))
            except OSError:
                pass

        with open(env_path, "w") as f:
            for extra in existing_extra:
                f.write(extra + "\n")
            for line in env_lines:
                f.write(line + "\n")

        events = [
            "SessionStart",
            "PostToolUse",
            "Stop",
            "SubagentStop",
            "Notification",
            "TaskCompleted",
        ]

        hooks_msg = ""
        settings_path = ""
        if install_claude_hooks:
            if scope == "global":
                settings_path = os.path.expanduser("~/.claude/settings.json")
            else:
                settings_path = os.path.join(project_dir, ".claude", "settings.json")

            # Load existing settings
            settings: dict = {}
            if os.path.exists(settings_path):
                with open(settings_path, "r") as f:
                    settings = json.load(f)

            hooks = settings.get("hooks", {})

            for event in events:
                command = f"python3 {shlex.quote(hook_handler)} {event}"
                hook_commands = [{"type": "command", "command": command}]

                # For SessionStart, also inject trace context from .arh-trace
                if event == "SessionStart" and os.path.isfile(inject_trace):
                    hook_commands.insert(
                        0, {"type": "command", "command": f"bash {shlex.quote(inject_trace)}"}
                    )

                new_entry = {
                    "matcher": "",
                    "hooks": hook_commands,
                }

                if event not in hooks:
                    hooks[event] = [new_entry]
                else:
                    # Remove existing ARH hooks, then add fresh
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

            hooks_msg = (
                f"Claude Code hooks installed to {settings_path}\n"
                f"Events: {', '.join(events)}\n"
            )
        else:
            # Plugin path active — also strip any leftover ARH entries from
            # an older setup_auto_tracking run so we don't keep firing twice.
            local_settings = os.path.join(project_dir, ".claude", "settings.json")
            if os.path.exists(local_settings):
                try:
                    with open(local_settings, "r") as f:
                        existing = json.load(f)
                    existing_hooks = existing.get("hooks", {})
                    cleaned_any = False
                    for event in events:
                        if event in existing_hooks:
                            before = existing_hooks[event]
                            after = [
                                e
                                for e in before
                                if not any(
                                    "hook-handler.py" in h.get("command", "")
                                    or "inject-trace-context" in h.get("command", "")
                                    for h in e.get("hooks", [])
                                )
                            ]
                            if len(after) != len(before):
                                cleaned_any = True
                                if after:
                                    existing_hooks[event] = after
                                else:
                                    del existing_hooks[event]
                    if cleaned_any:
                        existing["hooks"] = existing_hooks
                        with open(local_settings, "w") as f:
                            json.dump(existing, f, indent=2)
                            f.write("\n")
                        hooks_msg = (
                            f"Plugin hooks active — removed stale ARH entries "
                            f"from {local_settings} to avoid double-firing.\n"
                        )
                    else:
                        hooks_msg = (
                            "Plugin hooks active — skipping .claude/settings.json "
                            "install (the plugin's hooks/hooks.json already routes "
                            "events to hook-handler.py).\n"
                        )
                except (OSError, json.JSONDecodeError):
                    hooks_msg = "Plugin hooks active — skipping .claude/settings.json install.\n"
            else:
                hooks_msg = (
                    "Plugin hooks active — skipping .claude/settings.json install.\n"
                )

        # Also write project_id to .arh/settings.json so all sessions
        # (including subagents) in this directory use the same project
        if project_id and scope != "global":
            arh_settings_path = os.path.join(arh_dir, "settings.json")
            arh_settings: dict = {}
            if os.path.isfile(arh_settings_path):
                try:
                    with open(arh_settings_path, "r") as f:
                        arh_settings = json.load(f)
                except (json.JSONDecodeError, OSError):
                    pass
            arh_settings["project_id"] = project_id
            os.makedirs(arh_dir, exist_ok=True)
            with open(arh_settings_path, "w") as f:
                json.dump(arh_settings, f, indent=2)
                f.write("\n")

        # Install git post-commit hook if project_id is provided and we're in a git repo
        git_hook_msg = ""
        if project_id:
            from arh_client.git_tracker import install_post_commit_hook

            hook_path = install_post_commit_hook(
                project_id,
                api_url,
                api_key,
                repo_dir=project_dir,
            )
            if hook_path:
                git_hook_msg = f"Git post-commit hook installed: {hook_path}\n"
            else:
                git_hook_msg = "Note: No git repository detected — post-commit hook not installed.\n"

        return (
            f"Auto-tracking configured for {project_dir}.\n"
            f"{hooks_msg}"
            f"{git_hook_msg}"
            f"All future Claude Code sessions will be automatically tracked."
        )

    @mcp.tool()
    async def create_trace_context(
        project_id: str | None = None,
        title: str | None = None,
        metadata: dict | None = None,
    ) -> str:
        """Create a trace context for coordinating multi-agent work.

        This creates a shared context that links multiple agents/sessions working
        on the same task. A .arh-trace file is written to the current directory
        so that team agents automatically join the trace via SessionStart hooks.

        Args:
            project_id: Optional root research project ID
            title: Optional title for the trace
            metadata: Optional metadata dict
        """
        body: dict = {}
        if project_id:
            body["project_id"] = project_id
        if title:
            body["title"] = title
        if metadata:
            body["metadata"] = metadata

        result = await arh_client.post("/v1/traces", json=body)
        trace_id = result.get("trace_id", "")

        # Write .arh-trace file for automatic propagation to team agents
        trace_file_data = {
            "trace_id": trace_id,
            "created_at": result.get("created_at", ""),
            "title": title or "",
        }
        trace_file_path = os.path.join(os.getcwd(), ".arh-trace")
        try:
            with open(trace_file_path, "w") as f:
                json.dump(trace_file_data, f, indent=2)
                f.write("\n")
        except OSError as e:
            return (
                f"Trace context created (trace_id: {trace_id}) but failed to write "
                f".arh-trace file: {e}\n"
                f"Team agents will need to join manually with: join_trace(trace_id='{trace_id}')"
            )

        return (
            f"Trace context created.\n"
            f"  trace_id: {trace_id}\n"
            f"  .arh-trace written to: {trace_file_path}\n"
            f"Team agents sharing this working directory will automatically join."
        )

    @mcp.tool()
    async def join_trace(
        trace_id: str,
        role: str = "participant",
        display_label: str | None = None,
    ) -> str:
        """Manually join an existing trace context.

        Use this when automatic propagation via .arh-trace is not available
        (e.g., agent is in a different directory).

        Args:
            trace_id: The trace_id to join
            role: Role in the trace (e.g., "orchestrator", "coder", "tester")
            display_label: Optional human-readable label for this participant
        """
        body: dict = {"role": role}
        if display_label:
            body["display_label"] = display_label

        result = await arh_client.post(f"/v1/traces/{trace_id}/join", json=body)
        participant_id = result.get("participant_id", "")
        return (
            f"Joined trace {trace_id}\n"
            f"  participant_id: {participant_id}\n"
            f"  role: {role}"
        )

    @mcp.tool()
    async def prune_shadow_refs(
        repo_dir: str,
        older_than_days: int = 14,
        dry_run: bool = False,
    ) -> str:
        """Delete per-session shadow refs (`refs/heads/arh-auto/*`) older than a threshold.

        The harness creates one shadow ref per Claude Code session for audit
        and timeline reconstruction. Refs accumulate forever otherwise.
        Manual invocation: run periodically (e.g. monthly) to keep the ref
        directory bounded.

        Args:
            repo_dir: Absolute path to the git repository to clean.
            older_than_days: Delete refs whose tip commit is older than this
                threshold. Default 14. The threshold is conservative — auto
                refs aren't pushed and are cheap (40 bytes each), but a session
                only matters for audit ~2 weeks after the fact.
            dry_run: If True, list what would be deleted without deleting.

        Returns: a summary line plus one line per ref pruned (or would-prune).
        """
        if not os.path.isdir(os.path.join(repo_dir, ".git")) and not os.path.isfile(
            os.path.join(repo_dir, ".git")
        ):
            return f"Error: {repo_dir} is not a git repository."
        if older_than_days < 1:
            return "Error: older_than_days must be >= 1."

        now = int(time.time())
        cutoff = now - (older_than_days * 24 * 3600)

        # List shadow refs with committer timestamp.
        list_proc = subprocess.run(
            [
                "git",
                "for-each-ref",
                "--format=%(refname) %(committerdate:unix)",
                _SHADOW_REF_PREFIX,
            ],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if list_proc.returncode != 0:
            return f"Error listing refs: {list_proc.stderr.strip()}"

        candidates: list[tuple[str, int]] = []
        for line in list_proc.stdout.splitlines():
            parts = line.strip().split()
            if len(parts) != 2:
                continue
            ref, ts_str = parts
            try:
                ts = int(ts_str)
            except ValueError:
                continue
            if ts < cutoff:
                candidates.append((ref, ts))

        if not candidates:
            return (
                f"No shadow refs older than {older_than_days}d found under "
                f"{_SHADOW_REF_PREFIX} in {repo_dir}."
            )

        lines: list[str] = []
        deleted = 0
        for ref, ts in candidates:
            age_days = (now - ts) / 86400
            if dry_run:
                lines.append(f"  would prune: {ref}  (age {age_days:.1f}d)")
            else:
                # update-ref -d <ref> deletes the ref. No reflog kept since
                # auto refs aren't user-facing branches.
                del_proc = subprocess.run(
                    ["git", "update-ref", "-d", ref],
                    cwd=repo_dir,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if del_proc.returncode == 0:
                    deleted += 1
                    lines.append(f"  pruned: {ref}  (age {age_days:.1f}d)")
                else:
                    lines.append(f"  failed: {ref}  ({del_proc.stderr.strip()})")

        verb = "would prune" if dry_run else "pruned"
        header = (
            f"{verb} {len(candidates) if dry_run else deleted} shadow ref(s) "
            f"older than {older_than_days}d in {repo_dir}:"
        )
        return header + "\n" + "\n".join(lines)
