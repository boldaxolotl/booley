"""The T3 Code policy of one Sandbox, narrowed to its Agent CLIs."""

from __future__ import annotations

import json
from collections.abc import Iterable

from booley.core.boundary import as_dict, is_str_list

# ``[agent] clis`` name to T3 Code driver name.
_CLI_DRIVERS = {"claude": "claudeAgent", "codex": "codex"}


def narrowed_policy(base: bytes, clis: Iterable[str]) -> bytes:
    """Return *base* with ``allowedDrivers`` cut down to the drivers of *clis*.

    Drivers are only removed, never added. When none is removed, *base* is returned
    byte for byte. Raises ``ValueError`` when *base* has no such list of strings.
    """
    try:
        policy: object = json.loads(base)
    except ValueError:
        policy = None
    root = as_dict(policy) or {}
    limits = as_dict(root.get("limits")) or {}  # as_dict copies: reassigned below
    providers = as_dict(limits.get("providers")) or {}
    drivers: object = providers.get("allowedDrivers")
    if not is_str_list(drivers):
        raise ValueError(
            "limits.providers.allowedDrivers is not a list of strings in a JSON object"
        )
    allowed = {_CLI_DRIVERS.get(cli) for cli in clis}
    kept = [driver for driver in drivers if driver in allowed]
    if kept == drivers:
        return base
    providers["allowedDrivers"] = kept
    root["limits"] = limits | {"providers": providers}
    return (json.dumps(root, indent=2) + "\n").encode("utf-8")
