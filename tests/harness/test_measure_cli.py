"""``booley measure``: ``--validate`` reads the working tree's rules; a commit is measured (#3953).

The measurement tests replace the children (``prepare_children`` and ``run_children`` of
``booley.harness.measure_command``) with scripted ones, so no Flow runs. Git, the export, the
rules loader, the Target catalog, the store, the view and the Job record are real. The real
child Flows are in ``test_measure_command_flows.py``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from booley.flows.metric_declarations import FlowMetricValues
from booley.harness import booley as cli
from booley.harness.checkout_flow import CheckoutFlowError
from booley.harness.measure_children import ChildrenOutcome, TableMeasurement
from booley.harness.standalone_job import standalone_jobs_dir, standalone_log_path
from booley.measurement.metrics_file import TableRules
from booley.measurement.run_document import RunStatus, TableRun
from booley.measurement.run_key import resolve_measurement_key
from booley.measurement.run_store import MeasurementStore
from booley.measurement.verdicts import FlowFailed, FlowValues, evaluate_table
from booley.runtime import job_records
from tests.harness.test_checkout_flow import git
from tests.measurement.metrics_project_support import EXAMPLE_METRICS, write_metrics_project


def _measure(monkeypatch: pytest.MonkeyPatch, project: Path, *arguments: str) -> int:
    """Run the real entry point as ``booley -C <project> measure <arguments>`` in a Sandbox."""
    monkeypatch.setattr(cli.runtime_context, "inside_session_runtime", lambda: True)
    monkeypatch.setattr(cli, "_host_install_authority_error", lambda *_: None)
    monkeypatch.setattr(sys, "argv", ["booley", "-C", str(project), "measure", *arguments])
    return cli._dispatch_main()


def _listing(project: Path) -> list[str]:
    """Every path in the Project's Booley Directory, so a test can show nothing was written."""
    data = project / ".booley"
    return sorted(str(path.relative_to(data)) for path in data.rglob("*"))


def test_validate_valid_file_exits_zero_and_runs_no_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_metrics_project(tmp_path, EXAMPLE_METRICS)  # not a Git repository: no commit exists
    before = _listing(tmp_path)
    assert _measure(monkeypatch, tmp_path, "--validate") == 0
    out = capsys.readouterr().out
    assert "metrics.toml is valid: 6 tables" in out
    assert "[sim_fifo.sim]" in out
    assert _listing(tmp_path) == before


def test_validate_lists_every_load_error_and_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_metrics_project(
        tmp_path,
        "[nope.sim]\ntests_failed_max = 0\n[sim_core.sim]\nbogus_max = 1\n",
    )
    assert _measure(monkeypatch, tmp_path, "--validate") == 2
    err = capsys.readouterr().err
    assert "[nope.sim]" in err
    assert "[sim_core.sim] bogus_max: " in err
    assert "metrics.toml has 2 load errors" in err


def test_validate_reports_a_table_free_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_metrics_project(tmp_path, "[defaults.sim]\ntests_failed_max = 0\n")
    assert _measure(monkeypatch, tmp_path, "--validate") == 0
    assert "declares no `[<target>.<flow>]` table" in capsys.readouterr().out


def test_validate_without_metrics_file_declares_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_metrics_project(tmp_path)
    assert _measure(monkeypatch, tmp_path, "--validate") == 0
    out = capsys.readouterr().out
    assert "no metrics.toml" in out
    assert "declares nothing" in out


# ---------------------------------------------------------------- measuring a commit

TWO_LINT_TABLES = "[sim_core.lint]\nwarnings_max = 0\n[sim_two.lint]\nwarnings_max = 0\n"
ONE_LINT_TABLE = "[sim_core.lint]\nwarnings_max = 0\n"
DEADLINE_S = 20
NOTHING_IN_SCOPE = "nothing to measure, add a `[target.flow]` table"
COMMAND = "booley.harness.measure_command"


@pytest.fixture(autouse=True)
def _measuring_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A shell outside any Job, with the measurement's scratch under the test's own directory."""
    for name in ("BOOLEY_RUN_ID", "_BOOLEY_CLI_INVOCATION_ORIGIN", "BOOLEY_DIR"):
        monkeypatch.delenv(name, raising=False)
    scratch_root = tmp_path / "scratch-root"
    scratch_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch_root))
    return scratch_root


def committed_project(tmp_path: Path, metrics: str) -> Path:
    """A Git repository whose one commit holds the Targets, ``tests.toml`` and *metrics*."""
    project = write_metrics_project(tmp_path / "proj", metrics)
    git(project, "init", "-q", "-b", "main")
    git(project, "add", "-A", "-f")
    git(project, "commit", "-q", "-m", "measured commit")
    return project


def run_command(monkeypatch: pytest.MonkeyPatch, project: Path, *arguments: str) -> int:
    return _measure(monkeypatch, project, *arguments)


def outcome_of(
    tables: Sequence[TableRules],
    warnings: Sequence[int | None],
    *,
    status: RunStatus = RunStatus.COMPLETED,
    finished: Sequence[bool] | None = None,
) -> ChildrenOutcome:
    """A scripted outcome: table *n* reported ``warnings[n]`` warnings, or crashed on None."""
    measured: list[TableMeasurement] = []
    for index, (rules, count) in enumerate(zip(tables, warnings, strict=True)):
        if count is None:
            outcome: FlowFailed | FlowValues = FlowFailed("lint crashed: boom")
            values = None
        else:
            values = FlowMetricValues(plain={"warnings": count, "errors": 0}, per_clock={})
            outcome = FlowValues(values, ())
        run = TableRun(rules.identity, rules.flow, 1.5, values, evaluate_table(rules, outcome))
        done = True if finished is None else finished[index]
        measured.append(TableMeasurement(run, (), done))
    return ChildrenOutcome(status, tuple(measured))


def script_children(
    monkeypatch: pytest.MonkeyPatch,
    run: Callable[[Sequence[TableRules], Callable[[], bool]], ChildrenOutcome],
    prepare: Callable[[], object] | None = None,
) -> list[str]:
    """Replace the children; the returned list names each step that ran, in order."""
    steps: list[str] = []
    if importlib.util.find_spec(COMMAND) is None:
        return steps  # the command does not exist yet: `booley measure` refuses to run

    def prepare_children(tables: Sequence[TableRules], **_options: object) -> list[object]:
        steps.append("prepare")
        if prepare is not None:
            prepare()
        return []

    def run_children(
        _prepared: object,
        tables: Sequence[TableRules],
        *,
        cancelled: Callable[[], bool],
        **_options: object,
    ) -> ChildrenOutcome:
        steps.append("run")
        return run(tables, cancelled)

    monkeypatch.setattr(f"{COMMAND}.prepare_children", prepare_children)
    monkeypatch.setattr(f"{COMMAND}.run_children", run_children)
    return steps


def job_record(project: Path) -> job_records.JobRecord:
    """The one ``measure`` Job record of the Project."""
    records = job_records.list_records(
        standalone_jobs_dir(project / ".booley"), skip_malformed=True
    )
    (record,) = [r for r in records if r.endpoint == "measure"]
    return record


def job_log(project: Path) -> str:
    return standalone_log_path(project / ".booley", job_record(project).run_id).read_text(
        encoding="utf-8"
    )


def stored_runs(project: Path) -> list[Path]:
    return sorted((project / ".booley" / "measurements").glob("*/*.json"))


def key_of(project: Path):
    commit = git(project, "rev-parse", "HEAD")
    return resolve_measurement_key(project, commit).key


def test_unmet_and_error_together_exit_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = committed_project(tmp_path, TWO_LINT_TABLES)
    script_children(monkeypatch, lambda tables, _c: outcome_of(tables, [3, None]))

    assert run_command(monkeypatch, project) == 2

    out = capsys.readouterr().out
    assert "lint crashed: boom" in out
    (path,) = stored_runs(project)
    verdicts = {r["verdict"] for t in json.loads(path.read_text())["tables"] for r in t["results"]}
    assert verdicts == {"unmet", "error"}
    assert job_record(project).exit_code == 2


def test_nothing_in_scope_exits_two_and_stores_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = committed_project(tmp_path, "[defaults.sim]\ntests_failed_max = 0\n")
    steps = script_children(monkeypatch, lambda *_: pytest.fail("no child may run"))

    assert run_command(monkeypatch, project) == 2

    assert NOTHING_IN_SCOPE in capsys.readouterr().err
    assert steps == []
    assert not (project / ".booley" / "measurements").exists()
    assert (job_record(project).status, job_record(project).exit_code) == ("failed", 2)


def test_a_load_error_in_the_checkout_exits_two_and_stores_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = committed_project(tmp_path, "[nope.lint]\nwarnings_max = 0\n")
    # The working tree's copy is valid: only the committed one is a load error.
    (project / ".booley" / "metrics.toml").write_bytes(ONE_LINT_TABLE.encode())
    steps = script_children(monkeypatch, lambda *_: pytest.fail("no child may run"))

    assert run_command(monkeypatch, project) == 2

    err = capsys.readouterr().err
    assert "[nope.lint]" in err and "metrics.toml has 1 load error" in err
    assert steps == []
    assert not (project / ".booley" / "measurements").exists()
    record = job_record(project)
    assert (record.status, record.exit_code) == ("failed", 2)
    assert "[nope.lint]" in job_log(project)


def test_a_preflight_refusal_exits_two_and_stores_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = committed_project(tmp_path, ONE_LINT_TABLE)

    def refuse() -> None:
        raise CheckoutFlowError("[sim_core.lint] checkout run refused: unsafe path")

    steps = script_children(monkeypatch, lambda *_: pytest.fail("no child may run"), refuse)

    assert run_command(monkeypatch, project) == 2

    assert "checkout run refused: unsafe path" in capsys.readouterr().err
    assert steps == ["prepare"]
    assert not (project / ".booley" / "measurements").exists()
    assert (job_record(project).status, job_record(project).exit_code) == ("failed", 2)
    assert "checkout run refused: unsafe path" in job_log(project)


def test_a_first_child_that_cannot_launch_is_a_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = committed_project(tmp_path, ONE_LINT_TABLE)

    def cannot_launch(
        _tables: Sequence[TableRules], _cancelled: Callable[[], bool]
    ) -> ChildrenOutcome:
        raise CheckoutFlowError("Flow 'lint' could not be started: disk full")

    script_children(monkeypatch, cannot_launch)

    assert run_command(monkeypatch, project) == 2
    assert "could not be started: disk full" in capsys.readouterr().err
    assert not (project / ".booley" / "measurements").exists()


def test_json_prints_the_report_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = committed_project(tmp_path, ONE_LINT_TABLE)
    script_children(monkeypatch, lambda tables, _c: outcome_of(tables, [0]))

    assert run_command(monkeypatch, project, "--json", "--all") == 0

    document = json.loads(capsys.readouterr().out)
    assert "view" in document
    assert document["run_id"] == job_record(project).run_id
    assert job_record(project).argv == ["booley", "measure", "--all", "--json"]


def test_an_invalid_base_is_refused_before_the_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = committed_project(tmp_path, ONE_LINT_TABLE)
    steps = script_children(monkeypatch, lambda *_: pytest.fail("no child may run"))

    assert run_command(monkeypatch, project, "--base", "no-such-revision") == 2

    assert "--base 'no-such-revision' cannot be used" in capsys.readouterr().err
    assert steps == []
    assert not (project / ".booley" / "measurements").exists()


@pytest.mark.parametrize("revision", ["no-such-revision", "--exec=x"])
def test_a_revision_that_is_not_a_commit_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    revision: str,
) -> None:
    project = committed_project(tmp_path, ONE_LINT_TABLE)
    monkeypatch.setattr(sys, "argv", ["booley", "-C", str(project), "measure", "--", revision])
    monkeypatch.setattr(cli.runtime_context, "inside_session_runtime", lambda: True)
    monkeypatch.setattr(cli, "_host_install_authority_error", lambda *_: None)

    assert cli._dispatch_main() == 2
    assert "is not a commit of this Project" in capsys.readouterr().err


def test_rules_that_are_not_versioned_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = write_metrics_project(tmp_path / "proj", ONE_LINT_TABLE)
    git(project, "init", "-q", "-b", "main")
    git(project, "add", "-A")  # `.booley` is not ignored here: remove it from the commit
    git(project, "rm", "-q", "-r", "--cached", ".booley")
    git(project, "commit", "-q", "-m", "no rules")

    assert run_command(monkeypatch, project) == 2
    assert "rules must be versioned" in capsys.readouterr().err


def test_an_unexportable_commit_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from booley.runtime.commit_export import CommitExportError

    project = committed_project(tmp_path, ONE_LINT_TABLE)

    def export_fails(*_args: object) -> None:
        raise CommitExportError("a submodule is unpinned")

    monkeypatch.setattr(f"{COMMAND}.export_commit", export_fails)

    assert run_command(monkeypatch, project) == 2
    assert "the commit cannot be exported: a submodule is unpinned" in capsys.readouterr().err


def test_an_unreadable_metrics_file_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = committed_project(tmp_path, ONE_LINT_TABLE)

    def unreadable(_checkout: Path) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(f"{COMMAND}.load_metrics_rules", unreadable)

    assert run_command(monkeypatch, project) == 2
    assert "metrics.toml cannot be read: denied" in capsys.readouterr().err


@pytest.mark.parametrize("option", [["abc"], ["--base", "x"], ["--all"], ["--json"]])
def test_validate_takes_no_other_option(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    option: list[str],
) -> None:
    write_metrics_project(tmp_path, EXAMPLE_METRICS)
    assert _measure(monkeypatch, tmp_path, "--validate", *option) == 2
    assert "takes no other option" in capsys.readouterr().err


def test_measure_inside_another_job_gets_its_own_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = committed_project(tmp_path, ONE_LINT_TABLE)
    monkeypatch.setenv("BOOLEY_RUN_ID", "outer-run")
    monkeypatch.setenv("_BOOLEY_CLI_INVOCATION_ORIGIN", "transport")
    script_children(monkeypatch, lambda tables, _c: outcome_of(tables, [0]))

    assert run_command(monkeypatch, project) == 0

    record = job_record(project)
    assert record.run_id != "outer-run" and record.status == "done"
    assert os.environ["BOOLEY_RUN_ID"] == "outer-run"  # the caller's Job id is back


def test_a_project_without_a_job_directory_cannot_store_a_measurement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from booley.harness import standalone_job

    project = committed_project(tmp_path, ONE_LINT_TABLE)
    monkeypatch.setattr(
        standalone_job.StandaloneJob, "prepare", classmethod(lambda *_a, **_k: None)
    )

    assert run_command(monkeypatch, project) == 2
    assert "no Main Booley Directory" in capsys.readouterr().err


def test_an_unwritable_store_exits_two_with_the_error_in_the_job_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = committed_project(tmp_path, ONE_LINT_TABLE)
    (project / ".booley" / "measurements").write_bytes(b"a file, not a directory")
    script_children(monkeypatch, lambda tables, _c: outcome_of(tables, [0]))

    assert run_command(monkeypatch, project) == 2

    assert "ERROR: measurement failed:" in capsys.readouterr().err
    record = job_record(project)
    assert (record.status, record.exit_code) == ("failed", 2)
    assert "measurement failed" in job_log(project)


def assert_cancelled_run_stored(project: Path, *, measured: bool = False) -> None:
    """One stored run, cancelled, never Measured; the Job ends cancelled with exit 130."""
    (path,) = stored_runs(project)
    assert json.loads(path.read_text())["status"] == "cancelled"
    assert (
        MeasurementStore(project / ".booley").measured(key_of(project)) is not None
    ) is measured
    record = job_record(project)
    assert (record.status, record.exit_code) == ("cancelled", 130)


def send(number: int) -> None:
    signal.raise_signal(number)


def test_cancelled_after_a_finished_child_is_stored_as_cancelled_and_not_measured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _measuring_environment: Path
) -> None:
    project = committed_project(tmp_path, TWO_LINT_TABLES)
    script_children(
        monkeypatch,
        lambda tables, _c: outcome_of(
            tables, [0, 0], status=RunStatus.CANCELLED, finished=[True, False]
        ),
    )

    with pytest.raises(KeyboardInterrupt):
        run_command(monkeypatch, project)

    assert_cancelled_run_stored(project)
    assert not list(_measuring_environment.iterdir())  # the scratch checkout is gone


def test_cancelled_before_any_child_finished_stores_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = committed_project(tmp_path, TWO_LINT_TABLES)
    script_children(
        monkeypatch,
        lambda tables, _c: outcome_of(
            tables, [0, 0], status=RunStatus.CANCELLED, finished=[False, False]
        ),
    )

    with pytest.raises(KeyboardInterrupt):
        run_command(monkeypatch, project)

    assert "nothing stored" in capsys.readouterr().err
    assert not (project / ".booley" / "measurements").exists()
    record = job_record(project)
    assert (record.status, record.exit_code) == ("cancelled", 130)


def children_cancelled_by(
    project: Path, monkeypatch: pytest.MonkeyPatch, numbers: Sequence[int]
) -> None:
    """Run the command whose children receive each signal of *numbers* in turn, then cancel."""

    def run(tables: Sequence[TableRules], cancelled: Callable[[], bool]) -> ChildrenOutcome:
        for number in numbers:
            send(number)  # a repeated signal must not raise out of the children
        deadline = time.monotonic() + DEADLINE_S
        while not cancelled():
            assert time.monotonic() < deadline, "the signal never set the flag"
            time.sleep(0.01)
        return outcome_of(tables, [0, 0], status=RunStatus.CANCELLED, finished=[True, False])

    script_children(monkeypatch, run)
    with pytest.raises(KeyboardInterrupt):
        run_command(monkeypatch, project)


def test_signals_during_the_children_cancel_the_measurement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = committed_project(tmp_path, TWO_LINT_TABLES)

    children_cancelled_by(project, monkeypatch, [signal.SIGINT, signal.SIGINT])

    assert_cancelled_run_stored(project)


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGHUP])
def test_a_termination_signal_cancels_the_measurement_like_ctrl_c(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, number: int
) -> None:
    project = committed_project(tmp_path, TWO_LINT_TABLES)

    children_cancelled_by(project, monkeypatch, [number])

    assert_cancelled_run_stored(project)


def test_a_signal_after_the_children_finished_stores_the_run_as_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = committed_project(tmp_path, ONE_LINT_TABLE)

    def run(tables: Sequence[TableRules], _cancelled: Callable[[], bool]) -> ChildrenOutcome:
        send(signal.SIGINT)  # every bound met, yet the run must not be Measured
        return outcome_of(tables, [0])

    script_children(monkeypatch, run)

    with pytest.raises(KeyboardInterrupt):
        run_command(monkeypatch, project)

    assert_cancelled_run_stored(project)


def test_a_signal_before_the_children_aborts_at_once_and_stores_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _measuring_environment: Path
) -> None:
    project = committed_project(tmp_path, ONE_LINT_TABLE)

    def hang() -> None:
        send(signal.SIGHUP)
        time.sleep(DEADLINE_S)  # only reached when the signal did not interrupt

    steps = script_children(monkeypatch, lambda *_: pytest.fail("no child may run"), hang)

    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        run_command(monkeypatch, project)

    assert time.monotonic() - started < DEADLINE_S
    assert steps == ["prepare"]
    assert not (project / ".booley" / "measurements").exists()
    assert not list(_measuring_environment.iterdir())
    record = job_record(project)
    assert (record.status, record.exit_code) == ("cancelled", 130)


def test_the_signal_handlers_are_restored_after_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = committed_project(tmp_path, ONE_LINT_TABLE)
    script_children(monkeypatch, lambda tables, _c: outcome_of(tables, [0]))
    before = {n: signal.getsignal(n) for n in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}

    assert run_command(monkeypatch, project) == 0

    assert {n: signal.getsignal(n) for n in before} == before


def test_cancel_signals_skip_a_signal_this_platform_lacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from booley.harness import measure_command

    monkeypatch.setattr(measure_command, "_CANCEL_SIGNALS", ("SIGNOSUCH", "SIGINT"))
    with measure_command._cancel_signals() as signals:
        assert signal.getsignal(signal.SIGINT) == signals._on_signal
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


def test_cancel_signals_do_nothing_outside_the_main_thread() -> None:
    import threading

    from booley.harness import measure_command

    seen: list[bool] = []

    def attempt() -> None:
        with measure_command._cancel_signals() as signals:
            seen.append(signals.is_set())

    worker = threading.Thread(target=attempt)
    worker.start()
    worker.join(DEADLINE_S)
    assert seen == [False]
