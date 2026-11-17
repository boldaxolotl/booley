"""Real `bwave --format json` envelopes validated against the `bwave schema` document.

Fails, never skips, when the debug binary or `jsonschema` is missing: the
`bwave-integration` CI job builds the binary and installs the validator.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

THIS_DIR = Path(__file__).resolve().parent
BWAVE_SRC = THIS_DIR.parent
DEV_SUPPORT = BWAVE_SRC / "dev_support"
sys.path.insert(0, str(DEV_SUPPORT))

import check_schema

EXE_SUFFIX = ".exe" if sys.platform == "win32" else ""
BWAVE_BIN = BWAVE_SRC / "target" / "debug" / f"bwave{EXE_SUFFIX}"


def require_binary(path: Path) -> str:
    if not path.exists():
        pytest.fail(
            f"bwave binary missing at {path}; run `cargo build --manifest-path crates/bwave/Cargo.toml`"
        )
    return str(path)


@pytest.fixture(scope="module")
def validator() -> Any:
    return check_schema.load_validator(require_binary(BWAVE_BIN))


def test_every_json_command_matches_schema(validator: Any, tmp_path: Path) -> None:
    fixture = check_schema.find_fixture(BWAVE_SRC)
    cases = {name for name, _ in check_schema.query_cases(fixture)}
    assert cases == set(validator.schema["properties"]["command"]["enum"])
    assert check_schema.envelope_errors(str(BWAVE_BIN), validator, fixture, tmp_path) == []


def _unknown_property(envelope: dict[str, Any]) -> None:
    envelope["unexpected"] = True


def _mismatched_command(envelope: dict[str, Any]) -> None:
    envelope["command"] = "value"


def _negative_count(envelope: dict[str, Any]) -> None:
    envelope["data"]["signal_count"] = -1


@pytest.mark.parametrize(
    "corrupt",
    [
        pytest.param(_unknown_property, id="unknown-property"),
        pytest.param(_mismatched_command, id="mismatched-command-data"),
        pytest.param(_negative_count, id="negative-count"),
    ],
)
def test_schema_rejects_malformed_envelope(
    validator: Any, corrupt: Callable[[dict[str, Any]], None]
) -> None:
    fixture = check_schema.find_fixture(BWAVE_SRC)
    _, stdout, _ = check_schema.run_bwave(
        str(BWAVE_BIN), ["list", str(fixture), "--format", "json"]
    )
    envelope = json.loads(stdout)
    assert validator.is_valid(envelope)
    corrupt(envelope)
    assert not validator.is_valid(envelope)


def test_missing_binary_fails_instead_of_skipping(tmp_path: Path) -> None:
    with pytest.raises(pytest.fail.Exception, match="cargo build"):
        require_binary(tmp_path / "bwave")


def test_missing_validator_fails_import() -> None:
    code = f"import sys; sys.modules['jsonschema'] = None; sys.path.insert(0, {str(DEV_SUPPORT)!r}); import check_schema"
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )
    assert "ModuleNotFoundError" in proc.stderr
