"""Import-light owner of the installed Host Bootstrap runtime base."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from booley.runtime.docker_build import run_docker_build
from booley.runtime.docker_capacity import DockerBuildPlan, _plan_from_file
from booley.runtime.image_build_contracts import source_image_build_contracts
from booley.runtime.image_lifecycle import HostImageScope, _prepared_provenance, _source_graph_base
from booley.runtime.image_provenance import LABEL_RUNTIME_BASE_CONTRACT
from booley.runtime.lifecycle_lock import host_lifecycle_lock

REFERENCE = "booley-runtime-base:local"


def _inspect(reference: str) -> dict | None:
    result = subprocess.run(
        ["docker", "image", "inspect", reference],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if result.returncode:
        return None
    rows = json.loads(result.stdout)
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise RuntimeError(f"cannot inspect Bootstrap image {reference!r}")
    return rows[0]


def _labels(root: Path) -> dict[str, str]:
    node, _ = _source_graph_base(
        source_image_build_contracts(root), root / "src/booley/data/docker"
    )
    return _prepared_provenance(node, None)


def _build(root: Path, recipe: Path, output: str, plan: DockerBuildPlan | None) -> bool:
    labels = _labels(root)
    command = ["docker", "build"]
    for key, value in labels.items():
        command += ["--label", f"{key}={value}"]
    command += [
        "--build-arg",
        f"BOOLEY_BASE_CONTRACT={labels[LABEL_RUNTIME_BASE_CONTRACT]}",
        "-t",
        output,
        "-f",
        str(recipe),
        str(root),
    ]
    result = run_docker_build(
        command,
        image=output,
        verbose=True,
        timeout=7200,
        current_request=plan.current if plan else None,
        remaining_plan=plan,
    )
    return result.returncode == 0 and not result.timed_out


def _upgrade(root: Path, installed: dict, plan: DockerBuildPlan | None) -> str:
    labels = _labels(root)
    image_id = installed["Id"]
    if all(
        installed.get("Config", {}).get("Labels", {}).get(key) == value
        for key, value in labels.items()
    ):
        return image_id
    candidate = f"booley-bootstrap-base-{os.getpid()}:candidate"
    try:
        with tempfile.TemporaryDirectory(prefix="booley-base-metadata-") as directory:
            recipe = Path(directory) / "Dockerfile"
            recipe.write_text(f"FROM {image_id}\n", encoding="utf-8")
            if not _build(root, recipe, candidate, plan):
                raise RuntimeError("Bootstrap metadata migration failed")
        changed = _inspect(candidate)
        layers = installed.get("RootFS", {}).get("Layers")
        if changed is None or not layers or changed.get("RootFS", {}).get("Layers") != layers:
            raise RuntimeError("Bootstrap metadata migration changed filesystem layers")
        if any(
            changed.get("Config", {}).get("Labels", {}).get(key) != value
            for key, value in labels.items()
        ):
            raise RuntimeError("Bootstrap metadata migration did not preserve expected provenance")
        subprocess.run(["docker", "tag", changed["Id"], REFERENCE], check=True, timeout=30)
        return changed["Id"]
    finally:
        subprocess.run(
            ["docker", "image", "rm", candidate], capture_output=True, check=False, timeout=30
        )


def acquire(
    root: Path,
    *,
    scope: HostImageScope,
    rebuild: bool = False,
    capacity_plan: DockerBuildPlan | None = None,
    build: Callable[[], bool] | None = None,
) -> str | None:
    """Acquire a contract-compatible base exclusively under host ownership."""
    if not isinstance(scope, HostImageScope):
        raise TypeError("runtime-base acquisition requires HostImageScope")
    installed = _inspect(REFERENCE)
    expected = source_image_build_contracts(root).runtime_base
    if (
        installed is not None
        and not rebuild
        and installed.get("Config", {}).get("Labels", {}).get(LABEL_RUNTIME_BASE_CONTRACT)
        == expected
    ):
        return _upgrade(root, installed, capacity_plan)
    operation = build or (
        lambda: _build(
            root, root / "src/booley/data/docker/Dockerfile.base", REFERENCE, capacity_plan
        )
    )
    if not operation():
        return None
    result = _inspect(REFERENCE)
    if result is None or any(
        result.get("Config", {}).get("Labels", {}).get(key) != value
        for key, value in _labels(root).items()
    ):
        raise RuntimeError("Bootstrap runtime-base build has incomplete provenance")
    return result["Id"]


def main() -> int:
    """Acquire the host base for maintenance scripts without CLI dependencies."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--plan-file", type=Path)
    parser.add_argument("--current-index", type=int, default=0)
    args = parser.parse_args()
    plan = _plan_from_file(args.plan_file, args.current_index)[1] if args.plan_file else None
    with host_lifecycle_lock("host runtime-base maintenance"):
        image = acquire(
            args.repo.resolve(), scope=HostImageScope(), rebuild=args.refresh, capacity_plan=plan
        )
    return 0 if image else 2


if __name__ == "__main__":
    raise SystemExit(main())
