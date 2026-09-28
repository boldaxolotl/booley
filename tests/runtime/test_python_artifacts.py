"""Python artifact relocation policy tests."""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from booley.runtime.python_artifacts import python_artifact_root, relocate_python_artifacts


def test_relocation_copies_environment_and_sets_exact_destinations(tmp_path: Path) -> None:
    original = {"KEEP": "yes", "PYTEST_ADDOPTS": "--lf -q"}

    relocated = relocate_python_artifacts(original, tmp_path / "cache root")

    assert original == {"KEEP": "yes", "PYTEST_ADDOPTS": "--lf -q"}
    assert relocated["KEEP"] == "yes"
    assert relocated["PYTHONPYCACHEPREFIX"] == str(tmp_path / "cache root" / "bytecode")
    assert shlex.split(relocated["PYTEST_ADDOPTS"]) == [
        "--lf",
        "-q",
        "-o",
        f"cache_dir={tmp_path / 'cache root' / 'pytest'}",
    ]
    assert not (tmp_path / "cache root").exists()


def test_relocation_can_disable_bytecode_for_embedded_python(tmp_path: Path) -> None:
    relocated = relocate_python_artifacts(
        {
            "PYTHONPYCACHEPREFIX": "inherited",
            "PYTHONDONTWRITEBYTECODE": "0",
        },
        tmp_path,
        write_bytecode=False,
    )

    assert "PYTHONPYCACHEPREFIX" not in relocated
    assert relocated["PYTHONDONTWRITEBYTECODE"] == "1"
    assert f"cache_dir={tmp_path / 'pytest'}" in relocated["PYTEST_ADDOPTS"]


@pytest.mark.parametrize(
    "existing",
    [
        "-o cache_dir=old --ff",
        "-o=cache_dir=old --nf",
        "--override-ini cache_dir=old --cache-clear",
        "--override-ini=cache_dir=old --cache-show",
        "-o addopts=verbose -o cache_dir='old path' --stepwise",
    ],
)
def test_relocation_replaces_every_cache_override_and_preserves_options(
    tmp_path: Path, existing: str
) -> None:
    cache_root = tmp_path / "new cache"

    relocated = relocate_python_artifacts({"PYTEST_ADDOPTS": existing}, cache_root)
    tokens = shlex.split(relocated["PYTEST_ADDOPTS"])

    assert tokens[-2:] == ["-o", f"cache_dir={cache_root / 'pytest'}"]
    assert sum(token.startswith("cache_dir=") for token in tokens) == 1
    assert all("cache_dir=old" not in token for token in tokens)


def test_relocation_is_idempotent(tmp_path: Path) -> None:
    once = relocate_python_artifacts({"PYTEST_ADDOPTS": "--lf"}, tmp_path)

    assert relocate_python_artifacts(once, tmp_path) == once


def test_scoped_pytest_cache_keeps_shared_bytecode_root(tmp_path: Path) -> None:
    first = relocate_python_artifacts({}, tmp_path, pytest_scope="first/run")
    second = relocate_python_artifacts({}, tmp_path, pytest_scope="second/run")

    assert first["PYTHONPYCACHEPREFIX"] == second["PYTHONPYCACHEPREFIX"]
    assert first["PYTEST_ADDOPTS"] != second["PYTEST_ADDOPTS"]
    assert "first/run" not in first["PYTEST_ADDOPTS"]


def test_malformed_pytest_addopts_does_not_break_non_pytest_children(tmp_path: Path) -> None:
    relocated = relocate_python_artifacts({"PYTEST_ADDOPTS": "'unterminated"}, tmp_path)

    assert shlex.split(relocated["PYTEST_ADDOPTS"]) == [
        "-o",
        f"cache_dir={tmp_path / 'pytest'}",
    ]


def test_artifact_root_prefers_runtime_then_project_then_fallback(tmp_path: Path) -> None:
    fallback = tmp_path / "fallback"
    project = tmp_path / "project-data"
    runtime = tmp_path / "runtime"

    assert python_artifact_root({}, fallback) == fallback
    assert python_artifact_root({"BOOLEY_PROJECT_DIR": str(project)}, fallback) == (
        project / ".runtime" / "python-artifacts"
    )
    assert (
        python_artifact_root(
            {"BOOLEY_PROJECT_DIR": str(project), "BOOLEY_RUNTIME_DIR": str(runtime)}, fallback
        )
        == runtime / "python-artifacts"
    )
