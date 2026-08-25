"""Project-context path constants for the Yosys synthesis flow.

Resolves the repository root and the RTL / synthesis-output directories from
``booley.toml`` (via :mod:`booley.runtime.shared_infra`).  Extracted into its own leaf
module so discovery, parsing, configuration, and script generation share a
single source of truth without importing back into ``syn_core`` (which would
create a circular import).
"""

from __future__ import annotations

import sys
from pathlib import Path

# The synthesis package lives at ``<repo>/src/booley/yosys``. Insert ``src``
# on ``sys.path`` so the ``booley.*`` imports below resolve when
# this module is loaded before the package is otherwise on the path.
SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR.parent.parent))

from booley.runtime.shared_infra import (
    get_rtl_dir,
    get_syn_output_dir,
    resolve_project_root,
)

# Get project root — script lives at .booley/src/yosys/
PROJECT_ROOT = resolve_project_root(SCRIPT_DIR.parent.parent.parent)

# Synthesis output dir — from booley.toml [flows.synth].output_dir
SYN_DIR = get_syn_output_dir(PROJECT_ROOT)

# RTL directory — from booley.toml [sources.rtl].source_dirs
RTL_DIR = get_rtl_dir(PROJECT_ROOT)
