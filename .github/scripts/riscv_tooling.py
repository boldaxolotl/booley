#!/usr/bin/env python3
"""Identify the reusable RISC-V tooling stage of ``Dockerfile.riscv`` (ADR 0070).

The tooling key is a hash of the ``riscv-tooling`` stage's exact text, a key
schema version, and the platform. The stage is digest-pinned, declares every
``ARG`` it uses, and copies nothing from the build context, so its text covers
every input Booley pins. This module enforces those preconditions and fails
closed when the Dockerfile no longer satisfies them: a key that silently missed
an input would let CI reuse a stale toolchain.

It also answers whether a key is already published. Only a digest whose role
and key labels match is trusted: GHCR tags are mutable.

Runs from a plain checkout before Booley is installed; the only non-stdlib
import is the dependency-free registry resolver under ``src/``.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_ROOT / "src"))

from booley.runtime.docker_base_contract import resolve_labeled_image_remote

STAGE = "riscv-tooling"
DEFAULT_DOCKERFILE = Path("src/booley/data/docker/Dockerfile.riscv")
REPOSITORY = "ghcr.io/boldaxolotl/booley-sandbox-base"
# Bump the schema when the key derivation itself changes meaning.
_KEY_SCHEMA = b"riscv-tooling-v1"
_PLATFORM = b"linux/amd64"
_DIGEST_PINNED = re.compile(r"@sha256:[0-9a-f]{64}$")
_STAGE_ALIAS = re.compile(r"\s+AS\s+(\S+)\s*$", re.IGNORECASE)
_CONTEXT_IMPORTS = frozenset({"ADD", "COPY"})
ROLE_LABEL = "io.booley.artifact.role"
KEY_LABEL = "io.booley.riscv-tooling.key"
# `imagetools inspect` reports a missing tag with one of these messages.
_ABSENT_MARKERS = ("not found", "manifest unknown")


class ToolingStageError(ValueError):
    """Raised when the tooling stage cannot be keyed safely."""


class ToolingRegistryError(RuntimeError):
    """Raised when the registry cannot say whether a tooling image exists."""


@dataclass(frozen=True)
class _Instruction:
    """One logical Dockerfile instruction and the physical lines it spans."""

    keyword: str
    text: str
    first_line: int
    last_line: int


def _instructions(lines: list[str]) -> list[_Instruction]:
    """Group physical lines into logical instructions.

    Comment and blank lines inside a backslash continuation do not end the
    instruction, matching Docker's parser.
    """
    instructions: list[_Instruction] = []
    start: int | None = None
    parts: list[str] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if start is None:
            start, parts = index, []
        continued = stripped.endswith("\\")
        parts.append(stripped.removesuffix("\\").strip())
        if not continued:
            text = " ".join(part for part in parts if part)
            keyword = text.split(maxsplit=1)[0].upper()
            instructions.append(_Instruction(keyword, text, start, index))
            start = None
    if start is not None:
        raise ToolingStageError(f"Dockerfile line {start + 1}: unterminated continuation")
    return instructions


def _stage_bounds(instructions: list[_Instruction]) -> tuple[int, int]:
    """Return the indexes of the tooling stage's FROM and its last instruction."""
    froms = [index for index, item in enumerate(instructions) if item.keyword == "FROM"]
    if not froms:
        raise ToolingStageError("Dockerfile has no FROM instruction")
    for item in instructions[: froms[0]]:
        if item.keyword == "ARG":
            raise ToolingStageError(
                f"Dockerfile line {item.first_line + 1}: global ARG before the first FROM "
                "would change the stage without changing its text"
            )
    matches = [index for index in froms if _alias(instructions[index]) == STAGE]
    if len(matches) != 1:
        raise ToolingStageError(
            f"expected exactly one 'FROM … AS {STAGE}' stage, found {len(matches)}"
        )
    begin = matches[0]
    following = [index for index in froms if index > begin]
    end = (following[0] if following else len(instructions)) - 1
    return begin, end


def _alias(instruction: _Instruction) -> str | None:
    match = _STAGE_ALIAS.search(instruction.text)
    return match.group(1).lower() if match else None


def _require_self_contained(stage: list[_Instruction]) -> None:
    """Reject any stage input that its text does not fully describe."""
    head = stage[0]
    image = _STAGE_ALIAS.sub("", head.text).split()[-1]
    if not _DIGEST_PINNED.search(image):
        raise ToolingStageError(
            f"Dockerfile line {head.first_line + 1}: {STAGE} must start FROM a "
            f"@sha256 digest, found {image!r}"
        )
    for item in stage[1:]:
        location = f"Dockerfile line {item.first_line + 1}"
        if item.keyword in _CONTEXT_IMPORTS:
            raise ToolingStageError(f"{location}: {item.keyword} is not allowed in {STAGE}")
        if item.keyword == "RUN" and re.search(r"(^|\s)--mount[=\s]", item.text):
            raise ToolingStageError(f"{location}: RUN --mount is not allowed in {STAGE}")


def stage_text(dockerfile: Path) -> bytes:
    """Return the exact bytes of the tooling stage.

    The text runs from the ``FROM … AS riscv-tooling`` line through the stage's
    last instruction. Comments inside the stage are part of the key; comment
    and blank lines after its last instruction introduce the next stage and
    are not.
    """
    raw = dockerfile.read_bytes()
    lines = raw.decode("utf-8").splitlines(keepends=True)
    instructions = _instructions(lines)
    begin, end = _stage_bounds(instructions)
    _require_self_contained(instructions[begin : end + 1])
    first, last = instructions[begin].first_line, instructions[end].last_line
    return "".join(lines[first : last + 1]).encode("utf-8")


def tooling_key(dockerfile: Path) -> str:
    """Return the hexadecimal tooling key for ``dockerfile``."""
    digest = hashlib.sha256()
    digest.update(_KEY_SCHEMA + b"\0" + _PLATFORM + b"\0")
    digest.update(stage_text(dockerfile))
    return digest.hexdigest()


def tag(key: str) -> str:
    """Return the published image tag for a tooling key."""
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        raise ToolingStageError(f"malformed tooling key {key!r}")
    return f"{STAGE}-{key}"


def expected_labels(key: str) -> dict[str, str]:
    """Return the labels a published tooling image for ``key`` must carry."""
    return {ROLE_LABEL: STAGE, KEY_LABEL: key}


def published_image(reference: str, key: str) -> str | None:
    """Return the label-verified ``repository@digest`` of ``reference``.

    Returns ``None`` when the tag does not exist. Raises ``ValueError`` when it
    exists with the wrong labels and ``ToolingRegistryError`` when the
    registry lookup fails for any other reason.
    """
    try:
        return resolve_labeled_image_remote(reference, expected_labels(key))
    except subprocess.CalledProcessError as error:
        detail = str(error.stderr or "").strip()
        if any(marker in detail.lower() for marker in _ABSENT_MARKERS):
            return None
        raise ToolingRegistryError(f"cannot inspect {reference}: {detail or error}") from error


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    key = commands.add_parser("key", help="print the tooling key, tag, and image reference")
    published = commands.add_parser(
        "published", help="report whether the key's image exists with verified labels"
    )
    published.add_argument(
        "--reference", help="image to inspect (default: the key's final published tag)"
    )
    for command in (key, published):
        command.add_argument("--dockerfile", type=Path, default=DEFAULT_DOCKERFILE)
        command.add_argument("--github-output", type=Path, help="append outputs to this file")
    return parser.parse_args(argv)


def _outputs(arguments: argparse.Namespace, key: str) -> str:
    image = f"{REPOSITORY}:{tag(key)}"
    if arguments.command == "key":
        return f"key={key}\ntag={tag(key)}\nimage={image}\n"
    reference = arguments.reference or image
    verified = published_image(reference, key)
    if verified is None:
        return f"reference={reference}\nstate=absent\n"
    return f"reference={reference}\nstate=present\ndigest_reference={verified}\n"


def main(argv: list[str] | None = None) -> int:
    """Run the command-line interface."""
    arguments = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        outputs = _outputs(arguments, tooling_key(arguments.dockerfile))
    except (OSError, UnicodeDecodeError, ValueError, ToolingRegistryError) as error:
        print(f"riscv_tooling: {error}", file=sys.stderr)
        return 1
    if arguments.github_output is not None:
        with arguments.github_output.open("a", encoding="utf-8") as stream:
            stream.write(outputs)
    print(outputs, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
