"""Entering Goal Mode: refusals, the Goal Branch, protected inputs, and crash recovery.

Every test drives real Git in a throwaway repository laid out like a Booley
Project: the control Project directory ``<main>/.booley_project`` (excluded
from Git, as ``init`` does), and a linked worktree under its ``worktrees/``
carrying a copied ``.booley_project`` snapshot. Only the Target catalog is
faked, so entry never needs FuseSoC.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from booley.goals import entry as entry_module
from booley.goals.checkout import GoalCheckout
from booley.goals.entry import (
    EntryEnvironment,
    EntryRequest,
    GoalEntryError,
    enter_goal_mode,
    parse_entry_request,
)
from booley.goals.model import GoalState, parse_goal_args
from booley.goals.paths import record_paths
from booley.goals.store import GoalStore
from tests.goals.conftest import DATE, FakeCatalog, git, install_paired_project

BRANCH = f"goal/uart-fix-{DATE}"


class _Crash(BaseException):
    """Raised at a boundary to stop entry the way a killed process would (no rollback)."""


def _request(layout: SimpleNamespace, goals: list[dict[str, Any]] | None = None, **fields: Any):
    raw_goals = goals if goals is not None else [{"family": "lint", "target": "top"}]
    return EntryRequest(
        work_dir=fields.pop("work_dir", layout.worktree),
        slug=fields.pop("slug", "uart-fix"),
        goals=parse_goal_args(raw_goals),
        **fields,
    )


def _env(layout: SimpleNamespace, on_boundary: Callable[[str], None] | None = None):
    return EntryEnvironment(project_dir=layout.control, on_boundary=on_boundary)


def _store(layout: SimpleNamespace) -> GoalStore:
    return GoalStore(layout.control)


def _head_ref(path: Path) -> str:
    return git(path, "symbolic-ref", "-q", "HEAD")


def _branches(path: Path) -> set[str]:
    return set(git(path, "branch", "--format=%(refname:short)").split())


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_entry_creates_branch_record_and_unmet_strict_state(layout: SimpleNamespace) -> None:
    base = git(layout.worktree, "rev-parse", "HEAD")
    goals = [{"family": "lint", "target": "top"}, {"family": "sim", "target": "top"}]

    result = enter_goal_mode(_request(layout, goals), _env(layout))

    record = result.record
    assert record.state is GoalState.ACTIVE
    assert record.branch == BRANCH
    assert record.base_sha == base
    assert record.original_ref == "refs/heads/work"
    assert record.paired_project_base_sha is None
    assert _head_ref(layout.worktree) == f"refs/heads/{BRANCH}"
    state = json.loads(record_paths(layout.control, record.id).state_file.read_text("utf-8"))
    assert state["strict_criteria"] is True
    assert set(state["criteria"]) == {"lint_clean_top", "sim_pass_top"}
    assert all(not e["met"] and e["mandatory"] for e in state["criteria"].values())
    assert state["criteria"]["lint_clean_top"]["params"]["target"] == "top"
    assert "Pass `work_dir`" in result.render()
    assert _store(layout).active_for_worktree(layout.worktree) == record


def test_entry_works_from_a_detached_head(layout: SimpleNamespace) -> None:
    base = git(layout.worktree, "rev-parse", "HEAD")
    git(layout.worktree, "checkout", "-q", "--detach")

    record = enter_goal_mode(_request(layout), _env(layout)).record

    assert record.original_ref == base
    assert _head_ref(layout.worktree) == f"refs/heads/{BRANCH}"


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_missing_work_dir_is_refused() -> None:
    goals = [{"family": "lint", "target": "top"}]
    with pytest.raises(GoalEntryError, match="needs work_dir"):
        parse_entry_request({"slug": "uart-fix", "goals": goals}, session_key=None)
    with pytest.raises(GoalEntryError, match="needs work_dir"):
        parse_entry_request({"work_dir": "", "slug": "x", "goals": goals}, session_key=None)


def test_relative_work_dir_is_refused(layout: SimpleNamespace) -> None:
    with pytest.raises(GoalEntryError, match="absolute"):
        enter_goal_mode(_request(layout, work_dir=Path("wt")), _env(layout))


def test_main_checkout_is_refused(layout: SimpleNamespace) -> None:
    with pytest.raises(GoalEntryError, match="main checkout"):
        enter_goal_mode(_request(layout, work_dir=layout.main), _env(layout))


def test_dirty_tree_is_refused_without_a_record(layout: SimpleNamespace) -> None:
    (layout.worktree / "scratch.txt").write_text("wip\n", encoding="utf-8")

    with pytest.raises(GoalEntryError, match="uncommitted changes"):
        enter_goal_mode(_request(layout), _env(layout))

    assert _store(layout).list_records().records == ()
    assert _head_ref(layout.worktree) == "refs/heads/work"


def test_second_entry_on_an_active_worktree_is_refused(layout: SimpleNamespace) -> None:
    first = enter_goal_mode(_request(layout), _env(layout)).record

    with pytest.raises(GoalEntryError, match=f"already hosts Goal Mode {first.id}"):
        enter_goal_mode(_request(layout, slug="other"), _env(layout))


def test_project_dir_inside_the_worktree_is_refused(layout: SimpleNamespace) -> None:
    env = EntryEnvironment(project_dir=layout.worktree / ".booley_project")
    with pytest.raises(GoalEntryError, match="worktree's own copy"):
        enter_goal_mode(_request(layout), env)


@pytest.mark.parametrize(
    ("default_exists", "fields", "message"),
    [
        (True, {}, "has a default Goalset"),
        (True, {"default_skipped": True}, "skip_reason"),
        (True, {"default_skipped": True, "skip_reason": "  "}, "skip_reason"),
        (
            True,
            {"default_skipped": True, "skip_reason": "x", "goalsets_used": ("default",)},
            "both",
        ),
        (False, {"default_skipped": True, "skip_reason": "x"}, "no default Goalset"),
        (False, {"skip_reason": "x"}, "only accepted"),
        (False, {"goalsets_used": ("default",)}, "no default Goalset"),
    ],
)
def test_default_goalset_decision_must_be_consistent(
    layout: SimpleNamespace, default_exists: bool, fields: dict[str, Any], message: str
) -> None:
    if default_exists:
        (layout.control / "goalsets").mkdir()
        (layout.control / "goalsets" / "default.md").write_text("# default\n", encoding="utf-8")

    with pytest.raises(GoalEntryError, match=message):
        enter_goal_mode(_request(layout, **fields), _env(layout))


@pytest.mark.parametrize(
    "fields",
    [{"goalsets_used": ("default",)}, {"default_skipped": True, "skip_reason": "hotfix"}],
)
def test_default_goalset_applied_or_skipped_enters(
    layout: SimpleNamespace, fields: dict[str, Any]
) -> None:
    (layout.control / "goalsets").mkdir()
    (layout.control / "goalsets" / "default.md").write_text("# default\n", encoding="utf-8")

    record = enter_goal_mode(_request(layout, **fields), _env(layout)).record

    assert record.default_skipped is fields.get("default_skipped", False)
    assert record.skip_reason == fields.get("skip_reason")


def test_candidate_target_that_does_not_exist_warns(layout: SimpleNamespace) -> None:
    result = enter_goal_mode(
        _request(layout, [{"family": "lint", "target": "new_block"}]), _env(layout)
    )

    assert result.record.state is GoalState.ACTIVE
    assert any("'new_block', which does not exist yet" in w for w in result.warnings)


def test_baseline_target_missing_at_head_is_refused(layout: SimpleNamespace) -> None:
    goal = {
        "family": "synth",
        "target": "top",
        "baseline": "gone",
        "thresholds": {"area_increase_at_most": "5%"},
    }
    with pytest.raises(GoalEntryError, match="baseline Target 'gone'"):
        enter_goal_mode(_request(layout, [goal]), _env(layout))
    assert _store(layout).list_records().records == ()


def test_spec_review_needs_its_spec_file(layout: SimpleNamespace) -> None:
    missing = {"family": "review", "review": "rtl_spec", "verdict": "clean", "spec": "nope.md"}
    with pytest.raises(GoalEntryError, match=r"spec file 'nope\.md'"):
        enter_goal_mode(_request(layout, [missing]), _env(layout))

    present = {**missing, "spec": "docs/spec.md"}
    assert enter_goal_mode(_request(layout, [present]), _env(layout)).record.goals


def test_existing_goal_branch_name_is_refused(layout: SimpleNamespace) -> None:
    git(layout.main, "branch", BRANCH)

    with pytest.raises(GoalEntryError, match=f"branch {BRANCH} already exists"):
        enter_goal_mode(_request(layout), _env(layout))
    assert _store(layout).list_records().records == ()


def test_conflicting_goals_are_refused_before_any_write(layout: SimpleNamespace) -> None:
    goals = [
        {"family": "review", "review": "rtl_spec", "verdict": "clean", "spec": "a.md"},
        {"family": "review", "review": "rtl_spec", "verdict": "done", "spec": "b.md"},
    ]
    with pytest.raises(GoalEntryError, match="different spec files"):
        enter_goal_mode(_request(layout, goals), _env(layout))
    assert not (layout.control / "goals").exists()


# ---------------------------------------------------------------------------
# Failure and crash recovery (D15)
# ---------------------------------------------------------------------------

_BOUNDARIES = (
    "created",
    "branch_created",
    "checked_out",
    "pinned",
    "protected_saved",
    "state_saved",
)


def _crash_at(name: str) -> Callable[[str], None]:
    def hook(boundary: str) -> None:
        if boundary == name:
            raise _Crash(boundary)

    return hook


@pytest.mark.parametrize("boundary", _BOUNDARIES)
def test_a_crash_at_every_boundary_is_rolled_back_by_the_next_entry(
    layout: SimpleNamespace, boundary: str
) -> None:
    with pytest.raises(_Crash):
        enter_goal_mode(_request(layout), _env(layout, _crash_at(boundary)))
    (stale,) = _store(layout).list_records().records
    assert stale.state is GoalState.ENTERING

    result = enter_goal_mode(_request(layout, slug="retry"), _env(layout))

    assert result.recovered is not None and result.recovered.id == stale.id
    assert result.recovered.state is GoalState.FAILED
    assert result.recovered.failure == "entry was interrupted"
    assert BRANCH not in _branches(layout.main)  # rolled back, nothing left to name
    assert result.record.state is GoalState.ACTIVE
    assert result.record.base_sha == stale.base_sha


def test_a_crash_after_activation_leaves_an_active_goal_mode(layout: SimpleNamespace) -> None:
    with pytest.raises(_Crash):
        enter_goal_mode(_request(layout), _env(layout, _crash_at("active")))

    (record,) = _store(layout).list_records().records
    assert record.state is GoalState.ACTIVE
    with pytest.raises(GoalEntryError, match="already hosts"):
        enter_goal_mode(_request(layout, slug="retry"), _env(layout))


def test_a_failure_during_entry_rolls_back_at_once(layout: SimpleNamespace) -> None:
    def fail(boundary: str) -> None:
        if boundary == "pinned":
            raise RuntimeError("pinning exploded")

    with pytest.raises(GoalEntryError, match="pinning exploded") as caught:
        enter_goal_mode(_request(layout), _env(layout, fail))

    (record,) = _store(layout).list_records().records
    assert record.state is GoalState.FAILED
    assert record.failure == "entry failed: pinning exploded"
    assert "failed" in str(caught.value)
    assert _head_ref(layout.worktree) == "refs/heads/work"
    assert BRANCH not in _branches(layout.main)


def test_rollback_after_a_crash_back_to_a_detached_head(layout: SimpleNamespace) -> None:
    base = git(layout.worktree, "rev-parse", "HEAD")
    git(layout.worktree, "checkout", "-q", "--detach")
    with pytest.raises(_Crash):
        enter_goal_mode(_request(layout), _env(layout, _crash_at("checked_out")))

    with GoalStore(layout.control).worktree_lock(_identity(layout)):
        record = entry_module.rollback_entering(
            _store(layout),
            _store(layout).list_records().records[0].id,
            GoalCheckout(layout.worktree),
            "test",
        )

    assert record.state is GoalState.FAILED
    assert git(layout.worktree, "rev-parse", "HEAD") == base
    assert (
        subprocess.run(
            ["git", "symbolic-ref", "-q", "HEAD"], cwd=layout.worktree, check=False
        ).returncode
        == 1
    )


def _identity(layout: SimpleNamespace):
    identity = _store(layout).identify_worktree(layout.worktree)
    assert identity is not None
    return identity


def test_rollback_keeps_work_committed_on_the_goal_branch(layout: SimpleNamespace) -> None:
    with pytest.raises(_Crash):
        enter_goal_mode(_request(layout), _env(layout, _crash_at("checked_out")))
    (layout.worktree / "rtl.v").write_text("module top(); endmodule\n", encoding="utf-8")
    git(layout.worktree, "commit", "-q", "-am", "real work")

    result = enter_goal_mode(_request(layout, slug="retry"), _env(layout))

    assert result.recovered is not None
    assert "HEAD stays on Goal Branch" in (result.recovered.failure or "")
    assert BRANCH in _branches(layout.main)
    # The new Goal Mode stacks on the kept work.
    assert result.record.base_sha == git(layout.main, "rev-parse", BRANCH)


def test_rollback_keeps_a_goal_branch_that_moved_while_detached(layout: SimpleNamespace) -> None:
    with pytest.raises(_Crash):
        enter_goal_mode(_request(layout), _env(layout, _crash_at("checked_out")))
    git(layout.worktree, "checkout", "-q", "work")
    git(layout.main, "commit", "-q", "--allow-empty", "-m", "main moves")
    git(layout.main, "branch", "-f", BRANCH, "main")

    result = enter_goal_mode(_request(layout, slug="retry"), _env(layout))

    assert result.recovered is not None
    assert "has moved since entry; left in place" in (result.recovered.failure or "")
    assert BRANCH in _branches(layout.main)


def test_an_ambiguous_branch_from_a_crash_before_recording_it_is_kept(
    layout: SimpleNamespace,
) -> None:
    with pytest.raises(_Crash):
        enter_goal_mode(_request(layout), _env(layout, _crash_at("created")))
    (stale,) = _store(layout).list_records().records
    # The process created the branch, then died before saving branch_created.
    git(layout.worktree, "branch", stale.branch, stale.base_sha)

    result = enter_goal_mode(_request(layout, slug="retry"), _env(layout))

    assert result.recovered is not None
    assert "entry may have created it" in (result.recovered.failure or "")
    assert stale.branch in _branches(layout.main)


def test_recovered_record_is_reported_in_the_rendering(layout: SimpleNamespace) -> None:
    with pytest.raises(_Crash):
        enter_goal_mode(_request(layout), _env(layout, _crash_at("pinned")))

    text = enter_goal_mode(_request(layout, slug="retry"), _env(layout)).render()

    assert "Rolled back interrupted entry" in text


def test_shared_worktree_warning_names_other_sessions(layout: SimpleNamespace) -> None:
    env = replace(_env(layout), other_sessions=lambda _identity: ["pid:42:7"])

    result = enter_goal_mode(_request(layout), env)

    assert any("pid:42:7" in warning for warning in result.warnings)


# ---------------------------------------------------------------------------
# Baseline pinning with a paired Project repository (A3)
# ---------------------------------------------------------------------------


def test_relative_goal_pins_the_paired_project_revision_seen_at_entry(
    layout: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A paired Project repository without an upstream is pinned at its entry commit."""
    from contextlib import contextmanager

    from booley.evidence.acceptance import PairedProjectBaseline
    from booley.flows import baseline_worktree as baseline_module
    from booley.fusesoc import fusesoc_registry
    from booley.targets.catalog import TargetCatalog

    snapshot = install_paired_project(layout, tmp_path)
    paired_sha = git(snapshot, "rev-parse", "HEAD")

    calls: list[tuple[str, PairedProjectBaseline | None]] = []

    @contextmanager
    def fake_baseline(root: Path, ref: str, *, paired_project=None):
        calls.append((ref, paired_project))
        yield root

    monkeypatch.setattr(baseline_module, "baseline_worktree", fake_baseline)
    monkeypatch.setattr(
        TargetCatalog, "build", classmethod(lambda _c, root: FakeCatalog.build(root))
    )
    monkeypatch.setattr(
        fusesoc_registry, "resolve_target_handle", lambda handle, *, build_root: handle
    )
    family = entry_module.RecipeFamily(
        "synthesis_ok_", "Synthesis", lambda _resolved, selector: {"target": selector}
    )
    env = replace(_env(layout), recipe_families=(family,))
    goal = {"family": "synth", "target": "top", "thresholds": {"area_increase_at_most": "5%"}}

    record = enter_goal_mode(_request(layout, [goal]), env).record

    assert record.paired_project_base_sha == paired_sha
    assert calls == [(record.base_sha, PairedProjectBaseline.entry_pinned(paired_sha))]
    state = json.loads(record_paths(layout.control, record.id).state_file.read_text("utf-8"))
    params = state["criteria"]["synthesis_ok_top"]["params"]
    assert params["_baseline_ref"] == record.base_sha
    assert params["_recipe_snapshot"] == {"target": "top"}


def test_pin_errors_name_the_goal_mode_not_a_ticket(layout: SimpleNamespace) -> None:
    from booley.flows.baseline_pins import BaselinePinError, pin_cycle_count_baselines

    ctx = SimpleNamespace(work_dir=layout.worktree, base_sha="", recipe_freeze_root=layout.main)
    params = {"cycle_count_x": {"cycle_count_reduce_at_least": 5}}
    with pytest.raises(BaselinePinError) as caught:
        pin_cycle_count_baselines(ctx, params, wording=entry_module.GOAL_PIN_WORDING)

    assert "the Goal Mode has no base_sha" in str(caught.value)
    assert "ticket" not in str(caught.value)


# ---------------------------------------------------------------------------
# Concurrency (D15)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spelling", ["same", "alias"])
def test_a_concurrent_entry_on_one_worktree_is_refused(
    layout: SimpleNamespace, tmp_path: Path, spelling: str
) -> None:
    """The worktree lock is held for the whole entry, from every path spelling."""
    import threading

    from tests.conftest import symlink_or_skip

    second_dir = layout.worktree
    if spelling == "alias":
        second_dir = tmp_path / "alias"
        symlink_or_skip(second_dir, layout.worktree, target_is_directory=True)
    inside = threading.Event()
    release = threading.Event()

    def hold(boundary: str) -> None:
        if boundary == "pinned":
            inside.set()
            assert release.wait(20), "the test never released the first entry"

    outcome: dict[str, object] = {}

    def first() -> None:
        outcome["first"] = enter_goal_mode(_request(layout), _env(layout, hold)).record

    worker = threading.Thread(target=first)
    worker.start()
    try:
        assert inside.wait(20)
        env = replace(_env(layout), lock_timeout_s=0.3)
        with pytest.raises(GoalEntryError, match="stayed busy"):
            enter_goal_mode(_request(layout, slug="second", work_dir=second_dir), env)
    finally:
        release.set()
        worker.join(30)
    record = outcome["first"]
    assert getattr(record, "state", None) is GoalState.ACTIVE
    assert len(_store(layout).list_records().records) == 1


# ---------------------------------------------------------------------------
# Review round 1: rollback guard, paired tree, activation window, durability
# ---------------------------------------------------------------------------


def test_rollback_keeps_head_when_the_original_branch_moved(layout: SimpleNamespace) -> None:
    """Checking out a moved original branch could overwrite ignored files (B3)."""
    with pytest.raises(_Crash):
        enter_goal_mode(_request(layout), _env(layout, _crash_at("checked_out")))
    git(layout.main, "commit", "-q", "--allow-empty", "-m", "elsewhere")
    git(layout.main, "branch", "-f", "work", "main")

    result = enter_goal_mode(_request(layout, slug="retry"), _env(layout))

    assert result.recovered is not None
    failure = result.recovered.failure or ""
    assert "the original branch work moved to" in failure
    assert BRANCH in _branches(layout.main)
    # The new Goal Mode stacks on the Goal Branch HEAD was left on.
    assert result.record.original_ref == f"refs/heads/{BRANCH}"


def test_a_dirty_paired_project_checkout_is_refused(
    layout: SimpleNamespace, tmp_path: Path
) -> None:
    paired = install_paired_project(layout, tmp_path)
    (paired / "booley.toml").write_text("[project]\nname = 'wip'\n", encoding="utf-8")

    with pytest.raises(GoalEntryError, match="paired Project checkout has uncommitted"):
        enter_goal_mode(_request(layout), _env(layout))
    assert _store(layout).list_records().records == ()


def _during_pinning(action: Callable[[], None]) -> Callable[[str], None]:
    def hook(boundary: str) -> None:
        if boundary == "pinned":
            action()

    return hook


def test_a_protected_edit_while_pinning_is_refused_and_rolled_back(
    layout: SimpleNamespace,
) -> None:
    """The snapshot is taken before the freeze, so an edit during it is caught (B7)."""
    config = layout.control / "booley.toml"

    def edit() -> None:
        config.write_text("[project]\nname = 'sneaky'\n", encoding="utf-8")

    with pytest.raises(GoalEntryError, match="protected input differs"):
        enter_goal_mode(_request(layout), _env(layout, _during_pinning(edit)))

    (record,) = _store(layout).list_records().records
    assert record.state is GoalState.FAILED
    assert _head_ref(layout.worktree) == "refs/heads/work"
    assert BRANCH not in _branches(layout.main)


def test_a_commit_while_pinning_is_refused(layout: SimpleNamespace) -> None:
    def commit() -> None:
        git(layout.worktree, "commit", "-q", "--allow-empty", "-m", "sneaky")

    with pytest.raises(GoalEntryError, match="before activation"):
        enter_goal_mode(_request(layout), _env(layout, _during_pinning(commit)))

    (record,) = _store(layout).list_records().records
    assert record.state is GoalState.FAILED
    assert "HEAD stays on Goal Branch" in (record.failure or "")


def test_a_dirty_tree_while_pinning_is_refused(layout: SimpleNamespace) -> None:
    def scribble() -> None:
        (layout.worktree / "scratch.txt").write_text("wip\n", encoding="utf-8")

    with pytest.raises(GoalEntryError, match="uncommitted changes"):
        enter_goal_mode(_request(layout), _env(layout, _during_pinning(scribble)))

    (record,) = _store(layout).list_records().records
    assert record.state is GoalState.FAILED


def test_a_paired_project_commit_while_pinning_is_refused(
    layout: SimpleNamespace, tmp_path: Path
) -> None:
    paired = install_paired_project(layout, tmp_path)

    def commit() -> None:
        git(paired, "commit", "-q", "--allow-empty", "-m", "moved")

    with pytest.raises(GoalEntryError, match="paired Project checkout moved"):
        enter_goal_mode(_request(layout), _env(layout, _during_pinning(commit)))


def test_head_leaving_the_original_ref_before_checkout_is_refused(
    layout: SimpleNamespace,
) -> None:
    def detach(boundary: str) -> None:
        if boundary == "branch_created":
            git(layout.worktree, "checkout", "-q", "--detach")

    with pytest.raises(GoalEntryError, match="before checking out the Goal Branch"):
        enter_goal_mode(_request(layout), _env(layout, detach))


def test_the_state_file_is_flushed_before_activation(
    layout: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    flushed: list[Path] = []
    real = entry_module.fsync_directory
    monkeypatch.setattr(
        entry_module, "fsync_directory", lambda path: (flushed.append(path), real(path))
    )

    record = enter_goal_mode(_request(layout), _env(layout)).record

    assert record_paths(layout.control, record.id).state_file.parent in flushed
