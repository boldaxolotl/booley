"""A missing literal ``[flows.sim].run_cwd`` must fail before any build (issue #881)."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.flows.sim.campaign import serial_execution
from booley.flows.sim.campaign.codec import (
    SimulationCampaignIntegrityError,
    encode_simulation_campaign_manifest,
)
from booley.flows.sim.campaign.coordinator import (
    CampaignPolicy,
    NewCampaignRunRequest,
    SimulationCampaign,
)
from booley.flows.sim.campaign.model import create_simulation_campaign_plan
from booley.flows.sim.campaign.planning import finalize_manifest, manifest_digest
from booley.flows.sim.campaign.run_directory import RunDirectory, claimed_run_directory
from booley.flows.sim.campaign.store import CampaignStore
from booley.flows.sim.flow import SimulateFlow
from booley.flows.sim.verilator_coverage import SimulationBuildResult
from tests.flows.sim.test_campaign_manifest_codec import (
    _baseline_manifest,
    _manifest,
    _sha,
)
from tests.flows.sim.test_campaign_phase3_integrity import (
    _admission,
    _executor,
    _handle,
    _legacy_disclosures,
)
from tests.flows.sim.test_campaign_phase5_adversarial import _run_coverage_campaign
from tests.flows.sim.test_coverage_transaction import NativeExecution

_MISSING = "literal run directory must already exist"
_IDENTITY = {"campaign_id": "c", "work_item_id": "w", "attempt_id": "a"}


def literal_manifest(access: str, *, configured: str = "run"):
    """One-item ordinary HDL manifest whose run_cwd is the literal ``configured``."""
    return finalize_manifest(literal_document(access, configured=configured))


def literal_document(access: str, *, configured: str = "run") -> dict:
    """Unfinalized document behind :func:`literal_manifest`."""
    document = _manifest()
    document.pop("fingerprints")
    document["workload"]["pre_sim_build_access"] = access
    document["workload"]["run_cwd"] = {
        "configured": configured,
        "kind": "literal",
        "placeholders": [],
    }
    name = "alpha"
    suite_raw = f"[test.{name}]\n".encode()
    document["required_suite"] = {
        "names": [name],
        "default_invocation": False,
        "source_path": "tests.toml",
        "source_bytes": len(suite_raw),
        "source_sha256": "sha256:" + hashlib.sha256(suite_raw).hexdigest(),
    }
    variant = document["build_variants"][0]
    identity = {
        "ordinal": 0,
        "kind": "ordinary_hdl",
        "role": "candidate",
        "revision": "abc123",
        "target": document["target"],
        "selection": {"kind": "named", "names": [name]},
        "arguments": [name],
        "build_variant_id": variant["build_variant_id"],
        "run_directory": {
            "configured": configured,
            "kind": "literal",
            "collision_template": configured,
        },
    }
    fingerprint = _sha(identity)
    document["work_items"] = [
        {
            "work_item_id": "item:0000:" + fingerprint.removeprefix("sha256:")[:16],
            **identity,
            "fingerprint_sha256": fingerprint,
        }
    ]
    if access == "legacy-per-test":
        document["planning_disclosures"] = [_legacy_disclosures()[name]]
    return document


class _Harness:
    """One campaign run against a counting fake execution."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, access: str) -> None:
        self.tmp_path = tmp_path
        self.access = access
        self.project = tmp_path / "project"
        self.project.mkdir()
        build_root = tmp_path / "engine-build"
        build_root.mkdir()
        (build_root / "simv").write_bytes(b"image")
        self.counters = {"compile": 0, "durable_reuse": 0, "launch": 0}
        self.hooks: list[tuple[str, ...]] = []
        self.executor = _executor(build_root, self.counters, disclosures=_legacy_disclosures())
        monkeypatch.setattr(
            serial_execution,
            "_run_hook",
            lambda _handle, _root, names, *_args, **_kwargs: self.hooks.append(names),
        )
        monkeypatch.setattr(
            serial_execution.TargetCatalog,
            "build",
            lambda _root: SimpleNamespace(select=lambda *_a, **_k: _handle(self.project)),
        )
        self.invocation = tmp_path / "reports" / "000001"
        self.invocation.mkdir(parents=True)

    def run(self, manifest=None):
        plan = create_simulation_campaign_plan(manifest or literal_manifest(self.access))
        return SimulationCampaign(self.executor).run(
            NewCampaignRunRequest(
                plan,
                self.project,
                self.invocation.parent,
                CampaignPolicy(),
                self.invocation,
                _admission(),
            )
        )

    def assert_nothing_built_or_published(self) -> None:
        assert self.counters == {"compile": 0, "durable_reuse": 0, "launch": 0}
        root = self.invocation / "targets/sim/campaign"
        assert not tuple(root.glob("work-items/*/attempts/*"))
        assert not tuple(root.glob("build-variants/*/attempts/*"))


@pytest.fixture(params=["immutable", "legacy-per-test"])
def harness(request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Harness:
    return _Harness(tmp_path, monkeypatch, request.param)


def test_missing_literal_run_cwd_fails_before_any_build(harness: _Harness) -> None:
    with pytest.raises(SimulationCampaignIntegrityError, match=_MISSING) as caught:
        harness.run()

    message = str(caught.value)
    assert ".gitkeep" in message
    assert "{attempt}" in message
    harness.assert_nothing_built_or_published()


def test_linked_literal_run_cwd_fails_before_any_build(harness: _Harness) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks unavailable")
    real = harness.project / "real"
    real.mkdir()
    try:
        (harness.project / "run").symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")

    with pytest.raises(SimulationCampaignIntegrityError, match="not a link"):
        harness.run()

    harness.assert_nothing_built_or_published()


def test_existing_non_directory_literal_run_cwd_fails_before_any_build(
    harness: _Harness,
) -> None:
    (harness.project / "run").write_text("not a directory", encoding="utf-8")

    with pytest.raises(SimulationCampaignIntegrityError, match="exists but is not a directory"):
        harness.run()

    harness.assert_nothing_built_or_published()


def test_existing_literal_run_cwd_still_runs(harness: _Harness) -> None:
    (harness.project / "run").mkdir()

    outcome = harness.run()

    assert outcome.complete is True
    assert harness.counters["compile"] == 1
    assert harness.counters["launch"] == 1


def test_templated_run_cwd_is_not_preflighted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.flows.sim.test_campaign_phase3_integrity import _manifest_for

    harness = _Harness(tmp_path, monkeypatch, "immutable")

    outcome = harness.run(_manifest_for(("alpha",)))

    assert outcome.complete is True
    assert harness.counters["compile"] == 1


def test_complete_campaign_rerun_does_not_require_literal_run_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path, monkeypatch, "immutable")
    run_dir = harness.project / "run"
    run_dir.mkdir()
    assert harness.run().complete is True
    run_dir.rmdir()

    assert harness.run().complete is True
    assert harness.counters["compile"] == 1


def test_legacy_hook_never_runs_when_literal_run_cwd_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _Harness(tmp_path, monkeypatch, "legacy-per-test")

    with pytest.raises(SimulationCampaignIntegrityError, match=_MISSING):
        harness.run()

    assert harness.hooks == []
    assert harness.counters["compile"] == 0


def test_claim_rejects_literal_removed_after_preflight(tmp_path: Path) -> None:
    missing = tmp_path / "gone"
    run = RunDirectory(missing, str(missing), False, tmp_path / "locks" / "gone.lock")

    with (
        pytest.raises(SimulationCampaignIntegrityError, match=_MISSING) as caught,
        claimed_run_directory(run, identity=_IDENTITY),
    ):
        pass

    assert ".gitkeep" in str(caught.value)


def test_missing_literal_run_cwd_blocks_baseline_build_across_the_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A candidate's missing directory is found before the baseline builds or anything publishes."""
    harness = _Harness(tmp_path, monkeypatch, "immutable")
    baseline_root = tmp_path / "baseline-worktree"
    (baseline_root / "run").mkdir(parents=True)

    def request(manifest, root: Path) -> NewCampaignRunRequest:
        return NewCampaignRunRequest(
            create_simulation_campaign_plan(manifest),
            root,
            harness.invocation.parent,
            CampaignPolicy(),
            harness.invocation,
            _admission(),
        )

    baseline = request(_baseline_manifest(), baseline_root)
    candidate = request(literal_manifest("immutable"), harness.project)

    with pytest.raises(SimulationCampaignIntegrityError, match=_MISSING):
        SimulateFlow._publish_and_run_campaign_requests(
            SimulationCampaign(harness.executor), [baseline], [candidate]
        )

    assert harness.counters["compile"] == 0
    assert not tuple(harness.invocation.glob("targets/*/campaign/*manifest*"))


def test_owner_missing_run_cwd_fails_before_prerequisite_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The baseline's own directory exists, so only the owner check can stop its build."""
    harness = _Harness(tmp_path, monkeypatch, "immutable")
    baseline = _baseline_manifest()  # literal "run"
    (harness.project / "run").mkdir()
    store = CampaignStore(harness.invocation / "targets" / "base-revision" / "campaign")
    store.publish_manifest(baseline)
    raw = encode_simulation_campaign_manifest(baseline)
    baseline_json = json.loads(raw)
    owner_document = literal_document("immutable", configured="owner-run")
    owner_document.pop("fingerprints", None)
    owner_document["prerequisites"] = [
        {
            "role": "cycle_count_baseline",
            "manifest": {
                "path_base": "origin_invocation",
                "path": store.manifest_path.relative_to(harness.invocation).as_posix(),
                "bytes": len(raw),
                "sha256": manifest_digest(baseline),
                "kind": "simulation_campaign_manifest",
                "owner": baseline_json["campaign_id"],
            },
            "campaign_id": baseline_json["campaign_id"],
            "target": baseline_json["target"],
            "required_observation": "cycle_count",
            "work_item_id": baseline_json["work_items"][0]["work_item_id"],
        }
    ]

    with pytest.raises(SimulationCampaignIntegrityError, match=_MISSING) as caught:
        harness.run(finalize_manifest(owner_document))

    assert "owner-run" in str(caught.value)
    assert harness.counters["compile"] == 0


def test_missing_literal_run_cwd_fails_coverage_before_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "booley.flows.sim.campaign.flow_planning.resolve_run_cwd", lambda _root: "missing-run"
    )
    builds: list[object] = []

    class CountingNative(NativeExecution):
        def build(self, request):
            builds.append(request)
            return SimulationBuildResult(True)

    with pytest.raises(SimulationCampaignIntegrityError, match=_MISSING):
        _run_coverage_campaign(tmp_path, CountingNative())

    store = tmp_path / "reports/1/targets/sim_0/campaign"
    assert builds == []
    assert not tuple(store.glob("work-items/*/attempts/*"))
