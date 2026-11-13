"""Where a Simulation run keeps its Record, and the refusal of the layout Booley 0.4.0 used.

This is the one module that names the layout. It sits outside the
``booley.flows.sim.record`` package, and imports the codec lazily, so every build and
check site can import the constants at module level without an import cycle.
"""

from __future__ import annotations

import os
from pathlib import Path

# ``<invocation>/targets/<Target>/record/manifest.json`` is the Simulation Record.
RECORD_DIRECTORY = "record"
# ``.../attempts/<attempt>/coverage-record/`` is one attempt's Coverage Record.
COVERAGE_RECORD_DIRECTORY = "coverage-record"
# The name Booley 0.4.0 and earlier gave the Record directory. It is recognised only to be
# refused: it is never accepted as an alias. Every old ``coverage-campaign/`` directory lives
# under it, so this one name detects both old layouts.
_OLDER_RECORD_DIRECTORY = "campaign"


def _refusal(label: str, where: str, invocation: Path | None) -> Exception:
    """The Part 1 older-Booley refusal, for a layout rather than a schema."""
    from booley.flows.sim.record.codec import UnsupportedSimulationCampaignFormatError

    return UnsupportedSimulationCampaignFormatError(label, where, invocation)


def refuse_older_layout(invocation: Path) -> None:
    """Refuse an invocation directory holding a ``targets/<Target>/campaign`` directory."""
    for candidate in sorted((invocation / "targets").glob(f"*/{_OLDER_RECORD_DIRECTORY}")):
        raise _refusal(
            "Simulation Campaign directory",
            f"targets/{candidate.parent.name}/{_OLDER_RECORD_DIRECTORY}",
            invocation,
        )


def refuse_older_manifest_path(path: Path) -> None:
    """Refuse ``<invocation>/targets/<Target>/campaign/manifest.json``; ignore any other path.

    Any other path, such as a Mutation manifest, or a ``campaign/`` path that names no
    directory on disk (a habit from 0.4.0 aimed at a new run), falls through to the caller's
    own exact-name check. It is never diagnosed as an older record, so the user is never told
    to delete a Record that holds good data.
    """
    if (
        path.name == "manifest.json"
        and path.parent.name == _OLDER_RECORD_DIRECTORY
        and len(path.parents) > 3
        and path.parents[2].name == "targets"
        and os.path.lexists(path.parent)
    ):
        raise _refusal(
            "Simulation Campaign directory",
            f"targets/{path.parents[1].name}/{_OLDER_RECORD_DIRECTORY}",
            path.parents[3],
        )


def refuse_older_nested_path(path: str) -> None:
    """Refuse a Coverage reference whose nested path starts in the older ``campaign/`` directory."""
    if path.split("/", 1)[0] == _OLDER_RECORD_DIRECTORY:
        raise _refusal("Coverage Campaign path", path, None)
