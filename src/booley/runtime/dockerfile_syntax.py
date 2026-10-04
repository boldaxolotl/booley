"""Split Dockerfile text into logical instructions.

One parser serves the CI tooling-key script and the image-contract tests so
they cannot disagree about where an instruction starts or ends. It is
stdlib-only because ``.github/scripts`` import it before Booley is installed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_STAGE_ALIAS = re.compile(r"\s+AS\s+(\S+)\s*$", re.IGNORECASE)
_COPY_FROM = re.compile(r"(?:^|\s)--from=(\S+)", re.IGNORECASE)


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
