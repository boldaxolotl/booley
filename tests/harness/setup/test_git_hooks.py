"""Tests for `booley init` git-side steps in the setup.git_hooks module.

Currently covers Step 10c — the worktree prune guard (ADR 0028 Decision 10):
Ticket Mode worktrees are created in-container, so their git metadata records
container paths; `gc.worktreePruneExpire=never` keeps a host-side `git gc`
from pruning those registrations.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from booley.harness.setup.common import InitContext
from booley.harness.setup.git_hooks import (
    WORKTREE_PRUNE_KEY,
    WORKTREE_PRUNE_VALUE,
    _step_worktree_prune_guard,
    read_worktree_prune_expire,
)


def _git_init(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], capture_output=True, check=True)


def _autocrlf(root: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), "config", "--get", "core.autocrlf"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip()


def _local_autocrlf(root: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), "config", "--local", "--get", "core.autocrlf"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip()


def _git_commit(root: Path) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-qm",
            "wip",
        ],
        capture_output=True,
        check=True,
    )


def _ctx(root: Path, *, check_only: bool = False, fix_line_endings: bool = False) -> InitContext:
    return InitContext(
        project_root=root,
        check_only=check_only,
        fix_line_endings=fix_line_endings,
        interactive=False,
    )


class TestWorktreePruneGuardStep:
    def test_sets_config_on_git_repo(self, tmp_path: Path):
        _git_init(tmp_path)
        ctx = _ctx(tmp_path)

        _step_worktree_prune_guard(ctx)

        assert read_worktree_prune_expire(tmp_path) == WORKTREE_PRUNE_VALUE
        assert ctx.results[-1].name == "worktree_prune_guard"
        assert ctx.results[-1].status == "ok"

    def test_skips_when_already_set(self, tmp_path: Path):
        _git_init(tmp_path)
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", WORKTREE_PRUNE_KEY, WORKTREE_PRUNE_VALUE],
            capture_output=True,
            check=True,
        )
        ctx = _ctx(tmp_path)

        _step_worktree_prune_guard(ctx)

        assert ctx.results[-1].status == "skip"
        assert ctx.results[-1].detail == "already set"

    def test_overwrites_wrong_value(self, tmp_path: Path):
        _git_init(tmp_path)
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", WORKTREE_PRUNE_KEY, "3.months.ago"],
            capture_output=True,
            check=True,
        )
        ctx = _ctx(tmp_path)

        _step_worktree_prune_guard(ctx)

        assert read_worktree_prune_expire(tmp_path) == WORKTREE_PRUNE_VALUE
        assert ctx.results[-1].status == "ok"

    def test_check_only_does_not_write(self, tmp_path: Path):
        _git_init(tmp_path)
        ctx = _ctx(tmp_path, check_only=True)

        _step_worktree_prune_guard(ctx)

        assert read_worktree_prune_expire(tmp_path) is None
        assert ctx.results[-1].status == "warn"

    def test_not_a_git_repo_skips(self, tmp_path: Path):
        ctx = _ctx(tmp_path)

        _step_worktree_prune_guard(ctx)

        assert ctx.results[-1].status == "skip"
        assert ctx.results[-1].detail == "not a git repo"


class TestProjectCommitMsgHookVendoring:
    """Step 10b vendors the commit-msg sanitizer into .booley_project/hooks/.
    The SETUP-9 regression: it copied only the three hook scripts, so
    validate_commit_msg's `core.run_command` import crashed every host commit. The
    fix vendors the stdlib-only run_command.py flat beside them."""

    def test_vendors_run_tool_alongside_scripts(self, tmp_path: Path):
        from booley.harness.setup.git_hooks import (
            _PROJECT_HOOK_SCRIPTS,
            _step_project_git_hooks,
        )
        from booley.runtime.project_dir import resolve_project_dir

        _git_init(tmp_path)
        ctx = _ctx(tmp_path)

        _step_project_git_hooks(ctx)

        hooks = resolve_project_dir(tmp_path) / "hooks"
        for name in _PROJECT_HOOK_SCRIPTS:
            assert (hooks / name).is_file(), f"{name} not vendored"
        assert (hooks / "run_command.py").is_file(), "run_command.py not vendored (SETUP-9)"
        assert ctx.results[-1].name == "project_git_hooks"
        assert ctx.results[-1].status == "ok"

    def test_installed_hook_is_lf_not_crlf(self, tmp_path: Path):
        """QA_REPORT D0a: the hook must be written with LF newlines.

        On a Windows host, Path.write_text with the default newline translates
        every \\n to \\r\\n, so the shebang lands as ``#!/bin/sh\\r``. The Linux
        Session Runtime then tries to exec ``/bin/sh\\r`` (ENOENT) and EVERY
        in-container commit fails — hence all of Ticket Mode.
        """
        from booley.harness.setup.git_hooks import _step_project_git_hooks

        _git_init(tmp_path)
        ctx = _ctx(tmp_path)

        _step_project_git_hooks(ctx)

        hook = tmp_path / ".git" / "hooks" / "commit-msg"
        assert hook.is_file(), "commit-msg hook not installed"
        assert b"\r" not in hook.read_bytes(), "hook written with CRLF (D0a)"


class TestCommitMsgHookBody:
    """F-7: a bare `exec python3` breaks every commit on stock Windows — the
    Microsoft Store PATH alias resolves as python3 but exits non-zero with a
    Store nag, and winget's Python ships python.exe with no python3.exe."""

    def test_body_probes_interpreters_instead_of_bare_python3(self, tmp_path: Path):
        from booley.harness.setup.git_hooks import _build_commit_msg_hook_body

        body = _build_commit_msg_hook_body(
            tmp_path,
            tmp_path / ".booley_project" / "hooks",
        )
        assert body.startswith("#!/bin/sh\n")
        assert "exec python3 " not in body
        # The ladder must RUN each candidate (-c '') — `command -v` alone
        # cannot tell the Store alias from a real interpreter.
        assert "for cand in python3 python" in body
        assert "-c ''" in body
        assert "py -3" in body
        # Repo-relative delegation is preserved.
        assert '"$ROOT/.booley_project/hooks/commit_msg_hook.py"' in body


class TestHookInSecondaryWorktree:
    """fpu F-42: a user-made `git worktree add` checkout has its own toplevel,
    and .booley_project/ is untracked — so it exists ONLY in the main worktree.
    `exec`ing $ROOT/.booley_project/hooks/... there is an ENOENT that fails
    EVERY commit; seeding a branch needed --no-verify."""

    @staticmethod
    def _install(main: Path) -> None:
        """A git repo with a vendored hook script + the generated commit-msg hook."""
        from booley.harness.setup.git_hooks import _build_commit_msg_hook_body

        subprocess.run(["git", "init", "-q", "-b", "main", str(main)], check=True)
        for key, val in (("user.email", "t@example.com"), ("user.name", "T")):
            subprocess.run(["git", "-C", str(main), "config", key, val], check=True)
        hooks = main / ".booley_project" / "hooks"
        hooks.mkdir(parents=True)
        (hooks / "commit_msg_hook.py").write_text(
            "import pathlib, sys\npathlib.Path('hook_ran').write_text('yes')\n",
            encoding="utf-8",
        )
        (main / ".gitignore").write_text(".booley_project/\n", encoding="utf-8")
        hook = main / ".git" / "hooks" / "commit-msg"
        hook.write_text(_build_commit_msg_hook_body(main, hooks), encoding="utf-8", newline="\n")
        hook.chmod(0o755)
        subprocess.run(["git", "-C", str(main), "add", ".gitignore"], check=True)
        subprocess.run(["git", "-C", str(main), "commit", "-qm", "init"], check=True)

    @staticmethod
    def _commit(repo: Path, msg: str) -> subprocess.CompletedProcess:
        (repo / f"{msg}.txt").write_text("x", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        return subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", msg],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_commit_succeeds_in_a_secondary_worktree(self, tmp_path: Path):
        main = tmp_path / "main"
        self._install(main)
        wt = tmp_path / "wt"
        subprocess.run(
            ["git", "-C", str(main), "worktree", "add", "-q", str(wt), "-b", "side"],
            check=True,
        )

        result = self._commit(wt, "from-worktree")

        assert result.returncode == 0, result.stderr
        # It did not merely skip: the script was found via the shared git dir.
        assert (wt / "hook_ran").exists()

    def test_missing_script_skips_instead_of_blocking_the_commit(self, tmp_path: Path):
        main = tmp_path / "main"
        self._install(main)
        (main / ".booley_project" / "hooks" / "commit_msg_hook.py").unlink()

        result = self._commit(main, "no-script")

        assert result.returncode == 0, result.stderr
        assert "vendored hook script not found" in result.stderr

    def test_main_worktree_still_runs_the_hook(self, tmp_path: Path):
        main = tmp_path / "main"
        self._install(main)

        assert self._commit(main, "from-main").returncode == 0
        assert (main / "hook_ran").exists()


class TestPrePushFailsClosed:
    """The missing-script fallback must differ per hook.

    commit-msg is a convenience — a missing script skips so local work is never
    wedged (F-42). pre-push is the leak guard (F-17): banned-term scan plus the
    `[stealth] allowed_authors` allowlist. `.booley_project/` is git-ignored, so
    `git clean -xdf` deletes the vendored script while `.git/hooks/pre-push`
    survives; sharing commit-msg's `exit 0` there let the next push sail past
    both checks with one stderr line.
    """

    @staticmethod
    def _repo(root: Path) -> Path:
        """Repo with a bare origin, the pre-push delegator, and a stub script."""
        from booley.harness.setup.git_hooks import _build_pre_push_hook_body

        main = root / "main"
        subprocess.run(["git", "init", "-q", "-b", "main", str(main)], check=True)
        for key, val in (("user.email", "t@example.com"), ("user.name", "T")):
            subprocess.run(["git", "-C", str(main), "config", key, val], check=True)
        subprocess.run(["git", "init", "-q", "--bare", str(root / "origin")], check=True)
        subprocess.run(
            ["git", "-C", str(main), "remote", "add", "origin", str(root / "origin")],
            check=True,
        )

        hooks = main / ".booley_project" / "hooks"
        hooks.mkdir(parents=True)
        # Stands in for the real guard: consumes git's ref lines, passes.
        (hooks / "pre_push_hook.py").write_text(
            "import pathlib, sys\nsys.stdin.read()\npathlib.Path('guard_ran').write_text('yes')\n",
            encoding="utf-8",
        )
        (main / ".gitignore").write_text(".booley_project/\n", encoding="utf-8")
        hook = main / ".git" / "hooks" / "pre-push"
        hook.write_text(_build_pre_push_hook_body(main, hooks), encoding="utf-8", newline="\n")
        hook.chmod(0o755)
        subprocess.run(["git", "-C", str(main), "add", ".gitignore"], check=True)
        subprocess.run(["git", "-C", str(main), "commit", "-qm", "init"], check=True)
        return main

    @staticmethod
    def _push(main: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(main), "push", "-u", "origin", "main"],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_push_succeeds_while_the_guard_is_installed(self, tmp_path: Path):
        main = self._repo(tmp_path)

        result = self._push(main)

        assert result.returncode == 0, result.stderr
        assert (main / "guard_ran").exists(), "delegator never reached the guard"

    def test_missing_pre_push_script_blocks_the_push(self, tmp_path: Path):
        main = self._repo(tmp_path)
        (main / ".booley_project" / "hooks" / "pre_push_hook.py").unlink()

        result = self._push(main)

        assert result.returncode != 0, "fail-open leak guard: push went through"
        assert "REFUSED" in result.stderr
        assert "booley init" in result.stderr
        # And nothing reached the remote.
        listed = subprocess.run(
            ["git", "-C", str(tmp_path / "origin"), "for-each-ref", "--format=%(refname)"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert not listed.stdout.strip()

    def test_only_pre_push_fails_closed(self, tmp_path: Path):
        """The two bodies are built by one helper — pin the divergence."""
        from booley.harness.setup.git_hooks import (
            _build_commit_msg_hook_body,
            _build_pre_push_hook_body,
        )

        hooks = tmp_path / ".booley_project" / "hooks"
        commit_msg = _build_commit_msg_hook_body(tmp_path, hooks)
        pre_push = _build_pre_push_hook_body(tmp_path, hooks)

        assert "    exit 0\n" in commit_msg
        assert "    exit 1\n" not in commit_msg.split("no usable Python")[0]
        assert "    exit 1\n" in pre_push.split("no usable Python")[0]
        # Both still resolve through the shared git dir (F-42 stays fixed).
        for body in (commit_msg, pre_push):
            assert "git rev-parse --path-format=absolute --git-common-dir" in body


class TestLineEndingsStep:
    """Step 10d (F-15): keep host checkouts container-safe."""

    @staticmethod
    def _add_file(root: Path, name: str, data: bytes) -> None:
        (root / name).write_bytes(data)
        subprocess.run(
            ["git", "-C", str(root), "add", "-f", name],
            capture_output=True,
            check=True,
        )

    def test_lf_tree_passes(self, tmp_path: Path):
        from booley.harness.setup.git_hooks import _step_line_endings

        _git_init(tmp_path)
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "core.autocrlf", "false"],
            capture_output=True,
            check=True,
        )
        self._add_file(tmp_path, "a.v", b"module a;\nendmodule\n")
        ctx = _ctx(tmp_path)

        _step_line_endings(ctx)

        assert ctx.results[-1].name == "line_endings"
        assert ctx.results[-1].status == "ok"

    def test_dirty_crlf_tree_is_left_untouched(self, tmp_path: Path, capsys):
        # The real F-15 shape: core.autocrlf=true stores LF in the index and
        # checks CRLF out, so index and worktree disagree (`i/lf w/crlf`) and
        # the container's git — which does no conversion — reports every such
        # file as modified.
        from booley.harness.setup.git_hooks import _step_line_endings

        _git_init(tmp_path)
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "core.autocrlf", "true"],
            capture_output=True,
            check=True,
        )
        self._add_file(tmp_path, "a.v", b"module a;\nendmodule\n")
        (tmp_path / "a.v").write_bytes(b"module a;\r\nendmodule\r\n")
        ctx = _ctx(tmp_path)

        _step_line_endings(ctx)

        assert ctx.results[-1].status == "warn"
        assert ctx.results[-1].detail == "dirty tree"
        output = capsys.readouterr().out
        assert "[!!] 1 tracked file(s) are checked out with CRLF" in output
        assert "[ii] detected core.autocrlf=true" in output

    def test_failed_autocrlf_update_remains_a_warning(self, tmp_path: Path, capsys):
        from booley.harness.setup import git_hooks as init_git_hooks

        _git_init(tmp_path)
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "core.autocrlf", "true"],
            capture_output=True,
            check=True,
        )
        ctx = _ctx(tmp_path)

        with patch.object(init_git_hooks, "_disable_autocrlf", return_value=False):
            init_git_hooks._step_line_endings(ctx)

        assert ctx.results[-1].status == "warn"
        assert ctx.results[-1].detail == "autocrlf update failed"
        output = capsys.readouterr().out
        assert "[!!] core.autocrlf=true" in output
        assert "[ii] detected core.autocrlf=true" not in output

    def test_crlf_matching_the_index_is_not_a_phantom_diff(self, tmp_path: Path):
        # B5. With autocrlf=false git stores the CRLF bytes as-is: index and
        # worktree agree (`i/crlf w/crlf`), so `git status` is clean on the host
        # AND in the container. Counting these as phantom diffs is a false
        # positive — the check used to warn here purely because the worktree
        # said "crlf", never asking whether the index disagreed.
        from booley.harness.setup.git_hooks import _step_line_endings

        _git_init(tmp_path)
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "core.autocrlf", "false"],
            capture_output=True,
            check=True,
        )
        self._add_file(tmp_path, "a.v", b"module a;\r\nendmodule\r\n")
        ctx = _ctx(tmp_path)

        _step_line_endings(ctx)

        assert ctx.results[-1].status == "ok"

    def test_gitattributes_minus_text_files_are_exempt(self, tmp_path: Path):
        # B5, the shape that actually bit taxi: upstream marks CRLF-native
        # payloads carried as text (`*.bat`, vendor register dumps) `-text`, so
        # git never converts them — `i/crlf w/crlf attr/-text`, byte-identical
        # to the index. init flagged 8 such files with a blanket text rule plus
        # a delete + re-checkout, which would have corrupted them.
        from booley.harness.setup.git_hooks import _step_line_endings

        _git_init(tmp_path)
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "core.autocrlf", "false"],
            capture_output=True,
            check=True,
        )
        self._add_file(tmp_path, ".gitattributes", b"* text eol=lf\n*.bat -text\n")
        self._add_file(tmp_path, "run.bat", b"@echo off\r\n")
        self._add_file(tmp_path, "a.v", b"module a;\nendmodule\n")
        ctx = _ctx(tmp_path)

        _step_line_endings(ctx)

        assert ctx.results[-1].status == "ok"

    def test_autocrlf_true_with_lf_tree_is_auto_fixed(self, tmp_path: Path):
        # Nothing on disk is CRLF yet — autocrlf alone is the whole problem,
        # and flipping it is repo-local and reversible, so init just does it.
        from booley.harness.setup.git_hooks import _step_line_endings

        _git_init(tmp_path)
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "core.autocrlf", "true"],
            capture_output=True,
            check=True,
        )
        ctx = _ctx(tmp_path)

        _step_line_endings(ctx)

        assert ctx.results[-1].status == "ok"
        assert _autocrlf(tmp_path) == "false"

    def test_not_a_git_repo_skips(self, tmp_path: Path):
        from booley.harness.setup.git_hooks import _step_line_endings

        ctx = _ctx(tmp_path)

        _step_line_endings(ctx)

        assert ctx.results[-1].status == "skip"

    def test_separate_project_data_repository_is_normalized(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from booley.harness.setup.git_hooks import _step_line_endings

        global_config = tmp_path / "global.gitconfig"
        global_config.write_text("[core]\n\tautocrlf = true\n", encoding="utf-8")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        _git_init(tmp_path)
        project_dir = tmp_path / ".booley_project"
        hooks = project_dir / "hooks"
        hooks.mkdir(parents=True)
        _git_init(project_dir)
        assert _local_autocrlf(tmp_path) == ""
        assert _local_autocrlf(project_dir) == ""
        self._add_file(project_dir, "hooks/post-setup.sh", b"#!/bin/sh\nset -euo pipefail\n")
        _git_commit(project_dir)
        hook = hooks / "post-setup.sh"
        hook.unlink()
        subprocess.run(
            ["git", "-C", str(project_dir), "checkout", "--", "hooks/post-setup.sh"],
            capture_output=True,
            check=True,
        )
        assert b"\r\n" in hook.read_bytes()

        _step_line_endings(_ctx(tmp_path), project_dir)

        assert _local_autocrlf(tmp_path) == "false"
        assert _local_autocrlf(project_dir) == "false"
        assert hook.read_bytes() == b"#!/bin/sh\nset -euo pipefail\n"
        for repository in (tmp_path, project_dir):
            staged = subprocess.run(
                ["git", "-C", str(repository), "diff", "--cached", "--quiet"],
                capture_output=True,
                check=False,
            )
            assert staged.returncode == 0

    def test_unset_effective_policy_is_pinned_locally_in_both_repositories(self, tmp_path: Path):
        from booley.harness.setup.git_hooks import _step_line_endings

        _git_init(tmp_path)
        project_dir = tmp_path / ".booley_project"
        project_dir.mkdir()
        _git_init(project_dir)
        assert _local_autocrlf(tmp_path) == ""
        assert _local_autocrlf(project_dir) == ""

        _step_line_endings(_ctx(tmp_path), project_dir)

        assert _local_autocrlf(tmp_path) == "false"
        assert _local_autocrlf(project_dir) == "false"

    @pytest.mark.parametrize(
        ("effective", "local", "detail"),
        [
            (None, None, "autocrlf unreadable"),
            (False, None, "local autocrlf unreadable"),
        ],
    )
    def test_unreadable_autocrlf_policy_fails_safe(
        self,
        tmp_path: Path,
        effective: bool | None,
        local: bool | None,
        detail: str,
    ):
        from booley.harness.setup import git_hooks as init_git_hooks
        from booley.harness.setup.git_hooks import (
            AutocrlfSetting,
            LineEndingRepository,
        )

        def read_setting(_root: Path, *, local: bool = False):
            value = local_value if local else effective_value
            return None if value is None else AutocrlfSetting(value, is_set=True)

        effective_value = effective
        local_value = local
        repository = LineEndingRepository("project-checkout", tmp_path)
        with patch.object(init_git_hooks, "read_autocrlf_setting", side_effect=read_setting):
            outcome = init_git_hooks._repair_repository_line_endings(
                repository,
                check_only=False,
            )

        assert outcome.status == "warn"
        assert outcome.detail == detail

    def test_invalid_autocrlf_boolean_is_unreadable(self, tmp_path: Path):
        from booley.harness.setup import git_hooks as init_git_hooks

        probe = subprocess.CompletedProcess(
            args=["git", "config"],
            returncode=0,
            stdout="not-a-boolean\n",
            stderr="",
        )
        with patch.object(init_git_hooks.subprocess, "run", return_value=probe):
            setting = init_git_hooks.read_autocrlf_setting(tmp_path)

        assert setting is None


class TestLineEndingRepositoryDiscovery:
    def test_project_data_inside_outer_repository_is_deduplicated(self, tmp_path: Path):
        from booley.harness.setup.git_hooks import discover_line_ending_repositories

        _git_init(tmp_path)
        project_dir = tmp_path / ".booley_project"
        project_dir.mkdir()

        discovery = discover_line_ending_repositories(tmp_path, project_dir)

        assert discovery.failures == ()
        assert [(repo.role, repo.root) for repo in discovery.repositories] == [
            ("project-checkout", tmp_path.resolve())
        ]

    def test_separate_project_data_repository_is_discovered(self, tmp_path: Path):
        from booley.harness.setup.git_hooks import discover_line_ending_repositories

        _git_init(tmp_path)
        project_dir = tmp_path / ".booley_project"
        project_dir.mkdir()
        _git_init(project_dir)

        discovery = discover_line_ending_repositories(tmp_path, project_dir)

        assert discovery.failures == ()
        assert [(repo.role, repo.root) for repo in discovery.repositories] == [
            ("project-checkout", tmp_path.resolve()),
            ("project-data", project_dir.resolve()),
        ]

    def test_missing_project_data_prevents_aggregate_success(self, tmp_path: Path):
        from booley.harness.setup.git_hooks import _step_line_endings

        _git_init(tmp_path)
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "core.autocrlf", "false"],
            capture_output=True,
            check=True,
        )
        ctx = _ctx(tmp_path)

        _step_line_endings(ctx, tmp_path / "missing project data")

        assert ctx.results[-1].status == "warn"
        assert "project-data: directory does not exist" in ctx.results[-1].detail

    def test_check_only_does_not_mutate_either_repository(self, tmp_path: Path):
        from booley.harness.setup.git_hooks import _step_line_endings

        _git_init(tmp_path)
        project_dir = tmp_path / ".booley_project"
        project_dir.mkdir()
        _git_init(project_dir)
        for repository in (tmp_path, project_dir):
            subprocess.run(
                ["git", "-C", str(repository), "config", "core.autocrlf", "true"],
                capture_output=True,
                check=True,
            )
        ctx = _ctx(tmp_path, check_only=True)

        _step_line_endings(ctx, project_dir)

        assert ctx.results[-1].status == "warn"
        for repository in (tmp_path, project_dir):
            assert _local_autocrlf(repository) == "true"
            assert not (repository / ".gitattributes").exists()
            staged = subprocess.run(
                ["git", "-C", str(repository), "diff", "--cached", "--quiet"],
                capture_output=True,
                check=False,
            )
            assert staged.returncode == 0

    def test_dirty_project_data_is_refused_while_outer_is_repaired(self, tmp_path: Path):
        from booley.harness.setup.git_hooks import _step_line_endings

        _git_init(tmp_path)
        project_dir = tmp_path / ".booley_project"
        hooks = project_dir / "hooks"
        hooks.mkdir(parents=True)
        _git_init(project_dir)
        for repository in (tmp_path, project_dir):
            subprocess.run(
                ["git", "-C", str(repository), "config", "core.autocrlf", "true"],
                capture_output=True,
                check=True,
            )
        TestLineEndingsStep._add_file(
            project_dir,
            "hooks/post-setup.sh",
            b"#!/bin/sh\nset -euo pipefail\n",
        )
        _git_commit(project_dir)
        hook = hooks / "post-setup.sh"
        dirty = b"#!/bin/sh\r\nset -euo pipefail\r\necho local edit\r\n"
        hook.write_bytes(dirty)
        ctx = _ctx(tmp_path)

        _step_line_endings(ctx, project_dir)

        assert _local_autocrlf(tmp_path) == "false"
        assert hook.read_bytes() == dirty
        assert ctx.results[-1].status == "warn"
        assert "project-data: dirty tree" in ctx.results[-1].detail
        for repository in (tmp_path, project_dir):
            staged = subprocess.run(
                ["git", "-C", str(repository), "diff", "--cached", "--quiet"],
                capture_output=True,
                check=False,
            )
            assert staged.returncode == 0


class TestLineEndingsAutoFix:
    """Step 10d repairs clean trees and never overwrites local work."""

    def _crlf_repo(self, tmp_path: Path) -> Path:
        """A Git-for-Windows-shaped repo: LF in the index, CRLF on disk.

        git does the CRLF checkout itself (delete + restore) rather than the
        test hand-writing the bytes. It has to: a hand-written CRLF file
        leaves a stale stat cache, and `git status` then reports it modified —
        which a real Windows checkout does not do, and which would hide the
        dirty-tree refusal behind a false positive.
        """
        _git_init(tmp_path)
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "core.autocrlf", "true"],
            capture_output=True,
            check=True,
        )
        TestLineEndingsStep._add_file(tmp_path, "a.v", b"module a;\nendmodule\n")
        _git_commit(tmp_path)
        (tmp_path / "a.v").unlink()
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "--", "a.v"],
            capture_output=True,
            check=True,
        )
        assert (tmp_path / "a.v").read_bytes() == b"module a;\r\nendmodule\r\n"
        return tmp_path

    def test_gitattributes_rule_is_prepended_not_appended(self, tmp_path: Path):
        # The ordering guard. git resolves attributes last-match-wins, so
        # appending the whole-tree LF default would override `-text` exemptions
        # that keep CRLF-native payloads intact. First line loses to every rule
        # below it — which is what a default should do.
        from booley.harness.setup.git_hooks import GITATTRIBUTES_RULE, _step_line_endings

        self._crlf_repo(tmp_path)
        (tmp_path / ".gitattributes").write_bytes(b"*.bat -text\n")

        _step_line_endings(_ctx(tmp_path))

        lines = (tmp_path / ".gitattributes").read_text().splitlines()
        assert lines == [GITATTRIBUTES_RULE, "*.bat -text"]

    def test_gitattributes_rule_preserves_binary_detection(self, tmp_path: Path):
        from booley.harness.setup.git_hooks import GITATTRIBUTES_RULE, _step_line_endings

        _git_init(tmp_path)
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "core.autocrlf", "true"],
            capture_output=True,
            check=True,
        )
        png_bytes = b"\x89PNG\r\n\x1a\n\x00binary\r\npayload\x00"
        TestLineEndingsStep._add_file(tmp_path, "image.png", png_bytes)
        _git_commit(tmp_path)

        _step_line_endings(_ctx(tmp_path))

        assert (tmp_path / "image.png").read_bytes() == png_bytes
        diff = subprocess.run(
            ["git", "-C", str(tmp_path), "diff", "--name-only", "--", "image.png"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert diff.stdout == ""
        assert (tmp_path / ".gitattributes").read_text().splitlines() == [GITATTRIBUTES_RULE]

    def test_existing_whole_tree_policy_is_left_alone(self, tmp_path: Path):
        # `* text=auto` is the project stating its own policy. Deliberate
        # choice, not our business.
        from booley.harness.setup.git_hooks import _step_line_endings

        self._crlf_repo(tmp_path)
        (tmp_path / ".gitattributes").write_bytes(b"* text=auto\n")

        _step_line_endings(_ctx(tmp_path))

        assert (tmp_path / ".gitattributes").read_bytes() == b"* text=auto\n"

    def test_check_only_previews_the_one_run_normalization(self, tmp_path: Path, capsys):
        from booley.harness.setup.git_hooks import _step_line_endings

        self._crlf_repo(tmp_path)
        ctx = _ctx(tmp_path, check_only=True)

        _step_line_endings(ctx)

        assert ctx.results[-1].status == "warn"
        assert _autocrlf(tmp_path) == "true"
        assert not (tmp_path / ".gitattributes").exists()
        assert (tmp_path / "a.v").read_bytes() == b"module a;\r\nendmodule\r\n"
        output = capsys.readouterr().out
        assert "[!!] 1 tracked file(s) are checked out with CRLF" in output
        assert "[!!] core.autocrlf=true" in output
        assert "[ii]" not in output
        assert "would normalize 1 tracked file(s) to LF in place" in output
        assert "--fix-line-endings" not in output

    def test_clean_crlf_checkout_is_auto_fixed_in_one_run(self, tmp_path: Path, capsys):
        from booley.harness.setup.git_hooks import _step_line_endings

        self._crlf_repo(tmp_path)
        ctx = _ctx(tmp_path)

        _step_line_endings(ctx)

        assert _autocrlf(tmp_path) == "false"
        assert (tmp_path / "a.v").read_bytes() == b"module a;\nendmodule\n"
        tracked_status = subprocess.run(
            ["git", "-C", str(tmp_path), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            check=True,
        )
        staged_diff = subprocess.run(
            ["git", "-C", str(tmp_path), "diff", "--cached", "--quiet"],
            capture_output=True,
            check=False,
        )
        assert tracked_status.stdout == ""
        assert staged_diff.returncode == 0
        assert ctx.results[-1].status == "ok"
        output = capsys.readouterr().out
        assert "[ii] detected 1 tracked file(s) are checked out with CRLF" in output
        assert "[ii] detected core.autocrlf=true" in output
        assert "[!!]" not in output

    def test_fix_flag_rechecks_out_as_lf(self, tmp_path: Path):
        from booley.harness.setup.git_hooks import _step_line_endings

        self._crlf_repo(tmp_path)
        ctx = _ctx(tmp_path, fix_line_endings=True)

        _step_line_endings(ctx)

        assert (tmp_path / "a.v").read_bytes() == b"module a;\nendmodule\n"
        assert ctx.results[-1].status == "ok"

    def test_rerun_heals_stale_index_from_earlier_normalization(self, tmp_path: Path):
        from booley.harness.setup.git_hooks import _step_line_endings

        self._crlf_repo(tmp_path)
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "core.autocrlf", "false"],
            capture_output=True,
            check=True,
        )
        (tmp_path / "a.v").write_bytes(b"module a;\nendmodule\n")
        assert (
            subprocess.run(
                ["git", "-C", str(tmp_path), "status", "--porcelain", "--untracked-files=no"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            == " M a.v\n"
        )

        ctx = _ctx(tmp_path)
        _step_line_endings(ctx)

        assert (
            subprocess.run(
                ["git", "-C", str(tmp_path), "status", "--porcelain", "--untracked-files=no"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            == ""
        )
        assert ctx.results[-1].status == "ok"
        assert ctx.results[-1].detail == "index refreshed"

    def test_partial_rewrite_refreshes_successful_paths(self, tmp_path: Path):
        from booley.harness.setup import git_hooks as init_git_hooks

        self._crlf_repo(tmp_path)
        TestLineEndingsStep._add_file(tmp_path, "b.v", b"module b;\nendmodule\n")
        _git_commit(tmp_path)
        (tmp_path / "b.v").unlink()
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "--", "b.v"],
            capture_output=True,
            check=True,
        )
        real_rewrite = init_git_hooks._rewrite_from_stage

        def fail_second(path: Path, replacement: Path, expected: bytes) -> str | None:
            if path.name == "b.v":
                return "simulated second-file failure"
            return real_rewrite(path, replacement, expected)

        with patch.object(init_git_hooks, "_rewrite_from_stage", side_effect=fail_second):
            ctx = _ctx(tmp_path)
            init_git_hooks._step_line_endings(ctx)

        assert (tmp_path / "a.v").read_bytes() == b"module a;\nendmodule\n"
        assert (tmp_path / "b.v").read_bytes() == b"module b;\r\nendmodule\r\n"
        status = subprocess.run(
            ["git", "-C", str(tmp_path), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert status.stdout == ""
        assert ctx.results[-1].status == "warn"

        retry = _ctx(tmp_path)
        init_git_hooks._step_line_endings(retry)

        assert (tmp_path / "b.v").read_bytes() == b"module b;\nendmodule\n"
        assert retry.results[-1].status == "ok"

    def test_phantom_status_failure_includes_git_context(self, tmp_path: Path):
        from booley.harness.setup import git_hooks as init_git_hooks

        with patch.object(
            init_git_hooks.subprocess,
            "run",
            side_effect=OSError("git unavailable"),
        ):
            paths, error = init_git_hooks._tracked_phantom_paths(tmp_path)

        assert paths is None
        assert error is not None
        assert f"git -C {tmp_path} status --porcelain" in error
        assert "git unavailable" in error

    def test_fix_flag_ignores_init_created_untracked_gitattributes(self, tmp_path: Path):
        from booley.harness.setup.git_hooks import _step_line_endings

        # The compatibility flag remains harmless on the already-fixed output
        # of an ordinary first run, including its untracked policy file.
        self._crlf_repo(tmp_path)
        _step_line_endings(_ctx(tmp_path))
        assert (tmp_path / ".gitattributes").exists()
        assert (
            "?? .gitattributes"
            in subprocess.run(
                ["git", "-C", str(tmp_path), "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        )

        ctx = _ctx(tmp_path, fix_line_endings=True)
        _step_line_endings(ctx)

        assert (tmp_path / "a.v").read_bytes() == b"module a;\nendmodule\n"
        assert ctx.results[-1].status == "ok"

    def test_fix_flag_refuses_on_a_dirty_tree(self, tmp_path: Path):
        # Replacing even only the affected paths could lose uncommitted work,
        # so a dirty tree is a hard stop.
        from booley.harness.setup.git_hooks import _step_line_endings

        self._crlf_repo(tmp_path)
        (tmp_path / "b.v").write_bytes(b"module b;\nendmodule\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "b.v"],
            capture_output=True,
            check=True,
        )
        ctx = _ctx(tmp_path, fix_line_endings=True)

        _step_line_endings(ctx)

        assert ctx.results[-1].status == "warn"
        assert ctx.results[-1].detail == "dirty tree"
        assert (tmp_path / "a.v").read_bytes() == b"module a;\r\nendmodule\r\n"

    def test_gitattributes_rule_survives_normalization(self, tmp_path: Path):
        # .gitattributes is normally tracked, so its staged replacement comes
        # from the index. Writing the rule before applying that replacement
        # would silently lose the part of the fix that reaches teammates.
        from booley.harness.setup.git_hooks import GITATTRIBUTES_RULE, _step_line_endings

        self._crlf_repo(tmp_path)
        TestLineEndingsStep._add_file(tmp_path, ".gitattributes", b"*.bat -text\n")
        _git_commit(tmp_path)
        ctx = _ctx(tmp_path, fix_line_endings=True)

        _step_line_endings(ctx)

        assert (tmp_path / ".gitattributes").read_text().splitlines() == [
            GITATTRIBUTES_RULE,
            "*.bat -text",
        ]
        assert ctx.results[-1].status == "ok"

    def test_minus_text_files_keep_their_crlf_through_normalization(self, tmp_path: Path):
        # The taxi shape. `*.bat -text` files are stored CRLF and checked out
        # CRLF deliberately; normalization must leave them byte-identical rather
        # than "fix" a payload that was never broken.
        from booley.harness.setup.git_hooks import _step_line_endings

        self._crlf_repo(tmp_path)
        TestLineEndingsStep._add_file(tmp_path, ".gitattributes", b"*.bat -text\n")
        TestLineEndingsStep._add_file(tmp_path, "run.bat", b"@echo off\r\n")
        _git_commit(tmp_path)
        ctx = _ctx(tmp_path, fix_line_endings=True)

        _step_line_endings(ctx)

        assert (tmp_path / "run.bat").read_bytes() == b"@echo off\r\n"
        assert (tmp_path / "a.v").read_bytes() == b"module a;\nendmodule\n"
        assert ctx.results[-1].status == "ok"

    def test_untracked_files_survive_normalization(self, tmp_path: Path):
        from booley.harness.setup.git_hooks import _step_line_endings

        self._crlf_repo(tmp_path)
        (tmp_path / "notes.txt").write_bytes(b"keep me\n")
        assert (
            "?? notes.txt"
            in subprocess.run(
                ["git", "-C", str(tmp_path), "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        )
        ctx = _ctx(tmp_path, fix_line_endings=True)

        _step_line_endings(ctx)

        assert (tmp_path / "notes.txt").read_bytes() == b"keep me\n"
        assert ctx.results[-1].status == "ok"

    def test_auto_fix_leaves_unaffected_skip_worktree_edits_untouched(self, tmp_path: Path):
        from booley.harness.setup.git_hooks import _step_line_endings

        self._crlf_repo(tmp_path)
        TestLineEndingsStep._add_file(tmp_path, "local.v", b"module local;\nendmodule\n")
        _git_commit(tmp_path)
        subprocess.run(
            ["git", "-C", str(tmp_path), "update-index", "--skip-worktree", "local.v"],
            capture_output=True,
            check=True,
        )
        local_edit = b"module local;\n  localparam int KEEP = 1;\nendmodule\n"
        (tmp_path / "local.v").write_bytes(local_edit)
        assert not subprocess.run(
            ["git", "-C", str(tmp_path), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout

        ctx = _ctx(tmp_path)
        _step_line_endings(ctx)

        assert (tmp_path / "local.v").read_bytes() == local_edit
        assert (tmp_path / "a.v").read_bytes() == b"module a;\nendmodule\n"
        assert ctx.results[-1].status == "ok"

    def test_auto_fix_refuses_an_affected_skip_worktree_path(self, tmp_path: Path):
        from booley.harness.setup.git_hooks import _step_line_endings

        self._crlf_repo(tmp_path)
        subprocess.run(
            ["git", "-C", str(tmp_path), "update-index", "--skip-worktree", "a.v"],
            capture_output=True,
            check=True,
        )
        local_edit = b"module a;\r\n  localparam int KEEP = 1;\r\nendmodule\r\n"
        (tmp_path / "a.v").write_bytes(local_edit)
        assert not subprocess.run(
            ["git", "-C", str(tmp_path), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout

        ctx = _ctx(tmp_path)
        _step_line_endings(ctx)

        assert (tmp_path / "a.v").read_bytes() == local_edit
        assert ctx.results[-1].status == "warn"

    def test_auto_fix_treats_tracked_names_as_literals(self, tmp_path: Path):
        """A candidate filename must not expand onto a hidden neighboring edit."""
        from booley.harness.setup.git_hooks import _step_line_endings

        _git_init(tmp_path)
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "core.autocrlf", "true"],
            capture_output=True,
            check=True,
        )
        TestLineEndingsStep._add_file(tmp_path, "a[1].txt", b"candidate\n")
        TestLineEndingsStep._add_file(tmp_path, "a1.txt", b"neighbor\n")
        _git_commit(tmp_path)
        (tmp_path / "a[1].txt").unlink()
        (tmp_path / "a1.txt").unlink()
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "--", "a[1].txt", "a1.txt"],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "update-index", "--assume-unchanged", "a1.txt"],
            capture_output=True,
            check=True,
        )
        hidden_edit = b"LOCAL EDIT THAT MUST SURVIVE\n"
        (tmp_path / "a1.txt").write_bytes(hidden_edit)

        _step_line_endings(_ctx(tmp_path))

        assert (tmp_path / "a[1].txt").read_bytes() == b"candidate\n"
        assert (tmp_path / "a1.txt").read_bytes() == hidden_edit

    def test_explicit_crlf_attribute_is_not_a_phantom_diff(self, tmp_path: Path):
        from booley.harness.setup.git_hooks import _step_line_endings

        _git_init(tmp_path)
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "core.autocrlf", "false"],
            capture_output=True,
            check=True,
        )
        TestLineEndingsStep._add_file(tmp_path, ".gitattributes", b"*.txt text eol=crlf\n")
        TestLineEndingsStep._add_file(tmp_path, "intentional.txt", b"alpha\nbeta\n")
        _git_commit(tmp_path)
        (tmp_path / "intentional.txt").unlink()
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "--", "intentional.txt"],
            capture_output=True,
            check=True,
        )
        assert (tmp_path / "intentional.txt").read_bytes() == b"alpha\r\nbeta\r\n"

        ctx = _ctx(tmp_path)
        _step_line_endings(ctx)

        assert (tmp_path / "intentional.txt").read_bytes() == b"alpha\r\nbeta\r\n"
        assert ctx.results[-1].status == "ok"

    @pytest.mark.parametrize("true_value", ["yes", "on", "1"])
    def test_autocrlf_true_aliases_are_disabled(self, tmp_path: Path, true_value: str):
        from booley.harness.setup.git_hooks import _step_line_endings

        _git_init(tmp_path)
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "core.autocrlf", true_value],
            capture_output=True,
            check=True,
        )

        _step_line_endings(_ctx(tmp_path))

        assert _autocrlf(tmp_path) == "false"

    def test_unreadable_eol_scan_never_reports_container_safe(self, tmp_path: Path):
        from booley.harness.setup import git_hooks as init_git_hooks

        _git_init(tmp_path)
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "core.autocrlf", "false"],
            capture_output=True,
            check=True,
        )
        ctx = _ctx(tmp_path)

        with patch.object(init_git_hooks, "_crlf_worktree_files", return_value=None):
            init_git_hooks._step_line_endings(ctx)

        assert ctx.results[-1].status == "warn"
        assert ctx.results[-1].detail == "EOL scan unreadable"

    def test_unreadable_post_normalization_scan_never_reports_success(self, tmp_path: Path):
        from booley.harness.setup import git_hooks as init_git_hooks

        self._crlf_repo(tmp_path)
        real_count = init_git_hooks._count_crlf_worktree_files
        calls = 0

        def lose_verification(root: Path) -> int | None:
            nonlocal calls
            calls += 1
            return None if calls == 1 else real_count(root)

        # _step_line_endings gets candidates through _crlf_worktree_files;
        # this wrapper targets its sole post-normalization count.
        with patch.object(
            init_git_hooks, "_count_crlf_worktree_files", side_effect=lose_verification
        ):
            ctx = _ctx(tmp_path)
            init_git_hooks._step_line_endings(ctx)

        assert ctx.results[-1].status == "warn"
        assert ctx.results[-1].detail == "EOL verification unreadable"

    @pytest.mark.skipif(os.name == "nt", reason="Windows filenames are Unicode")
    def test_non_utf8_tracked_filename_is_normalized(self, tmp_path: Path):
        from booley.harness.setup.git_hooks import _step_line_endings

        _git_init(tmp_path)
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "core.autocrlf", "true"],
            capture_output=True,
            check=True,
        )
        name = os.fsdecode(b"bad-\xff.txt")
        TestLineEndingsStep._add_file(tmp_path, name, b"alpha\nbeta\n")
        _git_commit(tmp_path)
        (tmp_path / name).unlink()
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "--", name],
            capture_output=True,
            check=True,
        )

        ctx = _ctx(tmp_path)
        _step_line_endings(ctx)

        assert (tmp_path / name).read_bytes() == b"alpha\nbeta\n"
        assert ctx.results[-1].status == "ok"

    def test_failed_smudge_filter_leaves_original_file_present(self, tmp_path: Path):
        from booley.harness.setup.git_hooks import _step_line_endings

        self._crlf_repo(tmp_path)
        TestLineEndingsStep._add_file(tmp_path, ".gitattributes", b"a.v filter=fail\n")
        _git_commit(tmp_path)
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "filter.fail.clean", "cat"],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(tmp_path),
                "config",
                "filter.fail.smudge",
                "git booley-filter-must-fail",
            ],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "filter.fail.required", "true"],
            capture_output=True,
            check=True,
        )
        original = (tmp_path / "a.v").read_bytes()

        ctx = _ctx(tmp_path)
        _step_line_endings(ctx)

        assert (tmp_path / "a.v").read_bytes() == original
        assert ctx.results[-1].status == "warn"

    def test_edit_after_cleanliness_probe_is_not_overwritten(self, tmp_path: Path):
        from booley.harness.setup import git_hooks as init_git_hooks

        self._crlf_repo(tmp_path)
        local_edit = b"module a;\r\n  localparam int KEEP = 1;\r\nendmodule\r\n"

        def edit_during_guard(_root: Path, _paths: list[str]) -> list[str]:
            (tmp_path / "a.v").write_bytes(local_edit)
            return []

        ctx = _ctx(tmp_path)
        with patch.object(init_git_hooks, "_protected_index_paths", side_effect=edit_during_guard):
            init_git_hooks._step_line_endings(ctx)

        assert (tmp_path / "a.v").read_bytes() == local_edit
        assert ctx.results[-1].status == "warn"

    @pytest.mark.skipif(not hasattr(os, "link"), reason="hard links unavailable")
    def test_hardlinked_candidate_is_refused_without_breaking_link(self, tmp_path: Path):
        from booley.harness.setup.git_hooks import _step_line_endings

        self._crlf_repo(tmp_path)
        mirror = tmp_path / "untracked-mirror.v"
        os.link(tmp_path / "a.v", mirror)
        before = (tmp_path / "a.v").stat()

        ctx = _ctx(tmp_path)
        _step_line_endings(ctx)

        after = (tmp_path / "a.v").stat()
        assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
        assert (tmp_path / "a.v").read_bytes() == mirror.read_bytes()
        assert ctx.results[-1].status == "warn"
