"""Doctor reports a ``[goals]`` section as a FAIL with the fix ``booley-heal`` applies."""

from __future__ import annotations

import argparse

from booley.harness import doctor
from booley.presentation import colors
from tests.harness.doctor_environment_support import patch_doctor_environment
from tests.harness.test_doctor import _write_project


def test_doctor_fails_with_the_fix_for_a_goals_section(tmp_path, monkeypatch, capsys):
    booley_dir = _write_project(tmp_path)
    patch_doctor_environment(monkeypatch, tmp_path, booley_dir)
    toml = booley_dir / "booley.toml"
    toml.write_bytes(toml.read_bytes() + b"\n[goals]\nquiet_after = 60\n")
    lines = toml.read_bytes().decode().splitlines()
    first = lines.index("[goals]") + 1

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    output = " ".join(colors.strip_ansi(capsys.readouterr().out).split())
    assert rc != 0
    assert f"booley.toml has a [goals] section (line {first}); Goal Mode was removed" in output
    assert f"delete the [goals] tables (lines {first}-{first + 1}) from" in output
    assert "Project checks" in output  # Doctor completes instead of crashing on the section
