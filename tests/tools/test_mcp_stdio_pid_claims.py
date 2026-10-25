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
