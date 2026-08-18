//! End-to-end throughput benchmarks: VCD parsing, FST store build, queries.
//!
//! Generates in-memory VCD data at various scales and measures:
//! - Header parsing throughput
//! - Streaming event parsing throughput
//! - Store build (full VCD -> .fst)
//! - Store read + query (transition decode, grid build, virtual signals)

use std::fs;
use std::io::{BufReader, Cursor, Write};

use criterion::{black_box, criterion_group, criterion_main, BenchmarkId, Criterion, Throughput};
use rustc_hash::FxHashSet;

use bwave::cache::ColumnCache;
use bwave::format::{format_value_with_radix, Radix};
use bwave::fst::FstBuildHandler;
use bwave::parser::{parse_header, parse_streaming, VcdHandler};
use bwave::virtual_signal::{build_virtual_transitions, parse_virtual_def, resolve_virtual};

// -- VCD generator -----------------------------------------------------------

fn gen_vcd(num_signals: usize, num_cycles: usize) -> Vec<u8> {
    let mut buf = Vec::with_capacity(num_cycles * num_signals * 20);
    write!(buf, "$timescale 1ns $end\n").unwrap();
    write!(buf, "$scope module tb $end\n").unwrap();
    write!(buf, "$scope module dut $end\n").unwrap();

    // Signal IDs: use multi-char to be realistic for large signal counts
    let ids: Vec<String> = (0..num_signals)
        .map(|i| {
            if i < 94 {
                String::from((33 + i as u8) as char)
            } else {
                format!(
                    "{}{}",
                    (33 + (i / 94) as u8) as char,
                    (33 + (i % 94) as u8) as char
                )
            }
        })
        .collect();

    // Signal 0 = clk (1-bit), signal 1 = rstn (1-bit), rest = 8-bit data
    write!(buf, "$var wire 1 {} clk $end\n", ids[0]).unwrap();
    if num_signals > 1 {
        write!(buf, "$var wire 1 {} rstn $end\n", ids[1]).unwrap();
    }
    for i in 2..num_signals {
        write!(buf, "$var wire 8 {} sig_{:03} [7:0] $end\n", ids[i], i).unwrap();
    }

    write!(buf, "$upscope $end\n$upscope $end\n").unwrap();
    write!(buf, "$enddefinitions $end\n").unwrap();

    // Initial values
    write!(buf, "#0\n").unwrap();
    write!(buf, "0{}\n", ids[0]).unwrap();
    if num_signals > 1 {
        write!(buf, "0{}\n", ids[1]).unwrap();
    }
    for i in 2..num_signals {
        write!(buf, "b00000000 {}\n", ids[i]).unwrap();
    }

    let mut tick = 0u64;
    let mut counter = 0u8;

    for cycle in 0..num_cycles {
        // Rising edge
        tick += 5;
        write!(buf, "#{}\n1{}\n", tick, ids[0]).unwrap();

        // Deassert reset at cycle 3
        if cycle == 2 && num_signals > 1 {
            write!(buf, "1{}\n", ids[1]).unwrap();
        }

        // Data changes after reset
        if cycle >= 3 {
            counter = counter.wrapping_add(1);
            // Change ~1/3 of data signals each cycle (realistic activity)
            for i in 2..num_signals {
                if (cycle + i) % 3 == 0 {
                    let val = counter.wrapping_add(i as u8);
                    write!(buf, "b{:08b} {}\n", val, ids[i]).unwrap();
                }
            }
        }

        // Falling edge
        tick += 5;
        write!(buf, "#{}\n0{}\n", tick, ids[0]).unwrap();
    }

    buf
}

// -- Null handler (measures pure parsing overhead) ---------------------------

struct NullHandler {
    timestamp_count: u64,
    scalar_count: u64,
    vector_count: u64,
}

impl NullHandler {
    fn new() -> Self {
        NullHandler {
            timestamp_count: 0,
            scalar_count: 0,
            vector_count: 0,
        }
    }
}

impl VcdHandler for NullHandler {
    fn on_timestamp(&mut self, _time: u64) -> std::ops::ControlFlow<()> {
        self.timestamp_count += 1;
        std::ops::ControlFlow::Continue(())
    }
    fn on_time_update(&mut self, _time: u64, _byte_offset: u64) {}
    fn on_scalar(&mut self, _id: &str, _value: u8) {
        self.scalar_count += 1;
    }
    fn on_vector(&mut self, _id: &str, _bits: &str) {
        self.vector_count += 1;
    }
}

// -- Benchmarks --------------------------------------------------------------

fn bench_parse_header(c: &mut Criterion) {
    let mut group = c.benchmark_group("parse_header");

    for num_signals in [10, 100, 500] {
        let vcd = gen_vcd(num_signals, 10);
        group.throughput(Throughput::Bytes(vcd.len() as u64));
        group.bench_with_input(
            BenchmarkId::from_parameter(format!("{num_signals}sig")),
            &vcd,
            |b, vcd| {
                b.iter(|| {
                    let mut reader = BufReader::new(Cursor::new(black_box(vcd)));
                    parse_header(&mut reader)
                });
            },
        );
    }

    group.finish();
}

fn bench_parse_streaming(c: &mut Criterion) {
    let mut group = c.benchmark_group("parse_streaming");
    group.sample_size(20);

    for (num_signals, num_cycles) in [(10, 1_000), (10, 10_000), (50, 10_000), (100, 10_000)] {
        let vcd = gen_vcd(num_signals, num_cycles);
        let label = format!("{num_signals}sig_{num_cycles}cyc");

        group.throughput(Throughput::Bytes(vcd.len() as u64));
        group.bench_with_input(BenchmarkId::from_parameter(&label), &vcd, |b, vcd| {
            b.iter(|| {
                let mut reader = BufReader::new(Cursor::new(black_box(vcd)));
                let header = parse_header(&mut reader);
                let all_ids: FxHashSet<String> =
                    header.signals.iter().map(|s| s.id.clone()).collect();
                let mut handler = NullHandler::new();
                parse_streaming(&mut reader, &all_ids, &mut handler);
                handler.timestamp_count
            });
        });
    }

    group.finish();
}

fn bench_bwave_build(c: &mut Criterion) {
    let mut group = c.benchmark_group("fst_build");
    group.sample_size(10);

    for (num_signals, num_cycles) in [(10, 1_000), (10, 10_000), (50, 10_000)] {
        let vcd = gen_vcd(num_signals, num_cycles);
        let label = format!("{num_signals}sig_{num_cycles}cyc");

        group.throughput(Throughput::Bytes(vcd.len() as u64));
        group.bench_with_input(BenchmarkId::from_parameter(&label), &vcd, |b, vcd| {
            let tmp_dir = std::env::temp_dir();
            let store_path = tmp_dir.join("bench_test.fst");
            b.iter(|| {
                let mut reader = BufReader::new(Cursor::new(black_box(vcd)));
                let header = parse_header(&mut reader);
                let mut handler = FstBuildHandler::new(&header, None, &store_path).unwrap();
                handler.parse_bytes(&mut reader, None);
                handler.finalize_and_write();
            });
            let _ = fs::remove_file(&store_path);
        });
    }

    group.finish();
}

fn bench_bwave_query(c: &mut Criterion) {
    let mut group = c.benchmark_group("fst_query");
    group.sample_size(20);

    // Build a cache to query against
    let vcd = gen_vcd(50, 10_000);
    let tmp_dir = std::env::temp_dir();
    let bwave_path = tmp_dir.join("bench_query.fst");

    {
        let mut reader = BufReader::new(Cursor::new(&vcd));
        let header = parse_header(&mut reader);
        let mut handler = FstBuildHandler::new(&header, None, &bwave_path).unwrap();
        handler.parse_bytes(&mut reader, None);
        handler.finalize_and_write();
    }

    let cache = ColumnCache::load_from_file(&bwave_path).expect("cache should load");

    // Benchmark read_transitions (the core decode step for wave queries)
    {
        let matched = cache.match_signals(&["*".to_string()]);
        group.bench_function("read_transitions_all_50sig", |b| {
            b.iter(|| {
                let mut total = 0usize;
                for &i in &matched {
                    let t = cache.read_transitions(black_box(i));
                    total += t.len();
                }
                total
            });
        });
    }

    // Benchmark value_at_tick via binary search (simulates wave grid build)
    {
        let matched = cache.match_signals(&["*".to_string()]);
        let all_transitions: Vec<Vec<(u64, String)>> =
            matched.iter().map(|&i| cache.read_transitions(i)).collect();
        // 100-cycle window starting at cycle 5000
        let ticks: Vec<u64> = (5000..5100)
            .map(|c| cache.first_rise_tick + c * cache.clock_period_ticks)
            .collect();

        group.bench_function("value_at_tick_50sig_100cyc", |b| {
            b.iter(|| {
                let mut count = 0usize;
                for trans in black_box(&all_transitions) {
                    for &t in &ticks {
                        let _ = trans.binary_search_by_key(&t, |(tick, _)| *tick);
                        count += 1;
                    }
                }
                count
            });
        });

        // Full 10k cycle window
        let ticks_full: Vec<u64> = (0..10_000)
            .map(|c| cache.first_rise_tick + c * cache.clock_period_ticks)
            .collect();

        group.bench_function("value_at_tick_50sig_10kcyc", |b| {
            b.iter(|| {
                let mut count = 0usize;
                for trans in black_box(&all_transitions) {
                    for &t in &ticks_full {
                        let _ = trans.binary_search_by_key(&t, |(tick, _)| *tick);
                        count += 1;
                    }
                }
                count
            });
        });
    }

    let _ = fs::remove_file(&bwave_path);
    group.finish();
}

fn bench_bwave_query_large(c: &mut Criterion) {
    let mut group = c.benchmark_group("fst_query_large");
    group.sample_size(10);

    // Larger dataset: 200 signals, 50k cycles
    let vcd = gen_vcd(200, 50_000);
    let tmp_dir = std::env::temp_dir();
    let bwave_path = tmp_dir.join("bench_query_large.fst");

    {
        let mut reader = BufReader::new(Cursor::new(&vcd));
        let header = parse_header(&mut reader);
        let mut handler = FstBuildHandler::new(&header, None, &bwave_path).unwrap();
        handler.parse_bytes(&mut reader, None);
        handler.finalize_and_write();
    }

    let cache = ColumnCache::load_from_file(&bwave_path).expect("cache should load");

    // Decode all transitions for 200 signals
    {
        let matched = cache.match_signals(&["*".to_string()]);
        group.bench_function("read_transitions_200sig", |b| {
            b.iter(|| {
                let mut total = 0usize;
                for &i in &matched {
                    let t = cache.read_transitions(black_box(i));
                    total += t.len();
                }
                total
            });
        });

        // Grid build: 200 signals x 100 cycles (typical interactive query)
        let all_transitions: Vec<Vec<(u64, String)>> =
            matched.iter().map(|&i| cache.read_transitions(i)).collect();
        let ticks: Vec<u64> = (25_000..25_100)
            .map(|c| cache.first_rise_tick + c * cache.clock_period_ticks)
            .collect();

        group.bench_function("grid_200sig_100cyc", |b| {
            b.iter(|| {
                let mut count = 0usize;
                for trans in black_box(&all_transitions) {
                    for &t in &ticks {
                        let _ = trans.binary_search_by_key(&t, |(tick, _)| *tick);
                        count += 1;
                    }
                }
                count
            });
        });

        // Grid build: 200 signals x full 50k range
        let ticks_full: Vec<u64> = (0..50_000)
            .map(|c| cache.first_rise_tick + c * cache.clock_period_ticks)
            .collect();
        group.bench_function("grid_200sig_50kcyc", |b| {
            b.iter(|| {
                let mut count = 0usize;
                for trans in black_box(&all_transitions) {
                    for &t in &ticks_full {
                        let _ = trans.binary_search_by_key(&t, |(tick, _)| *tick);
                        count += 1;
                    }
                }
                count
            });
        });
    }

    let _ = fs::remove_file(&bwave_path);
    group.finish();
}

fn bench_radix_formatting(c: &mut Criterion) {
    let mut group = c.benchmark_group("radix_formatting");

    group.bench_function("format_decimal_8bit", |b| {
        b.iter(|| format_value_with_radix(black_box("FF"), 8, Radix::Dec));
    });

    let hex256 = "F".repeat(64);
    group.bench_function("format_decimal_256bit", |b| {
        b.iter(|| format_value_with_radix(black_box(&hex256), 256, Radix::Dec));
    });

    group.bench_function("format_binary_8bit", |b| {
        b.iter(|| format_value_with_radix(black_box("FF"), 8, Radix::Bin));
    });

    group.finish();
}

fn bench_virtual_eval(c: &mut Criterion) {
    let mut group = c.benchmark_group("virtual_eval");
    group.sample_size(20);

    // Build cache from generated VCD
    let vcd = gen_vcd(50, 10_000);
    let tmp_dir = std::env::temp_dir();
    let bwave_path = tmp_dir.join("bench_virtual.fst");

    {
        let mut reader = BufReader::new(Cursor::new(&vcd));
        let header = parse_header(&mut reader);
        let mut handler = FstBuildHandler::new(&header, None, &bwave_path).unwrap();
        handler.parse_bytes(&mut reader, None);
        handler.finalize_and_write();
    }

    let cache = ColumnCache::load_from_file(&bwave_path).expect("cache should load");
    let signal_names: Vec<String> = cache.signals.iter().map(|s| s.name.clone()).collect();
    let signal_widths: Vec<u32> = cache.signals.iter().map(|s| s.width).collect();
    let all_transitions: Vec<Vec<(u64, String)>> = (0..cache.signals.len())
        .map(|i| cache.read_transitions(i))
        .collect();

    // NonZero baseline
    {
        let def = parse_virtual_def("v = *sig_002").unwrap();
        let resolved = resolve_virtual(&def, &signal_names, &signal_widths, &[]).unwrap();
        group.bench_function("nonzero_baseline", |b| {
            b.iter(|| {
                build_virtual_transitions(
                    black_box(&resolved),
                    &[],
                    &all_transitions,
                    cache.sim_start_tick,
                    cache.sim_end_tick,
                )
            });
        });
    }

    // GT comparison with value literal
    {
        let def = parse_virtual_def("v = *sig_002 > 'd80").unwrap();
        let resolved = resolve_virtual(&def, &signal_names, &signal_widths, &[]).unwrap();
        group.bench_function("gt_value", |b| {
            b.iter(|| {
                build_virtual_transitions(
                    black_box(&resolved),
                    &[],
                    &all_transitions,
                    cache.sim_start_tick,
                    cache.sim_end_tick,
                )
            });
        });
    }

    // SLICE + NonZero
    {
        let def = parse_virtual_def("v = *sig_002[7]").unwrap();
        let resolved = resolve_virtual(&def, &signal_names, &signal_widths, &[]).unwrap();
        group.bench_function("slice_nonzero", |b| {
            b.iter(|| {
                build_virtual_transitions(
                    black_box(&resolved),
                    &[],
                    &all_transitions,
                    cache.sim_start_tick,
                    cache.sim_end_tick,
                )
            });
        });
    }

    // SLICE + GT + value
    {
        let def = parse_virtual_def("v = *sig_002[7:4] > 'd8").unwrap();
        let resolved = resolve_virtual(&def, &signal_names, &signal_widths, &[]).unwrap();
        group.bench_function("slice_gt_value", |b| {
            b.iter(|| {
                build_virtual_transitions(
                    black_box(&resolved),
                    &[],
                    &all_transitions,
                    cache.sim_start_tick,
                    cache.sim_end_tick,
                )
            });
        });
    }

    // Signal-to-signal EQUAL
    {
        let def = parse_virtual_def("v = *sig_002 == *sig_003").unwrap();
        let resolved = resolve_virtual(&def, &signal_names, &signal_widths, &[]).unwrap();
        group.bench_function("sig_to_sig_equal", |b| {
            b.iter(|| {
                build_virtual_transitions(
                    black_box(&resolved),
                    &[],
                    &all_transitions,
                    cache.sim_start_tick,
                    cache.sim_end_tick,
                )
            });
        });
    }

    // Multi-atom AND with SLICE + comparison
    {
        let def = parse_virtual_def("v = (*sig_002[7]) & (*sig_003 > 'd40)").unwrap();
        let resolved = resolve_virtual(&def, &signal_names, &signal_widths, &[]).unwrap();
        group.bench_function("combined_slice_and_gt", |b| {
            b.iter(|| {
                build_virtual_transitions(
                    black_box(&resolved),
                    &[],
                    &all_transitions,
                    cache.sim_start_tick,
                    cache.sim_end_tick,
                )
            });
        });
    }

    let _ = fs::remove_file(&bwave_path);
    group.finish();
}

criterion_group!(
    benches,
    bench_parse_header,
    bench_parse_streaming,
    bench_bwave_build,
    bench_bwave_query,
    bench_bwave_query_large,
    bench_radix_formatting,
    bench_virtual_eval,
);
criterion_main!(benches);
