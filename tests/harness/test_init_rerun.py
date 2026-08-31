"""Golden re-run coverage for ``booley init`` (the clobber-guard contract).

Runs the FULL init flow twice over a scratch git repo, hand-editing every
scaffolded file in between, and asserts the ownership contract file by file:

- user-owned skeletons (booley.toml, tests.toml, ticket_creation.md, FUSESOC_IGNORE,
  .booley_project/.gitignore) keep their hand edits, and
- fully-managed files (vendored hook scripts, .git/hooks delegators,
  devcontainer.json) come back byte-identical to the first run — hand edits
  are deliberately regenerated away.

Docker/vscode/systemctl are absent (shutil.which -> None). The fatal Docker
preflight is stubbed as healthy so this clobber-contract test can exercise the
remaining steps; image/network steps still skip or error without subprocesses.
The docker-file preservation path (SETUP-6, 8c6a01c) is exercised separately
with a stubbed image build.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from booley.eda import runtime_spec
from booley.harness import init_cmd
from booley.harness import session_runtime as sr
from booley.harness.init_common import InitContext
from booley.runtime import project_image as pi
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

    def docker_preflight(ctx: InitContext) -> bool:
        ctx.step_banner("host bootstrap tool detection")
        ctx.record("host_prerequisites", "ok")
        return True

    monkeypatch.setattr(init_cmd, "_step_host_prerequisites", docker_preflight)
    monkeypatch.setattr(runtime_spec, "_resolve_image_id", lambda _image: "sha256:test-image")
    monkeypatch.setattr(init_cmd, "_select_interactive_app", lambda *_: "none")
    pdk_root = tmp_path / "pdk"
    pdk_root.mkdir()
    monkeypatch.setattr(init_cmd, "_step_nangate_pdk", lambda _ctx: pdk_root)
    return root


class TestFullInitRerun:
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
            *sorted((pdir / "hooks").iterdir()),  # vendored sanitizer scripts
            hooks_dir / "commit-msg",  # delegators (contain the script
            hooks_dir / "pre-push",  # name, so they read as ours)
            repo / ".devcontainer" / "devcontainer.json",
        ]
        baseline = {}
        for f in managed:
            assert f.is_file(), f"expected managed file missing after init: {f}"
            baseline[f] = f.read_bytes()
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
        assert "commit_msg_hook.py" in (hooks_dir / "commit-msg").read_text(encoding="utf-8")

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


class TestProjectImagePreservation:
    """The SETUP-6 docker-file gate (8c6a01c), exercised through the step."""

    @pytest.fixture
    def image_repo(self, repo: Path, monkeypatch):
        """Project dir + stubbed docker so _step_project_image runs its writes.

        The generated image is reported as present (``image_exists`` -> True)
        so the SETUP-6 skip contract is what's under test; the F-5 absent-image
        build-from-user-files path re-stubs it to False per test.
        """
        pdir = repo / ".booley_project"
        pdir.mkdir()
        (pdir / "booley.toml").write_text("", encoding="utf-8")

        monkeypatch.setattr(
            init_cmd.shutil, "which", lambda n: "/usr/bin/docker" if n == "docker" else None
        )
        monkeypatch.setattr(init_cmd.idk, "image_exists", lambda name: True)
        builds: list = []
        monkeypatch.setattr(pi, "build_project_image", lambda *a, **k: builds.append(a) or True)
        body = f"{pi._GENERATED_HEADER}\n# Sources: test\n\nrequests==1.0\n"
        monkeypatch.setattr(
            init_cmd,
            "_resolve_baked_requirements",
            lambda ctx, sandbox: (body, ["requests==1.0"]),
        )
        reset_cache()
        return repo, builds

    def test_hand_edited_dockerfile_survives_and_skips_build(self, image_repo):
        repo, builds = image_repo
        ctx = InitContext(project_root=repo)
        init_cmd._step_project_image(ctx)

        dockerfile = repo / ".booley_project" / "docker" / "Dockerfile"
        requirements = repo / ".booley_project" / "docker" / "requirements.txt"
        assert dockerfile.is_file() and requirements.is_file()
        assert builds, "first run should build the project image"

        dockerfile.write_text(
            dockerfile.read_text(encoding="utf-8") + "RUN echo my-custom-layer\n",
            encoding="utf-8",
        )
        edited = dockerfile.read_text(encoding="utf-8")
        req_before = requirements.read_bytes()
        builds.clear()

        ctx2 = InitContext(project_root=repo)
        init_cmd._step_project_image(ctx2)

        assert dockerfile.read_text(encoding="utf-8") == edited, (
            "re-running init clobbered a hand-edited Dockerfile (SETUP-6)"
        )
        assert requirements.read_bytes() == req_before, (
            "sibling requirements.txt must be left alone when Dockerfile is user-owned"
        )
        assert not builds, "user-owned docker files must skip the image build"
        assert ctx2.results[-1].status == "skip"

    def test_keep_directive_makes_generated_file_user_owned(self, image_repo):
        repo, builds = image_repo
        init_cmd._step_project_image(InitContext(project_root=repo))

        requirements = repo / ".booley_project" / "docker" / "requirements.txt"
        flagged = requirements.read_text(encoding="utf-8") + "# booley:keep\nextra-dep==2.0\n"
        requirements.write_text(flagged, encoding="utf-8")
        builds.clear()

        ctx2 = InitContext(project_root=repo)
        init_cmd._step_project_image(ctx2)

        assert requirements.read_text(encoding="utf-8") == flagged
        assert not builds
        assert ctx2.results[-1].status == "skip"


class TestStaleSessionWarning:
    """F-9: rebuilding the project image while a session container is live leaves
    that container on the old image, silently. Init must say so at the build."""

    @pytest.fixture
    def built_repo(self, repo: Path, monkeypatch):
        """Project dir + stubbed docker so the build reaches the warning tail."""
        pdir = repo / ".booley_project"
        pdir.mkdir()
        (pdir / "booley.toml").write_text("", encoding="utf-8")

        monkeypatch.setattr(
            init_cmd.shutil, "which", lambda n: "/usr/bin/docker" if n == "docker" else None
        )
        monkeypatch.setattr(init_cmd.idk, "image_exists", lambda name: True)
        monkeypatch.setattr(pi, "build_project_image", lambda *a, **k: True)
        monkeypatch.setattr(
            init_cmd,
            "_resolve_baked_requirements",
            lambda ctx, sandbox: (f"{pi._GENERATED_HEADER}\n\nrequests==1.0\n", ["requests==1.0"]),
        )
        reset_cache()
        return repo

    def test_live_session_on_the_old_image_warns_with_the_recipe(self, built_repo, capsys):
        with patch.object(sr, "sessions_on_stale_image", return_value=["booley-session-x"]):
            init_cmd._step_project_image(InitContext(project_root=built_repo))

        out = capsys.readouterr().out
        assert "booley-session-x" in out
        assert "booley session down && booley session up" in out

    def test_no_stale_session_is_silent(self, built_repo, capsys):
        with patch.object(sr, "sessions_on_stale_image", return_value=[]):
            init_cmd._step_project_image(InitContext(project_root=built_repo))

        assert "booley session down" not in capsys.readouterr().out


class TestHandAuthoredImageBuild:
    """F-5: user-owned docker files whose image was never built.

    Skipping here dead-ended the natural path — drop a requirements.txt (or a
    whole Dockerfile) into .booley_project/docker/ and re-run init — leaving
    the user to `docker build` and edit [sandbox].image by hand. Init must now
    build from the user's files verbatim, and keep skipping once the image
    exists (SETUP-6 idempotency).
    """

    HAND_REQS = "# hand-authored\ncocotb==1.9.2\n"
    HAND_DOCKERFILE = "FROM booley-sandbox\nRUN echo custom\n"

    @pytest.fixture
    def hand_repo(self, repo: Path, monkeypatch):
        """Project dir + stubbed docker where the generated image is ABSENT."""
        pdir = repo / ".booley_project"
        pdir.mkdir()
        (pdir / "booley.toml").write_text("", encoding="utf-8")
        (pdir / "docker").mkdir()

        monkeypatch.setattr(
            init_cmd.shutil, "which", lambda n: "/usr/bin/docker" if n == "docker" else None
        )
        monkeypatch.setattr(init_cmd.idk, "image_exists", lambda name: False)
        builds: list = []
        monkeypatch.setattr(pi, "build_project_image", lambda *a, **k: builds.append(a) or True)
        reset_cache()
        return repo, builds

    def test_lone_requirements_gets_managed_dockerfile_and_builds(self, hand_repo):
        repo, builds = hand_repo
        docker_dir = repo / ".booley_project" / "docker"
        (docker_dir / "requirements.txt").write_text(self.HAND_REQS, encoding="utf-8")

        ctx = InitContext(project_root=repo)
        init_cmd._step_project_image(ctx)

        # Built from the user's file, which stays byte-identical; only the
        # missing Dockerfile is backfilled with the managed one.
        assert builds and builds[0][0] == pi.project_image_name(repo)
        assert (docker_dir / "requirements.txt").read_text(encoding="utf-8") == self.HAND_REQS
        dockerfile_text = (docker_dir / "Dockerfile").read_text(encoding="utf-8")
        assert dockerfile_text.startswith(pi._GENERATED_HEADER)
        assert ctx.results[-1].status == "ok"

    def test_hand_dockerfile_is_built_verbatim_and_selected_implicitly(self, hand_repo):
        repo, builds = hand_repo
        docker_dir = repo / ".booley_project" / "docker"
        (docker_dir / "Dockerfile").write_text(self.HAND_DOCKERFILE, encoding="utf-8")
        (docker_dir / "requirements.txt").write_text(self.HAND_REQS, encoding="utf-8")

        ctx = InitContext(project_root=repo)
        init_cmd._step_project_image(ctx)

        assert builds, "absent image + hand-authored files must build (F-5)"
        assert (docker_dir / "Dockerfile").read_text(encoding="utf-8") == self.HAND_DOCKERFILE
        assert (docker_dir / "requirements.txt").read_text(encoding="utf-8") == self.HAND_REQS
        # The generated name is derived from docker/; booley.toml stays clean.
        toml_text = (repo / ".booley_project" / "booley.toml").read_text(encoding="utf-8")
        assert "image =" not in toml_text
        assert init_cmd.project_sandbox_image(repo) == pi.project_image_name(repo)
        assert ctx.results[-1].status == "ok"

    def test_check_only_reports_would_build_without_touching_anything(self, hand_repo):
        repo, builds = hand_repo
        docker_dir = repo / ".booley_project" / "docker"
        (docker_dir / "requirements.txt").write_text(self.HAND_REQS, encoding="utf-8")

        ctx = InitContext(project_root=repo, check_only=True)
        init_cmd._step_project_image(ctx)

        assert not builds
        assert not (docker_dir / "Dockerfile").exists()
        assert ctx.results[-1].status == "warn"

    def test_once_image_exists_rerun_skips_again(self, hand_repo, monkeypatch):
        repo, builds = hand_repo
        docker_dir = repo / ".booley_project" / "docker"
        (docker_dir / "requirements.txt").write_text(self.HAND_REQS, encoding="utf-8")
        init_cmd._step_project_image(InitContext(project_root=repo))
        assert builds

        # The F-5 build happened; from now on the image exists, so the SETUP-6
        # manual-edits skip is back in force (idempotent re-init).
        monkeypatch.setattr(init_cmd.idk, "image_exists", lambda name: True)
        builds.clear()
        ctx2 = InitContext(project_root=repo)
        init_cmd._step_project_image(ctx2)

        assert not builds
        assert ctx2.results[-1].status == "skip"

    def test_stale_existing_image_rebuilds_from_unchanged_user_files(self, hand_repo, monkeypatch):
        repo, builds = hand_repo
        docker_dir = repo / ".booley_project" / "docker"
        dockerfile = docker_dir / "Dockerfile"
        dockerfile.write_text(self.HAND_DOCKERFILE, encoding="utf-8")
        monkeypatch.setattr(init_cmd.idk, "image_exists", lambda _name: True)
        monkeypatch.setattr(init_cmd, "source_fingerprint_mismatch", lambda _name: True)

        init_cmd._step_project_image(InitContext(project_root=repo))

        assert builds
        assert dockerfile.read_text(encoding="utf-8") == self.HAND_DOCKERFILE

    def test_project_image_refreshes_shipped_flavor_parent(self, hand_repo, monkeypatch):
        repo, builds = hand_repo
        docker_dir = repo / ".booley_project" / "docker"
        dockerfile = docker_dir / "Dockerfile"
        dockerfile.write_text("FROM booley-sandbox-riscv\nRUN echo custom\n", encoding="utf-8")
        monkeypatch.setattr(init_cmd.idk, "image_exists", lambda _name: True)
        refreshed: list[str] = []
        monkeypatch.setattr(
            init_cmd,
            "ensure_flavor_image",
            lambda _ctx, image: refreshed.append(image) or True,
        )
        monkeypatch.setattr(init_cmd, "_warn_on_live_session_on_old_image", lambda *_args: None)

        init_cmd._step_project_image(InitContext(project_root=repo))

        assert refreshed == ["booley-sandbox-riscv"]
        assert builds
        assert dockerfile.read_text(encoding="utf-8").startswith("FROM booley-sandbox-riscv")


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
    """fpu F-29: init vendors the hook scripts into .booley_project/hooks/ and
    running them drops __pycache__/*.pyc right beside the sources — which the
    inner .booley_project repo then tracked."""

    def test_new_gitignore_covers_python_bytecode(self, tmp_path: Path):
        init_cmd._backfill_project_gitignore(tmp_path, InitContext(project_root=tmp_path))

        lines = (tmp_path / ".gitignore").read_text(encoding="utf-8").splitlines()
        assert "__pycache__/" in lines
        assert "*.pyc" in lines

    def test_existing_gitignore_is_backfilled_without_losing_user_lines(self, tmp_path: Path):
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("tmp/\nmy_custom_dir/\n", encoding="utf-8")

        init_cmd._backfill_project_gitignore(tmp_path, InitContext(project_root=tmp_path))

        lines = gitignore.read_text(encoding="utf-8").splitlines()
        assert "my_custom_dir/" in lines
        assert "__pycache__/" in lines
        assert lines.count("tmp/") == 1  # already present -> not re-added

    def test_backfill_is_idempotent(self, tmp_path: Path):
        ctx = InitContext(project_root=tmp_path)
        init_cmd._backfill_project_gitignore(tmp_path, ctx)
        first = (tmp_path / ".gitignore").read_bytes()

        init_cmd._backfill_project_gitignore(tmp_path, ctx)

        assert (tmp_path / ".gitignore").read_bytes() == first
