"""Shell-variable indirection vs the guards (2026-09-26 incident).

A kanban worker's scratch script sent ``X="rm -rf /home"; $X`` through the REAL terminal tool, under a
profile with ``approvals.single_query_mode: approve``. The hardline floor saw only a quoted
assignment value (data) and a ``$X`` command word, so it returned (False, None). Approve mode
auto-approved the dangerous-pattern and Tirith findings, and the command wiped most of the home
directory.

These tests pin two layers:

* the hardline floor resolves same-command ``NAME=value`` assignments (``;``/``&&`` chains, export,
  arrays, chained variables) and re-checks the substituted command;
* unattended approve mode no longer auto-approves an unresolved ``$VAR`` command word whose
  arguments look destructive, or a Tirith blast-radius block combined with a destructive pattern.

TEST HYGIENE: every assertion here stops at ``check_all_command_guards`` or a pure detector. Nothing
in this file (and nothing anywhere in the suite) may send a must-block control through the real
terminal executor. See CONTRIBUTING.md, "Never execute a must-BLOCK control".
"""

from unittest.mock import patch

import pytest

import tools.approval as approval_module
from gateway.session_context import reset_session_vars
from tools.approval import check_all_command_guards
from tools.approval_detection import detect_dangerous_command, detect_hardline_command
from tools.approval_detection_assignments import (
    collect_shell_assignments, opaque_command_leaders, resolve_shell_assignments,
)
from tools.approval_unattended_floor import tirith_blast_rules, unattended_approve_refusal

INCIDENT_COMMANDS = [
    'X="rm -rf /home"; $X',
    'true && X="rm -rf /home" && $X',
]
_ALLOW_TIRITH = {"action": "allow", "findings": [], "summary": ""}


@pytest.fixture(autouse=True)
def _clean_approval_state():
    approval_module._permanent_approved.clear()
    approval_module.clear_session("default")
    reset_session_vars()
    yield
    approval_module._permanent_approved.clear()
    approval_module.clear_session("default")
    reset_session_vars()


@pytest.fixture
def single_query(monkeypatch):
    """A -q run (nobody to answer a prompt) with the mode chosen per test."""
    monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", "1")
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    for name in ("HERMES_GATEWAY_SESSION", "HERMES_EXEC_ASK", "HERMES_YOLO_MODE", "HERMES_CRON_SESSION"):
        monkeypatch.delenv(name, raising=False)

    def with_mode(mode):
        return patch("tools.approval_context._get_single_query_approval_mode", return_value=mode)
    return with_mode


# ---- hardline floor --------------------------------------------------------------------------

@pytest.mark.parametrize("command", INCIDENT_COMMANDS)
def test_incident_commands_are_hardline(command):
    assert detect_hardline_command(command) == (True, "recursive delete of system directory")


@pytest.mark.parametrize(("command", "description"), [
    ('X=rm; "$X" -rf /', "recursive delete of root filesystem"),
    ('A=rm; B="$A -rf /etc"; $B', "recursive delete of system directory"),
    ('export C="rm -rf /"; ${C}', "recursive delete of root filesystem"),
    ('declare -x C="rm -rf /usr"; $C', "recursive delete of system directory"),
    ("c=(rm -rf /usr); \"${c[@]}\"", "recursive delete of system directory"),
    ('D=/; rm -rf $D', "recursive delete of root filesystem"),
    ('H=/home; rm -rf "$H"', "recursive delete of system directory"),
    ('P=rm; P+=" -rf /boot"; $P', "recursive delete of system directory"),
    ('X="rm -rf /home"\n$X', "recursive delete of system directory"),
    ("bash -c 'X=\"rm -rf /home\"; $X'", "recursive delete of system directory"),
    ("X=\"shutdown -h now\"; $X", "system shutdown/reboot"),
    ("bash <<'EOF'\nX=\"rm -rf /home\"; $X\nEOF", "recursive delete of system directory"),
    ('X="dd if=/dev/zero of=/dev/sda"; $X', "dd to raw block device"),
])
def test_indirect_spellings_are_hardline(command, description):
    assert detect_hardline_command(command) == (True, description)


@pytest.mark.parametrize("command", [
    'X="rm -rf /home"; echo $X',          # the value is printed, never executed
    'X="rm -rf /home"',                   # assignment only
    "X='rm -rf /home'; echo '$X'",        # single quotes: no expansion
    'T=/tmp/x; rm -rf $T',
    'W=/home/brandonabyrd/ws; cd $W && ls',
    'PY=python3; $PY -c "print(1)"',
    'echo "X=rm -rf /home; \\$X"',
])
def test_benign_assignments_stay_off_the_floor(command):
    assert detect_hardline_command(command) == (False, None)


def test_resolution_reports_unchanged_commands_as_none():
    assert resolve_shell_assignments("ls -la") is None
    assert resolve_shell_assignments("echo $HOME") is None
    assert resolve_shell_assignments('X=1; echo $X') == 'X=1; echo 1'
    assert collect_shell_assignments('export A=1 B="two words"; C=3 cmd') == {"A": "1", "B": "two words", "C": "3"}


# ---- combined guard under single_query_mode: approve (the acceptance criterion) --------------

@pytest.mark.parametrize("command", INCIDENT_COMMANDS)
def test_incident_commands_blocked_by_combined_guard_in_approve_mode(single_query, command):
    with single_query("approve"):
        result = check_all_command_guards(command, "local")
    assert result["approved"] is False
    assert "hardline" in result["message"].lower()


@pytest.mark.parametrize("command", INCIDENT_COMMANDS)
def test_incident_commands_blocked_even_under_yolo(monkeypatch, command):
    monkeypatch.setattr(approval_module, "_YOLO_MODE_FROZEN", True)
    assert check_all_command_guards(command, "local")["approved"] is False


def test_unresolved_leader_with_destructive_args_refused_in_approve_mode(single_query):
    # `X=rm; $X -rf ~/.local` resolves to `rm -rf ~/.local`, which is only dangerous (recoverable
    # from backup), so it is the Tirith+destructive rule or the opaque-leader rule that must hold.
    with single_query("approve"), patch("tools.approval._tirith_scan", return_value=_ALLOW_TIRITH):
        result = check_all_command_guards("$X -rf ~/.local", "local")
    assert result["approved"] is False
    assert "unresolved shell expansion" in result["description"]
    assert "single_query_mode" in result["message"]


def test_tirith_blast_block_plus_destructive_pattern_refused_in_approve_mode(single_query):
    blast = {"action": "block", "summary": "", "findings": [
        {"rule_id": "blast_writes_system_path", "severity": "HIGH", "title": "system path"}]}
    with single_query("approve"), patch("tools.approval._tirith_scan", return_value=blast):
        result = check_all_command_guards("X=rm; $X -rf ~/.local", "local")
    assert result["approved"] is False
    assert "blast_writes_system_path" in result["description"]


@pytest.mark.parametrize("command", [
    "rm -rf build/",
    "rm -rf /tmp/stuff",
    'T=/tmp/x; rm -rf $T',
    'for p in a b; do $p --version; done',
    '$PY -c "print(1)"',
    'rm -f /tmp/why197.py; for f in *.pdf; do echo "$f"; done',
])
def test_routine_commands_still_auto_approve_in_approve_mode(single_query, command):
    incomplete = {"action": "block", "summary": "", "findings": [{"rule_id": "analysis_incomplete"}]}
    with single_query("approve"), patch("tools.approval._tirith_scan", return_value=incomplete):
        assert check_all_command_guards(command, "local")["approved"] is True


def test_refusal_is_not_a_floor_yolo_still_bypasses(monkeypatch, single_query):
    monkeypatch.setattr(approval_module, "_YOLO_MODE_FROZEN", True)
    with single_query("approve"), patch("tools.approval._tirith_scan", return_value=_ALLOW_TIRITH):
        assert check_all_command_guards("$X -rf ~/.local", "local")["approved"] is True


def test_deny_mode_still_blocks_first(single_query):
    with single_query("deny"):
        result = check_all_command_guards("X=rm; $X -rf ~/.local", "local")
    assert result["approved"] is False
    assert "Command flagged as dangerous" in result["message"]


# ---- pure classifiers ------------------------------------------------------------------------

def test_opaque_leaders_skip_resolved_and_literal_words():
    assert opaque_command_leaders("X=ls; $X -la")[1] == []
    assert [w for _, _, w in opaque_command_leaders("$CMD -rf x")[1]] == ["$CMD"]
    assert [w for _, _, w in opaque_command_leaders('"${TOOL}" --version')[1]] == ['"${TOOL}"']
    assert [w for _, _, w in opaque_command_leaders("$(which rm) -rf x")[1]] == ["$(which rm)"]


@pytest.mark.parametrize(("command", "refused"), [
    ("$X -rf ~/.local", True),
    ("$X -R ./data", True),
    ("$X --recursive dir", True),
    ("$X /home", True),
    ("$DD if=/dev/zero of=/dev/sdb", True),
    ("$(which rm) -fr ~", True),
    ("$X", False),
    ("$PY -m pytest -q", False),
    ("$p --version", False),
    ("$X -v file", False),
])
def test_opaque_leader_argument_classifier(command, refused):
    reason = unattended_approve_refusal(command, dangerous_description=None, tirith_result=None)
    assert (reason is not None) is refused


def test_only_tirith_blast_rules_count():
    assert tirith_blast_rules({"action": "block", "findings": [{"rule_id": "analysis_incomplete"}]}) == []
    assert tirith_blast_rules({"action": "warn", "findings": [{"rule_id": "blast_find_delete"}]}) == []
    assert tirith_blast_rules({"action": "block", "findings": [
        {"rule_id": "analysis_incomplete"}, {"rule_id": "blast_deletes_outside_repo"}]}) == ["blast_deletes_outside_repo"]
    assert tirith_blast_rules(None) == []


def test_dangerous_detector_sees_the_resolved_command():
    assert detect_dangerous_command("X=rm; $X -rf ~/.local")[0] is True
