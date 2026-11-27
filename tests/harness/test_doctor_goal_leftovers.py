"""Doctor warns about Goal Mode directories left behind by an earlier version and keeps them."""

from __future__ import annotations

import argparse
from pathlib import Path

from booley.harness import doctor
from booley.presentation import colors
from tests.harness.doctor_environment_support import patch_doctor_environment
from tests.harness.test_doctor import _write_project


def _tree(root: Path) -> dict[str, bytes | None]:
    """Every path under *root* with its bytes (None for a directory)."""
    return {
        str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
        for path in sorted(root.rglob("*"))
    }


def test_doctor_warns_about_leftover_goal_directories_and_keeps_them(
    tmp_path, monkeypatch, capsys
):
    booley_dir = _write_project(tmp_path)
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    main_dir = tmp_path / "main-booley"
    (main_dir / "goals" / "g1").mkdir(parents=True)
    (main_dir / "goals" / "g1" / "record.json").write_bytes(b'{"goal": "old"}\n')
    (booley_dir / "goalsets").mkdir()
    (booley_dir / "goalsets" / "default.md").write_bytes(b"# old goalset\n")
    monkeypatch.setattr(doctor, "resolve_common_booley_dir", lambda _root: main_dir)
    before = (_tree(main_dir / "goals"), _tree(booley_dir / "goalsets"))

    doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    output = " ".join(colors.strip_ansi(capsys.readouterr().out).split())
    for leftover in (main_dir / "goals", booley_dir / "goalsets"):
        assert f"{leftover} is left over from Goal Mode, which was removed" in output
    assert output.count("is left over from Goal Mode") == 2
    assert (_tree(main_dir / "goals"), _tree(booley_dir / "goalsets")) == before
