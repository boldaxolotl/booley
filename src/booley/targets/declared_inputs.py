"""Discover committed programs from schema-defined executable references."""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path, PurePosixPath
from typing import cast

from booley.core.boundary import as_dict

_PROGRAM_SUFFIXES = frozenset({".bash", ".js", ".pl", ".py", ".rb", ".sh", ".tcl"})
_PROGRAM_BASENAMES = frozenset({"makefile", "gnumakefile"})
_TARGET_COMMAND_KEYS = frozenset({"pre_run"})
_COMMAND_SEPARATORS = frozenset({";", "&&", "||", "|"})
_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)
_INTERPRETER_INLINE_OPTIONS: dict[str, tuple[str, ...]] = {
    "bash": ("-c",),
    "node": ("-e", "--eval", "-p", "--print"),
    "perl": ("-e", "-E"),
    "python": ("-c", "-m"),
    "python3": ("-c", "-m"),
    "ruby": ("-e",),
    "sh": ("-c",),
    "tclsh": (),
}
_INTERPRETER_VALUE_OPTIONS: dict[str, frozenset[str]] = {
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
    scripts = as_dict(doc.get("scripts"))
    if scripts is None:
        return
    for raw_spec in scripts.values():
        spec = as_dict(raw_spec)
        if spec is None:
            continue
        command = _argv(spec.get("cmd"))
        appended = _argv(spec.get("cmd_append", []))
        if command is not None and appended is not None:
            yield from _argv_program_candidates([*command, *appended])


def _generator_program_candidates(doc: Mapping[str, object]) -> Iterator[str]:
    generators = as_dict(doc.get("generators"))
    if generators is None:
        return
    for raw_spec in generators.values():
        spec = as_dict(raw_spec)
        if spec is None:
            continue
        command = spec.get("command")
        if isinstance(command, str) and command:
            yield command


def _target_program_candidates(doc: Mapping[str, object], *, strict: bool) -> Iterator[str]:
    targets = as_dict(doc.get("targets"))
    if targets is None:
        return
    for raw_target in targets.values():
        target = as_dict(raw_target)
        if target is None:
            continue
        options = as_dict(target.get("flow_options"))
        if options is None:
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
    """Return programs invoked by configured Pre-Sim Commands."""
    candidates: list[str] = []
    flows = as_dict(config.get("flows"))
    if flows is not None:
        for raw_flow in flows.values():
            flow = as_dict(raw_flow)
            if flow is None:
                continue
            commands = _argv(flow.get("pre_run_commands"))
            if commands is None:
                continue
            for command in commands:
                candidates.extend(_shell_program_candidates(command, strict=strict))
    return _resolve_program_paths(
        candidates,
        search_root=project_root,
        project_root=project_root,
        strict=strict,
    )


def _argv(value: object) -> list[str] | None:
    if not isinstance(value, list):
        return None
    items = cast(list[object], value)
    if not all(isinstance(item, str) for item in items):
        return None
    return cast(list[str], items)


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
