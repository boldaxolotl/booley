"""Contracts for the runtime-base package inventory helper (#829, #1750)."""

import json
import runpy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_HELPER = Path(__file__).parents[2] / "src/booley/data/docker/base_package_inventory.py"
inventory = SimpleNamespace(**runpy.run_path(str(_HELPER)))


def _build(*rows: str) -> str:
    return inventory.build_document("\n".join(rows), "3.13.15", "26.2.1")


def test_installed_rows_are_kept_and_absent_rows_are_dropped() -> None:
    text = _build(
        "gcc\t13\tamd64\tinstalled", "a\t1\tamd64\tnot-installed", "b\t2\tamd64\tconfig-files"
    )

    assert json.loads(text)["packages"] == [
        {"architecture": "amd64", "name": "gcc", "version": "13"}
    ]


def test_multiarch_rows_stay_separate() -> None:
    rows = json.loads(_build("libc6\t2\ti386\tinstalled", "libc6\t2\tamd64\tinstalled"))[
        "packages"
    ]

    assert [row["architecture"] for row in rows] == ["amd64", "i386"]


def test_reordered_dpkg_input_gives_identical_bytes() -> None:
    rows = ("zlib1g\t1:1.3\tamd64\tinstalled", "bash\t5.2\tamd64\tinstalled")

    assert _build(*rows) == _build(*reversed(rows))


@pytest.mark.parametrize(
    "text", ["{", '{"schema": 2}', '{"schema": 1, "python": {"version": "3"}, "packages": []}']
)
def test_validate_rejects_missing_fields_or_bad_json(text: str) -> None:
    assert inventory.validate_document(text)


def test_validate_cli_exit_status(tmp_path: Path) -> None:
    path = tmp_path / "inventory.json"
    command = [sys.executable, "-I", str(_HELPER), "validate", str(path)]
    missing = subprocess.run(command, capture_output=True, check=False, timeout=60)
    path.write_text(_build("bash\t5.2\tamd64\tinstalled"), encoding="utf-8")

    assert missing.returncode == 1
    assert subprocess.run(command, capture_output=True, check=False, timeout=60).returncode == 0
