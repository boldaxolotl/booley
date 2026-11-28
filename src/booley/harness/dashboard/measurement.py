"""The measurement line of a Dashboard session: what Git and the run store say about its checkout.

Pure reads, no Textual. A checkout that declares at least one ``[target.flow]`` table in its
``metrics.toml`` gets one line: ``Measured <id>`` when HEAD is Measured, else
``[last Measured <id> · ]latest <id>: <counts>``. The walk looks for the last Measured commit
on the first-parent history from HEAD back to the merge-base with the default branch, the
merge-base included. While a ``measure`` Job runs in the checkout the app shows
:func:`measuring_text` instead of the stored line.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from booley.core.booley_dir import main_booley_dir_of, resolve_checkout_booley_dir
from booley.core.boundary import as_int
from booley.harness.dashboard.jobs import JobView
from booley.mcp.session_registry import Attribution
from booley.measurement.base import BaseError, resolve_base
from booley.measurement.metrics_file import (
    METRICS_FILE,
    MetricsFileError,
    parse_metrics_as_written,
)
from booley.measurement.run_key import (
    MeasurementKey,
    RulesNotVersionedError,
    resolve_measurement_key,
)
from booley.measurement.run_store import MeasurementStore, MeasurementStoreError, StoredRun
from booley.runtime.pinned_history import FileCommitError, raw_git
from booley.runtime.timefmt import format_duration

#: Hex digits of a commit id shown in the line.
SHORT = 7
_COUNTED = (("error", "error"), ("unmet", "unmet"), ("not_evaluated", "not evaluated"))


@dataclass(frozen=True)
class MeasurementLine:
    """The line of one checkout."""

    #: The stored state, such as ``Measured 1a2b3c4``.
    text: str
    #: The Markdown Measurement Report of the run the text describes, when that file exists.
    report: str | None = None
    #: The running ``measure`` Job of the checkout, which replaces the text while it runs.
    measuring: JobView | None = None


def checkout_of(attribution: Attribution) -> str:
    """The checkout a session's calls target: its ``work_dir``, else its default one."""
    return attribution.work_dir or attribution.default_work_dir


def measuring_text(progress: Mapping[str, object] | None, elapsed_s: float | None) -> str:
    """``measuring 2/5 · 14m 00s``; without a readable count ``measuring · 14m 00s``."""
    counts: Mapping[str, object] = progress if isinstance(progress, Mapping) else {}
    finished = as_int(counts.get("finished"))
    total = as_int(counts.get("total"))
    count = f" {finished}/{total}" if finished is not None and total is not None else ""
    elapsed = "" if elapsed_s is None else f" · {format_duration(elapsed_s)}"
    return f"measuring{count}{elapsed}"


def measurement_lines(
    checkouts: Collection[str], jobs: Iterable[JobView]
) -> dict[str, MeasurementLine]:
    """One line per checkout that declares a table and whose HEAD Git can read."""
    running = [job for job in jobs if job.record.endpoint == "measure" and job.state == "running"]
    lines: dict[str, MeasurementLine] = {}
    for checkout in checkouts:
        if not checkout:
            continue
        stored = _stored_line(Path(checkout))
        if stored is not None:
            lines[checkout] = MeasurementLine(
                stored.text, stored.report, _running_in(checkout, running)
            )
    return lines


def _running_in(checkout: str, running: list[JobView]) -> JobView | None:
    """The newest running ``measure`` Job whose work directory is *checkout*."""
    here = _resolved(checkout)
    inside = [
        job
        for job in running
        if job.record.work_dir is not None and _resolved(job.record.work_dir) == here
    ]
    return max(inside, key=lambda job: job.record.started_at, default=None)


def _resolved(path: str) -> Path:
    try:
        return Path(path).resolve()
    except OSError:
        return Path(path)


def _declares_a_table(checkout: Path) -> bool:
    """Whether the checkout's ``metrics.toml`` loads and holds a ``[target.flow]`` table."""
    try:
        raw = (resolve_checkout_booley_dir(checkout) / METRICS_FILE).read_bytes()
        return bool(parse_metrics_as_written(raw).tables)
    except (OSError, ValueError, RuntimeError, MetricsFileError):
        return False


def _text(checkout: Path, *args: str) -> str:
    return raw_git(checkout, *args).decode("utf-8", "replace").strip()


def _head_key(checkout: Path, head: str) -> str | None:
    """The Booley Directory tree of HEAD's key; ``None`` when HEAD has no versioned rules."""
    try:
        return resolve_measurement_key(checkout, head).key.booley_tree
    except RulesNotVersionedError:
        return None


def first_parent_history(checkout: Path, head: str) -> list[tuple[str, str]]:
    """``(commit, tree)`` of the first-parent history from *head* to the merge-base, included."""
    try:
        stop = resolve_base(checkout, head).commit or head
    except BaseError:
        stop = head
    output = _text(checkout, "log", "--first-parent", "--format=%H %T", head, "--not", f"{stop}^@")
    return [(commit, tree) for commit, tree in (line.split() for line in output.splitlines())]


def _stored_line(checkout: Path) -> MeasurementLine | None:
    """The stored state of *checkout*; ``None`` hides the line."""
    if not _declares_a_table(checkout):
        return None
    try:
        head = _text(checkout, "rev-parse", "--verify", "HEAD^{commit}")
        booley_tree = _head_key(checkout, head)
        history = first_parent_history(checkout, head)
        store = MeasurementStore(main_booley_dir_of(checkout))
    except (FileCommitError, OSError, ValueError, RuntimeError):
        return None
    return _describe(store, head, booley_tree, history)


def _describe(
    store: MeasurementStore, head: str, booley_tree: str | None, history: list[tuple[str, str]]
) -> MeasurementLine:
    short = head[:SHORT]
    try:
        runs = store.history(MeasurementKey(history[0][1], booley_tree))
    except MeasurementStoreError:
        runs = None
    latest = runs[-1] if runs else None
    if latest is not None and latest.measured:
        return MeasurementLine(f"Measured {short}", _report(latest))
    state = (
        "unreadable run"
        if runs is None
        else ("not measured" if latest is None else _summary(latest))
    )
    ancestor = _measured_ancestor(store, history[1:], booley_tree)
    if ancestor is None:
        return MeasurementLine(
            f"latest {short}: {state}", None if latest is None else _report(latest)
        )
    commit, run = ancestor
    return MeasurementLine(
        f"last Measured {commit[:SHORT]} · latest {short}: {state}",
        _report(latest) if latest is not None else _report(run),
    )


def _measured_ancestor(
    store: MeasurementStore, ancestors: list[tuple[str, str]], booley_tree: str | None
) -> tuple[str, StoredRun] | None:
    """The newest ancestor whose key is Measured now; an unreadable store counts as not."""
    for commit, tree in ancestors:
        try:
            run = store.measured(MeasurementKey(tree, booley_tree))
        except MeasurementStoreError:
            continue
        if run is not None:
            return commit, run
    return None


def _summary(run: StoredRun) -> str:
    """The non-zero ``error``, ``unmet`` and ``not evaluated`` counts, else the run status."""
    try:
        verdicts = [r["verdict"] for table in run.document["tables"] for r in table["results"]]
    except (KeyError, TypeError):
        return "unreadable run"
    parts: list[str] = []
    for verdict, label in _COUNTED:
        count = verdicts.count(verdict)
        if count:
            parts.append(f"{count} {label}" + ("s" if verdict == "error" and count != 1 else ""))
    return ", ".join(parts) or run.status.value


def _report(run: StoredRun) -> str | None:
    return str(run.markdown) if run.markdown.is_file() else None
