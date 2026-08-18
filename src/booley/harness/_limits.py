"""Harness iteration limits and hard caps.

Two-tier pattern:
- Default limits: used unless the run config recommends lower values.
- Hard caps: absolute ceilings enforced by Python regardless of run-config input.
"""

from __future__ import annotations

MAX_DEBUG_ROUNDS = 5
MAX_COMPILE_RETRIES = 5
MAX_LINT_ITERATIONS = 3
MAX_MUTATION_ITERATIONS = 3
MAX_POST_REVIEW_SIM_ITERATIONS = 3
MAX_FIX_LINES = 120

MUTATION_TESTING_PASS_THRESHOLD = 0.5

MAX_JUDGE_RETRIES = 2  # consecutive judge attempts per investigation before re-investigating

HARD_MAX_DEBUG_ROUNDS = 10
HARD_MAX_COMPILE_RETRIES = 8
HARD_MAX_MUTATION_ITERATIONS = 5
