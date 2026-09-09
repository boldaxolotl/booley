"""Human-facing labels, requirements, and observations for console criteria.

The registry in this module is the single source of truth for how each built-in
criterion family is presented. Numeric threshold semantics remain owned by
``booley.criteria.thresholds``; this module only turns that metadata and the
corresponding evaluation evidence into concise UI text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from booley.core.boundary import as_dict, as_float, as_int, as_str
from booley.criteria.thresholds import ThresholdDescriptor, describe_threshold
from booley.evidence.timing import worst_fmax_from_json


@dataclass(frozen=True)
class _CriterionPresentation:
    """Human-facing identity and supporting detail for one criterion."""

    label: str
    detail: str


_ScopeKind = Literal["plain", "target", "cycle", "sim", "review_tb"]
_RequirementKind = Literal["fixed", "thresholds", "mutation", "coverage", "review"]
_ObservationKind = Literal["none", "thresholds", "coverage", "mutation", "lint", "review"]


@dataclass(frozen=True)
class _CriterionFamily:
    """Presentation policy for one durable criterion-key family."""

    prefix: str
    label: str
    scope: _ScopeKind
    requirement: _RequirementKind
    observation: _ObservationKind
    default_requirement: str = ""
    exact: bool = False

    def matches(self, key: str) -> bool:
        if self.exact:
            return key in {self.prefix, f"{self.prefix}_clean", f"{self.prefix}_done"}
        return key == self.prefix or key.startswith(f"{self.prefix}_")


_FAMILIES: tuple[_CriterionFamily, ...] = (
    _CriterionFamily(
        "elaborate_standalone",
        "Standalone elaboration",
        "plain",
        "fixed",
        "none",
        "every selected design elaborates",
    ),
    _CriterionFamily(
        "fpga_impl_ok",
        "FPGA implementation",
        "target",
        "thresholds",
        "thresholds",
        "implementation completes successfully",
    ),
    _CriterionFamily(
        "synthesis_ok",
        "ASIC synthesis",
        "target",
        "thresholds",
        "thresholds",
        "implementation completes successfully",
    ),
    _CriterionFamily(
        "mutation_score",
        "Mutation testing",
        "target",
        "mutation",
        "mutation",
    ),
    _CriterionFamily("cycle_count", "Cycle count", "cycle", "thresholds", "thresholds"),
    _CriterionFamily("lint_clean", "Lint", "target", "fixed", "lint", "no unwaived findings"),
    _CriterionFamily(
        "elab_pass",
        "Elaboration",
        "target",
        "fixed",
        "none",
        "every selected design elaborates",
    ),
    _CriterionFamily(
        "sim_pass",
        "Simulation",
        "sim",
        "fixed",
        "none",
        "all selected tests pass",
    ),
    _CriterionFamily("coverage", "Coverage", "target", "coverage", "coverage"),
    _CriterionFamily(
        "review_rtl_spec",
        "RTL specification review",
        "plain",
        "review",
        "review",
        exact=True,
    ),
    _CriterionFamily(
        "review_rtl_bugs",
        "RTL bugs review",
        "plain",
        "review",
        "review",
        exact=True,
    ),
    _CriterionFamily(
        "review_rtl_protocol",
        "RTL protocol review",
        "plain",
        "review",
        "review",
        exact=True,
    ),
    _CriterionFamily(
        "review_rtl_code_style",
        "RTL code-style review",
        "plain",
        "review",
        "review",
        exact=True,
    ),
    _CriterionFamily(
        "review_rtl_optimization",
        "RTL optimization review",
        "plain",
        "review",
        "review",
        exact=True,
    ),
    _CriterionFamily(
        "review_rtl_security",
        "RTL security review",
        "plain",
        "review",
        "review",
        exact=True,
    ),
    _CriterionFamily(
        "review_tb_quality",
        "Testbench quality review",
        "review_tb",
        "review",
        "review",
        exact=True,
    ),
)

_METRIC_LABELS: dict[str, tuple[str, str]] = {
    "area_um2": ("area", " μm²"),
    "area_kge": ("area", " kGE"),
    "area": ("area", ""),
    "cell_count": ("cells", ""),
    "wire_count": ("wires", ""),
    "critical_path_ps": ("critical path", " ps"),
    "fmax_mhz": ("Fmax", " MHz"),
    "lut_count": ("LUTs", ""),
    "ff_count": ("FFs", ""),
    "bram_count": ("BRAMs", ""),
    "dsp_count": ("DSPs", ""),
}


def _family_for(key: str) -> _CriterionFamily | None:
    return next((family for family in _FAMILIES if family.matches(key)), None)


def _target_name(key: str, family: _CriterionFamily, params: dict) -> str:
    target = as_str(params.get("target"))
    if target:
        return target
    prefix = f"{family.prefix}_"
    return key.removeprefix(prefix) if key.startswith(prefix) else ""


def _short_path(value: object) -> str:
    path = as_str(value)
    return path.replace("\\", "/").rsplit("/", 1)[-1] if path else ""


def _simulation_context(params: dict) -> str:
    parts = []
    tb_name = _short_path(params.get("tb_path"))
    if tb_name:
        parts.append(f"tb {tb_name}")
    selector = as_str(params.get("test_selector"))
    if selector and selector != "all":
        parts.append(f"test {selector}")
    return " · ".join(parts)


def _criterion_identity(key: str, family: _CriterionFamily, params: dict) -> tuple[str, str]:
    if family.scope == "plain":
        return family.label, ""
    if family.scope == "review_tb":
        target = as_str(params.get("target"))
        return family.label, f"target {target}" if target else ""
    target = _target_name(key, family, params)
    if family.scope == "cycle":
        test = as_str(params.get("test"))
        label = f"{family.label} · {test}" if test else family.label
        return label, f"target {target}" if target else ""
    if family.scope == "sim":
        label = f"{family.label} · {target}" if target else family.label
        return label, _simulation_context(params)
    return (f"{family.label} · {target}" if target else family.label, "")


def _format_number(value: object) -> str:
    number = as_float(value)
    if number is None:
        return str(value)
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.3f}".rstrip("0").rstrip(".")


def _format_percent(value: object) -> str:
    if isinstance(value, str) and value.endswith("%"):
        return value
    return f"{_format_number(value)}%"


def _split_threshold_scope(param: str) -> tuple[str, str]:
    if "." not in param:
        return "", param
    return param.rsplit(".", 1)


def _thresholds(params: dict) -> list[tuple[str, ThresholdDescriptor, object]]:
    thresholds = []
    for raw_param, value in params.items():
        param = str(raw_param)
        if param.startswith("_"):
            continue
        scope, threshold = _split_threshold_scope(param)
        descriptor = describe_threshold(threshold)
        if descriptor is not None:
            thresholds.append((scope, descriptor, value))
    return thresholds


def _metric_parts(scope: str, descriptor: ThresholdDescriptor) -> tuple[str, str]:
    if descriptor.metric == "cycle_count":
        return scope, " cycles"
    label, suffix = _METRIC_LABELS.get(
        descriptor.metric, (descriptor.metric.replace("_", " "), "")
    )
    return " ".join(part for part in (scope, label) if part), suffix


def _display_operator(descriptor: ThresholdDescriptor) -> str:
    operator = descriptor.operator
    if descriptor.relative and "_reduce_" in descriptor.param:
        operator = "ge" if operator == "le" else "le"
    return "≤" if operator == "le" else "≥"


def _format_threshold_requirement(
    scope: str, descriptor: ThresholdDescriptor, value: object
) -> str:
    metric, suffix = _metric_parts(scope, descriptor)
    if descriptor.relative:
        change = "reduction" if "_reduce_" in descriptor.param else "increase"
        unit = (
            f"{_format_number(value)} cycles"
            if descriptor.unit == "cycles"
            else _format_percent(value)
        )
        return " ".join(
            part for part in (metric, change, _display_operator(descriptor), unit) if part
        )
    value_text = f"{_format_number(value)}{suffix}"
    return " ".join(part for part in (metric, _display_operator(descriptor), value_text) if part)


def _format_threshold_requirement_set(params: dict) -> str:
    return " · ".join(
        _format_threshold_requirement(scope, descriptor, value)
        for scope, descriptor, value in _thresholds(params)
    )


def _format_coverage_requirement(params: dict) -> str:
    metrics = as_dict(params.get("metrics"), default={})
    parts = []
    for metric, policy_value in metrics.items():
        policy = as_dict(policy_value, default={})
        if "min_pct" in policy:
            label = str(metric).replace("_", " ")
            parts.append(f"{label} ≥ {_format_percent(policy['min_pct'])}")
    return " · ".join(parts) or "meets its configured coverage policy"


def _format_requirement(key: str, family: _CriterionFamily, params: dict) -> str:
    requirement = family.default_requirement
    if family.requirement == "thresholds":
        rendered = _format_threshold_requirement_set(params)
        if rendered:
            requirement = rendered
        elif family.prefix == "cycle_count":
            requirement = "passes its configured Cycle Count threshold"
    elif family.requirement == "mutation":
        minimum = as_int(params.get("min_detected"))
        total = as_int(params.get("total"))
        if minimum is not None and total is not None:
            requirement = f"≥ {minimum:,}/{total:,} mutations detected"
        else:
            requirement = "meets its configured mutation threshold"
    elif family.requirement == "coverage":
        requirement = _format_coverage_requirement(params)
    elif family.requirement == "review":
        requirement = "review completed" if key.endswith("_done") else "no open findings"
    elif family.prefix == "sim_pass" and params.get("from_state") == "fail":
        requirement = "fail → pass transition"
    return requirement


def _format_coverage_metric(key: str, d: dict, p: dict, stale: bool) -> str | None:
    """Coverage status fallback, or ``None`` for another criterion family."""
    family = _family_for(key)
    if family is None or family.prefix != "coverage":
        return None
    status = d.get("status")
    return "?" if stale else str(status or "")


def _format_fpga_impl_metric(d: dict, stale: bool) -> str:
    """Compact FPGA implementation summary used when no threshold is configured."""
    if stale:
        return "?"
    luts = as_int(d.get("lut_count"))
    ffs = as_int(d.get("ff_count"))
    wns = as_float(d.get("wns_ns"))
    parts = []
    if luts is not None:
        parts.append(f"{luts / 1000:.1f}k LUTs" if luts >= 1000 else f"{luts} LUTs")
    if ffs is not None:
        parts.append(f"{ffs / 1000:.1f}k FFs" if ffs >= 1000 else f"{ffs} FFs")
    if wns is not None:
        parts.append(f"WNS {wns:.2f}ns")
    return " | ".join(parts) if parts else ""


def _format_synthesis_metric(d: dict, stale: bool) -> str:
    """Compact synthesis summary used when no threshold is configured."""
    if stale:
        return "?"
    cells = as_int(d.get("cells"))
    fmax = worst_fmax_from_json(as_dict(d.get("per_clock"), default={}))
    parts = []
    if cells is not None:
        parts.append(f"{cells / 1000:.1f}k cells" if cells >= 1000 else f"{cells} cells")
    if fmax is not None:
        parts.append(f"{fmax:.0f}MHz")
    return " · ".join(parts) if parts else ""


def _format_cycle_metric(d: dict, stale: bool) -> str:
    """Cycle count fallback: current count with optional baseline delta."""
    if stale:
        return "?"
    current = as_int(d.get("cycles"))
    baseline = as_int(d.get("baseline_cycles"))
    if current is None:
        return ""
    if baseline is None:
        return f"{current:,} cycles"
    return f"{baseline:,} → {current:,} cycles ({current - baseline:+,})"


def _check_for(detail: dict, param: str) -> dict:
    checks = detail.get("checks")
    if not isinstance(checks, list):
        return {}
    for raw_check in checks:
        check = as_dict(raw_check, default={})
        if as_str(check.get("param")) == param:
            return check
    return {}


def _relative_observation(
    descriptor: ThresholdDescriptor, check: dict
) -> tuple[str, object] | None:
    current = as_float(check.get("current"))
    baseline = as_float(check.get("baseline"))
    if current is not None and baseline is not None:
        delta = current - baseline
        measured = delta if descriptor.unit == "cycles" else None
        if descriptor.unit == "percent" and baseline != 0:
            measured = delta / baseline * 100
    else:
        measured = as_float(check.get("delta_cycles" if descriptor.unit == "cycles" else "pct"))
    if measured is None:
        return None
    change = "reduction" if "_reduce_" in descriptor.param else "increase"
    return change, abs(measured) if change == "reduction" else measured


def _format_threshold_observation(scope: str, descriptor: ThresholdDescriptor, check: dict) -> str:
    metric, suffix = _metric_parts(scope, descriptor)
    if descriptor.relative:
        relative = _relative_observation(descriptor, check)
        if relative is None:
            return ""
        change, measured = relative
        value = (
            f"{_format_number(measured)} cycles"
            if descriptor.unit == "cycles"
            else _format_percent(measured)
        )
        return " ".join(part for part in (metric, change, value) if part)
    value = check.get("value", check.get("current"))
    if as_float(value) is None:
        return ""
    return " ".join(part for part in (metric, f"{_format_number(value)}{suffix}") if part)


def _format_threshold_observations(params: dict, detail: dict) -> str:
    observations = []
    for scope, descriptor, _ in _thresholds(params):
        full_param = f"{scope}.{descriptor.param}" if scope else descriptor.param
        check = _check_for(detail, full_param)
        if check:
            rendered = _format_threshold_observation(scope, descriptor, check)
            if rendered:
                observations.append(rendered)
    return " · ".join(observations)


def _coverage_metrics(detail: dict) -> list[dict]:
    metrics = detail.get("metrics")
    if not isinstance(metrics, list):
        evaluation = as_dict(detail.get("evaluation"), default={})
        metrics = evaluation.get("metrics")
    if not isinstance(metrics, list):
        return []
    return [as_dict(metric, default={}) for metric in metrics]


def _format_coverage_observation(params: dict, detail: dict) -> str:
    configured = as_dict(params.get("metrics"), default={})
    if not configured:
        return str(detail.get("status") or "")
    observed = {
        as_str(metric.get("metric")): metric.get("actual_percent")
        for metric in _coverage_metrics(detail)
    }
    parts = []
    for metric in configured:
        value = observed.get(str(metric))
        if as_float(value) is not None:
            parts.append(f"{str(metric).replace('_', ' ')} {_format_percent(value)}")
    return " · ".join(parts)


def _format_simple_observation(family: _CriterionFamily, detail: dict) -> str:
    observation = ""
    if family.observation == "mutation":
        detected = as_int(detail.get("detected"))
        total = as_int(detail.get("total_valid"))
        if detected is not None and total:
            observation = f"{detected}/{total} ({detected / total * 100:.0f}%)"
    elif family.observation == "lint":
        warnings = as_int(detail.get("warnings"))
        if warnings is not None:
            observation = f"{warnings} warnings" if warnings else "clean"
    elif family.observation == "review":
        issues = as_int(detail.get("issues"))
        if issues is not None:
            observation = f"{issues} issues" if issues else "clean"
    return observation


def _format_observation(family: _CriterionFamily, detail: dict, params: dict, stale: bool) -> str:
    if stale:
        return "?"
    observation = ""
    if family.observation == "thresholds":
        rendered = _format_threshold_observations(params, detail)
        if rendered or _thresholds(params):
            observation = rendered
        elif family.prefix == "fpga_impl_ok":
            observation = _format_fpga_impl_metric(detail, False)
        elif family.prefix == "synthesis_ok":
            observation = _format_synthesis_metric(detail, False)
        else:
            observation = _format_cycle_metric(detail, False)
    elif family.observation == "coverage":
        observation = _format_coverage_observation(params, detail)
    else:
        observation = _format_simple_observation(family, detail)
    return observation


def _format_metric(key: str, entry: object) -> str:
    """Return concise evidence aligned with this criterion's requirement."""
    family = _family_for(key)
    if family is None:
        return ""
    data = as_dict(entry, default={})
    detail = as_dict(data.get("detail"), default={})
    params = as_dict(data.get("params"), default={})
    return _format_observation(family, detail, params, data.get("stale") is True)


def _format_criterion_presentation(key: str, entry: object) -> _CriterionPresentation:
    """Build the complete human-facing presentation for one criterion."""
    family = _family_for(key)
    if family is None:
        return _CriterionPresentation(label=key, detail="")
    data = as_dict(entry, default={})
    params = as_dict(data.get("params"), default={})
    label, context = _criterion_identity(key, family, params)
    requirement = _format_requirement(key, family, params)
    observation = _format_metric(key, data)
    detail_parts = [context] if context else []
    if requirement:
        qualifier = "required" if data.get("mandatory", True) else "goal"
        detail_parts.append(f"{qualifier} {requirement}")
    if observation and observation != "?":
        detail_parts.append(f"observed {observation}")
    return _CriterionPresentation(label=label, detail=" · ".join(detail_parts))
