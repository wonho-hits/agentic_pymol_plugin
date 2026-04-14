"""AST-based safety checks for agent-generated Python before exec().

Goal: allow the agent to drive PyMOL freely via ``cmd.*`` while rejecting
the obvious destructive or exfiltration patterns. This is a speed bump, not
a sandbox.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass


BLOCKED_IMPORT_ROOTS = {
    "os",
    "subprocess",
    "shutil",
    "sys",
    "socket",
    "urllib",
    "requests",
    "httpx",
    "ftplib",
    "smtplib",
    "paramiko",
    "pickle",
}

BLOCKED_BUILTIN_CALLS = {
    "exec",
    "eval",
    "compile",
    "__import__",
    "breakpoint",
}

# cmd.<method> calls that nuke the whole session
BLOCKED_CMD_METHODS = {"reinitialize", "quit"}

# cmd.<method> calls that often do broad damage — warn + still allow
SUSPICIOUS_CMD_METHODS = {"delete", "remove"}

# file modes that write
WRITE_MODE_FLAGS = set("wax+")


@dataclass
class SafetyResult:
    warnings: list[str]


class SafetyError(Exception):
    """Raised when code contains a hard-blocked pattern."""


def _attr_chain(node: ast.AST) -> tuple[str, ...]:
    """Return dotted name for ``a.b.c`` style access (empty tuple otherwise)."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return tuple(reversed(parts))
    return ()


def _check_import(node: ast.AST) -> None:
    if isinstance(node, ast.Import):
        for alias in node.names:
            root = alias.name.split(".", 1)[0]
            if root in BLOCKED_IMPORT_ROOTS:
                raise SafetyError(f"Blocked import: {alias.name}")
    elif isinstance(node, ast.ImportFrom):
        module = node.module or ""
        root = module.split(".", 1)[0]
        if root in BLOCKED_IMPORT_ROOTS:
            raise SafetyError(f"Blocked import: from {module}")


def _check_call(node: ast.Call, warnings: list[str]) -> None:
    func = node.func

    if isinstance(func, ast.Name):
        if func.id in BLOCKED_BUILTIN_CALLS:
            raise SafetyError(f"Blocked call: {func.id}()")
        if func.id == "open":
            _check_open(node)
        return

    if not isinstance(func, ast.Attribute):
        return

    chain = _attr_chain(func)
    if not chain:
        return

    # cmd.<method>
    if chain[0] == "cmd" and len(chain) >= 2:
        method = chain[-1]
        if method in BLOCKED_CMD_METHODS:
            raise SafetyError(f"Blocked call: cmd.{method}()")
        if method == "delete":
            if node.args and isinstance(node.args[0], ast.Constant):
                val = node.args[0].value
                if isinstance(val, str) and val.strip().lower() in {"all", "*"}:
                    raise SafetyError("Blocked call: cmd.delete('all')")
            warnings.append("cmd.delete called — ensure the selection is specific")
        elif method in SUSPICIOUS_CMD_METHODS:
            warnings.append(f"cmd.{method} called — verify the selection")
        return

    # foo.system / foo.popen where foo is a blocked module name
    if chain[0] in BLOCKED_IMPORT_ROOTS:
        raise SafetyError(f"Blocked access: {'.'.join(chain)}")


def _check_open(node: ast.Call) -> None:
    mode = None
    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
        mode = node.args[1].value
    for kw in node.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            mode = kw.value.value
    if isinstance(mode, str) and any(c in WRITE_MODE_FLAGS for c in mode):
        raise SafetyError(f"Blocked file write: open(..., mode={mode!r})")


def check_code(code: str) -> SafetyResult:
    """Parse ``code`` and raise SafetyError on hard blocks.

    Returns a SafetyResult with soft warnings to surface back to the agent.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise SafetyError(f"Syntax error: {e}") from e

    warnings: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            _check_import(node)
        elif isinstance(node, ast.Call):
            _check_call(node, warnings)

    return SafetyResult(warnings=warnings)
