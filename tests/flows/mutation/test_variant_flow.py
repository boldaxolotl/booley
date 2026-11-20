"""Behavioral tests for compiler-isolated mutation variants.

Each test runs a whole campaign on the fake Target of ``test_flow``: the Mutants' builds and
launches are answered per Mutant, and the assertions read the classified rows of the result.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from booley.flows.mutation.flow import UnsupportedSimTargetError, compute_source_size_budget
from booley.flows.mutation.memory_claims import CampaignClaims
from booley.flows.sim.adapter_contract import PreparedSimulationWork
from booley.flows.sim.adapter_transport import (
    AdapterResult,
    work_transport_identity,
    write_adapter_result,
)
from booley.flows.sim.backends import cocotb_results as cr
from booley.flows.sim.backends.cocotb_results import COCOTB_RESULTS_PREFIX
from booley.runtime.endpoint_execution import EXIT_ERROR, EndpointOutcome
from booley.targets.catalog import TargetCatalog
from tests.flows.failing_compiler import put_failing_iverilog_on_path
from tests.flows.mutation.test_flow import (
    _SCOPE,
    _campaign,
    _candidates,
    _fake_proc,
    _make_endpoint,
    _patch_fake_simulation,
    _prepare_scope_files,
    _results_markdown,
    _wrap_preparer,
    _write_tests_toml,
)
from tests.flows.sim.test_execution_engine import _real_fusesoc

_TESTS_TOML = '[lite]\ntests = ["full", "smoke"]\nselect = "+test={name}"\n'
_COCOTB_PASS = COCOTB_RESULTS_PREFIX + (
    '{"state":"ok","detail":"","tests":['
    '{"name":"reset","module":"tb","status":"pass","failure":"","elapsed_s":0.1}]}'
)


def _test_name(work: PreparedSimulationWork) -> str:
    """The test a launch selects: ``select = "+test={name}"`` puts ``test=<name>`` in the plusargs."""
    return next(arg.removeprefix("test=") for arg in work.plusargs if arg.startswith("test="))


def _mutant_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutants: list[Callable[[PreparedSimulationWork], subprocess.CompletedProcess[str]]],
    *,
    cocotb: bool = False,
    tests_toml: str = _TESTS_TOML,
) -> tuple[EndpointOutcome, list[dict[str, Any]], Any]:
    """Run one campaign of ``len(mutants)`` Mutants; Mutant ``i`` answers each launch by
    ``mutants[i - 1](work)``, the baseline passes. Returns the result, its rows, the fake."""
    _write_tests_toml(tmp_path, tests_toml)

    def answer(index: int | None, work: PreparedSimulationWork):
        if index is None:
            return _fake_proc(0, _COCOTB_PASS if cocotb else "[sim] ok")
        return mutants[index - 1](work)

    _, result, fake = _campaign(
        tmp_path,
        monkeypatch,
        count=len(mutants),
        min_detected=0,
        simulation={"run_work": answer, "cocotb_module": "tb.test_x" if cocotb else None},
    )
    return result, result.detail["classified"], fake


def test_auto_budget_uses_source_size_without_hdl_features(tmp_path: Path) -> None:
    source = tmp_path / "rtl/dut.sv"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"not even valid HDL\n" * 16)

    breakdown = compute_source_size_budget(["rtl/dut.sv"], tmp_path)

    assert breakdown["method"] == "language_neutral_source_size"
    assert breakdown["source_lines"] == 16
    (source.parent / "unterminated.vhd").write_bytes(b"not HDL")
    (source.parent / "empty.sv").write_bytes(b"")
    paths = ["rtl/dut.sv", "rtl/unterminated.vhd", "rtl/empty.sv"]
    preview = compute_source_size_budget(paths, tmp_path)
    assert preview["files"] == [
        {"path": "rtl/dut.sv", "source_lines": 16, "source_bytes": 304},
        {"path": "rtl/unterminated.vhd", "source_lines": 1, "source_bytes": 7},
        {"path": "rtl/empty.sv", "source_lines": 0, "source_bytes": 0},
    ]
    assert (preview["source_lines"], preview["source_bytes"]) == (17, 311)
    assert preview["readable_files"] == 3
    assert "always_blocks" not in breakdown


def test_auto_budget_fails_when_scope_file_is_unreadable(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        compute_source_size_budget(["rtl/missing.sv"], tmp_path)


def _timeout_results_line(progress: str, selected: list[str]) -> str:
    """Build the results line the cocotb run-half prints after a timeout."""
    from booley.flows.sim.backends import cocotb_results as cr

    results = cr.recover_timeout_progress(progress, selected)
    return cr.format_results_line(cr.results_payload(results))


def _simulator_killed(
    work: PreparedSimulationWork, signal_number: int, adapter_status: int = 1
) -> subprocess.CompletedProcess[str]:
    """What an adapter leaves when its simulator was killed: its authenticated result."""
    identity = work_transport_identity(work)
    if identity is not None:  # without transport the run carries no evidence of the kill
        write_adapter_result(
            identity,
            AdapterResult(
                passed=False,
                inconclusive=False,
                sva_errors=0,
                tests=identity.selected_tests,
                simulator_returncode=-signal_number,
                termination="completed",
            ),
        )
    return _fake_proc(adapter_status, "ordinary failure output")


def test_sweep_builds_one_exact_replacement_at_a_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_scope_files(tmp_path, [_SCOPE])
    source = tmp_path / _SCOPE
    pristine = source.read_text(encoding="utf-8")
    picked = _candidates(tmp_path)[:2]
    staged: list[tuple[Path, str]] = []

    def record_staged_copy(build_root: Path) -> None:
        assert source.read_text(encoding="utf-8") == pristine
        copy = next(build_root.glob("src/**/mod_a.sv"))
        staged.append((build_root, copy.read_text(encoding="utf-8")))

    def run(index: int | None) -> subprocess.CompletedProcess[str]:
        assert source.read_text(encoding="utf-8") == pristine
        return _fake_proc(rc=0)

    _, _, fake = _campaign(
        tmp_path,
        monkeypatch,
        count=2,
        min_detected=0,
        run=run,
        simulation={"verilate": record_staged_copy},
    )

    assert fake.builds == [None, 1, 2]
    (_, baseline_copy), *mutant_copies = staged
    assert baseline_copy == pristine
    assert len({root for root, _ in staged}) == 3
    for (_, text), mutant in zip(mutant_copies, picked, strict=True):
        changed = [
            (number, got, was)
            for number, (got, was) in enumerate(
                zip(text.splitlines(), pristine.splitlines(), strict=True), start=1
            )
            if got != was
        ]
        assert [number for number, _, _ in changed] == [mutant.line]
        assert mutant.mutated_code in changed[0][1]
    assert source.read_text(encoding="utf-8") == pristine


_MISSING = COCOTB_RESULTS_PREFIX + '{"state":"missing","detail":"no xml","tests":[]}'
_ZERO_MATCH = cr.format_results_line(
    cr.results_payload(
        cr.compact_results(
            cr.CocotbResults(
                state=cr.STATE_OK, tests=(cr.CocotbTest("run_test/ifg=12", "tb", "skipped"),)
            ),
            ["run_test"],
        )
    )
)


@pytest.mark.parametrize(
    ("output", "status", "reason"),
    [
        pytest.param(_MISSING, 1, "state is missing", id="state-missing"),
        pytest.param("", 1, "cocotb result line is missing or malformed", id="no-line"),
        pytest.param(
            COCOTB_RESULTS_PREFIX + "{broken",
            1,
            "cocotb result line is missing or malformed",
            id="malformed",
        ),
        pytest.param(_COCOTB_PASS, 1, "without a failing test", id="all-pass-but-failed"),
        pytest.param(_ZERO_MATCH, 1, "without a failing test", id="zero-match-compact"),
    ],
)
def test_cocotb_results_that_prove_no_failing_test_are_inconclusive_not_a_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str, status: int, reason: str
) -> None:
    _, (row,), _ = _mutant_rows(
        tmp_path, monkeypatch, [lambda _work: _fake_proc(status, output)], cocotb=True
    )

    assert row["status"] == "inconclusive"
    assert reason in row["sim_output_snippet"]
    assert row["first_killing_test"] is None


def test_sweep_classifies_every_variant_after_inconclusive_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corner_fails = COCOTB_RESULTS_PREFIX + (
        '{"state":"ok","tests":[{"name":"corner","module":"tb",'
        '"status":"fail","failure":"mismatch","elapsed_s":0.1}]}'
    )
    _, rows, _ = _mutant_rows(
        tmp_path,
        monkeypatch,
        [lambda _work: _fake_proc(1, _MISSING), lambda _work: _fake_proc(1, corner_fails)],
        cocotb=True,
    )

    assert [row["status"] for row in rows] == ["inconclusive", "detected"]
    assert rows[1]["first_killing_test"] == "corner"


def _timed_out(output: str) -> Callable[[PreparedSimulationWork], subprocess.CompletedProcess]:
    def run(work: PreparedSimulationWork) -> subprocess.CompletedProcess:
        raise subprocess.TimeoutExpired(["sim"], 5, output=output)

    return run


_RUNNING_FULL = "0.00ns INFO cocotb.regression running tb.full (1/2)\n"
_AFTER_FAILURE = (
    "0.00ns INFO cocotb.regression running tb.smoke (1/2)\n"
    "1.00ns INFO cocotb.regression tb.smoke failed\n"
    "1.00ns INFO cocotb.regression running tb.full (2/2)\n"
)


@pytest.mark.parametrize(
    ("mutant", "killing"),
    [
        pytest.param(
            lambda _work: _fake_proc(
                1,
                "ERROR: cocotb simulation timed out (5s)\n"
                + _timeout_results_line(_RUNNING_FULL, ["full", "smoke"]),
            ),
            "full",
            id="run-half-timeout",
        ),
        pytest.param(
            _timed_out(_timeout_results_line(_RUNNING_FULL, ["full", "smoke"])), "full", id="outer"
        ),
        pytest.param(
            _timed_out(_timeout_results_line(_AFTER_FAILURE, ["smoke", "full"])),
            "full",
            id="after-earlier-failure",
        ),
        pytest.param(_timed_out(""), "<cocotb-suite>", id="no-results-line"),
    ],
)
def test_cocotb_timeout_is_a_kill_that_names_the_running_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutant: Any, killing: str
) -> None:
    _, (row,), _ = _mutant_rows(tmp_path, monkeypatch, [mutant], cocotb=True)

    assert (row["status"], row["timed_out"]) == ("detected", True)
    assert row["first_killing_test"] == killing


def test_variant_that_does_not_build_is_never_launched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Mutant build that leaves no marker cannot be run: its build is not restaged pristine."""
    _prepare_scope_files(tmp_path, [_SCOPE])

    def drop_marker(build_root: Path) -> None:
        if build_root.name.startswith("mutant_"):
            for marker in build_root.glob(".booley_*"):
                marker.unlink()

    fake = _patch_fake_simulation(monkeypatch, verilate=drop_marker)
    endpoint = _make_endpoint(tmp_path, monkeypatch, scope=_SCOPE, count=1, min_detected=0)

    with pytest.raises(UnsupportedSimTargetError, match="mutation build marker missing"):
        endpoint.run_with_claims(CampaignClaims())

    assert fake.runs == [None]
    assert fake.builds == [None, 1]


class _CatalogWithClosure:
    """The fake catalog, but its ``core_closure`` names the Project's ``.core`` files."""

    def __init__(self, catalog: Any, cores: frozenset[Path]) -> None:
        self._catalog = catalog
        self._cores = cores

    def __getattr__(self, name: str) -> Any:
        return getattr(self._catalog, name)

    def core_closure(self, _handles: Any) -> frozenset[Path]:
        return self._cores


def test_generated_direct_source_rejected_before_fusesoc_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_scope_files(tmp_path, [_SCOPE])
    pristine = (tmp_path / _SCOPE).read_bytes()
    core = tmp_path / "dut.core"
    core.write_text(
        "CAPI=2:\n"
        + yaml.safe_dump(
            {
                "name": "acme:test:dut:1",
                "filesets": {"rtl": {"files": [_SCOPE, "generated/derivative.sv"]}},
                "generators": {"derivative": {"command": "generate"}},
                "targets": {"sim": {"filesets": ["rtl"], "generate": ["derivative"]}},
            }
        )
    )
    fake = _patch_fake_simulation(monkeypatch)
    prepares = _wrap_preparer(monkeypatch)
    build = TargetCatalog.build
    monkeypatch.setattr(
        TargetCatalog,
        "build",
        classmethod(lambda _cls, root: _CatalogWithClosure(build(root), frozenset([core]))),
    )
    endpoint = _make_endpoint(tmp_path, monkeypatch, scope=_SCOPE, count=1, min_detected=0)

    result = endpoint.run_with_claims(CampaignClaims())

    assert result.exit_code == EXIT_ERROR
    assert "candidate Mutant did not build and was replaced" in result.report_text
    assert fake.builds == [None]  # no Mutant build reached `make`
    # Only the pristine baseline was prepared: FuseSoC never ran generators for a Mutant.
    assert [kwargs["build_root"].name.startswith("mutant_") for _, kwargs in prepares] == [False]
    assert (tmp_path / _SCOPE).read_bytes() == pristine


def _two_mutants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first: Any, second: Any
) -> tuple[EndpointOutcome, list[dict[str, Any]]]:
    result, rows, _ = _mutant_rows(tmp_path, monkeypatch, [first, second])
    return result, rows


def _by_test(**answers: subprocess.CompletedProcess) -> Callable[..., Any]:
    """A Mutant answering each launch by the test it selects (unlisted tests pass)."""
    return lambda work: answers.get(
        _test_name(work), _fake_proc(0, "iverilog sim PASSED (rc=0)\n")
    )


def test_detected_snippet_starts_at_the_killing_tests_failure_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    header = (
        "CWD: /work/error-correction/rtl\n"
        "CMD: iverilog -g2012 -o sim /work/error-correction/rtl/dut.sv\n"
    )
    failure = "FATAL: tb/mutation_tb.sv:30: mismatch: alu lt"
    result, (killed, survived) = _two_mutants(
        tmp_path,
        monkeypatch,
        _by_test(full=_fake_proc(1, f"{header}{failure}\n$finish called at 40\n")),
        _by_test(full=_fake_proc(0, "ran full\niverilog sim PASSED (rc=0)\n")),
    )

    assert killed["status"] == "detected"
    assert killed["sim_output_snippet"].startswith(failure)
    assert "PASSED" not in killed["sim_output_snippet"]
    assert f"| {failure} " in _results_markdown(tmp_path, result)
    # A survivor keeps the tail of the joined suite output.
    assert survived["status"] == "not_detected"
    assert survived["sim_output_snippet"].endswith("iverilog sim PASSED (rc=0)")


def test_cocotb_detected_snippet_comes_from_the_detecting_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    header = "CWD: /work/error-correction\nCMD: make\nFILTER: test_bad|test_done\n"
    failed_line = (
        "    60.00ns WARNING  cocotb.regression                  mixed_cases.test_bad failed\n"
    )
    traceback = (
        "                                                        Traceback (most recent call last):\n"
        "                                                        AssertionError: deliberate failure\n"
    )
    first_results = COCOTB_RESULTS_PREFIX + (
        '{"state":"ok","detail":"","tests":['
        '{"name":"test_bad","module":"mixed_cases","status":"fail",'
        '"failure":"deliberate","elapsed_s":0.1}]}'
    )
    second_results = COCOTB_RESULTS_PREFIX + (
        '{"state":"ok","detail":"","tests":['
        '{"name":"test_done","module":"mixed_cases","status":"pass",'
        '"failure":"","elapsed_s":0.1}]}'
    )
    toml = (
        '[lite]\ntests = ["test_bad", "test_done"]\n'
        '[lite.variants.test_bad]\ndefault = "x"\nx = { env = { MODE = "A" } }\n'
        '[lite.variants.test_done]\ndefault = "y"\ny = { env = { MODE = "B" } }\n'
    )

    def batches(work: PreparedSimulationWork) -> subprocess.CompletedProcess[str]:
        if work.tests == ("test_bad",):
            return _fake_proc(1, header + failed_line + traceback + first_results)
        return _fake_proc(0, "batch two ran\n    30.00ns INFO test_done passed\n" + second_results)

    result, (row), _ = _mutant_rows(
        tmp_path, monkeypatch, [batches], cocotb=True, tests_toml=toml
    )  # fmt: skip
    (row,) = row

    assert row["first_killing_test"] == "test_bad"
    expected = "60.00ns WARNING cocotb.regression mixed_cases.test_bad failed"
    assert row["sim_output_snippet"].startswith(expected)
    assert "batch two" not in row["sim_output_snippet"]
    assert f"| test_bad | {expected[:60]} |" in _results_markdown(tmp_path, result)


def test_timeout_detection_snippet_shows_the_timeout_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def killed(work: PreparedSimulationWork) -> subprocess.CompletedProcess:
        if _test_name(work) == "full":
            raise subprocess.TimeoutExpired(["sim"], 1, output="[sim] running full\n")
        return _fake_proc(0, "iverilog sim PASSED (rc=0)\n")

    printed = _by_test(
        full=_fake_proc(
            1, "CWD: /work/error-correction\nERROR: iverilog simulation timed out (25s)\n"
        )
    )
    _, (by_printed, by_kill) = _two_mutants(tmp_path, monkeypatch, printed, killed)

    assert by_printed["timed_out"] and by_kill["timed_out"]
    assert by_printed["sim_output_snippet"].startswith(
        "ERROR: iverilog simulation timed out (25s)"
    )
    assert by_kill["sim_output_snippet"].startswith(
        "full: ran past its time limit and was stopped"
    )
    assert "[sim] running full" in by_kill["sim_output_snippet"]


def test_failing_run_outranks_a_timed_out_run_in_the_snippet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def mutant(work: PreparedSimulationWork) -> subprocess.CompletedProcess:
        if _test_name(work) == "full":
            raise subprocess.TimeoutExpired(["sim"], 1, output="[sim] running full\n")
        return _fake_proc(1, "FATAL: tb/smoke_tb.sv:7: mismatch\n")

    _, (row,), _ = _mutant_rows(tmp_path, monkeypatch, [mutant])

    assert row["status"] == "detected" and not row["timed_out"]
    assert row["sim_output_snippet"].startswith("FATAL: tb/smoke_tb.sv:7: mismatch")
    assert "ran past its time limit" not in row["sim_output_snippet"]


def test_cocotb_snippet_anchors_on_the_killing_test_not_an_earlier_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = (
        (
            "    10.00ns INFO  cocotb.regression  test_done: error flag cleared\n"
            "    60.00ns WARNING  cocotb.regression  mixed_cases.test_bad failed\n"
        )
        + COCOTB_RESULTS_PREFIX
        + (
            '{"state":"ok","detail":"","tests":['
            '{"name":"test_done","module":"mixed_cases","status":"pass","failure":"","elapsed_s":0.1},'
            '{"name":"test_bad","module":"mixed_cases","status":"fail","failure":"x","elapsed_s":0.1}]}'
        )
    )

    _, (row,), _ = _mutant_rows(
        tmp_path, monkeypatch, [lambda _work: _fake_proc(1, output)], cocotb=True
    )

    assert row["first_killing_test"] == "test_bad"
    assert row["sim_output_snippet"].startswith(
        "60.00ns WARNING cocotb.regression mixed_cases.test_bad"
    )


_ONE_TEST = '[lite]\ntests = ["full"]\nselect = "+test={name}"\n'


def _run_signal_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launch: Callable[..., Any]
) -> tuple[EndpointOutcome, dict[str, Any]]:
    result, (row,), _ = _mutant_rows(tmp_path, monkeypatch, [launch], tests_toml=_ONE_TEST)
    assert list(tmp_path.rglob("*adapter-*")) == []  # the result files are removed
    return result, row


@pytest.mark.parametrize(
    ("launch", "name"),
    [
        pytest.param(lambda _work: _fake_proc(-9), "SIGKILL", id="adapter-killed"),
        pytest.param(lambda _work: _fake_proc(-99), "99", id="adapter-unnamed-signal"),
        pytest.param(lambda work: _simulator_killed(work, 9), "SIGKILL", id="simulator-killed"),
        # The adapter reports the pass its sentinel printed (exit 0), whatever killed the simulator.
        pytest.param(
            lambda work: _simulator_killed(work, 9, adapter_status=0),
            "SIGKILL",
            id="simulator-killed-after-pass",
        ),
        # A shell reports a child's death by a signal as 128 + n; the adapter's own handler too.
        pytest.param(lambda _work: _fake_proc(137), "SIGKILL", id="exit-137"),
        pytest.param(lambda _work: _fake_proc(143), "SIGTERM", id="exit-143"),
    ],
)
def test_signal_killed_run_is_inconclusive_not_a_kill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launch: Callable[..., Any], name: str
) -> None:
    result, row = _run_signal_case(tmp_path, monkeypatch, launch)

    assert f"killed by signal {name}" in row["sim_output_snippet"]
    assert (row["status"], row["first_killing_test"]) == ("inconclusive", None)
    # The score counts neither a detection nor a survivor.
    assert (result.detail["detected"], result.detail["not_detected"]) == (0, 0)


@pytest.mark.parametrize("evidence", ["missing", "guard-killed", "self-aborted"])
def test_a_failing_run_without_signal_evidence_still_detects_the_mutant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, evidence: str
) -> None:
    def launch(work: PreparedSimulationWork) -> subprocess.CompletedProcess[str]:
        if evidence != "missing":  # a timeout is Booley's own kill; SIGABRT is `$fatal` aborting
            identity = work_transport_identity(work)
            assert identity is not None
            write_adapter_result(
                identity,
                AdapterResult(
                    passed=False,
                    inconclusive=False,
                    sva_errors=0,
                    tests=identity.selected_tests,
                    simulator_returncode=-9 if evidence == "guard-killed" else -6,
                    termination="timeout" if evidence == "guard-killed" else "completed",
                ),
            )
        return _fake_proc(1, "test failed")

    result, row = _run_signal_case(tmp_path, monkeypatch, launch)

    assert row["status"] == "detected"
    assert result.detail["detected"] == 1


def test_a_timed_out_run_leaves_no_adapter_result_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def launch(work: PreparedSimulationWork) -> subprocess.CompletedProcess[str]:
        _simulator_killed(work, 9)  # leaves the adapter's result behind
        raise subprocess.TimeoutExpired(["sim"], 1)

    _, row = _run_signal_case(tmp_path, monkeypatch, launch)

    assert row["timed_out"] is True


def test_baseline_build_failure_names_project_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A baseline that does not build names each source by Project path (issue #4999)."""
    pytest.importorskip("fusesoc")
    pytest.importorskip("edalize")
    _write_tests_toml(tmp_path, '[sim]\ntests = ["t"]\n')
    _prepare_scope_files(tmp_path, [_SCOPE])
    (tmp_path / "dut.core").write_bytes(
        b"CAPI=2:\n"
        b"name: acme:lib:dut:1\n"
        b"filesets:\n"
        b"  rtl:\n"
        b"    files:\n"
        b"      - rtl/mod_a.sv: {file_type: systemVerilogSource}\n"
        b"targets:\n"
        b"  sim:\n"
        b"    flow: sim\n"
        b"    flow_options: {tool: icarus}\n"
        b"    filesets: [rtl]\n"
        b"    toplevel: dummy\n"
    )
    raw = "src/acme_lib_dut_1/rtl/mod_a.sv:4: syntax error"
    unmapped = "gen/other.sv:2: error: Unknown module type: other"
    put_failing_iverilog_on_path(tmp_path, monkeypatch, [raw, unmapped])
    endpoint = _make_endpoint(
        tmp_path, monkeypatch, target="sim", scope=_SCOPE, count=1, min_detected=0
    )

    with _real_fusesoc():
        result = endpoint.run_with_claims(CampaignClaims())

    assert result.exit_code == EXIT_ERROR
    for text in (result.detail["log_tail"], result.report_text):
        lines = text.splitlines()
        assert "rtl/mod_a.sv:4: syntax error" in lines
        assert unmapped in lines
        assert "src/acme_lib_dut_1/" not in text
    log = tmp_path / result.detail["artifacts"]["baseline_build_log"]
    assert raw in log.read_text(encoding="utf-8")
