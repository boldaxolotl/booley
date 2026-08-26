"""Materialize exact, isolated RTL mutation variants without parsing HDL.

The creator proposes one exact source replacement per mutation.  This module
owns the mutation seam: it validates those proposals against the pristine
source snapshot, applies one proposal at a time, and restores the original
bytes after the caller builds or simulates the variant.

No SystemVerilog or Verilog structure is inferred here.  Syntax and language
semantics belong to the project's configured compiler; this module deals only
in byte-exact source spans.
"""

from __future__ import annotations

import contextlib
import hashlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class MutationProposal(Protocol):
    """The proposal fields consumed by :class:`MutationVariantPlan`."""

    index: int
    file: str
    line: int
    original_code: str
    mutated_code: str


class MutationVariantError(ValueError):
    """A proposal cannot be anchored safely in the pristine source."""


@dataclass(frozen=True)
class ResolvedMutation:
    """One proposal anchored to an exact byte range in a pristine file."""

    index: int
    file: str
    line: int
    start: int
    end: int
    original: bytes
    replacement: bytes


class MutationVariantPlan:
    """Validated mutations plus the pristine source bytes they derive from.

    Interface invariants:

    * every proposal names an allowed scope file and a unique positive index;
    * ``original_code`` occurs exactly once on the declared starting line;
    * original and replacement text are non-empty and different;
    * applying a variant changes one exact span and always restores the file.
    """

    def __init__(
        self,
        work_dir: Path,
        originals: dict[str, bytes],
        mutations: tuple[ResolvedMutation, ...],
    ) -> None:
        self._work_dir = work_dir
        self._originals = originals
        self._mutations = mutations
        self._by_index = {mutation.index: mutation for mutation in mutations}

    @classmethod
    def resolve(
        cls,
        proposals: Sequence[MutationProposal],
        work_dir: Path,
        scope_files: Sequence[str],
    ) -> MutationVariantPlan:
        """Resolve proposals against pristine source or raise one clear error."""
        allowed = set(scope_files)
        originals = cls._read_originals(work_dir, allowed)
        seen: set[int] = set()
        resolved: list[ResolvedMutation] = []
        for proposal in proposals:
            if proposal.index <= 0 or proposal.index in seen:
                raise MutationVariantError(
                    f"mutation index must be unique and positive: {proposal.index}"
                )
            seen.add(proposal.index)
            if proposal.file not in allowed:
                raise MutationVariantError(
                    f"mutation #{proposal.index} escapes scope: {proposal.file}"
                )
            resolved.append(cls._resolve_one(proposal, originals[proposal.file]))
        return cls(work_dir, originals, tuple(resolved))

    @staticmethod
    def _read_originals(work_dir: Path, scope_files: set[str]) -> dict[str, bytes]:
        originals: dict[str, bytes] = {}
        for rel in scope_files:
            try:
                originals[rel] = (work_dir / rel).read_bytes()
            except OSError as exc:
                raise MutationVariantError(
                    f"cannot read mutation scope file {rel}: {exc}"
                ) from exc
        return originals

    @staticmethod
    def _resolve_one(proposal: MutationProposal, source: bytes) -> ResolvedMutation:
        if proposal.line <= 0:
            raise MutationVariantError(
                f"mutation #{proposal.index} has invalid source line {proposal.line}"
            )
        original = proposal.original_code.encode("utf-8")
        replacement = proposal.mutated_code.encode("utf-8")
        if not original or not replacement:
            raise MutationVariantError(
                f"mutation #{proposal.index} must provide non-empty exact source text"
            )
        if original == replacement:
            raise MutationVariantError(
                f"mutation #{proposal.index} replacement is identical to the original"
            )
        offsets = _all_offsets(source, original)
        line_offsets = [offset for offset in offsets if _line_at(source, offset) == proposal.line]
        if len(line_offsets) != 1:
            detail = "not found" if not line_offsets else "ambiguous"
            raise MutationVariantError(
                f"mutation #{proposal.index} original_code is {detail} on "
                f"{proposal.file}:{proposal.line}; return the exact source slice and line"
            )
        start = line_offsets[0]
        return ResolvedMutation(
            index=proposal.index,
            file=proposal.file,
            line=proposal.line,
            start=start,
            end=start + len(original),
            original=original,
            replacement=replacement,
        )

    @property
    def mutations(self) -> tuple[ResolvedMutation, ...]:
        return self._mutations

    @contextlib.contextmanager
    def applied(self, index: int) -> Iterator[ResolvedMutation]:
        """Apply exactly one mutation for the duration of the context."""
        mutation = self._by_index[index]
        path = self._work_dir / mutation.file
        pristine = self._originals[mutation.file]
        variant = _replace_exact(pristine, mutation)
        try:
            path.write_bytes(variant)
            yield mutation
        finally:
            path.write_bytes(pristine)

    def write_variants(self, destination: Path) -> dict[int, Path]:
        """Write one inspectable mutated source per proposal."""
        written: dict[int, Path] = {}
        for mutation in self._mutations:
            path = destination / f"mutant_{mutation.index}" / mutation.file
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(_replace_exact(self._originals[mutation.file], mutation))
            written[mutation.index] = path
        return written

    def source_fingerprint(self) -> str:
        """Return one stable digest for the pristine scope snapshot."""
        digest = hashlib.sha256()
        for rel, source in sorted(self._originals.items()):
            digest.update(rel.encode("utf-8"))
            digest.update(b"\0")
            digest.update(source)
            digest.update(b"\0")
        return f"sha256:{digest.hexdigest()}"


def _all_offsets(source: bytes, needle: bytes) -> list[int]:
    offsets: list[int] = []
    start = 0
    while (offset := source.find(needle, start)) >= 0:
        offsets.append(offset)
        start = offset + 1
    return offsets


def _line_at(source: bytes, offset: int) -> int:
    return source.count(b"\n", 0, offset) + 1


def _replace_exact(source: bytes, mutation: ResolvedMutation) -> bytes:
    if source[mutation.start : mutation.end] != mutation.original:
        raise MutationVariantError(
            f"mutation #{mutation.index} source changed after proposal resolution"
        )
    return source[: mutation.start] + mutation.replacement + source[mutation.end :]
