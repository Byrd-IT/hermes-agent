"""A live-verification refusal explains itself and leaves no acceptance behind (Byrd-IT carry)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_pr_acceptance_store as acceptance_store
from hermes_cli.kanban_live_verification import live_verification_refusal


@pytest.fixture
def conn(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    with kbc.connect() as c:
        yield c


def test_refusal_reason_names_the_unmet_gate_and_the_escape_hatch(conn):
    tid = kb.create_task(conn, title="release", requires_live_verification=True)
    reason = live_verification_refusal(conn, tid)
    assert "requires live verification" in reason and "kanban_record_completion_state" in reason
    assert "escape hatch" in reason and "unset" in reason
    kb.record_completion_state(conn, tid, "tested", {"proof": "42 passed"})
    assert "completion_state is tested" in live_verification_refusal(conn, tid)
    kb.record_completion_state(conn, tid, "verified", {"proof": "live endpoint returned 200"})
    assert live_verification_refusal(conn, tid) is None


def test_no_reason_for_cards_without_the_gate(conn):
    assert live_verification_refusal(conn, kb.create_task(conn, title="plain")) is None
    assert live_verification_refusal(conn, "t_missing") is None


def test_kanban_complete_error_shows_the_refusal_reason(conn):
    from tools import kanban_tools as tools
    tid = kb.create_task(conn, title="release", requires_live_verification=True)
    out = json.loads(tools._handle_complete({"task_id": tid, "summary": "tests passed"}))
    assert "requires live verification" in out["error"]
    assert "kanban_record_completion_state" in out["error"]
    assert kb.get_task(conn, tid).status == "ready"


def test_refused_completion_saves_no_acceptance_and_does_not_run_the_pr_check(conn, monkeypatch):
    calls = []
    monkeypatch.setattr(acceptance_store, "prepare_acceptance", lambda *a, **k: calls.append("prepare"))
    monkeypatch.setattr(acceptance_store, "record_acceptance", lambda *a, **k: calls.append("record") or True)
    tid = kb.create_task(conn, title="release", requires_live_verification=True)
    assert kb.complete_task(conn, tid, summary="tests passed") is False
    assert calls == []
    refusals = [e for e in kb.list_events(conn, tid) if e.kind == "completion_refused_missing_live_verification"]
    assert len(refusals) == 1


def test_verified_card_still_runs_acceptance_and_completes(conn, monkeypatch):
    calls = []
    monkeypatch.setattr(acceptance_store, "prepare_acceptance", lambda *a, **k: calls.append("prepare"))
    tid = kb.create_task(conn, title="release", requires_live_verification=True)
    kb.record_completion_state(conn, tid, "verified", {"proof": "operator waiver: checked live"})
    assert kb.complete_task(conn, tid, summary="done") is True
    assert calls == ["prepare"]


def test_a_done_card_keeps_its_real_failure_reason(conn):
    """A terminal card that still carries the flag is not told to record verification."""
    from tools import kanban_tools as tools
    tid = kb.create_task(conn, title="release", requires_live_verification=True)
    kb.record_completion_state(conn, tid, "verified", {"proof": "live"})
    assert kb.complete_task(conn, tid, summary="done")
    kb.record_completion_state(conn, tid, "tested", {"proof": "re-tested"})
    out = json.loads(tools._handle_complete({"task_id": tid, "summary": "again"}))
    assert "requires live verification" not in out["error"]
