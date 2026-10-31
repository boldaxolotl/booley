"""Persisted field names shared by evidence producers and Criteria policy."""

SOURCE_FINGERPRINT_DETAIL_KEY = "_source_fingerprint"
#: Reason of a change that unmeets a Criterion because its sources changed:
#: its detail describes the old evidence, not a new observation.
SOURCE_INVALIDATED_REASON = "source-invalidated"
#: Reason of a Goal verdict recorded unmet because its Test Runs are not resolved yet.
GOAL_TEST_RUN_PENDING_REASON = "goal-test-run-pending"
#: Reason of a Goal verdict recorded unmet because the change did not measure the
#: Goal's metric (a Cycle Count or coverage Goal graded by an ordinary run).
GOAL_TEST_RUN_NOT_MEASURED_REASON = "goal-test-run-metric-missing"
#: Reasons of an unmet verdict that judged nothing: the Goal was not evaluated, so
#: the verdict is "not yet run", never an observed failure (it must not latch
#: ``ever_failed``).
UNJUDGED_REASONS = frozenset({GOAL_TEST_RUN_PENDING_REASON, GOAL_TEST_RUN_NOT_MEASURED_REASON})

RECIPE_FINGERPRINT_PARAM = "_recipe_fingerprint"
RECIPE_FINGERPRINT_DETAIL = "_recipe_fingerprint"
RECIPE_SNAPSHOT_PARAM = "_recipe_snapshot"
RECIPE_SNAPSHOT_DETAIL = "_recipe_snapshot"
BASELINE_RECIPE_FINGERPRINT_DETAIL = "_baseline_recipe_fingerprint"
BASELINE_RECIPE_SNAPSHOT_DETAIL = "_baseline_recipe_snapshot"
BASELINE_REF_PARAM = "_baseline_ref"
BASELINE_REF_DETAIL = "_baseline_ref"
BASELINE_TARGET_DETAIL = "_baseline_target"
CANDIDATE_TARGET_DETAIL = "_candidate_target"

# Simulation acceptance facts, by schema version. v2 names the Test Run of
# every observation and of every baseline Cycle Count observation (ADR 0072).
ACCEPTANCE_FACTS_SCHEMA_V1 = "booley.simulation-acceptance-facts/v1"
ACCEPTANCE_FACTS_SCHEMA_V2 = "booley.simulation-acceptance-facts/v2"
ACCEPTANCE_FACTS_SCHEMAS = frozenset({ACCEPTANCE_FACTS_SCHEMA_V1, ACCEPTANCE_FACTS_SCHEMA_V2})
