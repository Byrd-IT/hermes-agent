"""Concurrent stdio spawns must not claim each other's children (mid-call orphan kill)."""
import asyncio
from unittest.mock import patch

from tools import mcp_tool_lifecycle as lc
from tools.mcp_tool_transport import MCPServerTransportMixin


class _T(MCPServerTransportMixin):
    def __init__(self, name):
        self.name = name


def _clean(*pids):
    for p in pids:
        for d in (lc._stdio_pids, lc._stdio_pgids, lc._stdio_starttimes, lc._stdio_pid_claims,
                  lc._orphan_stdio_pid_servers):
            d.pop(p, None)
        lc._orphan_stdio_pids.discard(p)


def test_shared_pid_is_not_orphaned_until_last_claim_drops():
    pid = 987654
    a, b = _T("a"), _T("b")
    try:
        with patch("hermes_cli.process_identity.register_child", create=True), \
             patch("tools.mcp_tool_transport._leader_start_time", return_value=None), \
             patch("tools.mcp_tool_transport.os.getpgid", return_value=pid), \
             patch("gateway.status._pid_exists", return_value=True):
            a._track_spawned_children({pid})
            b._track_spawned_children({pid})
            a._release_spawned_children({pid})
            # b still owns it: the orphan sweep must not see it.
            assert pid in lc._stdio_pids
            assert pid not in lc._orphan_stdio_pids
            b._release_spawned_children({pid})
            assert pid not in lc._stdio_pids
            assert pid in lc._orphan_stdio_pids
    finally:
        _clean(pid)


def test_spawn_lock_is_one_lock_per_loop_and_serializes_windows():
    async def main():
        lock = lc._stdio_spawn_lock()
        assert lock is lc._stdio_spawn_lock()
        order = []

        async def window(tag):
            async with lock:
                order.append(f"in-{tag}")
                await asyncio.sleep(0)
                order.append(f"out-{tag}")

        await asyncio.gather(window(1), window(2))
        return order

    order = asyncio.run(main())
    assert order in (["in-1", "out-1", "in-2", "out-2"], ["in-2", "out-2", "in-1", "out-1"])


def test_attribution_prefers_the_launched_command():
    with patch.object(lc, "_filter_mcp_children", side_effect=lambda p: set(p)):
        class P:
            def __init__(self, pid):
                self.pid = pid

            def cmdline(self):
                return {1: ["node", "srv.js", "--a"], 2: ["node", "other.js", "--b"]}[self.pid]

        with patch("psutil.Process", side_effect=P):
            assert lc._attribute_spawned_children({1, 2}, "node", ["srv.js", "--a"]) == {1}


def test_run_stdio_serializes_snapshot_windows():
    """Two concurrent _run_stdio spawns must each ledger only their own child."""
    from contextlib import asynccontextmanager
    from tools import mcp_tool as _mt
    from tools import mcp_tool_transport as tr

    children: set = set()
    seen: dict = {}
    entered: list = []
    both = None

    @asynccontextmanager
    async def fake_stdio_client(params, errlog=None):
        children.add(params.args[0])  # "pid" == the one arg
        entered.append(1)
        if len(entered) == 2:
            both.set()
        try:  # unlocked siblings both enter and see each other; locked ones time out alone
            await asyncio.wait_for(both.wait(), 0.3)
        except asyncio.TimeoutError:
            pass
        yield object(), object()

    class _Srv(MCPServerTransportMixin):
        def __init__(self, name):
            self.name = name
            self._stdio_child_pids = set()

        def _track_spawned_children(self, pids):
            seen[self.name] = set(pids)

        def _release_spawned_children(self, pids):
            pass

        def _session_kwargs(self):
            return {}

        async def _serve_session(self, *a, **k):
            return None

    class _Sess:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    async def preflight(name, command, args):
        return command, args

    class _Tee:
        sink = None

        def __init__(self, *a):
            pass

        def close(self, timeout=None):
            return None

    async def main():
        nonlocal both
        both = asyncio.Event()
        a, b = _Srv("a"), _Srv("b")
        cfg = lambda pid: {"command": "node", "args": [pid]}
        await asyncio.gather(a._run_stdio(cfg(101)), b._run_stdio(cfg(102)))

    with patch.object(_mt, "_ensure_mcp_sdk", return_value=True), \
         patch.object(tr, "_connect_inputs", side_effect=lambda n, c: ((c["command"], None, None), None)), \
         patch.object(_mt, "_preflight_stdio_command", preflight), \
         patch.object(_mt, "StdioServerParameters", lambda **kw: type("P", (), kw)()), \
         patch.object(_mt, "stdio_client", fake_stdio_client), \
         patch.object(_mt, "ClientSession", _Sess), \
         patch.object(tr._config, "_write_stderr_log_header"), \
         patch.object(tr._config, "_StderrTee", _Tee), \
         patch.object(tr._config, "_get_mcp_stderr_log", return_value=None), \
         patch.object(lc, "_kill_orphaned_mcp_children"), \
         patch.object(lc, "_snapshot_child_pids", side_effect=lambda: set(children)), \
         patch.object(lc, "_attribute_spawned_children", side_effect=lambda p, c, a: set(p)):
        asyncio.run(main())

    assert seen == {"a": {101}, "b": {102}}


def test_orphan_sweep_skips_pid_still_owned_then_reaps_after_release():
    pid = 987655
    try:
        lc._stdio_pids[pid] = "live"
        lc._orphan_stdio_pids.add(pid)
        lc._orphan_stdio_pid_servers[pid] = "dead"
        with patch.object(lc, "_signal_mcp_process") as sig:
            lc._kill_orphaned_mcp_children()
            assert not sig.called
        assert pid not in lc._orphan_stdio_pids and pid in lc._stdio_pids
        pids, _, _ = (lc._take_reapable_pids(False, None))
        assert pid not in pids
        # owner releases -> re-orphaned -> now reapable
        lc._stdio_pids.pop(pid)
        lc._orphan_stdio_pids.add(pid)
        pids, _, _ = lc._take_reapable_pids(False, None)
        assert pid in pids
    finally:
        _clean(pid)
