"""Unrecovered MCP calls log the exception type, never its message."""
import asyncio
import json
import logging
from types import SimpleNamespace

import pytest


pytest_plugins = ["tests.tools.test_mcp_session_env_close_failures"]

_PAYLOADS = [
    "Authorization: Bearer synthe...C123",
    "https://user:pass@host.invalid/path",
    "synthetic-token-recognizable-ABC123",
    "X-Api-Key: synthe...23",
    "https://host.invalid/path?api_key=synthetic-final-ABC123",
]


def _run_on_loop(server):
    def _run(coro_or_factory, timeout=30, **_kwargs):
        coro = coro_or_factory() if callable(coro_or_factory) else coro_or_factory

        async def _go():
            if getattr(server, "_rpc_lock", None) is None:
                server._rpc_lock = asyncio.Lock()
            return await coro

        return asyncio.run(_go())

    return _run


@pytest.mark.parametrize("payload", _PAYLOADS)
@pytest.mark.parametrize("kind", ["tool", "utility", "events"])
def test_final_failure_log_omits_exception_payload(
    monkeypatch, caplog, payload, kind, isolated_ledgers,
):
    from tools import mcp_tool_events as events
    from tools import mcp_tool_handlers as handlers
    from tools.mcp_tool_scope import _resolve_server_key

    name = "diag-final"
    key = _resolve_server_key(name)
    server = SimpleNamespace(session=SimpleNamespace(), _rpc_lock=None)

    async def boom(*_args, **_kwargs):
        raise RuntimeError(payload)

    server.session.call_tool = boom
    isolated_ledgers.servers[key] = server
    monkeypatch.setattr(handlers._loop, "_run_on_mcp_loop", _run_on_loop(server))

    async def rpc(_session, _args, _server_name):
        raise RuntimeError(payload)

    with caplog.at_level(logging.ERROR, logger="tools.mcp_tool"):
        if kind == "tool":
            result = handlers._make_tool_handler(name, "probe", 5.0)({})
            needle = "call failed"
        elif kind == "utility":
            factory = handlers._make_utility_handler(
                "resources/list", "list_resources", rpc, lambda *_a: {},
            )
            result = factory(name, 5.0)({})
            needle = "list_resources failed"
        else:
            factory = events._make_events_handler(
                "events/list", "list_event_types", rpc, needs_name=False,
            )
            result = factory(name, 5.0)({})
            needle = "list_event_types failed"
    parsed = json.loads(result)
    assert "error" in parsed, parsed
    failed = [record.getMessage() for record in caplog.records]
    assert any(needle in message for message in failed), caplog.text
    assert "RuntimeError" in caplog.text
    assert payload not in caplog.text
    assert "ABC123" not in caplog.text
    assert "user:pass" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
