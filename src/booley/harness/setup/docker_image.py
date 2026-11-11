"""Docker sandbox base-image build, pull, and staleness-fingerprint logic.

Extracted from ``init_cmd.py`` (Single Responsibility): everything that builds,
pulls, inspects, or fingerprints the project-agnostic ``booley-sandbox`` base
image lives here. The build-fingerprint guard rebuilds an image whose baked-in
source has since changed instead of silently skipping it (the failure that hid
the container-side skill deployment behind a stale image).

Depends only on ``init_common`` for console output and :class:`InitContext`;
it never imports back from ``init_cmd``.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from booley.harness.setup.common import InitContext, current, err, hint, info, ok, warn
from booley.runtime import interactive_docker as idk
from booley.runtime.build_stamp import (
    embedded_payload_fingerprint,
    iter_payload_files,
    resolve_build_commit,
    resolve_payload_fingerprint,
    resolve_source_updated_at,
    resolve_wheel_source_fingerprint,
    runtime_image_build_stamp,
    stamp_constants,
    wheel_embedded_commit,
)
from booley.runtime.docker_build import DockerBuildResult, run_docker_build
from booley.runtime.docker_capacity import (
    BuildEstimateClass,
    DockerBuildPlan,
    DockerBuildRequest,
    DockerCacheEvidence,
    DockerCapacityError,
)
from booley.runtime.host_base_acquisition import RuntimeBaseBuildError, acquire, build_timeout
from booley.runtime.image_build_contracts import source_image_build_contracts
from booley.runtime.image_lifecycle import ImageLifecycleError
from booley.runtime.image_provenance import (
    LABEL_BUILD_ORIGIN,
    LABEL_PARENT_ARTIFACT,
    LABEL_PARENT_ARTIFACT_KIND,
    LABEL_PAYLOAD_FINGERPRINT,
    LABEL_RECIPE_FINGERPRINT,
    LABEL_RUNTIME_BASE_CONTRACT,
    LABEL_SCHEMA,
    LABEL_WHEEL_SOURCE_FINGERPRINT,
    PARENT_ARTIFACT_LOCAL_IMAGE_ID,
    PROVENANCE_SCHEMA,
    resolve_recipe_fingerprint,
)
from booley.runtime.paths import docker_data_dir
from booley.runtime.timefmt import format_duration, utc_now_rfc3339

DOCKER_IMAGE = "booley-sandbox"
GHCR_IMAGE = "ghcr.io/boldaxolotl/booley-sandbox"
LOCAL_RUNTIME_BASE_IMAGE = "booley-runtime-base:local"

# Booley-SHIPPED sandbox flavors: purpose-built images a project selects by name
# via ``[sandbox].image``, each ``FROM booley-sandbox`` plus a domain toolchain.
# They are Booley's images, not the user's: this registry maps each tag to the
# Dockerfile shipped beside this module in ``booley/data/docker/``. Doctor
# reads it to recognise a shipped flavor; the image lifecycle
# (``booley.harness.image_lifecycle``) builds and refreshes them.
FLAVOR_IMAGES = {"booley-sandbox-riscv": "Dockerfile.riscv"}

# Docker image label carrying a content hash of the sources baked into the
# sandbox image. ``booley init`` stamps it at build time and compares it on a
# re-run so an image built from now-stale source is rebuilt instead of skipped
# (the failure that hid the container-side skill deployment from a stale image).
LABEL_FINGERPRINT = "booley.build-fingerprint"
LABEL_VERSION = "org.opencontainers.image.version"
DEFAULT_IMAGE_PULL_TIMEOUT_S = 7200
DEFAULT_IMAGE_TAG_TIMEOUT_S = 30
_WHEEL_GLOB = "booley_rtl-*.whl"


def _source_version(booley_root: Path) -> str | None:
    """Return the checkout's authoritative ``VERSION``, when available."""
    version_file = booley_root / "VERSION"
    try:
        value = version_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _expected_version(booley_root: Path) -> str:
    """Version the image built from *booley_root* must advertise."""
    return _source_version(booley_root) or _read_version()


def _image_build_metadata_args(booley_root: Path) -> list[str]:
    """Docker build args that make image provenance visible in-container."""
    built_at = utc_now_rfc3339()
    is_checkout = (booley_root / ".git").exists()
    values = {
        "BOOLEY_IMAGE_BUILT_AT": built_at,
        "BOOLEY_PAYLOAD_FINGERPRINT": (
            resolve_payload_fingerprint(booley_root) or embedded_payload_fingerprint() or "unknown"
        ),
        "BOOLEY_SOURCE_REVISION": (
            resolve_build_commit(booley_root) if is_checkout else "unknown"
        ),
        "BOOLEY_SOURCE_UPDATED_AT": (
            resolve_source_updated_at(booley_root) if is_checkout else "unknown"
        ),
        "BOOLEY_VERSION": _expected_version(booley_root),
    }
    return [
        item
        for name, value in values.items()
        for item in ("--build-arg", f"{name}={value or 'unknown'}")
    ]


def _docker_image_exists(image: str = DOCKER_IMAGE) -> bool:
    """Compatibility entry point for the shared, observation-only image probe."""
    from booley.runtime.interactive_docker import image_exists

    return image_exists(image)


# --- Image staleness guard (build-fingerprint label) -----------------------
#
# The sandbox image bakes a wheel built from ``src/booley`` plus the bwave Rust
# binary. When that source changes but the image is not rebuilt, the container
# silently runs old code. ``booley init`` normally *skips* the build when the
# image already exists, so drift went unnoticed. We fingerprint the baked-in
# sources, stamp it as an image label at build time, and rebuild on mismatch.


@dataclass(frozen=True)
class _DockerBuildSpec:
    """Inputs that vary between sandbox, runtime-base, and image-lifecycle builds."""

    dockerfile: Path
    context: Path
    exists: bool
    fingerprint: str | None = None
    image: str = DOCKER_IMAGE
    build_note: str = "this can take 20-30 minutes on first build"
    build_contexts: tuple[tuple[str, str], ...] = ()
    build_args: tuple[str, ...] = ()
    parent_artifact: str | None = None
    labels: tuple[tuple[str, str], ...] = ()
    capacity_request: DockerBuildRequest | None = None
    capacity_plan: DockerBuildPlan | None = None
    network: str | None = None


def _iter_fingerprint_files(booley_root: Path):
    """Compatibility view of files contributing to the sandbox image build."""
    yield from iter_payload_files(booley_root)


def _image_build_fingerprint(booley_root: Path) -> str | None:
    """SHA-256 over every source baked into the sandbox image.

    Returns ``None`` when the source tree is absent (e.g. a pip-installed Booley
    with no checkout); that disables the staleness check so the pull/pre-built
    flow is left untouched. Path-then-content is hashed so a rename or deletion
    changes the digest, not just an edit.
    """
    return resolve_payload_fingerprint(booley_root) or embedded_payload_fingerprint()


def _image_label(image: str, label: str) -> str | None:
    """Return *image*'s *label* value, or ``None`` if absent/unavailable."""
    try:
        result = subprocess.run(
            [
                "docker",
                "image",
                "inspect",
                "-f",
                f'{{{{ index .Config.Labels "{label}" }}}}',
                image,
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    val = result.stdout.strip()
    # Go's template prints "<no value>" when the label key is missing.
    return val if val and val != "<no value>" else None


def _installed_image_version() -> str | None:
    """Return release provenance for a pulled or locally built image."""
    fingerprint = _image_label(DOCKER_IMAGE, LABEL_FINGERPRINT)
    if fingerprint and fingerprint.startswith("pulled:"):
        version = fingerprint.removeprefix("pulled:").strip()
        if version:
            return version
    return _image_label(DOCKER_IMAGE, LABEL_VERSION)


def _image_is_stale(
    fingerprint: str | None,
    expected_version: str | None = None,
) -> bool:
    """Whether the present sandbox image no longer matches the local source.

    - ``fingerprint is None`` (no source tree to compare): no source-staleness
      verdict. Init checks release compatibility separately before calling here.
    - no fingerprint label: an image built before this guard existed (the exact
      stale-image bug this fixes) -> stale.
    - ``pulled:*`` label: current only when it names *expected_version*.
    - otherwise: stale iff the stamped hash differs from the current source.
    """
    if fingerprint is None:
        return False
    label = _image_label(DOCKER_IMAGE, LABEL_FINGERPRINT)
    if label is None:
        return True
    if label.startswith("pulled:"):
        return bool(expected_version and label != f"pulled:{expected_version}")
    return label != fingerprint or bool(
        expected_version and _image_label(DOCKER_IMAGE, LABEL_VERSION) != expected_version
    )


def source_fingerprint_mismatch(image: str) -> bool | None:
    """Exact staleness verdict for *image* from its build-fingerprint label.

    Unlike :func:`_image_is_stale` (init's rebuild decision, where a missing
    label means "predates the guard -> rebuild"), this is an *advisory* probe
    for session start and doctor: it answers only when both sides of the
    comparison exist, so a hand-authored image without the label is never
    nagged about.

    - ``None``: no verdict — pip-installed Booley (no checkout to hash), or the
      image is unlabeled / deliberately ``pulled:*``.
    - ``True``: the image bakes sources that no longer match this checkout.
    - ``False``: up to date.

    Derived project images (``FROM booley-sandbox``) inherit the base's label,
    so a project image built from a since-rebuilt base reads as stale here —
    exactly the drift that used to surface only as mysterious in-container
    behavior.
    """
    booley_root = docker_data_dir().parent.parent.parent.parent
    wheel_source = resolve_wheel_source_fingerprint(booley_root)
    wheel_label = _image_label(image, LABEL_WHEEL_SOURCE_FINGERPRINT)
    if (
        wheel_source is not None
        and wheel_label is not None
        and len(wheel_label) == 64
        and all(character in "0123456789abcdef" for character in wheel_label)
    ):
        return wheel_label != wheel_source
    fingerprint = _image_build_fingerprint(booley_root)
    if fingerprint is None:
        return None
    label = _image_label(image, LABEL_FINGERPRINT)
    if label is None or label.startswith("pulled:"):
        return None
    if label != fingerprint:
        return True
    expected_version = _source_version(booley_root)
    return bool(expected_version and _image_label(image, LABEL_VERSION) != expected_version)


def _read_version() -> str:
    try:
        from booley import __version__

        return __version__
    except Exception:  # noqa: BLE001 — version import is optional; fall back to a dev placeholder
        return "0.0.0-dev"


def remote_tag(image: str, version: str) -> str:
    """The GHCR tag *image* is published under. Public: init prints it as a hint."""
    registry = GHCR_IMAGE.rsplit("/", 1)[0]
    return f"{registry}/{image}:{version}"


def _image_pull_timeout_seconds() -> int:
    """Return the bounded registry-pull deadline, accepting a host override."""
    raw = os.environ.get("BOOLEY_IMAGE_PULL_TIMEOUT", str(DEFAULT_IMAGE_PULL_TIMEOUT_S))
    try:
        timeout = int(raw)
    except ValueError:
        warn(
            f"invalid BOOLEY_IMAGE_PULL_TIMEOUT={raw!r}; "
            f"using {DEFAULT_IMAGE_PULL_TIMEOUT_S} seconds"
        )
        return DEFAULT_IMAGE_PULL_TIMEOUT_S
    if timeout <= 0:
        warn(
            f"invalid BOOLEY_IMAGE_PULL_TIMEOUT={raw!r}; "
            f"using {DEFAULT_IMAGE_PULL_TIMEOUT_S} seconds"
        )
        return DEFAULT_IMAGE_PULL_TIMEOUT_S
    return timeout


def _try_pull_image(
    version: str,
    image: str = DOCKER_IMAGE,
    *,
    adopt: bool = True,
) -> bool:
    tag = remote_tag(image, version)
    info(f"trying to pull pre-built image: {tag}")
    timeout = _image_pull_timeout_seconds()
    try:
        result = subprocess.run(
            ["docker", "pull", tag],
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        warn(
            f"pre-built image pull timed out after {timeout} seconds; "
            "override with BOOLEY_IMAGE_PULL_TIMEOUT (seconds)"
        )
        return False
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        warn(f"pre-built image pull failed for {tag}: {exc}")
        return False
    if result.returncode != 0:
        warn(f"pre-built image pull failed for {tag} (docker exited {result.returncode})")
        return False

    if not adopt:
        return True

    return _tag_pulled_image(tag, image)


def _tag_pulled_image(tag: str, image: str) -> bool:
    """Adopt one already-pulled acquisition tag under its managed name."""

    try:
        subprocess.run(
            ["docker", "tag", tag, image],
            capture_output=True,
            timeout=DEFAULT_IMAGE_TAG_TIMEOUT_S,
            check=True,
        )
    except subprocess.TimeoutExpired:
        warn(
            f"could not tag pulled image {tag} as {image}: "
            f"docker tag timed out after {DEFAULT_IMAGE_TAG_TIMEOUT_S} seconds"
        )
        return False
    except (subprocess.SubprocessError, FileNotFoundError) as exc:
        warn(f"could not tag pulled image {tag} as {image}: {exc}")
        return False

    # Provenance belongs to the published artifact. Do not wrap a pulled image
    # merely to record acquisition history: doing so changes its immutable ID
    # and conflates "pulled" with evidence that its payload is current.
    return True


def _report_incompatible_image(
    ctx: InitContext,
    installed_version: str | None,
    expected_version: str,
) -> None:
    """Warn that the base image could not be refreshed to the required release."""
    found = f"v{installed_version}" if installed_version else "of unknown version"
    warn(
        f"{DOCKER_IMAGE} is {found}, but Booley v{expected_version} requires sandbox image "
        f"v{expected_version}; the existing image was left unchanged and may be incompatible"
    )
    info(f"  retry: docker pull {remote_tag(DOCKER_IMAGE, expected_version)}")
    ctx.record("docker_image", "warn", "compatible image pull failed")


def _refresh_installed_base_image(ctx: InitContext, expected_version: str) -> bool:
    """Refresh a release image when no checkout sources exist; True when handled."""
    installed_version = _installed_image_version()
    if installed_version == expected_version:
        return False
    if ctx.check_only:
        found = f"v{installed_version}" if installed_version else "an unknown version"
        warn(f"{DOCKER_IMAGE} is {found}; would pull the Booley v{expected_version} image")
        ctx.record("docker_image", "warn", "would pull compatible image")
        return True
    if _try_pull_image(expected_version):
        ok(f"{DOCKER_IMAGE} pulled from registry (v{expected_version})")
        ctx.record("docker_image", "ok", "pulled")
        return True
    _report_incompatible_image(ctx, installed_version, expected_version)
    return True


def _prepare_existing_base_image(
    ctx: InitContext,
    docker_dir: Path,
    *,
    exists: bool,
    fingerprint: str | None,
    expected_version: str,
    allow_pull: bool = True,
    capacity_plan: DockerBuildPlan | None = None,
) -> bool:
    """Skip or refresh a present base image; False when normal provisioning remains."""
    if not exists or ctx.force:
        return False
    if fingerprint is None and allow_pull and _refresh_installed_base_image(ctx, expected_version):
        return True
    if fingerprint is None or not _image_is_stale(fingerprint, expected_version=expected_version):
        current(f"{DOCKER_IMAGE} image already present")
        _report_build_cache()
        ctx.record("docker_image", "current", "already present")
        return True
    warn(f"{DOCKER_IMAGE} image is stale (source changed since build) — rebuilding")
    warn("a dev-install source/fingerprint change rebuilds the Sandbox Image locally")
    if ctx.check_only:
        ctx.record("docker_image", "warn", "would rebuild (stale)")
        return True
    _docker_local_build(
        ctx,
        docker_dir,
        exists,
        fingerprint,
        capacity_plan=capacity_plan,
    )
    return True


def _step_docker_image(
    ctx: InitContext,
    *,
    allow_pull: bool = True,
    rebuild_runtime_base: bool | None = None,
    capacity_plan: DockerBuildPlan | None = None,
) -> None:
    """Build or refresh ``booley-sandbox``."""
    ctx.step_banner("Docker image")
    if not _docker_cli_ready(ctx):
        return

    exists = _docker_image_exists()
    docker_dir = docker_data_dir()
    booley_root = docker_dir.parent.parent.parent.parent
    fingerprint = _image_build_fingerprint(booley_root)
    expected_version = _expected_version(booley_root)

    if _prepare_existing_base_image(
        ctx,
        docker_dir,
        exists=exists,
        fingerprint=fingerprint,
        expected_version=expected_version,
        allow_pull=allow_pull,
        capacity_plan=capacity_plan,
    ):
        return

    if ctx.check_only:
        _docker_check_only(ctx, exists)
        return

    if _pulled_base_image(ctx, allow_pull=allow_pull, expected_version=expected_version):
        return

    _docker_local_build(
        ctx,
        docker_dir,
        exists,
        fingerprint,
        rebuild_runtime_base=(ctx.force if rebuild_runtime_base is None else rebuild_runtime_base),
        capacity_plan=capacity_plan,
    )


def _pulled_base_image(ctx: InitContext, *, allow_pull: bool, expected_version: str) -> bool:
    """Try the non-forced registry path and report whether it completed the step."""
    if not allow_pull or ctx.force:
        return False
    if _try_pull_image(expected_version):
        ok(f"{DOCKER_IMAGE} pulled from registry (v{expected_version})")
        ctx.record("docker_image", "ok", "pulled")
        return True
    info("pre-built image unavailable, rebuilding the Sandbox Image locally")
    return False


def _docker_cli_ready(ctx: InitContext) -> bool:
    if shutil.which("docker"):
        return True
    err("docker not found on PATH — cannot build image")
    info(
        "  Docker Desktop users: the CLI joins PATH only after the app has "
        "started — launch Docker Desktop, then reopen this terminal"
    )
    ctx.record("docker_image", "err", "docker not on PATH")
    return False


def _docker_check_only(ctx: InitContext, exists: bool) -> None:
    """Handle --check-only mode for Docker image step."""
    if exists:
        current(f"{DOCKER_IMAGE} image already present")
        ctx.record("docker_image", "current", "already present")
    else:
        warn(f"{DOCKER_IMAGE} image missing (would build)")
        ctx.record("docker_image", "warn", "would build")


def _local_build_inputs(ctx: InitContext, docker_dir: Path) -> tuple[Path, Path, Path] | None:
    """Validate and return candidate Dockerfile, base Dockerfile, and Booley repository root."""
    dockerfile = docker_dir / "Dockerfile"
    base_dockerfile = docker_dir / "Dockerfile.base"
    missing_dockerfile = next(
        (path for path in (base_dockerfile, dockerfile) if not path.is_file()), None
    )
    if missing_dockerfile:
        err(f"Dockerfile not found at {missing_dockerfile}")
        ctx.record("docker_image", "err", "Dockerfile missing")
        return None

    booley_root = docker_dir.parent.parent.parent.parent
    if not (booley_root / "pyproject.toml").is_file():
        err("cannot determine Booley repository root for docker build context")
        info("  build manually: ./src/booley/data/docker/build.sh")
        ctx.record("docker_image", "err", "Booley repository root not found")
        return None
    return dockerfile, base_dockerfile, booley_root


def _docker_local_build(
    ctx: InitContext,
    docker_dir: Path,
    exists: bool,
    fingerprint: str | None = None,
    *,
    preserve_build_stamp: bool = False,
    rebuild_runtime_base: bool = False,
    capacity_plan: DockerBuildPlan | None = None,
) -> None:
    """Build the runtime base, wheel, and candidate image from local sources."""
    inputs = _local_build_inputs(ctx, docker_dir)
    if inputs is None:
        return
    dockerfile, base_dockerfile, booley_root = inputs
    if fingerprint is None:
        fingerprint = _image_build_fingerprint(booley_root)
    try:
        capacity_plan = _resolved_local_capacity_plan(
            base_dockerfile,
            dockerfile,
            booley_root,
            fingerprint,
            rebuild_runtime_base=rebuild_runtime_base,
            supplied=capacity_plan,
        )
    except (OSError, ValueError) as error:
        err(f"stable runtime-base contract failed: {error}")
        ctx.record("docker_image", "err", "runtime-base contract failed")
        return
    except ImageLifecycleError as error:
        err(f"Sandbox Image capacity check failed: {error}")
        ctx.record("docker_image", "err", f"Sandbox Image capacity check failed: {error}")
        return
    runtime_base_id = _acquire_runtime_base(
        ctx,
        booley_root,
        rebuild=rebuild_runtime_base,
        capacity_plan=capacity_plan,
    )
    if runtime_base_id is None:
        return
    _complete_local_sandbox_build(
        ctx,
        dockerfile,
        booley_root,
        exists=exists,
        fingerprint=fingerprint,
        runtime_base_id=runtime_base_id,
        capacity_plan=capacity_plan.tail_from_output(DOCKER_IMAGE),
        preserve_build_stamp=preserve_build_stamp,
    )


def _complete_local_sandbox_build(
    ctx: InitContext,
    dockerfile: Path,
    booley_root: Path,
    *,
    exists: bool,
    fingerprint: str | None,
    runtime_base_id: str,
    capacity_plan: DockerBuildPlan,
    preserve_build_stamp: bool,
) -> None:
    """Build the wheel and final Sandbox candidate after the base is ready."""
    if not _docker_build_wheel(ctx, booley_root, preserve_stamp=preserve_build_stamp):
        return
    returncode = _build_local_sandbox_candidate(
        ctx,
        dockerfile,
        booley_root,
        exists=exists,
        fingerprint=fingerprint,
        runtime_base_id=runtime_base_id,
        capacity_plan=capacity_plan,
    )
    if returncode is None:
        return  # error already recorded

    if returncode != 0:
        err("docker build failed — re-run with -v for full output")
        ctx.record("docker_image", "err", "build failed")
        return

    ok(f"{DOCKER_IMAGE} image built successfully")
    _report_build_cache()
    ctx.record("docker_image", "ok", "built")


def _resolved_local_capacity_plan(
    base_dockerfile: Path,
    dockerfile: Path,
    booley_root: Path,
    fingerprint: str | None,
    *,
    rebuild_runtime_base: bool,
    supplied: DockerBuildPlan | None,
) -> DockerBuildPlan:
    """Replan the local prefix while retaining any later command-level builds."""
    local_requests = _local_build_capacity_requests(
        base_dockerfile,
        dockerfile,
        booley_root,
        fingerprint,
        rebuild_runtime_base=rebuild_runtime_base,
    )
    if supplied is None:
        return DockerBuildPlan(local_requests)
    local_outputs = {LOCAL_RUNTIME_BASE_IMAGE, DOCKER_IMAGE}
    future = tuple(
        request for request in supplied.requests if request.output_tag not in local_outputs
    )
    return DockerBuildPlan((*local_requests, *future))


def _build_local_sandbox_candidate(
    ctx: InitContext,
    dockerfile: Path,
    booley_root: Path,
    *,
    exists: bool,
    fingerprint: str | None,
    runtime_base_id: str,
    capacity_plan: DockerBuildPlan | None = None,
) -> int | None:
    try:
        base_before = idk.image_id(LOCAL_RUNTIME_BASE_IMAGE)
    except idk.ImageInspectError as exc:
        err(f"cannot verify the stable runtime-base tag before the Sandbox Image build: {exc}")
        ctx.record("docker_image", "err", str(exc))
        return None
    if base_before != runtime_base_id:
        err("stable runtime-base tag changed before the Sandbox Image build")
        ctx.record("docker_image", "err", "runtime-base identity changed")
        return None
    build = _DockerBuildSpec(
        dockerfile=dockerfile,
        context=booley_root,
        exists=exists,
        fingerprint=fingerprint,
        build_contexts=(("booley-runtime-base", f"docker-image://{LOCAL_RUNTIME_BASE_IMAGE}"),),
        build_args=("--build-arg", f"BOOLEY_RUNTIME_BASE_IMAGE={runtime_base_id}"),
        parent_artifact=runtime_base_id,
        capacity_request=capacity_plan.current if capacity_plan is not None else None,
        capacity_plan=capacity_plan,
    )
    returncode = _docker_build_image(ctx, build)
    if returncode != 0:
        return returncode
    try:
        base_after = idk.image_id(LOCAL_RUNTIME_BASE_IMAGE)
    except idk.ImageInspectError as exc:
        err(f"cannot verify the stable runtime-base tag after the Sandbox Image build: {exc}")
        ctx.record("docker_image", "err", str(exc))
        return None
    if base_after != runtime_base_id:
        err("stable runtime-base tag changed during the Sandbox Image build")
        ctx.record("docker_image", "err", "runtime-base identity changed")
        return None
    return returncode


def _short_contract(contract: str) -> str:
    return contract[:12]


def _acquire_runtime_base(
    ctx: InitContext,
    booley_root: Path,
    *,
    rebuild: bool,
    capacity_plan: DockerBuildPlan | None = None,
) -> str | None:
    """Return one compatible immutable runtime-base ID, building when required."""
    try:
        return acquire(
            booley_root, rebuild=rebuild, capacity_plan=capacity_plan, verbose=ctx.verbose
        )
    except RuntimeBaseBuildError as exc:
        _report_runtime_base_build_failure(ctx, exc)
        return None
    except (OSError, ValueError, RuntimeError) as exc:
        err(f"runtime-base acquisition failed: {exc}")
        ctx.record("runtime_base", "err", str(exc))
        return None


def _report_runtime_base_build_failure(ctx: InitContext, failure: RuntimeBaseBuildError) -> None:
    """Report a failed runtime-base build the way every Docker image build reports it."""
    result = failure.result
    if result is None:
        cause = failure.__cause__
        err(f"docker build failed: {cause}")
        ctx.record("docker_image", "err", str(cause))
        return
    _render_build_diagnostics(result)
    if result.timed_out:
        err(
            f"docker build timed out after {format_duration(build_timeout())} "
            "(override with BOOLEY_IMAGE_BUILD_TIMEOUT)"
        )
        ctx.record("docker_image", "err", "build timed out")
        return
    err("stable runtime-base build failed — re-run with -v for full output")
    ctx.record("docker_image", "err", "runtime-base build failed")


def _local_build_capacity_requests(
    base_dockerfile: Path,
    dockerfile: Path,
    booley_root: Path,
    fingerprint: str | None,
    *,
    rebuild_runtime_base: bool,
) -> tuple[DockerBuildRequest, ...]:
    """Resolve the exact legacy local-build sequence before Docker mutation."""
    contract = source_image_build_contracts(booley_root).runtime_base
    try:
        runtime_id = idk.image_id(LOCAL_RUNTIME_BASE_IMAGE)
    except idk.ImageInspectError as exc:
        raise ImageLifecycleError(str(exc)) from exc
    runtime_current = (
        runtime_id is not None
        and _image_label(runtime_id, LABEL_RUNTIME_BASE_CONTRACT) == contract
        and not rebuild_runtime_base
    )
    requests: list[DockerBuildRequest] = []
    if not runtime_current:
        requests.append(
            DockerBuildRequest(
                "runtime base",
                LOCAL_RUNTIME_BASE_IMAGE,
                BuildEstimateClass.HEAVYWEIGHT,
                DockerCacheEvidence(
                    LOCAL_RUNTIME_BASE_IMAGE,
                    (
                        (LABEL_RUNTIME_BASE_CONTRACT, contract),
                        (LABEL_RECIPE_FINGERPRINT, resolve_recipe_fingerprint((base_dockerfile,))),
                    ),
                ),
            )
        )
    sandbox_labels = (
        (LABEL_FINGERPRINT, fingerprint or ""),
        (LABEL_RECIPE_FINGERPRINT, resolve_recipe_fingerprint((dockerfile,))),
    )
    requests.append(
        DockerBuildRequest(
            "Sandbox Image",
            DOCKER_IMAGE,
            BuildEstimateClass.HEAVYWEIGHT,
            DockerCacheEvidence(DOCKER_IMAGE, sandbox_labels),
        )
    )
    return tuple(requests)


def _size_to_gb(size: str) -> float:
    """Parse a docker size string ("29.3GB", "512MB", "1.2kB") into GB (base-10)."""
    m = re.match(r"\s*([0-9.]+)\s*([kKmMgGtT]?)i?B", size)
    if not m:
        return 0.0
    to_gb = {"": 1e-9, "k": 1e-6, "m": 1e-3, "g": 1.0, "t": 1e3}
    return float(m.group(1)) * to_gb.get(m.group(2).lower(), 0.0)


def _report_build_cache(prune_hint_gb: float = 10.0) -> None:
    """Report the docker build-cache size after a build so it can't balloon silently.

    A first sandbox build leaves a large builder cache — Ibex onboarding saw
    ~29 GB, which filled the disk and killed a running synth (SETUP-24). init
    does not prune automatically (the cache may be shared with other projects/
    images), but it surfaces the size and hints at ``docker builder prune`` once
    it grows past a threshold so the growth is visible rather than a surprise.
    """
    try:
        result = subprocess.run(
            ["docker", "system", "df", "--format", "{{.Type}}\t{{.Size}}\t{{.Reclaimable}}"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return
    if result.returncode != 0:
        return
    for line in result.stdout.splitlines():
        parts = [p.strip() for p in line.split("\t")]
        if len(parts) >= 2 and parts[0].lower() == "build cache":
            reclaimable = f" ({parts[2]} reclaimable)" if len(parts) >= 3 and parts[2] else ""
            info(f"docker build cache: {parts[1]}{reclaimable}")
            if _size_to_gb(parts[1]) >= prune_hint_gb:
                info(
                    "  large — docker builder prune can reclaim unused cache, but may "
                    "evict layers a retry would otherwise reuse"
                )
            return


def _wheel_build_stamp(booley_root: Path, *, preserve_stamp: bool):
    if not preserve_stamp:
        return runtime_image_build_stamp(booley_root)
    stamp_file = booley_root / "src" / "booley" / "_build_commit.py"
    if not stamp_file.is_file():
        raise OSError("verified development build stamp is missing")
    try:
        constants = stamp_constants(stamp_file.read_text(encoding="utf-8"), {"COMMIT"})
    except ValueError as exc:
        raise OSError("verified development build stamp is invalid") from exc
    commit = constants.get("COMMIT", "")
    if not isinstance(commit, str) or not commit:
        raise OSError("verified development build commit is missing")
    return contextlib.nullcontext(commit)


def _docker_build_wheel(
    ctx: InitContext, booley_root: Path, *, preserve_stamp: bool = False
) -> bool:
    """Build the provenance-stamped Booley wheel into ``dist/``."""
    dist_dir = booley_root / "dist"
    build_dir = booley_root / "build"
    info("building booley wheel...")
    try:
        with _wheel_build_stamp(booley_root, preserve_stamp=preserve_stamp) as commit:
            info(f"  build commit: {commit or '<unknown — not a git checkout>'}")
            # setuptools incrementally reuses build/lib. Package moves otherwise
            # leave deleted modules in the next wheel after a package move.
            shutil.rmtree(build_dir, ignore_errors=True)
            # Docker COPY and pip both expand this glob. A wheel from an older
            # Booley version would therefore make pip resolve two explicit
            # versions of the same package and fail with ResolutionImpossible.
            for wheel in dist_dir.glob(_WHEEL_GLOB):
                wheel.unlink()
            # -P keeps the repo-root ``build/`` setuptools artifact dir from
            # shadowing the pypa ``build`` module (cwd is prepended to sys.path
            # for ``-m`` otherwise, and the failure mode is a stale wheel being
            # silently reused by the docker COPY layer on the next image build).
            subprocess.run(
                [sys.executable, "-P", "-m", "build", "--wheel", "--outdir", str(dist_dir)],
                cwd=str(booley_root),
                check=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=not ctx.verbose,
                timeout=120,
            )
        wheels = sorted(dist_dir.glob(_WHEEL_GLOB))
        if len(wheels) != 1:
            names = ", ".join(wheel.name for wheel in wheels) or "none"
            return _report_wheel_failure(
                ctx,
                RuntimeError(
                    f"wheel build produced {len(wheels)} matching wheels ({names}); "
                    "expected exactly one"
                ),
            )
        if commit and wheel_embedded_commit(wheels[0]) != commit:
            return _report_wheel_failure(
                ctx,
                RuntimeError("built wheel commit differs from the expected nonempty build stamp"),
            )
        return True
    except (subprocess.SubprocessError, OSError, ValueError) as e:
        return _report_wheel_failure(ctx, e)


def _report_wheel_failure(ctx: InitContext, exc: Exception) -> bool:
    """Explain a failed wheel build, record it, and return False."""
    err(f"wheel build failed: {exc}")
    # A bare exit status is undebuggable (F-4) — surface the EDA tool's own
    # words. BOTH streams: pypa ``build`` writes its progress log *and* the
    # actual diagnosis to STDOUT and leaves stderr empty, so a stderr-only
    # report showed one useless "Creating isolated environment" line and hid
    # the real cause (fpu F-1). The common failures get their exact fix named.
    stdout = (getattr(exc, "stdout", "") or "").strip()
    stderr = (getattr(exc, "stderr", "") or "").strip()
    combined = "\n".join(part for part in (stdout, stderr) if part)
    for line in combined.splitlines()[-12:]:
        info(f"  {line}")
    if "No module named build" in combined or isinstance(exc, FileNotFoundError):
        hint("pip install build   (or: pip install -e '.[dev]')")
    elif "ensurepip is not available" in combined:
        # Debian/Ubuntu split venv out of the interpreter package; ``build``
        # needs it to make its isolated environment. The version must match the
        # interpreter running init, not the distro default (docs/user/TROUBLESHOOTING.md).
        pyver = f"{sys.version_info.major}.{sys.version_info.minor}"
        hint(f"sudo apt install python{pyver}-venv   (matches this interpreter)")
        hint("or: pip install build && python -m build --wheel --no-isolation")
    ctx.record("docker_image", "err", "wheel build failed")
    return False


def _local_parent_label_args(parent_artifact: str) -> list[str]:
    """Return Docker label arguments for exact local-image ancestry."""
    return [
        "--label",
        f"{LABEL_PARENT_ARTIFACT_KIND}={PARENT_ARTIFACT_LOCAL_IMAGE_ID}",
        "--label",
        f"{LABEL_PARENT_ARTIFACT}={parent_artifact}",
    ]


def _docker_build_command(spec: _DockerBuildSpec) -> list[str]:
    """Translate a build specification into the Docker CLI command."""
    build_cmd = ["docker", "build"]
    if spec.network is not None:
        if spec.network != "none":
            raise ValueError("unsupported layout build network")
        build_cmd += ["--network", "none", "--pull=false"]
    # ``ctx.force`` means "run the local build even if the image fingerprint is
    # current"; it must not mean "discard Docker's layer cache".  The current
    # wheel COPY invalidates Booley's own layers, while changed Dockerfile inputs
    # invalidate their normal descendants.  Keeping earlier EDA-tool layers is
    # what makes an explicit session refresh practical instead of another cold
    # 20-60 minute build.
    if spec.fingerprint:
        build_cmd += ["--label", f"{LABEL_FINGERPRINT}={spec.fingerprint}"]
        build_cmd += [
            "--label",
            f"{LABEL_SCHEMA}={PROVENANCE_SCHEMA}",
            "--label",
            f"{LABEL_PAYLOAD_FINGERPRINT}={spec.fingerprint}",
            "--label",
            f"{LABEL_RECIPE_FINGERPRINT}={resolve_recipe_fingerprint((spec.dockerfile,))}",
            "--label",
            f"{LABEL_BUILD_ORIGIN}=local",
        ]
    if spec.parent_artifact:
        build_cmd += _local_parent_label_args(spec.parent_artifact)
    for name, value in spec.labels:
        build_cmd += ["--label", f"{name}={value}"]
    if spec.image == DOCKER_IMAGE:
        build_cmd += _image_build_metadata_args(spec.context)
    build_cmd += spec.build_args
    for name, source in sorted(spec.build_contexts):
        build_cmd += ["--build-context", f"{name}={source}"]
    build_cmd += ["-t", spec.image, "-f", str(spec.dockerfile), str(spec.context)]
    return build_cmd


def _render_build_diagnostics(result: DockerBuildResult) -> None:
    if not result.diagnostics:
        return
    info("recent Docker output:")
    for line in result.diagnostics:
        info(f"  {line}")


def _docker_build_image(ctx: InitContext, spec: _DockerBuildSpec) -> int | None:
    """Run one specified image build, recording infrastructure failures."""
    action = "Rebuilding" if spec.exists else "Building"
    info(f"{action} {spec.image} image — {spec.build_note}.")
    timeout_seconds = build_timeout()
    try:
        result = run_docker_build(
            _docker_build_command(spec),
            image=spec.image,
            verbose=ctx.verbose,
            timeout=timeout_seconds,
            current_request=spec.capacity_request,
            remaining_plan=spec.capacity_plan,
        )
        _render_build_diagnostics(result)
        if result.timed_out:
            err(
                f"docker build timed out after {format_duration(timeout_seconds)} "
                "(override with BOOLEY_IMAGE_BUILD_TIMEOUT)"
            )
            ctx.record("docker_image", "err", "build timed out")
            return None
        return result.returncode
    except DockerCapacityError:
        raise
    except subprocess.TimeoutExpired:
        err(
            f"docker build timed out after {format_duration(timeout_seconds)} "
            "(override with BOOLEY_IMAGE_BUILD_TIMEOUT)"
        )
        ctx.record("docker_image", "err", "build timed out")
        return None
    except (FileNotFoundError, OSError) as e:
        err(f"docker build failed: {e}")
        ctx.record("docker_image", "err", str(e))
        return None
