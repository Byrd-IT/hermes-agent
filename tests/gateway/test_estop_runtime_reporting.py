"""The RUNNING gateway truthfully reports what a maintenance updater needs to drain it selectively.

``gateway_state.json`` of the running process carries (a) ``capabilities`` = this process's real
``agent.estop.capabilities()`` (never copied from a CLI, never invented) and (b) ``activity.voice_sessions``
= the number of voice sessions that are genuinely connected right now (``None`` when it cannot be
counted: the updater then fails closed). Nothing here ever reports a zero it did not count.
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from agent import estop
from gateway import status
from gateway.config import Platform
from gateway.run import GatewayRunner


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(status, "_runtime_capabilities_cache", status._UNSET, raising=False)
    monkeypatch.setattr(status, "_runtime_status_state", None)
    monkeypatch.setattr(status, "_runtime_status_state_path", None)
    return tmp_path


def _state(home):
    return json.loads((home / "gateway_state.json").read_text())


# ── capabilities ─────────────────────────────────────────────────────────────


def test_running_gateway_publishes_its_own_estop_capabilities(home):
    assert status.write_runtime_status(gateway_state="running", active_agents=0, wait_timeout=10)
    assert _state(home)["capabilities"] == estop.capabilities()


def test_capabilities_and_activity_are_stamped_with_the_pid_that_made_the_claim(home):
    """An older runtime does not know these keys and leaves them on disk after a rollback: a reader must be
    able to tell that the claim belongs to a dead process."""
    status.write_runtime_status(gateway_state="running", activity={"voice_sessions": 0}, wait_timeout=10)
    rec = _state(home)
    assert rec["capabilities_pid"] == os.getpid() == rec["pid"]
    assert rec["activity"]["pid"] == os.getpid()


def test_every_write_refreshes_capabilities_not_just_lifecycle_writes(home):
    status.write_runtime_status(gateway_state="running", wait_timeout=10)
    path = home / "gateway_state.json"
    rec = json.loads(path.read_text())
    rec.pop("capabilities")
    path.write_text(json.dumps(rec))
    status.write_runtime_status(active_agents=1, reload_existing=True, wait_timeout=10)   # heartbeat-style write
    assert _state(home)["capabilities"] == estop.capabilities()


def test_capabilities_are_omitted_not_invented_when_this_runtime_has_no_scoped_estop(home, monkeypatch):
    def boom():
        raise AttributeError("old runtime")

    monkeypatch.setattr(estop, "capabilities", boom)
    status.write_runtime_status(gateway_state="running", wait_timeout=10)
    assert "capabilities" not in _state(home)


def test_stale_capabilities_from_a_previous_process_are_not_inherited(home, monkeypatch):
    path = home / "gateway_state.json"
    path.write_text(json.dumps({"pid": 1, "gateway_state": "running", "capabilities": {"scoped_estop": 1}}))

    def boom():
        raise AttributeError("old runtime")

    monkeypatch.setattr(estop, "capabilities", boom)
    status.write_runtime_status(gateway_state="running", reload_existing=True, wait_timeout=10)
    assert "capabilities" not in _state(home)


def test_capabilities_are_pinned_at_first_publication_not_recomputed_after_files_change(home, monkeypatch):
    """A process keeps serving the code it started with; a later on-disk change must not upgrade its claim."""
    status.write_runtime_status(gateway_state="running", wait_timeout=10)
    first = _state(home)["capabilities"]
    monkeypatch.setattr(estop, "capabilities", lambda: {"scoped_estop": 99})
    status.write_runtime_status(active_agents=1, wait_timeout=10)
    assert _state(home)["capabilities"] == first


# ── activity record ──────────────────────────────────────────────────────────


def test_activity_voice_sessions_is_written_and_stamped(home):
    status.write_runtime_status(gateway_state="running", activity={"voice_sessions": 2}, wait_timeout=10)
    act = _state(home)["activity"]
    assert act["voice_sessions"] == 2 and isinstance(act["updated_at"], str)


def test_unknown_voice_count_is_stored_as_null_never_zero(home):
    status.write_runtime_status(gateway_state="running", activity={"voice_sessions": None}, wait_timeout=10)
    assert _state(home)["activity"]["voice_sessions"] is None


@pytest.mark.parametrize("bad", [-1, True, "3", 1.5, [], {}])
def test_garbage_voice_counts_are_stored_as_null(home, bad):
    status.write_runtime_status(gateway_state="running", activity={"voice_sessions": bad}, wait_timeout=10)
    assert _state(home)["activity"]["voice_sessions"] is None


def test_activity_of_a_previous_process_is_dropped_on_takeover(home):
    (home / "gateway_state.json").write_text(json.dumps(
        {"pid": 1, "gateway_state": "running", "active_agents": 3, "activity": {"voice_sessions": 4}}))
    status.write_runtime_status(gateway_state="starting", reload_existing=True, wait_timeout=10)
    assert "activity" not in _state(home)


def test_activity_survives_writes_that_do_not_mention_it(home):
    status.write_runtime_status(gateway_state="running", activity={"voice_sessions": 1}, wait_timeout=10)
    status.write_runtime_status(active_agents=2, wait_timeout=10)
    assert _state(home)["activity"]["voice_sessions"] == 1


# ── counting voice sessions ──────────────────────────────────────────────────


class _VC:
    def __init__(self, connected):
        self._c = connected

    def is_connected(self):
        return self._c


class _VoiceAdapter:
    def __init__(self, clients):
        self._voice_clients = clients

    async def join_voice_channel(self, channel):
        return True

    def is_in_voice_channel(self, guild_id):
        vc = self._voice_clients.get(guild_id)
        return vc is not None and vc.is_connected()


def _runner(default=None, profiles=None):
    r = object.__new__(GatewayRunner)
    r.adapters = default or {}
    r._profile_adapters = profiles or {}
    return r


def test_voice_count_is_the_number_of_connected_clients_across_all_profiles():
    r = _runner(
        {Platform.DISCORD: _VoiceAdapter({1: _VC(True), 2: _VC(False), 3: _VC(True)}),
         Platform.TELEGRAM: SimpleNamespace()},
        {"ops": {Platform.DISCORD: _VoiceAdapter({9: _VC(True)})}},
    )
    assert r._active_voice_session_count() == 3


def test_no_voice_capable_adapter_means_a_counted_zero():
    assert _runner({Platform.TELEGRAM: SimpleNamespace()})._active_voice_session_count() == 0
    assert _runner()._active_voice_session_count() == 0


def test_voice_capable_adapter_that_cannot_be_inspected_is_unknown_not_zero():
    no_clients = SimpleNamespace(join_voice_channel=lambda *a: None)
    assert _runner({Platform.DISCORD: no_clients})._active_voice_session_count() is None


def test_voice_adapter_that_raises_is_unknown_not_zero():
    class Broken(_VoiceAdapter):
        def is_in_voice_channel(self, guild_id):
            raise RuntimeError("gateway lib exploded")

    assert _runner({Platform.DISCORD: Broken({1: _VC(True)})})._active_voice_session_count() is None


def test_runtime_activity_reports_the_voice_count():
    r = _runner({Platform.DISCORD: _VoiceAdapter({1: _VC(True)})})
    assert r._runtime_activity() == {"voice_sessions": 1}


# ── when it is published ─────────────────────────────────────────────────────


def _runner_with_state(adapters):
    r = _runner(adapters)
    r._active_work_count = lambda: 0
    r._running_agent_items = list
    return r


def test_turn_boundary_persist_carries_the_voice_count(home, monkeypatch):
    r = _runner_with_state({Platform.DISCORD: _VoiceAdapter({1: _VC(True)})})
    seen = {}
    monkeypatch.setattr("gateway.run._write_runtime_status_quiet", lambda **kw: seen.update(kw))
    r._persist_active_agents()
    assert seen["activity"] == {"voice_sessions": 1} and seen["active_agents"] == 0


def test_housekeeping_heartbeat_republishes_activity_every_tick(monkeypatch):
    from gateway import run as run_mod

    r = _runner({Platform.DISCORD: _VoiceAdapter({1: _VC(True), 2: _VC(True)})})
    seen = []
    monkeypatch.setattr(run_mod, "_write_runtime_status_quiet", lambda **kw: seen.append(kw))
    run_mod._runtime_heartbeat(r)
    assert seen == [{"activity": {"voice_sessions": 2}}]
    seen.clear()
    run_mod._runtime_heartbeat(None)                    # no runner: still a plain heartbeat, nothing invented
    assert seen == [{}]


def _voice_runner(tmp_path, adapter):
    from unittest.mock import MagicMock

    r = object.__new__(GatewayRunner)
    r.adapters = {}
    r._profile_adapters = {}
    r._voice_mode = {}
    r._VOICE_MODE_PATH = tmp_path / "gateway_voice_mode.json"
    r._session_db = None
    r.session_store = MagicMock()
    r._is_user_authorized = lambda source: True
    r._delivery_adapter_for = lambda source: adapter
    r.published = []
    r._persist_active_agents = lambda: r.published.append(r._active_voice_session_count())
    return r


def _discord_event(text):
    from gateway.platforms.event import MessageEvent, MessageType
    from gateway.session import SessionSource

    source = SessionSource(chat_id="123", user_id="u1", platform=Platform.DISCORD)
    source.thread_id = None
    event = MessageEvent(text=text, message_type=MessageType.TEXT, source=source)
    event.raw_message = SimpleNamespace(guild_id=111, guild=None)
    return event


@pytest.mark.asyncio
async def test_voice_join_publishes_the_new_session_immediately(tmp_path):
    """/voice channel is a control command (exempt from the turns hold): the updater must see the new
    session at once, not at the next 60 s heartbeat."""
    from unittest.mock import AsyncMock, MagicMock

    adapter = _VoiceAdapter({})
    adapter.get_user_voice_channel = AsyncMock(return_value=MagicMock())
    adapter._voice_text_channels, adapter._voice_sources, adapter._voice_input_callback = {}, {}, None

    async def join(channel):
        adapter._voice_clients[111] = _VC(True)
        return True

    adapter.join_voice_channel = join
    r = _voice_runner(tmp_path, adapter)
    r.adapters[Platform.DISCORD] = adapter
    await r._handle_voice_channel_join(_discord_event("/voice channel"))
    assert r.published == [1]


@pytest.mark.asyncio
async def test_voice_leave_publishes_the_ended_session_immediately(tmp_path):
    adapter = _VoiceAdapter({111: _VC(True)})
    adapter._voice_input_callback = None

    async def leave(guild_id):
        adapter._voice_clients.pop(guild_id)

    adapter.leave_voice_channel = leave
    r = _voice_runner(tmp_path, adapter)
    r.adapters[Platform.DISCORD] = adapter
    await r._handle_voice_channel_leave(_discord_event("/voice leave"))
    assert r.published == [0]


def test_a_turn_boundary_never_fails_because_activity_cannot_be_computed(monkeypatch):
    """_persist_active_agents runs on every turn: a runner that cannot report activity (a stub, a raising
    adapter) must still publish active_agents, and the voice count is then UNKNOWN (null), never carried over."""
    from gateway.run_agent_cache import GatewayAgentCacheMixin

    seen = {}
    monkeypatch.setattr("gateway.run._write_runtime_status_quiet", lambda **kw: seen.update(kw))
    stub = SimpleNamespace(_active_work_count=lambda: 2, _running_agent_items=list)
    GatewayAgentCacheMixin._persist_active_agents(stub)
    assert seen == {"active_agents": 2, "activity": {"voice_sessions": None}}

    def boom():
        raise RuntimeError("x")

    seen.clear()
    stub._runtime_activity = boom
    GatewayAgentCacheMixin._persist_active_agents(stub)
    assert seen["activity"] == {"voice_sessions": None}
