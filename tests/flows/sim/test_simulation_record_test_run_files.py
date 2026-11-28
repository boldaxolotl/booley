"""The sim card and ``simulation.json`` name each Test Run's log and waveform (issue #2758).

Most tests run a real Simulation Record through the faked process edge of
``simulation_record_boundary`` (so the evidence files are the ones the Simulation Record keeps)
and read the card, ``simulation.json`` and the MCP detail it publishes. The
card's row budget is checked on rows and files built directly.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from booley.flows.base import SubprocessResult
from booley.flows.run_log import write_run_log_progress
from booley.flows.sim.record.codec import canonical_json_bytes
from booley.flows.sim.record.test_run_files import TestRunFiles, simulation_record_test_run_files
from booley.flows.sim.record.test_run_report import (
    PATH_ROW_LIMIT,
    informative_test_run_lines,
    test_run_report_lines,
    test_run_row,
)
from booley.flows.sim.trace_session import TraceArtifact
from booley.flows.test_runs import DEFAULT_SEED, build_test_run
from tests.flows.sim.simulation_record_boundary import (
    Launch,
    SimulationRecordSimRun,
    run_simulation_record_sim,
)

_LOG = "      log: "
_WAVEFORM = "      waveform: "
_NO_FILES = TestRunFiles(None, None)
_CUT = (
    "WARNING: input does not end with a newline; the final line (byte 351) "
    "may be a truncated record"
)


def _simulate(
    launch: Launch,
    *,
    failing: frozenset[str],
    traced: bool,
    trace_in_work_dir: bool = False,
    trace_subdir: str = "",
    on_live: Callable[[Launch], None] | None = None,
    warnings: tuple[str, ...] = (),
) -> SubprocessResult:
    """The fake simulator: fails the Tests in *failing*; when *traced* it dumps a waveform.

    *trace_in_work_dir* writes the waveform where the real wrapper does
    (``TraceSession.work_bwave_path``) instead of the run directory.
    """
    if on_live is not None:
        on_live(launch)
    failed = bool(failing & set(launch.tests))
    output = f"[SIM_RESULT] {'FAILED' if failed else 'PASSED'}\n"
    if traced:
        dump = (
            Path(launch.work.work_dir) / trace_subdir / "trace.fst"
            if trace_in_work_dir
            else launch.cwd / f"{'+'.join(launch.tests)}.fst"
        )
        dump.parent.mkdir(parents=True, exist_ok=True)
        dump.write_bytes(b"waveform")
        output += f"TRACE_OK: {dump}\n"
        if warnings:
            output += TraceArtifact(dump, 8, "tb", 1, 1, warnings).metadata_line() + "\n"
    return SubprocessResult(returncode=int(failed), stdout=output, duration_s=0.1)


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tests: list[str],
    *,
    failing: tuple[str, ...] = (),
    traced: bool = False,
    trace_in_work_dir: bool = False,
    trace_subdir: str = "",
    on_live: Callable[[Launch], None] | None = None,
    warnings: tuple[str, ...] = (),
    **kwargs: Any,
) -> tuple[SimulationRecordSimRun, dict[str, Any]]:
    """Run Target ``lite`` over *tests*; return the run and its ``simulation.json``."""
    run = run_simulation_record_sim(
        tmp_path,
        monkeypatch,
        {"lite": tests},
        lambda launch: _simulate(
            launch,
            failing=frozenset(failing),
            traced=traced,
            trace_in_work_dir=trace_in_work_dir,
            trace_subdir=trace_subdir,
            on_live=on_live,
            warnings=warnings,
        ),
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


@pytest.mark.parametrize("traced", [True, False], ids=["traced", "untraced"])
def test_failed_test_run_shows_its_log_and_a_waveform_only_when_traced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, traced: bool
):
    run, simulation = _run(tmp_path, monkeypatch, ["ok", "bad"], failing=("bad",), traced=traced)
    card = _card(run)
    target = _target_directory(tmp_path)
    bad = simulation["tests"][1]

    assert card[1] == "    PASS         ok@1"
    if not traced:
        assert card[2] == "    FAIL         bad@1"
    failed = card.index("    FAIL         bad@1")
    log = _path(card[failed + 1], _LOG)
    assert log.is_file()
    assert log.name == "run.log"
    # The card prints the resolved path; simulation.json holds it relative.
    assert (target / bad["log"]).resolve() == log.resolve()
    if traced:
        waveform = _path(card[failed + 2], _WAVEFORM)
        assert waveform.is_file()
        assert waveform.name.endswith("bad.fst")
        assert (target / bad["trace"]).resolve() == waveform.resolve()
    else:
        assert not any(line.startswith(_WAVEFORM) for line in card)
        assert bad["trace"] is None
    # The rerun block follows straight after: the path lines are the only addition.
    assert card[failed + 2 + traced] == "  Rerun a failed Test Run with the same seed:"


def _only_work_item(tmp_path: Path) -> tuple[Path, dict[str, Any], Path]:
    """The one result of the record under *tmp_path*: its path, document and attempt directory."""
    (result,) = (_target_directory(tmp_path) / "record").glob("work-items/*/result.json")
    document = json.loads(result.read_text("utf-8"))
    attempt = (
        result.parent / "attempts" / f"{document['attempt_ordinal']:04d}-{document['attempt_id']}"
    )
    return result, document, attempt


def test_traced_attempt_keeps_one_copy_of_each_evidence_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _run(tmp_path, monkeypatch, ["bad"], failing=("bad",), traced=True, trace_in_work_dir=True)
    _, _, attempt = _only_work_item(tmp_path)

    names = [
        path.name.split("-", 1)[1] if path.name[:4].isdigit() else path.name
        for path in attempt.rglob("*")
        if path.is_file()
    ]
    assert sum(name.endswith("run.log") for name in names) == 1
    assert sum(name.endswith(".fst") for name in names) == 1
    assert sum(name.startswith("adapter-") for name in names) == 1


def test_nested_trace_is_kept_in_place_with_every_new_folder_synced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from booley.flows.sim.record import serial_execution

    synced: list[Path] = []
    real = serial_execution.fsync_directory
    monkeypatch.setattr(
        serial_execution,
        "fsync_directory",
        lambda path: (synced.append(Path(path).resolve()), real(path))[1],
    )
    _run(
        tmp_path,
        monkeypatch,
        ["bad"],
        failing=("bad",),
        traced=True,
        trace_in_work_dir=True,
        trace_subdir="wave/dump",
    )
    _, document, attempt = _only_work_item(tmp_path)

    (trace,) = [item for item in document["evidence"] if item["kind"] == "trace"]
    assert trace["path"] == "execution-evidence/wave/dump/trace.fst"
    for directory in (
        "execution-evidence/wave/dump",
        "execution-evidence/wave",
        "execution-evidence",
    ):
        assert (attempt / directory).resolve() in synced
    assert attempt.resolve() in synced


def test_references_and_projection_name_the_kept_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    run, simulation = _run(
        tmp_path, monkeypatch, ["bad"], failing=("bad",), traced=True, trace_in_work_dir=True
    )
    _, document, attempt = _only_work_item(tmp_path)

    for reference in document["evidence"]:
        kept = (attempt / reference["path"]).read_bytes()
        assert len(kept) == reference["bytes"]
        assert hashlib.sha256(kept).hexdigest() == reference["sha256"].removeprefix("sha256:")
    paths = {item["kind"]: item["path"] for item in document["evidence"]}
    assert paths["live_run_log"] == "execution-evidence/run.log"
    assert paths["trace"] == "execution-evidence/trace.fst"
    card = _card(run)
    failed = card.index("    FAIL         bad@1")
    log = _path(card[failed + 1], _LOG)
    waveform = _path(card[failed + 2], _WAVEFORM)
    assert log.resolve() == (attempt / "execution-evidence/run.log").resolve()
    assert waveform.resolve() == (attempt / "execution-evidence/trace.fst").resolve()
    row = simulation["tests"][0]
    assert (_target_directory(tmp_path) / row["log"]).resolve() == log.resolve()
    assert (_target_directory(tmp_path) / row["trace"]).resolve() == waveform.resolve()


def test_live_log_is_the_log_the_result_keeps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    seen: dict[str, Any] = {}

    def live(launch: Launch) -> None:
        write_run_log_progress(launch.work.work_dir, "LIVE_MARKER\n", elapsed_s=0.0, line_count=1)
        progress = json.loads((tmp_path / "reports/sim/1/progress.json").read_text("utf-8"))
        seen["log"] = progress["active"][0]["log"]
        seen["text"] = (tmp_path / seen["log"]["path"]).read_text("utf-8")

    _run(tmp_path, monkeypatch, ["bad"], failing=("bad",), on_live=live)
    _, document, attempt = _only_work_item(tmp_path)

    assert seen["log"]["live"] is True
    assert "LIVE_MARKER" in seen["text"]
    (kept,) = [item for item in document["evidence"] if item["kind"] == "live_run_log"]
    assert (tmp_path / seen["log"]["path"]).resolve() == (attempt / kept["path"]).resolve()
    assert "[SIM_RESULT] FAILED" in (attempt / kept["path"]).read_text("utf-8")


def _files(directory: Path, *, log: bool = True, trace: bool = False) -> TestRunFiles:
    """Evidence files written into *directory*."""
    directory.mkdir(parents=True, exist_ok=True)
    found = {"log": directory / "0001-run.log", "trace": directory / "0002-trace.fst"}
    for path in found.values():
        path.write_bytes(b"evidence")
    return TestRunFiles(found["log"] if log else None, found["trace"] if trace else None)


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


@pytest.mark.parametrize("warned", [False, True], ids=["clean", "warned"])
def test_passing_test_run_of_a_traced_run_shows_its_waveform(
    warned: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    warnings = (_CUT, "WARNING: 2 value changes name no declared identifier") if warned else ()
    run, simulation = _run(tmp_path, monkeypatch, ["irq"], traced=True, warnings=warnings)
    card = _card(run)

    assert card[:2] == ["  Test Runs: 1 (1 passed)", "    PASS         irq@1"]
    waveform = _path(card[2], _WAVEFORM)
    assert waveform.is_file()
    # The log of a passing run stays in simulation.json; the card has no rerun block.
    # #5059: the conversion's warnings follow the waveform line, indented under it.
    assert card[3:] == (
        [f"        {_CUT}", "        WARNING: 2 value changes name no declared identifier"]
        if warned
        else []
    )
    _, document, _ = _only_work_item(tmp_path)
    assert [
        (item["code"], item["message"])
        for item in document["diagnostics"]
        if item["code"] == "trace_conversion"
    ] == [("trace_conversion", warning) for warning in warnings]
    assert document["grade"] == "pass"
    assert run.result.exit_code == 0
    assert simulation["tests"][0]["log"] is not None
    assert (_target_directory(tmp_path) / simulation["tests"][0]["trace"]).resolve() == (
        waveform.resolve()
    )


def test_build_failure_has_no_log_line_and_null_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    run, simulation = _run(
        tmp_path,
        monkeypatch,
        ["first", "second"],
        build=SubprocessResult(returncode=1, stdout="compile failed\n"),
    )
    report = json.loads((tmp_path / "reports/sim/1/report.json").read_text("utf-8"))
    previews = report["detail"]["records"]["lite"]["observations"]

    assert _card(run) == [
        "  Test Runs: 2 (2 error)",
        "    ERROR        first@1",
        "    ERROR        second@1",
    ]
    for row in (*simulation["tests"], *previews):
        assert (row["log"], row["trace"]) == (None, None)
    assert run.result.exit_code != 0


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


def _simulation_record_stand_in(tmp_path: Path) -> SimpleNamespace:
    """The outcome of the record under *tmp_path*, as the lookup reads it from disk."""
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
    _run(tmp_path, monkeypatch, ["bad"], failing=("bad",))
    outcome = _simulation_record_stand_in(tmp_path)
    (result,) = (_target_directory(tmp_path) / "record").glob("work-items/*/result.json")
    document = json.loads(result.read_text("utf-8"))
    first = result.parent / "attempts" / f"0001-{document['attempt_id']}"
    accepted = result.parent / "attempts" / f"0002-{document['attempt_id']}"

    # The Simulation Record accepted attempt 2; attempt 1 (an interrupted try) left its own log.
    first.rename(accepted)
    first.joinpath("evidence").mkdir(parents=True)
    first.joinpath("evidence", "0001-run.log").write_bytes(b"interrupted attempt")
    document["attempt_ordinal"] = 2
    result.write_bytes(canonical_json_bytes(document))

    (files,) = simulation_record_test_run_files(outcome)
    assert files.log is not None
    assert files.log.parents[1] == accepted


def _stranger(outcome: SimpleNamespace, result: Path) -> SimpleNamespace:
    """No consumed result for the observation's work item."""
    return SimpleNamespace(
        manifest_path=outcome.manifest_path,
        observations=({"work_item_id": "0099-missing"},),
        acceptance_facts=outcome.acceptance_facts,
    )


def _without_acceptance_facts(outcome: SimpleNamespace, result: Path) -> SimpleNamespace:
    return SimpleNamespace(manifest_path=outcome.manifest_path, observations=outcome.observations)


def _stray_manifest(outcome: SimpleNamespace, result: Path) -> SimpleNamespace:
    """A manifest kept outside an invocation names no results."""
    return SimpleNamespace(
        manifest_path=Path("manifest.json"),
        observations=outcome.observations,
        acceptance_facts=outcome.acceptance_facts,
    )


def _not_json(outcome: SimpleNamespace, result: Path) -> SimpleNamespace:
    result.write_bytes(b"not json")
    return outcome


def _wrong_shape(outcome: SimpleNamespace, result: Path) -> SimpleNamespace:
    """Canonical JSON whose fields have the wrong shape is no result either."""
    result.write_bytes(canonical_json_bytes({"evidence": [{}]}))
    return outcome


def _gone(outcome: SimpleNamespace, result: Path) -> SimpleNamespace:
    result.unlink()
    return outcome


def _escaping_reference(outcome: SimpleNamespace, result: Path) -> SimpleNamespace:
    """The live log reference climbs out of its attempt to a file that exists.

    Four levels up from the attempt directory is the Simulation Record manifest, so only
    the containment check can refuse it.
    """
    document = json.loads(result.read_text("utf-8"))
    for reference in document["evidence"]:
        if reference["kind"] == "live_run_log":
            reference["path"] = "../../../../manifest.json"
    result.write_bytes(canonical_json_bytes(document))
    assert (result.parents[2] / "manifest.json").is_file()
    return outcome


@pytest.mark.parametrize(
    "damage",
    [
        _stranger,
        _without_acceptance_facts,
        _stray_manifest,
        _not_json,
        _wrong_shape,
        _gone,
        _escaping_reference,
    ],
)
def test_lookup_yields_no_files_for_unreadable_or_missing_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: Callable[..., SimpleNamespace]
):
    _run(tmp_path, monkeypatch, ["bad"], failing=("bad",))
    outcome = _simulation_record_stand_in(tmp_path)
    (result,) = (_target_directory(tmp_path) / "record").glob("work-items/*/result.json")

    assert simulation_record_test_run_files(damage(outcome, result)) == (  # type: ignore[arg-type]
        TestRunFiles(None, None),
    )


def test_path_lines_are_capped(tmp_path: Path):
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
