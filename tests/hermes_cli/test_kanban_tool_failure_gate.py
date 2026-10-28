"""Duplicate tool-failure cards are refused unless they cite research (Byrd-IT fork carry)."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


def _connect(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return kbc.connect_closing(kb.kanban_db_path(board="tool-failure-gate"))


@pytest.mark.parametrize("title,body", [
    ("Tool failure: web_search timed out", "It failed twice."),
    ("Tool/service failure in browser", None),
    ("tool_call batch rejected for local tools", "needs a fix"),
    ("Something else entirely", "calls takes exactly one entry for local tools"),
])
def test_unresearched_failure_card_is_refused(tmp_path, monkeypatch, title, body):
    with _connect(tmp_path, monkeypatch) as conn:
        with pytest.raises(ValueError, match="no research cited"):
            kb.create_task(conn, title=title, body=body)


@pytest.mark.parametrize("body", [
    "Searched the KB for web_search timeout: no hits.",
    "KB id: abcd1234efgh checked, documented workaround applies.",
    "memory_search for web_search timeout came back empty.",
    "memory id: 0123456789abcdef shows no workaround.",
])
def test_researched_failure_card_is_accepted(tmp_path, monkeypatch, body):
    with _connect(tmp_path, monkeypatch) as conn:
        assert kb.create_task(conn, title="Tool failure: web_search timed out", body=body)


def test_ordinary_cards_are_untouched(tmp_path, monkeypatch):
    with _connect(tmp_path, monkeypatch) as conn:
        assert kb.create_task(conn, title="Upgrade the tool registry", body="Plain work, mentions tools.")
        assert kb.create_task(conn, title="Fix the batch importer", body="batch jobs")
