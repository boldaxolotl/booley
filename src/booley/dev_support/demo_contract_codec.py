"""Bootstrap-safe codec for the CI-owned PicoRV32 demo contract."""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from booley.core.boundary import (
    BoundaryError,
    is_str_list,
    require_dict,
    require_int,
    require_str,
)
from booley.dev_support.toolchain_provenance import validate_toolchain_provenance

__all__ = [
    "DemoContract",
    "DemoContractError",
    "GeneratedInput",
    "load_contract",
]


class DemoContractError(ValueError):
    """The demo contract is malformed or a checkout does not satisfy it."""


@dataclass(frozen=True)
class GeneratedInput:
    """One ignored artifact prepared for the demo's consuming Targets."""

    path: str
    producer: str
    targets: tuple[str, ...]


@dataclass(frozen=True)
class DemoContract:
    """Trusted, typed representation of the TOML contract boundary."""

    schema: int
    upstream_repository: str
    upstream_ref: str
    project_repository: str
    project_ref: str
    toolchain_url: str
    toolchain_sha256: str
    required_targets: tuple[str, ...]
    required_goals: tuple[dict[str, Any], ...]
    generated_inputs: tuple[GeneratedInput, ...]


def _require_trimmed_str(
    document: Mapping[str, Any], key: str, *, field: str | None = None
) -> str:
    label = field or key
    try:
        value = require_str(document, key)
    except BoundaryError as exc:
        raise BoundaryError(f"{label} must be a non-empty string") from exc
    if not value.strip():
        raise BoundaryError(f"{label} must be a non-empty string")
    if value != value.strip():
        raise BoundaryError(f"{label} must be trimmed")
    return value


def _safe_relative_path(document: Mapping[str, Any], key: str, *, field: str) -> str:
    value = _require_trimmed_str(document, key, field=field)
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise BoundaryError(f"{field} must be a safe relative path")
    return value


def _parse_toolchain_provenance(document: Mapping[str, Any]) -> tuple[str, str]:
    url = _require_trimmed_str(document, "toolchain_url")
    sha256 = _require_trimmed_str(document, "toolchain_sha256")
    try:
        validate_toolchain_provenance(url, sha256)
    except ValueError as exc:
        raise BoundaryError(str(exc)) from exc
    return url, sha256


def _require_unique_strings(value: Any, *, field: str) -> tuple[str, ...]:
    if not is_str_list(value) or not value:
        raise BoundaryError(f"{field} must be a non-empty list[str]")
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not item.strip():
            raise BoundaryError(f"{field}[{index}] must be a non-empty string")
        if item != item.strip():
            raise BoundaryError(f"{field}[{index}] must be trimmed")
        if item in seen:
            raise BoundaryError(f"{field} contains duplicate value {item!r}")
        seen.add(item)
        result.append(item)
    return tuple(result)


def _validate_goal_fields(goal: Mapping[str, Any], family: str, label: str) -> None:
    allowed = {"family", "target", "origin"}
    if family == "synth":
        allowed |= {"thresholds", "baseline"}
        thresholds = require_dict(goal.get("thresholds"), field=f"{label}.thresholds")
        if not thresholds:
            raise BoundaryError(f"{label}.thresholds must be non-empty")
        if "baseline" in goal:
            _require_trimmed_str(goal, "baseline", field=f"{label}.baseline")
    if family == "mutation":
        allowed |= {"scope", "min_detected", "total", "auto"}
        if "scope" in goal:
            _require_unique_strings(goal["scope"], field=f"{label}.scope")
        for key in ("min_detected", "total"):
            if key in goal and require_int(goal[key], field=f"{label}.{key}") < 1:
                raise BoundaryError(f"{label}.{key} must be positive")
        if "auto" in goal and goal["auto"] is not True:
            raise BoundaryError(f"{label}.auto must be true")
    if "origin" in goal:
        _require_trimmed_str(goal, "origin", field=f"{label}.origin")
    extra = set(goal) - allowed
    if extra:
        raise BoundaryError(f"{label} has unknown fields: {', '.join(sorted(extra))}")


def _parse_goals(
    document: Mapping[str, Any], required_targets: tuple[str, ...]
) -> tuple[dict[str, Any], ...]:
    raw = document.get("required_goal")
    if not isinstance(raw, list) or not raw:
        raise BoundaryError("required_goal must be a non-empty array of tables")
    goals: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, value in enumerate(raw):
        goal = require_dict(value, field=f"required_goal[{index}]")
        family = _require_trimmed_str(goal, "family", field=f"required_goal[{index}].family")
        target = _require_trimmed_str(goal, "target", field=f"required_goal[{index}].target")
        if family not in {"lint", "sim", "elab", "synth", "mutation"}:
            raise BoundaryError(f"required_goal[{index}].family is unsupported: {family!r}")
        if target not in required_targets:
            raise BoundaryError(
                f"required_goal[{index}].target {target!r} is not in required_targets"
            )
        _validate_goal_fields(goal, family, f"required_goal[{index}]")
        pair = (family, target)
        if pair in seen:
            raise BoundaryError(f"required_goal contains duplicate pair {family!r} -> {target!r}")
        seen.add(pair)
        goals.append(dict(goal))
    return tuple(goals)


def _parse_generated_inputs(
    document: Mapping[str, Any], required_targets: tuple[str, ...]
) -> tuple[GeneratedInput, ...]:
    raw_inputs = document.get("generated_input")
    if not isinstance(raw_inputs, list) or not raw_inputs:
        raise BoundaryError("generated_input must be a non-empty array of tables")
    allowed_targets = set(required_targets)
    inputs: list[GeneratedInput] = []
    for index, raw_input in enumerate(raw_inputs):
        generated = require_dict(raw_input, field=f"generated_input[{index}]")
        path_value = _safe_relative_path(generated, "path", field=f"generated_input[{index}].path")
        producer = _safe_relative_path(
            generated, "producer", field=f"generated_input[{index}].producer"
        )
        consumers = _require_unique_strings(
            generated.get("targets"), field=f"generated_input[{index}].targets"
        )
        for target in consumers:
            if target not in allowed_targets:
                raise BoundaryError(
                    f"generated_input[{index}].targets consumer {target!r} "
                    "is not in required_targets"
                )
        inputs.append(GeneratedInput(path_value, producer, consumers))
    return tuple(inputs)


def load_contract(path: Path | str) -> DemoContract:
    """Load and structurally validate a demo contract without runtime dependencies."""
    contract_path = Path(path)
    try:
        document = tomllib.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise DemoContractError(f"cannot read demo contract: {exc}") from exc
    try:
        schema = require_int(document.get("schema"), field="schema")
        if schema != 2:
            raise BoundaryError("schema must be 2")
        upstream_repository = _require_trimmed_str(document, "upstream_repository")
        upstream_ref = _require_trimmed_str(document, "upstream_ref")
        project_repository = _require_trimmed_str(document, "project_repository")
        project_ref = _require_trimmed_str(document, "project_ref")
        for key, commit in (("upstream_ref", upstream_ref), ("project_ref", project_ref)):
            if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
                raise BoundaryError(f"{key} must be a full lowercase Git commit SHA")

        required_targets = _require_unique_strings(
            document.get("required_targets"), field="required_targets"
        )
        toolchain_url, toolchain_sha256 = _parse_toolchain_provenance(document)
        goals = _parse_goals(document, required_targets)
        generated_inputs = _parse_generated_inputs(document, required_targets)
    except BoundaryError as exc:
        raise DemoContractError(str(exc)) from exc

    return DemoContract(
        schema=schema,
        upstream_repository=upstream_repository,
        upstream_ref=upstream_ref,
        project_repository=project_repository,
        project_ref=project_ref,
        toolchain_url=toolchain_url,
        toolchain_sha256=toolchain_sha256,
        required_targets=required_targets,
        required_goals=goals,
        generated_inputs=generated_inputs,
    )
