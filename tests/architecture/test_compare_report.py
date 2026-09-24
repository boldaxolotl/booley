from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_REPORT = Path(__file__).parent / "compare_report.py"


def _package(root: Path, *, dependency: str = "") -> Path:
    package = root / "booley"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    if dependency:
        (package / "source.py").write_text(f"import booley.{dependency}\n")
        (package / f"{dependency}.py").write_text("")
    return package


def _run(*arguments: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_REPORT), *arguments],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_root_mode_identifies_content_and_renders_explicit_empty_sections(
    tmp_path: Path,
) -> None:
    package = _package(tmp_path / "tree", dependency="target")

    result = _run(
        "--before-root",
        str(package),
        "--before-label",
        "working-copy-a",
        "--after-root",
        str(package),
        "--after-label",
        "working-copy-b",
    )

    assert result.returncode == 0, result.stderr
    assert "Before source: working-copy-a" in result.stdout
    assert "After source: working-copy-b" in result.stdout
    assert result.stdout.count("source digest: sha256:") == 2
    assert result.stdout.count("(none)") == 6
    assert "Before snapshot: 3 modules, 1 facts, 1 unique edges" in result.stdout


@pytest.mark.parametrize(
    "arguments",
    [
        ("--before-ref", "HEAD"),
        ("--before-root", "before", "--after-root", "after"),
        (
            "--before-ref",
            "HEAD",
            "--after-ref",
            "HEAD",
            "--before-root",
            "before",
            "--after-root",
            "after",
        ),
    ],
)
def test_rejects_incomplete_or_mixed_modes(arguments: tuple[str, ...]) -> None:
    result = _run(*arguments)

    assert result.returncode == 2


def test_root_mode_rejects_non_package_and_syntax_failure(tmp_path: Path) -> None:
    valid = _package(tmp_path / "valid")
    not_package = tmp_path / "not-package"
    not_package.mkdir()
    invalid = _package(tmp_path / "invalid")
    (invalid / "broken.py").write_text("def nope(:\n")

    missing = _run(
        "--before-root",
        str(valid),
        "--before-label",
        "valid",
        "--after-root",
        str(not_package),
        "--after-label",
        "missing",
    )
    syntax = _run(
        "--before-root",
        str(valid),
        "--before-label",
        "valid",
        "--after-root",
        str(invalid),
        "--after-label",
        "invalid",
    )

    assert missing.returncode == 1
    assert "source root is not a Python package" in missing.stderr
    assert syntax.returncode == 1
    assert "invalid syntax" in syntax.stderr


def test_ref_mode_resolves_tags_and_archives_only_exact_source_trees(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"], cwd=repository, check=True
    )
    package = _package(repository / "src", dependency="before")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "before"], cwd=repository, check=True)
    before = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repository, text=True
    ).strip()
    subprocess.run(["git", "tag", "before"], cwd=repository, check=True)
    (package / "source.py").write_text("import booley.after\n")
    (package / "after.py").write_text("")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "after"], cwd=repository, check=True)
    after = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repository, text=True
    ).strip()
    subprocess.run(["git", "tag", "-am", "after", "after"], cwd=repository, check=True)

    result = _run(
        "--before-ref",
        "before",
        "--after-ref",
        "after",
        "--repo-root",
        str(repository),
    )

    assert result.returncode == 0, result.stderr
    assert f"Before source: {before}" in result.stdout
    assert f"After source: {after}" in result.stdout
    assert "- booley.source -> booley.after" in result.stdout
    assert "- booley.source -> booley.before" in result.stdout
    assert result.stdout.count("Analyzer commit:") == 1


def test_ref_mode_rejects_bad_ref_and_archive_without_package(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"], cwd=repository, check=True
    )
    (repository / "README").write_text("empty")
    subprocess.run(["git", "add", "."], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "empty"], cwd=repository, check=True)

    bad_ref = _run(
        "--before-ref",
        "missing",
        "--after-ref",
        "HEAD",
        "--repo-root",
        str(repository),
    )
    bad_archive = _run(
        "--before-ref",
        "HEAD",
        "--after-ref",
        "HEAD",
        "--repo-root",
        str(repository),
    )

    assert bad_ref.returncode == 1
    assert "cannot resolve ref 'missing'" in bad_ref.stderr
    assert bad_archive.returncode == 1
    assert "cannot archive" in bad_archive.stderr
