"""Tests for `booley init` git-side steps (init_git_hooks module).

Currently covers Step 10c — the worktree prune guard (ADR 0028 Decision 10):
Ticket Mode worktrees are created in-container, so their git metadata records
container paths; `gc.worktreePruneExpire=never` keeps a host-side `git gc`
from pruning those registrations.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from booley.harness.init_common import InitContext
from booley.harness.init_git_hooks import (
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
        from booley.harness.init_git_hooks import (
            _PROJECT_HOOK_SCRIPTS,
            _step_project_git_hooks,
        )
        from booley.project_dir import resolve_project_dir

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
        from booley.harness.init_git_hooks import _step_project_git_hooks

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
        from booley.harness.init_git_hooks import _build_commit_msg_hook_body

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
        from booley.harness.init_git_hooks import _build_commit_msg_hook_body

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
        from booley.harness.init_git_hooks import _build_pre_push_hook_body

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
        from booley.harness.init_git_hooks import (
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
    """Step 10d (F-15): CRLF checkouts read as a fully dirty tree inside the
    Session Runtime container; init warns and names the mitigation."""

    @staticmethod
    def _add_file(root: Path, name: str, data: bytes) -> None:
        (root / name).write_bytes(data)
        subprocess.run(
            ["git", "-C", str(root), "add", "-f", name],
            capture_output=True,
            check=True,
        )

    def test_lf_tree_passes(self, tmp_path: Path):
        from booley.harness.init_git_hooks import _step_line_endings

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

    def test_crlf_tree_warns(self, tmp_path: Path):
        # The real F-15 shape: core.autocrlf=true stores LF in the index and
        # checks CRLF out, so index and worktree disagree (`i/lf w/crlf`) and
        # the container's git — which does no conversion — reports every such
        # file as modified.
        from booley.harness.init_git_hooks import _step_line_endings

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
        assert ctx.results[-1].detail == "CRLF working tree"

    def test_crlf_matching_the_index_is_not_a_phantom_diff(self, tmp_path: Path):
        # B5. With autocrlf=false git stores the CRLF bytes as-is: index and
        # worktree agree (`i/crlf w/crlf`), so `git status` is clean on the host
        # AND in the container. Counting these as phantom diffs is a false
        # positive — the check used to warn here purely because the worktree
        # said "crlf", never asking whether the index disagreed.
        from booley.harness.init_git_hooks import _step_line_endings

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
        # to the index. init flagged 8 such files with a fix (`* text eol=lf`,
        # delete + re-checkout) that would have corrupted them.
        from booley.harness.init_git_hooks import _step_line_endings

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
        from booley.harness.init_git_hooks import _step_line_endings

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
        from booley.harness.init_git_hooks import _step_line_endings

        ctx = _ctx(tmp_path)

        _step_line_endings(ctx)

        assert ctx.results[-1].status == "skip"


class TestLineEndingsAutoFix:
    """Step 10d's fixes, split by how much damage each could do: the config
    flip is automatic, the .gitattributes rule is written but never committed,
    and the tree-destroying re-checkout is opt-in and clean-tree-only."""

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
        # appending `* text eol=lf` would override the `-text` exemptions that
        # keep CRLF-native payloads intact. First line loses to every rule
        # below it — which is what a default should do.
        from booley.harness.init_git_hooks import GITATTRIBUTES_RULE, _step_line_endings

        self._crlf_repo(tmp_path)
        (tmp_path / ".gitattributes").write_bytes(b"*.bat -text\n")

        _step_line_endings(_ctx(tmp_path))

        lines = (tmp_path / ".gitattributes").read_text().splitlines()
        assert lines == [GITATTRIBUTES_RULE, "*.bat -text"]

    def test_existing_whole_tree_policy_is_left_alone(self, tmp_path: Path):
        # `* text=auto` is the project stating its own policy. Deliberate
        # choice, not our business.
        from booley.harness.init_git_hooks import _step_line_endings

        self._crlf_repo(tmp_path)
        (tmp_path / ".gitattributes").write_bytes(b"* text=auto\n")

        _step_line_endings(_ctx(tmp_path))

        assert (tmp_path / ".gitattributes").read_bytes() == b"* text=auto\n"

    def test_check_only_changes_nothing(self, tmp_path: Path):
        from booley.harness.init_git_hooks import _step_line_endings

        self._crlf_repo(tmp_path)
        ctx = _ctx(tmp_path, check_only=True)

        _step_line_endings(ctx)

        assert ctx.results[-1].status == "warn"
        assert _autocrlf(tmp_path) == "true"
        assert not (tmp_path / ".gitattributes").exists()
        assert (tmp_path / "a.v").read_bytes() == b"module a;\r\nendmodule\r\n"

    def test_recheckout_is_opt_in(self, tmp_path: Path):
        from booley.harness.init_git_hooks import _step_line_endings

        self._crlf_repo(tmp_path)
        ctx = _ctx(tmp_path)

        _step_line_endings(ctx)

        # Config and attributes fixed; the bytes on disk are not touched
        # without --fix-line-endings.
        assert _autocrlf(tmp_path) == "false"
        assert (tmp_path / "a.v").read_bytes() == b"module a;\r\nendmodule\r\n"
        assert ctx.results[-1].status == "warn"

    def test_fix_flag_rechecks_out_as_lf(self, tmp_path: Path):
        from booley.harness.init_git_hooks import _step_line_endings

        self._crlf_repo(tmp_path)
        ctx = _ctx(tmp_path, fix_line_endings=True)

        _step_line_endings(ctx)

        assert (tmp_path / "a.v").read_bytes() == b"module a;\nendmodule\n"
        assert ctx.results[-1].status == "ok"

    def test_fix_flag_refuses_on_a_dirty_tree(self, tmp_path: Path):
        # The re-checkout deletes every tracked file. Uncommitted work would
        # not survive it, so a dirty tree is a hard stop — init is not the
        # thing that eats someone's afternoon.
        from booley.harness.init_git_hooks import _step_line_endings

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

    def test_gitattributes_rule_survives_the_recheckout(self, tmp_path: Path):
        # .gitattributes is normally tracked, so the re-checkout restores it
        # from the index — writing the rule before the re-checkout handed it
        # straight back to git to overwrite. The tree came out LF either way,
        # which is what made the loss silent: the rule is the part that reaches
        # the user's teammates, and it had quietly gone.
        from booley.harness.init_git_hooks import GITATTRIBUTES_RULE, _step_line_endings

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

    def test_minus_text_files_keep_their_crlf_through_the_recheckout(self, tmp_path: Path):
        # The taxi shape. `*.bat -text` files are stored CRLF and checked out
        # CRLF deliberately; the re-checkout must hand them back byte-identical
        # rather than "fix" a payload that was never broken.
        from booley.harness.init_git_hooks import _step_line_endings

        self._crlf_repo(tmp_path)
        TestLineEndingsStep._add_file(tmp_path, ".gitattributes", b"*.bat -text\n")
        TestLineEndingsStep._add_file(tmp_path, "run.bat", b"@echo off\r\n")
        _git_commit(tmp_path)
        ctx = _ctx(tmp_path, fix_line_endings=True)

        _step_line_endings(ctx)

        assert (tmp_path / "run.bat").read_bytes() == b"@echo off\r\n"
        assert (tmp_path / "a.v").read_bytes() == b"module a;\nendmodule\n"
        assert ctx.results[-1].status == "ok"

    def test_untracked_files_survive_the_recheckout(self, tmp_path: Path):
        from booley.harness.init_git_hooks import _step_line_endings

        self._crlf_repo(tmp_path)
        (tmp_path / "notes.txt").write_bytes(b"keep me\n")
        (tmp_path / ".gitignore").write_bytes(b"notes.txt\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", ".gitignore"],
            capture_output=True,
            check=True,
        )
        _git_commit(tmp_path)
        ctx = _ctx(tmp_path, fix_line_endings=True)

        _step_line_endings(ctx)

        assert (tmp_path / "notes.txt").read_bytes() == b"keep me\n"
        assert ctx.results[-1].status == "ok"
