"""PyMOL-side execution of tool calls received from the agent.

The agent process forwards ``run_pymol_python`` invocations over ndjson.
This module is the handler that actually runs the code inside the PyMOL
Python interpreter, with AST-based safety checks and a lock to serialise
concurrent calls from the agent's multiple sub-agents.
"""
from __future__ import annotations

import contextlib
import io
import json
import threading
import traceback

from .safety import SafetyError, check_code


_EXEC_LOCK = threading.Lock()
_MAX_OUTPUT = 8000


def _build_namespace() -> dict:
    """Whitelisted globals for agent-generated code."""
    from pymol import cmd, stored  # type: ignore
    import math

    ns: dict = {
        "cmd": cmd,
        "stored": stored,
        "math": math,
    }
    try:
        import numpy as np  # type: ignore

        ns["np"] = np
    except ImportError:
        pass
    return ns


def _truncate(text: str) -> str:
    if len(text) <= _MAX_OUTPUT:
        return text
    return text[:_MAX_OUTPUT] + f"\n[...truncated, {len(text) - _MAX_OUTPUT} more chars]"


def run_pymol_python(code: str) -> tuple[bool, str]:
    """Execute ``code`` inside the PyMOL session.

    Returns ``(ok, output)`` where ``ok`` is False only for hard safety
    blocks or unrecoverable tool-bridge failures. Ordinary exceptions
    raised by the user code surface as
    ``(True, "[ERROR] code raised an exception:\n<traceback>")`` because
    the agent should see the traceback and react to it. The leading
    ``[ERROR]`` marker is what the agent prompts anchor their retry rule
    on — keep it as the first token of the result.
    """
    try:
        result = check_code(code)
    except SafetyError as exc:
        return False, f"[BLOCKED] {exc}"

    buf = io.StringIO()
    ns = _build_namespace()

    with _EXEC_LOCK:
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                exec(compile(code, "<agent>", "exec"), ns)
            output = buf.getvalue() or "[OK, no stdout]"
        except Exception:
            captured = buf.getvalue()
            tb = traceback.format_exc()
            output = (
                "[ERROR] code raised an exception:\n"
                + (captured + "\n" if captured else "")
                + tb
            )

    if result.warnings:
        output = "[WARN] " + "; ".join(result.warnings) + "\n" + output
    return True, _truncate(output)


def inspect_session() -> tuple[bool, str]:
    """Structured JSON snapshot of the live PyMOL session.

    Returned JSON has:
      objects: [{name, n_atoms, chains, ligand_groups: [{chain,resi,resn,n_atoms}]}]
      selections: [{name, n_atoms}]   (excludes the anonymous 'sele')

    Errors inside the probe do not raise; the field is simply omitted and a
    top-level 'warnings' list records what failed. Always returns ok=True.
    """
    try:
        from pymol import cmd, stored  # type: ignore
    except Exception as exc:  # pragma: no cover — PyMOL always present at runtime
        return True, json.dumps({"error": f"pymol unavailable: {exc}"})

    snap: dict = {"objects": [], "selections": [], "warnings": []}

    with _EXEC_LOCK:
        try:
            object_names = list(cmd.get_object_list() or [])
        except Exception as exc:
            snap["warnings"].append(f"get_object_list: {exc}")
            object_names = []

        for obj in object_names:
            info: dict = {"name": obj}
            try:
                info["n_atoms"] = int(cmd.count_atoms(obj))
            except Exception as exc:
                snap["warnings"].append(f"count_atoms({obj}): {exc}")
            try:
                info["chains"] = sorted(set(cmd.get_chains(obj) or []))
            except Exception as exc:
                snap["warnings"].append(f"get_chains({obj}): {exc}")

            # Ligand groups: non-solvent, non-ion HETATM residues.
            try:
                stored._lig = set()
                cmd.iterate(
                    f"{obj} and hetatm and not (resn HOH or solvent or inorganic)",
                    "stored._lig.add((chain, resi, resn))",
                )
                groups = []
                for chain, resi, resn in sorted(stored._lig):
                    n = 0
                    try:
                        n = int(cmd.count_atoms(
                            f"{obj} and chain {chain} and resi {resi} and resn {resn}"
                        ))
                    except Exception:
                        pass
                    groups.append({"chain": chain, "resi": resi, "resn": resn, "n_atoms": n})
                info["ligand_groups"] = groups
            except Exception as exc:
                snap["warnings"].append(f"ligand_groups({obj}): {exc}")

            snap["objects"].append(info)

        try:
            for name in cmd.get_names("selections") or []:
                if name == "sele":
                    continue
                try:
                    n_atoms = int(cmd.count_atoms(name))
                except Exception:
                    n_atoms = -1
                snap["selections"].append({"name": name, "n_atoms": n_atoms})
        except Exception as exc:
            snap["warnings"].append(f"get_names(selections): {exc}")

    if not snap["warnings"]:
        snap.pop("warnings")
    return True, json.dumps(snap, ensure_ascii=False, indent=2)


TOOL_HANDLERS = {
    "run_pymol_python": lambda args: run_pymol_python(str(args.get("code", ""))),
    "inspect_session": lambda args: inspect_session(),
}


def snapshot_session() -> dict:
    """Return a small summary of the current PyMOL session state.

    The plugin attaches this to each request so the agent plans around
    the live session instead of re-fetching objects that already exist.
    Must never raise — returns a best-effort snapshot.
    """
    try:
        from pymol import cmd  # type: ignore
    except Exception:
        return {}

    snap: dict = {}
    try:
        snap["objects"] = list(cmd.get_object_list() or [])
    except Exception:
        snap["objects"] = []
    try:
        snap["selections"] = [
            n for n in (cmd.get_names("selections") or []) if n != "sele"
        ]
    except Exception:
        snap["selections"] = []
    return snap
