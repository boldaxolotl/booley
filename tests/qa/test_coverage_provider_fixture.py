"""Validate the external provider fixture's protocol and isolated negative inputs."""

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "qa/shared/coverage/faults"
SPEC = importlib.util.spec_from_file_location("qa_provider", ROOT / "provider.py")
provider = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(provider)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux Session Runtime pipe transport")
def test_burst_messages_do_not_wait_for_more_fd_input():
    reader, writer = os.pipe()
    os.write(writer, b'{"id":1}\n{"id":2}\n')
    with os.fdopen(reader) as stream:
        try:
            deadline = time.monotonic() + 0.1
            assert provider.receive(stream, deadline) == {"id": 1}
            assert provider.receive(stream, deadline) == {"id": 2}
        finally:
            os.close(writer)


class Recorder:
    def __init__(self):
        self.requests = []

    def query(self, **request):
        self.requests.append(request)
        return {"points": [{"point_ref": "point:1"}], "next_cursor": "cursor"}


def test_observed_hit_candidate_has_proof_to_isolate_contradiction():
    evidence = Recorder()
    result = provider.advisory(evidence, "observed-hit")
    assert evidence.requests[-1]["covered"] is True
    assert result["waiver_candidates"][0]["proof_reference"] == "proof/parity.log"


def test_cross_campaign_uses_actual_foreign_selector(tmp_path, monkeypatch):
    foreign = tmp_path / "coverage.json"
    foreign.write_text("{}")
    monkeypatch.setenv("QA_FOREIGN_CAMPAIGN", str(foreign))
    evidence = Recorder()
    provider.query_fault(evidence, "cross-campaign")
    assert evidence.requests[-1] == {"view": "overview", "campaign": str(foreign)}
    provider.query_fault(evidence, "undelivered")
    assert evidence.requests[-1] == {"view": "source", "point_refs": ["point:999999999"]}


@pytest.mark.parametrize("adapter", ["codex", "claude"])
def test_incomplete_output_exits_without_terminal_success_or_failure(adapter, capsys):
    provider.output(adapter, "incomplete", {}, None)
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert records
    assert not any(r["type"] in {"result", "turn.completed", "turn.failed"} for r in records)


def test_legal_response_budget_case_uses_maximum_admitted_page(tmp_path, monkeypatch):
    evidence = Recorder()
    evidence.start = lambda: None
    evidence.close = lambda: None
    monkeypatch.setattr(provider, "Evidence", lambda *_: evidence)
    provider.model_result({}, False, tmp_path, "response-budget")
    assert evidence.requests[-1] == {"view": "points", "limit": 100}
    assert (tmp_path / "legal-page.json").is_file()


@pytest.mark.parametrize("case", ["incomplete", "context-exhausted"])
@pytest.mark.parametrize("adapter", ["codex", "claude"])
def test_failed_evidence_setup_is_not_replaced_by_intended_model_fault(case, adapter, capsys):
    with pytest.raises(SystemExit):
        provider.output(adapter, case, {}, "real MCP initialization failed")
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    last = records[-1]
    assert last["type"] in {"turn.failed", "result"}
    assert "real MCP initialization failed" in json.dumps(last)
    assert "maximum context" not in json.dumps(last)
