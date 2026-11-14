"""Architecture guard: Simulation Record names say "record", not "campaign".

The Simulation Record package and every other source, test and QA module name the concept
with Record names. A name that still says campaign names another concept and is listed
below with its reason; it is allowed only in the file, and for scoped entries the function or
class, that is listed.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.source_checkout_support import source_checkout_files

_ROOT = Path(__file__).resolve().parents[2]
_RECORD_PACKAGE = _ROOT / "src" / "booley" / "flows" / "sim" / "record"
_FORBIDDEN = "campaign"


class _DefinedNames(ast.NodeVisitor):
    """Collect (line, scope, name) for every identifier a module defines.

    *scope* is the dotted name of the enclosing classes and functions (the function or
    class itself for its own name and parameters), or <module> at the top level.
    """

    def __init__(self) -> None:
        self.found: list[tuple[int, str, str]] = []
        self._stack: list[str] = []

    def _add(self, line: int, name: str) -> None:
        self.found.append((line, ".".join(self._stack) or "<module>", name))

    def _add_arguments(self, arguments: ast.arguments) -> None:
        every_argument = [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]
        if arguments.vararg is not None:
            every_argument.append(arguments.vararg)
        if arguments.kwarg is not None:
            every_argument.append(arguments.kwarg)
        for argument in every_argument:
            self._add(argument.lineno, argument.arg)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._stack.append(node.name)
        self._add(node.lineno, node.name)
        self.generic_visit(node)
        self._stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._stack.append(node.name)
        self._add(node.lineno, node.name)
        self._add_arguments(node.args)
        self.generic_visit(node)
        self._stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._stack.append(node.name)
        self._add(node.lineno, node.name)
        self._add_arguments(node.args)
        self.generic_visit(node)
        self._stack.pop()

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._add_arguments(node.args)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store):
            self._add(node.lineno, node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Store):
            self._add(node.lineno, node.attr)
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name is not None:
            self._add(node.lineno, node.name)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.asname:
                self._add(node.lineno, alias.asname)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.asname:
                self._add(node.lineno, alias.asname)

    def _add_exports(self, targets: list[ast.expr], value: ast.expr | None) -> None:
        if any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in targets
        ) and isinstance(value, (ast.List, ast.Tuple)):
            for element in value.elts:
                if isinstance(element, ast.Constant) and isinstance(element.value, str):
                    self._add(element.lineno, element.value)

    def visit_Assign(self, node: ast.Assign) -> None:
        self._add_exports(node.targets, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._add_exports([node.target], node.value)
        self.generic_visit(node)


def _defined_names(tree: ast.AST) -> list[tuple[int, str, str]]:
    """Return (line, scope, name) for every identifier a module defines."""
    collector = _DefinedNames()
    collector.visit(tree)
    return collector.found


# Coverage Record names (the artifact itself, its model, store, reference, picker, waivers and
# its tests); #3912 and #3913 rename them.
_COVERAGE_RECORD_NAMES: dict[str, frozenset[str]] = {
    "qa/shared/coverage/evaluator/measurements.py": frozenset(
        {
            "CAMPAIGN_SCHEMA",
            "read_campaign",
        }
    ),
    "qa/shared/coverage/faults/approvals.py": frozenset(
        {
            "bind:campaign",
        }
    ),
    "qa/shared/coverage/faults/campaign.py": frozenset(
        {
            "nested_campaign",
            "rebind_reference:campaign",
        }
    ),
    "src/booley/flows/sim/acceptance.py": frozenset(
        {
            "_coverage_detail:campaign",
        }
    ),
    "src/booley/flows/sim/coverage_analysis_input.py": frozenset(
        {
            "campaign",
            "read_coverage_campaign",
        }
    ),
    "src/booley/flows/sim/coverage_campaign.py": frozenset(
        {
            "CoverageCampaign",
            "CoverageCampaignValidationError",
            "_decode_valid_campaign",
            "campaign",
            "campaign_id",
            "encode_coverage_campaign",
        }
    ),
    "src/booley/flows/sim/coverage_campaign_store.py": frozenset(
        {
            "CAMPAIGN_SCHEMA",
            "CampaignPaths",
            "CoverageCampaignStoreError",
            "CoverageCampaignSummary",
            "LoadedCoverageCampaign",
            "_canonical_campaign_path",
            "campaign",
            "campaign_path",
            "decode_coverage_campaign",
            "load_coverage_campaign",
            "load_coverage_campaign_bytes",
            "publish_coverage_campaign",
        }
    ),
    "src/booley/flows/sim/coverage_evidence.py": frozenset(
        {
            "_campaign",
            "campaign",
        }
    ),
    "src/booley/flows/sim/coverage_json.py": frozenset(
        {
            "CAMPAIGN_FINGERPRINT_JSON",
        }
    ),
    "src/booley/flows/sim/coverage_policy.py": frozenset(
        {
            "EvaluatedCoverageCampaign",
            "_blocked_campaign",
            "_scored_campaign",
            "_ungated_campaign",
            "campaign",
            "evaluate_coverage_campaign",
        }
    ),
    "src/booley/flows/sim/coverage_provisional.py": frozenset(
        {
            "_campaign_sources",
            "campaign",
            "campaign_id",
        }
    ),
    "src/booley/flows/sim/coverage_reference.py": frozenset(
        {
            "CoverageCampaignReference",
            "CoverageCampaignReferenceError",
            "ResolvedCoverageCampaign",
            "_resolve_coverage_campaign_reference_bytes",
            "authenticate_coverage_campaign_owner",
            "build_coverage_campaign_reference",
            "campaign",
            "campaign_path",
            "campaign_raw",
            "coverage_campaign_path",
            "decode_coverage_campaign_reference",
            "encode_coverage_campaign_reference",
            "publish_coverage_campaign_reference",
            "resolve_coverage_campaign_reference",
            "resolve_persisted_coverage_campaign_reference",
        }
    ),
    "src/booley/flows/sim/coverage_source_gaps.py": frozenset(
        {
            "campaign",
        }
    ),
    "src/booley/flows/sim/coverage_transaction.py": frozenset(
        {
            "_campaign",
            "campaign",
            "campaign_path",
            "campaign_published",
            "record_campaign_publication",
        }
    ),
    "src/booley/flows/sim/coverage_waivers.py": frozenset(
        {
            "_match_campaign",
            "campaign",
        }
    ),
    "src/booley/flows/sim/flow.py": frozenset(
        {
            "SimulateFlow._simulation_record_endpoint_outcome:campaign",
            "SimulateFlow._simulation_record_endpoint_outcome:coverage_campaigns",
            "SimulateFlow._with_coverage_campaign_lines:_with_coverage_campaign_lines",
        }
    ),
    "src/booley/flows/sim/flow_resume.py": frozenset(
        {
            "_checkpoint_coverage_campaign:_checkpoint_coverage_campaign",
        }
    ),
    "src/booley/flows/sim/metrics.py": frozenset(
        {
            "campaign",
            "coverage_campaign_path",
        }
    ),
    "src/booley/flows/sim/record_presentation.py": frozenset(
        {
            "_coverage_campaign_exit_code:_coverage_campaign_exit_code",
            "_coverage_campaign_exit_code:campaign",
            "_coverage_campaign_exit_code:coverage_campaigns",
            "_coverage_campaign_lines:_coverage_campaign_lines",
            "_coverage_compatibility_targets:campaign",
            "_coverage_compatibility_targets:coverage_campaigns",
            "_coverage_metrics_line:campaign",
            "_coverage_report_suffix:campaign",
            "_publication_failure_detail:campaign",
            "_resolved_coverage_campaigns:_resolved_coverage_campaigns",
            "_resolved_coverage_campaigns:campaigns",
            "_retain_coverage_campaign:_retain_coverage_campaign",
            "_retain_coverage_campaign:campaign",
            "_simulation_record_report_blocks:campaign",
            "_simulation_record_report_blocks:coverage_campaigns",
            "_simulation_record_report_lines:coverage_campaigns",
            "_waiver_block_hint_lines:campaign",
        }
    ),
    "src/booley/flows/sim/record_retention.py": frozenset(
        {
            "_availability_document:campaign",
            "_campaign_coverage_files:_campaign_coverage_files",
            "_campaign_coverage_files:campaign_path",
            "_native_artifacts:campaign",
            "_target:campaign",
        }
    ),
    "src/booley/flows/sim/waiver_candidates.py": frozenset(
        {
            "CampaignBinding",
            "campaign_id",
            "campaign_path",
        }
    ),
    "src/booley/goals/waiver_binding.py": frozenset(
        {
            "_campaign_reference",
            "campaign_id",
            "campaign_path",
            "campaign_reference",
        }
    ),
    "src/booley/harness/coverage_cli.py": frozenset(
        {
            "campaign",
        }
    ),
    "src/booley/harness/dashboard/specialist_form.py": frozenset(
        {
            "CampaignChoice",
            "NO_CAMPAIGNS",
            "TYPE_CAMPAIGN",
            "_CAMPAIGN",
            "finished_campaigns",
        }
    ),
    "src/booley/harness/dashboard/wizard.py": frozenset(
        {
            "CAMPAIGNS_GROUP",
            "LOADING_CAMPAIGNS",
            "_campaign_picker",
            "_campaigns",
            "_campaigns_read",
            "_campaigns_worker",
            "_open_campaigns",
        }
    ),
    "src/booley/mcp/catalog.py": frozenset(
        {
            "campaign",
        }
    ),
    "src/booley/mcp/coverage_evidence.py": frozenset(
        {
            "campaign_path",
        }
    ),
    "src/booley/mcp/goal_waivers.py": frozenset(
        {
            "_validate_campaign",
            "campaign",
        }
    ),
    "src/booley/specialists/coverage_analysis.py": frozenset(
        {
            "analyze_coverage_campaign",
            "campaign",
            "model_campaign",
        }
    ),
    "src/booley/specialists/coverage_analyst.py": frozenset(
        {
            "_evidence_campaign_path",
            "analyze_coverage_campaign",
            "campaign",
            "campaign_path",
        }
    ),
    "src/booley/specialists/coverage_candidate_recording.py": frozenset(
        {
            "campaign",
            "campaign_path",
        }
    ),
    "src/booley/specialists/coverage_input.py": frozenset(
        {
            "read_coverage_campaign",
        }
    ),
    "tests/docs/test_flow_reference.py": frozenset(
        {
            "test_coverage_card_example_matches_rendered_card:campaign",
        }
    ),
    "tests/flows/sim/coverage_campaign_test_support.py": frozenset(
        {
            "campaign_path",
            "corrupt_campaign_with_duplicate_negative_point",
        }
    ),
    "tests/flows/sim/coverage_scale_fixture.py": frozenset(
        {
            "scale_campaign:scale_campaign",
        }
    ),
    "tests/flows/sim/simulation_record_test_support.py": frozenset(
        {
            "CoverageInvocation.campaign_path:campaign_path",
        }
    ),
    "tests/flows/sim/test_coverage_campaign.py": frozenset(
        {
            "campaign",
            "first_campaign",
            "second_campaign",
            "test_campaign_rejects_unknown_collection_status",
            "test_decoded_campaign_is_deeply_immutable",
            "test_decoder_validates_campaign_state_sections_before_semantic_checks",
            "test_decoder_validates_diagnostic_shapes_at_the_campaign_boundary",
            "test_evaluated_campaign_preserves_exact_metric_evidence",
            "test_gated_campaign_requires_approved_waiver_set_digest",
            "test_invalid_campaign_message_is_bounded_and_control_safe",
            "test_invalid_campaign_message_names_all_point_defects_in_stable_order",
            "test_known_literal_decodes_to_immutable_campaign_and_encodes_canonically",
            "test_noncomplete_campaign_retains_points_without_rollups",
        }
    ),
    "tests/flows/sim/test_coverage_campaign_reference.py": frozenset(
        {
            "_nested_campaign",
            "campaign",
            "test_reference_codec_refuses_a_nested_v4_campaign_as_an_older_booley_record",
            "test_resolver_authenticates_real_nested_campaign_at_exact_attempt_path",
            "test_resolver_propagates_nested_campaign_validation_findings",
        }
    ),
    "tests/flows/sim/test_coverage_campaign_store.py": frozenset(
        {
            "_campaign",
            "_evaluated_campaign",
            "_invalid_campaign",
            "_large_campaign",
            "_publish_campaign_as_written_by_booley_0_3_0",
            "_source_campaign",
            "campaign",
            "large_campaign",
            "test_a_campaign_written_by_booley_0_3_0_is_refused_as_older",
            "test_a_scored_campaign_written_before_the_change_reloads_unchanged",
            "test_deep_load_validates_the_campaign_once",
            "test_large_campaign_keeps_manifest_and_summary_work_constant",
            "test_publish_never_replaces_existing_campaign_artifact",
            "test_readers_refuse_every_coverage_campaign_booley_0_4_0_wrote_as_older",
            "test_unsupported_schema_is_rejected_before_campaign_decode",
        }
    ),
    "tests/flows/sim/test_coverage_canonical_forms.py": frozenset(
        {
            "_campaign_document",
            "campaign",
            "test_coverage_campaign_fingerprint_golden",
            "test_coverage_campaign_reference_golden",
        }
    ),
    "tests/flows/sim/test_coverage_flow.py": frozenset(
        {
            "_assert_card_names_run_files:campaign_file",
            "_assert_enclosing_result_is_authenticated:campaign_path",
            "_assert_projection_tamper_rejected:campaign_path",
            "_coverage_evaluation:campaign",
            "_coverage_evaluation_status:campaign",
            "test_coverage_analysis_refuses_a_campaign_written_by_booley_0_3_0:campaign_path",
            "test_coverage_analysis_refuses_a_campaign_written_by_booley_0_3_0:test_coverage_analysis_refuses_a_campaign_written_by_booley_0_3_0",
            "test_coverage_publication_crash_resumes_at_the_aggregate_boundary:nested_campaigns",
            "test_flow_produces_numbered_target_reports_with_public_coverage_input:campaign_path",
            "test_interactive_collection_keeps_criteria_unchanged_across_verdicts:campaign_path",
            "test_interactive_collection_then_exact_campaign_analysis:campaign",
            "test_interactive_collection_then_exact_campaign_analysis:test_interactive_collection_then_exact_campaign_analysis",
            "test_job_report_coverage_metrics_match_campaign:campaign",
            "test_job_report_coverage_metrics_match_campaign:test_job_report_coverage_metrics_match_campaign",
            "test_multi_target_collector_error_preserves_completed_and_later_targets:campaign_path",
            "test_symlinked_booley_dir_publishes_and_analyses_coverage:campaign_path",
        }
    ),
    "tests/flows/sim/test_coverage_policy.py": frozenset(
        {
            "_campaign",
            "_two_metric_campaign",
            "campaign",
            "test_blocked_gated_campaign_still_resolves_approved_dispositions",
            "test_fingerprint_is_independent_of_campaign_storage_version",
            "test_ungated_campaign_applies_matched_waivers",
            "test_ungated_campaign_blocks_on_unmatched_waiver",
            "test_ungated_campaign_is_not_requested_and_preserves_observations",
        }
    ),
    "tests/flows/sim/test_coverage_provisional.py": frozenset(
        {
            "_CAMPAIGN_ID",
            "campaign",
            "test_candidates_are_rederived_from_the_campaign",
        }
    ),
    "tests/flows/sim/test_coverage_record_names.py": frozenset(
        {
            "campaign",
        }
    ),
    "tests/flows/sim/test_coverage_source_gaps.py": frozenset(
        {
            "_campaign",
            "campaign",
        }
    ),
    "tests/flows/sim/test_coverage_test_runs.py": frozenset(
        {
            "test_two_variants_and_a_seed_count_are_one_process_each:campaign",
        }
    ),
    "tests/flows/sim/test_coverage_transaction.py": frozenset(
        {
            "_assert_invalid_hook_campaign",
            "campaign",
            "test_ungated_target_persists_valid_campaign_and_independent_simulation",
            "test_unknown_native_records_remain_in_canonical_campaign",
        }
    ),
    "tests/flows/sim/test_coverage_waiver_promotion.py": frozenset(
        {
            "test_promoted_files_load_cleanly_and_waive_the_campaign_point",
        }
    ),
    "tests/flows/sim/test_coverage_waivers.py": frozenset(
        {
            "_campaign",
            "campaign",
        }
    ),
    "tests/flows/sim/test_endpoint_record_lifecycle.py": frozenset(
        {
            "test_coverage_cancellation_keeps_completed_report_only_counts:campaign_file",
        }
    ),
    "tests/flows/sim/test_sim_flow_metrics.py": frozenset(
        {
            "test_unreadable_coverage_campaign_produces_no_metrics:test_unreadable_coverage_campaign_produces_no_metrics",
        }
    ),
    "tests/flows/sim/test_simulation_output_record_names.py": frozenset(
        {
            "test_coverage_analysis_scratch_and_cancellation_thread_use_record_names:campaign",
            "test_coverage_analyst_refuses_the_old_campaign_input:test_coverage_analyst_refuses_the_old_campaign_input",
        }
    ),
    "tests/flows/sim/test_simulation_record_reports.py": frozenset(
        {
            "_coverage_card:campaign",
            "test_coverage_report_keeps_nonpassing_status_without_optional_detail:campaign",
            "test_cycle_card_preserves_complete_reason_strings:_campaign",
            "test_resume_count_budget_uses_only_actual_current_hook_keys.inspect:coverage_campaigns",
        }
    ),
    "tests/flows/sim/test_simulation_record_retention.py": frozenset(
        {
            "test_native_pruning_preserves_normalized_campaign_and_records_availability:test_native_pruning_preserves_normalized_campaign_and_records_availability",
        }
    ),
    "tests/flows/sim/test_simulation_record_unsupported_formats.py": frozenset(
        {
            "test_a_schema_less_projection_cannot_authenticate_a_coverage_campaign:test_a_schema_less_projection_cannot_authenticate_a_coverage_campaign",
            "test_full_prune_cli_refuses_an_unsupported_nested_coverage_campaign_naming_its_directory:test_full_prune_cli_refuses_an_unsupported_nested_coverage_campaign_naming_its_directory",
        }
    ),
    "tests/flows/sim/test_waiver_candidates.py": frozenset(
        {
            "campaign",
            "test_different_campaign_replaces_entry_and_resets_count",
            "test_same_campaign_bumps_count_and_replaces_justification",
        }
    ),
    "tests/goals/test_coverage_candidates.py": frozenset(
        {
            "campaign",
            "test_goal_candidate_record_retains_campaign_target_under_bound_record",
        }
    ),
    "tests/goals/test_goal_waivers.py": frozenset(
        {
            "campaign",
            "fixture_campaign",
            "persist_campaign",
            "test_candidate_campaign_mismatch_is_ineligible",
            "test_public_relax_then_waive_preserves_current_bounds_on_reloaded_campaign",
        }
    ),
    "tests/harness/test_coverage_cli.py": frozenset(
        {
            "_not_a_campaign",
            "campaign",
        }
    ),
    "tests/harness/test_dashboard_run_specialist.py": frozenset(
        {
            "CAMPAIGN_ENDED",
            "test_coverage_analyst_launches_a_picked_or_typed_campaign",
            "test_coverage_analyst_without_campaigns_explains_how_to_make_one",
            "test_reopening_the_picker_keeps_an_older_campaign_chosen",
            "type_campaign",
            "write_campaign",
        }
    ),
    "tests/harness/test_dashboard_specialist_form.py": frozenset(
        {
            "test_an_unreadable_campaign_does_not_hide_the_readable_ones",
            "test_finished_campaigns_are_listed_newest_first_with_target_and_date",
            "write_campaign",
        }
    ),
    "tests/mcp_tools/test_coverage_analysis.py": frozenset(
        {
            "campaign",
            "test_exact_eligible_campaign_produces_versioned_advisory_report",
            "test_incomplete_usable_campaign_keeps_independent_simulation_truth",
            "test_unusable_campaign_rejects_before_model_invocation",
        }
    ),
    "tests/mcp_tools/test_coverage_analyst.py": frozenset(
        {
            "_campaign",
            "campaign",
            "campaign_path",
            "persist_campaign",
            "persist_large_current_campaign",
            "test_exact_campaign_wrapper_analyzes_without_native_payload_or_project_state",
            "test_image_alias_analyst_binds_canonical_campaign",
            "test_large_current_campaign_uses_scoped_evidence_tool_without_oversized_prompt",
            "test_persisted_current_analysis_does_not_encode_the_complete_campaign",
            "test_relative_campaign_resolves_against_selected_work_dir",
        }
    ),
    "tests/mcp_tools/test_coverage_candidate_recording.py": frozenset(
        {
            "campaign",
            "test_campaign_outside_the_record_flow_reports_is_refused",
            "test_image_alias_reports_root_records_canonical_campaign",
            "test_legacy_campaign_without_point_store_is_refused",
        }
    ),
    "tests/mcp_tools/test_coverage_evidence.py": frozenset(
        {
            "_campaign_with_source_gaps",
            "_campaign_with_waiver",
            "_mixed_campaign",
            "campaign",
            "campaign_path",
            "test_bound_tool_deep_loads_campaign_and_publishes_audit",
            "test_source_view_is_limited_to_exact_campaign_point_ids",
        }
    ),
    "tests/performance/coverage_query_profile.py": frozenset(
        {
            "campaign_path",
        }
    ),
    "tests/qa/test_coverage_measurements.py": frozenset(
        {
            "half_campaign",
            "test_oracle_reads_campaign_the_product_publishes",
        }
    ),
    "tests/smoke/test_scaffold_sim_smoke.py": frozenset(
        {
            "_assert_coverage:campaign",
        }
    ),
    "tests/smoke/test_verilator_coverage_collector_smoke.py": frozenset(
        {
            "_assert_two_run_coverage_campaign:_assert_two_run_coverage_campaign",
            "_assert_two_run_coverage_campaign:campaign",
            "_coverage_campaign_path:_coverage_campaign_path",
            "test_real_coverage_flow_publishes_canonical_simulation_record:campaign",
            "test_real_lexical_scan_reports_unused_module_sources:campaign",
        }
    ),
}

# Mutation Tester and Target campaign names, which keep their name.
_MUTATION_TARGET_NAMES: dict[str, frozenset[str]] = {
    "src/booley/agent_workspace/isolation.py": frozenset(
        {
            "_campaign_roots",
        }
    ),
    "src/booley/criteria/templates.py": frozenset(
        {
            "TARGET_CAMPAIGN_CRITERIA",
            "_TARGET_CAMPAIGN_PARAM_REGISTRY",
        }
    ),
    "src/booley/flows/mutation/flow.py": frozenset(
        {
            "_apply_campaign_defaults",
            "_attach_campaign_artifacts",
            "_attach_campaign_directories",
            "_campaign_for_target",
            "_campaign_manifest",
            "_copy_campaign_dir",
            "_copy_campaign_file",
            "_resolve_campaign",
            "_run_campaign",
            "_stage_campaign_files",
            "_target_campaign",
            "_write_campaign_manifest",
            "campaign",
            "campaign_dir",
            "campaign_error",
        }
    ),
    "src/booley/flows/target_campaign.py": frozenset(
        {
            "CampaignResults",
            "CampaignUnit",
            "CampaignUnitResult",
            "TargetCampaign",
            "all_campaign_results_match",
            "describe_target_campaign",
            "resolve_target_campaign",
        }
    ),
    "src/booley/flows/target_criteria.py": frozenset(
        {
            "CampaignScopeError",
            "resolve_campaign_scope",
        }
    ),
    "tests/criteria/test_campaign_templates.py": frozenset(
        {
            "campaign",
            "test_coverage_campaign_expands_shared_policy_per_target",
            "test_coverage_campaign_rejects_duplicate_target_ownership",
            "test_coverage_campaign_rejects_invalid_authoring",
            "test_mutation_campaign_expands_target_and_parameters",
            "test_mutation_campaign_rejects_incoherent_counts",
        }
    ),
    "tests/flows/mutation/test_flow.py": frozenset(
        {
            "_assert_durable_campaign",
            "_campaign",
            "_target_campaign",
            "campaign",
            "campaign_dir",
            "test_campaign_artifacts_show_each_tests_baseline_duration_and_time_limit",
            "test_campaign_runs_the_baseline_then_each_mutant",
            "test_happy_path_publishes_campaign_artifacts",
        }
    ),
    "tests/flows/test_target_campaign.py": frozenset(
        {
            "campaign",
            "test_all_skipped_campaign_is_rejected",
            "test_batched_campaign_launches_one_process_per_given_batch",
            "test_campaign_aggregation_cannot_pass_vacuously",
            "test_campaign_domain_does_not_import_presentation_layers",
        }
    ),
    "tests/flows/test_transport_contract.py": frozenset(
        {
            "run_campaign",
        }
    ),
}

# Goal-side names left to #3045 (the freshness category and unsettled campaigns).
_GOAL_NAMES: dict[str, frozenset[str]] = {
    "src/booley/flows/source_fingerprint.py": frozenset(
        {
            "_campaign_core_files",
            "_campaign_files",
            "campaign",
            "campaign_fingerprint",
        }
    ),
    "src/booley/goals/simulation_record_accounting.py": frozenset(
        {
            "unsettled_campaigns",
        }
    ),
    "tests/flows/test_source_fingerprint_campaign.py": frozenset(
        {
            "campaign",
            "test_campaign_fingerprint_names_files_through_symlinked_booley_dir",
            "test_campaign_fingerprint_tracks_tests_and_selected_core",
            "test_campaign_fingerprint_tracks_transitive_core_changes",
            "test_campaign_fingerprint_uses_resolved_booley_dir",
        }
    ),
    "tests/goals/test_freshness.py": frozenset(
        {
            "test_target_less_tb_and_consumed_campaign_are_filtered",
        }
    ),
}


def _kept_entries() -> dict[str, frozenset[str]]:
    """Merge the keep lists by file. An entry is name or Scope.qualname:name."""
    merged: dict[str, set[str]] = {}
    for kept in (_COVERAGE_RECORD_NAMES, _MUTATION_TARGET_NAMES, _GOAL_NAMES):
        for relative, entries in kept.items():
            merged.setdefault(relative, set()).update(entries)
    return {relative: frozenset(entries) for relative, entries in merged.items()}


def test_simulation_record_package_defines_only_record_names() -> None:
    sources = sorted(_RECORD_PACKAGE.glob("*.py"))
    assert sources, f"no modules found under {_RECORD_PACKAGE}"
    offenders = [
        f"{source.name}:{line} {name}"
        for source in sources
        for line, _scope, name in _defined_names(ast.parse(source.read_bytes()))
        if _FORBIDDEN in name.lower()
    ]
    assert not offenders, "Simulation Record package defines campaign names:\n" + "\n".join(
        offenders
    )


def test_simulation_record_names_outside_the_package_use_record_names() -> None:
    kept = _kept_entries()
    used: set[tuple[str, str]] = set()
    offenders: list[str] = []
    for path in source_checkout_files(_ROOT, "src", "tests", "qa"):
        if path.suffix != ".py":
            continue
        relative = path.relative_to(_ROOT).as_posix()
        allowed = kept.get(relative, frozenset())
        for line, scope, name in _defined_names(ast.parse(path.read_bytes())):
            if _FORBIDDEN not in name.lower():
                continue
            entry = next((e for e in (name, f"{scope}:{name}") if e in allowed), None)
            if entry is None:
                offenders.append(f"{relative}:{line} {scope}:{name}")
            else:
                used.add((relative, entry))
    assert not offenders, (
        "names that say campaign for the Simulation Record; rename them to Record names, or "
        "list a name of another concept with its reason:\n" + "\n".join(offenders)
    )
    stale = sorted(
        f"{relative} {entry}"
        for relative, entries in kept.items()
        for entry in entries
        if (relative, entry) not in used
    )
    assert not stale, "keep entries that no longer match a defined name:\n" + "\n".join(stale)
