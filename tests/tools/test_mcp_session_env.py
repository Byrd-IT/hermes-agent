"""``mcp_servers.<name>.session_env``: a session whose client sent a declared variable calls its own
stdio connection, rendered with that value; the shared connection and other sessions never see it."""
import os
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
