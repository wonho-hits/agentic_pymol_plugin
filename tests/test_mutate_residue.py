"""Unit tests for the ``mutate_residue`` tool handler.

These tests stub ``pymol.cmd`` (the mutagenesis wizard is mode-stateful
and PyMOL is not importable in CI), so the assertions cover argument
validation, the correct wizard call sequence, chain auto-detection, and
the object-survival rollback path — not the actual side-effect of
residue swapping.
"""
from __future__ import annotations

import sys
import types
from typing import Any


class FakeWizard:
    def __init__(self, on_apply=None) -> None:
        self.modes: list[str] = []
        self.selects: list[str] = []
        self.applied = 0
        self._on_apply = on_apply

    def set_mode(self, mode: str) -> None:
        self.modes.append(mode)

    def do_select(self, sel: str) -> None:
        self.selects.append(sel)

    def apply(self) -> None:
        self.applied += 1
        if self._on_apply is not None:
            self._on_apply()


class FakeCmd:
    def __init__(
        self,
        *,
        objects: list[str],
        chains: dict[str, list[str]],
        resi_counts: dict[tuple[str, str, str], int],
        on_apply=None,
        delete_on_apply: str | None = None,
    ) -> None:
        self.objects = list(objects)
        self.chains = chains
        self.resi_counts = resi_counts
        self.wizard_active: str | None = None
        self.wizard_obj = FakeWizard(on_apply=self._on_apply)
        self.refreshed = 0
        self.closed_wizard = 0
        self.deleted_sels: list[str] = []
        self.undo_called = 0
        self._on_apply_extra = on_apply
        self._delete_on_apply = delete_on_apply

    def _on_apply(self) -> None:
        if self._delete_on_apply and self._delete_on_apply in self.objects:
            self.objects.remove(self._delete_on_apply)
        if self._on_apply_extra:
            self._on_apply_extra()

    def get_object_list(self) -> list[str]:
        return list(self.objects)

    def get_chains(self, obj: str) -> list[str]:
        return list(self.chains.get(obj, []))

    def count_atoms(self, sele: str) -> int:
        # Parse "obj and chain X and resi Y"
        parts = [p.strip() for p in sele.split(" and ")]
        obj = parts[0].strip()
        chain = ""
        resi = ""
        for p in parts[1:]:
            if p.startswith("chain "):
                chain = p.split(" ", 1)[1].strip()
            elif p.startswith("resi "):
                resi = p.split(" ", 1)[1].strip()
        return self.resi_counts.get((obj, chain, resi), 0)

    def wizard(self, name: str) -> None:
        self.wizard_active = name

    def refresh_wizard(self) -> None:
        self.refreshed += 1

    def get_wizard(self) -> FakeWizard:
        return self.wizard_obj

    def set_wizard(self, *args: Any) -> None:
        self.closed_wizard += 1
        self.wizard_active = None

    def delete(self, name: str) -> None:
        self.deleted_sels.append(name)

    def undo(self) -> None:
        self.undo_called += 1


def _install(monkeypatch, cmd: FakeCmd) -> None:
    pymol = types.ModuleType("pymol")
    pymol.cmd = cmd
    pymol.stored = types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "pymol", pymol)


def test_handler_is_registered() -> None:
    from plugin_side.pymol_tools import TOOL_HANDLERS

    assert "mutate_residue" in TOOL_HANDLERS


def test_success_path_with_explicit_chain(monkeypatch) -> None:
    cmd = FakeCmd(
        objects=["prot"],
        chains={"prot": ["A", "B"]},
        resi_counts={("prot", "A", "94"): 8},
    )
    _install(monkeypatch, cmd)

    from plugin_side.pymol_tools import mutate_residue

    ok, msg = mutate_residue("prot", "A", "94", "asn")

    assert ok is True
    assert msg.startswith("[OK] mutated prot/A/94 → ASN")
    assert cmd.wizard_obj.modes == ["ASN"]
    assert cmd.wizard_obj.selects == ["/prot//A/94/"]
    assert cmd.wizard_obj.applied == 1
    assert cmd.refreshed == 1
    assert cmd.closed_wizard == 1
    # _pk* selections cleaned up
    assert "_pkbase1" in cmd.deleted_sels
    assert "_pkfrag2" in cmd.deleted_sels


def test_auto_detects_chain_when_unique(monkeypatch) -> None:
    cmd = FakeCmd(
        objects=["prot"],
        chains={"prot": ["A", "B"]},
        resi_counts={("prot", "A", "94"): 8},  # only chain A has resi 94
    )
    _install(monkeypatch, cmd)

    from plugin_side.pymol_tools import mutate_residue

    ok, msg = mutate_residue("prot", "", "94", "ASN")

    assert ok is True
    assert "prot/A/94" in msg
    assert cmd.wizard_obj.selects == ["/prot//A/94/"]


def test_rejects_ambiguous_chain(monkeypatch) -> None:
    cmd = FakeCmd(
        objects=["prot"],
        chains={"prot": ["A", "B"]},
        resi_counts={("prot", "A", "94"): 8, ("prot", "B", "94"): 8},
    )
    _install(monkeypatch, cmd)

    from plugin_side.pymol_tools import mutate_residue

    ok, msg = mutate_residue("prot", "", "94", "ASN")

    assert ok is True
    assert msg.startswith("[ERROR]")
    assert "multiple chains" in msg
    assert cmd.wizard_obj.applied == 0


def test_rejects_unknown_object(monkeypatch) -> None:
    cmd = FakeCmd(objects=["other"], chains={}, resi_counts={})
    _install(monkeypatch, cmd)

    from plugin_side.pymol_tools import mutate_residue

    ok, msg = mutate_residue("prot", "A", "94", "ASN")

    assert ok is True
    assert msg.startswith("[ERROR]")
    assert "not found" in msg
    assert cmd.wizard_obj.applied == 0


def test_rejects_missing_residue(monkeypatch) -> None:
    cmd = FakeCmd(
        objects=["prot"],
        chains={"prot": ["A"]},
        resi_counts={},  # resi 94 returns 0 atoms
    )
    _install(monkeypatch, cmd)

    from plugin_side.pymol_tools import mutate_residue

    ok, msg = mutate_residue("prot", "A", "94", "ASN")

    assert ok is True
    assert msg.startswith("[ERROR]")


def test_rejects_invalid_target_aa(monkeypatch) -> None:
    cmd = FakeCmd(
        objects=["prot"],
        chains={"prot": ["A"]},
        resi_counts={("prot", "A", "94"): 8},
    )
    _install(monkeypatch, cmd)

    from plugin_side.pymol_tools import mutate_residue

    ok, msg = mutate_residue("prot", "A", "94", "XYZ")

    assert ok is True
    assert msg.startswith("[ERROR]")
    assert "3-letter" in msg
    assert cmd.wizard_obj.applied == 0


def test_rolls_back_when_apply_deletes_object(monkeypatch) -> None:
    cmd = FakeCmd(
        objects=["prot"],
        chains={"prot": ["A"]},
        resi_counts={("prot", "A", "94"): 8},
        delete_on_apply="prot",  # simulate the bug the tool exists to catch
    )
    _install(monkeypatch, cmd)

    from plugin_side.pymol_tools import mutate_residue

    ok, msg = mutate_residue("prot", "A", "94", "ASN")

    assert ok is True
    assert msg.startswith("[ERROR]")
    assert "deleted" in msg
    assert cmd.undo_called == 1
    assert cmd.closed_wizard == 1  # wizard still closed cleanly


def test_closes_wizard_even_on_apply_exception(monkeypatch) -> None:
    def boom() -> None:
        raise RuntimeError("wizard boom")

    cmd = FakeCmd(
        objects=["prot"],
        chains={"prot": ["A"]},
        resi_counts={("prot", "A", "94"): 8},
        on_apply=boom,
    )
    _install(monkeypatch, cmd)

    from plugin_side.pymol_tools import mutate_residue

    ok, msg = mutate_residue("prot", "A", "94", "ASN")

    assert ok is True
    assert msg.startswith("[ERROR]")
    assert "wizard boom" in msg
    assert cmd.closed_wizard == 1
