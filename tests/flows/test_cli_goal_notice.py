"""Human CLI admission discloses Goal recording without changing Goal authority."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from booley.flows.lint.flow import LintFlow
from booley.goals.model import GoalState
from booley.goals.store import GoalStore
from booley.runtime.endpoint_execution import EndpointOutcome
from booley.specialists.reviewer import ReviewerSpecialist
from tests.flows.report_headline import assert_paired_headline
from tests.goals import conftest as goal_fixtures

layout = goal_fixtures.layout
goal_mode = goal_fixtures.goal_mode


def _snapshot(root: Path) -> dict[str, bytes]:
    """Include lock files and Git metadata: advisory lookup may create nothing."""
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("endpoint_kind", ["builtin", "custom", "specialist"])
@pytest.mark.parametrize("diagnostic", [False, True])
def test_active_cli_warns_without_writing_goal(
    goal_mode, monkeypatch, capsys, endpoint_kind, diagnostic
):
    from booley.runtime import runtime_context
    from tests.flows.test_flow_base import EchoFlow

    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    if endpoint_kind == "builtin":
        endpoint = LintFlow()
        monkeypatch.setattr(endpoint, "_run", EndpointOutcome)
        argv = ["-C", str(goal_mode.worktree), "--target", "top", "--quiet"]
    elif endpoint_kind == "custom":
        endpoint = EchoFlow()
        monkeypatch.setattr(endpoint, "_run", EndpointOutcome)
        argv = ["-C", str(goal_mode.worktree), "--target", "top"]
    else:
        endpoint = ReviewerSpecialist()
        monkeypatch.setattr(endpoint, "_run", EndpointOutcome)
        argv = [
            "-C",
            str(goal_mode.worktree),
            "--category",
            "rtl",
            "--focus",
            "bugs",
            "--scope",
            "rtl.v",
        ]
    if diagnostic:
        argv.append("--diagnostic")
    before = _snapshot(goal_mode.control / "goals")
    metadata = _snapshot(goal_mode.main / ".git")
    result = endpoint.execute_cli(argv)
    assert result.exit_code == 0
    output = capsys.readouterr()
    assert f"not recorded for active Goal {goal_mode.record.id}" in output.err
    assert "MCP calls with work_dir" in output.err
    assert _snapshot(goal_mode.control / "goals") == before
    assert _snapshot(goal_mode.main / ".git") == metadata


def test_unbound_cli_report_omits_goal_headline(goal_mode, monkeypatch):
    from booley.runtime import runtime_context

    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    events = []
    monkeypatch.setattr("booley.flows.endpoint_reporting._write_display_event", events.append)
    flow = LintFlow()
    monkeypatch.setattr(
        flow,
        "_run",
        lambda: EndpointOutcome(
            criterion_key="lint_clean_top", criterion_met=True, detail={"warnings": 2}
        ),
    )
    result = flow.execute_cli(["-C", str(goal_mode.worktree), "--target", "top", "--quiet"])
    assert result.exit_code == 0
    report = assert_paired_headline(flow.args.report_dir, "lint", None)
    assert report["detail"]["warnings"] == 2

    completion = next(event for event in events if event["type"] == "endpoint_end")
    assert "criterion_key" not in completion and "criterion_met" not in completion


def test_sibling_and_finished_goal_do_not_warn(goal_mode, monkeypatch, capsys):
    from booley.runtime import runtime_context

    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    flow = LintFlow()
    monkeypatch.setattr(flow, "_run", EndpointOutcome)
    flow.execute_cli(["-C", str(goal_mode.main), "--target", "top", "--quiet"])
    assert "not recorded for active Goal" not in capsys.readouterr().err
    store = GoalStore(goal_mode.control)
    with store.record_lock(goal_mode.record.id) as lock:
        store.save(lock, replace(store.load(goal_mode.record.id), state=GoalState.ABANDONED))
    flow.execute_cli(["-C", str(goal_mode.worktree), "--target", "top", "--quiet"])
    assert "not recorded for active Goal" not in capsys.readouterr().err


@pytest.mark.parametrize("selection", ["nested", "project", "paired"])
def test_normalized_module_selection_warns(goal_mode, monkeypatch, capsys, selection, tmp_path):
    from tests.goals.conftest import install_paired_project

    if selection == "paired":
        install_paired_project(goal_mode, tmp_path)
    nested = goal_mode.worktree / "nested"
    nested.mkdir()
    monkeypatch.chdir(nested)
    args = ["--category", "rtl", "--focus", "bugs", "--scope", "rtl.v"]
    if selection == "project":
        args += ["--project", str(goal_mode.worktree)]
    ReviewerSpecialist().parse_args(args)
    assert goal_mode.record.id in capsys.readouterr().err


@pytest.mark.parametrize(
    "store_kind", ["foreign", "corrupt-foreign", "external", "ambiguous", "cached"]
)
def test_store_selection_matches_actual_worktree(
    goal_mode, monkeypatch, capsys, tmp_path, store_kind
):
    import shutil

    from booley.harness.cli_goal_notice import warn_cli_goal_recording
    from booley.runtime.booley_dir import reset_cache, resolve_booley_dir

    other = tmp_path / "other-store"
    other.mkdir()
    (other / "booley.toml").write_text("[project]\n")
    if store_kind not in {"foreign", "corrupt-foreign"}:
        shutil.copytree(goal_mode.control / "goals", other / "goals")
    if store_kind == "corrupt-foreign":
        from booley.goals.paths import record_paths

        broken = record_paths(other, goal_mode.record.id).record_file
        broken.parent.mkdir(parents=True)
        broken.write_text("broken")
    if store_kind in {"external", "cached"}:
        shutil.rmtree(goal_mode.control / "goals")
    monkeypatch.setenv("BOOLEY_DIR", str(other))
    reset_cache()
    if store_kind == "cached":
        resolve_booley_dir()
        monkeypatch.delenv("BOOLEY_DIR")
        monkeypatch.chdir(goal_mode.worktree)
    before = _snapshot(tmp_path)
    warn_cli_goal_recording(goal_mode.worktree)
    output = capsys.readouterr().err
    assert _snapshot(tmp_path) == before
    if store_kind == "corrupt-foreign":
        assert "Other Goal store advisory unavailable" in output
        assert output.count("WARN:") == 1
    if store_kind == "ambiguous":
        assert "advisory unavailable" in output
        assert "several Booley Directories" in output
    else:
        assert f"not recorded for active Goal {goal_mode.record.id}" in output


@pytest.mark.parametrize("damage", ["corrupt", "identity"])
def test_lookup_failures_are_read_only_advisories(goal_mode, capsys, damage):
    from booley.goals.paths import record_paths
    from booley.goals.store import REPOSITORY_ID_FILE
    from booley.harness.cli_goal_notice import warn_cli_goal_recording

    if damage == "corrupt":
        record_paths(goal_mode.control, goal_mode.record.id).record_file.write_text("broken")
    else:
        (goal_mode.main / ".git" / REPOSITORY_ID_FILE).unlink()
    before = _snapshot(goal_mode.main)
    warn_cli_goal_recording(goal_mode.worktree)
    assert "advisory unavailable" in capsys.readouterr().err
    assert _snapshot(goal_mode.main) == before


def test_non_git_project_is_silent_and_read_only(tmp_path, capsys):
    from booley.harness.cli_goal_notice import warn_cli_goal_recording

    (tmp_path / ".booley").mkdir()
    before = _snapshot(tmp_path)
    warn_cli_goal_recording(tmp_path)
    assert capsys.readouterr().err == ""
    assert _snapshot(tmp_path) == before


def test_notice_precedes_preparation_rejection(goal_mode, monkeypatch, capsys):
    from booley.runtime import runtime_context

    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: False)
    result = ReviewerSpecialist().execute_cli(
        ["-C", str(goal_mode.worktree), "--category", "rtl", "--focus", "bugs", "--scope", "rtl.v"]
    )
    assert result.exit_code == 2
    assert goal_mode.record.id in capsys.readouterr().err


def test_bound_mcp_flow_keeps_goal_evidence(goal_mode, monkeypatch):
    from booley.goals.flow_execution import GoalFlowExecution
    from booley.runtime import runtime_context
    from tests.goals.conftest import bind

    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    flow = LintFlow()

    def run():
        flow.set_criterion("lint_clean_top", True)
        return EndpointOutcome(detail={"warnings": 0})

    monkeypatch.setattr(flow, "_run", run)
    result = flow.execute(
        flow.request_type(target="top", work_dir=goal_mode.worktree),
        adapter=GoalFlowExecution(bind(goal_mode)),
    )
    assert result.exit_code == 0, result.outcome.report_text
    assert result.outcome.criterion_key == "lint_clean_top"
    assert result.outcome.criterion_met is True
    assert_paired_headline(flow.args.report_dir, "lint", ("lint_clean_top", True))


@pytest.mark.timeout(120)
def test_bound_done_reviewer_preserves_completion_policy_and_receipt(layout, monkeypatch):
    from booley.flows.endpoint_report_criteria import freeze
    from tests.goals.conftest import enter_goals
    from tests.goals.test_mcp_routing import _goal_reviewer_endpoint
    from tests.goals.test_reviewer_proposals import review_issues

    layout.record = enter_goals(
        layout, [{"family": "REVIEW", "review": "rtl_bugs", "verdict": "done", "paths": ["rtl.v"]}]
    )
    for replay in (False, True):
        endpoint, provider = _goal_reviewer_endpoint(layout, monkeypatch)
        provider.return_value.output = json.dumps({"issues": review_issues(True)})
        freeze(endpoint)
        result = endpoint._run()
        assert result.exit_code == 0, result.report_text
        assert "explicit human approval" not in result.report_text
        report = json.loads(endpoint.write_report(result).read_text())
        assert report["criterion_key"] == ("" if replay else "review_rtl_bugs_done")
        # Replay reuses evidence but does not claim a new evaluation.
        assert report["criterion_met"] is (None if replay else True)
        assert provider.call_count == (0 if replay else 1)


def test_configured_main_store_outvotes_foreign_environment(
    goal_mode, monkeypatch, capsys, tmp_path
):
    import shutil

    from booley.harness.cli_goal_notice import warn_cli_goal_recording
    from booley.runtime.booley_dir import reset_cache

    configured = goal_mode.main / "booley-data"
    configured.mkdir()
    shutil.move(str(goal_mode.control / "goals"), configured / "goals")
    (configured / "booley.toml").write_text("[project]\n")
    (goal_mode.main / "booley.toml").write_text("[project]\ndir='booley-data'\n")
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    monkeypatch.setenv("BOOLEY_DIR", str(foreign))
    reset_cache()
    before = _snapshot(tmp_path)
    warn_cli_goal_recording(goal_mode.worktree)
    assert goal_mode.record.id in capsys.readouterr().err
    assert _snapshot(tmp_path) == before


def test_git_checkout_without_goal_identity_is_silent(layout, capsys):
    from booley.harness.cli_goal_notice import warn_cli_goal_recording

    before = _snapshot(layout.main)
    warn_cli_goal_recording(layout.worktree)
    assert capsys.readouterr().err == ""
    assert _snapshot(layout.main) == before


@pytest.mark.parametrize("failure", ["source-checkout", "git-timeout"])
def test_advisory_boundary_failures_do_not_abort(goal_mode, monkeypatch, capsys, failure):
    import subprocess

    from booley.core.checkout_role import SourceCheckoutProjectError
    from booley.harness import cli_goal_notice

    error = (
        SourceCheckoutProjectError("source checkout cannot be a Project")
        if failure == "source-checkout"
        else subprocess.TimeoutExpired("git", 30)
    )

    def fail(*_args):
        raise error

    boundary = (
        "resolve_authoritative_booley_dir" if failure == "source-checkout" else "primary_checkout"
    )
    monkeypatch.setattr(cli_goal_notice, boundary, fail)
    before = _snapshot(goal_mode.main)
    cli_goal_notice.warn_cli_goal_recording(goal_mode.worktree)
    assert "advisory unavailable" in capsys.readouterr().err
    assert _snapshot(goal_mode.main) == before


def test_notice_does_not_select_cwd_directory_for_later_execution(
    goal_mode, monkeypatch, tmp_path
):
    from booley.core import booley_dir
    from booley.harness.cli_goal_notice import warn_cli_goal_recording
    from booley.runtime.booley_dir import reset_cache, resolve_booley_dir

    foreign = tmp_path / "cwd-project"
    (foreign / ".booley").mkdir(parents=True)
    monkeypatch.chdir(foreign)
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    reset_cache()
    expected = resolve_booley_dir(goal_mode.worktree)
    reset_cache()
    warn_cli_goal_recording(goal_mode.worktree)
    assert booley_dir._cache is None
    assert resolve_booley_dir(goal_mode.worktree) == expected


def test_foreign_store_does_not_require_identity_for_unrelated_checkout(
    goal_mode, monkeypatch, tmp_path, capsys
):
    from booley.harness.cli_goal_notice import warn_cli_goal_recording
    from booley.runtime.booley_dir import reset_cache

    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    goal_fixtures.git(unrelated, "init", "-q")
    monkeypatch.setenv("BOOLEY_DIR", str(goal_mode.control))
    reset_cache()
    before = _snapshot(tmp_path)
    warn_cli_goal_recording(unrelated)
    assert capsys.readouterr().err == ""
    assert _snapshot(tmp_path) == before


def test_malformed_identity_is_not_a_foreign_store(goal_mode, capsys):
    import shutil

    from booley.goals.store import REPOSITORY_ID_FILE
    from booley.harness.cli_goal_notice import warn_cli_goal_recording

    shutil.rmtree(goal_mode.control / "goals")
    (goal_mode.main / ".git" / REPOSITORY_ID_FILE).write_text("malformed")
    before = _snapshot(goal_mode.main)
    warn_cli_goal_recording(goal_mode.worktree)
    assert "advisory unavailable" in capsys.readouterr().err
    assert _snapshot(goal_mode.main) == before
