"""Tests for cheatsheet sectioning (`booley cheat --<section>`)."""

from __future__ import annotations

import argparse
import itertools
import os
import re
import shutil
import sys
from pathlib import Path

import pytest

from booley.harness import booley as tlr
from booley.harness import cheatsheet, colors
from booley.runtime.paths import cheatsheet_path, skills_dir

SAMPLE = """## Booley: Quick Reference

preamble line

### Booley Flows

flows body

### Criteria

crit body

### Sandbox & Docker

sandbox body
"""


# ===========================================================================
# split_sections()
# ===========================================================================


class TestSplitSections:
    def test_preamble_and_bodies(self):
        preamble, bodies = cheatsheet.split_sections(SAMPLE)
        assert "## Booley: Quick Reference" in preamble
        assert "preamble line" in preamble
        assert set(bodies) == {"Booley Flows", "Criteria", "Sandbox & Docker"}

    def test_body_keeps_its_own_heading(self):
        _, bodies = cheatsheet.split_sections(SAMPLE)
        assert bodies["Criteria"].startswith("### Criteria")
        assert "crit body" in bodies["Criteria"]

    def test_bodies_do_not_bleed_into_each_other(self):
        _, bodies = cheatsheet.split_sections(SAMPLE)
        assert "sandbox body" not in bodies["Criteria"]

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
        assert "sandbox body" not in out
        assert "## Booley: Quick Reference" not in out  # filtered view drops the title

    def test_order_follows_the_cheatsheet_not_the_flags(self):
        out = cheatsheet.select(SAMPLE, ["sandbox", "flows"])
        assert out.index("flows body") < out.index("sandbox body")

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
        assert all(flag.replace("-", "_").isidentifier() for flag in flags)


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
        args = self._parse(["cheat", "--criteria", "--sandbox"])
        assert args.criteria and args.sandbox
        assert not args.flows

    def test_docker_flag_remains_a_sandbox_alias(self):
        args = self._parse(["cheat", "--docker"])
        assert args.sandbox

    def test_runtime_flag_remains_a_sandbox_alias(self):
        args = self._parse(["cheat", "--runtime"])
        assert args.sandbox

    def test_tips_flag_is_removed(self):
        with pytest.raises(SystemExit):
            self._parse(["cheat", "--tips"])

    def test_no_flags_matches_explicit_list(self, capsys, monkeypatch):
        monkeypatch.setattr(tlr, "cheatsheet_path", lambda: pytest.fail("index read cheatsheet"))
        assert tlr._cmd_cheat(self._parse(["cheat"]), None) == 0
        default_out = capsys.readouterr().out
        assert tlr._cmd_cheat(self._parse(["cheat", "--list"]), None) == 0
        assert default_out == capsys.readouterr().out
        assert "Example: booley cheat --commands --project-files" in default_out
        assert "booley cheat --all" in default_out
        assert "\nCommands\n" not in default_out

    @pytest.mark.parametrize("flags", (["--all"], ["--all", "--criteria"]))
    def test_all_prints_whole_sheet(self, capsys, flags):
        assert tlr._cmd_cheat(self._parse(["cheat", *flags]), Path.cwd()) == 0
        out = capsys.readouterr().out
        assert "Criteria" in out
        assert "Sandbox & Docker" in out
        assert "Tips" not in out
        assert "Architecture" not in out
        assert (
            out.index("\nCommands\n")
            < out.index("\nGoal Mode\n")
            < out.index("\nBooley Flows\n")
            < out.index("\nSpecialists\n")
            < out.index("\nCriteria\n")
            < out.index("\nProject Files\n")
        )

    def test_all_and_list_are_mutually_exclusive(self):
        with pytest.raises(SystemExit):
            self._parse(["cheat", "--all", "--list"])

    def test_multiple_sections_print_only_requested_sections(self, capsys):
        args = self._parse(["cheat", "--commands", "--project-files"])
        assert tlr._cmd_cheat(args, None) == 0
        out = capsys.readouterr().out
        assert "\nCommands\n" in out
        assert "\nProject Files\n" in out
        assert "\nCriteria\n" not in out
        assert "\nSandbox & Docker\n" not in out

    def test_commands_include_configured_agent_chat(self, capsys):
        assert tlr._cmd_cheat(self._parse(["cheat", "--commands"]), Path.cwd()) == 0
        out = " ".join(capsys.readouterr().out.split())
        assert "Sandbox-only commands" in out
        assert "booley either Host: help; Sandbox: open the configured Developer Agent CLI" in out
        assert "booley chat Explicit spelling of the Sandbox default booley command" in out

    def test_commands_are_grouped_by_execution_location(self, capsys):
        assert tlr._cmd_cheat(self._parse(["cheat", "--commands"]), Path.cwd()) == 0
        out = capsys.readouterr().out
        assert (
            out.index("Sandbox-only commands")
            < out.index("Either-location commands")
            < out.index("Host-only commands")
        )

    def test_criteria_flag_narrows_output(self, capsys):
        assert tlr._cmd_cheat(self._parse(["cheat", "--criteria"]), Path.cwd()) == 0
        out = capsys.readouterr().out
        assert "ELAB:{target}" in out  # live-spliced criteria table survives filtering
        assert "SYNTH:{target}" in out
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
        assert "REVIEW:rtl_protocol" in specialists_out
        assert "Target campaign with target + scope" in specialists_out
        assert "\n  sim " not in specialists_out

    def _stub_terminal(self, monkeypatch, *, tty: bool, columns: int = 100, color: bool = False):
        """Fix TTY, colour, and width detection; the host terminal never leaks in."""
        monkeypatch.setenv("COLUMNS", "300")  # misleading: the stubbed size must win
        monkeypatch.setattr(sys.stdout, "isatty", lambda: tty)
        monkeypatch.setattr(
            shutil, "get_terminal_size", lambda *_: os.terminal_size((columns, 24))
        )
        monkeypatch.setattr(colors, "COLORS_ENABLED", color)

    def _cheat_lines(self, capsys, *flags: str) -> list[str]:
        assert tlr._cmd_cheat(self._parse(["cheat", *flags]), Path.cwd()) == 0
        return capsys.readouterr().out.splitlines()

    @pytest.mark.parametrize(
        ("section", "term"),
        [
            pytest.param(section, term, id=f"{section}-{term}")
            for section in (*cheatsheet.section_slugs(), "all")
            for term in ("pipe", "tty80")
            # The commands tables already fit 100 columns; tty80 and "all" cover them.
            if (section, term) != ("commands", "pipe")
        ],
    )
    def test_every_section_fits_the_width_budget(self, capsys, monkeypatch, section, term):
        piped = term == "pipe"
        budget = 100 if piped else 80
        self._stub_terminal(monkeypatch, tty=not piped, columns=budget, color=not piped)

        lines = self._cheat_lines(capsys, f"--{section}")

        assert lines
        widest = max(colors.len_visible(line) for line in lines)
        assert widest <= budget, f"--{section} has a {widest}-column line (budget {budget})"

    @pytest.mark.parametrize("section", (*cheatsheet.section_slugs(), "all"))
    def test_no_trailing_whitespace_or_blank_runs(self, capsys, monkeypatch, section):
        self._stub_terminal(monkeypatch, tty=False)

        lines = self._cheat_lines(capsys, f"--{section}")

        assert all(line == line.rstrip() for line in lines)
        assert not any(not a and not b for a, b in itertools.pairwise(lines))
        headings = {sec.heading for sec in cheatsheet.SECTIONS}
        for index, line in enumerate(lines):
            if line in headings:
                assert lines[index + 1] == ""
                assert lines[index + 2] != ""

    @pytest.mark.parametrize("columns", (60, 100), ids=("tty60", "tty100"))
    def test_index_is_an_aligned_two_column_list(self, capsys, monkeypatch, columns):
        self._stub_terminal(monkeypatch, tty=True, columns=columns, color=True)
        assert tlr._cmd_cheat(self._parse(["cheat"]), None) == 0
        raw = capsys.readouterr().out
        lines = [colors.strip_ansi(line) for line in raw.splitlines()]

        assert "`" not in raw
        assert colors.accent("booley targets") in raw
        flags = [cheatsheet.section_flags(slug)[0] for slug in cheatsheet.section_slugs()]
        column = 2 + len("--") + max(map(len, flags)) + 2
        rows = [i for i, line in enumerate(lines) if line.startswith("  --")]
        assert len(rows) == len(flags)
        for index, flag in zip(rows, flags, strict=True):
            assert colors.accent(f"--{flag}") in raw
            assert lines[index][:column] == f"  --{flag}".ljust(column)
            assert lines[index][column] != " "
        assert all(colors.len_visible(line) <= columns for line in lines)

        sandbox = rows[-1]
        continuation = lines[sandbox + 1].startswith(" " * column)
        if columns == 100:
            assert not continuation  # the styled flag does not make the row wrap early
        else:
            assert continuation and lines[sandbox + 1][column] != " "

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
            "goalsets/",
            "doctor-waivers.toml",
            "AGENTS.md",
        ):
            assert path in basic
        assert "/booley-goal" in basic
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
        assert "alias: --runtime, --docker" in out

    def test_usage_advertises_cheatsheet(self):
        usage = (Path(__file__).resolve().parents[2] / "docs" / "user" / "USAGE.md").read_text(
            encoding="utf-8"
        )
        assert "start with `booley cheat`" in usage
        assert "booley cheat --goals" in usage
        assert "booley cheat --commands --project-files" in usage

    def test_missing_cheatsheet_is_reported(self, capsys, monkeypatch):
        monkeypatch.setattr(tlr, "cheatsheet_path", lambda: Path("/nonexistent/cheat.md"))
        assert tlr._cmd_cheat(self._parse(["cheat", "--all"]), Path.cwd()) == 1
        assert "cheatsheet not found" in capsys.readouterr().err


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


def test_command_table_matches_locations():
    """All public commands and their venues agree with top-level help."""
    text = cheatsheet_path().read_text(encoding="utf-8").split("### Goal Mode")[0]
    sections = re.split(r"^#### ", text, flags=re.MULTILINE)[1:]
    locations = {}
    for section in sections:
        heading = section.splitlines()[0]
        for command, tail in re.findall(r"^\| `booley (\w+)` \| (.*)", section, re.MULTILINE):
            if heading == "Host-only commands":
                location = "host"
            elif heading == "Sandbox-only commands":
                location = "Sandbox"
            else:
                location = tail.split("|")[0].strip()
            assert command not in locations
            locations[command] = location
    assert locations == {
        name: location.value for name, location in tlr.command_locations().items()
    }


def test_skills_table_lists_every_packaged_skill():
    """Every packaged skill directory has a row in the cheatsheet Skills table (#1427)."""
    _, bodies = cheatsheet.split_sections(cheatsheet_path().read_text(encoding="utf-8"))
    listed = set(re.findall(r"^\| `/([\w-]+)`", bodies["Skills"], re.MULTILINE))
    packaged = {path.name for path in skills_dir().iterdir() if (path / "SKILL.md").is_file()}
    assert packaged, "no packaged skills found; skills_dir() is wrong"
    assert listed == packaged


def test_init_force_row_matches_its_help():
    """The `init --force` row keeps caches, as `init --help` says, not a no-cache rebuild."""
    parser = tlr._build_parser()
    commands = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    force = next(a for a in commands.choices["init"]._actions if "--force" in a.option_strings)
    row = next(
        line
        for line in cheatsheet_path().read_text(encoding="utf-8").splitlines()
        if line.startswith("| `booley init --force` |")
    )
    assert "caches" in force.help
    assert "keep caches" in row
    assert "no cache" not in row
