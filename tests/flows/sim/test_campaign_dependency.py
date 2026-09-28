"""Origin-owned reverse dependency receipts for Simulation Campaign resumes."""

import hashlib
import json


def test_dependency_receipt_round_trips_same_and_cross_root_paths(tmp_path):
    from booley.flows.sim.campaign.dependency import (
        read_campaign_dependencies,
        register_campaign_dependency,
    )

    origin = tmp_path / "reports/sim/1/targets/sim_0/campaign/manifest.json"
    origin.parent.mkdir(parents=True)
    origin.write_text("{}\n", encoding="utf-8")
    digest = "sha256:" + hashlib.sha256(origin.read_bytes()).hexdigest()
    campaign_id = "01234567-89ab-4def-8123-456789abcdef"
    dependents = (tmp_path / "reports/sim/2", tmp_path / "resumed/sim/1")
    for dependent in dependents:
        dependent.mkdir(parents=True)
        register_campaign_dependency(
            origin,
            campaign_id=campaign_id,
            manifest_sha256=digest,
            dependent_invocation=dependent,
        )

    receipts = read_campaign_dependencies(origin)
    assert tuple(item.dependent_invocation for item in receipts) == tuple(
        sorted((path.absolute() for path in dependents), key=str)
    )
    assert {item.origin_invocation for item in receipts} == {tmp_path / "reports/sim/1"}
    assert {item.campaign_id for item in receipts} == {campaign_id}
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
