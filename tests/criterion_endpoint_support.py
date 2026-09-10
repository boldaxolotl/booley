"""Shared Criterion endpoint catalog for interface-level tests."""

from booley.criteria.endpoint_catalog import CriterionEndpointCatalog
from booley.criteria.templates import load_base_criteria
from booley.mcp.registry import criterion_endpoint_relationships, discover_mcp_tools


def builtin_endpoint_catalog() -> CriterionEndpointCatalog:
    """Return the production built-in relationships through the public seam."""
    return CriterionEndpointCatalog.build(
        load_base_criteria(),
        criterion_endpoint_relationships(discover_mcp_tools()),
    )
