"""Real Git controls for the public companion construction recipe."""

import json
import subprocess
import sys
from pathlib import Path

BUILDER = Path(__file__).resolve().parents[2] / "qa/scenarios/taxi/fixtures/build_submodules.py"


def test_companion_freezes_two_distinct_recursive_histories(tmp_path):
    root = tmp_path / "fixture"
    result = subprocess.run(
        [sys.executable, str(BUILDER), str(root)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["outer"]["A"] != manifest["outer"]["B"]
    assert manifest["data"]["A"] != manifest["data"]["B"]
    assert manifest["leaf"]["A"] != manifest["leaf"]["B"]
    data = root / "companion/deps/data"
    baseline = subprocess.run(
        ["git", "-C", str(data), "show", manifest["data"]["A"] + ":rtl/data.sv"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert "16'h1357" in baseline.stdout
    assert "16'h2468" in (data / "rtl/data.sv").read_text()
    assert "qa_leaf" in (data / "rtl/data.sv").read_text()
    assert "8'h34" in (data / "nested/leaf/rtl/leaf.sv").read_text()


def test_failed_construction_can_be_retried_without_manual_cleanup(tmp_path, monkeypatch):
    import pytest
    from qa.scenarios.taxi.fixtures import build_submodules

    root = tmp_path / "fixture"
    with monkeypatch.context() as patch:
        patch.setenv("PATH", "")
        with pytest.raises(FileNotFoundError):
            build_submodules.construct(root)
    assert not root.exists()
    assert build_submodules.construct(root)["outer"]["B"]
