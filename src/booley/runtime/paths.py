"""Resolve paths to package data, project dir, and runtime dirs.

After packaging, reference docs, Docker configs, skills, and runtime assets
live inside the installed package under data/. This module provides
stable accessors that work in both editable and installed modes.
"""

from __future__ import annotations

import os
import shutil
import sys
from importlib.resources import files
from pathlib import Path


def dev_support_dir() -> Path:
    """Return the installed package's developer-support directory."""
    return Path(str(files("booley").joinpath("dev_support")))


def package_data_dir() -> Path:
    """Return path to the installed package's data/ directory.

    Falls back to __file__-relative resolution if importlib.resources
    resolves to a shadow booley/ directory (e.g. agent-created artifacts
    in the worktree) that lacks the expected data/ subtree.
    """
    resolved = Path(str(files("booley").joinpath("data")))
    if (resolved / "refs").is_dir():
        return resolved
    # Shadow package — fall back to this file's location
    fallback = Path(__file__).resolve().parent.parent / "data"
    if (fallback / "refs").is_dir():
        return fallback
    return resolved


def refs_dir() -> Path:
    """Return path to reference docs (style guides, review guides, etc.)."""
    return package_data_dir() / "refs"


def troubleshooting_path() -> Path:
    """Return the packaged troubleshooting guide."""
    return refs_dir() / "TROUBLESHOOTING.md"


def changelog_path() -> Path:
    """Return the packaged public changelog mirror."""
    return refs_dir() / "CHANGELOG.md"


def faq_path() -> Path:
    """Return the packaged troubleshooting guide (compatibility alias)."""
    return troubleshooting_path()


def docker_data_dir() -> Path:
    """Return path to Docker config files (Dockerfiles, PDK)."""
    return package_data_dir() / "docker"


def skills_dir() -> Path:
    """Return path to skill definitions shipped with the package."""
    return package_data_dir() / "skills"


def bwave_binary() -> Path:
    """Return the legacy package-data path for a native bwave binary.

    Release wheels do not contain this platform-specific executable. The
    sandbox image may inject it at this path for compatibility, while normal
    runtime resolution prefers ``BOOLEY_BWAVE_BIN`` and the static libexec path.
    """
    name = "bwave.exe" if sys.platform == "win32" else "bwave"
    binary = package_data_dir() / "bin" / name
    if sys.platform != "win32" and binary.exists() and not os.access(binary, os.X_OK):
        binary.chmod(0o755)
    return binary


def is_bwave_wrapper(path: Path) -> bool:
    """Return True when ``path`` is the Python ``bwave`` wrapper, not the binary.

    Two executables answer to the name ``bwave``: the native Rust binary
    (query subcommands: build/list/stats/wave/...) and the Python wrapper
    (booley.bwave.cli — adds gui/register/markers and forwards the rest).
    Only the wrapper is on PATH inside the sandbox image, so every resolver
    that wants the *binary* has to recognize and skip the wrapper — otherwise
    the wrapper resolves to itself and recurses, and FIFO streaming hands the
    simulator a script that exits before reading the FIFO.
    """
    if path.suffix.lower() in (".exe", ".com"):
        return False
    try:
        head = path.read_bytes()[:512]
    except OSError:
        return False
    lines = head.splitlines()
    first_line = lines[0].lower() if lines else b""
    return b"booley.bwave.cli" in head or b"python" in first_line


def _native_bwave_candidates() -> list[Path]:
    """Return native bwave locations, in preference order."""
    exe = "bwave.exe" if sys.platform == "win32" else "bwave"
    candidates: list[Path] = []

    override = os.environ.get("BOOLEY_BWAVE_BIN")
    if override:
        candidates.append(Path(override))

    # Compatibility path for sandbox images that inject the binary into package
    # data after installing the platform-neutral wheel. Deliberately off PATH.
    candidates.append(bwave_binary())

    # Editable/dev checkout: whatever `cargo build` last produced.
    repo_root = Path(__file__).resolve().parents[3]
    bwave_target = repo_root / "crates" / "bwave" / "target"
    candidates += [bwave_target / "release" / exe, bwave_target / "debug" / exe]

    # Sandbox image, static path (also named by $BOOLEY_BWAVE_BIN above). Both
    # candidates above follow the *imported* booley package, so a bind-mounted
    # source checkout that shadows the installed one moves them into a tree with
    # no built binary; this path doesn't move.
    candidates.append(Path("/usr/local/libexec/booley") / exe)

    # Sandbox images built before the binary moved into package data installed
    # it here. Kept so a stale image still resolves the binary rather than
    # falling through to a cargo build that its offline network can't do.
    candidates += [Path.home() / ".cargo" / "bin" / exe, Path("/home/agent/.cargo/bin") / exe]
    return candidates


def native_bwave_binary() -> Path | None:
    """Resolve the native (Rust) bwave binary, or None when it isn't installed.

    PATH is the last resort, and a PATH hit on the Python wrapper is rejected:
    inside the sandbox image the only ``bwave`` on PATH is the wrapper, so that
    a human typing ``bwave gui`` reaches the verb they mean. Callers that need
    raw binary semantics — coverage stats, FIFO streaming, the wrapper's own
    query forwarding — must resolve through here and never by bare name.
    """
    for candidate in _native_bwave_candidates():
        if candidate.exists() and not is_bwave_wrapper(candidate):
            return candidate
    on_path = shutil.which("bwave")
    if on_path and not is_bwave_wrapper(Path(on_path)):
        return Path(on_path)
    return None


def cheatsheet_path() -> Path:
    """Return path to the quick-reference cheatsheet."""
    return package_data_dir() / "cheatsheet.md"
