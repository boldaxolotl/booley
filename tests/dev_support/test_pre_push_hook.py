"""Tests for pre_push_hook — the last-chance guard on outgoing commits.

Exercised against real git repositories rather than mocked ``git log`` output:
the whole point of this hook is that it reads fields (author, committer) which
no earlier hook can see, so the parse of git's real output is the part worth
testing.
"""

from __future__ import annotations

import io
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

from booley.dev_support import pre_push_hook
from booley.dev_support.pre_push_hook import _commit_facts, _commit_offenses, main

_ZERO_SHA = "0" * 40


def _git(repo: Path, *args: str, **env_overrides: str) -> str:
    """Run git in *repo*, returning stdout; raises on failure."""
    env = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "PATH": os.environ.get("BOOLEY_TEST_GIT_PATH", "/usr/bin:/bin:/usr/local/bin"),
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
def repo(tmp_path: Path, monkeypatch) -> Path:
    """A git repo with one commit authored by 'Real Dev <dev@example.com>'."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Real Dev")
    _git(root, "config", "user.email", "dev@example.com")
    # Every commit otherwise spawns a detached `git maintenance run --auto` that
    # briefly holds objects/maintenance.lock and races object-store snapshots.
    _git(root, "config", "maintenance.auto", "false")
    (root / "file.txt").write_text("hello\n", encoding="utf-8")
    _git(root, "add", "file.txt")
    _git(root, "commit", "-q", "--no-verify", "-m", "feat(core): add the file")
    destination = tmp_path / "destination.git"
    _git(root, "init", "--bare", str(destination))
    monkeypatch.setattr(sys, "argv", ["pre-push", "destination", str(destination)])
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    return root


def test_fixture_commits_spawn_no_background_maintenance(repo: Path, tmp_path: Path) -> None:
    """Object-store snapshot tests need commits that leave no detached writer."""
    trace = tmp_path / "commit.trace"
    (repo / "file.txt").write_text("traced\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "fix(core): traced commit", "--no-verify", GIT_TRACE=str(trace))
    assert trace.is_file()
    assert "maintenance run" not in trace.read_text(encoding="utf-8")


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


def _remove_loose_object(repo: Path, oid: str) -> None:
    """Delete one loose object; Git writes them read-only, which Windows enforces."""
    path = repo / ".git" / "objects" / oid[:2] / oid[2:]
    path.chmod(path.stat().st_mode | stat.S_IWRITE)
    path.unlink()


def _commit_symlink(repo: Path, name: str, target: str) -> str:
    """Commit a symlink without reading through it from the worktree."""
    (repo / name).symlink_to(target)
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "--no-verify", "-m", "feat(core): add link")
    return _head(repo)


def _configure_external_project_state(repo: Path, monkeypatch) -> None:
    """Run the hook as if Project state used a separate runtime mount."""
    monkeypatch.chdir(repo)
    monkeypatch.delenv("BOOLEY_SKIP_PUSH_GUARD", raising=False)
    project_dir = repo.parent / "runtime-project-state"
    project_dir.mkdir()
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(project_dir))


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


def test_project_dir_fallback_uses_hook_parent_when_runtime_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hook_dir = tmp_path / "hooks"
    hook_dir.mkdir()
    monkeypatch.setattr(pre_push_hook, "__file__", str(hook_dir / "pre-push"))
    runtime_module = types.ModuleType("booley.runtime.project_dir")

    def unavailable(_repository_root: Path) -> Path:
        raise FileNotFoundError

    runtime_module.resolve_checkout_project_dir = unavailable
    monkeypatch.setitem(sys.modules, "booley.runtime.project_dir", runtime_module)

    assert pre_push_hook._guard_project_dir(tmp_path / "repo") == tmp_path


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
        _configure_external_project_state(repo, monkeypatch)
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
        _configure_external_project_state(repo, monkeypatch)
        sha = _commit_symlink(repo, "AGENTS.md", ".booley_project/AGENTS.md")

        with _stdin(sha):
            assert main() == 1

        err = capsys.readouterr().err
        assert "symlink target exposes project state" in err
        assert ".booley_project/AGENTS.md" in err

    def test_blocks_case_variant_of_checkout_project_state(self, repo, monkeypatch, capsys):
        _configure_external_project_state(repo, monkeypatch)
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
        _configure_external_project_state(repo, monkeypatch)
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
        _configure_external_project_state(repo, monkeypatch)
        config = Path(os.environ["BOOLEY_PROJECT_DIR"]) / "booley.toml"
        config.write_text("[stealth]\nbanned_words = []\n")
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


@pytest.fixture(params=["current", "minimum"], ids=["current", "minimum-2.37.2"])
def actual_git(request, monkeypatch):
    """Execute setup and hook with a real executable, never a version adapter."""
    if request.param == "current":
        executable = shutil.which("git")
    else:
        executable = os.environ.get("BOOLEY_TEST_MIN_GIT")
        if not executable:
            if os.environ.get("BOOLEY_TEST_REQUIRE_MIN_GIT") == "1":
                pytest.fail("mandatory real Git 2.37.2 executable is absent")
            pytest.skip(
                "optional minimum Git matrix requires BOOLEY_TEST_MIN_GIT; not local gate evidence"
            )
        assert Path(executable).is_absolute() and Path(executable).is_file()
        version = subprocess.run(
            [executable, "--version"], capture_output=True, text=True, timeout=10, check=True
        )
        assert re.fullmatch(r"git version 2\.37\.2(?:\.windows\.\d+)?\n?", version.stdout)
        probe = subprocess.run(
            [executable, "--no-lazy-fetch", "--version"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert probe.returncode == 129 and b"unknown option: --no-lazy-fetch" in probe.stderr
        print(f"minimum Git receipt: {Path(executable).resolve()} {version.stdout.strip()}")
    assert executable
    path = str(Path(executable).resolve().parent) + os.pathsep + os.environ.get("PATH", "")
    monkeypatch.setenv("PATH", path)
    monkeypatch.setenv("BOOLEY_TEST_GIT_PATH", path)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.delenv("BOOLEY_SKIP_PUSH_GUARD", raising=False)
    return executable


@pytest.fixture
def matrix_repo(actual_git, repo, monkeypatch):
    monkeypatch.chdir(repo)
    return repo


def _upstream_pair(repo, upstream, base=None):
    config = repo / ".booley_project" / "booley.toml"
    config.parent.mkdir(exist_ok=True)
    config.write_text(
        f'[stealth]\nupstream_repository = "{upstream.as_posix()}"\nupstream_base = "{base or _head(repo)}"\n',
        encoding="utf-8",
    )


def _clone_upstream(repo, name="independent.git"):
    upstream = repo.parent / name
    _git(repo, "clone", "--bare", str(repo), str(upstream))
    return upstream


def _push(sha, old=_ZERO_SHA):
    with _stdin(sha, old):
        return main()


def test_minimum_git_generated_only_first_import(matrix_repo: Path) -> None:
    repo = matrix_repo
    base = _commit(repo, "fix(core): generated instructions and docker verification agent")
    assert _push(base) == 0
    destination = Path(sys.argv[2])
    _git(repo, "push", "--no-verify", str(destination), "HEAD:refs/heads/main")
    incremental = _commit(repo, "fix(core): generated with care")
    assert _push(incremental, base) == 0
    assert _push(_commit(repo, "fix(core): claude metadata"), base) == 1


def test_minimum_git_destination_protected_ancestry_and_fresh_metadata(
    matrix_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = matrix_repo
    base = _commit(
        repo, "fix(core): generated by claude", author="Foreign <foreign@elsewhere.test>"
    )
    upstream = _clone_upstream(repo)
    sys.argv[2] = str(upstream)
    config = repo / ".booley_project" / "booley.toml"
    config.parent.mkdir(exist_ok=True)
    config.write_text('[stealth]\nallowed_authors = ["*@example.com"]\n')
    assert _push(base) == 0
    assert "destination advertisement unavailable" not in capsys.readouterr().err
    assert _push(_commit(repo, "fix(core): clean", author="Foreign <foreign@elsewhere.test>")) == 1
    _git(repo, "reset", "--hard", base)
    assert _push(_commit(repo, "fix(core): co-authored-by: Cursor Agent")) == 1
    _git(repo, "reset", "--hard", base)
    (repo / "claude.txt").write_text("fresh ordinary contents\n")
    _git(repo, "add", "claude.txt")
    _git(repo, "commit", "--no-verify", "-m", "fix(core): ordinary new file")
    assert _push(_head(repo)) == 1
    _git(repo, "reset", "--hard", base)
    assert _push(_commit_symlink(repo, "state-link", ".booley_project/private")) == 1


@pytest.mark.parametrize(
    "state", ["promisor", "filter", "partial", "include", "environment", "marker"]
)
def test_minimum_git_partial_state_preflight(
    matrix_repo: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    repo = matrix_repo
    if state == "marker":
        pack = repo / ".git" / "objects" / "pack"
        (pack / "synthetic.promisor").write_bytes(b"")
    elif state == "include":
        included = repo.parent / "partial.config"
        included.write_text('[remote "origin"]\n promisor = false\n')
        _git(repo, "config", "include.path", str(included))
    elif state == "environment":
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "remote.origin.promisor")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    else:
        key = {
            "promisor": "remote.origin.promisor",
            "filter": "remote.origin.partialclonefilter",
            "partial": "extensions.partialclone",
        }[state]
        _git(repo, "config", key, "false" if state != "partial" else "origin")
    executable = shutil.which("git")
    probe = subprocess.run(
        [executable, "--no-lazy-fetch", "--version"], capture_output=True, timeout=10, check=False
    )
    if probe.returncode == 0:
        # The current capability safely inspects local complete objects; file
        # authority preflight remains strict on this path (tested separately).
        assert _push(_head(repo)) == 0
    else:
        calls = []
        original = pre_push_hook._run_git

        def observe(args, **kwargs):
            calls.append(args)
            return original(args, **kwargs)

        monkeypatch.setattr(pre_push_hook, "_run_git", observe)
        assert _push(_head(repo)) == 1
        assert not any(
            "cat-file" in args or "rev-list" in args or "diff-tree" in args for args in calls
        )


@pytest.mark.parametrize("storage", ["alternates", "symlink"])
def test_minimum_git_complete_alternate_and_symlink_storage(
    matrix_repo: Path, monkeypatch: pytest.MonkeyPatch, storage: str
) -> None:
    repo = matrix_repo
    upstream = _clone_upstream(repo)
    if storage == "alternates":
        clone = repo.parent / "shared"
        _git(repo, "clone", "--shared", str(upstream), str(clone))
    else:
        clone = repo.parent / "symlinked"
        _git(repo, "clone", str(upstream), str(clone))
        objects = clone / ".git" / "objects"
        moved = clone.parent / "real-objects"
        objects.rename(moved)
        try:
            objects.symlink_to(moved, target_is_directory=True)
        except OSError:
            if os.environ.get("BOOLEY_TEST_REQUIRE_MIN_GIT") == "1":
                pytest.fail("required minimum matrix cannot skip directory symlink evidence")
            pytest.skip("native platform cannot create directory symlink")
    _git(clone, "config", "user.name", "Real Dev")
    _git(clone, "config", "user.email", "dev@example.com")
    monkeypatch.chdir(clone)
    assert _push(_head(clone)) == 0
    assert _push(_commit(clone, "fix(core): claude metadata")) == 1


@pytest.mark.parametrize("missing", ["commit", "tree", "symlink"])
def test_minimum_git_missing_inspected_objects_refuse(matrix_repo: Path, missing: str) -> None:
    repo = matrix_repo
    sha = _commit_symlink(repo, "guide", "docs/guide.md")
    oid = (
        sha
        if missing == "commit"
        else _git(repo, "rev-parse", f"{sha}^{{tree}}" if missing == "tree" else f"{sha}:guide")
    )
    _remove_loose_object(repo, oid)
    assert _push(sha) == 1


def test_destination_other_branch_is_authoritative(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    base = _commit(repo, "fix(core): claude imported history")
    _git(repo, "push", "--no-verify", sys.argv[2], "HEAD:refs/heads/other")
    assert _push(base) == 0


def test_unrelated_and_stale_remote_refs_never_exempt(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    base = _commit(repo, "fix(core): claude local history")
    _git(repo, "update-ref", "refs/remotes/unrelated/main", base)
    assert _push(base) == 1


@pytest.mark.parametrize("failure", ["timeout", "auth", "malformed", "denied"])
def test_destination_failure_complete_superset(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], failure: str
) -> None:
    monkeypatch.chdir(repo)
    old = _commit(repo, "fix(core): claude historical identity")
    fresh = _commit(repo, "fix(core): ordinary clean work")
    original = pre_push_hook._run_git
    calls = []

    def fail(args, **kwargs):
        if "ls-remote" in args:
            calls.append(args)
            if failure == "timeout":
                raise pre_push_hook.InspectionError("advertisement timeout")
            return subprocess.CompletedProcess(
                args,
                1 if failure == "auth" else 0,
                b"garbled" if failure == "malformed" else b"",
                b"credential stderr",
            )
        return original(args, **kwargs)

    monkeypatch.setattr(pre_push_hook, "_run_git", fail)
    if failure == "denied":
        monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "ssh")
    assert _push(fresh, old) == 0
    assert _push(_commit(repo, "fix(core): claude fresh identity"), old) == 1
    error = capsys.readouterr().err
    assert "advertisement unavailable" in error and "conservative" in error
    assert "credential stderr" not in error
    if failure == "denied":
        assert calls == []


@pytest.mark.parametrize("kind", ["rev-list", "cat-file", "diff-tree"])
def test_fallback_inspection_failure_refuses(
    repo: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    monkeypatch.chdir(repo)
    original = pre_push_hook._run_git

    def fail(args, **kwargs):
        if "ls-remote" in args or kind in args:
            return subprocess.CompletedProcess(args, 1, b"", b"")
        return original(args, **kwargs)

    monkeypatch.setattr(pre_push_hook, "_run_git", fail)
    assert _push(_head(repo)) == 1


@pytest.mark.parametrize(
    "line",
    [
        "garbled",
        "refs/heads/main 0000000 refs/heads/main 0000000",
        "refs/heads/bad..ref {oid} refs/heads/main {zero}",
    ],
)
def test_malformed_protocol_refuses_without_discovery(
    repo: Path, monkeypatch: pytest.MonkeyPatch, line: str
) -> None:
    monkeypatch.chdir(repo)
    line = line.format(oid=_head(repo), zero=_ZERO_SHA)
    original = pre_push_hook._run_git

    def observe(args, **kwargs):
        assert "ls-remote" not in args
        return original(args, **kwargs)

    monkeypatch.setattr(pre_push_hook, "_run_git", observe)
    monkeypatch.setattr(sys, "stdin", io.StringIO(line))
    assert main() == 1


def test_no_update_and_deletion_avoid_network(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo)
    _upstream_pair(repo, repo.parent / "offline.git", "f" * 40)
    original = pre_push_hook._run_git

    def observe(args, **kwargs):
        assert "ls-remote" not in args
        return original(args, **kwargs)

    monkeypatch.setattr(pre_push_hook, "_run_git", observe)
    with patch("sys.stdin", io.StringIO("")):
        assert main() == 0
    assert _push(_ZERO_SHA, _head(repo)) == 0
    monkeypatch.setattr(pre_push_hook, "_run_git", original)
    assert _push(_head(repo)) == 1


def test_destination_covered_base_needs_no_import_settings(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    base = _head(repo)
    _git(repo, "push", "--no-verify", sys.argv[2], "HEAD:refs/heads/main")
    assert _push(_commit(repo, "fix(core): new work"), base) == 0


def test_missing_old_destination_tip_conservative(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    assert _push(_head(repo), "f" * 40) == 0
    assert _push(_commit(repo, "fix(core): claude fresh work"), "f" * 40) == 1


@pytest.mark.parametrize("policy", ["never", "user"])
def test_effective_protocol_policy_cannot_be_widened(
    repo: Path, monkeypatch: pytest.MonkeyPatch, policy: str
) -> None:
    monkeypatch.chdir(repo)
    _git(repo, "config", "protocol.file.allow", policy)
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file:ssh:https")
    monkeypatch.setenv("GIT_PROTOCOL_FROM_USER", "0")
    original = pre_push_hook._run_git

    def observe(args, **kwargs):
        assert "ls-remote" not in args
        return original(args, **kwargs)

    monkeypatch.setattr(pre_push_hook, "_run_git", observe)
    assert _push(_head(repo)) == 0
    assert _push(_commit(repo, "fix(core): claude fresh work")) == 1


def test_url_rewriting_denies_authority(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo)
    _git(repo, "config", "url./untrusted/.insteadOf", sys.argv[2])
    base = _commit(repo, "fix(core): claude historical work")
    assert _push(base) == 1


def test_deep_history_oldest_leak_and_bounded_batches(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    base = _commit(repo, "fix(core): claude oldest new work")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    tip = base
    for index in range(505):
        tip = _git(repo, "commit-tree", tree, "-p", tip, "-m", f"fix(core): step {index}")
    original = pre_push_hook._run_git
    batches = []

    def observe(args, **kwargs):
        if "cat-file" in args or "diff-tree" in args:
            batches.append(args)
        return original(args, **kwargs)

    monkeypatch.setattr(pre_push_hook, "_run_git", observe)
    assert _push(tip) == 1
    assert len(batches) < 10


@pytest.mark.parametrize(
    "trail",
    [
        "Co-Authored-By: Cursor Agent <cursor@example.test>",
        "Generated with Cursor",
        "Generated with GPT-5",
        "llm metadata",
    ],
)
def test_bypassed_trailer_remains_blocked(
    repo: Path, monkeypatch: pytest.MonkeyPatch, trail: str
) -> None:
    monkeypatch.chdir(repo)
    assert _push(_commit(repo, f"fix(core): ordinary work\n\n{trail}")) == 1


def test_minimum_git_file_promisor_authority_preflight_before_upload_pack(
    matrix_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = matrix_repo
    upstream = _clone_upstream(repo)
    _git(upstream, "config", "remote.origin.promisor", "false")
    monkeypatch.setattr(sys, "argv", ["pre-push", "destination", str(upstream)])
    original = pre_push_hook._run_git
    calls = []

    def observe(args, **kwargs):
        if "ls-remote" in args:
            calls.append(args[-1])
        return original(args, **kwargs)

    monkeypatch.setattr(pre_push_hook, "_run_git", observe)
    request_trace = repo.parent / "promisor-destination-requests.trace"
    monkeypatch.setenv("GIT_TRACE_PACKET", str(request_trace))
    before = _destination_inspection_state(upstream / "objects", request_trace)
    assert _push(_head(repo)) == 0
    assert "complete nonpromisor clone" in capsys.readouterr().err
    assert not calls
    assert before == _destination_inspection_state(upstream / "objects", request_trace)
    assert _push(_commit(repo, "fix(core): claude fresh metadata")) == 1
    assert not calls
    assert before == _destination_inspection_state(upstream / "objects", request_trace)


@pytest.mark.parametrize(
    "endpoint",
    ["http://example.test/repo", "git://example.test/repo", "ext::helper", "helper::repo"],
)
def test_unsafe_transport_never_executes_helper(
    repo: Path, monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    monkeypatch.chdir(repo)
    monkeypatch.setattr(sys, "argv", ["pre-push", "destination", endpoint])
    original = pre_push_hook._run_git

    def observe(args, **kwargs):
        assert "ls-remote" not in args
        return original(args, **kwargs)

    monkeypatch.setattr(pre_push_hook, "_run_git", observe)
    assert _push(_head(repo)) == 0


@pytest.mark.parametrize("kind", ["global", "include", "environment"])
def test_merged_protocol_restrictions_are_respected(
    repo: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    monkeypatch.chdir(repo)
    if kind == "global":
        _git(repo, "config", "protocol.allow", "never")
    elif kind == "include":
        included = repo.parent / "deny.config"
        included.write_text("[protocol]\nallow = never\n")
        _git(repo, "config", "include.path", str(included))
    else:
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "protocol.file.allow")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "never")
    original = pre_push_hook._run_git

    def observe(args, **kwargs):
        assert "ls-remote" not in args
        return original(args, **kwargs)

    monkeypatch.setattr(pre_push_hook, "_run_git", observe)
    assert _push(_head(repo)) == 0


@pytest.mark.parametrize("override", ["environment", "url-ssl", "url-redirect"])
def test_https_detectable_security_override_denies_authority(
    repo: Path, monkeypatch: pytest.MonkeyPatch, override: str
) -> None:
    monkeypatch.chdir(repo)
    destination = "https://user:credential@example.test/repository.git"
    monkeypatch.setattr(sys, "argv", ["pre-push", "destination", destination])
    if override == "environment":
        monkeypatch.setenv("GIT_SSL_NO_VERIFY", "true")
    else:
        key = "sslVerify" if override == "url-ssl" else "followRedirects"
        _git(
            repo,
            "config",
            f"http.https://example.test/.{key}",
            "false" if key == "sslVerify" else "true",
        )
    original = pre_push_hook._run_git

    def observe(args, **kwargs):
        assert "ls-remote" not in args
        return original(args, **kwargs)

    monkeypatch.setattr(pre_push_hook, "_run_git", observe)
    assert _push(_head(repo)) == 0


@pytest.mark.parametrize(
    "command",
    [
        "ssh -o StrictHostKeyChecking=no",
        "ssh -o 'StrictHostKeyChecking no'",
        "ssh -o UserKnownHostsFile=/dev/null",
        "ssh -o UserKnownHostsFile=NUL",
        "ssh -o StrictHostKeyChecking=accept-new",
    ],
)
def test_ssh_detectable_insecure_options_deny_authority(
    repo: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    monkeypatch.chdir(repo)
    monkeypatch.setattr(sys, "argv", ["pre-push", "destination", "ssh://example.test/repo.git"])
    monkeypatch.setenv("GIT_SSH_COMMAND", command)
    original = pre_push_hook._run_git

    def observe(args, **kwargs):
        assert "ls-remote" not in args
        return original(args, **kwargs)

    monkeypatch.setattr(pre_push_hook, "_run_git", observe)
    assert _push(_head(repo)) == 0


@pytest.mark.parametrize("custom", [False, True])
def test_ssh_owner_wrapper_preserved_and_default_strict(
    repo: Path, monkeypatch: pytest.MonkeyPatch, custom: bool
) -> None:
    monkeypatch.chdir(repo)
    monkeypatch.setattr(sys, "argv", ["pre-push", "destination", "ssh://example.test/repo.git"])
    monkeypatch.delenv("GIT_SSH", raising=False)
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.delenv("GIT_SSH_VARIANT", raising=False)
    wrapper = "owner-wrapper --trusted-proxy"
    if custom:
        monkeypatch.setenv("GIT_SSH_COMMAND", wrapper)
    original = pre_push_hook._run_git
    calls = []

    def observe(args, **kwargs):
        if "ls-remote" in args:
            calls.append(args)
            assert kwargs["env"]["GIT_SSH_COMMAND"] == (
                wrapper if custom else "ssh -o BatchMode=yes -o StrictHostKeyChecking=yes"
            )
            assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
            return subprocess.CompletedProcess(args, 0, b"", b"")
        return original(args, **kwargs)

    monkeypatch.setattr(pre_push_hook, "_run_git", observe)
    assert _push(_head(repo)) == 0
    assert len(calls) == 1


def test_complete_stdin_consumed_and_lookup_has_devnull(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    stdin = io.StringIO(f"refs/heads/main {_head(repo)} refs/heads/main {_ZERO_SHA}\n")
    monkeypatch.setattr(sys, "stdin", stdin)
    original = subprocess.run

    def observe(args, **kwargs):
        if "ls-remote" in args:
            assert stdin.tell() == len(stdin.getvalue())
            assert kwargs["stdin"] == subprocess.DEVNULL
            assert kwargs["timeout"] <= 10
        return original(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", observe)
    assert main() == 0


def test_annotated_tags_older_import_and_noncommit_updates(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    older = _head(repo)
    _commit(repo, "fix(core): claude imported identity")
    upstream = _clone_upstream(repo)
    sys.argv[2] = str(upstream)
    _git(repo, "tag", "-a", "older", older, "-m", "upstream release")
    assert _push(_git(repo, "rev-parse", "older")) == 0
    assert _push(_git(repo, "rev-parse", "HEAD^{tree}")) == 1


def test_multiref_new_tips_do_not_exempt_each_other(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    leak = _commit(repo, "fix(core): claude local work")
    clean = _commit(repo, "fix(core): ordinary local work")
    protocol = f"refs/heads/a {leak} refs/heads/a {_ZERO_SHA}\nrefs/heads/b {clean} refs/heads/b {_ZERO_SHA}\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO(protocol))
    assert main() == 1


def test_merge_exposes_new_side_branch_commit(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo)
    base = _head(repo)
    _git(repo, "push", "--no-verify", sys.argv[2], "HEAD:refs/heads/main")
    _git(repo, "checkout", "-b", "side")
    _commit(repo, "fix(core): claude side identity")
    _git(repo, "checkout", "main")
    _git(repo, "merge", "--no-ff", "side", "-m", "fix(core): merge side")
    assert _push(_head(repo), base) == 1


def test_replacement_does_not_hide_identity_and_active_grafts_refuse(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    leak = _commit(repo, "fix(core): claude local work")
    clean = _git(
        repo,
        "commit-tree",
        _git(repo, "rev-parse", "HEAD^{tree}"),
        "-m",
        "fix(core): clean substitute",
    )
    _git(repo, "replace", leak, clean)
    assert _push(leak) == 1
    _git(repo, "replace", "-d", leak)
    grafts = repo / ".git" / "info" / "grafts"
    grafts.write_text(leak + "\n")
    assert _push(leak) == 1


def test_shallow_selected_boundary_refuses_but_destination_covered_increment_passes(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _commit(repo, "fix(core): ordinary upstream history")
    upstream = _clone_upstream(repo)
    clone = repo.parent / "shallow"
    _git(repo, "clone", "--depth=1", upstream.as_uri(), str(clone))
    _git(clone, "config", "user.name", "Real Dev")
    _git(clone, "config", "user.email", "dev@example.com")
    monkeypatch.chdir(clone)
    boundary = _head(clone)
    assert _push(boundary) == 1
    _git(repo, "push", "--no-verify", sys.argv[2], "HEAD:refs/heads/main")
    assert _push(_commit(clone, "fix(core): incremental shallow work"), boundary) == 0


def test_sha256_object_format_and_full_zero_deletion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "sha256"
    repo.mkdir()
    _git(repo, "init", "-q", "--object-format=sha256", "-b", "main")
    _git(repo, "config", "user.name", "Real Dev")
    _git(repo, "config", "user.email", "dev@example.com")
    _git(repo, "commit", "--allow-empty", "--no-verify", "-m", "fix(core): generated work")
    destination = tmp_path / "destination.git"
    _git(repo, "init", "--bare", "--object-format=sha256", str(destination))
    monkeypatch.chdir(repo)
    monkeypatch.setattr(sys, "argv", ["pre-push", "destination", str(destination)])
    assert _push(_head(repo), "0" * 64) == 0
    assert _push("0" * 64, _head(repo)) == 0


@pytest.mark.parametrize("failure", ["facts", "trees", "blobs", "changed-config"])
def test_malformed_batch_or_changed_inspection_state_refuses(
    repo: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    monkeypatch.chdir(repo)
    sha = _commit_symlink(repo, "guide", "docs/guide.md")
    original = pre_push_hook._run_git
    count = 0

    def observe(args, **kwargs):
        nonlocal count
        result = original(args, **kwargs)
        if "cat-file" in args and "--batch" in args:
            count += 1
            if (failure == "facts" and count == 2) or (failure == "blobs" and count == 3):
                return subprocess.CompletedProcess(args, 0, result.stdout[:-1], b"")
        if failure == "trees" and "diff-tree" in args:
            return subprocess.CompletedProcess(args, 0, b"", b"")
        if failure == "changed-config" and "diff-tree" in args:
            _git(repo, "config", "protocol.file.allow", "never")
        return result

    monkeypatch.setattr(pre_push_hook, "_run_git", observe)
    assert _push(sha) == 1


def test_real_blobless_clone_missing_committed_symlink_never_fetches(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sha = _commit_symlink(repo, "guide", "docs/missing.md")
    blob = _git(repo, "rev-parse", f"{sha}:guide")
    upstream = _clone_upstream(repo)
    _git(upstream, "config", "uploadpack.allowFilter", "true")
    clone = repo.parent / "blobless"
    _git(repo, "clone", "--filter=blob:none", "--no-checkout", upstream.as_uri(), str(clone))
    monkeypatch.chdir(clone)
    absent_before = subprocess.run(
        ["git", "--no-lazy-fetch", "cat-file", "-e", blob],
        cwd=clone,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert absent_before.returncode != 0
    before = {
        str(path.relative_to(clone / ".git" / "objects")): path.read_bytes()
        for path in (clone / ".git" / "objects").rglob("*")
        if path.is_file()
    }
    trace = clone.parent / "inspection-trace.json"
    monkeypatch.setenv("GIT_TRACE2_EVENT", str(trace))
    assert _push(sha) == 1
    after = {
        str(path.relative_to(clone / ".git" / "objects")): path.read_bytes()
        for path in (clone / ".git" / "objects").rglob("*")
        if path.is_file()
    }
    assert before == after
    assert '"fetch"' not in trace.read_text()
    absent = subprocess.run(
        ["git", "--no-lazy-fetch", "cat-file", "-e", blob],
        cwd=clone,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert absent.returncode != 0


def test_cloned_origin_is_not_destination_authority(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = _commit(repo, "fix(core): claude imported identity")
    upstream = _clone_upstream(repo)
    clone = repo.parent / "cloned-project"
    _git(repo, "clone", str(upstream), str(clone))
    monkeypatch.chdir(clone)
    assert _push(base) == 1
    assert _push(base) == 1


def test_destination_imported_path_identity_and_allowlist_exempt_but_new_state_refuses(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    (repo / "claude.txt").write_text("imported upstream path\n")
    _git(repo, "add", "claude.txt")
    _git(
        repo,
        "commit",
        "--no-verify",
        "--author=Foreign <foreign@example.test>",
        "-m",
        "fix(core): generated upstream history",
    )
    base = _head(repo)
    upstream = _clone_upstream(repo)
    sys.argv[2] = str(upstream)
    config = repo / ".booley_project" / "booley.toml"
    config.parent.mkdir(exist_ok=True)
    config.write_text('[stealth]\nallowed_authors = ["dev@example.com"]\n')
    assert _push(base) == 0
    sha = _commit_symlink(repo, "state-link", ".booley_project/private")
    assert _push(sha) == 1


def test_destination_identity_probe_clears_current_repository_selection(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    base = _commit(repo, "fix(core): claude imported identity")
    upstream = _clone_upstream(repo)
    monkeypatch.setattr(sys, "argv", ["pre-push", "destination", str(upstream)])
    monkeypatch.setenv("GIT_DIR", str(repo / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(repo))
    assert _push(base) == 0


def test_empty_protocol_allowlist_never_discovers(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "")
    original = pre_push_hook._run_git

    def observe(args, **kwargs):
        assert "ls-remote" not in args
        return original(args, **kwargs)

    monkeypatch.setattr(pre_push_hook, "_run_git", observe)
    assert _push(_head(repo)) == 0


def test_plain_identity_footer_and_cherry_pick_remain_protected(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    base = _head(repo)
    _git(repo, "checkout", "-b", "foreign")
    leak = _commit(repo, "fix(core): clean work\n\nCo-Authored-By: Cursor Agent <cursor@local>")
    _git(repo, "checkout", "main")
    _git(repo, "cherry-pick", leak)
    assert _push(_head(repo), base) == 1


def test_symbolic_head_input_supported_without_using_it_as_object_argument(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    protocol = f"HEAD {_head(repo)} refs/heads/main {_ZERO_SHA}\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO(protocol))
    assert main() == 0


def test_source_checkout_policy_never_reads_push_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from booley.commit_policy import policy

    root = Path(__file__).resolve().parents[2]
    monkeypatch.chdir(root)

    def forbidden(*args, **kwargs):
        pytest.fail("source pre-push must never load the Project push configuration")

    monkeypatch.setattr(pre_push_hook, "validate_push_configuration", forbidden)
    assert policy.source_checkout_policy_owner(root)
    assert main() == 0


@pytest.mark.parametrize("storage", ["reference", "inherited"])
def test_minimum_git_reference_and_inherited_alternate_are_complete(
    matrix_repo: Path, monkeypatch: pytest.MonkeyPatch, storage: str
) -> None:
    repo = matrix_repo
    upstream = _clone_upstream(repo)
    clone = repo.parent / "reference"
    _git(
        repo,
        "clone",
        *(["--reference", str(upstream)] if storage == "reference" else ["--shared"]),
        str(upstream),
        str(clone),
    )
    _git(clone, "config", "user.name", "Real Dev")
    _git(clone, "config", "user.email", "dev@example.com")
    clean = _head(clone)
    leak = _commit(clone, "fix(core): claude fresh identity")
    if storage == "inherited":
        (clone / ".git" / "objects" / "info" / "alternates").unlink()
        monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", str(upstream / "objects"))
    monkeypatch.chdir(clone)
    assert _push(clean) == 0
    assert _push(leak) == 1


def test_disabled_project_does_not_load_upstream_pair(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    config = repo / ".booley_project" / "booley.toml"
    config.parent.mkdir()
    config.write_text('[stealth]\nenabled = false\nupstream_repository = "invalid"\n')

    def forbidden(*args, **kwargs):
        pytest.fail("disabled guard must not inspect push configuration")

    monkeypatch.setattr(pre_push_hook, "validate_push_configuration", forbidden)
    assert _push(_head(repo)) == 0


def test_available_advertised_object_read_error_is_not_optional_discovery_failure(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    foreign = _git(
        repo,
        "commit-tree",
        _git(repo, "rev-parse", "HEAD^{tree}"),
        "-m",
        "fix(core): separate destination history",
    )
    _git(repo, "push", "--no-verify", sys.argv[2], f"{foreign}:refs/heads/other")
    original = pre_push_hook._run_git

    def fail(args, **kwargs):
        if "cat-file" in args and kwargs.get("data") == (foreign + "\n").encode():
            return subprocess.CompletedProcess(args, 1, b"", b"")
        return original(args, **kwargs)

    monkeypatch.setattr(pre_push_hook, "_run_git", fail)
    assert _push(_head(repo)) == 1


@pytest.mark.parametrize("node", ["objects/pack", "info/grafts"])
@pytest.mark.parametrize("target", ["missing", "self"])
def test_structural_inspection_broken_node_refuses(repo: Path, node: str, target: str) -> None:
    path = repo / ".git" / node
    if path.is_dir():
        path.rmdir()
    try:
        path.symlink_to(path.name if target == "self" else "missing")
    except OSError:
        pytest.skip("platform does not support symlink creation")
    with pytest.raises((OSError, pre_push_hook.InspectionError)):
        pre_push_hook._repository_state(repo, pre_push_hook._probe_environment(), complete=True)


@pytest.mark.parametrize("label", ["HEAD~1", "@", "short", "HEAD@{0 seconds ago}"])
def test_minimum_git_typed_push_source_label_uses_only_protocol_oid(
    matrix_repo: Path, label: str
) -> None:
    repo = matrix_repo
    _commit(repo, "fix(core): second clean commit")
    source = _head(repo)[:7] if label == "short" else label
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.write_text('#!/bin/sh\ncat > "$(git rev-parse --git-dir)/protocol-receipt"\n')
    hook.chmod(0o755)
    _git(repo, "push", "--dry-run", sys.argv[2], f"{source}:refs/heads/main")
    protocol = (repo / ".git" / "protocol-receipt").read_text()
    assert protocol.rsplit(None, 3)[0] == ("HEAD" if source == "@" else source)
    with patch.object(sys, "stdin", io.StringIO(protocol)):
        assert main() == 0


def test_many_advertised_refs_use_bounded_inventory_arguments(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    oid = _head(repo)
    payload = "".join(f"{oid}\trefs/heads/branch-{index}\n" for index in range(900)).encode()
    original = pre_push_hook._run_git
    calls = []

    def advertisement(args, **kwargs):
        calls.append((args, kwargs.get("data")))
        if "ls-remote" in args:
            return subprocess.CompletedProcess(args, 0, payload, b"")
        return original(args, **kwargs)

    monkeypatch.setattr(pre_push_hook, "_run_git", advertisement)
    assert _push(oid) == 0
    inspection = pre_push_hook._Inspection(repo)
    assert inspection.inventory([oid], [oid] * 900) == []
    assert any(data and len(data) > 32767 for args, data in calls if "rev-list" in args)
    assert not any("check-ref-format" in args for args, _ in calls)
    inventories = [(args, data) for args, data in calls if "rev-list" in args]
    assert inventories and all("--stdin" in args and data for args, data in inventories)
    assert all(len(args) < 10 for args, _ in inventories)


def test_unrelated_non_utf8_git_config_preserves_push(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    with (repo / ".git" / "config").open("ab") as stream:
        stream.write(b"\n[legacy]\n value = \xff\n")
    assert _push(_head(repo)) == 0


def _clean_merge(repo):
    _git(repo, "checkout", "-b", "clean-side")
    _commit(repo, "fix(core): ordinary side work")
    _git(repo, "checkout", "main")
    _git(repo, "merge", "--no-ff", "clean-side", "-m", "fix(core): clean merge")
    return _head(repo)


def test_minimum_git_clean_unadvertised_merge(matrix_repo: Path) -> None:
    repo = matrix_repo
    merge = _clean_merge(repo)
    assert len(_git(repo, "rev-list", "--parents", "-n", "1", merge).split()) == 3
    inspection = pre_push_hook._Inspection(repo)
    frame = inspection.git(
        ["diff-tree", "--stdin", "--always", "--root", "-m", "-r", "--no-renames", "--raw", "-z"],
        data=(merge + "\n").encode(),
    ).split(b"\0", 1)[0]
    assert frame == merge.encode()
    print("real merge frame receipt:", _git(repo, "--version"), frame.decode())
    assert _push(merge) == 0
    upstream = _clone_upstream(repo)
    sys.argv[2] = str(upstream)
    local = _commit(repo, "fix(core): ordinary post-import work")
    assert _push(local) == 0


@pytest.mark.parametrize("value", ["0", "false", "", "off"])
def test_ssl_no_verify_presence_denies_authority(
    repo: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("GIT_SSL_NO_VERIFY", value)
    with pytest.raises(pre_push_hook.InspectionError, match="certificate verification"):
        pre_push_hook._secure_https(repo, "https://example.test/repo", dict(os.environ))


def test_minimum_git_tag_heavy_advertisement_batches_peeling(
    matrix_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = matrix_repo
    destination = Path(sys.argv[2])
    oid = _head(repo)
    tags = []
    for index in range(12):
        name = f"release-{index}"
        _git(repo, "tag", "-a", name, "-m", "ordinary release")
        tags.append(_git(repo, "rev-parse", name))
    _git(repo, "push", "--no-verify", str(destination), "--tags")
    original = pre_push_hook._run_git
    batches = []

    def observe(args, **kwargs):
        if "cat-file" in args and "--batch" in args:
            batches.append(kwargs.get("data"))
        return original(args, **kwargs)

    monkeypatch.setattr(pre_push_hook, "_run_git", observe)
    assert _push(oid) == 0
    assert len(batches) < len(tags)


def test_minimum_git_missing_advertised_tag_target_grants_no_exclusion(matrix_repo: Path) -> None:
    repo = matrix_repo
    destination = Path(sys.argv[2])
    target = _git(
        repo, "commit-tree", _git(repo, "rev-parse", "HEAD^{tree}"), "-m", "orphan release"
    )
    _git(repo, "tag", "-a", "withdrawn-object", target, "-m", "ordinary release")
    _git(repo, "push", "--no-verify", str(destination), "refs/tags/withdrawn-object")
    _remove_loose_object(repo, target)
    assert _push(_commit(repo, "fix(core): fresh ordinary work")) == 0
    assert _push(_commit(repo, "fix(core): claude fresh leak")) == 1


@pytest.mark.parametrize("source", ["config", "environment"])
def test_default_auto_ssh_variant_keeps_noninteractive_flags(source: str) -> None:
    config = {"ssh.variant": "auto"} if source == "config" else {}
    env = {"GIT_SSH_VARIANT": "auto"} if source == "environment" else {}
    pre_push_hook._secure_ssh(config, env)
    assert "BatchMode=yes" in env.get("GIT_SSH_COMMAND", "")
    assert "StrictHostKeyChecking=yes" in env["GIT_SSH_COMMAND"]


def test_legacy_resolve_symlink_loop_uses_destination_fallback(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(repo)
    destination = repo.parent / "looping-destination"
    monkeypatch.setattr(sys, "argv", ["pre-push", "destination", str(destination)])
    original = Path.resolve

    def legacy_loop(path, *args, **kwargs):
        if path == destination:
            raise RuntimeError("Symlink loop from selected path")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", legacy_loop)
    assert _push(_head(repo)) == 0
    assert "destination advertisement unavailable" in capsys.readouterr().err


def test_git_timeout_reports_its_actual_failure(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def timed_out(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timed_out)
    with pytest.raises(pre_push_hook.InspectionError, match="timed out"):
        pre_push_hook._run_git(["rev-list", "--stdin"], cwd=repo, timeout=1)


def test_minimum_git_encoded_file_destination_uses_git_decoded_storage(
    matrix_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = matrix_repo
    base = _commit(repo, "fix(core): claude imported history")
    upstream = _clone_upstream(repo, "encoded space.git")
    assert "%20" in upstream.as_uri()
    sys.argv[2] = upstream.as_uri()
    assert _push(base) == 0
    assert "destination advertisement unavailable" not in capsys.readouterr().err
    assert _push(_commit(repo, "fix(core): claude new local metadata")) == 1


def _destination_inspection_state(
    objects: Path, request_trace: Path
) -> tuple[dict[str, bytes], bytes]:
    files = {
        str(path.relative_to(objects)): path.read_bytes()
        for path in objects.rglob("*")
        if path.is_file()
    }
    return files, request_trace.read_bytes() if request_trace.exists() else b""


@pytest.mark.skipif(os.name == "nt", reason="invalid UTF-8 filename fixture is POSIX-specific")
def test_minimum_git_invalid_utf8_file_destination_falls_back(
    matrix_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = matrix_repo
    _clone_upstream(repo, "encoded-\ufffd.git")
    base = _commit(repo, "fix(core): ordinary local metadata")
    alias = repo.parent / os.fsdecode(b"encoded-\xff.git")
    alias.symlink_to(repo, target_is_directory=True)
    sys.argv[2] = repo.parent.as_uri() + "/encoded-%FF.git"
    original = pre_push_hook._run_git
    advertisements = []

    def observe(args, **kwargs):
        if "ls-remote" in args:
            advertisements.append(args)
        return original(args, **kwargs)

    monkeypatch.setattr(pre_push_hook, "_run_git", observe)
    request_trace = repo.parent / "invalid-encoding-requests.trace"
    monkeypatch.setenv("GIT_TRACE_PACKET", str(request_trace))
    objects = repo / ".git" / "objects"
    before = _destination_inspection_state(objects, request_trace)
    assert _push(base) == 0
    assert not advertisements
    assert before == _destination_inspection_state(objects, request_trace)
    assert "file authority requires valid UTF-8 URL encoding" in capsys.readouterr().err
    fresh = _commit(repo, "fix(core): claude fresh local metadata")
    before = _destination_inspection_state(objects, request_trace)
    assert _push(fresh) == 1
    assert not advertisements
    assert before == _destination_inspection_state(objects, request_trace)


@pytest.mark.parametrize("storage", ["self", "shared"])
def test_minimum_git_upload_pack_suffix_cannot_substitute_destination(
    matrix_repo: Path,
    storage: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = matrix_repo
    parent = repo.parent / "authority-parent"
    _git(repo, "clone", str(repo), str(parent))
    location = parent / "up"
    location.mkdir()
    base = _commit(repo, "fix(core): ordinary local metadata")
    served = repo
    if storage == "shared":
        served = repo.parent / "shared-authority.git"
        _git(repo, "clone", "--bare", "--shared", str(repo), str(served))
    (parent / "up.git").symlink_to(served, target_is_directory=True)
    # Git for Windows reports forward slashes; compare paths, not spellings.
    assert Path(_git(location, "rev-parse", "--show-toplevel")) == parent
    advertisement = _git(repo, "ls-remote", "--refs", str(location))
    assert base in advertisement
    sys.argv[2] = str(location)
    original = pre_push_hook._run_git
    advertisements = []

    def observe(args, **kwargs):
        if "ls-remote" in args:
            advertisements.append(args)
        return original(args, **kwargs)

    monkeypatch.setattr(pre_push_hook, "_run_git", observe)
    request_trace = repo.parent / "suffix-destination-requests.trace"
    monkeypatch.setenv("GIT_TRACE_PACKET", str(request_trace))
    objects = served / ("objects" if storage == "shared" else ".git/objects")
    before = _destination_inspection_state(objects, request_trace)
    assert _push(base) == 0
    assert not advertisements
    assert before == _destination_inspection_state(objects, request_trace)
    assert "file authority must name its exact repository root" in capsys.readouterr().err
    fresh = _commit(repo, "fix(core): claude fresh local metadata")
    before = _destination_inspection_state(objects, request_trace)
    assert _push(fresh) == 1
    assert not advertisements
    assert before == _destination_inspection_state(objects, request_trace)


@pytest.mark.parametrize("form", ["bare", "worktree", "gitdir", "symlink"])
def test_minimum_git_exact_file_destination_accepts_storage(
    matrix_repo: Path, form: str, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = matrix_repo
    base = _commit(repo, "fix(core): claude imported history")
    authority = repo.parent / "exact-authority"
    args = ["--bare"] if form == "bare" else []
    _git(repo, "clone", *args, str(repo), str(authority))
    if form == "gitdir":
        authority = authority / ".git"
    elif form == "symlink":
        alias = repo.parent / "independent-alias"
        alias.symlink_to(authority, target_is_directory=True)
        authority = alias
    sys.argv[2] = str(authority)
    assert _push(base) == 0
    assert "destination advertisement unavailable" not in capsys.readouterr().err
    assert _push(_commit(repo, "fix(core): claude new local metadata")) == 1


def test_file_scp_form_cannot_bypass_ssh_classification(repo: Path) -> None:
    with pytest.raises(pre_push_hook.InspectionError, match="file authority"):
        pre_push_hook._location("file:" + repo.as_posix(), repo)


def test_ssh_core_command_precedes_git_ssh_environment() -> None:
    env = {"GIT_SSH": "/usr/bin/ssh"}
    config = {"core.sshcommand": "ssh -o StrictHostKeyChecking=no"}
    with pytest.raises(pre_push_hook.InspectionError, match="host verification"):
        pre_push_hook._secure_ssh(config, env)


def test_ssh_environment_command_precedes_core_command() -> None:
    env = {"GIT_SSH_COMMAND": "ssh -o StrictHostKeyChecking=yes"}
    config = {"core.sshcommand": "ssh -o StrictHostKeyChecking=no"}
    pre_push_hook._secure_ssh(config, env)
    assert env["GIT_SSH_COMMAND"] == "ssh -o StrictHostKeyChecking=yes"


def test_minimum_git_missing_local_tag_target_has_fetch_repair(
    matrix_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = matrix_repo
    target = _git(
        repo, "commit-tree", _git(repo, "rev-parse", "HEAD^{tree}"), "-m", "ordinary release"
    )
    _git(repo, "tag", "-a", "missing-local-target", target, "-m", "ordinary release")
    tag = _git(repo, "rev-parse", "refs/tags/missing-local-target")
    _remove_loose_object(repo, target)
    assert _push(tag) == 1
    assert "fetch missing pushed history" in capsys.readouterr().err


def test_minimum_git_tilde_promisor_destination_refuses_before_discovery(
    matrix_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = matrix_repo
    base = _head(repo)
    inspected = repo / "~" / "destination.git"
    inspected.parent.mkdir()
    _git(repo, "clone", "--bare", str(repo), str(inspected))
    home = repo.parent / "selected-home"
    home.mkdir()
    served = home / "destination.git"
    _git(repo, "clone", "--bare", str(repo), str(served))
    _git(served, "config", "remote.origin.promisor", "true")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(sys, "argv", ["pre-push", "destination", "~/destination.git"])
    assert base in _git(repo, "ls-remote", "--refs", "~/destination.git", HOME=str(home))
    original = pre_push_hook._run_git
    advertisements = []

    def observe(args, **kwargs):
        if "ls-remote" in args:
            advertisements.append(args)
        return original(args, **kwargs)

    monkeypatch.setattr(pre_push_hook, "_run_git", observe)
    request_trace = repo.parent / "tilde-destination-requests.trace"
    monkeypatch.setenv("GIT_TRACE_PACKET", str(request_trace))
    before = _destination_inspection_state(served / "objects", request_trace)
    assert _push(base) == 0
    assert not advertisements
    assert (
        "home-expanded authority requires an absolute repository path" in capsys.readouterr().err
    )
    assert before == _destination_inspection_state(served / "objects", request_trace)
    assert _push(_commit(repo, "fix(core): claude new local metadata")) == 1
    assert not advertisements
    assert before == _destination_inspection_state(served / "objects", request_trace)


def test_minimum_git_mistyped_bare_authority_has_path_repair(
    matrix_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = matrix_repo
    base = _head(repo)
    upstream = _clone_upstream(repo)
    sys.argv[2] = str(upstream / "objects")
    assert _push(base) == 0
    assert "file authority must name its exact repository root" in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["tree", "blob"])
def test_minimum_git_noncommit_update_has_commit_repair(
    matrix_repo: Path, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    repo = matrix_repo
    oid = _git(repo, "rev-parse", "HEAD^{tree}" if kind == "tree" else "HEAD:file.txt")
    assert _push(oid) == 1
    assert "select a commit update and retry" in capsys.readouterr().err


def test_tilde_scp_host_remains_ssh_location(tmp_path: Path) -> None:
    assert pre_push_hook._location("~owner:repository", tmp_path) == ("ssh", None)


@pytest.mark.parametrize(
    "name",
    [
        "docs_qa/booley_notes.txt",
        "notes/booley-notes.txt",
        "notes/mybooleyfile.txt",
        "booley_config",
        "BooleyRunner.py",
    ],
)
def test_identifier_tracked_paths_blocked(repo, monkeypatch, name):
    monkeypatch.chdir(repo)
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("opaque\n")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "--no-verify", "-m", "fix: add file")
    assert any(
        "tracked path has banned terms" in offense for offense in _commit_offenses(_head(repo), [])
    )


def test_identifier_generic_path_controls(repo, monkeypatch):
    monkeypatch.chdir(repo)
    for name in ("precursor.txt", "reagent.sv"):
        (repo / name).write_text("opaque\n")
        _git(repo, "add", name)
    _git(repo, "commit", "-q", "--no-verify", "-m", "fix: add files")
    assert _commit_offenses(_head(repo), []) == []


def test_identifier_symlink_and_identities(repo, monkeypatch):
    monkeypatch.chdir(repo)
    sha = _commit_symlink(repo, "guide", "notes/mybooleyfile.txt")
    assert any("symlink target" in offense for offense in _commit_offenses(sha, []))
    _git(repo, "rm", "guide")
    _git(repo, "commit", "-q", "--no-verify", "-m", "fix: remove link")
    sha = _commit(repo, "fix: tune layout", author="mybooleyfile <safe@example.com>")
    assert any("banned terms" in offense for offense in _commit_offenses(sha, []))
    _git(repo, "config", "user.name", "BooleyRunner")
    sha = _commit(repo, "fix: tune layout", author="Safe User <safe@example.com>")
    assert any("banned terms" in offense for offense in _commit_offenses(sha, []))
    _git(repo, "config", "user.name", "Safe User")
    sha = _commit(repo, "fix: tune booley_config", author="Safe User <safe@example.com>")
    assert any("banned terms" in offense for offense in _commit_offenses(sha, []))


def test_minimum_git_pinned_upstream_import_and_fresh_metadata(
    matrix_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = matrix_repo
    base = _commit(repo, "fix(core): claude imported identity")
    upstream = _clone_upstream(repo)
    _upstream_pair(repo, upstream, base)
    assert _push(base) == 0
    assert "unavailable" not in capsys.readouterr().err
    assert _push(_commit(repo, "fix(core): clean local work")) == 0
    fresh = _commit(repo, "fix(core): claude fresh local metadata")
    # Source advancement never expands the pinned exemption to new commits.
    _git(repo, "push", "--no-verify", str(upstream), "HEAD:refs/heads/main")
    assert _push(fresh) == 1


def test_minimum_git_upstream_base_can_be_advertised_ancestor(matrix_repo: Path) -> None:
    repo = matrix_repo
    base = _commit(repo, "fix(core): claude imported identity", author="Foreign <old@source.test>")
    tip = _commit(repo, "fix(core): subsequent upstream work")
    upstream = _clone_upstream(repo)
    _upstream_pair(repo, upstream, base)
    config = repo / ".booley_project" / "booley.toml"
    config.write_text(config.read_text() + 'allowed_authors = ["*@example.com"]\n')
    assert _push(tip) == 0
    assert _push(_commit(repo, "fix(core): local work", author="Foreign <old@source.test>")) == 1
    _git(repo, "reset", "--hard", tip)
    assert _push(_commit_symlink(repo, "guide", ".booley_project/private")) == 1


@pytest.mark.parametrize("base_kind", ["fresh", "tag", "blob", "missing", "wrong-format"])
def test_minimum_git_invalid_upstream_base_refuses(
    matrix_repo: Path, base_kind: str, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = matrix_repo
    base = _commit(repo, "fix(core): claude imported identity")
    upstream = _clone_upstream(repo)
    if base_kind == "fresh":
        base = _commit(repo, "fix(core): clean but unverified baseline")
    elif base_kind == "tag":
        _git(repo, "tag", "-a", "base", "-m", "base")
        base = _git(repo, "rev-parse", "refs/tags/base")
    elif base_kind == "blob":
        base = _git(repo, "rev-parse", "HEAD:file.txt")
    elif base_kind == "missing":
        base = "f" * 40
    else:
        base = "f" * 64
    _upstream_pair(repo, upstream, base)
    assert _push(_head(repo)) == 1
    assert "upstream_base" in capsys.readouterr().err


@pytest.mark.parametrize(
    "source_kind", ["self", "shared", "empty", "missing", "relative", "promisor", "shallow"]
)
def test_minimum_git_unverifiable_upstream_refuses(
    matrix_repo: Path, source_kind: str, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = matrix_repo
    base = _commit(repo, "fix(core): claude imported identity")
    upstream = repo.parent / "upstream.git"
    if source_kind == "self":
        upstream = repo
    elif source_kind == "shared":
        _git(repo, "clone", "--bare", "--shared", str(repo), str(upstream))
    elif source_kind == "empty":
        _git(repo, "init", "--bare", str(upstream))
    elif source_kind != "missing":
        upstream = _clone_upstream(repo)
        if source_kind == "relative":
            upstream = Path("../") / upstream.name
        elif source_kind == "promisor":
            _git(upstream, "config", "remote.origin.promisor", "true")
        elif source_kind == "shallow":
            (upstream / "shallow").write_text(base + "\n")
    _upstream_pair(repo, upstream, base)
    assert _push(base) == 1
    assert "ERROR: push blocked" in capsys.readouterr().err


def test_minimum_git_upstream_uses_runtime_project_state(
    matrix_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = matrix_repo
    base = _commit(repo, "fix(core): claude imported identity")
    upstream = _clone_upstream(repo)
    _upstream_pair(repo, upstream, base)
    state = repo.parent / "runtime-state"
    (repo / ".booley_project").rename(state)
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(state))
    assert _push(base) == 0
    assert _push(_commit(repo, "fix(core): claude fresh metadata")) == 1


def test_minimum_git_native_bundled_upstream_import(matrix_repo: Path) -> None:
    from booley.harness.setup.project_git_hook_bundle import build_project_git_hook_bundle

    repo = matrix_repo
    base = _commit(repo, "fix(core): generated upstream instructions by claude")
    upstream = _clone_upstream(repo)
    _upstream_pair(repo, upstream, base)
    bundle = repo / ".booley_project" / ".managed" / "project-git-hooks.pyz"
    bundle.parent.mkdir()
    bundle.write_bytes(build_project_git_hook_bundle().content)
    hook = repo / ".git" / "hooks" / "pre-push"
    command = f'{shlex.quote(sys.executable)} -I {shlex.quote(str(bundle))} pre-push "$@"'
    hook.write_text(f"#!/bin/sh\nexec {command}\n", encoding="utf-8")
    hook.chmod(0o755)
    destination = sys.argv[2]
    _git(repo, "push", destination, "HEAD:refs/heads/main")
    assert _git(Path(destination), "rev-parse", "refs/heads/main") == base
    fresh = _commit(repo, "fix(core): claude fresh metadata")
    with pytest.raises(subprocess.CalledProcessError) as failure:
        _git(repo, "push", destination, "HEAD:refs/heads/main")
    assert "push blocked" in failure.value.stderr and fresh[:12] in failure.value.stderr
    assert _git(Path(destination), "rev-parse", "refs/heads/main") == base


@pytest.mark.parametrize(
    "location",
    ["http://example.test/repo", "git://example.test/repo", "ext::unsafe", "file:/absolute/repo"],
)
def test_upstream_unsafe_transport_refuses_before_discovery(
    repo: Path, monkeypatch: pytest.MonkeyPatch, location: str
) -> None:
    monkeypatch.chdir(repo)
    _upstream_pair(repo, Path(location))
    # Preserve URL bytes; pathlib normalizes the URL's double slash.
    config = repo / ".booley_project" / "booley.toml"
    config.write_text(
        f'[stealth]\nupstream_repository = "{location}"\nupstream_base = "{_head(repo)}"\n'
    )
    original = pre_push_hook._run_git

    def observe(args, **kwargs):
        if "ls-remote" in args:
            assert args[-1] == sys.argv[2]
        return original(args, **kwargs)

    monkeypatch.setattr(pre_push_hook, "_run_git", observe)
    assert _push(_head(repo)) == 1


def test_minimum_git_unadvertised_protected_import_refuses(matrix_repo: Path) -> None:
    repo = matrix_repo
    base = _commit(repo, "fix(core): claude imported history")
    _clone_upstream(repo)
    assert _push(base) == 1


def test_minimum_git_destination_advertised_merge_import(
    matrix_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = matrix_repo
    _commit(repo, "fix(core): claude protected ancestry")
    merge = _clean_merge(repo)
    destination = _clone_upstream(repo)
    sys.argv[2] = str(destination)
    assert _push(merge) == 0
    assert "destination advertisement unavailable" not in capsys.readouterr().err
    assert _push(_commit(repo, "fix(core): claude fresh local metadata")) == 1


@pytest.mark.parametrize("configuration", ["incomplete-pair", "malformed", "unreadable"])
def test_minimum_git_zero_active_push_needs_no_selected_config(
    matrix_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    configuration: str,
) -> None:
    repo = matrix_repo
    config = repo / ".booley_project" / "booley.toml"
    config.parent.mkdir(exist_ok=True)
    config.write_text(
        '[stealth]\nupstream_base = ""\n' if configuration == "incomplete-pair" else "[stealth]\n"
    )
    if configuration == "malformed":
        config.write_text("[stealth]\nallowed_authors = [")
    if configuration == "unreadable":
        original = Path.open

        def denied(path, *args, **kwargs):
            if path == config:
                raise PermissionError("selected config unavailable")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", denied)
    original = pre_push_hook._run_git

    def observe(args, **kwargs):
        assert "ls-remote" not in args
        return original(args, **kwargs)

    monkeypatch.setattr(pre_push_hook, "_run_git", observe)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert main() == 0
    assert _push(_ZERO_SHA, _head(repo)) == 0
    assert _push(_head(repo)) == 1
    error = capsys.readouterr().err
    assert (
        "upstream_repository" if configuration == "incomplete-pair" else "cannot read selected"
    ) in error


# ---------------------------------------------------------------------------
# Fail-closed boundaries, exercised without real Git where the input is synthetic
# ---------------------------------------------------------------------------

_OID = "a" * 40
_OTHER_OID = "b" * 40


def _fake_inspection(git_output=b"", objects=None) -> pre_push_hook._Inspection:
    """An ``_Inspection`` whose Git reads return canned bytes (no repository needed).

    *git_output* is either fixed bytes or a callable ``(args) -> bytes``;
    *objects*, when given, maps object IDs to ``(type, body)`` or ``None``.
    """
    inspection = object.__new__(pre_push_hook._Inspection)
    inspection.root = Path("synthetic-root")
    inspection.oid_length = 40
    inspection.stable = lambda: None
    inspection.git = lambda args, *, data=None: (
        git_output(args) if callable(git_output) else git_output
    )
    if objects is not None:
        inspection.objects = lambda ids: {oid: objects.get(oid) for oid in ids}
    return inspection


def _fake_protocol() -> pre_push_hook._Protocol:
    protocol = object.__new__(pre_push_hook._Protocol)
    protocol.oid_length = 40
    return protocol


def test_bundle_import_falls_back_to_flat_commit_policy_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inside the zip bundle the installed package is absent; the flat module is used."""
    import importlib.util

    flat = types.ModuleType("booley_commit_policy")
    flat.validate_push_configuration = lambda root: "flat-module"
    monkeypatch.setitem(sys.modules, "booley.commit_policy.policy", None)
    monkeypatch.setitem(sys.modules, "booley_commit_policy", flat)
    spec = importlib.util.spec_from_file_location("bundled_pre_push", pre_push_hook.__file__)
    assert spec is not None and spec.loader is not None
    bundled = importlib.util.module_from_spec(spec)
    # Dataclass creation resolves annotations through the module registry.
    monkeypatch.setitem(sys.modules, spec.name, bundled)
    spec.loader.exec_module(bundled)
    assert bundled.validate_push_configuration(Path()) == "flat-module"


def test_unlaunchable_git_is_inspection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(pre_push_hook.InspectionError, match="inspection unavailable"):
        pre_push_hook._run_git(["--version"])


def test_unlaunchable_git_means_no_repository_root(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(args, **kwargs):
        raise pre_push_hook.InspectionError("Git inspection unavailable")

    monkeypatch.setattr(pre_push_hook, "_run_git", unavailable)
    assert pre_push_hook._repository_root() is None


def test_valueless_config_entry_parses_as_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = b"Core.Bare\0user.name\nReal Dev\0"
    monkeypatch.setattr(pre_push_hook, "_required_git", lambda args, **kwargs: raw)
    values, returned = pre_push_hook._config(Path(), {})
    assert values == {"core.bare": "", "user.name": "Real Dev"}
    assert returned == raw


def test_resolve_reraises_unrelated_runtime_error() -> None:
    class Unresolvable:
        def resolve(self, strict: bool = False) -> Path:
            raise RuntimeError("unrelated failure")

    with pytest.raises(RuntimeError, match="unrelated failure"):
        pre_push_hook._resolve_repository_path(Unresolvable())


def test_object_storage_that_is_not_a_directory_refuses(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "objects-file").write_text("not a directory\n")
    original = pre_push_hook._required_git

    def objects_is_file(args, **kwargs):
        if args == ["rev-parse", "--git-path", "objects"]:
            return b"objects-file\n"
        return original(args, **kwargs)

    monkeypatch.setattr(pre_push_hook, "_required_git", objects_is_file)
    with pytest.raises(pre_push_hook.InspectionError, match="readable Git object storage"):
        pre_push_hook._repository_state(repo, pre_push_hook._probe_environment(), complete=True)


def test_unsupported_repository_format_refuses(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = pre_push_hook._config

    def future_format(cwd, env):
        values, raw = original(cwd, env)
        return {**values, "core.repositoryformatversion": "2"}, raw

    monkeypatch.setattr(pre_push_hook, "_config", future_format)
    with pytest.raises(pre_push_hook.InspectionError, match="unsupported repository format"):
        pre_push_hook._repository_state(repo, pre_push_hook._probe_environment(), complete=True)


def test_unknown_object_format_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pre_push_hook, "_required_git", lambda args, **kwargs: b"md5\n")
    with pytest.raises(pre_push_hook.InspectionError, match="unsupported Git object format"):
        pre_push_hook._Protocol(Path())


def test_unexpected_capability_probe_failure_refuses(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only Git's documented unknown-option answer means "old Git"; anything else refuses."""
    original = pre_push_hook._run_git

    def broken_probe(args, **kwargs):
        if args == ["--no-lazy-fetch", "--version"]:
            return subprocess.CompletedProcess(args, 128, b"", b"fatal: broken")
        return original(args, **kwargs)

    monkeypatch.setattr(pre_push_hook, "_run_git", broken_probe)
    with pytest.raises(pre_push_hook.InspectionError, match="inspection capability"):
        pre_push_hook._Inspection(repo)


def test_inventory_without_tips_runs_no_git() -> None:
    def forbidden(args):
        pytest.fail("an empty inventory must not query Git")

    assert _fake_inspection(forbidden).inventory([]) == []


@pytest.mark.parametrize("output", [b"not-an-oid\n", f"{_OID}\n{_OID}\n".encode()])
def test_malformed_or_duplicated_inventory_refuses(output: bytes) -> None:
    with pytest.raises(pre_push_hook.InspectionError, match="malformed outgoing commit inventory"):
        _fake_inspection(output).inventory([_OID])


@pytest.mark.parametrize(
    ("output", "message"),
    [
        (b"", "truncated Git object batch"),
        (b"garbled header\n", "malformed Git object batch"),
        (f"{_OID} missing\ntrailing".encode(), "unexpected Git object batch trailer"),
    ],
)
def test_malformed_object_batch_refuses(output: bytes, message: str) -> None:
    with pytest.raises(pre_push_hook.InspectionError, match=message):
        _fake_inspection(output).objects([_OID])


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (f"object {_OID}\ntype tag\n".encode(), "cyclic annotated tag"),
        (b"type commit\n", "malformed annotated tag"),
        (b"object not-an-oid\n", "invalid annotated tag object"),
    ],
)
def test_malformed_annotated_tag_chain_refuses(body: bytes, message: str) -> None:
    inspection = _fake_inspection(objects={_OID: ("tag", body)})
    with pytest.raises(pre_push_hook.InspectionError, match=message):
        inspection.commits([_OID])


@pytest.mark.parametrize(
    ("line", "message"),
    [
        (f"refs/heads/main {_OID} HEAD {_ZERO_SHA}", "invalid pre-push ref name"),
        (f"(delete) {_OID} refs/heads/main {_ZERO_SHA}", "invalid pre-push deletion"),
    ],
)
def test_invalid_protocol_ref_or_deletion_refuses(line: str, message: str) -> None:
    with pytest.raises(pre_push_hook.InspectionError, match=message):
        pre_push_hook._updates(line, _fake_protocol())


def test_valid_deletion_produces_no_update() -> None:
    line = f"(delete) {_ZERO_SHA} refs/heads/main {_OID}"
    assert pre_push_hook._updates(line, _fake_protocol()) == []


@pytest.mark.parametrize(
    ("location", "message"),
    [
        ("/repo with space", "invalid repository location"),
        ("file://remote-host/repo", "unsupported file authority"),
        ("file:///repo%00name", "invalid file authority path"),
        ("https:///no-host", "invalid authenticated repository URL"),
    ],
)
def test_unestablishable_location_refuses(tmp_path: Path, location: str, message: str) -> None:
    with pytest.raises(pre_push_hook.InspectionError, match=message):
        pre_push_hook._location(location, tmp_path)


@pytest.mark.parametrize(
    ("decoded", "windows", "expected"),
    [
        ("/C:/work/repo.git", True, "C:/work/repo.git"),
        ("/c:/", True, "c:/"),
        ("/C:/work/repo.git", False, "/C:/work/repo.git"),
        ("/srv/repo.git", True, "/srv/repo.git"),
        ("/C:relative", True, "/C:relative"),
    ],
)
def test_file_url_drive_letter_path_matches_git(
    decoded: str, windows: bool, expected: str
) -> None:
    """``file:///C:/x`` names drive C on Windows (as Git opens it), not ``\\C:\\x``."""
    assert pre_push_hook._file_url_path(decoded, windows=windows) == expected


def test_invalid_transport_policy_refuses(repo: Path) -> None:
    _git(repo, "config", "protocol.allow", "sometimes")
    with pytest.raises(pre_push_hook.InspectionError, match="invalid Git transport policy"):
        pre_push_hook._transport_environment(repo, "https://example.test/repo.git")


def test_unparseable_owner_ssh_command_refuses() -> None:
    with pytest.raises(pre_push_hook.InspectionError, match="invalid owner SSH command"):
        pre_push_hook._secure_ssh({"core.sshcommand": 'ssh -o "unterminated'}, {})


def test_undeterminable_https_policy_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    def config_error(args, **kwargs):
        return subprocess.CompletedProcess(args, 128, b"", b"fatal: bad config")

    monkeypatch.setattr(pre_push_hook, "_run_git", config_error)
    with pytest.raises(pre_push_hook.InspectionError, match="cannot determine HTTPS"):
        pre_push_hook._secure_https(Path(), "https://example.test/repo.git", {})


def _serve_advertisement(monkeypatch: pytest.MonkeyPatch, stdout: bytes, path=None) -> None:
    monkeypatch.setattr(pre_push_hook, "_transport_environment", lambda root, location: ({}, path))
    monkeypatch.setattr(
        pre_push_hook,
        "_run_git",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 0, stdout, b""),
    )


def test_invalid_advertised_ref_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve_advertisement(monkeypatch, f"{_OID}\trefs/heads/bad..name\n".encode())
    with pytest.raises(pre_push_hook.InspectionError, match="invalid advertised ref"):
        pre_push_hook._advertised(_fake_inspection(), "https://example.test/repo.git")


def test_file_authority_changed_during_advertisement_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve_advertisement(monkeypatch, f"{_OID}\trefs/heads/main\n".encode(), Path("authority"))
    states = iter(["before", "after"])
    monkeypatch.setattr(pre_push_hook, "_file_authority_state", lambda path, env: next(states))
    with pytest.raises(pre_push_hook.InspectionError, match="file authority changed"):
        pre_push_hook._advertised(_fake_inspection(), "authority")


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (None, "missing inspected commit object"),
        (("tree", b""), "missing inspected commit object"),
        (("commit", b"tree x\nauthor A <a@x> 1 +0000"), "malformed commit object"),
        (("commit", b"tree x\n\nmessage"), "missing commit identities"),
        (
            ("commit", b"author no-email\ncommitter C <c@x> 1 +0000\n\nmessage"),
            "malformed commit identity",
        ),
    ],
)
def test_malformed_commit_object_refuses(value, message: str) -> None:
    with pytest.raises(pre_push_hook.InspectionError, match=message):
        pre_push_hook._parse_commit(value)


@pytest.mark.parametrize(
    ("output", "message"),
    [
        (b"unterminated", "truncated changed-tree batch"),
        (f"{_OTHER_OID}\0".encode(), "unexpected changed-tree commit frame"),
        (f":000000 100644 {_ZERO_SHA} {_OID} A\0path\0".encode(), "missing changed-tree frame"),
        (f"{_OID}\0:000000 100644 short ids A\0path\0".encode(), "malformed changed-tree entry"),
        (
            f"{_OID}\0:000000 100644 {_ZERO_SHA} {_OTHER_OID} A\0../escape\0".encode(),
            "invalid committed path",
        ),
    ],
)
def test_malformed_changed_tree_batch_refuses(output: bytes, message: str) -> None:
    with pytest.raises(pre_push_hook.InspectionError, match=message):
        pre_push_hook._batch_trees(_fake_inspection(output), [_OID])


@pytest.mark.parametrize(
    ("output", "message"),
    [
        (b"", "truncated symlink size batch"),
        (f"{_OID} blob {64 * 1024 + 1}\n".encode(), "oversized symlink target"),
    ],
)
def test_unsafe_symlink_size_batch_refuses(output: bytes, message: str) -> None:
    with pytest.raises(pre_push_hook.InspectionError, match=message):
        pre_push_hook._check_symlink_sizes(_fake_inspection(output), {_OID})


def test_unreadable_committed_symlink_blob_refuses() -> None:
    def git(args):
        if args[0] == "diff-tree":
            return f"{_OID}\0:000000 120000 {_ZERO_SHA} {_OTHER_OID} A\0link\0".encode()
        return f"{_OTHER_OID} blob 5\n".encode()

    commit = ("commit", b"author A <a@x> 1 +0000\ncommitter A <a@x> 1 +0000\n\nfix: link")
    inspection = _fake_inspection(git, objects={_OID: commit, _OTHER_OID: None})
    with pytest.raises(pre_push_hook.InspectionError, match="committed symlink target"):
        pre_push_hook._inspect_commits(inspection, [_OID], [], None, None)


def test_unreadable_commit_facts_are_an_offense(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    monkeypatch.setattr(pre_push_hook, "_commit_facts", lambda sha: None)
    assert _commit_offenses(_OID, [], repository_root=repo) == ["cannot inspect commit facts"]


def test_unresolvable_root_is_an_offense(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo)
    monkeypatch.setattr(pre_push_hook, "_repository_root", lambda: None)
    monkeypatch.setattr(
        pre_push_hook,
        "_commit_facts",
        lambda sha: ("Real Dev", "dev@example.com", "Real Dev", "dev@example.com", "fix: x"),
    )
    policy = pre_push_hook.stealth_policy(repo)
    assert policy.enabled
    assert _commit_offenses(_OID, [], policy=policy) == [
        "cannot resolve repository root to inspect tracked metadata"
    ]


def test_active_push_without_destination_arguments_refuses(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(repo)
    monkeypatch.setattr(sys, "argv", ["pre-push"])
    assert _push(_head(repo)) == 1
    assert "active push requires destination name and location" in capsys.readouterr().err
