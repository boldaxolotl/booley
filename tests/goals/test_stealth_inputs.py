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
