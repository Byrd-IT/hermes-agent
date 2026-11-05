"""Calls and discarded connection results cannot outlive their variant."""
import asyncio
import concurrent.futures
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tools import mcp_tool_session_env as se
from tools import mcp_tool as _core


@pytest.mark.parametrize("handler_kind", ["events", "utility"])
@pytest.mark.parametrize("forced", [False, True])
def test_teardown_between_lookup_and_rpc(monkeypatch, handler_kind, forced):
    from agent.session_client_env import client_env_scope
    from tools import mcp_tool_discovery as discovery
    from tools import mcp_tool_events as events
    from tools import mcp_tool_handlers as handlers
    from tools import mcp_tool_loop as loop

    base = "queued-rpc"
    monkeypatch.setattr(_core, "_lazy_server_configs", {base: {"command": "unused", "session_env": ["PIN"]}})
    monkeypatch.setattr(_core, "_servers", {})
    monkeypatch.setattr(loop, "_run_on_mcp_loop", lambda fn, **kw: asyncio.run(fn()))
    rpc = AsyncMock(return_value={})
    server = SimpleNamespace(session=object(), _rpc_lock=asyncio.Lock(), shutdown=AsyncMock())
    with client_env_scope({"PIN": "a"}):
        key = se.variant_key(base)
        _core._servers[key] = server

        def dispatch(name, selected, op, call, *args, **kwargs):
            if forced:
                se.close_for_bases([base])
                # Retain session deliberately: ownership, not a transport side effect, fences the RPC.
            else:
                assert se._close([key], protect_in_use=True) == 0
            return asyncio.run(call())

        monkeypatch.setattr(handlers, "_dispatch", dispatch)
        factory = (events._make_events_handler("list", "list", rpc, needs_name=False)
                   if handler_kind == "events" else handlers._make_utility_handler("list", "list", rpc, lambda x, _: x))
        try:
            result = factory(base, 5)({})
            if forced:
                rpc.assert_not_awaited()
                assert "not connected" in result
            else:
                rpc.assert_awaited_once()
                server.shutdown.assert_not_awaited()
        finally:
            _core._servers.pop(key, None)
            se.reset()


@pytest.mark.parametrize("interrupted", [True, False])
def test_completed_connection_is_cleaned_when_waiter_abandons(monkeypatch, interrupted):
    from agent import async_utils
    from agent.session_client_env import client_env_scope
    from tools import interrupt
    from tools import mcp_tool_discovery as discovery
    from tools import mcp_tool_loop as loop

    base = "discarded-connect"
    monkeypatch.setattr(_core, "_lazy_server_configs", {base: {"command": "unused", "session_env": ["PIN"]}})
    monkeypatch.setattr(_core, "_servers", {})
    monkeypatch.setattr(se, "_ensure_reaper", lambda: None)
    monkeypatch.setattr(loop, "_ensure_mcp_loop", lambda: None)
    monkeypatch.setattr(loop, "_running_loop", lambda: object())
    monkeypatch.setattr(interrupt, "is_interrupted", lambda: interrupted)
    server = SimpleNamespace(session=object(), shutdown=AsyncMock())
    monkeypatch.setattr(discovery, "_connect_server", AsyncMock(return_value=server))

    def schedule(coro, *args, **kwargs):
        future = concurrent.futures.Future()
        future.set_result(asyncio.run(coro))
        return future

    monkeypatch.setattr(async_utils, "safe_schedule_threadsafe", schedule)
    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", schedule)
    if not interrupted:
        run = loop._run_on_mcp_loop
        monkeypatch.setattr(loop, "_run_on_mcp_loop", lambda fn, **kw: run(fn, **{**kw, "timeout": -1}))
    try:
        with client_env_scope({"PIN": "a"}):
            key = se.variant_key(base)
            if interrupted:
                with pytest.raises(InterruptedError):
                    se.ensure_connected(base, key)
            else:
                assert se.ensure_connected(base, key) is None
            server.shutdown.assert_awaited_once()
            assert key not in _core._servers
    finally:
        se.reset()
