"""Recalled memory is framed as background, never as authoritative (KC-B4 / RC3).

Recall is LLM-derived, unattributed and often stale. The old note told the model to "Treat as
authoritative reference data", so agents obeyed stale recall over the KB and live checks. The note
now states the precedence: the KB, owner instructions in this conversation and live checks win, and
infrastructure facts, versions, credentials or rules are verified before acting.

The implementation moved from agent.memory_context to agent.memory_manager on fork main. This test
imports the current module.
"""
from __future__ import annotations

from agent.memory_manager import (
    MEMORY_CONTEXT_NOTE,
    MEMORY_CONTEXT_PRECEDENCE,
    StreamingContextScrubber,
    build_memory_context_block,
    sanitize_context,
)
from agent.turn_context import compose_multimodal_context_part, compose_user_api_content

PRECEDENCE = (
    "The knowledge base (KB), owner instructions in this conversation, and live checks win over it; "
    "verify before acting on infrastructure facts, versions, credentials or rules."
)
LEGACY_NOTE = (
    "[System note: The following is recalled memory context, NOT new user input. Treat as "
    "authoritative reference data — this is the agent's persistent memory and should inform all responses.]"
)


def test_block_renders_background_framing_exactly():
    block = build_memory_context_block("- ES cluster has 5 nodes")
    assert block == (
        "<memory-context>\n"
        "[System note: The following is recalled memory context, NOT new user input. "
        "Treat as background recall: it may be stale or wrong. " + PRECEDENCE + "]\n\n"
        "- ES cluster has 5 nodes\n"
        "</memory-context>"
    )


def test_block_never_says_authoritative():
    block = build_memory_context_block("- fact")
    assert "authoritative" not in block.lower()
    assert "should inform all responses" not in block


def test_block_carries_precedence_sentence_and_not_user_input_marker():
    block = build_memory_context_block("- fact")
    assert MEMORY_CONTEXT_PRECEDENCE == PRECEDENCE
    assert PRECEDENCE in block
    assert "may be stale or wrong" in block
    assert "NOT new user input" in block
    assert MEMORY_CONTEXT_NOTE in block


def test_empty_recall_still_injects_nothing():
    assert build_memory_context_block("") == ""
    assert build_memory_context_block("   \n") == ""


def test_sanitize_strips_new_and_legacy_notes():
    for note in (MEMORY_CONTEXT_NOTE, LEGACY_NOTE):
        assert sanitize_context(f"before {note}\nafter") == "before after"


def test_streaming_scrubber_hides_new_block():
    scrubber = StreamingContextScrubber()
    block = build_memory_context_block("- secret recall")
    out = scrubber.feed("hi\n" + block[:40]) + scrubber.feed(block[40:] + "\nbye") + scrubber.flush()
    assert "secret recall" not in out
    assert "background recall" not in out


def test_turn_composition_uses_new_note_and_plugin_context_is_unchanged():
    api = compose_user_api_content("question", "- recalled fact", "")
    assert api is not None
    assert api.startswith("question\n\n<memory-context>\n" + MEMORY_CONTEXT_NOTE)
    assert "authoritative" not in api.lower()
    # A non-memory-provider caller (pre_llm_call plugin context only) gets no memory note at all.
    assert compose_multimodal_context_part("", "plugin ctx") == "plugin ctx"
    assert compose_user_api_content("question", "", "plugin ctx") == "question\n\nplugin ctx"
    assert compose_user_api_content("question", "", "") is None
