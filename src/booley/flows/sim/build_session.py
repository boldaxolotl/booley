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
import subprocess
from collections.abc import Mapping
from contextlib import AbstractContextManager
from pathlib import Path
from typing import IO

import yaml

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
from booley.targets.domain import TargetHandle

from .build import PreparedSimulationBuild


class SimulationBuildSlotError(RuntimeError):
    """A Simulation build slot cannot be used safely."""


_GENERATION_DIR = "g"


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_elf(path: Path) -> bool:
    with path.open("rb") as stream:
        return stream.read(4) == b"\x7fELF"


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


def _build_input_hashes(prepared: PreparedSimulationBuild) -> dict[str, str]:
    """Snapshot files already present in a newly resolved generation."""
    root = prepared.work_root
    hashes: dict[str, str] = {}
    try:
        for path in sorted(root.rglob("*")):
            if ".booley-runtime-inputs" in path.parts:
                continue
            if path.is_symlink():
                raise SimulationBuildSlotError(f"symlinked generated build input: {path}")
            if path.is_dir():
                continue
            if path.name == RUN_LOG_NAME or path.name.startswith((".booley-adapter-", ".a-")):
                continue
            if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
                raise SimulationBuildSlotError(f"unsafe generated build input: {path}")
            relative = path.relative_to(root).as_posix()
            hashes[relative] = _hash_file(path)
        return hashes
    except OSError as exc:
        raise SimulationBuildSlotError(f"cannot read generated build input: {exc}") from exc


def _raise_walk_error(exc: OSError) -> None:
    raise exc


def project_compile_surface(project_root: Path) -> dict[str, str]:
    """Snapshot Project HDL and core metadata around setup and Pre-Sim Commands."""
    suffixes = {".core", ".v", ".sv", ".vh", ".svh"}
    result: dict[str, str] = {}
    runtime_root = (project_root / ".booley_project" / ".runtime").resolve()
    try:
        for directory, children, names in os.walk(project_root, onerror=_raise_walk_error):
            root = Path(directory)
            children[:] = [
                name
                for name in children
                if name != ".git" and (root / name).resolve() != runtime_root
            ]
            for name in names:
                path = root / name
                if path.suffix.lower() not in suffixes:
                    continue
                identity = path.relative_to(project_root).as_posix()
                if path.is_symlink():
                    identity += f" -> {path.readlink()}"
                if not path.is_file():
                    raise SimulationBuildSlotError(f"Project compile input is not a file: {path}")
                result[identity] = _hash_file(path)
    except OSError as exc:
        raise SimulationBuildSlotError(f"cannot capture Project compile inputs: {exc}") from exc
    return result


def _elf_dependencies(path: Path) -> tuple[Path, ...] | None:
    """List dynamic libraries for a local ELF executable, or decline reuse."""
    if not _is_elf(path):
        return None
    result = subprocess.run(
        ["ldd", str(path)], capture_output=True, text=True, timeout=10, check=False
    )
    if result.returncode != 0 or "not found" in result.stdout:
        return None
    paths = []
    for line in result.stdout.splitlines():
        match = re.search(r"(/\S+)", line)
        if match is not None:
            paths.append(Path(match.group(1)))
    return tuple(paths)


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
            if not _is_elf(path):
                continue
            dependencies = _elf_dependencies(path)
            if dependencies is None:
                return None
            paths.extend(dependencies)
        digest = hashlib.sha256()
        for path in sorted(set(paths)):
            if not path.is_file():
                return None
            digest.update(str(path).encode("utf-8"))
            digest.update(str(path.resolve()).encode("utf-8"))
            digest.update(_hash_file(path).encode("ascii"))
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
                    digest.update(_hash_file(path).encode("ascii"))
        return digest.hexdigest()
    except OSError:
        return None


def _has_generated_core_behavior(core_data: object) -> bool:
    if not isinstance(core_data, dict):
        return True
    if any(
        core_data.get(name) for name in ("generators", "generate", "scripts", "provider", "hooks")
    ):
        return True
    targets = core_data.get("targets", {})
    if not isinstance(targets, dict):
        return True
    return any(
        not isinstance(target, dict) or target.get("generate") or target.get("hooks")
        for target in targets.values()
    )


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
        if _hash_file(original) != _hash_file(source):
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
    core_file = core_path.resolve()
    if core_path.is_symlink() or not core_file.is_relative_to(project_root.resolve()):
        return None
    core_data = yaml.safe_load(core_file.read_text(encoding="utf-8"))
    if (
        _has_generated_core_behavior(core_data)
        or _has_unsupported_source(prepared, core_file)
        or _has_unsupported_icarus_recipe(prepared)
    ):
        return None
    return core_file


def _reuse_input_key(
    prepared: PreparedSimulationBuild,
    inputs: Mapping[str, str],
    variant: str,
    project_root: Path,
) -> str | None:
    """Recognize the first closed, ordinary Icarus recipe; decline all others."""
    if (
        variant
        or prepared.eda_tool != "icarus"
        or prepared.resolved.cocotb_module
        or any(item.file_type.lower() == "user" for item in prepared.resolved.files)
    ):
        return None
    if any(os.environ.get(name) for name in ("EDALIZE_LAUNCHER", "MAKEFLAGS", "MFLAGS")):
        return None
    try:
        core_file = _eligible_core_file(prepared, project_root)
        if core_file is None:
            return None
        tool = _icarus_tool_identity()
        recipe = _python_recipe_identity()
        if tool is None or recipe is None:
            return None
        versions = {name: importlib.metadata.version(name) for name in ("fusesoc", "edalize")}
        environment = hashlib.sha256(
            json.dumps(sorted(os.environ.items())).encode("utf-8")
        ).hexdigest()
        record = {
            "schema": 1,
            "target": prepared.target_identity,
            "inputs": dict(inputs),
            "core": _hash_file(core_file),
            "core_path": str(core_file),
            "tool": tool,
            "recipe": recipe,
            "versions": versions,
            "environment": environment,
            "target_environment": hashlib.sha256(
                json.dumps(sorted(prepared.environment.items())).encode("utf-8")
            ).hexdigest(),
        }
        return hashlib.sha256(json.dumps(record, sort_keys=True).encode("utf-8")).hexdigest()
    except (
        OSError,
        KeyError,
        UnicodeError,
        yaml.YAMLError,
        importlib.metadata.PackageNotFoundError,
    ):
        return None


def _retained_build_root(slot: Path) -> tuple[Path, Path] | None:
    """Read a contained pointer to one successful generation."""
    current = slot / "current.json"
    if current.is_symlink():
        return None
    pointer = json.loads(current.read_text(encoding="utf-8"))
    generation = pointer["generation"]
    if not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{16}", generation):
        return None
    root = slot / _GENERATION_DIR / generation
    if root.is_symlink() or not root.is_dir() or not root.resolve().is_relative_to(slot.resolve()):
        return None
    relative = Path(pointer["build_root"])
    if relative.is_absolute() or ".." in relative.parts:
        return None
    build_root = root / relative
    if (
        build_root.is_symlink()
        or not build_root.is_dir()
        or not build_root.resolve().is_relative_to(root.resolve())
    ):
        return None
    return root, build_root


def _matching_hashes(root: Path, raw: object) -> bool:
    """Validate a manifest path map without following escape or symlink paths."""
    if not isinstance(raw, dict):
        return False
    for name, digest in raw.items():
        if not isinstance(name, str) or not isinstance(digest, str):
            return False
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            return False
        path = root / relative
        if (
            path.is_symlink()
            or not path.is_file()
            or not path.resolve().is_relative_to(root.resolve())
            or _hash_file(path) != digest
        ):
            return False
    return True


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
        self.cache_decision = "unexamined"

    def __enter__(self) -> SimulationBuildSession:
        try:
            self.slot.mkdir(parents=True, exist_ok=True)
            if self.slot.is_symlink() or not self.slot.resolve().is_relative_to(
                self.handle.project_root.resolve()
            ):
                raise SimulationBuildSlotError(f"unsafe Simulation slot path: {self.slot}")
            lock_path = self.slot / "lease.lock"
            if lock_path.is_symlink():
                raise SimulationBuildSlotError(f"Simulation lease is a symlink: {lock_path}")
            lock = lock_path.open("a+", encoding="utf-8")
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
            if generations.is_symlink():
                raise SimulationBuildSlotError(
                    f"generations directory is a symlink: {generations}"
                )
            candidate = generations / secrets.token_hex(8)
            candidate.mkdir()
            return candidate
        except OSError as exc:
            raise SimulationBuildSlotError(
                f"cannot allocate Simulation generation: {exc}"
            ) from exc

    def capture_inputs(self, prepared: PreparedSimulationBuild) -> Mapping[str, str]:
        """Capture the bytes present immediately before compilation."""
        if self._lock is None:
            raise SimulationBuildSlotError("Simulation slot is not leased")
        return _build_input_hashes(prepared)

    def reusable_key(
        self, prepared: PreparedSimulationBuild, inputs: Mapping[str, str], *, hooks: bool
    ) -> str | None:
        """Return a key only for an input and installation closure we can prove."""
        if hooks:
            return None
        return _reuse_input_key(prepared, inputs, self.variant, self.handle.project_root)

    def try_reuse(
        self, candidate: PreparedSimulationBuild, key: str | None
    ) -> PreparedSimulationBuild | None:
        """Validate the current pointer and reconstruct its resolved build data."""
        if self._lock is None or key is None:
            self.cache_decision = "reuse unsupported"
            return None
        self.cache_decision = "missing provenance"
        try:
            retained = _retained_build_root(self.slot)
            if retained is None:
                self.cache_decision = "malformed provenance"
                return None
            root, build_root = retained
            manifest_path = build_root / ".booley-build-manifest.json"
            if manifest_path.is_symlink():
                raise ValueError("manifest path is a symlink")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            rejection = self._hit_rejection(root, build_root, manifest, candidate, key)
            if rejection is not None:
                self.cache_decision = rejection
                return None
            selected = self._restore_prepared_build(root, candidate)
            actual = {
                path.relative_to(selected.build_root).as_posix()
                for path in _runtime_artifacts(selected)
            }
            if actual != set(manifest["artifacts"]):
                self.cache_decision = "changed artifact"
                return None
            self.cache_decision = "hit"
            return selected
        except (
            OSError,
            KeyError,
            TypeError,
            ValueError,
            RuntimeError,
            SimulationBuildSlotError,
        ) as exc:
            self.cache_decision = (
                "missing provenance"
                if isinstance(exc, FileNotFoundError)
                else "malformed provenance"
            )
            return None

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
        )

    def verify_fresh_image(self, prepared: PreparedSimulationBuild) -> None:
        """Reauthenticate a non-reusable image while holding its Target lease."""
        if self._lock is None:
            raise SimulationBuildSlotError("Simulation slot is not leased")
        if prepared.work_root.parent != self.slot / _GENERATION_DIR:
            raise SimulationBuildSlotError("Simulation image is outside its leased generation")
        manifest_path = prepared.build_root / ".booley-build-manifest.json"
        try:
            if manifest_path.is_symlink():
                raise SimulationBuildSlotError("Simulation manifest is a symlink")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict) or (
                manifest.get("schema") != 1
                or manifest.get("target_identity") != prepared.target_identity
                or manifest.get("generation") != prepared.work_root.name
                or manifest.get("reusable") is not False
            ):
                raise SimulationBuildSlotError("Simulation manifest identity changed")
            if not _matching_hashes(prepared.work_root, manifest.get("inputs")):
                raise SimulationBuildSlotError("Simulation build inputs changed")
            if not _matching_hashes(prepared.build_root, manifest.get("artifacts")):
                raise SimulationBuildSlotError("Simulation image changed")
            artifacts = manifest["artifacts"]
            actual = {
                path.relative_to(prepared.build_root).as_posix()
                for path in _runtime_artifacts(prepared)
            }
            if actual != set(artifacts):
                raise SimulationBuildSlotError("Simulation image set changed")
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise SimulationBuildSlotError(f"cannot verify Simulation image: {exc}") from exc

    def discard_candidate(self, root: Path) -> None:
        """Remove only the unused fresh generation after a verified hit."""
        if self._lock is None or root.parent != self.slot / _GENERATION_DIR or root.is_symlink():
            raise SimulationBuildSlotError(f"unsafe candidate cleanup path: {root}")
        try:
            shutil.rmtree(root)
            selftest_overlay.remove_doctor_shadow(self.handle.project_root, root)
        except OSError as exc:
            raise SimulationBuildSlotError(f"cannot discard unused candidate: {exc}") from exc

    @staticmethod
    def _hit_rejection(
        root: Path,
        build_root: Path,
        manifest: object,
        candidate: PreparedSimulationBuild,
        key: str,
    ) -> str | None:
        if not isinstance(manifest, dict):
            return "malformed manifest"
        if (
            manifest.get("schema") != 1
            or manifest.get("target_identity") != candidate.target_identity
        ):
            return "schema mismatch" if manifest.get("schema") != 1 else "wrong Target"
        if (
            manifest.get("generation") != root.name
            or manifest.get("input_key") != key
            or manifest.get("reusable") is not True
        ):
            return "changed source, recipe, or tool"
        if not _matching_hashes(root, manifest.get("inputs")):
            return "changed source"
        if not _matching_hashes(build_root, manifest.get("artifacts")):
            return "changed artifact"
        return None

    def authorize_fresh_image(
        self,
        prepared: PreparedSimulationBuild,
        inputs: Mapping[str, str],
        key: str | None = None,
    ) -> None:
        """Record and check the image from this authenticated fresh compilation."""
        if self._lock is None:
            raise SimulationBuildSlotError("Simulation slot is not leased")
        root = prepared.build_root.resolve()
        if not root.is_relative_to((self.slot / _GENERATION_DIR).resolve()):
            raise SimulationBuildSlotError(f"build root escaped leased slot: {root}")
        try:
            current_inputs = _build_input_hashes(prepared)
            if any(current_inputs.get(name) != digest for name, digest in inputs.items()):
                raise SimulationBuildSlotError("Simulation build input changed during compilation")
            if key is not None and self.reusable_key(prepared, inputs, hooks=False) != key:
                raise SimulationBuildSlotError("Simulation recipe changed during compilation")
            artifacts = _runtime_artifacts(prepared)
            hashes = {}
            for path in artifacts:
                if (
                    path.is_symlink()
                    or not path.is_file()
                    or not path.resolve().is_relative_to(root)
                ):
                    raise SimulationBuildSlotError(f"unsafe Simulation image: {path}")
                hashes[path.relative_to(root).as_posix()] = _hash_file(path)
            self._write_manifest(prepared, inputs, key, hashes)
            for name, digest in hashes.items():
                if _hash_file(root / name) != digest:
                    raise SimulationBuildSlotError(
                        f"Simulation image changed before launch: {name}"
                    )
            if key is not None:
                self._promote_pointer(prepared)
        except (OSError, RuntimeError) as exc:
            raise SimulationBuildSlotError(f"cannot authorize Simulation image: {exc}") from exc

    @staticmethod
    def _write_manifest(
        prepared: PreparedSimulationBuild,
        inputs: Mapping[str, str],
        key: str | None,
        hashes: Mapping[str, str],
    ) -> None:
        manifest = {
            "schema": 1,
            "target_identity": prepared.target_identity,
            "generation": prepared.work_root.name,
            "reusable": key is not None,
            "reason": "" if key is not None else "complete compiler input closure not established",
            "input_key": key,
            "inputs": dict(inputs),
            "artifacts": dict(hashes),
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
    "SimulationBuildSession",
    "SimulationBuildSlotError",
    "preview_generation_root",
    "project_compile_surface",
    "simulation_build_slot",
]
