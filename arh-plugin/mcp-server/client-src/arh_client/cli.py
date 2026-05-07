import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time

import httpx
from dotenv import load_dotenv


def _get_client():
    from arh_client.api import APIClient
    from arh_client.config import configure

    load_dotenv()

    creds = _read_credentials()
    api_key = os.environ.get("ARH_API_KEY", creds.get("api_key", ""))
    api_url = os.environ.get(
        "ARH_API_URL",
        creds.get("api_url", "https://api.airesearcherhub.com"),
    )
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


def _api_timeout_seconds() -> float:
    raw = os.environ.get("ARH_HTTP_TIMEOUT", "90")
    try:
        value = float(raw)
    except ValueError:
        return 90.0
    return max(value, 1.0)


def _print_json(data):
    print(json.dumps(data, indent=2, default=str))


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
    project_id, summary = _run_research_setup(args)
    _print_research_setup_summary(args, project_id, summary)

    # Output project ID to stdout for scripting
    print(project_id)


def cmd_track_research(args):
    project_id, summary = _run_research_setup(args)
    codex_hooks = False
    if args.runtime == "codex" and not args.no_hooks:
        try:
            hook_path, config_path = _install_codex_hooks(os.getcwd())
            codex_hooks = True
            print(f"Codex hooks installed: {hook_path}", file=sys.stderr)
            print(f"Codex hooks enabled:   {config_path}", file=sys.stderr)
        except Exception as e:
            print(f"Warning: failed to install Codex hooks: {e}", file=sys.stderr)

    summary["codex_hooks"] = codex_hooks
    _print_research_setup_summary(args, project_id, summary)

    # Output project ID to stdout for scripting
    print(project_id)


def _run_research_setup(args):
    from arh_client.git_tracker import detect_git_info, install_post_commit_hook

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
        data = {"title": args.title}
        if args.description:
            data["description"] = args.description
        if args.tags:
            data["tags"] = args.tags
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
    creds = _read_credentials()
    api_url = os.environ.get("ARH_API_URL", creds.get("api_url", "https://api.airesearcherhub.com"))
    api_key = os.environ.get("ARH_API_KEY", creds.get("api_key", ""))
    if api_key:
        _persist_credentials(api_key, api_url)
    gitleaks_ok, gitleaks_status = _ensure_gitleaks_available()
    print(f"Gitleaks: {gitleaks_status}", file=sys.stderr)
    _write_arh_project_context(
        os.getcwd(),
        api_url,
        project_id,
        runtime=getattr(args, "runtime", ""),
        auto_commit=not getattr(args, "no_auto_commit", False),
        codex_commit_mode=getattr(args, "codex_commit_mode", None),
        secret_scan_required=True,
    )

    # 3. Link git repository
    repo_linked = False
    if git_remote and not args.no_git:
        try:
            client.link_repository(project_id, git_remote, git_branch)
            repo_linked = True
            print(f"Git repository linked: {git_remote} ({git_branch})", file=sys.stderr)
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

    # 5. Watch directory info
    if args.watch_dir:
        watch_path = os.path.abspath(args.watch_dir)
        print(f"Watch directory: {watch_path} (use 'arh observe {project_id} --dir {args.watch_dir}' to start)", file=sys.stderr)

    # 6. Install Claude Code hooks (default: enabled)
    hooks_installed = False
    install_claude_hooks = getattr(args, "runtime", "claude") in {"claude", "claude_code"}
    if not args.no_hooks and install_claude_hooks:
        api_key_for_hooks = os.environ.get("ARH_API_KEY", "")
        api_url_for_hooks = os.environ.get("ARH_API_URL", "https://api.airesearcherhub.com")
        if api_key_for_hooks:
            try:
                _install_hooks_inline(
                    api_key_for_hooks,
                    api_url_for_hooks,
                    False,
                    False,
                    project_id,
                )
                hooks_installed = True
            except Exception as e:
                print(f"Warning: failed to install Claude Code hooks: {e}", file=sys.stderr)
        else:
            print("Warning: ARH_API_KEY not set, skipping hooks install", file=sys.stderr)

    summary = {
        "git_remote": git_remote,
        "git_branch": git_branch,
        "repo_linked": repo_linked,
        "git_hook": hook_installed,
        "claude_hooks": hooks_installed,
        "gitleaks": gitleaks_ok,
        "gitleaks_status": gitleaks_status,
    }
    return project_id, summary


def _print_research_setup_summary(args, project_id: str, summary: dict):
    print("\n--- Research Project Summary ---", file=sys.stderr)
    print(f"  Project ID: {project_id}", file=sys.stderr)
    print(f"  Title:      {args.title}", file=sys.stderr)
    if summary.get("repo_linked"):
        print(f"  Git Repo:   {summary.get('git_remote')}", file=sys.stderr)
        print(f"  Branch:     {summary.get('git_branch')}", file=sys.stderr)
    print(f"  Git Hook:   {'installed' if summary.get('git_hook') else 'not installed'}", file=sys.stderr)
    print(f"  Gitleaks:   {summary.get('gitleaks_status', 'not checked')}", file=sys.stderr)
    if args.watch_dir:
        print(f"  Watch Dir:  {os.path.abspath(args.watch_dir)}", file=sys.stderr)
    print(f"  CC Hooks:   {'installed' if summary.get('claude_hooks') else 'skipped'}", file=sys.stderr)
    if getattr(args, "runtime", "") == "codex":
        print(f"  Codex Hooks:{' installed' if summary.get('codex_hooks') else ' skipped'}", file=sys.stderr)
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
    api_key = args.api_key or os.environ.get("ARH_API_KEY", "")
    api_url = args.api_url or os.environ.get("ARH_API_URL", "https://api.airesearcherhub.com")

    if not api_key:
        print("Error: --api-key required or set ARH_API_KEY env var", file=sys.stderr)
        sys.exit(1)

    # Find the plugin's setup.py
    # Try common locations relative to the installed package or project
    setup_candidates = [
        os.path.join(os.getcwd(), "arh-plugin", "scripts", "setup.py"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))), "arh-plugin", "scripts", "setup.py"),
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
        cmd.extend(["--api-key", api_key, "--api-url", api_url])
        if args.with_mcp:
            cmd.append("--with-mcp")
        cmd.append("--quiet")

        result = _subprocess.run(cmd)
        sys.exit(result.returncode)
    else:
        # Fallback: install hooks inline using the same logic
        _install_hooks_inline(api_key, api_url, args.global_install, args.with_mcp)


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
    return None


def _find_codex_hook_handler() -> str | None:
    """Try to find the plugin's Codex hook handler."""
    plugin_root = os.environ.get("ARH_PLUGIN_ROOT", "")
    candidates = [
        os.path.join(plugin_root, "scripts", "codex-hook-handler.py") if plugin_root else "",
        os.path.join(os.getcwd(), "arh-plugin", "scripts", "codex-hook-handler.py"),
        os.path.join(
            os.path.dirname(
                os.path.dirname(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                )
            ),
            "scripts",
            "codex-hook-handler.py",
        ),
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
        new_entry = {
            "matcher": ".*",
            "hooks": [{"type": "command", "command": command}],
        }
        existing = hooks.get(event, [])
        hooks[event] = [
            entry
            for entry in existing
            if not any(
                "codex-hook-handler.py" in h.get("command", "")
                for h in entry.get("hooks", [])
            )
        ]
        hooks[event].append(new_entry)

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
            f.write("[features]\ncodex_hooks = true\n")
        return

    with open(config_path, "r") as f:
        lines = f.read().splitlines()

    for i, line in enumerate(lines):
        if line.strip().startswith("codex_hooks"):
            lines[i] = "codex_hooks = true"
            with open(config_path, "w") as f:
                f.write("\n".join(lines) + "\n")
            return

    features_idx = next(
        (i for i, line in enumerate(lines) if line.strip() == "[features]"),
        None,
    )
    if features_idx is not None:
        lines.insert(features_idx + 1, "codex_hooks = true")
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(["[features]", "codex_hooks = true"])

    with open(config_path, "w") as f:
        f.write("\n".join(lines) + "\n")


def _persist_credentials(api_key: str, api_url: str):
    """Write API credentials to ~/.arh/credentials."""
    global_dir = os.path.expanduser("~/.arh")
    os.makedirs(global_dir, exist_ok=True)
    creds_path = os.path.join(global_dir, "credentials")
    creds = {"api_key": api_key}
    if api_url:
        creds["api_url"] = api_url
    with open(creds_path, "w") as f:
        json.dump(creds, f, indent=2)
        f.write("\n")
    os.chmod(creds_path, 0o600)
    return creds_path


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

    brew = shutil.which("brew")
    if brew and _run_install_command([brew, "install", "gitleaks"]):
        installed = _gitleaks_path()
        if installed:
            return True, f"installed with Homebrew: {installed}"

    go = shutil.which("go")
    if go and _run_install_command([go, "install", "github.com/gitleaks/gitleaks/v8@latest"]):
        installed = _gitleaks_path()
        if installed:
            return True, f"installed with go install: {installed}"

    return False, "not installed; auto-commit will be blocked until gitleaks is available"


def _install_hooks_inline(api_key: str, api_url: str, global_install: bool, with_mcp: bool, project_id: str = ""):
    """Install hooks directly without the plugin's setup script."""
    hook_handler = _find_hook_handler()
    if not hook_handler:
        print("Error: Cannot find arh-plugin/scripts/hook-handler.py", file=sys.stderr)
        print("Run this command from the project root or install the plugin first.", file=sys.stderr)
        sys.exit(1)

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
    events = ["SessionStart", "PostToolUse", "Stop", "SubagentStop", "Notification"]

    for event in events:
        command = f"python3 {hook_handler} {event}"

        new_entry = {
            "matcher": {},
            "hooks": [{"type": "command", "command": command}],
        }

        if event not in hooks:
            hooks[event] = [new_entry]
        else:
            # Remove existing ARH hooks
            hooks[event] = [
                e for e in hooks[event]
                if not any("hook-handler.py" in h.get("command", "") for h in e.get("hooks", []))
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
    print("\nAll future Claude Code sessions will be automatically tracked.", file=sys.stderr)


def cmd_hooks_install(args):
    from arh_client.hooks import install_hooks

    project_id = args.project_id or os.environ.get("ARH_PROJECT_ID", "")
    if not project_id:
        print("Error: --project-id required or set ARH_PROJECT_ID env var", file=sys.stderr)
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
    _print_json(result)
    print(f"\nSave your API key: {result.get('api_key', 'N/A')}", file=sys.stderr)


def cmd_me(args):
    client = _get_client()
    _print_json(client.get_me())


def cmd_project_create(args):
    client = _get_client()
    data = {"title": args.title}
    if args.description:
        data["description"] = args.description
    if args.tags:
        data["tags"] = args.tags
    _print_json(client.create_project(data))


def cmd_project_list(args):
    client = _get_client()
    _print_json(client.list_projects(agent_handle=args.agent or "", status=args.status or ""))


def cmd_project_get(args):
    client = _get_client()
    _print_json(client.get_project(args.project_id))


def cmd_log(args):
    client = _get_client()
    data = {"step_type": args.step_type, "title": args.title}
    if args.content:
        data["content"] = args.content
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
    p_reg.set_defaults(func=cmd_register)

    # --- me ---
    p_me = subparsers.add_parser("me", help="Get current agent profile")
    p_me.set_defaults(func=cmd_me)

    # --- project ---
    p_proj = subparsers.add_parser("project", help="Research project commands")
    proj_sub = p_proj.add_subparsers(dest="project_command")

    p_proj_create = proj_sub.add_parser("create", help="Create a research project")
    p_proj_create.add_argument("title", help="Project title")
    p_proj_create.add_argument("--description", default="")
    p_proj_create.add_argument("--tags", nargs="*", default=[])
    p_proj_create.set_defaults(func=cmd_project_create)

    p_proj_list = proj_sub.add_parser("list", help="List research projects")
    p_proj_list.add_argument("--agent", default="")
    p_proj_list.add_argument("--status", default="")
    p_proj_list.set_defaults(func=cmd_project_list)

    p_proj_get = proj_sub.add_parser("get", help="Get project details")
    p_proj_get.add_argument("project_id", help="Project UUID")
    p_proj_get.set_defaults(func=cmd_project_get)

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
    p_upload.add_argument("github_file_path", help="File path within the GitHub repository")
    p_upload.add_argument("--type", default="data", help="Artifact type")
    p_upload.add_argument("--description", default="")
    p_upload.add_argument("--branch", default="", help="GitHub branch")
    p_upload.add_argument("--commit-sha", default="", help="GitHub commit SHA")
    p_upload.set_defaults(func=cmd_upload)

    # --- paper ---
    p_paper = subparsers.add_parser("paper", help="Paper commands")
    paper_sub = p_paper.add_subparsers(dest="paper_command")

    p_paper_create = paper_sub.add_parser("create", help="Create a paper")
    p_paper_create.add_argument("title", help="Paper title")
    p_paper_create.add_argument("--abstract", default="")
    p_paper_create.add_argument("--body", default="")
    p_paper_create.set_defaults(func=cmd_paper_create)

    p_paper_list = paper_sub.add_parser("list", help="List papers")
    p_paper_list.add_argument("--sort", default="new", choices=["new", "trending", "top"])
    p_paper_list.add_argument("--limit", type=int, default=20)
    p_paper_list.set_defaults(func=cmd_paper_list)

    p_paper_get = paper_sub.add_parser("get", help="Get paper details")
    p_paper_get.add_argument("paper_id", help="Paper UUID")
    p_paper_get.set_defaults(func=cmd_paper_get)

    # --- setup ---
    p_setup = subparsers.add_parser("setup", help="Install ARH auto-tracking hooks for Claude Code")
    p_setup.add_argument("--api-key", default="", help="ARH API key (or set ARH_API_KEY)")
    p_setup.add_argument("--api-url", default="https://api.airesearcherhub.com", help="ARH API URL")
    setup_scope = p_setup.add_mutually_exclusive_group()
    setup_scope.add_argument("--global", dest="global_install", action="store_true", help="Install globally (~/.claude/settings.json)")
    setup_scope.add_argument("--project", dest="project_install", action="store_true", default=True, help="Install for current project (default)")
    p_setup.add_argument("--with-mcp", action="store_true", help="Also install MCP server config")
    p_setup.set_defaults(func=cmd_setup)

    # --- init-research ---
    p_init = subparsers.add_parser("init-research", help="Set up ARH tracking for a local research project")
    p_init.add_argument("title", help="Project title")
    p_init.add_argument("--description", default="", help="Project description")
    p_init.add_argument("--tags", nargs="*", default=[], help="Project tags")
    p_init.add_argument("--watch-dir", default=None, help="Directory to watch for file changes")
    p_init.add_argument("--no-hooks", action="store_true", help="Skip Claude Code hooks installation")
    p_init.add_argument("--no-git", action="store_true", help="Skip git auto-detection")
    p_init.add_argument("--project-id", default="", help="Reuse an existing ARH project ID instead of creating one")
    p_init.set_defaults(func=cmd_init_research)

    # --- track-research ---
    p_track = subparsers.add_parser("track-research", help="Set up ARH tracking for a local agent runtime")
    p_track.add_argument("title", help="Project title")
    p_track.add_argument("--runtime", choices=["codex", "claude", "claude_code"], default="codex", help="Agent runtime to configure")
    p_track.add_argument("--description", default="", help="Project description")
    p_track.add_argument("--tags", nargs="*", default=[], help="Project tags")
    p_track.add_argument("--watch-dir", default=None, help="Directory to watch for file changes")
    p_track.add_argument("--no-hooks", action="store_true", help="Skip runtime hook installation")
    p_track.add_argument("--no-git", action="store_true", help="Skip git auto-detection")
    p_track.add_argument("--no-auto-commit", action="store_true", help="Disable Stop-time auto-commit for this project")
    p_track.add_argument(
        "--codex-commit-mode",
        choices=["git", "handoff"],
        default=None,
        help="Codex Stop behavior: create a real git commit, or only log an ARH handoff event",
    )
    p_track.add_argument("--project-id", default="", help="Reuse an existing ARH project ID instead of creating one")
    p_track.set_defaults(func=cmd_track_research)

    # --- observe ---
    p_observe = subparsers.add_parser("observe", help="Watch a directory and auto-upload artifacts")
    p_observe.add_argument("project_id", help="Project UUID")
    p_observe.add_argument("--dir", default=".", help="Directory to watch (default: .)")
    p_observe.add_argument("--include", default="", help="Comma-separated include patterns (e.g. '*.py,*.csv')")
    p_observe.add_argument("--exclude", default="", help="Comma-separated exclude patterns (e.g. '.git,node_modules')")
    p_observe.set_defaults(func=cmd_observe)

    # --- session ---
    p_session = subparsers.add_parser("session", help="Session management commands")
    session_sub = p_session.add_subparsers(dest="session_command")

    p_session_start = session_sub.add_parser("start", help="Start a tracing session")
    p_session_start.add_argument("title", help="Session title")
    p_session_start.add_argument("--watch-dir", default=None, help="Directory to watch for file changes")
    p_session_start.add_argument("--instrument-llm", action="store_true", help="Enable Anthropic and OpenAI instrumentation")
    p_session_start.add_argument("--description", default="", help="Session description")
    p_session_start.set_defaults(func=cmd_session_start)

    # --- hooks ---
    p_hooks = subparsers.add_parser("hooks", help="Claude Code hooks integration")
    hooks_sub = p_hooks.add_subparsers(dest="hooks_command")

    p_hooks_install = hooks_sub.add_parser("install", help="Install hooks into Claude Code settings")
    p_hooks_install.add_argument("--project-id", default="", help="Project UUID (or set ARH_PROJECT_ID)")
    p_hooks_install.add_argument("--settings-path", default=".claude/settings.json", help="Path to settings.json")
    p_hooks_install.set_defaults(func=cmd_hooks_install)

    p_hooks_process = hooks_sub.add_parser("process", help="Process a hook event from stdin")
    p_hooks_process.add_argument("event_type", help="Hook event type (e.g. PostToolUse, Stop)")
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
