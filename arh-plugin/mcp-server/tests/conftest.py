"""Shared pytest fixtures for arh-mcp tests.

The fixtures here let MCP tools be invoked directly from test code without
needing a real ARH backend or a real FastMCP server. Two pieces:

1. ``fake_arh_client`` — replaces ``arh_mcp.client.arh_client`` with a stub
   that has the same ``api_key`` / ``base_url`` attributes that tools read,
   pointing at an unreachable test URL.

2. ``mcp_register`` — yields a helper that takes a tools-module ``register``
   function (e.g. ``arh_mcp.tools.tracing.register``) and returns a dict of
   ``{tool_name: callable}`` so tests can call the underlying coroutine
   directly. This mirrors the smoke pattern used in commit 0842dd9 where a
   minimal ``_MCP`` shim with a ``tool()`` decorator captures registrations.
"""

from __future__ import annotations

from typing import Callable

import pytest

import arh_mcp.client as arh_client_module


class _FakeARHClient:
    """Minimal stand-in for arh_mcp.client.ARHClient.

    Tools under test only read ``api_key`` and ``base_url``; nothing here
    issues real HTTP calls. ``base_url`` deliberately points at port 0 so
    any code path that does try to talk to a backend fails fast.
    """

    def __init__(
        self, api_key: str = "arh_sk_test", base_url: str = "http://localhost:0"
    ):
        self.api_key = api_key
        self.base_url = base_url


@pytest.fixture
def fake_arh_client(monkeypatch: pytest.MonkeyPatch) -> _FakeARHClient:
    """Replace the module-level arh_client with a fake for the duration of the test."""
    fake = _FakeARHClient()
    monkeypatch.setattr(arh_client_module, "arh_client", fake)
    # Tools import the symbol directly (`from arh_mcp.client import arh_client`),
    # so we also patch any module that has already imported it.
    import arh_mcp.tools.tracing as tracing_module

    monkeypatch.setattr(tracing_module, "arh_client", fake)
    return fake


class _MCP:
    """Tiny FastMCP stand-in that captures functions decorated with ``@mcp.tool()``.

    Mirrors the smoke harness used in commit 0842dd9: a real FastMCP isn't
    needed to invoke a tool's underlying coroutine — the decorator just
    needs to return the function unchanged.
    """

    def __init__(self) -> None:
        self.tools: dict[str, Callable] = {}

    def tool(self, *args, **kwargs):
        # Support both `@mcp.tool` and `@mcp.tool()`
        if args and callable(args[0]) and not kwargs:
            fn = args[0]
            self.tools[fn.__name__] = fn
            return fn

        def _decorator(fn: Callable) -> Callable:
            self.tools[fn.__name__] = fn
            return fn

        return _decorator


@pytest.fixture
def mcp_register():
    """Return a helper that registers a tools module and returns its tools dict.

    Usage::

        def test_something(mcp_register):
            from arh_mcp.tools import tracing
            tools = mcp_register(tracing.register)
            result = await tools["setup_auto_tracking"](project_dir="...")
    """

    def _register(register_fn: Callable[[_MCP], None]) -> dict[str, Callable]:
        mcp = _MCP()
        register_fn(mcp)
        return mcp.tools

    return _register
