//! Value formatting (bin->hex) and signal tree display.

use std::collections::BTreeMap;
use std::io::{self, Write};

// -- Typed time tokens (v0.2 Phase 4) -----------------------------------------

/// A time argument with an explicit unit. Parsed from CLI strings like
/// `100c`, `100t`, `100ns`, or the bare `100` (cycles in sync mode only).
///
/// Resolution to a simulation tick or cycle number is deferred until the
/// store header is available, since `ns`/`us`/`ms`/`ps` units must
/// be converted via the VCD timescale and `c` (cycle) units need the clock
/// period.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TimeToken {
    /// Cycle count (`100`, `100c`). Bare form only allowed in sync mode.
    Cycle(i64),
    /// Raw simulation tick (`100t`).
    Tick(i64),
    /// Picoseconds (`100ps`).
    Pico(i64),
    /// Nanoseconds (`100ns`).
    Nano(i64),
    /// Microseconds (`100us`).
    Micro(i64),
    /// Milliseconds (`100ms`).
    Milli(i64),
}

impl TimeToken {
    /// Parse a time token from a CLI string.
    ///
    /// Grammar: `integer suffix?` where `suffix` ∈ `{c, t, ns, us, ms, ps}`.
    /// In **sync** mode a bare integer (no suffix) means cycles. In **async**
    /// mode bare integers are rejected — callers must spell `100c` / `100t` /
    /// `100ns` explicitly. This catches the historical footgun where a value
    /// from a sync-mode invocation was reused under `--async` and silently
    /// changed meaning from "cycle" to "tick".
    pub fn parse(s: &str, async_mode: bool) -> Result<TimeToken, String> {
        let s = s.trim();
        if s.is_empty() {
            return Err("empty time token".to_string());
        }
        let (num_str, suffix) = split_time_suffix(s);
        if num_str.is_empty() {
            return Err(format!("invalid time token: '{}'", s));
        }
        let n: i64 = num_str
            .parse()
            .map_err(|_| format!("invalid integer in time token: '{}'", s))?;
        match suffix {
            "" => {
                if async_mode {
                    Err(format!(
                        "bare integer '{}' is ambiguous in async mode — use a unit \
                         suffix ('{n}c', '{n}t', '{n}ns'); see `bwave docs show reference/time-tokens`",
                        s, n = n
                    ))
                } else {
                    Ok(TimeToken::Cycle(n))
                }
            }
            "c" => Ok(TimeToken::Cycle(n)),
            "t" => Ok(TimeToken::Tick(n)),
            "ps" => Ok(TimeToken::Pico(n)),
            "ns" => Ok(TimeToken::Nano(n)),
            "us" => Ok(TimeToken::Micro(n)),
            "ms" => Ok(TimeToken::Milli(n)),
            other => Err(format!(
                "unknown time-unit suffix '{}' in '{}' (expected c / t / ps / ns / us / ms)",
                other, s
            )),
        }
    }

    /// Resolve this token to a **cycle** number, using `ticks_to_ns` and
    /// `clock_period_ticks` from the cache header.
    ///
    /// Async-mode callers should use [`Self::resolve_to_tick`] instead. This
    /// method is the right call for sync-mode time bounds and `--at` snapshots.
    pub fn resolve_to_cycle(
        &self,
        ticks_to_ns: f64,
        clock_period_ticks: u64,
    ) -> Result<i64, String> {
        match *self {
            TimeToken::Cycle(n) => Ok(n),
            TimeToken::Tick(n) => {
                if clock_period_ticks == 0 {
                    return Err("cannot resolve tick-token to cycle: no clock detected".to_string());
                }
                Ok(n / clock_period_ticks as i64)
            }
            TimeToken::Pico(n) | TimeToken::Nano(n) | TimeToken::Micro(n) | TimeToken::Milli(n) => {
                let ns = self.to_ns().unwrap();
                let _ = n; // silence unused warning when match cases collapse
                if ticks_to_ns <= 0.0 {
                    return Err("no VCD timescale available".to_string());
                }
                if clock_period_ticks == 0 {
                    return Err(
                        "cannot convert physical-time token to cycle: no clock detected"
                            .to_string(),
                    );
                }
                let ticks = (ns / ticks_to_ns) as i64;
                Ok(ticks / clock_period_ticks as i64)
            }
        }
    }

    /// Resolve this token to a raw **simulation tick**, for async-mode use.
    pub fn resolve_to_tick(
        &self,
        ticks_to_ns: f64,
        clock_period_ticks: u64,
    ) -> Result<i64, String> {
        match *self {
            TimeToken::Tick(n) => Ok(n),
            TimeToken::Cycle(n) => {
                if clock_period_ticks == 0 {
                    return Err("cannot resolve cycle-token to tick: no clock detected".to_string());
                }
                Ok(n * clock_period_ticks as i64)
            }
            _ => {
                let ns = self.to_ns().unwrap();
                if ticks_to_ns <= 0.0 {
                    return Err("no VCD timescale available".to_string());
                }
                Ok((ns / ticks_to_ns) as i64)
            }
        }
    }

    /// Helper: convert physical-time tokens to nanoseconds. Returns None for
    /// non-physical tokens (Cycle/Tick).
    fn to_ns(&self) -> Option<f64> {
        match *self {
            TimeToken::Pico(n) => Some(n as f64 / 1_000.0),
            TimeToken::Nano(n) => Some(n as f64),
            TimeToken::Micro(n) => Some(n as f64 * 1_000.0),
            TimeToken::Milli(n) => Some(n as f64 * 1_000_000.0),
            _ => None,
        }
    }
}

/// Split a time-token string into (numeric_prefix, unit_suffix).
/// `"100ns"` → `("100", "ns")`, `"100"` → `("100", "")`, `"-5c"` → `("-5", "c")`.
fn split_time_suffix(s: &str) -> (&str, &str) {
    // Walk from the end: the suffix is the longest run of ASCII letters.
    let mut split_at = s.len();
    for (i, ch) in s.char_indices().rev() {
        if ch.is_ascii_alphabetic() {
            split_at = i;
        } else {
            break;
        }
    }
    (&s[..split_at], &s[split_at..])
}

/// Parse a time-range string `START:END` into a pair of tokens. Either side
/// may be empty (open-ended), and the colon itself may be omitted for a
/// single-point form (then `START == END`).
pub fn parse_time_range(
    s: &str,
    async_mode: bool,
) -> Result<(Option<TimeToken>, Option<TimeToken>), String> {
    let parts: Vec<&str> = s.splitn(2, ':').collect();
    if parts.len() == 1 {
        let t = TimeToken::parse(parts[0], async_mode)?;
        return Ok((Some(t), Some(t)));
    }
    let start = if parts[0].is_empty() {
        None
    } else {
        Some(TimeToken::parse(parts[0], async_mode)?)
    };
    let end = if parts[1].is_empty() {
        None
    } else {
        Some(TimeToken::parse(parts[1], async_mode)?)
    };
    Ok((start, end))
}

// -- Radix display control ----------------------------------------------------

/// Display radix for signal values.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Radix {
    Hex,
    Dec,
    Bin,
}

/// Split a radix suffix from a signal pattern: "coeff%d" → ("coeff", Dec).
/// No suffix or `%h` → Hex.
///
/// An unknown suffix (`coeff%u`) is an error, not a fallback: `%` cannot
/// appear in a Verilog identifier, so the pattern would keep the suffix,
/// match nothing, and be dropped from the result set without a word — a
/// `wave` with nine `-s` filters silently rendered two rows because seven
/// carried `%u`. Fail loudly instead.
pub fn parse_radix_suffix(s: &str) -> Result<(String, Radix), String> {
    if let Some(base) = s.strip_suffix("%d") {
        Ok((base.to_string(), Radix::Dec))
    } else if let Some(base) = s.strip_suffix("%b") {
        Ok((base.to_string(), Radix::Bin))
    } else if let Some(base) = s.strip_suffix("%h") {
        Ok((base.to_string(), Radix::Hex))
    } else if let Some(pct) = s.rfind('%') {
        Err(format!(
            "unknown radix suffix '{}' in pattern '{}' — valid radixes are \
             %h (hex, default), %d (decimal), %b (binary)",
            &s[pct..],
            s
        ))
    } else {
        Ok((s.to_string(), Radix::Hex))
    }
}

/// Parse a Verilog-style literal: "'d255" → "FF", "'b11111111" → "FF", "'hFF" → "FF".
/// Optional width prefix: "8'd255" also accepted.
/// Also accepts a bare decimal integer: "0", "1", "255" → "0", "1", "FF".
/// Bare hex-looking strings (e.g. "FF", "C1C") are rejected with a hint.
/// Returns normalized uppercase hex.
pub fn parse_verilog_literal(s: &str) -> Result<String, String> {
    let tick_pos = match s.find('\'') {
        Some(p) => p,
        None => {
            // No tick: accept bare decimal integer (digits only).
            if !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit()) {
                return Ok(decimal_to_hex(s));
            }
            return Err(format!(
                "bare value '{}' is decimal-only; use 'hXX for hex, 'bNN for binary, or 'dNNN for decimal",
                s
            ));
        }
    };

    let width_prefix = &s[..tick_pos];
    if !width_prefix.is_empty() && !width_prefix.bytes().all(|b| b.is_ascii_digit()) {
        return Err(format!("invalid width prefix in literal: {}", s));
    }

    if tick_pos + 1 >= s.len() {
        return Err(format!("missing radix character after ': {}", s));
    }

    let radix = s.as_bytes()[tick_pos + 1];
    let digits = &s[tick_pos + 2..];

    if digits.is_empty() {
        return Err(format!("missing value digits in literal: {}", s));
    }

    match radix {
        b'd' | b'D' => Ok(decimal_to_hex(digits)),
        b'h' | b'H' => {
            if digits.eq_ignore_ascii_case("x") {
                return Ok("X".into());
            }
            if digits.eq_ignore_ascii_case("z") {
                return Ok("Z".into());
            }
            Ok(digits.to_uppercase())
        }
        b'b' | b'B' => {
            if digits.eq_ignore_ascii_case("x") {
                return Ok("X".into());
            }
            if digits.eq_ignore_ascii_case("z") {
                return Ok("Z".into());
            }
            bin_to_hex(digits).ok_or_else(|| format!("invalid binary value: {}", digits))
        }
        _ => Err(format!(
            "invalid radix '{}' in literal: must be 'd, 'h, or 'b",
            radix as char
        )),
    }
}

/// Format a hex value in the given radix for display.
/// `hex_val` is the internal uppercase-hex representation (may contain x/z).
pub fn format_value_with_radix(hex_val: &str, _width: u32, radix: Radix) -> String {
    match radix {
        Radix::Hex => hex_val.to_string(),
        Radix::Dec => {
            if hex_val
                .bytes()
                .any(|b| b == b'x' || b == b'X' || b == b'z' || b == b'Z')
            {
                return hex_val.to_string();
            }
            hex_to_decimal(hex_val)
        }
        Radix::Bin => {
            if hex_val
                .bytes()
                .any(|b| b == b'x' || b == b'X' || b == b'z' || b == b'Z')
            {
                return hex_val.to_string();
            }
            hex_to_binary(hex_val)
        }
    }
}

/// Format an internal hex value as a Verilog literal with radix prefix.
/// E.g. hex "FF" → "'hFF", or with Dec radix → "'d255".
pub fn format_as_verilog_literal(hex_val: &str, _width: u32, radix: Radix) -> String {
    if hex_val
        .bytes()
        .any(|b| b == b'x' || b == b'X' || b == b'z' || b == b'Z')
    {
        return format!("'h{}", hex_val);
    }
    match radix {
        Radix::Hex => format!("'h{}", hex_val),
        Radix::Dec => format!("'d{}", hex_to_decimal(hex_val)),
        Radix::Bin => format!("'b{}", hex_to_binary(hex_val)),
    }
}

/// Convert uppercase hex string to decimal string (big-integer, no crate needed).
fn hex_to_decimal(hex: &str) -> String {
    let hex = hex.trim_start_matches('0');
    if hex.is_empty() {
        return "0".to_string();
    }
    // Build a big-integer as a Vec<u8> of decimal digits (LSB first).
    let mut digits: Vec<u8> = vec![0];
    for &b in hex.as_bytes() {
        let nibble = HEX_DECODE_NIBBLE[b as usize];
        if nibble > 0x0F {
            return hex.to_string(); // not valid hex, return as-is
        }
        // digits = digits * 16 + nibble
        let mut carry = nibble as u16;
        for d in digits.iter_mut() {
            let v = (*d as u16) * 16 + carry;
            *d = (v % 10) as u8;
            carry = v / 10;
        }
        while carry > 0 {
            digits.push((carry % 10) as u8);
            carry /= 10;
        }
    }
    digits.iter().rev().map(|d| (b'0' + d) as char).collect()
}

/// Convert a decimal string to uppercase hex (big-integer).
fn decimal_to_hex(dec: &str) -> String {
    let dec = dec.trim_start_matches('0');
    if dec.is_empty() {
        return "0".to_string();
    }
    if !dec.bytes().all(|b| b.is_ascii_digit()) {
        return dec.to_string(); // not valid decimal
    }
    // Build big-integer as Vec<u8> of hex nibbles (LSB first).
    let mut nibbles: Vec<u8> = vec![0];
    for &b in dec.as_bytes() {
        let digit = b - b'0';
        // nibbles = nibbles * 10 + digit
        let mut carry = digit as u16;
        for n in nibbles.iter_mut() {
            let v = (*n as u16) * 10 + carry;
            *n = (v % 16) as u8;
            carry = v / 16;
        }
        while carry > 0 {
            nibbles.push((carry % 16) as u8);
            carry /= 16;
        }
    }
    nibbles
        .iter()
        .rev()
        .map(|n| HEX_NIBBLE[*n as usize] as char)
        .collect()
}

/// Convert hex string to binary string (no leading zeros except for "0").
fn hex_to_binary(hex: &str) -> String {
    let hex = hex.trim_start_matches('0');
    if hex.is_empty() {
        return "0".to_string();
    }
    let mut result = String::with_capacity(hex.len() * 4);
    let mut first = true;
    for &b in hex.as_bytes() {
        let nibble = HEX_DECODE_NIBBLE[b as usize];
        if nibble > 0x0F {
            return hex.to_string();
        }
        if first {
            // Skip leading zeros in the first nibble
            let bits = format!("{:b}", nibble);
            result.push_str(&bits);
            first = false;
        } else {
            let bits = format!("{:04b}", nibble);
            result.push_str(&bits);
        }
    }
    if result.is_empty() {
        "0".to_string()
    } else {
        result
    }
}

/// Format a VCD value as hex (multi-bit) or raw (1-bit / x/z).
///
/// Input values (after stripping VCD prefixes):
///   - single char: '0', '1', 'x', 'z'
///   - raw binary string: "0110..." (no 'b' prefix)
///   - real number string: "1.5" (no 'r' prefix)
pub fn format_value(val: &str) -> String {
    if val.len() <= 1 {
        return val.to_string();
    }

    // Check for x/z in multi-bit -- show as-is (binary with unknowns)
    if val.contains('x') || val.contains('z') || val.contains('X') || val.contains('Z') {
        return val.to_string();
    }

    // Try binary-to-hex conversion using nibble-based approach
    bin_to_hex(val).unwrap_or_else(|| val.to_string())
}

const HEX_NIBBLE: [u8; 16] = *b"0123456789ABCDEF";

/// Fast nibble-based binary-to-uppercase-hex conversion.
/// Returns None if input is not a valid binary string.
fn bin_to_hex(bin: &str) -> Option<String> {
    if bin.is_empty() {
        return Some("0".to_string());
    }

    // Validate all chars are 0 or 1
    if !bin.bytes().all(|b| b == b'0' || b == b'1') {
        return None;
    }

    // Pad to multiple of 4 by processing leading partial nibble
    let mut hex = String::with_capacity((bin.len() + 3) / 4);
    let remainder = bin.len() % 4;
    let start = if remainder > 0 {
        let nibble = u8_from_bin(&bin[..remainder]);
        if nibble > 0 || bin.len() <= 4 {
            hex.push(nibble_to_hex(nibble));
        }
        remainder
    } else {
        0
    };

    // Process full nibbles
    let mut i = start;
    let skip_leading = hex.is_empty();
    while i + 4 <= bin.len() {
        let nibble = u8_from_bin(&bin[i..i + 4]);
        if !skip_leading || nibble > 0 || !hex.is_empty() || i + 4 >= bin.len() {
            hex.push(nibble_to_hex(nibble));
        }
        i += 4;
    }

    if hex.is_empty() {
        Some("0".to_string())
    } else {
        Some(hex)
    }
}

fn u8_from_bin(s: &str) -> u8 {
    let mut val = 0u8;
    for b in s.bytes() {
        val = (val << 1) | (b - b'0');
    }
    val
}

fn nibble_to_hex(n: u8) -> char {
    match n {
        0..=9 => (b'0' + n) as char,
        10..=15 => (b'A' + n - 10) as char,
        _ => unreachable!(),
    }
}

// -- Binary packing for .bwave v4 ----------------------------------------

/// Hex ASCII → nibble value. Values > 0x0F indicate invalid (non-hex) input.
const HEX_DECODE_NIBBLE: [u8; 256] = {
    let mut t = [0xFFu8; 256];
    t[b'0' as usize] = 0;
    t[b'1' as usize] = 1;
    t[b'2' as usize] = 2;
    t[b'3' as usize] = 3;
    t[b'4' as usize] = 4;
    t[b'5' as usize] = 5;
    t[b'6' as usize] = 6;
    t[b'7' as usize] = 7;
    t[b'8' as usize] = 8;
    t[b'9' as usize] = 9;
    t[b'A' as usize] = 10;
    t[b'B' as usize] = 11;
    t[b'C' as usize] = 12;
    t[b'D' as usize] = 13;
    t[b'E' as usize] = 14;
    t[b'F' as usize] = 15;
    t[b'a' as usize] = 10;
    t[b'b' as usize] = 11;
    t[b'c' as usize] = 12;
    t[b'd' as usize] = 13;
    t[b'e' as usize] = 14;
    t[b'f' as usize] = 15;
    t
};

/// Convert hex string to u128. Returns None if value contains x/z or exceeds 128 bits.
pub fn hex_to_u128(hex: &str) -> Option<u128> {
    let hex = hex.trim_start_matches('0');
    if hex.is_empty() {
        return Some(0);
    }
    if hex.len() > 32 {
        return None;
    }
    if hex
        .bytes()
        .any(|b| b == b'x' || b == b'X' || b == b'z' || b == b'Z')
    {
        return None;
    }
    u128::from_str_radix(hex, 16).ok()
}

/// Extract bits [msb:lsb] from a u128 value (Verilog convention).
pub fn slice_bits(val: u128, msb: u32, lsb: u32) -> u128 {
    let width = msb - lsb + 1;
    if width >= 128 {
        return val;
    }
    let mask = (1u128 << width) - 1;
    (val >> lsb) & mask
}

/// Slice bits [msb:lsb] of a stored signal value, preserving X/Z bits that fall
/// inside the requested range. The input may be:
///   - pure hex (uppercase or lowercase) — each char = 4 bits.
///   - VCD-style binary with x/z (each char = 1 bit) — when X/Z is present and
///     the string is of length ≤ sig_width, this form is auto-detected.
///   - hex with nibble-level X/Z (e.g. "XX0C1C") — accepted as fallback.
///
/// Output is hex packed to ceil(slice_width/4) nibbles; a nibble that contains
/// any X bit becomes 'X', any Z bit becomes 'Z'. The output flows through
/// `values_match` for equality comparison, which is tolerant of leading zeros
/// and case.
pub fn hex_slice(value: &str, sig_width: u32, msb: u32, lsb: u32) -> String {
    if msb < lsb || msb >= sig_width {
        return "0".to_string();
    }
    let bits = expand_to_bits(value, sig_width);
    if bits.is_empty() {
        return value.to_string();
    }
    let high = (sig_width - 1 - msb) as usize;
    let low = (sig_width - 1 - lsb) as usize;
    pack_bits_to_hex(&bits[high..=low])
}

fn expand_to_bits(value: &str, sig_width: u32) -> Vec<u8> {
    let width = sig_width as usize;
    if width == 0 {
        return Vec::new();
    }
    let has_xz = value
        .bytes()
        .any(|b| matches!(b, b'x' | b'X' | b'z' | b'Z'));
    // Detect binary form: every char is 0/1/x/z and total length ≤ sig_width.
    // Pure-binary clean values (no x/z) are stored as hex in this codebase, so
    // the binary branch only triggers when x/z forces VCD raw form.
    let is_binary = has_xz
        && value.len() <= width
        && value
            .bytes()
            .all(|b| matches!(b, b'0' | b'1' | b'x' | b'X' | b'z' | b'Z'));

    let mut bits: Vec<u8> = Vec::with_capacity(width);
    if is_binary {
        let pad = width.saturating_sub(value.len());
        for _ in 0..pad {
            bits.push(b'0');
        }
        for b in value.bytes() {
            bits.push(b.to_ascii_lowercase());
        }
    } else {
        // Hex form (potentially with nibble-level X/Z).
        let total_bits = value.len().saturating_mul(4);
        let pad_bits = width.saturating_sub(total_bits);
        for _ in 0..pad_bits {
            bits.push(b'0');
        }
        let skip_bits = total_bits.saturating_sub(width);
        let mut idx = 0usize;
        for b in value.bytes() {
            let nibble: [u8; 4] = match b {
                b'x' | b'X' => [b'x'; 4],
                b'z' | b'Z' => [b'z'; 4],
                _ => {
                    let v = match (b as char).to_digit(16) {
                        Some(n) => n as u8,
                        None => return Vec::new(), // invalid input → caller falls back
                    };
                    [
                        if v & 0x8 != 0 { b'1' } else { b'0' },
                        if v & 0x4 != 0 { b'1' } else { b'0' },
                        if v & 0x2 != 0 { b'1' } else { b'0' },
                        if v & 0x1 != 0 { b'1' } else { b'0' },
                    ]
                }
            };
            for &nb in &nibble {
                if idx >= skip_bits {
                    bits.push(nb);
                }
                idx += 1;
            }
        }
    }
    // Defensive: pad MSB with 0 if input was shorter than declared width.
    while bits.len() < width {
        bits.insert(0, b'0');
    }
    bits.truncate(width);
    bits
}

fn pack_bits_to_hex(bits: &[u8]) -> String {
    if bits.is_empty() {
        return "0".to_string();
    }
    let pad = (4 - bits.len() % 4) % 4;
    let mut padded: Vec<u8> = Vec::with_capacity(pad + bits.len());
    for _ in 0..pad {
        padded.push(b'0');
    }
    padded.extend_from_slice(bits);
    let mut out = String::with_capacity(padded.len() / 4);
    for chunk in padded.chunks(4) {
        let any_x = chunk.iter().any(|&b| b == b'x');
        let any_z = chunk.iter().any(|&b| b == b'z');
        let c = if any_x {
            'X'
        } else if any_z {
            'Z'
        } else {
            let mut v = 0u8;
            for &b in chunk {
                v = (v << 1) | (b - b'0');
            }
            if v < 10 {
                (b'0' + v) as char
            } else {
                (b'A' + v - 10) as char
            }
        };
        out.push(c);
    }
    out
}

/// Compare two hex strings for numeric equality: strip leading zeros, case-fold.
/// Works for arbitrarily wide values (no integer parsing).
fn canonical_hex_eq(a: &str, b: &str) -> bool {
    let a = a.trim_start_matches('0');
    let b = b.trim_start_matches('0');
    a.eq_ignore_ascii_case(b)
}

/// Tree node for hierarchical signal display.
enum TreeNode {
    Module(BTreeMap<String, TreeNode>),
    Signal { width: u32, var_type: String },
}

/// Print signals grouped by module hierarchy with metadata.
/// Matches Python's _print_signal_tree output format.
pub fn print_signal_tree(
    signals: &[(String, u32, String)],
    out: &mut impl Write,
) -> io::Result<()> {
    // Build tree from dotted names
    let mut root: BTreeMap<String, TreeNode> = BTreeMap::new();
    for (name, width, vtype) in signals {
        let parts: Vec<&str> = name.split('.').collect();
        insert_into_tree(&mut root, &parts, *width, vtype.clone());
    }
    print_tree_node(&root, 0, out)
}

/// Print only the scope (module) hierarchy from a list of signals, with the
/// per-module signal count. Leaf signal names are suppressed. Used by --tree.
pub fn print_scope_tree(signals: &[(String, u32, String)], out: &mut impl Write) -> io::Result<()> {
    let mut root: BTreeMap<String, TreeNode> = BTreeMap::new();
    for (name, width, vtype) in signals {
        let parts: Vec<&str> = name.split('.').collect();
        insert_into_tree(&mut root, &parts, *width, vtype.clone());
    }
    print_scope_node(&root, 0, out)
}

fn print_scope_node(
    node: &BTreeMap<String, TreeNode>,
    indent: usize,
    out: &mut impl Write,
) -> io::Result<()> {
    let prefix = "  ".repeat(indent);
    for (key, val) in node {
        if key.starts_with('\x00') {
            continue;
        }
        if let TreeNode::Module(children) = val {
            let sig_count = count_signals(children);
            writeln!(out, "{}{:<30} ({} signals)", prefix, key, sig_count)?;
            print_scope_node(children, indent + 1, out)?;
        }
    }
    Ok(())
}

fn insert_into_tree(
    node: &mut BTreeMap<String, TreeNode>,
    parts: &[&str],
    width: u32,
    var_type: String,
) {
    if parts.len() == 1 {
        // Leaf signal -- use \x00 prefix to sort after modules (matching Python)
        node.insert(
            format!("\x00{}", parts[0]),
            TreeNode::Signal { width, var_type },
        );
    } else {
        let entry = node
            .entry(parts[0].to_string())
            .or_insert_with(|| TreeNode::Module(BTreeMap::new()));
        if let TreeNode::Module(ref mut children) = entry {
            insert_into_tree(children, &parts[1..], width, var_type);
        }
    }
}

fn count_signals(node: &BTreeMap<String, TreeNode>) -> usize {
    let mut count = 0;
    for (key, val) in node {
        match val {
            TreeNode::Signal { .. } if key.starts_with('\x00') => count += 1,
            TreeNode::Module(children) => count += count_signals(children),
            _ => {}
        }
    }
    count
}

fn print_tree_node(
    node: &BTreeMap<String, TreeNode>,
    indent: usize,
    out: &mut impl Write,
) -> io::Result<()> {
    let prefix = "  ".repeat(indent);

    // Modules first (alphabetical -- BTreeMap guarantees this)
    for (key, val) in node {
        if key.starts_with('\x00') {
            continue;
        }
        if let TreeNode::Module(children) = val {
            let sig_count = count_signals(children);
            writeln!(out, "{}{:<30} ({} signals)", prefix, key, sig_count)?;
            print_tree_node(children, indent + 1, out)?;
        }
    }

    // Leaf signals (alphabetical by name without \x00 prefix)
    for (key, val) in node {
        if !key.starts_with('\x00') {
            continue;
        }
        if let TreeNode::Signal { width, var_type } = val {
            let sig_name = &key[1..]; // strip \x00 prefix
            let width_str = format!("{}-bit", width);
            writeln!(
                out,
                "{}{:<30} {:<8} {}",
                prefix, sig_name, var_type, width_str
            )?;
        }
    }
    Ok(())
}

/// Check if a value string is an edge keyword. Returns Some("rising")/Some("falling")/None.
pub fn is_edge_keyword(value: &str) -> Option<&'static str> {
    match value.to_lowercase().as_str() {
        "rising" => Some("rising"),
        "falling" => Some("falling"),
        "change" => Some("change"),
        _ => None,
    }
}

/// Compare two values for equality (safe for x/z).
/// Handles case-insensitive hex, leading zeros, binary targets, x/z containment.
///
/// **Precedence**: when the target is all 0s/1s and >1 char, it is tried as
/// *binary first*, then hex. So `values_match("A", "10")` is true because
/// binary 10 == decimal 2 != hex A... wait, no: A=10, bin "10"=2, so false.
/// But `values_match("2", "10")` is true (binary "10" = 2). If you need hex
/// interpretation, prefix with "0x" or use uppercase letters in the target.
fn contains_xz(s: &str) -> bool {
    s.bytes()
        .any(|b| b == b'x' || b == b'X' || b == b'z' || b == b'Z')
}

pub fn values_match(actual: &str, target: &str) -> bool {
    if actual.eq_ignore_ascii_case(target) {
        return true;
    }

    // x/z containment matching (no allocation)
    if target.eq_ignore_ascii_case("Z") {
        return contains_xz(actual) && actual.bytes().any(|b| b == b'z' || b == b'Z');
    }
    if target.eq_ignore_ascii_case("X") {
        return contains_xz(actual) && actual.bytes().any(|b| b == b'x' || b == b'X');
    }

    if contains_xz(actual) || contains_xz(target) {
        return false;
    }

    // String-based canonical comparison — safe for arbitrarily wide values.
    // Never parse into fixed-width integers (u64 silently truncates >64-bit signals).

    // If target looks like binary (all 0/1, len>1), convert to hex and compare canonically
    if target.len() > 1 && target.bytes().all(|b| b == b'0' || b == b'1') {
        if let Some(target_hex) = bin_to_hex(target) {
            return canonical_hex_eq(actual, &target_hex);
        }
    }

    // Both sides as hex: strip leading zeros, case-fold
    canonical_hex_eq(actual, target)
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;

    // ---- Existing unit tests ----

    #[test]
    fn test_format_value_single_bit() {
        assert_eq!(format_value("0"), "0");
        assert_eq!(format_value("1"), "1");
        assert_eq!(format_value("x"), "x");
        assert_eq!(format_value("z"), "z");
    }

    #[test]
    fn test_format_value_binary_to_hex() {
        assert_eq!(format_value("00001010"), "A");
        assert_eq!(format_value("11111111"), "FF");
        assert_eq!(format_value("00000000"), "0");
        assert_eq!(format_value("0000000000000000"), "0");
    }

    #[test]
    fn test_format_value_xz_preserved() {
        assert_eq!(format_value("0000xxxx"), "0000xxxx");
        assert_eq!(format_value("xxxxxxxx"), "xxxxxxxx");
        assert_eq!(format_value("0000zzzz"), "0000zzzz");
    }

    #[test]
    fn test_format_value_real() {
        assert_eq!(format_value("1.5"), "1.5");
    }

    #[test]
    fn test_values_match() {
        assert!(values_match("FF", "ff"));
        assert!(values_match("0A", "A"));
        assert!(!values_match("xxxx", "A"));
        assert!(values_match("X", "X"));
        assert!(values_match("A", "00001010"));
        assert!(values_match("ff", "FF"));
        assert!(values_match("aB", "ab"));
        assert!(values_match("0a", "0A"));
        assert!(values_match("x", "X"));
        assert!(values_match("0000zzzz", "Z"));
        assert!(!values_match("0", "X"));
        assert!(!values_match("FF", "Z"));
    }

    #[test]
    fn test_bin_to_hex_edge_cases() {
        assert_eq!(bin_to_hex(""), Some("0".to_string()));
        assert_eq!(bin_to_hex("0"), Some("0".to_string()));
        assert_eq!(bin_to_hex("1"), Some("1".to_string()));
        assert_eq!(bin_to_hex("10"), Some("2".to_string()));
        assert_eq!(bin_to_hex("abc"), None);
    }

    // ---- Regression: values_match with 256-bit values (u64 overflow bug) ----

    #[test]
    fn test_values_match_256bit_hex() {
        // 256-bit value: exceeds u64 range. Old code silently truncated.
        let hex256 = "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF";
        let bin256 = "1".repeat(256);
        assert!(
            values_match(hex256, &bin256),
            "256-bit all-ones: hex vs binary should match"
        );
        assert!(values_match(hex256, hex256), "256-bit hex self-match");
        // Leading zeros
        let hex256_padded = format!("0000{}", hex256);
        assert!(
            values_match(&hex256_padded, hex256),
            "256-bit with leading zeros should match"
        );
    }

    #[test]
    fn test_values_match_256bit_specific() {
        // A specific 256-bit value that would be wrong with u64 truncation
        let hex = "DEADBEEFCAFEBABE0123456789ABCDEF0011223344556677AABBCCDDEEFF0099";
        let bin = format!(
            "{}{}{}{}{}{}{}{}",
            "11011110101011011011111011101111", // DEADBEEF
            "11001010111111101011101010111110", // CAFEBABE
            "00000001001000110100010101100111", // 01234567
            "10001001101010111100110111101111", // 89ABCDEF
            "00000000000100010010001000110011", // 00112233
            "01000100010101010110011001110111", // 44556677
            "10101010101110111100110011011101", // AABBCCDD
            "11101110111111110000000010011001", // EEFF0099
        );
        assert!(values_match(hex, &bin), "specific 256-bit: hex vs binary");
    }

    #[test]
    fn test_values_match_512bit() {
        let hex512 = "A".repeat(128); // 512-bit value
        let bin512 = "1010".repeat(128); // same value in binary
        assert!(values_match(&hex512, &bin512), "512-bit match");
    }

    // ---- Regression: bin_to_hex wide values ----

    #[test]
    fn test_bin_to_hex_256bit_zeros() {
        let bin = "0".repeat(256);
        assert_eq!(bin_to_hex(&bin), Some("0".to_string()));
    }

    #[test]
    fn test_bin_to_hex_256bit_ones() {
        let bin = "1".repeat(256);
        let hex = bin_to_hex(&bin).unwrap();
        // 256 bits of 1 = 64 hex F's
        assert_eq!(hex, "F".repeat(64));
    }

    #[test]
    fn test_bin_to_hex_non_multiple_of_4() {
        // 255 bits: partial leading nibble
        let bin = format!("1{}", "0".repeat(254));
        let hex = bin_to_hex(&bin).unwrap();
        assert_eq!(
            hex.chars().next(),
            Some('4'),
            "255-bit: first nibble should be 4 (0b100...)"
        );

        // 257 bits
        let bin = format!("1{}", "0".repeat(256));
        let hex = bin_to_hex(&bin).unwrap();
        assert!(hex.starts_with('1'), "257-bit: leading nibble should be 1");
        assert_eq!(hex.len(), 65, "257 bits = 65 hex chars");
    }

    // ---- Regression: format_value with mixed x/z ----

    #[test]
    fn test_format_value_mixed_xz() {
        assert_eq!(format_value("xxxx0000xxxx1111"), "xxxx0000xxxx1111");
        assert_eq!(format_value("0000zzzzxxxx0000"), "0000zzzzxxxx0000");
    }

    // ---- Property-based tests ----

    // bin_to_hex preserves numeric value for widths <= 128
    proptest! {
        #[test]
        fn prop_bin_to_hex_preserves_value(val in 0u128..=u128::MAX) {
            let bin = format!("{:b}", val);
            let hex = bin_to_hex(&bin).expect("valid binary should convert");
            let roundtrip = u128::from_str_radix(&hex, 16).expect("hex should parse back");
            prop_assert_eq!(val, roundtrip);
        }

        #[test]
        fn prop_format_value_stabilizes(val in "[01]{1,64}") {
            // format_value converts binary→hex. The hex output may itself
            // look like binary (e.g. "10"), so single-pass idempotency
            // doesn't hold. But it must stabilize after 2 passes.
            let once = format_value(&val);
            let twice = format_value(&once);
            let thrice = format_value(&twice);
            prop_assert_eq!(twice, thrice, "format_value must stabilize after 2 passes");
        }

        #[test]
        fn prop_format_value_never_panics(val in "\\PC{0,100}") {
            let _ = format_value(&val);
        }

        #[test]
        fn prop_values_match_symmetry_hex(
            a in "[0-9a-fA-F]{1,32}",
            b in "[0-9a-fA-F]{1,32}",
        ) {
            // For pure hex (no x/z), matching should be symmetric
            prop_assert_eq!(
                values_match(&a, &b),
                values_match(&b, &a),
                "values_match should be symmetric for pure hex"
            );
        }
    }

    // common_scope_prefix is a prefix of every input
    proptest! {
        #[test]
        fn prop_common_scope_prefix_is_prefix(
            names in prop::collection::vec("[a-z]{1,4}(\\.[a-z]{1,4}){0,5}", 1..10)
        ) {
            let prefix = crate::signal::common_scope_prefix(&names);
            for name in &names {
                prop_assert!(
                    name.starts_with(&prefix),
                    "prefix '{}' is not a prefix of '{}'", prefix, name
                );
            }
        }
    }

    // ---- Radix display control ----

    #[test]
    fn test_parse_radix_suffix() {
        assert_eq!(
            parse_radix_suffix("sig%d").unwrap(),
            ("sig".into(), Radix::Dec)
        );
        assert_eq!(
            parse_radix_suffix("sig").unwrap(),
            ("sig".into(), Radix::Hex)
        );
        assert_eq!(
            parse_radix_suffix("sig%b").unwrap(),
            ("sig".into(), Radix::Bin)
        );
        assert_eq!(
            parse_radix_suffix("sig%h").unwrap(),
            ("sig".into(), Radix::Hex)
        );
        assert_eq!(
            parse_radix_suffix("top.dut.coeff%d").unwrap(),
            ("top.dut.coeff".into(), Radix::Dec)
        );
    }

    #[test]
    fn test_parse_radix_suffix_rejects_unknown_radix() {
        // The silent-drop bug: `%u` is not a radix, so the pattern used to
        // keep the suffix, match no signal, and vanish from the result set.
        let err = parse_radix_suffix("expanded_key%u").unwrap_err();
        assert!(err.contains("unknown radix suffix '%u'"), "{}", err);
        assert!(err.contains("%h"), "{}", err);
    }

    #[test]
    fn test_parse_verilog_literal() {
        assert_eq!(parse_verilog_literal("'d255").unwrap(), "FF");
        assert_eq!(parse_verilog_literal("'d0").unwrap(), "0");
        assert_eq!(parse_verilog_literal("'b11111111").unwrap(), "FF");
        assert_eq!(parse_verilog_literal("'hFF").unwrap(), "FF");
        assert_eq!(parse_verilog_literal("'b10").unwrap(), "2");
        assert_eq!(parse_verilog_literal("'d256").unwrap(), "100");
        assert_eq!(parse_verilog_literal("8'd255").unwrap(), "FF");
        assert_eq!(parse_verilog_literal("'hx").unwrap(), "X");
        assert_eq!(parse_verilog_literal("'hz").unwrap(), "Z");
    }

    // ---- Bare-literal grammar (issue 2) ----

    #[test]
    fn parse_bare_decimal() {
        assert_eq!(parse_verilog_literal("0").unwrap(), "0");
        assert_eq!(parse_verilog_literal("1").unwrap(), "1");
        assert_eq!(parse_verilog_literal("255").unwrap(), "FF");
    }

    #[test]
    fn parse_width_prefixed() {
        assert_eq!(parse_verilog_literal("1'b1").unwrap(), "1");
        assert_eq!(parse_verilog_literal("13'h0C1C").unwrap(), "0C1C");
        assert_eq!(parse_verilog_literal("8'd255").unwrap(), "FF");
    }

    #[test]
    fn reject_bare_hex() {
        let err = parse_verilog_literal("C1C").unwrap_err();
        assert!(err.contains("decimal-only"), "expected hint: {}", err);
        let err = parse_verilog_literal("FF").unwrap_err();
        assert!(err.contains("decimal-only"), "expected hint: {}", err);
    }

    // ---- hex_slice (issue 4c) ----

    #[test]
    fn hex_slice_clean() {
        assert_eq!(hex_slice("0FAB", 16, 7, 0), "AB");
        assert_eq!(hex_slice("0FAB", 16, 15, 8), "0F");
        assert_eq!(hex_slice("0FAB", 16, 15, 0), "0FAB");
    }

    #[test]
    fn hex_slice_preserves_xz_in_range() {
        // X bits in upper byte, slice lower 13 bits → clean hex output.
        assert_eq!(hex_slice("XX0C1C", 24, 12, 0), "0C1C");
    }

    #[test]
    fn hex_slice_strips_xz_outside_range() {
        // The X bits are above the slice — they must not bleed into output.
        let out = hex_slice("XX0C1C", 24, 12, 0);
        assert!(
            !out.contains('X') && !out.contains('x'),
            "X leaked into output: {}",
            out
        );
    }

    #[test]
    fn hex_slice_xz_in_range_preserved() {
        // X bit *inside* the slice range → output must contain X.
        let out = hex_slice("0FXB", 16, 7, 0);
        assert!(
            out.to_ascii_uppercase().contains('X'),
            "expected X in output: {}",
            out
        );
    }

    #[test]
    fn hex_slice_binary_input() {
        // 24-bit VCD raw form: 8 X bits at top, then lower 13 bits = 0x0C1C.
        //   bits 23..16 = X       (8 chars)
        //   bits 15..13 = 000     (3 chars, padding)
        //   bits 12..0  = 0x0C1C  (13 chars: "0110000011100")
        let bin = "xxxxxxxx0000110000011100";
        assert_eq!(bin.len(), 24);
        let out = hex_slice(bin, 24, 12, 0);
        assert_eq!(out, "0C1C");
    }

    #[test]
    fn test_format_value_with_radix_hex() {
        assert_eq!(format_value_with_radix("FF", 8, Radix::Hex), "FF");
        assert_eq!(format_value_with_radix("0", 1, Radix::Hex), "0");
    }

    #[test]
    fn test_format_value_with_radix_dec() {
        assert_eq!(format_value_with_radix("FF", 8, Radix::Dec), "255");
        assert_eq!(format_value_with_radix("0", 8, Radix::Dec), "0");
        assert_eq!(format_value_with_radix("A", 8, Radix::Dec), "10");
        assert_eq!(format_value_with_radix("100", 16, Radix::Dec), "256");
    }

    #[test]
    fn test_format_value_with_radix_bin() {
        assert_eq!(format_value_with_radix("FF", 8, Radix::Bin), "11111111");
        assert_eq!(format_value_with_radix("0", 8, Radix::Bin), "0");
        assert_eq!(format_value_with_radix("A", 8, Radix::Bin), "1010");
    }

    #[test]
    fn test_format_value_with_radix_xz_passthrough() {
        assert_eq!(format_value_with_radix("xxxx", 8, Radix::Dec), "xxxx");
        assert_eq!(format_value_with_radix("zzzz", 8, Radix::Bin), "zzzz");
    }

    #[test]
    fn test_format_value_with_radix_256bit() {
        let hex256 = "F".repeat(64); // 256-bit all-ones
        let dec = format_value_with_radix(&hex256, 256, Radix::Dec);
        assert!(
            dec.starts_with("1157920892373161954235709850086879078532699846656405"),
            "256-bit dec: {}",
            dec
        );
    }

    proptest! {
        #[test]
        fn prop_radix_roundtrip_dec(val in 0u64..=u64::MAX) {
            let hex = format!("{:X}", val);
            let dec = format_value_with_radix(&hex, 64, Radix::Dec);
            let back = parse_verilog_literal(&format!("'d{}", dec)).unwrap();
            prop_assert!(
                canonical_hex_eq(&hex, &back),
                "roundtrip failed: {} -> {} -> {}", hex, dec, back
            );
        }

        #[test]
        fn prop_radix_roundtrip_bin(val in 0u64..=u64::MAX) {
            let hex = format!("{:X}", val);
            let bin = format_value_with_radix(&hex, 64, Radix::Bin);
            let back = parse_verilog_literal(&format!("'b{}", bin)).unwrap();
            prop_assert!(
                canonical_hex_eq(&hex, &back),
                "roundtrip failed: {} -> {} -> {}", hex, bin, back
            );
        }
    }
}
