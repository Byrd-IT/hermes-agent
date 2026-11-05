"""Per-session stdio connections for MCP servers that declare ``session_env``.

A shell client (``hermes chat`` oneshot) does not run its own MCP servers: the gateway that runs
the turn owns one shared connection per profile server, spawned once from the *gateway's*
environment. A server whose launch env is chosen per run (e.g. ``env: {PIN: ${RUN_PIN}}``) would
therefore never see the client's value. ``mcp_servers.<name>.session_env: [RUN_PIN, ...]`` opts a
server in: the named, non-secret variables travel as the session's ``client_env`` (see
``agent.session_client_env``; they are therefore merged into the terminal's
``client_env_passthrough`` allowlist, so a declared name also reaches terminal tools), and a turn that carries any of them calls its OWN connection,
spawned from the server's templates re-rendered with those values and keyed by their digest.
The shared connection is untouched, other runs never see this one, and its circuit breaker is its own
(trust policy and read-only hints stay the profile server's). Variant connections register no tools (the profile's overlay already
has them), live in ``_servers`` under a ``(<scope>#env:<digest>, name)`` key and are closed when
idle (``_IDLE_TTL_SEC``) or past ``_MAX_VARIANTS`` (least recently used first).
"""
from __future__ import annotations

import asyncio
import itertools
from contextvars import ContextVar
from functools import wraps
import hmac
import json
import os
import logging
import threading
import time
from typing import Optional

from tools.mcp_tool_common import _core

logger = logging.getLogger("tools.mcp_tool")

MARK = "#env:"
_IDLE_TTL_SEC = 900.0
_MAX_VARIANTS = 16

_pending: dict = {}      # variant key -> (base key, {name: value}); values stay in memory only
_last_used: dict = {}    # variant key -> monotonic time
_connect_locks: dict = {}
_call_leases: dict = {}
_base_epochs: dict = {}
_epoch_sequence = itertools.count()
_bound_keys: ContextVar[dict] = ContextVar("mcp_variant_call_keys", default={})
_retry_after: dict = {}  # variant key -> monotonic deadline after a failed connect (no respawn storm)
_CONNECT_RETRY_COOLDOWN_SEC = 30.0
_guard = threading.Lock()
_LOG_SALT = os.urandom(16)  # per-process: a logged id cannot be brute-forced offline
# Process key: a leaked variant id is not an offline preimage of session values.
_DIGEST_SECRET = os.urandom(32)
_REAP_INTERVAL_SEC = 60.0
_reaper_stop = threading.Event()
_reaper: Optional[threading.Thread] = None


def reset() -> None:
    """Forget all variant bookkeeping and stop the reaper (``shutdown_mcp_servers`` wildcard)."""
    global _reaper
    with _core._lock, _guard:
        _base_epochs.clear()
        _reaper_stop.set()
        _pending.clear()
        _last_used.clear()
        _connect_locks.clear()
        _retry_after.clear()
        _reaper = None
    for k in [k for k in _core._tool_read_only_hints if is_variant_key(k)]:
        _core._tool_read_only_hints.pop(k, None)
    for name in ("_server_error_counts", "_server_breaker_opened_at", "_server_errors_all_application",
                 "_server_connect_errors"):
        ledger = getattr(_core, name, None)
        if isinstance(ledger, dict):
            for k in [k for k in ledger if is_variant_key(k)]:
                ledger.pop(k, None)
    with _FENCED_LOCK:
        _FENCED_CLOSES.clear()


def _forget_ledgers(key) -> None:
    """Drop the breaker/error ledgers of a variant key (a re-created variant starts clean)."""
    _retry_after.pop(key, None)
    for name in ("_server_error_counts", "_server_breaker_opened_at", "_server_errors_all_application",
                 "_server_connect_errors"):
        ledger = getattr(_core, name, None)
        if isinstance(ledger, dict):
            ledger.pop(key, None)


def close_for_bases(base_keys) -> int:
    """Close every live variant whose base connection is in *base_keys* (scoped shutdown, config
    reload or eviction of the base). Returns the closed count."""
    bases = set(base_keys)
    if not bases:
        return 0
    return _close((), base_keys=bases)


def _variant_busy(key) -> bool:
    """True when a call is in flight. Eviction and the idle reaper must not close it."""
    if _call_leases.get(key, 0):
        return True
    server = _core._servers.get(key)
    if server is None:
        return False
    lock = getattr(server, "_rpc_lock", None)
    if lock is not None and getattr(lock, "locked", lambda: False)():
        return True
    tasks = tuple(getattr(server, "_inflight_tasks", ()) or ())
    return any(not t.done() for t in tasks)


def reap_idle(now: Optional[float] = None) -> int:
    """Close variant connections idle past the TTL and forget bookkeeping of keys that never
    connected (or were evicted) and have not been used for the TTL. Returns the closed count."""
    now = time.monotonic() if now is None else now
    with _guard:
        for k in [k for k, t in _retry_after.items() if now >= t]:
            _retry_after.pop(k, None)
        idle = [k for k, t in _last_used.items() if now - t > _IDLE_TTL_SEC]
    connected = [k for k in idle if k in _core._servers]
    live = [k for k in connected if not _variant_busy(k)]
    with _core._lock, _guard:
        for k in idle:
            if (k not in _core._servers and not _variant_busy(k)
                    and now - _last_used.get(k, now) > _IDLE_TTL_SEC):
                _pending.pop(k, None)
                _last_used.pop(k, None)
                _connect_locks.pop(k, None)
    return _close(live, protect_in_use=True, decided_at=now)


def _reaper_loop(stop: threading.Event) -> None:
    while not stop.wait(_REAP_INTERVAL_SEC):
        try:
            reap_idle()
        except Exception:  # health: allow BLE001 -- reaper pass must not die; logged
            logger.debug("MCP session-env idle reaper pass failed")


def _ensure_reaper() -> None:
    global _reaper, _reaper_stop
    with _guard:
        if _reaper is not None and _reaper.is_alive():
            return
        # Never clear the event an older thread still observes after reset.
        _reaper_stop = threading.Event()
        # health: allow HX012 -- the reaper must NOT inherit a request's bound client env or profile scope
        _reaper = threading.Thread(target=_reaper_loop, args=(_reaper_stop,),
                                   name="mcp-session-env-reaper", daemon=True)
        _reaper.start()


def declared_names(config) -> tuple:
    """The ``session_env`` variable names a server config declares (strings only)."""
    raw = config.get("session_env") if isinstance(config, dict) else None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(dict.fromkeys(n.strip() for n in raw if isinstance(n, str) and n.strip()))


def declared_names_in_config(config) -> frozenset:
    """Every ``session_env`` name across a full config's ``mcp_servers`` (client allowlist)."""
    servers = config.get("mcp_servers") if isinstance(config, dict) else None
    if not isinstance(servers, dict):
        return frozenset()
    return frozenset(n for cfg in servers.values() for n in declared_names(cfg))


def is_variant_key(key) -> bool:
    return isinstance(key, tuple) and MARK in key[0]


def _base_config(base_key):
    """The base server's config: its live connection's, else a not-yet-connected lazy server's
    (so the first pinned call builds a variant instead of waking the shared connection)."""
    server = _core._servers.get(base_key)
    config = getattr(server, "_config", None)
    if config is None:
        config = _core._lazy_server_configs.get(base_key)
    return config


def variant_key(base_key):
    """The variant key a call from the bound session must use for *base_key*, else None (not
    opted in, no bound client env, or the client sent none of the declared names)."""
    bound = _bound_keys.get()
    if base_key in bound:
        return bound[base_key]
    from agent.session_client_env import bound_client_env
    env = bound_client_env()
    if not env or is_variant_key(base_key):
        return None
    base = _base_config(base_key)
    if isinstance(base, dict) and base.get("url"):
        return None  # stdio only: a client value must never choose the host that receives the profile's headers
    names = declared_names(base)
    values = {n: env[n] for n in names if isinstance(env.get(n), str) and env[n] != ""}
    if not values:
        return None
    # The base config's identity is part of the digest: editing the server's command/args/env makes
    # the next call use a fresh variant instead of the one still running the old config.
    ident = json.dumps([values, _plain(_base_config(base_key))], sort_keys=True, default=str)
    scope, name = (base_key if isinstance(base_key, tuple) else (None, base_key))
    with _guard:
        if base_key not in _base_epochs:
            _base_epochs[base_key] = next(_epoch_sequence)
        # Same preimage as before (epoch + values + plain config), but keyed so the id
        # embedded in the server key cannot be brute-forced back to the session values.
        digest = hmac.new(
            _DIGEST_SECRET, f"{_base_epochs[base_key]}:{ident}".encode(), "sha256",
        ).hexdigest()[:16]
        key = (f"{scope or ''}{MARK}{digest}", name)
        _pending.setdefault(key, (base_key, values))
        _last_used[key] = time.monotonic()
    return key


def _plain(value):
    """A rendered config back to its templates, so a re-render starts from the ``${VAR}`` text."""
    if isinstance(value, str):
        return str(getattr(value, "template", value))
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _render(config: dict, values: dict) -> dict:
    from tools import mcp_tool_config as _config
    from tools.mcp_tool_discovery import _owner_secret_scope
    with _owner_secret_scope(), _config.session_env_override(values):
        return _config._interpolate_env_vars(_plain(config))


def _evict(now: float, keep=None) -> list:
    """Variant servers to close: idle past the TTL, then the least recently used beyond the cap.

    A variant with a call in flight is never closed, and neither is *keep* (the key a connect
    is about to use). If every other variant is busy, the count may exceed ``_MAX_VARIANTS``.
    """
    with _guard:
        live = [k for k in _last_used if k in _core._servers]
        closable = [k for k in live if k != keep and not _variant_busy(k)]
        stale = [k for k in closable if now - _last_used[k] > _IDLE_TTL_SEC]
        rest = sorted((k for k in closable if k not in stale), key=lambda k: _last_used[k])
        remaining = len(live) - len(stale)
        extra = max(0, remaining - _MAX_VARIANTS + 1)
        return stale + rest[:extra]


def _close(keys, *, base_keys=None, protect_in_use: bool = False, decided_at: Optional[float] = None) -> int:
    """Close variant servers. With ``protect_in_use``, skip a key that became busy or was
    touched after ``decided_at`` (a call resolved it between the snapshot and this close).
    Returns how many were closed. Shutdown passes neither flag and always closes.
    """
    if not keys and not base_keys:
        return 0
    from tools import mcp_tool_loop as _loop

    async def _shutdown():
        # RPC tasks and teardown run on the same loop. Re-check here, not on
        # the reaper thread before scheduling, or a call may start in between.
        with _core._lock, _guard:
            candidates = ([k for k, (base, _) in _pending.items() if base in base_keys]
                          if base_keys else keys)
            for base in base_keys or ():
                _base_epochs.pop(base, None)
            selected = [k for k in candidates if not protect_in_use or (
                not _variant_busy(k)
                and (decided_at is None or _last_used.get(k, 0) <= decided_at))]
            servers = [(k, _core._servers.pop(k, None)) for k in selected]
            for k in selected:
                _core._server_scope_keys.pop(k, None)
                _pending.pop(k, None)
                _last_used.pop(k, None)
                _connect_locks.pop(k, None)
                _core._tool_read_only_hints.pop(k, None)
                _forget_ledgers(k)
        cancelled = None
        for _, server in servers:
            if server is not None:
                try:
                    await server.shutdown()
                except asyncio.CancelledError as exc:
                    cancelled = exc
                except Exception:  # health: allow BLE001 -- best-effort close, logged without exception payloads
                    logger.debug("MCP session-env variant shutdown failed")
        if cancelled is not None:
            raise cancelled
        return sum(server is not None for _, server in servers)
    try:
        return _loop._run_on_mcp_loop(_shutdown, timeout=20)
    except Exception:  # health: allow BLE001 -- best-effort close, logged
        # A cancelled shutdown surfaces here as concurrent.futures.CancelledError. The on-loop
        # coroutine already re-raised it after attempting every popped server and selection/removal
        # ran on the loop, so sync callers (reload, lazy-forget, eviction) must carry on.
        logger.debug("MCP session-env variant close could not reach the loop")
        return 0


def fenced_error() -> str:
    """The result of a queued variant call whose connection was torn down before it ran."""
    from tools.registry import tool_error
    return tool_error("MCP server is not connected")


def call_is_current(server_name, server) -> bool:
    """A queued variant RPC cannot use a connection removed by explicit teardown."""
    from tools.mcp_tool_scope import _resolve_shared_key
    with _core._lock:
        key = _bound_keys.get().get(_resolve_shared_key(server_name))
        with _guard:
            return key is None or (key in _pending and _core._servers.get(key) is server)


async def _shutdown_discarded(server):
    if server is not None:
        try:
            await server.shutdown()
        except Exception:  # health: allow BLE001 -- cleanup is best-effort, without payloads
            logger.debug("MCP session-env discarded connection shutdown failed")


# Strong refs: the loop keeps only weak ones, and a timed-out waiter must not drop the close.
_FENCED_CLEANUPS: set = set()
_FENCED_CLEANUP_TIMEOUT = 20.0
_FENCED_LOCK = threading.Lock()
_FENCED_CLOSES: list = []


def _release_fenced(task) -> None:
    _FENCED_CLEANUPS.discard(task)
    if not task.cancelled():
        task.exception()  # retrieved; the close already logged its own failure


class _FencedClose:
    """A server shutdown that must outlive the waiter, drain, and loop.stop."""

    __slots__ = ("finished", "scope", "server", "started", "task")

    def __init__(self, server, finished, scope):
        self.server = server
        self.finished = finished
        self.scope = scope
        self.task = None
        self.started = False


def _forget_locked(record) -> None:
    try:
        _FENCED_CLOSES.remove(record)
    except ValueError:
        pass


def _drop_fenced(record) -> None:
    with _FENCED_LOCK:
        _forget_locked(record)


def _fail_fenced(record) -> None:
    record.finished.set()
    _drop_fenced(record)


def _fenced_tasks() -> set:
    """Tasks drain must not cancel: their close has not finished."""
    with _FENCED_LOCK:
        return {r.task for r in _FENCED_CLOSES if r.task is not None and not r.task.done()}


def _close_coro(record):
    from tools import mcp_tool_loop as _loop

    async def _close():
        record.started = True
        try:
            await _shutdown_discarded(record.server)
        finally:
            record.finished.set()
            _drop_fenced(record)

    return _loop._apply_fenced_scope(_close(), record.scope)


def _arm_fenced(record) -> Optional[asyncio.Task]:
    """Start *record*'s close, or return the task already running it. Loop thread only.

    A task cancelled before its first step never sets ``started`` and its coro is
    dead, so that one is replaced. Drain must not be able to drop the server.
    """
    with _FENCED_LOCK:
        running = record.task
        if running is not None and not running.done():
            return running
        if record.finished.is_set() or record.started:
            return None
        coro = _close_coro(record)
        try:
            task = asyncio.ensure_future(coro)
        except RuntimeError:
            coro.close()
            record.finished.set()
            _forget_locked(record)
            logger.debug("MCP session-env fenced connection cleanup failed")
            return None
        record.task = task
    _FENCED_CLEANUPS.add(task)
    task.add_done_callback(_release_fenced)
    return task


async def _finish_pending_fenced(*, timeout: float) -> None:
    """Run closes loop.stop discarded or drain cancelled before they stepped."""
    with _FENCED_LOCK:
        pending = [r for r in _FENCED_CLOSES if not r.finished.is_set()]
    tasks = []
    for record in pending:
        task = _arm_fenced(record)
        if task is not None:
            tasks.append(task)
    if not tasks:
        return
    done, _still = await asyncio.wait(tasks, timeout=timeout)
    for task in done:
        if not task.cancelled():
            task.exception()


def _schedule_fenced_close(server) -> threading.Event:
    """Queue *server*'s close before any cancellable wait.

    ``_run_on_mcp_loop`` cancels its future on timeout or interrupt. A cancel
    that lands before that coroutine is stepped never runs the body, so the
    close cannot live only there. The spawn callback is not that future: once
    queued, giving up on the wait cannot drop it.

    The record is registered before the callback is queued. ``loop.stop`` drops
    a callback that missed the final ready-batch, and drain's ``Task.cancel``
    drops a task that has not stepped. Either way the record can still start
    the close.
    """
    from tools import mcp_tool_loop as _loop

    finished = threading.Event()
    record = _FencedClose(server, finished, _loop._capture_fenced_scope())
    loop = _loop._running_loop()
    if loop is None:
        logger.debug("MCP session-env fenced connection cleanup failed")
        finished.set()
        return finished

    with _FENCED_LOCK:
        _FENCED_CLOSES.append(record)

    def _spawn():
        _arm_fenced(record)

    try:
        loop.call_soon_threadsafe(_spawn)
    except RuntimeError:
        _fail_fenced(record)
        logger.debug("MCP session-env fenced connection cleanup failed")
    return finished


def _wait_fenced(finished: threading.Event) -> None:
    """Wait for a close already queued. Timeout and interrupt do not cancel it."""
    from tools.interrupt import is_interrupted

    deadline = time.monotonic() + _FENCED_CLEANUP_TIMEOUT
    while not finished.is_set():
        if is_interrupted():
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        finished.wait(min(0.1, remaining))


def protect_call(server_name, handler):
    """Lease a variant before lookup and pin its identity until the call returns."""
    @wraps(handler)
    def call(*args, **kwargs):
        from tools.mcp_tool_scope import _resolve_server_key
        with _core._lock:
            key = _resolve_server_key(server_name)
            with _guard:
                leased = is_variant_key(key) and key in _pending
                if leased:
                    _call_leases[key] = _call_leases.get(key, 0) + 1
                    base = _pending[key][0]
                    token = _bound_keys.set({**_bound_keys.get(), base: key})
        try:
            return handler(*args, **kwargs)
        finally:
            if leased:
                _bound_keys.reset(token)
                with _guard:
                    remaining = _call_leases[key] - 1
                    if remaining:
                        _call_leases[key] = remaining
                    else:
                        _call_leases.pop(key)
                    if key in _pending:
                        _last_used[key] = time.monotonic()
    return call


def ensure_connected(server_name: str, key) -> Optional[object]:
    """The live variant server under *key*, connecting it first (blocking) when needed."""
    from tools import mcp_tool_loop as _loop
    from tools.mcp_tool_discovery import _connect_server

    with _guard:
        if key not in _pending:
            return None
        _last_used[key] = time.monotonic()
        lock = _connect_locks.setdefault(key, threading.Lock())
    with lock:
        server = _core._servers.get(key)
        if server is not None and server.session is not None:
            return server
        with _guard:
            retry_at = _retry_after.get(key)
        if retry_at is not None and time.monotonic() < retry_at:
            return server  # a recent connect failed: do not spawn another process yet
        with _guard:
            pending = _pending.get(key)
        base_config = _base_config(pending[0]) if pending else None
        if base_config is None:
            return server
        now = time.monotonic()
        _close(_evict(now, keep=key), protect_in_use=True, decided_at=now)
        config = _render(base_config, pending[1])
        # The server never sees the values in a log: only the declared names and the digest.
        logger.info("MCP server '%s': starting a per-session connection (session_env %s, id %s)",
                    server_name, ",".join(sorted(pending[1])), hmac.new(_LOG_SALT, key[0].encode(), "sha256").hexdigest()[:8])
        _loop._ensure_mcp_loop()

        stale = server  # a variant whose session dropped (recycle, backoff): replaced, never orphaned

        async def _go():
            with _guard:
                if _pending.get(key) is not pending:
                    return None
            if stale is not None:
                try:
                    await stale.shutdown()
                except Exception:  # health: allow BLE001 -- best-effort; the replacement still connects
                    logger.debug("MCP session-env stale variant shutdown failed")
            claimed: list = []
            token = _core._connect_server_claim.set(lambda s: (setattr(s, "_session_variant", True),
                                                               claimed.append(s)))
            try:
                # _connect_server re-renders the config: keep the override on for that pass too, so a
                # mixed template (``${A}-${B}``) keeps the session's value.
                from tools import mcp_tool_config as _config
                with _config.session_env_override(pending[1]):
                    return await _core_wait(_connect_server(server_name, config), config)
            except BaseException:  # health: allow BLE001 -- cleanup boundary, always re-raises
                if claimed:
                    try:
                        await claimed[0].shutdown()
                    except Exception:  # health: allow BLE001 -- best-effort cleanup, logged without exception payloads
                        logger.debug("MCP session-env variant cleanup after a failed connect failed")
                raise
            finally:
                _core._connect_server_claim.reset(token)
        try:
            server = _loop._run_on_mcp_loop(
                _go, timeout=float(config.get("connect_timeout", _core._DEFAULT_CONNECT_TIMEOUT)) + 30.0,
                on_discard=_shutdown_discarded)
        except InterruptedError:
            raise
        except Exception as exc:  # health: allow BLE001 -- failed connects cool down; only the type name is logged
            logger.warning("MCP server '%s': per-session connection failed (%s)", server_name, type(exc).__name__)
            with _guard:
                if _pending.get(key) is pending:
                    _forget_ledgers(key)
                    _retry_after[key] = time.monotonic() + _CONNECT_RETRY_COOLDOWN_SEC
            _ensure_reaper()
            return None
        with _core._lock, _guard:
            current = server is not None and _pending.get(key) is pending
            if current:
                _forget_ledgers(key)
                _core._servers[key] = server
                _core._server_scope_keys[key] = key[0]
        if not current:
            if server is not None:
                try:
                    _wait_fenced(_schedule_fenced_close(server))
                except Exception:  # health: allow BLE001 -- fenced cleanup is best-effort, without payloads
                    logger.debug("MCP session-env fenced connection cleanup failed")
            return None
        _ensure_reaper()
        return server


async def _core_wait(coro, config):
    import asyncio
    return await asyncio.wait_for(coro, timeout=config.get("connect_timeout", _core._DEFAULT_CONNECT_TIMEOUT))
