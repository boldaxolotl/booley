"""Goal-compatible coverage/waiver service composed outside the Goal domain.

Reuse strict Campaign evaluation, promotion byte builders and directory-owned
candidate storage. No Ticket review, all-criteria prerequisite or Acceptance
Journal enters this immediate, recoverable live-promotion lifecycle.
"""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal, cast

from booley.core.file_lock import release_file_lock, wait_for_file_lock
from booley.criteria.state import CriterionEntry, DevelopmentState
from booley.flows.sim.coverage_campaign import DurableTargetIdentity, encode_coverage_campaign
from booley.flows.sim.coverage_policy import (
    CoverageThreshold,
)
from booley.flows.sim.coverage_projection import project_coverage_criterion
from booley.flows.sim.coverage_provisional import (
    criterion_from_evaluation,
    evaluate_provisional_coverage,
)
from booley.flows.sim.coverage_reference import (
    ResolvedCoverageCampaign,
    resolve_persisted_coverage_campaign_reference,
)
from booley.flows.sim.coverage_waiver_application import (
    WaiverPromotionPlan,
    evaluation_json,
    load_promoted_waiver_set,
    strict_reevaluation,
)
from booley.flows.sim.coverage_waiver_promotion import (
    ApprovalStamps,
    WaiverCandidateEvidence,
    build_promotion,
    render_approval_document,
    review_proof_path,
    waiver_file_path,
)
from booley.flows.sim.coverage_waivers import (
    ApprovedWaiverSet,
    CoverageRepositoryRoots,
    load_approved_waiver_set,
)
from booley.goals.apply_effects import FileEffect, read_bytes
from booley.goals.derivation import CoverageEvaluation, fresh_observation, selected_observations
from booley.goals.model import GoalRecord, GoalSpec
from booley.goals.paths import record_paths
from booley.goals.proposals import Decision, Proposal, ProposalError, digest
from booley.goals.state_store import load_goal_state
from booley.goals.store import GoalStore
from booley.goals.waiver_policy import (
    WAIVER_POLICY_DETAIL_KEY,
    policy_location,
    waiver_policy_fingerprint,
)
from booley.runtime.project_dir import resolve_checkout_project_dir
from booley.targets.catalog import TargetCatalog
from booley.ticket_board import waiver_candidates as candidates


class GoalWaiverService:
    """One resolved record's checkout/Campaign/candidate composition."""

    def __init__(self, project_dir: Path, record: GoalRecord) -> None:
        self.project_dir = project_dir
        self.record = record
        self.root = Path(record.worktree_path)
        self.paths = record_paths(project_dir, record.id)

    def _roots(self) -> CoverageRepositoryRoots:
        return CoverageRepositoryRoots(self.root, resolve_checkout_project_dir(self.root))

    def _targets(self) -> tuple[DurableTargetIdentity, ...]:
        return tuple(
            DurableTargetIdentity(item.identity) for item in TargetCatalog.build(self.root).list()
        )

    def _resolved(self, detail: Mapping[str, Any]) -> ResolvedCoverageCampaign:
        reference = detail.get("coverage_campaign_reference")
        if not isinstance(reference, Mapping):
            raise ProposalError("coverage evidence has no authenticated Campaign reference")
        return resolve_persisted_coverage_campaign_reference(
            self.paths.runtime_dir / "flow-reports", cast("Mapping[str, Any]", reference)
        )

    def _current_waivers(self) -> ApprovedWaiverSet:
        config, _anchor = policy_location(self.root)
        return load_approved_waiver_set(config, self._roots(), self._targets())

    def _validate_campaign(self, detail: Mapping[str, Any]) -> ResolvedCoverageCampaign:
        resolved = self._resolved(detail)
        if (
            encode_coverage_campaign(resolved.loaded.campaign)["evaluation"]
            != detail.get("evaluation")
            and "goal_derivation" not in detail
        ):
            raise ProposalError("Campaign evaluation differs from selected producer evidence")
        current = self._current_waivers()
        expected = cast("Mapping[str, Any]", detail.get("evaluation") or {}).get(
            "approved_waiver_set_digest"
        )
        if expected != current.digest:
            raise ProposalError("approved waiver policy changed since the producer observation")
        return resolved

    def bind(self, record: GoalRecord, goal_key: str, candidate_id: str) -> dict[str, Any]:
        """Bind one exact candidate to a selected producer Campaign and current policy."""
        state = load_goal_state(GoalStore(self.project_dir), record)
        row = selected_observations(record, state, self.project_dir).get(goal_key)
        if row is None:
            raise ProposalError("waiver proposal requires selected immutable coverage evidence")
        item = self._candidate(candidate_id)
        resolved = self._validate_campaign(row["detail"])
        goal = next(item.spec for item in record.goals if item.spec.key == goal_key)
        if not fresh_observation(goal, row, state, self.root):
            raise ProposalError("selected coverage producer evidence is stale")
        self._validate_candidate(item, resolved, row["detail"])
        config, anchor = policy_location(self.root)
        if config is None:
            raise ProposalError("[coverage.waivers] is not configured")
        approval_dir = anchor / config.directory
        self._refuse_protected(approval_dir, record)
        return {
            "candidate_id": candidate_id,
            "candidate": item.to_json(),
            "candidate_digest": digest(item.to_json()),
            "goal_key": goal_key,
            "campaign_reference": row["detail"]["coverage_campaign_reference"],
            "target_surface": row["detail"]
            .get("_source_fingerprint", {})
            .get("fingerprint", {})
            .get("target_surface"),
            "policy": waiver_policy_fingerprint(self.root),
            "approval_root": str(approval_dir.absolute()),
            "anchor_root": str(anchor.absolute()),
            "anchor": config.anchor,
            "directory": config.directory,
            "candidate_root": str(self.paths.root),
            "record_id": record.id,
        }

    def _candidate(self, candidate_id: str) -> candidates.WaiverCandidate:
        record = candidates.load(self.paths.root, self.record.id, directory=self.paths.root)
        found = [item for item in record.candidates.values() if item.candidate_id == candidate_id]
        if len(found) != 1:
            raise ProposalError(
                "the exact selected Waiver Candidate is missing or already rejected"
            )
        return found[0]

    def _validate_candidate(
        self,
        item: candidates.WaiverCandidate,
        resolved: ResolvedCoverageCampaign,
        detail: Mapping[str, Any],
    ) -> None:
        binding = item.binding
        summary = resolved.loaded.summary
        reference = cast("Mapping[str, Any]", detail["coverage_campaign_reference"])
        if (
            binding.campaign_path != reference["path"]
            or binding.campaign_id != resolved.loaded.campaign.campaign_id
            or binding.manifest_sha256 != summary.manifest_sha256
            or summary.point_store is None
            or binding.point_store_sha256 != summary.point_store.sha256
        ):
            raise ProposalError(
                "candidate was replaced or belongs to another Campaign/point store"
            )
        source = self.root / item.proposal.source
        source_digest = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
        verdict = evaluate_provisional_coverage(
            resolved.loaded.campaign,
            criterion_from_evaluation(resolved.loaded.campaign),
            (item.as_provisional(),),
            current_sources={item.proposal.source: source_digest},
        )
        if item.candidate_id not in verdict.offered_ids:
            raise ProposalError("candidate is not eligible for this exact source/Campaign point")

    def _refuse_protected(self, destination: Path, record: GoalRecord) -> None:
        from booley.goals.protected_inputs import ProtectedInputRoots

        roots = ProtectedInputRoots(Path(record.worktree_path), self.project_dir)
        # Persisted resolved paths are authoritative; decode through their recorded roots.
        from booley.goals.protected_inputs import ProtectedPath

        for encoded in record.protected_paths:
            path = roots.absolute(ProtectedPath.decode(encoded), roots.main_checkout())
            if path is not None and (destination == path or path in destination.parents):
                raise ProposalError(f"waiver destination is a protected input: {destination}")

    @contextmanager
    def locking(self, binding: Mapping[str, Any]) -> Generator[None]:
        """Global order: Goal record -> shared approval storage -> candidate storage."""
        approval = Path(str(binding["approval_root"]))
        lock_path = approval.parent / f".{approval.name}.booley-approval.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as handle:
            wait_for_file_lock(handle, timeout_s=30)
            try:
                with candidates.candidate_lock(self.paths.root, self.record.id):
                    yield
            finally:
                release_file_lock(handle)

    def _revalidate(
        self, proposal: Proposal
    ) -> tuple[candidates.WaiverCandidate, ResolvedCoverageCampaign, dict[str, Any]]:
        assert proposal.waiver is not None
        binding = proposal.waiver
        if binding["policy"] != waiver_policy_fingerprint(self.root):
            raise ProposalError(
                "waiver policy changed since proposal creation; create a new proposal"
            )
        item = self._candidate(str(binding["candidate_id"]))
        if digest(item.to_json()) != binding["candidate_digest"]:
            raise ProposalError("selected candidate was replaced after proposal creation")
        state = load_goal_state(GoalStore(self.project_dir), self.record)
        row = selected_observations(self.record, state, self.project_dir).get(proposal.goal_key)
        if (
            row is None
            or row["detail"].get("coverage_campaign_reference") != binding["campaign_reference"]
        ):
            raise ProposalError("selected coverage Campaign changed since proposal creation")
        resolved = self._validate_campaign(row["detail"])
        if not fresh_observation(proposal.before or proposal.after, row, state, self.root):
            raise ProposalError("selected coverage producer evidence became stale")
        self._validate_candidate(item, resolved, row["detail"])
        self._refuse_protected(Path(str(binding["approval_root"])), self.record)
        return item, resolved, row["detail"]

    def prepare(
        self, proposal: Proposal, decision: Decision, state: DevelopmentState
    ) -> tuple[tuple[FileEffect, ...], CoverageEvaluation]:
        """Capture live file effects and predict strict Goal evidence with the real loader."""
        before_policy = waiver_policy_fingerprint(self.root)
        item, _resolved, _detail = self._revalidate(proposal)
        assert proposal.waiver is not None
        evidence = self._evidence(item)
        stamps = ApprovalStamps(
            decision.session_key or "unknown",
            decision.at,
            f"goal:{proposal.record_id}/proposal:{proposal.id}",
        )
        plan = WaiverPromotionPlan(
            str(proposal.waiver["anchor"]), str(proposal.waiver["directory"]), stamps, (evidence,)
        )
        effects = self._promotion_effects(plan, proposal)
        with tempfile.TemporaryDirectory(prefix="booley-goal-waiver-") as temporary:
            waivers = load_promoted_waiver_set(
                plan, self._roots(), Path(temporary), self._targets()
            )
        policy = waiver_policy_fingerprint(
            self.root, overrides={effect.path: effect.after for effect in effects}
        )

        if before_policy != waiver_policy_fingerprint(self.root):
            raise ProposalError("approved waiver policy changed during promotion preparation")

        def evaluate(goal: GoalSpec, detail: Mapping[str, Any]) -> tuple[bool, dict[str, Any]]:
            resolved = self._validate_campaign(detail)
            result = self._reevaluate(goal, resolved, waivers)
            return self._project(goal, detail, result, policy)

        return effects, evaluate

    @staticmethod
    def _evidence(item: candidates.WaiverCandidate) -> WaiverCandidateEvidence:
        return WaiverCandidateEvidence(
            item.candidate_id,
            item.binding.campaign_id,
            item.binding.manifest_sha256,
            item.binding.point_store_sha256,
            item.binding.target_identity,
            item.proposal.point_id,
            item.proposal.source,
            item.proposal.source_sha256,
            cast('Literal["excluded", "unreachable"]', item.proposal.reason),
            item.proposal.justification,
            item.proposal.evidence_refs,
            item.proposal_count,
        )

    def _promotion_effects(
        self, plan: WaiverPromotionPlan, proposal: Proposal
    ) -> tuple[FileEffect, ...]:
        assert proposal.waiver is not None
        root = Path(str(proposal.waiver["approval_root"]))
        item = plan.candidates[0]
        promotion = build_promotion(item, plan.stamps)
        path = root / waiver_file_path(item.source)
        before = read_bytes(path)
        after = render_approval_document(
            before,
            source=item.source,
            source_sha256=item.source_sha256,
            records=(promotion.record,),
        )
        effects = [FileEffect(path, before, after, "waiver")]
        if promotion.proof is not None:
            proof = root / review_proof_path(item.waiver_id)
            effects.append(FileEffect(proof, read_bytes(proof), promotion.proof, "waiver"))
        return tuple(effects)

    def rejection(self, proposal: Proposal, decision: Decision) -> tuple[FileEffect, ...]:
        """Persist exact candidate rejection, even if the proposal otherwise conflicts."""
        assert proposal.waiver is not None
        item = proposal.waiver["candidate"]
        record = candidates.load(self.paths.root, self.record.id, directory=self.paths.root)
        rejection = candidates.Rejection(
            str(item["target_identity"]),
            str(item["point_id"]),
            str(item["source_sha256"]),
            decision.at,
            decision.session_key or "unknown",
        )
        updated = candidates.rejected_record(record, (rejection,))
        path = self.paths.root / "waiver-candidates.json"
        return (FileEffect(path, read_bytes(path), updated.to_bytes(), "candidate"),)

    def validate_effect(self, binding: Mapping[str, Any], effect: FileEffect) -> None:
        """Recovery uses captured anchors; drift does not authorize new destinations."""
        expected = self.paths.root / "waiver-candidates.json"
        if effect.label == "candidate" and effect.path == expected:
            return
        root = Path(str(binding["approval_root"]))
        if effect.label != "waiver" or effect.path == root:
            raise ProposalError("invalid captured waiver effect")
        item = binding["candidate"]
        allowed = {root / waiver_file_path(str(item["source"]))}
        if item["reason"] == "unreachable":
            allowed.add(root / review_proof_path(str(binding["candidate_id"])))
        if effect.path not in allowed:
            raise ProposalError("waiver effect does not name the bound approval/proof")
        self._refuse_protected(effect.path, self.record)

    def evaluate(self, goal: GoalSpec, detail: Mapping[str, Any]) -> tuple[bool, dict[str, Any]]:
        """Numeric coverage relaxation re-evaluates strict points, never old percentages."""
        resolved = self._validate_campaign(detail)
        waivers, policy = self._checked_waivers()
        result = self._reevaluate(goal, resolved, waivers)
        return self._project(goal, detail, result, policy)

    def _checked_waivers(self) -> tuple[ApprovedWaiverSet, dict[str, Any]]:
        """Retain the exact bytes checked alongside the semantic loader."""
        before = waiver_policy_fingerprint(self.root)
        waivers = self._current_waivers()
        if before != waiver_policy_fingerprint(self.root):
            raise ProposalError("approved waiver policy changed during derivation validation")
        return waivers, before

    @staticmethod
    def _reevaluate(
        goal: GoalSpec, resolved: ResolvedCoverageCampaign, waivers: ApprovedWaiverSet
    ) -> Mapping[str, Any]:
        """Reset previous annotations and apply today's declared Goal semantics."""
        campaign = resolved.loaded.campaign
        original = criterion_from_evaluation(campaign)
        thresholds = tuple(
            CoverageThreshold(key, Fraction(str(value["min_pct"])))
            for key, value in goal.params["metrics"].items()
        )
        tests = goal.params["tests"]
        criterion = replace(
            original, thresholds=thresholds, tests=None if tests == "all" else tuple(tests)
        )
        return evaluation_json(strict_reevaluation(campaign, waivers, criterion=criterion))

    @staticmethod
    def _project(
        goal: GoalSpec,
        detail: Mapping[str, Any],
        result: Mapping[str, Any],
        policy: Mapping[str, Any],
    ) -> tuple[bool, dict[str, Any]]:
        enriched = {
            **detail,
            "evaluation": dict(result),
            "criterion_fingerprint": result.get("criterion_fingerprint"),
            WAIVER_POLICY_DETAIL_KEY: dict(policy),
        }
        return project_coverage_criterion(
            CriterionEntry(params=goal.params),
            result,
            enriched,
            atomic="criterion_metric" in detail,
        )
