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
    let p = json("stats", &path, &["-s", "valid", "-t", "1:6"]);
    assert_eq!(
        p["data"]["signals"][0]["time_in_state_clocked_cycles"]["'h1"],
        3
    );
    assert_eq!(
        p["data"]["signals"][0]["time_in_state_stopped_cycles"]["'h1"],
        3
    );
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
        }
    }
    for unknown in ['x', 'z'] {
        let body = clock(&[5, 15, 25, 35], 50).replace("#26\n0!", &format!("#26\n{unknown}!"));
        let (_dir, path, _) = store(&body, "");
        let p = json("overview", &path, &[]);
        assert_eq!(p["clock_regularity"]["status"], "warning");
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
    let p = json("overview", &path, &["--clock", "slow_clk"]);
    assert_eq!(p["data"]["period_ticks"], 20);
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
