"""Discover committed programs from schema-defined executable references."""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path, PurePosixPath

_PROGRAM_SUFFIXES = frozenset({".bash", ".js", ".pl", ".py", ".rb", ".sh", ".tcl"})
_PROGRAM_BASENAMES = frozenset({"makefile", "gnumakefile"})
_TARGET_COMMAND_KEYS = frozenset({"pre_run"})
_COMMAND_SEPARATORS = frozenset({";", "&&", "||", "|"})
_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)
_INTERPRETER_SAFE_FLAGS = {
    "bash": frozenset({"-e", "-f", "-n", "-u", "-v", "-x"}),
    "node": frozenset({"--no-warnings"}),
    "perl": frozenset({"-w"}),
    "python": frozenset({"-B", "-E", "-I", "-O", "-OO", "-P", "-q", "-s", "-S", "-u", "-v", "-x"}),
    "python3": frozenset(
        {"-B", "-E", "-I", "-O", "-OO", "-P", "-q", "-s", "-S", "-u", "-v", "-x"}
    ),
    "ruby": frozenset({"-w"}),
    "sh": frozenset({"-e", "-f", "-n", "-u", "-v", "-x"}),
    "tclsh": frozenset(),
}


def core_program_paths(
    doc: Mapping[str, object],
    *,
    core_file: Path,
    project_root: Path,
    strict: bool = False,
) -> tuple[Path, ...]:
    """Return programs from FuseSoC ``scripts.cmd`` and generator commands.

    Script commands are already argv; generator commands are single paths. Only
    explicitly executable Target options are parsed; other options and symbolic
    hook/generator references are deliberately ignored.
    """
    candidates = _core_program_candidates(doc)
    return _resolve_program_paths(
        candidates,
        search_root=core_file.parent,
        project_root=project_root,
        strict=strict,
    )


def _core_program_candidates(doc: Mapping[str, object]) -> tuple[str, ...]:
    return (
        *_script_program_candidates(doc),
        *_generator_program_candidates(doc),
        *_target_program_candidates(doc),
    )


def _script_program_candidates(doc: Mapping[str, object]) -> Iterator[str]:
    scripts = doc.get("scripts")
    if not isinstance(scripts, Mapping):
        return
    for spec in scripts.values():
        if not isinstance(spec, Mapping):
            continue
        command = spec.get("cmd")
        if isinstance(command, list) and all(isinstance(item, str) for item in command):
            yield from _argv_program_candidates(command)


def _generator_program_candidates(doc: Mapping[str, object]) -> Iterator[str]:
    generators = doc.get("generators")
    if not isinstance(generators, Mapping):
        return
    for spec in generators.values():
        if not isinstance(spec, Mapping):
            continue
        command = spec.get("command")
        if isinstance(command, str) and command:
            yield command


def _target_program_candidates(doc: Mapping[str, object]) -> Iterator[str]:
    targets = doc.get("targets")
    if not isinstance(targets, Mapping):
        return
    for target in targets.values():
        if not isinstance(target, Mapping):
            continue
        options = target.get("flow_options")
        if not isinstance(options, Mapping):
            continue
        for key in _TARGET_COMMAND_KEYS:
            command = options.get(key)
            if isinstance(command, str):
                yield from _shell_program_candidates(command)


def project_config_program_paths(
    config: Mapping[str, object],
    *,
    project_root: Path,
    strict: bool = False,
) -> tuple[Path, ...]:
    """Return programs invoked by configured Simulation Pre-Run Commands."""
    candidates: list[str] = []
    flows = config.get("flows")
    if isinstance(flows, Mapping):
        for flow in flows.values():
            if not isinstance(flow, Mapping):
                continue
            commands = flow.get("pre_run_commands")
            if not isinstance(commands, list):
                continue
            for command in commands:
                if isinstance(command, str):
                    candidates.extend(_shell_program_candidates(command))
    return _resolve_program_paths(
        candidates,
        search_root=project_root,
        project_root=project_root,
        strict=strict,
    )


def _shell_program_candidates(command: str) -> tuple[str, ...]:
    """Extract programs from a bounded shell grammar, never arbitrary operands."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return ()
    candidates: list[str] = []
    segment: list[str] = []
    for token in tokens:
        if token in _COMMAND_SEPARATORS:
            candidates.extend(_shell_segment_program_candidates(segment))
            segment = []
        elif token and all(char in ";&|" for char in token):
            return ()
        else:
            segment.append(token)
    candidates.extend(_shell_segment_program_candidates(segment))
    return tuple(candidates)


def _shell_segment_program_candidates(command: list[str]) -> tuple[str, ...]:
    index = 0
    while index < len(command) and _ASSIGNMENT_RE.fullmatch(command[index]):
        index += 1
    return _argv_program_candidates(command[index:])


def _argv_program_candidates(command: list[str]) -> tuple[str, ...]:
    if not command:
        return ()
    executable = PurePosixPath(command[0]).name.casefold()
    if executable in {"gmake", "make"}:
        return _makefile_candidates(command[1:])
    if executable in _INTERPRETER_SAFE_FLAGS:
        index = 1
        while index < len(command) and command[index] in _INTERPRETER_SAFE_FLAGS[executable]:
            index += 1
        if index == len(command) or command[index].startswith("-"):
            return ()
        script = command[index]
        return (script,) if _looks_like_program_path(PurePosixPath(script), script) else ()
    candidate = command[0]
    return (candidate,) if _looks_like_program_path(PurePosixPath(candidate), candidate) else ()


def _makefile_candidates(arguments: list[str]) -> tuple[str, ...]:
    candidates: list[str] = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in {"-f", "--file", "--makefile"} and index + 1 < len(arguments):
            candidates.append(arguments[index + 1])
            index += 2
            continue
        for prefix in ("--file=", "--makefile="):
            if argument.startswith(prefix) and argument != prefix:
                candidates.append(argument.removeprefix(prefix))
        if argument.startswith("-f") and argument != "-f":
            candidates.append(argument.removeprefix("-f"))
        index += 1
    return tuple(candidates)


def _resolve_program_paths(
    candidates: Iterable[str],
    *,
    search_root: Path,
    project_root: Path,
    strict: bool,
) -> tuple[Path, ...]:
    root = project_root.resolve()
    base = search_root.resolve()
    paths: set[Path] = set()
    for candidate in candidates:
        lexical = base / candidate
        resolved = lexical.resolve()
        if not resolved.is_relative_to(root):
            if strict:
                raise ValueError(
                    f"referenced program cannot be mapped to the Project: {candidate}"
                )
            continue
        if resolved.is_file():
            paths.add(resolved)
            paths.update(_redirecting_entries(root, lexical))
        elif strict:
            raise ValueError(f"referenced program is unavailable: {candidate}")
    return tuple(sorted(paths))


def _redirecting_entries(root: Path, path: Path) -> Iterator[Path]:
    """Yield symlink and nested-repository entries on a referenced path."""
    current = path
    while current != root and current.is_relative_to(root):
        if current.is_symlink() or (current.is_dir() and (current / ".git").exists()):
            yield current
        current = current.parent


def _looks_like_program_path(path: PurePosixPath, token: str) -> bool:
    return (
        path.suffix.casefold() in _PROGRAM_SUFFIXES
        or path.name.casefold() in _PROGRAM_BASENAMES
        or "/" in token
        or "\\" in token
    )
