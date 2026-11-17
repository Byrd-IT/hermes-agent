"""Duplicate tool-failure reports must cite research (Byrd-IT carry; was in kanban_db.py)."""
from __future__ import annotations

import re

_TOOL_FAILURE_PREFIXES = ("tool failure:", "tool/service failure", "service failure:")
_RETITLED_LOCAL_TOOL_BATCHING_ERROR = "takes exactly one entry for local tools"


def reject_unresearched_tool_failure_card(title, body) -> None:
    """Fail closed on duplicate tool-failure reports that cite no research.

    Prefix-only matching let known local ``tool_call`` batching behavior be
    re-filed as an apparently unrelated fix.  This chokepoint covers every
    card-creating surface, while preserving a path for researched new reports.
    """
    t = (title or "").strip().lower()
    b = (body or "")
    is_prefixed_failure = any(t.startswith(prefix) for prefix in _TOOL_FAILURE_PREFIXES)
    is_retitled_local_batching_failure = (
        "tool_call" in t
        and "batch" in t
        and any(word in t for word in ("reject", "refus", "validation"))
    ) or _RETITLED_LOCAL_TOOL_BATCHING_ERROR in b.lower()
    if not (is_prefixed_failure or is_retitled_local_batching_failure):
        return
    researched = bool(
        # An id label plus a colon takes any slug; otherwise the token must carry a digit (prose is not an id).
        re.search(r"\bkb[ _-]?(?:(?:id|entry|ref)\s*[:=]\s*[A-Za-z0-9_-]{8,}|"
                  r"(?:id|entry|ref)?\s*[:=]?\s*(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{8,})", b, re.IGNORECASE)
        or re.search(r"\b(?:search(?:ed)?(?:_kb)?|checked|looked)\b[^.\n]{0,100}"
                     r"\bkb\b[^.\n]{0,100}"
                     r"\b(?:no (?:hit|match|result|entry)|empty|nothing|none)\b", b, re.IGNORECASE)
        or re.search(r"\bkb\b[^.\n]{0,100}\b(?:no (?:hit|match|result|entry)s?|"
                     r"came back empty|returned nothing)\b", b, re.IGNORECASE)
        or re.search(r"\b(?:search_kb|kb[ _-]?search)\b[^.\n]{0,120}"
                     r"\b(?:no (?:hit|match|result|entry)s?|empty|nothing|none)\b", b, re.IGNORECASE)
        or re.search(r"\bno (?:existing )?kb (?:entry|hit|match)\b", b, re.IGNORECASE)
        # Byrd-IT_Memory replaced the KB: a memory id, or a memory_search that found nothing.
        or re.search(r"\bmemory[ _-]?(?:id|ref)\s*(?:[:=]\s*[A-Za-z0-9_-]{8,}|"
                     r"(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{8,})", b, re.IGNORECASE)
        or re.search(r"\bmemory_search\b[^.\n]{0,120}\b(?:no (?:hit|match|result|entry)s?|empty|nothing|none)\b",
                     b, re.IGNORECASE)
    )
    if researched:
        return
    raise ValueError(
        "Refusing to create a tool/service-failure card with no research cited.\n"
        "264 of these were filed in 30 days, almost all duplicates of known, intentional "
        "behavior (e.g. tool_call batches only connectors__ names; Tirith fails closed when "
        "package threat-intel cannot complete).\n"
        "Do this instead:\n"
        "  1. memory_search (or search_kb) for the exact error string / tool name / rule id.\n"
        "  2. KB HIT -> do not file. Note the KB id on your OWN card and continue using the "
        "documented workaround.\n"
        "  3. KB EMPTY and you reproduced it live -> re-file including either the KB/memory id you "
        "checked, or the sentence 'searched the KB for <term>: no hits' (or 'memory_search for <term>: "
        "no hits').\n"
        "See skill kanban-worker and KB entries tagged stop-filing-cards."
    )
