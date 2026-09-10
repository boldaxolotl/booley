"""Immutable Criterion-to-endpoint relationships consumed by Criteria policy."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from booley.criteria.templates import CriterionDef


class CriterionEndpointCatalogError(ValueError):
    """A Criterion-to-endpoint relationship cannot form a valid catalog."""


@dataclass(frozen=True)
class EndpointCriterionRelationship:
    """Plain endpoint metadata supplied by an outer composition root."""

    command: str
    satisfies: tuple[str, ...]
    arguments: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class CriterionEndpointBinding:
    """One validated Criterion family and its producing endpoint command."""

    family: str
    command: str
    workflow_region: str
    per_target: bool


@dataclass(frozen=True)
class CriterionEndpointCatalog:
    """Validated immutable lookup of producing endpoints by Criterion family."""

    _bindings: tuple[CriterionEndpointBinding, ...] = ()

    @classmethod
    def load(
        cls,
        project_criteria_path: Path | None,
        relationships: Iterable[EndpointCriterionRelationship],
    ) -> CriterionEndpointCatalog:
        """Load active definitions and compose them with endpoint relationships."""
        from booley.criteria.templates import (
            load_base_criteria,
            load_project_criteria,
            merge_criteria_defs,
        )

        project = (
            load_project_criteria(project_criteria_path)
            if project_criteria_path is not None
            else []
        )
        definitions, errors = merge_criteria_defs(load_base_criteria(), project)
        if errors:
            raise CriterionEndpointCatalogError("; ".join(errors))
        return cls.build(definitions, relationships)

    @classmethod
    def build(
        cls,
        definitions: Iterable[CriterionDef],
        relationships: Iterable[EndpointCriterionRelationship],
    ) -> CriterionEndpointCatalog:
        """Validate relationships against definitions and build deterministic bindings."""
        definitions_by_name = {definition.name: definition for definition in definitions}
        bindings: dict[str, CriterionEndpointBinding] = {}
        for relationship in relationships:
            arguments = dict(relationship.arguments)
            for family in relationship.satisfies:
                definition = definitions_by_name.get(family)
                if definition is None:
                    raise CriterionEndpointCatalogError(
                        f"Endpoint {relationship.command!r} claims unknown Criterion "
                        f"family {family!r}"
                    )
                command = relationship.command
                if argument_text := arguments.get(family):
                    command = f"{command} {argument_text}"
                binding = CriterionEndpointBinding(
                    family=family,
                    command=command,
                    workflow_region=definition.workflow_region,
                    per_target=definition.per_target,
                )
                existing = bindings.get(family)
                if existing is not None and existing != binding:
                    raise CriterionEndpointCatalogError(
                        f"Criterion family {family!r} has multiple endpoint bindings: "
                        f"{existing.command!r} and {command!r}"
                    )
                bindings[family] = binding
        return cls(tuple(bindings[name] for name in sorted(bindings)))

    def binding_for(self, family: str) -> CriterionEndpointBinding | None:
        """Return the binding for an exact family, or ``None`` when unbound."""
        return next((binding for binding in self._bindings if binding.family == family), None)

    def match(self, key: str) -> CriterionEndpointBinding | None:
        """Return the longest family binding matching one expanded Criterion key."""
        matches = [
            binding
            for binding in self._bindings
            if key == binding.family
            or key.startswith(f"{binding.family}_")
            or key in {f"{binding.family}_clean", f"{binding.family}_done"}
        ]
        return max(matches, key=lambda binding: len(binding.family), default=None)

    def items(self) -> tuple[tuple[str, CriterionEndpointBinding], ...]:
        """Return deterministic immutable family/binding pairs for presentation."""
        return tuple((binding.family, binding) for binding in self._bindings)
