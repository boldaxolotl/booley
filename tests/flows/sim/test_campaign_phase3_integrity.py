"""Independent Phase 3 build-scope and executable-integrity regressions."""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.flows.base import DEFAULT_TIMEOUT_S
from booley.flows.endpoint_admission import AdmissionContext
from booley.flows.sim.build_session import TargetCompileSurface
from booley.flows.sim.campaign import serial_execution
from booley.flows.sim.campaign.codec import (
    SimulationCampaignIntegrityError,
    canonical_json_bytes,
)
from booley.flows.sim.campaign.coordinator import (
    CampaignPolicy,
    NewCampaignRunRequest,
    SimulationCampaign,
    WorkExecutionRequest,
)
from booley.flows.sim.campaign.model import create_simulation_campaign_plan
from booley.flows.sim.campaign.planning import finalize_manifest
from booley.flows.sim.campaign.serial_execution import OrdinaryHdlSerialExecutor
from booley.flows.sim.campaign.store import CampaignStore
from booley.flows.sim.execution.contract import (
    SimulationOptions,
    SimulationTargetOutcome,
    SimulationTestOutcome,
)
from tests.flows.sim.test_campaign_manifest_codec import _manifest, _sha


def _admission() -> AdmissionContext:
    return AdmissionContext("unmanaged", None, None, 1, "interactive", "", None, lambda: False)


def _manifest_for(names: tuple[str, ...], *, access: str = "immutable"):
    document = _manifest()
    document.pop("fingerprints")
    document["workload"]["pre_sim_build_access"] = access  # type: ignore[index]
    document["workload"]["run_cwd"] = {  # type: ignore[index]
        "configured": "runs/{test}/{attempt}",
        "kind": "templated",
        "placeholders": ["test", "attempt"],
    }
    suite_raw = "".join(f"[test.{name}]\n" for name in names).encode()
    document["required_suite"] = {
        "names": list(names),
        "default_invocation": False,
        "source_path": "tests.toml",
        "source_bytes": len(suite_raw),
        "source_sha256": "sha256:" + hashlib.sha256(suite_raw).hexdigest(),
    }
    target = document["target"]
    variant = document["build_variants"][0]  # type: ignore[index]
    items = []
    for ordinal, name in enumerate(names):
        identity = {
            "ordinal": ordinal,
            "kind": "ordinary_hdl",
            "role": "candidate",
            "revision": "abc123",
            "target": target,
            "selection": {"kind": "named", "names": [name]},
            "arguments": [name],
            "build_variant_id": variant["build_variant_id"],
            "run_directory": {
                "configured": "runs/{test}/{attempt}",
                "kind": "templated",
                "collision_template": "runs/test/attempt",
            },
        }
        fingerprint = _sha(identity)
        items.append(
            {
                "work_item_id": f"item:{ordinal:04d}:" + fingerprint.removeprefix("sha256:")[:16],
                **identity,
                "fingerprint_sha256": fingerprint,
            }
        )
    document["work_items"] = items
    return finalize_manifest(document)


def _handle(project: Path) -> SimpleNamespace:
    return SimpleNamespace(
        identity="acme:lib:dut:1#sim",
        project_root=project,
        selector="sim",
        eda_tool="icarus",
    )


def test_campaign_pre_sim_commands_have_an_independent_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def capture(*_args: object, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(serial_execution, "run_pre_sim_commands", capture)
    monkeypatch.setattr(serial_execution, "simulation_target_environment", lambda _handle: {})

    serial_execution._run_hook(
        SimpleNamespace(eda_tool="icarus"),
        tmp_path,
        ("smoke",),
        SimulationOptions(timeout_ms=1000),
        None,
        True,
    )

    assert captured["timeout_s"] == DEFAULT_TIMEOUT_S


class _Group:
    def __init__(
        self,
        names: tuple[str, ...],
        build_root: Path,
        counters: dict[str, int],
        *,
        mutate_snapshot: bool = False,
        disclosure: object | None = None,
    ) -> None:
        self.names = names
        self.build_root = build_root
        self.compile_surface = TargetCompileSurface(build_root.parent, (), ())
        self.artifact_paths = (build_root / "simv",)
        self._counters = counters
        self._mutate_snapshot = mutate_snapshot
        self._disclosure = disclosure or {}

    def planning_disclosure(self):
        return self._disclosure

    def compile(self):
        self._counters["compile"] += 1
        return SimpleNamespace(passed=True)

    def reuse_compilation_from(self, _source: object) -> None:
        self._counters["memory_reuse"] += 1

    def build_recovery_document(self):
        return _build_execution()

    def bind_authenticated_bundle(self, evidence) -> None:
        assert evidence == _build_execution()
        self._counters["durable_reuse"] += 1

    def launch_snapshot(self, snapshot_root: Path, _run_cwd: Path):
        self._counters["launch"] += 1
        if self._mutate_snapshot:
            executable = snapshot_root / "simv"
            executable.chmod(0o700)
            executable.write_bytes(b"mutated")
        name = self.names[0]
        test = SimulationTestOutcome(name=name, verdict="pass", passed=True)
        return SimulationTargetOutcome(
            target="sim",
            target_identity="acme:lib:dut:1#sim",
            toplevel="tb",
            eda_tool="icarus",
            passed=True,
            verdict="pass",
            elapsed_s=0.01,
            tests=(test,),
        )


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


class _Execution:
    def __init__(
        self,
        build_root: Path,
        counters: dict[str, int],
        *,
        mutate_snapshot: bool = False,
        disclosures: dict[str, object] | None = None,
    ) -> None:
        self._build_root = build_root
        self._counters = counters
        self._mutate_snapshot = mutate_snapshot
        self._disclosures = disclosures or {}

    @contextmanager
    def ordinary_group(self, _handle: object, names: tuple[str, ...]):
        yield _Group(
            names,
            self._build_root,
            self._counters,
            mutate_snapshot=self._mutate_snapshot,
            disclosure=self._disclosures.get(names[0]),
        )


def _executor(
    build_root: Path,
    counters: dict[str, int],
    *,
    mutate_snapshot: bool = False,
    disclosures: dict[str, object] | None = None,
) -> OrdinaryHdlSerialExecutor:
    return OrdinaryHdlSerialExecutor(
        invoke=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
        execution_factory=lambda _options: _Execution(
            build_root,
            counters,
            mutate_snapshot=mutate_snapshot,
            disclosures=disclosures,
        ),  # type: ignore[arg-type,return-value]
    )


def _request(
    store: CampaignStore,
    manifest,
    item: object,
    project: Path,
    *,
    invocation: int,
) -> WorkExecutionRequest:
    item_id = item["work_item_id"]  # type: ignore[index]
    attempt_id = str(uuid.uuid4())
    ordinal, directory = store.allocate_attempt_directory(item_id, attempt_id)
    return WorkExecutionRequest(
        store,
        manifest,
        item,  # type: ignore[arg-type]
        attempt_id,
        ordinal,
        directory,
        invocation,
        CampaignPolicy(),
        _admission(),
        project,
        _handle(project),  # type: ignore[arg-type]
    )


def test_shared_build_is_recovered_by_a_fresh_executor_and_scoped_per_invocation(
    tmp_path: Path,
) -> None:
    manifest = _manifest_for(("alpha", "beta"))
    project = tmp_path / "project"
    project.mkdir()
    build_root = tmp_path / "engine-build"
    build_root.mkdir()
    (build_root / "simv").write_bytes(b"image")
    store = CampaignStore(tmp_path / "campaign")
    store.publish_manifest(manifest)
    items = manifest.document["work_items"]
    counters = {"compile": 0, "memory_reuse": 0, "durable_reuse": 0, "launch": 0}

    first = _executor(build_root, counters).execute(
        _request(store, manifest, items[0], project, invocation=1)
    )
    store.verify_result_evidence(
        store.work_item_directory(items[0]["work_item_id"])  # type: ignore[index]
        / "attempts"
        / f"{first.document['attempt_ordinal']:04d}-{first.document['attempt_id']}",
        first,
    )
    second = _executor(build_root, counters).execute(
        _request(store, manifest, items[1], project, invocation=1)
    )
    third = _executor(build_root, counters).execute(
        _request(store, manifest, items[0], project, invocation=2)
    )

    assert counters == {
        "compile": 2,
        "memory_reuse": 0,
        "durable_reuse": 1,
        "launch": 3,
    }
    assert first.document["build_result"]["sha256"] == second.document["build_result"]["sha256"]
    assert third.document["build_result"]["sha256"] != first.document["build_result"]["sha256"]
    assert len(tuple(store.root.glob("build-variants/*/attempts/*/build-result.json"))) == 2


def test_legacy_mode_builds_and_discloses_each_work_item_privately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    disclosures = _legacy_disclosures()
    base_manifest = _manifest_for(("alpha", "beta"), access="legacy-per-test")
    manifest_document = json.loads(canonical_json_bytes(base_manifest.document))
    manifest_document.pop("fingerprints")
    manifest_document["planning_disclosures"] = list(disclosures.values())
    manifest = finalize_manifest(manifest_document)
    plan = create_simulation_campaign_plan(manifest)
    project = tmp_path / "project"
    project.mkdir()
    build_root = tmp_path / "engine-build"
    build_root.mkdir()
    (build_root / "simv").write_bytes(b"image")
    counters = {"compile": 0, "memory_reuse": 0, "durable_reuse": 0, "launch": 0}
    hooks: list[tuple[tuple[str, ...], bool, Path | None]] = []

    def record_hook(
        _handle: object,
        _root: Path,
        names: tuple[str, ...],
        _options: object,
        run_cwd: Path | None,
        expose_build_root: bool,
    ) -> None:
        hooks.append((names, expose_build_root, run_cwd))

    monkeypatch.setattr(serial_execution, "_run_hook", record_hook)
    monkeypatch.setattr(
        serial_execution.TargetCatalog,
        "build",
        lambda _root: SimpleNamespace(select=lambda *_args, **_kwargs: _handle(project)),
    )
    invocation = tmp_path / "reports" / "000001"
    invocation.mkdir(parents=True)
    outcome = SimulationCampaign(_executor(build_root, counters, disclosures=disclosures)).run(
        NewCampaignRunRequest(
            plan, project, invocation.parent, CampaignPolicy(), invocation, _admission()
        )
    )
    _assert_private_legacy_results(outcome, counters, hooks, invocation)


def _legacy_disclosures():
    return {
        name: {
            "planner": f"fake-{name}",
            "scratch_inputs": [],
            "generated_files": [],
            "tool_provenance": {"kind": "fake", "version": "1", "contract_version": "1"},
            "cleanup": {"removed": True},
        }
        for name in ("alpha", "beta")
    }


def _packaged_source_disclosure(raw: bytes) -> dict[str, object]:
    return {
        "planner": "fusesoc_setup",
        "scratch_inputs": [],
        "generated_files": [
            {
                "path": "booley-package/refs/booley_vcd_dump.sv",
                "bytes": len(raw),
                "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
                "kind": "generated_input",
            }
        ],
        "tool_provenance": {
            "kind": "fusesoc",
            "version": "2.4.6",
            "contract_version": "1",
        },
        "cleanup": {"removed": True},
    }


def test_changed_packaged_source_fails_disclosure_equality_before_compile(
    tmp_path: Path,
) -> None:
    planned = _packaged_source_disclosure(b"planned wheel bytes")
    execution = _packaged_source_disclosure(b"different execution wheel bytes")
    base_manifest = _manifest_for(("alpha",))
    document = json.loads(canonical_json_bytes(base_manifest.document))
    document.pop("fingerprints")
    document["planning_disclosures"] = [planned]
    manifest = finalize_manifest(document)
    project = tmp_path / "project"
    project.mkdir()
    build_root = tmp_path / "engine-build"
    build_root.mkdir()
    (build_root / "simv").write_bytes(b"image")
    store = CampaignStore(tmp_path / "campaign")
    store.publish_manifest(manifest)
    counters = {"compile": 0, "memory_reuse": 0, "durable_reuse": 0, "launch": 0}

    with pytest.raises(
        SimulationCampaignIntegrityError,
        match="prepared generator source closure disagrees with campaign plan",
    ):
        _executor(build_root, counters, disclosures={"alpha": execution}).execute(
            _request(
                store,
                manifest,
                manifest.document["work_items"][0],
                project,
                invocation=1,
            )
        )

    assert counters["compile"] == 0


def _assert_private_legacy_results(outcome, counters, hooks, invocation) -> None:
    assert outcome.complete is True
    assert counters["compile"] == 2
    assert counters["memory_reuse"] == counters["durable_reuse"] == 0
    assert hooks == [(("alpha",), True, None), (("beta",), True, None)]
    store = CampaignStore(invocation / "targets/sim/campaign")
    private_results = tuple(
        store.root.glob("work-items/*/attempts/*/private-build/build-result.json")
    )
    assert len(private_results) == 2
    assert not tuple(store.root.glob("build-variants/*/attempts/*/build-result.json"))
    assert {
        json.loads((store.work_item_directory(item) / "result.json").read_text())["build_result"][
            "sharing"
        ]
        for item in store.scan().complete
    } == {"private_work_item"}


@pytest.mark.parametrize("access", ["immutable", "legacy-per-test"])
def test_snapshot_mutation_fails_closed_for_shared_and_private_bundles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, access: str
) -> None:
    manifest = _manifest_for(("alpha",), access=access)
    plan = create_simulation_campaign_plan(manifest)
    project = tmp_path / "project"
    project.mkdir()
    build_root = tmp_path / "engine-build"
    build_root.mkdir()
    (build_root / "simv").write_bytes(b"image")
    counters = {"compile": 0, "memory_reuse": 0, "durable_reuse": 0, "launch": 0}
    monkeypatch.setattr(serial_execution, "_run_hook", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        serial_execution.TargetCatalog,
        "build",
        lambda _root: SimpleNamespace(select=lambda *_args, **_kwargs: _handle(project)),
    )
    invocation = tmp_path / "reports" / "000001"
    invocation.mkdir(parents=True)

    with pytest.raises(SimulationCampaignIntegrityError, match="snapshot changed"):
        SimulationCampaign(_executor(build_root, counters, mutate_snapshot=True)).run(
            NewCampaignRunRequest(
                plan, project, invocation.parent, CampaignPolicy(), invocation, _admission()
            )
        )

    store = CampaignStore(invocation / "targets/sim/campaign")
    assert counters["launch"] == 1
    assert store.scan().pending == ()
    assert len(store.scan().interrupted) == 1
    assert not tuple(store.root.glob("work-items/*/result.json"))


def test_immutable_hook_compile_surface_mutation_stops_before_snapshot_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _manifest_for(("alpha",))
    plan = create_simulation_campaign_plan(manifest)
    project = tmp_path / "project"
    project.mkdir()
    build_root = tmp_path / "engine-build"
    build_root.mkdir()
    (build_root / "simv").write_bytes(b"image")
    counters = {"compile": 0, "memory_reuse": 0, "durable_reuse": 0, "launch": 0}
    surfaces = iter(("before", "after"))
    monkeypatch.setattr(serial_execution, "project_compile_surface", lambda _root: next(surfaces))
    monkeypatch.setattr(serial_execution, "_run_hook", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        serial_execution.TargetCatalog,
        "build",
        lambda _root: SimpleNamespace(select=lambda *_args, **_kwargs: _handle(project)),
    )
    invocation = tmp_path / "reports" / "000001"
    invocation.mkdir(parents=True)

    with pytest.raises(SimulationCampaignIntegrityError, match="compile inputs changed"):
        SimulationCampaign(_executor(build_root, counters)).run(
            NewCampaignRunRequest(
                plan, project, invocation.parent, CampaignPolicy(), invocation, _admission()
            )
        )

    assert counters["compile"] == 1
    assert counters["launch"] == 0
    store = CampaignStore(invocation / "targets/sim/campaign")
    assert len(store.scan().interrupted) == 1
    assert not tuple(store.root.glob("work-items/*/result.json"))
