"""Tests for ``AgentRunner`` history accumulation and ``_cap_history``.

The agent's create path pulls in a real ``deepagents`` graph, so these
tests build an ``AgentRunner`` via ``__new__`` and inject a fake
``_agent`` that replays a canned stream. This keeps the unit under test
(message collection + history extension + noise filtering + de-dup)
isolated from any LLM dependency.
"""
from __future__ import annotations

from typing import Iterable

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent_server.__main__ import _cap_history
from agent_server.session import AgentRunner, _last_ai_text


class _FakeAgent:
    def __init__(self, chunks: Iterable[dict]) -> None:
        self._chunks = list(chunks)

    def stream(self, inputs, config, stream_mode):  # noqa: ARG002 — signature match
        yield from self._chunks


def _make_runner(chunks: Iterable[dict]) -> AgentRunner:
    runner = AgentRunner.__new__(AgentRunner)
    runner._emit = lambda *_a, **_k: None
    runner._recursion_limit = 50
    runner._agent = _FakeAgent(chunks)
    return runner


def test_run_returns_accumulated_messages_with_tool_calls() -> None:
    ai_call = AIMessage(
        content="",
        tool_calls=[{"name": "run_pymol_python", "args": {"code": "cmd.fetch('3IOL')"}, "id": "c1", "type": "tool_call"}],
        id="ai-1",
    )
    tool_msg = ToolMessage(content="[OK, no stdout]", tool_call_id="c1", name="run_pymol_python", id="t1")
    ai_final = AIMessage(content="3IOL loaded.", id="ai-2")

    chunks = [
        {"agent": {"messages": [ai_call]}},
        {"tools": {"messages": [tool_msg]}},
        {"agent": {"messages": [ai_final]}},
    ]
    runner = _make_runner(chunks)

    user_msg = {"role": "user", "content": "fetch GLP-1"}
    final_text, new_history = runner.run([user_msg], thread_id="t")

    assert final_text == "3IOL loaded."
    assert new_history == [user_msg, ai_call, tool_msg, ai_final]


def test_run_skips_middleware_noise_nodes() -> None:
    echo = HumanMessage(content="echoed input", id="h-echo")
    ai_final = AIMessage(content="done", id="ai-1")

    chunks = [
        {"PatchToolCallsMiddleware.before_agent": {"messages": [echo]}},
        {"agent": {"messages": [ai_final]}},
    ]
    runner = _make_runner(chunks)

    final_text, new_history = runner.run([{"role": "user", "content": "hi"}], thread_id="t")

    assert final_text == "done"
    assert echo not in new_history
    assert ai_final in new_history


def test_run_dedupes_messages_by_id() -> None:
    ai = AIMessage(content="first", id="ai-dup")
    chunks = [
        {"agent": {"messages": [ai]}},
        {"agent": {"messages": [ai]}},  # same id re-emitted
    ]
    runner = _make_runner(chunks)

    _, new_history = runner.run([{"role": "user", "content": "hi"}], thread_id="t")

    ai_occurrences = [m for m in new_history if getattr(m, "id", None) == "ai-dup"]
    assert len(ai_occurrences) == 1


def test_last_ai_text_finds_trailing_assistant() -> None:
    msgs = [
        HumanMessage(content="q"),
        AIMessage(content="", tool_calls=[{"name": "x", "args": {}, "id": "c", "type": "tool_call"}]),
        ToolMessage(content="ok", tool_call_id="c", name="x"),
        AIMessage(content="final answer"),
    ]
    assert _last_ai_text(msgs) == "final answer"


def test_last_ai_text_skips_empty_ai_messages() -> None:
    msgs = [
        AIMessage(content="real"),
        AIMessage(content=""),  # trailing empty should be skipped
    ]
    assert _last_ai_text(msgs) == "real"


def test_cap_history_keeps_last_n_turns() -> None:
    # Build 12 turns of plain user+assistant dicts.
    history: list = []
    for i in range(12):
        history.append({"role": "user", "content": f"q{i}"})
        history.append({"role": "assistant", "content": f"a{i}"})

    capped = _cap_history(history, 10)

    assert capped[0] == {"role": "user", "content": "q2"}
    assert capped[-1] == {"role": "assistant", "content": "a11"}
    # 10 user + 10 assistant
    assert len(capped) == 20


def test_cap_history_preserves_tool_pairs_within_window() -> None:
    # Turn shape: user → ai(tool_call) → tool → ai(final)
    def turn(i: int) -> list:
        return [
            HumanMessage(content=f"u{i}", id=f"u{i}"),
            AIMessage(
                content="",
                tool_calls=[{"name": "run_pymol_python", "args": {"code": f"s{i}"}, "id": f"c{i}", "type": "tool_call"}],
                id=f"a{i}",
            ),
            ToolMessage(content="ok", tool_call_id=f"c{i}", name="run_pymol_python", id=f"t{i}"),
            AIMessage(content=f"done {i}", id=f"af{i}"),
        ]

    history: list = []
    for i in range(3):
        history.extend(turn(i))

    capped = _cap_history(history, 2)

    # Cap should start exactly at the second-to-last human message — no orphan tool results.
    assert capped[0].id == "u1"
    # Each surviving tool_call_id has its paired human+tool msgs.
    kept_tool_ids = {m.tool_call_id for m in capped if isinstance(m, ToolMessage)}
    kept_ai_call_ids = {
        tc["id"]
        for m in capped
        if isinstance(m, AIMessage)
        for tc in (m.tool_calls or [])
    }
    assert kept_tool_ids == kept_ai_call_ids


def test_cap_history_noop_below_threshold() -> None:
    history = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
    assert _cap_history(history, 10) == history


def test_cap_history_zero_means_no_cap() -> None:
    history = [{"role": "user", "content": f"q{i}"} for i in range(5)]
    assert _cap_history(history, 0) == history
