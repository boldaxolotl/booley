"""Evaluate Booley's executable source dependency contract."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from tests.architecture.import_graph import (
    Dependency,
    top_level_package,
    top_level_package_sccs,
)

_MODULE_PATTERN = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")
_ISSUE_PATTERN = re.compile(r"(?:#\d+|https://github\.com/[^/]+/[^/]+/issues/\d+)")


@dataclass(frozen=True)
class ModuleSelector:
    """One exact, prefix, or immediate-child module selector."""

    kind: Literal["exact", "prefix", "package-files"]
    module: str

    @classmethod
    def exact(cls, module: str) -> ModuleSelector:
        return cls("exact", module)

    @classmethod
    def prefix(cls, module: str) -> ModuleSelector:
        return cls("prefix", module)

    @classmethod
    def package_files(cls, module: str) -> ModuleSelector:
        """Select a package module and the file modules directly inside it."""
        return cls("package-files", module)

    def matches(self, candidate: str, path: Path | None = None) -> bool:
        if self.kind == "exact":
            return candidate == self.module
        if self.kind == "prefix":
            return candidate == self.module or candidate.startswith(f"{self.module}.")
        if candidate == self.module:
            return True
        prefix = f"{self.module}."
        is_direct_child = candidate.startswith(prefix) and "." not in candidate[len(prefix) :]
        return is_direct_child and (path is None or path.name != "__init__.py")


@dataclass(frozen=True)
class DirectionRule:
    """One forbidden Cartesian product of source and target selectors."""

    identifier: str
    sources: tuple[ModuleSelector, ...]
    targets: tuple[ModuleSelector, ...]
    reason: str

    def matches(self, dependency: Dependency) -> bool:
        return any(
            item.matches(dependency.source, dependency.path) for item in self.sources
        ) and any(item.matches(dependency.target) for item in self.targets)


@dataclass(frozen=True)
class CompositionPermission:
    """One exact, designed exception for a composition root."""

    identifier: str
    rule: str
    source: str
    target: str
    reason: str


@dataclass(frozen=True)
class LegacyWaiver:
    """One exact legacy edge with mandatory retirement metadata."""

    identifier: str
    rule: str
    source: str
    target: str
    explanation: str
    retirement_issue: str


@dataclass(frozen=True)
class ArchitectureContract:
    """Executable directions, exceptions, and approved legacy cycle groups."""

    rules: tuple[DirectionRule, ...] = ()
    permissions: tuple[CompositionPermission, ...] = ()
    waivers: tuple[LegacyWaiver, ...] = ()
    approved_sccs: tuple[frozenset[str], ...] = ()


@dataclass(frozen=True)
class ContractProblem:
    """One deterministic contract failure."""

    kind: Literal["metadata", "direction", "stale-waiver", "scc"]
    message: str
    rule: str | None = None
    dependency: Dependency | None = None


def evaluate_contract(
    dependencies: tuple[Dependency, ...], contract: ArchitectureContract
) -> tuple[ContractProblem, ...]:
    """Return every metadata, direction, stale-waiver, and SCC failure."""
    problems = [
        *_metadata_problems(contract),
        *_stale_waiver_problems(dependencies, contract.waivers),
        *_direction_problems(dependencies, contract),
        *_scc_problems(dependencies, contract.approved_sccs),
    ]
    return tuple(sorted(problems, key=_problem_key))


def format_problems(problems: tuple[ContractProblem, ...]) -> str:
    """Render failures with locations and normalized policy identities."""
    lines = []
    for problem in problems:
        if problem.dependency is None:
            lines.append(f"[{problem.kind}] {problem.message}")
            continue
        dependency = problem.dependency
        location = f"{dependency.path}:{dependency.line}:{dependency.column + 1}"
        lines.append(f"{location}: [{problem.kind}] {problem.message}")
    return "\n".join(lines)


def _metadata_problems(contract: ArchitectureContract) -> tuple[ContractProblem, ...]:
    problems: list[ContractProblem] = []
    rule_identifiers = {item.identifier for item in contract.rules}
    for rule in contract.rules:
        if not rule.reason.strip():
            problems.append(_metadata(f"{rule.identifier} has no design reason", rule.identifier))
        for selector in (*rule.sources, *rule.targets):
            if not _is_exact_module(selector.module):
                problems.append(
                    _metadata(
                        f"{rule.identifier} selector is not a module: {selector.module}",
                        rule.identifier,
                    )
                )

    for permission in contract.permissions:
        problems.extend(
            _policy_metadata_problems(
                permission.identifier,
                permission.rule,
                permission.source,
                permission.target,
                permission.reason,
                "design reason",
                rule_identifiers,
            )
        )
    for waiver in contract.waivers:
        problems.extend(
            _policy_metadata_problems(
                waiver.identifier,
                waiver.rule,
                waiver.source,
                waiver.target,
                waiver.explanation,
                "design explanation",
                rule_identifiers,
            )
        )
        if not waiver.retirement_issue.strip():
            problems.append(_metadata(f"{waiver.identifier} has no retirement issue", waiver.rule))
        elif not _ISSUE_PATTERN.fullmatch(waiver.retirement_issue):
            problems.append(
                _metadata(
                    f"{waiver.identifier} retirement issue is not a GitHub issue: "
                    f"{waiver.retirement_issue}",
                    waiver.rule,
                )
            )
    return tuple(problems)


def _stale_waiver_problems(
    dependencies: tuple[Dependency, ...], waivers: tuple[LegacyWaiver, ...]
) -> tuple[ContractProblem, ...]:
    current_edges = {(item.source, item.target) for item in dependencies}
    return tuple(
        ContractProblem(
            "stale-waiver",
            f"{waiver.identifier} is stale: {waiver.source} -> {waiver.target} "
            "is not a current exact edge",
            waiver.rule,
        )
        for waiver in waivers
        if (waiver.source, waiver.target) not in current_edges
    )


def _direction_problems(
    dependencies: tuple[Dependency, ...], contract: ArchitectureContract
) -> tuple[ContractProblem, ...]:
    allowed_edges = {
        (item.rule, item.source, item.target)
        for item in (*contract.permissions, *contract.waivers)
    }
    problems = []
    for dependency in dependencies:
        for identifier, rules in _rules_by_identifier(contract.rules).items():
            edge = (identifier, dependency.source, dependency.target)
            if not any(rule.matches(dependency) for rule in rules) or edge in allowed_edges:
                continue
            problems.append(
                ContractProblem(
                    "direction",
                    f"{dependency.source} -> {dependency.target} violates {identifier}: "
                    f"{rules[0].reason}; applicable policy: "
                    f"{_applicable_policy(identifier, dependency.source, contract)}",
                    identifier,
                    dependency,
                )
            )
    return tuple(problems)


def _scc_problems(
    dependencies: tuple[Dependency, ...],
    approved_sccs: tuple[frozenset[str], ...],
) -> tuple[ContractProblem, ...]:
    problems = []
    for group in top_level_package_sccs(dependencies):
        members = frozenset(group)
        if len(members) <= 1 or any(members <= approved for approved in approved_sccs):
            continue
        problems.append(
            ContractProblem(
                "scc",
                "current multi-package SCC is not a subset of one approved legacy "
                f"SCC: {', '.join(group)}; SCC subset gate witness edges:\n"
                + _format_scc_witnesses(dependencies, members, approved_sccs),
            )
        )
    return tuple(problems)


def _policy_metadata_problems(
    identifier: str,
    rule: str,
    source: str,
    target: str,
    explanation: str,
    explanation_name: str,
    rule_identifiers: set[str],
) -> list[ContractProblem]:
    problems = []
    if rule not in rule_identifiers:
        problems.append(_metadata(f"{identifier} refers to unknown rule {rule}", rule))
    if not _is_exact_module(source):
        problems.append(_metadata(f"{identifier} source is not an exact module: {source}", rule))
    if not _is_exact_module(target):
        problems.append(_metadata(f"{identifier} target is not an exact module: {target}", rule))
    if not explanation.strip():
        problems.append(_metadata(f"{identifier} has no {explanation_name}", rule))
    return problems


def _metadata(message: str, rule: str) -> ContractProblem:
    return ContractProblem("metadata", message, rule)


def _is_exact_module(module: str) -> bool:
    return bool(_MODULE_PATTERN.fullmatch(module))


def _format_scc_witnesses(
    dependencies: tuple[Dependency, ...],
    members: frozenset[str],
    approved_sccs: tuple[frozenset[str], ...],
) -> str:
    baseline = max(
        approved_sccs,
        key=lambda approved: len(members & approved),
        default=frozenset(),
    )
    unexpected_sources = members - baseline
    witnesses = []
    for dependency in dependencies:
        source_owner = top_level_package(dependency.source)
        target_owner = top_level_package(dependency.target)
        if (
            source_owner == target_owner
            or source_owner not in unexpected_sources
            or target_owner not in members
            or any(
                source_owner in approved and target_owner in approved for approved in approved_sccs
            )
        ):
            continue
        location = f"{dependency.path}:{dependency.line}:{dependency.column + 1}"
        witnesses.append(f"- {location}: {dependency.source} -> {dependency.target}")
    return "\n".join(sorted(witnesses))


def _rules_by_identifier(
    rules: tuple[DirectionRule, ...],
) -> dict[str, tuple[DirectionRule, ...]]:
    identifiers = sorted({item.identifier for item in rules})
    return {
        identifier: tuple(item for item in rules if item.identifier == identifier)
        for identifier in identifiers
    }


def _applicable_policy(rule: str, source: str, contract: ArchitectureContract) -> str:
    descriptions = [
        f"{item.identifier} allows only {item.source} -> {item.target}"
        for item in (*contract.permissions, *contract.waivers)
        if item.rule == rule and item.source == source
    ]
    return "; ".join(descriptions) if descriptions else "none"


def _problem_key(problem: ContractProblem) -> tuple[str, str, str, int, int]:
    dependency = problem.dependency
    return (
        problem.kind,
        problem.rule or "",
        dependency.path.as_posix() if dependency else "",
        dependency.line if dependency else 0,
        dependency.column if dependency else 0,
    )
