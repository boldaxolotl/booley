//! Property-based VCD → FST → query round-trip.
//!
//! Generates random VCDs, builds the FST store in-process via
//! `FstBuildHandler`, and asserts `ColumnCache` reports exactly the
//! transition streams and point values the VCD implies, computed by an
//! independent model of the canonical value rules (IEEE 1364 left-extension,
//! binary→hex formatting, minimal x/z form).
//!
//! The generator never emits a change to the value a signal already holds:
//! FST dedups same-value rewrites at write time, so they are invisible to
//! the store by design.
//!
//! (Older format/signal property tests live in #[cfg(test)] blocks inside
//! src/format.rs and src/signal.rs.)

use std::io::BufReader;
use std::sync::atomic::{AtomicUsize, Ordering};

use proptest::prelude::*;

use bwave::cache::ColumnCache;
use bwave::fst::FstBuildHandler;
use bwave::parser::parse_header;

static CASE_COUNTER: AtomicUsize = AtomicUsize::new(0);

#[derive(Debug, Clone)]
struct SigSpec {
    width: u32,
}

#[derive(Debug, Clone)]
struct VcdCase {
    signals: Vec<SigSpec>,
    /// (timestamp, signal index, value chars) — timestamps strictly increasing
    /// across groups; values never repeat the signal's current value.
    events: Vec<(u64, usize, String)>,
}

fn value_strategy(width: u32) -> impl Strategy<Value = String> {
    // Bit chars over 0/1/x/z; short values are legal VCD (left-extended).
    let len = 1..=(width as usize);
    (
        len,
        proptest::collection::vec(
            prop_oneof![
                Just('0'),
                Just('1'),
                Just('0'),
                Just('1'), // bias towards binary
                Just('x'),
                Just('z'),
            ],
            1..=width as usize,
        ),
    )
        .prop_map(|(l, chars)| chars.into_iter().take(l.max(1)).collect())
}

/// Width-extend a VCD value per IEEE 1364: fill with x/z when the MSB is
/// x/z, else with 0.
fn extend(v: &str, width: u32) -> String {
    let w = width as usize;
    let mut full = String::new();
    if v.len() < w {
        let fill = match v.as_bytes()[0] {
            b'x' => 'x',
            b'z' => 'z',
            _ => '0',
        };
        for _ in 0..(w - v.len()) {
            full.push(fill);
        }
    }
    full.push_str(v);
    full
}

/// The canonical value string the store reports for a full-width bit value:
/// pure binary converts to leading-zero-stripped uppercase hex; values with
/// x/z stay bit text reduced to the minimal VCD form (leading same-char x/z
/// run collapses to one; leading 0-padding drops, keeping one 0 when the
/// first significant char is x/z).
fn model_canon(full: &str) -> String {
    if full.len() <= 1 {
        return full.to_string();
    }
    if !full.contains('x') && !full.contains('z') {
        let n = u64::from_str_radix(full, 2).unwrap();
        return format!("{:X}", n);
    }
    let b = full.as_bytes();
    let first = b[0];
    if first == b'x' || first == b'z' {
        let mut start = 0;
        while start + 1 < b.len() && b[start + 1] == first {
            start += 1;
        }
        return full[start..].to_string();
    }
    if first == b'0' {
        let mut start = 0;
        while start + 1 < b.len() && b[start] == b'0' {
            start += 1;
        }
        if (b[start] == b'x' || b[start] == b'z') && start > 0 {
            start -= 1; // keep one zero: the fill char is significant
        }
        return full[start..].to_string();
    }
    full.to_string()
}

fn vcd_case_strategy() -> impl Strategy<Value = VcdCase> {
    let sigs = proptest::collection::vec((1u32..=40).prop_map(|width| SigSpec { width }), 1..=5);
    sigs.prop_flat_map(|signals| {
        let n = signals.len();
        let widths: Vec<u32> = signals.iter().map(|s| s.width).collect();
        let events = proptest::collection::vec(
            (0u64..500, 0..n).prop_flat_map(move |(dt, sig)| {
                value_strategy(widths[sig]).prop_map(move |v| (dt, sig, v))
            }),
            0..40,
        );
        let initial = proptest::collection::vec(prop_oneof![Just('0'), Just('1'), Just('x')], n);
        (Just(signals), initial, events).prop_map(|(signals, initial, raw)| {
            // Every signal gets a $dumpvars-style initial value at #0 (like
            // every real simulator dump). Without it, a never-written signal
            // hits the known, accepted empty-dump delta: the FST frame
            // materializes an implicit x at t=0 that the VCD never wrote.
            let mut t = 0u64;
            let mut current: Vec<Option<String>> = vec![None; signals.len()];
            let mut events = Vec::new();
            for (sig, ch) in initial.iter().enumerate() {
                let v = ch.to_string();
                current[sig] = Some(extend(&v, signals[sig].width));
                events.push((0u64, sig, v));
            }
            for (dt, sig, v) in raw {
                t += 1 + dt;
                let c = extend(&v, signals[sig].width);
                if current[sig].as_deref() == Some(c.as_str()) {
                    continue; // would be a same-value rewrite
                }
                current[sig] = Some(c);
                events.push((t, sig, v));
            }
            // Ensure at least one advancing timestep: a VCD whose only
            // content is the #0 initial dump is the known degenerate
            // "crashed before the first timestep" case — fst-writer only
            // materializes the initial frame at the first advancing
            // time_change, so those values are lost (accepted delta,
            // same class as the skipped test_empty_sim fixture).
            if events.len() == signals.len() {
                let one = extend("1", signals[0].width);
                let flip = if current[0].as_deref() == Some(one.as_str()) {
                    "0"
                } else {
                    "1"
                };
                current[0] = Some(extend(flip, signals[0].width));
                events.push((1, 0, flip.to_string()));
            }
            VcdCase { signals, events }
        })
    })
}

fn render_vcd(case: &VcdCase) -> String {
    let mut out = String::from("$timescale 1ns $end\n$scope module tb $end\n");
    for (i, s) in case.signals.iter().enumerate() {
        let id = (b'!' + i as u8) as char;
        if s.width == 1 {
            out.push_str(&format!("$var wire 1 {} sig{} $end\n", id, i));
        } else {
            out.push_str(&format!(
                "$var wire {} {} sig{} [{}:0] $end\n",
                s.width,
                id,
                i,
                s.width - 1
            ));
        }
    }
    out.push_str("$upscope $end\n$enddefinitions $end\n");
    let mut last_t: Option<u64> = None;
    for (t, sig, v) in &case.events {
        if last_t != Some(*t) {
            out.push_str(&format!("#{}\n", t));
            last_t = Some(*t);
        }
        let id = (b'!' + *sig as u8) as char;
        if case.signals[*sig].width == 1 && v.len() == 1 {
            out.push_str(&format!("{}{}\n", v, id));
        } else {
            out.push_str(&format!("b{} {}\n", v, id));
        }
    }
    out
}

/// Per-signal expected transition streams in canonical store form.
fn model_transitions(case: &VcdCase) -> Vec<Vec<(u64, String)>> {
    let mut out: Vec<Vec<(u64, String)>> = vec![Vec::new(); case.signals.len()];
    for (t, sig, v) in &case.events {
        let canon = model_canon(&extend(v, case.signals[*sig].width));
        out[*sig].push((*t, canon));
    }
    out
}

fn build_fst(vcd_text: &str) -> (ColumnCache, std::path::PathBuf) {
    let case = CASE_COUNTER.fetch_add(1, Ordering::Relaxed);
    let dir = std::env::temp_dir().join(format!("bwave_prop_{}_{}", std::process::id(), case));
    std::fs::create_dir_all(&dir).unwrap();
    let fst_path = dir.join("t.fst");

    let mut reader = BufReader::new(vcd_text.as_bytes());
    let header = parse_header(&mut reader);
    let mut h = FstBuildHandler::new(&header, None, &fst_path).unwrap();
    h.parse_bytes(&mut reader, None);
    h.finalize_and_write();

    let cache = ColumnCache::load_from_file(&fst_path).expect("load .fst");
    (cache, dir)
}

proptest! {
    #![proptest_config(ProptestConfig {
        cases: 48,
        .. ProptestConfig::default()
    })]

    #[test]
    fn random_vcd_roundtrips_identically(case in vcd_case_strategy()) {
        let vcd_text = render_vcd(&case);
        let (cache, dir) = build_fst(&vcd_text);
        let expected = model_transitions(&case);

        prop_assert_eq!(cache.signals.len(), case.signals.len(), "directory size");
        for i in 0..cache.signals.len() {
            let want_name = if case.signals[i].width == 1 {
                format!("tb.sig{}", i)
            } else {
                format!("tb.sig{}[{}:0]", i, case.signals[i].width - 1)
            };
            prop_assert_eq!(&cache.signals[i].name, &want_name, "name order");
            prop_assert_eq!(cache.signals[i].width, case.signals[i].width, "width");

            let got = cache.read_transitions(i);
            prop_assert_eq!(&got, &expected[i], "transitions of {} differ\nvcd:\n{}",
                &cache.signals[i].name, &vcd_text);

            // point queries at range boundaries and midpoints
            let probes = [0, cache.sim_end_tick / 2, cache.sim_end_tick];
            for t in probes {
                let want = expected[i]
                    .iter()
                    .take_while(|(et, _)| *et <= t)
                    .last()
                    .map(|(_, v)| v.clone())
                    .unwrap_or_else(|| "x".to_string());
                let got = cache.value_at_tick_direct(i, t);
                prop_assert_eq!(got, want, "value_at({}) of {} differs\nvcd:\n{}",
                    t, &cache.signals[i].name, &vcd_text);
            }
        }
        let last_t = case.events.iter().map(|(t, _, _)| *t).max().unwrap_or(0);
        prop_assert_eq!(cache.sim_end_tick, last_t, "sim_end");

        drop(cache);
        let _ = std::fs::remove_dir_all(dir);
    }
}
