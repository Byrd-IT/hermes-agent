"""Fenced server cleanup belongs to the loop that accepted it."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from tools import mcp_tool_lifecycle as lifecycle
from tools import mcp_tool_loop as loop_mod
from tools import mcp_tool_session_env as session_env

pytest_plugins = ["tests.tools.test_mcp_session_env_close_failures"]


def test_old_loop_cannot_steal_new_loop_fenced_shutdown(monkeypatch, isolated_ledgers):
    old_loop = asyncio.new_event_loop()
    new_loop = asyncio.new_event_loop()
    ready = threading.Event()
    new_loop.call_soon(ready.set)
    thread = threading.Thread(target=new_loop.run_forever, daemon=True)
    thread.start()
    assert ready.wait(5)
    monkeypatch.setattr(session_env, "_FENCED_CLOSES", [])
    monkeypatch.setattr(session_env, "_FENCED_CLEANUPS", set())
    monkeypatch.setattr(loop_mod, "_running_loop", lambda: new_loop)
    real_soon = new_loop.call_soon_threadsafe
    held = []
    observed = []

    async def shutdown():
        observed.append(asyncio.get_running_loop())

    def hold(callback, *args, context=None):
        held.append((callback, args, context))

    monkeypatch.setattr(new_loop, "call_soon_threadsafe", hold)
    try:
        finished = session_env._schedule_fenced_close(SimpleNamespace(shutdown=shutdown))
        assert held
        lifecycle._finish_fenced_before_close(old_loop)
        assert not finished.is_set(), "old loop consumed another loop's cleanup"
        assert not observed
        callback, args, context = held.pop()
        real_soon(callback, *args, context=context)
        assert finished.wait(5)
        assert observed == [new_loop]
    finally:
        monkeypatch.setattr(new_loop, "call_soon_threadsafe", real_soon)
        for callback, args, context in held:
            real_soon(callback, *args, context=context)
        real_soon(new_loop.stop)
        thread.join(5)
        new_loop.close()
        old_loop.close()
