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
from booley.flows.sim.execution.engine import (
    PreparedOrdinaryGroup,
    SimulationBuildSlotError,
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


def test_authenticated_bundle_rejects_timed_out_compiler_process() -> None:
    evidence = _build_execution()
    evidence["process"]["timed_out"] = True  # type: ignore[index]
    group = PreparedOrdinaryGroup.__new__(PreparedOrdinaryGroup)
    group._lease_active = True
    group._build_process = None
    group._build = None

    with pytest.raises(
        SimulationBuildSlotError,
        match="recovered bundle evidence is not successful",
    ):
        group.bind_authenticated_bundle(evidence)

    assert group._build_process is None
    assert group._build is None


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
    checkpoint=None,
) -> OrdinaryHdlSerialExecutor:
    return OrdinaryHdlSerialExecutor(
        publication_checkpoint=checkpoint,
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
    policy: CampaignPolicy | None = None,
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
        policy or CampaignPolicy(),
        _admission(),
        project,
        _handle(project),  # type: ignore[arg-type]
    )


def test_campaign_policy_carries_build_budget_into_execution_options(
    tmp_path: Path,
) -> None:
    manifest = _manifest_for(("alpha",))
    project = tmp_path / "project"
    project.mkdir()
    build_root = tmp_path / "engine-build"
    build_root.mkdir()
    (build_root / "simv").write_bytes(b"image")
    store = CampaignStore(tmp_path / "campaign")
    store.publish_manifest(manifest)
    captured: list[SimulationOptions] = []
    counters = {"compile": 0, "durable_reuse": 0, "launch": 0}

    def factory(options: SimulationOptions) -> _Execution:
        captured.append(options)
        return _Execution(build_root, counters)

    executor = OrdinaryHdlSerialExecutor(
        invoke=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
        execution_factory=factory,  # type: ignore[arg-type,return-value]
    )
    item = manifest.document["work_items"][0]
    executor.execute(
        _request(
            store,
            manifest,
            item,
            project,
            invocation=1,
            policy=CampaignPolicy(timeout_seconds=5, build_timeout_seconds=7),
        )
    )

    assert captured == [SimulationOptions(timeout_ms=5000, build_timeout_ms=7000)]
    assert counters["compile"] == 1


def test_private_campaign_build_uses_carried_build_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    disclosures = {"alpha": _legacy_disclosures()["alpha"]}
    base_manifest = _manifest_for(("alpha",), access="legacy-per-test")
    document = json.loads(canonical_json_bytes(base_manifest.document))
    document.pop("fingerprints")
    document["planning_disclosures"] = list(disclosures.values())
    manifest = finalize_manifest(document)
    project = tmp_path / "project"
    project.mkdir()
    build_root = tmp_path / "engine-build"
    build_root.mkdir()
    (build_root / "simv").write_bytes(b"image")
    store = CampaignStore(tmp_path / "campaign")
    store.publish_manifest(manifest)
    captured: list[SimulationOptions] = []
    counters = {"compile": 0, "durable_reuse": 0, "launch": 0}

    def factory(options: SimulationOptions) -> _Execution:
        captured.append(options)
        return _Execution(build_root, counters, disclosures=disclosures)

    monkeypatch.setattr(serial_execution, "_run_hook", lambda *_args, **_kwargs: None)
    executor = OrdinaryHdlSerialExecutor(
        invoke=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
        execution_factory=factory,  # type: ignore[arg-type,return-value]
    )
    executor.execute(
        _request(
            store,
            manifest,
            manifest.document["work_items"][0],
            project,
            invocation=1,
            policy=CampaignPolicy(timeout_seconds=5, build_timeout_seconds=7),
        )
    )

    assert captured == [SimulationOptions(timeout_ms=5000, build_timeout_ms=7000)]
    assert counters["compile"] == 1


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
    counters = {"compile": 0, "durable_reuse": 0, "launch": 0}

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
    counters = {"compile": 0, "durable_reuse": 0, "launch": 0}
    hooks: list[tuple[tuple[str, ...], bool, Path | None]] = []

    def record_hook(
        _handle: object,
        _root: Path,
        names: tuple[str, ...],
        _options: object,
        run_cwd: Path | None,
        expose_build_root: bool,
        *,
        commands: tuple[str, ...],
    ) -> None:
        assert commands == ()
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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("attempt_id", "wrong-owner"),
        ("ordinal", True),
        ("elapsed_s", float("nan")),
        ("returncode", True),
        ("command_count", 3),
        ("test_names", ["other"]),
        ("stdout_tail", "x" * 8193),
        ("status", "invented"),
    ],
)
def test_interrupted_hook_evidence_fails_closed_on_semantic_corruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    from tests.flows.sim.test_endpoint_campaign_lifecycle import _successful_pre_sim_campaign

    invocation, outcome = _successful_pre_sim_campaign(tmp_path, monkeypatch, "immutable")
    firing = outcome.pre_sim_firings[0]
    document = json.loads(firing.path.read_bytes())
    document[field] = value
    firing.path.write_text(json.dumps(document))
    (firing.path.parents[3] / "result.json").unlink()
    with pytest.raises(SimulationCampaignIntegrityError):
        CampaignStore(invocation / "targets/sim/campaign").scan()


def test_terminal_hook_tampering_and_symlink_are_not_authoritative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.flows.sim.test_endpoint_campaign_lifecycle import _successful_pre_sim_campaign

    invocation, outcome = _successful_pre_sim_campaign(tmp_path, monkeypatch, "immutable")
    firing = outcome.pre_sim_firings[0]
    firing.path.write_bytes(firing.path.read_bytes() + b" ")
    store = CampaignStore(invocation / "targets/sim/campaign")
    with pytest.raises(SimulationCampaignIntegrityError):
        store.scan()
    firing.path.unlink()
    firing.path.symlink_to(tmp_path / "outside.json")
    with pytest.raises(SimulationCampaignIntegrityError):
        store.scan()


@pytest.mark.parametrize("coverage", [False, True])
def test_pre_cancelled_campaign_hook_never_compiles_or_runs(tmp_path, coverage):
    from booley.flows.sim.campaign.coverage_execution import CoverageAggregateExecutor
    from booley.flows.sim.execution.contract import PreSimScopeStoppedError
    from booley.runtime.supervised_execution import (
        SupervisedExecutionScope,
        supervised_execution_scope,
    )
    from tests.flows.sim.test_endpoint_campaign_lifecycle import _pre_sim_plan

    def transform(document):
        if coverage:
            document["workload"]["coverage"] = True
            document["work_items"][0]["kind"] = "coverage_aggregate"

    plan, _disclosures = _pre_sim_plan("immutable", ("echo actual-hook",), transform)
    store = CampaignStore(tmp_path / "campaign")
    store.publish_manifest(plan.manifest)
    project = tmp_path / "project"
    project.mkdir()
    request = _request(
        store, plan.manifest, plan.manifest.document["work_items"][0], project, invocation=1
    )
    counters = {"compile": 0, "durable_reuse": 0, "launch": 0}
    executor = _executor(tmp_path / "build", counters)
    if coverage:
        executor = CoverageAggregateExecutor(
            plans={},
            execution_factory=lambda *_args: pytest.fail("cancelled coverage must not compile"),
        )
    scope = SupervisedExecutionScope(None, tmp_path / ".booley_project", lambda: True)
    with supervised_execution_scope(scope), pytest.raises(PreSimScopeStoppedError):
        executor.execute(request)
    assert counters == {"compile": 0, "durable_reuse": 0, "launch": 0}
    assert list(request.attempt_directory.glob("pre-sim/*.json")) == []


def _maximum_hook_selection_plan(names):
    from tests.flows.sim.test_endpoint_campaign_lifecycle import _pre_sim_plan

    def transform(document):
        workload = document["workload"]
        workload["coverage"] = True
        item = document["work_items"][0]
        item.update(
            kind="coverage_aggregate",
            selection={"kind": "named", "names": names},
            arguments=list(names),
        )
        suite = document["required_suite"]
        source = "\n".join(names).encode()
        suite.update(
            names=list(names),
            source_bytes=len(source),
            source_sha256="sha256:" + hashlib.sha256(source).hexdigest(),
        )

    plan, _disclosures = _pre_sim_plan("immutable", ("true",), transform)
    return plan, _disclosures


def test_maximum_selection_uses_large_sidecar_and_bounded_report_preview(tmp_path):
    from booley.flows.sim.campaign import read_pre_sim_firings
    from booley.flows.sim.campaign.pre_sim_evidence import publish_pre_sim_firing
    from booley.flows.sim.execution.contract import PreSimEvidence
    from booley.flows.sim.flow import _campaign_pre_sim_details

    names = tuple(f"test{index:04d}" + "x" * 504 for index in range(4000))

    plan, _disclosures = _maximum_hook_selection_plan(names)
    invocation = tmp_path / "reports/1"
    store = CampaignStore(invocation / "targets/sim/campaign")
    store.publish_manifest(plan.manifest)
    request = _request(
        store, plan.manifest, plan.manifest.document["work_items"][0], tmp_path, invocation=1
    )
    _executor(tmp_path / "build", {}).prepare_attempt(request)
    evidence = PreSimEvidence(
        ("true",),
        names,
        "passed",
        0.1,
        returncode=0,
        stdout_tail="s" * 8192,
        stderr_tail="e" * 8192,
    )
    reference = publish_pre_sim_firing(request, evidence)
    assert 1024 * 1024 < reference["bytes"] < 16 * 1024 * 1024
    assert len(canonical_json_bytes(reference)) < 1024
    firings = read_pre_sim_firings(store.manifest_path)
    assert firings[0].document["test_names"] == names
    detail = _campaign_pre_sim_details(
        [
            SimpleNamespace(
                pre_sim_firings=firings,
                producer_invocation_directory=invocation.absolute(),
                current_pre_sim_keys=frozenset(f.key for f in firings),
            )
        ],
        invocation,
    )
    assert len(json.dumps(detail["pre_sim_runs"]).encode()) <= 16 * 1024
    assert detail["pre_sim_runs"][0]["test_count"] == 4000
    assert len(detail["pre_sim_runs"][0]["test_names"]) == 8


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
    counters = {"compile": 0, "durable_reuse": 0, "launch": 0}

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
    assert counters["durable_reuse"] == 0
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
    counters = {"compile": 0, "durable_reuse": 0, "launch": 0}
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
    counters = {"compile": 0, "durable_reuse": 0, "launch": 0}
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


def test_scope_stop_after_real_hook_retains_firing_without_simulator_launch(tmp_path, monkeypatch):
    from booley.flows.sim.campaign import serial_execution
    from booley.flows.sim.execution.contract import PreSimScopeStoppedError
    from booley.runtime.supervised_execution import (
        SupervisedExecutionScope,
        supervised_execution_scope,
    )
    from tests.flows.sim.test_endpoint_campaign_lifecycle import _pre_sim_plan

    plan, disclosures = _pre_sim_plan("immutable", ("echo actual-hook",), None)
    store = CampaignStore(tmp_path / "campaign")
    store.publish_manifest(plan.manifest)
    project = tmp_path / "project"
    project.mkdir()
    build = tmp_path / "build"
    build.mkdir()
    (build / "simv").write_bytes(b"image")
    request = _request(
        store, plan.manifest, plan.manifest.document["work_items"][0], project, invocation=1
    )
    counters = {"compile": 0, "durable_reuse": 0, "launch": 0}
    executor = _executor(build, counters, disclosures=disclosures)
    monkeypatch.setattr(
        serial_execution.TargetCatalog,
        "build",
        lambda _root: SimpleNamespace(select=lambda *_a, **_k: _handle(project)),
    )
    monkeypatch.setattr(serial_execution, "simulation_target_environment", lambda _handle: {})
    original_hook = serial_execution._run_hook
    stopped = False

    def stop_after_hook(*args, **kwargs):
        nonlocal stopped
        evidence = original_hook(*args, **kwargs)
        stopped = True
        return evidence

    monkeypatch.setattr(serial_execution, "_run_hook", stop_after_hook)
    scope = SupervisedExecutionScope(None, project / ".booley_project", lambda: stopped)
    with supervised_execution_scope(scope), pytest.raises(PreSimScopeStoppedError):
        executor.execute(request)
    assert counters["launch"] == 0
    firings = store.read_pre_sim_firings()
    assert len(firings) == 1
    assert firings[0].document["returncode"] == 0
    assert firings[0].document["stdout_tail"] == "actual-hook\n"
    assert not (request.attempt_directory.parent.parent / "result.json").exists()


def _aggregate_result_bound_fixture(tmp_path):
    from booley.flows.sim.campaign.pre_sim_evidence import publish_pre_sim_firing
    from booley.flows.sim.execution.contract import PreSimEvidence
    from tests.flows.sim.test_campaign_codec_golden import _simulation_result

    names = tuple(f"test{index:04d}" for index in range(4000))
    plan, _ = _maximum_hook_selection_plan(names)
    store = CampaignStore(tmp_path / "campaign")
    store.publish_manifest(plan.manifest)
    request = _request(
        store, plan.manifest, plan.manifest.document["work_items"][0], tmp_path, invocation=1
    )
    _executor(tmp_path / "build", {}).prepare_attempt(request)
    for ordinal, name in enumerate(names, 1):
        publish_pre_sim_firing(
            request,
            PreSimEvidence(("true",), (name,), "passed", 0.0, returncode=0),
            ordinal=ordinal,
        )
    document = json.loads(_simulation_result("completed"))
    document.update(serial_execution._common(request))
    document["attempt_id"] = request.attempt_id
    document["executable_snapshot"]["manifest"]["owner"] = request.attempt_id
    document["evidence"] = store.pre_sim_references(request.attempt_directory)
    document["observations"] = [
        serial_execution._observation(
            SimulationTestOutcome(name=name, verdict="pass", passed=True)
        )
        for name in names
    ]
    return store, request, document


# Windows CI: 3x the slowest observed duration (tests/timeout_headroom.py).
@pytest.mark.timeout(90)
def test_maximum_aggregate_result_overflow_keeps_all_authenticated_sidecars(tmp_path):
    from booley.flows.sim.campaign.codec import RECORD_MAX_BYTES, decode_simulation_result

    store, request, document = _aggregate_result_bound_fixture(tmp_path)
    raw = canonical_json_bytes(document)
    assert len(document["evidence"]) == len(document["observations"]) == 4000
    assert len(raw) > RECORD_MAX_BYTES
    (tmp_path / "result-bound-proof.json").write_text(
        json.dumps(
            {
                "firings": len(document["evidence"]),
                "observations": len(document["observations"]),
                "hooked_result_bytes": len(raw),
                "without_hooks_bytes": len(canonical_json_bytes({**document, "evidence": []})),
                "record_limit_bytes": RECORD_MAX_BYTES,
                "fixture": "normalized sidecars and production observation serializer; wire-bound proof, not 4000 shell runs",
            },
            indent=2,
        )
    )
    with pytest.raises(SimulationCampaignIntegrityError, match="byte size ceiling"):
        store.publish_result(request.work_item["work_item_id"], decode_simulation_result(raw))
    assert not (request.attempt_directory.parent.parent / "result.json").exists()
    assert len(store.read_pre_sim_firings()) == 4000
    without_hooks = {**document, "evidence": []}
    assert len(canonical_json_bytes(without_hooks)) < RECORD_MAX_BYTES
    assert (
        len(decode_simulation_result(canonical_json_bytes(without_hooks)).document["observations"])
        == 4000
    )


def _hook_publication_request(root, names=("alpha", "beta"), invocation=1):
    plan, _ = _maximum_hook_selection_plan(names)
    store = CampaignStore(root / "campaign")
    store.publish_manifest(plan.manifest)
    request = _request(
        store,
        plan.manifest,
        plan.manifest.document["work_items"][0],
        root,
        invocation=invocation,
    )
    _executor(root / "build", {}).prepare_attempt(request)
    return request


def _publish_request_hook(request, name="alpha", ordinal=1, status="passed", returncode=0):
    from booley.flows.sim.campaign.pre_sim_evidence import publish_pre_sim_firing
    from booley.flows.sim.execution.contract import PreSimEvidence

    return publish_pre_sim_firing(
        request,
        PreSimEvidence(("true",), (name,), status, 0.0, returncode=returncode),
        ordinal=ordinal,
    )


def test_request_reuses_authenticated_manifest_across_firings(tmp_path, monkeypatch):
    from booley.flows.sim.campaign import pre_sim_evidence

    original = pre_sim_evidence.manifest_digest
    calls = []

    def authenticated_digest(manifest):
        calls.append(manifest)
        return original(manifest)

    monkeypatch.setattr(pre_sim_evidence, "manifest_digest", authenticated_digest)
    request = _hook_publication_request(tmp_path)
    first = _publish_request_hook(request)
    second = _publish_request_hook(request, "beta", 2)
    assert len(calls) == 1
    assert first["path"] == "pre-sim/0001.json"
    assert second["path"] == "pre-sim/0002.json"
    assert len(request.store.read_pre_sim_firings()) == 2


def test_request_replacement_reauthenticates_manifest_and_attempt(tmp_path, monkeypatch):
    from dataclasses import replace

    from booley.flows.sim.campaign import pre_sim_evidence

    original = pre_sim_evidence.manifest_digest
    calls = []

    def authenticated_digest(manifest):
        calls.append(manifest)
        return original(manifest)

    monkeypatch.setattr(pre_sim_evidence, "manifest_digest", authenticated_digest)
    first = _hook_publication_request(tmp_path / "first")
    second = _hook_publication_request(tmp_path / "second", ("gamma",), invocation=2)
    copied = replace(first, pre_sim_firing_published=lambda _key: None)
    changed = replace(
        first,
        store=second.store,
        manifest=second.manifest,
        work_item=second.work_item,
        attempt_id=second.attempt_id,
        attempt_ordinal=second.attempt_ordinal,
        attempt_directory=second.attempt_directory,
        producer_invocation_id=2,
    )
    assert len(calls) == 4
    assert len({id(r.pre_sim_decoder) for r in (first, second, copied, changed)}) == 4
    assert first.pre_sim_decoder.manifest_sha256 != changed.pre_sim_decoder.manifest_sha256
    _publish_request_hook(first)
    _publish_request_hook(changed, "gamma")
    assert len(calls) == 4
    records = changed.store.read_pre_sim_firings()
    assert records[0].document["manifest_sha256"] == changed.pre_sim_decoder.manifest_sha256
    assert records[0].document["producer_invocation_id"] == 2
    with pytest.raises((TypeError, ValueError), match="init=False"):
        replace(first, pre_sim_decoder=second.pre_sim_decoder)
    with pytest.raises(SimulationCampaignIntegrityError):
        _publish_request_hook(replace(first, producer_invocation_id=2), "beta", 2)
    assert not (first.attempt_directory / "pre-sim/0002.json").exists()


@pytest.mark.parametrize("corruption", ["type", "fingerprint", "structure"])
def test_request_rejects_malformed_manifest_at_construction(tmp_path, corruption):
    from dataclasses import replace

    from booley.flows.sim.campaign.model import SimulationCampaignManifest

    request = _hook_publication_request(tmp_path)
    document = json.loads(canonical_json_bytes(request.manifest.document))
    if corruption == "fingerprint":
        document["fingerprints"]["workload_sha256"] = "sha256:" + "0" * 64
    else:
        document.pop("work_items")
    invalid = object() if corruption == "type" else SimulationCampaignManifest(document)
    with pytest.raises(SimulationCampaignIntegrityError):
        replace(request, manifest=invalid)


def test_request_isolates_frozen_manifest_from_caller_mutation(tmp_path):
    from dataclasses import replace

    from booley.flows.sim.campaign.model import SimulationCampaignManifest

    request = _hook_publication_request(tmp_path)
    document = json.loads(canonical_json_bytes(request.manifest.document))
    manifest = SimulationCampaignManifest(document)
    frozen = replace(request, manifest=manifest)
    digest = frozen.pre_sim_decoder.manifest_sha256
    document["workload"]["source_recipe"]["pre_sim_commands"].append("wrong")
    document["work_items"][0]["selection"]["names"].append("outside")
    _publish_request_hook(frozen)
    assert frozen.pre_sim_decoder.manifest is manifest
    assert frozen.pre_sim_decoder.manifest_sha256 == digest
    assert manifest.document["workload"]["source_recipe"]["pre_sim_commands"] == ("true",)
    assert manifest.document["work_items"][0]["selection"]["names"] == ("alpha", "beta")
    assert frozen.store.read_pre_sim_firings()[0].document["manifest_sha256"] == digest


@pytest.mark.parametrize(
    "corruption",
    ["manifest", "producer", "selection", "ordinal", "status", "item", "manifest_copy"],
)
def test_request_publication_rejects_changed_attempt_after_success(tmp_path, corruption):
    from dataclasses import replace

    request = _hook_publication_request(tmp_path)
    published = []
    request = replace(request, pre_sim_firing_published=published.append)
    _publish_request_hook(request)
    first_bytes = (request.attempt_directory / "pre-sim/0001.json").read_bytes()
    first_publication = published.copy()
    if corruption in {"item", "manifest_copy"}:
        other = _hook_publication_request(tmp_path / "other", ("gamma",))
        changes = {"work_item": other.work_item}
        if corruption == "manifest_copy":
            changes["manifest"] = other.manifest
        request = replace(request, **changes)
    if corruption in {"manifest", "producer"}:
        path = request.attempt_directory / "attempt.json"
        document = json.loads(path.read_bytes())
        key = "manifest_sha256" if corruption == "manifest" else "producer_invocation_id"
        document[key] = "sha256:" + "0" * 64 if corruption == "manifest" else 2
        path.write_bytes(canonical_json_bytes(document))
    name = "outside" if corruption == "selection" else "beta"
    ordinal = 0 if corruption == "ordinal" else 2
    status = "timed_out" if corruption == "status" else "passed"
    expected = {
        "manifest": "hook attempt does not bind its frozen workload",
        "item": "hook attempt does not bind its frozen workload",
        "manifest_copy": "hook attempt does not bind its frozen workload",
        "producer": "Pre-Sim Commands evidence owner disagrees",
        "selection": "selection is outside workload",
        "ordinal": "invalid ordinal",
        "status": "timed out hook cannot supply an exit code",
    }
    with pytest.raises(SimulationCampaignIntegrityError, match=expected[corruption]):
        _publish_request_hook(request, name, ordinal, status)
    assert published == first_publication
    assert len(published) == 1
    assert (request.attempt_directory / "pre-sim/0001.json").read_bytes() == first_bytes
    assert sorted(p.name for p in (request.attempt_directory / "pre-sim").iterdir()) == [
        "0001.json"
    ]
