"""Project Initialization adapters for Sandbox Image reconciliation."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from booley.runtime import image_lifecycle as runtime_lifecycle
from booley.runtime import project_image
from booley.runtime.build_stamp import (
    embedded_official_release,
    extracted_development_context,
    wheel_embedded_source_fingerprint,
)
from booley.runtime.docker_capacity import (
    BuildEstimateClass,
    DockerBuildPlan,
    DockerBuildRequest,
    DockerCacheEvidence,
    ensure_docker_build_capacity,
)
from booley.runtime.image_build_contracts import source_image_build_contracts
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
IncrementalPlanUnavailableError = runtime_lifecycle.IncrementalPlanUnavailableError
InstalledImageContractError = runtime_lifecycle.InstalledImageContractError
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
        self,
        project_root: Path,
        docker: DockerPort | None = None,
        *,
        verbose: bool,
        refresh_runtime_base: bool = False,
        capacity_plan: DockerBuildPlan | None = None,
    ) -> None:
        self.project_root = project_root
        self.docker = docker
        self.verbose = verbose
        self.refresh_runtime_base = refresh_runtime_base
        self.capacity_plan = capacity_plan

    def build(
        self,
        node: ImageNode,
        *,
        force: bool,
        source: ArtifactSource,
    ) -> str | None:
        if node.role is ImageRole.PROJECT_DATA_LAYOUT:
            adapter = _IncrementalBuildAdapter(
                self.project_root, self.docker, verbose=self.verbose
            )
            return adapter.prepare(
                node,
                candidate_reference=f"booley-lifecycle-{os.getpid()}-{uuid4().hex}-layout:candidate",
                parent_reference=node.parent,
            )
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
            _step_docker_image(
                context,
                node.reference,
                allow_pull=False,
                rebuild_runtime_base=self.refresh_runtime_base,
                capacity_plan=self.capacity_plan,
            )
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
                    rebuild_runtime_base=self.refresh_runtime_base,
                    capacity_plan=self.capacity_plan,
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


def _capacity_request(node: ImageNode, *, output_tag: str | None = None) -> DockerBuildRequest:
    """Project one BUILD node into validated Docker capacity facts."""
    estimate = (
        BuildEstimateClass.THIN_OVERLAY
        if node.role in {ImageRole.WHEEL_OVERLAY, ImageRole.PROJECT_DATA_LAYOUT}
        else BuildEstimateClass.HEAVYWEIGHT
    )
    labels = [
        (runtime_lifecycle.LABEL_ARTIFACT_ROLE, node.role.value),
        (runtime_lifecycle.LABEL_RECIPE_FINGERPRINT, node.build.recipe_fingerprint),
        (runtime_lifecycle.LABEL_EFFECTIVE_INPUTS, node.effective_inputs or ""),
    ]
    if node.runtime_base_contract is not None:
        labels.append((runtime_lifecycle.LABEL_RUNTIME_BASE_CONTRACT, node.runtime_base_contract))
    if node.standard_substrate_contract is not None:
        labels.append(
            (
                runtime_lifecycle.LABEL_STANDARD_SUBSTRATE_CONTRACT,
                node.standard_substrate_contract,
            )
        )
    return DockerBuildRequest(
        node.reference,
        output_tag or node.reference,
        estimate,
        DockerCacheEvidence(node.reference, tuple(labels)),
    )


def capacity_plan(lifecycle_plan: LifecyclePlan) -> DockerBuildPlan | None:
    """Return the ordered BUILD-only capacity projection of a lifecycle plan."""
    steps = getattr(lifecycle_plan, "steps", ())
    requests = tuple(
        _capacity_request(node)
        for node, step in zip(lifecycle_plan.nodes, steps, strict=True)
        if step.action is PlanAction.BUILD
    )
    return DockerBuildPlan(requests) if requests else None


class _IncrementalBuildAdapter:
    """Build role-specific candidates without moving managed image tags."""

    def __init__(
        self,
        project_root: Path,
        docker: DockerPort,
        *,
        verbose: bool,
        requests: tuple[DockerBuildRequest, ...] = (),
    ) -> None:
        self.project_root = project_root
        self.docker = docker
        self.verbose = verbose
        self._wheel_sha256: str | None = None
        self._requests = requests
        self._next_request_index = 0
        import booley

        self.source_root = booley.version_attribution.source_root

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
        if node.role is ImageRole.PROJECT_DATA_LAYOUT:
            self._build_layout(context, node, candidate_reference, parent_id)
        else:
            self._materialize_managed_project_recipe(node)
            self._build_role(context, node, candidate_reference, parent_reference, parent_id)
        failures = [result.detail for result in context.results if result.status == "err"]
        if failures:
            raise ImageLifecycleError("; ".join(failures))
        if self.docker.image_id(candidate_reference) is None:
            raise ImageLifecycleError(f"candidate build produced no image {candidate_reference!r}")
        return candidate_reference

    def _build_layout(
        self, context, node: ImageNode, candidate: str, parent_id: str | None
    ) -> None:
        from booley.harness.setup import docker_image

        if parent_id is None:
            raise ImageLifecycleError(
                "Project-data layout parent is unavailable; run init or refresh"
            )
        parent = self._validated_layout_parent(node, parent_id)
        tag = f"127.0.0.1:1/booley-lifecycle-{os.getpid()}-{uuid4().hex}-layout-parent:candidate"
        self.docker.tag(parent_id, tag)
        try:
            with tempfile.TemporaryDirectory(prefix="booley-layout-context-") as directory:
                if self.docker.image_id(tag) != parent_id:
                    raise ImageLifecycleError(
                        "Project-data layout parent tag changed before build"
                    )
                spec, matching = self._layout_build_spec(node, candidate, parent, tag, directory)
                result = docker_image._docker_build_image(context, spec)
                if result != 0 or self.docker.image_id(tag) != parent_id:
                    raise ImageLifecycleError(
                        "local-only Project-data layout build failed or parent was substituted"
                    )
                project_image.verify_layout_image(
                    parent, project_image.inspect_layout_image(candidate)
                )
                if matching:
                    self._next_request_index += 1
        except RuntimeError as exc:
            raise ImageLifecycleError(str(exc)) from exc
        finally:
            self._remove_layout_parent_tag(context, tag, parent_id)

    def _remove_layout_parent_tag(self, context, tag: str, parent_id: str) -> None:
        try:
            if self.docker.image_id(tag) == parent_id:
                self.docker.remove_tag(tag)
        except (ImageLifecycleError, OSError) as exc:
            context.record("layout_parent_cleanup", "warn", str(exc))

    def _validated_layout_parent(self, node: ImageNode, parent_id: str) -> dict:
        try:
            parent = project_image.inspect_layout_image(parent_id)
            project_image.layout_image_user(parent)
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            raise ImageLifecycleError(str(exc)) from exc
        if parent.get("Id") != parent_id:
            raise ImageLifecycleError("Project-data layout parent identity changed")
        labels = parent.get("Config", {}).get("Labels", {}) or {}
        wheel_sha = labels.get(runtime_lifecycle.LABEL_WHEEL_SHA256)
        if (
            not wheel_sha
            or labels.get(runtime_lifecycle.LABEL_WHEEL_SOURCE_FINGERPRINT)
            != node.wheel_source_fingerprint
        ):
            raise ImageLifecycleError("Project-data layout parent has no matching wheel identity")
        self._wheel_sha256 = wheel_sha
        return parent

    def _layout_build_spec(self, node, candidate, parent, tag, directory):
        from booley.harness.setup import docker_image

        parent_id = parent["Id"]
        user = project_image.layout_image_user(parent)
        current = _capacity_request(node, output_tag=candidate)
        index = self._next_request_index
        matching = (
            index < len(self._requests) and self._requests[index].managed_image == node.reference
        )
        tail = self._requests[index + int(matching) :]
        spec = docker_image._DockerBuildSpec(
            dockerfile=node.recipe,
            context=Path(directory),
            exists=False,
            image=candidate,
            build_contexts=(("booley-layout-parent", f"docker-image://{tag}"),),
            build_args=("--build-arg", f"BOOLEY_LAYOUT_USER={user}"),
            parent_artifact=parent_id,
            labels=tuple(self._build_labels(node, parent_id)),
            network="none",
            capacity_request=current,
            capacity_plan=DockerBuildPlan((current, *tail)),
            build_note="preparing Project-data directory layout",
        )
        return spec, matching

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
        if node.role not in {ImageRole.PROJECT_SUBSTRATE, ImageRole.PROJECT_OVERLAY}:
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

        root = self.source_root
        inputs = self._role_build_inputs(context, node, root, parent_reference)
        if inputs is None:
            return
        build_context, contexts, build_args = inputs
        planned_index = self._next_request_index if self._requests else None
        if (
            planned_index is not None
            and self._requests[planned_index].managed_image != node.reference
        ):
            raise ImageLifecycleError(
                f"capacity plan expected {self._requests[planned_index].managed_image!r}, "
                f"not {node.reference!r}"
            )
        current = _capacity_request(node, output_tag=candidate)
        if planned_index is None:
            tail = DockerBuildPlan((current,))
        else:
            tail = DockerBuildPlan((current, *self._requests[planned_index + 1 :]))
        spec = docker_image._DockerBuildSpec(
            dockerfile=node.recipe,
            context=build_context,
            exists=False,
            image=candidate,
            build_contexts=contexts,
            build_args=build_args,
            parent_artifact=parent_id,
            labels=tuple(self._build_labels(node, parent_id)),
            build_note=f"preparing {node.role.value}",
            capacity_request=current,
            capacity_plan=tail,
        )
        result = docker_image._docker_build_image(context, spec)
        if result is None:
            return
        if result != 0:
            context.record("docker_image", "err", f"{node.role.value} build failed")
            return
        if planned_index is not None:
            self._next_request_index += 1

    def _inherited_wheel_sha(self, node, parent_id) -> str:
        if node.role is ImageRole.PROJECT_OVERLAY and parent_id is not None:
            sha = self.docker.label(parent_id, runtime_lifecycle.LABEL_WHEEL_SHA256)
            fingerprint = self.docker.label(
                parent_id, runtime_lifecycle.LABEL_WHEEL_SOURCE_FINGERPRINT
            )
            if not sha or fingerprint != node.wheel_source_fingerprint:
                raise ImageLifecycleError("Project overlay parent has no matching wheel identity")
            return sha
        return self._wheel_sha256 or ""

    def _build_labels(
        self, node: ImageNode, parent_id: str | None = None
    ) -> list[tuple[str, str]]:
        labels = [
            (runtime_lifecycle.LABEL_SCHEMA, runtime_lifecycle.PROVENANCE_SCHEMA),
            (runtime_lifecycle.LABEL_ARTIFACT_ROLE, node.role.value),
            (runtime_lifecycle.LABEL_EFFECTIVE_INPUTS, node.effective_inputs or ""),
            (runtime_lifecycle.LABEL_RECIPE_FINGERPRINT, node.build.recipe_fingerprint),
            (runtime_lifecycle.LABEL_BUILD_ORIGIN, "local"),
            (runtime_lifecycle.LABEL_VERSION, node.payload.version),
        ]
        if node.role is ImageRole.PROJECT_DATA_LAYOUT:
            labels.append((project_image.LABEL_LAYOUT_REFERENCE, node.reference))
        if node.wheel_source_fingerprint is not None:
            labels.append(
                (
                    runtime_lifecycle.LABEL_WHEEL_SOURCE_FINGERPRINT,
                    node.wheel_source_fingerprint,
                )
            )
            labels.append(
                (runtime_lifecycle.LABEL_WHEEL_SHA256, self._inherited_wheel_sha(node, parent_id))
            )
        if node.runtime_base_contract is not None:
            labels.append(
                (runtime_lifecycle.LABEL_RUNTIME_BASE_CONTRACT, node.runtime_base_contract)
            )
        if node.standard_substrate_contract is not None:
            labels.append(
                (
                    runtime_lifecycle.LABEL_STANDARD_SUBSTRATE_CONTRACT,
                    node.standard_substrate_contract,
                )
            )
        if node.logical_selection_fingerprint is not None:
            labels.append(
                (
                    runtime_lifecycle.LABEL_LOGICAL_SELECTION_FINGERPRINT,
                    node.logical_selection_fingerprint,
                )
            )
        return labels

    def _role_build_inputs(
        self,
        context,
        node: ImageNode,
        root: Path | None,
        parent_reference: str | None,
    ) -> tuple[Path, tuple[tuple[str, str], ...], tuple[str, ...]] | None:
        from booley.harness.setup import docker_image

        parent = f"docker-image://{parent_reference}"
        source_inputs = self._source_role_build_inputs(node, root, parent)
        if source_inputs is not None:
            return source_inputs
        if node.role is ImageRole.RISCV_SUBSTRATE:
            return docker_data_dir(), (("booley-standard-substrate", parent),), ()
        if node.role in {ImageRole.PROJECT_SUBSTRATE, ImageRole.PROJECT_OVERLAY}:
            return (
                resolve_checkout_project_dir(self.project_root) / "docker",
                ((project_image.MANAGED_PROJECT_PARENT, parent),),
                (),
            )
        if node.role is not ImageRole.WHEEL_OVERLAY:
            raise ImageLifecycleError(f"unsupported image role {node.role!r}")
        if root is None:
            raise ImageLifecycleError("wheel build has no verified source context")
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

    @staticmethod
    def _source_role_build_inputs(
        node: ImageNode,
        root: Path | None,
        parent: str,
    ) -> tuple[Path, tuple[tuple[str, str], ...], tuple[str, ...]] | None:
        from booley.harness.setup import docker_image

        if node.role is ImageRole.RUNTIME_BASE:
            if root is None:
                raise ImageLifecycleError("runtime-base build has no verified source context")
            actual = source_image_build_contracts(root).runtime_base
            if actual != node.effective_inputs:
                raise ImageLifecycleError(
                    "runtime-base build context differs from the planned compatibility inputs"
                )
            return root, (), tuple(docker_image._runtime_base_build_metadata_args(root, actual))
        if node.role is ImageRole.STANDARD_SUBSTRATE:
            if root is None:
                raise ImageLifecycleError(
                    "standard-substrate build has no verified source context"
                )
            if source_image_build_contracts(root).standard_substrate != node.effective_inputs:
                raise ImageLifecycleError(
                    "standard-substrate build context differs from the planned "
                    "compatibility inputs"
                )
            return root, (("booley-runtime-base", parent),), ()
        return None


def _docker_adapter() -> DockerPort:
    return runtime_lifecycle._docker_adapter()


def _build_adapter(
    project_root: Path,
    docker: DockerPort,
    *,
    verbose: bool,
    refresh_runtime_base: bool = False,
    capacity_plan: DockerBuildPlan | None = None,
) -> BuildPort:
    return _LegacyBuildAdapter(
        project_root,
        docker,
        verbose=verbose,
        refresh_runtime_base=refresh_runtime_base,
        capacity_plan=capacity_plan,
    )


def _transaction_build_adapter(
    project_root: Path,
    docker: DockerPort,
    *,
    verbose: bool,
    requests: tuple[DockerBuildRequest, ...] = (),
) -> _IncrementalBuildAdapter:
    return _IncrementalBuildAdapter(project_root, docker, verbose=verbose, requests=requests)


def _artifact_policy(project_root: Path | None = None) -> ArtifactPolicy:
    """Select artifact acquisition from the authoritative installation identity."""
    import booley

    if booley.version_attribution.origin is VersionOrigin.SOURCE:
        return ArtifactPolicy.LOCAL_ONLY
    if booley.version_attribution.origin is VersionOrigin.DISTRIBUTION:
        if not embedded_official_release():
            return ArtifactPolicy.LOCAL_ONLY
        if project_root is not None and _uses_managed_project_overlay(project_root):
            return ArtifactPolicy.VERIFIED_RELEASE_THEN_LOCAL
        return ArtifactPolicy.VERIFIED_RELEASE_ONLY
    raise ImageLifecycleError(
        "cannot select managed Sandbox Images because the running Booley code is "
        "neither an attributed source checkout nor an installed distribution"
    )


def _uses_managed_project_overlay(project_root: Path) -> bool:
    selected = runtime_lifecycle._selected_reference(project_root)
    if selected != project_image.project_image_name(project_root):
        return False
    dockerfile = resolve_checkout_project_dir(project_root) / "docker" / "Dockerfile"
    return runtime_lifecycle._project_requirements_body(project_root) is not None and (
        not dockerfile.is_file() or project_image.is_managed_generated_file(dockerfile)
    )


def reconcile(
    scope: ImageScope,
    intent: Intent,
    *,
    verbose: bool = False,
    capacity_plan: DockerBuildPlan | None = None,
) -> LifecycleResult:
    """Reconcile an image graph with Project Initialization build adapters."""
    docker = _docker_adapter()
    import booley

    if booley.version_attribution.origin is VersionOrigin.DISTRIBUTION:
        runtime_lifecycle._expected_image_build_contracts()
    if isinstance(scope, HostImageScope):
        root = booley.version_attribution.source_root or Path.cwd().resolve()
    elif isinstance(scope, ProjectImageScope):
        root = scope.project_root.resolve()
    else:
        raise TypeError("image lifecycle scope must be HostImageScope or ProjectImageScope")
    layout_result = _planned_layout_reconciliation(scope, intent, docker, verbose)
    if layout_result is not None:
        return layout_result
    builder = (
        None
        if intent is Intent.CHECK
        else _build_adapter(
            root,
            docker,
            verbose=verbose,
            refresh_runtime_base=(intent is Intent.REFRESH and isinstance(scope, HostImageScope)),
            capacity_plan=capacity_plan,
        )
    )
    return runtime_lifecycle.reconcile(
        scope,
        intent,
        verbose=verbose,
        docker=docker,
        builder=builder,
        artifact_policy=_artifact_policy(root if isinstance(scope, ProjectImageScope) else None),
    )


def plan(scope: ProjectImageScope, *, intent: Intent = Intent.ENSURE) -> LifecyclePlan:
    """Plan source/release convergence using the installed artifact policy."""
    docker = _docker_adapter()
    return runtime_lifecycle.plan(
        scope,
        docker=docker,
        artifact_policy=_artifact_policy(scope.project_root),
        intent=intent,
    )


def host_capacity_requests(intent: Intent) -> tuple[DockerBuildRequest, ...]:
    """Return the exact local Host Bootstrap build sequence without mutation."""
    docker = _docker_adapter()
    policy = _artifact_policy()
    if policy is ArtifactPolicy.VERIFIED_RELEASE_ONLY:
        return ()
    checked = runtime_lifecycle.reconcile(
        HostImageScope(),
        Intent.CHECK,
        docker=docker,
        artifact_policy=policy,
    )
    if intent is not Intent.REFRESH and checked.status is Status.CURRENT:
        return ()
    import booley
    from booley.harness.setup import docker_image

    root = booley.version_attribution.source_root
    if root is None:
        return (
            DockerBuildRequest("runtime base", docker_image.LOCAL_RUNTIME_BASE_IMAGE),
            DockerBuildRequest("Sandbox Image", docker_image.DOCKER_IMAGE),
        )
    docker_dir = docker_data_dir()
    fingerprint = docker_image._image_build_fingerprint(root)
    return docker_image._local_build_capacity_requests(
        docker_dir / "Dockerfile.base",
        docker_dir / "Dockerfile",
        root,
        fingerprint,
        rebuild_runtime_base=intent is Intent.REFRESH,
    )


@contextmanager
def _verified_source_context(needs_source: bool) -> Iterator[Path | None]:
    import booley

    if not needs_source or booley.version_attribution.origin is not VersionOrigin.DISTRIBUTION:
        yield booley.version_attribution.source_root
        return
    with ExitStack() as stack:
        try:
            root = stack.enter_context(extracted_development_context())
        except (OSError, ValueError) as error:
            raise ImageLifecycleError(
                f"cannot verify the development Sandbox Image build context: {error}"
            ) from error
        yield root


def prepare(
    lifecycle_plan: LifecyclePlan,
    *,
    verbose: bool = False,
    future_requests: tuple[DockerBuildRequest, ...] = (),
) -> PreparedConvergence:
    """Build and verify candidates while leaving managed tags unchanged."""
    docker = _docker_adapter()
    lifecycle_build_plan = capacity_plan(lifecycle_plan)
    planned_requests = (
        lifecycle_build_plan.requests if lifecycle_build_plan is not None else ()
    ) + future_requests
    build_plan = DockerBuildPlan(planned_requests) if planned_requests else None
    if build_plan is not None:
        ensure_docker_build_capacity(
            ("docker", "build"),
            image=build_plan.current.output_tag,
            current_request=build_plan.current,
            remaining_plan=build_plan,
        )

    needs_source = any(
        node.role
        in {ImageRole.RUNTIME_BASE, ImageRole.STANDARD_SUBSTRATE, ImageRole.WHEEL_OVERLAY}
        and node.acquisition_policy is not ArtifactPolicy.VERIFIED_RELEASE_ONLY
        for node in lifecycle_plan.nodes
    )
    with _verified_source_context(needs_source) as source_root:
        builder = _transaction_build_adapter(
            lifecycle_plan.project_root,
            docker,
            verbose=verbose,
            requests=build_plan.requests if build_plan is not None else (),
        )
        builder.source_root = source_root
        return runtime_lifecycle.prepare(
            lifecycle_plan,
            docker=docker,
            builder=builder,
            verbose=verbose,
        )


def commit(prepared: PreparedConvergence) -> LifecycleResult:
    """Adopt one previously prepared convergence."""
    return runtime_lifecycle.commit(prepared, docker=_docker_adapter())


def validate(prepared: PreparedConvergence) -> None:
    """Revalidate prepared image inputs before a refresh parks the Session."""
    runtime_lifecycle.validate(prepared, docker=_docker_adapter())


def abort(prepared: PreparedConvergence) -> None:
    """Discard one previously prepared convergence."""
    runtime_lifecycle.abort(prepared, docker=_docker_adapter())


def reconcile_planned(
    scope: ProjectImageScope,
    intent: Intent,
    *,
    verbose: bool = False,
) -> LifecycleResult:
    """Plan, preflight, prepare, and atomically adopt one Project image graph."""
    prepared = prepare(plan(scope, intent=intent), verbose=verbose)
    try:
        return commit(prepared)
    except BaseException:
        abort(prepared)
        raise


def _planned_layout_reconciliation(scope, intent, docker, verbose) -> LifecycleResult | None:
    if not isinstance(scope, ProjectImageScope) or intent is Intent.CHECK:
        return None
    root = scope.project_root.resolve()
    if runtime_lifecycle._external_data_layout(root):
        selected = runtime_lifecycle._selected_reference(root)
        selected_id = docker.image_id(selected)
        if (
            selected_id
            and selected in {BASE_IMAGE, *FLAVOR_RECIPES, project_image.project_image_name(root)}
            and runtime_lifecycle._layout_alias_capable(selected_id)
        ):
            try:
                return reconcile_planned(scope, intent, verbose=verbose)
            except runtime_lifecycle.IncrementalPlanUnavailableError:
                pass  # Preserve user-owned recipes through the legacy builder.
    return None
