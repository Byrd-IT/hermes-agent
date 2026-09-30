"""Suppress benign event-loop teardown noise on the gateway serving loop.

When the Desktop client forcibly closes its WebSocket while the gateway still has pending socket
operations, asyncio logs a traceback per pending ``_call_connection_lost`` callback —
``ConnectionResetError`` (WinError 10054), ``ConnectionAbortedError`` (10053) or ``BrokenPipeError``
(POSIX); one disconnect can emit 50+. They are the expected side effect of the peer hanging up
before our writes drained, so the handler here collapses exactly that class to one debug line and
forwards everything else to the previous handler unchanged.

It also collapses the CPython 3.14 ``asyncio.shield()`` report (python/cpython#156321) of a
websockets ``ConnectionClosed`` 1011/1006: a sleeping or dropped dashboard client misses a
keepalive pong, websockets.legacy fails the connection and handles the error, yet 3.14 still
hands it to the loop's exception handler as "... exception in shielded future".
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

_log = logging.getLogger(__name__)

# Connection-teardown errors that mean "the peer hung up mid-write".
_BENIGN_TEARDOWN_ERRORS = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)


def _is_benign_teardown(context: dict[str, Any]) -> bool:
    """True when the loop error is a peer-hangup during transport teardown.

    Gated on BOTH the exception type AND the ``_call_connection_lost`` callback (matched
    on repr) so the same error type raised elsewhere still reaches the default handler.
    """
    if not isinstance(context.get("exception"), _BENIGN_TEARDOWN_ERRORS):
        return False
    marker = "_call_connection_lost"
    return marker in repr(context.get("callback")) or marker in repr(context.get("handle"))


# 1011 = our side failed the connection (legacy keepalive "ping timeout"); 1006 = abnormal closure,
# i.e. no close frame received (websockets reports that when ``rcvd`` is None).
_BENIGN_WS_CLOSE_CODES = frozenset({1006, 1011})
_SHIELDED_FUTURE_SUFFIX = "exception in shielded future"


def _ws_close_codes(exc: BaseException) -> set[int]:
    """Close codes carried by a websockets ``ConnectionClosed`` without the deprecated ``.code``."""
    codes: set[int] = set()
    rcvd, sent = getattr(exc, "rcvd", None), getattr(exc, "sent", None)
    if rcvd is None:
        codes.add(1006)
    for frame in (rcvd, sent):
        code = getattr(frame, "code", None)
        if code is not None:
            with contextlib.suppress(TypeError, ValueError):
                codes.add(int(code))
    return codes


def _is_benign_ws_shield_close(context: dict[str, Any]) -> bool:
    """True for a dropped/sleeping WS client surfacing via CPython 3.14's shield() logging.

    CPython 3.14 (python/cpython#156321) makes ``asyncio.shield()`` report an exception to the
    loop handler even when the awaiting code already handled it, so websockets.legacy's
    ``close_connection`` turns every keepalive-ping timeout into an ERROR traceback. Gated on the
    message suffix AND a websockets ``ConnectionClosed`` AND close code 1011/1006, so any other
    shielded-future error (or another close code) still reaches the default handler.
    """
    if not str(context.get("message") or "").endswith(_SHIELDED_FUTURE_SUFFIX):
        return False
    exc = context.get("exception")
    if exc is None:
        return False
    try:
        from websockets.exceptions import ConnectionClosed
    except Exception:  # pragma: no cover - websockets is a hard dependency of the dashboard
        return False
    if not isinstance(exc, ConnectionClosed):
        return False
    return bool(_ws_close_codes(exc) & _BENIGN_WS_CLOSE_CODES)


def install_loop_noise_filter(loop: asyncio.AbstractEventLoop) -> None:
    """Chain a teardown-noise filter ahead of the loop's existing handler.

    Idempotent: a loop already carrying the filter is left alone, so it's safe to call
    on every reconnect/serve entry without stacking handlers.
    """
    if getattr(loop, "_hermes_noise_filter_installed", False):
        return
    previous = loop.get_exception_handler()

    def _handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        if _is_benign_teardown(context):
            _log.debug("ws peer hangup during teardown (suppressed): %s", context.get("exception"))
            return
        if _is_benign_ws_shield_close(context):
            _log.debug("ws client dropped, shielded close (suppressed): %r", context.get("exception"))
            return
        if previous is not None:
            previous(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(_handler)
    with contextlib.suppress(AttributeError, TypeError):  # pragma: no cover - exotic loop impls
        loop._hermes_noise_filter_installed = True  # type: ignore[attr-defined]
