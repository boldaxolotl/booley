"""Normalized, deterministic plans for built-in Flow work units."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from booley.core.boundary import require_finite_number

PLAN_SCHEMA_VERSION = 1
WorkUnitRole = Literal["candidate", "baseline", "ordinary", "standalone"]


class PlanInput(Protocol):
    """The Target input facts needed by normalized Flow planning."""

    @property
    def path(self) -> str: ...

    @property
    def file_type(self) -> str: ...


def _json_value(value: object) -> object:
    """Return a deterministic JSON-compatible copy of a plan value."""
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_json_value(item) for item in value), key=repr)
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, float):
        require_finite_number(value, field="plan value")
        return value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"plan value {value!r} is not JSON-serializable")


def plan_value_fingerprint(value: object) -> str:
    """Hash a semantic value without exposing it in the rendered plan."""
    encoded = json.dumps(
        _json_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def normalize_plan_path(path: str | Path, work_dir: Path) -> str:
    """Render *path* relative to *work_dir* when it belongs to that checkout."""
    candidate = Path(path)
    if not candidate.is_absolute():
        return candidate.as_posix()
    try:
        return candidate.resolve().relative_to(work_dir.resolve()).as_posix()
    except ValueError:
        return candidate.as_posix()


def normalize_plan_argv(argv: tuple[str, ...], work_dir: Path) -> tuple[str, ...]:
    """Remove the selected checkout's absolute prefix from command arguments."""
    checkout = work_dir.resolve().as_posix().rstrip("/")
    prefixes = ((checkout + "/", "/"), (checkout.replace("/", "\\") + "\\", "\\"))

    def normalize(argument: str) -> str:
        normalized = argument
        for prefix, separator in prefixes:
            if prefix in normalized:
                normalized = normalized.replace(prefix, "")
                if separator == "\\":
                    normalized = normalized.replace("\\", "/")
        for spelling in (checkout, checkout.replace("/", "\\")):
            if normalized == spelling:
                return "."
            normalized = normalized.replace(f"={spelling}", "=.").replace(f" {spelling}", " .")
        return normalized

    return tuple(normalize(argument) for argument in argv)


def normalize_plan_inputs(
    inputs: Iterable[PlanInput],
    work_dir: Path,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return normalized ``(sources, constraints)`` for Target inputs."""
    sources: list[str] = []
    constraints: list[str] = []
    for item in inputs:
        destination = constraints if item.file_type.lower() == "sdc" else sources
        destination.append(item.path)
    return normalize_plan_paths(sources, work_dir), normalize_plan_paths(constraints, work_dir)


def normalize_plan_paths(paths: Iterable[str | Path], work_dir: Path) -> tuple[str, ...]:
    """Normalize an ordered collection of paths to one checkout."""
    return tuple(normalize_plan_path(path, work_dir) for path in paths)


@dataclass(frozen=True)
class CommandPlan:
    """One ordered command, represented without lossy shell rendering."""

    argv: tuple[str, ...]
    cwd: str | None = None
    template: bool = False

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"argv": list(self.argv)}
        if self.cwd is not None:
            payload["cwd"] = self.cwd
        if self.template:
            payload["template"] = True
        return payload


@dataclass(frozen=True)
class WorkUnitPlan:
    """One timeout-bearing unit of Flow execution."""

    unit_id: str
    role: WorkUnitRole
    revision: str | None
    selector: str
    target_identity: str
    test_or_module_scope: tuple[str, ...]
    eda_tool: str | None
    timeout_ms: int
    sources: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    parameters: Mapping[str, object] = field(default_factory=dict)
    recipe: Mapping[str, object] = field(default_factory=dict)
    commands: tuple[CommandPlan, ...] = ()
    expected_artifacts: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.timeout_ms <= 0:
            raise ValueError("WorkUnitPlan.timeout_ms must be positive")
        _json_value(self.parameters)
        _json_value(self.recipe)

    def as_dict(self) -> dict[str, object]:
        return {
            "unit_id": self.unit_id,
            "role": self.role,
            "revision": self.revision,
            "selector": self.selector,
            "target_identity": self.target_identity,
            "test_or_module_scope": list(self.test_or_module_scope),
            "eda_tool": self.eda_tool,
            "timeout_ms": self.timeout_ms,
            "sources": list(self.sources),
            "constraints": list(self.constraints),
            "parameters": _json_value(self.parameters),
            "recipe": _json_value(self.recipe),
            "commands": [command.as_dict() for command in self.commands],
            "expected_artifacts": list(self.expected_artifacts),
            "errors": list(self.errors),
        }

    def semantic_projection(self) -> dict[str, object]:
        """Return execution meaning without invocation-local presentation data."""
        return {
            "role": self.role,
            "revision": self.revision,
            "selector": self.selector,
            "target_identity": self.target_identity,
            "test_or_module_scope": list(self.test_or_module_scope),
            "eda_tool": self.eda_tool,
            "timeout_ms": self.timeout_ms,
            "sources": list(self.sources),
            "constraints": list(self.constraints),
            "parameters": _json_value(self.parameters),
            "recipe": _json_value(self.recipe),
            # argv affects execution identity; cwd is intentionally excluded
            # because scratch/worktree prefixes are invocation-local.
            "commands": [list(command.argv) for command in self.commands],
        }


@dataclass(frozen=True)
class FlowPlan:
    """Aggregate built-in Flow plan, complete or diagnostically partial."""

    flow: str
    mode: str
    work_units: tuple[WorkUnitPlan, ...]
    aggregate_errors: tuple[str, ...] = ()
    planning_disclosures: tuple[str, ...] = ()
    schema_version: int = PLAN_SCHEMA_VERSION

    @property
    def semantic_plan_fingerprint(self) -> str:
        projection = {
            "schema_version": self.schema_version,
            "flow": self.flow,
            "mode": self.mode,
            "work_units": [unit.semantic_projection() for unit in self.work_units],
        }
        encoded = json.dumps(
            projection,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "flow": self.flow,
            "mode": self.mode,
            "semantic_plan_fingerprint": self.semantic_plan_fingerprint,
            "work_units": [unit.as_dict() for unit in self.work_units],
            "aggregate_errors": list(self.aggregate_errors),
            "planning_disclosures": list(self.planning_disclosures),
        }


def stable_unit_id(
    flow: str,
    selector: str,
    scope: tuple[str, ...],
    *,
    role: WorkUnitRole = "ordinary",
    revision: str | None = None,
) -> str:
    """Build a readable collision-resistant ID from semantic unit identity."""
    identity = json.dumps(
        [flow, role, revision, selector, list(scope)],
        separators=(",", ":"),
        ensure_ascii=True,
    )
    digest = hashlib.sha256(identity.encode()).hexdigest()[:12]
    stem = re.sub(r"[^a-zA-Z0-9_.-]+", "-", f"{flow}-{role}-{selector}").strip("-")
    return f"{stem}-{digest}"


__all__ = [
    "PLAN_SCHEMA_VERSION",
    "CommandPlan",
    "FlowPlan",
    "WorkUnitPlan",
    "WorkUnitRole",
    "normalize_plan_argv",
    "normalize_plan_inputs",
    "normalize_plan_path",
    "normalize_plan_paths",
    "plan_value_fingerprint",
    "stable_unit_id",
]
