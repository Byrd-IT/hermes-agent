"""A fenced variant close must finish after the caller gives up.

``ensure_connected`` waits for that close, but a timeout or interrupt must not
drop the server: it was already built, and skipping shutdown orphans the
process. That includes a give-up that lands before the close coroutine is
stepped.
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


def test_fenced_cleanup_finishes_after_caller_timeout(mcp_loop, monkeypatch):
    """The caller returns on timeout; the close, already in progress, still finishes."""
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
    monkeypatch.setattr(se, "_FENCED_CLEANUP_TIMEOUT", 0.05)

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


def _give_up_before_close_still_shuts_down(mcp_loop, monkeypatch, *, base, interrupt):
    """Hold the close callback until the caller has given up, then require shutdown.

    The second ``call_soon_threadsafe`` is the close. Releasing it only after
    the caller returns isolates a give-up that lands before the coroutine is
    stepped — the case a cancellable wait drops.
    """
    from agent.session_client_env import client_env_scope
    from tools import mcp_tool_discovery as discovery
    from tools import mcp_tool_loop as loop
    from tools.interrupt import set_interrupt

    monkeypatch.setattr(_core, "_lazy_server_configs",
                        {base: {"command": "unused", "session_env": ["PIN"]}})
    monkeypatch.setattr(_core, "_servers", {})
    monkeypatch.setattr(se, "_ensure_reaper", lambda: None)
    monkeypatch.setattr(loop, "_ensure_mcp_loop", lambda: None)
    monkeypatch.setattr(se, "_render", lambda config, values: config)
    monkeypatch.setattr(se, "_FENCED_CLEANUP_TIMEOUT", 0.05)

    started = threading.Event()
    finished = threading.Event()
    caller_done = threading.Event()
    held_event = threading.Event()
    result = {}
    held: list = []
    scheduled = {"n": 0}
    caller_tid = {}
    real_soon = mcp_loop.call_soon_threadsafe
    real_run = loop._run_on_mcp_loop

    def run_then_interrupt(*args, **kwargs):
        outcome = real_run(*args, **kwargs)
        caller_tid["id"] = threading.current_thread().ident
        set_interrupt(True, thread_id=caller_tid["id"])
        return outcome

    if interrupt:
        monkeypatch.setattr(loop, "_run_on_mcp_loop", run_then_interrupt)

    async def close():
        started.set()
        finished.set()

    server = SimpleNamespace(session=object(), shutdown=close)

    async def connect(_name, _config):
        with se._guard:
            se._pending.pop(key_box[0], None)
        return server

    monkeypatch.setattr(discovery, "_connect_server", connect)

    def gated(callback, *args, context=None):
        scheduled["n"] += 1
        if scheduled["n"] == 1:
            return real_soon(callback, *args, context=context)
        held.append((callback, args, context))
        held_event.set()
        return None

    monkeypatch.setattr(mcp_loop, "call_soon_threadsafe", gated)
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
        assert held_event.wait(5), "cleanup was never scheduled on the loop"
        assert caller_done.wait(5), "caller did not return after giving up"
        assert "exc" not in result, result.get("exc")
        assert result["value"] is None
        assert not started.is_set(), "close started before the caller gave up; race not isolated"
        callback, args, context = held[0]
        real_soon(callback, *args, context=context)
        assert finished.wait(5), "close did not finish after the caller gave up before it started"
        assert key_box[0] not in _core._servers
    finally:
        monkeypatch.setattr(mcp_loop, "call_soon_threadsafe", real_soon)
        if caller_tid.get("id") is not None:
            set_interrupt(False, thread_id=caller_tid["id"])
        caller_done.set()
        if held and not finished.is_set():
            callback, args, context = held[0]
            try:
                real_soon(callback, *args, context=context)
            except RuntimeError:
                pass
        thread.join(5)
        se.reset()
        _core._lazy_server_configs.pop(base, None)
        if key_box[0] is not None:
            _core._servers.pop(key_box[0], None)


def test_fenced_cleanup_runs_when_caller_gives_up_before_close_starts(mcp_loop, monkeypatch):
    """A timeout before the close coroutine is stepped must not drop the server."""
    _give_up_before_close_still_shuts_down(
        mcp_loop, monkeypatch, base="fenced-before-start", interrupt=False)


def test_fenced_cleanup_runs_when_interrupted_before_close_starts(mcp_loop, monkeypatch):
    """An interrupt before the close coroutine is stepped must not drop the server."""
    _give_up_before_close_still_shuts_down(
        mcp_loop, monkeypatch, base="fenced-before-interrupt", interrupt=True)
