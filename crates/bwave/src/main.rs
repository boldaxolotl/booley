//! bwave — Booley Wave signal database CLI.
//! Build FST waveform stores from VCD streams and query them.
//!
//! v0.2 surface: clap subcommands. No bare-invocation fallback — every query
//! routes through an explicit subcommand (`bwave signal foo.fst -s sig`,
//! `bwave wave foo.fst -t 100:200`, …). Narrative and per-command help
//! moved to embedded docs (`bwave docs`) in Phase 3.

use std::fs::File;
use std::io::{self, BufReader};
use std::path::Path;
use std::process;

#[cfg(unix)]
use std::os::fd::AsRawFd;
#[cfg(unix)]
use std::os::unix::fs::FileTypeExt;

use clap::{Args, Parser, Subcommand, ValueEnum};

use bwave::cache::{
    diff_from_cache, distance_from_cache, find_stuck_from_cache, find_value_from_cache,
    list_signals_from_cache, sample_at_from_cache, snapshot_from_cache, stats_from_cache,
    trace_from_cache, wave_from_cache, ColumnCache,
};
use bwave::format::{is_edge_keyword, parse_radix_suffix, parse_verilog_literal};
use bwave::parser::try_parse_header;
use bwave::ExtractConfig;

// ===================================================================
//   CLI surface
// ===================================================================

#[derive(Parser, Debug)]
#[command(
    name = "bwave",
    version,
    about = "Query FST waveform stores built from VCD traces",
    long_about = include_str!("../docs/public/intro.md"),
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand, Debug)]
enum Command {
    /// Build an FST waveform store from VCD input
    #[command(long_about = include_str!("../docs/public/commands/build.md"))]
    Build(BuildArgs),
    /// List matching signal names (no values)
    #[command(long_about = include_str!("../docs/public/commands/list.md"))]
    List(ListArgs),
    /// Cycle-by-cycle signal trace (default query mode)
    #[command(long_about = include_str!("../docs/public/commands/signal.md"))]
    Signal(SignalArgs),
    /// Horizontal waveform table (rows=signals, cols=cycles)
    #[command(long_about = include_str!("../docs/public/commands/wave.md"))]
    Wave(WaveArgs),
    /// Snapshot all signals at a single time point
    #[command(long_about = include_str!("../docs/public/commands/value.md"))]
    Value(ValueArgs),
    /// Find cycles where PATTERN equals VALUE
    #[command(long_about = include_str!("../docs/public/commands/find.md"))]
    Find(FindArgs),
    /// Snapshot signals each time TRIGGER matches VALUE
    #[command(long_about = include_str!("../docs/public/commands/sample.md"))]
    Sample(SampleArgs),
    /// Compare signal values between two time points
    #[command(long_about = include_str!("../docs/public/commands/diff.md"))]
    Diff(DiffArgs),
    /// Measure time between events (same-signal or two-event A→B)
    #[command(long_about = include_str!("../docs/public/commands/distance.md"))]
    Distance(DistanceArgs),
    /// Per-signal transition counts and time-in-state
    #[command(long_about = include_str!("../docs/public/commands/stats.md"))]
    Stats(StatsArgs),
    /// Find signals stuck at a constant value
    #[command(long_about = include_str!("../docs/public/commands/stuck.md"))]
    Stuck(StuckArgs),
    /// Print the JSON Schema describing `--format json` envelopes
    Schema,
    /// Browse embedded narrative documentation
    Docs(DocsArgs),
    /// Print the agent skill markdown to stdout
    Skill,
}

#[derive(Args, Debug)]
struct DocsArgs {
    #[command(subcommand)]
    action: DocsAction,
}

#[derive(Subcommand, Debug)]
enum DocsAction {
    /// List every doc topic (relative paths, no .md extension)
    Topics,
    /// Substring-search across the corpus (case-insensitive)
    Search {
        #[arg(value_name = "QUERY")]
        query: String,
    },
    /// Print one topic to stdout
    Show {
        #[arg(value_name = "TOPIC")]
        topic: String,
    },
}

// -- Shared option groups -------------------------------------------

/// Global query options — flattened into every query subcommand's args.
#[derive(Args, Debug, Clone)]
struct GlobalOpts {
    /// Async mode: VCD timescale unit timestamps, every transition.
    /// Default (sync) samples at the rising clock edge per cycle.
    #[arg(long = "async")]
    async_mode: bool,

    /// Clock signal pattern for sync mode (default: auto *clk*)
    #[arg(long)]
    clock: Option<String>,

    /// Reset signal pattern for sync mode (default: auto *rst*)
    #[arg(long)]
    reset: Option<String>,

    /// Include reset phase in output (default: skip until reset deasserts)
    #[arg(long = "with-reset")]
    with_reset: bool,

    /// Output format: "text" (default) or "json"
    #[arg(long, default_value = "text", value_parser = ["text", "json"])]
    format: String,

    /// Max output lines (default: 2000)
    #[arg(long, default_value_t = 2000)]
    limit: usize,
}

impl Default for GlobalOpts {
    fn default() -> Self {
        Self {
            async_mode: false,
            clock: None,
            reset: None,
            with_reset: false,
            format: "text".to_string(),
            limit: 2000,
        }
    }
}

/// Options scoped to subcommands that consume virtual signals:
/// `wave`, `find`, `sample`, `distance`, `value`.
#[derive(Args, Debug, Clone, Default)]
struct VirtualOpts {
    /// Uniquely named virtual signal: boolean predicate over existing signals (repeatable).
    /// Format: "name = expr". Verilog-subset syntax: &, |, ^, ~, ==, !=, >, etc.
    #[arg(long = "virtual", action = clap::ArgAction::Append, value_name = "DEF")]
    virtual_defs: Vec<String>,
}

#[derive(Args, Debug, Clone, Default)]
struct MarkerOpts {
    /// Named marker for output: --marker NAME CYCLE (repeatable)
    #[arg(long = "marker", num_args = 2, action = clap::ArgAction::Append,
          value_names = &["NAME", "CYCLE"])]
    markers: Vec<String>,
}

// -- Per-subcommand argument structs --------------------------------

#[derive(Args, Debug)]
struct BuildArgs {
    /// VCD input path (use --input for FIFOs; stdin if neither given)
    #[arg(value_name = "VCD_FILE")]
    vcd_file: Option<String>,

    /// Output store path (.fst)
    #[arg(
        short = 'o',
        long = "output",
        value_name = "STORE_FILE",
        required = true
    )]
    output: String,

    /// Read VCD from this path (for named pipes / FIFOs needing blocking open)
    #[arg(long = "input", value_name = "PATH", conflicts_with = "vcd_file")]
    input: Option<String>,

    /// Limit build to signals within this hierarchical scope (e.g. "tb.dut")
    #[arg(long)]
    scope: Option<String>,

    /// Hidden converter selection for diagnostics and serial-oracle checks
    #[arg(long, value_enum, default_value_t = BuildEngine::Parallel, hide = true)]
    engine: BuildEngine,

    /// Temporary total worker-budget override
    #[arg(long, hide = true)]
    jobs: Option<usize>,

    /// Temporary parser worker-count override
    #[arg(long, hide = true)]
    parse_jobs: Option<usize>,

    /// Temporary section-encoder worker-count override
    #[arg(long, hide = true)]
    encode_jobs: Option<usize>,

    /// Temporary per-signal compression worker-count override
    #[arg(long, hide = true)]
    pack_jobs: Option<usize>,

    /// Temporary timestamp-aligned chunk-size override
    #[arg(long, hide = true)]
    chunk_bytes: Option<usize>,

    /// Temporary output-section coalescing override
    #[arg(long, hide = true)]
    section_bytes: Option<usize>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, ValueEnum)]
enum BuildEngine {
    Serial,
    Parallel,
}

#[derive(Args, Debug)]
struct ListArgs {
    /// Path to an FST waveform store
    #[arg(value_name = "STORE_FILE")]
    bwave: String,

    /// Signal glob pattern (repeatable)
    #[arg(short = 's', long = "signals", action = clap::ArgAction::Append,
          value_name = "PATTERN")]
    signals: Vec<String>,

    /// List only scopes (modules), no leaf signals
    #[arg(long)]
    tree: bool,

    #[command(flatten)]
    global: GlobalOpts,
}

#[derive(Args, Debug)]
struct SignalArgs {
    #[arg(value_name = "STORE_FILE")]
    bwave: String,

    /// Signal glob pattern (repeatable). Append %d/%b/%h for radix display.
    #[arg(short = 's', long = "signals", action = clap::ArgAction::Append,
          value_name = "PATTERN[%RADIX]")]
    signals: Vec<String>,

    /// Time range START:END (sync: cycles, async: timescale units)
    #[arg(short = 't', long = "time")]
    time: Option<String>,

    #[command(flatten)]
    marker: MarkerOpts,

    #[command(flatten)]
    global: GlobalOpts,
}

#[derive(Args, Debug)]
struct WaveArgs {
    #[arg(value_name = "STORE_FILE")]
    bwave: String,

    /// Stored or virtual signal row pattern (repeatable). Append %d/%b/%h for radix display.
    #[arg(short = 's', long = "signals", action = clap::ArgAction::Append,
          value_name = "PATTERN[%RADIX]")]
    signals: Vec<String>,

    /// Time range START:END (required for wave queries)
    #[arg(short = 't', long = "time")]
    time: Option<String>,

    /// Run-length-encode the wave: contiguous columns where every signal
    /// holds the same value collapse to `<val>Ã—N`, so a 5000-cycle wave
    /// of a quiet signal is one column instead of 5000.
    #[arg(long = "rle")]
    rle: bool,

    #[command(flatten)]
    virtuals: VirtualOpts,

    #[command(flatten)]
    marker: MarkerOpts,

    #[command(flatten)]
    global: GlobalOpts,
}

#[derive(Args, Debug)]
struct ValueArgs {
    #[arg(value_name = "STORE_FILE")]
    bwave: String,

    /// Snapshot at time point. Accepts cycle counts (sync mode, bare integer
    /// or `Nc`), simulation ticks (`Nt`), or physical time (`Nns`/`us`/`ms`/`ps`).
    /// In async mode a unit suffix is required — bare integers are rejected.
    ///
    /// `allow_hyphen_values` lets users pass negative cycles (`--at -5` or
    /// `--at=-5`) without clap interpreting the leading minus as a new flag.
    #[arg(long, value_name = "T", required = true, allow_hyphen_values = true)]
    at: String,

    /// Stored or virtual signal row pattern (repeatable). Append %d/%b/%h for radix display.
    #[arg(short = 's', long = "signals", action = clap::ArgAction::Append,
          value_name = "PATTERN[%RADIX]")]
    signals: Vec<String>,

    #[command(flatten)]
    virtuals: VirtualOpts,

    #[command(flatten)]
    marker: MarkerOpts,

    #[command(flatten)]
    global: GlobalOpts,
}

#[derive(Args, Debug)]
struct FindArgs {
    #[arg(value_name = "STORE_FILE")]
    bwave: String,

    /// Signal pattern to search
    #[arg(value_name = "PATTERN")]
    pattern: String,

    /// Value to match — Verilog literal ('d255, 'hFF, 'b1010, 8'd255) or
    /// edge keyword (rising, falling, change)
    #[arg(value_name = "VALUE")]
    value: String,

    /// Time range START:END
    #[arg(short = 't', long = "time")]
    time: Option<String>,

    /// Stop after first match
    #[arg(long)]
    first: bool,

    /// Return only the last match
    #[arg(long)]
    last: bool,

    /// Find before cycle/time N (implies --last, sets -t :N)
    #[arg(long, value_name = "N", value_parser = clap::value_parser!(i64))]
    before: Option<i64>,

    /// Find after cycle/time N (implies --first, sets -t N:)
    #[arg(long, value_name = "N", value_parser = clap::value_parser!(i64))]
    after: Option<i64>,

    /// Print only match count
    #[arg(long)]
    count: bool,

    #[command(flatten)]
    virtuals: VirtualOpts,

    #[command(flatten)]
    marker: MarkerOpts,

    #[command(flatten)]
    global: GlobalOpts,
}

#[derive(Args, Debug)]
struct SampleArgs {
    #[arg(value_name = "STORE_FILE")]
    bwave: String,

    /// Trigger signal pattern
    #[arg(value_name = "TRIGGER_PAT")]
    trigger: String,

    /// Trigger value or edge keyword
    #[arg(value_name = "TRIGGER_VAL")]
    value: String,

    /// Stored or virtual signal row pattern to snapshot (repeatable)
    #[arg(short = 's', long = "signals", action = clap::ArgAction::Append,
          value_name = "PATTERN[%RADIX]")]
    signals: Vec<String>,

    #[arg(short = 't', long = "time")]
    time: Option<String>,

    #[arg(long)]
    first: bool,
    #[arg(long)]
    last: bool,
    #[arg(long, value_name = "N", value_parser = clap::value_parser!(i64))]
    before: Option<i64>,
    #[arg(long, value_name = "N", value_parser = clap::value_parser!(i64))]
    after: Option<i64>,
    #[arg(long)]
    count: bool,

    #[command(flatten)]
    virtuals: VirtualOpts,

    #[command(flatten)]
    marker: MarkerOpts,

    #[command(flatten)]
    global: GlobalOpts,
}

#[derive(Args, Debug)]
struct DiffArgs {
    #[arg(value_name = "STORE_FILE")]
    bwave: String,

    /// First time point. Accepts cycle/tick/ns/us/ms/ps tokens — see
    /// `bwave docs show reference/time-tokens`.
    #[arg(value_name = "T1")]
    t1: String,

    /// Second time point. Same grammar as T1.
    #[arg(value_name = "T2")]
    t2: String,

    #[arg(short = 's', long = "signals", action = clap::ArgAction::Append,
          value_name = "PATTERN[%RADIX]")]
    signals: Vec<String>,

    #[command(flatten)]
    marker: MarkerOpts,

    #[command(flatten)]
    global: GlobalOpts,
}

#[derive(Args, Debug)]
struct DistanceArgs {
    #[arg(value_name = "STORE_FILE")]
    bwave: String,

    /// Start-event signal pattern
    #[arg(value_name = "PATTERN")]
    pattern: String,

    /// Start-event value
    #[arg(value_name = "VALUE")]
    value: String,

    /// End-event for two-event A→B latency: --to PATTERN VALUE
    #[arg(long = "to", num_args = 2, value_names = &["PATTERN", "VALUE"])]
    distance_to: Option<Vec<String>>,

    /// Summary statistics instead of raw pairs
    #[arg(long)]
    stats: bool,

    #[arg(short = 's', long = "signals", action = clap::ArgAction::Append,
          value_name = "PATTERN[%RADIX]")]
    signals: Vec<String>,

    #[arg(short = 't', long = "time")]
    time: Option<String>,

    #[command(flatten)]
    virtuals: VirtualOpts,

    #[command(flatten)]
    marker: MarkerOpts,

    #[command(flatten)]
    global: GlobalOpts,
}

#[derive(Args, Debug)]
struct StatsArgs {
    #[arg(value_name = "STORE_FILE")]
    bwave: String,

    #[arg(short = 's', long = "signals", action = clap::ArgAction::Append,
          value_name = "PATTERN[%RADIX]")]
    signals: Vec<String>,

    #[arg(short = 't', long = "time")]
    time: Option<String>,

    #[command(flatten)]
    global: GlobalOpts,
}

#[derive(Args, Debug)]
struct StuckArgs {
    #[arg(value_name = "STORE_FILE")]
    bwave: String,

    /// Optional value filter — only signals stuck at VALUE
    #[arg(value_name = "VALUE")]
    value: Option<String>,

    #[arg(short = 's', long = "signals", action = clap::ArgAction::Append,
          value_name = "PATTERN[%RADIX]")]
    signals: Vec<String>,

    #[command(flatten)]
    global: GlobalOpts,
}

// ===================================================================
//   Patterns / values shared parsing helpers
// ===================================================================

/// Build (patterns, signal_radixes) from a raw -s list. Empty list defaults
/// to the wildcard pattern.
fn split_patterns_and_radixes(
    signals: &[String],
) -> (Vec<String>, Vec<(String, bwave::format::Radix)>) {
    if signals.is_empty() {
        return (vec!["*".to_string()], Vec::new());
    }
    let pairs: Vec<_> = signals
        .iter()
        .map(|s| match parse_radix_suffix(s) {
            Ok(pair) => pair,
            // Exit rather than skip: a mistyped radix used to silently
            // remove that signal from the query's result set.
            Err(e) => {
                eprintln!("ERROR: {}", e);
                process::exit(2);
            }
        })
        .collect();
    let pats = pairs.iter().map(|(p, _)| p.clone()).collect();
    let rads = pairs.into_iter().map(|(p, r)| (p, r)).collect();
    (pats, rads)
}

/// Normalize a Verilog literal (or pass-through edge keyword) → canonical hex.
fn normalize_value(val: &str) -> String {
    if is_edge_keyword(val).is_some() {
        return val.to_string();
    }
    match parse_verilog_literal(val) {
        Ok(hex) => hex,
        Err(e) => {
            eprintln!("ERROR: {}", e);
            process::exit(2);
        }
    }
}

/// Parse `--marker NAME CYCLE` repeatable pairs into (name, cycle) tuples.
fn parse_markers(raw: &[String]) -> Vec<(String, i64)> {
    raw.chunks(2)
        .filter_map(|chunk| {
            if chunk.len() == 2 {
                chunk[1].parse::<i64>().ok().map(|c| (chunk[0].clone(), c))
            } else {
                None
            }
        })
        .collect()
}

// ===================================================================
//   Store-file gate (most query subcommands require a built waveform
//   store — .fst — not raw VCD)
// ===================================================================

/// True when `path` names a built waveform store that loads through
/// `ColumnCache`.
fn is_store_path(path: &str) -> bool {
    path.ends_with(".fst")
}

/// The v7 columnar `.bwave` format was retired after the FST migration
/// (ADR 0041). `.bwave` files were ephemeral
/// per-run caches, always regenerable from the VCD.
fn reject_legacy_bwave(path: &str) {
    if path.ends_with(".bwave") {
        eprintln!("ERROR: the .bwave format was replaced by FST; rebuild with `bwave build <vcd> -o trace.fst`");
        process::exit(2);
    }
}

fn require_bwave(path: &str, cmd: &str) {
    reject_legacy_bwave(path);
    if !is_store_path(path) {
        eprintln!(
            "ERROR: `{}` requires a built waveform store (got: {})",
            cmd, path
        );
        eprintln!("  Build one first:  bwave build {} -o trace.fst", path);
        process::exit(2);
    }
}

// ===================================================================
//   build (VCD → FST store)
// ===================================================================

fn build_bwave_from_reader(
    output_path: &str,
    reader: &mut (impl io::BufRead + Send),
    scope: Option<&str>,
    engine: BuildEngine,
    jobs: Option<usize>,
    parse_jobs: Option<usize>,
    encode_jobs: Option<usize>,
    pack_jobs: Option<usize>,
    chunk_bytes: Option<usize>,
    section_bytes: Option<usize>,
    fifo_descriptor: Option<i32>,
) {
    let out = Path::new(output_path);
    let header = match try_parse_header(reader) {
        Ok(header) => header,
        Err(error) => {
            eprintln!("ERROR: {error}");
            process::exit(1);
        }
    };
    if engine == BuildEngine::Parallel {
        if let Some(descriptor) = fifo_descriptor {
            make_descriptor_nonblocking(descriptor).unwrap_or_else(|e| {
                eprintln!("ERROR: cannot configure FIFO input: {e}");
                process::exit(1);
            });
        }
    }
    // Refuse to build an unqueryable store. A VCD that declares no signals
    // produces a header-only .fst that answers every query with silence —
    // the caller then debugs the design instead of the trace setup. Same
    // for a --scope that filters everything out. Exit 2: caller-input
    // class, matching the query-side empty-store gate.
    if header.signals.is_empty() {
        eprintln!(
            "ERROR: input VCD declares no signals — refusing to build a \
             header-only (unqueryable) store. The classic producer is a \
             Verilator sim traced via the auto-generated --main, which opens \
             the dump and writes only the header; trace via a custom C++ \
             --exe main instead."
        );
        process::exit(2);
    }
    if let Some(s) = scope {
        if bwave::signal::signals_in_scope(&header.signals, s).is_empty() {
            eprintln!(
                "ERROR: --scope '{}' matches none of the {} signal(s) in the \
                 input VCD — refusing to build an empty store. Drop --scope, \
                 or check the hierarchy with `bwave build` + `bwave list`.",
                s,
                header.signals.len()
            );
            process::exit(2);
        }
    }
    // Heartbeat sidecar so external stall monitors (Booley FIFO watchdog)
    // see liveness during VCD parsing — the store itself isn't written
    // until finalize_and_write() at EOF.
    let heartbeat = out.with_file_name(format!(
        "{}.progress",
        out.file_name()
            .and_then(|s| s.to_str())
            .unwrap_or("trace.fst")
    ));
    let _ = std::fs::remove_file(&heartbeat);
    let mut handler = match bwave::fst::FstBuildHandler::new(&header, scope, out) {
        Ok(h) => h,
        Err(e) => {
            eprintln!("ERROR: {}", e);
            process::exit(1);
        }
    };
    let parse_result = match engine {
        BuildEngine::Serial => handler.parse_bytes(reader, Some(&heartbeat)),
        BuildEngine::Parallel => {
            let total_jobs = jobs.unwrap_or_else(default_build_jobs);
            let (parse_jobs, encode_jobs, pack_jobs) =
                match parallel_worker_counts(total_jobs, parse_jobs, encode_jobs, pack_jobs) {
                    Ok(counts) => counts,
                    Err(error) => {
                        eprintln!("ERROR: {error}");
                        process::exit(2);
                    }
                };
            handler.parse_bytes_parallel(
                reader,
                Some(&heartbeat),
                parse_jobs,
                encode_jobs,
                pack_jobs,
                chunk_bytes.unwrap_or(bwave::fst::PARALLEL_VCD_CHUNK_TARGET),
                section_bytes.unwrap_or(bwave::fst::PARALLEL_FST_SECTION_TARGET),
                fifo_descriptor,
            )
        }
    };
    if let Err(error) = parse_result {
        drop(handler);
        let _ = std::fs::remove_file(out);
        let _ = std::fs::remove_file(&heartbeat);
        eprintln!("ERROR: {error}");
        process::exit(1);
    }
    if let Err(error) = handler.finalize_and_write() {
        let _ = std::fs::remove_file(out);
        let _ = std::fs::remove_file(&heartbeat);
        eprintln!("ERROR: {error}");
        process::exit(1);
    }
    let _ = std::fs::remove_file(&heartbeat);
}

fn default_build_jobs() -> usize {
    std::thread::available_parallelism()
        .map(|count| count.get().clamp(2, 6))
        .unwrap_or(2)
}

fn parallel_worker_counts(
    total_jobs: usize,
    parse_jobs: Option<usize>,
    encode_jobs: Option<usize>,
    pack_jobs: Option<usize>,
) -> Result<(usize, usize, usize), String> {
    let default_pack = if total_jobs >= 6 && encode_jobs.is_none_or(|jobs| jobs == 1) {
        2
    } else {
        1
    };
    let pack = pack_jobs.unwrap_or(default_pack);
    let default_encode = if pack > 1 {
        1
    } else {
        total_jobs
            .div_ceil(4)
            .min(7)
            .min(total_jobs.saturating_sub(1).max(1))
    };
    let encode = encode_jobs.unwrap_or(default_encode);
    let parse = parse_jobs.unwrap_or(total_jobs.saturating_sub(encode.max(pack)).max(1));
    if parse == 0 || encode == 0 || pack == 0 {
        return Err("parallel worker counts must be positive".to_string());
    }
    if parse.saturating_add(encode) > total_jobs {
        return Err(format!(
            "parse jobs ({parse}) plus encode jobs ({encode}) exceed --jobs {total_jobs}"
        ));
    }
    if pack > 1 && encode != 1 {
        return Err("the compression prototype requires exactly one encode job".to_string());
    }
    if pack > 1 && parse.saturating_add(pack) > total_jobs {
        return Err(format!(
            "parse jobs ({parse}) plus pack jobs ({pack}) exceed --jobs {total_jobs}"
        ));
    }
    Ok((parse, encode, pack))
}

#[cfg(unix)]
fn fifo_descriptor(file: &File) -> io::Result<Option<i32>> {
    if !file.metadata()?.file_type().is_fifo() {
        return Ok(None);
    }
    Ok(Some(file.as_raw_fd()))
}

#[cfg(not(unix))]
fn fifo_descriptor(_file: &File) -> io::Result<Option<i32>> {
    Ok(None)
}

#[cfg(unix)]
fn stdin_descriptor(stdin: &io::Stdin) -> Option<i32> {
    Some(stdin.as_raw_fd())
}

#[cfg(not(unix))]
fn stdin_descriptor(_stdin: &io::Stdin) -> Option<i32> {
    None
}

#[cfg(unix)]
fn make_descriptor_nonblocking(descriptor: i32) -> io::Result<()> {
    // SAFETY: `descriptor` belongs to the live borrowed File. F_GETFL does
    // not mutate memory, and F_SETFL changes only the descriptor status flags.
    let flags = unsafe { libc::fcntl(descriptor, libc::F_GETFL) };
    if flags == -1 {
        return Err(io::Error::last_os_error());
    }
    // SAFETY: same valid descriptor; preserve every existing flag and add
    // O_NONBLOCK so the chunker's cancellation checks can interrupt FIFO waits.
    if unsafe { libc::fcntl(descriptor, libc::F_SETFL, flags | libc::O_NONBLOCK) } == -1 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

#[cfg(not(unix))]
fn make_descriptor_nonblocking(_descriptor: i32) -> io::Result<()> {
    Ok(())
}

fn run_build(args: BuildArgs) {
    reject_legacy_bwave(&args.output);
    // Three input modes: --input <path>, positional <vcd>, or stdin.
    let input_path = args.input.as_deref().or(args.vcd_file.as_deref());
    if let Some(path) = input_path {
        let file = File::open(path).unwrap_or_else(|e| {
            eprintln!("ERROR: cannot open '{}': {}", path, e);
            process::exit(1);
        });
        let fifo_descriptor = fifo_descriptor(&file).unwrap_or_else(|e| {
            eprintln!("ERROR: cannot inspect input '{}': {}", path, e);
            process::exit(1);
        });
        let mut reader = BufReader::with_capacity(256 * 1024, file);
        build_bwave_from_reader(
            &args.output,
            &mut reader,
            args.scope.as_deref(),
            args.engine,
            args.jobs,
            args.parse_jobs,
            args.encode_jobs,
            args.pack_jobs,
            args.chunk_bytes,
            args.section_bytes,
            fifo_descriptor,
        );
    } else {
        let stdin = io::stdin();
        let input_descriptor = stdin_descriptor(&stdin);
        let mut reader = BufReader::with_capacity(256 * 1024, stdin);
        build_bwave_from_reader(
            &args.output,
            &mut reader,
            args.scope.as_deref(),
            args.engine,
            args.jobs,
            args.parse_jobs,
            args.encode_jobs,
            args.pack_jobs,
            args.chunk_bytes,
            args.section_bytes,
            input_descriptor,
        );
    }
}

// ===================================================================
//   query dispatch (store → results)
// ===================================================================

/// Run a query through ColumnCache (loads the store file directly).
fn query_bwave(bwave_path: &str, mut cfg: ExtractConfig) {
    let mut cache = match ColumnCache::load_from_file(Path::new(bwave_path)) {
        Some(c) => c,
        None => {
            eprintln!("ERROR: cannot load waveform store '{}'", bwave_path);
            process::exit(1);
        }
    };
    // A store that parses but declares zero signals answers *every* query
    // with silence — the classic producer is a Verilator sim traced via the
    // auto-generated --main, which opens the dump and writes only the
    // header. Fail loudly here; `list` (run_list) stays exit-0 so it can
    // still be used to inspect the store.
    if cache.signals.is_empty() {
        eprintln!("ERROR: {}", bwave::cache::no_signals_in_store_message());
        process::exit(2);
    }
    if let Some(ref clk_pat) = cfg.clock_pattern {
        match cache.detect_clock_from_pattern(clk_pat) {
            Ok((period, first_rise, name)) => cache.override_clock(period, first_rise, &name),
            Err(e) => {
                eprintln!("ERROR: {}", e);
                // A --clock pattern that doesn't fit this trace is caller
                // input, same class as a -s total miss — exit 2, not 1
                // (1 is reserved for environment/I-O failures).
                process::exit(2);
            }
        }
    }
    // Resolve time tokens now that we have the cache header.
    resolve_time_tokens_or_exit(&mut cfg, cache.ticks_to_ns, cache.clock_period_ticks);
    if cfg.wave_mode {
        wave_from_cache(&cache, &cfg);
    } else if cfg.diff_points.is_some() {
        diff_from_cache(&cache, &cfg);
    } else if cfg.distance_a.is_some() {
        distance_from_cache(&cache, &cfg);
    } else if cfg.stats_mode {
        stats_from_cache(&cache, &cfg);
    } else if cfg.find_stuck.is_some() {
        find_stuck_from_cache(&cache, &cfg);
    } else if cfg.find_pattern.is_some() {
        find_value_from_cache(&cache, &cfg);
    } else if cfg.sample_at_pattern.is_some() {
        sample_at_from_cache(&cache, &cfg);
    } else if cfg.at_time.is_some() {
        snapshot_from_cache(&cache, &cfg);
    } else {
        trace_from_cache(&cache, &cfg);
    }
}

// ===================================================================
//   Subcommand handlers
// ===================================================================

fn run_list(args: ListArgs) {
    let g = args.global;
    let json_format = g.format == "json";
    let (patterns, _radixes) = split_patterns_and_radixes(&args.signals);
    require_bwave(&args.bwave, "bwave list");
    let cache = match ColumnCache::load_from_file(Path::new(&args.bwave)) {
        Some(c) => c,
        None => {
            eprintln!("ERROR: cannot load waveform store '{}'", args.bwave);
            process::exit(1);
        }
    };
    list_signals_from_cache(&cache, &patterns, args.tree, json_format, g.limit);
}

fn run_signal(args: SignalArgs) {
    let g = args.global;
    require_bwave(&args.bwave, "bwave signal");
    let (patterns, signal_radixes) = split_patterns_and_radixes(&args.signals);

    let cfg = ExtractConfig {
        patterns,
        signal_radixes,
        async_mode: g.async_mode,
        clock_pattern: g.clock,
        reset_pattern: g.reset,
        with_reset: g.with_reset,
        time_str: args.time,
        max_lines: g.limit,
        markers: parse_markers(&args.marker.markers),
        json_format: g.format == "json",
        ..Default::default()
    };
    query_bwave(&args.bwave, cfg);
}

fn run_wave(args: WaveArgs) {
    let g = args.global;
    // Grid rendering operates on the built store.
    require_bwave(&args.bwave, "bwave wave");
    let (patterns, signal_radixes) = split_patterns_and_radixes(&args.signals);

    let cfg = ExtractConfig {
        patterns,
        signal_radixes,
        wave_mode: true,
        wave_rle: args.rle,
        async_mode: g.async_mode,
        clock_pattern: g.clock,
        reset_pattern: g.reset,
        with_reset: g.with_reset,
        time_str: args.time,
        max_lines: g.limit,
        markers: parse_markers(&args.marker.markers),
        virtual_defs: args.virtuals.virtual_defs,
        json_format: g.format == "json",
        ..Default::default()
    };
    query_bwave(&args.bwave, cfg);
}

fn run_value(args: ValueArgs) {
    let g = args.global;
    require_bwave(&args.bwave, "bwave value");
    let (patterns, signal_radixes) = split_patterns_and_radixes(&args.signals);

    let cfg = ExtractConfig {
        patterns,
        signal_radixes,
        at_str: Some(args.at),
        // Sync values are cycles; async values are resolved to raw ticks by
        // resolve_time_tokens() before snapshot_from_cache consumes them.
        at_time_is_cycle: !g.async_mode,
        async_mode: g.async_mode,
        clock_pattern: g.clock,
        reset_pattern: g.reset,
        with_reset: g.with_reset,
        max_lines: g.limit,
        markers: parse_markers(&args.marker.markers),
        virtual_defs: args.virtuals.virtual_defs,
        json_format: g.format == "json",
        ..Default::default()
    };
    query_bwave(&args.bwave, cfg);
}

fn run_find(args: FindArgs) {
    let g = args.global;
    require_bwave(&args.bwave, "bwave find");

    if args.first && args.last {
        eprintln!("ERROR: --first and --last are mutually exclusive");
        process::exit(2);
    }
    if args.last && args.count {
        eprintln!("ERROR: --last and --count are mutually exclusive");
        process::exit(2);
    }
    if args.before.is_some() && args.after.is_some() {
        eprintln!("ERROR: --before and --after are mutually exclusive");
        process::exit(2);
    }
    if (args.before.is_some() || args.after.is_some()) && args.time.is_some() {
        eprintln!("ERROR: --before/--after cannot be combined with -t/--time");
        process::exit(2);
    }
    if (args.before.is_some() || args.after.is_some()) && (args.first || args.last) {
        eprintln!(
            "ERROR: --before/--after cannot be combined with --first/--last (direction is implied)"
        );
        process::exit(2);
    }

    // Expand --before / --after into time-range + direction.
    // --before/--after take bare i64 (cycle in sync, tick in async) — they're
    // *bounds*, not tokens, so the Phase-4 grammar doesn't apply here.
    let mut first_match = args.first;
    let mut last_match = args.last;
    let mut time_min: i64 = 0;
    let mut time_max: Option<i64> = None;
    let mut time_str: Option<String> = None;
    if let Some(n) = args.before {
        last_match = true;
        time_max = Some(n);
    } else if let Some(n) = args.after {
        first_match = true;
        time_min = n;
    } else {
        time_str = args.time;
    }

    let value = normalize_value(&args.value);
    let (patterns, signal_radixes) = (vec!["*".to_string()], Vec::new());

    let cfg = ExtractConfig {
        patterns,
        signal_radixes,
        find_pattern: Some(args.pattern),
        find_value: Some(value),
        first_match,
        last_match,
        count_only: args.count,
        async_mode: g.async_mode,
        clock_pattern: g.clock,
        reset_pattern: g.reset,
        with_reset: g.with_reset,
        time_min,
        time_max,
        time_str,
        max_lines: g.limit,
        markers: parse_markers(&args.marker.markers),
        virtual_defs: args.virtuals.virtual_defs,
        json_format: g.format == "json",
        ..Default::default()
    };
    query_bwave(&args.bwave, cfg);
}

fn run_sample(args: SampleArgs) {
    let g = args.global;
    require_bwave(&args.bwave, "bwave sample");

    if args.first && args.last {
        eprintln!("ERROR: --first and --last are mutually exclusive");
        process::exit(2);
    }
    if args.last && args.count {
        eprintln!("ERROR: --last and --count are mutually exclusive");
        process::exit(2);
    }
    if args.before.is_some() && args.after.is_some() {
        eprintln!("ERROR: --before and --after are mutually exclusive");
        process::exit(2);
    }
    if (args.before.is_some() || args.after.is_some()) && args.time.is_some() {
        eprintln!("ERROR: --before/--after cannot be combined with -t/--time");
        process::exit(2);
    }
    if (args.before.is_some() || args.after.is_some()) && (args.first || args.last) {
        eprintln!(
            "ERROR: --before/--after cannot be combined with --first/--last (direction is implied)"
        );
        process::exit(2);
    }

    let mut first_match = args.first;
    let mut last_match = args.last;
    let mut time_min: i64 = 0;
    let mut time_max: Option<i64> = None;
    let mut time_str: Option<String> = None;
    if let Some(n) = args.before {
        last_match = true;
        time_max = Some(n);
    } else if let Some(n) = args.after {
        first_match = true;
        time_min = n;
    } else {
        time_str = args.time;
    }

    let (patterns, signal_radixes) = split_patterns_and_radixes(&args.signals);
    let value = normalize_value(&args.value);

    let cfg = ExtractConfig {
        patterns,
        signal_radixes,
        sample_at_pattern: Some(args.trigger),
        sample_at_value: Some(value),
        first_match,
        last_match,
        count_only: args.count,
        async_mode: g.async_mode,
        clock_pattern: g.clock,
        reset_pattern: g.reset,
        with_reset: g.with_reset,
        time_min,
        time_max,
        time_str,
        max_lines: g.limit,
        markers: parse_markers(&args.marker.markers),
        virtual_defs: args.virtuals.virtual_defs,
        json_format: g.format == "json",
        ..Default::default()
    };
    query_bwave(&args.bwave, cfg);
}

fn run_diff(args: DiffArgs) {
    let g = args.global;
    // Diff operates on the built store.
    require_bwave(&args.bwave, "bwave diff");
    let (patterns, signal_radixes) = split_patterns_and_radixes(&args.signals);

    let cfg = ExtractConfig {
        patterns,
        signal_radixes,
        diff_strs: Some((args.t1, args.t2)),
        async_mode: g.async_mode,
        clock_pattern: g.clock,
        reset_pattern: g.reset,
        with_reset: g.with_reset,
        max_lines: g.limit,
        markers: parse_markers(&args.marker.markers),
        json_format: g.format == "json",
        ..Default::default()
    };
    query_bwave(&args.bwave, cfg);
}

fn run_distance(args: DistanceArgs) {
    let g = args.global;
    // Distance operates on the built store.
    require_bwave(&args.bwave, "bwave distance");
    let (patterns, signal_radixes) = split_patterns_and_radixes(&args.signals);

    let value = normalize_value(&args.value);
    let distance_a = Some((args.pattern, value));
    let distance_b = args.distance_to.map(|v| {
        let pat = v[0].clone();
        let val = normalize_value(&v[1]);
        (pat, val)
    });

    let cfg = ExtractConfig {
        patterns,
        signal_radixes,
        distance_a,
        distance_b,
        stats_mode: args.stats,
        async_mode: g.async_mode,
        clock_pattern: g.clock,
        reset_pattern: g.reset,
        with_reset: g.with_reset,
        time_str: args.time,
        max_lines: g.limit,
        markers: parse_markers(&args.marker.markers),
        virtual_defs: args.virtuals.virtual_defs,
        json_format: g.format == "json",
        ..Default::default()
    };
    query_bwave(&args.bwave, cfg);
}

fn run_stats(args: StatsArgs) {
    let g = args.global;
    require_bwave(&args.bwave, "bwave stats");
    let (patterns, signal_radixes) = split_patterns_and_radixes(&args.signals);

    let cfg = ExtractConfig {
        patterns,
        signal_radixes,
        stats_mode: true,
        async_mode: g.async_mode,
        clock_pattern: g.clock,
        reset_pattern: g.reset,
        with_reset: g.with_reset,
        time_str: args.time,
        max_lines: g.limit,
        json_format: g.format == "json",
        ..Default::default()
    };
    query_bwave(&args.bwave, cfg);
}

fn run_stuck(args: StuckArgs) {
    let g = args.global;
    require_bwave(&args.bwave, "bwave stuck");
    let (patterns, signal_radixes) = split_patterns_and_radixes(&args.signals);
    // `value` is an opaque filter — preserved verbatim like the old behavior
    // (cache.rs interprets empty string as "any value").
    let find_stuck = Some(args.value.unwrap_or_default());

    let cfg = ExtractConfig {
        patterns,
        signal_radixes,
        find_stuck,
        async_mode: g.async_mode,
        clock_pattern: g.clock,
        reset_pattern: g.reset,
        with_reset: g.with_reset,
        max_lines: g.limit,
        json_format: g.format == "json",
        ..Default::default()
    };
    query_bwave(&args.bwave, cfg);
}

// ===================================================================
//   Helpers
// ===================================================================

/// Resolve `cfg`'s raw time-token strings (`time_str` / `at_str` /
/// `diff_strs`) against the simulation's timescale. Exits with code 2 on
/// any parse / unit-resolution failure. Logged as a `--time` / `--at` /
/// `diff` error to make the offending argument obvious.
fn resolve_time_tokens_or_exit(cfg: &mut ExtractConfig, ticks_to_ns: f64, clock_period_ticks: u64) {
    if let Err(e) = cfg.resolve_time_tokens(ticks_to_ns, clock_period_ticks) {
        eprintln!("ERROR: {}", e);
        process::exit(2);
    }
}

// ===================================================================
//   main()
// ===================================================================

/// Print the embedded JSON Schema for `--format json` envelopes.
/// The schema is baked into the binary at build time (see schema/bwave.json).
fn run_schema() {
    const SCHEMA: &str = include_str!("../schema/bwave.json");
    print!("{}", SCHEMA);
    if !SCHEMA.ends_with('\n') {
        println!();
    }
}

fn run_docs(args: DocsArgs) {
    match args.action {
        DocsAction::Topics => bwave::docs::print_topics(),
        DocsAction::Search { query } => bwave::docs::search(&query),
        DocsAction::Show { topic } => {
            if !bwave::docs::show(&topic) {
                eprintln!(
                    "ERROR: unknown topic '{}'. Run `bwave docs topics` to list available topics.",
                    topic
                );
                process::exit(1);
            }
        }
    }
}

fn run_skill() {
    print!("{}", bwave::docs::SKILL);
    if !bwave::docs::SKILL.ends_with('\n') {
        println!();
    }
}

fn main() {
    let cli = Cli::parse();
    match cli.command {
        Command::Build(a) => run_build(a),
        Command::List(a) => run_list(a),
        Command::Signal(a) => run_signal(a),
        Command::Wave(a) => run_wave(a),
        Command::Value(a) => run_value(a),
        Command::Find(a) => run_find(a),
        Command::Sample(a) => run_sample(a),
        Command::Diff(a) => run_diff(a),
        Command::Distance(a) => run_distance(a),
        Command::Stats(a) => run_stats(a),
        Command::Stuck(a) => run_stuck(a),
        Command::Schema => run_schema(),
        Command::Docs(a) => run_docs(a),
        Command::Skill => run_skill(),
    }
}

#[cfg(test)]
mod build_worker_tests {
    use super::parallel_worker_counts;

    #[test]
    fn six_job_default_matches_promoted_topology() {
        assert_eq!(parallel_worker_counts(6, None, None, None), Ok((4, 1, 2)));
    }

    #[test]
    fn explicit_encoder_override_keeps_single_packer_default() {
        assert_eq!(
            parallel_worker_counts(6, None, Some(2), None),
            Ok((4, 2, 1))
        );
    }
}
