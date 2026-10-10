"""Goal freshness: met evidence that no longer describes the worktree (ADR 0067 D6, B5, B9).

:func:`evaluate_goal_freshness` wraps the shared
:func:`booley.criteria.freshness.evaluate_verification_freshness` with the
Goal policy. The shared comparison iterates only the categories a stamp
names, so a stamp that omits one would stay fresh forever; the Goal policy
instead requires, per family, today's categories plus ``target_surface``
(:func:`goal_required_categories`; the shared table in
``criteria.categories`` is untouched, so Ticket freshness is unchanged):

- each required category must be named by the stamp and carry a digest, and
  must resolve now; a missing, legacy, or unresolvable digest is ``stale``
  with a reason;
- any required digest that differs from the current one is ``stale``;
- a review Goal is stale when either its Reviewer receipt or the Goal source
  comparison says so;
- a simulation Goal is also stale when the suite its evidence recorded
  differs from the suite its Target resolves to now (:mod:`booley.goals.simulation`).

Only met evidence can be stale; an unmet Goal is reported as not stale.
Resolution goes through :class:`GoalFreshnessResolvers`, which the recorder
also uses to stamp evidence, so stamping and checking always agree.
"""

from __future__ import annotations

import copy
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from booley.criteria.categories import verification_fingerprint_categories
from booley.criteria.freshness import VerificationFreshness, evaluate_verification_freshness
from booley.evidence.fields import SOURCE_FINGERPRINT_DETAIL_KEY
from booley.flows.source_fingerprint import compute_source_fingerprint
from booley.goals.generated_artifacts import (
    generated_artifact_paths,
    goal_target_surface,
    project_source,
)
from booley.goals.model import GoalFamily, GoalSpec
from booley.goals.simulation import GOAL_SUITE_DETAIL_KEY, resolved_simulation_suite
from booley.goals.target_surface import (
    TARGET_SURFACE_CATEGORY,
    TargetSurfaceError,
    target_surface_fingerprint,
)
from booley.goals.waiver_policy import WAIVER_POLICY_DETAIL_KEY, waiver_policy_fingerprint
from booley.targets.domain import FuseSocError

#: Errors a resolver may raise; each makes the evidence stale rather than fresh.
RESOLVER_ERRORS: tuple[type[Exception], ...] = (
    FuseSocError,
    TargetSurfaceError,
    OSError,
    ValueError,
    tomllib.TOMLDecodeError,
)


@dataclass(frozen=True)
class GoalFreshnessResolvers:
    """How the current inputs are resolved; replaceable for tests."""

    source: Callable[..., dict[str, Any]] = compute_source_fingerprint
    target_surface: Callable[[Path, str | None], dict[str, Any]] = goal_target_surface
    simulation_suite: Callable[[Path, str], tuple[str, ...]] = resolved_simulation_suite
    waiver_policy: Callable[[Path], dict[str, Any]] = waiver_policy_fingerprint
    waiver_semantics: Callable[[Path], str] | None = None

    artifact_paths: Callable[[Path], frozenset[Path]] = generated_artifact_paths
    policy_surface: Callable[..., dict[str, Any]] | None = None

    def source_fingerprint(
        self, work_dir: Path, *, target: str | None, excluded: frozenset[Path] | None = None
    ) -> dict[str, Any]:
        """Sample producer sources once at the Goal freshness boundary."""
        current = dict(self.source(work_dir, target=target))
        if target is None and self.source is compute_source_fingerprint:
            selected = self.artifact_paths(work_dir) if excluded is None else excluded
            return project_source(work_dir, current, selected)
        return current

    def surface_fingerprint(
        self, work_dir: Path, target: str | None, excluded: frozenset[Path] | None = None
    ) -> dict[str, Any]:
        """Resolve a surface using the same immutable operation selection."""
        if self.policy_surface is not None or self.target_surface in (
            goal_target_surface,
            target_surface_fingerprint,
        ):
            selected = (
                (self.artifact_paths(work_dir) if target is None else frozenset())
                if excluded is None
                else excluded
            )
            provider = self.policy_surface or target_surface_fingerprint
            return provider(work_dir, target, artifact_paths=selected)
        return self.target_surface(work_dir, target)

    def select_artifacts(self, work_dir: Path, target: str | None) -> frozenset[Path]:
        """Select once only for production providers; custom providers own their values."""
        if target is None and (
            self.source is compute_source_fingerprint
            or self.target_surface in (goal_target_surface, target_surface_fingerprint)
            or self.policy_surface is not None
        ):
            return self.artifact_paths(work_dir)
        return frozenset()

    def fingerprint(self, work_dir: Path, *, target: str | None) -> dict[str, Any]:
        """The current source fingerprint plus the ``target_surface`` entry."""
        excluded = self.select_artifacts(work_dir, target)
        current = self.source_fingerprint(work_dir, target=target, excluded=excluded)
        current[TARGET_SURFACE_CATEGORY] = self.surface_fingerprint(work_dir, target, excluded)
        return current


DEFAULT_RESOLVERS = GoalFreshnessResolvers()


def goal_required_categories(key: str) -> frozenset[str]:
    """The categories Goal evidence for *key* must stamp; empty when it has none."""
    base = verification_fingerprint_categories(key)
    return frozenset({*base, TARGET_SURFACE_CATEGORY}) if base else frozenset()


def _is_simulation(key: str, goal: GoalSpec | None) -> bool:
    return goal.family is GoalFamily.SIM if goal is not None else key.startswith("sim_pass_")


def _stamp_target(stamp: Mapping[str, Any], goal: GoalSpec | None) -> str | None:
    """The Target a stamp describes: the Goal's own when known, else the stamp's."""
    if goal is not None:
        return goal.target
    target = stamp.get("target")
    return target if isinstance(target, str) and target else None


# ---------------------------------------------------------------------------
# Stamping (the recorder's side)
# ---------------------------------------------------------------------------


def stamp_goal_detail(
    key: str,
    detail: Mapping[str, Any],
    *,
    work_dir: Path,
    goal: GoalSpec | None,
    resolvers: GoalFreshnessResolvers = DEFAULT_RESOLVERS,
    target_surface: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Complete *detail* with the Goal surface, preserving producer digests (D6).

    Resolver failures stamp an error and read stale. A supplied *target_surface*
    describes the already validated declaration and avoids another read.
    """
    stamped = copy.deepcopy(dict(detail))
    if key.startswith("coverage_"):
        try:
            stamped[WAIVER_POLICY_DETAIL_KEY] = _producer_waiver_policy(
                detail, work_dir, resolvers
            )
        except RESOLVER_ERRORS as exc:
            stamped[WAIVER_POLICY_DETAIL_KEY] = {"error": str(exc)}
    raw = stamped.get(SOURCE_FINGERPRINT_DETAIL_KEY)
    stamp: dict[str, Any] = cast("dict[str, Any]", raw) if isinstance(raw, dict) else {}
    target = _stamp_target(stamp, goal)
    stamp["target"] = target
    if target is None:
        resolvers, error = _frozen_resolvers(resolvers, work_dir)
        if error is not None:
            stamp.setdefault("fingerprint", error)
            target_surface = error
    if not isinstance(stamp.get("fingerprint"), dict):
        stamp = {
            "target": target,
            "categories": [],
            "fingerprint": _source_or_error(resolvers, work_dir, target),
        }
    categories = {*_str_list(stamp.get("categories")), *goal_required_categories(key)}
    categories.add(TARGET_SURFACE_CATEGORY)
    stamp["categories"] = sorted(categories)
    fingerprint = cast("dict[str, Any]", stamp["fingerprint"])
    if target_surface is not None:
        fingerprint[TARGET_SURFACE_CATEGORY] = dict(target_surface)
    else:
        fingerprint[TARGET_SURFACE_CATEGORY] = resolve_target_surface(resolvers, work_dir, target)
    stamped[SOURCE_FINGERPRINT_DETAIL_KEY] = stamp
    return stamped


def _frozen_resolvers(
    resolvers: GoalFreshnessResolvers, root: Path
) -> tuple[GoalFreshnessResolvers, dict[str, Any] | None]:
    """Freeze one classification for source/surface stamping without resampling."""
    try:
        excluded = resolvers.select_artifacts(root, None)
        return replace(resolvers, artifact_paths=lambda root: excluded), None
    except RESOLVER_ERRORS as exc:
        return resolvers, {"error": str(exc)}


def _producer_waiver_policy(
    detail: Mapping[str, Any], work_dir: Path, resolvers: GoalFreshnessResolvers
) -> dict[str, Any]:
    """Bind the producer's semantic digest to one checked file snapshot."""
    if resolvers.waiver_semantics is None:
        raise ValueError("coverage publication has no semantic waiver policy reader")
    before = resolvers.waiver_policy(work_dir)
    current = resolvers.waiver_semantics(work_dir)
    evaluation = detail.get("evaluation")
    if (
        not isinstance(evaluation, Mapping)
        or cast("Mapping[str, Any]", evaluation).get("approved_waiver_set_digest") != current
    ):
        raise ValueError("approved waiver policy differs from the producer evaluation")
    if before != resolvers.waiver_policy(work_dir):
        raise ValueError("approved waiver policy changed during publication validation")
    return before


def resolve_target_surface(
    resolvers: GoalFreshnessResolvers, work_dir: Path, target: str | None
) -> dict[str, Any]:
    """The ``target_surface`` fingerprint of *target*, or ``{"error": ...}`` when unresolvable."""
    try:
        return dict(resolvers.surface_fingerprint(work_dir, target))
    except RESOLVER_ERRORS as exc:
        return {"error": str(exc)}


def _source_or_error(
    resolvers: GoalFreshnessResolvers, work_dir: Path, target: str | None
) -> dict[str, Any]:
    try:
        return dict(resolvers.source_fingerprint(work_dir, target=target))
    except RESOLVER_ERRORS as exc:
        return {"error": str(exc)}


def _str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in cast("list[object]", value) if isinstance(item, str)]


# ---------------------------------------------------------------------------
# Checking (the status side)
# ---------------------------------------------------------------------------


def evaluate_goal_freshness(
    key: str,
    entry: Any,
    *,
    goal: GoalSpec | None,
    work_dir: Path,
    resolvers: GoalFreshnessResolvers = DEFAULT_RESOLVERS,
) -> VerificationFreshness:
    """Whether Goal *key*'s met evidence (*entry*) is stale in *work_dir* (module docstring).

    *goal* is the Goal as the record holds it (its family and bound Target,
    which is authoritative over the Target a stamp names), or ``None`` when
    only the key is known.

    This compares sources, the Target declaration, the Reviewer receipt, and
    the simulation suite only. It knows neither the record's protected-input
    baseline nor any specification revision, so a caller reporting Goal
    status or finishing must also check the protected inputs and HEAD
    against the record (``protected_input_violations``) and that the
    evidence was recorded under the Goal's current ``spec_revision``.
    """
    required = goal_required_categories(key)
    if not required or _entry_value(entry, "met") is not True:
        return VerificationFreshness(False)
    detail = _entry_detail(entry)
    stamp = detail.get(SOURCE_FINGERPRINT_DETAIL_KEY)
    defect = _stamp_defect(stamp, required)
    if defect:
        return _stale(required, f"{defect}; re-run the Goal's Flow or Specialist")
    return _compare_met_evidence(
        key, entry, cast("Mapping[str, Any]", stamp), required, goal, work_dir, resolvers
    )


def _compare_met_evidence(
    key: str,
    entry: Any,
    stamp_map: Mapping[str, Any],
    required: frozenset[str],
    goal: GoalSpec | None,
    work_dir: Path,
    resolvers: GoalFreshnessResolvers,
) -> VerificationFreshness:
    """Compare a well-formed stamp, the receipt, and the suite with the current inputs."""
    target = _stamp_target(stamp_map, goal)
    policy_reason = _waiver_policy_reason(key, entry, work_dir, resolvers)
    if policy_reason:
        return _stale(required, policy_reason)
    try:
        current = resolvers.fingerprint(work_dir, target=target)
    except RESOLVER_ERRORS as exc:
        return _stale(required, f"the Goal's inputs can no longer be resolved: {exc}")
    source = _compare_required(stamp_map, current, required)
    if source.stale:
        return source
    stamped_target = stamp_map.get("target")
    cache_key = stamped_target if isinstance(stamped_target, str) and stamped_target else None
    shared = evaluate_verification_freshness(
        key,
        entry,
        work_dir=work_dir,
        fingerprint_provider=resolvers.fingerprint,
        fingerprints={cache_key: current},
    )
    if shared is not None and shared.stale:
        return shared
    if _is_simulation(key, goal):
        suite = _suite_staleness(_entry_detail(entry), work_dir, target, resolvers)
        if suite:
            return _stale(required, suite, current)
    return source


def _waiver_policy_reason(
    key: str, entry: Any, work_dir: Path, resolvers: GoalFreshnessResolvers
) -> str:
    if not key.startswith("coverage_"):
        return ""
    try:
        current = resolvers.waiver_policy(work_dir)
    except RESOLVER_ERRORS as exc:
        return f"approved waiver policy cannot be read: {exc}"
    if _entry_detail(entry).get(WAIVER_POLICY_DETAIL_KEY) != current:
        return "approved waiver policy changed or has no recorded identity"
    return ""


def _stamp_defect(stamp: object, required: frozenset[str]) -> str:
    """Why *stamp* cannot prove freshness for *required*; empty when it can."""
    if not isinstance(stamp, Mapping):
        return "the evidence has no source fingerprint"
    stamp_map = cast("Mapping[str, Any]", stamp)
    fingerprint = stamp_map.get("fingerprint")
    if not isinstance(fingerprint, Mapping):
        return "the evidence has an invalid source fingerprint"
    omitted = sorted(required - set(_str_list(stamp_map.get("categories"))))
    if omitted:
        return f"the evidence does not stamp {', '.join(omitted)}"
    undigested = sorted(
        category
        for category in required
        if not isinstance(_digest(cast("Mapping[str, Any]", fingerprint), category), str)
    )
    if undigested:
        return f"the evidence has no digest for {', '.join(undigested)}"
    return ""


def _compare_required(
    stamp: Mapping[str, Any], current: Mapping[str, Any], required: frozenset[str]
) -> VerificationFreshness:
    previous = cast("Mapping[str, Any]", stamp["fingerprint"])
    unresolved = sorted(c for c in required if not isinstance(_digest(current, c), str))
    if unresolved:
        reason = f"{', '.join(unresolved)} can no longer be resolved; re-run the Goal"
        return _stale(required, reason, current)
    changed = tuple(sorted(c for c in required if _digest(previous, c) != _digest(current, c)))
    if changed:
        reason = (
            f"{', '.join(changed)} changed after the evidence was recorded; "
            "re-run the Goal's Flow or Specialist"
        )
        return VerificationFreshness(True, changed, reason, dict(current))
    return VerificationFreshness(False, current_source_fingerprint=dict(current))


def _suite_staleness(
    detail: Mapping[str, Any],
    work_dir: Path,
    target: str | None,
    resolvers: GoalFreshnessResolvers,
) -> str:
    recorded = _str_list(detail.get(GOAL_SUITE_DETAIL_KEY))
    if GOAL_SUITE_DETAIL_KEY not in detail:
        return "the evidence does not record the simulation suite it was judged against"
    if target is None:
        return "the simulation Goal has no bound Target"
    try:
        current = resolvers.simulation_suite(work_dir, target)
    except RESOLVER_ERRORS as exc:
        return f"the simulation suite can no longer be resolved: {exc}"
    if sorted(recorded) == sorted(current):
        return ""
    added = sorted(set(current) - set(recorded))
    removed = sorted(set(recorded) - set(current))
    changes = [
        f"added {', '.join(added)}" if added else "",
        f"removed {', '.join(removed)}" if removed else "",
    ]
    return f"the resolved simulation suite changed ({'; '.join(c for c in changes if c)})"


def _digest(fingerprint: Mapping[str, Any], category: str) -> object:
    value = fingerprint.get(category)
    return cast("Mapping[str, Any]", value).get("digest") if isinstance(value, Mapping) else None


def _stale(
    required: frozenset[str], reason: str, current: Mapping[str, Any] | None = None
) -> VerificationFreshness:
    return VerificationFreshness(True, tuple(sorted(required)), reason, dict(current or {}))


def _entry_value(entry: Any, name: str) -> Any:
    if isinstance(entry, Mapping):
        return cast("Mapping[str, Any]", entry).get(name)
    return getattr(entry, name, None)


def _entry_detail(entry: Any) -> Mapping[str, Any]:
    detail = _entry_value(entry, "detail")
    return cast("Mapping[str, Any]", detail) if isinstance(detail, Mapping) else {}
