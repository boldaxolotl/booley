"""The ignore policy for ``.booley_project/``: transient Booley state Git must not track.

``booley init`` writes and backfills these patterns; Doctor warns when one is
missing.
"""

from __future__ import annotations

# Inside ``.booley_project/`` we ignore transient state that should never be
# committed (tmp scratch, runtime logs, lockfiles).  ``.interactive_logs/`` is
# new in ADR 0012 — per-session transcripts written by the MCP server when an
# outer Claude Code / Codex tab calls Booley Flows and Specialists.
#
# Only *fixed-name*, Booley-owned transient dirs belong here — patterns that are
# correct for every project. ``flow-reports/`` is durable Flow evidence that is
# transient to Git. ``/logs/`` holds root-level pre-intake diagnostics, while
# ``/.baseline-wt-*/`` covers root-level temporary baseline worktrees whose
# best-effort cleanup may be interrupted. ``.runtime/`` (dotted) is the
# scratch/EDA build root (``resolve_project_dir()/".runtime"``, holds the multi-GB
# edalize tree);
# ``runtime/`` (no dot) is the container-lifetime bookkeeping dir — the doctor
# stamp (``runtime/doctor_stamp.json``), the developer probe, and the job-slot
# store all live there (F-6: it is a distinct dir from ``.runtime/``, not a
# typo — do not "dedupe" the two away).  ``worktrees/`` holds per-run git
# worktrees.  Project-configurable output dirs (``[flows.sim].output_dir``
# etc.) are deliberately NOT listed — they vary per project and often live
# outside ``.booley_project/``.
#
# ``tickets/board/`` and ``tickets/state/`` (ADR 0065): live Ticket documents
# and their state records are working state, so a Ticket worktree never carries
# a stale board copy and closing a Ticket leaves the checkout clean. Only
# ``tickets/history/`` (Closed Tickets) stays tracked. ``tickets/waiver-candidates/``
# (ADR 0066) holds per-Ticket Waiver Candidates, disposable until approval.
#
# ``__pycache__/`` + ``*.pyc``: Project-authored Python lifecycle hooks may
# still run in ``.booley_project/hooks/``. The managed Git policy bundle is
# isolated under ``.booley_project/.managed/`` and runs from its zip archive.
PROJECT_GITIGNORE_PATTERNS = (
    "tmp/",
    "flow-reports/",
    "/logs/",
    "/.baseline-wt-*/",
    "tickets/board/",
    "tickets/state/",
    "tickets/waiver-candidates/",
    "tickets/logs/",
    "tickets/locks/",
    ".interactive_logs/",
    ".runtime/",
    "runtime/",
    "worktrees/",
    "__pycache__/",
    "*.pyc",
    "SETUP-REPORT.md",
    "FEEDBACK-REPORT.md",
)

PROJECT_GITIGNORE = "# Transient Booley state — do not commit.\n" + "".join(
    f"{pattern}\n" for pattern in PROJECT_GITIGNORE_PATTERNS
)


def _gitignore_line_key(line: str) -> str:
    """Normalize one ignore line so equivalent spellings compare equal.

    A pattern with a slash before its end is anchored to the ``.gitignore``
    directory either way, so ``/tickets/board/`` and ``tickets/board/`` match
    the same paths. A pattern without one (``worktrees/``) is not equivalent
    to its anchored form and keeps its spelling.
    """
    pattern = line.strip()
    body = pattern.removeprefix("/")
    return body if "/" in body.rstrip("/") else pattern


def missing_gitignore_patterns(content: str) -> list[str]:
    """Return the required ignore patterns that *content* does not already cover."""
    present = {_gitignore_line_key(line) for line in content.splitlines()}
    return [p for p in PROJECT_GITIGNORE_PATTERNS if _gitignore_line_key(p) not in present]
