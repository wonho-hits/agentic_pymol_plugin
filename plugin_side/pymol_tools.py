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
from pathlib import Path

from .safety import SafetyError, check_code


_EXEC_LOCK = threading.Lock()
_MAX_OUTPUT = 8000

_AA3_CODES = frozenset({
    "ALA", "ARG", "ASN", "ASP", "CYS", "GLN", "GLU", "GLY", "HIS", "ILE",
    "LEU", "LYS", "MET", "PHE", "PRO", "SER", "THR", "TRP", "TYR", "VAL",
})


_ERROR_HINTS = [
    (
        "More than one atom found",
        "Use get_min_distance(sel1, sel2) instead of cmd.get_distance "
        "— it handles multi-atom selections automatically.",
    ),
    (
        "x/y/z only available in iterate_state",
        "Use get_atom_coords(sele) instead — it returns "
        "[(name, elem, resi, resn, chain, x, y, z), ...] without "
        "needing cmd.iterate_state.",
    ),
    (
        "NameError",
        "Variables from Python (including np, math, and your own) are "
        "NOT available inside cmd.iterate/iterate_state expressions. "
        "Use get_atom_coords(sele) to get coordinates as a Python list, "
        "then process with np/math in normal Python code.",
    ),
    (
        "SyntaxError",
        "cmd.iterate expressions must be single Python expressions — "
        "no if/for statements. Use selection filters instead: "
        "cmd.iterate('sele and elem FE', 'print(name)').",
    ),
    (
        "has no attribute 'element'",
        "PyMOL Atom objects use a.symbol for the element (e.g. 'C', 'FE'), "
        "not a.element. Or use the pre-built get_atom_coords(sele) which "
        "already returns (name, elem, resi, resn, chain, x, y, z) tuples.",
    ),
    (
        "unexpected keyword argument",
        "PyMOL's cmd.iterate/iterate_state/alter do not accept extra "
        "keyword arguments like 'readonly'. Use positional args only: "
        "cmd.iterate(sele, expr) or cmd.iterate_state(state, sele, expr). "
        "Better yet, use get_atom_coords(sele) for coordinate access.",
    ),
]


def _error_hint(tb: str) -> str:
    for pattern, hint in _ERROR_HINTS:
        if pattern in tb:
            return hint
    return ""


def _build_namespace() -> dict:
    """Whitelisted globals for agent-generated code."""
    from pymol import cmd, stored  # type: ignore
    import math

    def get_min_distance(sel1: str, sel2: str) -> float:
        """Return the minimum inter-atomic distance between two selections."""
        m1, m2 = cmd.get_model(sel1), cmd.get_model(sel2)
        if not m1.atom or not m2.atom:
            raise ValueError(f"empty selection: sel1={len(m1.atom)} sel2={len(m2.atom)} atoms")
        return min(
            sum((c1 - c2) ** 2 for c1, c2 in zip(a1.coord, a2.coord)) ** 0.5
            for a1 in m1.atom
            for a2 in m2.atom
        )

    def get_atom_coords(sele: str) -> list[tuple]:
        """Return [(name, elem, resi, resn, chain, x, y, z), ...] for *sele*."""
        return [
            (a.name, a.symbol, a.resi, a.resn, a.chain, *a.coord)
            for a in cmd.get_model(sele).atom
        ]

    _COORD_VARS = {"x", "y", "z", "coord", "np.array", "element"}
    _real_iterate = cmd.iterate
    _real_iterate_state = cmd.iterate_state

    def _safe_iterate(sele, expr, *args, **kwargs):
        if any(v in expr for v in _COORD_VARS):
            raise RuntimeError(
                f"iterate expression references coordinate/element variables "
                f"({', '.join(v for v in _COORD_VARS if v in expr)}). "
                f"Use get_atom_coords(sele) instead — it returns "
                f"(name, elem, resi, resn, chain, x, y, z) tuples."
            )
        return _real_iterate(sele, expr, *args, **kwargs)

    def _safe_iterate_state(state, sele, expr, *args, **kwargs):
        if any(v in expr for v in {"np.array", "element"}):
            raise RuntimeError(
                f"iterate_state expression references unavailable variables. "
                f"Use get_atom_coords(sele) instead."
            )
        return _real_iterate_state(state, sele, expr, *args, **kwargs)

    # Wrap cmd so agent code uses safe versions transparently
    import types
    safe_cmd = types.SimpleNamespace(**{k: getattr(cmd, k) for k in dir(cmd) if not k.startswith('_')})
    safe_cmd.iterate = _safe_iterate
    safe_cmd.iterate_state = _safe_iterate_state

    ns: dict = {
        "cmd": safe_cmd,
        "stored": stored,
        "math": math,
        "get_min_distance": get_min_distance,
        "get_atom_coords": get_atom_coords,
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
            hint = _error_hint(tb)
            output = (
                "[ERROR] code raised an exception:\n"
                + (captured + "\n" if captured else "")
                + tb
                + (f"\n[HINT] {hint}" if hint else "")
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


def mutate_residue(
    obj: str, chain: str, resi: str, target_aa: str
) -> tuple[bool, str]:
    """Mutate a single residue via PyMOL's mutagenesis wizard.

    Drives the wizard via the selection-macro form ``/<obj>//<chain>/<resi>/``
    which avoids the "object not found" failure mode where the wizard
    silently deletes the whole object when fed a bare
    ``object and resi N`` expression. If ``chain`` is empty and the resi
    is unambiguous across chains, the chain is auto-detected.

    Returns ``(True, "[OK] mutated ...")`` on success. All other failures
    surface as ``(True, "[ERROR] ...")`` so the agent can react via its
    existing ``[ERROR]`` retry rule. The wizard is always closed via
    ``cmd.set_wizard()`` and the ``_pk*`` pick selections it leaves
    behind are cleaned up.
    """
    try:
        from pymol import cmd  # type: ignore
    except Exception as exc:
        return False, f"[ERROR] pymol unavailable: {exc}"

    obj = str(obj).strip()
    chain = str(chain).strip()
    resi = str(resi).strip()
    target_aa = str(target_aa).strip().upper()

    if not obj:
        return True, "[ERROR] obj is required"
    if not resi:
        return True, "[ERROR] resi is required"
    if target_aa not in _AA3_CODES:
        return True, (
            f"[ERROR] target_aa must be a 3-letter amino acid code "
            f"(e.g. 'ASN', 'ALA'), got {target_aa!r}"
        )

    buf = io.StringIO()
    with _EXEC_LOCK:
        try:
            objects = list(cmd.get_object_list() or [])
        except Exception as exc:
            return True, f"[ERROR] cmd.get_object_list failed: {exc}"
        if obj not in objects:
            return True, (
                f"[ERROR] object {obj!r} not found; loaded objects: {objects}"
            )

        if not chain:
            try:
                all_chains = sorted(set(cmd.get_chains(obj) or []))
            except Exception as exc:
                return True, f"[ERROR] cmd.get_chains failed: {exc}"
            matching: list[str] = []
            for c in all_chains:
                try:
                    n = int(cmd.count_atoms(f"{obj} and chain {c} and resi {resi}"))
                except Exception:
                    n = 0
                if n > 0:
                    matching.append(c)
            if not matching:
                return True, (
                    f"[ERROR] resi {resi} not found in any chain of {obj}; "
                    f"chains present: {all_chains}"
                )
            if len(matching) > 1:
                return True, (
                    f"[ERROR] resi {resi} appears in multiple chains of {obj}: "
                    f"{matching}. Pass the chain explicitly."
                )
            chain = matching[0]
        else:
            try:
                n_atoms = int(cmd.count_atoms(f"{obj} and chain {chain} and resi {resi}"))
            except Exception as exc:
                return True, f"[ERROR] residue lookup failed: {exc}"
            if n_atoms == 0:
                return True, (
                    f"[ERROR] no atoms in {obj} chain {chain} resi {resi} — "
                    f"confirm chain/resi via inspect_session()"
                )

        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                cmd.wizard("mutagenesis")
                cmd.refresh_wizard()
                cmd.get_wizard().set_mode(target_aa)
                cmd.get_wizard().do_select(f"/{obj}//{chain}/{resi}/")
                cmd.get_wizard().apply()
        except Exception:
            tb = traceback.format_exc()
            wizard_out = buf.getvalue()
            _close_wizard_and_cleanup(cmd)
            return True, (
                f"[ERROR] mutagenesis wizard raised:\n"
                f"{wizard_out}\n{tb}"
            )

        _close_wizard_and_cleanup(cmd)

        try:
            post_objects = list(cmd.get_object_list() or [])
        except Exception:
            post_objects = []
        if obj not in post_objects:
            try:
                cmd.undo()
            except Exception:
                pass
            return True, (
                f"[ERROR] mutagenesis deleted {obj!r}; attempted cmd.undo(). "
                f"Wizard output:\n{buf.getvalue()}"
            )

    stdout = buf.getvalue().strip()
    suffix = f"\n{stdout}" if stdout else ""
    return True, f"[OK] mutated {obj}/{chain}/{resi} → {target_aa}{suffix}"


def _close_wizard_and_cleanup(cmd) -> None:
    """Close the wizard and delete its leftover pick selections.

    Runs inside the ``_EXEC_LOCK`` guard; each step is wrapped so one
    failure does not leave the wizard half-closed.
    """
    try:
        cmd.set_wizard()
    except Exception:
        pass
    for sel in ("_pkbase1", "_pkbase2", "_pkfrag1", "_pkfrag2"):
        try:
            cmd.delete(sel)
        except Exception:
            pass


_PASTEL_COLORS = {
    "pastel_blue":     [0.68, 0.78, 0.90],
    "pastel_pink":     [0.95, 0.75, 0.78],
    "pastel_green":    [0.70, 0.88, 0.72],
    "pastel_lavender": [0.82, 0.75, 0.92],
    "pastel_peach":    [0.95, 0.82, 0.68],
    "pastel_mint":     [0.72, 0.90, 0.85],
    "pastel_yellow":   [0.98, 0.92, 0.65],
    "pastel_coral":    [0.95, 0.72, 0.65],
    "pastel_cyan":     [0.72, 0.88, 0.92],
    "pastel_lilac":    [0.88, 0.75, 0.88],
}

_CHAIN_COLORS = [
    "gray80",
    "pastel_blue",
    "pastel_pink",
    "pastel_green",
    "pastel_lavender",
    "pastel_peach",
    "pastel_mint",
    "pastel_yellow",
    "pastel_coral",
    "pastel_cyan",
    "pastel_lilac",
]


def pretty(selection: str = "all") -> tuple[bool, str]:
    """Apply the standard pastel visualization style.

    Polymer → cartoon with one pastel color per chain (cnc to keep
    element coloring on heteroatoms). Organic/ligands → sticks in
    pastel_coral + cnc. Rendering settings: depth_cue off,
    cartoon_fancy_helices on, cartoon_side_chain_helper on.
    """
    try:
        from pymol import cmd  # type: ignore
    except Exception as exc:
        return True, f"[ERROR] pymol unavailable: {exc}"

    sel = str(selection).strip() or "all"

    buf = io.StringIO()
    with _EXEC_LOCK:
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                for name, rgb in _PASTEL_COLORS.items():
                    cmd.set_color(name, rgb)

                cmd.show("cartoon", f"polymer and {sel}")
                cmd.hide("lines", f"polymer and {sel}")
                cmd.show("sticks", f"organic and {sel}")

                objects = cmd.get_object_list(f"polymer and {sel}") or []
                color_idx = 0
                for obj in objects:
                    chains = cmd.get_chains(f"polymer and {obj} and {sel}") or []
                    if len(chains) <= 1:
                        c = _CHAIN_COLORS[color_idx % len(_CHAIN_COLORS)]
                        cmd.color(c, f"polymer and {obj}")
                        cmd.util.cnc(f"polymer and {obj}")
                        color_idx += 1
                    else:
                        for chain in chains:
                            c = _CHAIN_COLORS[color_idx % len(_CHAIN_COLORS)]
                            cmd.color(c, f"polymer and {obj} and chain {chain}")
                            cmd.util.cnc(f"polymer and {obj} and chain {chain}")
                            color_idx += 1

                cmd.color("pastel_coral", f"organic and {sel}")
                cmd.util.cnc(f"organic and {sel}")

                cmd.set("depth_cue", 0)
                cmd.set("ray_shadows", "off")
                cmd.set("cartoon_fancy_helices", "on")
                cmd.set("cartoon_side_chain_helper", "on")

                cmd.orient(sel)
        except Exception:
            tb = traceback.format_exc()
            return True, f"[ERROR] pretty failed:\n{buf.getvalue()}\n{tb}"

    stdout = buf.getvalue().strip()
    n_obj = len(objects) if 'objects' in dir() else 0
    return True, f"[OK] applied pastel styling to {n_obj} object(s){': ' + stdout if stdout else ''}"


def capture_viewport(
    width: int = 800, height: int = 600
) -> tuple[bool, str]:
    """Save the current PyMOL viewport as a PNG and return the file path.

    The image is written to a fixed temp path so the agent-side
    ``describe_viewport`` tool can read it for vision analysis.
    """
    try:
        from pymol import cmd  # type: ignore
    except Exception as exc:
        return True, f"[ERROR] pymol unavailable: {exc}"

    import tempfile
    path = str(Path(tempfile.gettempdir()) / "pymol_viewport.png")

    with _EXEC_LOCK:
        try:
            cmd.png(path, width=int(width), height=int(height), ray=0, quiet=1)
        except Exception:
            tb = traceback.format_exc()
            return True, f"[ERROR] cmd.png failed:\n{tb}"

    if not Path(path).exists():
        return True, f"[ERROR] screenshot was not written to {path}"
    return True, path


def save_structure(
    selection: str, filename: str, format: str = ""
) -> tuple[bool, str]:
    """Save a PyMOL selection to a file in a writable directory.

    The file is saved to ``~/Desktop/<filename>`` by default. If
    ``filename`` already contains a path separator it is used as-is.
    ``format`` is auto-detected from the extension if omitted.
    """
    try:
        from pymol import cmd  # type: ignore
    except Exception as exc:
        return True, f"[ERROR] pymol unavailable: {exc}"

    import os

    selection = str(selection).strip()
    filename = str(filename).strip()
    if not selection:
        return True, "[ERROR] selection is required"
    if not filename:
        return True, "[ERROR] filename is required (e.g. 'complex.pdb')"

    if os.sep not in filename and "/" not in filename:
        save_dir = os.path.expanduser("~/Desktop")
        os.makedirs(save_dir, exist_ok=True)
        filepath = os.path.join(save_dir, filename)
    else:
        filepath = os.path.expanduser(filename)

    with _EXEC_LOCK:
        try:
            n = int(cmd.count_atoms(selection))
        except Exception as exc:
            return True, f"[ERROR] selection error: {exc}"
        if n == 0:
            return True, f"[ERROR] selection '{selection}' matched 0 atoms"

        try:
            if format:
                cmd.save(filepath, selection, format=format)
            else:
                cmd.save(filepath, selection)
        except Exception as exc:
            return True, f"[ERROR] cmd.save failed: {exc}"

    return True, f"[OK] saved {n} atoms to {filepath}"


_PYMOL_BOND_ORDER = {1: 1, 2: 2, 3: 3, 12: 4}  # RDKit int -> PyMOL order


def assign_bond_orders(selection: str, smiles: str) -> tuple[bool, str]:
    """Rewrite bonds in a PyMOL selection to match a reference SMILES.

    PyMOL loads PDB/CIF ligands with all single bonds. This function
    uses RDKit's ``AssignBondOrdersFromTemplate`` to transfer the real
    bond orders (single/double/aromatic) from a SMILES onto the 3D
    structure, then rewrites every heavy-heavy bond in PyMOL via
    ``cmd.unbond`` + ``cmd.bond``.

    The SMILES must describe the same molecule as the selection (same
    heavy-atom connectivity); hydrogen count differences are fine.
    """
    try:
        from pymol import cmd  # type: ignore
    except Exception as exc:
        return True, f"[ERROR] pymol unavailable: {exc}"

    try:
        from rdkit import Chem
        from rdkit.Chem import AllChem
    except ImportError:
        return True, (
            "[ERROR] rdkit is not installed in the PyMOL Python environment."
        )

    selection = str(selection).strip()
    smiles = str(smiles).strip()
    if not selection:
        return True, "[ERROR] selection is required"
    if not smiles:
        return True, "[ERROR] smiles is required"

    with _EXEC_LOCK:
        try:
            pdb_block = cmd.get_pdbstr(selection)
        except Exception as exc:
            return True, f"[ERROR] cmd.get_pdbstr failed: {exc}"

        raw = Chem.MolFromPDBBlock(pdb_block, removeHs=True, sanitize=False)
        if raw is None:
            return True, f"[ERROR] RDKit could not parse selection '{selection}'"

        template = Chem.MolFromSmiles(smiles)
        if template is None:
            return True, f"[ERROR] invalid SMILES: {smiles}"

        try:
            fixed = AllChem.AssignBondOrdersFromTemplate(template, raw)
        except Exception as exc:
            return True, (
                f"[ERROR] bond order assignment failed: {exc}. "
                f"SMILES and selection must have the same heavy-atom connectivity."
            )

        model = cmd.get_model(f"({selection}) and not hydro")
        pymol_indices = [a.index for a in model.atom]
        if len(pymol_indices) != fixed.GetNumAtoms():
            return True, (
                f"[ERROR] heavy-atom count mismatch: PyMOL {len(pymol_indices)} "
                f"vs RDKit {fixed.GetNumAtoms()}"
            )

        obj_names = cmd.get_object_list(selection)
        if len(obj_names) != 1:
            return True, (
                f"[ERROR] selection must be in one object, got {obj_names}"
            )
        obj = obj_names[0]

        n_changed = 0
        for b in fixed.GetBonds():
            i, j = b.GetBeginAtomIdx(), b.GetEndAtomIdx()
            order = _PYMOL_BOND_ORDER.get(int(b.GetBondTypeAsDouble()), 1)
            if b.GetIsAromatic():
                order = 4
            a1 = f"{obj} and index {pymol_indices[i]}"
            a2 = f"{obj} and index {pymol_indices[j]}"
            cmd.unbond(a1, a2)
            cmd.bond(a1, a2, order=order)
            n_changed += 1

    return True, f"[OK] rewrote {n_changed} bonds on {obj} from SMILES"


def align_to_core(
    probe: str, ref: str, core_smarts: str
) -> tuple[bool, str]:
    """Align a probe molecule/selection onto a reference using an RDKit
    SMARTS core.

    Both ``probe`` and ``ref`` are PyMOL selections (e.g. an object name,
    ``"obj and resn UNK"``, or a named selection). The function:

    1. Exports each selection to a temporary SDF.
    2. Reads them into RDKit.
    3. Finds all substructure matches for the core SMARTS and picks the
       atom mapping with the lowest RMSD (handles symmetry).
    4. Loads the aligned probe back into PyMOL as ``<original>_aligned``.

    Returns ``(True, "[OK] ...")`` with the core RMSD and the new object
    name, or ``(True, "[ERROR] ...")`` on failure.
    """
    try:
        from pymol import cmd  # type: ignore
    except Exception as exc:
        return True, f"[ERROR] pymol unavailable: {exc}"

    try:
        from rdkit import Chem
        from rdkit.Chem import rdMolAlign
    except ImportError:
        return True, (
            "[ERROR] rdkit is not installed in the PyMOL Python environment. "
            "Install with: <pymol-python> -m pip install rdkit"
        )

    import os
    import tempfile

    probe = str(probe).strip()
    ref = str(ref).strip()
    core_smarts = str(core_smarts).strip()

    if not probe or not ref:
        return True, "[ERROR] both probe and ref selections are required"
    if not core_smarts:
        return True, "[ERROR] core_smarts is required"

    tmp = tempfile.gettempdir()
    probe_path = os.path.join(tmp, "_align_probe.sdf")
    ref_path = os.path.join(tmp, "_align_ref.sdf")
    aligned_path = os.path.join(tmp, "_align_result.sdf")

    with _EXEC_LOCK:
        try:
            n_probe = int(cmd.count_atoms(probe))
            n_ref = int(cmd.count_atoms(ref))
        except Exception as exc:
            return True, f"[ERROR] selection error: {exc}"
        if n_probe == 0:
            return True, f"[ERROR] probe selection '{probe}' matched 0 atoms"
        if n_ref == 0:
            return True, f"[ERROR] ref selection '{ref}' matched 0 atoms"

        try:
            cmd.save(probe_path, probe)
            cmd.save(ref_path, ref)
        except Exception as exc:
            return True, f"[ERROR] failed to export selections: {exc}"

    probe_mol = Chem.MolFromMolFile(probe_path, removeHs=False)
    ref_mol = Chem.MolFromMolFile(ref_path, removeHs=False)
    if probe_mol is None:
        return True, f"[ERROR] RDKit could not parse probe from '{probe}'"
    if ref_mol is None:
        return True, f"[ERROR] RDKit could not parse ref from '{ref}'"

    core = Chem.MolFromSmarts(core_smarts)
    if core is None:
        return True, f"[ERROR] invalid SMARTS: '{core_smarts}'"

    p_matches = probe_mol.GetSubstructMatches(core, uniquify=False)
    r_matches = ref_mol.GetSubstructMatches(core, uniquify=False)
    if not p_matches:
        return True, f"[ERROR] core SMARTS not found in probe ({n_probe} atoms)"
    if not r_matches:
        return True, f"[ERROR] core SMARTS not found in ref ({n_ref} atoms)"

    best_rmsd, best_map = float("inf"), None
    for pm in p_matches:
        for rm in r_matches:
            atom_map = list(zip(pm, rm))
            trial = Chem.Mol(probe_mol)
            rmsd = rdMolAlign.AlignMol(trial, ref_mol, atomMap=atom_map)
            if rmsd < best_rmsd:
                best_rmsd, best_map = rmsd, atom_map

    rmsd = rdMolAlign.AlignMol(probe_mol, ref_mol, atomMap=best_map)
    Chem.MolToMolFile(probe_mol, aligned_path)

    probe_obj = probe.split()[0]
    aligned_name = f"{probe_obj}_aligned"

    with _EXEC_LOCK:
        try:
            cmd.load(aligned_path, aligned_name)
        except Exception as exc:
            return True, f"[ERROR] failed to load aligned result: {exc}"

    return True, (
        f"[OK] aligned '{probe}' → '{ref}' on core '{core_smarts}'\n"
        f"core RMSD = {rmsd:.3f} Å, result object: {aligned_name}\n"
        f"atom map (probe→ref): {best_map}"
    )


TOOL_HANDLERS = {
    "run_pymol_python": lambda args: run_pymol_python(str(args.get("code", ""))),
    "inspect_session": lambda args: inspect_session(),
    "mutate_residue": lambda args: mutate_residue(
        obj=str(args.get("obj", "")),
        chain=str(args.get("chain", "")),
        resi=str(args.get("resi", "")),
        target_aa=str(args.get("target_aa", "")),
    ),
    "pretty": lambda args: pretty(selection=str(args.get("selection", "all"))),
    "save_structure": lambda args: save_structure(
        selection=str(args.get("selection", "")),
        filename=str(args.get("filename", "")),
        format=str(args.get("format", "")),
    ),
    "assign_bond_orders": lambda args: assign_bond_orders(
        selection=str(args.get("selection", "")),
        smiles=str(args.get("smiles", "")),
    ),
    "align_to_core": lambda args: align_to_core(
        probe=str(args.get("probe", "")),
        ref=str(args.get("ref", "")),
        core_smarts=str(args.get("core_smarts", "")),
    ),
    "capture_viewport": lambda args: capture_viewport(
        width=int(args.get("width", 800)),
        height=int(args.get("height", 600)),
    ),
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
