from __future__ import annotations

from pathlib import Path

import pytest

from booley.flows.endpoint_admission import (
    AdmissionContext,
    AdmissionGate,
    _admission_cancellation,
)
from booley.flows.sim.campaign.model import _freeze_json
from booley.flows.sim.config import parse_run_cwd_template
from booley.flows.sim.mode import SimulationMode
from booley.flows.sim.request import SimRequest


def _context(**overrides):
    values = {
        "mode": "unmanaged",
        "slot_store": None,
        "outer_token": None,
        "max_heavy": 1,
        "role": "interactive",
        "execution_id": "e" * 32,
        "timeout_seconds": None,
        "cancellation": lambda: False,
    }
    values.update(overrides)
    return AdmissionContext(**values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"mode": "invalid"},
        {"max_heavy": 0},
        {"mode": "managed"},
        {"max_heavy": 2},
    ],
)
def test_admission_context_rejects_incoherent_claims(overrides) -> None:
    with pytest.raises(ValueError):
        _context(**overrides)


def test_admission_gate_can_only_be_entered_once() -> None:
    class Endpoint:
        _invocation_id = "e" * 32

        @staticmethod
        def _acquire_job_slot():
            return None, None

    gate = AdmissionGate(Endpoint())  # type: ignore[arg-type]
    with gate.enter():
        pass
    with pytest.raises(RuntimeError, match="exactly once"), gate.enter():
        pass


def test_unmanaged_admission_never_reports_cancellation() -> None:
    assert _admission_cancellation(None, object())() is False


@pytest.mark.parametrize(
    "kwargs",
    [
        {"coverage": "yes"},
        {"test": ("a", "a")},
        {"test": ("a",), "tests_file": Path("tests.txt")},
        {"test": ()},
        {"result_verbosity": "verbose"},
        {"resume_from": Path("manifest.json"), "target": "sim"},
        {"resume_from": Path("manifest.json"), "mode": SimulationMode.ELAB_ONLY},
    ],
)
def test_sim_request_rejects_ambiguous_or_invalid_inputs(kwargs) -> None:
    with pytest.raises(ValueError):
        SimRequest(**kwargs)


def test_sim_request_rejects_unreadable_empty_and_duplicate_test_files(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot read"):
        SimRequest(tests_file=tmp_path / "missing")
    empty = tmp_path / "empty"
    empty.write_text("# comment\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no test names"):
        SimRequest(tests_file=empty)
    duplicate = tmp_path / "duplicate"
    duplicate.write_text("a\na\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unique"):
        SimRequest(tests_file=duplicate)


@pytest.mark.parametrize("value", ["", "bad\0path", "{unknown}", "{test!r}", "{test:10}"])
def test_run_cwd_template_rejects_invalid_forms(value: str) -> None:
    with pytest.raises(ValueError):
        parse_run_cwd_template(value)


def test_run_cwd_template_deduplicates_placeholders_in_first_seen_order() -> None:
    assert parse_run_cwd_template("runs/{test}/{attempt}/{test}") == ("test", "attempt")


def test_campaign_model_only_freezes_json_shaped_values() -> None:
    with pytest.raises(TypeError, match="keys"):
        _freeze_json({1: "value"})
    with pytest.raises(TypeError, match="non-JSON"):
        _freeze_json(object())
