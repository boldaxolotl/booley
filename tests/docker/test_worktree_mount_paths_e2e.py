"""Actual Docker bind mounts prove host and Sandbox worktree identity."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest
from tests.docker.isolation.container_names import next_ci_container_name


def _run(args: list[str], *, cwd: Path | None = None):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=90, check=False)


@pytest.fixture(scope="module")
def docker_image():
    required = os.environ.get("BOOLEY_REQUIRE_WORKTREE_DOCKER") == "1"
    if not required:
        pytest.skip("set BOOLEY_REQUIRE_WORKTREE_DOCKER=1 for required bind-mount acceptance")
    assert shutil.which("docker"), "required Docker acceptance needs Docker"
    image = os.environ.get("BOOLEY_WORKTREE_DOCKER_IMAGE", "booley-sandbox:latest")
    inspect = _run(["docker", "image", "inspect", "--format", "{{.Id}}", image])
    assert inspect.returncode == 0, inspect.stderr
    parent_id = inspect.stdout.strip()
    alias = _canonical_alias_recipe()
    tag = f"127.0.0.1:1/booley1071-parent-{uuid4().hex}:local"
    output = f"booley1071-alias-test-{uuid4().hex}:local"
    assert _run(["docker", "tag", parent_id, tag]).returncode == 0
    try:
        _build_alias_fixture(alias, tag, output)
        alias_id = _run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", output]
        ).stdout.strip()
        yield parent_id, alias_id
    finally:
        _run(["docker", "image", "rm", output, tag])


def _repository(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    state = root / ".booley"
    state.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "fixture@example.test"),
        ("config", "user.name", "Fixture"),
        ("commit", "--allow-empty", "-qm", "initial"),
    ):
        result = _run(["git", *args], cwd=root)
        assert result.returncode == 0, result.stderr
    return root, state


def _container(image: str, root: Path, state: Path, script: str, *, duplicate: bool):
    source = Path(__file__).resolve().parents[2] / "src"
    args = [
        "docker",
        "run",
        "--rm",
        "--network=none",
        f"--user={os.getuid()}:{os.getgid()}" if os.name != "nt" else "--user=0:0",
        "--name",
        next_ci_container_name() or f"booley-worktree-test-{uuid4().hex}",
        "--entrypoint=python3",
        "--mount",
        f"type=bind,source={root},target=/work",
        "--mount",
        f"type=bind,source={state},target=/work/.booley",
        "--mount",
        f"type=bind,source={source},target=/opt/booley-test-src,readonly",
        "-e",
        "PYTHONPATH=/opt/booley-test-src",
        "-e",
        "BOOLEY_DIR=/booley-project",
    ]
    if duplicate:
        args += ["--mount", f"type=bind,source={state},target=/booley-project"]
    return _run([*args, image, "-c", script])


# Current images resolve the alias bind canonically; reproduce metadata written
# by the retired directory layout without introducing a legacy image producer.
_LEGACY_RELATIVE_METADATA = """
from pathlib import Path
checkout = Path('/work/.booley/worktrees/demo')
metadata = Path('/work/.git/worktrees/demo')
subprocess.run(['git', '-C', '/work', 'config', 'core.repositoryformatversion', '1'], check=True)
subprocess.run(['git', '-C', '/work', 'config', 'extensions.relativeWorktrees', 'true'], check=True)
(checkout / '.git').write_text('gitdir: ../../../work/.git/worktrees/demo\\n')
(metadata / 'gitdir').write_text('../../../../booley-project/worktrees/demo/.git\\n')
"""


@pytest.mark.slow
def test_external_directory_layout_uses_actual_guarded_builder(
    tmp_path: Path, docker_image, monkeypatch
):
    from booley.harness import image_lifecycle as harness
    from booley.runtime import image_lifecycle as lifecycle
    from booley.runtime import project_image

    root = tmp_path / "project"
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "proof").write_text("external-state\n")
    parent_id = docker_image[1]
    parent = project_image.inspect_layout_image(parent_id)
    monkeypatch.setenv("BOOLEY_DIR", str(external))
    from booley.core.booley_dir import reset_cache

    reset_cache()
    node = _layout_fixture_node(root, parent_id, parent)
    output = node.reference
    docker = harness._docker_adapter()
    adapter = harness._IncrementalBuildAdapter(root, docker, verbose=True)
    try:
        built = adapter.prepare(node, candidate_reference=output, parent_reference=parent_id)
        assert built == output
        child = project_image.inspect_layout_image(output)
        project_image.verify_layout_parent_identity(child["Id"], parent_id)
        assert child["Config"]["Labels"][lifecycle.LABEL_WHEEL_SHA256] == "a" * 64
        _assert_layout_logical_selection(output, parent_id, root)
        result = _external_container(
            output,
            root,
            external,
            "from pathlib import Path; assert not Path('/booley-project').is_symlink(); "
            "assert Path('/booley-project/proof').read_text() == 'external-state\\n'; "
            "assert not Path('/work/.booley').exists()",
        )
        assert result.returncode == 0, result.stderr
        assert not (root / ".booley").exists()
    finally:
        _run(["docker", "image", "rm", output])


@pytest.mark.slow
def test_canonical_mount_pins_state_and_preserves_discovery_boundary(tmp_path: Path, docker_image):
    root, state = _repository(tmp_path)
    result = _container(
        docker_image[1],
        root,
        state,
        """
import errno
import subprocess
from pathlib import Path
assert Path('/booley-project').resolve() == Path('/work/.booley')
try:
    Path('/work/.booley').rename('/work/replaced-state')
except OSError as exc:
    assert exc.errno == errno.EBUSY, exc
else:
    raise AssertionError('Booley Directory was replaceable despite its pinned mount')
for path in ['/tmp', '/']:
    result = subprocess.run(['git', '-C', path, 'rev-parse', '--show-toplevel'], capture_output=True, text=True)
    assert result.returncode != 0, result.stdout
assert not Path('/.git').exists()
""",
        duplicate=False,
    )
    assert result.returncode == 0, result.stderr
    assert state.is_dir()
    assert not (root / "replaced-state").exists()


def _canonical_alias_recipe() -> str:
    recipe_root = Path(__file__).resolve().parents[2] / "src/booley/data/docker"
    wheel = (recipe_root / "Dockerfile.wheel").read_text()
    legacy = (recipe_root / "Dockerfile").read_text()
    alias = wheel[
        wheel.index("RUN if test -e /booley-project") : wheel.index(
            "USER agent", wheel.index("RUN if test -e /booley-project")
        )
    ]
    assert alias in legacy
    return alias


def _build_alias_fixture(alias: str, tag: str, output: str) -> None:
    with tempfile.TemporaryDirectory(prefix="booley1071-image-") as directory:
        recipe = Path(directory) / "Dockerfile"
        recipe.write_text(
            "FROM booley-layout-parent\nUSER root\n"
            + alias
            + "LABEL io.booley.wheel.sha256="
            + "a" * 64
            + "\nLABEL io.booley.wheel.source-fingerprint=fixture-source\n"
            + "LABEL io.booley.sandbox.selection-fingerprint="
            + "c" * 64
            + "\nUSER agent\n"
        )
        build = _run(
            [
                "docker",
                "build",
                "--pull=false",
                "--network=none",
                "--progress=plain",
                "--build-context",
                f"booley-layout-parent=docker-image://{tag}",
                "-f",
                str(recipe),
                "-t",
                output,
                directory,
            ]
        )
        assert build.returncode == 0, build.stderr


def _layout_fixture_node(root: Path, parent_id: str, parent: dict):
    from booley.runtime import image_lifecycle as lifecycle
    from booley.runtime.paths import docker_data_dir

    labels = parent["Config"]["Labels"]
    upstream = lifecycle.ImageNode(
        parent_id,
        docker_data_dir() / "Dockerfile.wheel",
        lifecycle.PayloadProvenance(
            "3", labels[lifecycle.LABEL_VERSION], labels.get(lifecycle.LABEL_PAYLOAD_FINGERPRINT)
        ),
        lifecycle.BuildProvenance(labels[lifecycle.LABEL_RECIPE_FINGERPRINT], None),
        role=lifecycle.ImageRole.WHEEL_OVERLAY,
        wheel_source_fingerprint="fixture-source",
        logical_selection_fingerprint=labels[lifecycle.LABEL_LOGICAL_SELECTION_FINGERPRINT],
    )
    from booley.runtime import project_image

    return lifecycle._layout_node(project_image.layout_topology(root), upstream, parent_id)


def _assert_layout_logical_selection(output: str, parent_id: str, root: Path) -> None:
    from booley.runtime import image_identity, project_image

    def inspect(reference):
        return image_identity.decode_image_metadata(
            reference, [project_image.inspect_layout_image(reference)]
        )

    assert (
        image_identity.compare_logical_selection(
            output, parent_id, inspect, project_root=root
        ).status
        is image_identity.Status.MATCH
    )


def _external_container(image: str, root: Path, external: Path, script: str):
    source = Path(__file__).resolve().parents[2] / "src"
    return _run(
        [
            "docker",
            "run",
            "--rm",
            "--pull=never",
            "--network=none",
            f"--user={os.getuid()}:{os.getgid()}",
            "--name",
            next_ci_container_name() or f"booley-worktree-test-{uuid4().hex}",
            "--entrypoint=python3",
            "--mount",
            f"type=bind,source={root},target=/work",
            "--mount",
            f"type=bind,source={external},target=/booley-project",
            "--mount",
            f"type=bind,source={source},target=/opt/booley-test-src,readonly",
            "-e",
            "PYTHONPATH=/opt/booley-test-src",
            "-e",
            "BOOLEY_DIR=/booley-project",
            image,
            "-c",
            script,
        ]
    )


@pytest.mark.slow
def test_layout_cleanup_uses_actual_docker_latest_inventory(tmp_path, docker_image, monkeypatch):
    from booley.core.booley_dir import reset_cache
    from booley.harness import image_lifecycle as harness
    from booley.runtime import image_lifecycle as lifecycle
    from booley.runtime import project_image

    root = tmp_path / "project"
    root.mkdir()
    parent_id = docker_image[1]
    parent = project_image.inspect_layout_image(parent_id)
    docker = harness._docker_adapter()
    adapter = harness._IncrementalBuildAdapter(root, docker, verbose=False)
    references = []
    try:
        for name in ("first", "second"):
            external = tmp_path / name
            external.mkdir()
            monkeypatch.setenv("BOOLEY_DIR", str(external))
            reset_cache()
            node = _layout_fixture_node(root, parent_id, parent)
            references.append(node.reference)
            assert (
                adapter.prepare(
                    node, candidate_reference=node.reference, parent_reference=parent_id
                )
                == node.reference
            )
        checked = lifecycle._reconcile_layout_tag_cleanup(
            root, references[-1], lifecycle.Intent.CHECK, docker
        )
        assert checked.pending == (references[0] + ":latest",)
        cleaned = lifecycle._reconcile_layout_tag_cleanup(
            root, references[-1], lifecycle.Intent.ENSURE, docker
        )
        assert cleaned.removed == checked.pending
        assert docker.image_id(references[-1]) is not None
    finally:
        for reference in references:
            if docker.image_id(reference) is not None:
                docker.remove_tag(reference)
        reset_cache()
