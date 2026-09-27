"""The authored Ticket is converted through one strict document boundary."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from booley.runtime.project_dir import reset_cache, resolve_project_dir
from booley.targets.domain import UnknownTargetError
from booley.ticket_board.acceptance_targets import (
    criterion_targets_from_spec,
    validate_ticket_spec_targets,
)
from booley.ticket_board.cli import main
from booley.ticket_board.io import TicketFileSpec, TicketIO
from booley.ticket_board.readiness import check_ticket_ready
from booley.ticket_board.scanner import scan_all_tickets
from booley.ticket_board.ticket_baseline import TicketBaselineError
from booley.ticket_board.ticket_document import (
    TicketAuthoringView,
    TicketConversionContext,
    TicketDocument,
    convert_ticket_document,
    serialize_ticket_document,
    ticket_authoring_view,
    ticket_conversion_context,
)
from booley.ticket_board.validation import validate_ticket_spec
from booley.ticket_board.workspace_ops import prepare_converted_ticket_baseline


def _context() -> TicketConversionContext:
    view = TicketAuthoringView(
        resolve_target=lambda selector, _flow: selector,
        tests_for_target=lambda _target: ("smoke", "regression"),
    )
    return TicketConversionContext("draft", lambda _generated: view)


def _ticket(criteria: str, flags: str = "[merge]") -> str:
    return (
        "---\n"
        "summary: Check core\n"
        "type: feature\n"
        "branch: main\n"
        "scope: [rtl/core.sv]\n"
        f"on_success: {flags}\n"
        "CRITERIA_MANDATORY:\n"
        f"{criteria}"
        "---\n"
        "\n## Description\n\nCheck core.\n"
    )


def test_conversion_expands_sim_and_synth_thresholds() -> None:
    ticket = _ticket(
        "  SIM:\n"
        "    sim_core: {all: pass, smoke: fail -> pass}\n"
        "  SYNTH:\n"
        "    synth_core (new): {area_um2_max: 10000, fmax_mhz_min: 400}\n"
    )
    conversion = convert_ticket_document(ticket, _context())
    assert conversion.diagnostics == ()
    assert conversion.document is not None
    criteria = conversion.document.spec.criteria
    assert len(criteria) == 4
    assert {row.parameter for row in criteria if row.capability == "SYNTH"} == {
        "area_um2_max",
        "fmax_mhz_min",
    }
    assert conversion.document.spec.target_plan is not None
    assert conversion.document.spec.target_plan.entries[0].target == "synth_core"


def test_registered_project_criterion_uses_flow_name_syntax() -> None:
    view = TicketAuthoringView(
        resolve_target=lambda selector, _flow: selector,
        tests_for_target=lambda _target: (),
        project_scalar_criteria=frozenset({"IMPLEMENTATION_DONE"}),
    )
    context = TicketConversionContext("draft", lambda _generated: view)
    ticket = _ticket("  IMPLEMENTATION_DONE: true\n")

    converted = convert_ticket_document(ticket, context)
    assert converted.document is not None
    [criterion] = converted.document.spec.criteria
    assert criterion.capability == "IMPLEMENTATION_DONE"
    assert criterion.identity == "implementation_done"

    unknown = convert_ticket_document(ticket, _context())
    assert unknown.document is None
    assert "Unknown Ticket capabilities" in unknown.diagnostics[0].message

    invalid = convert_ticket_document(ticket.replace(": true", ": false"), context)
    assert invalid.document is None
    assert "must be true" in invalid.diagnostics[0].message


def test_coverage_rejects_zero_threshold() -> None:
    ticket = _ticket("  COVERAGE: {sim_core: {tests: all, metrics: {line: {min_pct: 0}}}}\n")

    converted = convert_ticket_document(ticket, _context())

    assert converted.document is None
    assert "greater than 0" in converted.diagnostics[0].message


def test_v2_serializer_round_trips_nested_criteria_and_policy() -> None:
    context = _context()
    original = convert_ticket_document(
        _ticket(
            "  SIM: {sim_core: {all: pass, smoke: fail -> pass}}\n"
            "  SYNTH:\n"
            "    synth_core (new): {area_um2_max: 10000, fmax_mhz_min: 400}\n",
            flags="[review, merge]",
        ),
        context,
    )
    assert original.document is not None

    rendered = serialize_ticket_document(original.document, context)
    parsed = convert_ticket_document(rendered, context)

    assert parsed.document is not None
    assert parsed.document.spec.semantic_digest() == original.document.spec.semantic_digest()
    assert "CRITERIA_MANDATORY:" in rendered


def test_v2_serializer_keeps_generated_metadata_separate() -> None:
    context = TicketConversionContext("executable", _context().resolve_view)
    text = _ticket("  LINT: {lint_core: clean}\n").replace(
        "on_success: [merge]", "on_success: [merge]\nmachine: {schema: 1}"
    )
    original = convert_ticket_document(text, context)
    assert original.document is not None

    rendered = serialize_ticket_document(original.document, context)
    parsed = convert_ticket_document(rendered, context)

    assert parsed.document is not None
    assert parsed.document.generated == {"machine": {"schema": 1}}
    assert "machine" not in parsed.document.spec.fields
    assert parsed.document.spec.semantic_digest() == original.document.spec.semantic_digest()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("created", "not-a-timestamp", "created must be"),
        ("feature_branch", 42, "feature_branch must be"),
        ("acceptance_amendment", "legacy", "acceptance_amendment"),
    ],
)
def test_executable_conversion_rejects_invalid_generated_metadata(
    field: str, value: object, message: str
) -> None:
    context = TicketConversionContext("executable", _context().resolve_view)
    text = _ticket("  LINT: {lint_core: clean}\n").replace(
        "on_success: [merge]",
        f"on_success: [merge]\nmachine: {{schema: 1}}\n{field}: {value!r}",
    )

    converted = convert_ticket_document(text, context)

    assert converted.document is None
    assert message in converted.diagnostics[0].message


def test_conversion_rejects_coverage_test_suites_that_execution_cannot_combine() -> None:
    ticket = _ticket(
        "  COVERAGE: {sim_core: {tests: [smoke], metrics: {line: {min_pct: 90}}}}\n"
    ).replace(
        "---\n\n## Description",
        "CRITERIA_OPTIONAL:\n"
        "  COVERAGE: {sim_core: {tests: [regression], metrics: {branch: {min_pct: 80}}}}\n"
        "---\n\n## Description",
    )

    converted = convert_ticket_document(ticket, _context())

    assert converted.document is None
    assert "conflicting test suites" in converted.diagnostics[0].message


def test_semantic_digest_detects_authored_drift_but_ignores_machine_metadata() -> None:
    converted = convert_ticket_document(_ticket("  LINT: {lint_core: clean}\n"), _context())
    assert converted.document is not None
    spec = converted.document.spec
    executable = TicketDocument(spec, {"machine": {"schema": 1}})
    assert executable.spec.semantic_digest() == spec.semantic_digest()
    changed = convert_ticket_document(
        _ticket("  LINT: {lint_core: clean}\n", flags="[review]"), _context()
    )
    assert changed.document is not None
    assert changed.document.spec.semantic_digest() != executable.spec.semantic_digest()


def test_conversion_rejects_old_fields() -> None:
    ticket = _ticket("  LINT: {core: clean}\n").replace(
        "on_success: [merge]", "criteria: {mandatory: {lint_clean: [core]}}\non_success: [merge]"
    )
    conversion = convert_ticket_document(ticket, _context())
    assert conversion.document is None
    assert "Old Ticket format" in conversion.diagnostics[0].message


def test_conversion_rejects_duplicate_nested_yaml_key() -> None:
    ticket = _ticket("  SIM:\n    sim_core: {smoke: pass, smoke: pass}\n")
    conversion = convert_ticket_document(ticket, _context())
    assert conversion.document is None
    assert "Duplicate Ticket key" in conversion.diagnostics[0].message
    assert conversion.diagnostics[0].line == 9


@pytest.mark.parametrize(
    ("ticket", "message"),
    [
        ("summary: no fences\n", "must begin with YAML"),
        ("---\nsummary: unterminated\n", "not terminated"),
        ("---\n[]\n---\n## Description\n", "YAML mapping"),
        (
            _ticket("  LINT: {core: clean}\n").replace("summary:", "7: bad\nsummary:"),
            "keys must be strings",
        ),
        (
            _ticket("  LINT: {core: clean}\n").replace(
                "summary: Check core", "summary: &name Check core"
            ),
            "anchors",
        ),
        (
            _ticket("  LINT: {core: clean}\n").replace("summary: Check core", "summary: .nan"),
            "NaN",
        ),
        (
            _ticket("  LINT: {core: clean}\n").replace(
                "scope: [rtl/core.sv]", "scope: 2026-01-01"
            ),
            "unsupported YAML value",
        ),
        (
            _ticket("  LINT: {core: clean}\n").replace("on_success: [merge]", "on_success: merge"),
            "YAML list",
        ),
        (_ticket("  LINT: {core: clean}\n", flags="[merge, merge]"), "duplicate flags"),
        (_ticket("  LINT: {core: clean}\n", flags="[publish]"), "unknown flags"),
        (
            _ticket("  LINT: {core: clean}\n").replace(
                "CRITERIA_MANDATORY:\n  LINT: {core: clean}", "CRITERIA_MANDATORY: []"
            ),
            "must be a mapping",
        ),
        (
            _ticket("  LINT: {core: clean}\n").replace(
                "---\n\n## Description", "CRITERIA_OPTIONAL: []\n---\n\n## Description"
            ),
            "must be a mapping",
        ),
        (
            _ticket("  LINT: {core: clean}\n").replace("  LINT: {core: clean}\n", "  {}\n"),
            "at least one Criterion",
        ),
        (
            _ticket("  LINT: {core: clean}\n").replace(
                "scope: [rtl/core.sv]", "scope: [rtl/core.sv, rtl/core.sv]"
            ),
            "unique names",
        ),
        (
            _ticket("  LINT: {core: clean}\n").replace("type: feature", "type: impossible"),
            "Ticket type",
        ),
        (
            _ticket("  LINT: {core: clean}\n").replace("branch: main", "branch: ''"),
            "nonempty string",
        ),
        (
            _ticket("  LINT: {core: clean}\n").replace("## Description", "## Notes"),
            "Description section",
        ),
        (
            _ticket("  LINT: {core: clean}\n").replace(
                "scope: [rtl/core.sv]", "priority: urgent\nscope: [rtl/core.sv]"
            ),
            "Ticket priority",
        ),
        (
            _ticket("  LINT: {core: clean}\n").replace(
                "scope: [rtl/core.sv]", "spec: 42\nscope: [rtl/core.sv]"
            ),
            "Ticket spec",
        ),
        (
            _ticket("  LINT: {core: clean}\n").replace(
                "scope: [rtl/core.sv]", "base_sha: abc\nscope: [rtl/core.sv]"
            ),
            "retired Ticket fields",
        ),
        (
            _ticket("  LINT: {core: clean}\n").replace(
                "scope: [rtl/core.sv]", "mystery: yes\nscope: [rtl/core.sv]"
            ),
            "Unknown Ticket fields",
        ),
    ],
)
def test_document_boundary_rejects_malformed_authoring(ticket: str, message: str) -> None:
    result = convert_ticket_document(ticket, _context())
    assert result.document is None
    assert message in result.diagnostics[0].message


@pytest.mark.parametrize(
    ("criteria", "message"),
    [
        ("  LINT: []\n", "must map Targets"),
        ("  LINT: {core: pass}\n", "requires 'clean'"),
        ("  ELAB: {core: clean}\n", "requires 'pass'"),
        ("  SIM: {core: {unknown: pass}}\n", "unregistered test"),
        ("  SIM: {core: {all: fail -> pass}}\n", "needs pass"),
        ("  CYCLE_COUNT: {core: {unknown: {cycle_count_max: 100}}}\n", "unregistered test"),
        ("  CYCLE_COUNT: {core: {smoke: {baseline: core}}}\n", "threshold"),
        ("  SYNTH: {core: {baseline: core}}\n", "threshold or scalar pass"),
        ("  SYNTH: {core: {baseline: 9, area_increase_at_most: '10%'}}\n", "baseline must"),
        ("  REVIEW: {unknown: {bugs: done}}\n", "category"),
        ("  REVIEW: {rtl: {unknown: done}}\n", "focus"),
        ("  REVIEW: {rtl: {bugs: [done, done]}}\n", "outcome"),
        ("  ELAB_STANDALONE: []\n", "nonempty Target list"),
        ("  ELAB_STANDALONE: [core, core]\n", "repeats Target"),
        ("  COVERAGE: {core: {tests: all}}\n", "exactly tests and metrics"),
        (
            "  COVERAGE: {core: {tests: [unknown], metrics: {line: {min_pct: 90}}}}\n",
            "registered named tests",
        ),
        (
            "  COVERAGE: {core: {tests: all, metrics: {mystery: {min_pct: 90}}}}\n",
            "unknown metrics",
        ),
        ("  COVERAGE: {core: {tests: all, metrics: {line: {max_pct: 90}}}}\n", "requires min_pct"),
        ("  SYNTH: {core (new): {area_um2_max: 100}}\n", "needs a mandatory Flow Criterion"),
    ],
)
def test_criterion_declarations_fail_at_document_boundary(criteria: str, message: str) -> None:
    ticket = _ticket(criteria)
    if "needs a mandatory" in message:
        ticket = ticket.replace(
            "CRITERIA_MANDATORY:\n" + criteria,
            "CRITERIA_MANDATORY: {}\nCRITERIA_OPTIONAL:\n" + criteria,
        )
    result = convert_ticket_document(ticket, _context())
    assert result.document is None
    assert message in result.diagnostics[0].message


def test_document_boundary_reports_invalid_stage_and_generated_draft_metadata() -> None:
    ticket = _ticket("  LINT: {core: clean}\n")
    bad_stage = convert_ticket_document(
        ticket, TicketConversionContext("unknown", _context().resolve_view)
    )
    assert bad_stage.document is None
    assert "Unknown Ticket conversion stage" in bad_stage.diagnostics[0].message
    stamped = ticket.replace("scope: [rtl/core.sv]", "created: '2026-09-14'\nscope: [rtl/core.sv]")
    generated = convert_ticket_document(stamped, _context())
    assert generated.document is None
    assert "generated execution metadata" in generated.diagnostics[0].message


def test_duplicate_atomic_requirement_across_sections_is_rejected() -> None:
    ticket = _ticket("  LINT: {core: clean}\n").replace(
        "---\n\n## Description",
        "CRITERIA_OPTIONAL:\n  LINT: {core: clean}\n---\n\n## Description",
    )
    result = convert_ticket_document(ticket, _context())
    assert result.document is None
    assert "Duplicate atomic Criterion" in result.diagnostics[0].message


def test_target_aliases_cannot_duplicate_one_flow_requirement() -> None:
    view = TicketAuthoringView(
        lambda selector, _flow: "same-target" if selector in {"first", "second"} else selector,
        lambda _target: ("smoke",),
    )
    context = TicketConversionContext("draft", lambda _generated: view)
    result = convert_ticket_document(_ticket("  LINT: {first: clean, second: clean}\n"), context)
    assert result.document is None
    assert "repeats Target" in result.diagnostics[0].message


@pytest.mark.parametrize(
    ("criteria", "view", "message"),
    [
        (
            "  SIM: {core: {all: pass}}\n",
            TicketAuthoringView(lambda selector, _flow: selector, lambda _target: ()),
            "no registered tests",
        ),
        (
            "  COVERAGE: {core: {tests: all, metrics: {line: {min_pct: 90}}}}\n",
            TicketAuthoringView(lambda selector, _flow: selector, lambda _target: ()),
            "no registered tests",
        ),
        (
            "  ELAB_STANDALONE: [core, 4]\n",
            TicketAuthoringView(lambda selector, _flow: selector, lambda _target: ()),
            "Target selectors must be strings",
        ),
        (
            "  LINT: {core (replaces core): clean}\n",
            TicketAuthoringView(lambda selector, _flow: selector, lambda _target: ()),
            "cannot replace itself",
        ),
    ],
)
def test_resolved_ticket_rejects_invalid_target_or_test_bindings(
    criteria: str, view: TicketAuthoringView, message: str
) -> None:
    result = convert_ticket_document(
        _ticket(criteria), TicketConversionContext("draft", lambda _generated: view)
    )
    assert result.document is None
    assert message in result.diagnostics[0].message


def test_serializer_rejects_generated_metadata_outside_execution_fields() -> None:
    converted = convert_ticket_document(_ticket("  LINT: {core: clean}\n"), _context())
    assert converted.document is not None
    document = TicketDocument(converted.document.spec, {"other": "generated"})
    with pytest.raises(ValueError, match="unsupported generated metadata"):
        serialize_ticket_document(document, _context())


def test_yaml_errors_keep_source_location_and_reject_merge_keys() -> None:
    malformed = convert_ticket_document(_ticket("  SIM: {core: [pass}\n"), _context())
    assert malformed.document is None
    assert malformed.diagnostics[0].line >= 8
    merged = _ticket("  LINT: {core: clean}\n").replace(
        "summary: Check core", "<<: {summary: hidden}\nsummary: Check core"
    )
    result = convert_ticket_document(merged, _context())
    assert result.document is None
    assert "keys must be strings" in result.diagnostics[0].message


def test_standalone_target_resolution_fails_at_conversion_boundary() -> None:
    def resolve(selector: str, _flow: str | None) -> str:
        raise UnknownTargetError(f"Unknown Target {selector}")

    view = TicketAuthoringView(resolve, lambda _target: ())
    result = convert_ticket_document(
        _ticket("  ELAB_STANDALONE: [missing]\n"),
        TicketConversionContext("draft", lambda _generated: view),
    )
    assert result.document is None
    assert "Unknown Target missing" in result.diagnostics[0].message


def test_executable_conversion_rejects_non_mapping_basis_pointer(tmp_path: Path) -> None:
    ticket = _ticket("  LINT: {core: clean}\n").replace(
        "scope: [rtl/core.sv]", "machine: invalid\nscope: [rtl/core.sv]"
    )
    with ticket_conversion_context(tmp_path, "demo", "executable") as context:
        result = convert_ticket_document(ticket, context)
    assert result.document is None
    assert "no valid machine baseline" in result.diagnostics[0].message


def test_serializer_detects_invalid_or_changed_converted_document() -> None:
    converted = convert_ticket_document(_ticket("  LINT: {core: clean}\n"), _context())
    assert converted.document is not None
    spec = converted.document.spec
    invalid = TicketDocument(replace(spec, fields={**spec.fields, "on_success": ["publish"]}), {})
    with pytest.raises(ValueError, match="Serialized Ticket is invalid"):
        serialize_ticket_document(invalid, _context())
    changed_view = TicketAuthoringView(lambda _selector, _flow: "other", lambda _target: ())
    changed_context = TicketConversionContext("draft", lambda _generated: changed_view)
    with pytest.raises(ValueError, match="changed authored or generated meaning"):
        serialize_ticket_document(converted.document, changed_context)


def test_review_done_and_clean_can_have_different_requirements() -> None:
    ticket = _ticket("  REVIEW: {rtl: {bugs: done}}\n")
    ticket = ticket.replace(
        "---\n\n## Description",
        "CRITERIA_OPTIONAL:\n  REVIEW: {rtl: {bugs: clean}}\n---\n\n## Description",
    )
    conversion = convert_ticket_document(ticket, _context())
    assert conversion.diagnostics == ()
    assert conversion.document is not None
    assert {(item.parameter, item.mandatory) for item in conversion.document.spec.criteria} == {
        ("done", True),
        ("clean", False),
    }


@pytest.mark.parametrize("annotation", ("new", "temp", "replaces lint_old"))
def test_annotated_target_requires_merge(annotation: str) -> None:
    ticket = _ticket(f"  LINT: {{lint_core ({annotation}): clean}}\n", flags="[]")
    conversion = convert_ticket_document(ticket, _context())
    assert conversion.document is None
    assert "require on_success.merge" in conversion.diagnostics[0].message


def test_standalone_uses_one_frozen_target_set() -> None:
    ticket = _ticket("  ELAB_STANDALONE: [sim_a, sim_b]\n")
    conversion = convert_ticket_document(ticket, _context())
    assert conversion.diagnostics == ()
    assert conversion.document is not None
    (criterion,) = conversion.document.spec.criteria
    assert criterion.capability == "ELAB_STANDALONE"
    assert criterion.value == ("sim_a", "sim_b")


def test_inconsistent_annotation_on_every_mention_fails() -> None:
    ticket = _ticket(
        "  LINT: {lint_core (new): clean}\n  SYNTH: {lint_core: {area_um2_max: 10000}}\n"
    )
    conversion = convert_ticket_document(ticket, _context())
    assert conversion.document is None
    assert "inconsistent lifecycle annotations" in conversion.diagnostics[0].message


def test_cleanup_without_merge_is_valid_for_existing_targets() -> None:
    ticket = _ticket("  LINT: {lint_core: clean}\n", flags="[cleanup]")
    conversion = convert_ticket_document(ticket, _context())
    assert conversion.diagnostics == ()
    assert conversion.document is not None
    assert conversion.document.spec.on_success == ("cleanup",)


def test_relative_baseline_requires_relative_threshold() -> None:
    ticket = _ticket("  SYNTH: {synth_core: {baseline: synth_old, area_um2_max: 10000}}\n")
    conversion = convert_ticket_document(ticket, _context())
    assert conversion.document is None
    assert "requires a relative threshold" in conversion.diagnostics[0].message


def test_semantic_digest_ignores_flag_order_but_tracks_authored_change() -> None:
    first = convert_ticket_document(
        _ticket("  LINT: {lint_core: clean}\n", flags="[merge, review]"), _context()
    )
    reordered = convert_ticket_document(
        _ticket("  LINT: {lint_core: clean}\n", flags="[review, merge]"), _context()
    )
    changed = convert_ticket_document(
        _ticket("  LINT: {lint_core: clean}\n", flags="[merge]"), _context()
    )
    assert first.document is not None
    assert reordered.document is not None
    assert changed.document is not None
    assert first.document.spec.semantic_digest() == reordered.document.spec.semantic_digest()
    assert first.document.spec.semantic_digest() != changed.document.spec.semantic_digest()


def test_replacement_relative_metric_defaults_to_predecessor() -> None:
    ticket = _ticket(
        "  SYNTH:\n    synth_new (replaces synth_old): {cell_count_reduce_at_least: 10%}\n"
    )
    conversion = convert_ticket_document(ticket, _context())
    assert conversion.diagnostics == ()
    assert conversion.document is not None
    (criterion,) = conversion.document.spec.criteria
    assert criterion.value["baseline"] == "synth_old"


def test_new_target_relative_metric_requires_explicit_existing_baseline() -> None:
    ticket = _ticket("  SYNTH:\n    synth_new (new): {cell_count_reduce_at_least: 10%}\n")
    conversion = convert_ticket_document(ticket, _context())
    assert conversion.document is None
    assert "needs an explicit baseline" in conversion.diagnostics[0].message


def test_cycle_count_baseline_needs_same_registered_test() -> None:
    view = TicketAuthoringView(
        resolve_target=lambda selector, _flow: selector,
        tests_for_target=lambda target: ("smoke",) if target == "sim_new" else ("other",),
    )
    context = TicketConversionContext("draft", lambda _generated: view)
    ticket = _ticket(
        "  CYCLE_COUNT:\n"
        "    sim_new (new):\n"
        "      smoke: {baseline: sim_old, cycle_count_reduce_at_least: 5%}\n"
    )
    conversion = convert_ticket_document(ticket, context)
    assert conversion.document is None
    assert "not registered on baseline Target" in conversion.diagnostics[0].message


def test_only_annotated_missing_target_is_unresolved_draft() -> None:
    def resolve(selector: str, _flow: str | None) -> str:
        raise UnknownTargetError(f"Unknown target {selector!r}")

    view = TicketAuthoringView(resolve, lambda _target: ())
    context = TicketConversionContext("draft", lambda _generated: view)
    annotated = convert_ticket_document(_ticket("  LINT: {lint_new (new): clean}\n"), context)
    ordinary = convert_ticket_document(_ticket("  LINT: {missing: clean}\n"), context)
    assert annotated.diagnostics[0].code == "unresolved_target"
    assert annotated.diagnostics[0].line == 8
    assert ordinary.diagnostics[0].code == "invalid"


def test_missing_new_target_cannot_hide_later_invalid_criterion() -> None:
    def resolve(selector: str, _flow: str | None) -> str:
        raise UnknownTargetError(f"Unknown target {selector!r}")

    context = TicketConversionContext(
        "draft", lambda _generated: TicketAuthoringView(resolve, lambda _target: ())
    )
    ticket = _ticket(
        "  LINT: {lint_new (new): clean}\n  SYNTH: {synth_new (new): {unknown_threshold: 5}}\n"
    )

    conversion = convert_ticket_document(ticket, context)

    assert conversion.document is None
    assert conversion.diagnostics[0].code == "invalid"
    assert "unknown_threshold" in conversion.diagnostics[0].message


def test_relative_baseline_cannot_be_another_ticket_authored_target() -> None:
    ticket = _ticket(
        "  SYNTH:\n"
        "    synth_new (new): {baseline: synth_other, cell_count_reduce_at_least: 10%}\n"
        "    synth_other (new): pass\n"
    )
    conversion = convert_ticket_document(ticket, _context())
    assert conversion.document is None
    assert "authored by this Ticket" in conversion.diagnostics[0].message


def test_executable_ticket_requires_basis_pointer() -> None:
    ticket = _ticket("  LINT: {lint_core: clean}\n")
    draft_context = _context()
    conversion = convert_ticket_document(
        ticket, TicketConversionContext("executable", draft_context.resolve_view)
    )
    assert conversion.document is None
    assert "requires machine baseline metadata" in conversion.diagnostics[0].message


def test_implicit_yaml_date_cannot_enter_semantic_record() -> None:
    ticket = _ticket("  LINT: {lint_core: clean}\n").replace("branch: main", "branch: 2026-09-14")
    conversion = convert_ticket_document(ticket, _context())
    assert conversion.document is None
    assert "unsupported YAML value date" in conversion.diagnostics[0].message


def test_project_view_freezes_canonical_target_identity(tmp_path: Path) -> None:
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "ticket_mode_smoke"
    project = tmp_path / "project"
    shutil.copytree(fixture, project)
    view = ticket_authoring_view(project)
    conversion = convert_ticket_document(
        _ticket("  LINT: {lint_smoke: clean}\n"),
        TicketConversionContext("draft", lambda _generated: view),
    )
    assert conversion.diagnostics == ()
    assert conversion.document is not None
    (criterion,) = conversion.document.spec.criteria
    assert criterion.target is not None
    assert criterion.target.endswith("#lint_smoke")
    assert validate_ticket_spec_targets(conversion.document.spec, project) == []


def test_top_level_field_error_points_at_authored_value() -> None:
    ticket = _ticket("  LINT: {lint_core: clean}\n").replace("scope: [rtl/core.sv]", "scope: 42")
    conversion = convert_ticket_document(ticket, _context())
    assert conversion.document is None
    assert "scope must be a list" in conversion.diagnostics[0].message
    assert conversion.diagnostics[0].line == 5


def test_each_synth_metric_has_its_own_target_binding() -> None:
    ticket = _ticket(
        "  SYNTH:\n    synth_core: {area_um2_max: 10000, cell_count_reduce_at_least: 10%}\n"
    )
    conversion = convert_ticket_document(ticket, _context())
    assert conversion.document is not None
    bindings = criterion_targets_from_spec(conversion.document.spec)
    assert len(bindings) == 2
    assert len({binding.key for binding in bindings}) == 2
    assert {binding.target for binding in bindings} == {"synth_core"}
    assert {binding.baseline for binding in bindings} == {"synth_core"}
    assert {binding.family for binding in bindings} == {None}


def test_v2_validation_uses_converted_sim_criterion(tmp_path: Path) -> None:
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "ticket_mode_smoke"
    project = tmp_path / "project"
    shutil.copytree(fixture, project)
    view = ticket_authoring_view(project)
    conversion = convert_ticket_document(
        _ticket("  SIM: {sim_smoke: {smoke: pass}}\n").replace(
            "scope: [rtl/core.sv]", "scope: [rtl/dut.sv]"
        ),
        TicketConversionContext("draft", lambda _generated: view),
    )
    assert conversion.document is not None
    assert validate_ticket_spec(conversion.document.spec, project_root=project) == []


def test_create_document_preserves_human_ticket_as_source(tmp_path: Path) -> None:
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "ticket_mode_smoke"
    project = tmp_path / "project"
    shutil.copytree(fixture, project)
    ticket = _ticket("  LINT: {lint_smoke: clean}\n")
    board = TicketIO(project / ".booley_project" / "tickets", project_root=project)

    created = board.create_ticket_document("new-format", ticket)

    assert created is not None
    assert created.read_text(encoding="utf-8") == ticket


def test_create_document_rejects_old_ticket_shape(tmp_path: Path) -> None:
    ticket = _ticket("  LINT: {lint_smoke: clean}\n").replace("CRITERIA_MANDATORY:", "criteria:")
    board = TicketIO(tmp_path / "tickets", project_root=tmp_path)

    assert board.create_ticket_document("old-format", ticket) is None
    assert not (tmp_path / "tickets" / "board" / "drafts" / "old-format.md").exists()


def test_create_document_rejects_invalid_slug_and_duplicate_without_overwrite(
    tmp_path: Path,
) -> None:
    (tmp_path / ".booley_project").mkdir()
    board = TicketIO(tmp_path / ".booley_project" / "tickets", project_root=tmp_path)
    ticket = _ticket("  REVIEW: {rtl: {bugs: done}}\n")
    assert board.create_ticket_document("../escape", ticket) is None
    assert board.create_ticket_document("x" * 81, ticket) is None
    created = board.create_ticket_document("review", ticket)
    assert created is not None and created.read_text() == ticket
    assert board.create_ticket_document("review", ticket.replace("Check core", "Changed")) is None
    assert created.read_text() == ticket
    with pytest.raises(ValueError, match="field-based Ticket creation is unsupported"):
        board.create_ticket_file("old", TicketFileSpec("Legacy", "verification", "main"))


def test_create_document_reports_atomic_file_creation_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from booley.ticket_board import io

    (tmp_path / ".booley_project").mkdir()
    board = TicketIO(tmp_path / ".booley_project" / "tickets", project_root=tmp_path)
    monkeypatch.setattr(io, "atomic_write_once", lambda *_args, **_kwargs: False)
    assert board.create_ticket_document("race", _ticket("  REVIEW: {rtl: {bugs: done}}\n")) is None
    assert not (board.tickets_dir / "board" / "drafts" / "race.md").exists()


def test_execution_start_rejects_unbound_ticket_without_moving_it(tmp_path: Path) -> None:
    (tmp_path / ".booley_project").mkdir()
    board = TicketIO(tmp_path / ".booley_project" / "tickets", project_root=tmp_path)
    queue = board.tickets_dir / "board" / "queue"
    queue.mkdir(parents=True)
    ticket = queue / "unbound.md"
    ticket.write_text(_ticket("  REVIEW: {rtl: {bugs: done}}\n"))
    assert board.init_ticket(ticket) is None
    assert ticket.exists()
    assert not (board.tickets_dir / "board" / "active" / "unbound.md").exists()


def test_create_file_cli_accepts_complete_document(tmp_path: Path, monkeypatch) -> None:
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "ticket_mode_smoke"
    project = tmp_path / "project"
    shutil.copytree(fixture, project)
    ticket = _ticket("  LINT: {lint_smoke: clean}\n")
    source = tmp_path / "ticket.md"
    source.write_text(ticket, encoding="utf-8")
    monkeypatch.setenv("TICKETS_DIR", str(project / ".booley_project" / "tickets"))

    assert main(["create-file", "v2-example", "--document-file", str(source)]) == 0
    saved = project / ".booley_project" / "tickets" / "board" / "drafts" / "v2-example.md"
    assert saved.read_text(encoding="utf-8") == ticket


def test_create_document_opens_git_authoring_workspace(tmp_path: Path) -> None:
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "ticket_mode_smoke"
    project = tmp_path / "project"
    shutil.copytree(fixture, project)
    for command in (
        ("init", "-q", "-b", "main"),
        ("config", "user.name", "Test"),
        ("config", "user.email", "test@example.invalid"),
        ("add", "."),
        ("add", "-f", ".booley_project/booley.toml", ".booley_project/.gitignore"),
        ("commit", "-qm", "baseline"),
    ):
        subprocess.run(["git", *command], cwd=project, check=True, capture_output=True)
    ticket = _ticket("  LINT: {lint_smoke: clean}\n")
    board = TicketIO(project / ".booley_project" / "tickets", project_root=project)

    created = board.create_ticket_document("git-v2", ticket)

    assert created is not None
    assert (resolve_project_dir(project) / "worktrees" / "git-v2").is_dir()


def test_v2_basis_publication_uses_converted_spec(tmp_path: Path, monkeypatch) -> None:
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "ticket_mode_smoke"
    project = tmp_path / "project"
    shutil.copytree(fixture, project)
    monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(project / ".booley_project"))
    reset_cache()
    for command in (
        ("init", "-q", "-b", "main"),
        ("config", "user.name", "Test"),
        ("config", "user.email", "test@example.invalid"),
        ("add", "."),
        ("add", "-f", ".booley_project/booley.toml", ".booley_project/.gitignore"),
        ("commit", "-qm", "baseline"),
    ):
        subprocess.run(["git", *command], cwd=project, check=True, capture_output=True)
    ticket = _ticket("  REVIEW: {rtl: {bugs: done}}\n").replace(
        "scope: [rtl/core.sv]", "scope: [README.md]"
    )
    board = TicketIO(project / ".booley_project" / "tickets", project_root=project)
    created = board.create_ticket_document("basis-v2", ticket)
    assert created is not None

    basis, _operation_id = prepare_converted_ticket_baseline(project, created, "basis-v2")
    assert basis.outer_sha

    assert board.enqueue_ticket("basis-v2")
    queued = board.tickets_dir / "board" / "queue" / "basis-v2.md"
    assert queued.is_file()
    with ticket_conversion_context(project, "basis-v2", "executable") as context:
        conversion = convert_ticket_document(queued.read_text(encoding="utf-8"), context)
    assert conversion.document is not None
    assert (
        conversion.document.generated["machine"]["authored_sha256"]
        == conversion.document.spec.semantic_digest()
    )
    assert board.load_basis("basis-v2").as_dict() == basis.as_dict()
    assert check_ticket_ready(project, "basis-v2").errors == ()
    found = board.find_ticket("basis-v2")
    assert found is not None
    assert found["on_success"]["merge"] is True
    assert found["on_success"]["cleanup"] is False
    [entry] = scan_all_tickets(board.tickets_dir, project_root=project)
    assert entry["status"] == "queued"
    assert entry["criteria"][0]["capability"] == "REVIEW"

    old = board.tickets_dir / "board" / "drafts" / "old-format.md"
    old.write_text("---\ncriteria: {mandatory: {lint_clean: [core]}}\n---\n")
    entries = scan_all_tickets(board.tickets_dir, project_root=project)
    assert len(entries) == 2
    assert any("Old Ticket format" in item.get("ticket_error", "") for item in entries)

    queued.write_text(
        queued.read_text(encoding="utf-8").replace("- merge\n", "- review\n"),
        encoding="utf-8",
    )
    with pytest.raises(TicketBaselineError, match="authored Ticket changed"):
        board.load_basis("basis-v2")
