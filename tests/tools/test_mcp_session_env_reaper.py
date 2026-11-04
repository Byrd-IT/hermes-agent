"""Lifecycle regression contracts for per-session MCP connections."""


def test_restart_does_not_reactivate_the_previous_reaper():
    from tools import mcp_tool_session_env as se

    se.reset()
    se._ensure_reaper()
    old_thread, old_stop = se._reaper, se._reaper_stop
    try:
        se.reset()
        se._ensure_reaper()
        assert old_stop.is_set()
        assert se._reaper_stop is not old_stop
        old_thread.join(timeout=2)
        assert not old_thread.is_alive()
        assert se._reaper.is_alive()
    finally:
        current = se._reaper
        old_stop.set()
        se.reset()
        old_thread.join(timeout=2)
        if current is not None:
            current.join(timeout=2)


def test_close_rechecks_inflight_calls_on_the_mcp_loop(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from tools import mcp_tool as core
    from tools import mcp_tool_loop as loop
    from tools import mcp_tool_session_env as se

    key = ("scope#env:inflight", "server")
    server = SimpleNamespace(_inflight_tasks=set(), shutdown=AsyncMock())
    monkeypatch.setattr(core, "_servers", {key: server})
    monkeypatch.setattr(core, "_server_scope_keys", {key: "scope"})
    monkeypatch.setattr(se, "_last_used", {key: 0.0})
    monkeypatch.setattr(se, "_pending", {key: ("server", {})})

    def run(fn, **kwargs):
        # A call started after the reaper's thread-side snapshot, before its
        # shutdown coroutine was scheduled on the shared MCP event loop.
        class Running:
            def done(self):
                return False
        server._inflight_tasks.add(Running())
        return asyncio.run(fn())

    monkeypatch.setattr(loop, "_run_on_mcp_loop", run)
    assert se._close([key], protect_in_use=True, decided_at=100.0) == 0
    assert core._servers[key] is server
    server.shutdown.assert_not_awaited()
