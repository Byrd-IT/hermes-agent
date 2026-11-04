"""``hermes pause`` / ``hermes resume`` — the global emergency stop.

``pause`` writes the ESTOP sentinel at ``$HERMES_HOME/ESTOP``; cron, kanban and new gateway
turns halt on their next check (in-flight work is never killed). Every local turn is a gateway
turn, so interactive CLI, TUI and Desktop turns are held too, with ``agent.estop.paused_reply``'s
notice. ``resume`` removes it and operation resumes on the next tick — no restart. Ported from gastownhall/gastown estop.go (MIT).

``pause --scope …`` is the *scoped* form used by maintenance tooling: an owned, deadman-bounded hold
on only the named scopes (e.g. ``background``: kanban, cron, cron-misfire, cron-webhook, heartbeat,
loop) that leaves user turns running. See ``agent.estop`` for the semantics; this module is the same
operations for callers that shell out (``--json`` replies; exit codes 0 ok, 2 conflict, 3 lost, 4 invalid).
"""

from __future__ import annotations

import argparse
import json
import math
import time

EXIT_OK, EXIT_CONFLICT, EXIT_LOST, EXIT_INVALID = 0, 2, 3, 4

_SCOPE_ALIASES = {"background": "BACKGROUND_SCOPES", "all": "ALL_SCOPES"}


def _emit(args: argparse.Namespace, payload: dict, text: str = "") -> None:
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif text:
        print(text)


def _fail(args: argparse.Namespace, code: int, error: str, message: str) -> int:
    _emit(args, {"ok": False, "error": error, "message": message}, f"hermes pause: {error}: {message}")
    return code


def _parse_scopes(values) -> list:
    """Comma-separated and/or repeated ``--scope`` values; ``background`` / ``all`` expand. Raises ValueError."""
    from agent import estop

    out: list = []
    for value in values or []:
        for part in str(value).split(","):
            name = part.strip()
            if not name:
                raise ValueError(f"empty scope name in {value!r}")
            alias = _SCOPE_ALIASES.get(name)
            out.extend(getattr(estop, alias) if alias else [name])
    if not out:
        raise ValueError("no scopes given")
    return out


def _parse_ttl(raw):
    try:
        ttl = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"--ttl must be a number of seconds, got {raw!r}") from None
    if not math.isfinite(ttl):
        raise ValueError(f"--ttl must be finite, got {raw!r}")
    return int(ttl) if ttl == int(ttl) else ttl


def _scoped_text(verb: str, hold: dict) -> str:
    from agent import estop

    turns = estop.SCOPE_TURNS in hold["scopes"]
    return "\n".join([
        f"⏸️  Hermes {verb} (scoped hold by {hold.get('owner')}): {', '.join(hold['scopes'])}",
        "    User turns are " + ("ALSO on hold." if turns else "NOT held — user turns keep running."),
        f"    Auto-releases in {max(0, hold['expires_at'] - time.time()):.0f}s unless renewed (deadman).",
        f"    token: {hold['token']}  (keep it: only the token holder can transition, renew or release)",
    ])


def _cmd_scoped(args: argparse.Namespace) -> int:
    from agent import estop

    home = estop.fleet_root() if getattr(args, "fleet", False) else None
    token = getattr(args, "token", None)
    reason = getattr(args, "reason", None)
    try:
        if getattr(args, "renew", False):
            if not token:
                raise ValueError("--renew requires --token")
            if getattr(args, "ttl", None) is None:
                raise ValueError("--renew requires --ttl")
            hold = estop.renew(token, _parse_ttl(args.ttl), home=home)
            action, verb = "renew", "hold renewed"
        else:
            scopes = _parse_scopes(args.scope)
            if getattr(args, "ttl", None) is None:
                raise ValueError("a scoped hold needs --ttl SECONDS (the deadman: it auto-releases if not renewed)")
            ttl = _parse_ttl(args.ttl)
            if token:  # transition an existing owned hold; never creates one
                hold = estop.set_scopes(token, scopes, ttl_s=ttl, home=home, **({"reason": reason} if reason else {}))
                action, verb = "transition", "hold moved"
            else:
                hold = estop.engage_scoped(
                    scopes, owner=getattr(args, "owner", None) or "cli", ttl_s=ttl, reason=reason, home=home)
                action, verb = "engage", "paused"
    except ValueError as exc:  # includes estop.InvalidScope
        return _fail(args, EXIT_INVALID, "invalid", str(exc))
    except estop.HoldConflict as exc:
        return _fail(args, EXIT_CONFLICT, "conflict", str(exc))
    except estop.HoldLost as exc:
        return _fail(args, EXIT_LOST, "lost", str(exc))
    _emit(args, {"ok": True, "action": action, **hold}, _scoped_text(verb, hold))
    return EXIT_OK


def _cmd_status(args: argparse.Namespace) -> int:
    from agent import estop

    snap = estop.status()
    lines = [f"Hermes is paused ({snap['mode']})." if snap["engaged"] else "Hermes is not paused."]
    for h in snap["holds"]:
        who = f" by {h['owner']}" if h.get("owner") else ""
        lines.append(
            f"  {h['mode']} hold{who} at {h['path']}: {', '.join(h['scopes'])}"
            + (f" — {h['reason']}" if h.get("reason") else "")
            + (f" (expires at epoch {h['expires_at']:.0f})" if h.get("expires_at") else ""))
    _emit(args, {"ok": True, "action": "status", "status": snap}, "\n".join(lines))
    return EXIT_OK


def cmd_pause(args: argparse.Namespace) -> int:
    """Engage the global emergency stop (full), or — with ``--scope`` — an owned scoped hold."""
    from agent.estop import capabilities, engage, get_state, is_engaged

    if getattr(args, "capabilities", False):
        caps = capabilities()
        _emit(args, {"ok": True, "action": "capabilities", "capabilities": caps}, json.dumps(caps, indent=2, sort_keys=True))
        return EXIT_OK
    if getattr(args, "status", False):
        return _cmd_status(args)
    if getattr(args, "scope", None) or getattr(args, "renew", False):
        return _cmd_scoped(args)
    if getattr(args, "token", None):
        return _fail(args, EXIT_INVALID, "invalid", "--token needs --scope (transition) or --renew")

    reason = getattr(args, "reason", None)
    already = is_engaged()
    path = engage(reason=reason)
    state = get_state() or {}
    verb = "Still paused" if already else "Hermes paused"
    detail = f" — reason: {state['reason']}" if state.get("reason") else ""
    print(f"⏸️  {verb}{detail}")
    print(f"    sentinel: {path}")
    print(
        "    Cron dispatch, kanban dispatch, and new gateway turns (messaging and interactive\n"
        "    CLI, TUI and Desktop turns alike) are on hold. In-flight work keeps running.\n"
        "    Run `hermes resume` to lift the pause.")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    """Disengage the global emergency stop; with ``--token`` release only the hold that token owns."""
    from agent.estop import disengage, fleet_root, release, sentinel_path

    token = getattr(args, "token", None)
    if token:
        home = fleet_root() if getattr(args, "fleet", False) else None
        if release(token, home=home):
            _emit(args, {"ok": True, "action": "release", "released": True}, "▶️  Scoped hold released.")
            return EXIT_OK
        return _fail(args, EXIT_LOST, "lost",
                     "no live hold owned by this token (already released, expired, or replaced); nothing was lifted")
    if disengage():
        print("▶️  Hermes resumed — dispatch picks up on the next tick.")
    else:
        print(f"Hermes is not paused (no sentinel at {sentinel_path()}).")
    return 0


def build_pause_parser(subparsers) -> None:
    """Attach the ``pause`` and ``resume`` subcommands to ``subparsers``."""
    pause_parser = subparsers.add_parser(
        "pause", help="Emergency stop: pause cron/kanban dispatch and new gateway turns",
        description="Engage the global emergency stop. Halts NEW work only — cron "
            "dispatch, kanban dispatch, and new gateway turns — until "
            "`hermes resume`. Interactive CLI, TUI and Desktop turns are gateway turns, so they "
            "are held too: each gets \"Hermes is paused\" with the reason. Turns already "
            "running finish; in-flight work is never killed. With --scope, hold only the named "
            "scopes (background = kanban, cron, cron-misfire, cron-webhook, heartbeat, loop; turns = "
            "new user turns) under an owner token with a deadman --ttl, e.g. "
            "`hermes pause --scope background --ttl 600 --owner updater --json`; move it with "
            "`--token T --scope background,turns --ttl 180`, keep it alive with `--renew --token T --ttl N`, "
            "and drop it with `hermes resume --token T`. --status / --capabilities are read-only.")
    pause_parser.add_argument(
        "--reason", default=None, help="Optional reason stored in the sentinel and shown to users")
    pause_parser.add_argument(
        "--scope", action="append", default=None, metavar="SCOPES",
        help="Scoped hold: comma-separated and/or repeated scopes (kanban, cron, cron-misfire, cron-webhook, "
             "heartbeat, loop, turns; aliases: background, all). Requires --ttl.")
    pause_parser.add_argument("--ttl", default=None, metavar="SECONDS",
                              help="Deadman for a scoped hold (1..3600); it auto-releases unless renewed")
    pause_parser.add_argument("--owner", default=None, help="Owner label recorded in a scoped hold (default: cli)")
    pause_parser.add_argument("--token", default=None,
                              help="Owner token: with --scope moves that hold to the new scopes; with --renew re-arms it")
    pause_parser.add_argument("--renew", action="store_true", help="Re-arm the deadman of the --token hold to --ttl")
    pause_parser.add_argument("--fleet", action="store_true",
                              help="Use the fleet-root sentinel (honored by every profile gateway) instead of this profile's")
    pause_parser.add_argument("--status", action="store_true", help="Print the effective hold(s) and exit")
    pause_parser.add_argument("--capabilities", action="store_true",
                              help="Print the runtime's scoped-pause capabilities and exit (updaters gate on this)")
    pause_parser.add_argument("--json", action="store_true", help="Machine-readable output for scoped/status/capabilities")
    pause_parser.set_defaults(func=cmd_pause)

    resume_parser = subparsers.add_parser(
        "resume", help="Lift the emergency stop set by `hermes pause`",
        description="Remove the ESTOP sentinel; dispatch resumes on the next tick. With --token, release only "
                    "the scoped hold that token owns (exit 3 if it no longer owns one); never lifts anyone else's.")
    resume_parser.add_argument("--token", default=None, help="Release only the scoped hold owned by this token")
    resume_parser.add_argument("--fleet", action="store_true", help="Look the --token hold up in the fleet-root sentinel")
    resume_parser.add_argument("--json", action="store_true", help="Machine-readable output for --token")
    resume_parser.set_defaults(func=cmd_resume)
