"""Classify every Booley Directory path as Common or Checkout Booley State (ADR 0082).

Common Booley State exists once per Project, in the Main Booley Directory.
Checkout Booley State exists once per checkout: it is versioned with the RTL or
tied to one checkout's sources. Everything else is Common.

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

from collections.abc import Iterator, Mapping
from enum import StrEnum
from fnmatch import fnmatchcase
from pathlib import PurePath
from types import MappingProxyType


class BooleyDirPart(StrEnum):
    """Which part of Booley state a Booley Directory path belongs to."""

    COMMON = "common"  # One per Project, in the Main Booley Directory.
    CHECKOUT = "checkout"  # One per checkout.


class UnclassifiedBooleyPathError(ValueError):
    """A path is outside the Booley Directory or no table pattern covers it."""


_COMMON = BooleyDirPart.COMMON
_CHECKOUT = BooleyDirPart.CHECKOUT

# ADR 0082 appendix rows first, then paths other Booley modules write (see the
# runtime/, mcp/, doctor and setup writers).
BOOLEY_DIR_PATHS: Mapping[str, BooleyDirPart] = MappingProxyType(
    {
        # Checkout: versioned with the RTL.
        "booley.toml": _CHECKOUT,
        "tests.toml": _CHECKOUT,
        "criteria.toml": _CHECKOUT,
        "doctor-waivers.toml": _CHECKOUT,
        "configs.toml": _CHECKOUT,
        "cores/": _CHECKOUT,
        "hooks/": _CHECKOUT,
        "generators/": _CHECKOUT,
        "mcp_tools/": _CHECKOUT,
        "selftest/": _CHECKOUT,
        "AGENTS.md": _CHECKOUT,
        "FUSESOC_IGNORE": _CHECKOUT,
        "goals/history/": _CHECKOUT,
        "tickets/history/": _CHECKOUT,
        # Checkout: tied to one checkout's sources.
        ".runtime/edalize/": _CHECKOUT,
        ".runtime/python-artifacts/": _CHECKOUT,
        "flow-reports/": _CHECKOUT,
        "mcp-tool-reports/": _CHECKOUT,
        "tmp/setup/": _CHECKOUT,
        # Checkout, written by other Booley modules.
        "pipeline.toml": _CHECKOUT,
        "tools/": _CHECKOUT,
        "rtl_style_guide.md": _CHECKOUT,
        "tb_style_guide.md": _CHECKOUT,
        # A Paired Checkout's own ``.git`` pointer file is per checkout; the main
        # Versioned Booley Directory's object store is reached through git, not this table.
        ".git/": _CHECKOUT,
        ".gitignore": _CHECKOUT,
        ".gitattributes": _CHECKOUT,
        ".managed/": _CHECKOUT,
        ".runtime/syn/": _CHECKOUT,
        ".runtime/simulation-run-locks/": _CHECKOUT,
        ".runtime/flow-console/": _CHECKOUT,
        ".runtime/mutation_tester/": _CHECKOUT,
        ".runtime/flow-reports/": _CHECKOUT,
        ".runtime/mcp-tool-reports/": _CHECKOUT,
        ".runtime/display.jsonl": _CHECKOUT,
        ".runtime/prompts/": _CHECKOUT,
        "tmp/fusesoc-isolated-cores/": _CHECKOUT,
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
        "tmp/doctor/": _COMMON,
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
        UnclassifiedBooleyPathError: the path is anchored, escapes with ``..``, or no
            table pattern covers it.
    """
    path = PurePath(relative)
    if path.anchor or ".." in path.parts:
        raise UnclassifiedBooleyPathError(f"not relative to the Booley Directory: {relative}")
    segments = tuple(segment for segment in path.parts if segment != ".")
    best = _best_part(segments)
    # An atomic write's temporary file (``.<name>.<pid>.tmp``) belongs to ``<name>``.
    for target in _temp_file_targets(segments[-1] if segments else ""):
        if best is not None:
            break
        best = _best_part((*segments[:-1], target))
    if best is None:
        raise UnclassifiedBooleyPathError(f"unclassified Booley Directory path: {relative}")
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


def _best_part(segments: tuple[str, ...]) -> BooleyDirPart | None:
    """Return the part of the deepest table pattern covering *segments*, if any."""
    best_depth = 0
    best: BooleyDirPart | None = None
    for pattern, part in BOOLEY_DIR_PATHS.items():
        depth = _covering_depth(pattern, segments)
        if depth is not None and depth > best_depth:
            best_depth, best = depth, part
    return best
