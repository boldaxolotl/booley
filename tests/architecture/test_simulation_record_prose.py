"""Architecture guard: source prose says "Simulation Record", not "Simulation Campaign".

Every string, f-string text and comment under ``src`` names the durable run a Simulation
Record. The retired format labels below are the only places the old name stays, because
they tell a user what an older Booley wrote (#2909).
"""

from __future__ import annotations

import io
import tokenize
from pathlib import Path

from tests.source_checkout_support import source_checkout_files

_ROOT = Path(__file__).resolve().parents[2]
_OLD_NAME = "Simulation Campaign"
_PROSE_TOKENS = {
    tokenize.STRING,
    tokenize.COMMENT,
    getattr(tokenize, "FSTRING_MIDDLE", tokenize.STRING),
}
_RETIRED_LABELS: dict[str, tuple[str, ...]] = {
    "src/booley/flows/sim/record_layout.py": ("Simulation Campaign directory",),
    "src/booley/flows/sim/record/codec.py": (
        "Simulation Campaign manifest",
        "Simulation Campaign dependency",
        "Simulation Campaign summary",
    ),
}


def _old_name_offsets(text: str) -> list[int]:
    offsets: list[int] = []
    start = text.find(_OLD_NAME)
    while start != -1:
        offsets.append(start)
        start = text.find(_OLD_NAME, start + 1)
    return offsets


def test_source_prose_says_simulation_record() -> None:
    used: set[tuple[str, str]] = set()
    found: list[str] = []
    for path in source_checkout_files(_ROOT, "src"):
        if path.suffix != ".py":
            continue
        relative = path.relative_to(_ROOT).as_posix()
        labels = _RETIRED_LABELS.get(relative, ())
        for token in tokenize.tokenize(io.BytesIO(path.read_bytes()).readline):
            if token.type not in _PROSE_TOKENS:
                continue
            for offset in _old_name_offsets(token.string):
                label = next(
                    (item for item in labels if token.string.startswith(item, offset)), None
                )
                if label is None:
                    found.append(f"{relative}:{token.start[0]}: {token.string[offset:][:60]!r}")
                else:
                    used.add((relative, label))
    assert not found, 'say "Simulation Record":\n' + "\n".join(found)
    stale = sorted(
        f"{relative}: {label}"
        for relative, labels in _RETIRED_LABELS.items()
        for label in labels
        if (relative, label) not in used
    )
    assert not stale, "drop retired labels that no longer match:\n" + "\n".join(stale)
