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
from booley.targets.target import TargetInspection, inspect_target, select_targets


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
) -> list[tuple[str, TargetInspection]]:
    if target_hint:
        selected = select_targets(project_root, target_hint)
        if category == "tb" and len(selected) != 1:
            raise ReviewContractError("TB review requires exactly one --target selector")
        return [
            (handle.selector, inspect_target(project_root, handle.identity)) for handle in selected
        ]
    candidates = [
        inspect_target(project_root, f"{ref.vlnv}#{ref.name}")
        for bucket in declarations.values()
        for ref in bucket
        if not ref.doctor_selftest
    ]
    unique = {inspection.handle.identity: inspection for inspection in candidates}
    return [
        (inspection.handle.selector, inspection)
        for inspection in sorted(unique.values(), key=lambda item: item.handle.identity)
    ]


def _target_contract(
    matches: list[tuple[str, TargetInspection]],
    *,
    category: str,
) -> ReviewTargetContract:
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

    refs = _candidate_refs(
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
