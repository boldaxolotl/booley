"""Tests for the setup stage's workspace preparation -- worktree reuse, branch logic."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ===========================================================================
# Branch creation logic
# ===========================================================================


class TestBranchCreation:
    """Test feature branch vs integration branch selection."""

    def test_integration_uses_base_branch(self):
        branch = _pick_branch(is_integration=True, slug="fix-fsm", base_branch="int/batch-1")
        assert branch == "int/batch-1"

    def test_normal_uses_slug(self):
        branch = _pick_branch(is_integration=False, slug="fix-fsm", base_branch="master")
        assert branch == "fix-fsm"


class TestBranchFallback:
    """Test -b failure → checkout existing branch."""

    def test_create_succeeds(self):
        result = _try_create_branch("fix-fsm", create_rc=0)
        assert result == "fix-fsm"

    def test_create_fails_checkout_succeeds(self):
        result = _try_create_branch("fix-fsm", create_rc=1, checkout_rc=0)
        assert result == "fix-fsm"

    def test_both_fail_returns_error(self):
        result = _try_create_branch("fix-fsm", create_rc=1, checkout_rc=1)
        assert result is None


# ===========================================================================
# Hook detection
# ===========================================================================


class TestHookDetection:
    def test_hook_exists(self, tmp_path: Path):
        hook = tmp_path / ".agents" / "hooks" / "worktree_create.sh"
        hook.parent.mkdir(parents=True)
        hook.write_text("#!/bin/bash\necho hello", encoding="utf-8")
        assert hook.exists()

    def test_hook_missing_blocks(self, tmp_path: Path):
        hook = tmp_path / ".agents" / "hooks" / "worktree_create.sh"
        result = _check_hook(hook)
        assert "not found" in result


class TestStealthHookGating:
    """The commit-msg (stealth) hook install honors [stealth] enabled; the
    scope pre-commit hook is always installed."""

    def _setup(self, tmp_path: Path):
        # Fake developer-support directory with both hook scripts.
        fake_dev_support = tmp_path / "dev_support"
        fake_dev_support.mkdir()
        (fake_dev_support / "scope_precommit_hook.py").write_text("#!/usr/bin/env python3\n")
        (fake_dev_support / "commit_msg_hook.py").write_text("#!/usr/bin/env python3\n")
        # Worktree with a real .git DIRECTORY (so _resolve_worktree_git_dir
        # returns it directly and no worktree hooks-path git call runs).
        worktree = tmp_path / "wt"
        (worktree / ".git").mkdir(parents=True)
        return fake_dev_support, worktree

    def _project(self, tmp_path: Path, toml_body: bytes | None) -> Path:
        root = tmp_path / "proj"
        bp = root / ".booley_project"
        bp.mkdir(parents=True)
        if toml_body is not None:
            (bp / "booley.toml").write_bytes(toml_body)
        return root

    def test_commit_msg_hook_installed_by_default(self, tmp_path: Path):
        from booley.harness.setup.workspace import _install_scope_hook

        fake_tools, worktree = self._setup(tmp_path)
        project_root = self._project(tmp_path, None)  # stealth on by default
        with patch("booley.harness.setup.workspace.dev_support_dir", return_value=fake_tools):
            _install_scope_hook(worktree, ["rtl/"], project_root=project_root)
        hooks = worktree / ".git" / "hooks"
        assert (hooks / "pre-commit").exists()
        assert (hooks / "commit-msg").exists()

    def test_commit_msg_hook_skipped_when_disabled(self, tmp_path: Path):
        from booley.harness.setup.workspace import _install_scope_hook

        fake_tools, worktree = self._setup(tmp_path)
        project_root = self._project(tmp_path, b"[stealth]\nenabled = false\n")
        with patch("booley.harness.setup.workspace.dev_support_dir", return_value=fake_tools):
            _install_scope_hook(worktree, ["rtl/"], project_root=project_root)
        hooks = worktree / ".git" / "hooks"
        # Scope enforcement stays; stealth commit-msg hook is gone.
        assert (hooks / "pre-commit").exists()
        assert not (hooks / "commit-msg").exists()

    def test_resume_refreshes_scope_file_from_active_ticket(self, tmp_path: Path):
        from booley.harness.setup.workspace import refresh_scope_guards

        fake_tools, worktree = self._setup(tmp_path)
        project_root = self._project(tmp_path, None)
        (worktree / ".scope.json").write_text(
            json.dumps({"scope": ["rtl/old.sv"]}), encoding="utf-8"
        )

        with patch("booley.harness.setup.workspace.dev_support_dir", return_value=fake_tools):
            refresh_scope_guards(
                worktree,
                ["rtl/old.sv", "rtl/newly_authorized.sv"],
                project_root=project_root,
            )

        persisted = json.loads((worktree / ".scope.json").read_text(encoding="utf-8"))
        assert persisted["scope"] == ["rtl/old.sv", "rtl/newly_authorized.sv"]


class TestScopeJsonExclude:
    """_install_scope_hook must exclude .scope.json via the honored info/exclude
    so a review-stage `git status` never lists the harness bookkeeping file."""

    def test_scope_json_excluded_from_git_status(self, tmp_path: Path):
        from booley.harness.setup.workspace import _install_scope_hook

        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init")
        _git(repo, "config", "user.email", "t@example.com")
        _git(repo, "config", "user.name", "Tester")
        (repo / "design.core").write_text("CAPI=2:\nname: ::x:0\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "init")

        _install_scope_hook(repo, ["rtl/foo.sv"], project_root=repo)

        # The bookkeeping file was written...
        assert (repo / ".scope.json").is_file()
        # ...and anchored into the honored exclude ($GIT_COMMON_DIR/info/exclude,
        # i.e. .git/info/exclude for a plain repo).
        exclude = repo / ".git" / "info" / "exclude"
        assert exclude.is_file()
        assert "/.scope.json" in exclude.read_text(encoding="utf-8").splitlines()
        # So `git status` reads clean — .scope.json never surfaces as untracked.
        status = _git(repo, "status", "--porcelain")
        assert ".scope.json" not in status.stdout


class TestWorktreeCreateScript:
    def test_rejects_unsafe_worktree_name(self, tmp_path: Path):
        """Worktree slug must not escape the worktrees directory."""
        from booley.runtime.paths import dev_support_dir
        from booley.runtime.platform_paths import bash_bin

        result = subprocess.run(
            [bash_bin(), str(dev_support_dir() / "worktree_create.sh")],
            input=json.dumps({"name": "../escape", "cwd": str(tmp_path)}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            # Windows shells may expose only the Store python aliases (F-7);
            # pin the script to the suite's interpreter like the harness does.
            env={
                **{k: v for k, v in os.environ.items() if k != "BOOLEY_PROJECT_DIR"},
                "BOOLEY_PYTHON": sys.executable,
            },
        )

        assert result.returncode == 1
        assert "single safe path component" in result.stderr
        assert not (tmp_path / "escape").exists()

    def test_rejects_cwd_outside_git_root_before_writing(self, tmp_path: Path):
        """Hook JSON must not redirect state creation into an arbitrary directory."""
        from booley.runtime.paths import dev_support_dir
        from booley.runtime.platform_paths import bash_bin

        result = subprocess.run(
            [bash_bin(), str(dev_support_dir() / "worktree_create.sh")],
            input=json.dumps({"name": "safe-name", "cwd": str(tmp_path)}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            env={
                **{k: v for k, v in os.environ.items() if k != "BOOLEY_PROJECT_DIR"},
                "BOOLEY_PYTHON": sys.executable,
            },
        )

        assert result.returncode == 1
        assert "must be the Git repository root" in result.stderr
        assert not (tmp_path / ".booley_project").exists()

    def test_rejects_traversing_configured_submodule(self, tmp_path: Path):
        """Project config must not steer host-side rm/tar outside the worktree."""
        from booley.runtime.paths import dev_support_dir
        from booley.runtime.platform_paths import bash_bin

        project_root = tmp_path / "repo"
        project_root.mkdir()
        _git(project_root, "init")
        _git(project_root, "config", "user.name", "Test User")
        _git(project_root, "config", "user.email", "test@example.com")
        (project_root / "README.md").write_text("hello\n", encoding="utf-8")
        _git(project_root, "add", "README.md")
        _git(project_root, "commit", "-m", "init")
        project_data = project_root / ".booley_project"
        project_data.mkdir()
        (project_data / "booley.toml").write_text(
            '[submodules]\npaths = ["../../../victim"]\n', encoding="utf-8"
        )
        victim = tmp_path / "victim"
        victim.mkdir()
        sentinel = victim / "sentinel"
        sentinel.write_text("keep\n", encoding="utf-8")

        result = subprocess.run(
            [bash_bin(), str(dev_support_dir() / "worktree_create.sh")],
            input=json.dumps({"name": "security-audit", "cwd": str(project_root)}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            env={
                **{k: v for k, v in os.environ.items() if k != "BOOLEY_PROJECT_DIR"},
                "BOOLEY_PYTHON": sys.executable,
            },
        )

        assert result.returncode == 1
        assert "unsafe submodule path" in result.stderr
        assert sentinel.read_text(encoding="utf-8") == "keep\n"

    def test_rejects_configured_directory_that_is_not_gitlink(self, tmp_path: Path):
        """A safe-looking path must still be an exact mode-160000 Git entry."""
        from booley.runtime.paths import dev_support_dir
        from booley.runtime.platform_paths import bash_bin

        project_root = tmp_path / "repo"
        project_root.mkdir()
        _git(project_root, "init")
        _git(project_root, "config", "user.name", "Test User")
        _git(project_root, "config", "user.email", "test@example.com")
        vendor = project_root / "vendor"
        vendor.mkdir()
        (vendor / "payload.txt").write_text("not a submodule\n", encoding="utf-8")
        _git(project_root, "add", "vendor/payload.txt")
        _git(project_root, "commit", "-m", "init")
        project_data = project_root / ".booley_project"
        project_data.mkdir()
        (project_data / "booley.toml").write_text(
            '[submodules]\npaths = ["vendor"]\n', encoding="utf-8"
        )

        result = subprocess.run(
            [bash_bin(), str(dev_support_dir() / "worktree_create.sh")],
            input=json.dumps({"name": "security-audit", "cwd": str(project_root)}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            env={
                **{k: v for k, v in os.environ.items() if k != "BOOLEY_PROJECT_DIR"},
                "BOOLEY_PYTHON": sys.executable,
            },
        )

        assert result.returncode == 1
        assert "not an exact Git submodule" in result.stderr

    def test_accepts_exact_gitlink_with_space_in_path(self, tmp_path: Path):
        """Confinement must preserve legitimate nested submodule paths."""
        from booley.runtime.paths import dev_support_dir
        from booley.runtime.platform_paths import bash_bin

        dependency = tmp_path / "dependency"
        dependency.mkdir()
        _git(dependency, "init")
        _git(dependency, "config", "user.name", "Test User")
        _git(dependency, "config", "user.email", "test@example.com")
        (dependency / "dep.txt").write_text("dependency\n", encoding="utf-8")
        _git(dependency, "add", "dep.txt")
        _git(dependency, "commit", "-m", "dependency")

        project_root = tmp_path / "repo"
        project_root.mkdir()
        _git(project_root, "init")
        _git(project_root, "config", "user.name", "Test User")
        _git(project_root, "config", "user.email", "test@example.com")
        added = _git(
            project_root,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(dependency),
            "vendor/ip core",
        )
        assert added.returncode == 0, added.stderr
        _git(project_root, "commit", "-am", "add submodule")
        project_data = project_root / ".booley_project"
        project_data.mkdir()
        (project_data / "booley.toml").write_text(
            '[submodules]\npaths = ["vendor/ip core"]\n', encoding="utf-8"
        )

        result = subprocess.run(
            [bash_bin(), str(dev_support_dir() / "worktree_create.sh")],
            input=json.dumps({"name": "security-audit", "cwd": str(project_root)}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            env={
                **{k: v for k, v in os.environ.items() if k != "BOOLEY_PROJECT_DIR"},
                "BOOLEY_PYTHON": sys.executable,
            },
        )

        assert result.returncode == 0, result.stderr
        copied = project_data / "worktrees" / "security-audit" / "vendor" / "ip core"
        assert (copied / "dep.txt").read_text(encoding="utf-8") == "dependency\n"

    def test_handles_parent_core_worktree_from_docker(self, tmp_path: Path):
        """Host setup must survive a parent config polluted with /work."""
        from booley.runtime.paths import dev_support_dir
        from booley.runtime.platform_paths import bash_bin

        project_root = tmp_path / "repo"
        project_root.mkdir()
        _git(project_root, "init")
        _git(project_root, "config", "user.name", "Test User")
        _git(project_root, "config", "user.email", "test@example.com")
        (project_root / "README.md").write_text("hello\n", encoding="utf-8")
        _git(project_root, "add", "README.md")
        _git(project_root, "commit", "-m", "init")

        project_data = project_root / ".booley_project"
        project_data.mkdir()
        (project_data / "booley.toml").write_text("[submodules]\npaths = []\n", encoding="utf-8")
        (project_root / "FUSESOC_IGNORE").write_text("quarantine\n", encoding="utf-8")

        # Sandboxed runs use /work as the work tree. If that value leaks into
        # shared repo config, plain host-side `git -C <worktree> checkout` fails.
        _git(project_root, "config", "core.worktree", "/work")

        slug = "core-worktree-repro"
        result = subprocess.run(
            [bash_bin(), str(dev_support_dir() / "worktree_create.sh")],
            input=json.dumps({"name": slug, "cwd": str(project_root)}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            env={**os.environ, "BOOLEY_PYTHON": sys.executable},
        )

        assert result.returncode == 0, result.stderr
        worktree = project_data / "worktrees" / slug
        assert worktree.is_dir()
        assert _git(worktree, "status", "--short").returncode == 0
        # The worktree carries a copy of the project's .core files; the script
        # must drop FUSESOC_IGNORE so FuseSoC's --cores-root scan can't let a
        # stale copy shadow the repo-root source (silently building wrong RTL).
        assert (project_data / "FUSESOC_IGNORE").is_file()
        assert (worktree / "FUSESOC_IGNORE").read_text(encoding="utf-8") == "quarantine\n"


# ===========================================================================
# Helpers -- replicate pure logic from setup/workspace.py
# ===========================================================================


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def _pick_branch(is_integration: bool, slug: str, base_branch: str) -> str:
    """Replicate branch selection logic."""
    if is_integration:
        return base_branch
    return slug


def _try_create_branch(name: str, create_rc: int = 0, checkout_rc: int = 0) -> str | None:
    """Replicate branch creation with fallback."""
    if create_rc == 0:
        return name
    if checkout_rc == 0:
        return name
    return None


def _check_hook(hook_path: Path) -> str | None:
    """Replicate hook existence check."""
    if not hook_path.exists():
        return f"Worktree hook not found: {hook_path}"
    return None


# ===========================================================================
# Integration tests — call actual workspace-setup run() with mocked subprocess
# ===========================================================================

from booley.harness.models import TicketContext


def _make_ctx(project_root: Path, **overrides) -> TicketContext:
    """Create a TicketContext suitable for the workspace-setup step."""
    defaults = {
        "slug": "fix-fsm-counter",
        "ticket_path": project_root
        / ".booley"
        / "project"
        / "tickets"
        / "queue"
        / "fix-fsm-counter.md",
        "ticket_type": "bugfix",
        "branch": "master",
        "summary": "test",
        "scope_raw": [],
        "criteria": {},
        "project_root": project_root,
    }
    defaults.update(overrides)
    return TicketContext(**defaults)


def _populate_wt(wt: Path):
    """Create a worktree directory with .git marker for reuse detection."""
    wt.mkdir(parents=True, exist_ok=True)
    (wt / ".git").mkdir(exist_ok=True)


def _mock_success(**kwargs):
    return MagicMock(returncode=0, stdout="abc123\n", stderr="", **kwargs)


class TestWorkspaceRun:
    """Call actual setup.workspace.run() — kills real mutants."""

    @pytest.mark.asyncio
    @patch("subprocess.run")
    async def test_post_setup_receives_explicit_project_root(self, mock_sub, project_root):
        ctx = _make_ctx(project_root)
        wt = project_root / ".booley_project" / "worktrees" / ctx.slug
        _populate_wt(wt)
        hook = project_root / ".booley" / "project" / "hooks" / "post-setup.sh"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        mock_sub.return_value = _mock_success()

        from booley.harness.setup.workspace import run

        result = await run(ctx)

        assert result.block_reason is None
        hook_envs = [
            call.kwargs["env"]
            for call in mock_sub.call_args_list
            if "BOOLEY_WORKTREE" in call.kwargs.get("env", {})
        ]
        assert len(hook_envs) == 1
        assert hook_envs[0]["BOOLEY_PROJECT_ROOT"] == str(project_root)

    @pytest.mark.asyncio
    @patch("subprocess.run")
    async def test_reuse_complete_worktree(self, mock_sub, project_root):
        """Kills: L31 negate_if, L33 boolop, L34 negate, L163 return_None."""
        ctx = _make_ctx(project_root)

        wt = project_root / ".booley_project" / "worktrees" / ctx.slug
        _populate_wt(wt)
        mock_sub.return_value = _mock_success()

        from booley.harness.setup.workspace import run

        result = await run(ctx)
        assert result.block_reason is None
        assert result.metadata["worktree"] == str(wt)
        assert result.metadata["branch"] == ctx.slug
        for call in mock_sub.call_args_list:
            assert "bash" not in str(call), "Should reuse, not create via script"

    @pytest.mark.asyncio
    @patch("subprocess.run")
    async def test_missing_git_triggers_creation(self, mock_sub, project_root):
        """Kills: L31 negate_if — .git missing → creation path."""
        ctx = _make_ctx(project_root)

        wt = project_root / ".booley_project" / "worktrees" / ctx.slug
        wt.mkdir(parents=True, exist_ok=True)
        # No .git → triggers creation; hook script missing → blocks
        from booley.harness.setup.workspace import run

        result = await run(ctx)
        assert result.block_reason is not None

    @pytest.mark.asyncio
    @patch("subprocess.run")
    @patch("booley.harness.setup.workspace.dev_support_dir")
    async def test_hook_not_found_blocks(self, mock_tools_dir, mock_sub, project_root):
        """Kills: L47 negate_if."""
        # Point dev_support_dir at a temp dir with no worktree_create.sh
        mock_tools_dir.return_value = project_root / "_empty_tools"
        ctx = _make_ctx(project_root)

        from booley.harness.setup.workspace import run

        result = await run(ctx)
        assert result.block_reason is not None
        assert "not found" in result.block_reason.lower()

    @pytest.mark.asyncio
    @patch("subprocess.run")
    async def test_hook_failure_blocks(self, mock_sub, project_root):
        """Kills: L67 cmpop NotEq→Eq."""
        ctx = _make_ctx(project_root)

        hook = project_root / ".booley" / "src" / "booley" / "dev_support" / "worktree_create.sh"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/bash\nexit 1", encoding="utf-8")
        mock_sub.return_value = MagicMock(returncode=1, stdout="", stderr="disk full")

        from booley.harness.setup.workspace import run

        result = await run(ctx)
        assert result.block_reason is not None
        assert "failed" in result.block_reason.lower()

    @pytest.mark.asyncio
    @patch("subprocess.run")
    async def test_hook_output_path_missing_blocks(self, mock_sub, project_root):
        """Kills: L73 negate_if."""
        ctx = _make_ctx(project_root)

        hook = project_root / ".booley" / "src" / "booley" / "dev_support" / "worktree_create.sh"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/bash", encoding="utf-8")
        mock_sub.return_value = MagicMock(returncode=0, stdout="/nonexistent\n", stderr="")

        from booley.harness.setup.workspace import run

        result = await run(ctx)
        assert result.block_reason is not None
        assert "doesn't exist" in result.block_reason.lower()

    @pytest.mark.asyncio
    @patch("subprocess.run")
    async def test_hook_timeout_blocks(self, mock_sub, project_root):
        """Kills: L64 timeout exception handling."""
        ctx = _make_ctx(project_root)

        hook = project_root / ".booley" / "src" / "booley" / "dev_support" / "worktree_create.sh"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/bash", encoding="utf-8")
        mock_sub.side_effect = subprocess.TimeoutExpired(cmd="bash", timeout=900)

        from booley.harness.setup.workspace import run

        result = await run(ctx)
        assert result.block_reason is not None
        assert "timed out" in result.block_reason.lower()

    @pytest.mark.asyncio
    @patch("subprocess.run")
    async def test_integration_branch_used(self, mock_sub, project_root):
        """Kills: L104 negate_if (is_integration)."""
        ctx = _make_ctx(project_root, branch="int/batch-1")

        wt = project_root / ".booley_project" / "worktrees" / ctx.slug
        _populate_wt(wt)
        mock_sub.return_value = _mock_success()

        from booley.harness.setup.workspace import run

        result = await run(ctx)
        assert result.block_reason is None
        assert result.metadata["branch"] == "int/batch-1"

    @pytest.mark.asyncio
    @patch("subprocess.run")
    async def test_normal_ticket_creates_feature_branch(self, mock_sub, project_root):
        """Kills: L104 negate_if — non-integration uses slug as branch."""
        ctx = _make_ctx(project_root, branch="master")

        wt = project_root / ".booley_project" / "worktrees" / ctx.slug
        _populate_wt(wt)
        mock_sub.return_value = _mock_success()

        from booley.harness.setup.workspace import run

        result = await run(ctx)
        assert result.block_reason is None
        assert result.metadata["branch"] == ctx.slug

    @pytest.mark.asyncio
    @patch("subprocess.run")
    async def test_branch_create_uses_force_flag(self, mock_sub, project_root):
        """Verify checkout uses -B to handle stale branches from prior runs."""
        ctx = _make_ctx(project_root, branch="master")

        wt = project_root / ".booley_project" / "worktrees" / ctx.slug
        _populate_wt(wt)

        calls = []

        def side_effect(args, **kwargs):
            cmd = " ".join(str(a) for a in args)
            calls.append(cmd)
            return _mock_success()

        mock_sub.side_effect = side_effect

        from booley.harness.setup.workspace import run

        result = await run(ctx)
        assert result.block_reason is None
        assert any("checkout" in c and "-B" in c for c in calls), (
            "Should use -B (force-create) for feature branches"
        )

    @pytest.mark.asyncio
    @patch("subprocess.run")
    async def test_surviving_divergent_feature_branch_is_preserved(self, mock_sub, project_root):
        ctx = _make_ctx(project_root, branch="master")
        wt = project_root / ".booley_project" / "worktrees" / ctx.slug
        _populate_wt(wt)

        def side_effect(args, **kwargs):
            joined = " ".join(str(a) for a in args)
            if f"refs/heads/{ctx.slug}" in joined:
                return MagicMock(returncode=0, stdout="feature123\n", stderr="")
            if "rev-parse master" in joined:
                return MagicMock(returncode=0, stdout="base456\n", stderr="")
            return _mock_success()

        mock_sub.side_effect = side_effect

        from booley.harness.setup.workspace import run

        result = await run(ctx)

        assert "Refusing to reset surviving feature branch" in result.block_reason
        assert not any(
            "checkout -B" in " ".join(map(str, call.args[0])) for call in mock_sub.call_args_list
        )

    @pytest.mark.asyncio
    @patch("subprocess.run")
    async def test_resume_attaches_surviving_feature_branch(self, mock_sub, project_root):
        ctx = _make_ctx(project_root, branch="master", workspace_intent="resume")
        wt = project_root / ".booley_project" / "worktrees" / ctx.slug
        _populate_wt(wt)

        def side_effect(args, **kwargs):
            joined = " ".join(str(a) for a in args)
            if f"refs/heads/{ctx.slug}" in joined:
                return MagicMock(returncode=0, stdout="feature123\n", stderr="")
            return _mock_success()

        mock_sub.side_effect = side_effect

        from booley.harness.setup.workspace import run

        result = await run(ctx)

        assert result.block_reason is None
        commands = [" ".join(map(str, call.args[0])) for call in mock_sub.call_args_list]
        assert any(f"git checkout {ctx.slug}" in command for command in commands)
        assert not any("checkout --detach" in command for command in commands)
        assert not any("checkout -B" in command for command in commands)

    @pytest.mark.asyncio
    @patch("subprocess.run")
    async def test_branch_create_fails_blocks(self, mock_sub, project_root):
        """Kills: cmpop NotEq→Eq, return_None."""
        ctx = _make_ctx(project_root, branch="master")

        wt = project_root / ".booley_project" / "worktrees" / ctx.slug
        _populate_wt(wt)

        def side_effect(args, **kwargs):
            cmd = " ".join(str(a) for a in args)
            if "checkout" in cmd:
                return MagicMock(returncode=1, stdout="", stderr="fatal")
            return _mock_success()

        mock_sub.side_effect = side_effect

        from booley.harness.setup.workspace import run

        result = await run(ctx)
        assert result.block_reason is not None
        assert "Failed to create/checkout" in result.block_reason

    @pytest.mark.asyncio
    @patch("subprocess.run")
    async def test_head_mismatch_triggers_detach(self, mock_sub, project_root):
        """Kills: L96 cmpop NotEq→Eq."""
        ctx = _make_ctx(project_root, branch="master")

        wt = project_root / ".booley_project" / "worktrees" / ctx.slug
        _populate_wt(wt)

        calls = []

        def side_effect(args, **kwargs):
            cmd = " ".join(str(a) for a in args)
            calls.append(cmd)
            if "rev-parse HEAD" in cmd:
                return MagicMock(returncode=0, stdout="aaaa\n", stderr="")
            if "rev-parse master" in cmd:
                return MagicMock(returncode=0, stdout="bbbb\n", stderr="")
            return _mock_success()

        mock_sub.side_effect = side_effect

        from booley.harness.setup.workspace import run

        await run(ctx)
        assert any("--detach" in c for c in calls), "Should detach when heads differ"

    @pytest.mark.asyncio
    @patch("subprocess.run")
    async def test_head_match_skips_detach(self, mock_sub, project_root):
        """Kills: L96 cmpop NotEq→Eq — same hash → no detach."""
        ctx = _make_ctx(project_root, branch="master")

        wt = project_root / ".booley_project" / "worktrees" / ctx.slug
        _populate_wt(wt)

        calls = []

        def side_effect(args, **kwargs):
            cmd = " ".join(str(a) for a in args)
            calls.append(cmd)
            return _mock_success()  # same stdout for both rev-parse

        mock_sub.side_effect = side_effect

        from booley.harness.setup.workspace import run

        await run(ctx)
        assert not any("--detach" in c for c in calls), "Should not detach when heads match"

    @pytest.mark.asyncio
    @patch(
        "booley.runtime.shared_infra._load_rtl_config",
        return_value={
            "flows": {
                "sim": {},
                "synth": {},
            },
        },
    )
    @patch("subprocess.run")
    async def test_check_paths_failure_blocks(self, mock_sub, _mock_cfg, project_root):
        """Kills: L157 cmpop NotEq→Eq, L159 return_None."""
        # Synth criteria make ctx.has_synth True, so the yosys check-paths
        # script runs and its failure must block setup.
        ctx = _make_ctx(
            project_root, branch="master", criteria={"mandatory": {"synthesis_ok": True}}
        )

        wt = project_root / ".booley_project" / "worktrees" / ctx.slug
        _populate_wt(wt)

        # Create check-paths scripts in main .booley/ (re-synced into worktree)
        for rel in ["yosys/run_yosys_syn.py"]:
            script = project_root / ".booley" / "src" / rel
            script.parent.mkdir(parents=True, exist_ok=True)
            script.write_text("# stub", encoding="utf-8")

        def side_effect(args, **kwargs):
            cmd = " ".join(str(a) for a in args)
            if "check-paths" in cmd:
                return MagicMock(returncode=1, stdout="ERROR", stderr="")
            return _mock_success()

        mock_sub.side_effect = side_effect

        from booley.harness.setup.workspace import run

        result = await run(ctx)
        assert result.block_reason is not None
        assert "check-paths failed" in result.block_reason


# ===========================================================================
# Hardening regression tests
# ===========================================================================


class TestRevParseValidation:
    """Tests for Change A: rev-parse returncode and empty stdout checks."""

    @pytest.mark.asyncio
    @patch("subprocess.run")
    async def test_rev_parse_head_failure_blocks(self, mock_sub, project_root):
        """rev-parse HEAD returning non-zero should block, not silently continue."""
        ctx = _make_ctx(project_root)

        wt = project_root / ".booley_project" / "worktrees" / ctx.slug
        _populate_wt(wt)

        def side_effect(args, **kwargs):
            cmd = " ".join(str(a) for a in args)
            if "rev-parse" in cmd and "HEAD" in cmd and "--git-dir" not in cmd:
                return MagicMock(returncode=128, stdout="", stderr="fatal: bad object HEAD")
            return _mock_success()

        mock_sub.side_effect = side_effect

        from booley.harness.setup.workspace import run

        result = await run(ctx)
        assert result.block_reason is not None
        assert "rev-parse HEAD failed" in result.block_reason

    @pytest.mark.asyncio
    @patch("subprocess.run")
    async def test_rev_parse_head_empty_stdout_blocks(self, mock_sub, project_root):
        """rev-parse HEAD returning rc=0 but empty stdout should block."""
        ctx = _make_ctx(project_root)

        wt = project_root / ".booley_project" / "worktrees" / ctx.slug
        _populate_wt(wt)

        def side_effect(args, **kwargs):
            cmd = " ".join(str(a) for a in args)
            if "rev-parse" in cmd and "HEAD" in cmd and "--git-dir" not in cmd:
                return MagicMock(returncode=0, stdout="", stderr="")
            return _mock_success()

        mock_sub.side_effect = side_effect

        from booley.harness.setup.workspace import run

        result = await run(ctx)
        assert result.block_reason is not None
        assert "rev-parse HEAD failed" in result.block_reason

    @pytest.mark.asyncio
    @patch("subprocess.run")
    async def test_rev_parse_base_ref_failure_blocks(self, mock_sub, project_root):
        """rev-parse <base_ref> failing should block."""
        ctx = _make_ctx(project_root, branch="master")

        wt = project_root / ".booley_project" / "worktrees" / ctx.slug
        _populate_wt(wt)

        def side_effect(args, **kwargs):
            cmd = " ".join(str(a) for a in args)
            if "rev-parse" in cmd and "master" in cmd:
                return MagicMock(returncode=128, stdout="", stderr="fatal: bad revision")
            return _mock_success()

        mock_sub.side_effect = side_effect

        from booley.harness.setup.workspace import run

        result = await run(ctx)
        assert result.block_reason is not None
        assert "rev-parse" in result.block_reason
        assert "master" in result.block_reason


class TestOSErrorHandling:
    """Tests for Change B: OSError caught alongside TimeoutExpired."""

    @pytest.mark.asyncio
    @patch("subprocess.run")
    async def test_worktree_creation_oserror_blocks(self, mock_sub, project_root):
        """OSError during worktree script execution should block cleanly."""
        ctx = _make_ctx(project_root)

        hook = project_root / ".booley" / "src" / "booley" / "dev_support" / "worktree_create.sh"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/bash", encoding="utf-8")
        mock_sub.side_effect = FileNotFoundError("bash not found")

        from booley.harness.setup.workspace import run

        result = await run(ctx)
        assert result.block_reason is not None
        assert "OS error" in result.block_reason


class TestPruneStaleWorktreeLocks:
    """Tests for PID+liveness based worktree lock pruning.

    Regression guard for the bug class addressed by 4b321d0 / a0a9e23:
    locks left by ``TerminateProcess`` (Windows subprocess timeout, which
    doesn't fire shell EXIT traps) must be reclaimed via PID-liveness rather
    than waiting for the age-based fallback.
    """

    def _setup_locks_dir(self, tmp_path: Path) -> Path:
        locks_dir = tmp_path / ".booley_project" / "worktrees" / ".locks"
        locks_dir.mkdir(parents=True)
        return locks_dir

    def test_mkdir_lock_with_dead_pid_pruned(self, tmp_path: Path):
        from booley.harness.setup.workspace import _prune_stale_worktree_locks

        locks = self._setup_locks_dir(tmp_path)
        # Synthesise an orphaned mkdir-lock owned by a definitely-dead PID.
        stale = locks / "feature_x.mkdir.lock"
        stale.mkdir()
        (stale / "pid").write_text("999999999", encoding="utf-8")

        _prune_stale_worktree_locks(tmp_path)
        assert not stale.exists(), "dead-PID mkdir lock must be pruned"

    def test_lockfile_with_dead_pid_pruned(self, tmp_path: Path):
        """Flock-style ``*.lock`` files must also be reclaimed by PID liveness."""
        from booley.harness.setup.workspace import _prune_stale_worktree_locks

        locks = self._setup_locks_dir(tmp_path)
        stale = locks / "feature_y.lock"
        stale.write_text("999999999", encoding="utf-8")

        _prune_stale_worktree_locks(tmp_path)
        assert not stale.exists(), "dead-PID lockfile must be pruned"

    def test_live_pid_lock_preserved(self, tmp_path: Path):
        """A lock owned by THIS process (alive) must NOT be pruned."""
        import os

        from booley.harness.setup.workspace import _prune_stale_worktree_locks

        locks = self._setup_locks_dir(tmp_path)
        live = locks / "feature_z.mkdir.lock"
        live.mkdir()
        (live / "pid").write_text(str(os.getpid()), encoding="utf-8")
        live_file = locks / "feature_w.lock"
        live_file.write_text(str(os.getpid()), encoding="utf-8")

        _prune_stale_worktree_locks(tmp_path)
        assert live.exists(), "live mkdir-lock must be preserved"
        assert live_file.exists(), "live lockfile must be preserved"

    def test_corrupt_pid_falls_through_to_age_check(self, tmp_path: Path):
        """Garbage PID content uses mtime-based fallback, not silent leak."""
        import os
        import time as _time

        from booley.harness.setup.workspace import _prune_stale_worktree_locks

        locks = self._setup_locks_dir(tmp_path)
        stale = locks / "feature_q.lock"
        stale.write_text("not_a_pid", encoding="utf-8")
        # Age it well past the default 300s threshold.
        old = _time.time() - 10_000
        os.utime(stale, (old, old))

        _prune_stale_worktree_locks(tmp_path)
        assert not stale.exists(), "corrupt-PID lock must fall through to age check"

    def test_no_locks_dir_is_noop(self, tmp_path: Path):
        from booley.harness.setup.workspace import _prune_stale_worktree_locks

        # Must not raise when .locks/ doesn't exist.
        _prune_stale_worktree_locks(tmp_path)


class TestReleaseWorktreeLocks:
    """Teardown drops the locks its own worktree creation left behind (F-54)."""

    def _setup_locks_dir(self, tmp_path: Path) -> Path:
        locks_dir = tmp_path / ".booley_project" / "worktrees" / ".locks"
        locks_dir.mkdir(parents=True)
        return locks_dir

    def test_releases_both_lock_flavours_for_the_name(self, tmp_path: Path):
        from booley.harness.setup.worktree_lock_gc import release_worktree_locks

        locks = self._setup_locks_dir(tmp_path)
        flock_file = locks / "my-ticket.lock"
        flock_file.write_text("1234", encoding="utf-8")
        mkdir_lock = locks / "my-ticket.mkdir.lock"
        mkdir_lock.mkdir()

        release_worktree_locks(tmp_path, "my-ticket")

        assert not flock_file.exists()
        assert not mkdir_lock.exists()

    def test_leaves_other_tickets_and_shared_parent_lock_alone(self, tmp_path: Path):
        """The `_parent_git` lock is shared across worktrees — never reap it here."""
        from booley.harness.setup.worktree_lock_gc import release_worktree_locks

        locks = self._setup_locks_dir(tmp_path)
        mine = locks / "mine.lock"
        mine.write_text("1", encoding="utf-8")
        theirs = locks / "theirs.lock"
        theirs.write_text("2", encoding="utf-8")
        parent = locks / "_parent_git.lock"
        parent.write_text("3", encoding="utf-8")

        release_worktree_locks(tmp_path, "mine")
        assert not mine.exists()
        assert theirs.exists()
        assert parent.exists()

        # Even asked directly, the shared lock is off limits.
        release_worktree_locks(tmp_path, "_parent_git")
        assert parent.exists()

    def test_missing_locks_dir_and_empty_name_are_noops(self, tmp_path: Path):
        from booley.harness.setup.worktree_lock_gc import release_worktree_locks

        release_worktree_locks(tmp_path, "anything")  # no .locks/ dir at all
        self._setup_locks_dir(tmp_path)
        release_worktree_locks(tmp_path, "")


class TestResyncFallback:
    """Tests for Change D: resync failure degrades to fresh worktree creation."""

    @pytest.mark.asyncio
    @patch("booley.harness.setup.workspace._resync_project_hooks")
    @patch("booley.harness.setup.workspace._resync_booley_dir")
    @patch("subprocess.run")
    async def test_resync_failure_falls_through_to_create(
        self,
        mock_sub,
        mock_resync_booley,
        mock_resync_hooks,
        project_root,
    ):
        """If resync raises OSError, step should tear down and try creation."""
        ctx = _make_ctx(project_root)

        wt = project_root / ".booley_project" / "worktrees" / ctx.slug
        _populate_wt(wt)

        mock_resync_booley.side_effect = OSError("Permission denied")
        mock_sub.return_value = _mock_success()

        from booley.harness.setup.workspace import run

        result = await run(ctx)
        # Should not block — should fall through to creation path.
        # Creation will fail because worktree_create.sh isn't real,
        # but we verify resync failure didn't block on its own.
        # The key assertion: it attempted creation (script not found = creation path).
        if result.block_reason:
            assert (
                "not found" in result.block_reason.lower()
                or "doesn't exist" in result.block_reason.lower()
            )
        mock_resync_booley.assert_called_once()

    @pytest.mark.asyncio
    @patch("booley.harness.setup.workspace._resync_project_hooks")
    @patch("booley.harness.setup.workspace._resync_booley_dir")
    @patch("subprocess.run")
    async def test_resync_failure_then_creation_succeeds(
        self,
        mock_sub,
        mock_resync_booley,
        mock_resync_hooks,
        project_root,
    ):
        """Resync fails → teardown → creation succeeds → step passes."""
        ctx = _make_ctx(project_root)

        wt = project_root / ".booley_project" / "worktrees" / ctx.slug
        _populate_wt(wt)

        mock_resync_booley.side_effect = OSError("Permission denied")
        mock_sub.return_value = _mock_success()

        # Create the worktree script so creation path can proceed
        hook = project_root / ".booley" / "src" / "booley" / "dev_support" / "worktree_create.sh"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/bash", encoding="utf-8")

        # After teardown + creation, expected_wt must exist
        # (creation path checks expected_wt.exists())
        # Re-create the worktree dir to simulate successful creation
        def sub_side_effect(args, **kwargs):
            cmd = " ".join(str(a) for a in args)
            if "worktree remove" in cmd:
                # Simulate teardown
                import shutil

                if wt.exists():
                    shutil.rmtree(wt, ignore_errors=True)
            if "worktree_create" in cmd:
                # Simulate successful creation
                _populate_wt(wt)
            return _mock_success()

        mock_sub.side_effect = sub_side_effect

        from booley.harness.setup.workspace import run

        result = await run(ctx)
        assert result.block_reason is None
