"""Doctor fail-path fixture overlays.

Simulation fail-path fixtures sometimes need to replace a file after FuseSoC
has staged a Target. Keep that Doctor-only mechanism out of project Flow
configuration: a project may mirror replacement files beneath
``.booley_project/selftest/<flow>/bad-overlay/``, and the internal Doctor run
copies that tree over the resolved build root.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

INTERNAL_KIND_ENV = "BOOLEY_INTERNAL_SELFTEST_KIND"
BAD_KIND = "bad"
_BAD_OVERLAY_DIR = "bad-overlay"
BAD_RUN_CWD_DIR = ".booley-doctor-run-cwd"
_ATTEMPT_TOKEN_RE = re.compile(r"[0-9a-f]{32}")
_OWNED_VIEW_RE = re.compile(
    rf"{re.escape(BAD_RUN_CWD_DIR)}-(?:[0-9a-f]{{16}}|a-[0-9a-f]{{32}})"
)


class SelftestOverlayError(RuntimeError):
    """A Doctor self-test overlay is unsafe or cannot be staged."""


def _validated_generation_parent(project_root: Path, generation_parent: Path) -> Path:
    """Validate the leased generation parent without following its ancestors."""
    root = project_root.resolve()
    parent = Path(generation_parent)
    try:
        relative = parent.relative_to(root)
    except ValueError as exc:
        raise SelftestOverlayError(
            f"Doctor generation parent is outside the Project: {generation_parent}"
        ) from exc
    if ".." in relative.parts:
        raise SelftestOverlayError(
            f"Doctor generation parent is outside the Project: {generation_parent}"
        )
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise SelftestOverlayError(f"Doctor generation parent is a symlink: {current}")
    if parent.is_symlink():
        raise SelftestOverlayError(f"Doctor generation parent is a symlink: {parent}")
    if not parent.resolve().is_relative_to(root):
        raise SelftestOverlayError(
            f"Doctor generation parent is outside the Project: {generation_parent}"
        )
    return parent


def doctor_runtime_view_path(
    project_root: Path, generation_parent: Path, attempt_token: str
) -> Path:
    """Return the attempt-owned runtime view beside a leased generation."""
    if _ATTEMPT_TOKEN_RE.fullmatch(attempt_token) is None:
        raise SelftestOverlayError(f"invalid Doctor attempt token: {attempt_token!r}")
    parent = _validated_generation_parent(project_root, generation_parent)
    return parent / f"{BAD_RUN_CWD_DIR}-a-{attempt_token}"


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
        shutil.copy(source, destination)
        copied += 1
    return copied


def _remove_shadow(path: Path) -> None:
    """Remove one stale Doctor-owned shadow without following its symlinks."""
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _remove_owned_views(generation_parent: Path) -> None:
    """Remove exact legacy and attempt-owned views in one leased slot."""
    for child in sorted(generation_parent.iterdir(), key=lambda path: path.name):
        if _OWNED_VIEW_RE.fullmatch(child.name):
            _remove_shadow(child)


def _note_cleanup_failure(primary: BaseException, path: Path, error: OSError) -> None:
    """Keep a body or staging error primary while recording cleanup failure."""
    primary.add_note(f"could not remove Doctor runtime view {path}: {error}")


@contextmanager
def managed_runtime_view(
    project_dir: Path,
    flow_name: str,
    run_cwd: Path,
    generation_parent: Path,
    attempt_token: str,
) -> Iterator[Path]:
    """Stage and remove one attempt-scoped Doctor runtime view."""
    project_root = project_dir.parent.resolve()
    parent = _validated_generation_parent(project_root, generation_parent)
    view = doctor_runtime_view_path(project_root, parent, attempt_token)
    try:
        _remove_owned_views(parent)
        copied = stage_bad_run_overlay(project_dir, flow_name, run_cwd, view)
        if copied == 0:
            raise SelftestOverlayError(
                f"Doctor requested a bad simulation fixture, but "
                f"{bad_overlay_dir(project_dir, flow_name)} is empty"
            )
    except BaseException as exc:
        try:
            _remove_shadow(view)
        except OSError as cleanup_error:
            _note_cleanup_failure(exc, view, cleanup_error)
        if isinstance(exc, SelftestOverlayError):
            raise
        raise SelftestOverlayError(f"could not stage Doctor runtime view: {exc}") from exc
    try:
        yield view
    except BaseException as exc:
        try:
            _remove_shadow(view)
        except OSError as cleanup_error:
            _note_cleanup_failure(exc, view, cleanup_error)
        raise
    try:
        _remove_shadow(view)
    except OSError as exc:
        raise SelftestOverlayError(f"could not remove Doctor runtime view {view}: {exc}") from exc


def _mirror_excluded_ancestor(
    source: Path,
    destination: Path,
    shadow_root: Path,
    excluded_dir: Path,
) -> bool:
    """Materialize an ancestor of *excluded_dir* without mirroring it."""
    resolved_source = source.resolve()
    if not source.is_dir() or not excluded_dir.is_relative_to(resolved_source):
        return False
    if source.is_symlink():
        raise SelftestOverlayError(f"self-test overlay crosses a symlinked runtime path: {source}")
    _mirror_runtime_dir(source, destination, set(), shadow_root, excluded_dir)
    return True


def _mirror_runtime_dir(
    source_dir: Path,
    shadow_dir: Path,
    overlay_parts: set[tuple[str, ...]],
    shadow_root: Path,
    excluded_dir: Path,
) -> None:
    """Mirror *source_dir* cheaply, excluding project infrastructure."""
    shadow_dir.mkdir(parents=True, exist_ok=True)
    branches: dict[str, set[tuple[str, ...]]] = {}
    for parts in overlay_parts:
        branches.setdefault(parts[0], set()).add(parts[1:])

    if source_dir.is_dir():
        for source in source_dir.iterdir():
            if source == shadow_root:
                continue
            resolved_source = source.resolve()
            if resolved_source == excluded_dir:
                continue
            remainders = branches.get(source.name)
            destination = shadow_dir / source.name
            if remainders is None:
                if _mirror_excluded_ancestor(source, destination, shadow_root, excluded_dir):
                    continue
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
                _mirror_runtime_dir(source, destination, deeper, shadow_root, excluded_dir)

    for name, remainders in branches.items():
        deeper = {parts for parts in remainders if parts}
        if deeper and not (shadow_dir / name).exists():
            _mirror_runtime_dir(
                source_dir / name,
                shadow_dir / name,
                deeper,
                shadow_root,
                excluded_dir,
            )


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
    project_dir = project_dir.resolve()
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
    _mirror_runtime_dir(run_cwd, shadow_root, overlay_parts, shadow_root, project_dir)
    return stage_bad_overlay(project_dir, flow_name, shadow_root)
