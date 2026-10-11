"""Results for another target cannot become optional evidence."""

from pathlib import Path

from booley.criteria.state import DevelopmentState


def _state(tmp_path: Path, criteria: dict[str, bool]) -> DevelopmentState:
    state = DevelopmentState.load(tmp_path / "booley_state.json")
    state.slug = "issue-88-f46"
    state.init_criteria({**criteria, "_report_submitted": True}, strict=True)
    state.set_criterion("_report_submitted", True)
    return state


def test_wrong_target_result_is_not_stored_as_optional_evidence(tmp_path: Path) -> None:
    """A wrong-Target Flow result must be rejected instead of hidden as optional."""
    state = _state(tmp_path, {"lint_clean_lint_uart": True})

    state.set_criterion("lint_clean_sim_uart", True, detail={"warnings": 0})

    assert "lint_clean_sim_uart" not in state.criteria
