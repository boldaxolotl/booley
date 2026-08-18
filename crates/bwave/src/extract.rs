//! Extraction state machine — all modes.
//! Implements VcdHandler trait for streaming VCD event processing.

use std::collections::HashMap;
use std::io::{self, BufWriter, Write};
use std::ops::ControlFlow;

use rustc_hash::{FxHashMap, FxHashSet};
use serde_json;

use crate::format::{format_value, is_edge_keyword, values_match};
use crate::index::IndexBuilder;
use crate::parser::{VcdHandler, VcdHeader};
use crate::signal::{common_scope_prefix, compile_patterns, match_signal, SignalMeta};
use crate::ExtractConfig;

// Sentinel value for "no signal index"
const NO_SIGNAL: usize = usize::MAX;

/// Main extractor implementing the VcdHandler trait.
pub struct Extractor {
    cfg: ExtractConfig,
    done: bool,
    line_count: usize,

    // Signal lookup tables (populated from header)
    // Numeric index is the canonical ID; all hot-path lookups use it.
    id_str_to_idx: FxHashMap<String, usize>, // VCD id string -> numeric index
    sig_names: Vec<Vec<String>>,             // idx -> signal names (aliases)
    sig_widths: Vec<u32>,                    // idx -> bit width
    sig_count: usize,                        // number of tracked signals

    // Ordered display list: indices into sig_names/sig_widths
    display_order: Vec<(usize, usize)>, // (sig_idx, name_index_within_aliases)

    // Current and previous values, indexed by sig_idx
    current_vals: Vec<String>,
    prev_emitted_vals: Vec<Option<String>>, // None = sentinel (never emitted)

    // Reverse lookup: display_order index -> (sig_idx)
    // Pre-built name_to_idx for ordered output
    name_to_sig_idx: Vec<(String, usize)>, // (display_name, sig_idx) in display order
    // Fast O(1) membership test for "is this sig_idx in the watched display set"
    // Built once in init_from_header -- avoids O(N*M) linear scan on the value-change hot path
    watched_sig_idx_set: FxHashSet<usize>,

    // Timescale
    ticks_to_ns: f64,
    timescale_str: String,

    // Time range in ticks (async) or cycles (sync, stored as-is)
    time_min_ticks: i64,
    time_max_ticks: Option<i64>,

    // Display
    scope_prefix: String,

    // Sync mode state
    sync_mode: bool,
    clock_idx: usize, // NO_SIGNAL if no clock
    clock_prev_val: u8,
    rising_edge_pending: bool,
    cycle_count: i64,
    first_rise_tick: Option<u64>,
    second_rise_tick: Option<u64>,
    clock_period_ticks: Option<u64>,

    // Reset state
    skip_reset: bool,
    reset_idx: usize, // NO_SIGNAL if no reset
    reset_name: Option<String>,
    reset_active_low: bool,
    reset_active: bool,

    // Find mode state
    find_target_indices: Vec<usize>, // sig indices that match find pattern
    find_val_cached: String,         // cached find_value for hot-path (avoids per-cycle clone)
    find_edge_mode: Option<String>,  // "rising"/"falling" or None
    prev_find_vals: Vec<String>,     // indexed by sig_idx (only find targets used)
    find_count: usize,
    find_last_result: Option<String>, // buffer for --last mode

    // Sample-at state
    sample_at_indices: Vec<usize>, // sig indices for trigger signals
    sample_at_str_ids: FxHashSet<String>, // VCD string IDs for quick membership test in parser
    sample_at_current_vals: Vec<String>, // indexed by position in sample_at_indices
    sample_at_prev_vals: Vec<String>, // indexed by position in sample_at_indices
    sample_at_idx_to_pos: FxHashMap<usize, usize>, // sig_idx -> position in sample_at_indices
    sample_at_edge_mode: Option<String>,
    sample_at_level_mode: bool,
    // 'change' mode = trigger on any transition (edge direction agnostic).
    // Distinct from sample_at_edge_mode which stores "rising"/"falling".
    sample_at_change_mode: bool,
    sample_at_triggered: bool,
    sample_at_count: usize,
    sample_at_watched_changed: bool,

    // Stats state
    stats_data: Vec<Option<StatsEntry>>, // indexed by sig_idx
    sim_start_tick: u64,
    sim_end_tick: u64,
    first_timestamp_seen: bool,

    // At-time state (async)
    at_time_ticks: Option<i64>,

    // Index building
    index_builder: Option<IndexBuilder>,
    last_timestamp_offset: u64,

    // Cache building

    // Buffered output
    stdout: BufWriter<io::Stdout>,
    stderr: BufWriter<io::Stderr>,
}

struct StatsEntry {
    transitions: u64,
    value_hist: FxHashMap<String, u64>,
    time_in_state: FxHashMap<String, u64>,
    last_change_tick: u64,
    last_value: String,
}

/// Try to write to stdout; return false on broken pipe.
macro_rules! write_out {
    ($self:expr, $($arg:tt)*) => {
        match writeln!($self.stdout, $($arg)*) {
            Ok(()) => true,
            Err(e) if e.kind() == io::ErrorKind::BrokenPipe => {
                $self.done = true;
                false
            }
            Err(_) => {
                $self.done = true;
                false
            }
        }
    };
}

impl Extractor {
    pub fn new(cfg: ExtractConfig) -> Self {
        let sync_mode = !cfg.async_mode;
        let skip_reset = !cfg.with_reset && !cfg.async_mode;
        Extractor {
            done: false,
            line_count: 0,
            id_str_to_idx: FxHashMap::default(),
            sig_names: Vec::new(),
            sig_widths: Vec::new(),
            sig_count: 0,
            display_order: Vec::new(),
            current_vals: Vec::new(),
            prev_emitted_vals: Vec::new(),
            name_to_sig_idx: Vec::new(),
            watched_sig_idx_set: FxHashSet::default(),
            ticks_to_ns: 1.0,
            timescale_str: String::from("1ns"),
            time_min_ticks: 0,
            time_max_ticks: None,
            scope_prefix: String::new(),
            sync_mode,
            clock_idx: NO_SIGNAL,
            clock_prev_val: b'x',
            rising_edge_pending: false,
            cycle_count: 0,
            first_rise_tick: None,
            second_rise_tick: None,
            clock_period_ticks: None,
            skip_reset,
            reset_idx: NO_SIGNAL,
            reset_name: None,
            reset_active_low: true,
            reset_active: true,
            find_target_indices: Vec::new(),
            find_val_cached: String::new(),
            find_edge_mode: None,
            prev_find_vals: Vec::new(),
            find_count: 0,
            find_last_result: None,
            sample_at_indices: Vec::new(),
            sample_at_str_ids: FxHashSet::default(),
            sample_at_current_vals: Vec::new(),
            sample_at_prev_vals: Vec::new(),
            sample_at_idx_to_pos: FxHashMap::default(),
            sample_at_edge_mode: None,
            sample_at_level_mode: false,
            sample_at_change_mode: false,
            sample_at_triggered: false,
            sample_at_count: 0,
            sample_at_watched_changed: false,
            stats_data: Vec::new(),
            sim_start_tick: 0,
            sim_end_tick: 0,
            first_timestamp_seen: false,
            at_time_ticks: None,
            index_builder: None,
            last_timestamp_offset: 0,
            stdout: BufWriter::new(io::stdout()),
            stderr: BufWriter::new(io::stderr()),
            cfg,
        }
    }

    /// Initialize from parsed VCD header. Must be called before streaming.
    /// Returns Err if a required signal pattern has no matches.
    pub fn init_from_header(
        &mut self,
        header: &VcdHeader,
        all_signals: &[SignalMeta],
    ) -> Result<(), String> {
        self.ticks_to_ns = header.ticks_to_ns;
        self.timescale_str = header.timescale_str.clone();

        if !self.sync_mode {
            let _ = writeln!(self.stderr, "# timescale: {}", self.timescale_str);
        }

        // Build signal lookup tables for watched signals
        let matchers = compile_patterns(&self.cfg.patterns)?;
        let mut signal_name_list: Vec<String> = Vec::new();

        for sig in all_signals {
            if match_signal(&sig.name, &matchers) {
                let idx = if let Some(&existing) = self.id_str_to_idx.get(&sig.id) {
                    // Alias: same VCD ID, add name to existing entry
                    self.sig_names[existing].push(sig.name.clone());
                    existing
                } else {
                    let idx = self.sig_count;
                    self.sig_count += 1;
                    self.id_str_to_idx.insert(sig.id.clone(), idx);
                    self.sig_names.push(vec![sig.name.clone()]);
                    self.sig_widths.push(sig.width);
                    idx
                };
                let name_pos = self.sig_names[idx].len() - 1;
                self.display_order.push((idx, name_pos));
                signal_name_list.push(sig.name.clone());
            }
        }

        // Initialize value vectors
        self.current_vals = vec!["x".to_string(); self.sig_count];
        self.prev_emitted_vals = vec![None; self.sig_count];
        self.prev_find_vals = vec!["x".to_string(); self.sig_count];

        // Common scope prefix
        self.scope_prefix = common_scope_prefix(&signal_name_list);
        if !self.scope_prefix.is_empty() {
            let display = &self.scope_prefix[..self.scope_prefix.len() - 1];
            let _ = writeln!(self.stderr, "# scope: {}", display);
        }

        // Build pre-computed display name -> sig_idx mapping
        self.name_to_sig_idx = self
            .display_order
            .iter()
            .map(|&(idx, name_pos)| {
                let full_name = &self.sig_names[idx][name_pos];
                let dname = self.strip_prefix(full_name).to_string();
                (dname, idx)
            })
            .collect();

        // Build O(1) membership set of watched sig indices once.
        // Used in on_value_change hot path to avoid O(N*M) linear scan.
        self.watched_sig_idx_set = self.name_to_sig_idx.iter().map(|(_, i)| *i).collect();

        // Find mode: identify target signals
        if let Some(ref find_pat) = self.cfg.find_pattern {
            if let Some(ref find_val) = self.cfg.find_value {
                self.find_edge_mode = is_edge_keyword(find_val).map(String::from);
                self.find_val_cached = find_val.clone();
            }
            let find_matchers = compile_patterns(&[find_pat.clone()])?;
            for sig in all_signals {
                if match_signal(&sig.name, &find_matchers) {
                    if let Some(&idx) = self.id_str_to_idx.get(&sig.id) {
                        if !self.find_target_indices.contains(&idx) {
                            self.find_target_indices.push(idx);
                        }
                    }
                }
            }
            if self.find_target_indices.is_empty() {
                let _ = writeln!(self.stderr, "# No signals match pattern '{}'", find_pat);
                self.done = true;
            }
        }

        // Sample-at mode: identify trigger signals from ALL VCD signals
        if let Some(ref sa_pat) = self.cfg.sample_at_pattern {
            // Infer trigger mode from VALUE keyword
            if let Some(ref sa_val) = self.cfg.sample_at_value {
                let edge_kw = is_edge_keyword(sa_val).map(String::from);
                self.sample_at_change_mode = edge_kw.as_deref() == Some("change");
                self.sample_at_edge_mode = if self.sample_at_change_mode {
                    None
                } else {
                    edge_kw
                };
                self.sample_at_level_mode =
                    self.sample_at_edge_mode.is_none() && !self.sample_at_change_mode;
            }
            let sa_matchers = compile_patterns(&[sa_pat.clone()])?;
            for sig in all_signals {
                if match_signal(&sig.name, &sa_matchers) {
                    // Register trigger signal (may or may not be in watched set)
                    let idx = if let Some(&existing) = self.id_str_to_idx.get(&sig.id) {
                        existing
                    } else {
                        // Trigger not in watched set: allocate an index
                        let idx = self.sig_count;
                        self.sig_count += 1;
                        self.id_str_to_idx.insert(sig.id.clone(), idx);
                        self.sig_names.push(vec![sig.name.clone()]);
                        self.sig_widths.push(sig.width);
                        self.current_vals.push("x".to_string());
                        self.prev_emitted_vals.push(None);
                        self.prev_find_vals.push("x".to_string());
                        idx
                    };
                    if !self.sample_at_idx_to_pos.contains_key(&idx) {
                        let pos = self.sample_at_indices.len();
                        self.sample_at_indices.push(idx);
                        self.sample_at_current_vals.push("x".to_string());
                        self.sample_at_prev_vals.push("x".to_string());
                        self.sample_at_idx_to_pos.insert(idx, pos);
                        self.sample_at_str_ids.insert(sig.id.clone());
                    }
                }
            }
            if self.sample_at_indices.is_empty() {
                return Err(format!(
                    "--sample: no signals match trigger pattern '{}'",
                    sa_pat
                ));
            }
            let _ = writeln!(
                self.stderr,
                "# sample: {} trigger signal(s)",
                self.sample_at_indices.len()
            );
        }

        // Clock detection
        if self.sync_mode {
            self.detect_clock(all_signals)?;
        }
        if !self.sync_mode {
            self.skip_reset = false;
        }

        // Reset detection
        if self.skip_reset {
            self.detect_reset(all_signals)?;
        }

        // Stats initialization
        if self.cfg.stats_mode || self.cfg.find_stuck.is_some() {
            self.stats_data = (0..self.sig_count)
                .map(|idx| {
                    // Only create stats for watched signals (those in display_order)
                    if self.display_order.iter().any(|&(i, _)| i == idx) {
                        Some(StatsEntry {
                            transitions: 0,
                            value_hist: FxHashMap::default(),
                            time_in_state: FxHashMap::default(),
                            last_change_tick: 0,
                            last_value: "x".into(),
                        })
                    } else {
                        None
                    }
                })
                .collect();
        }

        // Async mode: --time and --at-time values are in VCD timescale units (no conversion)
        if !self.sync_mode {
            self.time_min_ticks = self.cfg.time_min;
            self.time_max_ticks = self.cfg.time_max;
        }

        if !self.sync_mode {
            if let Some(at) = self.cfg.at_time {
                self.at_time_ticks = Some(at);
            }
        }

        let _ = self.stderr.flush();
        Ok(())
    }

    fn detect_clock(&mut self, all_signals: &[SignalMeta]) -> Result<(), String> {
        let mut candidates: Vec<(&str, &str)> = Vec::new();

        // Hoist pattern compilation outside the loop.
        // Propagate invalid-glob errors rather than silently falling back to '*'.
        let clock_matchers = match self.cfg.clock_pattern.as_ref() {
            Some(pat) => Some(compile_patterns(&[pat.clone()])?),
            None => None,
        };

        for sig in all_signals {
            if sig.width != 1 {
                continue;
            }
            if let Some(ref matchers) = clock_matchers {
                if match_signal(&sig.name, matchers) {
                    candidates.push((&sig.name, &sig.id));
                }
            } else {
                let stripped = sig.name.split('[').next().unwrap_or(&sig.name);
                if stripped.to_lowercase().contains("clk") {
                    candidates.push((&sig.name, &sig.id));
                }
            }
        }

        if candidates.is_empty() {
            let _ = writeln!(
                self.stderr,
                "# WARNING: no clock signal found, falling back to async mode"
            );
            if self.cfg.sample_at_pattern.is_some() {
                let _ = writeln!(
                    self.stderr,
                    "# WARNING: --sample will use async (timestamp-based) sampling instead of cycle-based"
                );
            }
            self.sync_mode = false;
            return Ok(());
        }

        // Sort by scope depth then alphabetically
        candidates.sort_by(|a, b| {
            let da = a.0.matches('.').count();
            let db = b.0.matches('.').count();
            da.cmp(&db).then(a.0.cmp(b.0))
        });

        let (clock_name, clock_id) = candidates[0];
        // Allocate index for clock if not already tracked
        self.clock_idx = self.get_or_alloc_idx(clock_id);
        if self.sync_mode {
            let _ = writeln!(self.stderr, "# sync: clock={}", clock_name);
        }

        Ok(())
    }

    fn detect_reset(&mut self, all_signals: &[SignalMeta]) -> Result<(), String> {
        let mut candidates: Vec<(&str, &str)> = Vec::new();

        // Hoist pattern compilation outside the loop.
        // Propagate invalid-glob errors rather than silently falling back to '*'.
        let reset_matchers = match self.cfg.reset_pattern.as_ref() {
            Some(pat) => Some(compile_patterns(&[pat.clone()])?),
            None => None,
        };

        for sig in all_signals {
            if sig.width != 1 {
                continue;
            }
            if let Some(ref matchers) = reset_matchers {
                if match_signal(&sig.name, matchers) {
                    candidates.push((&sig.name, &sig.id));
                }
            } else {
                let stripped = sig.name.split('[').next().unwrap_or(&sig.name);
                if stripped.to_lowercase().contains("rst") {
                    candidates.push((&sig.name, &sig.id));
                }
            }
        }

        if candidates.is_empty() {
            if self.cfg.reset_pattern.is_some() {
                let _ = writeln!(
                    self.stderr,
                    "# WARNING: no reset signal matches '{}'",
                    self.cfg.reset_pattern.as_ref().unwrap()
                );
            }
            self.skip_reset = false;
            return Ok(());
        }

        candidates.sort_by(|a, b| {
            let da = a.0.matches('.').count();
            let db = b.0.matches('.').count();
            da.cmp(&db).then(a.0.cmp(b.0))
        });

        let (reset_name, reset_id) = candidates[0];
        self.reset_idx = self.get_or_alloc_idx(reset_id);
        self.reset_name = Some(reset_name.to_string());

        // Polarity from leaf name
        let leaf = reset_name
            .split('[')
            .next()
            .unwrap_or(reset_name)
            .split('.')
            .last()
            .unwrap_or(reset_name)
            .to_lowercase();
        self.reset_active_low = leaf.ends_with('n') || leaf.contains("_n");

        let polarity = if self.reset_active_low {
            "active-low"
        } else {
            "active-high"
        };
        let _ = writeln!(self.stderr, "# sync: reset={} ({})", reset_name, polarity);
        Ok(())
    }

    /// Get or allocate a numeric index for a VCD ID string.
    fn get_or_alloc_idx(&mut self, id: &str) -> usize {
        if let Some(&idx) = self.id_str_to_idx.get(id) {
            idx
        } else {
            let idx = self.sig_count;
            self.sig_count += 1;
            self.id_str_to_idx.insert(id.to_string(), idx);
            self.sig_names.push(vec![]);
            self.sig_widths.push(0);
            self.current_vals.push("x".to_string());
            self.prev_emitted_vals.push(None);
            self.prev_find_vals.push("x".to_string());
            if !self.stats_data.is_empty() {
                self.stats_data.push(None);
            }
            idx
        }
    }

    /// If --at-time looks like an absolute timestamp (ns) rather than a cycle
    /// number, convert it to the equivalent cycle. Called once after clock
    /// period is determined (second rising edge).
    /// Skipped when --at-cycle was used (at_time_is_cycle = true).
    fn try_convert_at_time_from_ns(&mut self) {
        if self.cfg.at_time_is_cycle {
            return;
        }
        let at_time = match self.cfg.at_time {
            Some(v) => v,
            None => return,
        };
        let period = match self.clock_period_ticks {
            Some(p) if p > 0 => p,
            _ => return,
        };
        let first_rise = match self.first_rise_tick {
            Some(t) => t,
            None => return,
        };

        // Convert at_time from ns to ticks
        let at_ticks = (at_time as f64 / self.ticks_to_ns) as u64;

        // Heuristic: if the value (as ticks) falls at or after the first
        // rising edge, it's almost certainly an absolute timestamp —
        // no realistic cycle count would be that large.
        if at_ticks >= first_rise {
            let cycle = (at_ticks - first_rise) / period;
            let remainder_ticks = (at_ticks - first_rise) % period;
            let remainder_ns = (remainder_ticks as f64 * self.ticks_to_ns) as i64;
            let actual_ns = ((first_rise + cycle * period) as f64 * self.ticks_to_ns) as i64;
            let _ = writeln!(
                self.stderr,
                "# --at-time {}: interpreted as {}ns timestamp → cycle {} (nearest edge at {}ns, +{}ns offset)",
                at_time, at_time, cycle, actual_ns, remainder_ns
            );
            self.cfg.at_time = Some(cycle as i64);
        }
    }

    /// Strip the common scope prefix from a signal name.
    fn strip_prefix<'a>(&self, full_name: &'a str) -> &'a str {
        if !self.scope_prefix.is_empty() && full_name.starts_with(&self.scope_prefix) {
            &full_name[self.scope_prefix.len()..]
        } else {
            full_name
        }
    }

    /// Get the primary display name for a signal index.
    fn primary_name(&self, idx: usize) -> &str {
        self.sig_names[idx]
            .last()
            .map(|s| s.as_str())
            .unwrap_or("?")
    }

    fn display_name(&self, idx: usize) -> &str {
        let name = self.primary_name(idx);
        self.strip_prefix(name)
    }

    fn check_edge(prev: &str, cur: &str, edge_type: &str) -> bool {
        match edge_type {
            "rising" => prev == "0" && cur == "1",
            "falling" => prev == "1" && cur == "0",
            _ => false,
        }
    }

    fn check_max_lines(&mut self) -> bool {
        self.line_count += 1;
        if self.line_count >= self.cfg.max_lines {
            let _ = writeln!(
                self.stderr,
                "# WARNING: limit ({}) reached, output truncated",
                self.cfg.max_lines
            );
            self.done = true;
            true
        } else {
            false
        }
    }

    fn emit_snapshot(&mut self, time_label: &str) {
        write_out!(self, "# Snapshot at {}", time_label);
        for i in 0..self.name_to_sig_idx.len() {
            let (ref dname, idx) = self.name_to_sig_idx[i];
            let val = &self.current_vals[idx];
            write_out!(self, "{:<40} = {}", dname, val);
        }
        self.done = true;
    }

    fn emit_sync_default(&mut self, cycle: i64) {
        for i in 0..self.name_to_sig_idx.len() {
            let (ref dname, idx) = self.name_to_sig_idx[i];
            let val = &self.current_vals[idx];
            let prev = &self.prev_emitted_vals[idx];
            if prev.as_deref() != Some(val.as_str()) {
                if !write_out!(self, "{} {} {}", cycle, dname, val) {
                    return;
                }
                self.prev_emitted_vals[idx] = Some(val.clone());
                if self.check_max_lines() {
                    return;
                }
            }
        }
    }

    fn emit_async(&mut self, time: u64, formatted_value: &str, idx: usize) {
        let t = time as i64;
        if t < self.time_min_ticks {
            return;
        }
        if let Some(max) = self.time_max_ticks {
            if t > max {
                self.done = true;
                return;
            }
        }
        let dname = self.display_name(idx).to_string();
        if !write_out!(self, "{} {} {}", time, dname, formatted_value) {
            return;
        }
        self.check_max_lines();
    }

    fn emit_sample(&mut self, time_label: &str) {
        self.sample_at_count += 1;
        if self.cfg.count_only {
            return;
        }
        for i in 0..self.name_to_sig_idx.len() {
            let (ref dname, idx) = self.name_to_sig_idx[i];
            let val = &self.current_vals[idx];
            if !write_out!(self, "{} {} {}", time_label, dname, val) {
                return;
            }
            if self.check_max_lines() {
                return;
            }
        }
    }

    fn print_stats(&mut self) {
        let total_ticks = self.sim_end_tick.saturating_sub(self.sim_start_tick);
        let total_ns = (total_ticks as f64 * self.ticks_to_ns) as i64;

        // Header
        if self.sync_mode {
            if let Some(period) = self.clock_period_ticks {
                if period > 0 {
                    let total_cycles = total_ticks / period;
                    let period_ns = (period as f64 * self.ticks_to_ns) as i64;
                    write_out!(
                        self,
                        "# Simulation: {}ns, {} cycles ({}ns period)",
                        total_ns,
                        total_cycles,
                        period_ns
                    );
                } else {
                    write_out!(self, "# Simulation: {}ns", total_ns);
                }
            } else {
                write_out!(self, "# Simulation: {}ns", total_ns);
            }
        } else {
            write_out!(self, "# Simulation: {}ns", total_ns);
        }
        let analyzed_count = self.stats_data.iter().filter(|s| s.is_some()).count();
        write_out!(self, "# {} signals analyzed", analyzed_count);
        write_out!(self, "");

        // Collect (sig_idx, &StatsEntry) pairs, sort by transitions descending
        let mut sorted_sigs: Vec<(usize, &StatsEntry)> = self
            .stats_data
            .iter()
            .enumerate()
            .filter_map(|(i, opt)| opt.as_ref().map(|sd| (i, sd)))
            .collect();
        sorted_sigs.sort_by(|a, b| {
            b.1.transitions
                .cmp(&a.1.transitions)
                .then_with(|| self.display_name(a.0).cmp(self.display_name(b.0)))
        });

        // JSON output mode
        if self.cfg.json_format {
            let signals: Vec<serde_json::Value> = sorted_sigs
                .iter()
                .map(|&(idx, sd)| {
                    let vh: HashMap<String, u64> =
                        sd.value_hist.iter().map(|(k, v)| (k.clone(), *v)).collect();
                    let tis: HashMap<String, u64> = sd
                        .time_in_state
                        .iter()
                        .map(|(k, v)| (k.clone(), *v))
                        .collect();
                    serde_json::json!({
                        "name": self.display_name(idx),
                        "width": self.sig_widths[idx],
                        "transitions": sd.transitions,
                        "value_hist": vh,
                        "time_in_state": tis,
                    })
                })
                .collect();
            let json_out = serde_json::json!({
                "simulation_ns": total_ns,
                "signals": signals,
            });
            let _ = writeln!(
                self.stdout,
                "{}",
                serde_json::to_string_pretty(&json_out).unwrap_or_default()
            );
            let _ = self.stdout.flush();
            return;
        }

        for &(idx, sd) in &sorted_sigs {
            let dname = self.display_name(idx).to_string();
            let width = self.sig_widths[idx];
            let width_str = if width == 1 {
                "1-bit".to_string()
            } else {
                format!("{}-bit", width)
            };
            let n_unique = sd.value_hist.len();
            write_out!(
                self,
                "{}  {}  {} transitions  {} unique values",
                dname,
                width_str,
                sd.transitions,
                n_unique
            );

            // Time-in-state breakdown
            if !sd.time_in_state.is_empty() && total_ticks > 0 {
                let mut sorted_states: Vec<(&String, &u64)> = sd.time_in_state.iter().collect();
                sorted_states.sort_by(|a, b| b.1.cmp(a.1).then(a.0.cmp(b.0)));
                let mut parts = Vec::new();
                for (val, ticks) in &sorted_states {
                    let t = **ticks;
                    let pct = (t as f64 / total_ticks as f64) * 100.0;
                    if self.sync_mode {
                        if let Some(period) = self.clock_period_ticks {
                            if period > 0 {
                                let duration = t / period;
                                parts.push(format!("{}: {:.0}% ({} cyc)", val, pct, duration));
                                continue;
                            }
                        }
                    }
                    let duration_ns = (t as f64 * self.ticks_to_ns) as i64;
                    parts.push(format!("{}: {:.0}% ({}ns)", val, pct, duration_ns));
                }
                write_out!(self, "  {}", parts.join("  "));
            }
            write_out!(self, "");
        }

        // Top 5 most active
        let top5: Vec<_> = sorted_sigs.iter().take(5).collect();
        if !top5.is_empty() {
            let top_str: Vec<String> = top5
                .iter()
                .map(|&&(idx, sd)| {
                    let dname = self.display_name(idx).to_string();
                    let leaf = dname.split('.').last().unwrap_or(&dname);
                    format!("{}({})", leaf, sd.transitions)
                })
                .collect();
            write_out!(
                self,
                "# Top {} most active: {}",
                top5.len(),
                top_str.join(", ")
            );
        }
    }

    fn finalize_time_in_state(&mut self) {
        for sd in self.stats_data.iter_mut().flatten() {
            let final_elapsed = self.sim_end_tick.saturating_sub(sd.last_change_tick);
            let fv = sd.last_value.clone();
            *sd.time_in_state.entry(fv).or_insert(0) += final_elapsed;
        }
    }

    fn print_find_stuck(&mut self) {
        let total_ticks = self.sim_end_tick.saturating_sub(self.sim_start_tick);

        // Filter to stuck signals (0 transitions)
        let mut stuck: Vec<(String, String, String)> = Vec::new();
        for (idx, opt_sd) in self.stats_data.iter().enumerate() {
            let sd = match opt_sd {
                Some(sd) => sd,
                None => continue,
            };
            if sd.transitions != 0 {
                continue;
            }
            let dname = self.display_name(idx).to_string();
            let width = self.sig_widths[idx];
            let width_str = if width == 1 {
                "1-bit".to_string()
            } else {
                format!("{}-bit", width)
            };
            let stuck_val = sd
                .time_in_state
                .keys()
                .next()
                .cloned()
                .unwrap_or_else(|| "x".into());
            stuck.push((dname, width_str, stuck_val));
        }

        // Apply value filter
        if let Some(ref filter) = self.cfg.find_stuck {
            if !filter.is_empty() {
                let vf = filter.to_lowercase();
                stuck.retain(|(_, _, v)| {
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

        // Duration info
        let duration_str = if self.sync_mode {
            if let Some(period) = self.clock_period_ticks {
                if period > 0 {
                    format!("{} cycles", total_ticks / period)
                } else {
                    format!("{}ns", (total_ticks as f64 * self.ticks_to_ns) as i64)
                }
            } else {
                format!("{}ns", (total_ticks as f64 * self.ticks_to_ns) as i64)
            }
        } else {
            format!("{}ns", (total_ticks as f64 * self.ticks_to_ns) as i64)
        };

        // Header
        let total_analyzed = self.stats_data.iter().filter(|s| s.is_some()).count();
        let filter_str = match &self.cfg.find_stuck {
            Some(f) if !f.is_empty() => format!(" (filter: {})", f),
            _ => String::new(),
        };
        write_out!(
            self,
            "# Stuck signals: {} of {} analyzed{}",
            stuck.len(),
            total_analyzed,
            filter_str
        );

        if stuck.is_empty() {
            return;
        }

        // Sort by name
        stuck.sort_by(|a, b| a.0.cmp(&b.0));
        for (name, width_str, stuck_val) in &stuck {
            write_out!(
                self,
                "  {}  {}  stuck at {}  (100%, {})",
                name,
                width_str,
                stuck_val,
                duration_str
            );
        }
    }

    /// Enable index building during this scan.
    pub fn enable_index_building(&mut self, interval: u64) {
        self.index_builder = Some(IndexBuilder::new(interval));
    }

    /// After parsing, finalize the index builder with clock metadata and
    /// write the .idx sidecar file. Silently ignores write errors.
    pub fn write_index_if_ready(&mut self, vcd_path: &std::path::Path) {
        if let Some(mut builder) = self.index_builder.take() {
            // Populate clock metadata from detected values
            if let Some(period) = self.clock_period_ticks {
                builder.clock_period_ticks = period;
            }
            if let Some(first) = self.first_rise_tick {
                builder.first_rise_tick = first;
            }
            // Find clock VCD ID string from clock_idx
            for (id, &idx) in &self.id_str_to_idx {
                if idx == self.clock_idx {
                    builder.clock_id = id.clone();
                    break;
                }
            }
            if let Some(index) = builder.into_index() {
                let _ = index.write_to_file(vcd_path);
            }
        }
    }

    /// Compute the earliest cycle needed based on config.
    /// Returns None if a full scan is required.
    pub fn compute_seek_target(&self) -> Option<u64> {
        // --at-cycle N or --at-time N (when is_cycle)
        if self.cfg.at_time_is_cycle {
            if let Some(at) = self.cfg.at_time {
                if at > 0 {
                    return Some(at as u64);
                }
            }
        }
        // -t START:END in sync mode (time_min is cycle-based in sync)
        if self.sync_mode && self.cfg.time_min > 0 {
            return Some(self.cfg.time_min as u64);
        }
        None
    }

    /// Resume parsing from an indexed position mid-file.
    pub fn resume_from_seek(&mut self, start_cycle: u64, clock_period: u64, first_rise: u64) {
        self.cycle_count = start_cycle as i64;
        self.clock_period_ticks = Some(clock_period);
        self.first_rise_tick = Some(first_rise);
        self.second_rise_tick = Some(first_rise + clock_period);
        self.clock_prev_val = b'0';
        self.rising_edge_pending = false;
        self.reset_active = false;
        self.first_timestamp_seen = true;
        // Don't build index when seeking (incomplete scan)
        self.index_builder = None;
    }

    /// Finalization after parsing completes. Call this after parse_streaming.
    pub fn finalize(&mut self) {
        // Warn if reset never deasserted
        if self.skip_reset && self.reset_idx != NO_SIGNAL && self.reset_active {
            let name = self.reset_name.as_deref().unwrap_or("unknown");
            let _ = writeln!(
                self.stderr,
                "# WARNING: reset '{}' never deasserted — all output suppressed. Use --with-reset to override.",
                name
            );
        }

        // At-time finalization
        if let Some(at_time) = self.cfg.at_time {
            if !self.done {
                if self.sync_mode {
                    if at_time <= self.cycle_count {
                        self.emit_snapshot(&format!("cycle {}", at_time));
                    } else {
                        let sim_end_ns = (self.sim_end_tick as f64 * self.ticks_to_ns) as i64;
                        let sim_start_ns = (self.sim_start_tick as f64 * self.ticks_to_ns) as i64;
                        let _ = writeln!(
                            self.stderr,
                            "ERROR: --at-time {} (cycle) is beyond simulation range (sim length: {} cycles)",
                            at_time, self.cycle_count
                        );
                        // Hint: if the value looks like a timestamp
                        if at_time > self.cycle_count && at_time >= sim_start_ns {
                            let _ = writeln!(
                                self.stderr,
                                "HINT: did you mean --at-time {} with --async? In sync mode, --at-time expects a cycle number (0..{}). Simulation time range: {}ns..{}ns",
                                at_time, self.cycle_count, sim_start_ns, sim_end_ns
                            );
                        }
                    }
                } else {
                    let sim_end_tick = self.sim_end_tick as i64;
                    if at_time <= sim_end_tick {
                        self.emit_snapshot(&format!("{}", at_time));
                    } else {
                        let _ = writeln!(
                            self.stderr,
                            "ERROR: --at-time {} ({}) is beyond simulation range (sim length: {} ticks, timescale: {})",
                            at_time, self.timescale_str, sim_end_tick, self.timescale_str
                        );
                    }
                }
            }
        }

        // Find results summary (matches already streamed during parsing)
        if self.cfg.find_pattern.is_some() {
            if self.cfg.last_match {
                if let Some(line) = self.find_last_result.take() {
                    write_out!(self, "{}", line);
                }
            }
            if self.cfg.count_only {
                write_out!(self, "{}", self.find_count);
            } else if self.find_count > 0 {
                if !self.cfg.first_match && !self.cfg.last_match {
                    let _ = writeln!(self.stderr, "# {} matches found", self.find_count);
                }
            } else {
                let _ = writeln!(self.stderr, "# No matches found");
            }
        }

        // Sample-at summary
        if self.cfg.sample_at_pattern.is_some() {
            if self.cfg.count_only {
                write_out!(self, "{}", self.sample_at_count);
            } else {
                let _ = writeln!(self.stderr, "# {} trigger events", self.sample_at_count);
            }
        }

        // Finalize time-in-state once (used by both stats and find-stuck)
        if self.cfg.stats_mode || self.cfg.find_stuck.is_some() {
            self.finalize_time_in_state();
        }

        // Stats
        if self.cfg.stats_mode {
            self.print_stats();
        }

        // Find-stuck
        if self.cfg.find_stuck.is_some() {
            self.print_find_stuck();
        }

        let _ = self.stdout.flush();
        let _ = self.stderr.flush();
    }

    /// Get the set of all VCD ID strings that need tracking.
    pub fn watched_ids(&self) -> FxHashSet<String> {
        self.id_str_to_idx.keys().cloned().collect()
    }

    // -- Unified value-change handler ----------------------------------

    /// Handle a value change for any signal (scalar or vector).
    /// `formatted` is the display-ready value (e.g. "0", "1", "FF", "x").
    /// `idx` is the numeric signal index.
    fn on_value_change(&mut self, idx: usize, formatted: &str) {
        // Sample-at trigger tracking
        if let Some(&pos) = self.sample_at_idx_to_pos.get(&idx) {
            let prev = &self.sample_at_current_vals[pos];
            if let Some(ref edge) = self.sample_at_edge_mode {
                if Self::check_edge(prev, formatted, edge) {
                    self.sample_at_triggered = true;
                }
            } else if self.sample_at_change_mode {
                // Any-transition mode: fire on every real change
                if prev.as_str() != formatted {
                    self.sample_at_triggered = true;
                }
            }
            self.sample_at_current_vals[pos].clear();
            self.sample_at_current_vals[pos].push_str(formatted);
        }

        // Track whether any watched signal changed (for level-mode sample-at).
        // Use pre-built HashSet for O(1) lookup (was O(N) linear scan -> O(N*M) total).
        if self.watched_sig_idx_set.contains(&idx) {
            self.sample_at_watched_changed = true;
        }

        // Update current value
        if idx >= self.current_vals.len() {
            return; // not a tracked signal
        }
        let needs_prev = !self.sync_mode
            && self.cfg.at_time.is_some()
            && self.cfg.find_pattern.is_none()
            && self.cfg.sample_at_pattern.is_none();
        let prev_val = if needs_prev {
            Some(self.current_vals[idx].clone())
        } else {
            None
        };
        self.current_vals[idx].clear();
        self.current_vals[idx].push_str(formatted);

        // Skip during reset
        if self.skip_reset && self.reset_active {
            return;
        }

        // Stats accumulation (do NOT return early -- find-value/sample-at
        // paths below must still execute when combined with --stats)
        if !self.stats_data.is_empty() {
            if let Some(ref mut sd) = self.stats_data.get_mut(idx).and_then(|o| o.as_mut()) {
                let tick = self.sim_end_tick;
                let elapsed = tick.saturating_sub(sd.last_change_tick);
                if let Some(t) = sd.time_in_state.get_mut(&sd.last_value) {
                    *t += elapsed;
                } else {
                    sd.time_in_state.insert(sd.last_value.clone(), elapsed);
                }
                sd.transitions += 1;
                if let Some(c) = sd.value_hist.get_mut(formatted) {
                    *c += 1;
                } else {
                    sd.value_hist.insert(formatted.to_string(), 1);
                }
                sd.last_change_tick = tick;
                sd.last_value.clear();
                sd.last_value.push_str(formatted);
                // Only return early when stats is the sole output mode
                if self.cfg.find_pattern.is_none() && self.cfg.sample_at_pattern.is_none() {
                    return;
                }
            }
        }

        // Find mode (async)
        if self.cfg.find_pattern.is_some() && !self.sync_mode {
            if self.find_target_indices.contains(&idx) {
                let t = self.sim_end_tick as i64;
                if t < self.time_min_ticks {
                    self.prev_find_vals[idx] = formatted.to_string();
                    return;
                }
                if let Some(max) = self.time_max_ticks {
                    if t > max {
                        self.done = true;
                        return;
                    }
                }
                let matched = if let Some(ref edge) = self.find_edge_mode {
                    let prev = &self.prev_find_vals[idx];
                    let m = Self::check_edge(prev, formatted, edge);
                    self.prev_find_vals[idx] = formatted.to_string();
                    m
                } else {
                    let fv = self.cfg.find_value.as_deref().unwrap_or("");
                    values_match(formatted, fv)
                };
                if matched {
                    self.find_count += 1;
                    if !self.cfg.count_only {
                        let dname = self.display_name(idx).to_string();
                        if self.cfg.last_match {
                            self.find_last_result =
                                Some(format!("{} {} {}", self.sim_end_tick, dname, formatted));
                        } else {
                            write_out!(self, "{} {} {}", self.sim_end_tick, dname, formatted);
                            if self.check_max_lines() {
                                return;
                            }
                        }
                    }
                    if self.cfg.first_match {
                        self.done = true;
                        return;
                    }
                }
            }
            return;
        }

        // Async mode default
        if !self.sync_mode {
            if self.cfg.at_time.is_some() {
                if let Some(at_ticks) = self.at_time_ticks {
                    if (self.sim_end_tick as i64) > at_ticks {
                        // Revert to previous value for snapshot
                        if let Some(pv) = prev_val {
                            self.current_vals[idx] = pv;
                        }
                        let at = self.cfg.at_time.unwrap();
                        let label = format!("{}", at);
                        self.emit_snapshot(&label);
                        return;
                    }
                }
                return;
            }
            if self.cfg.sample_at_pattern.is_some() {
                return;
            }
            // Bug fix: pass formatted value, not raw bits
            self.emit_async(self.sim_end_tick, formatted, idx);
        }
    }
}

impl VcdHandler for Extractor {
    fn on_time_update(&mut self, time: u64, byte_offset: u64) {
        self.sim_end_tick = self.sim_end_tick.max(time);
        self.last_timestamp_offset = byte_offset;
        if !self.first_timestamp_seen {
            self.first_timestamp_seen = true;
            self.sim_start_tick = time;
        }
    }

    fn on_timestamp(&mut self, time: u64) -> ControlFlow<()> {
        self.sim_end_tick = self.sim_end_tick.max(time);

        if self.done {
            return ControlFlow::Break(());
        }

        // --- Async sample-at ---
        if !self.sync_mode && self.cfg.sample_at_pattern.is_some() {
            let t = time as i64;
            if let Some(max) = self.time_max_ticks {
                if t > max {
                    self.done = true;
                    return ControlFlow::Break(());
                }
            }
            let in_range = t >= self.time_min_ticks;
            if in_range {
                let mut triggered = false;
                if self.sample_at_edge_mode.is_some() || self.sample_at_change_mode {
                    // Edge modes (rising/falling/change): trigger flag is set in
                    // on_value_change; just consume it.
                    triggered = self.sample_at_triggered;
                    self.sample_at_triggered = false;
                } else if self.sample_at_level_mode {
                    // Level mode: literal comparison against TRIGGER_VAL (sa_val).
                    // Do NOT use values_match on edge keywords like "rising" --
                    // that was the previous silent-no-op bug.
                    let sa_val = self.cfg.sample_at_value.clone().unwrap_or_default();
                    for i in 0..self.sample_at_current_vals.len() {
                        if values_match(&self.sample_at_current_vals[i], &sa_val) {
                            triggered = true;
                            break;
                        }
                    }
                    if triggered && !self.sample_at_watched_changed {
                        triggered = false;
                    }
                }
                if triggered {
                    let label = time.to_string();
                    self.emit_sample(&label);
                }
            }
            self.sample_at_watched_changed = false;
            return ControlFlow::Continue(());
        }

        if !self.sync_mode {
            return ControlFlow::Continue(());
        }

        // --- Sync mode ---

        // Reset phase gating
        if self.skip_reset && self.reset_active {
            self.rising_edge_pending = false;
            return ControlFlow::Continue(());
        }

        // At-time cycle 0
        if self.cfg.at_time == Some(0) && !self.done {
            self.emit_snapshot("cycle 0 (before first post-reset clock edge)");
            return ControlFlow::Break(());
        }

        if !self.rising_edge_pending {
            return ControlFlow::Continue(());
        }
        self.rising_edge_pending = false;
        self.cycle_count += 1;
        let cycle = self.cycle_count;

        // Record index entry at interval boundaries
        if let Some(ref mut builder) = self.index_builder {
            builder.record(cycle as u64, self.last_timestamp_offset);
        }

        if cycle == 1 {
            self.sim_start_tick = time;
        }

        // At-time snapshot (sync)
        if let Some(at_time) = self.cfg.at_time {
            if cycle < at_time {
                // Track values silently
                for i in 0..self.sig_count {
                    self.prev_emitted_vals[i] = Some(self.current_vals[i].clone());
                }
                return ControlFlow::Continue(());
            }
            self.emit_snapshot(&format!("cycle {}", at_time));
            return ControlFlow::Break(());
        }

        // Time range (cycle-based)
        if self.cfg.time_min > 0 && cycle < self.cfg.time_min {
            for i in 0..self.sig_count {
                self.prev_emitted_vals[i] = Some(self.current_vals[i].clone());
            }
            return ControlFlow::Continue(());
        }
        if let Some(max) = self.cfg.time_max {
            if cycle > max {
                self.done = true;
                return ControlFlow::Break(());
            }
        }

        // Sample-at (sync)
        if self.cfg.sample_at_pattern.is_some() {
            let mut triggered = false;
            if self.sample_at_edge_mode.is_some() || self.sample_at_change_mode {
                // Edge/change mode: trigger flag was set in on_value_change.
                triggered = self.sample_at_triggered;
                self.sample_at_triggered = false;
            } else if self.sample_at_level_mode {
                // Level mode: literal comparison against TRIGGER_VAL (sa_val).
                let sa_val = self.cfg.sample_at_value.clone().unwrap_or_default();
                for i in 0..self.sample_at_current_vals.len() {
                    if values_match(&self.sample_at_current_vals[i], &sa_val) {
                        triggered = true;
                        break;
                    }
                }
            }
            self.sample_at_watched_changed = false;
            if triggered {
                let label = cycle.to_string();
                self.emit_sample(&label);
            }
            return ControlFlow::Continue(());
        }

        // Find mode (sync)
        if self.cfg.find_pattern.is_some() {
            let find_val = self.find_val_cached.clone();
            for ti in 0..self.find_target_indices.len() {
                let idx = self.find_target_indices[ti];
                let matched = if let Some(ref edge) = self.find_edge_mode {
                    let prev = &self.prev_find_vals[idx];
                    let val = &self.current_vals[idx];
                    let m = Self::check_edge(prev, val, edge);
                    self.prev_find_vals[idx] = self.current_vals[idx].clone();
                    m
                } else {
                    values_match(&self.current_vals[idx], &find_val)
                };
                if matched {
                    self.find_count += 1;
                    if !self.cfg.count_only {
                        let dname = self.display_name(idx).to_string();
                        let val = &self.current_vals[idx];
                        if self.cfg.last_match {
                            self.find_last_result =
                                Some(format!("cycle {} {} {}", cycle, dname, val));
                        } else {
                            write_out!(self, "cycle {} {} {}", cycle, dname, val);
                            if self.check_max_lines() {
                                return ControlFlow::Break(());
                            }
                        }
                    }
                    if self.cfg.first_match {
                        self.done = true;
                        return ControlFlow::Break(());
                    }
                }
            }
            // Update prev for level mode
            if self.find_edge_mode.is_none() {
                for ti in 0..self.find_target_indices.len() {
                    let idx = self.find_target_indices[ti];
                    self.prev_find_vals[idx] = self.current_vals[idx].clone();
                }
            }
            return ControlFlow::Continue(());
        }

        // Stats mode: just track prev
        if self.cfg.stats_mode || self.cfg.find_stuck.is_some() {
            for i in 0..self.sig_count {
                self.prev_emitted_vals[i] = Some(self.current_vals[i].clone());
            }
            return ControlFlow::Continue(());
        }

        // Default sync emission
        self.emit_sync_default(cycle);
        if self.done {
            return ControlFlow::Break(());
        }
        ControlFlow::Continue(())
    }

    fn on_scalar(&mut self, id: &str, value: u8) {
        if self.done {
            return;
        }
        let val_str = match value {
            b'0' => "0",
            b'1' => "1",
            b'x' | b'X' => "x",
            b'z' | b'Z' => "z",
            _ => return,
        };

        let idx = match self.id_str_to_idx.get(id) {
            Some(&i) => i,
            None => return,
        };

        // Clock tracking (scalar-only: clock is always 1-bit)
        if self.sync_mode && idx == self.clock_idx {
            if value == b'1' && self.clock_prev_val == b'0' {
                self.rising_edge_pending = true;
                if self.first_rise_tick.is_none() {
                    self.first_rise_tick = Some(self.sim_end_tick);
                } else if self.second_rise_tick.is_none() {
                    self.second_rise_tick = Some(self.sim_end_tick);
                    let period = self.sim_end_tick - self.first_rise_tick.unwrap();
                    self.clock_period_ticks = Some(period);
                    if self.sync_mode {
                        let period_ns = (period as f64 * self.ticks_to_ns) as i64;
                        let _ = writeln!(self.stderr, "# sync: period={}ns", period_ns);
                        self.try_convert_at_time_from_ns();
                    }
                }
            }
            self.clock_prev_val = value;
        }

        if self.skip_reset && idx == self.reset_idx {
            if value == b'0' || value == b'1' {
                let is_asserted = if self.reset_active_low {
                    value == b'0'
                } else {
                    value == b'1'
                };
                if self.reset_active && !is_asserted {
                    self.reset_active = false;
                    // Rebase stats
                    if !self.stats_data.is_empty() {
                        let tick = self.sim_end_tick;
                        for (i, sd) in self.stats_data.iter_mut().enumerate() {
                            if let Some(ref mut entry) = sd {
                                entry.last_change_tick = tick;
                                entry.last_value = self.current_vals[i].clone();
                                entry.time_in_state.clear();
                                entry.value_hist.clear();
                                entry.transitions = 0;
                            }
                        }
                    }
                } else if !self.reset_active && is_asserted {
                    self.reset_active = true;
                }
            }
        }

        // Delegate to unified handler
        self.on_value_change(idx, val_str);
    }

    fn on_vector(&mut self, id: &str, bits: &str) {
        if self.done {
            return;
        }

        let formatted = format_value(bits);

        let idx = match self.id_str_to_idx.get(id) {
            Some(&i) => i,
            None => return,
        };

        // Delegate to unified handler
        self.on_value_change(idx, &formatted);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::parser::{parse_header, parse_streaming};

    /// Helper: create an ExtractConfig with sensible defaults.
    fn default_config() -> ExtractConfig {
        ExtractConfig {
            with_reset: true,
            ..Default::default()
        }
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
1\"
b00001010 #
#15
1!
#20
0!
b11111111 #
#25
1!
#30
0!
";

    #[test]
    fn test_clock_detection() {
        let mut reader = std::io::BufReader::new(BASIC_VCD.as_bytes());
        let header = parse_header(&mut reader);
        let mut ext = Extractor::new(default_config());
        ext.init_from_header(&header, &header.signals).unwrap();

        assert_ne!(ext.clock_idx, NO_SIGNAL);
        assert!(ext.sync_mode);
    }

    #[test]
    fn test_reset_detection() {
        let mut reader = std::io::BufReader::new(BASIC_VCD.as_bytes());
        let header = parse_header(&mut reader);
        let mut cfg = default_config();
        cfg.with_reset = false; // enable reset detection
        let mut ext = Extractor::new(cfg);
        ext.init_from_header(&header, &header.signals).unwrap();

        assert_ne!(ext.reset_idx, NO_SIGNAL);
        assert!(ext.reset_active_low); // "rstn" -> active low
    }

    #[test]
    fn test_cycle_counting() {
        let mut reader = std::io::BufReader::new(BASIC_VCD.as_bytes());
        let header = parse_header(&mut reader);
        let mut cfg = default_config();
        cfg.with_reset = true;
        let mut ext = Extractor::new(cfg);
        ext.init_from_header(&header, &header.signals).unwrap();
        let watched = ext.watched_ids();
        parse_streaming(&mut reader, &watched, &mut ext);

        // Clock rises at #5, #15, #25 = 3 rising edges = 3 cycles
        assert_eq!(ext.cycle_count, 3);
    }

    #[test]
    fn test_clock_period_detection() {
        let mut reader = std::io::BufReader::new(BASIC_VCD.as_bytes());
        let header = parse_header(&mut reader);
        let mut ext = Extractor::new(default_config());
        ext.init_from_header(&header, &header.signals).unwrap();
        let watched = ext.watched_ids();
        parse_streaming(&mut reader, &watched, &mut ext);

        // First rise at #5, second at #15 -> period = 10 ticks
        assert_eq!(ext.clock_period_ticks, Some(10));
    }

    #[test]
    fn test_find_value_sync() {
        let mut reader = std::io::BufReader::new(BASIC_VCD.as_bytes());
        let header = parse_header(&mut reader);
        let mut cfg = default_config();
        cfg.find_pattern = Some("*data*".to_string());
        cfg.find_value = Some("A".to_string());

        cfg.with_reset = true;
        let mut ext = Extractor::new(cfg);
        ext.init_from_header(&header, &header.signals).unwrap();
        let watched = ext.watched_ids();
        parse_streaming(&mut reader, &watched, &mut ext);
        ext.finalize();

        // data becomes 0x0A at #10, first rising edge after is #15 -> cycle 2
        assert!(ext.find_count > 0);
    }

    #[test]
    fn test_find_value_async() {
        let mut reader = std::io::BufReader::new(BASIC_VCD.as_bytes());
        let header = parse_header(&mut reader);
        let mut cfg = default_config();
        cfg.async_mode = true;
        cfg.find_pattern = Some("*data*".to_string());
        cfg.find_value = Some("FF".to_string());

        let mut ext = Extractor::new(cfg);
        ext.init_from_header(&header, &header.signals).unwrap();
        let watched = ext.watched_ids();
        parse_streaming(&mut reader, &watched, &mut ext);
        ext.finalize();

        // data becomes FF at #20 -> 20ns
        assert!(ext.find_count > 0);
    }

    #[test]
    fn test_find_edge_rising() {
        let mut reader = std::io::BufReader::new(BASIC_VCD.as_bytes());
        let header = parse_header(&mut reader);
        let mut cfg = default_config();
        cfg.find_pattern = Some("*rstn*".to_string());
        cfg.find_value = Some("rising".to_string());

        cfg.with_reset = true;
        let mut ext = Extractor::new(cfg);
        ext.init_from_header(&header, &header.signals).unwrap();
        let watched = ext.watched_ids();
        parse_streaming(&mut reader, &watched, &mut ext);
        ext.finalize();

        // rstn goes 0->1 at #10
        assert_eq!(ext.find_count, 1);
    }

    #[test]
    fn test_async_mode_fallback_no_clock() {
        let vcd = "\
$timescale 1ns $end
$scope module tb $end
$var wire 8 ! data [7:0] $end
$upscope $end
$enddefinitions $end
#0
b00000000 !
#10
b00001010 !
";
        let mut reader = std::io::BufReader::new(vcd.as_bytes());
        let header = parse_header(&mut reader);
        let mut cfg = default_config();
        cfg.async_mode = false; // request sync mode
        let mut ext = Extractor::new(cfg);
        ext.init_from_header(&header, &header.signals).unwrap();

        // Should fall back to async since no clock signal found
        assert!(!ext.sync_mode);
    }

    #[test]
    fn test_stats_transitions() {
        let mut reader = std::io::BufReader::new(BASIC_VCD.as_bytes());
        let header = parse_header(&mut reader);
        let mut cfg = default_config();
        cfg.stats_mode = true;
        cfg.with_reset = true;
        let mut ext = Extractor::new(cfg);
        ext.init_from_header(&header, &header.signals).unwrap();
        let watched = ext.watched_ids();
        parse_streaming(&mut reader, &watched, &mut ext);

        // Check that stats were collected for data signal
        let data_idx = *ext.id_str_to_idx.get("#").unwrap();
        let sd = ext.stats_data[data_idx].as_ref().unwrap();
        // data changes: x->00 at #0, 00->0A at #10, 0A->FF at #20 = 3 transitions
        assert_eq!(sd.transitions, 3);
    }

    #[test]
    fn test_max_lines_limit() {
        let mut reader = std::io::BufReader::new(BASIC_VCD.as_bytes());
        let header = parse_header(&mut reader);
        let mut cfg = default_config();
        cfg.with_reset = true;
        cfg.max_lines = 2; // very low limit
        let mut ext = Extractor::new(cfg);
        ext.init_from_header(&header, &header.signals).unwrap();
        let watched = ext.watched_ids();
        parse_streaming(&mut reader, &watched, &mut ext);

        assert!(ext.done);
        assert!(ext.line_count >= 2);
    }

    #[test]
    fn test_find_value_max_lines() {
        let mut reader = std::io::BufReader::new(BASIC_VCD.as_bytes());
        let header = parse_header(&mut reader);
        let mut cfg = default_config();
        cfg.find_pattern = Some("*rstn*".to_string());
        cfg.find_value = Some("1".to_string());

        cfg.with_reset = true;
        cfg.max_lines = 1;
        let mut ext = Extractor::new(cfg);
        ext.init_from_header(&header, &header.signals).unwrap();
        let watched = ext.watched_ids();
        parse_streaming(&mut reader, &watched, &mut ext);

        // rstn=1 from cycle 2 onward (cycles 2 and 3 match),
        // but max_lines=1 should stop after the first match
        assert_eq!(ext.find_count, 1);
        assert!(ext.done);
    }

    #[test]
    fn test_sample_at_error_no_match() {
        let mut reader = std::io::BufReader::new(BASIC_VCD.as_bytes());
        let header = parse_header(&mut reader);
        let mut cfg = default_config();
        cfg.sample_at_pattern = Some("*nonexistent*".to_string());
        cfg.sample_at_value = Some("1".to_string());
        let mut ext = Extractor::new(cfg);
        let result = ext.init_from_header(&header, &header.signals);

        assert!(result.is_err());
        assert!(result.unwrap_err().contains("no signals match"));
    }

    #[test]
    fn test_value_formatting_in_current_vals() {
        let vcd = "\
$timescale 1ns $end
$scope module tb $end
$var wire 8 ! data [7:0] $end
$upscope $end
$enddefinitions $end
#0
b00001111 !
#10
b11111111 !
";
        let mut reader = std::io::BufReader::new(vcd.as_bytes());
        let header = parse_header(&mut reader);
        let mut cfg = default_config();
        cfg.async_mode = true;
        cfg.find_pattern = Some("*data*".to_string());
        cfg.find_value = Some("F".to_string());

        let mut ext = Extractor::new(cfg);
        ext.init_from_header(&header, &header.signals).unwrap();
        let watched = ext.watched_ids();
        parse_streaming(&mut reader, &watched, &mut ext);
        ext.finalize();

        // b00001111 = 0F, should match "F" via values_match
        assert!(ext.find_count >= 1);
    }

    #[test]
    fn test_at_time_ns_auto_conversion() {
        // VCD starting at tick 1000, 10ns clock period → cycle 1 at tick 1005
        // User passes --at-time 1025 (meaning 1025ns), should auto-convert to cycle 2
        let vcd = "\
$timescale 1ns $end
$scope module tb $end
$var wire 1 ! clk $end
$var wire 8 # data [7:0] $end
$upscope $end
$enddefinitions $end
#1000
0!
b00000000 #
#1005
1!
#1010
0!
b00001010 #
#1015
1!
#1020
0!
b11111111 #
#1025
1!
#1030
0!
b00000001 #
#1035
1!
";
        let mut reader = std::io::BufReader::new(vcd.as_bytes());
        let header = parse_header(&mut reader);
        let mut cfg = default_config();
        cfg.with_reset = true;
        cfg.at_time = Some(1025); // ns timestamp, should auto-convert to cycle 2
        let mut ext = Extractor::new(cfg);
        ext.init_from_header(&header, &header.signals).unwrap();
        let watched = ext.watched_ids();
        parse_streaming(&mut reader, &watched, &mut ext);

        // After clock period detection, at_time should be converted from 1025ns
        // to cycle 2: (1025 - 1005) / 10 = 2
        assert_eq!(ext.cfg.at_time, Some(2));
    }

    // ---- Regression tests for bug #4: --sample-mode level/rising ----
    //
    // Previously, --sample-mode level with a TRIGGER_VAL that parsed as an
    // edge keyword (e.g. "rising") would call values_match("1","rising"),
    // which always returns false -> silent no-op. And --sample-mode accepted
    // only {edge, level}, so users couldn't say "rising" directly.
    //
    // These tests lock in the corrected branching:
    //   - level   : literal value comparison
    //   - rising  : edge detection 0 -> 1 on trigger signal

    /// Async sample-at, level mode: trigger every timestamp where the trigger
    /// signal is literally '1' AND some watched signal changed that timestamp.
    ///
    /// VCD trigger 'flag' pulses high at #10 and #30. Watched signal 'data'
    /// changes at the same timestamps. Expect exactly 2 trigger events.
    #[test]
    fn test_sample_at_level_mode_matches_literal_one() {
        let vcd = "\
$timescale 1ns $end
$scope module tb $end
$var wire 1 ! flag $end
$var wire 8 # data [7:0] $end
$upscope $end
$enddefinitions $end
#0
0!
b00000000 #
#10
1!
b00001010 #
#20
0!
b00001111 #
#30
1!
b11111111 #
";
        let mut reader = std::io::BufReader::new(vcd.as_bytes());
        let header = parse_header(&mut reader);
        let mut cfg = default_config();
        cfg.async_mode = true;
        cfg.patterns = vec!["*data*".to_string()];
        cfg.sample_at_pattern = Some("*flag*".to_string());
        cfg.sample_at_value = Some("1".to_string());
        let mut ext = Extractor::new(cfg);
        ext.init_from_header(&header, &header.signals).unwrap();
        let watched = ext.watched_ids();
        parse_streaming(&mut reader, &watched, &mut ext);
        ext.finalize();

        // flag==1 and data changed at #10 and #30 -> 2 triggers
        assert_eq!(
            ext.sample_at_count, 2,
            "level mode with VALUE='1' should fire at timestamps where flag==1 and data changed"
        );
    }

    /// Async sample-at, rising mode: trigger on 0->1 transition of trigger
    /// signal. VALUE='rising' is the keyword.
    ///
    /// flag rises 0->1 at #10 and again at #30. Expect 2 trigger events.
    #[test]
    fn test_sample_at_rising_mode_detects_transition() {
        let vcd = "\
$timescale 1ns $end
$scope module tb $end
$var wire 1 ! flag $end
$var wire 8 # data [7:0] $end
$upscope $end
$enddefinitions $end
#0
0!
b00000000 #
#10
1!
b00001010 #
#20
0!
b00001111 #
#30
1!
b11111111 #
";
        let mut reader = std::io::BufReader::new(vcd.as_bytes());
        let header = parse_header(&mut reader);
        let mut cfg = default_config();
        cfg.async_mode = true;
        cfg.patterns = vec!["*data*".to_string()];
        cfg.sample_at_pattern = Some("*flag*".to_string());
        cfg.sample_at_value = Some("rising".to_string());
        let mut ext = Extractor::new(cfg);
        ext.init_from_header(&header, &header.signals).unwrap();
        let watched = ext.watched_ids();
        parse_streaming(&mut reader, &watched, &mut ext);
        ext.finalize();

        // flag rose at #10 and #30 -> 2 triggers
        assert_eq!(
            ext.sample_at_count, 2,
            "rising mode should trigger on 0->1 transitions of trigger signal"
        );
    }

    /// Async sample-at, change mode: trigger on any transition of trigger signal.
    /// VALUE='change' is the keyword.
    ///
    /// flag transitions at #10 (0->1) and #20 (1->0) and #30 (0->1). Expect 3 triggers.
    #[test]
    fn test_sample_at_change_mode_detects_any_transition() {
        let vcd = "\
$timescale 1ns $end
$scope module tb $end
$var wire 1 ! flag $end
$var wire 8 # data [7:0] $end
$upscope $end
$enddefinitions $end
#0
0!
b00000000 #
#10
1!
b00001010 #
#20
0!
b00001111 #
#30
1!
b11111111 #
";
        let mut reader = std::io::BufReader::new(vcd.as_bytes());
        let header = parse_header(&mut reader);
        let mut cfg = default_config();
        cfg.async_mode = true;
        cfg.patterns = vec!["*data*".to_string()];
        cfg.sample_at_pattern = Some("*flag*".to_string());
        cfg.sample_at_value = Some("change".to_string());
        let mut ext = Extractor::new(cfg);
        ext.init_from_header(&header, &header.signals).unwrap();
        let watched = ext.watched_ids();
        parse_streaming(&mut reader, &watched, &mut ext);
        ext.finalize();

        // flag transitions at #0 (x->0), #10 (0->1), #20 (1->0), #30 (0->1) -> 4 triggers
        assert_eq!(
            ext.sample_at_count, 4,
            "change mode should trigger on any transition of trigger signal"
        );
    }

    // ---- Phase 2: Additional extract.rs regression tests (1E) ----

    #[test]
    fn test_at_time_cycle_0_sync() {
        let mut reader = std::io::BufReader::new(BASIC_VCD.as_bytes());
        let header = parse_header(&mut reader);
        let mut cfg = default_config();
        cfg.with_reset = true;
        cfg.at_time = Some(0);
        cfg.at_time_is_cycle = true;
        let mut ext = Extractor::new(cfg);
        ext.init_from_header(&header, &header.signals).unwrap();
        let watched = ext.watched_ids();
        parse_streaming(&mut reader, &watched, &mut ext);
        ext.finalize();

        // cycle 0 snapshot should emit before first post-reset edge
        assert!(ext.done, "should be done after cycle 0 snapshot");
    }

    #[test]
    fn test_stats_with_signal_never_changes() {
        // A VCD where one signal never transitions after initial value
        let vcd = "\
$timescale 1ns $end
$scope module tb $end
$var wire 1 ! clk $end
$var wire 1 \" stuck $end
$upscope $end
$enddefinitions $end
#0
0!
1\"
#5
1!
#10
0!
#15
1!
#20
0!
";
        let mut reader = std::io::BufReader::new(vcd.as_bytes());
        let header = parse_header(&mut reader);
        let mut cfg = default_config();
        cfg.stats_mode = true;
        cfg.with_reset = true;
        cfg.patterns = vec!["*stuck*".to_string()];
        let mut ext = Extractor::new(cfg);
        ext.init_from_header(&header, &header.signals).unwrap();
        let watched = ext.watched_ids();
        parse_streaming(&mut reader, &watched, &mut ext);

        // stuck signal: initial set counts as 1 transition, then stays at 1
        let stuck_idx = *ext.id_str_to_idx.get("\"").unwrap();
        let sd = ext.stats_data[stuck_idx].as_ref().unwrap();
        assert_eq!(
            sd.transitions, 1,
            "stuck signal should have 1 transition (initial set)"
        );
    }

    #[test]
    fn test_multiple_clock_candidates_priority() {
        // Multiple signals contain "clk" — should pick shallowest scope first
        let vcd = "\
$timescale 1ns $end
$scope module tb $end
$scope module dut $end
$scope module sub $end
$var wire 1 ! sub_clk $end
$upscope $end
$var wire 1 \" dut_clk $end
$upscope $end
$var wire 1 # tb_clk $end
$upscope $end
$enddefinitions $end
#0
0!
0\"
0#
#5
1#
1\"
1!
";
        let mut reader = std::io::BufReader::new(vcd.as_bytes());
        let header = parse_header(&mut reader);
        let mut cfg = default_config();
        cfg.with_reset = true;
        let mut ext = Extractor::new(cfg);
        ext.init_from_header(&header, &header.signals).unwrap();

        // tb_clk (depth 1) should win over dut_clk (depth 2) and sub_clk (depth 3)
        assert_ne!(ext.clock_idx, NO_SIGNAL);
    }

    #[test]
    fn test_find_value_with_wide_signal() {
        // Find a specific 256-bit value
        let hex_val = "DEADBEEF".to_string() + &"0".repeat(56); // 256-bit
        let bin_val = format!(
            "{}{}",
            "11011110101011011011111011101111", // DEADBEEF
            "0".repeat(224)
        );
        let vcd = format!(
            "$timescale 1ns $end\n\
$scope module tb $end\n\
$var wire 256 ! wide [255:0] $end\n\
$upscope $end\n\
$enddefinitions $end\n\
#0\n\
b{} !\n\
#10\n\
b{} !\n",
            "0".repeat(256),
            bin_val,
        );
        let mut reader = std::io::BufReader::new(vcd.as_bytes());
        let header = parse_header(&mut reader);
        let mut cfg = default_config();
        cfg.async_mode = true;
        cfg.find_pattern = Some("*wide*".to_string());
        cfg.find_value = Some(hex_val);

        let mut ext = Extractor::new(cfg);
        ext.init_from_header(&header, &header.signals).unwrap();
        let watched = ext.watched_ids();
        parse_streaming(&mut reader, &watched, &mut ext);
        ext.finalize();

        assert_eq!(ext.find_count, 1, "should find exactly one 256-bit match");
    }

    #[test]
    fn test_finalize_time_in_state_correct_totals() {
        // Regression: finalize_time_in_state must produce correct totals
        // when called once. Previously both print_stats() and
        // print_find_stuck() each added final_elapsed independently,
        // doubling the last state's time when both modes were active.
        let vcd = "\
$timescale 1ns $end
$scope module tb $end
$var wire 1 ! clk $end
$var wire 1 # sig $end
$upscope $end
$enddefinitions $end
#0
0!
0#
#5
1!
#10
0!
1#
#15
1!
#20
0!
#25
1!
#30
0!
";
        // sig: 0 from #0 to #10 (10 ticks), 1 from #10 to #30 (20 ticks)
        let mut reader = std::io::BufReader::new(vcd.as_bytes());
        let header = parse_header(&mut reader);
        let mut cfg = default_config();
        cfg.stats_mode = true;
        cfg.find_stuck = Some("*".to_string());
        cfg.patterns = vec!["*sig*".to_string()];
        let mut ext = Extractor::new(cfg);
        ext.init_from_header(&header, &header.signals).unwrap();
        let watched = ext.watched_ids();
        parse_streaming(&mut reader, &watched, &mut ext);

        ext.finalize_time_in_state();

        let sig_idx = *ext.id_str_to_idx.get("#").unwrap();
        let sd = ext.stats_data[sig_idx].as_ref().unwrap();

        let time_0 = sd.time_in_state.get("0").copied().unwrap_or(0);
        let time_1 = sd.time_in_state.get("1").copied().unwrap_or(0);
        let total = time_0 + time_1;

        assert_eq!(
            total, 30,
            "total time_in_state should equal sim duration (30 ticks)"
        );
        assert_eq!(time_0, 10, "sig=0 from tick 0 to 10");
        assert_eq!(time_1, 20, "sig=1 from tick 10 to 30");
    }
}
