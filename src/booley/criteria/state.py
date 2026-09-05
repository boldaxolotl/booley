"""Development state — criteria-based progress tracking for developer runs.

Each criterion is either unmet (absent/false) or met (true). The developer
agent invokes Flows and Specialists that set criteria; the harness reads the state file on exit
to decide ticket disposition.

Atomic writes use .tmp -> os.replace() for crash safety.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from booley.criteria.categories import CATEGORY_RTL, CATEGORY_TB
from booley.criteria.cycle_count import build_cycle_comparison
from booley.criteria.templates import BASELINE_TARGET_PARAM

# Threshold-evaluator helpers — re-exported for backward compatibility and also
# used directly by DevelopmentState's delta-check methods below.
from booley.criteria.threshold_eval import (
    _SYNTH_METRIC_MAP,
    _check_absolute_cap,
    _check_absolute_min,
    resolve_metric,
)
from booley.criteria.thresholds import CYCLE_COUNT_PARAMS, evaluate_cycle_threshold
from booley.flows.recipe_evidence import (
    BASELINE_RECIPE_FINGERPRINT_DETAIL,
    BASELINE_REF_DETAIL,
    BASELINE_REF_PARAM,
    BASELINE_TARGET_DETAIL,
    CANDIDATE_TARGET_DETAIL,
    RECIPE_FINGERPRINT_DETAIL,
    RECIPE_FINGERPRINT_PARAM,
    RECIPE_SNAPSHOT_DETAIL,
    RECIPE_SNAPSHOT_PARAM,
    implementation_comparison_basis,
    recipe_changes,
)
from booley.flows.source_fingerprint import (  # noqa: F401  # compatibility re-export
    SOURCE_FINGERPRINT_DETAIL_KEY,
    as_str_list,
    compute_source_fingerprint,
)
from booley.runtime.timefmt import utc_now_rfc3339

logger = logging.getLogger(__name__)


def _recipe_flow(baseline: Any, current: Any) -> str | None:
    """Return the implementation-flow label embedded in either snapshot."""
    for snapshot in (current, baseline):
        if isinstance(snapshot, dict) and isinstance(snapshot.get("flow"), str):
            return snapshot["flow"]
    return None


def _canonical_implementation(detail: dict[str, Any]) -> dict[str, Any] | None:
    implementation = detail.get("implementation")
    return implementation if isinstance(implementation, dict) else None


def _canonical_recipe_value(
    implementation: dict[str, Any] | None,
    section: str,
    key: str,
) -> Any:
    if implementation is None:
        return None
    value = implementation.get(section)
    return value.get(key) if isinstance(value, dict) else None


@dataclass
class CriterionEntry:
    """Single criterion with metadata."""

    met: bool = False
    mandatory: bool = True
    # Latching flag: True once the criterion has ever been met (survives reset)
    ever_met: bool = False
    # Mirror latch: True once a Flow actually *reported* this criterion as not
    # met. Distinct from `not ever_met` — a criterion nobody has run yet has
    # neither flag set. Lets a "fail -> pass" transition criterion tell a real
    # observed transition from a run that was green on the first try (F-53).
    ever_failed: bool = False
    # When True, reset_category() skips this criterion (protects TB1 during TB2 debug)
    locked: bool = False
    # Timestamp of last status change (ISO 8601)
    updated_at: str = ""
    # Freeform metadata from the Flow that set it (e.g. cell count, pass rate)
    detail: dict[str, Any] = field(default_factory=dict)
    # Threshold params from CriterionSpec (e.g. cell_count_max: 500)
    params: dict[str, Any] = field(default_factory=dict)
    # True when reset_category() invalidated this criterion (detail is stale)
    stale: bool = False
    # Ordered, append-only evidence for criteria that explicitly require a
    # fail -> pass transition. Unlike ever_failed, this retains the red detail
    # after the green run replaces ``detail``.
    transition_evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"met": self.met, "mandatory": self.mandatory}
        if self.ever_met:
            d["ever_met"] = True
        if self.ever_failed:
            d["ever_failed"] = True
        if self.locked:
            d["locked"] = True
        if self.updated_at:
            d["updated_at"] = self.updated_at
        if self.detail:
            d["detail"] = self.detail
        if self.params:
            d["params"] = self.params
        if self.stale:
            d["stale"] = True
        if self.transition_evidence:
            d["transition_evidence"] = self.transition_evidence
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CriterionEntry:
        return cls(
            met=d.get("met", False),
            mandatory=d.get("mandatory", True),
            ever_met=d.get("ever_met", False),
            ever_failed=d.get("ever_failed", False),
            locked=d.get("locked", False),
            updated_at=d.get("updated_at", ""),
            detail=d.get("detail", {}),
            params=d.get("params", {}),
            stale=d.get("stale", False),
            transition_evidence=d.get("transition_evidence", []),
        )


@dataclass(frozen=True)
class CriterionChange:
    """One normalized effective mutation of a declared Criterion."""

    key: str
    met: bool
    reason: str
    detail: dict[str, Any]
    mandatory: bool
    params: dict[str, Any]


@dataclass(frozen=True)
class _RecipeEvidence:
    complete: bool
    expected_recipe: Any
    actual_recipe: Any
    expected_ref: Any
    actual_ref: Any
    baseline_snapshot: Any
    current_snapshot: Any
    baseline_target: Any
    candidate_target: Any
    changes: list[dict[str, Any]]
    basis_changes: list[dict[str, Any]]


def _canonical_recipe_sections(
    detail: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any], dict[str, Any]]:
    implementation = _canonical_implementation(detail)
    comparison = implementation.get("comparison") if implementation else None
    comparison = comparison if isinstance(comparison, dict) else {}
    baseline = comparison.get("baseline")
    baseline = baseline if isinstance(baseline, dict) else {}
    recipe = baseline.get("recipe")
    return implementation, comparison, recipe if isinstance(recipe, dict) else {}


def _recipe_targets(
    entry: CriterionEntry,
    detail: dict[str, Any],
    implementation: dict[str, Any] | None,
    comparison: dict[str, Any],
) -> tuple[Any, Any]:
    identity = implementation.get("identity") if implementation else None
    identity = identity if isinstance(identity, dict) else {}
    candidate = comparison.get("candidate_target") or identity.get("target")
    candidate = candidate or detail.get(CANDIDATE_TARGET_DETAIL)
    baseline = comparison.get("baseline_target") or detail.get(BASELINE_TARGET_DETAIL)
    baseline = baseline or entry.params.get(BASELINE_TARGET_PARAM, candidate)
    return baseline, candidate


def _validate_recipe_snapshots(
    complete: bool,
    baseline_snapshot: Any,
    current_snapshot: Any,
    baseline_target: Any,
    candidate_target: Any,
) -> tuple[bool, list[dict[str, Any]], list[dict[str, Any]]]:
    if isinstance(baseline_snapshot, dict):
        complete = complete and isinstance(current_snapshot, dict)
        if isinstance(baseline_target, str):
            complete = complete and baseline_snapshot.get("target") == baseline_target
    if isinstance(current_snapshot, dict) and isinstance(candidate_target, str):
        complete = complete and current_snapshot.get("target") == candidate_target
    available = isinstance(baseline_snapshot, dict) and isinstance(current_snapshot, dict)
    changes = recipe_changes(baseline_snapshot, current_snapshot) if available else []
    directed = available and isinstance(baseline_target, str) and isinstance(candidate_target, str)
    basis_changes: list[dict[str, Any]] = []
    if directed and baseline_target != candidate_target:
        basis_changes = recipe_changes(
            implementation_comparison_basis(baseline_snapshot),
            implementation_comparison_basis(current_snapshot),
        )
        complete = complete and not basis_changes
    return complete, changes, basis_changes


def _collect_recipe_evidence(entry: CriterionEntry) -> _RecipeEvidence:
    detail = entry.detail
    implementation, comparison, baseline_recipe_detail = _canonical_recipe_sections(detail)
    expected_recipe = entry.params.get(RECIPE_FINGERPRINT_PARAM)
    actual_recipe = _canonical_recipe_value(implementation, "recipe", "fingerprint")
    actual_recipe = actual_recipe or detail.get(RECIPE_FINGERPRINT_DETAIL)
    expected_ref = entry.params.get(BASELINE_REF_PARAM)
    actual_ref = comparison.get("resolved_ref") or detail.get(BASELINE_REF_DETAIL)
    baseline_recipe = baseline_recipe_detail.get("fingerprint") or detail.get(
        BASELINE_RECIPE_FINGERPRINT_DETAIL
    )
    complete = actual_recipe is not None
    if expected_ref is not None:
        complete = complete and actual_ref == expected_ref and baseline_recipe == expected_recipe
    baseline_snapshot = entry.params.get(RECIPE_SNAPSHOT_PARAM)
    current_snapshot = _canonical_recipe_value(implementation, "recipe", "snapshot")
    current_snapshot = current_snapshot or detail.get(RECIPE_SNAPSHOT_DETAIL)
    baseline_target, candidate_target = _recipe_targets(entry, detail, implementation, comparison)
    complete, changes, basis_changes = _validate_recipe_snapshots(
        complete, baseline_snapshot, current_snapshot, baseline_target, candidate_target
    )
    return _RecipeEvidence(
        complete,
        expected_recipe,
        actual_recipe,
        expected_ref,
        actual_ref,
        baseline_snapshot,
        current_snapshot,
        baseline_target,
        candidate_target,
        changes,
        basis_changes,
    )


# Well-known category prefixes: criteria whose key starts with these
# are auto-tagged into the corresponding categories.
# A prefix may map to multiple categories — e.g. sim_ criteria depend on
# both RTL correctness and TB correctness, so RTL changes must also
# invalidate simulation results.
_CATEGORY_PREFIXES: dict[str, frozenset[str]] = {
    "lint_": frozenset({CATEGORY_RTL}),
    "synthesis_": frozenset({CATEGORY_RTL}),
    "fpga_impl_": frozenset({CATEGORY_RTL}),
    "sim_": frozenset({CATEGORY_RTL, CATEGORY_TB}),
    "cycle_count_": frozenset({CATEGORY_RTL, CATEGORY_TB}),
    "coverage_": frozenset({CATEGORY_RTL}),
    # Exact key (prefix matching still applies): the standalone-elaboration
    # sweep is an RTL structural check, so an RTL edit must reset its met
    # status — a stale green would defeat the every-attempt re-verification.
    "elaborate_standalone": frozenset({CATEGORY_RTL}),
    # review_rtl / review_tb are handled through _REVIEW_CATEGORY below so
    # their persisted receipt/findings survive synchronous invalidation.
}

# Review criteria belong to a category but are kept separate from ordinary
# prefix resets because their receipt/findings remain useful after they become
# stale. `_clean` reviews also track verify_attempts, which must be cleared when
# the underlying code changes.
_REVIEW_CATEGORY: dict[str, str] = {
    "review_tb_": CATEGORY_TB,
    "review_rtl_": CATEGORY_RTL,
}


def _infer_categories(key: str) -> frozenset[str]:
    """Infer categories from criterion key prefix (may be multiple)."""
    for prefix, cats in _CATEGORY_PREFIXES.items():
        if key.startswith(prefix):
            return cats
    return frozenset()


@dataclass
class DevelopmentState:
    """Criteria-based development state, persisted as JSON.

    Thread-safe via atomic file writes. Not designed for concurrent
    writers — the developer is single-threaded.
    """

    slug: str = ""
    ticket_type: str = ""
    # Ticket runs seal their criteria at intake. In strict mode, endpoint
    # results may update only those declared keys (or their aliases); silently
    # inventing an optional key can otherwise hide a wrong-Target invocation.
    # Standalone/human mode keeps the historical permissive behaviour.
    strict_criteria: bool = False
    criteria: dict[str, CriterionEntry] = field(default_factory=dict)
    # Category overrides: criterion_key -> category string
    category_map: dict[str, str] = field(default_factory=dict)
    # Generic Flow key → file-specific criteria keys.  Populated from
    # structured sim YAML entries so that set_criterion("sim_pass_default")
    # fans out to "sim_pass_verif_tb.sv_default" etc.
    flow_key_aliases: dict[str, list[str]] = field(default_factory=dict)
    # Flow/MCP endpoint execution timeline (append-only log)
    timeline: list[dict[str, Any]] = field(default_factory=list)
    # Last worktree/root used by an endpoint; final acceptance uses this for
    # source freshness checks when the harness does not pass a work_dir.
    work_dir: str = ""
    last_updated: str = ""

    _file_path: Path | None = field(default=None, repr=False)

    # --- Persistence ---

    @classmethod
    def load(cls, path: Path) -> DevelopmentState:
        """Load from disk or return empty state."""
        if not path.exists():
            st = cls()
            st._file_path = path
            return st
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            st = cls(
                slug=data.get("slug", ""),
                ticket_type=data.get("ticket_type", ""),
                strict_criteria=data.get("strict_criteria", False),
                criteria={
                    k: CriterionEntry.from_dict(v) for k, v in data.get("criteria", {}).items()
                },
                category_map=data.get("category_map", {}),
                flow_key_aliases=data.get("flow_key_aliases", {}),
                timeline=data.get("timeline", []),
                work_dir=data.get("work_dir", ""),
                last_updated=data.get("last_updated", ""),
            )
            st._file_path = path
            return st
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("Corrupted state file %s, starting fresh: %s", path, exc)
            st = cls()
            st._file_path = path
            return st

    def save(self) -> None:
        """Atomically write state to disk. No-op when no file path (human mode)."""
        if self._file_path is None:
            return
        self.last_updated = utc_now_rfc3339()
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._file_path.with_suffix(".tmp")
        tmp_path.write_text(
            json.dumps(self._to_dict(), indent=2),
            encoding="utf-8",
        )
        _atomic_replace(tmp_path, self._file_path)
        logger.debug("Saved development state for %s", self.slug)

    def _to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "slug": self.slug,
            "ticket_type": self.ticket_type,
            "strict_criteria": self.strict_criteria,
            "criteria": {k: v.to_dict() for k, v in self.criteria.items()},
            "category_map": self.category_map,
            "all_mandatory_met": self.all_mandatory_met(),
            "timeline": self.timeline,
            "last_updated": self.last_updated,
        }
        if self.work_dir:
            d["work_dir"] = self.work_dir
        if self.flow_key_aliases:
            d["flow_key_aliases"] = self.flow_key_aliases
        return d

    # --- Criteria operations ---

    def init_criteria(
        self,
        criteria: dict[str, bool],
        *,
        category_overrides: dict[str, str] | None = None,
        flow_key_aliases: dict[str, list[str]] | None = None,
        criterion_params: dict[str, dict[str, Any]] | None = None,
        strict: bool | None = None,
    ) -> None:
        """Initialize criteria from a {name: mandatory} dict. All start unmet.

        ``criterion_params`` maps criterion key -> threshold params (e.g. from
        CriterionSpec.params). Used by the synthesis_ok threshold evaluator.
        """
        now = utc_now_rfc3339()
        params_map = criterion_params or {}
        self.criteria = {
            k: CriterionEntry(
                met=False,
                mandatory=v,
                updated_at=now,
                params=params_map.get(k, {}),
            )
            for k, v in criteria.items()
        }
        if category_overrides:
            self.category_map.update(category_overrides)
        if flow_key_aliases:
            self.flow_key_aliases.update(flow_key_aliases)
        if strict is not None:
            self.strict_criteria = strict

    def set_criterion(
        self,
        key: str,
        met: bool,
        *,
        detail: dict[str, Any] | None = None,
    ) -> list[CriterionChange]:
        """Set a criterion's status. Creates it (as optional) if unknown.

        If *key* is not a known criterion but has aliases (file-specific
        keys registered via structured sim YAML entries), the update
        fans out to all aliased keys instead.
        """
        if key in self.criteria:
            return [self._set_criterion_entry(key, met, detail=detail)]

        # Fan out to file-specific aliases when the generic key isn't
        # a direct criterion (e.g. "sim_pass_default" → [...]).
        aliases = self.flow_key_aliases.get(key)
        if aliases:
            changes: list[CriterionChange] = []
            for alias_key in aliases:
                if alias_key in self.criteria and self._alias_matches_run(alias_key, detail):
                    changes.append(self._set_criterion_entry(alias_key, met, detail=detail))
                elif alias_key not in self.criteria:
                    logger.warning(
                        "Alias target %r (from %r) not in criteria",
                        alias_key,
                        key,
                    )
            return changes

        # Single-target fallback: "sim_pass_default" → "sim_pass" when
        # the ticket declared a flat scalar criterion and only one target exists.
        last_us = key.rfind("_")
        if last_us > 0:
            base_key = key[:last_us]
            if base_key in self.criteria:
                logger.info(
                    "Mapping %r to base criterion %r (single-target fallback)",
                    key,
                    base_key,
                )
                return [self._set_criterion_entry(base_key, met, detail=detail)]

        if self.strict_criteria:
            logger.error(
                "Rejecting undeclared criterion %r for basis-bound Ticket %r",
                key,
                self.slug,
            )
            return []

        # A Flow reported a criterion the ticket never declared (e.g. a bare
        # `simulate` run during setup/onboarding, where no `sim_pass_*` criterion
        # exists yet). Auto-creating it as optional is the intended, benign
        # behaviour — not a misconfiguration — so this stays at debug level to
        # avoid spamming a WARNING on every otherwise-clean run.
        logger.debug("Creating unknown criterion %r as optional", key)
        now = utc_now_rfc3339()
        self.criteria[key] = CriterionEntry(
            met=met,
            mandatory=False,
            ever_met=met,
            updated_at=now,
            detail=detail or {},
        )
        entry = self.criteria[key]
        return [
            CriterionChange(
                key,
                entry.met,
                "outcome",
                dict(entry.detail),
                entry.mandatory,
                dict(entry.params),
            )
        ]

    def _alias_matches_run(self, alias_key: str, detail: dict[str, Any] | None) -> bool:
        """Whether one structured simulation alias was evaluated by this run."""
        expected = self.criteria[alias_key].params.get("test_selector")
        if not expected or not detail or "test_selector" not in detail:
            return True
        actual = detail.get("test_selector")
        if expected == "all":
            return actual == "all"
        selected = detail.get("selected_tests", [])
        return expected == actual or expected in selected

    def _set_criterion_entry(
        self,
        key: str,
        met: bool,
        *,
        detail: dict[str, Any] | None = None,
    ) -> CriterionChange:
        """Update an existing criterion entry.

        After setting met/detail, evaluates threshold params (if any) against
        the reported metrics. Threshold failures override met=False.

        Defensive structural check: a criterion that ships ``detail["pending"]``
        is reporting an open finding list. Setting ``met=True`` while that
        list still contains items is structurally impossible — it can only
        happen if the producing Flow miscounted or a malicious/buggy LLM
        verdict slipped through. We force ``met=False`` and log loudly so
        the bad call site is debuggable.
        """
        now = utc_now_rfc3339()
        if met and detail:
            pending = detail.get("pending")
            if isinstance(pending, list) and pending:
                logger.error(
                    "Refusing met=True for %r: %d pending finding(s) in "
                    "detail. Forcing met=False — investigate the producing "
                    "Flow, this should never happen.",
                    key,
                    len(pending),
                )
                met = False
        entry = self.criteria[key]
        entry.met = met
        entry.stale = False
        if met:
            entry.ever_met = True
        else:
            # Only Flow-reported verdicts reach here; init_criteria builds
            # entries directly, so an un-run criterion never latches this.
            entry.ever_failed = True
        entry.updated_at = now
        if detail:
            entry.detail = detail

        # Threshold evaluation for criteria with params (synthesis_ok, fpga_impl_ok, etc.)
        if entry.params and detail:
            self._evaluate_thresholds(entry)
        if entry.params.get("from_state") == "fail":
            entry.transition_evidence.append(
                {
                    "met": entry.met,
                    "recorded_at": now,
                    "detail": dict(entry.detail or {}),
                }
            )
        return CriterionChange(
            key,
            entry.met,
            "outcome",
            dict(entry.detail),
            entry.mandatory,
            dict(entry.params),
        )

    # -- Threshold evaluation --------------------------------------------------

    @staticmethod
    def _threshold_evidence(
        detail: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, str], set[str]]:
        implementation = _canonical_implementation(detail)
        metrics = implementation.get("metrics") if implementation else None
        evaluation = {**detail, **metrics} if isinstance(metrics, dict) else detail
        metric_map = detail.get("_metric_map", _SYNTH_METRIC_MAP)
        min_allowed = set(detail.get("_min_allowed", ["fmax_mhz"]))
        baseline = detail.get("baseline_metrics", {})
        comparison = implementation.get("comparison") if implementation else None
        canonical_baseline = comparison.get("baseline") if isinstance(comparison, dict) else None
        canonical_metrics = (
            canonical_baseline.get("metrics") if isinstance(canonical_baseline, dict) else None
        )
        if isinstance(canonical_metrics, dict):
            baseline = canonical_metrics
        return evaluation, baseline, metric_map, min_allowed

    def _evaluate_metric_params(
        self,
        entry: CriterionEntry,
        checks: list[dict[str, Any]],
        evaluation: dict[str, Any],
        baseline: dict[str, Any],
        metric_map: dict[str, str],
        min_allowed: set[str],
    ) -> bool:
        all_pass = True
        for param_key, threshold in entry.params.items():
            if param_key.startswith("_") or param_key in {"target", "test"}:
                continue
            if param_key in CYCLE_COUNT_PARAMS:
                result = evaluate_cycle_threshold(
                    param_key,
                    threshold,
                    current=evaluation.get("cycles"),
                    baseline=entry.detail.get("baseline_cycles"),
                )
            else:
                result = self._check_single_threshold(
                    param_key, threshold, evaluation, baseline, metric_map, min_allowed
                )
            if result is not None:
                checks.append(result)
                all_pass = all_pass and bool(result["pass"])
        return all_pass

    def _evaluate_thresholds(self, entry: CriterionEntry) -> None:
        """Evaluate threshold params against reported metrics.

        Uses _metric_map from detail dict if present, else falls back to
        _SYNTH_METRIC_MAP for backward compatibility with older Flow versions.
        Modifies entry.met and appends check results to entry.detail["checks"].
        """
        detail = entry.detail
        checks: list[dict[str, Any]] = []
        evaluation, baseline, metric_map, min_allowed = self._threshold_evidence(detail)
        recipe_pass = self._evaluate_recipe_evidence(entry, checks)
        metrics_pass = self._evaluate_metric_params(
            entry, checks, evaluation, baseline, metric_map, min_allowed
        )

        detail["checks"] = checks
        if any(param in CYCLE_COUNT_PARAMS for param in entry.params):
            detail["cycle_comparison"] = build_cycle_comparison(entry.params, detail, checks)
        if not recipe_pass or not metrics_pass:
            entry.met = False
            entry.ever_met = False

    @staticmethod
    def _evaluate_recipe_evidence(
        entry: CriterionEntry,
        checks: list[dict[str, Any]],
    ) -> bool:
        """Record old/new recipe changes and validate pinned baseline evidence."""
        expected_recipe = entry.params.get(RECIPE_FINGERPRINT_PARAM)
        if expected_recipe is None:
            return True
        evidence = _collect_recipe_evidence(entry)
        entry.detail["recipe_comparison"] = {
            "flow": _recipe_flow(evidence.baseline_snapshot, evidence.current_snapshot),
            "target": evidence.current_snapshot.get("target")
            if isinstance(evidence.current_snapshot, dict)
            else None,
            "baseline_target": evidence.baseline_target,
            "candidate_target": evidence.candidate_target,
            "baseline_ref": evidence.expected_ref,
            "baseline_fingerprint": evidence.expected_recipe,
            "current_fingerprint": evidence.actual_recipe,
            "changed": evidence.actual_recipe != evidence.expected_recipe,
            "changes": evidence.changes,
            "comparison_basis_changes": evidence.basis_changes,
        }
        checks.append(
            {
                "param": "_recipe_evidence",
                "expected": evidence.expected_ref or evidence.expected_recipe,
                "actual": evidence.actual_ref or evidence.actual_recipe,
                "pass": evidence.complete,
                "detail": "baseline and candidate evidence match the recorded Target pair"
                if evidence.complete
                else "implementation recipe comparison evidence is incomplete",
            }
        )
        return evidence.complete

    def _check_single_threshold(
        self,
        param_key: str,
        threshold: float,
        detail: dict[str, Any],
        baseline: dict[str, Any],
        metric_map: dict[str, str],
        min_allowed: set[str],
    ) -> dict[str, Any] | None:
        """Evaluate one threshold param. Returns check dict or None if skipped."""
        if param_key.endswith("_max"):
            return _check_absolute_cap(
                param_key,
                "_max",
                threshold,
                detail,
                metric_map,
            )
        if param_key.endswith("_min"):
            return _check_absolute_min(
                param_key,
                threshold,
                detail,
                metric_map,
                min_allowed,
            )
        if param_key.endswith("_increase_at_most"):
            metric_prefix = param_key.removesuffix("_increase_at_most")
            return self._check_delta(
                param_key,
                metric_prefix,
                threshold,
                detail,
                baseline,
                metric_map=metric_map,
                mode="increase",
            )
        if param_key.endswith("_reduce_at_least"):
            metric_prefix = param_key.removesuffix("_reduce_at_least")
            return self._check_delta(
                param_key,
                metric_prefix,
                threshold,
                detail,
                baseline,
                metric_map=metric_map,
                mode="reduce",
            )
        return None

    def _check_delta(
        self,
        param_key: str,
        metric_prefix: str,
        threshold: float,
        detail: dict[str, Any],
        baseline: dict[str, Any],
        *,
        metric_map: dict[str, str],
        mode: str,  # "increase" or "reduce"
    ) -> dict[str, Any]:
        """Check a delta (growth/reduction) threshold against baseline.

        ``metric_prefix`` may be per-clock (``<clock>.<sub>``); the shared
        resolver reads it out of both the current detail and the baseline's
        ``per_clock`` map, so a ``clk_i.critical_path_ps_increase_at_most`` gate
        works exactly like its flat counterpart.
        """
        cur_value, metric_key = resolve_metric(detail, metric_prefix, metric_map)
        if metric_key is None:
            return {
                "param": param_key,
                "pass": False,
                "skipped": True,
                "reason": f"unknown metric prefix {metric_prefix!r}",
            }

        # detail/baseline are Flow/LLM-supplied metric dicts; the resolver coerces
        # at this boundary so a non-numeric value skips the gate cleanly instead
        # of raising TypeError (or a "0"-string dividing by zero) below.
        base_value, _ = resolve_metric(baseline or {}, metric_prefix, metric_map)

        if base_value is None:
            logger.warning(
                "Cannot evaluate delta check %r: no baseline for %s",
                param_key,
                metric_key,
            )
            return {
                "param": param_key,
                "pass": False,
                "skipped": True,
                "reason": f"no baseline for {metric_key}",
            }

        if base_value == 0:
            return {
                "param": param_key,
                "pass": False,
                "skipped": True,
                "reason": f"zero baseline for {metric_key} cannot define a percentage",
            }

        if cur_value is None:
            return {
                "param": param_key,
                "pass": False,
                "skipped": True,
                "reason": f"current {metric_key} not available",
            }

        if mode == "increase":
            # pct increase: ((cur - base) / base) * 100
            pct = ((cur_value - base_value) / base_value) * 100.0
            passed = pct <= threshold
        else:
            # pct reduction: ((base - cur) / base) * 100
            pct = ((base_value - cur_value) / base_value) * 100.0
            passed = pct >= threshold

        return {
            "param": param_key,
            "pass": passed,
            "pct": round(pct, 2),
            "threshold": threshold,
            "current": cur_value,
            "baseline": base_value,
        }

    def reset_category(self, category: str) -> list[str]:
        """Reset all criteria belonging to *category* to unmet.

        A criterion belongs to a category if its explicit ``category_map``
        entry matches, **or** any of its inferred categories (from prefix)
        matches.  This means ``sim_*`` criteria are reset by both RTL and
        TB resets, since simulations depend on both.

        Returns list of keys that were actually reset.
        """
        now = utc_now_rfc3339()
        reset_keys: list[str] = []
        for key, entry in self.criteria.items():
            if entry.locked:
                continue
            # Explicit override takes precedence (single category)
            override = self.category_map.get(key)
            if override:
                belongs = category == override
            else:
                belongs = category in _infer_categories(key)
                if not belongs:
                    belongs = any(
                        key.startswith(prefix) and review_category == category
                        for prefix, review_category in _REVIEW_CATEGORY.items()
                    )
            if belongs and entry.met:
                entry.met = False
                entry.stale = True
                entry.updated_at = now
                reset_keys.append(key)
                continue
            # Clear exhausted verify_attempts for review criteria whose
            # category matches, even though reviews are excluded from
            # _CATEGORY_PREFIXES (they must not have met reset, but the
            # verify counter must be refreshed when code changes).
            # total_verify_cycles is NOT cleared — it tracks cumulative
            # attempts across all coder fixes to detect stale-finding impasses.
            if belongs and not entry.met and entry.detail.get("verify_attempts"):
                for prefix, cat in _REVIEW_CATEGORY.items():
                    if key.startswith(prefix) and cat == category:
                        entry.detail.pop("verify_attempts", None)
                        entry.updated_at = now
                        reset_keys.append(key)
                        break
        # Internal `_report_submitted` follows the code/tb state -- if a code-
        # modifying Specialist invalidated anything, the previously submitted report
        # is now stale and must be resubmitted reflecting the new state.
        report_entry = self.criteria.get("_report_submitted")
        if (
            reset_keys
            and report_entry is not None
            and report_entry.met
            and not report_entry.locked
        ):
            report_entry.met = False
            report_entry.stale = True
            report_entry.updated_at = now
            reset_keys.append("_report_submitted")

        if reset_keys:
            logger.info(
                "Reset %d criteria in category %r: %s",
                len(reset_keys),
                category,
                ", ".join(reset_keys),
            )
        return reset_keys

    # --- Queries ---

    def _resolve_entries(self, key: str) -> list[CriterionEntry]:
        """Return criterion entries for *key*, resolving aliases if needed."""
        entry = self.criteria.get(key)
        if entry is not None:
            return [entry]
        aliases = self.flow_key_aliases.get(key, [])
        return [self.criteria[a] for a in aliases if a in self.criteria]

    def has_criterion(self, key: str) -> bool:
        """True if *key* exists directly or has aliased entries in criteria."""
        return len(self._resolve_entries(key)) > 0

    def is_met(self, key: str) -> bool:
        """Check if a criterion is met (all aliased entries must be met)."""
        entries = self._resolve_entries(key)
        return len(entries) > 0 and all(e.met for e in entries)

    def all_mandatory_met(self) -> bool:
        """True when every mandatory criterion is met."""
        return all(
            e.met for k, e in self.criteria.items() if e.mandatory and not k.startswith("_")
        )

    def unmet_mandatory(self) -> list[str]:
        """Return keys of unmet mandatory criteria."""
        return [
            k
            for k, e in self.criteria.items()
            if e.mandatory and not e.met and not k.startswith("_")
        ]

    def summary(self) -> dict[str, Any]:
        """Return a human-readable summary dict."""
        real = {k: e for k, e in self.criteria.items() if not k.startswith("_")}
        total = len(real)
        met = sum(1 for e in real.values() if e.met)
        mandatory = sum(1 for e in real.values() if e.mandatory)
        mandatory_met = sum(1 for e in real.values() if e.mandatory and e.met)
        return {
            "total": total,
            "met": met,
            "mandatory": mandatory,
            "mandatory_met": mandatory_met,
            "all_mandatory_met": mandatory_met == mandatory,
            "unmet_mandatory": self.unmet_mandatory(),
        }

    # --- Timeline ---

    def record_mcp_tool_run(
        self,
        mcp_tool_name: str,
        exit_code: int,
        *,
        endpoint_kind: str = "mcp_tool",
        duration_s: float | None = None,
        criteria_set: list[str] | None = None,
        cost_usd: float | None = None,
        args: dict[str, Any] | None = None,
    ) -> None:
        """Append an MCP endpoint invocation to the timeline."""
        identity_key = "flow" if endpoint_kind == "flow" else "mcp_tool"
        entry: dict[str, Any] = {
            identity_key: mcp_tool_name,
            "endpoint_kind": endpoint_kind,
            "exit_code": exit_code,
            "timestamp": utc_now_rfc3339(),
        }
        if duration_s is not None:
            entry["duration_s"] = round(duration_s, 2)
        if criteria_set:
            entry["criteria_set"] = criteria_set
        if cost_usd:
            entry["cost_usd"] = round(cost_usd, 4)
        if args:
            entry["args"] = args
        self.timeline.append(entry)

    def total_cost(self) -> float:
        """Sum cost_usd across all timeline entries (endpoints + developer + summary)."""
        return sum(e.get("cost_usd", 0) for e in self.timeline)


def _atomic_replace(src: Path, dst: Path) -> None:
    """Replace dst with src atomically, with retry on Windows PermissionError."""
    for attempt in range(4):
        try:
            src.replace(dst)
            return
        except PermissionError:
            if attempt < 3:
                time.sleep(0.25 * (2**attempt))
                continue
            logger.warning(
                "State file replace() blocked after retries; tmp preserved at %s",
                src,
            )
            raise
