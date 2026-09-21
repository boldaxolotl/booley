"""Project Initialization adapters for Sandbox Image reconciliation."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from booley.runtime import image_lifecycle as runtime_lifecycle
from booley.runtime import project_image
from booley.runtime.build_stamp import (
    embedded_official_release,
    extracted_development_context,
    wheel_embedded_source_fingerprint,
)
from booley.runtime.paths import docker_data_dir
from booley.runtime.project_dir import resolve_checkout_project_dir
from booley.runtime.version_attribution import VersionOrigin

if TYPE_CHECKING:
    from booley.harness.setup.common import InitContext

BASE_IMAGE = runtime_lifecycle.BASE_IMAGE
FLAVOR_RECIPES = runtime_lifecycle.FLAVOR_RECIPES
RISCV_SUBSTRATE_IMAGE = runtime_lifecycle.RISCV_SUBSTRATE_IMAGE
BuildPort = runtime_lifecycle.BuildPort
ArtifactSource = runtime_lifecycle.ArtifactSource
ArtifactPolicy = runtime_lifecycle.ArtifactPolicy
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
ImageRole = runtime_lifecycle.ImageRole
LifecyclePlan = runtime_lifecycle.LifecyclePlan
PreparedConvergence = runtime_lifecycle.PreparedConvergence
PlanAction = runtime_lifecycle.PlanAction


class _LegacyBuildAdapter:
    def __init__(
        self, project_root: Path, docker: DockerPort | None = None, *, verbose: bool
    ) -> None:
        self.project_root = project_root
        self.docker = docker
        self.verbose = verbose

    def build(
        self,
        node: ImageNode,
        *,
        force: bool,
        source: ArtifactSource,
    ) -> str | None:
        if source is ArtifactSource.VERIFIED_RELEASE_PULL:
            return self._pull_release(node)
        self._build_local(node, force=force)
        return None

    def _pull_release(self, node: ImageNode) -> str:
        from booley.harness.setup.docker_image import _try_pull_image, remote_tag

        shipped = node.reference == BASE_IMAGE or node.reference in FLAVOR_RECIPES
        if not shipped:
            raise ImageLifecycleError(
                f"no published Sandbox Image source exists for {node.reference}"
            )
        if not _try_pull_image(node.payload.version, node.reference, adopt=False):
            raise ImageLifecycleError(
                f"could not pull current packaged Sandbox Image {node.reference}"
            )
        return remote_tag(node.reference, node.payload.version)

    def _build_local(self, node: ImageNode, *, force: bool) -> None:
        from booley.harness.setup.common import InitContext
        from booley.harness.setup.docker_image import ensure_flavor_image

        context = InitContext(
            project_root=self.project_root,
            force=force,
            verbose=self.verbose,
            show_step_banners=False,
        )
        if node.reference == BASE_IMAGE:
            self._build_base(node, context)
        elif node.reference in FLAVOR_RECIPES:
            ensure_flavor_image(context, node.reference, allow_pull=False)
        else:
            self._build_project(node, context)
        failures = [result.detail for result in context.results if result.status == "err"]
        if failures:
            raise ImageLifecycleError("; ".join(failures))

    def _build_base(self, node: ImageNode, context: InitContext) -> None:
        import booley
        from booley.harness.setup.docker_image import (
            _docker_image_exists,
            _docker_local_build,
            _step_docker_image,
        )

        if booley.version_attribution.origin is VersionOrigin.SOURCE:
            _step_docker_image(context, node.reference, allow_pull=False)
            return
        try:
            with extracted_development_context() as root:
                docker_dir = root / "src" / "booley" / "data" / "docker"
                _docker_local_build(
                    context,
                    docker_dir,
                    (
                        self.docker.image_id(BASE_IMAGE) is not None
                        if self.docker is not None
                        else _docker_image_exists(BASE_IMAGE)
                    ),
                    node.payload.fingerprint,
                    preserve_build_stamp=True,
                )
        except (OSError, ValueError) as error:
            raise ImageLifecycleError(
                f"cannot verify the development Sandbox Image build context: {error}"
            ) from error

    def _build_project(self, node: ImageNode, context: InitContext) -> None:
        from booley.harness import init_cmd

        docker_dir = resolve_checkout_project_dir(self.project_root) / "docker"
        user_owned = any(
            path.is_file() and not project_image.is_managed_generated_file(path)
            for path in (docker_dir / "Dockerfile", docker_dir / "requirements.txt")
        )
        if not user_owned:
            init_cmd._step_project_image(context)
            return
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


class _IncrementalBuildAdapter:
    """Build role-specific candidates without moving managed image tags."""

    def __init__(self, project_root: Path, docker: DockerPort, *, verbose: bool) -> None:
        self.project_root = project_root
        self.docker = docker
        self.verbose = verbose
        self._wheel_sha256: str | None = None

    def prepare(
        self,
        node: ImageNode,
        *,
        candidate_reference: str,
        parent_reference: str | None,
    ) -> str:
        if node.acquisition_policy is ArtifactPolicy.VERIFIED_RELEASE_ONLY:
            return self._pull_complete_release(node)
        from booley.harness.setup.common import InitContext

        context = InitContext(
            project_root=self.project_root,
            force=True,
            verbose=self.verbose,
            show_step_banners=False,
        )
        parent_id = self.docker.image_id(parent_reference) if parent_reference else None
        if parent_reference is not None and parent_id is None:
            raise ImageLifecycleError(
                f"could not resolve prepared parent {parent_reference!r} for {node.reference}"
            )
        self._materialize_managed_project_recipe(node)
        self._build_role(context, node, candidate_reference, parent_reference, parent_id)
        failures = [result.detail for result in context.results if result.status == "err"]
        if failures:
            raise ImageLifecycleError("; ".join(failures))
        if self.docker.image_id(candidate_reference) is None:
            raise ImageLifecycleError(f"candidate build produced no image {candidate_reference!r}")
        return candidate_reference

    def _pull_complete_release(self, node: ImageNode) -> str:
        from booley.harness.setup.docker_image import _try_pull_image, remote_tag

        if node.role is not ImageRole.WHEEL_OVERLAY or node.reference not in {
            BASE_IMAGE,
            *FLAVOR_RECIPES,
        }:
            raise ImageLifecycleError(
                f"no complete published Sandbox Image exists for {node.reference}"
            )
        if not _try_pull_image(node.payload.version, node.reference, adopt=False):
            raise ImageLifecycleError(
                f"could not pull current packaged Sandbox Image {node.reference}"
            )
        return remote_tag(node.reference, node.payload.version)

    def _materialize_managed_project_recipe(self, node: ImageNode) -> None:
        if node.role is not ImageRole.PROJECT_SUBSTRATE:
            return
        root = resolve_checkout_project_dir(self.project_root)
        body = runtime_lifecycle._project_requirements_body(self.project_root)
        project_image.write_project_image_files(
            root / "docker",
            body or "",
            parent_image=project_image.MANAGED_PROJECT_PARENT,
        )

    def _build_role(
        self,
        context,
        node: ImageNode,
        candidate: str,
        parent_reference: str | None,
        parent_id: str | None,
    ) -> None:
        from booley.harness.setup import docker_image

        root = docker_data_dir().parents[3]
        inputs = self._role_build_inputs(context, node, root, parent_reference)
        if inputs is None:
            return
        build_context, contexts, build_args = inputs
        labels = [
            (runtime_lifecycle.LABEL_SCHEMA, runtime_lifecycle.PROVENANCE_SCHEMA),
            (runtime_lifecycle.LABEL_ARTIFACT_ROLE, node.role.value),
            (runtime_lifecycle.LABEL_EFFECTIVE_INPUTS, node.effective_inputs or ""),
            (runtime_lifecycle.LABEL_RECIPE_FINGERPRINT, node.build.recipe_fingerprint),
            (runtime_lifecycle.LABEL_BUILD_ORIGIN, "local"),
            (runtime_lifecycle.LABEL_VERSION, node.payload.version),
        ]
        if node.wheel_source_fingerprint is not None:
            labels.append(
                (
                    runtime_lifecycle.LABEL_WHEEL_SOURCE_FINGERPRINT,
                    node.wheel_source_fingerprint,
                )
            )
            labels.append((runtime_lifecycle.LABEL_WHEEL_SHA256, self._wheel_sha256 or ""))
        spec = docker_image._DockerBuildSpec(
            dockerfile=node.recipe,
            context=build_context,
            exists=False,
            image=candidate,
            build_contexts=contexts,
            build_args=build_args,
            parent_artifact=parent_id,
            labels=tuple(labels),
            build_note=f"preparing {node.role.value}",
        )
        result = docker_image._docker_build_image(context, spec)
        if result is None:
            return
        if result != 0:
            context.record("docker_image", "err", f"{node.role.value} build failed")

    def _role_build_inputs(
        self,
        context,
        node: ImageNode,
        root: Path,
        parent_reference: str | None,
    ) -> tuple[Path, tuple[tuple[str, str], ...], tuple[str, ...]] | None:
        from booley.harness.setup import docker_image

        parent = f"docker-image://{parent_reference}"
        if node.role is ImageRole.RUNTIME_BASE:
            return root, (), tuple(docker_image._runtime_base_build_metadata_args(root))
        if node.role is ImageRole.STANDARD_SUBSTRATE:
            return root, (("booley-runtime-base", parent),), ()
        if node.role is ImageRole.RISCV_SUBSTRATE:
            return docker_data_dir(), (("booley-standard-substrate", parent),), ()
        if node.role is ImageRole.PROJECT_SUBSTRATE:
            return (
                resolve_checkout_project_dir(self.project_root) / "docker",
                ((project_image.MANAGED_PROJECT_PARENT, parent),),
                (),
            )
        if node.role is not ImageRole.WHEEL_OVERLAY:
            raise ImageLifecycleError(f"unsupported image role {node.role!r}")
        if not docker_image._docker_build_wheel(context, root):
            return None
        wheels = sorted((root / "dist").glob("booley_rtl-*.whl"))
        if len(wheels) != 1:
            raise ImageLifecycleError("wheel preparation did not produce exactly one wheel")
        if wheel_embedded_source_fingerprint(wheels[0]) != node.wheel_source_fingerprint:
            raise ImageLifecycleError(
                "built wheel source fingerprint differs from the planned inputs"
            )
        self._wheel_sha256 = hashlib.sha256(wheels[0].read_bytes()).hexdigest()
        return (
            root,
            (("booley-substrate", parent),),
            (
                *docker_image._image_build_metadata_args(root),
                "--build-arg",
                f"BOOLEY_WHEEL_SHA256={self._wheel_sha256}",
            ),
        )


def _docker_adapter() -> DockerPort:
    return runtime_lifecycle._docker_adapter()


def _build_adapter(project_root: Path, docker: DockerPort, *, verbose: bool) -> BuildPort:
    return _LegacyBuildAdapter(project_root, docker, verbose=verbose)


def _transaction_build_adapter(
    project_root: Path,
    docker: DockerPort,
    *,
    verbose: bool,
) -> _IncrementalBuildAdapter:
    return _IncrementalBuildAdapter(project_root, docker, verbose=verbose)


def _artifact_policy(project_root: Path | None = None) -> ArtifactPolicy:
    """Select artifact acquisition from the authoritative installation identity."""
    import booley

    if booley.version_attribution.origin is VersionOrigin.SOURCE:
        return ArtifactPolicy.LOCAL_ONLY
    if booley.version_attribution.origin is VersionOrigin.DISTRIBUTION:
        if project_root is not None and _uses_managed_riscv_project(project_root):
            return ArtifactPolicy.VERIFIED_RELEASE_THEN_LOCAL
        return (
            ArtifactPolicy.VERIFIED_RELEASE_ONLY
            if embedded_official_release()
            else ArtifactPolicy.LOCAL_ONLY
        )
    raise ImageLifecycleError(
        "cannot select managed Sandbox Images because the running Booley code is "
        "neither an attributed source checkout nor an installed distribution"
    )


def _uses_managed_riscv_project(project_root: Path) -> bool:
    configured = runtime_lifecycle._configured_image(project_root)
    return (
        configured == "booley-sandbox-riscv"
        and runtime_lifecycle._project_requirements_body(project_root) is not None
    )


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
        artifact_policy=_artifact_policy(root if isinstance(scope, ProjectImageScope) else None),
    )


def plan(scope: ProjectImageScope) -> LifecyclePlan:
    """Plan source/release convergence using the installed artifact policy."""
    docker = _docker_adapter()
    return runtime_lifecycle.plan(
        scope,
        docker=docker,
        artifact_policy=_artifact_policy(scope.project_root),
    )


def prepare(
    lifecycle_plan: LifecyclePlan,
    *,
    verbose: bool = False,
) -> PreparedConvergence:
    """Build and verify candidates while leaving managed tags unchanged."""
    docker = _docker_adapter()
    builder = _transaction_build_adapter(
        lifecycle_plan.project_root,
        docker,
        verbose=verbose,
    )
    return runtime_lifecycle.prepare(
        lifecycle_plan,
        docker=docker,
        builder=builder,
        verbose=verbose,
    )


def commit(prepared: PreparedConvergence) -> LifecycleResult:
    """Adopt one previously prepared convergence."""
    return runtime_lifecycle.commit(prepared, docker=_docker_adapter())


def abort(prepared: PreparedConvergence) -> None:
    """Discard one previously prepared convergence."""
    runtime_lifecycle.abort(prepared, docker=_docker_adapter())
