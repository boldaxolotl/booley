"""Derive copyable endpoint invocations from recorded criterion state."""

from __future__ import annotations

from typing import Any

from booley.criteria.endpoint_catalog import CriterionEndpointCatalog
from booley.evidence.fields import SOURCE_FINGERPRINT_DETAIL_KEY


def criterion_family(key: str, endpoint_catalog: CriterionEndpointCatalog) -> str | None:
    """Return the longest built-in family prefix matching *key*."""
    binding = endpoint_catalog.match(key)
    return binding.family if binding is not None else None


def criterion_target(
    key: str,
    entry: Any,
    family: str,
    *,
    per_target: bool,
) -> str | None:
    """Resolve a criterion's exact Target from params, evidence, or its key."""
    from booley.criteria.templates import TARGET_BOUND_CRITERION_FLOWS

    if not per_target and family not in TARGET_BOUND_CRITERION_FLOWS:
        return None

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

    if not key.startswith(f"{family}_"):
        return None

    # Structured simulation keys contain the TB path before the Target, so
    # they must carry params["target"]. Plain per-Target keys are unambiguous.
    if family == "sim_pass" and params.get("tb_path"):
        return None
    return key.removeprefix(f"{family}_")


def planned_invocation(
    key: str,
    entry: Any,
    endpoint_catalog: CriterionEndpointCatalog,
) -> str | None:
    """Build the exact terminal invocation that can satisfy *key*."""
    binding = endpoint_catalog.match(key)
    if binding is None:
        return None
    family = binding.family
    command = binding.command
    params = getattr(entry, "params", {}) or {}
    recorded_selector = params.get("_target_selector")
    target = (
        recorded_selector
        if isinstance(recorded_selector, str) and recorded_selector
        else criterion_target(key, entry, family, per_target=binding.per_target)
    )
    if target and "--target" not in command:
        command = f"{command} --target {target}"

    scope = params.get("scope")
    if isinstance(scope, list):
        scope_values = [str(path).strip() for path in scope if str(path).strip()]
        if scope_values and "--scope" not in command:
            command = f"{command} --scope {','.join(scope_values)}"

    selector = params.get("test_selector") or params.get("selector")
    if family == "sim_pass" and isinstance(selector, str) and selector not in {"", "all"}:
        command = f"{command} --test {selector}"
    return command
