"""Project Initialization adapters for Runtime Image reconciliation."""

from __future__ import annotations

from pathlib import Path

from booley.runtime import image_lifecycle as runtime_lifecycle
from booley.runtime import project_image
from booley.runtime.paths import docker_data_dir
from booley.runtime.project_dir import resolve_checkout_project_dir

BASE_IMAGE = runtime_lifecycle.BASE_IMAGE
FLAVOR_RECIPES = runtime_lifecycle.FLAVOR_RECIPES
BuildPort = runtime_lifecycle.BuildPort
DockerPort = runtime_lifecycle.DockerPort
HostImageScope = runtime_lifecycle.HostImageScope
ImageCleanup = runtime_lifecycle.ImageCleanup
ImageLifecycleError = runtime_lifecycle.ImageLifecycleError
ImageNode = runtime_lifecycle.ImageNode
ImageScope = runtime_lifecycle.ImageScope
Intent = runtime_lifecycle.Intent
LifecycleResult = runtime_lifecycle.LifecycleResult
ProjectImageScope = runtime_lifecycle.ProjectImageScope
Status = runtime_lifecycle.Status


class _LegacyBuildAdapter:
    def __init__(self, project_root: Path, *, verbose: bool) -> None:
        self.project_root = project_root
        self.verbose = verbose

    def build(self, node: ImageNode, *, force: bool) -> None:
        from booley.harness import init_cmd
        from booley.harness.setup.common import InitContext
        from booley.harness.setup.docker_image import (
            _step_docker_image,
            _try_pull_image,
            ensure_flavor_image,
        )

        shipped = node.reference == BASE_IMAGE or node.reference in FLAVOR_RECIPES
        source_root = docker_data_dir().parents[3]
        if shipped and not (source_root / "pyproject.toml").is_file():
            if not _try_pull_image(node.payload.version, node.reference):
                raise ImageLifecycleError(
                    f"could not pull current packaged Runtime Image {node.reference}"
                )
            return

        context = InitContext(
            project_root=self.project_root,
            force=force,
            verbose=self.verbose,
            show_step_banners=False,
        )
        if node.reference == BASE_IMAGE:
            _step_docker_image(context, node.reference)
        elif node.reference in FLAVOR_RECIPES:
            ensure_flavor_image(context, node.reference)
        else:
            docker_dir = resolve_checkout_project_dir(self.project_root) / "docker"
            user_owned = any(
                path.is_file() and not project_image.is_managed_generated_file(path)
                for path in (docker_dir / "Dockerfile", docker_dir / "requirements.txt")
            )
            if user_owned:
                if not (docker_dir / "Dockerfile").is_file():
                    raise ImageLifecycleError(
                        f"cannot refresh {node.reference}: {docker_dir / 'Dockerfile'} is missing"
                    )
                if not project_image.build_project_image(
                    node.reference,
                    docker_dir,
                    verbose=self.verbose,
                ):
                    raise ImageLifecycleError(f"failed to rebuild {node.reference}")
            else:
                init_cmd._step_project_image(context)
        failures = [result.detail for result in context.results if result.status == "err"]
        if failures:
            raise ImageLifecycleError("; ".join(failures))


def _docker_adapter() -> DockerPort:
    return runtime_lifecycle._docker_adapter()


def _build_adapter(project_root: Path, _docker: DockerPort, *, verbose: bool) -> BuildPort:
    return _LegacyBuildAdapter(project_root, verbose=verbose)


def reconcile(
    scope: ImageScope,
    intent: Intent,
    *,
    verbose: bool = False,
) -> LifecycleResult:
    """Reconcile an image graph with Project Initialization build adapters."""
    docker = _docker_adapter()
    if isinstance(scope, HostImageScope):
        root = docker_data_dir().parents[3]
    elif isinstance(scope, ProjectImageScope):
        root = scope.project_root.resolve()
    else:
        raise TypeError("image lifecycle scope must be HostImageScope or ProjectImageScope")
    builder = None if intent is Intent.CHECK else _build_adapter(root, docker, verbose=verbose)
    return runtime_lifecycle.reconcile(
        scope,
        intent,
        verbose=verbose,
        docker=docker,
        builder=builder,
    )
