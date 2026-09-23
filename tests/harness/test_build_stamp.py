"""The wheel's baked-in build commit (F-3).

Only ``build.sh`` used to stamp ``src/booley/_build_commit.py``, so every
init-driven image build baked a wheel whose in-container ``booley --version``
was a bare ``booley 0.1.0`` — and the freshness check the setup docs prescribe
("confirm the wheel matches the commit") was unanswerable exactly where it
matters. These pin the one shared helper and the two callers that use it.
"""

from __future__ import annotations

import hashlib
import io
import subprocess
import sys
import tarfile
import types
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from booley.harness.setup import docker_image as init_docker_image
from booley.harness.setup.common import InitContext
from booley.runtime import build_stamp as build_stamp_module
from booley.runtime.build_stamp import (
    _MAX_CONTEXT_BYTES,
    _MAX_CONTEXT_MEMBERS,
    STAMP_RELPATH,
    BuildProfile,
    _extract_development_context,
    _validate_context_members,
    _write_development_context,
    build_stamp,
    development_context_path,
    embedded_development_context_path,
    embedded_official_release,
    extracted_development_context,
    iter_payload_files,
    resolve_build_commit,
    resolve_payload_fingerprint,
    resolve_source_updated_at,
    resolve_wheel_source_fingerprint,
    stamp_path,
    wheel_embedded_source_fingerprint,
    write_build_stamp,
)

BUILD_SH = Path(__file__).resolve().parents[2] / "src" / "booley" / "data" / "docker" / "build.sh"
SOURCE_ROOT = Path(__file__).resolve().parents[2]
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
    docker_dir = root / "src" / "booley" / "data" / "docker"
    docker_dir.mkdir(parents=True)
    (docker_dir / "stable-base-inputs.txt").write_text("README\n", encoding="utf-8")
    (root / "src" / "booley" / "data" / "edalize").mkdir(parents=True)
    (root / "src" / "booley" / "data" / "edalize" / "verible.py").write_text(
        "# adapter\n", encoding="utf-8"
    )
    bwave = root / "crates" / "bwave"
    for relative in ("src/lib.rs", "schema/query.json", "docs/README.md"):
        path = bwave / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative + "\n", encoding="utf-8")
    (bwave / "Cargo.toml").write_text("[package]\nname='bwave'\n", encoding="utf-8")
    (bwave / "Cargo.lock").write_text("# lock\n", encoding="utf-8")
    _git(root, "add", ".")
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
        commit = write_build_stamp(repo, profile=BuildProfile.RUNTIME_IMAGE)
        text = stamp_path(repo).read_text(encoding="utf-8")

        assert stamp_path(repo) == repo / STAMP_RELPATH
        namespace: dict = {}
        exec(compile(text, "_build_commit.py", "exec"), namespace)
        assert namespace["COMMIT"] == commit != ""
        assert namespace["PAYLOAD_FINGERPRINT"] == resolve_payload_fingerprint(repo)
        assert namespace["WHEEL_SOURCE_FINGERPRINT"] == resolve_wheel_source_fingerprint(repo)
        assert len(namespace["RUNTIME_BASE_CONTRACT"]) == 64
        assert len(namespace["STANDARD_SUBSTRATE_CONTRACT"]) == 64
        assert namespace["OFFICIAL_RELEASE"] is False
        assert namespace["DEVELOPMENT_CONTEXT_SHA256"] == ""

    def test_reads_wheel_source_identity_without_importing_wheel(self, tmp_path: Path):
        wheel = tmp_path / WHEEL_NAME
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr(
                "booley/_build_commit.py",
                f'WHEEL_SOURCE_FINGERPRINT = "{"a" * 64}"\n',
            )

        assert wheel_embedded_source_fingerprint(wheel) == "a" * 64

    def test_wheel_source_fingerprint_skips_missing_generated_and_excluded_inputs(
        self, tmp_path: Path
    ):
        root = tmp_path / "empty"
        root.mkdir()
        assert resolve_wheel_source_fingerprint(root) is None
        source = root / "src" / "booley"
        source.mkdir(parents=True)
        (source / "real.py").write_text("value = 1\n", encoding="utf-8")
        (source / "__pycache__").mkdir()
        (source / "__pycache__" / "cached.pyc").write_bytes(b"cached")
        (source / "_build_commit.py").write_text("COMMIT = 'generated'\n", encoding="utf-8")
        (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        (root / "VERSION").write_text("0.2.6\n", encoding="utf-8")
        assert resolve_wheel_source_fingerprint(root)

    @pytest.mark.parametrize(
        "body",
        [
            "not a zip",
            "__invalid__ = True\n",
            "WHEEL_SOURCE_FINGERPRINT = 'short'\n",
            "WHEEL_SOURCE_FINGERPRINT = 1\n",
            "WHEEL_SOURCE_FINGERPRINT = 'a' * 64\n",
        ],
    )
    def test_rejects_invalid_wheel_source_provenance(self, tmp_path: Path, body: str):
        wheel = tmp_path / "invalid.whl"
        if body == "not a zip":
            wheel.write_bytes(body.encode())
        else:
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("booley/_build_commit.py", body)
        with pytest.raises(ValueError):
            wheel_embedded_source_fingerprint(wheel)

    def test_embedded_wheel_source_fingerprint_fails_closed(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "booley._build_commit", types.ModuleType("stamp"))
        assert build_stamp_module.embedded_wheel_source_fingerprint() is None

    def test_marks_an_official_release_build_explicitly(self, repo: Path):
        context = development_context_path(repo)
        context.parent.mkdir(parents=True, exist_ok=True)
        context.write_bytes(b"stale")
        write_build_stamp(repo, profile=BuildProfile.OFFICIAL_RELEASE)
        text = stamp_path(repo).read_text(encoding="utf-8")

        namespace: dict = {}
        exec(compile(text, "_build_commit.py", "exec"), namespace)

        assert namespace["OFFICIAL_RELEASE"] is True
        assert namespace["DEVELOPMENT_CONTEXT_SHA256"] == ""
        assert not context.exists()

    @pytest.mark.parametrize("value", [1, "official-release", None])
    def test_rejects_invalid_build_profiles(self, repo: Path, value: object):
        with pytest.raises(TypeError, match="BuildProfile"):
            write_build_stamp(repo, profile=value)  # type: ignore[arg-type]

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


class TestEmbeddedOfficialRelease:
    def test_missing_marker_is_development(self, monkeypatch):
        monkeypatch.delitem(sys.modules, "booley._build_commit", raising=False)

        assert embedded_official_release() is False

    @pytest.mark.parametrize("value", ["true", 1, None, object()])
    def test_malformed_values_fail_closed(self, monkeypatch, value: object):
        stamp = types.ModuleType("booley._build_commit")
        stamp.OFFICIAL_RELEASE = value
        monkeypatch.setitem(sys.modules, "booley._build_commit", stamp)

        assert embedded_official_release() is False

    def test_literal_true_is_official(self, monkeypatch):
        stamp = types.ModuleType("booley._build_commit")
        stamp.OFFICIAL_RELEASE = True
        monkeypatch.setitem(sys.modules, "booley._build_commit", stamp)

        assert embedded_official_release() is True


class TestDevelopmentBuildContext:
    def test_public_stamp_and_extraction_round_trip(self, tmp_path: Path, monkeypatch):
        package_root = tmp_path / "site-packages" / "booley"
        package_root.joinpath("runtime").mkdir(parents=True)
        stamp_file = package_root / "_build_commit.py"
        context_file = package_root / "data" / "development-build-context.tar.gz"

        with monkeypatch.context() as build_patch:
            build_patch.setattr(build_stamp_module, "stamp_path", lambda _root: stamp_file)
            build_patch.setattr(
                build_stamp_module,
                "development_context_path",
                lambda _root: context_file,
            )
            write_build_stamp(SOURCE_ROOT)

        namespace: dict[str, object] = {}
        exec(stamp_file.read_text(encoding="utf-8"), namespace)
        embedded = types.ModuleType("booley._build_commit")
        embedded.DEVELOPMENT_CONTEXT_SHA256 = namespace["DEVELOPMENT_CONTEXT_SHA256"]
        embedded.PAYLOAD_FINGERPRINT = namespace["PAYLOAD_FINGERPRINT"]
        monkeypatch.setitem(sys.modules, "booley._build_commit", embedded)
        monkeypatch.setattr(
            build_stamp_module,
            "__file__",
            str(package_root / "runtime" / "build_stamp.py"),
        )

        assert embedded_development_context_path() == context_file
        with extracted_development_context() as extracted:
            assert resolve_payload_fingerprint(extracted) == namespace["PAYLOAD_FINGERPRINT"]
            assert stamp_path(extracted).is_file()
            assert development_context_path(extracted).is_file()

    def test_archive_is_deterministic_and_reconstructs_the_payload(self, tmp_path: Path):
        first = tmp_path / "first.tar.gz"
        second = tmp_path / "second.tar.gz"

        digest = _write_development_context(SOURCE_ROOT, first)
        assert _write_development_context(SOURCE_ROOT, second) == digest
        assert first.read_bytes() == second.read_bytes()

        stamp = tmp_path / "_build_commit.py"
        stamp.write_text("COMMIT = 'test'\n", encoding="utf-8")
        extracted = tmp_path / "extracted"
        _extract_development_context(
            first,
            extracted,
            expected_sha256=digest,
            expected_payload_fingerprint=resolve_payload_fingerprint(SOURCE_ROOT) or "",
            stamp_source=stamp,
        )

        assert resolve_payload_fingerprint(extracted) == resolve_payload_fingerprint(SOURCE_ROOT)
        assert stamp_path(extracted).read_text(encoding="utf-8") == "COMMIT = 'test'\n"
        assert development_context_path(extracted).read_bytes() == first.read_bytes()
        ctx = InitContext()
        docker_dir = extracted / "src" / "booley" / "data" / "docker"
        assert init_docker_image._local_build_inputs(ctx, docker_dir) is not None

    def test_archive_includes_bwave_build_inputs(self, tmp_path: Path):
        archive_path = tmp_path / "context.tar.gz"
        _write_development_context(SOURCE_ROOT, archive_path)

        with tarfile.open(archive_path, mode="r:gz") as archive:
            names = {member.name for member in archive.getmembers()}

        assert {
            "crates/bwave/benches/micro.rs",
            "crates/bwave/benches/throughput.rs",
            "crates/bwave/docs/public/intro.md",
            "crates/bwave/docs/skills/bwave.md",
            "crates/bwave/schema/bwave.json",
        } <= names

    def test_hash_mismatch_writes_nothing(self, tmp_path: Path):
        archive = tmp_path / "context.tar.gz"
        _write_development_context(SOURCE_ROOT, archive)
        destination = tmp_path / "extracted"

        with pytest.raises(ValueError, match="hash does not match"):
            _extract_development_context(
                archive,
                destination,
                expected_sha256="0" * 64,
                expected_payload_fingerprint=resolve_payload_fingerprint(SOURCE_ROOT) or "",
                stamp_source=tmp_path / "missing-stamp.py",
            )

        assert not destination.exists()

    @pytest.mark.parametrize(
        "member",
        [
            tarfile.TarInfo("../escape"),
            tarfile.TarInfo("/absolute"),
            tarfile.TarInfo("back\\slash"),
            tarfile.TarInfo("directory/"),
        ],
    )
    def test_rejects_unsafe_archive_members(self, member: tarfile.TarInfo):
        member.type = tarfile.DIRTYPE if member.name.endswith("/") else tarfile.REGTYPE

        with pytest.raises(ValueError, match="unsafe"):
            _validate_context_members([member])

    def test_rejects_duplicate_archive_members(self):
        with pytest.raises(ValueError, match="unsafe"):
            _validate_context_members([tarfile.TarInfo("same"), tarfile.TarInfo("same")])

    def test_rejects_unsafe_member_before_writing_any_file(self, tmp_path: Path):
        archive_path = tmp_path / "unsafe.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            for name in ("safe", "../escape"):
                info = tarfile.TarInfo(name)
                info.size = 1
                archive.addfile(info, io.BytesIO(b"x"))
        stamp = tmp_path / "stamp.py"
        stamp.write_text("COMMIT = 'test'\n", encoding="utf-8")
        destination = tmp_path / "destination"

        with pytest.raises(ValueError, match="unsafe"):
            _extract_development_context(
                archive_path,
                destination,
                expected_sha256=hashlib.sha256(archive_path.read_bytes()).hexdigest(),
                expected_payload_fingerprint="0" * 64,
                stamp_source=stamp,
            )

        assert not destination.exists()
        assert not (tmp_path / "escape").exists()

    def test_rejects_member_count_and_total_size_bounds(self):
        members = [tarfile.TarInfo(f"file-{index}") for index in range(_MAX_CONTEXT_MEMBERS + 1)]
        with pytest.raises(ValueError, match="too many members"):
            _validate_context_members(members)

        oversized = tarfile.TarInfo("oversized")
        oversized.size = _MAX_CONTEXT_BYTES + 1
        with pytest.raises(ValueError, match="too large"):
            _validate_context_members([oversized])


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

    def test_verified_stamp_is_preserved_for_embedded_context(self, repo: Path, monkeypatch):
        stamp_path(repo).write_text("COMMIT = 'embedded'\n", encoding="utf-8")
        seen: list[str] = []

        def _fake_run(cmd, **kwargs):
            seen.append(stamp_path(repo).read_text(encoding="utf-8"))
            _write_wheel(repo)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(subprocess, "run", _fake_run)

        assert init_docker_image._docker_build_wheel(InitContext(), repo, preserve_stamp=True)
        assert seen == ["COMMIT = 'embedded'\n"]
        assert stamp_path(repo).is_file()

    def test_verified_stamp_is_required_for_embedded_context(self, repo: Path):
        ctx = InitContext()

        assert not init_docker_image._docker_build_wheel(ctx, repo, preserve_stamp=True)
        assert ctx.results[-1].detail == "wheel build failed"

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
    def test_stamp_never_reaches_the_image_fingerprint(self, repo: Path):
        """build.sh and init drop the stamp at different points, so hashing it
        would make their fingerprints disagree — every init after a build.sh
        build would call the image stale and rebuild it for 20 minutes."""
        root = repo
        (root / "src" / "booley" / "real.py").write_text("x = 1\n", encoding="utf-8")

        before = init_docker_image._image_build_fingerprint(root)
        write_build_stamp(root, profile=BuildProfile.RUNTIME_IMAGE)

        assert init_docker_image._image_build_fingerprint(root) == before
        assert STAMP_RELPATH not in [
            p.relative_to(root).as_posix() for p in iter_payload_files(root)
        ]
        assert development_context_path(root).relative_to(root).as_posix() not in [
            p.relative_to(root).as_posix() for p in iter_payload_files(root)
        ]
