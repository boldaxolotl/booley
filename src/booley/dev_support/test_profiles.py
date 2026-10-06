"""Standard-library-only prerequisites for source test profiles.

Native source tests deliberately use the current checkout's debug build, not
installed binaries, release builds, or runtime overrides.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

BWAVE_BUILD = (
    "cargo",
    "build",
    "--locked",
    "--manifest-path",
    "crates/bwave/Cargo.toml",
    "--target-dir",
    "crates/bwave/target",
)


def native_test_binary(root: Path) -> Path:
    """Return the one binary selected by all native source tests."""
    suffix = ".exe" if sys.platform == "win32" else ""
    return root.resolve() / "crates" / "bwave" / "target" / "debug" / f"bwave{suffix}"


def native_test_problem(root: Path) -> str | None:
    """Probe a prebuilt binary without building, chmodding, or using a shell."""
    binary = native_test_binary(root)
    try:
        if not binary.is_file():
            return "native B-Wave debug binary is missing"
        if os.name != "nt" and not os.access(binary, os.X_OK):
            return "native B-Wave debug binary is not executable"
        with binary.open("rb") as stream:
            head = stream.read(512)
        if head.startswith(b"#!") or b"booley.bwave.cli" in head:
            return "native B-Wave debug path contains a wrapper"
        result = subprocess.run(
            [str(binary), "--version"],
            cwd=root.resolve(),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return "native B-Wave debug version probe failed"
    if result.returncode != 0 or not re.fullmatch(r"bwave \S+", result.stdout.strip()):
        return "native B-Wave debug version probe failed"
    return None
