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
``apply`` during an update cherry-picks onto the tree the updater already
reset. That is not a worktree integration. Callers that must not write the
source checkout leave the hook at ``dry-run`` and use ``--apply --worktree``
(a directory outside the source). A repeat with a changed carried set uses a
new branch and a free worktree path. The previous branch is kept.
A standalone apply writes only a worktree outside the source checkout.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

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
# Modes git can replay as a file. Anything else (submodule, tree) is left out.
_FAITHFUL_BLOB_MODES = frozenset({"100644", "100755", "120000"})
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
        from hermes_cli.update_cmd import _updates_config
        return _updates_config()
    except Exception:  # health: allow BLE001 -- boundary omits credential-bearing exception text
        logger.error("carry-reapply could not read config; using dry-run")
        return {}


def _reject(args: list[str]) -> None:
    if not args:
        raise CarryError("refusing an empty git command")
    head = 0
    while head + 1 < len(args) and args[head] == "-c":
        head += 2
    if head >= len(args):
        raise CarryError("refusing a git command with no subcommand")
    if args[head] in _BANNED_HEAD or "--hard" in args:
        raise CarryError("refusing a git command that would reset or switch the checkout")
    args = args[head:]
    if args[0] == "stash" and len(args) > 1 and args[1] in {"drop", "pop", "apply", "clear"}:
        raise CarryError("refusing to drop or apply a stash")
    if args[0] == "branch" and any(flag in {"-D", "-d", "--delete"} for flag in args):
        raise CarryError("refusing to delete a branch")
    if args[0] == "update-ref" and "-d" in args:
        raise CarryError("refusing to delete a ref")


def _git(cwd: Path, args: list[str], *, git_bin: str = "git") -> subprocess.CompletedProcess:
    _reject(args)
    proc = subprocess.run([git_bin, "--literal-pathspecs", *args], cwd=cwd, capture_output=True, check=False, timeout=120)
    # text=True normalizes CR/CRLF, corrupting patches and newline-bearing paths.
    return subprocess.CompletedProcess(
        proc.args, proc.returncode,
        proc.stdout.decode("utf-8", errors="surrogateescape"),
        proc.stderr.decode("utf-8", errors="surrogateescape"),
    )


def _ok(proc: subprocess.CompletedProcess, what: str) -> str:
    if proc.returncode != 0:
        raise CarryError(f"{what} failed (git exit {proc.returncode})")
    return proc.stdout


def _patch_id(diff: str, *, git_bin: str = "git") -> str:
    if not diff.strip():
        return ""
    proc = subprocess.run(
        [git_bin, "patch-id", "--stable"], input=diff.encode("utf-8", errors="surrogateescape"), capture_output=True, check=False,
        timeout=120,
    )
    parts = proc.stdout.decode("utf-8", errors="surrogateescape").split()
    return parts[0] if parts else ""


def _outside_source(source: Path, dest: Path) -> None:
    source_r, dest_r = source.resolve(), dest.resolve()
    if dest_r == source_r or source_r in dest_r.parents:
        raise CarryError("refusing to write inside the source checkout")


def _is_backup(path: str) -> bool:
    return bool(_BACKUP_NAMES.search(path))


def _status_entries(repo: Path, *, git_bin: str) -> list[tuple[str, str]]:
    """Porcelain v1 ``-z`` records as ``(xy, path)``. Paths are not quoted."""
    proc = _git(repo, ["status", "--porcelain=v1", "-z", "-uall"], git_bin=git_bin)
    if proc.returncode != 0:
        return []
    parts = proc.stdout.split("\0")
    entries: list[tuple[str, str]] = []
    index = 0
    while index < len(parts):
        raw = parts[index]
        index += 1
        if len(raw) < 4:
            continue
        xy, path = raw[:2], raw[3:]
        if "R" in xy or "C" in xy:
            # A rename includes both the addition and the source deletion.
            if "R" in xy and index < len(parts):
                entries.append((" D", parts[index]))
            index += 1
        if path:
            entries.append((xy, path))
    return entries


def _diff_token(prefix: str, path: str) -> str:
    raw = (prefix + path).encode("utf-8", errors="surrogateescape")
    encoded = "".join(chr(b) if 33 <= b <= 126 and b not in (34, 92)
                      else f"\\{b:03o}" for b in raw)
    return f'"{encoded}"'


def _unusable_binary_summary(diff: str) -> bool:
    """True for git's non-applicable ``Binary files ... differ`` summary.

    A text hunk that merely contains those words is not a binary summary.
    ``git diff --binary`` emits ``GIT binary patch`` instead, which applies.
    """
    if "GIT binary patch" in diff:
        return False
    return any(line.startswith("Binary files ") for line in diff.splitlines())


def _normalize_diff(diff: str, path: str) -> str:
    """Rewrite a no-index diff so ``git apply`` uses ``path``."""
    if not diff.strip() or _unusable_binary_summary(diff):
        return ""
    lines = []
    for line in (part + "\n" for part in diff.split("\n")[:-1]):
        if line.startswith("diff --git "):
            lines.append(f"diff --git {_diff_token('a/', path)} {_diff_token('b/', path)}\n")
        elif line.startswith("--- /dev/null"):
            lines.append(line if line.endswith("\n") else line + "\n")
        elif line.startswith("--- "):
            lines.append(f"--- {_diff_token('a/', path)}\n")
        elif line.startswith("+++ /dev/null"):
            lines.append(line if line.endswith("\n") else line + "\n")
        elif line.startswith("+++ "):
            lines.append(f"+++ {_diff_token('b/', path)}\n")
        else:
            lines.append(line if line.endswith("\n") else line + "\n")
    return "".join(lines)


def _no_index_patch(rel: str, right: str, *, git_bin: str, cwd: Path, left: str = "/dev/null") -> str:
    """``git diff --binary --no-index``. Git lstats ``right``; a symlink is not opened."""
    proc = subprocess.run(
        [git_bin, "diff", "--no-index", "--binary", "--", left, right],
        cwd=cwd, capture_output=True, check=False, timeout=120,
    )
    if proc.returncode not in (0, 1):
        return ""
    return _normalize_diff(proc.stdout.decode("utf-8", errors="surrogateescape"), rel)


def _discard_tree(root: Path) -> None:
    """Remove a temp tree without following a symlink into the user's files."""
    if not root.exists():
        return
    # shutil.rmtree unlinks symlinks; it does not traverse their targets.
    shutil.rmtree(root)


def _carryable(path: Path) -> bool:
    try:
        mode = path.lstat().st_mode
    except OSError:
        return False
    return stat.S_ISREG(mode) or stat.S_ISLNK(mode)


def _stored_bytes(path: Path) -> bytes | None:
    """Bytes git would store. Link text only — ``read_bytes`` and ``hash-object`` follow."""
    try:
        mode = path.lstat().st_mode
    except OSError:
        return None
    if stat.S_ISLNK(mode):
        return os.fsencode(os.readlink(path))
    if stat.S_ISREG(mode):
        return path.read_bytes()
    return None


def _git_mode(path: Path) -> str | None:
    try:
        mode = path.lstat().st_mode
    except OSError:
        return None
    if stat.S_ISLNK(mode):
        return "120000"
    if not stat.S_ISREG(mode):
        return None
    return "100755" if mode & 0o111 else "100644"


def _materialize(root: Path, rel: str, mode: str | None, payload: bytes) -> Path | None:
    """Recreate ``payload`` at ``mode`` without opening a symlink target."""
    if mode not in _FAITHFUL_BLOB_MODES:
        return None
    dest = root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        if mode == "120000":
            dest.symlink_to(os.fsdecode(payload))
            return dest
        dest.write_bytes(payload)
        if mode == "100755":
            dest.chmod(0o755)
    except OSError:
        return None
    return dest


def _bytes_diff(
    path: str, current: bytes | None, baseline: bytes | None, *, git_bin: str,
    live: tuple[Path, str] | None = None, mode: str | None = None,
    baseline_mode: str | None = "100644",
) -> str:
    """Diff one path without touching an index.

    ``live`` is ``(cwd, relative arg)`` for a worktree path git must lstat
    itself, so executable and symlink modes survive. In-memory blobs (stash^3)
    are materialized at ``mode``; an unsupported mode yields "" rather than an
    empty regular file.
    """
    if live is None and current is None:
        return ""
    if live is None and mode not in (None, *_FAITHFUL_BLOB_MODES):
        return ""
    temp = Path(tempfile.mkdtemp(prefix="carry-diff-"))
    try:
        left = "/dev/null"
        if baseline is not None:
            baseline_path = _materialize(temp, "baseline", baseline_mode, baseline)
            if baseline_path is None:
                return ""
            left = str(baseline_path)
        if live is not None:
            cwd, right = live
            return _no_index_patch(path, right, git_bin=git_bin, cwd=cwd, left=left)
        tree = temp / "tree"
        made = _materialize(tree, path, mode or "100644", current or b"")
        if made is None:
            return ""
        return _no_index_patch(path, path, git_bin=git_bin, cwd=tree, left=left)
    finally:
        _discard_tree(temp)


def _empty_regular(current: bytes | None, mode: str | None, live: tuple[Path, str] | None) -> bool:
    if mode == "120000":
        return False
    if live is not None:
        cwd, right = live
        try:
            st = (cwd / right).lstat()
        except OSError:
            return False
        return stat.S_ISREG(st.st_mode) and st.st_size == 0
    return current == b""


def _addition_patch(
    path: str, current: bytes | None, *, git_bin: str,
    live: tuple[Path, str] | None = None, mode: str | None = None,
) -> str:
    """A new-file patch. Empty only when the source itself is an empty regular file."""
    patch = _bytes_diff(path, current, None, git_bin=git_bin, live=live, mode=mode)
    if patch.strip() or not _empty_regular(current, mode, live):
        return patch
    file_mode = "100755" if mode == "100755" or (live and _git_mode(live[0] / live[1]) == "100755") else "100644"
    return (
        f"diff --git {_diff_token('a/', path)} {_diff_token('b/', path)}\n"
        f"new file mode {file_mode}\n"
        "index 0000000..e69de29\n"
        "--- /dev/null\n"
        f"+++ {_diff_token('b/', path)}\n"
    )


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


def _tree_mode(repo: Path, ref: str, path: str, *, git_bin: str) -> str | None:
    proc = _git(repo, ["ls-tree", "-z", ref, "--", path], git_bin=git_bin)
    rows = _ls_tree_rows(proc.stdout)
    return rows[0][0] if proc.returncode == 0 and rows else None


def _matches_upstream(
    repo: Path, upstream: str, path: str, current: bytes | None, *, git_bin: str,
    mode: str | None = None,
) -> bool:
    """True when ``current`` is already the upstream blob. Missing files are not a match."""
    if current is None:
        return False
    blob = _blob(repo, upstream, path, git_bin=git_bin)
    return blob is not None and blob == current and mode == _tree_mode(repo, upstream, path, git_bin=git_bin)


def _file_apply_check(repo: Path, patch: str, paths: list[str], upstream: str, *, git_bin: str) -> str:
    if not patch.strip():
        return "empty"
    temp = Path(tempfile.mkdtemp(prefix="carry-apply-"))
    try:
        for path in paths:
            blob = _blob(repo, upstream, path, git_bin=git_bin)
            if blob is not None:
                mode = _tree_mode(repo, upstream, path, git_bin=git_bin)
                if _materialize(temp, path, mode, blob) is None:
                    return "unknown"
        proc = subprocess.run(
            [git_bin, "apply", "--check", "--unsafe-paths", "-"],
            cwd=temp, input=patch.encode("utf-8", errors="surrogateescape"), capture_output=True, check=False, timeout=120,
        )
        return "clean" if proc.returncode == 0 else "conflict"
    finally:
        shutil.rmtree(temp, ignore_errors=True)


def _ahead(repo: Path, upstream: str, tip: str, *, git_bin: str) -> tuple[list[str], list[str]]:
    listed = _git(repo, ["rev-list", "--reverse", f"{upstream}..{tip}"], git_bin=git_bin)
    if listed.returncode != 0:
        raise CarryError("could not list carried commits")
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


def _ls_tree_rows(text: str) -> list[tuple[str, str]]:
    rows = []
    for rec in text.split("\0"):
        if not rec:
            continue
        meta, sep, path = rec.partition("\t")
        if not sep or not path:
            continue
        rows.append((meta.split(" ", 1)[0], path))
    return rows


def _untracked_file_item(
    repo: Path, upstream: str, path: str, *, identity: str, kind: str,
    current: bytes | None, git_bin: str, extra: dict | None = None,
    live: tuple[Path, str] | None = None, mode: str | None = None,
) -> dict:
    """Classify a file git diff does not see (worktree ``??`` or stash^3)."""
    equivalent = _matches_upstream(repo, upstream, path, current, git_bin=git_bin, mode=mode)
    baseline = None if equivalent else _blob(repo, upstream, path, git_bin=git_bin)
    if equivalent:
        patch = ""
        apply_check = "clean"
    elif current is None and live is None:
        patch = ""
        apply_check = "unknown"
    elif baseline is None:
        patch = _addition_patch(path, current, git_bin=git_bin, live=live, mode=mode)
        apply_check = (
            _file_apply_check(repo, patch, [path], upstream, git_bin=git_bin)
            if patch.strip() else "unknown"
        )
    else:
        patch = _bytes_diff(
            path, current, baseline, git_bin=git_bin, live=live, mode=mode,
            baseline_mode=_tree_mode(repo, upstream, path, git_bin=git_bin),
        )
        apply_check = (
            _file_apply_check(repo, patch, [path], upstream, git_bin=git_bin)
            if patch.strip() else "unknown"
        )
    item = {
        "kind": kind,
        "identity": identity,
        "sha": "",
        "title": path,
        "files": [path],
        "patch": patch,
        "patch_id": _patch_id(patch, git_bin=git_bin),
        "patch_equivalent": equivalent,
        "apply_check": apply_check,
    }
    if extra:
        item.update(extra)
    item.update(decide(item))
    return item


def _rename_pairs(repo: Path, refs: list[str], *, git_bin: str) -> list[tuple[str, str]]:
    """Read Git's rename identities without splitting or unquoting filenames."""
    proc = _git(repo, ["diff", "--name-status", "-z", "--find-renames", "--diff-filter=R", *refs], git_bin=git_bin)
    fields = _ok(proc, "rename scan").split("\0")
    return [(fields[i + 1], fields[i + 2]) for i in range(0, len(fields) - 2, 3)
            if fields[i].startswith("R")]


def _atomic_renames(repo: Path, upstream: str, items: list[dict], pairs: list[tuple[str, str]], *, git_bin: str) -> list[dict]:
    """A rename's deletion and addition must succeed together, including at replay time."""
    for old, new in dict.fromkeys(pairs):
        rows = [item for item in items if old in item["files"] or new in item["files"]]
        if not rows:
            continue
        files = list(dict.fromkeys(path for row in rows for path in row["files"]))
        patch = "".join(row["patch"] for row in rows if not row["patch_equivalent"])
        complete = old in files and new in files
        equivalent = complete and all(row["patch_equivalent"] for row in rows)
        check = "unknown"
        if complete and all(row["patch"] or row["patch_equivalent"] for row in rows):
            check = "clean" if equivalent else _file_apply_check(repo, patch, files, upstream, git_bin=git_bin)
        merged = {**rows[0], "files": files, "patch": patch,
                  "patch_id": _patch_id(patch, git_bin=git_bin),
                  "patch_equivalent": equivalent, "apply_check": check}
        merged.update(decide(merged))
        first = items.index(rows[0])
        items = [row for row in items if row not in rows]
        items.insert(first, merged)
    return items


def _worktree_items(repo: Path, upstream: str, *, git_bin: str) -> list[dict]:
    items = []
    for xy, path in _status_entries(repo, git_bin=git_bin):
        if _is_backup(path):
            continue
        file_path = repo / path
        # lstat: is_file()/read_bytes() follow symlinks and would copy the target.
        live = (repo, path) if _carryable(file_path) else None
        current = _stored_bytes(file_path)
        mode = _git_mode(file_path)
        if xy == "??":
            items.append(_untracked_file_item(
                repo, upstream, path, identity=f"worktree:{path}", kind="uncommitted",
                current=current, git_bin=git_bin, live=live, mode=mode,
            ))
            continue
        diff = _git(repo, ["diff", "--binary", "HEAD", "--", path], git_bin=git_bin).stdout
        if not diff.strip():
            diff = _git(repo, ["diff", "--binary", "--cached", "--", path], git_bin=git_bin).stdout
        equivalent = _matches_upstream(repo, upstream, path, current, git_bin=git_bin, mode=mode)
        if not diff.strip() and not equivalent:
            items.append(_untracked_file_item(
                repo, upstream, path, identity=f"worktree:{path}", kind="uncommitted",
                current=current, git_bin=git_bin, live=live, mode=mode,
            ))
            continue
        if diff.strip() and _unusable_binary_summary(diff):
            diff = ""
            apply_check = "unknown"
        else:
            apply_check = "clean" if equivalent else _file_apply_check(
                repo, diff, [path], upstream, git_bin=git_bin,
            )
        item = {
            "kind": "uncommitted",
            "identity": f"worktree:{path}",
            "sha": "",
            "title": path,
            "files": [path],
            "patch": diff,
            "patch_id": _patch_id(diff, git_bin=git_bin),
            "patch_equivalent": equivalent,
            "apply_check": apply_check,
        }
        item.update(decide(item))
        items.append(item)
    pairs = _rename_pairs(repo, ["HEAD"], git_bin=git_bin)
    pairs.extend(_rename_pairs(repo, ["--cached"], git_bin=git_bin))
    return _atomic_renames(repo, upstream, items, pairs, git_bin=git_bin)


def _stash_items(repo: Path, stash_ref: str, upstream: str, *, git_bin: str) -> list[dict]:
    names = _git(repo, ["diff", "--no-renames", "--name-only", "-z", f"{stash_ref}^1", stash_ref], git_bin=git_bin)
    items = []
    if names.returncode != 0:
        return _stash_untracked_items(
            repo, stash_ref, upstream, git_bin=git_bin, seen=set(),
        )
    for path in [name for name in names.stdout.split("\0") if name]:
        diff_proc = _git(
            repo, ["diff", "--binary", f"{stash_ref}^1", stash_ref, "--", path], git_bin=git_bin,
        )
        diff = diff_proc.stdout if diff_proc.returncode == 0 else ""
        if diff.strip() and _unusable_binary_summary(diff):
            diff = ""
            apply_check = "unknown"
            equivalent = False
        else:
            shown = subprocess.run(
                [git_bin, "show", f"{stash_ref}:{path}"],
                cwd=repo, capture_output=True, check=False, timeout=120,
            )
            current = shown.stdout if shown.returncode == 0 else None
            mode = _tree_mode(repo, stash_ref, path, git_bin=git_bin)
            equivalent = _matches_upstream(repo, upstream, path, current, git_bin=git_bin, mode=mode)
            if equivalent:
                apply_check = "clean"
            elif diff_proc.returncode == 0 and diff.strip():
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
    items.extend(_stash_untracked_items(
        repo, stash_ref, upstream, git_bin=git_bin,
        seen={row["files"][0] for row in items if row.get("files")},
    ))
    pairs = _rename_pairs(repo, [f"{stash_ref}^1", stash_ref], git_bin=git_bin)
    return _atomic_renames(repo, upstream, items, pairs, git_bin=git_bin)


def _stash_untracked_items(
    repo: Path, stash_ref: str, upstream: str, *, git_bin: str, seen: set[str],
) -> list[dict]:
    """Files saved by ``stash push --include-untracked`` live on ``stash^3``."""
    probe = _git(repo, ["rev-parse", "--verify", "--quiet", f"{stash_ref}^3"], git_bin=git_bin)
    if probe.returncode != 0 or not (probe.stdout or "").strip():
        return []
    listed = _git(repo, ["ls-tree", "-r", "-z", f"{stash_ref}^3"], git_bin=git_bin)
    if listed.returncode != 0:
        return []
    extra = []
    for mode, path in _ls_tree_rows(listed.stdout or ""):
        if path in seen or _is_backup(path):
            continue
        if mode not in _FAITHFUL_BLOB_MODES:
            extra.append(_untracked_file_item(
                repo, upstream, path,
                identity=f"stash:{stash_ref}:untracked:{path}",
                kind="stash", current=None, git_bin=git_bin, mode=mode,
                extra={"stash_ref": stash_ref},
            ))
            continue
        shown = subprocess.run(
            [git_bin, "show", f"{stash_ref}^3:{path}"],
            cwd=repo, capture_output=True, check=False, timeout=120,
        )
        # show returns the blob. For a symlink that is the link text, not the target file.
        current = shown.stdout if shown.returncode == 0 else None
        extra.append(_untracked_file_item(
            repo, upstream, path,
            identity=f"stash:{stash_ref}:untracked:{path}",
            kind="stash",
            current=current,
            git_bin=git_bin,
            mode=mode,
            extra={"stash_ref": stash_ref},
        ))
    return extra


def _dedupe(items: list[dict]) -> list[dict]:
    seen: dict[str, str] = {}
    out = []
    for item in items:
        key = hashlib.sha256(item["patch"].encode("utf-8", errors="surrogateescape")).hexdigest() if item.get("patch") else ""
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
    if "previous cherry-pick is now empty" in text or "--allow-empty" in text:
        _git(dest, ["cherry-pick", "--abort"], git_bin=git_bin)
        return "drop"
    _git(dest, ["cherry-pick", "--abort"], git_bin=git_bin)
    return "conflict"


def _apply_patch(dest: Path, patch: str, *, git_bin: str) -> str:
    if not patch.strip():
        return "drop"
    check = subprocess.run(
        [git_bin, "apply", "--check", "-"], cwd=dest, input=patch.encode("utf-8", errors="surrogateescape"),
        capture_output=True, check=False, timeout=120,
    )
    if check.returncode != 0:
        return "conflict"
    applied = subprocess.run(
        [git_bin, "apply", "-"], cwd=dest, input=patch.encode("utf-8", errors="surrogateescape"),
        capture_output=True, check=False, timeout=120,
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
        repo, ["worktree", "add", "-b", branch, str(dest), plan["upstream_sha"]], git_bin=git_bin,
    )
    if added.returncode != 0:
        raise CarryError("worktree add failed")
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
            _ok(_git(dest, ["add", "-A"], git_bin=git_bin), "git add")
            _ok(_git(dest, [
                "-c", "user.name=carry-reapply", "-c", "user.email=carry-reapply@localhost",
                "commit", "-m", "carry-reapply: replay uncommitted edits",
            ], git_bin=git_bin), "git commit")
            leftover = _git(dest, ["status", "--porcelain"], git_bin=git_bin).stdout
            if leftover.strip():
                raise CarryError("replayed edits are not in the integration head")
    except CarryError:
        raise
    except Exception:  # health: allow BLE001 -- boundary omits credential-bearing exception text
        logger.error("carry-reapply worktree replay failed")
        raise CarryError("worktree replay failed; integration retained for inspection") from None
    after = _ok(_git(repo, ["rev-parse", "HEAD"], git_bin=git_bin), "rev-parse").strip()
    if after != before:
        raise CarryError("source checkout HEAD changed; refusing to continue")
    head = _ok(_git(dest, ["rev-parse", "HEAD"], git_bin=git_bin), "worktree HEAD").strip()
    return {"worktree": str(dest), "branch": branch, "head": head, "results": results}


def fingerprint(plan: dict) -> str:
    rows = [
        (item.get("identity"), item.get("action"), item.get("patch", ""))
        for item in plan.get("items") or []
    ]
    return hashlib.sha256(json.dumps(rows, ensure_ascii=True).encode("utf-8")).hexdigest()


_AUTOSTASH_PREFIX = "hermes-update-autostash-"


def autostash_refs(repo: Path, *, git_bin: str = "git") -> list[str]:
    """Stash refs whose message is an update autostash, not whatever is ``stash@{0}``."""
    proc = _git(repo, ["stash", "list", "--format=%gd%x09%gs"], git_bin=git_bin)
    if proc.returncode != 0:
        return []
    refs = []
    for line in proc.stdout.splitlines():
        ref, _, subject = line.partition("\t")
        if ref.startswith("stash@{") and _AUTOSTASH_PREFIX in subject:
            refs.append(ref)
    return refs


def apply_pr_state(item: dict, pr: dict | None) -> dict:
    """Re-run ``decide`` with a pull-request lookup. Does not touch git."""
    row = {key: value for key, value in item.items() if key not in {"action", "reason", "flags"}}
    lookup = "" if not pr else str(pr.get("lookup") or "")
    number = None if not pr else pr.get("number")
    if pr and lookup == "failed":
        row["pr_lookup"] = "failed"
        row.pop("upstream_pr", None)
        row.pop("pr_match", None)
    elif pr and number and lookup != "none":
        row["pr_lookup"] = "ok"
        row["upstream_pr"] = {
            "state": str(pr.get("state") or ""),
            "number": number,
            "url": pr.get("url") or "",
        }
        row["pr_match"] = str(pr.get("match") or "")
    else:
        row.pop("upstream_pr", None)
        row.pop("pr_match", None)
        row.pop("pr_lookup", None)
    row.update(decide(row))
    return row


def annotate_plan(plan: dict, prs_by_identity: dict[str, dict]) -> dict:
    items = []
    for item in plan.get("items") or []:
        pr = prs_by_identity.get(item.get("identity") or "")
        items.append(apply_pr_state(item, pr) if pr is not None else item)
    out = dict(plan)
    out["items"] = _dedupe(items)
    return out


def _ref_exists(repo: Path, name: str, *, git_bin: str = "git") -> bool:
    return _git(
        repo, ["show-ref", "--verify", "--quiet", f"refs/heads/{name}"], git_bin=git_bin,
    ).returncode == 0


def free_branch(repo: Path, name: str, fingerprint: str, *, git_bin: str = "git") -> str:
    """A branch name that does not already exist. Never deletes the old one."""
    if not _ref_exists(repo, name, git_bin=git_bin):
        return name
    alt = f"{name}-{fingerprint[:12]}"
    if not _ref_exists(repo, alt, git_bin=git_bin):
        return alt
    stem = f"{name}-{fingerprint[:20]}"
    candidate, n = stem, 1
    while _ref_exists(repo, candidate, git_bin=git_bin):
        n += 1
        candidate = f"{stem}-{n}"
    return candidate


def _registered_worktrees(repo: Path | None, *, git_bin: str = "git") -> set[str]:
    """Paths git still has registered as worktrees, including missing ones."""
    if repo is None:
        return set()
    proc = _git(repo, ["worktree", "list", "--porcelain"], git_bin=git_bin)
    if proc.returncode != 0:
        return set()
    found = set()
    for line in (proc.stdout or "").splitlines():
        if line.startswith("worktree "):
            found.add(str(Path(line[len("worktree "):]).resolve()))
    return found


def free_dest(
    requested: Path, fingerprint: str, repo: Path | None = None, *, git_bin: str = "git",
) -> Path:
    """A worktree path that is neither on disk nor registered with git.

    The previous path is left in place. A registered-but-deleted worktree
    counts as taken, because ``git worktree add`` refuses it.
    """
    taken = _registered_worktrees(repo, git_bin=git_bin)

    def used(path: Path) -> bool:
        return path.exists() or str(path.resolve()) in taken

    if not used(requested):
        return requested
    alt = requested.parent / f"{requested.name}-{fingerprint[:12]}"
    if not used(alt):
        return alt
    stem = f"{requested.name}-{fingerprint[:20]}"
    candidate, n = requested.parent / stem, 1
    while used(candidate):
        n += 1
        candidate = requested.parent / f"{stem}-{n}"
    return candidate


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


def _safe_report(value: Any, key: str = "") -> Any:
    """Report opaque refs rather than guessing whether free-form metadata is a secret."""
    enums = {
        "action": {"drop", "conflict", "reapply"},
        "applied": {"drop", "conflict", "reapply"},
        "kind": {"commit", "stash", "uncommitted"},
        "reason": {"merged-upstream", "empty", "conflict-left-out", "unknown-left-out",
                   "still-open", "not-upstream", "duplicate", "merge-commit-left-out", "empty-on-apply"},
        "apply_check": {"clean", "empty", "unknown", "conflict"},
        "mode": set(_MODES),
        "flags": {"no_upstream_pr", "pr_lookup_failed", "pr_closed_unmerged", "apply_check_unknown"},
    }
    if isinstance(value, dict):
        return {k: _safe_report(v, k) for k, v in value.items() if k != "patch"}
    if isinstance(value, list):
        return [_safe_report(v, key) for v in value]
    if not isinstance(value, str) or not value:
        return value
    if value in enums.get(key, ()):
        return value
    if key in {"sha", "head", "upstream_sha", "tip", "patch_id"} and re.fullmatch(r"[0-9a-f]{40,64}", value):
        return value
    return "ref:" + hashlib.sha256(value.encode("utf-8", errors="surrogateescape")).hexdigest()[:16]


def format_report(report: dict, *, quiet: bool) -> str:
    buckets = _safe_report(report.get("buckets") or summarize(report))
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
    if _git(cwd, ["merge-base", upstream_ref, tip], git_bin=git_bin).returncode != 0:
        print("carry-reapply: no common history with the update target; skipped")
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
    except Exception:  # health: allow BLE001 -- boundary omits credential-bearing exception text
        logger.error("carry-reapply after diverged reset failed")
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
    except Exception:  # health: allow BLE001 -- boundary omits credential-bearing exception text
        logger.error("carry-reapply parked stash failed")
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


class _CarryParser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, "carry-reapply: invalid arguments; use --help\n")


def _build_parser() -> argparse.ArgumentParser:
    parser = _CarryParser(prog="hermes_cli.update_carry")
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
    try:
        repo = Path(args.repo).resolve()
        if args.fetch:
            remote, _, branch = args.upstream.partition("/")
            if not branch:
                raise CarryError("upstream must look like remote/branch")
            fetched = _git(repo, ["fetch", remote, branch])
            if fetched.returncode != 0:
                raise CarryError("fetch failed")
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
                branch = free_branch(
                    repo,
                    args.branch or f"carry-reapply/{plan['upstream_sha'][:12]}/{fp[:12]}",
                    fp,
                )
                dest = free_dest(Path(args.worktree), fp, repo)
                applied = apply_in_worktree(repo, plan, dest, branch)
                report["integration"] = applied
                report["buckets"] = summarize(plan, applied["results"])
                if args.state_dir:
                    state_path = Path(args.state_dir)
                    state_path.mkdir(parents=True, exist_ok=True)
                    (state_path / "state.json").write_text(json.dumps({
                        "fingerprint": fp,
                        "upstream_sha": plan["upstream_sha"],
                        "worktree": applied["worktree"],
                        "branch": applied["branch"],
                        "head": applied["head"],
                    }), encoding="utf-8")
        text = format_report({"buckets": report["buckets"]}, quiet=args.quiet)
        if args.json:
            payload = json.dumps(_safe_report(report), indent=2, default=str)
            sys.stdout.write(payload + "\n")
        else:
            sys.stdout.write(text + "\n")
        return 0 if report["verification"]["ok"] else 2
    except Exception:  # health: allow BLE001 -- boundary omits credential-bearing exception text
        # Paths, subprocess arguments and exception messages can contain credentials.
        sys.stderr.write("carry-reapply failed; source refs and integration are retained\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
