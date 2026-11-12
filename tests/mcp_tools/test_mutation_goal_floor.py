"""Goal change policy orders an omitted Mutation floor as ceil(0.8 x total).

``tests/goals`` is excluded from the default collection (``collect_ignore``), so
these policy-level tests keep the implicit-floor contract in the broad suite.
"""

from __future__ import annotations

import pytest

from booley.goals.change_policy import translate_after, validate_kind
from booley.goals.changes import ChangeKind
from booley.goals.proposals import ProposalError

MUTATION = {"family": "MUTATION", "target": "top", "scope": ["rtl.v"], "total": 10}


def relax(before_floor: int | None, after_floor: int | None) -> None:
    """Validate a relax from one Mutation floor to another (``None`` omits the floor)."""
    before = {**MUTATION} if before_floor is None else {**MUTATION, "min_detected": before_floor}
    after = {**MUTATION} if after_floor is None else {**MUTATION, "min_detected": after_floor}
    validate_kind(ChangeKind.RELAX, translate_after(before), translate_after(after))


def test_relax_to_an_explicit_floor_above_the_implicit_eighty_percent_is_refused() -> None:
    # The omitted floor is ceil(0.8 x 10) = 8: an explicit 9 or 10 would tighten it.
    relax(None, 8)  # equal to the implicit floor: accepted
    for stricter in (9, 10):
        with pytest.raises(ProposalError, match="weaken"):
            relax(None, stricter)


def test_relax_to_an_omitted_floor_orders_as_the_implicit_eighty_percent() -> None:
    # Dropping an explicit 9 or 10 orders as the implicit 8, which is a relaxation.
    relax(9, None)
    relax(10, None)
    # An explicit floor between the implicit 8 and the old implicit 10 is stricter than omitted.
    with pytest.raises(ProposalError, match="weaken"):
        relax(None, 9)
