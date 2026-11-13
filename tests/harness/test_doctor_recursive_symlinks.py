"""Doctor groups ancestor links while retaining a complete verbose path report."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from booley.harness import doctor
from tests.conftest import symlink_or_skip
from tests.docker_cli_guard import route_docker_helper_through_subprocess_run
from tests.harness.test_doctor import (
    _patch_environment,
    _write_project,
)


@pytest.fixture(autouse=True)
def _docker_helper_uses_subprocess_run(monkeypatch):
    """This module fakes the Docker CLI through ``subprocess.run``."""
    route_docker_helper_through_subprocess_run(monkeypatch)


pytestmark = pytest.mark.usefixtures("isolated_git_attributes")


def _ancestor_links(root: Path, count: int) -> list[str]:
    paths = []
    for index in range(count):
        library = root / f"lib{index:02d}"
        library.mkdir()
        symlink_or_skip(library / "self", "..", target_is_directory=True)
        paths.append(f"lib{index:02d}/self")
    return paths


@pytest.mark.parametrize("verbose,concise", [(False, False), (True, False), (False, True)])
def test_public_doctor_groups_54_links_with_complete_verbose_paths(
    tmp_path, monkeypatch, capsys, verbose, concise
):
    booley_dir = _write_project(tmp_path)
    _patch_environment(monkeypatch, tmp_path, booley_dir)
    paths = _ancestor_links(tmp_path, 54)

    result = doctor.run_doctor_result(
        argparse.Namespace(deep=False, verbose=verbose, concise=concise), tmp_path
    )

    output = capsys.readouterr().out
    failures = [f for f in result.findings if "FuseSoC recursive symlink" in f.message]
    assert len(failures) == 1
    assert result.counts["fail"] == 1
    assert result.exit_code == 1
    assert not result.clean
    message = failures[0].message
    assert "54 links" in message
    assert str(tmp_path.resolve()) in message
    assert ", ".join(paths[:3]) in message
    assert "+51 more" in message
    assert "--verbose" in message
    assert paths[3] not in message
    assert output.count("  ✗  FuseSoC recursive symlinks") == 1
    assert "each link's containing directory" in failures[0].fix
    for path in paths:
        assert (path in output) is (verbose or path in paths[:3])
