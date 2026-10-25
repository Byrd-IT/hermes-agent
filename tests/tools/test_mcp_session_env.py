"""``mcp_servers.<name>.session_env``: a session whose client sent a declared variable calls its own
stdio connection, rendered with that value; the shared connection and other sessions never see it."""
import os
import time
import sys
from unittest.mock import patch

SERVER = '''import json, os, sys
for line in sys.stdin:
 r=json.loads(line); method=r.get('method'); ident=r.get('id')
 if ident is None: continue
 if method=='initialize': result={'protocolVersion':r['params']['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'pin','version':'1'}}
 elif method=='tools/list': result={'tools':[{'name':'who','description':'echo pin','inputSchema':{'type':'object','properties':{}}}]}
 elif method=='tools/call': result={'content':[{'type':'text','text':'pin=%s pid=%d' % (os.environ.get('PIN','UNSET'), os.getpid())}]}
 else: result={}
 print(json.dumps({'jsonrpc':'2.0','id':ident,'result':result}),flush=True)
'''


def _call(tool, env):
    from agent.session_client_env import client_env_scope
    from tools.registry import registry
    with client_env_scope(env):
        out = registry.dispatch(tool, {})
    return out


def test_session_env_gets_own_connection_and_shared_one_is_untouched(tmp_path):
    from tools.mcp_tool_discovery import register_mcp_servers
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    from tools.registry import registry

    script = tmp_path / "pin.py"
    script.write_text(SERVER)
    cfg = {"command": sys.executable, "args": [str(script)], "env": {"PIN": "${TEST_RUN_PIN}"},
           "session_env": ["TEST_RUN_PIN"], "connect_timeout": 10}
    env = {k: v for k, v in os.environ.items() if k != "TEST_RUN_PIN"}
    try:
        with patch.dict(os.environ, env, clear=True):
            from tools.mcp_tool_config import _interpolate_env_vars
            register_mcp_servers({"pinsrv": _interpolate_env_vars(cfg)})
            tool = next(n for n in registry.get_all_tool_names() if n.endswith("who"))
            shared = registry.dispatch(tool, {})
            a1 = _call(tool, {"TEST_RUN_PIN": "slug-a"})
            a2 = _call(tool, {"TEST_RUN_PIN": "slug-a"})
            b = _call(tool, {"TEST_RUN_PIN": "slug-b"})
            other = _call(tool, {"UNRELATED": "x"})
            shared_again = registry.dispatch(tool, {})
        assert "pin=UNSET" in shared and "pin=UNSET" in other and "pin=UNSET" in shared_again
        assert "pin=slug-a" in a1 and "pin=slug-a" in a2 and "pin=slug-b" in b
        pid = lambda s: s.split("pid=")[1].split()[0].strip('"}\\')
        assert pid(a1) == pid(a2) != pid(b) and pid(shared) == pid(shared_again) != pid(a1)
        # The variants register nothing of their own.
        assert sum(n.endswith("who") for n in registry.get_all_tool_names()) == 1
    finally:
        shutdown_mcp_servers()


def test_client_allowlist_carries_declared_names_but_never_secrets():
    from agent.session_client_env import collect_client_env

    config = {"mcp_servers": {"s": {"session_env": ["RUN_PIN", "RUN_API_TOKEN"]}}}
    sent = collect_client_env({"RUN_PIN": "x", "RUN_API_TOKEN": "t", "OTHER": "y"}, config)
    assert sent == {"RUN_PIN": "x"}


def test_undeclared_server_ignores_client_env(tmp_path):
    from tools.mcp_tool_scope import _resolve_server_key
    from agent.session_client_env import client_env_scope

    with client_env_scope({"TEST_RUN_PIN": "z"}):
        assert _resolve_server_key("nothing-registered") == "nothing-registered"


def _setup(tmp_path, template="${TEST_RUN_PIN}"):
    from tools.mcp_tool_config import _interpolate_env_vars
    from tools.mcp_tool_discovery import register_mcp_servers
    from tools.registry import registry
    script = tmp_path / "pin.py"
    script.write_text(SERVER)
    cfg = {"command": sys.executable, "args": [str(script)], "env": {"PIN": template},
           "session_env": ["TEST_RUN_PIN"], "connect_timeout": 10}
    register_mcp_servers({"pinsrv": _interpolate_env_vars(cfg)})
    return next(n for n in registry.get_all_tool_names() if n.endswith("who"))


def _clean_env():
    return {k: v for k, v in os.environ.items() if k not in ("TEST_RUN_PIN", "TEST_OTHER")}


def test_tool_stays_offered_in_and_after_session_env_scope_cold_cache(tmp_path):
    """F1: availability is judged on the shared connection, so a session_env turn's tool list
    keeps the tool and the answer cached for that turn never hides it from shared sessions."""
    from agent.session_client_env import client_env_scope
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    from tools.registry import invalidate_check_fn_cache, registry
    try:
        with patch.dict(os.environ, _clean_env(), clear=True):
            tool = _setup(tmp_path)
            invalidate_check_fn_cache()
            assert [d["function"]["name"] for d in registry.get_definitions({tool})] == [tool]
            invalidate_check_fn_cache()
            with client_env_scope({"TEST_RUN_PIN": "slug-a"}):
                assert [d["function"]["name"] for d in registry.get_definitions({tool})] == [tool]
            assert [d["function"]["name"] for d in registry.get_definitions({tool})] == [tool]
    finally:
        shutdown_mcp_servers()


def test_mixed_template_keeps_override_on_connect(tmp_path):
    """F5: ``${A}-${B}`` with B unset renders with A's override in every pass."""
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    try:
        with patch.dict(os.environ, _clean_env(), clear=True):  # TEST_OTHER stays unset
            tool = _setup(tmp_path, "${TEST_RUN_PIN}-${TEST_OTHER}")
            assert "pin=slug-a-${TEST_OTHER}" in _call(tool, {"TEST_RUN_PIN": "slug-a"})
    finally:
        shutdown_mcp_servers()


def test_lazy_server_first_pinned_call_gets_a_variant_not_the_shared_connect(tmp_path):
    """N1: a lazy session_env server that never connected still resolves to a variant."""
    from agent.session_client_env import client_env_scope
    from tools import mcp_tool_session_env as se
    from tools.mcp_tool_common import _core
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    from tools.mcp_tool_scope import _resolve_server_key
    try:
        with patch.dict(os.environ, _clean_env(), clear=True):
            _core._lazy_server_configs["pinsrv"] = {
                "command": sys.executable, "args": [], "env": {"PIN": "${TEST_RUN_PIN}"},
                "session_env": ["TEST_RUN_PIN"], "lazy": True}
            assert "pinsrv" not in _core._servers
            assert _resolve_server_key("pinsrv") == "pinsrv"
            with client_env_scope({"TEST_RUN_PIN": "slug-a"}):
                assert se.is_variant_key(_resolve_server_key("pinsrv"))
    finally:
        _core._lazy_server_configs.pop("pinsrv", None)
        shutdown_mcp_servers()


def test_closing_variant_forgets_all_bookkeeping(tmp_path):
    """F4: closing forgets all variant bookkeeping (hints are read from the shared key)."""
    from agent.session_client_env import client_env_scope
    from tools import mcp_tool_session_env as se
    from tools.mcp_tool_common import _core
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    try:
        with patch.dict(os.environ, _clean_env(), clear=True):
            tool = _setup(tmp_path)
            base = next(k for k in _core._servers if not se.is_variant_key(k))
            _core._tool_read_only_hints[base] = {"who": True}
            _call(tool, {"TEST_RUN_PIN": "slug-a"})
            (vkey,) = [k for k in _core._servers if se.is_variant_key(k)]
            assert vkey not in _core._tool_read_only_hints
            assert vkey in se._pending and vkey in se._last_used
        # a key that never connects is forgotten by the reaper once idle
        se._pending[("x#env:dead", "pinsrv")] = (base, {})
        se._last_used[("x#env:dead", "pinsrv")] = 0.0
        se.reap_idle(now=se._IDLE_TTL_SEC + 5)
        assert ("x#env:dead", "pinsrv") not in se._pending
    finally:
        shutdown_mcp_servers()
    assert not se._pending and not se._last_used and not se._connect_locks
    assert not [k for k in _core._tool_read_only_hints if se.is_variant_key(k)]


def test_idle_reaper_closes_idle_variants_without_a_new_connect(tmp_path):
    """F3: an idle variant is closed by the reaper, not only when another variant connects."""
    from tools import mcp_tool_session_env as se
    from tools.mcp_tool_common import _core
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    try:
        with patch.dict(os.environ, _clean_env(), clear=True):
            tool = _setup(tmp_path)
            _call(tool, {"TEST_RUN_PIN": "slug-a"})
            assert [k for k in _core._servers if se.is_variant_key(k)]
            assert se._reaper is not None and se._reaper.is_alive()
            assert se.reap_idle(now=time.monotonic() + 5) == 0  # fresh: kept
            assert se.reap_idle(now=time.monotonic() + se._IDLE_TTL_SEC + 5) == 1
            assert not [k for k in _core._servers if se.is_variant_key(k)]
            assert any(not se.is_variant_key(k) for k in _core._servers)  # shared stays
    finally:
        shutdown_mcp_servers()


def test_variant_breaker_strikes_do_not_open_shared_breaker(tmp_path):
    """F9: failures counted on a session_env variant never trip the shared breaker."""
    from agent.session_client_env import client_env_scope
    from tools import mcp_tool_session_env as se
    from tools.mcp_tool_common import _core
    from tools.mcp_tool_handlers import _check_circuit_breaker
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    try:
        with patch.dict(os.environ, _clean_env(), clear=True):
            tool = _setup(tmp_path)
            _call(tool, {"TEST_RUN_PIN": "slug-a"})
            (vkey,) = [k for k in _core._servers if se.is_variant_key(k)]
            _core._server_error_counts[vkey] = _core._CIRCUIT_BREAKER_THRESHOLD + 2
            _core._server_breaker_opened_at[vkey] = time.monotonic()
            with client_env_scope({"TEST_RUN_PIN": "slug-a"}):
                assert _check_circuit_breaker("pinsrv") is not None
            assert _check_circuit_breaker("pinsrv") is None
            assert "pin=UNSET" in __import__("tools.registry", fromlist=["registry"]).registry.dispatch(tool, {})
    finally:
        shutdown_mcp_servers()


def test_read_only_hint_known_on_first_pinned_call_before_variant_connects(tmp_path):
    """The trust gate runs before the variant connects: hints must come from the shared key."""
    from agent.session_client_env import client_env_scope
    from tools import mcp_tool_session_env as se
    from tools.mcp_tool_common import _core
    from tools.mcp_tool_handlers import _tool_is_read_only
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    try:
        with patch.dict(os.environ, _clean_env(), clear=True):
            _setup(tmp_path)
            base = next(k for k in _core._servers if not se.is_variant_key(k))
            _core._tool_read_only_hints[base] = {"who": True}
            with client_env_scope({"TEST_RUN_PIN": "slug-a"}):
                assert not [k for k in _core._servers if se.is_variant_key(k)]
                assert _tool_is_read_only("pinsrv", "who") is True
                assert _tool_is_read_only("pinsrv", "other") is False
    finally:
        shutdown_mcp_servers()


def test_changed_base_config_gets_a_fresh_variant_key(tmp_path):
    from agent.session_client_env import client_env_scope
    from tools import mcp_tool_session_env as se
    from tools.mcp_tool_common import _core
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    from tools.mcp_tool_scope import _resolve_server_key
    try:
        with patch.dict(os.environ, _clean_env(), clear=True):
            _setup(tmp_path)
            base = next(k for k in _core._servers if not se.is_variant_key(k))
            with client_env_scope({"TEST_RUN_PIN": "slug-a"}):
                k1 = _resolve_server_key("pinsrv")
                _core._servers[base]._config = {**_core._servers[base]._config, "args": ["--changed"]}
                k2 = _resolve_server_key("pinsrv")
                assert se.is_variant_key(k1) and se.is_variant_key(k2) and k1 != k2
    finally:
        shutdown_mcp_servers()


def test_scoped_shutdown_closes_that_scopes_variants(tmp_path):
    from tools import mcp_tool_session_env as se
    from tools.mcp_tool_common import _core
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    try:
        with patch.dict(os.environ, _clean_env(), clear=True):
            tool = _setup(tmp_path)
            base = next(k for k in _core._servers if not se.is_variant_key(k))
            _call(tool, {"TEST_RUN_PIN": "slug-a"})
            assert [k for k in _core._servers if se.is_variant_key(k)]
            assert se.close_for_bases([base]) == 1
            assert not [k for k in _core._servers if se.is_variant_key(k)]
            assert not se._pending and not se._last_used and not se._connect_locks
            assert base in _core._servers  # the shared connection is not touched here
    finally:
        shutdown_mcp_servers()


def test_url_server_ignores_session_env():
    from agent.session_client_env import client_env_scope
    from tools import mcp_tool_session_env as se
    from tools.mcp_tool_common import _core
    from tools.mcp_tool_scope import _resolve_server_key
    try:
        _core._lazy_server_configs["httpsrv"] = {"url": "https://${TEST_RUN_PIN}.example/mcp",
                                                  "session_env": ["TEST_RUN_PIN"], "lazy": True}
        with client_env_scope({"TEST_RUN_PIN": "evil"}):
            assert not se.is_variant_key(_resolve_server_key("httpsrv"))
    finally:
        _core._lazy_server_configs.pop("httpsrv", None)


def test_dropped_variant_session_is_replaced_without_orphaning_the_old_task(tmp_path):
    from agent.session_client_env import client_env_scope
    from tools import mcp_tool_session_env as se
    from tools.mcp_tool_common import _core
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    try:
        with patch.dict(os.environ, _clean_env(), clear=True):
            tool = _setup(tmp_path)
            first = _call(tool, {"TEST_RUN_PIN": "slug-a"})
            (vkey,) = [k for k in _core._servers if se.is_variant_key(k)]
            old = _core._servers[vkey]
            old.session = None  # as after a recycle / transport drop
            second = _call(tool, {"TEST_RUN_PIN": "slug-a"})
            new = _core._servers[vkey]
            assert new is not old and new.session is not None
            assert old.session is None and (old._task is None or old._task.done())
            assert "pin=slug-a" in first and "pin=slug-a" in second
            assert len([k for k in _core._servers if se.is_variant_key(k)]) == 1
    finally:
        shutdown_mcp_servers()


def test_closed_variant_clears_its_breaker_ledgers(tmp_path):
    from tools import mcp_tool_session_env as se
    from tools.mcp_tool_common import _core
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers
    try:
        with patch.dict(os.environ, _clean_env(), clear=True):
            tool = _setup(tmp_path)
            base = next(k for k in _core._servers if not se.is_variant_key(k))
            _call(tool, {"TEST_RUN_PIN": "slug-a"})
            (vkey,) = [k for k in _core._servers if se.is_variant_key(k)]
            _core._server_error_counts[vkey] = 3
            se.close_for_bases([base])
            assert vkey not in _core._server_error_counts
    finally:
        shutdown_mcp_servers()
