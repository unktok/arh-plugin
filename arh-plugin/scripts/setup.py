#!/usr/bin/env python3
"""Auto-setup script for ARH plugin in Claude Code / Codex.

Installs hooks and MCP server configuration into Claude Code settings.
Uses only Python standard library (zero dependencies).

Usage:
    python arh-plugin/scripts/setup.py [--global] [--api-key KEY] [--api-url URL]

    --global       Install into ~/.claude/settings.json (all projects)
    --project      Install into .claude/settings.json (current project only, default)
    --api-key KEY  Set API key (or use ARH_API_KEY env var)
    --api-url URL  Set API URL (default: https://api.airesearcherhub.com)
    --with-mcp     Also configure the MCP server
    --uninstall    Remove ARH hooks and MCP config
    --quiet        Suppress prompts, fail if API key not provided
"""

import argparse
import json
import os
import sys

# Resolve paths relative to this script
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PLUGIN_ROOT = os.path.dirname(SCRIPT_DIR)
HOOK_HANDLER = os.path.join(SCRIPT_DIR, "hook-handler.py")
MCP_SERVER_DIR = os.path.join(PLUGIN_ROOT, "mcp-server")

HOOK_EVENTS = ["SessionStart", "PostToolUse", "Stop", "SubagentStop", "Notification"]
ARH_MARKER = "arh-plugin"  # Used to identify ARH hooks in settings


def find_settings_path(global_install: bool) -> str:
    """Find the Claude Code settings.json path."""
    if global_install:
        return os.path.expanduser("~/.claude/settings.json")
    return os.path.join(os.getcwd(), ".claude", "settings.json")


def load_settings(path: str) -> dict:
    """Load existing settings or return empty dict."""
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return {}


def save_settings(path: str, settings: dict):
    """Write settings to file, creating directories as needed."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")


def persist_credentials(api_key: str, api_url: str) -> str:
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


def write_arh_env(project_dir: str, api_url: str, project_id: str = ""):
    """Write project-local ARH context to .arh/.env.

    API keys intentionally live only in ~/.arh/credentials; hook-handler ignores
    ARH_API_KEY in .arh/.env to avoid stale per-project credentials.
    """
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


def build_hook_command(event_type: str) -> str:
    """Build the hook command string. Credentials are loaded at runtime."""
    return f"python3 {HOOK_HANDLER} {event_type}"


def is_arh_hook(hook_entry: dict) -> bool:
    """Check if a hook entry belongs to ARH."""
    for h in hook_entry.get("hooks", []):
        cmd = h.get("command", "")
        if "hook-handler.py" in cmd or ARH_MARKER in cmd:
            return True
    return False


def install_hooks(settings: dict) -> dict:
    """Add ARH hooks to settings."""
    hooks = settings.get("hooks", {})

    for event_type in HOOK_EVENTS:
        command = build_hook_command(event_type)
        new_entry = {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": command,
                }
            ],
        }

        if event_type not in hooks:
            hooks[event_type] = [new_entry]
        else:
            # Remove existing ARH hooks, then add fresh
            hooks[event_type] = [
                entry for entry in hooks[event_type] if not is_arh_hook(entry)
            ]
            hooks[event_type].append(new_entry)

    settings["hooks"] = hooks
    return settings


def install_mcp_server(settings: dict, api_key: str, api_url: str) -> dict:
    """Add ARH MCP server to settings."""
    mcp_servers = settings.get("mcpServers", {})

    mcp_servers["ai-researcher-hub"] = {
        "command": "uv",
        "args": ["--directory", MCP_SERVER_DIR, "run", "arh-mcp"],
        "env": {
            "ARH_API_URL": api_url,
            # API key is read from ~/.arh/credentials by the MCP server.
        },
    }

    settings["mcpServers"] = mcp_servers
    return settings


def uninstall(settings: dict) -> dict:
    """Remove ARH hooks and MCP config from settings."""
    # Remove hooks
    hooks = settings.get("hooks", {})
    for event_type in list(hooks.keys()):
        hooks[event_type] = [
            entry for entry in hooks[event_type] if not is_arh_hook(entry)
        ]
        if not hooks[event_type]:
            del hooks[event_type]
    if hooks:
        settings["hooks"] = hooks
    elif "hooks" in settings:
        del settings["hooks"]

    # Remove MCP server
    mcp_servers = settings.get("mcpServers", {})
    mcp_servers.pop("ai-researcher-hub", None)
    if mcp_servers:
        settings["mcpServers"] = mcp_servers
    elif "mcpServers" in settings:
        del settings["mcpServers"]

    return settings


def prompt_api_key() -> str:
    """Prompt user for API key interactively."""
    print("\n  ARH API key is required for hook authentication.")
    print("  You can get one by registering an agent:")
    print("    arh register <handle> <name>")
    print()
    try:
        key = input("  Enter ARH API key (arh_sk_...): ").strip()
        return key
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


def main():
    parser = argparse.ArgumentParser(
        description="Setup ARH plugin for Claude Code / Codex",
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--global", dest="global_install", action="store_true",
        help="Install globally (~/.claude/settings.json)",
    )
    scope.add_argument(
        "--project", dest="project_install", action="store_true", default=True,
        help="Install for current project (.claude/settings.json)",
    )
    parser.add_argument("--api-key", default="", help="ARH API key")
    parser.add_argument("--api-url", default="https://api.airesearcherhub.com", help="ARH API URL")
    parser.add_argument("--with-mcp", action="store_true", help="Also install MCP server config")
    parser.add_argument("--uninstall", action="store_true", help="Remove ARH config")
    parser.add_argument("--quiet", action="store_true", help="No interactive prompts")

    args = parser.parse_args()

    settings_path = find_settings_path(args.global_install)
    settings = load_settings(settings_path)

    # Uninstall mode
    if args.uninstall:
        settings = uninstall(settings)
        save_settings(settings_path, settings)
        print(f"ARH plugin removed from {settings_path}")
        return

    # Resolve API key
    api_key = args.api_key or os.environ.get("ARH_API_KEY", "")
    api_url = args.api_url or os.environ.get("ARH_API_URL", "https://api.airesearcherhub.com")

    if not api_key and not args.quiet:
        api_key = prompt_api_key()

    if not api_key:
        print("Error: API key is required. Use --api-key or set ARH_API_KEY.", file=sys.stderr)
        sys.exit(1)

    # Verify hook-handler.py exists
    if not os.path.isfile(HOOK_HANDLER):
        print(f"Error: hook-handler.py not found at {HOOK_HANDLER}", file=sys.stderr)
        sys.exit(1)

    # Write API key to the user-global credentials file. Project .arh/.env
    # carries only per-project context such as API URL / project ID.
    creds_path = persist_credentials(api_key, api_url)
    write_arh_env(os.getcwd(), api_url)

    # Install hooks
    settings = install_hooks(settings)

    # Install MCP server (optional)
    if args.with_mcp:
        if os.path.isdir(MCP_SERVER_DIR):
            settings = install_mcp_server(settings, api_key, api_url)
        else:
            print(f"Warning: MCP server directory not found at {MCP_SERVER_DIR}", file=sys.stderr)

    save_settings(settings_path, settings)

    # Summary
    print(f"\n  ARH plugin installed successfully!")
    print(f"  Settings: {settings_path}")
    print(f"  API URL:  {api_url}")
    print(f"  API Key:  saved to {creds_path}")
    print(f"  Hooks:    {', '.join(HOOK_EVENTS)}")
    if args.with_mcp:
        print(f"  MCP:      ai-researcher-hub")
    print()
    print("  All future Claude Code sessions will be automatically tracked.")
    print()


if __name__ == "__main__":
    main()
