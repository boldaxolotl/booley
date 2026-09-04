"""Acceptance Basis Target bindings and protected-input discovery.

The module is deliberately below the harness and Flows. It owns Target/control
path discovery, criterion-to-Target bindings, and their boundary validation.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from booley.core.boundary import (
    BoundaryError,
    is_str_list,
)
from booley.criteria.thresholds import has_relative_threshold
from booley.fusesoc import fusesoc_registry
from booley.runtime.project_dir import resolve_checkout_project_dir
from booley.targets.declared_inputs import referenced_program_paths
from booley.targets.target import flow_can_drive, inspect_target_selector, select_target

_FLOW_BY_CRITERION = {
    "sim_pass": "sim",
    "cycle_count": "sim",
    "lint_clean": "lint",
    "synthesis_ok": "synth",
    "fpga_impl_ok": "fpga",
    "mutation_score": "sim",
    "coverage": "sim",
}


@dataclass(frozen=True, order=True)
class AcceptanceTargetBinding:
    """Canonical directed Target identities and their callable selectors."""

    flow: str
    criterion: str
    baseline: str
    candidate: str
    baseline_selector: str = ""
    candidate_selector: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "flow": self.flow,
            "criterion": self.criterion,
            "baseline": self.baseline,
            "candidate": self.candidate,
            "baseline_selector": self.baseline_selector,
            "candidate_selector": self.candidate_selector,
        }


@dataclass(frozen=True)
class CriterionTarget:
    """One ticket criterion bound to one FuseSoC Target and owning Flow."""

    section: str
    key: str
    target: str
    flow: str
    relative: bool
    baseline_target: str | None = None

    @property
    def baseline(self) -> str:
        """Baseline Target, defaulting to the candidate for equal-Target criteria."""
        return self.baseline_target or self.target

    @property
    def label(self) -> str:
        """Frontmatter-style location used in diagnostics."""
        return f"criteria.{self.section}.{self.key}"


def _identity(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _project_control_files(root: Path) -> Iterator[Path]:
    routing = root / "booley.toml"
    if routing.is_file():
        yield routing
    try:
        project_dir = resolve_checkout_project_dir(root)
    except FileNotFoundError:
        return
    tests_path = project_dir / "tests.toml"
    if tests_path.is_file():
        yield tests_path
    config_path = project_dir / "booley.toml"
    if config_path.is_file() and config_path != routing:
        yield config_path


def _target_config(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        data = tomllib.load(stream)
    return {key: data[key] for key in ("flows", "targets", "fusesoc") if key in data}


def _core_referenced_files(root: Path, core_file: Path, doc: Mapping[str, Any]) -> Iterator[Path]:
    filesets = doc.get("filesets")
    if not isinstance(filesets, Mapping):
        return
    for fileset in filesets.values():
        if not isinstance(fileset, Mapping):
            continue
        files = fileset.get("files")
        if not isinstance(files, list):
            continue
        for entry in files:
            if isinstance(entry, str):
                raw = entry
            elif isinstance(entry, Mapping):
                raw = next(iter(entry), "")
            else:
                continue
            if not raw:
                continue
            rel = fusesoc_registry.core_relative_to_project(core_file, root, str(raw))
            yield root / rel


def _core_auxiliary_paths(root: Path, core_file: Path, doc: Mapping[str, Any]) -> set[Path]:
    paths: set[Path] = set()
    for candidate in _core_referenced_files(root, core_file, doc):
        if candidate.suffix.casefold() in {".sdc", ".xdc"} and candidate.is_file():
            paths.add(candidate)
    imperative = {
        key: doc[key] for key in ("generators", "generate", "scripts", "targets") if key in doc
    }
    paths.update(
        referenced_program_paths(
            imperative,
            search_roots=(core_file.parent,),
            project_root=root,
            strict=True,
        )
    )
    return paths


def _config_auxiliary_paths(root: Path, config_path: Path) -> set[Path]:
    """Find executable hooks referenced by Target-selection configuration."""
    return set(
        referenced_program_paths(
            _target_config(config_path),
            search_roots=(root, config_path.parent),
            project_root=root,
            strict=True,
        )
    )


def contract_control_paths(project_root: Path | str) -> tuple[str, ...]:
    """Return control inputs and entries capable of redirecting them."""
    root = Path(project_root).resolve()
    paths: set[Path] = set()
    for core_file in fusesoc_registry.discover_cores(root):
        paths.add(core_file)
        paths.update(_core_auxiliary_paths(root, core_file, fusesoc_registry.read_core(core_file)))
    for config_path in _project_control_files(root):
        paths.add(config_path)
        if config_path.name == "booley.toml":
            paths.update(_config_auxiliary_paths(root, config_path))
    for path in tuple(paths):
        paths.update(_redirecting_control_entries(root, path))
    gitlinks = _tracked_gitlinks(root)
    for path in tuple(paths):
        identity = _identity(root, path)
        paths.update(
            root / gitlink
            for gitlink in gitlinks
            if identity == gitlink or identity.startswith(gitlink.rstrip("/") + "/")
        )
    return tuple(sorted(_identity(root, path) for path in paths))


def _redirecting_control_entries(root: Path, path: Path) -> Iterator[Path]:
    """Yield in-checkout symlink/gitlink ancestors for one protected input."""
    current = path
    while current != root and current.is_relative_to(root):
        if current.is_symlink() or (current.is_dir() and (current / ".git").exists()):
            yield current
        current = current.parent


def _tracked_gitlinks(root: Path) -> set[str]:
    result = subprocess.run(
        ["git", "ls-files", "--stage", "-z"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
        raise BoundaryError(
            f"git ls-files failed during protected-input discovery in {root}: {detail}"
        )
    return {
        record.partition("\t")[2]
        for record in result.stdout.split("\0")
        if record.startswith("160000 ") and "\t" in record
    }


def _criterion_flow(key: str) -> str | None:
    from booley.criteria.templates import TARGET_BOUND_CRITERION_FLOWS

    flows = {**_FLOW_BY_CRITERION, **TARGET_BOUND_CRITERION_FLOWS}
    for prefix in sorted(flows, key=len, reverse=True):
        if key == prefix or key.startswith(prefix + "_"):
            return flows[prefix]
    return None


def _relative_params(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    return has_relative_threshold(dict(value))


def _targets_from_value(key: str, value: Any) -> list[tuple[str, str, bool]]:
    if isinstance(value, Mapping):
        from booley.criteria.templates import parse_target_pair

        target = value.get("target")
        if isinstance(target, str):
            return [(target, target, _relative_params(value))]
        targets = value.get("targets")
        if isinstance(targets, list):
            pairs: list[tuple[str, str, bool]] = []
            for index, target in enumerate(targets):
                try:
                    pair = parse_target_pair(target, field=f"{key}.targets[{index}]")
                except ValueError:
                    continue
                pairs.append((pair.candidate, pair.baseline, _relative_params(value)))
            return pairs
        return []
    if not isinstance(value, list):
        return []
    return _targets_from_list(key, value)


def _targets_from_list(key: str, value: list[Any]) -> list[tuple[str, str, bool]]:
    from booley.criteria.templates import parse_sim_criterion

    targets: list[tuple[str, str, bool]] = []
    for item in value:
        if key == "coverage" and isinstance(item, Mapping) and is_str_list(item.get("targets")):
            targets.extend((target, target, False) for target in item["targets"])
        elif isinstance(item, Mapping) and isinstance(item.get("target"), str):
            targets.append((item["target"], item["target"], _relative_params(item)))
        elif isinstance(item, str) and "->" in item:
            try:
                target = parse_sim_criterion(item).target
                targets.append((target, target, False))
            except ValueError:
                continue
        elif isinstance(item, str) and "@" not in item:
            targets.append((item, item, False))
    return targets


def criterion_targets(criteria: Any) -> tuple[CriterionTarget, ...]:
    """Extract Target bindings from mandatory and optional ticket criteria."""
    if not isinstance(criteria, Mapping):
        return ()
    bindings: list[CriterionTarget] = []
    for section_name in ("mandatory", "optional"):
        section = criteria.get(section_name)
        if not isinstance(section, Mapping):
            continue
        for key, value in section.items():
            flow = _criterion_flow(str(key))
            if flow is None:
                continue
            for target, baseline, relative in _targets_from_value(str(key), value):
                bindings.append(
                    CriterionTarget(
                        section_name,
                        str(key),
                        target,
                        flow,
                        relative,
                        baseline if baseline != target else None,
                    )
                )
    return tuple(bindings)


def _coverage_suite_selections(
    criteria: Any,
) -> Iterator[tuple[str, str, tuple[str, ...]]]:
    if not isinstance(criteria, Mapping):
        return
    for section_name in ("mandatory", "optional"):
        section = criteria.get(section_name)
        if not isinstance(section, Mapping):
            continue
        records = section.get("coverage")
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, Mapping):
                continue
            targets = record.get("targets")
            tests = record.get("tests")
            if not is_str_list(targets) or not is_str_list(tests):
                continue
            for target in targets:
                yield f"criteria.{section_name}.coverage", target, tuple(tests)


def _validate_coverage_suites(criteria: Any, root: Path) -> list[str]:
    selections = tuple(_coverage_suite_selections(criteria))
    if not selections:
        return []
    from booley.config.project_config import lookup_target_section, normalize_tests_toml

    try:
        tests_path = resolve_checkout_project_dir(root) / "tests.toml"
        with tests_path.open("rb") as stream:
            registry = normalize_tests_toml(tomllib.load(stream))
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        return [f"criteria.coverage: cannot validate registered tests: {exc}"]

    errors: list[str] = []
    for label, target, selected in selections:
        target_registry = lookup_target_section(registry, target)
        declared = target_registry.get("tests", []) if isinstance(target_registry, Mapping) else []
        missing = sorted(set(selected) - set(declared))
        if missing:
            errors.append(
                f"{label}: target {target!r} has unregistered tests: {', '.join(missing)}"
            )
    return errors


def _new_scope_matches(scope: Any, path: str) -> bool:
    import fnmatch

    if not isinstance(scope, list):
        return False
    normalized = path.replace("\\", "/").removeprefix("./")
    for raw in scope:
        if not isinstance(raw, str) or not raw.endswith(" [new]"):
            continue
        entry = raw.removesuffix(" [new]").replace("\\", "/").removeprefix("./")
        if normalized == entry or normalized.startswith(entry.rstrip("/") + "/"):
            return True
        if any(char in entry for char in "*?[") and fnmatch.fnmatchcase(normalized, entry):
            return True
    return False


def _missing_target_sources(
    root: Path,
    target: str,
) -> list[str]:
    selected = tuple(item.path for item in inspect_target_selector(root, target).inputs)
    missing: list[str] = []
    for path in selected:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = root / candidate
        if not candidate.exists():
            missing.append(path)
    return sorted(set(missing))


def validate_criterion_targets(fields: Mapping[str, Any], project_root: Path | str) -> list[str]:
    """Validate every mandatory/optional criterion Target without running tools."""
    root = Path(project_root)
    errors: list[str] = []
    for binding in criterion_targets(fields.get("criteria")):
        errors.extend(_validate_binding(binding, fields, root))
    errors.extend(_validate_coverage_suites(fields.get("criteria"), root))
    return errors


def validate_targets_for_seal(
    fields: Mapping[str, Any],
    project_root: Path | str,
    build_root: Path | str,
    *,
    changed_targets: Iterable[str] = (),
) -> list[str]:
    """Validate bindings and dry-resolve criterion and changed Targets."""
    root = Path(project_root)
    errors = validate_criterion_targets(fields, root)
    if errors:
        return errors
    bindings = criterion_targets(fields.get("criteria"))
    required = _required_targets(root, bindings)
    errors.extend(_validate_required_targets(root, Path(build_root), required))
    for binding in bindings:
        if binding.baseline != binding.target and not _missing_target_sources(
            root, binding.target
        ):
            errors.extend(_validate_comparison_basis(binding, root, Path(build_root)))
    errors.extend(
        _validate_changed_targets(
            fields,
            root,
            Path(build_root),
            changed_targets,
            seen=set(required),
        )
    )
    return errors


def _required_targets(
    root: Path,
    bindings: Iterable[CriterionTarget],
) -> dict[str, tuple[CriterionTarget, bool]]:
    # A Target used as a baseline anywhere always receives the stronger
    # executable-at-base requirement, even if another pair also uses it as a
    # candidate whose [new] sources could otherwise defer resolution.
    required: dict[str, tuple[CriterionTarget, bool]] = {}
    for binding in bindings:
        candidate_missing = bool(_missing_target_sources(root, binding.target))
        prior = required.get(binding.target)
        required[binding.target] = (
            binding,
            (prior[1] if prior else False) or not candidate_missing,
        )
        if binding.relative or binding.baseline != binding.target:
            required[binding.baseline] = (binding, True)
    return required


def _validate_required_targets(
    root: Path,
    build_root: Path,
    required: Mapping[str, tuple[CriterionTarget, bool]],
) -> list[str]:
    errors: list[str] = []
    for target, (binding, must_resolve) in required.items():
        if not must_resolve:
            continue
        target_build = Path(build_root) / _safe_target_dir(target)
        errors.extend(_dry_resolve_binding(binding, root, target_build, target=target))
    return errors


def _validate_changed_targets(
    fields: Mapping[str, Any],
    root: Path,
    build_root: Path,
    changed_targets: Iterable[str],
    *,
    seen: set[str],
) -> list[str]:
    errors: list[str] = []
    for target in changed_targets:
        if target in seen:
            continue
        seen.add(target)
        missing = _missing_target_sources(root, target)
        undeclared = [
            path for path in missing if not _new_scope_matches(fields.get("scope"), path)
        ]
        if undeclared:
            errors.append(
                f"changed Target {target!r} has missing source(s) not declared Scope [new]: "
                + ", ".join(undeclared)
            )
            continue
        if missing:
            continue
        binding = CriterionTarget("contract", "changed_target", target, "", False)
        target_build = build_root / _safe_target_dir(target)
        errors.extend(_dry_resolve_binding(binding, root, target_build))
    return errors


def _safe_target_dir(target: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", target).strip("_") or "target"


def _validate_comparison_basis(
    binding: CriterionTarget,
    root: Path,
    build_root: Path,
) -> list[str]:
    """Fail sealing when two resolvable Targets change measurement methodology."""
    try:
        snapshots = _comparison_snapshots(binding, root, build_root)
    except (fusesoc_registry.FuseSocError, BoundaryError, OSError) as exc:
        return [f"{binding.label}: cannot compare Target measurement basis: {exc}"]
    if snapshots is None:
        return []
    from booley.flows.recipe_evidence import implementation_comparison_basis, recipe_changes

    baseline_snapshot, candidate_snapshot = snapshots
    changes = recipe_changes(
        implementation_comparison_basis(baseline_snapshot),
        implementation_comparison_basis(candidate_snapshot),
    )
    if not changes:
        return []
    paths = ", ".join(str(change.get("path")) for change in changes[:5])
    return [
        f"{binding.label}: baseline Target {binding.baseline!r} and candidate Target "
        f"{binding.target!r} use incompatible measurement bases ({paths})"
    ]


def _comparison_snapshots(
    binding: CriterionTarget, root: Path, build_root: Path
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    baseline = fusesoc_registry.resolve_target(
        binding.baseline,
        project_root=root,
        build_root=build_root / f"basis-baseline-{_safe_target_dir(binding.baseline)}",
    )
    candidate = fusesoc_registry.resolve_target(
        binding.target,
        project_root=root,
        build_root=build_root / f"basis-candidate-{_safe_target_dir(binding.target)}",
    )
    if binding.flow == "synth":
        from booley.flows.synth.recipe import default_recipe_args, synthesis_recipe_snapshot

        args = default_recipe_args()
        return (
            synthesis_recipe_snapshot(baseline, args, target=binding.baseline),
            synthesis_recipe_snapshot(candidate, args, target=binding.target),
        )
    if binding.flow == "fpga":
        from booley.flows.fpga.recipe import fpga_recipe_snapshot

        return (
            fpga_recipe_snapshot(baseline, target=binding.baseline),
            fpga_recipe_snapshot(candidate, target=binding.target),
        )
    return None


def _dry_resolve_binding(
    binding: CriterionTarget,
    root: Path,
    build_root: Path,
    *,
    target: str | None = None,
) -> list[str]:
    selected = target or binding.target
    try:
        resolved = fusesoc_registry.resolve_target(
            selected,
            project_root=root,
            build_root=build_root,
        )
    except (fusesoc_registry.FuseSocError, OSError) as exc:
        return [f"{binding.label}: target {selected!r} dry-run failed: {exc}"]
    if not resolved.toplevel:
        return [f"{binding.label}: target {selected!r} resolves without a toplevel"]
    return []


def _validate_binding(
    binding: CriterionTarget,
    fields: Mapping[str, Any],
    root: Path,
) -> list[str]:
    errors: list[str] = []
    for role, target in (("candidate", binding.target), ("baseline", binding.baseline)):
        if role == "baseline" and target == binding.target:
            continue
        try:
            ref = select_target(root, target)
        except fusesoc_registry.FuseSocError as exc:
            errors.append(f"{binding.label}: {role} target {target!r}: {exc}")
            continue
        if not flow_can_drive(binding.flow, ref):
            errors.append(
                f"{binding.label}: {role} target {target!r} cannot satisfy {binding.key} "
                f"with Flow {binding.flow} (flow={ref.flow!r}, EDA tool={ref.eda_tool!r})"
            )
            continue
        missing = _missing_target_sources(root, target)
        if not missing:
            continue
        if role == "baseline" or (binding.relative and binding.baseline == binding.target):
            errors.append(
                f"{binding.label}: relative-QoR baseline target {target!r} has missing "
                f"source(s): {', '.join(missing)}"
            )
            continue
        undeclared = [
            path for path in missing if not _new_scope_matches(fields.get("scope"), path)
        ]
        if undeclared:
            errors.append(
                f"{binding.label}: {role} target {target!r} has missing source(s) not "
                f"declared Scope [new]: {', '.join(undeclared)}"
            )
    return errors


def canonical_acceptance_bindings(
    project_root: Path | str,
    bindings: Iterable[CriterionTarget],
) -> tuple[AcceptanceTargetBinding, ...]:
    """Resolve bindings to durable identities and current callable selectors."""
    root = Path(project_root)
    rows: set[AcceptanceTargetBinding] = set()
    for binding in bindings:
        baseline = select_target(root, binding.baseline)
        candidate = select_target(root, binding.target)
        rows.add(
            AcceptanceTargetBinding(
                flow=binding.flow,
                criterion=binding.key,
                baseline=baseline.identity,
                candidate=candidate.identity,
                baseline_selector=baseline.selector,
                candidate_selector=candidate.selector,
            )
        )
    return tuple(sorted(rows))


def validate_binding_selectors(
    project_root: Path | str, bindings: Iterable[AcceptanceTargetBinding]
) -> list[str]:
    """Require every persisted selector to resolve to its persisted identity."""
    root = Path(project_root)
    errors: list[str] = []
    for binding in bindings:
        for role, selector, identity in (
            ("baseline", binding.baseline_selector, binding.baseline),
            ("candidate", binding.candidate_selector, binding.candidate),
        ):
            try:
                resolved = select_target(root, selector)
            except (fusesoc_registry.FuseSocError, OSError, ValueError) as exc:
                errors.append(
                    f"{binding.criterion}: {role} selector {selector!r} cannot be resolved: {exc}"
                )
                continue
            if resolved.identity != identity:
                errors.append(
                    f"{binding.criterion}: {role} selector {selector!r} resolves to "
                    f"{resolved.identity!r}, expected {identity!r}"
                )
    return errors


def resolve_commit(repository: Path | str, sha: str) -> str:
    """Resolve and require an exact full commit SHA in *repository*."""
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"{sha}^{{commit}}"],
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    resolved = result.stdout.strip().lower()
    if result.returncode != 0 or resolved != sha.lower():
        detail = (result.stderr or result.stdout).strip()
        raise ValueError(f"commit {sha!r} does not resolve exactly: {detail}")
    return resolved
