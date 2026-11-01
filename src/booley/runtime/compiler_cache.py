"""Fixed compiler-cache location that Session Issuance and the Sandbox renderer stamp."""

from __future__ import annotations

from booley.runtime.sandbox_layout import BOOLEY_DIR_TARGET

COMPILER_CACHE_ROOT_ENV = "BOOLEY_COMPILER_CACHE_ROOT"
# Cache directory below the Booley Directory mount.
COMPILER_CACHE_RELATIVE = ".runtime/compiler-cache/ccache"
# The only root Session Issuance hands to a Sandbox.
ISSUED_COMPILER_CACHE_ROOT = f"{BOOLEY_DIR_TARGET}/{COMPILER_CACHE_RELATIVE}"
