//! The one reset rule: which signal is the reset, its polarity, when it
//! deasserts, and the report every sync query and `overview` prints about it.

use schemars::JsonSchema;
use serde::Serialize;

use crate::cache::CachedSignal;
use crate::signal::SignalPattern;

/// The reset a store resolved to, for loading and for queries.
#[derive(Debug, Clone)]
pub struct Reset {
    pub name: String,
    pub active_low: bool,
    pub chosen_by_option: bool,
    pub deassertion: Deassertion,
}

/// Name of a signal without its bit-select, as the lowercased last scope part.
fn leaf(name: &str) -> String {
    let last = name.rsplit('.').next().unwrap_or(name);
    last.split('[').next().unwrap_or(last).to_lowercase()
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
            Some(m) => s.matches(m),
            None => {
                let leaf = leaf(&s.name);
                leaf.contains("rst") || leaf.contains("reset")
            }
        })
        .min_by(|(_, a), (_, b)| depth(a).cmp(&depth(b)).then(a.name.cmp(&b.name)))
        .map(|(i, _)| i)
}

/// A leaf is active-low when `rst` or `reset` is followed by one low marker
/// (`n`, `_n`, `_b`, `_l`), optionally `i`, then its end or a `_` suffix such
/// as `_sync`.
pub fn active_low(name: &str) -> bool {
    let leaf = leaf(name);
    ["rst", "reset"].iter().any(|word| {
        leaf.match_indices(word).any(|(at, _)| {
            let rest = &leaf[at + word.len()..];
            ["n", "_n", "_b", "_l"].iter().any(|marker| {
                rest.strip_prefix(marker).is_some_and(|tail| {
                    let tail = tail.strip_prefix('i').unwrap_or(tail);
                    tail.is_empty() || tail.starts_with('_')
                })
            })
        })
    })
}

/// Where a reset's first assertion ends.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Deassertion {
    /// The tick of the first inactive value after the first assertion.
    At(u64),
    /// Every value is inactive.
    NeverAsserts,
    /// It asserts and never returns to the inactive value.
    NeverDeasserts,
}

/// The reset's deassertion: the first inactive value after its first
/// assertion. A reset that starts X or Z counts as asserted from the start, so
/// it deasserts at its first inactive value; one that starts inactive asserts
/// at its first asserted value, X or Z in between do not count, and later
/// pulses do not move the deassertion.
pub fn deassertion(transitions: &[(u64, String)], active_low: bool) -> Deassertion {
    let (inactive, asserted) = if active_low { ("1", "0") } else { ("0", "1") };
    let starts_inactive = transitions.first().is_some_and(|(_, v)| v == inactive);
    let start = if starts_inactive {
        transitions.iter().position(|(_, v)| v == asserted)
    } else {
        (!transitions.is_empty()).then_some(0)
    };
    let Some(start) = start else {
        return Deassertion::NeverAsserts;
    };
    transitions[start..]
        .iter()
        .find(|(_, v)| v == inactive)
        .map_or(Deassertion::NeverDeasserts, |(t, _)| Deassertion::At(*t))
}

// What a sync query or `overview` says about the reset it used.
#[derive(Debug, Clone, Serialize, JsonSchema)]
#[schemars(rename = "resetReport", deny_unknown_fields)]
pub struct ResetReport {
    /// Full name of the reset signal; null when the store has none.
    pub signal: Option<String>,
    /// Polarity guessed from the signal's name; null without a reset.
    #[schemars(extend("enum" = ["active_low", "active_high", null]), transform = crate::output::without_type)]
    pub polarity: Option<&'static str>,
    /// Whether detection or `--reset` picked the signal.
    #[schemars(extend("enum" = ["auto", "--reset"]), transform = crate::output::without_type)]
    pub chosen_by: &'static str,
    /// Tick where the reset's first assertion ends; null when it never asserts
    /// or never deasserts.
    pub deassert_tick: Option<u64>,
    /// deassert_tick in the store's timescale, such as `25ns`.
    pub deassert_time: Option<String>,
    /// Clock cycles before the deassert that sync queries skip; 0 with
    /// --with-reset, without a clock or without a deassert.
    pub skipped_cycles: u64,
    /// Whether --with-reset kept the reset phase.
    pub with_reset: bool,
    /// Every value of the reset is inactive; only the header says so.
    #[serde(skip)]
    pub never_asserts: bool,
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
        if self.never_asserts {
            return format!("{head} never asserts; no cycles skipped");
        }
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

#[cfg(test)]
mod tests {
    use super::{active_low, deassertion, leaf, Deassertion};

    fn steps(values: &[(u64, &str)]) -> Vec<(u64, String)> {
        values.iter().map(|(t, v)| (*t, (*v).to_string())).collect()
    }

    #[test]
    fn deassertion_is_where_the_first_assertion_ends() {
        use Deassertion::{At, NeverAsserts, NeverDeasserts};
        let cases = [
            (steps(&[]), NeverAsserts),
            (steps(&[(0, "0"), (9, "0")]), NeverAsserts),
            (steps(&[(0, "1"), (9, "0")]), At(9)),
            (steps(&[(0, "x"), (9, "0")]), At(9)),
            (
                steps(&[(0, "0"), (5, "1"), (9, "0"), (20, "1"), (30, "0")]),
                At(9),
            ),
            (steps(&[(0, "0"), (5, "x"), (9, "0")]), NeverAsserts),
            (steps(&[(0, "0"), (5, "z"), (7, "1"), (9, "0")]), At(9)),
            (steps(&[(0, "0"), (5, "1")]), NeverDeasserts),
            (steps(&[(0, "x"), (5, "1")]), NeverDeasserts),
        ];
        for (transitions, expected) in cases {
            assert_eq!(
                deassertion(&transitions, false),
                expected,
                "{transitions:?}"
            );
        }
        let low = steps(&[(0, "1"), (5, "0"), (9, "1")]);
        assert_eq!(deassertion(&low, true), At(9));
    }

    #[test]
    fn leaf_is_the_last_scope_part_without_its_bit_select() {
        assert_eq!(leaf("tb.reset_ctrl[0].flag"), "flag");
        assert_eq!(leaf("tb.gen[0].RST_N"), "rst_n");
        assert_eq!(leaf("tb.rst[3]"), "rst");
    }

    #[test]
    fn low_marker_may_be_followed_by_a_suffix() {
        for name in [
            "tb.rst_n_sync",
            "tb.rstn_q",
            "tb.rst_ni_meta",
            "tb.reset_b_d",
        ] {
            assert!(active_low(name), "{name}");
        }
        for name in ["tb.rst_gen", "tb.rst_in", "tb.reset_en", "tb.rst_new"] {
            assert!(!active_low(name), "{name}");
        }
    }
}
