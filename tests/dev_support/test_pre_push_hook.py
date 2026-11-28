"""Tests for pre_push_hook — the last-chance guard on outgoing commits.

Exercised against real git repositories rather than mocked ``git log`` output:
the whole point of this hook is that it reads fields (author, committer) which
no earlier hook can see, so the parse of git's real output is the part worth
testing. Where a test must corrupt or fail one Git answer, it does so through the
shared ``GitCommandFake`` and asserts the refusal ``main()`` prints for it.
"""

from __future__ import annotations

import io
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import types
from collections.abc import Callable, Sequence
from pathlib import Path
from unittest.mock import patch

import pytest
from tests.conftest import add_git_command_config
from tests.git_command_fake import GitCommandFake, with_stdout
from tests.subprocess_budget import run_bounded_child
from tests.time_budget import hang_guard_s

from booley.dev_support import pre_push_hook
from booley.dev_support.pre_push_hook import main

_ZERO_SHA = "0" * 40


def _git(repo: Path, *args: str, **env_overrides: str) -> str:
    """Run git in *repo*, returning stdout; raises on failure."""
    env = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "PATH": os.environ.get("BOOLEY_TEST_GIT_PATH", "/usr/bin:/bin:/usr/local/bin"),
        **env_overrides,
    }
    proc = run_bounded_child(["git", "-C", str(repo), *args], env=env, check=True)
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
    _enable_stealth(root / ".booley")
    destination = tmp_path / "destination.git"
    _git(root, "init", "--bare", str(destination))
    monkeypatch.setattr(sys, "argv", ["pre-push", "destination", str(destination)])
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.chdir(root)
    return root


def _enable_stealth(
    booley_dir: Path, *, enabled: bool = True, allowed: Sequence[str] = ()
) -> None:
    """Write the Project's Stealth choice; the leak guard runs only when a Project enables it."""
    booley_dir.mkdir(parents=True, exist_ok=True)
    lines = ["[stealth]", f"enabled = {str(enabled).lower()}"]
    if allowed:
        lines.append(f"allowed_authors = {json.dumps(list(allowed))}")
    (booley_dir / "booley.toml").write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
    )


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def _commit(repo: Path, message: str, *, author: str | None = None, **env: str) -> str:
    """Add a commit carrying *message*, under a forged *author* or *env* identity when given."""
    path = repo / "file.txt"
    path.write_text(path.read_text(encoding="utf-8") + "more\n", encoding="utf-8")
    _git(repo, "add", "file.txt")
    args = ["commit", "-q", "--no-verify", "-m", message]
    if author is not None:
        args.append(f"--author={author}")
    _git(repo, *args, **env)
    return _head(repo)


def _commit_file(repo: Path, name: str, text: str = "opaque\n", message: str = "docs: add") -> str:
    """Commit one new file *name*, creating its directories, and return the commit."""
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "--no-verify", "-m", message)
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
    monkeypatch.delenv("BOOLEY_SKIP_PUSH_GUARD", raising=False)
    booley_dir = repo.parent / "runtime-project-state"
    booley_dir.mkdir()
    monkeypatch.setenv("BOOLEY_DIR", str(booley_dir))


# private: the hook-parent fallback runs only when the hook file sits in `hooks/` and Booley is
# unimportable, which `main()` from this checkout never is.
def test_booley_dir_fallback_uses_hook_parent_when_runtime_is_unavailable(tmp_path, monkeypatch):
    hook_dir = tmp_path / "hooks"
    hook_dir.mkdir()
    monkeypatch.setattr(pre_push_hook, "__file__", str(hook_dir / "pre-push"))
    runtime_module = types.ModuleType("booley.runtime.booley_dir")

    def unavailable(_repository_root: Path) -> Path:
        raise FileNotFoundError

    runtime_module.resolve_checkout_booley_dir = unavailable
    monkeypatch.setitem(sys.modules, "booley.runtime.booley_dir", runtime_module)

    assert pre_push_hook._guard_booley_dir(tmp_path / "repo") == tmp_path


def _forbid_discovery(monkeypatch) -> GitCommandFake:
    """Fail the test the moment the hook asks a destination for its advertisement."""
    fake = GitCommandFake.install(monkeypatch)
    fake.crash("ls-remote", error=AssertionError("destination discovery must not run"))
    return fake


def _stdin(local_sha: str, remote_sha: str = _ZERO_SHA):
    """Patch stdin with one git-supplied pre-push ref line."""
    line = f"refs/heads/main {local_sha} refs/heads/main {remote_sha}\n"
    return patch("sys.stdin", io.StringIO(line))


class TestMain:
    @pytest.mark.parametrize(
        "scenario", ["clean", "escape-hatch", "stealth-off", "deletion", "second-pattern"]
    )
    def test_allows_push(self, repo, monkeypatch, scenario):
        """Each way past the guard returns 0 although an unlisted author is outgoing."""
        monkeypatch.delenv("BOOLEY_SKIP_PUSH_GUARD", raising=False)
        clean = _head(repo)
        sha = _commit(repo, "test(mut): mutation muxes", author="mut-creator <mut@local>")
        _enable_stealth(
            repo / ".booley",
            enabled=scenario != "stealth-off",
            allowed=["*@example.com", "mut@local"]
            if scenario == "second-pattern"
            else ["*@example.com"],
        )
        if scenario == "escape-hatch":
            monkeypatch.setenv("BOOLEY_SKIP_PUSH_GUARD", "1")
        if scenario == "deletion":  # a delete push has no outgoing commits to check
            assert _push(_ZERO_SHA, sha) == 0
        else:
            assert _push(clean if scenario == "clean" else sha) == 0

    @pytest.mark.parametrize(
        ("author", "committer", "offense"),
        [
            (
                "mut-creator <mut@local>",
                {},
                "author not in [stealth] allowed_authors: mut-creator <mut@local>",
            ),
            (
                None,
                {"GIT_COMMITTER_NAME": "mut-committer", "GIT_COMMITTER_EMAIL": "mut@local"},
                "committer not in [stealth] allowed_authors: mut-committer <mut@local>",
            ),
        ],
        ids=["author", "committer"],
    )
    def test_blocks_unlisted_author(self, repo, monkeypatch, capsys, author, committer, offense):
        monkeypatch.delenv("BOOLEY_SKIP_PUSH_GUARD", raising=False)
        _enable_stealth(repo / ".booley", allowed=["*@example.com"])
        sha = _commit(repo, "test(mut): mutation muxes", author=author, **committer)
        assert _push(sha) == 1
        err = capsys.readouterr().err
        assert "push blocked" in err
        assert sha[:12] in err
        assert offense in err

    @pytest.mark.parametrize(
        ("variant", "target"),
        [
            ("checkout", ".booley/AGENTS.md"),
            ("swapped-link", ".booley/AGENTS.md"),
            ("external-alias", ".booley/AGENTS.md"),
            ("runtime-state", "../runtime-project-state/AGENTS.md"),
            ("ordinary", "docs/guide.md"),
        ],
    )
    def test_blocks_symlink_to_project_state(self, repo, monkeypatch, capsys, variant, target):
        monkeypatch.delenv("BOOLEY_SKIP_PUSH_GUARD", raising=False)
        if variant in ("external-alias", "runtime-state"):  # state mounted outside the checkout
            _configure_external_project_state(repo, monkeypatch)
        sha = _commit_symlink(repo, "AGENTS.md", target)
        if variant == "swapped-link":  # the committed target decides, not the checkout's link
            (repo / "AGENTS.md").unlink()
            (repo / "AGENTS.md").symlink_to("ordinary.txt")
        if variant == "ordinary":
            assert _push(sha) == 0
        else:
            _blocked(capsys, sha, f"symlink target exposes project state: AGENTS.md -> {target}")

    @pytest.mark.parametrize(
        "relative_path", [".booley/docs/example.md", ".BOOLEY/docs/example.md", ".booley"]
    )
    def test_blocks_tracked_state_with_external_alias(
        self, repo, monkeypatch, capsys, relative_path
    ):
        _configure_external_project_state(repo, monkeypatch)
        if relative_path == ".booley":
            # Isolate tracked paths from strict configuration-node validation; a
            # file at `.booley` holds no Stealth choice, so assert the policy directly.
            monkeypatch.setattr(pre_push_hook, "validate_push_configuration", lambda root: None)
            monkeypatch.setattr(pre_push_hook, "stealth_enabled", lambda root: True)
            shutil.rmtree(repo / ".booley")
        assert _push(_commit_file(repo, relative_path, "private project state\n")) == 1
        assert f"tracked path exposes project state: {relative_path}" in capsys.readouterr().err

    def test_blocks_tracked_relocated_state_with_external_alias(self, repo, monkeypatch, capsys):
        """A relative ``[project].dir`` names checkout state just as ``.booley`` does."""
        _configure_external_project_state(repo, monkeypatch)
        monkeypatch.setattr(pre_push_hook, "validate_push_configuration", lambda root: None)
        (repo / "booley.toml").write_text('[project]\ndir = "state"\n', encoding="utf-8")
        _enable_stealth(repo / "state")  # the relocated Booley Directory owns the choice
        sha = _commit_file(repo, "state/docs/example.md", "private project state\n")

        assert _push(sha) == 1
        assert (
            "tracked path exposes project state: state/docs/example.md" in capsys.readouterr().err
        )

    def test_booley_dir_equal_to_the_checkout_does_not_flag_every_path(self, repo, monkeypatch):
        _configure_external_project_state(repo, monkeypatch)
        monkeypatch.setattr(pre_push_hook, "validate_push_configuration", lambda root: None)
        (repo / "booley.toml").write_text('[project]\ndir = "."\n', encoding="utf-8")

        assert _push(_commit(repo, "docs: add notes")) == 0

    @pytest.mark.parametrize(
        "relative_path", [".booley_backup/example.md", "docs/.booley/example.md"]
    )
    def test_allows_checkout_project_state_name_lookalikes(self, repo, monkeypatch, relative_path):
        _configure_external_project_state(repo, monkeypatch)
        config = repo / ".booley" / "booley.toml"
        config.parent.mkdir(exist_ok=True)
        config.write_text("[stealth]\nenabled = true\nbanned_words = []\n")
        sha = _commit_file(repo, relative_path, "ordinary docs\n")

        with _stdin(sha):
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
        version = run_bounded_child([executable, "--version"], check=True)
        assert re.fullmatch(r"git version 2\.37\.2(?:\.windows\.\d+)?\n?", version.stdout)
        probe = run_bounded_child([executable, "--no-lazy-fetch", "--version"])
        assert probe.returncode == 129 and "unknown option: --no-lazy-fetch" in probe.stderr
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
    return repo


def _upstream_pair(repo, upstream, base=None):
    config = repo / ".booley" / "booley.toml"
    config.parent.mkdir(exist_ok=True)
    location = upstream.as_posix() if isinstance(upstream, Path) else upstream
    config.write_text(
        f'[stealth]\nenabled = true\nupstream_repository = "{location}"\nupstream_base = "{base or _head(repo)}"\n',
        encoding="utf-8",
    )


def _clone_upstream(repo, name="independent.git"):
    upstream = repo.parent / name
    _git(repo, "clone", "--bare", str(repo), str(upstream))
    return upstream


def _push(sha, old=_ZERO_SHA):
    with _stdin(sha, old):
        return main()


def _blocked(capsys, sha: str, message: str, old: str = _ZERO_SHA) -> str:
    """Push *sha*, expect a refusal whose stderr names *message*, and return that stderr."""
    assert _push(sha, old) == 1
    err = capsys.readouterr().err
    assert message in err
    return err


def test_minimum_git_generated_only_first_import(matrix_repo):
    repo = matrix_repo
    base = _commit(repo, "fix(core): generated instructions and docker verification agent")
    assert _push(base) == 0
    destination = Path(sys.argv[2])
    _git(repo, "push", "--no-verify", str(destination), "HEAD:refs/heads/main")
    incremental = _commit(repo, "fix(core): generated with care")
    assert _push(incremental, base) == 0
    assert _push(_commit(repo, "fix(core): claude metadata"), base) == 1


def test_minimum_git_destination_protected_ancestry_and_fresh_metadata(matrix_repo, capsys):
    repo = matrix_repo
    base = _commit(
        repo, "fix(core): generated by claude", author="Foreign <foreign@elsewhere.test>"
    )
    upstream = _clone_upstream(repo)
    sys.argv[2] = str(upstream)
    config = repo / ".booley" / "booley.toml"
    config.parent.mkdir(exist_ok=True)
    config.write_text('[stealth]\nenabled = true\nallowed_authors = ["*@example.com"]\n')
    assert _push(base) == 0
    assert "destination advertisement unavailable" not in capsys.readouterr().err
    assert _push(_commit(repo, "fix(core): clean", author="Foreign <foreign@elsewhere.test>")) == 1
    _git(repo, "reset", "--hard", base)
    assert _push(_commit(repo, "fix(core): co-authored-by: Cursor Agent")) == 1
    _git(repo, "reset", "--hard", base)
    sha = _commit_file(
        repo, "claude.txt", "fresh ordinary contents\n", "fix(core): ordinary new file"
    )
    assert _push(sha) == 1
    _git(repo, "reset", "--hard", base)
    assert _push(_commit_symlink(repo, "state-link", ".booley/private")) == 1


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
        add_git_command_config(monkeypatch, "remote.origin.promisor", "false")
    else:
        key = {
            "promisor": "remote.origin.promisor",
            "filter": "remote.origin.partialclonefilter",
            "partial": "extensions.partialclone",
        }[state]
        _git(repo, "config", key, "false" if state != "partial" else "origin")
    executable = shutil.which("git")
    probe = run_bounded_child([executable, "--no-lazy-fetch", "--version"])
    if probe.returncode == 0:
        # The current capability safely inspects local complete objects; file
        # authority preflight remains strict on this path (tested separately).
        assert _push(_head(repo)) == 0
    else:
        fake = GitCommandFake.install(monkeypatch)
        assert _push(_head(repo)) == 1
        assert not any(
            {"cat-file", "rev-list", "diff-tree"} & set(command) for command in fake.commands
        )


@pytest.mark.parametrize("storage", ["alternates", "symlink", "reference", "inherited"])
def test_minimum_git_complete_alternate_and_symlink_storage(matrix_repo, monkeypatch, storage):
    repo = matrix_repo
    upstream = _clone_upstream(repo)
    clone = repo.parent / "clone"
    if storage == "symlink":
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
    else:
        borrow = ["--reference", str(upstream)] if storage == "reference" else ["--shared"]
        _git(repo, "clone", *borrow, str(upstream), str(clone))
    _git(clone, "config", "user.name", "Real Dev")
    _git(clone, "config", "user.email", "dev@example.com")
    _enable_stealth(clone / ".booley")
    clean = _head(clone)
    leak = _commit(clone, "fix(core): claude fresh identity")
    if storage == "inherited":
        (clone / ".git" / "objects" / "info" / "alternates").unlink()
        monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", str(upstream / "objects"))
    monkeypatch.chdir(clone)
    assert _push(clean) == 0
    assert _push(leak) == 1


@pytest.mark.parametrize("missing", ["commit", "tree", "symlink"])
def test_minimum_git_missing_inspected_objects_refuse(matrix_repo, missing):
    repo = matrix_repo
    sha = _commit_symlink(repo, "guide", "docs/guide.md")
    oid = (
        sha
        if missing == "commit"
        else _git(repo, "rev-parse", f"{sha}^{{tree}}" if missing == "tree" else f"{sha}:guide")
    )
    _remove_loose_object(repo, oid)
    assert _push(sha) == 1


@pytest.mark.parametrize(
    "case", ["other-branch", "unrelated-ref", "url-rewriting", "unadvertised", "cloned-origin"]
)
def test_destination_authority_comes_only_from_its_own_advertisement(
    matrix_repo, monkeypatch, case
):
    repo = matrix_repo
    base = _commit(repo, "fix(core): claude imported history")
    if case == "other-branch":
        _git(repo, "push", "--no-verify", sys.argv[2], "HEAD:refs/heads/other")
    elif case == "unrelated-ref":
        _git(repo, "update-ref", "refs/remotes/unrelated/main", base)
    elif case == "url-rewriting":
        _git(repo, "config", "url./untrusted/.insteadOf", sys.argv[2])
    elif case == "unadvertised":  # a protected clone that is not the destination grants nothing
        _clone_upstream(repo)
    else:  # a project cloned from the protected upstream does not inherit it as destination
        clone = repo.parent / "cloned-project"
        _git(repo, "clone", str(_clone_upstream(repo)), str(clone))
        _enable_stealth(clone / ".booley")
        monkeypatch.chdir(clone)
    assert _push(base) == (0 if case == "other-branch" else 1)


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        ("timeout", "timed out"),
        ("auth", "repair credentials/network or fetch and retry"),
        ("malformed", "malformed repository advertisement"),
        ("denied", "denied by effective Git policy"),
        ("invalid-ref", "invalid advertised ref"),
        ("authority-changed", "file authority changed"),
    ],
)
def test_destination_failure_complete_superset(repo, monkeypatch, capsys, failure, reason):
    old = _commit(repo, "fix(core): claude historical identity")
    fresh = _commit(repo, "fix(core): ordinary clean work")
    fake = GitCommandFake.install(monkeypatch)
    if failure == "timeout":
        fake.crash("ls-remote", error=subprocess.TimeoutExpired("git", 10))
    elif failure == "invalid-ref":
        fake.answer("ls-remote", returncode=0, stdout=f"{old}\trefs/heads/bad..name\n".encode())
    elif failure == "authority-changed":
        destination = Path(sys.argv[2])
        fake.hook("ls-remote", action=lambda: _git(destination, "config", "test.changed", "1"))
    elif failure == "denied":
        monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "ssh")
    else:
        fake.answer(
            "ls-remote",
            returncode=1 if failure == "auth" else 0,
            stdout=b"garbled" if failure == "malformed" else b"",
            stderr=b"credential stderr",
        )
    assert _push(fresh, old) == 0
    assert _push(_commit(repo, "fix(core): claude fresh identity"), old) == 1
    error = capsys.readouterr().err
    assert "advertisement unavailable" in error and "conservative" in error and reason in error
    assert "credential stderr" not in error
    assert any("ls-remote" in command for command in fake.commands) is (failure != "denied")


@pytest.mark.parametrize("kind", ["rev-list", "cat-file", "diff-tree"])
def test_fallback_inspection_failure_refuses(repo, monkeypatch, capsys, kind):
    fake = GitCommandFake.install(monkeypatch)
    fake.answer("ls-remote", returncode=1, stdout=b"", stderr=b"")
    fake.answer(kind, returncode=1, stdout=b"", stderr=b"")
    _blocked(capsys, _head(repo), "Git inspection failed")


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("garbled", "malformed pre-push protocol"),
        ("refs/heads/main 0000000 refs/heads/main 0000000", "invalid pre-push object ID"),
        ("refs/heads/bad..ref {oid} refs/heads/main {zero}", "invalid pre-push ref name"),
        ("refs/heads/main {oid} HEAD {zero}", "invalid pre-push ref name"),
        ("(delete) {oid} refs/heads/main {zero}", "invalid pre-push deletion"),
    ],
)
def test_malformed_protocol_refuses_without_discovery(repo, monkeypatch, capsys, line, message):
    line = line.format(oid=_head(repo), zero=_ZERO_SHA)
    _forbid_discovery(monkeypatch)
    monkeypatch.setattr(sys, "stdin", io.StringIO(line))
    assert main() == 1
    assert message in capsys.readouterr().err


def test_no_update_and_deletion_avoid_network(repo, monkeypatch):
    _upstream_pair(repo, repo.parent / "offline.git", "f" * 40)
    fake = GitCommandFake.install(monkeypatch)
    with patch("sys.stdin", io.StringIO("")):
        assert main() == 0
    assert _push(_ZERO_SHA, _head(repo)) == 0
    assert not any("ls-remote" in command for command in fake.commands)
    assert _push(_head(repo)) == 1


def test_destination_covered_base_needs_no_import_settings(repo):
    base = _head(repo)
    _git(repo, "push", "--no-verify", sys.argv[2], "HEAD:refs/heads/main")
    assert _push(_commit(repo, "fix(core): new work"), base) == 0


def test_missing_old_destination_tip_conservative(repo):
    assert _push(_head(repo), "f" * 40) == 0
    assert _push(_commit(repo, "fix(core): claude fresh work"), "f" * 40) == 1


@pytest.mark.parametrize("policy", ["never", "user"])
def test_effective_protocol_policy_cannot_be_widened(repo, monkeypatch, policy):
    _git(repo, "config", "protocol.file.allow", policy)
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file:ssh:https")
    monkeypatch.setenv("GIT_PROTOCOL_FROM_USER", "0")
    _forbid_discovery(monkeypatch)
    assert _push(_head(repo)) == 0
    assert _push(_commit(repo, "fix(core): claude fresh work")) == 1


# Windows CI: 3x the slowest observed duration (tests/timeout_headroom.py).
@pytest.mark.timeout(120)
def test_deep_history_oldest_leak_and_bounded_batches(repo, monkeypatch):
    base = _commit(repo, "fix(core): claude oldest new work")
    tree = _git(repo, "rev-parse", "HEAD^{tree}")
    tip = base
    for index in range(505):
        tip = _git(repo, "commit-tree", tree, "-p", tip, "-m", f"fix(core): step {index}")
    fake = GitCommandFake.install(monkeypatch)
    assert _push(tip) == 1
    assert len([c for c in fake.commands if "cat-file" in c or "diff-tree" in c]) < 10


@pytest.mark.parametrize(
    "trail",
    [
        "Co-Authored-By: Cursor Agent <cursor@example.test>",
        "Generated with Copilot",
        "Generated with GPT-5",
        "llm metadata",
    ],
)
def test_bypassed_trailer_remains_blocked(repo, trail):
    # A clean first body line and a clean middle paragraph: the whole body is scanned.
    assert (
        _push(_commit(repo, f"fix(core): ordinary work\n\nfirst line\n\nsecond\n\n{trail}")) == 1
    )


def _tilde_destination(repo: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path]:
    """A ``~`` destination: Git expands the selected HOME, a literal ``~`` directory also exists."""
    base = _head(repo)
    (repo / "~").mkdir()
    _git(repo, "clone", "--bare", str(repo), str(repo / "~" / "destination.git"))
    home = repo.parent / "selected-home"
    home.mkdir()
    served = home / "destination.git"
    _git(repo, "clone", "--bare", str(repo), str(served))
    _git(served, "config", "remote.origin.promisor", "true")
    monkeypatch.setenv("HOME", str(home))
    assert base in _git(repo, "ls-remote", "--refs", "~/destination.git", HOME=str(home))
    return "~/destination.git", served / "objects"


def _unprovable_file_destination(
    case: str, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[str, str, Path, str]:
    """Build one file destination Git cannot prove exact and complete.

    Returns the pushed commit, the destination, the storage to watch and the expected reason.
    """
    base = _head(repo)
    if case == "promisor":
        upstream = _clone_upstream(repo)
        _git(upstream, "config", "remote.origin.promisor", "false")
        destination, objects = str(upstream), upstream / "objects"
        reason = "complete nonpromisor clone"
    elif case.startswith("invalid-utf8"):
        if os.name == "nt":
            pytest.skip("invalid UTF-8 filename fixture is POSIX-specific")
        _clone_upstream(repo, "encoded-\ufffd.git")
        base = _commit(repo, "fix(core): ordinary local metadata")
        alias = repo.parent / os.fsdecode(b"encoded-\xff.git")
        alias.symlink_to(repo, target_is_directory=True)
        destination, objects = repo.parent.as_uri() + "/encoded-%FF.git", repo / ".git" / "objects"
        reason = "file authority requires valid UTF-8 URL encoding"
    elif case.startswith("suffix"):
        parent = repo.parent / "authority-parent"
        _git(repo, "clone", str(repo), str(parent))
        (parent / "up").mkdir()
        base = _commit(repo, "fix(core): ordinary local metadata")
        served = repo
        if case == "suffix-shared":
            served = repo.parent / "shared-authority.git"
            _git(repo, "clone", "--bare", "--shared", str(repo), str(served))
        (parent / "up.git").symlink_to(served, target_is_directory=True)
        # Git for Windows reports forward slashes; compare paths, not spellings.
        assert Path(_git(parent / "up", "rev-parse", "--show-toplevel")) == parent
        assert base in _git(repo, "ls-remote", "--refs", str(parent / "up"))
        destination = str(parent / "up")
        objects = served / ("objects" if case == "suffix-shared" else ".git/objects")
        reason = "file authority must name its exact repository root"
    else:
        destination, objects = _tilde_destination(repo, monkeypatch)
        reason = "home-expanded authority requires an absolute repository path"
    return base, destination, objects, reason


@pytest.mark.parametrize(
    "case",
    ["promisor", "invalid-utf8", "invalid-utf8-locked", "suffix-self", "suffix-shared", "tilde"],
)
def test_minimum_git_unprovable_file_destination_never_reaches_upload_pack(
    matrix_repo, monkeypatch, capsys, case
):
    """A file destination Git cannot prove exact and complete is scanned conservatively."""
    repo = matrix_repo
    base, destination, objects, reason = _unprovable_file_destination(case, repo, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["pre-push", "destination", destination])
    fake = GitCommandFake.install(monkeypatch)
    if case == "invalid-utf8-locked":  # Git maintenance holds this lock while hooks run
        fake.hook(action=(objects / "maintenance.lock").touch, call=None)
    trace = repo.parent / "destination-requests.trace"
    monkeypatch.setenv("GIT_TRACE_PACKET", str(trace))
    before = _destination_inspection_state(objects, trace)
    assert _push(base) == 0
    assert reason in capsys.readouterr().err
    assert before == _destination_inspection_state(objects, trace)
    fresh = _commit(repo, "fix(core): claude fresh local metadata")
    before = _destination_inspection_state(objects, trace)
    assert _push(fresh) == 1
    assert not any("ls-remote" in command for command in fake.commands)
    assert before == _destination_inspection_state(objects, trace)


_HTTPS = "https://user:credential@example.test/repository.git"
_SSH = "ssh://example.test/repo.git"
_CERT, _HOST = "certificate verification", "host verification"
_NO_AUTHORITY = "cannot establish authority"
_UNVERIFIED = "GIT_SSH_COMMAND=ssh -o StrictHostKeyChecking=no"


def _apply_settings(repo: Path, monkeypatch: pytest.MonkeyPatch, settings: str) -> None:
    """Apply ``|``-separated ``NAME=value`` items: ``GIT_*`` ones to the environment, the rest
    to the repository's Git configuration."""
    for item in filter(None, settings.split("|")):
        key, _, value = item.partition("=")
        if key.startswith("GIT_"):
            monkeypatch.setenv(key, value)
        else:
            _git(repo, "config", key, value)


@pytest.mark.parametrize(
    ("destination", "settings", "reason"),
    [
        ("http://example.test/repo", "", _NO_AUTHORITY),
        ("git://example.test/repo", "", _NO_AUTHORITY),
        ("ext::helper", "", _NO_AUTHORITY),
        ("helper::repo", "", _NO_AUTHORITY),
        ("", "GIT_ALLOW_PROTOCOL=", "denied by effective Git policy"),
        (_HTTPS, "GIT_SSL_NO_VERIFY=true", _CERT),
        (_HTTPS, "GIT_SSL_NO_VERIFY=", _CERT),
        (_HTTPS, "GIT_SSL_NO_VERIFY=0", _CERT),
        (_HTTPS, "http.https://example.test/.sslVerify=false", _CERT),
        (_HTTPS, "http.https://example.test/.followRedirects=true", _CERT),
        (_SSH, _UNVERIFIED, _HOST),
        (_SSH, "GIT_SSH_COMMAND=ssh -o 'StrictHostKeyChecking no'", _HOST),
        (_SSH, "GIT_SSH_COMMAND=ssh -o UserKnownHostsFile=/dev/null", _HOST),
        (_SSH, "GIT_SSH_COMMAND=ssh -o UserKnownHostsFile=NUL", _HOST),
        (_SSH, "GIT_SSH_COMMAND=ssh -o StrictHostKeyChecking=accept-new", _HOST),
        (_SSH, "GIT_SSH=/usr/bin/ssh|core.sshCommand=ssh -o StrictHostKeyChecking=no", _HOST),
        (_SSH, 'GIT_SSH_COMMAND=ssh -o "unterminated', "invalid owner SSH command"),
        ("/repo with space", "", "invalid repository location"),
        ("file://remote-host/repo", "", "unsupported file authority"),
        ("file:///repo%00name", "", "invalid file authority path"),
        ("https:///no-host", "", "invalid authenticated repository URL"),
        ("file:/absolute/repo", "", "literal file:// URL"),
        ("~owner:repository", _UNVERIFIED, _HOST),  # an SCP-style SSH host, not a home path
    ],
)
def test_unprovable_destination_authority_never_discovers(
    repo, monkeypatch, capsys, destination, settings, reason
):
    """The destination is scanned conservatively, naming why, without asking it for refs."""
    monkeypatch.setattr(sys, "argv", ["pre-push", "destination", destination or sys.argv[2]])
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    _apply_settings(repo, monkeypatch, settings)
    _forbid_discovery(monkeypatch)
    assert _push(_head(repo)) == 0
    assert reason in capsys.readouterr().err


def test_undeterminable_https_policy_denies_authority(repo, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["pre-push", "destination", _HTTPS])
    fake = _forbid_discovery(monkeypatch)
    fake.answer("config", "--get-urlmatch", returncode=128, stderr=b"fatal: bad config")
    assert _push(_head(repo)) == 0
    assert "cannot determine HTTPS authority policy" in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["global", "include", "environment", "invalid"])
def test_merged_protocol_restrictions_are_respected(repo, monkeypatch, capsys, kind):
    if kind in ("global", "invalid"):
        _git(repo, "config", "protocol.allow", "never" if kind == "global" else "sometimes")
    elif kind == "include":
        included = repo.parent / "deny.config"
        included.write_text("[protocol]\nallow = never\n", encoding="utf-8")
        _git(repo, "config", "include.path", str(included))
    else:
        add_git_command_config(monkeypatch, "protocol.file.allow", "never")
    _forbid_discovery(monkeypatch)
    assert _push(_head(repo)) == 0
    reason = "invalid Git transport policy" if kind == "invalid" else "denied by effective Git"
    assert reason in capsys.readouterr().err


_STRICT = "ssh -o BatchMode=yes -o StrictHostKeyChecking=yes"


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        ("", _STRICT),
        ("GIT_SSH_VARIANT=auto", _STRICT),
        ("ssh.variant=auto", _STRICT),
        ("GIT_SSH_COMMAND=owner-wrapper --trusted-proxy", "owner-wrapper --trusted-proxy"),
        (
            "GIT_SSH_COMMAND=ssh -o StrictHostKeyChecking=yes|core.sshCommand=ssh -o StrictHostKeyChecking=no",
            "ssh -o StrictHostKeyChecking=yes",
        ),
    ],
)
def test_ssh_owner_wrapper_preserved_and_default_strict(repo, monkeypatch, settings, expected):
    monkeypatch.setattr(sys, "argv", ["pre-push", "destination", _SSH])
    for name in ("GIT_SSH", "GIT_SSH_COMMAND", "GIT_SSH_VARIANT"):
        monkeypatch.delenv(name, raising=False)
    _apply_settings(repo, monkeypatch, settings)
    fake = GitCommandFake.install(monkeypatch)
    fake.answer("ls-remote", returncode=0, stdout=b"")
    assert _push(_head(repo)) == 0
    [(_, lookup)] = fake.calls("ls-remote")
    assert lookup["env"]["GIT_SSH_COMMAND"] == expected
    assert lookup["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_complete_stdin_consumed_and_lookup_has_devnull(repo, monkeypatch):
    stdin = io.StringIO(f"refs/heads/main {_head(repo)} refs/heads/main {_ZERO_SHA}\n")
    monkeypatch.setattr(sys, "stdin", stdin)
    fake = GitCommandFake.install(monkeypatch)
    consumed = []
    fake.hook("ls-remote", action=lambda: consumed.append(stdin.tell()), call=None)
    assert main() == 0
    [(_, lookup)] = fake.calls("ls-remote")
    assert consumed == [len(stdin.getvalue())]
    assert lookup["stdin"] == subprocess.DEVNULL
    assert lookup["timeout"] <= 10


def test_annotated_tags_older_import_and_noncommit_updates(repo):
    older = _head(repo)
    _commit(repo, "fix(core): claude imported identity")
    upstream = _clone_upstream(repo)
    sys.argv[2] = str(upstream)
    _git(repo, "tag", "-a", "older", older, "-m", "upstream release")
    assert _push(_git(repo, "rev-parse", "older")) == 0
    assert _push(_git(repo, "rev-parse", "HEAD^{tree}")) == 1


def test_multiref_new_tips_do_not_exempt_each_other(repo, monkeypatch):
    leak = _commit(repo, "fix(core): claude local work")
    clean = _commit(repo, "fix(core): ordinary local work")
    protocol = f"refs/heads/a {leak} refs/heads/a {_ZERO_SHA}\nrefs/heads/b {clean} refs/heads/b {_ZERO_SHA}\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO(protocol))
    assert main() == 1


def test_merge_exposes_new_side_branch_commit(repo):
    base = _head(repo)
    _git(repo, "push", "--no-verify", sys.argv[2], "HEAD:refs/heads/main")
    _git(repo, "checkout", "-b", "side")
    _commit(repo, "fix(core): claude side identity")
    _git(repo, "checkout", "main")
    _git(repo, "merge", "--no-ff", "side", "-m", "fix(core): merge side")
    assert _push(_head(repo), base) == 1


def test_replacement_does_not_hide_identity_and_active_grafts_refuse(repo):
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
    _enable_stealth(clone / ".booley")
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


_BATCH = ("cat-file", "--batch")


def _corrupted(
    corrupt: Callable[[bytes], bytes] | bytes | tuple[str, bytes], real: bytes, oid: str
) -> bytes:
    """Apply one table row: a function of the real output, ``(kind, body)`` for the first
    ``cat-file --batch`` record only (the rest stay real), or bytes with ``{oid}`` (the first
    requested object), ``{bad}`` and ``{zero}`` placeholders."""

    def fill(raw: bytes) -> bytes:
        raw = raw.replace(b"{oid}", oid.encode()).replace(b"{bad}", b"b" * 40)
        return raw.replace(b"{zero}", _ZERO_SHA.encode())

    if callable(corrupt):
        return corrupt(real)
    if not isinstance(corrupt, tuple):
        return fill(corrupt)
    kind, body = corrupt
    header, _, rest = real.partition(b"\n")
    record = (
        f"{oid} missing\n".encode()
        if kind == "missing"
        else fill(f"{oid} {kind} {len(fill(body))}\n".encode() + body + b"\n")
    )
    return record + rest[int(header.split()[2]) + 1 :]


@pytest.mark.parametrize(
    ("words", "call", "corrupt", "message"),
    [
        (("rev-list",), None, b"not-an-oid\n", "malformed outgoing commit inventory"),
        (("rev-list",), None, lambda real: real * 2, "malformed outgoing commit inventory"),
        (_BATCH, 1, b"", "truncated Git object batch"),
        (_BATCH, 1, b"garbled header\n", "malformed Git object batch"),
        (_BATCH, 1, lambda real: real + b"trailing", "unexpected Git object batch trailer"),
        (_BATCH, 2, lambda real: real[:-1], "truncated Git object contents"),
        (_BATCH, 3, lambda real: real[:-1], "truncated Git object contents"),
        (_BATCH, 3, ("missing", b""), "cannot safely inspect committed symlink target"),
        (_BATCH, None, ("tag", b"object {oid}\ntype tag\n"), "cyclic annotated tag"),
        (_BATCH, None, ("tag", b"type commit\n"), "malformed annotated tag"),
        (_BATCH, None, ("tag", b"object not-an-oid\n"), "invalid annotated tag object"),
        (_BATCH, 2, ("missing", b""), "missing inspected commit object"),
        (_BATCH, 2, ("tree", b""), "missing inspected commit object"),
        (_BATCH, 2, ("commit", b"tree x\nauthor A <a@x> 1 +0000"), "malformed commit object"),
        (_BATCH, 2, ("commit", b"tree x\n\nmessage"), "missing commit identities"),
        (
            _BATCH,
            2,
            ("commit", b"author no-email\ncommitter C <c@x> 1 +0000\n\nmessage"),
            "malformed commit identity",
        ),
        (("diff-tree",), None, b"unterminated", "truncated changed-tree batch"),
        (("diff-tree",), None, b"", "missing changed-tree commit frames"),
        (("diff-tree",), None, b"{bad}\0", "unexpected changed-tree commit frame"),
        (
            ("diff-tree",),
            None,
            b":000000 100644 {zero} {bad} A\0path\0",
            "missing changed-tree frame",
        ),
        (
            ("diff-tree",),
            None,
            b"{oid}\0:000000 100644 short ids A\0path\0",
            "malformed changed-tree entry",
        ),
        (
            ("diff-tree",),
            None,
            b"{oid}\0:000000 100644 {zero} {bad} A\0../escape\0",
            "invalid committed path",
        ),
        (("cat-file", "--batch-check"), None, b"", "truncated symlink size batch"),
        (("cat-file", "--batch-check"), None, b"{oid} blob 65537\n", "oversized symlink target"),
        (("diff-tree",), 1, None, "repository configuration changed during inspection"),
    ],
)
def test_malformed_batch_or_changed_inspection_state_refuses(
    repo, monkeypatch, capsys, words, call, corrupt, message
):
    """Each Git answer the hook cannot trust refuses the push with its own message."""
    sha = _commit_symlink(repo, "guide", "docs/guide.md")
    fake = GitCommandFake.install(monkeypatch)
    if corrupt is None:  # the repository configuration changes while Git is inspected
        fake.hook(*words, action=lambda: _git(repo, "config", "protocol.file.allow", "never"))
        _blocked(capsys, sha, message)
        return

    def edit(result, options):
        oid = options["input"].split()[0].decode()
        return with_stdout(result, _corrupted(corrupt, result.stdout, oid))

    fake.rewrite(*words, call=call, edit=edit)
    _blocked(capsys, sha, message)


def test_real_blobless_clone_missing_committed_symlink_never_fetches(repo, monkeypatch):
    sha = _commit_symlink(repo, "guide", "docs/missing.md")
    blob = _git(repo, "rev-parse", f"{sha}:guide")
    upstream = _clone_upstream(repo)
    _git(upstream, "config", "uploadpack.allowFilter", "true")
    clone = repo.parent / "blobless"
    _git(repo, "clone", "--filter=blob:none", "--no-checkout", upstream.as_uri(), str(clone))
    _enable_stealth(clone / ".booley")
    monkeypatch.chdir(clone)
    absent_before = run_bounded_child(
        ["git", "--no-lazy-fetch", "cat-file", "-e", blob], cwd=clone
    )
    assert absent_before.returncode != 0
    trace = clone.parent / "inspection-trace.json"
    objects = clone / ".git" / "objects"
    before = _destination_inspection_state(objects, trace)[0]
    monkeypatch.setenv("GIT_TRACE2_EVENT", str(trace))
    assert _push(sha) == 1
    assert before == _destination_inspection_state(objects, trace)[0]
    assert '"fetch"' not in trace.read_text()
    absent = run_bounded_child(["git", "--no-lazy-fetch", "cat-file", "-e", blob], cwd=clone)
    assert absent.returncode != 0


def test_destination_imported_path_identity_and_allowlist_exempt_but_new_state_refuses(repo):
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
    config = repo / ".booley" / "booley.toml"
    config.parent.mkdir(exist_ok=True)
    config.write_text('[stealth]\nenabled = true\nallowed_authors = ["dev@example.com"]\n')
    assert _push(base) == 0
    sha = _commit_symlink(repo, "state-link", ".booley/private")
    assert _push(sha) == 1


def test_destination_identity_probe_clears_current_repository_selection(repo, monkeypatch):
    base = _commit(repo, "fix(core): claude imported identity")
    upstream = _clone_upstream(repo)
    monkeypatch.setattr(sys, "argv", ["pre-push", "destination", str(upstream)])
    monkeypatch.setenv("GIT_DIR", str(repo / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(repo))
    assert _push(base) == 0


def test_plain_identity_footer_and_cherry_pick_remain_protected(repo):
    base = _head(repo)
    _git(repo, "checkout", "-b", "foreign")
    leak = _commit(repo, "fix(core): clean work\n\nCo-Authored-By: Cursor Agent <cursor@local>")
    _git(repo, "checkout", "main")
    _git(repo, "cherry-pick", leak)
    assert _push(_head(repo), base) == 1


def test_symbolic_head_input_supported_without_using_it_as_object_argument(repo, monkeypatch):
    protocol = f"HEAD {_head(repo)} refs/heads/main {_ZERO_SHA}\n"
    monkeypatch.setattr(sys, "stdin", io.StringIO(protocol))
    assert main() == 0


def test_source_checkout_policy_never_reads_push_configuration(monkeypatch):
    from booley.commit_policy import policy

    root = Path(__file__).resolve().parents[2]
    monkeypatch.chdir(root)

    def forbidden(*args, **kwargs):
        pytest.fail("source pre-push must never load the Project push configuration")

    monkeypatch.setattr(pre_push_hook, "validate_push_configuration", forbidden)
    assert policy.source_checkout_policy_owner(root)
    assert main() == 0


def test_disabled_project_does_not_load_upstream_pair(repo, monkeypatch):
    config = repo / ".booley" / "booley.toml"
    config.parent.mkdir(exist_ok=True)
    config.write_text('[stealth]\nenabled = false\nupstream_repository = "invalid"\n')

    def forbidden(*args, **kwargs):
        pytest.fail("disabled guard must not inspect push configuration")

    monkeypatch.setattr(pre_push_hook, "validate_push_configuration", forbidden)
    assert _push(_head(repo)) == 0


def test_available_advertised_object_read_error_is_not_optional_discovery_failure(
    repo, monkeypatch, capsys
):
    foreign = _git(
        repo,
        "commit-tree",
        _git(repo, "rev-parse", "HEAD^{tree}"),
        "-m",
        "fix(core): separate destination history",
    )
    _git(repo, "push", "--no-verify", sys.argv[2], f"{foreign}:refs/heads/other")
    fake = GitCommandFake.install(monkeypatch)
    fake.rewrite(
        "cat-file",
        edit=lambda result, options: (
            subprocess.CompletedProcess(result.args, 1, b"", b"")
            if options["input"] == (foreign + "\n").encode()
            else result
        ),
    )
    err = _blocked(capsys, _head(repo), "Git inspection failed")
    assert "advertisement unavailable" not in err


@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("objects/pack:missing", "pack"),
        ("objects/pack:self", "pack"),
        ("info/grafts:missing", "grafts"),
        ("info/grafts:self", "grafts"),
        ("objects-not-a-directory", "cannot resolve readable Git object storage"),
        ("future-format", "unsupported repository format"),
        ("unknown-object-format", "unsupported Git object format"),
        ("broken-capability-probe", "cannot determine safe Git object inspection capability"),
    ],
)
def test_unusable_repository_state_refuses(repo, monkeypatch, capsys, fault, message):
    fake = GitCommandFake.install(monkeypatch)
    if fault == "objects-not-a-directory":
        (repo / "objects-file").write_bytes(b"not a directory\n")
        fake.answer("rev-parse", "--git-path", "objects", returncode=0, stdout=b"objects-file\n")
    elif fault == "future-format":
        fake.rewrite(
            "config",
            "--null",
            "--list",
            edit=lambda result, _: with_stdout(
                result, result.stdout + b"core.repositoryformatversion\n2\0"
            ),
        )
    elif fault == "unknown-object-format":
        fake.answer("rev-parse", "--show-object-format", returncode=0, stdout=b"sha1024\n")
    elif fault == "broken-capability-probe":
        fake.answer("--no-lazy-fetch", "--version", returncode=128, stderr=b"fatal: broken")
    else:  # a real broken structural node: Git ignores it, the hook must not
        node, target = fault.split(":")
        path = repo / ".git" / node
        if path.is_dir():
            path.rmdir()
        try:
            path.symlink_to(path.name if target == "self" else "missing")
        except OSError:
            pytest.skip("platform does not support symlink creation")
    _blocked(capsys, _head(repo), message)


@pytest.mark.parametrize("label", ["HEAD~1", "@", "short", "HEAD@{0 seconds ago}"])
def test_minimum_git_typed_push_source_label_uses_only_protocol_oid(matrix_repo, label):
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


def test_many_advertised_refs_use_bounded_inventory_arguments(repo, monkeypatch):
    oid = _head(repo)
    payload = "".join(f"{oid}\trefs/heads/branch-{index}\n" for index in range(900)).encode()
    fake = GitCommandFake.install(monkeypatch)
    fake.answer("ls-remote", returncode=0, stdout=payload)
    assert _push(oid) == 0
    assert not any("check-ref-format" in command for command in fake.commands)
    inventories = fake.calls("rev-list")
    assert inventories and all("--stdin" in c and o["input"] for c, o in inventories)
    assert all(len(command) < 10 for command, _ in inventories)


@pytest.mark.parametrize(
    "entry", [b" value = \xff\n", b" valueless\n"], ids=["non-utf8", "valueless"]
)
def test_unrelated_git_config_entries_preserve_push(repo, entry):
    with (repo / ".git" / "config").open("ab") as stream:
        stream.write(b"\n[legacy]\n" + entry)
    assert _push(_head(repo)) == 0


def _clean_merge(repo):
    _git(repo, "checkout", "-b", "clean-side")
    _commit(repo, "fix(core): ordinary side work")
    _git(repo, "checkout", "main")
    _git(repo, "merge", "--no-ff", "clean-side", "-m", "fix(core): clean merge")
    return _head(repo)


def test_minimum_git_clean_unadvertised_merge(matrix_repo):
    repo = matrix_repo
    merge = _clean_merge(repo)
    assert len(_git(repo, "rev-list", "--parents", "-n", "1", merge).split()) == 3
    assert _push(merge) == 0
    upstream = _clone_upstream(repo)
    sys.argv[2] = str(upstream)
    local = _commit(repo, "fix(core): ordinary post-import work")
    assert _push(local) == 0


def test_minimum_git_tag_heavy_advertisement_batches_peeling(matrix_repo, monkeypatch):
    repo = matrix_repo
    destination = Path(sys.argv[2])
    oid = _head(repo)
    tags = []
    for index in range(12):
        name = f"release-{index}"
        _git(repo, "tag", "-a", name, "-m", "ordinary release")
        tags.append(_git(repo, "rev-parse", name))
    _git(repo, "push", "--no-verify", str(destination), "--tags")
    fake = GitCommandFake.install(monkeypatch)
    assert _push(oid) == 0
    batches = [c for c in fake.commands if "cat-file" in c and "--batch" in c]
    assert len(batches) < len(tags)


def test_minimum_git_missing_advertised_tag_target_grants_no_exclusion(matrix_repo):
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


@pytest.mark.parametrize("failure", ["Symlink loop from selected path", "unrelated failure"])
def test_legacy_resolve_symlink_loop_uses_destination_fallback(repo, monkeypatch, capsys, failure):
    """Only Python's legacy symlink-loop ``RuntimeError`` is a destination failure."""
    destination = repo.parent / "looping-destination"
    monkeypatch.setattr(sys, "argv", ["pre-push", "destination", str(destination)])
    original = Path.resolve

    def legacy_loop(path, *args, **kwargs):
        if path == destination:
            raise RuntimeError(failure)
        return original(path, *args, **kwargs)

    sha = _head(repo)
    monkeypatch.setattr(Path, "resolve", legacy_loop)
    if failure.startswith("Symlink loop"):
        assert _push(sha) == 0
        assert "destination advertisement unavailable" in capsys.readouterr().err
    else:
        with pytest.raises(RuntimeError, match=failure):
            _push(sha)


def _destination_inspection_state(
    objects: Path, request_trace: Path
) -> tuple[dict[str, bytes], bytes]:
    files = {
        str(path.relative_to(objects)): path.read_bytes()
        for path in objects.rglob("*")
        # Git maintenance uses this transient lock; it is not an object.
        if path.is_file() and path != objects / "maintenance.lock"
    }
    return files, request_trace.read_bytes() if request_trace.exists() else b""


@pytest.mark.parametrize("form", ["bare", "worktree", "gitdir", "symlink", "uri", "merge"])
def test_minimum_git_exact_file_destination_accepts_storage(matrix_repo, form, capsys):
    repo = matrix_repo
    base = _commit(repo, "fix(core): claude imported history")
    if form == "merge":  # protected ancestry reached through a merge
        base = _clean_merge(repo)
    authority = repo.parent / ("encoded space.git" if form == "uri" else "exact-authority")
    args = ["--bare"] if form in ("bare", "uri", "merge") else []
    _git(repo, "clone", *args, str(repo), str(authority))
    if form == "gitdir":
        authority = authority / ".git"
    elif form == "symlink":
        alias = repo.parent / "independent-alias"
        alias.symlink_to(authority, target_is_directory=True)
        authority = alias
    sys.argv[2] = authority.as_uri() if form == "uri" else str(authority)
    assert _push(base) == 0
    assert "destination advertisement unavailable" not in capsys.readouterr().err
    assert _push(_commit(repo, "fix(core): claude new local metadata")) == 1


@pytest.mark.parametrize("case", ["missing-tag-target", "bare-authority-typo", "tree", "blob"])
def test_minimum_git_unsendable_update_names_its_repair(matrix_repo, capsys, case):
    repo = matrix_repo
    pushed, code, repair = _head(repo), 1, "select a commit update and retry"
    if case == "missing-tag-target":
        target = _git(repo, "commit-tree", "HEAD^{tree}", "-m", "ordinary release")
        _git(repo, "tag", "-a", "missing-local-target", target, "-m", "ordinary release")
        pushed = _git(repo, "rev-parse", "refs/tags/missing-local-target")
        repair = "fetch missing pushed history"
        _remove_loose_object(repo, target)
    elif case == "bare-authority-typo":
        sys.argv[2] = str(_clone_upstream(repo) / "objects")
        code, repair = 0, "file authority must name its exact repository root"
    else:
        pushed = _git(repo, "rev-parse", "HEAD^{tree}" if case == "tree" else "HEAD:file.txt")
    assert _push(pushed) == code
    assert repair in capsys.readouterr().err


@pytest.mark.parametrize(
    ("kind", "value", "offense"),
    [
        *(
            ("path", name, "tracked path has banned terms")
            for name in (
                "booley-state.txt",
                "docs_qa/booley_notes.txt",
                "notes/booley-notes.txt",
                "notes/mybooleyfile.txt",
                "booley_config",
                "BooleyRunner.py",
            )
        ),
        ("symlink", "notes/mybooleyfile.txt", "symlink target has banned terms"),
        ("author", "mybooleyfile <safe@example.com>", "banned terms: "),
        ("committer", "BooleyRunner", "banned terms: "),
        ("message", "fix: tune booley_config", "banned terms: "),
        ("path", "precursor.txt", None),  # generic words that only resemble the identifier
        ("path", "reagent.sv", None),
    ],
)
def test_banned_identifier_blocks_only_real_matches(repo, capsys, kind, value, offense):
    if kind == "path":
        sha = _commit_file(repo, value)
    elif kind == "symlink":
        sha = _commit_symlink(repo, "guide", value)
    elif kind == "message":
        sha = _commit(repo, value)
    else:
        if kind == "committer":
            _git(repo, "config", "user.name", value)
        author = value if kind == "author" else "Safe User <safe@example.com>"
        sha = _commit(repo, "fix: tune layout", author=author)
    if offense is None:
        assert _push(sha) == 0
    else:
        _blocked(capsys, sha, offense)


@pytest.mark.parametrize("uri", [False, True], ids=["path", "file-uri"])
def test_minimum_git_pinned_upstream_import_and_fresh_metadata(matrix_repo, capsys, uri):
    repo = matrix_repo
    base = _commit(repo, "fix(core): claude imported identity")
    upstream = _clone_upstream(repo, "upstream with spaces.git" if uri else "independent.git")
    _upstream_pair(repo, upstream.as_uri() if uri else upstream, base)
    assert _push(base) == 0
    assert "unavailable" not in capsys.readouterr().err
    assert _push(_commit(repo, "fix(core): clean local work")) == 0
    fresh = _commit(repo, "fix(core): claude fresh local metadata")
    # Source advancement never expands the pinned exemption to new commits.
    _git(repo, "push", "--no-verify", str(upstream), "HEAD:refs/heads/main")
    assert _push(fresh) == 1


def test_minimum_git_upstream_base_can_be_advertised_ancestor(matrix_repo):
    repo = matrix_repo
    base = _commit(repo, "fix(core): claude imported identity", author="Foreign <old@source.test>")
    tip = _commit(repo, "fix(core): subsequent upstream work")
    upstream = _clone_upstream(repo)
    _upstream_pair(repo, upstream, base)
    config = repo / ".booley" / "booley.toml"
    config.write_text(config.read_text() + 'allowed_authors = ["*@example.com"]\n')
    assert _push(tip) == 0
    assert _push(_commit(repo, "fix(core): local work", author="Foreign <old@source.test>")) == 1
    _git(repo, "reset", "--hard", tip)
    assert _push(_commit_symlink(repo, "guide", ".booley/private")) == 1


def test_minimum_git_upstream_advance_requires_fetch_before_verification(matrix_repo, capsys):
    repo = matrix_repo
    base = _commit(repo, "fix(core): claude pristine import")
    source = repo.parent / "advancing-source"
    _git(repo, "clone", str(repo), str(source))
    _git(source, "config", "user.name", "Upstream Dev")
    _git(source, "config", "user.email", "upstream@example.com")
    _git(source, "config", "maintenance.auto", "false")
    advanced = _commit(source, "fix(core): ordinary upstream advancement")
    with pytest.raises(subprocess.CalledProcessError):
        _git(repo, "cat-file", "-e", advanced)
    _upstream_pair(repo, source, base)
    assert _push(base) == 1
    assert "fetch upstream history" in capsys.readouterr().err
    _git(repo, "fetch", str(source), "main")
    assert _push(base) == 0
    assert _push(_commit(repo, "fix(core): claude local metadata")) == 1


_BASE_FAULTS = ("fresh", "tag", "blob", "missing", "wrong-format")
_SOURCE_FAULTS = ("self", "shared", "empty", "absent", "relative", "promisor", "shallow")


@pytest.mark.parametrize("kind", [*_BASE_FAULTS, *_SOURCE_FAULTS])
def test_minimum_git_unusable_upstream_refuses(matrix_repo, kind, capsys):
    repo = matrix_repo
    base = _commit(repo, "fix(core): claude imported identity")
    upstream = repo.parent / "upstream.git"
    if kind not in ("self", "shared", "empty", "absent"):
        upstream = _clone_upstream(repo)
    if kind == "fresh":
        base = _commit(repo, "fix(core): clean but unverified baseline")
    elif kind == "tag":
        _git(repo, "tag", "-a", "base", "-m", "base")
        base = _git(repo, "rev-parse", "refs/tags/base")
    elif kind == "blob":
        base = _git(repo, "rev-parse", "HEAD:file.txt")
    elif kind == "missing":
        base = "f" * 40
    elif kind == "wrong-format":
        base = "f" * 64
    elif kind == "self":
        upstream = repo
    elif kind == "shared":
        _git(repo, "clone", "--bare", "--shared", str(repo), str(upstream))
    elif kind == "empty":
        _git(repo, "init", "--bare", str(upstream))
    elif kind == "relative":
        upstream = Path("../") / upstream.name
    elif kind == "promisor":
        _git(upstream, "config", "remote.origin.promisor", "true")
    elif kind == "shallow":
        (upstream / "shallow").write_text(base + "\n", encoding="utf-8")
    _upstream_pair(repo, upstream, base)
    _blocked(
        capsys, _head(repo), "upstream_base" if kind in _BASE_FAULTS else "ERROR: push blocked:"
    )


@pytest.mark.parametrize("foreign_policy", ["disabled", "missing"])
def test_minimum_git_upstream_ignores_foreign_environment_policy(
    matrix_repo, monkeypatch, foreign_policy
):
    repo = matrix_repo
    base = _commit(repo, "fix(core): claude imported identity")
    upstream = _clone_upstream(repo)
    _upstream_pair(repo, upstream, base)
    state = repo.parent / "foreign-state"
    state.mkdir()
    if foreign_policy == "disabled":
        (state / "booley.toml").write_text("[stealth]\nenabled = false\nbanned_words = []\n")
    monkeypatch.setenv("BOOLEY_DIR", str(state))
    assert _push(base) == 0
    assert _push(_commit(repo, "fix(core): claude fresh metadata")) == 1


def test_minimum_git_native_bundled_upstream_import(matrix_repo):
    from booley.harness.setup.project_git_hook_bundle import build_project_git_hook_bundle

    repo = matrix_repo
    base = _commit(repo, "fix(core): generated upstream instructions by claude")
    upstream = _clone_upstream(repo)
    _upstream_pair(repo, upstream, base)
    bundle = repo / ".booley" / ".managed" / "project-git-hooks.pyz"
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


@pytest.mark.parametrize("ref_kind", ["pull-only", "annotated-tag"])
def test_minimum_git_upstream_import_requires_branch_or_tag(matrix_repo, ref_kind, capsys):
    repo = matrix_repo
    base = _commit(repo, "fix(core): claude pristine import")
    _git(repo, "tag", "-a", "import", "-m", "import point")
    upstream = _clone_upstream(repo)
    _git(upstream, "update-ref", "-d", "refs/heads/main")
    if ref_kind == "pull-only":
        _git(upstream, "update-ref", "-d", "refs/tags/import")
        _git(upstream, "update-ref", "refs/pull/7/head", base)
    _upstream_pair(repo, upstream, base)
    assert _push(base) == (1 if ref_kind == "pull-only" else 0)
    if ref_kind == "pull-only":
        assert "upstream_base is not reachable" in capsys.readouterr().err


@pytest.mark.parametrize("transport", ["https", "ssh"])
def test_upstream_authenticated_transport_keeps_new_commits_checked(repo, monkeypatch, transport):
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.delenv("GIT_SSH", raising=False)
    base = _commit(repo, "fix(core): claude pristine import")
    location = f"{transport}://example.test/upstream.git"
    _upstream_pair(repo, location, base)
    fake = GitCommandFake.install(monkeypatch)
    fake.answer("ls-remote", location, returncode=0, stdout=f"{base}\trefs/heads/main\n".encode())
    assert _push(base) == 0
    assert _push(_commit(repo, "fix(core): claude local metadata")) == 1
    command, options = next(call for call in fake.calls("ls-remote") if location in call[0])
    assert "refs/heads/*" in command and "refs/tags/*" in command
    if transport == "ssh":
        assert "StrictHostKeyChecking=yes" in options["env"]["GIT_SSH_COMMAND"]
    else:
        assert "http.followRedirects=false" in command


@pytest.mark.parametrize(
    ("location", "settings", "reason"),
    [
        ("https://example.test/source", "http.sslVerify=false", _CERT),
        (
            "https://example.test/source",
            "http.https://example.test/source.followRedirects=true",
            _CERT,
        ),
        ("ssh://example.test/source", "core.sshCommand=ssh -o StrictHostKeyChecking=no", _HOST),
        ("http://example.test/repo", "", _NO_AUTHORITY),
        ("git://example.test/repo", "", _NO_AUTHORITY),
        ("ext::unsafe", "", _NO_AUTHORITY),
        ("file:/absolute/repo", "", "literal file:// URL"),
    ],
)
def test_upstream_unprovable_transport_refuses_before_discovery(
    repo, monkeypatch, capsys, location, settings, reason
):
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    _apply_settings(repo, monkeypatch, settings)
    _upstream_pair(repo, location)
    _forbid_discovery(monkeypatch)
    _blocked(capsys, _head(repo), reason)


def test_minimum_git_upstream_ancestry_has_bounded_output(matrix_repo, monkeypatch):
    repo = matrix_repo
    base = _commit(repo, "fix(core): claude pristine import")
    records = []
    for index in range(1, 1025):
        parent = base if index == 1 else f":{index - 1}"
        records.append(
            f"commit refs/heads/source\nmark :{index}\n"
            "committer Real Dev <dev@example.com> 1700000000 +0000\n"
            f"data 13\nupstream work\nfrom {parent}\n\n"
        )
    # Kept on subprocess.run: the helper cannot feed stdin data.
    subprocess.run(
        ["git", "fast-import", "--quiet"],
        cwd=repo,
        # fast-import is a byte protocol; Windows text pipes would translate LF to CRLF.
        input="".join(records).encode("utf-8"),
        check=True,
        capture_output=True,
        timeout=hang_guard_s(),
    )
    upstream = _clone_upstream(repo)
    _git(upstream, "update-ref", "-d", "refs/heads/main")
    _upstream_pair(repo, upstream, base)
    fresh = _commit(repo, "fix(core): ordinary local work")
    fake = GitCommandFake.install(monkeypatch)

    def only_the_new_commit(result, options):
        # Only the single new local commit should cross the output boundary.
        assert result.stdout == (fresh + "\n").encode()
        return result

    fake.rewrite("rev-list", edit=only_the_new_commit)
    assert _push(fresh) == 0


@pytest.mark.parametrize("configuration", ["incomplete-pair", "malformed", "unreadable"])
def test_minimum_git_zero_active_push_needs_no_selected_config(
    matrix_repo, monkeypatch, capsys, configuration
):
    repo = matrix_repo
    config = repo / ".booley" / "booley.toml"
    config.parent.mkdir(exist_ok=True)
    config.write_text(
        {
            "incomplete-pair": '[stealth]\nenabled = true\nupstream_base = ""\n',
            "malformed": "[stealth]\nenabled = true\nallowed_authors = [",
        }.get(configuration, "[stealth]\nenabled = true\n")
    )
    if configuration == "unreadable":
        original = Path.open

        def denied(path, *args, **kwargs):
            if path == config:
                raise PermissionError("selected config unavailable")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "open", denied)
    _forbid_discovery(monkeypatch)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert main() == 0
    assert _push(_ZERO_SHA, _head(repo)) == 0
    assert _push(_head(repo)) == 1
    error = capsys.readouterr().err
    assert (
        "upstream_repository" if configuration == "incomplete-pair" else "cannot read selected"
    ) in error


def test_bundle_import_falls_back_to_flat_commit_policy_module(monkeypatch):
    """Inside the zip bundle the installed package is absent; the flat module is used."""
    import importlib.util

    flat = types.ModuleType("booley_commit_policy")
    flat.validate_push_configuration = lambda root: "flat-module"
    flat.checkout_booley_dir = lambda root: Path(root) / ".booley"
    monkeypatch.setitem(sys.modules, "booley.commit_policy.policy", None)
    monkeypatch.setitem(sys.modules, "booley_commit_policy", flat)
    spec = importlib.util.spec_from_file_location("bundled_pre_push", pre_push_hook.__file__)
    assert spec is not None and spec.loader is not None
    bundled = importlib.util.module_from_spec(spec)
    # Dataclass creation resolves annotations through the module registry.
    monkeypatch.setitem(sys.modules, spec.name, bundled)
    spec.loader.exec_module(bundled)
    assert bundled.validate_push_configuration(Path()) == "flat-module"


# private: `_Inspection.inventory`'s empty-tips guard is unreachable from `main()`, because
# `pre_push_hook.py::_outgoing_range` raises for an update without a commit before calling it.
def test_inventory_without_tips_runs_no_git() -> None:
    inspection = object.__new__(pre_push_hook._Inspection)  # no repository needed
    inspection.git = lambda args, *, data=None: pytest.fail("an empty inventory queried Git")
    assert inspection.inventory([]) == []


# private: `_file_url_path`'s Windows branch is selected by `os.name`, which `main()` on a
# POSIX runner cannot reach.
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
def test_file_url_drive_letter_path_matches_git(decoded, windows, expected):
    """``file:///C:/x`` names drive C on Windows (as Git opens it), not ``\\C:\\x``."""
    assert pre_push_hook._file_url_path(decoded, windows=windows) == expected


@pytest.mark.parametrize("failure", ["unlaunchable", "git-error", "no-output"])
def test_active_push_with_unresolvable_root_refuses(repo, monkeypatch, capsys, failure):
    fake = GitCommandFake.install(monkeypatch)
    if failure == "unlaunchable":
        fake.crash("rev-parse", "--show-toplevel", error=FileNotFoundError("git"))
    else:
        fake.answer(
            "rev-parse", "--show-toplevel", returncode=128 if failure == "git-error" else 0
        )
    _blocked(capsys, _head(repo), "could not resolve the repository root")


def test_active_push_without_destination_arguments_refuses(repo, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["pre-push"])
    assert _push(_head(repo)) == 1
    assert "active push requires destination name and location" in capsys.readouterr().err


@pytest.mark.parametrize("case", ["force", "stale", "intermediate"])
def test_local_destination_history_preserved(repo, capsys, case):
    root = _head(repo)
    old = _commit(repo, "fix: remote work")
    destination = Path(sys.argv[2])
    _git(repo, "push", "--no-verify", str(destination), "HEAD:refs/heads/main")
    if case in ("force", "stale"):
        _git(repo, "reset", "--hard", root)
    forbidden = _commit(repo, "fix: claude intermediate metadata")
    tip = _commit(repo, "fix: clean final metadata")
    if case == "stale":
        _git(repo, "remote", "add", "destination", str(destination))
        _git(repo, "push", "--no-verify", "--force", "destination", "HEAD:refs/heads/main")
        _git(repo, "--git-dir=" + str(destination), "update-ref", "refs/heads/main", old)
        assert _git(repo, "rev-parse", "refs/remotes/destination/main") == tip
    assert _push(tip, old) == 1
    assert forbidden[:12] in capsys.readouterr().err


def _ref_line(local_ref: str, remote_ref: str, repo: Path, *, delete: bool = False) -> str:
    """One pre-push protocol line; a deletion carries the all-zero local object."""
    local = _ZERO_SHA if delete else _head(repo)
    old = _head(repo) if delete else _ZERO_SHA
    return f"{local_ref} {local} {remote_ref} {old}\n"


@pytest.mark.parametrize(
    ("local_ref", "remote_ref", "delete", "blocked"),
    [
        pytest.param("refs/heads/main", "refs/heads/booley/s1", False, True, id="branch"),
        pytest.param("refs/tags/v1", "refs/tags/booley-v1", False, True, id="tag"),
        pytest.param("refs/heads/main", "refs/for/booley", False, True, id="other-namespace"),
        pytest.param("(delete)", "refs/heads/booley/s1", True, False, id="deletion"),
        pytest.param("(delete)", "refs/tags/booley-v1", True, False, id="tag-deletion"),
        pytest.param(
            "refs/heads/booley/s1", "refs/heads/wt/s1", False, False, id="renamed-to-clean-name"
        ),
        pytest.param("refs/heads/main", "refs/heads/main", False, False, id="clean"),
    ],
)
def test_ref_name_check(repo, monkeypatch, capsys, local_ref, remote_ref, delete, blocked):
    monkeypatch.delenv("BOOLEY_SKIP_PUSH_GUARD", raising=False)
    line = _ref_line(local_ref, remote_ref, repo, delete=delete)
    if blocked:
        _forbid_discovery(monkeypatch)
    monkeypatch.setattr(sys, "stdin", io.StringIO(line))
    assert main() == (1 if blocked else 0)
    error = capsys.readouterr().err
    if blocked:
        assert f"{remote_ref}: booley" in error
        assert "git push <remote> <local>:<new-name>" in error
    else:
        assert "banned" not in error


def test_ref_name_check_only_scans_the_name_below_the_namespace(repo, monkeypatch):
    """Git's own `refs/heads/` words are not user content."""
    (repo / ".booley" / "booley.toml").write_text(
        '[stealth]\nenabled = true\nbanned_words = ["heads", "tags"]\n', encoding="utf-8"
    )
    monkeypatch.delenv("BOOLEY_SKIP_PUSH_GUARD", raising=False)
    for ref in ("refs/heads/main", "refs/tags/v1"):
        monkeypatch.setattr(sys, "stdin", io.StringIO(_ref_line(ref, ref, repo)))
        assert main() == 0


@pytest.mark.parametrize("mode", ["stealth-off", "skip-env"])
def test_ref_name_check_is_skipped(repo, monkeypatch, mode):
    if mode == "stealth-off":
        (repo / ".booley" / "booley.toml").write_text(
            "[stealth]\nenabled = false\n", encoding="utf-8"
        )
        monkeypatch.delenv("BOOLEY_SKIP_PUSH_GUARD", raising=False)
    else:
        monkeypatch.setenv("BOOLEY_SKIP_PUSH_GUARD", "1")
    line = _ref_line("refs/heads/main", "refs/heads/booley/s1", repo)
    monkeypatch.setattr(sys, "stdin", io.StringIO(line))
    assert main() == 0
