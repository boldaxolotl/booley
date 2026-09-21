"""Authoritative provenance and ancestry reconciliation for Sandbox Images."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tomllib
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Protocol, TypeAlias
from uuid import uuid4

from booley.core.boundary import (
    BoundaryError,
    is_str_list,
    require_dict,
    require_opt_str,
    require_str,
)
from booley.runtime import project_image
from booley.runtime.build_stamp import (
    embedded_payload_fingerprint,
    embedded_wheel_source_fingerprint,
    resolve_payload_fingerprint,
    resolve_wheel_source_fingerprint,
)
from booley.runtime.image_provenance import (
    LABEL_ARTIFACT_ROLE,
    LABEL_BUILD_ORIGIN,
    LABEL_EFFECTIVE_INPUTS,
    LABEL_PARENT_ARTIFACT,
    LABEL_PARENT_ARTIFACT_KIND,
    LABEL_PAYLOAD_FINGERPRINT,
    LABEL_RECIPE_FINGERPRINT,
    LABEL_SCHEMA,
    LABEL_VERSION,
    LABEL_WHEEL_SHA256,
    LABEL_WHEEL_SOURCE_FINGERPRINT,
    LEGACY_FINGERPRINT_LABEL,
    LEGACY_PROVENANCE_SCHEMA,
    PARENT_ARTIFACT_LOCAL_IMAGE_ID,
    PARENT_ARTIFACT_REGISTRY_DIGEST,
    PROVENANCE_SCHEMA,
    is_local_image_id,
    normalize_registry_digest,
    resolve_build_context_fingerprint,
    resolve_recipe_fingerprint,
)
from booley.runtime.paths import docker_data_dir
from booley.runtime.project_dir import resolve_checkout_project_dir

BASE_IMAGE = "booley-sandbox"
STABLE_RUNTIME_BASE_IMAGE = "booley-runtime-base:local"
STANDARD_SUBSTRATE_IMAGE = "booley-sandbox-standard-substrate:local"
RISCV_SUBSTRATE_IMAGE = "booley-sandbox-riscv-substrate:local"
FLAVOR_RECIPES = {"booley-sandbox-riscv": "Dockerfile.riscv"}
PUBLISHED_REGISTRY = "ghcr.io/boldaxolotl"
_STABLE_RELEASE_TAG = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)")


class Intent(StrEnum):
    """Caller intent for one image-lifecycle reconciliation."""

    CHECK = "check"
    ENSURE = "ensure"
    REFRESH = "refresh"


class ArtifactSource(StrEnum):
    """Authorized source for one managed Sandbox Image mutation."""

    LOCAL_BUILD = "local-build"
    VERIFIED_RELEASE_PULL = "verified-release-pull"


class ArtifactPolicy(StrEnum):
    """Ordered acquisition policy for Booley-shipped Sandbox Images."""

    LOCAL_ONLY = "local-only"
    VERIFIED_RELEASE_ONLY = "verified-release-only"
    VERIFIED_RELEASE_THEN_LOCAL = "verified-release-then-local"

    @property
    def sources(self) -> tuple[ArtifactSource, ...]:
        return {
            ArtifactPolicy.LOCAL_ONLY: (ArtifactSource.LOCAL_BUILD,),
            ArtifactPolicy.VERIFIED_RELEASE_ONLY: (ArtifactSource.VERIFIED_RELEASE_PULL,),
            ArtifactPolicy.VERIFIED_RELEASE_THEN_LOCAL: (
                ArtifactSource.VERIFIED_RELEASE_PULL,
                ArtifactSource.LOCAL_BUILD,
            ),
        }[self]


class Status(StrEnum):
    """Observable outcome of image-lifecycle reconciliation."""

    CURRENT = "current"
    STALE = "stale"
    CHANGED = "changed"
    EXTERNAL = "external"


class ImageRole(StrEnum):
    """One independently compatible node in the managed Sandbox Image graph."""

    RUNTIME_BASE = "runtime-base"
    STANDARD_SUBSTRATE = "standard-substrate"
    RISCV_SUBSTRATE = "riscv-substrate"
    PROJECT_SUBSTRATE = "project-substrate"
    WHEEL_OVERLAY = "wheel-overlay"


class PlanAction(StrEnum):
    """The mutation, if any, needed to converge one image node."""

    REUSE = "reuse"
    BUILD = "build"
    PULL = "pull"


class ImageLifecycleError(RuntimeError):
    """A managed Sandbox Image could not be reconciled or verified."""


@dataclass(frozen=True, slots=True)
class HostImageScope:
    """Project-independent ownership of the base Sandbox Image."""


@dataclass(frozen=True, slots=True)
class ProjectImageScope:
    """One Project's selected image plus an optional reconciled host base."""

    project_root: Path
    base: LifecycleResult | None = None


ImageScope: TypeAlias = HostImageScope | ProjectImageScope


@dataclass(frozen=True)
class PayloadProvenance:
    """Booley payload identity embedded in a Sandbox Image."""

    schema: str
    version: str
    fingerprint: str | None


@dataclass(frozen=True)
class BuildProvenance:
    """Recipe and direct-parent inputs that produced a Sandbox Image."""

    recipe_fingerprint: str
    parent_artifact: str | None


@dataclass(frozen=True)
class Diagnostic:
    """Typed lifecycle fact for presentation by a caller."""

    code: str
    message: str


@dataclass(frozen=True, slots=True)
class ImageReference:
    """One exact local Docker reference and its immutable artifact ID."""

    reference: str
    image_id: str


@dataclass(frozen=True, slots=True)
class ImageCleanup:
    """Release-tag cleanup facts produced by one lifecycle reconciliation."""

    pending: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    retained_required: tuple[str, ...] = ()


@dataclass(frozen=True)
class LifecycleResult:
    """Stable facts returned across the image-lifecycle seam."""

    selected_reference: str
    selected_id: str | None
    status: Status
    changed_images: tuple[str, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()
    payload_fingerprint: str | None = None
    requires_spec_reseed: bool = False
    requires_runtime_recreation: bool = False
    cleanup: ImageCleanup = field(default_factory=ImageCleanup)
    wheel_source_fingerprint: str | None = None
    wheel_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class PlanStep:
    """One deterministic lifecycle decision and its stable reason code."""

    reference: str
    role: ImageRole
    action: PlanAction
    reason: Diagnostic


@dataclass(frozen=True, slots=True)
class InputSnapshot:
    """Compatibility inputs revalidated before prepared tags are adopted."""

    identities: tuple[tuple[str, str, str, str | None], ...]


@dataclass(frozen=True, slots=True)
class LifecyclePlan:
    """Pure, ordered convergence decision for one selected Sandbox Image."""

    nodes: tuple[ImageNode, ...]
    steps: tuple[PlanStep, ...]
    selected_reference: str
    input_snapshot: InputSnapshot
    project_root: Path


@dataclass(frozen=True, slots=True)
class PreparedImage:
    """Verified transaction-scoped artifact and its realized ancestry."""

    reference: str
    candidate_reference: str
    image_id: str
    parent_artifact: str | None
    wheel_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class TagSnapshot:
    """Managed tag identity captured before transaction commit."""

    reference: str
    image_id: str | None


@dataclass(frozen=True, slots=True)
class PreparedConvergence:
    """Built and verified candidates which have not changed managed tags."""

    plan: LifecyclePlan
    input_snapshot: InputSnapshot
    candidates: tuple[PreparedImage, ...]
    prior_tags: tuple[TagSnapshot, ...]


@dataclass(frozen=True)
class ImageNode:
    reference: str
    recipe: Path
    payload: PayloadProvenance
    build: BuildProvenance
    parent: str | None = None
    role: ImageRole | None = None
    effective_inputs: str | None = None
    parent_compatibility_key: str | None = None
    wheel_source_fingerprint: str | None = None
    acquisition_policy: ArtifactPolicy = ArtifactPolicy.LOCAL_ONLY

    @property
    def expected_labels(self) -> tuple[tuple[str, str], ...]:
        labels = [
            (LABEL_SCHEMA, self.payload.schema),
            (LABEL_VERSION, self.payload.version),
            (LABEL_RECIPE_FINGERPRINT, self.build.recipe_fingerprint),
        ]
        if self.payload.fingerprint:
            labels.append((LABEL_PAYLOAD_FINGERPRINT, self.payload.fingerprint))
            if self.role is None:
                labels.append((LEGACY_FINGERPRINT_LABEL, self.payload.fingerprint))
        if self.role is not None:
            labels.append((LABEL_ARTIFACT_ROLE, self.role.value))
        if self.effective_inputs is not None:
            labels.append((LABEL_EFFECTIVE_INPUTS, self.effective_inputs))
        if self.wheel_source_fingerprint is not None:
            labels.append((LABEL_WHEEL_SOURCE_FINGERPRINT, self.wheel_source_fingerprint))
        if self.build.parent_artifact:
            labels.append((LABEL_PARENT_ARTIFACT, self.build.parent_artifact))
        elif self.parent is not None:
            labels.append((LABEL_PARENT_ARTIFACT, ""))
        return tuple(labels)


class DockerPort(Protocol):
    def image_id(self, image: str) -> str | None: ...

    def label(self, image: str, name: str) -> str | None: ...

    def repo_digests(self, image: str) -> tuple[str, ...]: ...

    def image_references(self) -> tuple[ImageReference, ...]: ...

    def container_image_ids(self) -> frozenset[str]: ...

    def tag(self, source: str, target: str) -> None: ...

    def remove_tag(self, image: str) -> None: ...


class BuildPort(Protocol):
    """Acquire a node and optionally return a staged reference for validation."""

    def build(
        self,
        node: ImageNode,
        *,
        force: bool,
        source: ArtifactSource,
    ) -> str | None: ...


class TransactionBuildPort(Protocol):
    """Role-aware builder which never writes a managed image tag directly."""

    def prepare(
        self,
        node: ImageNode,
        *,
        candidate_reference: str,
        parent_reference: str | None,
    ) -> str: ...


def _candidate_reference_is_transactional(reference: str) -> bool:
    return reference.startswith("booley-lifecycle-") and reference.endswith(":candidate")


def _discard_orphaned_candidates(docker: DockerPort) -> None:
    """Remove unjournaled transaction tags left by a process crash."""
    for image in docker.image_references():
        if _candidate_reference_is_transactional(image.reference):
            docker.remove_tag(image.reference)


def _build_adapter(
    _project_root: Path,
    _docker: DockerPort,
    *,
    verbose: bool,
) -> BuildPort:
    """Require an orchestration layer to provide image-building policy."""
    del verbose
    raise TypeError("mutating image reconciliation requires a build adapter")


def _transaction_build_adapter(
    _project_root: Path,
    _docker: DockerPort,
    *,
    verbose: bool,
) -> TransactionBuildPort:
    del verbose
    raise TypeError("incremental convergence requires a transaction build adapter")


def _expected_version() -> str:
    from booley import __version__

    root = docker_data_dir().parents[3]
    version_file = root / "VERSION"
    try:
        source_version = version_file.read_text(encoding="utf-8").strip()
    except OSError:
        source_version = ""
    return source_version or __version__


def _expected_payload_fingerprint() -> str | None:
    return (
        resolve_payload_fingerprint(docker_data_dir().parents[3]) or embedded_payload_fingerprint()
    )


def _expected_wheel_source_fingerprint() -> str | None:
    root = docker_data_dir().parents[3]
    return resolve_wheel_source_fingerprint(root) or embedded_wheel_source_fingerprint()


def _direct_project_dir(project_root: Path) -> Path:
    return resolve_checkout_project_dir(project_root)


def _sandbox_config(project_root: Path) -> dict[str, object]:
    config = _direct_project_dir(project_root) / "booley.toml"
    if not config.is_file():
        return {}
    try:
        with config.open("rb") as config_file:
            document = tomllib.load(config_file)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ImageLifecycleError(f"could not read {config}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ImageLifecycleError(f"could not parse {config}: {exc}") from exc
    raw = document.get("sandbox")
    if raw is None:
        return {}
    try:
        return require_dict(raw, field="[sandbox]")
    except BoundaryError as exc:
        raise ImageLifecycleError(f"invalid {config}: {exc}") from exc


def _configured_image(project_root: Path) -> str | None:
    sandbox = _sandbox_config(project_root)
    try:
        raw = require_opt_str(sandbox, "image", field="sandbox.image")
    except BoundaryError as exc:
        raise ImageLifecycleError(f"invalid sandbox.image: {exc}") from exc
    return raw.strip() if raw is not None else None


def _project_requirements_body(project_root: Path) -> str | None:
    raw = _sandbox_config(project_root).get("pip_requirements")
    if raw is not None and not is_str_list(raw):
        raise ImageLifecycleError("[sandbox].pip_requirements must be a list of strings")
    requested = raw if isinstance(raw, list) else None
    requirements, missing = project_image.resolve_requirements(project_root, requested)
    if missing:
        raise ImageLifecycleError(
            "configured sandbox pip requirements are missing: " + ", ".join(missing)
        )
    if not requirements:
        return None
    body, kept, _skipped, _dropped = project_image.consolidated_requirements(
        project_root, requirements
    )
    return body if kept else None


def _prepare_project_recipe(project_root: Path, requirements_body: str | None) -> None:
    """Refresh Booley-owned recipe files while preserving any user-owned recipe."""
    docker_dir = _direct_project_dir(project_root) / "docker"
    dockerfile = docker_dir / "Dockerfile"
    requirements = docker_dir / "requirements.txt"
    if not project_image.is_managed_generated_file(dockerfile):
        return
    if not project_image.is_managed_generated_file(requirements):
        if not dockerfile.is_file():
            project_image.write_managed_dockerfile(docker_dir)
        return
    if requirements_body is None:
        dockerfile.unlink(missing_ok=True)
        requirements.unlink(missing_ok=True)
        return
    project_image.write_project_image_files(docker_dir, requirements_body)


def _selected_reference(project_root: Path) -> str:
    configured = _configured_image(project_root)
    requirements_body = _project_requirements_body(project_root)
    if configured in FLAVOR_RECIPES and requirements_body is not None:
        return project_image.project_image_name(project_root)
    if configured:
        return configured
    dockerfile = _direct_project_dir(project_root) / "docker" / "Dockerfile"
    return (
        project_image.project_image_name(project_root)
        if dockerfile.is_file() or requirements_body is not None
        else BASE_IMAGE
    )


def _base_node(payload: PayloadProvenance) -> ImageNode:
    recipe = docker_data_dir() / "Dockerfile"
    return ImageNode(
        reference=BASE_IMAGE,
        recipe=recipe,
        payload=payload,
        build=BuildProvenance(resolve_recipe_fingerprint((recipe,)), None),
    )


def _flavor_node(reference: str, parent: ImageNode, payload: PayloadProvenance) -> ImageNode:
    recipe = docker_data_dir() / FLAVOR_RECIPES[reference]
    return ImageNode(
        reference=reference,
        recipe=recipe,
        payload=payload,
        build=BuildProvenance(resolve_recipe_fingerprint((recipe,)), parent.reference),
        parent=parent.reference,
    )


def _project_recipe_fingerprint(
    project_root: Path,
    requirements_body: str | None,
    *,
    parent_image: str = BASE_IMAGE,
) -> str:
    docker_dir = _direct_project_dir(project_root) / "docker"
    dockerfile = docker_dir / "Dockerfile"
    requirements = docker_dir / "requirements.txt"
    overrides = None
    managed_recipe = project_image.is_managed_generated_file(
        dockerfile
    ) and project_image.is_managed_generated_file(requirements)
    if requirements_body is None and managed_recipe and dockerfile.is_file():
        return hashlib.sha256(b"<no-managed-project-image>").hexdigest()
    if requirements_body is not None and managed_recipe:
        dockerfile_body, requirements_content = project_image.managed_project_image_files(
            requirements_body,
            parent_image=parent_image,
        )
        overrides = {
            "Dockerfile": dockerfile_body.encode(),
            "requirements.txt": requirements_content.encode(),
        }
    return resolve_build_context_fingerprint(docker_dir, overrides)


def _project_node(
    project_root: Path, parent_reference: str, payload: PayloadProvenance
) -> ImageNode:
    docker_dir = _direct_project_dir(project_root) / "docker"
    dockerfile = docker_dir / "Dockerfile"
    return ImageNode(
        reference=project_image.project_image_name(project_root),
        recipe=dockerfile,
        payload=payload,
        build=BuildProvenance(
            _project_recipe_fingerprint(project_root, _project_requirements_body(project_root)),
            parent_reference,
        ),
        parent=parent_reference,
    )


def _nodes(project_root: Path, selected: str, docker: DockerPort) -> tuple[ImageNode, ...]:
    payload = PayloadProvenance(
        PROVENANCE_SCHEMA,
        _expected_version(),
        _expected_payload_fingerprint(),
    )
    if selected == BASE_IMAGE:
        return _with_parent_artifacts((_base_node(payload),), docker)
    if selected in FLAVOR_RECIPES:
        base = _base_node(payload)
        return _with_parent_artifacts((base, _flavor_node(selected, base, payload)), docker)
    generated = project_image.project_image_name(project_root)
    if selected != generated:
        raise ImageLifecycleError(f"unsupported managed Sandbox Image {selected!r}")
    parent_name = project_image.dockerfile_parent_image(
        _direct_project_dir(project_root) / "docker" / "Dockerfile"
    )
    dockerfile = _direct_project_dir(project_root) / "docker" / "Dockerfile"
    if parent_name is None and dockerfile.is_file():
        raise ImageLifecycleError(
            "the automatically managed project image has ambiguous ancestry; "
            "use a single concrete FROM or add '# booley:parent=<image>'"
        )
    nodes: list[ImageNode] = []
    parent_reference = BASE_IMAGE
    if parent_name in FLAVOR_RECIPES:
        base = _base_node(payload)
        parent = _flavor_node(parent_name, base, payload)
        nodes.extend((base, parent))
        parent_reference = parent.reference
    elif parent_name in (None, BASE_IMAGE):
        nodes.append(_base_node(payload))
    else:
        parent_reference = parent_name
    nodes.append(_project_node(project_root, parent_reference, payload))
    return _with_parent_artifacts(tuple(nodes), docker)


def _with_parent_artifacts(
    nodes: tuple[ImageNode, ...], docker: DockerPort
) -> tuple[ImageNode, ...]:
    resolved = []
    for node in nodes:
        parent_id = docker.image_id(node.parent) if node.parent else None
        resolved.append(
            ImageNode(
                reference=node.reference,
                recipe=node.recipe,
                payload=node.payload,
                build=BuildProvenance(node.build.recipe_fingerprint, parent_id),
                parent=node.parent,
            )
        )
    return tuple(resolved)


def _hash_paths(root: Path, paths: tuple[Path, ...]) -> str:
    """Hash an explicit set of files and trees using stable repository paths."""
    files: set[Path] = set()
    for path in paths:
        if path.is_dir():
            files.update(item for item in path.rglob("*") if item.is_file())
        elif path.is_file():
            files.add(path)
    digest = hashlib.sha256()
    for path in sorted(files):
        try:
            name = path.relative_to(root).as_posix()
        except ValueError:
            name = path.as_posix()
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _compatibility_key(node: ImageNode) -> str:
    digest = hashlib.sha256()
    for value in (
        node.role.value if node.role else "",
        node.effective_inputs or "",
        node.build.recipe_fingerprint,
        node.parent_compatibility_key or "",
    ):
        digest.update(value.encode())
        digest.update(b"\0")
    return digest.hexdigest()


def standard_substrate_fingerprint(root: Path) -> str:
    """Return the effective-input identity of the standard tool substrate."""
    return _hash_paths(
        root,
        (
            root / "crates" / "bwave" / "Cargo.toml",
            root / "crates" / "bwave" / "Cargo.lock",
            root / "crates" / "bwave" / "src",
            root / "crates" / "bwave" / "schema",
            root / "crates" / "bwave" / "docs",
            root / "src" / "booley" / "data" / "edalize" / "verible.py",
        ),
    )


def _graph_node(
    *,
    reference: str,
    role: ImageRole,
    recipe: Path,
    effective_inputs: str,
    parent: ImageNode | None,
    wheel_source_fingerprint: str | None = None,
    policy: ArtifactPolicy = ArtifactPolicy.LOCAL_ONLY,
    recipe_fingerprint: str | None = None,
) -> ImageNode:
    payload = PayloadProvenance(
        PROVENANCE_SCHEMA,
        _expected_version(),
        None,
    )
    return ImageNode(
        reference=reference,
        recipe=recipe,
        payload=payload,
        build=BuildProvenance(
            recipe_fingerprint or resolve_recipe_fingerprint((recipe,)),
            None,
        ),
        parent=parent.reference if parent else None,
        role=role,
        effective_inputs=effective_inputs,
        parent_compatibility_key=_compatibility_key(parent) if parent else None,
        wheel_source_fingerprint=wheel_source_fingerprint,
        acquisition_policy=policy,
    )


def _source_graph(project_root: Path, selected: str) -> tuple[ImageNode, ...]:
    root = docker_data_dir().parents[3]
    docker_dir = docker_data_dir()
    runtime, standard = _source_graph_base(root, docker_dir)
    nodes = [runtime, standard]
    substrate = standard
    if _configured_image(project_root) == "booley-sandbox-riscv":
        substrate = _source_graph_riscv(standard, docker_dir)
        nodes.append(substrate)
    if selected == project_image.project_image_name(project_root):
        project_node = _source_graph_project(project_root, selected, substrate)
        nodes.append(project_node)
        substrate = project_node
    overlay_recipe = docker_dir / "Dockerfile.wheel"
    wheel_source = _expected_wheel_source_fingerprint()
    if wheel_source is None:
        raise ImageLifecycleError("could not determine the Booley wheel-source fingerprint")
    overlay = _graph_node(
        reference=selected,
        role=ImageRole.WHEEL_OVERLAY,
        recipe=overlay_recipe,
        effective_inputs=wheel_source,
        parent=substrate,
        wheel_source_fingerprint=wheel_source,
    )
    nodes.append(overlay)
    return tuple(nodes)


def _source_graph_base(root: Path, docker_dir: Path) -> tuple[ImageNode, ImageNode]:
    from booley.runtime.docker_base_contract import contract as runtime_base_contract

    runtime = _graph_node(
        reference=STABLE_RUNTIME_BASE_IMAGE,
        role=ImageRole.RUNTIME_BASE,
        recipe=docker_dir / "Dockerfile.base",
        effective_inputs=runtime_base_contract(root),
        parent=None,
    )
    standard = _graph_node(
        reference=STANDARD_SUBSTRATE_IMAGE,
        role=ImageRole.STANDARD_SUBSTRATE,
        recipe=docker_dir / "Dockerfile.substrate",
        effective_inputs=standard_substrate_fingerprint(root),
        parent=runtime,
    )
    return runtime, standard


def _source_graph_riscv(standard: ImageNode, docker_dir: Path) -> ImageNode:
    return _graph_node(
        reference=RISCV_SUBSTRATE_IMAGE,
        role=ImageRole.RISCV_SUBSTRATE,
        recipe=docker_dir / "Dockerfile.riscv",
        effective_inputs=resolve_recipe_fingerprint((docker_dir / "Dockerfile.riscv",)),
        parent=standard,
    )


def _source_graph_project(
    project_root: Path, selected: str, substrate: ImageNode
) -> ImageNode:
    requirements_body = _project_requirements_body(project_root)
    dockerfile = _direct_project_dir(project_root) / "docker" / "Dockerfile"
    if dockerfile.is_file() and not project_image.is_managed_generated_file(dockerfile):
        raise ImageLifecycleError(
            "wheel-only convergence is unavailable for a user-owned Project Docker "
            "recipe; preserve its exact ancestry and rebuild it explicitly"
        )
    generated_recipe, _ = project_image.managed_project_image_files(
        requirements_body or "", parent_image=project_image.MANAGED_PROJECT_PARENT
    )
    return _graph_node(
        reference=f"{selected}-substrate",
        role=ImageRole.PROJECT_SUBSTRATE,
        recipe=dockerfile,
        effective_inputs=_project_recipe_fingerprint(
            project_root,
            requirements_body,
            parent_image=project_image.MANAGED_PROJECT_PARENT,
        ),
        parent=substrate,
        recipe_fingerprint=hashlib.sha256(generated_recipe.encode()).hexdigest(),
    )


def _complete_release_node(selected: str) -> ImageNode:
    wheel_source = _expected_wheel_source_fingerprint()
    if wheel_source is None:
        raise ImageLifecycleError("installed release has no wheel-source fingerprint")
    return _graph_node(
        reference=selected,
        role=ImageRole.WHEEL_OVERLAY,
        recipe=docker_data_dir() / "Dockerfile.wheel",
        effective_inputs=wheel_source,
        parent=None,
        wheel_source_fingerprint=wheel_source,
        policy=ArtifactPolicy.VERIFIED_RELEASE_ONLY,
    )


def _snapshot(nodes: tuple[ImageNode, ...]) -> InputSnapshot:
    return InputSnapshot(
        tuple(
            (
                node.reference,
                node.effective_inputs or "",
                node.build.recipe_fingerprint,
                node.parent_compatibility_key,
            )
            for node in nodes
        )
    )


def _planned_reason(
    node: ImageNode,
    docker: DockerPort,
    *,
    parent_invalid: bool,
) -> Diagnostic | None:
    if docker.image_id(node.reference) is None:
        return Diagnostic("missing", "managed artifact is missing")
    if parent_invalid:
        return Diagnostic("parent-changed", "a selected ancestor must be replaced")
    provenance = _node_provenance_reason(node, docker)
    if provenance is not None:
        return provenance
    return _node_ancestry_reason(node, docker)


def _node_provenance_reason(node: ImageNode, docker: DockerPort) -> Diagnostic | None:
    if docker.label(node.reference, LABEL_SCHEMA) != PROVENANCE_SCHEMA or docker.label(
        node.reference, LABEL_ARTIFACT_ROLE
    ) != (node.role.value if node.role else None):
        return Diagnostic("legacy-migration", "artifact uses an older image topology")
    if docker.label(node.reference, LABEL_EFFECTIVE_INPUTS) != node.effective_inputs:
        return Diagnostic("inputs-changed", "node-owned effective inputs changed")
    if docker.label(node.reference, LABEL_RECIPE_FINGERPRINT) != node.build.recipe_fingerprint:
        return Diagnostic("recipe-changed", "the node build recipe changed")
    if node.wheel_source_fingerprint is not None and (
        docker.label(node.reference, LABEL_WHEEL_SOURCE_FINGERPRINT)
        != node.wheel_source_fingerprint
        or not docker.label(node.reference, LABEL_WHEEL_SHA256)
    ):
        return Diagnostic("inputs-changed", "wheel identity differs or is incomplete")
    if _node_artifact_source(node, node.reference, docker) not in node.acquisition_policy.sources:
        return Diagnostic("wrong-origin", "artifact source violates acquisition policy")
    return None


def _node_ancestry_reason(node: ImageNode, docker: DockerPort) -> Diagnostic | None:
    if node.acquisition_policy is ArtifactPolicy.VERIFIED_RELEASE_ONLY:
        recorded = docker.label(node.reference, LABEL_PARENT_ARTIFACT)
        if (
            docker.label(node.reference, LABEL_PARENT_ARTIFACT_KIND)
            != PARENT_ARTIFACT_REGISTRY_DIGEST
            or recorded is None
            or normalize_registry_digest(recorded) is None
        ):
            return Diagnostic("parent-changed", "release ancestry is not digest-qualified")
    if node.parent is not None:
        expected_parent = docker.image_id(node.parent)
        if expected_parent is None:
            return Diagnostic("parent-changed", "reusable parent artifact is missing")
        if docker.label(node.reference, LABEL_PARENT_ARTIFACT_KIND) != (
            PARENT_ARTIFACT_LOCAL_IMAGE_ID
        ) or docker.label(node.reference, LABEL_PARENT_ARTIFACT) != expected_parent:
            return Diagnostic("parent-changed", "recorded immutable parent differs")
    return None


def plan(
    scope: ProjectImageScope,
    *,
    docker: DockerPort | None = None,
    artifact_policy: ArtifactPolicy = ArtifactPolicy.LOCAL_ONLY,
) -> LifecyclePlan:
    """Observe and purely plan the minimal invalid closure for one Project."""
    if not isinstance(scope, ProjectImageScope):
        raise TypeError("incremental planning requires a ProjectImageScope")
    resolved_docker = docker or _docker_adapter()
    root = scope.project_root.resolve()
    selected = _selected_reference(root)
    if selected not in {BASE_IMAGE, "booley-sandbox-riscv", project_image.project_image_name(root)}:
        raise ImageLifecycleError(f"Sandbox Image {selected!r} is externally managed")
    nodes = (
        (_complete_release_node(selected),)
        if artifact_policy is ArtifactPolicy.VERIFIED_RELEASE_ONLY
        else tuple(
            replace(node, acquisition_policy=artifact_policy)
            for node in _source_graph(root, selected)
        )
    )
    invalid = False
    steps = []
    for node in nodes:
        reason = _planned_reason(node, resolved_docker, parent_invalid=invalid)
        if reason is None:
            steps.append(PlanStep(node.reference, node.role, PlanAction.REUSE, Diagnostic("current", "verified compatible artifact")))
            continue
        invalid = True
        action = (
            PlanAction.PULL
            if artifact_policy is ArtifactPolicy.VERIFIED_RELEASE_ONLY
            and node.role is ImageRole.WHEEL_OVERLAY
            else PlanAction.BUILD
        )
        steps.append(PlanStep(node.reference, node.role, action, reason))
    return LifecyclePlan(nodes, tuple(steps), selected, _snapshot(nodes), root)


def _candidate_reference(transaction_id: str, node: ImageNode) -> str:
    role = node.role.value if node.role else "image"
    return f"booley-lifecycle-{transaction_id}-{role}:candidate"


def _verify_prepared_image(
    node: ImageNode,
    reference: str,
    parent_artifact: str | None,
    docker: DockerPort,
) -> PreparedImage:
    image_id = docker.image_id(reference)
    if image_id is None:
        raise ImageLifecycleError(f"prepared candidate {reference!r} disappeared")
    required = _prepared_provenance(node, parent_artifact)
    if parent_artifact is None and node.acquisition_policy is ArtifactPolicy.VERIFIED_RELEASE_ONLY:
        parent_artifact = _verify_release_parent(reference, docker)
    mismatched = [
        name for name, expected in required.items() if docker.label(reference, name) != expected
    ]
    if mismatched:
        raise ImageLifecycleError(
            f"prepared candidate {reference!r} has invalid provenance: "
            + ", ".join(mismatched)
        )
    wheel_sha256 = docker.label(reference, LABEL_WHEEL_SHA256)
    if node.role is ImageRole.WHEEL_OVERLAY and not wheel_sha256:
        raise ImageLifecycleError(f"prepared wheel overlay {reference!r} has no wheel SHA-256")
    return PreparedImage(node.reference, reference, image_id, parent_artifact, wheel_sha256)


def _prepared_provenance(
    node: ImageNode, parent_artifact: str | None
) -> dict[str, str]:
    required = {
        LABEL_SCHEMA: PROVENANCE_SCHEMA,
        LABEL_ARTIFACT_ROLE: node.role.value if node.role else "",
        LABEL_EFFECTIVE_INPUTS: node.effective_inputs or "",
        LABEL_RECIPE_FINGERPRINT: node.build.recipe_fingerprint,
        LABEL_BUILD_ORIGIN: (
            "registry"
            if node.acquisition_policy is ArtifactPolicy.VERIFIED_RELEASE_ONLY
            else "local"
        ),
    }
    if node.wheel_source_fingerprint is not None:
        required[LABEL_WHEEL_SOURCE_FINGERPRINT] = node.wheel_source_fingerprint
    if parent_artifact is not None:
        required[LABEL_PARENT_ARTIFACT] = parent_artifact
        required[LABEL_PARENT_ARTIFACT_KIND] = PARENT_ARTIFACT_LOCAL_IMAGE_ID
    return required


def _verify_release_parent(reference: str, docker: DockerPort) -> str:
    recorded = docker.label(reference, LABEL_PARENT_ARTIFACT)
    if (
        docker.label(reference, LABEL_PARENT_ARTIFACT_KIND) != PARENT_ARTIFACT_REGISTRY_DIGEST
        or recorded is None
        or normalize_registry_digest(recorded) is None
    ):
        raise ImageLifecycleError(f"prepared release {reference!r} has invalid registry ancestry")
    return recorded


def prepare(
    lifecycle_plan: LifecyclePlan,
    *,
    docker: DockerPort | None = None,
    builder: TransactionBuildPort | None = None,
    verbose: bool = False,
) -> PreparedConvergence:
    """Acquire and verify candidates without changing any managed image tag."""
    resolved_docker = docker or _docker_adapter()
    _discard_orphaned_candidates(resolved_docker)
    resolved_builder = builder or _transaction_build_adapter(
        lifecycle_plan.project_root,
        resolved_docker,
        verbose=verbose,
    )
    transaction_id = uuid4().hex[:16]
    prior_tags = tuple(
        TagSnapshot(node.reference, resolved_docker.image_id(node.reference))
        for node in lifecycle_plan.nodes
    )
    prepared: list[PreparedImage] = []
    realized: dict[str, PreparedImage] = {}
    candidate_references: list[str] = []
    try:
        for node, step in zip(lifecycle_plan.nodes, lifecycle_plan.steps, strict=True):
            acquired, candidates = _prepare_node(
                node,
                step,
                transaction_id,
                realized,
                resolved_docker,
                resolved_builder,
            )
            prepared.append(acquired)
            realized[node.reference] = acquired
            candidate_references.extend(candidates)
    except BaseException:
        for reference in candidate_references:
            if resolved_docker.image_id(reference) is not None:
                resolved_docker.remove_tag(reference)
        raise
    return PreparedConvergence(
        lifecycle_plan,
        lifecycle_plan.input_snapshot,
        tuple(prepared),
        prior_tags,
    )


def _prepare_node(
    node: ImageNode,
    step: PlanStep,
    transaction_id: str,
    realized: dict[str, PreparedImage],
    docker: DockerPort,
    builder: TransactionBuildPort,
) -> tuple[PreparedImage, tuple[str, ...]]:
    parent = realized.get(node.parent or "")
    parent_reference = parent.candidate_reference if parent else node.parent
    parent_artifact = (
        parent.image_id if parent else docker.image_id(node.parent) if node.parent else None
    )
    if step.action is PlanAction.REUSE:
        return _verify_prepared_image(node, node.reference, parent_artifact, docker), ()
    candidate = _candidate_reference(transaction_id, node)
    built = builder.prepare(node, candidate_reference=candidate, parent_reference=parent_reference)
    candidates = [candidate]
    if built not in candidates and built != node.reference:
        candidates.append(built)
    return _verify_prepared_image(node, built, parent_artifact, docker), tuple(candidates)


def validate(
    prepared: PreparedConvergence,
    *,
    docker: DockerPort | None = None,
) -> None:
    """Revalidate inputs and candidate identities before external downtime."""
    resolved_docker = docker or _docker_adapter()
    current = plan(
        ProjectImageScope(prepared.plan.project_root),
        docker=resolved_docker,
        artifact_policy=prepared.plan.nodes[-1].acquisition_policy,
    )
    if current.input_snapshot != prepared.input_snapshot:
        raise ImageLifecycleError("image inputs changed while candidates were being prepared")
    for image in prepared.candidates:
        if image.candidate_reference == image.reference:
            continue
        if resolved_docker.image_id(image.candidate_reference) != image.image_id:
            raise ImageLifecycleError(
                f"prepared candidate {image.candidate_reference!r} changed before commit"
            )


def _restore_tag_snapshots(
    snapshots: tuple[TagSnapshot, ...], docker: DockerPort
) -> tuple[str, ...]:
    failures = []
    for snapshot in reversed(snapshots):
        try:
            if snapshot.image_id is None:
                if docker.image_id(snapshot.reference) is not None:
                    docker.remove_tag(snapshot.reference)
            else:
                docker.tag(snapshot.image_id, snapshot.reference)
        except ImageLifecycleError as exc:
            failures.append(f"{snapshot.reference}: {exc}")
    return tuple(failures)


def abort(
    prepared: PreparedConvergence,
    *,
    docker: DockerPort | None = None,
) -> None:
    """Discard transaction references without disturbing managed tags."""
    resolved_docker = docker or _docker_adapter()
    for image in prepared.candidates:
        if image.candidate_reference != image.reference:
            resolved_docker.remove_tag(image.candidate_reference)


def commit(
    prepared: PreparedConvergence,
    *,
    docker: DockerPort | None = None,
) -> LifecycleResult:
    """Revalidate inputs and atomically adopt verified candidates in graph order."""
    resolved_docker = docker or _docker_adapter()
    try:
        validate(prepared, docker=resolved_docker)
    except ImageLifecycleError:
        abort(prepared, docker=resolved_docker)
        raise
    changed: list[str] = []
    try:
        for step, image in zip(prepared.plan.steps, prepared.candidates, strict=True):
            if step.action is PlanAction.REUSE:
                continue
            resolved_docker.tag(image.image_id, image.reference)
            if resolved_docker.image_id(image.reference) != image.image_id:
                raise ImageLifecycleError(f"managed tag {image.reference!r} changed during commit")
            changed.append(image.reference)
    except BaseException as exc:
        failures = _restore_tag_snapshots(prepared.prior_tags, resolved_docker)
        if failures:
            raise ImageLifecycleError(
                "candidate adoption failed and prior tags could not be restored: "
                + "; ".join(failures)
            ) from exc
        raise
    final = prepared.candidates[-1]
    abort(prepared, docker=resolved_docker)
    return LifecycleResult(
        prepared.plan.selected_reference,
        final.image_id,
        Status.CHANGED if changed else Status.CURRENT,
        changed_images=tuple(changed),
        payload_fingerprint=prepared.plan.nodes[-1].wheel_source_fingerprint,
        requires_spec_reseed=bool(changed),
        requires_runtime_recreation=bool(changed),
        wheel_source_fingerprint=prepared.plan.nodes[-1].wheel_source_fingerprint,
        wheel_sha256=final.wheel_sha256,
    )


def recover(
    prepared: PreparedConvergence,
    *,
    docker: DockerPort | None = None,
) -> LifecycleResult:
    """Idempotently finish adoption of a durably recorded prepared convergence."""
    return commit(prepared, docker=docker)


def converge(
    scope: ProjectImageScope,
    *,
    docker: DockerPort | None = None,
    builder: TransactionBuildPort | None = None,
    artifact_policy: ArtifactPolicy = ArtifactPolicy.LOCAL_ONLY,
    verbose: bool = False,
) -> LifecycleResult:
    """Convenience convergence for callers which do not need a commit point."""
    resolved_docker = docker or _docker_adapter()
    lifecycle_plan = plan(scope, docker=resolved_docker, artifact_policy=artifact_policy)
    acquired = prepare(
        lifecycle_plan,
        docker=resolved_docker,
        builder=builder,
        verbose=verbose,
    )
    return commit(acquired, docker=resolved_docker)


def _node_current(
    node: ImageNode,
    docker: DockerPort,
    reference: str | None = None,
) -> bool:
    inspected = reference or node.reference
    if docker.image_id(inspected) is None:
        return False
    schema = docker.label(inspected, LABEL_SCHEMA)
    if schema == LEGACY_PROVENANCE_SCHEMA:
        return _schema_one_node_current(node, inspected, docker)
    if schema is None:
        return _legacy_node_current(node, inspected, docker)
    return (
        schema == PROVENANCE_SCHEMA
        and _schema_two_parent_current(node, inspected, docker)
        and all(
            name == LABEL_PARENT_ARTIFACT or docker.label(inspected, name) == expected
            for name, expected in node.expected_labels
        )
    )


def _node_artifact_source(
    node: ImageNode,
    inspected: str,
    docker: DockerPort,
) -> ArtifactSource | None:
    origin = docker.label(inspected, LABEL_BUILD_ORIGIN)
    if origin == "local":
        return ArtifactSource.LOCAL_BUILD
    if origin == "registry":
        return ArtifactSource.VERIFIED_RELEASE_PULL
    legacy = docker.label(inspected, LEGACY_FINGERPRINT_LABEL)
    if legacy and legacy.startswith("pulled:"):
        return ArtifactSource.VERIFIED_RELEASE_PULL
    if legacy and node.payload.fingerprint:
        return ArtifactSource.LOCAL_BUILD
    return None


def _node_current_from(
    node: ImageNode,
    docker: DockerPort,
    sources: tuple[ArtifactSource, ...],
    reference: str | None = None,
) -> bool:
    inspected = reference or node.reference
    return _node_current(node, docker, inspected) and (
        _node_artifact_source(node, inspected, docker) in sources
    )


def _sources_for_node(
    node: ImageNode,
    shipped_sources: tuple[ArtifactSource, ...],
) -> tuple[ArtifactSource, ...]:
    if node.reference == BASE_IMAGE or node.reference in FLAVOR_RECIPES:
        return shipped_sources
    return (ArtifactSource.LOCAL_BUILD,)


def _schema_two_parent_current(
    node: ImageNode,
    inspected: str,
    docker: DockerPort,
) -> bool:
    """Validate typed schema-two parent provenance."""
    origin = docker.label(inspected, LABEL_BUILD_ORIGIN)
    kind = docker.label(inspected, LABEL_PARENT_ARTIFACT_KIND)
    recorded_parent = docker.label(inspected, LABEL_PARENT_ARTIFACT)
    expected_kind = {
        "local": PARENT_ARTIFACT_LOCAL_IMAGE_ID,
        "registry": PARENT_ARTIFACT_REGISTRY_DIGEST,
    }.get(origin or "")
    if expected_kind is None or kind != expected_kind or not recorded_parent:
        return False
    if node.reference == BASE_IMAGE:
        return _base_parent_current(origin or "", recorded_parent, docker)
    if node.parent is None:
        return False
    if kind == PARENT_ARTIFACT_LOCAL_IMAGE_ID:
        return (
            is_local_image_id(recorded_parent) and docker.image_id(node.parent) == recorded_parent
        )
    normalized = normalize_registry_digest(recorded_parent)
    return normalized is not None and normalized in docker.repo_digests(node.parent)


def _base_parent_current(origin: str, recorded_parent: str, docker: DockerPort) -> bool:
    """Validate the build-only parent of the base Sandbox Image."""
    if origin == "registry":
        return normalize_registry_digest(recorded_parent) is not None
    if not is_local_image_id(recorded_parent):
        return False
    from booley.runtime.docker_base_contract import contract as runtime_base_contract

    try:
        expected_contract = runtime_base_contract(docker_data_dir().parents[3])
    except (OSError, ValueError):
        return docker.image_id(STABLE_RUNTIME_BASE_IMAGE) == recorded_parent
    stable_contract = docker.label(STABLE_RUNTIME_BASE_IMAGE, "io.booley.runtime-base.contract")
    return (
        stable_contract == expected_contract
        and docker.image_id(STABLE_RUNTIME_BASE_IMAGE) == recorded_parent
    )


def _schema_one_node_current(
    node: ImageNode,
    inspected: str,
    docker: DockerPort,
) -> bool:
    """Migrate schema one without weakening its exact local ancestry checks."""
    origin = docker.label(inspected, LABEL_BUILD_ORIGIN)
    if origin not in {"local", "registry"}:
        return False
    labels_current = all(
        name in {LABEL_SCHEMA, LABEL_PARENT_ARTIFACT} or docker.label(inspected, name) == expected
        for name, expected in node.expected_labels
    )
    if not labels_current:
        return False
    recorded_parent = docker.label(inspected, LABEL_PARENT_ARTIFACT)
    if node.reference == BASE_IMAGE:
        return bool(recorded_parent) and (
            origin == "registry" or _base_parent_current(origin, recorded_parent or "", docker)
        )
    return (
        origin == "local"
        and node.parent is not None
        and bool(recorded_parent)
        and docker.image_id(node.parent) == recorded_parent
    )


def _legacy_node_current(node: ImageNode, inspected: str, docker: DockerPort) -> bool:
    legacy = docker.label(inspected, LEGACY_FINGERPRINT_LABEL)
    if node.payload.fingerprint:
        return legacy == node.payload.fingerprint and node.parent is None
    version = docker.label(inspected, LABEL_VERSION)
    if legacy and legacy.startswith("pulled:"):
        version = legacy.removeprefix("pulled:")
    return version == node.payload.version and node.parent is None


def _uses_accepted_legacy_provenance(node: ImageNode, docker: DockerPort) -> bool:
    return (
        docker.image_id(node.reference) is not None
        and docker.label(node.reference, LABEL_SCHEMA) is None
        and _legacy_node_current(node, node.reference, docker)
    )


def _backup_tag(scope_identity: str, reference: str) -> str:
    identity = hashlib.sha256(f"{scope_identity}\0{reference}".encode()).hexdigest()[:16]
    return f"booley-lifecycle-backup-{identity}:prior"


def _retain_prior_tags(
    scope_identity: str, nodes: tuple[ImageNode, ...], docker: DockerPort
) -> list[tuple[str, str | None]]:
    backups = []
    try:
        for node in nodes:
            prior_id = docker.image_id(node.reference)
            if prior_id is None:
                backups.append((node.reference, None))
                continue
            backup = _backup_tag(scope_identity, node.reference)
            docker.tag(prior_id, backup)
            backups.append((node.reference, backup))
    except BaseException:
        for _reference, backup in backups:
            if backup is not None:
                docker.remove_tag(backup)
        raise
    return backups


def _restore_prior_tags(backups: list[tuple[str, str | None]], docker: DockerPort) -> list[str]:
    failures = []
    for reference, backup in reversed(backups):
        try:
            if backup is None:
                if docker.image_id(reference) is not None:
                    docker.remove_tag(reference)
            else:
                docker.tag(backup, reference)
        except ImageLifecycleError as exc:
            failures.append(f"{reference}: {exc}")
    return failures


def _mutate(
    scope_identity: str,
    nodes: tuple[ImageNode, ...],
    intent: Intent,
    docker: DockerPort,
    builder: BuildPort,
    *,
    refreshable: frozenset[str] | None = None,
    shipped_sources: tuple[ArtifactSource, ...] = (ArtifactSource.LOCAL_BUILD,),
) -> tuple[str, ...]:
    changed = []
    backups: list[tuple[str, str | None]] = []
    cleanup_backups = False
    try:
        backups = _retain_prior_tags(scope_identity, nodes, docker)
        for node in nodes:
            sources = _sources_for_node(node, shipped_sources)
            current = _node_current_from(node, docker, sources)
            refresh = intent is Intent.REFRESH and (
                refreshable is None or node.reference in refreshable
            )
            if current and not refresh:
                continue
            existed = docker.image_id(node.reference) is not None
            _build_node(node, intent, docker, builder, sources, refresh, existed)
            changed.append(node.reference)
        cleanup_backups = True
    except BaseException as exc:
        restore_failures = _restore_prior_tags(backups, docker)
        if restore_failures:
            raise ImageLifecycleError(
                "image reconciliation failed and prior tags remain under their "
                "booley-lifecycle-backup-* recovery names: " + "; ".join(restore_failures)
            ) from exc
        cleanup_backups = True
        raise
    finally:
        if cleanup_backups:
            for _reference, backup in backups:
                if backup is not None:
                    docker.remove_tag(backup)
    return tuple(changed)


def _build_node(
    node: ImageNode,
    intent: Intent,
    docker: DockerPort,
    builder: BuildPort,
    sources: tuple[ArtifactSource, ...],
    refresh: bool,
    existed: bool,
) -> None:
    failures = []
    for index, source in enumerate(sources):
        force = refresh or existed or index > 0
        try:
            candidate = builder.build(node, force=force, source=source)
        except ImageLifecycleError as exc:
            failures.append(f"{source.value} failed for {node.reference}: {exc}")
            continue
        inspected = candidate or node.reference
        if _adopt_current_candidate(node, inspected, source, docker):
            return
        if (
            source is ArtifactSource.LOCAL_BUILD
            and len(sources) == 1
            and node.payload.fingerprint
            and not existed
            and intent is Intent.ENSURE
        ):
            retry = builder.build(node, force=True, source=source) or node.reference
            if _adopt_current_candidate(node, retry, source, docker):
                return
        failures.append(
            f"{source.value} for {node.reference} completed without the expected provenance"
        )
    raise ImageLifecycleError("; ".join(failures))


def _adopt_current_candidate(
    node: ImageNode,
    candidate: str,
    source: ArtifactSource,
    docker: DockerPort,
) -> bool:
    sources = (source,)
    if not _node_current_from(node, docker, sources, candidate):
        return False
    if candidate == node.reference:
        return True
    candidate_id = docker.image_id(candidate)
    if candidate_id is None:
        raise ImageLifecycleError(f"verified candidate {candidate} disappeared before adoption")
    docker.tag(candidate_id, node.reference)
    if not _node_current_from(node, docker, sources):
        raise ImageLifecycleError(
            f"verified candidate {candidate} changed during adoption as {node.reference}"
        )
    return True


def _inspect_nodes(
    nodes: tuple[ImageNode, ...],
    docker: DockerPort,
    shipped_sources: tuple[ArtifactSource, ...],
) -> tuple[tuple[str, ...], tuple[Diagnostic, ...]]:
    stale = tuple(
        node.reference
        for node in nodes
        if not _node_current_from(node, docker, _sources_for_node(node, shipped_sources))
    )
    legacy = tuple(
        node.reference for node in nodes if _uses_accepted_legacy_provenance(node, docker)
    )
    diagnostics = tuple(
        Diagnostic(
            "legacy-provenance",
            f"{reference} uses accepted legacy provenance; its next rebuild will migrate it",
        )
        for reference in legacy
    )
    return stale, diagnostics


def _check_result(
    selected: str,
    nodes: tuple[ImageNode, ...],
    docker: DockerPort,
    stale: tuple[str, ...],
    legacy_diagnostics: tuple[Diagnostic, ...],
    shipped_sources: tuple[ArtifactSource, ...],
) -> LifecycleResult:
    def pending_action(reference: str) -> str:
        source = (
            shipped_sources[0]
            if reference == BASE_IMAGE or reference in FLAVOR_RECIPES
            else ArtifactSource.LOCAL_BUILD
        )
        return {
            ArtifactSource.LOCAL_BUILD: "would build locally",
            ArtifactSource.VERIFIED_RELEASE_PULL: "would pull verified release",
        }[source]

    return LifecycleResult(
        selected,
        docker.image_id(selected),
        Status.STALE if stale else Status.CURRENT,
        diagnostics=(
            *(
                Diagnostic(
                    "stale",
                    f"{reference} is missing or has stale provenance; {pending_action(reference)}",
                )
                for reference in stale
            ),
            *legacy_diagnostics,
        ),
        payload_fingerprint=nodes[-1].payload.fingerprint,
    )


def _published_release_repository(reference: str) -> str:
    return f"{PUBLISHED_REGISTRY}/{reference}"


def _release_tag(reference: str, managed_reference: str) -> str | None:
    prefix = _published_release_repository(managed_reference) + ":"
    if not reference.startswith(prefix):
        return None
    tag = reference.removeprefix(prefix)
    return tag if _STABLE_RELEASE_TAG.fullmatch(tag) else None


def _release_cleanup_candidates(
    managed_reference: str,
    inventory: tuple[ImageReference, ...],
) -> tuple[ImageReference, ...]:
    """Return disposable official acquisition tags, including the current version."""
    candidates = (
        item for item in inventory if _release_tag(item.reference, managed_reference) is not None
    )
    return tuple(sorted(candidates, key=lambda item: item.reference))


def _reconcile_release_tag_cleanup(
    managed_reference: str,
    intent: Intent,
    docker: DockerPort,
) -> ImageCleanup:
    inventory = docker.image_references()
    candidates = _release_cleanup_candidates(managed_reference, inventory)
    if not candidates:
        return ImageCleanup()
    references_by_id: dict[str, set[str]] = {}
    for item in inventory:
        references_by_id.setdefault(item.image_id, set()).add(item.reference)
    container_ids = docker.container_image_ids()
    pending: list[str] = []
    removed: list[str] = []
    retained: list[str] = []
    for candidate in candidates:
        aliases = references_by_id.get(candidate.image_id, set()) - {candidate.reference}
        if candidate.image_id in container_ids and not aliases:
            retained.append(candidate.reference)
            continue
        if intent is Intent.CHECK:
            pending.append(candidate.reference)
            continue
        if docker.image_id(candidate.reference) != candidate.image_id:
            raise ImageLifecycleError(
                f"refused to clean changed Docker image reference {candidate.reference}"
            )
        docker.remove_tag(candidate.reference)
        removed.append(candidate.reference)
    return ImageCleanup(tuple(pending), tuple(removed), tuple(retained))


def _with_cleanup(result: LifecycleResult, cleanup: ImageCleanup) -> LifecycleResult:
    status = Status.CHANGED if cleanup.removed else result.status
    return replace(result, status=status, cleanup=cleanup)


def reconcile(
    scope: ImageScope,
    intent: Intent,
    *,
    verbose: bool = False,
    docker: DockerPort | None = None,
    builder: BuildPort | None = None,
    artifact_policy: ArtifactPolicy = ArtifactPolicy.LOCAL_ONLY,
) -> LifecycleResult:
    """Reconcile one image graph under an explicit artifact-selection policy."""
    resolved_docker = docker or _docker_adapter()
    if intent is not Intent.CHECK and builder is None:
        if isinstance(scope, HostImageScope):
            root = docker_data_dir().parents[3]
        elif isinstance(scope, ProjectImageScope):
            root = scope.project_root.resolve()
        else:
            raise TypeError("image lifecycle scope must be HostImageScope or ProjectImageScope")
        builder = _build_adapter(root, resolved_docker, verbose=verbose)
    if isinstance(scope, HostImageScope):
        return _reconcile_host(intent, resolved_docker, builder, artifact_policy.sources)
    if not isinstance(scope, ProjectImageScope):
        raise TypeError("image lifecycle scope must be HostImageScope or ProjectImageScope")
    return _reconcile_project(
        scope,
        intent,
        resolved_docker,
        builder,
        artifact_policy.sources,
    )


def _reconcile_host(
    intent: Intent,
    docker: DockerPort,
    builder: BuildPort | None,
    shipped_sources: tuple[ArtifactSource, ...],
) -> LifecycleResult:
    payload = PayloadProvenance(
        PROVENANCE_SCHEMA,
        _expected_version(),
        _expected_payload_fingerprint(),
    )
    nodes = _with_parent_artifacts((_base_node(payload),), docker)
    stale, legacy_diagnostics = _inspect_nodes(nodes, docker, shipped_sources)
    if intent is Intent.CHECK:
        result = _check_result(
            BASE_IMAGE,
            nodes,
            docker,
            stale,
            legacy_diagnostics,
            shipped_sources,
        )
        cleanup = _reconcile_release_tag_cleanup(BASE_IMAGE, intent, docker)
        return _with_cleanup(result, cleanup)
    if builder is None:
        raise TypeError("mutating image reconciliation requires a build adapter")
    changed = _mutate(
        "host",
        nodes,
        intent,
        docker,
        builder,
        shipped_sources=shipped_sources,
    )
    result = _verified_result(
        BASE_IMAGE,
        nodes[-1],
        changed,
        legacy_diagnostics,
        docker,
        shipped_sources,
    )
    cleanup = _reconcile_release_tag_cleanup(BASE_IMAGE, intent, docker)
    return _with_cleanup(result, cleanup)


def _reconcile_project(
    scope: ProjectImageScope,
    intent: Intent,
    docker: DockerPort,
    builder: BuildPort | None,
    shipped_sources: tuple[ArtifactSource, ...],
) -> LifecycleResult:
    root = scope.project_root.resolve()
    selected = _selected_reference(root)
    generated = project_image.project_image_name(root)
    if selected not in {BASE_IMAGE, generated, *FLAVOR_RECIPES}:
        return _external_result(selected, docker)
    if intent is not Intent.CHECK and selected == generated:
        _prepare_project_recipe(root, _project_requirements_body(root))
        selected = _selected_reference(root)
    if selected == BASE_IMAGE and scope.base is not None:
        _verify_host_base(scope.base, docker)
        return LifecycleResult(
            BASE_IMAGE,
            scope.base.selected_id,
            Status.CURRENT,
            payload_fingerprint=scope.base.payload_fingerprint,
        )
    nodes = _nodes(root, selected, docker)
    if scope.base is not None:
        _verify_host_base(scope.base, docker)
    return _reconcile_project_nodes(
        root,
        selected,
        nodes,
        intent,
        docker,
        builder,
        shipped_sources,
    )


def _external_result(selected: str, docker: DockerPort) -> LifecycleResult:
    return LifecycleResult(
        selected,
        docker.image_id(selected),
        Status.EXTERNAL,
        diagnostics=(Diagnostic("external", "image lifecycle is externally managed"),),
    )


def _reconcile_project_nodes(
    root: Path,
    selected: str,
    nodes: tuple[ImageNode, ...],
    intent: Intent,
    docker: DockerPort,
    builder: BuildPort | None,
    shipped_sources: tuple[ArtifactSource, ...],
) -> LifecycleResult:
    stale, legacy_diagnostics = _inspect_nodes(nodes, docker, shipped_sources)
    if intent is Intent.CHECK:
        result = _check_result(
            selected,
            nodes,
            docker,
            stale,
            legacy_diagnostics,
            shipped_sources,
        )
        if selected in FLAVOR_RECIPES:
            cleanup = _reconcile_release_tag_cleanup(selected, intent, docker)
            return _with_cleanup(result, cleanup)
        return result
    if builder is None:
        raise TypeError("mutating image reconciliation requires a build adapter")
    refreshable = frozenset({node.reference for node in nodes if node.reference != BASE_IMAGE})
    changed = _mutate(
        str(root),
        nodes,
        intent,
        docker,
        builder,
        refreshable=refreshable if intent is Intent.REFRESH else None,
        shipped_sources=shipped_sources,
    )
    result = _verified_result(
        selected,
        nodes[-1],
        changed,
        legacy_diagnostics,
        docker,
        shipped_sources,
    )
    if selected in FLAVOR_RECIPES:
        cleanup = _reconcile_release_tag_cleanup(selected, intent, docker)
        return _with_cleanup(result, cleanup)
    return result


def _verify_host_base(result: LifecycleResult, docker: DockerPort) -> None:
    if result.selected_reference != BASE_IMAGE or result.status not in {
        Status.CURRENT,
        Status.CHANGED,
    }:
        raise ImageLifecycleError(
            "Project image reconciliation requires a current host base image"
        )
    if result.selected_id is None or docker.image_id(BASE_IMAGE) != result.selected_id:
        raise ImageLifecycleError("host base image identity changed before Project reconciliation")


def _verified_result(
    selected: str,
    selected_node: ImageNode,
    changed: tuple[str, ...],
    legacy_diagnostics: tuple[Diagnostic, ...],
    docker: DockerPort,
    shipped_sources: tuple[ArtifactSource, ...],
) -> LifecycleResult:
    selected_id = docker.image_id(selected)
    sources = _sources_for_node(selected_node, shipped_sources)
    if selected_id is None or not _node_current_from(selected_node, docker, sources):
        raise ImageLifecycleError(f"selected Sandbox Image {selected!r} did not verify")
    return LifecycleResult(
        selected,
        selected_id,
        Status.CHANGED if changed else Status.CURRENT,
        changed_images=changed,
        diagnostics=() if changed else legacy_diagnostics,
        payload_fingerprint=selected_node.payload.fingerprint,
        requires_spec_reseed=bool(changed),
        requires_runtime_recreation=bool(changed),
    )


class _DockerCli:
    def image_id(self, image: str) -> str | None:
        try:
            return project_image.docker_image_id(image)
        except project_image.DockerImageError as exc:
            raise ImageLifecycleError(str(exc)) from exc

    def label(self, image: str, name: str) -> str | None:
        try:
            result = subprocess.run(
                [
                    "docker",
                    "image",
                    "inspect",
                    "-f",
                    f'{{{{ index .Config.Labels "{name}" }}}}',
                    image,
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ImageLifecycleError(f"could not inspect Docker label {name!r}: {exc}") from exc
        value = result.stdout.strip()
        if result.returncode == 0:
            return value if value and value != "<no value>" else None
        detail = (result.stderr or result.stdout).strip()
        if "no such image" in detail.lower():
            return None
        raise ImageLifecycleError(
            f"could not inspect Docker label {name!r} on {image!r}: "
            f"{detail or f'Docker exited {result.returncode}'}"
        )

    def repo_digests(self, image: str) -> tuple[str, ...]:
        """Return normalized registry identities attached to a local image."""
        try:
            result = subprocess.run(
                ["docker", "image", "inspect", "-f", "{{json .RepoDigests}}", image],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ImageLifecycleError(f"could not inspect Docker RepoDigests: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            if "no such image" in detail.lower():
                return ()
            raise ImageLifecycleError(
                f"could not inspect Docker RepoDigests on {image!r}: "
                f"{detail or f'Docker exited {result.returncode}'}"
            )
        try:
            values = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ImageLifecycleError(
                f"Docker returned invalid RepoDigests JSON for {image!r}"
            ) from exc
        if values is None or values == []:
            return ()
        if not is_str_list(values):
            raise ImageLifecycleError(f"Docker returned invalid RepoDigests for {image!r}")
        normalized = tuple(normalize_registry_digest(value) for value in values)
        if any(value is None for value in normalized):
            raise ImageLifecycleError(f"Docker returned malformed RepoDigests for {image!r}")
        return tuple(value for value in normalized if value is not None)

    def image_references(self) -> tuple[ImageReference, ...]:
        try:
            result = subprocess.run(
                ["docker", "image", "ls", "--no-trunc", "--format", "{{json .}}"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ImageLifecycleError(
                f"could not inventory Docker image references: {exc}"
            ) from exc
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise ImageLifecycleError(
                "could not inventory Docker image references: "
                f"{detail or f'Docker exited {result.returncode}'}"
            )
        return _parse_image_references(result.stdout)

    def container_image_ids(self) -> frozenset[str]:
        try:
            result = subprocess.run(
                ["docker", "container", "ls", "--all", "--quiet", "--no-trunc"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ImageLifecycleError(f"could not inventory Docker containers: {exc}") from exc
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise ImageLifecycleError(
                f"could not inventory Docker containers: "
                f"{detail or f'Docker exited {result.returncode}'}"
            )
        return _inspect_container_image_ids(result.stdout.splitlines())

    def tag(self, source: str, target: str) -> None:
        try:
            result = subprocess.run(
                ["docker", "tag", source, target], capture_output=True, text=True, check=False
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ImageLifecycleError(f"could not retain image {source}: {exc}") from exc
        if result.returncode != 0:
            raise ImageLifecycleError(f"could not retain image {source}: {result.stderr.strip()}")

    def remove_tag(self, image: str) -> None:
        try:
            result = subprocess.run(
                ["docker", "image", "rm", "--no-prune", image],
                capture_output=True,
                text=True,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ImageLifecycleError(f"could not remove retained tag {image}: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise ImageLifecycleError(
                f"could not remove retained tag {image}: "
                f"{detail or f'Docker exited {result.returncode}'}"
            )


def _parse_image_references(output: str) -> tuple[ImageReference, ...]:
    references: dict[str, str] = {}
    for row_number, line in enumerate(output.splitlines(), start=1):
        try:
            document = require_dict(json.loads(line), field="Docker image inventory row")
            repository = require_str(document, "Repository")
            tag = require_str(document, "Tag")
            image_id = require_str(document, "ID")
        except (BoundaryError, json.JSONDecodeError) as exc:
            raise ImageLifecycleError(
                f"Docker returned malformed image inventory row {row_number}: {exc}"
            ) from exc
        if not is_local_image_id(image_id):
            raise ImageLifecycleError(
                f"Docker returned malformed image inventory row {row_number}: invalid image ID"
            )
        reference = f"{repository}:{tag}"
        previous = references.setdefault(reference, image_id)
        if previous != image_id:
            raise ImageLifecycleError(f"Docker returned conflicting IDs for {reference}")
    return tuple(ImageReference(reference, image_id) for reference, image_id in references.items())


def _inspect_container_image_ids(container_ids: list[str]) -> frozenset[str]:
    image_ids = set()
    for container_id in container_ids:
        try:
            result = subprocess.run(
                ["docker", "container", "inspect", "--format", "{{.Image}}", container_id],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ImageLifecycleError(
                f"could not inspect Docker container {container_id}: {exc}"
            ) from exc
        image_id = result.stdout.strip()
        if result.returncode or not is_local_image_id(image_id):
            detail = (result.stderr or result.stdout).strip()
            raise ImageLifecycleError(
                f"could not inspect Docker container {container_id}: "
                f"{detail or f'Docker exited {result.returncode}'}"
            )
        image_ids.add(image_id)
    return frozenset(image_ids)


def _docker_adapter() -> DockerPort:
    return _DockerCli()
