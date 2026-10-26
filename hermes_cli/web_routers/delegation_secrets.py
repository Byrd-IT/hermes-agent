"""Nested route secrets for the delegation routes.

A model route can carry credentials below its top-level ``api_key``: each ``fallback_providers[]`` entry has
its own ``api_key`` (and possibly nested ``fallbacks``), and ``request_overrides`` holds headers / body fields
(``extra_headers.Authorization``, ``X-Api-Key``...). ``mask_nested`` turns every such string into a display-only
preview for GET; ``restore_nested`` is the inverse for PUT: a preview sent back unchanged is swapped for the
stored value at the same place, and with no stored counterpart it is dropped, so a preview is never persisted.
"""

from __future__ import annotations

from typing import Any

from hermes_cli.web_routers._common import is_redacted_credential_preview, redacted_credential_preview

_SECRET_NAME_PARTS = ("key", "token", "secret", "password", "passwd", "authorization", "credential", "cookie", "bearer", "jwt")
_DROP = object()


def _secret_name(name: Any) -> bool:
    lowered = str(name).lower()
    if lowered.endswith("_env"):  # names an environment variable, not a credential
        return False
    return any(part in lowered for part in _SECRET_NAME_PARTS)


def mask_nested(value: Any, secret: bool = False) -> Any:
    """Copy of ``value`` with every non-empty string under a secret-named key replaced by its preview.

    Only strings are masked: ``max_tokens: 100`` is a number, not a credential.
    """
    if isinstance(value, dict):
        return {k: mask_nested(v, secret or _secret_name(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_nested(v, secret) for v in value]
    if secret and isinstance(value, str) and value:
        return redacted_credential_preview(value)
    return value


def _identity(item: Any) -> tuple | None:
    if isinstance(item, dict) and (item.get("provider") or item.get("model")):
        return (item.get("provider") or None, item.get("model") or None, item.get("base_url") or None)
    return None


def _same_endpoint(item: dict, other: Any) -> bool:
    return (
        isinstance(other, dict)
        and _identity(other) is not None
        and (item.get("provider") or None) == (other.get("provider") or None)
        and (item.get("base_url") or None) == (other.get("base_url") or None)
    )


def _counterparts(items: list, stored: Any) -> list:
    """The stored element each submitted element corresponds to (None when there is none).

    Three passes, strongest evidence first, so a weaker match never takes an element a later item matches
    exactly: (1) same provider/model/base_url (reordering or deleting an entry never hands one entry's key to
    another); (2) same index with the same provider and base_url (only the model was edited); (3) same index
    for elements that name no provider/model at all (plain lists inside ``request_overrides``).
    """
    old = stored if isinstance(stored, list) else []
    picks: list[int | None] = [None] * len(items)
    used: set[int] = set()

    def take(i: int, j: int) -> None:
        picks[i] = j
        used.add(j)

    for i, item in enumerate(items):
        ident = _identity(item)
        if ident is None:
            continue
        same = [j for j, o in enumerate(old) if j not in used and _identity(o) == ident]
        if same:
            take(i, i if i in same else same[0])
    for i, item in enumerate(items):
        if picks[i] is not None or i >= len(old) or i in used:
            continue
        if _identity(item) is None:
            if _identity(old[i]) is None:
                take(i, i)
        elif _same_endpoint(item, old[i]):
            take(i, i)
    return [old[j] if j is not None else None for j in picks]


def _emptied(submitted: Any, restored: Any) -> bool:
    """True when dropping previews left a container empty that was not empty as submitted."""
    return isinstance(restored, (dict, list)) and not restored and bool(submitted)


def restore_nested(submitted: Any, stored: Any, secret: bool = False) -> Any:
    """``submitted`` with previews under secret-named keys replaced by the stored value at the same place.

    A preview with no usable stored counterpart is dropped (``_DROP`` inside the structure is removed), and a
    value the user typed is kept as sent.
    """
    if isinstance(submitted, dict):
        old = stored if isinstance(stored, dict) else {}
        out = {}
        for k, v in submitted.items():
            r = restore_nested(v, old.get(k), secret or _secret_name(k))
            if r is not _DROP and not _emptied(v, r):
                out[k] = r
        return out
    if isinstance(submitted, list):
        out = []
        for v, counterpart in zip(submitted, _counterparts(submitted, stored)):
            r = restore_nested(v, counterpart, secret)
            if r is not _DROP:
                out.append(r)
        return out
    if secret and isinstance(submitted, str) and is_redacted_credential_preview(submitted):
        if isinstance(stored, str) and stored and not is_redacted_credential_preview(stored):
            return stored
        return _DROP
    return submitted
