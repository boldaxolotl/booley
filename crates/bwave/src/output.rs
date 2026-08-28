//! JSON envelope + per-command data shapes for `--format json`.
//!
//! Every JSON emission goes through `emit_json`, which wraps a typed
//! payload in the canonical `{$schema, command, data, warnings}` envelope.
//! The schema URL is pinned to a git tag; the schema itself lives at
//! `schema/bwave.json` and is served by the `bwave schema` subcommand.

use std::io::{self, BufWriter, Write};

use serde::Serialize;

/// Pinned `$schema` URL. Bump alongside the git tag at release-cut time.
pub const SCHEMA_URL: &str =
    "https://raw.githubusercontent.com/boldaxolotl/booley/v0.2.7/crates/bwave/schema/bwave.json";

/// Canonical envelope. Every `--format json` payload is wrapped in this shape.
#[derive(Serialize)]
pub struct JsonEnvelope<'a, T: Serialize> {
    #[serde(rename = "$schema")]
    pub schema: &'static str,
    pub command: &'a str,
    pub data: T,
    pub warnings: Vec<String>,
}

/// Serialize and write the envelope to stdout (pretty-printed, trailing newline).
/// Errors are swallowed — the harness already exits non-zero on upstream failure.
pub fn emit_json<T: Serialize>(command: &str, data: T, warnings: Vec<String>) {
    let env = JsonEnvelope {
        schema: SCHEMA_URL,
        command,
        data,
        warnings,
    };
    let stdout = io::stdout();
    let mut out = BufWriter::new(stdout.lock());
    let _ = serde_json::to_writer_pretty(&mut out, &env);
    let _ = writeln!(out);
    let _ = out.flush();
}

// -- Per-command data shapes ----------------------------------------

/// `list` — signal names + widths under an optional common scope prefix.
#[derive(Serialize)]
pub struct ListData {
    pub scope_prefix: String,
    /// Distinct first-level scopes represented by the matched signals.
    pub root_scopes: Vec<String>,
    /// Total matched signals before `--limit` is applied to `signals`.
    pub signal_count: usize,
    /// Full hierarchical name of the detected (or `--clock`-overridden) clock;
    /// `None` on an async trace. Trace-level — not filtered by the pattern.
    ///
    /// It rides on `list` rather than `stats` because `stats` walks every
    /// transition of every signal (minutes on a multi-hundred-MB store) while
    /// `list` only reads the hierarchy. `bwave gui` needs the clock for row 1
    /// of every view and already calls `list` to expand its globs, so this
    /// costs it nothing.
    pub clock: Option<String>,
    /// End of the trace, in file-timescale ticks — the same number `stats`
    /// reports, here for the same reason as `clock`: resolving an open-ended
    /// time range (`--time 100c:`) should not cost a full-trace stats walk.
    pub total_ticks: u64,
    pub signals: Vec<SignalEntry>,
}

#[derive(Serialize)]
pub struct SignalEntry {
    pub name: String,
    pub width: u32,
    pub var_type: String,
}

/// `value` — snapshot of N signals at one time point.
#[derive(Serialize)]
pub struct ValueData {
    pub scope_prefix: String,
    pub mode: String,       // "sync" | "async"
    pub at: i64,            // user-provided time/cycle
    pub at_unit: String,    // "cycle" | "tick" ("ns" for legacy unresolved input)
    pub target_tick: u64,   // resolved cache tick
    pub time_label: String, // matches the text-mode "# Snapshot at ..." label
    pub signals: Vec<SignalValue>,
}

#[derive(Serialize)]
pub struct SignalValue {
    pub name: String,
    pub value: String,
}

/// `find` — every cycle/tick where PATTERN matched VALUE/edge.
#[derive(Serialize)]
pub struct FindData {
    pub scope_prefix: String,
    pub pattern: String,
    pub value: String, // edge keyword or literal — as the user passed it
    pub mode: String,  // "sync" | "async"
    pub unit: String,  // "cycle" | "tick"
    pub count: usize,
    pub matches: Vec<FindMatch>,
    pub truncated: bool,
    pub first_only: bool,
    pub last_only: bool,
    pub count_only: bool,
}

#[derive(Serialize)]
pub struct FindMatch {
    pub time: u64,
    pub name: String,
    pub value: String,
}

/// `stats` — wraps the existing `CacheStatsEntry` list. Header data lifted
/// out of the previous flat shape (`simulation_ns`, plus sync-mode summary).
#[derive(Serialize)]
pub struct StatsData<E: Serialize> {
    pub simulation_ns: i64,
    pub total_ticks: u64,
    pub total_cycles: Option<u64>,
    pub clock_period_ns: Option<i64>,
    pub signals: Vec<E>,
}

/// `stats` per-signal JSON shape — distinct from the internal `CacheStatsEntry`
/// because the JSON form (a) formats histogram keys as Verilog literals so
/// they match the text-mode rendering exactly, and (b) renames the duration
/// map to make the unit obvious in the key (`time_in_state_ticks`, with a
/// companion `_ns` map populated when the simulation has a known timescale).
/// Map iteration order isn't stable, but we use `BTreeMap` so JSON keys come
/// out sorted — handy for diffs.
#[derive(Serialize)]
pub struct JsonStatsEntry {
    pub name: String,
    pub width: u32,
    pub transitions: u64,
    pub toggle_pct: f64,
    pub value_pct: f64,
    /// `value_hist` keys are Verilog literals (`'hFF`, `'d255`, `'b101`)
    /// matching the per-signal radix display. v0.1 emitted raw cache values
    /// here, which made downstream parsing inconsistent with text mode.
    pub value_hist: std::collections::BTreeMap<String, u64>,
    /// Time-in-state in raw ticks. Renamed from `time_in_state` so the unit
    /// is unambiguous in the JSON; old key was easy to confuse with ns.
    pub time_in_state_ticks: std::collections::BTreeMap<String, u64>,
    /// Same data as `time_in_state_ticks` but converted to ns via the cache
    /// timescale. `None` only when no timescale is recorded in the VCD.
    pub time_in_state_ns: Option<std::collections::BTreeMap<String, i64>>,
}
