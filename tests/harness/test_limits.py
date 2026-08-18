"""Tests for harness._limits — iteration limits and hard caps."""

from __future__ import annotations

import pytest

from booley.harness._limits import (
    HARD_MAX_COMPILE_RETRIES,
    HARD_MAX_DEBUG_ROUNDS,
    HARD_MAX_MUTATION_ITERATIONS,
    MAX_COMPILE_RETRIES,
    MAX_DEBUG_ROUNDS,
    MAX_FIX_LINES,
    MAX_JUDGE_RETRIES,
    MAX_LINT_ITERATIONS,
    MAX_MUTATION_ITERATIONS,
    MAX_POST_REVIEW_SIM_ITERATIONS,
    MUTATION_TESTING_PASS_THRESHOLD,
)


class TestDefaultLimitsPositive:
    """Every default limit must be a positive integer."""

    @pytest.mark.parametrize(
        "limit",
        [
            MAX_DEBUG_ROUNDS,
            MAX_COMPILE_RETRIES,
            MAX_LINT_ITERATIONS,
            MAX_MUTATION_ITERATIONS,
            MAX_POST_REVIEW_SIM_ITERATIONS,
            MAX_FIX_LINES,
            MAX_JUDGE_RETRIES,
        ],
    )
    def test_positive_int(self, limit):
        assert isinstance(limit, int)
        assert limit > 0


class TestHardCapsEnforced:
    """Hard caps must be >= the corresponding default limit."""

    def test_debug_rounds(self):
        assert HARD_MAX_DEBUG_ROUNDS >= MAX_DEBUG_ROUNDS

    def test_compile_retries(self):
        assert HARD_MAX_COMPILE_RETRIES >= MAX_COMPILE_RETRIES

    def test_mutation_iterations(self):
        assert HARD_MAX_MUTATION_ITERATIONS >= MAX_MUTATION_ITERATIONS


class TestHardCapsReasonable:
    """Hard caps should be bounded — prevent runaway iteration."""

    @pytest.mark.parametrize(
        "cap",
        [
            HARD_MAX_DEBUG_ROUNDS,
            HARD_MAX_COMPILE_RETRIES,
            HARD_MAX_MUTATION_ITERATIONS,
        ],
    )
    def test_under_100(self, cap):
        assert cap < 100, "Hard cap suspiciously large — possible runaway risk"


class TestMutationThreshold:
    def test_between_zero_and_one(self):
        assert 0 < MUTATION_TESTING_PASS_THRESHOLD <= 1.0

    def test_is_float(self):
        assert isinstance(MUTATION_TESTING_PASS_THRESHOLD, float)
