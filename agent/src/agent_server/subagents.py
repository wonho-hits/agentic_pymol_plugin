"""SubAgent specs consumed by deepagents' ``create_deep_agent``.

The ``run_pymol_python`` tool here is an RPC proxy bound to the
per-request :class:`RemoteToolBridge` rather than a local executor.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

_PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


def _load(name: str) -> str:
    return (_PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")


def python_executor_spec(tools: list[Any]) -> dict:
    return {
        "name": "python_executor",
        "description": (
            "Writes and runs Python code inside the running PyMOL session to "
            "load structures, build selections, style representations, move "
            "the camera, and introspect scene state. Give it a concrete "
            "self-contained sub-goal described in natural language; it writes "
            "its own PyMOL code."
        ),
        "system_prompt": _load("python_executor"),
        "tools": list(tools),
    }
