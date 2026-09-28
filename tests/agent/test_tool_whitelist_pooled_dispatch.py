"""The thread tool whitelist is enforced on the pooled tool workers that actually dispatch.

Background-review and /btw forks install a whitelist on their own thread, but non-inline tools
run on a DaemonThreadPoolExecutor worker started through
``tools.thread_context.propagate_context_to_thread``. A whitelist that did not follow the call
onto that worker let a review fork run ``terminal`` / ``kanban_comment`` (#15204 contract).
"""

import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.tool_executor import execute_tool_calls_concurrent, execute_tool_calls_sequential
from hermes_cli.plugins import (
    clear_thread_tool_whitelist,
    get_pre_tool_call_block_message,
    set_thread_tool_whitelist,
)
from run_agent import AIAgent
from tools.thread_context import propagate_context_to_thread


def _make_agent(tmp_path: Path) -> AIAgent:
    tool_defs = [
        {"type": "function", "function": {"name": name, "description": "t",
                                          "parameters": {"type": "object", "properties": {}}}}
        for name in ("web_extract", "skill_view")
    ]
    with (
        patch("model_tools.get_tool_definitions", return_value=tool_defs),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
        patch("run_agent._hermes_home", tmp_path),
        patch("agent.model_metadata.fetch_model_metadata", return_value={}),
    ):
        agent = AIAgent(api_key="test-key", base_url="https://openrouter.ai/api/v1",
                        quiet_mode=True, skip_context_files=True, skip_memory=True)
    agent._flush_messages_to_session_db = MagicMock(return_value=True)
    agent._append_guardrail_observation = MagicMock(side_effect=lambda _n, _a, result, **_k: result)
    agent._record_file_mutation_result = MagicMock()
    agent._subdirectory_hints.check_tool_call = MagicMock(return_value="")
    agent._tool_result_content_for_active_model = MagicMock(side_effect=lambda _n, result: result)
    return agent


def _call(call_id: str, name: str):
    return SimpleNamespace(id=call_id, type="function",
                           function=SimpleNamespace(name=name, arguments="{}"))


def _run_under_whitelist(tmp_path, executor_fn):
    """Dispatch one allowed and one denied call from a fresh thread that installed the whitelist,
    as the review fork does; return (dispatched tool names, tool-result messages)."""
    agent = _make_agent(tmp_path)
    dispatched: list[str] = []
    messages: list[dict] = []

    def _dispatch(name, _args, *_a, **_kw):
        dispatched.append(name)
        return "ran " + name

    def _fork():
        set_thread_tool_whitelist({"skill_view"}, deny_msg_fmt="review denied {tool_name}")
        try:
            assistant = SimpleNamespace(tool_calls=[_call("ok", "skill_view"), _call("bad", "web_extract")])
            executor_fn(agent, assistant, messages, "task")
        finally:
            clear_thread_tool_whitelist()

    with patch("model_tools.handle_function_call", side_effect=_dispatch):
        t = threading.Thread(target=_fork)
        t.start()
        t.join(timeout=30)
    assert not t.is_alive()
    return dispatched, messages


def test_sequential_pooled_dispatch_honours_whitelist(tmp_path):
    dispatched, messages = _run_under_whitelist(tmp_path, execute_tool_calls_sequential)
    assert dispatched == ["skill_view"]
    by_id = {m["tool_call_id"]: m["content"] for m in messages}
    assert "review denied web_extract" in by_id["bad"]
    assert by_id["ok"] == "ran skill_view"


def test_concurrent_pooled_dispatch_honours_whitelist(tmp_path):
    dispatched, messages = _run_under_whitelist(tmp_path, execute_tool_calls_concurrent)
    assert dispatched == ["skill_view"]
    by_id = {m["tool_call_id"]: m["content"] for m in messages}
    assert "review denied web_extract" in by_id["bad"]


def test_whitelist_follows_propagated_context_but_not_bare_threads(monkeypatch):
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda *_a, **_k: [])
    seen: dict[str, object] = {}

    def _probe(key):
        seen[key] = get_pre_tool_call_block_message("terminal", {})

    set_thread_tool_whitelist({"skill_view"})
    try:
        propagated = threading.Thread(target=propagate_context_to_thread(lambda: _probe("propagated")))
        bare = threading.Thread(target=lambda: _probe("bare"))
        for t in (propagated, bare):
            t.start()
            t.join()
    finally:
        clear_thread_tool_whitelist()

    assert seen["propagated"] is not None
    assert seen["bare"] is None
