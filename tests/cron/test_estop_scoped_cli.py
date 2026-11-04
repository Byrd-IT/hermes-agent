"""`hermes pause --scope …` / `hermes resume --token …` / `--status` / `--capabilities`: the updater's CLI.

The Python API in agent.estop is the primary surface; the CLI is the same operations for an updater
that shells out. Every JSON reply carries ``ok``; exit codes are 0 ok, 2 conflict (a hold this caller
does not own is in place), 3 lost (token no longer owns a live hold), 4 invalid arguments.
"""

from __future__ import annotations

import argparse
import json

import pytest

from agent import estop
from hermes_cli.subcommands.pause import build_pause_parser


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    estop._logged_components.clear()
    return tmp_path


def run(argv, capsys):
    parser = argparse.ArgumentParser(prog="hermes")
    build_pause_parser(parser.add_subparsers())
    args = parser.parse_args(argv)
    rc = args.func(args)
    out = capsys.readouterr().out
    return rc, out


def run_json(argv, capsys):
    rc, out = run([*argv, "--json"], capsys)
    return rc, json.loads(out)


def test_capabilities_json_matches_the_python_api(hermes_home, capsys):
    rc, payload = run_json(["pause", "--capabilities"], capsys)
    assert rc == 0 and payload["ok"] is True
    assert payload["capabilities"] == estop.capabilities()
    assert not estop.is_engaged(), "--capabilities is read-only"


def test_scoped_pause_engages_background_and_returns_a_token(hermes_home, capsys):
    rc, payload = run_json(
        ["pause", "--scope", "background", "--owner", "updater", "--ttl", "600", "--reason", "update drain"], capsys)
    assert rc == 0 and payload["ok"] and payload["action"] == "engage"
    assert payload["scopes"] == list(estop.BACKGROUND_SCOPES) and payload["token"]
    assert estop.is_held("kanban") and not estop.is_held("turns")
    assert estop.verify(payload["token"])["owner"] == "updater"


def test_scope_accepts_comma_lists_repeats_and_aliases(hermes_home, capsys):
    rc, payload = run_json(["pause", "--scope", "kanban,cron", "--scope", "loop", "--ttl", "60"], capsys)
    assert rc == 0 and payload["scopes"] == ["kanban", "cron", "loop"]
    estop.disengage()
    rc, payload = run_json(["pause", "--scope", "all", "--ttl", "60"], capsys)
    assert payload["scopes"] == list(estop.ALL_SCOPES)
    estop.disengage()
    rc, payload = run_json(["pause", "--scope", "background,turns", "--ttl", "60"], capsys)
    assert payload["scopes"] == list(estop.ALL_SCOPES)


def test_token_transition_adds_turns_and_back_atomically(hermes_home, capsys):
    _, first = run_json(["pause", "--scope", "background", "--ttl", "600", "--owner", "updater"], capsys)
    rc, moved = run_json(["pause", "--token", first["token"], "--scope", "background,turns", "--ttl", "600"], capsys)
    assert rc == 0 and moved["action"] == "transition" and estop.is_held("turns")
    rc, _back = run_json(["pause", "--token", first["token"], "--scope", "background", "--ttl", "600"], capsys)
    assert rc == 0 and not estop.is_held("turns") and estop.is_held("kanban")


def test_renew_extends_the_deadman(hermes_home, capsys):
    _, first = run_json(["pause", "--scope", "background", "--ttl", "60"], capsys)
    rc, renewed = run_json(["pause", "--renew", "--token", first["token"], "--ttl", "900"], capsys)
    assert rc == 0 and renewed["action"] == "renew" and renewed["expires_at"] > first["expires_at"] + 800


def test_lost_token_exits_3_for_transition_and_renew(hermes_home, capsys):
    _, first = run_json(["pause", "--scope", "background", "--ttl", "60"], capsys)
    estop.engage(reason="operator")  # the operator takes over with a full pause
    rc, payload = run_json(["pause", "--token", first["token"], "--scope", "turns", "--ttl", "60"], capsys)
    assert rc == 3 and payload == {"ok": False, "error": "lost", "message": payload["message"]}
    rc, payload = run_json(["pause", "--renew", "--token", first["token"], "--ttl", "60"], capsys)
    assert rc == 3 and payload["error"] == "lost"
    assert estop.get_state()["mode"] == "full", "a lost owner never weakens the operator's pause"


def test_conflict_exits_2_and_never_overwrites_a_foreign_hold(hermes_home, capsys):
    estop.engage(reason="operator")
    rc, payload = run_json(["pause", "--scope", "background", "--ttl", "60"], capsys)
    assert rc == 2 and payload["ok"] is False and payload["error"] == "conflict"
    assert estop.get_state()["reason"] == "operator" and estop.get_state()["mode"] == "full"


@pytest.mark.parametrize(
    "argv",
    [
        ["pause", "--scope", "kanban"],  # a scoped hold needs a deadman
        ["pause", "--scope", "kanban", "--ttl", "0"],
        ["pause", "--scope", "kanban", "--ttl", "99999"],
        ["pause", "--scope", "kanban", "--ttl", "abc"],
        ["pause", "--scope", "bogus", "--ttl", "60"],
        ["pause", "--scope", "", "--ttl", "60"],
        ["pause", "--scope", "kanban,", "--ttl", "60"],
        ["pause", "--token", "x"],  # a token without a scope or --renew
        ["pause", "--renew", "--ttl", "60"],  # renew needs the token
        ["pause", "--renew", "--token", "x"],  # renew needs a ttl
    ],
)
def test_invalid_arguments_exit_4_and_engage_nothing(hermes_home, capsys, argv):
    rc, payload = run_json(argv, capsys)
    assert rc == 4 and payload["ok"] is False and payload["error"] == "invalid"
    assert estop.is_engaged() is False


def test_legacy_pause_and_resume_are_unchanged(hermes_home, capsys):
    rc, out = run(["pause", "--reason", "ops incident"], capsys)
    assert rc == 0 and "paused" in out.lower() and estop.get_state()["mode"] == "full"
    rc, out = run(["resume"], capsys)
    assert rc == 0 and "resumed" in out.lower() and not estop.is_engaged()


def test_resume_with_token_releases_only_the_owned_hold(hermes_home, capsys):
    _, first = run_json(["pause", "--scope", "background", "--ttl", "60"], capsys)
    rc, payload = run_json(["resume", "--token", "someone-elses"], capsys)
    assert rc == 3 and payload["error"] == "lost" and estop.is_held("kanban")
    rc, payload = run_json(["resume", "--token", first["token"]], capsys)
    assert rc == 0 and payload == {"ok": True, "action": "release", "released": True}
    assert not estop.is_engaged()


def test_resume_with_token_never_lifts_an_operator_pause(hermes_home, capsys):
    _, first = run_json(["pause", "--scope", "background", "--ttl", "60"], capsys)
    estop.engage(reason="operator")
    rc, _ = run_json(["resume", "--token", first["token"]], capsys)
    assert rc == 3 and estop.is_engaged()


def test_plain_resume_is_still_the_operator_override_of_a_scoped_hold(hermes_home, capsys):
    run_json(["pause", "--scope", "background", "--ttl", "60"], capsys)
    rc, _out = run(["resume"], capsys)
    assert rc == 0 and not estop.is_engaged()


def test_status_json_lists_holds_without_tokens(hermes_home, capsys):
    _, first = run_json(["pause", "--scope", "background", "--ttl", "60", "--owner", "updater"], capsys)
    rc, payload = run_json(["pause", "--status"], capsys)
    assert rc == 0 and payload["ok"] and payload["status"]["mode"] == "scoped"
    assert payload["status"]["holds"][0]["owner"] == "updater"
    assert first["token"] not in json.dumps(payload)
    rc, out = run(["pause", "--status"], capsys)
    assert rc == 0 and "scoped" in out and "kanban" in out and "updater" in out


def test_status_when_not_paused(hermes_home, capsys):
    rc, payload = run_json(["pause", "--status"], capsys)
    assert rc == 0 and payload["status"] == {"engaged": False, "mode": "none", "scopes": [], "holds": []}


def test_fleet_flag_targets_the_fleet_root_sentinel(tmp_path, monkeypatch, capsys):
    root = tmp_path / "root"
    profile = root / "profiles" / "p"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    estop._logged_components.clear()
    rc, payload = run_json(["pause", "--scope", "background", "--ttl", "60", "--fleet"], capsys)
    assert rc == 0 and (root / "ESTOP").exists() and not (profile / "ESTOP").exists()
    assert estop.is_held("kanban")  # a profile gateway honors the root hold
    rc, _ = run_json(["resume", "--token", payload["token"]], capsys)
    assert rc == 3, "without --fleet the token is looked up in the profile home"
    rc, _ = run_json(["resume", "--token", payload["token"], "--fleet"], capsys)
    assert rc == 0 and not estop.is_engaged()


def test_text_output_for_scoped_pause_names_scopes_ttl_and_token(hermes_home, capsys):
    rc, out = run(["pause", "--scope", "background", "--ttl", "120", "--owner", "updater"], capsys)
    assert rc == 0
    for needle in ("scoped", "kanban", "120", "token"):
        assert needle in out, out
    assert "user turns" in out.lower()


def test_status_line_for_a_scoped_hold(hermes_home):
    from hermes_cli.status import _estop_status_line

    estop.engage_scoped(["kanban", "cron"], owner="updater", ttl_s=90, reason="update drain")
    line = _estop_status_line()
    assert "scoped" in line.lower() and "kanban" in line and "updater" in line and "update drain" in line
    assert "user turns" in line.lower() or "turns running" in line.lower()


def test_gateway_pause_command_reports_a_scoped_hold_instead_of_clobbering_it(hermes_home):
    import asyncio
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    hold = estop.engage_scoped(["kanban"], owner="updater", ttl_s=60, reason="drain")

    class Ev:
        def get_command_args(self):
            return ""

    reply = asyncio.run(runner._handle_pause_command(Ev()))
    assert "already paused" in reply.lower()
    assert estop.verify(hold["token"]) is not None, "a bare /pause must not replace the maintenance hold"


def test_pause_help_documents_scoped_flags():
    parser = argparse.ArgumentParser(prog="hermes")
    build_pause_parser(parser.add_subparsers())
    sub = parser._subparsers._group_actions[0].choices["pause"]
    text = " ".join(sub.format_help().split())
    for needle in ("--scope", "--ttl", "--owner", "--token", "--renew", "--status", "--capabilities", "--json", "--fleet"):
        assert needle in text, needle
