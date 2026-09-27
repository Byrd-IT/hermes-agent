"""Shell-variable indirection vs the guards (2026-09-26 incident).

A kanban worker's scratch script sent ``X="rm -rf /home"; $X`` through the REAL terminal tool, under a
profile with ``approvals.single_query_mode: approve``. The hardline floor saw only a quoted
assignment value (data) and a ``$X`` command word, so it returned (False, None). Approve mode
auto-approved the dangerous-pattern and Tirith findings, and the command wiped most of the home
directory.

These tests pin two layers:

* the hardline floor resolves same-command ``NAME=value`` assignments (``;``/``&&`` chains, export,
  arrays, chained variables) and re-checks the substituted command;
* unattended approve mode no longer auto-approves a ``$VAR`` command word the command never
  assigns, a command substitution leader with destructive arguments, any Tirith block combined
  with ``delete in root path``, or a Tirith blast-radius block combined with another destructive
  pattern. A later reassignment never hides an earlier destructive use (resolution is per use).

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
    opaque_command_leaders, resolve_shell_assignment_variants, resolve_shell_assignments,
)
from tools.approval_unattended_floor import tirith_blast_rules, tirith_block_rules, unattended_approve_refusal

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
    assert resolve_shell_assignments('export A=1 B="two words"; C=3 cmd $A $B $C') == (
        'export A=1 B="two words"; C=3 cmd 1 two words 3')


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
    assert "never assigns" in result["description"]
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
    'PY=python3; $PY -c "print(1)"',
    'rm -f /tmp/why197.py; for f in *.pdf; do echo "$f"; done',
])
def test_routine_commands_still_auto_approve_in_approve_mode(single_query, command):
    with single_query("approve"), patch("tools.approval._tirith_scan", return_value=_ALLOW_TIRITH):
        assert check_all_command_guards(command, "local")["approved"] is True


@pytest.mark.parametrize("command", ["rm -rf build/", "for f in a b; do rm -r \"$f.tmp\"; done"])
def test_relative_recursive_delete_with_incomplete_tirith_still_approves(single_query, command):
    # analysis_incomplete + a recursive delete that is NOT rooted stays advisory (see floor module doc).
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

def _opaque_words(command):
    return [word for *_, word in opaque_command_leaders(command)]


def test_opaque_leaders_skip_resolved_and_literal_words():
    assert _opaque_words("X=ls; $X -la") == []
    assert _opaque_words("for p in python3 node; do $p --version; done") == []
    assert _opaque_words("$CMD -rf x") == ["$CMD"]
    assert _opaque_words('"${TOOL}" --version') == ['"${TOOL}"']
    assert _opaque_words("$(which rm) -fr x") == ["$(which rm)"]
    # Only bindings made BEFORE the use resolve it: the first $X comes from the environment.
    assert _opaque_words("$X; X=ls") == ["$X"]


@pytest.mark.parametrize(("command", "refused"), [
    # An unassigned variable in command position is refused whatever follows it.
    ("$X -rf ~/.local", True),
    ("$X /home", True),
    ("$DD if=/dev/zero of=/dev/sdb", True),
    ("$X", True),
    ("${X}", True),
    ('"$X"', True),
    ("true && $X", True),
    ("$PY -m pytest -q", True),
    ("for f in *.sh; do $f; done", True),        # a glob loop cannot be enumerated
    # A command substitution in command position is refused only with destructive arguments.
    ("$(which rm) -fr ~", True),
    ("$(which rm) /home", True),
    ("$(which python3) --version", False),
    # Resolved in the same command: judged by what it resolves to.
    ("X=echo; $X hello", False),
    ('PY=python3; $PY -c "print(1)"', False),
    ("for p in python3 node; do $p --version; done", False),
    # Expansions in argument positions or quoted data are not command words.
    ('echo "$X"', False),
    ("ls $HOME", False),
    ("echo '$X'", False),
    # Data that only LOOKS like a command word once a value is spliced back in, or heredoc text.
    ("B=$(printf '%s' '<?php $c=1; if(!f($c)){exit;}' | base64); ssh h \"echo $B | php\"", False),
    ("cat > /tmp/x.php <<'PHP'\n<?php\n$config = 1;\nPHP\nphp /tmp/x.php", False),
    ("FILES=\"a b\"\nfor f in $FILES; do grep -c x \"$f\"; done", False),
    # A variable whose same-command value is itself an environment variable stays opaque.
    ("P=$__TEST_PYTHON; $P -m pytest", True),
])
def test_opaque_leader_classifier(command, refused):
    reason = unattended_approve_refusal(command, dangerous_description=None, tirith_result=None)
    assert (reason is not None) is refused


def test_tirith_rule_extraction():
    assert tirith_blast_rules({"action": "block", "findings": [{"rule_id": "analysis_incomplete"}]}) == []
    assert tirith_blast_rules({"action": "warn", "findings": [{"rule_id": "blast_find_delete"}]}) == []
    assert tirith_blast_rules({"action": "block", "findings": [
        {"rule_id": "analysis_incomplete"}, {"rule_id": "blast_deletes_outside_repo"}]}) == ["blast_deletes_outside_repo"]
    assert tirith_blast_rules(None) == []
    assert tirith_block_rules({"action": "block", "findings": [{"rule_id": "analysis_incomplete"}]}) == [
        "analysis_incomplete"]
    assert tirith_block_rules({"action": "block", "findings": []}) == ["block"]
    assert tirith_block_rules({"action": "warn", "findings": [{"rule_id": "x"}]}) == []


_INCOMPLETE = {"action": "block", "summary": "", "findings": [{"rule_id": "analysis_incomplete"}]}


@pytest.mark.parametrize(("description", "tirith", "refused"), [
    # Review round 1, P1 #3: ANY Tirith block + 'delete in root path' is refused (RCA minimum).
    ("delete in root path", _INCOMPLETE, True),
    ("delete in root path", {"action": "block", "findings": [{"rule_id": "blast_writes_system_path"}]}, True),
    ("delete in root path", {"action": "warn", "findings": [{"rule_id": "blast_writes_system_path"}]}, False),
    ("delete in root path", _ALLOW_TIRITH, False),
    # The other destructive classes still need a blast-radius block.
    ("recursive delete", _INCOMPLETE, False),
    ("recursive delete", {"action": "block", "findings": [{"rule_id": "blast_deletes_outside_repo"}]}, True),
    ("find -delete", {"action": "block", "findings": [{"rule_id": "blast_find_delete"}]}, True),
    # Not destructive: a Tirith block stays advisory.
    ("pipe remote content to shell", {"action": "block", "findings": [{"rule_id": "blast_x"}]}, False),
])
def test_tirith_block_plus_destructive_classifier(description, tirith, refused):
    reason = unattended_approve_refusal("true", dangerous_description=description, tirith_result=tirith)
    assert (reason is not None) is refused


# ---- review round 1 regressions: combined guard, approve mode, guard-only ------------------

# P1 #1: a later reassignment must not erase an earlier destructive use or copy.
REASSIGNED_COMMANDS = [
    'X="rm -rf /home"; $X; X=echo',
    'X="rm -rf /home" && $X && X=echo',
    'X="rm -rf /home"; Y=$X; X=echo; $Y',
    'X="rm -rf /home"; $X\nX=ls; $X',
    'for c in echo "rm -rf /home"; do $c; done',
]


@pytest.mark.parametrize("command", REASSIGNED_COMMANDS)
def test_later_reassignment_does_not_hide_earlier_destructive_use(command):
    assert detect_hardline_command(command) == (True, "recursive delete of system directory")


@pytest.mark.parametrize("command", REASSIGNED_COMMANDS)
def test_later_reassignment_blocked_by_combined_guard_in_approve_mode(single_query, command):
    with single_query("approve"), patch("tools.approval._tirith_scan", return_value=_INCOMPLETE):
        result = check_all_command_guards(command, "local")
    assert result["approved"] is False
    assert "hardline" in result["message"].lower()


def test_resolution_is_per_use():
    assert resolve_shell_assignment_variants("X=a; echo $X; X=b; echo $X") == ["X=a; echo a; X=b; echo b"]
    assert resolve_shell_assignment_variants("X=a; Y=$X; X=b; echo $Y") == ["X=a; Y=a; X=b; echo a"]


# P1 #2: an unresolved whole-command variable is not auto-approved, with or without arguments.
@pytest.mark.parametrize("command", ["$X", "${X}", '"$X"', "cd /tmp && $X", "$CMD --help"])
def test_unresolved_variable_command_refused_in_approve_mode(single_query, command):
    with single_query("approve"), patch("tools.approval._tirith_scan", return_value=_ALLOW_TIRITH):
        result = check_all_command_guards(command, "local")
    assert result["approved"] is False
    assert "never assigns" in result["description"]
    assert "single_query_mode" in result["message"]


@pytest.mark.parametrize("command", [
    "X=echo; $X hello", 'X="rm -rf /home"; echo "$X"', "echo $X", "printf '%s' \"$X\"",
])
def test_resolved_or_printed_variables_still_approve(single_query, command):
    with single_query("approve"), patch("tools.approval._tirith_scan", return_value=_ALLOW_TIRITH):
        assert check_all_command_guards(command, "local")["approved"] is True


# P1 #3: any Tirith block + 'delete in root path' is refused, not only a blast_* block.
def test_non_blast_tirith_block_plus_delete_in_root_path_refused(single_query):
    with single_query("approve"), patch("tools.approval._tirith_scan", return_value=_INCOMPLETE):
        result = check_all_command_guards("rm -f /tmp/review-data", "local")
    assert result["approved"] is False
    assert "analysis_incomplete" in result["description"]
    assert "delete in root path" in result["description"]


def test_delete_in_root_path_without_tirith_block_still_approves(single_query):
    with single_query("approve"), patch("tools.approval._tirith_scan", return_value=_ALLOW_TIRITH):
        assert check_all_command_guards("rm -f /tmp/review-data", "local")["approved"] is True


def test_dangerous_detector_sees_the_resolved_command():
    assert detect_dangerous_command("X=rm; $X -rf ~/.local")[0] is True
