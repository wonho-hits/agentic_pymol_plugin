"""Agentic PyMOL Plugin — natural-language control via an out-of-process deep agent.

Architecture:
    * This module loads inside PyMOL (Python 3.10) and does only UI/command
      registration and PyMOL-side tool execution.
    * The actual LangChain / deepagents / Gemini stack runs in a separate
      Python 3.11 process managed by uv under ``agent/``.
    * They communicate over the subprocess's stdin/stdout using newline-
      delimited JSON (see ``plugin_side/protocol.py``).

Commands registered inside PyMOL:
    ask <text>      — send a request to the agent
    ask_reset       — restart agent process, clearing conversation memory
    ask_status      — show whether a request is currently in flight
    ask_cancel      — cancel the currently running request

Put your Gemini key in ``.env.local`` next to this file (see ``.env.example``).
Install the agent environment once with ``cd agent && uv sync``.
"""
from __future__ import annotations

from .config import ConfigError, load_config
from .plugin_side.agent_client import AgentClient, AgentClientError

_CLIENT: AgentClient | None = None


def _emit(msg: str) -> None:
    """Thread-safe-enough printer; PyMOL captures stdout to the feedback log."""
    print(msg)


def _ensure_client() -> AgentClient:
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT

    cfg = load_config()
    client = AgentClient(
        agent_python=cfg["agent_python"],
        agent_cwd=cfg["agent_dir"],
        on_event=_emit,
        env=cfg["agent_env"],
    )
    client.start()
    _CLIENT = client
    _emit(f"[agent] ready — model={cfg['model']} thread={client.thread_id[:8]}")
    return _CLIENT


def ask(*args, **_kwargs) -> None:
    """PyMOL command: ``ask <natural-language request>``."""
    message = " ".join(str(a) for a in args).strip()
    if not message:
        print("Usage: ask <your question or instruction>")
        return
    try:
        client = _ensure_client()
    except ConfigError as exc:
        print(f"[agent] config error: {exc}")
        return
    except AgentClientError as exc:
        print(f"[agent] failed to start agent subprocess: {exc}")
        return
    except Exception as exc:  # pragma: no cover — init paths
        print(f"[agent] init failed: {exc}")
        return
    client.ask(message)


def ask_reset(*_args, **_kwargs) -> None:
    """PyMOL command: clear conversation memory (restarts the subprocess)."""
    global _CLIENT
    if _CLIENT is None:
        print("[agent] no active session")
        return
    try:
        _CLIENT.reset()
    except AgentClientError as exc:
        print(f"[agent] reset failed: {exc}")
        _CLIENT = None
        return
    print(f"[agent] memory cleared — new thread={_CLIENT.thread_id[:8]}")


def ask_status(*_args, **_kwargs) -> None:
    """PyMOL command: show whether the agent is currently running."""
    if _CLIENT is None:
        print("[agent] not initialized (run `ask <...>` to start)")
        return
    state = "RUNNING" if _CLIENT.is_running else "idle"
    print(f"[agent] {state}  thread={_CLIENT.thread_id[:8]}")


def ask_cancel(*_args, **_kwargs) -> None:
    """PyMOL command: cancel the currently running request."""
    if _CLIENT is None or not _CLIENT.is_running:
        print("[agent] nothing to cancel")
        return
    _CLIENT.cancel()
    print("[agent] cancel requested")


def _show_usage() -> None:
    print(
        "Agentic PyMOL Plugin\n"
        "  ask <request>   — natural-language PyMOL control\n"
        "  ask_reset       — restart agent, clear conversation memory\n"
        "  ask_status      — show current agent status\n"
        "  ask_cancel      — cancel the running request\n"
        "Configure GOOGLE_API_KEY in .env.local next to the plugin.\n"
        "Install the agent env with: cd agent && uv sync"
    )


def __init_plugin__(app=None):  # noqa: N807 — PyMOL plugin entry point
    # Imported lazily so the package can be imported (e.g. by pytest) outside PyMOL.
    from pymol import cmd

    cmd.extend("ask", ask)
    cmd.extend("ask_reset", ask_reset)
    cmd.extend("ask_status", ask_status)
    cmd.extend("ask_cancel", ask_cancel)  # noqa: F821 — cmd bound above
    try:
        from pymol.plugins import addmenuitemqt

        addmenuitemqt("Agentic PyMOL (usage)", _show_usage)
    except Exception:
        # Plugins menu registration is best-effort; the commands still work.
        pass
