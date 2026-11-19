//! CLI tests for the one reset rule and the reset report (issue #4696): the
//! `# reset:` header on sync text output and `overview`, the JSON `reset`
//! object, `--reset` validation, and the `find` no-match hint.

use std::collections::BTreeMap;
use std::process::{Command, Stdio};

use serde_json::{json, Value};

fn run(args: &[&str]) -> (String, String, i32) {
    let mut child = Command::new(env!("CARGO_BIN_EXE_bwave"))
        .args(args)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let deadline = std::time::Instant::now() + std::time::Duration::from_secs(30);
    while child.try_wait().unwrap().is_none() {
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

/// Build a store whose clock `tb.clk` is 0 at tick 0 and rises at 5, 15, 25,
/// ... through `end` (period 10). `vars` are `(name, width)`; a name starting
/// with `dut.` lives in the nested scope `tb.dut`. `events` are
/// `(tick, "<value> <name>")`.
fn store(vars: &[(&str, u32)], events: &[(u64, &str)], end: u64) -> (tempfile::TempDir, String) {
    let id = |name: &str| -> char {
        let at = vars.iter().position(|(n, _)| *n == name).unwrap();
        char::from(b'"' + at as u8)
    };
    let mut vcd = String::from("$timescale 1ns $end\n$scope module tb $end\n");
    vcd.push_str("$var wire 1 ! clk $end\n");
    for (i, (name, width)) in vars.iter().enumerate() {
        if !name.starts_with("dut.") {
            let c = char::from(b'"' + i as u8);
            vcd.push_str(&format!("$var wire {width} {c} {name} $end\n"));
        }
    }
    vcd.push_str("$scope module dut $end\n");
    for (i, (name, width)) in vars.iter().enumerate() {
        if let Some(leaf) = name.strip_prefix("dut.") {
            let c = char::from(b'"' + i as u8);
            vcd.push_str(&format!("$var wire {width} {c} {leaf} $end\n"));
        }
    }
    vcd.push_str("$upscope $end\n$upscope $end\n$enddefinitions $end\n");
    let mut changes: BTreeMap<u64, Vec<String>> = BTreeMap::new();
    for tick in (0..=end).step_by(5) {
        if tick == 0 || tick % 10 == 0 {
            changes.entry(tick).or_default().push("0!".into());
        } else {
            changes.entry(tick).or_default().push("1!".into());
        }
    }
    for (tick, event) in events {
        let (value, name) = event.split_once(' ').unwrap();
        let value = if value.starts_with('b') {
            format!("{value} ")
        } else {
            value.to_string()
        };
        changes
            .entry(*tick)
            .or_default()
            .push(format!("{value}{}", id(name)));
    }
    for (tick, lines) in changes {
        vcd.push_str(&format!("#{tick}\n{}\n", lines.join("\n")));
    }
    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("source.vcd");
    let fst = dir.path().join("source.fst");
    std::fs::write(&source, vcd).unwrap();
    let (_, err, code) = run(&[
        "build",
        source.to_str().unwrap(),
        "-o",
        fst.to_str().unwrap(),
    ]);
    assert_eq!(code, 0, "{err}");
    (dir, fst.to_str().unwrap().to_string())
}

/// Active-low `rst_n` low at 0 and high at 25: deasserts at tick 25, two
/// cycles after the first rise at 5. `flag` pulses high at 12.
fn auto_store() -> (tempfile::TempDir, String) {
    store(
        &[("rst_n", 1), ("flag", 1)],
        &[
            (0, "0 rst_n"),
            (0, "0 flag"),
            (12, "1 flag"),
            (18, "0 flag"),
            (25, "1 rst_n"),
        ],
        100,
    )
}

fn reset_lines(text: &str) -> Vec<&str> {
    text.lines().filter(|l| l.starts_with("# reset:")).collect()
}

fn json_of(args: &[&str]) -> Value {
    let (out, err, code) = run(args);
    assert_eq!(code, 0, "{args:?}: {err}");
    serde_json::from_str(&out).unwrap()
}

const AUTO_LINE: &str = "# reset: tb.rst_n (active-low by name, auto) deasserts @ 25ns; 2 cycles skipped (--with-reset to include)";

#[test]
fn reset_option_drives_store_boundary_and_queries() {
    // `a_rst` (auto pick) deasserts at 12, off the clock; `b_rst` deasserts at
    // 25, on a clock rise. `clk_aux` is declared before the selected clock and
    // rises only at 25.
    let both = store(
        &[("clk_aux", 1), ("a_rst", 1), ("b_rst", 1), ("data", 1)],
        &[
            (0, "0 clk_aux"),
            (0, "1 a_rst"),
            (0, "1 b_rst"),
            (0, "0 data"),
            (12, "0 a_rst"),
            (20, "1 data"),
            (25, "0 b_rst"),
            (25, "1 clk_aux"),
            (30, "0 clk_aux"),
        ],
        100,
    );
    let only_b = store(
        &[("clk_aux", 1), ("b_rst", 1), ("data", 1)],
        &[
            (0, "0 clk_aux"),
            (0, "1 b_rst"),
            (0, "0 data"),
            (20, "1 data"),
            (25, "0 b_rst"),
            (25, "1 clk_aux"),
            (30, "0 clk_aux"),
        ],
        100,
    );
    let transitions = |store: &str, extra: &[&str]| -> Vec<(String, u64)> {
        let mut args = vec!["stats", store, "-s", "clk*", "--format", "json"];
        args.extend_from_slice(extra);
        json_of(&args)["data"]["signals"]
            .as_array()
            .unwrap()
            .iter()
            .map(|s| {
                (
                    s["name"].as_str().unwrap().to_string(),
                    s["transitions"].as_u64().unwrap(),
                )
            })
            .collect()
    };
    let chosen = transitions(&both.1, &["--reset", "tb.b_rst"]);
    assert_eq!(chosen, transitions(&only_b.1, &[]));
    assert_eq!(chosen.iter().find(|(n, _)| n == "tb.clk_aux").unwrap().1, 2);

    let (_, err, code) = run(&["find", &both.1, "data", "'b1", "--reset", "tb.b_rst"]);
    assert_eq!(code, 0, "{err}");
    assert_eq!(
        reset_lines(&err),
        [
            "# reset: tb.b_rst (active-high by name, --reset) deasserts @ 25ns; 2 cycles skipped (--with-reset to include)"
        ]
    );
}

#[test]
fn reset_names_are_auto_detected() {
    for (name, signal) in [("reset", "tb.reset"), ("dut.reset", "tb.dut.reset")] {
        let (_dir, path) = store(
            &[(name, 1)],
            &[(0, &format!("1 {name}")), (22, &format!("0 {name}"))],
            100,
        );
        let json = json_of(&["overview", &path, "--format", "json"]);
        assert_eq!(json["reset"]["signal"], signal);
        assert_eq!(json["reset"]["deassert_tick"], 22);
        let (_, err, code) = run(&["signal", &path, "-s", "clk"]);
        assert_eq!(code, 0, "{err}");
        assert_eq!(
            reset_lines(&err),
            [format!(
                "# reset: {signal} (active-high by name, auto) deasserts @ 22ns; 2 cycles skipped (--with-reset to include)"
            )]
        );
    }
}

/// A store with `rst_sig` driven by `rst`, plus the shared `flag`, `early` and
/// `gclk` (a clock with no edges from 56 to 94) signals.
fn pulse_store(rst_sig: &str, rst: &[(u64, &str)]) -> (tempfile::TempDir, String) {
    let mut events: Vec<(u64, String)> = vec![
        (0, "0 flag".into()),
        (12, "1 flag".into()),
        (18, "0 flag".into()),
        (42, "1 flag".into()),
        (48, "0 flag".into()),
        (0, "0 early".into()),
        (12, "1 early".into()),
        (0, "0 gclk".into()),
    ];
    for tick in (5..=100).step_by(5).filter(|t| *t <= 55 || *t >= 95) {
        let value = if tick % 10 == 5 { 1 } else { 0 };
        events.push((tick, format!("{value} gclk")));
    }
    for (tick, value) in rst {
        events.push((*tick, format!("{value} {rst_sig}")));
    }
    let events: Vec<(u64, &str)> = events.iter().map(|(t, e)| (*t, e.as_str())).collect();
    store(
        &[(rst_sig, 1), ("flag", 1), ("early", 1), ("gclk", 1)],
        &events,
        100,
    )
}

#[test]
fn reset_pulse_from_inactive_deasserts_where_it_ends() {
    // `asserted` is the reference: asserted from tick 0, deasserted at 25.
    let (_a, asserted) = pulse_store("rst", &[(0, "1"), (25, "0")]);
    let (_p, pulse) = pulse_store("rst", &[(0, "0"), (7, "1"), (25, "0")]);
    let (_n, pulse_n) = pulse_store("rst_n", &[(0, "1"), (7, "0"), (25, "1")]);
    let (_t, twice) = pulse_store(
        "rst",
        &[(0, "0"), (7, "1"), (25, "0"), (60, "1"), (70, "0")],
    );
    let commands: [&[&str]; 14] = [
        &["signal", "-s", "flag"],
        &["signal", "-s", "flag", "-t", "2c:4c"],
        &["signal", "-s", "flag", "-t", "200c:"],
        &["wave", "-s", "flag"],
        &["value", "-s", "flag", "--at", "2"],
        &["find", "flag", "'b1"],
        &["sample", "flag", "change", "-s", "flag"],
        &["diff", "1", "3", "-s", "flag"],
        &["distance", "flag", "change"],
        &["stats", "-s", "flag"],
        &["stats", "-s", "clk"],
        &["stuck", "-s", "early"],
        &["overview", "--clock", "tb.gclk"],
        &["value", "--async", "--at", "2c", "-s", "flag"],
    ];
    let body = |text: &str| -> String {
        text.lines()
            .filter(|l| !l.starts_with("# reset:"))
            .collect::<Vec<_>>()
            .join("\n")
    };
    let sources: [(&str, &str, &[&str], &str, &str); 5] = [
        ("pulse", &pulse, &[], "tb.rst", "active-high"),
        ("pulse_n", &pulse_n, &[], "tb.rst_n", "active-low"),
        ("twice", &twice, &[], "tb.rst", "active-high"),
        (
            "pulse_n --reset",
            &pulse_n,
            &["--reset", "tb.rst*"],
            "tb.rst_n",
            "active-low",
        ),
        (
            "pulse --reset",
            &pulse,
            &["--reset", "tb.rst"],
            "tb.rst",
            "active-high",
        ),
    ];
    for (label, path, extra, signal, polarity) in sources {
        for command in commands {
            let run_on = |path: &str| {
                let mut args = vec![command[0], path];
                args.extend_from_slice(&command[1..]);
                args.extend_from_slice(extra);
                let (out, err, code) = run(&args);
                assert_eq!(code, 0, "{label} {args:?}: {err}");
                (body(&out), body(&err))
            };
            assert_eq!(run_on(path), run_on(&asserted), "{label} {command:?}");
        }
        let chosen = if extra.is_empty() { "auto" } else { "--reset" };
        let mut args = vec!["signal", path, "-s", "flag"];
        args.extend_from_slice(extra);
        let (_, err_extra, _) = run(&args);
        let line = format!(
            "# reset: {signal} ({polarity} by name, {chosen}) deasserts @ 25ns; 2 cycles skipped (--with-reset to include)"
        );
        assert_eq!(reset_lines(&err_extra), [line.as_str()], "{label}");
        let mut args = vec!["overview", path, "--format", "json"];
        args.extend_from_slice(extra);
        let reset = &json_of(&args)["reset"];
        assert_eq!(reset["deassert_tick"], 25, "{label}");
        assert_eq!(reset["deassert_time"], "25ns", "{label}");
        assert_eq!(reset["skipped_cycles"], 2, "{label}");
        assert_eq!(reset["chosen_by"], chosen, "{label}");
    }
}

#[test]
fn polarity_follows_low_marker_names() {
    let names = [
        "rst_n", "rstn", "rst_ni", "reset_b", "rst", "reset", "rst_gen", "rst_in", "reset_en",
    ];
    let vars: Vec<(&str, u32)> = names.iter().map(|n| (*n, 1)).collect();
    let events: Vec<(u64, String)> = names
        .iter()
        .flat_map(|n| [(0, format!("0 {n}")), (30, format!("1 {n}"))])
        .collect();
    let events: Vec<(u64, &str)> = events.iter().map(|(t, e)| (*t, e.as_str())).collect();
    let (_dir, path) = store(&vars, &events, 100);
    for name in names {
        let low = ["rst_n", "rstn", "rst_ni", "reset_b"].contains(&name);
        let json = json_of(&[
            "overview",
            &path,
            "--reset",
            &format!("tb.{name}"),
            "--format",
            "json",
        ]);
        let reset = &json["reset"];
        assert_eq!(reset["signal"], format!("tb.{name}"), "{name}");
        assert_eq!(
            reset["polarity"],
            if low { "active_low" } else { "active_high" },
            "{name}"
        );
        assert_eq!(
            reset["deassert_tick"],
            if low { json!(30) } else { Value::Null },
            "{name}"
        );
    }
}

#[test]
fn x_initial_reset_waits_for_inactive_value() {
    let (_dir, path) = store(
        &[("rst_n", 1)],
        &[(0, "x rst_n"), (10, "0 rst_n"), (40, "1 rst_n")],
        100,
    );
    let json = json_of(&["overview", &path, "--format", "json"]);
    assert_eq!(
        json["reset"],
        json!({
            "signal": "tb.rst_n",
            "polarity": "active_low",
            "chosen_by": "auto",
            "deassert_tick": 40,
            "deassert_time": "40ns",
            "skipped_cycles": 4,
            "with_reset": false
        })
    );
    let (out, err, code) = run(&["signal", &path, "-s", "rst_n", "-t", "1:1"]);
    assert_eq!(code, 0, "{err}");
    assert_eq!(out.trim(), "1 rst_n 1");

    // X straight to inactive: the X counts as the asserted value.
    let (_dir, path) = store(&[("rst_n", 1)], &[(0, "x rst_n"), (20, "1 rst_n")], 100);
    let json = json_of(&["overview", &path, "--format", "json"]);
    assert_eq!(json["reset"]["deassert_tick"], 20);
}

#[test]
fn sync_text_and_overview_print_one_reset_line() {
    let (_dir, path) = auto_store();
    let commands: [&[&str]; 9] = [
        &["signal", "-s", "flag"],
        &["wave", "-s", "flag"],
        &["value", "-s", "flag", "--at", "1"],
        &["find", "flag", "'b1"],
        &["sample", "flag", "change", "-s", "flag"],
        &["diff", "1", "3", "-s", "flag"],
        &["distance", "flag", "change"],
        &["stats", "-s", "flag"],
        &["stuck", "'b0"],
    ];
    for command in commands {
        let mut args = vec![command[0], path.as_str()];
        args.extend_from_slice(&command[1..]);
        let (_, err, code) = run(&args);
        assert_eq!(code, 0, "{args:?}: {err}");
        assert_eq!(reset_lines(&err), [AUTO_LINE], "{args:?}");

        args.push("--with-reset");
        let (_, err, _) = run(&args);
        assert_eq!(
            reset_lines(&err),
            ["# reset: tb.rst_n (active-low by name, auto) deasserts @ 25ns; reset phase included (--with-reset)"],
            "{args:?}"
        );
    }
    for command in [
        ["signal", "-s", "flag"],
        ["wave", "-s", "flag"],
        ["stats", "-s", "flag"],
        ["find", "flag", "'b1"],
    ] {
        let mut args = vec![command[0], path.as_str()];
        args.extend_from_slice(&command[1..]);
        args.push("--async");
        let (out, err, code) = run(&args);
        assert_eq!(code, 0, "{args:?}: {err}");
        assert!(
            !err.contains("# reset:") && !out.contains("# reset:"),
            "{args:?}"
        );
    }

    let (out, _, code) = run(&["overview", &path]);
    assert_eq!(code, 0);
    assert_eq!(reset_lines(&out), [AUTO_LINE]);
    let (out, _, _) = run(&["overview", &path, "--with-reset"]);
    assert_eq!(
        reset_lines(&out),
        ["# reset: tb.rst_n (active-low by name, auto) deasserts @ 25ns; reset phase included (--with-reset)"]
    );

    let (_idle, idle_path) = store(
        &[("rst", 1), ("flag", 1)],
        &[(0, "0 rst"), (12, "1 flag")],
        100,
    );
    let idle_line = "# reset: tb.rst (active-high by name, auto) never asserts; no cycles skipped";
    let (out, err, _) = run(&["signal", &idle_path, "-s", "flag"]);
    assert_eq!(reset_lines(&err), [idle_line]);
    let (with_out, _, _) = run(&["signal", &idle_path, "-s", "flag", "--with-reset"]);
    assert_eq!(out, with_out);
    let (out, _, _) = run(&["overview", &idle_path]);
    assert_eq!(reset_lines(&out), [idle_line]);

    let (_none, none_path) = store(&[("flag", 1)], &[(0, "0 flag"), (12, "1 flag")], 100);
    let none_line = "# reset: none detected; no cycles skipped";
    let (_, err, _) = run(&["signal", &none_path, "-s", "flag"]);
    assert_eq!(reset_lines(&err), [none_line]);
    let (out, _, _) = run(&["overview", &none_path]);
    assert_eq!(reset_lines(&out), [none_line]);
}

#[test]
fn json_reset_object_matches_header() {
    let (_dir, path) = auto_store();
    let auto = json!({
        "signal": "tb.rst_n",
        "polarity": "active_low",
        "chosen_by": "auto",
        "deassert_tick": 25,
        "deassert_time": "25ns",
        "skipped_cycles": 2,
        "with_reset": false
    });
    let commands: [&[&str]; 3] = [
        &["find", "flag", "'b1"],
        &["stats", "-s", "flag"],
        &["overview"],
    ];
    for command in commands {
        let mut args = vec![command[0], path.as_str()];
        args.extend_from_slice(&command[1..]);
        args.extend(["--format", "json"]);
        assert_eq!(json_of(&args)["reset"], auto, "{args:?}");
    }
    let chosen = json_of(&["overview", &path, "--reset", "tb.flag", "--format", "json"]);
    assert_eq!(
        chosen["reset"],
        json!({
            "signal": "tb.flag",
            "polarity": "active_high",
            "chosen_by": "--reset",
            "deassert_tick": 18,
            "deassert_time": "18ns",
            "skipped_cycles": 2,
            "with_reset": false
        })
    );
    let (_idle, idle_path) = store(&[("rst", 1)], &[(0, "0 rst")], 100);
    assert_eq!(
        json_of(&["overview", &idle_path, "--format", "json"])["reset"],
        json!({
            "signal": "tb.rst",
            "polarity": "active_high",
            "chosen_by": "auto",
            "deassert_tick": null,
            "deassert_time": null,
            "skipped_cycles": 0,
            "with_reset": false
        })
    );
    let with = json_of(&[
        "find",
        &path,
        "flag",
        "'b1",
        "--with-reset",
        "--format",
        "json",
    ]);
    assert_eq!(
        with["reset"],
        json!({
            "signal": "tb.rst_n",
            "polarity": "active_low",
            "chosen_by": "auto",
            "deassert_tick": 25,
            "deassert_time": "25ns",
            "skipped_cycles": 0,
            "with_reset": true
        })
    );
    let (_none, none_path) = store(&[("flag", 1)], &[(0, "0 flag")], 100);
    assert_eq!(
        json_of(&["overview", &none_path, "--format", "json"])["reset"],
        json!({
            "signal": null,
            "polarity": null,
            "chosen_by": "auto",
            "deassert_tick": null,
            "deassert_time": null,
            "skipped_cycles": 0,
            "with_reset": false
        })
    );
    let asynchronous = json_of(&["find", &path, "flag", "'b1", "--async", "--format", "json"]);
    assert!(asynchronous.get("reset").is_none());
    assert!(json_of(&["list", &path, "--format", "json"])
        .get("reset")
        .is_none());
}

#[test]
fn bad_reset_pattern_exits_2_naming_it() {
    let (_dir, path) = store(
        &[("rst_n", 1), ("bus", 8)],
        &[(0, "0 rst_n"), (0, "b00000000 bus"), (25, "1 rst_n")],
        100,
    );
    let commands: [&[&str]; 4] = [
        &["signal", "-s", "clk"],
        &["signal", "-s", "clk", "--async"],
        &["overview"],
        &["list"],
    ];
    for pattern in ["no_such", "tb.bus", "[bad"] {
        for command in commands {
            let mut args = vec![command[0], path.as_str()];
            args.extend_from_slice(&command[1..]);
            args.extend(["--reset", pattern]);
            let (_, err, code) = run(&args);
            assert_eq!(code, 2, "{args:?}: {err}");
            assert!(
                err.contains(&format!("--reset '{pattern}'")),
                "{args:?}: {err}"
            );
        }
    }
}

#[test]
fn find_no_match_hint_follows_skipped_cycles() {
    let (_dir, path) = auto_store();
    let (_, err, code) = run(&["find", &path, "flag", "'b1"]);
    assert_eq!(code, 0, "{err}");
    assert!(err.lines().any(|l| l == "# No matches found; 2 cycles skipped before reset deasserts (--with-reset to include)"), "{err}");
    let (out, err, _) = run(&["find", &path, "flag", "'b1", "--with-reset"]);
    assert_eq!(out.trim(), "cycle 2 flag 1");
    assert!(!err.contains("No matches"), "{err}");

    // Reset deasserts at 3, before the first rise at 5: nothing is skipped.
    let (_early, early_path) = store(
        &[("rst_n", 1), ("flag", 1)],
        &[(0, "0 rst_n"), (0, "0 flag"), (3, "1 rst_n")],
        100,
    );
    let (_, err, code) = run(&["find", &early_path, "flag", "'b1"]);
    assert_eq!(code, 0, "{err}");
    assert!(err.lines().any(|l| l == "# No matches found"), "{err}");
    assert!(!err.contains("skipped before"), "{err}");
}

#[test]
fn deassert_time_does_not_saturate_for_large_ticks() {
    let dir = tempfile::tempdir().unwrap();
    let source = dir.path().join("big.vcd");
    let fst = dir.path().join("big.fst");
    std::fs::write(
        &source,
        "$timescale 100fs $end\n$scope module tb $end\n$var wire 1 ! clk $end\n\
         $var wire 1 \" rst $end\n$upscope $end\n$enddefinitions $end\n\
         #0\n0!\n1\"\n#200000000000000000\n0\"\n1!\n",
    )
    .unwrap();
    let (_, err, code) = run(&[
        "build",
        source.to_str().unwrap(),
        "-o",
        fst.to_str().unwrap(),
    ]);
    assert_eq!(code, 0, "{err}");
    let json = json_of(&["overview", fst.to_str().unwrap(), "--format", "json"]);
    assert_eq!(json["reset"]["deassert_tick"], 200000000000000000u64);
    assert_eq!(json["reset"]["deassert_time"], "20000000000000000000fs");
}
