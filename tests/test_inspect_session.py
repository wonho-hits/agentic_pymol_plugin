"""`inspect_session` works without a real PyMOL by faking `pymol.cmd`
and `pymol.stored` imports. Guards against regressions in the JSON shape
agents rely on.
"""
from __future__ import annotations

import json
import sys
import types


def _install_fake_pymol(monkeypatch, *, objects, chains_by_obj, ligs_by_obj, selections):
    """Install a minimal pymol stub. Returns the stub module for inspection."""
    cmd = types.SimpleNamespace()

    def get_object_list():
        return list(objects)

    def count_atoms(sele):
        if sele in objects:
            return 1000
        if sele in selections:
            return selections[sele]
        for obj, groups in ligs_by_obj.items():
            for (chain, resi, resn) in groups:
                if f"{obj} and chain {chain} and resi {resi} and resn {resn}" == sele:
                    return 20
        return 0

    def get_chains(sele):
        return list(chains_by_obj.get(sele, []))

    def get_names(kind):
        assert kind == "selections"
        return list(selections.keys())

    def iterate(sele, expr):
        # Parse "<obj> and hetatm and not (...)" — only used by inspect_session.
        obj = sele.split(" ", 1)[0]
        for entry in ligs_by_obj.get(obj, []):
            stored._lig.add(entry)

    cmd.get_object_list = get_object_list
    cmd.count_atoms = count_atoms
    cmd.get_chains = get_chains
    cmd.get_names = get_names
    cmd.iterate = iterate

    stored = types.SimpleNamespace()

    pymol = types.ModuleType("pymol")
    pymol.cmd = cmd
    pymol.stored = stored
    monkeypatch.setitem(sys.modules, "pymol", pymol)
    return pymol


def test_inspect_session_returns_structured_json(monkeypatch):
    _install_fake_pymol(
        monkeypatch,
        objects=["2wyk"],
        chains_by_obj={"2wyk": ["A", "B"]},
        ligs_by_obj={"2wyk": [("A", "1309", "NGE")]},
        selections={"lig": 22, "iface": 180, "sele": 5},
    )

    from plugin_side.pymol_tools import inspect_session

    ok, payload = inspect_session()
    assert ok is True
    data = json.loads(payload)

    # Objects + chains + ligand groups.
    assert data["objects"] == [
        {
            "name": "2wyk",
            "n_atoms": 1000,
            "chains": ["A", "B"],
            "ligand_groups": [
                {"chain": "A", "resi": "1309", "resn": "NGE", "n_atoms": 20}
            ],
        }
    ]

    # 'sele' is intentionally excluded from user selections.
    names = [s["name"] for s in data["selections"]]
    assert "sele" not in names
    assert {"name": "lig", "n_atoms": 22} in data["selections"]
    assert {"name": "iface", "n_atoms": 180} in data["selections"]


def test_inspect_session_handler_is_registered():
    from plugin_side.pymol_tools import TOOL_HANDLERS

    assert "inspect_session" in TOOL_HANDLERS
    assert "run_pymol_python" in TOOL_HANDLERS
