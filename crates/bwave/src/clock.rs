//! Advisory analysis of a selected clock against its uniform sampling grid.

use serde::Serialize;

#[derive(Clone, Debug, Serialize)]
pub struct ClockFinding {
    pub reason: String,
    pub tick: Option<u64>,
    pub detail: String,
}

#[derive(Clone, Debug, Serialize)]
pub struct ClockVerdict {
    pub status: String,
    pub reason: String,
    pub clock: String,
    pub first_off_grid_tick: Option<u64>,
    pub first_off_grid_cycle: Option<u64>,
    pub findings: Vec<ClockFinding>,
    pub stopped: StopSummary,
}

#[derive(Clone, Debug, Default, Serialize)]
pub struct StopSummary {
    pub stretches: usize,
    pub total_cycles: u64,
    pub longest: Option<StoppedRange>,
}

#[derive(Clone, Copy, Debug, Serialize)]
pub struct StoppedRange {
    pub from_cycle: u64,
    pub to_cycle: u64,
}

#[derive(Clone, Debug)]
pub struct ClockAnalysis {
    pub verdict: ClockVerdict,
    /// Inclusive missing grid ticks, only in the proven regular prefix.
    pub stopped_ticks: Vec<(u64, u64)>,
}

pub fn rising_ticks(transitions: &[(u64, String)]) -> Vec<u64> {
    let mut previous = "x";
    let mut rises = Vec::new();
    for (tick, value) in transitions {
        if previous == "0" && value == "1" {
            rises.push(*tick);
        }
        previous = value;
    }
    rises
}

/// Infer quantized fractional cadence; this is evidence, not simulator intent.
fn fractional_cadence(rises: &[u64]) -> bool {
    if rises.len() < 7 {
        return false;
    }
    let gaps: Vec<_> = rises.windows(2).map(|w| w[1] - w[0]).collect();
    let lo = *gaps.iter().min().unwrap();
    let hi = *gaps.iter().max().unwrap();
    if lo == 0
        || hi != lo + 1
        || gaps.iter().filter(|&&g| g == lo).count() < 2
        || gaps.iter().filter(|&&g| g == hi).count() < 2
    {
        return false;
    }
    let n = (rises.len() - 1) as u128;
    let elapsed = u128::from(rises[rises.len() - 1] - rises[0]);
    elapsed % n != 0
        && rises
            .iter()
            .enumerate()
            .all(|(i, &tick)| (u128::from(tick - rises[0]) * n).abs_diff(elapsed * i as u128) < n)
}

pub fn analyze(
    clock: &str,
    period: u64,
    first: u64,
    transitions: &[(u64, String)],
) -> ClockAnalysis {
    let rises = rising_ticks(transitions);
    let off_grid = (period > 0)
        .then(|| {
            rises
                .iter()
                .copied()
                .find(|&tick| tick < first || (tick - first) % period != 0)
        })
        .flatten();
    let unknown = rises.first().and_then(|start| {
        transitions
            .iter()
            .find(|(tick, value)| {
                tick > start && value.chars().any(|c| matches!(c, 'x' | 'X' | 'z' | 'Z'))
            })
            .map(|(tick, _)| *tick)
    });
    let cutoff = off_grid
        .into_iter()
        .chain(unknown)
        .min()
        .unwrap_or(u64::MAX);
    let mut findings = Vec::new();
    if clock.is_empty() || rises.len() < 2 || period == 0 {
        findings.push(ClockFinding {
            reason: "insufficient_edges".into(),
            tick: rises.first().copied(),
            detail: "fewer than two rising edges; no usable clock grid".into(),
        });
    }
    if let Some(tick) = off_grid {
        findings.push(ClockFinding {
            reason: if fractional_cadence(&rises) {
                "non_integer_period"
            } else {
                "off_grid_edge"
            }
            .into(),
            tick: Some(tick),
            detail: if fractional_cadence(&rises) {
                "inferred quantized non-integer period cadence; use --async with tick/time bounds"
            } else {
                "real rising edge is off the uniform grid; use --async with tick/time bounds"
            }
            .into(),
        });
    }
    if let Some(tick) = unknown {
        findings.push(ClockFinding {
            reason: "unknown_clock".into(),
            tick: Some(tick),
            detail: "clock becomes X/Z after its first rising edge; use --async".into(),
        });
    }
    let mut stopped_ticks = Vec::new();
    if period > 0 {
        for pair in rises.windows(2) {
            if pair[1] >= cutoff {
                break;
            }
            if pair[1] - pair[0] > period {
                stopped_ticks.push((pair[0] + period, pair[1] - period));
            }
        }
    }
    let status = if clock.is_empty() || rises.len() < 2 || period == 0 {
        "unavailable"
    } else if off_grid.is_some() || unknown.is_some() {
        "warning"
    } else {
        "pass"
    };
    let reason = findings.first().map_or_else(
        || {
            if stopped_ticks.is_empty() {
                "regular"
            } else {
                "stopped_stretches"
            }
            .into()
        },
        |f| f.reason.clone(),
    );
    ClockAnalysis {
        verdict: ClockVerdict {
            status: status.into(),
            reason,
            clock: clock.into(),
            first_off_grid_tick: off_grid,
            first_off_grid_cycle: off_grid.map(|t| (t - first) / period + 1),
            findings,
            stopped: StopSummary::default(),
        },
        stopped_ticks,
    }
}

impl ClockAnalysis {
    pub fn ranges(&self, base: u64, period: u64) -> Vec<StoppedRange> {
        if period == 0 {
            return Vec::new();
        }
        self.stopped_ticks
            .iter()
            .filter_map(|&(lo, hi)| {
                if hi < base {
                    return None;
                }
                Some(StoppedRange {
                    from_cycle: (lo.max(base) - base) / period + 1,
                    to_cycle: (hi - base) / period + 1,
                })
            })
            .collect()
    }
}

pub fn summary(ranges: &[StoppedRange]) -> StopSummary {
    StopSummary {
        stretches: ranges.len(),
        total_cycles: ranges.iter().map(|r| r.to_cycle - r.from_cycle + 1).sum(),
        longest: ranges
            .iter()
            .max_by_key(|r| r.to_cycle - r.from_cycle)
            .copied(),
    }
}

pub fn stopped_count(ranges: &[StoppedRange], lo: u64, hi: u64) -> u64 {
    if lo > hi {
        return 0;
    }
    ranges
        .iter()
        .map(|r| {
            let start = lo.max(r.from_cycle);
            let end = hi.min(r.to_cycle);
            if start <= end {
                end - start + 1
            } else {
                0
            }
        })
        .sum()
}

/// Interval subtraction with no expansion of stopped or matching cycles.
pub fn exclude_stopped(runs: &[(u64, u64)], stops: &[StoppedRange]) -> Vec<(u64, u64)> {
    let mut result = Vec::new();
    let mut index = 0;
    for &(lo, hi) in runs {
        let mut cursor = lo;
        while index < stops.len() && stops[index].to_cycle < cursor {
            index += 1;
        }
        let mut j = index;
        while j < stops.len() && stops[j].from_cycle <= hi {
            let stop = stops[j];
            if stop.from_cycle > cursor {
                result.push((cursor, stop.from_cycle - 1));
            }
            cursor = cursor.max(stop.to_cycle.saturating_add(1));
            if cursor > hi {
                break;
            }
            j += 1;
        }
        if cursor <= hi {
            result.push((cursor, hi));
        }
    }
    result
}

pub fn verdict_text(verdict: &ClockVerdict) -> String {
    let longest = verdict.stopped.longest.map_or(String::new(), |r| {
        format!(", longest {}:{}", r.from_cycle, r.to_cycle)
    });
    format!(
        "# clock: {}\n# clock regularity: {} ({})\n# clock stopped: {} stretches, {} cycles{}{}",
        if verdict.clock.is_empty() {
            "<not detected>"
        } else {
            &verdict.clock
        },
        verdict.status,
        verdict.reason,
        verdict.stopped.stretches,
        verdict.stopped.total_cycles,
        longest,
        verdict
            .findings
            .iter()
            .map(|f| format!(
                "\n# {}: {}{}",
                f.reason,
                f.detail,
                f.tick.map_or(String::new(), |t| format!(" (tick {t})"))
            ))
            .collect::<String>()
    )
}
