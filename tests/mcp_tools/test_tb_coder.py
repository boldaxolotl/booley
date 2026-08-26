"""Tests for TbCoderSpecialist — prompt, scope validation, interpret output, criteria.

TbCoderSpecialist is the TB-only successor to the old CoderTool: the category is
permanently "tb", there is no ``--category`` argument, no rtl mode, no planner
gating, and ``required_dut_info_halves()`` returns ``frozenset()``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from booley.dev_support.development_state import (
    DevelopmentState,
)
from booley.mcp.base import EXIT_ERROR, EXIT_FAILURE, EXIT_SUCCESS, McpToolResult
from booley.specialists.specialist import _git_head_sha, _read_commit_info
from booley.specialists.tb_coder import (
    TbCoderSpecialist,
    _resolve_scope_files,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_toml(
    work_dir: Path,
    *,
    rtl_dirs: list[str] | None = None,
    tb_dirs: list[str] | None = None,
) -> Path:
    """Write a minimal booley.toml under work_dir/.booley_project/."""
    rtl = rtl_dirs or ["rtl"]
    tb = tb_dirs or ["tb"]
    toml_dir = work_dir / ".booley_project"
    toml_dir.mkdir(parents=True, exist_ok=True)
    toml_path = toml_dir / "booley.toml"
    rtl_list = ", ".join(f'"{d}"' for d in rtl)
    tb_list = ", ".join(f'"{d}"' for d in tb)
    content = (
        f"[sources.rtl]\nsource_dirs = [{rtl_list}]\n\n"
        f"[sources.testbench]\nsource_dirs = [{tb_list}]\n"
    )
    toml_path.write_text(content, encoding="utf-8")
    return toml_path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def state_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a minimal state file with typical criteria."""
    sf = tmp_path / "state.json"
    st = DevelopmentState.load(sf)
    st.slug = "test-impl"
    st.init_criteria(
        {
            "lint_clean_lite": True,
            "sim_pass_lite": True,
            "review_rtl_bugs_done": True,
            "review_tb_quality_done": True,
            "synthesis_ok_lite": True,
            "coverage_toggle": True,
            "coverage_branch": True,
        }
    )
    # Mark some as met so we can test invalidation
    st.set_criterion("lint_clean_lite", True)
    st.set_criterion("sim_pass_lite", True)
    st.set_criterion("review_rtl_bugs_done", True)
    st.set_criterion("review_tb_quality_done", True)
    st.set_criterion("synthesis_ok_lite", True)
    st.set_criterion("coverage_toggle", True)
    st.set_criterion("coverage_branch", True)
    st.save()
    monkeypatch.setenv("BOOLEY_SLUG", "test-impl")
    monkeypatch.setenv("BOOLEY_STATE_FILE", str(sf))
    return sf


@pytest.fixture()
def instruction_file(tmp_path: Path) -> Path:
    """Create a sample instruction markdown file."""
    f = tmp_path / "instructions.md"
    f.write_text(
        "# Add directed checks for bank_sel mux\n\n"
        "Extend the bank selection testbench in mod_a_tb.sv with parameterized checks.\n",
        encoding="utf-8",
    )
    return f


@pytest.fixture()
def work_dir(tmp_path: Path) -> Path:
    """Create a fake work directory with scope-matching files."""
    rtl = tmp_path / "work" / "rtl"
    rtl.mkdir(parents=True)
    (rtl / "mod_a.sv").write_text("// mod_a stub\n", encoding="utf-8")
    (rtl / "mod_b.sv").write_text("// mod_b stub\n", encoding="utf-8")
    tb = tmp_path / "work" / "tb"
    tb.mkdir(parents=True)
    (tb / "mod_a_tb.sv").write_text("// tb stub\n", encoding="utf-8")
    (tb / "mod_b_tb.sv").write_text("// tb stub\n", encoding="utf-8")
    refs = tmp_path / "work" / ".booley" / "docs" / "refs"
    refs.mkdir(parents=True)
    (refs / "rtl_style_guide.md").write_text("# RTL Style Guide\n", encoding="utf-8")
    (refs / "tb_style_guide.md").write_text("# TB Style Guide\n", encoding="utf-8")
    (refs / "unit_tb_style_guide.md").write_text("# Unit TB Style Guide\n", encoding="utf-8")
    return tmp_path / "work"


def _make_endpoint(
    state_file: Path,
    instruction_file: Path,
    work_dir: Path,
    *,
    scope: str = "tb/*.sv",
    steer: str = "",
    extra_args: list[str] | None = None,
) -> TbCoderSpecialist:
    """Build and parse args for a TbCoderSpecialist instance.

    Expects BOOLEY_SLUG / BOOLEY_STATE_FILE to be set (via state_file fixture).
    The category is permanently "tb"; there is no --category argument.
    """
    endpoint = TbCoderSpecialist()
    argv = [
        "--work-dir",
        str(work_dir),
        "--instruction-file",
        str(instruction_file),
        "--scope",
        scope,
    ]
    if steer:
        argv.extend(["--steer", steer])
    if extra_args:
        argv.extend(extra_args)
    endpoint.parse_args(argv)
    return endpoint


# ---------------------------------------------------------------------------
# Class attributes
# ---------------------------------------------------------------------------


class TestClassAttributes:
    def test_name(self):
        assert TbCoderSpecialist.name == "tb_coder"

    def test_code_modifying(self):
        assert TbCoderSpecialist.code_modifying is True

    def test_default_timeout(self):
        assert TbCoderSpecialist.default_timeout == 1800

    def test_description_is_set(self):
        assert TbCoderSpecialist.description

    def test_display_tag_is_tb(self, state_file, instruction_file, work_dir):
        endpoint = _make_endpoint(state_file, instruction_file, work_dir)
        assert endpoint.display_tag == "tb"


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------


class TestArgparse:
    def test_required_args(self, state_file, instruction_file, work_dir):
        endpoint = _make_endpoint(state_file, instruction_file, work_dir)
        assert endpoint.args.instruction_file == instruction_file
        assert endpoint.args.scope == "tb/*.sv"

    def test_no_category_arg(self, state_file, instruction_file, work_dir):
        """The --category argument was removed; category is fixed to tb."""
        endpoint = TbCoderSpecialist()
        with pytest.raises(SystemExit):
            endpoint.parse_args(
                [
                    "--work-dir",
                    str(work_dir),
                    "--instruction-file",
                    str(instruction_file),
                    "--scope",
                    "tb/*.sv",
                    "--category",
                    "tb",  # no longer accepted
                ]
            )

    def test_steer_arg(self, state_file, instruction_file, work_dir):
        endpoint = _make_endpoint(
            state_file,
            instruction_file,
            work_dir,
            steer="Focus on the mux checks in mod_a_tb.sv",
        )
        assert endpoint.args.steer == "Focus on the mux checks in mod_a_tb.sv"

    def test_steer_default_empty(self, state_file, instruction_file, work_dir):
        endpoint = _make_endpoint(state_file, instruction_file, work_dir)
        assert endpoint.args.steer == ""

    def test_missing_instruction_file_exits(self, state_file, work_dir):
        endpoint = TbCoderSpecialist()
        with pytest.raises(SystemExit):
            endpoint.parse_args(
                [
                    "--scope",
                    "tb/*.sv",
                    # missing --instruction-file
                ]
            )

    def test_missing_scope_exits(self, state_file, instruction_file, work_dir):
        endpoint = TbCoderSpecialist()
        with pytest.raises(SystemExit):
            endpoint.parse_args(
                [
                    "--instruction-file",
                    str(instruction_file),
                    # missing --scope
                ]
            )

    def test_inherits_agent_args(self, state_file, instruction_file, work_dir):
        endpoint = _make_endpoint(
            state_file,
            instruction_file,
            work_dir,
            extra_args=["--model", "light", "--instruction", "be careful"],
        )
        assert endpoint.args.model == "light"
        assert endpoint.args.instruction == "be careful"


# ---------------------------------------------------------------------------
# Instruction file reading
# ---------------------------------------------------------------------------


class TestInstructionFile:
    def test_existing_file_ok(self, state_file, instruction_file, work_dir):
        """_run should not error on a valid instruction file."""
        endpoint = _make_endpoint(state_file, instruction_file, work_dir)
        endpoint.read_state()
        # Mock the agent call to avoid real invocation
        with patch("booley.specialists.tb_coder.Specialist._run") as mock_super:
            mock_super.return_value = McpToolResult(exit_code=EXIT_SUCCESS)
            result = endpoint._run()
        assert result.exit_code != EXIT_ERROR

    def test_missing_file_returns_error(self, state_file, work_dir, tmp_path):
        """_run should return exit 2 when instruction file doesn't exist."""
        missing = tmp_path / "nonexistent.md"
        endpoint = _make_endpoint(state_file, missing, work_dir)
        endpoint.read_state()
        result = endpoint._run()
        assert result.exit_code == EXIT_ERROR
        assert "not found" in result.report_text

    def test_directory_returns_error(self, state_file, work_dir, tmp_path):
        """_run should return exit 2 when --instruction-file is a directory."""
        d = tmp_path / "instructions_dir"
        d.mkdir()
        endpoint = _make_endpoint(state_file, d, work_dir)
        endpoint.read_state()
        result = endpoint._run()
        assert result.exit_code == EXIT_ERROR
        assert "must be a file" in result.report_text

    def test_no_identity_gate(self, state_file, instruction_file, work_dir):
        endpoint = _make_endpoint(state_file, instruction_file, work_dir)
        endpoint.read_state()
        assert not hasattr(endpoint, "required_dut_info_halves")


# ---------------------------------------------------------------------------
# Scope validation
# ---------------------------------------------------------------------------


class TestScopeValidation:
    """Integration tests for scope validation in TbCoderSpecialist._run().

    Pure-function tests for validate_scope_category() live in
    test_workspace_isolation.py.
    """

    def test_scope_mismatch_blocks_run(self, state_file, instruction_file, work_dir):
        """_run returns exit 2 when scope files are RTL (not tb category)."""
        endpoint = _make_endpoint(
            state_file,
            instruction_file,
            work_dir,
            scope="rtl/*.sv",  # mismatch — RTL files for a TB-only endpoint
        )
        endpoint.read_state()
        result = endpoint._run()
        assert result.exit_code == EXIT_ERROR

    def test_empty_scope_returns_error(self, state_file, instruction_file, work_dir):
        """Empty scope (no matching files) returns EXIT_ERROR."""
        endpoint = _make_endpoint(
            state_file,
            instruction_file,
            work_dir,
            scope="nonexistent_dir/*.sv",
        )
        endpoint.read_state()
        result = endpoint._run()
        assert result.exit_code == EXIT_ERROR
        assert "No files matched" in result.report_text

    def test_tb_empty_glob_stays_empty(
        self,
        state_file,
        instruction_file,
        work_dir,
    ):
        """Empty TB globs still return empty scope for normal error handling."""
        endpoint = _make_endpoint(
            state_file,
            instruction_file,
            work_dir,
            scope="verif/*.sv",
        )
        endpoint.read_state()

        assert endpoint._resolve_effective_scope_files() == []


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


class TestBuildPrompt:
    def test_prompt_includes_instruction_content(
        self,
        state_file,
        instruction_file,
        work_dir,
    ):
        # Default (planning on): instruction content is the requirements block.
        endpoint = _make_endpoint(state_file, instruction_file, work_dir)
        prompt = endpoint._build_prompt()
        assert "bank_sel" in prompt
        assert "parameterized checks" in prompt
        assert "## Begin Verification Requirements" in prompt
        assert "## End Verification Requirements" in prompt

    def test_planning_on_by_default(
        self,
        state_file,
        instruction_file,
        work_dir,
    ):
        endpoint = _make_endpoint(state_file, instruction_file, work_dir)
        prompt = endpoint._build_prompt()

        # Planning preamble + reused plan-structure guidance present.
        assert "Verification Planning (do this FIRST)" in prompt
        assert "Existing TB Assessment" in prompt
        assert "Stimulus Plan" in prompt
        # Agent is told to FIRST plan, then implement, and to emit the artifact.
        assert "FIRST produce a verification plan" in prompt
        assert "verification_plan.md" in prompt
        # RTL stays hidden — plan independently from the spec.
        assert "RTL implementation is hidden" in prompt

    def test_skip_plan_implements_directly(
        self,
        state_file,
        instruction_file,
        work_dir,
    ):
        endpoint = _make_endpoint(
            state_file, instruction_file, work_dir, extra_args=["--skip-plan"]
        )
        prompt = endpoint._build_prompt()

        # No planning preamble; legacy implement-only mandate + label.
        assert "Verification Planning (do this FIRST)" not in prompt
        assert "verification_plan.md" not in prompt
        assert (
            "Your task is to implement the verification/testbench plan below in "
            "SystemVerilog." in prompt
        )
        assert "## Begin Verification/Testbench Implementation Plan" in prompt
        assert "Do not edit or inspect RTL files" in prompt
        assert "context only" in prompt

    def test_plan_endpoint_narration_is_stripped(
        self,
        state_file,
        instruction_file,
        work_dir,
    ):
        instruction_file.write_text(
            "---\n"
            "source: plan agent capability\n"
            "dut_info:\n"
            "  dut_top_module: mod_a\n"
            "---\n\n"
            "I'll inspect the repo before revising the plan.\n\n"
            "The arbiter has resolved a decision.\n\n"
            "## Verification Steps\n"
            "1. Add directed checks in tb/mod_a_tb.sv.\n",
            encoding="utf-8",
        )
        endpoint = _make_endpoint(state_file, instruction_file, work_dir)
        prompt = endpoint._build_prompt()

        assert "source: plan agent capability" in prompt
        assert "dut_top_module: mod_a" in prompt
        assert "Add directed checks" in prompt
        assert "I'll inspect the repo" not in prompt
        assert "The arbiter has resolved a decision" not in prompt

    def test_tb_prompt_strips_rtl_implementation_steps(
        self,
        state_file,
        instruction_file,
        work_dir,
    ):
        instruction_file.write_text(
            "---\n"
            "source: plan agent capability\n"
            "---\n\n"
            "## RTL Implementation Steps\n"
            "1. Implement rtl/mod_a.sv.\n\n"
            "## Verification Steps\n"
            "1. Create directed checks in tb/mod_a_tb.sv.\n",
            encoding="utf-8",
        )

        endpoint = _make_endpoint(state_file, instruction_file, work_dir)
        prompt = endpoint._build_prompt()

        assert "Verification Steps" in prompt
        assert "Create directed checks" in prompt
        assert "## Begin Verification Requirements" in prompt
        assert "## End Verification Requirements" in prompt
        assert "RTL Implementation Steps" not in prompt
        assert "Implement rtl/mod_a.sv" not in prompt

    def test_plan_endpoint_arbiter_provenance_is_stripped(
        self,
        state_file,
        instruction_file,
        work_dir,
    ):
        instruction_file.write_text(
            "---\n"
            "source: plan agent capability\n"
            "---\n\n"
            "## Analysis\n"
            "Use pairwise interpolation. This follows spec arbiter decision D2, "
            "whose ruling is `use pairwise interpolation`.\n"
            "Per spec arbiter decision D3, divide signed sums by 2.\n\n"
            "## Verification Steps\n"
            "1. Iterate over pairs as required by arbiter decision D2.\n"
            "2. Divide by two, aligning with arbiter decision D3.\n",
            encoding="utf-8",
        )

        endpoint = _make_endpoint(state_file, instruction_file, work_dir)
        prompt = endpoint._build_prompt()

        assert "Use pairwise interpolation." in prompt
        assert "divide signed sums by 2" in prompt
        assert "Iterate over pairs." in prompt
        assert "Divide by two." in prompt
        assert "arbiter decision" not in prompt
        assert "spec arbiter" not in prompt

    def test_spec_decisions_are_not_injected(
        self,
        state_file,
        instruction_file,
        work_dir,
        monkeypatch,
    ):
        logs_dir = work_dir / "ticket-logs"
        logs_dir.mkdir()
        (logs_dir / "spec_decisions.json").write_text(
            '{"decisions": [{"id": "D1", "ruling": "keep plan"}]}',
            encoding="utf-8",
        )
        monkeypatch.setenv("BOOLEY_LOGS_DIR", str(logs_dir))

        endpoint = _make_endpoint(state_file, instruction_file, work_dir)
        prompt = endpoint._build_prompt()

        assert "Spec Decisions" not in prompt
        assert "spec_decisions.json" not in prompt
        assert "spec_arbiter" not in prompt

    def test_prompt_includes_scope(
        self,
        state_file,
        instruction_file,
        work_dir,
    ):
        endpoint = _make_endpoint(state_file, instruction_file, work_dir, scope="tb/mod_a_tb.sv")
        prompt = endpoint._build_prompt()
        assert "tb/mod_a_tb.sv" in prompt

    def test_prompt_includes_category(
        self,
        state_file,
        instruction_file,
        work_dir,
    ):
        endpoint = _make_endpoint(state_file, instruction_file, work_dir)
        prompt = endpoint._build_prompt()
        assert "tb" in prompt.lower()

    def test_tb_category_guard(
        self,
        state_file,
        instruction_file,
        work_dir,
    ):
        endpoint = _make_endpoint(state_file, instruction_file, work_dir, scope="tb/*.sv")
        prompt = endpoint._build_prompt()
        assert "Do NOT read, create, modify, or write any RTL files" in prompt
        assert "inaccessible" in prompt
        assert "tb_style_guide.md" in prompt
        assert "Only read project-specific extension files if they are listed above" in prompt

    def test_missing_absolute_style_guide_is_not_listed(
        self,
        state_file,
        instruction_file,
        work_dir,
        tmp_path,
    ):
        missing = tmp_path / "missing_style_guide.md"
        endpoint = _make_endpoint(state_file, instruction_file, work_dir)

        with patch(
            "booley.specialists.tb_coder._category_guides", return_value={"tb": [str(missing)]}
        ):
            prompt = endpoint._build_prompt()

        assert str(missing) not in prompt
        assert "Style Guides" not in prompt

    def test_no_git_restriction_in_prompt(
        self,
        state_file,
        instruction_file,
        work_dir,
    ):
        endpoint = _make_endpoint(state_file, instruction_file, work_dir)
        prompt = endpoint._build_prompt()
        assert "Do NOT run git commands" in prompt
        assert "commits are handled automatically" in prompt

    def test_no_compiler_restriction(
        self,
        state_file,
        instruction_file,
        work_dir,
    ):
        """Full sim + lint are forbidden, but elaborate is now allowed."""
        endpoint = _make_endpoint(state_file, instruction_file, work_dir)
        prompt = endpoint._build_prompt()
        assert "Do NOT run a full simulator or linter" in prompt
        # elaborate is the one allowed pre-submit check
        assert "elab" in prompt
        assert "testbench/verification changes compile and elaborate cleanly" in prompt

    def test_escalation_protocol(
        self,
        state_file,
        instruction_file,
        work_dir,
    ):
        endpoint = _make_endpoint(state_file, instruction_file, work_dir)
        prompt = endpoint._build_prompt()
        assert "[ESCALATION]" in prompt

    def test_steer_injection(
        self,
        state_file,
        instruction_file,
        work_dir,
    ):
        steer_text = "The mux was recently refactored, use the new wire names."
        endpoint = _make_endpoint(
            state_file,
            instruction_file,
            work_dir,
            steer=steer_text,
        )
        prompt = endpoint._build_prompt()
        assert "Developer Agent Context" in prompt
        assert steer_text in prompt

    def test_no_steer_no_section(
        self,
        state_file,
        instruction_file,
        work_dir,
    ):
        endpoint = _make_endpoint(state_file, instruction_file, work_dir)
        prompt = endpoint._build_prompt()
        assert "Developer Agent Context" not in prompt


# ---------------------------------------------------------------------------
# _interpret_output
# ---------------------------------------------------------------------------


class TestInterpretOutput:
    def _setup_endpoint(self, state_file, instruction_file, work_dir, before_sha="aaa111"):
        endpoint = _make_endpoint(state_file, instruction_file, work_dir)
        endpoint.read_state()
        endpoint._before_sha = before_sha
        return endpoint

    def test_escalation_returns_failure(
        self,
        state_file,
        instruction_file,
        work_dir,
    ):
        endpoint = self._setup_endpoint(state_file, instruction_file, work_dir)
        result = endpoint._interpret_output(
            "[ESCALATION] Missing coefficient width for protected mode",
            None,
        )
        assert result.exit_code == EXIT_FAILURE
        assert result.report_text == "BLOCKED"
        assert result.detail["reason"] == "escalation"

    def test_escalation_in_middle_of_output(
        self,
        state_file,
        instruction_file,
        work_dir,
    ):
        endpoint = self._setup_endpoint(state_file, instruction_file, work_dir)
        result = endpoint._interpret_output(
            "I started but then [ESCALATION] need more info",
            None,
        )
        assert result.exit_code == EXIT_FAILURE
        assert result.report_text == "BLOCKED"

    @patch.object(TbCoderSpecialist, "_commit_agent_changes", return_value=None)
    def test_no_commit_returns_failure(
        self,
        mock_commit,
        state_file,
        instruction_file,
        work_dir,
    ):
        endpoint = self._setup_endpoint(state_file, instruction_file, work_dir)
        result = endpoint._interpret_output("Done, no changes needed.", None)
        assert result.exit_code == EXIT_FAILURE
        assert "No commit" in result.report_text

    @patch.object(TbCoderSpecialist, "_commit_agent_changes")
    def test_successful_commit(
        self,
        mock_commit,
        state_file,
        instruction_file,
        work_dir,
    ):
        mock_commit.return_value = {
            "sha": "bbb222c",
            "subject": "test: add bank_sel directed checks",
            "stat_summary": "3 files changed, +42 -18",
            "changed_files": ["tb/mod_a_tb.sv", "tb/mod_b_tb.sv", "tb/pkg_tb.sv"],
        }
        endpoint = self._setup_endpoint(state_file, instruction_file, work_dir)
        result = endpoint._interpret_output("All changes applied.", None)

        assert result.exit_code == EXIT_SUCCESS
        assert "RESULT: PASS" in result.report_text
        assert "3 files changed" in result.report_text
        assert "bbb222c" in result.report_text
        assert "mod_a_tb.sv" in result.report_text
        assert result.detail["commit_sha"] == "bbb222c"
        assert result.detail["changed_files"] == [
            "tb/mod_a_tb.sv",
            "tb/mod_b_tb.sv",
            "tb/pkg_tb.sv",
        ]

    @patch.object(TbCoderSpecialist, "_commit_agent_changes")
    def test_uses_structured_commit_message(
        self,
        mock_commit,
        state_file,
        instruction_file,
        work_dir,
    ):
        mock_commit.return_value = {
            "sha": "ccc333d",
            "subject": "Add palindrome detector tb",
            "stat_summary": "1 file changed, +38 -0",
            "changed_files": ["tb/palindrome_detect_tb.sv"],
        }
        endpoint = self._setup_endpoint(state_file, instruction_file, work_dir)
        structured = {"commit_message": "Add palindrome detector tb"}
        endpoint._interpret_output("Done.", structured)
        # Verify commit was called with the structured message
        mock_commit.assert_called_once()
        call_kwargs = mock_commit.call_args
        assert call_kwargs[0][0] == "Add palindrome detector tb"

    @patch.object(TbCoderSpecialist, "_commit_agent_changes")
    def test_falls_back_to_default_message(
        self,
        mock_commit,
        state_file,
        instruction_file,
        work_dir,
    ):
        mock_commit.return_value = {
            "sha": "ddd444e",
            "subject": "implement: apply changes",
            "stat_summary": "1 file changed, +10 -0",
            "changed_files": ["tb/mod_a_tb.sv"],
        }
        endpoint = self._setup_endpoint(state_file, instruction_file, work_dir)
        endpoint._interpret_output("Done.", None)  # no structured output
        call_kwargs = mock_commit.call_args
        assert call_kwargs[0][0] == "feat: apply changes"


# ---------------------------------------------------------------------------
# modifies_category
# ---------------------------------------------------------------------------


class TestModifiesCategory:
    def test_tb_category_sets_modifies(
        self,
        state_file,
        instruction_file,
        work_dir,
    ):
        endpoint = _make_endpoint(
            state_file,
            instruction_file,
            work_dir,
            scope="tb/*.sv",
        )
        endpoint.read_state()
        with patch("booley.specialists.tb_coder.Specialist._run") as mock_super:
            mock_super.return_value = McpToolResult(exit_code=EXIT_SUCCESS)
            endpoint._run()
        assert endpoint.modifies_category == "tb"


# ---------------------------------------------------------------------------
# Criteria invalidation
# ---------------------------------------------------------------------------


class TestCriteriaInvalidation:
    def test_tb_resets_sim_and_tb_review_only(self, state_file, instruction_file, work_dir):
        """TB edits invalidate sim/TB review while preserving RTL evidence."""
        endpoint = _make_endpoint(
            state_file,
            instruction_file,
            work_dir,
            scope="tb/*.sv",
        )
        endpoint.read_state()
        endpoint.modifies_category = "tb"
        reset = endpoint.invalidate_dependent_criteria()
        assert "sim_pass_lite" in reset
        assert "coverage_toggle" not in reset
        assert "coverage_branch" not in reset
        assert "review_tb_quality_done" in reset
        assert "lint_clean_lite" not in reset
        assert "review_rtl_bugs_done" not in reset
        assert "synthesis_ok_lite" not in reset


# ---------------------------------------------------------------------------
# Diff stats extraction
# ---------------------------------------------------------------------------


class TestGitHelpers:
    @patch("booley.specialists.specialist.subprocess.run")
    def test_read_commit_info_parses(self, mock_run):
        """Verify _read_commit_info extracts sha, subject, stats, and per-file stats."""
        mock_run.side_effect = [
            MagicMock(stdout="abc1234 test: refactor bank_sel checks\n", returncode=0),  # log
            MagicMock(
                stdout="30\t10\ttb/mod_a_tb.sv\n12\t8\ttb/mod_b_tb.sv\n", returncode=0
            ),  # numstat
        ]
        info = _read_commit_info(Path("/fake"), "abc123")
        assert info["sha"] == "abc1234"
        assert info["subject"] == "test: refactor bank_sel checks"
        assert "2 files changed" in info["stat_summary"]
        assert "+42" in info["stat_summary"]
        assert info["changed_files"] == ["tb/mod_a_tb.sv", "tb/mod_b_tb.sv"]
        assert info["file_stats"] == {
            "tb/mod_a_tb.sv": (30, 10),
            "tb/mod_b_tb.sv": (12, 8),
        }

    @patch("booley.specialists.specialist.subprocess.run")
    def test_git_head_sha(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="deadbeef1234567890\n",
            returncode=0,
        )
        assert _git_head_sha(Path("/fake")) == "deadbeef1234567890"


# ---------------------------------------------------------------------------
# Resolve scope files
# ---------------------------------------------------------------------------


class TestResolveScopeFiles:
    def test_resolves_globs(self, work_dir):
        files = _resolve_scope_files("tb/*.sv", work_dir)
        assert len(files) == 2
        assert any("mod_a_tb.sv" in f for f in files)

    def test_empty_pattern(self, work_dir):
        files = _resolve_scope_files("", work_dir)
        assert files == []

    def test_no_match(self, work_dir):
        files = _resolve_scope_files("nonexistent/*.v", work_dir)
        assert files == []

    def test_forward_slashes(self, work_dir):
        files = _resolve_scope_files("tb/*.sv", work_dir)
        for f in files:
            assert "\\" not in f


# ---------------------------------------------------------------------------
# Full _run with mocked agent
# ---------------------------------------------------------------------------


class TestRunIntegration:
    @patch("booley.specialists.tb_coder.Specialist._run")
    def test_full_success_flow(
        self,
        mock_super_run,
        state_file,
        instruction_file,
        work_dir,
    ):
        """Verify _run validates, sets modifies_category, and delegates to super."""
        mock_super_run.return_value = McpToolResult(
            exit_code=EXIT_SUCCESS,
            report_text="RESULT: PASS",
        )
        endpoint = _make_endpoint(state_file, instruction_file, work_dir, scope="tb/*.sv")
        endpoint.read_state()
        _result = endpoint._run()

        assert endpoint.modifies_category == "tb"
        assert hasattr(endpoint, "_before_sha")
        mock_super_run.assert_called_once()

    def test_validation_order_instruction_before_scope(
        self,
        state_file,
        work_dir,
        tmp_path,
    ):
        """Instruction file check comes before scope validation."""
        missing = tmp_path / "missing.md"
        endpoint = _make_endpoint(
            state_file,
            missing,
            work_dir,
            scope="rtl/*.sv",  # would also fail scope check
        )
        endpoint.read_state()
        result = endpoint._run()
        # Should fail on instruction file, not scope
        assert result.exit_code == EXIT_ERROR
        assert "not found" in result.report_text


# ---------------------------------------------------------------------------
# Isolation tests moved to tests/dev_support/test_workspace_isolation.py
# ---------------------------------------------------------------------------
