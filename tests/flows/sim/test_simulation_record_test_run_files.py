"""The sim card and ``simulation.json`` name each Test Run's log and waveform (issue #2758).

Most tests run a real Campaign through the faked process edge of
``simulation_record_boundary`` (so the evidence files are the ones a Campaign records)
and read the card, ``simulation.json`` and the MCP detail it publishes. The
card's row budget is checked on rows and files built directly.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from booley.flows.base import SubprocessResult
from booley.flows.sim.record.test_run_report import (
    informative_test_run_lines,
    test_run_report_lines,
    test_run_row,
)
from booley.flows.test_runs import DEFAULT_SEED, build_test_run
from tests.flows.sim.simulation_record_boundary import (
    Launch,
    SimulationRecordSimRun,
    run_simulation_record_sim,
)

_LOG = "      log: "
_WAVEFORM = "      waveform: "
# Evidence files as the renderer reads them: any object with ``log`` and ``trace``.
_NO_FILES = SimpleNamespace(log=None, trace=None)


def _simulate(launch: Launch, *, failing: frozenset[str], traced: bool) -> SubprocessResult:
    """The fake simulator: fails the Tests in *failing*; when *traced* it dumps a waveform."""
    failed = bool(failing & set(launch.tests))
    output = f"[SIM_RESULT] {'FAILED' if failed else 'PASSED'}\n"
    if traced:
        dump = launch.cwd / f"{'+'.join(launch.tests)}.fst"
        dump.write_bytes(b"waveform")
        output += f"TRACE_OK: {dump}\n"
    return SubprocessResult(returncode=int(failed), stdout=output, duration_s=0.1)


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tests: list[str],
    *,
    failing: tuple[str, ...] = (),
    traced: bool = False,
    **kwargs: Any,
) -> tuple[SimulationRecordSimRun, dict[str, Any]]:
    """Run Target ``lite`` over *tests*; return the run and its ``simulation.json``."""
    run = run_simulation_record_sim(
        tmp_path,
        monkeypatch,
        {"lite": tests},
        lambda launch: _simulate(launch, failing=frozenset(failing), traced=traced),
        target="lite",
        trace=traced,
        **kwargs,
    )
    simulation = json.loads(
        _target_directory(tmp_path).joinpath("simulation.json").read_text("utf-8")
    )
    return run, simulation


def _target_directory(tmp_path: Path) -> Path:
    return tmp_path / "reports" / "sim" / "1" / "targets" / "lite"


def _card(run: SimulationRecordSimRun) -> list[str]:
    """The Test Run block of the card: from ``Test Runs:`` to the end of the report text."""
    lines = run.result.outcome.report_text.splitlines()
    starts = [index for index, line in enumerate(lines) if line.startswith("  Test Runs:")]
    assert starts, "the card lists no Test Runs"
    return lines[starts[0] :]


def _path(line: str, prefix: str) -> Path:
    assert line.startswith(prefix), f"expected a {prefix.strip()} line, found {line!r}"
    return Path(line.removeprefix(prefix))


def test_failed_test_run_shows_its_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    run, simulation = _run(tmp_path, monkeypatch, ["ok", "bad"], failing=("bad",))
    card = _card(run)

    assert card[1:3] == ["    PASS         ok@1", "    FAIL         bad@1"]
    log = _path(card[3], _LOG)
    assert log.is_file()
    assert log.name.endswith("-run.log")
    # The rerun block follows straight after: the log line is the only addition.
    assert card[4] == "  Rerun a failed Test Run with the same seed:"
    # The card prints the resolved path; simulation.json holds it relative.
    bad = simulation["tests"][1]
    assert (_target_directory(tmp_path) / bad["log"]).resolve() == log.resolve()


@pytest.mark.parametrize("traced", [True, False], ids=["traced", "untraced"])
def test_waveform_line_only_for_a_traced_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, traced: bool
):
    run, simulation = _run(tmp_path, monkeypatch, ["ok", "bad"], failing=("bad",), traced=traced)
    card = _card(run)

    failed = card.index("    FAIL         bad@1")
    log = _path(card[failed + 1], _LOG)
    assert log.is_file()
    if traced:
        waveform = _path(card[failed + 2], _WAVEFORM)
        assert waveform.is_file()
        assert waveform.name.endswith("bad.fst")
        assert (_target_directory(tmp_path) / simulation["tests"][1]["trace"]).resolve() == (
            waveform.resolve()
        )
    else:
        assert not any(line.startswith(_WAVEFORM) for line in card)
        assert simulation["tests"][1]["trace"] is None


def _files(directory: Path, *, log: bool = True, trace: bool = False) -> Any:
    """Evidence files written into *directory*."""
    directory.mkdir(parents=True, exist_ok=True)
    found = {"log": directory / "0001-run.log", "trace": directory / "0002-trace.fst"}
    for path in found.values():
        path.write_bytes(b"evidence")
    return SimpleNamespace(
        log=found["log"] if log else None, trace=found["trace"] if trace else None
    )


def _row(name: str, grade: str) -> dict[str, object]:
    """The report row of Test *name* at seed 1 that ended as *grade*."""
    return test_run_row(build_test_run(name, seed=1), grade)


@pytest.mark.parametrize("grade", ["inconclusive", "error"])
def test_inconclusive_and_error_runs_show_the_same_lines(tmp_path: Path, grade: str):
    rows = [_row("ok", "pass"), _row("odd", grade)]
    files = [_files(tmp_path / "ok", trace=True), _files(tmp_path / "odd", trace=True)]
    lines = test_run_report_lines("lite", rows, files=files)

    odd = files[1]
    assert odd.log is not None and odd.trace is not None
    at = lines.index(f"    {grade.upper():<12} odd@1")
    assert lines[at + 1 : at + 3] == [
        f"{_LOG}{odd.log.resolve()}",
        f"{_WAVEFORM}{odd.trace.resolve()}",
    ]


def test_inconclusive_run_of_a_simulation_record_shows_its_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    def silent(launch: Launch) -> SubprocessResult:
        return SubprocessResult(returncode=0, stdout="no sentinel\n", duration_s=0.1)

    run = run_simulation_record_sim(
        tmp_path, monkeypatch, {"lite": ["odd"]}, silent, target="lite"
    )
    card = _card(run)

    assert card[1] == "    INCONCLUSIVE odd@1"
    assert _path(card[2], _LOG).is_file()


def test_passing_test_runs_of_an_untraced_run_print_no_path_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    run, _ = _run(tmp_path, monkeypatch, ["ok", "bad"], failing=("bad",))
    card = _card(run)

    # A PASS row beside a FAIL row with a log: only the FAIL row has a path.
    assert [line for line in card if line.startswith("      ")] == [card[3]]
    assert card[1].startswith("    PASS ")
    assert card[2].startswith("    FAIL ")


def test_all_passing_untraced_run_prints_no_test_run_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    run, _ = _run(tmp_path, monkeypatch, ["ok"])

    assert "Test Runs:" not in run.result.outcome.report_text


def test_passing_test_run_of_a_traced_run_shows_its_waveform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    run, simulation = _run(tmp_path, monkeypatch, ["irq"], traced=True)
    card = _card(run)

    assert card[:2] == ["  Test Runs: 1 (1 passed)", "    PASS         irq@1"]
    waveform = _path(card[2], _WAVEFORM)
    assert waveform.is_file()
    # The log of a passing run stays in simulation.json; the card has no rerun block.
    assert len(card) == 3
    assert simulation["tests"][0]["log"] is not None
    assert (_target_directory(tmp_path) / simulation["tests"][0]["trace"]).resolve() == (
        waveform.resolve()
    )


def test_build_failure_card_has_no_log_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    run, _ = _run(
        tmp_path,
        monkeypatch,
        ["first", "second"],
        build=SubprocessResult(returncode=1, stdout="compile failed\n"),
    )
    card = _card(run)

    assert card == [
        "  Test Runs: 2 (2 error)",
        "    ERROR        first@1",
        "    ERROR        second@1",
    ]


def test_non_passing_run_without_a_log_renders_without_a_log_line(tmp_path: Path):
    """An unlogged run beside a logged one: only the logged run gets a path."""
    rows = [_row("crashed", "error"), _row("bad", "fail")]
    files = [_NO_FILES, _files(tmp_path / "bad")]
    lines = test_run_report_lines("lite", rows, files=files)

    assert lines[1:4] == [
        "    ERROR        crashed@1",
        "    FAIL         bad@1",
        f"{_LOG}{files[1].log.resolve()}",  # type: ignore[union-attr]
    ]


def test_rows_carry_null_paths_when_files_are_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    run, simulation = _run(
        tmp_path,
        monkeypatch,
        ["first"],
        build=SubprocessResult(returncode=1, stdout="compile failed\n"),
    )
    report = json.loads((tmp_path / "reports/sim/1/report.json").read_text("utf-8"))
    (preview,) = report["detail"]["records"]["lite"]["observations"]

    (row,) = simulation["tests"]
    assert (row["log"], row["trace"]) == (None, None)
    assert (preview["log"], preview["trace"]) == (None, None)
    assert run.result.exit_code != 0


def _simulation_record_stand_in(tmp_path: Path) -> SimpleNamespace:
    """The outcome of the campaign under *tmp_path*, as the lookup reads it from disk."""
    simulation_record = _target_directory(tmp_path) / "record"
    results = sorted(simulation_record.glob("work-items/*/result.json"))
    consumed = [
        {
            "work_item_id": json.loads(path.read_text("utf-8"))["work_item_id"],
            "result": {"path": path.relative_to(tmp_path / "reports/sim/1").as_posix()},
        }
        for path in results
    ]
    return SimpleNamespace(
        manifest_path=simulation_record / "manifest.json",
        observations=tuple({"work_item_id": item["work_item_id"]} for item in consumed),
        acceptance_facts=SimpleNamespace(document={"consumed_results": consumed}),
    )


def test_paths_name_the_attempt_the_simulation_record_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A resumed work item is accepted on its second attempt: the log is that attempt's."""
    from booley.flows.sim.record.codec import canonical_json_bytes
    from booley.flows.sim.record.test_run_files import simulation_record_test_run_files

    _run(tmp_path, monkeypatch, ["bad"], failing=("bad",))
    outcome = _simulation_record_stand_in(tmp_path)
    (result,) = (_target_directory(tmp_path) / "record").glob("work-items/*/result.json")
    document = json.loads(result.read_text("utf-8"))
    first = result.parent / "attempts" / f"0001-{document['attempt_id']}"
    accepted = result.parent / "attempts" / f"0002-{document['attempt_id']}"

    # The Campaign accepted attempt 2; attempt 1 (an interrupted try) left its own log.
    first.rename(accepted)
    first.joinpath("evidence").mkdir(parents=True)
    first.joinpath("evidence", "0001-run.log").write_bytes(b"interrupted attempt")
    document["attempt_ordinal"] = 2
    result.write_bytes(canonical_json_bytes(document))

    (files,) = simulation_record_test_run_files(outcome)
    assert files.log is not None
    assert files.log.parents[1] == accepted


def test_lookup_yields_no_files_for_unreadable_or_missing_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from booley.flows.sim.record.codec import canonical_json_bytes
    from booley.flows.sim.record.test_run_files import (
        TestRunFiles,
        simulation_record_test_run_files,
    )

    _run(tmp_path, monkeypatch, ["bad"], failing=("bad",))
    outcome = _simulation_record_stand_in(tmp_path)
    (result,) = (_target_directory(tmp_path) / "record").glob("work-items/*/result.json")

    # No consumed result for the observation's work item.
    stranger = SimpleNamespace(
        manifest_path=outcome.manifest_path,
        observations=({"work_item_id": "0099-missing"},),
        acceptance_facts=outcome.acceptance_facts,
    )
    assert simulation_record_test_run_files(stranger) == (TestRunFiles(None, None),)
    # No acceptance facts at all.
    bare = SimpleNamespace(manifest_path=outcome.manifest_path, observations=outcome.observations)
    assert simulation_record_test_run_files(bare) == (TestRunFiles(None, None),)  # type: ignore[arg-type]
    # A manifest kept outside an invocation names no results.
    stray = SimpleNamespace(
        manifest_path=Path("manifest.json"),
        observations=outcome.observations,
        acceptance_facts=outcome.acceptance_facts,
    )
    assert simulation_record_test_run_files(stray) == (TestRunFiles(None, None),)
    # A record that is not a result, or has gone.
    result.write_bytes(b"not json")
    assert simulation_record_test_run_files(outcome) == (TestRunFiles(None, None),)
    # Canonical JSON whose fields have the wrong shape is no result either.
    result.write_bytes(canonical_json_bytes({"evidence": [{}]}))
    assert simulation_record_test_run_files(outcome) == (TestRunFiles(None, None),)
    result.unlink()
    assert simulation_record_test_run_files(outcome) == (TestRunFiles(None, None),)


def test_a_reference_that_escapes_its_attempt_names_no_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from booley.flows.sim.record.codec import canonical_json_bytes
    from booley.flows.sim.record.test_run_files import (
        TestRunFiles,
        simulation_record_test_run_files,
    )

    _run(tmp_path, monkeypatch, ["bad"], failing=("bad",))
    outcome = _simulation_record_stand_in(tmp_path)
    (result,) = (_target_directory(tmp_path) / "record").glob("work-items/*/result.json")
    document = json.loads(result.read_text("utf-8"))
    for reference in document["evidence"]:
        if reference["kind"] == "live_run_log":
            # Four levels up from the attempt directory is the Campaign manifest: a
            # file that exists, so only the containment check can refuse it.
            reference["path"] = "../../../../manifest.json"
    result.write_bytes(canonical_json_bytes(document))

    assert (result.parents[2] / "manifest.json").is_file()
    assert simulation_record_test_run_files(outcome) == (TestRunFiles(None, None),)


def test_path_lines_are_capped(tmp_path: Path):
    from booley.flows.sim.record.test_run_report import PATH_ROW_LIMIT

    rows = [_row(f"t{n}", "fail") for n in range(PATH_ROW_LIMIT + 2)]
    files = [_files(tmp_path / f"t{n}") for n in range(len(rows))]
    lines = test_run_report_lines("lite", rows, files=files)

    assert sum(line.startswith(_LOG) for line in lines) == PATH_ROW_LIMIT
    # The first eight rows carry their path; the last two do not.
    for number, row in enumerate(rows):
        position = lines.index(f"    FAIL         {row['name']}")
        assert lines[position + 1].startswith(_LOG) == (number < PATH_ROW_LIMIT)
    assert "  Log and waveform paths of 2 more Test Runs: simulation.json tests[]" in lines


def test_path_line_budget_goes_to_non_passing_runs_first(tmp_path: Path):
    from booley.flows.sim.record.test_run_report import PATH_ROW_LIMIT

    rows = [_row(f"p{n}", "pass") for n in range(10)] + [_row("bad", "fail")]
    files = [_files(tmp_path / f"p{n}", trace=True) for n in range(10)]
    files.append(_files(tmp_path / "bad", trace=True))
    lines = test_run_report_lines("lite", rows, files=files)

    bad = lines.index("    FAIL         bad@1")
    assert lines[bad + 1].startswith(_LOG) and lines[bad + 2].startswith(_WAVEFORM)
    # Seven passing waveforms fit beside it, in row order; the other three do not.
    for number in range(10):
        position = lines.index(f"    PASS         p{number}@1")
        assert lines[position + 1].startswith(_WAVEFORM) == (number < PATH_ROW_LIMIT - 1)
    assert "  Log and waveform paths of 3 more Test Runs: simulation.json tests[]" in lines


def test_rows_stay_hidden_for_passing_runs_without_a_waveform(tmp_path: Path):
    rows = [_row("ok", "pass")]
    files = [_files(tmp_path / "ok")]

    assert informative_test_run_lines("lite", rows, default_seed=DEFAULT_SEED, files=files) == []
    traced = [_files(tmp_path / "ok", trace=True)]
    assert informative_test_run_lines("lite", rows, default_seed=DEFAULT_SEED, files=traced) != []
