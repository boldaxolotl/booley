"""Resolve the runnable test suite owned by a simulation Target."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from booley.config.project_config import lookup_target_section


@dataclass(frozen=True)
class TargetTestSuite:
    """Tests one Target will run and durable skips excluded from the suite."""

    tests: tuple[str | None, ...]
    skipped: tuple[str, ...] = ()

    @property
    def display_names(self) -> tuple[str, ...]:
        """Human-readable names, including the native default invocation."""
        return tuple(test if test is not None else "<default>" for test in self.tests)


def configured_test_names() -> Mapping[str, list[str]]:
    """Return the tests.toml test registry, or an empty registry if unavailable."""
    try:
        from booley.config.project_config import TEST_NAMES

        return TEST_NAMES
    except ImportError:
        return {}


def configured_test_skips() -> Mapping[str, list[str]]:
    """Return durable tests.toml skips, or an empty registry if unavailable."""
    try:
        from booley.config.project_config import TEST_SKIP

        return TEST_SKIP
    except ImportError:
        return {}


def resolve_target_test_suite(
    target: str,
    *,
    test_names: Mapping[str, list[str]] | None = None,
    test_skips: Mapping[str, list[str]] | None = None,
) -> TargetTestSuite:
    """Resolve all runnable tests for *target*.

    Durable skips are honored. An all-skipped Target runs its complete declared
    list so a campaign can never pass vacuously. A Target without a declared
    list gets one native default invocation, represented by ``None``.
    """
    names = configured_test_names() if test_names is None else test_names
    skips_by_target = configured_test_skips() if test_skips is None else test_skips
    available = list(lookup_target_section(names, target) or [])
    if not available:
        return TargetTestSuite((None,))

    durable_skips = set(lookup_target_section(skips_by_target, target) or [])
    runnable = [test for test in available if test not in durable_skips]
    if not runnable:
        return TargetTestSuite(tuple(available))
    skipped = tuple(test for test in available if test in durable_skips)
    return TargetTestSuite(tuple(runnable), skipped)
