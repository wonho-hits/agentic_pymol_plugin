"""Plugin-side copy of the ndjson message schema.

Intentionally duplicated from ``agent_server.protocol`` so that the
plugin package has zero dependency on the agent's Python environment
(they run in separate interpreters). Keep both copies in sync — the set
of constants and builder signatures is the contract.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final, TextIO


# Plugin → Agent
MSG_REQUEST: Final = "request"
MSG_TOOL_RESULT: Final = "tool_result"
MSG_CANCEL: Final = "cancel"
MSG_SHUTDOWN: Final = "shutdown"

# Agent → Plugin
MSG_READY: Final = "ready"
MSG_EVENT: Final = "event"
MSG_TOOL_CALL: Final = "tool_call"
MSG_DONE: Final = "done"
MSG_ERROR: Final = "error"

EVENT_MESSAGE: Final = "message"
EVENT_TOOL_CALL_PREVIEW: Final = "tool_call_preview"
EVENT_TOOL_OUTPUT: Final = "tool_output"
EVENT_INFO: Final = "info"


@dataclass(frozen=True)
class Message:
    type: str
    id: int | None
    payload: dict

    @classmethod
    def parse(cls, line: str) -> "Message":
        obj = json.loads(line)
        if not isinstance(obj, dict):
            raise ValueError(f"expected JSON object, got {type(obj).__name__}")
        mtype = obj.get("type")
        if not isinstance(mtype, str):
            raise ValueError("missing 'type' field")
        raw_id = obj.get("id")
        mid = raw_id if isinstance(raw_id, int) else None
        return cls(type=mtype, id=mid, payload=obj)


def write_message(stream: TextIO, payload: dict[str, Any]) -> None:
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    stream.write(line)
    stream.write("\n")
    stream.flush()


def request(request_id: int, prompt: str, context: dict | None = None) -> dict:
    payload: dict = {"type": MSG_REQUEST, "id": request_id, "prompt": prompt}
    if context:
        payload["context"] = context
    return payload


def tool_result(request_id: int, call_id: str, ok: bool, result: str) -> dict:
    return {
        "type": MSG_TOOL_RESULT,
        "id": request_id,
        "call_id": call_id,
        "ok": ok,
        "result": result,
    }


def cancel(request_id: int) -> dict:
    return {"type": MSG_CANCEL, "id": request_id}


def shutdown() -> dict:
    return {"type": MSG_SHUTDOWN}
