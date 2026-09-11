"""Exact-path Coverage Analyst wrapper with real Campaign/source files."""

import copy
import json
from pathlib import Path

import pytest

from booley.core.models import AgentResult
from booley.flows.sim.coverage_campaign import DurableTargetIdentity, decode_coverage_campaign
from booley.flows.sim.coverage_campaign_store import publish_coverage_campaign
from booley.specialists.coverage_analysis import CoverageAnalysisError
from booley.specialists.coverage_analyst import CoverageAnalystSpecialist
from tests.flows.sim.test_coverage_campaign import _valid_document


def persist_campaign(root: Path):
    document = _valid_document()
    campaign = decode_coverage_campaign(
        document, DurableTargetIdentity(document["target"]["identity"])
    )
    target_dir = root / "reports/sim/12/targets/sim_counter"
    path = publish_coverage_campaign(target_dir, campaign).campaign
    (target_dir / "simulation.json").write_text(
        json.dumps(
            {
                "flow": "sim",
                "complete": True,
                "target": "sim_counter",
                "target_identity": document["target"]["identity"],
                "collection": "complete",
                "evaluation": "not_requested",
                "coverage_campaign": "coverage.json",
            }
        )
    )
    return path


class Model:
    def __init__(self):
        self.calls = []

    def __call__(self, params):
        self.calls.append(params)
        return AgentResult(
            structured={"hypotheses": [], "recommendations": [], "waiver_candidates": []}
        )


def _successful_claude_query(options_seen):
    from claude_agent_sdk import ResultMessage

    async def query(*, prompt, options):
        options_seen.append(options)
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="test-coverage",
            result="",
            total_cost_usd=0.01,
            usage={"input_tokens": 20, "output_tokens": 5},
            structured_output={"hypotheses": [], "recommendations": [], "waiver_candidates": []},
        )

    return query


def persist_large_v3_campaign(root: Path, point_count: int = 2_000) -> Path:
    from booley.flows.sim.coverage_campaign import (
        DurableTargetIdentity,
        _point_id,
        decode_coverage_campaign,
    )
    from booley.flows.sim.coverage_campaign_store import publish_coverage_campaign

    document = _valid_document()
    template = document["points"][0]
    points = []
    for index in range(point_count):
        point = copy.deepcopy(template)
        line = index + 1
        point["identity"]["location"]["start"]["line"] = line
        point["identity"]["location"]["end"]["line"] = line
        point["identity"]["subject"]["basic_block"] = index
        point["identity"]["collector"]["native_key"] = f"{line}:basic-block-{index}"
        point["id"] = _point_id(point["identity"])
        points.append(point)
    document["points"] = points
    document["rollups"][0].update(
        total_points=point_count,
        eligible_points=point_count,
        covered_points=point_count,
    )
    campaign = decode_coverage_campaign(
        document, DurableTargetIdentity(document["target"]["identity"])
    )
    target_dir = root / "reports/sim/12/targets/sim_counter"
    paths = publish_coverage_campaign(target_dir, campaign)
    (target_dir / "simulation.json").write_text(
        json.dumps(
            {
                "flow": "sim",
                "complete": True,
                "target": "sim_counter",
                "target_identity": document["target"]["identity"],
                "collection": "complete",
                "evaluation": "not_requested",
            }
        )
    )
    return paths.campaign


def test_large_v3_campaign_uses_scoped_evidence_tool_without_oversized_prompt(tmp_path):
    path = persist_large_v3_campaign(tmp_path)
    calls = []

    def bounded_model(params):
        calls.append(params)
        if len(params.prompt) > 1_048_576:
            raise RuntimeError(
                "Codex exit code 1: turn/start input_too_large: "
                f"max_chars=1048576, actual_chars={len(params.prompt)}"
            )
        return AgentResult(
            structured={"hypotheses": [], "recommendations": [], "waiver_candidates": []}
        )

    result = CoverageAnalystSpecialist(model=bounded_model).execute_cli(
        ["--work-dir", str(tmp_path), "--campaign", str(path)]
    )

    assert result.exit_code == 0
    assert len(calls[0].prompt) < 16_384
    assert calls[0].nested_mcp_tools == ["coverage_evidence"]
    assert calls[0].nested_mcp_env["BOOLEY_COVERAGE_CAMPAIGN"] == str(path)
    assert "coverage-points.jsonl.gz" in calls[0].prompt
    assert "points" not in result.outcome.detail["observed_evidence"]
    assert result.outcome.detail["observed_evidence"]["point_store_sha256"].startswith("sha256:")


def test_report_records_exact_evidence_scope(tmp_path, monkeypatch):
    from booley.mcp import coverage_evidence

    path = persist_campaign(tmp_path)

    def querying_model(params):
        for key, value in params.nested_mcp_env.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setattr(coverage_evidence, "_ACTIVE_SESSION", None)
        coverage_evidence.query_active_coverage_evidence(
            {"view": "points", "disposition": "eligible", "limit": 1}
        )
        return AgentResult(
            structured={"hypotheses": [], "recommendations": [], "waiver_candidates": []}
        )

    analyst = CoverageAnalystSpecialist(model=querying_model)
    analyst.parse_args(["--work-dir", str(tmp_path), "--campaign", str(path)])
    report = analyst.coverage_analyst(path).to_dict()

    assert report["analysis_scope"]["points_retrieved"] == 1
    assert report["analysis_scope"]["point_ids"] == [_valid_document()["points"][0]["id"]]


def test_exact_campaign_wrapper_analyzes_without_native_payload_or_project_state(tmp_path):
    path = persist_campaign(tmp_path)
    model = Model()
    analyst = CoverageAnalystSpecialist(model=model)
    analyst.parse_args(["--work-dir", str(tmp_path), "--campaign", str(path)])
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    report = analyst.coverage_analyst(path, "Explain the gaps").to_dict()
    assert report["$schema"] == "booley.coverage-analysis/v2"
    assert report["source_access"] == "report_only"
    assert len(model.calls) == 1
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize(
    "defect", ["filename", "invocation", "target", "nonterminal", "projection", "missing"]
)
def test_wrapper_rejects_noncanonical_or_unfinished_report_before_model(tmp_path, defect):
    path = persist_campaign(tmp_path)
    if defect == "filename":
        other = path.with_name("latest.json")
        path.rename(other)
        path = other
    elif defect in {"invocation", "target"}:
        document = json.loads(path.read_text())
        if defect == "invocation":
            document["invocation"]["id"] = 13
        else:
            document["target"]["selector"] = "different"
        path.write_text(json.dumps(document))
    elif defect == "missing":
        path.unlink()
    else:
        projection = path.parent / "simulation.json"
        document = json.loads(projection.read_text())
        document["complete" if defect == "nonterminal" else "target_identity"] = False
        projection.write_text(json.dumps(document))
    model = Model()
    analyst = CoverageAnalystSpecialist(model=model)
    analyst.parse_args(["--work-dir", str(tmp_path), "--campaign", str(path)])
    with pytest.raises(CoverageAnalysisError):
        analyst.coverage_analyst(path)
    assert model.calls == []


def source_project(root):
    from booley.flows.sim.coverage_provenance import content_digest, coverage_digest

    path = persist_campaign(root)
    (root / "rtl").mkdir()
    (root / "tb").mkdir()
    (root / "rtl/counter.sv").write_bytes(b"module counter; endmodule\n")
    (root / "tb/counter_tb.sv").write_bytes(b"module counter_tb; endmodule\n")
    core = root / "counter.core"
    core.write_text(
        "CAPI=2:\nname: acme:demo:counter:1.0\nfilesets:\n  rtl:\n    files: [rtl/counter.sv]\n    file_type: systemVerilogSource\n  tb:\n    files: [tb/counter_tb.sv]\n    file_type: systemVerilogSource\n    tags: [tb]\ntargets:\n  sim_counter:\n    flow: sim\n    filesets: [rtl, tb]\n    toplevel: counter_tb\n    flow_options:\n      tool: verilator\n"
    )
    document = json.loads(path.read_text())
    for category in ("rtl", "testbench"):
        for item in document["source_closure"][category]:
            item["sha256"] = content_digest((root / item["path"]).read_bytes())
        document["fingerprints"][category + "_sources"] = coverage_digest(
            document["source_closure"][category]
        )
    document["fingerprints"]["target_definition"] = coverage_digest(
        {"core": core.read_text(), "identity": document["target"]["identity"]}
    )
    path.write_text(json.dumps(document))
    return path


def test_verified_sources_are_available_only_through_scoped_tool(tmp_path):
    path = source_project(tmp_path)
    model = Model()
    analyst = CoverageAnalystSpecialist(model=model)
    analyst.parse_args(["--work-dir", str(tmp_path), "--campaign", str(path)])
    report = analyst.coverage_analyst(path).to_dict()
    assert report["source_access"] == "verified"
    prompt = json.loads(model.calls[0].prompt)
    assert prompt["source_access"] == "verified"
    assert "module counter" not in model.calls[0].prompt
    assert model.calls[0].nested_mcp_tools == ["coverage_evidence"]


@pytest.mark.parametrize("ignore_native", [False, True])
def test_stealth_analysis_preserves_project_files(tmp_path, ignore_native):
    path = source_project(tmp_path)
    state = tmp_path / ".booley_project"
    (state / "cores").mkdir(parents=True)
    (tmp_path / "counter.core").rename(state / "cores/counter.core")
    (state / "booley.toml").write_text(
        f"[stealth]\nenabled = true\nignore_native_cores = {str(ignore_native).lower()}\n"
    )
    model = Model()
    analyst = CoverageAnalystSpecialist(model=model)
    analyst.parse_args(["--work-dir", str(tmp_path), "--campaign", str(path)])
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    report = analyst.coverage_analyst(path).to_dict()

    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    assert report["source_access"] == "report_only"
    assert json.loads(model.calls[0].prompt)["source_access"] == "report_only"


@pytest.mark.parametrize("defect", ["stale", "missing", "core", "closure", "symlink"])
def test_source_mismatch_degrades_transactionally_to_report_only(tmp_path, defect):
    path = source_project(tmp_path)
    source = tmp_path / "tb/counter_tb.sv"
    if defect == "stale":
        source.write_text("SECRET changed source")
    elif defect == "missing":
        source.unlink()
    elif defect == "core":
        (tmp_path / "counter.core").write_text("invalid target")
    elif defect == "closure":
        document = json.loads(path.read_text())
        document["source_closure"]["testbench"] = []
        path.write_text(json.dumps(document))
    else:
        from tests.conftest import symlink_or_skip

        source.unlink()
        symlink_or_skip(source, tmp_path / "rtl/counter.sv")
    model = Model()
    analyst = CoverageAnalystSpecialist(model=model)
    analyst.parse_args(["--work-dir", str(tmp_path), "--campaign", str(path)])
    report = analyst.coverage_analyst(path).to_dict()
    assert report["source_access"] == "report_only"
    assert json.loads(model.calls[0].prompt)["source_access"] == "report_only"
    assert "SECRET" not in model.calls[0].prompt


def test_verified_candidate_is_ready_only_for_human_review(tmp_path):
    path = source_project(tmp_path)
    point = _valid_document()["points"][0]

    def model(params):
        return AgentResult(
            structured={
                "hypotheses": [],
                "recommendations": [],
                "waiver_candidates": [
                    {
                        "point_id": point["id"],
                        "reason": "excluded",
                        "evidence": "Project scope excludes this hardware",
                        "proof_reference": "",
                    }
                ],
            }
        )

    analyst = CoverageAnalystSpecialist(model=model)
    analyst.parse_args(["--work-dir", str(tmp_path), "--campaign", str(path)])
    candidate = analyst.coverage_analyst(path).to_dict()["waiver_candidates"][0]
    assert candidate["screening"] == "ready_for_human_review"
    assert candidate["point_identity"] == point["identity"]
    assert candidate["approval"] == "not_approved"


def test_cli_analysis_never_loads_or_saves_harness_criteria(tmp_path, monkeypatch):
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    path = persist_campaign(tmp_path)
    state = tmp_path / "state.json"
    state.write_text("intentionally not a Harness state")
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(state))
    result = CoverageAnalystSpecialist(model=Model()).execute_cli(
        [
            "--work-dir",
            str(tmp_path),
            "--campaign",
            str(path),
            "--report-dir",
            str(tmp_path / "analysis-reports"),
        ]
    )
    assert result.exit_code == 0
    assert state.read_text() == "intentionally not a Harness state"
    assert result.outcome.criterion_key == ""


@pytest.mark.parametrize("failure", ["timed_out", "max_turns_exhausted"])
def test_cli_reports_unfinished_model_as_analysis_error(tmp_path, failure):
    path = persist_campaign(tmp_path)
    analyst = CoverageAnalystSpecialist(model=lambda params: AgentResult(**{failure: True}))
    result = analyst.execute_cli(["--work-dir", str(tmp_path), "--campaign", str(path)])
    assert result.exit_code == 2
    assert "model did not finish" in result.outcome.report_text


def test_cli_reports_backend_input_limit_as_actionable_error(tmp_path):
    from booley.runtime.agent_errors import ContextExhaustedError

    path = persist_campaign(tmp_path)

    def oversized(_params):
        raise ContextExhaustedError(
            "turn/start input_too_large: max_chars=1048576, actual_chars=57231875",
            provider="codex",
        )

    result = CoverageAnalystSpecialist(model=oversized).execute_cli(
        ["--work-dir", str(tmp_path), "--campaign", str(path)]
    )

    assert result.exit_code == 2
    assert "model input capacity" in result.outcome.report_text
    assert "Narrow the analysis instruction" in result.outcome.report_text


def test_cli_rejects_partial_report_after_evidence_budget_exhaustion(tmp_path):
    path = persist_campaign(tmp_path)

    def exhausted_model(params):
        Path(params.nested_mcp_env["BOOLEY_COVERAGE_AUDIT"]).write_text(
            json.dumps({"budget_exhausted": True}), encoding="utf-8"
        )
        return AgentResult(
            structured={"hypotheses": [], "recommendations": [], "waiver_candidates": []}
        )

    result = CoverageAnalystSpecialist(model=exhausted_model).execute_cli(
        ["--work-dir", str(tmp_path), "--campaign", str(path)]
    )

    assert result.exit_code == 2
    assert "evidence budget exhausted" in result.outcome.report_text
    assert "narrow the analysis instruction" in result.outcome.report_text


def test_cli_rejects_partial_report_after_terminal_evidence_error(tmp_path):
    path = persist_campaign(tmp_path)

    def failed_evidence_model(params):
        Path(params.nested_mcp_env["BOOLEY_COVERAGE_AUDIT"]).write_text(
            json.dumps({"terminal_error": "One Coverage Point exceeds the response limit"}),
            encoding="utf-8",
        )
        return AgentResult(
            structured={"hypotheses": [], "recommendations": [], "waiver_candidates": []}
        )

    result = CoverageAnalystSpecialist(model=failed_evidence_model).execute_cli(
        ["--work-dir", str(tmp_path), "--campaign", str(path)]
    )

    assert result.exit_code == 2
    assert "One Coverage Point exceeds the response limit" in result.outcome.report_text
    assert "Narrow the analysis instruction" in result.outcome.report_text


def test_persisted_collection_remains_analyzable_after_native_pruning(tmp_path):
    from booley.flows.sim.campaign_retention import prune_invocation, prune_native_payload
    from tests.flows.sim.test_campaign_retention import campaign

    outcome = campaign(tmp_path)
    analyst = CoverageAnalystSpecialist(model=Model())
    analyst.parse_args(["--work-dir", str(tmp_path), "--campaign", str(outcome.campaign_path)])
    before = analyst.coverage_analyst(outcome.campaign_path).to_dict()
    prune_native_payload(tmp_path / "reports", 1, "sim_0")
    after = analyst.coverage_analyst(outcome.campaign_path).to_dict()
    assert after == before
    assert after["source_access"] == "verified"
    prune_invocation(tmp_path / "reports", 1)
    with pytest.raises(CoverageAnalysisError, match="fully pruned"):
        analyst.coverage_analyst(outcome.campaign_path)


def test_relative_campaign_resolves_against_selected_work_dir(tmp_path):
    path = persist_campaign(tmp_path)
    analyst = CoverageAnalystSpecialist(model=Model())
    analyst.parse_args(
        ["--work-dir", str(tmp_path), "--campaign", str(path.relative_to(tmp_path))]
    )
    result = analyst.execute_cli(
        ["--work-dir", str(tmp_path), "--campaign", str(path.relative_to(tmp_path))]
    )
    assert result.exit_code == 0


def test_default_wrapper_keeps_role_usage_and_exposes_only_evidence_tool(tmp_path, monkeypatch):
    from booley.config.agent import AgentSettings
    from booley.runtime import _claude_backend
    from booley.runtime.agent_backend import ClaudeSDKBackend
    from booley.runtime.agent_config import BackendConfig, set_backend_config

    path = persist_campaign(tmp_path)
    options_seen = []

    monkeypatch.setattr(_claude_backend, "query", _successful_claude_query(options_seen))
    set_backend_config(
        BackendConfig(
            settings=AgentSettings(
                provider="claude",
                role_models={"coverage_analyst": "chosen-model"},
            ),
            active_backend=ClaudeSDKBackend(),
        )
    )
    try:
        result = CoverageAnalystSpecialist().execute_cli(
            [
                "--work-dir",
                str(tmp_path),
                "--campaign",
                str(path),
                "--transcript-dir",
                str(tmp_path / "transcripts"),
            ]
        )
    finally:
        set_backend_config(None)
    assert result.exit_code == 0
    assert result.outcome.input_tokens == 20
    assert result.outcome.output_tokens == 5
    assert options_seen[0].model == "chosen-model"
    assert options_seen[0].tools == []
    assert set(options_seen[0].mcp_servers) == {"booley"}
    server_env = options_seen[0].mcp_servers["booley"]["env"]
    assert server_env["BOOLEY_NESTED_MCP_TOOLS"] == "coverage_evidence"
    assert server_env["BOOLEY_COVERAGE_CAMPAIGN"] == str(path)
    assert not Path(options_seen[0].cwd).exists()
    assert list((tmp_path / "transcripts").rglob("*.jsonl"))


def test_in_memory_composition_stages_v2_evidence_for_the_scoped_tool(monkeypatch):
    from booley.flows.sim.coverage_campaign import DurableTargetIdentity, decode_coverage_campaign
    from booley.specialists.coverage_analyst import analyze_coverage_campaign

    campaign = decode_coverage_campaign(
        _valid_document(), DurableTargetIdentity("acme:demo:counter:1.0#sim_counter")
    )
    staged_paths = []

    def invoke(_self, params):
        campaign_path = Path(params.nested_mcp_env["BOOLEY_COVERAGE_CAMPAIGN"])
        staged_paths.append(campaign_path)
        assert campaign_path.is_file()
        assert campaign_path.with_name("coverage-points.jsonl.gz").is_file()
        return AgentResult(
            structured={"hypotheses": [], "recommendations": [], "waiver_candidates": []}
        )

    monkeypatch.setattr(CoverageAnalystSpecialist, "_invoke_agent", invoke)

    report = analyze_coverage_campaign(campaign, None, "Explain gaps").to_dict()

    assert report["$schema"] == "booley.coverage-analysis/v2"
    assert staged_paths and not staged_paths[0].exists()
