"""Materialize Target-declared runtime inputs at the Simulation execution seam."""

from __future__ import annotations

import filecmp
import shutil
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

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


__all__ = [
    "RuntimeInputError",
    "declared_runtime_inputs",
    "materialize_runtime_inputs",
    "preview_runtime_inputs",
]
