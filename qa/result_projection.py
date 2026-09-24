"""Project append-only Check Result corrections without erasing history."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

try:
    from .run_suite import RunSuiteError
except ImportError:
    from run_suite import RunSuiteError


STATUS_PRECEDENCE = ("fail", "pass", "blocked", "unavailable")


@dataclass(frozen=True, slots=True)
class ResultGroup:
    """One correction graph rooted at an independently recorded attempt."""

    root_id: str
    members: tuple[dict[str, Any], ...]
    corrected_ids: frozenset[str]
    surviving_heads: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class ResultProjection:
    """Deterministic correction roots, history, and effective surviving heads."""

    groups: tuple[ResultGroup, ...]
    roots: dict[str, str]
    corrected_ids: frozenset[str]
    surviving_heads: tuple[dict[str, Any], ...]


def project_results(
    records: Iterable[dict[str, Any]], context: str = "Check Results"
) -> ResultProjection:
    """Validate correction links and retain every append-ordered surviving head."""
    items = tuple(records)
    roots: dict[str, str] = {}
    by_id: dict[str, dict[str, Any]] = {}
    members: dict[str, list[dict[str, Any]]] = {}
    corrected_ids: set[str] = set()
    for item in items:
        result_id = item["check_result_id"]
        corrected = item["corrects_result_id"]
        if corrected is None:
            root_id = result_id
            members[root_id] = []
        else:
            if corrected not in by_id:
                raise RunSuiteError(
                    f"{context}: {result_id}: correction must name an earlier result"
                )
            previous = by_id[corrected]
            if previous["check_id"] != item["check_id"]:
                raise RunSuiteError(f"{context}: {result_id}: correction changed Check identity")
            if previous["attempt"] >= item["attempt"]:
                raise RunSuiteError(f"{context}: {result_id}: correction attempt did not advance")
            root_id = roots[corrected]
            corrected_ids.add(corrected)
        roots[result_id] = root_id
        by_id[result_id] = item
        members[root_id].append(item)
    groups = tuple(
        ResultGroup(
            root_id,
            tuple(group),
            frozenset(item["check_result_id"] for item in group) & corrected_ids,
            tuple(item for item in group if item["check_result_id"] not in corrected_ids),
        )
        for root_id, group in members.items()
    )
    return ResultProjection(
        groups,
        roots,
        frozenset(corrected_ids),
        tuple(item for item in items if item["check_result_id"] not in corrected_ids),
    )


def effective_status(results: Iterable[dict[str, Any]]) -> str:
    """Reduce surviving result statuses with the established conservative precedence."""
    statuses = {item["status"] for item in results}
    for status in STATUS_PRECEDENCE:
        if status in statuses:
            return status
    raise RunSuiteError("Check Result projection has no surviving result")
