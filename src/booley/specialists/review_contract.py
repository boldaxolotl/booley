"""Resolve the Target-specific part of a Reviewer invocation.

Reviewer operates on source paths, while simulation behavior belongs to a
FuseSoC Target.  This module keeps the path-to-Target inference and guide choice
out of prompt construction so every TB focus uses the same contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from booley.fusesoc import fusesoc_registry


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
    project_root: Path,
    ref: fusesoc_registry.TargetRef,
    scope: set[str],
    *,
    category: str,
) -> bool:
    sources = fusesoc_registry.target_source_files_for_ref(
        project_root,
        ref,
        include_dependencies=True,
        include_headers=True,
    )
    candidates = sources.tb_files if category == "tb" else sources.rtl_source_files
    return scope.issubset({_normalize(path) for path in candidates})


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

    refs: list[tuple[str, fusesoc_registry.TargetRef]] = []
    if target_hint:
        selected = fusesoc_registry.resolve_target_selection(target_hint, project_root)
        if category == "tb" and len(selected) != 1:
            raise ReviewContractError("TB review requires exactly one --target selector")
        for selector in selected:
            refs.append((selector, fusesoc_registry.resolve_ref(project_root, selector)))
    else:
        for bucket in declarations.values():
            for ref in bucket:
                refs.append((fusesoc_registry.minimal_selector(ref, bucket), ref))

    matches = [
        (selector, ref)
        for selector, ref in refs
        if _matches_scope(project_root, ref, normalized_scope, category=category)
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

    kinds = {"cocotb" if ref.cocotb_module else "hdl" for _, ref in matches}
    if len(kinds) != 1:
        if category == "rtl":
            return ReviewTargetContract(
                tuple(sorted(selector for selector, _ in matches)),
                "none",
            )
        candidates = ", ".join(selector for selector, _ in matches)
        raise ReviewContractError(
            "Review scope matches Targets with conflicting TB verdict contracts "
            f"({candidates}); pass --target <selector>"
        )
    return ReviewTargetContract(
        tuple(sorted(selector for selector, _ in matches)),
        next(iter(kinds)),
    )
