"""Reapply local changes an update would otherwise leave behind.

``hermes update`` already parks a diverged HEAD on a rescue ref and then resets
the same branch onto the update target (``_reconcile_diverged_checkout``). A
conflicted autostash is parked and the working tree is cleaned. This module
does not repeat that move and never runs ``reset --hard``, ``stash drop``, or
``push``. It classifies what the rescue ref or a parked stash still holds:

* already on the update target (patch-id or an ancestor) -> drop
* applies cleanly and is not upstream -> reapply
* conflicts or cannot be checked -> leave it out and record it

The command defaults to a dry run. ``updates.carry_reapply`` selects the
update-hook mode: ``dry-run`` (default when unset), ``apply``, or ``off``.
Apply during an update cherry-picks onto the tree the updater already reset.
A standalone apply writes only a worktree outside the source checkout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


class _RedactFilter(logging.Filter):
    """Scrub credential shapes from log records, including tracebacks."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        if record.exc_info:
            import traceback
            record.exc_text = redact("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        return True


logger.addFilter(_RedactFilter())

_MODES = ("off", "dry-run", "apply")
_BANNED_HEAD = {"reset", "clean", "push", "rebase", "checkout", "switch"}
_BACKUP_NAMES = re.compile(r"(^|/)[^/]+\.(bak|orig|rej)(-|$)")
_REDACTIONS = (
    (re.compile(r"(?i)(authorization\s*:\s*bearer\s+)\S+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"), "bearer [REDACTED]"),
    (re.compile(r"([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@"), r"\1[REDACTED]@"),
    (re.compile(r"(?i)(x-api-key\s*[:=]\s*)\S+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:api[_-]?key|token|access_token)\s*[=:]\s*)\S+"), r"\1[REDACTED]"),
    (re.compile(r"([?&](?:api[_-]?key|token|access_token|key)=)[^&\s]+"), r"\1[REDACTED]"),
)


class CarryError(RuntimeError):
    """A carry step refused to run or could not be checked."""


def redact(text: str, extra: tuple[str, ...] = ()) -> str:
    """Mask credential shapes. ``extra`` values are scrubbed in full, never by suffix."""
    if not text:
        return text
    out = text
    for pattern, repl in _REDACTIONS:
        out = pattern.sub(repl, out)
    for secret in extra:
        if secret:
            out = out.replace(secret, "[REDACTED]")
    return out


def carry_mode(updates: dict | None = None) -> str:
    """``off`` | ``dry-run`` | ``apply``. Unset or unknown stays ``dry-run``."""
    if updates is None:
        updates = _updates_section()
    raw = str((updates or {}).get("carry_reapply", "dry-run")).strip().lower()
    if raw in {"0", "false", "off", "no"}:
        return "off"
    if raw in {"1", "true", "on", "apply"}:
        return "apply"
    if raw in _MODES:
        return raw
    return "dry-run"


def decide(change: dict) -> dict:
    """Classify one carried change. Does not touch git."""
    flags: list[str] = []
    pr = change.get("upstream_pr") or None
    state = str((pr or {}).get("state") or "").upper()
    match = str(change.get("pr_match") or "")
    if change.get("pr_lookup") == "failed":
        flags.append("pr_lookup_failed")
    elif not pr:
        flags.append("no_upstream_pr")
    if state == "CLOSED":
        flags.append("pr_closed_unmerged")
    apply_check = str(change.get("apply_check") or "unknown")
    merged = bool(change.get("patch_equivalent")) or (state == "MERGED" and match == "sha")
    if merged:
        flags = [flag for flag in flags if flag != "no_upstream_pr"]
        return {"action": "drop", "reason": "merged-upstream", "flags": flags}
    if apply_check == "empty":
        return {"action": "drop", "reason": "empty", "flags": flags}
    if apply_check != "clean":
        if apply_check == "unknown":
            flags.append("apply_check_unknown")
        reason = "conflict-left-out" if apply_check == "conflict" else "unknown-left-out"
        return {"action": "conflict", "reason": reason, "flags": flags}
    reason = "still-open" if state == "OPEN" else "not-upstream"
    return {"action": "reapply", "reason": reason, "flags": flags}


def _updates_section() -> dict:
    try:
        from hermes_cli.config import load_config
        loaded = load_config()
    except Exception:
        logger.exception("carry-reapply could not read config; using dry-run")
        return {}
    updates = loaded.get("updates") if isinstance(loaded, dict) else None
    return updates if isinstance(updates, dict) else {}


def _reject(args: list[str]) -> None:
    if not args:
        raise CarryError("refusing an empty git command")
    if args[0] in _BANNED_HEAD or "--hard" in args:
        raise CarryError("refusing a git command that would reset or switch the checkout")
    if args[0] == "stash" and len(args) > 1 and args[1] in {"drop", "pop", "apply", "clear"}:
        raise CarryError("refusing to drop or apply a stash")
    if args[0] == "branch" and any(flag in {"-D", "-d", "--delete"} for flag in args):
        raise CarryError("refusing to delete a branch")
    if args[0] == "update-ref" and "-d" in args:
        raise CarryError("refusing to delete a ref")


def _git(cwd: Path, args: list[str], *, git_bin: str = "git") -> subprocess.CompletedProcess:
    _reject(args)
    return subprocess.run(
        [git_bin, *args], cwd=cwd, capture_output=True, text=True, check=False, timeout=120,
    )


def _ok(proc: subprocess.CompletedProcess, what: str) -> str:
    if proc.returncode != 0:
        detail = redact((proc.stderr or proc.stdout or "").strip())
        raise CarryError(f"{what} failed: {detail or proc.returncode}")
    return proc.stdout


def _patch_id(diff: str, *, git_bin: str = "git") -> str:
    if not diff.strip():
        return ""
    proc = subprocess.run(
        [git_bin, "patch-id", "--stable"], input=diff, capture_output=True, text=True, check=False,
        timeout=120,
    )
    parts = (proc.stdout or "").split()
    return parts[0] if parts else ""


def _outside_source(source: Path, dest: Path) -> None:
    source_r, dest_r = source.resolve(), dest.resolve()
    if dest_r == source_r or source_r in dest_r.parents:
        raise CarryError("refusing to write inside the source checkout")


def _is_backup(path: str) -> bool:
    return bool(_BACKUP_NAMES.search(path))


def _cherry_marks(repo: Path, upstream: str, tip: str, *, git_bin: str) -> dict[str, str]:
    proc = _git(repo, ["cherry", upstream, tip], git_bin=git_bin)
    marks: dict[str, str] = {}
    if proc.returncode != 0:
        return marks
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] in {"+", "-"}:
            marks[parts[1]] = parts[0]
    return marks


def _commit_apply_check(repo: Path, sha: str, upstream: str, *, git_bin: str) -> str:
    parent = _git(repo, ["rev-parse", f"{sha}^"], git_bin=git_bin)
    if parent.returncode != 0:
        return "unknown"
    proc = _git(
        repo,
        ["merge-tree", "--write-tree", f"--merge-base={parent.stdout.strip()}", upstream, sha],
        git_bin=git_bin,
    )
    if proc.returncode == 0:
        return "clean"
    if proc.returncode == 1:
        return "conflict"
    return "unknown"


def _blob(repo: Path, upstream: str, path: str, *, git_bin: str) -> bytes | None:
    proc = subprocess.run(
        [git_bin, "show", f"{upstream}:{path}"], cwd=repo, capture_output=True, check=False,
        timeout=120,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def _matches_upstream(
    repo: Path, upstream: str, path: str, current: bytes | None, *, git_bin: str,
) -> bool:
    """True when ``current`` is already the upstream blob. Missing files are not a match."""
    if current is None:
        return False
    blob = _blob(repo, upstream, path, git_bin=git_bin)
    return blob is not None and blob == current


def _file_apply_check(repo: Path, patch: str, paths: list[str], upstream: str, *, git_bin: str) -> str:
    if not patch.strip():
        return "empty"
    temp = Path(tempfile.mkdtemp(prefix="carry-apply-"))
    try:
        for path in paths:
            blob = _blob(repo, upstream, path, git_bin=git_bin)
            dest = temp / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            if blob is not None:
                dest.write_bytes(blob)
        proc = subprocess.run(
            [git_bin, "apply", "--check", "--unsafe-paths", "-"],
            cwd=temp, input=patch, capture_output=True, text=True, check=False, timeout=120,
        )
        return "clean" if proc.returncode == 0 else "conflict"
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def _ahead(repo: Path, upstream: str, tip: str, *, git_bin: str) -> tuple[list[str], list[str]]:
    listed = _git(repo, ["rev-list", "--reverse", f"{upstream}..{tip}"], git_bin=git_bin)
    if listed.returncode != 0:
        raise CarryError(redact((listed.stderr or "could not list carried commits").strip()))
    shas = [line for line in listed.stdout.splitlines() if line]
    merges = _git(repo, ["rev-list", "--merges", f"{upstream}..{tip}"], git_bin=git_bin)
    merge_set = set(merges.stdout.splitlines()) if merges.returncode == 0 else set()
    return shas, [sha for sha in shas if sha in merge_set]


def _subject(repo: Path, sha: str, *, git_bin: str) -> str:
    proc = _git(repo, ["log", "-1", "--format=%s", sha], git_bin=git_bin)
    return (proc.stdout or sha).strip()


def _files_of(repo: Path, sha: str, *, git_bin: str) -> list[str]:
    proc = _git(repo, ["diff-tree", "--no-commit-id", "--name-only", "-r", sha], git_bin=git_bin)
    return [line for line in (proc.stdout or "").splitlines() if line]


def _classify_commit(repo, sha, upstream, marks, *, git_bin: str) -> dict:
    equivalent = marks.get(sha) == "-" or _git(
        repo, ["merge-base", "--is-ancestor", sha, upstream], git_bin=git_bin,
    ).returncode == 0
    apply_check = "clean" if equivalent else _commit_apply_check(repo, sha, upstream, git_bin=git_bin)
    item = {
        "kind": "commit",
        "identity": sha,
        "sha": sha,
        "title": _subject(repo, sha, git_bin=git_bin),
        "files": _files_of(repo, sha, git_bin=git_bin),
        "patch_equivalent": equivalent,
        "apply_check": apply_check,
    }
    item.update(decide(item))
    return item


def _worktree_items(repo: Path, upstream: str, *, git_bin: str) -> list[dict]:
    status = _git(repo, ["status", "--porcelain=v1", "-uall"], git_bin=git_bin)
    items = []
    if status.returncode != 0:
        return items
    for line in status.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if _is_backup(path):
            continue
        diff = _git(repo, ["diff", "HEAD", "--", path], git_bin=git_bin).stdout
        if not diff.strip():
            diff = _git(repo, ["diff", "--cached", "--", path], git_bin=git_bin).stdout
        file_path = repo / path
        current = file_path.read_bytes() if file_path.is_file() else None
        equivalent = _matches_upstream(repo, upstream, path, current, git_bin=git_bin)
        item = {
            "kind": "uncommitted",
            "identity": f"worktree:{path}",
            "sha": "",
            "title": path,
            "files": [path],
            "patch": diff,
            "patch_id": _patch_id(diff, git_bin=git_bin),
            "patch_equivalent": equivalent,
            "apply_check": "clean" if equivalent else _file_apply_check(
                repo, diff, [path], upstream, git_bin=git_bin,
            ),
        }
        item.update(decide(item))
        items.append(item)
    return items


def _stash_items(repo: Path, stash_ref: str, upstream: str, *, git_bin: str) -> list[dict]:
    names = _git(repo, ["stash", "show", "--name-only", stash_ref], git_bin=git_bin)
    items = []
    if names.returncode != 0:
        return items
    for path in [line for line in names.stdout.splitlines() if line]:
        diff_proc = _git(repo, ["diff", f"{stash_ref}^1", stash_ref, "--", path], git_bin=git_bin)
        diff = diff_proc.stdout if diff_proc.returncode == 0 else ""
        shown = subprocess.run(
            [git_bin, "show", f"{stash_ref}:{path}"],
            cwd=repo, capture_output=True, check=False, timeout=120,
        )
        current = shown.stdout if shown.returncode == 0 else None
        equivalent = _matches_upstream(repo, upstream, path, current, git_bin=git_bin)
        if equivalent:
            apply_check = "clean"
        elif diff_proc.returncode == 0:
            apply_check = _file_apply_check(repo, diff, [path], upstream, git_bin=git_bin)
        else:
            apply_check = "unknown"
        item = {
            "kind": "stash",
            "identity": f"stash:{stash_ref}:{path}",
            "sha": "",
            "stash_ref": stash_ref,
            "title": path,
            "files": [path],
            "patch": diff,
            "patch_id": _patch_id(diff, git_bin=git_bin),
            "patch_equivalent": equivalent,
            "apply_check": apply_check,
        }
        item.update(decide(item))
        items.append(item)
    return items


def _dedupe(items: list[dict]) -> list[dict]:
    seen: dict[str, str] = {}
    out = []
    for item in items:
        key = item.get("patch_id") or ""
        if key and key in seen:
            item = dict(item)
            item["action"] = "drop"
            item["reason"] = "duplicate"
            item["flags"] = list(item.get("flags") or []) + [f"duplicate-of:{seen[key]}"]
        elif key:
            seen[key] = item["identity"]
        out.append(item)
    return out


def plan_repo(
    repo: Path, *, upstream: str = "origin/main", tip: str | None = None,
    stashes: tuple[str, ...] = (), include_worktree: bool = True, git_bin: str = "git",
) -> dict:
    """Read-only plan. ``tip`` defaults to HEAD; pass a rescue ref to classify that snapshot."""
    repo = Path(repo)
    tip = tip or _ok(_git(repo, ["rev-parse", "HEAD"], git_bin=git_bin), "rev-parse").strip()
    upstream_sha = _ok(_git(repo, ["rev-parse", upstream], git_bin=git_bin), "rev-parse upstream").strip()
    shas, merges = _ahead(repo, upstream, tip, git_bin=git_bin)
    marks = _cherry_marks(repo, upstream, tip, git_bin=git_bin)
    items = []
    for sha in shas:
        if sha in merges:
            item = {
                "kind": "commit", "identity": sha, "sha": sha,
                "title": _subject(repo, sha, git_bin=git_bin),
                "files": _files_of(repo, sha, git_bin=git_bin),
                "patch_equivalent": False, "apply_check": "conflict",
            }
            item.update(decide(item))
            item["reason"] = "merge-commit-left-out"
            items.append(item)
            continue
        items.append(_classify_commit(repo, sha, upstream, marks, git_bin=git_bin))
    if include_worktree and tip == _ok(_git(repo, ["rev-parse", "HEAD"], git_bin=git_bin), "HEAD").strip():
        items.extend(_worktree_items(repo, upstream, git_bin=git_bin))
    for stash_ref in stashes:
        items.extend(_stash_items(repo, stash_ref, upstream, git_bin=git_bin))
    items = _dedupe(items)
    return {
        "upstream": upstream,
        "upstream_sha": upstream_sha,
        "tip": tip,
        "items": items,
    }


def verify_accounted(repo: Path, plan: dict, *, git_bin: str = "git") -> list[str]:
    """Every carried commit must still exist and be drop, conflict, or reapply."""
    missing = []
    for item in plan["items"]:
        sha = item.get("sha") or ""
        if not sha:
            continue
        kind = _git(repo, ["cat-file", "-t", sha], git_bin=git_bin)
        if kind.returncode != 0 or kind.stdout.strip() != "commit":
            missing.append(sha)
            continue
        if item.get("action") not in {"drop", "conflict", "reapply"}:
            missing.append(sha)
    return missing


def _cherry_pick(dest: Path, sha: str, *, git_bin: str) -> str:
    proc = _git(dest, ["cherry-pick", "--no-edit", sha], git_bin=git_bin)
    text = f"{proc.stdout}\n{proc.stderr}".lower()
    if proc.returncode == 0:
        return "reapply"
    if "now empty" in text or "empty" in text:
        _git(dest, ["cherry-pick", "--abort"], git_bin=git_bin)
        return "drop"
    _git(dest, ["cherry-pick", "--abort"], git_bin=git_bin)
    return "conflict"


def _apply_patch(dest: Path, patch: str, *, git_bin: str) -> str:
    if not patch.strip():
        return "drop"
    check = subprocess.run(
        [git_bin, "apply", "--check", "-"], cwd=dest, input=patch,
        capture_output=True, text=True, check=False, timeout=120,
    )
    if check.returncode != 0:
        return "conflict"
    applied = subprocess.run(
        [git_bin, "apply", "-"], cwd=dest, input=patch,
        capture_output=True, text=True, check=False, timeout=120,
    )
    return "reapply" if applied.returncode == 0 else "conflict"


def apply_in_worktree(
    repo: Path, plan: dict, dest: Path, branch: str, *, git_bin: str = "git",
) -> dict:
    """Create ``dest`` from the plan's upstream and replay clean changes. Source HEAD is not moved."""
    repo, dest = Path(repo), Path(dest)
    _outside_source(repo, dest)
    before = _ok(_git(repo, ["rev-parse", "HEAD"], git_bin=git_bin), "rev-parse").strip()
    if dest.exists():
        raise CarryError("worktree path already exists")
    dest.parent.mkdir(parents=True, exist_ok=True)
    added = _git(
        repo, ["worktree", "add", "-b", branch, str(dest), plan["upstream"]], git_bin=git_bin,
    )
    if added.returncode != 0:
        raise CarryError(redact((added.stderr or "worktree add failed").strip()))
    results = []
    try:
        for item in plan["items"]:
            if item.get("action") != "reapply":
                results.append({**_public(item), "applied": item.get("action")})
                continue
            if item["kind"] == "commit":
                applied = _cherry_pick(dest, item["sha"], git_bin=git_bin)
            else:
                applied = _apply_patch(dest, item.get("patch") or "", git_bin=git_bin)
            row = dict(item)
            row["action"] = applied if applied != "reapply" else item["action"]
            if applied == "drop":
                row["reason"] = "empty-on-apply"
            elif applied == "conflict":
                row["reason"] = "conflict-left-out"
            results.append({**_public(row), "applied": applied})
        dirty = _git(dest, ["status", "--porcelain"], git_bin=git_bin).stdout
        if dirty.strip():
            _git(dest, ["add", "-A"], git_bin=git_bin)
            _git(dest, [
                "-c", "user.name=carry-reapply", "-c", "user.email=carry-reapply@localhost",
                "commit", "-m", "carry-reapply: replay uncommitted edits",
            ], git_bin=git_bin)
    except Exception:
        logger.exception("carry-reapply worktree replay failed")
        raise
    after = _ok(_git(repo, ["rev-parse", "HEAD"], git_bin=git_bin), "rev-parse").strip()
    if after != before:
        raise CarryError("source checkout HEAD changed; refusing to continue")
    head = _ok(_git(dest, ["rev-parse", "HEAD"], git_bin=git_bin), "worktree HEAD").strip()
    return {"worktree": str(dest), "branch": branch, "head": head, "results": results}


def fingerprint(plan: dict) -> str:
    rows = [
        (item.get("identity"), item.get("action"), item.get("patch_id", ""))
        for item in plan.get("items") or []
    ]
    return hashlib.sha256(json.dumps(rows).encode("utf-8")).hexdigest()


def _public(item: dict) -> dict:
    skip = {"patch"}
    return {key: value for key, value in item.items() if key not in skip}


def summarize(plan: dict, results: list[dict] | None = None) -> dict:
    rows = results if results is not None else [_public(item) for item in plan["items"]]
    buckets = {"carried": [], "dropped_merged": [], "dropped_other": [], "reapplied": [], "conflicted": []}
    for row in rows:
        buckets["carried"].append(row)
        action = row.get("applied") or row.get("action")
        reason = row.get("reason")
        if action == "drop" and reason == "merged-upstream":
            buckets["dropped_merged"].append(row)
        elif action == "drop":
            buckets["dropped_other"].append(row)
        elif action == "reapply":
            buckets["reapplied"].append(row)
        else:
            buckets["conflicted"].append(row)
    return buckets


def format_report(report: dict, *, quiet: bool) -> str:
    buckets = report.get("buckets") or summarize(report)
    line = (
        "carry-reapply "
        f"carried={len(buckets['carried'])} "
        f"dropped={len(buckets['dropped_merged'])} "
        f"other={len(buckets.get('dropped_other') or [])} "
        f"reapplied={len(buckets['reapplied'])} "
        f"conflicted={len(buckets['conflicted'])}"
    )
    if quiet:
        return redact(line)
    lines = [line]
    for label, key in (
        ("dropped-merged", "dropped_merged"),
        ("dropped", "dropped_other"),
        ("reapplied", "reapplied"),
        ("conflicted", "conflicted"),
    ):
        for row in buckets[key]:
            flags = ",".join(row.get("flags") or [])
            lines.append(
                f"  {label}: {row.get('identity')} {row.get('title', '')} "
                f"reason={row.get('reason', '')} flags={flags}"
            )
    return redact("\n".join(lines))


def _hook_plan(cwd: Path, *, upstream_ref: str, tip: str, stashes: tuple[str, ...], git_bin: str) -> dict | None:
    mode = carry_mode()
    if mode == "off":
        return None
    if _git(cwd, ["cat-file", "-t", tip], git_bin=git_bin).stdout.strip() != "commit":
        print("carry-reapply: pre-update commit is not readable; nothing was dropped")
        return None
    plan = plan_repo(
        cwd, upstream=upstream_ref, tip=tip, stashes=stashes,
        include_worktree=False, git_bin=git_bin,
    )
    missing = verify_accounted(cwd, plan, git_bin=git_bin)
    if missing:
        print("carry-reapply: carried commits are not all readable; apply skipped")
        return None
    plan["mode"] = mode
    return plan


def reapply_after_diverged_reset(git_cmd, cwd, *, pre_pull_sha: str, upstream_ref: str) -> None:
    """After the updater's own reset, plan (and maybe replay) commits parked on ``pre_pull_sha``.

    Never resets. A planner failure is logged and does not fail the update.
    """
    try:
        git_bin = git_cmd[0] if git_cmd else "git"
        plan = _hook_plan(
            Path(cwd), upstream_ref=upstream_ref, tip=pre_pull_sha, stashes=(), git_bin=git_bin,
        )
        if plan is None:
            return
        if plan["mode"] == "dry-run":
            print(format_report(plan, quiet=False))
            return
        _replay_in_place(Path(cwd), plan, git_bin=git_bin)
    except Exception:
        logger.exception("carry-reapply after diverged reset failed")
        print("carry-reapply: skipped; the update result is unchanged")


def reapply_parked_stash(git_cmd, cwd, stash_ref: str) -> None:
    """Per-file replay of a stash the updater kept after a conflict. Never drops the stash."""
    try:
        git_bin = git_cmd[0] if git_cmd else "git"
        head = _git(Path(cwd), ["rev-parse", "HEAD"], git_bin=git_bin).stdout.strip()
        plan = _hook_plan(
            Path(cwd), upstream_ref="HEAD", tip=head, stashes=(stash_ref,), git_bin=git_bin,
        )
        if plan is None:
            return
        if plan["mode"] == "dry-run":
            print(format_report(plan, quiet=False))
            return
        _replay_in_place(Path(cwd), plan, git_bin=git_bin)
    except Exception:
        logger.exception("carry-reapply parked stash failed")
        print("carry-reapply: stash left parked; nothing was dropped")


def _replay_in_place(cwd: Path, plan: dict, *, git_bin: str = "git") -> None:
    for item in plan["items"]:
        if item.get("action") != "reapply":
            continue
        if item["kind"] == "commit":
            applied = _cherry_pick(cwd, item["sha"], git_bin=git_bin)
        else:
            applied = _apply_patch(cwd, item.get("patch") or "", git_bin=git_bin)
        item["applied"] = applied
    print(format_report(plan, quiet=False))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes_cli.update_carry")
    parser.add_argument("--repo", default=".", help="Checkout to read. Not modified unless --in-place.")
    parser.add_argument("--upstream", default="origin/main")
    parser.add_argument("--tip", default=None, help="Classify this commit (a rescue ref) instead of HEAD.")
    parser.add_argument("--stash", action="append", default=[], help="Parked stash ref. Repeatable.")
    parser.add_argument("--fetch", action="store_true", help="Fetch the upstream remote. Off by default.")
    parser.add_argument("--apply", action="store_true", help="Replay clean changes. Default is dry-run.")
    parser.add_argument("--worktree", default=None, help="Directory outside the source checkout for --apply.")
    parser.add_argument("--branch", default=None, help="Branch name for --worktree.")
    parser.add_argument("--in-place", action="store_true", help="Replay onto --repo. Refuses without this flag.")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--state-dir", default=None, help="Remember the last apply so a repeat is a no-op.")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    repo = Path(args.repo).resolve()
    try:
        if args.fetch:
            remote, _, branch = args.upstream.partition("/")
            if not branch:
                raise CarryError("upstream must look like remote/branch")
            fetched = _git(repo, ["fetch", remote, branch])
            if fetched.returncode != 0:
                raise CarryError(redact((fetched.stderr or "fetch failed").strip()))
        plan = plan_repo(
            repo, upstream=args.upstream, tip=args.tip, stashes=tuple(args.stash),
        )
        missing = verify_accounted(repo, plan)
        report = {"plan": {k: v for k, v in plan.items() if k != "items"}, "buckets": summarize(plan)}
        report["verification"] = {"ok": not missing, "unaccounted": missing}
        fp = fingerprint(plan)
        if args.apply and args.state_dir and not args.in_place:
            state_path = Path(args.state_dir) / "state.json"
            if state_path.is_file():
                previous = json.loads(state_path.read_text(encoding="utf-8-sig"))
                same = previous.get("fingerprint") == fp and previous.get("upstream_sha") == plan["upstream_sha"]
                if same and previous.get("worktree") and Path(previous["worktree"]).is_dir():
                    sys.stdout.write("carry-reapply: unchanged\n")
                    return 0
        if args.apply:
            if args.in_place:
                _replay_in_place(repo, plan)
                report["buckets"] = summarize(plan)
            elif not args.worktree:
                raise CarryError("apply requires --worktree (outside the source checkout) or --in-place")
            else:
                branch = args.branch or "carry-reapply"
                applied = apply_in_worktree(repo, plan, Path(args.worktree), branch)
                report["integration"] = applied
                report["buckets"] = summarize(plan, applied["results"])
                if args.state_dir:
                    state_path = Path(args.state_dir)
                    state_path.mkdir(parents=True, exist_ok=True)
                    (state_path / "state.json").write_text(json.dumps({
                        "fingerprint": fp,
                        "upstream_sha": plan["upstream_sha"],
                        "worktree": applied["worktree"],
                        "head": applied["head"],
                    }), encoding="utf-8")
        text = format_report({"buckets": report["buckets"]}, quiet=args.quiet)
        if args.json:
            payload = redact(json.dumps(report, indent=2, default=str))
            sys.stdout.write(payload + "\n")
        else:
            sys.stdout.write(text + "\n")
        return 0 if report["verification"]["ok"] else 2
    except CarryError as exc:
        sys.stderr.write(redact(str(exc)) + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
