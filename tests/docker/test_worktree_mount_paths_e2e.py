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
    recipe_root = Path(__file__).resolve().parents[2] / "src/booley/data/docker"
    wheel = (recipe_root / "Dockerfile.wheel").read_text()
    legacy = (recipe_root / "Dockerfile").read_text()
    alias = wheel[
        wheel.index("RUN if test -e /booley-project") : wheel.index(
            "USER agent", wheel.index("RUN if test -e /booley-project")
        )
    ]
    assert alias in legacy
    tag = f"127.0.0.1:1/booley1071-parent-{uuid4().hex}:local"
    output = f"booley1071-alias-test-{uuid4().hex}:local"
    assert _run(["docker", "tag", parent_id, tag]).returncode == 0
    try:
        with tempfile.TemporaryDirectory(prefix="booley1071-image-") as directory:
            recipe = Path(directory) / "Dockerfile"
            recipe.write_text(
                "FROM booley-layout-parent\nUSER root\n"
                + alias
                + "LABEL io.booley.wheel.sha256="
                + "a" * 64
                + "\nLABEL io.booley.wheel.source-fingerprint=fixture-source\nUSER agent\n"
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
        alias_id = _run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", output]
        ).stdout.strip()
        yield parent_id, alias_id
    finally:
        _run(["docker", "image", "rm", output, tag])


def _repository(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()
    state = root / ".booley_project"
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
        next_ci_container_name(),
        "--entrypoint=python3",
        "--mount",
        f"type=bind,source={root},target=/work",
        "--mount",
        f"type=bind,source={state},target=/work/.booley_project",
        "--mount",
        f"type=bind,source={source},target=/opt/booley-test-src,readonly",
        "-e",
        "PYTHONPATH=/opt/booley-test-src",
        "-e",
        "BOOLEY_PROJECT_DIR=/booley-project",
    ]
    if duplicate:
        args += ["--mount", f"type=bind,source={state},target=/booley-project"]
    return _run([*args, image, "-c", script])


@pytest.mark.slow
@pytest.mark.parametrize("duplicate", [True, False], ids=["legacy-bind", "canonical-alias"])
def test_ticket_worktree_real_mount_portability(
    tmp_path: Path, docker_image: tuple[str, str], duplicate: bool
):
    root, state = _repository(tmp_path)
    script = """
import subprocess
from pathlib import Path
from booley.core.project_dir import resolve_project_dir
from booley.ticket_board.workspace_ops import _attach_worktree
root = Path('/work')
subprocess.run(['git', '-C', str(root), 'config', 'worktree.useRelativePaths', 'true'], check=True)
_attach_worktree(root, resolve_project_dir(root) / 'worktrees' / 'demo', 'ticket/demo', 'HEAD')
for path in ['/work/.booley_project/worktrees/demo', '/booley-project/worktrees/demo']:
    result = subprocess.run(['git', '-C', path, 'status', '--porcelain'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
"""
    image = docker_image[0] if duplicate else docker_image[1]
    if duplicate:
        script = script.replace(
            "_attach_worktree(root, resolve_project_dir(root) / 'worktrees' / 'demo', 'ticket/demo', 'HEAD')",
            "subprocess.run(['git', '-C', '/work', 'worktree', 'add', '-b', 'ticket/demo', '/booley-project/worktrees/demo'], check=True)",
        )
    result = _container(image, root, state, script, duplicate=duplicate)
    if duplicate:
        assert result.returncode != 0, "legacy duplicate bind must reproduce broken relative links"
        assert "not a git repository" in result.stderr, result.stderr
        return
    assert result.returncode == 0, result.stderr
    host = _run(["git", "-C", str(state / "worktrees/demo"), "status", "--porcelain"])
    assert host.returncode == 0, host.stderr
    listing = _run(["git", "-C", str(root), "worktree", "list", "--porcelain"])
    assert "prunable" not in listing.stdout
    assert "work/" not in (state / "worktrees/demo/.git").read_text()


@pytest.mark.slow
def test_external_directory_layout_uses_actual_guarded_builder(tmp_path: Path, docker_image):
    from booley.harness import image_lifecycle as harness
    from booley.runtime import image_lifecycle as lifecycle
    from booley.runtime import project_image
    from booley.runtime.paths import docker_data_dir

    root = tmp_path / "project"
    root.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "proof").write_text("external-state\n")
    parent_id = docker_image[1]
    parent = project_image.inspect_layout_image(parent_id)
    labels = parent["Config"]["Labels"]
    output = f"booley1071-external-layout-{uuid4().hex}:local"
    node = lifecycle.ImageNode(
        output,
        docker_data_dir() / "Dockerfile.project-data-layout",
        lifecycle.PayloadProvenance(
            "3", labels[lifecycle.LABEL_VERSION], labels[lifecycle.LABEL_PAYLOAD_FINGERPRINT]
        ),
        lifecycle.BuildProvenance("fixture-recipe", parent_id),
        role=lifecycle.ImageRole.PROJECT_DATA_LAYOUT,
        parent=parent_id,
        effective_inputs="fixture-layout-inputs",
        wheel_source_fingerprint="fixture-source",
    )
    docker = harness._docker_adapter()
    adapter = harness._IncrementalBuildAdapter(root, docker, verbose=True)
    try:
        built = adapter.prepare(node, candidate_reference=output, parent_reference=parent_id)
        assert built == output
        child = project_image.inspect_layout_image(output)
        project_image.verify_layout_image(parent, child)
        assert child["Config"]["Labels"][lifecycle.LABEL_WHEEL_SHA256] == "a" * 64
        result = _run(
            [
                "docker",
                "run",
                "--rm",
                "--pull=never",
                "--network=none",
                "--name",
                next_ci_container_name(),
                "--entrypoint=python3",
                "--mount",
                f"type=bind,source={root},target=/work",
                "--mount",
                f"type=bind,source={external},target=/booley-project",
                output,
                "-c",
                "from pathlib import Path; assert not Path('/booley-project').is_symlink(); "
                "assert Path('/booley-project/proof').read_text() == 'external-state\\n'; "
                "assert not Path('/work/.booley_project').exists()",
            ]
        )
        assert result.returncode == 0, result.stderr
        assert not (root / ".booley_project").exists()
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
assert Path('/booley-project').resolve() == Path('/work/.booley_project')
try:
    Path('/work/.booley_project').rename('/work/replaced-state')
except OSError as exc:
    assert exc.errno == errno.EBUSY, exc
else:
    raise AssertionError('Project root was replaceable despite its pinned mount')
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


@pytest.mark.slow
def test_legacy_links_repair_through_actual_canonical_mount(tmp_path: Path, docker_image):
    root, state = _repository(tmp_path)
    created = _container(
        docker_image[0],
        root,
        state,
        """
import subprocess
subprocess.run(['git', '-C', '/work', 'config', 'worktree.useRelativePaths', 'true'], check=True)
subprocess.run(['git', '-C', '/work', 'worktree', 'add', '-b', 'ticket/demo', '/booley-project/worktrees/demo'], check=True)
""",
        duplicate=True,
    )
    assert created.returncode == 0, created.stderr
    result = _container(
        docker_image[1],
        root,
        state,
        """
from pathlib import Path
import subprocess
from booley.runtime.worktree_repair import repair_ticket_workspace
root, checkout = Path('/work'), Path('/work/.booley_project/worktrees/demo')
repair_ticket_workspace(root, checkout, 'refs/heads/ticket/demo')
repair_ticket_workspace(root, checkout, 'refs/heads/ticket/demo')
for path in [str(checkout), '/booley-project/worktrees/demo']:
    subprocess.run(['git', '-C', path, 'status', '--porcelain'], check=True)
""",
        duplicate=False,
    )
    assert result.returncode == 0, result.stderr
    host = _run(["git", "-C", str(state / "worktrees/demo"), "status", "--porcelain"])
    assert host.returncode == 0, host.stderr
    assert (
        "prunable" not in _run(["git", "-C", str(root), "worktree", "list", "--porcelain"]).stdout
    )


@pytest.mark.slow
def test_blocked_board_review_and_show_repair_actual_legacy_checkout(tmp_path: Path, docker_image):
    root, state = _repository(tmp_path)
    produce = _container(
        docker_image[0],
        root,
        state,
        """
import subprocess
from pathlib import Path
from booley.ticket_board.io import TicketIO
from booley.ticket_board.board_layout import read_state_record, write_state_record
from booley.ticket_board.lifecycle import TicketState
root, state = Path('/work'), Path('/booley-project')
def git(*args):
    subprocess.run(['git', '-C', str(root), *args], check=True)
git('branch', '-M', 'main')
(state / 'tickets/board').mkdir(parents=True)
(state / '.gitignore').write_text('/worktrees/\\n/.runtime/\\n/hooks/\\n')
(state / 'booley.toml').write_text('[flows]\\n')
(root / 'README.md').write_text('fixture source\\n')
git('add', '-A')
git('add', '-f', '.booley_project')
git('commit', '-m', 'fixture sources')
tio = TicketIO(state / 'tickets', project_root=root)
created = tio.create_ticket_document('demo', '''---
summary: Fixture blocked diagnosis
type: feature
branch: main
scope: [README.md]
on_success: [review]
CRITERIA_MANDATORY: {REVIEW: {rtl: {bugs: clean}}}
---
## Description
Exercise blocked recovery.
''')
assert created is not None
assert tio.enqueue_ticket('demo')
record = read_state_record(tio.tickets_dir, 'demo')
assert record is not None
write_state_record(tio.tickets_dir, 'demo', record.with_state(TicketState.BLOCKED))
""",
        duplicate=True,
    )
    assert produce.returncode == 0, produce.stderr
    result = _container(
        docker_image[1],
        root,
        state,
        """
from pathlib import Path
from types import SimpleNamespace
from booley.core.models import AgentResult
from booley.harness import blocked_prep
from booley.harness.booley import _cmd_board_review, _cmd_board_show
async def bounded_fixture_agent(_ctx, _evidence):
    return AgentResult(structured={
        'classification': 'ticket-code', 'board_reason': 'fixture blocked',
        'blocked_stage': 'developer',
        'blockers': [{'name': 'fixture', 'reason': 'fixture failure', 'evidence': 'state'}],
        'passing_non_blocking': [], 'developer_questions': [],
        'recommended_action': 'retry after fixing fixture', 'findings': [],
    })
blocked_prep._invoke = bounded_fixture_agent
args = SimpleNamespace(slug='demo', request=False, reason='', force=False, repair=False, no_open_diffs=True)
assert _cmd_board_review(args, Path('/work')) == 0
assert _cmd_board_show(args, Path('/work')) == 0
assert blocked_prep.render_blocked_dossier(Path('/work'), 'demo').ready
""",
        duplicate=False,
    )
    assert result.returncode == 0, result.stderr
    assert "fixture failure" in result.stdout
    host = _run(["git", "-C", str(state / "worktrees/demo"), "status", "--porcelain"])
    assert host.returncode == 0, host.stderr
