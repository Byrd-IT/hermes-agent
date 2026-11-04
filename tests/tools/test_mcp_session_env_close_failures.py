"""Scoped session-env close must not orphan a live variant.

A TimeoutError while submitting the close to the MCP loop leaves the server
and its pending, last-used, and connect-lock entries in place, so a later
close or idle reap can still tear it down. A successful close clears those
entries and reports only servers that were actually live. Cancelling one
shutdown still awaits the others, then propagates.
"""

import asyncio
import concurrent.futures
import threading
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def isolated_ledgers(monkeypatch):
    """Swap process-global MCP ledgers so a test cannot leak into another."""
    from tools import mcp_tool as core
    from tools import mcp_tool_session_env as se

    state = SimpleNamespace(
        core=core,
        se=se,
        servers={},
        scope_keys={},
        hints={},
        error_counts={},
        breaker_opened_at={},
        errors_all_application={},
        connect_errors={},
        pending={},
        last_used={},
        connect_locks={},
        retry_after={},
    )
    monkeypatch.setattr(core, "_servers", state.servers)
    monkeypatch.setattr(core, "_server_scope_keys", state.scope_keys)
    monkeypatch.setattr(core, "_tool_read_only_hints", state.hints)
    monkeypatch.setattr(core, "_server_error_counts", state.error_counts)
    monkeypatch.setattr(
        core, "_server_breaker_opened_at", state.breaker_opened_at
    )
    monkeypatch.setattr(
        core, "_server_errors_all_application", state.errors_all_application
    )
    monkeypatch.setattr(core, "_server_connect_errors", state.connect_errors)
    monkeypatch.setattr(se, "_pending", state.pending)
    monkeypatch.setattr(se, "_last_used", state.last_used)
    monkeypatch.setattr(se, "_connect_locks", state.connect_locks)
    monkeypatch.setattr(se, "_retry_after", state.retry_after)
    return state


def _plant_live(ledgers, key, base, label):
    server = SimpleNamespace(shutdown=AsyncMock())
    lock = threading.Lock()
    ledgers.servers[key] = server
    ledgers.scope_keys[key] = key[0]
    ledgers.pending[key] = (base, {"RUN_LABEL": label})
    ledgers.last_used[key] = 0.0
    ledgers.connect_locks[key] = lock
    return server, lock


def _plant_pending(ledgers, key, base, label):
    lock = threading.Lock()
    ledgers.pending[key] = (base, {"RUN_LABEL": label})
    ledgers.last_used[key] = 0.0
    ledgers.connect_locks[key] = lock
    return lock


def _submit_times_out(coro_or_factory, **_kwargs):
    # Submit failed before loop-side selection. Do not run the factory.
    if asyncio.iscoroutine(coro_or_factory):
        coro_or_factory.close()
    raise TimeoutError("loop submission timed out")


def _run_inline(
    coro_or_factory: Callable[[], Awaitable[object]] | Awaitable[object],
    **_kwargs: object,
) -> object:
    if callable(coro_or_factory):
        pending: Awaitable[object] = coro_or_factory()
    else:
        pending = coro_or_factory
    return asyncio.run(pending)


def _arm(monkeypatch, runner):
    from tools import mcp_tool_loop as loop

    monkeypatch.setattr(loop, "_run_on_mcp_loop", runner)


def _assert_retained(ledgers, key, server, lock, base, label):
    assert ledgers.core._servers[key] is server
    assert ledgers.se._pending[key] == (base, {"RUN_LABEL": label})
    assert ledgers.se._last_used[key] == 0.0
    assert ledgers.se._connect_locks[key] is lock
    server.shutdown.assert_not_awaited()


def _assert_cleared(ledgers, key):
    assert key not in ledgers.core._servers
    assert key not in ledgers.se._pending
    assert key not in ledgers.se._last_used
    assert key not in ledgers.se._connect_locks


def test_close_for_bases_timeout_retains_live_entry_for_a_later_close(
    isolated_ledgers, monkeypatch
):
    """A failed loop submit must not drop a live variant a later close owns."""
    base = ("profile", "pinsrv")
    key = ("scope#env:one", "pinsrv")
    server, lock = _plant_live(isolated_ledgers, key, base, "alpha")
    _arm(monkeypatch, _submit_times_out)

    closed = isolated_ledgers.se.close_for_bases([base])

    assert closed == 0
    _assert_retained(isolated_ledgers, key, server, lock, base, "alpha")

    _arm(monkeypatch, _run_inline)
    assert isolated_ledgers.se.close_for_bases([base]) == 1
    _assert_cleared(isolated_ledgers, key)
    assert server.shutdown.await_count == 1


def test_close_for_bases_timeout_retains_live_entry_for_a_later_reap(
    isolated_ledgers, monkeypatch
):
    """A failed loop submit must not drop a live variant the reaper still owns."""
    base = ("profile", "pinsrv")
    key = ("scope#env:one", "pinsrv")
    server, lock = _plant_live(isolated_ledgers, key, base, "alpha")
    _arm(monkeypatch, _submit_times_out)

    closed = isolated_ledgers.se.close_for_bases([base])

    assert closed == 0
    _assert_retained(isolated_ledgers, key, server, lock, base, "alpha")

    _arm(monkeypatch, _run_inline)
    reaped = isolated_ledgers.se.reap_idle(
        now=isolated_ledgers.se._IDLE_TTL_SEC + 5
    )
    assert reaped == 1
    _assert_cleared(isolated_ledgers, key)
    assert server.shutdown.await_count == 1


def test_close_for_bases_counts_only_live_closures(
    isolated_ledgers, monkeypatch
):
    """A successful close clears what it closed and does not count never-connected keys."""
    base = ("profile", "pinsrv")
    other_base = ("profile", "other")
    live_keys = (
        ("scope#env:one", "pinsrv"),
        ("scope#env:two", "pinsrv"),
    )
    pending_only = ("scope#env:pending", "pinsrv")
    other_key = ("scope#env:other", "other")
    live = [
        _plant_live(isolated_ledgers, key, base, label)
        for key, label in zip(live_keys, ("alpha", "beta"), strict=True)
    ]
    _plant_pending(isolated_ledgers, pending_only, base, "gamma")
    other_server, other_lock = _plant_live(
        isolated_ledgers, other_key, other_base, "delta"
    )
    _arm(monkeypatch, _run_inline)

    closed = isolated_ledgers.se.close_for_bases([base])

    assert closed == len(live_keys)
    for key, (server, _lock) in zip(live_keys, live, strict=True):
        _assert_cleared(isolated_ledgers, key)
        assert server.shutdown.await_count == 1
    _assert_cleared(isolated_ledgers, pending_only)
    assert isolated_ledgers.core._servers[other_key] is other_server
    assert other_key in isolated_ledgers.se._pending
    assert other_key in isolated_ledgers.se._last_used
    assert isolated_ledgers.se._connect_locks[other_key] is other_lock
    other_server.shutdown.assert_not_awaited()


@pytest.mark.parametrize("threadsafe", [False, True])
def test_cancelled_shutdown_awaits_the_rest_then_propagates(
    isolated_ledgers, monkeypatch, threadsafe
):
    """The first shutdown's CancelledError still awaits the second, then propagates."""
    base = ("profile", "pinsrv")
    first_key = ("scope#env:one", "pinsrv")
    second_key = ("scope#env:two", "pinsrv")
    first, _lock = _plant_live(isolated_ledgers, first_key, base, "alpha")
    second, _lock = _plant_live(isolated_ledgers, second_key, base, "beta")
    first.shutdown.side_effect = asyncio.CancelledError
    def run(factory, **kwargs):
        try:
            return _run_inline(factory, **kwargs)
        except asyncio.CancelledError:
            if threadsafe:
                raise concurrent.futures.CancelledError from None
            raise

    _arm(monkeypatch, run)
    cancelled = concurrent.futures.CancelledError if threadsafe else asyncio.CancelledError
    with pytest.raises(cancelled):
        isolated_ledgers.se.close_for_bases([base])
    assert first.shutdown.await_count == 1
    assert second.shutdown.await_count == 1
