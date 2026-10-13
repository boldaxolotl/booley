# Streaming query removal (#1431)

## Reachability evidence

Checked against main `7bf0fdefd` before removal:

- Each of the ten query/introspection handlers in `src/main.rs` calls
  `require_bwave` and queries `ColumnCache`. Valid query invocations with VCD
  input exit 2 with a build hint, before parsing waveform data.
- Repository-wide Rust and Python searches found no users of `Extractor`
  outside `src/extract.rs`, and no users of `CycleIndex` or `IndexBuilder`
  outside `src/index.rs` and the extractor. The library root exported both
  modules, but no command, integration test, benchmark, fuzz target, or Booley
  wrapper used those exports. This establishes repository reachability, not
  the absence of unknown external Rust library consumers.
- `build` uses `try_parse_header` and the VCD chunk parser. Neither deleted
  module participates in VCD-to-FST conversion.
- The callback parser remains for its standalone throughput benchmark,
  dialect unit tests, and malformed-input fuzz target. These exercise parsing
  independently of FST encoding; they do not expose a raw-VCD query path.

## Disposition of all 35 extractor tests

Names below refer to the deleted unit tests. Existing CLI tests are in
`tests/integration_test.rs` unless a `cli_issues.rs` prefix is shown. All queries
in those suites use built stores. New `issue_1431_*` tests passed both before
and after removal, preserving the current store behavior.

| Removed test | Store behavior coverage or reason for dropping |
|---|---|
| `test_clock_detection` | `test_basic_sync_default` checks detected clock and sync output. |
| `test_reset_detection` | `test_basic_sync_default`, `test_basic_sync_with_include_reset`, and `test_active_high_reset` cover reset selection and polarity. |
| `test_cycle_counting` | Private rising-edge counter removed. Store cycle output is covered by `test_basic_sync_with_include_reset`; no irregular-clock semantics are transferred. |
| `test_clock_period_detection` | `test_basic_sync_default` checks the detected period. |
| `test_find_value_sync` | `test_find_value_sync`. |
| `test_find_value_async` | `test_find_value_async`. |
| `test_find_edge_rising` | `test_find_value_edge`. |
| `test_async_mode_fallback_no_clock` | `test_no_clock_fallback`. |
| `test_stats_transitions` | Private initialization-as-transition accounting removed. Store transition/occupancy policy is covered by `issue_1098_stats_reset_window_and_initial_occupancy` and `issue_1098_occupancy_edges_without_clock_or_reset`. |
| `test_max_lines_limit` | `test_max_lines` checks bounded CLI records; private `done` state removed. |
| `test_find_value_max_lines` | `cli_issues.rs: issue_1108_exact_aliases_and_limits` checks find record limits. |
| `test_sample_at_error_no_match` | `test_sample_at_no_match`. |
| `test_value_formatting_in_current_vals` | Private value storage and bare-hex config input removed. Public Verilog literal matching is covered by `cli_issues.rs: issue_1100_change_commands_agree_and_preserve_literals`. |
| `test_at_time_ns_auto_conversion` | Removed private heuristic that silently guessed cycles from large integers. Current typed time-token conversion is covered by `test_time_token_ns_in_sync_converts_to_cycle`. |
| `test_sample_at_level_mode_matches_literal_one` | New `issue_1431_async_sample_level_rising_and_change` checks literal level matches, including a watched change while the trigger stays high. |
| `test_sample_at_rising_mode_detects_transition` | New `issue_1431_async_sample_level_rising_and_change` checks both rising events and settled snapshots. |
| `test_sample_at_change_mode_detects_any_transition` | New `issue_1431_async_sample_level_rising_and_change` checks rising and falling changes, excluding the initial assignment. |
| `test_at_time_cycle_0_sync` | `test_at_time_cycle_0`. |
| `test_stats_with_signal_never_changes` | Private initial-assignment transition count removed. `issue_1098_stuck_seed_and_whole_window` asserts the store's zero-transition policy. |
| `test_multiple_clock_candidates_priority` | New `issue_1431_clock_detection_prefers_shallowest_scope` asserts the winning clock name, strengthening the original nonempty-candidate assertion. |
| `test_find_value_with_wide_signal` | `test_wide_signals_find_value_256bit`. |
| `test_finalize_time_in_state_correct_totals` | Combined stats/stuck finalization is private and unreachable from the CLI. Store occupancy totals are covered by `issue_1098_occupancy_edges_without_clock_or_reset` and `issue_1098_stats_reset_window_and_initial_occupancy`. |
| `issue_1100_legacy_raw_pulses_completed_snapshots_tail_and_count` | `cli_issues.rs: issue_1100_raw_pulses_unknowns_virtuals_and_fallback` covers raw pulses, settled snapshots, tail events, and counts. |
| `issue_1100_legacy_final_timestamp_and_observed_unknowns` | `cli_issues.rs: issue_1100_raw_pulses_unknowns_virtuals_and_fallback` covers settled same-timestamp changes, X/Z, and inclusive bounds. |
| `issue_1100_legacy_signal_major_first_last_and_limits` | `cli_issues.rs: issue_1100_reset_cutoff_reassert_unknown_and_first_last_limit` covers ordering and selectors; it preserves the store's global last result, rather than the extractor's private buffered event list. |
| `issue_1100_legacy_reset_origin_unknown_reassert_never_and_no_period` | `cli_issues.rs: issue_1100_reset_cutoff_reassert_unknown_and_first_last_limit` and `issue_1100_raw_pulses_unknowns_virtuals_and_fallback` cover reset cutoffs, unknown resets, never-deasserted reset, and clockless fallback. |
| `issue_1100_spool_cap_io_errors_propagate_and_cleanup` | Removed private temporary spool, error injection, and cleanup; store queries do not use it. |
| `issue_1100_legacy_bus_directional_and_sample_selection` | `cli_issues.rs: issue_1100_change_commands_agree_and_preserve_literals` covers bus changes and directional rejection. `test_sample_first_returns_only_first_trigger_event` and `test_sample_last_selects_global_last_event_before_applying_limit` cover sample selectors. |
| `issue_1100_legacy_first_observation_active_high_and_alias_name` | `cli_issues.rs: issue_1100_late_first_write_observes_fst_start_frame`, `issue_1108_exact_aliases_and_limits`, and `test_active_high_reset` cover the store's initial-frame policy, alias names, and active-high reset. |
| `issue_1100_legacy_change_combined_stats` | Combined find/sample plus stats configuration is not a CLI command. Independent store stats/occupancy contracts are covered by `issue_1098_occupancy_edges_without_clock_or_reset`. |
| `issue_1100_legacy_periodic_reset_and_one_rise_windows` | `cli_issues.rs: issue_1100_raw_pulses_unknowns_virtuals_and_fallback` covers single-rise fallback/windows; `issue_1100_reset_cutoff_reassert_unknown_and_first_last_limit` covers periodic reset and inclusive cycle bounds. |
| `issue_1100_spool_index_growth_is_amortized_and_accounted` | Removed private spool allocation accounting; no store-query equivalent. |
| `issue_1100_legacy_late_first_write_initializes` | `cli_issues.rs: issue_1100_late_first_write_observes_fst_start_frame` pins the distinct store policy: FST starts at X, so a later first write is a change. |
| `issue_1100_no_clock_change_stats_preserve_fallback_policy` | Combined find/sample plus stats is not exposed by the CLI. `issue_1098_clockless_reset_keeps_selected_window_policy` and the clockless cases in `cli_issues.rs: issue_1100_raw_pulses_unknowns_virtuals_and_fallback` cover the separately exposed store policies. |
| `issue_1100_clocked_change_stats_preserve_reset_rebase` | Combined find/sample plus stats is not exposed by the CLI. `issue_1098_stats_reset_window_and_initial_occupancy` and `cli_issues.rs: issue_1100_reset_cutoff_reassert_unknown_and_first_last_limit` cover store reset windows and change-event rebasing separately. |

The seek-index module's three tests exercise only its removed serialization,
binary search, and builder. FST seeking uses the native reader and has separate
store tests. `test_rejects_vcd_input` now checks exit 2, empty stdout, and the
build hint for all ten query/introspection commands.

This removal does not change sync sampling, clock checks, `ExtractConfig`, or
the build path. Sync semantics follow-up belongs to #1428.
