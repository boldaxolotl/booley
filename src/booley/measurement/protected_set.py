"""The protected set of a measured tree: the paths whose edits a Measurement reports.

Computed on the measured checkout (the commit export), so it is a fact of the tree
and the same on every machine. Entries are sorted, unique, ``/``-separated and
relative to the checkout root; a trailing ``/`` marks a directory prefix. The set
holds the Project's configuration files, every Target's ``.core`` file and
testbench files, the files its ``[project].protected_inputs`` declarations match,
its approved coverage waiver directory, and its timing constraint files.
"""

from __future__ import annotations

import os
import posixpath
import tomllib
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from booley.config.coverage_waiver_inputs import parse_coverage_waiver_config
from booley.config.protected_inputs import declaration_matches, parse_protected_inputs
from booley.core.booley_dir import own_booley_dir, own_booley_dir_relative
from booley.core.config_paths import resolve_toml
from booley.measurement.base import is_constraint
from booley.targets.catalog import TargetCatalog

_CONFIG_FILES = ("booley.toml", "pipeline.toml", "tests.toml", "metrics.toml", "reviews.toml")


class UnpinnableInputError(ValueError):
    """A configured input lives outside the measured checkout, so no commit pins it."""


def _checkout_paths(root: Path) -> list[str]:
    """Every non-directory entry of the checkout; a symlink to a directory is one path."""
    paths: list[str] = []
    for directory, subdirectories, names in os.walk(root, followlinks=False):
        here = Path(directory)
        links = [name for name in subdirectories if (here / name).is_symlink()]
        for name in (*names, *links):
            paths.append((here / name).relative_to(root).as_posix())
    return paths


def _read_config(root: Path) -> dict[str, Any]:
    """The checkout's own effective ``booley.toml``; empty when it has none."""
    try:
        with resolve_toml(own_booley_dir(root)).open("rb") as stream:
            return tomllib.load(stream)
    except FileNotFoundError:
        return {}


def _target_paths(root: Path) -> set[str]:
    """Each visible Target's ``.core`` file and testbench files, inside the checkout."""
    catalog = TargetCatalog.build(root)
    found: set[str] = set()
    for handle in catalog.list():
        found.add(_relative_to_root(root, handle.core_file))
        found.update(_relative_to_root(root, name) for name in catalog.inspect(handle).tb_files)
    found.discard("")
    return found


def _relative_to_root(root: Path, path: Path | str) -> str:
    """*path* (absolute, or relative to the root) as a root-relative POSIX path, else ``""``."""
    normal = Path(os.path.normpath(root / path))
    return normal.relative_to(root).as_posix() if normal.is_relative_to(root) else ""


def _declared_paths(declarations: tuple[str, ...], paths: list[str]) -> set[str]:
    """Files the declarations match, plus each literal ``dir/`` declaration as a prefix."""
    found = {
        path for path in paths if any(declaration_matches(rule, path) for rule in declarations)
    }
    found.update(rule for rule in declarations if rule.endswith("/") and not set("*?") & set(rule))
    return found


def _escapes_checkout(parent: tuple[str, ...], text: str) -> bool:
    """Whether a committed link's text leaves the checkout; reads the text, never the target."""
    if PurePosixPath(text).is_absolute() or PureWindowsPath(text).drive:
        return True
    joined = posixpath.normpath(posixpath.join(*parent, text))
    return joined == ".." or joined.startswith("../")


def _waiver_directory(root: Path, config: dict[str, Any]) -> str | None:
    """The coverage waiver directory as ``<rel>/``, or None when none is configured.

    Raises:
        UnpinnableInputError: a path component is a committed symlink out of the checkout.
    """
    waivers = parse_coverage_waiver_config(config)
    if waivers is None:
        return None
    anchor = root if waivers.anchor == "rtl_repository" else own_booley_dir(root)
    relative = PurePosixPath(*(anchor / waivers.directory).relative_to(root).parts)
    current = root
    for index, part in enumerate(relative.parts):
        current = current / part
        if not current.is_symlink():
            continue
        text = str(current.readlink())
        if _escapes_checkout(relative.parts[:index], text):
            raise UnpinnableInputError(
                f"coverage waiver directory {relative} points outside the measured checkout "
                f"through the committed link {PurePosixPath(*relative.parts[: index + 1])} -> "
                f"{text}; it cannot be pinned to a commit"
            )
    return f"{relative}/"


def protected_set(checkout: Path) -> tuple[str, ...]:
    """The protected set of the measured *checkout*.

    Raises:
        UnpinnableInputError: the coverage waiver directory is a committed link to a
            location outside the checkout.
        ValueError: ``[project].protected_inputs`` or ``[coverage.waivers]`` is invalid.
    """
    root = checkout.resolve()
    place = own_booley_dir_relative(root)
    config = _read_config(root)
    paths = _checkout_paths(root)
    entries = {"booley.toml", *(f"{place}/{name}" for name in _CONFIG_FILES)}
    entries |= _target_paths(root)
    entries |= _declared_paths(parse_protected_inputs(config), paths)
    entries |= {path for path in paths if is_constraint(path)}
    waiver_directory = _waiver_directory(root, config)
    if waiver_directory is not None:
        entries.add(waiver_directory)
    return tuple(sorted(entries))
