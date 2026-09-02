"""Canonical public Booley Flow names and configuration helpers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import cast

LEGACY_TO_CANONICAL: dict[str, str] = {
    "asic_synthesize": "synth",
    "fpga_impl": "fpga",
    "simulate": "sim",
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


def config_section(flows: Mapping[str, object], name: str) -> dict[str, object]:
    """Return a Flow's config table using its canonical spelling."""
    raw = flows.get(canonical(name))
    if not isinstance(raw, Mapping):
        return {}
    return {
        key: value
        for key, value in cast(Mapping[object, object], raw).items()
        if isinstance(key, str)
    }


def legacy(name: str) -> str | None:
    """Return a Flow name's former long spelling, when one exists."""
    return CANONICAL_TO_LEGACY.get(canonical(name))


def implementation_module(name: str) -> str:
    """Return the executable package implementing a built-in Flow."""
    return canonical(name)


def canonicalize_config(data: Mapping[str, object]) -> dict[str, object]:
    """Return config with canonical Flow table names inside ``[flows]``."""
    normalized = dict(data)
    raw_flows = data.get("flows", {})
    if not isinstance(raw_flows, Mapping):
        return normalized
    flows = {
        key: value
        for key, value in cast(Mapping[object, object], raw_flows).items()
        if isinstance(key, str)
    }
    for old, new in LEGACY_TO_CANONICAL.items():
        if new not in flows and old in flows:
            flows[new] = flows[old]
        flows.pop(old, None)
    normalized["flows"] = flows
    return normalized
