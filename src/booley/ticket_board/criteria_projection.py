"""Project converted Ticket Criteria onto the Flow recording state.

This module consumes typed, resolved Criteria only. Authored YAML is decoded
exclusively by :func:`booley.ticket_board.ticket_document.convert_ticket_document`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from booley.criteria.templates import BASELINE_TARGET_PARAM
from booley.targets.domain import TARGET_IDENTITY_PARAM

if TYPE_CHECKING:
    from .ticket_document import TicketCriterion, TicketSpec


_FLOW_FAMILIES = {
    "LINT": "lint_clean",
    "ELAB": "elab_pass",
    "ELAB_STANDALONE": "elaborate_standalone",
    "SIM": "sim_pass",
    "CYCLE_COUNT": "cycle_count",
    "SYNTH": "synthesis_ok",
    "FPGA": "fpga_impl_ok",
    "MUTATION": "mutation_score",
    "COVERAGE": "coverage",
}


@dataclass(frozen=True)
class TicketCriteriaProjection:
    """Atomic state declaration and Flow result aliases for one TicketSpec."""

    required: dict[str, bool]
    params: dict[str, dict[str, Any]]
    aliases: dict[str, list[str]]
    categories: dict[str, str]


def _target_name(identity: str) -> str:
    return identity.rsplit("#", maxsplit=1)[-1]


def _params(row: TicketCriterion) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if row.target is not None:
        params["target"] = row.target
        params[TARGET_IDENTITY_PARAM] = row.target
    if row.capability == "SIM":
        params["test_selector"] = row.test
        if row.test != "all":
            params["required_tests"] = [row.test]
            params["minimum_total"] = 1
        if row.value == "fail -> pass":
            params["from_state"] = "fail"
    elif row.capability == "CYCLE_COUNT":
        assert isinstance(row.value, dict)
        params["test"] = row.test
        params["test_selector"] = row.test
        params["required_tests"] = [row.test]
        params["minimum_total"] = 1
        params[row.parameter] = row.value["threshold"]
        if row.value["baseline"] is not None:
            params[BASELINE_TARGET_PARAM] = row.value["baseline"]
    elif row.capability in {"SYNTH", "FPGA"} and row.parameter != "run":
        assert isinstance(row.value, dict)
        params[row.parameter] = row.value["threshold"]
        if row.value["baseline"] is not None:
            params[BASELINE_TARGET_PARAM] = row.value["baseline"]
    elif row.capability == "MUTATION":
        params.update(row.value)
    elif row.capability == "COVERAGE":
        assert isinstance(row.value, dict)
        params["tests"] = row.value["tests"]
        params["metrics"] = {row.parameter: {"min_pct": row.value["min_pct"]}}
    elif row.capability == "ELAB_STANDALONE":
        params["targets"] = list(row.value)
    return params


def project_ticket_criteria(spec: TicketSpec) -> TicketCriteriaProjection:
    """Build state declarations solely from the converter's atomic model."""
    required: dict[str, bool] = {}
    params: dict[str, dict[str, Any]] = {}
    aliases: dict[str, list[str]] = {}
    categories: dict[str, str] = {}
    for row in spec.criteria:
        required[row.identity] = row.mandatory
        params[row.identity] = _params(row)
        if row.capability == "REVIEW":
            categories[row.identity] = "rtl" if row.target == "rtl" else "tb"
            continue
        if row.capability not in _FLOW_FAMILIES:
            continue
        family = _FLOW_FAMILIES[row.capability]
        if row.capability == "ELAB_STANDALONE":
            continue
        if row.target is not None:
            key = f"{family}_{_target_name(row.target)}"
            aliases.setdefault(key, []).append(row.identity)
    return TicketCriteriaProjection(required, params, aliases, categories)
