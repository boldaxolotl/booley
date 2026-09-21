"""Process-death matrix for the ordinary-HDL serial publication protocol."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.flows.endpoint_admission import AdmissionContext
from booley.flows.sim.campaign.codec import (
    SimulationCampaignIntegrityError,
    canonical_json_bytes,
)
from booley.flows.sim.campaign.coordinator import (
    CampaignPolicy,
    NewCampaignRunRequest,
    ResumeCampaignRunRequest,
    SimulationCampaign,
)
from booley.flows.sim.campaign.model import create_simulation_campaign_plan
from booley.flows.sim.campaign.planning import finalize_manifest, manifest_digest
from booley.flows.sim.campaign.resume import ValidatedManifestNode, ValidatedResumeManifest
from booley.flows.sim.campaign.serial_execution import OrdinaryHdlSerialExecutor
from booley.flows.sim.campaign.store import CampaignStore
from booley.flows.sim.campaign_reports import write_compatibility_projection
from booley.flows.sim.execution.contract import SimulationTargetOutcome, SimulationTestOutcome
from booley.ticket_board import acceptance_ledger
from tests.flows.sim.test_campaign_manifest_codec import _manifest
from tests.ticket_board.test_acceptance_ledger import _campaign_facts, _transaction_state

_DURABLE_BOUNDARIES = (
    "manifest_commit",
    "simulation_attempt",
    "build_attempt",
    "bundle_evidence",
    "build_result",
    "runtime_inputs",
    "snapshot_manifest",
    "execution_evidence",
    "simulation_result",
    "summary_replace",
)
_CRASH_POINTS = (
    *(
        f"{side}:{boundary}"
        for boundary in _DURABLE_BOUNDARIES
        for side in ("before", "after")
    ),
    "integrity:prelaunch_authentication",
    "integrity:post_exit_authentication",
)


class _InjectedProcessDeath(BaseException):
    pass


class _CrashOnce:
    def __init__(self, selected: str) -> None:
        self.selected = selected
        self.seen: list[str] = []
        self.tripped = False

    def __call__(self, boundary: str) -> None:
        self.seen.append(boundary)
        if boundary == self.selected and not self.tripped:
            self.tripped = True
            raise _InjectedProcessDeath(boundary)


def _admission() -> AdmissionContext:
    return AdmissionContext(
        "unmanaged", None, None, 1, "interactive", "", None, lambda: False
    )


@pytest.mark.parametrize("crash_point", _CRASH_POINTS)
def test_serial_publication_boundary_resume_matrix(  # noqa: PLR0915 -- matrix harness
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_point: str,
) -> None:
    """Every durable prefix resumes; only a committed Result suppresses rerun."""
    document = _manifest()
    document.pop("fingerprints")
    declaration_identity = {
        "source_artifact_path": "input.bin",
        "destination": "inputs/input.bin",
    }
    declaration_id = "sha256:" + hashlib.sha256(
        canonical_json_bytes(declaration_identity).rstrip(b"\n")
    ).hexdigest()
    document["workload"]["runtime_inputs"] = [  # type: ignore[index]
        {
            "declaration_id": declaration_id,
            **declaration_identity,
        }
    ]
    manifest = finalize_manifest(document)
    plan = create_simulation_campaign_plan(manifest)
    project = tmp_path / "project"
    project.mkdir()
    (project / "run").mkdir()
    build_root = tmp_path / "engine-build"
    build_root.mkdir()
    (build_root / "simv").write_bytes(b"image")
    (build_root / "input.bin").write_bytes(b"runtime")
    run_log = build_root / "run.log"
    run_log.write_text("PASS\n", encoding="utf-8")
    launches: list[int] = []

    class FakeCatalog:
        def select(self, token: str, *, for_flow: str):
            assert (token, for_flow) == ("sim", "sim")
            return SimpleNamespace(
                identity="acme:lib:dut:1#sim",
                project_root=project,
                selector="sim",
                eda_tool="icarus",
            )

    class FakeGroup:
        def __init__(self) -> None:
            self.build_root = build_root
            self.artifact_paths = (build_root / "simv",)

        def compile(self):
            return SimpleNamespace(passed=True)

        def planning_disclosure(self):
            return {}

        def launch_snapshot(self, snapshot_root: Path, run_cwd: Path):
            launches.append(1)
            assert (snapshot_root / "simv").is_file()
            assert (run_cwd / "inputs/input.bin").read_bytes() == b"runtime"
            test = SimulationTestOutcome(
                name="sim", verdict="pass", passed=True, run_log_path=str(run_log)
            )
            return SimulationTargetOutcome(
                target="sim",
                target_identity="acme:lib:dut:1#sim",
                toplevel="tb",
                eda_tool="icarus",
                passed=True,
                verdict="pass",
                elapsed_s=0.1,
                tests=(test,),
            )

    class FakeExecution:
        @contextmanager
        def ordinary_group(self, handle, names):
            del handle
            assert names == ()
            yield FakeGroup()

    monkeypatch.setattr(
        "booley.flows.sim.campaign.serial_execution.TargetCatalog.build",
        lambda _root: FakeCatalog(),
    )
    crash = _CrashOnce(crash_point)
    executor = OrdinaryHdlSerialExecutor(
        invoke=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
        execution_factory=lambda _options: FakeExecution(),  # type: ignore[arg-type,return-value]
        publication_checkpoint=crash,
    )
    campaign = SimulationCampaign(executor, publication_checkpoint=crash)
    invocation = tmp_path / "reports" / "000001"
    invocation.mkdir(parents=True)
    policy = CampaignPolicy()
    new_request = NewCampaignRunRequest(
        plan=plan,
        project_root=project,
        report_root=invocation.parent,
        policy=policy,
        invocation_directory=invocation,
        admission=_admission(),
    )

    with pytest.raises(_InjectedProcessDeath, match=crash_point):
        campaign.run(new_request)

    store = CampaignStore(invocation / "targets" / "sim" / "campaign")
    if crash_point == "before:manifest_commit":
        assert not store.manifest_path.exists()
        outcome = campaign.run(new_request)
    else:
        assert store.manifest_path.is_file()
        validated = ValidatedResumeManifest(
            ValidatedManifestNode(store.manifest_path, manifest, manifest_digest(manifest)),
            (),
            (),
        )
        outcome = campaign.run(
            ResumeCampaignRunRequest(
                validated=validated,
                current_plan=plan,
                project_root=project,
                report_root=invocation.parent,
                policy=policy,
                invocation_directory=invocation,
                admission=_admission(),
            )
        )

    recovered = store.scan().items[0]
    committed_before_crash = crash_point in {
        "after:simulation_result",
        "before:summary_replace",
        "after:summary_replace",
    }
    crashed_before_attempt_allocation = crash_point in {
        "before:manifest_commit",
        "after:manifest_commit",
    }
    assert outcome.complete is True
    assert recovered.state == "complete"
    assert recovered.attempt_count == (
        1 if committed_before_crash or crashed_before_attempt_allocation else 2
    )
    if committed_before_crash:
        assert len(launches) == 1
    else:
        assert 1 <= len(launches) <= 2
    assert crash.tripped is True
    assert crash_point in crash.seen


def test_fault_matrix_has_no_distinct_partial_fsync_state() -> None:
    """Document the intentional bound on injected states.

    Immutable records use create/write/file-fsync/parent-fsync and projections
    use temp/write/file-fsync/replace/parent-fsync. A process cannot resume from
    an in-memory "half fsync" state: it sees either no committed name, a trailing
    interrupted attempt, or canonical bytes that decode and authenticate.
    Corrupt/truncated bytes are covered by the store's fail-closed matrix, so
    injecting within an OS fsync would duplicate that contract rather than add
    a durable recovery classification.
    """
    assert set(_CRASH_POINTS) == {
        *(f"{side}:{boundary}" for boundary in _DURABLE_BOUNDARIES for side in ("before", "after")),
        "integrity:prelaunch_authentication",
        "integrity:post_exit_authentication",
    }


@pytest.mark.parametrize(
    "crash_point",
    [
        f"{side}:acceptance_{publication}"
        for publication in ("intent", "record", "commit", "state")
        for side in ("before", "after")
    ],
)
def test_acceptance_publication_boundary_retry_is_idempotent(
    tmp_path: Path,
    crash_point: str,
) -> None:
    log_dir, state, changes = _transaction_state(tmp_path)
    identity = {"generation": "d" * 32, "authored_sha256": "e" * 64}
    crash = _CrashOnce(crash_point)

    with pytest.raises(_InjectedProcessDeath, match=crash_point):
        acceptance_ledger.record_or_verify_transaction(
            log_dir,
            state,
            changes,
            acceptance_facts=_campaign_facts(),
            ticket_identity=identity,
            publication_checkpoint=crash,
        )

    transaction = acceptance_ledger.record_or_verify_transaction(
        log_dir,
        state,
        changes,
        acceptance_facts=_campaign_facts(),
        ticket_identity=identity,
        publication_checkpoint=crash,
    )
    assert state.acceptance_transactions == [transaction.transaction_id]
    assert len(list((log_dir / "acceptance" / "transactions").glob("*.json"))) == 1
    assert len(transaction.evidence) == len(changes)
    assert crash.tripped is True


@pytest.mark.parametrize(
    "crash_point",
    ["before:compatibility_projection", "after:compatibility_projection"],
)
def test_compatibility_projection_boundary_is_retryable(
    tmp_path: Path,
    crash_point: str,
) -> None:
    path = tmp_path / "simulation.json"
    crash = _CrashOnce(crash_point)

    with pytest.raises(_InjectedProcessDeath, match=crash_point):
        write_compatibility_projection(
            path,
            {"target": "sim"},
            acceptance_committed=True,
            publication_checkpoint=crash,
        )
    write_compatibility_projection(
        path,
        {"target": "sim"},
        acceptance_committed=True,
        publication_checkpoint=crash,
    )

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "complete": True,
        "target": "sim",
    }


def test_invalid_committed_result_fails_closed_before_executor_entry(
    tmp_path: Path,
) -> None:
    document = _manifest()
    document.pop("fingerprints")
    manifest = finalize_manifest(document)
    plan = create_simulation_campaign_plan(manifest)
    invocation = tmp_path / "reports" / "000001"
    store = CampaignStore(invocation / "targets" / "sim" / "campaign")
    store.publish_manifest(manifest)
    work_item_id = manifest.document["work_items"][0]["work_item_id"]  # type: ignore[index]
    result_path = store.work_item_directory(work_item_id) / "result.json"
    result_path.parent.mkdir(parents=True)
    result_path.write_bytes(b'{"truncated":')
    entered: list[str] = []

    class MustNotExecute:
        def execute(self, request):
            entered.append(request.attempt_id)
            raise AssertionError("invalid Result must fail before EDA execution")

    validated = ValidatedResumeManifest(
        ValidatedManifestNode(store.manifest_path, manifest, manifest_digest(manifest)),
        (),
        (),
    )
    request = ResumeCampaignRunRequest(
        validated=validated,
        current_plan=plan,
        project_root=tmp_path,
        report_root=invocation.parent,
        policy=CampaignPolicy(),
        invocation_directory=invocation,
        admission=_admission(),
    )

    with pytest.raises(SimulationCampaignIntegrityError):
        SimulationCampaign(MustNotExecute()).run(request)  # type: ignore[arg-type]

    assert entered == []


@pytest.mark.parametrize("boundary", ["before:build_result", "after:build_result"])
@pytest.mark.parametrize(
    ("failure_path", "expected_state"),
    [
        ("legacy_design", "design_failure"),
        ("legacy_spawn", "infrastructure_error"),
        ("compile_design", "design_failure"),
        ("compile_spawn", "infrastructure_error"),
    ],
)
def test_failed_build_result_publication_is_retryable_and_never_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    failure_path: str,
    expected_state: str,
) -> None:
    document = _manifest()
    document.pop("fingerprints")
    document["workload"]["pre_sim_build_access"] = (  # type: ignore[index]
        "legacy-per-test" if failure_path.startswith("legacy") else "immutable"
    )
    manifest = finalize_manifest(document)
    plan = create_simulation_campaign_plan(manifest)
    project = tmp_path / "project"
    project.mkdir()
    (project / "run").mkdir()
    engine_root = tmp_path / "build"
    engine_root.mkdir()
    infrastructure = expected_state == "infrastructure_error"

    class FakeCatalog:
        def select(self, *_args, **_kwargs):
            return SimpleNamespace(
                identity="acme:lib:dut:1#sim", project_root=project,
                selector="sim", eda_tool="icarus",
            )

    class FakeGroup:
        build_root = engine_root
        artifact_paths: tuple[Path, ...] = ()

        def planning_disclosure(self):
            return {}

        def compile(self):
            return SimpleNamespace(passed=False)

        def finish_build_failure(self):
            failure = (
                SimpleNamespace(kind="spawn", message="could not spawn", detail="missing")
                if infrastructure else None
            )
            test = SimulationTestOutcome(
                name="sim", verdict="elab_error", passed=False,
                elab_failed=True, error_tail="compile failed",
            )
            return SimulationTargetOutcome(
                target="sim", target_identity="acme:lib:dut:1#sim",
                toplevel="tb", eda_tool="icarus", passed=False,
                verdict="error" if infrastructure else "fail", elapsed_s=0.1,
                tests=(test,), infrastructure_failure=failure,
            )

    class FakeExecution:
        @contextmanager
        def ordinary_group(self, *_args):
            yield FakeGroup()

    if failure_path.startswith("legacy"):
        status = "spawn_error" if infrastructure else "failed"
        monkeypatch.setattr(
            "booley.flows.sim.campaign.serial_execution._run_hook",
            lambda *_args, **_kwargs: SimpleNamespace(
                status=status, detail="hook failed", elapsed_s=0.1
            ),
        )
    monkeypatch.setattr(
        "booley.flows.sim.campaign.serial_execution.TargetCatalog.build",
        lambda _root: FakeCatalog(),
    )
    crash = _CrashOnce(boundary)
    executor = OrdinaryHdlSerialExecutor(
        invoke=lambda *_args, **_kwargs: None,  # type: ignore[arg-type]
        execution_factory=lambda _options: FakeExecution(),  # type: ignore[arg-type,return-value]
        publication_checkpoint=crash,
    )
    invocation = tmp_path / "reports" / "000001"
    invocation.mkdir(parents=True)
    request = NewCampaignRunRequest(
        plan, project, invocation.parent, CampaignPolicy(), invocation, _admission()
    )

    with pytest.raises(_InjectedProcessDeath, match=boundary):
        SimulationCampaign(executor).run(request)

    store = CampaignStore(invocation / "targets" / "sim" / "campaign")
    recovery = store.scan()
    assert recovery.complete == ()
    attempts = sorted(store.root.glob("work-items/*/attempts/*"))
    assert len(attempts) == 1
    result_path = attempts[0] / "private-build" / "build-result.json"
    result = json.loads(result_path.read_text()) if result_path.exists() else None
    assert result is None or result["state"] == expected_state
