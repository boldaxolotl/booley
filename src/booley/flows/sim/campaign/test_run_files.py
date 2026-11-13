"""Where each Test Run's log and waveform are on disk (issue #2758).

A Campaign keeps a Test Run's evidence under its work item's accepted attempt:
``work-items/<NNNN>-<digest>/attempts/<NNNN>-<attempt id>/``. The names there
say nothing about the Test Run, so reports and tools ask this module instead
of searching. The accepted attempt is the one the acceptance facts consumed:
its ``result.json`` binds the final attempt of the work item and lists the
evidence that attempt recorded.

Every lookup is read-only presentation: a record that is missing, unreadable or
malformed yields no files rather than an error, so a report never fails a run.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from .codec import (
    SimulationCampaignIntegrityError,
    UnsupportedSimulationCampaignFormatError,
    decode_simulation_result,
    validate_relative_path,
)

__all__ = ["TestRunFiles", "campaign_test_run_files"]

#: Evidence kind of a Campaign work item's simulator log. A Campaign's
#: execution has no artifact root, so it records only the live log, never a
#: ``run_log`` archive.
LOG_KIND = "live_run_log"

#: Evidence kind of the waveform a ``--trace`` run captured.
TRACE_KIND = "trace"


@dataclass(frozen=True, slots=True)
class TestRunFiles:
    """One Test Run's evidence files; ``None`` where the file does not exist."""

    # Not a pytest class, although its name starts with ``Test``.
    __test__ = False

    log: Path | None
    trace: Path | None

    def relative_to(self, base: Path) -> dict[str, str | None]:
        """The ``log`` and ``trace`` keys of a report row: POSIX paths from *base*, or ``None``."""
        return {
            "log": _relative(self.log, base),
            "trace": _relative(self.trace, base),
        }


_NO_FILES = TestRunFiles(None, None)


class _Facts(Protocol):
    @property
    def document(self) -> Mapping[str, object]: ...


class _Outcome(Protocol):
    @property
    def manifest_path(self) -> Path: ...

    @property
    def observations(self) -> tuple[Mapping[str, object], ...]: ...

    @property
    def acceptance_facts(self) -> _Facts: ...


def campaign_test_run_files(outcome: _Outcome) -> tuple[TestRunFiles, ...]:
    """The files of each observation of *outcome*, aligned with ``outcome.observations``.

    The Test Runs of one Cocotb batch share its work item, so they share its
    log and waveform. A work item without a consumed result (it never ran, or
    the campaign was interrupted) has no files.
    """
    facts = getattr(getattr(outcome, "acceptance_facts", None), "document", {})
    consumed = cast(Sequence[Mapping[str, object]], facts.get("consumed_results", ()))
    # ``<invocation>/targets/<Target>/record/manifest.json``: a manifest kept
    # anywhere else names no invocation to resolve the results against.
    ancestors = outcome.manifest_path.parents
    if len(ancestors) < 4:
        return (_NO_FILES,) * len(outcome.observations)
    invocation = ancestors[3]
    by_work_item = {
        item["work_item_id"]: _work_item_files(
            invocation, cast(Mapping[str, object], item["result"])["path"]
        )
        for item in consumed
    }
    return tuple(
        by_work_item.get(observation["work_item_id"], _NO_FILES)
        for observation in outcome.observations
    )


def _relative(path: Path | None, base: Path) -> str | None:
    """*path* as POSIX text relative to the directory *base*, so a copied run keeps it valid."""
    return None if path is None else Path(os.path.relpath(path, base)).as_posix()


def _work_item_files(invocation: Path, relative: object) -> TestRunFiles:
    """The log and waveform of the attempt whose ``result.json`` is at *relative*."""
    try:
        result_path = invocation / validate_relative_path(relative)
        result = decode_simulation_result(result_path.read_bytes()).document
        attempt = (
            result_path.parent
            / "attempts"
            / (f"{cast(int, result['attempt_ordinal']):04d}-{result['attempt_id']}")
        )
        evidence = cast(Sequence[Mapping[str, object]], result["evidence"])
        return TestRunFiles(
            _evidence_file(attempt, evidence, LOG_KIND),
            _evidence_file(attempt, evidence, TRACE_KIND),
        )
    except (
        OSError,
        KeyError,
        TypeError,
        ValueError,
        SimulationCampaignIntegrityError,
        UnsupportedSimulationCampaignFormatError,
    ):
        # Includes a result whose fields have the wrong shape: ``SimulationResult``
        # checks canonical JSON, not the types of its members.
        return _NO_FILES


def _evidence_file(
    attempt: Path, evidence: Sequence[Mapping[str, object]], kind: str
) -> Path | None:
    """The existing file of the first *evidence* reference of *kind*, else ``None``."""
    for reference in evidence:
        if reference["kind"] == kind:
            path = attempt / validate_relative_path(reference["path"])
            return path if path.is_file() else None
    return None
