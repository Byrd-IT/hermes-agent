"""A timed-out fenced close must still shut the server down.

``ensure_connected`` waits for that close on the MCP loop. The wait's cancel
must not leave the server half-closed: once started the shielded task continues
independently. A cancel before that coroutine is stepped, and loop stop or drain
dropping the cleanup task, are deferred.
"""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from tools import mcp_tool as _core
from tools import mcp_tool_session_env as se

pytest_plugins = ["tests.tools.test_mcp_session_env_close_failures"]


@pytest.fixture(autouse=True)
def _isolate_variant_ledgers(isolated_ledgers):
    yield


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


def _unpublished_connect(monkeypatch, base):
    from tools import mcp_tool_discovery as discovery
    from tools import mcp_tool_loop as loop

    monkeypatch.setattr(
        _core, "_lazy_server_configs", {base: {"command": "unused", "session_env": ["PIN"]}}
    )
    monkeypatch.setattr(_core, "_servers", {})
    monkeypatch.setattr(se, "_ensure_reaper", lambda: None)
    monkeypatch.setattr(loop, "_ensure_mcp_loop", lambda: None)
    monkeypatch.setattr(se, "_render", lambda config, values: config)

    def connect(_name, _config):
        async def _connect():
            with se._guard:
                se._pending.pop(key_box[0], None)
            return server_box[0]

        return _connect()

    monkeypatch.setattr(discovery, "_connect_server", connect)
    key_box: list = [None]
    server_box: list = [None]
    return key_box, server_box


def test_fenced_cleanup_finishes_after_waiter_cancels_in_progress_close(mcp_loop, monkeypatch):
    """The caller returns when the wait is abandoned; shutdown still finishes.

    The close has reached its await, so this is not the deferred pre-start
    cancel. The waiter's ``cancel()`` must not be the last word on the server.
    """
    from agent import async_utils
    from agent.session_client_env import client_env_scope
    from tools import mcp_tool_loop as loop

    base = "fenced-on-discard"
    key_box, server_box = _unpublished_connect(monkeypatch, base)
    monkeypatch.setattr(se, "_FENCED_CLEANUP_TIMEOUT", 0, raising=False)

    started = threading.Event()
    release = asyncio.Event()
    finished = threading.Event()
    calls = {"n": 0}
    caller_done = threading.Event()
    result = {}

    async def close():
        calls["n"] += 1
        started.set()
        await release.wait()
        finished.set()

    server_box[0] = SimpleNamespace(session=object(), shutdown=close)
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
        # First schedule is the connect. The fenced close is the one abandoned.
        if scheduled["n"] == 1:
            return fut
        return _CancelAfterStart(fut)

    monkeypatch.setattr(async_utils, "safe_schedule_threadsafe", schedule)

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
        assert caller_done.wait(5), "caller did not return after the cleanup wait was abandoned"
        assert "exc" not in result, result.get("exc")
        assert result["value"] is None
        assert scheduled["n"] >= 2, "fenced cleanup never scheduled a close on the loop"
        assert not finished.is_set()
        mcp_loop.call_soon_threadsafe(release.set)
        assert finished.wait(5), "close did not finish after the waiter cancelled it"
        assert calls["n"] == 1, "shutdown must not be restarted after cancellation"
        assert key_box[0] not in _core._servers
    finally:
        caller_done.set()
        mcp_loop.call_soon_threadsafe(release.set)
        thread.join(5)
        se.reset()
        _core._lazy_server_configs.pop(base, None)
        if key_box[0] is not None:
            _core._servers.pop(key_box[0], None)


def test_fenced_cleanup_shuts_down_when_the_wait_is_not_abandoned(mcp_loop, monkeypatch):
    """A close that finishes inside the wait shuts the server down once."""
    from agent.session_client_env import client_env_scope

    base = "fenced-complete"
    key_box, server_box = _unpublished_connect(monkeypatch, base)
    finished = threading.Event()
    calls = {"n": 0}

    async def close():
        calls["n"] += 1
        finished.set()

    server_box[0] = SimpleNamespace(session=object(), shutdown=close)
    result = {}

    def caller():
        try:
            with client_env_scope({"PIN": "a"}):
                key_box[0] = se.variant_key(base)
                result["value"] = se.ensure_connected(base, key_box[0])
        except Exception as exc:  # health: allow BLE001 -- the test reports the caller failure
            result["exc"] = exc

    thread = threading.Thread(target=caller)
    thread.start()
    try:
        thread.join(5)
        assert not thread.is_alive()
        assert "exc" not in result, result.get("exc")
        assert result["value"] is None
        assert finished.is_set()
        assert calls["n"] == 1
        assert key_box[0] not in _core._servers
    finally:
        se.reset()
        _core._lazy_server_configs.pop(base, None)
        if key_box[0] is not None:
            _core._servers.pop(key_box[0], None)
