"""Lease and allocate immutable-path Simulation build generations."""

from __future__ import annotations

import contextvars
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import time
from collections.abc import Mapping
from contextlib import AbstractContextManager
from enum import StrEnum
from pathlib import Path
from typing import IO, Any

import yaml

from booley.core.booley_dir_links import lexical_booley_path
from booley.core.boundary import as_dict
from booley.core.build_paths import work_root_for
from booley.core.file_lock import (
    release_file_lock,
    reset_child_lease_fd,
    set_child_lease_fd,
    wait_for_file_lock,
)
from booley.flows import edam as edam_layer
from booley.flows.run_log import RUN_LOG_NAME
from booley.fusesoc import fusesoc_registry, selftest_overlay
from booley.targets.catalog import TargetCatalog, TargetCompileSurface
from booley.targets.domain import TargetHandle

from . import build_reuse
from .build import PreparedSimulationBuild
from .build_reuse import hash_file
from .campaign_reports import simulation_booley_dir


class SimulationBuildSlotError(RuntimeError):
    """A Simulation build slot cannot be used safely."""


class CacheDecision(StrEnum):
    """Why a build was, or was not, served by the slot's retained image."""

    UNEXAMINED = "unexamined"
    HIT = "hit"
    REUSE_UNSUPPORTED = "reuse unsupported"
    MISSING_PROVENANCE = "missing provenance"
    CHANGED_KEY = "changed source, recipe, or tool"
    CHANGED_SOURCE = "changed source"
    CHANGED_ARTIFACT = "changed artifact"


_GENERATION_DIR = "g"


def _runtime_artifacts(prepared: PreparedSimulationBuild) -> tuple[Path, ...]:
    """Select the exact image that the current concrete adapter will launch."""
    root = prepared.build_root
    if prepared.eda_tool == "icarus":
        scripts = tuple(root.glob("*.scr"))
        if len(scripts) != 1:
            raise SimulationBuildSlotError(
                f"expected one Icarus image descriptor in {root}, found {len(scripts)}"
            )
        native = tuple(sorted(root.glob("*.vpi")))
        return (scripts[0], scripts[0].with_suffix(""), *native)
    if prepared.resolved.cocotb_module:
        names = ("Vtop",)
    else:
        names = (f"V{prepared.toplevel}", f"V{prepared.toplevel}.exe")
    images = tuple(root / name for name in names if (root / name).is_file())
    if len(images) != 1:
        raise SimulationBuildSlotError(
            f"expected one Verilator image in {root}, found {len(images)}"
        )
    if not os.access(images[0], os.X_OK):
        raise SimulationBuildSlotError(f"Verilator image is not executable: {images[0]}")
    return images


_RUNTIME_INPUTS_DIR = ".booley-runtime-inputs"
# Files Booley itself writes into a generation around a build or launch.
_RESERVED_PREFIXES = (".booley-adapter-", ".a-")


def snapshot_build_inputs(prepared: PreparedSimulationBuild) -> dict[str, str]:
    """Snapshot files already present in a newly resolved generation."""
    return _hash_generation(prepared, skip_reserved=True)


def snapshot_generation_files(prepared: PreparedSimulationBuild) -> dict[str, str]:
    """Fingerprint the whole generation tree except the live run log.

    Pre-Sim Command detection uses this stricter view, so a hook cannot hide a
    change to the generation: reserved names :func:`snapshot_build_inputs`
    deliberately ignores are included, every file records its permission bits
    next to its content hash, and every directory is recorded with its mode, so
    a chmod-only or empty-directory change is visible too.
    """
    entries = _hash_generation(prepared, skip_reserved=False)
    for relative, digest in tuple(entries.items()):
        entries[relative] = f"{digest} mode={_permission_bits(prepared.work_root / relative)}"
    try:
        for directory in prepared.work_root.rglob("*"):
            if directory.is_dir() and not directory.is_symlink():
                relative = directory.relative_to(prepared.work_root).as_posix()
                entries[f"{relative}/"] = f"directory mode={_permission_bits(directory)}"
    except OSError as exc:
        raise SimulationBuildSlotError(f"cannot read generation directory: {exc}") from exc
    return entries


def _permission_bits(path: Path) -> str:
    """Return *path*'s permission and special mode bits as octal text."""
    try:
        return oct(stat.S_IMODE(path.lstat().st_mode))
    except OSError as exc:
        raise SimulationBuildSlotError(f"cannot read generation file mode: {exc}") from exc


def _hash_generation(prepared: PreparedSimulationBuild, *, skip_reserved: bool) -> dict[str, str]:
    root = prepared.work_root
    # The reserved-name filter already drops every run log by name.
    run_log = None if skip_reserved else prepared.build_root / RUN_LOG_NAME
    hashes: dict[str, str] = {}
    try:
        for path in sorted(root.rglob("*")):
            if skip_reserved and _RUNTIME_INPUTS_DIR in path.parts:
                continue
            if path.is_symlink():
                raise SimulationBuildSlotError(f"symlinked generated build input: {path}")
            if path.is_dir() or path == run_log:
                continue
            if skip_reserved and (
                path.name == RUN_LOG_NAME or path.name.startswith(_RESERVED_PREFIXES)
            ):
                continue
            if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
                raise SimulationBuildSlotError(f"unsafe generated build input: {path}")
            relative = path.relative_to(root).as_posix()
            hashes[relative] = hash_file(path)
        return hashes
    except OSError as exc:
        raise SimulationBuildSlotError(f"cannot read generated build input: {exc}") from exc


def verify_existing_build_inputs(
    prepared: PreparedSimulationBuild, before: Mapping[str, str]
) -> None:
    """Reject edits to staged inputs while allowing new Pre-Sim outputs."""
    after = snapshot_build_inputs(prepared)
    if any(after.get(name) != digest for name, digest in before.items()):
        raise SimulationBuildSlotError("Simulation build input changed during Pre-Sim Commands")


def resolve_target_compile_surface(handle: TargetHandle) -> TargetCompileSurface:
    """Resolve explicit compile inputs once through the handle's catalog snapshot."""
    try:
        return TargetCatalog.build(handle.project_root).compile_inputs(handle)
    except (fusesoc_registry.FuseSocError, OSError, RuntimeError, ValueError) as exc:
        raise SimulationBuildSlotError(
            f"cannot resolve compile inputs for Target {handle.identity!r}: {exc}"
        ) from exc


def _compile_input_identity(project_root: Path, path: Path) -> str:
    try:
        identity = path.relative_to(project_root).as_posix()
    except ValueError:
        identity = str(path)
    if path.is_symlink():
        identity += f" -> {path.readlink()}"
    return identity


def project_compile_surface(
    surface: TargetCompileSurface, *, include_operational_cores: bool = False
) -> dict[str, str]:
    """Snapshot one already-resolved Target compile-input surface."""
    result: dict[str, str] = {}
    paths = surface.authored_paths
    if include_operational_cores:
        paths += surface.operational_paths
    try:
        for path in paths:
            identity = _compile_input_identity(surface.project_root, path)
            if identity in result:
                continue
            if not path.is_file():
                raise SimulationBuildSlotError(f"Project compile input is not a file: {path}")
            result[identity] = hash_file(path)
        for path in surface.optional_paths:
            identity = _compile_input_identity(surface.project_root, path)
            if path.is_file():
                result[identity] = hash_file(path)
            elif path.is_symlink() or path.exists():
                raise SimulationBuildSlotError(f"Project compile input is not a file: {path}")
            else:
                result[identity] = "absent"
    except OSError as exc:
        raise SimulationBuildSlotError(f"cannot capture Project compile inputs: {exc}") from exc
    return result


def _icarus_tool_identity() -> str | None:
    """Hash the installed Icarus executables, modules, and dynamic libraries."""
    commands = ("iverilog", "vvp", "make", "sh")
    resolved = tuple(shutil.which(name) for name in commands)
    if any(path is None for path in resolved):
        return None
    paths = [Path(path).resolve() for path in resolved if path is not None]
    ivl = paths[0].parent.parent / "lib" / "ivl"
    if not ivl.is_dir() or ivl.is_symlink():
        return None
    paths.extend(path for path in ivl.rglob("*") if path.is_file())
    try:
        for path in tuple(paths):
            if not build_reuse.is_elf(path):
                continue
            dependencies = build_reuse.elf_dependencies(path)
            if dependencies is None:
                return None
            paths.extend(dependencies)
        digest = hashlib.sha256()
        for path in sorted(set(paths)):
            if not path.is_file():
                return None
            digest.update(str(path).encode("utf-8"))
            digest.update(str(path.resolve()).encode("utf-8"))
            digest.update(hash_file(path).encode("ascii"))
        return digest.hexdigest()
    except (OSError, subprocess.TimeoutExpired):
        return None


def _python_recipe_identity() -> str | None:
    """Hash the Python implementations that produce and interpret this recipe."""
    digest = hashlib.sha256()
    try:
        for name in ("booley", "fusesoc", "edalize"):
            spec = importlib.util.find_spec(name)
            if spec is None:
                return None
            locations = (
                (Path(spec.origin).parent,)
                if spec.origin is not None
                else tuple(Path(path) for path in spec.submodule_search_locations or ())
            )
            if not locations:
                return None
            for package in locations:
                for path in sorted(package.rglob("*.py")):
                    digest.update(name.encode("utf-8"))
                    digest.update(path.relative_to(package).as_posix().encode("utf-8"))
                    digest.update(hash_file(path).encode("ascii"))
        return digest.hexdigest()
    except OSError:
        return None


def _has_unsupported_source(prepared: PreparedSimulationBuild, core_file: Path) -> bool:
    for item in prepared.resolved.files:
        if item.file_type not in {"verilogSource", "systemVerilogSource"}:
            return True
        source = item.absolute(prepared.build_root)
        parts = Path(item.name).parts
        if len(parts) < 3 or parts[0] != "src":
            return True
        original = core_file.parent.joinpath(*parts[2:])
        if not original.is_file() or original.is_symlink():
            return True
        if hash_file(original) != hash_file(source):
            raise SimulationBuildSlotError(
                f"Project source changed during FuseSoC preparation: {original}"
            )
        if not source.is_relative_to(prepared.work_root.resolve()) or re.search(
            r"`include\b|#\s*include\b", source.read_text(encoding="utf-8")
        ):
            return True
    return False


def _has_unsupported_icarus_recipe(prepared: PreparedSimulationBuild) -> bool:
    """Allow only local source operands and the stock Edalize Icarus commands."""
    root = prepared.build_root
    scripts = tuple(root.glob("*.scr"))
    if len(scripts) != 1:
        return True
    for line in scripts[0].read_text(encoding="utf-8").splitlines():
        tokens = shlex.split(line)
        if not tokens:
            continue
        if len(tokens) != 1 or tokens[0].startswith("-"):
            return True
        source = root / tokens[0]
        if not source.is_file() or not source.resolve().is_relative_to(
            prepared.work_root.resolve()
        ):
            return True
    makefile = root / "Makefile"
    commands = [
        line.strip()
        for line in makefile.read_text(encoding="utf-8").splitlines()
        if line.startswith("\t")
    ]
    if len(commands) != 2:
        return True
    return not (
        commands[0].startswith("$(EDALIZE_LAUNCHER) iverilog ")
        and commands[1].startswith("$(EDALIZE_LAUNCHER) vvp ")
    )


def _eligible_core_file(prepared: PreparedSimulationBuild, project_root: Path) -> Path | None:
    """Accept a single local core without generators, VPI, or textual includes."""
    edam = yaml.safe_load(prepared.resolved.edam_path.read_text(encoding="utf-8"))
    if not isinstance(edam, dict) or edam.get("hooks") or edam.get("vpi"):
        return None
    cores = edam.get("cores")
    if not isinstance(cores, dict) or len(cores) != 1:
        return None
    core = next(iter(cores.values()))
    if not isinstance(core, dict) or core.get("dependencies"):
        return None
    core_path = prepared.resolved.edam_path.parent / core["core_file"]
    # normpath applies FuseSoC's lexical ``..`` first; the re-spelling keeps a core
    # behind a symlinked Booley Directory project-relative (#2191).
    core_file = lexical_booley_path(
        simulation_booley_dir(project_root), Path(os.path.normpath(core_path)).resolve()
    )
    if core_path.is_symlink() or not core_file.is_relative_to(project_root.resolve()):
        return None
    core_data = yaml.safe_load(core_file.read_text(encoding="utf-8"))
    if (
        build_reuse.has_generated_core_behavior(core_data)
        or _has_unsupported_source(prepared, core_file)
        or _has_unsupported_icarus_recipe(prepared)
    ):
        return None
    return core_file


def _json_digest(value: object) -> str:
    """Return the SHA-256 of *value*'s canonical JSON encoding."""
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode("utf-8")).hexdigest()


def _recipe_package_versions() -> dict[str, str]:
    """Versions of the packages that write and interpret the build recipe."""
    return {name: importlib.metadata.version(name) for name in ("fusesoc", "edalize")}


def _reuse_input_key(
    prepared: PreparedSimulationBuild,
    inputs: Mapping[str, str],
    variant: str,
    project_root: Path,
    environment: Mapping[str, str],
) -> str | None:
    """Recognize the closed, ordinary Icarus recipe; decline all others."""
    if (
        variant
        or prepared.eda_tool != "icarus"
        or prepared.resolved.cocotb_module
        or any(item.file_type.lower() == "user" for item in prepared.resolved.files)
    ):
        return None
    if any(environment.get(name) for name in _REUSE_DECLINING_ENVIRONMENT):
        return None
    try:
        core_file = _eligible_core_file(prepared, project_root)
        if core_file is None:
            return None
        tool = _icarus_tool_identity()
        recipe = _python_recipe_identity()
        if tool is None or recipe is None:
            return None
        record = {
            "schema": 1,
            "target": prepared.target_identity,
            "inputs": dict(inputs),
            "core": hash_file(core_file),
            "core_path": str(core_file),
            "tool": tool,
            "recipe": recipe,
            "versions": _recipe_package_versions(),
            "environment": _json_digest(sorted(environment.items())),
            "target_environment": _json_digest(sorted(prepared.environment.items())),
        }
        return _json_digest(record)
    except (
        OSError,
        KeyError,
        UnicodeError,
        yaml.YAMLError,
        importlib.metadata.PackageNotFoundError,
    ):
        return None


# Make variables that can rewrite either recipe out of sight; reuse declines.
_REUSE_DECLINING_ENVIRONMENT = ("EDALIZE_LAUNCHER", "MAKEFLAGS", "MFLAGS")


def _verilator_reuse_key(
    prepared: PreparedSimulationBuild,
    inputs: Mapping[str, str],
    environment: Mapping[str, str],
) -> tuple[str, build_reuse.VerilatorIdentity] | None:
    """Key a stock Edalize Verilator recipe by its complete pre-build identity.

    Trace and coverage variants each own a slot, so the variant needs no
    gate; Doctor's deliberately broken self-test image is never retained.
    Unlike Icarus, includes, dependency cores and C++/DPI sources are
    accepted: the post-build read closure proves what was actually read.
    """
    if (
        prepared.resolved.cocotb_module
        or prepared.core_closure is None
        or selftest_overlay.bad_selftest_requested(environment)
        or any(environment.get(name) for name in _REUSE_DECLINING_ENVIRONMENT)
    ):
        return None
    try:
        recipe = _stock_verilator_recipe(prepared)
        if recipe is None:
            return None
        build_environment = {**environment, **_generation_neutral(prepared)}
        identity = build_reuse.verilator_identity(build_environment, recipe.system_libraries)
        python_recipe = _python_recipe_identity()
        if identity is None or python_recipe is None:
            return None
        record = _verilator_key_record(
            prepared, inputs, identity, python_recipe, build_environment
        )
        return _json_digest(record), identity
    except (
        OSError,
        UnicodeError,
        yaml.YAMLError,
        importlib.metadata.PackageNotFoundError,
    ):
        return None


def _stock_verilator_recipe(
    prepared: PreparedSimulationBuild,
) -> build_reuse.VerilatorRecipe | None:
    """Accept an EDAM without hooks or VPI whose Makefile is the stock recipe."""
    edam = as_dict(yaml.safe_load(prepared.resolved.edam_path.read_text(encoding="utf-8")))
    if edam is None or edam.get("hooks") or edam.get("vpi"):
        return None
    return build_reuse.stock_verilator_recipe(prepared.build_root)


def _verilator_key_record(
    prepared: PreparedSimulationBuild,
    inputs: Mapping[str, str],
    identity: build_reuse.VerilatorIdentity,
    python_recipe: str,
    build_environment: Mapping[str, str],
) -> dict[str, object]:
    """Assemble everything a Verilator reuse key hashes."""
    return {
        "schema": 1,
        "tool_kind": "verilator",
        "target": prepared.target_identity,
        "inputs": dict(inputs),
        "cores": dict(prepared.core_closure or {}),
        "tool": identity.digest,
        "recipe": python_recipe,
        "versions": _recipe_package_versions(),
        "environment": build_reuse.build_environment_digest(
            build_environment, prepared.build_root, identity
        ),
        "target_environment": sorted(_generation_neutral(prepared).items()),
    }


def _generation_neutral(prepared: PreparedSimulationBuild) -> dict[str, str]:
    """Return the Target build environment without its generation path.

    The compiler cache stamps the generation into ``CCACHE_BASEDIR`` and a
    ``-fdebug-prefix-map``; neither changes simulation behavior, and keeping
    them would make every generation's key unique.
    """
    generation = str(prepared.build_root.absolute())
    return {
        name: value.replace(generation, "<generation>")
        for name, value in prepared.environment.items()
    }


def _ambient_environment(prepared: PreparedSimulationBuild) -> dict[str, str]:
    """The process environment minus what the Target build environment overrides.

    The key hashes the Target build environment whole, so only ambient names
    can reach a generated makefile unhashed.
    """
    return {name: value for name, value in os.environ.items() if name not in prepared.environment}


def _matching_hashes(root: Path, recorded: Mapping[str, str]) -> bool:
    """Whether every recorded file exists under *root* with its recorded digest."""
    return all(
        (root / name).is_file() and hash_file(root / name) == digest
        for name, digest in recorded.items()
    )


def simulation_build_slot(handle: TargetHandle, variant: str = "") -> Path:
    """Return a selector-independent, checkout-local slot for one Target variant."""
    parent = work_root_for(handle.project_root, "sim", "slot").parent
    identity = f"{handle.identity}\0{variant}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return parent / digest[:20]


def preview_generation_root(handle: TargetHandle, variant: str = "") -> Path:
    """Describe an unallocated generation path without touching slot state."""
    return simulation_build_slot(handle, variant) / _GENERATION_DIR / "<generation>"


class SimulationBuildSession(AbstractContextManager["SimulationBuildSession"]):
    """Hold one Target slot across preparation, selected tests, and artifact capture."""

    def __init__(self, handle: TargetHandle, variant: str = "") -> None:
        self.handle = handle
        self.variant = variant
        self.slot = simulation_build_slot(handle, variant)
        self._lock: IO[str] | None = None
        self._lease_token: contextvars.Token[int | None] | None = None
        self.cache_decision: CacheDecision = CacheDecision.UNEXAMINED
        # Set by the last Verilator ``reusable_key``; scopes read-closure paths.
        self._verilator_identity: build_reuse.VerilatorIdentity | None = None
        # Wall-clock time each generation's inputs were first captured.
        self._captured_ns: dict[Path, int] = {}

    def __enter__(self) -> SimulationBuildSession:
        try:
            self.slot.mkdir(parents=True, exist_ok=True)
            lock = (self.slot / "lease.lock").open("a+", encoding="utf-8")
            try:
                wait_for_file_lock(lock, timeout_s=60)
            except BaseException:
                lock.close()
                raise
            self._lock = lock
            self._lease_token = set_child_lease_fd(lock.fileno())
            return self
        except (OSError, RuntimeError) as exc:
            raise SimulationBuildSlotError(
                f"cannot lease Simulation slot {self.slot}: {exc}"
            ) from exc

    def new_generation(self) -> Path:
        """Allocate an empty, never-reused FuseSoC output root under the lease."""
        if self._lock is None:
            raise SimulationBuildSlotError("Simulation slot is not leased")
        generations = self.slot / _GENERATION_DIR
        try:
            generations.mkdir(exist_ok=True)
            candidate = generations / secrets.token_hex(8)
            candidate.mkdir()
            return candidate
        except OSError as exc:
            raise SimulationBuildSlotError(
                f"cannot allocate Simulation generation: {exc}"
            ) from exc

    def capture_inputs(self, prepared: PreparedSimulationBuild) -> Mapping[str, str]:
        """Capture the bytes present immediately before compilation."""
        self._captured_ns.setdefault(prepared.work_root, time.time_ns())
        return snapshot_build_inputs(prepared)

    def reusable_key(
        self, prepared: PreparedSimulationBuild, inputs: Mapping[str, str], *, hooks: bool
    ) -> str | None:
        """Return a key only for an input and installation closure we can prove.

        *hooks* reports that Pre-Sim Commands wrote into this generation; such
        a candidate is never reused because the hook output would be lost.
        """
        # A declined call must not leave an earlier identity scoping closures.
        self._verilator_identity = None
        if hooks:
            return None
        environment = os.environ
        if prepared.eda_tool == "verilator":
            keyed = _verilator_reuse_key(prepared, inputs, environment)
            if keyed is None:
                return None
            key, self._verilator_identity = keyed
            return key
        return _reuse_input_key(
            prepared, inputs, self.variant, self.handle.project_root, environment
        )

    def try_reuse(
        self, candidate: PreparedSimulationBuild, key: str | None
    ) -> PreparedSimulationBuild | None:
        """Validate the current pointer and reconstruct its resolved build data."""
        if key is None:
            self.cache_decision = CacheDecision.REUSE_UNSUPPORTED
            return None
        self.cache_decision = CacheDecision.MISSING_PROVENANCE
        try:
            pointer = json.loads((self.slot / "current.json").read_text(encoding="utf-8"))
            root = self.slot / _GENERATION_DIR / pointer["generation"]
            build_root = root / pointer["build_root"]
            manifest = json.loads(
                (build_root / ".booley-build-manifest.json").read_text(encoding="utf-8")
            )
            rejection = self._hit_rejection(root, build_root, manifest, key)
            if rejection is None and not self._read_closure_matches(root, manifest, candidate):
                rejection = CacheDecision.CHANGED_SOURCE
            if rejection is not None:
                self.cache_decision = rejection
                return None
            selected = self._restore_prepared_build(root, candidate)
            self.cache_decision = CacheDecision.HIT
            return selected
        except (OSError, KeyError, TypeError, ValueError, AttributeError, RuntimeError):
            # A record Booley wrote is parseable; anything else is missing provenance.
            return None

    def retained_artifacts(self, prepared: PreparedSimulationBuild) -> tuple[Path, ...]:
        """Return the launch artifacts of an image ``try_reuse`` just verified."""
        return _runtime_artifacts(prepared)

    def _read_closure_matches(
        self, root: Path, manifest: Mapping[str, object], candidate: PreparedSimulationBuild
    ) -> bool:
        """Re-hash every file a retained Verilator build reported reading."""
        if candidate.eda_tool != "verilator":
            return True
        identity = self._verilator_identity
        if identity is None:
            return False
        return build_reuse.verify_read_closure(
            manifest.get("read_closure"), root, identity, self.handle.project_root
        )

    def _restore_prepared_build(
        self, root: Path, candidate: PreparedSimulationBuild
    ) -> PreparedSimulationBuild:
        edam_rel = candidate.resolved.edam_path.relative_to(candidate.work_root)
        resolved = fusesoc_registry.parse_edam(
            root / edam_rel,
            target=candidate.resolved.name,
            vlnv=candidate.resolved.vlnv,
        )
        make_rel = edam_layer.relpath_for_make(resolved.build_root, self.handle.project_root)
        return PreparedSimulationBuild(
            target=candidate.target,
            target_identity=candidate.target_identity,
            resolved=resolved,
            work_root=root,
            build_root=resolved.build_root,
            eda_tool=candidate.eda_tool,
            toplevel=candidate.toplevel,
            make_argv=tuple(edam_layer.make_command(make_rel)),
            environment=candidate.environment,
            fileset=candidate.fileset,
            core_closure=candidate.core_closure,
        )

    def discard_candidate(self, root: Path) -> None:
        """Remove the unused fresh generation after a verified hit."""
        try:
            if root.exists():
                shutil.rmtree(root)
        except OSError as exc:
            raise SimulationBuildSlotError(f"cannot discard unused candidate: {exc}") from exc

    @staticmethod
    def _hit_rejection(
        root: Path, build_root: Path, manifest: Mapping[str, Any], key: str
    ) -> CacheDecision | None:
        """Name why a manifest does not match the candidate, or ``None`` for a hit."""
        if manifest.get("reusable") is not True or manifest.get("input_key") != key:
            return CacheDecision.CHANGED_KEY
        if not _matching_hashes(root, manifest["inputs"]):
            return CacheDecision.CHANGED_SOURCE
        if not _matching_hashes(build_root, manifest["artifacts"]):
            return CacheDecision.CHANGED_ARTIFACT
        return None

    def authorize_fresh_image(
        self,
        prepared: PreparedSimulationBuild,
        inputs: Mapping[str, str],
        key: str | None = None,
    ) -> tuple[Path, ...]:
        """Record the built image and return its launch artifact paths."""
        try:
            current_inputs = snapshot_build_inputs(prepared)
            if any(current_inputs.get(name) != digest for name, digest in inputs.items()):
                raise SimulationBuildSlotError("Simulation build input changed during compilation")
            closure, reason = self._fresh_read_closure(prepared, key)
            if closure is None:
                key = None
            artifacts = _runtime_artifacts(prepared)
            hashes = {
                path.relative_to(prepared.build_root).as_posix(): hash_file(path)
                for path in artifacts
            }
            self._write_manifest(prepared, inputs, key, hashes, closure or {}, reason)
            if key is not None:
                self._promote_pointer(prepared)
            return artifacts
        except (OSError, RuntimeError) as exc:
            raise SimulationBuildSlotError(f"cannot record Simulation image: {exc}") from exc

    def _fresh_read_closure(
        self, prepared: PreparedSimulationBuild, key: str | None
    ) -> tuple[dict[str, str] | None, str]:
        """Record what a keyed build read; ``None`` downgrades it to non-reusable."""
        unproven = "complete compiler input closure not established"
        if key is None:
            return None, unproven
        if prepared.eda_tool != "verilator":
            return {}, ""
        identity = self._verilator_identity
        captured_ns = self._captured_ns.get(prepared.work_root)
        if identity is None or captured_ns is None:
            return None, unproven
        try:
            closure = build_reuse.collect_read_closure(
                prepared.build_root,
                prepared.work_root,
                identity,
                self.handle.project_root,
                captured_ns,
            )
            unkeyed = build_reuse.unkeyed_build_environment(
                prepared.build_root, identity, _ambient_environment(prepared)
            )
        except (build_reuse.ReadClosureError, OSError, UnicodeError) as exc:
            return None, f"{unproven}: {exc}"
        if unkeyed:
            return None, f"{unproven}: generated makefiles read unkeyed environment {unkeyed}"
        return closure, ""

    @staticmethod
    def _write_manifest(
        prepared: PreparedSimulationBuild,
        inputs: Mapping[str, str],
        key: str | None,
        hashes: Mapping[str, str],
        read_closure: Mapping[str, str],
        reason: str,
    ) -> None:
        manifest = {
            "schema": 1,
            "target_identity": prepared.target_identity,
            "generation": prepared.work_root.name,
            "reusable": key is not None,
            "reason": reason,
            "input_key": key,
            "inputs": dict(inputs),
            "artifacts": dict(hashes),
            "read_closure": dict(read_closure),
        }
        root = prepared.build_root
        temporary = root / f".booley-build-manifest-{secrets.token_hex(8)}.tmp"
        temporary.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
        temporary.replace(root / ".booley-build-manifest.json")

    def _promote_pointer(self, prepared: PreparedSimulationBuild) -> None:
        pointer = {
            "generation": prepared.work_root.name,
            "build_root": prepared.build_root.relative_to(prepared.work_root).as_posix(),
        }
        temporary = self.slot / f".current-{secrets.token_hex(8)}.tmp"
        temporary.write_text(json.dumps(pointer, sort_keys=True), encoding="utf-8")
        temporary.replace(self.slot / "current.json")

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        lock = self._lock
        self._lock = None
        token = self._lease_token
        self._lease_token = None
        if token is not None:
            reset_child_lease_fd(token)
        if lock is not None:
            try:
                release_file_lock(lock)
            finally:
                lock.close()


__all__ = [
    "CacheDecision",
    "SimulationBuildSession",
    "SimulationBuildSlotError",
    "TargetCompileSurface",
    "preview_generation_root",
    "project_compile_surface",
    "resolve_target_compile_surface",
    "simulation_build_slot",
    "snapshot_build_inputs",
    "snapshot_generation_files",
    "verify_existing_build_inputs",
]
