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
BAD_RUN_CWD_DIR = ".booley-doctor-run-cwd"


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


def _remove_shadow(path: Path) -> None:
    """Remove one stale Doctor-owned shadow without following its symlinks."""
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _mirror_runtime_dir(
    source_dir: Path,
    shadow_dir: Path,
    overlay_parts: set[tuple[str, ...]],
    shadow_root: Path,
) -> None:
    """Mirror *source_dir* cheaply, materializing only overlay ancestors."""
    shadow_dir.mkdir(parents=True, exist_ok=True)
    branches: dict[str, set[tuple[str, ...]]] = {}
    for parts in overlay_parts:
        branches.setdefault(parts[0], set()).add(parts[1:])

    if source_dir.is_dir():
        for source in source_dir.iterdir():
            if source == shadow_root:
                continue
            remainders = branches.get(source.name)
            destination = shadow_dir / source.name
            if remainders is None:
                destination.symlink_to(source.resolve(), target_is_directory=source.is_dir())
                continue
            if source.is_symlink():
                raise SelftestOverlayError(
                    f"self-test overlay crosses a symlinked runtime path: {source}"
                )
            deeper = {parts for parts in remainders if parts}
            if deeper:
                if not source.is_dir():
                    raise SelftestOverlayError(
                        f"self-test overlay runtime ancestor is not a directory: {source}"
                    )
                _mirror_runtime_dir(source, destination, deeper, shadow_root)

    for name, remainders in branches.items():
        deeper = {parts for parts in remainders if parts}
        if deeper and not (shadow_dir / name).exists():
            _mirror_runtime_dir(source_dir / name, shadow_dir / name, deeper, shadow_root)


def stage_bad_run_overlay(
    project_dir: Path,
    flow_name: str,
    run_cwd: Path,
    shadow_root: Path,
) -> int:
    """Create an isolated runtime view and apply the bad fixture within it.

    Simulators deliberately run from a project-configured directory because
    testbenches often open firmware or vectors relative to their process cwd.
    FuseSoC can also stage those same inputs beneath its build tree.  A Doctor
    overlay therefore needs both views: the ordinary runtime tree remains
    visible through symlinks, while overlay paths are private real files in the
    per-build shadow.  The project-owned inputs are never modified.
    """
    source_root = bad_overlay_dir(project_dir, flow_name)
    files = [
        path for path in sorted(source_root.rglob("*")) if path.is_file() and not path.is_symlink()
    ]
    if not files:
        return 0
    if not run_cwd.is_dir():
        raise SelftestOverlayError(f"simulation run_cwd is not a directory: {run_cwd}")
    overlay_parts = {path.relative_to(source_root).parts for path in files}
    _remove_shadow(shadow_root)
    _mirror_runtime_dir(run_cwd, shadow_root, overlay_parts, shadow_root)
    return stage_bad_overlay(project_dir, flow_name, shadow_root)
