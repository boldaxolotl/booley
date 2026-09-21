"""Serial durable coordinator behind the small Simulation Campaign seam."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from booley.flows.endpoint_admission import AdmissionContext
from booley.flows.sim.campaign_reports import target_report_directory

from .codec import SimulationCampaignIntegrityError, encode_simulation_result
from .facts import AcceptanceFacts
from .model import SimulationCampaignManifest, SimulationCampaignPlan, SimulationResult
from .planning import WorkloadMismatch, compare_manifests, manifest_digest
from .resume import ValidatedResumeManifest
from .store import CampaignRecovery, CampaignStore


@dataclass(frozen=True, slots=True)
class CampaignPolicy:
    timeout_seconds: float | None = None
    no_kill: bool = False
    diagnostic: bool = False
    result_verbosity: str = "compact"

    def __post_init__(self) -> None:
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("campaign timeout must be positive")
        if self.result_verbosity not in {"compact", "full"}:
            raise ValueError("result verbosity must be compact or full")


@dataclass(frozen=True, slots=True)
class NewCampaignPreviewRequest:
    plan: SimulationCampaignPlan
    project_root: Path
    report_root: Path | None
    policy: CampaignPolicy


@dataclass(frozen=True, slots=True)
class ResumeCampaignPreviewRequest:
    validated: ValidatedResumeManifest
    current_plan: SimulationCampaignPlan
    project_root: Path
    report_root: Path | None
    policy: CampaignPolicy


CampaignPreviewRequest = NewCampaignPreviewRequest | ResumeCampaignPreviewRequest


@dataclass(frozen=True, slots=True)
class NewCampaignRunRequest(NewCampaignPreviewRequest):
    invocation_directory: Path
    admission: AdmissionContext


@dataclass(frozen=True, slots=True)
class ResumeCampaignRunRequest(ResumeCampaignPreviewRequest):
    invocation_directory: Path
    admission: AdmissionContext


CampaignRunRequest = NewCampaignRunRequest | ResumeCampaignRunRequest


@dataclass(frozen=True, slots=True)
class NewCampaignPreview:
    target: Mapping[str, object]
    required_suite: Mapping[str, object]
    build_variants: tuple[object, ...]
    planning_disclosures: tuple[object, ...]
    prerequisites: tuple[object, ...]
    work_items: tuple[object, ...]
    fingerprints: Mapping[str, object]
    effective_policy: CampaignPolicy
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResumeCampaignPreview:
    manifest_path: Path
    manifest_sha256: str
    completed: tuple[str, ...]
    interrupted: tuple[str, ...]
    pending: tuple[str, ...]
    required_bundle_variants: tuple[str, ...]
    effective_policy: CampaignPolicy
    mismatches: tuple[WorkloadMismatch, ...]


CampaignPreview = NewCampaignPreview | ResumeCampaignPreview


@dataclass(frozen=True, slots=True)
class WorkExecutionRequest:
    store: CampaignStore
    manifest: SimulationCampaignManifest
    work_item: Mapping[str, object]
    attempt_id: str
    attempt_ordinal: int
    attempt_directory: Path
    producer_invocation_id: int
    policy: CampaignPolicy
    admission: AdmissionContext
    project_root: Path


class SerialWorkExecutor(Protocol):
    """Private execution port; implementations publish attempt/build evidence."""

    def execute(self, request: WorkExecutionRequest) -> SimulationResult:
        """Return a validated result after publishing ``attempt.json`` and evidence."""
        ...


@dataclass(frozen=True, slots=True)
class CampaignOutcome:
    manifest_path: Path
    summary_path: Path
    target: Mapping[str, object]
    observations: tuple[Mapping[str, object], ...]
    aggregate_grade: str
    complete: bool
    coverage_reference: Mapping[str, object] | None
    acceptance_facts: AcceptanceFacts
    acceptance_ready: bool
    diagnostics: tuple[str, ...] = ()


class SimulationCampaign:
    """Plan/preview/run one Target's durable serial campaign."""

    def __init__(
        self,
        executor: SerialWorkExecutor | None = None,
        *,
        publication_checkpoint: Callable[[str], None] | None = None,
    ) -> None:
        self._executor = executor
        self._publication_checkpoint = publication_checkpoint or (lambda _boundary: None)

    def preview(self, request: CampaignPreviewRequest) -> CampaignPreview:
        if isinstance(request, NewCampaignPreviewRequest):
            return _new_preview(request)
        store = CampaignStore(request.validated.path.parent)
        mismatches = compare_manifests(
            request.validated.manifest, request.current_plan.manifest
        )
        recovery = store.scan()
        needed = _required_variants(request.validated.manifest, recovery)
        return ResumeCampaignPreview(
            request.validated.path,
            request.validated.sha256,
            recovery.complete,
            recovery.interrupted,
            recovery.pending,
            needed,
            request.policy,
            mismatches,
        )

    def run(self, request: CampaignRunRequest) -> CampaignOutcome:
        if self._executor is None:
            raise RuntimeError("SimulationCampaign requires a serial work executor")
        if isinstance(request, NewCampaignRunRequest):
            store = _new_store(request)
            self._publication_checkpoint("before:manifest_commit")
            store.publish_manifest(request.plan.manifest)
            self._publication_checkpoint("after:manifest_commit")
            manifest = request.plan.manifest
        else:
            store = CampaignStore(request.validated.path.parent)
            mismatches = compare_manifests(
                request.validated.manifest, request.current_plan.manifest
            )
            if mismatches:
                detail = "; ".join(item.message for item in mismatches)
                raise SimulationCampaignIntegrityError(
                    f"campaign workload mismatch: {detail}"
                )
            manifest = request.validated.manifest
        with store.mutation_lock():
            self._run_prerequisites(store, manifest, request, set())
            recovery = store.scan()
            self._run_pending(store, manifest, recovery, request)
            self._publication_checkpoint("before:summary_replace")
            summary = store.regenerate_summary()
            self._publication_checkpoint("after:summary_replace")
            return _outcome(store, manifest, summary)

    def _run_prerequisites(
        self,
        owner_store: CampaignStore,
        manifest: SimulationCampaignManifest,
        request: CampaignRunRequest,
        visited: set[str],
    ) -> None:
        entries = cast(tuple[Mapping[str, object], ...], manifest.document["prerequisites"])
        invocation = owner_store.root.parents[2]
        for entry in entries:
            campaign_id = cast(str, entry["campaign_id"])
            if campaign_id in visited:
                raise SimulationCampaignIntegrityError("cyclic prerequisite campaign")
            visited.add(campaign_id)
            reference = cast(Mapping[str, object], entry["manifest"])
            prerequisite_store = CampaignStore(
                (invocation / cast(str, reference["path"])).parent
            )
            prerequisite = prerequisite_store.load_manifest()
            with prerequisite_store.mutation_lock():
                self._run_prerequisites(
                    prerequisite_store, prerequisite, request, visited
                )
                recovery = prerequisite_store.scan()
                self._run_pending(
                    prerequisite_store, prerequisite, recovery, request
                )
                prerequisite_store.regenerate_summary()
            selected = prerequisite_store.scan()
            match = next(
                (
                    item
                    for item in selected.items
                    if item.work_item_id == entry["work_item_id"]
                ),
                None,
            )
            if match is None or match.result is None:
                raise SimulationCampaignIntegrityError(
                    "Cycle Count prerequisite did not publish its selected result"
                )

    def _run_pending(
        self,
        store: CampaignStore,
        manifest: SimulationCampaignManifest,
        recovery: CampaignRecovery,
        request: CampaignRunRequest,
    ) -> None:
        items = cast(tuple[Mapping[str, object], ...], manifest.document["work_items"])
        by_id = {cast(str, item["work_item_id"]): item for item in items}
        try:
            invocation = int(request.invocation_directory.name)
        except ValueError as exc:
            raise SimulationCampaignIntegrityError(
                "campaign invocation directory must end in a numeric id"
            ) from exc
        for recovered in recovery.items:
            if recovered.state == "complete":
                continue
            if request.admission.cancellation():
                break
            attempt_id = _new_uuid()
            ordinal, directory = store.allocate_attempt_directory(
                recovered.work_item_id, attempt_id
            )
            result = self._executor.execute(
                WorkExecutionRequest(
                    store,
                    manifest,
                    by_id[recovered.work_item_id],
                    attempt_id,
                    ordinal,
                    directory,
                    invocation,
                    request.policy,
                    request.admission,
                    request.project_root,
                )
            )
            document = result.document
            if (
                document["attempt_id"] != attempt_id
                or document["attempt_ordinal"] != ordinal
                or document["work_item_id"] != recovered.work_item_id
            ):
                raise SimulationCampaignIntegrityError(
                    "serial executor returned a result for another attempt"
                )
            store.scan()
            store.verify_result_evidence(directory, result)
            self._publication_checkpoint("before:simulation_result")
            store.publish_result(recovered.work_item_id, result)
            self._publication_checkpoint("after:simulation_result")
            self._publication_checkpoint("before:summary_replace")
            store.regenerate_summary()
            self._publication_checkpoint("after:summary_replace")


def _new_preview(request: NewCampaignPreviewRequest) -> NewCampaignPreview:
    document = request.plan.manifest.document
    return NewCampaignPreview(
        cast(Mapping[str, object], document["target"]),
        cast(Mapping[str, object], document["required_suite"]),
        cast(tuple[object, ...], document["build_variants"]),
        cast(tuple[object, ...], document["planning_disclosures"]),
        cast(tuple[object, ...], document["prerequisites"]),
        cast(tuple[object, ...], document["work_items"]),
        cast(Mapping[str, object], document["fingerprints"]),
        request.policy,
    )


def _new_store(request: NewCampaignRunRequest) -> CampaignStore:
    target = cast(Mapping[str, str], request.plan.manifest.document["target"])
    selector = target["selector"]
    if target["role"] == "cycle_count_baseline":
        selector = f"{selector}@baseline-{target['revision'][:12]}"
    directory = target_report_directory(
        request.invocation_directory, selector
    ) / "campaign"
    return CampaignStore(directory)


def _required_variants(
    manifest: SimulationCampaignManifest, recovery: CampaignRecovery
) -> tuple[str, ...]:
    pending = set(recovery.pending) | set(recovery.interrupted)
    items = cast(tuple[Mapping[str, str], ...], manifest.document["work_items"])
    return tuple(dict.fromkeys(item["build_variant_id"] for item in items if item["work_item_id"] in pending))


def _outcome(
    store: CampaignStore,
    manifest: SimulationCampaignManifest,
    summary: Mapping[str, object],
) -> CampaignOutcome:
    recovery = store.scan()
    facts, observations = _acceptance_facts(store, manifest, recovery)
    complete = cast(bool, summary["complete"])
    grade = cast(str, summary["aggregate_grade"])
    return CampaignOutcome(
        store.manifest_path,
        store.summary_path,
        cast(Mapping[str, object], manifest.document["target"]),
        observations,
        grade,
        complete,
        None,
        facts,
        _acceptance_ready(manifest, observations, complete, grade),
    )


def _acceptance_ready(
    manifest: SimulationCampaignManifest,
    observations: tuple[Mapping[str, object], ...],
    complete: bool,
    grade: str,
) -> bool:
    if not complete or grade == "error":
        return False
    suite = cast(Mapping[str, object], manifest.document["required_suite"])
    passing = {
        item["test"]
        for item in observations
        if item["execution"] == "completed"
        and item["functional"] == "pass"
        and item["assertions"] != "dirty"
    }
    if suite["default_invocation"] is True:
        return len(observations) == 1 and bool(passing)
    return set(cast(tuple[str, ...], suite["names"])).issubset(passing)


def _acceptance_facts(
    store: CampaignStore,
    manifest: SimulationCampaignManifest,
    recovery: CampaignRecovery,
) -> tuple[AcceptanceFacts, tuple[Mapping[str, object], ...]]:
    items = cast(tuple[Mapping[str, object], ...], manifest.document["work_items"])
    by_id = {item["work_item_id"]: item for item in items}
    consumed: list[dict[str, object]] = []
    observations: list[dict[str, object]] = []
    for recovered in recovery.items:
        if recovered.result is None:
            continue
        item = by_id[recovered.work_item_id]
        raw = encode_simulation_result(recovered.result)
        digest = "sha256:" + hashlib.sha256(raw.rstrip(b"\n")).hexdigest()
        result_path = store.work_item_directory(recovered.work_item_id) / "result.json"
        invocation = store.root.parents[2]
        relative = result_path.relative_to(invocation).as_posix()
        document = recovered.result.document
        reference = {
            "path_base": "origin_invocation",
            "path": relative,
            "bytes": len(raw),
            "sha256": digest,
            "kind": "simulation_result",
            "owner": document["attempt_id"],
        }
        consumed.append(
            {
                "work_item_id": recovered.work_item_id,
                "role": item["role"],
                "revision": item["revision"],
                "target": item["target"],
                "attempt_id": document["attempt_id"],
                "result": reference,
                "finished_at": document["finished_at"],
            }
        )
        for observation in cast(tuple[Mapping[str, object], ...], document["observations"]):
            observations.append(
                {
                    "work_item_id": recovered.work_item_id,
                    "role": item["role"],
                    "revision": item["revision"],
                    "target": item["target"],
                    "result_sha256": digest,
                    **dict(observation),
                }
            )
    suite = cast(Mapping[str, object], manifest.document["required_suite"])
    prerequisite_facts = _prerequisite_facts(store, manifest)
    facts = AcceptanceFacts(
        {
            "$schema": "booley.simulation-acceptance-facts/v1",
            "campaign_id": manifest.document["campaign_id"],
            "manifest_sha256": manifest_digest(manifest),
            "origin": manifest.document["origin"],
            "target": manifest.document["target"],
            "required_suite": {
                "names": suite["names"],
                "default_invocation": suite["default_invocation"],
                "source_sha256": suite["source_sha256"],
            },
            "prerequisites": prerequisite_facts,
            "consumed_results": consumed,
            "observations": observations,
            "coverage_reference": None,
        }
    )
    return facts, tuple(observations)


def _prerequisite_facts(
    owner_store: CampaignStore,
    manifest: SimulationCampaignManifest,
) -> list[dict[str, object]]:
    facts: list[dict[str, object]] = []
    entries = cast(tuple[Mapping[str, object], ...], manifest.document["prerequisites"])
    invocation = owner_store.root.parents[2]
    for entry in entries:
        manifest_reference = cast(Mapping[str, object], entry["manifest"])
        store = CampaignStore(
            (invocation / cast(str, manifest_reference["path"])).parent
        )
        selected = next(
            (
                item
                for item in store.scan().items
                if item.work_item_id == entry["work_item_id"]
            ),
            None,
        )
        if selected is None or selected.result is None:
            raise SimulationCampaignIntegrityError(
                "Cycle Count prerequisite result is unavailable"
            )
        raw = encode_simulation_result(selected.result)
        digest = "sha256:" + hashlib.sha256(raw.rstrip(b"\n")).hexdigest()
        result_path = store.work_item_directory(selected.work_item_id) / "result.json"
        result_reference = {
            "path_base": "origin_invocation",
            "path": result_path.relative_to(invocation).as_posix(),
            "bytes": len(raw),
            "sha256": digest,
            "kind": "simulation_result",
            "owner": selected.result.document["attempt_id"],
        }
        observation = next(
            (
                item
                for item in cast(
                    tuple[Mapping[str, object], ...],
                    selected.result.document["observations"],
                )
                if item["cycle_count"] is not None
                and item["execution"] == "completed"
                and item["functional"] == "pass"
                and item["assertions"] != "dirty"
            ),
            None,
        )
        if observation is None:
            raise SimulationCampaignIntegrityError(
                "Cycle Count prerequisite has no cycle observation"
            )
        facts.append(
            {
                "role": "cycle_count_baseline",
                "manifest": manifest_reference,
                "campaign_id": entry["campaign_id"],
                "target": entry["target"],
                "work_item_id": entry["work_item_id"],
                "result": result_reference,
                "cycle_observation": {
                    "test": observation["test"],
                    "cycle_count": observation["cycle_count"],
                    "unit": "cycles",
                },
            }
        )
    return facts


def _new_uuid() -> str:
    import uuid

    return str(uuid.uuid4())


__all__ = [
    "CampaignOutcome",
    "CampaignPolicy",
    "CampaignPreview",
    "CampaignPreviewRequest",
    "CampaignRunRequest",
    "NewCampaignPreview",
    "NewCampaignPreviewRequest",
    "NewCampaignRunRequest",
    "ResumeCampaignPreview",
    "ResumeCampaignPreviewRequest",
    "ResumeCampaignRunRequest",
    "SerialWorkExecutor",
    "SimulationCampaign",
    "WorkExecutionRequest",
]
