"""Neutral Docker CLI seam: the runner shape, validated inspection, and Booley's labels."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from typing import Any, Protocol, TypeVar

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


_T = TypeVar("_T")
_T_co = TypeVar("_T_co", covariant=True)


class Validator(Protocol[_T_co]):
    """A :mod:`booley.core.boundary` ``require_*`` check of one decoded value."""

    def __call__(self, value: Any, *, field: str) -> _T_co: ...


def inspect_value(
    run: Runner,
    kind: str,
    name: str,
    *,
    timeout: int,
    template: str,
    require: Validator[_T],
) -> _T:
    """Return what *require* accepts of the JSON that *template* renders for one object.

    Whatever *run* raises, such as a timeout, passes through.
    """
    result = run([kind, "inspect", name, "--format", template], timeout)
    if result.returncode:
        raise InspectError(f"cannot inspect {kind} {name}: {failure_detail(result)}", result)
    try:
        return require(json.loads(result.stdout), field=f"{kind} {name}")
    except ValueError as exc:
        raise InspectError(f"invalid inspection of {kind} {name}") from exc


def inspect(
    run: Runner, kind: str, name: str, *, timeout: int, template: str = "{{json .}}"
) -> dict[str, Any]:
    """Return the JSON object that *template* renders for one Docker object.

    Whatever *run* raises, such as a timeout, passes through.
    """
    return inspect_value(run, kind, name, timeout=timeout, template=template, require=require_dict)


def exists(run: Runner, kind: str, name: str, *, timeout: int) -> bool:
    """Whether ``docker <kind> inspect`` succeeds; whatever *run* raises passes through."""
    return run([kind, "inspect", name], timeout).returncode == 0


def inspect_label(run: Runner, kind: str, name: str, label: str, *, timeout: int) -> str | None:
    """Return one label of a network, container, or image; ``None`` when it is not set."""
    template = f'{{{{json (index {_labels_field(kind)} "{label}")}}}}'
    return inspect_value(
        run, kind, name, timeout=timeout, template=template, require=_optional_label
    )


def _labels_field(kind: str) -> str:
    return ".Labels" if kind == "network" else ".Config.Labels"


def _optional_label(value: Any, *, field: str) -> str | None:
    # Docker renders a missing key as ``null`` from a raw map and as ``""`` from a typed one.
    if value is None or value == "":
        return None
    return require_str_value(value, field=field)


def inspect_labels(
    run: Runner, kind: str, name: str, *, timeout: int, allow_empty: bool = False
) -> dict[str, str]:
    """Return the validated labels of one network, container, or image."""
    template = f"{{{{json {_labels_field(kind)}}}}}"
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
