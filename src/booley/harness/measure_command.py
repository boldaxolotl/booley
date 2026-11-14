"""``booley measure [<sha>]``: measure one commit in a clean checkout and store the run (#3953).

The command composes the measurement building blocks: it exports the commit, reads the
checkout's own ``metrics.toml``, preflights and runs one child Flow per rules table inside
the admission queue (:mod:`booley.harness.measure_children`), stores the Measurement Run in
the Main Booley Directory, writes the Markdown report and prints the report. The run is a
Standalone Job of its own, so the Dashboard shows it like any shell run.

Exit status: 0 when every enforced bound is met, 1 when one is unmet, 2 when a Flow errored,
when nothing was in scope or when the measurement failed before or while storing (nothing
is stored for a refusal), and 130 for a cancellation, which is stored as a cancelled run
when at least one child finished.

Cancellation has three phases. Before any child starts, a signal raises ``KeyboardInterrupt``
at once: every step cleans up after itself and nothing is stored. While the children run,
and after, a signal only sets a flag: the scheduler stops the children within its grace
period and the run is stored as cancelled, never as a Measured one.
"""

from __future__ import annotations

import json
import shutil
import signal
import sys
import tempfile
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType

from booley.harness.checkout_flow import CheckoutFlowError
from booley.harness.measure_children import (
    ChildrenOutcome,
    PreparedChild,
    prepare_children,
    run_children,
)
from booley.harness.standalone_job import (
    StandaloneJob,
    checkout_report_root,
    standalone_log_root,
)
from booley.measurement.base import BaseError, resolve_base
from booley.measurement.metrics_file import (
    MetricsFileError,
    MetricsRules,
    TableRules,
    load_metrics_rules,
)
from booley.measurement.protected_set import protected_set
from booley.measurement.report import terminal_report, write_markdown_report
from booley.measurement.run_document import MeasurementRun, RunStatus
from booley.measurement.run_key import KeyedCommit, RulesNotVersionedError, resolve_measurement_key
from booley.measurement.run_store import FlowReport, MeasurementStore
from booley.measurement.run_view import run_view
from booley.measurement.toolchain import ToolchainFingerprint, collect_toolchain_fingerprint
from booley.measurement.verdicts import measure_exit_code
from booley.runtime.commit_export import CommitExportError, export_commit
from booley.runtime.pinned_history import FileCommitError, raw_git
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import FuseSocError

NOTHING_IN_SCOPE = "nothing to measure, add a `[target.flow]` table"
_EXIT_REFUSED = 2
_CANCEL_NOTICE = "cancelling: stopping the running Flows (up to 30 s)"
_CANCEL_SIGNALS = ("SIGINT", "SIGTERM", "SIGHUP")


@dataclass(frozen=True)
class MeasureRequest:
    """What the user asked ``booley measure`` to do."""

    revision: str  # the user's <sha>; ``HEAD`` by default
    base: str | None  # --base
    all_tables: bool  # --all
    as_json: bool  # --json
    argv: tuple[str, ...]  # the user's arguments, for the Job record


class MeasureRefusedError(Exception):
    """The measurement cannot run or failed before any child started: exit 2, nothing stored."""


class _CancelSignals:
    """Turn SIGINT, SIGTERM and SIGHUP into a cancellation flag, or into an interrupt.

    While :attr:`interruptible` a signal raises ``KeyboardInterrupt`` (nothing to stop yet,
    and a hung step must end on the first signal); afterwards it only sets the flag the
    scheduler polls, so a second signal never loses a stored cancelled run.
    """

    def __init__(self) -> None:
        self.interruptible = True
        self._set = False

    def is_set(self) -> bool:
        return self._set

    def _on_signal(self, _signum: int, _frame: FrameType | None) -> None:
        if self.interruptible:
            raise KeyboardInterrupt
        if not self._set:
            print(_CANCEL_NOTICE, file=sys.stderr, flush=True)
        self._set = True


@contextmanager
def _cancel_signals() -> Iterator[_CancelSignals]:
    """Install the handlers for the run; the previous ones come back on exit."""
    signals = _CancelSignals()
    previous: dict[int, signal.Handlers | int | Callable[[int, FrameType | None], object] | None]
    previous = {}
    for name in _CANCEL_SIGNALS:
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            previous[number] = signal.signal(number, signals._on_signal)
        except ValueError:  # not the main thread: the caller cancels another way
            break
    try:
        yield signals
    finally:
        for number, handler in previous.items():
            signal.signal(number, signal.SIG_DFL if handler is None else handler)


def run_measure(project_root: Path, request: MeasureRequest) -> int:
    """Measure the commit *request* names as its own ``measure`` Job; return the exit status."""
    job = StandaloneJob.prepare(
        project_root,
        "measure",
        ["booley", "measure", *request.argv],
        report_root=checkout_report_root(project_root),
        own_run=True,
    )
    if job is None:
        print("ERROR: no Main Booley Directory to store the measurement in", file=sys.stderr)
        return _EXIT_REFUSED
    try:
        return job.run(lambda: _guarded(project_root, request, job.run_id))
    finally:
        job.discard()


def _guarded(project_root: Path, request: MeasureRequest, run_id: str) -> int:
    """Run the measurement; a refusal or a failure prints its error and exits 2.

    The text goes to stderr, which the Job copies into its ``job.log``. A
    ``KeyboardInterrupt`` passes through: the Job records it as cancelled.
    """
    try:
        with _cancel_signals() as signals:
            return _measure(project_root, request, run_id, signals)
    except MeasureRefusedError as refusal:
        print(f"ERROR: {refusal}", file=sys.stderr)
    except Exception as failure:  # noqa: BLE001 — the command boundary: infrastructure failures
        print(f"ERROR: measurement failed: {failure}", file=sys.stderr)
        traceback.print_exc()
    return _EXIT_REFUSED


def _resolve_commit(project_root: Path, request: MeasureRequest) -> str:
    """The full sha of the requested revision; ``--base`` is checked now, before any export."""
    revision = request.revision
    try:
        if revision.startswith("-"):
            raise FileCommitError(f"{revision!r} is not a revision")
        raw = raw_git(project_root, "rev-parse", "--verify", f"{revision}^{{commit}}")
    except FileCommitError as exc:
        raise MeasureRefusedError(f"{revision!r} is not a commit of this Project: {exc}") from exc
    commit = raw.decode("utf-8", "replace").strip()
    if request.base is not None:
        try:
            resolve_base(project_root, commit, request.base)
        except (FileCommitError, BaseError) as exc:
            raise MeasureRefusedError(f"--base {request.base!r} cannot be used: {exc}") from exc
    return commit


def _key_of(project_root: Path, commit: str, paired_commit: str | None = None) -> KeyedCommit:
    """The Measurement Key of *commit*, from the Booley Directory commit it was exported with."""
    try:
        return resolve_measurement_key(project_root, commit, booley_commit=paired_commit)
    except (RulesNotVersionedError, ValueError, FileCommitError) as exc:
        raise MeasureRefusedError(str(exc)) from exc


@dataclass(frozen=True)
class _Preflight:
    """Everything decided before any child starts."""

    protected: tuple[str, ...]
    toolchain: ToolchainFingerprint
    targets: list[str]
    prepared: list[tuple[TableRules, PreparedChild]]


def _load_rules(checkout: Path) -> MetricsRules:
    """The checkout's rules; a load error or no table at all is a refusal."""
    try:
        rules = load_metrics_rules(checkout)
    except MetricsFileError as exc:
        for error in exc.errors:
            print(error.render(), file=sys.stderr)
        raise MeasureRefusedError(str(exc)) from exc
    except OSError as exc:
        raise MeasureRefusedError(f"metrics.toml cannot be read: {exc}") from exc
    if not rules.tables:
        raise MeasureRefusedError(NOTHING_IN_SCOPE)
    return rules


def _preflight(checkout: Path, main: Path, run_id: str, rules: MetricsRules) -> _Preflight:
    """Preflight every table in *checkout*; a refusal for any starts no child."""
    try:
        protected = protected_set(checkout)
        toolchain = collect_toolchain_fingerprint()
        targets = [handle.identity for handle in TargetCatalog.build(checkout).list()]
        prepared = prepare_children(
            rules.tables,
            checkout=checkout,
            main_booley_dir=main,
            # Beside the Job's own log, so no child record names a path the scratch deletes.
            report_dir=standalone_log_root(main) / run_id / "children",
            timeout_s=None,
        )
    except (ValueError, FuseSocError) as exc:  # includes CheckoutFlowError, unpinnable inputs
        raise MeasureRefusedError(str(exc)) from exc
    return _Preflight(protected, toolchain, targets, prepared)


def _measure(
    project_root: Path, request: MeasureRequest, run_id: str, signals: _CancelSignals
) -> int:
    from booley.core.booley_dir import main_booley_dir_of

    started_at = datetime.now(UTC)
    started = time.monotonic()
    commit = _resolve_commit(project_root, request)
    _key_of(project_root, commit)  # a refusal now saves the export
    main = main_booley_dir_of(project_root)
    scratch = Path(tempfile.mkdtemp(prefix="booley-measure-"))
    try:
        checkout = scratch / "checkout"
        try:
            export = export_commit(project_root, commit, checkout)
        except CommitExportError as exc:
            raise MeasureRefusedError(f"the commit cannot be exported: {exc}") from exc
        keyed = _key_of(project_root, commit, export.paired_commit)
        rules = _load_rules(checkout)
        preflight = _preflight(checkout, main, run_id, rules)
        signals.interruptible = False  # the children run: a signal now cancels, never aborts
        try:
            outcome = run_children(
                preflight.prepared,
                rules.tables,
                main_booley_dir=main,
                all_tables=request.all_tables,
                cancelled=signals.is_set,
            )
        except CheckoutFlowError as exc:  # the first child could not be launched
            raise MeasureRefusedError(str(exc)) from exc
        flow_reports = _flow_reports(outcome)  # read before the status: a late signal counts
        status = RunStatus.CANCELLED if signals.is_set() else outcome.status
        if status is RunStatus.CANCELLED and not any(t.finished for t in outcome.tables):
            print("cancelled before any Flow finished; nothing stored", file=sys.stderr)
            raise KeyboardInterrupt
        run = MeasurementRun(
            run_id,
            keyed,
            status,
            started_at,
            datetime.now(UTC),
            time.monotonic() - started,
            rules,
            preflight.toolchain,
            preflight.protected,
            tuple(table.run for table in outcome.tables),
        )
        store = MeasurementStore(main)
        stored = store.write(run, flow_reports)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    view = run_view(
        project_root, store, stored, commit, base=request.base, targets=preflight.targets
    )
    write_markdown_report(view)
    if request.as_json:
        print(json.dumps(view, indent=2))
    else:
        print(terminal_report(view), end="")
    # A signal while storing leaves a complete run: the exit must agree with what is stored.
    if status is RunStatus.CANCELLED:
        raise KeyboardInterrupt
    return measure_exit_code(
        result.verdict for table in outcome.tables for result in table.run.results
    )


def _flow_reports(outcome: ChildrenOutcome) -> list[FlowReport]:
    """Every regular file (never a symlink) beside each table's final ``report.json``, it first."""
    reports: list[FlowReport] = []
    for table in outcome.tables:
        if not table.reports:
            continue
        directory = table.reports[-1].parent  # the final attempt's invocation directory
        files = sorted(
            path for path in directory.iterdir() if path.is_file() and not path.is_symlink()
        )
        files.sort(key=lambda path: path.name != table.reports[-1].name)
        reports.extend(FlowReport(table.run.identity, table.run.flow, path) for path in files)
    return reports
