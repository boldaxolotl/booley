"""Doctor audits the booley.toml the retired-key refusal reads: the Main Booley Directory's."""

from __future__ import annotations

import argparse

from booley.harness import doctor
from booley.harness.setup import readiness
from booley.presentation import colors
from tests.harness.test_doctor import _patch_environment, _write_project


def test_a_linked_worktrees_doctor_reports_the_main_copys_keys(tmp_path, monkeypatch, capsys):
    """The worktree's own copy is clean; the main copy's key is what every command refuses on."""
    booley_dir = _write_project(tmp_path)
    _patch_environment(monkeypatch, tmp_path, booley_dir)
    main_dir = tmp_path / "main-booley"
    main_dir.mkdir()
    (main_dir / "booley.toml").write_bytes(b"[jobs]\nmax_heavy = 2\n")
    monkeypatch.setattr(readiness, "resolve_common_booley_dir", lambda _start: main_dir)

    rc = doctor.run_doctor(argparse.Namespace(verbose=False, deep=False), tmp_path)

    output = colors.strip_ansi(capsys.readouterr().out)
    assert rc != 0
    assert "retired [jobs] key max_heavy (line 2)" in output
    assert str(main_dir / "booley.toml") in output
