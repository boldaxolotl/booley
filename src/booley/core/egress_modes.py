"""Sandbox egress mode vocabulary shared by host config and the Sandbox runtime.

Host ``config.toml [sandbox] egress`` and the in-Sandbox ``BOOLEY_EGRESS``
declaration name the same two modes. It imports nothing, so both
``booley.config`` and the bottom-of-graph ``booley.runtime.runtime_context``
can depend on it.
"""

from __future__ import annotations

EGRESS_MODES = ("proxy", "unproxied")


def declared_egress(value: str | None) -> str:
    """Classify a Sandbox's ``BOOLEY_EGRESS`` declaration as an egress mode.

    Booley issues ``BOOLEY_EGRESS=unproxied`` only for an Unproxied Sandbox;
    an absent or any other value declares the default ``proxy`` mode.
    """
    return "unproxied" if value == "unproxied" else "proxy"
