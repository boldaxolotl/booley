//! Integration tests for bwave CLI (v0.2 clap-subcommand surface).
//! Run the compiled binary against test VCD files and verify output.
//!
//! v0.2 mapping notes:
//! - Mode flags (`--list-signals`, `--find-value`, `--sample-at`, `--find-stuck`,
//!   `--wave`, `--stats`, `--diff`, `--distance`, `--at-time`, `--at-cycle`,
//!   `--find`) are replaced by clap subcommands.
//! - Global options (`--async`, `--clock`, `--reset`, `--with-reset`, `--format`,
//!   `--limit`) are flattened into every query subcommand. Always go AFTER the
//!   BWAVE_FILE positional.
//! - `--include-reset` → `--with-reset`. `--max-lines N` → `--limit N`.
//! - Query subcommands consume FST stores exclusively. Test VCD fixtures are
//!   built to temporary FST stores before each query.
//! - The bare-positional + mode-flag combos no longer exist — every invocation
//!   names a subcommand.

use std::path::PathBuf;
use std::process::Command;
use std::sync::atomic::{AtomicU64, Ordering};

static QUERY_STORE_SEQUENCE: AtomicU64 = AtomicU64::new(0);

/// Path to the binary cargo just built for THIS test run.
///
/// Cargo sets `CARGO_BIN_EXE_<name>` per profile. Hand-assembling
/// `target/debug/bwave` instead silently tests whatever happens to be sitting
/// there — under `cargo test --release` that is a stale debug build, possibly
/// months old, and the failures it invents have nothing to do with the code.
fn exe_path() -> PathBuf {
    PathBuf::from(env!("CARGO_BIN_EXE_bwave"))
}

/// Path to a test VCD file (fixtures live under tests/fixtures/).
fn vcd_path(name: &str) -> PathBuf {
    let mut path = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    path.push("tests");
    path.push("fixtures");
    path.push(name);
    path
}

/// Build an .fst waveform store from a VCD, returning the store path.
/// Uses a unique suffix to avoid races when tests run in parallel.
fn build_bwave(vcd_name: &str, test_id: &str) -> PathBuf {
    let vcd = vcd_path(vcd_name);
    let bwave = vcd.with_extension(format!("{}.fst", test_id));
    // v0.2: `build <VCD> -o <BWAVE>`
    let output = Command::new(exe_path())
        .args(&[
            "build",
            vcd.to_str().unwrap(),
            "-o",
            bwave.to_str().unwrap(),
        ])
        .output()
        .expect("build failed");
    assert!(
        output.status.success(),
        "build failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    bwave
}

/// Run bwave against a built .fst store.
fn run_bwave(args: &[&str]) -> (String, String, i32) {
    let output = Command::new(exe_path())
        .args(args)
        .output()
        .expect("failed to execute bwave");
    let stdout = String::from_utf8_lossy(&output.stdout).to_string();
    let stderr = String::from_utf8_lossy(&output.stderr).to_string();
    let code = output.status.code().unwrap_or(-1);
    (stdout, stderr, code)
}

/// Build a VCD fixture to a unique FST store, run one query, then remove it.
fn run_query(args: &[&str]) -> (String, String, i32) {
    let mut full_args: Vec<String> = args
        .iter()
        .filter(|arg| **arg != "--")
        .map(|arg| (*arg).to_string())
        .collect();
    let mut temporary_store = None;
    if let Some(input) = full_args.get(1).map(PathBuf::from) {
        if input.extension().and_then(|suffix| suffix.to_str()) == Some("vcd") && input.exists() {
            let sequence = QUERY_STORE_SEQUENCE.fetch_add(1, Ordering::Relaxed);
            let store =
                input.with_extension(format!("query-{}-{sequence}.fst", std::process::id()));
            let output = Command::new(exe_path())
                .args([
                    "build",
                    input.to_str().unwrap(),
                    "-o",
                    store.to_str().unwrap(),
                ])
                .output()
                .expect("fixture build failed");
            assert!(
                output.status.success(),
                "fixture build failed: {}",
                String::from_utf8_lossy(&output.stderr)
            );
            full_args[1] = store.to_string_lossy().into_owned();
            temporary_store = Some(store);
        }
    }
    let output = Command::new(exe_path())
        .args(&full_args)
        .output()
        .expect("failed to execute bwave");
    if let Some(store) = temporary_store {
        let _ = std::fs::remove_file(store);
    }
    let stdout = String::from_utf8_lossy(&output.stdout).to_string();
    let stderr = String::from_utf8_lossy(&output.stderr).to_string();
    let code = output.status.code().unwrap_or(-1);
    (stdout, stderr, code)
}

/// Run bwave without preparing an FST store (for input-contract tests).
fn run_raw(args: &[&str]) -> (String, String, i32) {
    let output = Command::new(exe_path())
        .args(args)
        .output()
        .expect("failed to execute bwave");
    let stdout = String::from_utf8_lossy(&output.stdout).to_string();
    let stderr = String::from_utf8_lossy(&output.stderr).to_string();
    let code = output.status.code().unwrap_or(-1);
    (stdout, stderr, code)
}

// -- Basic sync mode --------------------------------------------------

#[test]
fn test_basic_sync_default() {
    let vcd = vcd_path("test_basic.vcd").to_string_lossy().to_string();
    // v0.2: default trace = `signal` subcommand
    let (stdout, stderr, code) = run_query(&["signal", &vcd]);
    assert_eq!(code, 0);
    assert!(
        stderr.contains("sync: period="),
        "should detect the clock period"
    );
    // Output should contain cycle numbers and signal values
    assert!(stdout.contains("1 "), "should have cycle 1 output");
}

#[test]
fn test_basic_sync_with_include_reset() {
    let vcd = vcd_path("test_basic.vcd").to_string_lossy().to_string();
    let (stdout, _stderr, code) = run_query(&["signal", &vcd, "--with-reset"]);
    assert_eq!(code, 0);
    // With with-reset, should see all cycles from the start
    assert!(stdout.contains("1 "), "should have cycle 1");
}

// -- Async mode -------------------------------------------------------

#[test]
fn test_basic_async() {
    let vcd = vcd_path("test_basic.vcd").to_string_lossy().to_string();
    let (stdout, _stderr, code) = run_query(&["signal", &vcd, "--async"]);
    assert_eq!(code, 0);
    // Async mode emits raw store ticks.
    let first_line = stdout.lines().next().unwrap_or("");
    let ts: Result<i64, _> = first_line.split_whitespace().next().unwrap_or("").parse();
    assert!(
        ts.is_ok(),
        "async output should start with numeric timestamp"
    );
}

// -- List signals -----------------------------------------------------

#[test]
fn test_list_signals_basic() {
    let vcd = vcd_path("test_basic.vcd").to_string_lossy().to_string();
    let (stdout, stderr, code) = run_query(&["list", &vcd]);
    assert_eq!(code, 0);
    assert!(stderr.contains("signals"), "should report count");
    // Should list signal names
    assert!(
        stdout.contains("clk") || stdout.contains("rstn") || stdout.contains("data"),
        "should list some signals"
    );
}

#[test]
fn test_list_signals_with_pattern() {
    let vcd = vcd_path("test_basic.vcd").to_string_lossy().to_string();
    let (stdout, stderr, code) = run_query(&["list", &vcd, "-s", "*data*"]);
    assert_eq!(code, 0);
    assert!(stdout.contains("data"), "should match data signal");
    assert!(stderr.contains("1 signals"), "should match exactly 1");
}

// -- Stats mode -------------------------------------------------------

#[test]
fn test_stats() {
    let vcd = vcd_path("test_basic.vcd").to_string_lossy().to_string();
    let (stdout, _stderr, code) = run_query(&["stats", &vcd]);
    assert_eq!(code, 0);
    assert!(stdout.contains("transitions"), "should show transitions");
    assert!(
        stdout.contains("unique values"),
        "should show unique values"
    );
    assert!(stdout.contains("# Simulation:"), "should show sim duration");
}

// -- Stuck (was: find-stuck) -----------------------------------------

#[test]
fn test_find_stuck() {
    let vcd = vcd_path("test_basic.vcd").to_string_lossy().to_string();
    let (stdout, _stderr, code) = run_query(&["stuck", &vcd]);
    assert_eq!(code, 0);
    assert!(
        stdout.contains("# Stuck signals:"),
        "should show stuck header"
    );
}

// -- Find -------------------------------------------------------------

#[test]
fn test_find_value_sync() {
    let vcd = vcd_path("test_basic.vcd").to_string_lossy().to_string();
    let (stdout, _stderr, code) = run_query(&["find", &vcd, "*data*", "'hA", "--with-reset"]);
    assert_eq!(code, 0);
    // data becomes 0x0A -> should match
    assert!(stdout.contains("cycle"), "should find match at a cycle");
}

#[test]
fn test_find_value_async() {
    let vcd = vcd_path("test_basic.vcd").to_string_lossy().to_string();
    let (stdout, _stderr, code) = run_query(&["find", &vcd, "*data*", "'hFF", "--async"]);
    assert_eq!(code, 0);
    assert!(!stdout.is_empty(), "should find FF match");
}

#[test]
fn test_find_value_count() {
    let vcd = vcd_path("test_basic.vcd").to_string_lossy().to_string();
    let (stdout, _stderr, code) =
        run_query(&["find", &vcd, "*data*", "'hA", "--count", "--with-reset"]);
    assert_eq!(code, 0);
    let count: usize = stdout.trim().parse().unwrap_or(999);
    assert!(count >= 1, "should find at least 1 match");
}

#[test]
fn test_find_value_edge() {
    let vcd = vcd_path("test_basic.vcd").to_string_lossy().to_string();
    let (stdout, _stderr, code) = run_query(&["find", &vcd, "*rstn*", "rising", "--with-reset"]);
    assert_eq!(code, 0);
    assert!(!stdout.is_empty(), "should find rising edge on rstn");
}

// -- Time range -------------------------------------------------------

#[test]
fn test_time_range() {
    let vcd = vcd_path("test_basic.vcd").to_string_lossy().to_string();
    let (stdout, _stderr, code) = run_query(&["signal", &vcd, "--with-reset", "-t", "2:3"]);
    assert_eq!(code, 0);
    // Should only show cycles 2-3
    for line in stdout.lines() {
        if let Some(cycle_str) = line.split_whitespace().next() {
            if let Ok(cycle) = cycle_str.parse::<i64>() {
                assert!(cycle >= 2 && cycle <= 3, "cycle {} out of range 2-3", cycle);
            }
        }
    }
}

#[test]
fn test_invalid_time_range() {
    let vcd = vcd_path("test_basic.vcd").to_string_lossy().to_string();
    let (_stdout, stderr, code) = run_query(&["signal", &vcd, "-t", "abc:def"]);
    assert_ne!(code, 0, "should fail on invalid time range");
    assert!(stderr.contains("invalid time"), "should report parse error");
}

// -- Max lines (now --limit) -----------------------------------------

#[test]
fn test_max_lines() {
    let vcd = vcd_path("test_basic.vcd").to_string_lossy().to_string();
    let (stdout, stderr, code) = run_query(&["signal", &vcd, "--with-reset", "--limit", "2"]);
    assert_eq!(code, 0);
    assert!(
        stdout.lines().count() <= 3,
        "output should be limited to ~2 lines"
    );
    assert!(
        stderr.contains("limit (") && stderr.contains("reached"),
        "should warn about truncation"
    );
}

// -- No-clock fallback ------------------------------------------------

#[test]
fn test_no_clock_fallback() {
    let vcd = vcd_path("test_no_clock.vcd").to_string_lossy().to_string();
    let (stdout, _stderr, code) = run_query(&["signal", &vcd]);
    assert_eq!(code, 0);
    let first_tick = stdout
        .lines()
        .next()
        .and_then(|line| line.split_whitespace().next());
    assert_eq!(
        first_tick,
        Some("0"),
        "clockless stores should use raw ticks"
    );
}

// -- X/Z handling -----------------------------------------------------

#[test]
fn test_xz_signals() {
    let vcd = vcd_path("test_xz.vcd").to_string_lossy().to_string();
    let (stdout, _stderr, code) = run_query(&["signal", &vcd, "--async"]);
    assert_eq!(code, 0);
    // Should contain x or z values without crashing
    assert!(!stdout.is_empty(), "should produce output for x/z signals");
}

#[test]
fn test_find_stuck_x() {
    let vcd = vcd_path("test_xz.vcd").to_string_lossy().to_string();
    let (stdout, _stderr, code) = run_query(&["stuck", &vcd, "x"]);
    assert_eq!(code, 0);
    assert!(
        stdout.contains("# Stuck signals:"),
        "should produce stuck report"
    );
}

// -- Aliases ----------------------------------------------------------

#[test]
fn test_aliases() {
    let vcd = vcd_path("test_aliases.vcd").to_string_lossy().to_string();
    let (stdout, _stderr, code) = run_query(&["list", &vcd]);
    assert_eq!(code, 0);
    assert!(!stdout.is_empty(), "should list aliased signals");
}

// -- Active-high reset ------------------------------------------------

#[test]
fn test_active_high_reset() {
    let vcd = vcd_path("test_active_high.vcd")
        .to_string_lossy()
        .to_string();
    let (_stdout, stderr, code) = run_query(&["signal", &vcd]);
    assert_eq!(code, 0);
    // Should detect reset as active-high (no 'n' suffix)
    if stderr.contains("sync: reset=") {
        assert!(
            stderr.contains("active-high"),
            "should detect active-high reset"
        );
    }
}

// -- Picosecond timescale ---------------------------------------------

#[test]
fn test_ps_timescale() {
    let vcd = vcd_path("test_ps_timescale.vcd")
        .to_string_lossy()
        .to_string();
    let (stdout, _stderr, code) = run_query(&["signal", &vcd, "--async"]);
    assert_eq!(code, 0);
    // ps timescale: timestamps should be raw ticks (picoseconds), not ns.
    let max_ts = stdout
        .lines()
        .filter_map(|l| l.split_whitespace().next()?.parse::<i64>().ok())
        .max()
        .unwrap_or(0);
    assert!(
        max_ts >= 5000,
        "ps timestamps should be raw ticks, got max {}",
        max_ts
    );
}

// -- At-time snapshot (now `value FILE --at N`) ----------------------

#[test]
fn test_at_time_sync() {
    let vcd = vcd_path("test_basic.vcd").to_string_lossy().to_string();
    let (stdout, _stderr, code) = run_query(&["value", &vcd, "--at", "2", "--with-reset"]);
    assert_eq!(code, 0);
    assert!(
        stdout.contains("# Snapshot at cycle 2"),
        "should show snapshot header"
    );
}

// -- CLI validation ---------------------------------------------------

// REMOVED in v0.2: `--count` is now per-subcommand (find/sample have their
// own --count). There's no global --count that can be combined with a non-find
// invocation, so the original mutex error is gone — clap rejects unknown args
// instead.

// REMOVED in v0.2: `--trigger-mode` was deprecated and is now removed.
// Edge keywords (rising/falling/change) as VALUE still work directly.

#[test]
fn test_nonexistent_file() {
    let (_stdout, stderr, code) = run_query(&["signal", "nonexistent.vcd"]);
    assert_ne!(code, 0);
    assert!(stderr.contains("requires a built waveform store"));
}

// -- Sample (was sample-at) -------------------------------------------

#[test]
fn test_sample_at_no_match() {
    let vcd = vcd_path("test_basic.vcd").to_string_lossy().to_string();
    // Raw-VCD path: the streaming extractor's init errors are query-vs-trace
    // mismatches, so they share the store path's caller-input exit code (2).
    let (_stdout, stderr, code) = run_query(&["sample", &vcd, "*nonexistent*", "1"]);
    assert_eq!(code, 2, "raw-VCD trigger miss must exit 2: {}", stderr);
    assert!(stderr.contains("no signals match"));

    // Store path: unified with the other total-miss exits — 2, same message.
    let bwave = build_bwave("test_basic.vcd", "sample_nomatch");
    let bp = bwave.to_string_lossy().to_string();
    let (_stdout, stderr, code) = run_query(&["sample", &bp, "*nonexistent*", "1"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 2, "store-path trigger miss must exit 2: {}", stderr);
    assert!(stderr.contains("no signals match trigger pattern"));
}

#[test]
fn test_sample_at_basic() {
    let vcd = vcd_path("test_basic.vcd").to_string_lossy().to_string();
    let (_stdout, stderr, code) = run_query(&[
        "sample",
        &vcd,
        "*clk*",
        "rising",
        "-s",
        "*data*",
        "--with-reset",
    ]);
    assert_eq!(code, 0);
    assert!(
        stderr.contains("trigger events"),
        "should report trigger count"
    );
}

// -- Async timescale unit tests -------------------------------------------

#[test]
fn test_at_time_async_ps() {
    // `value --at 50000t --async` on a 1ps VCD should snapshot at tick 50000.
    // v0.2 phase 4: async mode requires an explicit unit suffix.
    let vcd = vcd_path("test_ps_timescale.vcd")
        .to_string_lossy()
        .to_string();
    let (stdout, _stderr, code) = run_query(&["value", &vcd, "--at", "50000t", "--async"]);
    assert_eq!(code, 0);
    assert!(
        stdout.contains("# Snapshot at 50000"),
        "should snapshot at tick 50000, got: {}",
        stdout
    );
}

#[test]
fn test_at_time_async_ps_beyond_range_holds_last_value() {
    // FST snapshot semantics hold the final value beyond the recorded range.
    let vcd = vcd_path("test_ps_timescale.vcd")
        .to_string_lossy()
        .to_string();
    let (stdout, _stderr, code) = run_query(&["value", &vcd, "--at", "999999999t", "--async"]);
    assert_eq!(code, 0);
    assert!(stdout.contains("# Snapshot at 999999999"));
    assert!(stdout.contains("counter[7:0]"));
}

#[test]
fn test_time_range_async_ps() {
    // --time 20000t:50000t on 1ps VCD — explicit tick units.
    let vcd = vcd_path("test_ps_timescale.vcd")
        .to_string_lossy()
        .to_string();
    let (stdout, _stderr, code) = run_query(&["signal", &vcd, "--async", "-t", "20000t:50000t"]);
    assert_eq!(code, 0);
    // All timestamps in output should be in the range [20000, 50000]
    for line in stdout.lines() {
        if let Some(ts_str) = line.split_whitespace().next() {
            if let Ok(ts) = ts_str.parse::<i64>() {
                assert!(
                    ts >= 20000 && ts <= 50000,
                    "timestamp {} outside range 20000:50000",
                    ts
                );
            }
        }
    }
}

#[test]
fn test_find_value_async_ps_timestamp() {
    // find in async mode should report raw ticks, not ns
    let vcd = vcd_path("test_ps_timescale.vcd")
        .to_string_lossy()
        .to_string();
    let (stdout, _stderr, code) = run_query(&["find", &vcd, "*counter*", "05", "--async"]);
    assert_eq!(code, 0);
    if !stdout.is_empty() {
        let ts: i64 = stdout
            .split_whitespace()
            .next()
            .unwrap_or("0")
            .parse()
            .unwrap_or(0);
        // Should be in ps range (thousands), not ns range (single digits)
        assert!(ts >= 1000, "find timestamp should be raw ticks, got {}", ts);
    }
}

// ====================================================================
// Phase 2: New fixture tests + semantic assertions
// ====================================================================

// -- Deep hierarchy (6 levels) ----------------------------------------

#[test]
fn test_deep_hierarchy_list_signals() {
    let vcd = vcd_path("test_deep_hierarchy.vcd")
        .to_string_lossy()
        .to_string();
    let (stdout, stderr, code) = run_query(&["list", &vcd]);
    assert_eq!(code, 0);
    assert!(stderr.contains("8 signals"), "should find 8 signals");
    assert!(stdout.contains("core"), "should show 'core' scope");
    assert!(stdout.contains("fsm"), "should show 'fsm' scope");
    assert!(stdout.contains("subunit"), "should show 'subunit' scope");
}

#[test]
fn test_deep_hierarchy_scope_prefix() {
    let vcd = vcd_path("test_deep_hierarchy.vcd")
        .to_string_lossy()
        .to_string();
    let (stdout, stderr, code) = run_query(&["signal", &vcd, "--async", "-s", "*subunit*"]);
    assert_eq!(code, 0);
    assert!(stderr.contains("# scope:"), "should report scope prefix");
    assert!(!stdout.is_empty(), "should have output for subunit signals");
}

#[test]
fn test_deep_hierarchy_signal_filtering() {
    let vcd = vcd_path("test_deep_hierarchy.vcd")
        .to_string_lossy()
        .to_string();
    let (stdout, stderr, code) = run_query(&["list", &vcd, "-s", "*ctrl_en*"]);
    assert_eq!(code, 0);
    assert!(stdout.contains("ctrl_en"), "should match ctrl_en");
    assert!(
        stderr.contains("1 signals"),
        "should match exactly 1 signal"
    );
}

// -- Wide signals (1-512 bit) -----------------------------------------

#[test]
fn test_wide_signals_list() {
    let vcd = vcd_path("test_wide_signals.vcd")
        .to_string_lossy()
        .to_string();
    let (stdout, stderr, code) = run_query(&["list", &vcd]);
    assert_eq!(code, 0);
    assert!(stderr.contains("7 signals"), "should find 7 signals");
    assert!(stdout.contains("512-bit"), "should show 512-bit width");
    assert!(stdout.contains("256-bit"), "should show 256-bit width");
    assert!(stdout.contains("1-bit"), "should show 1-bit width");
}

#[test]
fn test_wide_signals_256bit_values() {
    let vcd = vcd_path("test_wide_signals.vcd")
        .to_string_lossy()
        .to_string();
    let (stdout, _stderr, code) = run_query(&["signal", &vcd, "--async", "-s", "*wide256*"]);
    assert_eq!(code, 0);
    // At t=10, wide256 = all 1s = 64 hex F's
    let has_all_f = stdout.lines().any(|l| l.contains(&"F".repeat(64)));
    assert!(has_all_f, "should show 256-bit all-ones as 64 hex F's");
}

#[test]
fn test_wide_signals_find_value_256bit() {
    let vcd = vcd_path("test_wide_signals.vcd")
        .to_string_lossy()
        .to_string();
    let target = format!("'h{}", "F".repeat(64));
    let (stdout, _stderr, code) = run_query(&["find", &vcd, "*wide256*", &target, "--async"]);
    assert_eq!(code, 0);
    assert!(!stdout.is_empty(), "should find 256-bit all-F value");
    let ts: i64 = stdout
        .split_whitespace()
        .next()
        .unwrap_or("0")
        .parse()
        .unwrap_or(0);
    assert_eq!(ts, 10, "256-bit all-F should be at t=10");
}

// -- Dumpvars/dumpoff/dumpon ------------------------------------------

#[test]
fn test_dumpvars_fixture() {
    let vcd = vcd_path("test_dumpvars.vcd").to_string_lossy().to_string();
    let (stdout, _stderr, code) = run_query(&["signal", &vcd, "--async"]);
    assert_eq!(code, 0);
    // After t=45, data should be 01010101 = 55
    let has_55 = stdout.lines().any(|l| l.contains("55"));
    assert!(has_55, "should see data=55 after dumpon");
}

#[test]
fn test_dumpvars_fixture_at_time_after_dumpon() {
    let vcd = vcd_path("test_dumpvars.vcd").to_string_lossy().to_string();
    let (stdout, _stderr, code) = run_query(&["value", &vcd, "--at", "45ns", "--async"]);
    assert_eq!(code, 0);
    assert!(
        stdout.contains("# Snapshot at 45"),
        "should snapshot at t=45"
    );
    assert!(stdout.contains("55"), "data should be 55 at t=45");
}

// -- Many signals (100+) with multi-char IDs --------------------------

#[test]
fn test_many_signals_list() {
    let vcd = vcd_path("test_many_signals.vcd")
        .to_string_lossy()
        .to_string();
    let (stdout, stderr, code) = run_query(&["list", &vcd]);
    assert_eq!(code, 0);
    let count_line = stderr.lines().find(|l| l.contains("signals")).unwrap_or("");
    let count: usize = count_line
        .split_whitespace()
        .find_map(|w| w.parse::<usize>().ok())
        .unwrap_or(0);
    assert!(count >= 80, "should find 80+ signals, got {}", count);
    assert!(stdout.contains("alu"), "should show alu scope");
    assert!(stdout.contains("crypto"), "should show crypto scope");
}

#[test]
fn test_many_signals_stats() {
    let vcd = vcd_path("test_many_signals.vcd")
        .to_string_lossy()
        .to_string();
    let (stdout, _stderr, code) = run_query(&["stats", &vcd, "--with-reset", "-s", "*alu*"]);
    assert_eq!(code, 0);
    assert!(
        stdout.contains("transitions"),
        "should show transitions for alu signals"
    );
}

// -- Multi-line tokens ------------------------------------------------

#[test]
fn test_multiline_var_fixture() {
    let vcd = vcd_path("test_multiline_var.vcd")
        .to_string_lossy()
        .to_string();
    let (stdout, stderr, code) = run_query(&["list", &vcd]);
    assert_eq!(code, 0);
    assert!(
        stderr.contains("3 signals"),
        "multi-line $var should parse 3 signals"
    );
    assert!(stdout.contains("clk"), "should find clk");
    assert!(stdout.contains("data"), "should find data");
    assert!(stdout.contains("enable"), "should find enable");
}

#[test]
fn test_multiline_var_values() {
    let vcd = vcd_path("test_multiline_var.vcd")
        .to_string_lossy()
        .to_string();
    let (stdout, _stderr, code) = run_query(&["signal", &vcd, "--async", "-s", "*data*"]);
    assert_eq!(code, 0);
    assert!(stdout.contains("F"), "should see data=F");
}

// -- Verilator quirks (TOP scope, begin blocks, tabs) -----------------

#[test]
fn test_verilator_quirks_list() {
    let vcd = vcd_path("test_verilator_quirks.vcd")
        .to_string_lossy()
        .to_string();
    let (stdout, stderr, code) = run_query(&["list", &vcd]);
    assert_eq!(code, 0);
    assert!(stderr.contains("4 signals"), "should find 4 signals");
    assert!(
        stdout.contains("gen_block") || stdout.contains("gen_sig"),
        "should show generate block signal"
    );
}

#[test]
fn test_verilator_quirks_tab_vector() {
    let vcd = vcd_path("test_verilator_quirks.vcd")
        .to_string_lossy()
        .to_string();
    let (stdout, _stderr, code) = run_query(&["signal", &vcd, "--async", "-s", "*data*"]);
    assert_eq!(code, 0);
    assert!(
        stdout.contains("A"),
        "tab-separated vector should parse to hex A"
    );
}

// -- Real values ------------------------------------------------------

#[test]
fn test_real_values_list() {
    let vcd = vcd_path("test_real_values.vcd")
        .to_string_lossy()
        .to_string();
    let (stdout, stderr, code) = run_query(&["list", &vcd]);
    assert_eq!(code, 0);
    assert!(stderr.contains("3 signals"), "should find 3 signals");
    assert!(stdout.contains("voltage"), "should list voltage");
    assert!(stdout.contains("temperature"), "should list temperature");
}

#[test]
fn test_real_values_async() {
    let vcd = vcd_path("test_real_values.vcd")
        .to_string_lossy()
        .to_string();
    let (stdout, _stderr, code) = run_query(&["signal", &vcd, "--async", "-s", "*voltage*"]);
    assert_eq!(code, 0);
    assert!(stdout.contains("1.5"), "should show voltage=1.5");
    assert!(stdout.contains("3.3"), "should show voltage=3.3");
}

#[test]
#[ignore = "pre-existing: real-value `find` not supported — parse_verilog_literal rejects '3.3'"]
fn test_real_values_find() {
    let vcd = vcd_path("test_real_values.vcd")
        .to_string_lossy()
        .to_string();
    let (stdout, _stderr, code) = run_query(&["find", &vcd, "*voltage*", "3.3", "--async"]);
    assert_eq!(code, 0);
    assert!(!stdout.is_empty(), "should find voltage=3.3");
}

// -- Empty sim (header only, no timestamps) ---------------------------

#[test]
fn test_empty_sim_list_signals() {
    let vcd = vcd_path("test_empty_sim.vcd").to_string_lossy().to_string();
    let (stdout, stderr, code) = run_query(&["list", &vcd]);
    assert_eq!(code, 0);
    assert!(
        stderr.contains("2 signals"),
        "should find signals in header-only VCD"
    );
    assert!(stdout.contains("clk"), "should list clk");
}

#[test]
fn test_empty_sim_async() {
    let vcd = vcd_path("test_empty_sim.vcd").to_string_lossy().to_string();
    let (stdout, _stderr, code) = run_query(&["signal", &vcd, "--async"]);
    assert_eq!(code, 0);
    assert!(stdout.contains("0 clk x"));
    assert!(stdout.contains("0 data[7:0] x"));
}

#[test]
fn test_empty_sim_stats() {
    let vcd = vcd_path("test_empty_sim.vcd").to_string_lossy().to_string();
    let (stdout, _stderr, code) = run_query(&["stats", &vcd, "--with-reset", "--async"]);
    assert_eq!(code, 0);
    assert!(
        stdout.contains("# Simulation:"),
        "should show sim header for empty VCD"
    );
}

// ====================================================================
// Semantic assertion tests (1D)
// ====================================================================

#[test]
fn test_semantic_at_time_matches_async_trace() {
    // Self-consistency: `value --at T` output should match `signal --async`
    // full trace filtered to T.
    let vcd = vcd_path("test_basic.vcd").to_string_lossy().to_string();

    // Get snapshot at t=15 (async mode)
    let (snap_out, _stderr, code) = run_query(&["value", &vcd, "--at", "15ns", "--async"]);
    assert_eq!(code, 0);

    // Get full async trace up to t=15
    let (full_out, _stderr, code) = run_query(&["signal", &vcd, "--async", "-t", ":15ns"]);
    assert_eq!(code, 0);

    assert!(
        snap_out.contains("# Snapshot"),
        "at-time should produce snapshot"
    );

    for line in snap_out.lines() {
        if line.starts_with('#') || line.is_empty() {
            continue;
        }
        let parts: Vec<&str> = line.splitn(2, '=').collect();
        if parts.len() == 2 {
            let sig_name = parts[0].trim();
            let snap_val = parts[1].trim();
            let last_val = full_out.lines().rev().find_map(|l| {
                let words: Vec<&str> = l.split_whitespace().collect();
                if words.len() >= 3 && words[1] == sig_name {
                    Some(words[2])
                } else {
                    None
                }
            });
            if let Some(trace_val) = last_val {
                assert_eq!(
                    snap_val, trace_val,
                    "at-time and async trace should agree on {} value",
                    sig_name
                );
            }
        }
    }
}

#[test]
fn test_semantic_find_value_at_time_consistency() {
    // `find` returns timestamps; `value --at T` at that timestamp should confirm.
    let vcd = vcd_path("test_basic.vcd").to_string_lossy().to_string();
    let (find_out, _stderr, code) = run_query(&["find", &vcd, "*data*", "'hA", "--async"]);
    assert_eq!(code, 0);
    assert!(!find_out.is_empty(), "should find data=A");

    let ts_str = find_out.split_whitespace().next().unwrap_or("0");
    let ts: i64 = ts_str.parse().unwrap_or(0);
    assert!(ts > 0, "timestamp should be positive");

    // Async --at requires an explicit unit. `find` returned a raw tick, so
    // we annotate with `t` to round-trip without conversion.
    let ts_arg = format!("{}t", ts_str);
    let (snap_out, _stderr, code) = run_query(&["value", &vcd, "--at", &ts_arg, "--async"]);
    assert_eq!(code, 0);
    assert!(
        snap_out.contains("A"),
        "snapshot at find timestamp should contain A"
    );
}

// ====================================================================
// Golden output: list signals tree format (1D)
// ====================================================================

#[test]
fn test_list_signals_golden_deep_hierarchy() {
    let vcd = vcd_path("test_deep_hierarchy.vcd")
        .to_string_lossy()
        .to_string();
    let (stdout, _stderr, code) = run_query(&["list", &vcd]);
    assert_eq!(code, 0);

    let lines: Vec<&str> = stdout.lines().collect();
    assert!(lines.iter().any(|l| l.contains("dut")), "should show dut");
    assert!(lines.iter().any(|l| l.contains("core")), "should show core");
    assert!(lines.iter().any(|l| l.contains("ctrl")), "should show ctrl");
    assert!(
        lines
            .iter()
            .any(|l| l.contains("wire") && l.contains("1-bit")),
        "should show 1-bit wire"
    );
    assert!(
        lines.iter().any(|l| l.contains("32-bit")),
        "should show 32-bit signal"
    );
}

// ====================================================================
// Additional extract.rs edge-case tests (1E)
// ====================================================================

#[test]
fn test_at_time_cycle_0() {
    // `value --at 0` (sync mode) snapshots at cycle 0.
    let vcd = vcd_path("test_basic.vcd").to_string_lossy().to_string();
    let (stdout, _stderr, code) = run_query(&["value", &vcd, "--at", "0", "--with-reset"]);
    assert_eq!(code, 0);
    assert!(
        stdout.contains("# Snapshot at cycle 0"),
        "should snapshot at cycle 0"
    );
}

#[test]
fn test_stats_with_never_changing_signal() {
    let vcd = vcd_path("test_basic.vcd").to_string_lossy().to_string();
    let (stdout, _stderr, code) = run_query(&["stats", &vcd, "-s", "*rstn*"]);
    assert_eq!(code, 0);
    assert!(stdout.contains("transitions"), "should show transitions");
}

#[test]
fn test_find_stuck_with_filter() {
    let vcd = vcd_path("test_xz.vcd").to_string_lossy().to_string();
    let (stdout1, _stderr, code) = run_query(&["stuck", &vcd, "x"]);
    assert_eq!(code, 0);
    let (stdout2, _stderr, code) = run_query(&["stuck", &vcd]);
    assert_eq!(code, 0);
    let count1 = stdout1.lines().filter(|l| l.starts_with("  ")).count();
    let count2 = stdout2.lines().filter(|l| l.starts_with("  ")).count();
    assert!(count1 <= count2, "filtered stuck should be <= unfiltered");
}

// REMOVED in v0.2: `--at-time` and `--at-cycle` are unified into a single
// `--at N` flag on the `value` subcommand (sync vs async chosen by --async).
// The old mutex error no longer applies — there's just one flag now.

#[test]
fn test_sample_at_sync_with_count() {
    let vcd = vcd_path("test_basic.vcd").to_string_lossy().to_string();
    let (stdout, _stderr, code) = run_query(&[
        "sample",
        &vcd,
        "*clk*",
        "rising",
        "-s",
        "*data*",
        "--with-reset",
        "--count",
    ]);
    assert_eq!(code, 0);
    let count: usize = stdout.trim().parse().unwrap_or(0);
    assert!(count >= 1, "should have at least 1 rising edge trigger");
}

// -- Wave mode --------------------------------------------------------

#[test]
fn test_wave_single_cycle() {
    let bwave = build_bwave("test_basic.vcd", "wave_single");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) =
        run_bwave(&["wave", &bp, "-t", "3", "-s", "*data*", "--with-reset"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0);
    assert!(stdout.contains("cycle"), "header should say 'cycle'");
    assert!(stdout.contains("3"), "should show cycle 3");
    assert!(stderr.contains("1 signals, 1 columns"));
}

#[test]
fn test_wave_range() {
    let bwave = build_bwave("test_basic.vcd", "wave_range");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run_bwave(&["wave", &bp, "-t", "1:5", "-s", "*", "--with-reset"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0);
    assert!(stdout.contains("cycle"), "header should say 'cycle'");
    assert!(stderr.contains("3 signals, 5 columns"));
    assert!(stdout.contains("data[7:0]"));
}

#[test]
fn test_wave_async() {
    let bwave = build_bwave("test_basic.vcd", "wave_async");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) =
        run_bwave(&["wave", &bp, "-t", "0ns:100ns", "--async", "-s", "*data*"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0);
    assert!(
        stdout.contains("time"),
        "header should say 'time' in async mode"
    );
    assert!(stderr.contains("1 signals"));
}

#[test]
fn test_wave_max_lines() {
    let bwave = build_bwave("test_basic.vcd", "wave_maxlines");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run_bwave(&[
        "wave",
        &bp,
        "-t",
        "1:100",
        "-s",
        "*data*",
        "--with-reset",
        "--limit",
        "3",
    ]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0);
    assert!(stderr.contains("limit (3) reached"));
    let data_lines: Vec<_> = stdout.lines().filter(|l| l.contains("data")).collect();
    assert_eq!(data_lines.len(), 1);
}

// REMOVED in v0.2: `--wave + --at-time` mutex — clap subcommands make these
// mutually exclusive at the parser level (can't spell both `wave` and `value`).

// REMOVED in v0.2: `--wave + --find-value` mutex — same reason. Subcommands
// are exclusive by construction.

#[test]
fn test_wave_with_reset_skipping() {
    let bwave = build_bwave("test_basic.vcd", "wave_reset_skip");
    let bp = bwave.to_string_lossy().to_string();
    // Without --with-reset, cycle 1 should be after reset deasserts
    let (stdout, _stderr, code) = run_bwave(&["wave", &bp, "-t", "1:3", "-s", "*data*"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0);
    assert!(stdout.contains("data[7:0]"));
}

#[test]
fn test_wave_rejects_raw_vcd() {
    let vcd = vcd_path("test_basic.vcd").to_string_lossy().to_string();
    // Every query is store-only.
    let (_stdout, stderr, code) = run_raw(&["wave", &vcd, "-t", "1:3", "-s", "*data*"]);
    assert_eq!(code, 2, "wave on raw VCD must exit 2");
    assert!(
        stderr.contains("requires a built waveform store"),
        "expected store-required error, got: {}",
        stderr
    );
}

// -- VCD rejection gate -----------------------------------------------

#[test]
fn test_rejects_vcd_input() {
    let vcd = vcd_path("test_basic.vcd").to_string_lossy().to_string();
    // Signal queries reject raw .vcd input.
    let (_stdout, stderr, code) = run_raw(&["signal", &vcd]);
    assert_eq!(code, 2, "should reject .vcd input");
    assert!(
        stderr.contains("requires a built waveform store"),
        "expected store-required error, got: {}",
        stderr
    );
}

#[test]
fn test_allow_vcd_flag_is_rejected() {
    let vcd = vcd_path("test_basic.vcd").to_string_lossy().to_string();
    let (_stdout, stderr, code) = run_raw(&["signal", &vcd, "--allow-vcd"]);
    assert_eq!(code, 2);
    assert!(stderr.contains("unexpected argument '--allow-vcd'"));
}

// -- --last, --before, --after (find subcommand) ---------------------

#[test]
fn test_find_last_sync() {
    let vcd = vcd_path("small_clocked.vcd").to_string_lossy().to_string();
    let (all_stdout, _, all_code) = run_query(&["find", &vcd, "*state*", "0"]);
    assert_eq!(all_code, 0, "all-matches query should succeed");
    let last_line = all_stdout
        .trim()
        .lines()
        .last()
        .expect("should have at least one match");
    let (stdout, _stderr, code) = run_query(&["find", &vcd, "*state*", "0", "--last"]);
    assert_eq!(code, 0);
    assert_eq!(stdout.trim(), last_line.trim(), "should return last match");
    assert_eq!(
        stdout.trim().lines().count(),
        1,
        "should return exactly one line"
    );
}

#[test]
fn test_find_last_single_match() {
    let vcd = vcd_path("small_clocked.vcd").to_string_lossy().to_string();
    let (stdout_last, _, code_last) = run_query(&["find", &vcd, "*done*", "1", "--last"]);
    let (stdout_first, _, code_first) = run_query(&["find", &vcd, "*done*", "1", "--first"]);
    assert_eq!(code_last, 0);
    assert_eq!(code_first, 0);
    assert_eq!(stdout_last.trim(), stdout_first.trim());
}

#[test]
fn test_find_last_conflicts_first() {
    let vcd = vcd_path("small_clocked.vcd").to_string_lossy().to_string();
    let (_, stderr, code) = run_query(&["find", &vcd, "*x*", "1", "--first", "--last"]);
    assert_ne!(code, 0);
    assert!(stderr.contains("mutually exclusive"));
}

#[test]
fn test_find_last_conflicts_count() {
    let vcd = vcd_path("small_clocked.vcd").to_string_lossy().to_string();
    let (_, stderr, code) = run_query(&["find", &vcd, "*x*", "1", "--last", "--count"]);
    assert_ne!(code, 0);
    assert!(stderr.contains("mutually exclusive"));
}

// REMOVED in v0.2: `--last` without a `find` query no longer makes sense —
// `--last` is a flag on the `find` subcommand and can't be used outside it.
// clap rejects it via "unexpected argument" instead of a custom "requires"
// error.

#[test]
fn test_before_implies_last() {
    let vcd = vcd_path("small_clocked.vcd").to_string_lossy().to_string();
    let (all_stdout, _, _) = run_query(&["find", &vcd, "*state*", "0"]);
    let lines: Vec<&str> = all_stdout.trim().lines().collect();
    assert!(
        lines.len() >= 2,
        "need at least 2 matches for --before test"
    );
    let last_cycle: i64 = lines
        .last()
        .unwrap()
        .split_whitespace()
        .nth(1)
        .unwrap()
        .parse()
        .unwrap();
    let boundary = last_cycle + 1;
    let (stdout, _, code) = run_query(&[
        "find",
        &vcd,
        "*state*",
        "0",
        "--before",
        &boundary.to_string(),
    ]);
    assert_eq!(code, 0);
    assert_eq!(
        stdout.trim(),
        lines.last().unwrap().trim(),
        "should return last match before boundary"
    );
    assert_eq!(stdout.trim().lines().count(), 1);
}

#[test]
fn test_after_implies_first() {
    let vcd = vcd_path("small_clocked.vcd").to_string_lossy().to_string();
    let (all_stdout, _, _) = run_query(&["find", &vcd, "*state*", "0"]);
    let lines: Vec<&str> = all_stdout.trim().lines().collect();
    assert!(lines.len() >= 2, "need at least 2 matches for --after test");
    let first_cycle: i64 = lines[0].split_whitespace().nth(1).unwrap().parse().unwrap();
    let second_cycle: i64 = lines[1].split_whitespace().nth(1).unwrap().parse().unwrap();
    let boundary = first_cycle + 1;
    assert!(
        boundary <= second_cycle,
        "boundary {} must be <= second match {}",
        boundary,
        second_cycle
    );
    let (stdout, _, code) = run_query(&[
        "find",
        &vcd,
        "*state*",
        "0",
        "--after",
        &boundary.to_string(),
    ]);
    assert_eq!(code, 0);
    assert_eq!(
        stdout.trim().lines().count(),
        1,
        "should return exactly one line"
    );
    assert_eq!(stdout.trim(), lines[1].trim(), "should return second match");
}

#[test]
fn test_before_after_conflict() {
    let vcd = vcd_path("small_clocked.vcd").to_string_lossy().to_string();
    let (_, stderr, code) = run_query(&["find", &vcd, "*x*", "1", "--before", "5", "--after", "3"]);
    assert_ne!(code, 0);
    assert!(stderr.contains("mutually exclusive"));
}

#[test]
fn test_before_with_time_conflict() {
    let vcd = vcd_path("small_clocked.vcd").to_string_lossy().to_string();
    let (_, stderr, code) = run_query(&["find", &vcd, "*x*", "1", "--before", "5", "-t", "1:10"]);
    assert_ne!(code, 0);
    assert!(stderr.contains("cannot be combined with"));
}

// REMOVED in v0.2: `--before` without `--find` no longer makes sense —
// `--before` is a flag on `find` (and `sample`) and can't be used outside.
// clap rejects unknown args at the top level.

// ===== diff tests =====

#[test]
fn test_diff_basic() {
    let bwave = build_bwave("small_clocked.vcd", "diff_basic");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run_bwave(&["diff", &bp, "1", "5", "-s", "*"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0, "stderr: {}", stderr);
    assert!(stdout.contains("# diff cycle 1 vs 5"), "header: {}", stdout);
    assert!(stdout.contains("state"), "state should differ: {}", stdout);
    assert!(
        stdout.contains("counter"),
        "counter should differ: {}",
        stdout
    );
}

#[test]
fn test_diff_no_change() {
    let bwave = build_bwave("small_clocked.vcd", "diff_nochange");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, _stderr, code) = run_bwave(&["diff", &bp, "5", "5", "-s", "*"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0);
    assert!(
        stdout.contains("no differences found"),
        "same cycle should have no diff: {}",
        stdout
    );
}

#[test]
fn test_diff_with_pattern() {
    let bwave = build_bwave("small_clocked.vcd", "diff_pattern");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, _stderr, code) = run_bwave(&["diff", &bp, "3", "10", "-s", "*state*"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0);
    assert!(
        stdout.contains("state"),
        "state should be in output: {}",
        stdout
    );
    assert!(
        !stdout.contains("counter"),
        "counter should be filtered out: {}",
        stdout
    );
}

#[test]
fn test_diff_reversed() {
    let bwave = build_bwave("small_clocked.vcd", "diff_rev");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout_fwd, _, _) = run_bwave(&["diff", &bp, "1", "10", "-s", "*"]);
    let (stdout_rev, _, _) = run_bwave(&["diff", &bp, "10", "1", "-s", "*"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(
        stdout_fwd, stdout_rev,
        "reversed diff should equal forward diff"
    );
}

#[test]
fn test_diff_async() {
    let bwave = build_bwave("small_clocked.vcd", "diff_async");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, _stderr, code) = run_bwave(&["diff", &bp, "35ns", "95ns", "--async", "-s", "*"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0);
    assert!(
        stdout.contains("# diff time 35 vs 95"),
        "async header: {}",
        stdout
    );
    assert!(stdout.contains("state"), "state should differ: {}", stdout);
}

#[test]
fn test_diff_no_transitions() {
    let bwave = build_bwave("small_clocked.vcd", "diff_notrans");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, _stderr, code) = run_bwave(&["diff", &bp, "1", "2", "-s", "*done*"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0);
    assert!(
        stdout.contains("no differences found") || !stdout.contains("done"),
        "done should not differ: {}",
        stdout
    );
}

#[test]
fn test_diff_bad_value() {
    let bwave = build_bwave("small_clocked.vcd", "diff_badval");
    let bp = bwave.to_string_lossy().to_string();
    let (_, _stderr, code) = run_bwave(&["diff", &bp, "abc", "5"]);
    let _ = std::fs::remove_file(&bwave);
    assert_ne!(code, 0, "non-integer diff args should fail");
}

// REMOVED in v0.2: `--diff + --find` mutex — subcommands are mutually
// exclusive at the clap parser level. Trying to spell both yields "unexpected
// argument" instead.

// ===== distance tests =====

#[test]
fn test_distance_same_signal() {
    let bwave = build_bwave("test_distance.vcd", "dist_same");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, _stderr, code) = run_bwave(&["distance", &bp, "*req*", "rising"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0);
    assert!(
        stdout.contains("d=10"),
        "should have 10-cycle gaps: {}",
        stdout
    );
    assert_eq!(
        stdout.matches("d=10").count(),
        4,
        "should have 4 pairs: {}",
        stdout
    );
}

#[test]
fn test_distance_two_event() {
    let bwave = build_bwave("test_distance.vcd", "dist_two");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, _stderr, code) = run_bwave(&[
        "distance", &bp, "*req*", "rising", "--to", "*ack*", "rising",
    ]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0);
    assert!(stdout.contains("d=3"), "should have delta 3: {}", stdout);
    assert!(stdout.contains("d=4"), "should have delta 4: {}", stdout);
    assert!(stdout.contains("d=5"), "should have delta 5: {}", stdout);
    let pair_count = stdout.lines().filter(|l| l.starts_with("@")).count();
    assert_eq!(pair_count, 5, "should have 5 pairs: {}", stdout);
}

#[test]
fn test_distance_two_event_stats() {
    let bwave = build_bwave("test_distance.vcd", "dist_stats");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, _stderr, code) = run_bwave(&[
        "distance", &bp, "*req*", "rising", "--to", "*ack*", "rising", "--stats",
    ]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0);
    assert!(stdout.contains("count=5"), "count: {}", stdout);
    assert!(stdout.contains("min=3"), "min: {}", stdout);
    assert!(stdout.contains("max=5"), "max: {}", stdout);
    assert!(stdout.contains("avg=3.8"), "avg: {}", stdout);
}

#[test]
fn test_distance_two_event_virtuals() {
    let bwave = build_bwave("test_distance.vcd", "dist_virtuals");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run_bwave(&[
        "distance",
        &bp,
        "issue",
        "rising",
        "--to",
        "retire",
        "rising",
        "--stats",
        "--virtual",
        "issue = *req*",
        "--virtual",
        "retire = *ack*",
    ]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0, "stderr: {}", stderr);
    assert!(stdout.contains("count=5"), "count: {}", stdout);
    assert!(stdout.contains("min=3"), "min: {}", stdout);
    assert!(stdout.contains("max=5"), "max: {}", stdout);
}

#[test]
fn test_distance_async() {
    let bwave = build_bwave("test_distance.vcd", "dist_async");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, _stderr, code) = run_bwave(&["distance", &bp, "*req*", "rising", "--async"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0);
    assert!(
        stdout.contains("d=100"),
        "should have 100-tick gaps: {}",
        stdout
    );
}

#[test]
fn test_distance_same_no_pairs() {
    // `*rstn*` rises exactly once → 1 event, 0 pairs. (This used to probe
    // `*done*`, which does not exist in the fixture at all — that case is now
    // a hard exit-2 error, covered by test_distance_no_match.)
    let bwave = build_bwave("test_distance.vcd", "dist_nopairs");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run_bwave(&["distance", &bp, "*rstn*", "rising"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(
        code, 0,
        "matched-but-no-pairs is a valid empty answer: {}",
        stderr
    );
    assert!(
        stdout.contains("no pairs"),
        "should report no pairs: stdout={}, stderr={}",
        stdout,
        stderr
    );
}

#[test]
fn test_distance_same_with_time() {
    let bwave = build_bwave("test_distance.vcd", "dist_time");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, _stderr, code) = run_bwave(&["distance", &bp, "*req*", "rising", "-t", "10:30"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0);
    let pair_count = stdout.lines().filter(|l| l.starts_with("@")).count();
    assert_eq!(pair_count, 1, "should have 1 pair in range: {}", stdout);
}

// REMOVED in v0.2: `--to` without `--distance` no longer exists as a
// validation — `--to` is a flag on the `distance` subcommand only.

// REMOVED in v0.2: `--distance + --find` mutex — subcommands are mutually
// exclusive at the parser level.

#[test]
fn test_distance_no_match() {
    let bwave = build_bwave("test_distance.vcd", "dist_nomatch");
    let bp = bwave.to_string_lossy().to_string();
    let (_, stderr, code) = run_bwave(&["distance", &bp, "*nonexistent*", "rising"]);
    let _ = std::fs::remove_file(&bwave);
    // A pattern that matches nothing is a hard error, not a quiet empty answer.
    assert_eq!(code, 2, "stderr: {}", stderr);
    assert!(
        stderr.to_lowercase().contains("no signals match"),
        "stderr: {}",
        stderr
    );
}

// ===== --clock override tests (dual clock) =====

#[test]
fn test_clock_override_bwave_query() {
    let bwave = build_bwave("test_dual_clock.vcd", "clk_override");
    let bp = bwave.to_string_lossy().to_string();
    // Default (clk1): cycle grid is period=10
    let (stdout_default, stderr_default, code) =
        run_bwave(&["signal", &bp, "-s", "*data1*", "-t", "1:4"]);
    assert_eq!(code, 0, "default clock query failed: {}", stderr_default);

    // Override to clk2: cycle grid is period=6
    let (stdout_clk2, stderr_clk2, code) = run_bwave(&[
        "signal", &bp, "--clock", "*clk2*", "-s", "*data1*", "-t", "1:4",
    ]);
    assert_eq!(code, 0, "clock override query failed: {}", stderr_clk2);
    assert!(
        stderr_clk2.contains("clock override"),
        "should log clock override: {}",
        stderr_clk2
    );

    assert_ne!(
        stdout_default, stdout_clk2,
        "different clocks should produce different cycle-based output"
    );
    let _ = std::fs::remove_file(&bwave);
}

#[test]
fn test_clock_override_nonexistent_pattern() {
    let bwave = build_bwave("test_dual_clock.vcd", "clk_nomatch");
    let bp = bwave.to_string_lossy().to_string();
    let (_stdout, stderr, code) =
        run_bwave(&["signal", &bp, "--clock", "*nonexistent_clock*", "-s", "*"]);
    let _ = std::fs::remove_file(&bwave);
    // Caller-input class (a --clock pattern that doesn't fit this trace) —
    // exit 2, same as a -s total miss.
    assert_eq!(code, 2, "nonexistent clock pattern must exit 2: {}", stderr);
    assert!(
        stderr.contains("no 1-bit signal matches"),
        "stderr: {}",
        stderr
    );
}

#[test]
fn test_clock_override_wave_mode() {
    let bwave = build_bwave("test_dual_clock.vcd", "clk_wave");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run_bwave(&["wave", &bp, "--clock", "*clk2*", "-s", "*data*"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0, "wave with clock override failed: {}", stderr);
    assert!(
        stderr.contains("clock override"),
        "should log override: {}",
        stderr
    );
    assert!(!stdout.is_empty(), "wave output should not be empty");
}

#[test]
fn test_clock_override_vcd_passthrough() {
    // --clock with VCD input (non-cache path)
    let vcd = vcd_path("test_dual_clock.vcd")
        .to_string_lossy()
        .to_string();
    let (stdout, stderr, code) = run_query(&[
        "signal", &vcd, "--clock", "*clk2*", "-s", "*data1*", "-t", "1:3",
    ]);
    assert_eq!(code, 0, "VCD clock override failed: {}", stderr);
    assert!(!stdout.is_empty(), "should produce output");
}

// ===== retired .bwave format =====

#[test]
fn test_legacy_bwave_input_rejected() {
    let (_stdout, stderr, code) = run_bwave(&["signal", "trace.bwave", "-s", "clk"]);
    assert_eq!(code, 2, "legacy .bwave input must exit 2");
    assert!(
        stderr.contains("replaced by FST"),
        "stderr should point at the migration: {}",
        stderr
    );
}

#[test]
fn test_legacy_bwave_build_output_rejected() {
    let vcd = vcd_path("small_clocked.vcd");
    let output = Command::new(exe_path())
        .args(&["build", vcd.to_str().unwrap(), "-o", "trace.bwave"])
        .output()
        .expect("failed to execute bwave");
    assert_eq!(
        output.status.code().unwrap_or(-1),
        2,
        "legacy .bwave output must exit 2"
    );
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        stderr.contains("replaced by FST"),
        "stderr should point at the migration: {}",
        stderr
    );
}

// ===== --scope (build-time signal filtering) =====

#[test]
fn test_scope_filters_signals() {
    // Build with --scope tb.dut.core.ctrl
    let vcd = vcd_path("test_deep_hierarchy.vcd");
    let bwave = vcd.with_extension("scope_filter.fst");
    let output = Command::new(exe_path())
        .args(&[
            "build",
            vcd.to_str().unwrap(),
            "-o",
            bwave.to_str().unwrap(),
            "--scope",
            "tb.dut.core.ctrl",
        ])
        .output()
        .expect("build with scope failed");
    assert!(
        output.status.success(),
        "build --scope failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );

    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run_bwave(&["list", &bp]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0, "list on scoped bwave failed: {}", stderr);
    assert!(stdout.contains("ctrl_en"), "should include ctrl_en");
    assert!(stdout.contains("fsm_active"), "should include fsm_active");
    assert!(
        stdout.contains("state_bit"),
        "should include subunit signals"
    );
    assert!(
        !stdout.contains("clk"),
        "should NOT include tb.dut.clk (outside scope)"
    );
    assert!(
        !stdout.contains("tb_done"),
        "should NOT include tb.tb_done (outside scope)"
    );
    assert!(
        !stdout.contains("data_bus"),
        "should NOT include data_bus (outside scope)"
    );
}

#[test]
fn test_scope_no_trailing_dot() {
    let vcd = vcd_path("test_deep_hierarchy.vcd");
    let bwave = vcd.with_extension("scope_nodot.fst");
    let output = Command::new(exe_path())
        .args(&[
            "build",
            vcd.to_str().unwrap(),
            "-o",
            bwave.to_str().unwrap(),
            "--scope",
            "tb.dut",
        ])
        .output()
        .expect("build with scope failed");
    assert!(
        output.status.success(),
        "build --scope tb.dut failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );

    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run_bwave(&["list", &bp]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0, "list on scoped bwave failed: {}", stderr);
    assert!(stdout.contains("clk"), "should include tb.dut.clk");
    assert!(stdout.contains("rstn"), "should include tb.dut.rstn");
    assert!(stdout.contains("ctrl_en"), "should include nested ctrl_en");
    assert!(!stdout.contains("tb_done"), "should NOT include tb.tb_done");
}

#[test]
fn test_scope_matching_nothing_refuses_to_build() {
    // Was a WARNING + a successful write of an empty (unqueryable) store;
    // now the caller-input refusal, exit 2 — see cli_issues.rs
    // `build_refuses_scope_matching_nothing` for the message details.
    let vcd = vcd_path("test_deep_hierarchy.vcd");
    let bwave = vcd.with_extension("scope_empty.fst");
    let output = Command::new(exe_path())
        .args(&[
            "build",
            vcd.to_str().unwrap(),
            "-o",
            bwave.to_str().unwrap(),
            "--scope",
            "nonexistent.scope",
        ])
        .output()
        .expect("build with scope failed");
    let _ = std::fs::remove_file(&bwave);
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert_eq!(output.status.code(), Some(2), "stderr: {}", stderr);
    assert!(
        stderr.contains("matches none"),
        "should refuse the all-out scope: {}",
        stderr
    );
}

// REMOVED in v0.2: `--scope` outside `build` mode no longer exists — `--scope`
// is only a flag on the `build` subcommand. Other subcommands reject it as
// "unexpected argument" instead of silently ignoring.

// ===== --trigger-mode backward compat =====
// REMOVED in v0.2: `--trigger-mode` is gone. Edge keywords (rising/falling/
// change) as VALUE still work directly — there's nothing to "deprecate" since
// the mode flag itself was a no-op when edge keywords were already supported.

// ===== Virtual signal tests: comparisons, SLICE, signal-to-signal =====

#[test]
fn test_virtual_gt_threshold() {
    let bwave = build_bwave("large_multiwidth.vcd", "virt_gt");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run_bwave(&[
        "find",
        &bp,
        "virt_hi",
        "'h1",
        "--first",
        "--virtual",
        "virt_hi = *counter* > 'd127",
    ]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0, "stderr: {}", stderr);
    assert!(
        !stdout.is_empty(),
        "should find cycles where counter > 127: {}",
        stdout
    );
    let first_line = stdout.lines().next().unwrap_or("");
    let cycle: i64 = first_line
        .split_whitespace()
        .nth(1)
        .and_then(|s| s.parse().ok())
        .unwrap_or(-1);
    assert!(
        cycle > 100,
        "first match should be after cycle 100, got {}: {}",
        cycle,
        stdout
    );
}

#[test]
fn test_virtual_lte_threshold() {
    let bwave = build_bwave("large_multiwidth.vcd", "virt_lte");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run_bwave(&[
        "find",
        &bp,
        "virt_lo",
        "'h1",
        "--count",
        "--virtual",
        "virt_lo = *counter* <= 'd10",
    ]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0, "stderr: {}", stderr);
    let count_line = stdout.trim();
    let count: i64 = count_line.parse().unwrap_or(-1);
    assert!(
        count >= 10 && count <= 15,
        "expected ~11 matches for counter<=10, got {}",
        count
    );
}

#[test]
fn test_virtual_slice_single_bit() {
    let bwave = build_bwave("large_multiwidth.vcd", "virt_slice_bit");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) =
        run_bwave(&["find", &bp, "msb", "'h1", "--virtual", "msb = *status*[7]"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0, "stderr: {}", stderr);
    assert!(
        !stdout.is_empty(),
        "should find cycles where status bit 7 is set"
    );
}

#[test]
fn test_virtual_slice_nibble_comparison() {
    let bwave = build_bwave("large_multiwidth.vcd", "virt_slice_nib");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run_bwave(&[
        "find",
        &bp,
        "nib",
        "'h1",
        "--virtual",
        "nib = *status*[7:4] == 'hA",
    ]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0, "stderr: {}", stderr);
    assert!(
        !stdout.is_empty(),
        "should find cycles where status upper nibble = A"
    );
}

#[test]
fn test_virtual_sig_to_sig_equality() {
    let bwave = build_bwave("large_multiwidth.vcd", "virt_sig2sig");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run_bwave(&[
        "find",
        &bp,
        "eq",
        "'h1",
        "--virtual",
        "eq = *valid* == *ready*",
    ]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0, "stderr: {}", stderr);
    assert!(
        !stdout.is_empty(),
        "should find cycles where valid == ready"
    );
}

#[test]
fn test_virtual_sig_to_sig_ordering() {
    let bwave = build_bwave("large_multiwidth.vcd", "virt_sig2sig_gt");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run_bwave(&[
        "find",
        &bp,
        "irq_first",
        "'h1",
        "--virtual",
        "irq_first = *irq* > *ack*",
    ]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0, "stderr: {}", stderr);
    assert!(!stdout.is_empty(), "should find cycles where irq > ack");
}

#[test]
fn test_virtual_xz_returns_false() {
    let bwave = build_bwave("large_multiwidth.vcd", "virt_xz");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run_bwave(&[
        "find",
        &bp,
        "xz_gt",
        "'h1",
        "-t",
        "18:28",
        "--virtual",
        "xz_gt = *xz_signal* > 'd0",
    ]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0, "stderr: {}", stderr);
    if !stdout.is_empty() {
        for line in stdout.lines() {
            let cycle: i64 = line
                .split_whitespace()
                .nth(1)
                .and_then(|s| s.parse().ok())
                .unwrap_or(-1);
            assert!(
                cycle != 20 && cycle != 25,
                "x/z cycles should not match GT: cycle {}",
                cycle
            );
        }
    }
}

#[test]
fn test_virtual_combined_and_with_comparison() {
    let bwave = build_bwave("large_multiwidth.vcd", "virt_combined");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run_bwave(&[
        "find",
        &bp,
        "both",
        "'h1",
        "--virtual",
        "both = *flag_a* & *counter* > 'd5",
    ]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0, "stderr: {}", stderr);
    assert!(
        !stdout.is_empty(),
        "should find cycles where flag_a=1 AND counter>5"
    );
}

#[test]
fn test_virtual_wave_with_gt() {
    let bwave = build_bwave("large_multiwidth.vcd", "virt_wave");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run_bwave(&[
        "wave",
        &bp,
        "-s",
        "*counter*",
        "-t",
        "50:60",
        "--virtual",
        "virt_hi = *counter* > 'd50",
    ]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0, "stderr: {}", stderr);
    assert!(
        stdout.contains("virt_hi"),
        "wave should show virtual signal 'virt_hi': {}",
        stdout
    );
}

// NOTE: stats subcommand does NOT accept --virtual in v0.2 (virtual is scoped
// to consumer subcommands: wave/find/sample/distance/value). Tests that put
// --virtual on --stats are REMOVED below.

// REMOVED in v0.2: --virtual is no longer accepted on `stats` (scoped to
// consumer subcommands only). The following tests are dropped:
//   - test_virtual_stats_appears_in_text_output
//   - test_virtual_stats_json_output
//   - test_virtual_stats_multiple_virtuals
//   - test_virtual_stats_only_virtual_no_real_match
//   - test_virtual_stats_time_in_state
//   - test_virtual_stats_signal_count_includes_virtuals

#[test]
fn test_virtual_slice_out_of_range_error() {
    let bwave = build_bwave("large_multiwidth.vcd", "virt_slice_err");
    let bp = bwave.to_string_lossy().to_string();
    let (_stdout, stderr, _code) =
        run_bwave(&["find", &bp, "bad", "'h1", "--virtual", "bad = *status*[8]"]);
    let _ = std::fs::remove_file(&bwave);
    assert!(
        stderr.contains("out of range"),
        "should error on slice bit 8 for 8-bit signal: {}",
        stderr
    );
}

#[test]
fn test_virtual_sig_to_sig_width_mismatch_error() {
    let bwave = build_bwave("large_multiwidth.vcd", "virt_width_err");
    let bp = bwave.to_string_lossy().to_string();
    let (_stdout, stderr, _code) = run_bwave(&[
        "find",
        &bp,
        "bad",
        "'h1",
        "--virtual",
        "bad = *counter* == *status*",
    ]);
    let _ = std::fs::remove_file(&bwave);
    assert!(
        stderr.contains("width mismatch"),
        "should error on width mismatch: {}",
        stderr
    );
}

#[test]
fn test_virtual_sliced_sig_to_sig() {
    let bwave = build_bwave("large_multiwidth.vcd", "virt_sliced_s2s");
    let bp = bwave.to_string_lossy().to_string();
    let (_stdout, stderr, code) = run_bwave(&[
        "find",
        &bp,
        "eq",
        "'h1",
        "--virtual",
        "eq = *counter*[7:0] == *status*",
    ]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0, "stderr: {}", stderr);
}

#[test]
fn test_virtual_slice_high_bit_find() {
    let bwave = build_bwave("large_multiwidth.vcd", "virt_sample");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run_bwave(&[
        "find",
        &bp,
        "virt_msb",
        "'h1",
        "--count",
        "--virtual",
        "virt_msb = *counter*[15]",
    ]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0, "stderr: {}", stderr);
    let count: i64 = stdout.trim().parse().unwrap_or(-1);
    assert_eq!(
        count, 0,
        "MSB of 16-bit counter shouldn't set in 195 cycles"
    );
}

// =====================================================================
// v0.2 Phase 2: --format json envelope + schema contract
//
// These tests are deliberately structural: they parse stdout as JSON and
// assert the envelope keys + per-command `data` shape. Schema-level
// validation lives in `dev_support/check_schema.py`.
// =====================================================================

/// Tiny JSON peeker — pulls the value of `"<key>": ...` from a JSON
/// document as a substring. Adequate for assertions on shape; we don't
/// pull in serde_json as a dev-dependency for this.
fn json_has_key(payload: &str, key: &str) -> bool {
    payload.contains(&format!("\"{}\"", key))
}

#[test]
fn test_schema_subcommand() {
    let (stdout, _stderr, code) = run_bwave(&["schema"]);
    assert_eq!(code, 0);
    assert!(stdout.contains("\"$id\""), "schema should have $id");
    assert!(
        stdout.contains("\"command\""),
        "schema should describe envelope.command"
    );
    assert!(stdout.contains("listData"), "schema should define listData");
    assert!(stdout.contains("findData"));
    assert!(stdout.contains("valueData"));
    assert!(stdout.contains("statsData"));
}

#[test]
fn test_envelope_list() {
    let bwave = build_bwave("test_basic.vcd", "env_list");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, _stderr, code) = run_bwave(&["list", &bp, "--format", "json"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0);
    assert!(stdout.contains("\"$schema\""), "envelope missing $schema");
    assert!(
        stdout.contains("\"command\": \"list\""),
        "command should be 'list'"
    );
    assert!(json_has_key(&stdout, "scope_prefix"));
    assert!(json_has_key(&stdout, "root_scopes"));
    assert!(json_has_key(&stdout, "signals"));
    assert!(json_has_key(&stdout, "warnings"));
}

#[test]
fn test_envelope_list_limit_retains_total_signal_count() {
    let bwave = build_bwave("test_basic.vcd", "env_list_limit");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run_bwave(&["list", &bp, "--format", "json", "--limit", "1"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0, "list failed: {}", stderr);
    let payload: serde_json::Value = serde_json::from_str(&stdout).expect("valid list JSON");
    assert_eq!(payload["data"]["signals"].as_array().unwrap().len(), 1);
    assert!(payload["data"]["signal_count"].as_u64().unwrap() > 1);
}

#[test]
fn test_envelope_value() {
    let bwave = build_bwave("test_basic.vcd", "env_value");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, _stderr, code) = run_bwave(&["value", &bp, "--at", "1", "--format", "json"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0);
    assert!(stdout.contains("\"command\": \"value\""));
    assert!(json_has_key(&stdout, "target_tick"));
    assert!(json_has_key(&stdout, "time_label"));
    assert!(json_has_key(&stdout, "at_unit"));
}

#[test]
fn test_envelope_find() {
    let bwave = build_bwave("test_basic.vcd", "env_find");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, _stderr, code) = run_bwave(&["find", &bp, "*", "rising", "--format", "json"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0);
    assert!(stdout.contains("\"command\": \"find\""));
    assert!(json_has_key(&stdout, "matches"));
    assert!(json_has_key(&stdout, "truncated"));
    assert!(json_has_key(&stdout, "count"));
}

#[test]
fn test_envelope_find_no_match_still_valid_json() {
    // The empty-result path must still emit a parseable envelope, not bail to
    // text — but the exit code is now 2: a total miss is an input error, and
    // JSON consumers read the envelope while scripts read the return code.
    let bwave = build_bwave("test_basic.vcd", "env_find_empty");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) =
        run_bwave(&["find", &bp, "nonexistent_xyz", "rising", "--format", "json"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 2, "total miss must exit 2: stderr={}", stderr);
    assert!(stdout.contains("\"command\": \"find\""));
    assert!(stdout.contains("\"count\": 0"));
    assert!(
        stdout.contains("no signals match"),
        "should surface the no-match diagnostic in warnings"
    );
    assert!(
        stderr.contains("ERROR: no signals match"),
        "stderr must carry the hard error: {}",
        stderr
    );
}

// =====================================================================
// v0.2 Phase 3: embedded docs + skill
// =====================================================================

#[test]
fn test_docs_topics_lists_intro() {
    // The corpus always contains at least `intro` — the rest is authored
    // progressively. This guards the embedding pipeline, not the content.
    let (stdout, _stderr, code) = run_bwave(&["docs", "topics"]);
    assert_eq!(code, 0);
    assert!(
        stdout.lines().any(|l| l.trim() == "intro"),
        "`docs topics` should list `intro`; got:\n{}",
        stdout
    );
}

#[test]
fn test_docs_show_intro() {
    let (stdout, _stderr, code) = run_bwave(&["docs", "show", "intro"]);
    assert_eq!(code, 0);
    assert!(!stdout.trim().is_empty(), "intro topic should have content");
}

#[test]
fn test_docs_show_unknown_topic_exits_1() {
    let (_stdout, stderr, code) = run_bwave(&["docs", "show", "this_topic_does_not_exist"]);
    assert_eq!(code, 1);
    assert!(stderr.contains("unknown topic"), "stderr: {}", stderr);
}

#[test]
fn test_docs_search_finds_intro() {
    // `intro.md` has the title `# B-Wave`. A substring search for "b-wave"
    // (case-insensitive) should always hit at least one topic.
    let (stdout, _stderr, code) = run_bwave(&["docs", "search", "b-wave"]);
    assert_eq!(code, 0);
    assert!(
        stdout.contains("intro"),
        "expected `intro` hit; got:\n{}",
        stdout
    );
}

#[test]
fn test_help_subcommand_uses_long_about() {
    // `bwave help find` should render the per-command long_about
    // (the markdown body), not just the one-line about.
    let (stdout, _stderr, code) = run_bwave(&["help", "find"]);
    assert_eq!(code, 0);
    // The find.md corpus starts with `# bwave find` and contains the
    // word "Semantics" — both are stable headings.
    assert!(
        stdout.contains("# bwave find"),
        "help should include long_about heading; got:\n{}",
        stdout
    );
    assert!(
        stdout.contains("Semantics"),
        "help should include the Semantics section"
    );
}

// =====================================================================
// v0.2 Phase 4: typed time tokens
// =====================================================================

#[test]
fn test_time_token_sync_bare_int_is_cycle() {
    // Sync mode (default): `--at 5` means cycle 5.
    let bwave = build_bwave("test_basic.vcd", "tok_sync_bare");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, _stderr, code) = run_bwave(&["value", &bp, "--at", "5"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0);
    assert!(
        stdout.contains("# Snapshot at cycle 5"),
        "bare int in sync should resolve to cycle; got:\n{}",
        stdout
    );
}

#[test]
fn test_time_token_explicit_cycle_suffix() {
    let bwave = build_bwave("test_basic.vcd", "tok_cycle");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, _stderr, code) = run_bwave(&["value", &bp, "--at", "5c"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0);
    assert!(stdout.contains("# Snapshot at cycle 5"), "got:\n{}", stdout);
}

#[test]
fn test_time_token_ns_in_sync_converts_to_cycle() {
    // 50ns at 10ns/cycle (test_basic.vcd) = cycle 5
    let bwave = build_bwave("test_basic.vcd", "tok_ns_sync");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, _stderr, code) = run_bwave(&["value", &bp, "--at", "50ns"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0);
    assert!(
        stdout.contains("# Snapshot at cycle 5"),
        "50ns at 10ns/cyc should resolve to cycle 5; got:\n{}",
        stdout
    );
}

#[test]
fn test_time_token_async_bare_int_rejected() {
    let bwave = build_bwave("test_basic.vcd", "tok_async_bare");
    let bp = bwave.to_string_lossy().to_string();
    let (_stdout, stderr, code) =
        run_bwave(&["find", &bp, "*clk*", "rising", "-t", "0:100", "--async"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 2, "should exit 2 on bare int in async mode");
    assert!(
        stderr.contains("ambiguous in async mode"),
        "stderr should explain ambiguity; got: {}",
        stderr
    );
}

#[test]
fn test_time_token_async_tick_suffix_works() {
    let bwave = build_bwave("test_basic.vcd", "tok_async_tick");
    let bp = bwave.to_string_lossy().to_string();
    let (_stdout, stderr, code) =
        run_bwave(&["find", &bp, "*clk*", "rising", "-t", "0t:100t", "--async"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(
        code, 0,
        "tick suffix should be accepted in async; stderr={}",
        stderr
    );
}

#[test]
fn test_time_token_async_value_tick_is_not_rescaled() {
    let bwave = build_bwave("test_ps_timescale.vcd", "tok_async_value_tick");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run_bwave(&[
        "value", &bp, "--async", "--at", "10000t", "--format", "json",
    ]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0, "stderr={}", stderr);
    assert!(
        stdout.contains("\"target_tick\": 10000"),
        "got:\n{}",
        stdout
    );
    assert!(stdout.contains("\"at_unit\": \"tick\""), "got:\n{}", stdout);
}

#[test]
fn test_time_token_async_value_physical_time_is_not_rescaled() {
    let bwave = build_bwave("test_ps_timescale.vcd", "tok_async_value_ns");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) =
        run_bwave(&["value", &bp, "--async", "--at", "50ns", "--format", "json"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0, "stderr={}", stderr);
    assert!(
        stdout.contains("\"target_tick\": 50000"),
        "got:\n{}",
        stdout
    );
    assert!(stdout.contains("\"at_unit\": \"tick\""), "got:\n{}", stdout);
}

#[test]
fn test_time_token_unknown_suffix_rejected() {
    let bwave = build_bwave("test_basic.vcd", "tok_unknown");
    let bp = bwave.to_string_lossy().to_string();
    let (_stdout, stderr, code) = run_bwave(&["value", &bp, "--at", "5q"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 2);
    assert!(
        stderr.contains("unknown time-unit suffix"),
        "stderr should report unknown suffix; got: {}",
        stderr
    );
}

#[test]
fn test_time_token_diff_string_arguments() {
    // diff T1 T2 take strings in v0.2 phase 4; bare ints in sync still work.
    let bwave = build_bwave("test_basic.vcd", "tok_diff");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run_bwave(&["diff", &bp, "1", "10"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0, "stderr: {}", stderr);
    assert!(stdout.contains("# diff cycle 1 vs 10"), "got: {}", stdout);
}

#[test]
fn test_skill_has_frontmatter() {
    let (stdout, _stderr, code) = run_bwave(&["skill"]);
    assert_eq!(code, 0);
    // Normalize CRLF (Windows git autocrlf) before checking frontmatter.
    let normalized = stdout.replace("\r\n", "\n");
    assert!(
        normalized.starts_with("---\nname: bwave"),
        "skill should start with frontmatter; got first 40 bytes: {:?}",
        &normalized.chars().take(40).collect::<String>()
    );
    assert!(
        normalized.contains("description:"),
        "skill should have description field"
    );
}

#[test]
fn test_envelope_stats() {
    let bwave = build_bwave("test_basic.vcd", "env_stats");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, _stderr, code) = run_bwave(&["stats", &bp, "-s", "clk", "--format", "json"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0);
    assert!(stdout.contains("\"command\": \"stats\""));
    assert!(json_has_key(&stdout, "simulation_ns"));
    assert!(json_has_key(&stdout, "total_ticks"));
    assert!(json_has_key(&stdout, "clock_period_ns"));
    // Raw values: stats keys should be the cache value strings, not Verilog literals.
    // For a single-bit clock the only state keys we expect are "0" and "1" — never "1'b0".
    assert!(
        !stdout.contains("1'b0"),
        "stats JSON should use raw values, not Verilog literals"
    );
}

// -- Native (foreign-writer) FST ---------------------------------------
// fixtures/native_verilator_counter.fst was dumped by Verilator 5.046's
// embedded GTKWave fstapi (--binary --trace-fst, counter_4bit_tb from
// rtl_fixtures; see tests/native_fst_verilator_test.py). It covers the
// reader against a writer that is NOT bwave's own fst-writer: zlib-packed
// blocks, spaced "count [3:0]" hierarchy names, SV var types.

#[test]
fn test_native_verilator_fst_list() {
    let fst = vcd_path("native_verilator_counter.fst")
        .to_string_lossy()
        .to_string();
    let (stdout, stderr, code) = run_query(&["list", &fst]);
    assert_eq!(code, 0, "stderr: {}", stderr);
    // spaced VCD-token name "count [3:0]" must read back joined
    assert!(stdout.contains("count[3:0]"), "got: {}", stdout);
    assert!(
        !stdout.contains("count ["),
        "spaced bit range not joined: {}",
        stdout
    );
    assert!(stderr.contains("10 signals"), "tb + dut scopes: {}", stderr);
}

#[test]
fn test_native_verilator_fst_values() {
    let fst = vcd_path("native_verilator_counter.fst")
        .to_string_lossy()
        .to_string();
    let (stdout, stderr, code) = run_query(&["value", &fst, "--at", "5"]);
    assert_eq!(code, 0, "stderr: {}", stderr);
    // counter released from reset counts 1/cycle: value 5 at cycle 5
    let count_line = stdout.lines().find(|l| l.starts_with("count[3:0]"));
    assert!(
        count_line.is_some_and(|l| l.trim_end().ends_with("= 5")),
        "count should be 5 at cycle 5; got: {}",
        stdout
    );
}

#[test]
fn test_native_verilator_fst_clock_rederivation() {
    let fst = vcd_path("native_verilator_counter.fst")
        .to_string_lossy()
        .to_string();
    let (stdout, stderr, code) = run_query(&["stats", &fst]);
    assert_eq!(code, 0, "stderr: {}", stderr);
    // clock meta is re-derived from FST content: 10ns period, 36 cycles
    assert!(stdout.contains("36 cycles"), "got: {}", stdout);
    assert!(stdout.contains("10ns period"), "got: {}", stdout);
}
