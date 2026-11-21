//! Observable CLI contracts for #1428; all fixtures are converted to stores.
use std::io::Read;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

fn run(args: &[&str]) -> (String, String, i32) {
    let mut child = Command::new(env!("CARGO_BIN_EXE_bwave"))
        .args(args)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut stdout = child.stdout.take().unwrap();
    let mut stderr = child.stderr.take().unwrap();
    let out = std::thread::spawn(move || {
        let mut s = String::new();
        stdout.read_to_string(&mut s).unwrap();
        s
    });
    let err = std::thread::spawn(move || {
        let mut s = String::new();
        stderr.read_to_string(&mut s).unwrap();
        s
    });
    let start = Instant::now();
    let status = loop {
        if let Some(status) = child.try_wait().unwrap() {
            break status;
        }
        if start.elapsed() > Duration::from_secs(10) {
            child.kill().unwrap();
            child.wait().unwrap();
            panic!("CLI exceeded 10 seconds: {args:?}");
        }
        std::thread::sleep(Duration::from_millis(5));
    };
    (
        out.join().unwrap(),
        err.join().unwrap(),
        status.code().unwrap_or(-1),
    )
}

fn store(body: &str, extra_header: &str) -> (tempfile::TempDir, String, String) {
    let dir = tempfile::tempdir().unwrap();
    let vcd = dir.path().join("clock.vcd");
    let fst = dir.path().join("clock.fst");
    std::fs::write(&vcd,format!("$timescale 1ns $end\n$scope module tb $end\n$var wire 1 ! clk $end\n$var wire 1 \" valid $end\n$var wire 4 # data $end\n{extra_header}$upscope $end\n$enddefinitions $end\n{body}")).unwrap();
    let (_, err, code) = run(&["build", vcd.to_str().unwrap(), "-o", fst.to_str().unwrap()]);
    assert_eq!(code, 0, "{err}");
    (dir, fst.to_str().unwrap().into(), err)
}

fn clock(rises: &[u64], end: u64) -> String {
    let mut s = "#0\n0!\n1\"\nb0000 #\n".to_string();
    for &t in rises {
        s += &format!("#{t}\n1!\n#{}\n0!\n", t + 1);
    }
    s += &format!("#{end}\n");
    s
}

fn json(command: &str, path: &str, args: &[&str]) -> serde_json::Value {
    let mut tokens = vec![command, path];
    tokens.extend_from_slice(args);
    tokens.extend(["--format", "json"]);
    let (out, err, code) = run(&tokens);
    assert_eq!(code, 0, "{err}");
    serde_json::from_str(&out).unwrap_or_else(|e| panic!("{e}: {out}"))
}

const GATED:&str="#0\n0!\n1\"\nb0000 #\n#5\n1!\nb0001 #\n#10\n0!\n#15\n1!\nb0010 #\n#20\n0!\n#31\nb0011 #\n#55\n1!\nb0100 #\n#60\n0!\n";

#[test]
fn stopped_levels_and_window_metadata() {
    let (_dir, path, build) = store(GATED, "");
    assert!(build.contains("pass (stopped_stretches)"), "{build}");
    assert!(
        build.contains("1 stretches, 3 cycles, longest 3:5"),
        "{build}"
    );
    for (flag, count) in [(false, 3), (true, 6)] {
        let mut args = vec!["valid", "'h1", "--count", "-t", "1:6"];
        if flag {
            args.push("--include-stopped");
        }
        let payload = json("find", &path, &args);
        assert_eq!(payload["data"]["count"], count);
        assert_eq!(
            payload["clock_stopped"],
            serde_json::json!([{"from_cycle":3,"to_cycle":5}])
        );
        assert_eq!(payload["warnings"], serde_json::json!([]));
        let mut args = vec!["sample", &path, "valid", "'h1", "--count", "-t", "1:6"];
        if flag {
            args.push("--include-stopped");
        }
        let (out, err, code) = run(&args);
        assert_eq!(code, 0, "{err}");
        assert_eq!(out.trim(), count.to_string());
    }
    let p = json("value", &path, &["--at", "4", "-s", "data"]);
    assert_eq!(p["clock_stopped"][0]["from_cycle"], 3);
    let (out, err, code) = run(&["value", &path, "--at", "4", "-s", "data"]);
    assert_eq!(code, 0, "{out}\n{err}");
    assert!(err.contains("cycle 4 is stopped"), "{err}");
    let p = json("value", &path, &["--at", "2"]);
    assert!(p.get("clock_stopped").is_none());
    let (_, err, code) = run(&["find", &path, "valid", "'h1", "-t", "1:2"]);
    assert_eq!(code, 0);
    assert!(!err.contains("stopped"), "{err}");
    // Query windows preserve the full stretch endpoints, including a point in it.
    let p = json("find", &path, &["valid", "'h1", "--count", "-t", "4:4"]);
    assert_eq!(p["data"]["count"], 0);
    // Distance retains elapsed grid distance, and reports the stopped share.
    let p = json("distance", &path, &["data", "change", "-t", "1:6"]);
    assert_eq!(p["data"]["pairs"][1]["elapsed"], 2);
    assert_eq!(p["data"]["pairs"][0]["stopped_cycles"], 2);
    for (window, total, clocked) in [("1:6", 6, 3), ("0:6", 6, 3)] {
        let p = json("stats", &path, &["-s", "valid", "-t", window]);
        let signal = &p["data"]["signals"][0];
        assert_eq!(p["data"]["total_cycles"], total, "{window}");
        assert_eq!(signal["time_in_state_clocked_cycles"]["'h1"], clocked);
        assert_eq!(signal["time_in_state_stopped_cycles"]["'h1"], 3);
    }
}

#[test]
fn stopped_rendering_preserves_inside_stop_changes() {
    let (_dir, path, _) = store(GATED, "");
    let (out, err, code) = run(&["signal", &path, "-s", "data", "-t", "1:6"]);
    assert_eq!(code, 0, "{err}");
    let before = out.find("2 data").unwrap();
    let separator = out.find("# clk stopped 3:5").unwrap();
    let inside = out.find("4 data").unwrap();
    let resume = out.find("6 data").unwrap();
    assert!(
        before < separator && separator < inside && inside < resume,
        "{out}"
    );
    let p = json("signal", &path, &["-s", "data", "-t", "1:6"]);
    assert_eq!(p["data"]["records"][2]["kind"], "clock_stopped");
    for rle in [false, true] {
        let mut args = vec![
            "wave", &path, "-s", "data", "-t", "1:6", "--marker", "middle", "4",
        ];
        if rle {
            args.push("--rle");
        }
        let (out, err, code) = run(&args);
        assert_eq!(code, 0, "{err}");
        assert!(out.contains("clk stopped 3:5 [middle]"), "{out}");
        assert!(out.contains('6'), "{out}");
    }
}

fn assert_next_step(p: &serde_json::Value) {
    for finding in p["clock_regularity"]["findings"].as_array().unwrap() {
        let detail = finding["detail"].as_str().unwrap();
        assert!(
            detail.contains("--async") || detail.contains("--clock"),
            "{finding}"
        );
    }
}

#[test]
fn regularity_reasons_and_overrides() {
    for (rises, status, reason) in [
        (vec![5, 15, 25, 35], "pass", "regular"),
        (vec![25, 35, 45], "pass", "regular"), // delayed start, final stop ignored
        (vec![5, 15, 56, 66], "warning", "off_grid_edge"),
        (vec![5, 15, 20, 25], "warning", "off_grid_edge"), // frequency doubles
        (vec![5, 15, 35, 55], "pass", "stopped_stretches"), // integer slowdown is on grid
        (
            vec![5, 15, 26, 36, 47, 57, 68, 78],
            "warning",
            "non_integer_period",
        ),
        (vec![5, 15, 28, 40, 49, 66, 77], "warning", "off_grid_edge"),
        (vec![5], "unavailable", "insufficient_edges"),
        (vec![5, 35, 45, 55], "warning", "off_grid_edge"), // stopped first gap misestimates period
    ] {
        let (_dir, path, _) = store(&clock(&rises, 100), "");
        for args in [vec![], vec!["--clock", "clk"]] {
            let p = json("overview", &path, &args);
            assert_eq!(p["clock_regularity"]["status"], status, "{p}");
            assert_eq!(p["clock_regularity"]["reason"], reason, "{p}");
            assert_next_step(&p);
        }
    }
    for unknown in ['x', 'z'] {
        let body = clock(&[5, 15, 25, 35], 50).replace("#26\n0!", &format!("#26\n{unknown}!"));
        let (_dir, path, _) = store(&body, "");
        let p = json("overview", &path, &[]);
        assert_eq!(p["clock_regularity"]["status"], "warning");
        assert_next_step(&p);
        assert!(p["clock_regularity"]["findings"]
            .as_array()
            .unwrap()
            .iter()
            .any(|f| f["reason"] == "unknown_clock"));
    }
    let body = clock(&[5, 15, 25, 35], 50)
        .replace("#0\n", "#0\n0$\n")
        .replace("#5\n", "#5\n1$\n")
        .replace("#6\n", "#6\n0$\n")
        .replace("#25\n", "#25\n1$\n");
    let (_dir, path, _) = store(&body, "$var wire 1 $ slow_clk $end\n");
    let p = json("overview", &path, &[]);
    assert!(p["clock_regularity"]["findings"]
        .as_array()
        .unwrap()
        .iter()
        .any(|f| f["reason"] == "multiple_clock_periods"));
    assert_next_step(&p);
    let p = json("overview", &path, &["--clock", "slow_clk"]);
    assert_eq!(p["data"]["period_ticks"], 20);
}

#[test]
fn clock_header_says_how_the_clock_was_chosen() {
    let (_dir, path, build_err) = store(&clock(&[5, 15, 25, 35], 50), "");
    assert!(
        build_err.contains("# clock: tb.clk (auto-detected; --clock PATTERN to override)\n"),
        "{build_err}"
    );
    let (out, err, code) = run(&["overview", &path, "--clock", "clk"]);
    assert_eq!(code, 0, "{err}");
    assert!(out.starts_with("# clock: tb.clk (--clock)\n"), "{out}");
    let line = "# clock override: tb.clk (period=10 ticks, first_rise=5)";
    assert_eq!(err.matches(line).count(), 1, "{err}");
    let (_, err, code) = run(&["overview", &path, "--clock", "clk", "--format", "json"]);
    assert_eq!(code, 0, "{err}");
    assert!(!err.contains("clock override"), "{err}");
}

#[test]
fn every_sync_query_warns_and_async_cycle_tokens_warn() {
    let (_dir, path, _) = store(&clock(&[5, 15, 56, 66], 80), "");
    for (command, args) in [
        ("list", vec!["-s", "data"]),
        ("signal", vec!["-s", "data"]),
        ("wave", vec!["-s", "data"]),
        ("value", vec!["--at", "1"]),
        ("find", vec!["valid", "'h1"]),
        ("sample", vec!["valid", "'h1", "--count"]),
        ("diff", vec!["1", "2"]),
        ("distance", vec!["valid", "'h1"]),
        ("stats", vec!["-s", "data"]),
        ("stuck", vec!["-s", "data"]),
    ] {
        let mut tokens = vec![command, path.as_str()];
        tokens.extend(args);
        let (_, err, code) = run(&tokens);
        assert_eq!(code, 0, "{command}: {err}");
        assert!(err.contains("off_grid_edge at tick 56"), "{command}: {err}");
    }
    for (bound, warn) in [("1c:2c", true), ("1t:20t", false)] {
        let p = json(
            "find",
            &path,
            &["valid", "'h1", "--count", "--async", "-t", bound],
        );
        assert_eq!(!p["warnings"].as_array().unwrap().is_empty(), warn, "{p}");
    }
    // No stopped ranges fabricated after divergence.
    let p = json("find", &path, &["valid", "'h1", "--count", "-t", "3:7"]);
    assert_eq!(p["data"]["count"], 5);
    assert!(p.get("clock_stopped").is_none());
}

#[test]
fn pre_edge_preserves_cycles_and_trigger_capture_consistency() {
    let body="#0\n0!\n0\"\nb0000 #\n#3\nb0001 #\n#5\n1!\n1\"\nb0010 #\n#10\n0!\n#13\nb0011 #\n#15\n1!\nb0100 #\n#20\n0!\n#25\n1!\n#30\n0!\n";
    let (_dir, path, _) = store(body, "");
    for (cycle, pre, post) in [(1, "1", "2"), (2, "3", "4")] {
        let c = cycle.to_string();
        for (mode, expected) in [("pre-edge", pre), ("post-edge", post)] {
            let p = json(
                "value",
                &path,
                &["--at", &c, "-s", "data", "--sample-mode", mode],
            );
            assert_eq!(p["data"]["at"], cycle);
            assert_eq!(p["data"]["signals"][0]["value"], expected, "{p}");
            assert_eq!(p["data"]["target_tick"], 5 + (cycle - 1) * 10);
            assert_eq!(
                p["data"]["sample_tick"],
                5 + (cycle - 1) * 10 - i32::from(mode == "pre-edge")
            );
        }
    }
    let p = json(
        "find",
        &path,
        &["valid", "rising", "--sample-mode", "pre-edge"],
    );
    assert_eq!(p["data"]["matches"][0]["time"], 2);
    let (out, err, code) = run(&[
        "sample",
        &path,
        "valid",
        "rising",
        "-s",
        "data",
        "--sample-mode",
        "pre-edge",
    ]);
    assert_eq!(code, 0, "{err}");
    assert_eq!(out.trim(), "2 data 3");
    let (out, err, code) = run(&[
        "sample",
        &path,
        "v",
        "rising",
        "-s",
        "v",
        "--virtual",
        "v = valid",
        "--sample-mode",
        "pre-edge",
    ]);
    assert_eq!(code, 0, "{err}");
    assert_eq!(out.trim(), "2 v 1");
    for mode in ["pre-edge", "post-edge"] {
        let (_, err, code) = run(&[
            "value",
            &path,
            "--at",
            "5t",
            "--async",
            "--sample-mode",
            mode,
        ]);
        assert_eq!(code, 2, "{err}");
    }
    let p = json("value", &path, &["--at", "0", "--sample-mode", "pre-edge"]);
    assert_eq!(p["data"]["sample_tick"], serde_json::Value::Null);
    assert!(p["data"]["signals"]
        .as_array()
        .unwrap()
        .iter()
        .all(|s| s["value"] == "x"));
}

#[test]
fn huge_stop_is_bounded_and_break_survives_limit() {
    let (_dir, path, _) = store(&clock(&[5, 15, 10_000_000_005], 10_000_000_006), "");
    let p = json("find", &path, &["valid", "'h1", "--count"]);
    assert_eq!(p["data"]["count"], 3);
    let p = json("wave", &path, &["-s", "valid", "-t", "1:", "--limit", "4"]);
    assert_eq!(p["data"]["columns"].as_array().unwrap().len(), 4);
    assert_eq!(p["data"]["columns"][3], "1000000001");
    let p = json("signal", &path, &["-s", "valid", "--limit", "2"]);
    assert_eq!(p["data"]["records"].as_array().unwrap().len(), 2);
}

#[test]
fn reset_rebases_stops_and_pre_edge_is_strict() {
    let body = GATED
        .replace("#0\n", "#0\n0$\n")
        .replace("#10\n", "#10\n1$\n");
    let (_dir, path, _) = store(&body, "$var wire 1 $ rstn $end\n");
    let p = json("value", &path, &["--at", "2", "-s", "data"]);
    assert_eq!(p["clock_stopped"][0]["from_cycle"], 2);
    assert_eq!(p["clock_stopped"][0]["to_cycle"], 4);
    let p = json(
        "value",
        &path,
        &["--at", "1", "-s", "data", "--sample-mode", "pre-edge"],
    );
    assert_eq!(p["data"]["target_tick"], 15);
    assert_eq!(p["data"]["sample_tick"], 14);
    assert_eq!(p["data"]["signals"][0]["value"], "1");
}

#[test]
fn pre_edge_views_agree_at_between_edge_drives() {
    let body = "#0\n0!\n0\"\nb0000 #\n#3\nb0001 #\n#5\n1!\nb0010 #\n#10\n0!\n#13\nb0011 #\n#15\n1!\nb0100 #\n#20\n0!\n#25\n1!\n#30\n0!\n";
    let (_dir, path, _) = store(body, "");
    let p = json(
        "wave",
        &path,
        &["-s", "data", "-t", "1:3", "--sample-mode", "pre-edge"],
    );
    assert_eq!(p["data"]["values"][0], serde_json::json!(["1", "3", "4"]));
    let (out, err, code) = run(&[
        "signal",
        &path,
        "-s",
        "data",
        "-t",
        "1:3",
        "--sample-mode",
        "pre-edge",
    ]);
    assert_eq!(code, 0, "{err}");
    assert_eq!(out, "1 data 1\n2 data 3\n3 data 4\n");
    let p = json("find", &path, &["data", "'h3", "--sample-mode", "pre-edge"]);
    assert_eq!(p["data"]["matches"][0]["time"], 2);
    let p = json(
        "distance",
        &path,
        &["data", "change", "--sample-mode", "pre-edge"],
    );
    assert_eq!(p["data"]["pairs"][0]["from"], 2);
    assert_eq!(p["data"]["pairs"][0]["to"], 3);
    let (out, err, code) = run(&[
        "diff",
        &path,
        "1",
        "2",
        "-s",
        "data",
        "--sample-mode",
        "pre-edge",
    ]);
    assert_eq!(code, 0, "{err}");
    assert!(out.contains("@1=1") && out.contains("@2=3"), "{out}");
    let p = json(
        "stats",
        &path,
        &["-s", "data", "-t", "2:2", "--sample-mode", "pre-edge"],
    );
    assert_eq!(
        p["data"]["signals"][0]["time_in_state_clocked_cycles"]["'h3"],
        1
    );
    let (out, err, code) = run(&[
        "stats",
        &path,
        "-s",
        "data",
        "-t",
        "2:2",
        "--sample-mode",
        "pre-edge",
    ]);
    assert_eq!(code, 0, "{err}");
    assert!(
        out.contains("3: 100% (1 clocked, 0 stopped cycles)"),
        "{out}"
    );
}

#[test]
fn stops_keep_edge_triggers_and_distance_statistics() {
    let (_dir, path, _) = store(GATED, "");
    let normal = json("find", &path, &["data", "change", "-t", "1:6"]);
    let included = json(
        "find",
        &path,
        &["data", "change", "-t", "1:6", "--include-stopped"],
    );
    assert_eq!(normal["data"], included["data"]);
    let p = json("distance", &path, &["valid", "'h1", "--stats", "-t", "1:6"]);
    assert_eq!(p["data"]["count"], 5);
    assert_eq!(p["data"]["stopped_cycles"], 3);
    let p = json(
        "distance",
        &path,
        &[
            "valid", "'h1", "--to", "data", "'h4", "--stats", "-t", "1:6",
        ],
    );
    assert_eq!(p["data"]["count"], 5);
    assert_eq!(p["data"]["stopped_cycles"], 9);
    for (direction, count, first) in [("--first", 1, 1), ("--last", 3, 6)] {
        let p = json("find", &path, &["valid", "'h1", "-t", "1:6", direction]);
        assert_eq!(p["data"]["count"], count);
        assert_eq!(p["data"]["matches"][0]["time"], first);
    }
}

/// Period 10, clk rises at 5, 15, 25, 35 and 45. `data` becomes 1, 2, 3 at
/// 15, 25, 35; `p` rises at 14 so a pre-edge read of tick 14 differs from one
/// of tick 13; `rstn` rises at `reset_at`.
fn reset_body(reset_at: u64) -> String {
    let mut events: std::collections::BTreeMap<u64, String> = std::collections::BTreeMap::new();
    let mut at = |t: u64, s: &str| events.entry(t).or_default().push_str(s);
    at(0, "0!\n1\"\nb0000 #\n0$\n0%\n");
    for t in [5, 15, 25, 35, 45] {
        at(t, "1!\n");
        at(t + 1, "0!\n");
    }
    at(14, "1%\n");
    at(15, "b0001 #\n");
    at(25, "b0010 #\n");
    at(35, "b0011 #\n");
    at(reset_at, "1$\n");
    let mut body = String::new();
    for (t, s) in events {
        body += &format!("#{t}\n{s}");
    }
    body + "#50\n"
}

const RESET_HEADER: &str = "$var wire 1 $ rstn $end\n$var wire 1 % p $end\n";

fn reset_json(command: &str, path: &str, args: &[&str], mode: &str) -> serde_json::Value {
    let mut all = args.to_vec();
    all.extend(["--sample-mode", mode]);
    json(command, path, &all)["data"].clone()
}

fn reset_text(command: &str, path: &str, args: &[&str], mode: &str) -> String {
    let mut all = vec![command, path];
    all.extend_from_slice(args);
    all.extend(["--sample-mode", mode]);
    let (out, err, code) = run(&all);
    assert_eq!(code, 0, "{err}");
    out
}

fn strings(v: &serde_json::Value) -> Vec<String> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|x| x.as_str().unwrap().to_string())
        .collect()
}

fn match_times(v: &serde_json::Value) -> Vec<u64> {
    v["matches"]
        .as_array()
        .unwrap()
        .iter()
        .map(|m| m["time"].as_u64().unwrap())
        .collect()
}

fn pair_list(v: &serde_json::Value) -> Vec<(u64, u64)> {
    v["pairs"]
        .as_array()
        .unwrap()
        .iter()
        .map(|p| (p["from"].as_u64().unwrap(), p["to"].as_u64().unwrap()))
        .collect()
}

#[test]
fn cycle_zero_reads_before_the_start_tick() {
    // (reset tick, mode, cycle 0 sample tick, cycle 1 sample tick, cycle 0
    // data/rstn/p at cycle 0, wave data/rstn/p rows for cycles 0..2).
    type Row = (u64, &'static str, [u64; 3], &'static str, &'static str);
    let rows: [Row; 4] = [
        (
            15,
            "post-edge",
            [14, 15, 25],
            "0 0 1",
            "[0 1 2] [0 1 1] [1 1 1]",
        ),
        (
            15,
            "pre-edge",
            [13, 14, 24],
            "0 0 0",
            "[0 0 1] [0 0 1] [0 1 1]",
        ),
        (
            18,
            "post-edge",
            [17, 25, 35],
            "1 0 1",
            "[1 2 3] [0 1 1] [1 1 1]",
        ),
        (
            18,
            "pre-edge",
            [17, 24, 34],
            "1 0 1",
            "[1 1 2] [0 1 1] [1 1 1]",
        ),
    ];
    for (reset_at, mode, [read0, read1, _], zero, rows_wave) in rows {
        let (_dir, path, _) = store(&reset_body(reset_at), RESET_HEADER);
        let on_edge = reset_at == 15;
        let pre = mode == "pre-edge";
        let case = format!("reset {reset_at} {mode}");
        let v0 = reset_json(
            "value",
            &path,
            &["--at", "0", "-s", "data", "-s", "rstn", "-s", "p"],
            mode,
        );
        assert_eq!(v0["sample_tick"], read0, "{case}");
        assert_eq!(v0["target_tick"], reset_at, "{case}");
        let got: Vec<String> = v0["signals"]
            .as_array()
            .unwrap()
            .iter()
            .map(|s| s["value"].as_str().unwrap().to_string())
            .collect();
        assert_eq!(got.join(" "), zero, "{case}");
        assert_eq!(
            v0["time_label"], "cycle 0 (before reset deasserts)",
            "{case}"
        );
        let v1 = reset_json("value", &path, &["--at", "1", "-s", "data"], mode);
        assert_eq!(v1["sample_tick"], read1, "{case}");
        assert_ne!(v0["sample_tick"], v1["sample_tick"], "{case}");
        let w = reset_json(
            "wave",
            &path,
            &["-t", "0:2", "-s", "data", "-s", "rstn", "-s", "p"],
            mode,
        );
        assert_eq!(strings(&w["columns"]), ["0", "1", "2"], "{case}");
        assert_eq!(
            w["ticks"],
            serde_json::json!([
                reset_at,
                if on_edge { 15 } else { 25 },
                if on_edge { 25 } else { 35 }
            ]),
            "{case}"
        );
        let rendered: Vec<String> = w["values"]
            .as_array()
            .unwrap()
            .iter()
            .map(|r| format!("[{}]", strings(r).join(" ")))
            .collect();
        assert_eq!(rendered.join(" "), rows_wave, "{case}");
        // wave and find agree on cycle 0: rstn is 0 there and nowhere else.
        let f = reset_json("find", &path, &["rstn", "0", "-t", "0:2"], mode);
        let expected: Vec<u64> = if on_edge && pre { vec![0, 1] } else { vec![0] };
        assert_eq!(match_times(&f), expected, "{case}");
        let s = reset_text(
            "sample",
            &path,
            &["rstn", "0", "-s", "data", "-t", "0:2"],
            mode,
        );
        let lines: Vec<&str> = s.lines().collect();
        let data0 = zero.split(' ').next().unwrap();
        let mut expected_lines = vec![format!("0 data {data0}")];
        if on_edge && pre {
            expected_lines.push("1 data 0".into());
        }
        assert_eq!(lines, expected_lines, "{case}");
        let d = reset_json(
            "distance",
            &path,
            &["rstn", "0", "--to", "data", "'h3"],
            mode,
        );
        let want: Vec<(u64, u64)> = match (on_edge, pre) {
            (true, false) => vec![(0, 3)],
            (true, true) => vec![(0, 4), (1, 4)],
            (false, false) => vec![(0, 2)],
            (false, true) => vec![(0, 3)],
        };
        assert_eq!(pair_list(&d), want, "{case}");
        let t = reset_text(
            "signal",
            &path,
            &["-s", "data", "-s", "rstn", "-t", "0:2"],
            mode,
        );
        let want_signal = match (on_edge, pre) {
            (true, false) => "0 data 0\n0 rstn 0\n1 data 1\n1 rstn 1\n2 data 2\n",
            (true, true) => "0 data 0\n0 rstn 0\n2 data 1\n2 rstn 1\n",
            (false, false) => "0 data 1\n0 rstn 0\n1 data 2\n1 rstn 1\n2 data 3\n",
            (false, true) => "0 data 1\n0 rstn 0\n1 rstn 1\n2 data 2\n",
        };
        assert_eq!(t, want_signal, "{case}");
        let df = reset_text("diff", &path, &["0", "1", "-s", "data", "-s", "rstn"], mode);
        if on_edge && pre {
            assert!(df.contains("no differences"), "{case}: {df}");
        } else {
            assert!(df.contains("@0=0") && df.contains("@1=1"), "{case}: {df}");
        }
        assert_eq!(df.contains("data"), !pre, "{case}: {df}");
        let tok = reset_json("value", &path, &["--at", "14t", "-s", "data"], mode);
        assert_eq!(tok["at"], 0, "{case}");
        assert_eq!(tok["sample_tick"], read0, "{case}");
        let st = reset_json("stats", &path, &["-s", "data", "-t", "0:1"], mode);
        let occupancy = &st["signals"][0]["time_in_state_clocked_cycles"];
        // Cycles 0 and 1 of the window, as `wave -t 0:1` shows them.
        let (key0, key1) = match (on_edge, pre) {
            (true, false) => ("'h0", "'h1"),
            (true, true) => ("'h0", "'h0"),
            (false, false) => ("'h1", "'h2"),
            (false, true) => ("'h1", "'h1"),
        };
        let mut want = serde_json::Map::new();
        for key in [key0, key1] {
            let n = want.get(key).map_or(0, |n| n.as_u64().unwrap());
            want.insert(key.into(), (n + 1).into());
        }
        assert_eq!(st["total_cycles"], 2, "{case}");
        assert_eq!(occupancy, &serde_json::Value::Object(want), "{case}");
        // `p` rises at 14, so a consumer that kept reading tick 14 for cycle 0
        // on the on-edge trace is wrong in pre-edge mode.
        let pv = reset_json("value", &path, &["--at", "0", "-s", "p"], mode);
        assert_eq!(
            pv["signals"][0]["value"],
            if on_edge && pre { "0" } else { "1" },
            "{case}"
        );
        let pf = reset_json("find", &path, &["p", "0", "-t", "0:2"], mode);
        assert_eq!(
            match_times(&pf),
            if on_edge && pre { vec![0] } else { vec![] },
            "{case}"
        );
        let pd = reset_json("distance", &path, &["p", "0", "--to", "data", "'h3"], mode);
        assert_eq!(
            pair_list(&pd),
            if on_edge && pre { vec![(0, 4)] } else { vec![] },
            "{case}"
        );
        let ps = reset_text("signal", &path, &["-s", "p", "-t", "0:2"], mode);
        assert_eq!(
            ps,
            if on_edge && pre {
                "0 p 0\n1 p 1\n"
            } else {
                "0 p 1\n"
            },
            "{case}"
        );
    }
}

#[test]
fn cycle_zero_is_omitted_without_an_earlier_tick() {
    // --with-reset starts at tick 0, so no tick precedes cycle 0.
    let (_dir, path, _) = store(&reset_body(15), RESET_HEADER);
    let v = json("value", &path, &["--at", "0", "-s", "data", "--with-reset"])["data"].clone();
    assert!(v["sample_tick"].is_null(), "{v}");
    assert_eq!(v["target_tick"], 0);
    assert_eq!(v["signals"][0]["value"], "x");
    assert_eq!(
        v["time_label"],
        "cycle 0 (omitted: no tick before the start)"
    );
    let w = json("wave", &path, &["-t", "0:2", "-s", "data", "--with-reset"])["data"].clone();
    assert_eq!(strings(&w["columns"]), ["1", "2"]);
    let f = json("find", &path, &["rstn", "0", "-t", "0:2", "--with-reset"])["data"].clone();
    assert_eq!(match_times(&f), [1]);
    // The clock first rises at tick 1, together with reset: cycle 1 reads
    // tick 0 in pre-edge mode, leaving nothing for cycle 0.
    let body = "#0\n0!\n1\"\nb0000 #\n0$\n0%\n#1\n1!\n1$\n#2\n0!\n#11\n1!\n#12\n0!\n#21\n1!\n#22\n0!\n#30\n";
    let (_dir, path, _) = store(body, RESET_HEADER);
    let post = json("value", &path, &["--at", "0", "-s", "rstn"])["data"].clone();
    assert_eq!(post["sample_tick"], 0);
    let pre = json(
        "value",
        &path,
        &["--at", "0", "-s", "rstn", "--sample-mode", "pre-edge"],
    )["data"]
        .clone();
    assert!(pre["sample_tick"].is_null(), "{pre}");
    assert_eq!(
        pre["time_label"],
        "cycle 0 (omitted: no tick before the start)"
    );
}
