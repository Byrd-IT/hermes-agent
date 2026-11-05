"""Conditional gateway restart: "restart only if you are idle", decided by the gateway itself.

Protocol ``restart-if-idle/2`` -- for an out-of-process maintenance updater that owns a live scoped ESTOP hold
(``agent/estop.py``) and wants the gateway to restart ONLY IF no work would be interrupted, without a window in
which work can start after its look and before the restart.

Why the decision lives here: a SIGUSR1 cannot be recalled, and no external check sees every kind of work or can
freeze admission in the same instant. The gateway can: its inbound handlers, API handlers and WS verbs all run on
the event-loop thread, so ONE synchronous callback on that thread -- freeze admission (``_draining``), then count
all work -- decides atomically against everything on the loop; the gateway's own gates (the ``_draining`` checks
that every worker thread already honours) refuse the rest.

The exchange (two small files in the gateway's own home, next to ``gateway_state.json``; nothing is sent as a
signal, so an absent / unreadable / stale request can NEVER turn into an unconditional restart):

* request ``restart_if_idle.request.json`` -- written by the requester (mode 0600, atomic rename):
  ``{"schema": 1, "kind": "restart_if_idle", "request_id", "owner", "token_sha256", "pid", "start_time",
  "issued_at", "expires_at"}``. Bound to ONE process (``pid`` + ``start_time``), short-lived (at most
  :data:`MAX_REQUEST_TTL_S`), and authenticated by the live scoped hold it owns: ``token_sha256`` must be the
  digest of the token of a live scoped hold that includes the ``turns`` scope, and ``owner`` must be that hold's
  owner. The file must be a regular file owned by this user and not group/world writable.
* acknowledgement ``restart_if_idle.ack.json`` -- written by the gateway BEFORE it acts, atomically:
  ``result`` is ``accepted`` (frozen + restarting), ``refused_busy`` (work exists; ``active`` counts it),
  ``refused_unknown_activity`` (a source of work could not be read: never read as idle), ``refused_not_ready``
  (not running / already restarting or draining) or ``refused_invalid`` (the request failed validation).
  No acknowledgement, no restart: an unwritable ack file refuses and reopens admission.

The claimed marker (what makes "unknown" distinguishable from "withdrawn"): the gateway claims a request by an
atomic rename to ``restart_if_idle.request.json.claimed`` and KEEPS that marker until its decision is durable:
it is removed only AFTER the acknowledgement was written (accepted or any refusal) or after the gateway decided
not to restart and could not write an acknowledgement (admission is reopened, nothing restarted). So a requester
that reads the marker first and the acknowledgement second can conclude: marker gone + no ack -> the gateway
did not accept (proven); ack accepted -> restarting; marker present + no ack -> UNDECIDED (an ack write can
stall on a slow disk well past ``expires_at``: the expiry is checked before it, so a request is never ACCEPTED
after it expires, but it can still be accepted after the requester stopped waiting). A requester that
withdraws by unlinking the request proves nothing restarts only when the unlink succeeds; if the file is
already gone it was claimed and the requester must wait for the marker, never assume a refusal. A crashed
gateway can leave a stale marker; it carries the request_id, so a requester ignores one that is not its own.

Admission freeze, completely: ``_draining`` plus ``agent/admission_gate.py``. ``_draining`` excludes everything
on the loop thread; worker threads that passed their own gate earlier (cron ticker scan -> registration, a
manual/external cron fire, the misfire sweep, the kanban dispatcher's claim -> spawn, the kanban decomposer)
run inside admission SPANS. The decision raises the gate (``begin_decision``) BEFORE it counts, counts every
span that already entered as work (``admission:<name>`` in ``active``), and a span that enters after it waits
for the outcome: refused and idle -> admitted; accepted -> refused before it touched any state (a due job stays
due, a ready card stays ready: they belong to the replacement gateway). The decision never waits for a span, a
span never holds a lock while waiting, and a span nested in its own thread's span never waits.

Everything else is unchanged: SIGUSR1, ``hermes gateway restart`` and ``/restart`` stay unconditional drains.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import stat
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Optional

logger = logging.getLogger("gateway.restart_if_idle")

PROTOCOL_VERSION = 2
REQUEST_NAME = "restart_if_idle.request.json"
ACK_NAME = "restart_if_idle.ack.json"
_CLAIMED_SUFFIX = ".claimed"
MAX_REQUEST_BYTES = 4096
MAX_REQUEST_TTL_S = 60.0
_FUTURE_SKEW_S = 5.0
POLL_S = 0.25
_SEEN_MAX = 64

RESULT_ACCEPTED = "accepted"
RESULT_BUSY = "refused_busy"
RESULT_UNKNOWN = "refused_unknown_activity"
RESULT_NOT_READY = "refused_not_ready"
RESULT_INVALID = "refused_invalid"

# Set while the poller of THIS process is alive: the only thing that makes the capability claim true.
_ACTIVE = False


def _now() -> float:
    return time.time()


def mark_active(active: bool) -> None:
    global _ACTIVE
    _ACTIVE = bool(active)


def is_active() -> bool:
    return _ACTIVE


def token_digest(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def request_path(home: Any) -> Any:
    from pathlib import Path
    return Path(home) / REQUEST_NAME


def ack_path(home: Any) -> Any:
    from pathlib import Path
    return Path(home) / ACK_NAME


# ── claiming ─────────────────────────────────────────────────────────────────


def claimed_path(home: Any) -> Any:
    """The marker a claimed request becomes (``restart_if_idle.request.json.claimed``). It is the requester's proof
    that the gateway took the request and has NOT yet made its decision durable: it stays until the
    acknowledgement is written (or the gateway decided not to restart and could not say so)."""
    src = request_path(home)
    return src.with_name(src.name + _CLAIMED_SUFFIX)


def release_claim(home: Any) -> None:
    """Remove the claimed marker. Called ONLY after the decision is durable (the ack was written) or the gateway
    decided not to restart; the requester reads the marker BEFORE the ack, so a marker that is gone with no ack
    proves the request was not accepted."""
    try:
        os.unlink(claimed_path(home))
    except OSError:
        pass


def claim_request(home: Any) -> Optional[tuple[Optional[bytes], Optional[str]]]:
    """Atomically take the request out of its slot and leave it as the claimed marker (see :func:`claimed_path`;
    the caller removes it with :func:`release_claim` once its decision is durable). ``None``: there was none (or
    the requester withdrew it first). Otherwise ``(raw, None)`` for a trusted file, or ``(None, reason)`` for one
    that is consumed but not trusted."""
    src = request_path(home)
    mine = claimed_path(home)
    try:
        os.replace(src, mine)                    # atomic: either we or the requester's unlink gets it
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("restart-if-idle: cannot claim the request (%s)", exc)
        return None, f"cannot claim the request: {exc}"
    try:
        st = os.lstat(mine)
    except OSError as exc:
        return None, f"cannot inspect the request: {exc}"
    if not stat.S_ISREG(st.st_mode):
        return None, "the request is not a regular file"
    if os.name != "nt":
        if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
            return None, "the request is not owned by the gateway's user"
        if st.st_mode & 0o022:
            return None, "the request is group- or world-writable"
    if st.st_size > MAX_REQUEST_BYTES:
        return None, "the request is too large"
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(mine, flags)
        with os.fdopen(fd, "rb") as fh:
            raw = fh.read(MAX_REQUEST_BYTES + 1)
    except OSError as exc:
        return None, f"cannot read the request: {exc}"
    if len(raw) > MAX_REQUEST_BYTES:
        return None, "the request is too large"
    return raw, None


# ── validation ───────────────────────────────────────────────────────────────


def _real(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_request(raw: bytes, *, pid: int, start_time: Optional[int], now: float,
                     ) -> tuple[Optional[dict], Optional[str]]:
    """``(request, None)`` when ``raw`` is a valid request for THIS process at ``now``, else ``(None, reason)``."""
    try:
        req = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, "the request is not valid JSON"
    if not isinstance(req, dict):
        return None, "the request is not a JSON object"
    if not (_int(req.get("schema")) and req["schema"] == PROTOCOL_VERSION):
        return None, "unsupported request schema"
    if req.get("kind") != "restart_if_idle":
        return None, "unknown request kind"
    rid = req.get("request_id")
    if not (isinstance(rid, str) and 1 <= len(rid) <= 64 and rid.isprintable()):
        return None, "bad request_id"
    owner = req.get("owner")
    if not (isinstance(owner, str) and 1 <= len(owner) <= 200):
        return None, "bad owner"
    digest = req.get("token_sha256")
    if not (isinstance(digest, str) and len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)):
        return None, "bad token_sha256"
    if not (_int(req.get("pid")) and _int(req.get("start_time"))):
        return None, "pid and start_time must be integers"
    if not (_real(req.get("issued_at")) and _real(req.get("expires_at"))):
        return None, "issued_at and expires_at must be finite numbers"
    if start_time is None:
        return None, "this process's start time is unknown: a request cannot be bound to it"
    if req["pid"] != pid:
        return None, "the request is for another process (pid)"
    if req["start_time"] != start_time:
        return None, "the request is for another process incarnation (start_time)"
    if req["expires_at"] - req["issued_at"] > MAX_REQUEST_TTL_S or req["expires_at"] <= req["issued_at"]:
        return None, f"request validity must be within (0, {MAX_REQUEST_TTL_S:g}] seconds"
    if req["issued_at"] > now + _FUTURE_SKEW_S:
        return None, "the request was issued in the future"
    if now >= req["expires_at"]:
        return None, "the request has expired"
    from agent import estop
    hold = estop.live_hold_by_token_digest(digest)
    if hold is None:
        return None, "no live scoped hold proves this request (released, expired, replaced or never ours)"
    if hold.get("owner") != owner:
        return None, "the request's owner is not the owner of that hold"
    if estop.SCOPE_TURNS not in (hold.get("scopes") or []):
        return None, "the hold does not include the turns scope: this is not the final window"
    return req, None


# ── acknowledgement ──────────────────────────────────────────────────────────


def write_ack(home: Any, payload: dict) -> None:
    """Atomic, private. Raises OSError (the caller then refuses: no ack, no restart)."""
    path = ack_path(home)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}.tmp")
    try:
        fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2) + "\n")
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


# ── what counts as work ──────────────────────────────────────────────────────


def _each_adapter(runner: Any):
    maps = [getattr(runner, "adapters", None) or {}]
    maps += list((getattr(runner, "_profile_adapters", None) or {}).values())
    for adapters in maps:
        yield from list(adapters.values())


def _inbound_sessions(runner: Any) -> int:
    """Messages an adapter has ADMITTED but that may not be a running agent yet: the per-session guard whose owner
    task is alive (a stale guard with a finished task is not work)."""
    total = 0
    for adapter in _each_adapter(runner):
        sessions = getattr(adapter, "_active_sessions", None)
        if not isinstance(sessions, dict):
            continue
        stale = getattr(adapter, "_session_task_is_stale", None)
        for key in list(sessions):
            if callable(stale) and stale(key):
                continue
            total += 1
    return total


def _pending_messages(runner: Any) -> int:
    total = 0
    for adapter in _each_adapter(runner):
        pending = getattr(adapter, "_pending_messages", None)
        if isinstance(pending, dict):
            total += len(pending)
    return total


def _queued_events(runner: Any) -> int:
    queued = runner._queued_events
    return sum(len(v) for v in dict(queued).values() if v)


def _background_tasks(runner: Any) -> int:
    """Non-permanent runner tasks: plugin/internal message injections, ``/bg``-style fire-and-forget work."""
    return sum(1 for t in list(getattr(runner, "_background_tasks", None) or ())
               if not t.done() and not getattr(t, "_hermes_supervised_watcher", False))


def _delegations() -> int:
    from tools.async_delegation import active_count
    return int(active_count())


def _admission_spans() -> list[tuple[str, Callable[[], Any]]]:
    """Worker threads that are between their gate and the registration of their work (cron tick / registration /
    run / misfire fire, kanban claim+spawn, kanban decomposition: ``agent/admission_gate.py``). Read AFTER the
    gate began deciding, so a span that exists is one that entered before it: work admitted but not yet visible
    to any other count. Nothing is excluded."""
    from agent.admission_gate import gate
    return [(f"admission:{name}", (lambda n=n: n)) for name, n in sorted(gate().spans().items())]


def _sources(runner: Any) -> list[tuple[str, Callable[[], Any]]]:
    from gateway import run_runtime
    return _base_sources(runner, run_runtime) + _admission_spans()


def _base_sources(runner: Any, run_runtime: Any) -> list[tuple[str, Callable[[], Any]]]:
    return [
        ("agents", lambda: len(runner._running_agents)),                       # includes pending-start sentinels
        ("managed_turns", lambda: run_runtime.managed_turn_count(runner)),     # managed workers, side agents, ...
        ("cron", lambda: runner._running_cron_job_count()),                    # EVERY run, wedged / external too
        ("api_runs", lambda: runner._api_server_hook("active_agent_work_count")),
        ("api_workers", lambda: runner._active_api_worker_count()),
        ("deferred_workers", lambda: runner._active_deferred_agent_worker_count()),
        ("inbound_sessions", lambda: _inbound_sessions(runner)),
        ("pending_messages", lambda: _pending_messages(runner)),
        ("queued_events", lambda: _queued_events(runner)),
        ("background_tasks", lambda: _background_tasks(runner)),
        ("delegations", _delegations),
        ("voice_sessions", lambda: runner._active_voice_session_count()),      # None = could not be counted
    ]


def idle_probe(runner: Any) -> dict:
    """Count ALL work the gateway could interrupt by exiting. Fail closed: a source that cannot be read (or a voice
    count that is ``None``) is ``unknown``, never zero, and nothing is excluded (no wedged / restart-safe carve-out:
    those are work the requester should see, not skip). Returns ``{"busy": {name: n>0}, "unknown": [name]}``."""
    busy: dict = {}
    unknown: list = []
    for name, read in _sources(runner):
        try:
            n = read()
        except Exception:
            unknown.append(name)
            continue
        if n is None or isinstance(n, bool) or not isinstance(n, int):
            unknown.append(name)
        elif n > 0:
            busy[name] = n
    return {"busy": busy, "unknown": unknown}


# ── the decision (runs on the loop thread; synchronous from freeze to restart) ──


def _own_identity() -> tuple[int, Optional[int]]:
    from gateway.status import get_process_start_time
    pid = os.getpid()
    return pid, get_process_start_time(pid)


def _home() -> Any:
    from gateway.status import _get_process_hermes_home
    return _get_process_hermes_home()


def _remember(runner: Any, request_id: str) -> bool:
    """False when ``request_id`` was already decided by this process (a replayed file)."""
    seen = getattr(runner, "_restart_if_idle_seen", None)
    if seen is None:
        seen = runner._restart_if_idle_seen = OrderedDict()
    if request_id in seen:
        return False
    seen[request_id] = True
    while len(seen) > _SEEN_MAX:
        seen.popitem(last=False)
    return True


def decide(runner: Any) -> None:
    """Handle one request, if any. MUST run on the event-loop thread and contains no ``await``: from the freeze
    to ``request_restart`` nothing else on the loop can run. Never raises."""
    try:
        _decide(runner)
    except Exception:                                    # a bug here must never take the gateway down
        logger.exception("restart-if-idle: unexpected error while deciding; nothing was restarted")


def _decide(runner: Any) -> None:
    home = _home()
    claimed = claim_request(home)
    if claimed is None:
        return
    try:
        _decide_claimed(runner, home, claimed)
    finally:
        # The claimed marker is the requester's proof that a decision is still pending. It goes only now: every
        # path above has written its acknowledgement (accepted or a refusal) or decided NOT to restart and could
        # not say so, so "marker gone and no ack" proves nothing was accepted, and "marker present" is "undecided".
        release_claim(home)


def _decide_claimed(runner: Any, home: Any, claimed: tuple[Optional[bytes], Optional[str]]) -> None:
    raw, problem = claimed
    pid, start = _own_identity()
    ack: dict = {"schema": PROTOCOL_VERSION, "pid": pid, "start_time": start, "request_id": None}

    def finish(result: str, reason: str = "", **extra: Any) -> None:
        ack.update(result=result, reason=reason, at=time.time(), **extra)
        write_ack(home, ack)
        logger.info("restart-if-idle: %s%s", result, f" ({reason})" if reason else "")

    def refuse(result: str, reason: str, **extra: Any) -> None:
        try:
            finish(result, reason, **extra)
        except OSError as exc:
            logger.warning("restart-if-idle: refusal could not be acknowledged (%s)", exc)

    if raw is None:
        return refuse(RESULT_INVALID, problem or "untrusted request")
    req, why = validate_request(raw, pid=pid, start_time=start, now=_now())
    if req is None:
        try:                                             # echo the id when the file at least said one
            parsed = json.loads(raw.decode("utf-8", "replace"))
            if isinstance(parsed, dict) and isinstance(parsed.get("request_id"), str):
                ack["request_id"] = parsed["request_id"][:64]
        except ValueError:
            pass
        return refuse(RESULT_INVALID, why or "invalid request")
    ack["request_id"] = req["request_id"]
    if not _remember(runner, req["request_id"]):
        return refuse(RESULT_INVALID, "replayed request_id")
    if not getattr(runner, "_running", False):
        return refuse(RESULT_NOT_READY, "the gateway is not running")
    if getattr(runner, "_restart_requested", False) or getattr(runner, "_restart_task_started", False):
        return refuse(RESULT_NOT_READY, "a restart is already under way")
    if getattr(runner, "_draining", False):
        return refuse(RESULT_NOT_READY, "the gateway is already draining")

    # ---- the one synchronous step: freeze, count, decide, acknowledge, start the restart --------
    # Two freezes, one order. ``_draining`` excludes everything that runs on this (loop) thread; the admission gate
    # excludes the WORKER threads that passed their own gate earlier and register later (cron, kanban): spans that
    # entered before ``begin_decision`` are counted below, spans after it wait for the outcome. Both are raised
    # BEFORE the count, never after: a count taken first would leave a window in which work is admitted unseen.
    from agent.admission_gate import gate
    admission = gate()
    previous = runner._draining                          # False here (checked above)
    if not admission.begin_decision():
        return refuse(RESULT_NOT_READY, "another conditional restart decision is in progress")
    runner._draining = True                              # 1. freeze admission (inbound, cron, API, WS gates)
    accepted = False
    try:
        probe = idle_probe(runner)                       # 2. count everything (incl. worker threads' spans)
        if _now() >= req["expires_at"]:
            runner._draining = previous
            return refuse(RESULT_INVALID, "the request expired before the decision")
        if probe["busy"]:
            runner._draining = previous                  # 3a. work exists: reopen, restart nothing
            return refuse(RESULT_BUSY, "work is active: " + ", ".join(f"{k}={v}" for k, v in probe["busy"].items()),
                          active=probe["busy"], unknown=probe["unknown"])
        if probe["unknown"]:
            runner._draining = previous
            return refuse(RESULT_UNKNOWN, "could not read: " + ", ".join(probe["unknown"]),
                          active=probe["busy"], unknown=probe["unknown"])
        try:
            finish(RESULT_ACCEPTED, "idle at the decision; admission frozen", active={}, unknown=[])   # 3b. ack FIRST
        except OSError as exc:
            runner._draining = previous
            logger.warning("restart-if-idle: cannot acknowledge (%s); not restarting", exc)
            return
        # The acknowledgement is durable: this is the commit point. Workers waiting on the outcome are refused from
        # here on (the restart is under way), never admitted into a gateway that was counted as idle.
        admission.end_decision(True)
        accepted = True
        if not runner.request_restart(detached=False, via_service=True):     # 4. the normal graceful restart
            logger.error("restart-if-idle: accepted but request_restart declined (already started?)")
    except BaseException:
        if not getattr(runner, "_restart_task_started", False):
            runner._draining = previous
            admission.reopen()
        raise
    finally:
        if not accepted:
            admission.end_decision(False)                # every refusal reopens admission: workers go on


# ── the poller ───────────────────────────────────────────────────────────────


def run_watcher(stop: threading.Event, runner: Any, loop: Any, poll_interval: float = POLL_S) -> None:
    """Thread target: when a request file exists, run the decision ON THE LOOP THREAD. It only looks for a file;
    all trust, validation and effect are in :func:`decide`. A request that cannot be consumed (unwritable directory)
    is retried with a growing pause, not every poll."""
    try:
        path = request_path(_home())
        mark_active(True)
        pause = max(poll_interval, 0.05)
        while not stop.is_set():
            try:
                present = path.exists()
            except OSError:
                present = False
            if not present:
                pause = max(poll_interval, 0.05)
                stop.wait(poll_interval)
                continue
            try:
                loop.call_soon_threadsafe(runner.handle_restart_if_idle_request)
            except RuntimeError:                          # loop closed: the gateway is going away
                break
            stop.wait(pause)                              # give the loop time to consume it
            pause = min(pause * 2, 8.0)                   # still there next time: it cannot be consumed; back off
    finally:
        mark_active(False)


def start_watcher(runner: Any, loop: Any, poll_interval: float = POLL_S):
    stop = threading.Event()
    thread = threading.Thread(target=run_watcher, args=(stop, runner, loop, poll_interval),
                              name="restart-if-idle-watcher", daemon=True)
    mark_active(True)
    thread.start()
    return stop, thread


def attach_watcher(runner: Any, loop: Any) -> None:
    """Start this process's poller (idempotent) and re-publish ``gateway_state.json`` so the capability claim
    appears now, not at the next heartbeat. Called once the gateway is READY."""
    if getattr(runner, "_restart_if_idle_watcher", None) is not None:
        return
    runner._restart_if_idle_watcher = start_watcher(runner, loop)
    try:
        from gateway.status import publish_runtime_status
        publish_runtime_status()
    except Exception:
        logger.debug("restart-if-idle: status re-publication failed", exc_info=True)


def detach_watcher(runner: Any) -> None:
    """Stop the poller (process exit) and withdraw the claim. Never raises."""
    try:
        watcher = getattr(runner, "_restart_if_idle_watcher", None)
        if not (isinstance(watcher, tuple) and len(watcher) == 2):
            return
        runner._restart_if_idle_watcher = None
        stop, thread = watcher
        stop.set()
        thread.join(timeout=2)
        mark_active(False)
    except Exception:
        logger.debug("restart-if-idle: detach failed", exc_info=True)
