"""Tests for the RPC proxy tool bridge."""
from __future__ import annotations

import threading

import pytest

from agent_server.remote_tool import RemoteToolBridge


def test_tool_blocks_until_result_delivered() -> None:
    calls: list[tuple[str, str, dict]] = []

    def sender(call_id: str, name: str, args: dict) -> None:
        calls.append((call_id, name, args))

    bridge = RemoteToolBridge(send_tool_call=sender, timeout=5.0)
    tool = bridge.build_tool()

    captured: dict[str, str] = {}

    def invoke() -> None:
        captured["result"] = tool.invoke({"code": "print(1)"})

    worker = threading.Thread(target=invoke)
    worker.start()

    # Wait for the send to register, then deliver a fake result.
    for _ in range(50):
        if calls:
            break
        threading.Event().wait(0.02)
    assert calls, "tool should have sent a tool_call synchronously"

    call_id = calls[0][0]
    bridge.deliver_result(call_id, ok=True, result="OK")
    worker.join(timeout=2.0)
    assert not worker.is_alive()
    assert captured["result"] == "OK"


def test_tool_reports_timeout() -> None:
    bridge = RemoteToolBridge(send_tool_call=lambda *a: None, timeout=0.1)
    tool = bridge.build_tool()
    result = tool.invoke({"code": "print(1)"})
    assert "TIMEOUT" in result


def test_cancel_unblocks_pending_calls() -> None:
    bridge = RemoteToolBridge(send_tool_call=lambda *a: None, timeout=5.0)
    tool = bridge.build_tool()

    captured: dict[str, str] = {}

    def invoke() -> None:
        captured["result"] = tool.invoke({"code": "print(1)"})

    worker = threading.Thread(target=invoke)
    worker.start()
    # Give the worker a tick to park on the event.
    threading.Event().wait(0.05)
    bridge.cancel()
    worker.join(timeout=2.0)
    assert "CANCELLED" in captured["result"]


def test_failed_ok_surfaces_result_text() -> None:
    sent: list[str] = []

    def sender(call_id: str, name: str, args: dict) -> None:
        sent.append(call_id)

    bridge = RemoteToolBridge(send_tool_call=sender, timeout=5.0)
    tool = bridge.build_tool()

    captured: dict[str, str] = {}

    def invoke() -> None:
        captured["result"] = tool.invoke({"code": "x"})

    worker = threading.Thread(target=invoke)
    worker.start()
    for _ in range(50):
        if sent:
            break
        threading.Event().wait(0.02)
    bridge.deliver_result(sent[0], ok=False, result="[BLOCKED] nope")
    worker.join(timeout=2.0)
    assert captured["result"] == "[BLOCKED] nope"


def test_sender_exception_returns_error_string() -> None:
    def sender(call_id: str, name: str, args: dict) -> None:
        raise RuntimeError("pipe broken")

    bridge = RemoteToolBridge(send_tool_call=sender, timeout=5.0)
    tool = bridge.build_tool()
    result = tool.invoke({"code": "x"})
    assert "TOOL-BRIDGE-ERROR" in result
