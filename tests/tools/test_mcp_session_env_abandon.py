"""An abandoned connect never orphans its server, and a fenced call is not a breaker strike."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tools import mcp_tool as _core
from tools import mcp_tool_session_env as se


@pytest.fixture
def mcp_loop(monkeypatch):
    from tools import mcp_tool_loop as loop

    real = asyncio.new_event_loop()
    ready = threading.Event()
    real.call_soon(ready.set)
    thread = threading.Thread(target=real.run_forever, daemon=True)
    thread.start()
    assert ready.wait(5)
    monkeypatch.setattr(loop, "_running_loop", lambda: real)
    yield real
    real.call_soon_threadsafe(real.stop)
    thread.join(5)
    real.close()


def test_cancel_swallowed_by_the_task_still_discards_the_result(mcp_loop):
    """A task that finishes despite cancel() (py3.11 wait_for) must not strand its server."""
    from tools import mcp_tool_loop as loop

    server = object()
    discarded = threading.Event()
    seen = []

    async def cleanup(result):
        seen.append(result)
        discarded.set()

    async def connect():
        try:
            await asyncio.Event().wait()  # hangs until the caller abandons it
        except asyncio.CancelledError:
            return server  # swallow the cancel and hand back a live connection

    with pytest.raises(TimeoutError):
        loop._run_on_mcp_loop(connect, timeout=0.2, on_discard=cleanup)
    assert discarded.wait(5)
    assert seen == [server]


def test_result_finished_before_destination_cancel_is_discarded_once(mcp_loop, monkeypatch):
    """cancel() reports success but the task already produced a result: discard it, once."""
    from agent import async_utils
    from tools import mcp_tool_loop as loop

    server = object()
    seen = []
    done = threading.Event()
    finished = threading.Event()

    async def cleanup(result):
        seen.append(result)
        done.set()

    async def connect():
        finished.set()
        return server

    real_schedule = async_utils.safe_schedule_threadsafe

    class LateCancel:
        """The concurrent future loses the result: cancel() wins after the task is done."""

        def __init__(self, inner):
            self._inner = inner

        def cancel(self):
            assert finished.wait(5)
            self._inner.result(timeout=5)  # task is done; the proxy still claims a win
            return True

        def cancelled(self):
            return True

        def done(self):
            return False

        def result(self, timeout=None):
            raise TimeoutError

        def exception(self):
            return None

    monkeypatch.setattr(async_utils, "safe_schedule_threadsafe",
                        lambda *a, **k: LateCancel(real_schedule(*a, **k)))
    with pytest.raises(TimeoutError):
        loop._run_on_mcp_loop(connect, timeout=0.2, on_discard=cleanup)
    assert done.wait(5)
    assert seen == [server]


def test_fenced_call_does_not_count_as_a_breaker_strike(monkeypatch):
    from tools import mcp_tool_handlers as handlers

    bumped = []
    monkeypatch.setattr(_core, "_bump_server_error", lambda *a, **k: bumped.append(a))
    monkeypatch.setattr(_core, "_reset_server_error", lambda *a, **k: bumped.append(("reset",)))
    assert handlers._record_call_outcome("srv", se.fenced_error()) == se.fenced_error()
    assert bumped == []
    handlers._record_call_outcome("srv", '{"error": "real failure"}')
    assert len(bumped) == 1


def test_fenced_connect_cleanup_failure_does_not_escape(monkeypatch):
    """A timed-out cleanup of a fenced connection must not break the caller."""
    from agent.session_client_env import client_env_scope
    from tools import mcp_tool_discovery as discovery
    from tools import mcp_tool_loop as loop

    base = "fenced-cleanup"
    monkeypatch.setattr(_core, "_lazy_server_configs", {base: {"command": "unused", "session_env": ["PIN"]}})
    monkeypatch.setattr(_core, "_servers", {})
    monkeypatch.setattr(se, "_ensure_reaper", lambda: None)
    monkeypatch.setattr(loop, "_ensure_mcp_loop", lambda: None)
    server = SimpleNamespace(session=object(), shutdown=AsyncMock())
    monkeypatch.setattr(discovery, "_connect_server", AsyncMock(return_value=server))
    calls = []

    def run(fn, timeout=30, on_discard=None):
        calls.append(on_discard)
        if len(calls) == 1:
            result = asyncio.run(fn())
            se.reset()  # teardown while the connect was in flight
            return result
        raise TimeoutError

    monkeypatch.setattr(loop, "_run_on_mcp_loop", run)
    try:
        with client_env_scope({"PIN": "a"}):
            key = se.variant_key(base)
            assert se.ensure_connected(base, key) is None
        assert calls[1] is se._shutdown_discarded
    finally:
        se.reset()


def test_discard_started_before_the_pending_cancel_still_finishes(mcp_loop, monkeypatch):
    """The task finishes after the abandon flag is set but before task.cancel() lands."""
    from agent import async_utils
    from tools import mcp_tool_loop as loop

    server = object()
    proceed = asyncio.Event()
    gate = asyncio.Event()
    started = threading.Event()
    completed = threading.Event()

    async def cleanup(result):
        started.set()
        await gate.wait()  # still shutting down when the cancel arrives
        completed.set()

    async def connect():
        await proceed.wait()
        return server

    real_schedule = async_utils.safe_schedule_threadsafe

    class SlowCancel:
        def __init__(self, inner):
            self._inner = inner

        def cancel(self):
            mcp_loop.call_soon_threadsafe(proceed.set)
            assert started.wait(5)  # discard is running inside the wrapped task
            self._inner.cancel()  # the real task.cancel() now arrives
            mcp_loop.call_soon_threadsafe(gate.set)
            return True

        def result(self, timeout=None):
            raise TimeoutError

        def done(self):
            return False

    monkeypatch.setattr(async_utils, "safe_schedule_threadsafe",
                        lambda *a, **k: SlowCancel(real_schedule(*a, **k)))
    with pytest.raises(TimeoutError):
        loop._run_on_mcp_loop(connect, timeout=0.2, on_discard=cleanup)
    assert completed.wait(5)


def test_bound_variant_key_reaches_the_real_loop_task(mcp_loop):
    """The fence relies on the caller's context reaching the task run on the MCP loop."""
    from tools import mcp_tool_loop as loop

    token = se._bound_keys.set({"base": ("scope#env:x", "base")})
    try:
        seen = loop._run_on_mcp_loop(lambda: _read_bound(), timeout=5)
    finally:
        se._bound_keys.reset(token)
    assert seen == {"base": ("scope#env:x", "base")}


async def _read_bound():
    return se._bound_keys.get()
