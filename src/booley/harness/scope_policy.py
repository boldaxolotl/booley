"""Ticket Scope policy -- the single owner of "what may this ticket commit?".

A Ticket's ``scope:`` list is the authorization boundary for automatic commits.
The Harness commits matching work, preserves other working-tree edits for
explicit triage, and never lets unrelated dirt prevent authorized work from
being saved.

Three tiers, in increasing severity:

``OWNED``
    The path matches the ticket's Scope.  Ordinary work, nothing recorded.
``ADVISORY``
    Any other file in the worktree.  Preserved uncommitted and reported clearly
    for triage to adjudicate.
``FORBIDDEN``
    Harness bookkeeping and the configuration the run is graded against.  Still
    hard-blocked, because these are not "work outside the plan" -- they are the
    plan, the state tracking it, and the switches deciding whether a Criterion
    passes.  An agent that can edit them can make a red run look green.

The forbidden set is deliberately narrow.  Everything an agent could plausibly
need in order to *do hardware work* -- RTL, testbenches, firmware, constraints,
``.core`` files, docs -- is advisory no matter how far outside Scope it sits.
"""

from __future__ import annotations

import enum
import json
import logging
from pathlib import Path

from .git_utils import (
    git_run,
    is_new_scope_entry,
    is_scope_unknown,
    scope_matches_dirty_file,
    scope_matches_file,
    strip_scope_new_tag,
)

logger = logging.getLogger(__name__)

DEVIATION_REPORT_NAME = "scope_deviations.json"
"""Runtime report naming every committed file the ticket's Scope did not."""


class ScopeTier(enum.Enum):
    """Which tier of the Scope policy a path falls into."""

    OWNED = "owned"
    ADVISORY = "advisory"
    FORBIDDEN = "forbidden"


# Harness bookkeeping the agent must never rewrite.  Prefixes are matched
# against forward-slash-normalized worktree-relative paths.
_FORBIDDEN_PREFIXES: tuple[str, ...] = (
    # Development state, Criteria, ticket files, run logs, worktrees, and
    # booley.toml all live here.  Editing any of them rewrites either the
    # acceptance record or the config it is measured against.
    ".booley_project/",
    # The legacy layout (`.booley/project/`, still resolved by
    # `core.config_paths`) plus the harness-vendored scripts under
    # `.booley/src/` that synthesis actually executes.
    ".booley/",
    ".git/",
)
# Known gap: `[project] dir` can relocate the project directory to an arbitrary
# name.  Resolving that needs config the standalone pre-commit hook cannot read,
# so a relocated project dir falls into ADVISORY -- reported, not blocked.

# ...except the parts of the project dir that are genuine project content
# rather than bookkeeping.  Stealth projects author their FuseSoC cores under
# `.booley_project/cores/` (ADR 0036) and their adapters beside them.  Project
# documentation also lives here by design; a ticket may legitimately update a
# memory map or interface contract alongside the implementation.
_FORBIDDEN_CARVE_OUTS: tuple[str, ...] = (
    ".booley_project/cores/",
    ".booley_project/adapters/",
    ".booley_project/docs/",
)

# Exact worktree-relative paths that are forbidden on their own.
_FORBIDDEN_EXACT: frozenset[str] = frozenset(
    {
        # Written by the Harness per run and consumed by the pre-commit hook;
        # an agent that rewrites it rewrites its own deviation record.
        ".scope.json",
    }
)


def _normalize(path: str) -> str:
    """Return *path* with forward slashes and no leading ``./``."""
    normalized = path.replace("\\", "/").strip()
    return normalized.removeprefix("./")


def is_forbidden_path(path: str) -> bool:
    """True when *path* is Harness bookkeeping the agent must not modify.

    Intentionally duplicated in ``dev_support.scope_precommit_hook`` -- that hook
    runs as a standalone script inside a worktree and cannot import Booley.
    """
    normalized = _normalize(path)
    if normalized in _FORBIDDEN_EXACT:
        return True
    if any(normalized.startswith(carve_out) for carve_out in _FORBIDDEN_CARVE_OUTS):
        return False
    return any(normalized.startswith(prefix) for prefix in _FORBIDDEN_PREFIXES)


def classify_path(scope: list[str], path: str, status: str | None = None) -> ScopeTier:
    """Classify one worktree-relative *path* against the ticket's *scope*.

    *status* is the two-character ``git status --porcelain`` code when the path
    came from a dirty-tree scan; it lets `` [new]`` Scope entries be matched
    with the same deletion rules the staging path uses.  Omit it for paths that
    came from a committed diff, where no status applies.

    The ``["*"]`` unknown-Scope sentinel carries no permission meaning here: a
    ticket that named no files has nothing authorized, so everything it touched
    needs explicit triage.
    """
    if is_forbidden_path(path):
        return ScopeTier.FORBIDDEN
    if is_scope_unknown([strip_scope_new_tag(entry) for entry in scope]):
        return ScopeTier.ADVISORY
    in_scope = (
        scope_matches_dirty_file(scope, path, status)
        if status is not None
        else scope_matches_file(scope, path)
    )
    return ScopeTier.OWNED if in_scope else ScopeTier.ADVISORY


def is_restore_artifact(scope: list[str], path: str, status: str) -> bool:
    """True for a deletion that looks like worktree fallout rather than work.

    A `` [new]`` Scope entry says "expect files to appear here".  When a file
    matching only such an entry turns up *deleted*, the likeliest author is not
    the agent but the harness -- sparse-worktree handling or a restore -- so
    committing that deletion would ship an accident as ticket work.

    An ordinary out-of-scope deletion (a file no `` [new]`` entry claims) is
    preserved for triage like any other unauthorized edit.
    """
    if "D" not in status[:2]:
        return False
    claimed_as_new = False
    for entry in scope:
        if not scope_matches_file([strip_scope_new_tag(entry)], path):
            continue
        if not is_new_scope_entry(entry):
            return False  # a real Scope entry owns it — an intentional deletion
        claimed_as_new = True
    return claimed_as_new


# ---------------------------------------------------------------------------
# End-of-run deviation report
# ---------------------------------------------------------------------------


def committed_deviations(
    worktree: Path, base_branch: str, scope: list[str]
) -> tuple[list[str], list[str]] | None:
    """Return ``(deviations, harness_paths)`` committed on the branch, or ``None``.

    The two are separated because they mean different things.  A *deviation* is
    the agent working outside the plan, which triage adjudicates.  A
    *harness path* on the branch means the FORBIDDEN tier leaked -- usually the
    setup stage's own ``--no-verify`` commit, which is expected noise rather
    than agent behaviour, and folding it in would have triage chasing the
    harness's own bookkeeping.

    This is the authoritative check, and it deliberately reads the *committed*
    branch rather than replaying whatever the pre-commit hook happened to see:
    the branch is what review and merge act on, so it is the only surface where
    "what did this ticket actually change" has a single answer.

    Undecidable (missing base branch, git failure) yields ``None`` rather than
    an empty list -- "no deviations" and "could not tell" must not look alike
    in triage.
    """
    # ``-z`` because the default output C-quotes any path with a space or a
    # non-ASCII byte, and a quoted path defeats every prefix test below.
    result = git_run(worktree, ["diff", "--name-only", "-z", f"{base_branch}...HEAD"], timeout=30)
    if result.returncode != 0:
        logger.warning(
            "Cannot diff %s...HEAD for the Scope deviation report: %s",
            base_branch,
            (result.stderr or result.stdout or "").strip()[:200],
        )
        return None
    deviations: list[str] = []
    harness_paths: list[str] = []
    for path in (p for p in result.stdout.split("\0") if p):
        tier = classify_path(scope, path)
        if tier is ScopeTier.ADVISORY:
            deviations.append(path)
        elif tier is ScopeTier.FORBIDDEN:
            harness_paths.append(path)
    return deviations, harness_paths


def write_deviation_report(
    report_path: Path,
    *,
    slug: str,
    base_branch: str,
    scope: list[str],
    result: tuple[list[str], list[str]] | None,
) -> None:
    """Write the run's Scope deviation report.

    Always written, including the clean case: triage needs to distinguish "the
    ticket stayed inside its Scope" from "nobody checked".  *result* is what
    :func:`committed_deviations` returned -- ``None`` when undecidable.
    """
    deviations, harness_paths = result if result is not None else ([], [])
    payload = {
        "slug": slug,
        "base_branch": base_branch,
        "scope": scope,
        "decidable": result is not None,
        "deviations": deviations,
        "harness_paths": harness_paths,
    }
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        # Informational only -- a report we cannot write must never take down a
        # run that otherwise succeeded.
        logger.warning("Could not write the Scope deviation report to %s: %s", report_path, exc)
