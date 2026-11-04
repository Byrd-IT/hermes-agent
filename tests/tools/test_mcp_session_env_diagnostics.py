"""Session-env lifecycle diagnostics never disclose exception payloads."""
import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.parametrize("payload", [
    "Authorization: Bearer synthetic-bearer-ABC123",
    "https://user:pass@host.invalid/path",
    "synthetic-token-recognizable-ABC123",
    "X-Api-Key: synthetic-api-key-ABC123",
    "https://host.invalid/path?api_key=synthetic-query-ABC123",
])
@pytest.mark.parametrize("path", ["reaper", "close", "shutdown", "stale", "failed_connect", "discarded"])
def test_lifecycle_diagnostics_hide_exception_payload(monkeypatch, caplog, payload, path):
    from tools import mcp_tool_session_env as se
    from tools import mcp_tool_loop as loop
    from tools import mcp_tool_discovery as discovery
    from tools import mcp_tool as _core

    caplog.set_level(logging.DEBUG, logger="tools.mcp_tool")
    key = ("scope#env:diagnostic", "test-server")
    failure = RuntimeError(payload)
    server = SimpleNamespace(session=None, shutdown=AsyncMock(side_effect=failure))
    monkeypatch.setattr(_core, "_servers", {})
    monkeypatch.setattr(_core, "_server_scope_keys", {})
    monkeypatch.setattr(se, "_pending", {key: ("base", {"RUN_PIN": payload})})
    monkeypatch.setattr(se, "_last_used", {})
    monkeypatch.setattr(se, "_connect_locks", {})
    monkeypatch.setattr(se, "_retry_after", {})
    monkeypatch.setattr(loop, "_ensure_mcp_loop", lambda: None)
    monkeypatch.setattr(se, "_ensure_reaper", lambda: None)
    monkeypatch.setattr(loop, "_run_on_mcp_loop", lambda fn, **kw: asyncio.run(fn()))

    if path == "discarded":
        asyncio.run(se._shutdown_discarded(server))
    elif path == "reaper":
        monkeypatch.setattr(se, "reap_idle", lambda: (_ for _ in ()).throw(failure))
        waits = iter([False, True])
        se._reaper_loop(SimpleNamespace(wait=lambda _: next(waits)))
    elif path in ("close", "shutdown"):
        _core._servers[key] = server
        if path == "close":
            monkeypatch.setattr(loop, "_run_on_mcp_loop", lambda *a, **kw: (_ for _ in ()).throw(failure))
        se._close([key])
    else:
        monkeypatch.setattr(se, "_base_config", lambda _: {"command": "test"})
        monkeypatch.setattr(se, "_render", lambda config, values: config)
        monkeypatch.setattr(se, "_evict", lambda *a, **kw: [])
        if path == "stale":
            _core._servers[key] = server

        async def connect(name, config):
            _core._connect_server_claim.get()(server)
            raise failure

        monkeypatch.setattr(discovery, "_connect_server", connect)
        assert se.ensure_connected("test-server", key) is None
    assert caplog.records, "probe must exercise a diagnostic"
    assert payload not in caplog.text
    assert "ABC123" not in caplog.text
    assert "user:pass" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
