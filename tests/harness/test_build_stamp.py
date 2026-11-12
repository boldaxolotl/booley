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
import shutil
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
    development_context_path,
    embedded_development_context_path,
    embedded_official_release,
    extracted_development_context,
    iter_payload_files,
    resolve_build_commit,
    resolve_payload_fingerprint,
    resolve_source_updated_at,
    resolve_wheel_source_fingerprint,
    runtime_image_build_stamp,
    stamp_path,
    wheel_embedded_source_fingerprint,
    write_build_stamp,
)
from booley.runtime.packaged_docs import (
    PACKAGED_DOCS_RELATIVE_DIR,
    SHIPPED_DOCS,
    MissingShippedDocError,
)
from tests.source_checkout_support import copy_source_checkout
from tests.subprocess_budget import run_bounded_child

BUILD_SH = Path(__file__).resolve().parents[2] / "src" / "booley" / "data" / "docker" / "build.sh"
SOURCE_ROOT = Path(__file__).resolve().parents[2]
WHEEL_NAME = "booley_rtl-0.2.3-py3-none-any.whl"


def _write_wheel(root: Path, name: str = WHEEL_NAME) -> Path:
    wheel = root / "dist" / name
    wheel.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(wheel, "w") as archive:
        stamp = stamp_path(root)
        archive.writestr(
            "booley/_build_commit.py", stamp.read_text() if stamp.is_file() else "COMMIT = ''\n"
        )
    return wheel


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
    """Write every shipped doc source under *root*; no packaged copy is written."""
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
        assert namespace["COMMIT_TIME"].endswith("Z")
        assert namespace["PAYLOAD_FINGERPRINT"] == resolve_payload_fingerprint(repo)
        assert namespace["WHEEL_SOURCE_FINGERPRINT"] == resolve_wheel_source_fingerprint(repo)
        assert len(namespace["RUNTIME_BASE_CONTRACT"]) == 64
        assert len(namespace["STANDARD_SUBSTRATE_CONTRACT"]) == 64
        assert namespace["OFFICIAL_RELEASE"] is False
        assert namespace["DEVELOPMENT_CONTEXT_SHA256"] == ""

    def test_renders_every_shipped_doc_and_removes_orphans(self, repo: Path):
        shutil.rmtree(repo / PACKAGED_DOCS_RELATIVE_DIR, ignore_errors=True)
        directory = repo / PACKAGED_DOCS_RELATIVE_DIR
        directory.mkdir(parents=True)
        (directory / "RETIRED.md").write_bytes(b"old\n")

        write_build_stamp(repo, profile=BuildProfile.RUNTIME_IMAGE)

        for source in SHIPPED_DOCS:
            assert _copy_path(repo, source).read_bytes() == _rendered(repo, source)
        assert _copy_path(repo, "docs/user/CONFIG.md").read_bytes() == (
            b"# CONFIG.md\nsetup (host doc, not shipped)\n"
        )
        assert not (directory / "RETIRED.md").exists()
        assert len(list(directory.iterdir())) == len(SHIPPED_DOCS)

    def test_missing_shipped_source_fails_naming_it(self, repo: Path):
        (repo / "docs/user/TESTS.md").unlink()

        with pytest.raises(MissingShippedDocError, match=r"docs/user/TESTS\.md"):
            write_build_stamp(repo, profile=BuildProfile.RUNTIME_IMAGE)

        assert not stamp_path(repo).exists()
        assert not development_context_path(repo).exists()

    def test_stale_tracked_copy_is_rewritten_after_the_commit_is_stamped(self, repo: Path):
        """A clean tree with one stale tracked copy must not stamp ``+dirty``."""
        for source in SHIPPED_DOCS:
            copy = _copy_path(repo, source)
            copy.parent.mkdir(parents=True, exist_ok=True)
            copy.write_bytes(f"# {copy.name}\nsetup (host doc, not shipped)\n".encode())
        stale = _copy_path(repo, "docs/user/CONFIG.md")
        stale.write_bytes(b"stale\n")
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "stale copy")

        commit = write_build_stamp(repo, profile=BuildProfile.RUNTIME_IMAGE)

        assert commit != "" and not commit.endswith("+dirty")
        assert stale.read_bytes() == b"# CONFIG.md\nsetup (host doc, not shipped)\n"

    def test_build_on_current_checkout_leaves_tree_clean(self, repo: Path):
        """Guard: rendering must not dirty a tree whose copies already match."""
        from booley.runtime.packaged_docs import write_packaged_docs

        write_packaged_docs(repo)
        _git(repo, "add", ".")
        _git(repo, "commit", "-m", "current copies")

        with runtime_image_build_stamp(repo):
            pass

        status = run_bounded_child(
            ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
            check=True,
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
        for source in SHIPPED_DOCS:
            before = (resolve_payload_fingerprint(repo), resolve_wheel_source_fingerprint(repo))
            (repo / source).write_bytes(_doc_source_text(source) + b"edit\n")
            after = (resolve_payload_fingerprint(repo), resolve_wheel_source_fingerprint(repo))
            assert after[0] != before[0], source
            assert after[1] != before[1], source

        baseline = (resolve_payload_fingerprint(repo), resolve_wheel_source_fingerprint(repo))
        copy = _copy_path(repo, "docs/user/CONFIG.md")
        copy.parent.mkdir(parents=True, exist_ok=True)
        copy.write_bytes(b"rendered copy\n")
        assert (
            resolve_payload_fingerprint(repo),
            resolve_wheel_source_fingerprint(repo),
        ) == baseline
        copy.unlink()
        assert (
            resolve_payload_fingerprint(repo),
            resolve_wheel_source_fingerprint(repo),
        ) == baseline

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

    @staticmethod
    def _stamp_values(repo: Path) -> dict:
        namespace: dict = {}
        exec(compile(stamp_path(repo).read_text(encoding="utf-8"), "stamp.py", "exec"), namespace)
        return namespace

    def test_stamps_commit_time_in_utc_across_day_boundary(self, repo: Path):
        # 23:30 at UTC-7 is 06:30 the next day in UTC.
        self._commit_at(repo, "2026-10-26T23:30:00-07:00")
        write_build_stamp(repo, profile=BuildProfile.RUNTIME_IMAGE)

        assert self._stamp_values(repo).get("COMMIT_TIME") == "2026-10-27T06:30:00Z"

    def test_wheel_version_reads_commit_date_from_stamp(self, repo: Path, monkeypatch):
        import booley
        from booley import __version__
        from booley.harness import booley as cli
        from booley.runtime.version_attribution import VersionAttribution, VersionOrigin

        self._commit_at(repo, "2026-10-26T12:00:00+00:00")
        commit = write_build_stamp(repo, profile=BuildProfile.RUNTIME_IMAGE)
        stamp = types.ModuleType("booley._build_commit")
        stamp.__dict__.update(self._stamp_values(repo))
        monkeypatch.setattr(booley, "_build_commit", stamp, raising=False)
        monkeypatch.setitem(sys.modules, "booley._build_commit", stamp)
        monkeypatch.setattr(
            booley,
            "version_attribution",
            VersionAttribution(
                version="4.5.6",
                origin=VersionOrigin.DISTRIBUTION,
                distribution_name="booley-rtl",
            ),
        )
        monkeypatch.setattr(
            VersionAttribution,
            "source_git_metadata",
            lambda _self: pytest.fail("wheel attribution must not inspect Git"),
        )
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: pytest.fail("wheel must not run subprocesses")
        )

        assert cli._version_string() == f"booley {__version__} ({commit}, 2026-10-26)"

    def test_release_readers_accept_stamp_with_commit_time(self, repo: Path, monkeypatch):
        from booley.runtime import image_build_contracts

        self._commit_at(repo, "2026-10-26T12:00:00+00:00")
        commit = write_build_stamp(repo, profile=BuildProfile.OFFICIAL_RELEASE)
        wheel = _write_wheel(repo)
        values = self._stamp_values(repo)
        monkeypatch.setattr(
            image_build_contracts, "_embedded_stamp_path", lambda: stamp_path(repo)
        )

        assert values.get("COMMIT_TIME") == "2026-10-26T12:00:00Z"
        assert build_stamp_module.wheel_embedded_commit(wheel) == commit
        assert wheel_embedded_source_fingerprint(wheel) == resolve_wheel_source_fingerprint(repo)
        contracts = image_build_contracts.embedded_image_build_contracts()
        assert contracts.runtime_base == values["RUNTIME_BASE_CONTRACT"]
        assert contracts.standard_substrate == values["STANDARD_SUBSTRATE_CONTRACT"]

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

    def test_stamp_constants_reads_literals_and_first_assignment_wins(self):
        source = "A = 'one'\nA = 'two'\nB, C = 1, 2\nD = 3\nE: int = 4\n"

        assert build_stamp_module.stamp_constants(source, {"A", "B", "D", "MISSING"}) == {
            "A": "one",
            "D": 3,
        }

    @pytest.mark.parametrize("source", ["A = (\n", "A = 'a' * 64\n"])
    def test_stamp_constants_rejects_unparseable_or_non_literal_source(self, source: str):
        with pytest.raises(ValueError):
            build_stamp_module.stamp_constants(source, {"A"})

    @pytest.mark.parametrize("body", ["COMMIT = (\n", "COMMIT = 3\n", "OTHER = 'x'\n"])
    def test_rejects_invalid_wheel_commit(self, tmp_path: Path, body: str):
        wheel = tmp_path / "invalid.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("booley/_build_commit.py", body)

        with pytest.raises(ValueError, match="wheel build"):
            build_stamp_module.wheel_embedded_commit(wheel)

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

    @pytest.mark.parametrize("fail", [False, True])
    def test_context_manager_always_removes_the_stamp(self, repo: Path, fail: bool):
        """Leaving it behind makes the checkout claim a commit it doesn't have."""

        def build():
            with runtime_image_build_stamp(repo):
                namespace = {}
                exec(stamp_path(repo).read_text(), namespace)
                assert namespace["DEVELOPMENT_CONTEXT_SHA256"] == ""
                assert namespace["OFFICIAL_RELEASE"] is False
                if fail:
                    raise RuntimeError("build blew up")

        if fail:
            with pytest.raises(RuntimeError):
                build()
        else:
            build()
        assert not stamp_path(repo).exists()
        assert not development_context_path(repo).exists()


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
        # A copy, not the live checkout: the render step writes into its root.
        root = tmp_path / "checkout"
        copy_source_checkout(
            SOURCE_ROOT,
            root,
            "src/booley",
            "crates/bwave",
            ".github/scripts",
            *build_stamp_module._DEVELOPMENT_CONTEXT_FILES,
        )
        shutil.rmtree(root / PACKAGED_DOCS_RELATIVE_DIR)

        with monkeypatch.context() as build_patch:
            build_patch.setattr(build_stamp_module, "stamp_path", lambda _root: stamp_file)
            build_patch.setattr(
                build_stamp_module,
                "development_context_path",
                lambda _root: context_file,
            )
            write_build_stamp(root)

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
            for source in SHIPPED_DOCS:
                assert (extracted / source).read_bytes() == (root / source).read_bytes()
                assert _copy_path(extracted, source).read_bytes() == _rendered(root, source)

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
            seen.append(
                stamp_path(repo).is_file()
                and all(_copy_path(repo, source).is_file() for source in SHIPPED_DOCS)
            )
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

    @pytest.mark.parametrize("body", ["COMMIT = (\n", "COMMIT = 'a' + 'b'\n"])
    def test_unparseable_verified_stamp_fails_the_wheel_build(self, repo: Path, body: str):
        stamp_path(repo).write_text(body, encoding="utf-8")
        ctx = InitContext()

        assert not init_docker_image._docker_build_wheel(ctx, repo, preserve_stamp=True)
        assert ctx.results[-1].detail == "wheel build failed"

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


@pytest.mark.parametrize("official", [False, None])
def test_runtime_image_stamp_has_actionable_recovery(repo, monkeypatch, official):
    write_build_stamp(repo, profile=BuildProfile.RUNTIME_IMAGE)
    stamp = types.ModuleType("booley._build_commit")
    exec(stamp_path(repo).read_text(), stamp.__dict__)
    if official is None:
        del stamp.OFFICIAL_RELEASE
    monkeypatch.setitem(sys.modules, "booley._build_commit", stamp)
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
def test_invalid_or_official_context_has_no_runtime_recovery(monkeypatch, context_hash, official):
    stamp = types.ModuleType("booley._build_commit")
    stamp.DEVELOPMENT_CONTEXT_SHA256 = context_hash
    stamp.PAYLOAD_FINGERPRINT = "a" * 64
    stamp.OFFICIAL_RELEASE = official
    monkeypatch.setitem(sys.modules, "booley._build_commit", stamp)
    with (
        pytest.raises(ValueError, match="invalid build-context provenance"),
        extracted_development_context(),
    ):
        pytest.fail("invalid provenance must fail closed")
