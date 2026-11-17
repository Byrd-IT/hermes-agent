"""Global emergency stop (ESTOP) — a resumable pause for NEW work only.

``hermes pause`` writes a sentinel at ``$HERMES_HOME/ESTOP``; ``hermes resume``
removes it. While it exists the cron scheduler, kanban dispatcher and new gateway
turns skip work; in-flight work is never killed. The check is one or two uncached
``os.stat`` calls (process home + fleet root when they differ). The body is optional
JSON ``{"reason", "engaged_at"}``; a corrupt/empty file still counts as engaged
(fail safe, e.g. ``touch ~/.hermes/ESTOP``). Ported from gastownhall/gastown estop.go (MIT).

Scoped holds (``scoped-estop/1``)
---------------------------------
The same sentinel can carry a *scoped hold*: an owned, deadman-bounded hold on a named subset of
scopes (:data:`ALL_SCOPES`) so maintenance can quiesce the background dispatchers while user turns
keep working, then — atomically, only when the owner decides — hold the user turns too.

* A scoped body is ``{"schema": 1, "kind": "scoped", "token", "owner", "scopes": [...],
  "expires_at": <epoch>, "reason", "engaged_at", "updated_at"}``. Anything else in the sentinel is
  the legacy FULL pause. Every scoped body that does not validate *exactly* (unknown scope, empty
  scope list, wrong schema/types, non-finite expiry, missing token/owner) is also a FULL pause and
  has no deadman: ambiguity must never weaken a hold.
* Ownership: only the holder of ``token`` may transition (:func:`set_scopes`), renew or release a
  scoped hold, and :func:`engage_scoped` never overwrites a live hold it does not own (legacy,
  corrupt or foreign) — it raises :class:`HoldConflict`. Operators keep their overrides:
  ``hermes resume`` / ``/pause off`` remove any hold and ``hermes pause`` replaces an owned hold
  with a FULL one; the owner then finds its token lost (:class:`HoldLost`) and must abort.
* Deadman: a scoped hold carries ``expires_at``; past it the hold is ignored (a crashed owner never
  strands the fleet). The owner :func:`renew`\\ s while it is alive. Legacy pauses never expire.
* Composition: holds in the process home and the fleet root are UNIONED; a FULL hold anywhere is
  FULL. A profile-level owner can therefore never weaken a root-level (or any foreign) hold.
* Every write — including the operator's :func:`engage` / :func:`disengage` — is serialized by an
  ``ESTOP.lock`` flock and lands via write-temp + ``os.replace``, so a transition never exposes a
  partial file or a moment with no hold, and an operator ``hermes resume`` can never be undone by an
  owner write that was already in flight. Where no file lock exists (neither ``fcntl`` nor ``msvcrt``)
  the owner's writes refuse to run rather than pretend to be serialized; the operator's pause/resume
  still work, unserialized (they only ever fail safe).
* ``engage()`` never reports a pause that did not land: when the atomic write fails it truncates the
  sentinel in place (readers see a hold at every instant) and, if even that cannot produce a FULL
  hold, raises ``OSError``.

Old runtimes read the same file as an ordinary (full) engaged sentinel — fail safe, but it would
pause Telegram too. An updater must therefore judge the RUNNING gateway, not the code on disk: read
the ``capabilities`` claim in that gateway's ``gateway_state.json`` and accept it only when its
``capabilities_pid`` (and ``capabilities_start_time``) match that same record's ``pid`` /
``start_time``. :func:`capabilities` / ``hermes pause --capabilities`` only describe the code of the
process that calls them (a CLI that may be newer than the gateway) and are NOT proof of what the
running gateway will do with a scoped sentinel.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import threading
import time
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

# Same profile-aware / fleet-root resolvers the file-safety guards use (fail-open to ~/.hermes).
from agent.file_safety import _hermes_home_path as _hermes_home, _hermes_root_path as _canonical_root

try:  # POSIX advisory locks
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows only
    fcntl = None  # type: ignore[assignment]
try:  # Windows byte-range lock (the Windows counterpart of flock)
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore[assignment]

# Windows has no blocking flock: poll a 1-byte lock up to this long, then fail the write.
_LOCK_TIMEOUT_S = 30.0
_LOCK_POLL_S = 0.05
# errnos msvcrt.locking raises while another process holds the byte (EACCES; EDEADLOCK after its own 10 tries).
_LOCK_CONTENTION_ERRNOS = frozenset(
    e for e in (errno.EACCES, errno.EAGAIN, getattr(errno, "EDEADLK", None), getattr(errno, "EDEADLOCK", None)) if e)

SENTINEL_NAME = "ESTOP"
LOCK_NAME = "ESTOP.lock"

SCOPED_ESTOP_VERSION = 1
_KIND_SCOPED = "scoped"

# The `component` names passed to check_paused() by the real dispatch callsites:
#   kanban        gateway/kanban_watchers_common._kanban_dispatch_allowed (+ kanban run_daemon)
#   cron          cron/scheduler_tick (the ticker)
#   cron-misfire  cron/scheduler_provider.fire_overdue_jobs (housekeeping backstop)
#   cron-webhook  gateway/platforms/api_server cron-fire endpoint
#   heartbeat     gateway/run_goals heartbeat poller (+ the inbound gate for heartbeat turns)
#   loop          gateway/run_goals /loop wakeups
BACKGROUND_SCOPES: tuple[str, ...] = ("kanban", "cron", "cron-misfire", "cron-webhook", "heartbeat", "loop")
# New user turns (the inbound gateway gate; control commands and in-flight replies stay exempt).
SCOPE_TURNS = "turns"
ALL_SCOPES: tuple[str, ...] = (*BACKGROUND_SCOPES, SCOPE_TURNS)

# A scoped hold must be renewed by its owner at least this often; there is no permanent scoped hold.
MAX_TTL_S = 3600

_KEEP: Any = object()  # "leave this field as it is" marker for set_scopes(reason=...)

# Per-component "logged already for this engagement" flags: log once per engagement, not per tick.
_log_lock = threading.Lock()
_logged_components: set[str] = set()


class EstopError(Exception):
    """Base class for scoped-hold errors."""


class InvalidScope(EstopError, ValueError):
    """Unknown/empty scopes, a bad ttl or a missing owner. Nothing was written."""


class HoldConflict(EstopError):
    """A live hold this caller does not own already occupies the sentinel; it was left untouched."""


class HoldLost(EstopError):
    """The caller's token no longer owns a live hold (operator resume/pause, expiry, replacement)."""


def _now() -> float:
    return time.time()


def sentinel_path() -> Path:
    """Path of the ESTOP sentinel this process would write on `hermes pause`."""
    return _hermes_home() / SENTINEL_NAME


def fleet_root() -> Path:
    """Directory whose sentinel every profile gateway of this fleet also honors."""
    return _canonical_root()


def _candidate_sentinel_paths() -> list:
    """Profile home first, then the fleet root if it is a different directory: a profile
    gateway (HERMES_HOME=~/.hermes/profiles/<n>) must still honor an operator's ~/.hermes/ESTOP."""
    primary = sentinel_path()
    try:
        root = _canonical_root() / SENTINEL_NAME
    except Exception:
        return [primary]
    try:
        distinct = root.resolve() != primary.resolve()
    except Exception:
        # Non-Path test doubles fail .resolve(); plain equality still dedupes.
        distinct = root != primary
    return [primary, root] if distinct else [primary]


# ── reading holds ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Hold:
    """One sentinel file as this runtime understands it."""

    mode: str  # "full" (legacy / ambiguous / unreadable) or "scoped"
    scopes: frozenset
    reason: Optional[str] = None
    engaged_at: Optional[str] = None
    updated_at: Optional[str] = None
    owner: Optional[str] = None
    token: Optional[str] = None
    expires_at: Optional[float] = None
    path: Any = None

    def expired(self, now: float) -> bool:
        return self.mode == "scoped" and self.expires_at is not None and now >= self.expires_at


_ALL = frozenset(ALL_SCOPES)


def _str_or_none(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _full(path: Any, raw: Any = None) -> _Hold:
    reason = engaged_at = None
    if isinstance(raw, dict):
        reason, engaged_at = _str_or_none(raw.get("reason")), _str_or_none(raw.get("engaged_at"))
    return _Hold("full", _ALL, reason=reason, engaged_at=engaged_at, path=path)


def _is_real_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _parse(text: str, path: Any) -> _Hold:
    """Never raises. Exact scoped bodies become a scoped hold; everything else is a FULL pause."""
    try:
        raw = json.loads(text)
    except ValueError:
        return _full(path)
    if not isinstance(raw, dict) or raw.get("kind") != _KIND_SCOPED:
        return _full(path, raw)
    schema, token, owner, scopes, expires = (
        raw.get("schema"), raw.get("token"), raw.get("owner"), raw.get("scopes"), raw.get("expires_at"),
    )
    valid = (
        type(schema) is int and schema == SCOPED_ESTOP_VERSION
        and isinstance(token, str) and token
        and isinstance(owner, str) and owner
        and isinstance(scopes, list) and scopes
        and all(isinstance(s, str) and s in _ALL for s in scopes)
        and _is_real_number(expires)
    )
    if not valid:
        return _full(path, raw)
    return _Hold(
        "scoped", frozenset(scopes), reason=_str_or_none(raw.get("reason")),
        engaged_at=_str_or_none(raw.get("engaged_at")), updated_at=_str_or_none(raw.get("updated_at")),
        owner=owner, token=token, expires_at=float(expires), path=path,
    )


def _load(path: Any, *, include_expired: bool = False) -> Optional[_Hold]:
    """The hold in ``path``, or None when absent (or expired, unless ``include_expired``).

    Raises OSError when existence itself cannot be determined (callers fail safe to FULL); an
    unreadable body is a FULL hold. AttributeError (non-Path test doubles) propagates to callers
    that already tolerate it.
    """
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None  # lifted between exists() and read
    except (OSError, ValueError):
        return _full(path)  # unreadable/undecodable but present: fail safe
    hold = _parse(text, path)
    if not include_expired and hold.expired(_now()):
        return None
    return hold


def _reap_expired(path: Any, stale: _Hold) -> None:
    """Best-effort removal of a dead (expired) scoped sentinel so no stale file lingers for external
    watchdogs. Under the sentinel lock, and only if the file STILL holds that exact expired hold: a
    replacement written meanwhile is never touched."""
    with suppress(OSError, AttributeError):
        with _locked(path.parent):
            cur = _load(path, include_expired=True)
            if (cur is not None and cur.mode == "scoped" and cur.token == stale.token
                    and cur.expires_at == stale.expires_at and cur.expired(_now())):
                path.unlink()


def _load_reaping(path: Any) -> Optional[_Hold]:
    hold = _load(path, include_expired=True)
    if hold is not None and hold.expired(_now()):
        _reap_expired(path, hold)
        return None
    return hold


def _visible_holds() -> list[_Hold]:
    holds: list[_Hold] = []
    for path in _candidate_sentinel_paths():
        try:
            hold = _load_reaping(path)
        except OSError:
            holds.append(_full(path))  # stat failure: fail SAFE
        except AttributeError:
            continue
        else:
            if hold is not None:
                holds.append(hold)
    return holds


def _ordered(scopes: Iterable[str]) -> list[str]:
    wanted = set(scopes)
    return [s for s in ALL_SCOPES if s in wanted]


def is_engaged() -> bool:
    """True if ANY live hold (full or scoped) is visible; fail SAFE (True) on stat errors."""
    return bool(_visible_holds())


def is_held(component: str) -> bool:
    """True when ``component`` (a ``check_paused`` name, or :data:`SCOPE_TURNS`) is held.

    A FULL hold anywhere holds everything — including components this runtime has never heard of;
    a scoped hold holds exactly its scopes, and holds across home + fleet root are unioned.
    """
    for hold in _visible_holds():
        if hold.mode == "full" or component in hold.scopes:
            return True
    return False


def capabilities() -> dict:
    """Machine-readable feature descriptor of THIS process's code (``hermes pause --capabilities``).

    This is what the code that is calling can do — not proof about the gateway that is running, which may
    be older than the CLI (an older runtime reads any scoped sentinel as a FULL pause, Telegram included).
    An updater must gate on the RUNNING gateway's own claim: ``capabilities`` in its ``gateway_state.json``
    (written from this function at that process's start), valid only while ``capabilities_pid`` and
    ``capabilities_start_time`` equal that record's ``pid`` / ``start_time``; otherwise refuse."""
    return {
        "scoped_estop": SCOPED_ESTOP_VERSION,
        "scopes": list(ALL_SCOPES),
        "background_scopes": list(BACKGROUND_SCOPES),
        "owner_token": True,
        "deadman": True,
        "atomic_transition": True,
        "max_ttl_s": MAX_TTL_S,
    }


# ── writing holds ───────────────────────────────────────────────────────────


def _iso(epoch: Optional[float] = None) -> str:
    return datetime.fromtimestamp(_now() if epoch is None else epoch, tz=UTC).isoformat()


@contextmanager
def _locked(directory: Path) -> Iterator[None]:
    """Serialize sentinel read-modify-write cycles in ``directory`` (readers never lock).

    POSIX ``flock`` or a Windows ``msvcrt`` byte lock on ``ESTOP.lock``. Raises ``OSError`` when the lock
    cannot be taken (no primitive on this platform, lock file unusable, Windows timeout): callers either
    fail the write (owner operations) or, for the operator's pause/resume, proceed unserialized."""
    if fcntl is None and msvcrt is None:
        raise OSError(errno.ENOLCK, "no file-locking primitive on this platform: ESTOP writes cannot be serialized")
    fd = os.open(str(directory / LOCK_NAME), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)

            def unlock() -> None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        else:
            # msvcrt locks a byte range and fails on an empty file: seed one byte first (the same
            # convention as gateway/status.py::_try_acquire_file_lock and agent/vault_store.py).
            if os.fstat(fd).st_size == 0:
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, b"\n")
            deadline = time.monotonic() + _LOCK_TIMEOUT_S
            while True:
                os.lseek(fd, 0, os.SEEK_SET)
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    # Only contention is retried; any other failure (bad handle, unusable file) is real.
                    if exc.errno not in _LOCK_CONTENTION_ERRNOS or time.monotonic() >= deadline:
                        raise
                    time.sleep(_LOCK_POLL_S)

            def unlock() -> None:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        try:
            yield
        finally:
            with suppress(OSError):
                unlock()
    finally:
        os.close(fd)


def _atomic_write(path: Path, payload: dict) -> None:
    """Write-temp + fsync + ``os.replace``: readers see the old or the new sentinel, never a partial."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2) + "\n")
            fh.flush()
            with suppress(OSError):
                os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with suppress(OSError):
            tmp.unlink()
        raise


def _place_full_sentinel(path: Path, payload: dict) -> None:
    """Make ``path`` hold a FULL pause, or raise ``OSError`` (nothing is claimed that did not land).

    First choice is the atomic replace. If that fails (disk full, temp file unwritable) the sentinel is
    truncated and rewritten IN PLACE: there is never a moment without a hold, and an empty or partial body is
    itself a FULL pause, so even a failed rewrite replaces an owned scoped body by a stronger hold. Finally the
    result is read back: only a FULL hold counts as success.
    """
    try:
        _atomic_write(path, payload)
        return
    except OSError as first:
        failure = first
    with suppress(OSError):
        fd = os.open(str(path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2) + "\n")
    try:
        landed = _load(path, include_expired=True)
    except OSError:
        landed = None
    if landed is None or landed.mode != "full":
        raise failure


def engage(reason: Optional[str] = None) -> Path:
    """Create the (legacy, FULL) ESTOP sentinel. Idempotent; re-engaging updates the file. An owned
    scoped hold in the same home is replaced — an operator pause only ever strengthens.

    Raises ``OSError`` when no FULL pause could be put in place (and the caller must say so): it never
    returns a path for a pause that is not holding.
    """
    path = sentinel_path()
    payload = {"engaged_at": datetime.now(UTC).isoformat(), "reason": reason or None}
    with suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        with suppress(OSError):  # no usable lock: still pause — an operator pause only ever fails safe
            stack.enter_context(_locked(path.parent))
        _place_full_sentinel(path, payload)
    return path


def disengage() -> bool:
    """Remove every visible sentinel (process-local and fleet-root), whoever owns it: the operator
    override behind ``hermes resume`` and ``/pause off``.

    Each removal happens under that directory's ``ESTOP.lock``, so an owner's ``renew`` / ``set_scopes`` /
    ``engage_scoped`` that already passed its ownership check finishes first and cannot ``os.replace`` the
    hold back afterwards; the owner's next call then finds :class:`HoldLost`. The override never depends on
    the lock being obtainable: if it is not, the sentinel is removed unserialized.
    """
    lifted = False
    for path in _candidate_sentinel_paths():
        try:
            present = path.exists()
        except OSError:
            present = True  # cannot tell: try to remove it
        except AttributeError:
            continue
        if not present:
            continue  # nothing to lift, and no lock file is created for it
        try:
            with ExitStack() as stack:
                with suppress(OSError):
                    stack.enter_context(_locked(path.parent))
                path.unlink()
            lifted = True
        except (OSError, AttributeError):
            continue
    return lifted


def _normalise_scopes(scopes: Any) -> list[str]:
    if isinstance(scopes, (str, bytes)) or scopes is None:
        raise InvalidScope(f"scopes must be a list of scope names, got {scopes!r}")
    try:
        items = list(scopes)
    except TypeError:
        raise InvalidScope(f"scopes must be a list of scope names, got {scopes!r}") from None
    bad = [s for s in items if not (isinstance(s, str) and s in _ALL)]
    if bad:
        raise InvalidScope(f"unknown scope(s) {bad!r}; valid: {list(ALL_SCOPES)}")
    if not items:
        raise InvalidScope("at least one scope is required")
    return _ordered(items)


def _check_ttl(ttl_s: Any) -> float:
    if not _is_real_number(ttl_s) or not 0 < ttl_s <= MAX_TTL_S:
        raise InvalidScope(f"ttl_s must be a number in (0, {MAX_TTL_S}], got {ttl_s!r}")
    return float(ttl_s)


def _new_token() -> str:
    """Fresh ownership token. Hex, never ``token_urlsafe``: that alphabet includes ``-``, and a token that
    starts with it cannot be passed as ``hermes pause --token T`` (argparse reads it as an option; ~1 in 64)."""
    return secrets.token_hex(16)


def _check_owner(owner: Any) -> str:
    if not isinstance(owner, str) or not owner.strip():
        raise InvalidScope("owner must be a non-empty string")
    return owner.strip()[:200]


def _check_reason(reason: Any) -> Optional[str]:
    return (reason.strip()[:500] or None) if isinstance(reason, str) else None


def _target_dir(home: Any) -> Path:
    return Path(home) if home is not None else sentinel_path().parent


def _public(hold: _Hold, *, token: bool = False) -> dict:
    out = {
        "mode": hold.mode,
        "scopes": _ordered(hold.scopes),
        "owner": hold.owner,
        "reason": hold.reason,
        "engaged_at": hold.engaged_at,
        "updated_at": hold.updated_at,
        "expires_at": hold.expires_at,
        "path": str(hold.path) if hold.path is not None else None,
    }
    if token:
        out["token"] = hold.token
    return out


def _owned(hold: Optional[_Hold], token: str) -> bool:
    return hold is not None and hold.mode == "scoped" and hold.token == token


def _write_scoped(directory: Path, *, token: str, owner: str, scopes: list[str], reason: Optional[str],
                  engaged_at: str, expires_at: float) -> _Hold:
    path = directory / SENTINEL_NAME
    updated = _iso()
    _atomic_write(path, {
        "schema": SCOPED_ESTOP_VERSION, "kind": _KIND_SCOPED, "token": token, "owner": owner,
        "scopes": scopes, "reason": reason, "engaged_at": engaged_at, "updated_at": updated,
        "expires_at": expires_at,
    })
    return _Hold("scoped", frozenset(scopes), reason=reason, engaged_at=engaged_at, updated_at=updated,
                 owner=owner, token=token, expires_at=expires_at, path=path)


def engage_scoped(scopes: Iterable[str], *, owner: str, ttl_s: float, reason: Optional[str] = None,
                  token: Optional[str] = None, home: Any = None) -> dict:
    """Create (or, for the same ``token``, idempotently re-write) an owned scoped hold.

    ``home`` selects the directory whose sentinel is used: the process home by default, or e.g.
    :func:`fleet_root` to cover every profile gateway of the fleet. Returns the hold including its
    ``token`` (generated when not supplied) — the caller's only proof of ownership.

    Raises :class:`InvalidScope` (nothing written) or :class:`HoldConflict` when a live hold owned by
    someone else (legacy ``hermes pause``, a corrupt sentinel, another owner's scoped hold) is
    already there — it is never overwritten. OSError (e.g. unwritable home) propagates: no hold exists.
    """
    scope_list = _normalise_scopes(scopes)
    ttl = _check_ttl(ttl_s)
    owner = _check_owner(owner)
    if token is None:
        token = _new_token()
    elif not isinstance(token, str) or not token.strip():
        raise InvalidScope("token must be a non-empty string")
    directory = _target_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    with _locked(directory):
        current = _load(directory / SENTINEL_NAME)
        if current is not None and not _owned(current, token):
            raise HoldConflict(
                f"{directory / SENTINEL_NAME} is held by {current.owner or 'a legacy/foreign pause'}"
                f" ({current.mode}); not overwriting"
            )
        engaged_at = current.engaged_at if current is not None and current.engaged_at else _iso()
        hold = _write_scoped(directory, token=token, owner=owner, scopes=scope_list,
                             reason=_check_reason(reason), engaged_at=engaged_at, expires_at=_now() + ttl)
    return _public(hold, token=True)


def _transition(token: str, home: Any, mutate) -> dict:
    directory = _target_dir(home)
    if not directory.is_dir():
        raise HoldLost(f"{directory} does not exist")
    with _locked(directory):
        current = _load(directory / SENTINEL_NAME)
        if not _owned(current, token):
            raise HoldLost("no live hold owned by this token (released, expired, or replaced)")
        hold = mutate(directory, current)
    return _public(hold, token=True)


def set_scopes(token: str, scopes: Iterable[str], *, ttl_s: Optional[float] = None,
               reason: Any = _KEEP, home: Any = None) -> dict:
    """Atomically move an owned hold to ``scopes`` (e.g. background -> background + turns, and back).

    There is never a moment without the hold: the sentinel is replaced in one ``os.replace``. The
    deadman is re-armed to ``now + ttl_s`` when ``ttl_s`` is given, otherwise left as it was.
    Raises :class:`InvalidScope` (hold untouched) or :class:`HoldLost`.
    """
    scope_list = _normalise_scopes(scopes)
    ttl = None if ttl_s is None else _check_ttl(ttl_s)

    def mutate(directory: Path, cur: _Hold) -> _Hold:
        return _write_scoped(
            directory, token=cur.token, owner=cur.owner, scopes=scope_list,
            reason=cur.reason if reason is _KEEP else _check_reason(reason),
            engaged_at=cur.engaged_at or _iso(), expires_at=cur.expires_at if ttl is None else _now() + ttl,
        )

    return _transition(token, home, mutate)


def renew(token: str, ttl_s: float, *, home: Any = None) -> dict:
    """Re-arm the deadman of an owned, still-live hold to ``now + ttl_s``. Raises :class:`HoldLost`
    once it has expired or been replaced — the owner must then treat readiness as lost."""
    ttl = _check_ttl(ttl_s)

    def mutate(directory: Path, cur: _Hold) -> _Hold:
        return _write_scoped(directory, token=cur.token, owner=cur.owner, scopes=_ordered(cur.scopes),
                             reason=cur.reason, engaged_at=cur.engaged_at or _iso(), expires_at=_now() + ttl)

    return _transition(token, home, mutate)


def release(token: str, *, home: Any = None) -> bool:
    """Remove the hold iff ``token`` owns it. True = a live owned hold was lifted; False = nothing of
    ours was live (already released, expired — the stale file is cleaned — or replaced by someone
    else's hold, which is never touched)."""
    directory = _target_dir(home)
    path = directory / SENTINEL_NAME
    if not directory.is_dir():
        return False
    with _locked(directory):
        current = _load(path, include_expired=True)
        if not _owned(current, token):
            return False
        with suppress(FileNotFoundError):
            path.unlink()
        return not current.expired(_now())


def verify(token: str, *, home: Any = None) -> Optional[dict]:
    """The live hold owned by ``token`` (without the token), or None if it is gone/expired/replaced."""
    path = _target_dir(home) / SENTINEL_NAME
    try:
        current = _load(path)
    except OSError:
        return None
    return _public(current) if _owned(current, token) else None


# ── reporting ───────────────────────────────────────────────────────────────


def foreign_holds(token: str) -> list[dict]:
    """Every visible hold (profile home and fleet root) that is NOT the live scoped hold owned by
    ``token`` — a legacy/operator pause, another owner's hold, or the caller's own hold after it was
    replaced. :func:`verify` only inspects one home; an owner that must not proceed while anything
    else is holding (e.g. before a restart) asks for ``foreign_holds(token) == []`` as well."""
    return [_public(h) for h in _visible_holds() if not _owned(h, token)]


def live_hold_by_token_digest(digest: str) -> Optional[dict]:
    """The live SCOPED hold (profile home or fleet root) whose token hashes to ``digest`` (SHA-256 hex), without
    its token, or None. It lets the running gateway authenticate a request that proves it by the owner's token
    (``gateway/restart_if_idle.py``): only the holder of a live hold can ask, a full/legacy pause, an expired or a
    released hold cannot. The comparison is constant-time."""
    if not isinstance(digest, str) or not digest:
        return None
    for hold in _visible_holds():
        if hold.mode != "scoped" or not hold.token:
            continue
        if hmac.compare_digest(hashlib.sha256(hold.token.encode("utf-8")).hexdigest(), digest):
            return _public(hold)
    return None


def get_state() -> Optional[dict]:
    """Return the effective hold as ``{"reason", "engaged_at", "mode", "scopes", "owner",
    "expires_at"}`` or None when nothing holds; an unreadable/corrupt body still reports a FULL hold
    with reason/engaged_at None. Tokens are never reported."""
    holds = _visible_holds()
    if not holds:
        return None
    full = [h for h in holds if h.mode == "full"]
    if full:
        return {
            "reason": next((h.reason for h in full if h.reason), None),
            "engaged_at": next((h.engaged_at for h in full if h.engaged_at), None),
            "mode": "full", "scopes": list(ALL_SCOPES), "owner": None, "expires_at": None,
        }
    return {
        "reason": next((h.reason for h in holds if h.reason), None),
        "engaged_at": next((h.engaged_at for h in holds if h.engaged_at), None),
        "mode": "scoped",
        "scopes": _ordered(set().union(*(h.scopes for h in holds))),
        "owner": ", ".join(dict.fromkeys(h.owner for h in holds if h.owner)) or None,
        "expires_at": min(h.expires_at for h in holds if h.expires_at is not None),
    }


def status() -> dict:
    """Snapshot for ``hermes pause --status``: effective mode/scopes plus every visible hold."""
    holds = _visible_holds()
    state = get_state()
    return {
        "engaged": bool(holds),
        "mode": state["mode"] if state else "none",
        "scopes": state["scopes"] if state else [],
        "holds": [_public(h) for h in holds],
    }


def paused_reply() -> Optional[str]:
    """Short user-facing notice for new gateway turns, or None when user turns are not held. A
    scoped hold that does not include :data:`SCOPE_TURNS` (background-only drain) returns None."""
    state = get_state()
    if state is None or SCOPE_TURNS not in state["scopes"]:
        return None
    tag = f" ({state['reason']})" if state.get("reason") else ""
    if state["mode"] == "full":
        return f"⏸️ Hermes is paused{tag}. New work is on hold; run `hermes resume` to pick things back up."
    return (f"⏸️ Hermes is paused{tag}. New work is on hold and will resume automatically when maintenance "
            "finishes; run `hermes resume` to lift it now.")


def check_paused(component: str, logger: logging.Logger) -> bool:
    """Return True when ``component`` is held, logging once per engagement per component (re-armed
    after the hold lifts)."""
    if not is_held(component):
        with _log_lock:
            _logged_components.discard(component)
        return False
    with _log_lock:
        first = component not in _logged_components
        _logged_components.add(component)
    if first:
        state = get_state() or {}
        suffix = f" (reason: {state['reason']})" if state.get("reason") else ""
        scope = "" if state.get("mode") == "full" else f" (scoped hold by {state.get('owner') or 'unknown'})"
        logger.info(
            "%s dispatch paused by global emergency stop%s%s — remove with `hermes resume` (%s)",
            component, scope, suffix, sentinel_path(),
        )
    return True
