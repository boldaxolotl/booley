"""The persistence capability that defers the ever_failed latch to judged evidence."""

from __future__ import annotations

from booley.criteria.state import AtomicStateFile, JudgedEvidenceProjection
from booley.goals.state_store import GoalStatePersistence


class _Marked:
    def projects_judged_evidence(self) -> None:
        """Marker."""


class _ReadOnly:
    def read(self, path):
        raise NotImplementedError


def test_only_the_marker_satisfies_judged_evidence_projection():
    assert isinstance(_Marked(), JudgedEvidenceProjection)
    assert not isinstance(AtomicStateFile(), JudgedEvidenceProjection)
    assert not isinstance(_ReadOnly(), JudgedEvidenceProjection)
    assert issubclass(GoalStatePersistence, JudgedEvidenceProjection)
