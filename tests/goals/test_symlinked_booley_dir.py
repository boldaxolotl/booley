"""Goal Mode and SIM baselines under a symlinked Main Booley Directory entry (#2193)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.core.booley_dir import reset_cache
from booley.goals.apply_effects import FileEffect
from booley.goals.proposal_errors import ProposalError
from booley.goals.store import GoalStore
from tests.conftest import symlink_or_skip
from tests.goals.conftest import LINT_KEY, enter_goals, git

pytestmark = pytest.mark.timeout(120)


def _link_main_booley_dir(layout: SimpleNamespace, tmp_path: Path, spelling: str) -> None:
    """Move the Main Booley Directory elsewhere and leave ``<main>/.booley`` as a link to it."""
    store = tmp_path / "linked-store"
    shutil.move(str(layout.control), store)
    symlink_or_skip(layout.control, store, target_is_directory=True)
    if spelling == "physical":
        layout.worktree = store / "worktrees" / "wt"
    elif spelling == "physical-store":
        # The Main Booley Directory as a resolved selection (``BOOLEY_DIR``) names it.
        layout.control = store
    reset_cache()


@pytest.fixture(params=["lexical", "physical", "physical-store"])
def linked(layout, tmp_path, request):
    _link_main_booley_dir(layout, tmp_path, request.param)
    return layout


@pytest.fixture(params=["lexical", "physical-store"])
def lexical(layout, tmp_path, request):
    _link_main_booley_dir(layout, tmp_path, request.param)
    return layout


# --- AC2 -------------------------------------------------------------------


@pytest.mark.parametrize("publication", ["stealth", "committed"])
def test_goal_entered_through_symlinked_booley_dir_stays_fresh_and_finishes(linked, publication):
    from booley.goals.status import build_status
    from tests.goals import test_finish
    from tests.goals.test_status import publish

    if publication == "committed":
        test_finish.publication_layout(linked)
    else:
        linked.record = enter_goals(linked, [{"family": "LINT", "target": "top"}])
        publish(linked)
        git(linked.main, "config", "user.name", "Goal Test")
        git(linked.main, "config", "user.email", "goal@test.invalid")
    store = GoalStore(linked.control)
    for work_dir in (linked.worktree, linked.worktree.resolve()):
        view = build_status(store, store.load(linked.record.id), work_dir=work_dir)
        assert [(row.key, row.status) for row in view.goals] == [(LINT_KEY, "met")]
    result = test_finish.finish_goal(test_finish.request(linked), test_finish.environment(linked))
    assert result["status"] == "finished"


# --- AC4 -------------------------------------------------------------------


def test_protected_checkout_label_names_the_worktree_booley_dir(lexical):
    from booley.goals.input_identity import tests_label
    from booley.goals.protected_inputs import ProtectedInputRoots, RootKind

    record = enter_goals(lexical, [{"family": "LINT", "target": "top"}])
    roots = ProtectedInputRoots(lexical.worktree, lexical.control)
    assert roots.with_input_paths(record.input_paths).checkout_label == (
        RootKind.WORKTREE,
        ".booley",
    )
    assert tests_label(record.input_paths) == ".booley/tests.toml"


@pytest.mark.parametrize("spelling", ["plain", "lexical", "physical"])
def test_waiver_destination_under_symlinked_booley_dir_is_protected(layout, tmp_path, spelling):
    from booley.goals.waiver_policy import policy_location
    from booley.mcp.goal_waivers import GoalWaiverService

    if spelling != "plain":
        _link_main_booley_dir(layout, tmp_path, spelling)
    record = enter_goals(layout, [{"family": "LINT", "target": "top"}])
    (layout.worktree / ".booley/booley.toml").write_text(
        "[coverage.waivers]\nanchor = 'project_data_repository'\ndirectory = 'hooks'\n"
    )
    config, anchor = policy_location(layout.worktree)
    assert config is not None
    with pytest.raises(ProposalError, match="waiver destination is a protected input"):
        GoalWaiverService(layout.control, record)._refuse_protected(
            anchor / config.directory, record
        )


# --- AC1 / AC7 ---------------------------------------------------------------


def test_file_effect_follows_only_the_booley_dir_entry(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    store = tmp_path / "store"
    (store / "goals").mkdir(parents=True)
    entry = project / ".booley"
    symlink_or_skip(entry, store, target_is_directory=True)
    effect = FileEffect(entry / "goals/state.json", None, b"after", "state")
    with pytest.raises(ProposalError, match="symlink destination"):
        effect.apply()
    effect.apply(booley_dir=entry)
    assert (store / "goals/state.json").read_bytes() == b"after"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    symlink_or_skip(store / "goals/linked", elsewhere, target_is_directory=True)
    below = FileEffect(entry / "goals/linked/state.json", None, b"after", "state")
    with pytest.raises(ProposalError, match="symlink destination"):
        below.apply(booley_dir=entry)
    alias = tmp_path / "alias"
    symlink_or_skip(alias, project, target_is_directory=True)
    above = FileEffect(alias / ".booley/goals/other.json", None, b"after", "state")
    with pytest.raises(ProposalError, match="symlink destination"):
        above.apply(booley_dir=alias / ".booley")
    assert not (elsewhere / "state.json").exists()
    assert not (store / "goals/other.json").exists()


def test_goal_change_applies_and_recovers_through_symlinked_booley_dir(lexical):
    from booley.goals.apply import recover
    from tests.goals import test_proposals

    env = test_proposals.setup_cycle(lexical)
    test_proposals.publish(lexical)
    view = test_proposals.proposal(lexical, env)
    revision = GoalStore(lexical.control).load(lexical.record.id).revision

    def crash(at):
        if at == "intent":
            raise test_proposals.Crash(at)

    with pytest.raises(test_proposals.Crash):
        test_proposals.approve(lexical, replace(env, on_boundary=crash), view.proposal.id)
    recover(env.store, lexical.record.id, env)
    assert GoalStore(lexical.control).load(lexical.record.id).revision == revision + 1


# --- AC3 -------------------------------------------------------------------


def test_sim_baseline_runs_through_symlinked_booley_dir(linked):
    from tests.goals import test_sim_baseline as sim

    hook = linked.worktree / ".booley/hooks/build.py"
    hook.parent.mkdir()
    hook.write_bytes(b"# entry program\n")
    core = linked.worktree / ".booley/cores/bonus.core"
    core.parent.mkdir()
    original = b"CAPI=2:\nname: ::bonus:0\ntargets:\n  extra: {toplevel: before}\n"
    core.write_bytes(original)
    record = enter_goals(linked, [{"family": "SIM", "target": "top"}])
    hook.write_bytes(b"# subsequent edit\n")
    core.write_bytes(original.replace(b"before", b"edited"))
    (linked.worktree / "rtl.v").write_bytes(b"module edited; endmodule\n")
    mode = SimpleNamespace(**vars(linked), record=record, launches=[])
    checkouts = []

    def execute(checkout, target, runs, output):
        checkouts.append(checkout)
        assert (checkout / "rtl.v").read_bytes() == b"module top; endmodule\n"
        assert (checkout / ".booley/hooks/build.py").read_bytes() == b"# entry program\n"
        assert (checkout / ".booley/cores/bonus.core").read_bytes() == original
        return sim._execute(checkout, target, runs, output)

    rows = sim._complete(mode, execute)
    assert rows[0]["grade"] == "pass"
    assert rows[0]["proof"]["base_sha"] == record.base_sha
    assert not checkouts[0].exists()


def test_interrupted_sim_baseline_checkout_is_reaped_through_symlinked_booley_dir(lexical):
    from booley.goals.sim_baseline_store import read_document, write_document
    from booley.runtime import job_records
    from booley.runtime.timefmt import utc_now_rfc3339
    from tests.goals import test_sim_baseline as sim

    record = enter_goals(lexical, [{"family": "SIM", "target": "top"}])
    mode = SimpleNamespace(**vars(lexical), record=record, launches=[])
    path, raw = sim._schedule(mode)
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=30)
    owned = lexical.control / f".baseline-wt-{process.pid}-{record.base_sha[:7]}"
    git(lexical.main, "worktree", "add", "--detach", "-q", str(owned), record.base_sha)
    write_document(
        path,
        raw
        | {
            "owned_checkout": str(owned),
            "scratch_parent": str(owned.parent),
            "owned_paired_checkout": None,
        },
    )
    job_records.write_record(
        job_records.JobRecord(
            raw["job_id"],
            "sim_baseline",
            utc_now_rfc3339(),
            60,
            pid=process.pid,
            status=job_records.STATUS_FAILED,
        ),
        Path(raw["jobs_root"]),
    )
    sim._schedule(mode)
    assert not owned.exists()
    assert read_document(path)["status"] == "unavailable"


def test_restore_entry_inputs_follows_only_the_booley_dir_entry(lexical):
    from booley.mcp import sim_baseline_execution as execution
    from tests.goals.test_sim_baseline_boundaries import _restore_request

    record = enter_goals(lexical, [{"family": "SIM", "target": "top"}])
    mode = SimpleNamespace(**vars(lexical), record=record, launches=[])
    from tests.goals import test_sim_baseline as sim

    _path, raw = sim._schedule(mode)
    root = lexical.control / ".baseline-wt-restore"
    root.mkdir()
    execution.restore_entry_inputs(
        root, _restore_request(mode, raw, files={"inputs/entry.txt": "00"})
    )
    assert (root / "inputs/entry.txt").read_bytes() == b"\x00"
    (root / "outside").mkdir()
    symlink_or_skip(root / "linked", root / "outside", target_is_directory=True)
    with pytest.raises(ValueError, match="crosses a link"):
        execution.restore_entry_inputs(
            root, _restore_request(mode, raw, files={"linked/file": "00"})
        )
