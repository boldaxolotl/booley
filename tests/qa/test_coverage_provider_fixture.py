"""The coverage provider fixture must record the product's real evidence rejection."""

import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "qa_coverage_provider",
    Path(__file__).resolve().parents[2] / "qa/shared/coverage/faults/provider.py",
)
provider = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(provider)


def _evidence_returning(result: dict):
    """Build an Evidence client whose tool call returns *result* without a server."""
    evidence = provider.Evidence.__new__(provider.Evidence)
    evidence.request = lambda method, params: result
    return evidence


def _text(value: str) -> dict:
    return {"content": [{"type": "text", "text": value}]}


def test_plain_text_schema_rejection_is_recorded_verbatim():
    rejection = "Invalid tool arguments at $.limit: 100000 is greater than the maximum of 100"
    evidence = _evidence_returning({**_text(rejection), "isError": True})
    with pytest.raises(ValueError) as caught:
        evidence.query(view="points", limit=100000)
    assert str(caught.value) == rejection


def test_json_error_result_is_recorded_verbatim():
    rejection = json.dumps({"error": "coverage_evidence_rejected", "message": "Malformed cursor"})
    evidence = _evidence_returning({**_text(rejection), "isError": True})
    with pytest.raises(ValueError) as caught:
        evidence.query(view="points", cursor="bad")
    assert str(caught.value) == rejection


def test_success_returns_the_decoded_document():
    evidence = _evidence_returning(_text(json.dumps({"view": "overview"})))
    assert evidence.query(view="overview") == {"view": "overview"}
