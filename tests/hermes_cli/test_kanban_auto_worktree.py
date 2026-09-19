"""Automatic linked-worktree selection for protected Byrd-IT repositories."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


_BYRD_IT_WEBSITE_REPO = "/home/brandonabyrd/projects/Byrd-IT-Website"


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
    ):
        monkeypatch.delenv(var, raising=False)
    kb._INITIALIZED_PATHS.clear()
    return home


@pytest.mark.parametrize(
    ("title", "body", "kwargs", "expected_kind", "expected_path"),
    [
        (
            "Add customer accounts",
            f"Implement and test a feature branch in {_BYRD_IT_WEBSITE_REPO}/server/src.",
            {},
            "worktree",
            _BYRD_IT_WEBSITE_REPO,
        ),
        (
            "Audit customer accounts",
            f"Read-only review of {_BYRD_IT_WEBSITE_REPO}/server/src; do not edit.",
            {},
            "scratch",
            None,
        ),
        (
            "Research website issue",
            f"Inspect {_BYRD_IT_WEBSITE_REPO}; this card explicitly requests scratch.",
            {"workspace_kind": "scratch"},
            "scratch",
            None,
        ),
        (
            "Fix Hermes task creation",
            "Implement and test a feature branch in /usr/local/lib/hermes-agent/hermes_cli/kanban_db.py.",
            {},
            "worktree",
            None,
        ),
    ],
)
def test_create_task_auto_worktree_protects_repo_bound_mutating_cards(
    fresh_home, title, body, kwargs, expected_kind, expected_path,
):
    """Omitted workspace selection protects both known repos without overriding
    read-only or explicit-scratch callers (#106342)."""
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title=title, body=body, **kwargs)
        task = kb.get_task(conn, task_id)

    assert task is not None
    assert task.workspace_kind == expected_kind
    assert task.workspace_path == expected_path
