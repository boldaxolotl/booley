//! FST-backed implementation of the `ColumnCache` read interface.
//!
//! Loads a plain FST waveform (as written by `bwave build` or dumped natively
//! by a simulator) and presents it through the exact same `ColumnCache`
//! surface the ten `*_from_cache` query functions consume. The disk format is
//! pure FST — no sidecars, no custom attributes — so every trace bwave
//! produces opens directly in GTKWave and VaporView.
//!
//! Metadata that `.bwave` v7 headers carried (clock table, sampling edge,
//! first-rise tick, clock-before-reset flag, cycle-rebased sim start) is
//! re-derived here at load time from the FST content itself. FST stores full
//! async transitions losslessly, so everything the VCD streaming path
//! computed is recomputable — with one documented exception: VCD *line order
//! within a single timestamp* is not representable in FST, so the
//! clock-before-reset-at-deassert flag is approximated as "the clock has a
//! transition at exactly the deassert tick".
//!
//! Reader library: fst-reader (not wellen — wellen merges bit-blasted vars
//! like `bus[0]`/`bus[1]` into synthetic vectors with no opt-out, which
//! breaks signal-name parity with the VCD view; see the Phase 0 report).

use std::fs::File;
use std::io::{BufReader, Write};
use std::path::Path;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use fst_reader::{
    FstFilter, FstHierarchyEntry, FstReader, FstSignalHandle, FstSignalValue, FstVarType,
};
use memchr::{memchr, memchr2};
use rustc_hash::{FxHashMap, FxHashSet};

use crate::cache::{CachedSignal, ClockEntry, ColumnCache};
use crate::format::format_value;
use crate::parser::VcdHeader;
use crate::signal::signals_in_scope;

/// f64 read from an FST frame slot that was never written: fst-writer fills
/// frames with ASCII 'x' bytes, so a real signal with no change before the
/// first time step reads back as this exact bit pattern. We drop it.
const REAL_FRAME_GARBAGE_BITS: u64 = u64::from_le_bytes([b'x'; 8]);

/// Byte-oriented event boundary used by the production VCD scanner.
pub trait VcdByteSink {
    fn timestamp(&mut self, tick: u64);
    fn scalar(&mut self, id: &[u8], value: u8);
    fn vector(&mut self, id: &[u8], bits: &[u8]);
    fn real(&mut self, id: &[u8], value: &[u8]);
    fn current_tick(&self) -> u64 {
        0
    }
    fn directive(&mut self, _line: &[u8]) {}
    fn ignored_line(&mut self, _line: &[u8]) {}
}

impl<W: std::io::Write + std::io::Seek> VcdByteSink for FstBuildHandler<W> {
    fn current_tick(&self) -> u64 {
        self.current_tick
    }

    fn timestamp(&mut self, tick: u64) {
        if tick > self.current_tick || !self.any_time_written {
            self.current_tick = tick;
        }
        if tick <= self.last_written_time && self.any_time_written {
            return;
        }
        if self.body.size() >= FST_FLUSH_THRESHOLD {
            if let Err(e) = self.body.flush() {
                self.record_write_error(e);
            }
        }
        match self.body.time_change(tick) {
            Ok(()) => {
                self.last_written_time = tick;
                self.any_time_written = true;
            }
            Err(e) => self.record_write_error(e),
        }
    }
    fn scalar(&mut self, id: &[u8], value: u8) {
        self.handle_scalar(id, value);
    }
    fn vector(&mut self, id: &[u8], bits: &[u8]) {
        self.handle_vector(id, bits);
    }
    fn real(&mut self, id: &[u8], value: &[u8]) {
        self.handle_real(id, value);
    }
}

struct CountingBuildSink<'a, W: std::io::Write + std::io::Seek> {
    handler: &'a mut FstBuildHandler<W>,
    counters: &'a mut VcdBuildCounters,
}

impl<W: std::io::Write + std::io::Seek> CountingBuildSink<'_, W> {
    fn record_identifier(&mut self, id: &[u8]) {
        if id.len() == 1 {
            self.counters.one_character_ids += 1;
        } else {
            self.counters.multi_character_ids += 1;
        }
    }

    fn record_outcome(&mut self, outcome: ChangeOutcome) {
        match outcome {
            ChangeOutcome::Ignored => self.counters.ignored_ids += 1,
            ChangeOutcome::Duplicate => self.counters.duplicate_values += 1,
            ChangeOutcome::Changed | ChangeOutcome::Failed => {}
        }
    }
}

impl<W: std::io::Write + std::io::Seek> VcdByteSink for CountingBuildSink<'_, W> {
    fn current_tick(&self) -> u64 {
        self.handler.current_tick
    }

    fn timestamp(&mut self, tick: u64) {
        self.counters.timestamp_lines += 1;
        self.handler.timestamp(tick);
    }

    fn scalar(&mut self, id: &[u8], value: u8) {
        self.record_identifier(id);
        self.counters.scalar_lines += 1;
        let outcome = self.handler.handle_scalar(id, value);
        self.record_outcome(outcome);
    }

    fn vector(&mut self, id: &[u8], bits: &[u8]) {
        self.record_identifier(id);
        self.counters.vector_lines += 1;
        self.counters.vector_bytes += bits.len() as u64;
        if bits.iter().all(|bit| matches!(bit, b'0' | b'1')) {
            self.counters.two_state_vectors += 1;
        } else {
            self.counters.four_state_vectors += 1;
        }
        let outcome = self.handler.handle_vector(id, bits);
        self.record_outcome(outcome);
    }

    fn real(&mut self, id: &[u8], value: &[u8]) {
        self.record_identifier(id);
        self.counters.real_lines += 1;
        let outcome = self.handler.handle_real(id, value);
        self.record_outcome(outcome);
    }

    fn directive(&mut self, _line: &[u8]) {
        self.counters.directive_lines += 1;
    }

    fn ignored_line(&mut self, _line: &[u8]) {
        self.counters.ignored_lines += 1;
    }
}

fn parse_byte_timestamp(line: &[u8]) -> Option<u64> {
    let mut i = 1;
    while i < line.len() && line[i] == b' ' {
        i += 1;
    }
    let mut tick = 0u64;
    let mut valid = false;
    while i < line.len() && line[i].is_ascii_digit() {
        tick = tick.wrapping_mul(10).wrapping_add((line[i] - b'0') as u64);
        valid = true;
        i += 1;
    }
    valid.then_some(tick)
}

#[inline(always)]
fn separated_byte_value(line: &[u8]) -> Option<(&[u8], &[u8])> {
    let sep = memchr2(b' ', b'\t', line)?;
    let mut id = sep + 1;
    while id < line.len() && matches!(line[id], b' ' | b'\t') {
        id += 1;
    }
    Some((&line[1..sep], &line[id..]))
}

#[inline(always)]
fn dispatch_byte_line<S: VcdByteSink>(sink: &mut S, line: &[u8], dumpoff: &mut bool) {
    match line[0] {
        b'#' => match parse_byte_timestamp(line) {
            Some(t) => sink.timestamp(t),
            None => sink.ignored_line(line),
        },
        b'0' | b'1' | b'x' | b'z' | b'X' | b'Z' if !*dumpoff => {
            sink.scalar(&line[1..], line[0].to_ascii_lowercase())
        }
        b'b' | b'B' if !*dumpoff => match separated_byte_value(line) {
            Some((v, id)) => sink.vector(id, v),
            None => sink.ignored_line(line),
        },
        b'r' | b'R' if !*dumpoff => match separated_byte_value(line) {
            Some((v, id)) => sink.real(id, v),
            None => sink.ignored_line(line),
        },
        b'$' => {
            if line.starts_with(b"$dumpoff") {
                *dumpoff = true;
            } else if line.starts_with(b"$dumpon") {
                *dumpoff = false;
            }
            sink.directive(line);
        }
        _ => sink.ignored_line(line),
    }
}

fn scan_vcd_bytes_inner<R: std::io::BufRead, S: VcdByteSink>(
    reader: &mut R,
    sink: &mut S,
    heartbeat: Option<&Path>,
) -> u64 {
    const BUFFER_SIZE: usize = 4 * 1024 * 1024;
    const HEARTBEAT_INTERVAL: Duration = Duration::from_secs(5);
    let mut buf = vec![0u8; BUFFER_SIZE];
    let mut leftover = 0;
    let mut dumpoff = false;
    let mut total_bytes_read = 0u64;
    let mut last_heartbeat = Instant::now();
    loop {
        let n = match reader.read(&mut buf[leftover..]) {
            Ok(n) => n,
            Err(e) => {
                eprintln!("WARNING: I/O error reading VCD stream: {e}");
                0
            }
        };
        total_bytes_read += n as u64;
        if let Some(path) = heartbeat {
            if last_heartbeat.elapsed() >= HEARTBEAT_INTERVAL {
                if let Ok(mut f) = std::fs::OpenOptions::new()
                    .create(true)
                    .append(true)
                    .open(path)
                {
                    let _ = writeln!(f, "bytes={total_bytes_read} tick={}", sink.current_tick());
                }
                last_heartbeat = Instant::now();
            }
        }
        if n == 0 {
            if leftover > 0 {
                let mut line = &buf[..leftover];
                if line.last() == Some(&b'\r') {
                    line = &line[..line.len() - 1];
                }
                if !line.is_empty() {
                    dispatch_byte_line(sink, line, &mut dumpoff);
                }
            }
            break;
        }
        let total = leftover + n;
        let mut pos = 0;
        while let Some(newline_offset) = memchr(b'\n', &buf[pos..total]) {
            let end = pos + newline_offset;
            let mut line = &buf[pos..end];
            if line.last() == Some(&b'\r') {
                line = &line[..line.len() - 1];
            }
            if !line.is_empty() {
                dispatch_byte_line(sink, line, &mut dumpoff);
            }
            pos = end + 1;
        }
        if pos < total {
            buf.copy_within(pos..total, 0);
            leftover = total - pos;
        } else {
            leftover = 0;
        }
    }
    total_bytes_read
}

pub fn scan_vcd_bytes<R: std::io::BufRead, S: VcdByteSink>(reader: &mut R, sink: &mut S) -> u64 {
    scan_vcd_bytes_inner(reader, sink, None)
}

/// Live FST backing for a `ColumnCache`.
///
/// The reader is behind a `Mutex` because query code reads signals from
/// rayon worker threads (`&ColumnCache` is shared across threads).
///
/// Every `read_signals` call re-scans block metadata and re-decodes the
/// time table, so per-signal file passes are expensive on large traces.
/// Two mitigations keep query latency near the packed-format baseline:
/// - `prefetch` (called by the query entry points with their matched
///   signal set) decodes many signals in ONE pass into `memo`
/// - every single-signal read also lands in `memo`, so repeated reads
///   (clock/reset probes, two-phase queries) never hit the file twice
pub struct FstBacking {
    reader: Mutex<FstReader<BufReader<File>>>,
    /// Per signal index (parallel to `ColumnCache::signals`).
    handles: Vec<usize>, // FstSignalHandle indices (handle is not Clone)
    is_real: Vec<bool>,
    /// Real-ness indexed by FST handle (hot-path lookup for bulk decodes).
    is_real_by_handle: Vec<bool>,
    /// Decoded full streams for the handful of signals the loader probed
    /// (clock/reset candidates), keyed by FST handle index so aliases share
    /// one entry. Deliberately NOT a general cache: memoizing every stream
    /// a query touches would hold multi-GB of decoded strings on
    /// high-activity traces.
    memo: Mutex<FxHashMap<usize, std::sync::Arc<Vec<(u64, String)>>>>,
    /// Prefix streams (all changes in `[0, max_tick]`) bulk-decoded by
    /// `prefetch_to`; serves every windowed read with `tick_max <= max_tick`
    /// without another file pass.
    prefix: Mutex<Option<PrefixMemo>>,
}

struct PrefixMemo {
    max_tick: u64,
    by_handle: FxHashMap<usize, Vec<(u64, String)>>,
}

impl FstBacking {
    #[inline]
    fn handle_is_real(&self, handle_idx: usize) -> bool {
        self.is_real_by_handle
            .get(handle_idx)
            .copied()
            .unwrap_or(false)
    }

    /// Bulk-decode the `[0, max_tick]` prefix of many signals in ONE file
    /// pass (each fst-reader pass re-decodes the time table and block
    /// metadata, so per-signal passes dominate point-query latency). Called
    /// by windowed query entry points (value/wave/diff) with their matched
    /// signal set; the pass early-terminates past `max_tick`, so memory is
    /// bounded by the window, not the trace.
    pub(crate) fn prefetch_to(&self, sig_indices: &[usize], max_tick: u64) {
        if sig_indices.len() <= 1 {
            return; // a single signal costs the same either way
        }
        {
            let prefix = self.prefix.lock().unwrap();
            if let Some(p) = prefix.as_ref() {
                if p.max_tick >= max_tick
                    && sig_indices
                        .iter()
                        .all(|&i| p.by_handle.contains_key(&self.handles[i]))
                {
                    return;
                }
            }
        }
        let mut need: Vec<usize> = sig_indices.iter().map(|&i| self.handles[i]).collect();
        need.sort_unstable();
        need.dedup();
        let handles: Vec<FstSignalHandle> = need
            .iter()
            .map(|&h| FstSignalHandle::from_index(h))
            .collect();
        let mut by_handle: FxHashMap<usize, Vec<(u64, String)>> = FxHashMap::default();
        {
            let mut reader = self.reader.lock().unwrap();
            let _ = reader.read_signals(
                &FstFilter::new(0, max_tick, handles),
                |time, h, value| -> Result<(), ()> {
                    if time <= max_tick {
                        let hi = h.get_index();
                        if let Some(v) = canon_value(&value, self.handle_is_real(hi)) {
                            by_handle.entry(hi).or_default().push((time, v));
                        }
                    }
                    Ok(())
                },
            );
        }
        for (&h, list) in by_handle.iter_mut() {
            strip_real_frame_garbage(list, self.handle_is_real(h));
        }
        // signals with no changes in the window still need an entry so the
        // hit-check above and read_range know they were covered
        for h in need {
            by_handle.entry(h).or_default();
        }
        *self.prefix.lock().unwrap() = Some(PrefixMemo {
            max_tick,
            by_handle,
        });
    }

    /// All transitions of one signal, in bwave's canonical value-string form.
    /// Loader-probed signals (clock/reset) are served from the memo; other
    /// signals decode directly per call, exactly like the packed backend
    /// decodes transiently from its in-memory image.
    pub(crate) fn read_all(&self, sig_idx: usize) -> Vec<(u64, String)> {
        let h = self.handles[sig_idx];
        if let Some(hit) = self.memo.lock().unwrap().get(&h) {
            return hit.as_ref().clone();
        }
        let is_real = self.is_real[sig_idx];
        let mut out: Vec<(u64, String)> = Vec::new();
        {
            let mut reader = self.reader.lock().unwrap();
            let _ = reader.read_signals(
                &FstFilter::filter_signals(vec![FstSignalHandle::from_index(h)]),
                |time, _h, value| -> Result<(), ()> {
                    if let Some(v) = canon_value(&value, is_real) {
                        out.push((time, v));
                    }
                    Ok(())
                },
            );
        }
        strip_real_frame_garbage(&mut out, is_real);
        out
    }

    /// Transitions of one signal within `[tick_min, tick_max]` plus the last
    /// value before the range. Served from the prefix memo when covered;
    /// otherwise one windowed file pass from time 0 (section-frame semantics
    /// can never hide a value that precedes the window) with early
    /// termination past `tick_max`.
    pub(crate) fn read_range(
        &self,
        sig_idx: usize,
        tick_min: u64,
        tick_max: u64,
    ) -> (Option<String>, Vec<(u64, String)>) {
        let h = self.handles[sig_idx];
        {
            let prefix = self.prefix.lock().unwrap();
            if let Some(p) = prefix.as_ref() {
                if p.max_tick >= tick_max {
                    if let Some(stream) = p.by_handle.get(&h) {
                        return split_window(stream, tick_min, tick_max);
                    }
                }
            }
        }
        if let Some(hit) = self.memo.lock().unwrap().get(&h) {
            return split_window(hit, tick_min, tick_max);
        }
        let is_real = self.is_real[sig_idx];
        let mut all: Vec<(u64, String)> = Vec::new();
        {
            let mut reader = self.reader.lock().unwrap();
            let _ = reader.read_signals(
                &FstFilter::new(0, tick_max, vec![FstSignalHandle::from_index(h)]),
                |time, _h, value| -> Result<(), ()> {
                    if time <= tick_max {
                        if let Some(v) = canon_value(&value, is_real) {
                            all.push((time, v));
                        }
                    }
                    Ok(())
                },
            );
        }
        strip_real_frame_garbage(&mut all, is_real);
        split_window(&all, tick_min, tick_max)
    }

    /// One batched pass over several signals (used for clock/reset
    /// re-derivation at load). Returns raw transition lists per handle index.
    fn read_many(
        reader: &mut FstReader<BufReader<File>>,
        handle_indices: &[usize],
    ) -> rustc_hash::FxHashMap<usize, Vec<(u64, String)>> {
        let mut by_handle: rustc_hash::FxHashMap<usize, Vec<(u64, String)>> =
            rustc_hash::FxHashMap::default();
        if handle_indices.is_empty() {
            return by_handle;
        }
        let handles: Vec<FstSignalHandle> = handle_indices
            .iter()
            .map(|&i| FstSignalHandle::from_index(i))
            .collect();
        let _ = reader.read_signals(
            &FstFilter::filter_signals(handles),
            |time, h, value| -> Result<(), ()> {
                if let Some(v) = canon_value(&value, false) {
                    by_handle.entry(h.get_index()).or_default().push((time, v));
                }
                Ok(())
            },
        );
        by_handle
    }
}

/// Convert an FST value to bwave's canonical stored-string form — the same
/// transformation `format_value` applied to raw VCD tokens at build time:
/// 1-char values as-is, multi-bit with x/z kept as bit text, pure-binary
/// converted to leading-zero-stripped uppercase hex.
fn canon_value(value: &FstSignalValue, is_real: bool) -> Option<String> {
    match value {
        FstSignalValue::String(bytes) => {
            // FST stores bit strings at full declared width; VCD (and thus
            // .bwave) uses the minimal form where left-extension padding is
            // implied. The shared minimal_xz normalization (also applied at
            // .bwave decode) makes both backends report identical strings
            // regardless of how much padding the dump carried.
            let s = std::str::from_utf8(bytes).ok()?;
            Some(crate::cache::minimal_xz(format_value(s)))
        }
        FstSignalValue::Real(r) => {
            if is_real && r.to_bits() == REAL_FRAME_GARBAGE_BITS {
                // frame slot for a never-initialized real — no genuine value
                return Some(String::new()); // marker, stripped below
            }
            // Debug formatting keeps a decimal point ("100.0", not "100"),
            // matching the raw VCD real tokens .bwave stores. Never run
            // reals through format_value — a value like 100 would be
            // mistaken for binary and hexified.
            Some(format!("{r:?}"))
        }
    }
}

/// Remove the frame-garbage marker produced by `canon_value` for reals.
fn strip_real_frame_garbage(list: &mut Vec<(u64, String)>, is_real: bool) {
    if is_real {
        list.retain(|(_, v)| !v.is_empty());
    }
}

/// Split a `[0, ..]` change stream into (last value before `tick_min`,
/// changes within `[tick_min, tick_max]`).
fn split_window(
    stream: &[(u64, String)],
    tick_min: u64,
    tick_max: u64,
) -> (Option<String>, Vec<(u64, String)>) {
    let mut before: Option<String> = None;
    let mut in_range = Vec::new();
    for (t, v) in stream {
        if *t < tick_min {
            before = Some(v.clone());
        } else if *t <= tick_max {
            in_range.push((*t, v.clone()));
        } else {
            break;
        }
    }
    (before, in_range)
}

// ---------------------------------------------------------------- loading

struct VarEntry {
    name: String,
    width: u32,
    var_type: String,
    handle_idx: usize,
    is_real: bool,
}

fn var_type_str(tpe: FstVarType) -> &'static str {
    match tpe {
        FstVarType::Wire => "wire",
        FstVarType::Reg => "reg",
        FstVarType::Integer => "integer",
        FstVarType::Parameter => "parameter",
        FstVarType::Real | FstVarType::RealParameter | FstVarType::ShortReal => "real",
        FstVarType::RealTime => "realtime",
        FstVarType::Time => "time",
        FstVarType::Logic => "logic",
        FstVarType::Bit => "bit",
        FstVarType::Supply0 => "supply0",
        FstVarType::Supply1 => "supply1",
        FstVarType::Tri => "tri",
        FstVarType::TriAnd => "triand",
        FstVarType::TriOr => "trior",
        FstVarType::TriReg => "trireg",
        FstVarType::Tri0 => "tri0",
        FstVarType::Tri1 => "tri1",
        FstVarType::Wand => "wand",
        FstVarType::Wor => "wor",
        FstVarType::Event => "event",
        FstVarType::Port => "port",
        FstVarType::Int => "int",
        _ => "wire",
    }
}

/// Join the VCD token form "name [7:0]" / "name [3]" into "name[7:0]".
/// GTKWave-family writers (Verilator's embedded fstapi) keep the two-token
/// VCD form in the FST hierarchy; bwave's VCD parser joins the tokens, so
/// FST-loaded directories must match. Only a digits/colon bracket group
/// after a single space is joined — escaped identifiers with other
/// embedded spaces pass through untouched.
fn join_bit_range(name: String) -> String {
    if name.ends_with(']') {
        if let Some(pos) = name.rfind(" [") {
            let inner = &name[pos + 2..name.len() - 1];
            if !inner.is_empty() && inner.bytes().all(|b| b.is_ascii_digit() || b == b':') {
                let mut out = String::with_capacity(name.len() - 1);
                out.push_str(&name[..pos]);
                out.push_str(&name[pos + 1..]);
                return out;
            }
        }
    }
    name
}

/// Render an FST timescale exponent back to a VCD-style timescale string.
/// exponent -9 -> "1ns", -10 -> "100ps", -11 -> "10ps".
fn timescale_str_from_exponent(exp: i8) -> String {
    let units: [(i8, &str); 6] = [
        (0, "s"),
        (-3, "ms"),
        (-6, "us"),
        (-9, "ns"),
        (-12, "ps"),
        (-15, "fs"),
    ];
    for (u, name) in units {
        let delta = exp as i32 - u as i32;
        if (0..=2).contains(&delta) {
            let factor = 10_i32.pow(delta as u32);
            return format!("{factor}{name}");
        }
    }
    // out of the VCD range — fall back to 1ns semantics
    "1ns".to_string()
}

/// Load a plain FST file behind the `ColumnCache` interface.
/// Returns `None` if the file is missing or not a readable FST.
pub fn load_fst(path: &Path) -> Option<ColumnCache> {
    let file = File::open(path).ok()?;
    let mut reader = FstReader::open(BufReader::new(file)).ok()?;
    let header = reader.get_header();

    // -- signal directory from the raw FST hierarchy -----------------------
    let mut vars: Vec<VarEntry> = Vec::new();
    let mut scope_stack: Vec<String> = Vec::new();
    reader
        .read_hierarchy(|entry| match entry {
            FstHierarchyEntry::Scope { name, .. } => scope_stack.push(name),
            FstHierarchyEntry::UpScope => {
                scope_stack.pop();
            }
            FstHierarchyEntry::Var {
                tpe,
                name,
                length,
                handle,
                is_alias,
                ..
            } => {
                let name = join_bit_range(name);
                let full = if scope_stack.is_empty() {
                    name
                } else {
                    format!("{}.{}", scope_stack.join("."), name)
                };
                let is_real = matches!(
                    tpe,
                    FstVarType::Real
                        | FstVarType::RealTime
                        | FstVarType::RealParameter
                        | FstVarType::ShortReal
                );
                // reals report their byte length; VCD declares them as 64-bit
                let width = if is_real {
                    64
                } else if length == 0 {
                    1
                } else {
                    length
                };
                let mut var_type = var_type_str(tpe).to_string();
                let mut width = width;
                let mut is_real = is_real;
                // The .bwave builder keys alias groups by VCD id and keeps
                // only the FIRST declaration's metadata for the whole group;
                // replicate that so directory listings match. (An alias with
                // a different declared type, e.g. `reg` in tb and `wire` in
                // dut, shows the first type for both entries.)
                if is_alias {
                    if let Some(first) = vars.iter().find(|v| v.handle_idx == handle.get_index()) {
                        var_type = first.var_type.clone();
                        width = first.width;
                        is_real = first.is_real;
                    }
                }
                vars.push(VarEntry {
                    name: full,
                    width,
                    var_type,
                    handle_idx: handle.get_index(),
                    is_real,
                });
            }
            _ => {}
        })
        .ok()?;

    let signals: Vec<CachedSignal> = vars
        .iter()
        .map(|v| CachedSignal {
            name: v.name.clone(),
            width: v.width,
            var_type: v.var_type.clone(),
            // aliases share the FST handle; queries dedup alias groups by
            // group_id, so expose the handle index there.
            group_id: v.handle_idx as u64,
        })
        .collect();

    // -- clock + reset re-derivation ---------------------------------------
    // Mirrors the VCD streaming path's detection (extract.rs detect_clock):
    // candidates are 1-bit signals whose bracket-stripped name contains
    // "clk" (resp. "rst"), sorted by (scope depth, name).
    let mut clock_candidates: Vec<usize> = Vec::new();
    let mut reset_candidates: Vec<usize> = Vec::new();
    for (i, v) in vars.iter().enumerate() {
        if v.width == 1 && !v.is_real {
            let stripped = v.name.split('[').next().unwrap_or(&v.name).to_lowercase();
            if stripped.contains("clk") {
                clock_candidates.push(i);
            }
            if stripped.contains("rst") {
                reset_candidates.push(i);
            }
        }
    }
    let by_depth_then_name = |a: &usize, b: &usize| {
        let da = vars[*a].name.matches('.').count();
        let db = vars[*b].name.matches('.').count();
        da.cmp(&db).then(vars[*a].name.cmp(&vars[*b].name))
    };
    clock_candidates.sort_by(by_depth_then_name);
    reset_candidates.sort_by(by_depth_then_name);

    // one batched read for every candidate signal
    let mut probe_handles: Vec<usize> = Vec::new();
    for &i in clock_candidates.iter().chain(reset_candidates.iter()) {
        probe_handles.push(vars[i].handle_idx);
    }
    probe_handles.sort_unstable();
    probe_handles.dedup();
    let probe_data = FstBacking::read_many(&mut reader, &probe_handles);
    let empty: Vec<(u64, String)> = Vec::new();
    let trans_of = |var_idx: usize| -> &Vec<(u64, String)> {
        probe_data.get(&vars[var_idx].handle_idx).unwrap_or(&empty)
    };

    // first two rising edges per clock candidate -> (period, first_rise)
    let rises = |transitions: &[(u64, String)]| -> (Option<u64>, Option<u64>) {
        let mut prev = "x";
        let (mut first, mut second) = (None, None);
        for (t, v) in transitions {
            if v == "1" && prev == "0" {
                if first.is_none() {
                    first = Some(*t);
                } else if second.is_none() {
                    second = Some(*t);
                    break;
                }
            }
            prev = v;
        }
        (first, second)
    };

    // clock table, deduplicated by (period, phase) like the builder
    let mut clock_table: Vec<ClockEntry> = Vec::new();
    let mut primary_clock: Option<(u64, u64, String)> = None; // period, first_rise, name
    for &ci in &clock_candidates {
        let (first, second) = rises(trans_of(ci));
        if primary_clock.is_none() {
            if let (Some(f), Some(s)) = (first, second) {
                if s > f {
                    primary_clock = Some((s - f, f, vars[ci].name.clone()));
                }
            }
        }
        if let (Some(f), Some(s)) = (first, second) {
            if s > f {
                let period = s - f;
                let phase = f % period;
                let dup = clock_table
                    .iter()
                    .any(|e| e.period == period && e.first_rise % e.period == phase);
                if !dup {
                    clock_table.push(ClockEntry {
                        period,
                        first_rise: f,
                        id: vars[ci].name.clone(),
                    });
                }
            }
        }
    }
    // Note: primary clock = first candidate *with a valid period*. The
    // builder instead locks onto the first candidate by sort order even if
    // it never toggles; for a never-toggling first candidate it stores no
    // clock meta at all, which matches primary_clock = None here unless a
    // later candidate toggles. The differential harness arbitrates.

    // reset: first candidate; active-low from the leaf name
    let mut reset_deassert_tick: Option<u64> = None;
    let mut reset_transitions: Option<&Vec<(u64, String)>> = None;
    let mut reset_active_low = false;
    if let Some(&ri) = reset_candidates.first() {
        let leaf = vars[ri]
            .name
            .split('[')
            .next()
            .unwrap_or(&vars[ri].name)
            .split('.')
            .next_back()
            .unwrap_or(&vars[ri].name)
            .to_lowercase();
        reset_active_low = leaf.ends_with('n') || leaf.contains("_n");
        let rt = trans_of(ri);
        reset_transitions = Some(rt);
        // builder starts with reset_active = true; the deassert tick is the
        // first 0/1 transition observed in the deasserted state
        for (t, v) in rt {
            let asserted = if reset_active_low { v == "0" } else { v == "1" };
            if v == "0" || v == "1" {
                if !asserted {
                    reset_deassert_tick = Some(*t);
                    break;
                }
            }
        }
    }
    // reset asserted-state after all events at `tick` (builder evaluates at
    // the following timestamp boundary): last 0/1 value at or before tick,
    // initial state = asserted
    let reset_asserted_after = |tick: u64| -> bool {
        match reset_transitions {
            None => false, // no reset signal — builder counts every edge
            Some(rt) => {
                let mut asserted = true;
                for (t, v) in rt {
                    if *t > tick {
                        break;
                    }
                    if v == "0" || v == "1" {
                        asserted = if reset_active_low { v == "0" } else { v == "1" };
                    }
                }
                asserted
            }
        }
    };

    // clock-before-reset flag (approximation, see module docs)
    let mut clock_before_reset_at_deassert = false;
    if let (Some(dt), Some(&ci)) = (reset_deassert_tick, clock_candidates.first()) {
        clock_before_reset_at_deassert = trans_of(ci).iter().any(|(t, _)| *t == dt);
    }

    // sim range: end = last time in the file; start = first counted cycle
    // (first primary-clock rising edge with reset inactive after that tick),
    // falling back to the header start time
    let sim_end_tick = header.end_time;
    let mut sim_start_tick = header.start_time;
    if let Some(&ci) = clock_candidates.first() {
        let mut prev = "x";
        for (t, v) in trans_of(ci) {
            if v == "1" && prev == "0" && !reset_asserted_after(*t) {
                sim_start_tick = *t;
                break;
            }
            prev = v;
        }
    }

    let (clock_period_ticks, first_rise_tick, clock_id) = match primary_clock {
        Some((p, f, name)) => (p, f, name),
        None => (0, 0, String::new()),
    };

    let ticks_to_ns = 10f64.powi(header.timescale_exponent as i32 + 9);
    let timescale_str = timescale_str_from_exponent(header.timescale_exponent);

    let mut is_real_by_handle =
        vec![false; vars.iter().map(|v| v.handle_idx + 1).max().unwrap_or(0)];
    for v in &vars {
        if v.is_real {
            is_real_by_handle[v.handle_idx] = true;
        }
    }
    // Seed the decode memo with the clock/reset probe results — sync-mode
    // queries re-read those signals immediately (reset rebasing) and must
    // not pay a second file pass for them.
    let mut memo: FxHashMap<usize, std::sync::Arc<Vec<(u64, String)>>> = FxHashMap::default();
    for (h, list) in probe_data {
        memo.insert(h, std::sync::Arc::new(list));
    }
    let backing = FstBacking {
        reader: Mutex::new(reader),
        handles: vars.iter().map(|v| v.handle_idx).collect(),
        is_real: vars.iter().map(|v| v.is_real).collect(),
        is_real_by_handle,
        memo: Mutex::new(memo),
        prefix: Mutex::new(None),
    };

    Some(ColumnCache::new_fst_backed(
        signals,
        sim_start_tick,
        sim_end_tick,
        ticks_to_ns,
        clock_period_ticks,
        first_rise_tick,
        timescale_str,
        clock_id,
        clock_before_reset_at_deassert,
        clock_table,
        backing,
    ))
}

// ---------------------------------------------------------------- writing

const NO_GROUP: u32 = u32::MAX;
/// Flush the fst-writer signal buffer to disk once it grows past this size.
/// Bounds memory on huge streams (replaces CacheBuilder temp-file spilling);
/// small traces stay single-section, which keeps windowed reads cheap.
const FST_FLUSH_THRESHOLD: usize = 256 << 20;

/// "10ps" -> -11. Mirrors the VCD `1|10|100 <unit>` timescale grammar.
fn timescale_to_exponent(ts: &str) -> i8 {
    let ts = ts.trim();
    let digits: String = ts.chars().take_while(|c| c.is_ascii_digit()).collect();
    let unit = ts[digits.len()..].trim();
    let factor_exp: i8 = match digits.as_str() {
        "1" | "" => 0,
        "10" => 1,
        "100" => 2,
        _ => 0,
    };
    let unit_exp: i8 = match unit {
        "s" => 0,
        "ms" => -3,
        "us" => -6,
        "ns" => -9,
        "ps" => -12,
        "fs" => -15,
        _ => -9,
    };
    unit_exp + factor_exp
}

fn var_type_of(vt: &str) -> fst_writer::FstVarType {
    use fst_writer::FstVarType as W;
    match vt {
        "reg" => W::Reg,
        "integer" => W::Integer,
        "parameter" => W::Parameter,
        "real" => W::Real,
        "realtime" => W::RealTime,
        "time" => W::Time,
        "logic" => W::Logic,
        "bit" => W::Bit,
        "supply0" => W::Supply0,
        "supply1" => W::Supply1,
        "tri" => W::Tri,
        "triand" => W::TriAnd,
        "trior" => W::TriOr,
        "trireg" => W::TriReg,
        "tri0" => W::Tri0,
        "tri1" => W::Tri1,
        "wand" => W::Wand,
        "wor" => W::Wor,
        "event" => W::Event,
        "port" => W::Port,
        "int" => W::Int,
        _ => W::Wire,
    }
}

/// Emit scope transitions between two hierarchical paths: pop to the common
/// prefix, then push the new scopes. Preserving VCD declaration order means a
/// re-entered scope becomes a duplicate FST scope entry — exactly what
/// GTKWave's vcd2fst produces, and what keeps the signal directory order
/// identical to the .bwave backend's.
fn transition_scopes<W: std::io::Write + std::io::Seek>(
    hw: &mut fst_writer::FstHeaderWriter<W>,
    current: &mut Vec<String>,
    target: &[&str],
) -> Result<(), String> {
    let common = current
        .iter()
        .zip(target.iter())
        .take_while(|(a, b)| a.as_str() == **b)
        .count();
    while current.len() > common {
        hw.up_scope().map_err(|e| format!("fst up_scope: {e}"))?;
        current.pop();
    }
    for name in &target[common..] {
        hw.scope(*name, "", fst_writer::FstScopeType::Module)
            .map_err(|e| format!("fst scope '{name}': {e}"))?;
        current.push((*name).to_string());
    }
    Ok(())
}

struct GroupMeta {
    fst_id: fst_writer::FstSignalId,
    width: u32,
    is_real: bool,
}

const MAX_DENSE_IDS: usize = 1_000_000;

fn decode_canonical_numeric_id(id: &[u8]) -> Option<usize> {
    if id.is_empty() || (id.len() > 1 && id.last() == Some(&b'!')) {
        return None;
    }
    let mut value = 0usize;
    let mut place = 1usize;
    for &byte in id {
        let digit = byte.checked_sub(b'!')? as usize;
        if digit >= 94 {
            return None;
        }
        value = value.checked_add(digit.checked_mul(place)?)?;
        place = place.checked_mul(94)?;
    }
    Some(value)
}

pub struct VcdIdLookup {
    single: [u32; 256],
    dense: Vec<u32>,
    fallback: FxHashMap<Vec<u8>, u32>,
}

impl VcdIdLookup {
    pub fn new<'a>(ids: impl Iterator<Item = &'a str>) -> Self {
        let codes: FxHashSet<usize> = ids
            .filter_map(|id| {
                let bytes = id.as_bytes();
                if bytes.len() > 1 {
                    decode_canonical_numeric_id(bytes)
                } else {
                    None
                }
            })
            .collect();
        let dense_len = codes
            .iter()
            .max()
            .and_then(|code| code.checked_add(1))
            .filter(|&len| {
                len <= MAX_DENSE_IDS && len <= 4096usize.max(codes.len().saturating_mul(4))
            })
            .unwrap_or(0);
        Self {
            single: [NO_GROUP; 256],
            dense: vec![NO_GROUP; dense_len],
            fallback: FxHashMap::default(),
        }
    }

    #[inline(always)]
    pub fn resolve(&self, id: &[u8]) -> Option<u32> {
        if id.len() == 1 {
            return (self.single[id[0] as usize] != NO_GROUP)
                .then_some(self.single[id[0] as usize]);
        }
        if let Some(group) = decode_canonical_numeric_id(id)
            .and_then(|code| self.dense.get(code))
            .copied()
            .filter(|&group| group != NO_GROUP)
        {
            return Some(group);
        }
        self.fallback.get(id).copied()
    }

    pub fn insert(&mut self, id: &str, group: u32) {
        let bytes = id.as_bytes();
        if bytes.len() == 1 {
            self.single[bytes[0] as usize] = group;
        } else if let Some(slot) =
            decode_canonical_numeric_id(bytes).and_then(|code| self.dense.get_mut(code))
        {
            *slot = group;
        } else {
            self.fallback.insert(bytes.to_vec(), group);
        }
    }
}

/// Streaming VCD -> FST build handler (the replacement for the retired
/// `.bwave` builder). Construct with the parsed VCD header, feed the body
/// through `parse_bytes`, then `finalize_and_write`.
pub struct FstBuildHandler<W: std::io::Write + std::io::Seek = std::io::BufWriter<File>> {
    body: fst_writer::FstBodyWriter<W>,
    output_path: std::path::PathBuf,
    id_lookup: VcdIdLookup,
    groups: Vec<GroupMeta>,
    current_tick: u64,
    last_written_time: u64,
    any_time_written: bool,
    overwide_values: u64,
    write_error: Option<String>,
    counters: Option<VcdBuildCounters>,
}

#[derive(Clone, Copy)]
enum ChangeOutcome {
    Ignored,
    Changed,
    Duplicate,
    Failed,
}

/// Optional aggregate VCD-build diagnostics. Collection is disabled by default.
#[derive(Clone, Debug, Default)]
pub struct VcdBuildCounters {
    pub input_bytes: u64,
    pub timestamp_lines: u64,
    pub scalar_lines: u64,
    pub vector_lines: u64,
    pub real_lines: u64,
    pub directive_lines: u64,
    pub ignored_lines: u64,
    pub one_character_ids: u64,
    pub multi_character_ids: u64,
    pub vector_bytes: u64,
    pub two_state_vectors: u64,
    pub four_state_vectors: u64,
    pub ignored_ids: u64,
    pub duplicate_values: u64,
    pub flushes: u64,
    pub uncompressed_stream_bytes: u64,
    pub compressed_stream_bytes: u64,
    pub flush_time: Duration,
}

fn fst_build_info(header: &VcdHeader) -> fst_writer::FstInfo {
    fst_writer::FstInfo {
        start_time: 0,
        timescale_exponent: timescale_to_exponent(&header.timescale_str),
        version: format!("bwave {}", env!("CARGO_PKG_VERSION")),
        date: String::new(),
        file_type: fst_writer::FstFileType::Verilog,
    }
}

impl FstBuildHandler<std::io::BufWriter<File>> {
    /// Parse the header, emit the FST hierarchy, and return a handler ready
    /// to stream the VCD body. `scope` limits the store to a hierarchical
    /// subtree exactly like the .bwave builder.
    pub fn new(
        header: &VcdHeader,
        scope: Option<&str>,
        output_path: &Path,
    ) -> Result<Self, String> {
        let info = fst_build_info(header);
        let writer = fst_writer::open_fst(output_path, &info)
            .map_err(|e| format!("cannot create '{}': {e}", output_path.display()))?;
        FstBuildHandler::from_header_writer(header, scope, output_path.to_path_buf(), writer)
    }
}

impl FstBuildHandler<std::io::Cursor<Vec<u8>>> {
    /// Create a build backed by an in-memory seekable output.
    pub fn new_in_memory(header: &VcdHeader, scope: Option<&str>) -> Result<Self, String> {
        let info = fst_build_info(header);
        let writer = fst_writer::FstHeaderWriter::new(std::io::Cursor::new(Vec::new()), &info)
            .map_err(|e| format!("cannot create in-memory FST: {e}"))?;
        FstBuildHandler::from_header_writer(
            header,
            scope,
            std::path::PathBuf::from("<memory>"),
            writer,
        )
    }
}

impl<W: std::io::Write + std::io::Seek> FstBuildHandler<W> {
    fn from_header_writer(
        header: &VcdHeader,
        scope: Option<&str>,
        output_path: std::path::PathBuf,
        mut writer: fst_writer::FstHeaderWriter<W>,
    ) -> Result<Self, String> {
        let signals = match scope {
            Some(s) => {
                let filtered = signals_in_scope(&header.signals, s);
                if filtered.is_empty() {
                    eprintln!("WARNING: --scope '{}' matched 0 signals", s);
                }
                filtered
            }
            None => header.signals.clone(),
        };

        // Emit vars in exact VCD declaration order, streaming scope
        // transitions between consecutive signals. The first declaration of
        // a VCD id becomes the FST signal, later declarations alias it.
        let mut id_lookup = VcdIdLookup::new(signals.iter().map(|signal| signal.id.as_str()));
        let mut groups: Vec<GroupMeta> = Vec::new();
        let mut current_scope: Vec<String> = Vec::new();
        for sig in &signals {
            let mut parts: Vec<&str> = sig.name.split('.').collect();
            let var_name = parts.pop().unwrap_or(&sig.name);
            transition_scopes(&mut writer, &mut current_scope, &parts)?;

            let is_real = sig.var_type == "real" || sig.var_type == "realtime";
            let signal_tpe = if is_real {
                fst_writer::FstSignalType::real()
            } else {
                fst_writer::FstSignalType::bit_vec(sig.width)
            };
            let id_bytes = sig.id.as_bytes();
            let existing = id_lookup.resolve(id_bytes);
            let alias = existing.map(|group| groups[group as usize].fst_id);
            let fst_id = writer
                .var(
                    var_name,
                    signal_tpe,
                    var_type_of(&sig.var_type),
                    fst_writer::FstVarDirection::Implicit,
                    alias,
                )
                .map_err(|e| format!("fst var '{}': {e}", sig.name))?;
            if existing.is_none() {
                groups.push(GroupMeta {
                    fst_id,
                    width: sig.width,
                    is_real,
                });
                let g = (groups.len() - 1) as u32;
                id_lookup.insert(&sig.id, g);
            }
        }
        transition_scopes(&mut writer, &mut current_scope, &[])?;

        let body = writer
            .finish()
            .map_err(|e| format!("fst header finish: {e}"))?;
        Ok(FstBuildHandler {
            body,
            output_path,
            id_lookup,
            groups,
            current_tick: 0,
            last_written_time: 0,
            any_time_written: false,
            overwide_values: 0,
            write_error: None,
            counters: None,
        })
    }

    /// Enable aggregate diagnostics for subsequent parsing and section flushes.
    pub fn enable_counters(&mut self) {
        self.counters.get_or_insert_with(VcdBuildCounters::default);
        self.body.enable_stats();
    }

    /// Select compression for subsequent section flushes.
    pub fn set_compression(&mut self, compression: fst_writer::FstCompression) {
        self.body.set_compression(compression);
    }

    #[inline(always)]
    fn lookup_group(&self, id: &[u8]) -> u32 {
        self.id_lookup.resolve(id).unwrap_or(NO_GROUP)
    }

    fn record_write_error(&mut self, e: impl std::fmt::Display) {
        if self.write_error.is_none() {
            self.write_error = Some(e.to_string());
        }
    }

    #[inline]
    fn handle_scalar(&mut self, id: &[u8], value: u8) -> ChangeOutcome {
        let group = self.lookup_group(id);
        if group == NO_GROUP {
            return ChangeOutcome::Ignored;
        }
        let value = [value];
        let bytes_are_ready =
            self.groups[group as usize].width == 1 && !self.groups[group as usize].is_real;
        self.emit_change(group, bytes_are_ready, &value)
    }

    fn handle_vector(&mut self, id: &[u8], bits: &[u8]) -> ChangeOutcome {
        let group = self.lookup_group(id);
        if group == NO_GROUP {
            return ChangeOutcome::Ignored;
        }
        self.emit_change(group, false, bits)
    }

    fn handle_real(&mut self, id: &[u8], value: &[u8]) -> ChangeOutcome {
        let group = self.lookup_group(id);
        if group == NO_GROUP {
            return ChangeOutcome::Ignored;
        }
        self.emit_change(group, false, value)
    }

    fn emit_change(&mut self, group: u32, bytes_are_ready: bool, raw: &[u8]) -> ChangeOutcome {
        let group_meta = &self.groups[group as usize];
        let fst_id = group_meta.fst_id;
        let width = group_meta.width as usize;
        let is_real = group_meta.is_real;
        let result = if is_real {
            let v: f64 = std::str::from_utf8(raw)
                .ok()
                .and_then(|s| s.parse().ok())
                .unwrap_or(f64::NAN);
            self.body
                .signal_change_with_status(fst_id, &v.to_le_bytes())
        } else if bytes_are_ready {
            self.body.signal_change_with_status(fst_id, raw)
        } else {
            if raw.len() > width {
                self.overwide_values += 1;
            }
            self.body.signal_change_vcd_with_status(fst_id, raw)
        };
        match result {
            Ok(false) => ChangeOutcome::Duplicate,
            Ok(true) => ChangeOutcome::Changed,
            Err(error) => {
                self.record_write_error(error);
                ChangeOutcome::Failed
            }
        }
    }

    /// Block-based streaming parser: 4MB-chunk + memchr line scanning, with
    /// the heartbeat sidecar the Booley FIFO watchdog monitors.
    pub fn parse_bytes(
        &mut self,
        reader: &mut impl std::io::BufRead,
        heartbeat_path: Option<&Path>,
    ) {
        if let Some(mut counters) = self.counters.take() {
            let mut sink = CountingBuildSink {
                handler: self,
                counters: &mut counters,
            };
            let input_bytes = scan_vcd_bytes_inner(reader, &mut sink, heartbeat_path);
            counters.input_bytes += input_bytes;
            self.counters = Some(counters);
        } else {
            scan_vcd_bytes_inner(reader, self, heartbeat_path);
        }
    }

    /// Complete the FST without process-level logging or exits.
    pub fn finish(self) -> Result<Option<VcdBuildCounters>, String> {
        let FstBuildHandler {
            body,
            write_error,
            mut counters,
            ..
        } = self;
        if let Some(error) = write_error {
            return Err(error);
        }
        let writer_stats = body
            .finish_with_stats()
            .map_err(|error| error.to_string())?;
        if let (Some(counters), Some(writer_stats)) = (counters.as_mut(), writer_stats) {
            counters.flushes = writer_stats.sections;
            counters.uncompressed_stream_bytes = writer_stats.uncompressed_stream_bytes;
            counters.compressed_stream_bytes = writer_stats.compressed_stream_bytes;
            counters.flush_time = writer_stats.flush_time;
        }
        Ok(counters)
    }
}

impl FstBuildHandler<std::io::BufWriter<File>> {
    /// Finish the FST (writes the final value-change section and fixes up
    /// the header). Exits the process on write failure, matching the .bwave
    /// builder's behavior.
    pub fn finalize_and_write(self) {
        let overwide_values = self.overwide_values;
        let output_path = self.output_path.clone();
        if overwide_values > 0 {
            eprintln!(
                "WARNING: {} value(s) wider than their declared width were truncated",
                overwide_values
            );
        }
        match self.finish() {
            // Not a "cache": this is the primary build artifact. The old
            // wording cost an investigator a wrong-turn hunting a cache layer
            // that never existed.
            Ok(_) => eprintln!("# wrote {}", output_path.display()),
            Err(e) => {
                eprintln!("ERROR: failed to write {}: {}", output_path.display(), e);
                std::process::exit(1);
            }
        }
    }
}

#[cfg(test)]
mod byte_path_tests {
    use super::*;
    use std::io::{BufReader, Cursor, Seek};

    #[derive(Default)]
    struct Sink(Vec<String>);
    impl VcdByteSink for Sink {
        fn timestamp(&mut self, tick: u64) {
            self.0.push(format!("#{tick}"));
        }
        fn scalar(&mut self, id: &[u8], value: u8) {
            self.0
                .push(format!("{}{}", value as char, String::from_utf8_lossy(id)));
        }
        fn vector(&mut self, id: &[u8], bits: &[u8]) {
            self.0.push(format!(
                "b{} {}",
                String::from_utf8_lossy(bits),
                String::from_utf8_lossy(id)
            ));
        }
        fn real(&mut self, _: &[u8], _: &[u8]) {}
    }

    #[test]
    fn scanner_handles_crlf_tabs_and_dump_control() {
        let body = b"#0\r\n1!\r\nb10\t\"\r\n$dumpoff $end\r\n0!\r\n$dumpon $end\r\n";
        let mut reader = BufReader::new(Cursor::new(body));
        let mut sink = Sink::default();
        assert_eq!(scan_vcd_bytes(&mut reader, &mut sink), body.len() as u64);
        assert_eq!(sink.0, ["#0", "1!", "b10 \""]);
    }

    #[test]
    fn numeric_id_decoder_accepts_only_canonical_base94() {
        for byte in b'!'..=b'~' {
            assert_eq!(
                decode_canonical_numeric_id(&[byte]),
                Some((byte - b'!') as usize)
            );
        }
        assert_eq!(decode_canonical_numeric_id(b"!\""), Some(94));
        assert_eq!(decode_canonical_numeric_id(b"!!"), None);
        assert_eq!(decode_canonical_numeric_id(b"\"!"), None);
        assert_eq!(decode_canonical_numeric_id(&[0x7f]), None);
    }

    #[test]
    fn id_lookup_bounds_sparse_codes_and_preserves_fallback() {
        let mut lookup = VcdIdLookup::new(["~~~~", "é", "!!"].into_iter());
        assert!(lookup.dense.is_empty());
        lookup.insert("~~~~", 1);
        lookup.insert("é", 2);
        lookup.insert("!!", 3);
        assert_eq!(lookup.resolve(b"~~~~"), Some(1));
        assert_eq!(lookup.resolve("é".as_bytes()), Some(2));
        assert_eq!(lookup.resolve(b"!!"), Some(3));
    }

    #[test]
    fn id_lookup_uses_bounded_dense_storage_for_compact_codes() {
        let mut lookup = VcdIdLookup::new(["!\"", "~\""].into_iter());
        assert_eq!(lookup.dense.len(), 188);
        lookup.insert("!\"", 4);
        lookup.insert("~\"", 5);
        assert_eq!(lookup.resolve(b"!\""), Some(4));
        assert_eq!(lookup.resolve(b"~\""), Some(5));
    }

    #[test]
    fn build_counters_are_opt_in_and_cover_parser_writer_stages() {
        let vcd = b"$timescale 1ns $end\n$scope module tb $end\n\
            $var wire 1 ! sig $end\n$upscope $end\n$enddefinitions $end\n\
            #0\n0!\n#1\n1!\n1!\n1\"\n";
        let mut reader = BufReader::new(Cursor::new(vcd));
        let header = crate::parser::parse_header(&mut reader);
        let body_start = reader.stream_position().unwrap() as usize;
        let mut handler = FstBuildHandler::new_in_memory(&header, None).unwrap();
        handler.enable_counters();
        handler.set_compression(fst_writer::FstCompression::Disabled);
        let mut body_reader = BufReader::new(Cursor::new(&vcd[body_start..]));
        handler.parse_bytes(&mut body_reader, None);
        let counters = handler.finish().unwrap().unwrap();

        assert_eq!(counters.input_bytes, (vcd.len() - body_start) as u64);
        assert_eq!(counters.timestamp_lines, 2);
        assert_eq!(counters.scalar_lines, 4);
        assert_eq!(counters.one_character_ids, 4);
        assert_eq!(counters.ignored_ids, 1);
        assert_eq!(counters.duplicate_values, 1);
        assert_eq!(counters.flushes, 1);
        assert_eq!(
            counters.uncompressed_stream_bytes,
            counters.compressed_stream_bytes
        );
    }

    #[test]
    fn compression_disabled_build_remains_queryable() {
        let vcd = b"$timescale 1ns $end\n$scope module tb $end\n\
            $var wire 8 ! sig $end\n$upscope $end\n$enddefinitions $end\n\
            #0\nb00000000 !\n#1\nb10100101 !\n";
        let mut reader = BufReader::new(Cursor::new(vcd));
        let header = crate::parser::parse_header(&mut reader);
        let output_path = std::env::temp_dir().join(format!(
            "bwave_uncompressed_test_{}.fst",
            std::process::id()
        ));
        let mut handler = FstBuildHandler::new(&header, None, &output_path).unwrap();
        handler.set_compression(fst_writer::FstCompression::Disabled);
        handler.parse_bytes(&mut reader, None);
        assert!(handler.finish().unwrap().is_none());

        assert!(load_fst(&output_path).is_some());
        let _ = std::fs::remove_file(output_path);
    }
}
