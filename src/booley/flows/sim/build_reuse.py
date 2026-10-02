"""Prove the complete input closure of a reusable Verilator Simulation build.

A retained Verilator image may replace a fresh build only when everything that
shaped it is known and unchanged. The proof has two halves:

* **Before the build** (phase-stable, part of the reuse key): every FuseSoC core
  (:func:`capture_core_closure`), the stock Edalize recipe and its include
  search directories (:func:`stock_verilator_recipe`), the Verilator
  installation, the C++ toolchain, the system libraries the recipe links, and
  a fingerprint of the system include trees (:func:`verilator_identity`), and
  the build-relevant environment (:func:`build_environment_digest`).
* **After the build** (the read closure): every file Verilator and the C++
  compiler report having read, from ``<prefix>__ver.d`` and one ``-MMD``
  dependency file per object (:func:`collect_read_closure`). The closure is
  re-hashed before each reuse (:func:`verify_read_closure`). Generated
  makefiles must not read environment the key did not hash
  (:func:`unkeyed_build_environment`).

Every function here declines (returns ``None`` or raises
:class:`ReadClosureError`) rather than guessing: the cost of a false decline is
one rebuild, the cost of a false accept is a stale simulation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from booley.core.boundary import as_dict, as_str

from .compiler_cache import RESERVED as COMPILER_CACHE_MANAGED

# Upper bound on files a read closure may name; a larger closure is not reused.
MAX_CLOSURE_FILES = 100_000
# A file outside the generation whose inode changed within this window before
# input capture (or later) may have changed while the compiler read it. The
# window absorbs coarse filesystem timestamps and network-filesystem clock
# skew. Such a build is only downgraded: a previously retained image stays
# selectable, and the next build, outside the window, is reusable again.
RACE_MARGIN_NS = 2_000_000_000
_SUBPROCESS_TIMEOUT_S = 10
_ELF_MAGIC = b"\x7fELF"
# ELF ``e_type`` values of files the dynamic loader maps: ET_EXEC and ET_DYN.
_LOADABLE_ELF_TYPES = frozenset({2, 3})

# Toolchain variables named by Verilator's ``verilated.mk``.
_VERILATED_MK_TOOLS = ("CXX", "LINK", "AR", "RANLIB", "PERL", "PYTHON3")
# The compiler drivers must be named; the others are hashed when assigned.
_REQUIRED_TOOLS = frozenset({"CXX", "LINK"})
# Compiler sub-programs that shape objects and the linked image.
_COMPILER_PROGRAMS = ("cc1plus", "collect2", "as", "ld")
# Startup objects and runtime libraries the driver links into the image.
_LINKED_FILES = (
    "crt1.o",
    "crti.o",
    "crtn.o",
    "crtbegin.o",
    "crtend.o",
    "crtbeginS.o",
    "crtendS.o",
    "libgcc.a",
    "libgcc_s.so",
    "libstdc++.a",
    "libstdc++.so",
)
# Environment names read by compilers, linkers, or the Verilator wrapper even
# when no Makefile references them.
_TOOLCHAIN_ENVIRONMENT = frozenset(
    {
        "PATH",
        "CC",
        "CXX",
        "LINK",
        "AR",
        "CFLAGS",
        "CXXFLAGS",
        "CPPFLAGS",
        "LDFLAGS",
        "LDLIBS",
        "LIBS",
        "LOADLIBES",
        "VPATH",
        "MAKEFILES",
        "GNUMAKEFLAGS",
        "CPATH",
        "C_INCLUDE_PATH",
        "CPLUS_INCLUDE_PATH",
        "LIBRARY_PATH",
        "LD_LIBRARY_PATH",
        "COMPILER_PATH",
        "GCC_EXEC_PREFIX",
        "SOURCE_DATE_EPOCH",
    }
)
# ``VK_``/``VM_`` and ``SC_`` are read by the makefiles Verilator generates.
_ENVIRONMENT_PREFIXES = ("VERILATOR", "SYSTEMC", "SC_", "VM_", "VK_", "USER_", "OPT")
_MAKE_REFERENCE = re.compile(r"\$[({]([A-Za-z_][A-Za-z0-9_]*)")
_MAKE_ASSIGNMENT = re.compile(
    r"^[ \t]*(?:(?:override|export|private)\s+)*([A-Za-z_][A-Za-z0-9_]*)\s*(?::{1,3}|[?+!])?=",
    re.MULTILINE,
)
_TOOL_ASSIGNMENT = re.compile(r"^[ \t]*([A-Z0-9_]+)[ \t]*[:?]?=[ \t]*(.*?)[ \t]*$", re.MULTILINE)
# The two recipes the Edalize flow API writes for Verilator, and its stage rules.
_VERILATE_RECIPE = re.compile(r"\$\(EDALIZE_LAUNCHER\) verilator -f (\S+)")
_COMPILE_RECIPE = re.compile(r"\$\(EDALIZE_LAUNCHER\) make -f (\S+\.mk)((?: \S+)*)")
_STAGE_TARGETS = frozenset({"all", "pre_build", "post_build", "pre_run", "run", "post_run"})
_RULE = re.compile(r"([A-Za-z0-9_.+-]+):(?!=)")
_MAKE_OPTION = re.compile(r"-j[0-9]*|[A-Za-z_][A-Za-z0-9_]*=\S*")
# Make-option assignments that would swap a toolchain program unseen.
_TOOLCHAIN_OVERRIDE = re.compile(
    r"(?:^|\s)(?:CC|CXX|LINK|AR|RANLIB|PERL|PYTHON3|OBJCACHE|VERILATOR\w*)\s*[:?+]?="
)


class ReadClosureError(RuntimeError):
    """A build's read closure cannot be proven complete and safe."""


@dataclass(frozen=True)
class VerilatorIdentity:
    """Pre-build identity of the Verilator installation and C++ toolchain."""

    digest: str
    verilator_root: Path
    system_include_dirs: tuple[Path, ...]
    # Resolved files whose content the digest covers (e.g. a ``verilator_bin``
    # the install tree links to from outside it).
    tool_files: frozenset[Path] = frozenset()

    def allowed_roots(self, project_root: Path) -> tuple[Path, ...]:
        """Directories whose files a read closure may name by absolute path."""
        return (project_root.resolve(), self.verilator_root, *self.system_include_dirs)

    def allows(self, path: Path, project_root: Path) -> bool:
        """Report whether a resolved absolute closure path is within policy."""
        return path in self.tool_files or any(
            path.is_relative_to(root) for root in self.allowed_roots(project_root)
        )

    def makefiles(self) -> tuple[Path, ...]:
        """The installation's makefiles (``verilated.mk`` and what it includes)."""
        return tuple(sorted((self.verilator_root / "include").glob("*.mk")))


@dataclass(frozen=True)
class VerilatorRecipe:
    """What a stock flow-API recipe links from outside its generation."""

    # One entry per ``-l`` option the linker resolves on its system search
    # path: the file names it tries (``libfoo.so``, ``libfoo.a``).
    system_libraries: tuple[tuple[str, ...], ...] = ()


@dataclass
class _RecipeScan:
    """Paths and libraries one ``.vc`` recipe names."""

    # Search directories and file operands; each must stay in the generation.
    paths: list[str] = field(default_factory=list)
    library_dirs: list[str] = field(default_factory=list)
    libraries: list[tuple[str, ...]] = field(default_factory=list)

    def extend(self, other: _RecipeScan) -> None:
        self.paths += other.paths
        self.library_dirs += other.library_dirs
        self.libraries += other.libraries


def hash_file(path: Path) -> str:
    """Return the SHA-256 hex digest of one regular file."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _elf_header(path: Path) -> bytes:
    """Read the identification and ``e_type`` fields of an ELF header."""
    with path.open("rb") as stream:
        return stream.read(18)


def is_elf(path: Path) -> bool:
    """Report whether *path* starts with the ELF magic number."""
    return _elf_header(path)[:4] == _ELF_MAGIC


def elf_dependencies(path: Path) -> tuple[Path, ...] | None:
    """List dynamic libraries for a local ELF executable, or decline reuse."""
    if not is_elf(path):
        return None
    result = subprocess.run(
        ["ldd", str(path)],
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_S,
        check=False,
    )
    if result.returncode != 0 or "not found" in result.stdout:
        return None
    paths = []
    for line in result.stdout.splitlines():
        match = re.search(r"(/\S+)", line)
        if match is not None:
            paths.append(Path(match.group(1)))
    return tuple(paths)


def has_generated_core_behavior(core_data: object) -> bool:
    """Report core metadata that can produce inputs outside the staged closure."""
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


# --------------------------------------------------------------------------
# Pre-build identity
# --------------------------------------------------------------------------


def capture_core_closure(edam_path: Path, project_root: Path) -> dict[str, str] | None:
    """Hash every resolved FuseSoC core, or decline when one is not provable.

    Call while Booley's trace and coverage overlay cores still exist: they are
    removed right after preparation, before the reuse key is computed. Every
    Booley FuseSoC library root (Project, stealth cores, projections, isolated
    registry) lives under the Project root, so a core outside it declines.
    """
    try:
        edam = yaml.safe_load(edam_path.read_text(encoding="utf-8"))
        cores = edam.get("cores") if isinstance(edam, dict) else None
        if not isinstance(cores, dict) or not cores:
            return None
        root = project_root.resolve()
        closure: dict[str, str] = {}
        for name, core in sorted(cores.items(), key=lambda item: str(item[0])):
            if not isinstance(core, dict) or not isinstance(core.get("core_file"), str):
                return None
            core_file = (edam_path.parent / core["core_file"]).resolve()
            if not core_file.is_relative_to(root) or not core_file.is_file():
                return None
            if has_generated_core_behavior(yaml.safe_load(core_file.read_text(encoding="utf-8"))):
                return None
            relative = core_file.relative_to(root).as_posix()
            closure[str(name)] = f"{relative}:{hash_file(core_file)}"
        return closure
    except (OSError, UnicodeError, yaml.YAMLError):
        return None


def stock_verilator_recipe(build_root: Path) -> VerilatorRecipe | None:
    """Accept only the stock Edalize flow recipe with contained search paths.

    Include lookups that *miss* never appear in a dependency file. Requiring
    every Verilog and C++ search directory and every library or object operand
    to live inside the generation keeps those lookups covered by the
    staged-input snapshot: a new file in a search directory is a new staged
    input and changes the key. ``-l`` libraries not found in a contained
    ``-L`` directory are returned for :func:`verilator_identity` to hash.
    Returns ``None`` for any other recipe.
    """
    try:
        recipe = _flow_recipe((build_root / "Makefile").read_text(encoding="utf-8"))
        if recipe is None:
            return None
        vc_name, make_options = recipe
        if any(
            _MAKE_OPTION.fullmatch(option) is None or _TOOLCHAIN_OVERRIDE.search(option)
            for option in make_options
        ):
            return None
        vc_path = Path(vc_name)
        if vc_path.is_absolute() or ".." in vc_path.parts:
            return None
        tokens = shlex.split((build_root / vc_path).read_text(encoding="utf-8"), comments=True)
    except (OSError, UnicodeError, ValueError):
        return None
    scan = _scan_vc_tokens(tokens)
    root = build_root.resolve()
    if scan is None or any(
        not (build_root / item).resolve().is_relative_to(root) for item in scan.paths
    ):
        return None
    return VerilatorRecipe(_system_libraries(scan, build_root))


def _system_libraries(scan: _RecipeScan, build_root: Path) -> tuple[tuple[str, ...], ...]:
    """Drop libraries a contained ``-L`` directory provides; the linker searches those first."""
    return tuple(
        names
        for names in scan.libraries
        if not any(
            (build_root / directory / name).is_file()
            for directory in scan.library_dirs
            for name in names
        )
    )


def _flow_recipe(text: str) -> tuple[str, list[str]] | None:
    """Return the ``.vc`` name and make options of a stock flow-API Makefile.

    The Edalize flow API writes phony stage rules (``pre_build``, ``run``...)
    plus exactly two recipes: Verilate the ``.vc`` into ``V<top>.mk``, then
    compile it. A recipe under a stage rule is a hook; any variable
    assignment, include, or extra recipe is a non-stock recipe.
    """
    vc_name: str | None = None
    make_options: list[str] | None = None
    target: str | None = None
    for line in text.splitlines():
        if line.startswith("\t"):
            command = line[1:].strip()
            verilate = _VERILATE_RECIPE.fullmatch(command)
            compile_ = _COMPILE_RECIPE.fullmatch(command)
            if target is None or target in _STAGE_TARGETS:
                return None
            if verilate and target.endswith(".mk") and vc_name is None:
                vc_name = verilate.group(1)
            elif compile_ and compile_.group(1) == f"{target}.mk" and make_options is None:
                make_options = compile_.group(2).split()
            else:
                return None
        elif line.strip() and not line.startswith("#"):
            rule = _RULE.match(line)
            if rule is None:
                return None
            target = rule.group(1)
    if vc_name is None or make_options is None:
        return None
    return vc_name, make_options


# Linked operands a ``.vc`` file may name directly.
_LINKED_OPERAND = re.compile(r".+\.(?:a|o|so(?:\.[0-9]+)*)")


def _scan_vc_tokens(tokens: list[str]) -> _RecipeScan | None:
    """Collect every Verilog/C++ search path and linked library, or decline."""
    scan = _RecipeScan()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in {"-f", "-F", "-v"}:
            return None  # nested option files and library files escape this scan
        if token.startswith("+incdir+"):
            scan.paths += [item for item in token[len("+incdir+") :].split("+") if item]
        elif token in {"-y", "-I"} and index + 1 < len(tokens):
            index += 1
            scan.paths.append(tokens[index])
        elif token.startswith("-I") and len(token) > 2:
            scan.paths.append(token[2:])
        elif token in {"-CFLAGS", "-LDFLAGS"} and index + 1 < len(tokens):
            index += 1
            nested = _scan_compiler_flags(shlex.split(tokens[index]))
            if nested is None:
                return None
            scan.extend(nested)
        elif not token.startswith(("-", "+")) and _LINKED_OPERAND.fullmatch(token):
            scan.paths.append(token)  # a library or object Verilator links
        index += 1
    return scan


def _scan_compiler_flags(flags: list[str]) -> _RecipeScan | None:
    """Collect include/library directories and libraries from compiler flags."""
    scan = _RecipeScan()
    index = 0
    for_next = {"-I", "-isystem", "-iquote", "-idirafter", "-L", "-include", "-l"}
    while index < len(flags):
        flag = flags[index]
        value = None
        if flag in for_next:
            if index + 1 >= len(flags):
                return None
            index += 1
            value = flags[index]
        elif flag.startswith(("-I", "-L", "-l")) and len(flag) > 2:
            flag, value = flag[:2], flag[2:]
        elif flag.startswith(("-isystem", "-iquote", "-idirafter")):
            return None  # glued spellings are rare; decline instead of parsing
        elif not flag.startswith("-"):
            scan.paths.append(flag)  # a library or object operand
        if value is not None:
            _record_flag_value(scan, flag, value)
        index += 1
    return scan


def _record_flag_value(scan: _RecipeScan, flag: str, value: str) -> None:
    if flag == "-l":
        # ``-l:name`` names one file; ``-lname`` tries the shared library first.
        names = (value[1:],) if value.startswith(":") else (f"lib{value}.so", f"lib{value}.a")
        scan.libraries.append(names)
        return
    scan.paths.append(value)
    if flag == "-L":
        scan.library_dirs.append(value)


def verilator_identity(
    environment: Mapping[str, str], system_libraries: tuple[tuple[str, ...], ...] = ()
) -> VerilatorIdentity | None:
    """Hash the Verilator installation and the C++ toolchain it drives.

    *system_libraries* are the recipe's ``-l`` libraries the linker finds on
    its own search path (:attr:`VerilatorRecipe.system_libraries`).
    """
    try:
        located = _verilator_root(environment)
        if located is None:
            return None
        root, wrapper = located
        verilated_mk = root / "include" / "verilated.mk"
        tools = _verilated_mk_tools(verilated_mk.read_text(encoding="utf-8"))
        # Packaged installs keep small shims in the root's bin/ and the real
        # ``verilator_bin*`` executables beside the wrapper; hash both.
        siblings = sorted(path for path in wrapper.parent.glob("verilator*") if path.is_file())
        paths = [*siblings, *_tree_files(root / "bin"), *_tree_files(root / "include")]
        for name in ("make", "sh"):
            resolved = _which(name, environment)
            if resolved is None:
                return None
            paths.append(resolved)
        toolchain = _toolchain_closure(tools, environment, system_libraries)
        if toolchain is None:
            return None
        paths += toolchain.paths
        file_digest = _hash_paths_with_dependencies(paths)
        if file_digest is None:
            return None
        system_dirs = tuple(sorted(set(toolchain.include_dirs)))
        digest = hashlib.sha256()
        digest.update(file_digest.encode("ascii"))
        digest.update(json.dumps(toolchain.texts).encode("utf-8"))
        digest.update(_stat_fingerprint(system_dirs).encode("ascii"))
        tool_files = frozenset(path.resolve() for path in paths)
        return VerilatorIdentity(digest.hexdigest(), root, system_dirs, tool_files)
    except (OSError, UnicodeError, ValueError, subprocess.SubprocessError):
        return None


def _which(name: str, environment: Mapping[str, str]) -> Path | None:
    found = shutil.which(name, path=environment.get("PATH"))
    return Path(found).resolve() if found is not None else None


def _verilator_root(environment: Mapping[str, str]) -> tuple[Path, Path] | None:
    """Ask the Verilator wrapper where its installation lives."""
    configured = environment.get("VERILATOR_ROOT")
    wrapper = (
        Path(configured) / "bin" / "verilator" if configured else _which("verilator", environment)
    )
    if wrapper is None or not wrapper.is_file():
        return None
    result = _run([str(wrapper), "--getenv", "VERILATOR_ROOT"], environment)
    if result is None:
        return None
    root = Path(result.strip()).resolve()
    if not (root / "include" / "verilated.mk").is_file() or not (root / "bin").is_dir():
        return None
    return root, wrapper.resolve()


def _run(argv: list[str], environment: Mapping[str, str], *, stderr: bool = False) -> str | None:
    result = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_S,
        check=False,
        env=dict(environment),
    )
    if result.returncode != 0:
        return None
    return result.stderr if stderr else result.stdout


def _verilated_mk_tools(text: str) -> dict[str, list[str]]:
    """Return the toolchain commands ``verilated.mk`` assigns."""
    tools: dict[str, list[str]] = {}
    for match in _TOOL_ASSIGNMENT.finditer(text):
        name, value = match.groups()
        if name in _VERILATED_MK_TOOLS and name not in tools:
            command = [word for word in shlex.split(value) if word != "ccache"]
            if not command or "$" in value:
                raise ValueError(f"unsupported verilated.mk {name} assignment: {value!r}")
            tools[name] = command
    if not set(tools) >= _REQUIRED_TOOLS:
        raise ValueError("verilated.mk does not name the C++ compiler and linker")
    return tools


@dataclass(frozen=True)
class _ToolClosure:
    """Files, probe output, and system include directories of one tool."""

    paths: list[Path]
    texts: list[str]
    include_dirs: list[Path] = field(default_factory=list)


def _toolchain_closure(
    tools: Mapping[str, list[str]],
    environment: Mapping[str, str],
    system_libraries: tuple[tuple[str, ...], ...],
) -> _ToolClosure | None:
    """Combine every ``verilated.mk`` tool with the system libraries it links."""
    combined = _ToolClosure([], [])
    for name, command in sorted(tools.items()):
        closure = _tool_closure(name, command, environment)
        if closure is None:
            return None
        combined.paths.extend(closure.paths)
        combined.texts.extend(closure.texts)
        combined.include_dirs.extend(closure.include_dirs)
    libraries = _library_files(tools["LINK"], environment, system_libraries)
    if libraries is None:
        return None
    combined.paths.extend(libraries)
    return combined


def _tool_closure(
    name: str, command: list[str], environment: Mapping[str, str]
) -> _ToolClosure | None:
    """Return the files and probe texts that identify one toolchain command."""
    program = _which(command[0], environment)
    if program is None:
        return None
    if name not in _REQUIRED_TOOLS:
        return _ToolClosure([program], [f"{name}={shlex.join(command)}"])
    paths = [program]
    texts = [f"{name}={shlex.join(command)}"]
    for query in (
        ["--version"],
        ["-dumpmachine"],
        ["-print-search-dirs"],
        *(["-print-prog-name=" + item] for item in _COMPILER_PROGRAMS),
        *(["-print-file-name=" + item] for item in _LINKED_FILES),
    ):
        output = _run([*command, *query], environment)
        if output is None:
            return None
        texts.append(output)
        candidate = output.strip()
        if query[0].startswith("-print-prog-name="):
            resolved = Path(candidate) if Path(candidate).is_absolute() else None
            resolved = resolved or _which(candidate, environment)
            if resolved is None:
                return None
            paths.append(resolved.resolve())
        elif query[0].startswith("-print-file-name=") and Path(candidate).is_absolute():
            paths.append(Path(candidate).resolve())
    dirs = _system_include_dirs(command, environment)
    if dirs is None:
        return None
    return _ToolClosure(paths, texts, dirs)


def _library_files(
    link: list[str],
    environment: Mapping[str, str],
    system_libraries: tuple[tuple[str, ...], ...],
) -> list[Path] | None:
    """Resolve each system ``-l`` library through the link driver, or decline.

    Every candidate the driver finds is hashed (a shared and a static flavor
    both, when both exist), so whichever one the linker picks is covered.
    """
    found: list[Path] = []
    for names in system_libraries:
        resolved = []
        for name in names:
            output = _run([*link, f"-print-file-name={name}"], environment)
            candidate = Path(output.strip()) if output is not None else None
            if candidate is not None and candidate.is_absolute() and candidate.is_file():
                resolved.append(candidate.resolve())
        if not resolved:
            return None
        found += resolved
    return found


def _system_include_dirs(command: list[str], environment: Mapping[str, str]) -> list[Path] | None:
    """Parse the compiler's ``#include <...>`` search list."""
    output = _run([*command, "-E", "-x", "c++", "-v", os.devnull], environment, stderr=True)
    if output is None:
        return None
    lines = output.splitlines()
    try:
        start = lines.index("#include <...> search starts here:") + 1
        end = lines.index("End of search list.", start)
    except ValueError:
        return None
    dirs = [Path(line.strip().removesuffix(" (framework directory)")) for line in lines[start:end]]
    return [path.resolve() for path in dirs if path.is_dir()]


def _tree_files(root: Path) -> list[Path]:
    """List every regular file below *root*; symlinks are followed by hashing."""
    if not root.is_dir():
        raise OSError(f"missing toolchain directory: {root}")
    return sorted(path for path in root.rglob("*") if path.is_file())


def _hash_paths_with_dependencies(paths: Iterable[Path]) -> str | None:
    """Hash files plus the dynamic libraries of every ELF among them."""
    closure = list(paths)
    for path in tuple(closure):
        if not path.is_file():
            return None
        if _is_loadable_elf(path):
            dependencies = elf_dependencies(path)
            if dependencies is None:
                return None
            closure.extend(dependencies)
    digest = hashlib.sha256()
    for path in sorted(set(closure)):
        if not path.is_file():
            return None
        digest.update(str(path).encode("utf-8"))
        digest.update(str(path.resolve()).encode("utf-8"))
        digest.update(hash_file(path).encode("ascii"))
    return digest.hexdigest()


def _is_loadable_elf(path: Path) -> bool:
    """Report an ELF executable or shared object; relocatable objects have no deps."""
    header = _elf_header(path)
    if len(header) < 18 or header[:4] != _ELF_MAGIC:
        return False
    byteorder = "little" if header[5] == 1 else "big"
    return int.from_bytes(header[16:18], byteorder) in _LOADABLE_ELF_TYPES


def _stat_fingerprint(directories: Iterable[Path]) -> str:
    """Fingerprint system include trees by name, size, inode, and times.

    Content-hashing tens of thousands of system headers on every reuse is too
    slow; a package upgrade or edit changes size, inode, or ctime, and a new
    header (which could win an include lookup) changes the listing.
    """
    digest = hashlib.sha256()
    for directory in directories:
        digest.update(str(directory).encode("utf-8"))
        for current, dirnames, filenames in os.walk(directory, followlinks=False):
            dirnames.sort()
            for name in sorted((*dirnames, *filenames)):
                path = Path(current) / name
                info = path.lstat()
                digest.update(
                    f"{path}\0{info.st_mode}\0{info.st_size}\0{info.st_ino}\0"
                    f"{info.st_mtime_ns}\0{info.st_ctime_ns}\n".encode()
                )
                if path.is_symlink():
                    digest.update(str(path.readlink()).encode("utf-8"))
    return digest.hexdigest()


def build_environment_digest(
    environment: Mapping[str, str], build_root: Path, identity: VerilatorIdentity
) -> str:
    """Hash only environment entries that can reach the Verilator build.

    Make imports every environment entry as a variable, so the relevant names
    are the ones the pre-build Makefile and the installation's makefiles
    reference or assign, plus toolchain names and Verilator's own prefixes.
    Names Booley's compiler cache manages are excluded: they live in the
    Target build environment, which the key hashes separately.
    """
    names = keyed_environment_names(build_root, identity)
    selected = {
        name: value
        for name, value in environment.items()
        if _is_keyed(name, names) and name not in COMPILER_CACHE_MANAGED
    }
    return hashlib.sha256(json.dumps(sorted(selected.items())).encode("utf-8")).hexdigest()


def keyed_environment_names(build_root: Path, identity: VerilatorIdentity) -> set[str]:
    """Environment names the key hashes, besides the prefixed families."""
    names = set(_TOOLCHAIN_ENVIRONMENT)
    for path in (build_root / "Makefile", *identity.makefiles()):
        names |= _make_variable_names(path.read_text(encoding="utf-8"))
    return names


def unkeyed_build_environment(
    build_root: Path, identity: VerilatorIdentity, environment: Mapping[str, str]
) -> list[str]:
    """Return set environment names generated makefiles read but the key skipped.

    The makefiles Verilator writes (``V<top>.mk`` and friends) do not exist
    when the key is computed. Any variable they reference or assign that is
    set in *environment* yet was not hashed could change the build unseen.
    """
    keyed = keyed_environment_names(build_root, identity)
    generated: set[str] = set()
    for path in sorted(build_root.glob("*.mk")):
        generated |= _make_variable_names(path.read_text(encoding="utf-8"))
    return sorted(
        name
        for name in generated
        if name in environment
        and not _is_keyed(name, keyed)
        and name not in COMPILER_CACHE_MANAGED
    )


def _make_variable_names(text: str) -> set[str]:
    return {*_MAKE_REFERENCE.findall(text), *_MAKE_ASSIGNMENT.findall(text)}


def _is_keyed(name: str, names: set[str]) -> bool:
    return name in names or name.startswith(_ENVIRONMENT_PREFIXES)


# --------------------------------------------------------------------------
# Post-build read closure
# --------------------------------------------------------------------------


def parse_depfile(text: str) -> list[str]:
    """Return the prerequisites of a Make dependency file.

    Handles backslash continuations, ``\\ `` escaped spaces, ``$$`` and the
    ``-MP`` phony header rules (targets without prerequisites). Raises
    :class:`ReadClosureError` for anything that is not a rule.
    """
    joined = re.sub(r"\\\r?\n", " ", text)
    prerequisites: list[str] = []
    for line in joined.splitlines():
        if not line.strip():
            continue
        match = re.search(r"(?<!\\):(?:\s|$)", line)
        if match is None:
            raise ReadClosureError(f"malformed dependency rule: {line[:200]!r}")
        prerequisites += _depfile_words(line[match.end() :])
    return prerequisites


def _depfile_words(text: str) -> list[str]:
    words: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text) and text[index + 1] in " #\\":
            current.append(text[index + 1])
            index += 2
            continue
        if char == "$" and text.startswith("$$", index):
            current.append("$")
            index += 2
            continue
        if char.isspace():
            if current:
                words.append("".join(current))
                current = []
        else:
            current.append(char)
        index += 1
    if current:
        words.append("".join(current))
    return words


def collect_read_closure(
    build_root: Path,
    work_root: Path,
    identity: VerilatorIdentity,
    project_root: Path,
    captured_ns: int,
) -> dict[str, str]:
    """Hash every file the Verilate and C++ steps of a fresh build read.

    Keys are ``work:<path relative to the generation>`` or ``abs:<path>``.
    Raises :class:`ReadClosureError` when the dependency evidence is missing,
    incomplete, or names a file outside the allowed roots, or when a file
    outside the generation changed at or after input capture.
    """
    try:
        depfiles = _expected_depfiles(build_root)
        names: set[Path] = set()
        for depfile in depfiles:
            for word in parse_depfile(depfile.read_text(encoding="utf-8")):
                names.add((build_root / word).resolve())
        if len(names) > MAX_CLOSURE_FILES:
            raise ReadClosureError(f"read closure exceeds {MAX_CLOSURE_FILES} files")
        closure: dict[str, str] = {}
        work = work_root.resolve()
        for path in sorted(names):
            key = _closure_key(path, work, identity, project_root)
            if key.startswith("abs:") and path.stat().st_ctime_ns >= captured_ns - RACE_MARGIN_NS:
                raise ReadClosureError(f"read-closure file changed around the build: {path}")
            closure[key] = hash_file(path)
        return closure
    except (OSError, UnicodeError) as exc:
        raise ReadClosureError(f"cannot read build dependency evidence: {exc}") from exc


def _expected_depfiles(build_root: Path) -> list[Path]:
    """Return every depfile, requiring one Verilate depfile and one per object.

    Extra depfiles (precompiled-header rules, say) are parsed too: anything a
    build step reported reading belongs to the closure.
    """
    verilate = sorted(build_root.glob("*__ver.d"))
    if len(verilate) != 1:
        raise ReadClosureError(f"expected one Verilator dependency file, found {len(verilate)}")
    objects = sorted(build_root.glob("*.o"))
    if not objects:
        raise ReadClosureError("Verilator build produced no object files")
    depfiles = [path.with_suffix(".d") for path in objects]
    missing = [path.name for path in depfiles if not path.is_file()]
    if missing:
        raise ReadClosureError(f"object files lack dependency records: {missing[:5]}")
    return sorted(build_root.glob("*.d"))


def _closure_key(
    path: Path, work_root: Path, identity: VerilatorIdentity, project_root: Path
) -> str:
    """Classify one resolved closure path, rejecting anything uncontained."""
    if not path.is_file():
        raise ReadClosureError(f"read-closure entry is not a regular file: {path}")
    if path.is_relative_to(work_root):
        return "work:" + path.relative_to(work_root).as_posix()
    if identity.allows(path, project_root):
        return "abs:" + str(path)
    raise ReadClosureError(f"build read a file outside the allowed roots: {path}")


def verify_read_closure(
    raw: object, work_root: Path, identity: VerilatorIdentity, project_root: Path
) -> bool:
    """Re-hash a recorded read closure under the same path policy."""
    closure = as_dict(raw)
    if not closure or len(closure) > MAX_CLOSURE_FILES:
        return False
    work = work_root.resolve()
    try:
        for raw_key, digest in closure.items():
            key = as_str(raw_key)
            if key is None or as_str(digest) is None:
                return False
            path = _closure_path(key, work, identity, project_root)
            if path is None or hash_file(path) != digest:
                return False
    except OSError:
        return False
    return True


def _closure_path(
    key: str, work_root: Path, identity: VerilatorIdentity, project_root: Path
) -> Path | None:
    kind, _, name = key.partition(":")
    if kind == "work":
        relative = Path(name)
        if not name or relative.is_absolute() or ".." in relative.parts:
            return None
        path = work_root / relative
    elif kind == "abs":
        path = Path(name)
        if not path.is_absolute() or ".." in path.parts:
            return None
    else:
        return None
    resolved = path.resolve()
    contained = resolved.is_relative_to(work_root) or (
        kind == "abs" and identity.allows(resolved, project_root)
    )
    if resolved != path or not contained or not resolved.is_file():
        return None
    return resolved


__all__ = [
    "MAX_CLOSURE_FILES",
    "RACE_MARGIN_NS",
    "ReadClosureError",
    "VerilatorIdentity",
    "VerilatorRecipe",
    "build_environment_digest",
    "capture_core_closure",
    "collect_read_closure",
    "elf_dependencies",
    "has_generated_core_behavior",
    "hash_file",
    "is_elf",
    "keyed_environment_names",
    "parse_depfile",
    "stock_verilator_recipe",
    "unkeyed_build_environment",
    "verify_read_closure",
    "verilator_identity",
]
