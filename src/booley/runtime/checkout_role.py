"""Compatibility interface for Project checkout classification.

The dependency-light implementation lives below Config and Runtime so both may
enforce the same source-checkout protection without a Config-to-Runtime edge.
"""

from booley.core.checkout_role import (
    SourceCheckoutProjectError,
    is_booley_source_checkout,
    require_project_checkout,
    source_checkout_root,
)

__all__ = [
    "SourceCheckoutProjectError",
    "is_booley_source_checkout",
    "require_project_checkout",
    "source_checkout_root",
]
