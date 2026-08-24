//! End-to-end CLI reproducers for seven fixed bwave CLI issues. Each
//! `issue_N_*` test runs the compiled binary against a fixture VCD and
//! asserts the user-visible behavior the fix promised.
//!
//! Also includes a help-example regression test: every virtual-signal
//! example in the `--help` long_about must parse cleanly through the live
//! parser, so doc drift can't ship.

use std::path::PathBuf;
use std::process::{Command, Stdio};

#[cfg(unix)]
use std::io::{Read, Write};

// -- Test harness ----------------------------------------------------

/// Path to the binary cargo just built for THIS test run — see the same helper
/// in integration_test.rs for why this is not `target/debug/bwave`.
fn exe_path() -> PathBuf {
    PathBuf::from(env!("CARGO_BIN_EXE_bwave"))
}

fn fixture(name: &str) -> PathBuf {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.push("tests");
    p.push("fixtures");
    p.push(name);
    p
}

/// Build an `.fst` store from a fixture VCD; returns the store path. Uses a
/// unique suffix per test to avoid parallel collisions. (Runs against the FST
/// backend — the primary store after the migration.)
fn build_bwave(vcd_name: &str, test_id: &str) -> PathBuf {
    let vcd = fixture(vcd_name);
    let bwave = vcd.with_extension(format!("{}.fst", test_id));
    let _ = std::fs::remove_file(&bwave);
    // v0.2: `build INPUT -o OUTPUT`
    let out = Command::new(exe_path())
        .args(&[
            "build",
            vcd.to_str().unwrap(),
            "-o",
            bwave.to_str().unwrap(),
        ])
        .output()
        .expect("build failed to execute");
    assert!(
        out.status.success(),
        "build failed for {}: {}",
        vcd_name,
        String::from_utf8_lossy(&out.stderr)
    );
    bwave
}

/// Run the CLI; return (stdout, stderr, exit_code).
fn run(args: &[&str]) -> (String, String, i32) {
    let out = Command::new(exe_path())
        .args(args)
        .output()
        .expect("failed to execute bwave");
    (
        String::from_utf8_lossy(&out.stdout).to_string(),
        String::from_utf8_lossy(&out.stderr).to_string(),
        out.status.code().unwrap_or(-1),
    )
}

#[test]
fn build_default_uses_parallel_engine() {
    let vcd = fixture("small_clocked.vcd");
    let default_store = vcd.with_extension("default_engine.fst");
    let parallel_store = vcd.with_extension("explicit_parallel.fst");
    for store in [&default_store, &parallel_store] {
        let _ = std::fs::remove_file(store);
    }

    let (_, default_stderr, default_code) = run(&[
        "build",
        vcd.to_str().unwrap(),
        "-o",
        default_store.to_str().unwrap(),
    ]);
    let (_, parallel_stderr, parallel_code) = run(&[
        "build",
        "--engine",
        "parallel",
        vcd.to_str().unwrap(),
        "-o",
        parallel_store.to_str().unwrap(),
    ]);

    assert_eq!(default_code, 0, "default build failed: {default_stderr}");
    assert_eq!(parallel_code, 0, "parallel build failed: {parallel_stderr}");
    assert_eq!(
        std::fs::read(&default_store).unwrap(),
        std::fs::read(&parallel_store).unwrap()
    );
    for store in [default_store, parallel_store] {
        let _ = std::fs::remove_file(store);
    }
}

// -- Issue 1: `find` silently mixes signals via substring match -----

#[test]
fn issue_1_find_no_silent_mix() {
    // Two 13-bit signals: `dmem_addr` (value 0x0C1C at t≥20) and
    // `dmem_addr_next` (constant ≠ 0x0C1C). Bare pattern `dmem_addr`
    // must suffix-match — and find the value only in the real `dmem_addr`,
    // NOT in `dmem_addr_next`.
    let bwave = build_bwave("test_ambiguous_names.vcd", "issue1");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run(&["find", &bp, "dmem_addr", "'h0C1C", "--async"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0, "command failed: stderr={}", stderr);

    // Each match line is prefixed with `# signal: <name>` (per the existing
    // multi-signal find format), or the matched name appears on the value
    // line. Either way: the *plain* sibling must NOT appear in the output.
    assert!(
        !stdout.contains("dmem_addr_next"),
        "bare pattern leaked into `dmem_addr_next`:\n{}",
        stdout
    );
    // And the real signal must produce output (we know 0x0C1C is set at t=20).
    assert!(
        !stdout.is_empty(),
        "expected at least one match in dmem_addr, got empty"
    );
}

// -- Issue 2: value literals reject bare `1` and `1'b1` ---------------

#[test]
fn issue_2_bare_decimal_accepted() {
    // `find ... 1` (bare decimal) must parse and not exit with a literal error.
    let bwave = build_bwave("test_unpacked_array.vcd", "issue2_dec");
    let bp = bwave.to_string_lossy().to_string();
    let (_stdout, stderr, code) = run(&["find", &bp, "dmem_wr[0]", "1", "--async"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(
        code, 0,
        "bare decimal '1' should be accepted: stderr={}",
        stderr
    );
}

#[test]
fn issue_2_width_prefixed_accepted() {
    // `1'b1` is a width-prefixed Verilog literal.
    let bwave = build_bwave("test_unpacked_array.vcd", "issue2_wp");
    let bp = bwave.to_string_lossy().to_string();
    let (_stdout, stderr, code) = run(&["find", &bp, "dmem_wr[0]", "1'b1", "--async"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(
        code, 0,
        "width-prefixed `1'b1` should be accepted: stderr={}",
        stderr
    );
}

#[test]
fn issue_2_bare_hex_rejected_with_hint() {
    // Bare hex `C1C` must still error, with a "decimal-only" hint.
    let bwave = build_bwave("test_ambiguous_names.vcd", "issue2_hex");
    let bp = bwave.to_string_lossy().to_string();
    let (_stdout, stderr, code) = run(&["find", &bp, "dmem_addr", "C1C", "--async"]);
    let _ = std::fs::remove_file(&bwave);
    assert_ne!(code, 0, "bare hex `C1C` must be rejected");
    assert!(
        stderr.contains("decimal-only"),
        "expected 'decimal-only' hint in error, got: {}",
        stderr
    );
}

// -- Issue 3: `list scope.*` used to produce a confusing error -------

#[test]
fn issue_3_list_accepts_positional_pattern() {
    // `bwave list FILE -s <pattern>` must not error on the pattern filter.
    let bwave = build_bwave("test_ambiguous_names.vcd", "issue3");
    let bp = bwave.to_string_lossy().to_string();
    // v0.2: the old positional pattern shortcut is gone — pass via -s explicitly.
    let (stdout, stderr, code) = run(&["list", &bp, "-s", "tb.dut.*"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(
        code, 0,
        "pattern via -s should work with list: stderr={}",
        stderr
    );
    // Both signals are under tb.dut → both should appear.
    assert!(
        stdout.contains("dmem_addr"),
        "should list dmem_addr: {}",
        stdout
    );
}

// -- Issue 4a: bit-indexed signal resolution -------------------------

#[test]
fn issue_4a_single_index_resolves_to_literal_name() {
    // Virtual `*dmem_wr[0]` must resolve to the LITERAL scalar signal
    // `tb.dut.dmem_wr[0]`, not silently fall back to a bit slice of some
    // wider name. Probe via `find any_wr 1`: at least one cycle must see
    // dmem_wr[0] or dmem_wr[1] high.
    let bwave = build_bwave("test_unpacked_array.vcd", "issue4a");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run(&[
        "find",
        &bp,
        "any_wr",
        "1",
        "--with-reset",
        "--virtual",
        "any_wr = *dmem_wr[0] | *dmem_wr[1]",
    ]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0, "virtual + find should succeed: stderr={}", stderr);
    assert!(
        stdout.contains("any_wr 1"),
        "expected at least one cycle with any_wr=1, got stdout: {}",
        stdout
    );
    // Crucially: stderr must NOT show a "matches N signals" error — that's
    // the failure mode the fix avoided.
    assert!(
        !stderr.contains("matches") || !stderr.contains("signals (must be exactly 1)"),
        "literal-name resolution silently fell back, stderr={}",
        stderr
    );
}

#[test]
fn issue_4a_ambiguous_literal_hard_errors() {
    // `*wr[0]` matches BOTH `dmem_wr[0]` and `imem_wr[0]` literally → hard
    // error from build_virtuals (logged to stderr; process now exits 2 so
    // CI/scripts can detect bad `--virtual` defs without scraping stderr).
    let bwave = build_bwave("test_unpacked_array.vcd", "issue4a_amb");
    let bp = bwave.to_string_lossy().to_string();
    let (_stdout, stderr, code) = run(&[
        "find",
        &bp,
        "v",
        "1",
        "--with-reset",
        "--virtual",
        "v = *wr[0]",
    ]);
    let _ = std::fs::remove_file(&bwave);
    assert!(
        stderr.contains("ERROR: --virtual")
            && (stderr.contains("literal") || stderr.contains("must be exactly 1")),
        "expected literal-multimatch error in stderr, got: {}",
        stderr
    );
    assert_eq!(
        code, 2,
        "bad --virtual must exit non-zero, got {} (stderr={})",
        code, stderr
    );
}

// -- Issue 4b: `(*sig) == val` must parse ----------------------------

#[test]
fn issue_4b_paren_single_signal_compare_parses() {
    // Probe via find: if parse fails, build_virtuals emits an ERROR to
    // stderr and the virtual is dropped. A clean parse produces no
    // "ERROR: --virtual" line.
    let bwave = build_bwave("test_unpacked_array.vcd", "issue4b_ok");
    let bp = bwave.to_string_lossy().to_string();
    let (_stdout, stderr, code) = run(&[
        "find",
        &bp,
        "v",
        "1",
        "--with-reset",
        "--virtual",
        "v = (*dmem_wr[0]) == 'h1",
    ]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0, "stderr: {}", stderr);
    assert!(
        !stderr.contains("ERROR: --virtual"),
        "paren-wrapped single signal compare should parse cleanly, stderr={}",
        stderr
    );
}

#[test]
fn issue_4b_paren_combine_compare_rejects() {
    let bwave = build_bwave("test_unpacked_array.vcd", "issue4b_bad");
    let bp = bwave.to_string_lossy().to_string();
    let (_stdout, stderr, code) = run(&[
        "find",
        &bp,
        "v",
        "1",
        "--with-reset",
        "--virtual",
        "v = (*dmem_wr[0] & *dmem_wr[1]) == 'h0",
    ]);
    let _ = std::fs::remove_file(&bwave);
    let lower = stderr.to_lowercase();
    assert!(
        stderr.contains("ERROR: --virtual")
            && (lower.contains("single signal atom")
                || lower.contains("paren")
                || lower.contains("cannot be compared")),
        "expected paren-pointing parse error in stderr, got: {}",
        stderr
    );
    // Bad --virtual must surface as non-zero exit, not just stderr noise.
    assert_eq!(
        code, 2,
        "bad --virtual must exit non-zero, got {} (stderr={})",
        code, stderr
    );
}

// -- Issue 4c: sliced equality with X/Z outside slice ----------------

#[test]
fn issue_4c_sliced_eq_with_xz_outside_slice() {
    // 32-bit `addr` has X in upper 8 bits while lower 13 bits = 0x0C1C.
    // Slice [12:0] == 13'h0C1C must be true despite upper-bit X.
    let bwave = build_bwave("test_wide_xz_outside_slice.vcd", "issue4c");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run(&[
        "find",
        &bp,
        "hit",
        "1",
        "--with-reset",
        "--virtual",
        "hit = *addr[12:0] == 13'h0C1C",
    ]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0, "find must succeed: stderr={}", stderr);
    assert!(
        stdout.contains("hit 1"),
        "expected at least one match where slice [12:0] == 0x0C1C, got: {}",
        stdout
    );
}

// -- Issue 5: `list` gets a count hint -------------------------------

#[test]
fn issue_5_list_emits_count_hint() {
    let bwave = build_bwave("test_ambiguous_names.vcd", "issue5");
    let bp = bwave.to_string_lossy().to_string();
    let (_stdout, stderr, code) = run(&["list", &bp]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0);
    assert!(
        stderr.contains("signals")
            && stderr.contains("narrow with -s")
            && stderr.contains("--tree"),
        "expected count-with-hint footer, got stderr: {}",
        stderr
    );
}

#[test]
fn issue_5_tree_flag_suppresses_leaves() {
    // `list FILE --tree` prints scopes only, no leaf signal names.
    let bwave = build_bwave("test_ambiguous_names.vcd", "issue5_tree");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run(&["list", &bp, "--tree"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0, "stderr={}", stderr);
    // Scope nodes appear as "<name> (N signals)" lines; leaf signal lines
    // are formatted as "<name> wire/reg N-bit" — those must NOT show up.
    assert!(
        !stdout.contains("wire") && !stdout.contains("-bit"),
        "tree mode should suppress per-leaf detail, got: {}",
        stdout
    );
}

// -- Issue 6: `value --at N` single-cycle snapshot -------------------

#[test]
fn issue_6_at_flag_single_cycle_snapshot() {
    let bwave = build_bwave("test_ambiguous_names.vcd", "issue6");
    let bp = bwave.to_string_lossy().to_string();
    // Async mode: `value --at N` treats N as a timestamp. v0.2 phase 4
    // requires an explicit unit suffix in async mode — `ns` matches the
    // historical interpretation under the default 1ns timescale.
    let (stdout, stderr, code) = run(&["value", &bp, "--at", "20ns", "--async"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0, "stderr={}", stderr);
    assert!(
        stdout.contains("# Snapshot at"),
        "expected snapshot header, got: {}",
        stdout
    );
    // Both signals at t=20 should be reported.
    assert!(
        stdout.contains("dmem_addr"),
        "should show dmem_addr value: {}",
        stdout
    );
}

// -- Issue 7: mode-conflict error shows full "Pick one" list ---------
// REMOVED in v0.2: clap subcommands make modes mutually exclusive at the
// parser level — there's no longer a "Pick one" advisory because you can't
// even spell two query modes in the same invocation. The error surface is
// now clap's standard "subcommand expected" / "unexpected argument".

// -- Help-example regression -----------------------------------------
//
// Every virtual-signal example shipped in `--help` must parse cleanly through
// the live parser. If someone edits the long_about text without keeping the
// parser in sync, this test catches it before users do.

#[test]
fn help_virtual_examples_parse() {
    // Capture --help output (clap prints to stdout).
    // v0.2: virtual examples live under `bwave help find` (or similar
    // consumer-subcommand help). Probe top-level --help first; if that
    // doesn't surface any, walk consumer subcommands.
    let subcommands = ["", "find", "sample", "wave", "value", "distance"];
    let mut examples: Vec<String> = Vec::new();
    for sub in &subcommands {
        let mut cmd = Command::new(exe_path());
        if !sub.is_empty() {
            cmd.arg(sub);
        }
        cmd.arg("--help");
        let out = cmd.output().expect("failed to execute bwave --help");
        let help = String::from_utf8_lossy(&out.stdout);
        for line in help.lines() {
            if let Some(start) = line.find("--virtual \"") {
                let after = &line[start + "--virtual \"".len()..];
                if let Some(end) = after.find('"') {
                    examples.push(after[..end].to_string());
                }
            }
        }
    }
    assert!(
        examples.len() >= 4,
        "expected at least 4 --virtual examples across --help outputs, found {}: {:?}",
        examples.len(),
        examples
    );

    // Parse each example through the live parser. The library is the same
    // one main.rs uses, so a parse-clean example proves the help text isn't
    // showing syntax the parser would reject.
    for example in &examples {
        match bwave::virtual_signal::parse_virtual_def(example) {
            Ok(_) => {}
            Err(e) => panic!(
                "help example '{}' failed to parse: {}\n\
                 If the example is intentionally invalid, update --help long_about \
                 to remove or fix it.",
                example, e
            ),
        }
    }
}

// -- Async-mode virtual find no longer silently skipped --------------
//
// Pre-fix: `find_value_from_cache` only walked virtual transitions when
// edge_mode was set OR use_cycle_walk was true. With `--async` (sync_mode
// false) and a level-value match, use_cycle_walk evaluates to false and
// edge_mode is None — both branches fell through and the virtual was
// dropped silently. Tests had to use sync + `--with-reset` to work
// around it. This test locks in the fix.
#[test]
fn async_virtual_find_emits_matches() {
    let bwave = build_bwave("test_unpacked_array.vcd", "async_virt");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run(&[
        "find",
        &bp,
        "any_wr",
        "1",
        "--async",
        "--virtual",
        "any_wr = *dmem_wr[0] | *dmem_wr[1]",
    ]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(
        code, 0,
        "async virtual find should succeed: stderr={}",
        stderr
    );
    assert!(
        stdout.contains("any_wr 1"),
        "expected at least one async match where any_wr=1, got stdout: {}\nstderr: {}",
        stdout,
        stderr
    );
    assert!(
        !stderr.contains("No matches found"),
        "virtual was silently skipped — async fall-through regression: stderr={}",
        stderr
    );
}

// -- Bad --virtual must produce non-zero exit code --------------------
//
// Pre-fix: parse/resolve errors in build_virtuals were logged to stderr
// but the process exited 0. CI/scripts couldn't distinguish "no matches"
// from "your virtual def was garbage" without parsing stderr.
#[test]
fn bad_virtual_def_exits_nonzero() {
    let bwave = build_bwave("test_unpacked_array.vcd", "bad_virt_exit");
    let bp = bwave.to_string_lossy().to_string();
    // Parse error: missing RHS after `=`. Use find so build_virtuals runs
    // (list doesn't touch virtuals).
    let (_stdout, stderr, code) =
        run(&["find", &bp, "v", "1", "--with-reset", "--virtual", "v = "]);
    let _ = std::fs::remove_file(&bwave);
    assert!(
        stderr.contains("ERROR: --virtual"),
        "expected ERROR: --virtual in stderr, got: {}",
        stderr
    );
    assert_eq!(
        code, 2,
        "bad --virtual must exit 2, got {} (stderr={})",
        code, stderr
    );
}

// ===================================================================
//   Field findings: silent filter drops, unreadable wide waves, empty
//   held-value windows, unbounded `list`
//   (benchmark batches 1-2 bwave usage review — see MEMORY notes)
// ===================================================================

// -- Unknown radix suffix must be an error, not a silent drop ---------
//
// Observed: a `wave` with nine `-s` filters rendered two rows. Seven
// carried `%u` (not a radix), so their patterns kept the suffix, matched
// nothing, and vanished — the header cheerfully said "2 signals".
#[test]
fn unknown_radix_suffix_exits_nonzero() {
    let bwave = build_bwave("test_wide_signals.vcd", "radix_unknown");
    let bp = bwave.to_string_lossy().to_string();
    let (_stdout, stderr, code) =
        run(&["wave", &bp, "-s", "huge512%u", "--async", "-t", "0ns:30ns"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 2, "unknown radix must exit 2 (stderr={})", stderr);
    assert!(
        stderr.contains("unknown radix suffix '%u'"),
        "expected radix diagnostic, got: {}",
        stderr
    );
    assert!(
        stderr.contains("%d"),
        "error should name the valid radixes: {}",
        stderr
    );
}

// -- A pattern that matches nothing must say so -----------------------
//
// Even when a sibling pattern matched, so the query still produced output.
#[test]
fn unmatched_pattern_is_reported() {
    let bwave = build_bwave("test_wide_signals.vcd", "unmatched_pat");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run(&[
        "wave",
        &bp,
        "-s",
        "byte8",
        "-s",
        "no_such_signal_here",
        "--async",
        "-t",
        "0ns:30ns",
    ]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(
        code, 0,
        "query with one good pattern still runs: {}",
        stderr
    );
    assert!(!stdout.is_empty(), "matched pattern should still render");
    assert!(
        stderr.contains("no signals match 'no_such_signal_here'"),
        "dropped filter must be named on stderr, got: {}",
        stderr
    );
    assert!(
        stderr.contains("1 of 2 -s patterns matched nothing"),
        "expected drop summary, got: {}",
        stderr
    );
}

// -- Indexed array element miss carries a memory-dump hint ------------
#[test]
fn unmatched_array_element_hints_at_dumping() {
    let bwave = build_bwave("test_unpacked_array.vcd", "unmatched_arr");
    let bp = bwave.to_string_lossy().to_string();
    let (_stdout, stderr, _code) = run(&[
        "signal",
        &bp,
        "-s",
        "dmem_wr[0]",
        "-s",
        "mem[56]",
        "--async",
    ]);
    let _ = std::fs::remove_file(&bwave);
    assert!(
        stderr.contains("no signals match 'mem[56]'"),
        "expected miss report, got: {}",
        stderr
    );
    assert!(
        stderr.contains("unpacked"),
        "indexed miss should explain array dumping, got: {}",
        stderr
    );
}

// -- Wide signals must not turn `wave` into a wall of padding ---------
//
// A 256-bit bus renders 64 hex chars per cell; that width was applied to
// every column, so a two-signal wave blew the output budget on spaces.
#[test]
fn wave_elides_over_wide_values() {
    let bwave = build_bwave("test_wide_signals.vcd", "wide_elide");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run(&["wave", &bp, "-s", "huge512", "--async", "-t", "0ns:30ns"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0, "wave failed: {}", stderr);
    let widest = stdout.lines().map(|l| l.len()).max().unwrap_or(0);
    assert!(
        widest < 200,
        "256-bit bus should be elided, widest line was {} chars:\n{}",
        widest,
        stdout
    );
    assert!(
        stdout.contains(".."),
        "elided cells carry a '..' marker:\n{}",
        stdout
    );
    assert!(
        stderr.contains("values elided"),
        "elision must be announced, got: {}",
        stderr
    );
}

// -- `signal` over a quiet window shows the held value ----------------
//
// `signal -s sig -t N:N` printed nothing when the signal did not change
// inside the window, which reads as "no such signal" — the agent then
// re-derived that `value --at` was the query it wanted.
#[test]
fn signal_falls_back_to_held_value() {
    let bwave = build_bwave("small_clocked.vcd", "held_value");
    let bp = bwave.to_string_lossy().to_string();
    // A one-cycle window on the clock's own steady-state neighbourhood:
    // pick a late cycle so the pre-window value is already established.
    let (stdout, stderr, code) = run(&["signal", &bp, "-s", "*", "-t", "3:3"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0, "signal failed: {}", stderr);
    assert!(
        !stdout.is_empty(),
        "a quiet window must still report values, stderr={}",
        stderr
    );
    if stderr.contains("no transitions") {
        assert!(
            stderr.contains("held values"),
            "fallback must explain itself, got: {}",
            stderr
        );
    }
}

// -- `list` honors --limit instead of accepting-and-ignoring it -------
#[test]
fn list_honors_limit() {
    let bwave = build_bwave("test_many_signals.vcd", "list_limit");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run(&["list", &bp, "--limit", "5"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0, "list failed: {}", stderr);
    assert!(
        stderr.contains("limit (5) reached"),
        "truncation must be explicit, got: {}",
        stderr
    );
    assert!(
        stdout.lines().count() < 40,
        "limited list should be short, got {} lines",
        stdout.lines().count()
    );
}

// -- An element-indexed name must resolve to that element -------------
//
// `-s "dmem_wr[0]"` matched nothing, though the trace declares exactly
// that name: globset read `[0]` as a character class, so the pattern also
// skipped the bare-name `*` wrap and could never match a hierarchical
// name. Array elements looked absent from every trace.
#[test]
fn indexed_element_pattern_matches_its_signal() {
    let bwave = build_bwave("test_unpacked_array.vcd", "indexed_elem");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run(&["list", &bp, "-s", "dmem_wr[0]"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0, "list failed: {}", stderr);
    assert!(
        stdout.contains("dmem_wr[0]"),
        "indexed element must be found, got stdout={} stderr={}",
        stdout,
        stderr
    );
    assert!(
        !stderr.contains("no signals match"),
        "must not report a dropped filter: {}",
        stderr
    );
}

// A real character class keeps class semantics — the index fix must not
// swallow `[0-3]`-style patterns.
#[test]
fn character_class_pattern_still_globs() {
    let bwave = build_bwave("test_unpacked_array.vcd", "char_class");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, _stderr, code) = run(&["list", &bp, "-s", "*dmem_wr[0-9]*"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0);
    assert!(
        stdout.contains("dmem_wr"),
        "class pattern should still match: {}",
        stdout
    );
}

// -- Loud-fail: empty stores and total pattern misses -----------------
//
// A zero-signal store (header-only trace) and a query where ALL -s
// patterns miss both used to "succeed" with empty output, so the agent
// debugged the design instead of the trace or the glob. Queries now exit
// 2 for both; `list` stays exit 0 (it's the discovery tool) but names the
// empty store with an ERROR line.

/// A VCD that declares no signals at all — the exact shape a Verilator sim
/// traced via the auto-generated --main produces (header-only trace.fst).
const EMPTY_VCD: &str = "$timescale 1ns $end\n$scope module tb $end\n$upscope $end\n\
     $enddefinitions $end\n#0\n#10\n";

/// Build a header-only store IN-PROCESS via the library. `bwave build`
/// itself now refuses a zero-signal VCD (exit 2, see
/// `build_refuses_zero_signal_vcd`), but such stores still arrive from
/// external producers — Verilator's auto --main writes the FST directly —
/// so the query-side gates must keep handling them.
fn build_empty_store(test_id: &str) -> PathBuf {
    let fst = std::env::temp_dir().join(format!("bwave_empty_{}.fst", test_id));
    let mut reader = std::io::Cursor::new(EMPTY_VCD.as_bytes());
    let header = bwave::parser::parse_header(&mut reader);
    let mut handler = bwave::fst::FstBuildHandler::new(&header, None, &fst)
        .expect("in-process empty-store build failed");
    handler.parse_bytes(&mut reader, None).unwrap();
    handler.finalize_and_write().unwrap();
    fst
}

#[test]
fn build_refuses_zero_signal_vcd() {
    // Building a header-only store used to "succeed" (exit 0, `# wrote`),
    // arming a store that answers every query with silence. Now the producer
    // side fails loudly too, at the moment the mistake is cheapest to fix.
    let vcd = std::env::temp_dir().join("bwave_empty_refuse.vcd");
    let fst = std::env::temp_dir().join("bwave_empty_refuse.fst");
    std::fs::write(&vcd, EMPTY_VCD).expect("write empty vcd");
    let out = Command::new(exe_path())
        .args(&["build", vcd.to_str().unwrap(), "-o", fst.to_str().unwrap()])
        .output()
        .expect("build failed to execute");
    let stderr = String::from_utf8_lossy(&out.stderr).to_string();
    let _ = std::fs::remove_file(&vcd);
    let _ = std::fs::remove_file(&fst);
    assert_eq!(
        out.status.code(),
        Some(2),
        "zero-signal build must exit 2: {}",
        stderr
    );
    assert!(
        stderr.contains("declares no signals"),
        "error must name the cause: {}",
        stderr
    );
    assert!(
        stderr.contains("--main"),
        "error should point at the Verilator auto --main trap: {}",
        stderr
    );
    assert!(!fst.exists(), "no store file may be left behind");
}

#[test]
fn build_rejects_invalid_timestamp_without_publishing_output() {
    let vcd = std::env::temp_dir().join("bwave_invalid_timestamp.vcd");
    let fst = std::env::temp_dir().join("bwave_invalid_timestamp.fst");
    let input = "$timescale 1ns $end\n\
        $scope module tb $end\n\
        $var wire 1 ! sig $end\n\
        $upscope $end\n\
        $enddefinitions $end\n\
        #12junk\n\
        1!\n";
    std::fs::write(&vcd, input).expect("write malformed VCD");
    let _ = std::fs::remove_file(&fst);

    for engine in ["serial", "parallel"] {
        let _ = std::fs::remove_file(&fst);
        let out = Command::new(exe_path())
            .args([
                "build",
                "--engine",
                engine,
                "--chunk-bytes",
                "17",
                vcd.to_str().unwrap(),
                "-o",
                fst.to_str().unwrap(),
            ])
            .output()
            .expect("build failed to execute");
        let stderr = String::from_utf8_lossy(&out.stderr);
        assert_eq!(out.status.code(), Some(1), "{engine} stderr: {stderr}");
        assert!(
            stderr.contains("invalid VCD timestamp"),
            "{engine} stderr: {stderr}"
        );
        assert!(
            stderr.contains("trailing characters"),
            "{engine} stderr: {stderr}"
        );
        assert!(
            !fst.exists(),
            "failed {engine} build must not publish an FST"
        );
    }

    let _ = std::fs::remove_file(&vcd);
}

#[cfg(unix)]
#[test]
fn parallel_failure_cancels_a_blocked_fifo_reader() {
    let base = std::env::temp_dir().join(format!(
        "bwave_cancel_fifo_{}_{}",
        std::process::id(),
        std::thread::current().name().unwrap_or("test")
    ));
    let fifo = base.with_extension("fifo");
    let fst = base.with_extension("fst");
    let _ = std::fs::remove_file(&fifo);
    let _ = std::fs::remove_file(&fst);
    let mkfifo = Command::new("mkfifo").arg(&fifo).status().unwrap();
    assert!(mkfifo.success());

    let mut child = Command::new(exe_path())
        .args([
            "build",
            "--engine",
            "parallel",
            "--parse-jobs",
            "2",
            "--encode-jobs",
            "1",
            "--chunk-bytes",
            "8",
            "--input",
            fifo.to_str().unwrap(),
            "-o",
            fst.to_str().unwrap(),
        ])
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut writer = std::fs::File::create(&fifo).unwrap();
    writer
        .write_all(
            b"$scope module tb $end\n\
$var wire 1 ! sig $end\n\
$upscope $end\n\
$enddefinitions $end\n\
#0\n0!\n#10\n1!\n#bad\n0!\n#30\n1!\n#40\n0!\n#50\n1!\n#60\n0!\n#70\n1!\n#80\n0!\n",
        )
        .unwrap();
    writer.flush().unwrap();

    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(2);
    let status = loop {
        if let Some(status) = child.try_wait().unwrap() {
            break status;
        }
        if std::time::Instant::now() >= deadline {
            child.kill().unwrap();
            panic!("parallel build did not cancel while its FIFO writer remained open");
        }
        std::thread::sleep(std::time::Duration::from_millis(10));
    };
    let mut stderr = String::new();
    child
        .stderr
        .take()
        .unwrap()
        .read_to_string(&mut stderr)
        .unwrap();

    let producer_error = writer.write_all(b"#90\n1!\n").unwrap_err();
    assert_eq!(producer_error.kind(), std::io::ErrorKind::BrokenPipe);
    drop(writer);
    let _ = std::fs::remove_file(&fifo);
    let output_exists = fst.exists();
    let _ = std::fs::remove_file(&fst);
    assert_eq!(status.code(), Some(1), "stderr: {stderr}");
    assert!(stderr.contains("invalid VCD timestamp"), "stderr: {stderr}");
    assert!(!output_exists, "cancelled build must not publish an FST");
}

#[test]
fn build_refuses_scope_matching_nothing() {
    // Same refusal for a --scope that filters every signal out — previously
    // a WARNING followed by a successful write of an empty store.
    let vcd = fixture("test_basic.vcd");
    let fst = std::env::temp_dir().join("bwave_scope_refuse.fst");
    let out = Command::new(exe_path())
        .args(&[
            "build",
            vcd.to_str().unwrap(),
            "-o",
            fst.to_str().unwrap(),
            "--scope",
            "no.such.scope",
        ])
        .output()
        .expect("build failed to execute");
    let stderr = String::from_utf8_lossy(&out.stderr).to_string();
    let _ = std::fs::remove_file(&fst);
    assert_eq!(
        out.status.code(),
        Some(2),
        "all-out --scope build must exit 2: {}",
        stderr
    );
    assert!(
        stderr.contains("--scope 'no.such.scope' matches none"),
        "error must name the scope: {}",
        stderr
    );
}

#[test]
fn empty_store_query_exits_2() {
    let fst = build_empty_store("query");
    let fp = fst.to_string_lossy().to_string();
    let (_stdout, stderr, code) = run(&["signal", &fp]);
    let _ = std::fs::remove_file(&fst);
    assert_eq!(
        code, 2,
        "zero-signal store must be a hard error: {}",
        stderr
    );
    assert!(
        stderr.contains("ERROR: waveform store has no signals"),
        "error must name the empty store, got: {}",
        stderr
    );
    assert!(
        stderr.contains("header-only"),
        "error should explain the header-only-trace cause: {}",
        stderr
    );
}

#[test]
fn empty_store_list_stays_exit_0_but_loud() {
    let fst = build_empty_store("list");
    let fp = fst.to_string_lossy().to_string();
    let (_stdout, stderr, code) = run(&["list", &fp]);
    let _ = std::fs::remove_file(&fst);
    assert_eq!(
        code, 0,
        "list must still answer on an empty store: {}",
        stderr
    );
    assert!(
        stderr.contains("ERROR: waveform store has no signals"),
        "the '# 0 signals' shrug must be a loud ERROR line, got: {}",
        stderr
    );
}

#[test]
fn total_miss_query_exits_2() {
    let bwave = build_bwave("test_wide_signals.vcd", "total_miss");
    let bp = bwave.to_string_lossy().to_string();
    let (_stdout, stderr, code) = run(&[
        "signal",
        &bp,
        "-s",
        "no_such_a",
        "-s",
        "no_such_b",
        "--async",
    ]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 2, "all-patterns-miss must exit 2: {}", stderr);
    assert!(
        stderr.contains("ERROR: no signals match pattern(s) 'no_such_a', 'no_such_b'"),
        "error must name every missed pattern, got: {}",
        stderr
    );
    assert!(
        stderr.contains("signals in store"),
        "error should say how many signals the store does have: {}",
        stderr
    );
}

#[test]
fn total_miss_stats_json_keeps_envelope() {
    // JSON consumers (booley's coverage_analyst) still get a parseable empty
    // envelope on stdout; the exit code carries the failure.
    let bwave = build_bwave("test_wide_signals.vcd", "total_miss_json");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run(&["stats", &bp, "-s", "no_such_signal", "--format", "json"]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 2, "stderr: {}", stderr);
    assert!(
        stdout.contains("\"command\": \"stats\""),
        "stdout: {}",
        stdout
    );
    assert!(
        stdout.contains("no signals match"),
        "warning must ride in the envelope: {}",
        stdout
    );
    assert!(
        stderr.contains("ERROR: no signals match"),
        "stderr: {}",
        stderr
    );
}

#[test]
fn empty_store_list_json_is_loud_on_stderr_too() {
    // The JSON branch used to return before the stderr ERROR line, so a
    // consumer scanning stderr saw a clean run and had to parse warnings[].
    // Both channels must speak now.
    let fst = build_empty_store("list_json");
    let fp = fst.to_string_lossy().to_string();
    let (stdout, stderr, code) = run(&["list", &fp, "--format", "json"]);
    let _ = std::fs::remove_file(&fst);
    assert_eq!(code, 0, "list stays the discovery tool: {}", stderr);
    assert!(
        stdout.contains("has no signals"),
        "warning must ride the envelope: {}",
        stdout
    );
    assert!(
        stderr.contains("ERROR: waveform store has no signals"),
        "the ERROR line must reach stderr in JSON mode too: {}",
        stderr
    );
}

#[test]
fn wave_with_virtual_only_match_is_not_a_total_miss() {
    // wave/trace used to exit 2 on `matched.is_empty()` alone, even when a
    // --virtual def resolved and would have rendered rows — while stats/find
    // counted virtuals before exiting. Same query, same store, same exit now.
    let bwave = build_bwave("test_wide_signals.vcd", "virt_only_wave");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run(&[
        "wave",
        &bp,
        "-s",
        "no_such_signal",
        "--virtual",
        "virt = *byte8* > 'd0",
    ]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(
        code, 0,
        "virtual-only match must not be a total miss: {}",
        stderr
    );
    assert!(
        stdout.contains("virt"),
        "the virtual row must render: {}",
        stdout
    );
}

// Partial miss (one of two patterns matches) keeps exit 0 with a per-pattern
// warning — covered above by `unmatched_pattern_is_reported`.
