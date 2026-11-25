"""The review part of a Dashboard session line: the last Reviewed Commit of a checkout.

Pure reads, no Textual. A checkout whose Project enables the ``reviewed-commits`` Feature Flag
gets ``Reviewed <id>`` for the newest commit on the first-parent history from HEAD back to
the merge-base (the history the measurement part walks) that its stored Review Runs make a
Reviewed Commit.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

from booley.config.feature_flags import enabled_features, feature_enabled
from booley.core.booley_dir import main_booley_dir_of
from booley.harness.dashboard.measurement import SHORT, first_parent_history
from booley.review.review_run import load_review_runs, reviewed_by
from booley.runtime.pinned_history import FileCommitError, raw_git

FLAG = "reviewed-commits"


@dataclass(frozen=True)
class ReviewLine:
    """The review part of one checkout's line."""

    #: The stored state, such as ``Reviewed 1a2b3c4``.
    text: str
    #: The Markdown Review Report of the commit's newest Review Run, when that file exists.
    report: str | None = None


def review_lines(checkouts: Collection[str]) -> dict[str, ReviewLine]:
    """One part per checkout whose Project enables the flag and whose history holds a Reviewed Commit."""
    lines: dict[str, ReviewLine] = {}
    for checkout in checkouts:
        if not checkout:
            continue
        line = _line(Path(checkout))
        if line is not None:
            lines[checkout] = line
    return lines


def _line(checkout: Path) -> ReviewLine | None:
    """The part of *checkout*; ``None`` hides it, whatever Git or the run store cannot say."""
    try:
        if not feature_enabled(FLAG, enabled_features(checkout)):
            return None
        head = raw_git(checkout, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
        history = first_parent_history(checkout, head)
        store = main_booley_dir_of(checkout)
    except (FileCommitError, OSError, ValueError, RuntimeError):
        return None
    for commit, _tree in history:
        try:
            runs = load_review_runs(store, commit)
            reviewed = reviewed_by([document for _, document in runs])
        except (ValueError, KeyError):
            continue  # a damaged run counts as not Reviewed
        if reviewed is not None:
            report = runs[-1][0].with_suffix(".md")
            return ReviewLine(
                f"Reviewed {commit[:SHORT]}", str(report) if report.is_file() else None
            )
    return None
