"""Tests for preflight checks."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from booley.fusesoc.fusesoc_registry import CoreSetupHazard
from booley.harness.preflight import (
    PreflightError,
    _check_core_setup_hazards,
    _check_git,
    _check_inside_container,
    run_preflight,
)

# ===========================================================================
# PreflightError
# ===========================================================================


class TestPreflightError:
    def test_format(self):
        err = PreflightError(["no .tickets/", "git not found"])
        assert "no .tickets/" in str(err)
        assert "git not found" in str(err)
        assert err.failures == ["no .tickets/", "git not found"]


# ===========================================================================
# run_preflight
# ===========================================================================


# The venue guard reads env + the real filesystem (/.dockerenv); no-op it so
# these run_preflight tests behave identically whether the suite runs on the
# host or in a container. Its own behavior is covered by
# TestCheckInsideContainer below.
@patch("booley.harness.preflight._check_inside_container", return_value=None)
class TestRunPreflight:
    @patch("booley.harness.preflight._check_ticket_board", return_value=[])
    @patch("booley.harness.preflight._check_git", return_value=[])
    def test_passes_when_all_ok(self, mock_git, mock_tb, _mock_guard, project_root: Path):
        """No failures -> no exception."""
        run_preflight(project_root)

    @patch("booley.harness.preflight._check_ticket_board", return_value=[])
    @patch("booley.harness.preflight._check_git", return_value=[])
    def test_fails_when_no_tickets_dir(self, mock_git, mock_tb, _mock_guard, tmp_path: Path):
        """Missing tickets dir -> PreflightError."""
        with pytest.raises(PreflightError, match="tickets directory"):
            run_preflight(tmp_path)

    @patch("booley.harness.preflight._check_ticket_board", return_value=[])
    @patch("booley.harness.preflight._check_git", return_value=["git not found on PATH"])
    def test_fails_on_git_error(self, mock_git, mock_tb, _mock_guard, project_root: Path):
        with pytest.raises(PreflightError, match="git not found"):
            run_preflight(project_root)

    @patch(
        "booley.harness.preflight._check_ticket_board",
        return_value=["ticket_board package not importable: ModuleNotFoundError"],
    )
    @patch("booley.harness.preflight._check_git", return_value=[])
    def test_fails_on_ticket_board_missing(
        self, mock_git, mock_tb, _mock_guard, project_root: Path
    ):
        with pytest.raises(PreflightError, match="ticket_board"):
            run_preflight(project_root)

    @patch("booley.harness.preflight._check_ticket_board", return_value=["tb error"])
    @patch("booley.harness.preflight._check_git", return_value=["git error"])
    def test_aggregates_all_failures(self, mock_git, mock_tb, _mock_guard, project_root: Path):
        """All failures collected in one PreflightError."""
        # Also missing .tickets/ -> 3 total
        with pytest.raises(PreflightError) as exc_info:
            run_preflight(project_root / "nonexistent")
        assert len(exc_info.value.failures) >= 2


# ===========================================================================
# _check_inside_container (Ticket Mode is container-only; ADR 0028)
# ===========================================================================


class TestCheckInsideContainer:
    def test_raises_on_host(self, monkeypatch):
        """No container markers -> refuse with the Reopen-in-Container fix."""
        monkeypatch.delenv("BOOLEY_CONTAINER", raising=False)
        with (
            patch.object(Path, "exists", lambda self: False),
            pytest.raises(PreflightError, match="Session Runtime"),
        ):
            _check_inside_container()

    def test_error_names_the_fix(self, monkeypatch):
        """The refusal must tell the user HOW to get inside the container."""
        monkeypatch.delenv("BOOLEY_CONTAINER", raising=False)
        with (
            patch.object(Path, "exists", lambda self: False),
            pytest.raises(PreflightError, match="Reopen in Container"),
        ):
            _check_inside_container()

    def test_passes_with_env_marker(self, monkeypatch):
        """BOOLEY_CONTAINER=1 (baked into the sandbox image) -> no exception."""
        monkeypatch.setenv("BOOLEY_CONTAINER", "1")
        _check_inside_container()

    def test_passes_when_dockerenv_present(self, monkeypatch):
        """/.dockerenv fallback (pre-env-var images) -> no exception."""
        monkeypatch.delenv("BOOLEY_CONTAINER", raising=False)

        def fake_exists(self):
            # as_posix(): plain str() renders "\\.dockerenv" on Windows and
            # the comparison silently never matches (F-16).
            return self.as_posix() == "/.dockerenv"

        with patch.object(Path, "exists", fake_exists):
            _check_inside_container()

    def test_passes_when_containerenv_present(self, monkeypatch):
        """/run/.containerenv (Podman/OCI) fallback -> no exception."""
        monkeypatch.delenv("BOOLEY_CONTAINER", raising=False)

        def fake_exists(self):
            return self.as_posix() == "/run/.containerenv"

        with patch.object(Path, "exists", fake_exists):
            _check_inside_container()


class TestCoreSetupHazards:
    def test_recursive_symlink_is_fatal(self, tmp_path: Path, monkeypatch):
        link = tmp_path / "lib" / "repo"
        monkeypatch.setattr(
            "booley.fusesoc.fusesoc_registry.core_setup_hazards",
            lambda _root: [CoreSetupHazard("recursive-symlink", link, "points to ancestor")],
        )
        errors = _check_core_setup_hazards(tmp_path)
        assert len(errors) == 1
        assert "FUSESOC_IGNORE" in errors[0]

    def test_selected_provider_core_is_fatal(self, tmp_path: Path, monkeypatch):
        core = tmp_path / "design.core"
        monkeypatch.setattr(
            "booley.fusesoc.fusesoc_registry.core_setup_hazards",
            lambda _root: [CoreSetupHazard("provider", core, "remote fetch")],
        )
        monkeypatch.setattr(
            "booley.harness.preflight._configured_core_files",
            lambda _root: {core},
        )
        errors = _check_core_setup_hazards(tmp_path)
        assert len(errors) == 1
        assert "remove provider:" in errors[0]

    def test_unselected_provider_core_warns_only(self, tmp_path: Path, monkeypatch, caplog):
        core = tmp_path / "vendored" / "design.core"
        monkeypatch.setattr(
            "booley.fusesoc.fusesoc_registry.core_setup_hazards",
            lambda _root: [CoreSetupHazard("provider", core, "remote fetch")],
        )
        monkeypatch.setattr(
            "booley.harness.preflight._configured_core_files",
            lambda _root: set(),
        )
        assert _check_core_setup_hazards(tmp_path) == []
        assert "no configured Flow selects it" in caplog.text

    def test_provider_core_in_configured_dependency_closure_is_fatal(self, tmp_path: Path):
        (tmp_path / "top.core").write_text(
            """\
CAPI=2:
name: acme:demo:top:0
filesets:
  rtl:
    depend: [acme:demo:dep]
targets:
  sim: {flow: sim, flow_options: {tool: verilator}, filesets: [rtl]}
""",
            encoding="utf-8",
        )
        dep = tmp_path / "dep.core"
        dep.write_text(
            """\
CAPI=2:
name: acme:demo:dep:0
provider: {name: github, user: acme, repo: dep}
targets:
  default: {}
""",
            encoding="utf-8",
        )
        state = tmp_path / ".booley_project"
        state.mkdir()
        (state / "booley.toml").write_text(
            '[flows.sim]\ndefault_target = "sim"\n', encoding="utf-8"
        )

        errors = _check_core_setup_hazards(tmp_path)
        assert len(errors) == 1
        assert dep.name in errors[0]


# ===========================================================================
# _check_git
# ===========================================================================


class TestCheckGit:
    @patch("booley.harness.preflight.subprocess.run")
    def test_not_in_worktree(self, mock_run):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=128,
            stdout="",
            stderr="not a git repo",
        )
        errors = _check_git(Path("/tmp"))
        assert any("work tree" in e.lower() for e in errors)

    @patch("booley.harness.preflight.subprocess.run")
    def test_git_not_found(self, mock_run):
        mock_run.side_effect = FileNotFoundError()
        errors = _check_git(Path("/tmp"))
        assert any("not found" in e.lower() for e in errors)

    @patch("booley.harness.preflight.subprocess.run")
    def test_git_timeout(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=10)
        errors = _check_git(Path("/tmp"))
        assert any("timed out" in e.lower() for e in errors)

    @patch("booley.harness.preflight.subprocess.run")
    def test_dirty_tree_warns_but_no_error(self, mock_run, tmp_path: Path, caplog):
        """Dirty working tree emits warning but does not block."""

        def side_effect(cmd, **kw):
            if "rev-parse" in cmd and "--is-inside-work-tree" in cmd:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="true")
            if "status" in cmd:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=0,
                    stdout=" M rtl/foo.sv\n M tb/bar.sv\n",
                    stderr="",
                )
            if "rev-parse" in cmd and "--git-dir" in cmd:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=".git")
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="")

        mock_run.side_effect = side_effect
        import logging

        with caplog.at_level(logging.WARNING, logger="harness.preflight"):
            errors = _check_git(tmp_path)
        assert errors == []
        assert any("dirty" in r.message.lower() for r in caplog.records)

    @patch("booley.harness.preflight.subprocess.run")
    def test_merge_in_progress_detected(self, mock_run, tmp_path: Path):
        """MERGE_HEAD present -> error."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        (git_dir / "MERGE_HEAD").write_text("abc123", encoding="utf-8")

        def side_effect(cmd, **kw):
            if "--is-inside-work-tree" in cmd:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="true")
            if "status" in cmd:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
            if "--git-dir" in cmd:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=".git")
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="")

        mock_run.side_effect = side_effect
        errors = _check_git(tmp_path)
        assert any("merge" in e.lower() for e in errors)

    @patch("booley.harness.preflight.subprocess.run")
    def test_clean_repo_no_errors(self, mock_run, tmp_path: Path):
        """Clean repo -> empty error list."""

        def side_effect(cmd, **kw):
            if "--is-inside-work-tree" in cmd:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="true")
            if "status" in cmd:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
            if "--git-dir" in cmd:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=".git")
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="")

        mock_run.side_effect = side_effect
        errors = _check_git(tmp_path)
        assert errors == []
