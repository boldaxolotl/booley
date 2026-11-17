"""Pre-Sim Commands live behind the Project-scoped execution seam."""

from pathlib import Path
from types import SimpleNamespace
from typing import cast

from booley.flows.sim.execution.pre_sim import run_pre_sim_commands
from booley.targets.domain import TargetHandle


def _project(root: Path, marker: str) -> TargetHandle:
    project = root / ".booley"
    project.mkdir(parents=True)
    (project / "booley.toml").write_text(
        f'[flows.sim]\npre_run_commands = ["printf {marker} > hook.txt"]\nrun_cwd = "."\n',
        encoding="utf-8",
    )
    return cast(
        TargetHandle,
        SimpleNamespace(project_root=root.resolve(), selector="sim"),
    )


def test_each_target_handle_resolves_its_own_project_configuration(tmp_path: Path) -> None:
    first_root = tmp_path / "current"
    second_root = tmp_path / "baseline"
    first = _project(first_root, "current")
    second = _project(second_root, "baseline")

    for handle in (first, second):
        outcome = run_pre_sim_commands(
            handle,
            test_names=("smoke",),
            build_root=handle.project_root / "build",
            eda_tool="icarus",
            timeout_s=5,
        )
        assert outcome is not None
        assert outcome.status == "passed"

    assert (first_root / "hook.txt").read_text(encoding="utf-8") == "current"
    assert (second_root / "hook.txt").read_text(encoding="utf-8") == "baseline"
