"""Derive copyable endpoint invocations from sealed criterion state."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from booley.flows.source_fingerprint import SOURCE_FINGERPRINT_DETAIL_KEY


@lru_cache(maxsize=1)
def _endpoint_contracts() -> dict[str, tuple[str, bool]]:
    """Return criterion family -> (endpoint command, per-target)."""
    from booley.dev_support.criteria import load_base_criteria
    from booley.mcp.registry import build_criterion_endpoint_map, discover_mcp_tools

    definitions = load_base_criteria()
    endpoint_map = build_criterion_endpoint_map(
        {definition.name: definition for definition in definitions},
        discover_mcp_tools(),
    )
    per_target = {definition.name: definition.per_target for definition in definitions}
    return {
        family: (command, per_target.get(family, False))
        for family, (command, _region) in endpoint_map.items()
    }


def criterion_family(key: str) -> str | None:
    """Return the longest built-in family prefix matching *key*."""
    try:
        families = _endpoint_contracts()
    except Exception:  # noqa: BLE001 - status rendering must remain best-effort
        return None
    matches = [
        family
        for family in families
        if key == family
        or key.startswith(f"{family}_")
        or key in {f"{family}_clean", f"{family}_done"}
    ]
    return max(matches, key=len, default=None)


def criterion_target(key: str, entry: Any, family: str) -> str | None:
    """Resolve a criterion's exact Target from params, evidence, or its key."""
    params = getattr(entry, "params", {}) or {}
    target = params.get("target")
    if isinstance(target, str) and target:
        return target

    detail = getattr(entry, "detail", {}) or {}
    stamp = detail.get(SOURCE_FINGERPRINT_DETAIL_KEY)
    if isinstance(stamp, dict):
        target = stamp.get("target")
        if isinstance(target, str) and target:
            return target

    try:
        _command, per_target = _endpoint_contracts()[family]
    except (KeyError, TypeError):
        return None
    if not per_target or not key.startswith(f"{family}_"):
        return None

    # Structured simulation keys contain the TB path before the Target, so
    # they must carry params["target"]. Plain per-Target keys are unambiguous.
    if family == "sim_pass" and params.get("tb_path"):
        return None
    return key.removeprefix(f"{family}_")


def planned_invocation(key: str, entry: Any) -> str | None:
    """Build the exact terminal invocation that can satisfy *key*."""
    family = criterion_family(key)
    if family is None:
        return None
    command, _per_target = _endpoint_contracts()[family]
    target = criterion_target(key, entry, family)
    if target and "--target" not in command:
        command = f"{command} --target {target}"

    params = getattr(entry, "params", {}) or {}
    selector = params.get("test_selector") or params.get("selector")
    if family == "sim_pass" and isinstance(selector, str) and selector not in {"", "all"}:
        command = f"{command} --test {selector}"
    return command
