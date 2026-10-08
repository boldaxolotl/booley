"""Stealth projected cores use the same paths in baseline and Finish views."""

from types import SimpleNamespace

import pytest

from booley.evidence.acceptance import PairedProjectBaseline
from booley.flows.baseline_worktree import baseline_worktree
from booley.fusesoc.core_projection import reconcile_projected_cores
from booley.goals.entry import EntryEnvironment, EntryRequest, enter_goal_mode
from booley.goals.finish import finish_goal
from booley.goals.model import parse_goal_args
from booley.runtime.project_dir import reset_cache
from booley.targets.catalog import TargetCatalog
from tests.goals.conftest import git
from tests.goals.test_finish import environment, request
from tests.goals.test_status import publish

CORE = """CAPI=2:
name: ::top:0
filesets:
  rtl: {files: [rtl.v], file_type: verilogSource}
  constraints:
    files: [.booley_project/cores/constraints/top.sdc]
    file_type: SDC
targets:
  top: {filesets: [rtl, constraints], toplevel: top, default_tool: verilator}
""".replace(" targets:", "targets:")


@pytest.fixture(scope="module")
def stealth(tmp_path_factory):
    main = tmp_path_factory.mktemp("stealth") / "main"
    main.mkdir()
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
    git(control, "worktree", "add", "--detach", str(worktree / ".booley_project"), "HEAD")
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


def test_finish_consumes_committed_projected_rtl_and_sibling_constraint(stealth):
    publish(stealth)
    assert finish_goal(request(stealth), environment(stealth))["status"] == "finished"
