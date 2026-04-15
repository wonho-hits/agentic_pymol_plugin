"""NDJSON message schema shared over the plugin ↔ agent pipe.

Every line on stdin/stdout is a single JSON object. All inbound and
outbound messages carry an integer ``id`` that ties them to the
originating request. ``tool_call`` messages additionally carry a
string ``call_id`` so that multiple in-flight tool invocations inside
one request can be correlated with their results.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from typing import Any, Final, TextIO


# ---- message type constants --------------------------------------------------

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

# Event kinds carried inside MSG_EVENT.kind
EVENT_MESSAGE: Final = "message"
EVENT_TOOL_CALL_PREVIEW: Final = "tool_call_preview"
EVENT_TOOL_OUTPUT: Final = "tool_output"
EVENT_INFO: Final = "info"


# ---- typed envelope ----------------------------------------------------------


@dataclass(frozen=True)
class Message:
    """Parsed ndjson message — thin wrapper over the raw dict payload."""

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


# ---- low-level I/O -----------------------------------------------------------


def write_message(stream: TextIO, payload: dict[str, Any]) -> None:
    """Serialise one message as a JSON line and flush the stream.

    Flushing is mandatory — without it the reader on the other side will
    not see the line until the OS buffer fills.
    """
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    stream.write(line)
    stream.write("\n")
    stream.flush()


def read_messages(stream: TextIO):
    """Iterate over ndjson messages from ``stream`` until EOF.

    Blank lines are skipped; lines that fail to parse raise ``ValueError``
    with the offending text included so the caller can log and continue.
    """
    for raw in stream:
        line = raw.strip()
        if not line:
            continue
        yield Message.parse(line)


# ---- builders (agent → plugin) -----------------------------------------------


def ready(version: str) -> dict:
    return {"type": MSG_READY, "version": version}


def event(request_id: int, kind: str, **fields: Any) -> dict:
    return {"type": MSG_EVENT, "id": request_id, "kind": kind, **fields}


def tool_call(request_id: int, call_id: str, name: str, args: dict) -> dict:
    return {
        "type": MSG_TOOL_CALL,
        "id": request_id,
        "call_id": call_id,
        "name": name,
        "args": args,
    }


def done(request_id: int, final_text: str) -> dict:
    return {"type": MSG_DONE, "id": request_id, "final": final_text}


def error(request_id: int | None, message: str, *, traceback: str | None = None) -> dict:
    payload: dict[str, Any] = {"type": MSG_ERROR, "id": request_id, "message": message}
    if traceback:
        payload["traceback"] = traceback
    return payload


# ---- builders (plugin → agent) -----------------------------------------------


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


# ---- stdout redirection guard ------------------------------------------------


def silence_stdout_side_effects() -> TextIO:
    """Return the real stdout and redirect sys.stdout to stderr.

    Libraries (and user code inside tools) may ``print()`` — on the agent
    side those writes would corrupt the ndjson channel. We route them to
    stderr so the plugin's log file captures them harmlessly.
    """
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    return real_stdout
