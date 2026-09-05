"""CI-owned contract for the public PicoRV32 demo repository."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import tempfile
import tomllib
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from booley.core.boundary import (
    BoundaryError,
    is_str_list,
    require_dict,
    require_int,
    require_str,
)
from booley.fusesoc import fusesoc_registry
from booley.runtime.git import scope_matches_file
from booley.ticket_board.acceptance_targets import criterion_targets
from booley.ticket_board.frontmatter import parse_frontmatter
from booley.ticket_board.readiness import check_ticket_ready
from booley.ticket_board.scanner import find_ticket_file


class DemoContractError(ValueError):
    """The demo checkout does not satisfy its CI contract."""


@dataclass(frozen=True)
class RequiredBinding:
    """One criterion-to-Target binding required by the public demo."""

    criterion: str
    target: str


@dataclass(frozen=True)
class GeneratedInput:
    """One ignored artifact prepared for the demo's consuming Targets."""

    path: str
    producer: str
    targets: tuple[str, ...]


@dataclass(frozen=True)
class DemoContract:
    """Trusted, typed representation of the TOML contract boundary."""

    schema: int
    upstream_repository: str
    upstream_ref: str
    project_repository: str
    project_ref: str
    ticket_fixture: str
    ticket_slug: str
    toolchain_url: str
    toolchain_sha256: str
    required_targets: tuple[str, ...]
    required_bindings: tuple[RequiredBinding, ...]
    generated_inputs: tuple[GeneratedInput, ...]


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=60,
        check=check,
    )


def _safe_relative_path(document: Mapping[str, Any], key: str, *, field: str) -> str:
    value = require_str(document, key)
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:
        raise BoundaryError(f"{field} must be a safe relative path")
    return value


def _require_trimmed_str(document: Mapping[str, Any], key: str) -> str:
    """Read a boundary string and reject values containing only whitespace."""
    value = require_str(document, key).strip()
    if not value:
        raise BoundaryError(f"{key} must be a non-empty string")
    return value


def _parse_bindings(document: Mapping[str, Any]) -> tuple[RequiredBinding, ...]:
    raw_bindings = document.get("required_binding")
    if not isinstance(raw_bindings, list) or not raw_bindings:
        raise BoundaryError("required_binding must be a non-empty array of tables")
    bindings: list[RequiredBinding] = []
    for index, raw_binding in enumerate(raw_bindings):
        binding = require_dict(raw_binding, field=f"required_binding[{index}]")
        try:
            bindings.append(
                RequiredBinding(
                    criterion=require_str(binding, "criterion"),
                    target=require_str(binding, "target"),
                )
            )
        except BoundaryError as exc:
            raise BoundaryError(
                f"required_binding[{index}] must contain criterion and target strings"
            ) from exc
    return tuple(bindings)


def _parse_generated_inputs(document: Mapping[str, Any]) -> tuple[GeneratedInput, ...]:
    raw_inputs = document.get("generated_input")
    if not isinstance(raw_inputs, list) or not raw_inputs:
        raise BoundaryError("generated_input must be a non-empty array of tables")
    inputs: list[GeneratedInput] = []
    for index, raw_input in enumerate(raw_inputs):
        generated = require_dict(raw_input, field=f"generated_input[{index}]")
        path_value = _safe_relative_path(generated, "path", field=f"generated_input[{index}].path")
        producer = _safe_relative_path(
            generated, "producer", field=f"generated_input[{index}].producer"
        )
        consumers = generated.get("targets")
        if not is_str_list(consumers) or not consumers or not all(consumers):
            raise BoundaryError(f"generated_input[{index}].targets must be list[str]")
        inputs.append(GeneratedInput(path_value, producer, tuple(consumers)))
    return tuple(inputs)


def load_contract(path: Path | str) -> DemoContract:
    """Load and structurally validate a demo contract."""
    contract_path = Path(path)
    try:
        document = tomllib.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise DemoContractError(f"cannot read demo contract: {exc}") from exc
    try:
        schema = require_int(document.get("schema"), field="schema")
        if schema != 1:
            raise BoundaryError("schema must be 1")
        upstream_repository = _require_trimmed_str(document, "upstream_repository")
        upstream_ref = _require_trimmed_str(document, "upstream_ref")
        project_repository = _require_trimmed_str(document, "project_repository")
        project_ref = _require_trimmed_str(document, "project_ref")
        toolchain_url = _require_trimmed_str(document, "toolchain_url")
        toolchain_sha256 = _require_trimmed_str(document, "toolchain_sha256")
        for key, commit in (("upstream_ref", upstream_ref), ("project_ref", project_ref)):
            if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
                raise BoundaryError(f"{key} must be a full lowercase Git commit SHA")
        parsed_toolchain_url = urllib.parse.urlsplit(toolchain_url)
        if (
            parsed_toolchain_url.scheme != "https"
            or not parsed_toolchain_url.hostname
            or parsed_toolchain_url.username is not None
            or parsed_toolchain_url.password is not None
            or parsed_toolchain_url.query
            or parsed_toolchain_url.fragment
        ):
            raise BoundaryError("toolchain_url must be a plain HTTPS URL")
        if len(toolchain_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in toolchain_sha256
        ):
            raise BoundaryError("toolchain_sha256 must be a lowercase SHA-256 digest")

        raw_targets = document.get("required_targets")
        if not is_str_list(raw_targets) or not raw_targets or not all(raw_targets):
            raise BoundaryError("required_targets must be a non-empty list[str]")

        ticket_fixture = _safe_relative_path(document, "ticket_fixture", field="ticket_fixture")
        ticket_slug = _require_trimmed_str(document, "ticket_slug")
        bindings = _parse_bindings(document)
        generated_inputs = _parse_generated_inputs(document)
    except BoundaryError as exc:
        raise DemoContractError(str(exc)) from exc

    return DemoContract(
        schema=schema,
        upstream_repository=upstream_repository,
        upstream_ref=upstream_ref,
        project_repository=project_repository,
        project_ref=project_ref,
        ticket_fixture=ticket_fixture,
        ticket_slug=ticket_slug,
        toolchain_url=toolchain_url,
        toolchain_sha256=toolchain_sha256,
        required_targets=tuple(raw_targets),
        required_bindings=bindings,
        generated_inputs=generated_inputs,
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


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _ticket_fields(project_dir: Path, slug: str) -> tuple[dict[str, Any], Path]:
    ticket, _status_name = find_ticket_file(project_dir / "tickets", slug)
    if ticket is None:
        raise DemoContractError(f"ticket {slug!r} is missing")
    fields, _body = parse_frontmatter(ticket.read_text(encoding="utf-8"))
    return fields, ticket


def _validate_ticket_fixture(contract_path: Path, fixture: str, ticket: Path) -> list[str]:
    repository_root = contract_path.resolve().parents[2]
    fixture_path = repository_root / fixture
    if not fixture_path.is_file():
        return [f"CI-owned ticket fixture is missing: {fixture}"]
    if _digest(fixture_path) != _digest(ticket):
        return [f"injected ticket does not match CI-owned fixture: {fixture}"]
    return []


def _validate_targets(
    root: Path, fields: Mapping[str, Any], targets: tuple[str, ...]
) -> list[str]:
    """Resolve every advertised Target unless all missing inputs are Scope [new]."""
    errors: list[str] = []
    future = {
        entry.removesuffix(" [new]")
        for entry in fields.get("scope", [])
        if isinstance(entry, str) and entry.endswith(" [new]")
    }
    with tempfile.TemporaryDirectory(prefix="booley-demo-targets-") as build_root:
        for index, target in enumerate(targets):
            try:
                missing = fusesoc_registry.missing_target_sources(root, target)
                if missing and set(missing) <= future:
                    continue
                resolved = fusesoc_registry.resolve_target(
                    target,
                    project_root=root,
                    build_root=Path(build_root) / f"target-{index}",
                )
                if not resolved.toplevel:
                    errors.append(f"required Target {target!r} resolves without a toplevel")
            except (fusesoc_registry.FuseSocError, OSError) as exc:
                errors.append(f"required Target {target!r}: {exc}")
    return errors


def _validate_bindings(
    fields: Mapping[str, Any], bindings: tuple[RequiredBinding, ...]
) -> list[str]:
    actual = {
        (binding.label, binding.target) for binding in criterion_targets(fields.get("criteria"))
    }
    errors: list[str] = []
    for expected in bindings:
        pair = (expected.criterion, expected.target)
        if pair not in actual:
            errors.append(f"ticket is missing required binding {pair[0]} -> {pair[1]}")
    return errors


def _validate_generated_inputs(
    root: Path,
    fields: Mapping[str, Any],
    generated_inputs: tuple[GeneratedInput, ...],
) -> tuple[list[str], dict[str, str]]:
    errors: list[str] = []
    digests: dict[str, str] = {}
    scope = [
        entry.removesuffix(" [new]") for entry in fields.get("scope", []) if isinstance(entry, str)
    ]
    for generated in generated_inputs:
        item_errors, path, digest = _validate_generated_input(root, scope, generated)
        errors.extend(item_errors)
        if path and digest:
            digests[path] = digest
    return errors, digests


def _validate_generated_input(
    root: Path,
    scope: list[str],
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
        digest = _digest(artifact)
    if scope_matches_file(scope, path):
        errors.append(f"generated input must not be ticket Scope: {path}")
    if _git(root, "ls-files", "--error-unmatch", "--", path, check=False).returncode == 0:
        errors.append(f"generated input must not be committed: {path}")
    if _git(root, "check-ignore", "--quiet", "--", path, check=False).returncode != 0:
        errors.append(f"generated input must be ignored: {path}")
    if not (root / producer).is_file():
        errors.append(f"generated input producer is missing for {path}: {producer}")
    for target in targets:
        try:
            referenced = fusesoc_registry.target_referenced_files(root, target)
        except fusesoc_registry.FuseSocError as exc:
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
    root = Path(demo_root).resolve()
    project = Path(project_dir).resolve()
    errors: list[str] = []
    try:
        _require_checkout_ref(root, contract.upstream_ref, "upstream")
        _require_checkout_ref(project, contract.project_ref, "project")
        fields, ticket = _ticket_fields(project, contract.ticket_slug)
    except DemoContractError as exc:
        return [str(exc)]
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        return [f"Git inspection failed (rc={exc.returncode}): {detail}"]

    before = (_status(root), _status(project))
    errors.extend(_validate_ticket_fixture(Path(contract_path), contract.ticket_fixture, ticket))
    first = check_ticket_ready(root, contract.ticket_slug)
    errors.extend(first.errors)
    errors.extend(_validate_targets(root, fields, contract.required_targets))
    errors.extend(_validate_bindings(fields, contract.required_bindings))
    generated_errors, first_digests = _validate_generated_inputs(
        root, fields, contract.generated_inputs
    )
    errors.extend(generated_errors)

    second = check_ticket_ready(root, contract.ticket_slug)
    errors.extend(f"second preparation: {error}" for error in second.errors)
    generated_errors, second_digests = _validate_generated_inputs(
        root, fields, contract.generated_inputs
    )
    errors.extend(f"second preparation: {error}" for error in generated_errors)
    if first_digests != second_digests:
        errors.append("project preparation is not idempotent: generated input digests changed")
    after = (_status(root), _status(project))
    if before != after:
        errors.append("project preparation changed Git-visible checkout state")
    if any(after):
        errors.append("demo checkouts are not pristine after preparation")
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
