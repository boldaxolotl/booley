//! Bit slices of stored vectors: `-s "qa.data[5:4]"` selects bits 5..4.
//!
//! A slice is a derived row appended after the stored signals; it reads its
//! source signal and maps every value through `format::slice_value`.

use crate::cache::CachedSignal;
use crate::signal::SignalPattern;

/// Where a slice row reads from; `msb`/`lsb` are offsets from bit 0 of the
/// stored value.
#[derive(Debug, Clone, Copy)]
pub struct SliceOf {
    pub source: usize,
    pub msb: u32,
    pub lsb: u32,
}

/// The bit numbers a stored vector name carries.
#[derive(Debug, PartialEq, Eq)]
pub enum Bits {
    /// `[H:L]` with H >= L (the stored range, or `width-1:0` without one).
    Descending(u32, u32),
    /// Any other trailing range: selected only by its exact name.
    Ascending,
}

/// The `[A:B]` a name ends with.
fn trailing_range(name: &str) -> Option<(u32, u32)> {
    let inner = name.strip_suffix(']')?;
    let (_, inner) = inner.rsplit_once('[')?;
    let (high, low) = inner.split_once(':')?;
    Some((high.parse().ok()?, low.parse().ok()?))
}

/// The bit numbers of the vector `name` of `width` bits.
pub fn bits_of(name: &str, width: u32) -> Bits {
    match trailing_range(name) {
        Some((high, low)) if high >= low && high - low + 1 == width => Bits::Descending(high, low),
        Some(_) => Bits::Ascending,
        None => Bits::Descending(width.saturating_sub(1), 0),
    }
}

/// The slice rows `pattern` asks for, as `(row name, source)`; a select that
/// covers a whole vector needs none. Caller input that cannot be honored
/// (reversed, outside the vector, ascending, real) is an error.
pub fn plan(
    signals: &[CachedSignal],
    pattern: &SignalPattern,
    text: &str,
) -> Result<Vec<(String, SliceOf)>, String> {
    let Some(select) = pattern.select() else {
        return Ok(Vec::new());
    };
    // A stored element (`dmem_wr[0]`) is what `[N]` names when one exists.
    if select.single && signals.iter().any(|s| pattern.names_exactly(&s.name)) {
        return Ok(Vec::new());
    }
    let mut rows = Vec::new();
    for (source, signal) in signals
        .iter()
        .enumerate()
        .filter(|(_, s)| s.slice.is_none())
    {
        let Some((base, _)) = pattern.bit_select(&signal.name) else {
            continue;
        };
        let (name, width) = (&signal.name, signal.width);
        let (msb, lsb) = (select.msb, select.lsb);
        let Bits::Descending(high, low) = bits_of(name, width) else {
            return Err(format!(
                "'{text}': '{name}' has an ascending range; only its whole range can be selected"
            ));
        };
        let range = format!("{width} bits wide ([{high}:{low}])");
        if msb < lsb {
            return Err(format!(
                "'{text}' has a reversed range [{msb}:{lsb}]; '{base}' is {range}"
            ));
        }
        if lsb < low || msb > high {
            return Err(format!(
                "'{text}' selects bits [{msb}:{lsb}] of '{base}', which is {range}"
            ));
        }
        if signal.var_type == "real" || signal.var_type == "realtime" {
            return Err(format!(
                "'{text}': cannot select bits of real signal '{name}'"
            ));
        }
        if (msb, lsb) != (high, low) {
            let of = SliceOf {
                source,
                msb: msb - low,
                lsb: lsb - low,
            };
            rows.push((format!("{base}{}", select.text()), of));
        }
    }
    Ok(rows)
}

/// Map a source's transitions to a slice's, dropping the ones that leave the
/// slice unchanged. `before` is the slice's value entering the range.
pub fn slice_transitions(
    source: Vec<(u64, String)>,
    width: u32,
    of: &SliceOf,
    mut previous: Option<String>,
) -> Vec<(u64, String)> {
    let mut out = Vec::new();
    for (tick, value) in source {
        let value = crate::format::slice_value(&value, width, of.msb, of.lsb);
        if previous.as_ref() != Some(&value) {
            previous = Some(value.clone());
            out.push((tick, value));
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::signal::compile_patterns;

    fn signal(name: &str, width: u32, var_type: &str) -> CachedSignal {
        let (name, var_type) = (name.to_string(), var_type.to_string());
        CachedSignal {
            name,
            width,
            var_type,
            group_id: 0,
            slice: None,
        }
    }

    fn plan_for(signals: &[CachedSignal], text: &str) -> Result<Vec<String>, String> {
        let pattern = compile_patterns(&[text.to_string()], "").unwrap().remove(0);
        plan(signals, &pattern, text).map(|rows| rows.into_iter().map(|r| r.0).collect())
    }

    #[test]
    fn plan_names_slices_and_refuses_unsliceable_vectors() {
        let signals = [
            signal("tb.addr[31:2]", 30, "wire"),
            signal("tb.r", 8, "real"),
            signal("tb.v[0:3]", 4, "wire"),
            signal("tb.m[0]", 1, "wire"),
        ];
        assert_eq!(plan_for(&signals, "addr[3:2]").unwrap(), ["tb.addr[3:2]"]);
        assert_eq!(
            plan_for(&signals, "addr[31:2]").unwrap(),
            Vec::<String>::new()
        );
        assert!(plan_for(&signals, "r[3]").unwrap_err().contains("real"));
        assert!(plan_for(&signals, "v[1:0]")
            .unwrap_err()
            .contains("ascending"));
        assert!(plan_for(&signals, "addr[1:0]")
            .unwrap_err()
            .contains("30 bits"));
        // A stored element is what `[N]` names; no slice is added.
        assert!(plan_for(&signals, "m[0]").unwrap().is_empty());
    }
}
