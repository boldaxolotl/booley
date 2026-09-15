"""Edit a converted Ticket's authored Criteria for a human amendment."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from booley.core.boundary import require_dict, require_str

from .amendment_proposal import (
    AmendmentProposal,
    AmendmentProposalError,
    CriterionChange,
    _add_scope,
    _relaxation_direction,
    _threshold_number,
)
from .ticket_document import TicketAuthoringView, TicketCriterion, TicketSpec


def build_v2_amendment_proposal(
    spec: TicketSpec, request: Any, root: Path, view: TicketAuthoringView
) -> AmendmentProposal:
    """Apply only monotone, instance-addressed edits to v2 authoring fields."""
    try:
        actor, reason, feedback, edits, additions = _request_fields(request)
        revised = deepcopy(dict(spec.fields))
        seen: set[str] = set()
        by_identity = {row.identity: row for row in spec.criteria}
        changes = [_apply_criterion_edit(revised, raw, by_identity, seen, view) for raw in edits]
        scope_added = _add_scope(revised, additions, root)
        if not changes and not scope_added:
            raise AmendmentProposalError("no acceptance change; use ordinary unblock for feedback")
        return AmendmentProposal(
            revised, actor, reason, feedback, tuple(changes), tuple(scope_added)
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, AmendmentProposalError):
            raise
        raise AmendmentProposalError(str(exc)) from exc


def _request_fields(request: Any) -> tuple[str, str, str, list[Any], list[Any]]:
    data = require_dict(request, field="amendment")
    if set(data) - {"actor", "reason", "feedback", "criteria", "scope_add"}:
        raise AmendmentProposalError("amendment has unknown fields")
    actor = require_str(data, "actor").strip()
    reason = require_str(data, "reason").strip()
    feedback = data.get("feedback", "")
    if not actor or len(actor) > 200 or any(char in actor for char in "\r\n"):
        raise AmendmentProposalError("amendment actor must name one Human on one line")
    if not reason:
        raise AmendmentProposalError("amendment reason must be nonblank")
    if not isinstance(feedback, str):
        raise AmendmentProposalError("feedback must be a string")
    edits = data.get("criteria", [])
    additions = data.get("scope_add", [])
    if not isinstance(edits, list) or not isinstance(additions, list):
        raise AmendmentProposalError("criteria and scope_add must be lists")
    return actor, reason, feedback, edits, additions


def _apply_criterion_edit(
    revised: dict[str, Any],
    raw: Any,
    by_identity: dict[str, TicketCriterion],
    seen: set[str],
    view: TicketAuthoringView,
) -> CriterionChange:
    edit = require_dict(raw, field="criterion edit")
    if set(edit) - {"criterion", "thresholds", "make_optional"}:
        raise AmendmentProposalError("criterion edit has unknown fields")
    name = require_str(edit, "criterion").strip()
    if not name or name in seen:
        raise AmendmentProposalError("criterion edits require unique, nonblank instances")
    seen.add(name)
    row = by_identity.get(name)
    if row is None:
        raise AmendmentProposalError(f"unknown Criterion instance {name!r}")
    make_optional = edit.get("make_optional", False)
    thresholds = edit.get("thresholds", {})
    if not isinstance(make_optional, bool):
        raise AmendmentProposalError("make_optional must be boolean")
    if not isinstance(thresholds, dict):
        raise AmendmentProposalError("thresholds must be a mapping")
    if make_optional and not row.mandatory:
        raise AmendmentProposalError(f"{name!r} is already optional")
    if not make_optional and not thresholds:
        raise AmendmentProposalError(f"{name!r} has no change")
    leaf, parent, key = _locate(revised, row, view)
    deltas = _relax_thresholds(leaf, row, name, thresholds)
    if make_optional:
        _move_optional(revised, row, parent, key, leaf, view)
    return CriterionChange(name, row.mandatory, row.mandatory and not make_optional, deltas)


def _relax_thresholds(
    leaf: Any, row: TicketCriterion, name: str, thresholds: dict[str, Any]
) -> dict[str, tuple[Any, Any]]:
    deltas: dict[str, tuple[Any, Any]] = {}
    for param, new in thresholds.items():
        old, family = _threshold(leaf, row, param)
        direction = _relaxation_direction(family, param)
        before = _threshold_number(family, param, old)
        after = _threshold_number(family, param, new)
        if (direction == "lower" and after >= before) or (
            direction == "higher" and after <= before
        ):
            raise AmendmentProposalError(f"{name}.{param} must relax")
        _set_threshold(leaf, row, param, new)
        deltas[param] = (old, new)
    return deltas


def _target_key(entries: dict[str, Any], row: TicketCriterion, view: TicketAuthoringView) -> str:
    for authored in entries:
        selector = authored.split(" (", 1)[0]
        try:
            if view.resolve_target(selector, None) == row.target:
                return authored
        except ValueError:
            continue
    raise AmendmentProposalError(f"Target for {row.identity!r} is unavailable")


def _locate(
    fields: dict[str, Any], row: TicketCriterion, view: TicketAuthoringView
) -> tuple[Any, dict[str, Any], str]:
    section = fields["CRITERIA_MANDATORY" if row.mandatory else "CRITERIA_OPTIONAL"]
    if row.capability not in section:
        raise AmendmentProposalError(f"Criterion {row.identity!r} is unavailable")
    if row.capability not in {
        "LINT",
        "ELAB",
        "ELAB_STANDALONE",
        "SIM",
        "CYCLE_COUNT",
        "SYNTH",
        "FPGA",
        "REVIEW",
        "MUTATION",
        "COVERAGE",
    }:
        return section[row.capability], section, row.capability
    declaration = section[row.capability]
    if row.capability == "ELAB_STANDALONE":
        return declaration, section, row.capability
    if row.capability == "REVIEW":
        parent = declaration[row.target]
        return parent[row.test], parent, row.test
    target = _target_key(declaration, row, view)
    if row.capability in {"SIM", "CYCLE_COUNT"}:
        parent = declaration[target]
        return parent[row.test], parent, row.test
    return declaration[target], declaration, target


def _threshold(leaf: Any, row: TicketCriterion, param: str) -> tuple[Any, str]:
    family = {
        "SYNTH": "synthesis_ok",
        "FPGA": "fpga_impl_ok",
        "CYCLE_COUNT": "cycle_count",
        "COVERAGE": "coverage",
        "MUTATION": "mutation_score",
    }.get(row.capability)
    if family is None or not isinstance(leaf, dict):
        raise AmendmentProposalError(f"{row.identity!r} has no relaxable threshold")
    if row.capability == "COVERAGE":
        if param != f"{row.parameter}.min_pct":
            raise AmendmentProposalError(f"{row.identity!r} only permits {row.parameter}.min_pct")
        return leaf["metrics"][row.parameter]["min_pct"], family
    if row.capability == "MUTATION":
        if param != "min_detected":
            raise AmendmentProposalError("mutation_score only supports min_detected relaxation")
        return leaf[param], family
    if param != row.parameter:
        raise AmendmentProposalError(f"{row.identity!r} only permits {row.parameter}")
    return leaf[param], family


def _set_threshold(leaf: dict[str, Any], row: TicketCriterion, param: str, value: Any) -> None:
    if row.capability == "COVERAGE":
        leaf["metrics"][row.parameter]["min_pct"] = value
    else:
        leaf[param] = value


def _move_optional(
    fields: dict[str, Any],
    row: TicketCriterion,
    parent: dict[str, Any],
    key: str,
    leaf: Any,
    view: TicketAuthoringView,
) -> None:
    mandatory = fields["CRITERIA_MANDATORY"]
    optional = fields.setdefault("CRITERIA_OPTIONAL", {})
    capability = row.capability
    if capability == "REVIEW":
        _move_review_optional(mandatory, optional, row, parent, key, leaf)
    elif capability in {"SYNTH", "FPGA", "CYCLE_COUNT", "COVERAGE", "SIM"}:
        _move_flow_optional(mandatory, optional, row, view)
    elif capability in {"LINT", "ELAB", "MUTATION"}:
        destination = optional.setdefault(capability, {})
        destination[key] = parent.pop(key)
        if not parent:
            del mandatory[capability]
    else:
        optional[capability] = mandatory.pop(capability)


def _move_review_optional(
    mandatory: dict[str, Any],
    optional: dict[str, Any],
    row: TicketCriterion,
    parent: dict[str, Any],
    key: str,
    leaf: Any,
) -> None:
    values = [leaf] if isinstance(leaf, str) else list(leaf)
    values.remove(row.parameter)
    if values:
        parent[key] = values[0] if len(values) == 1 else values
    else:
        del parent[key]
        if not parent:
            del mandatory["REVIEW"][row.target]
        if not mandatory["REVIEW"]:
            del mandatory["REVIEW"]
    destination = optional.setdefault("REVIEW", {}).setdefault(row.target, {})
    prior = destination.get(key)
    outcomes = [] if prior is None else [prior] if isinstance(prior, str) else list(prior)
    outcomes.append(row.parameter)
    destination[key] = outcomes[0] if len(outcomes) == 1 else outcomes


def _move_flow_optional(
    mandatory: dict[str, Any],
    optional: dict[str, Any],
    row: TicketCriterion,
    view: TicketAuthoringView,
) -> None:
    capability = row.capability
    declaration = mandatory[capability]
    target = _target_key(declaration, row, view)
    source = declaration[target]
    destination = optional.setdefault(capability, {}).setdefault(target, {})
    if capability in {"SYNTH", "FPGA"} and source == "pass":
        optional[capability][target] = declaration.pop(target)
    elif capability == "COVERAGE":
        _move_coverage_threshold(declaration, target, source, destination, row)
    elif capability in {"SYNTH", "FPGA"}:
        _move_implementation_threshold(optional, declaration, target, source, destination, row)
    elif capability == "CYCLE_COUNT":
        _move_cycle_threshold(declaration, target, source, destination, row)
    else:
        destination[row.test] = source.pop(row.test)
        if not source:
            del declaration[target]
    if not declaration:
        del mandatory[capability]


def _move_coverage_threshold(
    declaration: dict[str, Any],
    target: str,
    source: dict[str, Any],
    destination: dict[str, Any],
    row: TicketCriterion,
) -> None:
    destination["tests"] = source["tests"]
    destination.setdefault("metrics", {})[row.parameter] = source["metrics"].pop(row.parameter)
    if not source["metrics"]:
        del declaration[target]


def _move_implementation_threshold(
    optional: dict[str, Any],
    declaration: dict[str, Any],
    target: str,
    source: dict[str, Any],
    destination: Any,
    row: TicketCriterion,
) -> None:
    if destination == "pass":
        destination = {}
        optional[row.capability][target] = destination
    if "baseline" in source:
        destination["baseline"] = source["baseline"]
    destination[row.parameter] = source.pop(row.parameter)
    if set(source) <= {"baseline"}:
        del declaration[target]


def _move_cycle_threshold(
    declaration: dict[str, Any],
    target: str,
    source: dict[str, Any],
    destination: dict[str, Any],
    row: TicketCriterion,
) -> None:
    policy = source[row.test]
    test_dest = destination.setdefault(row.test, {})
    if "baseline" in policy:
        test_dest["baseline"] = policy["baseline"]
    test_dest[row.parameter] = policy.pop(row.parameter)
    if set(policy) <= {"baseline"}:
        del source[row.test]
    if not source:
        del declaration[target]
