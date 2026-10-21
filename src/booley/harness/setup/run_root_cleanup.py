"""Marker-guarded removal of one Project Setup run root.

``prepare`` stamps ``.booley/tmp/setup/<run-id>/``; ``apply`` removes only a stamped root
after snapshotting Findings attachments, deleting the marker last so failures stay retryable.
Known limit: an attachment added after the final re-read would dangle (Setup is the only writer).
"""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from booley.feedback import findings, materialize
from booley.runtime.booley_dir import BOOLEY_DIR_NAME, resolve_checkout_booley_dir

MARKER = ".booley-setup-run"


class CleanupRefusedError(RuntimeError):
    """The run root cannot be proven ours; nothing was deleted (CLI exit 2)."""


def _report(status: str, root: Path, files: Sequence[dict[str, Any]] = ()) -> dict[str, Any]:
    """The JSON-ready outcome: ``status`` is ``prepared|preview|removed|gone|kept``."""
    total = sum(item["bytes"] for item in files)
    return {
        "status": status,
        "run_id": root.name,
        "run_root": str(root),
        "files": list(files),
        "bytes": total,
        "kept": [],
    }


def _keep(report: dict[str, Any], reason: str, paths: set[Path]) -> dict[str, Any]:
    """Mark the run root as kept, naming each blocking path."""
    kept = [{"reason": reason, "path": str(path)} for path in sorted(paths)]
    return {**report, "status": "kept", "kept": kept}


def _locate(project_root: Path, run_id: str) -> tuple[Path, Path]:
    """Validate the run ID; return ``(booley_dir, run_root)`` or refuse on any symlink."""
    if not run_id or run_id in {".", ".."} or Path(run_id).name != run_id or not run_id.isascii():
        raise CleanupRefusedError("run_id must be one safe ASCII path component")
    root = project_root.resolve()
    try:
        booley_dir = resolve_checkout_booley_dir(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CleanupRefusedError(f"cannot resolve the Booley Directory: {exc}") from exc
    if booley_dir != root / BOOLEY_DIR_NAME or not booley_dir.is_dir():
        raise CleanupRefusedError("no .booley directory was found")
    run_root = booley_dir / "tmp" / "setup" / run_id
    for path in (booley_dir, *reversed(run_root.parents[:2]), run_root):
        if path.is_symlink():
            raise CleanupRefusedError(f"refusing symlinked path: {path}")
    return booley_dir, run_root


def prepare(project_root: Path, run_id: str | None = None) -> dict[str, Any]:
    """Create a fresh, marked run root (``run_id`` defaults to a random token)."""
    run_id = run_id or uuid.uuid4().hex
    _, run_root = _locate(project_root, run_id)
    run_root.parent.mkdir(parents=True, exist_ok=True)
    try:
        run_root.mkdir()
    except FileExistsError as exc:
        raise CleanupRefusedError(f"run root already exists: {run_root}") from exc
    (run_root / MARKER).write_text("booley setup run root\n", encoding="utf-8")
    return _report("prepared", run_root)


def _attached(booley_dir: Path, run_root: Path) -> tuple[set[Path], int]:
    """Attachment paths under the run root, and the Findings Log's corrupt-line count."""
    log = findings.read_log(booley_dir)
    raw = (a for finding in log.entries for a in finding.attachments)
    paths = {materialize.normalize_attachment_path(a, booley_dir) for a in raw}
    return {p for p in paths if p.is_relative_to(run_root)}, log.corrupt_lines


def _inventory(root: Path, attached: set[Path]) -> list[dict[str, Any]]:
    """Every file and symlink under the run root; symlinks are listed, never followed."""
    found = sorted(Path(d) / n for d, subdirs, names in os.walk(root) for n in names + subdirs)
    return [
        {"path": str(p.relative_to(root)), "bytes": p.lstat().st_size, "attached": p in attached}
        for p in found
        if p.is_symlink() or not p.is_dir()
    ]


def preview(project_root: Path, run_id: str) -> dict[str, Any]:
    """List what ``apply`` would remove; read-only, no marker or lock needed."""
    booley_dir, run_root = _locate(project_root, run_id)
    if not run_root.exists():
        return _report("gone", run_root)
    return _report("preview", run_root, _inventory(run_root, _attached(booley_dir, run_root)[0]))


def apply(project_root: Path, run_id: str) -> dict[str, Any]:
    """Snapshot attachments, then remove the marked run root; keep it on any doubt."""
    booley_dir, run_root = _locate(project_root, run_id)
    if not run_root.exists():
        return _report("gone", run_root)
    if not (run_root / MARKER).is_file():
        raise CleanupRefusedError(f"{run_root} has no {MARKER} marker; remove it by hand")
    attached, corrupt = _attached(booley_dir, run_root)
    report = _report("removed", run_root, _inventory(run_root, attached))
    blockers = attached or {findings.log_path(booley_dir)}
    if corrupt:
        return _keep(report, f"{corrupt} corrupt Findings Log line(s)", blockers)
    try:
        materialize.materialize_attachments(booley_dir, attached)
    except (RuntimeError, OSError, ValueError) as exc:  # corrupt log, lock timeout, I/O
        return _keep(report, str(exc), blockers)
    left, corrupt_now = _attached(booley_dir, run_root)  # a half-written append shows as corrupt
    if left or corrupt_now != corrupt:
        return _keep(report, "Findings Log changed during cleanup", left or blockers)
    try:  # children first, marker last: a failed removal stays retryable
        for path in sorted(run_root.iterdir(), key=lambda entry: entry.name == MARKER):
            shutil.rmtree(path) if path.is_dir() and not path.is_symlink() else path.unlink()
        run_root.rmdir()
    except OSError as exc:
        (run_root / MARKER).touch()  # a late file made rmdir fail: restore ownership for the retry
        return _keep(report, str(exc), {Path(exc.filename or run_root)})
    return report
