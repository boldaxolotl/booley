"""A fake Xilinx release whose Vivado launcher answers the version probe."""

from __future__ import annotations

import platform
import sys
from pathlib import Path

import pytest

from tests.path_tool_fake import PathToolFake

LINUX_X86_64 = sys.platform.startswith("linux") and platform.machine().lower() in {
    "x86_64",
    "amd64",
}

requires_linux_x86_64 = pytest.mark.skipif(
    not LINUX_X86_64,
    reason="host-provisioned Vivado supports Linux x86-64 only, and the fake launcher is a POSIX script",
)


def fake_vivado_release(root: Path, version: str = "2025.2") -> Path:
    """Create a release root whose ``Vivado/bin/vivado -version`` reports *version*."""
    (root / "tps").mkdir(parents=True)
    bin_dir = root / "Vivado" / "bin"
    bin_dir.mkdir(parents=True)
    PathToolFake(bin_dir, "vivado").answer("-version", stdout=f"Vivado v{version} (64-bit)\n")
    return root
