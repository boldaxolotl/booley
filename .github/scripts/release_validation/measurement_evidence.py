"""Require the measurement round trip's proof before recording a release check.

The document comes from ``measure_round_trip.py``: the public demo measured to a
Measured Commit, and a submodule measured at its pin, not at its live tree.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parents[3] / "src"))

from booley.core.boundary import BoundaryError, require_dict, require_int, require_str_value

_SHA = re.compile(r"[0-9a-f]{40}")


def _read_document(path: Path) -> dict[str, Any]:
    try:
        contents = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"cannot read measurement evidence {path}: {exc}") from exc
    try:
        return require_dict(json.loads(contents), field="measurement evidence")
    except json.JSONDecodeError as exc:
        raise ValueError(f"measurement evidence {path}: malformed JSON: {exc}") from exc
    except BoundaryError as exc:
        raise ValueError(str(exc)) from exc


def _require_sha(value: object, *, field: str) -> str:
    sha = require_str_value(value, field=field)
    if not _SHA.fullmatch(sha):
        raise ValueError(f"{field} must be a full lowercase Git commit SHA")
    return sha


def _require_measured(leg: dict[str, Any], *, name: str) -> None:
    if require_int(leg.get("exit_code"), field=f"{name}.exit_code") != 0:
        raise ValueError(f"{name} measure must exit 0")
    if leg.get("measured") is not True:
        raise ValueError(f"{name} run must be Measured")


def _require_demo(demo: dict[str, Any]) -> None:
    _require_sha(demo.get("commit"), field="demo.commit")
    _require_measured(demo, name="demo")
    if not require_str_value(demo.get("verdict"), field="demo.verdict").strip():
        raise ValueError("demo.verdict must not be blank")


def _require_submodule(submodule: dict[str, Any]) -> None:
    _require_measured(submodule, name="submodule")
    pinned = _require_sha(submodule.get("pinned"), field="submodule.pinned")
    live = _require_sha(submodule.get("live"), field="submodule.live")
    if pinned == live:
        raise ValueError("submodule.pinned must differ from submodule.live")
    bad = require_dict(submodule.get("bad_pin"), field="submodule.bad_pin")
    if require_int(bad.get("exit_code"), field="bad_pin.exit_code") != 1:
        raise ValueError("submodule bad pin must exit 1")
    if bad.get("verdict") != "unmet":
        raise ValueError("submodule bad pin errors verdict must be unmet")
    errors = bad.get("errors")
    if isinstance(errors, bool) or not isinstance(errors, int) or errors <= 0:
        raise ValueError("submodule bad pin errors must be an integer above 0")


def validate(evidence: Path, *, image_digest: str) -> None:
    """Read the evidence and require both legs of the round trip."""
    document = _read_document(evidence)
    try:
        if require_int(document.get("schema"), field="schema") != 1:
            raise ValueError("measurement evidence schema must be 1")
        if document.get("image_digest") != image_digest:
            raise ValueError("measurement evidence image_digest differs from the candidate")
        _require_demo(require_dict(document.get("demo"), field="demo"))
        _require_submodule(require_dict(document.get("submodule"), field="submodule"))
    except BoundaryError as exc:
        raise ValueError(str(exc)) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--image-digest", required=True)
    args = parser.parse_args(argv)
    try:
        validate(args.evidence, image_digest=args.image_digest)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print("measurement evidence passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
