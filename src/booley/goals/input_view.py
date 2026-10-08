"""One explicit committed RTL/Project view and classified non-versioned observations."""

from __future__ import annotations

import copy
import hashlib
import os
import tempfile
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

from booley.core.config_paths import resolve_toml
from booley.goals.checkout import CheckoutError, GoalCheckout
from booley.goals.committed_export import confined as _confined
from booley.goals.committed_export import (
    export_tree,
    materializations_unchanged,
    pinned_working_bytes,
    tracked_matches_pin,
)
from booley.goals.freshness import GoalFreshnessResolvers, evaluate_goal_freshness
from booley.goals.input_identity import (
    InputIdentityError,
    capture_roots,
    current_path,
    directory_identity,
    logical_roots,
    rebase_path,
    require_bindings,
    root_bindings,
    tests_label,
)
from booley.goals.lifecycle import LifecycleError
from booley.goals.model import PROJECT_SNAPSHOT_NAMES, GoalRecord
from booley.goals.proposals import digest
from booley.goals.protected_inputs import ProtectedInputRoots, ProtectedPath
from booley.goals.target_surface import target_surface_fingerprint
from booley.goals.waiver_policy import policy_location, waiver_policy_fingerprint
from booley.runtime.pinned_history import raw_git, tree_rows, validate_pin
from booley.runtime.project_dir import (
    committed_project_scope,
    contains,
    resolve_checkout_project_dir,
)
from booley.runtime.project_repositories import RepositoryCheckoutError, paired_project_repository
from booley.targets.catalog import TargetCatalog


@dataclass(frozen=True)
class InputSelection:
    """Participant pins, original bases and the exact Project mapping."""

    rtl: Path
    rtl_pin: str
    rtl_base: str
    project: Path
    project_repository: Path | None
    project_pin: str | None
    project_base: str | None
    topology: str

    def to_json(self) -> dict[str, Any]:
        """Durable proof of every participant and topology."""
        return {
            "roots": root_bindings(
                {
                    "rtl": self.rtl,
                    "project": self.project,
                    **(
                        {"paired": self.project_repository}
                        if self.project_repository is not None
                        else {}
                    ),
                }
            ),
            "rtl": str(self.rtl),
            "rtl_pin": self.rtl_pin,
            "rtl_base": self.rtl_base,
            "project": str(self.project),
            "project_repository": None
            if self.project_repository is None
            else str(self.project_repository),
            "project_pin": self.project_pin,
            "project_base": self.project_base,
            "topology": self.topology,
        }


@dataclass(frozen=True)
class GeneratedBuildInput:
    """Authorization from an exact selected producer's immutable planning disclosure."""

    path: Path
    size: int
    sha256: str
    provenance: dict[str, Any]


class InputSelectionError(LifecycleError):
    """Live participant topology cannot represent the recorded input selection."""


def select_inputs(record: GoalRecord, root: Path) -> InputSelection:
    """Classify participant availability separately from persisted artifact integrity."""
    try:
        return _select_inputs(record, root)
    except (InputIdentityError, CheckoutError, RepositoryCheckoutError, OSError) as exc:
        raise InputSelectionError(str(exc)) from exc


def _select_inputs(record: GoalRecord, root: Path) -> InputSelection:
    """Select the checkout-local Project, never the ambient/control Project."""
    project = resolve_checkout_project_dir(root).resolve()
    paired = paired_project_repository(root)
    project_repo = None if paired is None else paired.worktree.resolve()
    containing = GoalCheckout(project).containing_repository()
    if (
        containing is not None
        and not any(
            path is not None and containing[0].samefile(path) for path in (root, project_repo)
        )
        and _tracked_project(containing[0], project)
    ):
        raise InputSelectionError(
            "versioned Project is outside the pinned RTL/paired participants; use a paired Project checkout"
        )
    if (record.paired_project_base_sha is None) != (paired is None):
        raise InputSelectionError("paired Project topology differs from Goal entry")
    pin = GoalCheckout(root).head_sha()
    project_pin = None if project_repo is None else GoalCheckout(project_repo).head_sha()
    topology = topology_digest(root, project, project_repo)
    _require_selection_identity(record, root, project, project_repo)
    if (
        record.input_topology_digest is not None
        and record.input_topology_digest != "sha256:" + topology
    ):
        raise InputSelectionError(
            "input Project topology differs from the recorded entry identity"
        )
    if project_repo is not None and record.input_topology_digest is None:
        raise InputSelectionError(
            "legacy paired Project has no entry topology proof; abandon and re-enter before finish"
        )
    for repository, commit in ((root, pin), (project_repo, project_pin)):
        if repository is not None and commit is not None:
            validate_pin(repository, commit)
    return InputSelection(
        root,
        pin,
        record.base_sha,
        project,
        project_repo,
        project_pin,
        record.paired_project_base_sha,
        topology,
    )


def _require_selection_identity(
    record: GoalRecord, root: Path, project: Path, project_repo: Path | None
) -> None:
    selected_roots = root_bindings(
        {
            "rtl": root,
            "project": project,
            **({"paired": project_repo} if project_repo is not None else {}),
        }
    )
    if record.input_paths is not None:
        require_bindings({key: record.input_paths[key] for key in selected_roots}, selected_roots)


def _tracked_project(repository: Path, project: Path) -> bool:
    if repository.samefile(project):
        return True
    # Git proves the containing-repository suffix even when only this subtree is
    # mounted elsewhere and its namespace parents differ from the repository's.
    return bool(
        raw_git(
            project,
            "ls-tree",
            "-r",
            "-z",
            "--name-only",
            GoalCheckout(repository).head_sha(),
            "--",
            ".",
        )
    )


def topology_digest(root: Path, project: Path, paired: Path | None) -> str:
    """Physical directories and Git admin identities prevent topology substitution."""
    mapped = contains(project, project_dir=root)
    value: dict[str, Any] = {
        "rtl_directory": directory_identity(root),
        "project_directory": directory_identity(project),
        "project_relative": None
        if mapped is None
        else mapped.relative_to(root.resolve()).as_posix(),
    }
    for label, path in (("rtl", root), ("paired", paired)):
        if path is None:
            value[label + "_git"] = None
            continue
        identities: list[list[int]] = []
        for option in ("--git-common-dir", "--git-dir"):
            directory = Path(os.fsdecode(raw_git(path, "rev-parse", option).rstrip(b"\n")))
            directory = (path / directory) if not directory.is_absolute() else directory
            identities.append(directory_identity(directory))
        value[label + "_git"] = identities
    return digest(value)


def capture_path_roots(root: Path, control_project: Path) -> dict[str, Any]:
    return capture_roots(root, control_project)


def snapshot_project(root: Path) -> dict[str, str | None] | None:
    """Retain original nonversioned Project configuration for semantic base comparison."""
    project = resolve_checkout_project_dir(root).resolve()
    paired = paired_project_repository(root)
    selection = InputSelection(
        root,
        GoalCheckout(root).head_sha(),
        "",
        project,
        None if paired is None else paired.worktree.resolve(),
        None,
        None,
        "",
    )
    if _project_versioned(selection):
        return None
    result: dict[str, str | None] = {}
    for name in PROJECT_SNAPSHOT_NAMES:
        path = project / name
        result[name] = path.read_bytes().hex() if path.is_file() else None
    return result


def require_clean(
    selection: InputSelection,
    *,
    owned: Path | None = None,
    retained: frozenset[Path] = frozenset(),
    preserve_publication_staging: bool = False,
) -> None:
    """Both participants, exempting only an already-verified own summary file."""
    for root in (selection.rtl, selection.project_repository):
        if root is None:
            continue
        rows = raw_git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        paths = [(row[:3], root / os.fsdecode(row[3:])) for row in rows.split(b"\0") if row]
        pin = selection.rtl_pin if root == selection.rtl else selection.project_pin
        projection = None
        for status, path in paths:
            if path == owned or (status == b"?? " and path in retained):
                continue
            if (
                preserve_publication_staging
                and owned is not None
                and status[:1] != b" "
                and (owned.is_relative_to(path) or path.is_relative_to(owned))
            ):
                # The caller proved its exact published blob and literal working
                # file. Preserve competing index entries confined to that artifact.
                continue
            if status == b" M " and pin is not None:
                if projection is None:
                    projection = pinned_working_bytes(root, pin)
                if tracked_matches_pin(root, pin, path, projection=projection):
                    continue
            raise LifecycleError(f"commit changes in {root} before finishing")


@dataclass(frozen=True)
class CommittedView:
    """Materialized committed inputs and explicitly classified exceptions."""

    selection: InputSelection
    root: Path
    project: Path
    observations: list[dict[str, Any]]
    mappings: tuple[tuple[Path, Path], ...]
    saved_roots: Mapping[str, Any] | None = None
    current_roots: Mapping[str, Any] | None = None
    materializations: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    def mapped(self, path: Path) -> Path:
        """The innermost participant owns paths, including RTL nested in shared control."""
        if self.saved_roots is not None and self.current_roots is not None:
            path = rebase_path(path, self.saved_roots, self.current_roots)
        for original, materialized in sorted(
            self.mappings, key=lambda pair: len(pair[0].parts), reverse=True
        ):
            if path.is_relative_to(original):
                return materialized / path.relative_to(original)
            mapped = contains(path, project_dir=original)
            if mapped is not None:
                return materialized / mapped.relative_to(original.resolve())
        raise LifecycleError(f"unclassified consumed input outside pinned participants: {path}")

    def observe(self, path: Path, classification: str) -> Path:
        """Capture an exact permitted non-versioned file, with provenance."""
        link = str(path.readlink()) if path.is_symlink() else None
        if link is not None and classification not in {"ProtectedInput", "ProjectSnapshot"}:
            raise LifecycleError(f"non-versioned input is a symlink: {path}")
        target = _confined(self.mapped(path), self.root.parent, leaf_link=True)
        if target.is_symlink():
            target.unlink()
        if target.is_file() and link is None:
            return target
        content = path.read_bytes()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        self.observations.append(
            {
                "path": str(path),
                "classification": classification,
                "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": content.hex(),
                "symlink": link,
            }
        )
        return target


@contextmanager
def committed_view(
    selection: InputSelection, record: GoalRecord, *, control_project: Path, baseline: bool = False
) -> Generator[CommittedView]:
    """One pinned view for freshness, semantic diff and package; no ambient fallback."""
    with tempfile.TemporaryDirectory(prefix="booley-goal-inputs-") as scratch:
        root = Path(scratch) / "rtl"
        snapshot = (
            None
            if _project_versioned(selection)
            else (
                record.project_snapshot
                if baseline and record.project_snapshot is not None
                else snapshot_project(selection.rtl)
            )
        )
        project, materializations = _export_participants(
            selection, root, Path(scratch), baseline, snapshot=snapshot
        )
        if snapshot is not None:
            # The temporary selection snapshot is not itself a committed blob.
            # Remove it so the ordinary capture records exact live provenance;
            # baseline restoration and the equality check below keep selection fixed.
            _write_snapshot(project, Path(scratch), dict.fromkeys(snapshot))
        view = CommittedView(
            selection,
            root,
            project,
            [],
            ((selection.project, project), (selection.rtl, root)),
            record.input_paths,
            capture_path_roots(selection.rtl, control_project),
            materializations,
        )
        _capture_nonversioned(view, record, control_project)
        if baseline and record.project_snapshot is not None:
            _restore_snapshot(view, record.project_snapshot)
        if snapshot is not None and any(
            ((view.project / name).read_bytes().hex() if (view.project / name).is_file() else None)
            != content
            for name, content in snapshot.items()
        ):
            raise LifecycleError("Project snapshot changed while exporting selected submodules")
        with committed_project_scope(root, project):
            yield view


def _export_participants(
    selection: InputSelection,
    root: Path,
    scratch: Path,
    baseline: bool,
    *,
    snapshot: Mapping[str, str | None] | None = None,
) -> tuple[Path, list[dict[str, Any]]]:
    selected_project = contains(selection.project, project_dir=selection.rtl)
    project = (
        root / selected_project.relative_to(selection.rtl.resolve())
        if selected_project is not None
        else scratch / "project"
    )
    paired: list[dict[str, Any]] = []
    excluded: frozenset[str] = frozenset()
    config = None
    if selection.project_repository is not None:
        pin = selection.project_base if baseline else selection.project_pin
        assert pin is not None
        paired = export_tree(
            selection.project_repository, pin, project, scratch=scratch, baseline=baseline
        )
        config = resolve_toml(project)
        if selected_project is not None:
            excluded = frozenset(
                {selected_project.relative_to(selection.rtl.resolve()).as_posix()}
            )
    elif snapshot is not None:
        project.mkdir(parents=True, exist_ok=True)
        _write_snapshot(project, scratch, snapshot)
        config = resolve_toml(project)
    materializations = export_tree(
        selection.rtl,
        selection.rtl_base if baseline else selection.rtl_pin,
        root,
        scratch=scratch,
        baseline=baseline,
        excluded_top_level=excluded,
        project_config=config,
    )
    return project, materializations + paired


def _restore_snapshot(view: CommittedView, snapshot: Mapping[str, str | None]) -> None:
    _write_snapshot(view.project, view.root.parent, snapshot)


def _write_snapshot(project: Path, scratch: Path, snapshot: Mapping[str, str | None]) -> None:
    for name, text in snapshot.items():
        path = _confined(project / name, scratch, leaf_link=True)
        if path.is_symlink():
            path.unlink()
        if text is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(bytes.fromhex(text))


def _project_versioned(selection: InputSelection) -> bool:
    """A snapshot exception cannot hide a versioned Project anchor."""
    if selection.project.samefile(selection.rtl):
        return True
    project = contains(selection.project, project_dir=selection.rtl)
    return selection.project_repository is not None or (
        project is not None
        and any(
            name.startswith(
                os.fsencode(project.relative_to(selection.rtl.resolve()).as_posix()) + b"/"
            )
            for name in tree_rows(selection.rtl, selection.rtl_pin)
        )
    )


def _capture_nonversioned(view: CommittedView, record: GoalRecord, control_project: Path) -> None:
    """D7 inputs and checkout-local Project snapshots are explicit exceptions."""
    project = view.selection.project
    # Snapshot configuration is permitted only for a non-versioned Project.
    if not _project_versioned(view.selection):
        for name in PROJECT_SNAPSHOT_NAMES:
            path = project / name
            if path.is_file():
                view.observe(path, "ProjectSnapshot")
            else:
                view.observations.append(
                    {"path": str(path), "classification": "AbsentProjectSnapshot"}
                )
    roots = ProtectedInputRoots(view.selection.rtl, control_project).with_input_paths(
        record.input_paths
    )
    main = roots.main_checkout()
    for encoded in record.protected_paths:
        path = roots.absolute(ProtectedPath.decode(encoded), main)
        if path is None:
            continue
        if path.is_symlink() and not path.is_file():
            view.observations.append(
                {
                    "path": str(path),
                    "classification": "ProtectedDirectoryLink",
                    "symlink": str(path.readlink()),
                }
            )
        if path.is_file():
            _protected_observation(view, path)
        elif path.is_dir():
            for child in sorted(path.rglob("*")):
                if "__pycache__" not in child.relative_to(path).parts and child.is_file():
                    _protected_observation(view, child)
    _confined(view.project, view.root.parent).mkdir(parents=True, exist_ok=True)
    _capture_policy(view)


def _capture_policy(view: CommittedView) -> None:
    """A nonversioned Project policy is observed; versioned anchors require user commits."""
    config, anchor = policy_location(view.selection.rtl)
    if config is None:
        return
    directory = anchor / config.directory
    mapped = _confined(view.mapped(directory), view.root.parent)
    if config.anchor == "project_data_repository" and not _project_versioned(view.selection):
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                view.observe(path, "ApprovedWaiverPolicy")
    # Empty policy directories have no Git object; their existence is a classified observation.
    if directory.is_dir():
        mapped.mkdir(parents=True, exist_ok=True)
        view.observations.append(
            {
                "path": str(directory),
                "classification": "PolicyDirectory",
                "files": sorted(
                    p.relative_to(directory).as_posix()
                    for p in directory.rglob("*")
                    if p.is_file()
                ),
            }
        )


def _logical_surface(view: CommittedView, root: Path, target: str | None) -> dict[str, Any]:
    """Preserve logical labels in the conservative declaration hash."""
    return target_surface_fingerprint(
        root,
        target,
        logical_roots={new: _entry_label(view, old) for old, new in view.mappings},
        logical_tests=None if view.saved_roots is None else tests_label(view.saved_roots),
    )


def _entry_label(view: CommittedView, path: Path) -> Path:
    if view.saved_roots is None or view.current_roots is None:
        return path
    for current, saved in sorted(
        logical_roots(view.saved_roots, view.current_roots).items(),
        key=lambda pair: len(pair[0].parts),
        reverse=True,
    ):
        if path.is_relative_to(current):
            return saved / path.relative_to(current)
    return path


def alias_resolvers(
    record: GoalRecord,
    selection: InputSelection,
    control_project: Path,
    live: GoalFreshnessResolvers,
) -> GoalFreshnessResolvers:
    """Read current physical inputs while preserving the producer's entry labels."""
    if record.input_paths is None:
        return live
    saved = record.input_paths
    mappings = logical_roots(saved, capture_path_roots(selection.rtl, control_project))

    def surface(root: Path, target: str | None) -> dict[str, Any]:
        return target_surface_fingerprint(
            root, target, logical_roots=mappings, logical_tests=tests_label(saved)
        )

    def waiver(root: Path) -> dict[str, Any]:
        value = live.waiver_policy(root)
        if _role_waiver_label(value, saved, selection.rtl, selection.project):
            return value
        if "root" in value:
            path = Path(value["root"])
            for current, original in sorted(
                mappings.items(), key=lambda pair: len(pair[0].parts), reverse=True
            ):
                if path.is_relative_to(current):
                    value["root"] = str(original / path.relative_to(current))
                    value["digest"] = digest(
                        {key: val for key, val in value.items() if key != "digest"}
                    )
                    break
        return value

    return replace(
        live,
        target_surface=surface
        if live.target_surface is target_surface_fingerprint
        else live.target_surface,
        waiver_policy=waiver,
    )


def _role_waiver_label(
    value: dict[str, Any], saved: Mapping[str, Any], rtl: Path, project: Path
) -> bool:
    """Policy anchor roles retain distinct labels even when their roots coalesce."""
    if "root" not in value:
        return False
    role = "project" if value["config"]["anchor"] == "project_data_repository" else "rtl"
    current = project if role == "project" else rtl
    root = Path(value["root"])
    if not root.is_relative_to(current):
        return False
    value["root"] = str(Path(saved[role]["path"]) / root.relative_to(current))
    value["digest"] = digest({key: val for key, val in value.items() if key != "digest"})
    return True


def committed_resolvers(
    view: CommittedView, live: GoalFreshnessResolvers
) -> GoalFreshnessResolvers:
    """Same freshness rules, with a proven waiver-root mapping and no live readers."""

    def waiver(root: Path) -> dict[str, Any]:
        value = waiver_policy_fingerprint(root)
        if view.saved_roots is not None and _role_waiver_label(
            value, view.saved_roots, view.root, view.project
        ):
            return value
        if "root" in value:
            raw = Path(value["root"])
            for original, materialized in view.mappings:
                if raw.is_relative_to(materialized):
                    value["root"] = str(
                        _entry_label(view, original / raw.relative_to(materialized))
                    )
                    value["digest"] = digest(
                        {key: val for key, val in value.items() if key != "digest"}
                    )
                    break
        return value

    return GoalFreshnessResolvers(
        target_surface=lambda root, target: _logical_surface(view, root, target),
        waiver_policy=waiver,
        waiver_semantics=live.waiver_semantics,
    )


def validate_committed(
    view: CommittedView,
    record: GoalRecord,
    criteria: Mapping[str, Any],
    live: GoalFreshnessResolvers,
    generated: tuple[GeneratedBuildInput, ...] = (),
) -> None:
    """Prove consumed design files exist in the committed representation, then freshness."""
    resolvers = committed_resolvers(view, live)
    for goal in record.goals:
        target = goal.spec.target
        current = live.fingerprint(view.selection.rtl, target=target)
        for category in ("rtl", "tb", "target_surface", "workload"):
            for name in current.get(category, {}).get("files", []):
                path = Path(name)
                original = path if path.is_absolute() else view.selection.rtl / path
                mapped = view.mapped(original)
                if mapped.is_symlink() or not mapped.resolve().is_relative_to(view.root.parent):
                    raise LifecycleError(
                        f"consumed design input escapes committed view: {original}"
                    )
                if not mapped.is_file():
                    _materialize_generated(view, original, generated)
        _require_materialized_target(view, target)
        entry = copy.deepcopy(criteria[goal.spec.key])
        _map_review_inputs(view, entry.detail)
        freshness = evaluate_goal_freshness(
            goal.spec.key, entry, goal=goal.spec, work_dir=view.root, resolvers=resolvers
        )
        if freshness.stale:
            raise LifecycleError(
                f"committed evidence for {goal.spec.key} is stale: {freshness.reason}"
            )
    _require_versioned_policy(view)


def _require_materialized_target(view: CommittedView, target: str | None) -> None:
    """Authored absolute references cannot make committed freshness read live sources."""
    catalog = TargetCatalog.build(view.root)
    handles = catalog.list() if target is None else (catalog.select(target),)
    for handle in handles:
        for item in catalog.inspect(handle).inputs:
            path = _confined(view.root / item.path, view.root.parent)
            if path.is_symlink():
                raise LifecycleError(f"consumed design input is a materialized symlink: {path}")


def _materialize_generated(
    view: CommittedView, path: Path, authorized: tuple[GeneratedBuildInput, ...]
) -> None:
    """Generated build data is exceptional; HDL and declaration inputs remain committed."""
    identity = next(
        (
            item
            for item in authorized
            if current_path(item.path, view.current_roots or {})
            == current_path(path, view.current_roots or {})
        ),
        None,
    )
    if identity is None or path.suffix.lower() in {
        ".v",
        ".sv",
        ".vh",
        ".svh",
        ".vhd",
        ".vhdl",
        ".core",
        ".toml",
        ".py",
        ".sh",
        ".tcl",
        ".sdc",
        ".xdc",
    }:
        raise LifecycleError(
            f"consumed design input has no committed representation: {path}; commit it before finish"
        )
    if path.is_symlink() or not path.is_file():
        raise LifecycleError(f"classified generated input is unavailable: {path}")
    content = path.read_bytes()
    if (
        len(content) != identity.size
        or "sha256:" + hashlib.sha256(content).hexdigest() != identity.sha256
    ):
        raise LifecycleError(
            f"classified generated input differs from selected producer proof: {path}"
        )
    view.observe(path, "GeneratedBuildInput")
    view.observations[-1]["provenance"] = identity.provenance


def _map_review_inputs(view: CommittedView, detail: dict[str, Any]) -> None:
    contract = detail.get("contract")
    if not isinstance(contract, dict):
        return
    contract = cast("dict[str, Any]", contract)
    for name in ("spec", "ticket", "decisions"):
        text = contract.get(name + "_source")
        if not isinstance(text, str) or not text:
            continue
        original = Path(text)
        if view.saved_roots is not None and view.current_roots is not None:
            original = rebase_path(original, view.saved_roots, view.current_roots)
        if name == "spec":
            mapped = _confined(view.mapped(original), view.root.parent)
            if not mapped.is_file():
                raise LifecycleError(f"review spec has no committed representation: {original}")
        else:
            # Receipts are local producer observations, frozen independently of design source.
            if not original.is_file():
                continue
            if original.is_symlink():
                raise LifecycleError(f"producer receipt is a symlink: {original}")
            mapped = _confined(
                view.root.parent / "receipts" / hashlib.sha256(text.encode()).hexdigest(),
                view.root.parent,
            )
            mapped.parent.mkdir(parents=True, exist_ok=True)
            content = original.read_bytes()
            mapped.write_bytes(content)
            view.observations.append(
                {
                    "path": text,
                    "classification": "ProducerReceipt",
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "bytes": content.hex(),
                }
            )
        contract[name + "_source"] = str(mapped)


def _require_versioned_policy(view: CommittedView) -> None:
    config, anchor = policy_location(view.selection.rtl)
    if config is None:
        return
    for path in sorted((anchor / config.directory).rglob("*")):
        if path.is_file() and (
            not view.mapped(path).is_file() or view.mapped(path).read_bytes() != path.read_bytes()
        ):
            raise LifecycleError(f"approved waiver input must be committed by the user: {path}")


def _protected_observation(view: CommittedView, path: Path) -> None:
    if any(path.is_relative_to(original) for original, _ in view.mappings):
        view.observe(path, "ProtectedInput")
    else:
        content = path.read_bytes()
        view.observations.append(
            {
                "path": str(path),
                "classification": "ProtectedInput",
                "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": content.hex(),
                "symlink": str(path.readlink()) if path.is_symlink() else None,
            }
        )


def observations_unchanged(
    proof: dict[str, Any], current_roots: Mapping[str, Any] | None = None
) -> bool:
    """Recheck exact captured exceptions on every recovery and terminal boundary."""
    saved = proof.get("path_roots")
    if saved is not None and current_roots is not None:
        require_bindings(saved, current_roots)
    if current_roots is not None and not materializations_unchanged(proof, dict(current_roots)):
        return False
    return all(
        _observation_unchanged(row, saved, current_roots)
        for row in proof["nonversioned_observations"]
    )


def _observation_unchanged(
    row: dict[str, Any],
    saved: Mapping[str, Any] | None = None,
    current: Mapping[str, Any] | None = None,
) -> bool:
    path = Path(row["path"])
    if saved is not None and current is not None:
        path = rebase_path(path, saved, current)
    if path.is_symlink() != (row.get("symlink") is not None):
        return False
    if path.is_symlink() and str(path.readlink()) != row["symlink"]:
        return False
    if row["classification"] == "ProtectedDirectoryLink":
        return True  # D7's full recursive digest is checked alongside these observations.
    if row["classification"] == "AbsentProjectSnapshot":
        return not path.exists()
    if row["classification"] == "PolicyDirectory":
        return (
            path.is_dir()
            and sorted(p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file())
            == row["files"]
        )
    matches = path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == row["sha256"]
    return matches and (
        row["classification"] != "GeneratedBuildInput"
        or _producer_unchanged(row["provenance"], saved, current)
    )


def _producer_unchanged(
    provenance: dict[str, Any],
    saved: Mapping[str, Any] | None = None,
    current: Mapping[str, Any] | None = None,
) -> bool:
    manifest = Path(provenance["producer_manifest_path"])
    if saved is not None and current is not None:
        manifest = rebase_path(manifest, saved, current)
    return (
        not manifest.is_symlink()
        and manifest.is_file()
        and manifest.read_bytes().hex() == provenance["producer_manifest_bytes"]
    )
