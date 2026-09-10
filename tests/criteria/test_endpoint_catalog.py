"""Behavioral tests for the Criterion endpoint catalog interface."""

import pytest

from booley.criteria.endpoint_catalog import (
    CriterionEndpointCatalog,
    CriterionEndpointCatalogError,
    EndpointCriterionRelationship,
)
from booley.criteria.templates import CriterionDef


def _definition(name: str, *, per_target: bool = False) -> CriterionDef:
    return CriterionDef(
        name=name,
        description=f"Run {name}",
        workflow_region="pre_sim",
        per_target=per_target,
        category="none",
        group="other",
    )


def test_custom_criterion_relationship_is_available_through_catalog() -> None:
    catalog = CriterionEndpointCatalog.build(
        [_definition("drc_clean")],
        [EndpointCriterionRelationship("drc", ("drc_clean",))],
    )

    binding = catalog.binding_for("drc_clean")

    assert binding is not None
    assert binding.command == "drc"
    assert catalog.match("drc_clean_done") == binding


def test_missing_binding_is_explicit() -> None:
    catalog = CriterionEndpointCatalog.build([_definition("drc_clean")], [])

    assert catalog.binding_for("drc_clean") is None
    assert catalog.match("drc_clean_core") is None


def test_unknown_criterion_relationship_fails_at_composition() -> None:
    relationships = [EndpointCriterionRelationship("drc", ("missing_criterion",))]

    with pytest.raises(
        CriterionEndpointCatalogError,
        match="claims unknown Criterion family 'missing_criterion'",
    ):
        CriterionEndpointCatalog.build([_definition("drc_clean")], relationships)


def test_per_target_command_keeps_definition_and_endpoint_arguments() -> None:
    catalog = CriterionEndpointCatalog.build(
        [_definition("coverage", per_target=True)],
        [
            EndpointCriterionRelationship(
                "sim",
                ("coverage",),
                (("coverage", "--coverage"),),
            )
        ],
    )

    binding = catalog.match("coverage_sim_uart")

    assert binding is not None
    assert binding.command == "sim --coverage"
    assert binding.per_target is True


def test_conflicting_endpoint_relationships_fail_at_composition() -> None:
    relationships = [
        EndpointCriterionRelationship("lint", ("lint_clean",)),
        EndpointCriterionRelationship("custom-lint", ("lint_clean",)),
    ]

    with pytest.raises(CriterionEndpointCatalogError, match="multiple endpoint bindings"):
        CriterionEndpointCatalog.build([_definition("lint_clean")], relationships)
