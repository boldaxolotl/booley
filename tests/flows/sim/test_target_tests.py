"""Tests for the shared Target test-suite contract."""

import pytest

from booley.flows.sim.target_tests import (
    NoRunnableTestsError,
    require_runnable_target_test_suite,
    resolve_target_test_suite,
)


def test_resolves_all_non_skipped_target_tests() -> None:
    suite = resolve_target_test_suite(
        "sim",
        test_names={"sim": ["reset", "read", "write"]},
        test_skips={"sim": ["read"]},
    )

    assert suite.tests == ("reset", "write")
    assert suite.skipped == ("read",)


def test_all_skipped_returns_empty_runnable_suite() -> None:
    suite = resolve_target_test_suite(
        "sim",
        test_names={"sim": ["reset", "read"]},
        test_skips={"sim": ["reset", "read"]},
    )

    assert suite.tests == ()
    assert suite.skipped == ("reset", "read")
    assert suite.all_skipped is True


def test_required_runnable_suite_rejects_all_skipped_target() -> None:
    with pytest.raises(NoRunnableTestsError, match="every declared test is skipped"):
        require_runnable_target_test_suite(
            "sim",
            test_names={"sim": ["reset", "read"]},
            test_skips={"sim": ["reset", "read"]},
        )


def test_target_without_declared_tests_gets_default_invocation() -> None:
    suite = resolve_target_test_suite("sim", test_names={}, test_skips={})

    assert suite.tests == (None,)
    assert suite.display_names == ("<default>",)


def test_vlnv_target_matches_bare_tests_section() -> None:
    suite = resolve_target_test_suite(
        "vendor:lib:core#sim",
        test_names={"sim": ["smoke", "corner"]},
        test_skips={},
    )

    assert suite.tests == ("smoke", "corner")
