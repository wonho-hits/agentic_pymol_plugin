"""Plugin configuration.

Loads ``.env.local`` next to ``__init__.py`` and resolves where to find
the out-of-process agent's Python interpreter (managed by uv under
``agent/.venv``).
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PLUGIN_ROOT = Path(__file__).resolve().parent
ENV_FILE = PLUGIN_ROOT / ".env.local"
AGENT_DIR = PLUGIN_ROOT / "agent"

DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_TIMEOUT = 60
DEFAULT_RECURSION = 50


class ConfigError(RuntimeError):
    pass


def load_config() -> dict:
    if ENV_FILE.exists():
        load_dotenv(ENV_FILE, override=False)

    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ConfigError(
            f"GOOGLE_API_KEY not set. Create {ENV_FILE} with GOOGLE_API_KEY=<your key> "
            "(see .env.example)."
        )
    os.environ["GOOGLE_API_KEY"] = api_key

    override = os.environ.get("AGENTIC_PYMOL_AGENT_PYTHON")
    if override:
        agent_python = Path(override).expanduser().resolve()
    else:
        from plugin_side.agent_client import resolve_agent_python

        agent_python = resolve_agent_python(AGENT_DIR)

    agent_dir_override = os.environ.get("AGENTIC_PYMOL_AGENT_DIR")
    if agent_dir_override:
        agent_dir = Path(agent_dir_override).expanduser().resolve()
    elif override:
        # `<agent_dir>/.venv/bin/python` → walk up to the agent project root.
        agent_dir = agent_python.parent.parent.parent
    else:
        agent_dir = AGENT_DIR

    if not agent_dir.is_dir():
        raise ConfigError(
            f"agent directory not found: {agent_dir}. Set AGENTIC_PYMOL_AGENT_DIR "
            "in .env.local to the absolute path of the `agent/` project, or "
            "symlink it next to the installed plugin."
        )

    model = os.environ.get("AGENTIC_PYMOL_MODEL", DEFAULT_MODEL)
    recursion = int(os.environ.get("AGENTIC_PYMOL_RECURSION", DEFAULT_RECURSION))

    return {
        "model": model,
        "timeout_seconds": int(os.environ.get("AGENTIC_PYMOL_TIMEOUT", DEFAULT_TIMEOUT)),
        "recursion_limit": recursion,
        "agent_python": agent_python,
        "agent_dir": agent_dir,
        "agent_env": {
            "GOOGLE_API_KEY": api_key,
            "AGENTIC_PYMOL_MODEL": model,
            "AGENTIC_PYMOL_RECURSION": str(recursion),
        },
    }
