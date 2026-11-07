"""Refuse a plain ``hermes update`` on a host that ships through release folders.

A plain update fetches upstream and moves the checkout to it, which silently drops every
carried (fork-only) commit. The branch-reset guard in ``update_cmd`` cannot see this case: a
release-folder install is a DETACHED HEAD, so it never reaches the branch checks. Every entry
point (CLI, gateway ``/update``, dashboard button, desktop app) runs ``hermes update`` ->
``main.cmd_update``, so one check there covers them all.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

ALLOW_ENV = "HERMES_ALLOW_PLAIN_UPDATE"
RELEASES_DIRNAME = "hermes-agent-releases"
_TRUTHY = {"1", "true", "yes", "on"}

REFUSAL_MESSAGE = (
    "This host updates through byrdit-live-update release-build / release-switch; "
    "plain hermes update drops the Byrd-IT carries."
)
OVERRIDE_HINT = f"To run a plain update deliberately, re-run with {ALLOW_ENV}=1 hermes update."


def _in_release_folder(root: Path) -> bool:
    try:
        resolved = Path(os.path.realpath(root))
    except OSError:
        return False
    return RELEASES_DIRNAME in resolved.parts[:-1]


def _carried_commit_count(root: Path) -> int:
    """Commits in HEAD that no ``upstream/*`` remote-tracking ref contains; 0 when unknown."""
    try:
        refs = subprocess.run(
            ["git", "-C", str(root), "for-each-ref", "--count=1", "--format=%(refname)", "refs/remotes/upstream"],
            capture_output=True, text=True, timeout=20, check=False)
        if refs.returncode != 0 or not refs.stdout.strip():
            return 0  # no upstream remote refs: nothing to compare against
        out = subprocess.run(
            ["git", "-C", str(root), "rev-list", "--count", "HEAD", "--not", "--remotes=upstream"],
            capture_output=True, text=True, timeout=20, check=False)
        return int(out.stdout.strip()) if out.returncode == 0 else 0
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def plain_update_refusal(project_root: Path) -> Optional[str]:
    """The refusal text when a plain update must not run on this install, else None."""
    if os.environ.get(ALLOW_ENV, "").strip().lower() in _TRUTHY:
        return None
    root = Path(project_root)
    if _in_release_folder(root):
        why = f"the live install is a release folder under {RELEASES_DIRNAME}/"
    else:
        carried = _carried_commit_count(root)
        if carried <= 0:
            return None
        why = f"HEAD carries {carried} commit(s) that are not in upstream"
    return f"✗ {REFUSAL_MESSAGE}\n  ({why}; nothing was changed.)\n  {OVERRIDE_HINT}"


def refuse_plain_update_or_exit(project_root: Path, gateway_mode: bool = False) -> None:
    """Print the refusal and exit 1 when a plain update must not run; return otherwise."""
    refusal = plain_update_refusal(project_root)
    if refusal is None:
        return
    import sys

    print(refusal)
    from hermes_cli.update_cmd_common import _record_stop
    _record_stop("managed_install", without_receipt="refused")  # closest closed token: admission refused
    if gateway_mode:
        from hermes_cli.update_cmd_fleet import _write_gateway_update_exit_code
        _write_gateway_update_exit_code(False)
    sys.exit(1)
