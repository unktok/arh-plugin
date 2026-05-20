import json
import os
import queue
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from arh_client.git_tracker import (
    _build_hook_script,
    _build_post_commit_hook_script,
    install_push_tracking_hook,
)


def _git(cwd, *args):
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result


def test_push_hook_does_not_embed_api_key():
    script = _build_hook_script(
        "00000000-0000-0000-0000-000000000001",
        "https://api.example.test",
        "arh_sk_should_not_be_written",
    )

    assert "arh_sk_should_not_be_written" not in script
    assert "Authorization: Bearer {api_key}" not in script
    assert "~/.arh/credentials" in script or ".arh\" / \"credentials" in script
    assert "pre-push" in script
    assert '"$remote_url"' not in script
    assert 'git("remote", "get-url", remote_name' in script
    assert 'remote_has_sha(remote_name, remote_ref, local_sha)' in script


def test_post_commit_hook_does_not_embed_api_key():
    script = _build_post_commit_hook_script(
        "00000000-0000-0000-0000-000000000001",
        "https://api.example.test",
        "arh_sk_should_not_be_written",
    )

    assert "arh_sk_should_not_be_written" not in script
    assert "post-commit" in script
    assert "/commits" in script
    assert "~/.arh/credentials" in script or ".arh\" / \"credentials" in script


def test_install_push_hook_replaces_legacy_post_commit_with_local_hook(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    (tmp_path / "README.md").write_text("base\n")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-m", "initial")
    hooks = tmp_path / ".git" / "hooks"
    legacy = hooks / "post-commit"
    legacy.write_text(
        "#!/bin/sh\n"
        "# >>> ARH post-commit hook >>>\n"
        "echo legacy\n"
        "# <<< ARH post-commit hook <<<\n"
    )

    hook = install_push_tracking_hook(
        "00000000-0000-0000-0000-000000000001",
        "https://api.example.test",
        "arh_sk_should_not_be_written",
        repo_dir=str(tmp_path),
    )

    assert hook == str(hooks / "pre-push")
    assert "ARH pre-push hook" in (hooks / "pre-push").read_text()
    post_commit = legacy.read_text()
    assert "ARH post-commit hook" in post_commit
    assert "echo legacy" not in post_commit
    assert "/commits" in post_commit


def test_post_commit_hook_records_regular_git_commit(tmp_path, monkeypatch):
    payloads = queue.Queue()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            payloads.put((self.path, json.loads(self.rfile.read(length))))
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    credentials = tmp_path / "home" / ".arh" / "credentials"
    credentials.parent.mkdir(parents=True)
    credentials.write_text(
        json.dumps(
            {
                "api_key": "arh_sk_test_key",
                "api_url": f"http://127.0.0.1:{server.server_port}",
            }
        )
    )

    try:
        _git(tmp_path, "init")
        _git(tmp_path, "config", "user.email", "test@example.com")
        _git(tmp_path, "config", "user.name", "Test User")
        install_push_tracking_hook(
            "00000000-0000-0000-0000-000000000001",
            f"http://127.0.0.1:{server.server_port}",
            "arh_sk_should_not_be_written",
            repo_dir=str(tmp_path),
        )
        (tmp_path / "README.md").write_text("local\n")
        _git(tmp_path, "add", "README.md")
        _git(tmp_path, "commit", "-m", "local commit")

        path, payload = payloads.get(timeout=5)
    finally:
        server.shutdown()
        server.server_close()

    assert path == "/v1/research/projects/00000000-0000-0000-0000-000000000001/commits"
    assert payload["message"] == "local commit"
    assert payload["sha"] == _git(tmp_path, "rev-parse", "HEAD").stdout.strip()


def test_pre_push_hook_links_repo_and_records_pushed_commits(tmp_path, monkeypatch):
    payloads = queue.Queue()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            payloads.put((self.path, json.loads(self.rfile.read(length))))
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    credentials = tmp_path / "home" / ".arh" / "credentials"
    credentials.parent.mkdir(parents=True)
    credentials.write_text(
        json.dumps(
            {
                "api_key": "arh_sk_test_key",
                "api_url": f"http://127.0.0.1:{server.server_port}",
            }
        )
    )

    try:
        _git(tmp_path, "init")
        _git(tmp_path, "config", "user.email", "test@example.com")
        _git(tmp_path, "config", "user.name", "Test User")
        (tmp_path / "README.md").write_text("base\n")
        _git(tmp_path, "add", "README.md")
        _git(tmp_path, "commit", "-m", "pushed commit")
        sha = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        branch = _git(tmp_path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

        hook = install_push_tracking_hook(
            "00000000-0000-0000-0000-000000000001",
            f"http://127.0.0.1:{server.server_port}",
            "arh_sk_should_not_be_written",
            repo_dir=str(tmp_path),
        )
        assert hook

        real_git = shutil.which("git")
        assert real_git
        fakebin = tmp_path / "fakebin"
        fakebin.mkdir()
        fake_git = fakebin / "git"
        fake_git.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"remote\" ] && [ \"$2\" = \"get-url\" ]; then\n"
            "  echo \"https://github.com/test-owner/test-repo.git\"\n"
            "  exit 0\n"
            "fi\n"
            "if [ \"$1\" = \"ls-remote\" ]; then\n"
            "  printf '%s\\t%s\\n' \"$HOOK_SHA\" \"$3\"\n"
            "  exit 0\n"
            "fi\n"
            "exec \"$REAL_GIT\" \"$@\"\n"
        )
        fake_git.chmod(0o755)

        env = os.environ.copy()
        env["PATH"] = f"{fakebin}{os.pathsep}{env['PATH']}"
        env["REAL_GIT"] = real_git
        env["HOOK_SHA"] = sha
        remote_ref = f"refs/heads/{branch}"
        result = subprocess.run(
            [hook, "origin"],
            input=f"{remote_ref} {sha} {remote_ref} {'0' * 40}\n",
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env=env,
            timeout=5,
        )
        assert result.returncode == 0, result.stderr

        seen = []
        deadline = time.time() + 10
        while len(seen) < 2 and time.time() < deadline:
            try:
                seen.append(payloads.get(timeout=0.5))
            except queue.Empty:
                pass
    finally:
        server.shutdown()
        server.server_close()

    assert seen[0] == (
        "/v1/research/projects/00000000-0000-0000-0000-000000000001/link-repo",
        {"remote_url": "https://github.com/test-owner/test-repo.git", "branch": branch},
    )
    assert seen[1][0] == "/v1/research/projects/00000000-0000-0000-0000-000000000001/commits/batch"
    assert seen[1][1]["commits"][0]["sha"] == sha
    assert seen[1][1]["commits"][0]["message"] == "pushed commit"


def test_install_push_hook_refuses_parent_repo(tmp_path):
    _git(tmp_path, "init")
    child = tmp_path / "child"
    child.mkdir()

    hook = install_push_tracking_hook(
        "00000000-0000-0000-0000-000000000001",
        "https://api.example.test",
        "arh_sk_should_not_be_written",
        repo_dir=str(child),
    )

    assert hook is None
