"""Resolve the source-scoped part of a Reviewer invocation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

_CPP_SUFFIXES = (".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx")
_HDL_SUFFIXES = (".v", ".vh", ".sv", ".svh")


class ReviewContractError(ValueError):
    """The requested review scope cannot be classified safely."""


@dataclass(frozen=True)
class ReviewScopeContract:
    """File-language facts which select review guidance and policy."""

    cocotb_files: frozenset[str] = frozenset()
    hdl_files: frozenset[str] = frozenset()
    support_files: frozenset[str] = frozenset()

    @property
    def has_cocotb(self) -> bool:
        return bool(self.cocotb_files)

    @property
    def has_hdl(self) -> bool:
        return bool(self.hdl_files)

    @property
    def has_cpp(self) -> bool:
        return any(
            PurePosixPath(path).suffix.lower() in _CPP_SUFFIXES for path in self.support_files
        )

    @property
    def simulation_sources(self) -> tuple[str, ...]:
        """Sorted scoped HDL, cocotb, and C/C++ files; support files are omitted."""
        cpp = {
            path
            for path in self.support_files
            if PurePosixPath(path).suffix.lower() in _CPP_SUFFIXES
        }
        return tuple(sorted(self.cocotb_files | self.hdl_files | cpp))

    def is_cocotb_file(self, path: str) -> bool:
        """Return whether *path* is a Python testbench in this scope."""
        return normalize_scope_path(path) in self.cocotb_files

    def contains_file(self, path: str) -> bool:
        """Return whether *path* is one of the explicitly scoped sources."""
        normalized = normalize_scope_path(path)
        return (
            normalized in self.cocotb_files
            or normalized in self.hdl_files
            or normalized in self.support_files
        )


def normalize_scope_path(path: str) -> str:
    """Return *path* with ``/`` separators and no leading ``./``."""
    value = path.replace("\\", "/").removeprefix("./")
    return str(PurePosixPath(value))


def reviewable(path: str, category: str) -> bool:
    """Whether a ``--scope`` of *category* accepts the file kind of *path*."""
    suffix = PurePosixPath(normalize_scope_path(path)).suffix.lower()
    if suffix in _HDL_SUFFIXES:
        return True
    return category == "tb" and (suffix == ".py" or suffix in _CPP_SUFFIXES)


def accepted_scope_kinds(category: str) -> str:
    """Return the file kinds ``--scope`` accepts for *category*, for messages and help."""
    hdl = ", ".join(_HDL_SUFFIXES)
    if category != "tb":
        return hdl
    return f"{hdl}, .py and C/C++ ({', '.join(_CPP_SUFFIXES)})"


def resolve_review_scope(
    scope: list[str], *, category: str, allow_support: bool = False
) -> ReviewScopeContract:
    """Classify the declared scope without consulting a build Target."""
    if not scope:
        raise ReviewContractError("Review scope must contain at least one source file")
    cocotb: set[str] = set()
    hdl: set[str] = set()
    support: set[str] = set()
    unsupported: list[str] = []
    for raw_path in scope:
        path = normalize_scope_path(raw_path)
        suffix = PurePosixPath(path).suffix.lower()
        if not reviewable(path, category):
            unsupported.append(path)
        elif suffix == ".py":
            cocotb.add(path)
        elif suffix in _HDL_SUFFIXES:
            hdl.add(path)
        else:
            support.add(path)
    if unsupported and not allow_support:
        raise ReviewContractError(
            f"Unsupported {category.upper()} source kind in --scope: "
            + ", ".join(sorted(unsupported))
            + f". Interactive review accepts {accepted_scope_kinds(category)} "
            + f"for category={category}."
        )
    return ReviewScopeContract(
        frozenset(cocotb), frozenset(hdl), frozenset(support | set(unsupported))
    )
