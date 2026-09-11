"""fusesoc_registry.py — the Booley↔FuseSoC design-description seam (ADR 0022).

ADR 0022 makes FuseSoC the home for *design-description* — files, parameters,
top modules, build targets — and deletes Booley's equivalent registry
(``configs.toml`` design fields, ``shared_infra`` source globbing,
``project_config`` parameter typing). This module is the seam that replaces it.

It does exactly two things, on two different sides of the trust boundary:

  * **Enumeration** (:func:`enumerate_targets`) reads Target *names* straight
    from ``.core`` YAML — data Booley already owns and parses, so it crosses no
    trust boundary and need not scrape the brittle human-formatted
    ``fusesoc list-cores`` text (decision 6). A name may be declared by several
    cores (ADR 0030, retiring decision 10's global uniqueness); selection
    disambiguates via :func:`resolve_ref` — a bare name when unique, else a
    ``vlnv#name`` qualifier. These names power ``--target`` validation and
    per-config criteria expansion.

  * **Resolution** (:func:`resolve_target`) goes through the ``fusesoc run
    --setup`` CLI — the ``depends`` graph, fileset expansion, and parameter
    typing are FuseSoC's job, driven through the FuseSoC program and **never** the
    mid-migration Python API (decision 6). FuseSoC *generates* the EDAM; Booley
    only **reads** the resolved ``.eda.yml`` it leaves behind (decision 4,
    superseding the Booley-built EDAM of 0019 decision 3) and partitions files
    RTL-vs-TB by the surviving ``tags: [tb]`` marker (decision 13).

The organizing line: **list names from data you trust; resolve through the
FuseSoC program.** This module writes no EDAM-generation code (decision 3) — ``fusesoc run
--setup`` already runs Edalize's ``configure()``, leaving a ready-to-``make``
build dir next to the ``.eda.yml``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from fusesoc.capi2.exprs import Exprs

from booley.core.boundary import is_str_list
from booley.fusesoc.constants import TRACE_OVERLAY_MARKER
from booley.fusesoc.core_projection import (
    PROJECTED_CORE_PREFIX,
    CoreProjectionError,
    isolated_core_path,
    isolated_registry_root,
    native_cores_ignored,
    projected_core_path,
    projection_enabled,
    reconcile_isolated_registry,
    reconcile_projected_cores,
)
from booley.targets.domain import (
    CoreCollisionError,
    CoreSources,
    ForeignTargetHandleError,
    FuseSocError,
    MissingSourceError,
    StaleTargetCatalogError,
    TargetHandle,
    TargetRef,
    TargetResolutionError,
)
from booley.targets.selection import (
    resolve as _resolve_selector,
)
from booley.targets.selection import (
    split_selector as _split_qualifier,
)
from booley.targets.selection import (
    vlnv_key as _vlnv_key,
)

logger = logging.getLogger(__name__)

# The default fusesoc invocation. In the Sandbox the ``fusesoc`` console script
# is on PATH (pinned in the image, Phase 0); callers may override for tests or
# for an interpreter-relative invocation.
DEFAULT_FUSESOC_CMD: tuple[str, ...] = ("fusesoc",)

# FuseSoC's implicit fallback Target — CAPI2 plumbing, not a Booley config, so
# it is never enumerated as a selectable ``--target`` name (decision 10).
_IMPLICIT_TARGET = "default"

# Path components that mark a generated FuseSoC build/cache tree. ``.core`` files
# unearthed under these are resolution artifacts, not authored design-description.
_BUILD_DIR_NAMES = frozenset({"build", "_build", ".runtime"})

# Booley's own state directory. It holds transient checkouts — per-ticket git
# worktrees (``.booley_project/worktrees/<name>``) and ephemeral baseline
# worktrees (``.booley_project/.baseline-wt-*``) — each a full clone carrying a
# COPY of the project's ``.core`` files with the same VLNV. A stale copy left
# there shadows the repo-root core in FuseSoC's recursive ``--cores-root`` scan
# (silently building the worktree's RTL, not ``/work``'s) and collapses under
# Booley's own version-dedup (:func:`_enumerate_all`). With one exception
# (``cores/``, below), nothing authored lives under it (config is ``.toml``,
# hooks are ``.sh``/``.py``), so no ``.core`` below it is a source. ``booley init``
# also drops a ``FUSESOC_IGNORE`` marker here so FuseSoC's own scanner (out of
# Booley's code control) skips it too.
_STATE_DIR_NAME = ".booley_project"

# The one sanctioned home for authored cores INSIDE the state directory
# (ADR 0036). Stealth projects — where Booley usage must stay invisible to the
# host repo — cannot commit ``.core`` files to the main tree; they author them
# under ``.booley_project/cores/`` instead, versioned by the project dir's own
# git repo. :func:`discover_cores` scans it as a *second root* (never as an
# exception carved into the state-dir skip), and :func:`setup_command` hands it
# to FuseSoC as a second ``--cores-root``. FuseSoC's scanner checks
# ``FUSESOC_IGNORE`` only on directories it walks *into*, never on ancestors of
# a scan root, so the ``.booley_project/FUSESOC_IGNORE`` marker does not veto
# this subtree (verified against the pinned fusesoc 2.4.6 ``find_cores``).
STATE_CORES_SUBDIR = "cores"


# Filename infix Booley stamps onto a generated ``--trace`` overlay ``.core`` (see
# :func:`write_trace_overlay`). It is co-located with its base ``.core`` so its
# relative fileset paths resolve, so it *would* be discovered by the ``*.core``
# glob — but it is a transient build artifact, never an authored Target. Both
# :func:`discover_cores` (Booley's enumeration) skip it so a stray overlay (left
# behind by a crashed run) can never pollute ``--target`` validation or criteria
# expansion; FuseSoC itself still sees it during the single resolve that needs it.
@dataclass(frozen=True)
class CoreSetupHazard:
    """A statically detectable condition that makes FuseSoC setup unsafe."""

    kind: str
    path: Path
    detail: str


# ---------------------------------------------------------------------------
# .core enumeration — trusted, data-only, no CLI
# ---------------------------------------------------------------------------


_DOCTOR_FLOW_NAMES = frozenset({"sim", "lint", "synth", "fpga"})
_RETIRED_DOCTOR_FLOW_NAMES = frozenset({"elab", "elaborate"})


def core_target_eda_tool(core_doc: Mapping[str, Any], name: str) -> str | None:
    """Return the EDA tool a Target declares, read from ``.core`` YAML.

    Prefers the flow-API ``flow_options.tool`` (the authoring convention,
    Phase-0) and falls back to the upstream ``default_tool`` mirror. ``None`` when the
    Target is absent or declares neither.
    """
    targets = core_doc.get("targets") or {}
    if not isinstance(targets, Mapping):
        return None
    target = targets.get(name)
    if not isinstance(target, Mapping):
        return None
    flow_options = target.get("flow_options")
    if isinstance(flow_options, Mapping) and flow_options.get("tool"):
        return str(flow_options["tool"])
    default_eda_tool = target.get("default_tool")  # upstream FuseSoC CAPI2 field
    return str(default_eda_tool) if default_eda_tool else None


def core_target_flow(core_doc: Mapping[str, Any], name: str) -> str | None:
    """Return a Target's CAPI2 ``flow`` (``sim``/``lint``/``generic``…), or ``None``.

    The intent discriminator the EDA tool cannot give (``verilator`` backs both
    ``sim`` and ``lint``): a Target needs a testbench iff ``flow == 'sim'``.
    """
    targets = core_doc.get("targets") or {}
    if not isinstance(targets, Mapping):
        return None
    target = targets.get(name)
    if not isinstance(target, Mapping):
        return None
    flow = target.get("flow")
    return str(flow) if flow else None


def core_target_uses_legacy_fusesoc_api(core_doc: Mapping[str, Any], name: str) -> bool:
    """True when *name* uses the upstream pre-flow-API FuseSoC ``tools:`` field.

    CAPI2 has two ways to say "run verilator on this Target": the modern flow API
    (``flow: sim`` + ``flow_options: {tool: verilator}``) and the legacy pair
    upstream ``default_tool:`` + ``tools:`` fields. Booley reads ``flow`` as the intent
    discriminator — ``verilator`` backs both ``sim`` and ``lint``, so the EDA-tool
    name alone cannot say whether a testbench is expected. A legacy Target
    therefore enumerates as ``flow=None`` and is unclassifiable, which is common
    in the wild (OpenCores, older FuseSoC cores) and reads as a Booley bug rather
    than an authoring gap. Detect it so doctor can name the fix.
    """
    targets = core_doc.get("targets") or {}
    if not isinstance(targets, Mapping):
        return False
    target = targets.get(name)
    if not isinstance(target, Mapping):
        return False
    if target.get("flow"):
        return False
    # ``default_tool`` and ``tools`` are upstream FuseSoC CAPI2 field names.
    return bool(target.get("default_tool")) or isinstance(target.get("tools"), Mapping)


def core_target_toplevel(core_doc: Mapping[str, Any], name: str) -> str:
    """Return a Target's statically-declared ``toplevel``, read from ``.core`` YAML.

    The cheap, subprocess-free analog of :attr:`ResolvedTarget.toplevel`: a sim
    Target's ``toplevel`` is its TB top (decision 4). Most Targets declare it as a
    literal in the ``.core`` (only a ``depends``/parameter-derived top needs a full
    resolve), so a display/preview that needs the TB top can read it here without
    running ``fusesoc``. CAPI2 also allows a *list* of tops (common in upstream
    cores: ``toplevel: [testbench]``); that renders space-joined rather than as
    Python's ``['testbench']`` repr. ``""`` when the Target is absent or declares
    no toplevel.
    """
    targets = core_doc.get("targets") or {}
    if not isinstance(targets, Mapping):
        return ""
    target = targets.get(name)
    if not isinstance(target, Mapping):
        return ""
    toplevel = target.get("toplevel")
    if isinstance(toplevel, (list, tuple)):
        return " ".join(str(t) for t in toplevel)
    return str(toplevel) if toplevel else ""


def state_cores_dir(project_root: Path | str) -> Path:
    """The stealth authored-cores root, ``<root>/.booley_project/cores`` (ADR 0036).

    Purely structural — no env-var or config resolution — so
    :func:`discover_cores` stays a deterministic function of the tree it is
    handed. A relocated project dir (``BOOLEY_PROJECT_DIR``) is out of scope
    for stealth cores; ``booley doctor`` reports the mismatch.
    """
    return Path(project_root) / _STATE_DIR_NAME / STATE_CORES_SUBDIR


def discover_cores(project_root: Path | str) -> list[Path]:
    """Return every authored ``.core`` under *project_root*, sorted.

    Two roots are scanned: *project_root* itself, and — when it exists — the
    stealth authored-cores dir ``.booley_project/cores/`` (ADR 0036), the one
    subtree of the state dir that holds sources rather than transient state.

    Git metadata and generated build/cache trees (``build/``, ``.runtime/``)
    are skipped — a ``.core`` copied there by a prior resolution is an
    artifact, not a source.
    The rest of the ``.booley_project/`` state tree is skipped (:data:`_STATE_DIR_NAME`):
    its per-ticket / baseline git worktrees carry stale VLNV-colliding copies of
    the project's cores that must never shadow the authored source. Directories
    carrying a ``FUSESOC_IGNORE`` marker file are skipped as well, mirroring
    FuseSoC's own library-scanner convention, so vendored third-party cores
    (with their unresolvable deps and colliding Target names) stay out of the
    project's selectable Targets.
    """
    root = Path(project_root)
    cores = [] if native_cores_ignored(root) else _scan_core_root(root)
    stealth_root = state_cores_dir(root)
    if stealth_root.is_dir():
        # Second root, not an exception to the state-dir skip: the skip above
        # stays fail-closed for everything else under .booley_project/, and the
        # scan below judges exclusions relative to the stealth root (so a
        # FUSESOC_IGNORE *at or under* it is honored, matching FuseSoC).
        cores += _scan_core_root(stealth_root)
    return sorted(cores)


def target_snapshot_id(project_root: Path | str) -> str:
    """Content identity for one Project's authored Target and projection inputs."""
    root = Path(project_root).resolve()
    digest = hashlib.sha256(str(root).encode())
    paths = list(discover_cores(root))
    projection_config = root / _STATE_DIR_NAME / "booley.toml"
    if projection_config.is_file():
        paths.append(projection_config)
    for path in sorted(set(paths)):
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            relative = str(path)
        digest.update(relative.encode())
        try:
            digest.update(path.read_bytes())
        except OSError as exc:
            digest.update(f"unreadable:{type(exc).__name__}:{exc}".encode())
    return digest.hexdigest()


@dataclass(frozen=True)
class CoreLibraryPlan:
    """Prepared FuseSoC library roots and canonical-to-operational core paths."""

    roots: tuple[Path, ...]
    ignored_dirs: tuple[frozenset[str], ...]
    _operational_cores: tuple[tuple[Path, Path], ...]

    def operational_core(self, authored_core: Path | str) -> Path:
        """Return the prepared operational view of one canonical authored core."""
        authored = Path(authored_core).resolve()
        for canonical, operational in self._operational_cores:
            if canonical == authored:
                return operational
        raise FuseSocError(f"core is outside the prepared FuseSoC library plan: {authored}")


def _core_library_ignored_dirs(root: Path) -> frozenset[str]:
    """Return real paths CoreManager must prune from the Project-root library."""
    excluded_roots: set[Path] = set()
    symlink_targets: set[Path] = set()
    for current, dirnames, _filenames in os.walk(root, followlinks=False):
        directory = Path(current)
        retained: list[str] = []
        for name in dirnames:
            candidate = directory / name
            resolved = Path(os.path.realpath(candidate))
            if name == _STATE_DIR_NAME or name in _BUILD_DIR_NAMES:
                excluded_roots.add(resolved)
                continue
            retained.append(name)
            if candidate.is_symlink():
                symlink_targets.add(resolved)
        dirnames[:] = retained
    ignored = excluded_roots | {
        target
        for target in symlink_targets
        if any(target.is_relative_to(excluded) for excluded in excluded_roots)
    }
    return frozenset(str(path) for path in ignored)


def prepare_core_library_plan(project_root: Path | str) -> CoreLibraryPlan:
    """Reconcile and describe the FuseSoC library view for one Project."""
    root = Path(project_root).resolve()
    cores = tuple(path.resolve() for path in discover_cores(root))
    stealth_root = state_cores_dir(root).resolve()
    if projection_enabled(root):
        reconcile_projected_cores(root)
    if native_cores_ignored(root):
        reconcile_isolated_registry(root)
        library_roots = (isolated_registry_root(root).resolve(),)
        operational = tuple((core, isolated_core_path(root, core).resolve()) for core in cores)
    elif projection_enabled(root):
        library_roots = (root,)
        operational = tuple(
            (
                core,
                projected_core_path(root, core).resolve()
                if core.is_relative_to(stealth_root)
                else core,
            )
            for core in cores
        )
    else:
        library_roots = (root, stealth_root) if stealth_root.is_dir() else (root,)
        operational = tuple((core, core) for core in cores)
    ignored = tuple(
        _core_library_ignored_dirs(root) if library_root == root else frozenset()
        for library_root in library_roots
    )
    return CoreLibraryPlan(library_roots, ignored, operational)


def core_setup_hazards(project_root: Path | str) -> list[CoreSetupHazard]:
    """Return offline ``provider:`` and recursive-symlink setup hazards.

    FuseSoC follows source-tree directory symlinks while scanning core roots.
    A link back to its own ancestor therefore makes setup recurse forever.
    Walking with ``followlinks=False`` lets Booley detect that shape without
    entering it. Published cores with a top-level ``provider:`` are reported
    separately because selecting one asks FuseSoC to fetch remote sources even
    when the checkout already contains them.
    """
    root = Path(project_root)
    hazards = (
        [] if native_cores_ignored(root) else _recursive_symlink_hazards(root, skip_state_dir=True)
    )
    stealth_root = state_cores_dir(root)
    if stealth_root.is_dir():
        hazards.extend(_recursive_symlink_hazards(stealth_root, skip_state_dir=False))
    for core_file in discover_cores(root):
        try:
            provider = read_core(core_file).get("provider")
        except FuseSocError:
            continue  # the schema/readability audit owns malformed cores
        if provider:
            hazards.append(
                CoreSetupHazard(
                    kind="provider",
                    path=core_file,
                    detail="top-level provider block requests a remote source fetch",
                )
            )
    return sorted(hazards, key=lambda h: (h.kind, str(h.path)))


def _recursive_symlink_hazards(scan_root: Path, *, skip_state_dir: bool) -> list[CoreSetupHazard]:
    """Find directory links that resolve to their own ancestor."""
    hazards: list[CoreSetupHazard] = []
    excluded = {".git", *_BUILD_DIR_NAMES}
    if skip_state_dir:
        excluded.add(_STATE_DIR_NAME)
    for current, dirnames, filenames in os.walk(scan_root, followlinks=False):
        directory = Path(current)
        if (directory / "FUSESOC_IGNORE").is_file():
            dirnames.clear()
            continue
        dirnames[:] = [name for name in dirnames if name not in excluded]
        for name in (*dirnames, *filenames):
            link = directory / name
            if not link.is_symlink():
                continue
            try:
                target = link.resolve(strict=True)
            except RuntimeError:
                hazards.append(
                    CoreSetupHazard(
                        kind="recursive-symlink",
                        path=link,
                        detail="symlink chain contains a cycle",
                    )
                )
                continue
            except OSError:
                continue  # broken/non-readable links are not recursion hazards
            if not target.is_dir():
                continue
            if directory == target or directory.is_relative_to(target):
                hazards.append(
                    CoreSetupHazard(
                        kind="recursive-symlink",
                        path=link,
                        detail=f"directory link resolves to ancestor {target}",
                    )
                )
        # ``followlinks=False`` is the primary guard; removing links from the
        # pending names makes that non-recursion explicit and future-proof.
        dirnames[:] = [name for name in dirnames if not (directory / name).is_symlink()]
    return hazards


def _scan_core_root(root: Path) -> list[Path]:
    """Collect authored ``.core`` files under one scan root (unsorted)."""
    cores: list[Path] = []
    excluded = {".git", _STATE_DIR_NAME, *_BUILD_DIR_NAMES}
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        directory = Path(current)
        if "FUSESOC_IGNORE" in filenames:
            dirnames.clear()
            continue
        # Prune before descent so mutable Git object directories cannot race
        # discovery. Exclusions are relative to this scan root, preserving the
        # F-13 rule for Project roots that are themselves ticket worktrees.
        dirnames[:] = [name for name in dirnames if name not in excluded]
        for name in filenames:
            if not name.endswith(".core"):
                continue
            if name.startswith(PROJECTED_CORE_PREFIX):
                continue  # generated view; authoritative copy is scanned separately
            if TRACE_OVERLAY_MARKER in name:  # transient trace overlay, not a source
                continue
            cores.append(directory / name)
    return cores


def read_core(core_file: Path | str) -> dict[str, Any]:
    """Parse a ``.core`` (CAPI2) into a dict.

    A ``.core`` is YAML with a leading ``CAPI=2:`` marker line; ``safe_load``
    parses it as an ordinary mapping (the marker becomes a harmless ``CAPI=2``
    key), so no special stripping is needed.
    """
    path = Path(core_file)
    try:
        with path.open("r", encoding="utf-8") as f:
            doc = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as exc:
        raise FuseSocError(f"could not read .core {path}: {exc}") from exc
    if not isinstance(doc, Mapping):
        raise FuseSocError(f".core {path} is not a YAML mapping")
    return dict(doc)


# CAPI2 fields FuseSoC's schema requires to be YAML sequences (arrays). Booley's
# own reader (``yaml.safe_load``) is more permissive than the FuseSoC pinned in
# the sandbox image, so a scalar — or a bare ``depend:`` that parses to ``None`` —
# where an array belongs passes Booley's cheap host-side audit and only explodes
# at ``--deep`` resolution with FuseSoC's ``... must be array`` verdict. These
# tables let the cheap pass reproduce that verdict host-side (no subprocess).
_CAPI2_FILESET_ARRAY_FIELDS = ("files", "depend", "tags")
# ``filesets_append`` MUST stay in this table: target_fileset_names() splats it
# (``[*td.get("filesets_append")]``), so a malformed scalar such as
# ``filesets_append: rtl`` that slipped past the audit would explode into
# per-character fileset names ["r", "t", "l"]. The golden schema test pins
# every key that reader consumes to an entry here.
_CAPI2_TARGET_ARRAY_FIELDS = ("filesets", "filesets_append", "depend", "parameters", "flags")

# The ONLY per-file attribute keys FuseSoC's CAPI2 schema permits inside a
# ``{path: {attrs}}`` file entry. Verbatim from fusesoc 2.4.6's authoritative
# json_schema.py (the ``files`` ``$def`` — ``additionalProperties: false``), so
# any other key makes real fusesoc reject the whole core at schema validation.
# Booley's tolerant ``yaml.safe_load`` swallows a stray key (e.g. ``vendored``)
# and greenlights a core that only explodes under ``--deep``; this set lets the
# cheap host-side pass reproduce fusesoc's ``additionalProperties`` verdict.
_CAPI2_FILE_ATTR_KEYS = frozenset(
    ("define", "is_include_file", "include_path", "file_type", "logical_name", "tags", "copyto")
)


def _check_file_attr_keys(fileset: Mapping[str, Any], fs_label: str, errors: list[str]) -> None:
    """Append an error for every non-CAPI2 per-file attribute key in a fileset.

    A file entry may be a bare path string (no attrs) or a single-key
    ``{path: {attrs}}`` map; only the map form can carry attributes. FuseSoC's
    schema pins those attribute keys with ``additionalProperties: false``
    (:data:`_CAPI2_FILE_ATTR_KEYS`), so a stray key such as ``vendored`` makes
    real fusesoc drop the entire core. Reproduce that host-side.
    """
    files = fileset.get("files")
    if not isinstance(files, list):
        return  # non-list ``files`` is already flagged by the array-field check
    for entry in files:
        if not isinstance(entry, Mapping) or len(entry) != 1:
            continue  # bare-string path, or a malformed entry other checks own
        ((path, attrs),) = entry.items()
        if not isinstance(attrs, Mapping):
            continue
        for key in attrs:
            if key not in _CAPI2_FILE_ATTR_KEYS:
                errors.append(f"{fs_label}.files[{path}].{key} is not a valid CAPI2 per-file key")


def _check_booley_target_metadata(
    target: Mapping[str, Any], target_label: str, errors: list[str]
) -> None:
    """Validate Booley's namespaced metadata inside one CAPI2 Target."""
    flow_options = target.get("flow_options")
    if not isinstance(flow_options, Mapping) or "booley" not in flow_options:
        return
    booley = flow_options["booley"]
    label = f"{target_label}.flow_options.booley"
    if not isinstance(booley, Mapping):
        errors.append(f"{label} must be a mapping")
        return
    for key in sorted(set(booley) - {"coverage", "doctor", "doctor_selftest"}):
        errors.append(f"{label}.{key} is not a supported Booley Target key")
    if "doctor" in booley:
        doctor = booley["doctor"]
        if not isinstance(doctor, list):
            errors.append(f"{label}.doctor must be an array")
        else:
            retired = [flow for flow in doctor if flow in _RETIRED_DOCTOR_FLOW_NAMES]
            if retired:
                errors.append(
                    f"{label}.doctor contains retired Flow value(s) {retired!r}; "
                    "Elaboration Check is now Simulation mode, so replace "
                    "doctor: [sim, elab] with doctor: [sim]"
                )
            invalid = [
                flow
                for flow in doctor
                if not isinstance(flow, str)
                or (flow not in _DOCTOR_FLOW_NAMES and flow not in _RETIRED_DOCTOR_FLOW_NAMES)
            ]
            if invalid:
                allowed = ", ".join(sorted(_DOCTOR_FLOW_NAMES))
                errors.append(
                    f"{label}.doctor contains invalid Flow values {invalid!r}; "
                    f"choose from {allowed}"
                )
            if len({str(flow) for flow in doctor}) != len(doctor):
                errors.append(f"{label}.doctor must not contain duplicates")
    if "doctor_selftest" in booley and not isinstance(booley["doctor_selftest"], bool):
        errors.append(f"{label}.doctor_selftest must be a boolean")
    if "coverage" in booley:
        _check_coverage_target_metadata(booley["coverage"], label, errors)


def _check_coverage_target_metadata(value: Any, label: str, errors: list[str]) -> None:
    """Validate the private Phase-3 coverage recipe on one Simulation Target."""
    coverage_label = f"{label}.coverage"
    if not isinstance(value, Mapping):
        errors.append(f"{coverage_label} must be a mapping")
        return
    for key in sorted(set(value) - {"custom_main_hooks", "reset_included"}):
        errors.append(f"{coverage_label}.{key} is not a supported coverage key")
    if "reset_included" in value and not isinstance(value["reset_included"], bool):
        errors.append(f"{coverage_label}.reset_included must be a boolean")
    if "custom_main_hooks" not in value:
        return
    hooks = value["custom_main_hooks"]
    if not isinstance(hooks, list):
        errors.append(f"{coverage_label}.custom_main_hooks must be an array")
        return
    if not is_str_list(hooks):
        errors.append(f"{coverage_label}.custom_main_hooks must contain only strings")
        return
    invalid = [hook for hook in hooks if hook not in {"start_hook", "write_hook"}]
    if invalid:
        errors.append(f"{coverage_label}.custom_main_hooks contains unknown hook(s) {invalid!r}")
    if len({str(hook) for hook in hooks}) != len(hooks):
        errors.append(f"{coverage_label}.custom_main_hooks must not contain duplicates")


def core_schema_errors(core_file: Path | str) -> list[str]:
    """Return the CAPI2 array-field schema violations in a single ``.core``.

    A host-side subset of FuseSoC's capi2 schema, focused on the ``must be array``
    class of error that Booley's tolerant ``yaml.safe_load`` silently accepts but
    the pinned FuseSoC rejects at resolution (decision 6). Messages mirror
    FuseSoC's own phrasing (``filesets.<name>.depend must be array``) so the cheap
    pass fails with the same reason the deep resolve would. Empty list = clean.
    """
    path = Path(core_file)
    try:
        doc = read_core(path)
    except FuseSocError as exc:
        # An unreadable / non-mapping .core is itself a schema violation to report.
        return [str(exc)]

    errors: list[str] = []

    def _check_array_fields(container: Any, container_label: str, fields: tuple[str, ...]) -> None:
        for field_name in fields:
            # Absent field is fine; a present field that isn't a YAML list is not.
            # ``depend:`` with no value parses to ``None`` — FuseSoC still rejects it.
            if field_name in container and not isinstance(container[field_name], list):
                errors.append(f"{container_label}.{field_name} must be array")

    filesets = doc.get("filesets")
    if filesets is not None and not isinstance(filesets, Mapping):
        errors.append("filesets must be a mapping")
    elif isinstance(filesets, Mapping):
        for fs_name, fs in filesets.items():
            if not isinstance(fs, Mapping):
                errors.append(f"filesets.{fs_name} must be a mapping")
                continue
            _check_array_fields(fs, f"filesets.{fs_name}", _CAPI2_FILESET_ARRAY_FIELDS)
            _check_file_attr_keys(fs, f"filesets.{fs_name}", errors)

    targets = doc.get("targets")
    if targets is not None and not isinstance(targets, Mapping):
        errors.append("targets must be a mapping")
    elif isinstance(targets, Mapping):
        for tg_name, tg in targets.items():
            if not isinstance(tg, Mapping):
                errors.append(f"targets.{tg_name} must be a mapping")
                continue
            _check_array_fields(tg, f"targets.{tg_name}", _CAPI2_TARGET_ARRAY_FIELDS)
            _check_booley_target_metadata(tg, f"targets.{tg_name}", errors)

        selectable = core_target_names(doc)
        selftests = [name for name in selectable if core_target_is_doctor_selftest(doc, name)]
        public = [name for name in selectable if name not in selftests]
        if selftests and public:
            errors.append(
                "Doctor self-test Targets "
                f"({', '.join(selftests)}) must live in a dedicated .core without "
                f"public Targets ({', '.join(public)})"
            )

    return errors


def core_target_flow_option(core_doc: Mapping[str, Any], name: str, option: str) -> Any:
    """Return a Target's ``flow_options.<option>`` value, or ``None``.

    Reads the flow-API ``flow_options`` mapping the same way :func:`core_target_eda_tool`
    reads ``flow_options.tool`` — used to check host-side that mandatory edalize
    plumbing (e.g. Yosys's required ``arch``) is present without a full resolve.
    """
    targets = core_doc.get("targets") or {}
    if not isinstance(targets, Mapping):
        return None
    target = targets.get(name)
    if not isinstance(target, Mapping):
        return None
    flow_options = target.get("flow_options")
    if isinstance(flow_options, Mapping):
        return flow_options.get(option)
    return None


def core_target_doctor_flows(core_doc: Mapping[str, Any], name: str) -> tuple[str, ...]:
    """Return the validated Doctor Flow list declared by one Target.

    Invalid metadata is returned as no selection; :func:`core_schema_errors`
    owns the actionable boundary error so cheap enumeration can still present
    every healthy Target in a core containing one malformed declaration.
    """
    booley = core_target_flow_option(core_doc, name, "booley")
    if not isinstance(booley, Mapping):
        return ()
    doctor = booley.get("doctor")
    if not isinstance(doctor, list):
        return ()
    if any(not isinstance(flow, str) or flow not in _DOCTOR_FLOW_NAMES for flow in doctor):
        return ()
    if len(set(doctor)) != len(doctor):
        return ()
    return tuple(doctor)


def core_target_is_doctor_selftest(core_doc: Mapping[str, Any], name: str) -> bool:
    """Return whether one Target is reserved for Doctor's fail-path proof."""
    booley = core_target_flow_option(core_doc, name, "booley")
    return isinstance(booley, Mapping) and booley.get("doctor_selftest") is True


def core_target_names(core_doc: Mapping[str, Any]) -> list[str]:
    """Return the selectable Target names declared by a parsed ``.core``.

    The implicit ``default`` Target is excluded — it is FuseSoC's resolution
    fallback, not a Booley config (decision 10).
    """
    targets = core_doc.get("targets") or {}
    if not isinstance(targets, Mapping):
        return []
    return [name for name in targets if name != _IMPLICIT_TARGET]


def core_identity_key(core_doc: Mapping[str, Any]) -> str:
    """The parsed core's version-independent ``vendor:library:name`` identity.

    ``""`` when the ``.core`` declares no usable ``name:`` — a malformed core is
    FuseSoC's to reject at resolve, not this reader's.
    """
    vlnv = core_doc.get("name")
    return _vlnv_key(vlnv) if isinstance(vlnv, str) and vlnv else ""


def _enumerate_all(project_root: Path | str) -> dict[str, list[TargetRef]]:
    """Map every selectable Target name to *all* cores that declare it (ADR 0030).

    Reads ``.core`` YAML directly (decision 6) — no CLI, no trust boundary. A
    name declared by several *distinct* cores is legal and expected in a
    multi-core FuseSoC repo (``lint`` on 54 cores in ibex): every declaring core
    is kept, ordered by :func:`discover_cores` (deterministic). Two *versions* of
    the same logical core (identical ``vendor:library:name``) collapse to the
    first discovered — FuseSoC resolves the version at run time. The implicit
    ``default`` Target is not enumerated. Never raises on a name clash: global
    uniqueness (ADR 0022 dec 10) is retired; disambiguation happens at selection
    (:func:`resolve_ref`), not enumeration.

    One clash IS fatal: the same logical VLNV authored both in the repo tree
    and in ``.booley_project/cores/`` raises :class:`CoreCollisionError` (ADR
    0036) — cross-root shadowing must never resolve silently.
    """
    root = Path(project_root)
    refs: dict[str, list[TargetRef]] = {}
    # vlnv key -> zone ('repo'|'state') -> first declaring core file. Same-zone
    # duplicates stay legal (two *versions* of one core); cross-zone is fatal.
    zones: dict[str, dict[str, Path]] = {}
    for core_file in discover_cores(root):
        try:
            doc = read_core(core_file)
        except FuseSocError as exc:
            # Enumeration powers selection menus and qualified lookups. One
            # unrelated malformed core must not make every valid Target
            # undiscoverable; Doctor's structural audit still reports it.
            logger.warning("skipping unreadable core during Target enumeration: %s", exc)
            continue
        vlnv = doc.get("name")
        if not isinstance(vlnv, str) or not vlnv:
            logger.debug("skipping core without a name: %s", core_file)
            continue
        zone = "state" if _STATE_DIR_NAME in core_file.relative_to(root).parts else "repo"
        seen = zones.setdefault(_vlnv_key(vlnv), {})
        other = seen.get("repo" if zone == "state" else "state")
        if other is not None:
            repo_file, state_file = (other, core_file) if zone == "state" else (core_file, other)
            raise CoreCollisionError(
                f"core VLNV {_vlnv_key(vlnv)!r} is authored in both core roots: "
                f"{repo_file} (repo tree) and {state_file} (.booley_project/cores/); "
                "rename or delete one — cross-root precedence is never silent (ADR 0036)"
            )
        seen.setdefault(zone, core_file)
        for name in core_target_names(doc):
            bucket = refs.setdefault(name, [])
            if any(_vlnv_key(r.vlnv) == _vlnv_key(vlnv) for r in bucket):
                continue  # same logical core, another version — first wins
            cocotb_module = core_target_flow_option(doc, name, "cocotb_module")
            bucket.append(
                TargetRef(
                    name=name,
                    vlnv=vlnv,
                    core_file=core_file,
                    eda_tool=core_target_eda_tool(doc, name),
                    flow=core_target_flow(doc, name),
                    cocotb_module=str(cocotb_module) if cocotb_module else None,
                    doctor_flows=core_target_doctor_flows(doc, name),
                    doctor_selftest=core_target_is_doctor_selftest(doc, name),
                )
            )
    return refs


def _target_declarations(project_root: Path | str) -> dict[str, list[TargetRef]]:
    """Map every selectable Target name to ALL cores that declare it (ADR 0030).

    The multi-core companion of :func:`enumerate_targets`: instead of the
    first-wins direct-lookup view, every declaring core's ref is kept, in
    :func:`discover_cores` order — the view a per-core listing (``booley
    targets``) needs to show *why* a bare name is ambiguous. Same failure mode
    as enumeration: a cross-root VLNV collision raises
    :class:`CoreCollisionError`.
    """
    return {name: list(bucket) for name, bucket in _enumerate_all(project_root).items()}


def _resolve_ref(project_root: Path | str, token: str) -> TargetRef:
    """Resolve a ``--target`` token to the one core that declares it (ADR 0030).

    *token* is either a bare Target name — which must be declared by exactly one
    core — or a ``vlnv#name`` qualifier whose VLNV may be shortened to any
    unambiguous segment-suffix (``ibex_top#lint``). Raises
    :class:`~booley.targets.domain.UnknownTargetError` when the name (or the
    qualified core) does not exist, and
    :class:`~booley.targets.domain.AmbiguousTargetError` when a bare name — or a too-short
    VLNV qualifier — matches more than one core, naming the candidates so the
    caller can qualify further. This low-level view includes Doctor self-tests.
    """
    return _resolve_selector(_enumerate_all(project_root), token)


def _resolve_public_ref(project_root: Path | str, token: str) -> TargetRef:
    """Resolve one user-selectable Target without exposing Doctor self-tests."""
    declarations = {
        name: [ref for ref in refs if not ref.doctor_selftest]
        for name, refs in _enumerate_all(project_root).items()
    }
    return _resolve_selector({name: refs for name, refs in declarations.items() if refs}, token)


# ---------------------------------------------------------------------------
# Selectable-target dependency closure — scopes host-side ``.core`` audits to
# the cores the project actually drives (SETUP-19). On a 208-core FuseSoC
# monorepo the security/provenance audit would otherwise fold in every
# unselectable core's scripts/paths and FAIL doctor on cores that can never be
# a selectable Target. No ``[fusesoc]`` scope configured → ``None`` (audit every
# core, the historical single-core behavior).
# ---------------------------------------------------------------------------


def _depend_keys(entries: Any) -> set[str]:
    """VLNV identity keys named by a CAPI2 ``depend:`` list.

    A ``depend`` entry may carry a version and a comparison operator
    (``vendor:lib:name``, ``vendor:lib:name:1.2``, ``(>=vendor:lib:name:1.0)``);
    the closure matches on the version-independent ``vendor:library:name``
    identity (:func:`_vlnv_key`), so parentheses, a leading operator, and the
    version segment are stripped before keying. Non-string / non-list input
    yields no keys — a malformed ``depend`` is FuseSoC's to reject at resolve.
    """
    keys: set[str] = set()
    if not isinstance(entries, (list, tuple)):
        return keys
    for entry in entries:
        if not isinstance(entry, str):
            continue
        for possible in _possible_expression_values(entry):
            token = possible.strip().strip("()").lstrip("<>=~^! ").strip()
            if token:
                keys.add(_vlnv_key(token))
    return keys


def _target_depend_keys(core_doc: Mapping[str, Any], target: str) -> set[str]:
    """Depend-VLNV keys a single Target pulls in.

    Its own ``depend`` plus the ``depend`` of each fileset it references
    (``filesets`` union ``filesets_append``, via :func:`target_fileset_names`, so a
    YAML-anchor ``filesets_append`` dependency is not silently dropped).
    """
    targets = core_doc.get("targets") or {}
    target_def = targets.get(target) if isinstance(targets, Mapping) else None
    if not isinstance(target_def, Mapping):
        return set()
    keys = _depend_keys(target_def.get("depend"))
    filesets = core_doc.get("filesets") or {}
    for fs_name in _possible_fileset_names(target_def):
        fileset = filesets.get(fs_name) if isinstance(filesets, Mapping) else None
        if isinstance(fileset, Mapping):
            keys |= _depend_keys(fileset.get("depend"))
    return keys


def _core_depend_keys(core_doc: Mapping[str, Any]) -> set[str]:
    """Every depend-VLNV key a core declares (all targets + all filesets).

    A core reached *transitively* is pulled in generically — a ``depend`` selects
    the whole core, not one Target — so its full depend surface is walked. (A
    *root* core, by contrast, seeds the closure from only its selectable Targets'
    depends, so its other, unselectable Targets never widen the audit scope.)
    """
    keys: set[str] = set()
    targets = core_doc.get("targets")
    if isinstance(targets, Mapping):
        for target_def in targets.values():
            if isinstance(target_def, Mapping):
                keys |= _depend_keys(target_def.get("depend"))
    filesets = core_doc.get("filesets")
    if isinstance(filesets, Mapping):
        for fileset in filesets.values():
            if isinstance(fileset, Mapping):
                keys |= _depend_keys(fileset.get("depend"))
    return keys


def depended_on_core_keys(project_root: Path | str) -> set[str]:
    """Every core identity some *other* discovered ``.core`` names in a ``depend:``.

    A core reached only as a dependency is built through its ``default`` Target
    (FuseSoC's ``_get_target`` falls back to ``"default"`` for any core that is
    not the toplevel) — and a dependency core with no ``default`` contributes
    *zero* filesets, silently. So "this core has dependents" is exactly the
    condition under which a ``default`` Target is load-bearing rather than dead
    weight; doctor uses it to decide whether to flag one.

    Unparseable cores are skipped: their depends are unknowable, and treating
    that as "no dependents" would only ever produce a softer finding.
    """
    keys: set[str] = set()
    for core_file in discover_cores(project_root):
        try:
            doc = read_core(core_file)
        except FuseSocError:
            continue
        keys |= _core_depend_keys(doc)
    return keys


def _index_core_documents(
    project_root: Path,
    documents: Mapping[Path, Mapping[str, Any]] | None = None,
) -> tuple[dict[Path, Mapping[str, Any]], dict[str, list[Path]]]:
    """Capture/index core documents, or index a catalog's frozen capture."""
    by_key: dict[str, list[Path]] = {}
    docs: dict[Path, Mapping[str, Any]] = {}
    source_documents = documents
    if source_documents is None:
        captured: dict[Path, Mapping[str, Any]] = {}
        for core_file in discover_cores(project_root):
            try:
                captured[core_file.resolve()] = read_core(core_file)
            except FuseSocError:
                continue
        source_documents = captured
    for core_file, doc in source_documents.items():
        resolved_core_file = core_file.resolve()
        docs[resolved_core_file] = doc
        vlnv = doc.get("name")
        if isinstance(vlnv, str) and vlnv:
            by_key.setdefault(_vlnv_key(vlnv), []).append(resolved_core_file)
    return docs, by_key


def _selectable_core_closure_for_refs(
    project_root: Path,
    seed_refs: Collection[TargetRef | TargetHandle],
    *,
    documents: Mapping[Path, Mapping[str, Any]] | None = None,
) -> frozenset[Path]:
    """Walk dependency cores from references already selected by an adapter.

    A catalog supplies its frozen *documents*. Legacy registry-internal callers
    omit them and get one live document capture without any token selection.
    """
    docs, by_key = _index_core_documents(project_root, documents)

    # Seed the closure with the declaring core of each seeded Target; the first
    # frontier is only those Targets' depends (precise per-Target — a root core's
    # *other*, unselectable Targets must not widen the closure).
    closure: set[Path] = set()
    frontier: set[str] = set()
    for ref in seed_refs:
        closure.add(ref.core_file)
        doc = docs.get(ref.core_file)
        if doc is not None:
            frontier |= _target_depend_keys(doc, ref.name)

    # BFS the depend graph. Visited-tracking is by core file, so a key whose cores
    # are all already in the closure contributes nothing — the walk terminates
    # even on a cyclic depend graph.
    while frontier:
        next_frontier: set[str] = set()
        for key in frontier:
            for core_file in by_key.get(key, ()):
                if core_file in closure:
                    continue
                closure.add(core_file)
                next_frontier |= _core_depend_keys(docs.get(core_file, {}))
        frontier = next_frontier
    return frozenset(closure)


def core_relative_to_project(core_file: Path | str, project_root: Path | str, path: str) -> str:
    """A fileset path (core-dir-relative per CAPI2) re-based project-root-relative.

    For a repo-root ``.core`` this is the identity — which is why consumers that
    joined fileset paths straight onto the project root used to get away with
    it — but a nested or state-zone core (ADR 0036 stealth cores) declares its
    files relative to the ``.core``'s own directory. Purely lexical (normpath,
    never ``resolve()``): a stealth core's reach-through symlinks must keep
    their project-relative spelling, not collapse to their targets.
    """
    # FuseSoC values can cross a Windows/POSIX process boundary (for example,
    # a provider-generated prospective checkout inspected in Linux). Treat both
    # separators as lexical path separators regardless of this process's host.
    portable_path = path.replace("\\", "/")
    joined = os.path.normpath(
        str(_core_files_root(Path(core_file), Path(project_root)) / portable_path)
    )
    # Forward slashes always — .core files are authored that way, and consumers
    # compare against git ls-files output (also forward-slash on every OS).
    return os.path.relpath(joined, str(project_root)).replace("\\", "/")


def canonical_project_path(project_root: Path | str, path: Path | str) -> str:
    """Resolve *path* and express an in-project target project-relative.

    Target files exposed beside a stealth ``.core`` are commonly symlinks into
    the repository. Consumers that edit files or compare them with Git output
    need the tracked target path, not the resolution-link spelling. Paths that
    resolve outside the project stay absolute so they cannot masquerade as
    project-owned files.
    """
    root = Path(project_root).resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return resolved.as_posix()


def all_referenced_files(project_root: Path | str) -> list[str]:
    """Every source path any ``.core`` fileset references, project-relative, sorted.

    A cheap ``.core`` YAML read (no resolve) that walks every fileset of every
    discovered core, including ``file_type: user`` data files (firmware hex,
    memory-init images) and include headers. Used by doctor to catch a ``.core``
    that references a file which exists on disk but is not tracked by git — the
    silent trap when a vendored data file matches the upstream ``.gitignore`` and
    ``git add`` no-ops without ``-f``.
    """
    root = Path(project_root)
    files: set[str] = set()
    for core_file in discover_cores(root):
        try:
            doc = read_core(core_file)
        except FuseSocError:
            continue
        filesets = doc.get("filesets")
        if not isinstance(filesets, Mapping):
            continue
        for fs in filesets.values():
            if isinstance(fs, Mapping):
                for path, _tags, _is_include in _fileset_entries(fs):
                    files.add(core_relative_to_project(core_file, root, path))
    return sorted(files)


def _fileset_file_attrs(
    fileset: Mapping[str, Any],
) -> Iterator[tuple[str, Mapping[str, Any]]]:
    """Yield ``(path, attrs)`` for each file entry in a ``.core`` fileset.

    Normalizes the three accepted entry forms — a bare string (no attrs), the
    CAPI2 ``{path: attrs}`` map, and a defensive resolved-EDAM ``{name, ...}``
    map — to a path plus its attribute mapping (empty for a bare string). This
    is the shared entry-shape dispatch behind :func:`_fileset_entries` and the
    per-file annotation readers (``tags``, ``is_include_file``).
    """
    for entry in fileset.get("files") or []:
        if isinstance(entry, str):
            yield entry, {}
            continue
        if not isinstance(entry, Mapping):
            continue
        if "name" in entry:  # defensive: resolved-EDAM-style entry
            yield str(entry["name"]), entry
        elif len(entry) == 1:  # CAPI2 ``{path: attrs}``
            ((name, raw_attrs),) = entry.items()
            yield str(name), raw_attrs if isinstance(raw_attrs, Mapping) else {}


def _fileset_entries(
    fileset: Mapping[str, Any],
) -> Iterator[tuple[str, tuple[str, ...], bool]]:
    """Yield ``(path, tags, is_include)`` for each file in a ``.core`` fileset.

    Fileset-level ``tags`` are the per-file default; a file given as a
    ``{path: {tags: [...]}}`` map appends its own (CAPI2 semantics, ADR Q1).
    Both the CAPI2 ``{path: attrs}`` form and a defensive ``{name, ...}`` form
    are accepted; a bare string carries only the fileset default tags.
    """
    default_tags = tuple(fileset.get("tags") or ())
    for name, attrs in _fileset_file_attrs(fileset):
        file_tags = tuple(attrs.get("tags") or ())
        yield name, default_tags + file_tags, bool(attrs.get("is_include_file"))


def vendored_files(project_root: Path | str) -> set[str]:
    """Project-relative paths any ``.core`` fileset tags ``vendored``.

    A vendored file is an upstream-shipped binary with no in-repo source that
    Booley cannot rebuild (a prebuilt test ELF, a golden memory image). The
    marker is the CAPI2-valid per-file ``tags: [vendored]`` attribute — an
    explicit opt-out of doctor's "committed artifact looks built from in-repo
    source" heuristic, which otherwise can't distinguish a frozen build of
    *local* source (bad — ship the toolchain) from a genuinely un-rebuildable
    vendored blob (fine). ``tags`` is used rather than a bare ``vendored: true``
    key because the latter is NOT a CAPI2 file attribute: real fusesoc rejects
    the whole core on it (QA-3), and Booley's own shallow ``.core`` check now
    flags it. Read cheaply from ``.core`` YAML, no resolve.
    """
    root = Path(project_root)
    vendored: set[str] = set()
    for core_file in discover_cores(root):
        try:
            doc = read_core(core_file)
        except FuseSocError:
            continue
        filesets = doc.get("filesets")
        if not isinstance(filesets, Mapping):
            continue
        for fs in filesets.values():
            if not isinstance(fs, Mapping):
                continue
            for name, attrs in _fileset_file_attrs(fs):
                tags = attrs.get("tags")
                if isinstance(tags, list) and "vendored" in tags:
                    vendored.add(name)
    return vendored


def _partition_fileset_files(
    core_file: Path,
    project_root: Path | str,
    filesets: Any,
    fileset_names: Collection[str],
    rtl: list[str],
    tb: list[str],
    *,
    include_headers: bool = False,
) -> None:
    """Append one core's fileset files into the RTL/TB partition, in place."""
    if not isinstance(filesets, Mapping):
        return
    for fs_name in fileset_names:
        fileset = filesets.get(fs_name)
        if not isinstance(fileset, Mapping):
            continue
        for path, tags, is_include in _fileset_entries(fileset):
            lexical = core_relative_to_project(core_file, project_root, path)
            rel = canonical_project_path(project_root, lexical)
            if "tb" in tags:
                tb.append(rel)
            elif include_headers or not is_include:
                rtl.append(rel)


def _dependency_fileset_names(doc: Mapping[str, Any]) -> list[str]:
    """Fileset names a core contributes when pulled in as a plain dependency.

    A CAPI2 ``depend`` selects the whole core, and FuseSoC builds it through
    its ``default`` target. When the core declares no ``default``, fall back to
    every fileset it defines — over-collecting a dependency's sources is
    recoverable, while missing the RTL entirely is the F-27 failure.
    """
    targets = doc.get("targets")
    default_def = targets.get("default") if isinstance(targets, Mapping) else None
    if isinstance(default_def, Mapping):
        return target_fileset_names(default_def)
    filesets = doc.get("filesets")
    return list(filesets) if isinstance(filesets, Mapping) else []


def _target_source_files(
    project_root: Path | str,
    target: str,
    include_dependencies: bool = False,
    *,
    include_headers: bool = False,
) -> CoreSources:
    """Partition a Target's declared source files RTL-vs-TB from the ``.core``.

    Reads ``.core`` YAML directly (decision 6) — no ``fusesoc run`` subprocess —
    walking the Target's ``filesets`` and splitting files by the ``tags:[tb]``
    marker. RTL include headers are omitted by default because this is normally
    a compile list; *include_headers* retains them for build-input consumers
    such as freshness fingerprinting. This is the pre-resolve partition
    (decision 13) for consumers that cannot wait for :func:`resolve_target`.
    Every returned source is canonicalized through resolution symlinks first;
    this applies equally to RTL, TB, and dependency files.

    With *include_dependencies*, also walk the CAPI2 ``depend`` closure. In a
    layered repo the root core owns only the harness — Ibex's sim Target holds
    the C++/firmware assets while ``ibex_top_tracing.sv`` arrives transitively —
    so a root-core-only read yields an empty RTL list and callers conclude the
    Target has no design to work on (F-27). Still pure YAML: the closure walk
    keeps the subprocess-free ordering guarantee that ``mutation_tester``'s
    in-place mux swap depends on.

    Raises :class:`~booley.targets.domain.UnknownTargetError` /
    :class:`~booley.targets.domain.AmbiguousTargetError` when
    *target* is not a single selectable Target (ADR 0030).
    """
    ref = _resolve_ref(project_root, target)
    return target_source_files_for_ref(
        project_root,
        ref,
        include_dependencies=include_dependencies,
        include_headers=include_headers,
    )


def target_source_files_for_ref(
    project_root: Path | str,
    ref: TargetRef,
    include_dependencies: bool = False,
    *,
    include_headers: bool = False,
) -> CoreSources:
    """Partition sources for a Target reference already resolved by the caller."""
    doc = read_core(ref.core_file)
    targets = doc.get("targets") or {}
    target_def = targets.get(ref.name) if isinstance(targets, Mapping) else None
    rtl: list[str] = []
    tb: list[str] = []
    _partition_fileset_files(
        ref.core_file,
        project_root,
        doc.get("filesets") or {},
        target_fileset_names(target_def),
        rtl,
        tb,
        include_headers=include_headers,
    )

    if include_dependencies:
        closure = _selectable_core_closure_for_refs(Path(project_root), [ref])
        for core_file in sorted(closure):
            if core_file == ref.core_file:
                continue  # already partitioned above, per the selected Target
            try:
                dep_doc = read_core(core_file)
            except FuseSocError:
                continue  # an unreadable dependency is resolve-time's to report
            _partition_fileset_files(
                core_file,
                project_root,
                dep_doc.get("filesets") or {},
                _dependency_fileset_names(dep_doc),
                rtl,
                tb,
                include_headers=include_headers,
            )

    # Preserve first-seen order while dropping duplicates: a file reachable
    # through two dependency paths must not be mutated or counted twice.
    return CoreSources(
        rtl_source_files=tuple(dict.fromkeys(rtl)),
        tb_files=tuple(dict.fromkeys(tb)),
    )


def target_fileset_names(target_def: Mapping[str, Any] | None) -> list[str]:
    """Fileset names a Target pulls in — ``filesets`` unioned with ``filesets_append``.

    Public interface: Simulation trace-overlay orchestration and Doctor depend on this
    name rather than reaching for a private helper (principle 9). Both keys it
    consumes are schema-audited as arrays (:data:`_CAPI2_TARGET_ARRAY_FIELDS`),
    so the splat below cannot silently explode a stray scalar into
    per-character fileset names.

    CAPI2 lets a target extend an inherited list with ``<key>_append`` — the
    idiom for YAML-anchor targets (``<<: *default_target`` + ``filesets_append``),
    since a plain ``filesets:`` override REPLACES the anchor's list. Every
    pre-resolve reader that walks a Target's filesets must union both keys or
    appended filesets silently vanish (the source-existence preflight, the
    tagged-TB / simulation-tag audits, and the trace-overlay dump-module check
    all shared this blind spot). Order is base filesets then appended, matching
    CAPI2 append semantics.
    """
    td = target_def or {}
    return [*(td.get("filesets") or []), *(td.get("filesets_append") or [])]


def _possible_expression_values(value: str) -> list[str]:
    """Return every value a CAPI2 conditional expression can select.

    This is a conservative inventory, independent of the active FuseSoC flag
    set: callers that promise every Target-referenced input must include both
    conditional branches. Invalid expressions are returned unchanged so the
    caller can apply the same conservative non-literal policy as FuseSoC.
    """
    try:
        ast = Exprs(value).ast
    except ValueError:
        return [value]

    values: list[str] = []

    def _walk(nodes: list[Any]) -> None:
        for node in nodes:
            if isinstance(node, str):
                values.extend(node.split())
            elif isinstance(node, tuple) and len(node) == 3 and isinstance(node[2], list):
                _walk(node[2])

    _walk(ast)
    return values


def possible_target_fileset_names(target_def: Mapping[str, Any] | None) -> list[str]:
    """Every fileset a Target may select, including conditional entries."""
    return [
        name
        for expression in target_fileset_names(target_def)
        if isinstance(expression, str)
        for name in _possible_expression_values(expression)
    ]


def _possible_fileset_names(target_def: Mapping[str, Any] | None) -> list[str]:
    """Compatibility wrapper for internal callers."""
    return possible_target_fileset_names(target_def)


# Characters that mark a fileset path as non-literal (a glob or a CAPI2
# conditional expression). FuseSoC itself does not expand globs in ``files``
# lists, but the preflight stays conservative: anything that is not a plain
# literal path is left for FuseSoC to judge rather than hard-failed here.
_NON_LITERAL_PATH_CHARS = frozenset("*?[")

# The directory-name convention for git-worktree baselines a ``.core`` may
# reference (e.g. ``worktrees/scalar_1bfe1733/rtl/alu.sv``). A missing path
# under it gets a ``git worktree add`` fix-hint in :class:`MissingSourceError`.
_WORKTREE_DIR_NAME = "worktrees"


def _literal_target_source_paths(
    core_doc: Mapping[str, Any],
    target: str,
) -> list[str]:
    """Literal source paths a Target's own filesets declare, in ``.core`` order.

    Walks the Target's filesets exactly like :func:`target_source_files` does —
    through :func:`target_fileset_names`, so ``filesets_append`` entries are
    included (unconditional fileset names only — a CAPI2 conditional such as
    ``"tool_x ? (fs)"`` is not a key of the ``filesets`` mapping, so it is
    skipped, and ``depend``-ed cores are not walked). Non-literal entries
    (globs / conditional expressions, :data:`_NON_LITERAL_PATH_CHARS`) are
    excluded so the existence preflight can never false-positive on them.
    De-duplicated, first occurrence wins.
    """
    targets = core_doc.get("targets") or {}
    target_def = targets.get(target) if isinstance(targets, Mapping) else None
    fileset_names = target_fileset_names(target_def)
    filesets = core_doc.get("filesets") or {}
    paths: list[str] = []
    seen: set[str] = set()
    for fs_name in fileset_names:
        fileset = filesets.get(fs_name) if isinstance(filesets, Mapping) else None
        if not isinstance(fileset, Mapping):
            continue
        for path, _tags, _is_include in _fileset_entries(fileset):
            if _NON_LITERAL_PATH_CHARS.intersection(path):
                continue
            if path not in seen:
                seen.add(path)
                paths.append(path)
    return paths


def _target_referenced_files(project_root: Path | str, target: str) -> tuple[str, ...]:
    """Return every file referenced by one Target, including data files.

    Unlike :func:`target_source_files`, this inventory intentionally includes
    ``file_type: user`` inputs such as firmware images. Conditional filesets and
    files from dependency cores' ``default`` Targets are included conservatively.
    Paths are relative to *project_root*, matching ticket Scope and CI contracts.
    """
    root = Path(project_root)
    ref = _resolve_ref(root, target)
    doc = read_core(ref.core_file)
    targets = doc.get("targets") or {}
    target_def = targets.get(ref.name) if isinstance(targets, Mapping) else None
    referenced = _referenced_files_for_filesets(
        ref.core_file,
        root,
        doc.get("filesets"),
        _possible_fileset_names(target_def),
    )

    closure = _selectable_core_closure_for_refs(root, [ref])
    for core_file in sorted(closure):
        if core_file == ref.core_file:
            continue
        dep_doc = read_core(core_file)
        referenced.extend(
            _referenced_files_for_filesets(
                core_file,
                root,
                dep_doc.get("filesets"),
                _possible_dependency_fileset_names(dep_doc),
            )
        )
    return tuple(dict.fromkeys(referenced))


def _possible_dependency_fileset_names(doc: Mapping[str, Any]) -> list[str]:
    """Every fileset a dependency core's default Target may contribute."""
    targets = doc.get("targets")
    default_def = targets.get("default") if isinstance(targets, Mapping) else None
    if isinstance(default_def, Mapping):
        return _possible_fileset_names(default_def)
    return []


def _referenced_files_for_filesets(
    core_file: Path,
    project_root: Path,
    filesets: Any,
    fileset_names: Collection[str],
) -> list[str]:
    """Expand one core's possible fileset inputs into project-relative paths."""
    if not isinstance(filesets, Mapping):
        return []
    referenced: list[str] = []
    for fileset_name in fileset_names:
        fileset = filesets.get(fileset_name)
        if not isinstance(fileset, Mapping):
            continue
        for expression, _attrs in _fileset_file_attrs(fileset):
            for declared in _possible_expression_values(expression):
                if _NON_LITERAL_PATH_CHARS.intersection(declared):
                    continue
                referenced.append(core_relative_to_project(core_file, project_root, declared))
    return referenced


def _worktree_root_of(path: str) -> str | None:
    """The ``worktrees/<name>`` prefix of *path*, or ``None`` when not under one.

    ``worktrees/scalar_1bfe1733/rtl/alu.sv`` → ``worktrees/scalar_1bfe1733``.
    """
    parts = PurePosixPath(path).parts
    for i, part in enumerate(parts[:-1]):  # need at least one component below it
        if part == _WORKTREE_DIR_NAME:
            return str(PurePosixPath(*parts[: i + 2]))
    return None


def _missing_target_sources(project_root: Path | str, target: str) -> list[str]:
    """Literal fileset paths of *target*'s declaring core that don't exist on disk.

    CAPI2 fileset paths are relative to the declaring ``.core``'s directory
    (the *core root*), which is where FuseSoC's export would look for them.
    Returned as declared (core-root-relative), in ``.core`` order. Empty when
    everything exists — or when *target* is not enumerable / its ``.core`` is
    unreadable, in which case existence is not this preflight's question and
    resolution's own errors take over.
    """
    try:
        ref = _resolve_ref(project_root, target)
    except FuseSocError:
        return []
    return missing_target_sources_for_ref(project_root, ref)


def missing_target_sources_for_ref(
    project_root: Path | str,
    ref: TargetRef,
) -> list[str]:
    """Return missing literal paths for an already-selected Target declaration."""
    try:
        doc = read_core(ref.core_file)
    except FuseSocError:
        return []
    if doc is None:
        return []
    core_root = _core_files_root(ref.core_file, Path(project_root))
    missing: list[str] = []
    for declared in _literal_target_source_paths(doc, ref.name):
        pure = PurePosixPath(declared)
        candidate = Path(declared) if pure.is_absolute() else core_root / pure
        if not candidate.exists():
            missing.append(declared)
    return missing


def _preflight_target_sources(target: str, project_root: Path | str) -> None:
    """Fail fast when *target* references source paths that don't exist on disk.

    The shared seam every built-in resolves through (:func:`resolve_target` —
    simulate, elaborate, lint, asic_synthesize's ``--extra-rtl`` filelist,
    fpga_impl, coverage/mutation) so a dangling fileset reference dies here
    with an actionable message instead of FuseSoC's first-file-only
    ``Cannot find <file> in .`` — or worse, a confusing downstream EDA-tool error.

    Raises :class:`MissingSourceError` listing **all** missing literal paths;
    paths under a ``worktrees/`` directory get a ``git worktree add`` fix-hint
    (the motivating incident: a baseline Target pointing at a worktree checkout
    the user was expected to create).
    """
    missing = _missing_target_sources(project_root, target)
    if not missing:
        return
    ref = _resolve_ref(project_root, target)  # exists: missing is non-empty
    _raise_missing_target_sources(target, ref, missing)


def preflight_target_sources_for_ref(project_root: Path | str, ref: TargetRef) -> None:
    """Fail fast for missing sources without resolving a selected token again."""
    missing = missing_target_sources_for_ref(project_root, ref)
    if missing:
        _raise_missing_target_sources(ref.name, ref, missing)


def _raise_missing_target_sources(target: str, ref: TargetRef, missing: list[str]) -> None:
    lines = [
        f"cannot resolve Target '{target}': {len(missing)} source path(s) "
        f"declared by {ref.core_file} do not exist on disk "
        f"(paths are relative to the .core's directory):",
    ]
    lines.extend(f"  - {path}" for path in missing)
    # One hint per distinct worktrees/<name> root, in first-seen order.
    worktree_roots = list(
        dict.fromkeys(root for path in missing if (root := _worktree_root_of(path)) is not None)
    )
    lines.extend(
        f"hint: '{root}' looks like a git worktree baseline; create it with: "
        f"git worktree add {root} <commit-ish>"
        for root in worktree_roots
    )
    raise MissingSourceError("\n".join(lines))


def _core_relative_path(core_file: Path, root: Path, declared: str) -> str:
    """Normalize a ``.core``-declared fileset path to project-root-relative POSIX.

    CAPI2 fileset paths are relative to the ``.core`` file's own directory, not
    the project root. For a flat repo (ADR 0026) the ``.core`` sits at the root
    so the two coincide; for a ``.core`` in a subdir they don't. This joins the
    declared path onto the core's directory, then re-expresses it relative to
    *root* (collapsing any ``..``). An absolute or escaping path that lands
    outside *root* is returned verbatim (POSIX-normalized) so it can never be
    silently mis-attributed to an in-tree file.
    """
    pure = PurePosixPath(declared)
    candidate = Path(declared) if pure.is_absolute() else _core_files_root(core_file, root) / pure
    return canonical_project_path(root, candidate)


def _core_files_root(core_file: Path, project_root: Path) -> Path:
    """Effective fileset root for an authoritative core.

    Explicit stealth mode projects cores from ``.booley_project/cores`` into
    the repository root before FuseSoC resolution.  Cheap YAML readers keep
    reading the authoritative file, so they must apply the same root semantics.
    Legacy hidden cores retain core-directory-relative behavior.
    """
    stealth_root = state_cores_dir(project_root)
    if projection_enabled(project_root) and core_file.is_relative_to(stealth_root):
        return project_root
    return core_file.parent


def classified_sources(project_root: Path | str) -> CoreSources:
    """Project-wide RTL/TB partition across **every** discovered ``.core``.

    The subprocess-free, target-agnostic ground truth for diff classification
    (decision 13). Unlike :func:`target_source_files` — which is scoped to one
    Target's filesets and drops ``is_include_file`` headers from the compile
    list — this unions all filesets of all cores and keeps include headers on
    the **RTL** side, because an edited ``.svh`` invalidates RTL builds and must
    classify as an RTL change. Paths are normalized project-root-relative
    (:func:`_core_relative_path`). Empty when no ``.core`` exists (pre-migration
    projects; callers fall back to hardcoded directory prefixes).
    """
    root = Path(project_root)
    rtl: set[str] = set()
    tb: set[str] = set()
    for core_file in discover_cores(root):
        try:
            doc = read_core(core_file)
        except FuseSocError:
            continue
        filesets = doc.get("filesets")
        if not isinstance(filesets, Mapping):
            continue
        for fs in filesets.values():
            if not isinstance(fs, Mapping):
                continue
            for path, tags, _is_include in _fileset_entries(fs):
                rel = _core_relative_path(core_file, root, path)
                (tb if "tb" in tags else rtl).add(rel)
    return CoreSources(rtl_source_files=tuple(sorted(rtl)), tb_files=tuple(sorted(tb)))


def source_dirs_from_core(
    project_root: Path | str,
) -> tuple[list[str], list[str], list[str]]:
    """Derive ``(rtl_dirs, tb_dirs, tb_include_dirs)`` from ``.core`` filesets.

    The directory-granularity view of :func:`classified_sources`, for consumers
    that need *where* sources live rather than *which* files they are — write
    boundaries (tb_coder), workspace staging, fingerprint enumeration roots.

    Each entry is the project-relative **parent directory** of a declared file,
    except a **root-level** file (flat repo, ADR 0026), whose entry is the file
    path itself — exactly what ``[sources.*].source_dirs`` used to list (e.g.
    ``picorv32.v``). This keeps the file-vs-dir mix that :func:`shared_infra
    .source_dir_prefixes` already file-detects, so migrating off ``[sources.*]``
    is behaviour-preserving for both flat and structured repos. TB include dirs
    are the parents (or file paths) of ``is_include_file`` entries in TB-tagged
    filesets. Sorted and de-duplicated.

    A resolvable ``.core`` is a precondition for every Booley Flow (ADR 0039):
    a project with none raises rather than guessing — the old hardcoded
    ``(["rtl", "fw"], ["tb"])`` fallback silently fed a wrong partition into
    the Specialist Source Isolation boundary (reviewer stash / mutation-tester
    blindness). ``booley doctor`` FAILs the same condition up front.
    """
    root = Path(project_root)
    rtl_dirs: set[str] = set()
    tb_dirs: set[str] = set()
    tb_incl: set[str] = set()
    saw_core = False

    def _entry(rel: str) -> str:
        parent = PurePosixPath(rel).parent.as_posix()
        return rel if parent == "." else parent  # root file → verbatim path

    for core_file in discover_cores(root):
        try:
            doc = read_core(core_file)
        except FuseSocError:
            continue
        filesets = doc.get("filesets")
        if not isinstance(filesets, Mapping):
            continue
        saw_core = True
        for fs in filesets.values():
            if not isinstance(fs, Mapping):
                continue
            for path, tags, is_include in _fileset_entries(fs):
                entry = _entry(_core_relative_path(core_file, root, path))
                if "tb" in tags:
                    tb_dirs.add(entry)
                    if is_include:
                        tb_incl.add(entry)
                else:
                    rtl_dirs.add(entry)
    if not saw_core:
        raise FuseSocError(
            "project has no .core: a resolvable FuseSoC .core Target is a "
            "precondition for every Booley Flow (ADR 0039) — author one (the "
            "booley-setup skill's project-config step walks through it)"
        )
    return (sorted(rtl_dirs), sorted(tb_dirs), sorted(tb_incl))


def _sim_target_has_untagged_tb(project_root: Path | str, target: str) -> bool:
    """True when a sim Target declares files but none carry ``tags:[tb]``.

    A sim Target needs a testbench, and Source Isolation partitions RTL-vs-TB by
    the ``tags:[tb]`` marker (decision 13).  A sim Target with files but **zero**
    tb-tagged files means its testbench is untagged — the partition would
    mis-classify TB as RTL (e.g. ``mutation_tester`` would mutate the TB).  ADR
    0022 dec 13 makes that a **setup-time Source-Isolation error**; this is the
    predicate the ``booley doctor`` audit (migration plan Phase 7 — "tagged TB
    filesets") raises on.  Non-sim Targets (synth/fpga) legitimately have no TB
    and are out of scope — callers gate on the Target's EDA tool first.
    """
    src = _target_source_files(project_root, target)
    return bool(src.rtl_source_files) and not src.tb_files


# ---------------------------------------------------------------------------
# Resolved EDAM — read FuseSoC's output, never generate it
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedFile:
    """One file entry from a resolved EDAM (``.eda.yml``)."""

    name: str
    """Path as FuseSoC emits it — relative to the EDAM's :attr:`build_root`."""

    file_type: str
    """The CAPI2 ``file_type`` (e.g. ``systemVerilogSource``)."""

    tags: tuple[str, ...] = ()
    """CAPI2 fileset tags that survive into the EDAM (decision 13)."""

    core: str | None = None
    """The VLNV of the core that contributed the file, if recorded."""

    is_include: bool = False
    """CAPI2 ``is_include_file`` — a header reached via ``include``, not a
    compiled source. Synthesis feeds these as include *directories*, not as
    sources to elaborate (asic_synthesize slice)."""

    @property
    def is_tb(self) -> bool:
        """True when tagged ``tb`` — the RTL/TB partition Source Isolation needs."""
        return "tb" in self.tags

    @property
    def is_hdl(self) -> bool:
        """True for (System)Verilog sources — what an HDL frontend can parse.

        Dependency cores contribute non-HDL EDAM entries too (``user`` data
        files like ``.vmem`` images or a ``copyto`` Python script); those
        matter for fingerprinting but must never be fed to sv2v/yosys.
        """
        return self.file_type.startswith(("verilogSource", "systemVerilogSource"))

    def absolute(self, build_root: Path | str) -> Path:
        """Resolve :attr:`name` against *build_root* into an absolute path."""
        return (Path(build_root) / self.name).resolve()


@dataclass(frozen=True)
class ResolvedTarget:
    """A fully resolved Target: FuseSoC's EDAM plus the build dir it generated."""

    name: str
    """The Target name that was resolved."""

    vlnv: str
    """The core VLNV that was resolved."""

    toplevel: str
    """The resolved top module (a sim Target's TB top)."""

    eda_tool: str | None
    """The EDA tool from ``flow_options.tool`` — drives criterion-family
    eligibility (decision 11)."""

    files: tuple[ResolvedFile, ...]
    """Every resolved source file, in EDAM order (packages already first)."""

    parameters: Mapping[str, Any]
    """The resolved ``parameters`` block (typed by FuseSoC)."""

    build_root: Path
    """The generated build dir holding ``Makefile`` / ``.vc`` / ``.eda.yml``;
    ``make -C build_root`` runs the flow."""

    edam_path: Path = field(repr=False)
    """Absolute path to the resolved ``.eda.yml`` that was parsed."""

    flow_options: Mapping[str, Any] = field(default_factory=dict)
    """The resolved Target's ``flow_options`` block.

    Build-recipe inputs belong to the Target, so Booley Flows consume this resolved
    mapping instead of duplicating those settings in ``booley.toml``.
    """

    cocotb_module: str | None = None
    """The cocotb Python test module from ``flow_options.cocotb_module``
    (ADR 0034 decision 2). Non-``None`` marks a **Cocotb Target**: the sim
    run-half becomes cocotb-aware (env glue + ``results.xml`` verdicts) and
    Simulation Sentinels are bypassed. Read from the *resolved* EDAM — the
    run-time authority (the cheap ``.core`` read backs validation only)."""

    @property
    def rtl_files(self) -> tuple[ResolvedFile, ...]:
        """Files not tagged ``tb`` — the design under test (decision 13)."""
        return tuple(f for f in self.files if not f.is_tb)

    @property
    def tb_files(self) -> tuple[ResolvedFile, ...]:
        """Files tagged ``tb`` — testbench sources (decision 13)."""
        return tuple(f for f in self.files if f.is_tb)

    @property
    def rtl_source_files(self) -> tuple[ResolvedFile, ...]:
        """Non-TB compiled sources — excludes ``include`` headers.

        Synthesis elaborates these (fed to sv2v/yosys as source files); the
        headers they ``include`` are surfaced separately via
        :attr:`rtl_include_dirs`.
        """
        return tuple(f for f in self.rtl_files if not f.is_include)

    @property
    def rtl_hdl_source_files(self) -> tuple[ResolvedFile, ...]:
        """:attr:`rtl_source_files` narrowed to (System)Verilog sources.

        The slice HDL frontends consume: dependency cores can contribute
        ``user``-typed EDAM entries (data ``.vmem`` images, ``copyto``
        scripts) that belong in fingerprints but crash sv2v/yosys if fed as
        sources (ibex: a dependency's ``check_tool_requirements.py``).
        """
        return tuple(f for f in self.rtl_source_files if f.is_hdl)

    @property
    def sdc_files(self) -> tuple[ResolvedFile, ...]:
        """Non-TB files tagged ``file_type: SDC`` — per-target STA constraints.

        ADR 0029: a Target carries its ASIC timing intent (clock period, I/O
        delays, false/multicycle paths) as an SDC fileset. ``asic_synthesize``
        forwards these to ``run_yosys_syn --sta-sdc``. TB-tagged SDCs are
        excluded — the same RTL/TB partition Source Isolation uses.
        """
        return tuple(f for f in self.rtl_files if f.file_type == "SDC")

    @property
    def xdc_files(self) -> tuple[ResolvedFile, ...]:
        """Non-TB files tagged ``file_type: xdc`` — per-target FPGA constraints.

        ADR 0031: the Xilinx-dialect twin of :attr:`sdc_files`. An XDC carries
        pin placement *and* ``create_clock``/false-paths — a design constraint,
        not a board knob — so it travels with the Target as a fileset, the sole
        source of FPGA constraints. ``fpga_impl`` feeds these into
        the vivado EDAM (Edalize recognizes ``file_type: xdc`` natively). Matched
        case-insensitively: a ``.core`` may tag it ``xdc`` (Edalize convention)
        or ``XDC``; either resolves. TB-tagged files are excluded, mirroring
        :attr:`sdc_files`.
        """
        return tuple(f for f in self.rtl_files if f.file_type.lower() == "xdc")

    @property
    def rtl_include_dirs(self) -> tuple[Path, ...]:
        """Directories holding non-TB ``include`` headers, de-duplicated.

        Order follows EDAM file order (first occurrence wins). Each is absolute
        and resolved against :attr:`build_root`; generated commands later make
        the path relative to the Session Runtime workspace where needed.
        """
        dirs: list[Path] = []
        for f in self.rtl_files:
            if not f.is_include:
                continue
            parent = f.absolute(self.build_root).parent
            if parent not in dirs:
                dirs.append(parent)
        return tuple(dirs)


def parse_edam(edam_path: Path | str, *, target: str, vlnv: str) -> ResolvedTarget:
    """Parse a resolved ``.eda.yml`` into a :class:`ResolvedTarget`.

    The EDAM records the design (files/top/params/EDA tool) but not the Booley
    Target name or core VLNV that produced it, so those are passed in.
    """
    path = Path(edam_path)
    try:
        with path.open("r", encoding="utf-8") as f:
            edam = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as exc:
        raise TargetResolutionError(f"could not read EDAM {path}: {exc}") from exc
    if not isinstance(edam, Mapping):
        raise TargetResolutionError(f"EDAM {path} is not a YAML mapping")

    files: list[ResolvedFile] = []
    for entry in edam.get("files") or []:
        if not isinstance(entry, Mapping) or not entry.get("name"):
            continue
        raw_tags = entry.get("tags") or ()
        tags = tuple(raw_tags) if isinstance(raw_tags, (list, tuple)) else ()
        files.append(
            ResolvedFile(
                name=str(entry["name"]),
                file_type=str(entry.get("file_type", "user")),
                tags=tags,
                core=entry.get("core"),
                is_include=bool(entry.get("is_include_file")),
            )
        )

    flow_options = edam.get("flow_options") or {}
    if not isinstance(flow_options, Mapping):
        flow_options = {}
    eda_tool = flow_options.get("tool")
    cocotb_module = flow_options.get("cocotb_module")

    return ResolvedTarget(
        name=target,
        vlnv=vlnv,
        toplevel=str(edam.get("toplevel", "")),
        eda_tool=eda_tool,
        flow_options=dict(flow_options),
        cocotb_module=str(cocotb_module) if cocotb_module else None,
        files=tuple(files),
        parameters=dict(edam.get("parameters") or {}),
        build_root=path.parent,
        edam_path=path,
    )


def _find_edam(build_root: Path, target: str) -> Path:
    """Locate the resolved ``.eda.yml`` for *target* under *build_root*.

    FuseSoC lays runs out as ``<build_root>/<name>/<target>/<name>.eda.yml``;
    glob for the Target dir so we tolerate the VLNV→dir name mangling.
    """
    matches = sorted(build_root.glob(f"*/{target}/*.eda.yml"))
    if not matches:
        # Fall back to any EDAM under the tree (single-target spikes nest flat).
        matches = sorted(build_root.rglob("*.eda.yml"))
    if not matches:
        raise TargetResolutionError(
            f"no .eda.yml found under {build_root} after resolving '{target}'"
        )
    if len(matches) > 1:
        logger.debug("multiple EDAMs under %s; using %s", build_root, matches[0])
    return matches[0]


def _setup_command(
    target: str,
    *,
    project_root: Path | str,
    build_root: Path | str,
    vlnv: str | None = None,
    fusesoc_cmd: Sequence[str] = DEFAULT_FUSESOC_CMD,
) -> list[str]:
    """Build (but do not run) the ``fusesoc run --setup`` argv for *target*.

    The command-construction half of :func:`resolve_target`, factored out so the
    ``--dry-run`` preview can show exactly what resolution *would* execute
    without running it (decision 4: FuseSoC owns the setup). The declaring core
    is always looked up via :func:`resolve_ref` (a cheap ``.core`` YAML read — no
    subprocess) because its ``flow``/upstream ``tool`` fields drive the
    ``tool_<x>`` use-flag
    re-injection below, and because *target* may be a ``vlnv#name`` qualifier
    (ADR 0030) whose bare ``name`` is what ``fusesoc --target`` must receive — so
    *vlnv*, when passed, overrides only the resolved VLNV argument and does
    **not** skip enumeration. Raises :class:`TargetResolutionError` if *target*
    is not a single known Target (unless *vlnv* is supplied, which names the core
    explicitly).
    """
    project_root = Path(project_root)
    build_root = Path(build_root)
    try:
        library_plan = prepare_core_library_plan(project_root)
    except (CoreProjectionError, OSError) as exc:
        raise TargetResolutionError(f"could not project stealth cores: {exc}") from exc
    if vlnv is None:
        try:
            ref = _resolve_ref(project_root, target)
        except FuseSocError as exc:  # unknown / ambiguous → resolution error
            raise TargetResolutionError(str(exc)) from exc
        vlnv = ref.vlnv
    else:
        # vlnv names the core explicitly; still enumerate for the tool_<x> flag.
        try:
            ref = _resolve_ref(project_root, target)
        except FuseSocError:
            ref = None
    target_name = ref.name if ref is not None else target
    return _setup_argv(
        library_plan,
        target_name=target_name,
        vlnv=vlnv,
        flow=ref.flow if ref is not None else None,
        eda_tool=ref.eda_tool if ref is not None else None,
        build_root=build_root,
        fusesoc_cmd=fusesoc_cmd,
    )


def setup_command_for_handle(
    handle: TargetHandle,
    *,
    build_root: Path | str,
    resolution_vlnv: str | None = None,
    fusesoc_cmd: Sequence[str] = DEFAULT_FUSESOC_CMD,
) -> list[str]:
    """Build FuseSoC setup argv from catalog-authorized Target facts."""
    root = require_current_target_handle(handle)
    try:
        library_plan = prepare_core_library_plan(root)
        library_plan.operational_core(handle.core_file)
    except (CoreProjectionError, OSError) as exc:
        raise TargetResolutionError(f"could not project stealth cores: {exc}") from exc
    return _setup_argv(
        library_plan,
        target_name=handle.name,
        vlnv=resolution_vlnv or handle.vlnv,
        flow=handle.flow,
        eda_tool=handle.eda_tool,
        build_root=Path(build_root),
        fusesoc_cmd=fusesoc_cmd,
    )


def require_current_target_handle(
    handle: TargetHandle,
    *,
    project_root: Path | str | None = None,
) -> Path:
    """Validate a handle's Project and snapshot before operational preparation."""
    root = handle.project_root
    if project_root is not None and Path(project_root).resolve() != root:
        raise ForeignTargetHandleError(
            f"Target {handle.identity!r} was selected for Project {root}, "
            f"not {Path(project_root).resolve()}"
        )
    if handle.snapshot_id and target_snapshot_id(root) != handle.snapshot_id:
        raise StaleTargetCatalogError(
            f"Target catalog for {root} is stale; select the Target again"
        )
    return root


def _setup_argv(
    library_plan: CoreLibraryPlan,
    *,
    target_name: str,
    vlnv: str,
    flow: str | None,
    eda_tool: str | None,
    build_root: Path,
    fusesoc_cmd: Sequence[str],
) -> list[str]:
    flag_args = ["--flag", f"tool_{eda_tool}"] if flow and eda_tool else []
    library_args = [
        argument
        for library_root in library_plan.roots
        for argument in ("--cores-root", str(library_root))
    ]
    return [
        *fusesoc_cmd,
        *library_args,
        "run",
        "--build-root",
        str(build_root),
        "--setup",
        *flag_args,
        "--target",
        target_name,
        vlnv,
    ]


def _resolve_target(
    target: str,
    *,
    project_root: Path | str,
    build_root: Path | str,
    vlnv: str | None = None,
    fusesoc_cmd: Sequence[str] = DEFAULT_FUSESOC_CMD,
    env: Mapping[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> ResolvedTarget:
    """Resolve *target* through ``fusesoc run --setup`` and read its EDAM.

    *vlnv*, when passed, overrides the resolved VLNV argument but does not skip
    enumeration (:func:`setup_command` still reads the ``.core`` to re-inject the
    EDA-tool use-flag); otherwise the declaring core is found via
    :func:`enumerate_targets`. Resolution runs the CLI (decision 6),
    pointing FuseSoC at *project_root* (``--cores-root``) and isolating output
    in *build_root* (``--build-root``); the resolved ``.eda.yml`` it generates
    is then parsed into a :class:`ResolvedTarget` (decision 4).

    *runner* defaults to :func:`subprocess.run` and is injectable for tests.
    """
    project_root = Path(project_root)
    build_root = Path(build_root)
    # A vlnv#name selection token (ADR 0030) carries a qualifier that setup_command
    # needs to resolve the declaring core, but FuseSoC's --target, its output dir,
    # and the EDAM all key off the BARE Target name — which is simply the part
    # after '#' (Target names never contain one).
    bare_target = _split_qualifier(target)[1]

    cmd = _setup_command(
        target,
        project_root=project_root,
        build_root=build_root,
        vlnv=vlnv,
        fusesoc_cmd=fusesoc_cmd,
    )
    vlnv = cmd[-1]
    # Fail fast — before the fusesoc subprocess — when the Target's filesets
    # reference sources that don't exist on disk (all of them, with a
    # `git worktree add` hint for missing worktree baselines). Checked against
    # the *enumerated* core for `target`, so an explicit-vlnv resolve (e.g. a
    # --trace overlay, which reuses the base core's filesets) is covered too.
    _preflight_target_sources(bare_target, project_root)
    return _run_setup_command(
        cmd,
        project_root=project_root,
        build_root=build_root,
        target=bare_target,
        vlnv=vlnv,
        fusesoc_cmd=fusesoc_cmd,
        env=env,
        runner=runner,
    )


def _run_setup_command(
    cmd: list[str],
    *,
    project_root: Path,
    build_root: Path,
    target: str,
    vlnv: str,
    fusesoc_cmd: Sequence[str],
    env: Mapping[str, str] | None,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> ResolvedTarget:
    logger.debug("resolving target via: %s", " ".join(cmd))
    try:
        proc = runner(
            cmd,
            cwd=str(project_root),
            env=dict(env) if env is not None else None,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        diagnostic = str(exc).strip()
        detail = f": {diagnostic}" if diagnostic else " with no diagnostic output"
        raise TargetResolutionError(
            f"could not invoke fusesoc ({fusesoc_cmd[0]}){detail}"
        ) from exc
    if proc.returncode != 0:
        diagnostic = proc.stderr.strip() or proc.stdout.strip()
        detail = f":\n{diagnostic}" if diagnostic else " with no diagnostic output"
        raise TargetResolutionError(
            f"fusesoc run --setup --target {target} {vlnv} failed (exit {proc.returncode}){detail}"
        )

    edam_path = _find_edam(build_root, target)
    return parse_edam(edam_path, target=target, vlnv=vlnv)


def resolve_target_handle(
    handle: TargetHandle,
    *,
    build_root: Path | str,
    resolution_vlnv: str | None = None,
    fusesoc_cmd: Sequence[str] = DEFAULT_FUSESOC_CMD,
    env: Mapping[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> ResolvedTarget:
    """Resolve a catalog-authorized handle without selecting its token again."""
    root = require_current_target_handle(handle)
    build = Path(build_root)
    vlnv = resolution_vlnv or handle.vlnv
    cmd = setup_command_for_handle(
        handle,
        build_root=build_root,
        resolution_vlnv=resolution_vlnv,
        fusesoc_cmd=fusesoc_cmd,
    )
    preflight_target_sources_for_ref(
        root,
        TargetRef(
            name=handle.name,
            vlnv=handle.vlnv,
            core_file=handle.core_file,
            eda_tool=handle.eda_tool,
            flow=handle.flow,
            cocotb_module=handle.cocotb_module,
            doctor_flows=handle.doctor_flows,
            doctor_selftest=handle.doctor_private,
        ),
    )
    return _run_setup_command(
        cmd,
        project_root=root,
        build_root=build,
        target=handle.name,
        vlnv=vlnv,
        fusesoc_cmd=fusesoc_cmd,
        env=env,
        runner=runner,
    )


def _try_resolve_target(
    target: str,
    *,
    project_root: Path | str,
    build_root: Path | str | None = None,
    **kwargs: Any,
) -> ResolvedTarget | None:
    """Resolve *target*'s ``.core`` Target, or ``None`` when none is authored / resolution fails.

    The **transitional bridge** (decision 23) for the legacy ``configs.toml``
    fallback path: ADR 0022 makes the ``.core`` the design-description home, but
    a project mid-migration may not have authored one yet. This returns a
    :class:`ResolvedTarget` when *target* is a selectable Target (so the caller
    sources defines/top/params/files from FuseSoC), and ``None`` otherwise — when
    no ``.core`` declares *target*, or when ``fusesoc run --setup`` is unavailable
    (e.g. no ``fusesoc`` on PATH outside the Sandbox) or fails. ``None`` is the
    caller's cue to fall back to the legacy ``configs.toml`` design-description
    until the project is hand-migrated (Phase 8).

    Unlike :func:`resolve_target` (which raises), this never propagates a
    :class:`FuseSocError`; it is the soft, "prefer the ``.core`` if there is one"
    entry point. *build_root* defaults to a payload-dedicated dir under
    ``project_root/.booley_project/.runtime``; extra *kwargs* are forwarded to
    :func:`resolve_target` (e.g. ``runner`` for tests).
    """
    proot = Path(project_root)
    try:
        ref = _resolve_ref(proot, target)
    except FuseSocError as exc:  # unknown / ambiguous / unreadable .core → legacy fallback
        logger.debug("try_resolve_target(%s): resolution failed: %s", target, exc)
        return None
    if build_root is None:
        build_root = proot / ".booley_project" / ".runtime" / "edalize" / "payload" / ref.name
    try:
        return _resolve_target(
            ref.name,
            project_root=proot,
            build_root=build_root,
            vlnv=ref.vlnv,
            **kwargs,
        )
    except FuseSocError as exc:  # no fusesoc on PATH / setup failure → legacy fallback
        logger.debug("try_resolve_target(%s): resolution failed: %s", target, exc)
        return None
