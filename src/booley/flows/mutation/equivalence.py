"""Tell whether a Mutant's Verilator build equals the baseline's (ADR 0088, amendment 2026-11-13).

Verilator turns the RTL into C++.  A Mutant whose generated C++ is the baseline's, apart
from where its private source copy lives, builds the same simulator, so no test can tell
it from the original: it is equivalent.  The check reads only the generated ``.cpp`` and
``.h`` files; it never compares object code or simulation output, and any other
difference keeps the Mutant (it may miss an equivalent Mutant, never drop another).

Pure Python: nothing here imports the Mutation Flow.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Mapping
from pathlib import Path

# Verilator writes one ``<top>__ver.d`` dependency file into the root of its generated C++.
_DEPENDENCY_SUFFIX = "__ver.d"
_SOURCE_SUFFIXES = (".cpp", ".h")
# Stands for the private copy's directory.  A NUL byte never appears in generated C++.
_PRIVATE_ROOT_TOKEN = b"\x00private-root\x00"
_VERSION_TIMEOUT_S = 30
UNKNOWN_VERSION = "unknown"


def generated_cpp(
    generated_dir: Path, private_root: Path, work_dir: Path
) -> dict[str, str] | None:
    """Digest every C++ file Verilator generated in ``generated_dir``, by relative path.

    Each file is read with the spellings of ``private_root`` (the build's private source
    copy) replaced by one token, so two builds that differ only in where their copy lives
    give equal digests.  Returns ``None`` when ``generated_dir`` holds no ``*__ver.d`` or no
    C++ file: the layout is not Verilator's, and the caller must not compare (two empty
    digests would otherwise be equal).
    """
    if not any(generated_dir.glob(f"*{_DEPENDENCY_SUFFIX}")):
        return None
    spellings = _root_spellings(private_root, work_dir)
    digests: dict[str, str] = {}
    for path in sorted(generated_dir.rglob("*")):
        if path.suffix in _SOURCE_SUFFIXES and path.is_file():
            content = path.read_bytes()
            for spelling in spellings:
                content = content.replace(spelling, _PRIVATE_ROOT_TOKEN)
            digests[path.relative_to(generated_dir).as_posix()] = (
                f"sha256:{hashlib.sha256(content).hexdigest()}"
            )
    return digests or None


def _root_spellings(private_root: Path, work_dir: Path) -> list[bytes]:
    """Every way a build can write ``private_root`` plus a separator, longest first."""
    roots = {private_root, private_root.resolve()}
    texts = {text for root in roots for text in (str(root), root.as_posix())}
    if private_root.resolve().is_relative_to(work_dir.resolve()):
        texts.add(private_root.resolve().relative_to(work_dir.resolve()).as_posix())
    spellings = {f"{text}{separator}".encode() for text in texts for separator in ("/", "\\")}
    return sorted(spellings, key=len, reverse=True)


def verilator_version(environment: Mapping[str, str]) -> str:
    """The first line of ``verilator --version`` under ``environment``, or ``"unknown"``."""
    try:
        completed = subprocess.run(
            ["verilator", "--version"],
            env=dict(environment),
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN_VERSION
    lines = completed.stdout.strip().splitlines()
    return lines[0].strip() if completed.returncode == 0 and lines else UNKNOWN_VERSION
