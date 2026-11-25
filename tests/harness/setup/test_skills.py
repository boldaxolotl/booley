"""Tests for host skill reconciliation during ``booley init``."""

from pathlib import Path

import pytest

from booley.harness.setup import skills as init_skills
from tests.conftest import symlink_or_skip


@pytest.mark.parametrize(
    ("claude", "targets"),
    [
        pytest.param(None, (".agents",), id="agents-only"),
        # Regression: with only `~/.claude`, the default `~/.agents` target was skipped.
        pytest.param("directory", (".agents", ".claude"), id="distinct-claude"),
        pytest.param("alias", (".agents",), id="claude-aliases-agents"),
    ],
)
def test_host_skills_reach_agents_and_each_distinct_claude_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, claude: str | None, targets: tuple[str, ...]
) -> None:
    source = tmp_path / "packaged"
    (source / "booley-setup").mkdir(parents=True)
    (source / "booley-setup" / "SKILL.md").write_text("# Setup\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    if claude == "directory":
        (home / ".claude").mkdir()
    elif claude == "alias":
        (home / ".agents").mkdir()
        symlink_or_skip(home / ".claude", home / ".agents", target_is_directory=True)
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(home))
    expected = [home / name / "skills" for name in targets]

    pending = init_skills.reconcile_host_skills(source, dry_run=True, allow_retarget=True)

    assert [item.target for item in pending] == expected
    assert [item.report.count("created") for item in pending] == [1] * len(expected)
    assert not (home / ".agents" / "skills").exists()

    changed = init_skills.reconcile_host_skills(source, dry_run=False, allow_retarget=True)

    assert [item.report.count("created") for item in changed] == [1] * len(expected)
    assert all((target / "booley-setup").is_dir() for target in expected)
