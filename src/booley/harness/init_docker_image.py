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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from booley.harness.build_stamp import (
    build_stamp,
    embedded_payload_fingerprint,
    iter_payload_files,
    resolve_build_commit,
    resolve_payload_fingerprint,
    resolve_source_updated_at,
)
from booley.harness.docker_base_contract import contract as runtime_base_contract
from booley.harness.init_common import InitContext, err, info, ok, skip, warn
from booley.runtime.docker_build import DockerBuildResult, run_docker_build
from booley.runtime.image_provenance import (
    LABEL_BUILD_ORIGIN,
    LABEL_PARENT_ARTIFACT,
    LABEL_PARENT_ARTIFACT_KIND,
    LABEL_PAYLOAD_FINGERPRINT,
    LABEL_RECIPE_FINGERPRINT,
    LABEL_SCHEMA,
    PARENT_ARTIFACT_LOCAL_IMAGE_ID,
    PROVENANCE_SCHEMA,
    resolve_recipe_fingerprint,
)
from booley.runtime.paths import docker_data_dir
from booley.runtime.timefmt import utc_now_rfc3339

DOCKER_IMAGE = "booley-sandbox"
GHCR_IMAGE = "ghcr.io/boldaxolotl/booley-sandbox"
LOCAL_RUNTIME_BASE_IMAGE = "booley-runtime-base:local"

# Booley-SHIPPED sandbox flavors: purpose-built images a project selects by name
# via ``[sandbox].image``, each ``FROM booley-sandbox`` plus a domain toolchain.
# They are Booley's images, not the user's, so init owns their lifecycle exactly
# like the base's — mapping the tag to the Dockerfile shipped beside this module
# in ``booley/data/docker/``. Without this registry a flavor fell through
# ``_project_image_setup_gate``'s "not the generated name -> user-managed" branch
# and was skipped, so init would rebuild the base for 20 minutes and leave the
# image the project actually runs frozen on the base's *previous* layers.
FLAVOR_IMAGES = {"booley-sandbox-riscv": "Dockerfile.riscv"}

# Docker image label carrying a content hash of the sources baked into the
# sandbox image. ``booley init`` stamps it at build time and compares it on a
# re-run so an image built from now-stale source is rebuilt instead of skipped
# (the failure that hid the container-side skill deployment from a stale image).
LABEL_FINGERPRINT = "booley.build-fingerprint"
LABEL_BASE_IMAGE_ID = "booley.base-image-id"
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


def _runtime_base_build_metadata_args(booley_root: Path) -> list[str]:
    """Docker build args for explicit stable-base provenance and compatibility."""
    is_checkout = (booley_root / ".git").exists()
    values = {
        "BOOLEY_BASE_SOURCE_REVISION": (
            resolve_build_commit(booley_root) if is_checkout else "unknown"
        ),
        "BOOLEY_BASE_CONTRACT": runtime_base_contract(booley_root),
        "BOOLEY_BASE_BUILT_AT": utc_now_rfc3339(),
    }
    return [
        item
        for name, value in values.items()
        for item in ("--build-arg", f"{name}={value or 'unknown'}")
    ]


def _docker_image_exists(image: str = DOCKER_IMAGE) -> bool:
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError):
        return False


def _docker_image_id(image: str) -> str | None:
    """Return *image*'s immutable Docker ID, or ``None`` when unavailable."""
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and value else None


# --- Image staleness guard (build-fingerprint label) -----------------------
#
# The sandbox image bakes a wheel built from ``src/booley`` plus the bwave Rust
# binary. When that source changes but the image is not rebuilt, the container
# silently runs old code. ``booley init`` normally *skips* the build when the
# image already exists, so drift went unnoticed. We fingerprint the baked-in
# sources, stamp it as an image label at build time, and rebuild on mismatch.


@dataclass(frozen=True)
class _DockerBuildSpec:
    """Inputs that vary between sandbox, runtime-base, and flavor builds."""

    dockerfile: Path
    context: Path
    exists: bool
    fingerprint: str | None = None
    image: str = DOCKER_IMAGE
    record_key: str = "docker_image"
    build_note: str = "this can take 20-30 minutes on first build"
    build_contexts: tuple[tuple[str, str], ...] = ()
    build_args: tuple[str, ...] = ()
    parent_artifact: str | None = None


def _iter_fingerprint_files(booley_root: Path):
    """Yield every source file that contributes to the sandbox image build."""
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


def _installed_image_version(image: str = DOCKER_IMAGE) -> str | None:
    """Return release provenance for a pulled or locally built image."""
    fingerprint = _image_label(image, LABEL_FINGERPRINT)
    if fingerprint and fingerprint.startswith("pulled:"):
        version = fingerprint.removeprefix("pulled:").strip()
        if version:
            return version
    return _image_label(image, LABEL_VERSION)


def _image_is_stale(
    fingerprint: str | None,
    image: str = DOCKER_IMAGE,
    expected_version: str | None = None,
) -> bool:
    """Whether the present sandbox image no longer matches the local source.

    - ``fingerprint is None`` (no source tree to compare): no source-staleness
      verdict. Init checks release compatibility separately before calling here.
    - no fingerprint label: an image built before this guard existed (the exact
      stale-image bug this fixes) -> stale.
    - ``pulled:*`` label: current only when it names *expected_version*.
    - otherwise: stale iff the stamped hash differs from the current source.

    Also asked of a :data:`FLAVOR_IMAGES` flavor, which carries the *base's*
    fingerprint (``build-riscv.sh`` stamps the same label from the same sources).
    That is what makes derived-image drift detectable: the source change that
    restamps the base leaves the flavor's label behind, so it reads as stale.
    """
    if fingerprint is None:
        return False
    label = _image_label(image, LABEL_FINGERPRINT)
    if label is None:
        return True
    if label.startswith("pulled:"):
        return bool(expected_version and label != f"pulled:{expected_version}")
    source_or_version_stale = label != fingerprint or bool(
        expected_version and _image_label(image, LABEL_VERSION) != expected_version
    )
    if source_or_version_stale:
        return True
    if image not in FLAVOR_IMAGES:
        return False
    stamped_base = _image_label(image, LABEL_BASE_IMAGE_ID)
    current_base = _docker_image_id(DOCKER_IMAGE)
    return not stamped_base or not current_base or stamped_base != current_base


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


def _warn_on_distribution_version_drift(booley_root: Path) -> None:
    """Surface stale editable-install metadata before image selection/build."""
    source_version = _source_version(booley_root)
    installed_version = _read_version()
    if source_version and source_version != installed_version:
        warn(
            "installed Booley distribution metadata reports "
            f"{installed_version}, but this checkout's VERSION is {source_version}; "
            "using the checkout version for image provenance. Reinstall the "
            "editable package to make `booley --version` agree."
        )


def _stamp_image_fingerprint(image: str, value: str) -> None:
    """Best-effort: set *image*'s build-fingerprint label to *value*.

    Used to mark a freshly *pulled* image as ``pulled:<version>`` so a later run
    recognises it as intentional and doesn't treat the missing label as stale.
    Implemented as a metadata-only ``FROM <image>`` rebuild (near-instant, no
    new layers). Failure is non-fatal — the image just gets re-checked later.
    """
    with contextlib.suppress(subprocess.SubprocessError, FileNotFoundError):
        subprocess.run(
            ["docker", "build", "-q", "--label", f"{LABEL_FINGERPRINT}={value}", "-t", image, "-"],
            input=f"FROM {image}\n",
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )


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


def _try_pull_image(version: str, image: str = DOCKER_IMAGE) -> bool:
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


def _base_image_note(selected_image: str) -> None:
    """Say why the base is built when the project's own image is something else.

    The Docker-image step always builds ``booley-sandbox``: it is Booley's own
    base image and a
    project that selects another one nearly always layers on top of it, so a
    skip here would just move the staleness one image down. But the step used to
    announce a 20-minute rebuild of an image the project never runs, with no
    word of the relationship — which reads as init building the wrong thing.
    """
    if not selected_image or selected_image == DOCKER_IMAGE:
        return
    if selected_image in FLAVOR_IMAGES:
        info(
            f"{DOCKER_IMAGE} is the base this project's [sandbox].image "
            f"'{selected_image}' layers on — it is built first, that flavor image next"
        )
        return
    info(
        f"this project runs [sandbox].image '{selected_image}'; {DOCKER_IMAGE} is "
        "Booley's base image and is kept current regardless"
    )


def _report_incompatible_image(
    ctx: InitContext,
    image: str,
    installed_version: str | None,
    expected_version: str,
    *,
    record_key: str,
    detail: str,
) -> None:
    """Warn that *image* could not be refreshed to the required release."""
    found = f"v{installed_version}" if installed_version else "of unknown version"
    warn(
        f"{image} is {found}, but Booley v{expected_version} requires sandbox image "
        f"v{expected_version}; the existing image was left unchanged and may be incompatible"
    )
    info(f"  retry: docker pull {remote_tag(image, expected_version)}")
    ctx.record(record_key, "warn", detail)


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
    _report_incompatible_image(
        ctx,
        DOCKER_IMAGE,
        installed_version,
        expected_version,
        record_key="docker_image",
        detail="compatible image pull failed",
    )
    return True


def _prepare_existing_base_image(
    ctx: InitContext,
    docker_dir: Path,
    *,
    exists: bool,
    fingerprint: str | None,
    expected_version: str,
) -> bool:
    """Skip or refresh a present base image; False when normal provisioning remains."""
    if not exists or ctx.force:
        return False
    if fingerprint is None and _refresh_installed_base_image(ctx, expected_version):
        return True
    if fingerprint is None or not _image_is_stale(fingerprint, expected_version=expected_version):
        skip(f"{DOCKER_IMAGE} image already present")
        _report_build_cache()
        ctx.record("docker_image", "skip", "already present")
        return True
    warn(f"{DOCKER_IMAGE} image is stale (source changed since build) — rebuilding")
    warn("a dev-install source/fingerprint change forces a full image rebuild (~20 min)")
    if ctx.check_only:
        ctx.record("docker_image", "warn", "would rebuild (stale)")
        return True
    _docker_local_build(ctx, docker_dir, exists, fingerprint)
    return True


def _step_docker_image(ctx: InitContext, selected_image: str = "") -> None:
    """Build/refresh the project-agnostic ``booley-sandbox`` base image.

    *selected_image* is the project's resolved ``[sandbox].image`` and is used
    only to explain this step's relationship to it; the base is built either way.
    """
    ctx.step_banner("Docker image")

    if not shutil.which("docker"):
        err("docker not found on PATH — cannot build image")
        info(
            "  Docker Desktop users: the CLI joins PATH only after the app has "
            "started — launch Docker Desktop, then reopen this terminal"
        )
        ctx.record("docker_image", "err", "docker not on PATH")
        return

    _base_image_note(selected_image)
    exists = _docker_image_exists()
    docker_dir = docker_data_dir()
    booley_root = docker_dir.parent.parent.parent.parent
    fingerprint = _image_build_fingerprint(booley_root)
    expected_version = _expected_version(booley_root)
    _warn_on_distribution_version_drift(booley_root)

    if _prepare_existing_base_image(
        ctx,
        docker_dir,
        exists=exists,
        fingerprint=fingerprint,
        expected_version=expected_version,
    ):
        return

    if ctx.check_only:
        _docker_check_only(ctx, exists)
        return

    # Pull-first strategy (skip if --force requests fresh local build)
    if not ctx.force:
        version = expected_version
        if _try_pull_image(version):
            ok(f"{DOCKER_IMAGE} pulled from registry (v{version})")
            ctx.record("docker_image", "ok", "pulled")
            return
        info("pre-built image unavailable, building locally (~20 min)")

    _docker_local_build(ctx, docker_dir, exists, fingerprint)


def _docker_check_only(ctx: InitContext, exists: bool) -> None:
    """Handle --check-only mode for Docker image step."""
    if exists:
        skip(f"{DOCKER_IMAGE} image already present")
        ctx.record("docker_image", "skip", "already present")
    else:
        warn(f"{DOCKER_IMAGE} image missing (would build)")
        ctx.record("docker_image", "warn", "would build")


def _local_build_inputs(ctx: InitContext, docker_dir: Path) -> tuple[Path, Path, Path] | None:
    """Validate and return candidate Dockerfile, base Dockerfile, and repo root."""
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
        err("cannot determine Booley repo root for docker build context")
        info("  build manually: ./src/booley/data/docker/build.sh")
        ctx.record("docker_image", "err", "repo root not found")
        return None
    return dockerfile, base_dockerfile, booley_root


def _docker_local_build(
    ctx: InitContext,
    docker_dir: Path,
    exists: bool,
    fingerprint: str | None = None,
) -> None:
    """Build the runtime base, wheel, and candidate image from local sources."""
    inputs = _local_build_inputs(ctx, docker_dir)
    if inputs is None:
        return
    dockerfile, base_dockerfile, booley_root = inputs

    if fingerprint is None:
        fingerprint = _image_build_fingerprint(booley_root)

    if not _docker_build_runtime_base(ctx, base_dockerfile, booley_root):
        return

    runtime_base_id = _docker_image_id(LOCAL_RUNTIME_BASE_IMAGE)
    if runtime_base_id is None:
        err("could not resolve the stable runtime-base artifact after its build")
        ctx.record("docker_image", "err", "runtime-base identity missing")
        return

    if not _docker_build_wheel(ctx, booley_root):
        return

    build = _DockerBuildSpec(
        dockerfile=dockerfile,
        context=booley_root,
        exists=exists,
        fingerprint=fingerprint,
        build_contexts=(("booley-runtime-base", f"docker-image://{LOCAL_RUNTIME_BASE_IMAGE}"),),
        build_args=("--build-arg", f"BOOLEY_RUNTIME_BASE_IMAGE={runtime_base_id}"),
        parent_artifact=runtime_base_id,
    )
    returncode = _docker_build_image(ctx, build)
    if returncode is None:
        return  # error already recorded

    if returncode != 0:
        err("docker build failed — re-run with -v for full output")
        ctx.record("docker_image", "err", "build failed")
        return

    ok(f"{DOCKER_IMAGE} image built successfully")
    _report_build_cache()
    ctx.record("docker_image", "ok", "built")


def _docker_build_runtime_base(ctx: InitContext, dockerfile: Path, booley_root: Path) -> bool:
    """Build the local named base consumed by the thin candidate Dockerfile."""
    try:
        build_args = _runtime_base_build_metadata_args(booley_root)
    except (OSError, ValueError) as error:
        err(f"stable runtime-base contract failed: {error}")
        ctx.record("docker_image", "err", "runtime-base contract failed")
        return False
    build = _DockerBuildSpec(
        dockerfile=dockerfile,
        context=booley_root,
        exists=_docker_image_exists(LOCAL_RUNTIME_BASE_IMAGE),
        image=LOCAL_RUNTIME_BASE_IMAGE,
        build_note="stable EDA/runtime layers are cached across source changes",
        build_args=tuple(build_args),
    )
    returncode = _docker_build_image(ctx, build)
    if returncode == 0:
        return True
    if returncode is not None:
        err("stable runtime-base build failed — re-run with -v for full output")
        ctx.record("docker_image", "err", "runtime-base build failed")
    return False


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
                info("  large — reclaim with: docker builder prune")
            return


def _docker_build_wheel(ctx: InitContext, booley_root: Path) -> bool:
    """Build the commit-stamped booley wheel into dist/. True on success.

    The stamp is what lets ``booley --version`` in the built container name the
    commit it came from; without it every init-driven image reported a bare
    ``booley <version>`` and the dev-install freshness check was unanswerable
    (F-3). ``build.sh`` stamps through the same helper.
    """
    dist_dir = booley_root / "dist"
    build_dir = booley_root / "build"
    info("building booley wheel...")
    try:
        with build_stamp(booley_root) as commit:
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
        return True
    except (subprocess.SubprocessError, OSError) as e:
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
        info("  fix: pip install build   (or: pip install -e '.[dev]')")
    elif "ensurepip is not available" in combined:
        # Debian/Ubuntu split venv out of the interpreter package; ``build``
        # needs it to make its isolated environment. The version must match the
        # interpreter running init, not the distro default (docs/user/TROUBLESHOOTING.md).
        pyver = f"{sys.version_info.major}.{sys.version_info.minor}"
        info(f"  fix: sudo apt install python{pyver}-venv   (matches this interpreter)")
        info("       or: pip install build && python -m build --wheel --no-isolation")
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
    if spec.image in FLAVOR_IMAGES:
        base_image_id = _docker_image_id(DOCKER_IMAGE)
        if base_image_id:
            build_cmd += ["--label", f"{LABEL_BASE_IMAGE_ID}={base_image_id}"]
            build_cmd += _local_parent_label_args(base_image_id)
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
    build_timeout = int(os.environ.get("BOOLEY_IMAGE_BUILD_TIMEOUT", "7200"))
    try:
        result = run_docker_build(
            _docker_build_command(spec),
            image=spec.image,
            verbose=ctx.verbose,
            timeout=build_timeout,
        )
        _render_build_diagnostics(result)
        if result.timed_out:
            err(
                f"docker build timed out after {build_timeout // 60} minutes "
                "(override with BOOLEY_IMAGE_BUILD_TIMEOUT)"
            )
            ctx.record(spec.record_key, "err", "build timed out")
            return None
        return result.returncode
    except subprocess.TimeoutExpired:
        err(
            f"docker build timed out after {build_timeout // 60} minutes "
            "(override with BOOLEY_IMAGE_BUILD_TIMEOUT)"
        )
        ctx.record(spec.record_key, "err", "build timed out")
        return None
    except (FileNotFoundError, OSError) as e:
        err(f"docker build failed: {e}")
        ctx.record(spec.record_key, "err", str(e))
        return None


# ---------------------------------------------------------------------------
# Booley-shipped sandbox flavors (record key: flavor_image) — booley-sandbox-riscv, ...
# ---------------------------------------------------------------------------


def _flavor_build(
    ctx: InitContext,
    image: str,
    dockerfile: Path,
    exists: bool,
    fingerprint: str | None,
) -> bool:
    """``docker build`` a flavor from its shipped Dockerfile; True once it exists."""
    docker_dir = dockerfile.parent
    build = _DockerBuildSpec(
        dockerfile=dockerfile,
        # Context is docker_dir, not the repo root build-riscv.sh passes: a
        # flavor Dockerfile has no COPY (it only layers toolchains onto the
        # base), so the context is unused — and a pip-installed Booley has no
        # repo root to point at while data/docker/ always exists. This is why
        # flavor Dockerfiles must stay COPY-free.
        context=docker_dir,
        exists=exists,
        fingerprint=fingerprint,
        image=image,
        record_key="project_image",
        build_note="this can take 10-20 minutes",
    )
    returncode = _docker_build_image(ctx, build)
    if returncode is None:
        return False  # error already recorded
    if returncode != 0:
        err(f"failed to build {image} — re-run with -v for full output")
        ctx.record("project_image", "err", f"{image} build failed")
        return False
    ok(f"{image} image built successfully")
    _report_build_cache()
    ctx.record("project_image", "ok", f"flavor {image} built")
    return True


def _prepare_flavor_without_build(
    ctx: InitContext,
    image: str,
    *,
    exists: bool,
    fingerprint: str | None,
    expected_version: str,
) -> bool | None:
    """Return changed/current when handled, or ``None`` when a local build is needed."""
    inspect_existing = exists and not ctx.force
    installed_release_mismatch = (
        inspect_existing
        and fingerprint is None
        and (_installed_image_version(image) != expected_version)
    )
    source_stale = (
        inspect_existing
        and fingerprint is not None
        and _image_is_stale(fingerprint, image, expected_version)
    )
    if inspect_existing and not installed_release_mismatch and not source_stale:
        skip(f"{image} is a Booley-shipped sandbox flavor and is up to date")
        ctx.record("project_image", "skip", f"flavor {image} current")
        return False
    if ctx.check_only:
        if installed_release_mismatch:
            warn(f"would pull compatible image for the {image} sandbox flavor")
            ctx.record("project_image", "warn", "would pull compatible image")
            return False
        verb = "rebuild (stale)" if exists else "pull or build"
        warn(f"would {verb} the {image} sandbox flavor")
        ctx.record("project_image", "warn", f"would {verb}")
        return False
    should_pull = not exists or installed_release_mismatch
    if should_pull and not ctx.force and _try_pull_image(expected_version, image):
        ok(f"{image} pulled from registry")
        ctx.record("project_image", "ok", f"flavor {image} pulled")
        return True
    return None


def _flavor_base_ready(ctx: InitContext, ensure_base: Callable[[], None] | None) -> bool:
    """Provision a flavor's local-build base on demand and report whether it succeeded."""
    if ensure_base is None:
        return True
    result_count = len(ctx.results)
    ensure_base()
    base_results = ctx.results[result_count:]
    return not any(result.status in {"warn", "err"} for result in base_results)


def _handle_flavor_without_dockerfile(
    ctx: InitContext,
    image: str,
    dockerfile_name: str,
    *,
    exists: bool,
    fingerprint: str | None,
    expected_version: str,
) -> bool:
    """Handle a flavor whose local build recipe is unavailable."""
    if not exists:
        err(f"{image} is missing and cannot be built — no shipped {dockerfile_name}")
        info(f"  pull it: docker pull {remote_tag(image, expected_version)}")
        ctx.record("project_image", "err", f"flavor {image} unavailable")
        return False
    installed_version = (
        _installed_image_version(image) if fingerprint is None else expected_version
    )
    if installed_version != expected_version:
        _report_incompatible_image(
            ctx,
            image,
            installed_version,
            expected_version,
            record_key="project_image",
            detail=f"flavor {image} compatible image pull failed",
        )
        return False
    skip(f"{image} present; no shipped {dockerfile_name} to rebuild from — trusting it")
    ctx.record("project_image", "skip", f"flavor {image} unverifiable")
    return False


def ensure_flavor_image(
    ctx: InitContext,
    image: str,
    *,
    ensure_base: Callable[[], None] | None = None,
) -> bool:
    """Pull, build, or refresh the selected Booley-shipped sandbox flavor.
    Return whether this run changed the image.
    """
    dockerfile_name = FLAVOR_IMAGES[image]
    docker_dir = docker_data_dir()
    dockerfile = docker_dir / dockerfile_name
    exists = _docker_image_exists(image)
    fingerprint = _image_build_fingerprint(docker_dir.parent.parent.parent.parent)
    expected_version = _expected_version(docker_dir.parent.parent.parent.parent)
    prepared = _prepare_flavor_without_build(
        ctx,
        image,
        exists=exists,
        fingerprint=fingerprint,
        expected_version=expected_version,
    )
    if prepared is not None:
        return prepared
    if not dockerfile.is_file():
        return _handle_flavor_without_dockerfile(
            ctx,
            image,
            dockerfile_name,
            exists=exists,
            fingerprint=fingerprint,
            expected_version=expected_version,
        )

    if not _flavor_base_ready(ctx, ensure_base):
        installed_version = _installed_image_version(image) if fingerprint is None else None
        if exists and installed_version != expected_version:
            _report_incompatible_image(
                ctx,
                image,
                installed_version,
                expected_version,
                record_key="project_image",
                detail=f"flavor {image} compatible image pull failed",
            )
        return False
    if exists:
        warn(f"{image} is stale (its {DOCKER_IMAGE} base or Booley's sources changed)")
        warn("  rebuilding — a session left on the old image keeps serving pre-rebuild code")
    return _flavor_build(ctx, image, dockerfile, exists, fingerprint)
