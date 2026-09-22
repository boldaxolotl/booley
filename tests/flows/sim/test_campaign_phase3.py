"""Shared Simulator Bundle and attempt-isolation contracts."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.flows.endpoint_admission import AdmissionContext
from booley.flows.sim.campaign.codec import canonical_json_bytes
from booley.flows.sim.campaign.coordinator import (
    CampaignPolicy,
    NewCampaignRunRequest,
    SimulationCampaign,
)
from booley.flows.sim.campaign.model import create_simulation_campaign_plan
from booley.flows.sim.campaign.planning import finalize_manifest
from booley.flows.sim.campaign.serial_execution import OrdinaryHdlSerialExecutor
from booley.flows.sim.campaign.store import CampaignStore
from booley.flows.sim.execution.contract import SimulationTargetOutcome, SimulationTestOutcome
from tests.flows.sim.test_campaign_manifest_codec import _manifest, _sha


def _admission() -> AdmissionContext:
    return AdmissionContext("unmanaged", None, None, 1, "interactive", "", None, lambda: False)


def _build_execution() -> dict[str, object]:
    return {
        "$schema": "booley.simulation-build-execution/v1",
        "process": {
            "returncode": 0,
            "stdout": "compile\n",
            "stderr": "",
            "timed_out": False,
            "duration_s": 0.1,
            "dispatched_unix": 1.0,
            "peak_rss_mb": None,
            "oom_kill_delta": 0,
        },
        "build": {
            "ran": True,
            "verdict": "pass",
            "failure_kind": None,
            "elapsed_s": 0.1,
            "output": "compile\n",
            "returncode": 0,
            "timed_out": False,
            "peak_rss_mb": None,
            "oom_kill_delta": 0,
            "terminal_record": True,
            "reason": "",
            "cache_decision": "",
        },
    }


def _two_item_manifest() -> object:
    document = _manifest()
    document.pop("fingerprints")
    document["workload"]["run_cwd"] = {  # type: ignore[index]
        "configured": "runs/{test}/{attempt}",
        "kind": "templated",
        "placeholders": ["test", "attempt"],
    }
    runtime_identity = {
        "source_artifact_path": "input.bin",
        "destination": "inputs/input.bin",
    }
    document["workload"]["runtime_inputs"] = [  # type: ignore[index]
        {
            "declaration_id": "sha256:"
            + hashlib.sha256(canonical_json_bytes(runtime_identity).rstrip(b"\n")).hexdigest(),
            **runtime_identity,
        }
    ]
    suite_raw = b"[test.alpha]\n[test.beta]\n"
    document["required_suite"] = {
        "names": ["alpha", "beta"],
        "default_invocation": False,
        "source_path": "tests.toml",
        "source_bytes": len(suite_raw),
        "source_sha256": "sha256:" + hashlib.sha256(suite_raw).hexdigest(),
    }
    target = document["target"]
    variant = document["build_variants"][0]  # type: ignore[index]
    document["work_items"] = [
        _work_item(ordinal, name, target, variant["build_variant_id"])
        for ordinal, name in enumerate(("alpha", "beta"))
    ]
    return finalize_manifest(document)


def _work_item(ordinal: int, name: str, target: object, variant_id: object) -> dict:
    identity = {
        "ordinal": ordinal,
        "kind": "ordinary_hdl",
        "role": "candidate",
        "revision": "abc123",
        "target": target,
        "selection": {"kind": "named", "names": [name]},
        "arguments": [name],
        "build_variant_id": variant_id,
        "run_directory": {
            "configured": "runs/{test}/{attempt}",
            "kind": "templated",
            "collision_template": "runs/test/attempt",
        },
    }
    fingerprint = _sha(identity)
    return {
        "work_item_id": f"item:{ordinal:04d}:" + fingerprint.removeprefix("sha256:")[:16],
        **identity,
        "fingerprint_sha256": fingerprint,
    }


def test_shareable_variant_compiles_once_and_isolates_attempt_runtime_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _two_item_manifest()
    plan = create_simulation_campaign_plan(manifest)  # type: ignore[arg-type]
    project = tmp_path / "project"
    project.mkdir()
    build_root = tmp_path / "engine-build"
    build_root.mkdir()
    (build_root / "simv").write_bytes(b"image")
    (build_root / "input.bin").write_bytes(b"pristine")
    run_log = build_root / "run.log"
    run_log.write_text("PASS\n", encoding="utf-8")
    compile_count = [0]
    launches: list[tuple[str, Path, bytes]] = []

    handle = SimpleNamespace(
        identity="acme:lib:dut:1#sim",
        project_root=project,
        selector="sim",
        eda_tool="icarus",
    )
    monkeypatch.setattr(
        "booley.flows.sim.campaign.serial_execution.TargetCatalog.build",
        lambda _root: SimpleNamespace(select=lambda *_args, **_kwargs: handle),
    )

    executor = _shared_executor(build_root, run_log, handle, launches, compile_count)
    invocation = tmp_path / "reports" / "000001"
    invocation.mkdir(parents=True)
    outcome = SimulationCampaign(executor).run(
        NewCampaignRunRequest(
            plan, project, invocation.parent, CampaignPolicy(), invocation, _admission()
        )
    )
    _assert_shared_campaign(outcome, compile_count[0], launches, invocation)


def _shared_executor(build_root, run_log, handle, launches, compile_count):
    class FakeGroup:
        def __init__(self, names: tuple[str, ...]) -> None:
            self.names = names
            self.build_root = build_root
            self.artifact_paths = (build_root / "simv",)
            self.reused = False

        def planning_disclosure(self):
            return {}

        def compile(self):
            compile_count[0] += 1
            return SimpleNamespace(passed=True)

        def reuse_compilation_from(self, source) -> None:
            assert source.names == ("alpha",)
            self.reused = True

        def build_recovery_document(self):
            return _build_execution()

        def bind_authenticated_bundle(self, evidence) -> None:
            assert evidence == _build_execution()

        def launch_snapshot(self, snapshot_root: Path, run_cwd: Path):
            name = self.names[0]
            staged = run_cwd / "inputs/input.bin"
            launches.append((name, run_cwd, staged.read_bytes()))
            staged.write_bytes(name.encode())
            assert (snapshot_root / "simv").read_bytes() == b"image"
            return _shared_outcome(name, handle, run_log)

    class FakeExecution:
        @contextmanager
        def ordinary_group(self, _handle, names):
            yield FakeGroup(names)

    return OrdinaryHdlSerialExecutor(
        invoke=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
        execution_factory=lambda _options: FakeExecution(),  # type: ignore[arg-type,return-value]
    )


def _shared_outcome(name, handle, run_log):
    test = SimulationTestOutcome(name=name, verdict="pass", passed=True, run_log_path=str(run_log))
    return SimulationTargetOutcome(
        target="sim",
        target_identity=handle.identity,
        toplevel="tb",
        eda_tool="icarus",
        passed=True,
        verdict="pass",
        elapsed_s=0.1,
        tests=(test,),
    )


def _assert_shared_campaign(outcome, compile_count, launches, invocation) -> None:
    assert outcome.complete is True
    assert compile_count == 1
    assert [item[0] for item in launches] == ["alpha", "beta"]
    assert launches[0][1] != launches[1][1]
    assert [item[2] for item in launches] == [b"pristine", b"pristine"]
    assert all(not item[1].exists() for item in launches)
    store = CampaignStore(invocation / "targets/sim/campaign")
    build_results = tuple(store.root.glob("build-variants/*/attempts/*/build-result.json"))
    assert len(build_results) == 1
    result_documents = [
        json.loads((store.work_item_directory(item) / "result.json").read_text())
        for item in store.scan().complete
    ]
    assert {item["build_result"]["sha256"] for item in result_documents} == {
        "sha256:" + hashlib.sha256(build_results[0].read_bytes()).hexdigest()
    }
    assert {item["build_result"]["sharing"] for item in result_documents} == {"shared_variant"}
