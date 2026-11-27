"""The crash-injection hook of the Simulation Flow's publication boundaries.

Callers call it through the module (``faults.fault_point(...)``), never through
``from ... import fault_point``: a test replaces the module attribute, and a name
bound at import would skip the replacement.
"""


def fault_point(boundary: str) -> None:
    """Mark a named publication boundary; does nothing outside crash-injection tests."""
