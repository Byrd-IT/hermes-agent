"""Run-history streak counters the dispatcher's failure accounting reads (split out of kanban_db_dispatch)."""

from __future__ import annotations

import sqlite3

# Closed runs to walk when counting the streak; it trips at a handful anyway.
_PROTOCOL_VIOLATION_SCAN_LIMIT = 50


def _protocol_violation_streak(conn: sqlite3.Connection, task_id: str) -> int:
    """Count the task's trailing run of clean-exit protocol violations.

    Walks closed runs newest-first (including the one ``detect_crashed_workers``
    just closed). ``rate_limited`` runs are neutral and skipped (a quota wall
    says nothing about the task); any other closed run breaks the streak, so
    the budget counts ONLY protocol violations. Violations are recognized by the
    ``protocol_violation`` run-metadata marker, with the error text as fallback
    for runs recorded before the marker existed.
    """
    from hermes_cli import kanban_db as _kb
    from hermes_cli.kanban_db_dispatch import WORKER_OUTPUT_SEP
    streak = 0
    rows = conn.execute(
        "SELECT outcome, error, metadata FROM task_runs "
        "WHERE task_id = ? AND ended_at IS NOT NULL "
        "ORDER BY id DESC LIMIT ?",
        (task_id, _PROTOCOL_VIOLATION_SCAN_LIMIT),
    ).fetchall()
    for row in rows:
        outcome = row["outcome"] or ""
        if outcome == "rate_limited":
            continue
        head = (row["error"] or "").partition(WORKER_OUTPUT_SEP)[0]  # never the worker's own words
        if outcome == "crashed" and (
                _kb._json_dict(row["metadata"]).get("protocol_violation") or "protocol violation" in head):
            streak += 1
            continue
        break
    return streak


# Consecutive restart-attributed losses of one card before it is held for an operator: an owner
# that never settles must end in a visible state, not an endless silent retry.
OWNER_RESTART_RETRY_LIMIT = 5


def owner_restart_streak(conn: sqlite3.Connection, task_id: str) -> int:
    """Newest-first run of this card's attempts that ended on an owner restart/drain.

    Only runs that ended after the card's latest ``unblocked`` event count: an operator who
    unblocked an "owner did not return" card starts it with a fresh bound.
    """
    from hermes_cli import kanban_db as _kb
    since = conn.execute(
        "SELECT COALESCE(MAX(created_at), 0) FROM task_events WHERE task_id = ? AND kind = 'unblocked'",
        (task_id,)).fetchone()[0]
    rows = conn.execute(
        "SELECT metadata FROM task_runs WHERE task_id = ? AND ended_at IS NOT NULL AND ended_at > ? "
        "ORDER BY id DESC LIMIT ?", (task_id, since, OWNER_RESTART_RETRY_LIMIT + 1)).fetchall()
    streak = 0
    for row in rows:
        if not _kb._json_dict(row["metadata"]).get("owner_restart"):
            break
        streak += 1
    return streak
