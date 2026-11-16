"""Authenticated transport shared by Simulation adapters."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from booley.flows.base import SubprocessResult
from booley.flows.sim.adapter_transport import (
    ADAPTER_RESULT_SCHEMA,
    AdapterResult,
    AdapterTestResult,
    AdapterTraceResult,
    AdapterTransportError,
    AdapterTransportIdentity,
    assess_native_verdict,
    evaluate_native_verdict,
    native_verdict_message,
    partial_result_identity,
    publish_native_adapter_result,
    read_adapter_result,
    write_adapter_result,
)
from booley.flows.sim.backends import cocotb
from booley.flows.sim.backends.cocotb_results import (
    STATE_OK,
    CocotbResults,
    CocotbTest,
)
from booley.flows.sim.backends.shared import RunTermination
from booley.flows.sim.execution.attempt import AdapterAttemptRequest, execute_adapter_attempt


def _identity(tmp_path) -> AdapterTransportIdentity:
    return AdapterTransportIdentity(
        adapter="verilator",
        attempt_token="abc123",
        target_identity="acme:lib:core:1#sim",
        selected_tests=("reset",),
        result_path=tmp_path / "adapter-result.json",
    )


def test_adapter_result_round_trips_with_expected_identity(tmp_path) -> None:
    identity = _identity(tmp_path)
    result = AdapterResult(
        passed=True,
        inconclusive=False,
        sva_errors=0,
        tests=("reset",),
        test_results=(AdapterTestResult("reset", "pass"),),
        trace=AdapterTraceResult(
            "ok",
            path="build/wave.fst",
            top_scope="tb",
            signal_count=2,
            total_ticks=3,
        ),
    )

    write_adapter_result(identity, result)

    assert read_adapter_result(identity) == result


@pytest.mark.parametrize("returncode", [-15, 0, 7])
def test_adapter_result_round_trips_actual_simulator_returncode(tmp_path, returncode) -> None:
    identity = _identity(tmp_path)
    result = AdapterResult(
        passed=False,
        inconclusive=False,
        sva_errors=0,
        tests=("reset",),
        simulator_returncode=returncode,
        failure_kind="design",
        test_results=(AdapterTestResult("reset", "fail", failure_kind="design"),),
    )

    write_adapter_result(identity, result)

    assert read_adapter_result(identity).simulator_returncode == returncode


@pytest.mark.parametrize(
    ("schema", "message"),
    [
        (999, "schema"),
        (ADAPTER_RESULT_SCHEMA, "simulator_returncode"),
    ],  # old format, current schema
)
def test_adapter_result_rejects_unknown_schema_and_old_format(tmp_path, schema, message) -> None:
    identity = _identity(tmp_path)
    identity.result_path.write_text(
        json.dumps(
            {
                "schema": schema,
                "adapter": identity.adapter,
                "attempt_token": identity.attempt_token,
                "target_identity": identity.target_identity,
                "selected_tests": list(identity.selected_tests),
                "passed": True,
                "inconclusive": False,
                "sva_errors": 0,
                "tests": ["reset"],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AdapterTransportError, match=message):
        read_adapter_result(identity)


def _mutate_valid_result(tmp_path, mutate) -> AdapterTransportIdentity:
    identity = _identity(tmp_path)
    write_adapter_result(
        identity,
        AdapterResult(
            passed=False,
            inconclusive=False,
            sva_errors=0,
            tests=("reset",),
            failure_kind="design",
            test_results=(AdapterTestResult("reset", "fail", failure_kind="design"),),
        ),
    )
    payload = json.loads(identity.result_path.read_text(encoding="utf-8"))
    mutate(payload)
    identity.result_path.write_text(json.dumps(payload), encoding="utf-8")
    return identity


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda payload: payload.update(schema="1"), "schema"),
        (
            lambda payload: payload.update(
                termination="timeout",
                failure_kind="timeout",
                detail="deadline",
                missing_input_path="memory.hex",
            ),
            "only fatal_init",
        ),
        (lambda payload: payload.update(missing_input_path="memory.hex"), "completed"),
        (lambda payload: payload.update(termination="unknown"), "termination is invalid"),
        (
            lambda payload: payload["test_results"][0].update(failure_kind="infrastructure"),
            "failure kind contradicts",
        ),
        (
            lambda payload: payload["test_results"][0].update(
                termination="timeout", failure_kind="timeout"
            ),
            "test result contradicts",
        ),
        (
            lambda payload: payload["test_results"][0].update(
                verdict="timeout",
                termination="timeout",
                failure_kind="timeout",
                detail="deadline",
            ),
            "completed adapter result",
        ),
    ],
)
def test_adapter_result_rejects_contradictory_termination_fields(
    tmp_path, mutate, message
) -> None:
    identity = _mutate_valid_result(tmp_path, mutate)

    with pytest.raises(AdapterTransportError, match=message):
        read_adapter_result(identity)


@pytest.mark.parametrize(
    ("written", "read_override", "message"),
    [
        ({"tests": ("reset",)}, {"attempt_token": "different"}, "attempt token"),
        ({"tests": ("other",)}, {}, "selected tests"),
        ({"tests": ("reset",)}, {}, "omits required per-test"),
        (
            {"tests": ("reset",), "test_results": (AdapterTestResult("reset", "fail"),)},
            {},
            "contradicts a per-test",
        ),
        (  # #884: a trace failure never sits beside a passing test
            {
                "passed": False,
                "inconclusive": True,
                "failure_kind": "artifact",
                "detail": "waveform missing",
                "tests": ("reset",),
                "test_results": (AdapterTestResult("reset", "pass"),),
            },
            {},
            "trace failure contradicts a passing test",
        ),
    ],
    ids=["identity", "unselected", "no-verdicts", "pass-beside-fail", "pass-beside-trace-failure"],
)
def test_adapter_result_rejects_inconsistent_evidence(
    tmp_path, written, read_override, message
) -> None:
    identity = _identity(tmp_path)
    fields = {"passed": True, "inconclusive": False, "sva_errors": 0, **written}
    write_adapter_result(identity, AdapterResult(**fields))

    with pytest.raises(AdapterTransportError, match=message):
        read_adapter_result(replace(identity, **read_override))


@pytest.mark.parametrize("failure_kind", ["infrastructure", "timeout", "missing_input"])
def test_completed_result_rejects_abort_only_failure_kinds(tmp_path, failure_kind) -> None:
    identity = _identity(tmp_path)
    write_adapter_result(
        identity,
        AdapterResult(
            passed=False,
            inconclusive=False,
            sva_errors=0,
            tests=("reset",),
            failure_kind=failure_kind,
            test_results=(AdapterTestResult("reset", "fail", failure_kind=failure_kind),),
        ),
    )

    with pytest.raises(AdapterTransportError, match="failure kind contradicts"):
        read_adapter_result(identity)


def test_aborted_result_rejects_inconclusive_aggregate(tmp_path) -> None:
    identity = _identity(tmp_path)
    termination = RunTermination("disk_budget", "disk full", "infrastructure")
    publish_native_adapter_result(identity, "", 0, termination=termination)
    payload = json.loads(identity.result_path.read_text(encoding="utf-8"))
    payload["inconclusive"] = True
    identity.result_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(AdapterTransportError, match="contradicts its termination"):
        read_adapter_result(identity)


def test_aborted_result_rejects_mismatched_per_test_termination(tmp_path) -> None:
    identity = _identity(tmp_path)
    termination = RunTermination("disk_budget", "disk full", "infrastructure")
    publish_native_adapter_result(identity, "", 0, termination=termination)
    payload = json.loads(identity.result_path.read_text(encoding="utf-8"))
    payload["test_results"][0].update(termination="fatal_init", failure_kind="missing_input")
    identity.result_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(AdapterTransportError, match="aggregate termination"):
        read_adapter_result(identity)


def test_native_timeout_without_selected_tests_is_timeout_not_design(tmp_path) -> None:
    identity = replace(_identity(tmp_path), selected_tests=())

    publish_native_adapter_result(
        identity,
        "",
        -15,
        termination=RunTermination("timeout", "deadline expired", "timeout"),
    )

    result = read_adapter_result(identity)
    assert result.tests == ()
    assert result.termination == "timeout"
    assert result.failure_kind == "timeout"
    assert result.inconclusive is False
    assert result.simulator_returncode == -15


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"adapter": "other"}, "adapter identity"),
        ({"target_identity": "other"}, "Target identity"),
        ({"selected_tests": ["other"]}, "selected tests"),
        ({"selected_tests": "reset"}, "string list"),
        ({"failure_kind": "mystery"}, "failure_kind"),
        ({"detail": 3}, "detail"),
        ({"failure_kind": "design"}, "failure kind"),
        ({"diagnostics": "bad"}, "string list"),
        ({"trace": {"status": "bad"}}, "trace status"),
        ({"trace": {"status": "ok", "path": 3}}, "text fields"),
        ({"trace": {"status": "ok", "path": "wave.fst", "signal_count": -1}}, "non-negative"),
        ({"trace": {"status": "ok"}}, "requires a path"),
        ({"trace": {"status": "incident"}}, "requires detail"),
        ({"trace": {"status": "ok", "path": "wave.fst", "signal_count": "bad"}}, "signal_count"),
    ],
)
def test_adapter_result_rejects_invalid_authenticated_fields(
    tmp_path,
    override,
    message,
) -> None:
    identity = _identity(tmp_path)
    write_adapter_result(
        identity,
        AdapterResult(
            passed=True,
            inconclusive=False,
            sva_errors=0,
            tests=("reset",),
            test_results=(AdapterTestResult("reset", "pass"),),
            diagnostics=("note",),
            trace=AdapterTraceResult("ok", path="wave.fst"),
        ),
    )
    payload = json.loads(identity.result_path.read_text(encoding="utf-8"))
    payload.update(override)
    identity.result_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(AdapterTransportError, match=message):
        read_adapter_result(identity)


def test_native_adapter_publishes_authenticated_terminal_evidence(tmp_path) -> None:
    identity = _identity(tmp_path)

    publish_native_adapter_result(identity, "[SIM_RESULT] PASSED\n", 0)

    assert read_adapter_result(identity) == AdapterResult(
        passed=True,
        inconclusive=False,
        sva_errors=0,
        tests=("reset",),
        test_results=(AdapterTestResult("reset", "pass"),),
    )


def test_native_run_exit_code_is_the_sentinel_verdict(tmp_path) -> None:
    identity = _identity(tmp_path)
    cases = [
        ("[SIM_RESULT] PASSED\n", 0, 0),
        ("[SIM_RESULT] FAILED\n", 0, 1),
        ("no sentinel\n", 0, 1),
        # The sentinel verdict alone decides, as the output re-read it did before.
        ("[SIM_RESULT] PASSED\nERROR: assertion\n", 0, 0),
        ("[SIM_RESULT] PASSED\n", 3, 0),
    ]
    for transport in (None, identity):
        for output, returncode, exit_code in cases:
            run = publish_native_adapter_result(transport, output, returncode)

            assert (run.output, run.exit_code) == (output, exit_code)
        assert identity.result_path.exists() is (transport is not None)


def test_trace_request_requires_positive_waveform_evidence(tmp_path) -> None:
    identity = _identity(tmp_path)

    reason = (
        "trace requested but no fresh .fst store or convertible .vcd was produced "
        "(a testbench with its own C++ main() writes its dump under a name "
        "Booley cannot guess - declare it in [flows.sim].trace_files)"
    )
    publish_native_adapter_result(
        identity,
        "[SIM_RESULT] PASSED\n",
        0,
        trace_required=True,
        trace=AdapterTraceResult("incident", path="incident.txt", detail=reason),
    )

    result = read_adapter_result(identity)
    assert result.passed is False
    assert result.inconclusive is True
    assert result.failure_kind == "artifact"
    # #884: a passing simulation without its waveform is inconclusive per test,
    # carrying the remedy, never a passing test inside a reasonless failed Target.
    assert result.test_results[0].verdict == "inconclusive"
    assert "[flows.sim].trace_files" in result.test_results[0].detail


def test_native_trace_missing_keeps_functional_failure_and_timeout(tmp_path) -> None:
    trace = AdapterTraceResult("incident", detail="waveform missing")
    identity = _identity(tmp_path)
    publish_native_adapter_result(
        identity, "[SIM_RESULT] FAILED\n", 1, trace_required=True, trace=trace
    )
    assert read_adapter_result(identity).test_results[0].verdict == "fail"

    timed_out = RunTermination(kind="timeout", detail="timed out", failure_kind="timeout")
    publish_native_adapter_result(
        identity, "", 0, trace_required=True, trace=trace, termination=timed_out
    )
    assert read_adapter_result(identity).test_results[0].verdict == "timeout"


@pytest.mark.parametrize(
    "test_status, extra_output, failure_kind, expected_verdict, expected_kind",
    [
        ("pass", "", "", "inconclusive", "artifact"),
        ("fail", "", "", "fail", "design"),
        ("pass", "\n$error assertion", "", "fail", "design"),
        ("pass", "", "design", "fail", "design"),
    ],
)
def test_cocotb_trace_incident_preserves_functional_verdicts(
    tmp_path,
    test_status,
    extra_output,
    failure_kind,
    expected_verdict,
    expected_kind,
) -> None:
    identity = replace(_identity(tmp_path), adapter="cocotb")
    results = CocotbResults(
        state=STATE_OK,
        tests=(CocotbTest("reset", "test_demo", test_status),),
    )
    cocotb._publish_adapter_result(
        identity,
        extra_output,
        False,
        results=results,
        failure_kind=failure_kind,
        trace=AdapterTraceResult("incident", detail="waveform missing"),
    )

    result = read_adapter_result(identity)
    assert result.test_results[0].verdict == expected_verdict
    assert result.failure_kind == expected_kind


def test_cocotb_abort_after_all_tests_completed_cannot_pass_batch(tmp_path) -> None:
    # Private seam: the full run's verdict (`cocotb.py::_assess_verdict`) already makes an
    # aborted batch fail, so only a direct call can hand the publisher `passed=True` beside
    # an abort and prove its own `not termination.aborted` guard (`_cocotb_adapter_result`).
    identity = replace(_identity(tmp_path), adapter="cocotb", selected_tests=("first", "second"))
    results = CocotbResults(
        state=STATE_OK,
        tests=(
            CocotbTest("first", "test_demo", "pass"),
            CocotbTest("second", "test_demo", "pass"),
        ),
    )

    cocotb._publish_adapter_result(
        identity,
        "",
        True,
        results=results,
        termination=RunTermination("disk_budget", "disk full", "infrastructure"),
        simulator_returncode=0,
    )

    result = read_adapter_result(identity)
    assert result.passed is False
    assert result.inconclusive is False
    assert result.termination == "disk_budget"
    assert result.failure_kind == "infrastructure"
    assert [test.verdict for test in result.test_results] == ["pass", "pass"]


@pytest.mark.parametrize("terminal", [False, True])
def test_attempt_preserves_terminal_evidence_and_removes_own_partial(tmp_path, terminal) -> None:
    identity = _identity(tmp_path)
    partial = partial_result_identity(identity)
    unrelated = tmp_path / "other.json.partial"
    unrelated.write_bytes(b"other attempt")
    result = AdapterResult(
        True, False, 0, ("reset",), test_results=(AdapterTestResult("reset", "pass"),)
    )

    def invoke(command, *, timeout):
        write_adapter_result(partial, result)
        if terminal:
            write_adapter_result(identity, result)
        return SubprocessResult(returncode=0, timed_out=not terminal)

    outcome = execute_adapter_attempt(
        invoke, AdapterAttemptRequest(("adapter",), 1, identity, tmp_path)
    )
    assert outcome.result == result
    assert read_adapter_result(identity) == result
    assert not partial.result_path.exists()
    assert unrelated.read_bytes() == b"other attempt"


def test_attempt_classifies_malformed_transport_as_authentication(tmp_path) -> None:
    identity = _identity(tmp_path)
    partial = partial_result_identity(identity)

    def invoke(command, *, timeout):
        identity.result_path.write_text("{}")
        partial.result_path.write_text("{}")
        return SubprocessResult(returncode=0)

    outcome = execute_adapter_attempt(
        invoke, AdapterAttemptRequest(("adapter",), 1, identity, tmp_path)
    )
    assert outcome.error_kind == "authentication"
    assert outcome.error
    assert not partial.result_path.exists()


@pytest.mark.parametrize("timed_out", [False, True])
def test_attempt_without_evidence_is_runtime_failure(tmp_path, timed_out) -> None:
    identity = _identity(tmp_path)
    outcome = execute_adapter_attempt(
        lambda command, timeout: SubprocessResult(returncode=-9, timed_out=timed_out),
        AdapterAttemptRequest(("adapter",), 1, identity, tmp_path),
    )
    assert outcome.result is None
    assert outcome.error_kind == "runtime"


def test_attempt_cleanup_error_keeps_authenticated_result(tmp_path, monkeypatch) -> None:
    identity = _identity(tmp_path)
    partial = partial_result_identity(identity)
    result = AdapterResult(
        True, False, 0, ("reset",), test_results=(AdapterTestResult("reset", "pass"),)
    )
    unlink = Path.unlink

    def failing_unlink(path, *args, **kwargs):
        if path == partial.result_path:
            raise OSError("cleanup denied")
        return unlink(path, *args, **kwargs)

    def invoke(command, *, timeout):
        write_adapter_result(identity, result)
        write_adapter_result(partial, result)
        return SubprocessResult(returncode=0)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    outcome = execute_adapter_attempt(
        invoke, AdapterAttemptRequest(("adapter",), 1, identity, tmp_path)
    )
    assert outcome.result == result
    assert outcome.error_kind == "cleanup"
    assert "cleanup denied" in outcome.cleanup_error
    assert read_adapter_result(identity) == result


@pytest.mark.parametrize("defect", ["token", "target", "stale"])
def test_attempt_rejects_unauthenticated_evidence(tmp_path, defect) -> None:
    identity = _identity(tmp_path)
    result = AdapterResult(
        True, False, 0, ("reset",), test_results=(AdapterTestResult("reset", "pass"),)
    )
    if defect == "stale":
        write_adapter_result(identity, result)

    def invoke(command, *, timeout):
        if defect == "token":
            write_adapter_result(replace(identity, attempt_token="foreign"), result)
        elif defect == "target":
            write_adapter_result(replace(identity, target_identity="foreign#sim"), result)
        return SubprocessResult(returncode=0)

    outcome = execute_adapter_attempt(
        invoke, AdapterAttemptRequest(("adapter",), 1, identity, tmp_path)
    )
    assert outcome.result is None
    assert outcome.error_kind == "authentication"
    assert outcome.error


def test_attempt_failed_promotion_preserves_only_durable_partial(tmp_path, monkeypatch) -> None:
    identity = _identity(tmp_path)
    partial = partial_result_identity(identity)
    result = AdapterResult(
        True, False, 0, ("reset",), test_results=(AdapterTestResult("reset", "pass"),)
    )
    replace_path = Path.replace

    def failing_replace(path, target):
        if path == partial.result_path:
            raise OSError("promotion denied")
        return replace_path(path, target)

    def invoke(command, *, timeout):
        write_adapter_result(partial, result)
        return SubprocessResult(returncode=-9, timed_out=True)

    monkeypatch.setattr(Path, "replace", failing_replace)
    outcome = execute_adapter_attempt(
        invoke, AdapterAttemptRequest(("adapter",), 1, identity, tmp_path)
    )
    assert outcome.result == result
    assert outcome.error_kind == "cleanup"
    assert read_adapter_result(partial) == result
    assert not identity.result_path.exists()


@pytest.mark.parametrize(
    ("count", "label"),
    [
        (1, "1 assertion/fatal error message"),
        (2, "2 assertion/fatal error messages"),
    ],
)
def test_native_fatal_only_failure_names_assertion_fatal_messages(count: int, label: str):
    """A testbench ``$fatal`` failure is not an SVA failure and the text must not say so (#2807)."""
    output = "".join(
        f"[{100 + i}] %Fatal: tb.sv:12: Dhrystone trapped before validated success marker\n"
        for i in range(count)
    )

    assessment = assess_native_verdict(output, 1)

    # The grade and machine-readable count are unchanged: still a design failure.
    assert assessment.sva_errors == count
    assert assessment.passed is False
    assert assessment.inconclusive is False
    message = native_verdict_message("Verilator", assessment, 1, RunTermination())
    assert message == f"\nVerilator sim FAILED ({label})"


# --- evaluate_native_verdict: the shared Verilator/iverilog verdict printer ----------------

_SENTINELS = {"pass_sentinels": ["ALL TESTS PASSED."], "fail_sentinels": ["ERROR!"]}


@pytest.mark.parametrize(
    ("tool", "output", "returncode", "sentinels", "expected"),
    [
        ("Verilator", "ALL TESTS PASSED.", 0, _SENTINELS, "PASSED (rc=0)"),
        ("iverilog", "ERROR!", 0, _SENTINELS, "FAILED (fail sentinel matched)"),
        ("Verilator", "[SIM_RESULT] FAILED", 1, {}, "FAILED (rc=1)"),
        ("iverilog", "no sentinel here", 0, {}, "INCONCLUSIVE (rc=0, no sentinel)"),
    ],
)
def test_native_verdict_message_names_the_tool(
    tmp_path, capsys, tool: str, output: str, returncode: int, sentinels: dict, expected: str
) -> None:
    evaluate_native_verdict(tool, output, returncode, tmp_path, **sentinels)

    assert f"\n{tool} sim {expected}" in capsys.readouterr().out
    assert (tmp_path / "run.log").read_text(
        encoding="utf-8"
    ) == output  # persisted on pass and fail


@pytest.mark.parametrize(
    ("line", "sentinels"),
    [
        ("[0]ERROR! list crc 0x1234", {"fail_sentinels": ["ERROR!"]}),
        ("Error at cycle 9", {}),
        ("Error :( expected 1", {}),
    ],
)
def test_native_verdict_captures_the_first_error(tmp_path, line: str, sentinels: dict) -> None:
    evaluate_native_verdict("iverilog", f"{line}\n[SIM_RESULT] FAILED\n", 1, tmp_path, **sentinels)

    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["first_error"] == line
