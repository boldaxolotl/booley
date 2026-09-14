"""Shared amendment request vocabulary and monotone threshold rules.

The human Ticket is parsed only by ticket_document. These helpers operate on
already-converted Criterion identities and values.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from booley.core.boundary import BoundaryError, require_finite_number
from booley.criteria.templates import (
    CYCLE_COUNT_PARAMS,
    FPGA_IMPL_OK_PARAMS,
    SYNTHESIS_OK_PARAMS,
    _parse_percentage,
)
from booley.criteria.thresholds import describe_threshold

from .acceptance_basis import PATH_POLICY
from .acceptance_path_policy import is_static_acceptance_path


class AmendmentProposalError(ValueError):
    """The requested amendment is invalid or not demonstrably monotone."""


@dataclass(frozen=True)
class CriterionChange:
    """One exact, expanded Criterion instance change."""

    criterion: str
    before_mandatory: bool
    after_mandatory: bool
    thresholds: dict[str, tuple[Any, Any]]


@dataclass(frozen=True)
class AmendmentProposal:
    """Validated authored fields and the exact human-visible delta."""

    fields: dict[str, Any]
    actor: str
    reason: str
    feedback: str
    changes: tuple[CriterionChange, ...]
    scope_added: tuple[str, ...]


_QOR_PARAMS = {"synthesis_ok": SYNTHESIS_OK_PARAMS, "fpga_impl_ok": FPGA_IMPL_OK_PARAMS}


def _relaxation_direction(key: str, param: str) -> str:
    if key == "coverage":
        return "lower"
    if key == "mutation_score":
        if param == "min_detected":
            return "lower"
        raise AmendmentProposalError("mutation_score only supports min_detected relaxation")
    allowed = CYCLE_COUNT_PARAMS if key == "cycle_count" else _QOR_PARAMS.get(key)
    base = param.rpartition(".")[2]
    if allowed is None or base not in allowed:
        raise AmendmentProposalError(f"{key}.{param} has no declared relaxation support")
    if "." in param and base not in {
        "critical_path_ps_max",
        "fmax_mhz_min",
        "critical_path_ps_increase_at_most",
        "fmax_mhz_increase_at_most",
        "critical_path_ps_reduce_at_least",
        "fmax_mhz_reduce_at_least",
    }:
        raise AmendmentProposalError(f"{key}.{param} is not a supported per-clock threshold")
    descriptor = describe_threshold(base)
    if descriptor is None:
        raise AmendmentProposalError(f"{key}.{param} has no declared relaxation support")
    negative_bound = "_reduce_" in base
    lower = (descriptor.operator == "ge") != negative_bound
    return "lower" if lower else "higher"


def _threshold_number(key: str, param: str, value: Any) -> int | float:
    percentage = param.endswith(
        ("_increase_at_most", "_increase_at_least", "_reduce_at_most", "_reduce_at_least")
    )
    if percentage and not param.endswith("_cycles"):
        return _parse_percentage(value, field=f"{key}.{param}")
    try:
        return require_finite_number(value, field=f"{key}.{param}")
    except BoundaryError as exc:
        raise AmendmentProposalError(str(exc)) from exc


def _add_scope(fields: dict[str, Any], additions: list[Any], root: Path) -> list[str]:
    current = fields.get("scope", [])
    if not isinstance(current, list):
        raise AmendmentProposalError("published Scope must be a list")
    if not additions:
        return []
    protected = set(PATH_POLICY.discover(root))
    added: list[str] = []
    for raw in additions:
        if not isinstance(raw, str):
            raise AmendmentProposalError("Scope additions must be strings")
        name = raw.removesuffix(" [new]")
        path = PurePosixPath(name)
        if (
            not name
            or name != path.as_posix()
            or path.is_absolute()
            or any(part in {".", ".."} for part in path.parts)
            or any(char in name for char in "*?[]\\")
            or is_static_acceptance_path(name)
        ):
            raise AmendmentProposalError(f"unsafe Scope addition {raw!r}")
        if (
            name.startswith((".git/", ".booley_project/tickets/", ".booley_project/acceptance/"))
            or name in protected
            or any(name.startswith(item.rstrip("/") + "/") for item in protected)
        ):
            raise AmendmentProposalError(f"protected Scope addition {raw!r}")
        candidate = root / name
        is_new = raw.endswith(" [new]")
        if (is_new and candidate.exists()) or (not is_new and not candidate.is_file()):
            raise AmendmentProposalError(f"Scope existence disagrees with {raw!r}")
        if raw in current or raw in added:
            raise AmendmentProposalError(f"Scope addition {raw!r} already exists")
        added.append(raw)
    fields["scope"] = [*current, *added]
    return added
