"""One short getUpdates on a Telegram cold start, then PTB's normal long poll.

PTB 22.8 closes over start_polling(timeout=) for the life of the updater loop.
Restarting the updater to switch 1s to 10s re-runs delete_webhook(drop_pending_updates)
and can drop updates that arrived after the first poll. This module rewrites only
the timeout argument of that first long poll, on PTB's own get_updates. Offset,
allowed_updates and drop_pending_updates are unchanged. Reconnects do not arm it.
"""

from __future__ import annotations

import inspect
import logging
from datetime import timedelta

logger = logging.getLogger("plugins.platforms.telegram.adapter")

_COLD_START_FIRST_POLL_TIMEOUT = 1.0


def _poll_timeout_seconds(value: object) -> float | None:
    """Seconds in a PTB getUpdates timeout (int or timedelta), or None if it is not one."""
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _short_first_poll_timeout(original: object) -> object:
    """Same type PTB passed in, so its read_timeout math stays on the short hold."""
    if isinstance(original, timedelta):
        return timedelta(seconds=_COLD_START_FIRST_POLL_TIMEOUT)
    return _COLD_START_FIRST_POLL_TIMEOUT


def arm_cold_start_short_first_poll(adapter) -> bool:
    """Shorten only the next long getUpdates. Returns False if the bot cannot be wrapped.

    The wrapper changes the timeout argument of PTB's own get_updates call. It does not
    issue a getUpdates of its own and it does not touch offset, allowed_updates, or
    drop_pending_updates. A timeout of 0 (updater.stop cleanup) is left alone so the
    arm stays available for the real first long poll.
    """
    bot = getattr(getattr(adapter, "_app", None), "bot", None)
    if bot is None:
        return False
    original_cls = bot.__class__
    if not inspect.iscoroutinefunction(getattr(original_cls, "get_updates", None)):
        return False
    # A leftover wrap means a previous disarm did not run. Nesting another
    # subclass would hide the class we must restore, so refuse rather than re-arm.
    if getattr(original_cls, "_hermes_cold_start_short_poll", False):
        return False

    class _ColdStartShortPollBot(original_cls):
        __slots__ = ()
        _hermes_cold_start_short_poll = True

        async def get_updates(self, *args, **kwargs):
            timeout = kwargs["timeout"] if "timeout" in kwargs else (args[2] if len(args) > 2 else None)
            if _consume_cold_start_short_poll(adapter, timeout):
                self.__class__ = original_cls
                short = _short_first_poll_timeout(timeout)
                if "timeout" in kwargs or len(args) <= 2:
                    kwargs = {**kwargs, "timeout": short}
                else:
                    args = (*args[:2], short, *args[3:])
                logger.debug(
                    "[%s] Cold-start first getUpdates long poll shortened to %.0fs",
                    adapter.name, _COLD_START_FIRST_POLL_TIMEOUT)
            return await original_cls.get_updates(self, *args, **kwargs)

    try:
        bot.__class__ = _ColdStartShortPollBot
    except TypeError:
        return False
    adapter._cold_start_short_poll_armed = True
    adapter._cold_start_short_poll_bot = bot
    adapter._cold_start_short_poll_original_cls = original_cls
    return True


def _consume_cold_start_short_poll(adapter, timeout: object) -> bool:
    if not getattr(adapter, "_cold_start_short_poll_armed", False):
        return False
    seconds = _poll_timeout_seconds(timeout)
    if seconds is None or seconds <= _COLD_START_FIRST_POLL_TIMEOUT:
        return False
    adapter._cold_start_short_poll_armed = False
    return True


def disarm_cold_start_short_first_poll(adapter) -> None:
    adapter._cold_start_short_poll_armed = False
    bot = getattr(adapter, "_cold_start_short_poll_bot", None)
    original = getattr(adapter, "_cold_start_short_poll_original_cls", None)
    if bot is None or original is None or bot.__class__ is original:
        return
    try:
        bot.__class__ = original
    except TypeError:
        return
