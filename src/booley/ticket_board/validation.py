"""Ticket field validation and log artifact validation."""

from __future__ import annotations

import fnmatch
import re
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from booley.dev_support.criteria import PER_TARGET_CRITERIA, TARGET_CAMPAIGN_CRITERIA

from .constants import (
    CRITERION_FLOW_MAP,
    DEPRECATED_FIELDS,
    KNOWN_FIELDS,
    REQUIRED_FIELDS,
    RUNTIME_FIELDS,
    VALID_PRIORITIES,
    VALID_TYPES,
)
from .git_status import parse_porcelain_v1_z
from .validation_logs import (  # noqa: F401  # re-exported for backward compatibility
    _validate_state_file,
    format_validate_logs_report,
    validate_logs,
)

# ---------------------------------------------------------------------------
# Step meta value validators (moved from constants.py)
# ---------------------------------------------------------------------------
# Each entry: list of (severity, check_fn, message) tuples.
# severity: "hard" = blocks step completion, "soft" = warning persisted in _gate_warnings.
# check_fn(meta_entry, ctx) -> bool (True = pass, False = fail).
# ctx keys: "type" (ticket type), "all_step_meta" (full step-meta dict).


def no_unfixed_critical(meta: dict[str, Any]) -> bool:
    """Check that no CRITICAL-severity issues remain unfixed."""
    critical_found = meta.get("critical_found", 0)
    critical_fixed = meta.get("critical_fixed", 0)
    if critical_found == 0:
        return True
    return critical_fixed >= critical_found


def _no_unfixed_critical_or_major(meta):
    """Check that no CRITICAL or MAJOR issues remain unfixed."""
    if not no_unfixed_critical(meta):
        return False
    major_found = meta.get("major_found", 0)
    major_fixed = meta.get("major_fixed", 0)
    if major_found == 0:
        return True
    return major_fixed >= major_found


def _rtl_modified_in_final_review(ctx):
    """Check if rtl-review-final fixed any issues (implying RTL changes)."""
    all_meta = ctx.get("all_step_meta", {})
    final = all_meta.get("rtl-review-final", {})
    return final.get("issues_fixed", 0) > 0


def no_large_area_increase(meta: dict[str, Any], threshold: float = 50.0) -> bool:
    """Check that no synthesis target has area increase > threshold%.

    Returns False (gate failure) if any target exceeds the threshold OR if
    delta_pct cannot be parsed — malformed data must not silently pass.
    The literal string "N/A" means no baseline was available (first-run
    synthesis); such targets are skipped, not treated as malformed.
    """
    for tgt in meta.get("targets", []):
        delta = tgt.get("delta_pct", "+0%")
        if str(delta).strip() == "N/A":
            continue  # no baseline to compare against; skip this target
        try:
            val = float(str(delta).replace("%", "").replace("+", ""))
            if val > threshold:
                return False
        except (ValueError, TypeError):
            return False  # malformed delta — fail the gate, don't silently pass
    return True


STEP_META_VALIDATORS = {
    "implementation": [
        (
            "hard",
            lambda m, ctx: m.get("diff_lines_added", 0) > 0,
            "no lines added -- implementation produced no changes",
        ),
        (
            "soft",
            lambda m, ctx: m.get("diff_lines_added", 0) <= 500,
            "large change (>500 lines added) -- review carefully",
        ),
    ],
    "rtl-review-1": [
        ("hard", lambda m, ctx: no_unfixed_critical(m), "unfixed CRITICAL issues remain"),
    ],
    "tb-review": [
        (
            "hard",
            lambda m, ctx: _no_unfixed_critical_or_major(m),
            "unfixed CRITICAL or MAJOR issues remain in testbench review",
        ),
    ],
    "sim-debug-loop": [
        ("hard", lambda m, ctx: m.get("converged") is True, "simulation did not converge"),
        (
            "hard",
            lambda m, ctx: m.get("configs_failed", 0) == 0 or bool(m.get("known_failures")),
            "configs_failed is not 0 (and no known_failures exemption)",
        ),
        (
            "hard",
            lambda m, ctx: m.get("debug_rounds_used", 0) <= m.get("debug_rounds_max", 10),
            "exceeded max debug rounds",
        ),
    ],
    "rtl-mutation-testing": [
        (
            "soft",
            lambda m, ctx: m.get("detection_rate", 0) >= 0.5,
            "mutation detection rate below 50%",
        ),
    ],
    "rtl-review-final": [
        ("hard", lambda m, ctx: no_unfixed_critical(m), "unfixed CRITICAL issues remain"),
    ],
    "post-review-sim": [
        (
            "hard",
            lambda m, ctx: (
                m.get("configs_failed", 1) == 0 or not _rtl_modified_in_final_review(ctx)
            ),
            "post-review sim has failures after RTL was modified in final review",
        ),
    ],
    "synthesis": [
        (
            "soft",
            lambda m, ctx: no_large_area_increase(m, threshold=50.0),
            "synthesis area increased >50% in one or more configs",
        ),
    ],
    "acceptance-check": [
        (
            "hard",
            lambda m, ctx: m.get("reviewer_verdict") != "fail",
            "adversarial reviewer rejected the ticket",
        ),
        (
            "hard",
            lambda m, ctx: m.get("reviewer_verdict") is not None,
            "reviewer_verdict is missing -- adversarial reviewer did not run",
        ),
        (
            "hard",
            lambda m, ctx: (
                (m.get("criteria_passed", 0) + m.get("criteria_failed", 0))
                == m.get("criteria_total", -1)
            ),
            "criteria_passed + criteria_failed != criteria_total (criteria were skipped)",
        ),
    ],
}


def _valid_campaign_scope(value: Any) -> bool:
    """Whether *value* is a non-empty list of non-empty paths."""
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(path, str) and path.strip() for path in value)
    )


def _validate_campaign_item(prefix: str, item: dict, errors: list[str]) -> None:
    """Validate one ``{target, scope, ...}`` campaign entry."""
    target = item.get("target")
    if not isinstance(target, str) or not target.strip():
        errors.append(f"{prefix}.target must be a non-empty string")
    if not _valid_campaign_scope(item.get("scope")):
        errors.append(f"{prefix}.scope must be a non-empty list[str]")


def _validate_criterion_list(section_name, key, value, errors):
    """Validate list-valued criteria, including Target campaign entries."""
    for i, item in enumerate(value):
        if isinstance(item, str) and not item.strip():
            errors.append(f"criteria.{section_name}.{key}[{i}]: empty string")
        elif not isinstance(item, (str, dict)):
            errors.append(
                f"criteria.{section_name}.{key}[{i}]: "
                f"items must be strings or dicts, got {type(item).__name__}"
            )
        elif key in TARGET_CAMPAIGN_CRITERIA and isinstance(item, dict):
            _validate_campaign_item(f"criteria.{section_name}.{key}[{i}]", item, errors)
    if key in TARGET_CAMPAIGN_CRITERIA and not all(isinstance(item, dict) for item in value):
        errors.append(f"criteria.{section_name}.{key}: use a list of Target campaign dicts")


def _validate_criterion_dict(section_name, key, value, errors):
    """Validate parameterized and legacy multi-Target criterion dictionaries."""
    targets = value.get("targets")
    if targets is not None and not isinstance(targets, list):
        errors.append(f"criteria.{section_name}.{key}.targets must be a list")
    if key in PER_TARGET_CRITERIA and not isinstance(targets, list):
        errors.append(
            f"criteria.{section_name}.{key}: per-target criterion requires a targets list"
        )
    if key in TARGET_CAMPAIGN_CRITERIA and not _valid_campaign_scope(value.get("scope")):
        errors.append(f"criteria.{section_name}.{key}.scope must be a non-empty list[str]")
    if isinstance(targets, list):
        from booley.dev_support.criteria import (
            has_relative_qor_threshold,
            parse_target_pair,
        )

        pair_entries = [item for item in targets if isinstance(item, dict)]
        if pair_entries and key not in {"synthesis_ok", "fpga_impl_ok"}:
            errors.append(
                f"criteria.{section_name}.{key}.targets: baseline/candidate mappings "
                "are only supported for synthesis_ok and fpga_impl_ok"
            )
        params = {name: item for name, item in value.items() if name != "targets"}
        if pair_entries and not has_relative_qor_threshold(params):
            errors.append(
                f"criteria.{section_name}.{key}.targets: baseline/candidate mappings "
                "require a relative threshold"
            )
        baselines_by_candidate: dict[str, str] = {}
        for index, item in enumerate(targets):
            try:
                pair = parse_target_pair(
                    item,
                    field=f"criteria.{section_name}.{key}.targets[{index}]",
                )
            except ValueError as exc:
                errors.append(str(exc))
                continue
            prior = baselines_by_candidate.get(pair.candidate)
            if prior is not None and prior != pair.baseline:
                errors.append(
                    f"criteria.{section_name}.{key}.targets assigns conflicting "
                    f"baselines {prior!r} and {pair.baseline!r} to candidate "
                    f"{pair.candidate!r}"
                )
            else:
                baselines_by_candidate[pair.candidate] = pair.baseline


def _validate_criterion_value(section_name, key, value, errors):
    """Validate a single criterion value (list, dict, or scalar)."""
    if isinstance(value, list):
        _validate_criterion_list(section_name, key, value, errors)
    elif isinstance(value, dict):
        _validate_criterion_dict(section_name, key, value, errors)
    elif isinstance(value, (str, bool, int, float, type(None))):
        if key in PER_TARGET_CRITERIA:
            errors.append(
                f"criteria.{section_name}.{key}: per-target criterion "
                f"requires a list (e.g. [{key}: [default]]) not a scalar"
            )
    else:
        errors.append(
            f"criteria.{section_name}.{key}: "
            f"value must be scalar, list, or dict, got {type(value).__name__}"
        )


def _validate_cycle_count_grammar(criteria: dict[str, Any], errors: list[str]) -> None:
    """Apply the runtime Cycle Count grammar during section validation."""
    cycle_count_sections: dict[str, dict[str, Any]] = {}
    for section_name in ("mandatory", "optional"):
        section = criteria.get(section_name)
        if isinstance(section, dict) and "cycle_count" in section:
            cycle_count_sections[section_name] = {"cycle_count": section["cycle_count"]}
    if not cycle_count_sections:
        return

    from booley.dev_support.criteria import CriteriaTemplate

    try:
        CriteriaTemplate.from_yaml(cycle_count_sections)
    except (ValueError, KeyError, TypeError) as exc:
        errors.append(f"criteria: {exc}")


def _validate_coverage_grammar(criteria: dict[str, Any], errors: list[str]) -> None:
    """Apply the runtime Coverage Criterion grammar during Preflight."""
    coverage_sections: dict[str, dict[str, Any]] = {}
    for section_name in ("mandatory", "optional"):
        section = criteria.get(section_name)
        if isinstance(section, dict) and "coverage" in section:
            coverage_sections[section_name] = {"coverage": section["coverage"]}
    if not coverage_sections:
        return

    from booley.dev_support.criteria import CriteriaTemplate

    try:
        CriteriaTemplate.from_yaml(coverage_sections)
    except (ValueError, KeyError, TypeError) as exc:
        errors.append(f"criteria: {exc}")


def validate_criteria_section(criteria: Any) -> list[str]:
    """Validate a ticket's ``criteria:`` section. Returns error strings."""
    errors: list[str] = []
    if not isinstance(criteria, dict):
        errors.append("criteria must be a dict with 'mandatory' and/or 'optional' keys")
        return errors

    unknown_top = set(criteria.keys()) - {"mandatory", "optional"}
    if unknown_top:
        errors.append(f"criteria: unknown top-level keys: {', '.join(sorted(unknown_top))}")

    for section_name in ("mandatory", "optional"):
        section = criteria.get(section_name)
        if section is None:
            continue
        if not isinstance(section, dict):
            errors.append(f"criteria.{section_name} must be a dict")
            continue
        for key, value in section.items():
            if not isinstance(key, str) or not key:
                errors.append(f"criteria.{section_name}: keys must be non-empty strings")
                continue
            _validate_criterion_value(section_name, key, value, errors)

    mandatory = criteria.get("mandatory")
    if not mandatory or (isinstance(mandatory, dict) and len(mandatory) == 0):
        errors.append("criteria.mandatory must contain at least one criterion")

    if not errors:
        _validate_cycle_count_grammar(criteria, errors)
    if not errors:
        _validate_coverage_grammar(criteria, errors)

    return errors


def _validate_criteria_type_rules(
    criteria: dict, ticket_type: str, errors: list[str], warnings: list[str]
) -> None:
    """Check type-specific rules on structured sim criteria entries (warnings only)."""
    sim_entries: list[tuple[str, str]] = []  # (entry_str, section_name)
    for section_name in ("mandatory", "optional"):
        section = criteria.get(section_name, {})
        if not isinstance(section, dict):
            continue
        for value in section.values():
            if not isinstance(value, list):
                continue
            for item in value:
                if isinstance(item, str) and "->" in item:
                    sim_entries.append((item, section_name))

    if ticket_type == "bugfix":
        has_fail_to_pass = any(_parse_transition(e) == ("fail", "pass") for e, _ in sim_entries)
        if sim_entries and not has_fail_to_pass:
            warnings.append(
                "[warning] Bugfix tickets typically have at least one sim entry "
                "with 'fail -> pass'"
            )
    elif ticket_type == "refactor":
        _REFACTOR_ALLOWED = {("pass", "pass"), ("none", "pass")}
        non_pass = [e for e, _ in sim_entries if _parse_transition(e) not in _REFACTOR_ALLOWED]
        if non_pass:
            warnings.append(
                "[warning] Refactor tickets typically have all sim entries as "
                "'pass -> pass' or 'none -> pass'"
            )


def _parse_transition(entry: str) -> tuple[str, str]:
    """Extract (current, expected) from a structured sim criterion string."""
    if "->" not in entry:
        return ("", "")
    left, expected = entry.rsplit("->", 1)
    expected = expected.strip()
    parts = [p.strip() for p in left.split("@")]
    current = parts[-1] if parts else ""
    return (current, expected)


def _validate_basic_fields(fields: dict[str, Any], body: str) -> list[str]:
    """Validate required fields, type, body, priority, and field names."""
    errors: list[str] = []

    for f in REQUIRED_FIELDS:
        if f not in fields or fields[f] is None or fields[f] == "":
            msg = f"Missing required field: {f}"
            # Name the legal shape right in the error (A-4): the accepted
            # criterion schema otherwise has to be dug out of the Python package.
            if f == "criteria":
                from booley.dev_support.criteria import PER_TARGET_CRITERIA

                names = ", ".join(sorted(PER_TARGET_CRITERIA))
                msg += (
                    f" — e.g. criteria: {{mandatory: {{sim_pass: {{targets: [sim]}}}}}}; "
                    f"per-target criterion names: {names}"
                )
            elif f == "scope":
                msg += " — e.g. scope: [rtl/verilog/] (files/dirs the ticket may touch)"
            errors.append(msg)

    # Type validation
    ticket_type = fields.get("type", "")
    if ticket_type and ticket_type not in VALID_TYPES:
        errors.append(
            f"Invalid type '{ticket_type}'. Must be one of: {', '.join(sorted(VALID_TYPES))}"
        )

    # Spec is informational — not required for any ticket type

    # Body must contain a description heading.
    if "## Description" not in body:
        errors.append("Body must contain a '## Description' section")

    # Priority validation
    prio = fields.get("priority")
    if prio is not None and prio not in VALID_PRIORITIES:
        errors.append(
            f"Invalid priority '{prio}'. Must be one of: {', '.join(sorted(VALID_PRIORITIES))}"
        )

    errors.extend(retired_ticket_field_errors(fields))

    # Unknown field detection
    unknown = set(fields) - KNOWN_FIELDS - RUNTIME_FIELDS
    unknown -= set(DEPRECATED_FIELDS)  # already reported above
    if unknown:
        errors.append(f"Unknown fields: {', '.join(sorted(unknown))}")

    return errors


def retired_ticket_field_errors(fields: Mapping[str, Any]) -> list[str]:
    """Return hard migration errors for retired Ticket frontmatter fields."""
    return [
        f"Deprecated field '{field}': {DEPRECATED_FIELDS[field]}"
        for field in fields
        if field in DEPRECATED_FIELDS
    ]


def _validate_scope(
    fields: dict[str, Any], check_files: bool, project_root: str | Path | None
) -> tuple[list[str], list[str]]:
    """Validate scope field type and file existence. Returns (errors, scope_list)."""
    errors: list[str] = []

    scope = fields.get("scope")
    if scope is not None and not isinstance(scope, list):
        errors.append("Field 'scope' must be a list")
    scope = scope if isinstance(scope, list) else []
    if not scope:
        errors.append("scope is empty — at least one file required")

    # Reject absolute paths — scope entries must be relative to project root
    for entry in scope:
        raw = entry.removesuffix(" [new]")
        if Path(raw).is_absolute():
            errors.append(f"Scope entry must be a relative path: {raw}")

    errors.extend(_validate_no_duplicated_source_roots(scope, project_root))

    # File existence checks — skip when scope is unknown (["*"] sentinel for bugfix tickets)
    if check_files and project_root and scope != ["*"]:
        root = Path(project_root)
        for entry in scope:
            is_new = entry.endswith(" [new]")
            path = entry.removesuffix(" [new]")
            if is_new or Path(path).is_absolute():
                continue
            if any(c in path for c in ("*", "?", "[")):
                if not list(root.glob(path)):
                    errors.append(f"Scope glob matches no files: {path}")
            elif not (root / path).exists():
                errors.append(f"Scope file not found: {path}")
        # Spec is informational — no file existence check

    return errors, scope


def _validate_on_success(value: Any) -> list[str]:
    """Validate the optional completion-policy block at the ticket boundary."""
    if value is None:
        return []
    if not isinstance(value, dict):
        return ["Field 'on_success' must be a mapping"]
    from booley.core.models import OnSuccess

    return OnSuccess.from_dict(value).validate()


def _validate_no_duplicated_source_roots(
    scope: list[str],
    project_root: str | Path | None,
) -> list[str]:
    """Reject scope paths like rtl/rtl/foo.sv or verif/verif/foo.sv."""
    source_roots = _configured_source_roots(project_root)
    errors: list[str] = []
    for entry in scope:
        path = entry.removesuffix(" [new]").replace("\\", "/").strip()
        while path.startswith("./"):
            path = path[2:]
        parts = [part for part in path.split("/") if part]
        if parts == ["*"]:
            continue
        for root in source_roots:
            root_parts = [part for part in root.replace("\\", "/").split("/") if part]
            if not root_parts:
                continue
            repeated = root_parts + root_parts
            if parts[: len(repeated)] == repeated:
                errors.append(f"Scope entry has duplicated source root '{root}': {path}")
                break
    return errors


def _configured_source_roots(project_root: str | Path | None) -> list[str]:
    """Return known source roots for duplicated-root validation.

    Seeds the conventional roots and augments them with the project's actual
    ``.core`` source entries (ADR 0026 follow-through).
    """
    roots = {"rtl", "tb", "verif", "fw"}
    if project_root:
        try:
            from booley.fusesoc.fusesoc_registry import source_dirs_from_core

            rtl_dirs, tb_dirs, tb_incl = source_dirs_from_core(Path(project_root))
            roots.update(str(d).rstrip("/") for d in (*rtl_dirs, *tb_dirs, *tb_incl))
        except Exception:  # noqa: BLE001 — best-effort probe; falls back to default roots
            pass
    return sorted(root for root in roots if root)


def _validate_criteria(
    fields: dict[str, Any],
    body: str,
    ticket_type: str,
    check_tb_files: bool,
    project_root: str | Path | None,
) -> tuple[list[str], list[str]]:
    """Validate criteria section. Returns (errors, warnings)."""
    criteria = fields.get("criteria")
    if criteria is None:
        return [], []

    errors: list[str] = []
    warnings: list[str] = []
    criteria_errors = validate_criteria_section(criteria)
    errors.extend(criteria_errors)

    if not isinstance(criteria, dict):
        return errors, warnings

    # Retired-key guard: fail here, pre-flight, with the same actionable rename
    # hint the harness produces at intake — otherwise a ticket authored before a
    # criteria-key rename passes validate-ticket clean and only fails opaquely
    # mid-run (see booley.dev_support.criteria.RETIRED_CRITERIA).
    errors.extend(_validate_retired_criteria(criteria))
    errors.extend(_validate_known_mandatory_criteria(criteria, project_root))

    # Structured sim entry validation. Sealed Tickets resolve Targets only after
    # setup materializes their contract checkout; project_root is the destination
    # checkout here and must not substitute for that immutable view.
    errors.extend(_validate_sim_entries(criteria))
    if project_root and fields.get("target_contract") is None:
        errors.extend(_validate_sim_targets(criteria, fields, body, project_root))

    # Type-specific criteria rules (warnings only, no structural errors)
    if not criteria_errors:
        _validate_criteria_type_rules(criteria, ticket_type, errors, warnings)

    # Disabled-Flow coherence
    if project_root:
        errors.extend(_validate_flow_coherence(criteria, project_root))

    # TB filename resolution against tb_source_prefixes
    if check_tb_files and project_root:
        errors.extend(_validate_tb_paths(criteria, fields, body, project_root))

    # Parameterized-criterion params (synthesis_ok/fpga_impl_ok): validate each
    # dict criterion's params against the registry directly and unconditionally,
    # so an unknown param like `configs` (the scoping key is `targets`) is
    # rejected here — precisely, per criterion — instead of surfacing only as a
    # mid-run CRITICAL traceback from CriteriaTemplate.from_yaml() at ticket intake.
    errors.extend(_validate_criteria_params(criteria))

    # Full-parse parity guard: run the same CriteriaTemplate.from_yaml() the
    # harness invokes at ticket intake, catching anything the checks above miss. Gated
    # on a clean result so it doesn't cascade noise onto an already-flagged
    # section (and so it never double-reports the param errors above).
    if not errors:
        from booley.dev_support.criteria import CriteriaTemplate

        try:
            CriteriaTemplate.from_yaml(criteria)
        except (ValueError, KeyError, TypeError) as exc:
            errors.append(f"criteria: {exc}")

    return errors, warnings


def _validate_known_mandatory_criteria(
    criteria: dict[str, Any],
    project_root: str | Path | None,
) -> list[str]:
    """Reject mandatory criteria absent from the catalog or live tool registry."""
    definitions, satisfying = _live_criterion_registry(project_root)
    mandatory = criteria.get("mandatory", {})
    if not isinstance(mandatory, dict):
        return []

    errors: list[str] = []
    for key in mandatory:
        family = _criterion_family(key, definitions)
        if family is None:
            errors.append(
                f"criteria.mandatory.{key}: no such registered criterion; "
                "run 'booley cheat --criteria' and choose a listed criterion name"
            )
        elif family not in satisfying:
            errors.append(
                f"criteria.mandatory.{key}: no enabled Flow or Specialist can satisfy "
                "this criterion; run 'booley cheat --criteria' and choose one with a "
                "'Set by' entry"
            )
    return errors


def _live_criterion_registry(
    project_root: str | Path | None,
) -> tuple[set[str], set[str]]:
    """Return merged criterion names and families owned by enabled endpoints."""
    from booley.dev_support.criteria import (
        load_base_criteria,
        load_project_criteria,
        merge_criteria_defs,
    )
    from booley.mcp.registry import discover_mcp_tools

    base = load_base_criteria()
    project = []
    project_tools = None
    mcp_config: dict[str, Any] = {}
    flow_config: dict[str, Any] = {}
    if project_root is not None:
        project_dir = Path(project_root) / ".booley_project"
        project = load_project_criteria(project_dir / "criteria.toml")
        project_tools = project_dir / "mcp_tools"
        mcp_config, flow_config = _read_endpoint_config(project_dir / "booley.toml")
    merged, _errors = merge_criteria_defs(base, project)
    endpoints = discover_mcp_tools(
        project_mcp_tools_dir=project_tools,
        mcp_tool_config=mcp_config,
        flow_config=flow_config,
    )
    satisfying = {family for endpoint in endpoints for family in endpoint.satisfies}
    return {definition.name for definition in merged}, satisfying


def _criterion_family(key: str, definitions: set[str]) -> str | None:
    """Map a raw ticket key to its registered family, including review verdicts."""
    if key in definitions:
        return key
    for name in definitions:
        if key in {f"{name}_done", f"{name}_clean"}:
            return name
    return None


def _read_endpoint_config(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read endpoint enablement for live-registry ticket validation."""
    import tomllib

    if not path.is_file():
        return {}, {}
    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError):
        return {}, {}
    mcp_tools = data.get("mcp_tools", {})
    flows = data.get("flows", {})
    return (
        mcp_tools if isinstance(mcp_tools, dict) else {},
        flows if isinstance(flows, dict) else {},
    )


def _validate_criteria_params(criteria: dict[str, Any]) -> list[str]:
    """Validate params of registry-known parameterized criteria (synthesis_ok,
    fpga_impl_ok). Mirrors the harness's ``_validate_criterion_params`` so bad
    params are caught at authoring/enqueue, not at run time."""
    from booley.dev_support.criteria import (
        _CRITERION_PARAM_REGISTRY,
        _validate_criterion_params,
    )

    errors: list[str] = []
    for section_name in ("mandatory", "optional"):
        section = criteria.get(section_name, {})
        if not isinstance(section, dict):
            continue
        for key, value in section.items():
            if not isinstance(value, dict) or key not in _CRITERION_PARAM_REGISTRY:
                continue
            # `targets` is the per-target scoping key, not a metric param.
            params = {k: v for k, v in value.items() if k != "targets"}
            try:
                _validate_criterion_params(key, params)
            except ValueError as exc:
                errors.append(f"criteria.{section_name}.{key}: {exc}")
    return errors


_STATE_WORDS = frozenset({"pass", "fail", "none"})


def _validate_sim_entries(criteria: dict[str, Any]) -> list[str]:
    """Validate structured sim criterion entries parse correctly."""
    errors: list[str] = []
    from booley.dev_support.criteria import parse_sim_criterion

    for section_name in ("mandatory", "optional"):
        section = criteria.get(section_name, {})
        if not isinstance(section, dict):
            continue
        for key, value in section.items():
            if not isinstance(value, list):
                continue
            for item in value:
                if isinstance(item, str) and "->" in item:
                    try:
                        sc = parse_sim_criterion(item)
                    except ValueError as exc:
                        errors.append(f"criteria.{section_name}.{key}: {exc}")
                        continue
                    if sc.test_name and sc.test_name in _STATE_WORDS:
                        errors.append(
                            f"criteria.{section_name}.{key}: test_name "
                            f"'{sc.test_name}' looks like a state word, not a "
                            f"test filter — use 3-segment format "
                            f"'tb @ config @ current -> expected' instead: "
                            f"{item!r}"
                        )
    return errors


def _eligible_sim_target_selectors(declarations: dict[str, list[Any]]) -> list[str]:
    """Return copy-pasteable selectors for Targets the sim Booley Flow can drive."""
    from booley.fusesoc import fusesoc_registry
    from booley.targets.target import flow_can_drive

    selectors: list[str] = []
    for bucket in declarations.values():
        for ref in bucket:
            if flow_can_drive("sim", ref):
                selectors.append(fusesoc_registry.minimal_selector(ref, bucket))
    return sorted(selectors)


_TARGET_CREATION_VERBS = re.compile(
    r"\b(?:add|author|create|define|extend|implement|introduce)\w*\b",
    re.IGNORECASE,
)


def _scope_paths(fields: dict[str, Any]) -> list[str]:
    """Return normalized, project-relative paths from a ticket's scope."""
    scope = fields.get("scope")
    if not isinstance(scope, list):
        return []
    paths: list[str] = []
    for entry in scope:
        if not isinstance(entry, str):
            continue
        path = entry.removesuffix(" [new]").replace("\\", "/").strip()
        while path.startswith("./"):
            path = path[2:]
        if path:
            paths.append(path)
    return paths


def _scope_contains_path(fields: dict[str, Any], candidate: str) -> bool:
    """Return whether scope explicitly covers *candidate*."""
    path = candidate.replace("\\", "/").removeprefix("./")
    for scoped in _scope_paths(fields):
        if scoped == path or path.startswith(scoped.rstrip("/") + "/"):
            return True
        if any(char in scoped for char in "*?[") and fnmatch.fnmatchcase(path, scoped):
            return True
    return False


def _ticket_declares_future_target(fields: dict[str, Any], body: str, target: str) -> bool:
    """Return whether a ticket meets the documented future-Target contract."""
    if not any(Path(path).suffix.casefold() == ".core" for path in _scope_paths(fields)):
        return False

    bare_target = target.rpartition("#")[2]
    names = {target, bare_target}
    paragraphs = re.split(r"\n\s*\n", body)
    return any(
        _TARGET_CREATION_VERBS.search(paragraph)
        and any(
            re.search(
                rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])",
                paragraph,
                re.IGNORECASE,
            )
            for name in names
        )
        for paragraph in paragraphs
    )


def _validate_sim_targets(
    criteria: dict[str, Any],
    fields: dict[str, Any],
    body: str,
    project_root: str | Path,
) -> list[str]:
    """Reject structured ``sim_pass`` entries aimed at non-simulation Targets."""
    from booley.dev_support.criteria import parse_sim_criterion
    from booley.fusesoc import fusesoc_registry
    from booley.targets.target import flow_can_drive, select_target

    root = Path(project_root)
    try:
        declarations = fusesoc_registry.target_declarations(root)
    except fusesoc_registry.FuseSocError as exc:
        return [f"criteria: cannot inspect simulation Targets: {exc}"]
    if not declarations:
        return []  # No authored .core surface yet; preserve pre-migration validation.

    eligible = _eligible_sim_target_selectors(declarations)
    eligible_hint = ", ".join(eligible) if eligible else "none"
    errors: list[str] = []
    for section_name in ("mandatory", "optional"):
        section = criteria.get(section_name, {})
        if not isinstance(section, dict):
            continue
        value = section.get("sim_pass")
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, str) or "->" not in item:
                continue
            try:
                target = parse_sim_criterion(item).target
                ref = select_target(root, target)
            except ValueError:
                continue  # _validate_sim_entries owns malformed-entry errors.
            except fusesoc_registry.UnknownTargetError as exc:
                if _ticket_declares_future_target(fields, body, target):
                    continue
                errors.append(
                    f"criteria.{section_name}.sim_pass: target {target!r}: {exc}; "
                    f"eligible simulation Targets: {eligible_hint}"
                )
                continue
            except fusesoc_registry.FuseSocError as exc:
                errors.append(
                    f"criteria.{section_name}.sim_pass: target {target!r}: {exc}; "
                    f"eligible simulation Targets: {eligible_hint}"
                )
                continue
            if not flow_can_drive("sim", ref):
                errors.append(
                    f"criteria.{section_name}.sim_pass: target {target!r} cannot satisfy "
                    f"sim_pass (flow={ref.flow!r}, EDA tool={ref.eda_tool!r}); eligible simulation "
                    f"Targets: {eligible_hint}"
                )
    return errors


def _validate_retired_criteria(criteria: dict[str, Any]) -> list[str]:
    """Reject retired criterion keys with the harness's own rename hint.

    Diffs the ticket's declared keys against the shared retired-key registry so
    ``validate-ticket`` fails a stale ticket at triage time with the exact,
    actionable message the harness raises at intake — instead of that error only
    surfacing (and, historically, being mislabeled as a SIGINT crash) mid-run.
    """
    from booley.dev_support.criteria import find_retired_criteria

    keys: list[str] = []
    for section_name in ("mandatory", "optional"):
        section = criteria.get(section_name, {})
        if isinstance(section, dict):
            keys.extend(section.keys())

    return [f"criteria: retired key '{key}' — {hint}" for key, hint in find_retired_criteria(keys)]


def _validate_flow_coherence(criteria: dict[str, Any], project_root: str | Path) -> list[str]:
    """Check criterion prefixes against disabled Flows in booley.toml."""
    errors: list[str] = []
    from .execution import disabled_flows

    disabled = disabled_flows(Path(project_root))
    if not disabled:
        return errors
    for section_name in ("mandatory", "optional"):
        section = criteria.get(section_name, {})
        if not isinstance(section, dict):
            continue
        for crit_key in section:
            for prefix, flow in CRITERION_FLOW_MAP.items():
                if crit_key.startswith(prefix) and flow in disabled:
                    errors.append(
                        f"criteria.{section_name}.{crit_key} requires "
                        f"Flow {flow} which is disabled in booley.toml"
                    )
    return errors


def _validate_tb_paths(
    criteria: dict[str, Any],
    fields: dict[str, Any],
    body: str,
    project_root: str | Path,
) -> list[str]:
    """Validate TB filenames resolve against configured tb_source_prefixes."""
    errors: list[str] = []
    from booley.dev_support.criteria import extract_tb_paths as _extract_tb

    tb_paths = _extract_tb(criteria)
    if not tb_paths:
        return errors

    from .execution import tb_source_prefixes

    prefixes = tb_source_prefixes(Path(project_root))
    has_valid_tb = any(any(p.startswith(pfx) for pfx in prefixes) for p in tb_paths)
    if not has_valid_tb:
        future_entries = _future_sim_entries(criteria, fields, body)
        if future_entries and all(
            _scope_contains_path(fields, tb_path) for tb_path, _target in future_entries
        ):
            return errors
        dirs = ", ".join(pfx.rstrip("/") for pfx in prefixes)
        errors.append(
            f"criteria TB paths must start with a configured testbench source_dir ({dirs})"
        )
    return errors


def _future_sim_entries(
    criteria: dict[str, Any], fields: dict[str, Any], body: str
) -> list[tuple[str, str]]:
    """Return structured sim entries whose Targets the ticket declares it will create."""
    from booley.dev_support.criteria import parse_sim_criterion

    entries: list[tuple[str, str]] = []
    for section_name in ("mandatory", "optional"):
        section = criteria.get(section_name, {})
        if not isinstance(section, dict):
            continue
        value = section.get("sim_pass")
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, str) or "->" not in item:
                continue
            try:
                parsed = parse_sim_criterion(item)
            except ValueError:
                return []  # The structured-entry validator owns this error.
            if not _ticket_declares_future_target(fields, body, parsed.target):
                return []
            entries.append((parsed.tb, parsed.target))
    return entries


def _source_prefixes(project_root: Path, section_name: str, default: str) -> list[str]:
    """Return source prefixes for scope validation, from the ``.core`` filesets.

    *section_name* is the legacy ``[sources.*]`` table name (``"rtl"`` /
    ``"testbench"``); it selects the corresponding ``.core`` partition (ADR 0026
    follow-through).
    """
    try:
        from booley.fusesoc.fusesoc_registry import source_dirs_from_core

        rtl_dirs, tb_dirs, _incl = source_dirs_from_core(project_root)
    except Exception:  # noqa: BLE001 — registry unavailable; default prefix
        return [default.rstrip("/") + "/"]
    dirs = tb_dirs if section_name == "testbench" else rtl_dirs
    if not dirs:
        return [default.rstrip("/") + "/"]
    from booley.runtime.shared_infra import source_dir_prefixes

    return [prefix for prefix in source_dir_prefixes(dirs, project_root) if "\\" not in prefix]


def _scope_hits_prefix(scope: list[str], prefixes: list[str]) -> bool:
    """True when a scope entry targets one of the configured source dirs."""
    from booley.runtime.shared_infra import source_path_matches

    for entry in scope:
        path = entry.removesuffix(" [new]")
        if source_path_matches(path, prefixes):
            return True
    return False


def _has_mandatory_sim_criterion(criteria: Any, ticket_type: str) -> bool:
    """Return True when criteria contains a mandatory sim_* check."""
    from booley.dev_support.criteria import CriteriaTemplate

    if criteria is None:
        template = CriteriaTemplate.for_ticket_type(ticket_type)
        return any(spec.mandatory and spec.name.startswith("sim_") for spec in template.specs)
    if not isinstance(criteria, dict):
        return False
    try:
        expanded = CriteriaTemplate.from_yaml(criteria).expand(["default"])
    except (TypeError, ValueError):
        mandatory = criteria.get("mandatory", {})
        return isinstance(mandatory, dict) and any(
            isinstance(key, str) and key.startswith("sim_") for key in mandatory
        )
    return any(
        key.startswith(("sim_", "cycle_count_")) and mandatory
        for key, mandatory in expanded.items()
    )


def _has_existing_testbench(project_root: Path, tb_prefixes: list[str]) -> bool:
    """Return True when configured TB dirs already contain Verilog sources."""
    exts = {".v", ".vh", ".sv", ".svh"}
    for prefix in tb_prefixes:
        tb_dir = project_root / prefix.rstrip("/\\")
        if tb_dir.is_file() and tb_dir.suffix.lower() in exts:
            return True
        if not tb_dir.is_dir():
            continue
        if any(path.is_file() and path.suffix.lower() in exts for path in tb_dir.rglob("*")):
            return True
    return False


def _validate_sim_shape_for_rtl_tb_scope(
    fields: dict[str, Any],
    scope: list[str],
    project_root: str | Path | None,
    *,
    check_files: bool,
) -> list[str]:
    """Require simulation criteria and TB availability for RTL/TB edits."""
    if not project_root or not scope:
        return []

    root = Path(project_root)
    rtl_prefixes = _source_prefixes(root, "rtl", "rtl")
    tb_prefixes = _source_prefixes(root, "testbench", "tb")
    unknown_scope = scope == ["*"]
    touches_rtl = unknown_scope or _scope_hits_prefix(scope, rtl_prefixes)
    touches_tb = unknown_scope or _scope_hits_prefix(scope, tb_prefixes)
    if not (touches_rtl or touches_tb):
        return []

    errors: list[str] = []
    criteria = fields.get("criteria")
    if not _has_mandatory_sim_criterion(criteria, str(fields.get("type", ""))):
        errors.append("RTL/TB-editing tickets must include at least one mandatory sim_* criterion")

    tb_allowed_by_scope = touches_tb
    if check_files and not tb_allowed_by_scope and not _has_existing_testbench(root, tb_prefixes):
        errors.append(
            "RTL/TB-editing tickets need an existing testbench, or scope must "
            "allow adding/modifying a testbench file"
        )
    return errors


def _check_branch_exists(branch, git_cwd):
    """Verify a git branch exists. Returns error string or None."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", branch],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=git_cwd,
            check=False,
        )
        if result.returncode != 0:
            return f"Branch '{branch}' does not exist"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "Could not verify branch (git not available)"
    return None


def owned_draft_dirty_paths(ticket_path: str | Path, tickets_dir: str | Path) -> tuple[Path, ...]:
    """Return the exact dirty path exemption for a canonical draft Ticket."""
    candidate = Path(ticket_path).resolve()
    drafts_dir = (Path(tickets_dir) / "board" / "drafts").resolve()
    if candidate.parent != drafts_dir or candidate.suffix.casefold() != ".md":
        return ()
    return (candidate,)


def _git_relative_paths(paths: Iterable[str | Path], git_cwd: str | Path | None) -> set[str]:
    """Normalize caller-owned paths to Git's repository-relative spelling."""
    root = Path(git_cwd).resolve() if git_cwd is not None else None
    normalized: set[str] = set()
    for raw in paths:
        path = Path(raw)
        if root is not None:
            try:
                path = path.resolve().relative_to(root)
            except (OSError, ValueError):
                continue
        elif path.is_absolute():
            continue
        value = path.as_posix().removeprefix("./")
        if value:
            normalized.add(value)
    return normalized


def _check_clean_worktree(git_cwd, allowed_dirty_paths: Iterable[str | Path] = ()):
    """Check for product dirt, excluding explicitly owned control artifacts."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "-z", "--untracked-files=all"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=git_cwd,
            check=False,
        )
        if result.returncode == 0:
            allowed = _git_relative_paths(allowed_dirty_paths, git_cwd)
            dirty = [
                entry for entry in parse_porcelain_v1_z(result.stdout) if entry.path not in allowed
            ]
            if dirty:
                return f"Dirty working tree ({len(dirty)} modified files)"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def _check_no_conflict_state(git_cwd):
    """Check for in-progress merge, rebase, or cherry-pick. Returns error list."""
    errors = []
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=git_cwd,
            check=False,
        )
        if r.returncode == 0:
            git_dir = Path(r.stdout.strip())
            for state, marker in {
                "merge": git_dir / "MERGE_HEAD",
                "rebase": git_dir / "rebase-merge",
                "rebase-apply": git_dir / "rebase-apply",
                "cherry-pick": git_dir / "CHERRY_PICK_HEAD",
            }.items():
                if marker.exists():
                    errors.append(f"Git {state} in progress — resolve before executing tickets")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return errors


def _validate_git_state(
    fields: dict[str, Any],
    project_root: str | Path | None,
    allowed_dirty_paths: Iterable[str | Path] = (),
) -> list[str]:
    """Validate git branch existence, clean worktree, and no in-progress operations."""
    errors: list[str] = []
    git_cwd = str(project_root) if project_root else None

    branch = fields.get("branch", "")
    if branch:
        err = _check_branch_exists(branch, git_cwd)
        if err:
            errors.append(err)

    err = _check_clean_worktree(git_cwd, allowed_dirty_paths)
    if err:
        errors.append(err)

    errors.extend(_check_no_conflict_state(git_cwd))
    return errors


def validate_ticket_fields(
    fields: dict[str, Any],
    body: str,
    check_files: bool = False,
    check_git: bool = False,
    project_root: str | Path | None = None,
    check_tb_files: bool = True,
    allowed_dirty_paths: Iterable[str | Path] = (),
) -> list[str]:
    """Validate ticket fields. Returns list of error strings (empty = valid).

    Strings prefixed with ``[warning] `` are non-blocking warnings.
    """
    errors: list[str] = []
    ticket_type = fields.get("type", "")

    errors.extend(_validate_basic_fields(fields, body))
    errors.extend(_validate_on_success(fields.get("on_success")))
    from .target_contract import validate_contract_fields

    # Generic Ticket Board validation may run from the destination branch,
    # while the sealed Targets exist only in the ticket's contract worktree.
    # Direction is resolved there by intake and every Flow gate.
    errors.extend(validate_contract_fields(fields))

    scope_errors, _scope = _validate_scope(fields, check_files, project_root)
    errors.extend(scope_errors)
    errors.extend(
        _validate_sim_shape_for_rtl_tb_scope(
            fields,
            _scope,
            project_root,
            check_files=check_files,
        )
    )

    criteria_errors, criteria_warnings = _validate_criteria(
        fields,
        body,
        ticket_type,
        check_tb_files,
        project_root,
    )
    errors.extend(criteria_errors)
    errors.extend(criteria_warnings)

    if check_git:
        errors.extend(_validate_git_state(fields, project_root, allowed_dirty_paths))

    return errors
