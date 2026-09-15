"""Checkout-local build identity, independent of Flow execution and Runtime."""

from pathlib import Path

import pytest
from tests.conftest import symlink_or_skip

from booley.core.build_paths import work_root_for
from booley.core.checkout_role import SourceCheckoutProjectError


@pytest.mark.parametrize(
    ("config", "leaf"),
    [
        ("sim_smoke", "sim_smoke"),
        ("::ip#sim/a", "ip_sim_a"),
        ("___", "config"),
        ("", "config"),
        ("?!", "config"),
        ("toy/target", "toy_target"),
        ("toy:target", "toy_target"),
    ],
)
def test_config_directory_identity(tmp_path: Path, config: str, leaf: str) -> None:
    assert work_root_for(tmp_path, "sim", config) == (
        tmp_path / ".booley_project" / ".runtime" / "edalize" / "sim" / leaf
    )
    assert not (tmp_path / ".booley_project").exists()


def test_variant_and_flow_directories_remain_distinct(tmp_path: Path) -> None:
    variants = ("", "trace", "coverage", "sweep", "baseline", "trace-run")
    paths = [work_root_for(tmp_path, "sim", "cfg", variant=v) for v in variants]
    assert [path.name for path in paths] == [
        "cfg",
        "cfg-trace",
        "cfg-coverage",
        "cfg-sweep",
        "cfg-baseline",
        "cfg-trace-run",
    ]
    assert len(set(paths)) == len(variants)
    assert work_root_for(tmp_path, "lint", "cfg") not in paths


def test_canonical_checkout_ignores_ambient_project_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / "booley.toml").write_text('[project]\ndir = "custom-state"\n')
    (checkout / "custom-state").mkdir()
    ambient = tmp_path / "ambient-state"
    ambient.mkdir()
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(ambient))
    monkeypatch.chdir(tmp_path)
    expected = checkout / ".booley_project" / ".runtime" / "edalize" / "sim" / "cfg"
    assert work_root_for("checkout", "sim", "cfg") == expected
    alias = tmp_path / "alias"
    symlink_or_skip(alias, checkout, target_is_directory=True)
    assert work_root_for(alias, "sim", "cfg") == expected
    assert work_root_for(tmp_path / "another-checkout", "sim", "cfg") != expected
    assert not expected.exists()


@pytest.mark.parametrize("selection", ["root", "nested", "symlink", "linked-worktree"])
def test_source_checkout_rejected_without_creating_state(tmp_path: Path, selection: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text("[tool.booley]\nsource_checkout = true\n")
    selected = source
    if selection == "nested":
        selected = source / "nested"
        selected.mkdir()
    elif selection == "symlink":
        selected = tmp_path / "alias"
        symlink_or_skip(selected, source, target_is_directory=True)
    elif selection == "linked-worktree":
        (source / ".git").write_text("gitdir: /unused/main/.git/worktrees/source\n")
    with pytest.raises(SourceCheckoutProjectError, match="cannot be initialized"):
        work_root_for(selected, "sim", "cfg", variant="trace")
    assert not (source / ".booley_project").exists()
    assert not (selected / ".booley_project").exists()


def test_nested_independent_checkout_keeps_its_own_builds(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.booley]\nsource_checkout = true\n")
    nested = tmp_path / "independent"
    (nested / ".git").mkdir(parents=True)
    assert work_root_for(nested, "sim", "cfg") == (
        nested / ".booley_project" / ".runtime" / "edalize" / "sim" / "cfg"
    )
