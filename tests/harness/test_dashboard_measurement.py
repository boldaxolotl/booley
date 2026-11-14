"""The Dashboard session line for Measured Commits (#3509).

Real temporary Git repositories and a real measurement store feed the reader; the app
tests drive ``DashboardApp`` with a fixed snapshot.
"""

from __future__ import annotations

import dataclasses
import importlib
from pathlib import Path

import pytest

from booley.harness.dashboard import app as dashboard_app
from booley.harness.dashboard import model, sessions
from booley.harness.dashboard.app import DashboardApp
from booley.harness.dashboard.jobs import JobSnapshot, JobView
from booley.measurement.run_document import Verdict
from booley.measurement.run_store import MeasurementStore
from booley.runtime.job_records import JobRecord
from tests.harness.test_dashboard_preview import (
    goal_view,
    move_to,
    session_row,
    snapshot_of,
)
from tests.harness.test_dashboard_sessions import stamp
from tests.measurement.test_base_run import Entry, Table, build_run
from tests.measurement.test_run_store import in_tree_repository, key_of, make_run
from tests.runtime.test_commit_export import commit_all, git, write


def reader():
    """The reader module, imported when a test runs so a missing module fails that test."""
    return importlib.import_module("booley.harness.dashboard.measurement")


ONE_TABLE = "[sim_core.lint]\nwarnings_max = 0\n"


@pytest.fixture(autouse=True)
def _generous_first_read_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dashboard_app, "READ_BUDGET", 60.0)
    monkeypatch.delenv("BOOLEY_DIR", raising=False)


def declared_repository(root: Path) -> tuple[Path, str]:
    """A repository on ``main`` whose first commit tracks a Booley Directory with one table."""
    repo, _ = in_tree_repository(root)
    write(repo / ".booley" / "metrics.toml", ONE_TABLE)
    return repo, commit_all(repo, "rules")


def change(repo: Path, text: str, message: str = "change") -> str:
    write(repo / "rtl.v", text)
    return commit_all(repo, message)


def store_run(repo: Path, commit: str, run_id: str, *, verdict: str = "met", minute: int = 0):
    store = MeasurementStore(repo / ".booley")
    return store.write(make_run(key_of(repo, commit), run_id, verdict=verdict, minute=minute), [])


def line_of(repo: Path, jobs: tuple[JobView, ...] = ()) -> reader().MeasurementLine | None:
    return reader().measurement_lines({str(repo)}, jobs).get(str(repo))


def short(commit: str) -> str:
    return commit[:7]


# --- AC1 ---------------------------------------------------------------------


def test_project_without_tables_shows_no_line(tmp_path: Path) -> None:
    repo, head = declared_repository(tmp_path / "repo")
    store_run(repo, head, "r1")
    metrics = repo / ".booley" / "metrics.toml"

    for text in (
        None,
        "[defaults.lint]\nwarnings_max = 0\n",
        '[defaults.lint]\nwarnings_max = "x"\n',
    ):
        if text is None:
            metrics.unlink()
        else:
            write(metrics, text)
        assert line_of(repo) is None, text


def test_a_checkout_git_cannot_read_has_no_line(tmp_path: Path) -> None:
    empty = tmp_path / "no-commits"
    write(empty / ".booley" / "booley.toml", "[project]\n")
    write(empty / ".booley" / "metrics.toml", ONE_TABLE)
    git(empty, "init", "-q", "-b", "main")

    assert reader().measurement_lines({str(empty), ""}, ()) == {}


# --- AC2 ---------------------------------------------------------------------


def test_measured_head_reads_measured_with_its_short_id(tmp_path: Path) -> None:
    repo, head = declared_repository(tmp_path / "repo")
    stored = store_run(repo, head, "r1")
    stored.markdown.write_bytes(b"# report\n")

    line = line_of(repo)

    assert line is not None
    assert line.text == f"Measured {short(head)}"
    assert line.report == str(stored.markdown)
    assert line.measuring is None


def test_head_without_a_run_is_not_measured_and_has_no_report(tmp_path: Path) -> None:
    repo, _ = declared_repository(tmp_path / "repo")

    line = line_of(repo)

    assert line is not None
    assert line.text == f"latest {short(git(repo, 'rev-parse', 'HEAD'))}: not measured"
    assert line.report is None


def test_unreadable_run_document_reads_unreadable_run(tmp_path: Path) -> None:
    repo, head = declared_repository(tmp_path / "repo")
    stored = store_run(repo, head, "r1")
    stored.path.write_bytes(b"{not json")

    line = line_of(repo)

    assert line is not None
    assert line.text == f"latest {short(head)}: unreadable run"


def test_a_run_without_failing_results_reads_its_status(tmp_path: Path) -> None:
    repo, head = declared_repository(tmp_path / "repo")
    store = MeasurementStore(repo / ".booley")
    store.write(make_run(key_of(repo, head), "r1", status="cancelled"), [])

    line = line_of(repo)

    assert line is not None
    assert line.text == f"latest {short(head)}: cancelled"


# --- AC3 ---------------------------------------------------------------------


def counted_run(repo: Path, commit: str, verdicts: tuple[Verdict, ...]):
    entries = tuple(
        Entry(f"m{n}_max", f"m{n}", 5, bound=1, verdict=verdict)
        for n, verdict in enumerate(verdicts)
    )
    run = build_run(key_of(repo, commit), "r-counts", (Table("lint", entries),), minute=5)
    return MeasurementStore(repo / ".booley").write(run, [])


def test_unmet_latest_run_and_measured_ancestor_show_both_parts(tmp_path: Path) -> None:
    repo, ancestor = declared_repository(tmp_path / "repo")
    ancestor_run = store_run(repo, ancestor, "r1")
    ancestor_run.markdown.write_bytes(b"# ancestor\n")
    git(repo, "switch", "-q", "-c", "feature")
    head = change(repo, "module b; endmodule\n")
    unmet = Verdict.UNMET
    skipped = Verdict.NOT_EVALUATED
    counted = counted_run(repo, head, (unmet, skipped, skipped, skipped, Verdict.MET))

    line = line_of(repo)

    assert line is not None
    assert line.text == (
        f"last Measured {short(ancestor)} · latest {short(head)}: 1 unmet, 3 not evaluated"
    )
    assert line.report is None  # the latest run has no report file: nothing to open
    counted.markdown.write_bytes(b"# latest\n")
    assert line_of(repo).report == str(counted.markdown)  # type: ignore[union-attr]


def test_two_errors_are_plural_and_come_first(tmp_path: Path) -> None:
    repo, head = declared_repository(tmp_path / "repo")
    counted_run(repo, head, (Verdict.UNMET, Verdict.ERROR, Verdict.ERROR))

    line = line_of(repo)

    assert line is not None
    assert line.text == f"latest {short(head)}: 2 errors, 1 unmet"


def test_without_a_head_run_the_last_measured_runs_report_is_linked(tmp_path: Path) -> None:
    repo, ancestor = declared_repository(tmp_path / "repo")
    stored = store_run(repo, ancestor, "r1")
    stored.markdown.write_bytes(b"# ancestor\n")
    git(repo, "switch", "-q", "-c", "feature")
    head = change(repo, "module b; endmodule\n")

    line = line_of(repo)

    assert line is not None
    assert line.text == f"last Measured {short(ancestor)} · latest {short(head)}: not measured"
    assert line.report == str(stored.markdown)


def test_head_without_committed_rules_still_finds_a_measured_ancestor(tmp_path: Path) -> None:
    repo, ancestor = declared_repository(tmp_path / "repo")
    store_run(repo, ancestor, "r1")
    git(repo, "switch", "-q", "-c", "feature")
    git(repo, "rm", "-q", "--cached", ".booley/booley.toml")
    git(repo, "commit", "-q", "-m", "untrack the rules")
    head = git(repo, "rev-parse", "HEAD")

    line = line_of(repo)

    assert line is not None
    assert line.text == f"last Measured {short(ancestor)} · latest {short(head)}: not measured"


# --- AC4 ---------------------------------------------------------------------


def test_message_only_amend_keeps_measured_with_the_new_id(tmp_path: Path) -> None:
    repo, head = declared_repository(tmp_path / "repo")
    store_run(repo, head, "r1")
    git(repo, "commit", "-q", "--amend", "-m", "reworded")
    amended = git(repo, "rev-parse", "HEAD")

    line = line_of(repo)

    assert amended != head
    assert line is not None
    assert line.text == f"Measured {short(amended)}"


# --- AC5 ---------------------------------------------------------------------


def measure_job(
    work_dir: str, *, state: str = "running", progress: dict[str, int] | None = None
) -> JobView:
    record = JobRecord(
        "measure-1", "measure", stamp(840), 60, work_dir=work_dir, progress=progress
    )
    return JobView(Path("/jobs"), record, state)


def test_measuring_text_names_the_count_only_when_it_is_readable() -> None:
    assert reader().measuring_text({"finished": 2, "total": 5}, 840) == "measuring 2/5 · 14m 00s"
    assert reader().measuring_text(None, 840) == "measuring · 14m 00s"
    assert reader().measuring_text({"finished": "x", "total": 5}, None) == "measuring"


def test_running_measure_job_shows_progress_then_the_stored_state(tmp_path: Path) -> None:
    repo, head = declared_repository(tmp_path / "repo")
    store_run(repo, head, "r1")
    stored = f"Measured {short(head)}"
    running = measure_job(str(repo), progress={"finished": 2, "total": 5})
    elsewhere = measure_job(str(tmp_path / "other"))
    done = measure_job(str(repo), state="completed")

    line = line_of(repo, (done, elsewhere, running))
    assert line is not None
    assert (line.text, line.measuring) == (stored, running)
    ended = line_of(repo, (done, elsewhere))
    assert ended is not None
    assert (ended.text, ended.measuring) == (stored, None)

    rows = [session_row(0, "wt0", work_dir=str(repo), branch="main")]

    def overview(line: reader().MeasurementLine) -> list[str]:
        app = DashboardApp(lambda: JobSnapshot(()))
        app.snapshot = dataclasses.replace(snapshot_of(rows), measurements={str(repo): line})
        return [row[0].plain for row in app._overview()[2]]

    assert overview(line)[-1].strip() == "measuring 2/5 · 14m 00s"
    assert overview(ended)[-1].strip() == stored


# --- AC6 ---------------------------------------------------------------------


def shown(report: str | None) -> model.DashboardSnapshot:
    rows = [session_row(0, "wt0", work_dir="/work/a", branch="main")]
    line = reader().MeasurementLine("Measured 1a2b3c4", report)
    return dataclasses.replace(snapshot_of(rows), measurements={"/work/a": line})


@pytest.mark.asyncio
async def test_enter_on_the_line_opens_the_markdown_report() -> None:
    opened: list[str] = []
    app = DashboardApp(lambda: shown("/store/run.md"), lambda path: opened.append(path) or "")
    async with app.run_test(size=(130, 30)) as pilot:
        await pilot.pause()
        assert app.row_keys[-2:] == ["codex:00", sessions.MEASUREMENT_PREFIX + "codex:00"]
        move_to(app, sessions.MEASUREMENT_PREFIX + "codex:00")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert opened == ["/store/run.md"]
        assert app.navigation.view == "overview"  # the line opens the report, not a view
        assert "⏎ open" in str(app.query_one("#footer").render())
        assert str(app.query_one("#detail").render()).startswith("S1 · ")  # the session preview


@pytest.mark.asyncio
async def test_a_line_without_a_report_is_never_selectable() -> None:
    opened: list[str] = []
    skipped = sessions.HEADER_PREFIX + sessions.MEASUREMENT_PREFIX + "codex:00"
    app = DashboardApp(lambda: shown(None), lambda path: opened.append(path) or "")
    async with app.run_test(size=(130, 30)) as pilot:
        await pilot.pause()
        assert app.row_keys[-1] == skipped
        await pilot.press("down")
        await pilot.pause()
        assert app._selected_key() == "codex:00"  # the cursor never lands on the line
        assert opened == []


# --- AC7 ---------------------------------------------------------------------


def test_history_walk_stops_at_the_merge_base(tmp_path: Path) -> None:
    repo, before = declared_repository(tmp_path / "repo")
    store_run(repo, before, "r-before")
    merge_base = change(repo, "module m; endmodule\n", "merge-base")
    git(repo, "switch", "-q", "-c", "feature")
    head = change(repo, "module f; endmodule\n", "feature")

    beyond = line_of(repo)
    assert beyond is not None
    assert beyond.text == f"latest {short(head)}: not measured"  # `before` is past the merge-base

    store_run(repo, merge_base, "r-base")
    included = line_of(repo)
    assert included is not None
    assert (
        included.text == f"last Measured {short(merge_base)} · latest {short(head)}: not measured"
    )


def test_a_commit_on_the_default_branch_looks_at_itself_only(tmp_path: Path) -> None:
    repo, before = declared_repository(tmp_path / "repo")
    store_run(repo, before, "r1")
    head = change(repo, "module m; endmodule\n")

    line = line_of(repo)

    assert line is not None
    assert line.text == f"latest {short(head)}: not measured"


def test_an_unreadable_ancestor_document_counts_as_not_measured(tmp_path: Path) -> None:
    repo, ancestor = declared_repository(tmp_path / "repo")
    store_run(repo, ancestor, "r1").path.write_bytes(b"{not json")
    git(repo, "switch", "-q", "-c", "feature")
    change(repo, "module f; endmodule\n")

    line = line_of(repo)

    assert line is not None
    assert "last Measured" not in line.text


# --- AC8 ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_goal_session_keeps_its_goal_views_beside_the_line() -> None:
    rows = [session_row(0, "wt0", work_dir="/work/a", branch="goal/fixture")]
    goal = goal_view("wt0", "active", met=2, goals=(0,) * 5, pending_proposals=1)
    plain = snapshot_of(rows, goals=[goal])
    line = reader().MeasurementLine("Measured 1a2b3c4", "/store/run.md")
    lined = dataclasses.replace(plain, measurements={"/work/a": line})
    details: list[list[str]] = []
    overviews: list[list[str]] = []
    for current in (plain, lined):
        app = DashboardApp(lambda current=current: current)
        async with app.run_test(size=(130, 30)) as pilot:
            await pilot.pause()
            overviews.append([row[0].plain for row in app._overview()[2]])
            await pilot.press("enter")
            await pilot.pause()
            details.append(list(app.row_keys))

    assert overviews[1][:2] == overviews[0]  # the group header and the session row, as before
    assert "Goal 2/5" in overviews[1][1]
    assert overviews[1][2].strip() == "Measured 1a2b3c4"
    assert details[0] == details[1] != []
