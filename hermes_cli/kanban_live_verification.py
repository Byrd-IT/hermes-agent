"""The ``requires_live_verification`` completion gate: its refusal reason and its refusal write.

``kanban_db.complete_task`` reports every refusal as a bare False; callers (the kanban_complete tool,
``hermes kanban complete``) use :func:`live_verification_refusal` to say which gate refused.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from hermes_cli.kanban_db_connect import write_txn

REFUSAL = (
    "{task_id} requires live verification (completion_state is {state}). Record it with "
    "kanban_record_completion_state(state='verified', evidence={{'proof': '<what was observed live>'}}), "
    "then complete again. Operator escape hatch: a human or orchestrator (no HERMES_KANBAN_TASK) may "
    "record state 'verified' with a proof naming the waiver; tests and merges never satisfy this gate."
)


def live_verification_refusal(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Why completing ``task_id`` is refused by this gate, or None when it is not the reason."""
    row = conn.execute(
        "SELECT completion_state, requires_live_verification FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    if not row or not bool(row["requires_live_verification"]) or row["completion_state"] == "verified":
        return None
    return REFUSAL.format(task_id=task_id, state=row["completion_state"] or "unset")


def refuse_unverified_completion(conn: sqlite3.Connection, task_id: str) -> bool:
    """True (after recording the refusal event) when the gate refuses. Runs before the PR acceptance
    is prepared, because preparing it binds the PR and runs gh: none of that may outlive a refusal."""
    from hermes_cli.kanban_db import _append_event

    if not live_verification_refusal(conn, task_id):
        return False
    with write_txn(conn):
        state = conn.execute("SELECT completion_state FROM tasks WHERE id = ?", (task_id,)).fetchone()[0]
        _append_event(conn, task_id, "completion_refused_missing_live_verification",
                      {"completion_state": state})
    return True
