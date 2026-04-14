"""Plugin-side test bootstrap.

The plugin's top-level ``__init__.py`` imports ``pymol``, which is only
available inside PyMOL itself. Tests should import ``plugin_side.*``
directly, so we put the plugin root on ``sys.path`` and rely on pytest's
rootdir-based module discovery (no ``tests/__init__.py``).
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
