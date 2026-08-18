//! Micro-benchmarks for VCD parser hot-path functions.

use criterion::{black_box, criterion_group, criterion_main, Criterion};

use bwave::format::format_value;

// -- format_value -------------------------------------------------------------

fn bench_format_value(c: &mut Criterion) {
    let mut group = c.benchmark_group("format_value");

    // 1-bit scalar
    group.bench_function("1bit", |b| b.iter(|| format_value(black_box("1"))));

    // 8-bit binary -> 2 hex chars
    let bits_8 = "10101010";
    group.bench_function("8bit", |b| b.iter(|| format_value(black_box(bits_8))));

    // 32-bit binary -> 8 hex chars
    let bits_32 = "10110011110011001010101011001100";
    group.bench_function("32bit", |b| b.iter(|| format_value(black_box(bits_32))));

    // 256-bit binary -> 64 hex chars
    let bits_256 = "1".repeat(128) + &"0".repeat(128);
    group.bench_function("256bit", |b| b.iter(|| format_value(black_box(&bits_256))));

    // x/z fallback (early exit)
    group.bench_function("32bit_xz", |b| {
        b.iter(|| format_value(black_box("1011001111001100x010101011001100")))
    });

    group.finish();
}

criterion_group!(benches, bench_format_value);
criterion_main!(benches);
