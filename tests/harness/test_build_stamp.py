"""The wheel's baked-in build commit (F-3).

Only ``build.sh`` used to stamp ``src/booley/_build_commit.py``, so every
init-driven image build baked a wheel whose in-container ``booley --version``
was a bare ``booley 0.1.0`` — and the freshness check the setup docs prescribe
("confirm the wheel matches the commit") was unanswerable exactly where it
matters. These pin the one shared helper and the two callers that use it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from booley.harness import init_docker_image
from booley.harness.build_stamp import (
    STAMP_RELPATH,
    build_stamp,
    iter_payload_files,
    resolve_build_commit,
    resolve_payload_fingerprint,
    resolve_source_updated_at,
    stamp_path,
    write_build_stamp,
)
from booley.harness.init_common import InitContext

BUILD_SH = Path(__file__).resolve().parents[2] / "src" / "booley" / "data" / "docker" / "build.sh"
WHEEL_NAME = "booley_rtl-0.2.3-py3-none-any.whl"


def _write_wheel(root: Path, name: str = WHEEL_NAME) -> Path:
    wheel = root / "dist" / name
    wheel.parent.mkdir(parents=True, exist_ok=True)
    wheel.write_bytes(b"wheel")
    return wheel


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway git checkout with one committed file."""
    root = tmp_path / "repo"
    (root / "src" / "booley").mkdir(parents=True)
    _git(root.parent, "init", root.name)
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "README").write_text("hi\n", encoding="utf-8")
    (root / "src" / "booley" / "payload.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "add", "README", "src/booley/payload.py")
    _git(root, "commit", "-m", "init")
    return root


class TestResolveBuildCommit:
    def test_reports_short_head(self, repo: Path):
        head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        assert resolve_build_commit(repo) == head

    def test_dirty_tree_is_marked(self, repo: Path):
        (repo / "README").write_text("changed\n", encoding="utf-8")

        assert resolve_build_commit(repo).endswith("+dirty")

    def test_non_git_tree_yields_empty(self, tmp_path: Path):
        """A source tarball is not a failure — there is simply no commit."""
        assert resolve_build_commit(tmp_path) == ""

    def test_reports_head_update_time(self, repo: Path):
        assert (
            resolve_source_updated_at(repo)
            == subprocess.run(
                ["git", "-C", str(repo), "log", "-1", "--format=%cI", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        )


class TestWriteBuildStamp:
    def test_writes_an_importable_module(self, repo: Path):
        commit = write_build_stamp(repo)
        text = stamp_path(repo).read_text(encoding="utf-8")

        assert stamp_path(repo) == repo / STAMP_RELPATH
        namespace: dict = {}
        exec(compile(text, "_build_commit.py", "exec"), namespace)
        assert namespace["COMMIT"] == commit != ""
        assert namespace["PAYLOAD_FINGERPRINT"] == resolve_payload_fingerprint(repo)

    def test_payload_fingerprint_changes_for_dirty_source_at_same_head(self, repo: Path):
        before = resolve_payload_fingerprint(repo)
        head = resolve_build_commit(repo)

        (repo / "src" / "booley" / "payload.py").write_text("VALUE = 2\n", encoding="utf-8")

        assert resolve_payload_fingerprint(repo) != before
        assert resolve_build_commit(repo).removesuffix("+dirty") == head

    def test_context_manager_always_removes_the_stamp(self, repo: Path):
        """Leaving it behind makes the checkout claim a commit it doesn't have."""
        with pytest.raises(RuntimeError), build_stamp(repo):
            assert stamp_path(repo).is_file()
            raise RuntimeError("build blew up")

        assert not stamp_path(repo).exists()


class TestInitStampsItsWheel:
    def test_wheel_build_runs_with_the_stamp_in_place(self, repo: Path, monkeypatch):
        """The F-3 regression: init's `python -m build` must see the stamp."""
        real_run = subprocess.run
        seen: list[bool] = []

        def _fake_run(cmd, **kwargs):
            # git still has to run for real — the stamp's content comes from it.
            if cmd[0] == "git":
                return real_run(cmd, **kwargs)
            seen.append(stamp_path(repo).is_file())
            _write_wheel(repo)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(subprocess, "run", _fake_run)

        assert init_docker_image._docker_build_wheel(InitContext(), repo) is True
        assert seen == [True]
        assert not stamp_path(repo).exists()

    def test_wheel_build_removes_stale_staging_tree(self, repo: Path, monkeypatch):
        stale_module = repo / "build" / "lib" / "booley" / "tools" / "legacy.py"
        stale_module.parent.mkdir(parents=True)
        stale_module.write_text("stale = True\n", encoding="utf-8")
        real_run = subprocess.run

        def _fake_run(cmd, **kwargs):
            if cmd[0] == "git":
                return real_run(cmd, **kwargs)
            assert not stale_module.exists()
            _write_wheel(repo)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(subprocess, "run", _fake_run)

        assert init_docker_image._docker_build_wheel(InitContext(), repo) is True

    def test_wheel_build_replaces_older_distribution_wheels(self, repo: Path, monkeypatch):
        old_wheel = _write_wheel(repo, "booley_rtl-0.1.0-py3-none-any.whl")
        real_run = subprocess.run

        def _fake_run(cmd, **kwargs):
            if cmd[0] == "git":
                return real_run(cmd, **kwargs)
            assert not old_wheel.exists()
            _write_wheel(repo)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(subprocess, "run", _fake_run)

        assert init_docker_image._docker_build_wheel(InitContext(), repo) is True
        assert [path.name for path in (repo / "dist").glob("booley_rtl-*.whl")] == [WHEEL_NAME]

    @pytest.mark.parametrize(
        "outputs",
        [[], [WHEEL_NAME, "booley_rtl-0.2.4-py3-none-any.whl"]],
        ids=["missing", "multiple"],
    )
    def test_wheel_build_requires_exactly_one_output(
        self, repo: Path, monkeypatch, outputs: list[str]
    ):
        real_run = subprocess.run

        def _fake_run(cmd, **kwargs):
            if cmd[0] == "git":
                return real_run(cmd, **kwargs)
            for name in outputs:
                _write_wheel(repo, name)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(subprocess, "run", _fake_run)
        ctx = InitContext()

        assert init_docker_image._docker_build_wheel(ctx, repo) is False
        assert ctx.results[-1].status == "err"

    def test_failed_wheel_build_still_cleans_up_and_reports(self, repo: Path, monkeypatch):
        def _boom(cmd, **kwargs):
            raise subprocess.CalledProcessError(1, cmd, stderr="No module named build")

        monkeypatch.setattr(subprocess, "run", _boom)
        ctx = InitContext()

        assert init_docker_image._docker_build_wheel(ctx, repo) is False
        assert not stamp_path(repo).exists()
        assert ctx.results[-1].status == "err"

    def test_build_sh_delegates_instead_of_hand_rolling_git(self):
        """The duplicate rule in bash is what drifted from init in the first place."""
        text = BUILD_SH.read_text(encoding="utf-8")

        assert "write_build_stamp" in text
        assert "rev-parse --short HEAD" not in text

    def test_build_sh_removes_old_distribution_wheels_before_build(self):
        text = BUILD_SH.read_text(encoding="utf-8")
        cleanup = 'rm -f "$BOOLEY_ROOT"/dist/booley_rtl-*.whl'
        build = '"$PYBUILD" -P -m build --wheel --outdir dist/'

        assert cleanup in text
        assert text.index(cleanup) < text.index(build)


class TestStampIsNotFingerprinted:
    def test_stamp_never_reaches_the_image_fingerprint(self, tmp_path: Path):
        """build.sh and init drop the stamp at different points, so hashing it
        would make their fingerprints disagree — every init after a build.sh
        build would call the image stale and rebuild it for 20 minutes."""
        root = tmp_path / "root"
        (root / "src" / "booley").mkdir(parents=True)
        (root / "src" / "booley" / "real.py").write_text("x = 1\n", encoding="utf-8")

        before = init_docker_image._image_build_fingerprint(root)
        write_build_stamp(root)

        assert init_docker_image._image_build_fingerprint(root) == before
        assert STAMP_RELPATH not in [
            p.relative_to(root).as_posix() for p in iter_payload_files(root)
        ]
