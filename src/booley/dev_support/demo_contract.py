"""CI-owned contract for the public PicoRV32 demo repository."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from booley.dev_support.demo_contract_codec import (
    DemoContract,
    DemoContractError,
    GeneratedInput,
    load_contract,
)
from booley.flows.execution import flow_enabled
from booley.fusesoc import fusesoc_registry
from booley.goals.model import parse_goal_arg
from booley.goals.translate import translate_goals
from booley.runtime.project_prepare import prepare_project
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import FuseSocError

__all__ = [
    "DemoContract",
    "DemoContractError",
    "GeneratedInput",
    "load_contract",
    "validate_demo",
]


def _target_inputs(catalog: TargetCatalog, target: str) -> tuple[Any, ...]:
    """Return condition-selected inputs through the demo checkout catalog."""
    return catalog.inspect(catalog.select(target)).inputs


def _resolve_catalog_target(
    catalog: TargetCatalog,
    target: str,
    build_root: Path,
) -> Any:
    """Resolve one already-selected demo Target."""
    return fusesoc_registry.resolve_target_handle(
        catalog.select(target),
        build_root=build_root,
    )


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=60,
        check=check,
    )


def _require_checkout_ref(repository: Path, expected: str, label: str) -> None:
    actual = _git(repository, "rev-parse", "HEAD").stdout.strip()
    if actual != expected:
        raise DemoContractError(f"{label} checkout is {actual}, expected {expected}")


def _status(repository: Path) -> str:
    return _git(
        repository,
        "status",
        "--porcelain",
        "--untracked-files=all",
    ).stdout.strip()


def _prepare_demo_project(root: Path) -> list[str]:
    preparation = prepare_project(
        root, root, slug="demo-readiness", sim_flow_enabled=flow_enabled("sim", root)
    )
    return [] if preparation.ok else [preparation.error]


def _validate_targets(root: Path, targets: tuple[str, ...]) -> list[str]:
    """Resolve every advertised Target; readiness never allows future inputs."""
    errors: list[str] = []
    catalog = TargetCatalog.build(root)
    with tempfile.TemporaryDirectory(prefix="booley-demo-targets-") as build_root:
        for index, target in enumerate(targets):
            try:
                resolved = _resolve_catalog_target(
                    catalog,
                    target,
                    Path(build_root) / f"target-{index}",
                )
                if not resolved.toplevel:
                    errors.append(f"required Target {target!r} resolves without a toplevel")
            except (FuseSocError, OSError) as exc:
                errors.append(f"required Target {target!r}: {exc}")
    return errors


def _validate_generated_inputs(
    root: Path,
    generated_inputs: tuple[GeneratedInput, ...],
) -> tuple[list[str], dict[str, str]]:
    errors: list[str] = []
    digests: dict[str, str] = {}
    for generated in generated_inputs:
        item_errors, path, digest = _validate_generated_input(root, generated)
        errors.extend(item_errors)
        if path and digest:
            digests[path] = digest
    return errors, digests


def _validate_generated_input(
    root: Path,
    generated: GeneratedInput,
) -> tuple[list[str], str, str]:
    """Validate one generated artifact's producer, consumers, and Git policy."""
    errors: list[str] = []
    path = generated.path
    producer = generated.producer
    targets = generated.targets
    digest = ""
    artifact = root / path
    if not artifact.is_file():
        errors.append(f"generated input was not prepared: {path}")
    else:
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    if _git(root, "ls-files", "--error-unmatch", "--", path, check=False).returncode == 0:
        errors.append(f"generated input must not be committed: {path}")
    if _git(root, "check-ignore", "--quiet", "--", path, check=False).returncode != 0:
        errors.append(f"generated input must be ignored: {path}")
    if not (root / producer).is_file():
        errors.append(f"generated input producer is missing for {path}: {producer}")
    catalog = TargetCatalog.build(root)
    for target in targets:
        try:
            referenced = [item.path for item in _target_inputs(catalog, target)]
        except FuseSocError as exc:
            errors.append(f"generated input {path} target {target!r}: {exc}")
            continue
        if path not in referenced:
            errors.append(f"Target {target!r} does not declare generated input {path}")
    return errors, path, digest


def validate_demo(
    contract_path: Path | str,
    demo_root: Path | str,
    project_dir: Path | str,
) -> list[str]:
    """Run the complete, idempotent public-demo readiness contract."""
    contract = load_contract(contract_path)
    try:
        translate_goals(tuple(parse_goal_arg(goal) for goal in contract.required_goals))
    except ValueError as exc:
        raise DemoContractError(f"invalid required Goal: {exc}") from exc
    root = Path(demo_root).resolve()
    project = Path(project_dir).resolve()
    errors: list[str] = []
    try:
        _require_checkout_ref(root, contract.upstream_ref, "upstream")
        _require_checkout_ref(project, contract.project_ref, "project")
    except DemoContractError as exc:
        return [str(exc)]
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        return [f"Git inspection failed (rc={exc.returncode}): {detail}"]

    before = (_status(root), _status(project))
    errors.extend(_prepare_demo_project(root))
    errors.extend(_validate_targets(root, contract.required_targets))
    generated_errors, first_digests = _validate_generated_inputs(root, contract.generated_inputs)
    errors.extend(generated_errors)
    errors.extend(_validate_second_preparation(root, contract, first_digests))
    after = (_status(root), _status(project))
    if before != after:
        errors.append("project preparation changed Git-visible checkout state")
    if any(after):
        errors.append("demo checkouts are not pristine after preparation")
    return errors


def _validate_second_preparation(
    root: Path, contract: DemoContract, first_digests: Mapping[str, str]
) -> list[str]:
    errors = [f"second preparation: {error}" for error in _prepare_demo_project(root)]
    generated_errors, second_digests = _validate_generated_inputs(root, contract.generated_inputs)
    errors.extend(f"second preparation: {error}" for error in generated_errors)
    if first_digests != second_digests:
        errors.append("project preparation is not idempotent: generated input digests changed")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contract", required=True, type=Path)
    parser.add_argument("--demo-root", required=True, type=Path)
    parser.add_argument("--project-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        errors = validate_demo(args.contract, args.demo_root, args.project_dir)
    except DemoContractError as exc:
        errors = [str(exc)]
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 2
    print("PicoRV32 demo contract passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
