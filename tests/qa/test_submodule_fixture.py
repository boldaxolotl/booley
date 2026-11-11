"""Real Git controls for the public companion construction recipe."""

import json
import sys
from pathlib import Path

from tests.subprocess_budget import run_bounded_child

BUILDER = Path(__file__).resolve().parents[2] / "qa/missions/taxi/fixtures/build_submodules.py"


def test_companion_freezes_two_distinct_recursive_histories(tmp_path):
    root = tmp_path / "fixture"
    result = run_bounded_child([sys.executable, str(BUILDER), str(root)])
    assert result.returncode == 0, result.stderr
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["outer"]["A"] != manifest["outer"]["B"]
    assert manifest["data"]["A"] != manifest["data"]["B"]
    assert manifest["leaf"]["A"] != manifest["leaf"]["B"]
    data = root / "companion/deps/data"
    baseline = run_bounded_child(
        ["git", "-C", str(data), "show", manifest["data"]["A"] + ":rtl/data.sv"],
        timeout=10,
        check=True,
    )
    assert "16'h1357" in baseline.stdout
    assert "16'h2468" in (data / "rtl/data.sv").read_text()
    assert "qa_leaf" in (data / "rtl/data.sv").read_text()
    assert "8'h34" in (data / "nested/leaf/rtl/leaf.sv").read_text()


def test_failed_construction_can_be_retried_without_manual_cleanup(tmp_path, monkeypatch):
    import pytest
    from qa.missions.taxi.fixtures import build_submodules

    root = tmp_path / "fixture"
    with monkeypatch.context() as patch:
        patch.setenv("PATH", "")
        with pytest.raises(FileNotFoundError):
            build_submodules.construct(root)
    assert not root.exists()
    assert build_submodules.construct(root)["outer"]["B"]
