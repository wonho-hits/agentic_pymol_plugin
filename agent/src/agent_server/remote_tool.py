"""RPC proxy for ``run_pymol_python`` and local agent-side tools.

The real PyMOL execution happens inside the plugin process. This module
exposes LangChain-compatible tools that, when the agent calls them,
forward the invocation to the plugin over ndjson and block until the
plugin replies with a ``tool_result``. ``describe_viewport`` is a
*local* tool that chains a remote screenshot capture with a Gemini
vision call — all from the agent subprocess.
"""
from __future__ import annotations

import base64
import logging
import os
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langchain_google_genai import ChatGoogleGenerativeAI

log = logging.getLogger(__name__)

_WIKI_MAX_CHARS = 4000


class _WikiTextExtractor:
    """Extract readable text from the ``#mw-content-text`` div of a
    MediaWiki page using only the stdlib ``html.parser``."""

    def __init__(self) -> None:
        from html.parser import HTMLParser

        class _Parser(HTMLParser):
            def __init__(self_inner) -> None:
                super().__init__()
                self_inner.in_content = False
                self_inner.skip = False
                self_inner.texts: list[str] = []
                self_inner._skip_tags = {"script", "style", "nav", "footer", "header"}

            def handle_starttag(self_inner, tag, attrs):
                if dict(attrs).get("id") == "mw-content-text":
                    self_inner.in_content = True
                if tag in self_inner._skip_tags:
                    self_inner.skip = True

            def handle_endtag(self_inner, tag):
                if tag in self_inner._skip_tags:
                    self_inner.skip = False

            def handle_data(self_inner, data):
                if self_inner.in_content and not self_inner.skip:
                    t = data.strip()
                    if t:
                        self_inner.texts.append(t)

        self._parser_cls = _Parser

    def extract(self, html: str) -> str:
        p = self._parser_cls()
        p.feed(html)
        return "\n".join(p.texts)


_wiki_extractor = _WikiTextExtractor()


def _fetch_pymol_wiki(command: str) -> str:
    """Fetch and extract text from a PyMOL Wiki page."""
    import urllib.request
    import urllib.error

    command = command.strip().capitalize()
    url = f"https://pymolwiki.org/index.php/{command}"
    req = urllib.request.Request(url, headers={"User-Agent": "AgenticPyMOL/1.0"})
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        html = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return f"[ERROR] {url} returned HTTP {exc.code}"
    except Exception as exc:
        return f"[ERROR] failed to fetch {url}: {exc}"

    text = _wiki_extractor.extract(html)
    if not text:
        return f"[ERROR] no content found at {url}"
    if len(text) > _WIKI_MAX_CHARS:
        text = text[:_WIKI_MAX_CHARS] + f"\n…(truncated, {len(text) - _WIKI_MAX_CHARS} more chars)"
    return text


ToolSender = Callable[[str, str, dict], None]
"""Callable injected by the server: ``sender(call_id, tool_name, args) -> None``."""


@dataclass
class _PendingCall:
    event: threading.Event = field(default_factory=threading.Event)
    result: str = ""
    ok: bool = True


class RemoteToolBridge:
    """Wire a single request's tool calls to/from the plugin.

    One bridge instance is created per request. The server calls
    :meth:`deliver_result` when a ``tool_result`` message arrives from
    the plugin; the tool function calls :meth:`_await` to block until
    the matching result lands.
    """

    def __init__(self, send_tool_call: ToolSender, timeout: float = 120.0) -> None:
        self._send = send_tool_call
        self._timeout = timeout
        self._pending: dict[str, _PendingCall] = {}
        self._lock = threading.Lock()
        self._cancelled = False

    # ---- server-facing API -------------------------------------------------

    def deliver_result(self, call_id: str, ok: bool, result: str) -> None:
        with self._lock:
            pending = self._pending.get(call_id)
        if pending is None:
            return
        pending.ok = ok
        pending.result = result
        pending.event.set()

    def reset(self, send_tool_call: ToolSender) -> None:
        """Prepare for a new request — clear pending state, set new sender."""
        with self._lock:
            self._send = send_tool_call
            self._pending.clear()
            self._cancelled = False

    def cancel(self) -> None:
        """Unblock any in-flight tool calls with a cancellation marker."""
        with self._lock:
            self._cancelled = True
            pendings = list(self._pending.values())
        for pending in pendings:
            pending.ok = False
            pending.result = "[CANCELLED] request was cancelled by the plugin"
            pending.event.set()

    # ---- tool-facing API ---------------------------------------------------

    def build_tools(self) -> list[Any]:
        """Return all LangChain tools bound to this bridge."""
        bridge = self

        @tool
        def run_pymol_python(code: str) -> str:
            """Execute Python code inside the running PyMOL session.

            Available names: ``cmd`` (pymol.cmd), ``stored`` (pymol.stored),
            ``math``, ``np`` (numpy if installed), plus two helpers:
            ``get_min_distance(sel1, sel2)`` (minimum distance, handles
            multi-atom selections) and ``get_atom_coords(sele)`` (returns
            list of (name, elem, resi, resn, chain, x, y, z) tuples).

            Imports of os / subprocess / shutil / sys / socket / urllib /
            requests are blocked. ``cmd.reinitialize()``,
            ``cmd.delete('all')``, ``cmd.quit()`` are blocked.
            ``cmd.fetch`` and other normal PyMOL commands are allowed.

            Use ``print(...)`` to surface values — the tool returns captured stdout.
            Run small verifiable chunks; inspect results before continuing.
            """
            return bridge._call("run_pymol_python", {"code": code})

        @tool
        def inspect_session() -> str:
            """Return a JSON snapshot of the current PyMOL session.

            The snapshot lists every loaded object with its atom count,
            chain list, and detected non-solvent HETATM (ligand candidate)
            groups, plus all user-created selections with their atom counts.

            Call this whenever you would otherwise write a probe like
            ``print(cmd.get_object_list())`` or ``print(cmd.get_chains(...))``
            — it is faster and gives you a parseable result. Does not
            modify the session.
            """
            return bridge._call("inspect_session", {})

        @tool
        def mutate_residue(obj: str, chain: str, resi: str, target_aa: str) -> str:
            """Mutate a single residue in a loaded object.

            Args:
                obj: object name (e.g. ``"boltz2_cyclase_wt"``).
                chain: chain identifier (e.g. ``"A"``). Pass an empty
                    string to auto-detect when the residue number is
                    unambiguous across chains.
                resi: residue number as a string (e.g. ``"94"``).
                target_aa: target amino acid, 3-letter UPPERCASE code
                    (``"ASN"``, ``"ALA"``, ``"LYS"``, ...).

            Use this instead of driving the mutagenesis wizard via
            ``run_pymol_python``. The wizard's ``apply()`` can silently
            delete the whole object when called with a bare
            ``"object and resi N"`` selection; this tool uses the
            selection-macro form, closes the wizard cleanly, cleans up
            the ``_pk*`` leftover selections, and rolls back via
            ``cmd.undo()`` if the object disappears.

            Returns ``"[OK] mutated <obj>/<chain>/<resi> → <target>"`` on
            success. Any string starting with ``[ERROR]`` means the
            mutation did not happen — surface that to the user verbatim.
            """
            return bridge._call(
                "mutate_residue",
                {"obj": obj, "chain": chain, "resi": resi, "target_aa": target_aa},
            )

        @tool
        def pretty(selection: str = "all") -> str:
            """Apply the standard pastel visualization style.

            Args:
                selection: PyMOL selection expression to style.
                    Defaults to ``"all"``.

            Applies: cartoon for polymer (one pastel color per chain,
            cnc for element coloring), sticks for organic/ligands in
            pastel_coral, depth_cue off, cartoon_fancy_helices on, and
            orients the camera. Call this for "show nicely", "enhance
            visualization", "make it pretty", or after loading a new
            structure.
            """
            return bridge._call("pretty", {"selection": selection})

        @tool
        def describe_viewport() -> str:
            """Capture a screenshot of the current PyMOL viewport and return
            a natural-language description of what is visible.

            The description covers: loaded objects, representation style
            (cartoon, sticks, surface, ...), coloring scheme, highlighted
            selections or labels, and the general camera orientation.

            Use this when you need to verify that a visualization looks
            correct, or when the user asks about what they see on screen.
            No arguments needed — the tool captures the live viewport
            automatically.
            """
            path = bridge._call("capture_viewport", {})
            if path.startswith("["):
                return path

            try:
                img_bytes = Path(path).read_bytes()
            except Exception as exc:
                return f"[ERROR] failed to read screenshot at {path}: {exc}"

            b64 = base64.b64encode(img_bytes).decode("ascii")
            model_name = os.environ.get(
                "AGENTIC_PYMOL_MODEL", "gemini-3-flash-preview"
            )
            try:
                vision_llm = ChatGoogleGenerativeAI(
                    model=model_name, temperature=0.2,
                )
                msg = HumanMessage(content=[
                    {
                        "type": "text",
                        "text": (
                            "Describe this PyMOL molecular visualization in "
                            "2-4 concise sentences. Cover: visible objects and "
                            "their representation (cartoon, sticks, surface, etc.), "
                            "coloring, any highlighted selections or labels, "
                            "and camera orientation / zoom level."
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    },
                ])
                response = vision_llm.invoke([msg])
                content = response.content
                if isinstance(content, list):
                    parts = [
                        p["text"] for p in content
                        if isinstance(p, dict) and p.get("type") == "text"
                    ]
                    return "\n".join(parts) if parts else str(content)
                return str(content)
            except Exception as exc:
                log.exception("vision call failed")
                return f"[ERROR] vision model call failed: {exc}"

        @tool
        def lookup_pymol_docs(command: str) -> str:
            """Look up a PyMOL command on the PyMOL Wiki and return its
            documentation as plain text.

            Args:
                command: PyMOL command or function name, e.g.
                    ``"iterate"``, ``"get_distance"``, ``"select"``,
                    ``"align"``, ``"pseudoatom"``.

            Call this **before** writing code when you are unsure about
            a command's exact syntax, accepted arguments, or
            limitations. One wiki lookup is far cheaper than an error →
            retry cycle. The result is truncated to ~4 000 characters.
            """
            return _fetch_pymol_wiki(command)

        return [run_pymol_python, inspect_session, mutate_residue, pretty,
                describe_viewport, lookup_pymol_docs]

    def build_tool(self) -> Any:  # pragma: no cover — kept for callers that only want the primary tool
        """Legacy alias: returns just ``run_pymol_python``."""
        return self.build_tools()[0]

    # ---- internals ---------------------------------------------------------

    def _call(self, name: str, args: dict) -> str:
        import time as _time

        args_summary = str(args)[:120] if args else ""
        log.info("→ %s(%s)", name, args_summary)
        t0 = _time.monotonic()

        call_id = uuid.uuid4().hex[:12]
        pending = _PendingCall()
        with self._lock:
            if self._cancelled:
                return "[CANCELLED] request was cancelled before tool call"
            self._pending[call_id] = pending

        try:
            self._send(call_id, name, args)
        except Exception as exc:
            with self._lock:
                self._pending.pop(call_id, None)
            return f"[TOOL-BRIDGE-ERROR] failed to send tool call: {exc}"

        if not pending.event.wait(self._timeout):
            with self._lock:
                self._pending.pop(call_id, None)
            log.info("← %s TIMEOUT (%.1fs)", name, _time.monotonic() - t0)
            return f"[TOOL-BRIDGE-TIMEOUT] no result in {self._timeout:.0f}s"

        with self._lock:
            self._pending.pop(call_id, None)

        elapsed = _time.monotonic() - t0
        status = "OK" if pending.ok else "FAIL"
        log.info("← %s %s (%d chars, %.1fs)", name, status, len(pending.result), elapsed)

        if not pending.ok:
            return pending.result or "[TOOL-BRIDGE-ERROR] plugin reported failure"
        return pending.result
