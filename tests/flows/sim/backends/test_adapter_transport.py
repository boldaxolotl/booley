"""Authenticated transport shared by Simulation adapters."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from booley.flows.sim.adapter_transport import (
    ADAPTER_RESULT_SCHEMA,
    AdapterResult,
    AdapterTestResult,
    AdapterTraceResult,
    AdapterTransportError,
    AdapterTransportIdentity,
    partial_result_identity,
    read_adapter_result,
    write_adapter_result,
)
from booley.flows.sim.backends import cocotb, icarus, verilator
from booley.flows.sim.backends.cocotb_results import (
    STATE_OK,
    CocotbResults,
    CocotbTest,
    format_results_line,
)
from booley.flows.sim.backends.shared import RunTermination


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


def test_adapter_result_rejects_identity_mismatch(tmp_path) -> None:
    identity = _identity(tmp_path)
    write_adapter_result(
        identity,
        AdapterResult(passed=True, inconclusive=False, sva_errors=0, tests=("reset",)),
    )

    with pytest.raises(AdapterTransportError, match="attempt token"):
        read_adapter_result(replace(identity, attempt_token="different"))


def test_adapter_result_rejects_unknown_schema(tmp_path) -> None:
    identity = _identity(tmp_path)
    identity.result_path.write_text(
        json.dumps(
            {
                "schema": 999,
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

    with pytest.raises(AdapterTransportError, match="schema"):
        read_adapter_result(identity)


def test_adapter_result_rejects_old_format_in_current_schema(tmp_path) -> None:
    identity = _identity(tmp_path)
    identity.result_path.write_text(
        json.dumps(
            {
                "schema": ADAPTER_RESULT_SCHEMA,
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

    with pytest.raises(AdapterTransportError, match="simulator_returncode"):
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


def test_adapter_result_rejects_unselected_test(tmp_path) -> None:
    identity = _identity(tmp_path)
    write_adapter_result(
        identity,
        AdapterResult(passed=True, inconclusive=False, sva_errors=0, tests=("other",)),
    )

    with pytest.raises(AdapterTransportError, match="selected tests"):
        read_adapter_result(identity)


def test_adapter_result_requires_per_test_verdicts(tmp_path) -> None:
    identity = _identity(tmp_path)
    write_adapter_result(
        identity,
        AdapterResult(passed=True, inconclusive=False, sva_errors=0, tests=("reset",)),
    )

    with pytest.raises(AdapterTransportError, match="omits required per-test"):
        read_adapter_result(identity)


def test_adapter_pass_cannot_contradict_per_test_failure(tmp_path) -> None:
    identity = _identity(tmp_path)
    write_adapter_result(
        identity,
        AdapterResult(
            passed=True,
            inconclusive=False,
            sva_errors=0,
            tests=("reset",),
            test_results=(AdapterTestResult("reset", "fail"),),
        ),
    )

    with pytest.raises(AdapterTransportError, match="contradicts a per-test"):
        read_adapter_result(identity)


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
    icarus._publish_adapter_result(identity, "", 0, termination=termination)
    payload = json.loads(identity.result_path.read_text(encoding="utf-8"))
    payload["inconclusive"] = True
    identity.result_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(AdapterTransportError, match="contradicts its termination"):
        read_adapter_result(identity)


def test_aborted_result_rejects_mismatched_per_test_termination(tmp_path) -> None:
    identity = _identity(tmp_path)
    termination = RunTermination("disk_budget", "disk full", "infrastructure")
    icarus._publish_adapter_result(identity, "", 0, termination=termination)
    payload = json.loads(identity.result_path.read_text(encoding="utf-8"))
    payload["test_results"][0].update(termination="fatal_init", failure_kind="missing_input")
    identity.result_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(AdapterTransportError, match="aggregate termination"):
        read_adapter_result(identity)


def test_native_timeout_without_selected_tests_is_timeout_not_design(tmp_path) -> None:
    identity = replace(_identity(tmp_path), selected_tests=())

    icarus._publish_adapter_result(
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


@pytest.mark.parametrize("adapter", [verilator, icarus])
def test_native_adapter_publishes_authenticated_terminal_evidence(tmp_path, adapter) -> None:
    identity = _identity(tmp_path)

    adapter._publish_adapter_result(identity, "[SIM_RESULT] PASSED\n", 0)

    assert read_adapter_result(identity) == AdapterResult(
        passed=True,
        inconclusive=False,
        sva_errors=0,
        tests=("reset",),
        test_results=(AdapterTestResult("reset", "pass"),),
    )


@pytest.mark.parametrize("adapter", [verilator, icarus])
def test_trace_request_requires_positive_waveform_evidence(tmp_path, adapter) -> None:
    identity = _identity(tmp_path)

    reason = (
        "trace requested but no fresh .fst store or convertible .vcd was produced "
        "(a testbench with its own C++ main() writes its dump under a name "
        "Booley cannot guess - declare it in [flows.sim].trace_files)"
    )
    adapter._publish_adapter_result(
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
    verilator._publish_adapter_result(
        identity, "[SIM_RESULT] FAILED\n", 1, trace_required=True, trace=trace
    )
    assert read_adapter_result(identity).test_results[0].verdict == "fail"

    timed_out = RunTermination(kind="timeout", detail="timed out", failure_kind="timeout")
    verilator._publish_adapter_result(
        identity, "", 0, trace_required=True, trace=trace, termination=timed_out
    )
    assert read_adapter_result(identity).test_results[0].verdict == "timeout"


def test_validator_rejects_passing_test_beside_trace_failure(tmp_path) -> None:
    identity = _identity(tmp_path)
    write_adapter_result(
        identity,
        AdapterResult(
            passed=False,
            inconclusive=True,
            sva_errors=0,
            tests=("reset",),
            failure_kind="artifact",
            detail="waveform missing",
            test_results=(AdapterTestResult("reset", "pass"),),
        ),
    )

    with pytest.raises(AdapterTransportError, match="trace failure contradicts a passing test"):
        read_adapter_result(identity)


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
    output = (
        format_results_line(
            CocotbResults(
                state=STATE_OK,
                tests=(CocotbTest("reset", "test_demo", test_status),),
            )
        )
        + extra_output
    )
    cocotb._publish_adapter_result(
        identity,
        output,
        False,
        failure_kind=failure_kind,
        trace=AdapterTraceResult("incident", detail="waveform missing"),
    )

    result = read_adapter_result(identity)
    assert result.test_results[0].verdict == expected_verdict
    assert result.failure_kind == expected_kind


def test_cocotb_abort_after_all_tests_completed_cannot_pass_batch(tmp_path) -> None:
    identity = replace(_identity(tmp_path), adapter="cocotb", selected_tests=("first", "second"))
    output = format_results_line(
        CocotbResults(
            state=STATE_OK,
            tests=(
                CocotbTest("first", "test_demo", "pass"),
                CocotbTest("second", "test_demo", "pass"),
            ),
        )
    )

    cocotb._publish_adapter_result(
        identity,
        output,
        True,
        termination=RunTermination("disk_budget", "disk full", "infrastructure"),
        simulator_returncode=0,
    )

    result = read_adapter_result(identity)
    assert result.passed is False
    assert result.inconclusive is False
    assert result.termination == "disk_budget"
    assert result.failure_kind == "infrastructure"
    assert [test.verdict for test in result.test_results] == ["pass", "pass"]


def test_cocotb_transport_preserves_partial_timeout_progress(tmp_path) -> None:
    identity = replace(
        _identity(tmp_path),
        adapter="cocotb",
        selected_tests=("done", "active", "later"),
    )
    progress = """\
0.00ns INFO cocotb.regression running done (1/3)
1.00ns INFO cocotb.regression done passed
1.00ns INFO cocotb.regression running active (2/3)
"""
    publish = cocotb._partial_result_publisher(
        identity, list(identity.selected_tests), tmp_path / "results.xml"
    )
    assert publish is not None
    for line in progress.splitlines(keepends=True):
        publish(line)

    result = read_adapter_result(partial_result_identity(identity))
    assert result.failure_kind == "timeout"
    assert [(item.name, item.verdict) for item in result.test_results] == [
        ("done", "pass"),
        ("active", "timeout"),
        ("later", "timeout"),
    ]


def test_default_cocotb_partial_transport_discovers_current_attempt_names(tmp_path) -> None:
    identity = replace(_identity(tmp_path), adapter="cocotb", selected_tests=())
    results = tmp_path / "results.xml"
    results.write_text(
        "<testsuite><testcase name='done'/><testcase name='active'>"
        "<failure message='interrupted'/></testcase>"
        "<testcase name='later'><skipped/></testcase></testsuite>",
        encoding="utf-8",
    )
    publish = cocotb._partial_result_publisher(identity, [], results)
    assert publish is not None

    publish("0.00ns INFO cocotb.regression running done (1/3)\n")
    publish("1.00ns INFO cocotb.regression done passed\n")
    publish("1.00ns INFO cocotb.regression running active (2/3)\n")

    result = read_adapter_result(partial_result_identity(identity))
    assert result.tests == ("done", "active", "later")
    assert [test.verdict for test in result.test_results] == [
        "pass",
        "timeout",
        "timeout",
    ]


@pytest.mark.parametrize("terminal", [False, True])
def test_attempt_preserves_terminal_evidence_and_removes_own_partial(tmp_path, terminal) -> None:
    from booley.flows.base import SubprocessResult
    from booley.flows.sim.execution.attempt import AdapterAttemptRequest, execute_adapter_attempt

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
    from booley.flows.base import SubprocessResult
    from booley.flows.sim.execution.attempt import AdapterAttemptRequest, execute_adapter_attempt

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
    from booley.flows.base import SubprocessResult
    from booley.flows.sim.execution.attempt import AdapterAttemptRequest, execute_adapter_attempt

    identity = _identity(tmp_path)
    outcome = execute_adapter_attempt(
        lambda command, timeout: SubprocessResult(returncode=-9, timed_out=timed_out),
        AdapterAttemptRequest(("adapter",), 1, identity, tmp_path),
    )
    assert outcome.result is None
    assert outcome.error_kind == "runtime"


def test_attempt_cleanup_error_keeps_authenticated_result(tmp_path, monkeypatch) -> None:
    from pathlib import Path

    from booley.flows.base import SubprocessResult
    from booley.flows.sim.execution.attempt import AdapterAttemptRequest, execute_adapter_attempt

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
    from booley.flows.base import SubprocessResult
    from booley.flows.sim.execution.attempt import AdapterAttemptRequest, execute_adapter_attempt

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
    from pathlib import Path

    from booley.flows.base import SubprocessResult
    from booley.flows.sim.execution.attempt import AdapterAttemptRequest, execute_adapter_attempt

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
