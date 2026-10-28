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

use serde::Serialize;

use crate::format::{format_value_with_radix, values_match, Radix, TriggerMode};
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
    /// Authoritative decimal seconds exponent for exact input conversion.
    pub timescale_exponent: Option<i8>,
    pub clock_period_ticks: u64,
    pub first_rise_tick: u64,
    pub timescale_str: String,
    pub clock_id: String,
    pub clock_before_reset_at_deassert: bool,
    pub clock_table: Vec<ClockEntry>,
    pub signals: Vec<CachedSignal>,
    fst: crate::fst::FstBacking,
    clock_analysis: std::sync::OnceLock<crate::clock::ClockAnalysis>,
}

struct SignalSelection {
    stored: Vec<usize>,
    extra: Vec<usize>,
    warnings: Vec<String>,
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
        timescale_exponent: Option<i8>,
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
            timescale_exponent,
            clock_period_ticks,
            first_rise_tick,
            timescale_str,
            clock_id,
            clock_before_reset_at_deassert,
            clock_table,
            signals,
            fst: backing,
            clock_analysis: std::sync::OnceLock::new(),
        }
    }

    /// Override the display clock with a different signal's timing.
    pub fn override_clock(&mut self, period: u64, first_rise: u64, clock_name: &str) {
        self.clock_analysis = std::sync::OnceLock::new();
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

        let rising_ticks = crate::clock::rising_ticks(&transitions);
        if rising_ticks.len() < 2 {
            return Ok((
                0,
                rising_ticks.first().copied().unwrap_or(0),
                clock_name.to_string(),
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

        Ok((period, first_rise, clock_name.to_string()))
    }

    pub fn clock_analysis(&self) -> &crate::clock::ClockAnalysis {
        self.clock_analysis.get_or_init(|| {
            let transitions = self.signals.iter().position(|s| s.name == self.clock_id)
                .map_or(Vec::new(), |i| self.read_transitions(i));
            let mut analysis = crate::clock::analyze(&self.clock_id, self.clock_period_ticks,
                self.first_rise_tick, &transitions);
            if self.clock_table.iter().any(|c| c.period != self.clock_period_ticks) {
                analysis.verdict.findings.push(crate::clock::ClockFinding {
                    reason: "multiple_clock_periods".into(), tick: None,
                    detail: "several clock candidates have different periods; select --clock or use --async".into()
                });
            }
            analysis
        })
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
    /// Preserves every exact alias selection. Globs keep the last matching
    /// alias per group unless an exact selection already retains that group.
    ///
    /// Exits with code 2 on glob-syntax errors. Used to silently return an
    /// empty match list, which made `list -s "[oops"` look like "no signals
    /// matched" instead of "bad pattern" — and erased the exit-code signal
    /// for scripts and CI.
    pub fn match_signals(&self, patterns: &[String]) -> Vec<usize> {
        self.match_signals_with_names(patterns, &[]).0
    }

    fn match_signals_with_names(
        &self,
        patterns: &[String],
        extra_names: &[String],
    ) -> (Vec<usize>, Vec<usize>) {
        let selection = self.select_signals_with_names(patterns, extra_names, true);
        (selection.stored, selection.extra)
    }

    fn select_signals_with_names(
        &self,
        patterns: &[String],
        extra_names: &[String],
        report: bool,
    ) -> SignalSelection {
        let matchers = match compile_patterns(patterns) {
            Ok(m) => m,
            Err(e) => {
                eprintln!("ERROR: {}", e);
                std::process::exit(2);
            }
        };
        let mut pattern_hit = vec![false; matchers.len()];
        let mut exact = std::collections::HashSet::new();
        let mut globs: HashMap<u64, usize> = HashMap::new();
        for (i, signal) in self.signals.iter().enumerate() {
            for (pi, matcher) in matchers.iter().enumerate() {
                if match_signal(&signal.name, std::slice::from_ref(matcher)) {
                    pattern_hit[pi] = true;
                    if explicit_glob(&patterns[pi]) {
                        globs.insert(signal.group_id, i);
                    } else {
                        exact.insert(i);
                    }
                }
            }
        }
        let exact_groups: std::collections::HashSet<u64> =
            exact.iter().map(|&i| self.signals[i].group_id).collect();
        exact.extend(
            globs
                .into_iter()
                .filter(|(group, _)| !exact_groups.contains(group))
                .map(|(_, i)| i),
        );
        let mut results: Vec<usize> = exact.into_iter().collect();
        results.sort_unstable();
        let mut extra_results = Vec::new();
        for (i, name) in extra_names.iter().enumerate() {
            let mut hit = false;
            for (pi, matcher) in matchers.iter().enumerate() {
                if match_signal(name, std::slice::from_ref(matcher)) {
                    pattern_hit[pi] = true;
                    hit = true;
                }
            }
            if hit {
                extra_results.push(i);
            }
        }
        if report {
            report_unmatched_patterns(
                patterns,
                &pattern_hit,
                results.is_empty() && extra_results.is_empty(),
            );
        }
        SignalSelection {
            warnings: unmatched_pattern_warnings(
                patterns,
                &pattern_hit,
                results.is_empty() && extra_results.is_empty(),
            ),
            stored: results,
            extra: extra_results,
        }
    }
}

fn explicit_glob(pattern: &str) -> bool {
    let scan = match (pattern.rfind('['), pattern.strip_suffix(']')) {
        (Some(open), Some(_)) => {
            let index = &pattern[open + 1..pattern.len() - 1];
            if !index.is_empty()
                && index.bytes().all(|b| b.is_ascii_digit() || b == b':')
                && index.bytes().any(|b| b.is_ascii_digit())
            {
                &pattern[..open]
            } else {
                pattern
            }
        }
        _ => pattern,
    };
    let mut escaped = false;
    for ch in scan.chars() {
        if escaped {
            escaped = false;
        } else if ch == '\\' {
            escaped = true;
        } else if matches!(ch, '*' | '?' | '[' | '{') {
            return true;
        }
    }
    false
}

#[derive(Default)]
struct AliasBudget {
    noted: usize,
    withheld: usize,
    event_withheld: bool,
    seen: std::collections::HashSet<String>,
}

fn alias_warnings(
    cache: &ColumnCache,
    patterns: &[String],
    event: bool,
    budget: &mut AliasBudget,
) -> Vec<String> {
    let mut warnings = Vec::new();
    let rows = cache.select_signals_with_names(patterns, &[], false).stored;
    for pattern in patterns.iter().filter(|p| explicit_glob(p)) {
        let matchers = compile_patterns(std::slice::from_ref(pattern)).unwrap_or_else(|error| {
            eprintln!("ERROR: {error}");
            std::process::exit(2);
        });
        let mut representative = HashMap::new();
        if event {
            for (i, signal) in cache
                .signals
                .iter()
                .enumerate()
                .filter(|(_, s)| match_signal(&s.name, &matchers))
            {
                representative.entry(signal.group_id).or_insert(i);
            }
        } else {
            for &i in &rows {
                representative.entry(cache.signals[i].group_id).or_insert(i);
            }
        }
        for (i, signal) in cache
            .signals
            .iter()
            .enumerate()
            .filter(|(_, s)| match_signal(&s.name, &matchers))
        {
            if (!event && rows.binary_search(&i).is_ok())
                || representative.get(&signal.group_id) == Some(&i)
            {
                continue;
            }
            if let Some(&shown) = representative.get(&signal.group_id) {
                if budget.seen.insert(signal.name.clone()) {
                    if budget.noted < 32 {
                        budget.noted += 1;
                        warnings.push(if event {
                            format!(
                                "# {} is an alias of {} (event selected once)",
                                signal.name, cache.signals[shown].name
                            )
                        } else {
                            format!(
                                "# {} is an alias of {} (shown once)",
                                signal.name, cache.signals[shown].name
                            )
                        });
                    } else {
                        budget.withheld += 1;
                        budget.event_withheld |= event;
                    }
                }
            }
        }
    }
    warnings
}

/// Shared advisory metadata: analysis never changes a query's chosen mode.
pub fn clock_query_config(cache: &ColumnCache, original: &ExtractConfig) -> ExtractConfig {
    let mut cfg = original.clone();
    let cycle_tokens = cfg
        .time_str
        .iter()
        .chain(cfg.at_str.iter())
        .chain(cfg.diff_strs.iter().flat_map(|(a, b)| [a, b]))
        .any(|s| s.split(':').any(|t| t.ends_with('c')))
        || cfg
            .marker_tokens
            .iter()
            .any(|(_, t)| matches!(t, crate::format::TimeToken::Cycle(_)));
    if cfg.async_mode && !cycle_tokens {
        return cfg;
    }
    let reset = if cfg.with_reset {
        0
    } else {
        find_reset_deassert_tick(cache, &cfg).unwrap_or(0)
    };
    let base = cycle_base(cache, reset);
    let analysis = cache.clock_analysis();
    let all_stops = analysis.ranges(base, cache.clock_period_ticks);
    let mut verdict = analysis.verdict.clone();
    verdict.first_off_grid_cycle = verdict
        .first_off_grid_tick
        .map(|t| tick_to_cycle(t, base, cache.clock_period_ticks));
    verdict.stopped = crate::clock::summary(&all_stops);
    for finding in &verdict.findings {
        if matches!(
            finding.reason.as_str(),
            "multiple_clock_periods" | "insufficient_edges"
        ) {
            continue;
        }
        let warning = format!(
            "# WARNING: clock '{}' {}{}: {}",
            verdict.clock,
            finding.reason,
            finding
                .tick
                .map_or(String::new(), |t| format!(" at tick {t}")),
            finding.detail
        );
        if !cfg.json_format {
            eprintln!("{warning}");
        }
        cfg.warnings.push(warning);
    }
    if !cfg.async_mode {
        let (mut lo, mut hi) = sampled_cycle_window(cache, &cfg, base);
        if let Some(at) = cfg.at_time {
            lo = at.max(0) as u64;
            hi = lo;
        }
        if let Some((a, b)) = cfg.diff_points {
            lo = a.max(0) as u64;
            hi = b.max(0) as u64;
        }
        cfg.clock_stopped = all_stops
            .into_iter()
            .filter(|r| lo <= hi && r.from_cycle <= hi && r.to_cycle >= lo)
            .collect();
        if !cfg.json_format && cfg.diff_points.is_some() {
            for r in &cfg.clock_stopped {
                eprintln!("# clk stopped {}:{}", r.from_cycle, r.to_cycle);
            }
        }
        if !cfg.json_format && (cfg.find_pattern.is_some() || cfg.sample_at_pattern.is_some()) {
            for r in &cfg.clock_stopped {
                eprintln!(
                    "# clk stopped {}:{}{}",
                    r.from_cycle,
                    r.to_cycle,
                    if cfg.include_stopped {
                        " (included in level matching)"
                    } else {
                        " (excluded from level matching)"
                    }
                );
            }
        }
    }
    cfg.clock_regularity = Some(verdict);
    cfg
}

fn sampled_value(cfg: &ExtractConfig, transitions: &[(u64, String)], edge: u64) -> String {
    cfg.sample_tick(edge)
        .map_or_else(|| "x".into(), |tick| value_at_tick(transitions, tick))
}

fn observation_boundaries(
    cfg: &ExtractConfig,
    base: u64,
    period: u64,
    transitions: &[(u64, String)],
    start: u64,
    end: u64,
) -> Vec<u64> {
    let mut boundaries = vec![start];
    for (tick, _) in transitions {
        let Some(observation) = tick.checked_add(u64::from(cfg.pre_edge)) else {
            continue;
        };
        if observation <= base {
            continue;
        }
        let offset = observation - base;
        let Some(cycle) = (offset / period).checked_add(u64::from(offset % period != 0) + 1) else {
            continue;
        };
        if cycle > start && cycle <= end {
            boundaries.push(cycle);
        }
    }
    boundaries.sort_unstable();
    boundaries.dedup();
    boundaries
}

fn query_config(cache: &ColumnCache, original: &ExtractConfig) -> ExtractConfig {
    let mut cfg = clock_query_config(cache, original);
    let mut budget = AliasBudget::default();
    let mut warnings = if cfg.explicit_selectors {
        alias_warnings(cache, &cfg.patterns, false, &mut budget)
    } else {
        Vec::new()
    };
    let event_patterns: Vec<String> = cfg
        .find_pattern
        .iter()
        .chain(cfg.sample_at_pattern.iter())
        .cloned()
        .chain(cfg.distance_a.iter().map(|(p, _)| p.clone()))
        .chain(cfg.distance_b.iter().map(|(p, _)| p.clone()))
        .collect();
    warnings.extend(alias_warnings(cache, &event_patterns, true, &mut budget));
    if budget.withheld > 0 {
        warnings.push(if budget.event_withheld {
            format!(
                "additional {} alias names selected once; narrow selectors for details",
                budget.withheld
            )
        } else {
            format!(
                "additional {} alias names shown once; narrow selectors for details",
                budget.withheld
            )
        });
    }
    if !cfg.json_format {
        for warning in &warnings {
            eprintln!("{warning}");
        }
    }
    cfg.warnings = crate::output::merge_warnings(cfg.warnings, &warnings);
    cfg
}

fn emit_query_json<T: serde::Serialize>(
    cfg: &ExtractConfig,
    command: &str,
    data: T,
    warnings: Vec<String>,
) {
    crate::output::emit_clock_json(
        command,
        data,
        crate::output::merge_warnings(warnings, &cfg.warnings),
        cfg.clock_regularity.clone(),
        cfg.clock_stopped.clone(),
    );
}

fn limit_warning(limit: usize) -> String {
    format!("# WARNING: limit ({limit}) reached, output truncated")
}

fn record_limit(cfg: &ExtractConfig, count: usize) -> Vec<String> {
    if count > cfg.max_lines {
        let warning = limit_warning(cfg.max_lines);
        if !cfg.json_format {
            eprintln!("{warning}");
        }
        vec![warning]
    } else {
        Vec::new()
    }
}

/// The `value --at` past-the-end diagnostic. Python's `bwave gui` clamps a
/// `--time` end on this marker (BEYOND_RANGE_MARKER in contract.py).
fn beyond_range_message(at_time: &str) -> String {
    format!("value time {at_time} is beyond simulation range")
}

fn unmatched_pattern_message(pattern: &str) -> String {
    format!("no signals match '{}' — filter dropped", pattern)
}

fn array_element_hint(pattern: &str) -> String {
    format!("(indexed element: simulators do not dump unpacked array/memory elements by default — probe the element into a wire, or check `bwave list -s '*{}*'` for what was dumped)", array_base_name(pattern))
}

fn unmatched_pattern_warnings(patterns: &[String], hit: &[bool], all_empty: bool) -> Vec<String> {
    if all_empty {
        return Vec::new();
    }
    patterns
        .iter()
        .zip(hit)
        .filter(|(_, hit)| !**hit)
        .map(|(pat, _)| {
            let mut warning = unmatched_pattern_message(pat);
            if looks_like_array_element(pat) {
                warning.push(' ');
                warning.push_str(&array_element_hint(pat));
            }
            warning
        })
        .collect()
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
        let _ = writeln!(err, "# WARNING: {}", unmatched_pattern_message(pat));
        if looks_like_array_element(pat) && hinted_bases.insert(array_base_name(pat)) {
            let _ = writeln!(err, "#   {}", array_element_hint(pat));
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
    build_virtual_transitions, evaluate_virtual_values, resolve_virtual_definitions,
    ResolvedVirtual, VirtualValue,
};

/// A virtual signal entry, with pre-built transitions.
pub struct VirtualEntry {
    pub name: String,
    pub transitions: Vec<(u64, String)>,
}

fn resolve_virtuals(cache: &ColumnCache, cfg: &ExtractConfig) -> Vec<ResolvedVirtual> {
    let signal_names: Vec<String> = cache.signals.iter().map(|s| s.name.clone()).collect();
    let signal_widths: Vec<u32> = cache.signals.iter().map(|s| s.width).collect();
    resolve_virtual_definitions(&cfg.virtual_defs, &signal_names, &signal_widths).unwrap_or_else(
        |error| {
            eprintln!("ERROR: --virtual '{}': {}", error.definition, error.message);
            std::process::exit(2);
        },
    )
}

/// Build virtual signal entries from cfg.virtual_defs, resolving against cache signals.
/// Returns (resolved_virtuals, their transition lists).
fn build_virtuals(cache: &ColumnCache, cfg: &ExtractConfig) -> Vec<VirtualEntry> {
    let resolved_virtuals = resolve_virtuals(cache, cfg);
    let mut prior_transitions: Vec<Vec<(u64, String)>> = Vec::new();
    let mut entries: Vec<VirtualEntry> = Vec::new();

    // Lazily load only signal columns referenced by virtual expressions. The
    // old eager path decoded every signal in large traces for a two-signal
    // predicate, making simple virtual finds tens of seconds slower.
    let mut real_transitions: Vec<Vec<(u64, String)>> = vec![Vec::new(); cache.signals.len()];
    let mut loaded_real_signals = std::collections::HashSet::new();

    for resolved in resolved_virtuals {
        for idx in resolved.referenced_signal_indices() {
            if loaded_real_signals.insert(idx) {
                real_transitions[idx] = cache.read_transitions(idx);
            }
        }

        let transitions = build_virtual_transitions(
            &resolved,
            &prior_transitions,
            &real_transitions,
            0,
            cache.sim_end_tick,
        );

        prior_transitions.push(transitions.clone());
        entries.push(VirtualEntry {
            name: resolved.name,
            transitions,
        });
    }

    entries
}

fn evaluate_virtuals_at_tick(
    cache: &ColumnCache,
    resolved: &[ResolvedVirtual],
    tick: u64,
) -> Vec<VirtualValue> {
    let mut referenced = std::collections::BTreeSet::new();
    for entry in resolved {
        referenced.extend(entry.referenced_signal_indices());
    }
    let referenced: Vec<usize> = referenced.into_iter().collect();
    cache.prefetch_window(&referenced, tick);
    let real_values: HashMap<usize, String> = referenced
        .into_iter()
        .map(|idx| (idx, cache.value_at_tick_direct(idx, tick)))
        .collect();
    evaluate_virtual_values(resolved, &|idx| real_values[&idx].clone())
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
) -> (HashMap<usize, Radix>, Vec<String>) {
    let mut map = HashMap::new();
    let mut warnings = Vec::new();
    if cfg.signal_radixes.is_empty() {
        return (map, warnings);
    }
    // Track explicit radix requests so collisions include an explicit Hex.
    let mut requested: HashMap<usize, Vec<Radix>> = HashMap::new();
    let exact_matchers: Vec<_> = cfg
        .patterns
        .iter()
        .filter(|p| !explicit_glob(p))
        .flat_map(|p| compile_patterns(std::slice::from_ref(p)).unwrap_or_default())
        .collect();
    let retained_exact: std::collections::HashSet<usize> = matched
        .iter()
        .copied()
        .filter(|&idx| match_signal(&cache.signals[idx].name, &exact_matchers))
        .collect();
    for (pat, radix) in &cfg.signal_radixes {
        if let Ok(matchers) = compile_patterns(&[pat.clone()]) {
            let selected_groups: std::collections::HashSet<u64> = cache
                .signals
                .iter()
                .filter(|s| match_signal(&s.name, &matchers))
                .map(|s| s.group_id)
                .collect();
            for &idx in matched {
                let signal = &cache.signals[idx];
                let matches = match_signal(&signal.name, &matchers);
                if (matches && !explicit_glob(pat))
                    || (explicit_glob(pat)
                        && !retained_exact.contains(&idx)
                        && selected_groups.contains(&signal.group_id))
                {
                    requested.entry(idx).or_default().push(*radix);
                    map.insert(idx, *radix);
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
            let warning = format!(
                "signal '{}' matched by multiple -s patterns with different radixes ({:?}); using last: {:?}",
                cache.signals[idx].name, distinct,
                map.get(&idx).copied().unwrap_or(Radix::Hex),
            );
            let _ = writeln!(err, "# WARNING: {}", warning);
            warnings.push(warning);
        }
    }
    let _ = err.flush();
    (map, warnings)
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
    let padded: Vec<Vec<Option<u8>>> = value_hist
        .keys()
        .map(|v| {
            let nibs: Vec<Option<u8>> = v
                .chars()
                .map(|c| c.to_digit(16).map(|digit| digit as u8))
                .collect();
            let padding = if nibs.first() == Some(&None) {
                None
            } else {
                Some(0u8)
            };
            let mut p = vec![padding; n_nibbles.saturating_sub(nibs.len())];
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
            if let Some(Some(nibble)) = val.get(nib_idx) {
                if nibble & (1 << bit_in_nib) != 0 {
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

fn query_reset_deassert_tick(cache: &ColumnCache, cfg: &ExtractConfig) -> Option<u64> {
    if !cfg.with_reset && !cfg.async_mode {
        find_reset_deassert_tick(cache, cfg)
    } else {
        None
    }
}

/// Scan actual changes separately from positive-duration occupied runs.
fn scan_stats(
    transitions: &[(u64, String)],
    start: u64,
    end: u64,
    strict_boundary: bool,
    initial: &str,
    name: String,
    width: u32,
) -> CacheStatsEntry {
    let mut stats = CacheStatsEntry {
        name,
        width,
        transitions: 0,
        toggle_pct: 0.0,
        value_pct: 0.0,
        value_hist: HashMap::new(),
        time_in_state: HashMap::new(),
    };
    let mut last_tick = start;
    let mut last_value = initial.to_string();
    for (tick, value) in transitions {
        // Initialization at zero establishes a state, not a transition.
        if *tick < start || (*tick == start && (start == 0 || strict_boundary)) {
            last_value = value.clone();
            continue;
        }
        if *tick > end {
            break;
        }
        if *value == last_value {
            continue;
        }
        record_occupied_run(&mut stats, &last_value, tick.saturating_sub(last_tick));
        stats.transitions += 1;
        last_value = value.clone();
        last_tick = *tick;
    }
    record_occupied_run(&mut stats, &last_value, end.saturating_sub(last_tick));
    stats.toggle_pct = compute_toggle_pct(&stats.value_hist, width);
    let known_values = stats
        .value_hist
        .keys()
        .filter(|value| value.chars().all(|digit| digit.is_ascii_hexdigit()))
        .count();
    stats.value_pct = compute_value_pct(known_values, width);
    stats
}

fn record_occupied_run(stats: &mut CacheStatsEntry, value: &str, elapsed: u64) {
    if elapsed > 0 {
        *stats.time_in_state.entry(value.to_string()).or_insert(0) += elapsed;
        *stats.value_hist.entry(value.to_string()).or_insert(0) += 1;
    }
}

fn stats_window(cache: &ColumnCache, cfg: &ExtractConfig, reset_start: u64) -> (u64, u64) {
    let resolve = |time: i64| {
        if time <= 0 {
            return reset_start;
        }
        let time = time as u64;
        if !cfg.async_mode && cache.clock_period_ticks > 0 {
            cycle_base(cache, reset_start)
                .saturating_add((time - 1).saturating_mul(cache.clock_period_ticks))
        } else {
            time
        }
    };
    let start = resolve(cfg.time_min)
        .max(reset_start)
        .min(cache.sim_end_tick);
    let end = cfg
        .time_max
        .map(resolve)
        .unwrap_or(cache.sim_end_tick)
        .min(cache.sim_end_tick)
        .max(start);
    (start, end)
}

type Occupancy = std::collections::BTreeMap<String, u64>;
fn sampled_occupancy(
    cache: &ColumnCache,
    cfg: &ExtractConfig,
    transitions: &[(u64, String)],
) -> (Occupancy, Occupancy) {
    if cfg.async_mode || cache.clock_period_ticks == 0 {
        return (Occupancy::new(), Occupancy::new());
    }
    let base = cycle_base(cache, query_reset_deassert_tick(cache, cfg).unwrap_or(0));
    let (start, end) = sampled_cycle_window(cache, cfg, base);
    if start > end {
        return (Occupancy::new(), Occupancy::new());
    }
    let boundaries =
        observation_boundaries(cfg, base, cache.clock_period_ticks, transitions, start, end);
    let (mut clocked, mut stopped) = (Occupancy::new(), Occupancy::new());
    for (index, &lo) in boundaries.iter().enumerate() {
        let hi = boundaries.get(index + 1).map_or(end, |next| next - 1);
        let value = sampled_value(cfg, transitions, base + (lo - 1) * cache.clock_period_ticks);
        let stopped_count = crate::clock::stopped_count(&cfg.clock_stopped, lo, hi);
        *clocked.entry(value.clone()).or_insert(0) += hi - lo + 1 - stopped_count;
        *stopped.entry(value).or_insert(0) += stopped_count;
    }
    clocked.retain(|_, n| *n > 0);
    stopped.retain(|_, n| *n > 0);
    (clocked, stopped)
}

/// Run stats query from cache.
pub fn stats_from_cache(cache: &ColumnCache, cfg: &ExtractConfig) {
    let resolved_cfg = query_config(cache, cfg);
    let cfg = &resolved_cfg;
    let matched = cache.match_signals(&cfg.patterns);
    let virtuals = build_virtuals(cache, cfg);

    let mut json_warnings = if cfg.json_format {
        let names: Vec<String> = resolve_virtuals(cache, cfg)
            .iter()
            .map(|v| v.name.clone())
            .collect();
        cache
            .select_signals_with_names(&cfg.patterns, &names, false)
            .warnings
    } else {
        Vec::new()
    };

    let sync_mode = !cfg.async_mode;
    let reset_deassert_tick = query_reset_deassert_tick(cache, cfg);
    let reset_start = reset_deassert_tick.unwrap_or(0);
    let (effective_start, window_end) = stats_window(cache, cfg, reset_start);
    let total_ticks = window_end.saturating_sub(effective_start);
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
            emit_query_json(
                cfg,
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
    let (radix_map, radix_warnings) = build_radix_map(cache, &matched, cfg);
    json_warnings.extend(radix_warnings);
    let stdout = io::stdout();
    let mut out = BufWriter::new(stdout.lock());
    let stderr = io::stderr();
    let mut err = BufWriter::new(stderr.lock());

    if !cfg.json_format && !cfg.async_mode {
        if let Some(verdict) = &cfg.clock_regularity {
            let _ = writeln!(out, "{}", crate::clock::verdict_text(verdict));
        }
    }
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

        let strict_boundary = is_clock_sig
            && cache.clock_before_reset_at_deassert
            && reset_deassert_tick == Some(effective_start);
        entries.push(scan_stats(
            &transitions,
            effective_start,
            window_end,
            strict_boundary,
            "x",
            sig.name.clone(),
            sig.width,
        ));
    }

    for ve in &virtuals {
        entries.push(scan_stats(
            &ve.transitions,
            effective_start,
            window_end,
            false,
            "0",
            ve.name.clone(),
            1,
        ));
    }

    // Sort by transitions descending
    entries.sort_by(|a, b| b.transitions.cmp(&a.transitions).then(a.name.cmp(&b.name)));

    json_warnings.extend(record_limit(cfg, entries.len()));
    entries.truncate(cfg.max_lines);

    let occupancy: HashMap<_, _> = matched
        .iter()
        .map(|&i| {
            (
                cache.signals[i].name.clone(),
                sampled_occupancy(cache, cfg, &cache.read_transitions(i)),
            )
        })
        .chain(virtuals.iter().map(|v| {
            (
                v.name.clone(),
                sampled_occupancy(cache, cfg, &v.transitions),
            )
        }))
        .collect();

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
                    time_in_state_clocked_cycles: (!cfg.async_mode && cache.clock_period_ticks > 0)
                        .then(|| {
                            occupancy[&e.name]
                                .0
                                .iter()
                                .map(|(v, n)| (format_as_verilog_literal(v, e.width, radix), *n))
                                .collect()
                        }),
                    time_in_state_stopped_cycles: (!cfg.async_mode && cache.clock_period_ticks > 0)
                        .then(|| {
                            occupancy[&e.name]
                                .1
                                .iter()
                                .map(|(v, n)| (format_as_verilog_literal(v, e.width, radix), *n))
                                .collect()
                        }),
                }
            })
            .collect();

        emit_query_json(
            cfg,
            "stats",
            crate::output::StatsData {
                simulation_ns: total_ns,
                total_ticks,
                total_cycles,
                clock_period_ns,
                signals: json_entries,
            },
            json_warnings,
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

        let (clocked, stopped) = &occupancy[&entry.name];
        let sampled_mode = sync_mode && cache.clock_period_ticks > 0;
        if (sampled_mode && (!clocked.is_empty() || !stopped.is_empty()))
            || (!entry.time_in_state.is_empty() && total_ticks > 0)
        {
            // Look up radix for this signal (find by name since entries are sorted)
            let sig_radix = matched
                .iter()
                .find(|&&i| cache.signals[i].name == entry.name)
                .and_then(|&i| radix_map.get(&i).copied());
            let mut states = entry.time_in_state.clone();
            if sampled_mode {
                for value in clocked.keys().chain(stopped.keys()) {
                    states.entry(value.clone()).or_insert(0);
                }
            }
            let mut sorted_states: Vec<(&String, &u64)> = states.iter().collect();
            sorted_states.sort_by(|a, b| b.1.cmp(a.1).then(a.0.cmp(b.0)));
            let sampled_total: u64 = clocked.values().chain(stopped.values()).sum();
            let mut zero_pct_count: usize = 0;
            for (val, ticks) in &sorted_states {
                let t = **ticks;
                let sampled_count = clocked.get(*val).copied().unwrap_or(0)
                    + stopped.get(*val).copied().unwrap_or(0);
                let pct = if sampled_mode {
                    sampled_count as f64 / sampled_total.max(1) as f64 * 100.0
                } else {
                    (t as f64 / total_ticks as f64) * 100.0
                };
                if pct < 0.5 && (!sampled_mode || sampled_count == 0) {
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
                if sampled_mode {
                    let _ = writeln!(
                        out,
                        "{}: {}% ({} clocked, {} stopped cycles)",
                        dval,
                        pct_str,
                        clocked.get(*val).copied().unwrap_or(0),
                        stopped.get(*val).copied().unwrap_or(0)
                    );
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
    let resolved_cfg = query_config(cache, cfg);
    let cfg = &resolved_cfg;
    let matched = cache.match_signals(&cfg.patterns);
    if matched.is_empty() {
        // A stuck-scan over zero signals would report "nothing stuck" — a
        // false all-clear. Total miss is the same hard error as elsewhere.
        exit_no_signal_match(&cfg.patterns, cache.unique_signal_count());
    }
    let sync_mode = !cfg.async_mode;

    let reset_deassert_tick = query_reset_deassert_tick(cache, cfg);
    let effective_start = reset_deassert_tick.unwrap_or(0);
    let total_ticks = cache.sim_end_tick.saturating_sub(effective_start);

    let (radix_map, _) = build_radix_map(cache, &matched, cfg);
    let stdout = io::stdout();
    let mut out = BufWriter::new(stdout.lock());

    let mut stuck: Vec<(String, String, String, usize)> = Vec::new(); // +sig_idx
    for &sig_idx in &matched {
        let transitions = cache.read_transitions(sig_idx);
        let sig = &cache.signals[sig_idx];

        let mut trans_count = 0u64;
        let mut last_value = "x".to_string();
        for (tick, val) in &transitions {
            if *tick < effective_start || (*tick == 0 && effective_start == 0) {
                last_value = val.clone();
                continue;
            }
            if *tick > cache.sim_end_tick {
                break;
            }
            if *val != last_value {
                trans_count += 1;
            }
            last_value = val.clone();
        }
        let stuck_value = last_value;

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
                    values_match(v, filter)
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
    record_limit(cfg, stuck.len());
    for (name, width_str, stuck_val, sig_idx) in stuck.iter().take(cfg.max_lines) {
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
    let resolved_cfg = query_config(cache, cfg);
    let cfg = &resolved_cfg;
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
            std::process::exit(2);
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

    // A missing target historically emits its error envelope before validating
    // watched selectors unless query_config already validated explicit selectors.
    let target_empty = target_indices.is_empty() && virtual_matches.is_empty();
    let mut json_warnings =
        if cfg.json_format && (!target_empty || compile_patterns(&cfg.patterns).is_ok()) {
            let virtual_names: Vec<String> = resolve_virtuals(cache, cfg)
                .iter()
                .map(|v| v.name.clone())
                .collect();
            cache
                .select_signals_with_names(&cfg.patterns, &virtual_names, false)
                .warnings
        } else {
            Vec::new()
        };

    if target_indices.is_empty() && virtual_matches.is_empty() {
        if cfg.json_format {
            // Emit an empty envelope so consumers get valid JSON to parse.
            let mode = if cfg.async_mode { "async" } else { "sync" };
            emit_query_json(
                cfg,
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
                {
                    let mut warnings = vec![format!("no signals match pattern '{}'", find_pattern)];
                    warnings.extend(json_warnings);
                    warnings
                },
            );
        }
        // Total miss is a hard error either way; JSON consumers got their
        // envelope above, the exit code says the pattern was wrong.
        exit_no_signal_match(
            std::slice::from_ref(find_pattern),
            cache.unique_signal_count(),
        );
    }

    let (radix_map, radix_warnings) = build_radix_map(cache, &target_indices, cfg);
    json_warnings.extend(radix_warnings);
    let mode = TriggerMode::classify(find_value_str);
    let sync_mode = !cfg.async_mode;

    let reset_deassert_tick = if !cfg.with_reset && sync_mode {
        find_reset_deassert_tick(cache, cfg)
    } else {
        None
    };
    let effective_start = reset_deassert_tick.unwrap_or(0);

    // Strip the common scope prefix from matched names for display.
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

    let use_cycle_walk = sync_mode && cache.clock_period_ticks > 0;

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
            if !cfg.count_only && !cfg.last_match && line_count >= cfg.max_lines {
                truncated = true;
                if !json_format {
                    eprintln!("{}", limit_warning(cfg.max_lines));
                }
            } else {
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
            let runs = sampled_event_runs(cache, cfg, cb, &transitions, sig.width, find_value_str);
            let (omitted, cycles) = find_run_selection(&runs, cfg, line_count);
            find_count = find_count
                .checked_add(omitted)
                .expect("find count overflow");
            for cycle in cycles {
                let tick = cb + (cycle - 1) * cache.clock_period_ticks;
                let val = sampled_value(cfg, &transitions, tick);
                let dval = fmt_val(&val, sig_idx, sig.width, &radix_map);
                record_match!(cycle, dname, val.clone(), dval);
                if truncated || cfg.first_match {
                    break 'outer;
                }
            }
        } else {
            // Edge mode or async: walk transitions only
            let mut prev_val: Option<&str> = None;
            for (tick, val) in &transitions {
                if *tick < effective_start {
                    prev_val = Some(val.as_str());
                    continue;
                }

                if sync_mode && !(mode == TriggerMode::Change && cache.clock_period_ticks == 0) {
                    if cache.clock_period_ticks > 0 && (cb > 0 || mode == TriggerMode::Change) {
                        let cycle = tick_to_cycle(*tick, cb, cache.clock_period_ticks);
                        if (cycle as i64) < cfg.time_min {
                            prev_val = Some(val.as_str());
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
                        prev_val = Some(val.as_str());
                        continue;
                    }
                    if let Some(max) = cfg.time_max {
                        if (*tick as i64) > max {
                            break;
                        }
                    }
                }

                let matched = mode.matches(prev_val, val, sig.width, find_value_str);
                prev_val = Some(val.as_str());

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
        if truncated || (use_cycle_walk && cfg.first_match && find_count > 0) {
            break;
        }
        let dname = &ve.name;

        if use_cycle_walk {
            let runs = sampled_event_runs(cache, cfg, cb, &ve.transitions, 1, find_value_str);
            let (omitted, cycles) = find_run_selection(&runs, cfg, line_count);
            find_count = find_count
                .checked_add(omitted)
                .expect("find count overflow");
            for cycle in cycles {
                let tick = cb + (cycle - 1) * cache.clock_period_ticks;
                let val = sampled_value(cfg, &ve.transitions, tick);
                record_match!(cycle, dname.as_str(), val.clone(), &val);
                if truncated || cfg.first_match {
                    break 'vouter;
                }
            }
        } else if mode != TriggerMode::Literal {
            let mut prev_val: Option<&str> = None;
            for (tick, val) in &ve.transitions {
                if *tick < effective_start {
                    prev_val = Some(val.as_str());
                    continue;
                }
                let m = mode.matches(prev_val, val, 1, find_value_str);
                prev_val = Some(val.as_str());
                if m {
                    let time = if sync_mode && cache.clock_period_ticks > 0 {
                        tick_to_cycle(*tick, cb, cache.clock_period_ticks)
                    } else {
                        *tick
                    };
                    if mode == TriggerMode::Change
                        && ((time as i64) < cfg.time_min
                            || cfg.time_max.is_some_and(|max| (time as i64) > max))
                    {
                        continue;
                    }
                    record_match!(time, dname.as_str(), val.clone(), val);
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

        emit_query_json(
            cfg,
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
            {
                if truncated {
                    json_warnings.push(limit_warning(cfg.max_lines));
                }
                json_warnings
            },
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
    let resolved_cfg = query_config(cache, cfg);
    let cfg = &resolved_cfg;
    let sa_pattern = match cfg.sample_at_pattern.as_ref() {
        Some(p) => p,
        None => return,
    };
    let sa_value = cfg.sample_at_value.as_deref().unwrap_or("");

    let virtuals = build_virtuals(cache, cfg);
    let virtual_names: Vec<String> = virtuals.iter().map(|entry| entry.name.clone()).collect();
    let (mut trigger_indices, trigger_virtual_indices) =
        cache.match_signals_with_names(std::slice::from_ref(sa_pattern), &virtual_names);
    let mut trigger_groups = std::collections::HashSet::new();
    trigger_indices.retain(|&i| trigger_groups.insert(cache.signals[i].group_id));
    let (watched_indices, watched_virtual_indices) =
        cache.match_signals_with_names(&cfg.patterns, &virtual_names);

    if trigger_indices.is_empty() && trigger_virtual_indices.is_empty() {
        // Same class of failure as the other total-miss exits — unified to 2
        // (was 1) so every "your pattern matched nothing" ends the same way.
        eprintln!(
            "ERROR: --sample: no signals match trigger pattern '{}'",
            sa_pattern
        );
        std::process::exit(2);
    }

    if !cfg.count_only && watched_indices.is_empty() && watched_virtual_indices.is_empty() {
        exit_no_signal_match(&cfg.patterns, cache.unique_signal_count());
    }

    let mode = TriggerMode::classify(sa_value);
    let level_mode = mode == TriggerMode::Literal;

    let sync_mode = !cfg.async_mode;
    let reset_deassert_tick = if !cfg.with_reset && sync_mode {
        find_reset_deassert_tick(cache, cfg)
    } else {
        None
    };
    let effective_start = reset_deassert_tick.unwrap_or(0);

    let cb = cycle_base(cache, effective_start);
    let mut trigger_data: Vec<(Vec<(u64, String)>, u32)> = trigger_indices
        .iter()
        .map(|&idx| (cache.read_transitions(idx), cache.signals[idx].width))
        .collect();
    trigger_data.extend(
        trigger_virtual_indices
            .iter()
            .map(|&idx| (virtuals[idx].transitions.clone(), 1)),
    );

    // Read trigger signal transitions and identify trigger ticks.
    // Synchronous predicates compare observations on the eligible clock grid.
    let mut trigger_ticks: Vec<u64> = Vec::new();
    let mut compressed_sample_count = None;
    // The output limit counts watched rows, not trigger events. Retain the
    // first event containing a row beyond the limit to prove truncation.
    let rows_per_event = watched_indices.len() + watched_virtual_indices.len();
    let discovery_limit = cfg.max_lines / rows_per_event.max(1);

    if sync_mode && cache.clock_period_ticks > 0 {
        let runs = union_cycle_runs(
            trigger_data
                .iter()
                .flat_map(|(transitions, width)| {
                    sampled_event_runs(cache, cfg, cb, transitions, *width, sa_value)
                })
                .collect(),
        );
        if cfg.count_only {
            let count = run_count(&runs);
            compressed_sample_count = Some(if cfg.first_match || cfg.last_match {
                count.min(1)
            } else {
                count
            });
        } else {
            let selected: Vec<u64> = if cfg.first_match {
                runs.first().map(|r| r.0).into_iter().collect()
            } else if cfg.last_match {
                runs.last().map(|r| r.1).into_iter().collect()
            } else {
                runs.iter()
                    .flat_map(|&(lo, hi)| lo..=hi)
                    .take(discovery_limit.saturating_add(1))
                    .collect()
            };
            trigger_ticks.extend(
                selected
                    .into_iter()
                    .map(|cycle| cb + (cycle - 1) * cache.clock_period_ticks),
            );
        }
    } else if level_mode && !sync_mode {
        // Async level mode: trigger at every watched-signal-change timestamp
        // where the settled trigger value matches.
        let mut change_ticks: std::collections::BTreeSet<u64> = std::collections::BTreeSet::new();
        for &w_idx in &watched_indices {
            for (tick, _) in &cache.read_transitions(w_idx) {
                if *tick >= effective_start {
                    change_ticks.insert(*tick);
                }
            }
        }
        for &w_idx in &watched_virtual_indices {
            for (tick, _) in &virtuals[w_idx].transitions {
                if *tick >= effective_start {
                    change_ticks.insert(*tick);
                }
            }
        }
        for tick in &change_ticks {
            for (trig_trans, _) in &trigger_data {
                if values_match(&value_at_tick(trig_trans, *tick), sa_value) {
                    trigger_ticks.push(*tick);
                    break;
                }
            }
        }
    } else {
        for (transitions, width) in &trigger_data {
            let mut previous = None;
            for (tick, val) in transitions {
                let triggered = mode.matches(previous, val, *width, sa_value);
                previous = Some(val.as_str());
                if *tick >= effective_start && triggered {
                    trigger_ticks.push(*tick);
                }
            }
        }
    }

    // Every discovery path feeds the same ordered event stream. Apply the
    // query window before directional selection so --first means the first
    // trigger in the requested range, not the first trigger in the store.
    trigger_ticks.sort_unstable();
    trigger_ticks.dedup();
    let query_time = |tick| {
        if sync_mode && cache.clock_period_ticks > 0 {
            tick_to_cycle(tick, cb, cache.clock_period_ticks)
        } else {
            tick
        }
    };
    trigger_ticks.retain(|&tick| {
        let time = query_time(tick) as i64;
        time >= cfg.time_min && cfg.time_max.is_none_or(|max| time <= max)
    });
    if cfg.first_match {
        trigger_ticks.truncate(1);
    } else if cfg.last_match {
        if let Some(last) = trigger_ticks.last().copied() {
            trigger_ticks.clear();
            trigger_ticks.push(last);
        }
    }

    // For each trigger tick, sample all watched signals
    let (radix_map, _) = build_radix_map(cache, &watched_indices, cfg);
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
    let _ = writeln!(
        err,
        "# sample: {} trigger signal(s)",
        trigger_indices.len() + trigger_virtual_indices.len()
    );

    // Preload watched signal transitions for binary search
    let watched_data: Vec<Vec<(u64, String)>> = watched_indices
        .iter()
        .map(|&i| cache.read_transitions(i))
        .collect();
    let watched_virtual_data: Vec<&VirtualEntry> = watched_virtual_indices
        .iter()
        .map(|&idx| &virtuals[idx])
        .collect();

    let mut sample_count = compressed_sample_count.unwrap_or(0u64);
    let mut line_count: usize = 0;

    'samples: for &trigger_tick in &trigger_ticks {
        sample_count += 1;
        if cfg.count_only {
            continue;
        }

        let time_label = query_time(trigger_tick).to_string();

        let stored_rows = watched_indices.iter().enumerate().map(|(wi, &w_idx)| {
            let sig = &cache.signals[w_idx];
            let dname = if !prefix.is_empty() && sig.name.starts_with(&prefix) {
                &sig.name[prefix.len()..]
            } else {
                &sig.name
            };
            // Binary search for value at trigger_tick
            let val = sampled_value(cfg, &watched_data[wi], trigger_tick);
            let dval = fmt_val(&val, w_idx, cache.signals[w_idx].width, &radix_map);
            (dname.to_string(), dval)
        });
        let virtual_rows = watched_virtual_data.iter().map(|entry| {
            let val = sampled_value(cfg, &entry.transitions, trigger_tick);
            (entry.name.clone(), val)
        });
        for (name, value) in stored_rows.chain(virtual_rows) {
            if line_count >= cfg.max_lines {
                let _ = writeln!(
                    err,
                    "# WARNING: limit ({}) reached, output truncated",
                    cfg.max_lines
                );
                break 'samples;
            }
            let _ = writeln!(out, "{} {} {}", time_label, name, value);
            line_count += 1;
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
    let resolved_cfg = query_config(cache, cfg);
    let cfg = &resolved_cfg;
    let at_time = match cfg.at_time {
        Some(t) => t,
        None => return,
    };

    let resolved_virtuals = resolve_virtuals(cache, cfg);
    let virtual_names: Vec<String> = resolved_virtuals
        .iter()
        .map(|entry| entry.name.clone())
        .collect();
    let selection = cache.select_signals_with_names(&cfg.patterns, &virtual_names, true);
    let matched = selection.stored;
    let matched_virtuals = selection.extra;
    if matched.is_empty() && matched_virtuals.is_empty() {
        if cfg.json_format {
            let mode = if cfg.async_mode { "async" } else { "sync" };
            emit_query_json(
                cfg,
                "value",
                crate::output::ValueData {
                    scope_prefix: String::new(),
                    mode: mode.to_string(),
                    at: at_time,
                    at_unit: "cycle".to_string(),
                    target_tick: 0,
                    sample_tick: None,
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
    let mut json_warnings = selection.warnings;

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

    let _ = err.flush();
    let unsigned_time = u64::try_from(at_time).unwrap_or_else(|_| {
        eprintln!("ERROR: value time {at_time} is outside simulation range");
        std::process::exit(2);
    });
    let target_tick = if sync_mode {
        if unsigned_time == 0 {
            effective_start
        } else {
            cycle_base(cache, effective_start)
                .checked_add(
                    (unsigned_time - 1)
                        .checked_mul(cache.clock_period_ticks)
                        .unwrap_or_else(|| {
                            eprintln!("ERROR: {}", beyond_range_message(&at_time.to_string()));
                            std::process::exit(2);
                        }),
                )
                .unwrap_or_else(|| {
                    eprintln!("ERROR: {}", beyond_range_message(&at_time.to_string()));
                    std::process::exit(2);
                })
        }
    } else if cfg.async_mode || cfg.at_time_is_cycle {
        unsigned_time
    } else {
        crate::format::nanoseconds_to_tick(unsigned_time, cache.timescale_exponent).unwrap_or_else(
            |error| {
                eprintln!("ERROR: {error}");
                std::process::exit(2);
            },
        )
    };
    if target_tick > cache.sim_end_tick {
        eprintln!("ERROR: {}", beyond_range_message(&at_time.to_string()));
        if sync_mode {
            let total_cycles = cache
                .sim_end_tick
                .saturating_sub(cycle_base(cache, effective_start))
                / cache.clock_period_ticks
                + 1;
            let start_ns = (effective_start as f64 * cache.ticks_to_ns) as i64;
            let end_ns = (cache.sim_end_tick as f64 * cache.ticks_to_ns) as i64;
            eprintln!("HINT: did you mean --at-time {at_time} with --async? In sync mode, --at-time expects a cycle number (0..{total_cycles}). Simulation time range: {start_ns}ns..{end_ns}ns");
        }
        std::process::exit(2);
    }

    if !cfg.json_format {
        for r in &cfg.clock_stopped {
            eprintln!(
                "# cycle {} is stopped (clk stopped {}:{})",
                at_time, r.from_cycle, r.to_cycle
            );
        }
    }
    let time_label = if sync_mode {
        if at_time == 0 {
            "cycle 0 (before first post-reset clock edge)".to_string()
        } else {
            format!("cycle {}", at_time)
        }
    } else {
        format!("{}", at_time)
    };

    let (radix_map, radix_warnings) = build_radix_map(cache, &matched, cfg);
    json_warnings.extend(radix_warnings);
    cache.prefetch_window(&matched, target_tick);
    let values: Vec<String> = matched
        .par_iter()
        .map(|&sig_idx| {
            cfg.sample_tick(target_tick)
                .map_or_else(|| "x".into(), |t| cache.value_at_tick_direct(sig_idx, t))
        })
        .collect();
    let virtual_values = evaluate_virtuals_at_tick(
        cache,
        &resolved_virtuals,
        cfg.sample_tick(target_tick).unwrap_or(0),
    );

    json_warnings.extend(record_limit(cfg, matched.len() + matched_virtuals.len()));
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
        let mut signals: Vec<crate::output::SignalValue> = matched
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
        signals.extend(matched_virtuals.iter().map(|&idx| {
            let entry = &virtual_values[idx];
            crate::output::SignalValue {
                name: entry.name.clone(),
                value: if cfg.sample_tick(target_tick).is_none() {
                    "x".into()
                } else {
                    entry.value.clone()
                },
            }
        }));
        signals.truncate(cfg.max_lines);
        emit_query_json(
            cfg,
            "value",
            crate::output::ValueData {
                scope_prefix,
                mode: mode.to_string(),
                at: at_time,
                at_unit: at_unit.to_string(),
                target_tick,
                sample_tick: cfg.sample_tick(target_tick),
                time_label,
                signals,
            },
            json_warnings,
        );
        return;
    }

    let _ = writeln!(out, "# Snapshot at {}", time_label);

    for (idx, &sig_idx) in matched.iter().take(cfg.max_lines).enumerate() {
        let sig = &cache.signals[sig_idx];
        let dname = if !prefix.is_empty() && sig.name.starts_with(&prefix) {
            &sig.name[prefix.len()..]
        } else {
            &sig.name
        };
        let dval = fmt_val(&values[idx], sig_idx, sig.width, &radix_map);
        let _ = writeln!(out, "{:<40} = {}", dname, dval);
    }
    for &idx in matched_virtuals
        .iter()
        .take(cfg.max_lines.saturating_sub(matched.len()))
    {
        let entry = &virtual_values[idx];
        let _ = writeln!(
            out,
            "{:<40} = {}",
            entry.name,
            if cfg.sample_tick(target_tick).is_none() {
                "x"
            } else {
                &entry.value
            }
        );
    }
    let _ = out.flush();
    let _ = err.flush();
}

// -- Resolve user time to simulation tick ----------------------------------

/// Convert a user-facing time value to a simulation tick.
/// In sync mode (is_cycle=true, clock present): time is a cycle number.
/// In async mode or no-clock fallback, native resolved points are raw ticks;
/// direct library points without string-token provenance retain nanoseconds.
/// Returns None if the time is out of simulation range.
fn resolve_time_to_tick(
    cache: &ColumnCache,
    cfg: &ExtractConfig,
    time: i64,
    effective_start: u64,
) -> Option<u64> {
    let time = u64::try_from(time).ok()?;
    let sync_mode = !cfg.async_mode && cache.clock_period_ticks > 0;
    let tick = if sync_mode {
        if time == 0 {
            effective_start
        } else {
            cycle_base(cache, effective_start)
                .checked_add((time - 1).checked_mul(cache.clock_period_ticks)?)?
        }
    } else if cfg.diff_strs.is_some() {
        time
    } else {
        crate::format::nanoseconds_to_tick(time, cache.timescale_exponent).ok()?
    };
    (tick <= cache.sim_end_tick).then_some(tick)
}

// -- Diff snapshot from cache ------------------------------------------------

pub fn diff_from_cache(cache: &ColumnCache, cfg: &ExtractConfig) {
    let resolved_cfg = query_config(cache, cfg);
    let cfg = &resolved_cfg;
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
            std::process::exit(2);
        }
    };
    let tick2 = match resolve_time_to_tick(cache, cfg, t2, effective_start) {
        Some(t) => t,
        None => {
            eprintln!("ERROR: --diff: time {} is beyond simulation range", t2);
            std::process::exit(2);
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

    let (radix_map, _) = build_radix_map(cache, &matched, cfg);
    cache.prefetch_window(&matched, tick1.max(tick2));
    let values: Vec<(String, String)> = matched
        .par_iter()
        .map(|&sig_idx| {
            let v1 = cfg
                .sample_tick(tick1)
                .map_or_else(|| "x".into(), |t| cache.value_at_tick_direct(sig_idx, t));
            let v2 = cfg
                .sample_tick(tick2)
                .map_or_else(|| "x".into(), |t| cache.value_at_tick_direct(sig_idx, t));
            (v1, v2)
        })
        .collect();

    let mut diff_count = 0usize;
    for (idx, &sig_idx) in matched.iter().enumerate() {
        let (ref v1, ref v2) = values[idx];
        if v1 != v2 {
            if diff_count >= cfg.max_lines {
                eprintln!("{}", limit_warning(cfg.max_lines));
                break;
            }
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
    /// Inclusive sampling-cycle runs for synchronous queries.
    Runs(CycleRuns),
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
        Err(error) => {
            eprintln!("ERROR: {error}");
            std::process::exit(2);
        }
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

    let mode = TriggerMode::classify(value_str);
    // Directional keywords require 1-bit signals; Change also accepts buses.
    // Track all-multibit matches for the existing directional diagnostic.
    let edge_on_multibit_only = mode.directional()
        && virtual_matches.is_empty()
        && target_indices.iter().all(|&i| cache.signals[i].width > 1);
    let mut ticks = Vec::new();
    let mut runs = Vec::new();

    for &sig_idx in &target_indices {
        let transitions = cache.read_transitions(sig_idx);
        if sync_mode && cache.clock_period_ticks > 0 {
            runs.extend(sampled_event_runs(
                cache,
                cfg,
                cb,
                &transitions,
                cache.signals[sig_idx].width,
                value_str,
            ));
            continue;
        }
        let mut prev_val: Option<&str> = None;

        for (tick, val) in &transitions {
            if *tick < effective_start {
                prev_val = Some(val.as_str());
                continue;
            }

            // Time range filtering
            if sync_mode && cache.clock_period_ticks > 0 && (cb > 0 || mode == TriggerMode::Change)
            {
                let cycle = tick_to_cycle(*tick, cb, cache.clock_period_ticks);
                if (cycle as i64) < cfg.time_min {
                    prev_val = Some(val.as_str());
                    continue;
                }
                if let Some(max) = cfg.time_max {
                    if (cycle as i64) > max {
                        break;
                    }
                }
            } else if !sync_mode {
                if (*tick as i64) < cfg.time_min {
                    prev_val = Some(val.as_str());
                    continue;
                }
                if let Some(max) = cfg.time_max {
                    if (*tick as i64) > max {
                        break;
                    }
                }
            }

            let matched = mode.matches(prev_val, val, cache.signals[sig_idx].width, value_str);
            prev_val = Some(val.as_str());

            if matched {
                ticks.push(*tick);
            }
        }
    }
    for ve in virtual_matches {
        if sync_mode && cache.clock_period_ticks > 0 {
            runs.extend(sampled_event_runs(
                cache,
                cfg,
                cb,
                &ve.transitions,
                1,
                value_str,
            ));
            continue;
        }
        let mut prev_val: Option<&str> = None;

        for (tick, val) in &ve.transitions {
            if *tick < effective_start {
                prev_val = Some(val.as_str());
                continue;
            }

            // Time range filtering
            if sync_mode && cache.clock_period_ticks > 0 && (cb > 0 || mode == TriggerMode::Change)
            {
                let cycle = tick_to_cycle(*tick, cb, cache.clock_period_ticks);
                if (cycle as i64) < cfg.time_min {
                    prev_val = Some(val.as_str());
                    continue;
                }
                if let Some(max) = cfg.time_max {
                    if (cycle as i64) > max {
                        break;
                    }
                }
            } else if !sync_mode {
                if (*tick as i64) < cfg.time_min {
                    prev_val = Some(val.as_str());
                    continue;
                }
                if let Some(max) = cfg.time_max {
                    if (*tick as i64) > max {
                        break;
                    }
                }
            }

            let matched = mode.matches(prev_val, val, 1, value_str);
            prev_val = Some(val.as_str());

            if matched {
                ticks.push(*tick);
            }
        }
    }
    if sync_mode && cache.clock_period_ticks > 0 {
        let runs = union_cycle_runs(runs);
        return if runs.is_empty() {
            EventCollect::NoEvents {
                multi_bit_edge: edge_on_multibit_only,
            }
        } else {
            EventCollect::Runs(runs)
        };
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

/// Inclusive runs of matching sampling cycles. Their size follows raw
/// transitions, including when a literal holds for billions of cycles.
type CycleRuns = Vec<(u64, u64)>;

fn sampled_event_runs(
    cache: &ColumnCache,
    cfg: &ExtractConfig,
    base: u64,
    transitions: &[(u64, String)],
    width: u32,
    value: &str,
) -> CycleRuns {
    let period = cache.clock_period_ticks;
    let (start, end) = sampled_cycle_window(cache, cfg, base);
    if start > end {
        return Vec::new();
    }
    let boundaries = observation_boundaries(cfg, base, period, transitions, start, end);
    let mut previous =
        (start > 1).then(|| sampled_value(cfg, transitions, base + (start - 2) * period));
    let mode = TriggerMode::classify(value);
    let mut runs = Vec::new();
    for (i, &lo) in boundaries.iter().enumerate() {
        let hi = boundaries.get(i + 1).map_or(end, |next| next - 1);
        let current = sampled_value(cfg, transitions, base + (lo - 1) * period);
        if mode.matches(previous.as_deref(), &current, width, value) {
            runs.push((lo, if mode == TriggerMode::Literal { hi } else { lo }));
        }
        previous = Some(current);
    }
    let runs = union_cycle_runs(runs);
    if mode == TriggerMode::Literal
        && !cfg.include_stopped
        && (cfg.find_pattern.is_some() || cfg.sample_at_pattern.is_some())
    {
        crate::clock::exclude_stopped(&runs, &cfg.clock_stopped)
    } else {
        runs
    }
}

fn union_cycle_runs(mut runs: CycleRuns) -> CycleRuns {
    runs.sort_unstable();
    let mut union: CycleRuns = Vec::new();
    for (lo, hi) in runs {
        if let Some(last) = union.last_mut() {
            if lo <= last.1.saturating_add(1) {
                last.1 = last.1.max(hi);
                continue;
            }
        }
        union.push((lo, hi));
    }
    union
}

/// Select only records required by find's signal-ordered renderer, while
/// accounting for suppressed records in count/last modes without expansion.
fn find_run_selection(
    runs: &[(u64, u64)],
    cfg: &ExtractConfig,
    line_count: usize,
) -> (usize, Vec<u64>) {
    if cfg.first_match {
        return (0, runs.first().map(|r| r.0).into_iter().collect());
    }
    if cfg.last_match {
        let count = run_count(runs);
        return (
            usize::try_from(count.saturating_sub(1)).expect("find count exceeds platform size"),
            runs.last().map(|r| r.1).into_iter().collect(),
        );
    }
    if cfg.count_only {
        return (
            usize::try_from(run_count(runs)).expect("find count exceeds platform size"),
            Vec::new(),
        );
    }
    (
        0,
        runs.iter()
            .flat_map(|&(lo, hi)| lo..=hi)
            .take(cfg.max_lines.saturating_sub(line_count).saturating_add(1))
            .collect(),
    )
}

fn run_count(runs: &[(u64, u64)]) -> u64 {
    runs.iter().fold(0u64, |count, &(lo, hi)| {
        count
            .checked_add(hi - lo + 1)
            .expect("sampled event count overflow")
    })
}

/// Consecutive A cycles map either to consecutive B cycles or one fixed B.
#[derive(Clone, Copy, Debug)]
struct PairSegment {
    a: u64,
    b: u64,
    count: u64,
    consecutive_b: bool,
}

fn sampled_pair_segments(a: &[(u64, u64)], b: Option<&[(u64, u64)]>) -> Vec<PairSegment> {
    let mut segments = Vec::new();
    if let Some(b) = b {
        let mut index = 0;
        for &(lo, hi) in a {
            let mut cursor = lo;
            while cursor <= hi {
                while index < b.len() && b[index].1 <= cursor {
                    index += 1;
                }
                let Some(&(b_lo, b_hi)) = b.get(index) else {
                    break;
                };
                let consecutive_b = cursor >= b_lo;
                let stop = hi.min(if consecutive_b { b_hi - 1 } else { b_lo - 1 });
                segments.push(PairSegment {
                    a: cursor,
                    b: if consecutive_b { cursor + 1 } else { b_lo },
                    count: stop - cursor + 1,
                    consecutive_b,
                });
                if stop == hi {
                    break;
                }
                cursor = stop + 1;
            }
        }
    } else {
        let mut previous = None;
        for &(lo, hi) in a {
            if let Some(prev) = previous {
                segments.push(PairSegment {
                    a: prev,
                    b: lo,
                    count: 1,
                    consecutive_b: false,
                });
            }
            if hi > lo {
                segments.push(PairSegment {
                    a: lo,
                    b: lo + 1,
                    count: hi - lo,
                    consecutive_b: true,
                });
            }
            previous = Some(hi);
        }
    }
    segments
}

impl PairSegment {
    fn delta_bounds(self) -> (u64, u64) {
        let high = self.b - self.a;
        (
            if self.consecutive_b {
                high
            } else {
                high - (self.count - 1)
            },
            high,
        )
    }
    fn sum(self) -> u128 {
        let (low, high) = self.delta_bounds();
        let n = u128::from(self.count);
        if self.consecutive_b {
            return n
                .checked_mul(u128::from(high))
                .expect("distance sum overflow");
        }
        let endpoints = u128::from(low) + u128::from(high);
        let (left, right) = if n % 2 == 0 {
            (n / 2, endpoints)
        } else {
            (n, endpoints / 2)
        };
        left.checked_mul(right).expect("distance sum overflow")
    }
    fn rank(self, value: u64) -> u64 {
        let (low, high) = self.delta_bounds();
        if value < low {
            0
        } else if self.consecutive_b || value >= high {
            self.count
        } else {
            value - low + 1
        }
    }
}

fn pair_summary(segments: &[PairSegment]) -> String {
    let count = segments.iter().fold(0u64, |n, s| {
        n.checked_add(s.count).expect("distance count overflow")
    });
    let sum = segments.iter().fold(0u128, |n, s| {
        n.checked_add(s.sum()).expect("distance sum overflow")
    });
    let min = segments.iter().map(|s| s.delta_bounds().0).min().unwrap();
    let max = segments.iter().map(|s| s.delta_bounds().1).max().unwrap();
    let kth = |rank| {
        let (mut lo, mut hi) = (min, max);
        while lo < hi {
            let mid = lo + (hi - lo) / 2;
            let below = segments.iter().fold(0u64, |n, s| {
                n.checked_add(s.rank(mid)).expect("distance rank overflow")
            });
            if below >= rank {
                hi = mid;
            } else {
                lo = mid + 1;
            }
        }
        lo
    };
    let median = if count % 2 == 0 {
        (u128::from(kth(count / 2)) + u128::from(kth(count / 2 + 1))) as f64 / 2.0
    } else {
        kth(count / 2 + 1) as f64
    };
    let mean = sum as f64 / count as f64;
    format!("count={count}  min={min}  max={max}  mean={mean:.1}  median={median:.1}")
}

fn stopped_pair_sum(segments: &[PairSegment], stops: &[crate::clock::StoppedRange]) -> u128 {
    // Sum clamped stopped-prefix counts over a consecutive sequence in O(1).
    let sum_prefix = |first: u64, count: u64, r: &crate::clock::StoppedRange| {
        let len = u128::from(r.to_cycle - r.from_cycle + 1);
        let start = u128::from(first);
        let end = start + u128::from(count);
        let ramp_start = start.max(u128::from(r.from_cycle));
        let ramp_end = end.min(u128::from(r.to_cycle) + 1);
        let ramp = if ramp_start < ramp_end {
            let n = ramp_end - ramp_start;
            n * ((ramp_start - u128::from(r.from_cycle) + 1)
                + (ramp_end - u128::from(r.from_cycle)))
                / 2
        } else {
            0
        };
        ramp + end.saturating_sub(start.max(u128::from(r.to_cycle) + 1)) * len
    };
    segments
        .iter()
        .map(|segment| {
            stops
                .iter()
                .map(|r| {
                    let b = if segment.consecutive_b {
                        sum_prefix(segment.b, segment.count, r)
                    } else {
                        u128::from(crate::clock::stopped_count(
                            std::slice::from_ref(r),
                            0,
                            segment.b,
                        )) * u128::from(segment.count)
                    };
                    b - sum_prefix(segment.a, segment.count, r)
                })
                .sum::<u128>()
        })
        .sum()
}

fn emit_empty_distance(cfg: &ExtractConfig, unit: &str) {
    if cfg.json_format {
        emit_query_json(
            cfg,
            "distance",
            crate::output::DistanceData {
                unit: unit.into(),
                count: 0,
                pairs: Vec::new(),
                summary: None,
                stopped_cycles: 0,
                truncated: false,
            },
            Vec::new(),
        );
    }
}

fn distance_from_runs(
    cache: &ColumnCache,
    cfg: &ExtractConfig,
    base: u64,
    a: CycleRuns,
    virtuals: &[VirtualEntry],
) {
    let (pat_a, val_a) = cfg.distance_a.as_ref().unwrap();
    let b = if let Some((pattern, value)) = &cfg.distance_b {
        match collect_event_ticks(cache, pattern, value, base, true, cfg, base, virtuals) {
            EventCollect::Runs(runs) => Some(runs),
            EventCollect::NoPatternMatch => {
                exit_no_signal_match(std::slice::from_ref(pattern), cache.unique_signal_count())
            }
            EventCollect::NoEvents { .. } => {
                emit_empty_distance(cfg, "cycles");
                if !cfg.json_format {
                    eprintln!("# distance: no events found for {pattern} {value}");
                }
                return;
            }
            EventCollect::Ticks(_) => unreachable!("sync distance uses runs"),
        }
    } else {
        None
    };
    let segments = sampled_pair_segments(&a, b.as_deref());
    let count = segments.iter().map(|s| s.count).sum::<u64>();
    let stopped_cycles = stopped_pair_sum(&segments, &cfg.clock_stopped);
    let summary = (count > 0).then(|| pair_summary(&segments));
    let mut pairs = Vec::new();
    if !cfg.stats_mode {
        for segment in &segments {
            for offset in 0..segment.count.min((cfg.max_lines - pairs.len()) as u64) {
                let from = segment.a + offset;
                let to = segment.b + if segment.consecutive_b { offset } else { 0 };
                pairs.push(crate::output::DistancePair {
                    from,
                    to,
                    elapsed: to - from,
                    stopped_cycles: crate::clock::stopped_count(&cfg.clock_stopped, from + 1, to),
                });
            }
            if pairs.len() == cfg.max_lines {
                break;
            }
        }
    }
    let truncated = !cfg.stats_mode && count > cfg.max_lines as u64;
    let warnings = if truncated {
        record_limit(cfg, cfg.max_lines + 1)
    } else {
        Vec::new()
    };
    if cfg.json_format {
        emit_query_json(
            cfg,
            "distance",
            crate::output::DistanceData {
                unit: "cycles".into(),
                count,
                pairs,
                summary,
                stopped_cycles,
                truncated,
            },
            warnings,
        );
        return;
    }
    let stdout = io::stdout();
    let mut out = BufWriter::new(stdout.lock());
    if let Some((pat, value)) = &cfg.distance_b {
        let _ = writeln!(out, "# A: {} {} -> B: {} {}", pat_a, val_a, pat, value);
    } else {
        let _ = writeln!(out, "# same-signal: {} {}", pat_a, val_a);
    }
    if count == 0 {
        if b.is_some() {
            let _ = writeln!(out, "# no pairs found");
        } else {
            let _ = writeln!(
                out,
                "# no pairs found (need at least 2 events, found {})",
                run_count(&a)
            );
        }
    } else if cfg.stats_mode {
        let note = if stopped_cycles > 0 {
            format!("  stopped_cycles={stopped_cycles}")
        } else {
            String::new()
        };
        let _ = writeln!(out, "{}{}", summary.unwrap(), note);
    } else {
        for pair in pairs {
            let note = if pair.stopped_cycles > 0 {
                format!(" cycles ({} stopped)", pair.stopped_cycles)
            } else {
                String::new()
            };
            let _ = writeln!(
                out,
                "@ {} -> @ {}  d={}{}",
                pair.from, pair.to, pair.elapsed, note
            );
        }
        eprintln!("# {} pairs, unit: cycles", count);
    }
    let _ = out.flush();
}

fn distance_summary(deltas: &[u64]) -> String {
    let mut sorted = deltas.to_vec();
    sorted.sort_unstable();
    let count = sorted.len();
    let mean = sorted.iter().map(|&v| u128::from(v)).sum::<u128>() as f64 / count as f64;
    let middle = count / 2;
    let median = if count % 2 == 0 {
        (u128::from(sorted[middle - 1]) + u128::from(sorted[middle])) as f64 / 2.0
    } else {
        sorted[middle] as f64
    };
    format!(
        "count={count}  min={}  max={}  mean={mean:.1}  median={median:.1}",
        sorted[0],
        sorted[count - 1]
    )
}

pub fn distance_from_cache(cache: &ColumnCache, cfg: &ExtractConfig) {
    let resolved_cfg = query_config(cache, cfg);
    let cfg = &resolved_cfg;
    let (ref pat_a, ref val_a) = match cfg.distance_a {
        Some(ref a) => a,
        None => return,
    };

    let sync_mode = !cfg.async_mode && cache.clock_period_ticks > 0;

    let change_query = TriggerMode::classify(val_a) == TriggerMode::Change
        || cfg
            .distance_b
            .as_ref()
            .is_some_and(|(_, value)| TriggerMode::classify(value) == TriggerMode::Change);
    let reset_deassert_tick = if !cfg.with_reset && (sync_mode || (change_query && !cfg.async_mode))
    {
        find_reset_deassert_tick(cache, cfg)
    } else {
        None
    };
    let effective_start = reset_deassert_tick.unwrap_or(0);
    let event_start = |value: &str| {
        if sync_mode || TriggerMode::classify(value) == TriggerMode::Change {
            effective_start
        } else {
            0
        }
    };
    let cb = cycle_base(cache, effective_start);
    let virtuals = build_virtuals(cache, cfg);

    // Resolve event A; report `no signals match` vs `no events found` distinctly
    // so the caller can tell whether their pattern was the problem.
    let ticks_a = match collect_event_ticks(
        cache,
        pat_a,
        val_a,
        event_start(val_a),
        sync_mode,
        cfg,
        cb,
        &virtuals,
    ) {
        EventCollect::Runs(runs) => {
            distance_from_runs(cache, cfg, cb, runs, &virtuals);
            return;
        }
        EventCollect::Ticks(t) => t,
        EventCollect::NoPatternMatch => {
            exit_no_signal_match(std::slice::from_ref(pat_a), cache.unique_signal_count());
        }
        EventCollect::NoEvents { multi_bit_edge } => {
            emit_empty_distance(cfg, if sync_mode { "cycles" } else { "ticks" });
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

    if cfg.json_format {
        let b = cfg.distance_b.as_ref().map(|(pattern, value)| {
            match collect_event_ticks(
                cache,
                pattern,
                value,
                event_start(value),
                false,
                cfg,
                cb,
                &virtuals,
            ) {
                EventCollect::Ticks(t) => t,
                EventCollect::NoPatternMatch => {
                    exit_no_signal_match(std::slice::from_ref(pattern), cache.unique_signal_count())
                }
                _ => Vec::new(),
            }
        });
        let pairs: Vec<_> = if let Some(b) = b {
            ticks_a
                .iter()
                .filter_map(|&a| b.get(b.partition_point(|&t| t <= a)).map(|&b| (a, b)))
                .collect()
        } else {
            ticks_a.windows(2).map(|w| (w[0], w[1])).collect()
        };
        let deltas: Vec<_> = pairs.iter().map(|(a, b)| b - a).collect();
        let count = pairs.len() as u64;
        let summary = (!deltas.is_empty()).then(|| distance_summary(&deltas));
        let truncated = !cfg.stats_mode && pairs.len() > cfg.max_lines;
        let warnings = if truncated {
            record_limit(cfg, pairs.len())
        } else {
            Vec::new()
        };
        let pairs = if cfg.stats_mode {
            Vec::new()
        } else {
            pairs
                .into_iter()
                .take(cfg.max_lines)
                .map(|(from, to)| crate::output::DistancePair {
                    from,
                    to,
                    elapsed: to - from,
                    stopped_cycles: 0,
                })
                .collect()
        };
        emit_query_json(
            cfg,
            "distance",
            crate::output::DistanceData {
                unit: "ticks".into(),
                count,
                pairs,
                summary,
                stopped_cycles: 0,
                truncated,
            },
            warnings,
        );
        return;
    }
    let stdout = io::stdout();
    let mut out = BufWriter::new(stdout.lock());
    let stderr = io::stderr();
    let mut err = BufWriter::new(stderr.lock());

    let tick_to_display = |tick: u64| -> String {
        if sync_mode && cache.clock_period_ticks > 0 {
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
            event_start(val_b),
            sync_mode,
            cfg,
            cb,
            &virtuals,
        ) {
            EventCollect::Runs(_) => {
                unreachable!("synchronous distance handled by compressed runs")
            }
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

            if !cfg.stats_mode && deltas.len() <= cfg.max_lines {
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
            let _ = writeln!(out, "{}", distance_summary(&deltas));
        } else {
            record_limit(cfg, deltas.len());
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

                if !cfg.stats_mode && deltas.len() <= cfg.max_lines {
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
                let _ = writeln!(out, "{}", distance_summary(&deltas));
            } else {
                record_limit(cfg, deltas.len());
                let _ = writeln!(err, "# {} pairs, unit: {}", deltas.len(), unit);
            }
        }
    }

    let _ = out.flush();
    let _ = err.flush();
}

// -- Cycle trace (default mode) from cache --------------------------------

pub fn trace_from_cache(cache: &ColumnCache, original: &ExtractConfig) {
    let cfg = query_config(cache, original);
    let matched = cache.match_signals(&cfg.patterns);
    if matched.is_empty() {
        exit_no_signal_match(&cfg.patterns, cache.unique_signal_count());
    }
    let (radixes, mut warnings) = build_radix_map(cache, &matched, &cfg);
    let names: Vec<_> = matched
        .iter()
        .map(|&i| cache.signals[i].name.clone())
        .collect();
    let prefix = crate::signal::common_scope_prefix(&names);
    if !cfg.json_format && !prefix.is_empty() {
        eprintln!("# scope: {}", &prefix[..prefix.len() - 1]);
    }
    let sync = !cfg.async_mode && cache.clock_period_ticks > 0;
    let reset = query_reset_deassert_tick(cache, &cfg).unwrap_or(0);
    let base = cycle_base(cache, reset);
    let (start, end) = sampled_cycle_window(cache, &cfg, base);
    let bound = if sync {
        base.saturating_add(
            end.saturating_sub(1)
                .saturating_mul(cache.clock_period_ticks),
        )
    } else {
        cfg.time_max.map_or(cache.sim_end_tick, |t| t.max(0) as u64)
    };
    cache.prefetch_window(&matched, bound);
    let data: Vec<_> = matched
        .iter()
        .map(|&i| cache.read_transitions_range(i, 0, bound).1)
        .collect();
    let mut positions = std::collections::BTreeSet::new();
    if sync && start <= end {
        for transitions in &data {
            positions.extend(observation_boundaries(
                &cfg,
                base,
                cache.clock_period_ticks,
                transitions,
                start,
                end,
            ));
        }
        for stop in &cfg.clock_stopped {
            positions.insert(start.max(stop.from_cycle));
        }
    } else if !sync {
        for transitions in &data {
            positions.extend(transitions.iter().map(|(t, _)| *t).filter(|&t| {
                (t as i128) >= i128::from(cfg.time_min)
                    && cfg
                        .time_max
                        .is_none_or(|end| (t as i128) <= i128::from(end))
            }));
        }
    }
    let mut previous: Vec<_> = if sync && start > 1 {
        data.iter()
            .map(|t| {
                Some(sampled_value(
                    &cfg,
                    t,
                    base + (start - 2) * cache.clock_period_ticks,
                ))
            })
            .collect()
    } else {
        vec![None; matched.len()]
    };
    let mut records = Vec::new();
    let mut value_rows = 0;
    for position in positions {
        if sync {
            for r in &cfg.clock_stopped {
                if position == start.max(r.from_cycle) {
                    records.push(crate::output::SignalRecord::ClockStopped {
                        from_cycle: r.from_cycle,
                        to_cycle: r.to_cycle,
                    });
                }
            }
        }
        let tick = if sync {
            base + (position - 1) * cache.clock_period_ticks
        } else {
            position
        };
        for (index, &signal) in matched.iter().enumerate() {
            let value = sampled_value(&cfg, &data[index], tick);
            if !sync || previous[index].as_ref() != Some(&value) {
                records.push(crate::output::SignalRecord::Value {
                    time: position,
                    name: cache.signals[signal]
                        .name
                        .strip_prefix(&prefix)
                        .unwrap_or(&cache.signals[signal].name)
                        .into(),
                    value: fmt_val(&value, signal, cache.signals[signal].width, &radixes),
                });
                value_rows += 1;
            }
            previous[index] = Some(value);
        }
        if records.len() > cfg.max_lines {
            break;
        }
    }
    if value_rows == 0 && (!sync || start <= end) {
        let position = if sync {
            start
        } else {
            cfg.time_min.max(0) as u64
        };
        let tick = if sync {
            base + (position - 1) * cache.clock_period_ticks
        } else {
            position
        };
        let mut held: Vec<_> = matched
            .iter()
            .enumerate()
            .take(cfg.max_lines + 1)
            .map(|(index, &signal)| {
                let value = sampled_value(&cfg, &data[index], tick);
                crate::output::SignalRecord::Value {
                    time: position,
                    name: cache.signals[signal]
                        .name
                        .strip_prefix(&prefix)
                        .unwrap_or(&cache.signals[signal].name)
                        .into(),
                    value: fmt_val(&value, signal, cache.signals[signal].width, &radixes),
                }
            })
            .collect();
        held.append(&mut records);
        records = held;
        if !cfg.json_format {
            eprintln!(
                "# no transitions in range — showing held values at {} {}",
                if sync { "cycle" } else { "time" },
                position
            );
        }
    }
    let truncated = records.len() > cfg.max_lines;
    warnings.extend(record_limit(&cfg, records.len()));
    records.truncate(cfg.max_lines);
    if cfg.json_format {
        emit_query_json(
            &cfg,
            "signal",
            crate::output::SignalData {
                mode: if sync { "sync" } else { "async" }.into(),
                records,
                truncated,
            },
            warnings,
        );
    } else {
        let stdout = io::stdout();
        let mut out = BufWriter::new(stdout.lock());
        if sync {
            eprintln!(
                "# sync: period={}ns",
                (cache.clock_period_ticks as f64 * cache.ticks_to_ns) as i64
            );
        }
        for record in records {
            match record {
                crate::output::SignalRecord::Value { time, name, value } => {
                    let _ = writeln!(out, "{} {} {}", time, name, value);
                }
                crate::output::SignalRecord::ClockStopped {
                    from_cycle,
                    to_cycle,
                } => {
                    let _ = writeln!(out, "# clk stopped {}:{}", from_cycle, to_cycle);
                }
            }
        }
        let _ = out.flush();
    }
}

// -- List signals from cache ----------------------------------------------

pub fn list_signals_from_cache(
    cache: &ColumnCache,
    patterns: &[String],
    tree_only: bool,
    json_format: bool,
    limit: usize,
    inherited_warnings: &[String],
) {
    let clock_cfg = clock_query_config(
        cache,
        &ExtractConfig {
            json_format,
            with_reset: true,
            ..Default::default()
        },
    );
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
    let root_scopes = crate::signal::top_scopes(
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
        let signal_count = stripped.len();
        let signals: Vec<crate::output::SignalEntry> = stripped
            .iter()
            .take(limit)
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
        let total_ticks = cache.sim_end_tick;
        // An empty *store* (not an empty match) rides along as a warning so
        // JSON consumers can tell "bad pattern" from "bad trace". It ALSO
        // goes to stderr like the text path does — stderr is free in JSON
        // mode, and a consumer that only scans stderr for ERROR: must not
        // see an empty store as a clean run.
        let mut warnings = if cache.signals.is_empty() {
            eprintln!("ERROR: {}", no_signals_in_store_message());
            vec![no_signals_in_store_message().to_string()]
        } else {
            Vec::new()
        };
        let hits: Vec<bool> = matchers
            .iter()
            .map(|matcher| {
                cache
                    .signals
                    .iter()
                    .any(|signal| match_signal(&signal.name, std::slice::from_ref(matcher)))
            })
            .collect();
        warnings.extend(unmatched_pattern_warnings(
            patterns,
            &hits,
            matched.is_empty(),
        ));
        if stripped.len() > limit {
            warnings.push(limit_warning(limit));
        }
        let warnings = crate::output::merge_warnings(warnings, inherited_warnings);
        crate::output::emit_clock_json(
            "list",
            crate::output::ListData {
                scope_prefix,
                root_scopes,
                signal_count,
                clock,
                total_ticks,
                signals,
            },
            crate::output::merge_warnings(warnings, &clock_cfg.warnings),
            clock_cfg.clock_regularity,
            Vec::new(),
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

pub(crate) fn tick_to_cycle(tick: u64, cycle_base_tick: u64, clock_period: u64) -> u64 {
    if clock_period == 0 || tick < cycle_base_tick {
        return 0;
    }
    (tick - cycle_base_tick) / clock_period + 1
}

/// Compute the first clock-grid tick at or after effective_start.
/// Cycle 1 starts there; later cycles advance by the detected clock period.
fn checked_cycle_base(first_rise: u64, period: u64, effective_start: u64) -> Result<u64, String> {
    if period == 0 {
        return Ok(effective_start);
    }
    if effective_start <= first_rise {
        return Ok(first_rise);
    }
    let elapsed = effective_start - first_rise;
    let aligned = first_rise + (elapsed / period) * period;
    if aligned >= effective_start {
        Ok(aligned)
    } else {
        aligned
            .checked_add(period)
            .ok_or_else(|| "cycle base is beyond simulation tick range".to_string())
    }
}

fn cycle_base(cache: &ColumnCache, effective_start: u64) -> u64 {
    // An unrepresentable next edge lies beyond every representable query tick.
    // Saturation keeps existing query consumers from wrapping into early time;
    // typed-token resolution reports this condition as an input error instead.
    checked_cycle_base(
        cache.first_rise_tick,
        cache.clock_period_ticks,
        effective_start,
    )
    .unwrap_or(u64::MAX)
}

/// Resolve typed query times using the same counted clock edges as sync queries.
/// Async cycle tokens deliberately use the sync reset policy, while async ticks
/// and physical times remain raw simulation instants.
pub fn resolve_time_token(
    cache: &ColumnCache,
    cfg: &ExtractConfig,
    token: &crate::format::TimeToken,
) -> Result<i64, String> {
    use crate::format::TimeToken;
    if let TimeToken::Cycle(n) = *token {
        if !cfg.async_mode {
            return Ok(n);
        }
        if n < 0 {
            return Err("cycle-token must be nonnegative".to_string());
        }
        if cache.clock_period_ticks == 0 {
            return Err("cannot resolve cycle-token to tick: no clock detected".to_string());
        }
    } else if cfg.async_mode {
        return token.resolve_to_tick(cache.timescale_exponent);
    } else if cache.clock_period_ticks == 0 {
        return Err("cannot resolve time-token to cycle: no clock detected".to_string());
    }
    let effective_start = if cfg.with_reset {
        0
    } else {
        find_reset_deassert_tick(cache, cfg).unwrap_or(0)
    };
    if let TimeToken::Cycle(0) = *token {
        return i64::try_from(effective_start)
            .map_err(|_| "cycle time is beyond signed tick range".to_string());
    }
    let base = checked_cycle_base(
        cache.first_rise_tick,
        cache.clock_period_ticks,
        effective_start,
    )?;
    match *token {
        TimeToken::Cycle(n) => {
            let tick = (n as u64 - 1)
                .checked_mul(cache.clock_period_ticks)
                .and_then(|offset| base.checked_add(offset))
                .ok_or_else(|| "cycle time is beyond simulation tick range".to_string())?;
            i64::try_from(tick).map_err(|_| "cycle time is beyond signed tick range".to_string())
        }
        _ => {
            let tick = token.resolve_to_tick(cache.timescale_exponent)?;
            if tick < 0 {
                return Ok(0);
            }
            i64::try_from(tick_to_cycle(tick as u64, base, cache.clock_period_ticks))
                .map_err(|_| "cycle number is beyond signed range".to_string())
        }
    }
}

fn sampled_cycle_window(cache: &ColumnCache, cfg: &ExtractConfig, base: u64) -> (u64, u64) {
    let period = cache.clock_period_ticks;
    let total = if period == 0 || base > cache.sim_end_tick {
        0
    } else {
        ((cache.sim_end_tick - base) / period)
            .checked_add(1)
            .expect("sampled cycle count overflow")
    };
    let start = u64::try_from(cfg.time_min).unwrap_or(0).max(1);
    let end = cfg
        .time_max
        .map_or(total, |max| u64::try_from(max).unwrap_or(0).min(total));
    (start, end)
}

/// Lazily compare observations on the eligible clock grid, within the query
/// window. A late window needs only its immediately preceding grid sample;
/// the first eligible edge after reset has no predecessor.
#[cfg(test)]
fn sampled_events<'a>(
    cache: &ColumnCache,
    cfg: &ExtractConfig,
    base: u64,
    transitions: &'a [(u64, String)],
    width: u32,
    value: &'a str,
) -> impl Iterator<Item = (u64, String, bool)> + 'a {
    let period = cache.clock_period_ticks;
    let (start, end) = sampled_cycle_window(cache, cfg, base);
    let mut previous = if start > 1 && start <= end {
        Some(value_at_tick(transitions, base + (start - 2) * period))
    } else {
        None
    };
    let mode = TriggerMode::classify(value);
    (start..=end).map(move |cycle| {
        let tick = base + (cycle - 1) * period;
        let current = value_at_tick(transitions, tick);
        let matched = mode.matches(previous.as_deref(), &current, width, value);
        previous = Some(current.clone());
        (tick, current, matched)
    })
}

/// Binary search for the value of a signal at a given tick.
/// Returns the most recent value at or before tick.
fn value_at_tick(transitions: &[(u64, String)], tick: u64) -> String {
    if transitions.is_empty() {
        return "x".to_string();
    }
    let end = transitions.partition_point(|(t, _)| *t <= tick);
    if end == 0 {
        "x".to_string()
    } else {
        transitions[end - 1].1.clone()
    }
}

/// Find the best reset signal index.
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

fn wave_column_position(sync_mode: bool, header: &str, tick: u64) -> Option<i64> {
    if sync_mode {
        header.parse::<i64>().ok()
    } else {
        i64::try_from(tick).ok()
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
        let marker_positions: std::collections::HashSet<i64> =
            markers.iter().map(|(_, c)| *c).collect();
        for j in 1..n_cols {
            let same = grid.iter().all(|row| row[j] == row[j - 1]);
            let is_marker_col = wave_column_position(sync_mode, &col_headers[j], col_ticks[j])
                .map(|position| marker_positions.contains(&position))
                .unwrap_or(false);
            if same
                && !is_marker_col
                && !col_headers[j].starts_with("clk stopped ")
                && !col_headers[j - 1].starts_with("clk stopped ")
            {
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
            .zip(col_ticks.iter())
            .map(|(header, tick)| {
                let Some(position) = wave_column_position(sync_mode, header, *tick) else {
                    return String::new();
                };
                markers
                    .iter()
                    .filter(|(_, marker_position)| *marker_position == position)
                    .map(|(name, _)| name.as_str())
                    .collect::<Vec<_>>()
                    .join(",")
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
    let resolved_cfg = query_config(cache, cfg);
    let cfg = &resolved_cfg;
    if !cfg.wave_mode {
        return;
    }
    let range_start = cfg.time_min;
    let range_end_opt = cfg.time_max;

    // Resolve every virtual so later definitions can depend on unselected
    // helpers, then apply the same row-selection rule as value and sample.
    let all_virtuals = build_virtuals(cache, cfg);
    let virtual_names: Vec<String> = all_virtuals
        .iter()
        .map(|entry| entry.name.clone())
        .collect();
    let (matched, selected_virtual_indices) =
        cache.match_signals_with_names(&cfg.patterns, &virtual_names);
    if matched.is_empty() && selected_virtual_indices.is_empty() {
        exit_no_signal_match(&cfg.patterns, cache.unique_signal_count());
    }
    let selected_virtuals: Vec<&VirtualEntry> = selected_virtual_indices
        .iter()
        .map(|&idx| &all_virtuals[idx])
        .collect();
    let (radix_map, _) = build_radix_map(cache, &matched, cfg);

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
        let mut c = cycle_start;
        while c <= cycle_end && col_headers.len() < cfg.max_lines {
            if let Some(stop) = cfg
                .clock_stopped
                .iter()
                .find(|r| r.from_cycle <= c && c <= r.to_cycle)
            {
                let marker_names: Vec<_> = cfg
                    .markers
                    .iter()
                    .filter(|(_, m)| *m >= stop.from_cycle as i64 && *m <= stop.to_cycle as i64)
                    .map(|(n, _)| n.as_str())
                    .collect();
                col_headers.push(format!(
                    "clk stopped {}:{}{}",
                    stop.from_cycle,
                    stop.to_cycle,
                    if marker_names.is_empty() {
                        String::new()
                    } else {
                        format!(" [{}]", marker_names.join(","))
                    }
                ));
                col_ticks.push(cb + (c - 1) * cache.clock_period_ticks);
                c = stop.to_cycle.saturating_add(1);
            } else {
                col_headers.push(c.to_string());
                col_ticks.push(if c == 0 {
                    effective_start
                } else {
                    cb + (c - 1) * cache.clock_period_ticks
                });
                c += 1;
            }
        }
        record_limit(cfg, col_headers.len() + usize::from(c <= cycle_end));

        if col_ticks.is_empty() {
            let _ = writeln!(err, "# No data in range");
            let _ = err.flush();
            return;
        }

        let tick_min = cfg.sample_tick(col_ticks[0]).unwrap_or(0);
        let tick_max = cfg.sample_tick(*col_ticks.last().unwrap()).unwrap_or(0);

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
                    let Some(t) = cfg.sample_tick(t) else {
                        return "x".into();
                    };
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

        // Append selected virtual rows in definition order.
        for ve in &selected_virtuals {
            display_names.push(ve.name.clone());
            let row: Vec<String> = col_ticks
                .iter()
                .map(|&t| sampled_value(cfg, &ve.transitions, t))
                .collect();
            grid.push(row);
        }

        for (column, header) in col_headers.iter().enumerate() {
            if header.starts_with("clk stopped ") {
                for row in &mut grid {
                    row[column] = "—".into();
                }
            }
        }
        if cfg.json_format {
            emit_query_json(
                cfg,
                "wave",
                crate::output::WaveData {
                    mode: "sync".into(),
                    columns: &col_headers,
                    ticks: &col_ticks,
                    names: &display_names,
                    values: &grid,
                },
                Vec::new(),
            );
            return;
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
        if range_end_opt.is_some_and(|end| end < 0) {
            let _ = writeln!(err, "# No data in range");
            let _ = err.flush();
            return;
        }
        let raw_ticks = cfg.async_mode && cfg.time_str.is_some();
        let bound_tick = |value: i64| {
            let value = value.max(0) as u64;
            if raw_ticks {
                Ok(value)
            } else {
                crate::format::nanoseconds_to_tick(value, cache.timescale_exponent)
            }
        };
        let range_start_tick = bound_tick(range_start).unwrap_or_else(|error| {
            eprintln!("ERROR: cannot convert wave start: {error}");
            std::process::exit(2);
        });
        let range_end_tick = range_end_opt
            .map(bound_tick)
            .transpose()
            .unwrap_or_else(|error| {
                eprintln!("ERROR: cannot convert wave end: {error}");
                std::process::exit(2);
            })
            .unwrap_or(cache.sim_end_tick);

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
        // Selected virtual signals contribute columns too. Unselected helpers
        // remain available to composition without changing the rendered grid.
        for ve in &selected_virtuals {
            for (tick, _) in &ve.transitions {
                if *tick >= range_start_tick && *tick <= range_end_tick {
                    tick_set.insert(*tick);
                }
            }
        }
        // A marker is itself a requested observation point. Keep an in-window
        // column even when no selected signal transitions at that exact tick.
        for (_, marker_tick) in &cfg.markers {
            if let Ok(tick) = u64::try_from(*marker_tick) {
                if tick >= range_start_tick && tick <= range_end_tick {
                    tick_set.insert(tick);
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
            col_headers.push(t.to_string());
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

        // Append selected virtual rows in definition order.
        for ve in &selected_virtuals {
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
        if cfg.json_format {
            emit_query_json(
                cfg,
                "wave",
                crate::output::WaveData {
                    mode: "async".into(),
                    columns: &col_headers,
                    ticks: &col_ticks,
                    names: &display_names,
                    values: &grid,
                },
                Vec::new(),
            );
            return;
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
    #[test]
    fn time_token_phase_checked_grid_overflow() {
        assert!(super::checked_cycle_base(6, 10, u64::MAX).is_err());
        assert_eq!(super::checked_cycle_base(5, 10, 18), Ok(25));
        assert_eq!(super::checked_cycle_base(5, 10, 15), Ok(15));
    }

    #[test]
    fn issue_1098_virtual_and_real_statistics_share_occupied_run_semantics() {
        let events = vec![
            (0, "1".to_string()),
            (5, "1".to_string()),
            (10, "0".to_string()),
            (20, "1".to_string()),
            (30, "0".to_string()),
        ];
        for initial in ["x", "0"] {
            let stats = super::scan_stats(&events, 0, 30, false, initial, "held".into(), 1);
            assert_eq!(stats.transitions, 3);
            assert_eq!(stats.value_hist.get("1"), Some(&2));
            assert_eq!(stats.value_hist.get("0"), Some(&1));
            assert_eq!(stats.time_in_state.get("1"), Some(&20));
            assert_eq!(stats.time_in_state.get("0"), Some(&10));
            assert!(!stats.time_in_state.contains_key("x"));
        }
    }

    #[test]
    fn issue_1098_unknown_occupancy_does_not_inflate_binary_coverage() {
        for unknown in ["x", "z"] {
            let events = vec![(10, "0".into()), (20, "1".into())];
            let stats = super::scan_stats(&events, 0, 30, false, unknown, "late".into(), 1);
            assert_eq!(stats.time_in_state.get(unknown), Some(&10));
            assert_eq!(stats.value_hist.get(unknown), Some(&1));
            assert_eq!(stats.value_pct, 100.0);
            let only_one =
                super::scan_stats(&[(10, "1".into())], 0, 20, false, unknown, "late".into(), 1);
            assert_eq!(only_one.value_pct, 50.0);
            assert_eq!(only_one.toggle_pct, 0.0);
            let wide = super::scan_stats(
                &[(10, "80".into())],
                0,
                20,
                false,
                unknown,
                "wide".into(),
                8,
            );
            assert_eq!(wide.toggle_pct, 0.0);
        }
        let mixed = super::scan_stats(
            &[(0, "x0".into()), (10, "z1".into())],
            0,
            20,
            false,
            "x",
            "mixed".into(),
            8,
        );
        assert_eq!(mixed.value_pct, 0.0);
        assert_eq!(mixed.toggle_pct, 12.5);
    }

    use super::*;
    use std::io::BufReader;
    use std::sync::atomic::{AtomicUsize, Ordering};

    static TEST_COUNTER: AtomicUsize = AtomicUsize::new(0);

    #[test]
    fn issue_1421_sampling_uses_last_same_tick_write() {
        let transitions = vec![
            (0, "0".to_string()),
            (10, "1".to_string()),
            (10, "0".to_string()),
            (10, "1".to_string()),
            (10, "0".to_string()),
            (20, "1".to_string()),
        ];
        assert_eq!(value_at_tick(&transitions, 10), "0");
        assert_eq!(value_at_tick(&transitions, 15), "0");
        for before in 0..16 {
            for duplicates in 1..64 {
                for after in 0..16 {
                    let mut records: Vec<_> = (0..before).map(|t| (t, t.to_string())).collect();
                    records.extend((0..duplicates).map(|i| (20, i.to_string())));
                    records.extend((0..after).map(|i| (30 + i, i.to_string())));
                    assert_eq!(value_at_tick(&records, 20), (duplicates - 1).to_string());
                    assert_eq!(value_at_tick(&records, 25), (duplicates - 1).to_string());
                }
            }
        }
    }

    #[test]
    fn issue_1421_compressed_pairs_match_small_exhaustive_oracle() {
        for mask_a in 0u64..64 {
            let a: Vec<_> = (1..=6).filter(|c| mask_a & (1 << (c - 1)) != 0).collect();
            let runs_a = union_cycle_runs(a.iter().map(|&c| (c, c)).collect());
            for mask_b in 0u64..64 {
                let b: Vec<_> = (1..=6).filter(|c| mask_b & (1 << (c - 1)) != 0).collect();
                let runs_b = union_cycle_runs(b.iter().map(|&c| (c, c)).collect());
                for two_event in [false, true] {
                    let expected: Vec<_> = if two_event {
                        a.iter()
                            .filter_map(|&a| b.iter().find(|&&b| b > a).map(|&b| (a, b)))
                            .collect()
                    } else {
                        a.windows(2).map(|p| (p[0], p[1])).collect()
                    };
                    let segments =
                        sampled_pair_segments(&runs_a, two_event.then_some(runs_b.as_slice()));
                    let actual: Vec<_> = segments
                        .iter()
                        .flat_map(|s| {
                            (0..s.count)
                                .map(move |i| (s.a + i, s.b + if s.consecutive_b { i } else { 0 }))
                        })
                        .collect();
                    assert_eq!(actual, expected, "A={a:?} B={b:?} two={two_event}");
                    if !expected.is_empty() {
                        let deltas: Vec<_> = expected.iter().map(|&(a, b)| b - a).collect();
                        assert_eq!(pair_summary(&segments), distance_summary(&deltas));
                    }
                }
            }
        }
        let huge = sampled_pair_segments(
            &[(1, 100_000_000_000)],
            Some(&[(100_000_000_001, 100_000_000_001)]),
        );
        assert_eq!(huge.len(), 1);
        assert_eq!(
            pair_summary(&huge),
            "count=100000000000  min=1  max=100000000000  mean=50000000000.5  median=50000000000.5"
        );
    }

    #[test]
    fn issue_1421_compressed_samples_match_grid_oracle() {
        let mut cache = build_cache("$timescale 1ns $end\n$scope module tb $end\n$var wire 1 ! sig $end\n$upscope $end\n$enddefinitions $end\n#0\n0!\n#100\n1!\n");
        cache.override_clock(10, 0, "test_clock");
        let transitions = vec![
            (0, "0"),
            (7, "1"),
            (9, "0"),
            (20, "1"),
            (31, "x"),
            (39, "z"),
            (60, "0"),
            (91, "1"),
        ]
        .into_iter()
        .map(|(t, v)| (t, v.to_string()))
        .collect::<Vec<_>>();
        for base in [0, 5, 25, 105] {
            for start in 0..=12 {
                for end in start..=12 {
                    let cfg = ExtractConfig {
                        time_min: start,
                        time_max: Some(end),
                        ..Default::default()
                    };
                    for width in [1, 4] {
                        for value in ["0", "1", "x", "z", "rising", "falling", "change"] {
                            let expected: Vec<_> =
                                sampled_events(&cache, &cfg, base, &transitions, width, value)
                                    .filter_map(|(tick, _, matched)| {
                                        matched.then_some(tick_to_cycle(tick, base, 10))
                                    })
                                    .collect();
                            let runs =
                                sampled_event_runs(&cache, &cfg, base, &transitions, width, value);
                            let actual: Vec<_> =
                                runs.iter().flat_map(|&(lo, hi)| lo..=hi).collect();
                            assert_eq!(
                                actual, expected,
                                "base={base} window={start}:{end} width={width} value={value}"
                            );
                        }
                    }
                }
            }
        }
    }

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
        assert_eq!(cache.detect_clock_from_pattern("*rstn*").unwrap().0, 0);
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
    fn beyond_range_message_carries_the_python_marker() {
        let msg = beyond_range_message("900");
        assert!(
            msg.to_lowercase().contains("beyond simulation range"),
            "marker BEYOND_RANGE_MARKER lost from: {msg}"
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
    #[test]
    fn issue_1100_zero_cycle_base_query_agreement() {
        const CHILD: &str = "BOOLEY_ISSUE_1100_ZERO_BASE_CHILD";
        if let Ok(query) = std::env::var(CHILD) {
            println!();
            let mut cache = build_cache("$timescale 1ns $end\n$scope module tb $end\n$var wire 1 ! sig $end\n$upscope $end\n$enddefinitions $end\n#0\n0!\n#7\n1!\n#9\n0!\n#17\n1!\n#19\n0!\n#27\n1!\n#29\n0!\n");
            cache.override_clock(10, 0, "test_clock");
            let name = if query.ends_with("virtual") {
                "v"
            } else {
                "sig"
            };
            let mut cfg = ExtractConfig {
                patterns: vec![name.into()],
                with_reset: true,
                time_min: 2,
                time_max: Some(2),
                virtual_defs: vec!["v=tb.sig=='b1".into()],
                ..Default::default()
            };
            let trigger = if query.contains("literal-") {
                cfg.time_min = 1;
                cfg.time_max = Some(3);
                "0"
            } else {
                "change"
            };
            if query.starts_with("find:") {
                cfg.find_pattern = Some(name.into());
                cfg.find_value = Some(trigger.into());
                find_value_from_cache(&cache, &cfg);
            } else if query.starts_with("sample:") {
                cfg.sample_at_pattern = Some(name.into());
                cfg.sample_at_value = Some(trigger.into());
                sample_at_from_cache(&cache, &cfg);
            } else {
                cfg.distance_a = Some((name.into(), trigger.into()));
                if query.ends_with(":mixed-forward") {
                    cfg.distance_a = Some(("sig".into(), "rising".into()));
                    cfg.distance_b = Some(("v".into(), "change".into()));
                } else if query.ends_with(":mixed-reverse") {
                    cfg.distance_a = Some(("v".into(), "change".into()));
                    cfg.distance_b = Some(("sig".into(), "falling".into()));
                } else if query.ends_with(":directional-only") {
                    cfg.distance_a = Some(("sig".into(), "rising".into()));
                }
                distance_from_cache(&cache, &cfg);
            }
            return;
        }
        for kind in [
            "physical",
            "virtual",
            "mixed-forward",
            "mixed-reverse",
            "directional-only",
            "literal-physical",
            "literal-virtual",
        ] {
            for command in ["find", "sample", "distance"] {
                if !kind.contains("physical") && !kind.contains("virtual") && command != "distance"
                {
                    continue;
                }
                let mut child = std::process::Command::new(std::env::current_exe().unwrap())
                    .args([
                        "--exact",
                        "cache::tests::issue_1100_zero_cycle_base_query_agreement",
                        "--nocapture",
                        "--test-threads=1",
                    ])
                    .env(CHILD, format!("{command}:{kind}"))
                    .stdout(std::process::Stdio::piped())
                    .stderr(std::process::Stdio::piped())
                    .spawn()
                    .unwrap();
                let deadline = std::time::Instant::now() + std::time::Duration::from_secs(30);
                while child.try_wait().unwrap().is_none() {
                    if std::time::Instant::now() >= deadline {
                        child.kill().unwrap();
                        panic!("zero-base query timeout");
                    }
                    std::thread::sleep(std::time::Duration::from_millis(10));
                }
                let output = child.wait_with_output().unwrap();
                assert!(
                    output.status.success(),
                    "{}",
                    String::from_utf8_lossy(&output.stderr)
                );
                let text = String::from_utf8(output.stdout).unwrap();
                let rows = text
                    .lines()
                    .filter(|line| {
                        line.starts_with("cycle ")
                            || line
                                .split_whitespace()
                                .next()
                                .is_some_and(|token| token.parse::<u64>().is_ok())
                            || line.starts_with("@ ")
                    })
                    .collect::<Vec<_>>()
                    .join("\n");
                let name = if kind.ends_with("virtual") {
                    "v"
                } else {
                    "sig"
                };
                // Raw pulses never survive the zero-based sampled grid.
                // Literal zero matches all three samples, including cycle 1.
                let expected = if kind.starts_with("literal-") {
                    match command {
                        "find" => format!("cycle 1 {name} 0\ncycle 2 {name} 0\ncycle 3 {name} 0"),
                        "sample" => format!("1 {name} 0\n2 {name} 0\n3 {name} 0"),
                        _ => "@ 1 -> @ 2  d=1\n@ 2 -> @ 3  d=1".into(),
                    }
                } else {
                    String::new()
                };
                assert_eq!(rows, expected, "{command} {kind}: {text}");
            }
        }
    }

    #[test]
    fn issue_1108_direct_diff_unit_provenance() {
        let mut cache = build_cache(BASIC_VCD);
        cache.ticks_to_ns = 0.001;
        cache.timescale_exponent = Some(-12);
        cache.sim_end_tick = 100_000;
        let mut cfg = ExtractConfig {
            async_mode: true,
            ..Default::default()
        };
        assert_eq!(resolve_time_to_tick(&cache, &cfg, 35, 0), Some(35_000));
        cfg.diff_strs = Some(("35t".to_string(), "95t".to_string()));
        assert_eq!(resolve_time_to_tick(&cache, &cfg, 35, 0), Some(35));
        for provenance in [None, Some(("0t".to_string(), "1t".to_string()))] {
            cfg.diff_strs = provenance;
            assert_eq!(resolve_time_to_tick(&cache, &cfg, -1, 0), None);
            assert_eq!(resolve_time_to_tick(&cache, &cfg, 100_001, 0), None);
        }
    }

    #[test]
    fn issue_1108_distance_summary_even_and_overflow() {
        assert_eq!(
            distance_summary(&[1, 3, 6, 15]),
            "count=4  min=1  max=15  mean=6.2  median=4.5"
        );
        assert_eq!(
            distance_summary(&[7]),
            "count=1  min=7  max=7  mean=7.0  median=7.0"
        );
        let summary = distance_summary(&[u64::MAX, u64::MAX]);
        assert!(summary.contains("count=2"));
        assert!(summary.contains("mean=18446744073709551616.0  median=18446744073709551616.0"));
    }
    #[test]
    fn time_token_exact_legacy_cache_bounds() {
        let mut cache = build_cache("$timescale 1ps $end\n$scope module tb $end\n$var wire 1 ! data $end\n$upscope $end\n$enddefinitions $end\n#0\n0!\n#10000000000000000000\n1!\n");
        let cfg = ExtractConfig::default();
        assert_eq!(
            resolve_time_to_tick(&cache, &cfg, 10_000_000_000_000_000, 0),
            Some(10_000_000_000_000_000_000)
        );
        assert_eq!(resolve_time_to_tick(&cache, &cfg, i64::MAX, 0), None);
        cache.timescale_exponent = None;
        assert_eq!(resolve_time_to_tick(&cache, &cfg, 123, 0), Some(123));
    }

    #[test]
    fn time_token_exact_legacy_snapshot_unsigned() {
        const CHILD: &str = "BWAVE_EXACT_LEGACY_SNAPSHOT_CHILD";
        if std::env::var_os(CHILD).is_some() {
            let cache = build_cache("$timescale 1ps $end\n$scope module tb $end\n$var wire 1 ! data $end\n$upscope $end\n$enddefinitions $end\n#0\n0!\n#10000000000000000000\n1!\n");
            snapshot_from_cache(
                &cache,
                &ExtractConfig {
                    at_time: Some(10_000_000_000_000_000),
                    json_format: true,
                    ..Default::default()
                },
            );
            diff_from_cache(
                &cache,
                &ExtractConfig {
                    diff_points: Some((9_999_999_999_999_999, 10_000_000_000_000_000)),
                    ..Default::default()
                },
            );
            return;
        }
        let mut child = std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "cache::tests::time_token_exact_legacy_snapshot_unsigned",
                "--nocapture",
            ])
            .env(CHILD, "1")
            .stdout(std::process::Stdio::piped())
            .stderr(std::process::Stdio::piped())
            .spawn()
            .unwrap();
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(30);
        while child.try_wait().unwrap().is_none() {
            if std::time::Instant::now() >= deadline {
                child.kill().unwrap();
                panic!("snapshot timeout");
            }
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
        let output = child.wait_with_output().unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        let text = String::from_utf8(output.stdout).unwrap();
        assert!(
            text.contains("\"target_tick\": 10000000000000000000"),
            "{text}"
        );
        assert!(text.contains("\"value\": \"1\""), "{text}");
        assert!(
            text.lines()
                .any(|line| line.split_whitespace().collect::<Vec<_>>()
                    == ["data", "@9999999999999999=0", "@10000000000000000=1"]),
            "{text}"
        );
    }

    #[test]
    fn time_token_exact_parent_grid_error_precedes_conversion() {
        let mut cache = build_cache("$timescale 1ps $end\n$scope module tb $end\n$var wire 1 ! rstn $end\n$upscope $end\n$enddefinitions $end\n#0\n0!\n#18446744073709551615\n1!\n");
        cache.first_rise_tick = 6;
        cache.clock_period_ticks = 10;
        let policy = ExtractConfig {
            reset_pattern: Some("tb.rstn".into()),
            ..Default::default()
        };
        assert_eq!(
            resolve_time_token(&cache, &policy, &crate::format::TimeToken::Nano(i64::MAX))
                .unwrap_err(),
            "cycle base is beyond simulation tick range"
        );
    }

    #[test]
    fn time_token_exact_config_destinations() {
        let mut cfg = ExtractConfig {
            async_mode: true,
            time_str: Some("1369600ps:1369700ps".into()),
            at_str: Some("1369600ps".into()),
            diff_strs: Some(("1369500ps".into(), "1369600ps".into())),
            marker_tokens: vec![("edge".into(), crate::format::TimeToken::Pico(1369600))],
            ..Default::default()
        };
        let mut cache = build_cache(BASIC_VCD);
        cache.timescale_exponent = Some(-12);
        cfg.resolve_time_tokens(&cache).unwrap();
        assert_eq!((cfg.time_min, cfg.time_max), (1369600, Some(1369700)));
        assert_eq!(cfg.at_time, Some(1369600));
        assert_eq!(cfg.diff_points, Some((1369500, 1369600)));
        assert_eq!(cfg.markers, vec![("edge".into(), 1369600)]);
    }
}
