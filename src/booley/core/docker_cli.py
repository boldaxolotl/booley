"""Neutral Docker CLI seam: the runner shape, validated inspection, and Booley's labels."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from typing import Any

from booley.core.boundary import BoundaryError, require_dict, require_str_value

ROLE_LABEL = "booley.role"
PROJECT_ID_LABEL = "booley.project-id"

#: Runs ``docker <args>`` within *timeout* seconds; a non-zero exit is returned, not raised.
Runner = Callable[[list[str], int], subprocess.CompletedProcess[str]]


class InspectError(Exception):
    """``docker <kind> inspect`` failed, or printed no valid document."""

    def __init__(
        self, message: str, result: subprocess.CompletedProcess[str] | None = None
    ) -> None:
        super().__init__(message)
        #: The command that exited non-zero; ``None`` when its output was malformed.
        self.result = result


def inspect(
    run: Runner, kind: str, name: str, *, timeout: int, template: str = "{{json .}}"
) -> dict[str, Any]:
    """Return the JSON object that *template* renders for one Docker object.

    Whatever *run* raises, such as a timeout, passes through.
    """
    result = run([kind, "inspect", name, "--format", template], timeout)
    if result.returncode:
        raise InspectError(f"cannot inspect {kind} {name}: {failure_detail(result)}", result)
    try:
        return require_dict(json.loads(result.stdout), field=f"{kind} {name}")
    except ValueError as exc:
        raise InspectError(f"invalid inspection of {kind} {name}") from exc


def inspect_labels(
    run: Runner, kind: str, name: str, *, timeout: int, allow_empty: bool = False
) -> dict[str, str]:
    """Return the validated labels of one network, container, or image."""
    template = "{{json .Labels}}" if kind == "network" else "{{json .Config.Labels}}"
    document = inspect(run, kind, name, timeout=timeout, template=template)
    try:
        return labels(document, allow_empty=allow_empty)
    except BoundaryError as exc:
        raise InspectError(f"invalid labels on {kind} {name}") from exc


def labels(raw: object, *, allow_empty: bool = False) -> dict[str, str]:
    """Validate a Docker ``Labels`` mapping of non-empty strings; *allow_empty* frees values."""
    values = require_dict(raw, field="Config.Labels")
    return {
        require_str_value(key, field="Config.Labels key"): require_str_value(
            value, field=f"Config.Labels[{key!r}]", allow_empty=allow_empty
        )
        for key, value in values.items()
    }


def failure_detail(result: subprocess.CompletedProcess[str], fallback: str | None = None) -> str:
    """Say why a command failed: stderr, else stdout, else *fallback* or the exit code."""
    return (
        result.stderr.strip() or result.stdout.strip() or fallback or f"exit {result.returncode}"
    )
