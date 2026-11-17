//! JSON envelope + per-command data shapes for `--format json`.
//!
//! Every JSON emission goes through `emit_json`, which wraps a typed
//! payload in the canonical `{$schema, command, data, warnings}` envelope.
//! The schema URL is pinned to the release tag; the schema itself lives at
//! `schema/bwave.json` and is served by the `bwave schema` subcommand.

use std::io::{self, BufWriter, Write};

use schemars::{generate::SchemaSettings, transform::RecursiveTransform, JsonSchema, Schema};
use serde::Serialize;
use serde_json::{json, Value};

/// Pinned `$schema` URL. Bump alongside the git tag at release-cut time.
pub const SCHEMA_URL: &str =
    "https://raw.githubusercontent.com/boldaxolotl/booley/v0.4.0/crates/bwave/schema/bwave.json";

/// B-Wave JSON envelope revision 2, emitted by `--format json` on list, value,
/// find, stats, signal, wave, distance and overview; sample, diff and stuck are
/// text-only and exit 2 on `--format json`. Clock-grid diagnostics and stopped
/// ranges are advisory. The `reset` object reports the reset the query used.
/// `bwave schema` prints the revision the installed binary emits.
// Every `--format json` payload is wrapped in this shape. `///` text on the
// envelope and data types is published as the schema's descriptions.
#[derive(Serialize, JsonSchema)]
#[schemars(title = "B-Wave --format json envelope", deny_unknown_fields)]
pub struct JsonEnvelope<'a, T: Serialize> {
    /// Immutable URL of the expanded schema revision.
    #[serde(rename = "$schema")]
    pub schema: &'static str,
    /// Which subcommand produced this envelope.
    pub command: &'a str,
    pub data: T,
    /// Diagnostics that text mode would write to stderr as `# WARNING: ...`
    /// lines, including --limit truncation. Empty on a clean run.
    pub warnings: Vec<String>,
    /// Verdict on the selected clock. Present on sync queries and on async
    /// queries that use cycle tokens; absent on async queries with only
    /// tick/time tokens. A `warning` or `unavailable` verdict is repeated in
    /// `warnings`.
    #[serde(skip_serializing_if = "Option::is_none")]
    #[schemars(with = "crate::clock::ClockVerdict")]
    pub clock_regularity: Option<crate::clock::ClockVerdict>,
    /// Present only when the query window overlaps a clock stop. Each entry is
    /// the whole stop, even when the window covers part of it, numbered on the
    /// query's reset-aware grid. Not capped by --limit.
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub clock_stopped: Vec<crate::clock::StoppedRange>,
    /// The reset the query used and the cycles it skipped. Present on sync
    /// queries and `overview`; absent on async queries and `list`.
    #[serde(skip_serializing_if = "Option::is_none")]
    #[schemars(with = "crate::reset::ResetReport")]
    pub reset: Option<crate::reset::ResetReport>,
}

/// Each JSON command and the `$defs` entry of the `data` it emits.
fn data_shapes(generator: &mut schemars::SchemaGenerator) -> [(&'static str, Schema); 8] {
    [
        ("list", generator.subschema_for::<ListData>()),
        ("value", generator.subschema_for::<ValueData>()),
        ("find", generator.subschema_for::<FindData>()),
        (
            "stats",
            generator.subschema_for::<StatsData<JsonStatsEntry>>(),
        ),
        ("signal", generator.subschema_for::<SignalData>()),
        ("wave", generator.subschema_for::<WaveData<'static>>()),
        ("distance", generator.subschema_for::<DistanceData>()),
        ("overview", generator.subschema_for::<OverviewData>()),
    ]
}

/// Drop `format` keywords and join wrapped doc-comment lines.
fn tidy(schema: &mut Schema) {
    schema.remove("format");
    if schema.get("const").is_some() {
        schema.remove("type");
    }
    if let Some(Value::String(text)) = schema.get_mut("description") {
        *text = text.replace('\n', " ");
    }
}

/// Keep an enum's spelling without `type`, as the published schema has it.
pub(crate) fn without_type(schema: &mut Schema) {
    schema.remove("type");
}

/// Keep `sample_tick` last in `valueData.required`, as the published schema has it.
fn sample_tick_last(schema: &mut Schema) {
    if let Some(Value::Array(required)) = schema.get_mut("required") {
        required.retain(|key| key != "sample_tick");
        required.push(json!("sample_tick"));
    }
}

/// The published `--format json` schema, generated from the types below.
/// `bwave schema` prints it; `schema/bwave.json` is its committed copy.
pub fn schema_json() -> String {
    let mut generator = SchemaSettings::draft2020_12()
        .for_serialize()
        .with_transform(RecursiveTransform(tidy))
        .into_generator();
    let shapes = data_shapes(&mut generator);
    let mut root = Value::from(generator.root_schema_for::<JsonEnvelope<'static, ()>>());
    root["$id"] = json!(SCHEMA_URL);
    root["properties"]["command"]["enum"] = json!(shapes.iter().map(|s| s.0).collect::<Vec<_>>());
    root["properties"]["data"] =
        json!({ "oneOf": shapes.iter().map(|s| &s.1).collect::<Vec<_>>() });
    root["allOf"] = shapes
        .iter()
        .map(|(command, data)| {
            json!({
                "if": { "properties": { "command": { "const": command } } },
                "then": { "properties": { "data": data } },
            })
        })
        .collect();
    serde_json::to_string_pretty(&root).expect("schema serializes") + "\n"
}

/// Serialize and write the envelope to stdout (pretty-printed, trailing newline).
/// Errors are swallowed — the harness already exits non-zero on upstream failure.
pub fn emit_json<T: Serialize>(command: &str, data: T, warnings: Vec<String>) {
    emit_clock_json(command, data, warnings, None, Vec::new(), None);
}

pub fn emit_clock_json<T: Serialize>(
    command: &str,
    data: T,
    warnings: Vec<String>,
    clock_regularity: Option<crate::clock::ClockVerdict>,
    clock_stopped: Vec<crate::clock::StoppedRange>,
    reset: Option<crate::reset::ResetReport>,
) {
    let env = JsonEnvelope {
        schema: SCHEMA_URL,
        command,
        data,
        warnings,
        clock_regularity,
        clock_stopped,
        reset,
    };
    let stdout = io::stdout();
    let mut out = BufWriter::new(stdout.lock());
    let _ = serde_json::to_writer_pretty(&mut out, &env);
    let _ = writeln!(out);
    let _ = out.flush();
}

// -- Per-command data shapes ----------------------------------------

// `list` — signal names + widths under an optional common scope prefix.
#[derive(Serialize, JsonSchema)]
#[schemars(rename = "listData", deny_unknown_fields)]
pub struct ListData {
    /// Common hierarchical prefix of all matched signals (without trailing
    /// dot). Empty when matches span multiple scopes or are top-level.
    pub scope_prefix: String,
    /// Distinct first-level scopes represented by the matched signals. Unlike
    /// scope_prefix, this remains populated when a trace has multiple roots.
    pub root_scopes: Vec<String>,
    /// Total matched signals before --limit is applied to the signals array.
    pub signal_count: usize,
    /// Full hierarchical name of the detected (or --clock-overridden) clock;
    /// null on an async trace. Trace-level, not filtered by the pattern.
    /// `bwave gui` makes it row 1 of a new view.
    // It rides on `list` rather than `stats` because `stats` walks every
    // transition of every signal (minutes on a multi-hundred-MB store) while
    // `list` only reads the hierarchy. `bwave gui` already calls `list` to
    // expand its globs, so this costs it nothing.
    pub clock: Option<String>,
    /// End of the trace in file-timescale ticks — same value `stats` reports.
    /// Trace-level, not filtered by the pattern; here so an open-ended time
    /// range does not cost a full stats walk.
    pub total_ticks: u64,
    pub signals: Vec<SignalEntry>,
}

#[derive(Serialize, JsonSchema)]
#[schemars(inline, deny_unknown_fields)]
pub struct SignalEntry {
    /// Signal name with `scope_prefix` stripped.
    pub name: String,
    #[schemars(range(min = 1))]
    pub width: u32,
    /// VCD var type — `wire`, `reg`, `integer`, etc.
    pub var_type: String,
}

// `value` — snapshot of N signals at one time point.
#[derive(Serialize, JsonSchema)]
#[schemars(rename = "valueData", deny_unknown_fields, transform = sample_tick_last)]
pub struct ValueData {
    pub scope_prefix: String,
    #[schemars(extend("enum" = ["sync", "async"]))]
    pub mode: String,
    /// User-supplied time argument verbatim.
    pub at: i64,
    /// Unit of `at`. `cycle` in sync mode; `tick` when sync was requested but
    /// no clock was found; `ns` in async mode.
    // "ns" only for legacy unresolved input.
    #[schemars(extend("enum" = ["cycle", "tick", "ns"]))]
    pub at_unit: String,
    pub sample_tick: Option<u64>,
    /// Resolved simulation tick where the snapshot was taken.
    pub target_tick: u64,
    /// Human label matching text mode's `# Snapshot at <label>` line.
    pub time_label: String,
    pub signals: Vec<SignalValue>,
}

#[derive(Serialize, JsonSchema)]
#[schemars(rename = "signalValue", deny_unknown_fields)]
pub struct SignalValue {
    pub name: String,
    /// Raw store value with no Verilog-literal prefix, padding or radix
    /// formatting (a 3-bit 5 is "5", 0xDEADBEEF is "DEADBEEF"); X and Z appear
    /// literally. Hex/binary mix per store encoding. The -s %RADIX suffix does
    /// not apply to JSON.
    pub value: String,
}

// `find` — every cycle/tick where PATTERN matched VALUE/edge.
#[derive(Serialize, JsonSchema)]
#[schemars(rename = "findData", deny_unknown_fields)]
pub struct FindData {
    pub scope_prefix: String,
    /// Find-target glob.
    pub pattern: String,
    /// Edge keyword (`rising`, `falling`, `change`), or the literal value
    /// normalized like signalValue.value ('d255 is "FF").
    pub value: String,
    #[schemars(extend("enum" = ["sync", "async"]))]
    pub mode: String,
    /// Unit of `time` in each match. `cycle` in sync mode with a clock; `tick`
    /// otherwise.
    #[schemars(extend("enum" = ["cycle", "tick"]))]
    pub unit: String,
    pub count: usize,
    /// Empty when `count_only`. Contains the single match when `last_only`.
    pub matches: Vec<FindMatch>,
    /// True when output was capped by --limit.
    pub truncated: bool,
    pub first_only: bool,
    pub last_only: bool,
    pub count_only: bool,
}

#[derive(Serialize, JsonSchema)]
#[schemars(rename = "findMatch", deny_unknown_fields)]
pub struct FindMatch {
    pub time: u64,
    pub name: String,
    /// Raw cache value (see signalValue.value).
    pub value: String,
}

// `stats` — wraps the existing `CacheStatsEntry` list. Header data lifted
// out of the previous flat shape (`simulation_ns`, plus sync-mode summary).
#[derive(Serialize, JsonSchema)]
#[schemars(rename = "statsData", deny_unknown_fields)]
pub struct StatsData<E: Serialize> {
    pub simulation_ns: i64,
    pub total_ticks: u64,
    /// Null when no clock was found.
    pub total_cycles: Option<u64>,
    #[schemars(range(min = 0))]
    pub clock_period_ns: Option<i64>,
    pub signals: Vec<E>,
}

// `stats` per-signal JSON shape — distinct from the internal `CacheStatsEntry`
// because the JSON form (a) formats histogram keys as Verilog literals so
// they match the text-mode rendering exactly, and (b) renames the duration
// map to make the unit obvious in the key (`time_in_state_ticks`, with a
// companion `_ns` map populated when the simulation has a known timescale).
// Map iteration order isn't stable, but we use `BTreeMap` so JSON keys come
// out sorted — handy for diffs.
#[derive(Serialize, JsonSchema)]
#[schemars(rename = "statsEntry", deny_unknown_fields)]
pub struct JsonStatsEntry {
    pub name: String,
    #[schemars(range(min = 1))]
    pub width: u32,
    pub transitions: u64,
    pub toggle_pct: f64,
    pub value_pct: f64,
    /// Map of Verilog-literal value (e.g. "'hFF", "'d255", "'b101") ->
    /// occurrence count. Matches the text-mode display radix per signal.
    // v0.1 emitted raw cache values here, which made downstream parsing
    // inconsistent with text mode.
    pub value_hist: std::collections::BTreeMap<String, u64>,
    /// Map of Verilog-literal value -> tick count held. Renamed from
    /// `time_in_state` in v0.2.1 so the unit is unambiguous; pair with
    /// `time_in_state_ns` if you want nanoseconds.
    pub time_in_state_ticks: std::collections::BTreeMap<String, u64>,
    /// Same keys as `time_in_state_ticks`, values converted to ns via the cache
    /// timescale. Null when no timescale is recorded in the VCD.
    // Always emitted, but the published schema has never required it.
    #[schemars(skip_serializing_if = "Option::is_none")]
    pub time_in_state_ns: Option<std::collections::BTreeMap<String, i64>>,
    pub time_in_state_clocked_cycles: Option<std::collections::BTreeMap<String, u64>>,
    pub time_in_state_stopped_cycles: Option<std::collections::BTreeMap<String, u64>>,
}

/// Combine diagnostics once without process-global state.
pub fn merge_warnings(mut warnings: Vec<String>, inherited: &[String]) -> Vec<String> {
    for warning in inherited {
        if !warnings.contains(warning) {
            warnings.push(warning.clone());
        }
    }
    warnings
}

#[derive(Serialize, JsonSchema)]
#[schemars(rename = "overviewData", deny_unknown_fields)]
pub struct OverviewData {
    pub total_ticks: u64,
    pub signal_count: usize,
    pub period_ticks: u64,
    pub first_rise_tick: u64,
}

#[derive(Serialize, JsonSchema)]
#[serde(tag = "kind", rename_all = "snake_case")]
#[schemars(rename = "signalRecord", deny_unknown_fields)]
pub enum SignalRecord {
    Value {
        time: u64,
        name: String,
        value: String,
    },
    ClockStopped {
        from_cycle: u64,
        to_cycle: u64,
    },
}

#[derive(Serialize, JsonSchema)]
#[schemars(rename = "signalData", deny_unknown_fields)]
pub struct SignalData {
    #[schemars(extend("enum" = ["sync", "async"]), transform = without_type)]
    pub mode: String,
    pub records: Vec<SignalRecord>,
    pub truncated: bool,
}

#[derive(Serialize, JsonSchema)]
#[schemars(rename = "waveData", deny_unknown_fields)]
pub struct WaveData<'a> {
    #[schemars(extend("enum" = ["sync", "async"]), transform = without_type)]
    pub mode: String,
    pub columns: &'a [String],
    pub ticks: &'a [u64],
    pub names: &'a [String],
    pub values: &'a [Vec<String>],
}

#[derive(Serialize, JsonSchema)]
#[schemars(rename = "distancePair", deny_unknown_fields)]
pub struct DistancePair {
    pub from: u64,
    pub to: u64,
    pub elapsed: u64,
    pub stopped_cycles: u64,
}

#[derive(Serialize, JsonSchema)]
#[schemars(rename = "distanceData", deny_unknown_fields)]
pub struct DistanceData {
    #[schemars(extend("enum" = ["ticks", "cycles"]), transform = without_type)]
    pub unit: String,
    pub count: u64,
    pub pairs: Vec<DistancePair>,
    pub summary: Option<String>,
    pub stopped_cycles: u128,
    pub truncated: bool,
}
