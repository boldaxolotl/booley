//! Query layer over the FST waveform store.
//!
//! `ColumnCache` presents header metadata (sim range, timescale, clock
//! table) plus transition-read primitives, and the ten `*_from_cache`
//! query functions implement the CLI's query surface on top of them. The
//! on-disk format is plain FST (see `crate::fst`); this module holds no
//! format knowledge of its own — it replaced the retired `.bwave` columnar
//! format, whose writer/reader lived here through v0.2.

use std::collections::HashMap;
use std::io::{self, BufWriter, Write};
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};

/// Legacy: set by `build_virtuals` when a `--virtual` def fails to parse or
/// resolve. Kept for source compatibility with `main.rs`, but `build_virtuals`
/// now `exit(2)`s on the first bad def so this flag is never observed by the
/// outer-process-exit check at the bottom of main(). The exit-on-error path
/// is the correct behaviour: a stale virtual definition used to silently
/// fall through to whatever real signal happened to glob-match.
static VIRTUAL_DEF_ERROR: AtomicBool = AtomicBool::new(false);

/// True if any `--virtual` def was rejected during this process.
pub fn virtual_def_error_seen() -> bool {
    VIRTUAL_DEF_ERROR.load(Ordering::SeqCst)
}

use serde::Serialize;

use crate::format::{format_value_with_radix, is_edge_keyword, values_match, Radix};
use crate::signal::{compile_patterns, match_signal};
use crate::ExtractConfig;

use rayon::prelude::*;

/// Clock entry for the multi-clock table (re-derived at FST load).
#[derive(Debug, Clone)]
pub struct ClockEntry {
    pub period: u64,
    pub first_rise: u64,
    pub id: String,
}

// -- ColumnCache (read path) ----------------------------------------------

/// Reduce a stored text value to its minimal VCD form. Simulators disagree
/// on how much left-extension padding they dump; normalizing at decode keeps
/// query output independent of dump dialect and identical between the
/// .bwave and FST backends. Two rules, both semantics-preserving under the
/// IEEE 1364 left-extension:
/// - a leading run of the same x/z char collapses to one
///   ("xxxx01" == "x01")
/// - leading 0-fill padding on a bit-text value drops ("0001z" == "01z" ==
///   "1z"), keeping one '0' when the first significant char is x/z —
///   "0z1" and "z1" extend differently, so that zero is load-bearing.
pub(crate) fn minimal_xz(mut s: String) -> String {
    let b = s.as_bytes();
    if b.len() <= 1 {
        return s;
    }
    let first = b[0].to_ascii_lowercase();
    if matches!(first, b'x' | b'z') {
        let c = b[0];
        let mut run = 0;
        while run + 1 < b.len() && b[run + 1] == c {
            run += 1;
        }
        if run > 0 {
            s.drain(..run);
        }
        return s;
    }
    if first == b'0' {
        // Only pure bit-text values (the text path also stores reals and
        // other tokens, where a leading zero is not padding).
        let is_bit_text = b
            .iter()
            .all(|&c| matches!(c.to_ascii_lowercase(), b'0' | b'1' | b'x' | b'z'))
            && b.iter()
                .any(|&c| matches!(c.to_ascii_lowercase(), b'x' | b'z'));
        if is_bit_text {
            let mut start = 0;
            while start + 1 < b.len() && b[start] == b'0' {
                start += 1;
            }
            if matches!(b[start].to_ascii_lowercase(), b'x' | b'z') && start > 0 {
                start -= 1; // keep one zero: the fill char is significant
            }
            if start > 0 {
                s.drain(..start);
            }
        }
    }
    s
}

/// Directory entry for one signal in the store.
#[derive(Debug, Clone)]
pub struct CachedSignal {
    pub name: String,
    pub width: u32,
    pub var_type: String,
    /// Alias-group key (the FST handle index): aliases of the same
    /// underlying signal share it, and queries dedup alias groups by it.
    pub group_id: u64,
}

/// Query-facing view of an FST waveform store: header metadata plus read
/// primitives, all backed by `crate::fst::FstBacking`.
pub struct ColumnCache {
    pub sim_start_tick: u64,
    pub sim_end_tick: u64,
    pub ticks_to_ns: f64,
    pub clock_period_ticks: u64,
    pub first_rise_tick: u64,
    pub timescale_str: String,
    pub clock_id: String,
    pub clock_before_reset_at_deassert: bool,
    pub clock_table: Vec<ClockEntry>,
    pub signals: Vec<CachedSignal>,
    fst: crate::fst::FstBacking,
}

impl ColumnCache {
    /// Load a waveform store. Delegates to the FST loader; returns `None`
    /// if the file is missing or not a readable FST.
    pub fn load_from_file(store_path: &Path) -> Option<ColumnCache> {
        crate::fst::load_fst(store_path)
    }

    /// Distinct signals in the store, deduplicated by `group_id` the same
    /// way `match_signals` dedups results. `signals.len()` counts every
    /// alias entry, so quoting it in a "(N signals in store)" diagnostic
    /// can name a number larger than anything `list` will ever show.
    pub fn unique_signal_count(&self) -> usize {
        let mut groups = std::collections::HashSet::new();
        self.signals
            .iter()
            .filter(|s| groups.insert(s.group_id))
            .count()
    }

    /// Construct a cache over an open FST backing (see `crate::fst::load_fst`).
    /// FST stores full async transitions; cycle-domain queries sample at
    /// query time.
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn new_fst_backed(
        signals: Vec<CachedSignal>,
        sim_start_tick: u64,
        sim_end_tick: u64,
        ticks_to_ns: f64,
        clock_period_ticks: u64,
        first_rise_tick: u64,
        timescale_str: String,
        clock_id: String,
        clock_before_reset_at_deassert: bool,
        clock_table: Vec<ClockEntry>,
        backing: crate::fst::FstBacking,
    ) -> ColumnCache {
        ColumnCache {
            sim_start_tick,
            sim_end_tick,
            ticks_to_ns,
            clock_period_ticks,
            first_rise_tick,
            timescale_str,
            clock_id,
            clock_before_reset_at_deassert,
            clock_table,
            signals,
            fst: backing,
        }
    }

    /// Override the display clock with a different signal's timing.
    pub fn override_clock(&mut self, period: u64, first_rise: u64, clock_name: &str) {
        debug_assert!(period > 0, "override_clock: period must be non-zero");
        self.clock_period_ticks = period;
        self.first_rise_tick = first_rise;
        self.clock_id = clock_name.to_string();
        // VCD event ordering between clock and reset is unknown for the new clock;
        // false (>=) is the conservative default — includes the boundary edge.
        self.clock_before_reset_at_deassert = false;
    }

    /// Detect clock period from a cached signal's transitions.
    /// Finds the signal matching `pattern`, reads its rising edges, and returns (period, first_rise, name).
    pub fn detect_clock_from_pattern(&self, pattern: &str) -> Result<(u64, u64, String), String> {
        let matchers = compile_patterns(&[pattern.to_string()])
            .map_err(|e| format!("invalid clock pattern: {}", e))?;

        let mut candidates: Vec<(usize, &str)> = self
            .signals
            .iter()
            .enumerate()
            .filter(|(_, s)| s.width == 1 && match_signal(&s.name, &matchers))
            .map(|(i, s)| (i, s.name.as_str()))
            .collect();

        if candidates.is_empty() {
            return Err(format!(
                "no 1-bit signal matches clock pattern '{}'",
                pattern
            ));
        }

        // Sort by scope depth then alphabetically (same as extract.rs detect_clock)
        candidates.sort_by(|a, b| {
            let da = a.1.matches('.').count();
            let db = b.1.matches('.').count();
            da.cmp(&db).then(a.1.cmp(b.1))
        });

        let (sig_idx, clock_name) = candidates[0];
        let transitions = self.read_transitions(sig_idx);

        // Find rising edges (0→1)
        let mut rising_ticks: Vec<u64> = Vec::new();
        let mut prev_val = "x";
        for (tick, val) in &transitions {
            if (val == "1" || val == "01") && (prev_val == "0" || prev_val == "00") {
                rising_ticks.push(*tick);
            }
            prev_val = val;
        }

        if rising_ticks.len() < 2 {
            return Err(format!(
                "clock '{}' has fewer than 2 rising edges — cannot determine period",
                clock_name
            ));
        }

        let first_rise = rising_ticks[0];
        let period = rising_ticks[1] - rising_ticks[0];

        if period == 0 {
            return Err(format!(
                "clock '{}' has zero-width period (edges at same tick)",
                clock_name
            ));
        }

        // Validate subsequent edges for consistency (detect gated/jittered clocks)
        let mut inconsistent = 0u32;
        for i in 2..rising_ticks.len() {
            let gap = rising_ticks[i] - rising_ticks[i - 1];
            if gap != period {
                inconsistent += 1;
            }
        }
        if inconsistent > 0 {
            eprintln!("WARNING: clock '{}' has {}/{} inconsistent period gaps (using first gap = {} ticks)",
                clock_name, inconsistent, rising_ticks.len() - 1, period);
        }

        eprintln!(
            "# clock override: {} (period={} ticks, first_rise={})",
            clock_name, period, first_rise
        );
        Ok((period, first_rise, clock_name.to_string()))
    }

    /// Bulk-read hint from windowed query entry points: decode the
    /// `[0, tick_max]` prefix of all `sig_indices` in ONE FST pass before
    /// the per-signal range reads start. Collapses the pass-per-signal cost
    /// that dominates FST point-query latency, while keeping memory bounded
    /// by the window.
    pub fn prefetch_window(&self, sig_indices: &[usize], tick_max: u64) {
        self.fst.prefetch_to(sig_indices, tick_max);
    }

    /// Read all transitions for a signal by index.
    pub fn read_transitions(&self, sig_idx: usize) -> Vec<(u64, String)> {
        self.fst.read_all(sig_idx)
    }

    /// Read transitions in a tick range.
    /// Returns (before_value, transitions_in_range).
    /// `before_value` is the last value at or before `tick_min` (None if no transition before range).
    pub fn read_transitions_range(
        &self,
        sig_idx: usize,
        tick_min: u64,
        tick_max: u64,
    ) -> (Option<String>, Vec<(u64, String)>) {
        self.fst.read_range(sig_idx, tick_min, tick_max)
    }

    /// Get the value of a signal at a specific tick.
    pub fn value_at_tick_direct(&self, sig_idx: usize, tick: u64) -> String {
        let (before, transitions) = self.read_transitions_range(sig_idx, tick, tick);
        transitions
            .last()
            .map(|(_, v)| v.clone())
            .or(before)
            .unwrap_or_else(|| "x".to_string())
    }

    /// Find cached signal indices matching glob patterns.
    /// Deduplicates by alias group, keeping the LAST matching alias per
    /// group — matches Extractor's `primary_name()` which returns
    /// `sig_names[idx].last()`.
    ///
    /// Exits with code 2 on glob-syntax errors. Used to silently return an
    /// empty match list, which made `list -s "[oops"` look like "no signals
    /// matched" instead of "bad pattern" — and erased the exit-code signal
    /// for scripts and CI.
    pub fn match_signals(&self, patterns: &[String]) -> Vec<usize> {
        let matchers = match compile_patterns(patterns) {
            Ok(m) => m,
            Err(e) => {
                eprintln!("ERROR: {}", e);
                std::process::exit(2);
            }
        };
        // Entries ordered by first occurrence; an alias group keeps the LAST
        // matching alias (matches Extractor's primary_name()).
        let mut group_pos: std::collections::HashMap<u64, usize> = std::collections::HashMap::new();
        let mut results: Vec<usize> = Vec::new();
        // Per-pattern hit flags: a `-s` filter that matches nothing used to
        // disappear without a word whenever a sibling filter did match, so a
        // nine-signal `wave` could quietly render two rows.
        let mut pattern_hit: Vec<bool> = vec![false; matchers.len()];
        for (i, s) in self.signals.iter().enumerate() {
            let mut hit = false;
            for (pi, matcher) in matchers.iter().enumerate() {
                if match_signal(&s.name, std::slice::from_ref(matcher)) {
                    pattern_hit[pi] = true;
                    hit = true;
                }
            }
            if hit {
                if let Some(&p) = group_pos.get(&s.group_id) {
                    results[p] = i; // overwrite with later alias
                } else {
                    group_pos.insert(s.group_id, results.len());
                    results.push(i);
                }
            }
        }
        report_unmatched_patterns(patterns, &pattern_hit, results.is_empty());
        results
    }
}

/// Warn (once per query) about `-s` patterns that matched no signal.
///
/// Silent when every pattern matched, and silent when *nothing* matched at
/// all — the total-miss case is a hard error handled by the callers via
/// `exit_no_signal_match`, so reporting it here would only duplicate it.
fn report_unmatched_patterns(patterns: &[String], hit: &[bool], all_empty: bool) {
    if all_empty {
        return;
    }
    let misses: Vec<&String> = patterns
        .iter()
        .zip(hit.iter())
        .filter(|(_, &h)| !h)
        .map(|(p, _)| p)
        .collect();
    if misses.is_empty() {
        return;
    }
    let mut err = BufWriter::new(io::stderr().lock());
    // One hint per *array base*, not per pattern: `-s mem[0] … mem[15]`
    // would otherwise print 16 near-identical hint blocks.
    let mut hinted_bases = std::collections::HashSet::new();
    for pat in &misses {
        let _ = writeln!(
            err,
            "# WARNING: no signals match '{}' — filter dropped",
            pat
        );
        if looks_like_array_element(pat) && hinted_bases.insert(array_base_name(pat)) {
            let _ = writeln!(
                err,
                "#   (indexed element: simulators do not dump unpacked \
                 array/memory elements by default — probe the element into a \
                 wire, or check `bwave list -s '*{}*'` for what was dumped)",
                array_base_name(pat)
            );
        }
    }
    let _ = writeln!(
        err,
        "# {} of {} -s patterns matched nothing",
        misses.len(),
        patterns.len()
    );
    let _ = err.flush();
}

/// The empty-store diagnostic, shared by the `list` footer (cache path) and
/// the query gate in main.rs. One canonical string so the docs and both call
/// sites can never drift apart.
pub fn no_signals_in_store_message() -> &'static str {
    "waveform store has no signals (header-only trace? see docs: a Verilator \
     sim traced via auto --main produces a header-only trace.fst)"
}

/// The total-miss diagnostic line, shared between the stderr hard error and
/// the JSON-envelope warning so both channels say the same thing.
fn no_match_message(patterns: &[String], total_signals: usize) -> String {
    let listed = patterns
        .iter()
        .map(|p| format!("'{}'", p))
        .collect::<Vec<_>>()
        .join(", ");
    format!(
        "no signals match pattern(s) {} ({} signals in store; try `bwave list` \
         or a broader -s glob)",
        listed, total_signals
    )
}

/// Hard-fail for the total-miss case: *every* pattern matched nothing.
///
/// An empty result with exit 0 reads as "the signal exists and is idle" — the
/// caller then debugs the design instead of the glob. Exit 2 matches the
/// other input errors (bad radix, bad pattern, missing store). Callers that
/// owe consumers a JSON envelope emit it on stdout *before* calling this.
pub(crate) fn exit_no_signal_match(patterns: &[String], total_signals: usize) -> ! {
    let mut err = BufWriter::new(io::stderr().lock());
    let _ = writeln!(err, "ERROR: {}", no_match_message(patterns, total_signals));
    // Same hint as the partial-miss path: an element-indexed name usually
    // means an undumped unpacked array, not a typo. Dedup by array base so
    // a 16-element pattern list prints one hint, not 16.
    let mut hinted_bases = std::collections::HashSet::new();
    for pat in patterns {
        if looks_like_array_element(pat) && hinted_bases.insert(array_base_name(pat)) {
            let _ = writeln!(
                err,
                "  (indexed element: simulators do not dump unpacked \
                 array/memory elements by default — probe the element into a \
                 wire, or check `bwave list -s '*{}*'` for what was dumped)",
                array_base_name(pat)
            );
        }
    }
    let _ = err.flush();
    std::process::exit(2);
}

/// True for patterns ending in a numeric element index — `words[56]`,
/// `mem[0]` — the shape that means "one element of an unpacked array".
fn looks_like_array_element(pattern: &str) -> bool {
    match (pattern.rfind('['), pattern.strip_suffix(']')) {
        (Some(open), Some(_)) => {
            let inner = &pattern[open + 1..pattern.len() - 1];
            !inner.is_empty() && inner.bytes().all(|b| b.is_ascii_digit())
        }
        _ => false,
    }
}

/// The leaf name of an indexed pattern: `tb.dut.u_ke.words[56]` → `words`.
fn array_base_name(pattern: &str) -> &str {
    let base = match pattern.rfind('[') {
        Some(open) => &pattern[..open],
        None => pattern,
    };
    match base.rfind('.') {
        Some(dot) => &base[dot + 1..],
        None => base,
    }
}

// -- Cached query implementations -----------------------------------------

// -- Virtual signal integration ------------------------------------------------

use crate::virtual_signal::{
    build_virtual_transitions, parse_virtual_def, resolve_virtual, ResolvedAtom, ResolvedExpr,
    ResolvedRhs,
};

/// A virtual signal entry, with pre-built transitions.
pub struct VirtualEntry {
    pub name: String,
    pub transitions: Vec<(u64, String)>,
}

fn collect_resolved_rhs_signal_indices(
    rhs: &ResolvedRhs,
    out: &mut std::collections::HashSet<usize>,
) {
    if let ResolvedRhs::Signal(idx, _, _) = rhs {
        out.insert(*idx);
    }
}

fn collect_resolved_expr_signal_indices(
    expr: &ResolvedExpr,
    out: &mut std::collections::HashSet<usize>,
) {
    match expr {
        ResolvedExpr::Atom(atom) => match atom {
            ResolvedAtom::NonZero(idx, _, _) => {
                out.insert(*idx);
            }
            ResolvedAtom::Equal(idx, _, _, rhs)
            | ResolvedAtom::NotEqual(idx, _, _, rhs)
            | ResolvedAtom::Gt(idx, _, _, rhs)
            | ResolvedAtom::Gte(idx, _, _, rhs)
            | ResolvedAtom::Lt(idx, _, _, rhs)
            | ResolvedAtom::Lte(idx, _, _, rhs) => {
                out.insert(*idx);
                collect_resolved_rhs_signal_indices(rhs, out);
            }
            ResolvedAtom::VirtualRef(_) => {}
        },
        ResolvedExpr::Not(inner) => collect_resolved_expr_signal_indices(inner, out),
        ResolvedExpr::Combine(_, children) => {
            for child in children {
                collect_resolved_expr_signal_indices(child, out);
            }
        }
    }
}

/// Build virtual signal entries from cfg.virtual_defs, resolving against cache signals.
/// Returns (resolved_virtuals, their transition lists).
fn build_virtuals(cache: &ColumnCache, cfg: &ExtractConfig) -> Vec<VirtualEntry> {
    if cfg.virtual_defs.is_empty() {
        return Vec::new();
    }

    let signal_names: Vec<String> = cache.signals.iter().map(|s| s.name.clone()).collect();
    let signal_widths: Vec<u32> = cache.signals.iter().map(|s| s.width).collect();
    let mut prior_names: Vec<String> = Vec::new();
    let mut prior_transitions: Vec<Vec<(u64, String)>> = Vec::new();
    let mut entries: Vec<VirtualEntry> = Vec::new();

    // Lazily load only signal columns referenced by virtual expressions. The
    // old eager path decoded every signal in large traces for a two-signal
    // predicate, making simple virtual finds tens of seconds slower.
    let mut real_transitions: Vec<Vec<(u64, String)>> = vec![Vec::new(); cache.signals.len()];
    let mut loaded_real_signals = std::collections::HashSet::new();

    for def_str in &cfg.virtual_defs {
        // Fail-fast on bad virtual defs: previously we set a sticky error
        // flag and let the query run anyway, which let a bad def silently
        // fall through to whatever existing signal happened to glob-match
        // the virtual's name. That was the worst class of silent-success
        // bug (results looked plausible). Exit before any stdout is written.
        let def = match parse_virtual_def(def_str) {
            Ok(d) => d,
            Err(e) => {
                eprintln!("ERROR: --virtual '{}': {}", def_str, e);
                VIRTUAL_DEF_ERROR.store(true, Ordering::SeqCst);
                std::process::exit(2);
            }
        };
        let resolved = match resolve_virtual(&def, &signal_names, &signal_widths, &prior_names) {
            Ok(r) => r,
            Err(e) => {
                eprintln!("ERROR: --virtual '{}': {}", def_str, e);
                VIRTUAL_DEF_ERROR.store(true, Ordering::SeqCst);
                std::process::exit(2);
            }
        };

        let mut referenced_real_signals = std::collections::HashSet::new();
        collect_resolved_expr_signal_indices(&resolved.expr, &mut referenced_real_signals);
        for idx in referenced_real_signals {
            if loaded_real_signals.insert(idx) {
                real_transitions[idx] = cache.read_transitions(idx);
            }
        }

        let transitions = build_virtual_transitions(
            &resolved,
            &prior_transitions,
            &real_transitions,
            cache.sim_start_tick,
            cache.sim_end_tick,
        );

        prior_names.push(def.name.clone());
        prior_transitions.push(transitions.clone());
        entries.push(VirtualEntry {
            name: def.name,
            transitions,
        });
    }

    entries
}

/// Build a radix map: signal_index → Radix, from cfg.signal_radixes.
/// Each (pattern, radix) in signal_radixes is matched against signal names.
///
/// When the same signal index is matched by multiple `-s` patterns that
/// request *different* radixes (e.g. `-s "foo%h" -s "foo%d"`), only the last
/// radix is recorded — we can't emit the same signal twice in one snapshot
/// row. The collision is reported on stderr so users notice; otherwise the
/// extra `-s` flags would silently disappear and look like the radix syntax
/// was broken.
fn build_radix_map(
    cache: &ColumnCache,
    matched: &[usize],
    cfg: &ExtractConfig,
) -> HashMap<usize, Radix> {
    let mut map = HashMap::new();
    if cfg.signal_radixes.is_empty() {
        return map;
    }
    // Track each request (idx, radix) so we can detect collisions even when
    // one of the radixes is the default Hex (which build_radix_map drops).
    let mut requested: HashMap<usize, Vec<Radix>> = HashMap::new();
    for (pat, radix) in &cfg.signal_radixes {
        if let Ok(matchers) = compile_patterns(&[pat.clone()]) {
            for &idx in matched {
                if match_signal(&cache.signals[idx].name, &matchers) {
                    requested.entry(idx).or_default().push(*radix);
                    if *radix != Radix::Hex {
                        map.insert(idx, *radix);
                    }
                }
            }
        }
    }
    // Warn on collisions — multiple distinct radixes for the same signal.
    let stderr = io::stderr();
    let mut err = BufWriter::new(stderr.lock());
    for (&idx, rads) in &requested {
        // Radix doesn't implement Hash, but the variant set is tiny — just
        // dedup by equality.
        let mut distinct: Vec<Radix> = Vec::new();
        for r in rads {
            if !distinct.contains(r) {
                distinct.push(*r);
            }
        }
        if distinct.len() > 1 {
            let _ = writeln!(
                err,
                "# WARNING: signal '{}' matched by multiple -s patterns with different radixes ({:?}); using last: {:?}",
                cache.signals[idx].name,
                distinct,
                map.get(&idx).copied().unwrap_or(Radix::Hex),
            );
        }
    }
    let _ = err.flush();
    map
}

/// Format a value for display, applying per-signal radix if set.
fn fmt_val(val: &str, sig_idx: usize, width: u32, radix_map: &HashMap<usize, Radix>) -> String {
    match radix_map.get(&sig_idx) {
        Some(&radix) => format_value_with_radix(val, width, radix),
        None => val.to_string(),
    }
}

/// Compute value coverage: unique values observed / 2^width.
fn compute_value_pct(n_unique: usize, width: u32) -> f64 {
    if width == 0 || width > 20 {
        return 0.0;
    }
    let possible = 1u64 << width;
    (n_unique as f64 / possible as f64) * 100.0
}

/// Compute toggle coverage: percentage of bits that were observed as both 0 and 1.
fn compute_toggle_pct(value_hist: &HashMap<String, u64>, width: u32) -> f64 {
    if width == 0 {
        return 0.0;
    }
    let n_nibbles = ((width + 3) / 4) as usize;
    let padded: Vec<Vec<u8>> = value_hist
        .keys()
        .map(|v| {
            let nibs: Vec<u8> = v
                .chars()
                .map(|c| c.to_digit(16).unwrap_or(0) as u8)
                .collect();
            let mut p = vec![0u8; n_nibbles.saturating_sub(nibs.len())];
            p.extend(nibs);
            p
        })
        .collect();
    let mut toggled = 0u32;
    for bit_pos in 0..width {
        let nib_idx = n_nibbles - 1 - (bit_pos / 4) as usize;
        let bit_in_nib = bit_pos % 4;
        let (mut s0, mut s1) = (false, false);
        for val in &padded {
            if nib_idx < val.len() {
                if val[nib_idx] & (1 << bit_in_nib) != 0 {
                    s1 = true;
                } else {
                    s0 = true;
                }
            }
            if s0 && s1 {
                break;
            }
        }
        if s0 && s1 {
            toggled += 1;
        }
    }
    (toggled as f64 / width as f64) * 100.0
}

/// Stats entry from cache analysis.
#[derive(Serialize)]
pub struct CacheStatsEntry {
    pub name: String,
    pub width: u32,
    pub transitions: u64,
    pub toggle_pct: f64,
    pub value_pct: f64,
    pub value_hist: HashMap<String, u64>,
    pub time_in_state: HashMap<String, u64>,
}

/// Run stats query from cache.
pub fn stats_from_cache(cache: &ColumnCache, cfg: &ExtractConfig) {
    let matched = cache.match_signals(&cfg.patterns);
    let virtuals = build_virtuals(cache, cfg);

    let total_ticks = cache.sim_end_tick.saturating_sub(cache.sim_start_tick);
    let total_ns = (total_ticks as f64 * cache.ticks_to_ns) as i64;

    if matched.is_empty() && virtuals.is_empty() {
        if cfg.json_format {
            // JSON consumers (coverage_analyst) still get a parseable empty
            // envelope on stdout; the exit code carries the failure.
            let total_cycles = if cache.clock_period_ticks > 0 {
                Some(total_ticks / cache.clock_period_ticks)
            } else {
                None
            };
            let clock_period_ns = if cache.clock_period_ticks > 0 {
                Some((cache.clock_period_ticks as f64 * cache.ticks_to_ns) as i64)
            } else {
                None
            };
            crate::output::emit_json(
                "stats",
                crate::output::StatsData::<crate::output::JsonStatsEntry> {
                    simulation_ns: total_ns,
                    total_ticks,
                    total_cycles,
                    clock_period_ns,
                    signals: Vec::new(),
                },
                vec![no_match_message(&cfg.patterns, cache.unique_signal_count())],
            );
        }
        exit_no_signal_match(&cfg.patterns, cache.unique_signal_count());
    }
    let radix_map = build_radix_map(cache, &matched, cfg);
    let sync_mode = !cfg.async_mode;

    // Determine reset deassert tick for stats rebasing
    let reset_deassert_tick = if !cfg.with_reset && sync_mode {
        find_reset_deassert_tick(cache, cfg)
    } else {
        None
    };

    let stdout = io::stdout();
    let mut out = BufWriter::new(stdout.lock());
    let stderr = io::stderr();
    let mut err = BufWriter::new(stderr.lock());

    // Header (skip in JSON mode — JSON output is self-describing)
    if !cfg.json_format {
        if sync_mode {
            if cache.clock_period_ticks > 0 {
                let total_cycles = total_ticks / cache.clock_period_ticks;
                let period_ns = (cache.clock_period_ticks as f64 * cache.ticks_to_ns) as i64;
                let _ = writeln!(
                    out,
                    "# Simulation: {}ns, {} cycles ({}ns period)",
                    total_ns, total_cycles, period_ns
                );
            } else {
                let _ = writeln!(out, "# Simulation: {}ns", total_ns);
            }
        } else {
            let _ = writeln!(out, "# Simulation: {}ns", total_ns);
        }
        let _ = writeln!(out, "# {} signals analyzed", matched.len() + virtuals.len());
        let _ = writeln!(out, "");
    }

    // VCD file-order at the deassert tick determines which events count.
    // The cache stores a flag indicating whether clock came before reset
    // at the deassert tick. Signals sharing clock's data_offset use > T
    // when clock_before_reset is true; all others use >= T.
    let mut entries: Vec<CacheStatsEntry> = Vec::new();
    let effective_start = reset_deassert_tick.unwrap_or(cache.sim_start_tick);

    // Find clock signal data_offset for boundary disambiguation
    let clock_group_id: Option<u64> = if reset_deassert_tick.is_some() && !cache.clock_id.is_empty()
    {
        cache
            .signals
            .iter()
            .find(|s| {
                let leaf = s.name.rsplit('.').next().unwrap_or(&s.name);
                leaf.starts_with("clk") || leaf == "clock"
            })
            .map(|s| s.group_id)
    } else {
        None
    };

    for &sig_idx in &matched {
        let transitions = cache.read_transitions(sig_idx);
        let sig = &cache.signals[sig_idx];
        let is_clock_sig = clock_group_id == Some(sig.group_id);

        let mut stats = CacheStatsEntry {
            name: sig.name.clone(),
            width: sig.width,
            transitions: 0,
            toggle_pct: 0.0,
            value_pct: 0.0,
            value_hist: HashMap::new(),
            time_in_state: HashMap::new(),
        };

        let mut last_tick = effective_start;
        let mut last_value = "x".to_string();

        // Clock signals at the deassert tick: use > T if clock came before
        // reset in VCD file order (its event was cleared by rebase), else >= T.
        let use_strict =
            is_clock_sig && cache.clock_before_reset_at_deassert && reset_deassert_tick.is_some();
        if use_strict {
            for (tick, val) in &transitions {
                if *tick > effective_start {
                    break;
                }
                last_value = val.clone();
            }
        } else {
            for (tick, val) in &transitions {
                if *tick >= effective_start {
                    break;
                }
                last_value = val.clone();
            }
        }
        let count_from = if use_strict {
            effective_start + 1
        } else {
            effective_start
        };
        for (tick, val) in &transitions {
            if *tick < count_from {
                continue;
            }
            // Same-value rewrites (VCD dump noise) are not transitions:
            // FST dedups them at write time, and counting them here made
            // the metric depend on the simulator's dump style.
            if *val == last_value {
                continue;
            }
            let elapsed = tick.saturating_sub(last_tick);
            *stats.time_in_state.entry(last_value.clone()).or_insert(0) += elapsed;
            stats.transitions += 1;
            *stats.value_hist.entry(val.clone()).or_insert(0) += 1;
            last_value = val.clone();
            last_tick = *tick;
        }
        // Finalize: time from last change to sim_end
        let final_elapsed = cache.sim_end_tick.saturating_sub(last_tick);
        *stats.time_in_state.entry(last_value).or_insert(0) += final_elapsed;
        stats.toggle_pct = compute_toggle_pct(&stats.value_hist, stats.width);
        stats.value_pct = compute_value_pct(stats.value_hist.len(), stats.width);

        entries.push(stats);
    }

    // Virtual signal stats
    for ve in &virtuals {
        let mut stats = CacheStatsEntry {
            name: ve.name.clone(),
            width: 1,
            transitions: 0,
            toggle_pct: 0.0,
            value_pct: 0.0,
            value_hist: HashMap::new(),
            time_in_state: HashMap::new(),
        };

        let mut last_tick = effective_start;
        let mut last_value = "0".to_string();

        for (tick, val) in &ve.transitions {
            if *tick < effective_start {
                last_value = val.clone();
                continue;
            }
            let elapsed = tick.saturating_sub(last_tick);
            *stats.time_in_state.entry(last_value.clone()).or_insert(0) += elapsed;
            stats.transitions += 1;
            *stats.value_hist.entry(val.clone()).or_insert(0) += 1;
            last_value = val.clone();
            last_tick = *tick;
        }
        let final_elapsed = cache.sim_end_tick.saturating_sub(last_tick);
        *stats.time_in_state.entry(last_value).or_insert(0) += final_elapsed;
        stats.toggle_pct = compute_toggle_pct(&stats.value_hist, stats.width);
        stats.value_pct = compute_value_pct(stats.value_hist.len(), stats.width);

        entries.push(stats);
    }

    // Sort by transitions descending
    entries.sort_by(|a, b| b.transitions.cmp(&a.transitions).then(a.name.cmp(&b.name)));

    // JSON output mode — wrap in envelope and return early.
    // v0.2 bugfix: keys are now Verilog literals (matching the text-mode
    // rendering), and `time_in_state` is renamed to `time_in_state_ticks`
    // with a companion `_ns` map. Previously keys were raw cache values
    // ("3", "ff") and the unit on the duration map was ticks but not in
    // the name, which let callers confuse it with nanoseconds.
    if cfg.json_format {
        use crate::format::format_as_verilog_literal;
        let total_cycles = if cache.clock_period_ticks > 0 {
            Some(total_ticks / cache.clock_period_ticks)
        } else {
            None
        };
        let clock_period_ns = if cache.clock_period_ticks > 0 {
            Some((cache.clock_period_ticks as f64 * cache.ticks_to_ns) as i64)
        } else {
            None
        };

        // Build name→sig_idx lookup so we can resolve the per-signal radix
        // for histogram key formatting. Virtuals (not in `matched`) default
        // to Hex.
        let name_to_idx: HashMap<&str, usize> = matched
            .iter()
            .map(|&i| (cache.signals[i].name.as_str(), i))
            .collect();

        let json_entries: Vec<crate::output::JsonStatsEntry> = entries
            .iter()
            .map(|e| {
                let radix = name_to_idx
                    .get(e.name.as_str())
                    .and_then(|i| radix_map.get(i).copied())
                    .unwrap_or(crate::format::Radix::Hex);
                let value_hist: std::collections::BTreeMap<String, u64> = e
                    .value_hist
                    .iter()
                    .map(|(k, v)| (format_as_verilog_literal(k, e.width, radix), *v))
                    .collect();
                let time_in_state_ticks: std::collections::BTreeMap<String, u64> = e
                    .time_in_state
                    .iter()
                    .map(|(k, v)| (format_as_verilog_literal(k, e.width, radix), *v))
                    .collect();
                let time_in_state_ns = if cache.ticks_to_ns > 0.0 {
                    Some(
                        time_in_state_ticks
                            .iter()
                            .map(|(k, ticks)| {
                                (k.clone(), (*ticks as f64 * cache.ticks_to_ns) as i64)
                            })
                            .collect(),
                    )
                } else {
                    None
                };
                crate::output::JsonStatsEntry {
                    name: e.name.clone(),
                    width: e.width,
                    transitions: e.transitions,
                    toggle_pct: e.toggle_pct,
                    value_pct: e.value_pct,
                    value_hist,
                    time_in_state_ticks,
                    time_in_state_ns,
                }
            })
            .collect();

        crate::output::emit_json(
            "stats",
            crate::output::StatsData {
                simulation_ns: total_ns,
                total_ticks,
                total_cycles,
                clock_period_ns,
                signals: json_entries,
            },
            Vec::new(),
        );
        return;
    }

    // Common scope prefix
    let all_names: Vec<String> = entries.iter().map(|e| e.name.clone()).collect();
    let prefix = crate::signal::common_scope_prefix(&all_names);
    if !prefix.is_empty() {
        let _ = writeln!(err, "# scope: {}", &prefix[..prefix.len() - 1]);
    }

    for entry in &entries {
        let dname = if !prefix.is_empty() && entry.name.starts_with(&prefix) {
            &entry.name[prefix.len()..]
        } else {
            &entry.name
        };
        let width_str = if entry.width == 1 {
            "1-bit".to_string()
        } else {
            format!("{}-bit", entry.width)
        };
        let n_unique = entry.value_hist.len();
        let _ = writeln!(
            out,
            "{}  {};  {} transitions;  {} unique values;",
            dname, width_str, entry.transitions, n_unique
        );
        let _ = writeln!(out, "");

        // Coverage lines
        let _ = writeln!(out, "Toggle coverage: {:.0}%", entry.toggle_pct);
        if entry.value_pct > 0.0 {
            let _ = writeln!(out, "Value coverage: {:.0}%", entry.value_pct);
        }
        let _ = writeln!(out, "");

        if !entry.time_in_state.is_empty() && total_ticks > 0 {
            // Look up radix for this signal (find by name since entries are sorted)
            let sig_radix = matched
                .iter()
                .find(|&&i| cache.signals[i].name == entry.name)
                .and_then(|&i| radix_map.get(&i).copied());
            let mut sorted_states: Vec<(&String, &u64)> = entry.time_in_state.iter().collect();
            sorted_states.sort_by(|a, b| b.1.cmp(a.1).then(a.0.cmp(b.0)));
            let mut zero_pct_count: usize = 0;
            for (val, ticks) in &sorted_states {
                let t = **ticks;
                let pct = (t as f64 / total_ticks as f64) * 100.0;
                if pct < 0.5 {
                    zero_pct_count += 1;
                    continue;
                }
                let dval = match sig_radix {
                    Some(r) => format_value_with_radix(val, entry.width, r),
                    None => (*val).clone(),
                };
                let pct_str = if pct < 1.0 {
                    "<1".to_string()
                } else {
                    format!("{:.0}", pct)
                };
                if sync_mode && cache.clock_period_ticks > 0 {
                    let duration = t / cache.clock_period_ticks;
                    let _ = writeln!(out, "{}: {}% ({} cyc)", dval, pct_str, duration);
                } else {
                    let duration_ns = (t as f64 * cache.ticks_to_ns) as i64;
                    let _ = writeln!(out, "{}: {}% ({}ns)", dval, pct_str, duration_ns);
                }
            }
            if zero_pct_count > 0 {
                let _ = writeln!(out, "rest: <1% ({} values)", zero_pct_count);
            }
        }
        let _ = writeln!(out, "");
    }

    let _ = out.flush();
    let _ = err.flush();
}

/// Run find-stuck query from cache (signals with 0 transitions).
pub fn find_stuck_from_cache(cache: &ColumnCache, cfg: &ExtractConfig) {
    let matched = cache.match_signals(&cfg.patterns);
    if matched.is_empty() {
        // A stuck-scan over zero signals would report "nothing stuck" — a
        // false all-clear. Total miss is the same hard error as elsewhere.
        exit_no_signal_match(&cfg.patterns, cache.unique_signal_count());
    }
    let total_ticks = cache.sim_end_tick.saturating_sub(cache.sim_start_tick);
    let sync_mode = !cfg.async_mode;

    let reset_deassert_tick = if !cfg.with_reset && sync_mode {
        find_reset_deassert_tick(cache, cfg)
    } else {
        None
    };
    let effective_start = reset_deassert_tick.unwrap_or(cache.sim_start_tick);

    let radix_map = build_radix_map(cache, &matched, cfg);
    let stdout = io::stdout();
    let mut out = BufWriter::new(stdout.lock());

    let mut stuck: Vec<(String, String, String, usize)> = Vec::new(); // +sig_idx
    for &sig_idx in &matched {
        let transitions = cache.read_transitions(sig_idx);
        let sig = &cache.signals[sig_idx];

        let count_from = effective_start;
        let mut trans_count = 0u64;
        let mut last_value = "x".to_string();
        for (tick, val) in &transitions {
            if *tick >= count_from {
                break;
            }
            last_value = val.clone();
        }
        let stuck_value = last_value.clone();
        for (tick, _) in &transitions {
            if *tick < count_from {
                continue;
            }
            trans_count += 1;
        }

        if trans_count > 0 {
            continue;
        }

        let all_names: Vec<String> = matched
            .iter()
            .map(|&i| cache.signals[i].name.clone())
            .collect();
        let prefix = crate::signal::common_scope_prefix(&all_names);
        let dname = if !prefix.is_empty() && sig.name.starts_with(&prefix) {
            sig.name[prefix.len()..].to_string()
        } else {
            sig.name.clone()
        };
        let width_str = if sig.width == 1 {
            "1-bit".to_string()
        } else {
            format!("{}-bit", sig.width)
        };
        stuck.push((dname, width_str, stuck_value, sig_idx));
    }

    // Apply value filter
    if let Some(ref filter) = cfg.find_stuck {
        if !filter.is_empty() {
            let vf = filter.to_lowercase();
            stuck.retain(|(_, _, v, _)| {
                if vf == "z" {
                    v.to_lowercase().contains('z')
                } else if vf == "x" {
                    v.to_lowercase().contains('x')
                } else {
                    v == filter
                }
            });
        }
    }

    let duration_str = if sync_mode && cache.clock_period_ticks > 0 {
        format!("{} cycles", total_ticks / cache.clock_period_ticks)
    } else {
        format!("{}ns", (total_ticks as f64 * cache.ticks_to_ns) as i64)
    };

    let filter_str = match &cfg.find_stuck {
        Some(f) if !f.is_empty() => format!(" (filter: {})", f),
        _ => String::new(),
    };
    let _ = writeln!(
        out,
        "# Stuck signals: {} of {} analyzed{}",
        stuck.len(),
        matched.len(),
        filter_str
    );
    if stuck.is_empty() {
        let _ = out.flush();
        return;
    }
    stuck.sort_by(|a, b| a.0.cmp(&b.0));
    for (name, width_str, stuck_val, sig_idx) in &stuck {
        let dval = fmt_val(
            stuck_val,
            *sig_idx,
            cache.signals[*sig_idx].width,
            &radix_map,
        );
        let _ = writeln!(
            out,
            "  {}  {}  stuck at {}  (100%, {})",
            name, width_str, dval, duration_str
        );
    }
    let _ = out.flush();
}

/// Run find-value query from cache.
pub fn find_value_from_cache(cache: &ColumnCache, cfg: &ExtractConfig) {
    let find_pattern = match cfg.find_pattern.as_ref() {
        Some(p) => p,
        None => return,
    };
    let find_value_str = match cfg.find_value.as_ref() {
        Some(v) => v,
        None => return,
    };

    let find_matchers = match compile_patterns(&[find_pattern.clone()]) {
        Ok(m) => m,
        Err(e) => {
            eprintln!("ERROR: {}", e);
            return;
        }
    };
    let target_indices: Vec<usize> = {
        let mut seen = std::collections::HashSet::new();
        cache
            .signals
            .iter()
            .enumerate()
            .filter(|(_, s)| match_signal(&s.name, &find_matchers))
            .filter(|(_, s)| seen.insert(s.group_id))
            .map(|(i, _)| i)
            .collect()
    };

    // Also check virtual signals early so we can report "no match" correctly
    let virtuals = build_virtuals(cache, cfg);
    let virtual_matches: Vec<&VirtualEntry> = virtuals
        .iter()
        .filter(|ve| match_signal(&ve.name, &find_matchers))
        .collect();

    if target_indices.is_empty() && virtual_matches.is_empty() {
        if cfg.json_format {
            // Emit an empty envelope so consumers get valid JSON to parse.
            let mode = if cfg.async_mode { "async" } else { "sync" };
            crate::output::emit_json(
                "find",
                crate::output::FindData {
                    scope_prefix: String::new(),
                    pattern: find_pattern.clone(),
                    value: find_value_str.clone(),
                    mode: mode.to_string(),
                    unit: "tick".to_string(),
                    count: 0,
                    matches: Vec::new(),
                    truncated: false,
                    first_only: cfg.first_match,
                    last_only: cfg.last_match,
                    count_only: cfg.count_only,
                },
                vec![format!("no signals match pattern '{}'", find_pattern)],
            );
        }
        // Total miss is a hard error either way; JSON consumers got their
        // envelope above, the exit code says the pattern was wrong.
        exit_no_signal_match(
            std::slice::from_ref(find_pattern),
            cache.unique_signal_count(),
        );
    }

    let radix_map = build_radix_map(cache, &target_indices, cfg);
    let edge_mode = is_edge_keyword(find_value_str).map(String::from);
    let sync_mode = !cfg.async_mode;

    let reset_deassert_tick = if !cfg.with_reset && sync_mode {
        find_reset_deassert_tick(cache, cfg)
    } else {
        None
    };
    let effective_start = reset_deassert_tick.unwrap_or(0);

    // Compute display names from watched patterns (matches Extractor's display_name)
    let watched = cache.match_signals(&cfg.patterns);
    let all_names: Vec<String> = watched
        .iter()
        .map(|&i| cache.signals[i].name.clone())
        .collect();
    let prefix = crate::signal::common_scope_prefix(&all_names);

    let stdout = io::stdout();
    let mut out = BufWriter::new(stdout.lock());
    let stderr = io::stderr();
    let mut err = BufWriter::new(stderr.lock());

    let json_format = cfg.json_format;
    if !prefix.is_empty() && !json_format {
        let _ = writeln!(err, "# scope: {}", &prefix[..prefix.len() - 1]);
    }

    let mut find_count: usize = 0;
    let mut line_count: usize = 0;
    let mut last_result: Option<String> = None;
    let mut json_matches: Vec<crate::output::FindMatch> = Vec::new();
    let mut last_json: Option<crate::output::FindMatch> = None;
    let mut truncated = false;
    let cb = cycle_base(cache, effective_start);

    let use_cycle_walk = sync_mode && edge_mode.is_none() && cache.clock_period_ticks > 0 && cb > 0;

    // Closure that records one match. In JSON mode it pushes to `json_matches`
    // (or `last_json` for --last); in text mode it streams writeln. `raw_val`
    // is the unformatted cache value (used for JSON); `dval` is the
    // radix-formatted display string (used for text).
    //
    // Returns true if the caller should break the outer signal loop because
    // the line limit was reached. (`first_match` short-circuit is handled by
    // the caller — it has additional flush/return logic.)
    macro_rules! record_match {
        ($time:expr, $dname:expr, $raw_val:expr, $dval:expr) => {{
            find_count += 1;
            let time_u64: u64 = $time;
            let name_str: String = $dname.to_string();
            let raw_str: String = $raw_val;
            if cfg.last_match {
                if json_format {
                    last_json = Some(crate::output::FindMatch {
                        time: time_u64,
                        name: name_str.clone(),
                        value: raw_str.clone(),
                    });
                }
                let line = if sync_mode && cache.clock_period_ticks > 0 {
                    format!("cycle {} {} {}", time_u64, &name_str, $dval)
                } else {
                    format!("{} {} {}", time_u64, &name_str, $dval)
                };
                last_result = Some(line);
            } else if !cfg.count_only {
                if json_format {
                    json_matches.push(crate::output::FindMatch {
                        time: time_u64,
                        name: name_str.clone(),
                        value: raw_str.clone(),
                    });
                } else if sync_mode && cache.clock_period_ticks > 0 {
                    let _ = writeln!(out, "cycle {} {} {}", time_u64, &name_str, $dval);
                } else {
                    let _ = writeln!(out, "{} {} {}", time_u64, &name_str, $dval);
                }
                line_count += 1;
                if line_count >= cfg.max_lines {
                    truncated = true;
                    if !json_format {
                        let _ = writeln!(
                            err,
                            "# WARNING: limit ({}) reached, output truncated",
                            cfg.max_lines
                        );
                    }
                }
            }
        }};
    }

    'outer: for &sig_idx in &target_indices {
        let transitions = cache.read_transitions(sig_idx);
        let sig = &cache.signals[sig_idx];
        // Use the find-matched signal's own name (not a watched-set alias
        // that may share the same data_offset).
        let full_name: &str = &sig.name;
        let dname = if !prefix.is_empty() && full_name.starts_with(&prefix) {
            &full_name[prefix.len()..]
        } else {
            full_name
        };

        if use_cycle_walk {
            let total_cycles =
                (cache.sim_end_tick.saturating_sub(cb)) / cache.clock_period_ticks + 1;
            let start_cycle = if cfg.time_min > 0 {
                cfg.time_min as u64
            } else {
                1
            };
            let end_cycle = match cfg.time_max {
                Some(max) if max > 0 => std::cmp::min(max as u64, total_cycles),
                _ => total_cycles,
            };

            for cycle in start_cycle..=end_cycle {
                let tick = cb + (cycle - 1) * cache.clock_period_ticks;
                let val = value_at_tick(&transitions, tick);
                if values_match(&val, find_value_str) {
                    let dval = fmt_val(&val, sig_idx, sig.width, &radix_map);
                    record_match!(cycle, dname, val.clone(), dval);
                    if truncated {
                        break 'outer;
                    }
                    if cfg.first_match {
                        break 'outer;
                    }
                }
            }
        } else {
            // Edge mode or async: walk transitions only
            let mut prev_val = "x".to_string();
            for (tick, val) in &transitions {
                if *tick < effective_start {
                    prev_val = val.clone();
                    continue;
                }

                if sync_mode {
                    if cache.clock_period_ticks > 0 && cb > 0 {
                        let cycle = tick_to_cycle(*tick, cb, cache.clock_period_ticks);
                        if (cycle as i64) < cfg.time_min {
                            prev_val = val.clone();
                            continue;
                        }
                        if let Some(max) = cfg.time_max {
                            if (cycle as i64) > max {
                                break;
                            }
                        }
                    }
                } else {
                    if (*tick as i64) < cfg.time_min {
                        prev_val = val.clone();
                        continue;
                    }
                    if let Some(max) = cfg.time_max {
                        if (*tick as i64) > max {
                            break;
                        }
                    }
                }

                let matched = if let Some(ref edge) = edge_mode {
                    let m = check_edge(&prev_val, val, edge);
                    prev_val = val.clone();
                    m
                } else {
                    prev_val = val.clone();
                    values_match(val, find_value_str)
                };

                if matched {
                    let dval = fmt_val(val, sig_idx, sig.width, &radix_map);
                    let time = if sync_mode && cache.clock_period_ticks > 0 {
                        tick_to_cycle(*tick, cb, cache.clock_period_ticks)
                    } else {
                        *tick
                    };
                    record_match!(time, dname, val.clone(), dval);
                    if truncated {
                        break 'outer;
                    }
                    if cfg.first_match {
                        break 'outer;
                    }
                }
            }
        }
    }

    // Search virtual signals whose names match find_pattern.
    // (Virtuals already use raw cache values — no fmt_val applied.)
    'vouter: for ve in &virtual_matches {
        let dname = &ve.name;

        if edge_mode.is_some() {
            let mut prev_val = "x".to_string();
            for (tick, val) in &ve.transitions {
                if *tick < effective_start {
                    prev_val = val.clone();
                    continue;
                }
                let m = if let Some(ref edge) = edge_mode {
                    let m = check_edge(&prev_val, val, edge);
                    prev_val = val.clone();
                    m
                } else {
                    prev_val = val.clone();
                    values_match(val, find_value_str)
                };
                if m {
                    let time = if sync_mode && cache.clock_period_ticks > 0 {
                        tick_to_cycle(*tick, cb, cache.clock_period_ticks)
                    } else {
                        *tick
                    };
                    record_match!(time, dname.as_str(), val.clone(), val);
                    if truncated {
                        break 'vouter;
                    }
                    if cfg.first_match {
                        break 'vouter;
                    }
                }
            }
        } else if use_cycle_walk {
            let total_cycles =
                (cache.sim_end_tick.saturating_sub(cb)) / cache.clock_period_ticks + 1;
            let start_cycle = if cfg.time_min > 0 {
                cfg.time_min as u64
            } else {
                1
            };
            let end_cycle = match cfg.time_max {
                Some(max) if max > 0 => std::cmp::min(max as u64, total_cycles),
                _ => total_cycles,
            };
            for cycle in start_cycle..=end_cycle {
                let tick = cb + (cycle - 1) * cache.clock_period_ticks;
                let val = value_at_tick(&ve.transitions, tick);
                if values_match(&val, find_value_str) {
                    record_match!(cycle, dname.as_str(), val.clone(), &val);
                    if truncated {
                        break 'vouter;
                    }
                    if cfg.first_match {
                        break 'vouter;
                    }
                }
            }
        } else {
            // Async mode, or sync without a usable clock: walk virtual
            // transitions directly with level-value matching. Mirrors the
            // real-signal else-branch above so virtuals don't get silently
            // skipped (the buggy fall-through pre-fix).
            for (tick, val) in &ve.transitions {
                if *tick < effective_start {
                    continue;
                }

                if sync_mode {
                    if cache.clock_period_ticks > 0 && cb > 0 {
                        let cycle = tick_to_cycle(*tick, cb, cache.clock_period_ticks);
                        if (cycle as i64) < cfg.time_min {
                            continue;
                        }
                        if let Some(max) = cfg.time_max {
                            if (cycle as i64) > max {
                                break;
                            }
                        }
                    }
                } else {
                    if (*tick as i64) < cfg.time_min {
                        continue;
                    }
                    if let Some(max) = cfg.time_max {
                        if (*tick as i64) > max {
                            break;
                        }
                    }
                }

                if values_match(val, find_value_str) {
                    let time = if sync_mode && cache.clock_period_ticks > 0 {
                        tick_to_cycle(*tick, cb, cache.clock_period_ticks)
                    } else {
                        *tick
                    };
                    record_match!(time, dname.as_str(), val.clone(), val);
                    if truncated {
                        break 'vouter;
                    }
                    if cfg.first_match {
                        break 'vouter;
                    }
                }
            }
        }
    }

    // Emit results.
    if json_format {
        let scope_prefix = if prefix.is_empty() {
            String::new()
        } else {
            prefix[..prefix.len() - 1].to_string()
        };
        let mode = if sync_mode { "sync" } else { "async" };
        let unit = if sync_mode && cache.clock_period_ticks > 0 {
            "cycle"
        } else {
            "tick"
        };

        // For --last we still publish the single best match (if any) in
        // `matches`; downstream consumers don't need to special-case last_only.
        let mut final_matches = json_matches;
        if cfg.last_match {
            if let Some(m) = last_json.take() {
                final_matches.push(m);
            }
        }

        crate::output::emit_json(
            "find",
            crate::output::FindData {
                scope_prefix,
                pattern: find_pattern.clone(),
                value: find_value_str.clone(),
                mode: mode.to_string(),
                unit: unit.to_string(),
                count: find_count,
                matches: final_matches,
                truncated,
                first_only: cfg.first_match,
                last_only: cfg.last_match,
                count_only: cfg.count_only,
            },
            Vec::new(),
        );
        return;
    }

    if cfg.last_match {
        if let Some(line) = last_result {
            let _ = writeln!(out, "{}", line);
        }
    }

    if cfg.count_only {
        let _ = writeln!(out, "{}", find_count);
    } else if find_count > 0 && !cfg.first_match && !cfg.last_match {
        let _ = writeln!(err, "# {} matches found", find_count);
    } else if find_count == 0 {
        let _ = writeln!(err, "# No matches found");
    }
    let _ = out.flush();
    let _ = err.flush();
}

/// Run sample-at query from cache.
pub fn sample_at_from_cache(cache: &ColumnCache, cfg: &ExtractConfig) {
    let sa_pattern = match cfg.sample_at_pattern.as_ref() {
        Some(p) => p,
        None => return,
    };
    let sa_value = cfg.sample_at_value.as_deref().unwrap_or("");

    // Find trigger signals
    let sa_matchers = match compile_patterns(&[sa_pattern.clone()]) {
        Ok(m) => m,
        Err(e) => {
            eprintln!("ERROR: {}", e);
            return;
        }
    };
    let trigger_indices: Vec<usize> = {
        let mut seen = std::collections::HashSet::new();
        cache
            .signals
            .iter()
            .enumerate()
            .filter(|(_, s)| match_signal(&s.name, &sa_matchers))
            .filter(|(_, s)| seen.insert(s.group_id))
            .map(|(i, _)| i)
            .collect()
    };

    if trigger_indices.is_empty() {
        // Same class of failure as the other total-miss exits — unified to 2
        // (was 1) so every "your pattern matched nothing" ends the same way.
        eprintln!(
            "ERROR: --sample: no signals match trigger pattern '{}'",
            sa_pattern
        );
        std::process::exit(2);
    }

    // Determine trigger mode from VALUE keyword
    let edge_kw = is_edge_keyword(sa_value).map(String::from);
    let change_mode = edge_kw.as_deref() == Some("change");
    let edge_mode = if change_mode { None } else { edge_kw };
    let level_mode = edge_mode.is_none() && !change_mode;

    let sync_mode = !cfg.async_mode;
    let reset_deassert_tick = if !cfg.with_reset && sync_mode {
        find_reset_deassert_tick(cache, cfg)
    } else {
        None
    };
    let effective_start = reset_deassert_tick.unwrap_or(0);

    let cb = cycle_base(cache, effective_start);

    // Read trigger signal transitions and identify trigger ticks.
    // For level mode in sync: check every cycle, like the Extractor does.
    let mut trigger_ticks: Vec<u64> = Vec::new();

    if level_mode && sync_mode && cache.clock_period_ticks > 0 && cb > 0 {
        // Sync level mode: walk every cycle, check trigger value at each
        let total_cycles = (cache.sim_end_tick.saturating_sub(cb)) / cache.clock_period_ticks + 1;
        let trig_data: Vec<Vec<(u64, String)>> = trigger_indices
            .iter()
            .map(|&i| cache.read_transitions(i))
            .collect();
        for cycle in 1..=total_cycles {
            let tick = cb + (cycle - 1) * cache.clock_period_ticks;
            for trig_trans in &trig_data {
                if values_match(&value_at_tick(trig_trans, tick), sa_value) {
                    trigger_ticks.push(tick);
                    break;
                }
            }
        }
    } else if level_mode && !sync_mode {
        // Async level mode: trigger at every watched-signal-change timestamp
        // where the trigger value matches (mirrors Extractor's timestamp callback)
        let watched_indices = cache.match_signals(&cfg.patterns);
        let mut change_ticks: std::collections::BTreeSet<u64> = std::collections::BTreeSet::new();
        for &w_idx in &watched_indices {
            for (tick, _) in &cache.read_transitions(w_idx) {
                if *tick >= effective_start {
                    change_ticks.insert(*tick);
                }
            }
        }
        let trig_data: Vec<Vec<(u64, String)>> = trigger_indices
            .iter()
            .map(|&i| cache.read_transitions(i))
            .collect();
        for tick in &change_ticks {
            for trig_trans in &trig_data {
                if values_match(&value_at_tick(trig_trans, *tick), sa_value) {
                    trigger_ticks.push(*tick);
                    break;
                }
            }
        }
    } else {
        for &trig_idx in &trigger_indices {
            let transitions = cache.read_transitions(trig_idx);
            let mut prev_val = "x".to_string();
            for (tick, val) in &transitions {
                if *tick < effective_start {
                    prev_val = val.clone();
                    continue;
                }
                let triggered = if let Some(ref edge) = edge_mode {
                    check_edge(&prev_val, val, edge)
                } else if change_mode {
                    prev_val != *val
                } else if level_mode {
                    values_match(val, sa_value)
                } else {
                    false
                };
                prev_val = val.clone();
                if triggered {
                    trigger_ticks.push(*tick);
                }
            }
        }
        trigger_ticks.sort();
        trigger_ticks.dedup();

        // Extractor quirk: sample_at_triggered is not cleared on reset rebase.
        // If any trigger signal changed during reset (from initial "x"), the
        // flag carries over to the first post-reset cycle. Simulate this by
        // injecting effective_start as a trigger if there were pre-reset events.
        if change_mode && effective_start > 0 {
            let has_pre_reset = trigger_indices.iter().any(|&idx| {
                let trans = cache.read_transitions(idx);
                trans.iter().any(|(t, _)| *t < effective_start)
            });
            if has_pre_reset && (trigger_ticks.is_empty() || trigger_ticks[0] > cb) {
                trigger_ticks.insert(0, cb);
            }
        }
    }

    // For each trigger tick, sample all watched signals
    let watched_indices = cache.match_signals(&cfg.patterns);
    let radix_map = build_radix_map(cache, &watched_indices, cfg);
    let all_names: Vec<String> = watched_indices
        .iter()
        .map(|&i| cache.signals[i].name.clone())
        .collect();
    let prefix = crate::signal::common_scope_prefix(&all_names);

    let stdout = io::stdout();
    let mut out = BufWriter::new(stdout.lock());
    let stderr = io::stderr();
    let mut err = BufWriter::new(stderr.lock());

    if !prefix.is_empty() {
        let _ = writeln!(err, "# scope: {}", &prefix[..prefix.len() - 1]);
    }
    let _ = writeln!(err, "# sample: {} trigger signal(s)", trigger_indices.len());

    // Preload watched signal transitions for binary search
    let watched_data: Vec<Vec<(u64, String)>> = watched_indices
        .iter()
        .map(|&i| cache.read_transitions(i))
        .collect();

    let mut sample_count: usize = 0;
    let mut line_count: usize = 0;

    for &trigger_tick in &trigger_ticks {
        // Time range filtering
        if sync_mode && cache.clock_period_ticks > 0 {
            let cycle = tick_to_cycle(
                trigger_tick,
                cycle_base(cache, effective_start),
                cache.clock_period_ticks,
            );
            if (cycle as i64) < cfg.time_min {
                continue;
            }
            if let Some(max) = cfg.time_max {
                if (cycle as i64) > max {
                    break;
                }
            }
        } else {
            if (trigger_tick as i64) < cfg.time_min {
                continue;
            }
            if let Some(max) = cfg.time_max {
                if (trigger_tick as i64) > max {
                    break;
                }
            }
        }

        sample_count += 1;
        if cfg.count_only {
            continue;
        }

        let time_label = if sync_mode && cache.clock_period_ticks > 0 {
            let cycle = tick_to_cycle(
                trigger_tick,
                cycle_base(cache, effective_start),
                cache.clock_period_ticks,
            );
            cycle.to_string()
        } else {
            trigger_tick.to_string()
        };

        for (wi, &w_idx) in watched_indices.iter().enumerate() {
            let sig = &cache.signals[w_idx];
            let dname = if !prefix.is_empty() && sig.name.starts_with(&prefix) {
                &sig.name[prefix.len()..]
            } else {
                &sig.name
            };
            // Binary search for value at trigger_tick
            let val = value_at_tick(&watched_data[wi], trigger_tick);
            let dval = fmt_val(&val, w_idx, cache.signals[w_idx].width, &radix_map);
            let _ = writeln!(out, "{} {} {}", time_label, dname, dval);
            line_count += 1;
            if line_count >= cfg.max_lines {
                let _ = writeln!(
                    err,
                    "# WARNING: limit ({}) reached, output truncated",
                    cfg.max_lines
                );
                break;
            }
        }
        if line_count >= cfg.max_lines {
            break;
        }
    }

    if cfg.count_only {
        let _ = writeln!(out, "{}", sample_count);
    } else {
        let _ = writeln!(err, "# {} trigger events", sample_count);
    }
    let _ = out.flush();
    let _ = err.flush();
}

// -- Snapshot query (--at-cycle / --at-time) from cache ------------------

pub fn snapshot_from_cache(cache: &ColumnCache, cfg: &ExtractConfig) {
    let at_time = match cfg.at_time {
        Some(t) => t,
        None => return,
    };

    let matched = cache.match_signals(&cfg.patterns);
    if matched.is_empty() {
        if cfg.json_format {
            let mode = if cfg.async_mode { "async" } else { "sync" };
            crate::output::emit_json(
                "value",
                crate::output::ValueData {
                    scope_prefix: String::new(),
                    mode: mode.to_string(),
                    at: at_time,
                    at_unit: "cycle".to_string(),
                    target_tick: 0,
                    time_label: String::new(),
                    signals: Vec::new(),
                },
                vec!["no signals match pattern".to_string()],
            );
        }
        // Total miss is a hard error either way; JSON consumers got their
        // envelope above, the exit code says the pattern was wrong.
        exit_no_signal_match(&cfg.patterns, cache.unique_signal_count());
    }

    // Sync mode requires both --at-cycle flag AND a detected clock
    let sync_mode = cfg.at_time_is_cycle && cache.clock_period_ticks > 0;

    let all_names: Vec<String> = matched
        .iter()
        .map(|&i| cache.signals[i].name.clone())
        .collect();
    let prefix = crate::signal::common_scope_prefix(&all_names);

    let stdout = io::stdout();
    let mut out = BufWriter::new(stdout.lock());
    let stderr = io::stderr();
    let mut err = BufWriter::new(stderr.lock());

    // JSON mode collects diagnostics into the envelope's `warnings` instead
    // of streaming `# ...` lines to stderr.
    let mut json_warnings: Vec<String> = Vec::new();

    if !prefix.is_empty() {
        if cfg.json_format {
            // scope_prefix is published in data; no need to mirror to warnings.
        } else {
            let _ = writeln!(err, "# scope: {}", &prefix[..prefix.len() - 1]);
        }
    }

    // No clock → fall back to async: treat at_time as raw timestamp
    if cfg.at_time_is_cycle && cache.clock_period_ticks == 0 {
        if cfg.json_format {
            json_warnings.push("no clock signal found, falling back to async mode".to_string());
        } else {
            let _ = writeln!(
                err,
                "# WARNING: no clock signal found, falling back to async mode"
            );
        }
    }

    let reset_deassert_tick = if !cfg.with_reset && sync_mode {
        find_reset_deassert_tick(cache, cfg)
    } else {
        None
    };
    let effective_start = reset_deassert_tick.unwrap_or(0);

    let target_tick = if sync_mode {
        let cb = cycle_base(cache, effective_start);
        if at_time == 0 {
            // cycle 0 = snapshot at effective_start (before first post-reset edge)
            effective_start
        } else {
            let total_cycles =
                (cache.sim_end_tick.saturating_sub(cb)) / cache.clock_period_ticks + 1;
            if at_time as u64 > total_cycles {
                let _ = writeln!(err,
                    "ERROR: --at-time {} (cycle) is beyond simulation range (sim length: {} cycles)",
                    at_time, total_cycles);
                let sim_start_ns = (cache.sim_start_tick as f64 * cache.ticks_to_ns) as i64;
                let sim_end_ns = (cache.sim_end_tick as f64 * cache.ticks_to_ns) as i64;
                let _ = writeln!(err,
                    "HINT: did you mean --at-time {} with --async? In sync mode, --at-time expects a cycle number (0..{}). Simulation time range: {}ns..{}ns",
                    at_time, total_cycles, sim_start_ns, sim_end_ns);
                let _ = err.flush();
                return;
            }
            cb + (at_time as u64 - 1) * cache.clock_period_ticks
        }
    } else if cfg.async_mode || cfg.at_time_is_cycle {
        // Typed async tokens have already been resolved to raw ticks. A sync
        // cycle token also lands here when no clock exists, preserving the
        // historical no-clock fallback.
        at_time as u64
    } else {
        // Legacy unresolved --at-time mode: convert ns to ticks.
        if cache.ticks_to_ns > 0.0 {
            (at_time as f64 / cache.ticks_to_ns) as u64
        } else {
            at_time as u64
        }
    };

    let time_label = if sync_mode {
        if at_time == 0 {
            "cycle 0 (before first post-reset clock edge)".to_string()
        } else {
            format!("cycle {}", at_time)
        }
    } else {
        format!("{}", at_time)
    };

    let radix_map = build_radix_map(cache, &matched, cfg);
    cache.prefetch_window(&matched, target_tick);
    let values: Vec<String> = matched
        .par_iter()
        .map(|&sig_idx| cache.value_at_tick_direct(sig_idx, target_tick))
        .collect();

    if cfg.json_format {
        let scope_prefix = if prefix.is_empty() {
            String::new()
        } else {
            prefix[..prefix.len() - 1].to_string()
        };
        let mode = if sync_mode { "sync" } else { "async" };
        let at_unit = if sync_mode {
            "cycle"
        } else if cfg.async_mode || cfg.at_time_is_cycle {
            "tick"
        } else {
            "ns"
        };
        let signals: Vec<crate::output::SignalValue> = matched
            .iter()
            .enumerate()
            .map(|(idx, &sig_idx)| {
                let sig = &cache.signals[sig_idx];
                let name = if !prefix.is_empty() && sig.name.starts_with(&prefix) {
                    sig.name[prefix.len()..].to_string()
                } else {
                    sig.name.clone()
                };
                // Raw cache value — text mode still applies radix via fmt_val below.
                crate::output::SignalValue {
                    name,
                    value: values[idx].clone(),
                }
            })
            .collect();
        crate::output::emit_json(
            "value",
            crate::output::ValueData {
                scope_prefix,
                mode: mode.to_string(),
                at: at_time,
                at_unit: at_unit.to_string(),
                target_tick,
                time_label,
                signals,
            },
            json_warnings,
        );
        return;
    }

    let _ = writeln!(out, "# Snapshot at {}", time_label);

    for (idx, &sig_idx) in matched.iter().enumerate() {
        let sig = &cache.signals[sig_idx];
        let dname = if !prefix.is_empty() && sig.name.starts_with(&prefix) {
            &sig.name[prefix.len()..]
        } else {
            &sig.name
        };
        let dval = fmt_val(&values[idx], sig_idx, sig.width, &radix_map);
        let _ = writeln!(out, "{:<40} = {}", dname, dval);
    }
    let _ = out.flush();
    let _ = err.flush();
}

// -- Resolve user time to simulation tick ----------------------------------

/// Convert a user-facing time value to a simulation tick.
/// In sync mode (is_cycle=true, clock present): time is a cycle number.
/// In async mode or no-clock fallback: time is ns (converted via ticks_to_ns).
/// Returns None if the time is out of simulation range.
fn resolve_time_to_tick(
    cache: &ColumnCache,
    cfg: &ExtractConfig,
    time: i64,
    effective_start: u64,
) -> Option<u64> {
    let sync_mode = !cfg.async_mode && cache.clock_period_ticks > 0;
    if sync_mode {
        let cb = cycle_base(cache, effective_start);
        if time == 0 {
            Some(effective_start)
        } else {
            let total_cycles =
                (cache.sim_end_tick.saturating_sub(cb)) / cache.clock_period_ticks + 1;
            if time as u64 > total_cycles {
                None
            } else {
                Some(cb + (time as u64 - 1) * cache.clock_period_ticks)
            }
        }
    } else if cache.ticks_to_ns > 0.0 {
        Some((time as f64 / cache.ticks_to_ns) as u64)
    } else {
        Some(time as u64)
    }
}

// -- Diff snapshot from cache ------------------------------------------------

pub fn diff_from_cache(cache: &ColumnCache, cfg: &ExtractConfig) {
    let (t1, t2) = match cfg.diff_points {
        Some(p) => p,
        None => return,
    };

    let matched = cache.match_signals(&cfg.patterns);
    if matched.is_empty() {
        exit_no_signal_match(&cfg.patterns, cache.unique_signal_count());
    }

    let sync_mode = !cfg.async_mode && cache.clock_period_ticks > 0;

    let reset_deassert_tick = if !cfg.with_reset && sync_mode {
        find_reset_deassert_tick(cache, cfg)
    } else {
        None
    };
    let effective_start = reset_deassert_tick.unwrap_or(0);

    let tick1 = match resolve_time_to_tick(cache, cfg, t1, effective_start) {
        Some(t) => t,
        None => {
            eprintln!("ERROR: --diff: time {} is beyond simulation range", t1);
            return;
        }
    };
    let tick2 = match resolve_time_to_tick(cache, cfg, t2, effective_start) {
        Some(t) => t,
        None => {
            eprintln!("ERROR: --diff: time {} is beyond simulation range", t2);
            return;
        }
    };

    let all_names: Vec<String> = matched
        .iter()
        .map(|&i| cache.signals[i].name.clone())
        .collect();
    let prefix = crate::signal::common_scope_prefix(&all_names);

    let stdout = io::stdout();
    let mut out = BufWriter::new(stdout.lock());
    let stderr = io::stderr();
    let mut err = BufWriter::new(stderr.lock());

    if !prefix.is_empty() {
        let _ = writeln!(err, "# scope: {}", &prefix[..prefix.len() - 1]);
    }

    let label = if sync_mode { "cycle" } else { "time" };
    let _ = writeln!(out, "# diff {} {} vs {}", label, t1, t2);

    let radix_map = build_radix_map(cache, &matched, cfg);
    cache.prefetch_window(&matched, tick1.max(tick2));
    let values: Vec<(String, String)> = matched
        .par_iter()
        .map(|&sig_idx| {
            let v1 = cache.value_at_tick_direct(sig_idx, tick1);
            let v2 = cache.value_at_tick_direct(sig_idx, tick2);
            (v1, v2)
        })
        .collect();

    let mut diff_count = 0usize;
    for (idx, &sig_idx) in matched.iter().enumerate() {
        let (ref v1, ref v2) = values[idx];
        if v1 != v2 {
            let sig = &cache.signals[sig_idx];
            let dname = if !prefix.is_empty() && sig.name.starts_with(&prefix) {
                &sig.name[prefix.len()..]
            } else {
                &sig.name
            };
            let dv1 = fmt_val(v1, sig_idx, sig.width, &radix_map);
            let dv2 = fmt_val(v2, sig_idx, sig.width, &radix_map);
            let _ = writeln!(out, "{:<40} @{}={:<12} @{}={}", dname, t1, dv1, t2, dv2);
            diff_count += 1;
        }
    }

    if diff_count == 0 {
        let _ = writeln!(out, "# no differences found");
    } else {
        let _ = writeln!(err, "# {} signal(s) differ", diff_count);
    }
    let _ = out.flush();
    let _ = err.flush();
}

// -- Distance measurement from cache ------------------------------------------

/// Collect event ticks for a (pattern, value) pair.
/// Returns sorted vec of ticks where the event occurs within the time window.
/// Outcome of an event-tick collection — lets `distance_from_cache` give
/// callers a precise diagnostic instead of the old "match-or-events" blob.
pub(crate) enum EventCollect {
    /// Pattern didn't match any signal in the cache.
    NoPatternMatch,
    /// Pattern matched signals but none satisfied the value/edge predicate.
    /// `multi_bit_edge` is true when an edge keyword was requested but every
    /// matching signal is wider than 1 bit (edges are only defined on 1-bit
    /// signals — multi-bit "rising" silently returned 0 events historically).
    NoEvents { multi_bit_edge: bool },
    /// Sorted list of tick positions where the predicate fired.
    Ticks(Vec<u64>),
}

fn collect_event_ticks(
    cache: &ColumnCache,
    pattern: &str,
    value_str: &str,
    effective_start: u64,
    sync_mode: bool,
    cfg: &ExtractConfig,
    cb: u64,
    virtuals: &[VirtualEntry],
) -> EventCollect {
    let matchers = match compile_patterns(&[pattern.to_string()]) {
        Ok(m) => m,
        Err(_) => return EventCollect::NoPatternMatch,
    };
    let target_indices: Vec<usize> = {
        let mut seen = std::collections::HashSet::new();
        cache
            .signals
            .iter()
            .enumerate()
            .filter(|(_, s)| match_signal(&s.name, &matchers))
            .filter(|(_, s)| seen.insert(s.group_id))
            .map(|(i, _)| i)
            .collect()
    };
    let virtual_matches: Vec<&VirtualEntry> = virtuals
        .iter()
        .filter(|ve| match_signal(&ve.name, &matchers))
        .collect();
    if target_indices.is_empty() && virtual_matches.is_empty() {
        return EventCollect::NoPatternMatch;
    }

    let edge_mode = is_edge_keyword(value_str).map(String::from);
    // Edge keywords are only meaningful for 1-bit signals — `check_edge`
    // compares prev/cur against "0"/"1". Track whether every matched
    // signal is wider so the error message can call this out.
    let edge_on_multibit_only = edge_mode.is_some()
        && virtual_matches.is_empty()
        && target_indices.iter().all(|&i| cache.signals[i].width > 1);
    let mut ticks = Vec::new();

    for &sig_idx in &target_indices {
        let transitions = cache.read_transitions(sig_idx);
        let mut prev_val = "x".to_string();

        for (tick, val) in &transitions {
            if *tick < effective_start {
                prev_val = val.clone();
                continue;
            }

            // Time range filtering
            if sync_mode && cache.clock_period_ticks > 0 && cb > 0 {
                let cycle = tick_to_cycle(*tick, cb, cache.clock_period_ticks);
                if (cycle as i64) < cfg.time_min {
                    prev_val = val.clone();
                    continue;
                }
                if let Some(max) = cfg.time_max {
                    if (cycle as i64) > max {
                        break;
                    }
                }
            } else if !sync_mode {
                if (*tick as i64) < cfg.time_min {
                    prev_val = val.clone();
                    continue;
                }
                if let Some(max) = cfg.time_max {
                    if (*tick as i64) > max {
                        break;
                    }
                }
            }

            let matched = if let Some(ref edge) = edge_mode {
                let m = check_edge(&prev_val, val, edge);
                prev_val = val.clone();
                m
            } else {
                prev_val = val.clone();
                values_match(val, value_str)
            };

            if matched {
                ticks.push(*tick);
            }
        }
    }
    for ve in virtual_matches {
        let mut prev_val = "x".to_string();

        for (tick, val) in &ve.transitions {
            if *tick < effective_start {
                prev_val = val.clone();
                continue;
            }

            // Time range filtering
            if sync_mode && cache.clock_period_ticks > 0 && cb > 0 {
                let cycle = tick_to_cycle(*tick, cb, cache.clock_period_ticks);
                if (cycle as i64) < cfg.time_min {
                    prev_val = val.clone();
                    continue;
                }
                if let Some(max) = cfg.time_max {
                    if (cycle as i64) > max {
                        break;
                    }
                }
            } else if !sync_mode {
                if (*tick as i64) < cfg.time_min {
                    prev_val = val.clone();
                    continue;
                }
                if let Some(max) = cfg.time_max {
                    if (*tick as i64) > max {
                        break;
                    }
                }
            }

            let matched = if let Some(ref edge) = edge_mode {
                let m = check_edge(&prev_val, val, edge);
                prev_val = val.clone();
                m
            } else {
                prev_val = val.clone();
                values_match(val, value_str)
            };

            if matched {
                ticks.push(*tick);
            }
        }
    }
    ticks.sort();
    ticks.dedup();
    if ticks.is_empty() {
        EventCollect::NoEvents {
            multi_bit_edge: edge_on_multibit_only,
        }
    } else {
        EventCollect::Ticks(ticks)
    }
}

pub fn distance_from_cache(cache: &ColumnCache, cfg: &ExtractConfig) {
    let (ref pat_a, ref val_a) = match cfg.distance_a {
        Some(ref a) => a,
        None => return,
    };

    let sync_mode = !cfg.async_mode && cache.clock_period_ticks > 0;

    let reset_deassert_tick = if !cfg.with_reset && sync_mode {
        find_reset_deassert_tick(cache, cfg)
    } else {
        None
    };
    let effective_start = reset_deassert_tick.unwrap_or(0);
    let cb = cycle_base(cache, effective_start);
    let virtuals = build_virtuals(cache, cfg);

    // Resolve event A; report `no signals match` vs `no events found` distinctly
    // so the caller can tell whether their pattern was the problem.
    let ticks_a = match collect_event_ticks(
        cache,
        pat_a,
        val_a,
        effective_start,
        sync_mode,
        cfg,
        cb,
        &virtuals,
    ) {
        EventCollect::Ticks(t) => t,
        EventCollect::NoPatternMatch => {
            exit_no_signal_match(std::slice::from_ref(pat_a), cache.unique_signal_count());
        }
        EventCollect::NoEvents { multi_bit_edge } => {
            if multi_bit_edge {
                eprintln!(
                    "# distance: signal(s) matched '{}' but edge keyword '{}' \
                     only fires on 1-bit signals; use a Verilog literal value \
                     ('hN / 'dN / 'bN) for multi-bit signals.",
                    pat_a, val_a
                );
            } else {
                eprintln!(
                    "# distance: pattern '{}' matched but value '{}' never occurred",
                    pat_a, val_a
                );
            }
            return;
        }
    };

    let stdout = io::stdout();
    let mut out = BufWriter::new(stdout.lock());
    let stderr = io::stderr();
    let mut err = BufWriter::new(stderr.lock());

    let tick_to_display = |tick: u64| -> String {
        if sync_mode && cache.clock_period_ticks > 0 && cb > 0 {
            format!("{}", tick_to_cycle(tick, cb, cache.clock_period_ticks))
        } else {
            format!("{}", tick)
        }
    };
    let unit = if sync_mode { "cycles" } else { "ticks" };

    if let Some(ref b) = cfg.distance_b {
        // Two-event mode: A→B latency
        let (ref pat_b, ref val_b) = b;
        let ticks_b = match collect_event_ticks(
            cache,
            pat_b,
            val_b,
            effective_start,
            sync_mode,
            cfg,
            cb,
            &virtuals,
        ) {
            EventCollect::Ticks(t) => t,
            EventCollect::NoPatternMatch => {
                exit_no_signal_match(std::slice::from_ref(pat_b), cache.unique_signal_count());
            }
            EventCollect::NoEvents { multi_bit_edge } => {
                if multi_bit_edge {
                    eprintln!(
                        "# distance: signal(s) matched '{}' but edge keyword '{}' \
                         only fires on 1-bit signals.",
                        pat_b, val_b
                    );
                } else {
                    eprintln!(
                        "# distance: pattern '{}' matched but value '{}' never occurred",
                        pat_b, val_b
                    );
                }
                return;
            }
        };

        let _ = writeln!(out, "# A: {} {} -> B: {} {}", pat_a, val_a, pat_b, val_b);

        let mut deltas: Vec<u64> = Vec::new();
        let mut b_idx = 0;
        for &ta in &ticks_a {
            // Binary search for first tb > ta
            while b_idx < ticks_b.len() && ticks_b[b_idx] <= ta {
                b_idx += 1;
            }
            if b_idx >= ticks_b.len() {
                break;
            }
            let tb = ticks_b[b_idx];

            let delta_ticks = tb - ta;
            let delta_display = if sync_mode && cache.clock_period_ticks > 0 {
                delta_ticks / cache.clock_period_ticks
            } else {
                delta_ticks
            };
            deltas.push(delta_display);

            if !cfg.stats_mode {
                let _ = writeln!(
                    out,
                    "@ {} -> @ {}  d={}",
                    tick_to_display(ta),
                    tick_to_display(tb),
                    delta_display
                );
            }
        }

        if deltas.is_empty() {
            let _ = writeln!(out, "# no pairs found");
        } else if cfg.stats_mode {
            let count = deltas.len();
            let min = *deltas.iter().min().unwrap();
            let max = *deltas.iter().max().unwrap();
            let avg = deltas.iter().sum::<u64>() as f64 / count as f64;
            let _ = writeln!(
                out,
                "count={}  min={}  max={}  avg={:.1}",
                count, min, max, avg
            );
        } else {
            let _ = writeln!(err, "# {} pairs, unit: {}", deltas.len(), unit);
        }
    } else {
        // Same-signal mode: gaps between consecutive events
        let _ = writeln!(out, "# same-signal: {} {}", pat_a, val_a);

        if ticks_a.len() < 2 {
            let _ = writeln!(
                out,
                "# no pairs found (need at least 2 events, found {})",
                ticks_a.len()
            );
        } else {
            let mut deltas: Vec<u64> = Vec::new();
            for w in ticks_a.windows(2) {
                let delta_ticks = w[1] - w[0];
                let delta_display = if sync_mode && cache.clock_period_ticks > 0 {
                    delta_ticks / cache.clock_period_ticks
                } else {
                    delta_ticks
                };
                deltas.push(delta_display);

                if !cfg.stats_mode {
                    let _ = writeln!(
                        out,
                        "@ {} -> @ {}  d={}",
                        tick_to_display(w[0]),
                        tick_to_display(w[1]),
                        delta_display
                    );
                }
            }

            if cfg.stats_mode {
                let count = deltas.len();
                let min = *deltas.iter().min().unwrap();
                let max = *deltas.iter().max().unwrap();
                let avg = deltas.iter().sum::<u64>() as f64 / count as f64;
                let _ = writeln!(
                    out,
                    "count={}  min={}  max={}  avg={:.1}",
                    count, min, max, avg
                );
            } else {
                let _ = writeln!(err, "# {} pairs, unit: {}", deltas.len(), unit);
            }
        }
    }

    let _ = out.flush();
    let _ = err.flush();
}

// -- Cycle trace (default mode) from cache --------------------------------

pub fn trace_from_cache(cache: &ColumnCache, cfg: &ExtractConfig) {
    let matched = cache.match_signals(&cfg.patterns);
    if matched.is_empty() {
        // Unlike wave/stats/find, `signal` renders no virtual rows, so a
        // resolving --virtual cannot rescue a -s total miss here: exit 0
        // would mean empty output — the silent failure this gate exists to
        // prevent. (Use wave/stats/find to inspect virtual signals.)
        exit_no_signal_match(&cfg.patterns, cache.unique_signal_count());
    }
    let radix_map = build_radix_map(cache, &matched, cfg);

    let sync_mode = !cfg.async_mode;

    let all_names: Vec<String> = matched
        .iter()
        .map(|&i| cache.signals[i].name.clone())
        .collect();
    let prefix = crate::signal::common_scope_prefix(&all_names);

    let stdout = io::stdout();
    let mut out = BufWriter::new(stdout.lock());
    let stderr = io::stderr();
    let mut err = BufWriter::new(stderr.lock());

    if !prefix.is_empty() {
        let _ = writeln!(err, "# scope: {}", &prefix[..prefix.len() - 1]);
    }

    let reset_deassert_tick = if !cfg.with_reset && sync_mode {
        find_reset_deassert_tick(cache, cfg)
    } else {
        None
    };
    let effective_start = reset_deassert_tick.unwrap_or(0);

    // Preload transitions. When the query has an upper time bound, both
    // walkers below only ever sample at or before its tick — so the preload
    // is windowed to that tick (one bulk FST pass instead of a full decode
    // per signal). Unbounded queries keep the full per-signal decode: their
    // output is the whole trace anyway.
    let preload_max_tick: Option<u64> = match cfg.time_max {
        Some(max) if max > 0 => {
            if sync_mode && cache.clock_period_ticks > 0 {
                let cb = cycle_base(cache, effective_start);
                Some(cb + (max as u64).saturating_sub(1) * cache.clock_period_ticks)
            } else if !sync_mode {
                Some(max as u64)
            } else {
                None
            }
        }
        _ => None,
    };
    let sig_data: Vec<Vec<(u64, String)>> = match preload_max_tick {
        Some(bound) => {
            cache.prefetch_window(&matched, bound);
            matched
                .iter()
                .map(|&i| cache.read_transitions_range(i, 0, bound).1)
                .collect()
        }
        None => matched.iter().map(|&i| cache.read_transitions(i)).collect(),
    };

    let mut line_count: usize = 0;

    if sync_mode && cache.clock_period_ticks > 0 {
        // Sync mode: walk cycle by cycle, emit changed signals
        let cb = cycle_base(cache, effective_start);
        let total_cycles = (cache.sim_end_tick.saturating_sub(cb)) / cache.clock_period_ticks + 1;
        let start_cycle = if cfg.time_min > 0 {
            cfg.time_min as u64
        } else {
            1
        };
        let end_cycle = match cfg.time_max {
            Some(max) if max > 0 => std::cmp::min(max as u64, total_cycles),
            _ => total_cycles,
        };

        if cache.clock_period_ticks > 0 {
            let period_ns = (cache.clock_period_ticks as f64 * cache.ticks_to_ns) as i64;
            let _ = writeln!(err, "# sync: period={}ns", period_ns);
        }

        // Pre-initialize prev_vals at the cycle before start to avoid
        // emitting everything as "changed" at the first cycle in range
        let mut prev_vals: Vec<Option<String>> = if start_cycle > 1 {
            let prev_tick = cb + (start_cycle - 2) * cache.clock_period_ticks;
            sig_data
                .iter()
                .map(|trans| Some(value_at_tick(trans, prev_tick)))
                .collect()
        } else {
            vec![None; matched.len()]
        };

        for cycle in start_cycle..=end_cycle {
            let tick = cb + (cycle - 1) * cache.clock_period_ticks;
            for (si, &sig_idx) in matched.iter().enumerate() {
                let val = value_at_tick(&sig_data[si], tick);
                let changed = match &prev_vals[si] {
                    Some(pv) => pv != &val,
                    None => true,
                };
                if changed {
                    let sig = &cache.signals[sig_idx];
                    let dname = if !prefix.is_empty() && sig.name.starts_with(&prefix) {
                        &sig.name[prefix.len()..]
                    } else {
                        &sig.name
                    };
                    let dval = fmt_val(&val, sig_idx, sig.width, &radix_map);
                    let _ = writeln!(out, "{} {} {}", cycle, dname, dval);
                    prev_vals[si] = Some(val);
                    line_count += 1;
                    if line_count >= cfg.max_lines {
                        let _ = writeln!(
                            err,
                            "# WARNING: limit ({}) reached, output truncated",
                            cfg.max_lines
                        );
                        let _ = out.flush();
                        let _ = err.flush();
                        return;
                    }
                }
            }
        }

        // `signal` prints transitions, so a window where nothing changes —
        // the common `-t N:N` "what is it holding right now?" query — used to
        // print nothing at all, which reads as "no such signal". Fall back to
        // the held value at the window start.
        if line_count == 0 && start_cycle <= end_cycle {
            let tick = cb + (start_cycle - 1) * cache.clock_period_ticks;
            let _ = writeln!(
                err,
                "# no transitions in cycles {}:{} — showing held values at cycle {}",
                start_cycle, end_cycle, start_cycle
            );
            for (si, &sig_idx) in matched.iter().enumerate() {
                let sig = &cache.signals[sig_idx];
                let dname = if !prefix.is_empty() && sig.name.starts_with(&prefix) {
                    &sig.name[prefix.len()..]
                } else {
                    &sig.name
                };
                let val = value_at_tick(&sig_data[si], tick);
                let dval = fmt_val(&val, sig_idx, sig.width, &radix_map);
                let _ = writeln!(out, "{} {} {}", start_cycle, dname, dval);
                line_count += 1;
                if line_count >= cfg.max_lines {
                    break;
                }
            }
        }
    } else {
        // Async mode: merge all transitions into time-ordered stream
        let mut events: Vec<(u64, usize)> = Vec::new();
        for (si, trans) in sig_data.iter().enumerate() {
            for (tick, _) in trans {
                if (*tick as i64) < cfg.time_min {
                    continue;
                }
                if let Some(max) = cfg.time_max {
                    if (*tick as i64) > max {
                        break;
                    }
                }
                events.push((*tick, si));
            }
        }
        events.sort_by_key(|&(t, si)| (t, si));

        for (tick, si) in &events {
            let sig_idx = matched[*si];
            let sig = &cache.signals[sig_idx];
            let dname = if !prefix.is_empty() && sig.name.starts_with(&prefix) {
                &sig.name[prefix.len()..]
            } else {
                &sig.name
            };
            let val = value_at_tick(&sig_data[*si], *tick);
            let dval = fmt_val(&val, sig_idx, sig.width, &radix_map);
            let _ = writeln!(out, "{} {} {}", tick, dname, dval);
            line_count += 1;
            if line_count >= cfg.max_lines {
                let _ = writeln!(
                    err,
                    "# WARNING: limit ({}) reached, output truncated",
                    cfg.max_lines
                );
                break;
            }
        }

        // Same held-value fallback as sync mode: no transition inside the
        // window is not the same as no signal.
        if line_count == 0 {
            let at_tick = cfg.time_min.max(0) as u64;
            let _ = writeln!(
                err,
                "# no transitions in range — showing held values at time {}",
                at_tick
            );
            for (si, &sig_idx) in matched.iter().enumerate() {
                let sig = &cache.signals[sig_idx];
                let dname = if !prefix.is_empty() && sig.name.starts_with(&prefix) {
                    &sig.name[prefix.len()..]
                } else {
                    &sig.name
                };
                let val = value_at_tick(&sig_data[si], at_tick);
                let dval = fmt_val(&val, sig_idx, sig.width, &radix_map);
                let _ = writeln!(out, "{} {} {}", at_tick, dname, dval);
                line_count += 1;
                if line_count >= cfg.max_lines {
                    break;
                }
            }
        }
    }

    let _ = out.flush();
    let _ = err.flush();
}

// -- List signals from cache ----------------------------------------------

pub fn list_signals_from_cache(
    cache: &ColumnCache,
    patterns: &[String],
    tree_only: bool,
    json_format: bool,
    limit: usize,
) {
    let matchers = match crate::signal::compile_patterns(patterns) {
        Ok(m) => m,
        Err(e) => {
            eprintln!("ERROR: {}", e);
            std::process::exit(2);
        }
    };

    let matched: Vec<(String, u32, String)> = cache
        .signals
        .iter()
        .filter(|s| crate::signal::match_signal(&s.name, &matchers))
        .map(|s| (s.name.clone(), s.width, s.var_type.clone()))
        .collect();

    let prefix = crate::signal::common_scope_prefix(
        &matched
            .iter()
            .map(|(n, _, _)| n.clone())
            .collect::<Vec<_>>(),
    );

    let stripped: Vec<(String, u32, String)> = if !prefix.is_empty() {
        matched
            .iter()
            .map(|(n, w, vt)| (n[prefix.len()..].to_string(), *w, vt.clone()))
            .collect()
    } else {
        matched.clone()
    };

    if json_format {
        // Scope prefix is published in `data` with the trailing '.' trimmed,
        // matching the text-mode "# scope: ..." label.
        let scope_prefix = if prefix.is_empty() {
            String::new()
        } else {
            prefix[..prefix.len() - 1].to_string()
        };
        let signals: Vec<crate::output::SignalEntry> = stripped
            .iter()
            .map(|(n, w, vt)| crate::output::SignalEntry {
                name: n.clone(),
                width: *w,
                var_type: vt.clone(),
            })
            .collect();
        let clock = if cache.clock_id.is_empty() {
            None
        } else {
            Some(cache.clock_id.clone())
        };
        let total_ticks = cache.sim_end_tick.saturating_sub(cache.sim_start_tick);
        // An empty *store* (not an empty match) rides along as a warning so
        // JSON consumers can tell "bad pattern" from "bad trace". It ALSO
        // goes to stderr like the text path does — stderr is free in JSON
        // mode, and a consumer that only scans stderr for ERROR: must not
        // see an empty store as a clean run.
        let warnings = if cache.signals.is_empty() {
            eprintln!("ERROR: {}", no_signals_in_store_message());
            vec![no_signals_in_store_message().to_string()]
        } else {
            Vec::new()
        };
        crate::output::emit_json(
            "list",
            crate::output::ListData {
                scope_prefix,
                clock,
                total_ticks,
                signals,
            },
            warnings,
        );
        return;
    }

    let mut stderr = BufWriter::new(io::stderr().lock());
    if !prefix.is_empty() {
        let _ = writeln!(stderr, "# scope: {}", &prefix[..prefix.len() - 1]);
    }

    // Honor --limit (it used to be accepted and ignored here). An unbounded
    // list of a big scope overruns the caller's output window, which then
    // keeps the *tail* — dropping the top of the tree along with the scope
    // header. Truncating here keeps the head and says so.
    let shown = if tree_only {
        stripped.as_slice()
    } else {
        &stripped[..stripped.len().min(limit)]
    };

    let mut stdout = BufWriter::new(io::stdout().lock());
    if tree_only {
        let _ = crate::format::print_scope_tree(shown, &mut stdout);
    } else {
        let _ = crate::format::print_signal_tree(shown, &mut stdout);
    }
    let _ = stdout.flush();

    if cache.signals.is_empty() {
        // Not a narrow pattern — the store itself is empty. Loud (ERROR:) but
        // still exit 0: `list` is the discovery tool one runs to see what is
        // wrong, so it must be allowed to answer.
        let _ = writeln!(stderr, "ERROR: {}", no_signals_in_store_message());
    } else if shown.len() < stripped.len() {
        let _ = writeln!(
            stderr,
            "# WARNING: limit ({}) reached — showing {} of {} signals; \
             narrow with -s PATTERN, use --tree, or raise --limit",
            limit,
            shown.len(),
            matched.len()
        );
    } else {
        let _ = writeln!(
            stderr,
            "# {} signals — narrow with -s PATTERN or use --tree",
            matched.len()
        );
    }
    let _ = stderr.flush();
}

// -- Helper functions -----------------------------------------------------

fn check_edge(prev: &str, cur: &str, edge_type: &str) -> bool {
    match edge_type {
        "rising" => prev == "0" && cur == "1",
        "falling" => prev == "1" && cur == "0",
        _ => false,
    }
}

fn tick_to_cycle(tick: u64, cycle_base_tick: u64, clock_period: u64) -> u64 {
    if clock_period == 0 || tick < cycle_base_tick {
        return 0;
    }
    (tick - cycle_base_tick) / clock_period + 1
}

/// Compute the tick of the first rising edge at or after effective_start.
/// Matches Extractor's cycle counting which starts at the first on_rising_edge
/// after reset deassert.
fn cycle_base(cache: &ColumnCache, effective_start: u64) -> u64 {
    if cache.clock_period_ticks == 0 {
        return effective_start;
    }
    let frt = cache.first_rise_tick;
    if effective_start <= frt {
        return frt;
    }
    // Snap to clock grid: first rising edge tick >= effective_start
    let elapsed = effective_start - frt;
    let full_periods = elapsed / cache.clock_period_ticks;
    let aligned = frt + full_periods * cache.clock_period_ticks;
    if aligned >= effective_start {
        aligned
    } else {
        aligned + cache.clock_period_ticks
    }
}

/// Binary search for the value of a signal at a given tick.
/// Returns the most recent value at or before tick.
fn value_at_tick(transitions: &[(u64, String)], tick: u64) -> String {
    if transitions.is_empty() {
        return "x".to_string();
    }
    match transitions.binary_search_by_key(&tick, |(t, _)| *t) {
        Ok(i) => transitions[i].1.clone(),
        Err(0) => "x".to_string(),
        Err(i) => transitions[i - 1].1.clone(),
    }
}

/// Find the best reset signal index, matching Extractor's depth-based selection.
/// With explicit pattern: glob match. Without: contains("rst") on leaf name.
/// Among matches, prefer shallowest scope depth, then lexicographic.
fn find_best_reset_idx(cache: &ColumnCache, cfg: &ExtractConfig) -> Option<usize> {
    let explicit_matchers = match cfg.reset_pattern.as_ref() {
        Some(pat) => Some(compile_patterns(&[pat.clone()]).ok()?),
        None => None,
    };

    let mut candidates: Vec<(usize, &str)> = Vec::new();
    for (i, s) in cache.signals.iter().enumerate() {
        if s.width != 1 {
            continue;
        }
        if let Some(ref matchers) = explicit_matchers {
            if match_signal(&s.name, matchers) {
                candidates.push((i, &s.name));
            }
        } else {
            let stripped = s.name.split('[').next().unwrap_or(&s.name);
            if stripped.to_lowercase().contains("rst") {
                candidates.push((i, &s.name));
            }
        }
    }

    candidates.sort_by(|a, b| {
        let da = a.1.matches('.').count();
        let db = b.1.matches('.').count();
        da.cmp(&db).then(a.1.cmp(b.1))
    });

    candidates.first().map(|&(i, _)| i)
}

/// Find the tick where reset deasserts. Returns None if no reset or reset never deasserts.
fn find_reset_deassert_tick(cache: &ColumnCache, cfg: &ExtractConfig) -> Option<u64> {
    let idx = find_best_reset_idx(cache, cfg)?;
    let sig = &cache.signals[idx];
    let transitions = cache.read_transitions(idx);

    // Determine polarity from name
    let leaf = sig
        .name
        .split('[')
        .next()
        .unwrap_or(&sig.name)
        .split('.')
        .last()
        .unwrap_or(&sig.name)
        .to_lowercase();
    let active_low = leaf.ends_with('n') || leaf.contains("_n");

    // Find deassert
    for (tick, val) in &transitions {
        let is_asserted = if active_low { val == "0" } else { val == "1" };
        if !is_asserted {
            return Some(*tick);
        }
    }
    None
}

// -- Wave rendering helper ----------------------------------------------------

/// Widest value cell `wave` renders before eliding. 24 chars keeps every
/// signal up to 96 bits intact; beyond that one 1920-bit bus (480 hex chars)
/// sets the column width for the whole table and the output degenerates into
/// a wall of padding that blows the caller's output budget.
const WAVE_CELL_MAX: usize = 24;
/// Chars kept from each end of an elided cell (`WAVE_CELL_MAX` minus the
/// two-char ".." marker, split evenly).
const WAVE_CELL_KEEP: usize = (WAVE_CELL_MAX - 2) / 2;

/// Elide over-wide value cells in place: `0011..99AA`. The RLE `×N` suffix,
/// when present, is preserved — it carries the run length, not the value.
/// ASCII ".." (not "…") so byte length still equals column width.
fn elide_wide_cells(
    display_names: &[String],
    grid: &mut [Vec<String>],
    err: &mut BufWriter<io::StderrLock>,
) {
    let mut elided: Vec<&str> = Vec::new();
    for (ri, row) in grid.iter_mut().enumerate() {
        let mut row_elided = false;
        for cell in row.iter_mut() {
            let (value, suffix) = match cell.find('×') {
                Some(p) => (cell[..p].to_string(), cell[p..].to_string()),
                None => (cell.clone(), String::new()),
            };
            if value.chars().count() <= WAVE_CELL_MAX {
                continue;
            }
            let chars: Vec<char> = value.chars().collect();
            let head: String = chars[..WAVE_CELL_KEEP].iter().collect();
            let tail: String = chars[chars.len() - WAVE_CELL_KEEP..].iter().collect();
            *cell = format!("{}..{}{}", head, tail, suffix);
            row_elided = true;
        }
        if row_elided {
            if let Some(name) = display_names.get(ri) {
                elided.push(name);
            }
        }
    }
    if !elided.is_empty() {
        let _ = writeln!(
            err,
            "# NOTE: values elided to {} chars ({}) — use `value --at CYCLE` \
             or `signal` for full width",
            WAVE_CELL_MAX,
            elided.join(", ")
        );
    }
}

fn render_wave_grid(
    display_names: &[String],
    col_headers: &[String],
    col_ticks: &[u64],
    grid: &[Vec<String>],
    sync_mode: bool,
    markers: &[(String, i64)],
    rle: bool,
    err: &mut BufWriter<io::StderrLock>,
) {
    let label = if sync_mode { "cycle" } else { "time" };
    let name_width = display_names
        .iter()
        .map(|n| n.len())
        .max()
        .unwrap_or(0)
        .max(label.len());

    // Run-length encoding: collapse contiguous columns where every signal in
    // the grid holds the same value as the previous column. The header for a
    // collapsed run shows the first cycle; the value cell shows `val×N`. A
    // run of length 1 is rendered as the plain value (no `×1` noise). Marker
    // columns are never collapsed away — they always start a new run so the
    // label remains aligned with the cycle it points to.
    let (col_headers, grid, col_ticks) = if rle && !col_headers.is_empty() {
        let n_cols = col_headers.len();
        let mut keep: Vec<bool> = vec![true; n_cols];
        let marker_cycles: std::collections::HashSet<i64> =
            markers.iter().map(|(_, c)| *c).collect();
        for j in 1..n_cols {
            let same = grid.iter().all(|row| row[j] == row[j - 1]);
            let is_marker_col = col_headers[j]
                .parse::<i64>()
                .ok()
                .map(|c| marker_cycles.contains(&c))
                .unwrap_or(false);
            if same && !is_marker_col {
                keep[j] = false;
            }
        }
        // Build runs: each kept column anchors a run that extends until the
        // next kept column.
        let kept_indices: Vec<usize> = (0..n_cols).filter(|j| keep[*j]).collect();
        let mut new_headers: Vec<String> = Vec::with_capacity(kept_indices.len());
        let mut new_ticks: Vec<u64> = Vec::with_capacity(kept_indices.len());
        let mut runs: Vec<usize> = Vec::with_capacity(kept_indices.len());
        for (i, &start) in kept_indices.iter().enumerate() {
            let end = kept_indices.get(i + 1).copied().unwrap_or(n_cols);
            new_headers.push(col_headers[start].clone());
            new_ticks.push(col_ticks[start]);
            runs.push(end - start);
        }
        let new_grid: Vec<Vec<String>> = grid
            .iter()
            .map(|row| {
                kept_indices
                    .iter()
                    .enumerate()
                    .map(|(i, &j)| {
                        if runs[i] > 1 {
                            format!("{}×{}", row[j], runs[i])
                        } else {
                            row[j].clone()
                        }
                    })
                    .collect()
            })
            .collect();
        (new_headers, new_grid, new_ticks)
    } else {
        (col_headers.to_vec(), grid.to_vec(), col_ticks.to_vec())
    };
    let mut grid = grid;
    elide_wide_cells(display_names, &mut grid, err);
    let col_headers = col_headers.as_slice();
    let grid = grid.as_slice();
    let col_ticks = col_ticks.as_slice();

    let mut col_widths: Vec<usize> = col_headers.iter().map(|h| h.len()).collect();
    for row in grid {
        for (j, val) in row.iter().enumerate() {
            col_widths[j] = col_widths[j].max(val.len());
        }
    }

    // Build marker labels per column
    let marker_labels: Vec<String> = if !markers.is_empty() {
        col_headers
            .iter()
            .map(|hdr| {
                if let Ok(col_val) = hdr.parse::<i64>() {
                    markers
                        .iter()
                        .find(|(_, cycle)| *cycle == col_val)
                        .map(|(name, _)| name.clone())
                        .unwrap_or_default()
                } else {
                    String::new()
                }
            })
            .collect()
    } else {
        Vec::new()
    };

    // Update col_widths to account for marker labels
    let has_markers = marker_labels.iter().any(|l| !l.is_empty());
    if has_markers {
        for (j, lbl) in marker_labels.iter().enumerate() {
            col_widths[j] = col_widths[j].max(lbl.len());
        }
    }

    let stdout = io::stdout();
    let mut out = BufWriter::new(stdout.lock());

    // Marker row (above cycle header)
    if has_markers {
        let _ = write!(out, "{:>width$}", "", width = name_width);
        for (j, lbl) in marker_labels.iter().enumerate() {
            let _ = write!(out, "  {:>width$}", lbl, width = col_widths[j]);
        }
        let _ = writeln!(out);
    }

    let _ = write!(out, "{:>width$}", label, width = name_width);
    for (j, hdr) in col_headers.iter().enumerate() {
        let _ = write!(out, "  {:>width$}", hdr, width = col_widths[j]);
    }
    let _ = writeln!(out);

    for (i, name) in display_names.iter().enumerate() {
        let _ = write!(out, "{:>width$}", name, width = name_width);
        for (j, val) in grid[i].iter().enumerate() {
            let _ = write!(out, "  {:>width$}", val, width = col_widths[j]);
        }
        let _ = writeln!(out);
    }

    let _ = writeln!(
        err,
        "# {} signals, {} columns",
        display_names.len(),
        col_ticks.len()
    );
    let _ = out.flush();
    let _ = err.flush();
}

// -- Wave (horizontal waveform) from cache -----------------------------------

pub fn wave_from_cache(cache: &ColumnCache, cfg: &ExtractConfig) {
    if !cfg.wave_mode {
        return;
    }
    let range_start = cfg.time_min;
    let range_end_opt = cfg.time_max;

    let matched = cache.match_signals(&cfg.patterns);
    // Virtuals count as matches (wave renders them as rows), same as
    // stats/find: `-s missing --virtual ok=...` is a partial miss, not a
    // total one. Built once here; the row-append below reuses it.
    let virtuals = build_virtuals(cache, cfg);
    if matched.is_empty() && virtuals.is_empty() {
        exit_no_signal_match(&cfg.patterns, cache.unique_signal_count());
    }
    let radix_map = build_radix_map(cache, &matched, cfg);

    let sync_mode = !cfg.async_mode && cache.clock_period_ticks > 0;

    let all_names: Vec<String> = matched
        .iter()
        .map(|&i| cache.signals[i].name.clone())
        .collect();
    let prefix = crate::signal::common_scope_prefix(&all_names);

    let stderr = io::stderr();
    let mut err = BufWriter::new(stderr.lock());

    if !prefix.is_empty() {
        let _ = writeln!(err, "# scope: {}", &prefix[..prefix.len() - 1]);
    }

    if cfg.async_mode && cache.clock_period_ticks == 0 {
        // async is expected, no warning
    } else if !cfg.async_mode && cache.clock_period_ticks == 0 {
        let _ = writeln!(
            err,
            "# WARNING: no clock signal found, falling back to async mode"
        );
    }

    let reset_deassert_tick = if !cfg.with_reset && sync_mode {
        find_reset_deassert_tick(cache, cfg)
    } else {
        None
    };
    let effective_start = reset_deassert_tick.unwrap_or(0);

    // Build column headers and target ticks (BEFORE reading transitions for range decode)
    if sync_mode {
        // Sync: compute tick range, then range-decode only what's needed
        let cb = cycle_base(cache, effective_start);
        let total_cycles = if cache.clock_period_ticks > 0 {
            (cache.sim_end_tick.saturating_sub(cb)) / cache.clock_period_ticks + 1
        } else {
            1
        };

        let cycle_start = range_start.max(0) as u64;
        let cycle_end = match range_end_opt {
            Some(e) => (e as u64).min(total_cycles),
            None => total_cycles,
        };

        if cycle_start >= cycle_end && range_start != range_end_opt.unwrap_or(-1) as i64 {
            // Range but no valid cycles
        }

        let mut col_headers = Vec::new();
        let mut col_ticks = Vec::new();
        for c in cycle_start..=cycle_end.min(cycle_start + cfg.max_lines as u64 - 1) {
            col_headers.push(c.to_string());
            if c == 0 {
                col_ticks.push(effective_start);
            } else {
                col_ticks.push(cb + (c - 1) * cache.clock_period_ticks);
            }
        }
        if cycle_end > cycle_start + cfg.max_lines as u64 - 1 {
            let _ = writeln!(
                err,
                "# WARNING: limit ({}) reached, output truncated",
                cfg.max_lines
            );
        }

        if col_ticks.is_empty() {
            let _ = writeln!(err, "# No data in range");
            let _ = err.flush();
            return;
        }

        let tick_min = col_ticks[0];
        let tick_max = *col_ticks.last().unwrap();

        // Range-bounded read: only decode transitions in [tick_min, tick_max]
        cache.prefetch_window(&matched, tick_max);
        let range_results: Vec<(Option<String>, Vec<(u64, String)>)> = matched
            .par_iter()
            .map(|&i| cache.read_transitions_range(i, tick_min, tick_max))
            .collect();

        // Build grid using range results
        let mut display_names: Vec<String> = matched
            .iter()
            .map(|&i| {
                let name = &cache.signals[i].name;
                if !prefix.is_empty() && name.starts_with(&prefix) {
                    name[prefix.len()..].to_string()
                } else {
                    name.clone()
                }
            })
            .collect();

        let mut grid: Vec<Vec<String>> = Vec::with_capacity(matched.len());
        for (ri, (before_val, transitions)) in range_results.iter().enumerate() {
            let sig_idx = matched[ri];
            let width = cache.signals[sig_idx].width;
            let row: Vec<String> = col_ticks
                .iter()
                .map(|&t| {
                    let v = match transitions.binary_search_by_key(&t, |(tick, _)| *tick) {
                        Ok(i) => transitions[i].1.clone(),
                        Err(0) => before_val.clone().unwrap_or_else(|| "x".to_string()),
                        Err(i) => transitions[i - 1].1.clone(),
                    };
                    fmt_val(&v, sig_idx, width, &radix_map)
                })
                .collect();
            grid.push(row);
        }

        // Append virtual signal rows (built above, before the empty-match gate)
        for ve in &virtuals {
            display_names.push(ve.name.clone());
            let row: Vec<String> = col_ticks
                .iter()
                .map(
                    |&t| match ve.transitions.binary_search_by_key(&t, |(tick, _)| *tick) {
                        Ok(i) => ve.transitions[i].1.clone(),
                        Err(0) => "0".to_string(),
                        Err(i) => ve.transitions[i - 1].1.clone(),
                    },
                )
                .collect();
            grid.push(row);
        }

        // Hint when the wave is long and the user didn't ask for compression
        // — agents and humans both get blasted by 1000-column rows otherwise.
        if !cfg.wave_rle && col_headers.len() > 200 {
            let _ = writeln!(
                err,
                "# hint: {} columns — pass --rle to collapse identical runs",
                col_headers.len()
            );
        }
        render_wave_grid(
            &display_names,
            &col_headers,
            &col_ticks,
            &grid,
            sync_mode,
            &cfg.markers,
            cfg.wave_rle,
            &mut err,
        );
    } else {
        // Async: need range-bounded decode, then collect tick set from results
        let range_start_tick = if cache.ticks_to_ns > 0.0 {
            (range_start as f64 / cache.ticks_to_ns) as u64
        } else {
            range_start as u64
        };
        let range_end_tick = match range_end_opt {
            Some(e) => {
                if cache.ticks_to_ns > 0.0 {
                    (e as f64 / cache.ticks_to_ns) as u64
                } else {
                    e as u64
                }
            }
            None => cache.sim_end_tick,
        };

        cache.prefetch_window(&matched, range_end_tick);
        let range_results: Vec<(Option<String>, Vec<(u64, String)>)> = matched
            .par_iter()
            .map(|&i| cache.read_transitions_range(i, range_start_tick, range_end_tick))
            .collect();

        // Build tick set from range results
        let mut tick_set = std::collections::BTreeSet::new();
        for (_, transitions) in &range_results {
            for (tick, _) in transitions {
                tick_set.insert(*tick);
            }
        }
        // Virtual signals contribute columns too — without this, a
        // virtual-only match (every -s pattern missed but a --virtual def
        // resolved) rendered zero columns: "# No data in range" despite
        // having rows to show.
        for ve in &virtuals {
            for (tick, _) in &ve.transitions {
                if *tick >= range_start_tick && *tick <= range_end_tick {
                    tick_set.insert(*tick);
                }
            }
        }

        let mut col_headers = Vec::new();
        let mut col_ticks = Vec::new();
        for (i, &t) in tick_set.iter().enumerate() {
            if i >= cfg.max_lines {
                let _ = writeln!(
                    err,
                    "# WARNING: limit ({}) reached, output truncated",
                    cfg.max_lines
                );
                break;
            }
            let ns = (t as f64 * cache.ticks_to_ns) as u64;
            col_headers.push(ns.to_string());
            col_ticks.push(t);
        }

        if col_ticks.is_empty() {
            let _ = writeln!(err, "# No data in range");
            let _ = err.flush();
            return;
        }

        let mut display_names: Vec<String> = matched
            .iter()
            .map(|&i| {
                let name = &cache.signals[i].name;
                if !prefix.is_empty() && name.starts_with(&prefix) {
                    name[prefix.len()..].to_string()
                } else {
                    name.clone()
                }
            })
            .collect();

        let mut grid: Vec<Vec<String>> = Vec::with_capacity(matched.len());
        for (ri, (before_val, transitions)) in range_results.iter().enumerate() {
            let sig_idx = matched[ri];
            let width = cache.signals[sig_idx].width;
            let row: Vec<String> = col_ticks
                .iter()
                .map(|&t| {
                    let v = match transitions.binary_search_by_key(&t, |(tick, _)| *tick) {
                        Ok(i) => transitions[i].1.clone(),
                        Err(0) => before_val.clone().unwrap_or_else(|| "x".to_string()),
                        Err(i) => transitions[i - 1].1.clone(),
                    };
                    fmt_val(&v, sig_idx, width, &radix_map)
                })
                .collect();
            grid.push(row);
        }

        // Append virtual signal rows (built above, before the empty-match gate)
        for ve in &virtuals {
            display_names.push(ve.name.clone());
            let row: Vec<String> = col_ticks
                .iter()
                .map(
                    |&t| match ve.transitions.binary_search_by_key(&t, |(tick, _)| *tick) {
                        Ok(i) => ve.transitions[i].1.clone(),
                        Err(0) => "0".to_string(),
                        Err(i) => ve.transitions[i - 1].1.clone(),
                    },
                )
                .collect();
            grid.push(row);
        }

        // Hint when the wave is long and the user didn't ask for compression
        // — agents and humans both get blasted by 1000-column rows otherwise.
        if !cfg.wave_rle && col_headers.len() > 200 {
            let _ = writeln!(
                err,
                "# hint: {} columns — pass --rle to collapse identical runs",
                col_headers.len()
            );
        }
        render_wave_grid(
            &display_names,
            &col_headers,
            &col_ticks,
            &grid,
            sync_mode,
            &cfg.markers,
            cfg.wave_rle,
            &mut err,
        );
    }
}

// -- Tests ----------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::BufReader;
    use std::sync::atomic::AtomicUsize;

    static TEST_COUNTER: AtomicUsize = AtomicUsize::new(0);

    /// Build an FST store from VCD text via the streaming build handler and
    /// load it back through the query-facing `ColumnCache` interface.
    fn build_cache(vcd_text: &str) -> ColumnCache {
        let n = TEST_COUNTER.fetch_add(1, Ordering::Relaxed);
        let path =
            std::env::temp_dir().join(format!("bwave_cache_test_{}_{}.fst", std::process::id(), n));
        let mut reader = BufReader::new(vcd_text.as_bytes());
        let header = crate::parser::parse_header(&mut reader);
        let mut h =
            crate::fst::FstBuildHandler::new(&header, None, &path).expect("open fst for writing");
        h.parse_bytes(&mut reader, None).unwrap();
        h.finalize_and_write().unwrap();
        let cache = ColumnCache::load_from_file(&path).expect("load fst");
        let _ = std::fs::remove_file(&path);
        cache
    }

    const BASIC_VCD: &str = "\
$timescale 1ns $end
$scope module tb $end
$var wire 1 ! clk $end
$var wire 1 \" rstn $end
$var wire 8 # data [7:0] $end
$upscope $end
$enddefinitions $end
#0
0!
0\"
b00000000 #
#5
1!
#10
0!
#15
1!
1\"
#20
0!
#25
1!
b00001010 #
#30
0!
";

    #[test]
    fn test_load_directory_and_clock() {
        let cache = build_cache(BASIC_VCD);
        assert_eq!(cache.signals.len(), 3);
        assert_eq!(cache.signals[0].name, "tb.clk");
        assert_eq!(cache.signals[1].name, "tb.rstn");
        assert_eq!(cache.signals[2].name, "tb.data[7:0]");
        assert_eq!(cache.signals[2].width, 8);
        // clock re-derived from content: rises at 5 and 15 -> period 10
        assert_eq!(cache.clock_period_ticks, 10);
        assert_eq!(cache.first_rise_tick, 5);
        assert_eq!(cache.sim_end_tick, 30);
        assert_eq!(cache.timescale_str, "1ns");
    }

    #[test]
    fn test_read_transitions_canonical_values() {
        let cache = build_cache(BASIC_VCD);
        let data = cache.read_transitions(2);
        // pure-binary multi-bit values report as stripped uppercase hex
        assert_eq!(data, vec![(0, "0".to_string()), (25, "A".to_string())]);
        let clk = cache.read_transitions(0);
        assert_eq!(clk.len(), 7);
        assert_eq!(clk[0], (0, "0".to_string()));
        assert_eq!(clk[1], (5, "1".to_string()));
    }

    #[test]
    fn test_read_transitions_range_window() {
        let cache = build_cache(BASIC_VCD);
        let (before, in_range) = cache.read_transitions_range(0, 10, 20);
        assert_eq!(before.as_deref(), Some("1")); // value entering the window
        assert_eq!(
            in_range,
            vec![
                (10, "0".to_string()),
                (15, "1".to_string()),
                (20, "0".to_string())
            ]
        );
    }

    #[test]
    fn test_value_at_tick_direct() {
        let cache = build_cache(BASIC_VCD);
        assert_eq!(cache.value_at_tick_direct(2, 0), "0");
        assert_eq!(cache.value_at_tick_direct(2, 24), "0");
        assert_eq!(cache.value_at_tick_direct(2, 25), "A");
        assert_eq!(cache.value_at_tick_direct(2, 30), "A");
    }

    #[test]
    fn test_no_transitions_signal() {
        let vcd = "\
$timescale 1ns $end
$scope module tb $end
$var wire 4 ! quiet [3:0] $end
$upscope $end
$enddefinitions $end
#0
#10
";
        let cache = build_cache(vcd);
        assert_eq!(cache.signals.len(), 1);
        // never dumped: the FST frame materializes x at t=0
        let t = cache.read_transitions(0);
        assert!(t.is_empty() || t == vec![(0, "x".to_string())]);
    }

    #[test]
    fn test_alias_dedup_in_match_signals() {
        let vcd = "\
$timescale 1ns $end
$scope module tb $end
$var wire 1 ! sig_a $end
$var wire 1 ! sig_b $end
$var wire 1 \" other $end
$upscope $end
$enddefinitions $end
#0
0!
0\"
#5
1!
";
        let cache = build_cache(vcd);
        assert_eq!(cache.signals.len(), 3);
        // aliases share a group id; distinct signals do not
        assert_eq!(cache.signals[0].group_id, cache.signals[1].group_id);
        assert_ne!(cache.signals[0].group_id, cache.signals[2].group_id);
        // '*' keeps the LAST alias of each group plus the distinct signal
        let matched = cache.match_signals(&["*".to_string()]);
        assert_eq!(matched.len(), 2);
        assert_eq!(cache.signals[matched[0]].name, "tb.sig_b");
        assert_eq!(cache.signals[matched[1]].name, "tb.other");
        // both aliases read the same stream
        assert_eq!(cache.read_transitions(0), cache.read_transitions(1));
    }

    #[test]
    fn test_detect_clock_from_pattern() {
        let cache = build_cache(BASIC_VCD);
        let (period, first_rise, name) = cache.detect_clock_from_pattern("*clk*").expect("detect");
        assert_eq!(period, 10);
        assert_eq!(first_rise, 5);
        assert_eq!(name, "tb.clk");
        assert!(cache.detect_clock_from_pattern("*nope*").is_err());
        // multi-bit signals are not clock candidates
        assert!(cache.detect_clock_from_pattern("*data*").is_err());
        // a stuck signal has < 2 rising edges
        assert!(cache.detect_clock_from_pattern("*rstn*").is_err());
    }

    #[test]
    fn test_override_clock_workflow() {
        let vcd = "\
$timescale 1ns $end
$scope module tb $end
$var wire 1 ! clk $end
$var wire 1 \" clk2 $end
$upscope $end
$enddefinitions $end
#0
0!
0\"
#5
1!
#10
0!
1\"
#15
1!
#20
0!
0\"
#25
1!
#30
0!
1\"
";
        let mut cache = build_cache(vcd);
        // primary clock: clk (period 10); clk2 has period 20
        assert_eq!(cache.clock_period_ticks, 10);
        let (p2, f2, n2) = cache.detect_clock_from_pattern("*clk2*").expect("clk2");
        assert_eq!((p2, f2), (20, 10));
        cache.override_clock(p2, f2, &n2);
        assert_eq!(cache.clock_period_ticks, 20);
        assert_eq!(cache.first_rise_tick, 10);
        assert_eq!(cache.clock_id, "tb.clk2");
        // transition data is unaffected by a display-clock override
        assert_eq!(cache.read_transitions(0).len(), 7);
    }

    #[test]
    fn test_tick_to_cycle() {
        assert_eq!(tick_to_cycle(0, 5, 10), 0); // before the cycle base
        assert_eq!(tick_to_cycle(5, 5, 10), 1);
        assert_eq!(tick_to_cycle(14, 5, 10), 1);
        assert_eq!(tick_to_cycle(15, 5, 10), 2);
        assert_eq!(tick_to_cycle(25, 5, 10), 3);
    }

    #[test]
    fn test_value_at_tick_helper() {
        let transitions = vec![
            (0u64, "0".to_string()),
            (10, "1".to_string()),
            (20, "A".to_string()),
        ];
        assert_eq!(value_at_tick(&transitions, 0), "0");
        assert_eq!(value_at_tick(&transitions, 9), "0");
        assert_eq!(value_at_tick(&transitions, 10), "1");
        assert_eq!(value_at_tick(&transitions, 100), "A");
        assert_eq!(value_at_tick(&[], 5), "x");
    }

    #[test]
    fn test_minimal_xz() {
        assert_eq!(minimal_xz("xxxx01".to_string()), "x01");
        assert_eq!(minimal_xz("0001z".to_string()), "1z");
        assert_eq!(minimal_xz("0z1".to_string()), "0z1");
        assert_eq!(minimal_xz("zzz".to_string()), "z");
        assert_eq!(minimal_xz("0".to_string()), "0");
        assert_eq!(minimal_xz("0101".to_string()), "0101");
    }

    // -- Process-boundary contract pins ----------------------------------
    //
    // These substrings are matched by Python consumers across the process
    // boundary (src/booley/bwave/contract.py). Rewording the
    // diagnostics is fine ONLY if the marker substring survives — otherwise
    // e.g. coverage_analyst's discovery fallback turns into a hard error.
    // Change these together with _bwave_contract.py and its tests.

    #[test]
    fn no_match_message_carries_the_python_marker() {
        let msg = no_match_message(&["nope".to_string()], 7);
        assert!(
            msg.to_lowercase().contains("no signals match"),
            "marker NO_MATCH_MARKER lost from: {msg}"
        );
    }

    #[test]
    fn empty_store_message_carries_the_python_marker() {
        let msg = no_signals_in_store_message();
        assert!(
            msg.to_lowercase().contains("has no signals"),
            "marker NO_SIGNALS_IN_STORE_MARKER lost from: {msg}"
        );
    }
}
