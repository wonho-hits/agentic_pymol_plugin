"""Tests for the ndjson message schema."""
from __future__ import annotations

import io
import json

import pytest

from agent_server import protocol


def test_write_message_emits_single_line() -> None:
    buf = io.StringIO()
    protocol.write_message(buf, {"type": "ready", "version": "0.1.0"})
    raw = buf.getvalue()
    assert raw.endswith("\n")
    assert raw.count("\n") == 1
    obj = json.loads(raw)
    assert obj == {"type": "ready", "version": "0.1.0"}


def test_write_message_preserves_non_ascii() -> None:
    buf = io.StringIO()
    protocol.write_message(buf, {"type": "event", "text": "한글 テスト"})
    obj = json.loads(buf.getvalue())
    assert obj["text"] == "한글 テスト"


def test_read_messages_skips_blank_lines() -> None:
    stream = io.StringIO(
        '{"type":"request","id":1,"prompt":"hi"}\n'
        "\n"
        '{"type":"cancel","id":1}\n'
    )
    messages = list(protocol.read_messages(stream))
    assert [m.type for m in messages] == ["request", "cancel"]
    assert messages[0].id == 1
    assert messages[0].payload["prompt"] == "hi"


def test_parse_rejects_non_object() -> None:
    with pytest.raises(ValueError):
        protocol.Message.parse("[1, 2, 3]")


def test_parse_rejects_missing_type() -> None:
    with pytest.raises(ValueError):
        protocol.Message.parse('{"id": 1}')


def test_builders_roundtrip() -> None:
    msg = protocol.tool_call(1, "c1", "run_pymol_python", {"code": "print(1)"})
    parsed = protocol.Message.parse(json.dumps(msg))
    assert parsed.type == protocol.MSG_TOOL_CALL
    assert parsed.id == 1
    assert parsed.payload["call_id"] == "c1"
    assert parsed.payload["args"] == {"code": "print(1)"}


def test_error_builder_optional_traceback() -> None:
    msg = protocol.error(None, "boom")
    assert "traceback" not in msg

    msg = protocol.error(7, "boom", traceback="frame1\nframe2")
    assert msg["traceback"] == "frame1\nframe2"
