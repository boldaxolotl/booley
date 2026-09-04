"""Authenticated transport shared by Simulation adapters."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from booley.flows.sim.adapter_transport import (
    AdapterResult,
    AdapterTestResult,
    AdapterTransportError,
    AdapterTransportIdentity,
    read_adapter_result,
    write_adapter_result,
)
from booley.flows.sim.backends import cocotb, icarus, verilator
from booley.flows.sim.backends.cocotb_results import (
    format_results_line,
    recover_timeout_progress,
)


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
    )

    write_adapter_result(identity, result)

    assert read_adapter_result(identity) == result


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
    recovered = recover_timeout_progress(progress, list(identity.selected_tests))

    cocotb._publish_adapter_result(
        identity,
        f"{progress}\n{format_results_line(recovered)}\n",
        False,
    )

    result = read_adapter_result(identity)
    assert result.failure_kind == "timeout"
    assert [(item.name, item.verdict) for item in result.test_results] == [
        ("done", "pass"),
        ("active", "timeout"),
        ("later", "inconclusive"),
    ]
