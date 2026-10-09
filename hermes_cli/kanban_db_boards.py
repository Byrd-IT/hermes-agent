"""Board metadata (``board.json``): read/write, board creation, and the
board's persistent scratch ``workspaces_root`` with its safety check.

Split out of ``hermes_cli.kanban_db``; origin-resident helpers are reached
late-bound via ``_kb`` (import-cycle breaking) so monkeypatching
``kanban_db.<name>`` keeps working.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional


def board_metadata_path(board: Optional[str] = None) -> Path:
    """``board.json`` path — display metadata only; the directory slug is the identity."""
    return _kb.board_dir(_kb._slug_or_default(board)) / "board.json"


def _default_board_display_name(slug: str) -> str:
    """``atm10-server`` -> ``Atm10 Server``."""
    return " ".join(part.capitalize() for part in slug.replace("_", "-").split("-") if part) or slug


def read_board_metadata(board: Optional[str] = None) -> dict:
    """``board.json`` merged over defaults, plus ``slug`` and ``db_path``. Never
    raises — a missing/malformed file yields the synthesized entry."""
    slug = _kb._slug_or_default(board)
    meta: dict[str, Any] = {
        "slug": slug,
        "name": _default_board_display_name(slug),
        "description": "",
        "icon": "",
        "color": "",
        "default_workdir": None,
        # Project scope: new tasks inherit it (deterministic worktree + branch).
        "project_id": None,
        "created_at": None,
        "archived": False,
    }
    try:
        p = board_metadata_path(slug)
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                # Never let the metadata file claim a different slug than
                # its directory — trust the filesystem.
                raw["slug"] = slug
                meta.update(raw)
    except (OSError, json.JSONDecodeError):
        pass
    meta["db_path"] = str(_kb.kanban_db_path(slug))
    return meta


def write_board_metadata(
    board: Optional[str], *, name: Optional[str] = None, description: Optional[str] = None,
    icon: Optional[str] = None, color: Optional[str] = None, archived: Optional[bool] = None,
    default_workdir: Optional[str] = None, project_id: Optional[str] = None,
    workspaces_root: Optional[str] = None,
) -> dict:
    """Create/update ``board.json``; unmentioned fields are preserved, ``created_at``
    set on first write. ``project_id``/``default_workdir``/``workspaces_root``:
    ``None`` = unchanged, "" = clear (``project_id`` is not validated here; a
    ``workspaces_root`` failing :func:`workspaces_root_rejection` raises
    ``ValueError``)."""
    _kb._assert_not_delegated_child_mutation()
    slug = _kb._slug_or_default(board)
    if workspaces_root:
        reason = workspaces_root_rejection(workspaces_root)
        if reason:
            raise ValueError(reason)
        workspaces_root = str(Path(workspaces_root).expanduser())
    meta = read_board_metadata(slug)
    # db_path is derived on every read; never persist it into board.json.
    meta.pop("db_path", None)
    if name is not None:
        meta["name"] = str(name).strip() or _default_board_display_name(slug)
    for key, value in (("description", description), ("icon", icon), ("color", color)):
        if value is not None:
            meta[key] = str(value)
    if archived is not None:
        meta["archived"] = bool(archived)
    for key, value in (
        ("default_workdir", default_workdir), ("project_id", project_id),
        ("workspaces_root", workspaces_root),
    ):
        if value is not None:
            meta[key] = str(value) if value else None
    if not meta.get("created_at"):
        meta["created_at"] = int(time.time())
    path = board_metadata_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8",
    )
    meta["db_path"] = str(_kb.kanban_db_path(slug))
    return meta


def create_board(
    slug: str, *, name: Optional[str] = None, description: Optional[str] = None,
    icon: Optional[str] = None, color: Optional[str] = None, default_workdir: Optional[str] = None,
    project_id: Optional[str] = None,
) -> dict:
    """Create board dir + DB + metadata (``mkdir -p`` semantics: existing board returns its metadata)."""
    normed = _kb._require_slug(slug)
    meta = write_board_metadata(
        normed, name=name, description=description, icon=icon, color=color,
        default_workdir=default_workdir, project_id=project_id,
    )
    # Touch the DB so list_boards() sees it immediately.
    _kb.init_db(board=normed)
    return meta


def workspaces_root_rejection(root: Path | str) -> Optional[str]:
    """Why *root* cannot be a board's configured scratch root, else ``None``.

    Every strict descendant of a scratch root is ``rmtree``-able on task
    completion, so the root must be absolute, must not be (or contain) the
    filesystem anchor or the user's home, and must stay out of the kanban home
    (its DBs, board metadata, logs and profiles live there; the built-in roots
    under it need no setting)."""
    p = Path(str(root)).expanduser()
    if not p.is_absolute():
        return f"workspaces root must be an absolute path, got {str(root)!r}"
    try:
        resolved = p.resolve(strict=False)
        home_real = Path.home().resolve(strict=False)
        kanban_real = _kb.kanban_home().resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        return f"cannot resolve workspaces root {str(root)!r}: {exc}"
    for guarded in (Path(resolved.anchor), home_real, kanban_real):
        if guarded.is_relative_to(resolved):
            return f"workspaces root {str(root)!r} would contain {str(guarded)!r}"
    if resolved.is_relative_to(kanban_real):
        return (
            f"workspaces root {str(root)!r} is inside the kanban home {str(kanban_real)!r}; "
            "leave it unset to use the built-in root there"
        )
    return None


def board_configured_workspaces_root(meta: dict) -> Optional[Path]:
    """The ``workspaces_root`` a board's metadata dict pins, or ``None`` when
    unset or unsafe (:func:`workspaces_root_rejection`). Hand-edited
    ``board.json`` values go through the same check as CLI-written ones."""
    raw = meta.get("workspaces_root") if isinstance(meta, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        return None
    reason = workspaces_root_rejection(raw.strip())
    if reason:
        _kb._log.warning("Ignoring board %r workspaces_root: %s", meta.get("slug"), reason)
        return None
    return Path(raw.strip()).expanduser()


from hermes_cli import kanban_db as _kb  # noqa: E402
