"""Liberty-file discovery for the Yosys Synthesis Flow.

Resolves the Liberty timing library from CLI argument / ``$PRJ_LIB_DIR`` / a
platform default. A pure, side-effect-free leaf module.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Default liberty library path (platform-dependent fallback)
DEFAULT_LIB_DIR = Path("C:/tools") if sys.platform == "win32" else Path("/opt/pdk")
DEFAULT_LIBERTY = DEFAULT_LIB_DIR / "cell" / "lib" / "NangateOpenCellLibrary_typical_ccs.lib"


def resolve_liberty(cli_liberty: str | None = None) -> Path:
    """
    Resolve liberty library path with precedence:
    1. --liberty CLI argument
    2. PRJ_LIB_DIR environment variable
    3. Default path
    """
    if cli_liberty:
        lib = Path(cli_liberty)
        if not lib.exists():
            sys.exit(f"ERROR: Liberty file not found: {lib}")
        return lib

    if env_dir := os.environ.get("PRJ_LIB_DIR"):
        lib = Path(env_dir) / "cell" / "lib" / "NangateOpenCellLibrary_typical_ccs.lib"
        if lib.exists():
            return lib
        print(f"WARNING: PRJ_LIB_DIR set but liberty not found at {lib}, trying default")

    if DEFAULT_LIBERTY.exists():
        return DEFAULT_LIBERTY

    sys.exit(
        f"ERROR: Liberty file not found.\n"
        f"  Tried: {DEFAULT_LIBERTY}\n"
        f"  Set PRJ_LIB_DIR env var or use --liberty flag."
    )


def resolve_liberty_lenient(cli_liberty: str | None = None) -> tuple[Path, bool]:
    """Resolve the liberty path without requiring it to exist here.

    Same precedence as :func:`resolve_liberty`; returns ``(path, found_locally)``
    so diagnostic/configuration callers can warn instead of aborting.
    """
    if cli_liberty:
        lib = Path(cli_liberty)
        return lib, lib.exists()

    if env_dir := os.environ.get("PRJ_LIB_DIR"):
        lib = Path(env_dir) / "cell" / "lib" / "NangateOpenCellLibrary_typical_ccs.lib"
        if lib.exists():
            return lib, True
        if DEFAULT_LIBERTY.exists():
            return DEFAULT_LIBERTY, True
        # Keep the issued PRJ_LIB_DIR-derived path for boundary diagnostics.
        return lib, False

    return DEFAULT_LIBERTY, DEFAULT_LIBERTY.exists()
