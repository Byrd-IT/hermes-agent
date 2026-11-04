"""Scoped emergency stop — agent/estop.py (selective background pause, Telegram-last maintenance).

``hermes pause`` (legacy) is a full stop: every ``check_paused`` component *and* new user turns.
A *scoped hold* is an owned, deadman-bounded sentinel that holds only the named scopes — typically
the background dispatchers (kanban, cron, cron-misfire, cron-webhook, heartbeat, loop) while user
turns keep working — and later, atomically, the user turns too. Everything ambiguous fails safe to a
FULL pause, and composition across profile home + fleet root can only add holds, never remove one.
"""

from __future__ import annotations

import json
import logging
import threading
import time

import pytest

from agent import estop

LOG = logging.getLogger("test.estop.scoped")
BG = estop.BACKGROUND_SCOPES


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    estop._logged_components.clear()
    return tmp_path


@pytest.fixture
def profile_and_root(tmp_path, monkeypatch):
    """HERMES_HOME is a profile dir under a fleet root (the profile-gateway layout)."""
    root = tmp_path / "hermes-root"
    profile = root / "profiles" / "inline-coder"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    estop._logged_components.clear()
    return root, profile


def _write(path, payload):
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")


def _scoped_payload(**over):
    now = time.time()
    body = {
        "schema": 1, "kind": "scoped", "token": "tok-1", "owner": "test",
        "scopes": ["kanban"], "reason": "maintenance",
        "engaged_at": "2026-11-04T00:00:00+00:00", "updated_at": "2026-11-04T00:00:00+00:00",
        "expires_at": now + 600,
    }
    body.update(over)
    return body


# ── the scope vocabulary mirrors the real callsites ─────────────────────────


def test_scope_vocabulary_matches_the_real_check_paused_callsites():
    assert set(BG) == {"kanban", "cron", "cron-misfire", "cron-webhook", "heartbeat", "loop"}
    assert estop.SCOPE_TURNS == "turns"
    assert set(estop.ALL_SCOPES) == set(BG) | {"turns"}


def test_capabilities_identify_scoped_estop_for_the_updater():
    caps = estop.capabilities()
    assert caps["scoped_estop"] == estop.SCOPED_ESTOP_VERSION == 1
    assert set(caps["scopes"]) == set(estop.ALL_SCOPES)
    assert caps["deadman"] is True and caps["owner_token"] is True
    assert caps["max_ttl_s"] == estop.MAX_TTL_S
    json.dumps(caps)  # plain data: the CLI prints it


# ── legacy behaviour is preserved exactly ───────────────────────────────────


def test_legacy_pause_is_still_a_full_pause_of_every_component_and_turns(hermes_home):
    estop.engage(reason="ops")
    for component in (*BG, "some-future-component"):
        assert estop.check_paused(component, LOG) is True
    assert estop.is_held(estop.SCOPE_TURNS) is True
    assert estop.paused_reply() == (
        "⏸️ Hermes is paused (ops). New work is on hold; run `hermes resume` to pick things back up."
    )
    state = estop.get_state()
    assert state["reason"] == "ops" and state["mode"] == "full"


@pytest.mark.parametrize("body", ["", "not json", "[]", '"str"', "null", "{}", '{"scopes": ["kanban"]}'])
def test_legacy_corrupt_or_foreign_sentinel_bodies_are_a_full_pause(hermes_home, body):
    _write(hermes_home / "ESTOP", body)
    assert estop.is_engaged() is True
    assert estop.is_held("kanban") and estop.is_held(estop.SCOPE_TURNS)
    assert estop.get_state()["mode"] == "full"


# ── a scoped hold holds exactly its scopes ──────────────────────────────────


def test_scoped_hold_blocks_only_its_scopes_and_lets_user_turns_through(hermes_home):
    hold = estop.engage_scoped(BG, owner="updater", ttl_s=600, reason="update drain")
    assert hold["token"] and hold["scopes"] == list(BG)
    assert estop.is_engaged() is True
    for component in BG:
        assert estop.check_paused(component, LOG) is True
    assert estop.is_held(estop.SCOPE_TURNS) is False
    assert estop.paused_reply() is None, "user turns must keep flowing while only background is held"
    assert estop.check_paused("some-future-component", LOG) is False


def test_partial_scope_leaves_other_background_components_running(hermes_home):
    estop.engage_scoped(["kanban", "cron"], owner="updater", ttl_s=60)
    assert estop.check_paused("kanban", LOG) and estop.check_paused("cron", LOG)
    for component in ("cron-misfire", "cron-webhook", "heartbeat", "loop"):
        assert estop.check_paused(component, LOG) is False


def test_turns_scope_holds_user_turns_with_a_scoped_notice(hermes_home):
    estop.engage_scoped([*BG, "turns"], owner="updater", ttl_s=60, reason="update restart")
    assert estop.is_held("turns") is True
    notice = estop.paused_reply()
    assert notice is not None and "update restart" in notice
    assert "resume automatically" in notice  # deadman-bounded, not an indefinite operator pause
    assert estop.get_state()["mode"] == "scoped"


def test_get_state_reports_scoped_metadata_but_never_the_token(hermes_home):
    hold = estop.engage_scoped(["kanban"], owner="updater", ttl_s=120, reason="drain")
    state = estop.get_state()
    assert state["mode"] == "scoped" and state["scopes"] == ["kanban"]
    assert state["owner"] == "updater" and state["reason"] == "drain"
    assert state["expires_at"] == pytest.approx(hold["expires_at"])
    assert hold["token"] not in json.dumps(state)


def test_scoped_sentinel_file_keeps_legacy_reason_and_engaged_at_keys(hermes_home):
    """An OLD runtime reading the same file sees an engaged sentinel with its usual keys (and so
    fails safe to a full pause) — which is why the updater must check capabilities first."""
    estop.engage_scoped(["kanban"], owner="updater", ttl_s=60, reason="drain")
    raw = json.loads((hermes_home / "ESTOP").read_text(encoding="utf-8"))
    assert raw["reason"] == "drain" and raw["engaged_at"]


def test_check_paused_logs_once_per_engagement_and_rearms_after_release(hermes_home, caplog):
    caplog.set_level(logging.INFO, logger=LOG.name)
    hold = estop.engage_scoped(["kanban"], owner="updater", ttl_s=60, reason="drain")
    for _ in range(3):
        estop.check_paused("kanban", LOG)
    assert len([r for r in caplog.records if "kanban" in r.getMessage()]) == 1
    estop.release(hold["token"])
    assert estop.check_paused("kanban", LOG) is False
    estop.engage_scoped(["kanban"], owner="updater", ttl_s=60)
    estop.check_paused("kanban", LOG)
    assert len([r for r in caplog.records if "kanban" in r.getMessage()]) == 2


# ── malformed / unknown values fail SAFE to a full pause ────────────────────


@pytest.mark.parametrize(
    "over",
    [
        {"scopes": ["kanban", "not-a-scope"]},
        {"scopes": []},
        {"scopes": "kanban"},
        {"scopes": None},
        {"scopes": ["kanban", 3]},
        {"scopes": [["kanban"]]},
        {"schema": 2},
        {"schema": "1"},
        {"schema": True},
        {"token": ""},
        {"token": None},
        {"owner": ""},
        {"owner": 7},
        {"expires_at": "9999999999"},
        {"expires_at": None},
        {"expires_at": True},
        {"expires_at": float("nan")},
        {"expires_at": float("inf")},
        {"expires_at": [1]},
    ],
)
def test_malformed_scoped_hold_fails_safe_to_a_full_pause(hermes_home, over):
    _write(hermes_home / "ESTOP", _scoped_payload(**over))
    assert estop.is_engaged() is True
    assert estop.is_held("cron") is True and estop.is_held(estop.SCOPE_TURNS) is True
    assert estop.get_state()["mode"] == "full"


def test_a_malformed_scoped_hold_has_no_deadman(hermes_home):
    _write(hermes_home / "ESTOP", _scoped_payload(scopes=["bogus"], expires_at=time.time() - 10))
    assert estop.is_held("cron") is True, "an expired-looking but malformed hold must stay engaged"


def test_unknown_future_kind_is_a_full_pause(hermes_home):
    _write(hermes_home / "ESTOP", _scoped_payload(kind="selective-v2"))
    assert estop.is_held("cron") and estop.is_held(estop.SCOPE_TURNS)


def test_stat_error_still_fails_safe(hermes_home, monkeypatch):
    class _Boom:
        def exists(self):
            raise OSError("permission denied")

    monkeypatch.setattr(estop, "sentinel_path", lambda: _Boom())
    assert estop.is_engaged() is True
    assert estop.is_held("kanban") is True and estop.is_held("turns") is True


@pytest.mark.parametrize(
    "scopes", [[], ["nope"], ["kanban", "nope"], "kanban", None, [1], ["Kanban"], [" kanban"]]
)
def test_engage_scoped_rejects_invalid_scopes(hermes_home, scopes):
    with pytest.raises(estop.InvalidScope):
        estop.engage_scoped(scopes, owner="u", ttl_s=60)
    assert estop.is_engaged() is False


@pytest.mark.parametrize("ttl", [0, -1, None, "60", True, float("nan"), float("inf"), estop.MAX_TTL_S + 1])
def test_engage_scoped_requires_a_bounded_positive_ttl_deadman(hermes_home, ttl):
    with pytest.raises(estop.InvalidScope):
        estop.engage_scoped(["kanban"], owner="u", ttl_s=ttl)
    assert estop.is_engaged() is False


@pytest.mark.parametrize("owner", ["", None, 3, " "])
def test_engage_scoped_requires_an_owner(hermes_home, owner):
    with pytest.raises(estop.InvalidScope):
        estop.engage_scoped(["kanban"], owner=owner, ttl_s=60)


def test_duplicate_scopes_are_normalised_in_canonical_order(hermes_home):
    hold = estop.engage_scoped(["loop", "kanban", "loop", "turns"], owner="u", ttl_s=60)
    assert hold["scopes"] == ["kanban", "loop", "turns"]


# ── deadman ─────────────────────────────────────────────────────────────────


def test_expired_scoped_hold_stops_holding_and_renew_extends_it(hermes_home, monkeypatch):
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(estop, "_now", lambda: clock["now"])
    hold = estop.engage_scoped(["kanban"], owner="u", ttl_s=100)
    clock["now"] += 99
    assert estop.is_held("kanban") is True
    renewed = estop.renew(hold["token"], ttl_s=100)
    assert renewed["expires_at"] == pytest.approx(clock["now"] + 100)
    clock["now"] += 99
    assert estop.is_held("kanban") is True, "renew re-armed the deadman"
    clock["now"] += 2
    assert estop.is_held("kanban") is False and estop.is_engaged() is False
    assert estop.get_state() is None and estop.paused_reply() is None


def test_an_expired_hold_cannot_be_renewed_or_transitioned_only_replaced(hermes_home, monkeypatch):
    clock = {"now": 5_000.0}
    monkeypatch.setattr(estop, "_now", lambda: clock["now"])
    hold = estop.engage_scoped(["kanban"], owner="u", ttl_s=10)
    clock["now"] += 11
    with pytest.raises(estop.HoldLost):
        estop.renew(hold["token"], ttl_s=10)
    with pytest.raises(estop.HoldLost):
        estop.set_scopes(hold["token"], ["turns"])
    assert estop.verify(hold["token"]) is None
    estop.engage_scoped(["cron"], owner="other", ttl_s=10)  # a dead hold never blocks a new one
    assert estop.is_held("cron") and not estop.is_held("kanban")


def test_legacy_pause_has_no_deadman(hermes_home, monkeypatch):
    estop.engage()
    far_future = time.time() + 10 * 365 * 86400
    monkeypatch.setattr(estop, "_now", lambda: far_future)
    assert estop.is_engaged() is True


# ── owned token + atomic transitions ────────────────────────────────────────


def test_release_only_removes_the_owners_hold(hermes_home):
    hold = estop.engage_scoped(["kanban"], owner="updater", ttl_s=60)
    assert estop.release("wrong-token") is False
    assert estop.is_held("kanban") is True
    assert estop.release(hold["token"]) is True
    assert estop.is_engaged() is False
    assert estop.release(hold["token"]) is False  # idempotent, reports nothing to release


def test_engage_scoped_never_overwrites_a_foreign_hold(hermes_home):
    estop.engage(reason="operator incident")
    with pytest.raises(estop.HoldConflict):
        estop.engage_scoped(["kanban"], owner="updater", ttl_s=60)
    assert estop.get_state()["mode"] == "full" and estop.get_state()["reason"] == "operator incident"

    estop.disengage()
    other = estop.engage_scoped(["cron"], owner="someone-else", ttl_s=60)
    with pytest.raises(estop.HoldConflict):
        estop.engage_scoped(["kanban"], owner="updater", ttl_s=60)
    assert estop.verify(other["token"])["scopes"] == ["cron"]


def test_engage_scoped_over_a_corrupt_sentinel_is_a_conflict_not_an_overwrite(hermes_home):
    (hermes_home / "ESTOP").write_text("not json", encoding="utf-8")
    with pytest.raises(estop.HoldConflict):
        estop.engage_scoped(["kanban"], owner="updater", ttl_s=60)
    assert (hermes_home / "ESTOP").read_text(encoding="utf-8") == "not json"


def test_engage_scoped_with_the_same_token_is_an_idempotent_transition(hermes_home):
    first = estop.engage_scoped(["kanban"], owner="u", ttl_s=60, token="my-token")
    again = estop.engage_scoped(["kanban", "cron"], owner="u", ttl_s=60, token="my-token")
    assert first["token"] == again["token"] == "my-token"
    assert again["scopes"] == ["kanban", "cron"]
    assert again["engaged_at"] == first["engaged_at"]


def test_set_scopes_transition_requires_ownership(hermes_home):
    hold = estop.engage_scoped(BG, owner="updater", ttl_s=60)
    with pytest.raises(estop.HoldLost):
        estop.set_scopes("wrong", [*BG, "turns"])
    assert estop.is_held("turns") is False
    moved = estop.set_scopes(hold["token"], [*BG, "turns"], reason="final pause")
    assert estop.is_held("turns") is True and moved["reason"] == "final pause"
    back = estop.set_scopes(hold["token"], BG)  # abort/resume Telegram while background stays held
    assert estop.is_held("turns") is False and estop.is_held("kanban") is True
    assert back["expires_at"] >= moved["expires_at"] - 1


def test_set_scopes_rejects_bad_scopes_and_leaves_the_hold_untouched(hermes_home):
    hold = estop.engage_scoped(["kanban"], owner="u", ttl_s=60)
    with pytest.raises(estop.InvalidScope):
        estop.set_scopes(hold["token"], ["kanban", "bogus"])
    assert estop.get_state()["scopes"] == ["kanban"]


def test_verify_returns_the_live_owned_hold_or_none(hermes_home):
    hold = estop.engage_scoped(["kanban"], owner="u", ttl_s=60)
    assert estop.verify(hold["token"])["owner"] == "u"
    assert estop.verify("nope") is None
    estop.release(hold["token"])
    assert estop.verify(hold["token"]) is None


def test_a_legacy_pause_over_an_owned_hold_strengthens_it_and_the_owner_sees_it_lost(hermes_home):
    hold = estop.engage_scoped(BG, owner="updater", ttl_s=60)
    estop.engage(reason="operator says stop everything")
    assert estop.is_held("turns") is True and estop.get_state()["mode"] == "full"
    assert estop.verify(hold["token"]) is None
    with pytest.raises(estop.HoldLost):
        estop.set_scopes(hold["token"], BG)
    assert estop.release(hold["token"]) is False
    assert estop.is_engaged() is True, "the owner's release must not lift an operator's full pause"


def test_operator_resume_removes_an_owned_hold_and_the_owner_sees_it_lost(hermes_home):
    hold = estop.engage_scoped(BG, owner="updater", ttl_s=60)
    assert estop.disengage() is True
    assert estop.is_engaged() is False
    assert estop.verify(hold["token"]) is None
    with pytest.raises(estop.HoldLost):
        estop.renew(hold["token"], ttl_s=60)


def test_set_scopes_replaces_atomically_user_flow_never_sees_a_gap(hermes_home):
    """A reader hammering the gate while the owner flips background -> background+turns -> background
    must never observe the background scopes released."""
    hold = estop.engage_scoped(BG, owner="updater", ttl_s=300)
    stop, gaps = threading.Event(), []

    def reader():
        while not stop.is_set():
            if not all(estop.is_held(c) for c in BG):
                gaps.append(1)

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    try:
        for i in range(300):
            estop.set_scopes(hold["token"], [*BG, "turns"] if i % 2 else BG)
    finally:
        stop.set()
        for t in threads:
            t.join()
    assert gaps == []


def test_concurrent_engages_have_exactly_one_winner(hermes_home):
    results, barrier = [], threading.Barrier(8)

    def worker(i):
        barrier.wait()
        try:
            results.append(("ok", estop.engage_scoped(["kanban"], owner=f"o{i}", ttl_s=60)["token"]))
        except estop.HoldConflict:
            results.append(("conflict", None))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    winners = [tok for kind, tok in results if kind == "ok"]
    assert len(winners) == 1 and len(results) == 8
    assert estop.verify(winners[0]) is not None


def test_sentinel_is_replaced_not_truncated_no_partial_file_is_ever_visible(hermes_home):
    hold = estop.engage_scoped(BG, owner="u", ttl_s=300)
    path = hermes_home / "ESTOP"
    stop, bad = threading.Event(), []

    def reader():
        while not stop.is_set():
            try:
                body = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                bad.append("missing")
                continue
            try:
                json.loads(body)
            except ValueError:
                bad.append(body[:40])

    t = threading.Thread(target=reader)
    t.start()
    try:
        for i in range(300):
            estop.set_scopes(hold["token"], [*BG, "turns"] if i % 2 else BG)
    finally:
        stop.set()
        t.join()
    assert bad == []
    assert not list(hermes_home.glob(".ESTOP*tmp")), "temp files are cleaned up"


# ── profile home + fleet root composition never weakens a foreign hold ──────


def test_union_of_profile_and_root_scoped_holds(profile_and_root):
    root, profile = profile_and_root
    root_hold = estop.engage_scoped(["kanban"], owner="fleet-updater", ttl_s=60, home=root)
    estop.engage_scoped(["cron"], owner="profile-updater", ttl_s=60)
    assert (root / "ESTOP").exists() and (profile / "ESTOP").exists()
    assert estop.is_held("kanban") and estop.is_held("cron")
    assert estop.is_held("heartbeat") is False
    assert sorted(estop.get_state()["scopes"]) == ["cron", "kanban"]
    estop.release(root_hold["token"], home=root)
    assert estop.is_held("kanban") is False and estop.is_held("cron") is True


def test_a_foreign_full_pause_at_the_root_is_never_weakened_by_a_profile_scoped_hold(profile_and_root):
    root, _profile = profile_and_root
    _write(root / "ESTOP", {"reason": "operator"})
    mine = estop.engage_scoped(["kanban"], owner="profile-updater", ttl_s=60)
    assert estop.is_held("turns") is True and estop.get_state()["mode"] == "full"
    estop.set_scopes(mine["token"], ["cron"])
    assert estop.is_held("turns") is True and estop.is_held("loop") is True
    assert estop.release(mine["token"]) is True
    assert estop.is_engaged() is True, "the root operator pause outlives the profile owner's release"
    assert (root / "ESTOP").exists()


def test_a_foreign_scoped_hold_at_the_root_is_not_removed_or_narrowed_by_a_profile_owner(profile_and_root):
    root, _profile = profile_and_root
    foreign = estop.engage_scoped(BG, owner="fleet-updater", ttl_s=60, home=root)
    mine = estop.engage_scoped(["kanban"], owner="profile-updater", ttl_s=60)
    assert estop.release(foreign["token"]) is False, "wrong home: the profile does not own the root hold"
    estop.release(mine["token"])
    for component in BG:
        assert estop.is_held(component) is True
    assert estop.verify(foreign["token"], home=root) is not None


def test_a_profile_cannot_use_its_token_against_the_root_hold(profile_and_root):
    root, _profile = profile_and_root
    estop.engage_scoped(BG, owner="fleet-updater", ttl_s=60, home=root)
    mine = estop.engage_scoped(["kanban"], owner="profile", ttl_s=60)
    with pytest.raises(estop.HoldLost):
        estop.set_scopes(mine["token"], ["cron"], home=root)
    assert estop.release(mine["token"], home=root) is False
    assert estop.get_state()["scopes"] == sorted(BG, key=estop.ALL_SCOPES.index)


def test_malformed_root_hold_makes_a_profile_scoped_hold_irrelevant_full_pause_wins(profile_and_root):
    root, _profile = profile_and_root
    (root / "ESTOP").write_text("garbage", encoding="utf-8")
    estop.engage_scoped(["kanban"], owner="p", ttl_s=60)
    assert estop.is_held("turns") is True


def test_expired_root_scoped_hold_does_not_hold_but_profile_hold_still_does(profile_and_root):
    root, _profile = profile_and_root
    _write(root / "ESTOP", _scoped_payload(scopes=["cron"], expires_at=time.time() - 5))
    estop.engage_scoped(["kanban"], owner="p", ttl_s=60)
    assert estop.is_held("cron") is False and estop.is_held("kanban") is True


def test_fleet_root_helper_resolves_the_root_sentinel_home(profile_and_root):
    root, profile = profile_and_root
    assert estop.fleet_root().resolve() == root.resolve()
    assert estop.sentinel_path().parent.resolve() == profile.resolve()


# ── snapshot for the CLI / updater ──────────────────────────────────────────


def test_status_snapshot_lists_every_visible_hold_without_tokens(profile_and_root):
    root, profile = profile_and_root
    estop.engage_scoped(["kanban"], owner="fleet", ttl_s=60, home=root)
    _write(profile / "ESTOP", "touch")
    snap = estop.status()
    assert snap["engaged"] is True and snap["mode"] == "full"
    modes = sorted(h["mode"] for h in snap["holds"])
    assert modes == ["full", "scoped"]
    assert "tok" not in json.dumps(snap) and "token" not in json.dumps(snap)
    estop.disengage()
    assert estop.status() == {"engaged": False, "mode": "none", "scopes": [], "holds": []}


# ── expired holds are reaped by readers; foreign-hold detection for the owner ─


def test_reading_reaps_an_expired_scoped_hold_file_so_stale_alerts_cannot_fire(hermes_home):
    _write(hermes_home / "ESTOP", _scoped_payload(expires_at=time.time() - 5))
    assert estop.is_engaged() is False
    assert not (hermes_home / "ESTOP").exists(), "the dead hold's file is removed on first read"


def test_reaping_never_removes_a_live_or_legacy_or_malformed_sentinel(hermes_home):
    for body in (_scoped_payload(), {"reason": "operator"}, "garbage", _scoped_payload(scopes=["bogus"], expires_at=1)):
        _write(hermes_home / "ESTOP", body)
        assert estop.is_engaged() is True
        assert (hermes_home / "ESTOP").exists()
        (hermes_home / "ESTOP").unlink()


def test_reaping_does_not_remove_a_hold_that_replaced_the_expired_one(hermes_home, monkeypatch):
    """Between reading an expired body and unlinking it, the owner's replacement must survive."""
    path = hermes_home / "ESTOP"
    _write(path, _scoped_payload(token="old", expires_at=time.time() - 5))
    real_load = estop._load
    swapped = []

    def load_then_replace(p, **kw):
        hold = real_load(p, **kw)
        if not swapped:  # a fresh hold lands right after the stale read
            swapped.append(1)
            _write(path, _scoped_payload(token="new", scopes=["cron"]))
        return hold

    monkeypatch.setattr(estop, "_load", load_then_replace)
    estop.is_engaged()
    monkeypatch.setattr(estop, "_load", real_load)
    assert path.exists() and estop.is_held("cron")


def test_foreign_holds_lists_everything_that_is_not_the_owners_hold(profile_and_root):
    root, _profile = profile_and_root
    mine = estop.engage_scoped(BG, owner="updater", ttl_s=60)
    assert estop.foreign_holds(mine["token"]) == []
    _write(root / "ESTOP", {"reason": "operator"})
    foreign = estop.foreign_holds(mine["token"])
    assert [h["mode"] for h in foreign] == ["full"] and foreign[0]["path"] == str(root / "ESTOP")
    (root / "ESTOP").unlink()
    other = estop.engage_scoped(["cron"], owner="someone", ttl_s=60, home=root)
    assert [h["owner"] for h in estop.foreign_holds(mine["token"])] == ["someone"]
    estop.release(other["token"], home=root)
    assert estop.foreign_holds(mine["token"]) == []


def test_foreign_holds_reports_a_replaced_own_hold_as_foreign(hermes_home):
    mine = estop.engage_scoped(BG, owner="updater", ttl_s=60)
    estop.engage(reason="operator")
    assert [h["mode"] for h in estop.foreign_holds(mine["token"])] == ["full"]
