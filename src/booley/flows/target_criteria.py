"""Resolve Target-specific criterion parameters and authorized RTL scope."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any


class CampaignScopeError(ValueError):
    """The campaign has no unambiguous authorized RTL scope."""

    reason: str

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class TargetCriterion:
    """One Target-specific criterion and its immutable parameter view."""

    base_key: str
    key: str
    params: Mapping[str, Any]


def resolve_target_criteria(
    target: str,
    criterion_keys: Sequence[str],
    criteria: Mapping[str, object],
) -> tuple[TargetCriterion, ...]:
    """Return the criteria belonging to *target* in requested order."""
    resolved: list[TargetCriterion] = []
    for base_key in criterion_keys:
        key = f"{base_key}_{target}"
        if key not in criteria:
            continue
        params = _entry_params(criteria[key])
        resolved.append(TargetCriterion(base_key, key, MappingProxyType(dict(params))))
    return tuple(resolved)


def resolve_campaign_scope(
    explicit_scope: str | Sequence[str] | None,
    criteria: Sequence[TargetCriterion],
) -> tuple[str, ...]:
    """Resolve one explicit or criterion-derived campaign scope."""
    explicit = _scope_paths(explicit_scope)
    if explicit:
        return explicit
    scopes = {_scope_paths(criterion.params.get("scope")) for criterion in criteria}
    scopes.discard(())
    if len(scopes) == 1:
        return next(iter(scopes))
    reason = "missing" if not scopes else "conflicting"
    raise CampaignScopeError(reason)


def _entry_params(entry: object) -> Mapping[str, Any]:
    params = getattr(entry, "params", None)
    if isinstance(params, Mapping):
        return params
    if isinstance(entry, Mapping):
        nested = entry.get("params")
        if isinstance(nested, Mapping):
            return nested
    return {}


def _scope_paths(raw_scope: object) -> tuple[str, ...]:
    if isinstance(raw_scope, str):
        values = raw_scope.split(",")
    elif isinstance(raw_scope, Sequence) and not isinstance(raw_scope, (bytes, bytearray)):
        values = [value for value in raw_scope if isinstance(value, str)]
    else:
        return ()
    return tuple(value.strip() for value in values if value.strip())
