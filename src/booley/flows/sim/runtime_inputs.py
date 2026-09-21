"""Materialize Target-declared runtime inputs at the Simulation execution seam."""

from __future__ import annotations

import filecmp
import hashlib
import shutil
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from booley.flows.sim.campaign_durability import durable_copy, durable_directory

if TYPE_CHECKING:
    from booley.fusesoc.fusesoc_registry import ResolvedTarget
    from booley.targets.domain import TargetInput

_OVERLAY_NAME = ".booley-runtime-inputs"


class RuntimeInputError(RuntimeError):
    """A declared staged input cannot be made available safely."""


@dataclass(frozen=True)
class _RuntimeInput:
    relative: Path
    source: Path
    overlay: Path


@dataclass(frozen=True)
class RuntimeInputBinding:
    """Authenticated attempt-owned input and its exposed destination."""

    declaration_id: str
    authoritative_copy: Path
    destination: str
    method: str
    bytes: int
    sha256: str
    owned: bool

    def result_document(self, *, reference_path: str, owner: str) -> dict[str, object]:
        """Return the exact durable Result binding."""
        return {
            "declaration_id": self.declaration_id,
            "authoritative_copy": {
                "path": reference_path,
                "bytes": self.bytes,
                "sha256": self.sha256,
                "kind": "runtime_input",
                "owner": owner,
            },
            "destination": self.destination,
            "method": self.method,
            "destination_bytes": self.bytes,
            "destination_sha256": self.sha256,
            "owned": self.owned,
        }


def declared_runtime_inputs(resolved: ResolvedTarget) -> tuple[str, ...]:
    """Return build-relative ``user`` files staged by FuseSoC ``copyto``."""
    return tuple(
        file.name
        for file in resolved.files
        if file.file_type.lower() == "user" and not Path(file.name).is_absolute()
    )


def preview_runtime_inputs(inputs: Iterable[TargetInput]) -> tuple[str, ...]:
    """Return declared ``copyto`` destinations before FuseSoC resolution."""
    return tuple(
        str(copyto)
        for item in inputs
        if item.file_type.lower() == "user"
        and (copyto := item.attributes.get("copyto"))
        and not Path(str(copyto)).is_absolute()
    )


def _safe_child(root: Path, relative: Path, *, label: str) -> Path:
    text = str(relative)
    if not text or relative.is_absolute() or ".." in relative.parts:
        raise RuntimeInputError(f"invalid {label} path {text!r}")
    candidate = root.joinpath(*relative.parts)
    if not candidate.parent.resolve().is_relative_to(root):
        raise RuntimeInputError(f"{label} path escapes its root: {text!r}")
    return candidate


def _prepare_overlay(build: Path, inputs: tuple[str, ...]) -> tuple[_RuntimeInput, ...]:
    overlay_root = build / _OVERLAY_NAME
    prepared = []
    try:
        if overlay_root.is_symlink():
            raise RuntimeInputError("runtime input overlay conflicts with a symlink")
        if overlay_root.exists():
            shutil.rmtree(overlay_root)
        for value in inputs:
            relative = Path(value)
            if _OVERLAY_NAME in relative.parts:
                raise RuntimeInputError(f"invalid build input path {value!r}")
            source = _safe_child(build, relative, label="build input").resolve()
            if not source.is_relative_to(build) or not source.is_file():
                raise RuntimeInputError(f"declared staged input is missing: {value}")
            overlay = _safe_child(overlay_root, relative, label="runtime input overlay")
            overlay.parent.mkdir(parents=True, exist_ok=True)
            if not overlay.exists() and not overlay.is_symlink():
                overlay.symlink_to(source)
            elif not overlay.is_symlink() or overlay.resolve() != source:
                raise RuntimeInputError(f"runtime input overlay conflicts: {value}")
            prepared.append(_RuntimeInput(relative, source, overlay))
    except OSError as exc:
        raise RuntimeInputError(f"could not prepare runtime input overlay: {exc}") from exc
    return tuple(prepared)


def _link_target(link: Path) -> Path:
    raw = link.readlink()
    return raw if raw.is_absolute() else link.parent / raw


def _is_owned_link(link: Path, relative: Path, overlay_root: Path) -> bool:
    if not link.is_symlink():
        return False
    return _link_target(link) == overlay_root / relative


def _replace_owned_link(destination: Path, target: Path) -> tuple[Path, Path]:
    if destination.readlink() != target:
        destination.unlink()
        destination.symlink_to(target, target_is_directory=target.is_dir())
    return destination, destination.readlink()


def _stage_one(run: Path, item: _RuntimeInput, overlay_root: Path) -> tuple[Path, Path] | None:
    parent = run
    parts = item.relative.parts
    for index, part in enumerate(parts[:-1], start=1):
        candidate = parent / part
        relative = Path(*parts[:index])
        overlay = item.overlay.parents[len(parts) - index - 1]
        if _is_owned_link(candidate, relative, overlay_root):
            return _replace_owned_link(candidate, overlay)
        if candidate.is_symlink():
            raise RuntimeInputError(f"run input conflicts with an existing path: {item.relative}")
        if candidate.is_dir():
            parent = candidate
            continue
        if candidate.exists() or candidate.is_symlink():
            raise RuntimeInputError(f"run input conflicts with an existing path: {item.relative}")
        candidate.symlink_to(overlay, target_is_directory=True)
        return candidate, candidate.readlink()

    destination = parent / parts[-1]
    if _is_owned_link(destination, item.relative, overlay_root):
        return _replace_owned_link(destination, item.overlay)
    if destination.exists() or destination.is_symlink():
        if destination.is_file() and filecmp.cmp(item.source, destination, shallow=False):
            return None
        raise RuntimeInputError(f"run input conflicts with an existing path: {item.relative}")
    destination.symlink_to(item.overlay)
    return destination, destination.readlink()


def _remove_staged(items: Iterable[tuple[Path, Path]]) -> None:
    for destination, target in reversed(tuple(items)):
        if destination.is_symlink() and destination.readlink() == target:
            destination.unlink()


def _clean_runtime_state(staged: list[tuple[Path, Path]], overlay_root: Path) -> None:
    failure: OSError | None = None
    try:
        _remove_staged(staged)
    except OSError as exc:
        failure = exc
    if not overlay_root.is_symlink():
        try:
            shutil.rmtree(overlay_root)
        except FileNotFoundError:
            pass
        except OSError as exc:
            failure = failure or exc
    if failure is not None:
        raise RuntimeInputError(
            f"could not clean simulation runtime inputs: {failure}"
        ) from failure


@contextmanager
def materialize_runtime_inputs(
    build_dir: Path,
    run_cwd: Path,
    inputs: Iterable[str],
) -> Iterator[None]:
    """Expose declared build-staged inputs in ``run_cwd`` for one run."""
    declared = tuple(inputs)
    if not declared:
        yield
        return
    build = build_dir.resolve()
    run = run_cwd.resolve()
    if not build.is_dir():
        raise RuntimeInputError(f"simulation build directory does not exist: {build}")
    if not run.is_dir():
        raise RuntimeInputError(f"simulation run directory does not exist: {run}")
    overlay_root = build / _OVERLAY_NAME
    staged: list[tuple[Path, Path]] = []
    try:
        for item in _prepare_overlay(build, declared):
            link = _stage_one(run, item, overlay_root)
            if link is not None and link[0] not in {path for path, _ in staged}:
                staged.append(link)
        yield
    except OSError as exc:
        raise RuntimeInputError(f"could not expose runtime input: {exc}") from exc
    finally:
        _clean_runtime_state(staged, overlay_root)


@contextmanager
def materialize_campaign_runtime_inputs(  # noqa: PLR0912,PLR0915 -- ordered integrity transaction
    *,
    bundle_root: Path,
    attempt_root: Path,
    run_cwd: Path,
    declarations: Iterable[dict[str, str]],
    owned_run_directory: bool,
) -> Iterator[tuple[RuntimeInputBinding, ...]]:
    """Copy declared inputs into one attempt, then expose and clean them safely.

    The attempt copy is authoritative.  A literal cwd receives temporary
    copies; an owned templated cwd receives the authoritative copies directly.
    No path is sourced from the mutable bundle again after this step.
    """
    copies_root = attempt_root / "runtime-inputs"
    if copies_root.exists():
        raise RuntimeInputError("attempt runtime-input directory already exists")
    durable_directory(copies_root)
    bindings: list[RuntimeInputBinding] = []
    owned_destinations: list[Path] = []
    owned_directories: list[Path] = []
    try:
        for declaration in declarations:
            required = {"declaration_id", "source_artifact_path", "destination"}
            if set(declaration) != required:
                raise RuntimeInputError(
                    "runtime input declaration has unexpected fields"
                )
            source_relative = _safe_relative(declaration["source_artifact_path"])
            destination_relative = _safe_relative(declaration["destination"])
            source = bundle_root / source_relative
            if source.is_symlink() or not source.is_file():
                raise RuntimeInputError(f"declared runtime input is missing: {source_relative}")
            authoritative = copies_root / destination_relative
            authoritative.parent.mkdir(parents=True, exist_ok=True)
            durable_copy(source, authoritative)
            size, digest = _runtime_identity(authoritative)
            destination = run_cwd / destination_relative
            _prepare_destination_parent(
                run_cwd,
                destination_relative,
                owned_directories,
            )
            method = "copy"
            owned = False
            if owned_run_directory and destination.resolve(strict=False) == authoritative.resolve():
                method = "copy"
                owned = True
            elif destination.exists() or destination.is_symlink():
                if destination.is_file() and filecmp.cmp(
                    authoritative, destination, shallow=False
                ):
                    method = "identical_existing"
                else:
                    raise RuntimeInputError(
                        f"run input conflicts with an existing path: {destination_relative}"
                    )
            else:
                shutil.copyfile(authoritative, destination, follow_symlinks=False)
                owned_destinations.append(destination)
                owned = True
            exposed_size, exposed_digest = _runtime_identity(destination)
            if (exposed_size, exposed_digest) != (size, digest):
                raise RuntimeInputError(
                    f"runtime input authentication failed: {destination_relative}"
                )
            bindings.append(
                RuntimeInputBinding(
                    declaration["declaration_id"],
                    authoritative,
                    destination_relative.as_posix(),
                    method,
                    size,
                    digest,
                    owned,
                )
            )
        yield tuple(bindings)
    finally:
        for destination in reversed(owned_destinations):
            if destination.is_file() and not destination.is_symlink():
                destination.unlink()
        for directory in reversed(owned_directories):
            try:
                directory.rmdir()
            except FileNotFoundError:
                pass
            except OSError:
                # A consumer-created sibling is not owned by this attempt.
                # Never recurse through a literal Project directory to remove it.
                pass


def _prepare_destination_parent(
    run_cwd: Path,
    destination: Path,
    owned_directories: list[Path],
) -> None:
    """Create and remember only missing literal-cwd parent directories."""
    current = run_cwd
    for component in destination.parts[:-1]:
        current /= component
        if current.is_symlink():
            raise RuntimeInputError(
                f"run input destination contains a symlink: {destination}"
            )
        if current.exists():
            if not current.is_dir():
                raise RuntimeInputError(
                    f"run input destination parent is not a directory: {destination}"
                )
            continue
        current.mkdir()
        owned_directories.append(current)


def _safe_relative(value: str) -> Path:
    path = Path(value)
    if (
        not value
        or "\\" in value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise RuntimeInputError(f"invalid runtime input path: {value!r}")
    return path


def _runtime_identity(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(64 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, "sha256:" + digest.hexdigest()


__all__ = [
    "RuntimeInputBinding",
    "RuntimeInputError",
    "declared_runtime_inputs",
    "materialize_campaign_runtime_inputs",
    "materialize_runtime_inputs",
    "preview_runtime_inputs",
]
