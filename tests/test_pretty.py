"""Tests for the ``pretty`` tool handler."""
from __future__ import annotations

import sys
import types


class FakeCmd:
    def __init__(self, objects, chains_by_obj):
        self.objects = list(objects)
        self.chains_by_obj = chains_by_obj
        self.colors_set: list[tuple] = []
        self.colors_applied: list[tuple] = []
        self.shown: list[tuple] = []
        self.hidden: list[tuple] = []
        self.settings: dict = {}
        self.oriented = False
        self.util = types.SimpleNamespace(cnc=lambda sele: None)

    def set_color(self, name, rgb):
        self.colors_set.append((name, rgb))

    def show(self, rep, sele=""):
        self.shown.append((rep, sele))

    def hide(self, rep, sele=""):
        self.hidden.append((rep, sele))

    def color(self, color, sele=""):
        self.colors_applied.append((color, sele))

    def get_object_list(self, sele=""):
        return list(self.objects)

    def get_chains(self, sele=""):
        for obj in self.objects:
            if obj in sele:
                return list(self.chains_by_obj.get(obj, []))
        return []

    def set(self, key, value):
        self.settings[key] = value

    def orient(self, sele=""):
        self.oriented = True


def _install(monkeypatch, cmd):
    pymol = types.ModuleType("pymol")
    pymol.cmd = cmd
    pymol.stored = types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "pymol", pymol)


def test_handler_is_registered():
    from plugin_side.pymol_tools import TOOL_HANDLERS

    assert "pretty" in TOOL_HANDLERS


def test_pretty_applies_pastel_colors(monkeypatch):
    cmd = FakeCmd(objects=["prot"], chains_by_obj={"prot": ["A", "B"]})
    _install(monkeypatch, cmd)

    from plugin_side.pymol_tools import pretty

    ok, msg = pretty("all")

    assert ok is True
    assert msg.startswith("[OK]")
    assert len(cmd.colors_set) == 10
    color_names = [c[0] for c in cmd.colors_set]
    assert "pastel_blue" in color_names
    assert "pastel_coral" in color_names


def test_pretty_colors_per_chain(monkeypatch):
    cmd = FakeCmd(objects=["prot"], chains_by_obj={"prot": ["A", "B"]})
    _install(monkeypatch, cmd)

    from plugin_side.pymol_tools import pretty

    pretty("all")

    chain_colors = [c for c in cmd.colors_applied if "chain" in c[1]]
    assert len(chain_colors) == 2
    assert chain_colors[0][0] != chain_colors[1][0]


def test_pretty_single_chain_colors_object(monkeypatch):
    cmd = FakeCmd(objects=["mono"], chains_by_obj={"mono": ["A"]})
    _install(monkeypatch, cmd)

    from plugin_side.pymol_tools import pretty

    pretty("all")

    obj_colors = [c for c in cmd.colors_applied if "polymer and mono" == c[1]]
    assert len(obj_colors) == 1


def test_pretty_sets_rendering_options(monkeypatch):
    cmd = FakeCmd(objects=["prot"], chains_by_obj={"prot": ["A"]})
    _install(monkeypatch, cmd)

    from plugin_side.pymol_tools import pretty

    pretty("all")

    assert cmd.settings.get("depth_cue") == 0
    assert cmd.settings.get("cartoon_fancy_helices") == "on"
    assert cmd.settings.get("cartoon_side_chain_helper") == "on"
    assert cmd.oriented is True
