"""Origin-owned reverse dependency receipts for Simulation Campaign resumes."""

import hashlib
import json
import shutil

import pytest

CAMPAIGN_ID = "01234567-89ab-4def-8123-456789abcdef"


def _origin(tmp_path):
    origin = tmp_path / "reports/sim/1/targets/sim_0/campaign/manifest.json"
    origin.parent.mkdir(parents=True)
    origin.write_text(json.dumps({"campaign_id": CAMPAIGN_ID}) + "\n", encoding="utf-8")
    digest = "sha256:" + hashlib.sha256(origin.read_bytes()).hexdigest()
    return origin, digest


def test_same_selector_baseline_and_candidate_hook_owners_are_collected_once(
    tmp_path, monkeypatch
):
    from dataclasses import replace

    from booley.flows.sim.campaign import collect_pre_sim_firings
    from booley.flows.sim.campaign.model import create_simulation_campaign_plan
    from booley.flows.sim.campaign.store import CampaignStore
    from booley.flows.sim.flow import (
        SimulateFlow,
        _campaign_pre_sim_details,
        _campaign_pre_sim_report_lines,
    )
    from tests.flows.sim.test_campaign_phase3_integrity import _Group
    from tests.flows.sim.test_endpoint_campaign_lifecycle import _successful_pre_sim_campaign

    original = _Group.launch_snapshot

    def with_cycles(group, *args):
        outcome = original(group, *args)
        return replace(
            outcome,
            tests=tuple(
                replace(test, cycles=17, cycle_status="observed") for test in outcome.tests
            ),
        )

    monkeypatch.setattr(_Group, "launch_snapshot", with_cycles)
    baseline_root = tmp_path / "baseline"
    baseline_root.mkdir()
    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir()
    invocation = tmp_path / "reports/1"

    def baseline_role(document):
        document["campaign_id"] = CAMPAIGN_ID
        document["target"].update(role="cycle_count_baseline", revision="baseline")

    _, baseline = _successful_pre_sim_campaign(
        baseline_root,
        monkeypatch,
        "immutable",
        transform=baseline_role,
        invocation_directory=invocation,
    )
    manifest = CampaignStore(baseline.manifest_path.parent).load_manifest()
    prerequisites = SimulateFlow._campaign_prerequisite_documents(
        create_simulation_campaign_plan(manifest), baseline.manifest_path.relative_to(invocation)
    )
    _, candidate = _successful_pre_sim_campaign(
        candidate_root,
        monkeypatch,
        "immutable",
        transform=lambda document: document.update(prerequisites=prerequisites),
        invocation_directory=invocation,
    )
    assert candidate.aggregate_grade == "pass"
    assert len(collect_pre_sim_firings(candidate.manifest_path)) == 2
    detail = _campaign_pre_sim_details([candidate, baseline], invocation)
    assert detail["pre_sim_total"] == detail["pre_sim_current"] == 2
    assert set(detail["pre_sim_roles"]) == {"candidate", "cycle_count_baseline"}
    lines = _campaign_pre_sim_report_lines([candidate, baseline], invocation)
    assert "Candidate:" in lines and "Cycle Count baseline:" in lines
    assert len([line for line in lines if line.startswith("pre_run_commands")]) == 2


def test_dependency_receipt_round_trips_same_and_cross_root_paths(tmp_path):
    from booley.flows.sim.campaign.dependency import (
        read_campaign_dependencies,
        register_campaign_dependency,
    )

    origin, digest = _origin(tmp_path)
    dependents = (tmp_path / "reports/sim/2", tmp_path / "resumed/sim/1")
    for dependent in dependents:
        dependent.mkdir(parents=True)
        register_campaign_dependency(
            origin,
            campaign_id=CAMPAIGN_ID,
            manifest_sha256=digest,
            dependent_invocation=dependent,
        )

    receipts = read_campaign_dependencies(origin)
    assert tuple(item.dependent_invocation for item in receipts) == tuple(
        sorted((path.absolute() for path in dependents), key=str)
    )
    assert {item.origin_invocation for item in receipts} == {tmp_path / "reports/sim/1"}
    assert {item.campaign_id for item in receipts} == {CAMPAIGN_ID}
    assert all(item.authoritative for item in receipts)
    assert {item.manifest_sha256 for item in receipts} == {digest}
    for receipt in origin.parent.glob("dependency-receipts/*.json"):
        assert set(json.loads(receipt.read_text())) == {
            "$schema",
            "campaign_id",
            "dependent_invocation",
            "dependent_invocation_id",
            "manifest_sha256",
            "origin_invocation",
            "origin_target",
        }


@pytest.mark.parametrize(
    ("campaign_id", "digest"),
    [
        ("not-a-uuid", "sha256:" + "0" * 64),
        (CAMPAIGN_ID, "sha256:not-a-digest"),
    ],
)
def test_dependency_receipt_rejects_invalid_identity(tmp_path, campaign_id, digest):
    from booley.flows.sim.campaign.dependency import register_campaign_dependency

    origin, _actual = _origin(tmp_path)
    dependent = tmp_path / "reports/sim/2"
    dependent.mkdir(parents=True)

    with pytest.raises(ValueError, match="invalid"):
        register_campaign_dependency(
            origin,
            campaign_id=campaign_id,
            manifest_sha256=digest,
            dependent_invocation=dependent,
        )


def test_dependency_receipt_rejects_changed_oversized_and_linked_files(tmp_path):
    from booley.flows.sim.campaign.dependency import (
        DEPENDENCY_MAX_BYTES,
        read_campaign_dependencies,
        register_campaign_dependency,
    )

    origin, digest = _origin(tmp_path)
    dependent = tmp_path / "reports/sim/2"
    dependent.mkdir(parents=True)
    receipt = register_campaign_dependency(
        origin,
        campaign_id=CAMPAIGN_ID,
        manifest_sha256=digest,
        dependent_invocation=dependent,
    )
    document = json.loads(receipt.read_text())
    document["dependent_invocation_id"] = True
    receipt.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="Malformed"):
        read_campaign_dependencies(origin)

    receipt.write_bytes(b"x" * (DEPENDENCY_MAX_BYTES + 1))
    with pytest.raises(ValueError, match="Unsafe"):
        read_campaign_dependencies(origin)

    receipt.unlink()
    target = tmp_path / "receipt.json"
    target.write_text("{}", encoding="utf-8")
    receipt.symlink_to(target)
    with pytest.raises(ValueError, match="Unsafe"):
        read_campaign_dependencies(origin)


def test_detached_dependency_receipt_is_inventory_but_not_deletion_authority(tmp_path):
    from booley.flows.sim.campaign.dependency import (
        dependency_receipt_files,
        read_campaign_dependencies,
        register_campaign_dependency,
    )

    origin, digest = _origin(tmp_path)
    dependent = tmp_path / "reports/sim/2"
    dependent.mkdir(parents=True)
    register_campaign_dependency(
        origin,
        campaign_id=CAMPAIGN_ID,
        manifest_sha256=digest,
        dependent_invocation=dependent,
    )
    copied = tmp_path / "copied/sim/1/targets/sim_0/campaign"
    shutil.copytree(origin.parent, copied)
    copied_manifest = copied / "manifest.json"

    receipts = read_campaign_dependencies(copied_manifest)
    assert len(receipts) == 1
    assert receipts[0].authoritative is False
    assert dependency_receipt_files(copied_manifest) == {receipts[0].path.absolute()}


def test_dependency_receipt_refuses_conflicting_existing_record(tmp_path):
    from booley.flows.sim.campaign.dependency import register_campaign_dependency

    origin, digest = _origin(tmp_path)
    dependent = tmp_path / "reports/sim/2"
    dependent.mkdir(parents=True)
    receipt = register_campaign_dependency(
        origin,
        campaign_id=CAMPAIGN_ID,
        manifest_sha256=digest,
        dependent_invocation=dependent,
    )
    document = json.loads(receipt.read_text())
    document["manifest_sha256"] = "sha256:" + "f" * 64
    receipt.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="disagrees"):
        register_campaign_dependency(
            origin,
            campaign_id=CAMPAIGN_ID,
            manifest_sha256=digest,
            dependent_invocation=dependent,
        )


def test_dependency_registry_refuses_non_directory_and_excessive_entries(tmp_path, monkeypatch):
    from pathlib import Path

    from booley.flows.sim.campaign.dependency import read_campaign_dependencies

    origin, _digest = _origin(tmp_path)
    registry = origin.parent / "dependency-receipts"
    registry.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ValueError, match="unsafe"):
        read_campaign_dependencies(origin)

    registry.unlink()
    registry.mkdir()
    original = Path.iterdir

    def excessive(path):
        if path == registry:
            return iter(registry / f"{index}.json" for index in range(1001))
        return original(path)

    monkeypatch.setattr(Path, "iterdir", excessive)
    with pytest.raises(ValueError, match="too large"):
        read_campaign_dependencies(origin)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("$schema", "wrong", "Malformed"),
        ("dependent_invocation_id", 3, "Malformed"),
        ("origin_invocation", 7, "invalid"),
        ("origin_invocation", "relative/path", "not canonical"),
    ],
)
def test_dependency_receipt_rejects_malformed_fields(tmp_path, field, value, message):
    from booley.flows.sim.campaign.dependency import (
        read_campaign_dependencies,
        register_campaign_dependency,
    )

    origin, digest = _origin(tmp_path)
    dependent = tmp_path / "reports/sim/2"
    dependent.mkdir(parents=True)
    receipt = register_campaign_dependency(
        origin,
        campaign_id=CAMPAIGN_ID,
        manifest_sha256=digest,
        dependent_invocation=dependent,
    )
    document = json.loads(receipt.read_text())
    document[field] = value
    receipt.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        read_campaign_dependencies(origin)


@pytest.mark.parametrize(
    "manifest_document",
    [[], {"campaign_id": "fedcba98-7654-4def-8123-456789abcdef"}],
)
def test_dependency_receipt_reauthenticates_origin_manifest(tmp_path, manifest_document):
    from booley.flows.sim.campaign.dependency import (
        read_campaign_dependencies,
        register_campaign_dependency,
    )

    origin, digest = _origin(tmp_path)
    dependent = tmp_path / "reports/sim/2"
    dependent.mkdir(parents=True)
    register_campaign_dependency(
        origin,
        campaign_id=CAMPAIGN_ID,
        manifest_sha256=digest,
        dependent_invocation=dependent,
    )
    origin.write_text(json.dumps(manifest_document), encoding="utf-8")

    with pytest.raises(ValueError, match="origin"):
        read_campaign_dependencies(origin)


def test_dependency_registration_requires_exact_paths(tmp_path):
    from booley.flows.sim.campaign.dependency import register_campaign_dependency

    origin, digest = _origin(tmp_path)
    misplaced = tmp_path / "manifest.json"
    misplaced.write_bytes(origin.read_bytes())
    with pytest.raises(ValueError, match="exact"):
        register_campaign_dependency(
            misplaced,
            campaign_id=CAMPAIGN_ID,
            manifest_sha256=digest,
            dependent_invocation=tmp_path / "reports/sim/2",
        )

    dependent = tmp_path / "reports/sim/zero"
    dependent.mkdir(parents=True)
    with pytest.raises(ValueError, match="positive numeric"):
        register_campaign_dependency(
            origin,
            campaign_id=CAMPAIGN_ID,
            manifest_sha256=digest,
            dependent_invocation=dependent,
        )
