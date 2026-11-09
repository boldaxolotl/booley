"""Contracts for the Docker stable project-dependency seam."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from booley.runtime import docker_base_contract

_EXPORTER = Path("src/booley/data/docker/export_project_dependencies.py")


def test_exporter_preserves_pep508_requirements_and_markers(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    requirements = tmp_path / "requirements.txt"
    pyproject.write_text(
        """\
[project]
dependencies = [
  "plain>=1.2",
  "platform-pkg==3.4; sys_platform == 'win32'",
  "extra-pkg[feature]~=5.0; python_version >= '3.13'",
]
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(_EXPORTER), str(pyproject), str(requirements)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert requirements.read_text(encoding="utf-8").splitlines() == [
        "plain>=1.2",
        "platform-pkg==3.4; sys_platform == 'win32'",
        "extra-pkg[feature]~=5.0; python_version >= '3.13'",
    ]


def test_exporter_rejects_a_non_string_dependency(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    requirements = tmp_path / "requirements.txt"
    pyproject.write_text("[project]\ndependencies = [42]\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(_EXPORTER), str(pyproject), str(requirements)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "project.dependencies must be a list of strings" in result.stderr
    assert not requirements.exists()


def test_exporter_rejects_a_non_table_project(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    requirements = tmp_path / "requirements.txt"
    pyproject.write_text('project = "invalid"\n', encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(_EXPORTER), str(pyproject), str(requirements)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "project must be a table" in result.stderr
    assert not requirements.exists()


_PYPROJECT = """\
[project]
dependencies = ["plain>=1.2", "marker-pkg==3; sys_platform == 'win32'"]

[project.optional-dependencies]
dev = ["pytest"]
"""


def test_contract_hashes_the_exporter_output(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    requirements = tmp_path / "requirements.txt"
    pyproject.write_text(_PYPROJECT, encoding="utf-8")
    subprocess.run([sys.executable, str(_EXPORTER), str(pyproject), str(requirements)], check=True)

    assert (
        docker_base_contract.contract_contents("pyproject.toml", pyproject.read_bytes())
        == requirements.read_bytes()
    )
    assert docker_base_contract.contract_contents("other.txt", b"raw") == b"raw"


def test_exporter_writes_lf_requirements_in_process(tmp_path: Path) -> None:
    from booley.data.docker import export_project_dependencies as exporter

    pyproject = tmp_path / "pyproject.toml"
    requirements = tmp_path / "requirements.txt"
    pyproject.write_text(_PYPROJECT, encoding="utf-8")

    exporter.export_project_dependencies(pyproject, requirements)

    assert requirements.read_bytes() == b"plain>=1.2\nmarker-pkg==3; sys_platform == 'win32'\n"


def test_exporter_main_reports_unreadable_input_in_process(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from booley.data.docker import export_project_dependencies as exporter

    missing = tmp_path / "missing.toml"
    destination = tmp_path / "requirements.txt"
    monkeypatch.setattr(sys, "argv", ["exporter", str(missing), str(destination)])

    assert exporter.main() == 2
    assert "error:" in capsys.readouterr().err
    assert not destination.exists()
