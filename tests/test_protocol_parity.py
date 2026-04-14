"""Ensures the plugin-side and agent-side protocol copies stay in sync.

Both sides hand-write a copy of the ndjson message schema so they can
stay import-independent. This test reads both files and compares the
public API surface (constants + builder signatures) so drift is
noticed at CI time rather than at runtime.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENT_PROTOCOL = ROOT / "agent" / "src" / "agent_server" / "protocol.py"
PLUGIN_PROTOCOL = ROOT / "plugin_side" / "protocol.py"


def _collect_module_api(path: Path) -> dict[str, dict]:
    """Return ``{name: {'kind': ..., 'signature': ...}}`` for top-level defs."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: dict[str, dict] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            args = [a.arg for a in node.args.args]
            out[node.name] = {"kind": "function", "args": tuple(args)}
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.startswith(
                    ("MSG_", "EVENT_")
                ):
                    if isinstance(node.value, ast.Constant):
                        out[target.id] = {"kind": "const", "value": node.value.value}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
            if name.startswith(("MSG_", "EVENT_")) and isinstance(
                node.value, ast.Constant
            ):
                out[name] = {"kind": "const", "value": node.value.value}
    return out


def test_message_type_constants_match() -> None:
    a = _collect_module_api(AGENT_PROTOCOL)
    p = _collect_module_api(PLUGIN_PROTOCOL)

    a_consts = {k: v for k, v in a.items() if v["kind"] == "const"}
    p_consts = {k: v for k, v in p.items() if v["kind"] == "const"}

    # Plugin side is a subset: it doesn't need to mirror every constant
    # (e.g. the agent-only EVENT_* kinds) but every constant it does
    # export must match the agent's value.
    for name, meta in p_consts.items():
        assert name in a_consts, f"{name} missing from agent protocol"
        assert meta["value"] == a_consts[name]["value"], f"{name} value drift"


def test_shared_builders_have_matching_signatures() -> None:
    a = _collect_module_api(AGENT_PROTOCOL)
    p = _collect_module_api(PLUGIN_PROTOCOL)

    shared = {"request", "tool_result", "cancel", "shutdown"}
    for name in shared:
        assert name in a, f"agent protocol missing {name}"
        assert name in p, f"plugin protocol missing {name}"
        assert a[name]["args"] == p[name]["args"], f"{name} signature drift"
