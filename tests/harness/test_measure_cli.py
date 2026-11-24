"""``booley measure``: ``--validate`` reads the working tree's rules; a commit is measured (#3953).

The measurement tests replace the children (``prepare_children`` and ``run_children`` of
``booley.harness.measure_command``) with scripted ones, so no Flow runs. Git, the export, the
rules loader, the Target catalog, the store, the view and the Job record are real. The real
child Flows are in ``test_measure_command_flows.py``.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import signal
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from booley.flows.metric_declarations import FlowMetricValues
from booley.harness import booley as cli
from booley.harness.checkout_flow import CheckoutFlowError
from booley.harness.measure_children import ChildrenOutcome, TableMeasurement
from booley.harness.standalone_job import (
    standalone_jobs_dir,
    standalone_log_path,
    standalone_log_root,
)
from booley.measurement.measuring_rules import ERROR_RULE, UNMET_RULE
from booley.measurement.metrics_file import TableRules
from booley.measurement.run_document import RunStatus, TableRun
from booley.measurement.run_key import resolve_measurement_key
from booley.measurement.run_store import MeasurementStore
from booley.measurement.verdicts import FlowFailed, FlowValues, evaluate_table
from booley.runtime import job_records
from tests.harness.test_checkout_flow import git
from tests.measurement.metrics_project_support import EXAMPLE_METRICS, write_metrics_project
from tests.time_budget import scaled


def _measure(monkeypatch: pytest.MonkeyPatch, project: Path, *arguments: str) -> int:
    """Run the real entry point as ``booley -C <project> measure <arguments>`` in a Sandbox."""
    monkeypatch.setattr(cli.runtime_context, "inside_session_runtime", lambda: True)
    # Windows has no mountinfo, which reads as an issued Sandbox that mounts no ``/work``.
    hand_written = cli.runtime_context.RuntimeLocation.HAND_WRITTEN_SANDBOX
    monkeypatch.setattr(cli.runtime_context, "runtime_location", lambda: hand_written)
    monkeypatch.setattr(sys, "argv", ["booley", "-C", str(project), "measure", *arguments])
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)  # main() quiets a Ctrl-C's traceback
    return cli.main()


def _listing(project: Path) -> list[str]:
    """Every path in the Project's Booley Directory, so a test can show nothing was written."""
    data = project / ".booley"
    return sorted(str(path.relative_to(data)) for path in data.rglob("*"))


@pytest.mark.parametrize(
    ("metrics", "said"),
    [
        pytest.param(
            EXAMPLE_METRICS, ["metrics.toml is valid: 6 tables", "[sim_fifo.sim]"], id="valid"
        ),
        pytest.param(
            "[defaults.sim]\ntests_failed_max = 0\n",
            ["declares no `[<target>.<flow>]` table"],
            id="table-free",
        ),
        pytest.param(None, ["no metrics.toml", "declares nothing"], id="no-file"),
    ],
)
def test_validate_a_loadable_file_exits_zero_and_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    metrics: str | None,
    said: list[str],
) -> None:
    write_metrics_project(tmp_path, metrics)  # not a Git repository: no commit exists
    before = _listing(tmp_path)
    assert _measure(monkeypatch, tmp_path, "--validate") == 0
    out = capsys.readouterr().out
    assert [text for text in said if text not in out] == []
    assert _listing(tmp_path) == before


BOUND_IS_ABSOLUTE = "; a bound is an absolute number, not a percentage"


@pytest.mark.parametrize(
    ("metrics", "said"),
    [
        pytest.param(
            "[nope.sim]\ntests_failed_max = 0\n[sim_core.sim]\nbogus_max = 1\n"
            "[sim_core.coverage]\nline_coverage_min = 100.01\n",
            [
                "[nope.sim]",
                "[sim_core.sim] bogus_max: ",
                "[sim_core.coverage] line_coverage_min: line_coverage must be within [0, 100], "
                "got 100.01\n  write instead: line_coverage_min = 100",
                "metrics.toml has 3 load errors",
            ],
            id="every-error",
        ),
        pytest.param(  # AC2 of #4388: an automatic mutation count names its table and key
            '[sim_core.mutation]\nmutation_score_min = 50\ncount = "auto"\n',
            ["[sim_core.mutation] count: count must be an integer, got 'auto'"],
            id="auto-count",
        ),
        pytest.param(  # a percentage says bounds are absolute; another string keeps its error
            '[sim_core.sim]\ntests_total_min = "+10%"\ntests_failed_max = "10%"\n'
            'tests_total_max = "zero"\n',
            [
                "tests_total_min must be a finite number, got '+10%'" + BOUND_IS_ABSOLUTE,
                "tests_failed_max must be a finite number, got '10%'" + BOUND_IS_ABSOLUTE,
                "got 'zero'\n",
            ],
            id="percentage",
        ),
    ],
)
def test_validate_lists_every_load_error_and_exits_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    metrics: str,
    said: list[str],
) -> None:
    write_metrics_project(tmp_path, metrics)
    assert _measure(monkeypatch, tmp_path, "--validate") == 2
    err = capsys.readouterr().err
    assert [text for text in said if text not in err] == []


@pytest.mark.parametrize(
    ("entry", "key"),
    [
        pytest.param('"rx_clkk.wns_ns_min" = 0', "rx_clkk.wns_ns_min", id="bound"),
        pytest.param('track = ["rx_clkk.wns_ns"]', "rx_clkk.wns_ns", id="tracked"),
    ],
)
def test_validate_warns_on_a_clock_no_stored_run_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    entry: str,
    key: str,
) -> None:
    """AC1-AC5: an unreported clock prefix warns and exits 0; reported clocks and no store stay quiet."""
    from booley.measurement.run_key import KeyedCommit, MeasurementKey
    from tests.measurement.test_run_store import make_run

    metrics = (
        '[synth_core.synth]\n"rx_clk.wns_ns_min" = 0\n"tx_clk.fmax_mhz_min" = 100\n' + entry + "\n"
    )
    write_metrics_project(tmp_path, metrics)

    assert _measure(monkeypatch, tmp_path, "--validate") == 0
    captured = capsys.readouterr()
    assert "metrics.toml is valid: 1 table" in captured.out
    assert captured.err == ""

    keyed = KeyedCommit(MeasurementKey("0" * 40, None), "1" * 40, "m", (), None)
    identity = "::synth_core:0#synth_core"
    store = MeasurementStore(tmp_path / ".booley")
    first = make_run(
        keyed,
        "r1",
        minute=1,
        identity=identity,
        flow="synth",
        per_clock={"wns_ns": {"rx_clk": 0.5}},
    )
    second = make_run(keyed, "r2", minute=2, identity=identity, flow="synth", clocks=("tx_clk",))
    store.write(first, [])
    store.write(second, [])

    assert _measure(monkeypatch, tmp_path, "--validate") == 0
    captured = capsys.readouterr()
    assert "metrics.toml is valid: 1 table" in captured.out
    assert captured.err == (
        f"warning: [synth_core.synth] {key}: clock rx_clkk was never reported; "
        "reported clocks: rx_clk, tx_clk\n"
    )


# ---------------------------------------------------------------- measuring a commit

TWO_LINT_TABLES = "[sim_core.lint]\nwarnings_max = 0\n[sim_two.lint]\nwarnings_max = 0\n"
ONE_LINT_TABLE = "[sim_core.lint]\nwarnings_max = 0\n"
DEADLINE_S = scaled(20)
# Windows has no SIGHUP: SIGINT stands in, so the tests still import and run there.
HANGUP = getattr(signal, "SIGHUP", signal.SIGINT)
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


def job_dir(project: Path) -> Path:
    """The directory the ``measure`` Job's record names by its run id."""
    return standalone_log_root(project / ".booley") / job_record(project).run_id


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


@pytest.mark.parametrize(
    ("metrics", "arguments", "said"),
    [
        pytest.param(
            "[defaults.sim]\ntests_failed_max = 0\n", [], NOTHING_IN_SCOPE, id="no-table"
        ),
        pytest.param(
            ONE_LINT_TABLE, ["--target", "sim_fifo"], NOTHING_IN_SCOPE, id="target-no-table"
        ),
        pytest.param(ONE_LINT_TABLE, ["--target", "nope"], "Unknown target 'nope'", id="unknown"),
        pytest.param(
            ONE_LINT_TABLE, ["--target", "shared"], "'shared' is declared by 2", id="ambiguous"
        ),
        pytest.param(
            ONE_LINT_TABLE,
            ["--base", "no-such-revision"],
            "--base 'no-such-revision' cannot be used",
            id="base",
        ),
        pytest.param(
            ONE_LINT_TABLE,
            ["--", "no-such-revision"],
            "is not a commit of this Project",
            id="revision",
        ),
        pytest.param(
            ONE_LINT_TABLE,
            ["--", "--exec=x"],
            "is not a commit of this Project",
            id="option-revision",
        ),
    ],
)
def test_a_refused_measurement_exits_two_and_stores_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    metrics: str,
    arguments: list[str],
    said: str,
) -> None:
    project = committed_project(tmp_path, metrics)
    steps = script_children(monkeypatch, lambda *_: pytest.fail("no child may run"))

    assert run_command(monkeypatch, project, *arguments) == 2

    *_, refusal, rule = capsys.readouterr().err.rstrip().splitlines()
    assert said in refusal and rule == ERROR_RULE
    assert steps == []
    assert not (project / ".booley" / "measurements").exists()
    assert not (job_dir(project) / "measurements").exists()
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


def test_a_failing_post_setup_hook_refuses_with_its_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hook = tmp_path / "proj" / ".booley" / "hooks" / "post-setup.py"
    hook.parent.mkdir(parents=True)
    hook.write_bytes(b"import sys\nprint('hook-output')\nsys.exit(3)\n")
    project = committed_project(tmp_path, ONE_LINT_TABLE)
    steps = script_children(monkeypatch, lambda *_: pytest.fail("no child may run"))

    assert run_command(monkeypatch, project) == 2

    run_id = job_record(project).run_id
    log = standalone_log_root(project / ".booley") / run_id / "post-setup.log"
    err = capsys.readouterr().err
    assert ".booley/hooks/post-setup.py" in err.replace("\\", "/")
    assert f"post-setup hook failed (rc=3); log: {log}" in err
    assert "hook-output" in log.read_text(encoding="utf-8")
    assert steps == []
    assert not (project / ".booley" / "measurements").exists()
    assert (job_record(project).status, job_record(project).exit_code) == ("failed", 2)


@pytest.mark.parametrize(
    ("failure", "said"),
    [
        (CheckoutFlowError("Flow 'lint' could not be started: disk full"), "ERROR: Flow 'lint'"),
        (RuntimeError("boom"), "ERROR: measurement failed: boom"),
    ],
    ids=["launch", "crash"],
)
def test_children_that_fail_to_run_exit_two_and_store_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _measuring_environment: Path,
    failure: Exception,
    said: str,
) -> None:
    project = committed_project(tmp_path, ONE_LINT_TABLE)

    def fail(_tables: Sequence[TableRules], _cancelled: Callable[[], bool]) -> ChildrenOutcome:
        raise failure

    script_children(monkeypatch, fail)

    assert run_command(monkeypatch, project) == 2
    assert said in capsys.readouterr().err
    assert not (project / ".booley" / "measurements").exists()
    assert not list(_measuring_environment.iterdir())  # the scratch checkout is gone


def test_measure_job_record_carries_flow_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = committed_project(tmp_path, TWO_LINT_TABLES)
    script_children(monkeypatch, lambda *_: pytest.fail("replaced below"))
    seen: list[dict[str, int] | None] = []

    def run_children(
        _prepared: object,
        tables: Sequence[TableRules],
        *,
        progress: Callable[[int, int, tuple[str, ...]], None],
        **_options: object,
    ) -> ChildrenOutcome:
        progress(1, len(tables), ("sim_two.lint",))
        seen.append(job_record(project).progress)  # the record on disk while the Job runs
        return outcome_of(tables, [0, 0])

    monkeypatch.setattr(f"{COMMAND}.run_children", run_children)

    assert run_command(monkeypatch, project) == 0

    assert seen == [{"finished": 1, "total": 2}]
    assert job_record(project).status == "done"


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


def test_validate_takes_no_other_option(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_metrics_project(tmp_path, EXAMPLE_METRICS)
    options = [["abc"], ["--base", "x"], ["--all"], ["--json"], ["--again"], ["--target", "x"]]
    for option in options:
        assert _measure(monkeypatch, tmp_path, "--validate", *option) == 2, option
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


def test_an_unwritable_store_exits_two_with_the_error_in_the_job_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = committed_project(tmp_path, ONE_LINT_TABLE)
    (project / ".booley" / "measurements").write_bytes(b"a file, not a directory")
    script_children(monkeypatch, lambda tables, _c: outcome_of(tables, [0]))

    assert run_command(monkeypatch, project) == 2

    err = capsys.readouterr().err
    assert "ERROR: measurement failed:" in err
    assert err.rstrip().splitlines()[-1] == ERROR_RULE
    record = job_record(project)
    assert (record.status, record.exit_code) == ("failed", 2)
    assert "measurement failed" in job_log(project)


def test_a_measured_commit_is_shown_without_a_checkout_or_a_flow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _measuring_environment: Path,
) -> None:
    project = committed_project(tmp_path, ONE_LINT_TABLE)
    steps = script_children(monkeypatch, lambda tables, _c: outcome_of(tables, [0]))
    assert run_command(monkeypatch, project) == 0
    (first,) = stored_runs(project)
    err = capsys.readouterr().err  # a met run prints no rule
    assert UNMET_RULE not in err and ERROR_RULE not in err
    exports: list[tuple[object, ...]] = []
    export = importlib.import_module(COMMAND).export_commit

    def recording_export(*args: object):
        exports.append(args)
        return export(*args)  # type: ignore[arg-type]

    # The checkout is observable only through this call: the command exports and discards it.
    monkeypatch.setattr(f"{COMMAND}.export_commit", recording_export)

    assert run_command(monkeypatch, project) == 0

    captured = capsys.readouterr()
    assert steps == ["prepare", "run"]  # the second command started no Flow
    assert (exports, list(_measuring_environment.iterdir())) == ([], [])
    assert stored_runs(project) == [first]
    assert "Targets with no metrics: " in captured.out
    run_id = json.loads(first.read_text())["run_id"]
    assert f"is already Measured by run {run_id}; showing it" in captured.err
    assert "--again" in captured.err
    assert "Measurement Report" not in captured.err
    assert run_command(monkeypatch, project, "--json") == 0
    document = json.loads(capsys.readouterr().out)
    assert "view" in document and document["status"] == "completed"


@pytest.mark.parametrize("later", ["unmet", "error", "stopped", "cancelled"])
def test_a_later_run_that_is_not_measured_is_measured_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, later: str
) -> None:
    project = committed_project(tmp_path, ONE_LINT_TABLE)
    scripted: dict[str, dict[str, object]] = {
        "unmet": {"warnings": [3]},
        "error": {"warnings": [None]},
        "stopped": {"warnings": [0], "status": RunStatus.STOPPED},
        "cancelled": {"warnings": [0], "status": RunStatus.CANCELLED, "finished": [True]},
    }
    queue = [{"warnings": [0]}, scripted[later], {"warnings": [0]}]
    steps = script_children(
        monkeypatch,
        lambda tables, _c: outcome_of(tables, **queue.pop(0)),  # type: ignore[arg-type]
    )
    assert run_command(monkeypatch, project) == 0
    if later == "cancelled":
        with pytest.raises(KeyboardInterrupt):
            run_command(monkeypatch, project, "--again")
    else:
        run_command(monkeypatch, project, "--again")
    store = MeasurementStore(project / ".booley")
    assert len(stored_runs(project)) == 2
    assert store.measured(key_of(project)) is None  # the later run decides

    assert run_command(monkeypatch, project) == 0  # no --again: the key is not Measured

    assert steps == ["prepare", "run"] * 3
    assert len(stored_runs(project)) == 3
    assert store.measured(key_of(project)) is not None


def test_an_unreadable_stored_document_is_noted_and_the_commit_is_measured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = committed_project(tmp_path, ONE_LINT_TABLE)
    steps = script_children(monkeypatch, lambda tables, _c: outcome_of(tables, [0]))
    assert run_command(monkeypatch, project) == 0
    (first,) = stored_runs(project)
    first.write_bytes(b"not json")
    capsys.readouterr()

    assert run_command(monkeypatch, project) == 0

    assert "measuring again" in capsys.readouterr().err
    assert steps == ["prepare", "run"] * 2


def project_with_live_changes(tmp_path: Path) -> Path:
    """A project whose HEAD has a submodule ``ip``; its live tree and ``ip`` have moved on."""
    origin = tmp_path / "ip-origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    (origin / "lib.v").write_bytes(b"lib v1\n")
    git(origin, "add", "-A")
    git(origin, "commit", "-q", "-m", "ip v1")
    project = committed_project(tmp_path, ONE_LINT_TABLE)
    git(project, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(origin), "ip")
    (project / "notes.txt").write_bytes(b"v1\n")
    git(project, "add", ".gitmodules", "ip", "notes.txt")
    git(project, "commit", "-q", "-m", "add ip")
    (project / "notes.txt").write_bytes(b"v2\n")
    git(project, "commit", "-q", "-am", "notes v2")
    (project / "notes.txt").write_bytes(b"v3 uncommitted\n")
    (project / "ip" / "lib.v").write_bytes(b"lib v2\n")
    git(project / "ip", "commit", "-q", "-am", "ip v2")
    return project


@pytest.mark.parametrize("revision", ["head", "head-sha", "other-commit", "stored"])
def test_live_tree_notices(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    revision: str,
) -> None:
    project = project_with_live_changes(tmp_path)
    script_children(monkeypatch, lambda tables, _c: outcome_of(tables, [0]))
    asked = {
        "head": [],
        "head-sha": [git(project, "rev-parse", "HEAD")],
        "other-commit": ["HEAD~1"],
        "stored": [],
    }[revision]
    changes = "uncommitted changes in the working tree are not measured"
    drift = "submodule working-tree differences are not measured: ip"
    if revision in ("other-commit", "stored"):
        # The same live state does warn for HEAD: only the other commit goes without notices.
        assert run_command(monkeypatch, project) == 0
        first = capsys.readouterr()
        assert changes in first.err

    assert run_command(monkeypatch, project, *asked) == 0

    captured = capsys.readouterr()
    err = captured.err
    if revision == "stored":
        assert captured.out == first.out
        assert changes in err and "working-tree differences" not in err
        assert err.index(changes) < err.index("is already Measured by run")
    elif revision == "other-commit":
        assert changes not in err and "working-tree differences" not in err
    else:
        assert changes in err and drift in err
        assert "untracked" not in err


def stealth_project(tmp_path: Path, metrics: str) -> Path:
    """A project whose ``.booley/`` is its own Git repository, excluded from the RTL one."""
    project = write_metrics_project(tmp_path / "proj", metrics)
    git(project, "init", "-q", "-b", "main")
    exclude = project / ".git" / "info" / "exclude"
    exclude.write_bytes(exclude.read_bytes() + b"/.booley/\n")
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "rtl")
    data = project / ".booley"
    git(data, "init", "-q", "-b", "main")
    git(data, "add", "-A")
    git(data, "commit", "-q", "-m", "booley directory")
    return project


@pytest.mark.parametrize("edited", ["tests.toml", "metrics.toml"])
def test_stealth_notes_uncommitted_booley_directory_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    edited: str,
) -> None:
    project = stealth_project(tmp_path, ONE_LINT_TABLE)
    script_children(monkeypatch, lambda tables, _c: outcome_of(tables, [0]))
    assert run_command(monkeypatch, project) == 0
    capsys.readouterr()
    assert run_command(monkeypatch, project) == 0
    clean = capsys.readouterr()
    assert "note:" not in clean.err
    before = {path: path.read_bytes() for path in stored_runs(project)}
    with (project / ".booley" / edited).open("ab") as handle:
        handle.write(b"# edited\n")
    marker = "uncommitted changes in the Booley Directory's repository"

    for arguments in ((), ("--again",)):
        assert run_command(monkeypatch, project, *arguments) == 0
        captured = capsys.readouterr()
        lines = [text for text in captured.err.splitlines() if marker in text]
        assert len(lines) == 1, captured.err
        line = lines[0]
        assert ("metrics.toml" in line) is (edited == "metrics.toml")
        assert "working tree" not in captured.err
        assert "note:" not in captured.out
        if not arguments:
            assert captured.out == clean.out
            assert {path: path.read_bytes() for path in stored_runs(project)} == before

    git(project, "commit", "-q", "--allow-empty", "-m", "same tree")
    for arguments in (("HEAD~1",), ("--again", "HEAD~1")):
        assert run_command(monkeypatch, project, *arguments) == 0
        assert "note:" not in capsys.readouterr().err


def test_an_unreadable_status_does_not_hide_the_stored_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = committed_project(tmp_path, ONE_LINT_TABLE)
    (project / "scratch.txt").write_bytes(b"untracked\n")  # an untracked file is no change
    script_children(monkeypatch, lambda tables, _c: outcome_of(tables, [0]))
    assert run_command(monkeypatch, project) == 0
    clean = capsys.readouterr()
    assert "note:" not in clean.err
    (project / ".git" / "index").write_bytes(b"not an index")

    assert run_command(monkeypatch, project) == 0
    captured = capsys.readouterr()
    assert captured.out == clean.out
    assert "note:" not in captured.err


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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _measuring_environment: Path,
    capsys: pytest.CaptureFixture[str],
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
    (path,) = stored_runs(project)
    assert path.with_suffix(".md").is_file()
    assert f"Run {path.stem}: cancelled" in capsys.readouterr().out


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

    err = capsys.readouterr().err
    assert "nothing stored" in err  # a cancelled run prints no rule
    assert UNMET_RULE not in err and ERROR_RULE not in err
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


@pytest.mark.parametrize(
    "numbers",
    [[signal.SIGINT, signal.SIGINT], [signal.SIGTERM], [HANGUP]],
    ids=["two-ctrl-c", "term", "hangup"],
)
def test_signals_during_the_children_cancel_the_measurement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, numbers: list[int]
) -> None:
    project = committed_project(tmp_path, TWO_LINT_TABLES)

    children_cancelled_by(project, monkeypatch, numbers)

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
        send(HANGUP)
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


def test_a_signal_while_storing_leaves_a_complete_run_and_a_matching_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = committed_project(tmp_path, ONE_LINT_TABLE)
    script_children(monkeypatch, lambda tables, _c: outcome_of(tables, [0]))
    write = MeasurementStore.write

    def write_then_signal(store: MeasurementStore, *args: object, **kwargs: object):
        stored = write(store, *args, **kwargs)  # type: ignore[arg-type]
        send(signal.SIGINT)  # too late to change what is stored
        return stored

    monkeypatch.setattr(MeasurementStore, "write", write_then_signal)

    assert run_command(monkeypatch, project) == 0

    (path,) = stored_runs(project)
    assert json.loads(path.read_text())["status"] == "completed"
    assert MeasurementStore(project / ".booley").measured(key_of(project)) is not None


class _BrokenPipe:
    """A stdout whose reader is gone."""

    def write(self, _text: str) -> int:
        raise BrokenPipeError

    def flush(self) -> None:
        raise BrokenPipeError


@pytest.mark.parametrize(
    ("failing", "note"),
    [
        ("view", "the report of cancelled run {} cannot be built: "),
        ("markdown", "the Markdown report of cancelled run {} cannot be written: "),
        ("print", "the report of cancelled run {} cannot be printed: "),
    ],
    ids=["view", "markdown", "print"],
)
def test_a_cancelled_run_whose_report_fails_exits_130_with_a_note(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failing: str,
    note: str,
) -> None:
    project = committed_project(tmp_path, TWO_LINT_TABLES)
    aside = tmp_path / "git-aside"

    def children(tables: Sequence[TableRules], _cancelled: object) -> ChildrenOutcome:
        if failing == "view":  # a real Git failure: the store needs no Git, the view does
            (project / ".git").rename(aside)
        if failing == "print":  # the terminal is outside Booley: a closed pipe
            monkeypatch.setattr(sys, "stdout", _BrokenPipe())
        return outcome_of(tables, [0, 0], status=RunStatus.CANCELLED, finished=[True, False])

    script_children(monkeypatch, children)
    write = MeasurementStore.write

    def write_with_blocked_markdown(store: MeasurementStore, *args: object, **kwargs: object):
        stored = write(store, *args, **kwargs)  # type: ignore[arg-type]
        if failing == "markdown":  # a directory in the way makes the atomic replace fail
            stored.path.with_suffix(".md").mkdir()
        return stored

    # No public seam fails only the Markdown write, so the store's write is wrapped (as in the
    # signal-while-storing test); the failure itself is a real OSError from the replace.
    monkeypatch.setattr(MeasurementStore, "write", write_with_blocked_markdown)

    with pytest.raises(KeyboardInterrupt):
        run_command(monkeypatch, project)

    if aside.exists():
        aside.rename(project / ".git")
    monkeypatch.undo()
    assert_cancelled_run_stored(project)
    captured = capsys.readouterr()
    (path,) = stored_runs(project)
    notes = [line for line in captured.err.splitlines() if line.startswith("note:")]
    assert len(notes) == 1
    assert notes[0].startswith("note: " + note.format(path.stem))
    assert "Traceback" not in captured.err
    assert "measurement failed" not in captured.err
    assert (f"Run {path.stem}: cancelled" in captured.out) is (failing == "markdown")
    assert path.with_suffix(".md").is_file() is (failing == "print")
    if failing == "print":  # a bare exception still names what failed
        assert notes[0].endswith(": BrokenPipeError")


def test_a_cancelled_run_with_no_terminal_still_ends_as_a_ctrl_c(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = committed_project(tmp_path, TWO_LINT_TABLES)

    def children(tables: Sequence[TableRules], _cancelled: object) -> ChildrenOutcome:
        monkeypatch.setattr(sys, "stdout", _BrokenPipe())
        monkeypatch.setattr(sys, "stderr", _BrokenPipe())  # the note cannot be written either
        return outcome_of(tables, [0, 0], status=RunStatus.CANCELLED, finished=[True, False])

    script_children(monkeypatch, children)

    with pytest.raises(KeyboardInterrupt):
        run_command(monkeypatch, project)

    monkeypatch.undo()
    assert_cancelled_run_stored(project)


# kind: its metrics table; every Flow leaves progress and hidden temporaries beside its reports
TABLE_OF_KIND = {
    "lint": "[sim_core.lint]\nwarnings_max = 0\n",
    "sim": "[sim_core.sim]\ntests_failed_max = 0\n",
    "coverage": "[sim_core.coverage]\nline_coverage_min = 90\n",
    "synth": "[synth_core.synth]\narea_kge_max = 60\n",
    "fpga": "[synth_core.fpga]\nlut_count_max = 5000\n",
    "mutation": "[sim_core.mutation]\nmutation_score_min = 50\ncount = 1\nseed = 1\n",
}
BOOKKEEPING = ("progress.json", ".progress.lock", ".report.json.1.tmp", ".progress-3")


@pytest.mark.parametrize("kind", list(TABLE_OF_KIND))
def test_only_report_artifacts_are_stored_listed_and_counted(
    kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.measurement import run_store

    reports = ("report.json", "lint_report.json") if kind == "lint" else ("report.json",)
    project = committed_project(tmp_path, TABLE_OF_KIND[kind])
    invocation = tmp_path / "invocation"
    invocation.mkdir()
    for name in (*reports, *BOOKKEEPING):
        (invocation / name).write_bytes(b"{}")
    with contextlib.suppress(OSError):  # a symlink is never stored; Windows may not allow one
        (invocation / "tool.log").symlink_to(invocation / "report.json")
    # A counted bookkeeping file would push a real report out, or itself show as dropped.
    monkeypatch.setattr(run_store, "REPORT_CAP_BYTES", 2 * len(reports))

    def run(tables: Sequence[TableRules], _cancelled: Callable[[], bool]) -> ChildrenOutcome:
        scripted = outcome_of(tables, [None])
        measured = TableMeasurement(scripted.tables[0].run, (invocation / "report.json",), True)
        return ChildrenOutcome(scripted.status, (measured,))

    script_children(monkeypatch, run)
    run_command(monkeypatch, project)

    (path,) = stored_runs(project)
    document = json.loads(path.read_text())
    assert [entry["name"] for entry in document["reports"]] == list(reports)
    assert document["dropped_reports"] == []
    assert path.with_suffix(".md").read_text().count(" report: `") == len(reports)


def test_the_signal_handlers_are_restored_after_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = committed_project(tmp_path, ONE_LINT_TABLE)
    script_children(monkeypatch, lambda tables, _c: outcome_of(tables, [0]))
    before = {n: signal.getsignal(n) for n in (signal.SIGINT, signal.SIGTERM, HANGUP)}

    assert run_command(monkeypatch, project) == 0

    assert {n: signal.getsignal(n) for n in before} == before
    monkeypatch.delattr(signal, "SIGHUP", raising=False)  # as on Windows
    assert run_command(monkeypatch, project, "--again") == 0
    assert {n: signal.getsignal(n) for n in before} == before


def test_a_measurement_outside_the_main_thread_runs_without_signal_handlers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = committed_project(tmp_path, ONE_LINT_TABLE)
    script_children(monkeypatch, lambda tables, _c: outcome_of(tables, [0]))
    codes: list[int] = []

    worker = threading.Thread(target=lambda: codes.append(run_command(monkeypatch, project)))
    worker.start()
    worker.join(DEADLINE_S)

    assert not worker.is_alive()
    assert codes == [0]
    assert len(stored_runs(project)) == 1


# ---------------------------------------------------------------- measuring one Target (#3955)


def test_a_target_run_is_kept_with_its_job_and_stores_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = committed_project(tmp_path, TWO_LINT_TABLES)
    report = tmp_path / "children" / "report.json"
    report.parent.mkdir()
    report.write_bytes(b'{"lint": "ok"}')

    def run(tables: Sequence[TableRules], _cancelled: Callable[[], bool]) -> ChildrenOutcome:
        outcome = outcome_of(tables, [0] * len(tables))
        first, *rest = outcome.tables
        kept = TableMeasurement(first.run, (report,), True)
        return ChildrenOutcome(outcome.status, (kept, *rest))

    steps = script_children(monkeypatch, run)

    assert run_command(monkeypatch, project, "--target", "sim_two") == 0

    captured = capsys.readouterr()
    assert stored_runs(project) == []
    assert not (project / ".booley" / "measurements").exists()
    run_id = job_record(project).run_id
    (document,) = (job_dir(project) / "measurements").glob(f"*/{run_id}.json")
    assert document.with_suffix(".md").is_file()
    tables = json.loads(document.read_text())["tables"]
    assert [table["identity"].split("#")[1] for table in tables] == ["sim_two"]
    kept_report = document.parent / run_id / "0-lint" / "report.json"
    assert kept_report.read_bytes() == report.read_bytes()
    assert str(document.with_suffix(".md")) in job_log(project)
    assert f"Run {run_id}: not stored" in captured.out
    assert job_record(project).argv == ["booley", "measure", "--target", "sim_two"]
    # The commit is not Measured: the next plain measurement runs the children.
    assert run_command(monkeypatch, project) == 0
    assert steps == ["prepare", "run", "prepare", "run"]
    assert len(stored_runs(project)) == 1


def test_a_target_run_exits_like_a_full_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = committed_project(tmp_path, TWO_LINT_TABLES)
    for warnings, code in ((3, 1), (None, 2)):
        script_children(monkeypatch, lambda tables, _c, w=warnings: outcome_of(tables, [w]))

        assert run_command(monkeypatch, project, "--target", "sim_core") == code

    exits = [
        r.exit_code
        for r in job_records.list_records(
            standalone_jobs_dir(project / ".booley"), skip_malformed=True
        )
        if r.endpoint == "measure"
    ]
    assert sorted(exits) == [1, 2]
    assert stored_runs(project) == []


@pytest.mark.parametrize(
    ("arguments", "code", "said", "runs"),
    [
        (["--target", "nope"], 2, "Unknown target 'nope'", 1),
        (["--target", "sim_fifo"], 2, NOTHING_IN_SCOPE, 1),
        (["--all", "--target", "sim_core"], 0, ": not stored", 2),
    ],
)
def test_a_target_on_a_measured_commit_is_validated_and_measured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    code: int,
    said: str,
    runs: int,
) -> None:
    project = committed_project(tmp_path, ONE_LINT_TABLE)
    steps = script_children(monkeypatch, lambda tables, _c: outcome_of(tables, [0]))
    assert run_command(monkeypatch, project) == 0
    (first,) = stored_runs(project)
    capsys.readouterr()

    assert run_command(monkeypatch, project, *arguments) == code

    captured = capsys.readouterr()
    assert said in captured.out + captured.err
    assert "is already Measured" not in captured.err
    assert steps == ["prepare", "run"] * runs
    assert stored_runs(project) == [first]
    assert MeasurementStore(project / ".booley").measured(key_of(project)).path == first


def test_validate_init_defaults_bound_listed_sim_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The Setup "now" recipe: listed `.sim` headers of Targets with Tests join the defaults."""
    from booley.harness.init_cmd import METRICS_TOML_SKELETON
    from booley.measurement.metrics_listing import render_metrics_listing

    write_metrics_project(tmp_path)
    (tmp_path / ".booley" / "tests.toml").write_bytes(
        b'[sim_core]\ntests = ["smoke"]\n[sim_two]\ntests = ["a", "b"]\n[shared]\ntests = ["t"]\n'
    )
    headers: list[str] = []
    for block in render_metrics_listing(tmp_path).split("\n\n"):
        header = block.splitlines()[0]
        # A Target with registered Tests lists a per-test bound line.
        if header.endswith(".sim]") and '.cycle_count_max" =' in block:
            headers.append(header)
    assert len(headers) == 4
    assert any(header.startswith('["') for header in headers)  # quoted identities
    (tmp_path / ".booley" / "metrics.toml").write_bytes(
        (METRICS_TOML_SKELETON + "\n" + "\n\n".join(headers) + "\n").encode()
    )
    capsys.readouterr()
    assert _measure(monkeypatch, tmp_path, "--validate") == 0
    out = capsys.readouterr().out
    assert "4 tables" in out
    assert out.count("1 bounds") == 4


MUTATION_TABLE = "[sim_core.mutation]\nmutation_score_min = 50\ncount = {count}\nseed = {seed}\n"


def test_a_mutation_table_runs_its_child_with_count_and_seed_and_keys_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3: the child gets the table's settings, and a changed setting is a new run key."""
    from booley.harness import measure_children
    from booley.harness.checkout_flow import (
        CheckoutFlowRequest,
        CheckoutFlowResult,
        CheckoutFlowStatus,
    )

    requests: list[CheckoutFlowRequest] = []

    class Instant:
        """A child that has already ended with a mutation score of 75."""

        pid = 4_194_305  # above every Linux PID: no live process has it

        def start(self) -> Instant:
            return self

        def discard(self) -> None:
            pass

        def poll(self) -> bool:
            return True

        def settle(self) -> CheckoutFlowResult:
            values = FlowMetricValues(plain={"mutation_score": 75.0}, per_clock={})
            return CheckoutFlowResult(
                "run",
                CheckoutFlowStatus.PASSED,
                0,
                (Path("report.json"),),
                {"sim_core": values},
                Path("job.log"),
            )

    def prepare(request: CheckoutFlowRequest) -> Instant:
        requests.append(request)
        return Instant()

    monkeypatch.setattr(measure_children, "prepare_checkout_flow", prepare)
    project = committed_project(tmp_path, MUTATION_TABLE.format(count=4, seed=3))
    keys = [key_of(project)]
    assert run_command(monkeypatch, project) == 0
    for count, seed in ((5, 3), (5, 4)):  # change one setting at a time, each in its own commit
        (project / ".booley" / "metrics.toml").write_bytes(
            MUTATION_TABLE.format(count=count, seed=seed).encode()
        )
        git(project, "commit", "-q", "-a", "-m", f"count {count} seed {seed}")
        keys.append(key_of(project))
        assert run_command(monkeypatch, project) == 0

    assert [(r.flow, r.target, dict(r.settings)) for r in requests] == [
        ("mutation", "sim_core", {"count": 4, "seed": 3}),
        ("mutation", "sim_core", {"count": 5, "seed": 3}),
        ("mutation", "sim_core", {"count": 5, "seed": 4}),
    ]
    assert len(set(keys)) == 3
    store = MeasurementStore(project / ".booley")
    assert [len(store.history(key)) for key in keys] == [1, 1, 1]
