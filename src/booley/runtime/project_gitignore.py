"""The ignore policy for ``.booley_project/``: transient Booley state Git must not track.

``booley init`` writes and backfills these patterns; Doctor warns when one is
missing.
"""

from __future__ import annotations

from fnmatch import fnmatchcase

# Inside ``.booley_project/`` we ignore transient state that should never be
# committed (tmp scratch, runtime logs, lockfiles).  ``.interactive_logs/`` is
# new in ADR 0012 — per-session transcripts written by the MCP server when an
# outer Claude Code / Codex tab calls Booley Flows and Specialists.
#
# Only *fixed-name*, Booley-owned local output paths belong here — patterns that are
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
# ``goals/*/`` and ``!goals/history/`` (ADR 0067): each Goal Record directory and
# the Goal lock directory are local working state, while ``goals/history/``
# holds the committed Goal summaries. The re-include must follow the pattern it
# overrides, so :func:`missing_gitignore_patterns` only counts it when it does.
#
# ``__pycache__/`` + ``*.pyc``: Project-authored Python lifecycle hooks may
# still run in ``.booley_project/hooks/``. The managed Git policy bundle is
# isolated under ``.booley_project/.managed/`` and runs from its zip archive.
# Ignore the generated bundle and temporary files from interrupted publication.
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
    "goals/*/",
    "!goals/history/",
    ".interactive_logs/",
    ".runtime/",
    "runtime/",
    "worktrees/",
    ".managed/",
    "__pycache__/",
    "*.pyc",
    "SETUP-REPORT.md",
    "FEEDBACK-REPORT.md",
    # Durable local evidence is transient to Git, including interrupted rewrites.
    "/reviewer-evidence/",
    "/findings.jsonl",
    "/findings.jsonl.tmp",
    "/BOOLEY-FEEDBACK.md",
    "/setup-evidence/",
    "/PARITY-REPORT.md",
)

# Each ``!`` re-include and the required pattern it overrides. Git applies the
# last matching line, so the re-include must follow that pattern.
REINCLUDES: dict[str, str] = {"!goals/history/": "goals/*/"}

PROJECT_GITIGNORE = "# Transient Booley state — do not commit.\n" + "".join(
    f"{pattern}\n" for pattern in PROJECT_GITIGNORE_PATTERNS
)


def _gitignore_line_key(line: str) -> str:
    """Normalize one ignore line so equivalent spellings compare equal.

    A pattern with a slash before its end is anchored to the ``.gitignore``
    directory either way, so ``/tickets/board/`` and ``tickets/board/`` match
    the same paths. A pattern without one (``worktrees/``) is not equivalent
    to its anchored form and keeps its spelling. A ``!`` re-include keeps its
    prefix and normalizes the pattern after it.
    """
    pattern = line.strip()
    negation = "!" if pattern.startswith("!") else ""
    pattern = pattern.removeprefix("!")
    body = pattern.removeprefix("/")
    return negation + (body if "/" in body.rstrip("/") else pattern)


def missing_gitignore_patterns(content: str) -> list[str]:
    """Return the required ignore patterns that *content* does not already cover.

    A ``!`` re-include only takes effect after the pattern it overrides
    (:data:`REINCLUDES`). It counts as present only
    when that pattern is present too and the re-include occurs after its last
    occurrence, so appending the reported patterns in order always leaves a
    working re-include.
    """
    keys = [_gitignore_line_key(line) for line in content.splitlines()]
    return [pattern for pattern in PROJECT_GITIGNORE_PATTERNS if not _covers(keys, pattern)]


def _covers(keys: list[str], pattern: str) -> bool:
    """Whether normalized *keys* cover the required *pattern*."""
    key = _gitignore_line_key(pattern)
    if key not in keys:
        return False
    overridden = REINCLUDES.get(pattern)
    return overridden is None or _reincludes_after(keys, key, overridden)


def _reincludes_after(keys: list[str], reinclude: str, overridden_pattern: str) -> bool:
    """Whether *reinclude* follows every line of the pattern it overrides."""
    overridden = _gitignore_line_key(overridden_pattern)
    overridden_positions = [position for position, key in enumerate(keys) if key == overridden]
    if not overridden_positions:
        return False
    last_reinclude = max(position for position, key in enumerate(keys) if key == reinclude)
    return last_reinclude > max(overridden_positions)


def project_transient_pattern(path: str) -> str | None:
    """Return the canonical rule responsible for excluding a file or ancestor.

    This handles the fixed directory/basename patterns and ordered re-includes
    in PROJECT_GITIGNORE_PATTERNS, independently of a Project's stale ignore file.
    The first excluded ancestor wins; its last matching positive rule is
    responsible because Git cannot re-include a child of an excluded directory.
    """
    parts = path.split("/")
    for end in range(1, len(parts) + 1):
        responsible = None
        for pattern in PROJECT_GITIGNORE_PATTERNS:
            body = pattern.lstrip("!").strip("/")
            if pattern.endswith("/") and end == len(parts):
                continue
            expected = body.split("/")
            candidate = (
                parts[:end]
                if "/" in body or pattern.lstrip("!").startswith("/")
                else [parts[end - 1]]
            )
            if len(candidate) == len(expected) and all(
                fnmatchcase(value, glob) for value, glob in zip(candidate, expected, strict=True)
            ):
                responsible = None if pattern.startswith("!") else pattern
        if responsible is not None:
            return responsible
    return None


def is_project_transient_path(path: str) -> bool:
    """Whether Booley's canonical ignore policy excludes a file or ancestor."""
    return project_transient_pattern(path) is not None
