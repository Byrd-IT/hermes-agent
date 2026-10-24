"""Per-session stdio connections for MCP servers that declare ``session_env``.

A shell client (``hermes chat`` oneshot) does not run its own MCP servers: the gateway that runs
the turn owns one shared connection per profile server, spawned once from the *gateway's*
environment. A server whose launch env is chosen per run (e.g. ``env: {PIN: ${RUN_PIN}}``) would
therefore never see the client's value. ``mcp_servers.<name>.session_env: [RUN_PIN, ...]`` opts a
server in: the named, non-secret variables travel as the session's ``client_env`` (see
``agent.session_client_env``), and a turn that carries any of them calls its OWN connection,
spawned from the server's templates re-rendered with those values and keyed by their digest.
The shared connection is untouched, other runs never see this one, and its breaker, trust and
hint ledgers are its own. Variant connections register no tools (the profile's overlay already
has them), live in ``_servers`` under a ``(<scope>#env:<digest>, name)`` key and are closed when
idle (``_IDLE_TTL_SEC``) or past ``_MAX_VARIANTS`` (least recently used first).
"""
from __future__ import annotations

import hashlib
import json
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
_guard = threading.Lock()


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


def variant_key(base_key):
    """The variant key a call from the bound session must use for *base_key*, else None (not
    opted in, no bound client env, or the client sent none of the declared names)."""
    from agent.session_client_env import bound_client_env
    env = bound_client_env()
    if not env or is_variant_key(base_key):
        return None
    server = _core._servers.get(base_key)
    names = declared_names(getattr(server, "_config", None))
    values = {n: env[n] for n in names if isinstance(env.get(n), str) and env[n] != ""}
    if not values:
        return None
    digest = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()[:16]
    scope, name = (base_key if isinstance(base_key, tuple) else (None, base_key))
    key = (f"{scope or ''}{MARK}{digest}", name)
    with _guard:
        _pending[key] = (base_key, values)
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


def _evict(now: float) -> list:
    """Variant servers to close: idle past the TTL, then the least recently used beyond the cap."""
    with _guard:
        live = [k for k in _last_used if k in _core._servers]
        stale = [k for k in live if now - _last_used[k] > _IDLE_TTL_SEC]
        rest = sorted((k for k in live if k not in stale), key=_last_used.get)
        stale += rest[:max(0, len(rest) - _MAX_VARIANTS + 1)]
        return stale


def _close(keys) -> None:
    if not keys:
        return
    with _core._lock:
        servers = [(k, _core._servers.pop(k, None)) for k in keys]
        for k in keys:
            _core._server_scope_keys.pop(k, None)
    for k in keys:
        with _guard:
            _pending.pop(k, None)
            _last_used.pop(k, None)
            _connect_locks.pop(k, None)
    from tools import mcp_tool_loop as _loop

    async def _shutdown():
        for _, server in servers:
            if server is not None:
                try:
                    await server.shutdown()
                except Exception:
                    logger.debug("MCP session-env variant shutdown failed", exc_info=True)
    try:
        _loop._run_on_mcp_loop(_shutdown, timeout=20)
    except Exception:
        logger.debug("MCP session-env variant close could not reach the loop", exc_info=True)


def ensure_connected(server_name: str, key) -> Optional[object]:
    """The live variant server under *key*, connecting it first (blocking) when needed."""
    from tools import mcp_tool_loop as _loop
    from tools.mcp_tool_discovery import _connect_server

    with _guard:
        _last_used[key] = time.monotonic()
        lock = _connect_locks.setdefault(key, threading.Lock())
    with lock:
        server = _core._servers.get(key)
        if server is not None and server.session is not None:
            return server
        with _guard:
            pending = _pending.get(key)
        base = _core._servers.get(pending[0]) if pending else None
        if base is None:
            return server
        _close(_evict(time.monotonic()))
        config = _render(base._config, pending[1])
        # The server never sees the values in a log: only the declared names and the digest.
        logger.info("MCP server '%s': starting a per-session connection (session_env %s, id %s)",
                    server_name, ",".join(sorted(pending[1])), key[0].rsplit(MARK, 1)[-1])
        _loop._ensure_mcp_loop()

        async def _go():
            claimed: list = []
            token = _core._connect_server_claim.set(lambda s: (setattr(s, "_session_variant", True),
                                                               claimed.append(s)))
            try:
                return await _core_wait(_connect_server(server_name, config), config)
            except BaseException:
                if claimed:
                    try:
                        await claimed[0].shutdown()
                    except Exception:
                        pass
                raise
            finally:
                _core._connect_server_claim.reset(token)
        try:
            server = _loop._run_on_mcp_loop(
                _go, timeout=float(config.get("connect_timeout", _core._DEFAULT_CONNECT_TIMEOUT)) + 30.0)
        except BaseException as exc:
            logger.warning("MCP server '%s': per-session connection failed (%s)", server_name, type(exc).__name__)
            return None
        with _core._lock:
            _core._servers[key] = server
            _core._server_scope_keys[key] = key[0]
        return server


async def _core_wait(coro, config):
    import asyncio
    return await asyncio.wait_for(coro, timeout=config.get("connect_timeout", _core._DEFAULT_CONNECT_TIMEOUT))
