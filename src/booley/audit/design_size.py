"""Design-size analysis independent of Doctor presentation and sequencing."""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from booley.fusesoc import fusesoc_registry

HDL_SUFFIXES = frozenset({".v", ".sv", ".vh", ".svh"})
SKIP_DIRECTORIES = frozenset(
    {
        ".git",
        "build",
        "dist",
        "node_modules",
        ".venv",
        "__pycache__",
        ".hg",
        ".svn",
    }
)
LARGE_DESIGN_FILES = 250
LARGE_DESIGN_LOC = 150_000


class DesignSizeScope(StrEnum):
    """Source population used to calculate a design's size."""

    CONFIGURED_TARGETS = "configured-targets"
    REPOSITORY = "repository"


@dataclass(frozen=True, slots=True)
class DesignSizeAudit:
    """A design-size measurement and the source population it represents."""

    hdl_files: int
    lines_of_code: int
    scope: DesignSizeScope

    @property
    def exceeds_deep_smoke_budget(self) -> bool:
        """Whether the design warrants Doctor's large-design advisory."""
        return self.hdl_files >= LARGE_DESIGN_FILES or self.lines_of_code >= LARGE_DESIGN_LOC


def analyze_design_size(
    project_root: Path,
    project_dir: Path,
    configured_targets: Iterable[str],
) -> DesignSizeAudit:
    """Measure configured Target closure, falling back to a pruned repository scan."""
    configured_paths = _configured_hdl_paths(project_root, configured_targets)
    if configured_paths:
        files, lines = _count_design_files(configured_paths)
        return DesignSizeAudit(files, lines, DesignSizeScope.CONFIGURED_TARGETS)

    files, lines = _count_design_files(_repository_hdl_paths(project_root, project_dir))
    return DesignSizeAudit(files, lines, DesignSizeScope.REPOSITORY)


def _configured_hdl_paths(project_root: Path, targets: Iterable[str]) -> set[Path]:
    paths: set[Path] = set()
    for target in targets:
        try:
            sources = fusesoc_registry.target_source_files(
                project_root,
                target,
                include_dependencies=True,
            )
        except fusesoc_registry.FuseSocError:
            continue
        for relative in (*sources.rtl_source_files, *sources.tb_files):
            path = project_root / relative
            if path.suffix.lower() in HDL_SUFFIXES:
                paths.add(path)
    return paths


def _repository_hdl_paths(project_root: Path, project_dir: Path) -> set[Path]:
    paths: set[Path] = set()
    resolved_project_dir = project_dir.resolve()
    for dirpath, dirnames, filenames in os.walk(project_root):
        directory = Path(dirpath)
        dirnames[:] = [
            name
            for name in dirnames
            if name not in SKIP_DIRECTORIES
            and (directory / name).resolve() != resolved_project_dir
        ]
        paths.update(
            Path(dirpath) / name for name in filenames if Path(name).suffix.lower() in HDL_SUFFIXES
        )
    return paths


def _count_design_files(paths: set[Path]) -> tuple[int, int]:
    files = 0
    lines = 0
    for path in paths:
        try:
            with path.open("rb") as handle:
                lines += sum(
                    chunk.count(b"\n") for chunk in iter(lambda: handle.read(1 << 20), b"")
                )
        except OSError:
            continue
        files += 1
    return files, lines
