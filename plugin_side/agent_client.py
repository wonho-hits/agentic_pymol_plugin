"""Plugin-side client for the out-of-process agent server.

Spawns the agent as a subprocess (using the Python interpreter inside
the agent's uv-managed ``.venv``), reads ndjson messages from its
stdout on a background thread, dispatches tool calls to PyMOL-side
handlers, and surfaces events through a user-supplied ``on_event``
callback so the plugin can print them to the PyMOL feedback log.
"""
from __future__ import annotations

import itertools
import json
import logging
import os
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable
from pathlib import Path

from . import protocol
from .pymol_tools import TOOL_HANDLERS, snapshot_session

log = logging.getLogger("agent_client")

EventSink = Callable[[str], None]
"""Receives human-readable status lines (one per call). Must be safe to
call from a background thread — PyMOL's ``print`` is."""

ToolHandler = Callable[[dict], "tuple[bool, str]"]


class AgentClientError(RuntimeError):
    """Raised when the subprocess cannot start or the pipe dies."""


class AgentClient:
    """Owns one long-lived agent subprocess.

    Thread-safety model: only ``ask``/``reset``/``cancel``/``close`` are
    meant to be called from PyMOL's main thread. All stdout reading and
    event rendering happens on a single reader thread.
    """

    def __init__(
        self,
        agent_python: Path,
        agent_cwd: Path,
        on_event: EventSink,
        env: dict[str, str] | None = None,
        tool_handlers: dict[str, ToolHandler] | None = None,
        stderr_log_path: Path | None = None,
    ) -> None:
        self._agent_python = agent_python
        self._agent_cwd = agent_cwd
        self._on_event = on_event
        self._env = env or {}
        self._tool_handlers = tool_handlers or TOOL_HANDLERS
        self._stderr_log_path = stderr_log_path
        self._stderr_file = None  # opened lazily in _stderr_loop

        self._proc: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._id_counter = itertools.count(1)

        self._thread_id = uuid.uuid4().hex
        self._current_request: int | None = None
        self._current_done = threading.Event()
        self._ready_event = threading.Event()
        self._ready_received = False
        self._closed = False

    # ---- public API -------------------------------------------------------

    @property
    def thread_id(self) -> str:
        return self._thread_id

    @property
    def is_running(self) -> bool:
        return self._current_request is not None

    def start(self, ready_timeout: float = 30.0) -> None:
        if self._proc is not None:
            return
        if not self._agent_python.exists():
            raise AgentClientError(
                f"agent python not found: {self._agent_python}\n"
                f"Run `uv sync` inside {self._agent_cwd}."
            )

        env = os.environ.copy()
        env.update(self._env)
        # Force unbuffered stdio on the child so ndjson streams in real time.
        env.setdefault("PYTHONUNBUFFERED", "1")
        # Make `python -m agent_server` work even if the user pointed
        # AGENTIC_PYMOL_AGENT_PYTHON at a bare interpreter that doesn't
        # have the package installed.
        src_dir = self._agent_cwd / "src"
        if src_dir.is_dir():
            existing = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = (
                f"{src_dir}{os.pathsep}{existing}" if existing else str(src_dir)
            )

        cmd = [str(self._agent_python), "-u", "-m", "agent_server"]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                bufsize=1,
                cwd=str(self._agent_cwd),
                env=env,
            )
        except OSError as exc:
            raise AgentClientError(f"failed to spawn agent: {exc}") from exc

        self._reader = threading.Thread(
            target=self._read_loop, daemon=True, name="agent-stdout-reader"
        )
        self._reader.start()

        self._stderr_reader = threading.Thread(
            target=self._stderr_loop, daemon=True, name="agent-stderr-reader"
        )
        self._stderr_reader.start()

        if not self._ready_event.wait(ready_timeout):
            raise AgentClientError("agent did not become ready in time")
        if not self._ready_received:
            raise AgentClientError(
                "agent subprocess exited before sending 'ready' "
                "(check the [agent-stderr] lines above for the real error)"
            )

    def ask(self, prompt: str) -> bool:
        """Send a request. Returns False if another request is in flight."""
        self._ensure_started()
        if self._current_request is not None:
            self._on_event("[agent] busy — previous request still running")
            return False

        rid = next(self._id_counter)
        self._current_request = rid
        self._current_done.clear()
        self._on_event(f"[agent] ▶ {_short(prompt, 200)}")
        try:
            context = snapshot_session()
        except Exception:
            context = {}
        self._send(protocol.request(rid, prompt, context))
        return True

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Block until the current request finishes (best effort)."""
        if self._current_request is None:
            return True
        return self._current_done.wait(timeout)

    def cancel(self) -> None:
        rid = self._current_request
        if rid is None:
            return
        self._send(protocol.cancel(rid))

    def reset(self) -> None:
        """Clear conversation memory by restarting the agent subprocess.

        The agent keeps history in-process for simplicity; the cleanest
        way to clear it is to bounce the subprocess.
        """
        self.close()
        self._thread_id = uuid.uuid4().hex
        self._ready_event.clear()
        self.start()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        if proc is None:
            return
        try:
            self._send(protocol.shutdown())
        except Exception:
            pass
        try:
            proc.stdin.close() if proc.stdin else None
        except Exception:
            pass
        try:
            proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
        self._proc = None
        self._closed = False  # allow restart via reset()

    # ---- internals --------------------------------------------------------

    def _ensure_started(self) -> None:
        if self._proc is None:
            self.start()

    def _send(self, payload: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise AgentClientError("agent is not running")
        with self._write_lock:
            try:
                protocol.write_message(proc.stdin, payload)
            except (BrokenPipeError, ValueError) as exc:
                raise AgentClientError(f"agent pipe broken: {exc}") from exc

    def _read_loop(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        try:
            for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    msg = protocol.Message.parse(line)
                except (ValueError, json.JSONDecodeError) as exc:
                    log.warning("bad ndjson line from agent: %s (%s)", line[:200], exc)
                    continue
                try:
                    self._handle_message(msg)
                except Exception:
                    log.exception("handler crashed on %s", msg.type)
        finally:
            self._ready_event.set()  # unblock any waiter
            self._current_done.set()

    def _stderr_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        # Append agent stderr to a log file instead of printing to PyMOL's
        # console. Users can `tail` it when debugging, but day-to-day the
        # feedback area stays clean.
        sink = None
        if self._stderr_log_path is not None:
            try:
                self._stderr_log_path.parent.mkdir(parents=True, exist_ok=True)
                sink = self._stderr_log_path.open("a", encoding="utf-8")
                self._stderr_file = sink
                sink.write(f"\n--- agent-stderr session {self._thread_id[:8]} ---\n")
                sink.flush()
            except Exception:
                sink = None
        try:
            for raw in proc.stderr:
                line = raw.rstrip()
                if not line:
                    continue
                if sink is not None:
                    try:
                        sink.write(line + "\n")
                        sink.flush()
                    except Exception:
                        pass
                log.debug("agent-stderr: %s", line)
        finally:
            if sink is not None:
                try:
                    sink.close()
                except Exception:
                    pass
                self._stderr_file = None

    def _handle_message(self, msg: protocol.Message) -> None:
        if msg.type == protocol.MSG_READY:
            self._on_event(
                f"[agent] ready (server v{msg.payload.get('version', '?')})"
            )
            self._ready_received = True
            self._ready_event.set()
            return

        if msg.type == protocol.MSG_EVENT:
            self._render_event(msg)
            return

        if msg.type == protocol.MSG_TOOL_CALL:
            self._handle_tool_call(msg)
            return

        if msg.type == protocol.MSG_DONE:
            final = str(msg.payload.get("final", "") or "")
            if final:
                self._on_event(f"[agent] ✓ {final}")
            else:
                self._on_event("[agent] done")
            self._current_request = None
            self._current_done.set()
            return

        if msg.type == protocol.MSG_ERROR:
            self._on_event(f"[agent] ERROR: {msg.payload.get('message', '?')}")
            tb = msg.payload.get("traceback")
            if tb:
                self._on_event(str(tb))
            self._current_request = None
            self._current_done.set()
            return

        log.warning("unknown message type from agent: %s", msg.type)

    def _render_event(self, msg: protocol.Message) -> None:
        kind = msg.payload.get("kind")
        node = msg.payload.get("node") or "?"
        if kind == protocol.EVENT_MESSAGE:
            text = msg.payload.get("text") or ""
            if text:
                self._on_event(f"[{node}] {text}")
        elif kind == protocol.EVENT_TOOL_CALL_PREVIEW:
            name = msg.payload.get("name", "?")
            preview = msg.payload.get("preview", "")
            if name == "write_todos":
                self._on_event(self._format_todos_preview(node, preview))
            elif "\n" in preview:
                body = "\n".join("  " + line for line in preview.splitlines())
                self._on_event(f"[{node}] → {name}:\n{body}")
            else:
                self._on_event(f"[{node}] → {name}({preview})")
        elif kind == protocol.EVENT_TOOL_OUTPUT:
            name = msg.payload.get("name", "tool")
            text = msg.payload.get("text", "")
            if name == "write_todos":
                self._on_event(self._format_todos_output(text))
            elif text == "[OK, no stdout]":
                self._on_event(f"[tool·{name}] OK")
            elif "\n" in text:
                body = "\n".join("  " + line for line in text.splitlines())
                self._on_event(f"[tool·{name}]\n{body}")
            else:
                self._on_event(f"[tool·{name}] {text}")
        elif kind == protocol.EVENT_INFO:
            text = msg.payload.get("text", "")
            if text:
                self._on_event(f"[agent] {text}")

    @staticmethod
    def _format_todos_preview(node: str, preview: str) -> str:
        """Render a write_todos tool call as a readable checklist."""
        _STATUS_ICONS = {"in_progress": "▶", "completed": "✓"}
        try:
            import ast
            data = ast.literal_eval(preview)
            todos = data.get("todos", []) if isinstance(data, dict) else data
            if not isinstance(todos, list):
                raise ValueError
        except Exception:
            return f"[{node}] → write_todos({_short(preview, 120)})"
        lines = [f"[{node}] → write_todos:"]
        for t in todos:
            if isinstance(t, dict):
                icon = _STATUS_ICONS.get(t.get("status", ""), "☐")
                lines.append(f"  {icon} {t.get('content', '?')}")
        return "\n".join(lines)

    @staticmethod
    def _format_todos_output(text: str) -> str:
        """Render write_todos result as a short summary."""
        _STATUS_ICONS = {"in_progress": "▶", "completed": "✓"}
        try:
            prefix = "Updated todo list to "
            if text.startswith(prefix):
                import ast
                todos = ast.literal_eval(text[len(prefix):])
                if isinstance(todos, list):
                    counts = {}
                    for t in todos:
                        s = t.get("status", "pending") if isinstance(t, dict) else "pending"
                        counts[s] = counts.get(s, 0) + 1
                    parts = []
                    for s in ("completed", "in_progress", "pending"):
                        if s in counts:
                            icon = _STATUS_ICONS.get(s, "☐")
                            parts.append(f"{icon}{counts[s]}")
                    return f"[tool·write_todos] {' '.join(parts)}"
        except Exception:
            pass
        return f"[tool·write_todos] OK"

    def _handle_tool_call(self, msg: protocol.Message) -> None:
        request_id = msg.id
        call_id = msg.payload.get("call_id")
        name = msg.payload.get("name")
        args = msg.payload.get("args") or {}
        if request_id is None or not isinstance(call_id, str) or not isinstance(name, str):
            return

        handler = self._tool_handlers.get(name)
        if handler is None:
            self._send(
                protocol.tool_result(
                    request_id, call_id, False, f"[UNKNOWN-TOOL] {name}"
                )
            )
            return

        try:
            ok, result = handler(args if isinstance(args, dict) else {})
        except Exception as exc:
            ok = False
            result = f"[TOOL-HANDLER-ERROR] {exc}"
        self._send(protocol.tool_result(request_id, call_id, bool(ok), str(result)))


def _short(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + f" …(+{len(text) - limit} chars)"


# ---- path resolution ---------------------------------------------------------


def resolve_agent_python(agent_dir: Path) -> Path:
    """Locate the uv-managed interpreter inside ``agent_dir/.venv``.

    Windows places the executable in ``Scripts``; POSIX uses ``bin``.
    """
    if sys.platform.startswith("win"):
        return agent_dir / ".venv" / "Scripts" / "python.exe"
    return agent_dir / ".venv" / "bin" / "python"
