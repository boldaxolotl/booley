"""Resolve the source-scoped part of a Reviewer invocation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_CPP_SUFFIXES = frozenset({".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"})
_HDL_SUFFIXES = frozenset({".v", ".vh", ".sv", ".svh"})

# Booley already treats ``fw/`` as the firmware directory (always an RTL prefix).
FIRMWARE_DIR = "fw"


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
        """Sorted scoped HDL, cocotb, and C/C++ files; Goal support files are omitted."""
        cpp = {
            path
            for path in self.support_files
            if PurePosixPath(path).suffix.lower() in _CPP_SUFFIXES
        }
        return tuple(sorted(self.cocotb_files | self.hdl_files | cpp))

    def is_cocotb_file(self, path: str) -> bool:
        """Return whether *path* is a Python testbench in this scope."""
        return _normalize(path) in self.cocotb_files

    def contains_file(self, path: str) -> bool:
        """Return whether *path* is one of the explicitly scoped sources."""
        normalized = _normalize(path)
        return (
            normalized in self.cocotb_files
            or normalized in self.hdl_files
            or normalized in self.support_files
        )


def _normalize(path: str) -> str:
    value = path.replace("\\", "/").removeprefix("./")
    return str(PurePosixPath(value))


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
        path = _normalize(raw_path)
        suffix = PurePosixPath(path).suffix.lower()
        if suffix == ".py" and category == "tb":
            cocotb.add(path)
        elif suffix in _HDL_SUFFIXES:
            hdl.add(path)
        elif category == "tb" and suffix in _CPP_SUFFIXES:
            support.add(path)
        else:
            unsupported.append(path)
    if unsupported and not allow_support:
        raise ReviewContractError(
            f"Unsupported {category.upper()} source kind in --scope: "
            + ", ".join(sorted(unsupported))
            + ". Interactive review accepts .v, .vh, .sv, .svh"
            + (
                ", .py and C/C++ (.c, .cc, .cpp, .cxx, .h, .hh, .hpp, .hxx) for category=tb"
                if category == "tb"
                else ""
            )
            + "; see CLI-REFERENCE.md (host doc, not shipped). Goal Mode also accepts explicitly scoped support files."
        )
    return ReviewScopeContract(
        frozenset(cocotb), frozenset(hdl), frozenset(support | set(unsupported))
    )


def is_firmware_path(work_dir: Path | str, path: Path | str) -> bool:
    """Whether *path* is firmware: a non-HDL file under the project's ``fw/`` directory.

    HDL under ``fw/`` stays RTL so Specialist Source Isolation still hides it from
    the testbench Reviewer. Absolute, ``./``, ``..`` and symlinked spellings
    resolve to one project-relative path; outside-project paths are never firmware.
    """
    from booley.fusesoc.fusesoc_registry import canonical_project_path

    relative = PurePosixPath(canonical_project_path(work_dir, path))
    # Any HDL suffix keeps a file RTL, as Source Isolation's source detection does, so
    # backups such as ``rom.sv.orig`` or ``rom.sv~`` never become firmware.
    hdl_named = any(suffix.lower().rstrip("~") in _HDL_SUFFIXES for suffix in relative.suffixes)
    return not relative.is_absolute() and relative.parts[:1] == (FIRMWARE_DIR,) and not hdl_named
