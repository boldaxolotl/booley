"""Simulation records are named for the Simulation Record, and Booley 0.4.0's are refused.

Part 3 of ADR 0090 renames the Simulation Record manifest, attempt result, dependency
receipt, supervised child context and terminal causes. Every 0.4.0 record is read from
committed bytes: ``tests/flows/fixtures/booley_0_4_0/{complete,interrupted,coverage}``
were written by Booley 0.4.0's own ``SimulateFlow``, and ``resumed/`` holds the one
document those captures lack, a dependency receipt. It was captured the same way: the
tag's ``tests/flows/sim/test_campaign_resume_diagnostics.py::_interrupted_campaign`` helper
and fake simulator ran an interrupted run, a source change made the tag's
``SimulateFlow().execute(SimRequest(resume_from=<manifest>))`` refuse the resume, and that
refusal registered the receipt under the origin's ``campaign/dependency-receipts/``. The
receipt alone is committed, with the capture's temporary root replaced by
``/booley-0.4.0-capture``.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import uuid
from pathlib import Path
from typing import Any

import pytest

from booley.flows.builtin_cli import parse_request
from booley.flows.sim.campaign import inspect_retained_campaign, read_durable_results
from booley.flows.sim.campaign.codec import (
    SimulationCampaignIntegrityError,
    canonical_json_bytes,
    decode_bundle_build_attempt,
    decode_bundle_build_result,
    decode_executable_snapshot,
    decode_simulation_attempt,
    decode_simulation_campaign_manifest,
    decode_simulation_result,
    decode_simulator_bundle,
)
from booley.flows.sim.campaign.dependency import (
    read_campaign_dependencies,
    register_campaign_dependency,
)
from booley.flows.sim.campaign_retention import CampaignRetentionError, prune_invocation
from booley.flows.sim.flow import SimulateFlow
from booley.runtime import execution_records
from booley.runtime.execution_records import (
    ExecutionId,
    child_context_matches,
    execution_paths,
    read_json,
)
from booley.runtime.execution_recovery import recover_execution
from tests.flows.sim import test_campaign_lane_pool as lane_pool
from tests.flows.sim.campaign_test_support import _retained_invocation
from tests.flows.sim.test_campaign_dependency import CAMPAIGN_ID, _origin, _two_role_campaigns
from tests.flows.sim.test_campaign_unsupported_formats import (
    _assert_refused,
    _assert_unsupported_type,
    copy_older_invocation,
)
from tests.flows.sim.test_campaign_unsupported_formats import (
    reports as _reports_fixture,
)

# The report-root fixture of the Part 1 tests, collected here under the same name.
reports = _reports_fixture

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "booley_0_4_0"
_RECEIPT_DIRECTORY = (
    _FIXTURES / "resumed" / "sim" / "1" / "targets" / "sim_0" / "campaign" / "dependency-receipts"
)

# Every schema Booley 0.4.0 wrote for the records this part renames, with the decoder
# that must refuse it.
_OLD_MANIFEST = "booley.simulation-campaign-manifest/v4"
_OLD_RESULT = "booley.simulation-result/v3"
_OLD_RECEIPT = "booley.simulation-campaign-dependency/v1"
_OLD_RECORDS = {
    _OLD_MANIFEST: decode_simulation_campaign_manifest,
    _OLD_RESULT: decode_simulation_result,
    "booley.simulation-attempt/v1": decode_simulation_attempt,
    "booley.bundle-build-attempt/v1": decode_bundle_build_attempt,
    "booley.bundle-build-result/v1": decode_bundle_build_result,
    "booley.simulator-bundle/v1": decode_simulator_bundle,
    "booley.executable-snapshot/v1": decode_executable_snapshot,
}


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_bytes())


def _at_record_layout(invocation: Path) -> None:
    """Rename a 0.4.0 run's directories to the Record layout, so only a schema can refuse it."""
    for campaign in sorted(invocation.glob("targets/*/campaign")):
        campaign.rename(campaign.with_name("record"))
    for coverage in sorted(invocation.rglob("coverage-campaign")):
        coverage.rename(coverage.with_name("coverage-record"))


def _tree(root: Path) -> dict[str, bytes]:
    """Every file under *root* by relative path, to show a refusal changed nothing."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _rewrite(path: Path, raw: bytes) -> None:
    path.chmod(0o600)
    path.write_bytes(raw)


def _0_4_0_receipt() -> Path:
    (receipt,) = _RECEIPT_DIRECTORY.glob("*.json")
    return receipt


# --- AC1: the names a run writes -----------------------------------------------------------


def test_a_run_writes_simulation_record_names(tmp_path: Path) -> None:
    _reports, _project_data, store = _retained_invocation(tmp_path)

    manifest = _json(store.manifest_path)
    assert manifest["$schema"] == "booley.simulation-record-manifest/v1"
    record_id = manifest["simulation_record_id"]
    assert uuid.UUID(record_id).version == 4
    # Goal Records own the bare ``record_id``, so it must never name a Simulation Record.
    assert "campaign_id" not in manifest
    assert "record_id" not in manifest

    schemas = {
        "work-items/*/result.json": "booley.simulation-result/v4",
        "work-items/*/attempts/*/attempt.json": "booley.simulation-attempt/v2",
        "build-variants/*/attempts/*/build-attempt.json": "booley.bundle-build-attempt/v2",
        "build-variants/*/attempts/*/build-result.json": "booley.bundle-build-result/v2",
        "build-variants/*/attempts/*/evidence/bundle.json": "booley.simulator-bundle/v2",
        "work-items/*/attempts/*/snapshot/snapshot.json": "booley.executable-snapshot/v2",
    }
    for pattern, schema in schemas.items():
        documents = sorted(store.root.glob(pattern))
        assert documents, pattern
        for path in documents:
            document = _json(path)
            assert document["$schema"] == schema, path
            assert document["simulation_record_id"] == record_id, path
            assert "campaign_id" not in document, path
            assert "record_id" not in document, path


def test_prerequisites_and_dependency_receipts_name_the_simulation_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _invocation, candidate, baseline = _two_role_campaigns(tmp_path, monkeypatch)
    baseline_id = _json(baseline.manifest_path)["simulation_record_id"]
    prerequisites = _json(candidate.manifest_path)["prerequisites"]
    assert prerequisites
    for entry in prerequisites:
        assert entry["simulation_record_id"] == baseline_id
        assert "campaign_id" not in entry

    origin, digest = _origin(tmp_path / "origin")
    dependent = tmp_path / "origin" / "reports" / "sim" / "2"
    dependent.mkdir(parents=True)
    register_campaign_dependency(
        origin,
        campaign_id=CAMPAIGN_ID,
        manifest_sha256=digest,
        dependent_invocation=dependent,
    )

    (receipt_path,) = origin.parent.glob("dependency-receipts/*.json")
    receipt = _json(receipt_path)
    assert receipt["$schema"] == "booley.simulation-record-dependency/v1"
    assert receipt["simulation_record_id"] == CAMPAIGN_ID
    assert "campaign_id" not in receipt
    assert "record_id" not in receipt
    (read_back,) = read_campaign_dependencies(origin)
    assert read_back.campaign_id == CAMPAIGN_ID


def _prepared_lane(campaign: lane_pool._Campaign, parent: str) -> tuple[ExecutionId, Any]:
    """Prepare one child lane the way the scheduler does, and return it with its paths."""
    child = ExecutionId(uuid.uuid4().hex)
    campaign.registry.prepare(
        child,
        work_item_id=campaign.items[0]["work_item_id"],
        attempt_id=str(uuid.uuid4()),
        attempt_ordinal=1,
        attempt_directory=campaign.store.root / "attempt",
        parent_execution_id=ExecutionId(parent),
    )
    return child, execution_paths(child, booley_dir=campaign.registry.project_data)


def test_a_child_lane_writes_the_record_context_and_terminal_causes(tmp_path: Path) -> None:
    campaign = lane_pool._Campaign(tmp_path, ("alpha",), max_heavy=1)
    try:
        child, paths = _prepared_lane(campaign, "a" * 32)

        context = read_json(paths.context)
        assert context is not None
        assert context["$schema"] == "booley.supervised-child-context/v2"
        assert (
            context["simulation_record_id"] == campaign.manifest.document["simulation_record_id"]
        )
        assert "campaign_id" not in context
        assert "record_id" not in context
        assert child_context_matches(paths, child)

        # A lane's cancellation request names the Record. The terminal cause
        # ``record_error`` is pinned by the parametrized lane-pool test.
        cancelled, cancelled_paths = _prepared_lane(campaign, "a" * 32)
        assert campaign.registry.cancel(cancelled)
        assert read_json(cancelled_paths.cancel)["reason"] == "record_cancelled"
    finally:
        campaign.slots.release(campaign.outer)


# --- AC2: every 0.4.0 record is refused as older -------------------------------------------


def test_a_0_4_0_manifest_is_refused_naming_its_invocation(reports: Path) -> None:
    invocation = copy_older_invocation("0.4.0", "complete", reports)
    _at_record_layout(invocation)
    manifest = invocation / "targets" / "sim" / "record" / "manifest.json"

    with pytest.raises(ValueError) as decoded:
        decode_simulation_campaign_manifest(manifest.read_bytes())
    _assert_unsupported_type(decoded.value)
    _assert_refused(str(decoded.value), _OLD_MANIFEST)

    # Retention, Goal status and the Dashboard each read it through one of these.
    for read in (
        lambda: inspect_retained_campaign(manifest),
        lambda: read_durable_results(manifest, "sha256:" + "0" * 64),
    ):
        with pytest.raises(ValueError) as refused:
            read()
        _assert_unsupported_type(refused.value)
        _assert_refused(str(refused.value), _OLD_MANIFEST, invocation)

    with pytest.raises(CampaignRetentionError) as pruned:
        prune_invocation(reports, 1)
    _assert_refused(str(pruned.value), _OLD_MANIFEST, invocation)


def test_a_0_4_0_attempt_result_is_refused_naming_its_invocation(tmp_path: Path) -> None:
    reports_root, _project_data, store = _retained_invocation(tmp_path)
    (old_result,) = _FIXTURES.glob("complete/sim/1/targets/sim/campaign/work-items/*/result.json")
    result_path = next(store.root.glob("work-items/*/result.json"))
    _rewrite(result_path, old_result.read_bytes())

    with pytest.raises(ValueError) as excinfo:
        store.scan()

    _assert_unsupported_type(excinfo.value)
    _assert_refused(str(excinfo.value), _OLD_RESULT, reports_root / "sim/1")


def test_a_0_4_0_dependency_receipt_is_refused(tmp_path: Path) -> None:
    reports_root, _project_data, store = _retained_invocation(tmp_path)
    receipts = store.root / "dependency-receipts"
    receipts.mkdir()
    shutil.copyfile(_0_4_0_receipt(), receipts / _0_4_0_receipt().name)

    with pytest.raises(ValueError) as read:
        read_campaign_dependencies(store.manifest_path)
    _assert_unsupported_type(read.value)
    _assert_refused(str(read.value), _OLD_RECEIPT)

    # Pruning with dependents reads the registry: an older record is not a corrupt one.
    invocation = reports_root / "sim" / "1"
    before = _tree(invocation)
    with pytest.raises(CampaignRetentionError) as pruned:
        prune_invocation(reports_root, 1, include_dependents=True)
    _assert_refused(str(pruned.value), _OLD_RECEIPT, reports_root / "sim/1")
    assert "dependency registry is invalid" not in str(pruned.value)
    assert _tree(invocation) == before


def test_every_0_4_0_record_of_a_complete_run_is_refused(reports: Path) -> None:
    invocation = copy_older_invocation("0.4.0", "complete", reports)
    seen: set[str] = set()
    for path in sorted(invocation.rglob("*.json")):
        schema = _json(path).get("$schema")
        decode = _OLD_RECORDS.get(schema)
        if decode is None:
            continue
        seen.add(schema)
        # Decoder level: a sibling record is reached only after its manifest, which is
        # refused first, so this proves the decoder never reports it as corrupt.
        with pytest.raises(ValueError) as excinfo:
            decode(path.read_bytes())
        _assert_unsupported_type(excinfo.value)
        _assert_refused(str(excinfo.value), schema)
    assert seen == set(_OLD_RECORDS)


# --- AC3: --resume-from a 0.4.0 manifest at the Record path --------------------------------


def test_resume_from_a_0_4_0_manifest_at_the_record_path_is_refused_before_any_work(
    tmp_path: Path, reports: Path
) -> None:
    for kind in ("interrupted", "coverage"):
        shutil.rmtree(reports / "sim", ignore_errors=True)
        invocation = copy_older_invocation("0.4.0", kind, reports)
        _at_record_layout(invocation)
        (manifest,) = invocation.glob("targets/*/record/manifest.json")
        before = _tree(invocation)

        request = parse_request(SimulateFlow(), ["--resume-from", str(manifest)])
        result = SimulateFlow().execute(
            dataclasses.replace(request, work_dir=tmp_path, report_dir=reports)
        )

        assert result.exit_code == 2, kind
        _assert_refused(result.outcome.report_text, _OLD_MANIFEST, invocation)
        assert not (reports / "sim" / "2").exists()
        assert _tree(invocation) == before


# --- AC4: no old name is an alias ------------------------------------------------------------


def _with_old_key(document: dict[str, Any]) -> bytes:
    document = dict(document)
    document["campaign_id"] = document.pop("simulation_record_id", document.get("campaign_id"))
    return canonical_json_bytes(document)


def test_old_simulation_record_names_are_not_aliases(tmp_path: Path) -> None:
    _reports, _project_data, store = _retained_invocation(tmp_path)
    documents = {
        decode_simulation_campaign_manifest: store.manifest_path,
        decode_simulation_result: next(store.root.glob("work-items/*/result.json")),
        decode_simulation_attempt: next(store.root.glob("work-items/*/attempts/*/attempt.json")),
    }
    for decode, path in documents.items():
        # The old key is an unknown field: an integrity error, not an older Booley's record.
        with pytest.raises(SimulationCampaignIntegrityError, match="exact fields") as refused:
            decode(_with_old_key(_json(path)))
        assert "older Booley" not in str(refused.value)

    # The current receipt schema with the old key is malformed.
    origin, digest = _origin(tmp_path / "origin")
    dependent = tmp_path / "origin" / "reports" / "sim" / "2"
    dependent.mkdir(parents=True)
    register_campaign_dependency(
        origin, campaign_id=CAMPAIGN_ID, manifest_sha256=digest, dependent_invocation=dependent
    )
    (receipt_path,) = origin.parent.glob("dependency-receipts/*.json")
    _rewrite(receipt_path, _with_old_key(_json(receipt_path)))
    with pytest.raises(ValueError, match="Malformed Simulation Campaign dependency receipt"):
        read_campaign_dependencies(origin)

    # A context sidecar is the current one only: schema v1, or v2 with the old key, is not.
    campaign = lane_pool._Campaign(tmp_path / "lane", ("alpha",), max_heavy=1)
    try:
        child, paths = _prepared_lane(campaign, "a" * 32)
        context = _json(paths.context)
        for altered in (
            {**context, "$schema": "booley.supervised-child-context/v1"},
            json.loads(_with_old_key(context)),
        ):
            _rewrite(paths.context, canonical_json_bytes(altered))
            assert not child_context_matches(paths, child)
    finally:
        campaign.slots.release(campaign.outer)


def test_a_lane_a_0_4_0_booley_left_is_still_recovered(tmp_path: Path) -> None:
    """Only recovery reads the 0.4.0 sidecar, so its heavy job slot is released."""
    campaign = lane_pool._Campaign(tmp_path, ("alpha",), max_heavy=1)
    booley_dir = campaign.registry.project_data
    try:
        child, paths = _prepared_lane(campaign, "a" * 32)
        other, other_paths = _prepared_lane(campaign, "a" * 32)
        # The sidecar Booley 0.4.0 wrote: schema v1 and ``campaign_id``.
        older = {
            "$schema": "booley.supervised-child-context/v1",
            "child_execution_id": child,
            "parent_execution_id": "a" * 32,
            "campaign_id": str(uuid.uuid4()),
            "manifest_sha256": "sha256:" + "1" * 64,
            "work_item_id": campaign.items[0]["work_item_id"],
            "attempt_id": str(uuid.uuid4()),
            "attempt_ordinal": 1,
        }
        _rewrite(paths.context, canonical_json_bytes(older))
        # The same 0.4.0 sidecar naming another execution is not this execution's.
        _rewrite(other_paths.context, canonical_json_bytes({**older, "child_execution_id": child}))

        assert not child_context_matches(paths, child)
        assert execution_records.is_older_child_context(paths, child)
        assert not execution_records.is_older_child_context(other_paths, other)
        assert recover_execution(child, booley_dir=booley_dir)
        assert read_json(paths.record)["tree_terminal"] is True
        assert not recover_execution(other, booley_dir=booley_dir)
        assert read_json(other_paths.record)["state"] == "waiting"
    finally:
        campaign.slots.release(campaign.outer)
