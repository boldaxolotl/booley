"""DevelopmentState.save() delegates to a persistence strategy the state carries."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from booley.criteria import state as state_module
from booley.criteria.state import AtomicStateFile, CriterionEntry, DevelopmentState

FROZEN_NOW = "2026-10-05T12:00:00Z"

# Bytes main's DevelopmentState.save() wrote for _fixed_state() before the seam.
GOLDEN_STATE_FILE = """{
  "slug": "golden",
  "ticket_type": "feature",
  "strict_criteria": true,
  "criteria": {
    "lint_clean_lite": {
      "met": true,
      "mandatory": true,
      "updated_at": "2026-10-05T11:00:00Z",
      "detail": {
        "warnings": 0
      }
    },
    "sim_pass_lite": {
      "met": false,
      "mandatory": false
    }
  },
  "category_map": {
    "lint_clean_lite": "rtl"
  },
  "all_mandatory_met": true,
  "timeline": [
    {
      "endpoint": "lint",
      "exit_code": 0
    }
  ],
  "acceptance_transactions": [
    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  ],
  "authorized_zero_mandatory_basis_id": "",
  "last_updated": "2026-10-05T12:00:00Z",
  "work_dir": "/work",
  "flow_key_aliases": {
    "sim_pass": [
      "sim_pass_lite"
    ]
  }
}"""


class RecordingPersistence:
    """Strategy double that records every state it is asked to save."""

    def __init__(self) -> None:
        self.saved: list[DevelopmentState] = []

    def save(self, state: DevelopmentState) -> None:
        self.saved.append(state)


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(state_module, "utc_now_rfc3339", lambda: FROZEN_NOW)


def _fixed_state(path: Path, persistence: RecordingPersistence | None = None) -> DevelopmentState:
    state = DevelopmentState.load(path, persistence)
    state.slug = "golden"
    state.ticket_type = "feature"
    state.strict_criteria = True
    state.criteria = {
        "lint_clean_lite": CriterionEntry(
            met=True, updated_at="2026-10-05T11:00:00Z", detail={"warnings": 0}
        ),
        "sim_pass_lite": CriterionEntry(mandatory=False),
    }
    state.category_map = {"lint_clean_lite": "rtl"}
    state.flow_key_aliases = {"sim_pass": ["sim_pass_lite"]}
    state.timeline = [{"endpoint": "lint", "exit_code": 0}]
    state.work_dir = "/work"
    state.acceptance_transactions = ["a" * 64]
    return state


class TestDefaultStrategy:
    def test_save_writes_the_bytes_main_wrote(self, tmp_path: Path) -> None:
        path = tmp_path / "booley_state.json"

        _fixed_state(path).save()

        assert path.read_text(encoding="utf-8") == GOLDEN_STATE_FILE
        assert not path.with_suffix(".tmp").exists()

    def test_explicit_default_strategy_writes_the_same_bytes(self, tmp_path: Path) -> None:
        path = tmp_path / "booley_state.json"
        state = _fixed_state(path)

        AtomicStateFile().save(state)

        assert path.read_text(encoding="utf-8") == GOLDEN_STATE_FILE
        assert state.last_updated == FROZEN_NOW

    def test_state_without_file_path_saves_nothing(self, tmp_path: Path) -> None:
        state = DevelopmentState(slug="memory")

        state.save()

        assert state.last_updated == ""
        assert list(tmp_path.iterdir()) == []


class TestInjectedStrategy:
    @pytest.mark.parametrize("existing", ["missing", "valid", "corrupted"])
    def test_load_attaches_strategy_on_every_return_path(
        self, tmp_path: Path, existing: str
    ) -> None:
        path = tmp_path / "booley_state.json"
        if existing == "valid":
            path.write_text(GOLDEN_STATE_FILE, encoding="utf-8")
        elif existing == "corrupted":
            path.write_text("{not json", encoding="utf-8")
        before = path.read_bytes() if path.exists() else None
        persistence = RecordingPersistence()

        state = DevelopmentState.load(path, persistence)
        state.save()

        assert persistence.saved == [state]
        assert state._file_path == path
        # The strategy owns the write: the default file write did not run.
        assert (path.read_bytes() if path.exists() else None) == before

    def test_load_without_strategy_uses_default(self, tmp_path: Path) -> None:
        state = DevelopmentState.load(tmp_path / "booley_state.json")

        assert state._persistence is None

    def test_constructed_state_saves_through_its_strategy(self) -> None:
        persistence = RecordingPersistence()
        state = DevelopmentState(_persistence=persistence)

        state.save()

        assert persistence.saved == [state]

    def test_deepcopy_saves_through_the_same_strategy_object(self, tmp_path: Path) -> None:
        persistence = RecordingPersistence()
        state = _fixed_state(tmp_path / "booley_state.json", persistence)

        shadow = copy.deepcopy(state)
        shadow.criteria["sim_pass_lite"].met = True
        shadow.save()

        assert shadow._persistence is persistence
        assert persistence.saved == [shadow]
        assert shadow.criteria is not state.criteria
        assert state.criteria["sim_pass_lite"].met is False
        assert shadow._file_path == state._file_path

    def test_deepcopy_without_strategy_keeps_the_default(self, tmp_path: Path) -> None:
        path = tmp_path / "booley_state.json"
        shadow = copy.deepcopy(_fixed_state(path))

        shadow.save()

        assert shadow._persistence is None
        assert path.read_text(encoding="utf-8") == GOLDEN_STATE_FILE

    def test_strategy_is_not_serialized_compared_or_shown(self, tmp_path: Path) -> None:
        path = tmp_path / "booley_state.json"
        persistence = RecordingPersistence()
        with_strategy = _fixed_state(path, persistence)
        without_strategy = _fixed_state(path)

        assert with_strategy._to_dict() == without_strategy._to_dict()
        assert "persistence" not in json.dumps(with_strategy._to_dict())
        assert with_strategy == without_strategy
        assert repr(with_strategy) == repr(without_strategy)
