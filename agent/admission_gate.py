"""Process-wide admission gate for the conditional-restart decision (``gateway/restart_if_idle.py``).

The conditional restart freezes the gateway's own admission flag (``_draining``) and counts every kind of work in
ONE synchronous step on the loop thread. That alone only excludes callers that run on the loop thread. Worker
threads (the cron ticker, a manual cron run, the misfire sweep, the kanban dispatcher's claim/spawn, the kanban
auto-decomposer) pass their own gates earlier and register their work later; without more they can start work AFTER
an accepted idle count, and the restart then waits on it (an extra drain nobody asked for).

This gate closes that. A thread that is about to start work and change state does so inside a *span*::

    with gate.admit("cron_tick") as admitted:
        if not admitted:
            return                      # refused BEFORE any state change; the work stays due for the next gateway
        ...claim / register / spawn...

The decision then has three states:

* ``OPEN``      -- spans are admitted and counted.
* ``DECIDING``  -- the decision is running (freeze -> count -> acknowledge). New spans WAIT for the outcome
  (bounded, never while holding any lock the decision needs), so a refused decision costs a worker a few
  milliseconds, not a dropped fire. Spans entered before the decision began are COUNTED by it (so the idle count
  refuses while any worker is between its gate and its registration).
* ``CLOSED``    -- the decision was accepted and the restart is under way: new spans are refused.

Linearization: ``begin_decision`` and every ``admit`` serialize on one lock, so each span either entered before
the decision began (and is counted) or enters after it (and waits / is refused). There is no third case.

Deadlock freedom: the decision never waits for a span. A span never holds a lock while it waits. A thread that
already holds a span re-enters without waiting (so the cron ticker's registration nested inside its tick span
cannot wait for the decision that is counting it), and a span requested by the thread that IS running the decision
is refused at once instead of waiting for itself. "Holds a span" is the same fact for both entry points: ``admit``
(a ``with`` block) and ``enter`` (a handle that is ``exit()``-ed later, possibly by another thread) count toward the
entering thread's nesting depth, and a handle passed to a worker thread is ``adopt()``-ed there for the same reason.
"""

from __future__ import annotations

import contextlib
import threading
import time
from typing import Callable, Iterator, Optional

OPEN = "open"
DECIDING = "deciding"
CLOSED = "closed"

# A decision is milliseconds of CPU plus one small durable ack write. A worker waits at most this long for it;
# past that the span is REFUSED (fail closed: nothing was started, nothing was changed).
ADMIT_WAIT_S = 15.0


class _Depth:
    """How many spans one thread holds (mutated only under the gate's lock). A span handle remembers the depth
    record of the thread that ENTERED it, so ``exit()`` from another thread still releases that thread's nesting."""

    __slots__ = ("n",)

    def __init__(self) -> None:
        self.n = 0


class Span:
    """A handle for a span that outlives one ``with`` block (handed to another thread). ``exit`` is idempotent."""

    __slots__ = ("_depth", "_done", "_gate", "_lock", "_name")

    def __init__(self, gate: AdmissionGate, name: str, depth: _Depth) -> None:
        self._gate, self._name, self._done, self._lock, self._depth = gate, name, False, threading.Lock(), depth

    def exit(self) -> None:
        with self._lock:
            if self._done:
                return
            self._done = True
        self._gate._leave(self._name, self._depth)

    @contextlib.contextmanager
    def adopt(self) -> Iterator[None]:
        """Run a block on ANOTHER thread (the one the handle was handed to) as nested inside this span: spans it
        takes meanwhile do not wait for a decision that is counting this span (they would wait for nothing: the
        decision cannot be accepted while this span lives)."""
        depth = self._gate._depth()
        with self._gate._cond:
            depth.n += 1
        try:
            yield
        finally:
            with self._gate._cond:
                depth.n = max(depth.n - 1, 0)


class AdmissionGate:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._state = OPEN
        self._spans: dict[str, int] = {}
        self._decider: Optional[int] = None
        self._local = threading.local()
        self._clock = clock

    # ── the decision's side ──────────────────────────────────────────────────

    def begin_decision(self) -> bool:
        """Start the decision (call BEFORE counting). False when a decision is already running or was accepted."""
        with self._cond:
            if self._state != OPEN:
                return False
            self._state = DECIDING
            self._decider = threading.get_ident()
            return True

    def end_decision(self, accepted: bool) -> None:
        """Finish it: ``accepted`` closes the gate for good (the restart is under way); otherwise it reopens."""
        with self._cond:
            if self._state == DECIDING:
                self._state = CLOSED if accepted else OPEN
                self._decider = None
                self._cond.notify_all()

    def reopen(self) -> None:
        """Back to OPEN (a decision that could not complete, or an accepted restart that never started)."""
        with self._cond:
            self._state = OPEN
            self._decider = None
            self._cond.notify_all()

    def spans(self) -> dict[str, int]:
        """Spans entered and not yet left, by name (count them AFTER ``begin_decision``)."""
        with self._cond:
            return {k: v for k, v in self._spans.items() if v > 0}

    @property
    def state(self) -> str:
        with self._cond:
            return self._state

    # ── the worker's side ────────────────────────────────────────────────────

    def _depth(self) -> _Depth:
        d = getattr(self._local, "depth", None)
        if d is None:
            d = self._local.depth = _Depth()
        return d

    def enter(self, name: str, wait_s: Optional[float] = None) -> Optional[Span]:
        """Enter a span that the caller will ``exit()`` itself (possibly on another thread). ``None``: refused,
        nothing was started. Prefer :meth:`admit` when the span fits one ``with`` block."""
        depth = self._acquire(name, ADMIT_WAIT_S if wait_s is None else wait_s)
        return None if depth is None else Span(self, name, depth)

    @contextlib.contextmanager
    def admit(self, name: str, wait_s: Optional[float] = None) -> Iterator[bool]:
        depth = self._acquire(name, ADMIT_WAIT_S if wait_s is None else wait_s)
        if depth is None:
            yield False
            return
        try:
            yield True
        finally:
            self._leave(name, depth)

    def _acquire(self, name: str, wait_s: float) -> Optional[_Depth]:
        """Take a span for the calling thread; ``None`` = refused (nothing was counted). The span and the calling
        thread's nesting depth change in the SAME critical section, so a decision never sees one without the other."""
        me = threading.get_ident()
        depth = self._depth()
        with self._cond:
            if depth.n == 0:                           # nested inside this thread's own span: always allowed (the
                end = self._clock() + max(float(wait_s), 0.0)       # outer span is what the decision counts)
                while self._state == DECIDING:
                    if self._decider == me:
                        return None                    # the decision itself must never wait for its own span
                    left = end - self._clock()
                    if left <= 0 or not self._cond.wait(left):
                        if self._state == DECIDING:
                            return None                # fail closed: the decision did not finish in time
                if self._state == CLOSED:
                    return None
            self._spans[name] = self._spans.get(name, 0) + 1
            depth.n += 1
            return depth

    def _leave(self, name: str, depth: _Depth) -> None:
        with self._cond:
            n = self._spans.get(name, 0) - 1
            if n > 0:
                self._spans[name] = n
            else:
                self._spans.pop(name, None)
            depth.n = max(depth.n - 1, 0)


_GATE = AdmissionGate()


def gate() -> AdmissionGate:
    return _GATE


def reset_for_tests() -> AdmissionGate:
    """Fresh process-wide gate (tests only)."""
    global _GATE
    _GATE = AdmissionGate()
    return _GATE
