"""A gateway restart/drain is the host's condition, never a card failure.

The incident: a release switch drained and restarted the gateway that owns every kanban worker.
Six running cards lost their owner at once, shared one error fingerprint, tripped the systemic
rule (limit 1, sticky) and stayed blocked; cards spawned during the drain waited out
``OWNER_WAIT_S`` and exited as real failures.

  a) a loss with owner-restart evidence is infrastructure: no failure counted, no systemic trip;
  b) the embedded dispatcher claims nothing while its gateway drains / waits / is update-paused;
  c) genuine crashes keep today's budget and the systemic rule;
  d) an owner that never returns ends in a bounded, visible, sticky block.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import psutil
import pytest

from hermes_cli import kanban_db as kb
from tests.gateway.test_kanban_owner_unavailable import drive_watcher, runner_with
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_db_dispatch_streaks import OWNER_RESTART_RETRY_LIMIT
from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER

NOW = int(time.time())
HOST = kb._claimer_id().split(":", 1)[0]
FINGERPRINT = "6a232414-2dbf-40ab-a534-d9b71db05658:116|4162240"
SILENT = "Kanban owner silent for 181s: Gateway draining: restart_wait"


def _absent_pid(base: int = 3_800_000) -> int:
    pid = base
    while psutil.pid_exists(pid):
        pid += 1
    return pid


class _Board:
    def __init__(self, conn):
        self.conn = conn
        self._n = 0

    def running(self, tid, *, owner_alive=False, worker_lost=None, failures=0):
        """A ``running`` card whose worker is dead; its owner is gone unless ``owner_alive``."""
        self._n += 1
        worker_pid = _absent_pid(3_800_000 + self._n * 50)
        lock = f"{HOST}:{_absent_pid(3_900_000 + self._n * 50)}"
        started = NOW - 120
        run_id = int(self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, claim_lock, claim_expires, worker_pid, started_at) "
            "VALUES (?, 'fan', 'running', ?, ?, ?, ?)", (tid, lock, NOW + 900, worker_pid, started)).lastrowid)
        self.conn.execute(
            "INSERT INTO tasks (id, title, assignee, status, created_at, claim_lock, claim_expires, worker_pid, "
            "worker_started_at, current_run_id, started_at, priority, consecutive_failures) "
            "VALUES (?, ?, 'fan', 'running', ?, ?, ?, ?, ?, ?, ?, 5, ?)",
            (tid, tid, NOW - 86400, lock, NOW + 900, worker_pid, FINGERPRINT, run_id, started, failures))
        self.event(tid, run_id, "spawned", {"pid": worker_pid, "started_at": FINGERPRINT}, started)
        owner_pid = os.getpid() if owner_alive else _absent_pid(3_950_000 + self._n * 50)
        self.event(tid, run_id, "owner_admitted",
                   {"db": str(self.conn_path), "session_id": "s", "pid": owner_pid,
                    "request_id": f"kanban:[\"default\",\"{tid}\",{run_id}]"}, started + 3)
        import sqlite3
        with sqlite3.connect(self.conn_path) as store:
            store.execute("CREATE TABLE IF NOT EXISTS session_admissions "
                          "(target_session_id TEXT, request_id TEXT, status TEXT, outcome TEXT)")
            store.execute("INSERT INTO session_admissions VALUES ('s', ?, 'terminal', 'interrupted')",
                          (f"kanban:[\"default\",\"{tid}\",{run_id}]",))
        if worker_lost:
            self.event(tid, run_id, "worker_lost", {"reason": worker_lost, "run_id": run_id}, started + 24)
        self.conn.commit()
        return run_id

    conn_path = ""

    def ready(self, tid):
        self.conn.execute("INSERT INTO tasks (id, title, assignee, status, created_at, priority) "
                          "VALUES (?, ?, 'fan', 'ready', ?, 5)", (tid, tid, NOW))
        self.conn.commit()

    def prior_restart_runs(self, tid, count):
        for i in range(count):
            self.conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at, metadata) "
                "VALUES (?, 'fan', 'crashed', 'crashed', ?, ?, ?)",
                (tid, NOW - 3000 + i, NOW - 2990 + i, json.dumps({"owner_restart": "x", "infrastructure": True})))
        self.conn.commit()

    def event(self, tid, run, kind, payload, ts):
        self.conn.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,?,?,?)",
                          (tid, run, kind, json.dumps(payload), ts))

    def state(self, tid):
        return dict(self.conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())

    def kinds(self, tid):
        return [r[0] for r in self.conn.execute("SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (tid,))]

    def tick(self):
        return kbd.dispatch_once(self.conn, max_spawn=0, stale_timeout_seconds=0)


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    conn = kbc.connect()
    scratch = _Board(conn)
    scratch.conn_path = tmp_path / "state.db"
    yield scratch
    conn.close()


# --- a) restart evidence -> infrastructure -------------------------------------------------

def test_six_workers_lost_to_an_owner_restart_all_go_back_to_ready(board):
    ids = [f"t_r{i}" for i in range(6)]
    for tid in ids:
        board.running(tid, worker_lost="EOFError")  # identical fingerprint: the old systemic shape
    board.tick()
    for tid in ids:
        row = board.state(tid)
        assert row["status"] == "ready", tid
        assert row["consecutive_failures"] == 0
        assert "gave_up" not in board.kinds(tid)
        run = board.conn.execute("SELECT metadata FROM task_runs WHERE task_id=?", (tid,)).fetchone()
        meta = json.loads(run[0])
        assert meta["infrastructure"] is True and meta["owner_restart"]


def test_a_worker_that_exited_on_owner_silence_is_infrastructure(board):
    run_id = board.running("t_silent", owner_alive=True)
    log = kb.worker_log_path("t_silent")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(f"{SILENT}\n{KANBAN_WORKER_EXIT_TRAILER}1\n", encoding="utf-8")
    board.tick()
    row = board.state("t_silent")
    assert row["status"] == "ready" and row["consecutive_failures"] == 0
    meta = json.loads(board.conn.execute("SELECT metadata FROM task_runs WHERE id=?", (run_id,)).fetchone()[0])
    assert meta["owner_restart"] == "worker exit: owner silent"


def test_repeated_owner_silence_never_counts_toward_the_card_budget(board):
    for _ in range(2):
        board.conn.execute("DELETE FROM tasks WHERE id='t_twice'")
        board.running("t_twice", owner_alive=True, failures=0)
        log = kb.worker_log_path("t_twice")
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(f"{SILENT}\n{KANBAN_WORKER_EXIT_TRAILER}1\n", encoding="utf-8")
        board.tick()
    assert board.state("t_twice")["status"] == "ready"
    assert board.state("t_twice")["consecutive_failures"] == 0


# --- c) real failures unchanged ------------------------------------------------------------

def test_negative_control_a_shared_non_restart_error_still_trips_at_one(board):
    for i in range(3):
        board.running(f"t_p{i}", owner_alive=True)  # live owner: nothing says restart
    board.tick()
    for i in range(3):
        row = board.state(f"t_p{i}")
        assert row["status"] == "blocked" and row["consecutive_failures"] == 1
        trip = board.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='gave_up'", (f"t_p{i}",)).fetchone()
        assert json.loads(trip[0])["sticky"] is True


def test_two_real_crashes_still_block(board):
    board.running("t_real", owner_alive=True, failures=kbd.DEFAULT_FAILURE_LIMIT - 1)
    board.tick()
    assert board.state("t_real")["status"] == "blocked"


def test_a_real_crash_beside_restart_losses_is_not_swept_into_the_systemic_rule(board):
    """Restart losses share a fingerprint with a lone real crash; they must not count toward it."""
    board.running("t_lone", owner_alive=True)
    for i in range(3):
        board.running(f"t_rs{i}", worker_lost="EOFError")
    board.tick()
    assert board.state("t_lone")["status"] == "ready"  # one real crash is below the limit
    assert board.state("t_lone")["consecutive_failures"] == 1


# --- d) bounded ------------------------------------------------------------------------

def test_an_owner_that_never_returns_ends_in_a_sticky_visible_block(board):
    limit = OWNER_RESTART_RETRY_LIMIT
    board.prior_restart_runs("t_gone", limit - 1)
    board.running("t_gone", worker_lost="EOFError")
    board.tick()
    row = board.state("t_gone")
    assert row["status"] == "blocked"
    assert "owner did not return" in row["last_failure_error"]
    trip = json.loads(board.conn.execute(
        "SELECT payload FROM task_events WHERE task_id='t_gone' AND kind='gave_up'").fetchone()[0])
    assert trip["sticky"] is True and trip["owner_did_not_return"] is True
    board.tick()  # recompute_ready must not resume it
    assert board.state("t_gone")["status"] == "blocked"


def test_below_the_bound_the_card_keeps_retrying(board):
    board.prior_restart_runs("t_few", OWNER_RESTART_RETRY_LIMIT - 2)
    board.running("t_few", worker_lost="EOFError")
    board.tick()
    assert board.state("t_few")["status"] == "ready"


def test_simulated_drain_six_running_three_ready_none_blocked(board):
    """The incident: six running workers lose their owner during a drain with three cards ready."""
    running = [f"t_run{i}" for i in range(6)]
    queued = [f"t_new{i}" for i in range(3)]
    for tid in running:
        board.running(tid, worker_lost="EOFError")
    for tid in queued:
        board.ready(tid)
    # The draining gateway's dispatcher claims nothing (b) ...
    drive_watcher(runner_with(_draining=True, session_runtime_descriptor={"drain_reason": "restart_wait"}), ticks=3) \
        .tick_once.assert_not_called()
    assert all(board.state(t)["status"] == "ready" for t in queued)
    # ... and the replacement gateway's first sweep books the six lost workers as infrastructure (a).
    board.tick()
    states = {t: board.state(t) for t in running + queued}
    assert not [t for t, row in states.items() if row["status"] == "blocked"]
    assert all(row["status"] == "ready" and row["consecutive_failures"] == 0 for row in states.values())
    assert not any("gave_up" in board.kinds(t) for t in running)


def test_the_owner_did_not_return_block_and_pause_log_quote_no_worker_text(board, caplog):
    """The new texts are fixed strings: a secret in the worker's own output never reaches them."""
    secret = "Bearer synth-AbC123SECRETtoken https://user:synthpass@host/?api_key=synthKEY987"
    board.prior_restart_runs("t_leak", OWNER_RESTART_RETRY_LIMIT - 1)
    board.running("t_leak", worker_lost="EOFError")
    log = kb.worker_log_path("t_leak")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(f"{secret}\n{KANBAN_WORKER_EXIT_TRAILER}1\n", encoding="utf-8")
    board.tick()
    row = board.state("t_leak")
    assert row["status"] == "blocked" and "owner did not return" in row["last_failure_error"]
    payload = board.conn.execute("SELECT payload FROM task_events WHERE task_id='t_leak' AND kind='gave_up'").fetchone()[0]
    for form in ("synth-AbC123SECRETtoken", "synthpass", "synthKEY987"):
        assert form not in row["last_failure_error"] and form not in payload
    from gateway.kanban_watchers_common import _kanban_owner_unavailable
    with caplog.at_level("DEBUG"):
        _kanban_owner_unavailable(runner_with(_draining=True))
    assert "synth" not in caplog.text


def test_a_card_that_only_prints_the_silence_phrase_mid_run_is_a_real_crash(board):
    board.running("t_quote2", owner_alive=True)
    log = kb.worker_log_path("t_quote2")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(f"{SILENT}\n" + "my own long traceback line\n" * 20 + f"{KANBAN_WORKER_EXIT_TRAILER}1\n",
                   encoding="utf-8")
    board.tick()
    assert board.state("t_quote2")["consecutive_failures"] == 1


@pytest.mark.parametrize("detail", [
    "Gateway draining: restart_wait",
    "Gateway draining: restart_wait (update in progress)",
    "Gateway connection failed; no local fallback",
    "Gateway disconnected; turn outcome is unknown. Re-run is safe.",
    "Gateway disconnected before the request was sent; nothing was sent.",
    "Gateway did not answer kanban.run",
    "Gateway inaccessible: socket gone (ECONNREFUSED)",
    "Gateway not ready",
    "InvalidStatus (HTTP 503)",
    "ConnectionRefusedError",
    "runtime_draining",
    "runtime_retired",
    "Gateway starting: not_ready",
    "Gateway bootstrap timed out",
    "Invalid gateway bootstrap response",
    "Gateway bootstrap identity changed; retry launch",
])
def test_every_client_message_is_recognised_as_owner_silence(board, detail):
    board.running("t_msg", owner_alive=True)
    log = kb.worker_log_path("t_msg")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(f"Kanban owner silent for 181s: {detail}\n{KANBAN_WORKER_EXIT_TRAILER}1\n", encoding="utf-8")
    board.tick()
    row = board.state("t_msg")
    assert row["status"] == "ready" and row["consecutive_failures"] == 0


def test_an_unblocked_owner_did_not_return_card_gets_a_fresh_bound(board):
    from hermes_cli.kanban_db_dispatch_streaks import owner_restart_streak
    board.prior_restart_runs("t_unb", OWNER_RESTART_RETRY_LIMIT)
    assert owner_restart_streak(board.conn, "t_unb") == OWNER_RESTART_RETRY_LIMIT
    board.conn.execute("INSERT INTO tasks (id, title, assignee, status, created_at, priority) "
                       "VALUES ('t_unb', 't', 'fan', 'blocked', ?, 5)", (NOW,))
    assert kb.unblock_task(board.conn, "t_unb")
    board.conn.execute("UPDATE task_events SET created_at = ? WHERE task_id='t_unb' AND kind='unblocked'", (NOW,))
    board.conn.commit()
    assert owner_restart_streak(board.conn, "t_unb") == 0  # the five pre-unblock losses no longer count
    board.conn.execute(
        "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at, metadata) "
        "VALUES ('t_unb', 'fan', 'crashed', 'crashed', ?, ?, ?)",
        (NOW + 1, NOW + 2, json.dumps({"owner_restart": "x", "infrastructure": True})))
    assert owner_restart_streak(board.conn, "t_unb") == 1
