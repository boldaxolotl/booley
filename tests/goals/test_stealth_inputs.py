"""Stealth projected cores use the same paths in baseline and Finish views."""

from types import SimpleNamespace

import pytest

from booley.evidence.acceptance import PairedProjectBaseline
from booley.flows.baseline_worktree import baseline_worktree
from booley.fusesoc.core_projection import reconcile_projected_cores
from booley.goals.entry import EntryEnvironment, EntryRequest, enter_goal_mode
from booley.goals.finish import finish_goal
from booley.goals.model import parse_goal_args
from booley.goals.target_surface import target_surface_fingerprint
from booley.mcp.goal_generated_inputs import _committed_only_inputs
from booley.runtime.project_dir import reset_cache
from booley.runtime.project_worktree_pairing import pair_project_worktree
from booley.targets.catalog import TargetCatalog
from tests.goals.conftest import git
from tests.goals.stealth_support import CORE
from tests.goals.test_finish import environment, request
from tests.goals.test_status import publish


@pytest.fixture
def stealth(tmp_path):
    main = tmp_path / "stealth" / "main"
    main.mkdir(parents=True)
    git(main, "init", "-q", "-b", "main")
    (main / "rtl.v").write_text("module top; endmodule\n")
    git(main, "add", ".")
    git(main, "commit", "-qm", "RTL")
    (main / ".git/info/exclude").write_text("/.booley_project\n/.booley-projected-*.core\n")
    control = main / ".booley_project"
    (control / "cores/constraints").mkdir(parents=True)
    (control / "booley.toml").write_text("[stealth]\nenabled=true\n")
    (control / ".gitignore").write_text("worktrees/\ngoals/\n.baseline-wt-*/\n.runtime/\n")
    (control / "cores/top.core").write_text(CORE)
    (control / "cores/constraints/top.sdc").write_text("create_clock -period 10 [get_ports clk]\n")
    git(control, "init", "-q", "-b", "main")
    git(control, "add", ".")
    git(control, "commit", "-qm", "Project")
    worktree = control / "worktrees/wt"
    git(main, "worktree", "add", "--detach", str(worktree), "HEAD")
    assert pair_project_worktree(main, worktree, "wt", source=control)
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("BOOLEY_PROJECT_DIR", str(control))
        reset_cache()
        reconcile_projected_cores(worktree)
        record = enter_goal_mode(
            EntryRequest(
                worktree, "stealth", parse_goal_args([{"family": "lint", "target": "top"}])
            ),
            EntryEnvironment(control),
        ).record
        layout = SimpleNamespace(main=main, control=control, worktree=worktree, record=record)
        yield layout
        reset_cache()


@pytest.mark.parametrize("paired", [True, False])
def test_baseline_resolves_projected_rtl_and_sibling_constraint(stealth, paired):
    root = stealth.worktree if paired else stealth.main
    policy = (
        PairedProjectBaseline.entry_pinned(stealth.record.paired_project_base_sha)
        if paired
        else PairedProjectBaseline.standalone()
    )
    with baseline_worktree(root, stealth.record.base_sha, paired_project=policy) as baseline:
        catalog = TargetCatalog.build(baseline)
        inputs = catalog.inspect(catalog.select("top")).inputs
        paths = {item.path for item in inputs}
        assert paths == {"rtl.v", ".booley_project/cores/constraints/top.sdc"}
        assert all((baseline / item.path).is_file() for item in inputs)


def test_user_pairing_pins_project_head_at_goal_entry(stealth):
    assert stealth.record.paired_project_base_sha == git(stealth.control, "rev-parse", "HEAD")
    assert (
        git(stealth.worktree / ".booley_project", "symbolic-ref", "--short", "HEAD")
        == "booley-worktree/wt"
    )


@pytest.mark.timeout(120)
def test_finish_consumes_committed_projected_rtl_and_sibling_constraint(stealth):
    publish(stealth)
    assert finish_goal(request(stealth), environment(stealth))["status"] == "finished"


PROGRAM_CORE = """CAPI=2:
name: ::gen:0
filesets:
  rtl: {files: [rtl.v], file_type: verilogSource}
scripts:
  prepare: {cmd: [sh, scripts/prepare.sh]}
generators:
  build: {command: scripts/generate.py}
targets:
  gen: {filesets: [rtl], toplevel: top, default_tool: verilator}
"""


@pytest.fixture
def stealth_programs(tmp_path, monkeypatch):
    """A standalone Stealth Project whose projected core names root-relative programs."""
    root = tmp_path / "programs"
    (root / "scripts").mkdir(parents=True)
    (root / "rtl.v").write_text("module top; endmodule\n")
    (root / "scripts/prepare.sh").write_text("echo prepare\n")
    (root / "scripts/generate.py").write_text("print('generate')\n")
    control = root / ".booley_project"
    (control / "cores").mkdir(parents=True)
    (control / "booley.toml").write_text("[stealth]\nenabled=true\n")
    (control / "cores/gen.core").write_text(PROGRAM_CORE)
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(control))
    reset_cache()
    reconcile_projected_cores(root)
    yield root
    reset_cache()


def test_projected_core_programs_resolve_from_the_project_root(stealth_programs):
    root = stealth_programs
    before = target_surface_fingerprint(root, "gen")

    assert {"scripts/prepare.sh", "scripts/generate.py"} <= set(before["files"])
    (root / "scripts/prepare.sh").write_text("echo changed\n")
    assert target_surface_fingerprint(root, "gen")["digest"] != before["digest"]


def test_projected_core_programs_never_receive_a_generated_exemption(stealth_programs):
    root = stealth_programs
    catalog = TargetCatalog.build(root)

    committed_only = _committed_only_inputs(root, catalog, "gen")

    assert (root / "scripts/prepare.sh").resolve() in committed_only
    assert (root / "scripts/generate.py").resolve() in committed_only


def test_target_less_parent_ignore_does_not_hide_paired_constraint(stealth):
    from booley.goals.freshness import DEFAULT_RESOLVERS
    from booley.goals.generated_artifacts import generated_artifact_paths

    constraint = stealth.worktree / ".booley_project/cores/constraints/top.sdc"
    assert constraint.resolve() not in generated_artifact_paths(stealth.worktree)
    before = DEFAULT_RESOLVERS.fingerprint(stealth.worktree, target=None)
    constraint.write_text("create_clock -period 20 [get_ports clk]\n")
    assert DEFAULT_RESOLVERS.fingerprint(stealth.worktree, target=None)["rtl"] != before["rtl"]


def test_target_less_nonversioned_hidden_constraint_follows_literal_definition(layout):
    from booley.goals.freshness import DEFAULT_RESOLVERS
    from booley.goals.generated_artifacts import generated_artifact_paths
    from booley.goals.input_view import is_always_committed_input
    from tests.goals.conftest import enter_goals

    layout.record = enter_goals(
        layout, [{"family": "review", "review": "rtl_bugs", "verdict": "done"}]
    )
    constraint = layout.worktree / ".booley_project/top.sdc"
    constraint.write_text("constraint first\n")
    core = layout.worktree / "top.core"
    core.write_text(
        core.read_text().replace(
            "files: [rtl.v]", "files: [rtl.v, {.booley_project/top.sdc: {file_type: user}}]"
        )
    )
    git(layout.worktree, "add", "top.core")
    git(layout.worktree, "commit", "-qm", "hidden nonversioned constraint")
    assert constraint.resolve() in generated_artifact_paths(layout.worktree)
    before = DEFAULT_RESOLVERS.fingerprint(layout.worktree, target=None)
    constraint.write_text("constraint second\n")
    assert DEFAULT_RESOLVERS.fingerprint(layout.worktree, target=None) == before
    assert is_always_committed_input(constraint)
    assert (
        ".booley_project/top.sdc"
        in DEFAULT_RESOLVERS.fingerprint(layout.worktree, target="top")["rtl"]["files"]
    )


def test_presentation_epoch_maps_real_projected_catalog_and_worktree_alias(stealth, tmp_path):
    from booley.goals.generated_artifacts import artifact_epoch, presentation_exclusions
    from booley.goals.input_view import committed_view, select_inputs
    from tests.conftest import symlink_or_skip

    project = stealth.worktree / ".booley_project"
    core = project / "cores/data.core"
    core.write_bytes(
        b"CAPI=2:\nname: ::data:0\nfilesets:\n"
        b"  data: {files: [artifact.hex], file_type: user}\n"
        b"targets:\n  data: {filesets: [data], toplevel: top}\n"
    )
    git(project, "add", "cores/data.core")
    git(project, "commit", "-qm", "projected unused data target")
    reconcile_projected_cores(stealth.worktree)
    artifact = stealth.worktree / "artifact.hex"
    artifact.write_bytes(b"generated bytes\n")
    with (stealth.main / ".git/info/exclude").open("a") as stream:
        stream.write("artifact.hex\n")
    alias = tmp_path / "worktree-alias"
    symlink_or_skip(alias, stealth.worktree, target_is_directory=True)
    selection = select_inputs(stealth.record, alias.resolve())
    with committed_view(
        selection, stealth.record, control_project=stealth.control, baseline=True
    ) as view:
        before = artifact_epoch(view, stealth.record, baseline=True)
    with committed_view(selection, stealth.record, control_project=stealth.control) as view:
        catalog = TargetCatalog.build(view.root)
        data = catalog.inspect(catalog.select("data"))
        assert [item.path for item in data.inputs] == ["artifact.hex"]
        assert view.mapped(alias / "artifact.hex") == view.root / "artifact.hex"
        after = artifact_epoch(view, stealth.record, baseline=False)
    assert ("artifact.hex", artifact.resolve()) in after.inputs
    assert artifact.resolve() in after.eligible and artifact.resolve() not in after.tracked
    labels, omitted = presentation_exclusions(selection.rtl, (before, after), frozenset())
    assert labels == (frozenset(), frozenset({"artifact.hex"}))
    assert "artifact.hex" in omitted and str(artifact.resolve()) in omitted
