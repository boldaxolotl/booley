"""Length-framed digests for Result Cache key parts."""

from __future__ import annotations

from typing import Any


def frame(hasher: Any, *fields: bytes) -> None:
    """Feed *fields* to *hasher*, each prefixed by its length, so no two splits collide."""
    for field in fields:
        hasher.update(len(field).to_bytes(8, "big") + field)
