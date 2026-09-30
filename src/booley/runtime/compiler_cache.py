"""Fixed compiler-cache identity within the authorized Project-data mount.

The Sandbox renderer, Session Issuance, the legacy-tree copier, and the
Simulation Flow all derive cache locations from this module, so the layout is
spelled once.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from booley.runtime.sandbox_layout import PROJECT_DIR_TARGET

COMPILER_CACHE_ROOT_ENV = "BOOLEY_COMPILER_CACHE_ROOT"
# Directory names below the Project-data owner, outermost first.
COMPILER_CACHE_SEGMENTS = (".runtime", "compiler-cache", "ccache")
COMPILER_CACHE_RELATIVE = "/".join(COMPILER_CACHE_SEGMENTS)
# The only root Session Issuance hands to a Sandbox.
ISSUED_COMPILER_CACHE_ROOT = f"{PROJECT_DIR_TARGET}/{COMPILER_CACHE_RELATIVE}"


@dataclass(frozen=True)
class IssuedCacheIdentity:
    """What the current process was issued, read once at the process boundary."""

    root: str | None
    """``BOOLEY_COMPILER_CACHE_ROOT`` when issued, else ``None``."""
    in_sandbox: bool
    """Running inside an issued Sandbox (container flag and Project mount present)."""


def read_issued_identity(environ: Mapping[str, str] = os.environ) -> IssuedCacheIdentity:
    """Read the issued compiler-cache identity from *environ* and the Sandbox layout."""
    return IssuedCacheIdentity(
        root=environ.get(COMPILER_CACHE_ROOT_ENV) or None,
        in_sandbox=environ.get("BOOLEY_CONTAINER") == "1" and Path(PROJECT_DIR_TARGET).is_dir(),
    )
