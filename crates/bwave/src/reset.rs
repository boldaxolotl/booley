//! The one reset rule: which signal is the reset, its polarity, when it
//! deasserts, and the report every sync query and `overview` prints about it.

use serde::Serialize;

use crate::cache::CachedSignal;
use crate::signal::{match_signal, SignalPattern};

/// The reset a store resolved to, for loading and for queries.
#[derive(Debug, Clone)]
pub struct Reset {
    pub name: String,
    pub active_low: bool,
    pub chosen_by_option: bool,
    pub deassert_tick: Option<u64>,
}

/// Name of a signal without its bit-select, as the lowercased last scope part.
fn leaf(name: &str) -> String {
    let base = name.split('[').next().unwrap_or(name);
    base.rsplit('.').next().unwrap_or(base).to_lowercase()
}

/// Pick the reset signal: the shallowest, then alphabetically first, 1-bit
/// signal that matches `matchers`, or without them whose leaf name contains
/// `rst` or `reset`.
pub fn candidate(signals: &[CachedSignal], matchers: Option<&[SignalPattern]>) -> Option<usize> {
    let depth = |s: &CachedSignal| s.name.matches('.').count();
    signals
        .iter()
        .enumerate()
        .filter(|(_, s)| s.width == 1 && s.var_type != "real" && s.var_type != "realtime")
        .filter(|(_, s)| match matchers {
            Some(m) => match_signal(&s.name, m),
            None => {
                let leaf = leaf(&s.name);
                leaf.contains("rst") || leaf.contains("reset")
            }
        })
        .min_by(|(_, a), (_, b)| depth(a).cmp(&depth(b)).then(a.name.cmp(&b.name)))
        .map(|(i, _)| i)
}

/// A leaf is active-low when `rst` or `reset` is followed by exactly one low
/// marker (`n`, `_n`, `_b`, `_l`), optionally `i` or `_i`, then its end.
pub fn active_low(name: &str) -> bool {
    let leaf = leaf(name);
    ["rst", "reset"].iter().any(|word| {
        leaf.match_indices(word).any(|(at, _)| {
            let rest = &leaf[at + word.len()..];
            ["n", "_n", "_b", "_l"].iter().any(|marker| {
                rest.strip_prefix(marker)
                    .is_some_and(|tail| matches!(tail, "" | "i" | "_i"))
            })
        })
    })
}

/// Tick of the first transition to the inactive value. X, Z and the asserted
/// value never deassert.
pub fn deassert_tick(transitions: &[(u64, String)], active_low: bool) -> Option<u64> {
    let inactive = if active_low { "1" } else { "0" };
    transitions
        .iter()
        .find(|(_, v)| v == inactive)
        .map(|(t, _)| *t)
}

/// What a sync query or `overview` says about the reset it used.
#[derive(Debug, Clone, Serialize)]
pub struct ResetReport {
    pub signal: Option<String>,
    pub polarity: Option<&'static str>,
    pub chosen_by: &'static str,
    pub deassert_tick: Option<u64>,
    pub deassert_time: Option<String>,
    pub skipped_cycles: u64,
    pub with_reset: bool,
}

impl ResetReport {
    /// The `# reset:` header line.
    pub fn text(&self) -> String {
        let Some(signal) = &self.signal else {
            return "# reset: none detected; no cycles skipped".to_string();
        };
        let polarity = match self.polarity {
            Some("active_low") => "active-low",
            _ => "active-high",
        };
        let head = format!("# reset: {signal} ({polarity} by name, {})", self.chosen_by);
        let Some(time) = &self.deassert_time else {
            return format!("{head} never deasserts; no cycles skipped");
        };
        let tail = if self.with_reset {
            "reset phase included (--with-reset)".to_string()
        } else if self.skipped_cycles == 0 {
            "no cycles skipped".to_string()
        } else {
            format!(
                "{} {} skipped (--with-reset to include)",
                self.skipped_cycles,
                if self.skipped_cycles == 1 {
                    "cycle"
                } else {
                    "cycles"
                }
            )
        };
        format!("{head} deasserts @ {time}; {tail}")
    }
}
