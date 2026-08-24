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
use rustc_hash::{FxHashMap, FxHashSet};

use bwave::cache::ColumnCache;
use bwave::format::{format_value_with_radix, Radix};
use bwave::fst::{scan_vcd_bytes, FstBuildHandler, VcdByteSink, VcdIdLookup};
use bwave::parser::{parse_header, parse_streaming, VcdHandler};
use bwave::vcd_chunk::VcdChunkSource;
use bwave::virtual_signal::{build_virtual_transitions, parse_virtual_def, resolve_virtual};

const VECTOR_WIDTHS: [usize; 6] = [1, 8, 32, 64, 256, 1_024];

#[derive(Clone, Copy)]
enum RepresentativeDialect {
    Verilator,
    Xcelium,
}

#[derive(Clone, Copy)]
enum RepresentativeShape {
    ScalarHeavy,
    WideVectorHeavy,
}

#[derive(Clone, Copy)]
struct RepresentativeWorkload {
    label: &'static str,
    signals: usize,
    cycles: usize,
    dialect: RepresentativeDialect,
    shape: RepresentativeShape,
}

const REPRESENTATIVE_WORKLOADS: [RepresentativeWorkload; 2] = [
    RepresentativeWorkload {
        label: "verilator_4000sig_scalar",
        signals: 4_000,
        cycles: 1_000,
        dialect: RepresentativeDialect::Verilator,
        shape: RepresentativeShape::ScalarHeavy,
    },
    RepresentativeWorkload {
        label: "xcelium_11000sig_wide",
        signals: 11_000,
        cycles: 400,
        dialect: RepresentativeDialect::Xcelium,
        shape: RepresentativeShape::WideVectorHeavy,
    },
];

// -- VCD generator -----------------------------------------------------------

fn gen_vcd(num_signals: usize, num_cycles: usize) -> Vec<u8> {
    let mut buf = Vec::with_capacity(num_cycles * num_signals * 20);
    write!(buf, "$timescale 1ns $end\n").unwrap();
    write!(buf, "$scope module tb $end\n").unwrap();
    write!(buf, "$scope module dut $end\n").unwrap();

    // Signal IDs: use multi-char to be realistic for large signal counts
    let ids: Vec<String> = (0..num_signals).map(numeric_vcd_id).collect();

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

fn numeric_vcd_id(mut value: usize) -> String {
    let mut bytes = Vec::with_capacity(3);
    loop {
        bytes.push(b'!' + (value % 94) as u8);
        value /= 94;
        if value == 0 {
            return String::from_utf8(bytes).unwrap();
        }
    }
}

fn gen_representative_vcd(workload: RepresentativeWorkload) -> Vec<u8> {
    let ids: Vec<String> = (0..workload.signals).map(numeric_vcd_id).collect();
    let widths: Vec<usize> = (0..workload.signals)
        .map(|signal| match workload.shape {
            RepresentativeShape::ScalarHeavy => 1,
            RepresentativeShape::WideVectorHeavy if signal == 0 || signal % 4 == 0 => 1,
            RepresentativeShape::WideVectorHeavy => 64,
        })
        .collect();
    let mut output = Vec::with_capacity(16 * 1024 * 1024);
    match workload.dialect {
        RepresentativeDialect::Verilator => writeln!(output, "$timescale 1ns $end").unwrap(),
        RepresentativeDialect::Xcelium => {
            writeln!(output, "$version\n  TOOL:\txmsim(64) synthetic\n$end").unwrap();
            writeln!(output, "$timescale\n    1 ns\n$end").unwrap();
        }
    }
    writeln!(output, "$scope module tb $end").unwrap();
    for signal in 0..workload.signals {
        let var_type = match (workload.dialect, signal) {
            (RepresentativeDialect::Xcelium, 1) => "parameter",
            (RepresentativeDialect::Xcelium, 2) => "integer",
            _ => "wire",
        };
        let name = match (workload.dialect, workload.shape, signal) {
            (_, _, 0) => "clk".to_string(),
            (RepresentativeDialect::Xcelium, RepresentativeShape::WideVectorHeavy, signal)
                if signal % 4 == 0 =>
            {
                format!("wide_bus [{signal}]")
            }
            _ => format!("sig_{signal}"),
        };
        writeln!(
            output,
            "$var {var_type} {} {} {name} $end",
            widths[signal], ids[signal]
        )
        .unwrap();
    }
    writeln!(output, "$upscope $end\n$enddefinitions $end\n#0").unwrap();
    for signal in 0..workload.signals {
        write_representative_value(
            &mut output,
            widths[signal],
            &ids[signal],
            0,
            signal,
            workload.dialect,
        );
    }

    let activity_divisor = match workload.shape {
        RepresentativeShape::ScalarHeavy => 3,
        RepresentativeShape::WideVectorHeavy => 32,
    };
    for cycle in 1..=workload.cycles {
        writeln!(output, "#{cycle}").unwrap();
        writeln!(
            output,
            "{}{id}",
            (b'0' + (cycle & 1) as u8) as char,
            id = ids[0]
        )
        .unwrap();
        for signal in 1..workload.signals {
            if (cycle + signal) % activity_divisor == 0 {
                write_representative_value(
                    &mut output,
                    widths[signal],
                    &ids[signal],
                    cycle,
                    signal,
                    workload.dialect,
                );
            }
        }
    }
    output
}

fn write_representative_value(
    output: &mut Vec<u8>,
    width: usize,
    id: &str,
    cycle: usize,
    signal: usize,
    dialect: RepresentativeDialect,
) {
    if width == 1 {
        writeln!(
            output,
            "{}{id}",
            (b'0' + ((cycle + signal) & 1) as u8) as char
        )
        .unwrap();
        return;
    }
    output.push(match dialect {
        RepresentativeDialect::Verilator => b'b',
        RepresentativeDialect::Xcelium => b'B',
    });
    for bit in 0..width {
        let mixed = cycle
            .wrapping_mul(31)
            .wrapping_add(signal.wrapping_mul(17))
            .wrapping_add(bit.wrapping_mul(13));
        output.push(b'0' + ((mixed >> (bit & 7)) & 1) as u8);
    }
    match dialect {
        RepresentativeDialect::Verilator => output.push(b' '),
        RepresentativeDialect::Xcelium => output.extend_from_slice(b"\t\t"),
    }
    output.extend_from_slice(id.as_bytes());
    output.push(b'\n');
}

fn gen_width_vcd(width: usize) -> Vec<u8> {
    const SIGNALS: usize = 64;
    const TARGET_BODY_BYTES: usize = 4 * 1024 * 1024;

    let ids: Vec<String> = (0..SIGNALS).map(numeric_vcd_id).collect();
    let line_bytes = width.max(1) + 8;
    let cycles = (TARGET_BODY_BYTES / (SIGNALS * line_bytes)).max(16);
    let mut buf = Vec::with_capacity(TARGET_BODY_BYTES + 16 * 1024);
    writeln!(buf, "$timescale 1ns $end").unwrap();
    writeln!(buf, "$scope module width_bench $end").unwrap();
    for (signal, id) in ids.iter().enumerate() {
        writeln!(buf, "$var wire {width} {id} sig_{signal} $end").unwrap();
    }
    writeln!(buf, "$upscope $end\n$enddefinitions $end\n#0").unwrap();
    for id in &ids {
        write_width_value(&mut buf, width, id, 0, 0);
    }
    for cycle in 1..=cycles {
        writeln!(buf, "#{cycle}").unwrap();
        for (signal, id) in ids.iter().enumerate() {
            write_width_value(&mut buf, width, id, cycle, signal);
        }
    }
    buf
}

fn write_width_value(output: &mut Vec<u8>, width: usize, id: &str, cycle: usize, signal: usize) {
    if width == 1 {
        writeln!(
            output,
            "{}{id}",
            (b'0' + ((cycle + signal) & 1) as u8) as char
        )
        .unwrap();
        return;
    }
    output.push(b'b');
    for bit in 0..width {
        let mixed = cycle
            .wrapping_mul(31)
            .wrapping_add(signal.wrapping_mul(17))
            .wrapping_add(bit.wrapping_mul(13));
        output.push(b'0' + ((mixed >> (bit & 7)) & 1) as u8);
    }
    output.push(b' ');
    output.extend_from_slice(id.as_bytes());
    output.push(b'\n');
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

#[derive(Default)]
struct ByteNullSink(u64);

impl VcdByteSink for ByteNullSink {
    fn timestamp(&mut self, tick: u64) {
        self.0 = self.0.wrapping_add(tick);
    }
    fn scalar(&mut self, id: &[u8], value: u8) {
        self.0 = self.0.wrapping_add(id.len() as u64 + value as u64);
    }
    fn vector(&mut self, id: &[u8], bits: &[u8]) {
        self.0 = self.0.wrapping_add((id.len() + bits.len()) as u64);
    }
    fn real(&mut self, id: &[u8], value: &[u8]) {
        self.0 = self.0.wrapping_add((id.len() + value.len()) as u64);
    }
}

struct BaselineLookup {
    single: [usize; 256],
    multi: FxHashMap<String, usize>,
    widths: Vec<usize>,
}

impl BaselineLookup {
    fn from_header(header: &bwave::parser::VcdHeader) -> Self {
        let mut lookup = Self {
            single: [usize::MAX; 256],
            multi: FxHashMap::default(),
            widths: Vec::new(),
        };
        for signal in &header.signals {
            if lookup.resolve(signal.id.as_bytes()).is_some() {
                continue;
            }
            let group = lookup.widths.len();
            lookup.widths.push(signal.width as usize);
            if signal.id.len() == 1 {
                lookup.single[signal.id.as_bytes()[0] as usize] = group;
            } else {
                lookup.multi.insert(signal.id.clone(), group);
            }
        }
        lookup
    }

    #[inline(always)]
    fn resolve(&self, id: &[u8]) -> Option<usize> {
        let group = if id.len() == 1 {
            self.single[id[0] as usize]
        } else {
            let id = std::str::from_utf8(id).ok()?;
            self.multi.get(id).copied().unwrap_or(usize::MAX)
        };
        (group != usize::MAX).then_some(group)
    }
}

struct LookupSink<'a> {
    lookup: &'a BaselineLookup,
    checksum: u64,
}

struct ProductionLookupSink<'a> {
    lookup: &'a VcdIdLookup,
    checksum: u64,
}

struct NormalizeSink<'a> {
    lookup: &'a BaselineLookup,
    current: Vec<Vec<u8>>,
    normalized: Vec<u8>,
    checksum: u64,
}

impl<'a> NormalizeSink<'a> {
    fn new(lookup: &'a BaselineLookup) -> Self {
        Self {
            lookup,
            current: lookup
                .widths
                .iter()
                .map(|&width| vec![b'x'; width])
                .collect(),
            normalized: Vec::with_capacity(256),
            checksum: 0,
        }
    }

    fn change(&mut self, id: &[u8], bits: &[u8]) {
        let Some(group) = self.lookup.resolve(id) else {
            return;
        };
        let width = self.lookup.widths[group];
        self.normalized.clear();
        if bits.len() < width {
            let fill = match bits.first().copied().unwrap_or(b'0').to_ascii_lowercase() {
                b'x' => b'x',
                b'z' => b'z',
                _ => b'0',
            };
            self.normalized.resize(width - bits.len(), fill);
        }
        let source = if bits.len() > width {
            &bits[bits.len() - width..]
        } else {
            bits
        };
        self.normalized
            .extend(source.iter().map(|byte| byte.to_ascii_lowercase()));
        if self.current[group] != self.normalized {
            self.current[group].copy_from_slice(&self.normalized);
            self.checksum = self.checksum.wrapping_add(group as u64 + 1);
        }
    }
}

impl VcdByteSink for NormalizeSink<'_> {
    fn timestamp(&mut self, tick: u64) {
        self.checksum = self.checksum.wrapping_add(tick);
    }
    fn scalar(&mut self, id: &[u8], value: u8) {
        self.change(id, &[value]);
    }
    fn vector(&mut self, id: &[u8], bits: &[u8]) {
        self.change(id, bits);
    }
    fn real(&mut self, id: &[u8], value: &[u8]) {
        self.change(id, value);
    }
}

impl LookupSink<'_> {
    fn change(&mut self, id: &[u8]) {
        self.checksum = self
            .checksum
            .wrapping_add(self.lookup.resolve(id).unwrap_or(usize::MAX) as u64);
    }
}

impl VcdByteSink for LookupSink<'_> {
    fn timestamp(&mut self, tick: u64) {
        self.checksum = self.checksum.wrapping_add(tick);
    }
    fn scalar(&mut self, id: &[u8], _value: u8) {
        self.change(id);
    }
    fn vector(&mut self, id: &[u8], _bits: &[u8]) {
        self.change(id);
    }
    fn real(&mut self, id: &[u8], _value: &[u8]) {
        self.change(id);
    }
}

impl ProductionLookupSink<'_> {
    fn change(&mut self, id: &[u8]) {
        self.checksum = self
            .checksum
            .wrapping_add(self.lookup.resolve(id).unwrap_or(u32::MAX) as u64);
    }
}

impl VcdByteSink for ProductionLookupSink<'_> {
    fn timestamp(&mut self, tick: u64) {
        self.checksum = self.checksum.wrapping_add(tick);
    }
    fn scalar(&mut self, id: &[u8], _value: u8) {
        self.change(id);
    }
    fn vector(&mut self, id: &[u8], _bits: &[u8]) {
        self.change(id);
    }
    fn real(&mut self, id: &[u8], _value: &[u8]) {
        self.change(id);
    }
}

fn production_lookup(header: &bwave::parser::VcdHeader) -> VcdIdLookup {
    let mut lookup = VcdIdLookup::new(header.signals.iter().map(|signal| signal.id.as_str()));
    let mut next_group = 0;
    for signal in &header.signals {
        if lookup.resolve(signal.id.as_bytes()).is_none() {
            lookup.insert(&signal.id, next_group);
            next_group += 1;
        }
    }
    lookup
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

fn bench_specialized_scanner(c: &mut Criterion) {
    let mut group = c.benchmark_group("specialized_scanner");
    group.sample_size(10);
    for workload in REPRESENTATIVE_WORKLOADS {
        let vcd = gen_representative_vcd(workload);
        let mut header_reader = BufReader::new(Cursor::new(&vcd));
        let header = parse_header(&mut header_reader);
        let lookup = BaselineLookup::from_header(&header);
        let production_lookup = production_lookup(&header);
        let marker = b"$enddefinitions $end\n";
        let start = vcd.windows(marker.len()).position(|w| w == marker).unwrap() + marker.len();
        let body = &vcd[start..];
        group.throughput(Throughput::Bytes(body.len() as u64));
        group.bench_with_input(
            BenchmarkId::new("null", workload.label),
            &body,
            |b, body| {
                b.iter(|| {
                    let mut r = BufReader::new(Cursor::new(black_box(*body)));
                    let mut sink = ByteNullSink::default();
                    scan_vcd_bytes(&mut r, &mut sink);
                    black_box(sink.0)
                });
            },
        );
        group.bench_with_input(
            BenchmarkId::new("lookup", workload.label),
            &body,
            |b, body| {
                b.iter(|| {
                    let mut r = BufReader::new(Cursor::new(black_box(*body)));
                    let mut sink = LookupSink {
                        lookup: &lookup,
                        checksum: 0,
                    };
                    scan_vcd_bytes(&mut r, &mut sink);
                    black_box(sink.checksum)
                });
            },
        );
        group.bench_with_input(
            BenchmarkId::new("production_lookup", workload.label),
            &body,
            |b, body| {
                b.iter(|| {
                    let mut r = BufReader::new(Cursor::new(black_box(*body)));
                    let mut sink = ProductionLookupSink {
                        lookup: &production_lookup,
                        checksum: 0,
                    };
                    scan_vcd_bytes(&mut r, &mut sink);
                    black_box(sink.checksum)
                });
            },
        );
        group.bench_with_input(
            BenchmarkId::new("normalize", workload.label),
            &body,
            |b, body| {
                b.iter(|| {
                    let mut r = BufReader::new(Cursor::new(black_box(*body)));
                    let mut sink = NormalizeSink::new(&lookup);
                    scan_vcd_bytes(&mut r, &mut sink);
                    black_box(sink.checksum)
                });
            },
        );
    }
    group.finish();
}

fn bench_vcd_chunk_source(c: &mut Criterion) {
    let vcd = gen_vcd(100, 100_000);
    let mut group = c.benchmark_group("vcd_chunk_source");
    group.sample_size(20);
    group.throughput(Throughput::Bytes(vcd.len() as u64));
    group.bench_function("1mib_timestamp_aligned", |b| {
        b.iter(|| {
            let mut source =
                VcdChunkSource::new(Cursor::new(black_box(vcd.as_slice())), 0, 1 << 20).unwrap();
            let mut bytes = 0usize;
            while let Some(chunk) = source.next_chunk().unwrap() {
                bytes += chunk.bytes.len();
                source.recycle(chunk);
            }
            black_box(bytes)
        });
    });
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
                handler.parse_bytes(&mut reader, None).unwrap();
                handler.finalize_and_write().unwrap();
            });
            let _ = fs::remove_file(&store_path);
        });
    }

    group.finish();
}

fn bench_bwave_build_representative(c: &mut Criterion) {
    let mut group = c.benchmark_group("fst_build_representative");
    group.sample_size(10);

    for workload in REPRESENTATIVE_WORKLOADS {
        let vcd = gen_representative_vcd(workload);
        let label = workload.label;
        group.throughput(Throughput::Bytes(vcd.len() as u64));
        group.bench_with_input(BenchmarkId::from_parameter(label), &vcd, |b, vcd| {
            let store_path = std::env::temp_dir().join(format!("bench_{label}.fst"));
            b.iter(|| {
                let mut reader = BufReader::new(Cursor::new(black_box(vcd)));
                let header = parse_header(&mut reader);
                let mut handler = FstBuildHandler::new(&header, None, &store_path).unwrap();
                handler.parse_bytes(&mut reader, None).unwrap();
                handler.finalize_and_write().unwrap();
            });
            let _ = fs::remove_file(&store_path);
        });
    }

    group.finish();
}

fn bench_bwave_build_widths(c: &mut Criterion) {
    let mut group = c.benchmark_group("fst_build_widths");
    group.sample_size(10);

    for width in VECTOR_WIDTHS {
        let vcd = gen_width_vcd(width);
        group.throughput(Throughput::Bytes(vcd.len() as u64));
        group.bench_with_input(BenchmarkId::from_parameter(width), &vcd, |b, vcd| {
            let store_path = std::env::temp_dir().join(format!("bench_{width}bit.fst"));
            b.iter(|| {
                let mut reader = BufReader::new(Cursor::new(black_box(vcd)));
                let header = parse_header(&mut reader);
                let mut handler = FstBuildHandler::new(&header, None, &store_path).unwrap();
                handler.parse_bytes(&mut reader, None).unwrap();
                handler.finalize_and_write().unwrap();
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
        handler.parse_bytes(&mut reader, None).unwrap();
        handler.finalize_and_write().unwrap();
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
        handler.parse_bytes(&mut reader, None).unwrap();
        handler.finalize_and_write().unwrap();
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
        handler.parse_bytes(&mut reader, None).unwrap();
        handler.finalize_and_write().unwrap();
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
    bench_specialized_scanner,
    bench_vcd_chunk_source,
    bench_bwave_build,
    bench_bwave_build_representative,
    bench_bwave_build_widths,
    bench_bwave_query,
    bench_bwave_query_large,
    bench_radix_formatting,
    bench_virtual_eval,
);
criterion_main!(benches);
