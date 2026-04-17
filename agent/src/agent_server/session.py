"""Runs one deep-agent request and streams structured events.

Unlike the in-process version this module knows nothing about PyMOL —
``run_pymol_python`` is wired in from the outside as a LangChain tool
bound to a :class:`RemoteToolBridge`. Events are published through an
``emit`` callback as plain ``dict`` payloads so the server can forward
them as ndjson messages.
"""
from __future__ import annotations

import logging
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from deepagents import create_deep_agent
from langchain_google_genai import ChatGoogleGenerativeAI

from .subagents import python_executor_spec

log = logging.getLogger(__name__)

_VERBOSE_TOOLS = frozenset({"describe_viewport", "capture_viewport"})

_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
_MAX_EVENT_LEN = 600


EventEmitter = Callable[[str, dict[str, Any]], None]
"""``emit(kind, fields) -> None`` — kind is one of protocol.EVENT_*."""


def _text_of(msg: Any) -> str:
    if isinstance(msg, dict):
        content = msg.get("content")
    else:
        content = getattr(msg, "content", None)
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for p in content:
            if isinstance(p, dict):
                t = p.get("text") or p.get("content") or ""
                if t:
                    parts.append(str(t))
            else:
                parts.append(str(p))
        return "\n".join(parts)
    return str(content)


def _tool_calls_of(msg: Any) -> list[dict]:
    if isinstance(msg, dict):
        return list(msg.get("tool_calls") or [])
    calls = getattr(msg, "tool_calls", None) or []
    out: list[dict] = []
    for c in calls:
        if isinstance(c, dict):
            out.append(c)
        else:
            out.append({"name": getattr(c, "name", "?"), "args": getattr(c, "args", {})})
    return out


def _is_noise_node(name: Any) -> bool:
    """Suppress middleware echo nodes that just replay the input prompt.

    deepagents wraps the agent in middleware nodes such as
    ``PatchToolCallsMiddleware.before_agent`` that re-emit the user's
    message before each step. The user already typed it; we don't need
    to print it back. Heuristic: any node name with ``Middleware`` in it.
    """
    if not isinstance(name, str):
        return False
    return "Middleware" in name


def _unwrap_messages(value: Any) -> list:
    """Coerce a state-channel value into a plain list of messages.

    A node may return ``langgraph.types.Overwrite(value=[...])`` to bypass
    the reducer; that wrapper isn't iterable on its own.
    """
    if value is None:
        return []
    if hasattr(value, "value") and not isinstance(value, (list, tuple, dict, str)):
        value = value.value
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, dict):
        return [value]
    try:
        return list(value)
    except TypeError:
        return []


def _short(text: str, limit: int = _MAX_EVENT_LEN) -> str:
    text = text.strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f" …(+{len(text) - limit} chars)"


def _short_smart(text: str, head: int = 240, tail: int = 480) -> str:
    """Truncate keeping both ends — important for tracebacks where the last
    line carries the real exception message."""
    text = text.strip()
    if not text:
        return ""
    if len(text) <= head + tail + 32:
        return text
    middle = len(text) - head - tail
    return f"{text[:head]} …(+{middle} chars)…\n{text[-tail:]}"


def _last_ai_text(messages: list) -> str:
    """Return the last non-empty text content from an AIMessage in ``messages``."""
    for m in reversed(messages):
        role = m.get("type") if isinstance(m, dict) else getattr(m, "type", None)
        if role == "ai":
            t = _text_of(m)
            if t:
                return t
    return ""


class AgentRunner:
    """One request. Builds a fresh deep agent graph, streams, emits events."""

    def __init__(
        self,
        model_name: str,
        tools: list[Any],
        emit: EventEmitter,
        recursion_limit: int = 50,
    ) -> None:
        self._emit = emit
        self._recursion_limit = recursion_limit

        llm = ChatGoogleGenerativeAI(model=model_name, temperature=0.2)
        system_prompt = (_PROMPT_DIR / "main.md").read_text(encoding="utf-8")

        self._agent = create_deep_agent(
            model=llm,
            tools=tools,
            system_prompt=system_prompt,
            subagents=[python_executor_spec(tools)],
        )

    def run(self, messages: list, thread_id: str) -> tuple[str, list]:
        """Execute one turn. Returns ``(final_text, updated_messages)``.

        ``messages`` is the conversation history — either LangChain
        ``BaseMessage`` objects or OpenAI-shaped dicts
        (``{"role": "...", "content": "..."}``). The returned list extends
        the input with every non-noise message emitted during this turn
        (AIMessage with tool_calls, ToolMessage, final AIMessage), so that
        subsequent turns see the full tool exchange instead of a bare text
        summary.
        """
        config = {
            "configurable": {"thread_id": thread_id},
            "recursion_limit": self._recursion_limit,
        }
        inputs = {"messages": list(messages)}

        accumulated: list = []
        seen_ids: set[str] = set()

        try:
            for chunk in self._agent.stream(inputs, config=config, stream_mode="updates"):
                self._handle_chunk(chunk)
                self._collect_messages(chunk, accumulated, seen_ids)
        except Exception as exc:
            self._emit("info", {"text": f"ERROR: {exc}"})
            raise RuntimeError(f"{exc}\n{traceback.format_exc()}") from exc

        final_text = _last_ai_text(accumulated)
        new_history = list(messages) + accumulated
        return final_text, new_history

    # ---- internals ---------------------------------------------------------

    def _handle_chunk(self, chunk: dict) -> None:
        if not isinstance(chunk, dict):
            return
        for node_name, update in chunk.items():
            if _is_noise_node(node_name):
                continue
            if not isinstance(update, dict):
                continue
            for m in _unwrap_messages(update.get("messages")):
                self._render_message(node_name, m)

    def _collect_messages(
        self, chunk: dict, out: list, seen_ids: set[str]
    ) -> None:
        if not isinstance(chunk, dict):
            return
        for node_name, update in chunk.items():
            if _is_noise_node(node_name):
                continue
            if not isinstance(update, dict):
                continue
            for m in _unwrap_messages(update.get("messages")):
                mid = m.get("id") if isinstance(m, dict) else getattr(m, "id", None)
                if mid:
                    if mid in seen_ids:
                        continue
                    seen_ids.add(mid)
                out.append(m)

    def _render_message(self, node: str, msg: Any) -> None:
        role = msg.get("type") if isinstance(msg, dict) else getattr(msg, "type", None)
        tool_calls = _tool_calls_of(msg)
        text = _text_of(msg)

        if role == "tool":
            name = msg.get("name") if isinstance(msg, dict) else getattr(msg, "name", "tool")
            if text:
                if name in _VERBOSE_TOOLS:
                    log.debug("tool_output [%s]: %s", name, _short(text, 200))
                    self._emit(
                        "tool_output",
                        {"node": node, "name": name, "text": f"({len(text)} chars received)"},
                    )
                else:
                    self._emit(
                        "tool_output",
                        {"node": node, "name": name, "text": _short_smart(text)},
                    )
            return

        if tool_calls:
            for tc in tool_calls:
                name = tc.get("name", "?")
                args = tc.get("args") or {}
                preview = ""
                if isinstance(args, dict):
                    code = args.get("code")
                    if isinstance(code, str):
                        preview = _short(code, 500)
                    else:
                        preview = _short(str(args), 200)
                self._emit(
                    "tool_call_preview",
                    {"node": node, "name": name, "preview": preview},
                )

        if text:
            self._emit("message", {"node": node, "text": _short(text)})

