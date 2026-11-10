"""Import-light owner of the installed Host Bootstrap runtime base."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

from booley.runtime.build_stamp import resolve_build_commit
from booley.runtime.docker_build import DockerBuildResult, run_docker_build
from booley.runtime.docker_capacity import (
    BuildEstimateClass,
    DockerBuildPlan,
    DockerBuildRequest,
    DockerCapacityError,
    _plan_from_file,
)
from booley.runtime.image_build_contracts import source_image_build_contracts
from booley.runtime.image_identity import ImageMetadata
from booley.runtime.image_lifecycle import _prepared_provenance, _source_graph_base
from booley.runtime.image_provenance import LABEL_RUNTIME_BASE_CONTRACT
from booley.runtime.interactive_docker import inspect_image_metadata
from booley.runtime.lifecycle_lock import host_lifecycle_lock
from booley.runtime.timefmt import utc_now_rfc3339

REFERENCE = "booley-runtime-base:local"


def _labels(root: Path) -> dict[str, str]:
    node, _ = _source_graph_base(
        source_image_build_contracts(root), root / "src/booley/data/docker"
    )
    return _prepared_provenance(node, None)


class RuntimeBaseBuildError(RuntimeError):
    """A runtime-base ``docker build`` did not produce the image.

    ``result`` is the failed or timed-out build outcome, or ``None`` when the
    Docker CLI could not run (the underlying ``OSError`` is the ``__cause__``).
    """

    def __init__(self, message: str, result: DockerBuildResult | None) -> None:
        super().__init__(message)
        self.result = result


def build_timeout() -> int:
    """Seconds a runtime-base build may run (``BOOLEY_IMAGE_BUILD_TIMEOUT``, default 7200)."""
    return int(os.environ.get("BOOLEY_IMAGE_BUILD_TIMEOUT", "7200"))


def _metadata_args(root: Path) -> tuple[str, ...]:
    """Build args that ``Dockerfile.base`` turns into revision and created labels."""
    revision = resolve_build_commit(root) if (root / ".git").exists() else "unknown"
    values = {
        "BOOLEY_BASE_SOURCE_REVISION": revision or "unknown",
        "BOOLEY_BASE_BUILT_AT": utc_now_rfc3339(),
    }
    return tuple(
        item for name, value in values.items() for item in ("--build-arg", f"{name}={value}")
    )


def _build(
    root: Path,
    recipe: Path,
    output: str,
    plan: DockerBuildPlan | None,
    *,
    verbose: bool = True,
    extra_args: tuple[str, ...] = (),
) -> DockerBuildResult:
    labels = _labels(root)
    command = ["docker", "build"]
    for key, value in labels.items():
        command += ["--label", f"{key}={value}"]
    command += [
        "--build-arg",
        f"BOOLEY_BASE_CONTRACT={labels[LABEL_RUNTIME_BASE_CONTRACT]}",
        *extra_args,
        "-t",
        output,
        "-f",
        str(recipe),
        str(root),
    ]
    return run_docker_build(
        command,
        image=output,
        verbose=verbose,
        timeout=build_timeout(),
        current_request=plan.current if plan else None,
        remaining_plan=plan,
    )


def _upgrade(root: Path, installed: ImageMetadata, plan: DockerBuildPlan | None) -> str:
    labels = _labels(root)
    image_id = installed.image_id
    if all(installed.labels.get(key) == value for key, value in labels.items()):
        return image_id
    transaction = f"booley-bootstrap-base-{os.getpid()}-{uuid4().hex}"
    candidate, parent = f"{transaction}:candidate", f"{transaction}:parent"
    migration_plan = DockerBuildPlan(
        (
            DockerBuildRequest(REFERENCE, candidate, BuildEstimateClass.THIN_OVERLAY),
            *(
                (request for request in plan.requests if request.output_tag != REFERENCE)
                if plan
                else ()
            ),
        )
    )
    try:
        _tag(image_id, parent)
        pinned = inspect_image_metadata(parent)
        if pinned is None or pinned.image_id != image_id:
            raise RuntimeError("Bootstrap metadata migration parent alias changed")
        with tempfile.TemporaryDirectory(prefix="booley-base-metadata-") as directory:
            recipe = Path(directory) / "Dockerfile"
            recipe.write_text(f"FROM {parent}\n", encoding="utf-8")
            migration = _build(root, recipe, candidate, migration_plan)
            if migration.returncode != 0 or migration.timed_out:
                raise RuntimeError("Bootstrap metadata migration failed")
        changed = _verified_upgrade(installed, candidate, labels)
        current = inspect_image_metadata(REFERENCE)
        if current is None or current.image_id != image_id:
            raise RuntimeError("Bootstrap runtime-base identity changed before migration adoption")
        _tag(changed.image_id, REFERENCE)
        return changed.image_id
    finally:
        _cleanup_migration((candidate, parent), sys.exception())


def _verified_upgrade(
    installed: ImageMetadata, candidate: str, labels: dict[str, str]
) -> ImageMetadata:
    changed = inspect_image_metadata(candidate)
    layers = installed.layers
    if changed is None or not layers or changed.layers != layers:
        raise RuntimeError("Bootstrap metadata migration changed filesystem layers")
    if any(changed.labels.get(k) != v for k, v in labels.items()):
        raise RuntimeError("Bootstrap metadata migration did not preserve expected provenance")
    return changed


def _tag(source: str, target: str) -> None:
    try:
        subprocess.run(
            ["docker", "tag", source, target],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"cannot tag Bootstrap image {source!r} as {target!r}: {exc}") from exc


def _cleanup_migration(references: tuple[str, ...], primary: BaseException | None) -> None:
    failures = []
    for reference in references:
        try:
            result = subprocess.run(
                ["docker", "image", "rm", reference],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            if result.returncode and "no such image" not in (result.stderr or "").lower():
                failures.append(f"{reference}: {result.stderr or result.returncode}")
        except (OSError, subprocess.SubprocessError) as exc:
            failures.append(f"{reference}: {exc}")
    if failures:
        message = "Bootstrap migration cleanup failed: " + "; ".join(failures)
        if primary is not None:
            primary.add_note(message)
        else:
            raise RuntimeError(message)


def acquire(
    root: Path,
    *,
    rebuild: bool = False,
    capacity_plan: DockerBuildPlan | None = None,
    verbose: bool = True,
) -> str:
    """Acquire a contract-compatible base exclusively under host ownership.

    Raise ``RuntimeBaseBuildError`` when a required build fails.
    """
    installed = inspect_image_metadata(REFERENCE)
    expected = source_image_build_contracts(root).runtime_base
    if (
        installed is not None
        and not rebuild
        and installed.labels.get(LABEL_RUNTIME_BASE_CONTRACT) == expected
    ):
        return _upgrade(root, installed, capacity_plan)
    action = "Rebuilding" if installed is not None else "Building"
    print(
        f"{action} {REFERENCE} image — reusing layers when runtime-base "
        "Dockerfile inputs are unchanged."
    )
    try:
        outcome = _build(
            root,
            root / "src/booley/data/docker/Dockerfile.base",
            REFERENCE,
            capacity_plan,
            verbose=verbose,
            extra_args=_metadata_args(root),
        )
    except DockerCapacityError:
        raise  # a subclass of OSError: keep it distinct from CLI failures below
    except OSError as exc:
        raise RuntimeBaseBuildError(f"docker build failed: {exc}", None) from exc
    if outcome.returncode != 0 or outcome.timed_out:
        raise RuntimeBaseBuildError("runtime-base docker build failed", outcome)
    result = inspect_image_metadata(REFERENCE)
    if result is None or any(
        result.labels.get(key) != value for key, value in _labels(root).items()
    ):
        raise RuntimeError("Bootstrap runtime-base build has incomplete provenance")
    return result.image_id


def main() -> int:
    """Acquire the host base for maintenance scripts without CLI dependencies."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--plan-file", type=Path)
    parser.add_argument("--current-index", type=int, default=0)
    args = parser.parse_args()
    try:
        plan = _plan_from_file(args.plan_file, args.current_index)[1] if args.plan_file else None
        with host_lifecycle_lock("host runtime-base maintenance"):
            acquire(args.repo.resolve(), rebuild=args.refresh, capacity_plan=plan)
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"Bootstrap runtime-base acquisition failed: {exc}", file=sys.stderr)
        for note in getattr(exc, "__notes__", ()):
            print(note, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
