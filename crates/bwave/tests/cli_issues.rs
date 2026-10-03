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

fn live_usage(help: &str) -> &str {
    help.rsplit_once("\nUsage:")
        .map(|(_, usage)| usage)
        .unwrap_or(help)
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

#[test]
fn signal_and_diff_reject_virtual() {
    for subcommand in ["signal", "diff"] {
        let (help, stderr, code) = run(&[subcommand, "--help"]);
        assert_eq!(code, 0, "{subcommand} --help failed: {stderr}");
        let usage = live_usage(&help);
        assert!(
            !usage.contains("--virtual"),
            "{subcommand} must not advertise --virtual:\n{help}"
        );

        let args: Vec<&str> = if subcommand == "signal" {
            vec![subcommand, "missing.fst", "--virtual", "v = *a"]
        } else {
            vec![subcommand, "missing.fst", "1", "2", "--virtual", "v = *a"]
        };
        let (stdout, stderr, code) = run(&args);
        assert_eq!(code, 2, "{subcommand} must reject --virtual: {stderr}");
        assert!(stdout.is_empty(), "parser rejection wrote stdout: {stdout}");
        assert!(
            stderr.contains("unexpected argument '--virtual'"),
            "{subcommand} rejection did not come from clap: {stderr}"
        );
    }
}

struct MarkerCommandCase {
    name: &'static str,
    help: &'static [&'static str],
    rejection: Option<&'static [&'static str]>,
}

const MARKER_COMMAND_CASES: &[MarkerCommandCase] = &[
    MarkerCommandCase {
        name: "build",
        help: &["build", "--help"],
        rejection: Some(&[
            "build",
            "missing.vcd",
            "-o",
            "unused.fst",
            "--marker",
            "m",
            "1",
        ]),
    },
    MarkerCommandCase {
        name: "list",
        help: &["list", "--help"],
        rejection: Some(&["list", "missing.fst", "--marker", "m", "1"]),
    },
    MarkerCommandCase {
        name: "signal",
        help: &["signal", "--help"],
        rejection: Some(&["signal", "missing.fst", "--marker", "m", "1"]),
    },
    MarkerCommandCase {
        name: "wave",
        help: &["wave", "--help"],
        rejection: None,
    },
    MarkerCommandCase {
        name: "value",
        help: &["value", "--help"],
        rejection: Some(&["value", "missing.fst", "--at", "1", "--marker", "m", "1"]),
    },
    MarkerCommandCase {
        name: "find",
        help: &["find", "--help"],
        rejection: Some(&["find", "missing.fst", "sig", "1", "--marker", "m", "1"]),
    },
    MarkerCommandCase {
        name: "sample",
        help: &["sample", "--help"],
        rejection: Some(&["sample", "missing.fst", "sig", "1", "--marker", "m", "1"]),
    },
    MarkerCommandCase {
        name: "diff",
        help: &["diff", "--help"],
        rejection: Some(&["diff", "missing.fst", "1", "2", "--marker", "m", "1"]),
    },
    MarkerCommandCase {
        name: "distance",
        help: &["distance", "--help"],
        rejection: Some(&["distance", "missing.fst", "sig", "1", "--marker", "m", "1"]),
    },
    MarkerCommandCase {
        name: "stats",
        help: &["stats", "--help"],
        rejection: Some(&["stats", "missing.fst", "--marker", "m", "1"]),
    },
    MarkerCommandCase {
        name: "stuck",
        help: &["stuck", "--help"],
        rejection: Some(&["stuck", "missing.fst", "--marker", "m", "1"]),
    },
    MarkerCommandCase {
        name: "schema",
        help: &["schema", "--help"],
        rejection: Some(&["schema", "--marker", "m", "1"]),
    },
    MarkerCommandCase {
        name: "docs",
        help: &["docs", "topics", "--help"],
        rejection: Some(&["docs", "topics", "--marker", "m", "1"]),
    },
    MarkerCommandCase {
        name: "skill",
        help: &["skill", "--help"],
        rejection: Some(&["skill", "--marker", "m", "1"]),
    },
];

fn assert_marker_surface(case: &MarkerCommandCase) {
    let (help, stderr, code) = run(case.help);
    assert_eq!(code, 0, "{} --help failed: {stderr}", case.name);
    assert_eq!(
        live_usage(&help).contains("--marker"),
        case.rejection.is_none(),
        "unexpected --marker help surface for {}:\n{help}",
        case.name
    );
    if let Some(args) = case.rejection {
        let (stdout, stderr, code) = run(args);
        assert_eq!(code, 2, "{} accepted --marker: {stderr}", case.name);
        assert!(
            stdout.is_empty(),
            "{} parser rejection wrote stdout: {stdout}",
            case.name
        );
        assert!(
            stderr.contains("unexpected argument '--marker'"),
            "{} rejection did not come from clap: {stderr}",
            case.name
        );
    }
}

#[test]
fn marker_option_is_wave_only() {
    for case in MARKER_COMMAND_CASES {
        assert_marker_surface(case);
    }
}

#[test]
fn wave_renders_sync_marker_and_rejects_malformed_time() {
    let bwave = build_bwave("test_basic.vcd", "marker_sync");
    let store_path = bwave.to_string_lossy().to_string();

    let (stdout, stderr, code) = run(&[
        "wave",
        &store_path,
        "-s",
        "data",
        "-t",
        "4:8",
        "--with-reset",
        "--marker",
        "checkpoint",
        "6c",
    ]);
    assert_eq!(code, 0, "sync marker wave failed: {stderr}");
    assert!(
        stdout.contains("checkpoint"),
        "marker label is absent:\n{stdout}"
    );

    let (stdout, stderr, code) = run(&[
        "wave",
        &store_path,
        "-s",
        "data",
        "-t",
        "4:8",
        "--marker",
        "checkpoint",
        "nope",
    ]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 2, "malformed marker time was accepted: {stderr}");
    assert!(stdout.is_empty(), "malformed marker wrote stdout: {stdout}");
    assert!(
        stderr.contains("invalid time token"),
        "wrong error: {stderr}"
    );

    let (stdout, stderr, code) = run(&[
        "wave",
        &store_path,
        "-s",
        "data",
        "--marker",
        "before_zero",
        "-1c",
    ]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 2, "negative marker time was accepted: {stderr}");
    assert!(stdout.is_empty(), "negative marker wrote stdout: {stdout}");
    assert!(
        stderr.contains("unexpected argument '-1'"),
        "negative marker did not fail during clap parsing: {stderr}"
    );
}

#[test]
fn wave_marker_collisions_are_deterministic_and_aligned() {
    let bwave = build_bwave("test_basic.vcd", "marker_collisions");
    let store_path = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run(&[
        "wave",
        &store_path,
        "-s",
        "data",
        "-t",
        "4:8",
        "--with-reset",
        "--marker",
        "checkpoint",
        "5c",
        "--marker",
        "checkpoint",
        "6c",
        "--marker",
        "peer",
        "6c",
    ]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0, "collision wave failed: {stderr}");
    assert_eq!(stdout.matches("checkpoint").count(), 1, "{stdout}");
    let marker_line = stdout
        .lines()
        .find(|line| line.contains("checkpoint,peer"))
        .unwrap();
    let cycle_line = stdout
        .lines()
        .find(|line| line.trim_start().starts_with("cycle"))
        .unwrap();
    let label_end = marker_line.find("checkpoint,peer").unwrap() + "checkpoint,peer".len();
    assert_eq!(
        cycle_line.as_bytes()[label_end - 1],
        b'6',
        "marker is misaligned:\n{stdout}"
    );
}

#[test]
fn async_marker_adds_and_preserves_a_quiet_tick_column() {
    let bwave = build_bwave("test_ps_timescale.vcd", "marker_async");
    let store_path = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run(&[
        "wave",
        &store_path,
        "-s",
        "counter",
        "-t",
        "0t:40000t",
        "--async",
        "--rle",
        "--marker",
        "tick",
        "22500t",
        "--marker",
        "physical",
        "22500ps",
    ]);
    let _ = std::fs::remove_file(&bwave);

    assert_eq!(code, 0, "async marker wave failed: {stderr}");
    assert!(
        stdout.contains("tick,physical"),
        "equivalent tick and picosecond markers did not share a column:\n{stdout}"
    );
}

#[test]
fn value_emits_a_selected_virtual_only_snapshot() {
    let bwave = build_bwave("test_basic.vcd", "value_virtual_only");
    let store_path = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run(&[
        "value",
        &store_path,
        "--at",
        "9",
        "--with-reset",
        "-s",
        "hi",
        "--virtual",
        "helper = *data > 'd5",
        "--virtual",
        "hi = helper & *rstn",
    ]);
    let _ = std::fs::remove_file(&bwave);

    assert_eq!(code, 0, "virtual-only value failed: {stderr}");
    assert!(stdout.contains("hi"), "virtual row is absent: {stdout}");
    assert!(stdout.contains("= 1"), "virtual value is wrong: {stdout}");
    assert!(
        !stdout.contains("helper"),
        "unselected helper leaked into value output: {stdout}"
    );
    assert!(
        !stderr.contains("no signals match"),
        "selected virtual was reported missing: {stderr}"
    );
}

#[test]
fn sample_accepts_a_virtual_trigger() {
    let bwave = build_bwave("test_basic.vcd", "sample_virtual_trigger");
    let store_path = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run(&[
        "sample",
        &store_path,
        "hi",
        "rising",
        "--with-reset",
        "-s",
        "data",
        "--virtual",
        "hi = *data > 'd5",
    ]);
    let _ = std::fs::remove_file(&bwave);

    assert_eq!(code, 0, "virtual sample trigger failed: {stderr}");
    assert!(stdout.contains("data"), "capture row is absent: {stdout}");
    assert!(
        stderr.contains("# sample: 1 trigger signal(s)"),
        "virtual trigger was not counted: {stderr}"
    );
    assert!(
        stderr.contains("# 1 trigger events"),
        "unexpected virtual trigger event count: {stderr}"
    );
}

#[test]
fn sample_emits_a_selected_virtual_capture_row() {
    let bwave = build_bwave("test_basic.vcd", "sample_virtual_capture");
    let store_path = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run(&[
        "sample",
        &store_path,
        "data",
        "change",
        "--with-reset",
        "-s",
        "hi",
        "--virtual",
        "helper = *data > 'd5",
        "--virtual",
        "hi = helper & *rstn",
    ]);
    let _ = std::fs::remove_file(&bwave);

    assert_eq!(code, 0, "virtual sample capture failed: {stderr}");
    assert!(
        stdout.contains("hi 0"),
        "false virtual rows are absent: {stdout}"
    );
    assert!(
        stdout.contains("hi 1"),
        "true virtual rows are absent: {stdout}"
    );
    assert!(
        !stdout.contains("helper"),
        "unselected helper leaked into sample output: {stdout}"
    );
    assert!(
        !stderr.contains("filter dropped"),
        "selected virtual was reported missing: {stderr}"
    );
}

#[test]
fn value_json_orders_stored_then_composed_virtual_rows_in_async_mode() {
    let bwave = build_bwave("test_basic.vcd", "value_virtual_json_async");
    let store_path = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run(&[
        "value",
        &store_path,
        "--at",
        "80ns",
        "--async",
        "--format",
        "json",
        "-s",
        "data",
        "-s",
        "hi",
        "-s",
        "low",
        "--virtual",
        "hi = *data > 'd5",
        "--virtual",
        "low = ~hi",
    ]);
    let _ = std::fs::remove_file(&bwave);

    assert_eq!(code, 0, "async JSON value failed: {stderr}");
    assert!(
        !stderr.contains("filter dropped"),
        "virtual selection produced a false warning: {stderr}"
    );
    let envelope: serde_json::Value = serde_json::from_str(&stdout).unwrap();
    let rows = envelope["data"]["signals"].as_array().unwrap();
    let names: Vec<&str> = rows
        .iter()
        .map(|row| row["name"].as_str().unwrap())
        .collect();
    assert_eq!(names, vec!["data[7:0]", "hi", "low"]);
    assert_eq!(rows[1]["value"], "1");
    assert_eq!(rows[2]["value"], "0");
}

#[test]
fn async_level_sample_uses_selected_virtual_row_change_ticks() {
    let bwave = build_bwave("test_basic.vcd", "sample_virtual_async_level");
    let store_path = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run(&[
        "sample",
        &store_path,
        "rstn",
        "1",
        "--async",
        "--with-reset",
        "-s",
        "low",
        "--virtual",
        "hi = *data > 'd5",
        "--virtual",
        "low = ~hi",
    ]);
    let _ = std::fs::remove_file(&bwave);

    assert_eq!(code, 0, "async virtual sample failed: {stderr}");
    assert!(
        stdout.contains("70 low 0"),
        "virtual change was not sampled: {stdout}"
    );
    assert_eq!(stdout.lines().collect::<Vec<_>>(), vec!["70 low 0"]);
    assert!(
        stderr.contains("# 1 trigger events"),
        "async level callback schedule is wrong: {stderr}"
    );
}

#[test]
fn every_virtual_capable_command_rejects_a_stored_signal_name_collision() {
    let bwave = build_bwave("test_basic.vcd", "sample_virtual_collision");
    let store_path = bwave.to_string_lossy().to_string();
    let cases: Vec<Vec<&str>> = vec![
        vec![
            "wave",
            &store_path,
            "-s",
            "tb.rstn",
            "--virtual",
            "tb.rstn = *rstn",
        ],
        vec![
            "find",
            &store_path,
            "tb.rstn",
            "'h1",
            "--virtual",
            "tb.rstn = *rstn",
        ],
        vec![
            "sample",
            &store_path,
            "tb.rstn",
            "rising",
            "-s",
            "data",
            "--virtual",
            "tb.rstn = *rstn",
        ],
        vec![
            "distance",
            &store_path,
            "tb.rstn",
            "rising",
            "--virtual",
            "tb.rstn = *rstn",
        ],
        vec![
            "value",
            &store_path,
            "--at",
            "1",
            "-s",
            "tb.rstn",
            "--virtual",
            "tb.rstn = *rstn",
        ],
    ];

    for args in cases {
        let subcommand = args[0];
        let (stdout, stderr, code) = run(&args);
        assert_eq!(code, 2, "{subcommand} accepted a collision: {stderr}");
        assert!(
            stdout.is_empty(),
            "{subcommand} emitted output before rejecting the collision: {stdout}"
        );
        assert!(
            stderr.contains("virtual signal name 'tb.rstn' conflicts with stored signal 'tb.rstn'"),
            "{subcommand} collision diagnostic is unclear: {stderr}"
        );
    }

    let _ = std::fs::remove_file(&bwave);
}

#[test]
fn duplicate_virtual_signal_names_are_rejected_before_output() {
    let bwave = build_bwave("test_basic.vcd", "duplicate_virtual_name");
    let store_path = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run(&[
        "value",
        &store_path,
        "--at",
        "1",
        "-s",
        "helper",
        "--virtual",
        "helper = *rstn",
        "--virtual",
        "helper = *data > 'd0",
    ]);
    let _ = std::fs::remove_file(&bwave);

    assert_eq!(code, 2, "duplicate virtual name was accepted: {stderr}");
    assert!(stdout.is_empty(), "duplicate name wrote output: {stdout}");
    assert!(
        stderr.contains("virtual signal name 'helper' is defined more than once"),
        "duplicate-name diagnostic is unclear: {stderr}"
    );
}

#[test]
fn virtual_signal_name_collision_check_uses_full_stored_names() {
    let bwave = build_bwave("test_basic.vcd", "virtual_full_name_boundary");
    let store_path = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run(&[
        "value",
        &store_path,
        "--at",
        "1",
        "-s",
        "rstn",
        "--virtual",
        "rstn = *rstn",
    ]);
    let _ = std::fs::remove_file(&bwave);

    assert_eq!(code, 0, "suffix-only name overlap was rejected: {stderr}");
    assert!(
        stdout.lines().filter(|line| line.contains("rstn")).count() == 2,
        "stored and virtual rows were not both selected: {stdout}"
    );
}

#[test]
fn virtual_option_help_matches_the_five_command_contract() {
    for subcommand in ["wave", "find", "sample", "distance", "value"] {
        let (help, stderr, code) = run(&[subcommand, "--help"]);
        assert_eq!(code, 0, "{subcommand} --help failed: {stderr}");
        assert!(
            live_usage(&help).contains("--virtual"),
            "{subcommand} omits --virtual:\n{help}"
        );
    }
    for subcommand in ["list", "signal", "diff", "stats", "stuck", "build"] {
        let (help, stderr, code) = run(&[subcommand, "--help"]);
        assert_eq!(code, 0, "{subcommand} --help failed: {stderr}");
        assert!(
            !live_usage(&help).contains("--virtual"),
            "{subcommand} advertises --virtual:\n{help}"
        );
    }
}

#[test]
fn unsupported_commands_reject_virtual_during_argument_parsing() {
    let cases: Vec<Vec<&str>> = vec![
        vec!["list", "missing.fst", "--virtual", "v = *a"],
        vec!["signal", "missing.fst", "--virtual", "v = *a"],
        vec!["diff", "missing.fst", "1", "2", "--virtual", "v = *a"],
        vec!["stats", "missing.fst", "--virtual", "v = *a"],
        vec!["stuck", "missing.fst", "--virtual", "v = *a"],
        vec!["build", "missing.vcd", "--virtual", "v = *a"],
    ];
    for args in cases {
        let subcommand = args[0];
        let (stdout, stderr, code) = run(&args);
        assert_eq!(code, 2, "{subcommand} accepted --virtual: {stderr}");
        assert!(stdout.is_empty(), "{subcommand} wrote stdout: {stdout}");
        assert!(
            stderr.contains("unexpected argument '--virtual'"),
            "{subcommand} rejection did not come from clap: {stderr}"
        );
    }
}

#[test]
fn every_virtual_capable_command_rejects_bad_definitions_before_output() {
    let bwave = build_bwave("test_basic.vcd", "virtual_bad_matrix");
    let store_path = bwave.to_string_lossy().to_string();
    for bad_definition in ["bad = ", "bad = *missing_signal"] {
        let cases: Vec<Vec<&str>> = vec![
            vec![
                "wave",
                &store_path,
                "-t",
                "1:2",
                "--virtual",
                bad_definition,
            ],
            vec![
                "find",
                &store_path,
                "data",
                "1",
                "--virtual",
                bad_definition,
            ],
            vec![
                "sample",
                &store_path,
                "data",
                "change",
                "--virtual",
                bad_definition,
            ],
            vec![
                "distance",
                &store_path,
                "data",
                "change",
                "--virtual",
                bad_definition,
            ],
            vec![
                "value",
                &store_path,
                "--at",
                "1",
                "--virtual",
                bad_definition,
            ],
        ];
        for args in cases {
            let subcommand = args[0];
            let (stdout, stderr, code) = run(&args);
            assert_eq!(code, 2, "{subcommand} accepted a bad definition: {stderr}");
            assert!(
                stdout.is_empty(),
                "{subcommand} emitted partial result output: {stdout}"
            );
            assert!(
                stderr.contains(&format!("ERROR: --virtual '{bad_definition}':")),
                "{subcommand} omitted the definition error: {stderr}"
            );
        }
    }
    let _ = std::fs::remove_file(&bwave);
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

#[cfg(unix)]
#[test]
fn parallel_failure_cancels_a_blocked_stdin_reader() {
    let fst = std::env::temp_dir().join(format!(
        "bwave_cancel_stdin_{}_{}.fst",
        std::process::id(),
        std::thread::current().name().unwrap_or("test")
    ));
    let _ = std::fs::remove_file(&fst);
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
            "-o",
            fst.to_str().unwrap(),
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut writer = child.stdin.take().unwrap();
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
            panic!("parallel build did not cancel while its stdin writer remained open");
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
fn wave_selects_a_composed_virtual_without_rendering_its_helper() {
    let bwave = build_bwave("test_basic.vcd", "virt_only_wave");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run(&[
        "wave",
        &bp,
        "-s",
        "result",
        "--virtual",
        "helper = *data > 'd0",
        "--virtual",
        "result = helper & *rstn",
    ]);
    let _ = std::fs::remove_file(&bwave);
    assert_eq!(code, 0, "virtual-only match failed: {stderr}");
    assert!(
        stdout.lines().any(|line| line.starts_with("result")),
        "the selected virtual row must render: {stdout}"
    );
    assert!(
        stdout.lines().all(|line| !line.starts_with("helper")),
        "the unselected helper leaked into the output: {stdout}"
    );
    assert!(
        !stderr.contains("no signals match"),
        "the selected virtual was reported missing: {stderr}"
    );
}

#[test]
fn wave_rejects_a_selection_that_misses_stored_and_virtual_names() {
    let bwave = build_bwave("test_wide_signals.vcd", "virt_total_miss_wave");
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

    assert_eq!(code, 2, "total miss must fail: {stderr}");
    assert!(stdout.is_empty(), "total miss wrote output: {stdout}");
    assert!(
        stderr.contains("ERROR: no signals match pattern(s) 'no_such_signal'"),
        "total-miss diagnostic is unclear: {stderr}"
    );
}

#[test]
fn wave_orders_stored_then_selected_virtual_rows_without_duplicates() {
    let bwave = build_bwave("test_basic.vcd", "virtual_wave_order");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run(&[
        "wave",
        &bp,
        "-t",
        "1:2",
        "-s",
        "rstn",
        "-s",
        "helper",
        "-s",
        "result",
        "-s",
        "*result*",
        "--virtual",
        "helper = *data > 'd0",
        "--virtual",
        "result = helper & *rstn",
    ]);
    let _ = std::fs::remove_file(&bwave);

    assert_eq!(code, 0, "mixed stored/virtual wave failed: {stderr}");
    let row_names: Vec<&str> = stdout
        .lines()
        .skip(1)
        .filter_map(|line| line.split_whitespace().next())
        .collect();
    assert_eq!(row_names, vec!["rstn", "helper", "result"]);
}

#[test]
fn wave_default_selection_includes_stored_and_virtual_rows() {
    let bwave = build_bwave("test_basic.vcd", "virtual_wave_default_selection");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run(&[
        "wave",
        &bp,
        "-t",
        "1:2",
        "--virtual",
        "helper = *data > 'd0",
        "--virtual",
        "result = helper & *rstn",
    ]);
    let _ = std::fs::remove_file(&bwave);

    assert_eq!(code, 0, "default wave selection failed: {stderr}");
    assert!(stdout.contains("rstn"), "stored row is absent: {stdout}");
    assert!(stdout.contains("helper"), "helper row is absent: {stdout}");
    assert!(
        stdout.contains("result"),
        "composed row is absent: {stdout}"
    );
}

#[test]
fn wave_reports_only_patterns_missing_from_both_signal_namespaces() {
    let bwave = build_bwave("test_basic.vcd", "virtual_wave_partial_miss");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run(&[
        "wave",
        &bp,
        "-t",
        "1:2",
        "-s",
        "data",
        "-s",
        "result",
        "-s",
        "absent",
        "--virtual",
        "helper = *data > 'd0",
        "--virtual",
        "result = helper & *rstn",
    ]);
    let _ = std::fs::remove_file(&bwave);

    assert_eq!(code, 0, "partial-miss wave failed: {stderr}");
    assert!(stdout.contains("data"), "stored row is absent: {stdout}");
    assert!(stdout.contains("result"), "virtual row is absent: {stdout}");
    assert!(
        stderr.contains("no signals match 'absent'"),
        "missing pattern was not reported: {stderr}"
    );
    assert!(
        stderr.contains("1 of 3 -s patterns matched nothing"),
        "matched virtual pattern was falsely reported missing: {stderr}"
    );
}

#[test]
fn async_wave_columns_ignore_unselected_virtual_helper_transitions() {
    let bwave = build_bwave("test_basic.vcd", "virtual_wave_async_columns");
    let bp = bwave.to_string_lossy().to_string();
    let (stdout, stderr, code) = run(&[
        "wave",
        &bp,
        "--async",
        "--with-reset",
        "-t",
        "0ns:100ns",
        "-s",
        "result",
        "--virtual",
        "helper = *data > 'd0",
        "--virtual",
        "result = *data > 'd5",
    ]);
    let _ = std::fs::remove_file(&bwave);

    assert_eq!(code, 0, "async virtual wave failed: {stderr}");
    let headers: Vec<&str> = stdout
        .lines()
        .next()
        .expect("wave header")
        .split_whitespace()
        .collect();
    // The initial predicate value belongs to tick zero; only the actual
    // transition at 70 belongs in the selected-row event columns.
    assert_eq!(headers, vec!["time", "0", "70"]);
    let row = stdout.lines().nth(1).unwrap();
    assert_eq!(
        row.split_whitespace().collect::<Vec<_>>(),
        vec!["result", "0", "1"]
    );
    assert!(
        !stdout.contains("helper"),
        "unselected helper leaked into async output: {stdout}"
    );
}

// Partial miss (one of two patterns matches) keeps exit 0 with a per-pattern
// warning — covered above by `unmatched_pattern_is_reported`.

fn issue_1100_store(body: &str, engine: &str) -> (tempfile::TempDir, String) {
    let dir = tempfile::tempdir().unwrap();
    let vcd = dir.path().join("source.vcd");
    let fst = dir.path().join("source.fst");
    let header = "$timescale 1ns $end\n$scope module tb $end\n$var wire 1 ! scalar $end\n$var wire 4 # bus $end\n$var wire 1 $ clk $end\n$var wire 1 % rst_n $end\n$upscope $end\n$enddefinitions $end\n";
    std::fs::write(&vcd, format!("{header}{body}")).unwrap();
    let (_, err, code) = issue_1100_run(&[
        "build",
        "--engine",
        engine,
        vcd.to_str().unwrap(),
        "-o",
        fst.to_str().unwrap(),
    ]);
    assert_eq!(code, 0, "{err}");
    (dir, fst.to_str().unwrap().to_string())
}

fn issue_1100_run(args: &[&str]) -> (String, String, i32) {
    let mut child = Command::new(exe_path())
        .args(args)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(30);
    loop {
        if child.try_wait().unwrap().is_some() {
            break;
        }
        if std::time::Instant::now() > deadline {
            child.kill().unwrap();
            panic!("bwave timeout: {args:?}");
        }
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
    let output = child.wait_with_output().unwrap();
    (
        String::from_utf8(output.stdout).unwrap(),
        String::from_utf8(output.stderr).unwrap(),
        output.status.code().unwrap_or(-1),
    )
}

fn issue_1100_query(
    store: &str,
    command: &str,
    signal: &str,
    trigger: &str,
    extra: &[&str],
) -> String {
    let mut args = vec![command, store, signal, trigger];
    args.extend_from_slice(extra);
    let (out, err, code) = issue_1100_run(&args);
    assert_eq!(code, 0, "{args:?}: {err}");
    out.lines()
        .filter(|line| !line.starts_with('#'))
        .collect::<Vec<_>>()
        .join("\n")
}

const ISSUE_1100_TIMELINE: &str = "#0\n0!\nb0000 #\n0$\n1%\n#5\n0!\nb0000 #\n#10\n1!\nb0001 #\n#15\n1!\nb0001 #\n#20\n0!\nb0011 #\n#30\n1!\nb0010 #\n#40\n0!\nb0000 #\n";

#[test]
fn issue_1100_scalar_change_count() {
    let (_dir, store) = issue_1100_store(ISSUE_1100_TIMELINE, "parallel");
    assert_eq!(
        issue_1100_query(
            &store,
            "find",
            "scalar",
            "change",
            &["--async", "--with-reset", "--count"]
        ),
        "4"
    );
}

#[test]
fn issue_1100_bus_change_distance() {
    let (_dir, store) = issue_1100_store(ISSUE_1100_TIMELINE, "parallel");
    assert_eq!(
        issue_1100_query(
            &store,
            "distance",
            "bus",
            "change",
            &["--async", "--with-reset", "-t", "10t:30t"]
        ),
        "@ 10 -> @ 20  d=10\n@ 20 -> @ 30  d=10"
    );
}

#[test]
fn issue_1100_change_commands_agree_and_preserve_literals() {
    for engine in ["parallel", "serial"] {
        let (_dir, store) = issue_1100_store(ISSUE_1100_TIMELINE, engine);
        for signal in ["scalar", "bus"] {
            let values = if signal == "scalar" {
                ["1", "0", "1"]
            } else {
                ["1", "3", "2"]
            };
            let expected = [10, 20, 30]
                .iter()
                .zip(values)
                .map(|(tick, val)| format!("{tick} {signal} {val}"))
                .collect::<Vec<_>>()
                .join("\n");
            for keyword in ["change", "ChAnGe"] {
                let flags = ["--async", "--with-reset", "-t", "10t:30t"];
                assert_eq!(
                    issue_1100_query(&store, "find", signal, keyword, &flags),
                    expected
                );
                assert_eq!(
                    issue_1100_query(
                        &store,
                        "sample",
                        signal,
                        keyword,
                        &["--async", "--with-reset", "-t", "10t:30t", "-s", signal]
                    ),
                    expected
                );
                assert_eq!(
                    issue_1100_query(
                        &store,
                        "find",
                        signal,
                        keyword,
                        &["--async", "--count", "-t", "10t:30t"]
                    ),
                    "3"
                );
                assert_eq!(
                    issue_1100_query(
                        &store,
                        "distance",
                        signal,
                        keyword,
                        &["--async", "-t", "10t:30t", "--to", signal, keyword]
                    ),
                    "@ 10 -> @ 20  d=10\n@ 20 -> @ 30  d=10"
                );
            }
        }
        assert_eq!(
            issue_1100_query(&store, "find", "scalar", "RISING", &["--async", "--count"]),
            "2"
        );
        assert_eq!(
            issue_1100_query(&store, "find", "scalar", "FaLlInG", &["--async", "--count"]),
            "2"
        );
        for literal in ["'d3", "'h3", "'b0011"] {
            assert_eq!(
                issue_1100_query(&store, "find", "bus", literal, &["--async"]),
                "20 bus 3"
            );
        }
        for command in ["find", "sample"] {
            let extra = if command == "sample" {
                vec!["--async", "--count", "-s", "bus"]
            } else {
                vec!["--async", "--count"]
            };
            assert_eq!(
                issue_1100_query(&store, command, "bus", "rising", &extra),
                "0"
            );
        }
        let (_, error, _) = issue_1100_run(&["distance", &store, "bus", "rising", "--async"]);
        assert!(error.contains("only fires on 1-bit signals"));
    }
}

#[test]
fn issue_1100_raw_pulses_unknowns_virtuals_and_fallback() {
    let pulse = "#0\n0!\nb0000 #\n0$\n1%\n#5\n1$\n#7\n1!\nb0001 #\n#9\n0!\nb0000 #\n#10\n0$\n#15\n1$\n#17\n1!\nb0001 #\n#20\n0$\n#25\n1$\n#29\n0!\nb0000 #\n";
    let (_dir, store) = issue_1100_store(pulse, "parallel");
    for signal in ["scalar", "bus"] {
        assert_eq!(
            issue_1100_query(
                &store,
                "find",
                signal,
                "change",
                &["--with-reset", "-t", "1:2"]
            ),
            format!("cycle 1 {signal} 1\ncycle 1 {signal} 0\ncycle 2 {signal} 1")
        );
        assert_eq!(
            issue_1100_query(
                &store,
                "sample",
                signal,
                "change",
                &["--with-reset", "-t", "1:2", "-s", signal]
            ),
            format!("1 {signal} 1\n1 {signal} 0\n2 {signal} 1")
        );
        assert_eq!(
            issue_1100_query(
                &store,
                "distance",
                signal,
                "change",
                &["--with-reset", "-t", "1:2"]
            ),
            "@ 1 -> @ 1  d=0\n@ 1 -> @ 2  d=0"
        );
        assert_eq!(
            issue_1100_query(
                &store,
                "distance",
                signal,
                "change",
                &["--async", "-t", "7t:17t"]
            ),
            "@ 7 -> @ 9  d=2\n@ 9 -> @ 17  d=8"
        );
    }
    for command in ["find", "sample", "distance"] {
        let mut args = vec!["--async", "--virtual", "v=scalar=='b1", "-t", "7t:17t"];
        if command == "sample" {
            args.extend(["-s", "v"]);
        }
        let out = issue_1100_query(&store, command, "v", "change", &args);
        if command == "distance" {
            assert_eq!(out, "@ 7 -> @ 9  d=2\n@ 9 -> @ 17  d=8");
        } else {
            assert_eq!(out, "7 v 1\n9 v 0\n17 v 1");
        }
    }
    for (flag, expected) in [("--first", "7 v 1"), ("--last", "17 v 1"), ("--count", "3")] {
        assert_eq!(
            issue_1100_query(
                &store,
                "find",
                "v",
                "change",
                &[
                    "--async",
                    "--virtual",
                    "v=scalar=='b1",
                    "-t",
                    "7t:17t",
                    flag
                ]
            ),
            expected
        );
    }
    assert_eq!(
        issue_1100_query(
            &store,
            "find",
            "v",
            "change",
            &["--virtual", "v=scalar=='b1", "-t", "1:2"]
        ),
        "cycle 1 v 1\ncycle 1 v 0\ncycle 2 v 1"
    );
    for clock in ["", "#5\n1$\n"] {
        let timeline =
            format!("#0\nx!\n0$\n0%\n{clock}#10\n0!\n#15\n1%\n#20\nx!\n#30\nz!\n#40\n1!\n");
        let (_dir, store) = issue_1100_store(&timeline, "parallel");
        assert_eq!(
            issue_1100_query(&store, "find", "scalar", "change", &["-t", "10:30"]),
            "20 scalar x\n30 scalar z"
        );
        assert_eq!(
            issue_1100_query(
                &store,
                "sample",
                "scalar",
                "change",
                &["-t", "10:30", "-s", "scalar"]
            ),
            "20 scalar x\n30 scalar z"
        );
        assert_eq!(
            issue_1100_query(&store, "distance", "scalar", "change", &["-t", "10:30"]),
            "@ 20 -> @ 30  d=10"
        );
        assert_eq!(
            issue_1100_query(&store, "find", "scalar", "change", &["--async", "--count"]),
            "4"
        );
    }
}

#[test]
fn issue_1100_reset_cutoff_reassert_unknown_and_first_last_limit() {
    for reset in ["1%", "x%", "z%"] {
        let body = format!("#0\n0!\nb0000 #\n0$\n0%\n#5\n1$\n#7\n1!\nb0001 #\n#10\n0$\n{reset}\n1!\n#15\n1$\n#17\n0!\nb0000 #\n#20\n0$\n0%\n#25\n1$\n#27\n1!\nb0001 #\n");
        let (_dir, store) = issue_1100_store(&body, "parallel");
        for (flag, expected) in [
            ("--first", "cycle 1 v 0"),
            ("--last", "cycle 2 v 1"),
            ("--count", "2"),
        ] {
            assert_eq!(
                issue_1100_query(
                    &store,
                    "find",
                    "v",
                    "change",
                    &["--virtual", "v=scalar=='b1", "-t", "1:2", flag]
                ),
                expected
            );
        }
        for signal in ["scalar", "bus"] {
            assert_eq!(
                issue_1100_query(&store, "find", signal, "change", &[]),
                format!("cycle 1 {signal} 0\ncycle 2 {signal} 1")
            );
            assert_eq!(
                issue_1100_query(&store, "sample", signal, "change", &["-s", signal]),
                format!("1 {signal} 0\n2 {signal} 1")
            );
            assert_eq!(
                issue_1100_query(&store, "distance", signal, "change", &[]),
                "@ 1 -> @ 2  d=1"
            );
            assert_eq!(
                issue_1100_query(
                    &store,
                    "find",
                    signal,
                    "change",
                    &["--with-reset", "--count"]
                ),
                "3"
            );
        }
    }
    let body = "#0\n0!\nb0000 #\n0$\n1%\n#10\nb0001 #\n#20\n1!\n#30\n0!\nb0011 #\n";
    let (_dir, store) = issue_1100_store(body, "parallel");
    assert_eq!(
        issue_1100_query(&store, "find", "*", "change", &["--async", "--first"]),
        "20 scalar 1"
    );
    assert_eq!(
        issue_1100_query(&store, "find", "*", "change", &["--async", "--last"]),
        "30 bus 3"
    );
    assert_eq!(
        issue_1100_query(&store, "find", "*", "change", &["--async", "--limit", "1"]),
        "20 scalar 1"
    );
    assert_eq!(
        issue_1100_query(
            &store,
            "find",
            "*",
            "change",
            &["--async", "--limit", "1", "--count"]
        ),
        "4"
    );
    assert_eq!(
        issue_1100_query(
            &store,
            "sample",
            "scalar",
            "change",
            &["--async", "-s", "scalar", "-s", "bus", "--limit", "1"]
        ),
        "20 scalar 1"
    );
    assert_eq!(
        issue_1100_query(
            &store,
            "sample",
            "scalar",
            "change",
            &["--async", "-s", "scalar", "-s", "bus", "--limit", "1", "--count"]
        ),
        "2"
    );
}

#[test]
fn issue_1100_mixed_distance_preserves_other_mode_reset_origin() {
    let body = "#0\n0!\nb0000 #\n0$\n0%\n#7\n1!\n#10\n1%\n#17\nb0001 #\n#20\n0!\n";
    let (_dir, store) = issue_1100_store(body, "parallel");
    assert_eq!(
        issue_1100_query(
            &store,
            "distance",
            "scalar",
            "rising",
            &["--to", "bus", "change"]
        ),
        "@ 7 -> @ 17  d=10"
    );
    let body =
        "#0\n0!\nb0000 #\n0$\n0%\n#7\nb0001 #\n#9\n1!\n#10\n1%\n#12\n0!\n#17\nb0011 #\n#20\n1!\n";
    let (_dir, store) = issue_1100_store(body, "parallel");
    assert_eq!(
        issue_1100_query(
            &store,
            "distance",
            "bus",
            "change",
            &["--to", "scalar", "rising"]
        ),
        "@ 17 -> @ 20  d=3"
    );
    let body = "#0\n0!\nb0000 #\n0$\n0%\n#5\n1$\n#7\n1!\n#9\nb0001 #\n#10\n0$\n1%\n#12\n0!\n#15\n1$\n#17\n1!\n#19\nb0011 #\n#20\n0$\n#25\n1$\n#27\n0!\n";
    let (_dir, store) = issue_1100_store(body, "parallel");
    assert_eq!(
        issue_1100_query(
            &store,
            "distance",
            "scalar",
            "rising",
            &["--to", "bus", "change"]
        ),
        "@ 1 -> @ 1  d=0"
    );
    assert_eq!(
        issue_1100_query(
            &store,
            "distance",
            "bus",
            "change",
            &["--to", "scalar", "falling"]
        ),
        "@ 1 -> @ 2  d=0"
    );
}

#[test]
fn issue_1100_late_first_write_observes_fst_start_frame() {
    for engine in ["serial", "parallel"] {
        let (_dir, store) =
            issue_1100_store("#0\n0$\n1%\n#10\n1!\nb0001 #\n#20\n0!\nb0010 #\n", engine);
        let (value, error, code) =
            issue_1100_run(&["value", &store, "--async", "--at", "0t", "-s", "scalar"]);
        assert_eq!(code, 0, "{error}");
        assert!(
            value
                .lines()
                .any(|line| line.split_whitespace().collect::<Vec<_>>() == ["scalar", "=", "x"]),
            "{value}"
        );
        for signal in ["scalar", "bus"] {
            assert_eq!(
                issue_1100_query(&store, "find", signal, "change", &["--async", "--count"]),
                "2"
            );
            assert_eq!(
                issue_1100_query(
                    &store,
                    "sample",
                    signal,
                    "change",
                    &["--async", "--count", "-s", signal]
                ),
                "2"
            );
            assert_eq!(
                issue_1100_query(&store, "distance", signal, "change", &["--async"]),
                "@ 10 -> @ 20  d=10"
            );
        }
    }
}

fn issue_1108_store(label: &str) -> (PathBuf, PathBuf) {
    let dir = std::env::temp_dir().join(format!("bwave-1108-{}-{label}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let vcd = dir.join("input.vcd");
    let fst = dir.join("input.fst");
    std::fs::write(&vcd, "$timescale 1ps $end\n$scope module tb $end\n$var wire 4 ! a $end\n$var wire 4 ! alias $end\n$var wire 4 \" b $end\n$var wire 4 # c $end\n$var wire 4 $ constant $end\n$upscope $end\n$enddefinitions $end\n#0\nb0000 !\nb0000 \"\nb0000 #\nb1010 $\n#1000\nb0001 !\nb0001 \"\nb0001 #\n#3000\nb0000 !\n#4000\nb0001 !\n#5000\nb0010 \"\nb0010 #\n#9000\nb0000 !\n#10000\nb0001 !\n#20000\nb0000 !\n#25000\nb0001 !\n#100000\n").unwrap();
    let (_, err, code) = run(&["build", vcd.to_str().unwrap(), "-o", fst.to_str().unwrap()]);
    assert_eq!(code, 0, "{err}");
    (dir, fst)
}

#[test]
fn issue_1108_unsupported_json_before_io() {
    for (command, positionals) in [
        ("signal", vec![]),
        ("wave", vec![]),
        ("sample", vec!["a", "change"]),
        ("diff", vec!["1", "2"]),
        ("distance", vec!["a", "change"]),
        ("stuck", vec![]),
    ] {
        let mut args = vec![command, "/nonexistent/1108.fst"];
        args.extend(positionals);
        args.extend(["--format", "json"]);
        let (out, err, code) = run(&args);
        assert_eq!(code, 2, "{command}: {err}");
        assert!(out.is_empty());
        assert!(
            err.contains(&format!(
                "JSON output is not implemented for {command}; use find/value/stats/list"
            )),
            "{err}"
        );
    }
    let (_, err, code) = run(&[
        "list",
        "/nonexistent/1108.fst",
        "--tree",
        "--format",
        "json",
    ]);
    assert_eq!(code, 2, "{err}");
}

#[test]
fn issue_1108_exact_aliases_and_limits() {
    let (dir, fst) = issue_1108_store("aliases-and-limits");
    let p = fst.to_str().unwrap();
    for command in ["value", "wave", "signal", "stats", "diff", "sample"] {
        let mut args = vec![command, p];
        match command {
            "value" => args.extend(["--at", "1000t"]),
            "diff" => args.extend(["0t", "1000t"]),
            "sample" => args.extend(["a", "change"]),
            _ => {}
        }
        args.extend(["--async", "-s", "tb.a", "-s", "tb.alias", "--limit", "100"]);
        let (out, err, code) = run(&args);
        assert_eq!(code, 0, "{command}: {err}");
        for name in ["a", "alias"] {
            let found = out.lines().any(|line| {
                if command == "signal" || command == "sample" {
                    line.split_whitespace().nth(1) == Some(name)
                } else {
                    line.split_whitespace().next() == Some(name)
                }
            });
            assert!(found, "{command} omitted {name}: {out}");
        }
        assert!(!err.contains("shown once"));
    }
    for command in ["value", "stats"] {
        let mut args = vec![command, p, "--async", "--format", "json", "--limit", "2"];
        if command == "value" {
            args.extend(["--at", "1000t"]);
        }
        let (out, err, code) = run(&args);
        assert_eq!(code, 0, "{err}");
        let json: serde_json::Value = serde_json::from_str(&out).unwrap();
        assert_eq!(
            json["data"]["signals"].as_array().unwrap().len(),
            2,
            "{out}"
        );
        assert!(!json["warnings"].as_array().unwrap().is_empty(), "{out}");
    }
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn issue_1108_point_errors_stats_window_and_stuck_literal() {
    let (dir, fst) = issue_1108_store("points");
    let p = fst.to_str().unwrap();
    for point in ["-1t", "100001t"] {
        for command in ["value", "diff"] {
            let mut args = vec![command, p, "--async"];
            if command == "value" {
                args.extend(["--at", point]);
            } else {
                args.extend(["0t", point]);
            }
            let (_, err, code) = run(&args);
            assert_eq!(code, 2, "{command} {point}: {err}");
        }
    }
    let (out, err, code) = run(&["stuck", p, "'hA", "--async", "-s", "constant"]);
    assert_eq!(code, 0, "{err}");
    assert!(out.contains("stuck at A"), "{out}");
    let (_, err, code) = run(&["stuck", p, "rising", "--async"]);
    assert_eq!(code, 2, "{err}");
    let (out, err, code) = run(&[
        "stats",
        p,
        "--async",
        "-s",
        "b",
        "-t",
        "2000t:4000t",
        "--format",
        "json",
    ]);
    assert_eq!(code, 0, "{err}");
    let json: serde_json::Value = serde_json::from_str(&out).unwrap();
    assert_eq!(json["data"]["total_ticks"], 2000, "{out}");
    assert_eq!(json["data"]["signals"][0]["transitions"], 0, "{out}");
    let (_, err, code) = run(&["sample", p, "a", "change", "-s", "missing", "--async"]);
    assert_eq!(code, 2, "{err}");
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn issue_1108_distance_contract() {
    let (dir, fst) = issue_1108_store("distance");
    let p = fst.to_str().unwrap();
    let (out, err, code) = run(&[
        "distance", p, "a", "'h1", "--async", "--stats", "--limit", "1",
    ]);
    assert_eq!(code, 0, "{err}");
    assert!(out.contains("mean=8000.0  median=6000.0"), "{out}");
    let (out, err, code) = run(&["distance", p, "a", "'h1", "--async", "--limit", "2"]);
    assert_eq!(code, 0, "{err}");
    assert_eq!(
        out.lines().filter(|l| l.starts_with("@ ")).count(),
        2,
        "{out}"
    );
    assert!(err.contains("truncated"), "{err}");
    let (_, err, code) = run(&["distance", p, "a", "change", "-s", "b"]);
    assert_eq!(code, 2, "{err}");
    std::fs::remove_dir_all(dir).unwrap();
}

fn issue_1108_case(case: &str) {
    let (dir, fst) = issue_1108_store(case);
    let p = fst.to_str().unwrap();
    match case {
        "aliases" | "mixed" | "radix" => {
            let selectors = match case {
                "mixed" => vec!["tb.a", "tb.*"],
                "radix" => vec!["tb.*%d", "tb.a"],
                _ => vec!["tb.alias", "tb.a", "tb.a"],
            };
            let mut args = vec!["value", p, "--at", "1000t", "--async"];
            for selector in selectors {
                args.extend(["-s", selector]);
            }
            let (out, err, code) = run(&args);
            assert_eq!(code, 0, "{err}");
            assert_eq!(
                out.lines().filter(|l| l.starts_with("a ")).count(),
                1,
                "{out}"
            );
            if case == "aliases" {
                assert_eq!(
                    out.lines().filter(|l| l.starts_with("alias ")).count(),
                    1,
                    "{out}"
                );
                assert!(!err.contains("shown once"));
            } else {
                assert!(err.contains("shown once"), "{err}");
                if case == "radix" {
                    assert!(
                        out.lines()
                            .any(|line| line.starts_with("constant ") && line.ends_with("= 10")),
                        "{out}"
                    );
                }
            }
        }
        "glob_json" | "event_json" => {
            let args = if case == "glob_json" {
                vec![
                    "value", p, "--at", "1000t", "--async", "-s", "tb.*", "--format", "json",
                ]
            } else {
                vec!["find", p, "tb.*", "change", "--async", "--format", "json"]
            };
            let (out, err, code) = run(&args);
            assert_eq!(code, 0, "{err}");
            let json: serde_json::Value = serde_json::from_str(&out).unwrap();
            assert!(
                json["warnings"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|w| w.as_str().unwrap().contains("alias")),
                "{out}"
            );
            if case == "event_json" {
                assert!(!out.contains("shown once"));
            }
        }
        "stats_window" => {
            let (out, err, code) = run(&[
                "stats", p, "--async", "-s", "b", "-t", "2ns:4ns", "--format", "json",
            ]);
            assert_eq!(code, 0, "{err}");
            let json: serde_json::Value = serde_json::from_str(&out).unwrap();
            assert_eq!(json["data"]["total_ticks"], 2000, "{out}");
            assert_eq!(json["data"]["signals"][0]["transitions"], 0, "{out}");
            assert_eq!(
                json["data"]["signals"][0]["time_in_state_ticks"]["'h1"], 2000,
                "{out}"
            );
        }
        "diff_ps" => {
            let (out, err, code) = run(&["diff", p, "0ns", "1ns", "--async", "-s", "b"]);
            assert_eq!(code, 0, "{err}");
            let (ticks, _, tickcode) = run(&["diff", p, "0t", "1000t", "--async", "-s", "b"]);
            assert_eq!(tickcode, 0);
            assert_eq!(out, ticks);
            assert!(out.contains("@1000=1"), "{out}");
        }
        "stuck_literal" => {
            let (out, err, code) = run(&["stuck", p, "8'd10", "--async", "-s", "constant"]);
            assert_eq!(code, 0, "{err}");
            assert!(out.contains("stuck at A"), "{out}");
        }
        "sample_miss" => {
            let (_, err, code) = run(&["sample", p, "a", "change", "-s", "missing", "--async"]);
            assert_eq!(code, 2, "{err}");
        }
        "invalid_glob_find" | "invalid_glob_distance" => {
            let command = if case.ends_with("find") {
                "find"
            } else {
                "distance"
            };
            let (_, err, code) = run(&[command, p, "[oops", "change", "--async"]);
            assert_eq!(code, 2, "{err}");
            assert!(err.contains("invalid glob pattern"), "{err}");
        }
        "value_out" | "diff_out" => {
            let args = if case.starts_with("value") {
                vec!["value", p, "--at", "100001t", "--async", "--format", "json"]
            } else {
                vec!["diff", p, "0t", "100001t", "--async"]
            };
            let (out, err, code) = run(&args);
            assert_eq!(code, 2, "{err}");
            assert!(out.is_empty(), "{out}");
        }
        "zero_limit" => {
            let (_, err, code) = run(&["wave", p, "--limit", "0"]);
            assert_eq!(code, 2, "{err}");
        }
        "find_limit" | "find_boundary" => {
            let limit = if case == "find_boundary" { "4" } else { "2" };
            let (out, err, code) = run(&[
                "find", p, "a", "'h1", "--async", "--format", "json", "--limit", limit,
            ]);
            assert_eq!(code, 0, "{err}");
            let json: serde_json::Value = serde_json::from_str(&out).unwrap();
            assert_eq!(json["data"]["truncated"], case == "find_limit", "{out}");
            assert_eq!(
                !json["warnings"].as_array().unwrap().is_empty(),
                case == "find_limit",
                "{out}"
            );
            assert_eq!(
                json["data"]["count"],
                limit.parse::<u64>().unwrap(),
                "{out}"
            );
        }
        "signal_boundary" | "sample_boundary" => {
            let args = if case.starts_with("signal") {
                vec!["signal", p, "--async", "-s", "b", "--limit", "3"]
            } else {
                vec![
                    "sample", p, "b", "change", "--async", "-s", "c", "--limit", "3",
                ]
            };
            let (_, err, code) = run(&args);
            assert_eq!(code, 0, "{err}");
            assert!(!err.contains("truncated"), "{err}");
        }
        "diff_limit" => {
            let (out, err, code) = run(&["diff", p, "0t", "1000t", "--async", "--limit", "2"]);
            assert_eq!(code, 0, "{err}");
            assert_eq!(
                out.lines().filter(|l| !l.starts_with('#')).count(),
                2,
                "{out}"
            );
            assert!(err.contains("truncated"), "{err}");
        }
        "help" => {
            for command in ["signal", "wave", "sample", "diff", "distance", "stuck"] {
                for help in ["-h", "--help"] {
                    let (out, _, code) = run(&[command, help]);
                    assert_eq!(code, 0);
                    assert!(
                        out.contains("Output format: text (JSON requests exit 2)"),
                        "{out}"
                    );
                    assert!(
                        !live_usage(&out).contains("possible values: text, json"),
                        "{out}"
                    );
                }
            }
        }
        _ => panic!("unknown case {case}"),
    }
    std::fs::remove_dir_all(dir).unwrap();
}

macro_rules! issue_1108_cases {
    ($($name:ident => $case:literal),* $(,)?) => {$(#[test] fn $name(){issue_1108_case($case);})*};
}
issue_1108_cases! {
    issue_1108_alias_rows => "aliases", issue_1108_mixed_rows => "mixed", issue_1108_mixed_radix => "radix",
    issue_1108_glob_json => "glob_json", issue_1108_event_json => "event_json", issue_1108_stats_window => "stats_window",
    issue_1108_diff_ps => "diff_ps", issue_1108_stuck_literal => "stuck_literal", issue_1108_sample_miss => "sample_miss",
    issue_1108_invalid_glob_find => "invalid_glob_find", issue_1108_invalid_glob_distance => "invalid_glob_distance",
    issue_1108_value_out => "value_out", issue_1108_diff_out => "diff_out", issue_1108_zero_limit => "zero_limit",
    issue_1108_find_limit => "find_limit", issue_1108_find_boundary => "find_boundary", issue_1108_signal_boundary => "signal_boundary",
    issue_1108_sample_boundary => "sample_boundary", issue_1108_diff_limit => "diff_limit", issue_1108_help => "help",
}

#[test]
fn issue_1108_documentation_goldens() {
    let (dir, fst) = issue_1108_store("doc-goldens");
    let p = fst.to_str().unwrap();
    let cases: &[(&str, &[&str])] = &[
        ("signal", &["--async", "-s", "b", "-t", "0t:5000t"]),
        ("wave", &["--async", "-s", "b", "-t", "0t:5000t"]),
        ("value", &["--async", "-s", "b", "--at", "1000t"]),
        ("find", &["--async", "b", "'h1"]),
        ("sample", &["--async", "b", "'h1", "-s", "c"]),
        ("diff", &["--async", "0t", "5000t", "-s", "b"]),
        ("stats", &["--async", "-s", "b", "-t", "0t:5000t"]),
        ("stuck", &["--async", "-s", "constant"]),
        ("distance", &["--async", "a", "'h1"]),
    ];
    for (command, options) in cases {
        let mut args = vec![*command, p];
        args.extend_from_slice(options);
        let (out, err, code) = run(&args);
        assert_eq!(code, 0, "{err}");
        let doc = std::fs::read_to_string(
            PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join(format!("docs/public/commands/{command}.md")),
        )
        .unwrap();
        let shape = doc.split("## Output shape").nth(1).unwrap();
        let golden = shape
            .split("```\n")
            .nth(1)
            .unwrap()
            .split("```")
            .next()
            .unwrap();
        assert_eq!(out, golden, "{command}");
    }
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn issue_1108_alias_warning_budget_and_implicit_selection() {
    let (dir, fst) = issue_1108_store("many-aliases");
    let vcd = dir.join("input.vcd");
    let mut input = "$timescale 1ps $end\n$scope module tb $end\n".to_string();
    for i in 0..34 {
        input.push_str(&format!("$var wire 1 ! a{i} $end\n"));
    }
    input.push_str("$upscope $end\n$enddefinitions $end\n#0\n0!\n#1000\n1!\n#2000\n");
    std::fs::write(&vcd, input).unwrap();
    let (_, err, code) = run(&["build", vcd.to_str().unwrap(), "-o", fst.to_str().unwrap()]);
    assert_eq!(code, 0, "{err}");
    let p = fst.to_str().unwrap();
    for command in ["value", "stats", "stuck", "diff", "sample", "find"] {
        let mut args = vec![command, p, "--async"];
        match command {
            "value" => args.extend(["--at", "1000t"]),
            "diff" => args.extend(["0t", "1000t"]),
            "find" | "sample" => args.extend(["tb.a0", "change"]),
            _ => {}
        }
        let (_, err, code) = run(&args);
        assert_eq!(code, 0, "{command}: {err}");
        assert!(!err.contains("alias"), "{command}: {err}");
    }
    let (out, err, code) = run(&[
        "value", p, "--async", "--at", "1000t", "-s", "tb.*", "--format", "json",
    ]);
    assert_eq!(code, 0, "{err}");
    let json: serde_json::Value = serde_json::from_str(&out).unwrap();
    let warnings = json["warnings"].as_array().unwrap();
    assert_eq!(warnings.len(), 33, "{out}");
    assert_eq!(
        warnings[32],
        "additional 1 alias names shown once; narrow selectors for details"
    );
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn issue_1108_list_clock_override_metadata() {
    let fst = build_bwave("small_clocked.vcd", "1108-list-clock");
    let (out, err, code) = run(&[
        "list",
        fst.to_str().unwrap(),
        "--clock",
        "flag",
        "--format",
        "json",
    ]);
    assert_eq!(code, 0, "{err}");
    let json: serde_json::Value = serde_json::from_str(&out).unwrap();
    assert_eq!(json["data"]["clock"], "tb.dut.flag");
    std::fs::remove_file(fst).unwrap();
}

#[test]
fn issue_1108_stats_zero_duration_and_raw_window_consumers() {
    let (dir, fst) = issue_1108_store("windows");
    let p = fst.to_str().unwrap();
    for command in ["signal", "wave", "stats", "find", "sample", "distance"] {
        let mut base = vec![command, p, "--async"];
        match command {
            "find" | "sample" => base.extend(["b", "'h1"]),
            "distance" => base.extend(["a", "'h1"]),
            _ => {}
        }
        if command != "find" && command != "distance" {
            base.extend(["-s", "b"]);
        }
        let mut physical = base.clone();
        physical.extend(["-t", "1ns:5ns"]);
        let mut ticks = base;
        ticks.extend(["-t", "1000t:5000t"]);
        let (out, err, code) = run(&physical);
        let (other, othererr, othercode) = run(&ticks);
        assert_eq!(code, 0, "{command}: {err}");
        assert_eq!(othercode, 0, "{othererr}");
        assert_eq!(out, other, "{command}");
    }
    let (out, err, code) = run(&[
        "stats",
        p,
        "--async",
        "-s",
        "b",
        "-t",
        "1000t:1000t",
        "--format",
        "json",
    ]);
    assert_eq!(code, 0, "{err}");
    let json: serde_json::Value = serde_json::from_str(&out).unwrap();
    assert_eq!(json["data"]["total_ticks"], 0);
    assert_eq!(json["data"]["signals"][0]["transitions"], 1);
    assert_eq!(
        json["data"]["signals"][0]["time_in_state_ticks"],
        serde_json::json!({})
    );
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn issue_1108_distance_even_public_and_all_pairs_stats() {
    let (dir, fst) = issue_1108_store("even");
    let vcd = dir.join("input.vcd");
    let input = std::fs::read_to_string(&vcd)
        .unwrap()
        .replace("#100000\n", "#26000\nb0000 !\n#27000\nb0001 !\n#100000\n");
    std::fs::write(&vcd, input).unwrap();
    let (_, err, code) = run(&["build", vcd.to_str().unwrap(), "-o", fst.to_str().unwrap()]);
    assert_eq!(code, 0, "{err}");
    let p = fst.to_str().unwrap();
    let mut expected = None;
    for limit in ["1", "4", "100"] {
        let (out, err, code) = run(&[
            "distance", p, "a", "'h1", "--async", "--stats", "--limit", limit,
        ]);
        assert_eq!(code, 0, "{err}");
        assert!(
            out.contains("count=4  min=2000  max=15000  mean=6500.0  median=4500.0"),
            "{out}"
        );
        if let Some(ref prev) = expected {
            assert_eq!(&out, prev);
        }
        expected = Some(out);
        assert!(!err.contains("truncated"));
    }
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn issue_1108_stuck_limits_literals_and_unknowns() {
    let (dir, fst) = issue_1108_store("constants");
    let vcd = dir.join("input.vcd");
    std::fs::write(&vcd,"$timescale 1ns $end\n$scope module tb $end\n$var wire 8 ! aaa $end\n$var wire 8 \" bbb $end\n$var wire 8 # ccc $end\n$var wire 8 $ xxx $end\n$var wire 8 % zzz $end\n$upscope $end\n$enddefinitions $end\n#0\nb00001010 !\nb00001010 \"\nb00001010 #\nbxxxx $\nbzzzz %\n#10\n").unwrap();
    let (_, err, code) = run(&["build", vcd.to_str().unwrap(), "-o", fst.to_str().unwrap()]);
    assert_eq!(code, 0, "{err}");
    let p = fst.to_str().unwrap();
    for literal in ["'hA", "'h000a", "8'd10", "'b1010"] {
        for limit in ["2", "3"] {
            let (out, err, code) = run(&["stuck", p, literal, "--async", "--limit", limit]);
            assert_eq!(code, 0, "{err}");
            assert_eq!(
                out.lines().filter(|l| l.contains("stuck at")).count(),
                limit.parse::<usize>().unwrap(),
                "{out}"
            );
            assert_eq!(err.contains("truncated"), limit == "2", "{err}");
        }
    }
    for literal in ["x", "'hX", "z", "'hZ"] {
        let (out, err, code) = run(&["stuck", p, literal, "--async"]);
        assert_eq!(code, 0, "{err}");
        assert_eq!(
            out.lines().filter(|l| l.contains("stuck at")).count(),
            1,
            "{out}"
        );
    }
    for literal in ["rising", "change", "A", "'hQ"] {
        let (_, err, code) = run(&["stuck", p, literal, "--async"]);
        assert_eq!(code, 2, "{err}");
    }
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn issue_1108_list_value_stats_limit_boundaries() {
    let (dir, fst) = issue_1108_store("limit-boundaries");
    let p = fst.to_str().unwrap();
    for command in ["list", "value", "stats"] {
        for limit in ["3", "4"] {
            let mut args = vec![
                command, p, "--limit", limit, "--format", "json", "-s", "tb.*",
            ];
            if command == "value" {
                args.extend(["--at", "1000t", "--async"]);
            } else if command == "stats" {
                args.push("--async");
            }
            let (out, err, code) = run(&args);
            assert_eq!(code, 0, "{err}");
            let json: serde_json::Value = serde_json::from_str(&out).unwrap();
            let warnings = json["warnings"].as_array().unwrap();
            let expected_total = if command == "list" { 5 } else { 4 };
            assert_eq!(
                json["data"]["signals"].as_array().unwrap().len(),
                limit.parse::<usize>().unwrap().min(expected_total),
                "{out}"
            );
            assert_eq!(
                warnings
                    .iter()
                    .filter(|w| w.as_str().unwrap().contains("truncated"))
                    .count(),
                usize::from(limit.parse::<usize>().unwrap() < expected_total),
                "{out}"
            );
        }
    }
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn issue_1108_clockless_sync_and_negative_points() {
    let (dir, fst) = issue_1108_store("clockless");
    let p = fst.to_str().unwrap();
    let (a, err, code) = run(&["diff", p, "0", "1000", "-s", "b"]);
    assert_eq!(code, 0, "{err}");
    let (b, err, code) = run(&["diff", p, "0t", "1000t", "--async", "-s", "b"]);
    assert_eq!(code, 0, "{err}");
    assert_eq!(a, b);
    for mode in [vec![], vec!["--with-reset"]] {
        for format in ["text", "json"] {
            let mut args = vec!["value", p, "--at=-1", "--format", format];
            args.extend(mode.clone());
            let (out, err, code) = run(&args);
            assert_eq!(code, 2, "{err}");
            assert!(out.is_empty());
        }
    }
    let (out, err, code) = run(&[
        "sample", p, "a", "change", "-s", "missing", "--async", "--count", "--limit", "1",
    ]);
    assert_eq!(code, 0, "{err}");
    assert!(out.trim().parse::<u64>().unwrap() > 1, "{out}");
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn issue_1108_supplemental_documentation_goldens() {
    let (dir, fst) = issue_1108_store("extra-doc-goldens");
    let p = fst.to_str().unwrap();
    let cases: &[(&str, usize, &[&str])] = &[
        ("find", 1, &["--async", "b", "'h1", "--count"]),
        ("sample", 1, &["--async", "b", "'h1", "-s", "c", "--count"]),
        ("distance", 1, &["a", "'h1", "--to", "b", "'h2", "--async"]),
        ("distance", 2, &["a", "'h1", "--async", "--stats"]),
        (
            "wave",
            1,
            &[
                "--async", "-s", "b", "-t", "0t:5000t", "--marker", "change", "1000t",
            ],
        ),
        ("stuck", 1, &["'hA", "--async", "-s", "constant"]),
    ];
    for (command, index, options) in cases {
        let mut args = vec![*command, p];
        args.extend_from_slice(options);
        let (out, err, code) = run(&args);
        assert_eq!(code, 0, "{err}");
        let doc = std::fs::read_to_string(
            PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .join(format!("docs/public/commands/{command}.md")),
        )
        .unwrap();
        let shape = doc.split("## Output shape").nth(1).unwrap();
        let blocks: Vec<&str> = shape
            .split("```")
            .skip(1)
            .step_by(2)
            .map(|b| b.strip_prefix('\n').unwrap_or(b))
            .collect();
        let normalized: String = out
            .lines()
            .map(|line| format!("{}\n", line.trim_end()))
            .collect();
        assert_eq!(normalized, blocks[*index], "{command}");
    }
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn issue_1108_stats_clock_override_window_and_reset_policy() {
    let fst = build_bwave("small_clocked.vcd", "1108-stats-clock");
    let p = fst.to_str().unwrap();
    for (clock, expected) in [("clk", 10), ("flag", 140)] {
        let (out, err, code) = run(&[
            "stats", p, "--clock", clock, "-s", "state", "-t", "2:3", "--format", "json",
        ]);
        assert_eq!(code, 0, "{err}");
        let json: serde_json::Value = serde_json::from_str(&out).unwrap();
        assert_eq!(json["data"]["total_ticks"], expected, "{out}");
        let durations = json["data"]["signals"][0]["time_in_state_ticks"]
            .as_object()
            .unwrap()
            .values()
            .map(|v| v.as_u64().unwrap())
            .sum::<u64>();
        assert_eq!(durations, expected as u64, "{out}");
    }
    std::fs::remove_file(fst).unwrap();
}

#[test]
fn issue_1108_diff_physical_tick_equivalence_in_ps_and_ns() {
    let (dir, fst) = issue_1108_store("diff-units");
    for (scale, middle, end, lo, hi) in [
        ("1ps", 50000, 100000, "35000t", "95000t"),
        ("1ns", 50, 100, "35t", "95t"),
    ] {
        let vcd = dir.join("input.vcd");
        std::fs::write(&vcd, format!("$timescale {scale} $end\n$scope module tb $end\n$var wire 1 ! data $end\n$upscope $end\n$enddefinitions $end\n#0\n0!\n#{middle}\n1!\n#{end}\n")).unwrap();
        let p = fst.to_str().unwrap();
        let (_, err, code) = run(&["build", vcd.to_str().unwrap(), "-o", p]);
        assert_eq!(code, 0, "{err}");
        let (out, err, code) = run(&["diff", p, "35ns", "95ns", "--async", "-s", "data"]);
        assert_eq!(code, 0, "{err}");
        assert!(
            out.lines().any(|line| {
                let fields = line.split_whitespace().collect::<Vec<_>>();
                fields.len() == 3
                    && fields[0] == "data"
                    && fields[1].ends_with("=0")
                    && fields[2].ends_with("=1")
            }),
            "{out}"
        );
        let (other, err, code) = run(&["diff", p, lo, hi, "--async", "-s", "data"]);
        assert_eq!(code, 0, "{err}");
        assert_eq!(out, other);
    }
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn issue_1108_alias_command_order_and_radix_provenance() {
    let (dir, fst) = issue_1108_store("row-order");
    let vcd = dir.join("input.vcd");
    std::fs::write(&vcd,"$timescale 1ns $end\n$scope module tb $end\n$var wire 4 ! zheld $end\n$var wire 4 ! aheld $end\n$var wire 4 \" z $end\n$var wire 4 \" a $end\n$var wire 1 # event $end\n$upscope $end\n$enddefinitions $end\n#0\nb1010 !\nb0000 \"\n0#\n#10\nb1010 \"\n1#\n#20\n0#\n").unwrap();
    let (_, err, code) = run(&["build", vcd.to_str().unwrap(), "-o", fst.to_str().unwrap()]);
    assert_eq!(code, 0, "{err}");
    let p = fst.to_str().unwrap();
    for command in [
        "value", "wave", "signal", "sample", "stats", "stuck", "diff",
    ] {
        let diff = command == "diff";
        let names = if diff { ["z", "a"] } else { ["zheld", "aheld"] };
        let mut args = vec![command, p, "--async", "-s", names[1], "-s", names[0]];
        match command {
            "value" => args.extend(["--at", "10t"]),
            "diff" => args.extend(["0t", "10t"]),
            "sample" => args.extend(["event", "rising"]),
            _ => {}
        }
        let (out, err, code) = run(&args);
        assert_eq!(code, 0, "{command}: {err}");
        let rows: Vec<&str> = out
            .lines()
            .filter_map(|line| {
                let mut tokens = line.split_whitespace();
                let candidate = if command == "signal" || command == "sample" {
                    tokens.nth(1)
                } else {
                    tokens.next()
                };
                candidate.filter(|name| names.contains(name))
            })
            .collect();
        let expected = if command == "stats" || command == "stuck" {
            vec![names[1], names[0]]
        } else {
            vec![names[0], names[1]]
        };
        assert_eq!(rows, expected, "{command}: {out}");
    }
    let (out, err, code) = run(&[
        "value", p, "--async", "--at", "10t", "-s", "zheld%d", "-s", "aheld%h",
    ]);
    assert_eq!(code, 0, "{err}");
    assert!(
        out.lines()
            .any(|l| l.starts_with("zheld ") && l.ends_with("= 10")),
        "{out}"
    );
    assert!(
        out.lines()
            .any(|l| l.starts_with("aheld ") && l.ends_with("= A")),
        "{out}"
    );
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn issue_1108_find_combined_namespaces_warns_once() {
    let (dir, fst) = issue_1108_store("combined-find");
    let (out, err, code) = run(&[
        "find",
        fst.to_str().unwrap(),
        "*",
        "'h1",
        "--async",
        "--virtual",
        "v = b",
        "--limit",
        "2",
    ]);
    assert_eq!(code, 0, "{err}");
    assert_eq!(out.lines().count(), 2, "{out}");
    assert_eq!(err.matches("output truncated").count(), 1, "{err}");
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn issue_1108_exact_unsuffixed_alias_does_not_inherit_glob_radix() {
    let (dir, fst) = issue_1108_store("exact-glob-radix");
    let vcd = dir.join("input.vcd");
    let input = std::fs::read_to_string(&vcd)
        .unwrap()
        .replace("b0001 !", "b1010 !");
    std::fs::write(&vcd, input).unwrap();
    let (_, err, code) = run(&["build", vcd.to_str().unwrap(), "-o", fst.to_str().unwrap()]);
    assert_eq!(code, 0, "{err}");
    let (out, err, code) = run(&[
        "value",
        fst.to_str().unwrap(),
        "--async",
        "--at",
        "1000t",
        "-s",
        "tb.*%d",
        "-s",
        "tb.a",
    ]);
    assert_eq!(code, 0, "{err}");
    assert!(
        out.lines()
            .any(|l| l.starts_with("a ") && l.ends_with("= A")),
        "{out}"
    );
    assert!(
        out.lines()
            .any(|l| l.starts_with("constant ") && l.ends_with("= 10")),
        "{out}"
    );
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn issue_1108_vector_alias_names_are_exact_selectors() {
    let (dir, fst) = issue_1108_store("vector-aliases");
    let vcd = dir.join("input.vcd");
    let input = std::fs::read_to_string(&vcd)
        .unwrap()
        .replace("! a $end", "! a [3:0] $end")
        .replace("! alias $end", "! alias [3:0] $end");
    std::fs::write(&vcd, input).unwrap();
    let (_, err, code) = run(&["build", vcd.to_str().unwrap(), "-o", fst.to_str().unwrap()]);
    assert_eq!(code, 0, "{err}");
    let (out, err, code) = run(&[
        "value",
        fst.to_str().unwrap(),
        "--async",
        "--at",
        "1000t",
        "-s",
        "tb.a[3:0]",
        "-s",
        "tb.alias[3:0]",
    ]);
    assert_eq!(code, 0, "{err}");
    assert!(out.lines().any(|l| l.starts_with("a[3:0] ")), "{out}");
    assert!(out.lines().any(|l| l.starts_with("alias[3:0] ")), "{out}");
    assert!(!err.contains("shown once"), "{err}");
    std::fs::remove_dir_all(dir).unwrap();
}

#[test]
fn issue_1108_brace_glob_aliases_are_deduplicated() {
    let (dir, fst) = issue_1108_store("brace-glob");
    let (out, err, code) = run(&[
        "value",
        fst.to_str().unwrap(),
        "--async",
        "--at",
        "1000t",
        "-s",
        "{tb.a,tb.alias}",
    ]);
    assert_eq!(code, 0, "{err}");
    assert_eq!(
        out.lines().filter(|l| l.contains(" = ")).count(),
        1,
        "{out}"
    );
    assert!(err.contains("shown once"), "{err}");
    std::fs::remove_dir_all(dir).unwrap();
}
