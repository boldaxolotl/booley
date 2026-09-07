"""Human-readable scope labels for Booley Flow display boxes."""

from __future__ import annotations

from collections.abc import Iterable

from booley.config.project_config import bare_target


def short_target_name(selector: str) -> str:
    """Return the Target name from a bare or VLNV-qualified selector."""
    return bare_target(selector.strip()).strip()


def _distinct(values: Iterable[str]) -> tuple[str, ...]:
    """Return non-empty values once, preserving their authored order."""
    return tuple(dict.fromkeys(value.strip() for value in values if value.strip()))


def format_flow_display_label(
    targets: Iterable[str],
    *,
    tests: Iterable[str] | None = (),
    mode: str | None = None,
) -> str | None:
    """Summarize one Flow invocation for a compact endpoint-box heading.

    ``tests=None`` means the selected suite's cardinality is unknown. Empty
    tests mean the Flow has no test dimension. Repeated test names across
    Targets count once because the heading describes the selected test scope,
    not simulator process executions.
    """
    selected_targets = tuple(target.strip() for target in targets if target.strip())
    if not selected_targets:
        return None

    if len(selected_targets) == 1:
        target_part = f"target {short_target_name(selected_targets[0])}"
    else:
        target_part = f"{len(selected_targets)} targets"

    parts = [target_part]
    if mode:
        parts.append(mode)
    elif tests is None:
        parts.append("tests")
    else:
        selected_tests = _distinct(tests)
        if len(selected_tests) == 1:
            parts.append(f"test {selected_tests[0]}")
        elif selected_tests:
            parts.append(f"{len(selected_tests)} tests")
    return " · ".join(parts)
