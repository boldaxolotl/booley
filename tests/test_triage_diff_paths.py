"""Regression contract for readable ticket-triage diff paths."""

from pathlib import Path

from booley.review.triage_package import _write_diff_pair


def test_triage_diff_paths_preserve_repository_filenames(tmp_path, monkeypatch):
    ctx = type("Context", (), {"base_sha": "base", "head_sha": "head"})()
    monkeypatch.setattr(
        "booley.review.triage_package._revision_content",
        lambda _ctx, _revision, path: path.encode(),
    )

    change = {"status": "R100", "old_path": "rtl/old/core.sv", "path": "rtl/new/core.sv"}
    result = _write_diff_pair(ctx, tmp_path, 1, change)

    left = Path(result["diff_left"])
    right = Path(result["diff_right"])
    assert left.relative_to(tmp_path).as_posix() == "001/base/rtl/old/core.sv"
    assert right.relative_to(tmp_path).as_posix() == "001/head/rtl/new/core.sv"
    assert left.read_bytes() == b"rtl/old/core.sv"
    assert right.read_bytes() == b"rtl/new/core.sv"
