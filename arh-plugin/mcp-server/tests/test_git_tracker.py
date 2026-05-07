from arh_client.git_tracker import _build_hook_script


def test_post_commit_hook_does_not_embed_api_key():
    script = _build_hook_script(
        "00000000-0000-0000-0000-000000000001",
        "https://api.example.test",
        "arh_sk_should_not_be_written",
    )

    assert "arh_sk_should_not_be_written" not in script
    assert "Authorization: Bearer {api_key}" not in script
    assert "~/.arh/credentials" in script or ".arh\" / \"credentials" in script
    assert "json.dumps(payload)" in script
