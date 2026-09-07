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
_INTERPRETER_INLINE_OPTIONS = {
    "bash": ("-c",),
    "node": ("-e", "--eval", "-p", "--print"),
    "perl": ("-e", "-E"),
    "python": ("-c", "-m"),
    "python3": ("-c", "-m"),
    "ruby": ("-e",),
    "sh": ("-c",),
    "tclsh": (),
}
_INTERPRETER_VALUE_OPTIONS = {
    "bash": frozenset({"-O", "-o", "--init-file", "--rcfile"}),
    "node": frozenset({"-r", "--require", "--import", "--loader", "--input-type"}),
    "perl": frozenset({"-I", "-M", "-m"}),
    "python": frozenset({"-W", "-X", "--check-hash-based-pycs"}),
    "python3": frozenset({"-W", "-X", "--check-hash-based-pycs"}),
    "ruby": frozenset({"-E", "-I", "-r", "--encoding"}),
    "sh": frozenset({"-o"}),
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
    candidates = _core_program_candidates(doc, strict=strict)
    return _resolve_program_paths(
        candidates,
        search_root=core_file.parent,
        project_root=project_root,
        strict=strict,
    )


def _core_program_candidates(doc: Mapping[str, object], *, strict: bool) -> tuple[str, ...]:
    return (
        *_script_program_candidates(doc),
        *_generator_program_candidates(doc),
        *_target_program_candidates(doc, strict=strict),
    )


def _script_program_candidates(doc: Mapping[str, object]) -> Iterator[str]:
    scripts = doc.get("scripts")
    if not isinstance(scripts, Mapping):
        return
    for spec in scripts.values():
        if not isinstance(spec, Mapping):
            continue
        command = spec.get("cmd")
        appended = spec.get("cmd_append", [])
        if (
            isinstance(command, list)
            and all(isinstance(item, str) for item in command)
            and isinstance(appended, list)
            and all(isinstance(item, str) for item in appended)
        ):
            yield from _argv_program_candidates([*command, *appended])


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


def _target_program_candidates(doc: Mapping[str, object], *, strict: bool) -> Iterator[str]:
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
                yield from _shell_program_candidates(command, strict=strict)


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
                    candidates.extend(_shell_program_candidates(command, strict=strict))
    return _resolve_program_paths(
        candidates,
        search_root=project_root,
        project_root=project_root,
        strict=strict,
    )


def _shell_program_candidates(command: str, *, strict: bool) -> tuple[str, ...]:
    """Extract programs from a bounded shell grammar, never arbitrary operands."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError as exc:
        if strict:
            raise ValueError(f"invalid executable command syntax: {command!r}") from exc
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
    if executable in _INTERPRETER_INLINE_OPTIONS:
        return _interpreter_script_candidates(executable, command[1:])
    candidate = command[0]
    return (candidate,) if _looks_like_program_path(PurePosixPath(candidate), candidate) else ()


def _interpreter_script_candidates(executable: str, arguments: list[str]) -> tuple[str, ...]:
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            index += 1
            break
        if _matches_interpreter_option(argument, _INTERPRETER_INLINE_OPTIONS[executable]):
            return ()
        value_options = _INTERPRETER_VALUE_OPTIONS[executable]
        if argument in value_options:
            index += 2
            continue
        if _matches_attached_value_option(argument, value_options):
            index += 1
            continue
        if argument.startswith("-"):
            index += 1
            continue
        break
    if index >= len(arguments):
        return ()
    script = arguments[index]
    return (script,) if _looks_like_program_path(PurePosixPath(script), script) else ()


def _matches_interpreter_option(argument: str, options: tuple[str, ...]) -> bool:
    return any(
        argument == option
        or argument.startswith(f"{option}=")
        or (option.startswith("-") and not option.startswith("--") and argument.startswith(option))
        for option in options
    )


def _matches_attached_value_option(argument: str, options: frozenset[str]) -> bool:
    return any(
        argument.startswith(f"{option}=")
        or (option.startswith("-") and not option.startswith("--") and argument.startswith(option))
        for option in options
        if argument != option
    )


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
