"""Golden re-run coverage for ``booley init`` (the clobber-guard contract).

Runs the FULL init flow twice over a scratch git repo, hand-editing every
scaffolded file in between, and asserts the ownership contract file by file:

- user-owned skeletons (booley.toml, tests.toml, ticket_creation.md, FUSESOC_IGNORE,
  .booley/.gitignore) keep their hand edits, and
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
import stat
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from booley.harness import init_cmd
from booley.harness.setup import interactive as interactive_init
from booley.harness.setup.common import InitContext
from booley.runtime import project_image as pi
from booley.runtime import session_issuance as runtime_spec
from booley.runtime import session_runtime as sr
from booley.runtime.booley_dir import reset_cache
from booley.runtime.git import add_git_excludes
from booley.runtime.image_lifecycle import LifecycleResult
from booley.runtime.image_lifecycle import Status as ImageLifecycleStatus
from booley.runtime.project_gitignore import PROJECT_GITIGNORE_PATTERNS
from tests.conftest import symlink_or_skip, use_terminal_stdin
from tests.docker_host_support import fake_docker_host
from tests.harness.test_project_initialization import _initialize
from tests.session_issuance_support import SHIPPED_T3_POLICY
from tests.subprocess_budget import run_bounded_child

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


def _unknown_sandbox_git_version(monkeypatch) -> None:
    """Init's Sandbox git-version probe answers ``None``, as without the image."""
    # private: the version probe starts Docker; no public input makes it answer without it.
    monkeypatch.setattr(init_cmd, "_sandbox_git_version", lambda *_args, **_kwargs: None)


@pytest.fixture(autouse=True)
def _no_sandbox_git_version(monkeypatch):
    _unknown_sandbox_git_version(monkeypatch)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    """A scratch git repo with isolated HOME and no docker/vscode/systemctl."""
    root = tmp_path / "proj"
    root.mkdir()
    run_bounded_child(["git", "init", str(root)], check=True)

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("BOOLEY_DIR", raising=False)

    trusted_booley_path = home / ".local" / "bin" / "booley"
    trusted_booley_path.parent.mkdir(parents=True)
    trusted_booley_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    trusted_booley_path.chmod(0o755)
    trusted_booley = str(trusted_booley_path)
    monkeypatch.setattr(
        init_cmd.shutil, "which", lambda name: trusted_booley if name == "booley" else None
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
        init_cmd.image_lifecycle, "reconcile_planned", lambda *_args, **_kwargs: current_image
    )
    # private: image ids come from Docker, which these tests never reach.
    monkeypatch.setattr(runtime_spec, "_resolve_image_id", lambda _image: "sha256:test-image")
    monkeypatch.setattr(runtime_spec, "_keeper_image_id", lambda _image: "sha256:test-image")
    # private: the image's T3 Code policy is read through Docker too.
    monkeypatch.setattr(runtime_spec, "_image_t3_policy", lambda _image: SHIPPED_T3_POLICY)
    fake_docker_host(monkeypatch)
    monkeypatch.setattr("booley.runtime.host_docker.image_id", lambda _image: None)
    # private: host-only probes (editor detection, credentials) that the public `run_init`
    # would answer from the host.
    monkeypatch.setattr(init_cmd, "_select_agent_app", lambda *_: "none")
    monkeypatch.setattr(
        init_cmd, "_step_auth", lambda ctx, *_a, **_k: ctx.record("auth", "skip", "current")
    )
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
    _unknown_sandbox_git_version(monkeypatch)
    return root


def _write_booley_toml(repo: Path, *, stealth: bool) -> None:
    (repo / ".booley").mkdir(exist_ok=True)
    (repo / ".booley/booley.toml").write_text(f"[stealth]\nenabled = {str(stealth).lower()}\n")


@pytest.mark.timeout(120)
def test_every_path_init_creates_is_classified(repo):
    """Every file and empty directory of a full Stealth init has a Booley State part."""
    from booley.core.booley_dir_parts import booley_dir_part

    _write_booley_toml(repo, stealth=True)  # adds inner .git
    assert _run_full_init(repo) == 0
    booley = repo / ".booley"
    created = [
        path
        for path in booley.rglob("*")
        if path.is_file() or (path.is_dir() and not any(path.iterdir()))
    ]
    assert created, "init created nothing under the Booley Directory"
    for path in created:
        booley_dir_part(path.relative_to(booley))


def _info_exclude(repo: Path) -> list[str]:
    return (repo / ".git/info/exclude").read_text(encoding="utf-8").splitlines()


@pytest.mark.timeout(120)
def test_fresh_init_without_stealth_choice_keeps_booley_dir_trackable(repo):
    """The init skeleton has no ``[stealth] enabled``, so init must not hide `.booley`."""
    assert _run_full_init(repo) == 0

    assert not (repo / ".booley/.git").exists()
    assert "/.booley" not in _info_exclude(repo)


@pytest.mark.timeout(120)
def test_hidden_setup_choice_hides_booley_dir_on_init_rerun(repo):
    """Setup's hidden answer writes ``enabled = true``; the next init applies it."""
    assert _run_full_init(repo) == 0
    config = repo / ".booley/booley.toml"
    config.write_text(
        config.read_text(encoding="utf-8") + "\n[stealth]\nenabled = true\n", encoding="utf-8"
    )

    assert _run_full_init(repo) == 0

    assert (repo / ".booley/.git").is_dir()
    assert "/.booley" in _info_exclude(repo)


@pytest.mark.timeout(120)
def test_init_names_unrecorded_stealth_choice_of_hidden_booley_dir(repo, capsys):
    """A Project hidden while Stealth defaulted on, with no `enabled` key, gets the fix."""
    _write_booley_toml(repo, stealth=True)
    assert _run_full_init(repo) == 0
    (repo / ".booley/booley.toml").write_text("# key predates the explicit choice\n")
    capsys.readouterr()

    assert _run_full_init(repo) == 0

    output = capsys.readouterr().out
    assert "hides /.booley" in output and "[stealth] enabled = true" in output


@pytest.mark.timeout(120)
def test_nonstealth_init_reports_global_ignored_booley_paths(repo, capsys):
    _write_booley_toml(repo, stealth=False)
    ignores = repo.parent / "global ignores"
    ignores.write_text("# local policy\n.booley/\n")
    subprocess.run(["git", "config", "core.excludesFile", str(ignores)], cwd=repo, check=True)
    assert _run_full_init(repo) == 0
    output = capsys.readouterr().out
    assert "trackability" in output and str(ignores) in output, (
        "non-Stealth init must warn with the excluding source"
    )
    assert ":2" in output and ".booley/booley.toml" in output
    assert "edit the reported" in output and "warn" in output.lower()
    assert ignores.read_text() == "# local policy\n.booley/\n"


@pytest.mark.timeout(120)
def test_nonstealth_trackability_check_only_is_read_only(repo, capsys):
    _write_booley_toml(repo, stealth=False)
    assert _run_full_init(repo) == 0
    capsys.readouterr()
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "initialized",
        ],
        cwd=repo,
        check=True,
    )
    ignores = repo.parent / "global ignores"
    ignores.write_text("# local policy\n.booley/\n")
    subprocess.run(["git", "config", "core.excludesFile", str(ignores)], cwd=repo, check=True)
    before = _filesystem_snapshot(repo)
    args = _init_args()
    args.check_only = True
    assert init_cmd.run_init(args, repo) == 1
    output = capsys.readouterr().out
    assert "project_trackability" in output and str(ignores) + ":2" in output
    assert _filesystem_snapshot(repo) == before


class TestFullInitRerun:
    # Windows CI measured 34.1s: 3x slowest rounded up to 30s requires 120s.
    @pytest.mark.timeout(120)
    def test_check_only_after_full_init_is_current_and_mutation_free(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
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

    @pytest.mark.parametrize(
        ("row", "code", "message"),
        [
            ("reconciled", 0, "reconciled stopped Sandbox resources from their prior issuance"),
            ("apply-fails", 2, "could not reconcile the Sandbox: container became active"),
            (
                "inspect-fails",
                2,
                "could not reconcile the Sandbox: run `booley session refresh` on the host",
            ),
        ],
    )
    def test_sandbox_runtime_reconciliation(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        row: str,
        code: int,
        message: str,
    ) -> None:
        def inspect(*_args: object) -> SimpleNamespace:
            if row == "inspect-fails":
                raise sr.SessionError("run `booley session refresh` on the host")
            return SimpleNamespace(pending=True)

        def reconcile(*_args: object) -> bool:
            if row == "apply-fails":
                raise sr.SessionError("container became active")
            return True

        monkeypatch.setattr(sr, "plan_stopped_headless_runtime_reconciliation", inspect)
        monkeypatch.setattr(sr, "reconcile_stopped_headless_runtime", reconcile)

        assert _run_full_init(repo) == code

        output = capsys.readouterr().out
        assert message in output
        if row != "reconciled":
            assert "this project is ready" not in output

    def test_foreign_root_guidance_blocks_before_any_filesystem_mutation(self, repo: Path):
        booley_dir = repo / ".booley"
        booley_dir.mkdir()
        canon = booley_dir / "AGENTS.md"
        canon.write_text("# canonical\n", encoding="utf-8")
        foreign = repo / "AGENTS.md"
        foreign.write_text("# user-owned\n", encoding="utf-8")
        before = _complete_snapshot(repo, skip_git=True)

        assert _run_full_init(repo) == 2

        assert _complete_snapshot(repo, skip_git=True) == before
        assert not (repo / "CLAUDE.md").exists()

    # Windows CI measured 40.6s; 3x rounded up to 30s requires 150s.
    @pytest.mark.timeout(150)
    def test_second_run_preserves_user_edits_and_regenerates_managed_files(self, repo: Path):
        rc1 = _run_full_init(repo)
        assert rc1 in (0, 2)  # 2: docker/vscode absent -> dependency-detection step errs

        pdir = repo / ".booley"
        hooks_dir = repo / ".git" / "hooks"

        # --- user-owned scaffolding: init must never touch these again -----
        # NB: no adapters/ here — init scaffolds no adapter stubs (ADR 0039
        # dropped the project-native path; every Booley Flow uses built-in orchestration).
        user_owned = [
            pdir / "booley.toml",
            pdir / "tests.toml",
            pdir / "metrics.toml",
            pdir / "FUSESOC_IGNORE",
        ]
        for f in user_owned:
            assert f.is_file(), f"expected scaffolded file missing after init: {f}"
            f.write_text(f.read_text(encoding="utf-8") + HAND_EDIT, encoding="utf-8")

        gitignore = pdir / ".gitignore"
        gitignore.write_text(
            gitignore.read_text(encoding="utf-8") + "my_custom_dir/\n", encoding="utf-8"
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
                    f.read_text(encoding="utf-8") + "# vandalism\n", encoding="utf-8", newline="\n"
                )

        rc2 = _run_full_init(repo)
        assert rc2 in (0, 2)

        for f in user_owned:
            assert f.read_text(encoding="utf-8").endswith(HAND_EDIT), (
                f"re-running init clobbered user-owned file {f}"
            )
        assert "my_custom_dir/" in gitignore.read_text(encoding="utf-8"), (
            "re-running init dropped a user line from .booley/.gitignore"
        )
        for f, expected in baseline.items():
            assert f.read_bytes() == expected, (
                f"managed file {f} not byte-stable across init re-runs"
            )

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


@pytest.mark.parametrize(
    "base_packages", [{"cocotb": "2.1.0"}, {}], ids=["base-image", "no-base-image"]
)
def test_curated_override_advisory(repo, monkeypatch, capsys, base_packages):
    """F-13: a pin that re-versions a package the base image curated is named; no base, no noise."""
    (repo / ".booley").mkdir()
    (repo / ".booley/booley.toml").write_text(
        '[sandbox]\npip_requirements = ["requirements.txt"]\n'
    )
    (repo / "requirements.txt").write_text("cocotb==1.5.1\nnumpy==2.0\n")
    monkeypatch.setattr(pi, "base_image_packages", lambda *_a, **_k: base_packages)

    assert _run_full_init(repo) == 0

    out = capsys.readouterr().out
    if base_packages:
        assert "cocotb==1.5.1" in out and "cocotb==2.1.0" in out
        assert "numpy" not in out
    else:
        assert "overrides the base image" not in out


@pytest.mark.parametrize(
    ("authored", "spelled_otherwise"),
    [
        (b"# custom policy\r\nmy_custom_dir/\r\ntmp/\r\n", set()),
        (b"/tickets/board/\n/tickets/state/\n", {"tickets/board/", "tickets/state/"}),
    ],
    ids=["crlf-custom-lines", "anchored-spelling"],
)
def test_gitignore_backfill_appends_each_missing_pattern_once(repo, authored, spelled_otherwise):
    ignore = repo / ".booley/.gitignore"
    ignore.parent.mkdir()
    ignore.write_bytes(authored)

    _initialize(repo, check_only=True)
    assert ignore.read_bytes() == authored

    _initialize(repo)
    first = ignore.read_bytes()
    lines = first.decode().splitlines()
    kept = authored.decode().splitlines()
    assert lines[: len(kept)] == kept  # the authored lines lead; init only appends
    for pattern in PROJECT_GITIGNORE_PATTERNS:
        assert lines.count(pattern) == (0 if pattern in spelled_otherwise else 1), pattern
    _initialize(repo)
    assert ignore.read_bytes() == first


# ---------------------------------------------------------------------------
# Windows filesystem evidence for #609 (runs in the Windows CI shards)
# ---------------------------------------------------------------------------


def _crlf_project_data(booley_dir: Path) -> list[str]:
    """Tracked or untracked files in the Booley Directory whose working-tree copy has CRLF.

    ``git ls-files --eol`` reports ``w/crlf`` (or ``w/mixed``) for such files;
    ignored runtime state is out of scope.
    """
    listing = _git(booley_dir, "ls-files", "--eol", "--cached", "--others", "--exclude-standard")
    return [line for line in listing.splitlines() if "w/crlf" in line or "w/mixed" in line]


_IDENTITY = ("-c", "user.name=t", "-c", "user.email=t@t")


def _crlf_checkout_project_data(booley_dir: Path, files: dict[str, bytes]) -> None:
    """Commit Booley configuration with LF line endings, then re-check it out as Git for Windows would.

    Git for Windows defaults to ``core.autocrlf=true``: every tracked file
    lands with CRLF while ``git status`` stays clean.
    """
    booley_dir.mkdir()
    _git(booley_dir, "init", "-q")
    for name, data in files.items():
        (booley_dir / name).write_bytes(data)
    _git(booley_dir, "add", "-A")
    _git(booley_dir, *_IDENTITY, "commit", "-qm", "Booley Directory")
    _git(booley_dir, "config", "core.autocrlf", "true")
    for name in files:
        (booley_dir / name).unlink()
    _git(booley_dir, "checkout", "--", ".")
    assert _crlf_project_data(booley_dir)
    assert _git(booley_dir, "status", "--porcelain", "--untracked-files=no") == ""


def _check_only(root: Path) -> int:
    args = _init_args()
    args.check_only = True
    reset_cache()
    return init_cmd.run_init(args, root)


_GUIDANCE = b"# Guidance\n\nUse LF.\n"
_AGENT_TABLE = b'[agent]\ndefault = "claude"\nauth = "subscription"\n'
_CONFIG_WITHOUT_AGENT = b"# user config\n[flows.sim]\nenabled = true\n"


def test_agent_selection_write_does_not_block_line_ending_repair(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Init's own [agent] write to Booley configuration with CRLF line endings must not look like user work."""
    booley_dir = repo / ".booley"
    _crlf_checkout_project_data(
        booley_dir, {"AGENTS.md": _GUIDANCE, "booley.toml": _CONFIG_WITHOUT_AGENT}
    )

    assert _run_full_init(repo) == 0
    assert "refusing to normalize" not in capsys.readouterr().out

    config = (booley_dir / "booley.toml").read_bytes()
    assert config.startswith(_CONFIG_WITHOUT_AGENT)
    assert config.endswith(_AGENT_TABLE)
    assert _crlf_project_data(booley_dir) == []
    assert _check_only(repo) == 0


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows link and text-mode semantics")
class TestWindowsProjectDataLineEndings:
    """Real Project Initialization on the Windows filesystem (#609).

    Nothing that writes files is stubbed: init creates the Booley Directory, the
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

        return request.param

    def _assert_safe_and_linked(self, repo: Path, *, hardlink: bool) -> None:
        booley_dir = repo / ".booley"
        canonical = booley_dir / "AGENTS.md"
        assert b"\r" not in canonical.read_bytes()
        assert _crlf_project_data(booley_dir) == []
        for name in ("AGENTS.md", "CLAUDE.md"):
            entry = repo / name
            assert entry.samefile(canonical), name
            if hardlink:
                assert not entry.is_symlink(), name
        if hardlink:
            assert canonical.stat().st_nlink == 3
        assert _check_only(repo) == 0

    @pytest.mark.timeout(240)
    def test_clean_windows_init_writes_lf_project_data_and_passes_check_only(
        self, repo: Path, force_hardlink_fallback: bool
    ) -> None:
        booley_dir = repo / ".booley"
        booley_dir.mkdir()
        # booley-setup Step 3 authors the canonical guidance; init links it.
        (booley_dir / "AGENTS.md").write_bytes(_GUIDANCE)

        assert _run_full_init(repo) == 0
        self._assert_safe_and_linked(repo, hardlink=force_hardlink_fallback)

        # Committing the scaffolds keeps them LF in the index and worktree.
        _git(booley_dir, "add", "-A")
        _git(booley_dir, *_IDENTITY, "commit", "-qm", "setup")
        eol = _git(booley_dir, "ls-files", "--eol")
        assert "booley.toml" in eol
        assert "i/crlf" not in eol
        self._assert_safe_and_linked(repo, hardlink=force_hardlink_fallback)

    # Windows CI measured 73.8s; 3x rounded up to 30s requires 240s.
    @pytest.mark.timeout(240)
    def test_crlf_project_data_behind_guidance_links_is_repaired(
        self, repo: Path, force_hardlink_fallback: bool, capsys: pytest.CaptureFixture[str]
    ) -> None:
        booley_dir = repo / ".booley"
        # 1. A Windows checkout with Git for Windows' default autocrlf=true:
        #    every tracked file in the Booley Directory lands with CRLF, index clean.
        _crlf_checkout_project_data(
            booley_dir,
            {"AGENTS.md": _GUIDANCE, "booley.toml": _AGENT_TABLE, "notes.md": b"# Notes\n"},
        )

        # 2. An uncommitted user edit makes the line-ending repair refuse, so
        #    init reports setup incomplete (#611) -- yet the guidance step still
        #    links the root guidance to the CRLF canonical file.
        with (booley_dir / "notes.md").open("ab") as notes:
            notes.write(b"work in progress\r\n")
        assert _run_full_init(repo) == 2
        assert "refusing to normalize" in capsys.readouterr().out
        canonical = booley_dir / "AGENTS.md"
        assert b"\r\n" in canonical.read_bytes()
        assert (repo / "AGENTS.md").samefile(canonical)
        if force_hardlink_fallback:
            assert not (repo / "AGENTS.md").is_symlink()
            assert canonical.stat().st_nlink == 3
        assert _check_only(repo) != 0

        # 3. The user commits the edit and re-runs init, as init instructs. The
        #    Booley-created hardlinks no longer block the repair (#609).
        _git(booley_dir, "add", "notes.md")
        _git(booley_dir, *_IDENTITY, "commit", "-qm", "notes")
        capsys.readouterr()
        assert _run_full_init(repo) == 0
        output = capsys.readouterr().out
        if force_hardlink_fallback:
            assert "released guidance hardlink AGENTS.md" in output
        assert _git(booley_dir, "config", "--local", "core.autocrlf").strip() == "false"
        self._assert_safe_and_linked(repo, hardlink=force_hardlink_fallback)

    def test_agent_selection_write_on_crlf_project_data_passes_check_only(
        self, repo: Path, force_hardlink_fallback: bool, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A tracked booley.toml without [agent]: init edits it, then repairs."""
        booley_dir = repo / ".booley"
        _crlf_checkout_project_data(
            booley_dir, {"AGENTS.md": _GUIDANCE, "booley.toml": _CONFIG_WITHOUT_AGENT}
        )

        assert _run_full_init(repo) == 0
        assert "refusing to normalize" not in capsys.readouterr().out
        assert (booley_dir / "booley.toml").read_bytes().endswith(_AGENT_TABLE)
        self._assert_safe_and_linked(repo, hardlink=force_hardlink_fallback)


@pytest.mark.parametrize("stale", ["none", "headless", "vscode"])
def test_image_convergence_reports_live_sandbox_drift(repo, monkeypatch, capsys, stale):
    image = pi.project_image_name(repo)
    changed = LifecycleResult(
        selected_reference=image,
        selected_id="sha256:new-image",
        status=ImageLifecycleStatus.CHANGED,
        changed_images=(image,),
    )
    name = {"none": None, "headless": sr.session_container_name(repo), "vscode": "vsc-editor"}[
        stale
    ]
    stale_sessions = [name] if name else []
    monkeypatch.setattr(init_cmd.image_lifecycle, "reconcile_planned", lambda *a, **k: changed)
    monkeypatch.setattr(sr, "sessions_on_stale_image", lambda root, selected: stale_sessions)

    # private: a full init with a CHANGED image also retires superseded images, which needs Docker.
    assert init_cmd._step_image_lifecycle(InitContext(project_root=repo)) is changed

    output = " ".join(capsys.readouterr().out.split())
    assert "session down" not in output
    if stale == "headless":
        assert f"'{name}'" in output
        assert "To restart it on the image just built, run `booley session refresh`" in output
    elif stale == "vscode":
        assert (
            "To restart it on the image just built, use Dev Containers: Rebuild Container."
            in output
        )
    else:
        assert "keeps serving it" not in output


def _complete_snapshot(
    root: Path, *, skip_git: bool = False
) -> dict[Path, tuple[bytes | None, int]]:
    return {
        path.relative_to(root): (
            path.read_bytes() if path.is_file() else None,
            stat.S_IMODE(path.stat().st_mode),
        )
        for path in [root, *root.rglob("*")]
        if not (skip_git and ".git" in path.relative_to(root).parts)
    }


def _set_project_mode(target: Path, mode: int) -> None:
    target.chmod(mode)
    if stat.S_IMODE(target.stat().st_mode) != mode:
        pytest.skip("filesystem cannot represent POSIX directory permissions")


def _stale_project(root: Path) -> Path:
    target = root / ".booley"
    target.mkdir()
    (target / ".gitignore").write_bytes(b"# custom ignore\ntmp/\n")
    return target


def _assert_backfill_report(output: str, missing: list[str]) -> None:
    assert "would add 3 config skeleton file(s)" in output
    assert (
        f"would add {len(missing)} missing ignore pattern(s) to .gitignore: {', '.join(missing)}"
        in output
    )
    assert "would add FUSESOC_IGNORE" in output


def _assert_applied_backfills(repo: Path, target: Path, missing: list[str]) -> None:
    from booley.runtime.project_gitignore import missing_gitignore_patterns

    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert (target / "booley.toml").read_bytes() == init_cmd.BOOLEY_TOML_SKELETON.encode()
    assert (target / "tests.toml").read_bytes() == init_cmd.TESTS_TOML_SKELETON.encode()
    assert (target / "metrics.toml").read_bytes() == init_cmd.METRICS_TOML_SKELETON.encode()
    assert (target / "FUSESOC_IGNORE").read_bytes() == init_cmd.FUSESOC_IGNORE_BODY.encode()
    ignore = (target / ".gitignore").read_text(encoding="utf-8")
    assert ignore.startswith("# custom ignore\ntmp/\n")
    assert missing_gitignore_patterns(ignore) == []
    assert set(ignore.splitlines()) - set("# custom ignore\ntmp/\n".splitlines()) == set(
        missing
    ) | {"", "# Added by booley init — transient Booley state."}
    # The skeleton records no Stealth choice, so no inner repository is created.
    assert not (target / ".git").exists()


def test_fresh_booley_dir_is_private_lf_and_valid(repo, monkeypatch, capsys):
    from tests.harness.test_measure_cli import _measure

    _initialize(repo)
    booley_dir = repo / ".booley"
    if os.name != "nt":
        assert stat.S_IMODE(booley_dir.stat().st_mode) == 0o700
    (booley_dir / ".gitignore").write_bytes(b"# user rules\n")  # Windows text mode: #609
    _initialize(repo)

    scaffolds = [path for path in booley_dir.iterdir() if path.is_file()]
    assert {".gitignore", "booley.toml", "FUSESOC_IGNORE"} <= {path.name for path in scaffolds}
    for path in scaffolds:
        assert b"\r\n" not in path.read_bytes(), path.name
    metrics = tomllib.loads((booley_dir / "metrics.toml").read_text(encoding="utf-8"))
    assert metrics == {
        "defaults": {"sim": {"tests_failed_max": 0}, "coverage": {"tests_failed_max": 0}}
    }
    capsys.readouterr()
    assert _measure(monkeypatch, repo, "--validate") == 0
    assert "declares no `[<target>.<flow>]` table" in capsys.readouterr().out


@pytest.mark.parametrize("mode", [0o755, 0o700])
def test_booley_dir_check_reports_backfills_then_converges(repo, capsys, mode):
    from booley.runtime.project_gitignore import missing_gitignore_patterns

    posix = os.name != "nt"
    if mode == 0o755 and not posix:
        pytest.skip("POSIX permission repair")
    target = _stale_project(repo)
    if posix:
        _set_project_mode(target, mode)
    missing = missing_gitignore_patterns((target / ".gitignore").read_text())
    before = _complete_snapshot(repo)

    results = _initialize(repo, check_only=True)

    output = capsys.readouterr().out
    _assert_backfill_report(output, missing)
    records = [r for r in results if r.name == "booley_dir"]
    assert records and all(r.status == "warn" for r in records)
    assert ("would secure Booley Directory permissions" in output) is (mode == 0o755)
    assert any(r.detail == "permissions need 0700" for r in records) is (mode == 0o755)
    for skeleton in ("tests.toml", "metrics.toml"):
        assert any(skeleton in r.detail for r in records)
    assert _complete_snapshot(repo) == before
    _initialize(repo)
    _assert_applied_backfills(repo, target, missing)
    capsys.readouterr()
    again = _initialize(repo, check_only=True)
    assert "would" not in capsys.readouterr().out
    assert [(r.status, r.detail) for r in again if r.name == "booley_dir"] == [
        ("current", "already present")
    ]


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory search permissions")
def test_booley_dir_check_inaccessible_reports_incomplete(repo, capsys):
    target = _stale_project(repo)
    for name in ("booley.toml", "tests.toml", "FUSESOC_IGNORE"):
        (target / name).write_bytes(b"# custom\n")
    before = _complete_snapshot(repo)
    _set_project_mode(target, 0o600)
    try:
        if os.access(target, os.R_OK | os.X_OK):
            pytest.skip("privileges bypass directory search permissions")
        # private: the full run's core-projection step stats `.booley/cores` and raises
        # PermissionError on Python 3.11 when the directory is unsearchable; only this step
        # reports the incomplete backfill.
        ctx = InitContext(project_root=repo, check_only=True)
        init_cmd._step_booley_dir(ctx)
        output = capsys.readouterr().out
        assert "would secure Booley Directory permissions" in output
        assert "backfill report is incomplete" in output
        assert "cannot inspect" in output
        assert "would add" not in output
        assert len(ctx.results) == 1 and ctx.results[0].status == "warn"
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    finally:
        target.chmod(0o700)
    before[Path(".booley")] = (None, 0o700)
    assert _complete_snapshot(repo) == before


@pytest.mark.timeout(120)
def test_booley_dir_check_full_init_reports_backfills_without_mutation(repo, capsys):
    from booley.runtime.project_gitignore import missing_gitignore_patterns

    posix = os.name != "nt"
    assert _run_full_init(repo) == 0
    target = repo / ".booley"
    (target / "tests.toml").unlink()
    (target / "FUSESOC_IGNORE").unlink()
    (target / ".gitignore").write_bytes(b"# custom ignore\ntmp/\n")
    if posix:
        _set_project_mode(target, 0o755)
    missing = missing_gitignore_patterns((target / ".gitignore").read_text())
    before = _complete_snapshot(repo)
    capsys.readouterr()
    assert _check_only(repo) == 1
    output = capsys.readouterr().out
    assert "would add 1 config skeleton file(s)" in output
    assert "would add FUSESOC_IGNORE" in output
    assert f"would add {len(missing)} missing ignore pattern(s)" in output
    assert ("would secure Booley Directory permissions" in output) is posix
    assert "tests.toml" in output.splitlines()[-1]  # the verdict line names the pending backfill
    assert _complete_snapshot(repo) == before
    assert _run_full_init(repo) == 0
    after = _complete_snapshot(repo)
    changed = {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}
    assert changed == {
        *([Path(".booley")] if posix else []),
        Path(".booley/tests.toml"),
        Path(".booley/FUSESOC_IGNORE"),
        Path(".booley/.gitignore"),
    }
    assert (target / "tests.toml").read_bytes() == init_cmd.TESTS_TOML_SKELETON.encode()
    assert (target / "FUSESOC_IGNORE").read_bytes() == init_cmd.FUSESOC_IGNORE_BODY.encode()
    assert missing_gitignore_patterns((target / ".gitignore").read_text()) == []
    capsys.readouterr()
    before = _complete_snapshot(repo)
    assert _check_only(repo) == 0
    assert "would" not in capsys.readouterr().out
    assert _complete_snapshot(repo) == before


def _managed_git(root: Path, *args: str) -> str:
    return run_bounded_child(["git", "-C", str(root), *args], check=True).stdout


def test_initial_authored_scaffolding_remains_uncommitted(repo):
    before = _managed_git(repo, "ls-files", "--stage", "-z")
    assert _run_full_init(repo) == 0
    data = repo / ".booley"
    assert (data / "booley.toml").is_file()
    assert _managed_git(repo, "ls-files", "--stage", "-z") == before
    assert not _managed_git(data, "ls-files", "--stage", "-z")
    assert "booley.toml" in _managed_git(data, "status", "--porcelain", "--untracked-files=all")


# Windows CI measured 36.0s; 120s preserves the required timeout headroom.
@pytest.mark.timeout(120)
@pytest.mark.parametrize("topology", ["standalone", "nested"])
def test_managed_bundle_publication_leaves_versioned_project_clean(repo, topology):
    if topology == "nested":
        initial = repo / ".booley"
        initial.mkdir(mode=0o700)
        (initial / "booley.toml").write_text("[stealth]\nenabled=false\n")
    assert _run_full_init(repo) == 0
    data = repo / ".booley"
    owner = data if topology == "standalone" else repo
    for repository in {repo, owner}:
        _managed_git(repository, "config", "user.name", "Fixture")
        _managed_git(repository, "config", "user.email", "fixture@example.invalid")
    # The user's authored baseline is committed between first scaffolding and
    # runtime initialization. The generated bundle is never included in it.
    bundle = data / ".managed/project-git-hooks.pyz"
    bundle.unlink()
    _managed_git(owner, "add", ".")
    _managed_git(owner, "commit", "-qm", "prepared authored Project")
    if topology == "standalone":
        (repo / "rtl.v").write_text("module top; endmodule\n")
        _managed_git(repo, "add", ".")
        _managed_git(repo, "commit", "-qm", "prepared RTL")
    index = _managed_git(owner, "ls-files", "--stage", "-z")
    assert _run_full_init(repo) == 0
    assert bundle.is_file()
    assert _managed_git(data, "check-ignore", "--no-index", str(bundle))
    assert _managed_git(owner, "status", "--porcelain", "--untracked-files=all") == ""
    assert _managed_git(owner, "ls-files", "--stage", "-z") == index


def test_managed_bundle_ignore_backfill_and_check(repo, capsys):
    _write_booley_toml(repo, stealth=True)  # inner repo
    assert _run_full_init(repo) == 0
    data = repo / ".booley"
    ignore = data / ".gitignore"
    stale = ignore.read_text().replace(".managed/\n", "") + "# custom policy\ncustom/\n"
    ignore.write_text(stale)
    _managed_git(data, "config", "user.name", "Fixture")
    _managed_git(data, "config", "user.email", "fixture@example.invalid")
    _managed_git(data, "add", ".gitignore")
    _managed_git(data, "commit", "-qm", "stale authored policy")
    before = _managed_git(data, "ls-files", "--stage", "-z")
    capsys.readouterr()
    _initialize(repo, check_only=True)
    assert (
        "would add 1 missing ignore pattern(s) to .gitignore: .managed/" in capsys.readouterr().out
    )
    assert ignore.read_text() == stale
    assert _managed_git(data, "ls-files", "--stage", "-z") == before
    _initialize(repo)
    repaired = ignore.read_text()
    assert repaired.startswith(stale)
    assert repaired.count(".managed/\n") == 1
    assert ".managed" not in _managed_git(data, "status", "--porcelain", "--untracked-files=all")
    assert " M .gitignore" in _managed_git(data, "status", "--porcelain")
    _initialize(repo)
    assert ignore.read_text() == repaired
    assert _managed_git(data, "ls-files", "--stage", "-z") == before


def test_seed_relocated_booley_dir_passes_pdk_root(repo, monkeypatch):
    """``--seed`` follows ``[project].dir``: a relocated directory still counts as seeded."""
    (repo / "booley.toml").write_text('[project]\ndir = "state"\n', encoding="utf-8")
    (repo / "state").mkdir()
    seen: list[dict] = []
    # private: `_step_sandbox` is where the PDK root arrives; its only public effect needs Docker.
    monkeypatch.setattr(init_cmd, "_step_sandbox", lambda _ctx, **kwargs: seen.append(kwargs))
    monkeypatch.setattr(init_cmd.nangate_pdk, "is_ready", lambda: True)
    args = _init_args()
    args.seed = True

    init_cmd.run_init(args, repo)

    assert seen[0]["nangate_pdk_root"] == repo.parent / "pdk"


def _scaffold_args():
    args = _init_args()
    args.scaffold = "starter"
    args.sim_eda_tool = "verilator"
    args.lint_eda_tool = "verilator"
    args.tb_style = "sv"
    args.asic = False
    args.fpga_part = None
    return args


def test_scaffold_origin_survives_plain_rerun(repo, monkeypatch, capsys):
    assert init_cmd.run_init(_scaffold_args(), repo) == 0
    config = repo / ".booley/booley.toml"
    assert "# booley-init-origin: scaffold" in config.read_text(), (
        "scaffold rerun must use greenfield guidance"
    )
    assert "/booley-setup new" in capsys.readouterr().out
    for check in (False, True):
        args = _init_args()
        args.check_only = check
        assert init_cmd.run_init(args, repo) == 0
        output = capsys.readouterr().out
        assert "plan phase" not in output
        assert "Step 2 (fpga)" not in output
        assert "doctor" in output
        assert "AGENTS.md is" not in output
        assert "# booley-init-origin: scaffold" in config.read_text()


def test_check_only_scaffold_reports_prospective_greenfield_route(repo, monkeypatch, capsys):
    args = _scaffold_args()
    args.check_only = True
    before = _filesystem_snapshot(repo)

    assert init_cmd.run_init(args, repo) == 1

    output = capsys.readouterr().out
    assert "After applying the scaffold" in output, "planned scaffold needs prospective advice"
    assert "/booley-setup new` in your agent chat here on the host" in output
    advice = output[output.index("After applying the scaffold") :]
    assert advice.index("/booley-setup new") < advice.index("booley session up")
    assert "Reopen in Container" not in output
    assert "Scaffolded starter project" not in output
    assert "plan phase, here on the host" not in output
    assert "Checked initialization: pending or unresolved" in output.splitlines()[-1]
    assert _filesystem_snapshot(repo) == before
    assert not (repo / ".booley/booley.toml").exists()


def test_check_only_early_failures_end_with_matching_verdict(repo, monkeypatch, capsys):
    args = _init_args()
    args.check_only = True

    def fail(*_args):
        raise OSError("unique-inspection-failure")

    # private: an OSError from layout inspection has no public trigger.
    monkeypatch.setattr(init_cmd, "_check_init_layout", fail)
    assert init_cmd.run_init(args, repo) == 2
    output = capsys.readouterr().out
    assert "Checked initialization: failed" in output.splitlines()[-1], (
        "early check error must end with checked verdict"
    )
    assert "unique-inspection-failure" in output.splitlines()[-1]


def test_comment_only_port_skeleton_then_scaffold_updates_creation_origin(repo, monkeypatch):
    config = repo / ".booley/booley.toml"
    config.parent.mkdir(mode=0o700)
    config.write_text(
        "# booley-init-origin: port\n" + init_cmd.BOOLEY_TOML_SKELETON, encoding="utf-8"
    )
    assert init_cmd.run_init(_scaffold_args(), repo) == 0
    assert "# booley-init-origin: scaffold" in config.read_text(encoding="utf-8")
    assert "# booley-init-origin: port" not in config.read_text(encoding="utf-8")


def test_existing_directory_config_backfill_never_invents_port_origin(repo, monkeypatch):
    (repo / ".booley").mkdir(mode=0o700)
    assert _run_full_init(repo) == 0
    assert "booley-init-origin:" not in (repo / ".booley/booley.toml").read_text()


def test_legacy_init_modes_never_backfill_origin(repo, monkeypatch, capsys):
    monkeypatch.setattr(init_cmd.nangate_pdk, "is_ready", lambda: True)
    assert _run_full_init(repo) == 0
    config = repo / ".booley/booley.toml"
    original = config.read_text(encoding="utf-8").replace("# booley-init-origin: port\n", "")
    original = (
        "# booley.toml — generated by `booley init --scaffold` for the 'starter' starter IP.\n"
        + original
    )
    config.write_text(original, encoding="utf-8")
    for mode in ("plain", "force", "check_only", "seed"):
        args = _init_args()
        if mode != "plain":
            setattr(args, mode, True)
        assert init_cmd.run_init(args, repo) == 0
        assert config.read_text(encoding="utf-8") == original
        # A legacy header without the origin marker never gets the scaffold send-off.
        output = capsys.readouterr().out
        assert "Scaffolded starter project" not in output
        assert "if it ran this scaffold" not in output


def _git(root: Path, *args: str) -> str:
    """Run one Git command with a fixed identity and no detached auto-maintenance.

    ``git commit`` may spawn ``maintenance run --auto`` in the background; it
    creates and removes ``.git/objects/maintenance.lock`` at an arbitrary time,
    which flakes any test that snapshots the tree around a later call.
    """
    result = run_bounded_child(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "maintenance.auto=false",
            "-c",
            "gc.auto=0",
            *args,
        ],
        cwd=root,
        check=True,
    )
    return result.stdout


def _plain_init_with_legacy_hidden_layout(repo: Path) -> None:
    """A completed plain init carrying an older Booley's hidden layout.

    Plain init no longer hides `.booley/` (Stealth is off without an explicit
    key), but a Project initialized before that change still has the empty inner
    repository and the `/.booley` exclusion. Recreate that footprint by hand.
    """
    assert _run_full_init(repo) == 0
    assert not (repo / ".booley/.git").exists(), "plain init must not create an inner repo"
    _git(repo / ".booley", "init", "-q")
    add_git_excludes(repo, [".booley"])
    assert "/.booley" in _exclude_lines(repo)


def _exclude_lines(repo: Path) -> list[str]:
    return (repo / ".git/info/exclude").read_text(encoding="utf-8").splitlines()


def _after_init_scaffold_args(**overrides):
    """Scaffold args as `/booley-setup new` passes them: no provider or auth."""
    args = _scaffold_args()
    args.provider = None
    args.auth = None
    for name, value in overrides.items():
        setattr(args, name, value)
    return args


def _never_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the run interactive and fail the test on any prompt."""

    def no_prompt(*_args, **_kwargs):
        raise AssertionError("scaffold after plain init prompted the user")

    use_terminal_stdin(monkeypatch)
    monkeypatch.setattr("builtins.input", no_prompt)


def test_scaffold_after_plain_init_opens_hidden_layout(repo, monkeypatch, capsys):
    from booley.harness import doctor

    _plain_init_with_legacy_hidden_layout(repo)
    config = repo / ".booley/booley.toml"
    assert "[agent]" in config.read_text(encoding="utf-8")
    _never_prompt(monkeypatch)
    capsys.readouterr()

    # A check-only pass reports the removal and changes nothing.
    before = _filesystem_snapshot(repo)
    check = _after_init_scaffold_args(check_only=True)
    assert init_cmd.run_init(check, repo) == 1
    output = capsys.readouterr().out
    assert "would remove empty inner git repository" in output
    assert "would remove /.booley from git info/exclude" in output
    assert _filesystem_snapshot(repo) == before
    assert (repo / ".booley/.git").is_dir()

    reset_cache()
    assert init_cmd.run_init(_after_init_scaffold_args(), repo) == 0

    text = config.read_text(encoding="utf-8")
    parsed = tomllib.loads(text)
    assert parsed["agent"] == {"default": "claude", "auth": "subscription"}, "[agent] kept"
    assert parsed["stealth"]["enabled"] is False
    assert "# booley-init-origin: scaffold" in text
    assert not (repo / ".booley/.git").exists(), "no inner repository for an open scaffold"
    assert "/.booley" not in _exclude_lines(repo)
    assert "?? .booley/" in _git(repo, "status", "--porcelain"), "`.booley/` must be trackable"
    assert (repo / "rtl/starter.sv").is_file()

    # The result passes the same Doctor core audit as a fresh scaffold.
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "scaffold")
    project = doctor.ProjectAudit(repo, repo / ".booley", parsed, "sim")
    reporter = doctor._Reporter.create()
    # private: the reporter is the audit's collector, and the public `doctor.run_doctor_result`
    # also runs host and Docker probes.
    doctor._run_core_audit(
        project,
        reporter.pass_,
        reporter.warn_,
        reporter.skip_,
        reporter.fail_,
        _diagnostics=reporter.diagnostics,
        _note=reporter.note_,
    )
    assert not [
        (f.check_id, f.subject, f.message)
        for f in reporter.result(0).findings
        if f.severity in {"warn", "fail"}
    ]


def test_stealth_scaffold_after_plain_init_keeps_hidden_layout(repo, monkeypatch):
    _plain_init_with_legacy_hidden_layout(repo)
    _never_prompt(monkeypatch)

    reset_cache()
    assert init_cmd.run_init(_after_init_scaffold_args(stealth=True), repo) == 0

    parsed = tomllib.loads((repo / ".booley/booley.toml").read_text(encoding="utf-8"))
    assert parsed["agent"] == {"default": "claude", "auth": "subscription"}
    assert parsed["stealth"]["enabled"] is True
    assert (repo / ".booley/.git").is_dir(), "Stealth keeps the inner repository"
    assert "/.booley" in _exclude_lines(repo)
    assert (repo / ".booley/cores/starter.core").is_file()


def test_scaffold_refuses_inner_repo_with_commits(repo, monkeypatch, capsys):
    _plain_init_with_legacy_hidden_layout(repo)
    inner = repo / ".booley"
    _git(inner, "add", "booley.toml")
    _git(inner, "commit", "-qm", "history")
    capsys.readouterr()
    before = _filesystem_snapshot(repo)

    assert init_cmd.run_init(_after_init_scaffold_args(), repo) == 2

    output = capsys.readouterr().out
    assert "own git repository with history" in output
    assert "--stealth" in output, "the refusal says how to resolve it"
    assert _filesystem_snapshot(repo) == before, "a refused scaffold changes nothing"
    assert "/.booley" in _exclude_lines(repo)


def test_scaffold_refuses_symlinked_inner_git(repo, monkeypatch, capsys, tmp_path):
    """A symlinked `.booley/.git` cannot be removed with rmtree: refuse before any change."""
    _plain_init_with_legacy_hidden_layout(repo)
    real_git = tmp_path / "elsewhere.git"
    (repo / ".booley/.git").rename(real_git)
    symlink_or_skip(repo / ".booley/.git", real_git)
    capsys.readouterr()
    before = _filesystem_snapshot(repo)

    assert init_cmd.run_init(_after_init_scaffold_args(), repo) == 2

    assert "own git repository with history" in capsys.readouterr().out
    assert _filesystem_snapshot(repo) == before
    assert "/.booley" in _exclude_lines(repo)


def test_scaffold_removal_failure_keeps_the_exclusion(repo, monkeypatch, capsys):
    """If the inner repository cannot be removed, the exclusion stays and the run stops."""
    _plain_init_with_legacy_hidden_layout(repo)

    def failing_rmtree(path, *args, **kwargs):
        raise OSError("simulated rmtree failure")

    monkeypatch.setattr("booley.harness.setup.scaffold.shutil.rmtree", failing_rmtree)
    capsys.readouterr()

    assert init_cmd.run_init(_after_init_scaffold_args(), repo) == 2

    assert "could not remove the hidden layout" in capsys.readouterr().out
    assert "/.booley" in _exclude_lines(repo)
    assert (repo / ".booley/.git").is_dir()


def test_scaffold_leaves_symlinked_booley_footprint(repo, monkeypatch, capsys, tmp_path):
    """A symlinked `.booley` with a hidden footprint is left alone with a warning."""
    _plain_init_with_legacy_hidden_layout(repo)
    real = tmp_path / "pinned"
    (repo / ".booley").rename(real)
    symlink_or_skip(repo / ".booley", real)
    capsys.readouterr()

    init_cmd.run_init(_after_init_scaffold_args(), repo)

    assert "is a symlink; leaving its hidden layout as is" in capsys.readouterr().out
    assert (real / ".git").is_dir()
    assert "/.booley" in _exclude_lines(repo)


def test_scaffold_ownership_conflict_keeps_hidden_layout(repo, monkeypatch, capsys):
    """Refusals precede the destructive step: a blocked write leaves the footprint."""
    _plain_init_with_legacy_hidden_layout(repo)
    constraints = repo / "constraints"
    constraints.mkdir()
    sdc = constraints / "starter.sdc"
    sdc.write_text("# user-owned\n", encoding="utf-8")
    _git(repo, "add", "constraints/starter.sdc")
    capsys.readouterr()

    args = _after_init_scaffold_args(asic=True)
    assert init_cmd.run_init(args, repo) == 2

    assert "user-owned path" in capsys.readouterr().out
    assert (repo / ".booley/.git").is_dir()
    assert "/.booley" in _exclude_lines(repo)
    assert sdc.read_text(encoding="utf-8") == "# user-owned\n"


def test_scaffold_after_init_refuses_rtl_and_other_config(repo, monkeypatch):
    assert _run_full_init(repo) == 0
    config = repo / ".booley/booley.toml"

    # The generated config holds non-ASCII comments: write it back as UTF-8
    # (Windows defaults to cp1252) and keep "\n" line endings.
    config.write_text(
        config.read_text(encoding="utf-8") + '\n[project]\nname = "x"\n',
        encoding="utf-8",
        newline="\n",
    )
    authored = config.read_bytes()
    assert init_cmd.run_init(_after_init_scaffold_args(), repo) == 2
    assert config.read_bytes() == authored

    config.write_bytes(authored.split(b"\n[project]")[0])
    (repo / "top.sv").write_text("module top; endmodule\n", encoding="utf-8")
    assert init_cmd.run_init(_after_init_scaffold_args(), repo) == 2
    assert not (repo / "tb").exists()


def test_init_origin_ignores_external_booley_dir(repo, monkeypatch):
    external = repo.parent / "external"
    external.mkdir()
    config = external / "booley.toml"
    config.write_text("# external\n")
    monkeypatch.setenv("BOOLEY_DIR", str(external))
    assert _run_full_init(repo) == 0
    assert "# booley-init-origin: port" in (repo / ".booley/booley.toml").read_text()
    assert config.read_text() == "# external\n"
    assert os.environ["BOOLEY_DIR"] == str(external)


@pytest.mark.parametrize(
    "mode",
    [
        "scaffold",
        "plain",
        "force",
        "check",
        "seed",
        "seed+force",
        "seed+check",
        "seed+force+check",
        "bare-seed",
    ],
)
def test_init_mode_headings_are_sequential(repo, monkeypatch, capsys, mode):
    """F-2: step labels are 1..n with no gaps and no `9b`-style suffixes, in every mode."""
    flags = set(mode.split("+"))
    if "seed" in flags or mode in {"force", "check"}:
        assert _run_full_init(repo) == 0  # these modes act on an initialized Project
    monkeypatch.setattr(init_cmd.nangate_pdk, "is_ready", lambda: True)
    intents = []
    real = init_cmd.image_lifecycle.reconcile_planned
    monkeypatch.setattr(
        init_cmd.image_lifecycle,
        "reconcile_planned",
        lambda scope, intent, **kwargs: intents.append(intent) or real(scope, intent, **kwargs),
    )
    args = _scaffold_args() if mode == "scaffold" else _init_args()
    args.seed = bool(flags & {"seed", "bare-seed"})  # bare-seed: no Booley Directory yet
    args.force = "force" in flags
    args.check_only = "check" in flags
    capsys.readouterr()

    assert init_cmd.run_init(args, repo) == (2 if mode == "bare-seed" else 0)

    labels = re.findall(r"^Step (\S+) —", capsys.readouterr().out, re.MULTILINE)
    assert labels == [str(n) for n in range(1, len(labels) + 1)] and labels
    assert set(intents) <= {init_cmd.ImageLifecycleIntent.CHECK} or not args.seed  # #1564
    assert intents or "seed" not in flags


@pytest.mark.parametrize("name", [".gitignore", "FUSESOC_IGNORE"])
def test_symlinked_booley_dir_file_is_refused(repo, capsys, name):
    booley_dir = repo / ".booley"
    booley_dir.mkdir()
    outside = repo.parent / "outside"
    outside.write_bytes(b"# user rules\n")
    symlink_or_skip(booley_dir / name, outside)
    planted_target = (booley_dir / name).readlink()

    results = _initialize(repo)

    assert outside.read_bytes() == b"# user rules\n"
    assert (booley_dir / name).readlink() == planted_target
    assert ("booley_dir", "warn") in [(r.name, r.status) for r in results]
    assert "refusing symlinked" in capsys.readouterr().out


def test_core_projection_step_excludes_relocated_booley_dir_in_stealth(tmp_path, monkeypatch):
    """Stealth init hides both `.booley` and the relocated Booley Directory."""
    root = tmp_path / "proj"
    root.mkdir()
    run_bounded_child(["git", "init", "-q", str(root)], check=True)
    (root / "booley.toml").write_bytes(b'[project]\ndir = "tools/booley"\n')
    project = root / "tools" / "booley"
    project.mkdir(parents=True)
    (project / "booley.toml").write_bytes(b"[stealth]\nenabled = true\n")
    # Core projection still reads the default `.booley` entry, which full init creates.
    (root / ".booley").mkdir()
    (root / ".booley/booley.toml").write_bytes(b"[stealth]\nenabled = true\n")
    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    reset_cache()

    # private: `initialize_project` also adds these exclusions from later steps, which would
    # mask this step dropping them.
    init_cmd._step_core_projections(InitContext(project_root=root))

    exclude = (root / ".git/info/exclude").read_text(encoding="utf-8").splitlines()
    assert "/.booley" in exclude
    assert "/tools/booley" in exclude
    reset_cache()


EXPECTED_FRESH_INIT = (
    [
        "Host Bootstrap",
        "Booley Directory",
        "agent provider and authentication policy",
        "stealth core projections",
        "Sandbox Image lifecycle",
        "project Git-hook bundle",
        "worktree prune guard",
        "worktree link policy",
        "line endings",
        "guidance links",
        "Sandbox",
        "Project Inventory",
        "post-setup advisories",
    ],
    [
        ("booley_dir", "current"),
        ("agent_config", "ok"),
        ("core_projections", "ok"),
        ("auth", "skip"),
        ("docker_image", "current"),
        ("git_hooks", "skip"),
        ("project_git_hooks", "ok"),
        ("worktree_prune_guard", "ok"),
        ("worktree_link_policy", "warn"),
        ("line_endings", "ok"),
        ("guidance_links", "ok"),
        ("interactive", "ok"),
        ("project_inventory", "ok"),
        ("advisories", "ok"),
    ],
)

EXPECTED_RERUN_INIT = (
    [
        "Host Bootstrap",
        "Booley Directory",
        "agent provider and authentication policy",
        "stealth core projections",
        "Sandbox Image lifecycle",
        "project Git-hook bundle",
        "worktree prune guard",
        "worktree link policy",
        "line endings",
        "guidance links",
        "Sandbox",
        "Project Inventory",
        "post-setup advisories",
    ],
    [
        ("booley_dir", "current"),
        ("agent_config", "skip"),
        ("core_projections", "ok"),
        ("auth", "skip"),
        ("docker_image", "current"),
        ("git_hooks", "skip"),
        ("project_git_hooks", "current"),
        ("worktree_prune_guard", "current"),
        ("worktree_link_policy", "current"),
        ("line_endings", "ok"),
        ("guidance_links", "ok"),
        ("interactive", "current"),
        ("project_inventory", "ok"),
        ("advisories", "ok"),
    ],
)


def _init_banners_and_results(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> tuple[list[str], list[tuple[str, str]]]:
    """Run full init once; return its step banner titles and ``(record, status)`` pairs."""
    summaries: list[InitContext] = []
    # private: the summary prints status as a glyph, so record statuses come from its context.
    real_summary = init_cmd._print_summary

    def capture_summary(ctx: InitContext) -> int:
        summaries.append(ctx)
        return real_summary(ctx)

    monkeypatch.setattr(init_cmd, "_print_summary", capture_summary)
    assert _run_full_init(repo) == 0
    banners = re.findall(r"^Step \d+ — (.*)$", capsys.readouterr().out, re.MULTILINE)
    (ctx,) = summaries
    return banners, [(result.name, result.status) for result in ctx.results]


def test_full_init_output_order_is_unchanged(repo, monkeypatch, capsys):
    """Host init keeps its public step order and summary records (ADR 0085 refactor guard).

    The literal sequences were captured from ``main`` before Project Initialization was
    extracted; a step that is dropped, duplicated or reordered changes them.
    """
    (repo / ".booley").mkdir()
    (repo / ".booley" / "AGENTS.md").write_text("# Project guide\n", encoding="utf-8")
    # The captured sequences are a Stealth Project's (no Project trackability step).
    (repo / ".booley" / "booley.toml").write_text("[stealth]\nenabled = true\n")

    fresh = _init_banners_and_results(repo, monkeypatch, capsys)
    rerun = _init_banners_and_results(repo, monkeypatch, capsys)

    assert fresh == EXPECTED_FRESH_INIT
    assert rerun == EXPECTED_RERUN_INIT
