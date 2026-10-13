"""Goal capabilities and the legacy evidence families that implement them."""

from collections.abc import Mapping

from booley.goals.model import REVIEW_KINDS, REVIEW_VERDICTS, GoalFamily

CRITERION_FOR_FAMILY: Mapping[GoalFamily, str] = {
    GoalFamily.LINT: "lint_clean",
    GoalFamily.SIM: "sim_pass",
    GoalFamily.ELAB: "elab_pass",
    GoalFamily.SYNTH: "synthesis_ok",
    GoalFamily.FPGA: "fpga_impl_ok",
    GoalFamily.CYCLE_COUNT: "cycle_count",
    GoalFamily.COVERAGE: "coverage",
    GoalFamily.MUTATION: "mutation_score",
}

# Older review specs have no separate kind/verdict field. The compatibility
# reader recognizes this finite registry rather than splitting arbitrary keys.
LEGACY_REVIEWS: Mapping[str, tuple[str, str]] = {
    f"review_{kind}_{verdict}": (kind, verdict)
    for kind in REVIEW_KINDS
    for verdict in REVIEW_VERDICTS
}


def evidence_goal_name(criterion: str) -> str | None:
    """Public catalog name for a supported evidence family, without opaque decoding."""
    for family, name in CRITERION_FOR_FAMILY.items():
        if criterion == name:
            return f"{family.name}:{{target}}"
    for kind in REVIEW_KINDS:
        if criterion == f"review_{kind}":
            return f"REVIEW:{kind}"
    review = LEGACY_REVIEWS.get(criterion)
    return None if review is None else f"REVIEW:{review[0]}"
