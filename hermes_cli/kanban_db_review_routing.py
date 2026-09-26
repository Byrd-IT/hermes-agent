"""Independent-reviewer routing for the Kanban review lane.

A card that enters ``review`` without a named reviewer keeps its implementer
as assignee, so the dispatcher used to spawn the SAME profile to review (and
approve) its own work. ``kanban.default_reviewer`` names the profile that
takes such self-review cards instead:

* unset (``""``, the default): legacy behaviour, the assignee reviews.
* set to an installed profile that is NOT the implementer: the card is
  reassigned to it (an ``assigned`` event with ``source=kanban.default_reviewer``)
  before the review claim, so the gate is independent.
* set, but the implementer IS the default reviewer: no other profile can give
  an independent verdict, so the card is left in ``review`` for a human
  (``DispatchResult.skipped_self_review``).
* set to a profile that is not installed: legacy behaviour with a warning, so
  a typo never parks every review card.
"""

from __future__ import annotations

import sqlite3
from typing import Optional


def default_reviewer() -> Optional[str]:
    """``kanban.default_reviewer`` (canonical profile name) or ``None``.
    Re-read per tick like ``review_dispatch`` (config load is mtime-cached)."""
    try:
        from hermes_cli.config import load_config

        raw = (load_config() or {}).get("kanban", {}).get("default_reviewer")
    except Exception:
        return None
    name = raw.strip() if isinstance(raw, str) else ""
    if not name:
        return None
    try:
        from hermes_cli.profiles import normalize_profile_name

        return normalize_profile_name(name)
    except (ImportError, ValueError):
        return None


def review_implementer(conn: sqlite3.Connection, task_id: str) -> Optional[str]:
    """Implementer recorded by the latest ``review_requested`` event."""
    from hermes_cli import kanban_db as _kb

    event = _kb._latest_event(conn, task_id, "review_requested")
    implementer = _kb._json_dict(_kb._row_get(event, "payload")).get("implementer")
    if not isinstance(implementer, str) or not implementer.strip():
        return None
    return _canonical(implementer)


def _canonical(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    try:
        from hermes_cli.profiles import normalize_profile_name

        return normalize_profile_name(name)
    except (ImportError, ValueError):
        return name.strip().lower() or None


def _installed(name: str) -> bool:
    try:
        from hermes_cli.profiles import profile_exists
    except ImportError:
        return True
    return bool(profile_exists(name))


def route_review_row(
    conn: sqlite3.Connection, task_id: str, assignee: str, *, reviewer: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """Decide who reviews ``task_id``. Pure read.

    Returns ``(assignee_to_spawn, action)`` where ``action`` is ``None``
    (spawn ``assignee`` as-is), ``"reassign"`` (hand the card to the default
    reviewer first) or ``"self_review"`` (refuse; returned assignee is None).
    """
    if reviewer is None:
        return assignee, None
    implementer = review_implementer(conn, task_id)
    if implementer is None or _canonical(assignee) != implementer:
        return assignee, None
    if reviewer == implementer:
        return None, "self_review"
    if not _installed(reviewer):
        from hermes_cli import kanban_db as _kb

        _kb._log.warning(
            "kanban dispatcher: kanban.default_reviewer=%r is not an installed profile; "
            "%s is reviewed by its implementer %r", reviewer, task_id, implementer,
        )
        return assignee, None
    return reviewer, "reassign"


def apply_default_reviewer(
    conn: sqlite3.Connection, task_id: str, implementer: str, reviewer: str,
) -> bool:
    """Persist the reassignment on an unclaimed ``review`` row (CAS on the
    current assignee so a concurrent operator reassign wins)."""
    from hermes_cli import kanban_db as _kb

    with _kb.write_txn(conn):
        cur = conn.execute(
            "UPDATE tasks SET assignee = ? WHERE id = ? AND status = 'review' "
            "AND claim_lock IS NULL AND assignee = ?",
            (reviewer, task_id, implementer),
        )
        if cur.rowcount != 1:
            return False
        _kb._append_event(
            conn, task_id, "assigned",
            {"assignee": reviewer, "from": implementer, "source": "kanban.default_reviewer"},
        )
    return True
