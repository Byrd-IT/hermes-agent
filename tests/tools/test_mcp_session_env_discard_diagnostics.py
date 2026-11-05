"""A rejected discarded-result cleanup must not log scheduler exception payloads."""
import asyncio
import concurrent.futures
import logging
from unittest.mock import AsyncMock

import pytest


@pytest.mark.parametrize("payload", [
    "Authorization: Bearer synthetic-discard-ABC123",
    "https://user:pass@host.invalid/path",
    "synthetic-token-recognizable-ABC123",
    "X-Api-Key: synthetic-discard-key-ABC123",
    "https://host.invalid/path?api_key=synthetic-discard-ABC123",
])
def test_discard_scheduling_error_has_no_payload(monkeypatch, caplog, payload):
    from tools import interrupt
    from tools import mcp_tool_loop as loop

    monkeypatch.setattr(loop, "_running_loop", lambda: object())
    monkeypatch.setattr(interrupt, "is_interrupted", lambda: True)
    cleanup = AsyncMock()
    scheduled = []

    def schedule(coro, target):
        scheduled.append(coro)
        if len(scheduled) == 1:
            future = concurrent.futures.Future()
            future.set_result(asyncio.run(coro))
            return future
        raise RuntimeError(payload)

    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", schedule)
    async def result():
        return object()

    with caplog.at_level(logging.DEBUG, logger="tools.mcp_tool"):
        with pytest.raises(InterruptedError):
            loop._run_on_mcp_loop(result, on_discard=cleanup)
    assert len(scheduled) == 2
    assert scheduled[1].cr_frame is None
    assert "cleanup scheduling failed" in caplog.text
    assert payload not in caplog.text
    assert "ABC123" not in caplog.text
    assert "user:pass" not in caplog.text
