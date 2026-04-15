"""Agent server ndjson loop.

Reads one JSON message per line from stdin, dispatches to a worker
thread that runs the deep agent, and writes events/tool_calls/done/error
messages back to stdout as they happen.

Responsibilities kept here:
    * Process lifecycle and graceful shutdown.
    * Routing ``tool_result`` messages back to the right
      :class:`RemoteToolBridge`.
    * Guarding stdout from stray prints so the ndjson channel stays clean.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import traceback
import uuid
from typing import Any

from . import protocol
from .remote_tool import RemoteToolBridge
from .session import AgentRunner

VERSION = "0.1.0"
DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_RECURSION = 50

log = logging.getLogger("agent_server")


def _prepend_session_context(prompt: str, context: dict | None) -> str:
    """Annotate the user prompt with a summary of the live PyMOL session.

    Keeps the agent from re-fetching or recreating objects that already
    exist. Uses a single plain-English line prefix rather than XML-like
    tags — smaller models otherwise parrot the tag block back instead of
    treating it as state.
    """
    if not context:
        return prompt
    objects = list(context.get("objects") or [])
    selections = list(context.get("selections") or [])
    if not objects and not selections:
        return prompt
    state = f"(current PyMOL session — objects: {objects}; user selections: {selections})"
    return f"{state}\n{prompt}"


class Server:
    def __init__(self) -> None:
        # Swap stdout for stderr so stray prints from libraries/tools do
        # not corrupt the ndjson channel. ``_out`` is the real stdout.
        self._out = protocol.silence_stdout_side_effects()
        self._out_lock = threading.Lock()

        self._model = os.environ.get("AGENTIC_PYMOL_MODEL", DEFAULT_MODEL)
        self._recursion = int(
            os.environ.get("AGENTIC_PYMOL_RECURSION", str(DEFAULT_RECURSION))
        )

        self._thread_id = uuid.uuid4().hex
        self._history: list[dict] = []

        self._active_request: int | None = None
        self._active_bridge: RemoteToolBridge | None = None
        self._active_lock = threading.Lock()
        self._stop_event = threading.Event()

    # ---- output ------------------------------------------------------------

    def _write(self, payload: dict) -> None:
        with self._out_lock:
            protocol.write_message(self._out, payload)

    # ---- lifecycle ---------------------------------------------------------

    def run(self) -> int:
        self._write(protocol.ready(VERSION))
        try:
            for msg in protocol.read_messages(sys.stdin):
                if self._stop_event.is_set():
                    break
                try:
                    self._dispatch(msg)
                except Exception as exc:
                    log.exception("dispatch failed")
                    self._write(
                        protocol.error(
                            msg.id,
                            f"dispatch failed: {exc}",
                            traceback=traceback.format_exc(),
                        )
                    )
        except Exception as exc:
            log.exception("server loop crashed")
            self._write(protocol.error(None, f"server crashed: {exc}"))
            return 1
        return 0

    def _dispatch(self, msg: protocol.Message) -> None:
        if msg.type == protocol.MSG_REQUEST:
            self._handle_request(msg)
        elif msg.type == protocol.MSG_TOOL_RESULT:
            self._handle_tool_result(msg)
        elif msg.type == protocol.MSG_CANCEL:
            self._handle_cancel(msg)
        elif msg.type == protocol.MSG_SHUTDOWN:
            self._stop_event.set()
        else:
            self._write(protocol.error(msg.id, f"unknown message type: {msg.type}"))

    # ---- request handling --------------------------------------------------

    def _handle_request(self, msg: protocol.Message) -> None:
        request_id = msg.id
        if request_id is None:
            self._write(protocol.error(None, "request missing 'id'"))
            return
        prompt = msg.payload.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            self._write(protocol.error(request_id, "request missing 'prompt'"))
            return
        context = msg.payload.get("context") if isinstance(msg.payload.get("context"), dict) else None

        with self._active_lock:
            if self._active_request is not None:
                self._write(
                    protocol.error(
                        request_id,
                        f"busy: request {self._active_request} still running",
                    )
                )
                return

            bridge = RemoteToolBridge(
                send_tool_call=lambda call_id, name, args: self._send_tool_call(
                    request_id, call_id, name, args
                )
            )
            self._active_request = request_id
            self._active_bridge = bridge

        thread = threading.Thread(
            target=self._run_request,
            args=(request_id, prompt, context, bridge),
            daemon=True,
            name=f"agent-request-{request_id}",
        )
        thread.start()

    def _run_request(
        self,
        request_id: int,
        prompt: str,
        context: dict | None,
        bridge: RemoteToolBridge,
    ) -> None:
        try:
            runner = AgentRunner(
                model_name=self._model,
                run_pymol_python=bridge.build_tool(),
                emit=lambda kind, fields: self._emit_event(request_id, kind, fields),
                recursion_limit=self._recursion,
            )
            annotated = _prepend_session_context(prompt, context)
            self._history.append({"role": "user", "content": annotated})
            final_text, new_history = runner.run(self._history, self._thread_id)
            self._history = new_history
            self._write(protocol.done(request_id, final_text))
        except Exception as exc:
            self._write(
                protocol.error(
                    request_id,
                    f"{exc}",
                    traceback=traceback.format_exc(),
                )
            )
        finally:
            with self._active_lock:
                if self._active_request == request_id:
                    self._active_request = None
                    self._active_bridge = None

    # ---- callbacks wired into the session ---------------------------------

    def _emit_event(self, request_id: int, kind: str, fields: dict[str, Any]) -> None:
        self._write(protocol.event(request_id, kind, **fields))

    def _send_tool_call(
        self, request_id: int, call_id: str, name: str, args: dict
    ) -> None:
        self._write(protocol.tool_call(request_id, call_id, name, args))

    # ---- inbound tool_result / cancel -------------------------------------

    def _handle_tool_result(self, msg: protocol.Message) -> None:
        call_id = msg.payload.get("call_id")
        if not isinstance(call_id, str):
            self._write(protocol.error(msg.id, "tool_result missing 'call_id'"))
            return
        ok = bool(msg.payload.get("ok", True))
        result = str(msg.payload.get("result", ""))

        with self._active_lock:
            bridge = self._active_bridge
            if bridge is None or self._active_request != msg.id:
                return
        bridge.deliver_result(call_id, ok, result)

    def _handle_cancel(self, msg: protocol.Message) -> None:
        with self._active_lock:
            bridge = self._active_bridge
            if bridge is None or self._active_request != msg.id:
                return
        bridge.cancel()


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("AGENTIC_PYMOL_LOG", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    return Server().run()


if __name__ == "__main__":
    sys.exit(main())
