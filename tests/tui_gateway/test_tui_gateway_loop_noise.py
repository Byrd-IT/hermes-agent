"""Tests for tui_gateway.loop_noise — the WS peer-hangup teardown filter (#50005)."""

from __future__ import annotations

import asyncio

import pytest
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
from websockets.frames import Close

from tui_gateway.loop_noise import (
    _is_benign_teardown,
    _is_benign_ws_shield_close,
    install_loop_noise_filter,
)


class _FakeConnectionLostCallback:
    """Stand-in whose repr matches asyncio's ``_call_connection_lost`` flood."""

    def __repr__(self) -> str:
        return "<Handle _ProactorBasePipeTransport._call_connection_lost(None)>"


def test_benign_teardown_matches_reset_in_connection_lost():
    ctx = {
        "exception": ConnectionResetError(10054, "forcibly closed"),
        "handle": _FakeConnectionLostCallback(),
    }
    assert _is_benign_teardown(ctx) is True


def test_benign_teardown_matches_aborted_and_broken_pipe():
    for exc in (
        ConnectionAbortedError(10053, "aborted"),
        BrokenPipeError("epipe"),
    ):
        ctx = {"exception": exc, "callback": _FakeConnectionLostCallback()}
        assert _is_benign_teardown(ctx) is True


def test_reset_outside_connection_lost_is_not_suppressed():
    # Same error type, but NOT from the connection-lost teardown path — must
    # fall through to the default handler.
    ctx = {
        "exception": ConnectionResetError("reset in a real handler"),
        "handle": "<Handle some_other_handler()>",
    }
    assert _is_benign_teardown(ctx) is False


def test_unrelated_exception_is_not_suppressed():
    ctx = {
        "exception": ValueError("boom"),
        "handle": _FakeConnectionLostCallback(),
    }
    assert _is_benign_teardown(ctx) is False


def test_no_exception_is_not_suppressed():
    assert _is_benign_teardown({"message": "loop warning, no exc"}) is False


def test_install_suppresses_flood_and_forwards_real_errors():
    loop = asyncio.new_event_loop()
    try:
        forwarded: list[dict] = []
        loop.set_exception_handler(lambda _loop, ctx: forwarded.append(ctx))

        install_loop_noise_filter(loop)

        # Benign teardown flood → swallowed, not forwarded.
        loop.call_exception_handler(
            {
                "exception": ConnectionResetError(10054, "forcibly closed"),
                "handle": _FakeConnectionLostCallback(),
            }
        )
        assert forwarded == []

        # Genuine loop error → forwarded to the previous handler unchanged.
        real_ctx = {"exception": RuntimeError("genuine loop bug")}
        loop.call_exception_handler(real_ctx)
        assert len(forwarded) == 1
        assert forwarded[0] is real_ctx
    finally:
        loop.close()


# --- CPython 3.14 shield() report of a dropped dashboard WS client (python/cpython#156321) ------



def _shield_ctx(exc: BaseException) -> dict:
    return {"message": f"{exc.__class__.__name__} exception in shielded future", "exception": exc}


def test_shield_keepalive_timeout_1011_is_suppressed():
    # Exact production signature: we sent 1011, never received a close frame.
    exc = ConnectionClosedError(None, Close(1011, "keepalive ping timeout"), None)
    assert _is_benign_ws_shield_close(_shield_ctx(exc)) is True


def test_shield_abnormal_closure_1006_is_suppressed():
    exc = ConnectionClosedError(None, None, None)
    assert _is_benign_ws_shield_close(_shield_ctx(exc)) is True


def test_shield_other_close_code_is_not_suppressed():
    # Both sides exchanged a 1002 protocol error close: a real problem, keep logging it.
    exc = ConnectionClosedError(Close(1002, "protocol error"), Close(1002, "protocol error"), True)
    assert _is_benign_ws_shield_close(_shield_ctx(exc)) is False


def test_shield_non_websockets_exception_is_not_suppressed():
    assert _is_benign_ws_shield_close(_shield_ctx(RuntimeError("real bug"))) is False


def test_connection_closed_outside_shield_message_is_not_suppressed():
    exc = ConnectionClosedError(None, Close(1011, "keepalive ping timeout"), None)
    ctx = {"message": "Task exception was never retrieved", "exception": exc}
    assert _is_benign_ws_shield_close(ctx) is False


def test_shield_message_without_exception_is_not_suppressed():
    assert _is_benign_ws_shield_close({"message": "X exception in shielded future"}) is False


def test_shield_close_does_not_touch_deprecated_code_property(recwarn):
    exc = ConnectionClosedOK(Close(1000, ""), Close(1000, ""), True)
    assert _is_benign_ws_shield_close(_shield_ctx(exc)) is False
    assert not [w for w in recwarn if issubclass(w.category, DeprecationWarning)]


def test_install_suppresses_shield_close_and_forwards_other_shield_errors():
    loop = asyncio.new_event_loop()
    try:
        forwarded: list[dict] = []
        loop.set_exception_handler(lambda _loop, ctx: forwarded.append(ctx))
        install_loop_noise_filter(loop)

        loop.call_exception_handler(
            _shield_ctx(ConnectionClosedError(None, Close(1011, "keepalive ping timeout"), None))
        )
        assert forwarded == []

        real_ctx = _shield_ctx(ValueError("genuine shielded failure"))
        loop.call_exception_handler(real_ctx)
        assert forwarded == [real_ctx]
    finally:
        loop.close()


def test_uvicorn_legacy_keepalive_timeout_end_to_end():
    """Real uvicorn websockets.legacy server + a client that never pongs, then vanishes.

    On CPython 3.14 the unfiltered loop gets 'ConnectionClosedError exception in shielded
    future'; with the filter installed nothing reaches the previous handler on any version.
    """
    import base64
    import os
    import socket

    uvicorn = pytest.importorskip("uvicorn")

    async def app(scope, receive, send):
        if scope["type"] != "websocket":
            return
        await receive()
        await send({"type": "websocket.accept"})
        while (await receive())["type"] != "websocket.disconnect":
            pass

    async def run(with_filter: bool) -> list[dict]:
        forwarded: list[dict] = []
        loop = asyncio.get_running_loop()
        loop.set_exception_handler(lambda _l, ctx: forwarded.append(ctx))
        if with_filter:
            install_loop_noise_filter(loop)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        server = uvicorn.Server(uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="warning", ws="websockets",
            ws_ping_interval=0.2, ws_ping_timeout=0.2, lifespan="off"))
        serve = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.02)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        key = base64.b64encode(os.urandom(16)).decode()
        writer.write((
            f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        await writer.drain()
        assert b" 101 " in await reader.readuntil(b"\r\n\r\n")
        await asyncio.sleep(1.0)  # suspended client: never pongs
        writer.transport.abort()
        await asyncio.sleep(0.5)
        server.should_exit = True
        await serve
        return forwarded

    import sys

    unfiltered = asyncio.run(run(False))
    shield_noise = [c for c in unfiltered if _is_benign_ws_shield_close(c)]
    if sys.version_info >= (3, 14):
        assert shield_noise, "expected the CPython 3.14 shield report to reproduce"
    assert asyncio.run(run(True)) == []
