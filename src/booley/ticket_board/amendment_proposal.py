"""Pure, fail-closed validation of human Ticket amendment requests."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

from booley.core.boundary import BoundaryError, require_dict, require_finite_number, require_str
from booley.criteria.templates import (
    CYCLE_COUNT_PARAMS,
    FPGA_IMPL_OK_PARAMS,
    SYNTHESIS_OK_PARAMS,
    CriteriaTemplate,
    _parse_percentage,
    _validate_criterion_params,
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


@dataclass(frozen=True)
class _Atom:
    section: str
    key: str
    kind: str
    value: Any
    instance: str


_QOR_PARAMS = {"synthesis_ok": SYNTHESIS_OK_PARAMS, "fpga_impl_ok": FPGA_IMPL_OK_PARAMS}


def build_amendment_proposal(
    fields: dict[str, Any], request: Any, project_root: Path
) -> AmendmentProposal:
    """Validate supported edits and return a complete, unpersisted proposal."""
    try:
        data = require_dict(request, field="amendment")
        if set(data) - {"actor", "reason", "feedback", "criteria", "scope_add"}:
            raise AmendmentProposalError("amendment has unknown fields")
        actor = require_str(data, "actor").strip()
        reason = require_str(data, "reason").strip()
        feedback = data.get("feedback", "")
        if not isinstance(feedback, str):
            raise AmendmentProposalError("feedback must be a string")
        if not actor or len(actor) > 200 or any(char in actor for char in "\r\n"):
            raise AmendmentProposalError("amendment actor must name one Human on one line")
        if not reason:
            raise AmendmentProposalError("amendment reason must be nonblank")
        raw_edits = data.get("criteria", [])
        raw_scope = data.get("scope_add", [])
        if not isinstance(raw_edits, list) or not isinstance(raw_scope, list):
            raise AmendmentProposalError("criteria and scope_add must be lists")
        revised = deepcopy(fields)
        atoms = _atoms(revised.get("criteria"))
        changes = _apply_edits(atoms, raw_edits)
        revised["criteria"] = _serialize_atoms(atoms)
        scope_added = _add_scope(revised, raw_scope, project_root)
        if not changes and not scope_added:
            raise AmendmentProposalError("no acceptance change; use ordinary unblock for feedback")
        CriteriaTemplate.from_yaml(revised["criteria"])
        return AmendmentProposal(
            revised, actor, reason, feedback, tuple(changes), tuple(scope_added)
        )
    except (BoundaryError, TypeError, KeyError, ValueError) as exc:
        if isinstance(exc, AmendmentProposalError):
            raise
        raise AmendmentProposalError(str(exc)) from exc


def _atoms(criteria: Any) -> list[_Atom]:
    if not isinstance(criteria, dict) or set(criteria) - {"mandatory", "optional"}:
        raise AmendmentProposalError("published Criteria are invalid")
    atoms: list[_Atom] = []
    for section in ("mandatory", "optional"):
        entries = criteria.get(section, {})
        if not isinstance(entries, dict):
            raise AmendmentProposalError(f"criteria.{section} must be a mapping")
        for key, value in entries.items():
            for kind, item in _split_entry(key, value):
                fragment = {section: {key: _entry_value(kind, item)}}
                instances = CriteriaTemplate.from_yaml(fragment).expand(["default"])
                if len(instances) != 1:
                    raise AmendmentProposalError(
                        f"criteria.{section}.{key} cannot be addressed as one instance"
                    )
                atoms.append(_Atom(section, key, kind, deepcopy(item), next(iter(instances))))
    identities = [atom.instance for atom in atoms]
    if len(set(identities)) != len(identities):
        raise AmendmentProposalError("published Criteria have duplicate instance identities")
    return atoms


def _split_entry(key: str, value: Any) -> list[tuple[str, Any]]:
    if key in {"coverage", "cycle_count"}:
        if not isinstance(value, list) or not value:
            raise AmendmentProposalError(f"{key} must be a nonempty list")
        if key == "cycle_count":
            return [("cycle", item) for item in value]
        return [
            ("coverage", {**item, "targets": [target]})
            for item in value
            for target in item["targets"]
        ]
    if isinstance(value, dict) and isinstance(value.get("targets"), list):
        return [("target_dict", {**value, "targets": [target]}) for target in value["targets"]]
    if isinstance(value, list):
        if not value:
            raise AmendmentProposalError(f"{key} must not be empty")
        if all(isinstance(item, str) and "@" not in item and "->" not in item for item in value):
            return [("target_list", item) for item in value]
        return [("list", item) for item in value]
    return [("single", value)]


def _entry_value(kind: str, value: Any) -> Any:
    return [value] if kind in {"cycle", "coverage", "target_list", "list"} else value


def _apply_edits(atoms: list[_Atom], edits: list[Any]) -> list[CriterionChange]:
    changes: list[CriterionChange] = []
    seen: set[str] = set()
    for raw in edits:
        edit = require_dict(raw, field="criterion edit")
        if set(edit) - {"criterion", "thresholds", "make_optional"}:
            raise AmendmentProposalError("criterion edit has unknown fields")
        name = require_str(edit, "criterion").strip()
        if not name or name in seen:
            raise AmendmentProposalError("criterion edits require unique, nonblank instances")
        seen.add(name)
        candidates = [(index, atom) for index, atom in enumerate(atoms) if atom.instance == name]
        if len(candidates) != 1:
            raise AmendmentProposalError(f"unknown or ambiguous Criterion instance {name!r}")
        index, atom = candidates[0]
        make_optional = edit.get("make_optional", False)
        if not isinstance(make_optional, bool):
            raise AmendmentProposalError("make_optional must be boolean")
        if make_optional and atom.section != "mandatory":
            raise AmendmentProposalError(f"{name!r} is already optional")
        raw_thresholds = edit.get("thresholds", {})
        if not isinstance(raw_thresholds, dict):
            raise AmendmentProposalError("thresholds must be a mapping")
        if not make_optional and not raw_thresholds:
            raise AmendmentProposalError(f"{name!r} has no change")
        updated, deltas = _relax_thresholds(atom, raw_thresholds)
        atoms[index] = replace(
            atom,
            section="optional" if make_optional else atom.section,
            value=updated,
        )
        changes.append(
            CriterionChange(
                name,
                atom.section == "mandatory",
                not make_optional and atom.section == "mandatory",
                deltas,
            )
        )
    return changes


def _relax_thresholds(
    atom: _Atom, edits: dict[str, Any]
) -> tuple[Any, dict[str, tuple[Any, Any]]]:
    value = deepcopy(atom.value)
    changes: dict[str, tuple[Any, Any]] = {}
    for param, new in edits.items():
        if not isinstance(param, str):
            raise AmendmentProposalError("threshold parameter names must be strings")
        old = _threshold_value(atom, value, param)
        direction = _relaxation_direction(atom.key, param)
        old_number = _threshold_number(atom.key, param, old)
        new_number = _threshold_number(atom.key, param, new)
        if direction == "lower" and new_number >= old_number:
            raise AmendmentProposalError(f"{atom.instance}.{param} must decrease to relax")
        if direction == "higher" and new_number <= old_number:
            raise AmendmentProposalError(f"{atom.instance}.{param} must increase to relax")
        _set_threshold(atom, value, param, new)
        changes[param] = (old, new)
    if atom.key in _QOR_PARAMS or atom.key == "cycle_count":
        params = _param_mapping(atom, value)
        _validate_criterion_params(atom.key, params)
    return value, changes


def _param_mapping(atom: _Atom, value: Any) -> dict[str, Any]:
    if atom.kind == "target_dict":
        return {key: val for key, val in value.items() if key != "targets"}
    if atom.kind == "cycle":
        return {key: val for key, val in value.items() if key not in {"target", "test"}}
    if atom.kind == "list" and isinstance(value, dict):
        return {key: val for key, val in value.items() if key != "target"}
    if atom.kind == "single" and isinstance(value, dict):
        return value
    return {}


def _threshold_value(atom: _Atom, value: Any, param: str) -> Any:
    if atom.key == "coverage":
        metric, separator, leaf = param.partition(".")
        if separator != "." or leaf != "min_pct":
            raise AmendmentProposalError("Coverage thresholds use <metric>.min_pct")
        try:
            return value["metrics"][metric]["min_pct"]
        except (KeyError, TypeError) as exc:
            raise AmendmentProposalError(f"{atom.instance}.{param} does not exist") from exc
    params = _param_mapping(atom, value)
    if param not in params:
        raise AmendmentProposalError(f"{atom.instance}.{param} does not exist")
    return params[param]


def _set_threshold(atom: _Atom, value: Any, param: str, new: Any) -> None:
    if atom.key == "coverage":
        metric = param.partition(".")[0]
        value["metrics"][metric]["min_pct"] = new
    else:
        value[param] = new


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
    # Cycle Count reductions evaluate a negative delta against -threshold.
    # Invert the stored bound direction for those declared parameters.
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


def _serialize_atoms(atoms: list[_Atom]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {"mandatory": {}, "optional": {}}
    for section in ("mandatory", "optional"):
        keys = dict.fromkeys(atom.key for atom in atoms if atom.section == section)
        for key in keys:
            entries = [atom for atom in atoms if atom.section == section and atom.key == key]
            kinds = {entry.kind for entry in entries}
            if len(kinds) != 1:
                raise AmendmentProposalError(f"{key} has incompatible authoring forms")
            kind = entries[0].kind
            values = [entry.value for entry in entries]
            if kind in {"cycle", "coverage", "target_list", "list"}:
                result[section][key] = values
            elif kind == "target_dict":
                result[section][key] = _serialize_targets(values)
            elif len(values) == 1:
                result[section][key] = values[0]
            else:
                raise AmendmentProposalError(f"{key} has duplicate scalar Criteria")
    return result


def _serialize_targets(values: list[dict[str, Any]]) -> Any:
    params = [{key: value for key, value in item.items() if key != "targets"} for item in values]
    if all(item == params[0] for item in params):
        return {**params[0], "targets": [item["targets"][0] for item in values]}
    return [{"target": item["targets"][0], **params[index]} for index, item in enumerate(values)]


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
