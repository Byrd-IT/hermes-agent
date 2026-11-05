"""Variant teardown must fence late connects and never echo exception payloads."""
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tools import mcp_tool_session_env as se
from tools.mcp_tool_common import _core

# Ledgers live on the process. Borrow the close-failure fixture instead of
# mutating the real dicts and restoring them in finally.
pytest_plugins = ["tests.tools.test_mcp_session_env_close_failures"]


@pytest.mark.parametrize("interrupt", [False, True])
def test_connect_cannot_publish_after_teardown_or_swallow_interrupt(
    monkeypatch, interrupt, isolated_ledgers,
):
    from agent.session_client_env import client_env_scope
    from tools import mcp_tool_loop as loop

    base = "lifetime"
    _core._lazy_server_configs[base] = {"command": "unused", "session_env": ["RUN_PIN"]}
    server = SimpleNamespace(session=object(), shutdown=AsyncMock())

    def close_fenced(server):
        import asyncio
        import threading
        asyncio.run(se._shutdown_discarded(server))
        finished = threading.Event()
        finished.set()
        return finished

    monkeypatch.setattr(se, "_schedule_fenced_close", close_fenced)
    monkeypatch.setattr(loop, "_ensure_mcp_loop", lambda: None)
    monkeypatch.setattr(se, "_ensure_reaper", lambda: None)
    monkeypatch.setattr(se, "_render", lambda config, values: config)

    calls = []

    def dispatch(factory, timeout, **kwargs):
        import asyncio
        calls.append(factory)
        if len(calls) > 1:
            return asyncio.run(factory())
        if interrupt:
            raise InterruptedError("cancelled")
        se.close_for_bases([base])
        return server

    monkeypatch.setattr(loop, "_run_on_mcp_loop", dispatch)
    with client_env_scope({"RUN_PIN": "a"}):
        key = se.variant_key(base)
        if interrupt:
            with pytest.raises(InterruptedError):
                se.ensure_connected(base, key)
            assert key not in se._retry_after
        else:
            assert se.ensure_connected(base, key) is None
            assert key not in _core._servers
            server.shutdown.assert_awaited_once()


@pytest.mark.parametrize("utility", [False, True])
def test_handler_lease_prevents_eviction_before_rpc_starts(
    monkeypatch, utility, isolated_ledgers,
):
    from agent.session_client_env import client_env_scope
    from tools import mcp_tool_discovery as discovery
    from tools import mcp_tool_handlers as handlers

    base = "leased"
    _core._lazy_server_configs[base] = {"command": "unused", "session_env": ["RUN_PIN"]}
    monkeypatch.setattr(handlers, "_trust_gate_check", lambda *args: None)
    monkeypatch.setattr(handlers, "_check_circuit_breaker", lambda *args: None)
    with client_env_scope({"RUN_PIN": "a"}):
        key = se.variant_key(base)
        _core._servers[key] = SimpleNamespace(session=object())

        def acquire(*args):
            assert se._close([key], protect_in_use=True) == 0
            assert key in _core._servers
            if utility:
                return None
            return None, "unavailable"

        if utility:
            monkeypatch.setattr(discovery, "_get_connected_server_for_call", acquire)
            handler = handlers._make_utility_handler("list", "list", None, None)(base, 5)
        else:
            monkeypatch.setattr(handlers, "_acquire_call_server", acquire)
            handler = handlers._make_tool_handler(base, "who", 5)
        handler({})
        assert key not in se._call_leases


@pytest.mark.parametrize("reset_all", [False, True])
@pytest.mark.parametrize("new_caller", [False, True])
def test_shutdown_invalidates_a_call_queued_after_lease(
    monkeypatch, reset_all, new_caller, isolated_ledgers,
):
    from contextvars import Context
    from agent.session_client_env import client_env_scope
    from tools import mcp_tool_discovery as discovery
    from tools import mcp_tool_loop as loop
    from tools.mcp_tool_scope import _resolve_server_key

    base = "queued-shutdown"
    _core._lazy_server_configs[base] = {"command": "unused", "session_env": ["RUN_PIN"]}
    keys = []
    monkeypatch.setattr(loop, "_ensure_mcp_loop", lambda: None)
    monkeypatch.setattr(se, "_ensure_reaper", lambda: None)
    monkeypatch.setattr(se, "_render", lambda config, values: config)
    launched = []
    def dispatch(factory, **kwargs):
        import asyncio
        if factory.__name__ == "_go":
            launched.append(True)
        return asyncio.run(factory())

    monkeypatch.setattr(loop, "_run_on_mcp_loop", dispatch)

    def replacement():
        with client_env_scope({"RUN_PIN": "a"}):
            fresh = se.variant_key(base)
            keys.append(fresh)
            _core._servers[fresh] = SimpleNamespace(session=object())

    def queued():
        keys.append(_resolve_server_key(base))
        if reset_all:
            se.reset()
        else:
            se.close_for_bases([base])
        if new_caller:
            Context().run(replacement)
        assert discovery._get_connected_server_for_call(base) is None
        assert not launched
        assert keys[0] not in se._pending

    with client_env_scope({"RUN_PIN": "a"}):
        se.protect_call(base, queued)()
    assert not se._call_leases


def test_scoped_close_selects_variants_atomically(monkeypatch, isolated_ledgers):
    import asyncio
    from agent.session_client_env import client_env_scope
    from tools import mcp_tool_loop as loop

    monkeypatch.setattr(loop, "_run_on_mcp_loop", lambda fn, **kw: asyncio.run(fn()))

    base = "atomic-close"
    _core._lazy_server_configs[base] = {"command": "unused", "session_env": ["RUN_PIN"]}
    close = se._close
    keys = []

    def interleaved(*args, **kwargs):
        with client_env_scope({"RUN_PIN": "later"}):
            keys.append(se.variant_key(base))
        return close(*args, **kwargs)

    monkeypatch.setattr(se, "_close", interleaved)
    with client_env_scope({"RUN_PIN": "first"}):
        keys.append(se.variant_key(base))
    se.close_for_bases([base])
    assert all(key not in se._pending for key in keys)


def test_reaper_failure_does_not_log_exception_payload(monkeypatch, caplog):
    payload = "Bearer synthetic-lifetime-secret-ABC123"

    def fail():
        raise RuntimeError(payload)

    class Stop:
        calls = 0

        def wait(self, timeout):
            self.calls += 1
            return self.calls > 1

    monkeypatch.setattr(se, "reap_idle", fail)
    with caplog.at_level(logging.DEBUG, logger="tools.mcp_tool"):
        se._reaper_loop(Stop())
    assert "reaper pass failed" in caplog.text
    assert payload not in caplog.text
    assert "ABC123" not in caplog.text
