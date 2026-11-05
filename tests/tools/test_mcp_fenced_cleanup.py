"""A fenced variant close must finish after the caller's wait times out.

``ensure_connected`` waits for that close on the MCP loop with a timeout. The
wait's cancel must not cut the close short: the server was already built, and
dropping it here orphans the process.
"""
import asyncio
import threading
from types import SimpleNamespace

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


def test_fenced_cleanup_finishes_after_caller_timeout(mcp_loop, monkeypatch):
    """The caller returns on timeout; the close, already in progress, still finishes."""
    from agent import async_utils
    from agent.session_client_env import client_env_scope
    from tools import mcp_tool_discovery as discovery
    from tools import mcp_tool_loop as loop

    base = "fenced-noncancel"
    monkeypatch.setattr(_core, "_lazy_server_configs",
                        {base: {"command": "unused", "session_env": ["PIN"]}})
    monkeypatch.setattr(_core, "_servers", {})
    monkeypatch.setattr(se, "_ensure_reaper", lambda: None)
    monkeypatch.setattr(loop, "_ensure_mcp_loop", lambda: None)
    monkeypatch.setattr(se, "_render", lambda config, values: config)

    started = threading.Event()
    finished = threading.Event()
    release = asyncio.Event()
    caller_done = threading.Event()
    result = {}

    async def close():
        started.set()
        await release.wait()
        finished.set()

    server = SimpleNamespace(session=object(), shutdown=close)

    async def connect(_name, _config):
        # Teardown wins while the connect is in flight: the result must be fenced.
        with se._guard:
            se._pending.pop(key_box[0], None)
        return server

    monkeypatch.setattr(discovery, "_connect_server", connect)

    real_schedule = async_utils.safe_schedule_threadsafe
    scheduled = {"n": 0}

    class _CancelAfterStart:
        """Deliver the real cancel only once close has reached its await."""

        def __init__(self, inner):
            self._inner = inner

        def cancel(self):
            assert started.wait(5)
            return self._inner.cancel()

        def result(self, timeout=None):
            raise TimeoutError

        def done(self):
            return False

    def schedule(coro, target, **kwargs):
        scheduled["n"] += 1
        fut = real_schedule(coro, target, **kwargs)
        # First schedule is the connect. The fenced close is the one that times out.
        if scheduled["n"] == 1:
            return fut
        return _CancelAfterStart(fut)

    monkeypatch.setattr(se, "_FENCED_CLEANUP_TIMEOUT", 0.05)
    monkeypatch.setattr(async_utils, "safe_schedule_threadsafe", schedule)

    key_box: list = [None]

    def caller():
        try:
            with client_env_scope({"PIN": "a"}):
                key_box[0] = se.variant_key(base)
                result["value"] = se.ensure_connected(base, key_box[0])
        except Exception as exc:  # health: allow BLE001 -- the test reports the caller failure
            result["exc"] = exc
        finally:
            caller_done.set()

    thread = threading.Thread(target=caller)
    thread.start()
    try:
        assert started.wait(5), "fenced cleanup never started the close"
        assert caller_done.wait(5), "caller did not return after the cleanup timeout"
        assert "exc" not in result, result.get("exc")
        assert result["value"] is None
        assert not finished.is_set()
        mcp_loop.call_soon_threadsafe(release.set)
        assert finished.wait(5), "close did not finish after the caller timed out"
        assert key_box[0] not in _core._servers
    finally:
        caller_done.set()
        mcp_loop.call_soon_threadsafe(release.set)
        thread.join(5)
        se.reset()
        _core._lazy_server_configs.pop(base, None)
        if key_box[0] is not None:
            _core._servers.pop(key_box[0], None)
