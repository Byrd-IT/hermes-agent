"""Scoped emergency stop at every real callsite: background held, user turns (Telegram) allowed.

Each background dispatcher is exercised through its real code path with a scoped hold that names it
(must stop) and one that does not (must keep running); the inbound gateway gate is exercised for a
Telegram user turn, a heartbeat-provenance turn and the legacy full pause.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from types import SimpleNamespace

import pytest

from agent import estop
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource

BG = estop.BACKGROUND_SCOPES
LOG = logging.getLogger("test.estop.callsites")


@pytest.fixture(autouse=True)
def hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    estop._logged_components.clear()
    return tmp_path


def hold(scopes, **kw):
    return estop.engage_scoped(scopes, owner="updater", ttl_s=600, reason="update drain", **kw)


def others(component):
    return [s for s in BG if s != component]


# ── kanban ──────────────────────────────────────────────────────────────────


def test_kanban_dispatch_blocked_only_by_the_kanban_scope():
    from gateway.kanban_watchers_common import _kanban_dispatch_allowed

    token = hold(others("kanban"))["token"]
    assert _kanban_dispatch_allowed() is True
    estop.set_scopes(token, ["kanban"])
    assert _kanban_dispatch_allowed() is False
    estop.release(token)
    assert _kanban_dispatch_allowed() is True


def test_standalone_kanban_daemon_holds_its_dispatch_under_the_kanban_scope(monkeypatch):
    """`hermes kanban daemon` is a standing background spawner: a hold must stop it too."""
    from hermes_cli import kanban_db_dispatch as kbd

    ticks = []
    monkeypatch.setattr(kbd, "dispatch_once", lambda conn, **kw: ticks.append(1) or SimpleNamespace())
    monkeypatch.setattr(kbd._kbc, "connect", lambda *a, **k: SimpleNamespace(close=lambda: None))

    def run_for(seconds):
        stop = threading.Event()
        t = threading.Thread(target=kbd.run_daemon, kwargs=dict(interval=0.01, stop_event=stop))
        t.start()
        stop.wait(seconds)
        stop.set()
        t.join(5)

    token = hold(["kanban"])["token"]
    run_for(0.3)
    assert ticks == [], "a held daemon must not dispatch"
    estop.set_scopes(token, others("kanban"))
    run_for(0.3)
    assert ticks, "holding other scopes must not stop the kanban daemon"


# ── cron ticker / misfire sweep / webhook ───────────────────────────────────


def test_cron_tick_stops_only_for_the_cron_scope(monkeypatch):
    from cron import scheduler

    scans = []
    monkeypatch.setattr(scheduler, "get_due_jobs", lambda: scans.append(1) or [])
    token = hold(others("cron"))["token"]
    scheduler.tick(verbose=False)
    assert scans == [1]
    estop.set_scopes(token, ["cron"])
    assert scheduler.tick(verbose=False) == 0
    assert scans == [1], "the held ticker must not even scan due jobs"


def test_cron_misfire_sweep_stops_only_for_its_own_scope(monkeypatch):
    from cron import scheduler_provider as sp

    reached = []
    monkeypatch.setattr(sp, "_misfire_grace_minutes", lambda: reached.append(1) or 0)
    provider = SimpleNamespace()
    token = hold(others("cron-misfire"))["token"]
    sp.fire_overdue_jobs(provider)
    assert reached == [1], "other scopes held: the sweep still runs past the estop gate"
    estop.set_scopes(token, ["cron-misfire"])
    assert sp.fire_overdue_jobs(provider) == 0
    assert reached == [1], "held: it returned before reaching the sweep"


@pytest.mark.asyncio
async def test_cron_webhook_is_refused_only_for_its_scope(monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from gateway.platforms.api_server import APIServerAdapter, cors_middleware

    class Spy:
        def __init__(self):
            self.claimed, self.fired = [], []

        def claim_fire(self, job_id):
            self.claimed.append(job_id)
            return {"id": job_id, "execution_id": "e1"}

        def fire_claimed(self, job, *, adapters=None, loop=None):
            self.fired.append(job["id"])
            return True

    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "sk-secret"}))
    app = web.Application(middlewares=[cors_middleware])
    app["api_server_adapter"] = adapter
    app.router.add_post("/api/cron/fire", adapter._handle_cron_fire)
    provider = Spy()
    monkeypatch.setattr("cron.scheduler_provider.resolve_cron_scheduler", lambda: provider)
    monkeypatch.setattr("plugins.cron_providers.chronos.verify.get_fire_verifier",
                        lambda: (lambda **kw: {"purpose": "cron_fire"}))
    token = hold(["cron-webhook"])["token"]
    async with TestClient(TestServer(app)) as cli:
        refused = await cli.post("/api/cron/fire", headers={"Authorization": "Bearer good"}, json={"job_id": "j"})
        assert refused.status == 503 and refused.headers["Retry-After"] == "60"
        assert provider.claimed == []
        estop.set_scopes(token, others("cron-webhook"))
        ok = await cli.post("/api/cron/fire", headers={"Authorization": "Bearer good"}, json={"job_id": "j"})
        assert ok.status == 202
        for _ in range(100):
            if provider.fired:
                break
            await asyncio.sleep(0.01)
    assert provider.claimed == ["j"] and provider.fired == ["j"]


# ── heartbeat + loop (gateway watchers) ─────────────────────────────────────


@pytest.fixture
def heartbeat_poller(monkeypatch):
    from hermes_cli import goals, heartbeat
    from hermes_cli.heartbeat import HeartbeatManager
    from gateway.session import build_session_key
    from tests.gateway.test_heartbeat_poller import _HeartbeatAdapter

    goals._DB_CACHE.clear()
    goals._get_session_db()
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(heartbeat, "time", SimpleNamespace(time=lambda: clock.now))
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", user_id="42", chat_type="dm", message_id="w")
    key = build_session_key(source)
    adapter = _HeartbeatAdapter(PlatformConfig(enabled=True, typing_indicator=False), Platform.TELEGRAM)
    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._delivery_adapter_for = lambda source: adapter
    runner._run_in_executor_with_context = asyncio.to_thread
    HeartbeatManager("heartbeat-session").set("check status", 60)
    yield runner, adapter, {key: (source, "heartbeat-session")}, clock
    goals._DB_CACHE.clear()


@pytest.mark.asyncio
async def test_heartbeat_tick_is_held_only_by_the_heartbeat_scope_and_stays_due(heartbeat_poller):
    from hermes_cli.heartbeat import HeartbeatManager

    runner, adapter, watch, clock = heartbeat_poller
    received = []

    async def agent_turn(event):
        event._heartbeat_execution_started = True
        received.append(event)

    adapter.set_message_handler(agent_turn)
    clock.now += 900
    before = HeartbeatManager("heartbeat-session").state.to_json()
    token = hold(["heartbeat"])["token"]
    for _ in range(3):
        await runner._heartbeat_poll_once(watch)
        await asyncio.gather(*adapter._background_tasks)
    assert received == [] and HeartbeatManager("heartbeat-session").state.to_json() == before

    estop.set_scopes(token, others("heartbeat"))
    await runner._heartbeat_poll_once(watch)
    await asyncio.gather(*adapter._background_tasks)
    assert len(received) == 1, "background scopes without `heartbeat` must not hold it"


@pytest.mark.asyncio
async def test_heartbeat_admitted_through_the_session_authority_is_held_too(heartbeat_poller, monkeypatch):
    """With a session authority the poller hands the tick to admit_heartbeat, which builds an
    INTERNAL event the inbound gate never sees — so the hold must be checked before that branch."""
    runner, adapter, watch, clock = heartbeat_poller
    admitted = []

    async def admit(runner_, adapter_, source, session_id, route):
        admitted.append(session_id)

    monkeypatch.setattr("gateway.run_heartbeat_acceptance.admit_heartbeat", admit)
    runner.session_authority = object()
    adapter.set_message_handler(lambda event: None)
    clock.now += 900
    token = hold(["heartbeat"])["token"]
    await runner._heartbeat_poll_once(watch)
    assert admitted == []
    estop.set_scopes(token, others("heartbeat"))
    await runner._heartbeat_poll_once(watch)
    assert admitted == ["heartbeat-session"]


@pytest.mark.asyncio
async def test_legacy_pause_also_holds_the_authority_heartbeat_path(heartbeat_poller, monkeypatch):
    runner, adapter, watch, clock = heartbeat_poller
    admitted = []

    async def admit(*a):
        admitted.append(a)

    monkeypatch.setattr("gateway.run_heartbeat_acceptance.admit_heartbeat", admit)
    runner.session_authority = object()
    adapter.set_message_handler(lambda event: None)
    clock.now += 900
    estop.engage(reason="operator")
    await runner._heartbeat_poll_once(watch)
    assert admitted == []


@pytest.mark.asyncio
async def test_loop_wakeup_is_held_only_by_the_loop_scope(monkeypatch):
    from hermes_cli import loops
    from tests.gateway.test_multiplex_notice_egress_profile_adapter import _runner

    fired = []

    class Mgr:
        state = SimpleNamespace(ticks_fired=1)

        def __init__(self, session_id=None):
            pass

        def is_due(self, now):
            return True

        def fire_tick(self):
            fired.append(1)
            return "tick"

        def complete_tick(self, *_):
            return {}

    monkeypatch.setattr(loops, "LoopManager", Mgr)
    monkeypatch.setattr(loops, "goal_blocks_loop_tick", lambda sid: False)
    r = _runner()
    r._running = True
    route = {"platform": "telegram", "chat_id": "42", "chat_type": "dm", "user_id": "42", "profile": "sec"}
    state = SimpleNamespace(awaiting_response=False, next_due_at=0, route=route)

    token = hold(["loop"])["token"]
    await r._loop_wakeup_fire_one("sid", state, 1e12, set())
    assert fired == [] and r._profile_adapters["sec"][Platform.TELEGRAM].handled == []
    estop.set_scopes(token, others("loop"))
    await r._loop_wakeup_fire_one("sid", state, 1e12, set())
    assert fired == [1] and r._profile_adapters["sec"][Platform.TELEGRAM].handled == ["tick"]


# ── the inbound gate: Telegram user turns allowed, background provenance blocked ──


def _runner():
    r = object.__new__(GatewayRunner)
    r._running_agents = {}
    r.session_store = None
    return r


def _telegram_event(text="hello", heartbeat=False):
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="42", user_id="42", chat_type="dm", message_id="1")
    event = MessageEvent(text=text, source=source, message_id="1")
    if heartbeat:
        event._heartbeat_session_id = "hb-session"
    return event, source


def test_telegram_user_turn_is_allowed_while_only_background_is_held():
    hold(BG)
    event, source = _telegram_event()
    assert _runner()._hm_estop_gate(event, source, False) is None


def test_every_non_turn_scope_combination_lets_a_telegram_turn_through():
    token = hold(["kanban"])["token"]
    for scopes in (["kanban", "cron"], list(BG), ["heartbeat"], ["loop", "cron-webhook"]):
        estop.set_scopes(token, scopes)
        event, source = _telegram_event()
        assert _runner()._hm_estop_gate(event, source, False) is None, scopes


def test_final_turns_scope_gives_telegram_the_scoped_notice():
    hold([*BG, "turns"])
    event, source = _telegram_event()
    notice = _runner()._hm_estop_gate(event, source, False)
    assert notice == estop.paused_reply() and "resume automatically" in notice


def test_turns_scope_still_exempts_the_same_control_traffic_as_the_legacy_pause():
    """/pause off, other slash commands and replies owned by in-flight work pass the turn gate."""
    hold([*BG, "turns"])
    r = _runner()
    event, source = _telegram_event("/status")
    event.get_command = lambda: "status"
    assert r._hm_estop_gate(event, source, False) is None
    event, source = _telegram_event("keep going")
    r._is_session_running = lambda key: True
    r._session_key_for_source = lambda source: "agent:main:telegram:dm:42"
    assert r._hm_estop_gate(event, source, False) is None


def test_heartbeat_turn_that_slips_past_the_poller_is_dropped_silently_under_the_heartbeat_scope():
    token = hold(["heartbeat"])["token"]
    event, source = _telegram_event("check status", heartbeat=True)
    assert _runner()._hm_estop_gate(event, source, False) == ""
    estop.set_scopes(token, others("heartbeat"))
    assert _runner()._hm_estop_gate(event, source, False) is None


def test_internal_events_still_bypass_the_inbound_gate_under_any_hold():
    hold([*BG, "turns"])
    event, source = _telegram_event()
    assert _runner()._hm_estop_gate(event, source, True) is None


def test_legacy_full_pause_gate_behaviour_is_unchanged():
    estop.engage(reason="maintenance window")
    event, source = _telegram_event()
    assert _runner()._hm_estop_gate(event, source, False) == estop.paused_reply()
    hb, source = _telegram_event("check", heartbeat=True)
    assert _runner()._hm_estop_gate(hb, source, False) == ""


@pytest.mark.asyncio
async def test_real_handle_message_path_background_only_hold_does_not_answer_with_a_pause_notice():
    class FakeSource:
        platform = None
        chat_id = "c1"
        user_id = "u1"
        user_name = "user"
        chat_type = "dm"
        profile = None

    class FakeEvent:
        internal = False
        text = "hello"
        source = FakeSource()

        def get_command(self):
            return None

    runner = object.__new__(GatewayRunner)
    runner._is_user_authorized = lambda source: True
    token = hold(BG)["token"]
    try:
        reply = await runner._handle_message(FakeEvent())
    except Exception:
        reply = None  # the bare runner fails further down the pipeline: the gate let the turn through
    assert reply is None or "paused" not in reply.lower()

    estop.set_scopes(token, [*BG, "turns"])
    reply = await runner._handle_message(FakeEvent())
    assert reply == estop.paused_reply() and "update drain" in reply
