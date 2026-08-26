"""Canonical criterion dependency categories for invalidation and freshness."""

from __future__ import annotations

CATEGORY_RTL = "rtl"
CATEGORY_TB = "tb"

_FINGERPRINT_CATEGORIES: tuple[tuple[tuple[str, ...], frozenset[str]], ...] = (
    (("review_rtl_",), frozenset({CATEGORY_RTL})),
    (("review_tb_",), frozenset({CATEGORY_TB})),
    (("mutation_score_", "coverage_"), frozenset({CATEGORY_RTL, CATEGORY_TB, "campaign"})),
    (
        ("cycle_count_",),
        frozenset({CATEGORY_RTL, CATEGORY_TB, "campaign", "workload"}),
    ),
    (("sim_", "elab_"), frozenset({CATEGORY_RTL, CATEGORY_TB})),
    (
        ("lint_", "synthesis_", "fpga_impl_", "elaborate_standalone"),
        frozenset({CATEGORY_RTL}),
    ),
)


def verification_fingerprint_categories(key: str) -> set[str]:
    """Return source categories that a passing verification Criterion depends on."""
    for prefixes, categories in _FINGERPRINT_CATEGORIES:
        if key.startswith(prefixes):
            return set(categories)
    return set()
