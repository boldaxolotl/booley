"""Shared Docker container naming for parallel image validations."""

from __future__ import annotations

import itertools
import os

_CONTAINER_SEQUENCE = itertools.count(1)


def next_ci_container_name() -> str | None:
    """Return a unique cleanup-addressable name when CI provides a prefix."""
    prefix = os.environ.get("BOOLEY_DOCKER_NAME_PREFIX")
    return f"{prefix}-{next(_CONTAINER_SEQUENCE)}" if prefix else None
