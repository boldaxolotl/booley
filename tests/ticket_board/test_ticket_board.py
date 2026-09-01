#!/usr/bin/env python3
"""Comprehensive tests for ticket_board.py.

Tests cover pure functions, TicketIO filesystem operations, composite operations,
CLI integration via main(argv=[...]), and edge cases.

Adapted for the filesystem-based ticket system (no board.json).
"""

import json
import sys
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import pytest

# Import from the scripts directory (one level up from unit/)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from datetime import UTC

from booley.ticket_board import (
    PRIORITY_ORDER,
    PROGRESS_DEFAULTS,
    STEP_ORDER,
    VALID_PRIORITIES,
    TicketFileSpec,
    TicketIO,
    append_incident,
    attribute_tokens_to_steps,
    classify_tickets,
    clear_from_step,
    collect_all_messages,
    collect_step_transcript_usage,
    collect_step_usage,
    compute_cost_detailed,
    compute_step_durations,
    display_board,
    find_ticket_file,
    fmt_datetime_user,
    fmt_duration,
    format_frontmatter,
    format_usage_report,
    format_validate_logs_report,
    generate_slug,
    load_progress,
    main,
    next_from_planned,
    no_large_area_increase,
    no_unfixed_critical,
    normalize_dir,
    op_approve,
    op_archive,
    op_block,
    op_board_move,
    op_claim,
    op_complete,
    op_fail,
    op_handoff,
    op_promote_waiting,
    op_reset,
    op_unblock,
    parse_frontmatter,
    parse_transcript_usage,
    parse_transitions_log,
    parse_usage_log,
    resume_detect,
    save_progress,
    scan_all_tickets,
    select_mutation_config,
    update_frontmatter,
    usage_entries_to_steps,
    validate_logs,
    validate_ticket_fields,
)

# Internal helpers imported directly from source modules for testing
from booley.ticket_board.analytics import _match_pricing
from booley.ticket_board.paths import (
    STEP_DIR_MAP,
    human_log_file,
    runtime_file,
)

# Test-local mirror of the filename sets that used to live in paths.py
# (RUNTIME_FILENAMES / HUMAN_LOG_FILENAMES had no production callers and
# were removed; _persistent_file below still needs the routing logic to
# lay out fake log directories for validate_logs() tests).
_TEST_RUNTIME_FILENAMES = {
    "booley_state.json",
    "display.jsonl",
    "progress.json",
    "status.json",
    "ticket.lock",
}

_TEST_HUMAN_LOG_FILENAMES = {
    "harness.log",
    "run.log",
    "transitions.log",
}


def _persistent_file(logs_dir, slug, filename):
    """Test-local routing mirror of the retired paths.persistent_file().

    Kept here (rather than in production code) because the real function
    had no production callers; this fixture still needs the routing logic
    to lay out fake log directories for validate_logs() tests.
    """
    if filename in _TEST_RUNTIME_FILENAMES:
        return runtime_file(logs_dir, slug, filename)
    if filename in _TEST_HUMAN_LOG_FILENAMES:
        return human_log_file(logs_dir, slug, filename)
    return logs_dir / slug / filename


# ---------------------------------------------------------------------------
# Auto-mock notifications — prevent real pushes during tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_ntfy(monkeypatch):
    """Silence all ntfy.sh notifications during tests."""
    monkeypatch.setattr("booley.ticket_board.operations.ntfy_send", lambda *a, **kw: None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_tio(tmp_path):
    """Create a tickets dir with subdirectories and return a TicketIO instance."""
    tickets_dir = tmp_path / "tickets"
    for d in [
        "board/drafts",
        "board/queue",
        "board/waiting",
        "board/active",
        "board/blocked",
        "board/review",
        "board/done",
        "board/archived",
    ]:
        (tickets_dir / d).mkdir(parents=True, exist_ok=True)
    (tickets_dir / "logs").mkdir(parents=True, exist_ok=True)
    # Pin project_root: the bare tmp/tickets layout matches neither supported
    # convention, so TicketIO's inference would walk up to the SHARED pytest
    # tmp base — where stale .core files from other tests' retained runs leak
    # into .core-derived validation (tb_source_prefixes rglob).
    return TicketIO(tickets_dir, project_root=tmp_path)


def make_ticket_file(tio, subdir, slug, content=None):
    """Create a ticket .md file under tickets_dir/subdir/slug.md."""
    if content is None:
        content = (
            "---\n"
            f"summary: {slug.replace('-', ' ')}\n"
            "type: feature\n"
            "branch: master\n"
            "scope:\n  - rtl/foo.sv\n"
            "criteria:\n"
            "  mandatory:\n"
            "    sim_pass:\n"
            "      - tb/foo_tb.sv @ default @ all @ pass -> pass\n"
            "---\n"
            "## Description\nSome work.\n"
        )
    # Normalize bare dir names to board/ prefix
    if not subdir.startswith("board/"):
        subdir = f"board/{subdir}"
    d = tio.tickets_dir / subdir
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{slug}.md"
    p.write_text(content, encoding="utf-8")
    return p


def _test_step_dir(logs_dir, slug, step):
    """Test helper: return the legacy stages/<NN-step>/ path (for test setup only)."""
    dir_name = STEP_DIR_MAP[step]
    return Path(logs_dir) / slug / "stages" / dir_name


def _test_ensure_step_dir(logs_dir, slug, step):
    """Test helper: create and return the legacy stages/<NN-step>/ path."""
    d = _test_step_dir(logs_dir, slug, step)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _test_step_artifact(logs_dir, slug, step, filename):
    """Test helper: return path to artifact in legacy stage dir."""
    return _test_step_dir(logs_dir, slug, step) / filename


def _test_save_step_meta(logs_dir, slug, meta):
    """Test helper: write per-step meta.json files (replaces removed save_step_meta)."""
    for step_name, step_data in meta.items():
        dir_name = STEP_DIR_MAP.get(step_name)
        if dir_name is None:
            continue
        d = Path(logs_dir) / slug / "stages" / dir_name
        d.mkdir(parents=True, exist_ok=True)
        (d / "meta.json").write_text(json.dumps(step_data, indent=2) + "\n", encoding="utf-8")


def write_stage_file(logs_dir, slug, stage, filename, content="# placeholder"):
    """Write an artifact file into the correct per-stage directory."""
    d = _test_ensure_step_dir(logs_dir, slug, stage)
    (d / filename).write_text(content, encoding="utf-8")


def make_progress(tio, slug, progress_fields=None):
    """Create progress.json for a ticket with optional runtime field overrides."""
    import copy

    progress = copy.deepcopy(PROGRESS_DEFAULTS)
    if progress_fields:
        progress.update(progress_fields)
    save_progress(tio.logs_dir, slug, progress)
    return progress


def make_ticket_in_dir(tio, subdir, slug, extra_fields=None, body="## Description\nSome work.\n"):
    """Create a ticket .md file with frontmatter in the specified directory."""
    fields = {
        "summary": slug.replace("-", " "),
        "type": "feature",
        "branch": "master",
        "scope": ["rtl/foo.sv"],
        "criteria": {"mandatory": {"sim_pass": ["tb/foo_tb.sv @ default @ all @ pass -> pass"]}},
    }
    if extra_fields:
        fields.update(extra_fields)

    content = format_frontmatter(fields, body)

    # Normalize bare dir names to board/ prefix
    if not subdir.startswith("board/"):
        subdir = f"board/{subdir}"
    d = tio.tickets_dir / subdir
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{slug}.md"
    p.write_text(content, encoding="utf-8")
    return p


# ===========================================================================
# 1. Pure function tests
# ===========================================================================


class TestAcceptanceProgress:
    def test_find_and_scan_expose_validated_journal_state(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "review", "partial")
        acceptance = tmp_path / ".runtime" / "acceptance"
        acceptance.mkdir(parents=True)
        (acceptance / "partial.json").write_text(
            json.dumps(
                {
                    "schema": 2,
                    "transaction": "a" * 32,
                    "ticket": "partial",
                    "state": "initializing",
                    "policy": {"merge": True, "cleanup": True},
                    "participants": [
                        {
                            "role": "outer",
                            "sealed_sha": "b" * 40,
                            "ticket_ref": "refs/heads/partial",
                            "destination_ref": "refs/heads/main",
                            "destination_sha": "c" * 40,
                        }
                    ],
                    "sources": {},
                    "candidates": {},
                    "published": [],
                    "cleaned": [],
                }
            ),
            encoding="utf-8",
        )

        assert tio.find_ticket("partial")["acceptance_state"] == "initializing"
        assert scan_all_tickets(tio.tickets_dir)[0]["acceptance_state"] == "initializing"


class TestGenerateSlug:
    def test_normal_text(self):
        assert generate_slug("Add ALU pipeline module") == "add-alu-pipeline-module"

    def test_special_chars(self):
        assert generate_slug("Fix bug #42 (urgent!)") == "fix-bug-42-urgent"

    def test_unicode(self):
        # Unicode chars get stripped, leaving only ASCII alphanum + hyphens
        assert generate_slug("Sch\u00f6ne M\u00fcsik") == "sch-ne-m-sik"

    def test_truncation_at_40(self):
        long = "a" * 60
        result = generate_slug(long)
        assert len(result) <= 40

    def test_empty_string(self):
        assert generate_slug("") == ""

    def test_trailing_hyphens_stripped(self):
        # When truncation lands mid-word, trailing hyphens are removed
        slug = generate_slug("a-b-c-d-e-f-g-h-i-j-k-l-m-n-o-p-q-r-s-t-u-v-w-x-y-z")
        assert not slug.endswith("-")
        assert len(slug) <= 40

    def test_only_special_chars(self):
        assert generate_slug("!!!@@@###") == ""


class TestStepOrder:
    def test_step_order_has_expected_steps(self):
        assert "setup" in STEP_ORDER
        assert "review" in STEP_ORDER
        assert "sim-debug-loop" in STEP_ORDER


class TestSelectMutationConfig:
    def test_returns_first(self):
        assert select_mutation_config(["config_a", "config_d/variant"]) == "config_a"


class TestClassifyTickets:
    def test_mixed_statuses(self):
        # "waiting" is now a directory-level state, not derived from deps
        tickets = [
            {"status": "done", "feature_branch": "done-ticket"},
            {"status": "queued", "feature_branch": "ready", "dependencies": []},
            {
                "status": "waiting",
                "feature_branch": "waiting-on-dep",
                "dependencies": ["not-done"],
            },
            {"status": "blocked", "feature_branch": "stuck"},
        ]
        result = classify_tickets(tickets)
        assert len(result["executable"]) == 1
        assert result["executable"][0]["feature_branch"] == "ready"
        assert len(result["blocked"]) == 1
        assert len(result["waiting"]) == 1
        assert result["waiting"][0]["feature_branch"] == "waiting-on-dep"

    def test_dependency_resolution(self):
        tickets = [
            {"status": "done", "feature_branch": "dep-a"},
            {"status": "queued", "feature_branch": "child", "dependencies": ["dep-a"]},
        ]
        result = classify_tickets(tickets)
        assert len(result["executable"]) == 1
        assert result["executable"][0]["feature_branch"] == "child"

    def test_empty_board(self):
        result = classify_tickets([])
        assert result == {
            "executable": [],
            "active": [],
            "blocked": [],
            "waiting": [],
            "review": [],
            "orphaned": [],
        }


class TestResumeDetect:
    def test_fresh(self):
        entry = {"status": "queued"}
        r = resume_detect(entry)
        assert r["action"] == "fresh"
        assert r["stage"] == "setup"

    def test_continue(self):
        entry = {
            "status": "running",
            "steps_completed": ["setup", "planning"],
            "feature_branch": "x",
            "type": "feature",
        }
        r = resume_detect(entry)
        assert r["action"] == "continue"
        assert r["stage"] == "run-config"

    def test_blocked(self):
        entry = {
            "status": "blocked",
            "blocked_step": "synthesis",
            "blocked_reason": "EDA tool crash",
            "feature_branch": "x",
        }
        r = resume_detect(entry)
        assert r["action"] == "resume_blocked"
        assert r["stage"] == "synthesis"
        assert "blocked_reason" in r["clear_fields"]

    def test_resume_blocked_from_queued(self):
        entry = {
            "status": "queued",
            "steps_completed": ["setup"],
            "blocked_step": "planning",
            "feature_branch": "x",
            "type": "feature",
        }
        r = resume_detect(entry)
        assert r["action"] == "resume_blocked"
        assert r["stage"] == "planning"

    def test_continue_uses_step_order_running(self):
        """resume_detect always uses STEP_ORDER to find next stage."""
        entry = {
            "status": "running",
            "steps_completed": ["setup", "planning", "run-config"],
            "feature_branch": "",
            "type": "bugfix",
        }
        r = resume_detect(entry)
        assert r["action"] == "continue"
        # STEP_ORDER after run-config is implementation
        assert r["stage"] == next_from_planned(STEP_ORDER, "run-config")

    def test_continue_uses_step_order_queued(self):
        """Queued ticket with progress resumes via STEP_ORDER."""
        entry = {
            "status": "queued",
            "steps_completed": ["setup", "planning", "run-config"],
            "feature_branch": "",
            "type": "bugfix",
        }
        r = resume_detect(entry)
        assert r["action"] == "continue"
        assert r["stage"] == next_from_planned(STEP_ORDER, "run-config")


class TestParseFrontmatter:
    def test_scalars(self):
        text = "---\nsummary: Hello World\ntype: bugfix\n---\nBody"
        fields, body = parse_frontmatter(text)
        assert fields["summary"] == "Hello World"
        assert fields["type"] == "bugfix"
        assert body == "Body"

    def test_block_list(self):
        text = "---\nscope_current:\n  - rtl/a.sv\n  - rtl/b.sv\n---\n"
        fields, _ = parse_frontmatter(text)
        assert fields["scope_current"] == ["rtl/a.sv", "rtl/b.sv"]

    def test_inline_empty_list(self):
        text = "---\ndependencies: []\n---\n"
        fields, _ = parse_frontmatter(text)
        assert fields["dependencies"] == []

    def test_booleans(self):
        text = "---\nprotected: true\nskip: false\n---\n"
        fields, _ = parse_frontmatter(text)
        assert fields["protected"] is True
        assert fields["skip"] is False

    def test_missing_markers(self):
        text = "No frontmatter here"
        fields, body = parse_frontmatter(text)
        assert fields == {}
        assert body == text

    def test_extra_unknown_fields(self):
        text = "---\nsummary: test\ncustom_field: foo\n---\n"
        fields, _ = parse_frontmatter(text)
        assert fields["custom_field"] == "foo"

    def test_integer_values(self):
        text = "---\nmax_debug_rounds: 3\n---\n"
        fields, _ = parse_frontmatter(text)
        assert fields["max_debug_rounds"] == 3
        assert isinstance(fields["max_debug_rounds"], int)

    def test_quoted_strings(self):
        text = '---\nsummary: "true"\ncount: "42"\n---\n'
        fields, _ = parse_frontmatter(text)
        assert fields["summary"] == "true"
        assert isinstance(fields["summary"], str)
        assert fields["count"] == "42"
        assert isinstance(fields["count"], str)

    def test_single_quoted_strings(self):
        text = "---\nsummary: 'hello world'\n---\n"
        fields, _ = parse_frontmatter(text)
        assert fields["summary"] == "hello world"


class TestValidateTicketFields:
    def test_all_required_present(self):
        fields = {
            "summary": "Do something",
            "type": "feature",
            "branch": "master",
            "scope": ["rtl/foo.sv"],
            "criteria": {
                "mandatory": {"sim_pass": ["tb/foo_tb.sv @ default @ all @ pass -> pass"]}
            },
        }
        errors = validate_ticket_fields(fields, "## Description\nSome text")
        assert errors == []

    @pytest.mark.parametrize("value", ["yes", 1, None])
    def test_triage_report_must_be_boolean(self, value):
        fields = {
            "summary": "Do something",
            "type": "feature",
            "branch": "master",
            "scope": ["rtl/foo.sv"],
            "criteria": {
                "mandatory": {"sim_pass": ["tb/foo_tb.sv @ default @ all @ pass -> pass"]}
            },
            "on_success": {"triage_report": value},
        }
        errors = validate_ticket_fields(fields, "## Description\nSome text")
        assert errors == ["on_success.triage_report must be true or false"]

    @pytest.mark.parametrize("field", ["merge", "cleanup"])
    def test_on_success_flags_must_be_boolean(self, field):
        fields = {
            "summary": "Do something",
            "type": "feature",
            "branch": "master",
            "scope": ["rtl/foo.sv"],
            "criteria": {
                "mandatory": {"sim_pass": ["tb/foo_tb.sv @ default @ all @ pass -> pass"]}
            },
            "on_success": {field: "yes"},
        }
        errors = validate_ticket_fields(fields, "## Description\nSome text")
        assert errors == [f"on_success.{field} must be true or false"]

    def test_integration_base_is_rejected_for_schema_3_tickets(self):
        fields = {
            "summary": "Do something",
            "type": "feature",
            "branch": "main",
            "integration_base": "main~1",
            "scope": ["rtl/foo.sv"],
            "criteria": {
                "mandatory": {"sim_pass": ["tb/foo_tb.sv @ default @ all @ pass -> pass"]}
            },
        }

        errors = validate_ticket_fields(fields, "## Description\nSome text")

        assert errors == [
            "Deprecated field 'integration_base': schema-3 Tickets publish their "
            "sealed Ticket refs directly to destination refs"
        ]

    def test_missing_fields(self):
        errors = validate_ticket_fields({}, "## Description\ntext")
        missing = [e for e in errors if "Missing required field" in e]
        assert len(missing) >= 5
        # Verify each required field is mentioned at least once
        for f in ("summary", "type", "branch", "scope", "criteria"):
            assert any(f in e for e in missing), f"No error for {f}"

    def test_invalid_type(self):
        fields = {
            "summary": "x",
            "type": "invalid_type",
            "branch": "m",
            "scope_current": [],
            "criteria": {
                "mandatory": {"sim_pass": ["tb/foo_tb.sv @ default @ all @ pass -> pass"]}
            },
        }
        errors = validate_ticket_fields(fields, "## Description\ntext")
        assert any("Invalid type" in e for e in errors)

    def test_missing_description(self):
        fields = {
            "summary": "x",
            "type": "bugfix",
            "branch": "m",
            "scope_current": [],
            "criteria": {
                "mandatory": {"sim_pass": ["tb/foo_tb.sv @ default @ all @ fail -> pass"]}
            },
        }
        errors = validate_ticket_fields(fields, "No description section")
        assert any("## Description" in e for e in errors)

    def test_spec_always_optional(self):
        """Spec is informational �� not required for any ticket type."""
        fields = {
            "summary": "x",
            "type": "feature",
            "branch": "m",
            "scope_current": [],
            "criteria": {
                "mandatory": {"sim_pass": ["tb/foo_tb.sv @ default @ all @ pass -> pass"]}
            },
        }
        errors = validate_ticket_fields(fields, "## Description\ntext")
        assert not any("'spec'" in e or "field: spec" in e.lower() for e in errors)

    def test_scope_must_be_list(self):
        fields = {
            "summary": "x",
            "type": "bugfix",
            "branch": "m",
            "scope": "rtl/foo.sv",  # string, not list
            "criteria": {
                "mandatory": {"sim_pass": ["tb/foo_tb.sv @ config_a @ all @ fail -> pass"]}
            },
        }
        errors = validate_ticket_fields(fields, "## Description\ntext")
        assert any("scope" in e and "list" in e for e in errors)

    def test_scope_new_tag_accepted(self):
        """scope entries with [new] suffix are valid."""
        fields = {
            "summary": "x",
            "type": "feature",
            "branch": "m",
            "scope": ["rtl/new.sv [new]"],
            "criteria": {
                "mandatory": {"sim_pass": ["tb/foo_tb.sv @ default @ all @ pass -> pass"]}
            },
        }
        errors = validate_ticket_fields(fields, "## Description\ntext")
        assert not any("scope" in e for e in errors)

    def test_scope_duplicated_source_root_rejected(self):
        fields = {
            "summary": "x",
            "type": "feature",
            "branch": "m",
            "scope": ["rtl/rtl/detect_sequence.sv [new]"],
            "criteria": {
                "mandatory": {"sim_pass": ["tb/foo_tb.sv @ default @ all @ pass -> pass"]}
            },
        }
        errors = validate_ticket_fields(fields, "## Description\ntext")
        assert any("duplicated source root" in e for e in errors)

    def test_empty_scope_is_error(self):
        fields = {
            "summary": "x",
            "type": "bugfix",
            "branch": "m",
            "scope": [],
            "criteria": {
                "mandatory": {"sim_pass": ["tb/foo_tb.sv @ config_a @ all @ fail -> pass"]}
            },
        }
        errors = validate_ticket_fields(fields, "## Description\ntext")
        assert any("scope" in e and "empty" in e for e in errors)

    def test_empty_criteria_mandatory_rejected(self):
        """criteria with empty mandatory section is rejected."""
        fields = {
            "summary": "x",
            "type": "feature",
            "branch": "m",
            "scope": ["rtl/foo.sv"],
            "criteria": {"mandatory": {}},
        }
        errors = validate_ticket_fields(fields, "## Description\ntext")
        assert any("mandatory" in e and "at least one" in e for e in errors)

    def test_nonempty_criteria_accepted(self):
        fields = {
            "summary": "x",
            "type": "feature",
            "branch": "m",
            "scope": ["rtl/foo.sv"],
            "criteria": {
                "mandatory": {"sim_pass": ["tb/foo_tb.sv @ default @ all @ pass -> pass"]}
            },
        }
        errors = validate_ticket_fields(fields, "## Description\ntext")
        assert not any("criteria" in e.lower() and "empty" in e for e in errors)

    def test_retired_criterion_key_rejected_with_rename_hint(self):
        """validate-ticket must catch a retired criterion key pre-flight.

        Regression: a ticket authored before a criteria-key rename used to pass
        validate-ticket clean and only fail opaquely mid-run (mislabeled as a
        SIGINT crash). It must now fail here with the harness's rename hint.
        """
        fields = {
            "summary": "x",
            "type": "feature",
            "branch": "m",
            "scope": ["rtl/foo.sv"],
            "criteria": {
                "mandatory": {
                    "review_rtl_functional": True,
                    "sim_pass": ["tb/foo_tb.sv @ default @ all @ pass -> pass"],
                },
                "optional": {"review_rtl_quality": True},
            },
        }
        errors = validate_ticket_fields(fields, "## Description\ntext")
        assert any("review_rtl_functional" in e and "review_rtl_bugs" in e for e in errors)
        assert any("review_rtl_quality" in e and "review_rtl_code_style" in e for e in errors)

    def test_current_criterion_keys_accepted(self):
        """The renamed-to keys must NOT trip the retired-key guard."""
        fields = {
            "summary": "x",
            "type": "feature",
            "branch": "m",
            "scope": ["rtl/foo.sv"],
            "criteria": {
                "mandatory": {
                    "review_rtl_bugs": True,
                    "sim_pass": ["tb/foo_tb.sv @ default @ all @ pass -> pass"],
                },
                "optional": {"review_rtl_code_style": True},
            },
        }
        errors = validate_ticket_fields(fields, "## Description\ntext")
        assert not any("retired key" in e for e in errors)

    def test_unknown_mandatory_criterion_is_rejected_with_catalog_hint(self):
        """F-22: intake cannot persist an unsatisfiable mandatory criterion."""
        fields = {
            "summary": "x",
            "type": "feature",
            "branch": "m",
            "scope": ["rtl/foo.sv"],
            "criteria": {"mandatory": {"criterion_no_tool_owns": True}},
        }

        errors = validate_ticket_fields(fields, "## Description\ntext")

        assert any(
            "criterion_no_tool_owns" in error and "booley cheat --criteria" in error
            for error in errors
        )

    def test_mandatory_criterion_without_live_tool_is_rejected(self):
        fields = {
            "summary": "x",
            "type": "verification",
            "branch": "m",
            "scope": ["docs/coverage.md"],
            "criteria": {"mandatory": {"coverage_toggle": 90}},
        }

        errors = validate_ticket_fields(fields, "## Description\ntext")

        assert any(
            "coverage_toggle" in error and "no enabled Flow or Specialist" in error
            for error in errors
        )

    def test_rtl_scope_without_mandatory_sim_rejected(self, tmp_path: Path):
        fields = {
            "summary": "x",
            "type": "feature",
            "branch": "m",
            "scope": ["rtl/foo.sv"],
            "criteria": {"mandatory": {"lint_clean": ["default"]}},
        }
        errors = validate_ticket_fields(
            fields,
            "## Description\ntext",
            project_root=tmp_path,
            check_files=True,
        )
        assert any("mandatory sim_*" in e for e in errors)

    def test_rtl_scope_with_default_criteria_has_sim(self, tmp_path: Path):
        (tmp_path / "tb").mkdir()
        (tmp_path / "tb" / "foo_tb.sv").write_text("module foo_tb; endmodule\n", encoding="utf-8")
        fields = {
            "summary": "x",
            "type": "feature",
            "branch": "m",
            "scope": ["rtl/foo.sv"],
        }
        errors = validate_ticket_fields(
            fields,
            "## Description\ntext",
            project_root=tmp_path,
            check_files=True,
        )
        assert not any("mandatory sim_*" in e for e in errors)

    def test_wildcard_scope_without_mandatory_sim_rejected(self, tmp_path: Path):
        fields = {
            "summary": "x",
            "type": "bugfix",
            "branch": "m",
            "scope": ["*"],
            "criteria": {"mandatory": {"lint_clean": ["default"]}},
        }
        errors = validate_ticket_fields(
            fields,
            "## Description\ntext",
            project_root=tmp_path,
            check_files=True,
        )
        assert any("mandatory sim_*" in e for e in errors)

    def test_tb_scope_without_mandatory_sim_rejected(self, tmp_path: Path):
        fields = {
            "summary": "x",
            "type": "verification",
            "branch": "m",
            "scope": ["tb/foo_tb.sv"],
            "criteria": {"mandatory": {"coverage_toggle": 80}},
        }
        errors = validate_ticket_fields(
            fields,
            "## Description\ntext",
            project_root=tmp_path,
            check_files=True,
        )
        assert any("mandatory sim_*" in e for e in errors)

    def test_rtl_scope_with_sim_and_existing_tb_accepted(self, tmp_path: Path):
        (tmp_path / "tb").mkdir()
        (tmp_path / "tb" / "foo_tb.sv").write_text("module foo_tb; endmodule\n", encoding="utf-8")
        fields = {
            "summary": "x",
            "type": "feature",
            "branch": "m",
            "scope": ["rtl/foo.sv"],
            "criteria": {
                "mandatory": {"sim_pass": ["tb/foo_tb.sv @ default @ all @ pass -> pass"]}
            },
        }
        errors = validate_ticket_fields(
            fields,
            "## Description\ntext",
            project_root=tmp_path,
            check_files=True,
        )
        assert not any("RTL/TB-editing tickets" in e for e in errors)

    def test_rtl_scope_with_sim_and_new_tb_allowed(self, tmp_path: Path):
        fields = {
            "summary": "x",
            "type": "feature",
            "branch": "m",
            "scope": ["rtl/foo.sv", "tb/foo_tb.sv [new]"],
            "criteria": {
                "mandatory": {"sim_pass": ["tb/foo_tb.sv @ default @ all @ pass -> pass"]}
            },
        }
        errors = validate_ticket_fields(
            fields,
            "## Description\ntext",
            project_root=tmp_path,
            check_files=True,
        )
        assert not any("existing testbench" in e for e in errors)

    def test_no_existing_tb_and_no_tb_scope_rejected(self, tmp_path: Path):
        fields = {
            "summary": "x",
            "type": "feature",
            "branch": "m",
            "scope": ["rtl/foo.sv"],
            "criteria": {
                "mandatory": {"sim_pass": ["tb/foo_tb.sv @ default @ all @ pass -> pass"]}
            },
        }
        errors = validate_ticket_fields(
            fields,
            "## Description\ntext",
            project_root=tmp_path,
            check_files=True,
        )
        assert any("existing testbench" in e for e in errors)

    def test_criteria_must_be_dict(self):
        fields = {
            "summary": "x",
            "type": "feature",
            "branch": "m",
            "scope": ["rtl/foo.sv"],
            "criteria": "foo_tb",
        }
        errors = validate_ticket_fields(fields, "## Description\ntext")
        assert any("criteria" in e.lower() and "dict" in e for e in errors)

    def test_valid_baseline_tests_accepted(self):
        fields = {
            "summary": "x",
            "type": "feature",
            "branch": "m",
            "scope_current": ["rtl/foo.sv"],
            "scope_new": [],
            "criteria": {
                "mandatory": {"sim_pass": ["tb/foo_tb.sv @ default @ all @ pass -> pass"]}
            },
            "baseline_tests": {"foo_tb": "pass"},
        }
        errors = validate_ticket_fields(fields, "## Description\ntext")
        assert not any("baseline_tests" in e for e in errors)

    def test_deprecated_field_rejected(self):
        fields = {
            "summary": "x",
            "type": "feature",
            "branch": "m",
            "scope": ["rtl/foo.sv"],
            "criteria": {
                "mandatory": {"sim_pass": ["tb/foo_tb.sv @ default @ all @ pass -> pass"]}
            },
            "test": {"tb/foo_tb.sv@default": "pass"},
        }
        errors = validate_ticket_fields(fields, "## Description\ntext")
        assert any("Deprecated field 'test'" in e for e in errors)

    def test_unknown_field_rejected(self):
        fields = {
            "summary": "x",
            "type": "feature",
            "branch": "m",
            "scope": ["rtl/foo.sv"],
            "criteria": {
                "mandatory": {"sim_pass": ["tb/foo_tb.sv @ default @ all @ pass -> pass"]}
            },
            "bogus_field": "surprise",
        }
        errors = validate_ticket_fields(fields, "## Description\ntext")
        assert any("Unknown fields" in e and "bogus_field" in e for e in errors)

    def test_known_optional_fields_accepted(self):
        fields = {
            "summary": "x",
            "type": "feature",
            "branch": "m",
            "scope": ["rtl/foo.sv"],
            "criteria": {
                "mandatory": {"sim_pass": ["tb/foo_tb.sv @ default @ all @ pass -> pass"]}
            },
            "spec": "docs/spec.md",
            "synthesis": True,
            "auto_approve": False,
            "dependencies": [],
            "priority": "low",
            "feature_branch": "",
            "created": "2026-05-21T15:00:00Z",
        }
        errors = validate_ticket_fields(fields, "## Description\ntext")
        assert not any("Unknown" in e or "Deprecated" in e for e in errors)

    def test_enqueue_rejects_invalid_ticket(self, tmp_path):
        """enqueue_ticket must refuse tickets that fail field validation."""
        tio = make_tio(tmp_path)
        # Ticket with empty baseline_tests — should fail validation
        content = (
            "---\n"
            "summary: bad ticket\ntype: feature\nbranch: master\n"
            "scope_current:\n  - rtl/foo.sv\nscope_new: []\n"
            "criteria:\n  mandatory:\n    sim_pass:\n"
            "      - tb/foo_tb.sv @ default @ all @ pass -> pass\n"
            "baseline_tests: {}\n"
            "---\n## Description\ntext\n"
        )
        slug = "bad-ticket"
        make_ticket_file(tio, "board/queue", slug, content)
        success = tio.enqueue_ticket(slug, "bad ticket", "feature", "master")
        assert success is False


# ===========================================================================
# 2. Frontmatter serialization tests
# ===========================================================================


class TestFormatFrontmatter:
    """Test format_frontmatter serialization."""

    def test_basic_fields(self):
        fields = {"summary": "Hello", "type": "bugfix"}
        result = format_frontmatter(fields, "Body text")
        assert result.startswith("---\n")
        assert "summary: Hello\n" in result
        assert "type: bugfix\n" in result
        assert result.endswith("Body text\n")

    def test_field_ordering(self):
        # summary should come before type, type before branch
        fields = {"branch": "master", "summary": "A", "type": "bugfix"}
        result = format_frontmatter(fields, "")
        lines = result.split("\n")
        field_lines = [l for l in lines if ": " in l and not l.startswith("---")]
        keys = [l.split(":")[0] for l in field_lines]
        assert keys.index("summary") < keys.index("type")
        assert keys.index("type") < keys.index("branch")

    def test_list_serialization(self):
        fields = {"scope_current": ["rtl/a.sv", "rtl/b.sv"]}
        result = format_frontmatter(fields, "")
        assert "scope_current:\n  - rtl/a.sv\n  - rtl/b.sv\n" in result

    def test_empty_list(self):
        fields = {"dependencies": []}
        result = format_frontmatter(fields, "")
        assert "dependencies: []\n" in result

    def test_bool_serialization(self):
        fields = {"some_flag": True, "skip": False}
        result = format_frontmatter(fields, "")
        assert "true" in result
        assert "false" in result

    def test_int_serialization(self):
        fields = {"max_debug_rounds": 3}
        result = format_frontmatter(fields, "")
        assert "max_debug_rounds: 3\n" in result

    def test_none_values_omitted(self):
        fields = {"summary": "A", "blocked_reason": None}
        result = format_frontmatter(fields, "")
        assert "blocked_reason" not in result

    def test_round_trip(self):
        """parse_frontmatter(format_frontmatter(fields, body)) == (fields, body)."""
        fields = {
            "summary": "Test ticket",
            "type": "feature",
            "scope_current": ["rtl/a.sv"],
            "some_flag": True,
            "max_debug_rounds": 2,
            "dependencies": [],
        }
        body = "## Description\nSome work."
        text = format_frontmatter(fields, body)
        parsed_fields, parsed_body = parse_frontmatter(text)
        for k, v in fields.items():
            if v is not None:
                assert parsed_fields[k] == v, f"Mismatch on {k}: {parsed_fields.get(k)} != {v}"
        assert parsed_body == body

    def test_quoting_special_chars(self):
        """Strings with YAML-special chars get quoted."""
        fields = {"summary": 'Fix: the "bug"'}
        result = format_frontmatter(fields, "")
        # Should be quoted in output
        assert '"Fix: the \\"bug\\""' in result or "'Fix: the" in result or '"Fix:' in result

    def test_quoting_boolean_strings(self):
        """Strings that look like booleans get quoted."""
        fields = {"summary": "true"}
        result = format_frontmatter(fields, "")
        assert '"true"' in result

    def test_empty_string_round_trip(self):
        """Empty string fields must round-trip as empty strings, not lists.

        Regression: bare `key:` output parsed as an empty block list, so
        string-typed fields like feature_branch ended up as [] and failed
        validation after a single format -> parse cycle.
        """
        fields = {"feature_branch": "", "spec": ""}
        text = format_frontmatter(fields, "")
        assert 'feature_branch: ""\n' in text
        parsed, _ = parse_frontmatter(text)
        assert parsed["feature_branch"] == ""
        assert parsed["spec"] == ""


class TestUpdateFrontmatter:
    """Test update_frontmatter merge and remove."""

    def test_merge_updates(self, tmp_path):
        p = tmp_path / "ticket.md"
        p.write_text("---\nsummary: Test\ntype: bugfix\n---\n## Desc\n", encoding="utf-8")
        fields = update_frontmatter(p, {"priority": "high"})
        assert fields["priority"] == "high"
        assert fields["summary"] == "Test"
        # Verify written back correctly
        reread, _ = parse_frontmatter(p.read_text(encoding="utf-8"))
        assert reread["priority"] == "high"

    def test_remove_keys(self, tmp_path):
        p = tmp_path / "ticket.md"
        p.write_text("---\nsummary: Test\nblocked_reason: stuff\n---\n## Desc\n", encoding="utf-8")
        fields = update_frontmatter(p, {}, remove_keys=["blocked_reason"])
        assert "blocked_reason" not in fields

    def test_none_value_removes(self, tmp_path):
        p = tmp_path / "ticket.md"
        p.write_text("---\nsummary: Test\nerror: oops\n---\n## Desc\n", encoding="utf-8")
        fields = update_frontmatter(p, {"error": None})
        assert "error" not in fields

    def test_empty_string_removes(self, tmp_path):
        p = tmp_path / "ticket.md"
        p.write_text("---\nsummary: Test\nerror: oops\n---\n## Desc\n", encoding="utf-8")
        fields = update_frontmatter(p, {"error": ""})
        assert "error" not in fields

    def test_preserves_structured_campaign_during_unrelated_update(self, tmp_path):
        p = tmp_path / "ticket.md"
        criteria = {
            "mandatory": {"sim_pass": ["tb.sv @ sim_core @ smoke @ pass -> pass"]},
            "optional": {
                "mutation_score": [
                    {
                        "target": "sim_core",
                        "scope": ["picorv32.v"],
                        "min_detected": 14,
                        "total": 15,
                    }
                ]
            },
        }
        p.write_text(
            format_frontmatter(
                {"summary": "Test", "type": "feature", "criteria": criteria},
                "## Description\nTest.\n",
            ),
            encoding="utf-8",
        )

        update_frontmatter(p, {"priority": "high"})

        reread, _ = parse_frontmatter(p.read_text(encoding="utf-8"))
        assert reread["criteria"] == criteria

    def test_refuses_to_replace_ticket_when_serialization_changes_meaning(
        self, tmp_path, monkeypatch
    ):
        from booley.ticket_board import frontmatter

        p = tmp_path / "ticket.md"
        original = "---\nsummary: Test\ntype: bugfix\n---\n## Description\nTest.\n"
        p.write_text(original, encoding="utf-8")
        monkeypatch.setattr(
            frontmatter,
            "format_frontmatter",
            lambda _fields, _body: "---\nsummary: Corrupted\n---\n",
        )

        with pytest.raises(ValueError, match="serialization changed ticket fields"):
            update_frontmatter(p, {"priority": "high"})

        assert p.read_text(encoding="utf-8") == original


# ===========================================================================
# 3. Filesystem discovery tests
# ===========================================================================


class TestFindTicketFile:
    def test_finds_in_queue(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_file(tio, "queue", "my-ticket")
        path, status = find_ticket_file(tio.tickets_dir, "my-ticket")
        assert path is not None
        assert status == "queued"
        assert path.stem == "my-ticket"

    def test_finds_in_active(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_file(tio, "active", "my-ticket")
        _path, status = find_ticket_file(tio.tickets_dir, "my-ticket")
        assert status == "running"

    def test_not_found(self, tmp_path):
        tio = make_tio(tmp_path)
        path, status = find_ticket_file(tio.tickets_dir, "nonexistent")
        assert path is None
        assert status is None

    def test_accepts_md_suffixed_slug(self, tmp_path):
        """A slug copied from the board's ``<slug>.md`` name still resolves."""
        tio = make_tio(tmp_path)
        make_ticket_file(tio, "queue", "my-ticket")
        path, status = find_ticket_file(tio.tickets_dir, "my-ticket.md")
        assert path is not None
        assert status == "queued"
        assert path.stem == "my-ticket"


class TestScanAllTickets:
    def test_scans_multiple_dirs(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_file(tio, "queue", "t1")
        make_ticket_file(tio, "active", "t2")
        make_ticket_file(tio, "done", "t3")
        tickets = scan_all_tickets(tio.tickets_dir)
        assert len(tickets) == 3
        statuses = {t["status"] for t in tickets}
        assert statuses == {"queued", "running", "done"}

    def test_empty_dirs(self, tmp_path):
        tio = make_tio(tmp_path)
        tickets = scan_all_tickets(tio.tickets_dir)
        assert tickets == []

    def test_fields_populated(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_file(tio, "queue", "t1")
        tickets = scan_all_tickets(tio.tickets_dir)
        assert len(tickets) == 1
        t = tickets[0]
        assert t["summary"] == "t1"
        assert t["type"] == "feature"
        assert t["status"] == "queued"
        assert t["file"] == "board/queue/t1.md"

    def test_done_ticket_criteria_come_from_accepted_snapshot(self, tmp_path):
        from booley.criteria.state import DevelopmentState
        from booley.ticket_board.acceptance_ledger import freeze_acceptance

        tio = make_tio(tmp_path)
        make_ticket_file(tio, "done", "t1")
        state_path = runtime_file(tio.logs_dir, "t1", "booley_state.json")
        state = DevelopmentState.load(state_path)
        state.slug = "t1"
        state.init_criteria({"sim_pass": True})
        state.set_criterion("sim_pass", True)
        state.save()
        freeze_acceptance(
            tio.logs_dir / "t1",
            state,
            execution_id="generation-1",
            target_contract=None,
        )
        state_path.unlink()

        [ticket] = scan_all_tickets(tio.tickets_dir)

        assert (ticket["criteria_passed"], ticket["criteria_total"]) == (1, 1)

    def test_feature_branch_uses_filename_stem(self, tmp_path):
        """feature_branch must be the filename stem, not generate_slug(summary).

        Regression: edited summaries caused slug mismatch between filename and
        generate_slug(summary), breaking logs/branch lookups.
        """
        tio = make_tio(tmp_path)
        # Filename is "short-name" but summary would slug to something longer
        content = (
            "---\n"
            "summary: Short name with extra words that change the slug\n"
            "type: bugfix\n"
            "branch: master\n"
            "scope_current:\n  - rtl/foo.sv\n"
            "criteria:\n  mandatory:\n    sim_pass:\n"
            "      - tb/foo_tb.sv @ default @ all @ pass -> pass\n"
            "---\n"
        )
        make_ticket_file(tio, "queue", "short-name", content=content)
        tickets = scan_all_tickets(tio.tickets_dir)
        assert len(tickets) == 1
        # feature_branch must be filename stem, not the computed slug
        assert tickets[0]["feature_branch"] == "short-name"


# ===========================================================================
# 4. TicketIO tests
# ===========================================================================


class TestTicketIOFindTicket:
    def test_by_slug(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_file(tio, "queue", "my-ticket")
        t = tio.find_ticket("my-ticket")
        assert t is not None
        assert t["status"] == "queued"
        assert t["file"] == "board/queue/my-ticket.md"

    def test_not_found(self, tmp_path):
        tio = make_tio(tmp_path)
        assert tio.find_ticket("nonexistent") is None


class TestTicketIOMoveTicketFile:
    def test_moves_file(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_file(tio, "queue", "t1")
        tio.move_ticket_file("t1", "active")
        assert (tio.tickets_dir / "board" / "active" / "t1.md").exists()
        assert not (tio.tickets_dir / "board" / "queue" / "t1.md").exists()

    def test_file_accessible_after_move(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_file(tio, "queue", "t1")
        tio.move_ticket_file("t1", "active")
        _path, status = find_ticket_file(tio.tickets_dir, "t1")
        assert status == "running"


class TestTicketIOAppendTransition:
    def test_creates_dir_and_file(self, tmp_path):
        tio = make_tio(tmp_path)
        tio.append_transition("t1", "queued:init", "running:init", "test", "picked up")
        log = human_log_file(tio.logs_dir, "t1", "transitions.log")
        assert log.exists()
        content = log.read_text(encoding="utf-8")
        assert "queued:init" in content
        assert "running:init" in content

    def test_appends_correctly(self, tmp_path):
        tio = make_tio(tmp_path)
        tio.append_transition("t1", "a", "b", "actor1", "first")
        tio.append_transition("t1", "b", "c", "actor2", "second")
        content = human_log_file(tio.logs_dir, "t1", "transitions.log").read_text(encoding="utf-8")
        lines = content.strip().split("\n")
        assert len(lines) == 2
        assert "first" in lines[0]
        assert "second" in lines[1]


# ===========================================================================
# 5. Operation tests
# ===========================================================================


class TestOpClaim:
    """Atomic claim: queue→active under lock, rejects non-queued tickets."""

    def test_claims_queued_ticket(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "queue", "t1")
        assert op_claim(tio, "t1") is True
        assert (tio.tickets_dir / "board" / "active" / "t1.md").exists()
        assert not (tio.tickets_dir / "board" / "queue" / "t1.md").exists()
        log = human_log_file(tio.logs_dir, "t1", "transitions.log").read_text(encoding="utf-8")
        assert "claimed for execution" in log

    def test_rejects_already_active(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "active", "t1")
        assert op_claim(tio, "t1") is False

    def test_rejects_nonexistent(self, tmp_path):
        tio = make_tio(tmp_path)
        assert op_claim(tio, "ghost") is False

    def test_rejects_blocked(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "blocked", "t1")
        assert op_claim(tio, "t1") is False

    def test_second_claim_fails(self, tmp_path):
        """Simulates two runners claiming the same ticket."""
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "queue", "t1")
        assert op_claim(tio, "t1") is True
        assert op_claim(tio, "t1") is False


class TestOpBlock:
    def test_moves_and_updates(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "active", "t1")
        make_progress(tio, "t1", {"step": "planning"})
        op_block(tio, "t1", "need spec clarification", "planning")
        # File moved
        assert (tio.tickets_dir / "board" / "blocked" / "t1.md").exists()
        # Runtime fields updated in progress.json
        _path, status = find_ticket_file(tio.tickets_dir, "t1")
        assert status == "blocked"
        progress = load_progress(tio.logs_dir, "t1")
        assert progress["blocked_reason"] == "need spec clarification"
        # Transition logged
        log = human_log_file(tio.logs_dir, "t1", "transitions.log").read_text(encoding="utf-8")
        assert "blocked" in log


class TestOpFail:
    def test_moves_and_updates(self, tmp_path):
        """op_fail delegates to op_block — ticket lands in blocked/."""
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "active", "t1")
        make_progress(tio, "t1", {"step": "sim-debug-loop"})
        op_fail(tio, "t1", "sim timeout", "sim-debug-loop")
        assert (tio.tickets_dir / "board" / "blocked" / "t1.md").exists()
        _path, status = find_ticket_file(tio.tickets_dir, "t1")
        assert status == "blocked"
        progress = load_progress(tio.logs_dir, "t1")
        assert progress["blocked_reason"] == "sim timeout"


def _write_transitions_log(tio, slug, lines):
    """Helper: write transitions.log for a ticket."""
    log_path = human_log_file(tio.logs_dir, slug, "transitions.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _make_handoff_ready_ticket(tio, slug, stages=None):
    """Create an active ticket with run.log and valid transitions.log."""
    if stages is None:
        stages = ["setup", "planning", "implementation", "sim-debug-loop", "summary"]
    make_ticket_in_dir(
        tio, "active", slug, extra_fields={"step": "summary", "steps_completed": stages}
    )
    log_dir = tio.logs_dir / slug
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "run.log").write_text("# Developer Agent run log\n", encoding="utf-8")
    # Write required log files for stage gates
    for s in stages:
        if s == "planning":
            write_stage_file(tio.logs_dir, slug, "planning", "plan.md", "# Plan")
        elif s == "sim-debug-loop":
            write_stage_file(
                tio.logs_dir, slug, "sim-debug-loop", "sim-results.md", "# Sim Results"
            )
    # Build transitions.log with proper step-complete entries
    lines = [
        "2026-01-01T00:00:00Z | queued:init -> running:init | ticket-execute | picked up",
    ]
    prev_step = "init"
    for s in stages:
        if s == "setup":
            continue  # logged as "picked up", not "step complete"
        lines.append(
            f"2026-01-01T00:01:00Z | running:{prev_step} -> running:{s} "
            f"| ticket-execute | step complete"
        )
        prev_step = s
    _write_transitions_log(tio, slug, lines)


class TestOpHandoff:
    def test_freezes_live_acceptance_before_review(self, tmp_path):
        import json

        from booley.criteria.state import DevelopmentState
        from booley.ticket_board.acceptance_ledger import read_acceptance

        tio = make_tio(tmp_path)
        _make_handoff_ready_ticket(tio, "t1")
        state_path = runtime_file(tio.logs_dir, "t1", "booley_state.json")
        state = DevelopmentState.load(state_path)
        state.slug = "t1"
        state.ticket_type = "feature"
        state.init_criteria({"sim_pass": True, "_report_submitted": True})
        state.set_criterion("sim_pass", True)
        state.set_criterion("_report_submitted", True)
        state.save()
        prep_dir = tio.logs_dir / "t1" / ".runtime" / "triage-prep"
        prep_dir.mkdir(parents=True)
        briefing = prep_dir / "briefing.json"
        briefing.write_text('{"assessment": {}}\n', encoding="utf-8")
        (prep_dir / "manifest.json").write_text(
            json.dumps({"status": "ready", "briefing_path": str(briefing)}),
            encoding="utf-8",
        )

        assert op_handoff(tio, "t1") is True

        accepted = read_acceptance(tio.logs_dir / "t1")
        assert accepted.kind == "accepted"
        assert accepted.snapshot is not None
        assert accepted.snapshot.criteria["sim_pass"]["met"] is True
        binding = json.loads(
            (tio.logs_dir / "t1" / "acceptance" / "review-package.json").read_text(
                encoding="utf-8"
            )
        )
        assert binding["snapshot_digest"] == accepted.snapshot.digest

    def test_moves_and_updates(self, tmp_path):
        tio = make_tio(tmp_path)
        _make_handoff_ready_ticket(tio, "t1")
        op_handoff(tio, "t1")
        assert (tio.tickets_dir / "board" / "review" / "t1.md").exists()
        _path, status = find_ticket_file(tio.tickets_dir, "t1")
        assert status == "review"

    def test_rejects_stale_execution_generation(self, tmp_path, capsys):
        tio = make_tio(tmp_path)
        _make_handoff_ready_ticket(tio, "t1")
        progress = load_progress(tio.logs_dir, "t1") or dict(PROGRESS_DEFAULTS)
        progress["step"] = "summary"
        progress["steps_completed"] = ["setup", "planning", "summary"]
        progress["execution_id"] = "current-run"
        save_progress(tio.logs_dir, "t1", progress)

        assert not op_handoff(tio, "t1", expected_execution_id="stale-run")

        _path, status = find_ticket_file(tio.tickets_dir, "t1")
        assert status == "running"
        assert "execution changed concurrently" in capsys.readouterr().err
        assert not (tio.logs_dir / "t1" / "acceptance" / "accepted.json").exists()

    def test_review_handoff_announces_deferred_cleanup(self, tmp_path, capsys):
        """cleanup:true + destination:review keeps the worktree — say so (F-55)."""
        tio = make_tio(tmp_path)
        _make_handoff_ready_ticket(tio, "t1")
        assert op_handoff(tio, "t1") is True

        out = capsys.readouterr().out
        assert "cleanup is deferred until the review is approved" in out
        assert "t1" in out

    def test_handoff_blocked_without_transitions_log(self, tmp_path):
        """Handoff must fail if transitions.log is missing entirely."""
        tio = make_tio(tmp_path)
        make_ticket_in_dir(
            tio,
            "active",
            "t2",
            extra_fields={"step": "summary", "steps_completed": ["setup", "planning", "summary"]},
        )
        log_dir = tio.logs_dir / "t2"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "run.log").write_text("# Run log\n", encoding="utf-8")
        # No transitions.log at all
        result = op_handoff(tio, "t2")
        assert result is False
        # Ticket should NOT have moved to review
        assert not (tio.tickets_dir / "board" / "review" / "t2.md").exists()

    def test_handoff_blocked_with_missing_stages(self, tmp_path):
        """Handoff must fail if steps_completed has stages not in transitions.log."""
        tio = make_tio(tmp_path)
        # Progress claims many stages completed, but log only has setup
        make_ticket_in_dir(tio, "active", "t3")
        make_progress(
            tio,
            "t3",
            {
                "step": "summary",
                "steps_completed": [
                    "setup",
                    "planning",
                    "implementation",
                    "sim-debug-loop",
                    "summary",
                ],
            },
        )
        log_dir = tio.logs_dir / "t3"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "run.log").write_text("# Run log\n", encoding="utf-8")
        # Only "picked up" entry — no step-complete transitions
        _write_transitions_log(
            tio,
            "t3",
            [
                "2026-01-01T00:00:00Z | queued:init -> running:init | ticket-execute | picked up",
            ],
        )
        result = op_handoff(tio, "t3")
        assert result is False

    def test_handoff_passes_with_valid_transitions(self, tmp_path):
        """Handoff succeeds when transitions.log matches steps_completed."""
        tio = make_tio(tmp_path)
        _make_handoff_ready_ticket(tio, "t4")
        result = op_handoff(tio, "t4")
        assert result is True
        assert (tio.tickets_dir / "board" / "review" / "t4.md").exists()

    def test_handoff_with_extra_stages_in_transitions(self, tmp_path):
        """Handoff succeeds when transitions.log has all steps_completed covered."""
        tio = make_tio(tmp_path)
        stages = ["setup", "planning", "implementation", "sim-debug-loop", "summary"]
        make_ticket_in_dir(
            tio, "active", "t5", extra_fields={"step": "summary", "steps_completed": stages}
        )
        log_dir = tio.logs_dir / "t5"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "run.log").write_text("# Run log\n", encoding="utf-8")
        # Transitions for all stages
        lines = [
            "2026-01-01T00:00:00Z | queued:init -> running:init | ticket-execute | picked up",
        ]
        prev = "init"
        for s in stages:
            if s == "setup":
                continue
            lines.append(
                f"2026-01-01T00:01:00Z | running:{prev} -> running:{s} "
                f"| ticket-execute | step complete"
            )
            prev = s
        _write_transitions_log(tio, "t5", lines)
        result = op_handoff(tio, "t5")
        assert result is True


class TestInitTicket:
    def test_init_fresh(self, tmp_path):
        tio = make_tio(tmp_path)
        ticket_content = (
            "---\n"
            "summary: Fix FSM bug\n"
            "type: bugfix\n"
            "branch: master\n"
            "scope_current:\n  - rtl/fsm.sv\n"
            "criteria:\n  mandatory:\n    sim_pass:\n"
            "      - tb/fsm_tb.sv @ config_a @ all @ fail -> pass\n"
            "---\n"
            "## Description\nFix the FSM thing.\n"
        )
        make_ticket_file(tio, "queue", "fix-fsm-bug", content=ticket_content)
        result = tio.init_ticket(str(tio.tickets_dir / "board" / "queue" / "fix-fsm-bug.md"))
        assert result is not None
        assert result["slug"] == "fix-fsm-bug"
        # File moved to active/
        assert (tio.tickets_dir / "board" / "active" / "fix-fsm-bug.md").exists()
        # Logs created
        assert (tio.logs_dir / "fix-fsm-bug" / "ticket.md").exists()

    def test_init_slug_uses_filename_not_summary(self, tmp_path):
        """Slug must come from filename stem, not generate_slug(summary).

        Regression test: if the summary was edited after ticket creation,
        generate_slug(summary) produces a different slug than the filename,
        causing logs dir / branch / progress lookups to diverge.
        """
        tio = make_tio(tmp_path)
        # Filename is short, but summary is long enough to produce a DIFFERENT slug
        ticket_content = (
            "---\n"
            "summary: Fix CM3 word count false positive for short unaligned inputs\n"
            "type: bugfix\n"
            "branch: master\n"
            "scope_current:\n  - rtl/sha3.sv\n"
            "criteria:\n  mandatory:\n    sim_pass:\n"
            "      - tb/sha3_tb.sv @ config_a @ all @ fail -> pass\n"
            "---\n"
            "## Description\nFix it.\n"
        )
        make_ticket_file(tio, "queue", "fix-cm3-word-count-false-positive", content=ticket_content)
        result = tio.init_ticket(
            str(tio.tickets_dir / "board" / "queue" / "fix-cm3-word-count-false-positive.md")
        )
        assert result is not None
        # Slug must be filename stem, NOT the truncated generate_slug output
        assert result["slug"] == "fix-cm3-word-count-false-positive"
        # Logs dir must use filename stem
        assert (tio.logs_dir / "fix-cm3-word-count-false-positive" / "ticket.md").exists()
        # Must NOT create a logs dir with the computed slug
        assert not (tio.logs_dir / "fix-cm3-word-count-false-positive-for-sh").exists()


class TestEnqueueTicket:
    def test_enqueue_duplicate_guard(self, tmp_path):
        """If the ticket file already exists and has been stamped, enqueue returns False."""
        tio = make_tio(tmp_path)
        make_ticket_in_dir(
            tio,
            "queue",
            "add-thing",
            extra_fields={
                "summary": "Add a thing",
                "type": "feature",
                "created": "2026-03-15T10:00:00Z",
            },
        )
        result = tio.enqueue_ticket("add-thing", "Add a thing", "feature", "master")
        # File already exists with 'created' stamp -> duplicate guard kicks in
        assert result is False

    def test_enqueue_stamps_frontmatter(self, tmp_path):
        """enqueue_ticket stamps created/last_update when file exists but hasn't
        been stamped yet (no 'created' field)."""
        tio = make_tio(tmp_path)
        # Write the file directly without a 'created' field
        content = (
            "---\n"
            "summary: A completely different summary here\n"
            "type: feature\n"
            "branch: master\n"
            "scope:\n  - rtl/foo.sv\n"
            "criteria:\n  mandatory:\n    sim_pass:\n"
            "      - tb/foo_tb.sv @ default @ all @ pass -> pass\n"
            "---\n"
            "## Description\nSome work.\n"
        )
        queue_dir = tio.tickets_dir / "board" / "queue"
        queue_dir.mkdir(parents=True, exist_ok=True)
        (queue_dir / "add-thing.md").write_text(content, encoding="utf-8")

        result = tio.enqueue_ticket(
            "add-thing", "A completely different summary here", "feature", "master"
        )
        # File exists but has no 'created' stamp -> re-enqueue allowed, stamps it
        assert result is True
        # Verify frontmatter was stamped (created is a spec field)
        fields, _ = parse_frontmatter((queue_dir / "add-thing.md").read_text(encoding="utf-8"))
        assert fields.get("created") is not None
        # last_update is a runtime field, now in progress.json
        progress = load_progress(tio.logs_dir, "add-thing")
        assert progress is not None
        assert progress.get("last_update") not in (None, "")

    def test_enqueue_transition_log(self, tmp_path):
        """Verify transition log creation via append_transition (used by enqueue)."""
        tio = make_tio(tmp_path)
        tio.append_transition("add-thing", "---", "queued:init", "ticket-create", "ticket created")
        log_file = human_log_file(tio.logs_dir, "add-thing", "transitions.log")
        assert log_file.exists()
        content = log_file.read_text()
        assert "queued:init" in content


# ===========================================================================
# 6. CLI integration tests
# ===========================================================================


class TestCLI:
    """Test CLI subcommands via main(argv=[...])."""

    def _patch_tickets_dir(self, tmp_path):
        """Create a tickets dir with subdirs and return the path."""
        tickets_dir = tmp_path / "tickets"
        for d in ["drafts", "queue", "waiting", "active", "blocked", "review", "done", "archived"]:
            (tickets_dir / d).mkdir(parents=True, exist_ok=True)
        (tickets_dir / "logs").mkdir(parents=True, exist_ok=True)
        return tickets_dir

    def test_slug(self, tmp_path, capsys):
        tickets_dir = self._patch_tickets_dir(tmp_path)
        with patch("booley.ticket_board.cli.detect_tickets_dir", return_value=tickets_dir):
            rc = main(argv=["slug", "Hello World Test"])
        assert rc == 0
        assert capsys.readouterr().out.strip() == "hello-world-test"

    def test_next_stage(self, tmp_path, capsys):
        tickets_dir = self._patch_tickets_dir(tmp_path)
        with patch("booley.ticket_board.cli.detect_tickets_dir", return_value=tickets_dir):
            rc = main(argv=["next-step", "feature", "planning"])
        assert rc == 0
        assert capsys.readouterr().out.strip() == "run-config"

    def test_next_stage_at_end(self, tmp_path, capsys):
        tickets_dir = self._patch_tickets_dir(tmp_path)
        with patch("booley.ticket_board.cli.detect_tickets_dir", return_value=tickets_dir):
            rc = main(argv=["next-step", "feature", "review"])
        assert rc == 0
        assert capsys.readouterr().out.strip() == "done"

    def test_stages(self, tmp_path, capsys):
        tickets_dir = self._patch_tickets_dir(tmp_path)
        with patch("booley.ticket_board.cli.detect_tickets_dir", return_value=tickets_dir):
            rc = main(argv=["steps", "bugfix"])
        assert rc == 0
        stages = json.loads(capsys.readouterr().out)
        assert stages == STEP_ORDER

    def test_mutation_config(self, tmp_path, capsys):
        tickets_dir = self._patch_tickets_dir(tmp_path)
        with patch("booley.ticket_board.cli.detect_tickets_dir", return_value=tickets_dir):
            rc = main(argv=["mutation-config", "config_a", "config_d/variant"])
        assert rc == 0
        assert capsys.readouterr().out.strip() == "config_a"

    def test_classify_empty(self, tmp_path, capsys):
        tickets_dir = self._patch_tickets_dir(tmp_path)
        with patch("booley.ticket_board.cli.detect_tickets_dir", return_value=tickets_dir):
            rc = main(argv=["classify"])
        assert rc == 0
        result = json.loads(capsys.readouterr().out)
        assert result == {
            "executable": [],
            "active": [],
            "blocked": [],
            "waiting": [],
            "review": [],
            "orphaned": [],
        }

    def test_parse_ticket(self, tmp_path, capsys):
        tickets_dir = self._patch_tickets_dir(tmp_path)
        ticket_file = tmp_path / "test.md"
        ticket_file.write_text(
            "---\nsummary: Test\ntype: bugfix\n---\n## Description\nHello\n",
            encoding="utf-8",
        )
        with patch("booley.ticket_board.cli.detect_tickets_dir", return_value=tickets_dir):
            rc = main(argv=["parse-ticket", str(ticket_file)])
        assert rc == 0
        result = json.loads(capsys.readouterr().out)
        assert result["fields"]["summary"] == "Test"

    def test_validate_ticket_invalid(self, tmp_path, capsys):
        tickets_dir = self._patch_tickets_dir(tmp_path)
        ticket_file = tmp_path / "bad.md"
        ticket_file.write_text("---\nsummary: Bad\n---\nNo desc section\n", encoding="utf-8")
        with (
            patch("booley.ticket_board.cli.detect_tickets_dir", return_value=tickets_dir),
            patch("booley.ticket_board.cli_handlers.detect_project_root", return_value=tmp_path),
        ):
            rc = main(argv=["validate-ticket", str(ticket_file)])
        assert rc == 1
        result = json.loads(capsys.readouterr().out)
        assert len(result["errors"]) > 0


# ===========================================================================
# 7. Edge cases
# ===========================================================================


class TestEdgeCases:
    def test_unicode_in_summary_slug(self):
        slug = generate_slug("R\u00e9sum\u00e9 du projet")
        assert slug  # not empty
        assert all(c.isascii() for c in slug)

    def test_ticket_found_by_stem(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_file(tio, "queue", "mystery")
        # Should find by file stem
        t = tio.find_ticket("mystery")
        assert t is not None

    def test_very_long_summary_truncation(self):
        long_summary = "implement the extremely complex and extraordinarily long feature for the high-performance signal processing module"
        slug = generate_slug(long_summary)
        assert len(slug) <= 40
        assert not slug.endswith("-")


# ============================================================================
# Token Usage Tests
# ============================================================================


def _make_jsonl_transcript(path, messages):
    """Write a list of message dicts as JSONL transcript entries."""
    with Path(path).open("w", encoding="utf-8") as f:
        for msg in messages:
            entry = {
                "type": "assistant",
                "timestamp": msg.get("timestamp", "2026-03-16T10:00:00Z"),
                "message": {
                    "model": msg.get("model", "claude-sonnet-4-6"),
                    "usage": {
                        "input_tokens": msg.get("input", 100),
                        "output_tokens": msg.get("output", 50),
                        "cache_read_input_tokens": msg.get("cache_read", 1000),
                        "cache_creation_input_tokens": msg.get("cache_create", 200),
                    },
                },
            }
            f.write(json.dumps(entry) + "\n")


def _make_transitions_log(path, transitions):
    """Write transitions.log from list of (timestamp, from_state, to_state) tuples."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for ts, fr, to in transitions:
            f.write(f"{ts} | {fr} \u2192 {to} | ticket-execute | step complete\n")


class TestParseTranscriptUsage:
    def test_extracts_assistant_messages(self, tmp_path):
        transcript = tmp_path / "session.jsonl"
        _make_jsonl_transcript(
            transcript,
            [
                {
                    "input": 10,
                    "output": 20,
                    "cache_read": 100,
                    "cache_create": 30,
                    "model": "claude-opus-4-6",
                    "timestamp": "2026-03-16T10:00:00Z",
                },
                {
                    "input": 5,
                    "output": 15,
                    "cache_read": 200,
                    "cache_create": 10,
                    "model": "claude-sonnet-4-6",
                    "timestamp": "2026-03-16T10:01:00Z",
                },
            ],
        )
        msgs = parse_transcript_usage(transcript)
        assert len(msgs) == 2
        assert msgs[0]["model"] == "claude-opus-4-6"
        assert msgs[0]["input_tokens"] == 140
        assert msgs[0]["output_tokens"] == 20
        assert msgs[1]["cache_read_tokens"] == 200

    def test_skips_non_assistant_entries(self, tmp_path):
        transcript = tmp_path / "session.jsonl"
        with transcript.open("w") as f:
            f.write(json.dumps({"type": "user", "message": {"content": "hello"}}) + "\n")
            f.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "timestamp": "T1",
                        "message": {
                            "model": "claude-sonnet-4-6",
                            "usage": {
                                "input_tokens": 5,
                                "output_tokens": 10,
                                "cache_read_input_tokens": 0,
                                "cache_creation_input_tokens": 0,
                            },
                        },
                    }
                )
                + "\n"
            )
        msgs = parse_transcript_usage(transcript)
        assert len(msgs) == 1

    def test_empty_file(self, tmp_path):
        transcript = tmp_path / "empty.jsonl"
        transcript.write_text("")
        assert parse_transcript_usage(transcript) == []

    def test_nonexistent_file(self, tmp_path):
        assert parse_transcript_usage(tmp_path / "nope.jsonl") == []

    def test_extracts_codex_turn_completed_usage(self, tmp_path):
        transcript = tmp_path / "codex.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "type": "turn.completed",
                    "timestamp": "2026-03-16T10:02:00Z",
                    "model": "gpt-5.4",
                    "usage": {
                        "input_tokens": 123,
                        "output_tokens": 45,
                        "cached_input_tokens": 67,
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        msgs = parse_transcript_usage(transcript)
        assert len(msgs) == 1
        assert msgs[0]["model"] == "gpt-5.4"
        assert msgs[0]["input_tokens"] == 123
        assert msgs[0]["cache_read_tokens"] == 67

    def test_extracts_stage_transcript_usage_entries(self, tmp_path):
        transcript = tmp_path / "stage.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "timestamp": "2026-03-16T10:02:00Z",
                    "usage": {
                        "input_tokens": 50,
                        "output_tokens": 25,
                        "cache_read_input_tokens": 10,
                    },
                    "content": [],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        msgs = parse_transcript_usage(transcript)
        assert len(msgs) == 1
        assert msgs[0]["input_tokens"] == 60
        assert msgs[0]["cache_read_tokens"] == 10


class TestUsageLog:
    def test_parse_usage_log(self, tmp_path):
        usage = tmp_path / "usage.jsonl"
        usage.write_text(
            json.dumps(
                {
                    "timestamp": "2026-03-16T10:03:00Z",
                    "stage": "planning",
                    "label": "draft",
                    "model": "claude-sonnet-4-6",
                    "input_tokens": 1000,
                    "output_tokens": 200,
                    "cached_tokens": 800,
                    "cost_usd": 0.42,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        entries = parse_usage_log(usage)
        assert len(entries) == 1
        assert entries[0]["stage"] == "planning"
        assert entries[0]["cache_read_tokens"] == 800
        assert entries[0]["cost_usd"] == pytest.approx(0.42)

    def test_collect_stage_usage_and_aggregate(self, tmp_path):
        logs_dir = tmp_path / "logs"
        usage_dir = _test_ensure_step_dir(logs_dir, "slug", "planning")
        (usage_dir / "usage.jsonl").write_text(
            json.dumps(
                {
                    "timestamp": "2026-03-16T10:03:00Z",
                    "stage": "planning",
                    "label": "draft",
                    "model": "claude-sonnet-4-6",
                    "input_tokens": 1000,
                    "output_tokens": 200,
                    "cached_tokens": 300,
                    "cost_usd": 0.12,
                }
            )
            + "\n",
            encoding="utf-8",
        )
        entries = collect_step_usage(logs_dir, "slug")
        step_data = usage_entries_to_steps(entries)
        assert len(entries) == 1
        assert step_data["planning"]["input_tokens"] == 1000
        assert step_data["planning"]["output_tokens"] == 200
        assert step_data["planning"]["cost_usd"] == pytest.approx(0.12)


class TestParseTransitionsLog:
    def test_basic_parsing(self, tmp_path):
        log = tmp_path / "transitions.log"
        _make_transitions_log(
            log,
            [
                ("2026-03-16T10:00:00Z", "queued:init", "running:setup"),
                ("2026-03-16T10:01:00Z", "running:setup", "running:planning"),
            ],
        )
        transitions = parse_transitions_log(log)
        assert len(transitions) == 2
        assert transitions[0]["stage"] == "setup"
        assert transitions[1]["stage"] == "planning"

    def test_empty_file(self, tmp_path):
        log = tmp_path / "transitions.log"
        log.write_text("")
        assert parse_transitions_log(log) == []

    def test_nonexistent(self, tmp_path):
        assert parse_transitions_log(tmp_path / "nope.log") == []


class TestAttributeTokensToStages:
    def test_no_transitions_puts_all_in_unknown(self):
        msgs = [
            {
                "timestamp": "T1",
                "model": "m",
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_tokens": 100,
                "cache_create_tokens": 20,
            },
        ]
        result = attribute_tokens_to_steps(msgs, [])
        assert "unknown" in result
        assert result["unknown"]["input_tokens"] == 10
        assert result["unknown"]["message_count"] == 1

    def test_attributes_to_correct_stage(self):
        transitions = [
            {
                "timestamp": "2026-03-16T10:00:00Z",
                "to_state": "running:planning",
                "stage": "planning",
            },
            {
                "timestamp": "2026-03-16T10:05:00Z",
                "to_state": "running:implementation",
                "stage": "implementation",
            },
        ]
        msgs = [
            {
                "timestamp": "2026-03-16T10:02:00Z",
                "model": "m",
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_read_tokens": 0,
                "cache_create_tokens": 0,
            },
            {
                "timestamp": "2026-03-16T10:06:00Z",
                "model": "m",
                "input_tokens": 20,
                "output_tokens": 15,
                "cache_read_tokens": 0,
                "cache_create_tokens": 0,
            },
        ]
        result = attribute_tokens_to_steps(msgs, transitions)
        assert result["planning"]["input_tokens"] == 10
        assert result["implementation"]["input_tokens"] == 20

    def test_empty_messages(self):
        assert attribute_tokens_to_steps([], []) == {}


class TestCostCalculation:
    def test_compute_cost_detailed_opus(self):
        msgs = [
            {
                "model": "claude-opus-4-6",
                "input_tokens": 1_000_000,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_create_tokens": 0,
            }
        ]
        cost = compute_cost_detailed(msgs)
        assert cost == pytest.approx(5.0)  # $5/M input for opus 4.6

    def test_compute_cost_detailed_sonnet(self):
        msgs = [
            {
                "model": "claude-sonnet-4-6",
                "input_tokens": 0,
                "output_tokens": 1_000_000,
                "cache_read_tokens": 0,
                "cache_create_tokens": 0,
            }
        ]
        cost = compute_cost_detailed(msgs)
        assert cost == pytest.approx(15.0)  # $15/M output for sonnet

    def test_compute_cost_detailed_mixed(self):
        msgs = [
            {
                "model": "claude-opus-4-6",
                "input_tokens": 100,
                "output_tokens": 100,
                "cache_read_tokens": 0,
                "cache_create_tokens": 0,
            },
            {
                "model": "claude-sonnet-4-6",
                "input_tokens": 100,
                "output_tokens": 100,
                "cache_read_tokens": 0,
                "cache_create_tokens": 0,
            },
        ]
        cost = compute_cost_detailed(msgs)
        # opus 4.6: 100*5/1M + 100*25/1M = 0.0005 + 0.0025 = 0.003
        # sonnet 4.6: 100*3/1M + 100*15/1M = 0.0003 + 0.0015 = 0.0018
        assert cost == pytest.approx(0.0048)

    def test_match_pricing_partial_model_id(self):
        assert _match_pricing("claude-opus-4-6-20250901")["input"] == 5.0
        # sonnet-4-5 is not in the table; the "sonnet" family fallback covers it.
        assert _match_pricing("claude-sonnet-4-5-20250929")["input"] == 3.0
        # An unrecognised Opus falls back to the priciest Anthropic tier, so an
        # unknown model is over-costed rather than reported as nearly free.
        assert _match_pricing("some-opus-variant")["input"] == 10.0

    def test_match_pricing_never_returns_none(self):
        """An unpriceable model still gets DEFAULT_PRICING, not a zero-cost run."""
        from booley.ticket_board.analytics import DEFAULT_PRICING

        assert _match_pricing("some-vendor-model-9000") == DEFAULT_PRICING

    def test_cache_create_bills_above_plain_input(self):
        """Cache writes carry a 1.25x premium -- the harness used to bill them at 1x."""
        written = compute_cost_detailed(
            [
                {
                    "model": "claude-opus-4-8",
                    "input_tokens": 1_000_000,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_create_tokens": 1_000_000,
                }
            ]
        )
        assert written == pytest.approx(6.25)


class TestCollectAllMessages:
    def test_includes_subagent_transcripts(self, tmp_path):
        # Main transcript
        main = tmp_path / "session.jsonl"
        _make_jsonl_transcript(
            main,
            [
                {
                    "input": 10,
                    "output": 5,
                    "cache_read": 0,
                    "cache_create": 0,
                    "timestamp": "2026-03-16T10:00:00Z",
                },
            ],
        )
        # Subagent dir
        sa_dir = tmp_path / "session" / "subagents"
        sa_dir.mkdir(parents=True)
        _make_jsonl_transcript(
            sa_dir / "agent-abc123.jsonl",
            [
                {
                    "input": 20,
                    "output": 15,
                    "cache_read": 0,
                    "cache_create": 0,
                    "timestamp": "2026-03-16T10:01:00Z",
                },
            ],
        )
        msgs = collect_all_messages(main)
        assert len(msgs) == 2
        # Should be sorted by timestamp
        assert msgs[0]["input_tokens"] == 10
        assert msgs[1]["input_tokens"] == 20

    def test_no_subagents(self, tmp_path):
        main = tmp_path / "session.jsonl"
        _make_jsonl_transcript(main, [{"input": 10, "output": 5}])
        msgs = collect_all_messages(main)
        assert len(msgs) == 1

    def test_collect_stage_transcript_usage_uses_stage_directory(self, tmp_path):
        logs_dir = tmp_path / "logs"
        transcript_dir = _test_ensure_step_dir(logs_dir, "slug", "planning") / "transcripts"
        transcript_dir.mkdir(parents=True)
        _make_jsonl_transcript(
            transcript_dir / "draft.jsonl",
            [
                {
                    "input": 10,
                    "output": 5,
                    "cache_read": 0,
                    "cache_create": 0,
                    "timestamp": "2026-03-16T10:00:00Z",
                },
            ],
        )
        data = collect_step_transcript_usage(logs_dir, "slug")
        assert set(data) == {"planning"}
        assert data["planning"]["input_tokens"] == 10
        assert data["planning"]["output_tokens"] == 5


class TestFormatUsageReport:
    def test_basic_format(self):
        step_data = {
            "planning": {
                "input_tokens": 100,
                "output_tokens": 200,
                "cache_read_tokens": 1000,
                "cache_create_tokens": 50,
                "message_count": 3,
                "messages": [
                    {
                        "model": "claude-sonnet-4-6",
                        "input_tokens": 100,
                        "output_tokens": 200,
                        "cache_read_tokens": 1000,
                        "cache_create_tokens": 50,
                    }
                ],
            }
        }
        report = format_usage_report(step_data, 0.01, "Test Report")
        assert "# Test Report" in report
        assert "planning" in report
        assert "**Total**" in report
        assert "300 total tokens" in report  # input already includes cached tokens


# ============================================================================
# Triage Operation Tests
# ============================================================================


class TestOpUnblock:
    def test_moves_to_queue_and_clears_fields(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(
            tio,
            "blocked",
            "my-ticket",
            extra_fields={
                "blocked_reason": "need answer",
                "blocked_step": "planning",
            },
        )
        op_unblock(tio, "my-ticket")

        path, status = find_ticket_file(tio.tickets_dir, "my-ticket")
        assert status == "queued"
        fields, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert "blocked_reason" not in fields
        assert "blocked_step" not in fields
        assert human_log_file(tio.logs_dir, "my-ticket", "transitions.log").exists()
        progress = load_progress(tio.logs_dir, "my-ticket")
        assert progress["workspace_intent"] == "resume"


class TestOpApprove:
    def test_moves_to_done_complete(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "review", "my-ticket")
        make_progress(tio, "my-ticket", {"step": "summary"})
        op_approve(tio, "my-ticket")

        _path, status = find_ticket_file(tio.tickets_dir, "my-ticket")
        assert status == "done"
        progress = load_progress(tio.logs_dir, "my-ticket")
        assert progress["step"] == "complete"


class TestOpPromoteWaiting:
    def test_promotes_when_deps_done(self, tmp_path):
        """Waiting ticket moves to queue/ when all deps are done."""
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "done", "dep-a", extra_fields={"summary": "dep a"})
        make_ticket_in_dir(
            tio, "waiting", "child", extra_fields={"dependencies": ["dep-a"], "summary": "child"}
        )
        promoted = op_promote_waiting(tio)
        assert len(promoted) == 1
        assert promoted[0]["slug"] == "child"
        # File should now be in queue/
        _path, status = find_ticket_file(tio.tickets_dir, "child")
        assert status == "queued"

    def test_stays_waiting_when_deps_unmet(self, tmp_path):
        """Waiting ticket stays in waiting/ when deps are not done."""
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "active", "dep-a", extra_fields={"summary": "dep a"})
        make_ticket_in_dir(
            tio, "waiting", "child", extra_fields={"dependencies": ["dep-a"], "summary": "child"}
        )
        promoted = op_promote_waiting(tio)
        assert len(promoted) == 0
        _path, status = find_ticket_file(tio.tickets_dir, "child")
        assert status == "waiting"

    def test_no_waiting_tickets(self, tmp_path):
        """Returns empty list when no waiting tickets exist."""
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "queue", "ready", extra_fields={"summary": "ready"})
        promoted = op_promote_waiting(tio)
        assert len(promoted) == 0


# ============================================================================
# Harness-driven transition enforcement
# ============================================================================


class TestHarnessTransitionGuard:
    """Normal operations validate the locked source state before mutation."""

    def test_legal_op_block_logs_no_warning(self, tmp_path, caplog):
        """A normal running->blocked op_block emits no transition warning."""
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "active", "t1")
        make_progress(tio, "t1", {"step": "planning"})
        with caplog.at_level("WARNING", logger="booley.ticket_board.operations"):
            assert op_block(tio, "t1", "need spec", "planning") is True
        assert not [r for r in caplog.records if "illegal ticket transition" in r.getMessage()]

    def test_illegal_source_is_rejected_before_progress_update(self, tmp_path, capsys):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "blocked", "t1")
        make_progress(tio, "t1", {"step": "planning"})
        assert op_block(tio, "t1", "still stuck", "planning") is False
        assert "illegal ticket transition blocked -> blocked" in capsys.readouterr().err
        _path, status = find_ticket_file(tio.tickets_dir, "t1")
        assert status == "blocked"
        assert load_progress(tio.logs_dir, "t1")["blocked_reason"] is None

    def test_log_uses_locked_source_status(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "active", "t1")
        make_progress(tio, "t1", {"step": "planning"})

        assert tio.move_and_update(
            "t1",
            "blocked",
            {},
            transition=("queued:planning", "blocked:planning", "test", "blocked"),
            enforce_lifecycle=True,
        )

        transition = human_log_file(tio.logs_dir, "t1", "transitions.log").read_text()
        assert "running:planning -> blocked:planning" in transition


# ============================================================================
# Reset tests
# ============================================================================


class TestOpReset:
    """Test op_reset: move to queue, clear state, wipe logs."""

    def test_reset_from_failed(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "archived", "my-ticket")
        make_progress(
            tio,
            "my-ticket",
            {
                "error": "boom",
                "failed_step": "sim-debug-loop",
                "step": "sim-debug-loop",
                "steps_completed": ["planning", "implementation"],
            },
        )
        # Create some log files in stage dirs
        write_stage_file(tio.logs_dir, "my-ticket", "planning", "plan.md", "old plan")

        ok = op_reset(tio, "my-ticket")
        assert ok is True

        _path, status = find_ticket_file(tio.tickets_dir, "my-ticket")
        assert status == "queued"
        # op_reset wipes logs then calls reset_progress -> PROGRESS_DEFAULTS
        progress = load_progress(tio.logs_dir, "my-ticket")
        assert progress["step"] == ""  # reset to default
        assert progress["steps_completed"] == []
        assert progress.get("error") is None
        assert progress.get("failed_step") is None
        assert progress["workspace_intent"] == "fresh"

    def test_reset_force_deletes_the_ticket_branch(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "blocked", "my-ticket")

        with patch(
            "booley.ticket_board.operations.cleanup_worktree_and_branch",
            return_value=True,
        ) as cleanup:
            assert op_reset(tio, "my-ticket") is True

        cleanup.assert_called_once_with("my-ticket", force=True)

    def test_reset_does_not_queue_when_branch_cleanup_fails(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "blocked", "my-ticket")

        with patch(
            "booley.ticket_board.operations.cleanup_worktree_and_branch",
            return_value=False,
        ):
            assert op_reset(tio, "my-ticket") is False

        _path, status = find_ticket_file(tio.tickets_dir, "my-ticket")
        assert status == "blocked"

    def test_reset_refuses_while_a_live_process_owns_the_ticket(self, tmp_path):
        """F-28: reset queued the ticket, and the still-running orchestrator
        immediately re-selected it — while the wipe pulled its runtime state
        out from under it. Refuse until the run is stopped."""
        import os

        from booley.ticket_board.io import migrate_runtime_file
        from booley.ticket_board.logs import ticket_log_dir

        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "active", "my-ticket")
        make_progress(tio, "my-ticket", {"step": "implementation"})

        # A live PID that is not us: the current process's parent is alive and
        # its PID is stable for the test's duration.
        log_dir = ticket_log_dir(tio.logs_dir, "my-ticket")
        lock_path = migrate_runtime_file(log_dir, "ticket.lock")
        lock_path.write_text(str(os.getppid()), encoding="utf-8")

        assert op_reset(tio, "my-ticket") is False
        # Untouched: not queued, progress intact.
        _path, status = find_ticket_file(tio.tickets_dir, "my-ticket")
        assert status == "running"
        assert load_progress(tio.logs_dir, "my-ticket")["step"] == "implementation"

    def test_reset_force_overrides_the_live_owner_guard(self, tmp_path):
        import os

        from booley.ticket_board.io import migrate_runtime_file
        from booley.ticket_board.logs import ticket_log_dir

        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "active", "my-ticket")
        make_progress(tio, "my-ticket", {"step": "implementation"})
        lock_path = migrate_runtime_file(ticket_log_dir(tio.logs_dir, "my-ticket"), "ticket.lock")
        lock_path.write_text(str(os.getppid()), encoding="utf-8")

        assert op_reset(tio, "my-ticket", force=True) is True
        _path, status = find_ticket_file(tio.tickets_dir, "my-ticket")
        assert status == "queued"

    def test_reset_force_refuses_while_a_detached_job_is_active(self, tmp_path):
        """Force cannot archive the runtime directory beneath an endpoint job."""
        from types import SimpleNamespace

        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "active", "my-ticket")
        make_progress(tio, "my-ticket", {"step": "implementation"})
        active = SimpleNamespace(endpoint="mutation_tester", run_id="mutation-47")

        with patch("booley.harness.job_fence.active_ticket_jobs", return_value=[active]):
            assert op_reset(tio, "my-ticket", force=True) is False

        _path, status = find_ticket_file(tio.tickets_dir, "my-ticket")
        assert status == "running"
        assert load_progress(tio.logs_dir, "my-ticket")["step"] == "implementation"

    def test_reset_rechecks_detached_jobs_after_acquiring_ticket_lock(self, tmp_path):
        """A job appearing while reset waits for the ticket lock must win."""
        from contextlib import contextmanager
        from types import SimpleNamespace

        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "active", "my-ticket")
        make_progress(tio, "my-ticket", {"step": "implementation"})
        events: list[str] = []

        @contextmanager
        def observed_lock(_slug):
            events.append("locked")
            yield
            events.append("unlocked")

        def active_jobs(_log_dir):
            assert events == ["locked"]
            return [SimpleNamespace(endpoint="mutation_tester", run_id="mutation-48")]

        tio._ticket_lock = observed_lock
        with patch("booley.harness.job_fence.active_ticket_jobs", active_jobs):
            assert op_reset(tio, "my-ticket", force=True) is False

        assert events == ["locked", "unlocked"]
        _path, status = find_ticket_file(tio.tickets_dir, "my-ticket")
        assert status == "running"

    def test_reset_proceeds_when_the_recorded_pid_is_dead(self, tmp_path, monkeypatch):
        """A crashed run leaves a stale lock; that must not block recovery."""
        from booley.ticket_board import helpers
        from booley.ticket_board.io import migrate_runtime_file
        from booley.ticket_board.logs import ticket_log_dir

        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "active", "my-ticket")
        make_progress(tio, "my-ticket", {"step": "implementation"})
        lock_path = migrate_runtime_file(ticket_log_dir(tio.logs_dir, "my-ticket"), "ticket.lock")
        lock_path.write_text("424242", encoding="utf-8")
        monkeypatch.setattr(helpers, "is_pid_alive", lambda pid: False)

        assert op_reset(tio, "my-ticket") is True
        _path, status = find_ticket_file(tio.tickets_dir, "my-ticket")
        assert status == "queued"

    def test_reset_clears_transient_fields(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "blocked", "my-ticket")
        make_progress(
            tio,
            "my-ticket",
            {
                "blocked_reason": "need info",
                "blocked_step": "planning",
                "steps_completed": ["planning"],
            },
        )

        op_reset(tio, "my-ticket")

        progress = load_progress(tio.logs_dir, "my-ticket")
        for key in ("blocked_reason", "blocked_step"):
            assert progress.get(key) is None, f"{key} should have been cleared"
        assert progress["steps_completed"] == []

    def test_reset_wipes_logs(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "active", "my-ticket")
        log_dir = tio.logs_dir / "my-ticket"
        log_dir.mkdir(parents=True, exist_ok=True)
        write_stage_file(tio.logs_dir, "my-ticket", "planning", "plan.md", "old plan")
        write_stage_file(tio.logs_dir, "my-ticket", "summary", "summary.md", "old summary")

        op_reset(tio, "my-ticket")

        # Log dir should exist but be empty except transitions.log,
        # progress.json, ticket.lock, and runs/ (archived artifacts).
        assert log_dir.exists()
        allowed = {".runtime", "human-logs", "runs"}
        remaining = [f.name for f in log_dir.iterdir() if f.name not in allowed]
        assert remaining == [], f"Expected empty log dir, found: {remaining}"

        # Archived artifacts should be in runs/001/
        runs_dir = log_dir / "runs" / "001"
        assert runs_dir.exists()
        archived = {f.name for f in runs_dir.rglob("*") if f.is_file()}
        assert "plan.md" in archived
        assert "summary.md" in archived

    def test_reset_cleanup_failure_does_not_publish_queued_state(
        self, tmp_path, monkeypatch, capsys
    ):
        """A failed wipe must not produce a queued ticket with stale run evidence."""
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "blocked", "my-ticket")
        make_progress(tio, "my-ticket", {"step": "developer"})
        state_path = runtime_file(tio.logs_dir, "my-ticket", "booley_state.json")
        state_path.write_text(
            json.dumps(
                {
                    "criteria": {"sim_pass": {"met": True}},
                    "timeline": [{"agent": "developer_agent"}],
                }
            ),
            encoding="utf-8",
        )

        def fail_before_wipe(*_args, **_kwargs):
            raise OSError("injected reset cleanup failure")

        monkeypatch.setattr("booley.ticket_board.operations._wipe_log_dir", fail_before_wipe)

        assert op_reset(tio, "my-ticket") is False

        _path, status = find_ticket_file(tio.tickets_dir, "my-ticket")
        assert status == "blocked"
        assert state_path.exists()
        assert load_progress(tio.logs_dir, "my-ticket")["step"] == "developer"
        assert "Ticket was not moved to queue" in capsys.readouterr().err

    def test_reset_clears_board_runtime_summary(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "blocked", "my-ticket")
        make_progress(tio, "my-ticket", {"step": "developer", "last_update": "old"})
        state_path = runtime_file(tio.logs_dir, "my-ticket", "booley_state.json")
        state_path.write_text(
            json.dumps(
                {
                    "criteria": {
                        "lint_clean": {"met": True},
                        "sim_pass": {"met": False},
                    },
                    "timeline": [
                        {"flow": "lint", "timestamp": "old"},
                        {"agent": "developer_agent", "timestamp": "old"},
                    ],
                }
            ),
            encoding="utf-8",
        )

        # Board output is intentionally copy/pasteable as ``<slug>.md``. The
        # reset command must resolve that display name back to the canonical
        # runtime slug before removing state.
        assert op_reset(tio, "my-ticket.md") is True

        [entry] = scan_all_tickets(tio.tickets_dir)
        assert entry["status"] == "queued"
        assert entry["step"] == ""
        assert entry["steps_completed"] == []
        assert entry["last_update"] == ""
        assert "criteria_passed" not in entry
        assert "criteria_total" not in entry
        assert not (tio.logs_dir / "my-ticket.md").exists()

    def test_reset_preserves_core_fields(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(
            tio,
            "archived",
            "my-ticket",
            extra_fields={"summary": "important work", "error": "kaboom", "failed_step": "sim"},
        )

        op_reset(tio, "my-ticket")

        path, _ = find_ticket_file(tio.tickets_dir, "my-ticket")
        fields, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert fields["summary"] == "important work"
        assert fields["branch"] == "master"  # from make_ticket_in_dir defaults

    def test_reset_nonexistent(self, tmp_path):
        tio = make_tio(tmp_path)
        assert op_reset(tio, "nonexistent") is False

    def test_reset_logs_transition(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "archived", "my-ticket", extra_fields={"step": "sim-debug-loop"})

        op_reset(tio, "my-ticket")

        # Should have a transition logged
        trans_file = human_log_file(tio.logs_dir, "my-ticket", "transitions.log")
        assert trans_file.exists()
        content = trans_file.read_text(encoding="utf-8")
        assert "queued:reset" in content
        assert "user reset ticket" in content

    def test_reset_logs_the_correction_reason(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "review", "my-ticket", extra_fields={"step": "summary"})

        assert op_reset(tio, "my-ticket", reason="review found an invalid protocol fix")

        trans_file = human_log_file(tio.logs_dir, "my-ticket", "transitions.log")
        assert "review found an invalid protocol fix" in trans_file.read_text(encoding="utf-8")


# ============================================================================
# MoveAndUpdate tests
# ============================================================================


class TestMoveAndUpdate:
    """Verify move_and_update does atomic move + field update."""

    def test_moves_and_updates_atomically(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "active", "t1")
        make_progress(tio, "t1", {"step": "planning"})

        tio.move_and_update(
            "t1",
            "blocked",
            {
                "blocked_reason": "need info",
            },
        )

        _path, status = find_ticket_file(tio.tickets_dir, "t1")
        assert status == "blocked"
        progress = load_progress(tio.logs_dir, "t1")
        assert progress["blocked_reason"] == "need info"
        assert (tio.tickets_dir / "board" / "blocked" / "t1.md").exists()
        assert not (tio.tickets_dir / "board" / "active" / "t1.md").exists()

    def test_clears_fields_with_none(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "blocked", "t1", extra_fields={"blocked_reason": "stuff"})

        tio.move_and_update(
            "t1",
            "queue",
            {
                "blocked_reason": None,
            },
        )

        path, _ = find_ticket_file(tio.tickets_dir, "t1")
        fields, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert "blocked_reason" not in fields

    def test_append_stage(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "active", "t1")
        make_progress(tio, "t1", {"steps_completed": ["setup"]})

        tio.move_and_update("t1", "active", {}, append_step="planning")

        progress = load_progress(tio.logs_dir, "t1")
        assert "planning" in progress["steps_completed"]

    def test_not_found(self, tmp_path):
        tio = make_tio(tmp_path)
        assert tio.move_and_update("nope", "queue", {}) is False

    def test_expected_status_rejects_concurrent_state_change(self, tmp_path, capsys):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "active", "t1")
        make_progress(tio, "t1", {"step": "planning"})

        assert (
            tio.move_and_update(
                "t1",
                "blocked",
                {"blocked_reason": "stale writer"},
                expected_status="queued",
            )
            is False
        )

        _path, status = find_ticket_file(tio.tickets_dir, "t1")
        assert status == "running"
        assert load_progress(tio.logs_dir, "t1")["blocked_reason"] is None
        assert "changed concurrently" in capsys.readouterr().err

    def test_expected_execution_rejects_running_aba(self, tmp_path, capsys):
        from booley.ticket_board.operations import op_activate, op_requeue

        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "active", "t1")
        make_progress(
            tio,
            "t1",
            {"step": "planning", "execution_id": "old-run"},
        )

        assert op_requeue(tio, "t1")
        assert op_activate(tio, "t1", owner_pid=123, execution_id="new-run")
        assert not op_block(
            tio,
            "t1",
            "stale writer",
            "planning",
            expected_execution_id="old-run",
        )

        _path, status = find_ticket_file(tio.tickets_dir, "t1")
        assert status == "running"
        assert load_progress(tio.logs_dir, "t1")["execution_id"] == "new-run"
        assert "execution changed concurrently" in capsys.readouterr().err

    def test_requeue_rejects_review_ticket(self, tmp_path, capsys):
        from booley.ticket_board.operations import op_requeue

        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "review", "t1")
        make_progress(tio, "t1", {"step": "summary"})

        assert not op_requeue(tio, "t1")

        _path, status = find_ticket_file(tio.tickets_dir, "t1")
        assert status == "review"
        assert "illegal ticket transition review -> queued" in capsys.readouterr().err

    def test_requeue_refuses_while_a_live_process_owns_the_ticket(self, tmp_path):
        import os

        from booley.ticket_board.io import migrate_runtime_file
        from booley.ticket_board.logs import ticket_log_dir
        from booley.ticket_board.operations import op_requeue

        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "active", "t1")
        make_progress(tio, "t1", {"step": "implementation"})
        lock_path = migrate_runtime_file(ticket_log_dir(tio.logs_dir, "t1"), "ticket.lock")
        lock_path.write_text(str(os.getppid()), encoding="utf-8")

        assert not op_requeue(tio, "t1")

        _path, status = find_ticket_file(tio.tickets_dir, "t1")
        assert status == "running"
        assert load_progress(tio.logs_dir, "t1")["step"] == "implementation"


class TestClassifyWithReview:
    """classify_tickets includes review tickets."""

    def test_review_tickets_classified(self):
        tickets = [
            {"status": "review", "feature_branch": "in-review"},
            {"status": "queued", "feature_branch": "ready", "dependencies": []},
        ]
        result = classify_tickets(tickets)
        assert len(result["review"]) == 1
        assert result["review"][0]["feature_branch"] == "in-review"
        assert len(result["executable"]) == 1


class TestOpArchive:
    """archive removes done tickets from done/ directory."""

    def test_archives_done_tickets(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "done", "t1", extra_fields={"summary": "done ticket"})
        make_ticket_in_dir(tio, "active", "t2", extra_fields={"summary": "active ticket"})
        write_stage_file(tio.logs_dir, "t1", "planning", "plan.md", "plan")

        archived = op_archive(tio)

        assert len(archived) == 1
        assert archived[0] == "done ticket"
        assert not (tio.tickets_dir / "board" / "done" / "t1.md").exists()
        assert not (tio.logs_dir / "t1").exists()
        # active ticket should still exist
        assert (tio.tickets_dir / "board" / "active" / "t2.md").exists()

    def test_keep_logs(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "done", "t1", extra_fields={"summary": "done"})
        write_stage_file(tio.logs_dir, "t1", "planning", "plan.md", "plan")

        op_archive(tio, keep_logs=True)

        assert _test_step_artifact(tio.logs_dir, "t1", "planning", "plan.md").exists()

    def test_nothing_to_archive(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "active", "t1")
        archived = op_archive(tio)
        assert archived == []


class TestValidateCriteriaField:
    """criteria field validation (structured mandatory/optional sections)."""

    def test_mandatory_with_sim_entry_is_valid(self):
        fields = {
            "summary": "x",
            "type": "feature",
            "branch": "m",
            "scope": ["rtl/foo.sv"],
            "criteria": {
                "mandatory": {"sim_pass": ["tb/foo_tb.sv @ default @ all @ pass -> pass"]}
            },
        }
        errors = validate_ticket_fields(fields, "## Description\ntext")
        assert not any("criteria" in e.lower() and "invalid" in e.lower() for e in errors)

    def test_optional_section_accepted(self):
        fields = {
            "summary": "x",
            "type": "feature",
            "branch": "m",
            "scope": ["rtl/foo.sv"],
            "criteria": {
                "mandatory": {"sim_pass": ["tb/foo_tb.sv @ default @ all @ pass -> pass"]},
                "optional": {"lint_clean": True},
            },
        }
        errors = validate_ticket_fields(fields, "## Description\ntext")
        assert not any("criteria" in e.lower() and "invalid" in e.lower() for e in errors)

    def test_unknown_top_level_key_rejected(self):
        fields = {
            "summary": "x",
            "type": "feature",
            "branch": "m",
            "scope": ["rtl/foo.sv"],
            "criteria": {
                "mandatory": {"sim_pass": ["tb/foo_tb.sv @ default @ all @ pass -> pass"]},
                "bogus": {},
            },
        }
        errors = validate_ticket_fields(fields, "## Description\ntext")
        assert any("unknown top-level" in e for e in errors)

    def test_relative_qor_target_pair_is_valid(self):
        fields = {
            "summary": "x",
            "type": "feature",
            "branch": "m",
            "scope": ["rtl/foo.sv"],
            "criteria": {
                "mandatory": {
                    "synthesis_ok": {
                        "targets": [{"baseline": "synth_before", "candidate": "synth_after"}],
                        "area_reduce_at_least": "10%",
                    }
                }
            },
        }

        errors = validate_ticket_fields(fields, "## Description\ntext")

        assert not any("baseline/candidate" in error for error in errors)

    def test_target_pair_without_relative_threshold_is_rejected(self):
        fields = {
            "summary": "x",
            "type": "feature",
            "branch": "m",
            "scope": ["rtl/foo.sv"],
            "criteria": {
                "mandatory": {
                    "synthesis_ok": {
                        "targets": [{"baseline": "synth_before", "candidate": "synth_after"}],
                        "cell_count_max": 500,
                    }
                }
            },
        }

        errors = validate_ticket_fields(fields, "## Description\ntext")

        assert any("require a relative threshold" in error for error in errors)

    def test_malformed_target_pair_is_rejected(self):
        fields = {
            "summary": "x",
            "type": "feature",
            "branch": "m",
            "scope": ["rtl/foo.sv"],
            "criteria": {
                "mandatory": {
                    "synthesis_ok": {
                        "targets": [{"baseline": "synth_before"}],
                        "area_reduce_at_least": "10%",
                    }
                }
            },
        }

        errors = validate_ticket_fields(fields, "## Description\ntext")

        assert any("exactly 'baseline' and 'candidate'" in error for error in errors)


class TestResumeDetectTypeFallback:
    """resume_detect warns on missing/invalid type."""

    def test_warns_on_missing_type(self, capsys):
        entry = {"status": "queued", "feature_branch": "x"}
        r = resume_detect(entry)
        assert r["action"] == "fresh"
        captured = capsys.readouterr()
        assert "Warning" in captured.err

    def test_warns_on_invalid_type(self, capsys):
        entry = {"status": "queued", "type": "invalid_type", "feature_branch": "x"}
        r = resume_detect(entry)
        assert r["action"] == "fresh"
        captured = capsys.readouterr()
        assert "Warning" in captured.err

    def test_no_warning_on_valid_type(self, capsys):
        entry = {"status": "queued", "type": "feature", "feature_branch": "x"}
        resume_detect(entry)
        captured = capsys.readouterr()
        assert "Warning" not in captured.err


class TestCLIUpdateBoardWithLog:
    """Test update-board --log flag."""

    def test_update_board_with_log(self, tmp_path, capsys):
        tickets_dir = tmp_path / "tickets"
        for d in ["drafts", "queue", "waiting", "active", "blocked", "review", "done", "archived"]:
            (tickets_dir / d).mkdir(parents=True, exist_ok=True)
        (tickets_dir / "logs").mkdir(parents=True, exist_ok=True)

        # Create a ticket in active/ with frontmatter + progress.json
        tio = TicketIO(tickets_dir)
        make_ticket_in_dir(tio, "active", "t1")
        make_progress(tio, "t1", {"step": "planning", "steps_completed": ["setup"]})

        # Create required artifacts for stage gate
        write_stage_file(tickets_dir / "logs", "t1", "planning", "plan.md", "# Plan\nDo stuff.")
        _test_save_step_meta(
            tickets_dir / "logs",
            "t1",
            {
                "planning": {"clarifying_questions": 0},
            },
        )
        with patch("booley.ticket_board.cli.detect_tickets_dir", return_value=tickets_dir):
            rc = main(
                argv=[
                    "update-board",
                    "t1",
                    "--set",
                    "step=implementation",
                    "--append-step",
                    "planning",
                    "--log",
                ]
            )
        assert rc == 0
        # Verify transition log was created
        log_file = human_log_file(tickets_dir / "logs", "t1", "transitions.log")
        assert log_file.exists()
        content = log_file.read_text(encoding="utf-8")
        assert "planning" in content
        assert "implementation" in content


class TestCLIMoveTicket:
    """Test move-ticket CLI."""

    def test_move_ticket(self, tmp_path, capsys):
        tickets_dir = tmp_path / "tickets"
        for d in ["drafts", "queue", "waiting", "active", "blocked", "review", "done", "archived"]:
            (tickets_dir / d).mkdir(parents=True, exist_ok=True)
        (tickets_dir / "logs").mkdir(parents=True, exist_ok=True)

        tio = TicketIO(tickets_dir)
        make_ticket_file(tio, "queue", "t1")

        with patch("booley.ticket_board.cli.detect_tickets_dir", return_value=tickets_dir):
            rc = main(argv=["move-ticket", "t1", "--to", "active"])
        assert rc == 0
        assert (tickets_dir / "board" / "active" / "t1.md").exists()

    def test_move_ticket_cannot_bypass_review_reset(self, tmp_path, capsys):
        tickets_dir = tmp_path / "tickets"
        for directory in (
            "drafts",
            "queue",
            "waiting",
            "active",
            "blocked",
            "review",
            "done",
            "archived",
        ):
            (tickets_dir / directory).mkdir(parents=True, exist_ok=True)
        (tickets_dir / "logs").mkdir(parents=True, exist_ok=True)
        tio = TicketIO(tickets_dir)
        make_ticket_file(tio, "review", "t1")

        with patch("booley.ticket_board.cli.detect_tickets_dir", return_value=tickets_dir):
            rc = main(argv=["move-ticket", "t1", "--to", "queue"])

        assert rc == 1
        assert (tickets_dir / "board" / "review" / "t1.md").exists()
        assert "Use 'reset' for a clean run" in capsys.readouterr().err


class TestCLIUsageEndToEnd:
    """Test usage subcommand end-to-end."""

    def test_usage_with_slug(self, tmp_path, capsys):
        tickets_dir = tmp_path / "tickets"
        for d in ["drafts", "queue", "waiting", "active", "blocked", "review", "done", "archived"]:
            (tickets_dir / d).mkdir(parents=True, exist_ok=True)
        (tickets_dir / "logs").mkdir(parents=True, exist_ok=True)

        # Create transcript
        transcript = tmp_path / "session.jsonl"
        _make_jsonl_transcript(
            transcript,
            [
                {
                    "input": 100,
                    "output": 50,
                    "cache_read": 1000,
                    "cache_create": 200,
                    "model": "claude-sonnet-4-6",
                    "timestamp": "2026-03-16T10:00:00Z",
                },
            ],
        )

        # Create transitions.log
        log_dir = tickets_dir / "logs" / "test-slug"
        log_dir.mkdir(parents=True)
        _make_transitions_log(
            log_dir / "transitions.log",
            [
                ("2026-03-16T09:59:00Z", "queued:init", "running:planning"),
            ],
        )

        with patch("booley.ticket_board.cli.detect_tickets_dir", return_value=tickets_dir):
            rc = main(
                argv=[
                    "usage",
                    str(transcript),
                    "--slug",
                    "test-slug",
                ]
            )
        assert rc == 0
        assert not (log_dir / "usage.md").exists()
        output = capsys.readouterr().out
        assert "planning" in output
        assert "Total" in output

    def test_usage_slug_only_auto_discover(self, tmp_path, capsys):
        """When only --slug is given (no positional transcript), discover transcripts from stages/."""
        tickets_dir = tmp_path / "tickets"
        for d in ["drafts", "queue", "waiting", "active", "blocked", "review", "done", "archived"]:
            (tickets_dir / d).mkdir(parents=True, exist_ok=True)
        (tickets_dir / "logs").mkdir(parents=True, exist_ok=True)

        log_dir = tickets_dir / "logs" / "test-slug"
        log_dir.mkdir(parents=True)

        # Create per-stage transcripts
        impl_dir = log_dir / "stages" / "03-implementation" / "transcripts"
        impl_dir.mkdir(parents=True)
        _make_jsonl_transcript(
            impl_dir / "session.jsonl",
            [
                {
                    "input": 200,
                    "output": 80,
                    "cache_read": 500,
                    "cache_create": 100,
                    "model": "claude-sonnet-4-6",
                    "timestamp": "2026-03-16T10:05:00Z",
                },
            ],
        )
        sim_dir = log_dir / "stages" / "07-sim-debug-loop" / "transcripts"
        sim_dir.mkdir(parents=True)
        _make_jsonl_transcript(
            sim_dir / "session.jsonl",
            [
                {
                    "input": 300,
                    "output": 120,
                    "cache_read": 800,
                    "cache_create": 150,
                    "model": "claude-sonnet-4-6",
                    "timestamp": "2026-03-16T11:00:00Z",
                },
            ],
        )

        _make_transitions_log(
            log_dir / "transitions.log",
            [
                ("2026-03-16T10:00:00Z", "queued:init", "running:implementation"),
                ("2026-03-16T10:30:00Z", "running:implementation", "running:sim-debug-loop"),
            ],
        )

        with patch("booley.ticket_board.cli.detect_tickets_dir", return_value=tickets_dir):
            rc = main(argv=["usage", "--slug", "test-slug"])
        assert rc == 0
        assert not (log_dir / "usage.md").exists()
        output = capsys.readouterr().out
        assert "implementation" in output
        assert "sim-debug-loop" in output
        assert "Total" in output

    def test_usage_combines_current_agents_and_ignores_reset_archives(self, tmp_path, capsys):
        tickets_dir = tmp_path / "tickets"
        (tickets_dir / "logs").mkdir(parents=True)
        log_dir = tickets_dir / "logs" / "test-slug"
        developer = log_dir / ".runtime" / "developer" / "developer.jsonl"
        specialist = log_dir / ".runtime" / "transcripts" / "reviewer" / "1" / "review.jsonl"
        archived = log_dir / "runs" / "001" / ".runtime" / "developer" / "old.jsonl"
        for transcript in (developer, specialist, archived):
            transcript.parent.mkdir(parents=True, exist_ok=True)
        _make_jsonl_transcript(
            developer,
            [
                {
                    "input": 100,
                    "output": 10,
                    "cache_read": 80,
                    "cache_create": 0,
                    "timestamp": "2026-01-01T00:00:00Z",
                }
            ],
        )
        _make_jsonl_transcript(
            specialist,
            [
                {
                    "input": 50,
                    "output": 5,
                    "cache_read": 40,
                    "cache_create": 0,
                    "timestamp": "2026-01-01T00:01:00Z",
                }
            ],
        )
        _make_jsonl_transcript(
            archived,
            [{"input": 999, "output": 999, "timestamp": "2025-01-01T00:00:00Z"}],
        )
        (log_dir / ".runtime" / "booley_state.json").write_text(
            json.dumps(
                {
                    "criteria": {},
                    "timeline": [
                        {"agent": "developer_agent", "cost_usd": 2.0},
                        {"mcp_tool": "reviewer", "cost_usd": 0.5},
                    ],
                }
            ),
            encoding="utf-8",
        )

        with patch("booley.ticket_board.cli.detect_tickets_dir", return_value=tickets_dir):
            assert main(argv=["usage", "--slug", "test-slug", "--summary"]) == 0

        output = capsys.readouterr().out
        assert output.strip() == "285 tokens · $2.50"
        assert "1,998" not in output

    def test_usage_no_transcript_no_slug_errors(self, tmp_path, capsys):
        """Error when neither transcript nor --slug is provided."""
        tickets_dir = tmp_path / "tickets"
        for d in ["drafts", "queue", "waiting", "active", "blocked", "review", "done", "archived"]:
            (tickets_dir / d).mkdir(parents=True, exist_ok=True)
        (tickets_dir / "logs").mkdir(parents=True, exist_ok=True)

        with patch("booley.ticket_board.cli.detect_tickets_dir", return_value=tickets_dir):
            rc = main(argv=["usage"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "provide a transcript path or --slug" in err


class TestCLIArchive:
    """Test archive CLI subcommand."""

    def test_archive_cli(self, tmp_path, capsys):
        tickets_dir = tmp_path / "tickets"
        for d in ["drafts", "queue", "waiting", "active", "blocked", "review", "done", "archived"]:
            (tickets_dir / d).mkdir(parents=True, exist_ok=True)
        (tickets_dir / "logs").mkdir(parents=True, exist_ok=True)

        tio = TicketIO(tickets_dir)
        make_ticket_in_dir(tio, "done", "t1", extra_fields={"summary": "Done ticket"})
        make_ticket_in_dir(tio, "active", "t2", extra_fields={"summary": "Active ticket"})

        with patch("booley.ticket_board.cli.detect_tickets_dir", return_value=tickets_dir):
            rc = main(argv=["archive"])
        assert rc == 0
        output = capsys.readouterr().out
        assert "Done ticket" in output
        # done ticket should be removed
        assert not (tickets_dir / "board" / "done" / "t1.md").exists()
        # active ticket should still exist
        assert (tickets_dir / "board" / "active" / "t2.md").exists()


class TestInitAlreadyActive:
    """Test init when ticket is already in active/."""

    def test_init_already_active(self, tmp_path):
        tio = make_tio(tmp_path)
        ticket_content = (
            "---\n"
            "summary: Fix FSM bug\n"
            "type: bugfix\n"
            "branch: master\n"
            "scope_current:\n  - rtl/fsm.sv\n"
            "criteria:\n  mandatory:\n    sim_pass:\n"
            "      - tb/fsm_tb.sv @ config_a @ all @ fail -> pass\n"
            "---\n"
            "## Description\nFix the FSM thing.\n"
        )
        make_ticket_file(tio, "active", "fix-fsm-bug", content=ticket_content)
        result = tio.init_ticket(str(tio.tickets_dir / "board" / "active" / "fix-fsm-bug.md"))
        # Should succeed (idempotent), not crash
        assert result is not None
        assert result["slug"] == "fix-fsm-bug"


class TestEnqueueDuplicateSlug:
    """Test enqueueing a duplicate slug."""

    def test_enqueue_duplicate(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "queue", "add-thing", extra_fields={"summary": "Add a thing"})
        # First enqueue succeeds (stamps frontmatter)
        tio.enqueue_ticket("add-thing", "Add a thing", "feature", "master")
        # Second enqueue should detect existing ticket and return False
        result = tio.enqueue_ticket("add-thing", "Add a thing again", "feature", "master")
        assert result is False


# ============================================================================
# Log Archiving and Incident Logging
# ============================================================================


class TestAppendIncident:
    """append_incident writes structured entries to incidents.md."""

    def test_first_incident_creates_file(self, tmp_path):
        logs = tmp_path / "logs"
        n = append_incident(
            logs, "my-ticket", "compilation_failure", "sim-debug-loop", "Compilation failed"
        )
        assert n == 1
        content = (logs / "my-ticket" / "incidents.md").read_text()
        assert "# Incidents" in content
        assert "## Incident 1: compilation_failure" in content
        assert "**Step:** sim-debug-loop" in content
        assert "**Description:** Compilation failed" in content
        assert "**Resolution:** unresolved" in content

    def test_second_incident_appends(self, tmp_path):
        logs = tmp_path / "logs"
        append_incident(logs, "my-ticket", "compilation_failure", "sim-debug-loop", "first error")
        n = append_incident(
            logs,
            "my-ticket",
            "context_exhaustion",
            "sim-debug-loop",
            "agent ran out of context",
            resolution="spawned fresh agent",
        )
        assert n == 2
        content = (logs / "my-ticket" / "incidents.md").read_text()
        assert "## Incident 1: compilation_failure" in content
        assert "## Incident 2: context_exhaustion" in content
        assert "**Resolution:** spawned fresh agent" in content

    def test_creates_logs_directory(self, tmp_path):
        logs = tmp_path / "logs"
        # Directory doesn't exist yet
        assert not logs.exists()
        append_incident(logs, "new-ticket", "sim_timeout", "sim-debug-loop", "timeout after 10min")
        assert (logs / "new-ticket" / "incidents.md").exists()


class TestLogIncidentCLI:
    """CLI integration for log-incident."""

    def test_cli_creates_incident(self, tmp_path):
        tio = make_tio(tmp_path)
        logs = tio.logs_dir / "t1"
        logs.mkdir(parents=True)

        with patch("booley.ticket_board.cli.detect_tickets_dir", return_value=tio.tickets_dir):
            ret = main(
                [
                    "log-incident",
                    "t1",
                    "--type",
                    "context_exhaustion",
                    "--step",
                    "sim-debug-loop",
                    "--description",
                    "agent hit budget",
                    "--resolution",
                    "spawned fresh agent",
                ]
            )

        assert ret == 0
        content = (logs / "incidents.md").read_text()
        assert "context_exhaustion" in content
        assert "spawned fresh agent" in content


class TestFormatStageDetailIncidents:
    """format_step_detail appends incident count."""

    def test_incidents_only(self):
        from booley.ticket_board import format_step_detail

        detail = format_step_detail("implementation", {"incidents": 2})
        assert detail == "2 incidents"

    def test_incidents_with_step_detail(self):
        from booley.ticket_board import format_step_detail

        detail = format_step_detail(
            "sim-debug-loop",
            {
                "debug_rounds_used": 3,
                "debug_rounds_max": 10,
                "targets_passed": 4,
                "configs_failed": 1,
                "incidents": 1,
            },
        )
        assert "3/10 debug rounds" in detail
        assert "1 incident" in detail

    def test_no_incidents(self):
        from booley.ticket_board import format_step_detail

        detail = format_step_detail("planning", {"clarifying_questions": 0})
        assert "incident" not in detail


# ===========================================================================
# Auto-approve & integration branch tests
# ===========================================================================


class TestEnqueueOnSuccess:
    """Test on_success fields and retired enqueue arguments."""

    def test_enqueue_rejects_integration_base_override(self, tmp_path, capsys):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "queue", "ticket-a", extra_fields={"summary": "Ticket A"})

        assert tio.enqueue_ticket("ticket-a", integration_base="main~1") is False

        assert "--integration-base is retired" in capsys.readouterr().err
        path, _ = find_ticket_file(tio.tickets_dir, "ticket-a")
        fields, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert "integration_base" not in fields

    def test_enqueue_with_on_success(self, tmp_path):
        tio = make_tio(tmp_path)
        on_success = {"destination": "done", "merge": False, "cleanup": True}
        make_ticket_in_dir(
            tio,
            "queue",
            "ticket-a",
            extra_fields={"summary": "Ticket A", "on_success": on_success},
        )
        tio.enqueue_ticket("ticket-a", "Ticket A", "feature", "int/enc-dec", on_success=on_success)
        path, _ = find_ticket_file(tio.tickets_dir, "ticket-a")
        fields, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert fields["on_success"]["destination"] == "done"
        assert fields["on_success"]["merge"] is False
        assert fields["on_success"]["cleanup"] is True

    def test_enqueue_default_no_on_success(self, tmp_path):
        """Without on_success param, field should not be stamped by enqueue."""
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "queue", "ticket-b", extra_fields={"summary": "Ticket B"})
        tio.enqueue_ticket("ticket-b", "Ticket B", "feature", "master")
        path, _ = find_ticket_file(tio.tickets_dir, "ticket-b")
        fields, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        # on_success may exist from create_ticket_file defaults but enqueue doesn't stamp it
        assert "on_success" not in fields or isinstance(fields.get("on_success"), dict)

    def test_enqueue_default_no_integration_base(self, tmp_path):
        """Without integration_base, field should not appear."""
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "queue", "ticket-b", extra_fields={"summary": "Ticket B"})
        tio.enqueue_ticket("ticket-b", "Ticket B", "feature", "master")
        path, _ = find_ticket_file(tio.tickets_dir, "ticket-b")
        fields, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert "integration_base" not in fields


class TestOpApproveActorDetail:
    """Test that op_approve logs custom actor and detail in transitions."""

    def test_default_actor_and_detail(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "review", "my-ticket", extra_fields={"step": "summary"})
        op_approve(tio, "my-ticket")

        log = human_log_file(tio.logs_dir, "my-ticket", "transitions.log").read_text()
        assert "ticket-triage" in log
        assert "user approved merge" in log

    def test_custom_actor_and_detail(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "review", "auto-ticket")
        make_progress(tio, "auto-ticket", {"step": "summary"})

        op_approve(tio, "auto-ticket", actor="ticket-execute", detail="auto-approved and merged")

        log = human_log_file(tio.logs_dir, "auto-ticket", "transitions.log").read_text()
        assert "ticket-execute" in log
        assert "auto-approved and merged" in log
        # Verify ticket moved to done
        _path, status = find_ticket_file(tio.tickets_dir, "auto-ticket")
        assert status == "done"
        progress = load_progress(tio.logs_dir, "auto-ticket")
        assert progress["step"] == "complete"


class TestCLIEnqueueOnSuccess:
    """Test CLI --destination/--merge/--cleanup and --integration-base flags.

    Note: enqueue_ticket has a duplicate guard -- if the file already exists
    in queue/ and find_ticket_file finds it by stem, enqueue returns False.
    These tests verify the CLI exits correctly and that the flags parse.
    """

    def test_cli_enqueue_duplicate_returns_2(self, tmp_path, capsys):
        """Enqueue with pre-existing ticket file returns exit code 2 (duplicate guard)."""
        tickets_dir = tmp_path / "tickets"
        for d in ["drafts", "queue", "waiting", "active", "blocked", "review", "done", "archived"]:
            (tickets_dir / d).mkdir(parents=True, exist_ok=True)
        (tickets_dir / "logs").mkdir(parents=True, exist_ok=True)

        tio = TicketIO(tickets_dir)
        make_ticket_in_dir(
            tio,
            "queue",
            "my-slug",
            extra_fields={"summary": "My ticket", "created": "2026-03-15T10:00:00Z"},
        )

        with patch("booley.ticket_board.cli.detect_tickets_dir", return_value=tickets_dir):
            rc = main(
                argv=[
                    "enqueue",
                    "my-slug",
                    "--summary",
                    "My ticket",
                    "--type",
                    "feature",
                    "--branch",
                    "int/foo",
                    "--destination",
                    "done",
                    "--no-merge",
                    "--integration-base",
                    "devel_branch",
                ]
            )
        # Duplicate guard (ticket already stamped) -> enqueue returns False -> CLI returns 2
        assert rc == 2

    def test_cli_enqueue_no_file_returns_2(self, tmp_path, capsys):
        """Enqueue when no ticket file exists returns exit code 2."""
        tickets_dir = tmp_path / "tickets"
        for d in ["drafts", "queue", "waiting", "active", "blocked", "review", "done", "archived"]:
            (tickets_dir / d).mkdir(parents=True, exist_ok=True)
        (tickets_dir / "logs").mkdir(parents=True, exist_ok=True)

        with patch("booley.ticket_board.cli.detect_tickets_dir", return_value=tickets_dir):
            rc = main(
                argv=[
                    "enqueue",
                    "nonexistent",
                    "--summary",
                    "My ticket",
                    "--type",
                    "feature",
                    "--branch",
                    "master",
                ]
            )
        # No file found -> returns 2
        assert rc == 2


class TestCLIApproveActorDetail:
    """Test CLI approve --actor and --detail flags."""

    def test_cli_approve_custom_actor(self, tmp_path, capsys):
        tickets_dir = tmp_path / "tickets"
        for d in ["drafts", "queue", "waiting", "active", "blocked", "review", "done", "archived"]:
            (tickets_dir / d).mkdir(parents=True, exist_ok=True)
        (tickets_dir / "logs").mkdir(parents=True, exist_ok=True)

        tio = TicketIO(tickets_dir)
        make_ticket_in_dir(tio, "review", "t", extra_fields={"step": "summary"})

        with patch("booley.ticket_board.cli.detect_tickets_dir", return_value=tickets_dir):
            rc = main(
                argv=[
                    "approve",
                    "t",
                    "--actor",
                    "ticket-execute",
                    "--detail",
                    "auto-approved and merged",
                ]
            )
        assert rc == 0
        log = human_log_file(tio.logs_dir, "t", "transitions.log").read_text()
        assert "ticket-execute" in log
        assert "auto-approved and merged" in log


class TestClassifyAutoApproved:
    """Auto-approved tickets (status=done) should unblock dependents."""

    def test_done_tickets_unblock_deps(self, tmp_path):
        """When ticket A is auto-approved (done), ticket B depending on A
        should become executable."""
        result = classify_tickets(
            [
                {
                    "file": "done/ticket-a.md",
                    "summary": "A",
                    "status": "done",
                    "feature_branch": "ticket-a",
                },
                {
                    "file": "queue/ticket-b.md",
                    "summary": "B",
                    "status": "queued",
                    "dependencies": ["ticket-a"],
                },
            ]
        )
        # ticket-b should be executable (not waiting)
        assert len(result["executable"]) == 1
        assert result["executable"][0]["summary"] == "B"
        assert len(result["waiting"]) == 0


# ============================================================================
# Log Validation Tests
# ============================================================================


class TestValidateLogs:
    """Tests for validate_logs() pure function."""

    # Reverse map: filename -> stage name (for routing to stage dirs)
    _FILE_TO_STAGE: ClassVar[dict[str, str]] = {
        "plan.md": "planning",
        "plan-context.json": "run-config",
        "lint-report.md": "lint-check",
        "rtl-review-1.md": "rtl-review-1",
        "tb-review.md": "tb-review",
        "sim-results.md": "sim-debug-loop",
        "sim-iterations.md": "sim-debug-loop",
        "rtl-mutation-testing.md": "rtl-mutation-testing",
        "rtl-review-final.md": "rtl-review-final",
        "synthesis-report.md": "synthesis",
        "acceptance-checks.md": "acceptance-check",
        "summary.md": "summary",
    }

    def _setup_logs(self, tmp_path, slug, files=None, meta=None):
        """Create a logs directory with optional files and stage metadata.

        Files are routed to per-stage directories if they belong to a stage,
        or placed at the log root for persistent files (transitions.log, etc.).
        """
        logs_dir = tmp_path / "logs"
        log_dir = logs_dir / slug
        log_dir.mkdir(parents=True, exist_ok=True)
        if files:
            for fname in files:
                stage = self._FILE_TO_STAGE.get(fname)
                if stage:
                    write_stage_file(logs_dir, slug, stage, fname, f"# {fname}\nContent.")
                else:
                    path = _persistent_file(logs_dir, slug, fname)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(f"# {fname}\nContent.", encoding="utf-8")
        runtime_dir = log_dir / ".runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        (runtime_dir / "booley_state.json").write_text(
            json.dumps(
                {
                    "slug": slug,
                    "criteria": {
                        "lint_clean": {
                            "met": True,
                            "visible": True,
                            "mandatory": True,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        (runtime_dir / "progress.json").write_text(
            json.dumps({"step": "", "steps_completed": []}),
            encoding="utf-8",
        )
        if meta:
            _test_save_step_meta(logs_dir, slug, meta)
        return logs_dir

    def test_all_present_no_issues(self, tmp_path):
        """Full feature pipeline with transitions.log -- clean."""
        slug = "test-ticket"
        stages = [
            "setup",
            "planning",
            "run-config",
            "implementation",
            "implementation-tb",
            "lint-check",
            "rtl-review-1",
            "tb-review",
            "sim-debug-loop",
            "rtl-mutation-testing",
            "rtl-review-final",
            "post-review-sim",
            "synthesis",
            "acceptance-check",
            "summary",
        ]
        logs_dir = self._setup_logs(tmp_path, slug, files=["transitions.log"])

        result = validate_logs(logs_dir, slug, "feature", stages)
        assert result["missing_files"] == []
        assert result["missing_meta"] == []
        assert result["skipped_steps"] == []
        assert result["warnings"] == []

    def test_missing_files_always_empty(self, tmp_path):
        """missing_files check removed with stages/ system."""
        slug = "test-ticket"
        logs_dir = self._setup_logs(tmp_path, slug, files=["transitions.log"])
        result = validate_logs(logs_dir, slug, "feature", ["setup", "planning", "sim-debug-loop"])
        assert result["missing_files"] == []

    def test_missing_meta_always_empty(self, tmp_path):
        """missing_meta check removed with stages/ system."""
        slug = "test-ticket"
        logs_dir = self._setup_logs(tmp_path, slug, files=["transitions.log"])
        result = validate_logs(logs_dir, slug, "feature", ["setup", "planning", "sim-debug-loop"])
        assert result["missing_meta"] == []

    def test_skipped_stages_detected(self, tmp_path):
        """Stages in the middle of the pipeline missing from completed list."""
        slug = "test-ticket"
        logs_dir = self._setup_logs(tmp_path, slug, files=["transitions.log"])
        # Jumped from implementation to sim-debug-loop (skipped rtl-review-1)
        result = validate_logs(
            logs_dir,
            slug,
            "feature",
            ["setup", "planning", "implementation", "sim-debug-loop"],
        )
        skipped = [s["step"] for s in result["skipped_steps"]]
        assert "lint-check" in skipped
        assert "rtl-review-1" in skipped
        # Stages after sim-debug-loop should NOT be flagged
        assert "rtl-review-final" not in skipped
        assert "synthesis" not in skipped

    def test_skipped_stages_only_between_completed(self, tmp_path):
        """Only stages BETWEEN first and last completed are flagged as skipped."""
        slug = "test-ticket"
        logs_dir = self._setup_logs(tmp_path, slug, files=["transitions.log"])
        # Completed stages with gaps — skipped stages between them are flagged
        completed = [
            "setup",
            "planning",
            "run-config",
            "implementation",
            "lint-check",
            "rtl-review-1",
            "sim-debug-loop",
            "summary",
        ]
        result = validate_logs(logs_dir, slug, "feature", completed)
        skipped = [s["step"] for s in result["skipped_steps"]]
        # Stages after summary (like review) should NOT be flagged
        assert "review" not in skipped
        # Stages between completed ones that are in STEP_ORDER are flagged
        assert "implementation-tb" in skipped  # between implementation and lint-check

    def test_validate_logs_uses_step_order(self, tmp_path):
        """validate_logs always uses STEP_ORDER for skipped-stage detection."""
        slug = "test-ticket"
        logs_dir = self._setup_logs(
            tmp_path, slug, files=["plan.md", "sim-results.md", "summary.md", "transitions.log"]
        )
        # All stages completed in order — no gaps
        stages = [
            "setup",
            "planning",
            "run-config",
            "implementation",
            "implementation-tb",
            "lint-check",
            "rtl-review-1",
            "tb-review",
            "sim-debug-loop",
            "rtl-mutation-testing",
            "rtl-review-final",
            "post-review-sim",
            "synthesis",
            "acceptance-check",
            "summary",
        ]
        result = validate_logs(logs_dir, slug, "feature", stages)
        skipped = [s["step"] for s in result["skipped_steps"]]
        # No gaps in a complete pipeline → nothing skipped
        assert skipped == []

    def test_missing_transitions_log_warning(self, tmp_path):
        """No transitions.log -- warning."""
        slug = "test-ticket"
        logs_dir = self._setup_logs(tmp_path, slug, files=["plan.md"])
        result = validate_logs(logs_dir, slug, "feature", ["setup", "planning"])
        assert any("transitions.log" in w for w in result["warnings"])

    def test_empty_stages_no_issues(self, tmp_path):
        """No completed stages -- nothing to validate."""
        slug = "test-ticket"
        logs_dir = self._setup_logs(tmp_path, slug)
        result = validate_logs(logs_dir, slug, "feature", [])
        assert result["missing_files"] == []
        assert result["missing_meta"] == []
        assert result["skipped_steps"] == []


class TestFormatValidateLogsReport:
    """Tests for format_validate_logs_report()."""

    def test_clean_report(self):
        result = {
            "missing_files": [],
            "missing_meta": [],
            "skipped_steps": [],
            "warnings": [],
        }
        report, errors = format_validate_logs_report(result, "test")
        assert errors == 0
        assert "All log artifacts present" in report

    def test_report_with_issues(self):
        result = {
            "missing_files": [{"step": "sim-debug-loop", "file": "sim-results.md"}],
            "missing_meta": [{"step": "sim-debug-loop", "keys": ["converged"]}],
            "skipped_steps": [{"step": "rtl-review-1", "reason": "not in steps_completed"}],
            "warnings": ["transitions.log is missing"],
        }
        report, errors = format_validate_logs_report(result, "test")
        assert errors == 4
        assert "sim-results.md" in report
        assert "converged" in report
        assert "rtl-review-1" in report
        assert "transitions.log" in report


class TestStageMetaValidators:
    """Tests for STEP_META_VALIDATORS content-validation gates."""

    # -- Helper functions --

    def testno_unfixed_critical_no_issues(self):
        assert no_unfixed_critical({"critical_found": 0, "critical_fixed": 0})

    def testno_unfixed_critical_all_fixed(self):
        assert no_unfixed_critical({"critical_found": 2, "critical_fixed": 2})

    def testno_unfixed_critical_unfixed(self):
        assert not no_unfixed_critical({"critical_found": 2, "critical_fixed": 1})

    def testno_unfixed_critical_missing_keys(self):
        """Missing keys default to 0 -- passes."""
        assert no_unfixed_critical({})

    def testno_large_area_increase_within_threshold(self):
        meta = {"targets": [{"name": "v01", "delta_pct": "+10.5%"}]}
        assert no_large_area_increase(meta, threshold=50.0)

    def testno_large_area_increase_exceeds_threshold(self):
        meta = {"targets": [{"name": "v01", "delta_pct": "+75.3%"}]}
        assert not no_large_area_increase(meta, threshold=50.0)

    def testno_large_area_increase_negative_delta(self):
        """Negative delta (area reduction) -- always passes."""
        meta = {"targets": [{"name": "v01", "delta_pct": "-20%"}]}
        assert no_large_area_increase(meta, threshold=50.0)

    def testno_large_area_increase_no_configs(self):
        assert no_large_area_increase({}, threshold=50.0)


class TestValidateLogsValueValidation:
    """Tests for validate_logs() — stages/ gate validation removed, returns empty lists."""

    def test_gate_failures_always_empty(self, tmp_path):
        """validate_logs returns empty gate_failures (stage gates removed)."""
        logs_dir = tmp_path / "logs"
        log_dir = logs_dir / "t1"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "transitions.log").write_text("# transitions\n")
        result = validate_logs(logs_dir, "t1", "feature", ["sim-debug-loop"])
        assert result["gate_failures"] == []
        assert result["gate_warnings"] == []


class TestCollectEvidence:
    """Tests for the collect-evidence CLI subcommand."""

    def _setup_tickets_dir(self, tmp_path):
        tickets_dir = tmp_path / "tickets"
        for d in ["drafts", "queue", "waiting", "active", "blocked", "review", "done", "archived"]:
            (tickets_dir / d).mkdir(parents=True, exist_ok=True)
        (tickets_dir / "logs").mkdir(parents=True, exist_ok=True)
        return tickets_dir

    def test_basic_evidence_collection(self, tmp_path, capsys):
        """collect-evidence returns structured JSON (stage meta no longer populated)."""
        tickets_dir = self._setup_tickets_dir(tmp_path)
        tio = TicketIO(tickets_dir)
        make_ticket_in_dir(tio, "active", "t1", extra_fields={"step": "acceptance-check"})

        with patch("booley.ticket_board.cli.detect_tickets_dir", return_value=tickets_dir):
            rc = main(argv=["collect-evidence", "t1"])
        assert rc == 0
        captured = capsys.readouterr()
        evidence = json.loads(captured.out)
        assert evidence["ticket"]["type"] == "feature"
        # Stage metadata was removed with the stages system; do not fabricate
        # empty compile/simulation evidence in its place.
        assert set(evidence) == {"ticket"}


# ============================================================================
# Session & Transcript Tests
# ============================================================================


class TestValidateLogsCLI:
    """Test validate-logs CLI subcommand."""

    def _setup_tickets_dir(self, tmp_path):
        tickets_dir = tmp_path / "tickets"
        for d in ["drafts", "queue", "waiting", "active", "blocked", "review", "done", "archived"]:
            (tickets_dir / d).mkdir(parents=True, exist_ok=True)
        (tickets_dir / "logs").mkdir(parents=True, exist_ok=True)
        return tickets_dir

    def test_clean_ticket(self, tmp_path, capsys):
        tickets_dir = self._setup_tickets_dir(tmp_path)
        tio = TicketIO(tickets_dir)
        make_ticket_in_dir(tio, "done", "t1", extra_fields={"type": "feature"})
        import copy

        p = copy.deepcopy(PROGRESS_DEFAULTS)
        p["steps_completed"] = ["setup", "planning"]
        save_progress(tickets_dir / "logs", "t1", p)

        trans_file = human_log_file(tickets_dir / "logs", "t1", "transitions.log")
        trans_file.parent.mkdir(parents=True, exist_ok=True)
        trans_file.write_text("log entry", encoding="utf-8")
        state_file = runtime_file(tickets_dir / "logs", "t1", "booley_state.json")
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(
            json.dumps({"slug": "t1", "criteria": {}}),
            encoding="utf-8",
        )

        with patch("booley.ticket_board.cli.detect_tickets_dir", return_value=tickets_dir):
            rc = main(argv=["validate-logs", "t1"])
        assert rc == 0
        assert "All log artifacts present" in capsys.readouterr().out

    def test_skipped_steps_reported(self, tmp_path, capsys):
        """validate-logs reports skipped steps (missing_files/meta checks removed)."""
        tickets_dir = self._setup_tickets_dir(tmp_path)
        tio = TicketIO(tickets_dir)
        make_ticket_in_dir(tio, "done", "t1", extra_fields={"type": "feature"})
        import copy

        p2 = copy.deepcopy(PROGRESS_DEFAULTS)
        p2["steps_completed"] = [
            "setup",
            "planning",
            "implementation",
            "sim-debug-loop",
            "summary",
        ]
        save_progress(tickets_dir / "logs", "t1", p2)

        log_dir = tickets_dir / "logs" / "t1"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "transitions.log").write_text("log")

        with patch("booley.ticket_board.cli.detect_tickets_dir", return_value=tickets_dir):
            rc = main(argv=["validate-logs", "t1"])
        # Should report skipped steps between completed ones
        assert rc == 1
        output = capsys.readouterr().out
        assert "Skipped Steps" in output

    def test_ticket_not_found(self, tmp_path, capsys):
        tickets_dir = self._setup_tickets_dir(tmp_path)

        with patch("booley.ticket_board.cli.detect_tickets_dir", return_value=tickets_dir):
            rc = main(argv=["validate-logs", "nonexistent"])
        assert rc == 1


# ===========================================================================
# New tests for ticket system fixes
# ===========================================================================


class TestOpMissingSlug:
    """Test all op_* functions return False on nonexistent slugs, no phantom transitions."""

    def test_op_block_missing(self, tmp_path):
        tio = make_tio(tmp_path)
        result = op_block(tio, "nonexistent", "reason", "planning")
        assert result is False
        # No transition log should exist
        assert not (tio.logs_dir / "nonexistent" / "transitions.log").exists()

    def test_op_fail_missing(self, tmp_path):
        tio = make_tio(tmp_path)
        result = op_fail(tio, "nonexistent", "error", "planning")
        assert result is False

    def test_op_unblock_missing(self, tmp_path):
        tio = make_tio(tmp_path)
        result = op_unblock(tio, "nonexistent")
        assert result is False

    def test_op_approve_missing(self, tmp_path):
        tio = make_tio(tmp_path)
        result = op_approve(tio, "nonexistent")
        assert result is False

    def test_op_handoff_missing_run_log(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "active", "t1")
        # No run.log exists
        result = op_handoff(tio, "t1")
        assert result is False


class TestOpReturnValues:
    """Test that refactored ops return True on success."""

    def test_op_block_success(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "active", "t1")
        assert op_block(tio, "t1", "need info", "planning") is True

    def test_op_fail_success(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "active", "t1")
        assert op_fail(tio, "t1", "boom", "sim-debug-loop") is True

    def test_op_approve_success(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "review", "t1")
        assert op_approve(tio, "t1") is True

    def test_complete_rejects_corrupt_accepted_snapshot(self, tmp_path, capsys):
        from booley.criteria.state import DevelopmentState
        from booley.ticket_board.acceptance_ledger import freeze_acceptance
        from booley.ticket_board.operations import op_complete

        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "review", "t1")
        state_path = runtime_file(tio.logs_dir, "t1", "booley_state.json")
        state = DevelopmentState.load(state_path)
        state.slug = "t1"
        state.init_criteria({"sim_pass": True}, strict=True)
        state.set_criterion("sim_pass", True)
        state.save()
        frozen = freeze_acceptance(
            tio.logs_dir / "t1",
            state,
            execution_id="run-1",
            target_contract=None,
        )
        snapshot_path = tio.logs_dir / "t1" / "acceptance" / "snapshots" / f"{frozen.digest}.json"
        snapshot_path.write_text('{"tampered":true}\n', encoding="utf-8")

        assert op_complete(tio, "t1") is False
        assert "accepted snapshot" in capsys.readouterr().err

    def test_complete_rejects_review_package_changed_after_binding(self, tmp_path, capsys):
        import json

        from booley.criteria.state import DevelopmentState
        from booley.ticket_board.acceptance_ledger import bind_review_package, freeze_acceptance
        from booley.ticket_board.operations import op_complete

        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "review", "t1")
        state = DevelopmentState.load(runtime_file(tio.logs_dir, "t1", "booley_state.json"))
        state.slug = "t1"
        state.init_criteria({"sim_pass": True}, strict=True)
        state.set_criterion("sim_pass", True)
        state.save()
        snapshot = freeze_acceptance(
            tio.logs_dir / "t1",
            state,
            execution_id="run-1",
            target_contract=None,
        )
        prep_dir = tio.logs_dir / "t1" / ".runtime" / "triage-prep"
        prep_dir.mkdir(parents=True)
        briefing = prep_dir / "briefing.json"
        briefing.write_text('{"assessment": {}}\n', encoding="utf-8")
        (prep_dir / "manifest.json").write_text(
            json.dumps({"status": "ready", "briefing_path": str(briefing)}),
            encoding="utf-8",
        )
        assert bind_review_package(tio.logs_dir / "t1", snapshot)
        briefing.write_text('{"assessment": {"changed": true}}\n', encoding="utf-8")

        assert op_complete(tio, "t1") is False
        assert "review package binding" in capsys.readouterr().err

    def test_op_complete_rejects_no_merge_with_target_removal(self, tmp_path, capsys):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(
            tio,
            "review",
            "t1",
            extra_fields={
                "on_success": {
                    "destination": "review",
                    "merge": True,
                    "cleanup": False,
                    "triage_report": False,
                    "remove_targets": ["acme:lib:toy:1.0#baseline"],
                }
            },
        )

        assert op_complete(tio, "t1", no_merge=True) is False
        assert "cannot remove Targets when merge is disabled" in capsys.readouterr().err


class TestDraftsDirectory:
    """Test that drafts/ directory is used for new ticket creation."""

    def test_create_ticket_file_lands_in_drafts(self, tmp_path):
        """create_ticket_file should place new tickets in board/drafts/."""
        tio = make_tio(tmp_path)
        path = tio.create_ticket_file(
            "new-ticket",
            TicketFileSpec(
                summary="New ticket",
                ticket_type="feature",
                branch="master",
                scope=["rtl/foo.sv"],
                criteria={
                    "mandatory": {"sim_pass": ["tb/foo_tb.sv @ default @ all @ pass -> pass"]}
                },
            ),
        )
        assert path is not None
        assert path.parent.name == "drafts"
        assert path.exists()

    def test_create_ticket_file_preserves_explicit_on_success(self, tmp_path):
        tio = make_tio(tmp_path)
        on_success = {
            "destination": "done",
            "merge": False,
            "cleanup": False,
            "triage_report": False,
            "remove_targets": [],
        }
        path = tio.create_ticket_file(
            "custom-handoff",
            TicketFileSpec(
                summary="Custom handoff",
                ticket_type="feature",
                branch="master",
                criteria={"mandatory": {"custom": True}},
                on_success=on_success,
            ),
        )
        assert path is not None
        fields, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert fields["on_success"] == on_success

    def test_create_file_cli_accepts_on_success(self, tmp_path, monkeypatch):
        tickets_dir = tmp_path / "tickets"
        monkeypatch.setenv("TICKETS_DIR", str(tickets_dir))
        on_success = {
            "destination": "done",
            "merge": False,
            "cleanup": True,
            "triage_report": False,
        }

        rc = main(
            argv=[
                "create-file",
                "cli-defaults",
                "--summary",
                "CLI defaults",
                "--type",
                "feature",
                "--branch",
                "main",
                "--criteria",
                json.dumps({"mandatory": {"custom": True}}),
                "--on-success",
                json.dumps(on_success),
            ]
        )

        assert rc == 0
        path = tickets_dir / "board" / "drafts" / "cli-defaults.md"
        fields, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert fields["on_success"] == {**on_success, "remove_targets": []}

    @pytest.mark.parametrize(
        ("on_success", "error"),
        [
            ("not-json", "invalid JSON"),
            (json.dumps([]), "--on-success must be a mapping"),
            (
                json.dumps({"destination": "done"}),
                "missing keys: cleanup, merge, triage_report",
            ),
            (
                json.dumps(
                    {
                        "destination": "done",
                        "merge": False,
                        "cleanup": True,
                        "triage_report": False,
                        "remove_targets": [],
                        "unexpected": True,
                    }
                ),
                "unknown keys: unexpected",
            ),
            (
                json.dumps(
                    {
                        "destination": "done",
                        "merge": "no",
                        "cleanup": True,
                        "triage_report": False,
                        "remove_targets": [],
                    }
                ),
                "on_success.merge must be true or false",
            ),
        ],
    )
    def test_create_file_cli_rejects_invalid_on_success(
        self, tmp_path, monkeypatch, capsys, on_success, error
    ):
        tickets_dir = tmp_path / "tickets"
        monkeypatch.setenv("TICKETS_DIR", str(tickets_dir))

        rc = main(
            argv=[
                "create-file",
                "invalid-defaults",
                "--summary",
                "Invalid defaults",
                "--type",
                "feature",
                "--branch",
                "main",
                "--on-success",
                on_success,
            ]
        )

        assert rc == 2
        assert error in capsys.readouterr().err
        assert not (tickets_dir / "board" / "drafts" / "invalid-defaults.md").exists()

    def test_create_ticket_file_omits_empty_legacy_and_runtime_fields(self, tmp_path):
        """New tickets should not carry stale blank plan/runtime placeholders."""
        tio = make_tio(tmp_path)
        path = tio.create_ticket_file(
            "new-ticket",
            TicketFileSpec(
                summary="New ticket",
                ticket_type="feature",
                branch="master",
                scope=["rtl/foo.sv [new]"],
                criteria={
                    "mandatory": {"sim_pass": ["tb/foo_tb.sv @ default @ all @ pass -> pass"]}
                },
            ),
        )
        assert path is not None
        text = path.read_text(encoding="utf-8")

        assert "spec:" not in text
        assert "feature_branch:" not in text
        assert "created:" not in text
        assert "integration_base:" not in text

    def test_scan_draft_status(self, tmp_path):
        """Tickets in drafts/ should scan as 'draft' status."""
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "drafts", "t1")
        tickets = scan_all_tickets(tio.tickets_dir)
        assert len(tickets) == 1
        assert tickets[0]["status"] == "draft"

    def test_scan_done_not_affected(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "done", "t1")
        tickets = scan_all_tickets(tio.tickets_dir)
        assert tickets[0]["status"] == "done"


class TestTimingCompleted:
    """Test compute_step_durations with end_time avoids inflation."""

    def test_end_time_caps_last_stage(self):
        from datetime import datetime

        transitions = [
            {
                "timestamp": "2026-03-16T10:00:00Z",
                "to_state": "running:planning",
                "stage": "planning",
            },
            {
                "timestamp": "2026-03-16T10:05:00Z",
                "to_state": "running:impl",
                "stage": "implementation",
            },
        ]
        end_time = datetime(2026, 3, 16, 10, 10, 0, tzinfo=UTC)
        durations = compute_step_durations(transitions, end_time=end_time)
        assert durations["implementation"] == 300  # 5 min, not inflated to "now"

    def test_no_end_time_uses_now(self):
        transitions = [
            {
                "timestamp": "2026-03-16T10:00:00Z",
                "to_state": "running:planning",
                "stage": "planning",
            },
        ]
        durations = compute_step_durations(transitions)
        # Should be > 0 (uses now as end)
        assert durations["planning"] > 0


class TestYamlEscapeQuotes:
    """Test round-trip with embedded quotes in values."""

    def test_embedded_double_quotes(self):
        fields = {"summary": 'fix "edge" case'}
        text = format_frontmatter(fields, "")
        parsed, _ = parse_frontmatter(text)
        assert parsed["summary"] == 'fix "edge" case'

    def test_embedded_backslash(self):
        fields = {"summary": r"path\to\file"}
        text = format_frontmatter(fields, "")
        parsed, _ = parse_frontmatter(text)
        assert parsed["summary"] == r"path\to\file"

    def test_embedded_quotes_and_colon(self):
        fields = {"summary": 'failed: "timeout" exceeded'}
        text = format_frontmatter(fields, "")
        parsed, _ = parse_frontmatter(text)
        assert parsed["summary"] == 'failed: "timeout" exceeded'


class TestNegativeIntegers:
    """Test parse_frontmatter handles negative integer values."""

    def test_negative_int(self):
        text = "---\noffset: -5\n---\n"
        fields, _ = parse_frontmatter(text)
        assert fields["offset"] == -5
        assert isinstance(fields["offset"], int)

    def test_positive_int_still_works(self):
        text = "---\ncount: 42\n---\n"
        fields, _ = parse_frontmatter(text)
        assert fields["count"] == 42

    def test_zero(self):
        text = "---\ncount: 0\n---\n"
        fields, _ = parse_frontmatter(text)
        assert fields["count"] == 0


class TestListItemsWithColons:
    """Test that list items containing colons parse correctly."""

    def test_colon_in_list_item(self):
        text = "---\nscope_current:\n  - rtl/my_pkg.sv: updated\n  - tb/test.sv\n---\n"
        fields, _ = parse_frontmatter(text)
        assert fields["scope_current"] == ["rtl/my_pkg.sv: updated", "tb/test.sv"]


class TestTransitionsSorted:
    """Test parse_transitions_log sorts output by timestamp."""

    def test_out_of_order_sorted(self, tmp_path):
        log = tmp_path / "transitions.log"
        # Write in reverse order
        log.write_text(
            "2026-03-16T10:05:00Z | running:planning -> running:implementation | x | y\n"
            "2026-03-16T10:00:00Z | queued:init -> running:planning | x | y\n",
            encoding="utf-8",
        )
        transitions = parse_transitions_log(log)
        assert transitions[0]["stage"] == "planning"
        assert transitions[1]["stage"] == "implementation"


class TestCollectAllMessagesRecovered:
    """Test collect_all_messages finds subagents in sibling dir (copied transcripts)."""

    def test_sibling_subagents_included(self, tmp_path):
        # Simulate copied transcript layout: logs/<slug>/transcript.jsonl + subagents/
        import json as _json

        log_dir = tmp_path / "logs" / "my-ticket"
        log_dir.mkdir(parents=True)

        # Main transcript
        main_transcript = log_dir / "transcript.jsonl"
        main_msg = {
            "type": "assistant",
            "timestamp": "2026-03-16T10:00:00Z",
            "message": {
                "model": "claude-sonnet-4-6",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
            },
        }
        main_transcript.write_text(_json.dumps(main_msg) + "\n", encoding="utf-8")

        # Subagent in sibling dir
        sa_dir = log_dir / "subagents"
        sa_dir.mkdir()
        sa_msg = {
            "type": "assistant",
            "timestamp": "2026-03-16T10:01:00Z",
            "message": {
                "model": "claude-opus-4-6",
                "usage": {
                    "input_tokens": 200,
                    "output_tokens": 100,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
            },
        }
        (sa_dir / "agent-001.jsonl").write_text(_json.dumps(sa_msg) + "\n", encoding="utf-8")

        messages = collect_all_messages(main_transcript)
        assert len(messages) == 2
        assert messages[0]["input_tokens"] == 100  # main
        assert messages[1]["input_tokens"] == 200  # subagent


class TestEnqueueAppliesParams:
    """Test that enqueue_ticket writes on_success to frontmatter."""

    def test_on_success_applied(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "queue", "t1", extra_fields={"summary": "t1"})
        on_success = {"destination": "done", "merge": False, "cleanup": True}
        # Bypass the duplicate guard so enqueue_ticket actually stamps the file.
        # In normal workflow, enqueue is called before find_ticket_file can find it.
        with patch("booley.ticket_board.io.find_ticket_file", return_value=(None, None)):
            tio.enqueue_ticket(
                "t1",
                "t1",
                "feature",
                "master",
                on_success=on_success,
            )
        path = tio.tickets_dir / "board" / "queue" / "t1.md"
        fields, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        assert fields["on_success"]["destination"] == "done"
        assert fields["on_success"]["merge"] is False

    def test_no_on_success_by_default(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "queue", "t2", extra_fields={"summary": "t2"})
        # Bypass the duplicate guard so enqueue_ticket actually stamps the file.
        with patch("booley.ticket_board.io.find_ticket_file", return_value=(None, None)):
            tio.enqueue_ticket("t2", "t2", "feature", "master")
        path = tio.tickets_dir / "board" / "queue" / "t2.md"
        fields, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
        # on_success should not be stamped by enqueue when not provided
        assert "on_success" not in fields or isinstance(fields.get("on_success"), dict)


class TestFmtDuration:
    """Test new HH:MM:SS / MM:SS duration format."""

    def test_seconds_only(self):
        assert fmt_duration(45) == "00:45"

    def test_minutes_and_seconds(self):
        assert fmt_duration(125) == "02:05"

    def test_hours(self):
        assert fmt_duration(3661) == "01:01:01"

    def test_zero(self):
        assert fmt_duration(0) == "00:00"

    def test_negative(self):
        assert fmt_duration(-1) == "---"


class TestFmtDatetimeUser:
    """Test local ``HH:MM · DD MMM YYYY`` date display format."""

    def test_iso_format(self, monkeypatch):
        monkeypatch.setenv("BOOLEY_LOCAL_TIMEZONE", "+04:00")
        assert fmt_datetime_user("2026-03-19T14:30:00Z") == "18:30 · 19 MAR 2026"

    def test_empty_string(self):
        assert fmt_datetime_user("") == "---"

    def test_none(self):
        assert fmt_datetime_user(None) == "---"

    def test_invalid_format(self):
        assert fmt_datetime_user("not-a-date") == "not-a-date"


class TestValidateTicketDirtyTree:
    """Test validate_ticket_fields with check_git for dirty tree detection."""

    def test_dirty_tree_error(self, tmp_path):
        """Mock git status to return dirty files."""
        fields = {
            "summary": "test",
            "type": "feature",
            "branch": "master",
            "scope_current": ["rtl/foo.sv"],
            "criteria": {
                "mandatory": {"sim_pass": ["tb/foo_tb.sv @ default @ all @ pass -> pass"]}
            },
        }
        body = "## Description\nTest."
        # Mock subprocess.run to simulate dirty tree
        from unittest.mock import MagicMock

        mock_result_branch = MagicMock(returncode=0, stdout="", stderr="")
        mock_result_status = MagicMock(
            returncode=0, stdout=" M rtl/foo.sv\0 M rtl/bar.sv\0", stderr=""
        )
        mock_result_gitdir = MagicMock(returncode=0, stdout=str(tmp_path / ".git"), stderr="")
        with patch(
            "subprocess.run",
            side_effect=[mock_result_branch, mock_result_status, mock_result_gitdir],
        ):
            errors = validate_ticket_fields(
                fields, body, check_git=True, project_root=str(tmp_path)
            )
        dirty_errors = [e for e in errors if "Dirty" in e]
        assert len(dirty_errors) == 1
        assert "2 modified files" in dirty_errors[0]


# ---------------------------------------------------------------------------
# Priority
# ---------------------------------------------------------------------------


class TestPriorityConstants:
    def test_valid_priorities(self):
        assert {"low", "medium", "high"} == VALID_PRIORITIES

    def test_priority_order_covers_all(self):
        assert set(PRIORITY_ORDER.keys()) == VALID_PRIORITIES

    def test_high_sorts_first(self):
        assert PRIORITY_ORDER["high"] < PRIORITY_ORDER["medium"] < PRIORITY_ORDER["low"]


class TestClassifyPriority:
    def test_high_priority_sorted_first(self):
        tickets = [
            {"status": "queued", "feature_branch": "low-t", "priority": "low", "dependencies": []},
            {
                "status": "queued",
                "feature_branch": "high-t",
                "priority": "high",
                "dependencies": [],
            },
            {
                "status": "queued",
                "feature_branch": "med-t",
                "priority": "medium",
                "dependencies": [],
            },
        ]
        result = classify_tickets(tickets)
        slugs = [t["feature_branch"] for t in result["executable"]]
        assert slugs == ["high-t", "med-t", "low-t"]

    def test_priority_then_in_progress(self):
        """Within same priority, in-progress (has steps_completed) comes first."""
        tickets = [
            {
                "status": "queued",
                "feature_branch": "fresh",
                "priority": "medium",
                "dependencies": [],
            },
            {
                "status": "queued",
                "feature_branch": "started",
                "priority": "medium",
                "dependencies": [],
                "steps_completed": ["setup"],
            },
        ]
        result = classify_tickets(tickets)
        slugs = [t["feature_branch"] for t in result["executable"]]
        assert slugs == ["started", "fresh"]

    def test_missing_priority_defaults_medium(self):
        """Tickets without priority field sort as medium."""
        tickets = [
            {"status": "queued", "feature_branch": "no-prio", "dependencies": []},
            {"status": "queued", "feature_branch": "low-t", "priority": "low", "dependencies": []},
            {
                "status": "queued",
                "feature_branch": "high-t",
                "priority": "high",
                "dependencies": [],
            },
        ]
        result = classify_tickets(tickets)
        slugs = [t["feature_branch"] for t in result["executable"]]
        assert slugs == ["high-t", "no-prio", "low-t"]


class TestValidatePriority:
    def _base_fields(self):
        return {
            "summary": "x",
            "type": "feature",
            "branch": "m",
            "scope_current": [],
            "criteria": {
                "mandatory": {"sim_pass": ["tb/foo_tb.sv @ default @ all @ pass -> pass"]}
            },
        }

    def test_valid_priorities_accepted(self):
        for prio in ("low", "medium", "high"):
            fields = {**self._base_fields(), "priority": prio}
            errors = validate_ticket_fields(fields, "## Description\ntext")
            assert not any("priority" in e.lower() for e in errors)

    def test_invalid_priority_rejected(self):
        fields = {**self._base_fields(), "priority": "critical"}
        errors = validate_ticket_fields(fields, "## Description\ntext")
        assert any("Invalid priority" in e for e in errors)

    def test_no_priority_is_valid(self):
        """Omitting priority entirely is fine (defaults to medium at runtime)."""
        fields = self._base_fields()
        errors = validate_ticket_fields(fields, "## Description\ntext")
        assert not any("priority" in e.lower() for e in errors)


class TestFrontmatterPriority:
    def test_priority_roundtrip(self):
        fields = {"summary": "test", "type": "bugfix", "priority": "high"}
        text = format_frontmatter(fields, "## Description\nbody")
        parsed, _body = parse_frontmatter(text)
        assert parsed["priority"] == "high"

    def test_priority_in_field_order(self):
        """Priority appears after dependencies in formatted output."""
        fields = {"summary": "test", "type": "bugfix", "dependencies": [], "priority": "low"}
        text = format_frontmatter(fields, "")
        lines = text.split("\n")
        dep_idx = next(i for i, l in enumerate(lines) if l.startswith("dependencies:"))
        prio_idx = next(i for i, l in enumerate(lines) if l.startswith("priority:"))
        assert prio_idx > dep_idx


class TestScanTicketsPriority:
    def test_priority_copied_from_frontmatter(self, tmp_path):
        """scan_all_tickets picks up priority from ticket files."""
        queue = tmp_path / "board" / "queue"
        queue.mkdir(parents=True)
        ticket = queue / "test-ticket.md"
        ticket.write_text(
            "---\nsummary: test\ntype: bugfix\nbranch: main\n"
            "scope_current:\n  - rtl/x.sv\n"
            "criteria:\n  mandatory:\n    sim_pass:\n"
            "      - tb/tb.sv @ config_a @ all @ pass -> pass\npriority: high\n---\n## Description\ntext\n",
            encoding="utf-8",
        )
        tickets = scan_all_tickets(tmp_path)
        assert len(tickets) == 1
        assert tickets[0]["priority"] == "high"

    def test_missing_priority_not_in_entry(self, tmp_path):
        """Tickets without priority don't get a priority key in the entry."""
        queue = tmp_path / "board" / "queue"
        queue.mkdir(parents=True)
        ticket = queue / "test-ticket.md"
        ticket.write_text(
            "---\nsummary: test\ntype: bugfix\nbranch: main\n"
            "scope_current:\n  - rtl/x.sv\n"
            "criteria:\n  mandatory:\n    sim_pass:\n"
            "      - tb/tb.sv @ config_a @ all @ fail -> pass\n---\n## Description\ntext\n",
            encoding="utf-8",
        )
        tickets = scan_all_tickets(tmp_path)
        assert "priority" not in tickets[0]


# ===========================================================================
# Review fix tests — inline YAML lists, comment stripping, feature_branch
# identity, malformed synthesis data, orphan threshold, board display
# ===========================================================================


class TestInlineYamlLists:
    """CRITICAL #1: parse_frontmatter must handle [a, b, c] inline lists."""

    def test_inline_list_with_values(self):
        text = "---\ndependencies: [dep-a, dep-b, dep-c]\n---\n"
        fields, _ = parse_frontmatter(text)
        assert fields["dependencies"] == ["dep-a", "dep-b", "dep-c"]

    def test_inline_list_single_item(self):
        text = "---\ndependencies: [dep-a]\n---\n"
        fields, _ = parse_frontmatter(text)
        assert fields["dependencies"] == ["dep-a"]

    def test_inline_list_with_spaces(self):
        text = "---\nscope_current: [ rtl/a.sv , rtl/b.sv ]\n---\n"
        fields, _ = parse_frontmatter(text)
        assert fields["scope_current"] == ["rtl/a.sv", "rtl/b.sv"]

    def test_inline_empty_list_still_works(self):
        """Regression: [] must still parse as empty list."""
        text = "---\ndependencies: []\n---\n"
        fields, _ = parse_frontmatter(text)
        assert fields["dependencies"] == []

    def test_inline_list_round_trip(self):
        """Inline list parsed then serialized via format_frontmatter."""
        text = "---\ndeps: [a, b, c]\n---\nBody"
        fields, body = parse_frontmatter(text)
        assert fields["deps"] == ["a", "b", "c"]
        output = format_frontmatter(fields, body)
        fields2, body2 = parse_frontmatter(output)
        assert fields2["deps"] == ["a", "b", "c"]
        assert body2 == "Body"

    def test_inline_list_dependency_resolution(self):
        """End-to-end: inline list deps are properly resolved by classify_tickets."""
        tickets = [
            {"status": "done", "feature_branch": "dep-a"},
            {
                "status": "queued",
                "feature_branch": "child",
                "dependencies": ["dep-a", "dep-b"],
            },  # dep-b not done
        ]
        result = classify_tickets(tickets)
        # child should be waiting because dep-b is not done
        assert len(result["executable"]) == 0
        assert len(result["waiting"]) == 1

    def test_inline_list_with_empty_items_stripped(self):
        """Trailing commas or empty items are filtered out."""
        text = "---\nscope_current: [a, , b]\n---\n"
        fields, _ = parse_frontmatter(text)
        assert fields["scope_current"] == ["a", "b"]


class TestYamlCommentStripping:
    """YAML inline comments must be stripped from values."""

    def test_boolean_with_comment(self):
        text = "---\nsynthesis: false  # skip synthesis stage\n---\n"
        fields, _ = parse_frontmatter(text)
        assert fields["synthesis"] is False

    def test_string_with_comment(self):
        text = "---\nspec: docs/spec.md  # architecture spec\n---\n"
        fields, _ = parse_frontmatter(text)
        assert fields["spec"] == "docs/spec.md"

    def test_integer_with_comment(self):
        text = "---\nmax_debug_rounds: 3  # bumped by triage\n---\n"
        fields, _ = parse_frontmatter(text)
        assert fields["max_debug_rounds"] == 3
        assert isinstance(fields["max_debug_rounds"], int)

    def test_value_with_hash_in_middle(self):
        """A # not preceded by whitespace is part of the value, not a comment."""
        text = "---\nsummary: fix issue#42\n---\n"
        fields, _ = parse_frontmatter(text)
        assert fields["summary"] == "fix issue#42"

    def test_quoted_string_preserves_hash(self):
        """Comments inside quotes are not stripped."""
        text = '---\nsummary: "value  # not a comment"\n---\n'
        fields, _ = parse_frontmatter(text)
        assert fields["summary"] == "value  # not a comment"


class TestFeatureBranchIdentity:
    """CRITICAL #2: scan_all_tickets uses frontmatter feature_branch when set."""

    def test_frontmatter_feature_branch_preferred(self, tmp_path):
        """When feature_branch is in frontmatter, use it over filename stem."""
        active = tmp_path / "board" / "active"
        active.mkdir(parents=True)
        ticket = active / "old-slug-name.md"
        ticket.write_text(
            "---\nsummary: test ticket\ntype: feature\nbranch: main\n"
            "scope_current:\n  - rtl/x.sv\n"
            "criteria:\n  mandatory:\n    sim_pass:\n"
            "      - tb/tb.sv @ config_a @ all @ pass -> pass\n"
            "feature_branch: actual-branch-name\n---\n"
            "## Description\ntext\n",
            encoding="utf-8",
        )
        tickets = scan_all_tickets(tmp_path)
        assert len(tickets) == 1
        assert tickets[0]["feature_branch"] == "actual-branch-name"

    def test_fallback_to_filename_stem(self, tmp_path):
        """Without frontmatter feature_branch, fall back to filename stem."""
        queue = tmp_path / "board" / "queue"
        queue.mkdir(parents=True)
        ticket = queue / "my-ticket.md"
        ticket.write_text(
            "---\nsummary: test\ntype: bugfix\nbranch: main\n"
            "scope_current:\n  - rtl/x.sv\n"
            "criteria:\n  mandatory:\n    sim_pass:\n"
            "      - tb/tb.sv @ config_a @ all @ pass -> pass\n---\n## Description\ntext\n",
            encoding="utf-8",
        )
        tickets = scan_all_tickets(tmp_path)
        assert tickets[0]["feature_branch"] == "my-ticket"

    def test_dependency_resolution_uses_frontmatter_branch(self, tmp_path):
        """classify_tickets resolves deps via feature_branch from frontmatter."""
        done_dir = tmp_path / "board" / "done"
        done_dir.mkdir(parents=True)
        queue_dir = tmp_path / "board" / "queue"
        queue_dir.mkdir(parents=True)

        # Done ticket: filename is "old-name.md" but feature_branch is "real-dep"
        (done_dir / "old-name.md").write_text(
            "---\nsummary: dep ticket\ntype: feature\nbranch: main\n"
            "scope_current:\n  - rtl/x.sv\n"
            "criteria:\n  mandatory:\n    sim_pass:\n"
            "      - tb/tb.sv @ config_a @ all @ pass -> pass\n"
            "feature_branch: real-dep\n---\n"
            "## Description\ntext\n",
            encoding="utf-8",
        )
        # Queued ticket depends on "real-dep"
        (queue_dir / "child-ticket.md").write_text(
            "---\nsummary: child\ntype: bugfix\nbranch: main\n"
            "scope_current:\n  - rtl/y.sv\n"
            "criteria:\n  mandatory:\n    sim_pass:\n"
            "      - tb/tb.sv @ config_a @ all @ fail -> pass\n"
            "dependencies:\n  - real-dep\n---\n"
            "## Description\ntext\n",
            encoding="utf-8",
        )
        tickets = scan_all_tickets(tmp_path)
        result = classify_tickets(tickets)
        # Child should be executable because "real-dep" is done
        assert len(result["executable"]) == 1
        assert result["executable"][0]["feature_branch"] == "child-ticket"


class TestAreaInceaseMalformedData:
    """CRITICAL #3: no_large_area_increase must fail on malformed delta_pct."""

    def test_malformed_delta_fails_gate(self):
        meta = {"targets": [{"delta_pct": "ERROR"}]}
        assert no_large_area_increase(meta) is False

    def test_none_delta_fails_gate(self):
        meta = {"targets": [{"delta_pct": None}]}
        assert no_large_area_increase(meta) is False

    def test_empty_string_delta_fails_gate(self):
        meta = {"targets": [{"delta_pct": ""}]}
        assert no_large_area_increase(meta) is False

    def test_normal_delta_passes(self):
        meta = {"targets": [{"delta_pct": "+5%"}]}
        assert no_large_area_increase(meta) is True

    def test_over_threshold_fails(self):
        meta = {"targets": [{"delta_pct": "+60%"}]}
        assert no_large_area_increase(meta) is False

    def test_na_delta_passes_gate(self):
        """First-run synthesis: no baseline -> delta_pct='N/A' -> skip config, not fail."""
        meta = {"targets": [{"delta_pct": "N/A"}]}
        assert no_large_area_increase(meta) is True

    def test_mixed_na_and_within_threshold(self):
        """One 'N/A' + one small delta = pass (N/A skipped, +10% under threshold)."""
        meta = {
            "targets": [
                {"delta_pct": "N/A"},
                {"delta_pct": "+10%"},
            ]
        }
        assert no_large_area_increase(meta) is True

    def test_mixed_na_and_over_threshold(self):
        """'N/A' is skipped but the other config still triggers the threshold check."""
        meta = {
            "targets": [
                {"delta_pct": "N/A"},
                {"delta_pct": "+60%"},
            ]
        }
        assert no_large_area_increase(meta) is False

    def test_missing_delta_pct_uses_default(self):
        """Missing delta_pct defaults to +0%, which passes."""
        meta = {"targets": [{}]}
        assert no_large_area_increase(meta) is True


class TestOrphanThreshold:
    """Default orphan threshold should be 30 min (aligned with CLI default)."""

    def test_default_threshold_is_30(self):
        """Running ticket at 20 min should NOT be orphaned with default threshold."""
        from datetime import datetime, timedelta

        now = datetime.now(UTC)
        last_update = (now - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
        tickets = [
            {"status": "running", "feature_branch": "x", "last_update": last_update},
        ]
        result = classify_tickets(tickets)
        # 20 min < 30 min threshold → not orphaned
        assert len(result["orphaned"]) == 0

    def test_over_threshold_is_orphaned(self):
        """Running ticket at 150 min should be orphaned."""
        from datetime import datetime, timedelta

        now = datetime.now(UTC)
        last_update = (now - timedelta(minutes=150)).strftime("%Y-%m-%dT%H:%M:%SZ")
        tickets = [
            {"status": "running", "feature_branch": "x", "last_update": last_update},
        ]
        result = classify_tickets(tickets)
        assert len(result["orphaned"]) == 1

    def test_custom_threshold_override(self):
        """Explicit threshold still works."""
        from datetime import datetime, timedelta

        now = datetime.now(UTC)
        last_update = (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        tickets = [
            {"status": "running", "feature_branch": "x", "last_update": last_update},
        ]
        result = classify_tickets(tickets, orphan_threshold_min=5)
        assert len(result["orphaned"]) == 1


class TestBoardDisplayColumns:
    """Ticket Board display: developer-model columns (Last Endpoint, Tools, Criteria)."""

    def test_board_shows_criteria_column(self, capsys):
        """Ticket Board header includes Criteria column."""
        tickets = [
            {
                "file": "queue/test.md",
                "summary": "test",
                "type": "refactor",
                "status": "queued",
                "branch": "main",
                "feature_branch": "test",
                "step": "",
                "steps_completed": [],
                "last_update": "",
            }
        ]
        display_board(tickets)
        output = capsys.readouterr().out
        assert "Criteria" in output

    def test_board_shows_last_endpoint_and_endpoints_headers(self, capsys):
        """Ticket Board header includes Last Endpoint and Tools columns."""
        tickets = [
            {
                "file": "queue/test.md",
                "summary": "test",
                "type": "feature",
                "status": "queued",
                "branch": "main",
                "feature_branch": "test",
                "step": "",
                "steps_completed": [],
                "last_update": "",
            }
        ]
        display_board(tickets)
        output = capsys.readouterr().out
        assert "Last Endpoint" in output
        assert "Endpoints" in output

    def test_board_shows_criteria_progress(self, capsys):
        """Criteria column shows passed/total when criteria data present."""
        tickets = [
            {
                "file": "queue/test.md",
                "summary": "test",
                "type": "feature",
                "status": "running",
                "branch": "main",
                "feature_branch": "test",
                "step": "sim-debug-loop",
                "steps_completed": ["setup", "planning"],
                "last_update": "",
                "criteria_total": 5,
                "criteria_passed": 3,
            }
        ]
        display_board(tickets)
        output = capsys.readouterr().out
        assert "3/5" in output

    def test_board_shows_tools_count(self, capsys):
        """Endpoints column shows count of completed stages (no denominator)."""
        tickets = [
            {
                "file": "queue/test.md",
                "summary": "test",
                "type": "feature",
                "status": "running",
                "branch": "main",
                "feature_branch": "test",
                "step": "implementation",
                "steps_completed": ["setup", "planning", "run-config"],
                "last_update": "",
            }
        ]
        display_board(tickets)
        output = capsys.readouterr().out
        # Should show "3" not "3/18" or similar fraction
        lines = output.strip().split("\n")
        data_line = lines[2]
        assert "3" in data_line
        assert "/18" not in data_line

    def test_board_shows_dash_when_no_criteria(self, capsys):
        """No criteria data → shows --- in the Criteria column."""
        tickets = [
            {
                "file": "queue/test.md",
                "summary": "test",
                "type": "feature",
                "status": "queued",
                "branch": "main",
                "feature_branch": "test",
                "step": "",
                "steps_completed": [],
                "last_update": "",
            }
        ]
        display_board(tickets)
        output = capsys.readouterr().out
        lines = output.strip().split("\n")
        data_line = lines[2]
        cols = [c.strip() for c in data_line.split("│")]
        # Last column is Criteria
        assert cols[-1].strip() == "---"


class TestFeatureBranchFieldOrder:
    """feature_branch should appear in _FM_FIELD_ORDER for stable serialization."""

    def test_feature_branch_serialized_in_order(self):
        fields = {
            "summary": "test",
            "type": "bugfix",
            "feature_branch": "my-branch",
        }
        output = format_frontmatter(fields, "body")
        lines = output.split("\n")
        # feature_branch should come after type in the field ordering
        fb_idx = next(i for i, l in enumerate(lines) if l.startswith("feature_branch:"))
        type_idx = next(i for i, l in enumerate(lines) if l.startswith("type:"))
        # feature_branch should come after type (it's in _FM_FIELD_ORDER after spec fields)
        assert fb_idx > type_idx


# ===========================================================================
# normalize_dir
# ===========================================================================


class TestNormalizeDir:
    def test_bare_name(self):
        assert normalize_dir("queue") == "board/queue"

    def test_already_prefixed(self):
        assert normalize_dir("board/queue") == "board/queue"

    def test_all_dirs(self):
        for bare in [
            "drafts",
            "queue",
            "waiting",
            "active",
            "blocked",
            "review",
            "done",
            "archived",
        ]:
            assert normalize_dir(bare) == f"board/{bare}"


# ===========================================================================
# clear_from_step
# ===========================================================================


class TestClearFromStage:
    def test_from_stage_clears_subsequent(self, tmp_path):
        """from_stage mode clears target + all subsequent stages."""
        tio = make_tio(tmp_path)
        log_dir = tio.logs_dir / "my-ticket"
        log_dir.mkdir(parents=True, exist_ok=True)
        write_stage_file(tio.logs_dir, "my-ticket", "planning", "plan.md", "plan")
        write_stage_file(tio.logs_dir, "my-ticket", "sim-debug-loop", "sim-results.md", "sim data")
        write_stage_file(
            tio.logs_dir, "my-ticket", "synthesis", "synthesis-report.md", "synth data"
        )
        _test_save_step_meta(
            tio.logs_dir,
            "my-ticket",
            {
                "planning": {"clarifying_questions": []},
                "sim-debug-loop": {"converged": True},
                "synthesis": {"targets": []},
            },
        )
        make_progress(
            tio,
            "my-ticket",
            {
                "steps_completed": [
                    "setup",
                    "planning",
                    "implementation",
                    "sim-debug-loop",
                    "synthesis",
                ],
            },
        )

        clear_from_step(tio.logs_dir, "my-ticket", "planning")

        # progress should only have setup
        import json

        progress = json.loads(runtime_file(tio.logs_dir, "my-ticket", "progress.json").read_text())
        assert progress["steps_completed"] == ["setup"]

    def test_preserves_ticket_lock(self, tmp_path):
        """clear_from_step must NOT delete ticket.lock.

        Deleting a lock file on Unix while another process holds an fcntl
        lock on it silently breaks mutual exclusion (new inode). The lock
        is managed by _ticket_lock's context manager instead.
        """
        tio = make_tio(tmp_path)
        log_dir = tio.logs_dir / "my-ticket"
        log_dir.mkdir(parents=True, exist_ok=True)
        lock_path = runtime_file(tio.logs_dir, "my-ticket", "ticket.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("locked")
        make_progress(tio, "my-ticket")

        clear_from_step(tio.logs_dir, "my-ticket", "planning")

        assert lock_path.exists()

    def test_appends_retry_banner(self, tmp_path):
        """clear_from_step should append a retry banner to harness.log."""
        tio = make_tio(tmp_path)
        log_dir = tio.logs_dir / "my-ticket"
        log_dir.mkdir(parents=True, exist_ok=True)
        harness_log = human_log_file(tio.logs_dir, "my-ticket", "harness.log")
        harness_log.parent.mkdir(parents=True, exist_ok=True)
        harness_log.write_text("existing log\n")
        make_progress(tio, "my-ticket")

        clear_from_step(tio.logs_dir, "my-ticket", "sim-debug-loop")

        content = harness_log.read_text()
        assert "=== RETRY from sim-debug-loop" in content

    def test_restores_prerequisite_stages(self, tmp_path):
        """from_stage mode restores prerequisite stages lost by earlier resets."""
        tio = make_tio(tmp_path)
        log_dir = tio.logs_dir / "my-ticket"
        log_dir.mkdir(parents=True, exist_ok=True)
        # run-config (idx=2) is missing from stages_done — lost by
        # an earlier reset — but rtl-review-1 (idx=6) was reached, so it
        # must have been completed.
        make_progress(
            tio,
            "my-ticket",
            {
                "steps_completed": [
                    "setup",
                    "planning",
                    "implementation",
                    "rtl-review-1",
                ],
            },
        )

        clear_from_step(tio.logs_dir, "my-ticket", "rtl-review-1")

        import json

        progress = json.loads(runtime_file(tio.logs_dir, "my-ticket", "progress.json").read_text())
        assert progress["steps_completed"] == [
            "setup",
            "planning",
            "run-config",
            "implementation",
            "implementation-tb",
            "lint-check",
        ]

    def test_does_not_restore_unplanned_stages(self, tmp_path):
        """from_stage mode keeps only planned prerequisite stages."""
        tio = make_tio(tmp_path)
        log_dir = tio.logs_dir / "my-ticket"
        log_dir.mkdir(parents=True, exist_ok=True)
        make_progress(
            tio,
            "my-ticket",
            {
                "steps_completed": [
                    "setup",
                    "planning",
                    "run-config",
                    "sim-debug-loop",
                ],
            },
        )

        # Bugfix planned_stages: only the stages that are actually planned
        planned = {
            "setup",
            "planning",
            "run-config",
            "sim-debug-loop",
            "acceptance-check",
            "summary",
            "review",
        }
        clear_from_step(tio.logs_dir, "my-ticket", "sim-debug-loop", planned_steps=planned)

        import json

        progress = json.loads(runtime_file(tio.logs_dir, "my-ticket", "progress.json").read_text())
        assert "implementation" not in progress["steps_completed"]
        assert progress["steps_completed"] == [
            "setup",
            "planning",
            "run-config",
        ]


class TestOpResetPreservesTicketMd:
    def test_ticket_md_survives_reset(self, tmp_path):
        """op_reset should preserve ticket.md in logs dir."""
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "archived", "my-ticket")
        log_dir = tio.logs_dir / "my-ticket"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "ticket.md").write_text("original ticket")
        write_stage_file(tio.logs_dir, "my-ticket", "planning", "plan.md", "old plan")
        make_progress(tio, "my-ticket", {"failed_step": "sim", "error": "boom"})

        op_reset(tio, "my-ticket")

        assert (log_dir / "ticket.md").exists()
        assert (log_dir / "ticket.md").read_text() == "original ticket"
        # Stage directories should be wiped
        assert not _test_step_dir(tio.logs_dir, "my-ticket", "planning").exists()


class TestOpResetPreservesBlockedMd:
    def test_blocked_md_survives_reset(self, tmp_path):
        """op_reset should preserve blocked.md in logs dir (append-only log)."""
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "archived", "my-ticket")
        log_dir = tio.logs_dir / "my-ticket"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "blocked.md").write_text("# Escalation History\n\nold block\n")
        write_stage_file(tio.logs_dir, "my-ticket", "planning", "plan.md", "old plan")
        make_progress(tio, "my-ticket", {"failed_step": "sim", "error": "boom"})

        op_reset(tio, "my-ticket")

        assert (log_dir / "blocked.md").exists()
        assert "old block" in (log_dir / "blocked.md").read_text()
        assert "Reset Boundary" in (log_dir / "blocked.md").read_text()
        assert not _test_step_dir(tio.logs_dir, "my-ticket", "planning").exists()

    def test_reset_archives_active_runtime_and_human_logs(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "blocked", "my-ticket")
        log_dir = tio.logs_dir / "my-ticket"
        runtime = log_dir / ".runtime"
        human = log_dir / "human-logs"
        runtime.mkdir(parents=True)
        human.mkdir(parents=True)
        (runtime / "booley_state.json").write_text('{"slug":"my-ticket"}')
        (human / "run.log").write_text("stale run")

        op_reset(tio, "my-ticket")

        assert not (runtime / "booley_state.json").exists()
        assert not (human / "run.log").exists()
        archive = log_dir / "runs" / "001"
        assert (archive / ".runtime" / "booley_state.json").exists()
        assert (archive / "human-logs" / "run.log").exists()


class TestOpBoardMove:
    """Test op_board_move enforces valid state transitions."""

    def test_draft_to_queue(self, tmp_path):
        """draft -> queue is a valid transition."""
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "drafts", "my-ticket")
        result = op_board_move(tio, "my-ticket", "queue")
        assert result is True
        _path, status = find_ticket_file(tio.tickets_dir, "my-ticket")
        assert status == "queued"

    def test_blocked_to_queue(self, tmp_path):
        """blocked -> queue is a valid transition (unblock)."""
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "blocked", "my-ticket")
        make_progress(
            tio,
            "my-ticket",
            {
                "blocked_reason": "need info",
                "blocked_step": "planning",
            },
        )
        result = op_board_move(tio, "my-ticket", "queue")
        assert result is True
        _path, status = find_ticket_file(tio.tickets_dir, "my-ticket")
        assert status == "queued"

    def test_review_to_done(self, tmp_path):
        """review -> done is a valid transition."""
        tio = make_tio(tmp_path)
        # on_success: no merge/cleanup to avoid git operations in test
        make_ticket_in_dir(
            tio,
            "review",
            "my-ticket",
            extra_fields={
                "on_success": {"destination": "review", "merge": False, "cleanup": False},
                "target_contract": {
                    "schema": 3,
                    "outer_sha": "a" * 40,
                    "project_sha": "",
                    "surface_digest": "b" * 64,
                    "targets": [],
                    "bindings": [],
                    "surface_entries": [],
                    "participants": [
                        {
                            "role": "outer",
                            "sealed_sha": "a" * 40,
                            "ticket_ref": "refs/heads/my-ticket",
                            "destination_ref": "refs/heads/main",
                            "destination_sha": "c" * 40,
                        }
                    ],
                },
            },
        )
        make_progress(tio, "my-ticket", {"step": "summary"})
        result = op_board_move(tio, "my-ticket", "done")
        assert result is True
        _path, status = find_ticket_file(tio.tickets_dir, "my-ticket")
        assert status == "done"

    def test_running_to_queue(self, tmp_path):
        """running -> queue is a valid transition (requeue)."""
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "active", "my-ticket")
        make_progress(tio, "my-ticket", {"step": "planning"})
        result = op_board_move(tio, "my-ticket", "queue")
        assert result is True
        _path, status = find_ticket_file(tio.tickets_dir, "my-ticket")
        assert status == "queued"

    def test_invalid_transition_returns_false(self, tmp_path):
        """Invalid transitions (e.g. done -> queue) return False."""
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "done", "my-ticket")
        result = op_board_move(tio, "my-ticket", "queue")
        assert result is False
        # Ticket should still be in done/
        _path, status = find_ticket_file(tio.tickets_dir, "my-ticket")
        assert status == "done"

    def test_missing_slug_returns_false(self, tmp_path):
        """Nonexistent slug returns False."""
        tio = make_tio(tmp_path)
        result = op_board_move(tio, "nonexistent", "queue")
        assert result is False


class TestBoardMoveTerminalActionOverrides:
    """``board move <slug> done`` honoring --no-merge/--no-cleanup.

    Both flags used to be accepted, documented in --help, and then silently
    dropped: the worktree was destroyed anyway. These pin the wiring end to
    end plus the two coupling traps (the merge step tears the worktree down
    itself; the cleanup step branches on the merge decision)."""

    @staticmethod
    def _target_contract():
        return {
            "schema": 3,
            "outer_sha": "a" * 40,
            "project_sha": "",
            "surface_digest": "b" * 64,
            "targets": [],
            "bindings": [],
            "surface_entries": [],
            "participants": [
                {
                    "role": "outer",
                    "sealed_sha": "a" * 40,
                    "ticket_ref": "refs/heads/my-ticket",
                    "destination_ref": "refs/heads/main",
                    "destination_sha": "c" * 40,
                }
            ],
        }

    @staticmethod
    def _review_ticket(tmp_path, *, merge: bool, cleanup: bool):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(
            tio,
            "review",
            "my-ticket",
            extra_fields={
                "on_success": {"destination": "review", "merge": merge, "cleanup": cleanup},
                "feature_branch": "feat/my-ticket",
                "target_contract": TestBoardMoveTerminalActionOverrides._target_contract(),
            },
        )
        make_progress(tio, "my-ticket", {"step": "summary"})
        return tio

    def test_no_merge_skips_the_merge_step(self, tmp_path):
        tio = self._review_ticket(tmp_path, merge=True, cleanup=False)
        with patch("booley.ticket_board.completion.complete_review_ticket") as complete:
            assert op_board_move(tio, "my-ticket", "done", no_merge=True) is True
        complete.assert_not_called()

    def test_merge_still_runs_without_the_flag(self, tmp_path):
        tio = self._review_ticket(tmp_path, merge=True, cleanup=False)
        with patch(
            "booley.ticket_board.completion.complete_review_ticket", return_value=True
        ) as complete:
            assert op_board_move(tio, "my-ticket", "done") is True
        assert complete.call_count == 1

    def test_done_ticket_can_retry_journaled_merge_recovery(self, tmp_path):
        from booley.ticket_board.operations import op_complete

        tio = self._review_ticket(tmp_path, merge=True, cleanup=False)
        review, _status = find_ticket_file(tio.tickets_dir, "my-ticket")
        assert review is not None
        done = review.parent.parent / "done" / review.name
        done.parent.mkdir(parents=True, exist_ok=True)
        review.rename(done)

        with patch(
            "booley.ticket_board.completion.complete_review_ticket", return_value=True
        ) as complete:
            assert op_complete(tio, "my-ticket") is True
        complete.assert_called_once()

    def test_feature_branch_alias_uses_canonical_slug_for_terminal_actions(self, tmp_path):
        tio = self._review_ticket(tmp_path, merge=True, cleanup=False)
        with patch(
            "booley.ticket_board.completion.complete_review_ticket", return_value=True
        ) as complete:
            assert op_board_move(tio, "feat/my-ticket", "done") is True
        assert complete.call_args.args[1] == "my-ticket"

    def test_no_cleanup_skips_the_cleanup_step(self, tmp_path):
        tio = self._review_ticket(tmp_path, merge=False, cleanup=True)
        with patch("booley.ticket_board.operations._do_cleanup") as do_cleanup:
            assert op_board_move(tio, "my-ticket", "done", no_cleanup=True) is True
        do_cleanup.assert_not_called()

    def test_no_cleanup_keeps_the_worktree_even_when_merging(self, tmp_path):
        """Trap: the merge step removes the worktree and branch itself, so
        --no-cleanup has to reach into it too."""
        tio = self._review_ticket(tmp_path, merge=True, cleanup=True)
        with (
            patch(
                "booley.ticket_board.completion.complete_review_ticket", return_value=True
            ) as complete,
            patch("booley.ticket_board.operations._do_cleanup") as do_cleanup,
        ):
            assert op_board_move(tio, "my-ticket", "done", no_cleanup=True) is True
        assert complete.call_args.args[2].cleanup is False
        do_cleanup.assert_not_called()

    def test_cleanup_sees_the_effective_merge_decision(self, tmp_path):
        """Trap: _do_cleanup force-deletes the feature branch only when the
        run did NOT merge — so it must read the overridden value, not the
        ticket's frontmatter."""
        tio = self._review_ticket(tmp_path, merge=True, cleanup=True)
        with patch("booley.ticket_board.operations._do_cleanup") as do_cleanup:
            assert op_board_move(tio, "my-ticket", "done", no_merge=True) is True
        assert do_cleanup.call_args.args[2].merge is False

    def test_overrides_never_enable_a_declined_action(self, tmp_path):
        """Subtractive only: a ticket that opted out of merging does not start
        merging because the flags were left off."""
        tio = self._review_ticket(tmp_path, merge=False, cleanup=False)
        with patch("booley.ticket_board.operations._do_cleanup") as do_cleanup:
            assert op_board_move(tio, "my-ticket", "done") is True
        do_cleanup.assert_not_called()

    def test_flags_are_announced_as_ignored_on_other_edges(self, tmp_path, capsys):
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "drafts", "my-ticket")
        assert op_board_move(tio, "my-ticket", "queue", no_cleanup=True) is True
        assert "apply to the review->done move only" in capsys.readouterr().err


class TestArchiveWithSlug:
    """Test op_archive with slug parameter for per-ticket archiving."""

    def test_archive_done_ticket_by_slug(self, tmp_path):
        """Archive a specific done ticket by slug."""
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "done", "t1", extra_fields={"summary": "done ticket"})
        write_stage_file(tio.logs_dir, "t1", "planning", "plan.md", "plan")

        archived = op_archive(tio, slug="t1")
        assert len(archived) == 1
        assert archived[0] == "done ticket"
        assert not (tio.tickets_dir / "board" / "done" / "t1.md").exists()
        assert not (tio.logs_dir / "t1").exists()

    def test_archive_blocked_ticket_refused_without_force(self, tmp_path):
        """A non-done ticket is NOT archived without force (A-5)."""
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "blocked", "t1", extra_fields={"summary": "blocked ticket"})
        write_stage_file(tio.logs_dir, "t1", "planning", "plan.md", "plan")

        archived = op_archive(tio, slug="t1")
        assert archived == []
        assert (tio.tickets_dir / "board" / "blocked" / "t1.md").exists()

    def test_archive_blocked_ticket_by_slug_with_force(self, tmp_path):
        """Archive a specific blocked ticket by slug with force=True."""
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "blocked", "t1", extra_fields={"summary": "blocked ticket"})
        write_stage_file(tio.logs_dir, "t1", "planning", "plan.md", "plan")

        archived = op_archive(tio, slug="t1", force=True)
        assert len(archived) == 1
        assert archived[0] == "blocked ticket"
        assert not (tio.tickets_dir / "board" / "blocked" / "t1.md").exists()

    def test_archive_slug_keep_logs(self, tmp_path):
        """Archive with slug + keep_logs preserves log directory."""
        tio = make_tio(tmp_path)
        make_ticket_in_dir(tio, "done", "t1", extra_fields={"summary": "done ticket"})
        write_stage_file(tio.logs_dir, "t1", "planning", "plan.md", "plan")

        archived = op_archive(tio, slug="t1", keep_logs=True)
        assert len(archived) == 1
        # Ticket file should be gone
        assert not (tio.tickets_dir / "board" / "done" / "t1.md").exists()
        # Logs should be preserved
        assert _test_step_artifact(tio.logs_dir, "t1", "planning", "plan.md").exists()

    def test_archive_slug_not_found(self, tmp_path):
        """Archive nonexistent slug returns empty list."""
        tio = make_tio(tmp_path)
        archived = op_archive(tio, slug="nonexistent")
        assert archived == []


class TestActivateTransitionDetail:
    """fpu F-43: `booley run --ticket <slug>` pre-claims the ticket through
    op_activate before the harness starts, and the harness's own init_ticket
    logs "picked up" moments later. Both said "picked up (resume)" / "picked
    up", so a never-run ticket's transitions.log claimed it had resumed."""

    from booley.ticket_board.operations import op_activate as _op_activate

    @staticmethod
    def _details(tio, slug):
        log = human_log_file(tio.logs_dir, slug, "transitions.log")
        return [ln.split(" | ")[3].strip() for ln in log.read_text(encoding="utf-8").splitlines()]

    def test_never_run_ticket_is_not_called_a_resume(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_file(tio, "queue", "t1")

        assert TestActivateTransitionDetail._op_activate(tio, "t1")

        assert self._details(tio, "t1") == ["claimed for execution"]

    def test_ticket_with_progress_still_reads_as_a_resume(self, tmp_path):
        tio = make_tio(tmp_path)
        make_ticket_file(tio, "queue", "t1")
        # steps_completed is a runtime field: it lives in .runtime/progress.json.
        save_progress(tio.logs_dir, "t1", {"step": "planning", "steps_completed": ["setup"]})

        assert TestActivateTransitionDetail._op_activate(tio, "t1")

        assert self._details(tio, "t1") == ["picked up (resume)"]

    def test_ticket_coming_back_from_blocked_reads_as_a_resume(self, tmp_path):
        """A ticket blocked at setup has no steps_completed, but it IS a resume."""
        tio = make_tio(tmp_path)
        make_ticket_file(tio, "blocked", "t1")

        assert TestActivateTransitionDetail._op_activate(tio, "t1")

        assert self._details(tio, "t1") == ["picked up (resume)"]

    def test_the_harness_pickup_entry_is_unchanged(self, tmp_path):
        """init_ticket still logs the authoritative "picked up" run-start."""
        tio = make_tio(tmp_path)
        make_ticket_file(tio, "queue", "t1")
        TestActivateTransitionDetail._op_activate(tio, "t1")

        tio.init_ticket(tio.tickets_dir / "board" / "active" / "t1.md")

        assert self._details(tio, "t1") == ["claimed for execution", "picked up"]
