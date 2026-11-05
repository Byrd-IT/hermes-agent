"""Fenced server cleanup belongs to the loop that accepted it."""
import asyncio
import itertools
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


def _start_loop():
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    loop.call_soon(ready.set)
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    assert ready.wait(5)
    return loop, thread, loop.call_soon_threadsafe


def _join_loop(loop, thread, soon):
    if loop.is_closed():
        return
    if loop.is_running():
        try:
            soon(loop.stop)
        except RuntimeError:
            pass
        thread.join(5)
    if not loop.is_closed():
        loop.close()


def _hold_soon(loop):
    held = []
    real = loop.call_soon_threadsafe

    def hold(callback, *args, context=None):
        held.append((callback, args, context))

    return real, hold, held


def _release_held(soon, held):
    for callback, args, context in held:
        soon(callback, *args, context=context)


def _isolate_fenced(monkeypatch):
    """Fresh fenced ledgers. Generation ids must not leak across tests."""
    monkeypatch.setattr(session_env, "_FENCED_CLOSES", [])
    monkeypatch.setattr(session_env, "_FENCED_CLEANUPS", set())
    monkeypatch.setattr(session_env, "_LOOP_GENERATIONS", {})
    monkeypatch.setattr(session_env, "_LOOP_GENERATION_SEQ", itertools.count(1))


def _server(observed, loop_tag):
    async def shutdown():
        observed.append(loop_tag)

    return SimpleNamespace(shutdown=shutdown)


def test_old_loop_still_finishes_only_its_own_close(monkeypatch, isolated_ledgers):
    """A retiring loop runs the close it accepted, not one a newer loop accepted."""
    old_loop, old_thread, _old_soon = _start_loop()
    new_loop, new_thread, _new_soon = _start_loop()
    old_real, old_hold, old_held = _hold_soon(old_loop)
    new_real, new_hold, new_held = _hold_soon(new_loop)
    monkeypatch.setattr(old_loop, "call_soon_threadsafe", old_hold)
    monkeypatch.setattr(new_loop, "call_soon_threadsafe", new_hold)
    current = {"loop": old_loop}
    monkeypatch.setattr(loop_mod, "_running_loop", lambda: current["loop"])
    _isolate_fenced(monkeypatch)
    observed = []
    try:
        old_done = session_env._schedule_fenced_close(_server(observed, old_loop))
        current["loop"] = new_loop
        new_done = session_env._schedule_fenced_close(_server(observed, new_loop))
        assert old_held and new_held
        monkeypatch.setattr(old_loop, "call_soon_threadsafe", old_real)
        old_real(old_loop.stop)
        old_thread.join(5)
        lifecycle._finish_fenced_before_close(old_loop)
        assert old_done.wait(5)
        assert observed == [old_loop], "retiring loop ran another generation's close"
        assert not new_done.is_set()
        monkeypatch.setattr(new_loop, "call_soon_threadsafe", new_real)
        _release_held(new_real, new_held)
        assert new_done.wait(5)
        assert observed == [old_loop, new_loop]
    finally:
        monkeypatch.setattr(old_loop, "call_soon_threadsafe", old_real)
        monkeypatch.setattr(new_loop, "call_soon_threadsafe", new_real)
        _release_held(old_real, old_held)
        _release_held(new_real, new_held)
        _join_loop(old_loop, old_thread, old_real)
        _join_loop(new_loop, new_thread, new_real)


def test_reset_keeps_newer_close_for_its_own_loop(monkeypatch, isolated_ledgers):
    """Wildcard reset must not erase a close the retiring loop must not run."""
    old_loop = asyncio.new_event_loop()
    new_loop, new_thread, _new_soon = _start_loop()
    new_real, new_hold, new_held = _hold_soon(new_loop)
    monkeypatch.setattr(new_loop, "call_soon_threadsafe", new_hold)
    monkeypatch.setattr(loop_mod, "_running_loop", lambda: new_loop)
    _isolate_fenced(monkeypatch)
    observed = []
    try:
        finished = session_env._schedule_fenced_close(_server(observed, new_loop))
        assert new_held
        session_env.reset()
        assert session_env._FENCED_CLOSES, "reset dropped a live loop's fenced close"
        lifecycle._finish_fenced_before_close(old_loop)
        assert not finished.is_set()
        assert not observed
        monkeypatch.setattr(new_loop, "call_soon_threadsafe", new_real)
        new_real(new_loop.stop)
        new_thread.join(5)
        lifecycle._finish_fenced_before_close(new_loop)
        assert finished.wait(5)
        assert observed == [new_loop]
    finally:
        monkeypatch.setattr(new_loop, "call_soon_threadsafe", new_real)
        _release_held(new_real, new_held)
        _join_loop(new_loop, new_thread, new_real)
        old_loop.close()


def test_reset_keeps_in_flight_close_protected_from_drain(monkeypatch, isolated_ledgers):
    """Shutdown's reset runs before drain; the in-flight close must stay protected."""
    loop, thread, soon = _start_loop()
    monkeypatch.setattr(loop_mod, "_running_loop", lambda: loop)
    _isolate_fenced(monkeypatch)
    started = threading.Event()
    done = threading.Event()
    release = {}

    async def shutdown():
        started.set()
        await release["event"].wait()
        done.set()

    try:
        gate = threading.Event()

        async def install_release():
            release["event"] = asyncio.Event()
            gate.set()

        asyncio.run_coroutine_threadsafe(install_release(), loop).result(5)
        assert gate.wait(5)
        finished = session_env._schedule_fenced_close(SimpleNamespace(shutdown=shutdown))
        assert started.wait(5)
        session_env.reset()

        async def probe():
            protected = session_env._fenced_tasks()
            current = asyncio.current_task()
            pending = [
                task for task in asyncio.all_tasks()
                if task is not current and not task.done() and task not in protected
            ]
            close_tasks = [
                record.task for record in session_env._FENCED_CLOSES
                if record.task is not None
            ]
            return bool(close_tasks) and all(task not in pending for task in close_tasks)

        assert asyncio.run_coroutine_threadsafe(probe(), loop).result(5)
        loop.call_soon_threadsafe(release["event"].set)
        assert done.wait(5)
        assert finished.wait(5)
    finally:
        if release.get("event") is not None:
            loop.call_soon_threadsafe(release["event"].set)
        _join_loop(loop, thread, soon)


@pytest.mark.parametrize("give_up", ["timeout", "interrupt"])
def test_give_up_does_not_hand_close_to_another_loop(
    monkeypatch, isolated_ledgers, give_up,
):
    """Timeout and interrupt return the waiter; they do not retarget the close."""
    from tools.interrupt import set_interrupt

    old_loop = asyncio.new_event_loop()
    new_loop, new_thread, _new_soon = _start_loop()
    new_real, new_hold, new_held = _hold_soon(new_loop)
    monkeypatch.setattr(new_loop, "call_soon_threadsafe", new_hold)
    monkeypatch.setattr(loop_mod, "_running_loop", lambda: new_loop)
    monkeypatch.setattr(session_env, "_FENCED_CLEANUP_TIMEOUT", 0)
    _isolate_fenced(monkeypatch)
    observed = []
    tid = threading.current_thread().ident
    try:
        finished = session_env._schedule_fenced_close(_server(observed, new_loop))
        assert new_held
        if give_up == "interrupt":
            set_interrupt(True, thread_id=tid)
        session_env._wait_fenced(finished)
        lifecycle._finish_fenced_before_close(old_loop)
        assert not finished.is_set(), "give-up let another loop consume the close"
        assert not observed
        monkeypatch.setattr(new_loop, "call_soon_threadsafe", new_real)
        new_real(new_loop.stop)
        new_thread.join(5)
        lifecycle._finish_fenced_before_close(new_loop)
        assert finished.wait(5)
        assert observed == [new_loop]
    finally:
        set_interrupt(False, thread_id=tid)
        monkeypatch.setattr(new_loop, "call_soon_threadsafe", new_real)
        _release_held(new_real, new_held)
        _join_loop(new_loop, new_thread, new_real)
        old_loop.close()


def test_concurrent_arm_publishes_only_on_the_owning_loop(monkeypatch, isolated_ledgers):
    """Task publish stays under the ownership lock, and only the owner publishes."""
    old_loop, old_thread, old_soon = _start_loop()
    new_loop, new_thread, new_real = _start_loop()
    held_soon, new_hold, new_held = _hold_soon(new_loop)
    monkeypatch.setattr(new_loop, "call_soon_threadsafe", new_hold)
    monkeypatch.setattr(loop_mod, "_running_loop", lambda: new_loop)
    _isolate_fenced(monkeypatch)
    observed = []
    published = []
    real_ensure = asyncio.ensure_future

    def tracing_ensure(coro, *args, **kwargs):
        if session_env._FENCED_LOCK.locked():
            published.append(asyncio.get_running_loop())
        return real_ensure(coro, *args, **kwargs)

    monkeypatch.setattr(asyncio, "ensure_future", tracing_ensure)
    try:
        finished = session_env._schedule_fenced_close(_server(observed, new_loop))
        assert new_held
        record = session_env._FENCED_CLOSES[0]
        results = {}
        armed = {"old": threading.Event(), "new": threading.Event()}

        def schedule_arm(soon, name):
            def kick():
                async def _arm():
                    results[name] = session_env._arm_fenced(record)
                    armed[name].set()

                asyncio.ensure_future(_arm())

            soon(kick)

        # Saved soon, not the held call_soon: the spawn callback stays queued
        # while both loops enter _arm_fenced.
        schedule_arm(old_soon, "old")
        schedule_arm(new_real, "new")
        assert armed["old"].wait(5) and armed["new"].wait(5)
        assert results["old"] is None
        assert results["new"] is not None
        assert published == [new_loop]
        assert finished.wait(5)
        assert observed == [new_loop]
        assert record.task.get_loop() is new_loop
    finally:
        monkeypatch.setattr(asyncio, "ensure_future", real_ensure)
        monkeypatch.setattr(new_loop, "call_soon_threadsafe", held_soon)
        _release_held(held_soon, new_held)
        _join_loop(old_loop, old_thread, old_soon)
        _join_loop(new_loop, new_thread, new_real)


def test_old_loop_shutdown_does_not_consume_newer_fenced_close(
    monkeypatch, isolated_ledgers,
):
    """Stopping the registered loop must not run a close a replacement loop accepted."""
    from tools import mcp_tool as origin

    old_loop, old_thread, old_soon = _start_loop()
    new_loop, new_thread, _new_soon = _start_loop()
    new_real, new_hold, new_held = _hold_soon(new_loop)
    monkeypatch.setattr(new_loop, "call_soon_threadsafe", new_hold)
    monkeypatch.setattr(loop_mod, "_running_loop", lambda: new_loop)
    monkeypatch.setattr(origin, "_mcp_loop", old_loop)
    monkeypatch.setattr(origin, "_mcp_thread", old_thread)
    monkeypatch.setattr(lifecycle, "_kill_orphaned_mcp_children", lambda *args, **kwargs: None)
    _isolate_fenced(monkeypatch)
    observed = []
    try:
        finished = session_env._schedule_fenced_close(_server(observed, new_loop))
        assert new_held
        session_env.reset()
        assert loop_mod._stop_mcp_loop()
        assert not finished.is_set(), "old loop shutdown consumed a newer generation's close"
        assert not observed
        assert old_loop.is_closed()
        monkeypatch.setattr(new_loop, "call_soon_threadsafe", new_real)
        _release_held(new_real, new_held)
        assert finished.wait(5)
        assert observed == [new_loop]
    finally:
        monkeypatch.setattr(new_loop, "call_soon_threadsafe", new_real)
        _release_held(new_real, new_held)
        if not old_loop.is_closed():
            _join_loop(old_loop, old_thread, old_soon)
        _join_loop(new_loop, new_thread, new_real)


def _parked_task(loop):
    """A task bound to *loop*, so its loop is the one that built it."""

    async def make():
        async def hang():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return

        return asyncio.ensure_future(hang())

    return asyncio.run_coroutine_threadsafe(make(), loop).result(5)


def _bound_server(task, observed):
    async def shutdown():
        observed.append(asyncio.get_running_loop())

    return SimpleNamespace(shutdown=shutdown, _task=task)


def _drop_task(loop, soon, task):
    if task is None or task.done() or loop.is_closed():
        return
    if not loop.is_running():
        task.cancel()
        try:
            loop.run_until_complete(task)
        except (asyncio.CancelledError, RuntimeError):
            pass
        return

    async def cancel():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    try:
        asyncio.run_coroutine_threadsafe(cancel(), loop).result(5)
    except (asyncio.CancelledError, TimeoutError, RuntimeError):
        pass
    else:
        return
    try:
        soon(task.cancel)
    except RuntimeError:
        pass


def _finish_on(loop, timeout):
    return asyncio.run_coroutine_threadsafe(
        session_env._finish_pending_fenced(timeout=timeout), loop,
    ).result(5)


def test_fenced_close_uses_creator_loop_not_current(monkeypatch, isolated_ledgers):
    """A server built on one loop must not be shut down on a later MCP loop."""
    creator, creator_thread, _creator_soon = _start_loop()
    current, current_thread, _current_soon = _start_loop()
    task = _parked_task(creator)
    creator_real, creator_hold, creator_held = _hold_soon(creator)
    current_real, current_hold, current_held = _hold_soon(current)
    monkeypatch.setattr(creator, "call_soon_threadsafe", creator_hold)
    monkeypatch.setattr(current, "call_soon_threadsafe", current_hold)
    monkeypatch.setattr(loop_mod, "_running_loop", lambda: current)
    _isolate_fenced(monkeypatch)
    observed = []
    try:
        finished = session_env._schedule_fenced_close(_bound_server(task, observed))
        assert creator_held, "close was not queued on the loop that built the server"
        assert not current_held, "close was queued on the current loop"
        monkeypatch.setattr(current, "call_soon_threadsafe", current_real)
        _finish_on(current, 1)
        assert not finished.is_set(), "replacement loop consumed the creator's close"
        assert not observed
        monkeypatch.setattr(creator, "call_soon_threadsafe", creator_real)
        _release_held(creator_real, creator_held)
        assert finished.wait(5)
        assert observed == [creator]
    finally:
        monkeypatch.setattr(creator, "call_soon_threadsafe", creator_real)
        monkeypatch.setattr(current, "call_soon_threadsafe", current_real)
        _release_held(creator_real, creator_held)
        _release_held(current_real, current_held)
        _drop_task(creator, creator_real, task)
        _join_loop(creator, creator_thread, creator_real)
        _join_loop(current, current_thread, current_real)


def test_closed_creator_loop_is_not_retargeted(monkeypatch, isolated_ledgers):
    """A dead creator is a failed close, not a shutdown on the replacement loop."""
    creator, creator_thread, creator_soon = _start_loop()
    current, current_thread, _current_soon = _start_loop()
    task = _parked_task(creator)
    _drop_task(creator, creator_soon, task)
    creator_soon(creator.stop)
    creator_thread.join(5)
    creator.close()
    assert task.get_loop().is_closed()
    current_real, current_hold, current_held = _hold_soon(current)
    monkeypatch.setattr(current, "call_soon_threadsafe", current_hold)
    monkeypatch.setattr(loop_mod, "_running_loop", lambda: current)
    _isolate_fenced(monkeypatch)
    observed = []
    try:
        finished = session_env._schedule_fenced_close(_bound_server(task, observed))
        assert finished.is_set(), "closed creator left the waiter blocked on another loop"
        assert not current_held, "closed creator was retargeted to the current loop"
        assert not observed
        assert not session_env._FENCED_CLOSES
    finally:
        monkeypatch.setattr(current, "call_soon_threadsafe", current_real)
        _release_held(current_real, current_held)
        _join_loop(current, current_thread, current_real)


def test_stopped_creator_is_not_finished_by_replacement(monkeypatch, isolated_ledgers):
    """Drain on the live loop must not run a close the stopped creator still owns."""
    creator, creator_thread, creator_soon = _start_loop()
    current, current_thread, _current_soon = _start_loop()
    task = _parked_task(creator)
    creator_soon(creator.stop)
    creator_thread.join(5)
    assert not creator.is_running() and not creator.is_closed()
    current_real, current_hold, current_held = _hold_soon(current)
    monkeypatch.setattr(current, "call_soon_threadsafe", current_hold)
    monkeypatch.setattr(loop_mod, "_running_loop", lambda: current)
    _isolate_fenced(monkeypatch)
    observed = []
    try:
        finished = session_env._schedule_fenced_close(_bound_server(task, observed))
        assert not current_held, "stopped creator's close was queued on the current loop"
        assert not finished.is_set()
        monkeypatch.setattr(current, "call_soon_threadsafe", current_real)
        _finish_on(current, 1)
        assert not finished.is_set(), "replacement drain ran another loop's close"
        assert not observed
        lifecycle._finish_fenced_before_close(creator)
        assert finished.wait(5)
        assert observed == [creator]
    finally:
        monkeypatch.setattr(current, "call_soon_threadsafe", current_real)
        _release_held(current_real, current_held)
        if not creator.is_closed():
            _drop_task(creator, creator_soon, task)
            if not creator.is_closed():
                creator.close()
        _join_loop(current, current_thread, current_real)


async def _parked_body():
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        return


def test_ensure_connected_fences_close_on_the_loop_that_built_the_server(
    monkeypatch, isolated_ledgers,
):
    """After connect returns, the close follows the builder loop, not ``_running_loop``."""
    from agent.session_client_env import client_env_scope
    from tools import mcp_tool_discovery as discovery

    creator, creator_thread, creator_soon = _start_loop()
    current, current_thread, current_soon = _start_loop()
    current_real, current_hold, current_held = _hold_soon(current)
    monkeypatch.setattr(current, "call_soon_threadsafe", current_hold)
    loops = {"active": creator}
    monkeypatch.setattr(loop_mod, "_running_loop", lambda: loops["active"])
    monkeypatch.setattr(loop_mod, "_ensure_mcp_loop", lambda: None)
    monkeypatch.setattr(session_env, "_render", lambda config, values: config)
    monkeypatch.setattr(session_env, "_FENCED_CLEANUP_TIMEOUT", 0)
    _isolate_fenced(monkeypatch)
    base = "origin-loop"
    isolated_ledgers.lazy_configs[base] = {
        "command": "unused",
        "session_env": ["PIN"],
    }
    observed = []
    parked = {}
    key_box: list = [None]
    closed = threading.Event()

    def run_on(coro_or_factory, timeout=30, on_discard=None):
        loop = loops["active"]
        coro = coro_or_factory() if callable(coro_or_factory) else coro_or_factory
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)  # type: ignore[arg-type]

    async def connect(_name, _config):
        task = asyncio.ensure_future(_parked_body())
        parked["task"] = task

        async def shutdown():
            observed.append(asyncio.get_running_loop())
            if not task.done():
                task.cancel()
            closed.set()

        with session_env._guard:
            session_env._pending.pop(key_box[0], None)
        loops["active"] = current
        return SimpleNamespace(session=object(), shutdown=shutdown, _task=task)

    monkeypatch.setattr(loop_mod, "_run_on_mcp_loop", run_on)
    monkeypatch.setattr(discovery, "_connect_server", connect)
    try:
        with client_env_scope({"PIN": "a"}):
            key_box[0] = session_env.variant_key(base)
            assert key_box[0] is not None
            result = session_env.ensure_connected(base, key_box[0])
        assert result is None
        assert not current_held, "close was queued on the replacement loop"
        assert closed.wait(5), "close did not run on the loop that built the server"
        assert observed == [creator]
    finally:
        monkeypatch.setattr(current, "call_soon_threadsafe", current_real)
        _release_held(current_real, current_held)
        _drop_task(creator, creator_soon, parked.get("task"))
        _join_loop(creator, creator_thread, creator_soon)
        _join_loop(current, current_thread, current_soon)
