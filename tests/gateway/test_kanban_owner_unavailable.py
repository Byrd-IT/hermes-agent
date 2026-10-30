"""The embedded kanban dispatcher claims nothing while its gateway cannot own workers (drain, restart, update, not ready)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


def runner_with(**state):
    from gateway.run import GatewayRunner
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner._draining = False
    for key, value in state.items():
        setattr(runner, key, value)
    return runner


@pytest.mark.parametrize("state,expected", [
    ({}, ""),
    ({"_draining": True}, "draining"),
    ({"_draining": True, "session_runtime_descriptor": {"drain_reason": "restart_wait"}}, "restart_wait"),
    ({"_restart_requested": True}, "draining"),
    ({"session_runtime_descriptor": {"state": "starting"}}, "not_ready"),
    ({"session_runtime_descriptor": {"state": "failed"}}, "not_ready"),
    ({"session_runtime_descriptor": {"state": "ready"}}, ""),
])
def test_owner_unavailable_states(state, expected):
    from gateway.kanban_watchers_common import _kanban_owner_unavailable
    with patch("hermes_cli.gateway_runtime._update_fenced", return_value=False):
        assert _kanban_owner_unavailable(runner_with(**state)) == expected


def test_update_fence_reads_as_update_paused():
    from gateway.kanban_watchers_common import _kanban_owner_unavailable
    with patch("hermes_cli.gateway_runtime._update_fenced", return_value=True):
        assert _kanban_owner_unavailable(runner_with()) == "update_paused"


def drive_watcher(runner, ticks):
    """Run the embedded watcher for ``ticks`` ticks; return the fake dispatcher."""
    dispatcher = MagicMock()
    dispatcher.tick_once.return_value = []
    dispatcher.ready_nonempty.return_value = False
    seen = []

    async def fake_sleep(_delay):
        seen.append(1)
        if len(seen) > ticks:
            runner._running = False

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    cfg = {"kanban": {}}
    runner._kanban_dispatcher_boot = lambda: (lambda: cfg, MagicMock(), cfg["kanban"])
    runner._sleep_between_ticks = lambda interval: fake_sleep(interval)
    with patch("gateway.kanban_watchers._KanbanDispatcher", return_value=dispatcher), \
         patch("gateway.kanban_watchers._resolve_dispatcher_settings",
               return_value=SimpleNamespace(interval=1.0)), \
         patch("gateway.kanban_watchers._kanban_dispatch_allowed", return_value=True), \
         patch("gateway.kanban_watchers._resolve_auto_decompose_settings", return_value=(False, 0)), \
         patch("gateway.kanban_watchers._to_thread_process_service", side_effect=fake_to_thread), \
         patch("hermes_cli.kanban_db_dispatch.reap_worker_zombies", return_value=[]), \
         patch("hermes_cli.gateway_runtime._update_fenced", return_value=False), \
         patch("asyncio.sleep", side_effect=fake_sleep):
        asyncio.run(runner._kanban_dispatcher_watcher())
    return dispatcher


def test_watcher_claims_nothing_while_draining_and_resumes_after():
    drained = drive_watcher(runner_with(_draining=True), ticks=3)
    drained.tick_once.assert_not_called()
    assert drive_watcher(runner_with(), ticks=3).tick_once.call_count >= 1


def test_watcher_resumes_on_the_same_runner_once_the_drain_ends():
    runner = runner_with(_draining=True)
    dispatcher = MagicMock()
    dispatcher.tick_once.return_value = []
    dispatcher.ready_nonempty.return_value = False
    ticks = []

    async def fake_sleep(_delay):
        ticks.append(dispatcher.tick_once.call_count)
        if len(ticks) == 3:
            runner._draining = False  # the new gateway is running
        if len(ticks) > 5:
            runner._running = False

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    cfg = {"kanban": {}}
    runner._kanban_dispatcher_boot = lambda: (lambda: cfg, MagicMock(), cfg["kanban"])
    with patch("gateway.kanban_watchers._KanbanDispatcher", return_value=dispatcher), \
         patch("gateway.kanban_watchers._resolve_dispatcher_settings", return_value=SimpleNamespace(interval=1.0)), \
         patch("gateway.kanban_watchers._kanban_dispatch_allowed", return_value=True), \
         patch("gateway.kanban_watchers._resolve_auto_decompose_settings", return_value=(False, 0)), \
         patch("gateway.kanban_watchers._to_thread_process_service", side_effect=fake_to_thread), \
         patch("hermes_cli.kanban_db_dispatch.reap_worker_zombies", return_value=[]), \
         patch("hermes_cli.gateway_runtime._update_fenced", return_value=False), \
         patch("asyncio.sleep", side_effect=fake_sleep):
        asyncio.run(runner._kanban_dispatcher_watcher())
    assert ticks[2] == 0, "no claim while draining"
    assert dispatcher.tick_once.call_count >= 1, "dispatch resumed after the drain ended"
