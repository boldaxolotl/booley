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

use std::collections::{BTreeMap, VecDeque};
use std::fs::File;
use std::io::{BufReader, Write};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc, Mutex};
use std::time::{Duration, Instant};

use fst_reader::{
    FstFilter, FstHierarchyEntry, FstReader, FstSignalHandle, FstSignalValue, FstVarType,
};
use memchr::{memchr, memchr2};
use rustc_hash::{FxHashMap, FxHashSet};

use crate::cache::{CachedSignal, ClockEntry, ColumnCache};
use crate::format::format_value;
use crate::parser::{TimestampError, VcdHeader, VcdParseError};
use crate::signal::signals_in_scope;
use crate::vcd_chunk::{VcdChunk, VcdChunkSource};

/// f64 read from an FST frame slot that was never written: fst-writer fills
/// frames with ASCII 'x' bytes, so a real signal with no change before the
/// first time step reads back as this exact bit pattern. We drop it.
const REAL_FRAME_GARBAGE_BITS: u64 = u64::from_le_bytes([b'x'; 8]);

/// Byte-oriented event boundary retained for scanner attribution benchmarks.
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

fn parse_byte_timestamp(line: &[u8]) -> Option<u64> {
    let mut index = 1;
    while index < line.len() && line[index] == b' ' {
        index += 1;
    }
    let mut tick = 0u64;
    let mut valid = false;
    while index < line.len() && line[index].is_ascii_digit() {
        tick = tick
            .wrapping_mul(10)
            .wrapping_add((line[index] - b'0') as u64);
        valid = true;
        index += 1;
    }
    valid.then_some(tick)
}

#[inline(always)]
fn separated_byte_value(line: &[u8]) -> Option<(&[u8], &[u8])> {
    let separator = memchr2(b' ', b'\t', line)?;
    let mut id = separator + 1;
    while id < line.len() && matches!(line[id], b' ' | b'\t') {
        id += 1;
    }
    Some((&line[1..separator], &line[id..]))
}

#[inline(always)]
fn dispatch_byte_line<S: VcdByteSink>(sink: &mut S, line: &[u8], dumpoff: &mut bool) {
    match line[0] {
        b'#' => match parse_byte_timestamp(line) {
            Some(tick) => sink.timestamp(tick),
            None => sink.ignored_line(line),
        },
        b'0' | b'1' | b'x' | b'z' | b'X' | b'Z' if !*dumpoff => {
            sink.scalar(&line[1..], line[0].to_ascii_lowercase())
        }
        b'b' | b'B' if !*dumpoff => match separated_byte_value(line) {
            Some((value, id)) => sink.vector(id, value),
            None => sink.ignored_line(line),
        },
        b'r' | b'R' if !*dumpoff => match separated_byte_value(line) {
            Some((value, id)) => sink.real(id, value),
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

/// Scan VCD body bytes for attribution benchmarks without constructing IR.
pub fn scan_vcd_bytes<R: std::io::BufRead, S: VcdByteSink>(reader: &mut R, sink: &mut S) -> u64 {
    const BUFFER_SIZE: usize = 4 * 1024 * 1024;
    let mut buffer = vec![0u8; BUFFER_SIZE];
    let mut leftover = 0;
    let mut dumpoff = false;
    let mut total_bytes_read = 0u64;
    loop {
        let bytes_read = match reader.read(&mut buffer[leftover..]) {
            Ok(bytes_read) => bytes_read,
            Err(error) => {
                eprintln!("WARNING: I/O error reading VCD benchmark stream: {error}");
                0
            }
        };
        total_bytes_read += bytes_read as u64;
        if bytes_read == 0 {
            if leftover > 0 {
                let mut line = &buffer[..leftover];
                if line.last() == Some(&b'\r') {
                    line = &line[..line.len() - 1];
                }
                if !line.is_empty() {
                    dispatch_byte_line(sink, line, &mut dumpoff);
                }
            }
            break;
        }
        let total = leftover + bytes_read;
        let mut position = 0;
        while let Some(newline_offset) = memchr(b'\n', &buffer[position..total]) {
            let end = position + newline_offset;
            let mut line = &buffer[position..end];
            if line.last() == Some(&b'\r') {
                line = &line[..line.len() - 1];
            }
            if !line.is_empty() {
                dispatch_byte_line(sink, line, &mut dumpoff);
            }
            position = end + 1;
        }
        if position < total {
            buffer.copy_within(position..total, 0);
            leftover = total - position;
        } else {
            leftover = 0;
        }
    }
    total_bytes_read
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

/// Dense ID lookup retained for scanner attribution benchmarks.
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

/// Flush the fst-writer signal buffer to disk once it grows past this size.
/// Bounds memory on huge streams (replaces CacheBuilder temp-file spilling);
/// small traces stay single-section, which keeps windowed reads cheap.
const FST_FLUSH_THRESHOLD: usize = 256 << 20;
const SERIAL_VCD_CHUNK_TARGET: usize = 4 << 20;
pub const PARALLEL_VCD_CHUNK_TARGET: usize = 8 << 20;
pub const PARALLEL_FST_SECTION_TARGET: usize = 128 << 20;

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

/// Canonicalize a VCD bit-vector value to exactly `width` lowercase chars in
/// `out` (fst-writer panics on over-wide values and self-extends short ones,
/// but VCD extension rules for x/z need to be ours). Returns true when the
/// value arrived wider than the declared width (simulator dialect quirk;
/// least-significant bits are kept, matching GTKWave).
fn canon_bits_into(bits: &[u8], width: usize, out: &mut Vec<u8>) -> bool {
    out.clear();
    canon_bits_append(bits, width, out)
}

fn canon_bits_append(bits: &[u8], width: usize, out: &mut Vec<u8>) -> bool {
    let len = bits.len();
    if len < width {
        let fill = match bits.first().copied().unwrap_or(b'0').to_ascii_lowercase() {
            b'x' => b'x',
            b'z' => b'z',
            _ => b'0',
        };
        out.resize(out.len() + width - len, fill);
    }
    let src = if len > width {
        &bits[len - width..]
    } else {
        bits
    };
    out.extend(src.iter().map(|b| b.to_ascii_lowercase()));
    len > width
}

fn try_pack_binary_bits_append(bits: &[u8], width: usize, out: &mut Vec<u8>) -> Option<bool> {
    let len = bits.len();
    let src = if len > width {
        &bits[len - width..]
    } else {
        bits
    };
    let bit_offset = width - src.len();
    let output_start = out.len();
    out.resize(output_start + width.div_ceil(8), 0);
    for (chunk_index, chunk) in src.chunks(8).enumerate() {
        let packed = if let Ok(eight) = <[u8; 8]>::try_from(chunk) {
            let word = u64::from_le_bytes(eight);
            if word & 0xfefefefefefefefe != 0x3030303030303030 {
                out.truncate(output_start);
                return None;
            }
            (((word & 0x0101010101010101).wrapping_mul(0x8040201008040201)) >> 56) as u8
        } else {
            let mut packed = 0u8;
            for value in chunk {
                if !matches!(value, b'0' | b'1') {
                    out.truncate(output_start);
                    return None;
                }
                packed = (packed << 1) | (value - b'0');
            }
            packed << (8 - chunk.len())
        };
        let destination_bit = bit_offset + chunk_index * 8;
        let destination_byte = output_start + destination_bit / 8;
        let shift = destination_bit & 7;
        out[destination_byte] |= packed >> shift;
        if shift != 0 && destination_byte + 1 < out.len() {
            out[destination_byte + 1] |= packed << (8 - shift);
        }
    }
    Some(len > width)
}

fn unpack_binary_bits_into(bytes: &[u8], out: &mut [u8]) {
    for (index, output) in out.iter_mut().enumerate() {
        let bit = (bytes[index / 8] >> (7 - (index & 7))) & 1;
        *output = b'0' + bit;
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

#[derive(Clone, Copy)]
struct GroupMeta {
    fst_id: fst_writer::FstSignalId,
    width: u32,
    is_real: bool,
    frame_offset: usize,
}

#[derive(Clone)]
struct SignalSchema {
    single_char_idx: [u32; 256],
    two_char_idx: Option<Box<[u32]>>,
    three_char_idx: Option<Box<[u32]>>,
    multi_char_idx: FxHashMap<Vec<u8>, u32>,
    groups: Vec<GroupMeta>,
}

const VCD_ID_ALPHABET: usize = 94;

#[inline(always)]
fn dense_vcd_id_index(id: &[u8], expected_len: usize) -> Option<usize> {
    if id.len() != expected_len {
        return None;
    }
    let mut index = 0usize;
    for byte in id {
        if !(b'!'..=b'~').contains(byte) {
            return None;
        }
        index = index * VCD_ID_ALPHABET + (byte - b'!') as usize;
    }
    Some(index)
}

impl SignalSchema {
    #[inline(always)]
    fn lookup_group(&self, id: &[u8]) -> u32 {
        match id.len() {
            1 => self.single_char_idx[id[0] as usize],
            2 => dense_vcd_id_index(id, 2)
                .and_then(|index| self.two_char_idx.as_ref().map(|table| table[index]))
                .unwrap_or_else(|| self.multi_char_idx.get(id).copied().unwrap_or(NO_GROUP)),
            3 => dense_vcd_id_index(id, 3)
                .and_then(|index| self.three_char_idx.as_ref().map(|table| table[index]))
                .unwrap_or_else(|| self.multi_char_idx.get(id).copied().unwrap_or(NO_GROUP)),
            _ => self.multi_char_idx.get(id).copied().unwrap_or(NO_GROUP),
        }
    }
}

struct ConversionState {
    current_tick: u64,
    last_written_time: u64,
    any_time_written: bool,
    in_dumpoff: bool,
    val_buf: Vec<u8>,
    overwide_values: u64,
    write_error: Option<String>,
    frame: Vec<u8>,
}

struct IrTimestamp {
    tick: u64,
}

struct ChunkSignalChain {
    signal: u32,
    prefix_last: u32,
    enabled_last: u32,
}

struct ChunkEvents {
    sequence: u64,
    chunk_count: u64,
    input_bytes: usize,
    fst_stream_bytes: usize,
    changes: Vec<fst_writer::FstSignalRecord>,
    chains: Vec<ChunkSignalChain>,
    timestamps: Vec<IrTimestamp>,
    values: Vec<u8>,
    prefix_overwide_values: u64,
    enabled_overwide_values: u64,
    max_timestamp: Option<u64>,
    final_dump_enabled: Option<bool>,
    recycle_chain_slots: Vec<u32>,
    #[cfg(feature = "profile")]
    parse_cpu_seconds: f64,
    #[cfg(feature = "profile")]
    parse_wall_seconds: f64,
    #[cfg(feature = "profile")]
    recycled_capacity_bytes: usize,
    #[cfg(feature = "profile")]
    newly_allocated_capacity_bytes: usize,
}

struct ChunkBuffers {
    changes: Vec<fst_writer::FstSignalRecord>,
    chains: Vec<ChunkSignalChain>,
    timestamps: Vec<IrTimestamp>,
    values: Vec<u8>,
    chain_slots: Vec<u32>,
}

impl ChunkBuffers {
    fn new(input_bytes: usize) -> Self {
        Self {
            changes: Vec::with_capacity(input_bytes / 12),
            chains: Vec::new(),
            timestamps: Vec::with_capacity(input_bytes / 256),
            values: Vec::with_capacity(input_bytes / 4),
            chain_slots: Vec::new(),
        }
    }

    fn clear(&mut self) {
        self.changes.clear();
        self.chains.clear();
        self.timestamps.clear();
        self.values.clear();
    }

    #[cfg(feature = "profile")]
    fn vector_capacity_bytes(&self) -> usize {
        self.changes.capacity() * std::mem::size_of::<fst_writer::FstSignalRecord>()
            + self.chains.capacity() * std::mem::size_of::<ChunkSignalChain>()
            + self.timestamps.capacity() * std::mem::size_of::<IrTimestamp>()
            + self.values.capacity()
            + self.chain_slots.capacity() * std::mem::size_of::<u32>()
    }
}

struct EncodeSectionStart {
    sequence: u64,
    incoming_frame: Vec<u8>,
    incoming_tick: u64,
    incoming_time_seen: bool,
    incoming_dumpoff: bool,
    first_file_section: bool,
}

enum EncodeMessage {
    Start(EncodeSectionStart),
    Chunk(ChunkEvents),
    Finish,
}

struct EncodedChunk {
    sequence: u64,
    section: fst_writer::EncodedFstSection,
    #[cfg(feature = "profile")]
    encode_seconds: f64,
    #[cfg(feature = "profile")]
    pack_cpu_seconds: f64,
    #[cfg(feature = "profile")]
    compression_seconds: f64,
    #[cfg(feature = "profile")]
    compression_cpu_seconds: f64,
    #[cfg(feature = "profile")]
    packer_input_bytes: usize,
    #[cfg(feature = "profile")]
    packer_worker_cpu_seconds: Vec<f64>,
    #[cfg(feature = "profile")]
    recycled_capacity_bytes: usize,
    #[cfg(feature = "profile")]
    newly_allocated_capacity_bytes: usize,
    #[cfg(feature = "profile")]
    arena_to_packer_copied_bytes: usize,
}

struct EncodeTimings {
    #[cfg(feature = "profile")]
    encode_seconds: f64,
    #[cfg(feature = "profile")]
    write_seconds: f64,
    #[cfg(feature = "profile")]
    pack_cpu_seconds: f64,
    #[cfg(feature = "profile")]
    compression_seconds: f64,
    #[cfg(feature = "profile")]
    compression_cpu_seconds: f64,
    #[cfg(feature = "profile")]
    assembler_cpu_seconds: f64,
    #[cfg(feature = "profile")]
    packer_input_bytes: usize,
    #[cfg(feature = "profile")]
    packer_worker_cpu_seconds: Vec<f64>,
    #[cfg(feature = "profile")]
    recycled_capacity_bytes: usize,
    #[cfg(feature = "profile")]
    newly_allocated_capacity_bytes: usize,
    #[cfg(feature = "profile")]
    arena_to_packer_copied_bytes: usize,
}

struct ActiveSectionEncoder {
    sequence: u64,
    encoder: fst_writer::FstSignalChainEncoder,
    current_dump_enabled: bool,
    current_tick: u64,
    time_seen: bool,
    encode_seconds: f64,
}

#[cfg(feature = "profile")]
#[derive(Default)]
struct ParallelProfile {
    parse_seconds: f64,
    parse_cpu_seconds: f64,
    reconcile_seconds: f64,
    coordinator_cpu_seconds: f64,
    encode_seconds: f64,
    pack_cpu_seconds: f64,
    compression_seconds: f64,
    compression_cpu_seconds: f64,
    write_seconds: f64,
    assembler_cpu_seconds: f64,
    input_bytes: usize,
    representation_bytes: usize,
    peak_batch_representation_bytes: usize,
    event_count: usize,
    value_arena_bytes: usize,
    touched_signal_chains: usize,
    packer_input_bytes: usize,
    packer_worker_cpu_seconds: Vec<f64>,
    encoder_queue_block_seconds: f64,
    encoder_queue_high_water: usize,
    recycled_capacity_bytes: usize,
    newly_allocated_capacity_bytes: usize,
    arena_to_packer_copied_bytes: usize,
}

#[cfg(feature = "profile")]
impl ParallelProfile {
    fn record_queue(&mut self, metrics: (f64, usize)) {
        self.encoder_queue_block_seconds += metrics.0;
        self.encoder_queue_high_water = self.encoder_queue_high_water.max(metrics.1);
    }

    fn record_packer_workers(&mut self, cpu_seconds: &[f64]) {
        self.packer_worker_cpu_seconds.resize(
            self.packer_worker_cpu_seconds.len().max(cpu_seconds.len()),
            0.0,
        );
        for (total, section) in self.packer_worker_cpu_seconds.iter_mut().zip(cpu_seconds) {
            *total += section;
        }
    }
}

#[cfg(feature = "profile")]
fn chunk_representation_bytes(ir: &ChunkEvents) -> usize {
    ir.changes.len() * std::mem::size_of::<fst_writer::FstSignalRecord>()
        + ir.chains.len() * std::mem::size_of::<ChunkSignalChain>()
        + ir.timestamps.len() * std::mem::size_of::<IrTimestamp>()
        + ir.values.len()
}

struct ChunkParser<'a> {
    schema: &'a SignalSchema,
    ir: ChunkEvents,
    chain_slots: Vec<u32>,
    local_dump_enabled: Option<bool>,
    last_timestamp: Option<u64>,
    current_time_ordinal: u32,
}

impl<'a> ChunkParser<'a> {
    fn new(schema: &'a SignalSchema, chunk: &VcdChunk, recycled: Option<ChunkBuffers>) -> Self {
        #[cfg(feature = "profile")]
        let was_recycled = recycled.is_some();
        let mut buffers = recycled.unwrap_or_else(|| ChunkBuffers::new(chunk.bytes.len()));
        buffers.clear();
        if buffers.chain_slots.len() != schema.groups.len() {
            buffers.chain_slots = vec![u32::MAX; schema.groups.len()];
        }
        #[cfg(feature = "profile")]
        let initial_capacity = buffers.vector_capacity_bytes();
        Self {
            schema,
            ir: ChunkEvents {
                sequence: chunk.sequence,
                chunk_count: 1,
                input_bytes: chunk.bytes.len(),
                fst_stream_bytes: 0,
                changes: buffers.changes,
                chains: buffers.chains,
                timestamps: buffers.timestamps,
                values: buffers.values,
                prefix_overwide_values: 0,
                enabled_overwide_values: 0,
                max_timestamp: None,
                final_dump_enabled: None,
                recycle_chain_slots: Vec::new(),
                #[cfg(feature = "profile")]
                parse_cpu_seconds: 0.0,
                #[cfg(feature = "profile")]
                parse_wall_seconds: 0.0,
                #[cfg(feature = "profile")]
                recycled_capacity_bytes: if was_recycled { initial_capacity } else { 0 },
                #[cfg(feature = "profile")]
                newly_allocated_capacity_bytes: if was_recycled { 0 } else { initial_capacity },
            },
            chain_slots: buffers.chain_slots,
            local_dump_enabled: None,
            last_timestamp: None,
            current_time_ordinal: fst_writer::FST_FRAME_TIME_INDEX,
        }
    }

    fn parse(mut self, chunk: &VcdChunk) -> Result<ChunkEvents, VcdParseError> {
        #[cfg(feature = "profile")]
        let cpu_started = thread_cpu_seconds();
        #[cfg(feature = "profile")]
        let wall_started = Instant::now();
        let mut start = 0;
        while let Some(relative) = memchr(b'\n', &chunk.bytes[start..]) {
            let line_end = start + relative;
            self.parse_line_ending_at(chunk, start, line_end)?;
            start = line_end + 1;
        }
        if start < chunk.bytes.len() {
            self.parse_line_ending_at(chunk, start, chunk.bytes.len())?;
        }
        #[cfg(feature = "profile")]
        {
            self.ir.parse_cpu_seconds = thread_cpu_seconds() - cpu_started;
            self.ir.parse_wall_seconds = wall_started.elapsed().as_secs_f64();
            let final_capacity = self.ir.changes.capacity()
                * std::mem::size_of::<fst_writer::FstSignalRecord>()
                + self.ir.chains.capacity() * std::mem::size_of::<ChunkSignalChain>()
                + self.ir.timestamps.capacity() * std::mem::size_of::<IrTimestamp>()
                + self.ir.values.capacity()
                + self.chain_slots.capacity() * std::mem::size_of::<u32>();
            let initial_capacity =
                self.ir.recycled_capacity_bytes + self.ir.newly_allocated_capacity_bytes;
            self.ir.newly_allocated_capacity_bytes +=
                final_capacity.saturating_sub(initial_capacity);
        }
        for chain in &self.ir.chains {
            self.chain_slots[chain.signal as usize] = u32::MAX;
        }
        self.ir.recycle_chain_slots = self.chain_slots;
        Ok(self.ir)
    }

    fn parse_line_ending_at(
        &mut self,
        chunk: &VcdChunk,
        start: usize,
        mut end: usize,
    ) -> Result<(), VcdParseError> {
        if end > start && chunk.bytes[end - 1] == b'\r' {
            end -= 1;
        }
        if end > start {
            self.parse_line(&chunk.bytes[start..end], chunk.start_offset + start as u64)?;
        }
        Ok(())
    }

    fn parse_line(&mut self, line: &[u8], input_offset: u64) -> Result<(), VcdParseError> {
        match line[0] {
            b'#' => {
                let tick = parse_timestamp(line, input_offset)?;
                self.ir.max_timestamp =
                    Some(self.ir.max_timestamp.map_or(tick, |old| old.max(tick)));
                self.push_timestamp(tick)?;
            }
            b'0' | b'1' | b'x' | b'z' | b'X' | b'Z' => {
                let value = [line[0].to_ascii_lowercase()];
                self.push_change(&line[1..], &value, true)?;
            }
            b'b' | b'B' | b'r' | b'R' => self.parse_vector(line)?,
            b'$' if line.starts_with(b"$dumpoff") => {
                self.local_dump_enabled = Some(false);
                self.ir.final_dump_enabled = Some(false);
            }
            b'$' if line.starts_with(b"$dumpon") => {
                self.local_dump_enabled = Some(true);
                self.ir.final_dump_enabled = Some(true);
            }
            _ => {}
        }
        Ok(())
    }

    fn push_timestamp(&mut self, tick: u64) -> Result<(), VcdParseError> {
        let payload =
            u32::try_from(self.ir.timestamps.len()).map_err(|_| VcdParseError::Worker {
                message: "chunk timestamp table exceeds u32".to_string(),
            })?;
        self.ir.timestamps.push(IrTimestamp { tick });
        self.current_time_ordinal = payload;
        let delta = self
            .last_timestamp
            .map_or(tick, |previous| tick.saturating_sub(previous));
        self.last_timestamp = Some(self.last_timestamp.map_or(tick, |old| old.max(tick)));
        self.ir.fst_stream_bytes += encoded_varint_len(delta);
        Ok(())
    }

    fn parse_vector(&mut self, line: &[u8]) -> Result<(), VcdParseError> {
        let Some(separator) = memchr2(b' ', b'\t', line) else {
            return Ok(());
        };
        let id_start = if separator + 1 < line.len() && matches!(line[separator + 1], b' ' | b'\t')
        {
            separator + 2
        } else {
            separator + 1
        };
        self.push_change(&line[id_start..], &line[1..separator], false)
    }

    fn push_change(&mut self, id: &[u8], raw: &[u8], ready: bool) -> Result<(), VcdParseError> {
        let group = self.schema.lookup_group(id);
        if group == NO_GROUP {
            return Ok(());
        }
        let meta = self.schema.groups[group as usize];
        let dump_state = match self.local_dump_enabled {
            None => fst_writer::FstDumpState::Prefix,
            Some(true) => fst_writer::FstDumpState::Enabled,
            Some(false) => fst_writer::FstDumpState::Suppressed,
        };
        if ready && meta.width == 1 && !meta.is_real {
            self.ir.fst_stream_bytes += 1;
            let change = fst_writer::FstSignalRecord::inline(
                group,
                self.current_time_ordinal,
                raw[0],
                dump_state,
            )
            .map_err(|error| worker_error(self.ir.sequence, error))?;
            self.push_record(change)?;
            return Ok(());
        }
        let start = u32::try_from(self.ir.values.len()).map_err(|_| VcdParseError::Worker {
            message: "chunk value arena exceeds u32".to_string(),
        })?;
        let (packed_binary, overwide) = if meta.is_real {
            let value = std::str::from_utf8(raw)
                .ok()
                .and_then(|text| text.parse::<f64>().ok())
                .unwrap_or(f64::NAN);
            self.ir.values.extend_from_slice(&value.to_le_bytes());
            (false, false)
        } else if let Some(overwide) =
            try_pack_binary_bits_append(raw, meta.width as usize, &mut self.ir.values)
        {
            (true, overwide)
        } else {
            (
                false,
                canon_bits_append(raw, meta.width as usize, &mut self.ir.values),
            )
        };
        if overwide {
            match self.local_dump_enabled {
                None => self.ir.prefix_overwide_values += 1,
                Some(true) => self.ir.enabled_overwide_values += 1,
                Some(false) => {}
            }
        }
        let value_bytes = self.ir.values.len() - start as usize;
        self.ir.fst_stream_bytes += 1 + value_bytes;
        let change = if packed_binary {
            fst_writer::FstSignalRecord::packed_binary(
                group,
                self.current_time_ordinal,
                start,
                dump_state,
            )
        } else {
            fst_writer::FstSignalRecord::arena(group, self.current_time_ordinal, start, dump_state)
        }
        .map_err(|error| worker_error(self.ir.sequence, error))?;
        self.push_record(change)?;
        Ok(())
    }

    #[inline(always)]
    fn push_record(&mut self, change: fst_writer::FstSignalRecord) -> Result<(), VcdParseError> {
        let record = u32::try_from(self.ir.changes.len()).map_err(|_| VcdParseError::Worker {
            message: "chunk change table exceeds u32".to_string(),
        })?;
        let signal = change.signal();
        self.ir.changes.push(change);
        let slot = self.chain_slots[signal as usize];
        if slot != u32::MAX {
            let chain = &mut self.ir.chains[slot as usize];
            match change.dump_state() {
                fst_writer::FstDumpState::Prefix => chain.prefix_last = record,
                fst_writer::FstDumpState::Enabled => chain.enabled_last = record,
                fst_writer::FstDumpState::Suppressed => {}
            }
        } else {
            self.chain_slots[signal as usize] = u32::try_from(self.ir.chains.len())
                .map_err(|_| worker_error(self.ir.sequence, "chunk chain table exceeds u32"))?;
            self.ir.chains.push(ChunkSignalChain {
                signal,
                prefix_last: if change.dump_state() == fst_writer::FstDumpState::Prefix {
                    record
                } else {
                    fst_writer::FST_NO_CHANGE
                },
                enabled_last: if change.dump_state() == fst_writer::FstDumpState::Enabled {
                    record
                } else {
                    fst_writer::FST_NO_CHANGE
                },
            });
        }
        Ok(())
    }
}

impl ConversionState {
    fn new(frame_bytes: usize) -> Self {
        Self {
            current_tick: 0,
            last_written_time: 0,
            any_time_written: false,
            in_dumpoff: false,
            val_buf: Vec::with_capacity(256),
            overwide_values: 0,
            write_error: None,
            frame: vec![b'x'; frame_bytes],
        }
    }
}

fn invalid_timestamp(line: &[u8], offset: u64, reason: TimestampError) -> VcdParseError {
    VcdParseError::InvalidTimestamp {
        offset,
        text: String::from_utf8_lossy(line).into_owned(),
        reason,
    }
}

fn parse_timestamp(line: &[u8], offset: u64) -> Result<u64, VcdParseError> {
    let mut pos = 1;
    while pos < line.len() && matches!(line[pos], b' ' | b'\t') {
        pos += 1;
    }

    let digit_start = pos;
    let mut tick = 0u64;
    while pos < line.len() && line[pos].is_ascii_digit() {
        tick = tick
            .checked_mul(10)
            .and_then(|value| value.checked_add((line[pos] - b'0') as u64))
            .ok_or_else(|| invalid_timestamp(line, offset, TimestampError::Overflow))?;
        pos += 1;
    }
    if pos == digit_start {
        return Err(invalid_timestamp(
            line,
            offset,
            TimestampError::MissingDigits,
        ));
    }
    while pos < line.len() && matches!(line[pos], b' ' | b'\t') {
        pos += 1;
    }
    if pos != line.len() {
        return Err(invalid_timestamp(
            line,
            offset,
            TimestampError::TrailingCharacters,
        ));
    }
    Ok(tick)
}

fn encoded_varint_len(value: u64) -> usize {
    ((u64::BITS - value.leading_zeros()).max(1) as usize).div_ceil(7)
}

#[cfg(feature = "profile")]
fn thread_cpu_seconds() -> f64 {
    let mut value = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    // SAFETY: `value` points to writable storage for the duration of the call.
    let result = unsafe { libc::clock_gettime(libc::CLOCK_THREAD_CPUTIME_ID, &mut value) };
    if result == 0 {
        value.tv_sec as f64 + value.tv_nsec as f64 / 1_000_000_000.0
    } else {
        0.0
    }
}

fn panic_message(payload: Box<dyn std::any::Any + Send>) -> String {
    if let Some(message) = payload.downcast_ref::<&str>() {
        (*message).to_string()
    } else if let Some(message) = payload.downcast_ref::<String>() {
        message.clone()
    } else {
        "worker panicked with a non-string payload".to_string()
    }
}

fn write_parallel_heartbeat(
    heartbeat_path: Option<&Path>,
    bytes: u64,
    tick: u64,
    last_heartbeat: &mut Instant,
) {
    if last_heartbeat.elapsed() < Duration::from_secs(5) {
        return;
    }
    if let Some(path) = heartbeat_path {
        if let Ok(mut file) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)
        {
            let _ = writeln!(file, "bytes={bytes} tick={tick}");
        }
    }
    *last_heartbeat = Instant::now();
}

impl ActiveSectionEncoder {
    fn new(
        factory: &fst_writer::FstSectionEncoder,
        start: EncodeSectionStart,
    ) -> Result<Self, VcdParseError> {
        let started = Instant::now();
        let encoder = if start.first_file_section {
            factory.fresh_signal_chains()
        } else {
            factory.signal_chains_from_frame(&start.incoming_frame, start.incoming_tick)
        }
        .map_err(|error| worker_error(start.sequence, error))?;
        Ok(Self {
            sequence: start.sequence,
            encoder,
            current_dump_enabled: !start.incoming_dumpoff,
            current_tick: start.incoming_tick,
            time_seen: start.incoming_time_seen,
            encode_seconds: started.elapsed().as_secs_f64(),
        })
    }

    fn apply(&mut self, mut ir: ChunkEvents) -> Result<ChunkBuffers, VcdParseError> {
        let started = Instant::now();
        let incoming_time_index = self.encoder.current_time_index();
        let mut time_map = Vec::with_capacity(ir.timestamps.len());
        for timestamp in &ir.timestamps {
            if timestamp.tick > self.current_tick || !self.time_seen {
                let advanced = timestamp.tick > self.current_tick;
                self.current_tick = timestamp.tick;
                self.time_seen = true;
                if advanced {
                    self.encoder
                        .time_change(self.current_tick)
                        .map_err(|error| worker_error(self.sequence, error))?;
                }
            }
            time_map.push(self.encoder.current_time_index());
        }
        for change in &mut ir.changes {
            if change.dump_state() == fst_writer::FstDumpState::Prefix {
                change.set_dump_state(if self.current_dump_enabled {
                    fst_writer::FstDumpState::Enabled
                } else {
                    fst_writer::FstDumpState::Suppressed
                });
            }
            let time_index = if change.time_index() == fst_writer::FST_FRAME_TIME_INDEX {
                incoming_time_index
            } else {
                *time_map.get(change.time_index() as usize).ok_or_else(|| {
                    worker_error(self.sequence, "change timestamp ordinal is out of bounds")
                })?
            };
            change.set_time_index(time_index);
        }
        self.encoder
            .apply_signal_records(&ir.changes, &ir.values)
            .map_err(|error| worker_error(self.sequence, error))?;
        if let Some(enabled) = ir.final_dump_enabled {
            self.current_dump_enabled = enabled;
        }
        self.encode_seconds += started.elapsed().as_secs_f64();
        ir.chains.clear();
        ir.timestamps.clear();
        Ok(ChunkBuffers {
            changes: ir.changes,
            chains: ir.chains,
            timestamps: ir.timestamps,
            values: ir.values,
            chain_slots: ir.recycle_chain_slots,
        })
    }

    fn finish(mut self) -> Result<EncodedChunk, VcdParseError> {
        let started = Instant::now();
        let section = self
            .encoder
            .encode_section()
            .map_err(|error| worker_error(self.sequence, error))?;
        let compression_seconds = started.elapsed().as_secs_f64();
        #[cfg(feature = "profile")]
        let compression_cpu_seconds = section.compression_cpu_seconds();
        #[cfg(feature = "profile")]
        let packer_input_bytes = section.packer_input_bytes();
        #[cfg(feature = "profile")]
        let packer_worker_cpu_seconds = section.worker_cpu_seconds().to_vec();
        #[cfg(feature = "profile")]
        let pack_cpu_seconds = section.pack_cpu_seconds();
        #[cfg(feature = "profile")]
        let recycled_capacity_bytes = section.recycled_capacity_bytes();
        #[cfg(feature = "profile")]
        let newly_allocated_capacity_bytes = section.newly_allocated_capacity_bytes();
        #[cfg(feature = "profile")]
        let arena_to_packer_copied_bytes = section.arena_to_packer_copied_bytes();
        self.encode_seconds += compression_seconds;
        Ok(EncodedChunk {
            sequence: self.sequence,
            section,
            #[cfg(feature = "profile")]
            encode_seconds: self.encode_seconds,
            #[cfg(feature = "profile")]
            pack_cpu_seconds,
            #[cfg(feature = "profile")]
            compression_seconds,
            #[cfg(feature = "profile")]
            compression_cpu_seconds,
            #[cfg(feature = "profile")]
            packer_input_bytes,
            #[cfg(feature = "profile")]
            packer_worker_cpu_seconds,
            #[cfg(feature = "profile")]
            recycled_capacity_bytes,
            #[cfg(feature = "profile")]
            newly_allocated_capacity_bytes,
            #[cfg(feature = "profile")]
            arena_to_packer_copied_bytes,
        })
    }
}

fn encode_worker_loop(
    factory: &fst_writer::FstSectionEncoder,
    receiver: mpsc::Receiver<EncodeMessage>,
    sender: &mpsc::SyncSender<Result<EncodedChunk, VcdParseError>>,
    recycle_sender: &mpsc::Sender<ChunkBuffers>,
) -> Result<(), VcdParseError> {
    let mut active: Option<ActiveSectionEncoder> = None;
    while let Ok(message) = receiver.recv() {
        match message {
            EncodeMessage::Start(start) => {
                if active.is_some() {
                    return Err(worker_error(start.sequence, "section already active"));
                }
                active = Some(ActiveSectionEncoder::new(factory, start)?);
            }
            EncodeMessage::Chunk(ir) => {
                let buffers = active
                    .as_mut()
                    .ok_or_else(|| worker_error(ir.sequence, "section not started"))?
                    .apply(ir)?;
                let _ = recycle_sender.send(buffers);
            }
            EncodeMessage::Finish => {
                let encoder = active
                    .take()
                    .ok_or_else(|| worker_error(0, "section not started"))?;
                if sender.send(encoder.finish()).is_err() {
                    return Ok(());
                }
            }
        }
    }
    if active.is_some() {
        return Err(worker_error(
            0,
            "encoder channel closed with an active section",
        ));
    }
    Ok(())
}

fn worker_error(sequence: u64, error: impl std::fmt::Display) -> VcdParseError {
    VcdParseError::Worker {
        message: format!("chunk {sequence}: {error}"),
    }
}

/// Streaming VCD -> FST build handler (the replacement for the retired
/// `.bwave` builder). Construct with the parsed VCD header, feed the body
/// through `parse_bytes`, then `finalize_and_write`.
pub struct FstBuildHandler {
    encoder: fst_writer::FstSectionEncoder,
    writer: fst_writer::OrderedFstWriter<std::io::BufWriter<File>>,
    output_path: std::path::PathBuf,
    body_offset: u64,
    schema: SignalSchema,
    state: ConversionState,
    sections_encoded_externally: bool,
}

impl FstBuildHandler {
    /// Parse the header, emit the FST hierarchy, and return a handler ready
    /// to stream the VCD body. `scope` limits the store to a hierarchical
    /// subtree exactly like the .bwave builder.
    pub fn new(
        header: &VcdHeader,
        scope: Option<&str>,
        output_path: &Path,
    ) -> Result<FstBuildHandler, String> {
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

        let info = fst_writer::FstInfo {
            start_time: 0,
            timescale_exponent: timescale_to_exponent(&header.timescale_str),
            version: format!("bwave {}", env!("CARGO_PKG_VERSION")),
            date: String::new(),
            file_type: fst_writer::FstFileType::Verilog,
        };
        let mut hw = fst_writer::open_fst(output_path, &info)
            .map_err(|e| format!("cannot create '{}': {e}", output_path.display()))?;

        // Emit vars in exact VCD declaration order, streaming scope
        // transitions between consecutive signals. The first declaration of
        // a VCD id becomes the FST signal, later declarations alias it.
        let mut single_char_idx = [NO_GROUP; 256];
        let mut two_char_idx =
            Some(vec![NO_GROUP; VCD_ID_ALPHABET * VCD_ID_ALPHABET].into_boxed_slice());
        let mut three_char_idx = signals
            .iter()
            .any(|signal| dense_vcd_id_index(signal.id.as_bytes(), 3).is_some())
            .then(|| {
                vec![NO_GROUP; VCD_ID_ALPHABET * VCD_ID_ALPHABET * VCD_ID_ALPHABET]
                    .into_boxed_slice()
            });
        let mut multi_char_idx = FxHashMap::<Vec<u8>, u32>::default();
        let mut groups: Vec<GroupMeta> = Vec::new();
        let mut current_scope: Vec<String> = Vec::new();
        for sig in &signals {
            let mut parts: Vec<&str> = sig.name.split('.').collect();
            let var_name = parts.pop().unwrap_or(&sig.name);
            transition_scopes(&mut hw, &mut current_scope, &parts)?;

            let is_real = sig.var_type == "real" || sig.var_type == "realtime";
            let signal_tpe = if is_real {
                fst_writer::FstSignalType::real()
            } else {
                fst_writer::FstSignalType::bit_vec(sig.width)
            };
            let id_bytes = sig.id.as_bytes();
            let existing = match id_bytes.len() {
                1 => single_char_idx[id_bytes[0] as usize],
                2 => dense_vcd_id_index(id_bytes, 2)
                    .and_then(|index| two_char_idx.as_ref().map(|table| table[index]))
                    .unwrap_or_else(|| multi_char_idx.get(id_bytes).copied().unwrap_or(NO_GROUP)),
                3 => dense_vcd_id_index(id_bytes, 3)
                    .and_then(|index| three_char_idx.as_ref().map(|table| table[index]))
                    .unwrap_or_else(|| multi_char_idx.get(id_bytes).copied().unwrap_or(NO_GROUP)),
                _ => multi_char_idx.get(id_bytes).copied().unwrap_or(NO_GROUP),
            };
            let alias = if existing != NO_GROUP {
                Some(groups[existing as usize].fst_id)
            } else {
                None
            };
            let fst_id = hw
                .var(
                    var_name,
                    signal_tpe,
                    var_type_of(&sig.var_type),
                    fst_writer::FstVarDirection::Implicit,
                    alias,
                )
                .map_err(|e| format!("fst var '{}': {e}", sig.name))?;
            if existing == NO_GROUP {
                let frame_offset = groups
                    .last()
                    .map(|group| {
                        group.frame_offset
                            + if group.is_real {
                                8
                            } else {
                                group.width as usize
                            }
                    })
                    .unwrap_or(0);
                groups.push(GroupMeta {
                    fst_id,
                    width: sig.width,
                    is_real,
                    frame_offset,
                });
                let g = (groups.len() - 1) as u32;
                match id_bytes.len() {
                    1 => single_char_idx[id_bytes[0] as usize] = g,
                    2 => {
                        if let Some(index) = dense_vcd_id_index(id_bytes, 2) {
                            two_char_idx.as_mut().expect("two-character table")[index] = g;
                        } else {
                            multi_char_idx.insert(id_bytes.to_vec(), g);
                        }
                    }
                    3 => {
                        if let Some(index) = dense_vcd_id_index(id_bytes, 3) {
                            three_char_idx.as_mut().expect("three-character table")[index] = g;
                        } else {
                            multi_char_idx.insert(id_bytes.to_vec(), g);
                        }
                    }
                    _ => {
                        multi_char_idx.insert(id_bytes.to_vec(), g);
                    }
                }
            }
        }
        transition_scopes(&mut hw, &mut current_scope, &[])?;

        let (encoder, writer) = hw
            .finish_split()
            .map_err(|e| format!("fst header finish: {e}"))?;
        let frame_bytes = groups
            .last()
            .map(|group| {
                group.frame_offset
                    + if group.is_real {
                        8
                    } else {
                        group.width as usize
                    }
            })
            .unwrap_or(0);
        Ok(FstBuildHandler {
            encoder,
            writer,
            output_path: output_path.to_path_buf(),
            body_offset: header.body_offset,
            schema: SignalSchema {
                single_char_idx,
                two_char_idx,
                three_char_idx,
                multi_char_idx,
                groups,
            },
            state: ConversionState::new(frame_bytes),
            sections_encoded_externally: false,
        })
    }

    fn record_write_error(&mut self, e: impl std::fmt::Display) {
        if self.state.write_error.is_none() {
            self.state.write_error = Some(e.to_string());
        }
    }

    fn apply_timestamp(&mut self, tick: u64) {
        if tick > self.state.current_tick || !self.state.any_time_written {
            self.state.current_tick = tick;
        }
        if tick <= self.state.last_written_time && self.state.any_time_written {
            return;
        }
        if self.encoder.size() >= FST_FLUSH_THRESHOLD {
            self.flush_section();
        }
        match self.encoder.time_change(tick) {
            Ok(()) => {
                self.state.last_written_time = tick;
                self.state.any_time_written = true;
            }
            Err(error) => self.record_write_error(error),
        }
    }

    fn flush_section(&mut self) {
        match self.encoder.encode_section() {
            Ok(section) => {
                if let Err(error) = self.writer.append_section(section) {
                    self.record_write_error(error);
                }
            }
            Err(error) => self.record_write_error(error),
        }
    }

    fn accept_encoded(
        &mut self,
        result: Result<EncodedChunk, VcdParseError>,
        submitted: &mut VecDeque<u64>,
        completed: &mut BTreeMap<u64, fst_writer::EncodedFstSection>,
    ) -> Result<EncodeTimings, VcdParseError> {
        let chunk = result?;
        #[cfg(feature = "profile")]
        let encode_seconds = chunk.encode_seconds;
        #[cfg(feature = "profile")]
        let pack_cpu_seconds = chunk.pack_cpu_seconds;
        #[cfg(feature = "profile")]
        let compression_seconds = chunk.compression_seconds;
        #[cfg(feature = "profile")]
        let compression_cpu_seconds = chunk.compression_cpu_seconds;
        #[cfg(feature = "profile")]
        let packer_input_bytes = chunk.packer_input_bytes;
        #[cfg(feature = "profile")]
        let packer_worker_cpu_seconds = chunk.packer_worker_cpu_seconds;
        #[cfg(feature = "profile")]
        let recycled_capacity_bytes = chunk.recycled_capacity_bytes;
        #[cfg(feature = "profile")]
        let newly_allocated_capacity_bytes = chunk.newly_allocated_capacity_bytes;
        #[cfg(feature = "profile")]
        let arena_to_packer_copied_bytes = chunk.arena_to_packer_copied_bytes;
        completed.insert(chunk.sequence, chunk.section);
        #[cfg(feature = "profile")]
        let write_started = Instant::now();
        #[cfg(feature = "profile")]
        let cpu_started = thread_cpu_seconds();
        while let Some(sequence) = submitted.front().copied() {
            let Some(section) = completed.remove(&sequence) else {
                break;
            };
            submitted.pop_front();
            if let Err(error) = self.writer.append_section(section) {
                self.record_write_error(error);
            }
            self.sections_encoded_externally = true;
        }
        Ok(EncodeTimings {
            #[cfg(feature = "profile")]
            encode_seconds,
            #[cfg(feature = "profile")]
            write_seconds: write_started.elapsed().as_secs_f64(),
            #[cfg(feature = "profile")]
            pack_cpu_seconds,
            #[cfg(feature = "profile")]
            compression_seconds,
            #[cfg(feature = "profile")]
            compression_cpu_seconds,
            #[cfg(feature = "profile")]
            assembler_cpu_seconds: thread_cpu_seconds() - cpu_started,
            #[cfg(feature = "profile")]
            packer_input_bytes,
            #[cfg(feature = "profile")]
            packer_worker_cpu_seconds,
            #[cfg(feature = "profile")]
            recycled_capacity_bytes,
            #[cfg(feature = "profile")]
            newly_allocated_capacity_bytes,
            #[cfg(feature = "profile")]
            arena_to_packer_copied_bytes,
        })
    }

    fn send_encode_message(
        sender: &mpsc::SyncSender<EncodeMessage>,
        message: EncodeMessage,
    ) -> Result<(f64, usize), VcdParseError> {
        match sender.try_send(message) {
            Ok(()) => Ok((0.0, 1)),
            Err(mpsc::TrySendError::Full(message)) => {
                let started = Instant::now();
                sender.send(message).map_err(|_| VcdParseError::Worker {
                    message: "encoder worker queue disconnected".to_string(),
                })?;
                Ok((started.elapsed().as_secs_f64(), 1))
            }
            Err(mpsc::TrySendError::Disconnected(_)) => Err(VcdParseError::Worker {
                message: "encoder worker queue disconnected".to_string(),
            }),
        }
    }

    #[inline]
    fn emit_change(&mut self, group: u32, bytes_are_ready: bool, raw: &[u8]) {
        let g = self.schema.groups[group as usize];
        let fst_id = g.fst_id;
        if g.is_real {
            let v: f64 = std::str::from_utf8(raw)
                .ok()
                .and_then(|s| s.parse().ok())
                .unwrap_or(f64::NAN);
            let bytes = v.to_le_bytes();
            self.store_frame(g, &bytes);
            if let Err(e) = self.encoder.signal_change_exact(fst_id, &bytes) {
                self.record_write_error(e);
            }
            return;
        }
        if bytes_are_ready {
            self.store_frame(g, raw);
            if let Err(e) = self.encoder.signal_change_exact(fst_id, raw) {
                self.record_write_error(e);
            }
        } else {
            let width = g.width as usize;
            let mut buf = std::mem::take(&mut self.state.val_buf);
            if canon_bits_into(raw, width, &mut buf) {
                self.state.overwide_values += 1;
            }
            self.store_frame(g, &buf);
            if let Err(e) = self.encoder.signal_change_exact(fst_id, &buf) {
                self.record_write_error(e);
            }
            self.state.val_buf = buf;
        }
    }

    fn store_frame(&mut self, group: GroupMeta, value: &[u8]) {
        let end = group.frame_offset + value.len();
        self.state.frame[group.frame_offset..end].copy_from_slice(value);
    }

    #[inline(always)]
    fn process_line(&mut self, line: &[u8], line_offset: u64) -> Result<(), VcdParseError> {
        let end = line.len();
        match line[0] {
            b'#' => {
                let tick = parse_timestamp(line, line_offset)?;
                // tolerate non-monotonic timestamps like the .bwave path:
                // stragglers attribute to the previous max tick
                self.apply_timestamp(tick);
            }
            b'0' | b'1' | b'x' | b'z' | b'X' | b'Z' if !self.state.in_dumpoff => {
                let group = self.schema.lookup_group(&line[1..]);
                if group != NO_GROUP {
                    let v = [line[0].to_ascii_lowercase()];
                    // 1-bit fast path: value is already canonical
                    if self.schema.groups[group as usize].width == 1
                        && !self.schema.groups[group as usize].is_real
                    {
                        self.emit_change(group, true, &v);
                    } else {
                        self.emit_change(group, false, &v);
                    }
                }
            }
            b'b' | b'B' if !self.state.in_dumpoff => {
                if let Some(sep) = memchr2(b' ', b'\t', line) {
                    let bits = &line[1..sep];
                    let id_start =
                        if sep + 1 < end && (line[sep + 1] == b' ' || line[sep + 1] == b'\t') {
                            sep + 2
                        } else {
                            sep + 1
                        };
                    let group = self.schema.lookup_group(&line[id_start..]);
                    if group != NO_GROUP {
                        self.emit_change(group, false, bits);
                    }
                }
            }
            b'r' | b'R' if !self.state.in_dumpoff => {
                if let Some(sep) = memchr2(b' ', b'\t', line) {
                    let bits = &line[1..sep];
                    let id_start =
                        if sep + 1 < end && (line[sep + 1] == b' ' || line[sep + 1] == b'\t') {
                            sep + 2
                        } else {
                            sep + 1
                        };
                    let group = self.schema.lookup_group(&line[id_start..]);
                    if group != NO_GROUP {
                        self.emit_change(group, false, bits);
                    }
                }
            }
            b'$' => {
                // Blackout sections are skipped without x-filling: the
                // The builder holds the last value through $dumpoff, and
                // the store keeps that behavior.
                if end >= 8 && &line[..8] == b"$dumpoff" {
                    self.state.in_dumpoff = true;
                } else if end >= 7 && &line[..7] == b"$dumpon" {
                    self.state.in_dumpoff = false;
                }
            }
            _ => {}
        }
        Ok(())
    }

    fn encode_section_start(&self, sequence: u64) -> EncodeSectionStart {
        EncodeSectionStart {
            sequence,
            incoming_frame: self.state.frame.clone(),
            incoming_tick: self.state.current_tick,
            incoming_time_seen: self.state.any_time_written,
            incoming_dumpoff: self.state.in_dumpoff,
            first_file_section: sequence == 0,
        }
    }

    fn reconcile_chunk_events(&mut self, ir: &ChunkEvents) {
        let incoming_dumpoff = self.state.in_dumpoff;
        self.state.overwide_values += ir.enabled_overwide_values;
        if !incoming_dumpoff {
            self.state.overwide_values += ir.prefix_overwide_values;
            self.apply_last_changes(ir, true);
        }
        self.apply_last_changes(ir, false);
        if let Some(max_timestamp) = ir.max_timestamp {
            if max_timestamp > self.state.current_tick || !self.state.any_time_written {
                self.state.current_tick = max_timestamp;
                self.state.last_written_time = max_timestamp;
                self.state.any_time_written = true;
            }
        }
        if let Some(enabled) = ir.final_dump_enabled {
            self.state.in_dumpoff = !enabled;
        }
    }

    fn apply_last_changes(&mut self, ir: &ChunkEvents, prefix: bool) {
        for chain in &ir.chains {
            let record = if prefix {
                chain.prefix_last
            } else {
                chain.enabled_last
            };
            if record == fst_writer::FST_NO_CHANGE {
                continue;
            }
            let change = ir.changes[record as usize];
            let meta = self.schema.groups[change.signal() as usize];
            if change.is_inline() {
                self.store_frame(meta, &[change.inline_value()]);
                continue;
            }
            let start = change.value_offset() as usize;
            let len = if meta.is_real { 8 } else { meta.width as usize };
            if change.is_packed_binary() {
                let packed_len = len.div_ceil(8);
                let destination = &mut self.state.frame[meta.frame_offset..meta.frame_offset + len];
                unpack_binary_bits_into(&ir.values[start..start + packed_len], destination);
            } else {
                self.store_frame(meta, &ir.values[start..start + len]);
            }
        }
    }

    fn process_chunk(&mut self, chunk: &VcdChunk) -> Result<(), VcdParseError> {
        let mut start = 0;
        while let Some(relative) = memchr(b'\n', &chunk.bytes[start..]) {
            let line_end = start + relative;
            let mut end = line_end;
            if end > start && chunk.bytes[end - 1] == b'\r' {
                end -= 1;
            }
            if end > start {
                self.process_line(&chunk.bytes[start..end], chunk.start_offset + start as u64)?;
            }
            start = line_end + 1;
        }
        if start < chunk.bytes.len() {
            let mut end = chunk.bytes.len();
            if chunk.bytes[end - 1] == b'\r' {
                end -= 1;
            }
            if end > start {
                self.process_line(&chunk.bytes[start..end], chunk.start_offset + start as u64)?;
            }
        }
        Ok(())
    }

    /// Block-based streaming parser: 4MB-chunk + memchr line scanning, with
    /// the heartbeat sidecar the Booley FIFO watchdog monitors.
    pub fn parse_bytes(
        &mut self,
        reader: &mut impl std::io::BufRead,
        heartbeat_path: Option<&Path>,
    ) -> Result<(), VcdParseError> {
        const HEARTBEAT_INTERVAL: Duration = Duration::from_secs(5);
        let mut total_bytes_read: u64 = 0;
        let mut last_heartbeat = Instant::now();
        let mut source = VcdChunkSource::new(reader, self.body_offset, SERIAL_VCD_CHUNK_TARGET)
            .expect("fixed VCD chunk target is positive");
        while let Some(chunk) = source.next_chunk()? {
            total_bytes_read += chunk.bytes.len() as u64;
            if let Some(hb_path) = heartbeat_path {
                if last_heartbeat.elapsed() >= HEARTBEAT_INTERVAL {
                    if let Ok(mut f) = std::fs::OpenOptions::new()
                        .create(true)
                        .append(true)
                        .open(hb_path)
                    {
                        let _ = writeln!(
                            f,
                            "bytes={} tick={}",
                            total_bytes_read, self.state.current_tick
                        );
                    }
                    last_heartbeat = Instant::now();
                }
            }
            self.process_chunk(&chunk)?;
            source.recycle(chunk);
        }
        Ok(())
    }

    /// Parse and encode timestamp-aligned chunks on a bounded worker pool,
    /// reconciling state and appending completed sections in input order.
    pub fn parse_bytes_parallel(
        &mut self,
        reader: &mut (impl std::io::BufRead + Send),
        heartbeat_path: Option<&Path>,
        parse_worker_count: usize,
        encode_worker_count: usize,
        pack_worker_count: usize,
        chunk_target: usize,
        section_target: usize,
    ) -> Result<(), VcdParseError> {
        if parse_worker_count == 0
            || encode_worker_count == 0
            || pack_worker_count == 0
            || chunk_target == 0
            || section_target == 0
        {
            return Err(VcdParseError::Worker {
                message: "parse/encode worker counts and chunk/section targets must be positive"
                    .to_string(),
            });
        }
        self.encoder
            .set_compression_workers(pack_worker_count)
            .map_err(|error| VcdParseError::Worker {
                message: error.to_string(),
            })?;
        let encode_schema = Arc::new(self.schema.clone());
        let encoder_factories = (0..encode_worker_count)
            .map(|_| {
                self.encoder.fresh().map_err(|error| VcdParseError::Worker {
                    message: error.to_string(),
                })
            })
            .collect::<Result<Vec<_>, _>>()?;
        let batch_capacity = parse_worker_count.saturating_add(2);
        let mut total_bytes = 0u64;
        let mut next_sequence = 0u64;
        let mut last_heartbeat = Instant::now();
        #[cfg(feature = "profile")]
        let mut profile = ParallelProfile::default();
        #[cfg(feature = "profile")]
        let pipeline_started = Instant::now();
        let cancellation = Arc::new(AtomicBool::new(false));
        let reader_cancellation = Arc::clone(&cancellation);
        let body_offset = self.body_offset;
        let pipeline_result = std::thread::scope(|thread_scope| {
            let (sender, receiver) = mpsc::sync_channel(batch_capacity);
            let (recycle_sender, recycle_receiver) = mpsc::channel();
            thread_scope.spawn(move || {
                let mut source = VcdChunkSource::new(reader, body_offset, chunk_target)
                    .expect("validated VCD chunk target is positive")
                    .with_cancellation(reader_cancellation);
                loop {
                    while let Ok(chunk) = recycle_receiver.try_recv() {
                        source.recycle(chunk);
                    }
                    match source.next_chunk() {
                        Ok(Some(chunk)) => {
                            if sender.send(Ok(chunk)).is_err() {
                                break;
                            }
                        }
                        Ok(None) => break,
                        Err(error) => {
                            let _ = sender.send(Err(error));
                            break;
                        }
                    }
                }
            });
            let (encoded_sender, encoded_receiver) = mpsc::sync_channel(encode_worker_count + 2);
            let (event_recycle_sender, event_recycle_receiver) = mpsc::channel();
            let mut encode_senders = Vec::with_capacity(encode_worker_count);
            for factory in encoder_factories {
                let (encode_sender, encode_receiver) = mpsc::sync_channel(1);
                encode_senders.push(encode_sender);
                let encoded_sender = encoded_sender.clone();
                let event_recycle_sender = event_recycle_sender.clone();
                thread_scope.spawn(move || {
                    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                        encode_worker_loop(
                            &factory,
                            encode_receiver,
                            &encoded_sender,
                            &event_recycle_sender,
                        )
                    }))
                    .unwrap_or_else(|payload| {
                        Err(VcdParseError::Worker {
                            message: panic_message(payload),
                        })
                    });
                    if let Err(error) = result {
                        let _ = encoded_sender.send(Err(error));
                    }
                });
            }
            drop(encoded_sender);
            let (parsed_sender, parsed_receiver) = mpsc::sync_channel(batch_capacity);
            let mut parse_senders = Vec::with_capacity(parse_worker_count);
            for _ in 0..parse_worker_count {
                let (parse_sender, parse_receiver) =
                    mpsc::sync_channel::<(VcdChunk, Option<ChunkBuffers>)>(1);
                parse_senders.push(parse_sender);
                let parsed_sender = parsed_sender.clone();
                let recycle_sender = recycle_sender.clone();
                let schema = Arc::clone(&encode_schema);
                thread_scope.spawn(move || {
                    let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                        while let Ok((chunk, recycled)) = parse_receiver.recv() {
                            let sequence = chunk.sequence;
                            let result = ChunkParser::new(&schema, &chunk, recycled)
                                .parse(&chunk)
                                .map_err(|source| VcdParseError::Chunk {
                                    sequence,
                                    source: Box::new(source),
                                });
                            let _ = recycle_sender.send(chunk);
                            if parsed_sender.send(result).is_err() {
                                break;
                            }
                        }
                    }));
                    if let Err(payload) = result {
                        let _ = parsed_sender.send(Err(VcdParseError::Worker {
                            message: panic_message(payload),
                        }));
                    }
                });
            }
            let dispatcher_sender = parsed_sender.clone();
            thread_scope.spawn(move || {
                let mut worker = 0usize;
                while let Ok(message) = receiver.recv() {
                    match message {
                        Ok(chunk) => {
                            let recycled = event_recycle_receiver.try_recv().ok();
                            if parse_senders[worker].send((chunk, recycled)).is_err() {
                                break;
                            }
                            worker = (worker + 1) % parse_senders.len();
                        }
                        Err(error) => {
                            let _ = dispatcher_sender.send(Err(error));
                            break;
                        }
                    }
                }
            });
            drop(parsed_sender);
            let mut submitted = VecDeque::new();
            let mut completed = BTreeMap::new();
            let mut active_sequence: Option<u64> = None;
            let mut active_fst_stream_bytes = 0usize;
            let mut active_worker = 0usize;
            let result = (|| {
                let mut parsed_pending = BTreeMap::new();
                #[cfg(feature = "profile")]
                let mut pending_representation_bytes = 0usize;
                while let Ok(parsed) = parsed_receiver.recv() {
                    let ir = parsed?;
                    #[cfg(feature = "profile")]
                    {
                        let representation_bytes = chunk_representation_bytes(&ir);
                        pending_representation_bytes += representation_bytes;
                        profile.representation_bytes += representation_bytes;
                        profile.peak_batch_representation_bytes = profile
                            .peak_batch_representation_bytes
                            .max(pending_representation_bytes);
                        profile.parse_seconds += ir.parse_wall_seconds;
                        profile.input_bytes += ir.input_bytes;
                        profile.event_count += ir.changes.len();
                        profile.value_arena_bytes += ir.values.len();
                        profile.touched_signal_chains += ir.chains.len();
                        profile.recycled_capacity_bytes += ir.recycled_capacity_bytes;
                        profile.newly_allocated_capacity_bytes += ir.newly_allocated_capacity_bytes;
                        profile.parse_cpu_seconds += ir.parse_cpu_seconds;
                    }
                    let sequence = ir.sequence;
                    if parsed_pending.insert(sequence, ir).is_some() {
                        return Err(worker_error(sequence, "duplicate parsed chunk sequence"));
                    }
                    while let Some(ir) = parsed_pending.remove(&next_sequence) {
                        #[cfg(feature = "profile")]
                        {
                            pending_representation_bytes = pending_representation_bytes
                                .saturating_sub(chunk_representation_bytes(&ir));
                        }
                        #[cfg(feature = "profile")]
                        let stage_started = Instant::now();
                        #[cfg(feature = "profile")]
                        let cpu_started = thread_cpu_seconds();
                        next_sequence += ir.chunk_count;
                        total_bytes += ir.input_bytes as u64;
                        if active_sequence.is_none() {
                            let start = self.encode_section_start(ir.sequence);
                            let queue_metrics = Self::send_encode_message(
                                &encode_senders[active_worker],
                                EncodeMessage::Start(start),
                            )?;
                            #[cfg(feature = "profile")]
                            profile.record_queue(queue_metrics);
                            #[cfg(not(feature = "profile"))]
                            let _ = queue_metrics;
                            active_sequence = Some(ir.sequence);
                        }
                        active_fst_stream_bytes += ir.fst_stream_bytes;
                        self.reconcile_chunk_events(&ir);
                        let queue_metrics = Self::send_encode_message(
                            &encode_senders[active_worker],
                            EncodeMessage::Chunk(ir),
                        )?;
                        #[cfg(feature = "profile")]
                        profile.record_queue(queue_metrics);
                        #[cfg(not(feature = "profile"))]
                        let _ = queue_metrics;
                        if active_fst_stream_bytes >= section_target {
                            let sequence = active_sequence.take().expect("active section exists");
                            let queue_metrics = Self::send_encode_message(
                                &encode_senders[active_worker],
                                EncodeMessage::Finish,
                            )?;
                            #[cfg(feature = "profile")]
                            profile.record_queue(queue_metrics);
                            #[cfg(not(feature = "profile"))]
                            let _ = queue_metrics;
                            submitted.push_back(sequence);
                            active_fst_stream_bytes = 0;
                            active_worker = (active_worker + 1) % encode_senders.len();
                        }
                        #[cfg(feature = "profile")]
                        {
                            profile.reconcile_seconds += stage_started.elapsed().as_secs_f64();
                            profile.coordinator_cpu_seconds += thread_cpu_seconds() - cpu_started;
                        }
                        while let Ok(encoded) = encoded_receiver.try_recv() {
                            let timings =
                                self.accept_encoded(encoded, &mut submitted, &mut completed)?;
                            #[cfg(feature = "profile")]
                            {
                                profile.encode_seconds += timings.encode_seconds;
                                profile.write_seconds += timings.write_seconds;
                                profile.pack_cpu_seconds += timings.pack_cpu_seconds;
                                profile.compression_seconds += timings.compression_seconds;
                                profile.compression_cpu_seconds += timings.compression_cpu_seconds;
                                profile.assembler_cpu_seconds += timings.assembler_cpu_seconds;
                                profile.packer_input_bytes += timings.packer_input_bytes;
                                profile.recycled_capacity_bytes += timings.recycled_capacity_bytes;
                                profile.newly_allocated_capacity_bytes +=
                                    timings.newly_allocated_capacity_bytes;
                                profile.arena_to_packer_copied_bytes +=
                                    timings.arena_to_packer_copied_bytes;
                                profile.record_packer_workers(&timings.packer_worker_cpu_seconds);
                            }
                            #[cfg(not(feature = "profile"))]
                            let _ = timings;
                        }
                        write_parallel_heartbeat(
                            heartbeat_path,
                            total_bytes,
                            self.state.current_tick,
                            &mut last_heartbeat,
                        );
                    }
                }
                if !parsed_pending.is_empty() {
                    return Err(VcdParseError::Worker {
                        message: "parser workers stopped before all chunks were ordered"
                            .to_string(),
                    });
                }
                if let Some(sequence) = active_sequence.take() {
                    let queue_metrics = Self::send_encode_message(
                        &encode_senders[active_worker],
                        EncodeMessage::Finish,
                    )?;
                    #[cfg(feature = "profile")]
                    profile.record_queue(queue_metrics);
                    #[cfg(not(feature = "profile"))]
                    let _ = queue_metrics;
                    submitted.push_back(sequence);
                }
                drop(encode_senders);
                while let Ok(encoded) = encoded_receiver.recv() {
                    let timings = self.accept_encoded(encoded, &mut submitted, &mut completed)?;
                    #[cfg(feature = "profile")]
                    {
                        profile.encode_seconds += timings.encode_seconds;
                        profile.write_seconds += timings.write_seconds;
                        profile.pack_cpu_seconds += timings.pack_cpu_seconds;
                        profile.compression_seconds += timings.compression_seconds;
                        profile.compression_cpu_seconds += timings.compression_cpu_seconds;
                        profile.assembler_cpu_seconds += timings.assembler_cpu_seconds;
                        profile.packer_input_bytes += timings.packer_input_bytes;
                        profile.recycled_capacity_bytes += timings.recycled_capacity_bytes;
                        profile.newly_allocated_capacity_bytes +=
                            timings.newly_allocated_capacity_bytes;
                        profile.arena_to_packer_copied_bytes +=
                            timings.arena_to_packer_copied_bytes;
                        profile.record_packer_workers(&timings.packer_worker_cpu_seconds);
                    }
                    #[cfg(not(feature = "profile"))]
                    let _ = timings;
                }
                if !submitted.is_empty() || !completed.is_empty() {
                    return Err(VcdParseError::Worker {
                        message: "encoder workers stopped before all sections were committed"
                            .to_string(),
                    });
                }
                Ok(())
            })();
            cancellation.store(true, Ordering::Relaxed);
            drop(parsed_receiver);
            result
        });
        pipeline_result?;
        #[cfg(feature = "profile")]
        {
            let pipeline_seconds = pipeline_started.elapsed().as_secs_f64();
            let measured_cpu_seconds = profile.parse_cpu_seconds
                + profile.coordinator_cpu_seconds
                + profile.pack_cpu_seconds
                + profile.compression_cpu_seconds
                + profile.assembler_cpu_seconds;
            let packer_worker_cpu_seconds = profile
                .packer_worker_cpu_seconds
                .iter()
                .map(|seconds| format!("{seconds:.6}"))
                .collect::<Vec<_>>()
                .join(",");
            eprintln!(
                "# bwave_parallel_profile pipeline_s={:.6} parse_s={:.6} parse_cpu_s={:.6} \
                 reconcile_s={:.6} coordinator_cpu_s={:.6} encode_s={:.6} pack_cpu_s={:.6} \
                 compression_s={:.6} compression_cpu_s={:.6} write_s={:.6} \
                 assembler_cpu_s={:.6} measured_cpu_s={:.6} measured_active_workers={:.3} \
                 encoder_queue_block_s={:.6} encoder_queue_high_water={} input_bytes={} \
                 representation_bytes={} representation_per_input={:.4} \
                 peak_batch_representation_bytes={} event_count={} value_arena_bytes={} \
                 input_to_arena_copied_bytes={} arena_to_packer_copied_bytes={} \
                 packer_to_compressor_bytes={} touched_signal_chains={} \
                 recycled_capacity_bytes={} newly_allocated_capacity_bytes={} \
                 packer_worker_cpu_s={}",
                pipeline_seconds,
                profile.parse_seconds,
                profile.parse_cpu_seconds,
                profile.reconcile_seconds,
                profile.coordinator_cpu_seconds,
                profile.encode_seconds,
                profile.pack_cpu_seconds,
                profile.compression_seconds,
                profile.compression_cpu_seconds,
                profile.write_seconds,
                profile.assembler_cpu_seconds,
                measured_cpu_seconds,
                measured_cpu_seconds / pipeline_seconds.max(f64::EPSILON),
                profile.encoder_queue_block_seconds,
                profile.encoder_queue_high_water,
                profile.input_bytes,
                profile.representation_bytes,
                profile.representation_bytes as f64 / profile.input_bytes.max(1) as f64,
                profile.peak_batch_representation_bytes,
                profile.event_count,
                profile.value_arena_bytes,
                profile.value_arena_bytes,
                profile.arena_to_packer_copied_bytes,
                profile.packer_input_bytes,
                profile.touched_signal_chains,
                profile.recycled_capacity_bytes,
                profile.newly_allocated_capacity_bytes,
                packer_worker_cpu_seconds,
            );
        }
        Ok(())
    }

    /// Finish the FST and patch the header after every body write succeeded.
    pub fn finalize_and_write(mut self) -> Result<(), String> {
        if self.state.overwide_values > 0 {
            eprintln!(
                "WARNING: {} value(s) wider than their declared width were truncated",
                self.state.overwide_values
            );
        }
        if let Some(e) = &self.state.write_error {
            return Err(format!(
                "failed to write {}: {}",
                self.output_path.display(),
                e
            ));
        }
        if !self.sections_encoded_externally {
            let section = self
                .encoder
                .encode_section()
                .map_err(|e| format!("failed to encode {}: {e}", self.output_path.display()))?;
            self.writer
                .append_section(section)
                .map_err(|e| format!("failed to write {}: {e}", self.output_path.display()))?;
        }
        self.writer
            .finish()
            .map_err(|e| format!("failed to write {}: {e}", self.output_path.display()))?;
        // Not a "cache": this is the primary build artifact. The old wording
        // cost an investigator a wrong-turn hunting a cache layer that never existed.
        eprintln!("# wrote {}", self.output_path.display());
        Ok(())
    }
}

#[cfg(test)]
mod build_tests {
    use super::*;
    use crate::parser::{parse_header, TimestampError, VcdParseError};
    use std::io::{self, BufRead, Cursor, Read};
    use std::sync::atomic::{AtomicUsize, Ordering};

    static TEST_COUNTER: AtomicUsize = AtomicUsize::new(0);

    struct ErrorReader;

    impl Read for ErrorReader {
        fn read(&mut self, _buf: &mut [u8]) -> io::Result<usize> {
            Err(io::Error::other("injected read error"))
        }
    }

    impl BufRead for ErrorReader {
        fn fill_buf(&mut self) -> io::Result<&[u8]> {
            Err(io::Error::other("injected read error"))
        }

        fn consume(&mut self, _amount: usize) {}
    }

    fn assert_timestamp_error(line: &[u8], expected: TimestampError) {
        let error = parse_timestamp(line, 123).unwrap_err();
        assert!(matches!(
            error,
            VcdParseError::InvalidTimestamp {
                offset: 123,
                reason,
                ..
            } if reason == expected
        ));
    }

    #[test]
    fn timestamps_use_checked_strict_arithmetic() {
        assert_eq!(
            parse_timestamp(b"#18446744073709551615", 0).unwrap(),
            u64::MAX
        );
        assert_eq!(parse_timestamp(b"#\t42  ", 0).unwrap(), 42);
        assert_timestamp_error(b"#", TimestampError::MissingDigits);
        assert_timestamp_error(b"#18446744073709551616", TimestampError::Overflow);
        assert_timestamp_error(b"#12junk", TimestampError::TrailingCharacters);
    }

    #[test]
    fn body_read_error_is_not_eof() {
        let header_text = b"$scope module tb $end\n\
$var wire 1 ! sig $end\n\
$upscope $end\n\
$enddefinitions $end\n";
        let mut header_reader = Cursor::new(header_text);
        let header = parse_header(&mut header_reader);
        let test_number = TEST_COUNTER.fetch_add(1, Ordering::Relaxed);
        let output = std::env::temp_dir().join(format!(
            "bwave_read_error_{}_{}.fst",
            std::process::id(),
            test_number
        ));
        let mut handler = FstBuildHandler::new(&header, None, &output).unwrap();
        let error = handler.parse_bytes(&mut ErrorReader, None).unwrap_err();
        assert!(matches!(
            error,
            VcdParseError::Read {
                section: "body",
                offset,
                ..
            } if offset == header.body_offset
        ));
        drop(handler);
        let _ = std::fs::remove_file(output);
    }

    #[test]
    fn encoded_section_is_independent_of_ordered_file_writer() {
        let test_number = TEST_COUNTER.fetch_add(1, Ordering::Relaxed);
        let output = std::env::temp_dir().join(format!(
            "bwave_independent_section_{}_{}.fst",
            std::process::id(),
            test_number
        ));
        let info = fst_writer::FstInfo {
            start_time: 0,
            timescale_exponent: -9,
            version: "bwave test".to_string(),
            date: String::new(),
            file_type: fst_writer::FstFileType::Verilog,
        };
        let mut header_writer = fst_writer::open_fst(&output, &info).unwrap();
        header_writer
            .scope("tb", "", fst_writer::FstScopeType::Module)
            .unwrap();
        let signal = header_writer
            .var(
                "sig",
                fst_writer::FstSignalType::bit_vec(1),
                fst_writer::FstVarType::Wire,
                fst_writer::FstVarDirection::Implicit,
                None,
            )
            .unwrap();
        header_writer.up_scope().unwrap();

        let (mut encoder, mut writer) = header_writer.finish_split().unwrap();
        encoder.time_change(0).unwrap();
        encoder.signal_change(signal, b"0").unwrap();
        encoder.time_change(10).unwrap();
        encoder.signal_change(signal, b"1").unwrap();
        let section = encoder.encode_section().unwrap();
        assert!(!section.is_empty());
        assert_eq!(section.end_time(), 10);
        writer.append_section(section).unwrap();
        let mut second_encoder = encoder.from_frame(b"1", 10).unwrap();
        second_encoder.time_change(20).unwrap();
        second_encoder.signal_change(signal, b"0").unwrap();
        writer
            .append_section(second_encoder.encode_section().unwrap())
            .unwrap();
        writer.finish().unwrap();

        let cache = crate::cache::ColumnCache::load_from_file(&output).unwrap();
        assert_eq!(
            cache.read_transitions(0),
            vec![(0, "0".into()), (10, "1".into()), (20, "0".into())]
        );
        let _ = std::fs::remove_file(output);
    }
}
