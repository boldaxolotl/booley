"""Sandbox egress mode vocabulary shared by host config and the Sandbox runtime.

Host ``config.toml [sandbox] egress`` and the in-Sandbox ``BOOLEY_EGRESS``
declaration name the same two modes. It imports nothing, so both
``booley.config`` and the bottom-of-graph ``booley.runtime.runtime_context``
can depend on it.
"""

from __future__ import annotations

EGRESS_MODES = ("proxy", "unproxied")
