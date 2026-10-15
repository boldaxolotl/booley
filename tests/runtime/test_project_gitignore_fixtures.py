"""Project fixture ignore policies reconcile through `booley init`."""

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
    sorted(FIXTURES.glob("*/.booley/.gitignore")),
    ids=lambda path: path.parents[1].name,
)
def test_fixture_project_gitignore_is_reconciled_by_init(gitignore: Path, tmp_path: Path) -> None:
    # A stale fixture lets board documents and state records be committed,
    # which the Ticket Board legacy-layout guard then refuses (ADR 0065).
    from booley.harness import init_cmd
    from booley.harness.setup.common import InitContext

    assert set(missing_gitignore_patterns(gitignore.read_text(encoding="utf-8"))) <= {
        "mcp-tool-reports/",
        "reviewer-state/",
    }
    copied = tmp_path / ".gitignore"
    copied.write_bytes(gitignore.read_bytes())
    init_cmd._backfill_project_gitignore(tmp_path, InitContext(project_root=tmp_path))
    assert missing_gitignore_patterns(copied.read_text(encoding="utf-8")) == []


def test_every_reinclude_follows_the_pattern_it_overrides() -> None:
    for reinclude, overridden in REINCLUDES.items():
        assert PROJECT_GITIGNORE_PATTERNS.index(overridden) < PROJECT_GITIGNORE_PATTERNS.index(
            reinclude
        )
    assert missing_gitignore_patterns(PROJECT_GITIGNORE) == []


@pytest.fixture
def verbose_ignore(tmp_path, isolated_git_attributes):
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("needs git for the canonical ignore oracle")

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

    def observe(patterns, path):
        (tmp_path / ".gitignore").write_text("".join(f"{p}\n" for p in patterns), encoding="utf-8")
        file = tmp_path / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("state\n", encoding="utf-8")
        result = git("check-ignore", "--no-index", "-v", path)
        assert result.returncode in {0, 1}
        if not result.stdout:
            return None
        metadata, observed = result.stdout.rstrip("\n").split("\t")
        assert observed == path
        return metadata.split(":", 2)[2]

    return observe


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
def test_responsible_canonical_pattern(path, responsible, verbose_ignore):
    from booley.runtime.project_gitignore import (
        is_project_transient_path,
        project_transient_pattern,
    )

    assert verbose_ignore(PROJECT_GITIGNORE_PATTERNS, path) == responsible
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
    patterns, path, responsible, git_pattern, monkeypatch, verbose_ignore
):
    from booley.runtime import project_gitignore as policy

    monkeypatch.setattr(policy, "PROJECT_GITIGNORE_PATTERNS", patterns)
    assert verbose_ignore(patterns, path) == git_pattern
    assert policy.project_transient_pattern(path) == responsible
    assert policy.is_project_transient_path(path) == (responsible is not None)


def _old_policy_checkouts(tmp_path, paired):
    from tests.goals.conftest import git

    root = tmp_path / "rtl"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    excludes = tmp_path / "empty-excludes"
    excludes.write_text("")
    git(root, "config", "core.excludesFile", str(excludes))
    directory = root / ".booley"
    directory.mkdir()
    if paired:
        git(directory, "init", "-q", "-b", "main")
        git(directory, "config", "core.excludesFile", str(excludes))
        (root / ".gitignore").write_text("/.booley/\n")
    (root / "rtl").mkdir()
    (root / "rtl/top.sv").write_text("module top; endmodule\n")
    (root / "design.core").write_text(
        "CAPI=2:\nname: ::ignore_regression:0\nfilesets:\n"
        "  rtl: {files: [rtl/top.sv], file_type: systemVerilogSource}\n"
        "targets:\n  default: {filesets: [rtl], toplevel: top}\n"
    )
    (directory / "booley.toml").write_text("[project]\nname='test'\n")
    old = PROJECT_GITIGNORE.replace("mcp-tool-reports/\n", "").replace("reviewer-state/\n", "")
    (directory / ".gitignore").write_text(old, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "old ignores")
    owner = directory if paired else root
    if paired:
        git(owner, "add", "-A")
        git(owner, "commit", "-qm", "old ignores")
    # An existing Goal branch retains its tracked old policy after main init.
    worktree = tmp_path / "selected"
    git(root, "worktree", "add", "-qb", "selected", str(worktree))
    selected_dir = worktree / ".booley"
    if paired:
        git(directory, "worktree", "add", "-qb", "selected", str(selected_dir))
    return root, directory, worktree, selected_dir


@pytest.mark.parametrize("paired", [False, True])
def test_reconciled_checkout_stays_clean_after_real_reviewer(tmp_path, monkeypatch, paired):
    from tests.goals.conftest import git

    from booley.core.models import AgentResult
    from booley.harness import init_cmd
    from booley.harness.setup.common import InitContext
    from booley.runtime import runtime_context
    from booley.specialists.reviewer import ReviewerSpecialist

    root, directory, worktree, selected_dir = _old_policy_checkouts(tmp_path, paired)
    init_cmd._backfill_project_gitignore(directory, InitContext(project_root=root))
    assert "mcp-tool-reports/" not in (selected_dir / ".gitignore").read_text(encoding="utf-8")
    init_cmd._backfill_project_gitignore(selected_dir, InitContext(project_root=worktree))
    policy_owner = selected_dir if paired else worktree
    git(policy_owner, "add", ".gitignore" if paired else ".booley/.gitignore")
    git(policy_owner, "commit", "--allow-empty", "-qm", "reconcile selected checkout")
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    monkeypatch.setenv("BOOLEY_DIR", str(selected_dir))
    for key in (
        "BOOLEY_STATE_FILE",
        "BOOLEY_RUNTIME_DIR",
        "BOOLEY_LOGS_DIR",
        "BOOLEY_GOAL_RUN_BINDING",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(
        "booley.specialists.specialist._call_agent_sync",
        lambda *_a, **_kw: AgentResult(output='{"issues": []}'),
    )
    selected_rtl = worktree
    monkeypatch.chdir(worktree)
    from booley.runtime.booley_dir import reset_cache

    reset_cache()
    result = ReviewerSpecialist().execute_cli(
        ["-C", str(selected_rtl), "--scope", "rtl/top.sv", "--category", "rtl", "--focus", "bugs"]
    )
    assert result.exit_code == 0, result.outcome.report_text
    assert list(selected_dir.glob("mcp-tool-reports/**/report.json"))
    assert list(selected_dir.glob("reviewer-state/**/*"))
    assert git(worktree, "status", "--porcelain") == ""
    assert git(policy_owner, "status", "--porcelain") == ""
