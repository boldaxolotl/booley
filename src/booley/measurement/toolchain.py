"""The toolchain fingerprint recorded with every Measurement Run (ADR 0092, #3045).

It says which Sandbox image, Booley build and EDA tool versions produced a run.
It is recorded, never keyed: a differing fingerprint does not change the
Measurement Key. It is collected once per Measurement Run, never per Flow.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from booley.measurement.environment import SANDBOX_IMAGE_ID_ENV
from booley.runtime.execution_identity import (
    IdentityError,
    booley_build_identity,
    booley_source_identity,
)

# (executable, version arguments, timeout in seconds). Vivado's launcher is slow.
_TOOL_PROBES: tuple[tuple[str, tuple[str, ...], int], ...] = (
    ("verilator", ("--version",), 10),
    ("yosys", ("-V",), 10),
    ("iverilog", ("-V",), 10),
    ("sv2v", ("--version",), 10),
    ("verible-verilog-lint", ("--version",), 10),
    ("openroad", ("-version",), 10),
    ("vivado", ("-version",), 60),
)
UNKNOWN_VERSION = "unknown"


@dataclass(frozen=True)
class ToolchainFingerprint:
    image: str | None  # the Sandbox image ID; None outside the Sandbox
    booley: str  # ``sha256:...``
    booley_kind: Literal["build", "source"]  # "source" = development install fallback
    booley_note: str | None  # why the build identity was unavailable
    tools: dict[str, str]  # tool -> first line of its version output


def _probe(executable: str, arguments: tuple[str, ...], timeout_s: int) -> str:
    try:
        result = subprocess.run(
            [executable, *arguments],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return UNKNOWN_VERSION
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return lines[0] if result.returncode == 0 and lines else UNKNOWN_VERSION


def collect_toolchain_fingerprint(
    environ: Mapping[str, str] = os.environ,
) -> ToolchainFingerprint:
    """Collect the fingerprint of the calling environment; ``IdentityError`` if unhashable."""
    kind: Literal["build", "source"] = "build"
    note: str | None = None
    try:
        booley = booley_build_identity()
    except IdentityError as error:
        booley, kind, note = booley_source_identity(), "source", str(error)
    tools = {
        name: _probe(path, arguments, timeout_s)
        for name, arguments, timeout_s in _TOOL_PROBES
        if (path := shutil.which(name)) is not None
    }
    return ToolchainFingerprint(
        environ.get(SANDBOX_IMAGE_ID_ENV) or None,
        booley,
        kind,
        note,
        tools,
    )
