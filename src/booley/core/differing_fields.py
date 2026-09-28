"""Format bounded, named identity differences for operator diagnostics."""

from collections.abc import Mapping


def format_differing_fields(expected: Mapping[str, object], actual: Mapping[str, object]) -> str:
    """Return named differences between caller-selected safe fields."""
    missing = object()
    differences = []
    for field in sorted(expected.keys() | actual.keys()):
        expected_value = expected.get(field, missing)
        actual_value = actual.get(field, missing)
        if expected_value == actual_value:
            continue
        expected_text = "<missing>" if expected_value is missing else repr(expected_value)
        actual_text = "<missing>" if actual_value is missing else repr(actual_value)
        differences.append(f"{field} {expected_text} -> {actual_text}")
    return "; ".join(differences)
