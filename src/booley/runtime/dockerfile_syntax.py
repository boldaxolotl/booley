"""Split Dockerfile text into logical instructions.

One parser serves the CI tooling-key script and the image-contract tests so
they cannot disagree about where an instruction starts or ends. It is
stdlib-only because ``.github/scripts`` import it before Booley is installed.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

_STAGE_ALIAS = re.compile(r"\s+AS\s+(\S+)\s*$", re.IGNORECASE)
_COPY_FROM = re.compile(r"(?:^|\s)--from=(\S+)", re.IGNORECASE)
_ARGUMENT_REFERENCE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")


@dataclass(frozen=True)
class Instruction:
    """One logical Dockerfile instruction.

    ``line`` and ``last_line`` are the 1-based physical lines it spans.
    """

    keyword: str
    value: str
    line: int
    last_line: int

    @property
    def stage_alias(self) -> str | None:
        """Return the lowercased ``AS`` name of a ``FROM``, else ``None``."""
        if self.keyword != "FROM":
            return None
        match = _STAGE_ALIAS.search(self.value)
        return match.group(1).lower() if match else None

    @property
    def copy_source_stage(self) -> str | None:
        """Return the lowercased ``--from=`` source of a ``COPY``, else ``None``."""
        if self.keyword != "COPY":
            return None
        match = _COPY_FROM.search(self.value)
        return match.group(1).lower() if match else None


def logical_instructions(contents: str) -> tuple[Instruction, ...]:
    """Group physical lines into logical instructions.

    Comment and blank lines inside a backslash continuation do not end the
    instruction and contribute no text, matching Docker's parser. Raises
    ``ValueError`` when the text ends inside a continuation.
    """
    instructions: list[Instruction] = []
    parts: list[str] = []
    start = 0
    for number, raw in enumerate(contents.splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not parts:
            start = number
        continued = stripped.endswith("\\")
        parts.append(stripped.removesuffix("\\").strip())
        if continued:
            continue
        keyword, *rest = " ".join(part for part in parts if part).split(maxsplit=1)
        value = rest[0] if rest else ""
        instructions.append(Instruction(keyword.upper(), value, start, number))
        parts = []
    if parts:
        raise ValueError(f"Dockerfile line {start}: unterminated continuation")
    return tuple(instructions)


def stage_aliases(instructions: tuple[Instruction, ...]) -> frozenset[str]:
    """Return the lowercased names of every ``FROM … AS <name>`` stage."""
    return frozenset(alias for item in instructions if (alias := item.stage_alias) is not None)


def build_context_imports(contents: str) -> tuple[Instruction, ...]:
    """Return ``COPY``/``ADD`` instructions that read the build context.

    ``COPY --from=<stage>`` naming a stage of the same file reads no context.
    ``--from`` naming an image or named context still counts.
    """
    instructions = logical_instructions(contents)
    stages = stage_aliases(instructions)
    return tuple(
        item
        for item in instructions
        if item.keyword in {"ADD", "COPY"} and item.copy_source_stage not in stages
    )


@dataclass(frozen=True)
class BuildArgument:
    """One ``ARG`` declaration.

    ``stage`` is ``None`` for a declaration before the first ``FROM`` (global
    scope) and the 0-based index of the build stage otherwise. ``default`` is
    the declared literal; a bare ``ARG NAME`` has ``None``.
    """

    name: str
    default: str | None
    stage: int | None
    line: int


@dataclass(frozen=True)
class FromReference:
    """One ``FROM`` line with global ``ARG`` defaults substituted.

    ``image`` is the first non-flag field. ``stage`` is the 0-based index of
    the build stage the line opens.
    """

    image: str
    alias: str | None
    stage: int
    line: int


def build_arguments(contents: str) -> tuple[BuildArgument, ...]:
    """Return every ``ARG`` declaration in file order, repeats included.

    Raises ``ValueError`` naming the line for an empty name or an ``ARG`` that
    declares several names (the repository never uses that form).
    """
    arguments: list[BuildArgument] = []
    stage: int | None = None
    for instruction in logical_instructions(contents):
        if instruction.keyword == "FROM":
            stage = 0 if stage is None else stage + 1
        elif instruction.keyword == "ARG":
            fields = shlex.split(instruction.value)
            if len(fields) != 1:
                raise ValueError(
                    f"Dockerfile:{instruction.line}: ARG must declare exactly one name, "
                    f"found {instruction.value!r}"
                )
            name, separator, default = fields[0].partition("=")
            if not name:
                raise ValueError(f"Dockerfile:{instruction.line}: ARG has an empty name")
            arguments.append(
                BuildArgument(name, default if separator else None, stage, instruction.line)
            )
    return tuple(arguments)


def argument_default(contents: str, name: str) -> str:
    """Return the default of the one ``ARG`` declaration of ``name`` that has one.

    Bare redeclarations (``ARG NAME``) do not count. Raises ``ValueError``
    when no declaration carries a default or when more than one does, equal
    or not: a pinned value has a single source.
    """
    defaults = [
        argument.default
        for argument in build_arguments(contents)
        if argument.name == name and argument.default is not None
    ]
    if len(defaults) != 1:
        raise ValueError(f"ARG {name}: expected one declaration with a default, found {defaults}")
    return defaults[0]


def from_references(contents: str) -> tuple[FromReference, ...]:
    """Return every ``FROM`` with global ``ARG`` defaults substituted.

    Raises ``ValueError`` when a ``$`` reference stays unresolved.
    """
    globals_: dict[str, str] = {}
    references: list[FromReference] = []
    stage = -1
    for instruction in logical_instructions(contents):
        if instruction.keyword == "ARG" and stage < 0:
            name, separator, default = instruction.value.partition("=")
            if separator:
                globals_[name] = default
        elif instruction.keyword == "FROM":
            stage += 1
            image = next(
                (field for field in shlex.split(instruction.value) if not field.startswith("--")),
                "",
            )
            resolved = _ARGUMENT_REFERENCE.sub(
                lambda match: globals_.get(match.group(1) or match.group(2), match.group(0)),
                image,
            )
            if not resolved or "$" in resolved:
                raise ValueError(f"Dockerfile:{instruction.line}: unresolved FROM {image}")
            references.append(
                FromReference(resolved, instruction.stage_alias, stage, instruction.line)
            )
    return tuple(references)
