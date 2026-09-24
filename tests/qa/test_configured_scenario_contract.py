"""Protect the complete reviewed Configured Scenario semantics."""

import hashlib
import json
from pathlib import Path

import yaml
from qa.validate import (
    load_scenarios,
    resolved_configured_checks,
    resolved_pre_run_requirements,
)

ROOT = Path(__file__).resolve().parents[2] / "qa"


def membership_digest(values):
    return hashlib.sha256(("\n".join(sorted(values)) + "\n").encode()).hexdigest()


def scenario_step(scenario, step_id):
    return next(step for step in scenario["steps"] if step["id"] == step_id)


def test_every_configured_scenario_preserves_reviewed_semantics():
    contract = json.loads(
        Path(__file__).with_name("configured-scenario-contract.json").read_text()
    )["configured_scenarios"]
    scenarios = load_scenarios(ROOT)
    configured_scenarios = {
        configured["id"]: (scenario, configured)
        for scenario in scenarios.values()
        for configured in scenario["configured_scenarios"]
    }
    assert configured_scenarios.keys() == contract.keys()
    for configured_id, (scenario, configured) in configured_scenarios.items():
        expected = contract[configured_id]
        prefix = scenario["scenario_id"] + "."
        checks = [
            prefix + check for check in resolved_configured_checks(scenario, configured, ROOT)
        ]
        exclusion_checks = [prefix + item["check"] for item in configured["exclusions"]]
        assert configured["required"] == expected["required"]
        assert membership_digest(checks) == expected["checks"], configured_id
        assert membership_digest(exclusion_checks) == expected["exclusions"]
        semantics = {
            "parameters": configured["parameters"],
            "pre_run_requirements": resolved_pre_run_requirements(scenario, configured),
            "exclusions": configured["exclusions"],
        }
        assert (
            hashlib.sha256(
                json.dumps(semantics, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            == expected["semantics"]
        )


def test_origin_push_checks_exercise_an_external_git_server():
    coverage = yaml.safe_load((ROOT / "coverage.yaml").read_text())
    capability = next(
        item for item in coverage["capabilities"] if item["id"] == "SECURITY-RUNTIME-ISOLATION"
    )
    assert "outside-runtime control push" in capability["contract"]
    assert "Sandbox attempts the same authorized transport" in capability["contract"]
    assert "container-local and local-path remotes are out of scope" in capability["contract"]

    scenarios = load_scenarios(ROOT)
    for scenario_id in (
        "picorv32-published-demo-continuity",
        "taxi-10g-mac-port-evolution",
        "opentitan-uart-clean-room-greenfield",
    ):
        step = scenario_step(
            scenarios[scenario_id], "inventory.SECURITY-RUNTIME-ISOLATION.origin-push-deny"
        )
        check = step["checks"][0]
        assert "outside-runtime control client can push" in check["stimulus"]
        assert "same authorized network transport" in check["stimulus"]
        assert "outside-runtime control push succeeds" in check["expected"]
        assert "blocked by its network boundary" in check["expected"]
        assert "external probe ref remains unchanged" in check["expected"]
        assert "successful outside-runtime control push" in check["evidence"]
        assert "identifying the network-policy denial" in check["evidence"]
        assert "local-path remotes cannot satisfy this Check" in check["evidence"]


def test_picorv32_ticket_destinations_include_applicable_project_setup():
    scenario = load_scenarios(ROOT)["picorv32-published-demo-continuity"]
    setup = scenario_step(scenario, "project.separate-repository")
    expected = setup["checks"][0]["expected"]
    assert "paired destination refs exist before Ticket Create" in expected
    assert "flows.fpga.enabled = true" in expected
    assert "configurations that exclude FPGA preserve their declared opt-out" in expected
    assert "Both repositories are clean" in expected
    assert "publish the intended command and pre-state checkpoint" in setup["action"]
    assert "publish the complete cleanup-ledger candidate" in setup["action"]
    assert "then execute" in setup["action"]
    assert "exact acquired identities" in setup["action"]

    for step_id in (
        "create.dhrystone-self-checking-cycle-contract.payload",
        "create.rv32-zbb-pcpi.payload",
    ):
        assert "project.separate-repository" in scenario_step(scenario, step_id)["requires"]

    zbb = scenario_step(scenario, "create.rv32-zbb-pcpi.payload")["checks"][0]
    assert "already-prepared immutable project-data destination" in zbb["expected"]
    assert "Configurations that exclude FPGA" in zbb["expected"]


def test_picorv32_ticket_assets_bind_both_destination_roles():
    schema = json.loads((ROOT / "scenario.schema.json").read_text())
    asset = schema["properties"]["steps"]["items"]["properties"]["assets"]["items"]
    substitutions = asset["properties"]["substitutions"]["items"]["enum"]
    assert "outer_destination_branch" in substitutions
    assert "project_destination_ref" in substitutions
    assert "configured_fpga_criterion" in substitutions

    scenario = load_scenarios(ROOT)["picorv32-published-demo-continuity"]
    expected_assets = {
        "create.dhrystone-self-checking-cycle-contract.payload": "tickets/continuity.md",
        "create.rv32-zbb-pcpi.payload": "tickets/evolution.md",
    }
    for step_id, asset_path in expected_assets.items():
        step = scenario_step(scenario, step_id)
        ticket_asset = next(item for item in step["assets"] if item["path"] == asset_path)
        expected_substitutions = [
            "outer_destination_branch",
            "project_destination_ref",
        ]
        if asset_path == "tickets/evolution.md":
            expected_substitutions.append("configured_fpga_criterion")
        assert ticket_asset["substitutions"] == expected_substitutions
        ticket_text = (ROOT / "scenarios/picorv32" / asset_path).read_text()
        assert "branch: {{ outer_destination_branch }}" in ticket_text
        assert "project_destination_ref: {{ project_destination_ref }}" in ticket_text
        check = step["checks"][0]
        assert "git-topology" in check["stimulus"]
        assert "ticket-routing" in check["expected"]
        assert "field-to-ref mapping" in check["evidence"]
        assert check["authority_ref"].endswith("docs/user/USAGE.md#creating-tickets")


def test_picorv32_ticket_create_attempts_are_file_backed_bounded_and_not_retried():
    scenario = load_scenarios(ROOT)["picorv32-published-demo-continuity"]
    create_phase = next(phase for phase in scenario["phases"] if phase["id"] == "create")
    create_steps = [
        scenario_step(scenario, "create.dhrystone-self-checking-cycle-contract.payload"),
        scenario_step(scenario, "create.rv32-zbb-pcpi.payload"),
    ]

    assert sum(step["timeout_minutes"] for step in create_steps) <= create_phase["minutes"]
    assert (
        sum(phase["minutes"] for phase in scenario["phases"])
        + scenario["budget"]["contingency_minutes"]
        <= scenario["budget"]["deadline_minutes"]
    )
    for step in create_steps:
        assert "retry" not in step
        check = step["checks"][0]
        assert "--input-file" in check["stimulus"]
        assert "staged" in check["evidence"]
        assert "post-attempt" in check["evidence"]

    developer_retry = scenario_step(scenario, "retry.exact-allowance")
    assert developer_retry["retry"] == {
        "max_attempts": 1,
        "signature": "API Error: Response stalled mid-stream",
    }
    assert "Developer Agent" in developer_retry["action"]
    assert "never applies to either Ticket Create attempt" in developer_retry["action"]


def test_picorv32_ticket_two_packet_uses_current_v2_static_vocabulary():
    packet = (ROOT / "scenarios/picorv32/tickets/evolution.md").read_text()
    assert "CRITERIA_MANDATORY:" in packet
    assert "on_success: [triage_report, review, merge, cleanup]" in packet
    assert "sim_core_zbb (temp)" in packet
    assert "target_plan:" not in packet
    assert "role: ephemeral" not in packet
    assert "elab_pass:" not in packet
    assert "sim_pass:" not in packet

    rendered = (
        packet.replace("{{ outer_destination_branch }}", "outer-release")
        .replace("{{ project_destination_ref }}", "refs/heads/project-data")
        .replace(
            "{{ configured_fpga_criterion }}",
            "  FPGA:\n    fpga_core_zbb (temp): pass",
        )
    )
    frontmatter = rendered.split("```markdown\n---\n", 1)[1].split("\n---\n", 1)[0]
    fields = yaml.safe_load(frontmatter)
    assert fields["branch"] == "outer-release"
    assert fields["project_destination_ref"] == "refs/heads/project-data"
    assert fields["CRITERIA_MANDATORY"]["FPGA"] == {"fpga_core_zbb (temp)": "pass"}


def test_ticket_create_interruption_guidance_preserves_evidence_status_boundary():
    execute = (ROOT / "doc/EXECUTE.md").read_text()
    record = (ROOT / "doc/RECORD.md").read_text()
    run_skill = (ROOT / "booley-qa-run/SKILL.md").read_text()
    record_contract = " ".join(record.split())

    for category in (
        "terminal product failure",
        "client/provider error",
        "declared timeout",
        "operator stop",
        "lost control",
    ):
        assert category in run_skill
    assert "Missing later milestones" in execute
    assert "starts Finish immediately" in execute
    assert "before a trustworthy behavioral result is `blocked`" in record_contract
    assert "not contradictory evidence" in record_contract
    assert "missing prompt or payload hash" in record_contract


def test_picorv32_public_qa_corrections_are_explicitly_contractual():
    scenario = load_scenarios(ROOT)["picorv32-published-demo-continuity"]
    for name in ("wave", "find", "sample", "distance", "value"):
        assert (
            "interactive.wishbone-baseline"
            in scenario_step(scenario, f"bwave.semantic-{name}")["requires"]
        )
    for name in ("list", "signal", "diff", "stats", "stuck"):
        assert (
            "interactive.wishbone-baseline"
            not in scenario_step(scenario, f"bwave.rejection-{name}")["requires"]
        )
    assert (
        "interactive.reproduce"
        in scenario_step(scenario, "interactive.trace-observability")["requires"]
    )
    assert (
        "interactive.trace-observability"
        in scenario_step(scenario, "interactive.bwave-diagnosis")["requires"]
    )
    assert "interactive.reproduce" in scenario_step(scenario, "interactive.repair")["requires"]
    for name in ("rerun-simulation", "rerun-lint", "local-commit"):
        assert "interactive.repair" in scenario_step(scenario, f"interactive.{name}")["requires"]
    baseline = scenario_step(scenario, "baseline.source-unchanged")
    assert "post-setup destination ref" in baseline["checks"][0]["expected"]
    assert (
        "does not compare Project data with the pre-setup Project pin"
        in baseline["checks"][0]["expected"]
    )
    assert "project.separate-repository" in baseline["requires"]

    doctor = scenario_step(scenario, "doctor.deep")["checks"][0]
    assert "stable warning signatures" in doctor["evidence"]
    assert "raw message SHA-256 alone are never pass conditions" in doctor["expected"]

    repair = scenario_step(scenario, "interactive.repair")["checks"][0]
    assert "assign we = |mem_wstrb;" in repair["expected"]
    assert "separate negative fixture case" in repair["expected"]
    commit = scenario_step(scenario, "interactive.local-commit")["checks"][0]
    assert "meaningful `picorv32.v` repair diff" in commit["expected"]
