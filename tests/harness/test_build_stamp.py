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
import os
import posixpath
import re
import shutil
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from booley.harness.setup import docker_image as init_docker_image
from booley.harness.setup.common import InitContext
from booley.runtime import build_stamp as build_stamp_module
from booley.runtime.build_stamp import (
    STAMP_RELPATH,
    BuildProfile,
    development_context_path,
    embedded_development_context_path,
    embedded_official_release,
    extracted_development_context,
    resolve_build_commit,
    resolve_payload_fingerprint,
    resolve_source_updated_at,
    resolve_wheel_source_fingerprint,
    runtime_image_build_stamp,
    stamp_path,
    wheel_embedded_source_fingerprint,
    write_build_stamp,
)
from booley.runtime.packaged_docs import PACKAGED_DOCS_RELATIVE_DIR, SHIPPED_DOCS
from tests.installed_wheel_support import install_package
from tests.source_checkout_support import copy_source_checkout
from tests.subprocess_budget import run_bounded_child
from tests.wheel_build_fake import WheelBuild, install_wheel_build

BUILD_SH = Path(__file__).resolve().parents[2] / "src" / "booley" / "data" / "docker" / "build.sh"
SOURCE_ROOT = Path(__file__).resolve().parents[2]
WHEEL_NAME = "booley_rtl-0.2.3-py3-none-any.whl"
BUILT_WHEEL = "booley_rtl-0.0.0-py3-none-any.whl"  # what the wheel build fake writes
CHANGELOG_SOURCE = "CHANGELOG.md"
PACKAGED_CHANGELOG = "src/booley/data/refs/CHANGELOG.md"


def _write_wheel(root: Path, name: str = WHEEL_NAME) -> Path:
    wheel = root / "dist" / name
    wheel.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(wheel, "w") as archive:
        stamp = stamp_path(root)
        archive.writestr(
            "booley/_build_commit.py", stamp.read_text() if stamp.is_file() else "COMMIT = ''\n"
        )
    return wheel


def _stamp_values(repo: Path) -> dict[str, object]:
    namespace: dict[str, object] = {}
    exec(compile(stamp_path(repo).read_text(encoding="utf-8"), "stamp.py", "exec"), namespace)
    return namespace


def _git(root: Path, *args: str) -> None:
    run_bounded_child(["git", "-C", str(root), *args], check=True)


def _doc_source_text(source: str) -> bytes:
    """Return a shipped doc source with one link to an unshipped doc.

    The link makes the rendered copy differ from its source; it is written as
    bytes so Windows keeps LF line endings.
    """
    setup = posixpath.relpath("docs/user/SETUP.md", posixpath.dirname(source))
    return f"# {posixpath.basename(source)}\n[setup]({setup})\n".encode()


def _write_doc_sources(root: Path) -> None:
    """Write every shipped doc source and the changelog under *root*; no copy is written."""
    (root / CHANGELOG_SOURCE).write_bytes(b"# Changelog\n")
    for source in SHIPPED_DOCS:
        path = root / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_doc_source_text(source))


def _rendered(root: Path, source: str) -> bytes:
    """Return the packaged copy of *source* as the renderer defines it."""
    from booley.runtime.packaged_docs import render_packaged_doc

    return render_packaged_doc(source, (root / source).read_bytes().decode("utf-8")).encode(
        "utf-8"
    )


def _copy_path(root: Path, source: str) -> Path:
    """Return where the packaged copy of *source* lives under *root*."""
    return root / PACKAGED_DOCS_RELATIVE_DIR / posixpath.basename(source)


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
    _write_doc_sources(root)
    _git(root, "add", ".")
    _git(root, "commit", "-m", "init")
    return root


def test_rendered_docs_are_untracked_and_ignored() -> None:
    """No rendered copy is committed and the build output directory is Git-ignored (#3658)."""
    tracked = run_bounded_child(
        ["git", "-C", str(SOURCE_ROOT), "ls-files", "--", PACKAGED_DOCS_RELATIVE_DIR], check=True
    )
    assert tracked.stdout == ""
    changelog = run_bounded_child(
        ["git", "-C", str(SOURCE_ROOT), "ls-files", "--", PACKAGED_CHANGELOG], check=True
    )
    assert changelog.stdout == ""
    paths = [
        f"{PACKAGED_DOCS_RELATIVE_DIR}/{posixpath.basename(source)}" for source in SHIPPED_DOCS
    ]
    paths.append(PACKAGED_CHANGELOG)
    # Exit 1 means nothing matched, which the comparison below reports.
    ignored = run_bounded_child(["git", "-C", str(SOURCE_ROOT), "check-ignore", "--", *paths])
    assert sorted(ignored.stdout.split()) == sorted(paths)


class TestResolveBuildCommit:
    def test_reports_head_commit_and_its_time(self, repo: Path):
        def head(*args: str) -> str:
            return run_bounded_child(["git", "-C", str(repo), *args], check=True).stdout.strip()

        assert resolve_build_commit(repo) == head("rev-parse", "--short", "HEAD")
        assert resolve_source_updated_at(repo) == head("log", "-1", "--format=%cI", "HEAD")

    def test_non_git_tree_yields_empty(self, tmp_path: Path):
        """A source tarball is not a failure — there is simply no commit."""
        assert resolve_build_commit(tmp_path) == ""


class TestWriteBuildStamp:
    @pytest.mark.parametrize(
        ("profile", "official"),
        [(BuildProfile.RUNTIME_IMAGE, False), (BuildProfile.OFFICIAL_RELEASE, True)],
    )
    def test_writes_an_importable_module(self, repo: Path, profile: BuildProfile, official: bool):
        context = development_context_path(repo)
        context.parent.mkdir(parents=True, exist_ok=True)
        context.write_bytes(b"stale")

        commit = write_build_stamp(repo, profile=profile)
        namespace = _stamp_values(repo)

        assert stamp_path(repo) == repo / STAMP_RELPATH
        assert namespace["COMMIT"] == commit != ""
        assert str(namespace["COMMIT_TIME"]).endswith("Z")
        assert namespace["PAYLOAD_FINGERPRINT"] == resolve_payload_fingerprint(repo)
        assert namespace["WHEEL_SOURCE_FINGERPRINT"] == resolve_wheel_source_fingerprint(repo)
        assert len(str(namespace["RUNTIME_BASE_CONTRACT"])) == 64
        assert len(str(namespace["STANDARD_SUBSTRATE_CONTRACT"])) == 64
        assert namespace["OFFICIAL_RELEASE"] is official
        assert namespace["DEVELOPMENT_CONTEXT_SHA256"] == ""
        assert not context.exists()

    def test_renders_every_shipped_doc_and_removes_orphans(self, repo: Path):
        shutil.rmtree(repo / PACKAGED_DOCS_RELATIVE_DIR, ignore_errors=True)
        directory = repo / PACKAGED_DOCS_RELATIVE_DIR
        directory.mkdir(parents=True)
        (directory / "RETIRED.md").write_bytes(b"old\n")

        # A renderer or text-mode copy would rewrite the link or the CRLF.
        (repo / CHANGELOG_SOURCE).write_bytes(b"# Changelog\n[x](docs/user/SETUP.md)\r\n")

        write_build_stamp(repo, profile=BuildProfile.RUNTIME_IMAGE)

        assert (repo / PACKAGED_CHANGELOG).read_bytes() == (repo / CHANGELOG_SOURCE).read_bytes()
        for source in SHIPPED_DOCS:
            assert _copy_path(repo, source).read_bytes() == _rendered(repo, source)
        assert _copy_path(repo, "docs/user/CONFIG.md").read_bytes() == (
            b"# CONFIG.md\nsetup (host doc, not shipped)\n"
        )
        assert not (directory / "RETIRED.md").exists()
        assert len(list(directory.iterdir())) == len(SHIPPED_DOCS)

    @pytest.mark.parametrize("source", ["docs/user/TESTS.md", CHANGELOG_SOURCE])
    def test_missing_shipped_source_fails_naming_it(self, repo: Path, source: str):
        (repo / source).unlink()

        with pytest.raises(ValueError, match=re.escape(source)):
            write_build_stamp(repo, profile=BuildProfile.RUNTIME_IMAGE)

        assert not stamp_path(repo).exists()
        assert not development_context_path(repo).exists()

    @pytest.mark.parametrize("fail", [False, True])
    def test_runtime_stamp_leaves_checkout_clean(self, repo: Path, fail: bool):
        """After a build, failed or not, no stamp stays and the checkout is clean (#3658).

        The rendered copies are Git-ignored build output, so a clean tree that holds only
        committed sources, with the repository's real ``.gitignore``, stays clean.
        """
        shutil.copyfile(SOURCE_ROOT / ".gitignore", repo / ".gitignore")
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-m", "ignore build output")

        def build() -> None:
            with runtime_image_build_stamp(repo) as commit:
                assert (repo / PACKAGED_DOCS_RELATIVE_DIR / "CONFIG.md").is_file()
                assert (repo / PACKAGED_CHANGELOG).is_file()
                assert commit != "" and not commit.endswith("+dirty")
                assert _stamp_values(repo)["DEVELOPMENT_CONTEXT_SHA256"] == ""
                assert _stamp_values(repo)["OFFICIAL_RELEASE"] is False
                if fail:
                    raise RuntimeError("build blew up")

        if fail:
            with pytest.raises(RuntimeError):
                build()
        else:
            build()
        assert not stamp_path(repo).exists()
        assert not development_context_path(repo).exists()
        status = run_bounded_child(
            ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"], check=True
        )
        assert status.stdout == ""

    def test_render_step_runs_without_third_party_packages(self, repo: Path):
        code = (
            "import importlib.util, sys\n"
            "from pathlib import Path\n"
            "assert importlib.util.find_spec('markdown_it') is None\n"
            "from booley.runtime.build_stamp import BuildProfile, write_build_stamp\n"
            "write_build_stamp(Path(sys.argv[1]), profile=BuildProfile.RUNTIME_IMAGE)\n"
        )

        result = run_bounded_child(
            [sys.executable, "-S", "-P", "-c", code, str(repo)],
            env={**os.environ, "PYTHONPATH": str(SOURCE_ROOT / "src")},
        )

        assert result.returncode == 0, result.stderr
        assert _copy_path(repo, "docs/user/CONFIG.md").read_bytes() == (
            b"# CONFIG.md\nsetup (host doc, not shipped)\n"
        )

    def test_shipped_sources_enter_fingerprints_and_rendered_copies_do_not(self, repo: Path):
        for source in (*SHIPPED_DOCS, CHANGELOG_SOURCE):
            before = (resolve_payload_fingerprint(repo), resolve_wheel_source_fingerprint(repo))
            with (repo / source).open("ab") as handle:
                handle.write(b"edit\n")
            after = (resolve_payload_fingerprint(repo), resolve_wheel_source_fingerprint(repo))
            assert after[0] != before[0], source
            assert after[1] != before[1], source

        baseline = (resolve_payload_fingerprint(repo), resolve_wheel_source_fingerprint(repo))
        copy = _copy_path(repo, "docs/user/CONFIG.md")
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_bytes(b"rendered copy\n")
        (repo / PACKAGED_CHANGELOG).parent.mkdir(parents=True, exist_ok=True)
        (repo / PACKAGED_CHANGELOG).write_bytes(b"rendered changelog\n")
        assert (
            resolve_payload_fingerprint(repo),
            resolve_wheel_source_fingerprint(repo),
        ) == baseline
        copy.unlink()
        (repo / PACKAGED_CHANGELOG).unlink()
        assert (
            resolve_payload_fingerprint(repo),
            resolve_wheel_source_fingerprint(repo),
        ) == baseline

        # A sibling of the rendered changelog ships as package data, so it is hashed.
        sibling = repo / f"{PACKAGED_CHANGELOG}.bak"
        sibling.parent.mkdir(parents=True, exist_ok=True)
        sibling.write_bytes(b"stray\n")
        assert resolve_payload_fingerprint(repo) != baseline[0]
        assert resolve_wheel_source_fingerprint(repo) != baseline[1]

    def test_fingerprints_of_fixed_tree_are_pinned(self, tmp_path: Path):
        """Fingerprints are persisted in image labels and stamps (ADR 0053).

        Changing the literal digests below invalidates every previously built
        image; they last changed when the shipped doc sources joined the inputs
        (#3657).
        The fixture omits ``VERSION``: ``Path`` sorting is case-insensitive on
        Windows, so a mixed-case sibling would order differently there.
        """
        root = tmp_path / "tree"
        included = {
            "src/booley/a.py": b"a = 1\n",
            "src/booley/sub/b.py": b"b = 2\n",
            "crates/bwave/src/lib.rs": b"fn f() {}\n",
            "crates/bwave/Cargo.toml": b"[package]\n",
            "crates/bwave/Cargo.lock": b"# lock\n",
            ".dockerignore": b"*\n",
            "pyproject.toml": b"[project]\n",
            "docs/user/CONFIG.md": b"# config\n",
        }
        excluded = {
            "src/booley/_build_commit.py": b"COMMIT = 'x'\n",
            "src/booley/data/development-build-context.tar.gz": b"context",
            "src/booley/__pycache__/a.cpython-312.pyc": b"bytecode",
            "src/booley/data/docs/CONFIG.md": b"# config copy\n",
        }
        for relative, content in {**included, **excluded}.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

        assert resolve_payload_fingerprint(root) == (
            "b27b90d3504d1a7f37db02e82c4045e8c2e6df8ee1e013263601ce648eefad2a"
        )
        assert resolve_wheel_source_fingerprint(root) == (
            "dcc42754de17f9855cdfec8e85ec40ddc5e29b59cf680d6ae86dafcd124a6167"
        )
        assert resolve_wheel_source_fingerprint(tmp_path / "empty") is None

    @staticmethod
    def _commit_at(repo: Path, committed_at: str) -> None:
        """Add a commit whose committer date is pinned to *committed_at*."""
        (repo / "README").write_text(f"at {committed_at}\n", encoding="utf-8")
        _git(repo, "add", ".")
        run_bounded_child(
            ["git", "-C", str(repo), "commit", "-m", "dated"],
            check=True,
            # Ignore the developer's global git config (signing, hooks).
            env={
                **os.environ,
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_COMMITTER_DATE": committed_at,
            },
        )

    def test_stamps_commit_time_in_utc_across_day_boundary(self, repo: Path):
        # 23:30 at UTC-7 is 06:30 the next day in UTC.
        self._commit_at(repo, "2026-10-26T23:30:00-07:00")
        write_build_stamp(repo, profile=BuildProfile.RUNTIME_IMAGE)

        assert _stamp_values(repo).get("COMMIT_TIME") == "2026-10-27T06:30:00Z"

    def test_release_readers_accept_stamp_with_commit_time(self, repo: Path, monkeypatch):
        from booley.runtime import image_build_contracts

        self._commit_at(repo, "2026-10-26T12:00:00+00:00")
        commit = write_build_stamp(repo, profile=BuildProfile.OFFICIAL_RELEASE)
        wheel = _write_wheel(repo)
        values = _stamp_values(repo)
        install_package(monkeypatch, repo / "src" / "booley")

        assert values.get("COMMIT_TIME") == "2026-10-26T12:00:00Z"
        assert build_stamp_module.wheel_embedded_commit(wheel) == commit
        assert wheel_embedded_source_fingerprint(wheel) == resolve_wheel_source_fingerprint(repo)
        contracts = image_build_contracts.embedded_image_build_contracts()
        assert contracts.runtime_base == values["RUNTIME_BASE_CONTRACT"]
        assert contracts.standard_substrate == values["STANDARD_SUBSTRATE_CONTRACT"]

    @pytest.mark.parametrize(
        ("reader", "body"),
        [
            (wheel_embedded_source_fingerprint, "not a zip"),
            (wheel_embedded_source_fingerprint, "__invalid__ = True\n"),
            (wheel_embedded_source_fingerprint, "WHEEL_SOURCE_FINGERPRINT = 'short'\n"),
            (wheel_embedded_source_fingerprint, "WHEEL_SOURCE_FINGERPRINT = 1\n"),
            (wheel_embedded_source_fingerprint, "WHEEL_SOURCE_FINGERPRINT = 'a' * 64\n"),
            (build_stamp_module.wheel_embedded_commit, "COMMIT = (\n"),
            (build_stamp_module.wheel_embedded_commit, "COMMIT = 3\n"),
            (build_stamp_module.wheel_embedded_commit, "OTHER = 'x'\n"),
        ],
    )
    def test_wheel_readers_reject_invalid_provenance(self, tmp_path: Path, reader, body: str):
        wheel = tmp_path / "invalid.whl"
        if body == "not a zip":
            wheel.write_bytes(body.encode())
        else:
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("booley/_build_commit.py", body)
        with pytest.raises(ValueError, match="wheel build"):
            reader(wheel)

    def test_stamp_constants_reads_literals_and_first_assignment_wins(self):
        source = "A = 'one'\nA = 'two'\nB, C = 1, 2\nD = 3\nE: int = 4\n"

        assert build_stamp_module.stamp_constants(source, {"A", "B", "D", "MISSING"}) == {
            "A": "one",
            "D": 3,
        }

    @pytest.mark.parametrize("value", [1, "official-release", None])
    def test_rejects_invalid_build_profiles(self, repo: Path, value: object):
        with pytest.raises(TypeError, match="BuildProfile"):
            write_build_stamp(repo, profile=value)  # type: ignore[arg-type]

    def test_payload_fingerprint_changes_for_dirty_source_at_same_head(self, repo: Path):
        before = resolve_payload_fingerprint(repo)
        head = resolve_build_commit(repo)

        (repo / "src" / "booley" / "payload.py").write_text("VALUE = 2\n", encoding="utf-8")

        assert resolve_payload_fingerprint(repo) != before
        dirty = resolve_build_commit(repo)
        assert dirty.endswith("+dirty")
        assert dirty.removesuffix("+dirty") == head


@pytest.mark.parametrize(
    "stamp",
    [None, "OFFICIAL_RELEASE = 'true'\n", "OFFICIAL_RELEASE = 1\n", "OFFICIAL_RELEASE = True\n"],
)
def test_embedded_stamp_readers_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stamp: str | None
) -> None:
    if stamp is not None:
        (tmp_path / "_build_commit.py").write_bytes(stamp.encode())
    install_package(monkeypatch, tmp_path)

    assert embedded_official_release() is (stamp == "OFFICIAL_RELEASE = True\n")
    assert build_stamp_module.embedded_wheel_source_fingerprint() is None


class TestDevelopmentBuildContext:
    def test_public_stamp_and_extraction_round_trip(self, tmp_path: Path, monkeypatch):
        # A copy, not the live checkout: the render step writes into its root.
        root = tmp_path / "checkout"
        copy_source_checkout(
            SOURCE_ROOT,
            root,
            "src/booley",
            "crates/bwave",
            ".github/scripts",
            # private: the context's file list has no public name, and a full copy costs more
            *build_stamp_module._DEVELOPMENT_CONTEXT_FILES,
        )
        write_build_stamp(root)
        values = _stamp_values(root)
        os.utime(root / "pyproject.toml", (978_307_200, 978_307_200))
        write_build_stamp(root)
        assert (
            _stamp_values(root)["DEVELOPMENT_CONTEXT_SHA256"]
            == (values["DEVELOPMENT_CONTEXT_SHA256"])
        )
        install_package(monkeypatch, root / "src" / "booley")

        assert embedded_development_context_path() == development_context_path(root).resolve()
        with extracted_development_context() as extracted:
            assert resolve_payload_fingerprint(extracted) == values["PAYLOAD_FINGERPRINT"]
            assert stamp_path(extracted).read_bytes() == stamp_path(root).read_bytes()
            assert (
                development_context_path(extracted).read_bytes()
                == development_context_path(root).read_bytes()
            )
            assert (extracted / PACKAGED_CHANGELOG).read_bytes() == (
                root / CHANGELOG_SOURCE
            ).read_bytes()
            for source in SHIPPED_DOCS:
                assert (extracted / source).read_bytes() == (root / source).read_bytes()
                assert _copy_path(extracted, source).read_bytes() == _rendered(root, source)
            for bwave_input in (
                "benches/micro.rs",
                "benches/throughput.rs",
                "docs/public/intro.md",
                "docs/skills/bwave.md",
                "schema/bwave.json",
            ):
                assert (extracted / "crates" / "bwave" / bwave_input).is_file(), bwave_input

    @pytest.mark.parametrize(
        "members",
        [["../escape"], ["/absolute"], ["back\\slash"], ["directory/"], ["same", "same"]],
        ids=["parent", "absolute", "backslash", "directory", "duplicate"],
    )
    def test_extraction_rejects_a_tampered_context(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, members: list[str]
    ):
        package = tmp_path / "pkg"
        archive_path = package / "data" / "development-build-context.tar.gz"
        archive_path.parent.mkdir(parents=True)
        _write_archive(archive_path, members)
        digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        (package / "_build_commit.py").write_bytes(
            f"DEVELOPMENT_CONTEXT_SHA256 = '{digest}'\n"
            f"PAYLOAD_FINGERPRINT = '{'a' * 64}'\n".encode()
        )
        install_package(monkeypatch, package)
        temporary = tmp_path / "tmp"
        temporary.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(temporary))

        with pytest.raises(ValueError, match="unsafe"), extracted_development_context():
            pytest.fail("a tampered context must not be extracted")

        assert list(temporary.iterdir()) == []

    @pytest.mark.parametrize(
        ("members", "sha", "message"),
        [(["safe"], "0" * 64, "hash does not match"), (["safe", "../escape"], None, "unsafe")],
        ids=["hash", "unsafe-member"],
    )
    def test_rejects_a_bad_context_before_writing_any_file(
        self, tmp_path: Path, members: list[str], sha: str | None, message: str
    ):
        archive_path = tmp_path / "bad.tar.gz"
        _write_archive(archive_path, members)
        stamp = tmp_path / "stamp.py"
        stamp.write_text("COMMIT = 'test'\n", encoding="utf-8")
        destination = tmp_path / "destination"

        with pytest.raises(ValueError, match=message):
            # private: public extraction deletes its folder on failure, hiding an early write
            build_stamp_module._extract_development_context(
                archive_path,
                destination,
                expected_sha256=sha or hashlib.sha256(archive_path.read_bytes()).hexdigest(),
                expected_payload_fingerprint="0" * 64,
                stamp_source=stamp,
            )

        assert not destination.exists()
        assert not (tmp_path / "escape").exists()

    def test_rejects_member_count_and_total_size_bounds(self):
        # private: the public extraction reaches these bounds only through a
        # 5,001-member or 64 MiB archive
        members = [
            tarfile.TarInfo(f"file-{index}")
            for index in range(build_stamp_module._MAX_CONTEXT_MEMBERS + 1)  # private: as above
        ]
        with pytest.raises(ValueError, match="too many members"):
            build_stamp_module._validate_context_members(members)  # private: as above

        oversized = tarfile.TarInfo("oversized")
        oversized.size = build_stamp_module._MAX_CONTEXT_BYTES + 1  # private: as above
        with pytest.raises(ValueError, match="too large"):
            build_stamp_module._validate_context_members([oversized])  # private: as above


def _write_archive(path: Path, members: list[str]) -> None:
    """Write a ``tar.gz`` of one-byte files; a name ending in ``/`` is a directory."""
    with tarfile.open(path, "w:gz") as archive:
        for name in members:
            info = tarfile.TarInfo(name)
            if name.endswith("/"):
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            else:
                info.size = 1
                archive.addfile(info, io.BytesIO(b"x"))


@pytest.fixture
def wheel_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WheelBuild:
    return install_wheel_build(tmp_path / "wheel-fake", monkeypatch)


def _build_wheel(ctx: InitContext, repo: Path, *, preserve_stamp: bool = False) -> bool:
    # private: no public function reaches the wheel step without a whole local Sandbox Image
    # build (tests/harness/setup/test_docker_image.py)
    return init_docker_image._docker_build_wheel(ctx, repo, preserve_stamp=preserve_stamp)


class TestInitStampsItsWheel:
    def test_wheel_packs_the_stamp_and_rendered_docs_then_removes_the_stamp(
        self, repo: Path, wheel_build: WheelBuild
    ):
        """The F-3 regression: init's `python -m build` must see the stamp."""
        stale_module = repo / "build" / "lib" / "booley" / "tools" / "legacy.py"
        stale_module.parent.mkdir(parents=True)
        stale_module.write_text("stale = True\n", encoding="utf-8")
        _write_wheel(repo, "booley_rtl-0.1.0-py3-none-any.whl")

        assert _build_wheel(InitContext(), repo) is True

        wheels = list((repo / "dist").glob("booley_rtl-*.whl"))
        assert [path.name for path in wheels] == [BUILT_WHEEL]
        with zipfile.ZipFile(wheels[0]) as archive:
            packed = set(archive.namelist())
        assert "booley/_build_commit.py" in packed
        assert {f"booley/data/docs/{posixpath.basename(doc)}" for doc in SHIPPED_DOCS} <= packed
        assert "booley/tools/legacy.py" not in packed
        assert not stamp_path(repo).exists()

    def test_verified_stamp_is_packed_and_kept(self, repo: Path, wheel_build: WheelBuild):
        stamp_path(repo).write_bytes(b"COMMIT = 'embedded'\n")

        assert _build_wheel(InitContext(), repo, preserve_stamp=True) is True

        assert stamp_path(repo).read_bytes() == b"COMMIT = 'embedded'\n"

    @pytest.mark.parametrize(
        ("stamp", "preserve", "reply"),
        [
            ("COMMIT = (\n", True, {}),
            ("COMMIT = 'a' + 'b'\n", True, {}),
            (None, True, {}),
            (None, False, {"code": 1, "stderr": "No module named build"}),
            (None, False, {"wheels": ()}),
            (None, False, {"wheels": (BUILT_WHEEL, WHEEL_NAME)}),
        ],
        ids=["unparseable", "non-literal", "no-stamp", "build-fails", "no-wheel", "two-wheels"],
    )
    def test_wheel_build_failure_is_reported(
        self,
        repo: Path,
        wheel_build: WheelBuild,
        stamp: str | None,
        preserve: bool,
        reply: dict[str, object],
    ):
        if stamp is not None:
            stamp_path(repo).write_bytes(stamp.encode())
        wheel_build.answer(**{"code": 0, **reply})  # type: ignore[arg-type]
        ctx = InitContext()

        assert _build_wheel(ctx, repo, preserve_stamp=preserve) is False

        assert (ctx.results[-1].status, ctx.results[-1].detail) == ("err", "wheel build failed")
        assert stamp_path(repo).exists() is (stamp is not None)

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


@pytest.mark.parametrize("official", [False, None])
def test_runtime_image_stamp_has_actionable_recovery(repo, monkeypatch, official):
    write_build_stamp(repo, profile=BuildProfile.RUNTIME_IMAGE)
    if official is None:
        lines = stamp_path(repo).read_text(encoding="utf-8").splitlines()
        kept = [line for line in lines if not line.startswith("OFFICIAL_RELEASE")]
        stamp_path(repo).write_bytes("\n".join(kept).encode())
    install_package(monkeypatch, repo / "src" / "booley")
    with pytest.raises(ValueError) as caught, extracted_development_context():
        pytest.fail("runtime-image wheel cannot supply development context")
    message = str(caught.value)
    assert "runtime-image-style" in message
    assert "write_build_stamp(root, profile=BuildProfile.DEVELOPMENT_WHEEL)" in message
    assert "python3 -P -m build --wheel" in message
    assert "python3 -P .github/scripts/build_development_wheel.py" in message
    assert "src/booley/_build_commit.py" in message
    assert "src/booley/data/development-build-context.tar.gz" in message
    assert "CONTRIBUTING.md#building-a-development-wheel" in message


@pytest.mark.parametrize("context_hash,official", [("", True), ("bad", False), (None, False)])
def test_invalid_or_official_context_has_no_runtime_recovery(
    tmp_path, monkeypatch, context_hash, official
):
    (tmp_path / "_build_commit.py").write_bytes(
        f"DEVELOPMENT_CONTEXT_SHA256 = {context_hash!r}\n"
        f"PAYLOAD_FINGERPRINT = '{'a' * 64}'\n"
        f"OFFICIAL_RELEASE = {official}\n".encode()
    )
    install_package(monkeypatch, tmp_path)
    with (
        pytest.raises(ValueError, match="invalid build-context provenance"),
        extracted_development_context(),
    ):
        pytest.fail("invalid provenance must fail closed")
