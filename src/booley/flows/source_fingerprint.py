"""Source SHA fingerprinting — hash RTL/TB sources to detect staleness.

Enumerates the RTL/TB sources straight from the project's ``.core`` filesets
(the ``tags:[tb]`` partition, ADR 0026 follow-through) and produces a
conservative SHA-256 fingerprint of exactly those declared files. The
development state stores this fingerprint so acceptance can detect whether
sources changed since a criterion was met. A pre-migration project with no
``.core`` falls back to hashing every regular file under the hardcoded default
source directories. Split out of ``development_state`` (principle 8); imports
nothing from it so the dependency flows one way.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path, PurePosixPath
from typing import Any

from booley.core import boundary

logger = logging.getLogger(__name__)


SOURCE_FINGERPRINT_DETAIL_KEY = "_source_fingerprint"


def as_str_list(value: Any, default: list[str]) -> list[str]:
    """Coerce a TOML ``source_dirs`` value to ``list[str]``.

    Thin adapter over :func:`booley.core.boundary.as_str_list` that keeps this
    module's historical *required-positional* ``default`` (many importers call
    ``as_str_list(x, ["rtl"])``); the shared helper takes ``default`` keyword-only.
    Behaviour is identical: a bare ``str`` becomes a one-element list (the classic
    ``source_dirs = "rtl"`` footgun), a list is filtered to its string entries,
    and anything else — or a list that filters empty — falls back to *default*.
    """
    return boundary.as_str_list(value, default=default)


def _core_source_files(
    work_dir: Path,
    target: str | None,
) -> tuple[list[str], list[str]] | None:
    """Exact RTL/TB source files (project-relative POSIX) from ``.core`` files.

    ``None`` when no ``.core`` is authored under *work_dir* (caller falls back to
    hashing whole directories). When *target* is set, only that Target's
    dependency closure is returned.
    """
    try:
        from booley.fusesoc.fusesoc_registry import (
            classified_sources,
            discover_cores,
            target_source_files,
        )
    except Exception:  # noqa: BLE001 — registry unavailable; use directory fallback
        return None
    if not discover_cores(work_dir):
        return None
    cs = (
        target_source_files(
            work_dir,
            target,
            include_dependencies=True,
            include_headers=True,
        )
        if target
        else classified_sources(work_dir)
    )
    return list(cs.rtl_source_files), list(cs.tb_files)


def _read_source_dirs(work_dir: Path) -> tuple[list[str], list[str]]:
    """RTL/TB source dirs from the ``.core`` filesets (legacy defaults if none)."""
    try:
        from booley.fusesoc.fusesoc_registry import source_dirs_from_core

        rtl_dirs, tb_dirs, _incl = source_dirs_from_core(work_dir)
        return rtl_dirs, tb_dirs
    except Exception:  # noqa: BLE001 — registry unavailable
        return ["rtl", "fw"], ["tb"]


def _hash_named_files(work_dir: Path, rel_names: list[str]) -> dict[str, Any]:
    """SHA-256 over an explicit list of project-relative files (content + length)."""
    root = work_dir.resolve()
    digest = hashlib.sha256()
    file_names = sorted({PurePosixPath(n).as_posix() for n in rel_names})
    for rel_name in file_names:
        file_path = root / rel_name
        try:
            data = file_path.read_bytes()
        except OSError:
            data = b""
        digest.update(rel_name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(data)).encode("ascii"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return {"digest": digest.hexdigest(), "files": file_names}


def _hash_source_group(work_dir: Path, source_dirs: list[str]) -> dict[str, Any]:
    """Hash every regular file under the configured source directories."""
    root = work_dir.resolve()
    rel_files: list[Path] = []
    for rel_dir in source_dirs:
        source_root = (root / rel_dir).resolve()
        if not source_root.is_dir():
            continue
        for path in source_root.rglob("*"):
            if path.is_file():
                try:
                    rel_files.append(path.resolve().relative_to(root))
                except ValueError:
                    continue

    digest = hashlib.sha256()
    file_names = sorted({p.as_posix() for p in rel_files})
    for rel_name in file_names:
        file_path = root / rel_name
        try:
            data = file_path.read_bytes()
        except OSError:
            data = b""
        digest.update(rel_name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(data)).encode("ascii"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")

    return {"digest": digest.hexdigest(), "files": file_names}


def compute_source_fingerprint(
    work_dir: Path,
    *,
    target: str | None = None,
) -> dict[str, Any]:
    """Return a conservative fingerprint of the ``.core``-declared RTL and TB sources.

    When *target* is provided, the fingerprint covers only that Target's
    filesets and transitive core dependency closure. Without a Target it keeps
    the project-wide behavior needed by non-targeted checks. A pre-migration
    project with no ``.core`` falls back to hashing every regular file under
    the default source directories.
    """
    root = work_dir.resolve()
    core = _core_source_files(root, target)
    if core is not None:
        rtl_files, tb_files = core
        return {
            "algorithm": "sha256",
            "work_dir": str(root),
            "target": target,
            "rtl_dirs": sorted({PurePosixPath(f).parent.as_posix() for f in rtl_files}),
            "tb_dirs": sorted({PurePosixPath(f).parent.as_posix() for f in tb_files}),
            "rtl": _hash_named_files(root, rtl_files),
            "tb": _hash_named_files(root, tb_files),
        }
    rtl_dirs, tb_dirs = _read_source_dirs(root)
    return {
        "algorithm": "sha256",
        "work_dir": str(root),
        "target": None,
        "rtl_dirs": rtl_dirs,
        "tb_dirs": tb_dirs,
        "rtl": _hash_source_group(root, rtl_dirs),
        "tb": _hash_source_group(root, tb_dirs),
    }
