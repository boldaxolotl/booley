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
    """Strategy double that records every state it observes and is asked to save."""

    def __init__(self) -> None:
        self.loaded_states: list[DevelopmentState] = []
        # (file path, serialized state) as the hook saw them, before any mutation.
        self.loaded_views: list[tuple[Path | None, dict]] = []
        self.saved: list[DevelopmentState] = []

    def loaded(self, state: DevelopmentState) -> None:
        self.loaded_states.append(state)
        self.loaded_views.append((state._file_path, copy.deepcopy(state.to_dict())))

    def save(self, state: DevelopmentState) -> None:
        self.saved.append(state)


class FalsyPersistence(RecordingPersistence):
    """A valid strategy whose truth value is False must still be used."""

    def __len__(self) -> int:
        return 0


class BaselinePersistence:
    """Strategy that stashes the as-loaded state on the instance, as a merge would."""

    def loaded(self, state: DevelopmentState) -> None:
        state.persistence_baseline = copy.deepcopy(state.to_dict())  # type: ignore[attr-defined]

    def save(self, state: DevelopmentState) -> None:
        return


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
        assert persistence.loaded_states == [state]
        assert state._file_path == path
        # The strategy owns the write: the default file write did not run.
        assert (path.read_bytes() if path.exists() else None) == before

    def test_load_without_strategy_uses_default(self, tmp_path: Path) -> None:
        state = DevelopmentState.load(tmp_path / "booley_state.json")

        assert state._persistence is None

    def test_in_memory_state_is_observed_and_saves_through_its_strategy(self) -> None:
        persistence = RecordingPersistence()
        state = DevelopmentState.in_memory(persistence)

        state.save()

        assert persistence.loaded_states == [state]
        assert persistence.loaded_views[0][0] is None
        assert persistence.saved == [state]

    def test_in_memory_without_strategy_saves_nothing(self, tmp_path: Path) -> None:
        state = DevelopmentState.in_memory()

        state.save()

        assert state._persistence is None
        assert state.last_updated == ""
        assert list(tmp_path.iterdir()) == []

    def test_falsy_strategy_is_used_not_replaced_by_default(self, tmp_path: Path) -> None:
        path = tmp_path / "booley_state.json"
        persistence = FalsyPersistence()
        assert not persistence

        state = _fixed_state(path, persistence)
        state.save()

        assert persistence.loaded_states == [state]
        assert persistence.saved == [state]
        assert not path.exists()

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

        assert with_strategy.to_dict() == without_strategy.to_dict()
        assert "persistence" not in json.dumps(with_strategy.to_dict())
        assert with_strategy == without_strategy
        assert repr(with_strategy) == repr(without_strategy)


class TestLoadedHook:
    @pytest.mark.parametrize("existing", ["missing", "valid", "corrupted"])
    def test_load_calls_hook_once_with_the_parsed_unmutated_state(
        self, tmp_path: Path, existing: str
    ) -> None:
        path = tmp_path / "booley_state.json"
        if existing == "valid":
            path.write_text(GOLDEN_STATE_FILE, encoding="utf-8")
        elif existing == "corrupted":
            path.write_text("{not json", encoding="utf-8")
        persistence = RecordingPersistence()

        state = DevelopmentState.load(path, persistence)
        as_loaded = copy.deepcopy(state.to_dict())
        state.slug = "mutated"
        state.save()

        assert persistence.loaded_states == [state]
        hook_path, hook_view = persistence.loaded_views[0]
        assert hook_path == path
        assert hook_view == as_loaded
        assert hook_view["slug"] == ("golden" if existing == "valid" else "")

    def test_baseline_stashed_on_the_state_survives_deepcopy_independently(
        self, tmp_path: Path
    ) -> None:
        path = tmp_path / "booley_state.json"
        path.write_text(GOLDEN_STATE_FILE, encoding="utf-8")
        persistence = BaselinePersistence()
        state = DevelopmentState.load(path, persistence)

        shadow = copy.deepcopy(state)

        baseline = state.persistence_baseline  # type: ignore[attr-defined]
        shadow_baseline = shadow.persistence_baseline  # type: ignore[attr-defined]
        assert shadow_baseline == baseline
        assert shadow_baseline is not baseline
        assert shadow_baseline["criteria"] is not baseline["criteria"]
        assert shadow._persistence is persistence
        # The stash is not state data: not serialized, compared, or shown.
        assert "persistence_baseline" not in state.to_dict()
        assert state == DevelopmentState.load(path)
        assert "persistence_baseline" not in repr(state)

    def test_default_strategy_hook_leaves_golden_bytes_unchanged(self, tmp_path: Path) -> None:
        path = tmp_path / "booley_state.json"
        path.write_text(GOLDEN_STATE_FILE, encoding="utf-8")
        state = DevelopmentState.load(path)

        AtomicStateFile().loaded(state)
        state.save()

        assert path.read_text(encoding="utf-8") == GOLDEN_STATE_FILE
