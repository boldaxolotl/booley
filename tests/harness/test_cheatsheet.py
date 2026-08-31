"""Tests for cheatsheet sectioning (`booley cheat --<section>`)."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from booley.harness import booley as tlr
from booley.harness import cheatsheet
from booley.runtime.paths import cheatsheet_path

SAMPLE = """## Booley: Quick Reference

preamble line

### Booley Flows

flows body

### Criteria

crit body

### Runtime & Docker

runtime body
"""


# ===========================================================================
# split_sections()
# ===========================================================================


class TestSplitSections:
    def test_preamble_and_bodies(self):
        preamble, bodies = cheatsheet.split_sections(SAMPLE)
        assert "## Booley: Quick Reference" in preamble
        assert "preamble line" in preamble
        assert set(bodies) == {"Booley Flows", "Criteria", "Runtime & Docker"}

    def test_body_keeps_its_own_heading(self):
        _, bodies = cheatsheet.split_sections(SAMPLE)
        assert bodies["Criteria"].startswith("### Criteria")
        assert "crit body" in bodies["Criteria"]

    def test_bodies_do_not_bleed_into_each_other(self):
        _, bodies = cheatsheet.split_sections(SAMPLE)
        assert "runtime body" not in bodies["Criteria"]

    def test_no_headings_is_all_preamble(self):
        preamble, bodies = cheatsheet.split_sections("just text\n")
        assert preamble == "just text"
        assert bodies == {}


# ===========================================================================
# select()
# ===========================================================================


class TestSelect:
    def test_empty_selection_returns_text_verbatim(self):
        assert cheatsheet.select(SAMPLE, []) == SAMPLE

    def test_single_section_drops_everything_else(self):
        out = cheatsheet.select(SAMPLE, ["criteria"])
        assert "crit body" in out
        assert "runtime body" not in out
        assert "## Booley: Quick Reference" not in out  # filtered view drops the title

    def test_order_follows_the_cheatsheet_not_the_flags(self):
        out = cheatsheet.select(SAMPLE, ["runtime", "flows"])
        assert out.index("flows body") < out.index("runtime body")

    def test_missing_section_is_skipped_not_fatal(self):
        """A section the file doesn't carry must not blank out the ones it does."""
        out = cheatsheet.select(SAMPLE, ["criteria", "missing"])
        assert "crit body" in out


# ===========================================================================
# Drift guard: SECTIONS vs the shipped cheatsheet
# ===========================================================================


class TestSectionsMatchCheatsheet:
    @staticmethod
    def _headings() -> list[str]:
        text = cheatsheet_path().read_text(encoding="utf-8")
        return [ln[len("### ") :].strip() for ln in text.splitlines() if ln.startswith("### ")]

    def test_every_heading_is_flag_addressable(self):
        """A `### ` heading with no Section entry is unreachable by flag."""
        declared = {s.heading for s in cheatsheet.SECTIONS}
        assert set(self._headings()) == declared

    def test_declared_order_matches_file_order(self):
        assert [section.heading for section in cheatsheet.SECTIONS] == self._headings()

    def test_architecture_is_not_a_section(self):
        assert "architecture" not in cheatsheet.section_slugs()
        assert "Architecture" not in self._headings()

    def test_slugs_are_unique_and_flag_safe(self):
        flags = tuple(
            flag for slug in cheatsheet.section_slugs() for flag in cheatsheet.section_flags(slug)
        )
        assert len(set(flags)) == len(flags)
        assert all(flag.isidentifier() for flag in flags)


# ===========================================================================
# `booley cheat` CLI wiring
# ===========================================================================


class TestCheatCommand:
    @staticmethod
    def _parse(argv):
        return tlr._build_parser().parse_args(argv)

    def test_every_section_has_a_flag(self):
        args = self._parse(["cheat"])
        for slug in cheatsheet.section_slugs():
            assert getattr(args, slug) is False

    def test_flags_are_combinable(self):
        args = self._parse(["cheat", "--criteria", "--runtime"])
        assert args.criteria and args.runtime
        assert not args.flows

    def test_docker_flag_remains_a_runtime_alias(self):
        args = self._parse(["cheat", "--docker"])
        assert args.runtime

    def test_tips_flag_is_removed(self):
        with pytest.raises(SystemExit):
            self._parse(["cheat", "--tips"])

    def test_no_flags_prints_whole_sheet(self, capsys):
        assert tlr._cmd_cheat(self._parse(["cheat"]), Path.cwd()) == 0
        out = capsys.readouterr().out
        assert "Criteria" in out
        assert "Runtime & Docker" in out
        assert "Tips" not in out
        assert "Architecture" not in out
        assert (
            out.index("\nCommands\n")
            < out.index("\nTicket Board\n")
            < out.index("\nBooley Flows\n")
            < out.index("\nSpecialists\n")
            < out.index("\nCriteria\n")
            < out.index("\nProject Files\n")
        )

    def test_commands_include_configured_agent_chat(self, capsys):
        assert tlr._cmd_cheat(self._parse(["cheat", "--commands"]), Path.cwd()) == 0
        out = " ".join(capsys.readouterr().out.split())
        assert "booley Open the Project's configured Claude Code or Codex CLI" in out
        assert "booley chat Explicit spelling of the default booley command" in out

    def test_board_flag_explains_review_without_partial_rework(self, capsys):
        assert tlr._cmd_cheat(self._parse(["cheat", "--board"]), Path.cwd()) == 0
        out = " ".join(capsys.readouterr().out.split())
        assert "review → archived" in out
        assert "review ──full reset──► queued" in out
        assert "Ordinary review → queued is invalid" in out
        assert "clean start" in out
        assert "Booley Flows" not in out

    def test_criteria_flag_narrows_output(self, capsys):
        assert tlr._cmd_cheat(self._parse(["cheat", "--criteria"]), Path.cwd()) == 0
        out = capsys.readouterr().out
        assert "elab_pass" in out  # live-spliced criteria table survives filtering
        assert "synthesis_ok" in out
        assert "booley-sandbox" not in out  # Docker section is gone

    def test_flows_and_specialists_are_separate_views(self, capsys):
        assert tlr._cmd_cheat(self._parse(["cheat", "--flows"]), Path.cwd()) == 0
        flows_out = capsys.readouterr().out
        assert "Booley Flows" in flows_out
        assert "\n  sim " in flows_out
        assert "review_rtl_protocol" not in flows_out

        assert tlr._cmd_cheat(self._parse(["cheat", "--specialists"]), Path.cwd()) == 0
        specialists_out = capsys.readouterr().out
        assert "Specialists" in specialists_out
        assert "review_rtl_protocol" in specialists_out
        assert "Target campaign with target + scope" in specialists_out
        assert "\n  sim " not in specialists_out

    @pytest.mark.parametrize("section", ("commands", "board", "project", "skills"))
    def test_compact_tables_fit_120_columns(self, capsys, section):
        """Keep the fixed-width table from wrapping its separator or rows."""
        assert tlr._cmd_cheat(self._parse(["cheat", f"--{section}"]), Path.cwd()) == 0
        out = capsys.readouterr().out
        assert max(map(len, out.splitlines())) <= 120

    def test_commands_lists_every_public_top_level_command(self, capsys):
        parser = tlr._build_parser()
        subparsers = next(
            action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
        )
        public = {
            action.dest
            for action in subparsers._choices_actions
            if action.help not in (None, argparse.SUPPRESS)
        }

        assert tlr._cmd_cheat(self._parse(["cheat", "--commands"]), Path.cwd()) == 0
        out = capsys.readouterr().out
        for command in public:
            if command == "feedback":
                continue  # Public docs route feedback through /booley-feedback.
            assert f"booley {command}" in out
        assert "booley mcp-tool" not in out
        assert "booley link-guidance" not in out
        assert "booley tool" not in out
        assert "booley shell" not in out

    def test_project_files_separate_basic_and_custom_inputs(self, capsys):
        assert tlr._cmd_cheat(self._parse(["cheat", "--project"]), Path.cwd()) == 0
        out = capsys.readouterr().out
        basic, custom = out.split("Custom tool files", maxsplit=1)
        for path in (
            "booley.toml",
            "tests.toml",
            "ticket_creation.md",
            "doctor-waivers.toml",
            "AGENTS.md",
        ):
            assert path in basic
        assert "/booley-ticket-create" in basic
        for path in ("criteria.toml", "mcp_tools/*.py"):
            assert path not in basic
            assert path in custom
        assert "Most projects do not need these files" in " ".join(custom.split())
        assert "tickets/<state>/<slug>.md" not in out
        assert "SETUP-PLAN.md" not in out
        assert "guides/*.md" not in out

    def test_list_names_every_section(self, capsys):
        assert tlr._cmd_cheat(self._parse(["cheat", "--list"]), Path.cwd()) == 0
        out = capsys.readouterr().out
        for slug in cheatsheet.section_slugs():
            assert f"--{slug}" in out
        assert "alias: --docker" in out

    def test_usage_advertises_cheatsheet(self):
        usage = (Path(__file__).resolve().parents[2] / "docs" / "USAGE.md").read_text(
            encoding="utf-8"
        )
        assert "start with `booley cheat`" in usage
        assert "booley cheat --board" in usage
        assert "booley cheat --commands --project" in usage

    def test_missing_cheatsheet_is_reported(self, capsys, monkeypatch):
        monkeypatch.setattr(tlr, "cheatsheet_path", lambda: Path("/nonexistent/cheat.md"))
        assert tlr._cmd_cheat(self._parse(["cheat"]), Path.cwd()) == 1
        assert "cheatsheet not found" in capsys.readouterr().err


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
