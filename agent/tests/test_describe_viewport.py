"""Tests for the agent-side ``describe_viewport`` local tool.

Stubs the remote bridge call and the Gemini vision LLM to verify the
tool chains screenshot capture → file read → vision call correctly.
"""
from __future__ import annotations

import base64
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent_server.remote_tool import RemoteToolBridge


def _build_bridge_with_fake_capture(tmp_path: Path):
    """Return (bridge, tool_dict) where capture_viewport writes a real file."""
    png_path = tmp_path / "pymol_viewport.png"
    png_path.write_bytes(b"\x89PNG_FAKE_IMAGE_DATA")

    calls: list[tuple[str, str, dict]] = []

    def sender(call_id: str, name: str, args: dict) -> None:
        calls.append((call_id, name, args))

    bridge = RemoteToolBridge(send_tool_call=sender, timeout=5.0)
    tools = bridge.build_tools()
    tool_dict = {t.name: t for t in tools}

    def auto_reply():
        for _ in range(100):
            if calls:
                break
            threading.Event().wait(0.02)
        if calls:
            cid = calls[-1][0]
            bridge.deliver_result(cid, ok=True, result=str(png_path))

    return bridge, tool_dict, auto_reply


def test_describe_viewport_returns_vision_description(tmp_path):
    bridge, tool_dict, auto_reply = _build_bridge_with_fake_capture(tmp_path)
    assert "describe_viewport" in tool_dict

    fake_response = MagicMock()
    fake_response.content = "A cartoon representation of a protein colored by chain."

    with patch(
        "agent_server.remote_tool.ChatGoogleGenerativeAI"
    ) as MockLLM:
        mock_instance = MagicMock()
        mock_instance.invoke.return_value = fake_response
        MockLLM.return_value = mock_instance

        worker = threading.Thread(target=auto_reply)
        worker.start()

        result = tool_dict["describe_viewport"].invoke({})

        worker.join(timeout=2.0)

    assert result == "A cartoon representation of a protein colored by chain."
    mock_instance.invoke.assert_called_once()

    call_args = mock_instance.invoke.call_args[0][0]
    assert len(call_args) == 1
    msg = call_args[0]
    assert any("image_url" in c.get("type", "") for c in msg.content if isinstance(c, dict))


def test_describe_viewport_returns_error_on_capture_failure(tmp_path):
    calls: list[tuple[str, str, dict]] = []

    def sender(call_id: str, name: str, args: dict) -> None:
        calls.append((call_id, name, args))

    bridge = RemoteToolBridge(send_tool_call=sender, timeout=5.0)
    tools = bridge.build_tools()
    tool_dict = {t.name: t for t in tools}

    def auto_reply_error():
        for _ in range(100):
            if calls:
                break
            threading.Event().wait(0.02)
        if calls:
            bridge.deliver_result(
                calls[-1][0], ok=True, result="[ERROR] cmd.png failed"
            )

    worker = threading.Thread(target=auto_reply_error)
    worker.start()

    result = tool_dict["describe_viewport"].invoke({})

    worker.join(timeout=2.0)
    assert result.startswith("[ERROR]")
