"""Tests for pre_push_hook — the last-chance guard on outgoing commits.

Exercised against real git repositories rather than mocked ``git log`` output:
the whole point of this hook is that it reads fields (author, committer) which
no earlier hook can see, so the parse of git's real output is the part worth
testing.
"""

from __future__ import annotations

import io
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from booley.dev_support.pre_push_hook import _commit_facts, _commit_offenses, main

_ZERO_SHA = "0" * 40


def _git(repo: Path, *args: str, **env_overrides: str) -> str:
    """Run git in *repo*, returning stdout; raises on failure."""
    env = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        **env_overrides,
    }
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env=env,
        timeout=30,
    )
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repo with one commit authored by 'Real Dev <dev@example.com>'."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Real Dev")
    _git(root, "config", "user.email", "dev@example.com")
    (root / "file.txt").write_text("hello\n", encoding="utf-8")
    _git(root, "add", "file.txt")
    _git(root, "commit", "-q", "--no-verify", "-m", "feat(core): add the file")
    return root


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def _commit(repo: Path, message: str, *, author: str | None = None) -> str:
    """Add a commit carrying *message*, optionally under a forged *author*."""
    path = repo / "file.txt"
    path.write_text(path.read_text(encoding="utf-8") + "more\n", encoding="utf-8")
    _git(repo, "add", "file.txt")
    args = ["commit", "-q", "--no-verify", "-m", message]
    if author is not None:
        args.append(f"--author={author}")
    _git(repo, *args)
    return _head(repo)


def _commit_symlink(repo: Path, name: str, target: str) -> str:
    """Commit a symlink without reading through it from the worktree."""
    (repo / name).symlink_to(target)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "--no-verify", "-m", "feat(core): add link")
    return _head(repo)


# ---------------------------------------------------------------------------
# _commit_facts
# ---------------------------------------------------------------------------


class TestCommitFacts:
    def test_parses_identities_and_message(self, repo, monkeypatch):
        monkeypatch.chdir(repo)
        facts = _commit_facts(_head(repo))
        assert facts is not None
        author_name, author_email, committer_name, committer_email, message = facts
        assert (author_name, author_email) == ("Real Dev", "dev@example.com")
        assert (committer_name, committer_email) == ("Real Dev", "dev@example.com")
        assert message.startswith("feat(core): add the file")

    def test_multiline_body_survives_the_split(self, repo, monkeypatch):
        """The message is the last field precisely so its newlines are harmless."""
        monkeypatch.chdir(repo)
        sha = _commit(repo, "feat(core): thing\n\nfirst body line\nsecond body line")
        facts = _commit_facts(sha)
        assert facts is not None
        assert "first body line" in facts[4]
        assert "second body line" in facts[4]
        assert facts[0] == "Real Dev"  # identity fields unaffected by the body

    def test_forged_author_is_reported_separately_from_committer(self, repo, monkeypatch):
        """`--author` changes only the author — the exact shape of the real case."""
        monkeypatch.chdir(repo)
        sha = _commit(repo, "test(mut): mutation muxes", author="mut-creator <mut@local>")
        facts = _commit_facts(sha)
        assert facts is not None
        assert (facts[0], facts[1]) == ("mut-creator", "mut@local")
        assert (facts[2], facts[3]) == ("Real Dev", "dev@example.com")

    def test_unknown_sha_returns_none(self, repo, monkeypatch):
        monkeypatch.chdir(repo)
        assert _commit_facts("0" * 40) is None


# ---------------------------------------------------------------------------
# _commit_offenses
# ---------------------------------------------------------------------------


class TestCommitOffenses:
    def test_clean_commit_no_allowlist(self, repo, monkeypatch):
        monkeypatch.chdir(repo)
        assert _commit_offenses(_head(repo), []) == []

    def test_clean_commit_matching_allowlist(self, repo, monkeypatch):
        monkeypatch.chdir(repo)
        assert _commit_offenses(_head(repo), ["*@example.com"]) == []

    def test_banned_phrase_in_message(self, repo, monkeypatch):
        monkeypatch.chdir(repo)
        sha = _commit(repo, "fix(core): repair it\n\nPaired with claude on this.")
        offenses = _commit_offenses(sha, [])
        assert any("banned terms" in o and "claude" in o for o in offenses)

    def test_forged_author_blocked_by_allowlist(self, repo, monkeypatch):
        """The motivating case: commit-msg never sees `--author`, this does."""
        monkeypatch.chdir(repo)
        sha = _commit(repo, "test(mut): mutation muxes", author="mut-creator <mut@local>")
        offenses = _commit_offenses(sha, ["*@example.com"])
        assert len(offenses) == 1
        assert "author not in" in offenses[0]
        assert "mut@local" in offenses[0]

    def test_forged_author_passes_when_allowlisted(self, repo, monkeypatch):
        monkeypatch.chdir(repo)
        sha = _commit(repo, "test(mut): mutation muxes", author="mut-creator <mut@local>")
        assert _commit_offenses(sha, ["*@example.com", "mut@local"]) == []

    def test_no_allowlist_permits_any_identity(self, repo, monkeypatch):
        """The check is opt-in — an unconfigured project behaves as before."""
        monkeypatch.chdir(repo)
        sha = _commit(repo, "test(mut): mutation muxes", author="mut-creator <mut@local>")
        assert _commit_offenses(sha, []) == []

    def test_committer_is_checked_too(self, repo, monkeypatch):
        """A real author with a forged committer is the same problem, mirrored."""
        monkeypatch.chdir(repo)
        sha = _commit(repo, "test(mut): mutation muxes", author="mut-creator <mut@local>")
        offenses = _commit_offenses(sha, ["mut@local"])  # allows author, not committer
        assert len(offenses) == 1
        assert "committer not in" in offenses[0]

    def test_both_offense_kinds_reported_together(self, repo, monkeypatch):
        """One commit can be wrong in two ways; neither hides the other."""
        monkeypatch.chdir(repo)
        sha = _commit(
            repo,
            "fix(core): repair it\n\nBuilt with claude.",
            author="mut-creator <mut@local>",
        )
        offenses = _commit_offenses(sha, ["*@example.com"])
        assert any("banned terms" in o for o in offenses)
        assert any("author not in" in o for o in offenses)

    def test_banned_term_in_tracked_path_is_blocked(self, repo, monkeypatch):
        monkeypatch.chdir(repo)
        path = repo / "booley-state.txt"
        path.write_text("opaque\n", encoding="utf-8")
        _git(repo, "add", path.name)
        _git(repo, "commit", "-q", "--no-verify", "-m", "feat(core): add state")

        offenses = _commit_offenses(_head(repo), [])

        assert any("tracked path has banned terms" in offense for offense in offenses)

    def test_booley_source_paths_are_outside_project_stealth(self, repo, monkeypatch):
        monkeypatch.chdir(repo)
        (repo / "pyproject.toml").write_text(
            "[tool.booley]\nsource_checkout = true\n",
            encoding="utf-8",
        )
        package = repo / "src" / "booley" / "feature.py"
        package.parent.mkdir(parents=True)
        package.write_text("value = 1\n", encoding="utf-8")
        _git(repo, "add", ".")
        _git(repo, "commit", "-q", "--no-verify", "-m", "docs: explain Booley architecture")

        assert _commit_offenses(_head(repo), [], repository_root=repo) == []

    def test_symlink_target_is_read_from_committed_blob(self, repo, monkeypatch):
        monkeypatch.chdir(repo)
        (repo / ".booley_project").mkdir()
        sha = _commit_symlink(repo, "guide", ".booley_project/AGENTS.md")
        (repo / "guide").unlink()
        (repo / "guide").symlink_to("ordinary.txt")

        offenses = _commit_offenses(sha, [])

        assert any("symlink target exposes project state" in offense for offense in offenses)
        assert any(".booley_project/AGENTS.md" in offense for offense in offenses)

    def test_custom_project_dir_target_is_blocked_without_banned_term(self, repo, monkeypatch):
        monkeypatch.chdir(repo)
        project_dir = repo.parent / ".private-state"
        project_dir.mkdir()
        sha = _commit_symlink(repo, "guide", "../.private-state/AGENTS.md")

        offenses = _commit_offenses(sha, [], project_dir=project_dir)

        assert any("symlink target exposes project state" in offense for offense in offenses)

    def test_ordinary_symlink_is_allowed(self, repo, monkeypatch):
        monkeypatch.chdir(repo)
        sha = _commit_symlink(repo, "guide", "docs/guide.md")

        assert _commit_offenses(sha, []) == []


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _stdin(local_sha: str, remote_sha: str = _ZERO_SHA):
    """Patch stdin with one git-supplied pre-push ref line."""
    line = f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n"
    return patch("sys.stdin", io.StringIO(line))


class TestMain:
    def test_allows_clean_push(self, repo, monkeypatch):
        monkeypatch.chdir(repo)
        monkeypatch.delenv("BOOLEY_SKIP_PUSH_GUARD", raising=False)
        with (
            _stdin(_head(repo)),
            patch("booley.dev_support.pre_push_hook.allowed_authors", return_value=[]),
        ):
            assert main() == 0

    def test_blocks_unlisted_author(self, repo, monkeypatch, capsys):
        monkeypatch.chdir(repo)
        monkeypatch.delenv("BOOLEY_SKIP_PUSH_GUARD", raising=False)
        sha = _commit(repo, "test(mut): mutation muxes", author="mut-creator <mut@local>")
        with (
            _stdin(sha),
            patch(
                "booley.dev_support.pre_push_hook.allowed_authors", return_value=["*@example.com"]
            ),
        ):
            assert main() == 1
        err = capsys.readouterr().err
        assert "push blocked" in err
        assert sha[:12] in err
        assert "mut@local" in err

    def test_blocks_symlink_to_project_state(self, repo, monkeypatch, capsys):
        monkeypatch.chdir(repo)
        monkeypatch.delenv("BOOLEY_SKIP_PUSH_GUARD", raising=False)
        project_dir = repo / ".booley_project"
        project_dir.mkdir()
        sha = _commit_symlink(repo, "AGENTS.md", ".booley_project/AGENTS.md")
        with (
            _stdin(sha),
            patch("booley.dev_support.pre_push_hook.stealth_enabled", return_value=True),
            patch("booley.dev_support.pre_push_hook.allowed_authors", return_value=[]),
        ):
            assert main() == 1
        err = capsys.readouterr().err
        assert "push blocked" in err
        assert "symlink target" in err
        assert ".booley_project/AGENTS.md" in err

    def test_blocks_tracked_project_state_when_runtime_uses_external_alias(
        self, repo, monkeypatch, capsys
    ):
        monkeypatch.chdir(repo)
        monkeypatch.delenv("BOOLEY_SKIP_PUSH_GUARD", raising=False)
        project_dir = repo.parent / "runtime-project-state"
        project_dir.mkdir()
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(project_dir))
        path = repo / ".booley_project" / "docs" / "example.md"
        path.parent.mkdir(parents=True)
        path.write_text("private project state\n", encoding="utf-8")
        _git(repo, "add", ".booley_project/docs/example.md")
        _git(repo, "commit", "-q", "--no-verify", "-m", "docs: add example")

        with _stdin(_head(repo)):
            assert main() == 1

        err = capsys.readouterr().err
        assert "tracked path exposes project state" in err
        assert ".booley_project/docs/example.md" in err

    def test_blocks_checkout_state_symlink_when_runtime_uses_external_alias(
        self, repo, monkeypatch, capsys
    ):
        monkeypatch.chdir(repo)
        monkeypatch.delenv("BOOLEY_SKIP_PUSH_GUARD", raising=False)
        project_dir = repo.parent / "runtime-project-state"
        project_dir.mkdir()
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(project_dir))
        sha = _commit_symlink(repo, "AGENTS.md", ".booley_project/AGENTS.md")

        with _stdin(sha):
            assert main() == 1

        err = capsys.readouterr().err
        assert "symlink target exposes project state" in err
        assert ".booley_project/AGENTS.md" in err

    def test_blocks_case_variant_of_checkout_project_state(self, repo, monkeypatch, capsys):
        monkeypatch.chdir(repo)
        monkeypatch.delenv("BOOLEY_SKIP_PUSH_GUARD", raising=False)
        project_dir = repo.parent / "runtime-project-state"
        project_dir.mkdir()
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(project_dir))
        path = repo / ".BOOLEY_PROJECT" / "docs" / "example.md"
        path.parent.mkdir(parents=True)
        path.write_text("private project state\n", encoding="utf-8")
        _git(repo, "add", ".BOOLEY_PROJECT/docs/example.md")
        _git(repo, "commit", "-q", "--no-verify", "-m", "docs: add example")

        with _stdin(_head(repo)):
            assert main() == 1

        err = capsys.readouterr().err
        assert "tracked path exposes project state" in err
        assert ".BOOLEY_PROJECT/docs/example.md" in err

    def test_blocks_tracked_project_state_root(self, repo, monkeypatch, capsys):
        monkeypatch.chdir(repo)
        monkeypatch.delenv("BOOLEY_SKIP_PUSH_GUARD", raising=False)
        project_dir = repo.parent / "runtime-project-state"
        project_dir.mkdir()
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(project_dir))
        (repo / ".booley_project").write_text("private project state\n", encoding="utf-8")
        _git(repo, "add", ".booley_project")
        _git(repo, "commit", "-q", "--no-verify", "-m", "docs: add state")

        with _stdin(_head(repo)):
            assert main() == 1

        err = capsys.readouterr().err
        assert "tracked path exposes project state: .booley_project" in err

    @pytest.mark.parametrize(
        "relative_path",
        [
            ".booley_project_backup/example.md",
            "docs/.booley_project/example.md",
        ],
    )
    def test_allows_checkout_project_state_name_lookalikes(self, repo, monkeypatch, relative_path):
        monkeypatch.chdir(repo)
        monkeypatch.delenv("BOOLEY_SKIP_PUSH_GUARD", raising=False)
        project_dir = repo.parent / "runtime-project-state"
        project_dir.mkdir()
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(project_dir))
        path = repo / relative_path
        path.parent.mkdir(parents=True)
        path.write_text("ordinary docs\n", encoding="utf-8")
        _git(repo, "add", relative_path)
        _git(repo, "commit", "-q", "--no-verify", "-m", "docs: add example")

        with _stdin(_head(repo)):
            assert main() == 0

    def test_escape_hatch_skips_everything(self, repo, monkeypatch):
        monkeypatch.chdir(repo)
        monkeypatch.setenv("BOOLEY_SKIP_PUSH_GUARD", "1")
        sha = _commit(repo, "test(mut): mutation muxes", author="mut-creator <mut@local>")
        with (
            _stdin(sha),
            patch(
                "booley.dev_support.pre_push_hook.allowed_authors", return_value=["*@example.com"]
            ),
        ):
            assert main() == 0

    def test_stealth_off_disables_the_guard(self, repo, monkeypatch):
        monkeypatch.chdir(repo)
        monkeypatch.delenv("BOOLEY_SKIP_PUSH_GUARD", raising=False)
        sha = _commit(repo, "test(mut): mutation muxes", author="mut-creator <mut@local>")
        with (
            _stdin(sha),
            patch("booley.dev_support.pre_push_hook.stealth_enabled", return_value=False),
            patch(
                "booley.dev_support.pre_push_hook.allowed_authors", return_value=["*@example.com"]
            ),
        ):
            assert main() == 0

    def test_branch_deletion_is_not_scanned(self, repo, monkeypatch):
        """A delete push has no outgoing commits — nothing to check."""
        monkeypatch.chdir(repo)
        monkeypatch.delenv("BOOLEY_SKIP_PUSH_GUARD", raising=False)
        with (
            _stdin(_ZERO_SHA, _head(repo)),
            patch(
                "booley.dev_support.pre_push_hook.allowed_authors", return_value=["nobody@nowhere"]
            ),
        ):
            assert main() == 0
