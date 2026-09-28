"""Python artifact relocation policy tests."""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from booley.runtime.python_artifacts import relocate_python_artifacts


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
