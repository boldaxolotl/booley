"""Authoritative provenance and ancestry reconciliation for Session Images."""

from __future__ import annotations

import hashlib
import subprocess
import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from booley.core.boundary import BoundaryError, is_str_list, require_dict, require_opt_str
from booley.harness.build_stamp import embedded_payload_fingerprint
from booley.runtime import project_image
from booley.runtime.image_provenance import (
    LABEL_BUILD_ORIGIN,
    LABEL_PARENT_ARTIFACT,
    LABEL_PAYLOAD_FINGERPRINT,
    LABEL_RECIPE_FINGERPRINT,
    LABEL_SCHEMA,
    LABEL_VERSION,
    LEGACY_FINGERPRINT_LABEL,
    PROVENANCE_SCHEMA,
    resolve_build_context_fingerprint,
    resolve_recipe_fingerprint,
)
from booley.runtime.paths import docker_data_dir
from booley.runtime.project_dir import resolve_checkout_project_dir

BASE_IMAGE = "booley-sandbox"
STABLE_RUNTIME_BASE_IMAGE = "booley-runtime-base:local"
FLAVOR_RECIPES = {"booley-sandbox-riscv": "Dockerfile.riscv"}

class Intent(StrEnum):
    """Caller intent for one image-lifecycle reconciliation."""

    CHECK = "check"
    ENSURE = "ensure"
    REFRESH = "refresh"


class Status(StrEnum):
    """Observable outcome of image-lifecycle reconciliation."""

    CURRENT = "current"
    STALE = "stale"
    CHANGED = "changed"
    EXTERNAL = "external"


class ImageLifecycleError(RuntimeError):
    """A managed Session Image could not be reconciled or verified."""


@dataclass(frozen=True)
class PayloadProvenance:
    """Booley payload identity embedded in a Session Image."""

    schema: str
    version: str
    fingerprint: str | None


@dataclass(frozen=True)
class BuildProvenance:
    """Recipe and direct-parent inputs that produced a Session Image."""

    recipe_fingerprint: str
    parent_artifact: str | None


@dataclass(frozen=True)
class Diagnostic:
    """Typed lifecycle fact for presentation by a caller."""

    code: str
    message: str


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


@dataclass(frozen=True)
class _ImageNode:
    reference: str
    recipe: Path
    payload: PayloadProvenance
    build: BuildProvenance
    parent: str | None = None

    @property
    def expected_labels(self) -> tuple[tuple[str, str], ...]:
        labels = [
            (LABEL_SCHEMA, self.payload.schema),
            (LABEL_VERSION, self.payload.version),
            (LABEL_RECIPE_FINGERPRINT, self.build.recipe_fingerprint),
        ]
        if self.payload.fingerprint:
            labels.append((LABEL_PAYLOAD_FINGERPRINT, self.payload.fingerprint))
            labels.append((LEGACY_FINGERPRINT_LABEL, self.payload.fingerprint))
        if self.build.parent_artifact:
            labels.append((LABEL_PARENT_ARTIFACT, self.build.parent_artifact))
        elif self.parent is not None:
            labels.append((LABEL_PARENT_ARTIFACT, ""))
        return tuple(labels)


class _DockerPort(Protocol):
    def image_id(self, image: str) -> str | None: ...

    def label(self, image: str, name: str) -> str | None: ...

    def tag(self, source: str, target: str) -> None: ...

    def remove_tag(self, image: str) -> None: ...


class _BuildPort(Protocol):
    def build(self, node: _ImageNode, *, force: bool) -> None: ...


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
    from booley.harness.init_docker_image import _image_build_fingerprint

    return _image_build_fingerprint(docker_data_dir().parents[3]) or embedded_payload_fingerprint()


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
    if configured:
        return configured
    dockerfile = _direct_project_dir(project_root) / "docker" / "Dockerfile"
    requirements_body = _project_requirements_body(project_root)
    return (
        project_image.project_image_name(project_root)
        if dockerfile.is_file() or requirements_body is not None
        else BASE_IMAGE
    )


def _base_node(payload: PayloadProvenance) -> _ImageNode:
    recipe = docker_data_dir() / "Dockerfile"
    return _ImageNode(
        reference=BASE_IMAGE,
        recipe=recipe,
        payload=payload,
        build=BuildProvenance(resolve_recipe_fingerprint((recipe,)), None),
    )


def _flavor_node(reference: str, parent: _ImageNode, payload: PayloadProvenance) -> _ImageNode:
    recipe = docker_data_dir() / FLAVOR_RECIPES[reference]
    return _ImageNode(
        reference=reference,
        recipe=recipe,
        payload=payload,
        build=BuildProvenance(resolve_recipe_fingerprint((recipe,)), parent.reference),
        parent=parent.reference,
    )


def _project_recipe_fingerprint(
    project_root: Path, requirements_body: str | None
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
    if (
        requirements_body is not None
        and managed_recipe
    ):
        dockerfile_body, requirements_content = project_image.managed_project_image_files(
            requirements_body
        )
        overrides = {
            "Dockerfile": dockerfile_body.encode(),
            "requirements.txt": requirements_content.encode(),
        }
    return resolve_build_context_fingerprint(docker_dir, overrides)


def _project_node(
    project_root: Path, parent_reference: str, payload: PayloadProvenance
) -> _ImageNode:
    docker_dir = _direct_project_dir(project_root) / "docker"
    dockerfile = docker_dir / "Dockerfile"
    return _ImageNode(
        reference=project_image.project_image_name(project_root),
        recipe=dockerfile,
        payload=payload,
        build=BuildProvenance(
            _project_recipe_fingerprint(project_root, _project_requirements_body(project_root)),
            parent_reference,
        ),
        parent=parent_reference,
    )


def _nodes(project_root: Path, selected: str, docker: _DockerPort) -> tuple[_ImageNode, ...]:
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
        raise ImageLifecycleError(f"unsupported managed Session Image {selected!r}")
    parent_name = project_image.dockerfile_parent_image(
        _direct_project_dir(project_root) / "docker" / "Dockerfile"
    )
    dockerfile = _direct_project_dir(project_root) / "docker" / "Dockerfile"
    if parent_name is None and dockerfile.is_file():
        raise ImageLifecycleError(
            "the automatically managed project image has ambiguous ancestry; "
            "use a single concrete FROM or add '# booley:parent=<image>'"
        )
    nodes: list[_ImageNode] = []
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
    nodes: tuple[_ImageNode, ...], docker: _DockerPort
) -> tuple[_ImageNode, ...]:
    resolved = []
    for node in nodes:
        parent_id = docker.image_id(node.parent) if node.parent else None
        resolved.append(
            _ImageNode(
                reference=node.reference,
                recipe=node.recipe,
                payload=node.payload,
                build=BuildProvenance(node.build.recipe_fingerprint, parent_id),
                parent=node.parent,
            )
        )
    return tuple(resolved)


def _node_current(node: _ImageNode, docker: _DockerPort) -> bool:
    if docker.image_id(node.reference) is None:
        return False
    if docker.label(node.reference, LABEL_SCHEMA) != PROVENANCE_SCHEMA:
        return _legacy_node_current(node, docker)
    if not _build_origin_and_base_parent_current(node, docker):
        return False
    for name, expected in node.expected_labels:
        expected_value = expected
        if name == LABEL_PARENT_ARTIFACT and node.parent is not None:
            expected_value = docker.image_id(node.parent) or ""
        if docker.label(node.reference, name) != expected_value:
            return False
    return True


def _build_origin_and_base_parent_current(node: _ImageNode, docker: _DockerPort) -> bool:
    """Validate acquisition-independent build origin and base ancestry."""
    origin = docker.label(node.reference, LABEL_BUILD_ORIGIN)
    if origin not in {"local", "registry"}:
        return False
    if node.reference != BASE_IMAGE:
        return True
    recorded_parent = docker.label(node.reference, LABEL_PARENT_ARTIFACT)
    if not recorded_parent:
        return False
    if origin == "registry":
        return True
    from booley.harness.docker_base_contract import contract as runtime_base_contract

    try:
        expected_contract = runtime_base_contract(docker_data_dir().parents[3])
    except (OSError, ValueError):
        return docker.image_id(STABLE_RUNTIME_BASE_IMAGE) == recorded_parent
    stable_contract = docker.label(
        STABLE_RUNTIME_BASE_IMAGE, "io.booley.runtime-base.contract"
    )
    return (
        stable_contract == expected_contract
        and docker.image_id(STABLE_RUNTIME_BASE_IMAGE) == recorded_parent
    )


def _legacy_node_current(node: _ImageNode, docker: _DockerPort) -> bool:
    legacy = docker.label(node.reference, LEGACY_FINGERPRINT_LABEL)
    if node.payload.fingerprint:
        return legacy == node.payload.fingerprint and node.parent is None
    version = docker.label(node.reference, LABEL_VERSION)
    if legacy and legacy.startswith("pulled:"):
        version = legacy.removeprefix("pulled:")
    return version == node.payload.version and node.parent is None


def _uses_accepted_legacy_provenance(node: _ImageNode, docker: _DockerPort) -> bool:
    return (
        docker.image_id(node.reference) is not None
        and docker.label(node.reference, LABEL_SCHEMA) != PROVENANCE_SCHEMA
        and _legacy_node_current(node, docker)
    )


def _backup_tag(project_root: Path, reference: str) -> str:
    identity = hashlib.sha256(
        f"{project_root.resolve()}\0{reference}".encode()
    ).hexdigest()[:16]
    return f"booley-lifecycle-backup-{identity}:prior"


def _retain_prior_tags(
    project_root: Path, nodes: tuple[_ImageNode, ...], docker: _DockerPort
) -> list[tuple[str, str | None]]:
    backups = []
    try:
        for node in nodes:
            prior_id = docker.image_id(node.reference)
            if prior_id is None:
                backups.append((node.reference, None))
                continue
            backup = _backup_tag(project_root, node.reference)
            docker.tag(prior_id, backup)
            backups.append((node.reference, backup))
    except BaseException:
        for _reference, backup in backups:
            if backup is not None:
                docker.remove_tag(backup)
        raise
    return backups


def _restore_prior_tags(
    backups: list[tuple[str, str | None]], docker: _DockerPort
) -> list[str]:
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
    project_root: Path,
    nodes: tuple[_ImageNode, ...],
    intent: Intent,
    docker: _DockerPort,
    builder: _BuildPort,
) -> tuple[str, ...]:
    changed = []
    backups: list[tuple[str, str | None]] = []
    cleanup_backups = False
    try:
        backups = _retain_prior_tags(project_root, nodes, docker)
        for node in nodes:
            current = _node_current(node, docker)
            if current and intent is not Intent.REFRESH:
                continue
            existed = docker.image_id(node.reference) is not None
            builder.build(node, force=intent is Intent.REFRESH or existed)
            if not _node_current(node, docker):
                if node.payload.fingerprint and not existed and intent is Intent.ENSURE:
                    builder.build(node, force=True)
                if not _node_current(node, docker):
                    raise ImageLifecycleError(
                        f"{node.reference} build completed without the expected provenance"
                    )
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


def _inspect_nodes(
    nodes: tuple[_ImageNode, ...], docker: _DockerPort
) -> tuple[tuple[str, ...], tuple[Diagnostic, ...]]:
    stale = tuple(node.reference for node in nodes if not _node_current(node, docker))
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
    nodes: tuple[_ImageNode, ...],
    docker: _DockerPort,
    stale: tuple[str, ...],
    legacy_diagnostics: tuple[Diagnostic, ...],
) -> LifecycleResult:
    return LifecycleResult(
        selected,
        docker.image_id(selected),
        Status.STALE if stale else Status.CURRENT,
        diagnostics=(
            *(
                Diagnostic("stale", f"{reference} is missing or has stale provenance")
                for reference in stale
            ),
            *legacy_diagnostics,
        ),
        payload_fingerprint=nodes[-1].payload.fingerprint,
    )


def reconcile(
    project_root: Path,
    intent: Intent,
    *,
    verbose: bool = False,
) -> LifecycleResult:
    """Resolve, reconcile, and verify one Project's selected Session Image."""
    root = project_root.resolve()
    docker = _docker_adapter()
    selected = _selected_reference(root)
    generated = project_image.project_image_name(root)
    if selected not in {BASE_IMAGE, generated, *FLAVOR_RECIPES}:
        return LifecycleResult(
            selected,
            docker.image_id(selected),
            Status.EXTERNAL,
            diagnostics=(Diagnostic("external", "image lifecycle is externally managed"),),
        )
    if intent is not Intent.CHECK and selected == generated:
        _prepare_project_recipe(root, _project_requirements_body(root))
        selected = _selected_reference(root)
    nodes = _nodes(root, selected, docker)
    stale, legacy_diagnostics = _inspect_nodes(nodes, docker)
    if intent is Intent.CHECK:
        return _check_result(selected, nodes, docker, stale, legacy_diagnostics)
    builder = _build_adapter(root, docker, verbose=verbose)
    changed = _mutate(root, nodes, intent, docker, builder)
    selected_id = docker.image_id(selected)
    if selected_id is None or not _node_current(nodes[-1], docker):
        raise ImageLifecycleError(f"selected Session Image {selected!r} did not verify")
    return LifecycleResult(
        selected,
        selected_id,
        Status.CHANGED if changed else Status.CURRENT,
        changed_images=changed,
        diagnostics=() if changed else legacy_diagnostics,
        payload_fingerprint=nodes[-1].payload.fingerprint,
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
                ["docker", "image", "rm", image], capture_output=True, text=True, check=False
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ImageLifecycleError(f"could not remove retained tag {image}: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise ImageLifecycleError(
                f"could not remove retained tag {image}: "
                f"{detail or f'Docker exited {result.returncode}'}"
            )


class _LegacyBuildAdapter:
    def __init__(self, project_root: Path, *, verbose: bool) -> None:
        self.project_root = project_root
        self.verbose = verbose

    def build(self, node: _ImageNode, *, force: bool) -> None:
        from booley.harness import init_cmd
        from booley.harness.init_common import InitContext
        from booley.harness.init_docker_image import (
            _step_docker_image,
            _try_pull_image,
            ensure_flavor_image,
        )

        shipped = node.reference == BASE_IMAGE or node.reference in FLAVOR_RECIPES
        source_root = docker_data_dir().parents[3]
        if shipped and not (source_root / "pyproject.toml").is_file():
            if not _try_pull_image(node.payload.version, node.reference):
                raise ImageLifecycleError(
                    f"could not pull current packaged Session Image {node.reference}"
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
            docker_dir = _direct_project_dir(self.project_root) / "docker"
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
                    node.reference, docker_dir, verbose=self.verbose
                ):
                    raise ImageLifecycleError(f"failed to rebuild {node.reference}")
            else:
                init_cmd._step_project_image(context)
        failures = [result.detail for result in context.results if result.status == "err"]
        if failures:
            raise ImageLifecycleError("; ".join(failures))


def _docker_adapter() -> _DockerPort:
    return _DockerCli()


def _build_adapter(
    project_root: Path, _docker: _DockerPort, *, verbose: bool
) -> _BuildPort:
    return _LegacyBuildAdapter(project_root, verbose=verbose)
