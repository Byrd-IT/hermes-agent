"""Scoped ESTOP: operator overrides racing an owner's writes, and a failing pause write.

Regression tests for the final-review blockers:

* ``disengage()`` (``hermes resume`` / ``/pause off``) must take ``ESTOP.lock`` like every other writer,
  or a ``renew`` / ``set_scopes`` that already passed its ownership check ``os.replace``\\ s the hold back
  after the operator's resume reported success.
* ``engage()`` (``hermes pause`` / ``/pause``) must never report a global pause that is not in place: when the
  atomic write fails it has to leave a FULL sentinel behind (replacing an owned scoped body), or fail loudly.

The concurrency tests use real threads and the real flock; only a delay is injected between a writer's
ownership check and its ``os.replace`` to make the (millisecond) window deterministic.
"""

from __future__ import annotations

import argparse
import errno
import os
import threading
import time

import pytest

from agent import estop

BG = estop.BACKGROUND_SCOPES


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    estop._logged_components.clear()
    return tmp_path


@pytest.fixture
def profile_and_root(tmp_path, monkeypatch):
    root = tmp_path / "hermes-root"
    profile = root / "profiles" / "inline-coder"
    profile.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(profile))
    estop._logged_components.clear()
    return root, profile


def _hold(**kw):
    return estop.engage_scoped(kw.pop("scopes", BG), owner="updater", ttl_s=kw.pop("ttl_s", 60), **kw)


def _fail_tmp_writes(monkeypatch, err=errno.ENOSPC):
    """The atomic temp file cannot be created (disk full); everything else works."""
    real_open = os.open

    def fake_open(path, flags, *a, **k):
        if str(path).endswith(".tmp"):
            raise OSError(err, os.strerror(err))
        return real_open(path, flags, *a, **k)

    monkeypatch.setattr(os, "open", fake_open)


# ── disengage vs in-flight owner writes ─────────────────────────────────────


@pytest.mark.parametrize("writer", ["renew", "set_scopes", "engage_scoped"])
def test_resume_waits_for_an_in_flight_owner_write_and_is_not_undone(hermes_home, monkeypatch, writer):
    """The owner's write passed its ownership check and is about to replace the sentinel when the operator
    resumes. The resume must serialize behind it and win; the hold must be gone afterwards."""
    token = _hold()["token"]
    real_write = estop._write_scoped
    in_write, proceed = threading.Event(), threading.Event()

    def gated_write(*a, **k):
        in_write.set()
        assert proceed.wait(10)
        return real_write(*a, **k)

    monkeypatch.setattr(estop, "_write_scoped", gated_write)
    calls = {
        "renew": lambda: estop.renew(token, 60),
        "set_scopes": lambda: estop.set_scopes(token, [*BG, estop.SCOPE_TURNS]),
        "engage_scoped": lambda: estop.engage_scoped(BG, owner="updater", ttl_s=60, token=token),
    }
    owner = threading.Thread(target=calls[writer])
    owner.start()
    assert in_write.wait(10)

    result: dict = {}
    resume = threading.Thread(target=lambda: result.setdefault("lifted", estop.disengage()))
    resume.start()
    resume.join(0.4)
    assert resume.is_alive(), "disengage() must wait for ESTOP.lock while an owner write is in flight"

    proceed.set()
    owner.join(10)
    resume.join(10)
    assert not owner.is_alive() and not resume.is_alive()
    assert result["lifted"] is True
    assert not estop.is_engaged(), "an operator resume must not be undone by a write that was already in flight"
    assert estop.verify(token) is None


def test_resume_under_sustained_owner_writes_never_leaves_a_resurrected_hold(hermes_home, monkeypatch):
    """Real contention: several owner threads hammer renew/set_scopes (slowed a little so a write is nearly always
    in flight) while the operator resumes. Whatever interleaving happens, once every thread has finished the
    operator's resume must be the last word."""
    real_write = estop._write_scoped

    def slow_write(*a, **k):
        time.sleep(0.004)
        return real_write(*a, **k)

    monkeypatch.setattr(estop, "_write_scoped", slow_write)
    for _ in range(12):
        token = _hold()["token"]
        stop = threading.Event()
        errors: list = []

        def hammer(i):
            n = 0
            while not stop.is_set() and n < 400:
                n += 1
                try:
                    if i % 2:
                        estop.renew(token, 60)
                    else:
                        estop.set_scopes(token, BG if n % 2 else [*BG, estop.SCOPE_TURNS])
                except estop.HoldLost:
                    return  # the owner finds the operator's resume, as documented
                except Exception as exc:  # pragma: no cover - would be a real bug
                    errors.append(exc)
                    return

        threads = [threading.Thread(target=hammer, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        time.sleep(0.03)
        assert estop.disengage() is True
        stop.set()
        for t in threads:
            t.join(15)
        assert not any(t.is_alive() for t in threads)
        assert errors == []
        assert not estop.is_engaged(), "a hold survived `hermes resume`"
        assert not (hermes_home / "ESTOP").exists()


def test_resume_lifts_the_fleet_root_and_profile_holds_under_their_own_locks(profile_and_root):
    root, profile = profile_and_root
    estop.engage_scoped(BG, owner="a", ttl_s=60)
    estop.engage_scoped(BG, owner="b", ttl_s=60, home=root)
    seen = []
    real_locked = estop._locked

    def spy(directory):
        seen.append(directory)
        return real_locked(directory)

    estop_locked = estop._locked
    try:
        estop._locked = spy
        assert estop.disengage() is True
    finally:
        estop._locked = estop_locked
    assert profile in seen and root in seen
    assert not estop.is_engaged()


def test_resume_still_works_when_the_lock_file_cannot_be_opened(hermes_home, monkeypatch):
    """The operator override must not depend on the lock being obtainable (e.g. a lock file owned by another user)."""
    _hold()
    lock = hermes_home / estop.LOCK_NAME
    real_open = os.open

    def fake_open(path, flags, *a, **k):
        if str(path) == str(lock):
            raise PermissionError(errno.EACCES, "denied")
        return real_open(path, flags, *a, **k)

    monkeypatch.setattr(os, "open", fake_open)
    assert estop.disengage() is True
    assert not estop.is_engaged()


def test_resume_without_a_sentinel_reports_nothing_lifted_and_creates_no_lock(hermes_home):
    assert estop.disengage() is False
    assert not (hermes_home / estop.LOCK_NAME).exists()


# ── engage() when the atomic write fails ────────────────────────────────────


def test_engage_write_failure_over_an_owned_scoped_hold_still_pauses_user_turns(hermes_home, monkeypatch):
    """`hermes pause` over a background-only owned hold, with the temp file unwritable (ENOSPC): the old
    `path.touch()` fallback kept the scoped body, so the CLI said 'paused' while turns kept running."""
    token = _hold()["token"]
    assert estop.is_held("turns") is False
    _fail_tmp_writes(monkeypatch)
    path = estop.engage("operator pause")
    assert path == hermes_home / "ESTOP"
    assert estop.is_held(estop.SCOPE_TURNS) is True, "engage() reported success but user turns are not paused"
    assert all(estop.is_held(c) for c in BG)
    state = estop.get_state()
    assert state["mode"] == "full" and state["owner"] is None
    with pytest.raises(estop.HoldLost):
        estop.renew(token, 60)
    assert estop.verify(token) is None


def test_engage_write_failure_with_no_hold_creates_a_full_sentinel(hermes_home, monkeypatch):
    _fail_tmp_writes(monkeypatch)
    estop.engage("disk full")
    assert estop.get_state()["mode"] == "full"
    assert estop.is_held(estop.SCOPE_TURNS)


def test_engage_write_failure_replaces_a_scoped_hold_in_the_fleet_root_only_for_its_own_home(profile_and_root, monkeypatch):
    """engage() writes the process home; a fleet-root hold is a different file and is unioned, never edited."""
    root, _profile = profile_and_root
    estop.engage_scoped(BG, owner="root-owner", ttl_s=60, home=root)
    estop.engage_scoped(BG, owner="profile-owner", ttl_s=60)
    _fail_tmp_writes(monkeypatch)
    estop.engage("pause")
    assert estop.is_held(estop.SCOPE_TURNS)
    assert (root / "ESTOP").read_text().count("root-owner") == 1, "the root hold must be left untouched"


def test_engage_that_cannot_land_anything_raises_and_never_claims_success(hermes_home, monkeypatch):
    """Even the in-place fallback fails and the existing hold is only scoped: raise, do not return a path."""
    _hold()
    real_open = os.open

    def fake_open(path, flags, *a, **k):
        if str(path) == str(hermes_home / "ESTOP") or str(path).endswith(".tmp"):
            raise OSError(errno.EROFS, "read-only")
        return real_open(path, flags, *a, **k)

    monkeypatch.setattr(os, "open", fake_open)
    with pytest.raises(OSError):
        estop.engage("pause")
    assert estop.is_held(estop.SCOPE_TURNS) is False  # untouched: the caller was told it did not land


def test_engage_that_cannot_write_but_a_full_pause_already_stands_is_still_a_pause(hermes_home, monkeypatch):
    (hermes_home / "ESTOP").write_text("")  # `touch ESTOP`: legacy FULL pause
    _fail_tmp_writes(monkeypatch, errno.EROFS)
    real_open = os.open

    def fake_open(path, flags, *a, **k):
        if str(path) == str(hermes_home / "ESTOP"):
            raise OSError(errno.EROFS, "read-only")
        return real_open(path, flags, *a, **k)

    monkeypatch.setattr(os, "open", fake_open)
    assert estop.engage("again") == hermes_home / "ESTOP"
    assert estop.is_held(estop.SCOPE_TURNS)


def test_engage_does_not_leave_a_moment_without_any_hold_during_the_fallback(hermes_home, monkeypatch):
    """A concurrent reader must see a hold (old scoped, or full) at every instant: the fallback truncates in
    place rather than unlinking first."""
    sentinel = hermes_home / "ESTOP"
    stop = threading.Event()
    gaps: list = []

    def reader():
        while not stop.is_set():
            if not sentinel.exists():
                gaps.append(time.time())

    t = threading.Thread(target=reader)
    estop._write_scoped(hermes_home, token="t", owner="u", scopes=list(BG), reason=None,
                        engaged_at=estop._iso(), expires_at=time.time() + 60)
    t.start()
    try:
        for _ in range(150):
            with monkeypatch.context() as m:
                _fail_tmp_writes(m)
                estop.engage("pause")
            assert estop.is_held(estop.SCOPE_TURNS)
            # restore an owned background-only hold with one atomic replace (no gap of its own)
            estop._write_scoped(hermes_home, token="t", owner="u", scopes=list(BG), reason=None,
                                engaged_at=estop._iso(), expires_at=time.time() + 60)
    finally:
        stop.set()
        t.join(5)
    assert gaps == []


# ── callers report a failed pause honestly ──────────────────────────────────


def test_cli_pause_fails_loudly_when_no_pause_could_be_written(hermes_home, monkeypatch, capsys):
    from hermes_cli.subcommands import pause

    def boom(reason=None):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(estop, "engage", boom)
    rc = pause.cmd_pause(argparse.Namespace(reason="x"))
    out = capsys.readouterr()
    assert rc != 0
    assert "paused" not in out.out.lower().replace("not paused", "")
    assert "No space left" in (out.out + out.err)


@pytest.mark.asyncio
async def test_gateway_pause_command_does_not_claim_a_pause_that_did_not_land(hermes_home, monkeypatch):
    from gateway.run import GatewayRunner

    class Ev:
        text = "/pause x"

        def get_command(self):
            return "pause"

        def get_command_args(self):
            return "x"

    def boom(reason=None):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(estop, "engage", boom)
    reply = await object.__new__(GatewayRunner)._handle_pause_command(Ev())
    assert "not" in reply.lower() and "No space left" in reply
    assert "⏸️ Paused" not in reply


# ── locking on platforms without fcntl ──────────────────────────────────────


def test_no_file_lock_primitive_fails_writes_instead_of_pretending_to_serialize(hermes_home, monkeypatch):
    monkeypatch.setattr(estop, "fcntl", None)
    monkeypatch.setattr(estop, "msvcrt", None)
    with pytest.raises(OSError):
        _hold()
    assert not estop.is_engaged()
    # ...but the operator's pause/resume still work (best effort, unserialized): fail safe, never fail open.
    estop.engage("pause")
    assert estop.is_held(estop.SCOPE_TURNS)
    assert estop.disengage() is True


def test_windows_lock_uses_msvcrt_byte_lock_and_releases_it(hermes_home, monkeypatch):
    events: list = []

    class FakeMsvcrt:
        LK_NBLCK, LK_UNLCK = 2, 0

        @staticmethod
        def locking(fd, mode, nbytes):
            events.append(("lock" if mode == FakeMsvcrt.LK_NBLCK else "unlock", nbytes))

    monkeypatch.setattr(estop, "fcntl", None)
    monkeypatch.setattr(estop, "msvcrt", FakeMsvcrt)
    token = _hold()["token"]
    estop.renew(token, 60)
    assert events and events.count(("lock", 1)) == events.count(("unlock", 1)) == 2


def test_windows_lock_gives_up_after_its_deadline_rather_than_hanging(hermes_home, monkeypatch):
    class Busy:
        LK_NBLCK, LK_UNLCK = 2, 0

        @staticmethod
        def locking(fd, mode, nbytes):
            if mode == Busy.LK_NBLCK:
                raise OSError(errno.EACCES, "locked")

    monkeypatch.setattr(estop, "fcntl", None)
    monkeypatch.setattr(estop, "msvcrt", Busy)
    monkeypatch.setattr(estop, "_LOCK_TIMEOUT_S", 0.2)
    monkeypatch.setattr(estop, "_LOCK_POLL_S", 0.01)
    t0 = time.monotonic()
    with pytest.raises(OSError):
        _hold()
    assert time.monotonic() - t0 < 5


def test_generated_tokens_can_always_be_passed_to_the_cli_token_option(hermes_home):
    """token_urlsafe tokens start with '-' about 1 time in 64, which `--token T` cannot parse."""
    assert all(not estop._new_token().startswith("-") for _ in range(5000))
    assert not _hold()["token"].startswith("-")
