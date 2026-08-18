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
use rustc_hash::FxHashMap;

use crate::cache::{CachedSignal, ClockEntry, ColumnCache};
use crate::format::format_value;
use crate::parser::VcdHeader;
use crate::signal::signals_in_scope;

/// f64 read from an FST frame slot that was never written: fst-writer fills
/// frames with ASCII 'x' bytes, so a real signal with no change before the
/// first time step reads back as this exact bit pattern. We drop it.
const REAL_FRAME_GARBAGE_BITS: u64 = u64::from_le_bytes([b'x'; 8]);

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

/// Canonicalize a VCD bit-vector value to exactly `width` lowercase chars in
/// `out` (fst-writer panics on over-wide values and self-extends short ones,
/// but VCD extension rules for x/z need to be ours). Returns true when the
/// value arrived wider than the declared width (simulator dialect quirk;
/// least-significant bits are kept, matching GTKWave).
fn canon_bits_into(bits: &[u8], width: usize, out: &mut Vec<u8>) -> bool {
    out.clear();
    let len = bits.len();
    if len < width {
        let fill = match bits.first().copied().unwrap_or(b'0').to_ascii_lowercase() {
            b'x' => b'x',
            b'z' => b'z',
            _ => b'0',
        };
        out.resize(width - len, fill);
    }
    let src = if len > width {
        &bits[len - width..]
    } else {
        bits
    };
    out.extend(src.iter().map(|b| b.to_ascii_lowercase()));
    len > width
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

/// Streaming VCD -> FST build handler (the replacement for the retired
/// `.bwave` builder). Construct with the parsed VCD header, feed the body
/// through `parse_bytes`, then `finalize_and_write`.
pub struct FstBuildHandler {
    body: fst_writer::FstBodyWriter<std::io::BufWriter<File>>,
    output_path: std::path::PathBuf,
    // Direct VCD-ID -> group lookup (single-char fast path + hash map)
    single_char_idx: [u32; 256],
    multi_char_idx: FxHashMap<String, u32>,
    groups: Vec<GroupMeta>,
    current_tick: u64,
    last_written_time: u64,
    any_time_written: bool,
    val_buf: Vec<u8>,
    overwide_values: u64,
    write_error: Option<String>,
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
        let mut multi_char_idx = FxHashMap::<String, u32>::default();
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
            let existing = if id_bytes.len() == 1 {
                single_char_idx[id_bytes[0] as usize]
            } else {
                multi_char_idx.get(&sig.id).copied().unwrap_or(NO_GROUP)
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
                groups.push(GroupMeta {
                    fst_id,
                    width: sig.width,
                    is_real,
                });
                let g = (groups.len() - 1) as u32;
                if id_bytes.len() == 1 {
                    single_char_idx[id_bytes[0] as usize] = g;
                } else {
                    multi_char_idx.insert(sig.id.clone(), g);
                }
            }
        }
        transition_scopes(&mut hw, &mut current_scope, &[])?;

        let body = hw.finish().map_err(|e| format!("fst header finish: {e}"))?;
        Ok(FstBuildHandler {
            body,
            output_path: output_path.to_path_buf(),
            single_char_idx,
            multi_char_idx,
            groups,
            current_tick: 0,
            last_written_time: 0,
            any_time_written: false,
            val_buf: Vec::with_capacity(256),
            overwide_values: 0,
            write_error: None,
        })
    }

    #[inline(always)]
    fn lookup_group(&self, id: &[u8]) -> u32 {
        if id.len() == 1 {
            self.single_char_idx[id[0] as usize]
        } else {
            let s = std::str::from_utf8(id).unwrap_or("");
            self.multi_char_idx.get(s).copied().unwrap_or(NO_GROUP)
        }
    }

    fn record_write_error(&mut self, e: impl std::fmt::Display) {
        if self.write_error.is_none() {
            self.write_error = Some(e.to_string());
        }
    }

    #[inline]
    fn emit_change(&mut self, group: u32, bytes_are_ready: bool, raw: &[u8]) {
        let g = &self.groups[group as usize];
        let fst_id = g.fst_id;
        if g.is_real {
            let v: f64 = std::str::from_utf8(raw)
                .ok()
                .and_then(|s| s.parse().ok())
                .unwrap_or(f64::NAN);
            if let Err(e) = self.body.signal_change(fst_id, &v.to_le_bytes()) {
                self.record_write_error(e);
            }
            return;
        }
        if bytes_are_ready {
            if let Err(e) = self.body.signal_change(fst_id, raw) {
                self.record_write_error(e);
            }
        } else {
            let width = g.width as usize;
            let mut buf = std::mem::take(&mut self.val_buf);
            if canon_bits_into(raw, width, &mut buf) {
                self.overwide_values += 1;
            }
            if let Err(e) = self.body.signal_change(fst_id, &buf) {
                self.record_write_error(e);
            }
            self.val_buf = buf;
        }
    }

    #[inline(always)]
    fn process_line(&mut self, line: &[u8], in_dumpoff: &mut bool) {
        let end = line.len();
        match line[0] {
            b'#' => {
                let mut i = 1;
                while i < end && line[i] == b' ' {
                    i += 1;
                }
                let mut tick: u64 = 0;
                let mut valid = false;
                while i < end {
                    let d = line[i];
                    if d.is_ascii_digit() {
                        tick = tick.wrapping_mul(10).wrapping_add((d - b'0') as u64);
                        valid = true;
                    } else {
                        break;
                    }
                    i += 1;
                }
                if valid {
                    // tolerate non-monotonic timestamps like the .bwave path:
                    // stragglers attribute to the previous max tick
                    if tick > self.current_tick || !self.any_time_written {
                        self.current_tick = tick;
                    }
                    if tick > self.last_written_time || !self.any_time_written {
                        // flushing is only legal right before a time change
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
                }
            }
            b'0' | b'1' | b'x' | b'z' | b'X' | b'Z' if !*in_dumpoff => {
                let group = self.lookup_group(&line[1..]);
                if group != NO_GROUP {
                    let v = [line[0].to_ascii_lowercase()];
                    // 1-bit fast path: value is already canonical
                    if self.groups[group as usize].width == 1
                        && !self.groups[group as usize].is_real
                    {
                        self.emit_change(group, true, &v);
                    } else {
                        self.emit_change(group, false, &v);
                    }
                }
            }
            b'b' | b'B' if !*in_dumpoff => {
                if let Some(sep) = memchr2(b' ', b'\t', line) {
                    let bits = &line[1..sep];
                    let id_start =
                        if sep + 1 < end && (line[sep + 1] == b' ' || line[sep + 1] == b'\t') {
                            sep + 2
                        } else {
                            sep + 1
                        };
                    let group = self.lookup_group(&line[id_start..]);
                    if group != NO_GROUP {
                        self.emit_change(group, false, bits);
                    }
                }
            }
            b'r' | b'R' if !*in_dumpoff => {
                if let Some(sep) = memchr2(b' ', b'\t', line) {
                    let bits = &line[1..sep];
                    let id_start =
                        if sep + 1 < end && (line[sep + 1] == b' ' || line[sep + 1] == b'\t') {
                            sep + 2
                        } else {
                            sep + 1
                        };
                    let group = self.lookup_group(&line[id_start..]);
                    if group != NO_GROUP {
                        self.emit_change(group, false, bits);
                    }
                }
            }
            b'$' => {
                // Blackout sections are skipped without x-filling: the
                // .bwave builder holds the last value through $dumpoff
                // (unlike the raw-VCD streaming path, which x-fills), and
                // the store keeps that behavior.
                if end >= 8 && &line[..8] == b"$dumpoff" {
                    *in_dumpoff = true;
                } else if end >= 7 && &line[..7] == b"$dumpon" {
                    *in_dumpoff = false;
                }
            }
            _ => {}
        }
    }

    /// Block-based streaming parser: 4MB-chunk + memchr line scanning, with
    /// the heartbeat sidecar the Booley FIFO watchdog monitors.
    pub fn parse_bytes(
        &mut self,
        reader: &mut impl std::io::BufRead,
        heartbeat_path: Option<&Path>,
    ) {
        const BUF_SIZE: usize = 4 * 1024 * 1024;
        const HEARTBEAT_INTERVAL: Duration = Duration::from_secs(5);
        let mut buf = vec![0u8; BUF_SIZE];
        let mut leftover = 0usize;
        let mut in_dumpoff = false;
        let mut total_bytes_read: u64 = 0;
        let mut last_heartbeat = Instant::now();

        loop {
            let n = match reader.read(&mut buf[leftover..]) {
                Ok(n) => n,
                Err(e) => {
                    eprintln!("WARNING: I/O error reading VCD stream: {e}");
                    0
                }
            };

            if let Some(hb_path) = heartbeat_path {
                total_bytes_read += n as u64;
                if last_heartbeat.elapsed() >= HEARTBEAT_INTERVAL {
                    if let Ok(mut f) = std::fs::OpenOptions::new()
                        .create(true)
                        .append(true)
                        .open(hb_path)
                    {
                        let _ =
                            writeln!(f, "bytes={} tick={}", total_bytes_read, self.current_tick);
                    }
                    last_heartbeat = Instant::now();
                }
            }

            if n == 0 {
                if leftover > 0 {
                    let mut end = leftover;
                    if end > 0 && buf[end - 1] == b'\n' {
                        end -= 1;
                    }
                    if end > 0 && buf[end - 1] == b'\r' {
                        end -= 1;
                    }
                    if end > 0 {
                        self.process_line(&buf[..end], &mut in_dumpoff);
                    }
                }
                break;
            }
            let total = leftover + n;
            let mut pos = 0;

            loop {
                let nl = match memchr(b'\n', &buf[pos..total]) {
                    Some(offset) => offset,
                    None => break,
                };
                let line_end = pos + nl;
                let mut end = line_end;
                if end > pos && buf[end - 1] == b'\r' {
                    end -= 1;
                }
                if end > pos {
                    self.process_line(&buf[pos..end], &mut in_dumpoff);
                }
                pos = line_end + 1;
            }

            if pos < total {
                buf.copy_within(pos..total, 0);
                leftover = total - pos;
            } else {
                leftover = 0;
            }
        }
    }

    /// Finish the FST (writes the final value-change section and fixes up
    /// the header). Exits the process on write failure, matching the .bwave
    /// builder's behavior.
    pub fn finalize_and_write(self) {
        if self.overwide_values > 0 {
            eprintln!(
                "WARNING: {} value(s) wider than their declared width were truncated",
                self.overwide_values
            );
        }
        if let Some(e) = &self.write_error {
            eprintln!(
                "ERROR: failed to write {}: {}",
                self.output_path.display(),
                e
            );
            std::process::exit(1);
        }
        match self.body.finish() {
            // Not a "cache": this is the primary build artifact. The old
            // wording cost an investigator a wrong-turn hunting a cache layer
            // that never existed.
            Ok(()) => eprintln!("# wrote {}", self.output_path.display()),
            Err(e) => {
                eprintln!(
                    "ERROR: failed to write {}: {}",
                    self.output_path.display(),
                    e
                );
                std::process::exit(1);
            }
        }
    }
}
