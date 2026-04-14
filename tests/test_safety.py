"""Plugin-side AST safety checks for agent-generated Python."""
from __future__ import annotations

import pytest

from plugin_side.safety import SafetyError, check_code


def test_blocks_os_import() -> None:
    with pytest.raises(SafetyError):
        check_code("import os")


def test_blocks_subprocess_from_import() -> None:
    with pytest.raises(SafetyError):
        check_code("from subprocess import run")


def test_blocks_exec_builtin() -> None:
    with pytest.raises(SafetyError):
        check_code("exec('print(1)')")


def test_blocks_cmd_reinitialize() -> None:
    with pytest.raises(SafetyError):
        check_code("cmd.reinitialize()")


def test_blocks_cmd_delete_all() -> None:
    with pytest.raises(SafetyError):
        check_code("cmd.delete('all')")


def test_allows_cmd_fetch() -> None:
    result = check_code("cmd.fetch('1UBQ')")
    assert result.warnings == []


def test_warns_on_cmd_delete_specific() -> None:
    result = check_code("cmd.delete('lig')")
    assert any("cmd.delete" in w for w in result.warnings)


def test_blocks_file_write() -> None:
    with pytest.raises(SafetyError):
        check_code("open('/tmp/x', 'w')")


def test_allows_file_read() -> None:
    result = check_code("open('/tmp/x', 'r')")
    assert result.warnings == []
