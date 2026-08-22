"""Resolve runnable tests and durable skips for one simulation Target."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from booley.config.project_config import lookup_target_section


class NoRunnableTestsError(ValueError):
    """A Target's durable skip policy excludes every declared test."""

    target: str
    skipped: tuple[str, ...]

    def __init__(self, target: str, skipped: tuple[str, ...]) -> None:
        self.target = target
        self.skipped = skipped
        super().__init__(
            f"Target {target!r} has no runnable tests; "
            f"every declared test is skipped: {', '.join(skipped)}"
        )


@dataclass(frozen=True)
class TargetTestSuite:
    """Tests one Target will run and durable skips excluded from the suite."""

    tests: tuple[str | None, ...]
    skipped: tuple[str, ...] = ()

    @property
    def display_names(self) -> tuple[str, ...]:
        """Human-readable names, including the native default invocation."""
        return tuple(test if test is not None else "<default>" for test in self.tests)

    @property
    def all_skipped(self) -> bool:
        """Whether the Target declared tests but excluded every one of them."""
        return not self.tests and bool(self.skipped)


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
    """Resolve runnable tests and preserve the complete durable skip set."""
    names = configured_test_names() if test_names is None else test_names
    skips_by_target = configured_test_skips() if test_skips is None else test_skips
    available = list(lookup_target_section(names, target) or [])
    if not available:
        return TargetTestSuite((None,))

    durable_skips = set(lookup_target_section(skips_by_target, target) or [])
    runnable = [test for test in available if test not in durable_skips]
    if not runnable:
        return TargetTestSuite((), tuple(available))
    skipped = tuple(test for test in available if test in durable_skips)
    return TargetTestSuite(tuple(runnable), skipped)


def require_runnable_target_test_suite(
    target: str,
    *,
    test_names: Mapping[str, list[str]] | None = None,
    test_skips: Mapping[str, list[str]] | None = None,
) -> TargetTestSuite:
    """Resolve *target* and reject an all-skipped, vacuous campaign."""
    suite = resolve_target_test_suite(
        target,
        test_names=test_names,
        test_skips=test_skips,
    )
    if suite.all_skipped:
        raise NoRunnableTestsError(target, suite.skipped)
    return suite
