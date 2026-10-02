"""Golden re-run coverage for ``booley init`` (the clobber-guard contract).

Runs the FULL init flow twice over a scratch git repo, hand-editing every
scaffolded file in between, and asserts the ownership contract file by file:

- user-owned skeletons (booley.toml, tests.toml, ticket_creation.md, FUSESOC_IGNORE,
  .booley_project/.gitignore) keep their hand edits, and
- fully-managed files (the Project Git-hook bundle, .git/hooks adapters,
  devcontainer.json) come back byte-identical to the first run — hand edits
  are deliberately regenerated away.

Docker/vscode/systemctl are absent (shutil.which -> None). Image reconciliation
is stubbed as current so this clobber-contract test can exercise the remaining
steps without subprocesses.
The docker-file preservation path (SETUP-6, 8c6a01c) is exercised separately
with a stubbed image build.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from booley.harness import init_cmd
from booley.harness.image_lifecycle import LifecycleResult
from booley.harness.image_lifecycle import Status as ImageLifecycleStatus
from booley.harness.setup import interactive as interactive_init
from booley.harness.setup.common import InitContext
from booley.runtime import project_image as pi
from booley.runtime import session_issuance as runtime_spec
from booley.runtime import session_runtime as sr
from booley.runtime.project_dir import reset_cache

HAND_EDIT = "# HAND EDIT — must survive re-init\n"


def _init_args() -> argparse.Namespace:
    return argparse.Namespace(
        seed=False,
        check_only=False,
        force=False,
        verbose=False,
        provider="claude",
        auth="subscription",
    )


def _run_full_init(root: Path) -> int:
    reset_cache()
    return init_cmd.run_init(_init_args(), root)


def _filesystem_snapshot(root: Path) -> dict[Path, tuple[bytes, int]]:
    return {
        path.relative_to(root.parent): (path.read_bytes(), path.stat().st_mode)
        for path in root.parent.rglob("*")
        if path.is_file()
    }


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    """A scratch git repo with isolated HOME and no docker/vscode/systemctl."""
    root = tmp_path / "proj"
    root.mkdir()
    subprocess.run(["git", "init", str(root)], capture_output=True, check=True)

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)

    trusted_booley_path = home / ".local" / "bin" / "booley"
    trusted_booley_path.parent.mkdir(parents=True)
    trusted_booley_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    trusted_booley_path.chmod(0o755)
    trusted_booley = str(trusted_booley_path)
    monkeypatch.setattr(
        init_cmd.shutil,
        "which",
        lambda name: trusted_booley if name == "booley" else None,
    )

    monkeypatch.setattr(
        init_cmd,
        "reconcile_bootstrap",
        lambda intent, **_kwargs: init_cmd.BootstrapResult(intent, ()),
    )
    current_image = LifecycleResult(
        selected_reference="booley-sandbox",
        selected_id="sha256:test-image",
        status=ImageLifecycleStatus.CURRENT,
    )
    monkeypatch.setattr(
        init_cmd.image_lifecycle,
        "reconcile_planned",
        lambda *_args, **_kwargs: current_image,
    )
    monkeypatch.setattr(runtime_spec, "_resolve_image_id", lambda _image: "sha256:test-image")
    monkeypatch.setattr(
        "booley.runtime.interactive_docker.image_id_strict",
        lambda _image: None,
    )
    monkeypatch.setattr(init_cmd, "_select_interactive_app", lambda *_: "none")
    monkeypatch.setattr(
        interactive_init.session_runtime,
        "plan_stopped_headless_runtime_reconciliation",
        lambda *_args: type("Plan", (), {"pending": False})(),
    )
    monkeypatch.setattr(
        interactive_init.session_runtime,
        "reconcile_stopped_headless_runtime",
        lambda *_args: False,
    )
    pdk_root = tmp_path / "pdk"
    pdk_root.mkdir()
    monkeypatch.setattr(init_cmd.nangate_pdk, "cache_root", lambda: pdk_root)
    return root


class TestFullInitRerun:
    def test_check_only_after_full_init_is_current_and_mutation_free(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        current_image = LifecycleResult(
            selected_reference="booley-sandbox",
            selected_id="sha256:test-image",
            status=ImageLifecycleStatus.CURRENT,
        )

        def current_image_step(ctx, _bootstrap=None):
            ctx.record("docker_image", "skip", "current")
            return current_image

        monkeypatch.setattr(init_cmd, "_reconcile_initialized_image", current_image_step)
        monkeypatch.setattr(
            init_cmd,
            "_step_auth",
            lambda ctx, *_args, **_kwargs: ctx.record("auth", "skip", "current"),
        )
        assert _run_full_init(repo) == 0
        capsys.readouterr()
        before = _filesystem_snapshot(repo)
        monkeypatch.setattr(
            runtime_spec,
            "issue_prepared",
            lambda *_args, **_kwargs: pytest.fail("check-only issued Runtime state"),
        )
        monkeypatch.setattr(
            interactive_init.session_runtime,
            "reconcile_stopped_headless_runtime",
            lambda *_args: pytest.fail("check-only reconciled Runtime state"),
        )
        args = _init_args()
        args.check_only = True

        assert init_cmd.run_init(args, repo) == 0

        output = capsys.readouterr().out
        assert "project_git_hooks — current" in output
        assert "interactive — current" in output
        assert _filesystem_snapshot(repo) == before

    def test_reconciles_stopped_headless_runtime_after_issuing(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        with (
            patch.object(
                sr,
                "reconcile_stopped_headless_runtime",
                return_value=True,
                create=True,
            ) as reconcile,
            patch.object(
                sr,
                "plan_stopped_headless_runtime_reconciliation",
                return_value=type("Plan", (), {"pending": True})(),
            ),
        ):
            _run_full_init(repo)

        reconcile.assert_called_once()
        assert (
            "reconciled stopped Sandbox resources from their prior issuance"
            in capsys.readouterr().out
        )

    def test_runtime_reconciliation_failure_makes_init_incomplete(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _run_full_init(repo)
        capsys.readouterr()
        ctx = InitContext(project_root=repo)
        with (
            patch.object(
                sr,
                "reconcile_stopped_headless_runtime",
                side_effect=sr.SessionError("container became active"),
            ),
            patch.object(
                sr,
                "plan_stopped_headless_runtime_reconciliation",
                return_value=type("Plan", (), {"pending": True})(),
            ),
        ):
            init_cmd._step_interactive(
                ctx,
                nangate_pdk_root=repo.parent / "pdk",
                agent_app="none",
            )
        ctx.record("advisories", "ok", "configured")

        assert init_cmd._print_summary(ctx) == 2
        output = capsys.readouterr().out
        assert "could not reconcile stopped Sandbox" in output
        assert "this project is ready" not in output

    def test_foreign_root_guidance_blocks_before_any_filesystem_mutation(self, repo: Path):
        project_dir = repo / ".booley_project"
        project_dir.mkdir()
        canon = project_dir / "AGENTS.md"
        canon.write_text("# canonical\n", encoding="utf-8")
        foreign = repo / "AGENTS.md"
        foreign.write_text("# user-owned\n", encoding="utf-8")
        before = {
            path.relative_to(repo).as_posix(): path.read_bytes()
            for path in repo.rglob("*")
            if path.is_file() and ".git" not in path.parts
        }

        assert _run_full_init(repo) == 2

        after = {
            path.relative_to(repo).as_posix(): path.read_bytes()
            for path in repo.rglob("*")
            if path.is_file() and ".git" not in path.parts
        }
        assert after == before
        assert not (repo / "CLAUDE.md").exists()

    def test_second_run_preserves_user_edits_and_regenerates_managed_files(self, repo: Path):
        rc1 = _run_full_init(repo)
        assert rc1 in (0, 2)  # 2: docker/vscode absent -> dependency-detection step errs

        pdir = repo / ".booley_project"
        hooks_dir = repo / ".git" / "hooks"

        # --- user-owned scaffolding: init must never touch these again -----
        # NB: no adapters/ here — init scaffolds no adapter stubs (ADR 0039
        # dropped the project-native path; every Booley Flow uses built-in orchestration).
        user_owned = [
            pdir / "booley.toml",
            pdir / "tests.toml",
            pdir / "ticket_creation.md",
            pdir / "FUSESOC_IGNORE",
        ]
        for f in user_owned:
            assert f.is_file(), f"expected scaffolded file missing after init: {f}"
            f.write_text(f.read_text(encoding="utf-8") + HAND_EDIT, encoding="utf-8")

        gitignore = pdir / ".gitignore"
        gitignore.write_text(
            gitignore.read_text(encoding="utf-8") + "my_custom_dir/\n",
            encoding="utf-8",
        )

        # --- fully managed: regenerated byte-identical on every run --------
        managed = [
            pdir / ".managed" / "project-git-hooks.pyz",
            hooks_dir / "commit-msg",
            hooks_dir / "pre-push",
            repo / ".devcontainer" / "devcontainer.json",
        ]
        baseline = {}
        for f in managed:
            assert f.is_file(), f"expected managed file missing after init: {f}"
            baseline[f] = f.read_bytes()
            if f.suffix == ".pyz":
                f.write_bytes(f.read_bytes() + b"vandalism\n")
            else:
                f.write_text(
                    f.read_text(encoding="utf-8") + "# vandalism\n",
                    encoding="utf-8",
                    newline="\n",
                )

        rc2 = _run_full_init(repo)
        assert rc2 in (0, 2)

        for f in user_owned:
            assert f.read_text(encoding="utf-8").endswith(HAND_EDIT), (
                f"re-running init clobbered user-owned file {f}"
            )
        assert "my_custom_dir/" in gitignore.read_text(encoding="utf-8"), (
            "re-running init dropped a user line from .booley_project/.gitignore"
        )
        for f, expected in baseline.items():
            assert f.read_bytes() == expected, (
                f"managed file {f} not byte-stable across init re-runs"
            )

    def test_missing_ticket_creation_is_backfilled_without_touching_other_config(self, repo: Path):
        assert _run_full_init(repo) in (0, 2)
        pdir = repo / ".booley_project"
        guidance = pdir / "ticket_creation.md"
        guidance.unlink()
        booley_before = (pdir / "booley.toml").read_bytes()
        tests_before = (pdir / "tests.toml").read_bytes()

        assert _run_full_init(repo) in (0, 2)

        assert guidance.read_text(encoding="utf-8").startswith("# Ticket Creation Guidance")
        assert (pdir / "booley.toml").read_bytes() == booley_before
        assert (pdir / "tests.toml").read_bytes() == tests_before

    def test_check_only_reports_ticket_creation_backfill_without_writing(self, repo: Path, capsys):
        assert _run_full_init(repo) in (0, 2)
        pdir = repo / ".booley_project"
        guidance = pdir / "ticket_creation.md"
        guidance.unlink()

        ctx = InitContext(project_root=repo, check_only=True)
        init_cmd._backfill_config_skeletons(pdir, ctx)

        assert not guidance.exists()
        assert "would add 1 config skeleton file" in capsys.readouterr().out

    def test_legacy_ticket_defaults_suppresses_new_guidance_scaffold(self, repo: Path):
        assert _run_full_init(repo) in (0, 2)
        pdir = repo / ".booley_project"
        guidance = pdir / "ticket_creation.md"
        guidance.unlink()
        legacy = pdir / "ticket_defaults.md"
        legacy_text = "# Existing project guidance\n\nAlways run the full regression.\n"
        legacy.write_text(legacy_text, encoding="utf-8")

        assert _run_full_init(repo) in (0, 2)

        assert not guidance.exists()
        assert legacy.read_text(encoding="utf-8") == legacy_text

    def test_step_numbers_are_contiguous_end_to_end(self, repo: Path, capsys):
        """F-2: the emitted sequence used to read 1, 2, 3, 5, 8, 9, 9b, 10,
        10b ... 12, and a first-time user has no way to tell a retired number
        apart from a step that silently failed."""
        assert _run_full_init(repo) in (0, 2)

        numbers = re.findall(r"=== Step (\S+) —", capsys.readouterr().out)

        assert numbers, "init emitted no step banners"
        assert numbers == [str(n) for n in range(1, len(numbers) + 1)]

    def test_foreign_git_hook_backed_up_not_clobbered(self, repo: Path):
        hooks_dir = repo / ".git" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        foreign = "#!/bin/sh\necho my precious pre-existing hook\n"
        (hooks_dir / "commit-msg").write_text(foreign, encoding="utf-8", newline="\n")

        assert _run_full_init(repo) in (0, 2)

        backup = hooks_dir / "commit-msg.pre-booley"
        assert backup.read_text(encoding="utf-8") == foreign, (
            "pre-existing non-Booley hook was not backed up"
        )
        assert "project-git-hooks.pyz" in (hooks_dir / "commit-msg").read_text(encoding="utf-8")

    def test_gitignore_selfheal_restores_deleted_booley_pattern(self, repo: Path):
        assert _run_full_init(repo) in (0, 2)
        gitignore = repo / ".booley_project" / ".gitignore"
        lines = gitignore.read_text(encoding="utf-8").splitlines()
        assert ".runtime/" in [ln.strip() for ln in lines]

        # User deletes a load-bearing pattern; re-init appends it back while
        # keeping the rest of the user's file intact.
        kept = [ln for ln in lines if ln.strip() != ".runtime/"]
        gitignore.write_text("\n".join(["# user comment", *kept]) + "\n", encoding="utf-8")

        assert _run_full_init(repo) in (0, 2)
        healed = gitignore.read_text(encoding="utf-8")
        assert ".runtime/" in {ln.strip() for ln in healed.splitlines()}
        assert "# user comment" in healed


class TestCuratedOverrideAdvisory:
    """F-13: a project pin that re-versions a package the base image curated
    (cocotb 2.1.0 -> 1.5.1) was baked with zero output, and the base image's
    cocotb/VPI validation layer never re-runs on the project layer."""

    def test_names_each_overridden_package(self, monkeypatch, capsys):
        monkeypatch.setattr(pi, "base_image_packages", lambda *a, **k: {"cocotb": "2.1.0"})

        init_cmd._report_curated_overrides(["cocotb==1.5.1", "numpy==2.0"])

        out = capsys.readouterr().out
        assert "cocotb==1.5.1" in out and "cocotb==2.1.0" in out
        assert "numpy" not in out

    def test_silent_when_the_base_image_cannot_be_queried(self, monkeypatch, capsys):
        monkeypatch.setattr(pi, "base_image_packages", lambda *a, **k: {})

        init_cmd._report_curated_overrides(["cocotb==1.5.1"])

        assert capsys.readouterr().out == ""


class TestProjectGitignoreBackfill:
    """Project-authored Python hooks may still produce ignored bytecode."""

    def test_new_gitignore_covers_transient_reports_and_python_bytecode(self, tmp_path: Path):
        init_cmd._backfill_project_gitignore(tmp_path, InitContext(project_root=tmp_path))

        lines = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
        assert "flow-reports/" in lines
        assert "/logs/" in lines
        assert "/.baseline-wt-*/" in lines
        assert "__pycache__/" in lines
        assert "*.pyc" in lines

    def test_existing_gitignore_is_backfilled_without_losing_user_lines(self, tmp_path: Path):
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("tmp/\nmy_custom_dir/\n", encoding="utf-8")

        init_cmd._backfill_project_gitignore(tmp_path, InitContext(project_root=tmp_path))

        lines = gitignore.read_text(encoding="utf-8").splitlines()
        assert "my_custom_dir/" in lines
        assert lines.count("flow-reports/") == 1
        assert lines.count("/logs/") == 1
        assert lines.count("/.baseline-wt-*/") == 1
        assert "__pycache__/" in lines
        assert lines.count("tmp/") == 1  # already present -> not re-added

    def test_backfill_is_idempotent(self, tmp_path: Path):
        ctx = InitContext(project_root=tmp_path)
        init_cmd._backfill_project_gitignore(tmp_path, ctx)
        first = (tmp_path / ".gitignore").read_bytes()

        init_cmd._backfill_project_gitignore(tmp_path, ctx)

        assert (tmp_path / ".gitignore").read_bytes() == first

    def test_generated_patterns_are_honored_by_git(self, tmp_path: Path):
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        init_cmd._backfill_project_gitignore(tmp_path, InitContext(project_root=tmp_path))
        diagnostic = tmp_path / "logs" / "runner-diagnostics.log"
        baseline_worktree = tmp_path / ".baseline-wt-42-deadbeef"
        diagnostic.parent.mkdir()
        baseline_worktree.mkdir()
        diagnostic.write_text("diagnostic\n", encoding="utf-8")
        nested_log = tmp_path / "cores" / "example" / "logs" / "result.log"
        nested_log.parent.mkdir(parents=True)
        nested_log.write_text("durable input\n", encoding="utf-8")

        result = subprocess.run(
            [
                "git",
                "check-ignore",
                "logs/runner-diagnostics.log",
                ".baseline-wt-42-deadbeef/",
            ],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        )

        assert result.stdout.splitlines() == [
            "logs/runner-diagnostics.log",
            ".baseline-wt-42-deadbeef/",
        ]
        nested_result = subprocess.run(
            ["git", "check-ignore", "cores/example/logs/result.log"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
        )
        assert nested_result.returncode == 1
        assert nested_result.stdout == ""

    def test_live_ticket_state_is_ignored_and_ticket_history_is_tracked(self, tmp_path: Path):
        # ADR 0065: the board and state records are working state; history is committed.
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, timeout=30)
        init_cmd._backfill_project_gitignore(tmp_path, InitContext(project_root=tmp_path))

        def ignored(path: str) -> bool:
            result = subprocess.run(
                ["git", "-c", "core.excludesFile=/dev/null", "check-ignore", "-q", path],
                cwd=tmp_path,
                check=False,
                timeout=30,
            )
            return result.returncode == 0

        assert ignored("tickets/board/alpha.md")
        assert ignored("tickets/state/alpha.json")
        assert ignored("tickets/waiver-candidates/alpha.json")
        assert not ignored("tickets/history/alpha.md")

    def test_anchored_spelling_is_not_duplicated(self, tmp_path: Path):
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("/tickets/board/\n/tickets/state/\n", encoding="utf-8")

        init_cmd._backfill_project_gitignore(tmp_path, InitContext(project_root=tmp_path))

        lines = gitignore.read_text(encoding="utf-8").splitlines()
        assert "tickets/board/" not in lines
        assert "tickets/state/" not in lines
        assert "tickets/logs/" in lines


# ---------------------------------------------------------------------------
# Windows filesystem evidence for #609 (runs in the Windows CI shards)
# ---------------------------------------------------------------------------


def _git(repository: Path, *args: str) -> str:
    """Run one Git command in *repository* and return its stdout."""
    result = subprocess.run(
        ["git", "-C", str(repository), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return result.stdout


def _crlf_project_data(project_dir: Path) -> list[str]:
    """Tracked or untracked Project data whose working-tree copy has CRLF.

    ``git ls-files --eol`` reports ``w/crlf`` (or ``w/mixed``) for such files;
    ignored runtime state is out of scope.
    """
    listing = _git(project_dir, "ls-files", "--eol", "--cached", "--others", "--exclude-standard")
    return [line for line in listing.splitlines() if "w/crlf" in line or "w/mixed" in line]


_IDENTITY = ("-c", "user.name=t", "-c", "user.email=t@t")


def _crlf_checkout_project_data(project_dir: Path, files: dict[str, bytes]) -> None:
    """Commit LF Project data, then re-check it out as Git for Windows would.

    Git for Windows defaults to ``core.autocrlf=true``: every tracked file
    lands with CRLF while ``git status`` stays clean.
    """
    project_dir.mkdir()
    _git(project_dir, "init", "-q")
    for name, data in files.items():
        (project_dir / name).write_bytes(data)
    _git(project_dir, "add", "-A")
    _git(project_dir, *_IDENTITY, "commit", "-qm", "project data")
    _git(project_dir, "config", "core.autocrlf", "true")
    for name in files:
        (project_dir / name).unlink()
    _git(project_dir, "checkout", "--", ".")
    assert _crlf_project_data(project_dir)
    assert _git(project_dir, "status", "--porcelain", "--untracked-files=no") == ""


def _stub_current_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Report the image and auth as current so init reaches every file step."""
    current_image = LifecycleResult(
        selected_reference="booley-sandbox",
        selected_id="sha256:test-image",
        status=ImageLifecycleStatus.CURRENT,
    )

    def current_image_step(ctx: InitContext, _bootstrap: object = None) -> LifecycleResult:
        ctx.record("docker_image", "skip", "current")
        return current_image

    monkeypatch.setattr(init_cmd, "_reconcile_initialized_image", current_image_step)
    monkeypatch.setattr(
        init_cmd,
        "_step_auth",
        lambda ctx, *_args, **_kwargs: ctx.record("auth", "skip", "current"),
    )


def _check_only(root: Path) -> int:
    args = _init_args()
    args.check_only = True
    reset_cache()
    return init_cmd.run_init(args, root)


_GUIDANCE = b"# Guidance\n\nUse LF.\n"
_AGENT_TABLE = b'[agent]\nprovider = "claude"\nauth = "subscription"\n'
_CONFIG_WITHOUT_AGENT = b"# user config\n[flows.sim]\nenabled = true\n"


def test_agent_selection_write_does_not_block_line_ending_repair(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Init's own [agent] write to CRLF project data must not look like user work."""
    _stub_current_host(monkeypatch)
    project_dir = repo / ".booley_project"
    _crlf_checkout_project_data(
        project_dir, {"AGENTS.md": _GUIDANCE, "booley.toml": _CONFIG_WITHOUT_AGENT}
    )

    assert _run_full_init(repo) == 0
    assert "refusing to normalize" not in capsys.readouterr().out

    config = (project_dir / "booley.toml").read_bytes()
    assert config.startswith(_CONFIG_WITHOUT_AGENT)
    assert config.endswith(_AGENT_TABLE)
    assert _crlf_project_data(project_dir) == []
    assert _check_only(repo) == 0


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows link and text-mode semantics")
class TestWindowsProjectDataLineEndings:
    """Real Project Initialization on the Windows filesystem (#609).

    Nothing that writes files is stubbed: init creates the project data, the
    guidance links, and runs the line-ending repair against real Git. Only
    host services (bootstrap, image, auth, Runtime) are stubbed by ``repo``.
    ``force_hardlink_fallback`` makes the guidance symlink attempt fail the way
    it does without Developer Mode (CI runners are admin, so symlinks would
    otherwise succeed) and exercises the hardlink fallback from #609.
    """

    @pytest.fixture(params=[True, False], ids=["hardlink-fallback", "native-links"])
    def force_hardlink_fallback(
        self, request: pytest.FixtureRequest, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> bool:
        if request.param:
            original_symlink_to = Path.symlink_to
            project_root = os.path.normcase(repo.resolve())

            def no_developer_mode(link: Path, *args: object, **kwargs: object) -> None:
                """Refuse only the root guidance symlinks, as Windows does sans Developer Mode."""
                is_guidance = link.name in ("AGENTS.md", "CLAUDE.md") and (
                    os.path.normcase(link.parent.resolve()) == project_root
                )
                if is_guidance:
                    raise OSError(1314, "A required privilege is not held by the client")
                return original_symlink_to(link, *args, **kwargs)

            monkeypatch.setattr(Path, "symlink_to", no_developer_mode)

        _stub_current_host(monkeypatch)
        return request.param

    def _assert_safe_and_linked(self, repo: Path, *, hardlink: bool) -> None:
        project_dir = repo / ".booley_project"
        canonical = project_dir / "AGENTS.md"
        assert b"\r" not in canonical.read_bytes()
        assert _crlf_project_data(project_dir) == []
        for name in ("AGENTS.md", "CLAUDE.md"):
            entry = repo / name
            assert entry.samefile(canonical), name
            if hardlink:
                assert not entry.is_symlink(), name
        if hardlink:
            assert canonical.stat().st_nlink == 3
        assert _check_only(repo) == 0

    def test_clean_windows_init_writes_lf_project_data_and_passes_check_only(
        self, repo: Path, force_hardlink_fallback: bool
    ) -> None:
        project_dir = repo / ".booley_project"
        project_dir.mkdir()
        # booley-setup Step 3 authors the canonical guidance; init links it.
        (project_dir / "AGENTS.md").write_bytes(_GUIDANCE)

        assert _run_full_init(repo) == 0
        self._assert_safe_and_linked(repo, hardlink=force_hardlink_fallback)

        # Committing the scaffolds keeps them LF in the index and worktree.
        _git(project_dir, "add", "-A")
        _git(project_dir, *_IDENTITY, "commit", "-qm", "setup")
        eol = _git(project_dir, "ls-files", "--eol")
        assert "booley.toml" in eol
        assert "i/crlf" not in eol
        self._assert_safe_and_linked(repo, hardlink=force_hardlink_fallback)

    def test_crlf_project_data_behind_guidance_links_is_repaired(
        self,
        repo: Path,
        force_hardlink_fallback: bool,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        project_dir = repo / ".booley_project"
        # 1. A Windows checkout with Git for Windows' default autocrlf=true:
        #    every tracked Project data file lands with CRLF, index clean.
        _crlf_checkout_project_data(
            project_dir,
            {"AGENTS.md": _GUIDANCE, "booley.toml": _AGENT_TABLE, "notes.md": b"# Notes\n"},
        )

        # 2. An uncommitted user edit makes the line-ending repair refuse, so
        #    init reports setup incomplete (#611) -- yet the guidance step still
        #    links the root guidance to the CRLF canonical file.
        with (project_dir / "notes.md").open("ab") as notes:
            notes.write(b"work in progress\r\n")
        assert _run_full_init(repo) == 2
        assert "refusing to normalize" in capsys.readouterr().out
        canonical = project_dir / "AGENTS.md"
        assert b"\r\n" in canonical.read_bytes()
        assert (repo / "AGENTS.md").samefile(canonical)
        if force_hardlink_fallback:
            assert not (repo / "AGENTS.md").is_symlink()
            assert canonical.stat().st_nlink == 3
        assert _check_only(repo) != 0

        # 3. The user commits the edit and re-runs init, as init instructs. The
        #    Booley-created hardlinks no longer block the repair (#609).
        _git(project_dir, "add", "notes.md")
        _git(project_dir, *_IDENTITY, "commit", "-qm", "notes")
        capsys.readouterr()
        assert _run_full_init(repo) == 0
        output = capsys.readouterr().out
        if force_hardlink_fallback:
            assert "released guidance hardlink AGENTS.md" in output
        assert _git(project_dir, "config", "--local", "core.autocrlf").strip() == "false"
        self._assert_safe_and_linked(repo, hardlink=force_hardlink_fallback)

    def test_agent_selection_write_on_crlf_project_data_passes_check_only(
        self,
        repo: Path,
        force_hardlink_fallback: bool,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A tracked booley.toml without [agent]: init edits it, then repairs."""
        project_dir = repo / ".booley_project"
        _crlf_checkout_project_data(
            project_dir, {"AGENTS.md": _GUIDANCE, "booley.toml": _CONFIG_WITHOUT_AGENT}
        )

        assert _run_full_init(repo) == 0
        assert "refusing to normalize" not in capsys.readouterr().out
        assert (project_dir / "booley.toml").read_bytes().endswith(_AGENT_TABLE)
        self._assert_safe_and_linked(repo, hardlink=force_hardlink_fallback)


@pytest.mark.parametrize("stale_sessions", [[], ["booley-session-old"]])
def test_image_convergence_reports_live_sandbox_drift(repo, monkeypatch, capsys, stale_sessions):
    image = pi.project_image_name(repo)
    changed = LifecycleResult(
        selected_reference=image,
        selected_id="sha256:new-image",
        status=ImageLifecycleStatus.CHANGED,
        changed_images=(image,),
    )
    monkeypatch.setattr(init_cmd.image_lifecycle, "reconcile_planned", lambda *a, **k: changed)
    monkeypatch.setattr(sr, "sessions_on_stale_image", lambda root, selected: stale_sessions)

    assert init_cmd._step_image_lifecycle(InitContext(project_root=repo)) is changed

    output = capsys.readouterr().out
    if stale_sessions:
        assert "booley-session-old" in output
        assert "booley session down && booley session up" in output
    else:
        assert "booley session down" not in output
