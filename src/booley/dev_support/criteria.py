"""Criteria templates — default criteria sets per ticket type.

Handles per-target expansion: a single logical criterion like ``lint_clean``
with targets ``[lite, full, combo]`` expands into ``lint_clean_lite``,
``lint_clean_full``, ``lint_clean_combo``.

Naming convention (from design doc):
  - Simple: ``{type}_{target}`` — e.g. ``lint_clean_lite``
  - Sim: ``{type}_{tb}_{target}_{test}`` — e.g. ``sim_pass_alu_tb_lite_all``
  - No target: ``{type}`` — e.g. ``review_rtl_spec_done``

Structured sim criterion format:
  ``tb @ target [@ test_name] @ current -> expected``
  Parsed right-to-left on ``->`` for current/expected, then left side on ``@``.

Also provides TOML-based criterion *definitions* (base + project):
  - Base: ``data/criteria.toml`` (ships with Booley, loaded via importlib.resources)
  - Project: ``.booley_project/criteria.toml`` (project-specific extensions)
"""

from __future__ import annotations

import logging
import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from booley.core.boundary import (
    BoundaryError,
    as_positive_int,
    as_str,
    is_str_list,
    require_bool,
    require_dict,
    require_finite_number,
    require_list,
    require_str,
)
from booley.dev_support.thresholds import CYCLE_COUNT_PARAMS
from booley.targets import target_naming

logger = logging.getLogger(__name__)

_RELATIVE_QOR_SUFFIXES = ("_increase_at_most", "_reduce_at_least")
_PAIRED_TARGET_CRITERIA = frozenset({"synthesis_ok", "fpga_impl_ok"})
BASELINE_TARGET_PARAM = "_baseline_target"


@dataclass(frozen=True)
class TargetPair:
    """One frozen baseline/candidate Target relationship for a relative Criterion."""

    baseline: str
    candidate: str


def parse_target_pair(value: Any, *, field: str = "target") -> TargetPair:
    """Normalize a Target string or exact ``{baseline, candidate}`` mapping."""
    target = as_str(value)
    if target is not None:
        target = target.strip()
        if not target:
            raise ValueError(f"{field} must be a non-empty Target name")
        return TargetPair(target, target)
    try:
        mapping = require_dict(value, field=field)
    except BoundaryError as exc:
        raise ValueError(f"{field} must be a Target name or baseline/candidate mapping") from exc
    keys = set(mapping)
    if keys != {"baseline", "candidate"}:
        raise ValueError(f"{field} mapping must contain exactly 'baseline' and 'candidate'")
    try:
        baseline = require_str(mapping, "baseline").strip()
        candidate = require_str(mapping, "candidate").strip()
    except BoundaryError as exc:
        key = "baseline" if not as_str(mapping.get("baseline")) else "candidate"
        raise ValueError(f"{field}.{key} must be a non-empty Target name") from exc
    if not baseline:
        raise ValueError(f"{field}.baseline must be a non-empty Target name")
    if not candidate:
        raise ValueError(f"{field}.candidate must be a non-empty Target name")
    return TargetPair(baseline.strip(), candidate.strip())


def has_relative_qor_threshold(params: dict[str, Any]) -> bool:
    """Whether public Criterion params request a baseline-relative QoR check."""
    return any(
        isinstance(key, str) and key.endswith(_RELATIVE_QOR_SUFFIXES)
        for key in params
        if not str(key).startswith("_")
    )


# ---------------------------------------------------------------------------
# Retired criterion keys — migration guard
# ---------------------------------------------------------------------------

# Criterion keys removed or renamed by past migrations, each mapped to a
# specific, actionable hint. This is the single source of truth, diffed against
# a ticket both at harness intake (booley.harness.setup.intake) and in
# ``validate-ticket`` pre-flight (booley.ticket_board.validation) so a ticket
# authored before a rename fails fast with the exact fix — instead of the
# opaque mid-run CRITICAL crash that motivated this registry. An unrecognized
# key is otherwise silently created as *optional*, downgrading a mandatory gate
# to a no-op; hence a hard error, not a warning.
_COVERAGE_MIGRATION_HINT = (
    "replace it with 'coverage: [{targets: [...], metrics: {...}, tests: all}]'"
)
RETIRED_CRITERIA: dict[str, str] = {
    "plan_done": "remove it; the planner specialists were pruned",
    "plan_created": "remove it; the planner specialists were pruned",
    "rtl_plan_done": "remove it; nothing has satisfied this since the planner specialists were pruned",
    "verification_plan_done": (
        "remove it; nothing has satisfied this since the planner specialists were pruned"
    ),
    "review_rtl_functional": "rename to 'review_rtl_bugs'",
    "review_rtl_quality": "rename to 'review_rtl_code_style'",
    "review_rtl_ifdef": "remove it; ifdef/config review folded into 'review_rtl_bugs'",
    "coverage_toggle": _COVERAGE_MIGRATION_HINT,
    "coverage_fsm": _COVERAGE_MIGRATION_HINT,
    "coverage_value": _COVERAGE_MIGRATION_HINT,
    "coverage_branch": _COVERAGE_MIGRATION_HINT,
    "coverage_expression": _COVERAGE_MIGRATION_HINT,
    "coverage_mean": _COVERAGE_MIGRATION_HINT,
}


def find_retired_criteria(keys: Iterable[str]) -> list[tuple[str, str]]:
    """Return sorted (retired_key, hint) pairs for any retired key in *keys*.

    Review criteria expand with a verdict suffix (``review_x`` ->
    ``review_x_done``/``_clean``), so a retired base matches both its bare form
    and any suffixed form of it — this accepts either raw ticket-YAML keys or
    post-expansion keys.
    """
    keys = list(keys)
    hits = {
        retired
        for retired in RETIRED_CRITERIA
        if any(key == retired or key.startswith(f"{retired}_") for key in keys)
    }
    return [(k, RETIRED_CRITERIA[k]) for k in sorted(hits)]


# ---------------------------------------------------------------------------
# TOML-based criterion definitions (base + project)
# ---------------------------------------------------------------------------

_VALID_WORKFLOW_REGIONS = frozenset({"pre_sim", "core_loop", "post_sim"})
_VALID_CATEGORIES = frozenset({"rtl", "tb", "none"})
_COVERAGE_METRICS = frozenset({"line", "branch", "expression", "toggle", "cover_property"})


@dataclass(frozen=True)
class CriterionDef:
    """Single criterion definition from TOML."""

    name: str
    description: str
    workflow_region: str  # pre_sim | core_loop | post_sim
    per_target: bool
    category: str  # rtl | tb | none
    group: str  # functional family for docs grouping (see criteria_reference)
    # True for a family whose public binding is one Target and one named test.
    per_test: bool = False
    # Omitted from the generated reference/cheatsheet, but still fully usable if a
    # ticket declares it. For criteria whose producing Flow is de-registered, so
    # listing them would advertise a criterion nothing can currently satisfy.
    hidden: bool = False


def _load_toml(path: Path) -> dict[str, Any]:
    """Load a TOML file using tomllib."""
    with path.open("rb") as f:
        return tomllib.load(f)


def _parse_criteria_toml(data: dict[str, Any], source: str) -> list[CriterionDef]:
    """Parse a criteria TOML dict into CriterionDef list."""
    defs: list[CriterionDef] = []
    for name, section in data.items():
        if not isinstance(section, dict):
            logger.warning("Skipping non-table entry %r in %s", name, source)
            continue

        description = section.get("description", "")
        # "workflow_region" is canonical; "phase" is the legacy key, still read.
        workflow_region = section.get("workflow_region")
        if workflow_region is None:
            workflow_region = section.get("phase", "pre_sim")
            if "phase" in section:
                logger.warning(
                    "Criterion %r in %s uses legacy key 'phase'; rename it to 'workflow_region'",
                    name,
                    source,
                )
        per_target = section.get("per_target", False)
        per_test = require_bool(
            section,
            "per_test",
            field=f"criterion {name!r} per_test",
        )
        category = section.get("category", "none")
        group = section.get("group", "other")
        hidden = bool(section.get("hidden", False))

        if workflow_region not in _VALID_WORKFLOW_REGIONS:
            logger.warning(
                "Invalid workflow_region %r for criterion %r in %s",
                workflow_region,
                name,
                source,
            )
            continue
        if category not in _VALID_CATEGORIES:
            logger.warning(
                "Invalid category %r for criterion %r in %s",
                category,
                name,
                source,
            )
            continue

        defs.append(
            CriterionDef(
                name=name,
                description=description,
                workflow_region=workflow_region,
                per_target=per_target,
                category=category,
                group=group,
                per_test=per_test,
                hidden=hidden,
            )
        )
    return defs


def load_base_criteria() -> list[CriterionDef]:
    """Load base criteria from package data (``data/criteria.toml``)."""
    import tomllib
    from importlib.resources import files

    criteria_res = files("booley.data").joinpath("criteria.toml")
    if hasattr(criteria_res, "__fspath__"):
        path = Path(criteria_res)
        data = _load_toml(path)
    else:
        text = criteria_res.read_text(encoding="utf-8")
        data = tomllib.loads(text)

    return _parse_criteria_toml(data, "base")


def load_project_criteria(project_criteria_path: Path) -> list[CriterionDef]:
    """Load project criteria from ``.booley_project/criteria.toml``.

    Returns empty list if file doesn't exist.
    """
    if not project_criteria_path.exists():
        return []
    try:
        data = _load_toml(project_criteria_path)
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.error("Failed to parse project criteria %s: %s", project_criteria_path, e)
        return []
    return _parse_criteria_toml(data, str(project_criteria_path))


def merge_criteria_defs(
    base: list[CriterionDef],
    project: list[CriterionDef],
) -> tuple[list[CriterionDef], list[str]]:
    """Merge base and project criteria definitions.

    Returns (merged_list, errors). Errors when project criteria
    attempt to override base criteria names.
    """
    base_names = {c.name for c in base}
    errors: list[str] = []
    merged = list(base)

    for c in project:
        if c.name in base_names:
            errors.append(f"Project criterion {c.name!r} conflicts with base criterion")
        else:
            merged.append(c)

    return merged, errors


# Resolution tool → eligible per-target criterion families (ADR 0022
# decision 11). FPGA intent overrides this map because that Flow rebuilds the
# resolved inputs into a Vivado EDAM; ``expand_criteria_defs`` applies the
# Target-name policy before consulting the declared tool.
EDA_TOOL_CRITERION_FAMILIES: dict[str, frozenset[str]] = {
    "verilator": frozenset({"sim_pass", "cycle_count", "lint_clean"}),
    "icarus": frozenset({"sim_pass", "cycle_count"}),
    "iverilog": frozenset({"sim_pass", "cycle_count"}),
    "yosys": frozenset({"synthesis_ok"}),
    "vivado": frozenset({"fpga_impl_ok"}),
}

# Every family that any EDA tool gates — a family outside this set is not EDA-tool-gated
# (e.g. review/coverage criteria), so eligibility never filters it.
_EDA_TOOL_GATED_FAMILIES: frozenset[str] = frozenset().union(*EDA_TOOL_CRITERION_FAMILIES.values())


def eligible_eda_tool_criterion_families(eda_tool: str | None) -> frozenset[str]:
    """Criterion families contributed by a Target's declared resolution tool."""
    return EDA_TOOL_CRITERION_FAMILIES.get(eda_tool or "", frozenset())


def unsupported_eda_tool_boundary(eda_tool: str) -> str:
    """The ADR 0039 §5 boundary statement for an EDA tool outside the matrix.

    ``EDA_TOOL_CRITERION_FAMILIES`` is the resolution-tool boundary for
    simulation, lint, and synthesis. FPGA-axis Targets are the exception: their
    name selects the fixed Vivado backend while this tool controls resolution.
    """
    return (
        f"EDA tool {eda_tool!r} is outside Booley's built-in matrix "
        f"({', '.join(sorted(EDA_TOOL_CRITERION_FAMILIES))}): it contributes "
        "no sim/lint/synth criteria, so non-FPGA Targets cannot run RTL tickets "
        "(ADR 0039 §5). Widening the matrix (edalize wiring → output parser "
        "→ criteria-map row → doctor probe) is the supported extension axis; "
        "a Custom Flow adds new analyses with its own criterion families but "
        "is not a side door into sim_pass_*."
    )


def _criterion_eligible(
    crit_name: str,
    eda_tool: str | None,
    *,
    target_name: str | None = None,
) -> bool:
    """Whether a criterion family is eligible for a Target declaration.

    FPGA intent is exclusive when *target_name* is available: an FPGA Target
    carries only ``fpga_impl_ok`` among the gated families. Other Targets follow
    the declared tool; an unknown tool widens eligibility rather than narrowing it.
    """
    if crit_name not in _EDA_TOOL_GATED_FAMILIES:
        return True
    if target_name is not None:
        if target_naming.fpga_intent(target_name, eda_tool):
            return crit_name == "fpga_impl_ok"
        if crit_name == "fpga_impl_ok" and target_naming.axis_of(target_name) is not None:
            return False
    if eda_tool is None:
        return True
    return crit_name in eligible_eda_tool_criterion_families(eda_tool)


def expand_criteria_defs(
    criteria: list[CriterionDef],
    targets: list[str],
    target_eda_tools: dict[str, str | None] | None = None,
) -> dict[str, CriterionDef]:
    """Expand per_target criteria across all project targets.

    When ``target_eda_tools`` (Target name → declared resolution tool) is
    supplied, a per-target criterion is skipped when the Target declaration is
    incompatible with that family (decision 11). FPGA uses its name axis;
    other built-ins use the tool. Omitting the map preserves unfiltered expansion.

    Returns dict of expanded_name -> CriterionDef.
    """
    eda_tools = target_eda_tools or {}
    expanded: dict[str, CriterionDef] = {}
    warned_eda_tools: set[str] = set()
    for crit in criteria:
        if crit.per_target and targets:
            for tgt in targets:
                eda_tool = eda_tools.get(tgt)
                target_name = tgt if tgt in eda_tools else None
                if not _criterion_eligible(crit.name, eda_tool, target_name=target_name):
                    # A known EDA tool with no row in the matrix is the ADR 0039
                    # §5 boundary (an unsupported simulator), not a routine
                    # cross-family skip — say so, once per EDA tool.
                    if (
                        eda_tool is not None
                        and eda_tool not in EDA_TOOL_CRITERION_FAMILIES
                        and eda_tool not in warned_eda_tools
                    ):
                        warned_eda_tools.add(eda_tool)
                        logger.warning("%s", unsupported_eda_tool_boundary(eda_tool))
                    continue
                expanded[f"{crit.name}_{tgt}"] = crit
        else:
            expanded[crit.name] = crit
    return expanded


# ---------------------------------------------------------------------------
# Structured sim criterion parsing
# ---------------------------------------------------------------------------


@dataclass
class SimCriterion:
    """Parsed structured sim criterion entry."""

    tb: str
    target: str
    test_name: str  # empty string if not specified
    current: str
    expected: str


def parse_sim_criterion(entry: str) -> SimCriterion:
    """Parse ``tb @ target [@ test_name] @ current -> expected``.

    Splits on ``->`` first for current/expected, then splits the left
    side on ``@`` (3 or 4 segments).

    Raises ValueError on malformed entries.
    """
    if "->" not in entry:
        raise ValueError(f"Missing '->' in sim criterion: {entry!r}")
    left, expected = entry.rsplit("->", 1)
    expected = expected.strip()
    parts = [p.strip() for p in left.split("@")]
    if len(parts) == 3:
        tb, target, current = parts
        test_name = ""
    elif len(parts) == 4:
        tb, target, test_name, current = parts
    else:
        raise ValueError(
            f"Expected 3-4 '@'-separated segments before '->',  got {len(parts)}: {entry!r}"
        )
    return SimCriterion(
        tb=tb,
        target=target,
        test_name=test_name,
        current=current.strip(),
        expected=expected,
    )


# ---------------------------------------------------------------------------
# synthesis_ok parameter validation
# ---------------------------------------------------------------------------

SYNTHESIS_OK_PARAMS: frozenset[str] = frozenset(
    {
        "area_um2_max",
        "area_kge_max",
        "cell_count_max",
        "wire_count_max",
        "critical_path_ps_max",
        "fmax_mhz_min",
        "area_increase_at_most",
        "cell_count_increase_at_most",
        "wire_count_increase_at_most",
        "critical_path_ps_increase_at_most",
        "fmax_mhz_increase_at_most",
        "area_reduce_at_least",
        "cell_count_reduce_at_least",
        "wire_count_reduce_at_least",
        "critical_path_ps_reduce_at_least",
        "fmax_mhz_reduce_at_least",
    }
)

SYNTHESIS_OK_MUTEX_PAIRS: list[tuple[str, str]] = [
    ("area_um2_max", "area_kge_max"),
    ("critical_path_ps_max", "fmax_mhz_min"),
]

FPGA_IMPL_OK_PARAMS: frozenset[str] = frozenset(
    {
        "lut_count_max",
        "ff_count_max",
        "bram_count_max",
        "dsp_count_max",
        "critical_path_ps_max",
        "fmax_mhz_min",
        "lut_count_increase_at_most",
        "ff_count_increase_at_most",
        "bram_count_increase_at_most",
        "dsp_count_increase_at_most",
        "critical_path_ps_increase_at_most",
        "lut_count_reduce_at_least",
        "ff_count_reduce_at_least",
        "bram_count_reduce_at_least",
        "dsp_count_reduce_at_least",
        "critical_path_ps_reduce_at_least",
    }
)

FPGA_IMPL_OK_MUTEX_PAIRS: list[tuple[str, str]] = [
    ("critical_path_ps_max", "fmax_mhz_min"),
]

_TARGET_CAMPAIGN_PARAM_REGISTRY: dict[str, tuple[frozenset[str], list[tuple[str, str]]]] = {
    "mutation_score": (
        frozenset({"scope", "min_detected", "total", "auto"}),
        [("auto", "total")],
    ),
}

# Criteria whose execution and acceptance evidence belong to one Target campaign.
TARGET_CAMPAIGN_CRITERIA: frozenset[str] = frozenset(_TARGET_CAMPAIGN_PARAM_REGISTRY)


# Registry: criterion name -> (valid params, mutex pairs)
_CRITERION_PARAM_REGISTRY: dict[str, tuple[frozenset[str], list[tuple[str, str]]]] = {
    "synthesis_ok": (SYNTHESIS_OK_PARAMS, SYNTHESIS_OK_MUTEX_PAIRS),
    "fpga_impl_ok": (FPGA_IMPL_OK_PARAMS, FPGA_IMPL_OK_MUTEX_PAIRS),
    "cycle_count": (CYCLE_COUNT_PARAMS, []),
    **_TARGET_CAMPAIGN_PARAM_REGISTRY,
}

# Fmax and critical-path delay are inherently per-clock, so a threshold on one
# of these metrics may be scoped to a single clock by name — written
# ``<clock>.<param>`` (e.g. ``clk_i.fmax_mhz_min`` / ``clk_i.critical_path_ps_max``).
# Only these metrics are clock-scopable; area/utilization/counts are not.
_PER_CLOCK_METRICS: frozenset[str] = frozenset(
    {"critical_path_ps", "fmax_mhz", "wns_ns", "whs_ns", "period_ns"}
)
# Threshold-param flavour suffixes, longest first so the base metric splits off
# unambiguously (mirrors criteria_reference._THRESHOLD_SUFFIXES).
_THRESHOLD_SUFFIXES: tuple[str, ...] = (
    "_increase_at_most",
    "_reduce_at_least",
    "_max",
    "_min",
)


def _param_base_metric(param: str) -> str:
    """Strip a threshold flavour suffix off a param → its base metric name."""
    for suffix in _THRESHOLD_SUFFIXES:
        if param.endswith(suffix):
            return param[: -len(suffix)]
    return param


def _split_clock_scope(param: str) -> tuple[str, str]:
    """Split ``<clock>.<param>`` → ``(clock, param)``; flat → ``("", param)``."""
    if "." in param:
        clock, _, base = param.partition(".")
        return clock, base
    return "", param


def has_synth_criteria(criteria: dict[str, Any]) -> bool:
    """Check whether a criteria dict contains any synthesis-related keys."""
    for section in ("mandatory", "optional"):
        sub = criteria.get(section, {})
        if isinstance(sub, dict) and any(
            k.startswith(("synth", "synthesis_ok", "fpga_impl_ok")) for k in sub
        ):
            return True
    return False


def _extract_sim_criterion_fields(criteria: dict[str, Any], key: str) -> list[str]:
    """Walk sim structured entries, collecting unique values of ``SimCriterion.<key>``.

    Shared by ``extract_sim_targets`` (``key="target"``) and ``extract_tb_paths``
    (``key="tb"``) — the two were byte-identical except for which parsed field
    they read.
    """
    seen: set[str] = set()
    result: list[str] = []
    for section in ("mandatory", "optional"):
        sub = criteria.get(section, {})
        if not isinstance(sub, dict):
            continue
        for _key, value in sub.items():
            if not isinstance(value, list):
                continue
            for item in value:
                if not isinstance(item, str) or "->" not in item:
                    continue
                try:
                    sc = parse_sim_criterion(item)
                    field_value = getattr(sc, key)
                    if field_value not in seen:
                        seen.add(field_value)
                        result.append(field_value)
                except ValueError:
                    pass
    return result


def extract_sim_targets(criteria: dict[str, Any]) -> list[str]:
    """Extract unique target names from sim structured entries."""
    result = _extract_sim_criterion_fields(criteria, "target")
    seen = set(result)
    for section_name in ("mandatory", "optional"):
        section = criteria.get(section_name, {})
        entries = section.get("cycle_count", []) if isinstance(section, dict) else []
        if not isinstance(entries, list):
            continue
        for entry in entries:
            target = entry.get("target") if isinstance(entry, dict) else None
            if isinstance(target, str) and target and target not in seen:
                seen.add(target)
                result.append(target)
    return result


def extract_tb_paths(criteria: dict[str, Any]) -> list[str]:
    """Extract unique TB filenames from sim structured entries."""
    return _extract_sim_criterion_fields(criteria, "tb")


@dataclass
class CriterionSpec:
    """Single criterion specification before target expansion."""

    name: str
    mandatory: bool = True
    # If per_target is True, expand once per target
    per_target: bool = False
    # Category override (auto-inferred from prefix if None)
    category: str | None = None
    # Extra parameters (e.g. max_percent for synthesis delta)
    params: dict[str, Any] = field(default_factory=dict)
    # Explicit target names from YAML (used by expand when available)
    targets: list[str] | None = None
    # Flow alias: the generic per-target key a Flow would set for this
    # file-specific criterion.  E.g. "sim_pass_default" for a spec whose
    # expanded name is "sim_pass_verif_tb_aes128_dec.sv_default".
    flow_key_alias: str | None = None

    def expand(self, targets: list[str]) -> list[tuple[str, bool]]:
        """Expand into (key, mandatory) pairs.

        Uses self.targets (from YAML) when available, falling back to
        the passed targets list.
        """
        if not self.per_target:
            return [(self.name, self.mandatory)]
        effective = self.targets if self.targets is not None else targets
        if not effective:
            return [(self.name, self.mandatory)]
        return [(f"{self.name}_{tgt}", self.mandatory) for tgt in effective]


# --- Default templates ---

_FEATURE_CRITERIA: list[CriterionSpec] = [
    CriterionSpec("lint_clean", per_target=True),
    CriterionSpec("sim_pass", per_target=True),
    CriterionSpec("review_rtl_bugs_clean"),
    CriterionSpec("review_tb_quality_clean"),
]

_BUGFIX_CRITERIA: list[CriterionSpec] = [
    CriterionSpec("sim_pass", per_target=True),
]

_REFACTOR_CRITERIA: list[CriterionSpec] = [
    CriterionSpec("lint_clean", per_target=True),
    CriterionSpec("sim_pass", per_target=True),
    CriterionSpec("review_rtl_bugs_clean"),
]

_VERIFICATION_CRITERIA: list[CriterionSpec] = [
    CriterionSpec("sim_pass", per_target=True),
    CriterionSpec("review_tb_quality_clean"),
]

TEMPLATE_REGISTRY: dict[str, list[CriterionSpec]] = {
    "feature": _FEATURE_CRITERIA,
    "bugfix": _BUGFIX_CRITERIA,
    "refactor": _REFACTOR_CRITERIA,
    "verification": _VERIFICATION_CRITERIA,
}

# Criteria that Flows always set with a target suffix (e.g. sim_pass_default).
# A ticket declaring these as bare scalars (sim_pass: true) will never match.
PER_TARGET_CRITERIA: frozenset[str] = (
    frozenset(
        spec.name for specs in TEMPLATE_REGISTRY.values() for spec in specs if spec.per_target
    )
    | TARGET_CAMPAIGN_CRITERIA
    | {"coverage", "cycle_count"}
)


@dataclass
class CriteriaTemplate:
    """Expanded criteria set ready for DevelopmentState initialization."""

    specs: list[CriterionSpec] = field(default_factory=list)

    @classmethod
    def for_ticket_type(cls, ticket_type: str) -> CriteriaTemplate:
        """Load default template for a ticket type."""
        specs = TEMPLATE_REGISTRY.get(ticket_type, _BUGFIX_CRITERIA)
        return cls(specs=list(specs))

    @classmethod
    def from_yaml(cls, criteria_section: dict[str, Any]) -> CriteriaTemplate:
        """Parse criteria from ticket YAML ``criteria:`` section.

        Expected format::

            criteria:
              mandatory:
                lint_clean: [lite, full, combo]
                sim_pass:
                  - alu_tb@lite@all
                review_rtl_spec_done: approved
              optional:
                mutation_score:
                  - target: sim_alu
                    scope: [rtl/alu.sv]
                    min_detected: 8
                    total: 10
        """
        specs: list[CriterionSpec] = []
        cycle_bindings: set[tuple[str, str]] = set()
        for key, value in criteria_section.get("mandatory", {}).items():
            specs.extend(
                _parse_cycle_count_entries(value, True, cycle_bindings)
                if key == "cycle_count"
                else _parse_criterion_entry(key, value, mandatory=True)
            )
        for key, value in criteria_section.get("optional", {}).items():
            specs.extend(
                _parse_cycle_count_entries(value, False, cycle_bindings)
                if key == "cycle_count"
                else _parse_criterion_entry(key, value, mandatory=False)
            )
        baselines: dict[tuple[str, str], str] = {}
        coverage_targets: set[str] = set()
        for spec in specs:
            for candidate in spec.targets or []:
                if spec.name == "coverage":
                    if candidate in coverage_targets:
                        raise ValueError(
                            f"Target {candidate!r} occurs in more than one coverage record"
                        )
                    coverage_targets.add(candidate)
                baseline = spec.params.get(BASELINE_TARGET_PARAM, candidate)
                identity = (spec.name, candidate)
                prior = baselines.get(identity)
                if prior is not None and prior != baseline:
                    raise ValueError(
                        f"{spec.name} candidate {candidate!r} has conflicting baselines "
                        f"{prior!r} and {baseline!r} across criteria sections"
                    )
                baselines[identity] = baseline
        return cls(specs=specs)

    def expand(self, targets: list[str]) -> dict[str, bool]:
        """Expand all specs into a flat {criterion_key: mandatory} dict."""
        result: dict[str, bool] = {}
        for spec in self.specs:
            for key, mandatory in spec.expand(targets):
                result[key] = mandatory
        return result

    def expand_params(self, targets: list[str]) -> dict[str, dict[str, Any]]:
        """Expand specs into {criterion_key: params} for specs with non-empty params."""
        result: dict[str, dict[str, Any]] = {}
        for spec in self.specs:
            if not spec.params:
                continue
            for key, _mandatory in spec.expand(targets):
                result[key] = dict(spec.params)
        return result

    def flow_key_aliases(self) -> dict[str, list[str]]:
        """Return {generic_flow_key: [file_specific_key, ...]} mapping.

        When a ticket uses structured sim entries like
        ``verif/tb.sv @ default @ fail -> pass``, the expanded key is
        file-specific (``sim_pass_verif_tb.sv_default``) but the Flow
        only knows the generic per-target key (``sim_pass_default``).
        This map lets DevelopmentState fan out Flow-set generic keys
        to matching file-specific criteria.
        """
        aliases: dict[str, list[str]] = {}
        for spec in self.specs:
            if spec.flow_key_alias:
                aliases.setdefault(spec.flow_key_alias, []).append(spec.name)
        return aliases

    def category_overrides(self, targets: list[str]) -> dict[str, str]:
        """Return explicit category overrides, expanded per-target."""
        result: dict[str, str] = {}
        for spec in self.specs:
            if not spec.category:
                continue
            for key, _mandatory in spec.expand(targets):
                result[key] = spec.category
        return result

    def add(self, spec: CriterionSpec) -> None:
        """Add a criterion spec."""
        self.specs.append(spec)

    def remove(self, name: str) -> bool:
        """Remove a criterion spec by name. Returns True if found."""
        before = len(self.specs)
        self.specs = [s for s in self.specs if s.name != name]
        return len(self.specs) < before


def _is_review_base_key(key: str) -> bool:
    """True if *key* is a review criterion base (no verdict suffix).

    Bare YAML review criteria mean "currently clean" and therefore expand to
    ``_clean``. Authors who intentionally want a terminal advisory review that
    reports findings without fixing them can spell ``_done`` explicitly.
    """
    return key.startswith("review_") and not key.endswith(("_done", "_clean"))


def _parse_criterion_entry(  # noqa: PLR0911 — one early return per criterion value form (list/dict/string/compound)
    key: str,
    value: Any,
    *,
    mandatory: bool,
) -> list[CriterionSpec]:
    """Parse a single criterion entry from YAML into CriterionSpec(s).

    Value forms:
      - list of strings (targets): per-target expansion
      - list of dicts or compound strings: sim-style explicit keys
      - dict with params: parameterized criterion
      - scalar (str/bool/None): simple criterion, no expansion

    Review base keys (``review_rtl_spec``, ``review_tb_quality``, etc.)
    expand into ``_clean``. Explicit ``_done`` retains terminal advisory semantics.
    """
    if key == "coverage":
        try:
            records = require_list(value, field="coverage")
        except BoundaryError:
            raise ValueError("coverage must be a list of authoring records") from None
        return _parse_coverage_entries(records, mandatory=mandatory)
    if _is_review_base_key(key):
        return [CriterionSpec(f"{key}_clean", mandatory=mandatory)]
    if isinstance(value, list):
        return _parse_list_criterion(key, value, mandatory=mandatory)
    if isinstance(value, dict):
        return _parse_dict_criterion(key, value, mandatory=mandatory)
    # Scalar: check for "auto" or "N/M" fraction format (e.g. "8/10")
    if isinstance(value, str):
        if value.strip().lower() == "auto":
            return [CriterionSpec(key, mandatory=mandatory, params={"auto": True})]
        m = re.fullmatch(r"(\d+)\s*/\s*(\d+)", value)
        if m:
            min_detected, total = int(m.group(1)), int(m.group(2))
            return [
                CriterionSpec(
                    key,
                    mandatory=mandatory,
                    params={"min_detected": min_detected, "total": total},
                )
            ]
    # Integer percentage shorthand retained for compatible custom Criteria.
    if isinstance(value, int):
        return [CriterionSpec(key, mandatory=mandatory, params={"min_pct": value})]
    return [CriterionSpec(key, mandatory=mandatory)]


def encode_criterion_component(value: str) -> str:
    """Encode one arbitrary name as a stable collision-free key component."""
    encoded = value.encode("utf-8").hex()
    return f"u{len(encoded)}x{encoded}"


def cycle_count_criterion_key(target: str, test: str) -> str:
    """Return the stable Criterion key for one Target/test binding."""
    return f"cycle_count_{encode_criterion_component(target)}_{encode_criterion_component(test)}"


def _parse_cycle_count_entries(
    value: Any,
    mandatory: bool,
    seen: set[tuple[str, str]],
) -> list[CriterionSpec]:
    """Parse the dedicated list-of-mappings Cycle Count grammar."""
    if not isinstance(value, list) or not value:
        raise ValueError("cycle_count must be a non-empty list of mappings")
    specs: list[CriterionSpec] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"cycle_count[{index}] must be a mapping")
        target = item.get("target")
        test = item.get("test")
        if not isinstance(target, str) or not target.strip():
            raise ValueError(f"cycle_count[{index}].target must be a non-empty string")
        if not isinstance(test, str) or not test.strip():
            raise ValueError(f"cycle_count[{index}].test must be a non-empty string")
        binding = (target, test)
        if binding in seen:
            raise ValueError(
                f"duplicate cycle_count Target/test binding: target={target!r}, test={test!r}"
            )
        seen.add(binding)
        thresholds = {name: raw for name, raw in item.items() if name not in {"target", "test"}}
        if not thresholds:
            raise ValueError(
                f"cycle_count[{index}] must declare at least one Cycle Count threshold"
            )
        _validate_criterion_params("cycle_count", thresholds)
        specs.append(
            CriterionSpec(
                cycle_count_criterion_key(target, test),
                mandatory=mandatory,
                params={"target": target, "test": test, **thresholds},
            )
        )
    return specs


def _parse_coverage_entries(
    items: list[Any],
    *,
    mandatory: bool,
) -> list[CriterionSpec]:
    """Expand canonical Coverage Criterion authoring records by Target."""
    if not items:
        raise ValueError("coverage must contain at least one authoring record")
    specs: list[CriterionSpec] = []
    for item in items:
        try:
            record = require_dict(item, field="coverage record")
        except BoundaryError:
            raise ValueError("coverage must be a list of authoring records") from None
        targets, policy = _validate_coverage_record(record)
        for target in targets:
            specs.append(
                CriterionSpec(
                    "coverage",
                    mandatory=mandatory,
                    per_target=True,
                    targets=[target],
                    params={"target": target, **policy},
                )
            )
    return specs


def _parse_list_criterion(
    key: str,
    items: list,
    *,
    mandatory: bool,
) -> list[CriterionSpec]:
    """Parse list-valued criterion.

    If all items are simple strings with no @ and no ->, treat as target list.
    If items contain ->, parse as structured sim entries via parse_sim_criterion.
    If items contain @ (legacy), treat as explicit sim-style keys.
    """
    if not items:
        return [CriterionSpec(key, mandatory=mandatory)]
    if all(isinstance(item, dict) and "target" in item for item in items):
        specs = []
        for item in items:
            target = item.get("target")
            if not isinstance(target, str) or not target.strip():
                raise ValueError(f"{key} campaign target must be a non-empty string")
            params = {name: value for name, value in item.items() if name != "target"}
            if key in _CRITERION_PARAM_REGISTRY:
                _validate_criterion_params(key, params)
            specs.append(
                CriterionSpec(
                    key,
                    mandatory=mandatory,
                    per_target=True,
                    targets=[target],
                    params=params,
                )
            )
        return specs
    # Check if these are target names or explicit keys
    if all(isinstance(item, str) and "@" not in item and "->" not in item for item in items):
        target_names = [str(item) for item in items]
        return [CriterionSpec(key, mandatory=mandatory, per_target=True, targets=target_names)]
    # Explicit keys: create one spec per entry
    specs: list[CriterionSpec] = []
    for item in items:
        if isinstance(item, str):
            _parse_string_list_item(key, item, mandatory, specs)
        elif isinstance(item, dict):
            for sub_key, sub_val in item.items():
                flat_key = f"{key}_{sub_key.replace('@', '_')}"
                params = sub_val if isinstance(sub_val, dict) else {}
                specs.append(CriterionSpec(flat_key, mandatory=mandatory, params=params))
    return specs


def _validate_name_list(value: object, *, field: str) -> list[str]:
    if not is_str_list(value) or not value:
        raise ValueError(f"coverage.{field} must be a non-empty list of names")
    if not all(item.strip() for item in value):
        raise ValueError(f"coverage.{field} must contain only non-empty strings")
    names = [item.strip() for item in value]
    if len(set(names)) != len(names):
        raise ValueError(f"coverage.{field} must not contain duplicates")
    return names


def _validate_coverage_metrics(value: object) -> dict[str, Any]:
    try:
        metrics = require_dict(value, field="coverage.metrics")
    except BoundaryError:
        raise ValueError("coverage.metrics must be a non-empty mapping") from None
    if not metrics:
        raise ValueError("coverage.metrics must be a non-empty mapping")
    unknown = set(metrics) - _COVERAGE_METRICS
    if unknown:
        raise ValueError(f"coverage.metrics has unknown metrics: {sorted(unknown, key=str)}")
    for metric, raw_policy in metrics.items():
        try:
            policy = require_dict(raw_policy, field=f"coverage.metrics.{metric}")
        except BoundaryError:
            raise ValueError(f"coverage.metrics.{metric} must contain exactly 'min_pct'") from None
        if set(policy) != {"min_pct"}:
            raise ValueError(f"coverage.metrics.{metric} must contain exactly 'min_pct'")
        threshold = policy["min_pct"]
        try:
            number = require_finite_number(threshold, field=f"coverage metric {metric}")
        except BoundaryError:
            raise ValueError(f"coverage.metrics.{metric}.min_pct must be numeric") from None
        if not 0 < number <= 100:
            raise ValueError(f"coverage.metrics.{metric}.min_pct must be in (0, 100]")
    return metrics


def _validate_coverage_tests(value: object) -> str | list[str]:
    if value == "all":
        return "all"
    try:
        return _validate_name_list(value, field="tests")
    except ValueError as exc:
        if "duplicates" in str(exc):
            raise
        raise ValueError("coverage.tests must be 'all' or a non-empty list of names") from None


def _validate_coverage_record(item: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    unknown = set(item) - {"targets", "metrics", "tests"}
    if unknown:
        raise ValueError(f"coverage record has unknown fields: {sorted(unknown)}")
    if "targets" not in item:
        raise ValueError("coverage.targets is required")
    if "metrics" not in item:
        raise ValueError("coverage.metrics is required")
    if "tests" not in item:
        raise ValueError("coverage.tests is required")
    targets = _validate_name_list(item["targets"], field="targets")
    metrics = _validate_coverage_metrics(item["metrics"])
    tests = _validate_coverage_tests(item["tests"])
    return targets, {"metrics": metrics, "tests": tests}


def _parse_string_list_item(
    key: str,
    item: str,
    mandatory: bool,
    specs: list[CriterionSpec],
) -> None:
    """Parse a single string item in a list-valued criterion into specs."""
    if "->" not in item:
        # Legacy: "alu_tb@lite@all"
        flat_key = f"{key}_{item.replace('@', '_')}"
        specs.append(CriterionSpec(flat_key, mandatory=mandatory))
        return
    # Structured: "tb @ target @ test @ pass -> pass"
    try:
        sc = parse_sim_criterion(item)
    except ValueError:
        flat_key = f"{key}_{item.replace('@', '_').replace('->', '_').replace(' ', '')}"
        specs.append(CriterionSpec(flat_key, mandatory=mandatory))
        return
    parts = [sc.tb, sc.target]
    if sc.test_name:
        parts.append(sc.test_name)
    flat_key = f"{key}_{'_'.join(p.replace('/', '_') for p in parts)}"
    # Generic per-target key a Flow would set (e.g. "sim_pass_default")
    generic_key = f"{key}_{sc.target}"
    # `from_state` preserves the left-hand leg of the transition ("fail" in
    # `... @ fail -> pass`). It used to be parsed and dropped, which silently
    # degraded a fail->pass contract to a plain pass (F-53); carrying it into
    # the spec lets the acceptance report say whether the transition was
    # actually observed. It is not a threshold param — the threshold evaluator
    # only matches the _max/_min/_increase_at_most suffixes and skips it.
    specs.append(
        CriterionSpec(
            flat_key,
            mandatory=mandatory,
            flow_key_alias=generic_key,
            params={
                "tb_path": sc.tb,
                "target": sc.target,
                "from_state": sc.current,
                "test_selector": sc.test_name or "all",
            },
        )
    )


def _parse_dict_criterion(
    key: str,
    value: dict,
    *,
    mandatory: bool,
) -> list[CriterionSpec]:
    """Parse dict-valued criterion (parameterized, possibly with targets)."""
    targets = value.get("targets")
    params = {k: v for k, v in value.items() if k != "targets"}

    # Validate params via registry (synthesis_ok, fpga_impl_ok, etc.)
    if key in _CRITERION_PARAM_REGISTRY:
        _validate_criterion_params(key, params)

    if isinstance(targets, list):
        # Preserve the legacy all-string representation exactly. Besides being
        # the common path, old persisted criteria state and tests rely on one
        # spec carrying the whole Target list.
        if all(isinstance(target, str) for target in targets):
            return [
                CriterionSpec(
                    key, mandatory=mandatory, per_target=True, targets=targets, params=params
                )
            ]
        if key not in _PAIRED_TARGET_CRITERIA:
            raise ValueError(
                f"{key}.targets baseline/candidate mappings are only supported for "
                "synthesis_ok and fpga_impl_ok"
            )
        if not has_relative_qor_threshold(params):
            raise ValueError(
                f"{key}.targets baseline/candidate mappings require a relative threshold"
            )
        return _paired_target_specs(key, targets, params, mandatory=mandatory)
    return [CriterionSpec(key, mandatory=mandatory, params=params)]


def _paired_target_specs(
    key: str,
    targets: list[Any],
    params: dict[str, Any],
    *,
    mandatory: bool,
) -> list[CriterionSpec]:
    """Expand directed pairs while rejecting conflicting candidate identities."""
    specs: list[CriterionSpec] = []
    baselines_by_candidate: dict[str, str] = {}
    for index, raw_target in enumerate(targets):
        pair = parse_target_pair(raw_target, field=f"{key}.targets[{index}]")
        prior = baselines_by_candidate.get(pair.candidate)
        if prior is not None:
            if prior != pair.baseline:
                raise ValueError(
                    f"{key}.targets assigns conflicting baselines {prior!r} and "
                    f"{pair.baseline!r} to candidate {pair.candidate!r}"
                )
            continue
        baselines_by_candidate[pair.candidate] = pair.baseline
        pair_params = dict(params)
        if pair.baseline != pair.candidate:
            pair_params[BASELINE_TARGET_PARAM] = pair.baseline
        specs.append(
            CriterionSpec(
                key,
                mandatory=mandatory,
                per_target=True,
                targets=[pair.candidate],
                params=pair_params,
            )
        )
    return specs


def _validate_criterion_params(key: str, params: dict[str, Any]) -> None:
    """Validate criterion params using the registry. Raises ValueError on invalid.

    A param may be flat (``fmax_mhz_min``) or clock-scoped
    (``clk_i.fmax_mhz_min``) — the latter only for per-clock metrics
    (:data:`_PER_CLOCK_METRICS`). Mutex pairs are enforced *within each scope*
    (flat, and separately per clock).
    """
    valid_params, mutex_pairs = _CRITERION_PARAM_REGISTRY[key]
    unknown = []
    for param in params:
        clock, base = _split_clock_scope(param)
        if base not in valid_params or (
            clock and _param_base_metric(base) not in _PER_CLOCK_METRICS
        ):
            unknown.append(param)
    if unknown:
        raise ValueError(
            f"Unknown {key} params: {sorted(unknown)}. Valid: {sorted(valid_params)} "
            f"(per-clock metrics {sorted(_PER_CLOCK_METRICS)} may be clock-scoped "
            f"as '<clock>.<param>')"
        )
    for param, value in params.items():
        _validate_criterion_param_value(key, param, value)
    if key == "cycle_count":
        _validate_cycle_count_bounds(params)
    if (
        key == "mutation_score"
        and {"min_detected", "total"} <= params.keys()
        and params["min_detected"] > params["total"]
    ):
        raise ValueError("mutation_score min_detected cannot exceed total")
    # Mutex is per scope: clk_i.critical_path_ps_max and clk_i.fmax_mhz_min clash,
    # but clk_i.fmax_mhz_min and clk_2x.critical_path_ps_max do not.
    by_scope: dict[str, set[str]] = {}
    for param in params:
        clock, base = _split_clock_scope(param)
        by_scope.setdefault(clock, set()).add(base)
    for a, b in mutex_pairs:
        for scope, bases in by_scope.items():
            if a in bases and b in bases:
                where = f" (clock {scope!r})" if scope else ""
                raise ValueError(f"{key} params {a!r} and {b!r} are mutually exclusive{where}")


def _validate_criterion_param_value(key: str, param: str, value: Any) -> None:
    """Validate one registered criterion parameter value."""
    if key == "cycle_count":
        _validate_cycle_count_value(param, value)
        return
    if param == "scope":
        if not is_str_list(value) or not value or not all(path.strip() for path in value):
            raise ValueError(f"{key} param 'scope' must be a non-empty list[str]")
        return
    if param == "auto":
        if value is not True:
            raise ValueError(f"{key} param 'auto' must be true when present")
        return
    if key == "mutation_score" and param in {"min_detected", "total"}:
        if as_positive_int(value, 0) == 0:
            raise ValueError(f"{key} param {param!r} must be a positive integer, got {value!r}")
        return
    try:
        number = require_finite_number(value, field=f"{key} param {param!r}")
    except BoundaryError:
        raise ValueError(
            f"{key} param {param!r} must be a positive number, got {value!r}"
        ) from None
    if number < 0:
        raise ValueError(f"{key} param {param!r} must be a positive number or zero, got {value!r}")


def _validate_cycle_count_value(param: str, value: Any) -> None:
    """Validate one Cycle Count threshold under the settled numeric rules."""
    integer = param.endswith(("_max", "_min", "_cycles"))
    if integer:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(
                f"cycle_count param {param!r} must be a non-negative integer, got {value!r}"
            )
        return
    try:
        number = require_finite_number(value, field=f"cycle_count param {param!r}")
    except BoundaryError:
        raise ValueError(
            f"cycle_count param {param!r} must be a non-negative finite number, got {value!r}"
        ) from None
    if number < 0:
        raise ValueError(
            f"cycle_count param {param!r} must be a non-negative finite number, got {value!r}"
        )
    if param == "cycle_count_reduce_at_least" and number > 100:
        raise ValueError(f"cycle_count param {param!r} cannot exceed 100, got {value!r}")


def _validate_cycle_count_bounds(params: dict[str, Any]) -> None:
    """Reject same-unit Cycle Count bounds that can never be satisfied."""
    absolute_lower = params.get("cycle_count_min")
    absolute_upper = params.get("cycle_count_max")
    if (
        absolute_lower is not None
        and absolute_upper is not None
        and absolute_lower > absolute_upper
    ):
        raise ValueError("cycle_count has contradictory absolute min/max bounds")
    _validate_signed_bounds(params, unit="percent", suffix="")
    _validate_signed_bounds(params, unit="cycles", suffix="_cycles")


def _validate_signed_bounds(params: dict[str, Any], *, unit: str, suffix: str) -> None:
    """Validate composable signed lower/upper relative bounds for one unit."""
    lower = [
        params.get(f"cycle_count_increase_at_least{suffix}"),
        (
            -params[f"cycle_count_reduce_at_most{suffix}"]
            if f"cycle_count_reduce_at_most{suffix}" in params
            else None
        ),
    ]
    upper = [
        params.get(f"cycle_count_increase_at_most{suffix}"),
        (
            -params[f"cycle_count_reduce_at_least{suffix}"]
            if f"cycle_count_reduce_at_least{suffix}" in params
            else None
        ),
    ]
    lower_values = [value for value in lower if value is not None]
    upper_values = [value for value in upper if value is not None]
    if lower_values and upper_values and max(lower_values) > min(upper_values):
        raise ValueError(f"cycle_count has contradictory relative {unit} bounds")
