"""Conditional restart, round 5: worker-thread admission and the claimed marker.

Reviewer findings reproduced here with deterministic barriers on the REAL thread entry points:

* B1 -- a worker thread that passed its gate BEFORE the idle decision registered its work AFTER an accepted idle
  count (cron ticker: ``can_dispatch()`` -> ``get_due_jobs`` -> ``try_register_running_job``; kanban dispatcher:
  owner check and ``tick_once`` are separate thread hops). The restart then waited on that work: an extra drain.
  The fix is an admission gate (``agent/admission_gate.py``): each thread entry is a span; a span that entered
  before the decision is COUNTED by it, one after it waits for the outcome and is refused when the restart was
  accepted. These tests fail on the head that has only ``runner._draining``.
* B2 (core half) -- the gateway must keep the claimed marker until its decision is durable, so a requester can
  tell "claimed, still deciding" (unknown) from "withdrawn / declined" (proven).
"""

from __future__ import annotations

import json
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent import admission_gate, estop
from gateway import restart_if_idle as rii
from gateway.run import GatewayRunner
from tests.gateway.test_restart_if_idle import (
    BG, OWNER, _ack, _no_queued_events, _put, _request, _runner, hold, home,
)


def _threaded_runner():
    """A runner whose restart is a flag (the real one needs the running loop), for tests that run the decision on
    a plain thread to stall it."""
    runner = _runner()
    runner._external_drain_active = False
    runner._running_cron_job_count = GatewayRunner._running_cron_job_count      # the real cron count (staticmethod)

    def request_restart(*, detached=False, via_service=False):
        if runner._restart_task_started:
            return False
        runner._restart_requested = runner._restart_task_started = runner._draining = True
        return True
    runner.request_restart = request_restart
    return runner


def _decide_in_thread(runner):
    t = threading.Thread(target=runner.handle_restart_if_idle_request, daemon=True)
    t.start()
    return t


def _until(pred, what, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return
        time.sleep(0.005)
    raise AssertionError(f"timed out waiting for {what}")


# ── the gate itself ──────────────────────────────────────────────────────────


def test_a_span_that_entered_before_the_decision_is_counted_and_one_after_it_waits_for_the_outcome():
    g = admission_gate.AdmissionGate()
    with g.admit("a") as ok:
        assert ok and g.spans() == {"a": 1}
        assert g.begin_decision()
        assert g.spans() == {"a": 1}                      # the decision counts it
    assert g.spans() == {}
    got = {}

    def late():
        with g.admit("b") as ok2:
            got["ok"] = ok2

    t = threading.Thread(target=late); t.start()
    time.sleep(0.1)
    assert t.is_alive() and "ok" not in got                # waits while the decision runs
    g.end_decision(False)                                  # refused/busy -> reopened
    t.join(5)
    assert got["ok"] is True


def test_an_accepted_decision_closes_the_gate_for_good():
    g = admission_gate.AdmissionGate()
    assert g.begin_decision()
    got = []
    t = threading.Thread(target=lambda: got.append(g.enter("x"))); t.start()
    time.sleep(0.05)
    g.end_decision(True)
    t.join(5)
    assert got == [None]
    with g.admit("y") as ok:
        assert ok is False
    assert g.state == admission_gate.CLOSED and not g.begin_decision()


def test_a_span_nested_in_the_threads_own_span_never_waits_for_the_decision_that_counts_it():
    g = admission_gate.AdmissionGate()
    done = threading.Event()

    def worker():
        with g.admit("outer") as ok:
            assert ok
            started.set()
            release.wait(5)
            with g.admit("inner") as ok2:                 # during DECIDING: must not wait (the outer is counted)
                assert ok2
        done.set()

    started, release = threading.Event(), threading.Event()
    t = threading.Thread(target=worker); t.start()
    assert started.wait(5)
    assert g.begin_decision() and g.spans() == {"outer": 1}
    release.set()
    assert done.wait(2), "the nested span waited for the decision that was counting its outer span"
    g.end_decision(False)
    t.join(5)


def test_the_thread_that_runs_the_decision_is_refused_not_deadlocked_if_it_asks_for_a_span():
    g = admission_gate.AdmissionGate()
    assert g.begin_decision()
    t0 = time.time()
    with g.admit("self") as ok:
        assert ok is False
    assert time.time() - t0 < 1.0


def test_a_span_gives_up_closed_when_the_decision_never_finishes():
    g = admission_gate.AdmissionGate()
    assert g.begin_decision()
    t0 = time.time()
    other = []
    t = threading.Thread(target=lambda: other.append(g.enter("slow", wait_s=0.2))); t.start(); t.join(5)
    assert other == [None] and time.time() - t0 < 3
    assert g.spans() == {}


def test_span_exit_is_idempotent_and_counts_stay_correct():
    g = admission_gate.AdmissionGate()
    s = g.enter("h")
    s.exit(); s.exit()
    assert g.spans() == {}


# ── B1: the real cron ticker ─────────────────────────────────────────────────


def _patch_cron(monkeypatch, *, parked, go, started, body_release, before_span=None):
    import cron.scheduler as sched
    import cron.scheduler_tick as tick_mod

    def get_due_jobs():
        parked.set()
        assert go.wait(20), "barrier never released"
        return [{"id": "probe-job", "name": "probe", "schedule": {"kind": "interval"}, "enabled": True}]

    def process_due_job(job, adapters, loop, verbose):
        started.set()
        body_release.wait(20)
        return True

    monkeypatch.setattr(sched, "get_due_jobs", get_due_jobs)
    monkeypatch.setattr(sched, "_process_due_job", process_due_job)
    monkeypatch.setattr(tick_mod, "_advance_or_drop_recurring", lambda _s, jobs: jobs)
    monkeypatch.setattr(sched, "_sweep_stale_inflight_for_tick", lambda jobs: None)
    monkeypatch.setattr(tick_mod, "_pre_dispatch_housekeeping", before_span or (lambda _s, h: None))
    monkeypatch.setattr(sched, "_maybe_run_worktree_maintenance", lambda: None)
    monkeypatch.setattr(sched, "_sweep_mcp_orphans_when_all_done", lambda futures: None)
    return sched, tick_mod


def _can_dispatch(runner):
    return lambda: not (runner._draining or runner._external_drain_active)       # run_bootstrap.py's gate


def _start_tick(tick_mod, runner):
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("n", tick_mod.tick(
        verbose=False, sync=False, can_dispatch=_can_dispatch(runner))), daemon=True)
    t.start()
    return t, out


def test_a_ticker_stalled_in_its_scan_is_counted_so_the_idle_decision_refuses_instead_of_accepting_then_drain(
        home, monkeypatch):
    """Reviewer probe A. The ticker passed ``can_dispatch`` and parks in the scan; the old head counted 0 jobs,
    accepted, and the ticker then registered a job after the accept (an extra drain of up to 960 s)."""
    runner = _threaded_runner()
    parked, go, started, release = (threading.Event() for _ in range(4))
    sched, tick_mod = _patch_cron(monkeypatch, parked=parked, go=go, started=started, body_release=release)
    t, _out = _start_tick(tick_mod, runner)
    try:
        assert parked.wait(20)
        hold_ = estop.engage_scoped(BG + [estop.SCOPE_TURNS], owner=OWNER, ttl_s=120, reason="probe", home=home)
        _put(home, _request(home, hold_))
        runner.handle_restart_if_idle_request()
        ack = _ack(home)
        go.set()
        assert started.wait(10), "the ticker never ran its job"
        registered = sched.get_running_job_ids()
        assert not (ack["result"] == "accepted" and registered), \
            f"job {sorted(registered)} registered AFTER the restart was accepted"
        assert ack["result"] == "refused_busy" and ack["active"].get("admission:cron_tick") == 1, ack
        assert runner._draining is False and runner._restart_task_started is False
    finally:
        release.set(); go.set(); t.join(10)


def test_a_ticker_that_is_still_before_its_scan_is_refused_untouched_when_the_restart_was_accepted(home, monkeypatch):
    """The other side of the same linearization point: the tick has not entered its span yet, the decision is
    accepted, so the tick must stop BEFORE it scans (a recurring job is not advanced, a one-shot not claimed)."""
    runner = _threaded_runner()
    parked, go, started, release = (threading.Event() for _ in range(4))
    at_housekeeping, pass_housekeeping = threading.Event(), threading.Event()

    def housekeeping(_s, _h):
        at_housekeeping.set()
        assert pass_housekeeping.wait(20)

    sched, tick_mod = _patch_cron(monkeypatch, parked=parked, go=go, started=started, body_release=release,
                                  before_span=housekeeping)
    go.set()
    t, out = _start_tick(tick_mod, runner)
    assert at_housekeeping.wait(20)
    hold_ = estop.engage_scoped(BG + [estop.SCOPE_TURNS], owner=OWNER, ttl_s=120, reason="probe", home=home)
    _put(home, _request(home, hold_))
    runner.handle_restart_if_idle_request()
    assert _ack(home)["result"] == "accepted"
    pass_housekeeping.set()
    t.join(10)
    assert out["n"] == 0 and not parked.is_set(), "the tick scanned (and would advance/claim) after the accept"
    assert sched.get_running_job_ids() == frozenset()


def test_a_ticker_arriving_while_the_decision_is_writing_its_ack_waits_and_is_then_refused_untouched(home, monkeypatch):
    runner = _threaded_runner()
    parked, go, started, release = (threading.Event() for _ in range(4))
    at_housekeeping, pass_housekeeping = threading.Event(), threading.Event()

    def housekeeping(_s, _h):                           # after the tick's pause + dispatch gates, before its span
        at_housekeeping.set()
        assert pass_housekeeping.wait(20)

    _sched, tick_mod = _patch_cron(monkeypatch, parked=parked, go=go, started=started, body_release=release,
                                  before_span=housekeeping)
    go.set()
    in_write, let_write = threading.Event(), threading.Event()
    real_write = rii.write_ack

    def slow_write(h, payload, **kw):
        in_write.set()
        assert let_write.wait(20)
        return real_write(h, payload, **kw)
    monkeypatch.setattr(rii, "write_ack", slow_write)
    t, out = _start_tick(tick_mod, runner)
    assert at_housekeeping.wait(20)
    hold = estop.engage_scoped(BG + [estop.SCOPE_TURNS], owner=OWNER, ttl_s=120, reason="t", home=home)
    _put(home, _request(home, hold))
    d = _decide_in_thread(runner)
    assert in_write.wait(10)
    pass_housekeeping.set()
    time.sleep(0.3)
    assert t.is_alive(), "the tick did not wait for the pending decision"
    let_write.set(); d.join(10); t.join(10)
    assert _ack(home)["result"] == "accepted"
    assert out["n"] == 0 and not parked.is_set(), "the tick scanned after the accepted decision"


def test_a_manual_or_background_registration_after_the_accept_is_refused_and_one_before_it_is_counted(home, hold):
    import cron.scheduler as sched
    runner = _threaded_runner()
    owner = object()
    assert sched.try_register_running_job("before", owner=owner) is True
    try:
        _put(home, _request(home, hold))
        runner.handle_restart_if_idle_request()
        ack = _ack(home)
        assert ack["result"] == "refused_busy" and ack["active"].get("cron") == 1
    finally:
        sched.release_running_job("before", owner=owner)
    os.unlink(rii.ack_path(home))
    _put(home, _request(home, hold))
    runner.handle_restart_if_idle_request()
    assert _ack(home)["result"] == "accepted"
    assert sched.try_register_running_job("after", owner=object()) is False
    assert sched.get_running_job_ids() == frozenset()


def test_a_fire_that_reaches_run_one_job_after_the_accept_is_not_started(home, monkeypatch):
    import cron.scheduler as sched
    runner = _threaded_runner()
    ran = []
    monkeypatch.setattr(sched, "_run_one_job_admitted", lambda job, **kw: ran.append(job["id"]) or True)
    assert sched.run_one_job({"id": "j1"}) is True and ran == ["j1"]          # open gate: unchanged behavior
    hold_ = estop.engage_scoped(BG + [estop.SCOPE_TURNS], owner=OWNER, ttl_s=120, reason="t", home=home)
    _put(home, _request(home, hold_))
    runner.handle_restart_if_idle_request()
    assert _ack(home)["result"] == "accepted"
    assert sched.run_one_job({"id": "j2"}) is False and ran == ["j1"]


# ── B1: misfire sweep (external cron providers) ──────────────────────────────


def _overdue_provider(monkeypatch, calls, fire_gate=None):
    import cron.scheduler_provider as sp

    class P(sp.CronScheduler):
        name = "ext"

        def start(self, *a, **k): ...

        def claim_fire(self, job_id, **kw):
            calls.append(("claim", job_id))
            return {"id": job_id}

        def fire_claimed(self, claimed_job, **kw):
            calls.append(("fire", claimed_job["id"]))
            if fire_gate is not None:
                fire_gate.wait(20)
            return True

    job = {"id": "overdue-job", "name": "o", "next_run_at": "2000-01-01T00:00:00+00:00",
           "schedule": {"kind": "interval"}, "enabled": True}
    from cron import jobs
    monkeypatch.setattr(jobs, "load_jobs", lambda *a, **k: [job])
    monkeypatch.setattr(jobs, "is_job_runnable", lambda j: True)
    monkeypatch.setattr(sp, "_misfire_grace_minutes", lambda: 1.0)
    # The updater's hold pauses the sweep at its own pause check; the race is a sweep that PASSED that check before
    # the hold existed, so the check is neutralized here and only the admission gate stands between claim and run.
    monkeypatch.setattr(estop, "check_paused", lambda *a, **k: False)
    return sp, P()


def test_the_misfire_sweep_claims_nothing_once_the_restart_was_accepted(home, monkeypatch):
    runner = _threaded_runner()
    calls = []
    sp, provider = _overdue_provider(monkeypatch, calls)
    hold_ = estop.engage_scoped(BG + [estop.SCOPE_TURNS], owner=OWNER, ttl_s=120, reason="t", home=home)
    _put(home, _request(home, hold_))
    runner.handle_restart_if_idle_request()
    assert _ack(home)["result"] == "accepted"
    assert sp.fire_overdue_jobs(provider) == 0
    assert calls == [], "a fire was claimed in a gateway that accepted the restart"


def test_a_misfire_fire_in_progress_is_counted_until_it_ends(home, monkeypatch):
    runner = _threaded_runner()
    calls, gate_ = [], threading.Event()
    sp, provider = _overdue_provider(monkeypatch, calls, fire_gate=gate_)
    assert sp.fire_overdue_jobs(provider) == 1
    _until(lambda: ("fire", "overdue-job") in calls, "the fire thread")
    hold_ = estop.engage_scoped(BG + [estop.SCOPE_TURNS], owner=OWNER, ttl_s=120, reason="t", home=home)
    _put(home, _request(home, hold_))
    runner.handle_restart_if_idle_request()
    ack = _ack(home)
    assert ack["result"] == "refused_busy" and ack["active"].get("admission:cron_misfire") == 1, ack
    gate_.set()
    _until(lambda: not admission_gate.gate().spans(), "the misfire span to end")


# ── B1: kanban dispatcher ────────────────────────────────────────────────────


@pytest.fixture
def kanban(home, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    kb.init_db()
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="card", assignee="alice")
    return SimpleNamespace(kb=kb, kbc=kbc, kbd=kbd, tid=tid)


def _status(k):
    with k.kbc.connect() as conn:
        return conn.execute("SELECT status FROM tasks WHERE id = ?", (k.tid,)).fetchone()[0]


def test_the_kanban_dispatcher_spawns_nothing_after_the_restart_was_accepted_even_past_its_owner_check(
        home, kanban, monkeypatch):
    """Reviewer probe K: ``_kanban_owner_unavailable`` (thread hop 1) said available, the decision landed, then
    ``tick_once`` (hop 2) claimed and spawned a worker for an owner that was about to go."""
    from gateway.kanban_watchers_common import _kanban_owner_unavailable
    from gateway.kanban_watchers_dispatcher import _DispatcherSettings, _KanbanDispatcher
    runner = _threaded_runner()
    assert _kanban_owner_unavailable(runner) == ""                       # hop 1: available
    hold_ = estop.engage_scoped(BG + [estop.SCOPE_TURNS], owner=OWNER, ttl_s=120, reason="t", home=home)
    _put(home, _request(home, hold_))
    runner.handle_restart_if_idle_request()
    assert _ack(home)["result"] == "accepted"
    spawned = []
    monkeypatch.setattr(kanban.kbd, "_default_spawn", lambda task, ws, *, board=None: spawned.append(task.id) or 4242)
    settings = _DispatcherSettings(interval=1.0, max_spawn=None, max_in_progress=None, failure_limit=3,
                                   stale_timeout_seconds=0, reconcile_orphans=False, default_assignee=None,
                                   max_in_progress_per_profile=None)
    _KanbanDispatcher(kanban.kb, settings).tick_once()                   # hop 2
    assert spawned == [] and _status(kanban) == "ready"


def test_a_kanban_claim_in_progress_is_counted_by_the_decision(home, kanban, monkeypatch):
    runner = _threaded_runner()
    in_spawn, let_spawn = threading.Event(), threading.Event()

    def spawn(task, ws, *, board=None):
        in_spawn.set()
        assert let_spawn.wait(20)
        return 4242
    monkeypatch.setattr(kanban.kbd, "_default_spawn", spawn)
    out = {}

    def tick():
        with kanban.kbc.connect() as conn:
            out["r"] = kanban.kbd.dispatch_once(conn)
    t = threading.Thread(target=tick, daemon=True); t.start()
    try:
        assert in_spawn.wait(20)
        hold_ = estop.engage_scoped(BG + [estop.SCOPE_TURNS], owner=OWNER, ttl_s=120, reason="t", home=home)
        _put(home, _request(home, hold_))
        runner.handle_restart_if_idle_request()
        ack = _ack(home)
        assert ack["result"] == "refused_busy" and ack["active"].get("admission:kanban_spawn") == 1, ack
    finally:
        let_spawn.set(); t.join(10)
    assert out["r"].spawned


def test_the_kanban_decomposer_is_refused_after_the_accept_and_counted_before_it(home, monkeypatch):
    from gateway.kanban_watchers_dispatcher import _KanbanDispatcher
    runner = _threaded_runner()
    calls = []
    decomp = SimpleNamespace(decompose_task=lambda tid, author: calls.append(tid) or SimpleNamespace(
        ok=True, fanout=False, child_ids=[], reason=""))
    assert _KanbanDispatcher._decompose_one(decomp, "default", "t1") == 1 and calls == ["t1"]
    hold_ = estop.engage_scoped(BG + [estop.SCOPE_TURNS], owner=OWNER, ttl_s=120, reason="t", home=home)
    _put(home, _request(home, hold_))
    runner.handle_restart_if_idle_request()
    assert _ack(home)["result"] == "accepted"
    assert _KanbanDispatcher._decompose_one(decomp, "default", "t2") == 0 and calls == ["t1"]


# ── B1: loop-thread and plugin entry points (audit) ──────────────────────────


def test_a_plugin_injection_scheduled_from_a_thread_after_the_accept_is_not_admitted(home):
    runner = _threaded_runner()
    hold_ = estop.engage_scoped(BG + [estop.SCOPE_TURNS], owner=OWNER, ttl_s=120, reason="t", home=home)
    _put(home, _request(home, hold_))
    assert runner._plugin_injection_accepting() is True
    runner.handle_restart_if_idle_request()
    assert _ack(home)["result"] == "accepted"
    assert runner._plugin_injection_accepting() is False


@pytest.mark.asyncio
async def test_a_dispatched_plugin_injection_that_wakes_after_the_accept_never_reaches_an_adapter(home):
    runner = _runner()
    runner._running_cron_job_count = lambda: 0
    adapter = MagicMock()
    runner._delivery_adapter_for = lambda source: adapter
    hold_ = estop.engage_scoped(BG + [estop.SCOPE_TURNS], owner=OWNER, ttl_s=120, reason="t", home=home)
    _put(home, _request(home, hold_))
    runner.handle_restart_if_idle_request()
    assert _ack(home)["result"] == "accepted"
    ok = await runner._dispatch_plugin_origin_injection(
        source=SimpleNamespace(platform=SimpleNamespace(value="telegram"), chat_id="1"), content="x", plugin_id="p")
    assert ok is False and adapter.handle_message.call_count == 0
    await runner._restart_task


@pytest.mark.asyncio
async def test_the_heartbeat_poller_hands_its_turn_to_the_adapter_gate_which_refuses_while_draining(home):
    """The heartbeat poll is a loop coroutine: it cannot interleave with the (await-free) decision, and the event
    it builds goes through the adapter's own ``_draining`` gate (run_inbound_hooks)."""
    import inspect
    from gateway import run_inbound_hooks
    src = inspect.getsource(run_inbound_hooks)
    assert "self._draining and getattr(event, \"_admitted_before_drain\", False) is not True" in src


# ── deadlock freedom / randomized interleaving ───────────────────────────────


def test_decision_and_registering_threads_never_deadlock_and_nothing_registers_after_an_accept(home, hold):
    import cron.scheduler as sched
    hold_ = hold
    for rnd in range(25):
        admission_gate.reset_for_tests()
        runner = _threaded_runner()
        stop = threading.Event()
        late = []
        accepted_at = {}

        def worker(n):
            while not stop.is_set():
                owner = object()
                ok = sched.try_register_running_job(f"w{n}", owner=owner)
                if ok:
                    if accepted_at.get("t") is not None and time.monotonic() > accepted_at["t"]:
                        late.append(n)
                    time.sleep(0.0005)
                    sched.release_running_job(f"w{n}", owner=owner)
                    time.sleep(0.004 + 0.001 * n)       # duty cycle: idle instants exist, but are rare
                else:
                    time.sleep(0.001)

        threads = [threading.Thread(target=worker, args=(n,), daemon=True) for n in range(4)]
        for t in threads:
            t.start()
        time.sleep(0.003 * (rnd % 7))
        for _ in range(200):                              # keep asking until a decision accepts (idle instant)
            _put(home, _request(home, hold_))
            d = _decide_in_thread(runner)
            d.join(10)
            assert not d.is_alive(), "the decision deadlocked"
            if runner._restart_task_started:
                accepted_at["t"] = time.monotonic()
                break
            os.path.exists(rii.ack_path(home)) and os.unlink(rii.ack_path(home))
            runner._restart_if_idle_seen.clear() if hasattr(runner, "_restart_if_idle_seen") else None
        time.sleep(0.05)
        stop.set()
        for t in threads:
            t.join(10)
            assert not t.is_alive(), "a registering thread is stuck"
        assert runner._restart_task_started, "never found an idle instant"
        assert late == [], f"registrations succeeded after the accept: {late}"
        sched._running_job_ids.clear()
        if os.path.exists(rii.ack_path(home)):
            os.unlink(rii.ack_path(home))


# ── the decision's use of the gate ───────────────────────────────────────────


def test_the_gate_is_deciding_and_admission_frozen_when_work_is_counted_and_closed_after_accept(home, monkeypatch):
    runner = _threaded_runner()
    seen = {}
    real = rii.idle_probe

    def spy(r):
        seen["state"], seen["draining"] = admission_gate.gate().state, r._draining
        return real(r)
    monkeypatch.setattr(rii, "idle_probe", spy)
    hold_ = estop.engage_scoped(BG + [estop.SCOPE_TURNS], owner=OWNER, ttl_s=120, reason="t", home=home)
    _put(home, _request(home, hold_))
    runner.handle_restart_if_idle_request()
    assert seen == {"state": admission_gate.DECIDING, "draining": True}
    assert admission_gate.gate().state == admission_gate.CLOSED


@pytest.mark.parametrize("how", ["busy", "unknown", "ack_unwritable", "expired", "exception"])
def test_every_way_of_not_restarting_reopens_the_gate(home, monkeypatch, how):
    runner = _threaded_runner()
    if how == "busy":
        runner._running_agents["c"] = MagicMock()
    elif how == "unknown":
        runner._active_voice_session_count = lambda: None
    elif how == "ack_unwritable":
        monkeypatch.setattr(rii, "write_ack", MagicMock(side_effect=OSError("disk full")))
    elif how == "expired":
        real_probe, jumped = rii.idle_probe, []

        def probe_then_time_passes(r):
            jumped.append(1)                            # the request's validity ends while the count runs
            return real_probe(r)
        monkeypatch.setattr(rii, "idle_probe", probe_then_time_passes)
        real_now = rii._now
        monkeypatch.setattr(rii, "_now", lambda: real_now() + (100 if jumped else 0))
    elif how == "exception":
        monkeypatch.setattr(rii, "idle_probe", MagicMock(side_effect=RuntimeError("bug")))
    hold_ = estop.engage_scoped(BG + [estop.SCOPE_TURNS], owner=OWNER, ttl_s=120, reason="t", home=home)
    _put(home, _request(home, hold_))
    runner.handle_restart_if_idle_request()
    assert admission_gate.gate().state == admission_gate.OPEN
    assert runner._draining is False and runner._restart_task_started is False


# ── B2 (core half): the claimed marker lives until the decision is durable ───


def test_the_claimed_marker_stays_while_the_ack_write_is_stalled_and_goes_after_the_ack_is_durable(home, monkeypatch):
    runner = _threaded_runner()
    in_write, let_write = threading.Event(), threading.Event()
    real_write = rii.write_ack
    order = []

    def slow_write(h, payload, **kw):
        order.append("write-begin")
        in_write.set()
        assert let_write.wait(20)
        real_write(h, payload, **kw)
        order.append("write-done")
    real_release = rii.release_claim

    def spy_release(h):
        order.append("ack-exists" if rii.ack_path(h).exists() else "NO-ACK")
        real_release(h)
    monkeypatch.setattr(rii, "write_ack", slow_write)
    monkeypatch.setattr(rii, "release_claim", spy_release)
    hold_ = estop.engage_scoped(BG + [estop.SCOPE_TURNS], owner=OWNER, ttl_s=120, reason="t", home=home)
    body = _request(home, hold_)
    _put(home, body)
    d = _decide_in_thread(runner)
    assert in_write.wait(10)
    marker = rii.claimed_path(home)
    assert marker.exists() and not rii.request_path(home).exists() and _ack(home) is None
    assert json.loads(marker.read_text())["request_id"] == body["request_id"]     # the requester can recognise it
    let_write.set(); d.join(10)
    assert _ack(home)["result"] == "accepted" and not marker.exists()
    assert order == ["write-begin", "write-done", "ack-exists"]


@pytest.mark.parametrize("how", ["refused_busy", "invalid", "ack_unwritable", "exception", "untrusted"])
def test_the_marker_is_removed_on_every_path_and_never_before_the_answer(home, monkeypatch, how):
    runner = _threaded_runner()
    hold_ = estop.engage_scoped(BG + [estop.SCOPE_TURNS], owner=OWNER, ttl_s=120, reason="t", home=home)
    body = _request(home, hold_)
    mode = 0o600
    if how == "refused_busy":
        runner._running_agents["c"] = MagicMock()
    elif how == "invalid":
        body["pid"] = os.getpid() + 1
    elif how == "ack_unwritable":
        monkeypatch.setattr(rii, "write_ack", MagicMock(side_effect=OSError("disk full")))
    elif how == "exception":
        monkeypatch.setattr(rii, "idle_probe", MagicMock(side_effect=RuntimeError("bug")))
    elif how == "untrusted":
        mode = 0o666
    _put(home, body, mode=mode)
    at_release = []
    real_release = rii.release_claim
    monkeypatch.setattr(rii, "release_claim", lambda h: (at_release.append(rii.ack_path(h).exists()), real_release(h)))
    runner.handle_restart_if_idle_request()
    assert not rii.claimed_path(home).exists() and not rii.request_path(home).exists()
    assert runner._restart_task_started is False
    if how in ("refused_busy", "invalid", "untrusted"):
        assert at_release == [True], "the marker went before the refusal was durable"
    else:
        assert _ack(home) is None or _ack(home)["result"] != "accepted"


def test_a_stale_marker_from_a_crashed_gateway_is_replaced_by_the_next_claim(home, hold):
    rii.claimed_path(home).write_text(json.dumps({"request_id": "old"}))
    body = _request(home, hold)
    _put(home, body)
    raw, problem = rii.claim_request(home)
    assert problem is None and json.loads(raw)["request_id"] == body["request_id"]
    assert json.loads(rii.claimed_path(home).read_text())["request_id"] == body["request_id"]
    rii.release_claim(home)
    assert not rii.claimed_path(home).exists()


def test_the_protocol_is_version_2_so_a_client_of_the_unmarked_version_1_is_refused_and_never_trusts_a_vanished_claim(
        home, hold):
    """Version 1 unlinked the claim before deciding, so "claim gone, no ack" proved nothing there. A requester
    that relies on the marker must only ever talk to a gateway that keeps it: the version is the gate, both ways."""
    assert rii.PROTOCOL_VERSION == 2
    runner = _threaded_runner()
    _put(home, _request(home, hold, schema=1))
    runner.handle_restart_if_idle_request()
    ack = _ack(home)
    assert ack["result"] == "refused_invalid" and runner._restart_task_started is False
