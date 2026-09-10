"""Shared Criterion endpoint catalog for interface-level tests."""

from booley.criteria.endpoint_catalog import CriterionEndpointCatalog
from booley.mcp.registry import criterion_endpoint_relationships, discover_mcp_tools


def builtin_endpoint_catalog() -> CriterionEndpointCatalog:
    """Return the production built-in relationships through the public seam."""
    return CriterionEndpointCatalog.load(
        None,
        criterion_endpoint_relationships(discover_mcp_tools()),
    )
