"""Classify every Booley Directory path as Common or Checkout Booley State (ADR 0082).

Common Booley State exists once per Project, in the Main Booley Directory.
Checkout Booley State exists once per checkout: it is versioned with the RTL or
tied to one checkout's sources. Everything else is Common. A Snapshot copies
only the versioned rows; :func:`iter_booley_dir` walks a real directory in
these terms.

Patterns in :data:`BOOLEY_DIR_PATHS` are ``/``-separated and relative to the
Booley Directory. A trailing ``/`` covers the directory and everything below it;
otherwise a pattern names exactly that path. ``*`` matches within one segment.
The pattern with the most segments wins, so ``goals/`` (Common) and
``goals/history/`` (Checkout) coexist. Container directories (``.runtime``,
``runtime``, ``tmp``) are not state themselves: only their listed children are
classified, so a new child must be added to the table explicitly.

Stdlib-only.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from enum import StrEnum
from fnmatch import fnmatchcase
from pathlib import Path, PurePath
from types import MappingProxyType


class BooleyDirPart(StrEnum):
    """Which part of Booley state a Booley Directory path belongs to."""

    COMMON = "common"  # One per Project, in the Main Booley Directory.
    CHECKOUT = "checkout"  # One per checkout.


class BooleyDirClass(StrEnum):
    """A Booley Directory path's class: its part, with Checkout split by what it is tied to."""

    VERSIONED = "versioned"  # Checkout, versioned with the RTL: copied into a Snapshot.
    SOURCE_TIED = "source-tied"  # Checkout, tied to one checkout's sources: never copied.
    COMMON = "common"  # One per Project, in the Main Booley Directory.

    @property
    def part(self) -> BooleyDirPart:
        """Return the part of Booley state this class belongs to."""
        return BooleyDirPart.COMMON if self is BooleyDirClass.COMMON else BooleyDirPart.CHECKOUT


class UnclassifiedBooleyPathError(ValueError):
    """A path is outside the Booley Directory or no table pattern covers it."""


_COMMON = BooleyDirClass.COMMON
_VERSIONED = BooleyDirClass.VERSIONED
_SOURCE_TIED = BooleyDirClass.SOURCE_TIED

# Every Checkout row says whether it is versioned with the RTL (copied into a
# Snapshot) or tied to one checkout's sources (never copied). ADR 0082 appendix rows first, then paths other Booley modules write (see the
# runtime/, mcp/, doctor and setup writers).
BOOLEY_DIR_PATHS: Mapping[str, BooleyDirClass] = MappingProxyType(
    {
        # Checkout: versioned with the RTL.
        "booley.toml": _VERSIONED,
        "tests.toml": _VERSIONED,
        "metrics.toml": _VERSIONED,
        "criteria.toml": _VERSIONED,
        "doctor-waivers.toml": _VERSIONED,
        "configs.toml": _VERSIONED,
        "cores/": _VERSIONED,
        "hooks/": _VERSIONED,
        "generators/": _VERSIONED,
        "mcp_tools/": _VERSIONED,
        "selftest/": _VERSIONED,
        "AGENTS.md": _VERSIONED,
        "FUSESOC_IGNORE": _VERSIONED,
        "goals/history/": _VERSIONED,
        "tickets/history/": _VERSIONED,
        # Checkout: tied to one checkout's sources.
        "tmp/doctor/": _SOURCE_TIED,
        "runtime/doctor/core-resolve/": _SOURCE_TIED,
        ".runtime/edalize/": _SOURCE_TIED,
        ".runtime/python-artifacts/": _SOURCE_TIED,
        "flow-reports/": _SOURCE_TIED,
        "mcp-tool-reports/": _SOURCE_TIED,
        "tmp/setup/": _SOURCE_TIED,
        # Checkout, written by other Booley modules: versioned.
        "pipeline.toml": _VERSIONED,
        "tools/": _VERSIONED,
        "rtl_style_guide.md": _VERSIONED,
        "tb_style_guide.md": _VERSIONED,
        ".gitignore": _VERSIONED,
        ".gitattributes": _VERSIONED,
        # Generated (gitignored) Git hook bundle; hook adapters prefer the
        # checkout's own copy, so a Snapshot keeps it.
        ".managed/": _VERSIONED,
        # Checkout, written by other Booley modules: tied to one checkout's sources.
        # A Paired Checkout's own ``.git`` pointer file is per checkout; the main
        # Versioned Booley Directory's object store is reached through git, not this table.
        ".git/": _SOURCE_TIED,
        ".runtime/syn/": _SOURCE_TIED,
        ".runtime/simulation-run-locks/": _SOURCE_TIED,
        ".runtime/flow-console/": _SOURCE_TIED,
        ".runtime/mutation/": _SOURCE_TIED,
        # The retired Specialist's scratch tree: the next mutation run deletes it, but an
        # upgraded checkout may still hold one, and a Snapshot must classify it.
        ".runtime/mutation_tester/": _SOURCE_TIED,
        ".runtime/flow-reports/": _SOURCE_TIED,
        ".runtime/mcp-tool-reports/": _SOURCE_TIED,
        ".runtime/display.jsonl": _SOURCE_TIED,
        ".runtime/prompts/": _SOURCE_TIED,
        # bwave's session store: its sessions name this checkout's traces (#3483).
        ".runtime/bwave/": _SOURCE_TIED,
        "tmp/fusesoc-isolated-cores/": _SOURCE_TIED,
        # Common: ADR rows.
        "goals/": _COMMON,
        "runtime/jobs/": _COMMON,
        "runtime/job-handles/": _COMMON,
        "runtime/sessions/": _COMMON,
        "runtime/session_activity/": _COMMON,
        "runtime/dashboard-task.lock": _COMMON,
        ".interactive_logs/": _COMMON,
        "runtime/doctor_stamp.json": _COMMON,
        "runtime/doctor_deep_stamp.json": _COMMON,
        "runtime/upgrade_review.json": _COMMON,
        ".runtime/compiler-cache/": _COMMON,
        "result-cache/": _COMMON,
        "measurements/": _COMMON,
        "worktrees/": _COMMON,
        ".baseline-wt-*/": _COMMON,
        "directory-layout.lock": _COMMON,
        ".directory-relocation.json": _COMMON,
        "findings.jsonl": _COMMON,
        "BOOLEY-FEEDBACK.md": _COMMON,
        "setup-evidence/": _COMMON,
        "reviewer-evidence/": _COMMON,
        "reviewer-state/": _COMMON,
        "handoffs/": _COMMON,
        "tickets/": _COMMON,
        "goalsets/": _COMMON,
        "docker/": _COMMON,
        # Common, written by other Booley modules.
        "runtime/dashboard-task.json": _COMMON,
        "runtime/upgrade_review.lock": _COMMON,
        "runtime/doctor/": _COMMON,
        "runtime/developer_probe.json": _COMMON,
        "runtime/synth_probe.json": _COMMON,
        "runtime/resources/": _COMMON,
        ".runtime/executions/": _COMMON,
        ".runtime/jobs/": _COMMON,
        ".runtime/findings.lock": _COMMON,
        ".runtime/sessions/": _COMMON,
        ".runtime/transcripts/": _COMMON,
        "findings.jsonl.tmp": _COMMON,
        "SETUP-REPORT.md": _COMMON,
        "FEEDBACK-REPORT.md": _COMMON,
        "SETUP-PLAN.md": _COMMON,
        "PARITY-REPORT.md": _COMMON,
        "parity/": _COMMON,
        "plans/": _COMMON,
        "logs/": _COMMON,
        "human-logs/": _COMMON,  # Markdown copies of .runtime/transcripts/
    }
)


def _covering_depth(pattern: str, parts: tuple[str, ...]) -> int | None:
    """Return the pattern's segment count when it covers *parts*, else ``None``."""
    segments = tuple(pattern.rstrip("/").split("/"))
    covers_subtree = pattern.endswith("/")
    if len(parts) < len(segments) or (not covers_subtree and len(parts) != len(segments)):
        return None
    if all(fnmatchcase(part, segment) for part, segment in zip(parts, segments, strict=False)):
        return len(segments)
    return None


def booley_dir_part(relative: str | PurePath) -> BooleyDirPart:
    """Return the part of Booley state that owns *relative* (relative to the Booley Directory).

    Raises:
        UnclassifiedBooleyPathError: see :func:`booley_dir_class`.
    """
    return booley_dir_class(relative).part


def booley_dir_class(relative: str | PurePath) -> BooleyDirClass:
    """Return the class that owns *relative* (relative to the Booley Directory).

    Raises:
        UnclassifiedBooleyPathError: the path is anchored, escapes with ``..``, or no
            table pattern covers it.
    """
    path = PurePath(relative)
    if path.anchor or ".." in path.parts:
        raise UnclassifiedBooleyPathError(f"not relative to the Booley Directory: {relative}")
    best = _class_of(tuple(segment for segment in path.parts if segment != "."))
    if best is None:
        raise UnclassifiedBooleyPathError(f"unclassified Booley Directory path: {relative}")
    return best


def _class_of(segments: tuple[str, ...]) -> BooleyDirClass | None:
    """Return the class covering *segments*, an atomic-write temporary taking its target's."""
    best = _best_class(segments)
    # An atomic write's temporary file (``.<name>.<pid>.tmp``) belongs to ``<name>``.
    for target in _temp_file_targets(segments[-1] if segments else ""):
        if best is not None:
            break
        best = _best_class((*segments[:-1], target))
    return best


def _temp_file_targets(leaf: str) -> Iterator[str]:
    """Yield the names a ``.<name>.<suffix...>`` temporary file may be writing, longest first.

    ``<name>`` is tried bare (``booley.toml``) and dotted (``.gitignore``).
    """
    if not leaf.startswith("."):
        return
    candidate = leaf[1:]
    while "." in candidate:
        candidate = candidate.rsplit(".", 1)[0]
        yield candidate
        yield f".{candidate}"


def _best_class(segments: tuple[str, ...]) -> BooleyDirClass | None:
    """Return the class of the deepest table pattern covering *segments*, if any."""
    best_depth = 0
    best: BooleyDirClass | None = None
    for pattern, booley_class in BOOLEY_DIR_PATHS.items():
        depth = _covering_depth(pattern, segments)
        if depth is not None and depth > best_depth:
            best_depth, best = depth, booley_class
    return best


def _splits_below(segments: tuple[str, ...], own: BooleyDirClass | None) -> bool:
    """Return whether a table row below *segments* has a class other than *own*.

    An unclassified directory (*own* is ``None``) is a container: any row below
    it splits it.
    """
    for pattern, booley_class in BOOLEY_DIR_PATHS.items():
        pattern_segments = pattern.rstrip("/").split("/")
        if len(pattern_segments) <= len(segments) or booley_class is own:
            continue
        if all(
            fnmatchcase(part, seg) for part, seg in zip(segments, pattern_segments, strict=False)
        ):
            return True
    return False


def iter_booley_dir(root: Path) -> Iterator[tuple[str, BooleyDirClass | None]]:
    """Yield the top-most entries of a Booley Directory whose whole subtree has one class.

    Paths are ``/``-separated and relative to *root*; ``None`` marks an entry no
    table row covers. A real directory is entered only when a deeper table row
    gives part of it another class (``goals/`` holds ``goals/history/``) or when
    it is an unclassified container (``runtime/``); a classified subtree is
    never walked. Symlinks are leaves. Entries come in sorted order.

    Raises:
        OSError: *root* or a walked directory cannot be listed.
    """
    yield from _walk(root, ())


def _walk(directory: Path, prefix: tuple[str, ...]) -> Iterator[tuple[str, BooleyDirClass | None]]:
    with os.scandir(directory) as entries:
        children = sorted(entries, key=lambda entry: entry.name)
    for child in children:
        segments = (*prefix, child.name)
        own = _class_of(segments)
        if child.is_dir(follow_symlinks=False) and _splits_below(segments, own):
            yield from _walk(Path(child.path), segments)
        else:
            yield "/".join(segments), own
