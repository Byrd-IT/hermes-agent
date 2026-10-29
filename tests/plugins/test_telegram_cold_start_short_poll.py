"""Cold-start Telegram getUpdates is one short poll, then the normal 10s loop.

No network. A fake request records the timeout PTB sends. Readiness must clear
from that first real getUpdates, and the requested hold is 1s rather than 10s.
"""

import asyncio
import json
import time

import pytest

telegram = pytest.importorskip("telegram", reason="python-telegram-bot not installed")
if not getattr(telegram, "__file__", None):
    pytest.skip("telegram is the gateway test mock, not python-telegram-bot", allow_module_level=True)
from telegram.error import NetworkError
from telegram.ext import Application
from telegram.request import BaseRequest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


_MESSAGE = {
    "message_id": 1,
    "date": 1800000000,
    "chat": {"id": 42, "type": "private"},
    "from": {"id": 7, "is_bot": False, "first_name": "Human"},
    "text": "hi",
}


def _update(update_id: int) -> dict:
    return {"update_id": update_id, "message": dict(_MESSAGE)}


class _GeneralRequest(BaseRequest):
    @property
    def read_timeout(self):
        return 5

    async def initialize(self):
        return None

    async def shutdown(self):
        return None

    async def do_request(self, url, method, request_data=None, **_kwargs):
        if str(url).endswith("/getMe"):
            body = {
                "ok": True,
                "result": {"id": 1, "is_bot": True, "first_name": "Test", "username": "test_bot"},
            }
            return 200, json.dumps(body).encode()
        return 200, b'{"ok":true,"result":true}'


class _ScriptedUpdates(BaseRequest):
    """getUpdates stand-in. ``script`` maps offset -> list of update dicts.

    ``None`` offset is the first poll. A repeated offset returns the same updates
    again so a double-fetch shows up as a duplicate delivery.
    """

    def __init__(self, script, *, fail_first=False):
        self.script = script
        self.fail_first = fail_first
        self.calls = []
        self._failed = False

    @property
    def read_timeout(self):
        return 5

    async def initialize(self):
        return None

    async def shutdown(self):
        return None

    async def do_request(self, url, method, request_data=None, **kwargs):
        if not str(url).endswith("/getUpdates"):
            return 200, b'{"ok":true,"result":true}'
        params = request_data.parameters if request_data is not None else {}
        timeout = params.get("timeout")
        offset = params.get("offset")
        self.calls.append({
            "timeout": timeout,
            "offset": offset,
            "read_timeout": kwargs.get("read_timeout"),
            "allowed_updates": params.get("allowed_updates"),
        })
        if self.fail_first and not self._failed:
            self._failed = True
            raise NetworkError("cold-start getUpdates failed")
        updates = self.script.get(offset, [])
        body = {"ok": True, "result": updates}
        return 200, json.dumps(body).encode()


async def _adapter_and_app(script, *, fail_first=False):
    updates = _ScriptedUpdates(script, fail_first=fail_first)
    app = (
        Application.builder()
        .token("123456:test-token")
        .request(_GeneralRequest())
        .get_updates_request(updates)
        .build()
    )
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="123456:test-token"))
    adapter._instrument_polling_request(updates)
    adapter._app = app
    await app.initialize()
    return adapter, app, updates


async def _stop(adapter, app):
    updater = getattr(app, "updater", None)
    if updater is not None and updater.running:
        await updater.stop()
    verifier = getattr(adapter, "_polling_progress_verifier_task", None)
    if verifier is not None and not verifier.done():
        verifier.cancel()
        await asyncio.gather(verifier, return_exceptions=True)
    for task in list(getattr(adapter, "_background_tasks", ())):
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    await app.shutdown()


def _long_polls(updates):
    return [call for call in updates.calls if (call["timeout"] or 0) > 0]


@pytest.mark.asyncio
async def test_cold_start_first_poll_is_short_and_readiness_does_not_hold_10s():
    script = {None: []}
    adapter, app, updates = await _adapter_and_app(script)
    started = time.monotonic()
    try:
        ok = await asyncio.wait_for(
            adapter._start_polling_resilient(
                drop_pending_updates=True,
                error_callback=lambda _error: None,
                require_progress=True,
            ),
            timeout=2,
        )
        elapsed = time.monotonic() - started
        # Let the steady-state poll (the one after readiness) be issued.
        for _ in range(50):
            if len(_long_polls(updates)) >= 2:
                break
            await asyncio.sleep(0)
        longs = _long_polls(updates)
        assert ok is True
        assert adapter._polling_progress_event.is_set()
        assert longs[0]["timeout"] == 1
        assert longs[0]["offset"] == 0
        assert float(longs[1]["timeout"]) == 10
        # Client read budget must shrink with the API timeout, or a hung socket still waits ~10s.
        assert float(longs[1]["read_timeout"]) - float(longs[0]["read_timeout"]) == 9
        # The fake returns immediately; the clock is the timeout Telegram was asked to hold.
        # Later polls are 10s and must not be folded into the readiness bound.
        assert float(longs[0]["timeout"]) <= 1.5
        assert elapsed < 2
        assert longs[0]["allowed_updates"] == longs[1]["allowed_updates"]
    finally:
        await _stop(adapter, app)


@pytest.mark.asyncio
async def test_updates_during_and_after_the_switch_are_delivered_once():
    script = {0: [_update(100)], 101: [_update(101)], 102: []}
    adapter, app, updates = await _adapter_and_app(script)
    try:
        await asyncio.wait_for(
            adapter._start_polling_resilient(
                drop_pending_updates=True,
                error_callback=lambda _error: None,
                require_progress=True,
            ),
            timeout=2,
        )
        seen = []
        for _ in range(50):
            while True:
                try:
                    item = app.update_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                seen.append(item.update_id)
            if 101 in seen and any(call["offset"] == 102 for call in updates.calls):
                break
            await asyncio.sleep(0)
        assert seen == [100, 101]
        offsets = [call["offset"] for call in _long_polls(updates)]
        assert offsets[:3] == [0, 101, 102]
        assert [float(call["timeout"]) for call in _long_polls(updates)[:3]] == [1.0, 10.0, 10.0]
        assert offsets.count(0) == 1
    finally:
        await _stop(adapter, app)


@pytest.mark.asyncio
async def test_reconnect_keeps_the_10s_long_poll():
    script = {0: [], 101: []}
    adapter, app, updates = await _adapter_and_app(script)
    try:
        await asyncio.wait_for(
            adapter._start_polling_resilient(
                drop_pending_updates=True,
                error_callback=lambda _error: None,
                require_progress=True,
            ),
            timeout=2,
        )
        if app.updater.running:
            await app.updater.stop()
        before = len(updates.calls)
        await asyncio.wait_for(
            adapter._start_polling_once(
                app,
                drop_pending_updates=False,
                error_callback=adapter._polling_error_callback_ref,
            ),
            timeout=2,
        )
        for _ in range(50):
            fresh = updates.calls[before:]
            if any((call["timeout"] or 0) >= 10 for call in fresh):
                break
            await asyncio.sleep(0)
        fresh_long = [call for call in updates.calls[before:] if (call["timeout"] or 0) > 1]
        assert fresh_long, "reconnect issued no long poll"
        assert float(fresh_long[0]["timeout"]) == 10
        assert all(float(call["timeout"]) != 1 for call in updates.calls[before:])
    finally:
        await _stop(adapter, app)


@pytest.mark.asyncio
async def test_failure_before_first_success_still_raises_and_does_not_tight_loop():
    adapter, app, updates = await _adapter_and_app({0: []}, fail_first=True)
    try:
        with pytest.raises(OSError):
            await asyncio.wait_for(
                adapter._start_polling_resilient(
                    drop_pending_updates=True,
                    error_callback=lambda _error: None,
                    require_progress=True,
                ),
                timeout=2,
            )
        assert not adapter._polling_progress_event.is_set()
        longs = _long_polls(updates)
        assert longs, "cold start never called getUpdates"
        assert float(longs[0]["timeout"]) == 1
        assert all(float(call["timeout"]) == 10 for call in longs[1:])
    finally:
        await _stop(adapter, app)
