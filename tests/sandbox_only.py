"""Registry of Sandbox-only tests, skipped on Windows.

Booley runs on a Windows host only for ``HOST`` and ``EITHER`` commands (see
``COMMAND_LOCATIONS`` in ``booley.harness.booley``); Goal Mode, Booley Flows, Specialists, MCP
tools, the Dashboard, chat, and worktree code run inside the Sandbox, a Linux container.
Windows CI therefore skips tests whose subject only runs there, and Linux CI runs them all.

This registry is the only place that applies the ``sandbox_only`` marker; the guard in
``tests/architecture/test_sandbox_only_tests.py`` rejects any other use and fails when a
module a host command can import is covered only by Sandbox-only tests. Mark by subject: the
module a test exercises. A test that exercises host-reachable code stays on Windows.
See ``.github/ci/README.md#sandbox-only-tests``.
"""

from __future__ import annotations

import sys

import pytest

SKIP_REASON = (
    "sandbox_only: exercises Sandbox-only code, which never runs on a Windows host; "
    "runs on Linux CI"
)

# Repository-relative test directories (trailing "/") and test modules that are Sandbox-only.
SANDBOX_ONLY_PATHS: tuple[str, ...] = (
    # Goal Mode (`booley goal` is a Sandbox command). WINDOWS_HOST_TESTS lists the exceptions.
    "tests/goals/",
    # Simulation Flow runtime (campaigns, builds, coverage, backends): `booley flow sim` is a
    # Sandbox command. The host imports flows.sim.flow and its modules only to print endpoint
    # help and never executes the runtime, so tests of that runtime stay Sandbox-only. Tests of
    # what the host does run (help text, argument parsing, config loaders) stay on Windows.
    "tests/flows/sim/backends/test_cocotb_run.py",
    "tests/flows/sim/backends/test_declared_trace_streams.py",
    "tests/flows/sim/backends/test_iverilog_run.py",
    "tests/flows/sim/backends/test_supervision.py",
    "tests/flows/sim/backends/test_trace_finalization.py",
    "tests/flows/sim/backends/test_trace_freshness.py",
    "tests/flows/sim/backends/test_verilator_run.py",
    "tests/flows/sim/test_build_failure_reporting.py",
    "tests/flows/sim/test_campaign_acceptance.py",
    "tests/flows/sim/test_campaign_build_execution_validation.py",
    "tests/flows/sim/test_campaign_build_records.py",
    "tests/flows/sim/test_campaign_cancellation_cause.py",
    "tests/flows/sim/test_campaign_characterization.py",
    "tests/flows/sim/test_campaign_cocotb_coverage_items.py",
    "tests/flows/sim/test_campaign_crash_matrix.py",
    "tests/flows/sim/test_campaign_disclosure_test_runs.py",
    "tests/flows/sim/test_campaign_execution_recovery.py",
    "tests/flows/sim/test_campaign_file_readers.py",
    "tests/flows/sim/test_campaign_fixture_contract.py",
    "tests/flows/sim/test_campaign_lane_pool.py",
    "tests/flows/sim/test_campaign_lane_progress.py",
    "tests/flows/sim/test_campaign_large_build_output.py",
    "tests/flows/sim/test_campaign_protocol_rejections.py",
    "tests/flows/sim/test_campaign_run_cwd_preflight.py",
    "tests/flows/sim/test_campaign_run_directory_branches.py",
    "tests/flows/sim/test_campaign_run_directory_cleanup.py",
    "tests/flows/sim/test_campaign_run_lock_unlink_failure.py",
    "tests/flows/sim/test_campaign_shared_bundle.py",
    "tests/flows/sim/test_campaign_test_runs.py",
    "tests/flows/sim/test_campaign_unsupported_formats.py",
    "tests/flows/sim/test_cocotb.py",
    "tests/flows/sim/test_cycle_baseline_campaign_cwd.py",
    "tests/flows/sim/test_cycle_baseline_test_runs.py",
    "tests/flows/sim/test_cycle_baseline_two_revisions.py",
    "tests/flows/sim/test_cycle_observation.py",
    "tests/flows/sim/test_defensive_contracts.py",
    "tests/flows/sim/test_direct_test_run_rows.py",
    "tests/flows/sim/test_elab_only.py",
    "tests/flows/sim/test_endpoint_campaign_lifecycle.py",
    "tests/flows/sim/test_isolated_sim_headers.py",
    "tests/flows/sim/test_one_test_run_rule.py",
    "tests/flows/sim/test_rerun_command_execution.py",
    "tests/flows/sim/test_summary_output.py",
    "tests/flows/sim/test_test_run_environment.py",
    "tests/flows/sim/test_test_run_evidence_rows.py",
    "tests/flows/sim/test_test_run_launch.py",
    "tests/flows/sim/test_test_run_resolution_errors.py",
    "tests/flows/sim/test_verilator_build_reuse_campaign.py",
    "tests/flows/sim/test_verilator_build_reuse_engine.py",
    # Other built-in Flow tests whose subject is MCP adapter, Goal, or Sandbox-tool plumbing.
    "tests/flows/lint/test_adapters.py",
    "tests/flows/test_baseline_pins.py",
    "tests/flows/test_string_parameter_eda.py",
    "tests/flows/test_transport_contract.py",
    # MCP server tools and Specialist internals: the MCP server runs only in the Sandbox.
    "tests/mcp_tools/test_application.py",
    "tests/mcp_tools/test_goal_tools.py",
    "tests/mcp_tools/test_job_lifecycle.py",
    "tests/mcp_tools/test_live_catalog.py",
    "tests/mcp_tools/test_mcp_dispatch.py",
    "tests/mcp_tools/test_mutation_runtime_inputs.py",
    "tests/mcp_tools/test_mutation_variant_flow.py",
    "tests/mcp_tools/test_mutation_variant_interruptions.py",
    "tests/mcp_tools/test_nested_session_presence.py",
    "tests/mcp_tools/test_owned_job_poll.py",
    "tests/mcp_tools/test_peer_checkout.py",
    "tests/mcp_tools/test_project_flow_loader.py",
    "tests/mcp_tools/test_report_encoding.py",
    "tests/mcp_tools/test_schema_extractor.py",
    "tests/mcp_tools/test_session_attribution.py",
    "tests/mcp_tools/test_session_requests.py",
    "tests/mcp_tools/test_sim_seed_dispatch.py",
    "tests/mcp_tools/test_unread_job_notice.py",
    # Dashboard, chat, Goal status, and MCP probe commands: Sandbox commands.
    "tests/harness/test_chat_terminal.py",
    "tests/harness/test_dashboard.py",
    "tests/harness/test_dashboard_cli.py",
    "tests/harness/test_dashboard_job_columns.py",
    "tests/harness/test_dashboard_job_filters.py",
    "tests/harness/test_dashboard_jobs.py",
    "tests/harness/test_dashboard_keys.py",
    "tests/harness/test_dashboard_layout.py",
    "tests/harness/test_dashboard_links.py",
    "tests/harness/test_dashboard_preview.py",
    "tests/harness/test_dashboard_sessions.py",
    "tests/harness/test_dashboard_targets.py",
    "tests/harness/test_dashboard_view_owner.py",
    "tests/harness/test_dashboard_view_probe.py",
    "tests/harness/test_dashboard_windows.py",
    "tests/harness/test_goal_cli.py",
    "tests/harness/test_mcp_probe.py",
    # In-Sandbox registrars, heartbeat, and MCP transport tests (root and runtime directories).
    "tests/runtime/test_incontainer_gateway_provider.py",
    "tests/test_heartbeat.py",
    "tests/test_incontainer_live_preview.py",
    "tests/test_incontainer_register.py",
    "tests/test_incontainer_vaporview.py",
    "tests/test_mcp_server.py",
    "tests/test_mcp_v2_adapter.py",
)

# Test modules inside a SANDBOX_ONLY_PATHS directory that stay on Windows because they cover
# host-reachable source: the module, and the host root that reaches it (or why it is kept).
WINDOWS_HOST_TESTS: tuple[str, ...] = (
    "tests/goals/test_binding.py",  # goals.binding (host: harness.booley)
    "tests/goals/test_changes.py",  # goals.changes (host: harness.booley)
    "tests/goals/test_declared_inputs.py",  # goals.protected_inputs, goals.entry (host)
    "tests/goals/test_freshness.py",  # criteria.freshness (host: specialists.reviewer)
    "tests/goals/test_model.py",  # goals.model (host: harness.booley)
    "tests/goals/test_naming.py",  # goals.naming (host: harness.booley)
    "tests/goals/test_paths.py",  # goals.paths (host: harness.booley)
    "tests/goals/test_presentation.py",  # goals.presentation (host: harness.booley)
    "tests/goals/test_proposals.py",  # goals.proposals (host: harness.booley)
    "tests/goals/test_protected_inputs.py",  # goals.protected_inputs (host: harness.booley)
    "tests/goals/test_session_key.py",  # goals.session_key (host: harness.booley)
    "tests/goals/test_store.py",  # goals.store (host: harness.booley)
    "tests/goals/test_symlinked_booley_dir.py",  # only Windows test of goals.proposal_errors
    "tests/goals/test_test_run_freezing.py",  # goals.test_run_freezing (host: harness.booley)
    "tests/goals/test_translate.py",  # goals.translate (host: harness.booley)
    "tests/goals/test_waiver_binding.py",  # goals.waiver_binding (host: harness.booley)
)


def is_sandbox_only(relative_path: str) -> bool:
    """True when the repository-relative test path is registered as Sandbox-only."""
    if relative_path in WINDOWS_HOST_TESTS:
        return False
    return any(
        relative_path.startswith(entry) if entry.endswith("/") else relative_path == entry
        for entry in SANDBOX_ONLY_PATHS
    )


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Mark registered tests; skip them on Windows.

    Runs before the CI shard plugin so its balancing sees the skip.
    """
    skip = pytest.mark.skip(reason=SKIP_REASON)
    for item in items:
        if is_sandbox_only(item.path.resolve().relative_to(config.rootpath.resolve()).as_posix()):
            item.add_marker(pytest.mark.sandbox_only)
            if sys.platform == "win32":
                item.add_marker(skip)
