"""Doctor fail-path fixture overlays.

Simulation fail-path fixtures sometimes need to replace a file after FuseSoC
has staged a Target. Keep that Doctor-only mechanism out of project Flow
configuration: a project may mirror replacement files beneath
``.booley_project/selftest/<flow>/bad-overlay/``, and the internal Doctor run
copies that tree over the resolved build root.
"""

from __future__ import annotations

import shutil
from pathlib import Path

INTERNAL_KIND_ENV = "BOOLEY_INTERNAL_SELFTEST_KIND"
BAD_KIND = "bad"
_BAD_OVERLAY_DIR = "bad-overlay"


class SelftestOverlayError(RuntimeError):
    """A Doctor self-test overlay is unsafe or cannot be staged."""


def bad_overlay_dir(project_dir: Path, flow_name: str) -> Path:
    """Return the conventional bad-fixture overlay directory for *flow_name*."""
    return project_dir / "selftest" / flow_name / _BAD_OVERLAY_DIR


def has_bad_overlay(project_dir: Path, flow_name: str) -> bool:
    """Return whether *flow_name* has at least one regular overlay file."""
    root = bad_overlay_dir(project_dir, flow_name)
    return root.is_dir() and any(
        path.is_file() and not path.is_symlink() for path in root.rglob("*")
    )


def stage_bad_overlay(project_dir: Path, flow_name: str, build_root: Path) -> int:
    """Copy *flow_name*'s bad-fixture overlay over *build_root*.

    The overlay must contain only real directories and regular files. Symlinks
    are rejected on both sides so a project fixture cannot redirect a copy
    outside the resolved build tree.
    """
    source_root = bad_overlay_dir(project_dir, flow_name)
    if not source_root.is_dir():
        return 0
    resolved_build_root = build_root.resolve()
    copied = 0
    for source in sorted(source_root.rglob("*")):
        if source.is_symlink():
            raise SelftestOverlayError(f"self-test overlay contains a symlink: {source}")
        relative = source.relative_to(source_root)
        destination = build_root / relative
        if source.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        if not source.is_file():
            raise SelftestOverlayError(f"self-test overlay contains a non-file: {source}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.resolve().is_relative_to(resolved_build_root):
            raise SelftestOverlayError(
                f"self-test overlay destination escapes the build root: {destination}"
            )
        shutil.copy2(source, destination)
        copied += 1
    return copied
