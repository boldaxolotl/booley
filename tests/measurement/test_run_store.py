"""The Measurement Run store: key, runs of a key, Measured, base lookup, reports."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from booley.core.booley_dir import reset_cache, resolve_common_booley_dir
from booley.flows.metric_declarations import FlowMetricValues
from booley.measurement.metrics_file import Bound, MetricsRules, TableRules, TrackedMetric
from booley.runtime.project_gitignore import PROJECT_GITIGNORE

if TYPE_CHECKING:
    from collections.abc import Iterator

    from booley.measurement.run_document import MeasurementRun
    from booley.measurement.run_key import KeyedCommit
    from booley.measurement.toolchain import ToolchainFingerprint
from tests.runtime.test_commit_export import (
    commit_all,
    git,
    make_versioned_booley_directory,
    new_repository,
    write,
)

IDENTITY = "acme:ip:x:1.0#syn"
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def no_ambient_booley_dir(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Drop the env override and the process-wide Booley Directory cache another test warmed."""
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    reset_cache()
    yield
    reset_cache()


def in_tree_repository(root: Path) -> tuple[Path, str]:
    """An RTL repository whose Booley Directory is tracked; returns it with HEAD."""
    repo = new_repository(root)
    write(repo / "rtl.v", "module a; endmodule\n")
    write(repo / ".booley" / "booley.toml", "[project]\n")
    write(repo / ".booley" / ".gitignore", PROJECT_GITIGNORE)
    return repo, commit_all(repo)


def rules(*, with_tracked: bool = False) -> MetricsRules:
    bound = Bound("cell_count_max", "cell_count", "max", 100, None, None, None)
    tracked = (TrackedMetric("area", "area", None, None),) if with_tracked else ()
    table = TableRules("syn", IDENTITY, "syn", (bound,), tracked)
    return MetricsRules(Path("metrics.toml"), (table,))


def fingerprint(booley: str = "sha256:a") -> ToolchainFingerprint:
    from booley.measurement.toolchain import ToolchainFingerprint

    return ToolchainFingerprint(None, booley, "build", None, {"yosys": "0.1"})


def make_run(
    keyed: KeyedCommit,
    run_id: str,
    *,
    minute: int = 0,
    start_minute: int | None = None,
    status: str = "completed",
    verdict: str = "met",
    toolchain: ToolchainFingerprint | None = None,
    per_clock: dict[str, dict[str, float]] | None = None,
    omit_results: bool = False,
) -> MeasurementRun:
    from booley.measurement.run_document import (
        MeasurementRun,
        MetricResult,
        RunStatus,
        TableRun,
        Verdict,
    )

    results = (
        () if omit_results else (MetricResult("cell_count_max", 100, 90, Verdict(verdict), None),)
    )
    values = FlowMetricValues({}, per_clock or {})
    table = TableRun(IDENTITY, "syn", 1.5, values, results)
    finished = EPOCH + timedelta(minutes=minute)
    started = finished - timedelta(seconds=30)
    if start_minute is not None:
        started = EPOCH + timedelta(minutes=start_minute)
    return MeasurementRun(
        run_id,
        keyed,
        RunStatus(status),
        started,
        finished,
        30.0,
        rules(),
        toolchain or fingerprint(),
        (),
        (table,),
    )


def key_of(repo: Path, commit: str) -> KeyedCommit:
    from booley.measurement.run_key import resolve_measurement_key

    return resolve_measurement_key(repo, commit)


def test_amended_commit_with_same_tree_finds_the_run_and_a_changed_file_does_not(
    tmp_path: Path,
) -> None:
    from booley.measurement.run_store import MeasurementStore

    repo, head = in_tree_repository(tmp_path / "repo")
    store = MeasurementStore(repo / ".booley")
    first = key_of(repo, head)
    store.write(make_run(first, "r1"), [])

    git(repo, "commit", "-q", "--amend", "-m", "reworded")
    amended = key_of(repo, git(repo, "rev-parse", "HEAD"))
    write(repo / "rtl.v", "module b; endmodule\n")
    changed = key_of(repo, commit_all(repo))

    assert amended.commit != first.commit
    assert amended.key == first.key
    found = store.measured(amended.key)
    assert found is not None and found.run_id == "r1"
    assert changed.key != first.key
    assert store.history(changed.key) == []


def test_linked_worktree_finds_the_main_checkouts_run_and_git_status_is_clean(
    tmp_path: Path,
) -> None:
    from booley.measurement.run_store import MeasurementStore

    repo, head = in_tree_repository(tmp_path / "repo")
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(linked), head)
    keyed = key_of(repo, head)

    main_store = MeasurementStore(resolve_common_booley_dir(repo))
    main_store.write(make_run(keyed, "r1"), [])
    # In real use the linked worktree is another process: it starts with a cold cache.
    reset_cache()
    linked_booley_dir = resolve_common_booley_dir(linked)
    assert linked_booley_dir == repo / ".booley"
    linked_store = MeasurementStore(linked_booley_dir)

    found = linked_store.measured(keyed.key)
    assert found is not None and found.run_id == "r1"
    assert found.path.is_relative_to(repo / ".booley" / "measurements")
    for checkout in (repo, linked):
        assert git(checkout, "status", "--porcelain", "--untracked-files=all") == ""


def test_every_run_of_a_key_is_kept_and_measured_follows_the_latest(tmp_path: Path) -> None:
    from booley.measurement.run_store import MeasurementStore

    repo, head = in_tree_repository(tmp_path / "repo")
    store = MeasurementStore(repo / ".booley")
    keyed = key_of(repo, head)

    # Run ids are random in real use: order must follow finish time, never the id.
    store.write(make_run(keyed, "c", minute=1), [])
    store.write(make_run(keyed, "b", minute=2, verdict="unmet"), [])

    assert [run.run_id for run in store.history(keyed.key)] == ["c", "b"]
    assert store.measured(keyed.key) is None
    store.write(make_run(keyed, "a", minute=3), [])
    latest = store.measured(keyed.key)
    assert latest is not None and latest.run_id == "a"
    # A run that finished earlier is history, not the latest, whatever order it was written in.
    store.write(make_run(keyed, "d", minute=0, verdict="unmet"), [])
    assert [run.run_id for run in store.history(keyed.key)] == ["d", "c", "b", "a"]
    latest = store.measured(keyed.key)
    assert latest is not None and latest.run_id == "a"
    # Overlapping runs: the one that started first but finished last is the latest.
    store.write(make_run(keyed, "e", minute=4, start_minute=-10, verdict="unmet"), [])
    assert [run.run_id for run in store.history(keyed.key)][-1] == "e"
    assert store.measured(keyed.key) is None


def test_stopped_and_cancelled_runs_are_stored_but_not_measured(tmp_path: Path) -> None:
    from booley.measurement.run_store import MeasurementStore

    for status in ("stopped", "cancelled"):
        repo, head = in_tree_repository(tmp_path / status)
        store = MeasurementStore(repo / ".booley")
        keyed = key_of(repo, head)
        store.write(make_run(keyed, "r1", minute=1), [])
        assert store.measured(keyed.key) is not None

        stored = store.write(make_run(keyed, "r2", minute=2, status=status), [])

        assert stored.status.value == status and not stored.measured
        assert [run.run_id for run in store.history(keyed.key)] == ["r1", "r2"]
        assert store.measured(keyed.key) is None


def test_rule_change_in_versioned_booley_directory_changes_the_key_but_not_the_base_run(
    tmp_path: Path,
) -> None:
    from booley.measurement.run_store import MeasurementStore

    repo, booley = make_versioned_booley_directory(tmp_path, "standalone")
    head = git(repo, "rev-parse", "HEAD")
    store = MeasurementStore(booley)
    before = key_of(repo, head)
    store.write(make_run(before, "r1"), [])

    write(booley / "metrics.toml", "[syn]\n")
    git(booley, "add", "metrics.toml")
    git(booley, "commit", "-q", "-m", "rule")
    after = key_of(repo, head)

    assert after.key.project_tree == before.key.project_tree
    assert after.key.booley_tree != before.key.booley_tree
    assert after.booley_commit == git(booley, "rev-parse", "HEAD")
    assert store.measured(after.key) is None
    base = store.base_run(after.key.project_tree)
    assert base is not None and base.run_id == "r1"
    assert base.document["booley_commit"] == before.booley_commit

    # The base is the latest run of the project tree whatever its rules tree or status.
    store.write(make_run(after, "r2", minute=1, status="stopped"), [])
    base = store.base_run(after.key.project_tree)
    assert base is not None and base.run_id == "r2"
    assert base.document["booley_commit"] == after.booley_commit


def test_stored_key_follows_the_exported_booley_commit(tmp_path: Path) -> None:
    from booley.measurement.run_key import resolve_measurement_key

    repo, booley = make_versioned_booley_directory(tmp_path, "standalone")
    head = git(repo, "rev-parse", "HEAD")
    exported = key_of(repo, head)
    write(booley / "metrics.toml", "[syn]\n")
    git(booley, "add", "metrics.toml")
    git(booley, "commit", "-q", "-m", "moved on")

    pinned = resolve_measurement_key(repo, head, booley_commit=exported.booley_commit)

    assert pinned.key == exported.key
    assert key_of(repo, head).key != exported.key
    plain, plain_head = in_tree_repository(tmp_path / "plain")
    with pytest.raises(ValueError, match="needs a Versioned Booley Directory"):
        resolve_measurement_key(plain, plain_head, booley_commit=plain_head)


def test_unversioned_booley_directory_without_commits_is_refused(tmp_path: Path) -> None:
    from booley.measurement.run_key import RulesNotVersionedError

    repo, _ = make_versioned_booley_directory(tmp_path, "standalone")
    empty = new_repository(tmp_path / "empty-rules")
    (repo / ".booley").rename(tmp_path / "moved")
    empty.rename(repo / ".booley")

    with pytest.raises(RulesNotVersionedError, match="no commit"):
        key_of(repo, git(repo, "rev-parse", "HEAD"))


def _gitignored_booley_directory(tmp_path: Path) -> tuple[Path, str]:
    repo = new_repository(tmp_path / "repo")
    write(repo / ".gitignore", ".booley/\n")
    write(repo / "rtl.v", "x\n")
    write(repo / ".booley" / "booley.toml", "[project]\n")
    git(repo, "add", ".gitignore", "rtl.v")  # the ignored Booley Directory stays untracked
    return repo, commit_all(repo, staged_only=True)


def _outside_booley_directory(tmp_path: Path) -> tuple[Path, str]:
    repo = new_repository(tmp_path / "repo")
    write(repo / "booley.toml", f'[project]\ndir = "{(tmp_path / "elsewhere").as_posix()}"\n')
    write(repo / "rtl.v", "x\n")
    return repo, commit_all(repo)


def test_unversioned_rules_fail_with_rules_must_be_versioned(tmp_path: Path) -> None:
    from booley.measurement.run_key import RulesNotVersionedError

    for name, layout in (
        ("ignored", _gitignored_booley_directory),
        ("outside", _outside_booley_directory),
    ):
        repo, head = layout(tmp_path / name)

        with pytest.raises(RulesNotVersionedError, match="rules must be versioned"):
            key_of(repo, head)


def test_booley_directory_place_is_read_from_the_commit(tmp_path: Path) -> None:
    from booley.measurement.run_key import RulesNotVersionedError

    repo = new_repository(tmp_path / "repo")
    write(repo / "booley.toml", '[project]\ndir = "cfg/booley"\n')
    write(repo / "cfg" / "booley" / "booley.toml", "[project]\n")
    old = commit_all(repo)
    git(repo, "mv", "cfg/booley", "moved")
    write(repo / "booley.toml", '[project]\ndir = "moved"\n')
    commit_all(repo)
    write(repo / "booley.toml", "[project\n")  # unreadable: the default place, not tracked
    unreadable = commit_all(repo)

    # The live configuration now selects "moved"; the old commit still resolves by its own.
    assert key_of(repo, old).commit == old
    with pytest.raises(RulesNotVersionedError, match=r"\.booley/booley\.toml is not in commit"):
        key_of(repo, unreadable)


def test_toolchain_difference_keeps_the_key(tmp_path: Path) -> None:
    from booley.measurement.run_store import MeasurementStore

    repo, head = in_tree_repository(tmp_path / "repo")
    store = MeasurementStore(repo / ".booley")
    keyed = key_of(repo, head)

    store.write(make_run(keyed, "r1", minute=1, toolchain=fingerprint("sha256:a")), [])
    store.write(make_run(keyed, "r2", minute=2, toolchain=fingerprint("sha256:b")), [])

    runs = store.history(keyed.key)
    assert [run.document["toolchain"]["booley"] for run in runs] == ["sha256:a", "sha256:b"]
    assert runs[0].document["toolchain"]["tools"] == {"yosys": "0.1"}
    latest = store.measured(keyed.key)
    assert latest is not None and latest.run_id == "r2"


def test_reports_beyond_the_cap_are_dropped_and_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.measurement import run_store
    from booley.measurement.run_store import FlowReport, MeasurementStore

    repo, head = in_tree_repository(tmp_path / "repo")
    store = MeasurementStore(repo / ".booley")
    monkeypatch.setattr(run_store, "REPORT_CAP_BYTES", 10)
    sources = tmp_path / "sources"
    for name, size in (("a.rpt", 4), ("big.rpt", 20), ("c.rpt", 5)):
        write(sources / name, "x" * size)
    reports = [FlowReport(IDENTITY, "syn", sources / n) for n in ("a.rpt", "big.rpt", "c.rpt")]

    stored = store.write(make_run(key_of(repo, head), "r1"), reports)

    assert [entry["name"] for entry in stored.document["reports"]] == ["a.rpt", "c.rpt"]
    assert stored.document["dropped_reports"] == [
        {"identity": IDENTITY, "flow": "syn", "name": "big.rpt", "bytes": 20}
    ]
    for entry in stored.document["reports"]:
        copy = stored.path.parent / entry["path"]
        assert copy.read_bytes() == (sources / entry["name"]).read_bytes()
    assert not (stored.path.parent / "r1" / "0-syn" / "big.rpt").exists()


def test_duplicate_report_and_repeated_run_id_are_refused(tmp_path: Path) -> None:
    from booley.measurement.run_store import FlowReport, MeasurementStore
    from booley.runtime.atomic_files import WriteOnceConflictError

    repo, head = in_tree_repository(tmp_path / "repo")
    store = MeasurementStore(repo / ".booley")
    keyed = key_of(repo, head)
    report = FlowReport(IDENTITY, "syn", write(tmp_path / "a.rpt", "x"))

    with pytest.raises(ValueError, match="duplicate report"):
        store.write(make_run(keyed, "r1"), [report, report])
    assert store.history(keyed.key) == []
    store.write(make_run(keyed, "r1"), [report])
    with pytest.raises(WriteOnceConflictError):
        store.write(make_run(keyed, "r1"), [])


@pytest.mark.parametrize(
    "flaw",
    [
        "missing-result",
        "unknown-table",
        "stray-table",
        "bound-as-tracked",
        "bound-drift",
        "tracked-with-bound",
        "duplicate-table",
        "met-without-value",
        "naive-time",
    ],
)
def test_write_rejects_a_run_whose_results_do_not_cover_its_rules(
    tmp_path: Path, flaw: str
) -> None:
    from dataclasses import replace

    from booley.measurement.run_document import MetricResult, Verdict
    from booley.measurement.run_store import MeasurementStore

    repo, head = in_tree_repository(tmp_path / "repo")
    store = MeasurementStore(repo / ".booley")
    run = make_run(key_of(repo, head), "r1", omit_results=flaw == "missing-result")
    table = run.tables[0]
    if flaw == "unknown-table":
        run = replace(run, tables=())
    if flaw == "stray-table":
        run = replace(run, tables=(*run.tables, replace(table, flow="sim")))
    if flaw == "duplicate-table":
        # The first, unmet copy must not be hidden by a met second one.
        unmet = replace(table, results=(replace(table.results[0], verdict=Verdict.UNMET),))
        run = replace(run, tables=(unmet, table))
    if flaw in ("bound-as-tracked", "bound-drift", "met-without-value"):
        change = {
            "bound-as-tracked": {"bound": None, "verdict": Verdict.TRACKED},
            "bound-drift": {"bound": 5},
            "met-without-value": {"value": None},
        }[flaw]
        run = replace(
            run, tables=(replace(table, results=(replace(table.results[0], **change),)),)
        )
    if flaw == "tracked-with-bound":
        run = replace(run, rules=rules(with_tracked=True))
        tracked = MetricResult("area", 5, 7, Verdict.TRACKED, None)
        run = replace(run, tables=(replace(table, results=(*table.results, tracked)),))
    if flaw == "naive-time":
        run = replace(run, finished_at=run.finished_at.replace(tzinfo=None))

    with pytest.raises(ValueError):
        store.write(run, [])

    assert not (repo / ".booley" / "measurements").exists()


def test_unreadable_document_fails_the_lookup(tmp_path: Path) -> None:
    from booley.measurement.run_store import MeasurementStore, MeasurementStoreError

    repo, head = in_tree_repository(tmp_path / "repo")
    store = MeasurementStore(repo / ".booley")
    keyed = key_of(repo, head)
    store.write(make_run(keyed, "r1", minute=1), [])
    broken = repo / ".booley" / "measurements" / keyed.key.project_tree / "r2.json"
    broken.write_bytes(b'{"schema": 2}\n')

    with pytest.raises(MeasurementStoreError, match=r"r2\.json"):
        store.measured(keyed.key)
    assert store.clock_names(IDENTITY, "syn") == ()


def test_clock_names_come_from_the_latest_run_that_has_them(tmp_path: Path) -> None:
    from booley.measurement.run_store import MeasurementStore

    repo, head = in_tree_repository(tmp_path / "repo")
    store = MeasurementStore(repo / ".booley")
    keyed = key_of(repo, head)
    assert store.clock_names(IDENTITY, "syn") == ()
    # Neither ascending nor descending id order, nor start order, picks the newest run.
    store.write(make_run(keyed, "a", minute=1, per_clock={"fmax": {"old_a": 1.0}}), [])
    store.write(make_run(keyed, "c", minute=2, per_clock={"fmax": {"old_c": 1.0}}), [])
    store.write(
        make_run(keyed, "b", minute=3, start_minute=-10, per_clock={"fmax": {"clk_i": 2.0}}), []
    )
    store.write(make_run(keyed, "d", minute=4), [])

    assert store.clock_names(IDENTITY, "syn") == ("clk_i",)
    assert store.clock_names(IDENTITY, "sim") == ()


def test_tracked_metric_and_empty_results_decide_measured(tmp_path: Path) -> None:
    from dataclasses import replace

    from booley.measurement.run_document import MetricResult, Verdict
    from booley.measurement.run_store import MeasurementStore

    repo, head = in_tree_repository(tmp_path / "repo")
    store = MeasurementStore(repo / ".booley")
    keyed = key_of(repo, head)
    tracked_rules = rules(with_tracked=True)
    run = make_run(keyed, "r1")
    table = replace(
        run.tables[0],
        results=(
            *run.tables[0].results,
            MetricResult("area", None, 3.0, Verdict.TRACKED, None),
        ),
    )

    assert store.write(replace(run, rules=tracked_rules, tables=(table,)), []).measured
    errored = replace(
        table, results=(*table.results[:1], MetricResult("area", None, None, Verdict.ERROR, "x"))
    )
    assert not store.write(
        replace(run, run_id="r2", rules=tracked_rules, tables=(errored,)), []
    ).measured
    mismatched = replace(
        table, results=(MetricResult("cell_count_max", 100, 90, Verdict.TRACKED, None),)
    )
    assert not store.write(replace(run, run_id="r4", tables=(mismatched,)), []).measured
    empty = replace(run, run_id="r3", rules=MetricsRules(Path("metrics.toml"), ()), tables=())
    assert not store.write(empty, []).measured


def test_failed_publication_leaves_no_report_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.measurement import run_store
    from booley.measurement.run_store import FlowReport, MeasurementStore

    repo, head = in_tree_repository(tmp_path / "repo")
    store = MeasurementStore(repo / ".booley")
    keyed = key_of(repo, head)
    report = FlowReport(IDENTITY, "syn", write(tmp_path / "a.rpt", "x"))

    def refuse(_path: Path, _content: bytes) -> bool:
        raise OSError("disk full")

    monkeypatch.setattr(run_store, "atomic_write_once", refuse)

    with pytest.raises(OSError, match="disk full"):
        store.write(make_run(keyed, "r1"), [report])

    assert not (repo / ".booley" / "measurements" / keyed.key.project_tree / "r1").exists()


def test_lost_publication_race_keeps_the_winners_report_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.measurement import run_store
    from booley.measurement.run_store import FlowReport, MeasurementStore
    from booley.runtime.atomic_files import WriteOnceConflictError

    repo, head = in_tree_repository(tmp_path / "repo")
    store = MeasurementStore(repo / ".booley")
    keyed = key_of(repo, head)
    report = FlowReport(IDENTITY, "syn", write(tmp_path / "a.rpt", "x"))

    def lose(_path: Path, _content: bytes) -> bool:
        raise WriteOnceConflictError("another writer published this run")

    monkeypatch.setattr(run_store, "atomic_write_once", lose)

    with pytest.raises(WriteOnceConflictError):
        store.write(make_run(keyed, "r1"), [report])

    assert (repo / ".booley" / "measurements" / keyed.key.project_tree / "r1").is_dir()


def test_report_of_no_rules_table_is_refused(tmp_path: Path) -> None:
    from booley.measurement.run_store import FlowReport, MeasurementStore

    repo, head = in_tree_repository(tmp_path / "repo")
    store = MeasurementStore(repo / ".booley")
    report = FlowReport(IDENTITY, "sim", write(tmp_path / "a.rpt", "x"))

    with pytest.raises(ValueError, match="no rules table"):
        store.write(make_run(key_of(repo, head), "r1"), [report])


def test_non_commit_booley_commit_and_non_text_timestamp_are_refused(tmp_path: Path) -> None:
    from booley.measurement.run_key import resolve_measurement_key
    from booley.measurement.run_store import MeasurementStore, MeasurementStoreError
    from booley.runtime.pinned_history import FileCommitError

    repo, booley = make_versioned_booley_directory(tmp_path, "standalone")
    head = git(repo, "rev-parse", "HEAD")
    with pytest.raises(FileCommitError, match="full canonical"):
        resolve_measurement_key(repo, head, booley_commit="HEAD")

    store = MeasurementStore(booley)
    keyed = key_of(repo, head)
    stored = store.write(make_run(keyed, "r1"), [])
    stored.path.write_text(
        stored.path.read_text(encoding="utf-8").replace(
            '"finished_at": "', '"finished_at": 7, "x": "'
        ),
        encoding="utf-8",
    )
    with pytest.raises(MeasurementStoreError, match=r"r1\.json"):
        store.measured(keyed.key)
