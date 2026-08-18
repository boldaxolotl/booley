"""Canonical public Booley Flow names and configuration helpers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

LEGACY_TO_CANONICAL: dict[str, str] = {
    "asic_synthesize": "synth",
    "fpga_impl": "fpga",
    "simulate": "sim",
    "elaborate": "elab",
}
CANONICAL_TO_LEGACY = {new: old for old, new in LEGACY_TO_CANONICAL.items()}
DEFAULT_TARGET_KEY = "default_target"
RETIRED_TARGET_KEY = "target"


def canonical(name: str) -> str:
    """Return the canonical public spelling for a Flow name."""
    return LEGACY_TO_CANONICAL.get(name, name)


def canonical_set(names: Iterable[str]) -> set[str]:
    """Canonicalize a collection of Flow-name tokens."""
    return {canonical(name) for name in names}


def config_section(flows: Mapping[str, Any], name: str) -> dict[str, Any]:
    """Return a Flow's config table using its canonical spelling."""
    raw = flows.get(canonical(name))
    return dict(raw) if isinstance(raw, Mapping) else {}


def legacy(name: str) -> str | None:
    """Return a Flow name's former long spelling, when one exists."""
    return CANONICAL_TO_LEGACY.get(canonical(name))


def implementation_module(name: str) -> str:
    """Return the Python module implementing a built-in Flow."""
    canonical_name = canonical(name)
    return CANONICAL_TO_LEGACY.get(canonical_name, canonical_name)


def canonicalize_config(data: Mapping[str, Any]) -> dict[str, Any]:
    """Return config with canonical Flow table names inside ``[flows]``."""
    normalized = dict(data)
    raw_flows = data.get("flows", {})
    if not isinstance(raw_flows, Mapping):
        return normalized
    flows = dict(raw_flows)
    for old, new in LEGACY_TO_CANONICAL.items():
        if new not in flows and old in flows:
            flows[new] = flows[old]
        flows.pop(old, None)
    normalized["flows"] = flows
    return normalized
