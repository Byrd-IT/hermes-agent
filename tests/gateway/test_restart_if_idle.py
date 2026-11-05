"""Conditional restart ("restart if idle") decided INSIDE the running gateway (protocol ``restart-if-idle/2``).

An out-of-process updater cannot recall a SIGUSR1 and cannot see every kind of work, so the question "is the
gateway idle?" and the admission freeze must be one synchronous step on the gateway's own loop thread. The
request is a short-lived file bound to the gateway's pid + start time, authenticated by the live scoped ESTOP hold
the requester owns; the answer is an acknowledgement file. No request (or a malformed / stale / foreign one) never
restarts anything.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent import admission_gate, estop
from gateway import restart_if_idle as rii
from gateway import status
from gateway.config import Platform
from gateway.run import GatewayRunner
from tests.gateway.restart_test_helpers import make_restart_runner

OWNER = "byrdit-live-update"
BG = list(estop.BACKGROUND_SCOPES)


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(status, "_runtime_capabilities_cache", status._UNSET, raising=False)
    monkeypatch.setattr(status, "_runtime_status_state", None)
    monkeypatch.setattr(status, "_runtime_status_state_path", None)
    monkeypatch.setattr(rii, "_ACTIVE", False)
    return tmp_path


@pytest.fixture
def hold(home):
    """A live scoped hold with the turns scope: what the updater owns in its final window."""
    return estop.engage_scoped(BG + [estop.SCOPE_TURNS], owner=OWNER, ttl_s=120, reason="test", home=home)


def _me():
    pid = os.getpid()
    return pid, status.get_process_start_time(pid)


def _request(home, hold_, *, now=None, ttl=10.0, **over):
    pid, start = _me()
    now = time.time() if now is None else now
    body = {"schema": rii.PROTOCOL_VERSION, "kind": "restart_if_idle", "request_id": "r" + os.urandom(6).hex(), "owner": OWNER,
            "token_sha256": rii.token_digest(hold_["token"]), "pid": pid, "start_time": start,
            "issued_at": now, "expires_at": now + ttl}
    body.update(over)
    return {k: v for k, v in body.items() if v is not _DROP}


_DROP = object()


def _put(home, body, *, raw=None, mode=0o600):
    path = rii.request_path(home)
    path.write_text(raw if raw is not None else json.dumps(body))
    os.chmod(path, mode)
    return path


def _ack(home):
    p = rii.ack_path(home)
    return json.loads(p.read_text()) if p.exists() else None


def _runner():
    runner, _adapter = make_restart_runner()
    runner.stop = AsyncMock()
    runner._launch_detached_restart_command = AsyncMock()
    runner._profile_adapters = {}
    runner._active_voice_session_count = lambda: 0
    runner._running_cron_job_count = lambda: 0
    runner._active_api_worker_count = lambda: 0
    runner.handle_restart_if_idle_request = GatewayRunner.handle_restart_if_idle_request.__get__(runner)
    return runner


@pytest.fixture(autouse=True)
def _no_queued_events(monkeypatch):
    monkeypatch.setattr(GatewayRunner, "_queued_events", property(lambda self: self.__dict__.setdefault("_qe", {})))


# ── the owner-bound lookup in estop ──────────────────────────────────────────


def test_estop_finds_the_live_hold_by_token_digest_without_returning_the_token(hold):
    found = estop.live_hold_by_token_digest(rii.token_digest(hold["token"]))
    assert found is not None and found["owner"] == OWNER and estop.SCOPE_TURNS in found["scopes"]
    assert "token" not in found


def test_estop_digest_lookup_is_none_for_a_wrong_digest_a_full_pause_and_an_expired_hold(home, hold):
    assert estop.live_hold_by_token_digest(rii.token_digest("not-the-token")) is None
    estop.disengage()
    estop.engage(reason="operator")
    assert estop.live_hold_by_token_digest(rii.token_digest(hold["token"])) is None


# ── claiming a request: atomic, trusted files only ───────────────────────────


def test_no_request_file_claims_nothing(home):
    assert rii.claim_request(home) is None


def test_claim_returns_the_bytes_and_removes_the_file(home, hold):
    _put(home, _request(home, hold))
    raw, problem = rii.claim_request(home)
    assert problem is None and json.loads(raw)["kind"] == "restart_if_idle"
    assert not rii.request_path(home).exists()


def test_a_request_revoked_by_the_requester_before_the_claim_is_never_acted_on(home, hold):
    """The requester withdraws by unlinking; whoever's rename/unlink succeeds first wins."""
    path = _put(home, _request(home, hold))
    path.unlink()
    assert rii.claim_request(home) is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_group_or_world_writable_request_is_not_trusted(home, hold):
    _put(home, _request(home, hold), mode=0o666)
    raw, problem = rii.claim_request(home)
    assert raw is None and "writable" in problem
    assert not rii.request_path(home).exists()          # consumed: it must not linger


@pytest.mark.skipif(os.name == "nt", reason="symlinks")
def test_symlink_in_place_of_the_request_is_not_followed(home, hold, tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps(_request(home, hold)))
    rii.request_path(home).symlink_to(target)
    raw, problem = rii.claim_request(home)
    assert raw is None and "regular file" in problem
    assert target.exists()                               # the link's target is untouched


def test_oversize_request_is_refused_not_parsed(home, hold):
    _put(home, None, raw=" " * (rii.MAX_REQUEST_BYTES + 10))
    raw, problem = rii.claim_request(home)
    assert raw is None and "large" in problem


# ── validation: every defect is a refusal, never a restart ───────────────────


def _validate(home, hold_, body=None, raw=None, **kw):
    pid, start = _me()
    args = dict(pid=pid, start_time=start, now=time.time())
    args.update(kw)
    if raw is None:
        raw = json.dumps(body if body is not None else _request(home, hold_)).encode()
    return rii.validate_request(raw, **args)


def test_a_well_formed_request_for_this_process_is_valid(home, hold):
    req, why = _validate(home, hold)
    assert why is None and req["request_id"]


@pytest.mark.parametrize("raw", [b"", b"not json", b"[]", b"null", b'"x"', b"{}", b"{\"schema\": 1}"])
def test_malformed_requests_are_refused(home, hold, raw):
    req, why = _validate(home, hold, raw=raw)
    assert req is None and why


@pytest.mark.parametrize("field,value", [
    ("schema", 1), ("schema", 3), ("schema", True), ("schema", "1"), ("kind", "restart"), ("request_id", ""), ("request_id", 5),
    ("request_id", "x" * 200), ("owner", ""), ("owner", None), ("token_sha256", "abc"), ("token_sha256", None),
    ("pid", 1), ("pid", True), ("pid", "1"), ("start_time", 12345), ("start_time", None),
    ("issued_at", "now"), ("expires_at", float("inf")), ("expires_at", None),
])
def test_each_bad_field_refuses(home, hold, field, value):
    body = _request(home, hold)
    body[field] = value
    req, why = _validate(home, hold, body)
    assert req is None and why


@pytest.mark.parametrize("field", ["schema", "kind", "request_id", "owner", "token_sha256", "pid", "start_time",
                                   "issued_at", "expires_at"])
def test_a_missing_field_refuses(home, hold, field):
    body = _request(home, hold, **{field: _DROP})
    req, why = _validate(home, hold, body)
    assert req is None and why


def test_pid_reuse_is_refused_by_the_start_time_binding(home, hold):
    _pid, start = _me()
    req, why = _validate(home, hold, _request(home, hold, start_time=start + 1))
    assert req is None and "start_time" in why


def test_unknown_own_start_time_binds_nothing_and_refuses(home, hold):
    req, why = _validate(home, hold, start_time=None)
    assert req is None and "start" in why


def test_expired_request_is_refused_at_decision_time(home, hold):
    body = _request(home, hold, now=time.time() - 100, ttl=10)
    req, why = _validate(home, hold, body)
    assert req is None and "expired" in why


def test_request_valid_when_claimed_but_expired_by_decision_time_is_refused(home, hold):
    body = _request(home, hold, now=1000.0, ttl=10)
    assert _validate(home, hold, body, now=1005.0)[1] is None
    assert "expired" in _validate(home, hold, body, now=1010.0)[1]


def test_a_validity_longer_than_the_cap_is_refused(home, hold):
    body = _request(home, hold, ttl=rii.MAX_REQUEST_TTL_S + 1)
    req, why = _validate(home, hold, body)
    assert req is None and "validity" in why


def test_a_request_issued_in_the_future_is_refused(home, hold):
    body = _request(home, hold, now=time.time() + 600, ttl=10)
    req, why = _validate(home, hold, body)
    assert req is None and "future" in why


def test_request_without_a_matching_live_hold_is_refused(home, hold):
    body = _request(home, hold, token_sha256=rii.token_digest("someone else's token"))
    req, why = _validate(home, hold, body)
    assert req is None and "hold" in why


def test_request_after_the_hold_was_released_is_refused(home, hold):
    body = _request(home, hold)
    estop.release(hold["token"], home=home)
    req, why = _validate(home, hold, body)
    assert req is None and "hold" in why


def test_owner_must_be_the_owner_of_the_live_hold(home, hold):
    req, why = _validate(home, hold, _request(home, hold, owner="someone-else"))
    assert req is None and "owner" in why


def test_the_hold_must_include_the_turns_scope(home):
    bg_only = estop.engage_scoped(BG, owner=OWNER, ttl_s=120, home=home)
    req, why = _validate(home, bg_only)
    assert req is None and "turns" in why


# ── the decision, on a real runner, in one synchronous step ──────────────────


def _decide(runner, home, body):
    _put(home, body)
    runner.handle_restart_if_idle_request()
    return _ack(home)


@pytest.mark.asyncio
async def test_idle_gateway_accepts_freezes_admission_and_restarts(home, hold):
    runner = _runner()
    body = _request(home, hold)
    ack = _decide(runner, home, body)
    assert ack["result"] == "accepted" and ack["request_id"] == body["request_id"]
    assert ack["pid"] == os.getpid()
    assert runner._draining is True and runner._restart_task_started is True and runner._restart_requested is True
    assert runner._restart_via_service is True and runner._restart_detached is False
    await runner._restart_task
    runner.stop.assert_awaited_once_with(restart=True, detached_restart=False, service_restart=True)
    assert not rii.request_path(home).exists()


@pytest.mark.asyncio
async def test_the_acknowledgement_is_written_before_the_restart_task_exists(home, hold):
    """No restart without an ack on disk: the requester can always learn the decision."""
    runner = _runner()
    seen = {}
    real = runner.request_restart

    def spy(**kw):
        seen["ack_when_restart_began"] = _ack(home)
        return real(**kw)

    runner.request_restart = spy
    _decide(runner, home, _request(home, hold))
    assert seen["ack_when_restart_began"]["result"] == "accepted"
    await runner._restart_task


def _busy_cases():
    def agents(r, **_):
        r._running_agents["chat:1"] = MagicMock()

    def pending_sentinel(r, **_):
        from gateway.run import _AGENT_PENDING_SENTINEL
        r._running_agents["chat:2"] = _AGENT_PENDING_SENTINEL

    def managed(r, monkeypatch, **_):
        monkeypatch.setattr("gateway.run_runtime.managed_turn_count", lambda runner: 1)

    def cron(r, **_):
        r._running_cron_job_count = lambda: 2

    def api_run(r, **_):
        r.adapters[Platform.API_SERVER] = SimpleNamespace(active_agent_work_count=lambda: 1)

    def api_worker(r, **_):
        r._active_api_worker_count = lambda: 1

    def deferred(r, **_):
        fut = MagicMock()
        fut.done.return_value = False
        r._deferred_agent_workers = {fut: object()}

    def inbound_guard(r, **_):
        adapter = r.adapters[Platform.TELEGRAM]
        adapter._active_sessions["telegram:9"] = asyncio.Event()

    def pending_message(r, **_):
        r.adapters[Platform.TELEGRAM]._pending_messages["telegram:9"] = object()

    def profile_inbound(r, **_):
        other = SimpleNamespace(_active_sessions={"k": asyncio.Event()}, _pending_messages={},
                                _session_task_is_stale=lambda key: False)
        r._profile_adapters = {"ops": {Platform.TELEGRAM: other}}

    def queued(r, monkeypatch, **_):
        monkeypatch.setattr(GatewayRunner, "_queued_events",
                            property(lambda self: {"telegram:9": [object()]}))

    def bg_task(r, **_):
        t = MagicMock()
        t.done.return_value = False
        t._hermes_supervised_watcher = False
        r._background_tasks.add(t)

    def voice(r, **_):
        r._active_voice_session_count = lambda: 1

    def delegation(r, monkeypatch, **_):
        monkeypatch.setattr("tools.async_delegation.active_count", lambda: 1)

    return [("agents", agents), ("agents", pending_sentinel), ("managed_turns", managed), ("cron", cron),
            ("api_runs", api_run), ("api_workers", api_worker), ("deferred_workers", deferred),
            ("inbound_sessions", inbound_guard), ("pending_messages", pending_message),
            ("inbound_sessions", profile_inbound), ("queued_events", queued), ("background_tasks", bg_task),
            ("voice_sessions", voice), ("delegations", delegation)]


@pytest.mark.parametrize("name,arrange", _busy_cases(), ids=[f"{n}-{i}" for i, (n, _) in enumerate(_busy_cases())])
def test_any_kind_of_active_work_refuses_and_leaves_admission_open(home, hold, monkeypatch, name, arrange):
    runner = _runner()
    arrange(runner, monkeypatch=monkeypatch)
    body = _request(home, hold)
    ack = _decide(runner, home, body)
    assert ack["result"] == "refused_busy" and ack["request_id"] == body["request_id"]
    assert ack["active"][name] >= 1
    assert runner._draining is False and runner._restart_task_started is False and runner._restart_requested is False
    assert getattr(runner, "_restart_task", None) is None


def test_a_wedged_or_restart_safe_unit_still_counts_as_work_here(home, hold):
    """Unlike the after-turn wait, the idle test excludes nothing: a stuck chat or an external cron worker is
    work, so the conditional restart is refused rather than waited for."""
    runner = _runner()
    runner._running_cron_job_count = lambda: 1                 # e.g. a restart-safe external cron run
    runner._wedged_chat_agent_count = lambda: 1
    assert _decide(runner, home, _request(home, hold))["result"] == "refused_busy"


def test_unreadable_activity_source_refuses_as_unknown_never_as_idle(home, hold):
    runner = _runner()

    def boom():
        raise RuntimeError("scheduler unreadable")

    runner._running_cron_job_count = boom
    ack = _decide(runner, home, _request(home, hold))
    assert ack["result"] == "refused_unknown_activity" and "cron" in ack["unknown"]
    assert runner._draining is False and runner._restart_task_started is False


def test_unknown_voice_count_refuses(home, hold):
    runner = _runner()
    runner._active_voice_session_count = lambda: None
    ack = _decide(runner, home, _request(home, hold))
    assert ack["result"] == "refused_unknown_activity" and "voice_sessions" in ack["unknown"]


def test_admission_is_frozen_BEFORE_the_idle_count_is_taken(home, hold):
    """Freeze first, then count: anything admitted after the count would already have been refused."""
    runner = _runner()
    observed = []
    real = rii.idle_probe

    def spy(r):
        observed.append(r._draining)
        return real(r)

    rii_probe = rii.idle_probe
    try:
        rii.idle_probe = spy
        assert _decide(runner, home, _request(home, hold))["result"] == "accepted"
    finally:
        rii.idle_probe = rii_probe
    assert observed == [True]


def test_a_refusal_restores_the_previous_admission_state_exactly(home, hold):
    runner = _runner()
    runner._running_agents["chat:1"] = MagicMock()
    runner._draining = False
    assert _decide(runner, home, _request(home, hold))["result"] == "refused_busy"
    assert runner._draining is False
    # the same on a gateway whose draining flag was already up for another reason: refused, left as it was
    runner2 = _runner()
    runner2._draining = True
    assert _decide(runner2, home, _request(home, hold))["result"] == "refused_not_ready"
    assert runner2._draining is True and runner2._restart_task_started is False


@pytest.mark.asyncio
async def test_a_restart_already_under_way_is_not_joined_or_restarted_again(home, hold):
    runner = _runner()
    assert runner.request_restart(detached=False, via_service=True) is True
    ack = _decide(runner, home, _request(home, hold))
    assert ack["result"] == "refused_not_ready" and "restart" in ack["reason"]
    await runner._restart_task


def test_a_gateway_that_is_not_running_refuses(home, hold):
    runner = _runner()
    runner._running = False
    assert _decide(runner, home, _request(home, hold))["result"] == "refused_not_ready"
    assert runner._draining is False


def test_an_invalid_request_is_acknowledged_as_refused_and_nothing_restarts(home, hold):
    runner = _runner()
    ack = _decide(runner, home, _request(home, hold, token_sha256=rii.token_digest("nope")))
    assert ack["result"] == "refused_invalid" and ack["reason"]
    assert runner._draining is False and runner._restart_task_started is False


@pytest.mark.parametrize("raw", ["", "garbage{", "[]", json.dumps({"schema": rii.PROTOCOL_VERSION, "kind": "restart_if_idle"})])
def test_malformed_marker_is_a_refusal_never_an_unconditional_restart(home, hold, raw):
    runner = _runner()
    _put(home, None, raw=raw)
    runner.handle_restart_if_idle_request()
    assert runner._draining is False and runner._restart_task_started is False
    assert not rii.request_path(home).exists()
    ack = _ack(home)
    assert ack is None or ack["result"] == "refused_invalid"


def test_absent_marker_is_a_noop(home, hold):
    runner = _runner()
    runner.handle_restart_if_idle_request()
    assert runner._draining is False and runner._restart_task_started is False
    assert _ack(home) is None


def test_a_replayed_request_id_is_refused(home, hold):
    runner = _runner()
    runner._running_agents["chat:1"] = MagicMock()
    body = _request(home, hold)
    assert _decide(runner, home, body)["result"] == "refused_busy"
    runner._running_agents.clear()
    ack = _decide(runner, home, body)                 # the same request put back (replay)
    assert ack["result"] == "refused_invalid" and "replay" in ack["reason"]
    assert runner._restart_task_started is False


def test_a_request_that_expires_between_claim_and_decision_is_refused(home, hold, monkeypatch):
    """Time passes while the loop is busy: the decision re-reads the clock after the idle count."""
    runner = _runner()
    body = _request(home, hold, now=time.time(), ttl=5)
    clock = iter([body["issued_at"] + 1, body["expires_at"] + 1, body["expires_at"] + 1, body["expires_at"] + 1])
    monkeypatch.setattr(rii, "_now", lambda: next(clock))
    ack = _decide(runner, home, body)
    assert ack["result"] == "refused_invalid" and "expired" in ack["reason"]
    assert runner._draining is False and runner._restart_task_started is False


def test_if_the_acknowledgement_cannot_be_written_nothing_restarts_and_admission_reopens(home, hold, monkeypatch):
    runner = _runner()

    def no_ack(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(rii, "write_ack", no_ack)
    _put(home, _request(home, hold))
    runner.handle_restart_if_idle_request()
    assert runner._draining is False and runner._restart_task_started is False


def test_ack_file_is_private_and_atomic(home, hold):
    runner = _runner()
    runner._running_agents["c"] = MagicMock()
    _decide(runner, home, _request(home, hold))
    st = rii.ack_path(home).stat()
    assert stat.S_IMODE(st.st_mode) == 0o600
    assert not [p for p in home.iterdir() if p.name.endswith(".tmp")]


# ── admission races on the loop thread ───────────────────────────────────────


@pytest.mark.asyncio
async def test_admission_and_the_decision_never_interleave_on_the_loop(home, hold):
    """An inbound arrival is a loop callback. For every ordering, either the work is admitted first and the
    restart is REFUSED, or the restart is accepted first and the arrival sees the freeze and is refused."""
    outcomes = set()
    loop = asyncio.get_running_loop()
    for i in range(200):
        runner = _runner()
        admission_gate.reset_for_tests()           # one gateway per round: an accepted restart closes its gate for good
        body = _request(home, hold)
        admitted = []

        def arrive():
            if runner._draining:
                admitted.append(False)               # the real inbound gate: refuses while draining
            else:
                runner._running_agents[f"chat:{i}"] = MagicMock()
                admitted.append(True)

        _put(home, body)
        order = (arrive, runner.handle_restart_if_idle_request) if i % 2 else (runner.handle_restart_if_idle_request, arrive)
        for fn in order:
            loop.call_soon(fn)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        ack = _ack(home)
        accepted = ack["result"] == "accepted"
        outcomes.add((bool(i % 2), accepted, admitted[0]))
        assert not (accepted and admitted[0]), "work admitted into a gateway that accepted the restart"
        assert not (not accepted and not admitted[0]), "restart refused although nothing was admitted"
        if accepted:
            await runner._restart_task
        os.unlink(rii.ack_path(home))
    assert outcomes == {(True, False, True), (False, True, False)}


@pytest.mark.asyncio
async def test_work_that_appears_between_writing_the_request_and_the_decision_is_refused(home, hold):
    runner = _runner()
    _put(home, _request(home, hold))                  # the requester believed the gateway idle...
    runner._running_agents["chat:late"] = MagicMock()  # ...and a turn started before the loop got to it
    runner.handle_restart_if_idle_request()
    assert _ack(home)["result"] == "refused_busy"
    assert runner._restart_task_started is False


# ── the poller (no signal: an absent file can never mean "restart") ───────────


def _start(runner):
    loop = asyncio.get_running_loop()
    stop, thread = rii.start_watcher(runner, loop, poll_interval=0.02)
    return stop, thread


async def _wait_for(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


@pytest.mark.asyncio
async def test_poller_picks_up_a_request_and_the_decision_runs_on_the_loop_thread(home, hold):
    runner = _runner()
    main = threading.get_ident()
    seen = []
    real = runner.handle_restart_if_idle_request

    def wrapped():
        seen.append(threading.get_ident())
        return real()

    runner.handle_restart_if_idle_request = wrapped
    stop, thread = _start(runner)
    try:
        _put(home, _request(home, hold))
        assert await _wait_for(lambda: _ack(home) is not None)
        assert _ack(home)["result"] == "accepted" and seen == [main]
        await runner._restart_task
    finally:
        stop.set()
        thread.join(2)
    assert not thread.is_alive()


@pytest.mark.asyncio
async def test_poller_with_no_request_never_acts(home, hold):
    runner = _runner()
    stop, thread = _start(runner)
    try:
        await asyncio.sleep(0.2)
        assert runner._draining is False and runner._restart_task_started is False and _ack(home) is None
    finally:
        stop.set()
        thread.join(2)


@pytest.mark.asyncio
async def test_poller_stops_acting_once_the_gateway_is_draining_and_goes_inactive(home, hold):
    runner = _runner()
    stop, thread = _start(runner)
    try:
        assert rii.is_active()
        stop.set()
        thread.join(2)
        assert not rii.is_active()
    finally:
        stop.set()


# ── capability: only a gateway that is really listening claims it ────────────


def test_capability_is_not_claimed_until_the_poller_is_active(home):
    status.write_runtime_status(gateway_state="running", wait_timeout=10)
    caps = json.loads((home / "gateway_state.json").read_text())["capabilities"]
    assert "restart_if_idle" not in caps


def test_capability_is_published_bound_to_this_process_once_active(home):
    rii.mark_active(True)
    status.write_runtime_status(gateway_state="running", wait_timeout=10)
    rec = json.loads((home / "gateway_state.json").read_text())
    assert rec["capabilities"]["restart_if_idle"] == rii.PROTOCOL_VERSION
    assert rec["capabilities_pid"] == rec["pid"] and rec["capabilities_start_time"] == rec["start_time"]
    assert rec["capabilities"]["scoped_estop"] == estop.SCOPED_ESTOP_VERSION    # the rest is unchanged


def test_capability_disappears_with_the_poller(home):
    rii.mark_active(True)
    status.write_runtime_status(gateway_state="running", wait_timeout=10)
    rii.mark_active(False)
    status.write_runtime_status(active_agents=0, wait_timeout=10)
    assert "restart_if_idle" not in json.loads((home / "gateway_state.json").read_text())["capabilities"]


# ── nothing changed for everything else ──────────────────────────────────────


@pytest.mark.asyncio
async def test_sigusr1_and_slash_restart_are_unconditional_as_before(home, hold):
    from gateway.run import _start_gateway_make_restart_signal_handler

    runner = _runner()
    runner._running_agents["chat:1"] = MagicMock()          # busy
    runner.request_restart = MagicMock(return_value=True)
    _start_gateway_make_restart_signal_handler(runner)()
    runner.request_restart.assert_called_once_with(detached=False, via_service=True)


# ── fault injection and the withdraw/claim race ──────────────────────────────


def test_a_claim_that_fails_is_a_refusal_not_a_restart(home, hold, monkeypatch):
    runner = _runner()
    _put(home, _request(home, hold))
    real = os.replace

    def broken(src, dst):
        if str(src).endswith(rii.REQUEST_NAME):
            raise PermissionError("read-only")
        return real(src, dst)

    monkeypatch.setattr(os, "replace", broken)
    runner.handle_restart_if_idle_request()
    assert runner._draining is False and runner._restart_task_started is False


def test_an_exception_inside_the_decision_never_leaves_admission_frozen(home, hold, monkeypatch):
    runner = _runner()

    def boom(r):
        raise RuntimeError("probe bug")

    monkeypatch.setattr(rii, "idle_probe", boom)
    _put(home, _request(home, hold))
    runner.handle_restart_if_idle_request()          # decide() contains it
    assert runner._draining is False and runner._restart_task_started is False


def test_withdrawing_races_the_claim_with_exactly_one_winner(home, hold):
    """The requester withdraws by unlinking, the gateway claims by renaming. Over many rounds exactly one wins:
    never both (a restart for a request the requester believes withdrawn) and never neither."""
    both = neither = 0
    for _ in range(300):
        path = _put(home, _request(home, hold))
        start = threading.Barrier(2)
        res = {}

        def gateway():
            start.wait()
            res["claimed"] = rii.claim_request(home) is not None

        def requester():
            start.wait()
            try:
                os.unlink(path)
                res["withdrew"] = True
            except FileNotFoundError:
                res["withdrew"] = False

        ts = [threading.Thread(target=gateway), threading.Thread(target=requester)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        both += res["claimed"] and res["withdrew"]
        neither += (not res["claimed"]) and (not res["withdrew"])
        assert not rii.request_path(home).exists()
    assert both == 0 and neither == 0


def test_after_expiry_no_late_decision_can_accept(home, hold, monkeypatch):
    """The requester may stop waiting at expires_at (+ margin): a decision made at or after it is a refusal, so
    'no ack by then' proves this request restarts nothing."""
    runner = _runner()
    body = _request(home, hold, now=time.time(), ttl=3)
    t = {"now": body["issued_at"] + 1}
    monkeypatch.setattr(rii, "_now", lambda: t["now"])
    _put(home, body)
    t["now"] = body["expires_at"]                    # the loop only got to it at the very expiry
    runner.handle_restart_if_idle_request()
    assert _ack(home)["result"] == "refused_invalid" and runner._restart_task_started is False


# ── the real runner, unmodified; the bootstrap wiring; the poller's behaviour on a request it cannot consume ─────


def test_an_unmodified_runner_reads_as_idle_with_every_source_readable():
    """No test double for any work source: the real counting methods all work on a real runner (a source that raised
    would make every conditional restart on a real gateway 'unknown activity', i.e. never accepted)."""
    runner, _ = make_restart_runner()
    assert rii.idle_probe(runner) == {"busy": {}, "unknown": []}


def test_bootstrap_starts_the_poller_only_once_ready_and_stops_it_on_every_exit():
    import inspect
    from gateway import run_bootstrap
    src = inspect.getsource(run_bootstrap)
    ready = src.index("publish_gateway_runtime_ready(runner)")
    attach = src.index("restart_if_idle.attach_watcher(runner, loop)")
    watchdog = src.index("runner._start_systemd_watchdog()", ready)
    assert ready < attach < watchdog                      # the claim appears only after READY
    assert src.count("restart_if_idle.detach_watcher(runner)") >= 2      # normal shutdown tail AND the error exit


@pytest.mark.asyncio
async def test_a_request_that_cannot_be_consumed_is_retried_with_a_growing_pause_not_every_poll(home, hold, monkeypatch):
    runner = _runner()
    calls = []
    runner.handle_restart_if_idle_request = lambda: calls.append(time.monotonic())     # consumes nothing
    _put(home, _request(home, hold))
    stop, thread = rii.start_watcher(runner, asyncio.get_running_loop(), poll_interval=0.02)
    try:
        await asyncio.sleep(1.0)
    finally:
        stop.set()
        thread.join(2)
    assert 2 <= len(calls) <= 8                           # 0.05, 0.1, 0.2, 0.4 ... not 50


def test_a_clean_claim_leaves_only_the_marker_until_released_and_a_second_claim_finds_nothing(home, hold):
    _put(home, _request(home, hold))
    assert rii.claim_request(home)[0] is not None
    assert rii.claim_request(home) is None
    assert [p.name for p in home.iterdir() if "restart_if_idle" in p.name] == [rii.claimed_path(home).name]
    rii.release_claim(home)
    assert not [p for p in home.iterdir() if "restart_if_idle" in p.name]


def test_a_profile_gateway_authenticates_against_the_hold_the_updater_keeps_at_the_fleet_root(tmp_path, monkeypatch):
    """The updater's hold lives at the fleet root; a profile gateway (HERMES_HOME=<root>/profiles/<n>) sees it
    through the same union every other estop gate uses, and its own request files live in ITS home."""
    root = tmp_path / "fleet"
    prof = root / "profiles" / "ops"
    prof.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(prof))
    monkeypatch.setattr(status, "_runtime_status_state", None)
    monkeypatch.setattr(status, "_runtime_status_state_path", None)
    h = estop.engage_scoped(BG + [estop.SCOPE_TURNS], owner=OWNER, ttl_s=120, reason="t", home=root)
    pid, start = _me()
    body = _request(prof, h)
    assert rii.validate_request(json.dumps(body).encode(), pid=pid, start_time=start, now=time.time())[1] is None
    assert rii._home() == prof
