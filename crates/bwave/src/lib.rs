pub mod cache;
pub mod docs;
pub mod extract;
pub mod format;
pub mod fst;
pub mod index;
pub mod output;
pub mod parser;
mod profile;
pub mod signal;
pub mod vcd_chunk;
pub mod virtual_signal;

use format::{Radix, TimeToken};

pub struct ExtractConfig {
    pub patterns: Vec<String>,
    pub async_mode: bool,
    pub clock_pattern: Option<String>,
    pub reset_pattern: Option<String>,
    pub with_reset: bool,
    pub at_time: Option<i64>,
    pub at_time_is_cycle: bool,
    pub find_pattern: Option<String>,
    pub find_value: Option<String>,
    pub first_match: bool,
    pub last_match: bool,
    pub stats_mode: bool,
    pub sample_at_pattern: Option<String>,
    pub sample_at_value: Option<String>,
    pub count_only: bool,
    pub find_stuck: Option<String>,
    pub time_min: i64,
    pub time_max: Option<i64>,
    pub max_lines: usize,
    pub wave_mode: bool,
    /// Run-length-encode wave output: contiguous columns with identical
    /// values across all signals collapse to `<val>×N`. Off by default.
    pub wave_rle: bool,
    pub diff_points: Option<(i64, i64)>,
    pub distance_a: Option<(String, String)>,
    pub distance_b: Option<(String, String)>,
    /// Per-signal radix overrides: (pattern, radix), parallel to patterns
    pub signal_radixes: Vec<(String, Radix)>,
    /// Named markers for --wave display, resolved to cycles (sync) or ticks (async).
    pub markers: Vec<(String, i64)>,
    /// Marker time tokens awaiting resolution against the store header.
    pub marker_tokens: Vec<(String, TimeToken)>,
    /// Virtual signal definitions: "name = expr"
    pub virtual_defs: Vec<String>,
    /// Output format: true = JSON, false = text
    pub json_format: bool,
    // -- v0.2 Phase 4: raw time-token strings (resolved once cache is loaded)
    /// Raw `-t` argument; e.g. `"100c:200c"` or `"5us:"`. Parsed against the
    /// cache's timescale in `resolve_time_tokens()` to populate `time_min` /
    /// `time_max`.
    pub time_str: Option<String>,
    /// Raw `--at` argument; e.g. `"100"` (sync), `"500ns"` (async). Parsed in
    /// `resolve_time_tokens()` to populate `at_time`.
    pub at_str: Option<String>,
    /// Raw `diff T1 T2`; e.g. `("100", "200")` or `("5us", "10us")`. Parsed
    /// in `resolve_time_tokens()` to populate `diff_points`.
    pub diff_strs: Option<(String, String)>,
}

impl Default for ExtractConfig {
    fn default() -> Self {
        Self {
            patterns: vec!["*".to_string()],
            async_mode: false,
            clock_pattern: None,
            reset_pattern: None,
            with_reset: false,
            at_time: None,
            at_time_is_cycle: false,
            find_pattern: None,
            find_value: None,
            first_match: false,
            last_match: false,
            stats_mode: false,
            sample_at_pattern: None,
            sample_at_value: None,
            count_only: false,
            find_stuck: None,
            time_min: 0,
            time_max: None,
            max_lines: 2000,
            wave_mode: false,
            wave_rle: false,
            diff_points: None,
            distance_a: None,
            distance_b: None,
            signal_radixes: Vec::new(),
            markers: Vec::new(),
            marker_tokens: Vec::new(),
            virtual_defs: Vec::new(),
            json_format: false,
            time_str: None,
            at_str: None,
            diff_strs: None,
        }
    }
}

impl ExtractConfig {
    /// Parse the raw time-token strings (`time_str`, `at_str`, `diff_strs`)
    /// against `ticks_to_ns` and `clock_period_ticks` from the cache header.
    /// Populates `time_min`, `time_max`, `at_time`, and `diff_points` in
    /// place. Returns a human-readable error on bad syntax or
    /// timescale-missing conditions. No-op when the relevant string field
    /// is `None`.
    ///
    /// Sync vs async: when `self.async_mode` is true, time bounds resolve to
    /// raw simulation ticks; in sync mode they resolve to cycle numbers.
    pub fn resolve_time_tokens(
        &mut self,
        ticks_to_ns: f64,
        clock_period_ticks: u64,
    ) -> Result<(), String> {
        use format::{parse_time_range, TimeToken};

        let resolve = |tok: &TimeToken| -> Result<i64, String> {
            if self.async_mode {
                tok.resolve_to_tick(ticks_to_ns, clock_period_ticks)
            } else {
                tok.resolve_to_cycle(ticks_to_ns, clock_period_ticks)
            }
        };

        let mut markers = Vec::with_capacity(self.marker_tokens.len());
        for (name, token) in &self.marker_tokens {
            let position = resolve(token)
                .map_err(|error| format!("--marker {name}: cannot resolve marker time: {error}"))?;
            // Repeating a name updates its position. Retaining insertion order
            // keeps distinct marker labels deterministic for rendering.
            markers.retain(|(existing, _)| existing != name);
            markers.push((name.clone(), position));
        }
        self.markers = markers;

        if let Some(s) = self.time_str.clone() {
            let (lo, hi) = parse_time_range(&s, self.async_mode)?;
            if let Some(t) = lo {
                self.time_min = resolve(&t)?;
            }
            self.time_max = match hi {
                Some(t) => Some(resolve(&t)?),
                None => None,
            };
            // Reject reversed ranges (`-t 1000:500`) — silent empty output is
            // worse than an upfront error.
            if let Some(hi) = self.time_max {
                if self.time_min > hi {
                    return Err(format!(
                        "-t '{}': start ({}) is after end ({}) — did you mean `{}:{}`?",
                        s, self.time_min, hi, hi, self.time_min,
                    ));
                }
            }
        }

        if let Some(s) = self.at_str.clone() {
            let tok = TimeToken::parse(&s, self.async_mode)?;
            // Resolve once to a cycle in sync mode or a raw tick in async;
            // snapshot_from_cache must not apply the timescale a second time.
            let v = resolve(&tok)?;
            self.at_time = Some(v);
        }

        if let Some((a, b)) = self.diff_strs.clone() {
            let ta = TimeToken::parse(&a, self.async_mode)?;
            let tb = TimeToken::parse(&b, self.async_mode)?;
            let (ra, rb) = (resolve(&ta)?, resolve(&tb)?);
            // Normalize ordering so `diff bp 10 1` == `diff bp 1 10`.
            self.diff_points = Some(if ra <= rb { (ra, rb) } else { (rb, ra) });
        }

        Ok(())
    }
}
