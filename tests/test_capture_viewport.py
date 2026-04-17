"""Tests for the ``capture_viewport`` plugin-side handler."""
from __future__ import annotations

import sys
import tempfile
import types
from pathlib import Path


def _install_fake_pymol(monkeypatch, *, png_succeeds: bool = True):
    """Stub ``pymol.cmd.png`` to write a tiny file (or not)."""
    written: list[str] = []

    def fake_png(path, width=800, height=600, ray=0, quiet=1):
        written.append(path)
        if png_succeeds:
            Path(path).write_bytes(b"\x89PNG_FAKE")

    cmd = types.SimpleNamespace(png=fake_png)
    pymol = types.ModuleType("pymol")
    pymol.cmd = cmd
    pymol.stored = types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "pymol", pymol)
    return written


def test_handler_is_registered():
    from plugin_side.pymol_tools import TOOL_HANDLERS

    assert "capture_viewport" in TOOL_HANDLERS


def test_capture_writes_png_and_returns_path(monkeypatch, tmp_path):
    written = _install_fake_pymol(monkeypatch)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

    from plugin_side.pymol_tools import capture_viewport

    ok, result = capture_viewport(width=640, height=480)

    assert ok is True
    assert result.endswith("pymol_viewport.png")
    assert Path(result).exists()
    assert written == [result]


def test_capture_returns_error_when_png_fails(monkeypatch, tmp_path):
    _install_fake_pymol(monkeypatch, png_succeeds=False)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

    from plugin_side.pymol_tools import capture_viewport

    ok, result = capture_viewport()

    assert ok is True
    assert result.startswith("[ERROR]")
