"""Release validation requires the measurement round trip's evidence."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from tests.subprocess_budget import run_bounded_child

ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / ".github/scripts/release_validation/measurement_evidence.py"
DIGEST = "sha256:" + "a" * 64
GOOD_PIN = "1" * 40
BAD_PIN = "2" * 40


def _evidence() -> dict[str, Any]:
    return {
        "schema": 1,
        "image_digest": DIGEST,
        "demo": {
            "commit": "3" * 40,
            "booley_directory_commit": "4" * 40,
            "run_id": "measure-1",
            "verdict": "1 of 1 enforced met",
            "exit_code": 0,
            "measured": True,
        },
        "submodule": {
            "pinned": GOOD_PIN,
            "live": BAD_PIN,
            "exit_code": 0,
            "measured": True,
            "bad_pin": {"exit_code": 1, "errors": 1, "verdict": "unmet"},
        },
    }


def _run(
    tmp_path: Path, document: object, digest: str = DIGEST
) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "measurement.json"
    if document is not None:
        path.write_text(document if isinstance(document, str) else json.dumps(document))
    return run_bounded_child(
        [sys.executable, "-I", str(SCRIPT), "--evidence", str(path), "--image-digest", digest],
        timeout=30,
        check=False,
    )


def test_complete_measurement_evidence_passes(tmp_path: Path) -> None:
    result = _run(tmp_path, _evidence())
    assert result.returncode == 0, result.stderr
    assert "measurement evidence passed" in result.stdout


def _edit(*path: str, value: object) -> dict[str, Any]:
    document = _evidence()
    node: Any = document
    for key in path[:-1]:
        node = node[key]
    if value is _MISSING:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return document


_MISSING = object()
_CASES = [
    ("missing-file", None, "cannot read measurement evidence"),
    ("malformed-json", "{", "malformed JSON"),
    ("not-an-object", [], "must be a mapping"),
    ("schema", _edit("schema", value=2), "schema must be 1"),
    ("digest", _edit("image_digest", value="sha256:other"), "image_digest differs"),
    ("demo-exit", _edit("demo", "exit_code", value=1), "demo measure must exit 0"),
    ("unmeasured", _edit("demo", "measured", value=False), "demo run must be Measured"),
    ("verdict", _edit("demo", "verdict", value=" "), "demo.verdict must not be blank"),
    ("demo-commit", _edit("demo", "commit", value="abc"), "demo.commit"),
    ("no-demo", _edit("demo", value=_MISSING), "demo must be a mapping"),
    ("no-submodule", _edit("submodule", value=_MISSING), "submodule must be a mapping"),
    ("sub-exit", _edit("submodule", "exit_code", value=1), "submodule measure must exit 0"),
    ("same-commit", _edit("submodule", "live", value=GOOD_PIN), "must differ"),
    ("no-bad-pin", _edit("submodule", "bad_pin", value=_MISSING), "bad_pin must be a mapping"),
    ("bad-pin-passes", _edit("submodule", "bad_pin", "exit_code", value=0), "must exit 1"),
    ("bad-pin-errors", _edit("submodule", "bad_pin", "exit_code", value=2), "must exit 1"),
    ("no-lint-errors", _edit("submodule", "bad_pin", "errors", value=0), "above 0"),
    ("bool-errors", _edit("submodule", "bad_pin", "errors", value=True), "above 0"),
    ("verdict-met", _edit("submodule", "bad_pin", "verdict", value="met"), "must be unmet"),
]


def test_missing_or_failing_measurement_evidence_fails(tmp_path: Path) -> None:
    for index, (name, document, message) in enumerate(copy.deepcopy(_CASES)):
        directory = tmp_path / str(index)
        directory.mkdir()
        result = _run(directory, document)
        assert result.returncode == 2, name
        assert message in result.stderr, name
        assert not result.stdout, name
