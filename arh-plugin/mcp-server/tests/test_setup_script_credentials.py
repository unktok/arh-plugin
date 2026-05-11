import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
SETUP_SCRIPT = REPO_ROOT / "arh-plugin" / "scripts" / "setup.py"


def _load_setup_script():
    spec = importlib.util.spec_from_file_location("arh_setup_script", SETUP_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_setup_script_persist_credentials_sets_private_modes(tmp_path, monkeypatch):
    setup = _load_setup_script()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    creds_path = Path(
        setup.persist_credentials("arh_sk_test", "https://api.example.test")
    )

    assert stat.S_IMODE((home / ".arh").stat().st_mode) == 0o700
    assert stat.S_IMODE(creds_path.stat().st_mode) == 0o600


def test_setup_script_explicit_key_ignores_ambient_api_url(tmp_path, monkeypatch):
    setup = _load_setup_script()
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ARH_API_URL", "https://env.example.test")

    assert setup.resolve_credentials() == ("https://env.example.test", "")
    setup.persist_credentials("arh_sk_new", setup.DEFAULT_API_URL)

    creds = json.loads((home / ".arh" / "credentials").read_text())
    assert creds["api_key"] == "arh_sk_new"
    assert creds["api_url"] == setup.DEFAULT_API_URL


@pytest.mark.skipif(
    not hasattr(os, "O_NOFOLLOW"),
    reason="symlink refusal relies on O_NOFOLLOW",
)
def test_setup_script_persist_credentials_refuses_symlink_file(tmp_path, monkeypatch):
    setup = _load_setup_script()
    home = tmp_path / "home"
    creds_dir = home / ".arh"
    creds_dir.mkdir(parents=True)
    target = tmp_path / "target"
    (creds_dir / "credentials").symlink_to(target)
    monkeypatch.setenv("HOME", str(home))

    with pytest.raises(OSError):
        setup.persist_credentials("arh_sk_test", "https://api.example.test")

    assert not target.exists()
