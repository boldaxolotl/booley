"""Compatibility imports for the shared Target campaign domain."""

from booley.flows.target_test_suite import (
    NoRunnableTestsError,
    TargetTestSuite,
    configured_test_names,
    configured_test_skips,
    require_runnable_target_test_suite,
    resolve_target_test_suite,
)

__all__ = [
    "NoRunnableTestsError",
    "TargetTestSuite",
    "configured_test_names",
    "configured_test_skips",
    "require_runnable_target_test_suite",
    "resolve_target_test_suite",
]
