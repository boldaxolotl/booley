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

    adapter._publish_adapter_result(
        identity,
        "[SIM_RESULT] PASSED\n",
        0,
        trace_required=True,
    )

    result = read_adapter_result(identity)
    assert result.passed is False
    assert result.inconclusive is True
    assert result.failure_kind == "artifact"


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
