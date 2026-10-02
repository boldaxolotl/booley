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
import shutil
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
    (root / "file.txt").write_text("hello\n", encoding="utf-8")
    _git(root, "add", "file.txt")
    _git(root, "commit", "-q", "--no-verify", "-m", "feat(core): add the file")
    destination = tmp_path / "destination.git"
    _git(root, "init", "--bare", str(destination))
    monkeypatch.setattr(sys, "argv", ["pre-push", "destination", str(destination)])
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
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
        # Isolate the tracked-path contract from strict configuration-node validation.
        monkeypatch.setattr(pre_push_hook, "upstream_record", lambda root: None)
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


def test_recorded_upstream_first_push_and_fresh_identity(repo, monkeypatch, tmp_path):
    upstream = tmp_path / "upstream.git"
    destination = tmp_path / "import-destination.git"
    _git(repo, "commit", "--allow-empty", "--no-verify", "-m", "fix(core): generated by claude")
    base = _head(repo)
    _git(repo, "clone", "--bare", str(repo), str(upstream))
    _git(repo, "init", "--bare", str(destination))
    config = repo / ".booley_project" / "booley.toml"
    config.parent.mkdir()
    config.write_text(
        f'[stealth]\nupstream_repository = "{upstream.as_posix()}"\nupstream_base = "{base}"\n'
    )
    monkeypatch.chdir(repo)
    monkeypatch.setattr(sys, "argv", ["pre-push", "destination", str(destination)])
    monkeypatch.delenv("BOOLEY_SKIP_PUSH_GUARD", raising=False)
    with _stdin(base):
        assert main() == 0
    fresh = _commit(repo, "fix(core): paired with claude")
    with _stdin(fresh):
        assert main() == 1


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


def _record(repo, upstream, base=None):
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


def test_minimum_git_first_push_incremental_and_fresh_leak(matrix_repo):
    repo = matrix_repo
    base = _commit(repo, "fix(core): generated instructions and docker verification agent")
    assert _push(base) == 0
    destination = Path(sys.argv[2])
    _git(repo, "push", "--no-verify", str(destination), "HEAD:refs/heads/main")
    incremental = _commit(repo, "fix(core): generated with care")
    assert _push(incremental, base) == 0
    assert _push(_commit(repo, "fix(core): claude metadata"), base) == 1


def test_minimum_git_recorded_import_and_new_metadata(matrix_repo):
    repo = matrix_repo
    base = _commit(
        repo, "fix(core): generated by claude", author="Foreign <foreign@elsewhere.test>"
    )
    upstream = _clone_upstream(repo)
    _record(repo, upstream, base)
    config = repo / ".booley_project" / "booley.toml"
    config.write_text(config.read_text() + 'allowed_authors = ["*@example.com"]\n')
    assert _push(base) == 0
    assert _push(_commit(repo, "fix(core): clean", author="Foreign <foreign@elsewhere.test>")) == 1
    assert _push(_commit(repo, "fix(core): co-authored-by: Cursor Agent")) == 1


@pytest.mark.parametrize(
    "state", ["promisor", "filter", "partial", "include", "environment", "marker"]
)
def test_minimum_git_partial_state_preflight(matrix_repo, monkeypatch, state):
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
def test_minimum_git_complete_alternate_and_symlink_storage(matrix_repo, monkeypatch, storage):
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
def test_minimum_git_missing_inspected_objects_refuse(matrix_repo, missing):
    repo = matrix_repo
    sha = _commit_symlink(repo, "guide", "docs/guide.md")
    oid = (
        sha
        if missing == "commit"
        else _git(repo, "rev-parse", f"{sha}^{{tree}}" if missing == "tree" else f"{sha}:guide")
    )
    (repo / ".git" / "objects" / oid[:2] / oid[2:]).unlink()
    assert _push(sha) == 1


@pytest.mark.parametrize("authority", ["self", "symlink", "shared"])
def test_minimum_git_upstream_requires_independent_storage(matrix_repo, authority):
    repo = matrix_repo
    base = _commit(repo, "fix(core): claude imported history")
    if authority == "self":
        upstream = repo
    elif authority == "symlink":
        upstream = repo.parent / "alias"
        upstream.symlink_to(repo, target_is_directory=True)
    else:
        upstream = repo.parent / "shared.git"
        _git(repo, "clone", "--bare", "--shared", str(repo), str(upstream))
    _record(repo, upstream, base)
    assert _push(base) == 1


def test_destination_other_branch_is_authoritative(repo, monkeypatch):
    monkeypatch.chdir(repo)
    base = _commit(repo, "fix(core): claude imported history")
    _git(repo, "push", "--no-verify", sys.argv[2], "HEAD:refs/heads/other")
    assert _push(base) == 0


def test_unrelated_and_stale_remote_refs_never_exempt(repo, monkeypatch):
    monkeypatch.chdir(repo)
    base = _commit(repo, "fix(core): claude local history")
    _git(repo, "update-ref", "refs/remotes/unrelated/main", base)
    assert _push(base) == 1


@pytest.mark.parametrize("failure", ["timeout", "auth", "malformed", "denied"])
def test_destination_failure_complete_superset(repo, monkeypatch, capsys, failure):
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
def test_fallback_inspection_failure_refuses(repo, monkeypatch, kind):
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
def test_malformed_protocol_refuses_without_discovery(repo, monkeypatch, line):
    monkeypatch.chdir(repo)
    line = line.format(oid=_head(repo), zero=_ZERO_SHA)
    original = pre_push_hook._run_git

    def observe(args, **kwargs):
        assert "ls-remote" not in args
        return original(args, **kwargs)

    monkeypatch.setattr(pre_push_hook, "_run_git", observe)
    monkeypatch.setattr(sys, "stdin", io.StringIO(line))
    assert main() == 1


def test_no_update_and_deletion_avoid_network(repo, monkeypatch):
    monkeypatch.chdir(repo)
    _record(repo, repo.parent / "offline.git", "f" * 40)
    original = pre_push_hook._run_git

    def observe(args, **kwargs):
        assert "ls-remote" not in args
        return original(args, **kwargs)

    monkeypatch.setattr(pre_push_hook, "_run_git", observe)
    with patch("sys.stdin", io.StringIO("")):
        assert main() == 0
    assert _push(_ZERO_SHA, _head(repo)) == 0
    assert _push(_head(repo)) == 1


def test_recorded_base_offline_applicable_and_disjoint(repo, monkeypatch):
    monkeypatch.chdir(repo)
    base = _head(repo)
    _record(repo, repo.parent / "offline.git", base)
    assert _push(base) == 1
    _git(repo, "checkout", "--orphan", "orphan")
    _git(repo, "commit", "--no-verify", "-m", "fix(core): independent history")
    assert _push(_head(repo)) == 0


def test_destination_covered_base_avoids_upstream_lookup(repo, monkeypatch):
    monkeypatch.chdir(repo)
    base = _head(repo)
    _record(repo, repo.parent / "offline.git", base)
    _git(repo, "push", "--no-verify", sys.argv[2], "HEAD:refs/heads/main")
    assert _push(_commit(repo, "fix(core): new work"), base) == 0


def test_missing_old_destination_tip_conservative(repo, monkeypatch):
    monkeypatch.chdir(repo)
    assert _push(_head(repo), "f" * 40) == 0
    assert _push(_commit(repo, "fix(core): claude fresh work"), "f" * 40) == 1


@pytest.mark.parametrize("policy", ["never", "user"])
def test_effective_protocol_policy_cannot_be_widened(repo, monkeypatch, policy):
    monkeypatch.chdir(repo)
    upstream = _clone_upstream(repo)
    _git(repo, "config", "protocol.file.allow", policy)
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file:ssh:https")
    monkeypatch.setenv("GIT_PROTOCOL_FROM_USER", "0")
    original = pre_push_hook._run_git

    def observe(args, **kwargs):
        assert "ls-remote" not in args
        return original(args, **kwargs)

    monkeypatch.setattr(pre_push_hook, "_run_git", observe)
    assert _push(_head(repo)) == 0
    _record(repo, upstream)
    assert _push(_head(repo)) == 1


def test_url_rewriting_denies_authority(repo, monkeypatch):
    monkeypatch.chdir(repo)
    _git(repo, "config", "url./untrusted/.insteadOf", sys.argv[2])
    base = _commit(repo, "fix(core): claude historical work")
    assert _push(base) == 1


def test_deep_history_oldest_leak_and_bounded_batches(repo, monkeypatch):
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
def test_bypassed_trailer_remains_blocked(repo, monkeypatch, trail):
    monkeypatch.chdir(repo)
    assert _push(_commit(repo, f"fix(core): ordinary work\n\n{trail}")) == 1


@pytest.mark.parametrize("authority", ["destination", "upstream"])
def test_minimum_git_file_promisor_authority_preflight_before_upload_pack(
    matrix_repo, monkeypatch, authority
):
    repo = matrix_repo
    upstream = _clone_upstream(repo)
    _git(upstream, "config", "remote.origin.promisor", "false")
    if authority == "destination":
        monkeypatch.setattr(sys, "argv", ["pre-push", "destination", str(upstream)])
    else:
        _record(repo, upstream)
    original = pre_push_hook._run_git
    calls = []

    def observe(args, **kwargs):
        if "ls-remote" in args:
            calls.append(args[-1])
        return original(args, **kwargs)

    monkeypatch.setattr(pre_push_hook, "_run_git", observe)
    assert _push(_head(repo)) == (0 if authority == "destination" else 1)
    assert str(upstream) not in calls


@pytest.mark.parametrize(
    "endpoint",
    ["http://example.test/repo", "git://example.test/repo", "ext::helper", "helper::repo"],
)
def test_unsafe_transport_never_executes_helper(repo, monkeypatch, endpoint):
    monkeypatch.chdir(repo)
    monkeypatch.setattr(sys, "argv", ["pre-push", "destination", endpoint])
    original = pre_push_hook._run_git

    def observe(args, **kwargs):
        assert "ls-remote" not in args
        return original(args, **kwargs)

    monkeypatch.setattr(pre_push_hook, "_run_git", observe)
    assert _push(_head(repo)) == 0


@pytest.mark.parametrize("kind", ["global", "include", "environment"])
def test_merged_protocol_restrictions_are_respected(repo, monkeypatch, kind):
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
def test_https_detectable_security_override_denies_authority(repo, monkeypatch, override):
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
def test_ssh_detectable_insecure_options_deny_authority(repo, monkeypatch, command):
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
def test_ssh_owner_wrapper_preserved_and_default_strict(repo, monkeypatch, custom):
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


def test_complete_stdin_consumed_and_lookup_has_devnull(repo, monkeypatch):
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


def test_annotated_tags_older_import_and_noncommit_updates(repo, monkeypatch):
    monkeypatch.chdir(repo)
    older = _head(repo)
    base = _commit(repo, "fix(core): claude imported identity")
    upstream = _clone_upstream(repo)
    _record(repo, upstream, base)
    _git(repo, "tag", "-a", "older", older, "-m", "upstream release")
    assert _push(_git(repo, "rev-parse", "older")) == 0
    assert _push(_git(repo, "rev-parse", "HEAD^{tree}")) == 1


def test_multiref_new_tips_do_not_exempt_each_other(repo, monkeypatch):
    monkeypatch.chdir(repo)
    leak = _commit(repo, "fix(core): claude local work")
    clean = _commit(repo, "fix(core): ordinary local work")
    protocol = f"refs/heads/a {leak} refs/heads/a {_ZERO_SHA}\nrefs/heads/b {clean} refs/heads/b {_ZERO_SHA}\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO(protocol))
    assert main() == 1


def test_merge_exposes_new_side_branch_commit(repo, monkeypatch):
    monkeypatch.chdir(repo)
    base = _head(repo)
    _git(repo, "push", "--no-verify", sys.argv[2], "HEAD:refs/heads/main")
    _git(repo, "checkout", "-b", "side")
    _commit(repo, "fix(core): claude side identity")
    _git(repo, "checkout", "main")
    _git(repo, "merge", "--no-ff", "side", "-m", "fix(core): merge side")
    assert _push(_head(repo), base) == 1


def test_replacement_does_not_hide_identity_and_active_grafts_refuse(repo, monkeypatch):
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
    repo, monkeypatch
):
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


def test_sha256_object_format_and_full_zero_deletion(tmp_path, monkeypatch):
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
def test_malformed_batch_or_changed_inspection_state_refuses(repo, monkeypatch, failure):
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


def test_real_blobless_clone_missing_committed_symlink_never_fetches(repo, monkeypatch):
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


@pytest.mark.parametrize("failure", ["withdrawn", "missing", "tag"])
def test_recorded_upstream_provenance_failures_refuse(repo, monkeypatch, failure):
    monkeypatch.chdir(repo)
    base = _commit(repo, "fix(core): claude imported identity")
    upstream = _clone_upstream(repo)
    if failure == "withdrawn":
        _git(upstream, "update-ref", "-d", "refs/heads/main")
    elif failure == "missing":
        upstream = repo.parent / "unavailable.git"
    else:
        _git(repo, "tag", "-a", "base", "-m", "imported base")
        base = _git(repo, "rev-parse", "base")
    _record(repo, upstream, base)
    assert _push(_head(repo)) == 1


def test_cloned_origin_is_not_authority_until_owner_records_pair(repo, monkeypatch):
    base = _commit(repo, "fix(core): claude imported identity")
    upstream = _clone_upstream(repo)
    clone = repo.parent / "cloned-project"
    _git(repo, "clone", str(upstream), str(clone))
    monkeypatch.chdir(clone)
    assert _push(base) == 1
    _record(clone, upstream, base)
    assert _push(base) == 0


def test_imported_path_identity_and_allowlist_exempt_but_new_state_refuses(repo, monkeypatch):
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
    _record(repo, upstream, base)
    config = repo / ".booley_project" / "booley.toml"
    config.write_text(config.read_text() + 'allowed_authors = ["dev@example.com"]\n')
    assert _push(base) == 0
    sha = _commit_symlink(repo, "state-link", ".booley_project/private")
    assert _push(sha) == 1


def test_current_worktree_and_shared_object_alias_are_not_upstream_authority(repo, monkeypatch):
    monkeypatch.chdir(repo)
    base = _commit(repo, "fix(core): claude imported identity")
    worktree = repo.parent / "linked-upstream"
    _git(repo, "worktree", "add", "--detach", str(worktree), base)
    _record(repo, worktree, base)
    assert _push(base) == 1
    independent = _clone_upstream(repo)
    objects = independent / "objects"
    shutil.rmtree(objects)
    objects.symlink_to(repo / ".git" / "objects", target_is_directory=True)
    _record(repo, independent, base)
    assert _push(base) == 1


def test_upstream_identity_probe_clears_current_repository_selection(repo, monkeypatch):
    monkeypatch.chdir(repo)
    base = _commit(repo, "fix(core): claude imported identity")
    upstream = _clone_upstream(repo)
    _record(repo, upstream, base)
    monkeypatch.setenv("GIT_DIR", str(repo / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(repo))
    assert _push(base) == 0


def test_empty_protocol_allowlist_never_discovers(repo, monkeypatch):
    monkeypatch.chdir(repo)
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "")
    original = pre_push_hook._run_git

    def observe(args, **kwargs):
        assert "ls-remote" not in args
        return original(args, **kwargs)

    monkeypatch.setattr(pre_push_hook, "_run_git", observe)
    assert _push(_head(repo)) == 0


def test_plain_identity_footer_and_cherry_pick_remain_protected(repo, monkeypatch):
    monkeypatch.chdir(repo)
    base = _head(repo)
    _git(repo, "checkout", "-b", "foreign")
    leak = _commit(repo, "fix(core): clean work\n\nCo-Authored-By: Cursor Agent <cursor@local>")
    _git(repo, "checkout", "main")
    _git(repo, "cherry-pick", leak)
    assert _push(_head(repo), base) == 1


def test_symbolic_head_input_supported_without_using_it_as_object_argument(repo, monkeypatch):
    monkeypatch.chdir(repo)
    protocol = f"HEAD {_head(repo)} refs/heads/main {_ZERO_SHA}\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO(protocol))
    assert main() == 0


def test_source_checkout_policy_never_reads_recorded_pair(monkeypatch):
    from booley.commit_policy import policy

    root = Path(__file__).resolve().parents[2]
    monkeypatch.chdir(root)

    def forbidden(*args, **kwargs):
        pytest.fail("source pre-push must never load the Project upstream record")

    monkeypatch.setattr(pre_push_hook, "upstream_record", forbidden)
    assert policy.source_checkout_policy_owner(root)
    assert main() == 0


@pytest.mark.parametrize("storage", ["reference", "inherited"])
def test_minimum_git_reference_and_inherited_alternate_are_complete(
    matrix_repo, monkeypatch, storage
):
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


def test_disabled_project_does_not_load_upstream_record(repo, monkeypatch):
    monkeypatch.chdir(repo)
    config = repo / ".booley_project" / "booley.toml"
    config.parent.mkdir()
    config.write_text('[stealth]\nenabled = false\nupstream_repository = "invalid"\n')

    def forbidden(*args, **kwargs):
        pytest.fail("disabled guard must not inspect upstream state")

    monkeypatch.setattr(pre_push_hook, "upstream_record", forbidden)
    assert _push(_head(repo)) == 0


def test_available_advertised_object_read_error_is_not_optional_discovery_failure(
    repo, monkeypatch
):
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
def test_structural_inspection_broken_node_refuses(repo, node, target):
    path = repo / ".git" / node
    if path.is_dir():
        path.rmdir()
    try:
        path.symlink_to(path.name if target == "self" else "missing")
    except OSError:
        pytest.skip("platform does not support symlink creation")
    with pytest.raises((OSError, pre_push_hook.InspectionError)):
        pre_push_hook._repository_state(repo, pre_push_hook._probe_environment(), complete=True)


@pytest.mark.parametrize("label", ["HEAD~1", "@", "short"])
def test_minimum_git_typed_push_source_label_uses_only_protocol_oid(matrix_repo, label):
    repo = matrix_repo
    _commit(repo, "fix(core): second clean commit")
    source = _head(repo)[:7] if label == "short" else label
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.write_text('#!/bin/sh\ncat > "$(git rev-parse --git-dir)/protocol-receipt"\n')
    hook.chmod(0o755)
    _git(repo, "push", "--dry-run", sys.argv[2], f"{source}:refs/heads/main")
    protocol = (repo / ".git" / "protocol-receipt").read_text()
    assert protocol.split()[0] == ("HEAD" if source == "@" else source)
    with patch.object(sys, "stdin", io.StringIO(protocol)):
        assert main() == 0


def test_many_advertised_refs_use_bounded_inventory_arguments(repo, monkeypatch):
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


def test_unrelated_non_utf8_git_config_preserves_push(repo, monkeypatch):
    monkeypatch.chdir(repo)
    with (repo / ".git" / "config").open("ab") as stream:
        stream.write(b"\n[legacy]\n value = \xff\n")
    assert _push(_head(repo)) == 0
