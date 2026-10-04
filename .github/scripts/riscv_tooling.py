#!/usr/bin/env python3
"""Identify the reusable RISC-V tooling stage of ``Dockerfile.riscv`` (ADR 0070).

The tooling key is a hash of the ``riscv-tooling`` stage's exact text, a key
schema version, and the platform. The stage is digest-pinned, declares every
``ARG`` it uses, and copies nothing from the build context, so its text covers
every input Booley pins. This module enforces those preconditions and fails
closed when the Dockerfile no longer satisfies them: a key that silently missed
an input would let CI reuse a stale toolchain.

It also answers whether a key is already published, confirms a promotion
published the verified bytes, and renders the runtime contract's RISC-V checks
for a bare tooling image. Only a digest whose role and key labels match is
trusted: GHCR tags are mutable.

For the ``Tests`` workflow it selects the **tooling source**: ``registry``
reuses the published digest, ``local`` builds the stage in CI, and
``local-compat-fallback`` records a published image that failed the composed
candidate's quick compatibility check. The JSON source record is the evidence
the run's duration budget and the #568 savings report are derived from.

Runs from a plain checkout before Booley is installed; it imports only
stdlib-only modules under ``src/``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_ROOT / "src"))

from booley.core.boundary import as_dict
from booley.runtime.docker_base_contract import RemoteImage, resolve_labeled_image_remote
from booley.runtime.dockerfile_syntax import Instruction, logical_instructions
from booley.runtime.image_provenance import ImageProvenanceError, resolve_recipe_fingerprint

STAGE = "riscv-tooling"
DEFAULT_DOCKERFILE = _ROOT / "src/booley/data/docker/Dockerfile.riscv"
DEFAULT_CONTRACT = _ROOT / ".github/contracts/session-runtime.toml"
REPOSITORY = "ghcr.io/boldaxolotl/booley-sandbox-base"
# Bump the schema when the key derivation itself changes meaning.
_KEY_SCHEMA = b"riscv-tooling-v1"
_PLATFORM = b"linux/amd64"
_DIGEST_PINNED = re.compile(r"@sha256:[0-9a-f]{64}$")
_STAGE_ALIAS = re.compile(r"\s+AS\s+\S+\s*$", re.IGNORECASE)
_CONTEXT_IMPORTS = frozenset({"ADD", "COPY"})
ROLE_LABEL = "io.booley.artifact.role"
KEY_LABEL = "io.booley.riscv-tooling.key"
RECIPE_LABEL = "io.booley.build.recipe-fingerprint"
# Contract paths the tooling stage itself must ship.
_TOOLING_PATH_PREFIXES = ("/opt/riscv/", "/opt/riscv-docs/")
# Why CI may skip the registry entirely: the `cold` measurement arm forces a
# local build, and a registry lookup that exceeded the workflow's timeout
# cannot be distinguished from absence safely, so it also builds locally.
FORCED_LOCAL_REASONS = ("cold", "registry-timeout")
RECORD_SCHEMA = 1
TOOLING_SOURCES = ("registry", "local", "local-compat-fallback")


class ToolingStageError(ValueError):
    """Raised when the tooling stage cannot be keyed safely."""


class ToolingRegistryError(RuntimeError):
    """Raised when the registry cannot say whether a tooling image exists."""


def _stage_bounds(instructions: tuple[Instruction, ...]) -> tuple[int, int]:
    """Return the indexes of the tooling stage's FROM and its last instruction."""
    froms = [index for index, item in enumerate(instructions) if item.keyword == "FROM"]
    if not froms:
        raise ToolingStageError("Dockerfile has no FROM instruction")
    for item in instructions[: froms[0]]:
        if item.keyword == "ARG":
            raise ToolingStageError(
                f"Dockerfile line {item.line}: global ARG before the first FROM "
                "would change the stage without changing its text"
            )
    matches = [index for index in froms if instructions[index].stage_alias == STAGE]
    if len(matches) != 1:
        raise ToolingStageError(
            f"expected exactly one 'FROM … AS {STAGE}' stage, found {len(matches)}"
        )
    begin = matches[0]
    following = [index for index in froms if index > begin]
    end = (following[0] if following else len(instructions)) - 1
    return begin, end


def _require_self_contained(stage: tuple[Instruction, ...]) -> None:
    """Reject any stage input that its text does not fully describe."""
    head = stage[0]
    image = _STAGE_ALIAS.sub("", head.value).split()[-1]
    if not _DIGEST_PINNED.search(image):
        raise ToolingStageError(
            f"Dockerfile line {head.line}: {STAGE} must start FROM a "
            f"@sha256 digest, found {image!r}"
        )
    for item in stage[1:]:
        location = f"Dockerfile line {item.line}"
        if item.keyword in _CONTEXT_IMPORTS:
            raise ToolingStageError(f"{location}: {item.keyword} is not allowed in {STAGE}")
        if item.keyword == "RUN" and re.search(r"(^|\s)--mount[=\s]", item.value):
            raise ToolingStageError(f"{location}: RUN --mount is not allowed in {STAGE}")


def stage_text(dockerfile: Path) -> bytes:
    """Return the exact bytes of the tooling stage.

    The text runs from the ``FROM … AS riscv-tooling`` line through the stage's
    last instruction. Comments inside the stage are part of the key; comment
    and blank lines after its last instruction introduce the next stage and
    are not.
    """
    text = dockerfile.read_bytes().decode("utf-8")
    try:
        instructions = logical_instructions(text)
    except ValueError as error:
        raise ToolingStageError(str(error)) from error
    begin, end = _stage_bounds(instructions)
    _require_self_contained(instructions[begin : end + 1])
    lines = text.splitlines(keepends=True)
    first, last = instructions[begin].line, instructions[end].last_line
    return "".join(lines[first - 1 : last]).encode("utf-8")


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


def final_image(key: str) -> str:
    """Return the final published reference for a tooling key."""
    return f"{REPOSITORY}:{tag(key)}"


def expected_labels(key: str) -> dict[str, str]:
    """Return the labels a published tooling image for ``key`` must carry."""
    return {ROLE_LABEL: STAGE, KEY_LABEL: key}


def _is_missing_tag(reference: str, stderr: str) -> bool:
    """Return whether ``stderr`` is buildx's exact report of a missing tag.

    Anything else, including authorization or transport errors that merely
    mention "not found", is a registry failure: treating it as absence would
    let the publisher overwrite a tag it could not see.
    """
    return stderr.strip() == f"ERROR: {reference}: not found"


def published_image(reference: str, key: str) -> RemoteImage | None:
    """Return the label-verified image behind ``reference``.

    Returns ``None`` only when the tag does not exist. Raises ``ValueError``
    when it exists with the wrong labels and ``ToolingRegistryError`` when the
    registry lookup fails for any other reason.
    """
    try:
        return resolve_labeled_image_remote(reference, expected_labels(key))
    except subprocess.CalledProcessError as error:
        detail = str(error.stderr or "").strip()
        if _is_missing_tag(reference, detail):
            return None
        raise ToolingRegistryError(f"cannot inspect {reference}: {detail or error}") from error


def verify_promotion(candidate: str, key: str) -> RemoteImage:
    """Confirm the key's final tag serves the verified candidate's image.

    Promotion writes a new index (it adds an annotation), so the final digest
    differs from the candidate's. The Linux/AMD64 image manifest pins the
    config and every layer, so equal manifests prove the published bytes are
    the verified bytes.
    """
    final = final_image(key)
    published = published_image(final, key)
    if published is None:
        raise ToolingRegistryError(f"{final} is missing after promotion")
    verified = published_image(candidate, key)
    if verified is None:
        raise ToolingRegistryError(f"candidate {candidate} is missing")
    if published.platform_manifest != verified.platform_manifest:
        raise ValueError(
            f"{final} serves image manifest {published.platform_manifest}, "
            f"but the verified candidate is {verified.platform_manifest}"
        )
    return published


def resolve_source(dockerfile: Path, force_local: str | None = None) -> dict[str, object]:
    """Select where CI gets the tooling stage and return the source record.

    A label-verified published digest is a ``registry`` source. A missing tag
    or an unreachable registry is a ``local`` source: availability wins over
    speed, so a registry problem never fails the candidate. A tag with the
    wrong labels raises ``ValueError``; that is an integrity failure, not a
    miss.
    """
    key = tooling_key(dockerfile)
    reference = final_image(key)
    record: dict[str, object] = {
        "schema_version": RECORD_SCHEMA,
        "key": key,
        "reference": reference,
    }
    if force_local is not None:
        return {**record, "source": "local", "reason": force_local}
    try:
        image = published_image(reference, key)
    except ToolingRegistryError as error:
        return {**record, "source": "local", "reason": "registry-error", "detail": str(error)}
    if image is None:
        return {**record, "source": "local", "reason": "absent"}
    return {
        **record,
        "source": "registry",
        "reason": "published",
        "digest_reference": image.digest_reference,
        "platform_manifest": image.platform_manifest,
    }


def read_source_record(path: Path) -> dict[str, object]:
    """Return a validated tooling source record; raise ``ValueError`` otherwise."""
    try:
        record = as_dict(json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError as error:
        raise ValueError(f"{path} is not a tooling source record: {error}") from error
    if record is None or record.get("schema_version") != RECORD_SCHEMA:
        raise ValueError(f"{path} is not a version {RECORD_SCHEMA} tooling source record")
    if record.get("source") not in TOOLING_SOURCES:
        raise ValueError(f"{path} names an unknown tooling source: {record.get('source')!r}")
    return record


def _write_record(path: Path, record: dict[str, object]) -> None:
    """Replace the record atomically so a reader never sees half of it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def compat_fallback(path: Path, detail: str) -> dict[str, object]:
    """Rewrite a registry source record as a compatibility fallback.

    The composed candidate failed its quick check against the published
    tooling, so CI rebuilds the stage locally. The rejected digest stays in
    the record; repeated fallbacks for one key mean it needs republishing.
    """
    record = read_source_record(path)
    if record.get("source") != "registry":
        raise ValueError(f"{path} does not record a registry tooling source")
    rejected = record.pop("digest_reference")
    record.pop("platform_manifest", None)
    record.update(
        source="local-compat-fallback",
        reason="compat-check-failed",
        detail=detail,
        rejected_digest_reference=rejected,
    )
    _write_record(path, record)
    return record


def tooling_checks(contract: Path) -> str:
    """Return a bash script that checks a bare tooling image.

    It reuses the session runtime contract instead of restating it: every
    ``[riscv]`` probe, plus each required path the tooling stage ships. Each
    probe runs in its own ``bash -euo pipefail`` so ``set -e`` stays effective.
    """
    with contract.open("rb") as stream:
        section = tomllib.load(stream)["riscv"]
    lines = ["set -euo pipefail"]
    for path in section.get("required_paths", []):
        if path.startswith(_TOOLING_PATH_PREFIXES):
            lines.append(f"test -e {shlex.quote(path)}")
    for probe in section.get("probe", []):
        name = shlex.quote(f"tooling check failed: {probe['name']}")
        lines.append(
            f"bash -euo pipefail -c {shlex.quote(probe['command'])} "
            f"|| {{ echo {name} >&2; exit 1; }}"
        )
    return "\n".join(lines) + "\n"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    key = commands.add_parser("key", help="print the tooling key, tag, image, and labels")
    published = commands.add_parser(
        "published", help="report whether the key's image exists with verified labels"
    )
    published.add_argument(
        "--reference", help="image to inspect (default: the key's final published tag)"
    )
    promoted = commands.add_parser(
        "promoted", help="verify the final tag serves the verified candidate's image"
    )
    promoted.add_argument("--candidate", required=True, help="verified candidate digest reference")
    checks = commands.add_parser("checks", help="print the bash checks for a tooling image")
    checks.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    resolve = commands.add_parser(
        "resolve", help="select the tooling source for CI and write its source record"
    )
    resolve.add_argument("--record", type=Path, required=True, help="source record to write")
    resolve.add_argument(
        "--force-local", choices=FORCED_LOCAL_REASONS, help="build locally for this reason"
    )
    fallback = commands.add_parser(
        "fallback", help="record that the published tooling failed the compatibility check"
    )
    fallback.add_argument("--record", type=Path, required=True, help="source record to rewrite")
    fallback.add_argument("--detail", required=True, help="why the check failed")
    for command in (key, published, promoted, resolve):
        command.add_argument("--dockerfile", type=Path, default=DEFAULT_DOCKERFILE)
        command.add_argument("--github-output", type=Path, help="append outputs to this file")
    return parser.parse_args(argv)


def _key_outputs(dockerfile: Path, key: str) -> str:
    labels = {
        **expected_labels(key),
        RECIPE_LABEL: resolve_recipe_fingerprint((dockerfile,)),
    }
    label_lines = "".join(f"{name}={value}\n" for name, value in labels.items())
    return (
        f"key={key}\ntag={tag(key)}\nimage={final_image(key)}\n"
        f"labels<<RISCV_TOOLING_LABELS\n{label_lines}RISCV_TOOLING_LABELS\n"
    )


def _image_outputs(reference: str, image: RemoteImage | None) -> str:
    if image is None:
        return f"reference={reference}\nstate=absent\n"
    return (
        f"reference={reference}\nstate=present\n"
        f"digest_reference={image.digest_reference}\n"
        f"platform_manifest={image.platform_manifest}\n"
    )


def _resolve_outputs(arguments: argparse.Namespace) -> str:
    record = resolve_source(arguments.dockerfile, arguments.force_local)
    _write_record(arguments.record, record)
    outputs = f"key={record['key']}\nsource={record['source']}\nreason={record['reason']}\n"
    if record["source"] == "registry":
        # Only the immutable, label-verified digest reaches BuildKit.
        outputs += f"context=docker-image://{record['digest_reference']}\n"
    elif record["reason"] == "registry-error":
        # Stdout carries GitHub workflow commands; stderr would not annotate.
        print(f"::warning::RISC-V tooling builds locally: {record['detail']}")
    return outputs


def _outputs(arguments: argparse.Namespace) -> str:
    if arguments.command == "checks":
        return tooling_checks(arguments.contract)
    if arguments.command == "resolve":
        return _resolve_outputs(arguments)
    if arguments.command == "fallback":
        record = compat_fallback(arguments.record, arguments.detail)
        return f"source={record['source']}\n"
    key = tooling_key(arguments.dockerfile)
    if arguments.command == "key":
        return _key_outputs(arguments.dockerfile, key)
    if arguments.command == "promoted":
        return _image_outputs(final_image(key), verify_promotion(arguments.candidate, key))
    reference = arguments.reference or final_image(key)
    return _image_outputs(reference, published_image(reference, key))


def main(argv: list[str] | None = None) -> int:
    """Run the command-line interface."""
    arguments = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        outputs = _outputs(arguments)
    except (
        OSError,
        ImageProvenanceError,
        KeyError,
        UnicodeDecodeError,
        ValueError,
        tomllib.TOMLDecodeError,
        ToolingRegistryError,
    ) as error:
        print(f"riscv_tooling: {error}", file=sys.stderr)
        return 1
    github_output = getattr(arguments, "github_output", None)
    if github_output is not None:
        with github_output.open("a", encoding="utf-8") as stream:
            stream.write(outputs)
    print(outputs, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
