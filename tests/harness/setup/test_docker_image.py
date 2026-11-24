"""Tests for the Sandbox Image init step: staleness, release refresh, pull and local build.

The step runs against the shared fake of its two command boundaries, the Docker CLI
and the wheel build (``tests/harness/sandbox_image_docker_fake.py``), so the real
fingerprints, labels, capacity plan, runtime-base acquisition and build runner execute.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import booley
from booley.harness import init_cmd
from booley.harness.setup import docker_image as init_docker_image
from booley.harness.setup.common import InitContext
from booley.runtime import host_base_acquisition as owner
from booley.runtime.build_stamp import (
    resolve_payload_fingerprint,
    resolve_wheel_source_fingerprint,
)
from booley.runtime.docker_build import DockerBuildResult
from booley.runtime.packaged_docs import SHIPPED_DOCS
from tests.git_support import git, repository
from tests.harness.sandbox_image_docker_fake import (
    Build,
    Reply,
    SandboxImageDocker,
    serve_sandbox_image_docker,
)
from tests.subprocess_budget import run_bounded_child

VERSION = "0.2.6"
SANDBOX = init_docker_image.DOCKER_IMAGE
BASE = init_docker_image.LOCAL_RUNTIME_BASE_IMAGE
RELEASE_TAG = f"ghcr.io/boldaxolotl/booley-sandbox:{VERSION}"
FINGERPRINT = init_docker_image.LABEL_FINGERPRINT
OCI_VERSION = init_docker_image.LABEL_VERSION
WHEEL_SOURCE = init_docker_image.LABEL_WHEEL_SOURCE_FINGERPRINT
#: Label placeholders that resolve to the checkout's own current fingerprints.
CURRENT = "<current>"
CURRENT_WHEEL = "<current-wheel>"

#: The tracked files the local build reads; the wheel step writes to its checkout, so the
#: tests build in a copy of these, never in the live repository.
_CHECKOUT_FILES = (
    ".dockerignore",
    "VERSION",
    "pyproject.toml",
    "src/booley/data/docker",
    "src/booley/data/edalize/verible.py",
    "src/booley/runtime/docker_base_contract.py",
    "crates/bwave/Cargo.toml",
    "crates/bwave/Cargo.lock",
    "crates/bwave/src",
    "crates/bwave/schema",
    "crates/bwave/docs",
    *SHIPPED_DOCS,
)


@pytest.fixture
def docker(monkeypatch: pytest.MonkeyPatch) -> SandboxImageDocker:
    return serve_sandbox_image_docker(monkeypatch)


@pytest.fixture(scope="session")
def _checkout_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    repo = Path(__file__).resolve().parents[3]
    listed = run_bounded_child(
        ["git", "ls-files", "-z", "--", *_CHECKOUT_FILES], cwd=repo, check=True
    ).stdout
    template = tmp_path_factory.mktemp("checkout-template")
    for name in filter(None, listed.split("\0")):
        (template / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(repo / name, template / name)
    return template


@pytest.fixture
def source_checkout(
    _checkout_template: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """A plain copy of the files the local build reads, safe for the wheel step to write to."""
    root = tmp_path / "checkout"
    shutil.copytree(_checkout_template, root)
    # No enclosing repository may supply a source revision to a copy that has none.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    return root


def _seed_source(root: Path) -> None:
    """Minimal checkout the payload fingerprint hashes (no Dockerfile: a build stops early)."""
    pkg = root / "src" / "booley"
    pkg.mkdir(parents=True)
    (pkg / "incontainer_register.py").write_text("x = 1\n", encoding="utf-8")
    (root / ".dockerignore").write_text(".git\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='booley'\n", encoding="utf-8")
    (root / "VERSION").write_text(f"{VERSION}\n", encoding="utf-8")
    bwave = root / "crates" / "bwave"
    (bwave / "src").mkdir(parents=True)
    (bwave / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
    (bwave / "Cargo.toml").write_text("[package]\n", encoding="utf-8")


def _point_at(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Make *root* the Booley tree the step inspects; the install location is the host's."""
    monkeypatch.setattr(
        init_docker_image, "docker_data_dir", lambda: root / "src" / "booley" / "data" / "docker"
    )


def _run_step(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    check_only: bool = False,
    force: bool = False,
    version: str = VERSION,
    **step_kwargs: bool,
) -> InitContext:
    """Run the Docker image step for the Booley tree at *root* (empty: a release install)."""
    monkeypatch.setattr(booley, "__version__", version)
    _point_at(monkeypatch, root)
    ctx = InitContext(project_root=root, check_only=check_only, force=force)
    # private: no public function reaches the step with a Docker client to fake. Its only
    # caller, ``image_lifecycle``, runs it inside a plan of the whole Project image chain.
    init_docker_image._step_docker_image(ctx, **step_kwargs)
    return ctx


def _verdict(ctx: InitContext) -> tuple[str, str]:
    return ctx.results[-1].status, ctx.results[-1].detail


def _add_image(docker: SandboxImageDocker, name: str, root: Path, labels: dict[str, str]) -> None:
    """Add an image whose ``CURRENT`` and ``CURRENT_WHEEL`` labels name *root*'s fingerprints."""
    current = {
        CURRENT: resolve_payload_fingerprint(root),
        CURRENT_WHEEL: resolve_wheel_source_fingerprint(root),
    }
    docker.add(name, {key: current.get(value, value) or "" for key, value in labels.items()})


def _build_tags(docker: SandboxImageDocker, since: int = 0) -> list[str]:
    return [
        call[call.index("-t") + 1]
        for call in docker.calls[since:]
        if call[:2] == ["docker", "build"]
    ]


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        pytest.param(
            {"parent_artifact": "sha256:" + "a" * 64},
            [
                [
                    "--label",
                    f"{init_docker_image.LABEL_PARENT_ARTIFACT_KIND}="
                    f"{init_docker_image.PARENT_ARTIFACT_LOCAL_IMAGE_ID}",
                ],
                ["--label", f"{init_docker_image.LABEL_PARENT_ARTIFACT}=sha256:{'a' * 64}"],
            ],
            id="parent-labels",
        ),
        pytest.param(
            {"labels": (("io.booley.wheel.sha256", "f" * 64),)},
            [["--label", "io.booley.wheel.sha256=" + "f" * 64]],
            id="extra-labels",
        ),
        pytest.param(
            {"build_contexts": (("booley-runtime-base", "docker-image://base-image"),)},
            [["--build-context", "booley-runtime-base=docker-image://base-image"]],
            id="build-contexts",
        ),
        pytest.param(
            {"image": SANDBOX},
            [
                ["--build-arg", "BOOLEY_SOURCE_REVISION={revision}"],
                ["--build-arg", "BOOLEY_SOURCE_UPDATED_AT=2026-08-10T10:00:00+02:00"],
                ["--build-arg", "BOOLEY_VERSION={version}"],
                ["--build-arg", "BOOLEY_PAYLOAD_FINGERPRINT={payload}"],
            ],
            id="booley-sandbox-provenance",
        ),
    ],
)
def test_build_command_carries_provenance_labels_and_contexts(
    tmp_path, monkeypatch, fields, expected
) -> None:
    repo = repository(tmp_path / "repo")
    (repo / "VERSION").write_text("1.2.3\n", encoding="utf-8")
    (repo / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    git(repo, "add", ".")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-08-10T10:00:00+02:00")
    git(repo, "commit", "-q", "-m", "seed")
    facts = {
        "revision": git(repo, "rev-parse", "--short", "HEAD"),
        "version": "1.2.3",
        "payload": resolve_payload_fingerprint(repo),
    }
    fields = {"image": "project-image", **fields}
    # private: extra labels and a non-default image come only from ``image_lifecycle`` plans,
    # which no run of the step reaches, so the command is built from the spec directly.
    spec = init_docker_image._DockerBuildSpec(
        dockerfile=repo / "Dockerfile", context=repo, exists=False, **fields
    )
    command = init_docker_image._docker_build_command(spec)

    pairs = [command[i : i + 2] for i in range(len(command) - 1)]
    for fragment in expected:
        assert [part.format(**facts) for part in fragment] in pairs
    built_at = [pair for pair in pairs if pair[1].startswith("BOOLEY_IMAGE_BUILT_AT=")]
    assert bool(built_at) is (fields["image"] == SANDBOX)


STALE = ("warn", "would rebuild (stale)")
PRESENT = ("current", "already present")


@pytest.mark.parametrize(
    ("labels", "force", "expected", "df"),
    [
        pytest.param({}, False, STALE, 0, id="no-label"),
        pytest.param(
            {FINGERPRINT: CURRENT, OCI_VERSION: VERSION}, False, PRESENT, 1, id="current"
        ),
        pytest.param(
            {FINGERPRINT: CURRENT, OCI_VERSION: "0.1.0"}, False, STALE, 0, id="old-version"
        ),
        pytest.param({FINGERPRINT: "other", OCI_VERSION: VERSION}, False, STALE, 0, id="drifted"),
        pytest.param({FINGERPRINT: f"pulled:{VERSION}"}, False, PRESENT, 1, id="pulled-current"),
        pytest.param({FINGERPRINT: "pulled:0.1.0"}, False, STALE, 0, id="pulled-old"),
        pytest.param({FINGERPRINT: "other"}, True, PRESENT, 0, id="forced"),
        pytest.param(None, False, ("warn", "would build"), 0, id="absent"),
    ],
)
def test_checkout_image_is_rebuilt_only_when_stale(
    tmp_path, monkeypatch, docker, labels, force, expected, df
) -> None:
    _seed_source(tmp_path)
    if labels is not None:
        _add_image(docker, SANDBOX, tmp_path, labels)

    # Distribution metadata says 0.1.0; the checkout's VERSION file (0.2.6) must win.
    ctx = _run_step(monkeypatch, tmp_path, check_only=True, force=force, version="0.1.0")

    assert _verdict(ctx) == expected
    assert docker.count("pull") == 0
    assert docker.count("system", "df") == df


@pytest.mark.parametrize(
    ("checkout", "labels", "expected"),
    [
        pytest.param(False, {FINGERPRINT: "whatever"}, None, id="no-checkout"),
        pytest.param(True, {}, None, id="unlabeled"),
        pytest.param(True, {FINGERPRINT: ""}, None, id="empty-label"),
        pytest.param(True, None, None, id="image-absent"),
        pytest.param(True, {FINGERPRINT: "pulled:0.1.0"}, None, id="pulled"),
        pytest.param(True, {FINGERPRINT: CURRENT, OCI_VERSION: VERSION}, False, id="current"),
        pytest.param(True, {FINGERPRINT: CURRENT, OCI_VERSION: "0.1.0"}, True, id="old-version"),
        pytest.param(True, {FINGERPRINT: "other"}, True, id="drifted"),
        pytest.param(
            True, {FINGERPRINT: CURRENT, WHEEL_SOURCE: "b" * 64}, True, id="wheel-source-differs"
        ),
        pytest.param(
            True, {FINGERPRINT: "other", WHEEL_SOURCE: CURRENT_WHEEL}, False, id="wheel-matches"
        ),
    ],
)
def test_source_fingerprint_mismatch_verdicts(
    tmp_path, monkeypatch, docker, checkout, labels, expected
) -> None:
    if checkout:
        _seed_source(tmp_path)
    if labels is not None:
        _add_image(docker, "img", tmp_path, labels)
    _point_at(monkeypatch, tmp_path)

    assert init_docker_image.source_fingerprint_mismatch("img") is expected


@pytest.mark.parametrize(
    ("labels", "options", "check_only", "expected", "present", "absent", "pulls"),
    [
        pytest.param(
            {FINGERPRINT: "pulled:0.2.3"},
            {},
            False,
            ("ok", "pulled"),
            (),
            (),
            1,
            id="old-release-pulled",
        ),
        pytest.param(
            {FINGERPRINT: "pulled:0.2.3"},
            {"pull": Reply(1, stderr="not found")},
            False,
            ("warn", "compatible image pull failed"),
            (
                "v0.2.3",
                f"Booley v{VERSION} requires sandbox image v{VERSION}",
                "may be incompatible",
            ),
            (),
            1,
            id="pull-fails",
        ),
        pytest.param(
            {FINGERPRINT: f"pulled:{VERSION}"},
            {},
            False,
            ("current", "already present"),
            (),
            (),
            0,
            id="matching-release",
        ),
        pytest.param(
            {FINGERPRINT: "abc123", OCI_VERSION: "0.2.3"},
            {},
            False,
            ("ok", "pulled"),
            (),
            (),
            1,
            id="oci-version-without-pull-stamp",
        ),
        pytest.param(
            {},
            {},
            True,
            ("warn", "would pull compatible image"),
            ("unknown version", f"would pull the Booley v{VERSION} image"),
            (),
            0,
            id="check-only-unverifiable",
        ),
        pytest.param(
            {FINGERPRINT: "pulled:0.2.3"},
            {"tag": Reply(raises=subprocess.TimeoutExpired(["docker", "tag"], 30))},
            False,
            ("warn", "compatible image pull failed"),
            ("docker tag timed out after 30 seconds",),
            ("pull timed out",),
            1,
            id="tag-times-out",
        ),
    ],
)
def test_release_install_refreshes_image_to_its_version(
    tmp_path,
    monkeypatch,
    capsys,
    docker,
    labels,
    options,
    check_only,
    expected,
    present,
    absent,
    pulls,
) -> None:
    # An empty tree is a pip install: no checkout fingerprint, but the release pins the image.
    docker.add(SANDBOX, labels)
    for name, reply in options.items():
        setattr(docker, name, reply)

    ctx = _run_step(monkeypatch, tmp_path, check_only=check_only)

    output = capsys.readouterr().out
    assert _verdict(ctx) == expected
    assert all(fragment in output for fragment in present)
    assert not any(fragment in output for fragment in absent)
    assert docker.count("pull") == pulls
    if pulls:
        assert ["docker", "pull", RELEASE_TAG] in docker.calls


@pytest.mark.parametrize(
    ("override", "pull", "timeout", "fragment"),
    [
        pytest.param(
            None,
            Reply(1),
            init_docker_image.DEFAULT_IMAGE_PULL_TIMEOUT_S,
            "docker exited 1",
            id="default",
        ),
        pytest.param(
            "900",
            Reply(raises=subprocess.TimeoutExpired(["docker", "pull"], 900)),
            900,
            "BOOLEY_IMAGE_PULL_TIMEOUT",
            id="override",
        ),
        pytest.param(
            "never",
            Reply(1),
            init_docker_image.DEFAULT_IMAGE_PULL_TIMEOUT_S,
            "invalid BOOLEY_IMAGE_PULL_TIMEOUT='never'",
            id="invalid",
        ),
    ],
)
def test_registry_pull_timeout(
    tmp_path, monkeypatch, capsys, docker, override, pull, timeout, fragment
) -> None:
    monkeypatch.delenv("BOOLEY_IMAGE_PULL_TIMEOUT", raising=False)
    if override is not None:
        monkeypatch.setenv("BOOLEY_IMAGE_PULL_TIMEOUT", override)
    docker.pull = pull

    _run_step(monkeypatch, tmp_path)

    assert [sent["timeout"] for sent in docker.sent("pull")] == [timeout]
    assert fragment in capsys.readouterr().out


def test_checkout_builds_locally_when_pull_is_unavailable(
    tmp_path, monkeypatch, capsys, docker
) -> None:
    _seed_source(tmp_path)
    docker.pull = Reply(1)

    ctx = _run_step(monkeypatch, tmp_path)

    assert docker.count("pull") == 1
    assert (
        "pre-built image unavailable, rebuilding the Sandbox Image locally"
        in capsys.readouterr().out
    )
    # The seeded tree has no Dockerfile, so the local build starts and stops there.
    assert _verdict(ctx) == ("err", "Dockerfile missing")


def test_staged_pull_leaves_canonical_adoption_to_lifecycle(docker) -> None:
    # private: ``image_lifecycle`` is the only caller of a staged pull and runs it inside a
    # full plan of the Project image chain.
    assert init_docker_image._try_pull_image("0.2.0", adopt=False) is True

    assert docker.count("pull") == 1
    assert docker.count("tag") == 0


@pytest.mark.parametrize(
    ("df", "present", "absent"),
    [
        pytest.param(
            "Images\t2GB\t1GB\nBuild Cache\t29.3GB\t29.3GB\n",
            (
                "docker build cache: 29.3GB",
                "(29.3GB reclaimable)",
                "docker builder prune",
                "evict layers",
            ),
            (),
            id="large",
        ),
        pytest.param(
            "Build Cache\t2.0GB\t2.0GB\n", ("docker build cache: 2.0GB",), ("prune",), id="small"
        ),
        pytest.param("Build Cache\t10GiB\t1GiB\n", ("docker builder prune",), (), id="gibibytes"),
        pytest.param(
            "Build Cache\t512MB\t0B\n", ("docker build cache: 512MB",), ("prune",), id="megabytes"
        ),
        pytest.param("Build Cache\t2TB\t0B\n", ("docker builder prune",), (), id="terabytes"),
        pytest.param(
            "Build Cache\tN/A\tN/A\n", ("docker build cache: N/A",), ("prune",), id="unparseable"
        ),
        pytest.param(None, (), ("docker build cache",), id="docker-fails"),
    ],
)
def test_current_image_reports_build_cache(
    tmp_path, monkeypatch, capsys, docker, df, present, absent
) -> None:
    _seed_source(tmp_path)
    _add_image(docker, SANDBOX, tmp_path, {FINGERPRINT: CURRENT, OCI_VERSION: VERSION})
    docker.system_df = Reply(stdout=df) if df is not None else Reply(1)

    ctx = _run_step(monkeypatch, tmp_path)

    output = capsys.readouterr().out
    assert _verdict(ctx) == PRESENT
    assert all(fragment in output for fragment in present)
    assert not any(fragment in output for fragment in absent)


def _local_build(monkeypatch, root: Path, **kwargs: bool) -> InitContext:
    """The local build the way ``image_lifecycle`` runs it: a missing image, no pull."""
    return _run_step(monkeypatch, root, allow_pull=False, rebuild_runtime_base=False, **kwargs)


@pytest.mark.parametrize(
    ("runs", "prepare", "status", "detail", "builds"),
    [
        pytest.param(1, None, "ok", "built", [BASE, SANDBOX], id="base-absent"),
        pytest.param(2, None, "ok", "built", [SANDBOX], id="base-reused"),
        pytest.param(
            1,
            lambda docker: docker.failing.add(BASE),
            "err",
            "Sandbox Image capacity check failed: cannot inspect",
            [],
            id="capacity-check-cannot-inspect",
        ),
        pytest.param(
            1,
            lambda docker: docker.after.update(wheel=lambda: docker.retag(BASE)),
            "err",
            "runtime-base identity changed",
            [BASE],
            id="base-retagged-before-candidate",
        ),
        pytest.param(
            1,
            lambda docker: docker.after.update({SANDBOX: lambda: docker.retag(BASE)}),
            "err",
            "runtime-base identity changed",
            [BASE, SANDBOX],
            id="base-retagged-during-candidate",
        ),
        pytest.param(
            1,
            lambda docker: docker.after.update(wheel=lambda: docker.failing.add(BASE)),
            "err",
            "cannot inspect Docker image",
            [BASE],
            id="base-unreadable-before-candidate",
        ),
        pytest.param(
            1,
            lambda docker: docker.after.update({SANDBOX: lambda: docker.failing.add(BASE)}),
            "err",
            "daemon down",
            [BASE, SANDBOX],
            id="base-unreadable-after-candidate",
        ),
    ],
)
def test_local_build_guards_the_runtime_base(
    source_checkout, monkeypatch, docker, runs, prepare, status, detail, builds
) -> None:
    if prepare is not None:
        prepare(docker)
    for _ in range(runs):
        docker.images.pop(SANDBOX, None)
        start = len(docker.calls)
        ctx = _local_build(monkeypatch, source_checkout)

    assert _build_tags(docker, start) == builds
    assert ctx.results[-1].status == status
    assert detail in ctx.results[-1].detail
    if status == "ok":
        base_id = docker.images[BASE]["Id"]
        candidate = next(
            c for c in docker.calls[start:] if c[:2] == ["docker", "build"] and SANDBOX in c
        )
        pairs = [candidate[i : i + 2] for i in range(len(candidate) - 1)]
        assert ["--build-context", f"booley-runtime-base=docker-image://{BASE}"] in pairs
        assert ["--build-arg", f"BOOLEY_RUNTIME_BASE_IMAGE={base_id}"] in pairs
        assert [
            "--label",
            f"{init_docker_image.LABEL_PARENT_ARTIFACT}={base_id}",
        ] in pairs
        assert [
            "--label",
            f"{init_docker_image.LABEL_PARENT_ARTIFACT_KIND}={init_docker_image.PARENT_ARTIFACT_LOCAL_IMAGE_ID}",
        ] in pairs


_PYVER = f"{sys.version_info.major}.{sys.version_info.minor}"


@pytest.mark.parametrize(
    ("prepare", "detail", "present", "absent"),
    [
        pytest.param(
            lambda docker, root: docker.builds.update(
                {BASE: Build(raises=FileNotFoundError("no docker"))}
            ),
            "no docker",
            ("docker build failed: no docker",),
            (),
            id="docker-cli-missing",
        ),
        pytest.param(
            lambda docker, root: docker.builds.update({BASE: Build(1, b"boom\n")}),
            "runtime-base build failed",
            ("stable runtime-base build failed", "boom"),
            (),
            id="runtime-base-exits-1",
        ),
        pytest.param(
            lambda docker, root: docker.builds.update(
                {SANDBOX: Build(1, b"ERROR: checksum mismatch\n")}
            ),
            "build failed",
            ("docker build failed", "ERROR: checksum mismatch"),
            (),
            id="candidate-exits-1",
        ),
        pytest.param(
            lambda docker, root: setattr(
                docker,
                "wheel",
                Reply(1, stdout="* Creating isolated environment\nboom: real cause"),
            ),
            "wheel build failed",
            ("boom: real cause",),
            (),
            id="wheel-reports-on-stdout",
        ),
        pytest.param(
            lambda docker, root: setattr(
                docker, "wheel", Reply(1, stderr="stderr side of the story")
            ),
            "wheel build failed",
            ("stderr side of the story",),
            (),
            id="wheel-reports-on-stderr",
        ),
        pytest.param(
            lambda docker, root: setattr(
                docker,
                "wheel",
                Reply(
                    1,
                    stdout="* Creating isolated environment: venv+pip...\n"
                    "The virtual environment was not created successfully because "
                    "ensurepip is not available.",
                ),
            ),
            "wheel build failed",
            (f"apt install python{_PYVER}-venv", "--no-isolation"),
            (),
            id="wheel-ensurepip-missing",
        ),
        pytest.param(
            lambda docker, root: setattr(
                docker, "wheel", Reply(1, stdout="No module named build")
            ),
            "wheel build failed",
            ("pip install build",),
            (),
            id="wheel-build-module-missing",
        ),
        pytest.param(
            lambda docker, root: (root / SHIPPED_DOCS[0]).unlink(),
            "wheel build failed",
            (SHIPPED_DOCS[0],),
            ("pip install build",),
            id="shipped-doc-missing",
        ),
    ],
)
def test_local_build_reports_build_failures(
    source_checkout, monkeypatch, capsys, docker, prepare, detail, present, absent
) -> None:
    prepare(docker, source_checkout)

    ctx = _local_build(monkeypatch, source_checkout)

    output = capsys.readouterr().out
    assert (ctx.results[-1].name, ctx.results[-1].status) == ("docker_image", "err")
    assert detail in ctx.results[-1].detail
    # Each message and the retained Docker output are shown once.
    assert all(output.count(fragment) == 1 for fragment in present)
    assert not any(fragment in output for fragment in absent)


@pytest.mark.parametrize(
    ("output", "force"),
    [
        pytest.param(b">>> Building \x81 from source\n", False, id="byte-outside-cp1252"),
        pytest.param(b">>> ok\n", True, id="forced-keeps-layer-cache"),
    ],
)
def test_local_build_streams_docker_output_as_utf8(
    source_checkout, monkeypatch, docker, output, force
) -> None:
    docker.builds[SANDBOX] = Build(0, output)

    ctx = _local_build(monkeypatch, source_checkout, force=force)

    assert _verdict(ctx) == ("ok", "built")
    builds = docker.sent("build")
    assert len(builds) == 2
    # Decoding the pipe with the locale codec (cp1252 on Windows) once killed a long rebuild.
    assert all(sent["encoding"] == "utf-8" and sent["errors"] == "replace" for sent in builds)
    assert not any(
        "--no-cache" in call for call in docker.calls if call[:2] == ["docker", "build"]
    )


def test_runtime_base_build_timeout_keeps_docker_image_records(
    tmp_path, monkeypatch, capsys
) -> None:
    # A real build timeout needs a real wait, so ``acquire`` reports it directly. private: the
    # runtime-base report is reached only through the local build of a whole Sandbox Image.
    monkeypatch.delenv("BOOLEY_IMAGE_BUILD_TIMEOUT", raising=False)
    failure = owner.RuntimeBaseBuildError(
        "build failed", DockerBuildResult(None, timed_out=True, diagnostics=("slow",))
    )

    def acquire(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(init_docker_image, "acquire", acquire)
    ctx = InitContext(project_root=tmp_path)

    assert init_docker_image._acquire_runtime_base(ctx, tmp_path, rebuild=False) is None

    assert _verdict(ctx) == ("err", "build timed out")
    output = capsys.readouterr().out
    assert (
        "docker build timed out after 2h 00m (override with BOOLEY_IMAGE_BUILD_TIMEOUT)" in output
    )
    assert "slow" in output


def test_timeout_records_build_failure(monkeypatch, tmp_path, capsys) -> None:
    # A real build timeout needs a real wait, so the runner reports it directly. private: the
    # build step is reached only through a whole local build.
    monkeypatch.setattr(
        init_docker_image,
        "run_docker_build",
        lambda *_args, **_kwargs: DockerBuildResult(
            None, timed_out=True, diagnostics=("last build output",)
        ),
    )
    ctx = InitContext(project_root=tmp_path)
    build = init_docker_image._DockerBuildSpec(
        dockerfile=tmp_path / "Dockerfile", context=tmp_path, exists=False
    )

    assert init_docker_image._docker_build_image(ctx, build) is None

    assert ctx.results[-1].status == "err"
    assert ctx.results[-1].detail == "build timed out"
    assert "last build output" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# build.sh Python-selection guard (source invariant)
# ---------------------------------------------------------------------------
# The guard picks an interpreter that ships the *pypa* `build` module. Its probe
# MUST run under the same `-P` isolation the wheel step uses: without it, `-c`
# puts the cwd on sys.path and the repo-root `build/` setuptools artifact dir (a
# bare namespace package) satisfies `import build`, so the probe green-lights a
# Python whose `-P -m build` then fails with "No module named build" — a stale
# wheel gets baked into the image. These are source invariants: cheap guards that
# fail loudly if the `-P` (or the freshness assertion) is ever dropped.

_BUILD_SH = Path(__file__).resolve().parents[3] / "src" / "booley" / "data" / "docker" / "build.sh"


def test_build_sh_probe_uses_dash_p_isolation() -> None:
    """The candidate-Python probe must use `-P`, matching the wheel invocation."""
    text = _BUILD_SH.read_text(encoding="utf-8")
    # The probe line runs each candidate against `import build` inside the loop.
    probe = next(
        (l for l in text.splitlines() if "import build" in l and '"$cand"' in l),
        None,
    )
    assert probe is not None, "guard probe line not found in build.sh"
    assert re.search(r'"\$cand"\s+-P\s+-c', probe), (
        f"probe must isolate with -P so the repo-root build/ dir cannot shadow "
        f"the pypa build module; got: {probe.strip()!r}"
    )


def test_build_sh_asserts_a_fresh_wheel() -> None:
    """A freshness check must guard against the docker COPY baking a stale wheel."""
    text = _BUILD_SH.read_text(encoding="utf-8")
    assert "-newer" in text and "booley_rtl-*.whl" in text, (
        "build.sh must assert the wheel was (re)written this run before the docker build COPYs it"
    )


def test_build_sh_removes_stale_staging_tree() -> None:
    """Deleted packages must not survive in setuptools' incremental build/lib."""
    text = _BUILD_SH.read_text(encoding="utf-8")
    assert 'rm -rf "$BOOLEY_ROOT/build"' in text


def test_manual_image_build_scripts_run_shared_capacity_preflight() -> None:
    """Every shipped manual Sandbox Image build must use the shared guard."""
    scripts = (_BUILD_SH, _BUILD_SH.with_name("build-riscv.sh"))
    for script in scripts:
        text = script.read_text(encoding="utf-8")
        assert "booley.runtime.docker_capacity" in text
        assert "run_docker_build " in text


def _seed_riscv_script_tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    docker_dir = root / "src" / "booley" / "data" / "docker"
    docker_dir.mkdir(parents=True)
    for name in (
        "build.sh",
        "build-riscv.sh",
        "Dockerfile.base",
        "Dockerfile.substrate",
        "Dockerfile.wheel",
        "Dockerfile.riscv",
    ):
        source = _BUILD_SH.with_name(name)
        target = docker_dir / name
        target.write_bytes(source.read_bytes())
        target.chmod(0o755 if name.endswith(".sh") else 0o644)
    (root / "VERSION").write_text("0.0.0\n", encoding="utf-8")
    return docker_dir


def _fake_riscv_build_tools(tmp_path: Path, *, build_marker: Path, plan_marker: Path) -> Path:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python"
    fake_python.write_text(
        """#!/bin/bash
set -e
case "$*" in
  *"import build"*) exit 0 ;;
  *"sys.executable"*) echo "$0" ;;
  *"-m build"*)
    while [ "$#" -gt 0 ]; do
      if [ "$1" = "--outdir" ]; then shift; mkdir -p "$1"; touch "$1/booley_rtl-0.whl"; exit 0; fi
      shift
    done
    exit 2 ;;
  *"booley.runtime.docker_capacity"*|*"booley.runtime.host_base_acquisition"*)
    while [ "$#" -gt 0 ]; do
      if [ "$1" = "--plan-file" ]; then shift; plan="$1"; break; fi
      shift
    done
    count=$(grep -o managed_image "$plan" | wc -l)
    printf '%s' "$count" >"$PLAN_MARKER"
    [ "$count" -eq 5 ] || exit 8
    exit 9 ;;
  *) echo fingerprint ;;
esac
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        f"#!/bin/bash\ntouch {build_marker}\nexit 0\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)
    return fake_bin


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell-script execution")
def test_riscv_script_refuses_five_image_plan_before_first_docker_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    docker_dir = _seed_riscv_script_tree(tmp_path)
    build_marker = tmp_path / "docker-build-ran"
    plan_marker = tmp_path / "plan-count"
    fake_bin = _fake_riscv_build_tools(
        tmp_path,
        build_marker=build_marker,
        plan_marker=plan_marker,
    )
    fake_python = fake_bin / "python"
    monkeypatch.setenv("PYTHON", str(fake_python))
    monkeypatch.setenv("PLAN_MARKER", str(plan_marker))
    monkeypatch.setenv("PATH", f"{fake_bin}:/usr/bin:/bin")

    result = subprocess.run(
        [str(docker_dir / "build-riscv.sh")],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 9
    assert plan_marker.read_text(encoding="utf-8") == "5"
    assert not build_marker.exists()


# ---------------------------------------------------------------------------
# Step-number drift in comments (fpu F-12)
# ---------------------------------------------------------------------------
# Banner numbers are allocated at print time from the steps that actually run
# (InitContext.step_banner), so a hardcoded "Step 9b" in a section comment goes
# stale the moment a step is added, skipped, or retired. Steps are named by
# their record key instead.

_INIT_MODULES = (
    "init_cmd.py",
    "setup/docker_image.py",
    "setup/git_hooks.py",
)


def test_init_sources_carry_no_hardcoded_step_numbers() -> None:
    src = Path(init_cmd.__file__).parent
    offenders = []
    for name in _INIT_MODULES:
        for lineno, line in enumerate(
            src.joinpath(name).read_text(encoding="utf-8").splitlines(), 1
        ):
            # `Step 0`-`Step 4` are the booley-setup skill's own steps (a real,
            # user-facing numbering) — only init's internal numbering drifts.
            for m in re.finditer(r"Step (\d+)([a-z]?)", line):
                if int(m.group(1)) > 4 or m.group(2):
                    offenders.append(f"{name}:{lineno}: {line.strip()}")
    assert not offenders, "hardcoded init step numbers drift (F-12):\n" + "\n".join(offenders)


@pytest.mark.parametrize("raises", [False, True])
def test_build_timeout_uses_human_duration_and_preserves_record(
    monkeypatch, tmp_path, capsys, raises
):
    # A 3661 s timeout renders as ``1h 01m``; no real wait can reach it, so the runner is
    # replaced. private: the build step is reached only through a whole local build.
    def build(*_a, **_k):
        if raises:
            raise subprocess.TimeoutExpired("docker build", 3661)
        return DockerBuildResult(None, timed_out=True)

    monkeypatch.setenv("BOOLEY_IMAGE_BUILD_TIMEOUT", "3661")
    monkeypatch.setattr(init_docker_image, "run_docker_build", build)
    ctx = InitContext(project_root=tmp_path)
    spec = init_docker_image._DockerBuildSpec(
        dockerfile=tmp_path / "Dockerfile", context=tmp_path, exists=False
    )
    assert init_docker_image._docker_build_image(ctx, spec) is None
    assert "timed out after 1h 01m" in capsys.readouterr().out
    assert ctx.results[-1].detail == "build timed out"
