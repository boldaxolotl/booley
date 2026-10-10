"""Checked-in Project fixtures keep every ignore pattern `booley init` writes."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.runtime.project_gitignore import (
    PROJECT_GITIGNORE,
    PROJECT_GITIGNORE_PATTERNS,
    REINCLUDES,
    missing_gitignore_patterns,
)

FIXTURES = Path(__file__).parents[1] / "fixtures"


@pytest.mark.parametrize(
    "gitignore",
    sorted(FIXTURES.glob("*/.booley_project/.gitignore")),
    ids=lambda path: path.parents[1].name,
)
def test_fixture_project_gitignore_is_current(gitignore: Path) -> None:
    # A stale fixture lets board documents and state records be committed,
    # which the Ticket Board legacy-layout guard then refuses (ADR 0065).
    assert missing_gitignore_patterns(gitignore.read_text(encoding="utf-8")) == []


def test_every_reinclude_follows_the_pattern_it_overrides() -> None:
    for reinclude, overridden in REINCLUDES.items():
        assert PROJECT_GITIGNORE_PATTERNS.index(overridden) < PROJECT_GITIGNORE_PATTERNS.index(
            reinclude
        )
    assert missing_gitignore_patterns(PROJECT_GITIGNORE) == []


@pytest.mark.parametrize(
    ("path", "responsible"),
    [
        ("logs/authored.txt", "/logs/"),
        ("cores/logs/authored.txt", None),
        ("goals/g1/record.json", "goals/*/"),
        ("goals/history/kept.md", None),
        ("goals/history/tmp/state.json", "tmp/"),
        ("foo/__pycache__/a.pyc", "__pycache__/"),
        ("foo/a.pyc", "*.pyc"),
        ("cores/runtime/state.json", "runtime/"),
        ("cores/.managed/bundle.zip", ".managed/"),
    ],
)
def test_responsible_canonical_pattern(path, responsible, tmp_path, isolated_git_attributes):
    import subprocess

    from booley.runtime.project_gitignore import (
        is_project_transient_path,
        project_transient_pattern,
    )

    (tmp_path / ".gitignore").write_text(PROJECT_GITIGNORE, encoding="utf-8")
    file = tmp_path / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("state\n", encoding="utf-8")
    empty = tmp_path / "empty-excludes"
    empty.write_text("", encoding="utf-8")

    def git(*args):
        return subprocess.run(
            ["git", "-c", f"core.excludesFile={empty}", *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    assert git("init", "-q").returncode == 0
    result = git("check-ignore", "--no-index", "-v", path)
    assert result.returncode in {0, 1}
    if responsible is None:
        assert result.stdout == ""
    else:
        metadata, observed = result.stdout.rstrip("\n").split("\t")
        assert observed == path
        assert metadata.split(":", 2)[2] == responsible
    assert project_transient_pattern(path) == responsible
    assert is_project_transient_path(path) == (responsible is not None)


@pytest.mark.parametrize(
    ("patterns", "path", "responsible", "git_pattern"),
    [
        (("tmp/", "/tmp/"), "tmp/state.json", "/tmp/", "/tmp/"),
        (("/tmp/", "tmp/"), "tmp/state.json", "tmp/", "tmp/"),
        (("tmp/", "!tmp/"), "tmp/state.json", None, None),
        (("!tmp/", "tmp/"), "tmp/state.json", "tmp/", "tmp/"),
        (("tmp/", "!tmp/keep.json"), "tmp/keep.json", "tmp/", "tmp/"),
        (("tmp/", "!tmp/", "*.json"), "tmp/state.json", "*.json", "*.json"),
        (("*.json", "!*.json"), "state.json", None, "!*.json"),
    ],
)
def test_responsible_order_matches_verbose_git(
    patterns, path, responsible, git_pattern, tmp_path, monkeypatch, isolated_git_attributes
):
    import subprocess

    from booley.runtime import project_gitignore as policy

    monkeypatch.setattr(policy, "PROJECT_GITIGNORE_PATTERNS", patterns)
    (tmp_path / ".gitignore").write_text("".join(f"{p}\n" for p in patterns), encoding="utf-8")
    file = tmp_path / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("state\n", encoding="utf-8")
    empty = tmp_path / "empty-excludes"
    empty.write_text("", encoding="utf-8")

    def git(*args):
        return subprocess.run(
            ["git", "-c", f"core.excludesFile={empty}", *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    assert git("init", "-q").returncode == 0
    result = git("check-ignore", "--no-index", "-v", path)
    assert result.returncode in {0, 1}
    if git_pattern is None:
        assert result.stdout == ""
    else:
        metadata, observed = result.stdout.rstrip("\n").split("\t")
        assert observed == path
        assert metadata.split(":", 2)[2] == git_pattern
    assert policy.project_transient_pattern(path) == responsible
    assert policy.is_project_transient_path(path) == (responsible is not None)
