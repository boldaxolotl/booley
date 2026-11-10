"""``booley worktree gc``: remove idle worktrees with ``rm``'s checks, never forced.

A worktree is idle when its last-used stamp is older than the age (``--older-than``,
else ``[worktree].gc_older_than``, default 7 days). Every other outcome keeps the
worktree and says why: no stamp, too recent, you are inside it, or ``rm`` refuses
it (live Session, active Goal Mode, unsaved work, damaged checkout, ...). ADR 0082.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from booley.config.worktree import GcAge, gc_older_than
from booley.core.booley_dir import resolve_common_booley_dir, resolve_main_checkout_root
from booley.harness.worktree_list import Registration, interactive_registrations
from booley.harness.worktree_rm import (
    REMOVAL_FAILURES,
    WorktreeRemovalBlockedError,
    WorktreeRemovalRefusedError,
    remove_worktree,
    report_removal,
    running_inside,
)
from booley.runtime.timefmt import format_age
from booley.runtime.worktree_usage import last_used


@dataclass(frozen=True)
class GcOutcome:
    """What ``gc`` did with one worktree, as the text to print."""

    status: Literal["removed", "kept", "failed"]
    text: str


def _kept(name: str, reason: str) -> GcOutcome:
    return GcOutcome("kept", f"Kept worktree {name}: {reason}")


def _collect_one(
    project_root: Path, registration: Registration, common: Path, age: GcAge, now: float
) -> GcOutcome:
    """Outcome for one worktree; an error becomes a ``failed`` outcome so the run goes on."""
    try:
        return _decide(project_root, registration, common, age, now)
    except REMOVAL_FAILURES as exc:
        return GcOutcome("failed", f"could not remove {registration.path.name}: {exc}")


def _decide(
    project_root: Path, registration: Registration, common: Path, age: GcAge, now: float
) -> GcOutcome:
    """Decide on one worktree and, when it is idle, try the non-forced removal."""
    path = registration.path
    name = path.name
    if running_inside(project_root, path):
        return _kept(name, "you are inside it; run gc from the main checkout or another worktree")
    stamp = last_used(common, name)
    if stamp is None:
        return _kept(name, "no last-used record, so its idle time is unknown")
    if now - stamp <= age.seconds:
        return _kept(name, f"last used {format_age(stamp, now=now)}, not older than {age.text}")
    try:
        removal = remove_worktree(project_root, name, force=False)
    except WorktreeRemovalRefusedError as refusal:
        reasons = "".join(f"\n  - {reason}" for reason in refusal.reasons)
        earlier = "".join(
            f"\n  An earlier attempt already moved evidence to: {path}"
            for path in refusal.earlier_evidence
        )
        return _kept(name, f"refused{reasons}{earlier}")
    except WorktreeRemovalBlockedError as blocked:
        return _kept(name, f"refused\n  - {blocked}")
    return GcOutcome("removed", report_removal(name, removal))


def collect_garbage(project_root: Path, age: GcAge, *, now: float) -> list[GcOutcome]:
    """Remove every idle, safely removable worktree; return one outcome per worktree."""
    common = resolve_common_booley_dir(project_root)
    main = resolve_main_checkout_root(project_root)
    return [
        _collect_one(project_root, registration, common, age, now)
        for registration in interactive_registrations(main, common)
    ]


def render(outcomes: list[GcOutcome]) -> str:
    """One block per worktree, then the totals; ``No worktrees.`` when there are none."""
    if not outcomes:
        return "No worktrees."
    removed, kept, failed = (
        sum(outcome.status == status for outcome in outcomes)
        for status in ("removed", "kept", "failed")
    )
    summary = f"Removed {removed} worktree(s); kept {kept}"
    if failed:
        summary += f"; failed {failed}"
    return "\n".join([*(outcome.text for outcome in outcomes), summary + "."])


def run(args: argparse.Namespace, project_root: Path) -> int:
    """Print what was removed and kept; exit 1 on a bad config, a listing failure, or an error."""
    try:
        age = args.older_than
        if age is None:
            age = gc_older_than(resolve_common_booley_dir(project_root))
        outcomes = collect_garbage(project_root, age, now=time.time())
    except REMOVAL_FAILURES as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(render(outcomes))
    return 1 if any(outcome.status == "failed" for outcome in outcomes) else 0
