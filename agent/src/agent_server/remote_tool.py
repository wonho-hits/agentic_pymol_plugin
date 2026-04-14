"""RPC proxy for ``run_pymol_python``.

The real PyMOL execution happens inside the plugin process. This module
exposes a LangChain-compatible tool that, when the agent calls it,
forwards the invocation to the plugin over ndjson and blocks until the
plugin replies with a ``tool_result``. From the agent's perspective it
is a synchronous, string-in/string-out tool.
"""
from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from langchain_core.tools import tool


ToolSender = Callable[[str, str, dict], None]
"""Callable injected by the server: ``sender(call_id, tool_name, args) -> None``."""


@dataclass
class _PendingCall:
    event: threading.Event = field(default_factory=threading.Event)
    result: str = ""
    ok: bool = True


class RemoteToolBridge:
    """Wire a single request's tool calls to/from the plugin.

    One bridge instance is created per request. The server calls
    :meth:`deliver_result` when a ``tool_result`` message arrives from
    the plugin; the tool function calls :meth:`_await` to block until
    the matching result lands.
    """

    def __init__(self, send_tool_call: ToolSender, timeout: float = 120.0) -> None:
        self._send = send_tool_call
        self._timeout = timeout
        self._pending: dict[str, _PendingCall] = {}
        self._lock = threading.Lock()
        self._cancelled = False

    # ---- server-facing API -------------------------------------------------

    def deliver_result(self, call_id: str, ok: bool, result: str) -> None:
        with self._lock:
            pending = self._pending.get(call_id)
        if pending is None:
            return
        pending.ok = ok
        pending.result = result
        pending.event.set()

    def cancel(self) -> None:
        """Unblock any in-flight tool calls with a cancellation marker."""
        with self._lock:
            self._cancelled = True
            pendings = list(self._pending.values())
        for pending in pendings:
            pending.ok = False
            pending.result = "[CANCELLED] request was cancelled by the plugin"
            pending.event.set()

    # ---- tool-facing API ---------------------------------------------------

    def build_tool(self) -> Any:
        """Return a LangChain tool bound to this bridge."""
        bridge = self

        @tool
        def run_pymol_python(code: str) -> str:
            """Execute Python code inside the running PyMOL session.

            Available names: ``cmd`` (pymol.cmd), ``stored`` (pymol.stored),
            ``math``, ``np`` (numpy if installed). Imports of os / subprocess /
            shutil / sys / socket / urllib / requests are blocked.
            ``cmd.reinitialize()``, ``cmd.delete('all')``, ``cmd.quit()`` are blocked.
            ``cmd.fetch`` and other normal PyMOL commands are allowed.

            Use ``print(...)`` to surface values — the tool returns captured stdout.
            Run small verifiable chunks; inspect results before continuing.
            """
            return bridge._call("run_pymol_python", {"code": code})

        return run_pymol_python

    # ---- internals ---------------------------------------------------------

    def _call(self, name: str, args: dict) -> str:
        call_id = uuid.uuid4().hex[:12]
        pending = _PendingCall()
        with self._lock:
            if self._cancelled:
                return "[CANCELLED] request was cancelled before tool call"
            self._pending[call_id] = pending

        try:
            self._send(call_id, name, args)
        except Exception as exc:
            with self._lock:
                self._pending.pop(call_id, None)
            return f"[TOOL-BRIDGE-ERROR] failed to send tool call: {exc}"

        if not pending.event.wait(self._timeout):
            with self._lock:
                self._pending.pop(call_id, None)
            return f"[TOOL-BRIDGE-TIMEOUT] no result in {self._timeout:.0f}s"

        with self._lock:
            self._pending.pop(call_id, None)

        if not pending.ok:
            return pending.result or "[TOOL-BRIDGE-ERROR] plugin reported failure"
        return pending.result
