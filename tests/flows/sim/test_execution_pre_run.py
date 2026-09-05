"""Pre-Run Commands live behind the Project-scoped execution seam."""

from pathlib import Path
from types import SimpleNamespace
from typing import cast

from booley.config.project_config import load_test_configuration_field, render_test_selector
from booley.flows.sim.execution.pre_run import run_pre_run_commands
from booley.targets.target import TargetHandle


def _project(root: Path, marker: str) -> TargetHandle:
    project = root / ".booley_project"
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
        outcome = run_pre_run_commands(
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


def test_test_registry_is_scoped_to_each_checkout(tmp_path: Path) -> None:
    current = tmp_path / "current"
    baseline = tmp_path / "baseline"
    for root, name, selector in (
        (current, "now", "+test={name}"),
        (baseline, "then", "+case={index}"),
    ):
        project = root / ".booley_project"
        project.mkdir(parents=True)
        (project / "tests.toml").write_text(
            f'[sim]\ntests = ["{name}"]\nselect = "{selector}"\n',
            encoding="utf-8",
        )

    assert load_test_configuration_field(current, "tests") == {"sim": ["now"]}
    assert load_test_configuration_field(baseline, "tests") == {"sim": ["then"]}
    assert render_test_selector("sim", 0, "now", work_dir=current) == "+test=now"
    assert render_test_selector("sim", 0, "then", work_dir=baseline) == "+case=0"
