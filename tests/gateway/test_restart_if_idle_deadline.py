"""Conditional restart, round 7: the decision is BOUNDED by the requester's window, and a span handle nests.

Reviewer findings reproduced here with real thread barriers, each asserting EFFECTIVE admission (the runner's own
``_draining`` flag AND the process-wide admission gate -- what an inbound message / cron registration really meets),
not only that a timer fired:

* P1 (core half) -- a claimed request whose acknowledgement write stalls must not keep the gateway closed past the
  requester's window. ``accepted`` is the commit point and is published only before ``expires_at``; a stalled write is
  ABANDONED (admission reopens, nothing restarts, the marker goes with no acknowledgement), and a published ``accepted``
  is never revoked. A decided REFUSAL reopens admission before its (bounded) acknowledgement write.
* P3 -- ``AdmissionGate.enter()`` spans (cron tick, misfire, kanban spawn) did not count toward the entering thread's
  nesting depth, so a nested registration waited ``ADMIT_WAIT_S`` for the decision that was counting its own outer span
  and was then refused: an advanced job was dropped.
* ``request_restart`` raising AFTER the accepted acknowledgement was published must not leave the gateway frozen with no
  restart task.
"""

from __future__ import annotations

import os
import threading
import time
from unittest.mock import MagicMock

import pytest

from agent import admission_gate, estop
from gateway import restart_if_idle as rii
from tests.gateway.test_restart_if_idle import (
    BG, OWNER, _ack, _no_queued_events, _put, _request, _runner, hold, home,
)
from tests.gateway.test_restart_if_idle_admission import (
    _can_dispatch, _decide_in_thread, _patch_cron, _start_tick, _threaded_runner, _until,
)


def _effective_admission(runner) -> dict:
    """What a new arrival really meets: the runner's drain flag (inbound / API / cron-dispatch gates) and the gate that
    worker-thread registrations pass. Both must be open for work to be admitted."""
    gate_state = admission_gate.gate().state
    return {"draining": bool(runner._draining), "gate": gate_state,
            "open": (not runner._draining) and gate_state == admission_gate.OPEN}


def _stall_staging(monkeypatch, release: threading.Event, entered: threading.Event):
    """The acknowledgement's staging (write + fsync of the temporary file) stalls until ``release``."""
    real = rii._stage_ack

    def slow(home_, payload):
        entered.set()
        release.wait(30)
        return real(home_, payload)
    monkeypatch.setattr(rii, "_stage_ack", slow)


def _tmp_files(home):
    return sorted(p.name for p in home.iterdir() if p.name.startswith(".restart_if_idle"))


# ── P3: a span handle (enter) nests like a with-block span ───────────────────

def test_P3_a_span_entered_with_enter_lets_the_same_thread_nest_without_waiting_for_the_decision(monkeypatch):
    monkeypatch.setattr(admission_gate, "ADMIT_WAIT_S", 1.0)       # production: 15 s; the same code path
    g = admission_gate.AdmissionGate()
    res: dict = {}
    entered, go = threading.Event(), threading.Event()

    def worker():
        span = g.enter("cron_tick")
        assert span is not None
        entered.set()
        go.wait(10)
        t0 = time.monotonic()
        with g.admit("cron_register") as ok:                        # nested: must not wait for the decision
            res["ok"], res["took"] = ok, time.monotonic() - t0
        nested = g.enter("cron_register2")
        res["enter_ok"] = nested is not None
        if nested:
            nested.exit()
        span.exit()

    t = threading.Thread(target=worker, daemon=True); t.start()
    assert entered.wait(5)
    assert g.begin_decision() and g.spans() == {"cron_tick": 1}     # the decision counts the outer span, stays DECIDING
    go.set(); t.join(10)
    assert res["ok"] is True and res["took"] < 0.5, res
    assert res["enter_ok"] is True
    g.end_decision(False)
    assert g.spans() == {}


def test_P3_exit_on_another_thread_releases_the_entering_threads_nesting():
    g = admission_gate.AdmissionGate()
    box: dict = {}

    def enter_here():
        box["span"] = g.enter("cron_misfire")
    t = threading.Thread(target=enter_here); t.start(); t.join(5)
    span = box["span"]
    assert span is not None
    # the entering thread is gone, but the handle must still release ITS depth record (not leak a nesting)
    assert span._depth.n == 1
    span.exit(); span.exit()                                        # idempotent
    assert span._depth.n == 0 and g.spans() == {}


def test_P3_a_thread_that_adopts_a_handed_over_span_nests_inside_it(monkeypatch):
    monkeypatch.setattr(admission_gate, "ADMIT_WAIT_S", 1.0)
    g = admission_gate.AdmissionGate()
    span = g.enter("cron_misfire")
    assert g.begin_decision() and g.spans() == {"cron_misfire": 1}
    res: dict = {}

    def fire():
        t0 = time.monotonic()
        with span.adopt():
            with g.admit("cron_run") as ok:
                res["inside"] = (ok, time.monotonic() - t0)
        t1 = time.monotonic()
        with g.admit("cron_run") as ok:                             # no longer adopted: waits, then fails closed
            res["after"] = (ok, time.monotonic() - t1)

    t = threading.Thread(target=fire, daemon=True); t.start(); t.join(10)
    assert res["inside"][0] is True and res["inside"][1] < 0.5, res
    assert res["after"][0] is False and res["after"][1] >= 0.9, res
    g.end_decision(False)
    span.exit()


@pytest.mark.asyncio
async def test_P3_real_cron_tick_registration_nested_in_its_tick_span_is_not_dropped_while_a_decision_runs(home, monkeypatch):
    """Reviewer probe R on the real ticker: the tick's span (``enter``) is COUNTED by the decision; the registration the
    tick makes inside it must go through, not wait ``ADMIT_WAIT_S`` and be refused after the job was advanced."""
    monkeypatch.setattr(admission_gate, "ADMIT_WAIT_S", 1.0)
    runner = _threaded_runner()
    parked, go, started, release = (threading.Event() for _ in range(4))
    _sched, tick_mod = _patch_cron(monkeypatch, parked=parked, go=go, started=started, body_release=release)
    t, _out = _start_tick(tick_mod, runner)
    try:
        assert parked.wait(20)                                       # inside the tick span (after get_due_jobs began)
        assert admission_gate.gate().begin_decision()                # a decision is running (stalled, e.g. its disk)
        t0 = time.monotonic()
        go.set()
        assert started.wait(10), "the nested registration was dropped: the advanced job never started"
        assert time.monotonic() - t0 < 0.9, "the nested registration waited for the decision that counts its own span"
    finally:
        admission_gate.gate().end_decision(False)
        release.set(); go.set(); t.join(10)


# ── P1 core half: the claimed decision is bounded ────────────────────────────

def test_P1_a_stalled_accept_ack_is_abandoned_at_expires_at_admission_reopens_and_nothing_restarts(home, hold, monkeypatch):
    runner = _threaded_runner()
    release, entered = threading.Event(), threading.Event()
    _stall_staging(monkeypatch, release, entered)
    body = _request(home, hold, ttl=1.0)
    _put(home, body)
    t0 = time.time()
    d = _decide_in_thread(runner)
    assert entered.wait(10)
    mid = _effective_admission(runner)                               # during the stall the gateway IS closed ...
    assert mid["open"] is False and mid["draining"] is True and mid["gate"] == admission_gate.DECIDING
    d.join(10)
    took = time.time() - t0
    assert not d.is_alive() and took < 1.0 + 1.5, f"the decision outlived the request's window by {took - 1.0:.1f}s"
    assert took >= 0.9                                                # ... exactly until the window ends, not shorter
    after = _effective_admission(runner)                              # ... and fully open afterwards
    assert after == {"draining": False, "gate": admission_gate.OPEN, "open": True}, after
    assert runner._restart_task_started is False and runner._restart_requested is False
    assert _ack(home) is None                                         # nothing was published ...
    assert not rii.claimed_path(home).exists()                        # ... and the marker went: "decided, not accepted"
    release.set()                                                     # the stalled stage finally returns
    _until(lambda: not _tmp_files(home), "the abandoned stage to delete its temporary file")
    assert _ack(home) is None                                         # it can never publish afterwards
    assert runner._restart_task_started is False


def test_P1_an_ack_that_is_staged_but_whose_deadline_passed_before_the_publish_is_not_published(home, monkeypatch):
    """The decisive clock reading is taken AFTER staging, right before the publishing rename."""
    deadline = 1_000_000.0
    reads = iter([deadline - 10.0, deadline + 1.0])                 # 1st: how long to wait; 2nd: the last look
    monkeypatch.setattr(rii, "_now", lambda: next(reads))
    with pytest.raises(rii.AckAbandoned):
        rii.write_ack(home, {"result": "accepted", "request_id": "x"}, deadline=deadline)
    assert _ack(home) is None and _tmp_files(home) == []
    # control: the same write with the clock still before the deadline is published
    reads2 = iter([deadline - 10.0, deadline - 1.0])
    monkeypatch.setattr(rii, "_now", lambda: next(reads2))
    rii.write_ack(home, {"result": "accepted", "request_id": "x"}, deadline=deadline)
    assert _ack(home)["request_id"] == "x" and _tmp_files(home) == []


@pytest.mark.asyncio
async def test_P1_a_published_accepted_is_irreversible_and_the_gateway_stays_closed(home, hold):
    runner = _runner()
    _put(home, _request(home, hold, ttl=30.0))
    runner.handle_restart_if_idle_request()
    assert _ack(home)["result"] == "accepted"
    st = _effective_admission(runner)
    assert st["open"] is False and st["gate"] == admission_gate.CLOSED and runner._restart_task_started is True
    await runner._restart_task


def test_P1_a_decided_refusal_reopens_admission_before_its_acknowledgement_is_written(home, hold, monkeypatch):
    """Probe R / refusal stall: the decision refused (work exists), the refusal's ack write stalls. Admission must already
    be open: the gate does not make workers wait and ``_draining`` is back, long before the disk answers."""
    runner = _threaded_runner()
    runner._running_agents["chat"] = MagicMock()
    release, entered = threading.Event(), threading.Event()
    _stall_staging(monkeypatch, release, entered)
    _put(home, _request(home, hold, ttl=30.0))
    d = _decide_in_thread(runner)
    assert entered.wait(10)
    assert _effective_admission(runner) == {"draining": False, "gate": admission_gate.OPEN, "open": True}
    got: dict = {}
    t0 = time.monotonic()
    with admission_gate.gate().admit("cron_register") as ok:         # a worker arriving during the stalled write
        got["ok"], got["took"] = ok, time.monotonic() - t0
    assert got["ok"] is True and got["took"] < 0.5
    assert _ack(home) is None and rii.claimed_path(home).exists()     # still undecided FOR THE REQUESTER (marker present)
    release.set(); d.join(10)
    assert _ack(home)["result"] == "refused_busy" and not rii.claimed_path(home).exists()


def test_P1_a_refusal_write_that_outlives_the_requesters_give_up_instant_is_dropped_not_waited_for(home, hold, monkeypatch):
    monkeypatch.setattr(rii, "ACK_MARGIN_S", 0.4)
    runner = _threaded_runner()
    runner._running_agents["chat"] = MagicMock()
    release, entered = threading.Event(), threading.Event()
    _stall_staging(monkeypatch, release, entered)
    _put(home, _request(home, hold, ttl=0.5))
    t0 = time.time()
    d = _decide_in_thread(runner)
    d.join(10)
    took = time.time() - t0
    assert not d.is_alive() and took < 0.9 + 1.5, took                # expires_at + margin, then the loop is free again
    assert _ack(home) is None and not rii.claimed_path(home).exists()
    assert _effective_admission(runner)["open"] is True
    release.set()
    _until(lambda: not _tmp_files(home), "the dropped stage to clean up")
    assert _ack(home) is None


def test_P1_the_protocol_margin_is_the_one_the_requester_waits(home):
    """The core bounds a refusal by the instant the requester stops reading (expires_at + its ACK_MARGIN_S)."""
    assert rii.ACK_MARGIN_S == 3.0


# ── request_restart raising after the accepted ack was published ─────────────

def test_a_request_restart_that_raises_after_accepted_reopens_admission_and_says_so(home, hold, caplog):
    runner = _threaded_runner()

    def boom(*, detached=False, via_service=False):
        runner._restart_requested = runner._restart_task_started = True       # half-set, as the real one does
        raise RuntimeError("no running event loop")
    runner.request_restart = boom
    body = _request(home, hold, ttl=30.0)
    _put(home, body)
    runner.handle_restart_if_idle_request()
    assert _effective_admission(runner) == {"draining": False, "gate": admission_gate.OPEN, "open": True}
    assert runner._restart_requested is False and runner._restart_task_started is False
    ack = _ack(home)
    assert ack["result"] == rii.RESULT_RESTART_FAILED and ack["request_id"] == body["request_id"]
    assert "did NOT happen" in caplog.text
    assert not rii.claimed_path(home).exists()


@pytest.mark.asyncio
async def test_a_request_restart_that_raises_after_creating_the_restart_task_leaves_the_restart_alone(home, hold):
    runner = _threaded_runner()
    task = MagicMock()

    def boom(*, detached=False, via_service=False):
        runner._restart_requested = runner._restart_task_started = runner._draining = True
        runner._restart_task = task
        raise RuntimeError("late failure")
    runner.request_restart = boom
    _put(home, _request(home, hold, ttl=30.0))
    runner.handle_restart_if_idle_request()
    assert _ack(home)["result"] == "accepted"                         # nothing superseded: it IS restarting
    assert runner._draining is True and runner._restart_task_started is True


def test_the_module_documents_the_request_schema_it_enforces():
    assert f'"schema": {rii.PROTOCOL_VERSION},' in rii.__doc__ or '"schema": 2' in rii.__doc__
    assert '"schema": 1' not in rii.__doc__
