"""Resolve the Target-specific part of a Reviewer invocation.

Reviewer operates on source paths, while simulation behavior belongs to a
FuseSoC Target.  This module keeps the path-to-Target inference and guide choice
out of prompt construction so every TB focus uses the same contract.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from booley.fusesoc import fusesoc_registry
from booley.targets.target import (
    TargetInspection,
    flow_can_drive,
    inspect_target,
    select_target,
    select_targets,
)


class ReviewContractError(ValueError):
    """The requested review cannot be bound to one Target behavior."""


@dataclass(frozen=True)
class ReviewTargetContract:
    """Target facts which affect review guidance and freshness."""

    selectors: tuple[str, ...]
    kind: str

    @property
    def is_cocotb(self) -> bool:
        return self.kind == "cocotb"


@dataclass(frozen=True)
class _InspectionFailure:
    """One candidate whose relevance could not be determined."""

    identity: str
    error: str


def _normalize(path: str) -> str:
    value = path.replace("\\", "/").removeprefix("./")
    return str(PurePosixPath(value))


def _matches_scope(
    inspection: TargetInspection,
    scope: set[str],
    *,
    category: str,
) -> bool:
    candidates = {
        _normalize(item.path)
        for item in inspection.inputs
        if ("tb" in item.tags) == (category == "tb")
    }
    return scope.issubset(candidates)


def _candidate_refs(
    project_root: Path,
    declarations: Mapping[str, list[fusesoc_registry.TargetRef]],
    *,
    category: str,
    target_hint: str | None,
) -> tuple[list[tuple[str, TargetInspection]], list[_InspectionFailure]]:
    if target_hint:
        selected = select_targets(project_root, target_hint)
        if category == "tb" and len(selected) != 1:
            raise ReviewContractError("TB review requires exactly one --target selector")
        candidates = []
        for handle in selected:
            try:
                candidates.append((handle.selector, inspect_target(project_root, handle)))
            except (fusesoc_registry.FuseSocError, OSError) as exc:
                raise ReviewContractError(
                    f"Relevant Target {handle.identity!r} could not be inspected: {exc}"
                ) from exc
        return candidates, []

    handles = {}
    failures: list[_InspectionFailure] = []
    for ref in (ref for bucket in declarations.values() for ref in bucket):
        if ref.doctor_selftest or (category == "tb" and not flow_can_drive("sim", ref)):
            continue
        identity = f"{ref.vlnv}#{ref.name}"
        try:
            handle = select_target(project_root, identity)
        except (fusesoc_registry.FuseSocError, OSError) as exc:
            failures.append(_InspectionFailure(identity, str(exc)))
            continue
        handles[handle.identity] = handle

    candidates = []
    for handle in sorted(handles.values(), key=lambda item: item.identity):
        try:
            candidates.append((handle.selector, inspect_target(project_root, handle)))
        except (fusesoc_registry.FuseSocError, OSError) as exc:
            failures.append(_InspectionFailure(handle.identity, str(exc)))
    return candidates, sorted(failures, key=lambda item: item.identity)


def _raise_uncertain_failures(
    failures: list[_InspectionFailure],
    matches: list[tuple[str, TargetInspection]],
) -> None:
    """Fail closed when an uninspectable candidate might own the scope."""
    if not failures:
        return
    matched = ", ".join(selector for selector, _ in matches)
    context = f"; inspected matches: {matched}" if matched else ""
    details = "; ".join(f"{item.identity}: {item.error}" for item in failures)
    raise ReviewContractError(
        "Cannot determine a unique review Target because potentially relevant "
        f"candidate inspection failed{context}; failures: {details}"
    )


def _target_contract(
    matches: list[tuple[str, TargetInspection]],
    *,
    category: str,
) -> ReviewTargetContract:
    if category == "tb" and len(matches) > 1:
        candidates = ", ".join(selector for selector, _ in matches)
        kinds = {
            "cocotb" if inspection.flow_options.get("cocotb_module") else "hdl"
            for _, inspection in matches
        }
        kind_context = f" with conflicting kinds {sorted(kinds)}" if len(kinds) > 1 else ""
        raise ReviewContractError(
            f"Review scope ambiguously matches multiple TB Targets ({candidates}){kind_context}; "
            "pass --target <selector>"
        )
    kinds = {
        "cocotb" if inspection.flow_options.get("cocotb_module") else "hdl"
        for _, inspection in matches
    }
    selectors = tuple(sorted(selector for selector, _ in matches))
    if len(kinds) == 1:
        return ReviewTargetContract(selectors, next(iter(kinds)))
    if category == "rtl":
        return ReviewTargetContract(selectors, "none")
    candidates = ", ".join(selector for selector, _ in matches)
    raise ReviewContractError(
        "Review scope matches Targets with conflicting TB verdict contracts "
        f"({candidates}); pass --target <selector>"
    )


def resolve_review_target(
    project_root: Path,
    scope: list[str],
    *,
    category: str,
    target_hint: str | None = None,
) -> ReviewTargetContract:
    """Resolve the Target kind shared by all Targets covering *scope*.

    An explicit hint narrows the candidate set exactly.  Without one, every
    live Target is considered.  Multiple aliases are safe only when they agree
    whether Cocotb or HDL owns the testbench verdict contract.
    """

    normalized_scope = {_normalize(path) for path in scope}
    declarations = fusesoc_registry.target_declarations(project_root)

    refs, failures = _candidate_refs(
        project_root,
        declarations,
        category=category,
        target_hint=target_hint,
    )
    matches = [
        (selector, inspection)
        for selector, inspection in refs
        if _matches_scope(inspection, normalized_scope, category=category)
    ]
    _raise_uncertain_failures(failures, matches)
    if target_hint and not matches:
        raise ReviewContractError(
            f"--target {target_hint!r} does not contain every {category.upper()} scope file"
        )
    if not matches:
        if category == "tb" and declarations:
            raise ReviewContractError(
                "No selectable Target contains every TB scope file; register the files "
                "with tags: [tb] or pass --target <selector>"
            )
        return ReviewTargetContract((), "none")

    return _target_contract(matches, category=category)
