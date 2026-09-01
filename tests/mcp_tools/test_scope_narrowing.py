"""Tests for scope narrowing in TbCoderSpecialist (Source Isolation).

Covers the narrowing MECHANICS that ``test_tb_coder.py`` does not already
cover:

- ``_category_dir_prefixes(work_dir)`` / ``_category_globs(work_dir)`` output
  (the 1-arg, testbench-only API).
- ``_narrowed_scope_file`` replace / restore / restore-on-exception / no-op.
- A ``tb_coder`` ``_run`` narrows ``.scope.json`` to the tb globs and restores
  the original on both success and exception.

Scope narrowing is directory-categorical. The RTL/TB file partition is derived
from FuseSoC ``tags:[tb]`` at resolve time, so the surviving code-modifying
specialist (``TbCoderSpecialist``, tb-permanent) narrows to its own category's
testbench source-directory globs rather than an explicit per-half file list.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from booley.criteria.state import DevelopmentState
from booley.mcp.base import EXIT_SUCCESS, McpToolResult
from booley.specialists.tb_coder import (
    TbCoderSpecialist,
    _category_dir_prefixes,
    _category_globs,
    _narrowed_scope_file,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def work_dir(tmp_path: Path) -> Path:
    """Worktree-like dir with rtl/, tb/, and a .scope.json + booley.toml."""
    wd = tmp_path / "wt"
    (wd / "rtl").mkdir(parents=True)
    (wd / "rtl" / "fifo.sv").write_text("// fifo\n", encoding="utf-8")
    (wd / "rtl" / "fifo_pkg.sv").write_text("// pkg\n", encoding="utf-8")
    (wd / "tb").mkdir(parents=True)
    (wd / "tb" / "tb_fifo.sv").write_text("// tb_fifo\n", encoding="utf-8")
    (wd / ".booley_project").mkdir(parents=True)
    (wd / ".booley_project" / "booley.toml").write_text(
        '[sources.rtl]\nsource_dirs = ["rtl"]\n\n[sources.testbench]\nsource_dirs = ["tb"]\n',
        encoding="utf-8",
    )
    # Style guides expected by the prompt builder
    refs = wd / ".booley" / "docs" / "refs"
    refs.mkdir(parents=True)
    (refs / "rtl_style_guide.md").write_text("# RTL\n", encoding="utf-8")
    (refs / "tb_style_guide.md").write_text("# TB\n", encoding="utf-8")
    (refs / "unit_tb_style_guide.md").write_text("# Unit TB\n", encoding="utf-8")
    # Initial broad ticket scope
    (wd / ".scope.json").write_text(
        json.dumps({"scope": ["rtl/*.sv", "tb/*.sv"]}, indent=2) + "\n",
        encoding="utf-8",
    )
    return wd


@pytest.fixture()
def instruction_file(tmp_path: Path) -> Path:
    f = tmp_path / "inst.md"
    f.write_text("# do work\n", encoding="utf-8")
    return f


@pytest.fixture()
def ticket_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Ordinary ticket state file."""
    sf = tmp_path / "state.json"
    st = DevelopmentState.load(sf)
    st.slug = "scope-narrow-test"
    st.init_criteria({"sim_pass_default": True})
    st.save()
    monkeypatch.setenv("BOOLEY_SLUG", "scope-narrow-test")
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(sf))
    return sf


@pytest.fixture()
def empty_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Minimal state file with no criteria evidence."""
    sf = tmp_path / "state-empty.json"
    st = DevelopmentState.load(sf)
    st.slug = "legacy-ticket"
    st.init_criteria({"sim_pass_default": True})
    st.save()
    monkeypatch.setenv("BOOLEY_SLUG", "legacy-ticket")
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(sf))
    return sf


def _make_tb_coder(
    instruction_file: Path,
    work_dir: Path,
    *,
    scope: str = "tb/*.sv",
) -> TbCoderSpecialist:
    """Build and parse args for a TbCoderSpecialist (category is permanently tb)."""
    endpoint = TbCoderSpecialist()
    endpoint.parse_args(
        [
            "--work-dir",
            str(work_dir),
            "--instruction-file",
            str(instruction_file),
            "--scope",
            scope,
        ]
    )
    return endpoint


# ---------------------------------------------------------------------------
# Pure helpers — _category_dir_prefixes / _category_globs (1-arg, tb-only)
# ---------------------------------------------------------------------------


class TestCategoryDirPrefixes:
    def test_uses_configured_tb_dir(self, work_dir):
        prefixes = _category_dir_prefixes(work_dir)
        assert "tb" in prefixes

    def test_does_not_include_rtl_dirs(self, work_dir):
        # tb_coder is tb-permanent — narrowing never reaches the rtl dirs.
        prefixes = _category_dir_prefixes(work_dir)
        assert "rtl" not in prefixes


class TestCategoryGlobs:
    def test_emits_sv_svh_v_for_tb(self, work_dir):
        globs = _category_globs(work_dir)
        for sfx in ("sv", "svh", "v"):
            assert f"tb/*.{sfx}" in globs

    def test_omits_rtl_globs(self, work_dir):
        globs = _category_globs(work_dir)
        for sfx in ("sv", "svh", "v"):
            assert f"rtl/*.{sfx}" not in globs


# ---------------------------------------------------------------------------
# _narrowed_scope_file context manager
# ---------------------------------------------------------------------------


class TestNarrowedScopeFile:
    def test_replaces_and_restores(self, work_dir):
        original = (work_dir / ".scope.json").read_text(encoding="utf-8")
        with _narrowed_scope_file(work_dir, ["tb/tb_fifo.sv", "tb/*.sv"]):
            inside = json.loads((work_dir / ".scope.json").read_text(encoding="utf-8"))
            assert inside == {"scope": ["tb/tb_fifo.sv", "tb/*.sv"]}
        # Restored verbatim
        assert (work_dir / ".scope.json").read_text(encoding="utf-8") == original

    def test_restores_on_exception(self, work_dir):
        original = (work_dir / ".scope.json").read_text(encoding="utf-8")
        with pytest.raises(RuntimeError), _narrowed_scope_file(work_dir, ["tb/only.sv"]):
            raise RuntimeError("boom")
        assert (work_dir / ".scope.json").read_text(encoding="utf-8") == original

    def test_noop_when_scope_file_missing(self, tmp_path):
        # No .scope.json → no narrowing, no error.
        with _narrowed_scope_file(tmp_path, ["tb/anything.sv"]):
            assert not (tmp_path / ".scope.json").exists()
        assert not (tmp_path / ".scope.json").exists()

    def test_noop_when_narrowed_empty(self, work_dir):
        # Empty narrowed list → broader ticket scope stays in effect.
        original = (work_dir / ".scope.json").read_text(encoding="utf-8")
        with _narrowed_scope_file(work_dir, []):
            assert (work_dir / ".scope.json").read_text(encoding="utf-8") == original
        assert (work_dir / ".scope.json").read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# TbCoderSpecialist._build_narrowed_scope — directory-categorical (tb globs)
# ---------------------------------------------------------------------------


class TestBuildNarrowedScope:
    def test_returns_tb_category_globs(self, ticket_state, instruction_file, work_dir):
        endpoint = _make_tb_coder(instruction_file, work_dir)
        endpoint.read_state()
        narrowed = endpoint._build_narrowed_scope()
        assert narrowed == _category_globs(work_dir)
        assert "tb/*.sv" in narrowed
        # Never the rtl globs — tb_coder is tb-permanent.
        assert "rtl/*.sv" not in narrowed


# ---------------------------------------------------------------------------
# TbCoderSpecialist._run scope narrowing (integration)
# ---------------------------------------------------------------------------


class TestTbCoderRunNarrowsScope:
    def test_run_narrows_to_tb_globs_then_restores(
        self,
        ticket_state,
        instruction_file,
        work_dir,
    ):
        endpoint = _make_tb_coder(instruction_file, work_dir, scope="tb/*.sv")
        endpoint.read_state()

        observed: dict[str, object] = {}

        def _capture(_self):
            data = json.loads((work_dir / ".scope.json").read_text(encoding="utf-8"))
            observed["scope_during_run"] = data["scope"]
            return McpToolResult(exit_code=EXIT_SUCCESS)

        with patch("booley.specialists.tb_coder.Specialist._run", _capture):
            endpoint._run()

        scope = observed["scope_during_run"]
        # Narrowed to the tb category source-dir globs.
        assert "tb/*.sv" in scope
        # RTL globs are NOT in the narrowed scope for the tb coder.
        assert "rtl/*.sv" not in scope
        # Restored after run.
        restored = json.loads((work_dir / ".scope.json").read_text(encoding="utf-8"))
        assert restored == {"scope": ["rtl/*.sv", "tb/*.sv"]}

    def test_restores_scope_on_exception(
        self,
        ticket_state,
        instruction_file,
        work_dir,
    ):
        endpoint = _make_tb_coder(instruction_file, work_dir, scope="tb/*.sv")
        endpoint.read_state()
        original = (work_dir / ".scope.json").read_text(encoding="utf-8")

        def _boom(_self):
            raise RuntimeError("agent crashed")

        with (
            patch("booley.specialists.tb_coder.Specialist._run", _boom),
            pytest.raises(RuntimeError),
        ):
            endpoint._run()

        # Scope file restored even though the agent invocation blew up.
        assert (work_dir / ".scope.json").read_text(encoding="utf-8") == original

    def test_narrows_to_tb_dirs_with_minimal_state(
        self,
        empty_state,
        instruction_file,
        work_dir,
    ):
        """Minimal state still narrows to the configured TB directory globs."""
        endpoint = _make_tb_coder(instruction_file, work_dir, scope="tb/*.sv")
        endpoint.read_state()
        original = (work_dir / ".scope.json").read_text(encoding="utf-8")

        observed: dict[str, object] = {}

        def _capture(_self):
            data = json.loads((work_dir / ".scope.json").read_text(encoding="utf-8"))
            observed["scope_during_run"] = data["scope"]
            return McpToolResult(exit_code=EXIT_SUCCESS)

        with patch("booley.specialists.tb_coder.Specialist._run", _capture):
            endpoint._run()

        scope = observed["scope_during_run"]
        assert "tb/*.sv" in scope
        assert "rtl/*.sv" not in scope
        # Restored after run.
        assert (work_dir / ".scope.json").read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# Non-tb_coder Specialists are not narrowed
# ---------------------------------------------------------------------------


class TestOtherSpecialistsNotNarrowed:
    """Only the code-modifying ``TbCoderSpecialist`` defines ``_build_narrowed_scope``.

    A code-shape check rather than running each endpoint's full _run path, since
    most need an LLM agent.  The surviving specialists below must not carry the
    narrowing hook.
    """

    def test_reviewer_has_no_narrowing(self):
        from booley.specialists.reviewer import ReviewerSpecialist

        assert not hasattr(ReviewerSpecialist, "_build_narrowed_scope")

    def test_mutation_tester_has_no_narrowing(self):
        from booley.specialists.mutation_tester import MutationTesterSpecialist

        assert not hasattr(MutationTesterSpecialist, "_build_narrowed_scope")

    def test_coverage_analyst_has_no_narrowing(self):
        from booley.specialists.coverage_analyst import CoverageAnalystSpecialist

        assert not hasattr(CoverageAnalystSpecialist, "_build_narrowed_scope")

    def test_tb_coder_does_have_narrowing(self):
        assert hasattr(TbCoderSpecialist, "_build_narrowed_scope")
