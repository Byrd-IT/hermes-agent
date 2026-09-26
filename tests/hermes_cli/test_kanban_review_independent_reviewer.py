"""The review lane must not spawn a card's implementer to review its own work
when ``kanban.default_reviewer`` is configured (t_cc7d7bfd / t_ff7d8625 self-approvals)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

INSTALLED = {"app-coder", "code-reviewer"}


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(profmod, "profile_exists", lambda name: name in INSTALLED)
    return home


def _config(monkeypatch: pytest.MonkeyPatch, **kanban) -> None:
    import hermes_cli.config as cfgmod

    cfg = {"kanban": {"review_dispatch": True, **kanban}}
    monkeypatch.setattr(cfgmod, "load_config", lambda *a, **k: cfg)


def _self_review_card(conn, implementer: str = "app-coder", reviewer=None) -> str:
    """A card an implementer worked and handed to review without naming a
    different reviewer — the shape of the self-approved incidents."""
    tid = kb.create_task(conn, title="fix", assignee=implementer)
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None
    assert kb.request_review(
        conn, tid, summary="done", reviewer=reviewer, expected_run_id=claimed.current_run_id,
    )
    return tid


def _spawned_as(captured):
    def spawn(task, workspace):
        captured.append(task.assignee)
        return None
    return spawn


def _claimed_profile(conn, tid) -> str:
    return conn.execute(
        "SELECT profile FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1", (tid,),
    ).fetchone()["profile"]


def test_unset_default_reviewer_keeps_legacy_self_review(kanban_home, monkeypatch) -> None:
    _config(monkeypatch)
    captured: list = []
    with kbc.connect() as conn:
        tid = _self_review_card(conn)
        res = kbd.dispatch_once(conn, spawn_fn=_spawned_as(captured))
    assert [t for t, *_ in res.spawned] == [tid]
    assert captured == ["app-coder"]
    assert not res.reviewer_reassigned and not res.skipped_self_review


def test_default_reviewer_takes_self_review_card(kanban_home, monkeypatch) -> None:
    _config(monkeypatch, default_reviewer="code-reviewer")
    captured: list = []
    with kbc.connect() as conn:
        tid = _self_review_card(conn)
        res = kbd.dispatch_once(conn, spawn_fn=_spawned_as(captured))
        task = kb.get_task(conn, tid)
        assigned = [
            json.loads(r["payload"]) for r in conn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'assigned'", (tid,),
            )
        ]
        claimer = _claimed_profile(conn, tid)
    assert captured == ["code-reviewer"]
    assert res.reviewer_reassigned == [(tid, "app-coder", "code-reviewer")]
    assert task.assignee == "code-reviewer" and task.status == "running"
    assert claimer == "code-reviewer"
    assert assigned[-1] == {
        "assignee": "code-reviewer", "from": "app-coder", "source": "kanban.default_reviewer",
    }


def test_changes_requested_routes_back_to_implementer(kanban_home, monkeypatch) -> None:
    """Reassigning to the default reviewer must not lose the implementer:
    request_changes returns the card to the profile that did the work, and the
    re-review goes back to the same independent reviewer."""
    _config(monkeypatch, default_reviewer="code-reviewer")
    with kbc.connect() as conn:
        tid = _self_review_card(conn)
        kbd.dispatch_once(conn, spawn_fn=lambda t, w: None)
        run_id = kb.get_task(conn, tid).current_run_id
        ok, who = kb.request_changes(conn, tid, reason="regression", expected_run_id=run_id)
        assert ok and who == "app-coder"
        assert kb.get_task(conn, tid).assignee == "app-coder"
        rework = kb.claim_task(conn, tid)
        assert kb.request_review(conn, tid, summary="fixed", expected_run_id=rework.current_run_id)
        assert kb.get_task(conn, tid).assignee == "code-reviewer"


def test_implementer_who_is_default_reviewer_waits_for_human(kanban_home, monkeypatch) -> None:
    _config(monkeypatch, default_reviewer="code-reviewer")
    with kbc.connect() as conn:
        tid = _self_review_card(conn, implementer="code-reviewer")
        res = kbd.dispatch_once(conn, spawn_fn=lambda t, w: pytest.fail("self-review spawned"))
        task = kb.get_task(conn, tid)
    assert res.skipped_self_review == [tid]
    assert task.status == "review" and task.assignee == "code-reviewer"


def test_explicit_independent_reviewer_is_untouched(kanban_home, monkeypatch) -> None:
    """``reviewer=`` naming another profile is already independent; the
    default reviewer must not override the explicit choice."""
    INSTALLED.add("security-reviewer")
    try:
        _config(monkeypatch, default_reviewer="code-reviewer")
        captured: list = []
        with kbc.connect() as conn:
            tid = _self_review_card(conn, reviewer="security-reviewer")
            res = kbd.dispatch_once(conn, spawn_fn=_spawned_as(captured))
        assert captured == ["security-reviewer"]
        assert not res.reviewer_reassigned
        assert [t for t, *_ in res.spawned] == [tid]
    finally:
        INSTALLED.discard("security-reviewer")


def test_explicit_self_reviewer_is_rerouted(kanban_home, monkeypatch) -> None:
    """An implementer naming ITSELF as reviewer is still a self-review."""
    _config(monkeypatch, default_reviewer="code-reviewer")
    captured: list = []
    with kbc.connect() as conn:
        _self_review_card(conn, reviewer="app-coder")
        kbd.dispatch_once(conn, spawn_fn=_spawned_as(captured))
    assert captured == ["code-reviewer"]


def test_uninstalled_default_reviewer_falls_back_to_legacy(kanban_home, monkeypatch) -> None:
    _config(monkeypatch, default_reviewer="no-such-profile")
    captured: list = []
    with kbc.connect() as conn:
        tid = _self_review_card(conn)
        res = kbd.dispatch_once(conn, spawn_fn=_spawned_as(captured))
        task = kb.get_task(conn, tid)
    assert captured == ["app-coder"]
    assert task.assignee == "app-coder"
    assert not res.reviewer_reassigned


def test_dry_run_reports_reroute_without_writing(kanban_home, monkeypatch) -> None:
    _config(monkeypatch, default_reviewer="code-reviewer")
    with kbc.connect() as conn:
        tid = _self_review_card(conn)
        res = kbd.dispatch_once(conn, dry_run=True)
        task = kb.get_task(conn, tid)
    assert res.reviewer_reassigned == [(tid, "app-coder", "code-reviewer")]
    assert (tid, "code-reviewer", "") in res.spawned
    assert task.status == "review" and task.assignee == "app-coder"


def test_reroute_honours_reviewer_per_profile_cap(kanban_home, monkeypatch) -> None:
    """The cap is checked against the profile actually spawned (the default
    reviewer), not the implementer the card was assigned to."""
    _config(monkeypatch, default_reviewer="code-reviewer")
    with kbc.connect() as conn:
        busy = kb.create_task(conn, title="busy", assignee="code-reviewer")
        assert kb.claim_task(conn, busy) is not None
        tid = _self_review_card(conn)
        res = kbd.dispatch_once(
            conn, spawn_fn=lambda t, w: pytest.fail("cap ignored"), max_in_progress_per_profile=1,
        )
    assert (tid, "code-reviewer", 1) in res.skipped_per_profile_capped


def test_default_reviewer_config_key_is_registered() -> None:
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert "default_reviewer" in DEFAULT_CONFIG["kanban"]
    assert not DEFAULT_CONFIG["kanban"]["default_reviewer"]  # legacy by default
