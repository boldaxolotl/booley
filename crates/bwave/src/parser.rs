//! VCD line parser: header parsing and streaming event dispatch.

use std::collections::HashMap;
use std::fmt;
use std::io::{self, BufRead};
use std::ops::ControlFlow;

use memchr::memchr2;
use rustc_hash::FxHashSet;

use crate::signal::SignalMeta;

/// Parsed VCD header information.
#[derive(Debug)]
pub struct VcdHeader {
    /// All signal declarations found in VCD
    pub signals: Vec<SignalMeta>,
    /// Map from signal ID to indices in `signals` vec (supports aliases)
    pub id_to_indices: HashMap<String, Vec<usize>>,
    /// Conversion factor: ticks * ticks_to_ns = nanoseconds
    pub ticks_to_ns: f64,
    /// Raw timescale string (e.g. "1ns")
    pub timescale_str: String,
    /// Absolute input byte offset immediately after `$enddefinitions $end`.
    pub body_offset: u64,
}

/// Why a VCD timestamp token could not be decoded.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TimestampError {
    MissingDigits,
    Overflow,
    TrailingCharacters,
}

impl fmt::Display for TimestampError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let message = match self {
            TimestampError::MissingDigits => "timestamp has no digits",
            TimestampError::Overflow => "timestamp exceeds u64",
            TimestampError::TrailingCharacters => "timestamp has trailing characters",
        };
        f.write_str(message)
    }
}

/// A fatal error encountered while reading or validating a VCD stream.
#[derive(Debug)]
pub enum VcdParseError {
    Read {
        section: &'static str,
        offset: u64,
        source: io::Error,
    },
    InvalidTimestamp {
        offset: u64,
        text: String,
        reason: TimestampError,
    },
    Cancelled {
        section: &'static str,
        offset: u64,
    },
    Chunk {
        sequence: u64,
        source: Box<VcdParseError>,
    },
    Worker {
        message: String,
    },
}

impl fmt::Display for VcdParseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            VcdParseError::Read {
                section,
                offset,
                source,
            } => write!(f, "VCD {section} read failed at byte {offset}: {source}"),
            VcdParseError::InvalidTimestamp {
                offset,
                text,
                reason,
            } => write!(
                f,
                "invalid VCD timestamp at byte {offset} ({text:?}): {reason}"
            ),
            VcdParseError::Cancelled { section, offset } => {
                write!(f, "VCD {section} processing cancelled at byte {offset}")
            }
            VcdParseError::Chunk { sequence, source } => {
                write!(f, "VCD chunk {sequence} failed: {source}")
            }
            VcdParseError::Worker { message } => write!(f, "VCD worker failed: {message}"),
        }
    }
}

impl std::error::Error for VcdParseError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            VcdParseError::Read { source, .. } => Some(source),
            VcdParseError::Chunk { source, .. } => Some(source.as_ref()),
            VcdParseError::InvalidTimestamp { .. }
            | VcdParseError::Cancelled { .. }
            | VcdParseError::Worker { .. } => None,
        }
    }
}

/// Callback trait for streaming VCD events.
pub trait VcdHandler {
    /// Called after all value changes at timestamp T are settled.
    /// Return `ControlFlow::Break(())` to stop parsing.
    fn on_timestamp(&mut self, time: u64) -> ControlFlow<()>;

    /// Called when a new timestamp is seen, BEFORE value changes are dispatched.
    /// `byte_offset` is the absolute file position of the `#<time>` line.
    fn on_time_update(&mut self, time: u64, byte_offset: u64);

    /// Called for a scalar value change (single char: 0/1/x/z followed by ID).
    fn on_scalar(&mut self, id: &str, value: u8);

    /// Called for a vector value change (b/B prefix, bits string + space + ID).
    fn on_vector(&mut self, id: &str, bits: &str);
}

/// Parse the VCD header up to $enddefinitions.
/// Returns VcdHeader with signal metadata, timescale, and body offset.
pub fn try_parse_header(reader: &mut impl BufRead) -> Result<VcdHeader, VcdParseError> {
    let mut signals = Vec::new();
    let mut id_to_indices: HashMap<String, Vec<usize>> = HashMap::new();
    let mut scope_stack: Vec<String> = Vec::new();
    let mut ticks_to_ns: f64 = 1.0;
    let mut timescale_str = String::from("1ns");

    let mut line_buf = String::new();
    // Accumulate multi-line tokens (VCD allows tokens to span lines)
    let mut token_buf = String::new();
    let mut byte_offset = 0u64;

    loop {
        line_buf.clear();
        let bytes_read = match reader.read_line(&mut line_buf) {
            Ok(0) => break,
            Err(source) => {
                return Err(VcdParseError::Read {
                    section: "header",
                    offset: byte_offset,
                    source,
                });
            }
            Ok(bytes_read) => bytes_read,
        };
        byte_offset += bytes_read as u64;
        let line = line_buf.trim_end_matches(|c| c == '\n' || c == '\r');

        // Accumulate tokens -- VCD allows multi-line $var, $timescale, etc.
        if !token_buf.is_empty() || line.contains('$') {
            if token_buf.is_empty() {
                token_buf = line.to_string();
            } else {
                token_buf.push(' ');
                token_buf.push_str(line);
            }

            // Process complete tokens (containing standalone $end)
            // Must match $end as a complete token -- not inside $enddefinitions
            while let Some(end_pos) = find_standalone_end(&token_buf) {
                let token = token_buf[..end_pos].trim().to_string();
                token_buf = token_buf[end_pos + 4..].to_string();

                if token.starts_with("$enddefinitions") {
                    // Done with header
                    return Ok(VcdHeader {
                        signals,
                        id_to_indices,
                        ticks_to_ns,
                        timescale_str,
                        body_offset: byte_offset,
                    });
                } else if token.starts_with("$scope") {
                    // $scope module <name>
                    let parts: Vec<&str> = token.split_whitespace().collect();
                    if parts.len() >= 3 {
                        scope_stack.push(parts[2].to_string());
                    }
                } else if token.starts_with("$upscope") {
                    scope_stack.pop();
                } else if token.starts_with("$var") {
                    // $var <type> <width> <id> <name> [bit_range]
                    let parts: Vec<&str> = token.split_whitespace().collect();
                    if parts.len() >= 5 {
                        let var_type = parts[1].to_string();
                        let width: u32 = parts[2].parse().unwrap_or(0);
                        let id = parts[3].to_string();
                        let leaf_name = parts[4].to_string();

                        // Build full hierarchical name
                        let full_name = if scope_stack.is_empty() {
                            leaf_name.clone()
                        } else {
                            format!("{}.{}", scope_stack.join("."), leaf_name)
                        };

                        // Append bit range if present in $var (e.g. [7:0])
                        // No space before bracket -- matches vcdvcd behavior
                        let full_name = if parts.len() >= 6 && parts[5].starts_with('[') {
                            format!("{}{}", full_name, parts[5])
                        } else {
                            full_name
                        };

                        let idx = signals.len();
                        signals.push(SignalMeta {
                            name: full_name,
                            id: id.clone(),
                            width,
                            var_type,
                        });
                        id_to_indices.entry(id).or_insert_with(Vec::new).push(idx);
                    }
                } else if token.starts_with("$timescale") {
                    // $timescale <value><unit>
                    let ts_text = token.trim_start_matches("$timescale").trim();
                    timescale_str = ts_text.to_string();
                    ticks_to_ns = parse_timescale(ts_text);
                }
            }
            continue;
        }
    }

    // If we get here, $enddefinitions was never found
    Ok(VcdHeader {
        signals,
        id_to_indices,
        ticks_to_ns,
        timescale_str,
        body_offset: byte_offset,
    })
}

/// Parse a VCD header from a trusted reader.
///
/// Production input paths should call [`try_parse_header`] so I/O failures can
/// be reported to the user. This convenience wrapper keeps in-memory callers
/// concise while still failing loudly instead of treating an error as EOF.
pub fn parse_header(reader: &mut impl BufRead) -> VcdHeader {
    try_parse_header(reader).expect("failed to read VCD header")
}

/// Parse timescale string to ns conversion factor.
/// E.g. "1ns" -> 1.0, "1ps" -> 0.001, "10ns" -> 10.0
fn parse_timescale(s: &str) -> f64 {
    let s = s.trim();
    // Split into numeric part and unit
    let (num_str, unit) = split_timescale(s);
    let num: f64 = num_str.parse().unwrap_or(1.0);

    let unit_to_ns = match unit {
        "fs" => 1e-6,
        "ps" => 1e-3,
        "ns" => 1.0,
        "us" => 1e3,
        "ms" => 1e6,
        "s" => 1e9,
        _ => 1.0, // default to ns
    };

    num * unit_to_ns
}

/// Find the position of a standalone `$end` token in the buffer.
/// Must NOT match inside `$enddefinitions`. `$end` must be followed by
/// whitespace, end-of-string, or preceded by whitespace.
fn find_standalone_end(s: &str) -> Option<usize> {
    let mut start = 0;
    while let Some(pos) = s[start..].find("$end") {
        let abs_pos = start + pos;
        // Check that this $end is not part of a longer $-keyword
        // (e.g. $enddefinitions). After "$end" (4 chars), the next char
        // must be whitespace, EOF, or '$'.
        let after = abs_pos + 4;
        let is_standalone = if after >= s.len() {
            true
        } else {
            let next_byte = s.as_bytes()[after];
            next_byte == b' '
                || next_byte == b'\t'
                || next_byte == b'\n'
                || next_byte == b'\r'
                || next_byte == b'$'
        };
        if is_standalone {
            return Some(abs_pos);
        }
        start = abs_pos + 4;
    }
    None
}

fn split_timescale(s: &str) -> (&str, &str) {
    // Find where letters start
    let idx = s.find(|c: char| c.is_alphabetic()).unwrap_or(s.len());
    let num = s[..idx].trim();
    let unit = s[idx..].trim();
    if num.is_empty() {
        ("1", unit)
    } else {
        (num, unit)
    }
}

/// Parse streaming VCD events after the header.
/// Calls handler methods for timestamps and value changes.
/// Only dispatches events for signal IDs in `watched_ids`.
/// `base_offset` is the absolute byte offset where this reader starts
/// (post-header position), used for index building.
///
/// Callback ordering matches vcdvcd: all value changes at timestamp T
/// are dispatched first, then on_timestamp(T) fires. This means
/// on_timestamp sees the fully settled state at time T.
pub fn parse_streaming_with_offsets(
    reader: &mut impl BufRead,
    watched_ids: &FxHashSet<String>,
    handler: &mut impl VcdHandler,
    base_offset: u64,
) {
    let mut line_buf = String::new();
    let mut in_dumpvars = false;
    let mut in_dumpoff = false;
    let mut current_time: Option<u64> = None;
    let mut malformed_time_count: u64 = 0;
    let mut rel_bytes: u64 = 0;

    loop {
        line_buf.clear();
        let n = match reader.read_line(&mut line_buf) {
            Ok(n) => n,
            Err(e) => {
                eprintln!("WARNING: I/O error reading VCD stream: {e}");
                break;
            }
        };
        if n == 0 {
            break; // EOF
        }
        let line_offset = base_offset + rel_bytes;
        rel_bytes += n as u64;
        let line = line_buf.trim_end_matches(|c| c == '\n' || c == '\r');

        if line.is_empty() {
            continue;
        }

        let first = line.as_bytes()[0];
        match first {
            b'#' => {
                // New timestamp: first fire on_timestamp for the PREVIOUS time
                // (all value changes at previous time are now settled)
                if let Some(prev_time) = current_time {
                    if handler.on_timestamp(prev_time).is_break() {
                        return;
                    }
                }
                in_dumpvars = false;
                match line[1..].trim().parse::<u64>() {
                    Ok(time) => {
                        current_time = Some(time);
                        handler.on_time_update(time, line_offset);
                    }
                    Err(e) => {
                        malformed_time_count += 1;
                        if malformed_time_count <= 5 {
                            eprintln!("WARNING: malformed VCD timestamp line '{}': {}", line, e);
                        }
                    }
                }
            }
            b'0' | b'1' | b'x' | b'z' | b'X' | b'Z' if !in_dumpoff => {
                // Scalar value change: <value><id>
                let id = &line[1..];
                if watched_ids.contains(id) || in_dumpvars {
                    handler.on_scalar(id, first);
                }
            }
            b'b' | b'B' if !in_dumpoff => {
                // Vector value change: b<bits> <id> (space or tab separator)
                if let Some(sep_pos) = memchr2(b' ', b'\t', line.as_bytes()) {
                    let bits = &line[1..sep_pos];
                    let id = line[sep_pos + 1..].trim_start();
                    if watched_ids.contains(id) || in_dumpvars {
                        handler.on_vector(id, bits);
                    }
                }
            }
            b'r' | b'R' if !in_dumpoff => {
                // Real value change: r<value> <id> (space or tab separator)
                if let Some(sep_pos) = memchr2(b' ', b'\t', line.as_bytes()) {
                    let bits = &line[1..sep_pos];
                    let id = line[sep_pos + 1..].trim_start();
                    if watched_ids.contains(id) || in_dumpvars {
                        handler.on_vector(id, bits);
                    }
                }
            }
            b'$' => {
                if line.starts_with("$dumpvars") {
                    in_dumpvars = true;
                } else if line.starts_with("$dumpoff") {
                    // IEEE 1364-2005 §18: $dumpoff sets all dump variables to x.
                    // Use on_vector("x") for all widths — VCD short-form "x"
                    // means all bits unknown. Avoids on_scalar which would
                    // incorrectly update clock/reset tracking state.
                    in_dumpoff = true;
                    for id in watched_ids.iter() {
                        handler.on_vector(id, "x");
                    }
                } else if line.starts_with("$dumpon") {
                    // $dumpon: real values follow as regular value changes.
                    // Do NOT set in_dumpvars — the watched_ids filter must
                    // remain active for subsequent value-change lines.
                    in_dumpoff = false;
                } else if line.starts_with("$end") {
                    in_dumpvars = false;
                }
            }
            _ => {} // skip unknown lines
        }
    }

    // Fire final on_timestamp for the last timestamp in the file
    if let Some(last_time) = current_time {
        let _ = handler.on_timestamp(last_time);
    }

    // Final summary of malformed timestamp lines -- user MUST see this.
    if malformed_time_count > 0 {
        eprintln!(
            "WARNING: {} malformed VCD #<time> line(s) encountered -- \
             value changes after those lines were attributed to the previous timestamp. \
             Results may be incorrect.",
            malformed_time_count
        );
    }
}

/// Convenience wrapper: parse_streaming with base_offset = 0.
pub fn parse_streaming(
    reader: &mut impl BufRead,
    watched_ids: &FxHashSet<String>,
    handler: &mut impl VcdHandler,
) {
    parse_streaming_with_offsets(reader, watched_ids, handler, 0);
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{self, Read};

    struct ErrorReader;

    impl Read for ErrorReader {
        fn read(&mut self, _buf: &mut [u8]) -> io::Result<usize> {
            Err(io::Error::other("injected read error"))
        }
    }

    impl BufRead for ErrorReader {
        fn fill_buf(&mut self) -> io::Result<&[u8]> {
            Err(io::Error::other("injected read error"))
        }

        fn consume(&mut self, _amount: usize) {}
    }

    #[test]
    fn header_read_error_is_not_eof() {
        let error = try_parse_header(&mut ErrorReader).unwrap_err();
        assert!(matches!(
            error,
            VcdParseError::Read {
                section: "header",
                offset: 0,
                ..
            }
        ));
    }

    #[test]
    fn test_parse_timescale() {
        assert!((parse_timescale("1ns") - 1.0).abs() < 1e-10);
        assert!((parse_timescale("1ps") - 0.001).abs() < 1e-10);
        assert!((parse_timescale("10ns") - 10.0).abs() < 1e-10);
        assert!((parse_timescale("1us") - 1000.0).abs() < 1e-10);
    }

    #[test]
    fn test_parse_header_basic() {
        let vcd = "\
$timescale 1ns $end
$scope module tb $end
$scope module dut $end
$var wire 1 ! clk $end
$var wire 1 \" rstn $end
$var wire 8 # data [7:0] $end
$upscope $end
$upscope $end
$enddefinitions $end
";
        let mut reader = std::io::BufReader::new(vcd.as_bytes());
        let header = parse_header(&mut reader);

        assert_eq!(header.signals.len(), 3);
        assert_eq!(header.signals[0].name, "tb.dut.clk");
        assert_eq!(header.signals[0].id, "!");
        assert_eq!(header.signals[0].width, 1);
        assert_eq!(header.signals[1].name, "tb.dut.rstn");
        assert_eq!(header.signals[2].name, "tb.dut.data[7:0]");
        assert_eq!(header.signals[2].width, 8);
        assert!((header.ticks_to_ns - 1.0).abs() < 1e-10);
    }

    // ---- Regression: $scope begin / $scope task / $scope function ----

    #[test]
    fn test_parse_header_scope_types() {
        let vcd = "\
$timescale 1ns $end
$scope module tb $end
$scope begin gen_block $end
$var wire 1 ! sig_a $end
$upscope $end
$scope task my_task $end
$var wire 1 \" sig_b $end
$upscope $end
$scope function my_func $end
$var wire 1 # sig_c $end
$upscope $end
$upscope $end
$enddefinitions $end
";
        let mut reader = std::io::BufReader::new(vcd.as_bytes());
        let header = parse_header(&mut reader);

        assert_eq!(header.signals.len(), 3);
        assert_eq!(header.signals[0].name, "tb.gen_block.sig_a");
        assert_eq!(header.signals[1].name, "tb.my_task.sig_b");
        assert_eq!(header.signals[2].name, "tb.my_func.sig_c");
    }

    // ---- Regression: multi-character VCD IDs ----

    #[test]
    fn test_parse_header_multichar_ids() {
        let vcd = "\
$timescale 1ns $end
$scope module tb $end
$var wire 1 A1 sig_a $end
$var wire 1 !! sig_b $end
$var wire 1 n0 sig_c $end
$var wire 8 {2 data [7:0] $end
$upscope $end
$enddefinitions $end
";
        let mut reader = std::io::BufReader::new(vcd.as_bytes());
        let header = parse_header(&mut reader);

        assert_eq!(header.signals.len(), 4);
        assert_eq!(header.signals[0].id, "A1");
        assert_eq!(header.signals[1].id, "!!");
        assert_eq!(header.signals[2].id, "n0");
        assert_eq!(header.signals[3].id, "{2");
    }

    // ---- Regression: tab-delimited vector lines ----

    /// A test handler that records vector values for verification.
    struct RecordingHandler {
        vectors: Vec<(String, String)>, // (id, bits)
        scalars: Vec<(String, u8)>,
    }
    impl RecordingHandler {
        fn new() -> Self {
            Self {
                vectors: Vec::new(),
                scalars: Vec::new(),
            }
        }
    }
    impl VcdHandler for RecordingHandler {
        fn on_timestamp(&mut self, _time: u64) -> std::ops::ControlFlow<()> {
            std::ops::ControlFlow::Continue(())
        }
        fn on_time_update(&mut self, _time: u64, _byte_offset: u64) {}
        fn on_scalar(&mut self, id: &str, value: u8) {
            self.scalars.push((id.to_string(), value));
        }
        fn on_vector(&mut self, id: &str, bits: &str) {
            self.vectors.push((id.to_string(), bits.to_string()));
        }
    }

    #[test]
    fn test_tab_delimited_vector_lines() {
        let vcd = "$timescale 1ns $end\n\
$scope module tb $end\n\
$var wire 8 # data [7:0] $end\n\
$upscope $end\n\
$enddefinitions $end\n\
#0\n\
b00001010\t#\n\
#10\n\
b11111111\t#\n";
        let mut reader = std::io::BufReader::new(vcd.as_bytes());
        let _header = parse_header(&mut reader);
        let mut watched = FxHashSet::default();
        watched.insert("#".to_string());
        let mut handler = RecordingHandler::new();
        parse_streaming(&mut reader, &watched, &mut handler);

        assert_eq!(
            handler.vectors.len(),
            2,
            "both tab-delimited vectors should be parsed"
        );
        assert_eq!(
            handler.vectors[0],
            ("#".to_string(), "00001010".to_string())
        );
        assert_eq!(
            handler.vectors[1],
            ("#".to_string(), "11111111".to_string())
        );
    }

    #[test]
    fn test_tab_delimited_real_values() {
        let vcd = "$timescale 1ns $end\n\
$scope module tb $end\n\
$var real 64 ! voltage $end\n\
$upscope $end\n\
$enddefinitions $end\n\
#0\n\
r1.5\t!\n\
#10\n\
r3.14\t!\n";
        let mut reader = std::io::BufReader::new(vcd.as_bytes());
        let _header = parse_header(&mut reader);
        let mut watched = FxHashSet::default();
        watched.insert("!".to_string());
        let mut handler = RecordingHandler::new();
        parse_streaming(&mut reader, &watched, &mut handler);

        assert_eq!(
            handler.vectors.len(),
            2,
            "tab-delimited real values should be parsed"
        );
        assert_eq!(handler.vectors[0].1, "1.5");
        assert_eq!(handler.vectors[1].1, "3.14");
    }

    // ---- Regression: malformed timestamps ----

    #[test]
    fn test_malformed_timestamp_warning() {
        let vcd = "$timescale 1ns $end\n\
$scope module tb $end\n\
$var wire 1 ! sig $end\n\
$upscope $end\n\
$enddefinitions $end\n\
#0\n\
0!\n\
#abc\n\
1!\n\
#10\n\
0!\n";
        let mut reader = std::io::BufReader::new(vcd.as_bytes());
        let _header = parse_header(&mut reader);
        let mut watched = FxHashSet::default();
        watched.insert("!".to_string());
        let mut handler = RecordingHandler::new();
        parse_streaming(&mut reader, &watched, &mut handler);

        // Values after malformed #abc attributed to previous timestamp (0)
        // then #10 updates correctly. Should have 3 scalar events total.
        assert_eq!(handler.scalars.len(), 3);
    }

    // ---- Regression: truncated VCD (EOF mid-token) ----

    #[test]
    fn test_truncated_vcd_eof_mid_header() {
        let vcd = "$timescale 1ns $end\n\
$scope module tb $end\n\
$var wire 1 ! clk"; // EOF mid-$var, no $end, no $enddefinitions
        let mut reader = std::io::BufReader::new(vcd.as_bytes());
        let header = parse_header(&mut reader);
        // Should not panic; may find 0 or 1 signals depending on parsing
        assert!(header.signals.len() <= 1);
    }

    // ---- Regression: CRLF line endings ----

    #[test]
    fn test_crlf_line_endings() {
        let vcd = "$timescale 1ns $end\r\n\
$scope module tb $end\r\n\
$var wire 1 ! clk $end\r\n\
$var wire 8 # data [7:0] $end\r\n\
$upscope $end\r\n\
$enddefinitions $end\r\n\
#0\r\n\
0!\r\n\
b00001010 #\r\n\
#10\r\n\
1!\r\n\
b11111111 #\r\n";
        let mut reader = std::io::BufReader::new(vcd.as_bytes());
        let header = parse_header(&mut reader);
        assert_eq!(header.signals.len(), 2, "CRLF: should parse 2 signals");

        let mut watched = FxHashSet::default();
        watched.insert("!".to_string());
        watched.insert("#".to_string());
        let mut handler = RecordingHandler::new();
        parse_streaming(&mut reader, &watched, &mut handler);
        assert!(
            handler.scalars.len() >= 2,
            "CRLF: should parse scalar events"
        );
        assert!(
            handler.vectors.len() >= 2,
            "CRLF: should parse vector events"
        );
    }

    // ---- Regression: $comment in header ----

    #[test]
    fn test_comment_in_header() {
        let vcd = "\
$comment This is a test VCD file $end
$date Mon Jan 01 00:00:00 2024 $end
$version Verilator 5.0 $end
$timescale 1ns $end
$scope module tb $end
$var wire 1 ! clk $end
$upscope $end
$enddefinitions $end
";
        let mut reader = std::io::BufReader::new(vcd.as_bytes());
        let header = parse_header(&mut reader);
        assert_eq!(header.signals.len(), 1);
        assert_eq!(header.signals[0].name, "tb.clk");
    }

    // ---- Regression: $timescale / $var spanning multiple lines ----

    #[test]
    fn test_multiline_var() {
        let vcd = "\
$timescale
    1ns
$end
$scope module tb $end
$var wire 1
    ! clk
$end
$upscope $end
$enddefinitions $end
";
        let mut reader = std::io::BufReader::new(vcd.as_bytes());
        let header = parse_header(&mut reader);
        assert_eq!(header.signals.len(), 1, "multi-line $var should parse");
        assert_eq!(header.signals[0].name, "tb.clk");
    }

    // ---- Regression: duplicate $var IDs (aliases) ----

    #[test]
    fn test_duplicate_var_ids_alias() {
        let vcd = "\
$timescale 1ns $end
$scope module tb $end
$var wire 8 # data_a [7:0] $end
$var wire 8 # data_b [7:0] $end
$upscope $end
$enddefinitions $end
";
        let mut reader = std::io::BufReader::new(vcd.as_bytes());
        let header = parse_header(&mut reader);
        assert_eq!(header.signals.len(), 2, "aliases should both appear");
        // Both should map to the same VCD ID
        let indices = header.id_to_indices.get("#").unwrap();
        assert_eq!(indices.len(), 2, "alias ID should map to 2 signal indices");
    }

    // ---- Regression: $dumpvars in streaming section ----

    #[test]
    fn test_dumpvars_initial_values() {
        let vcd = "$timescale 1ns $end\n\
$scope module tb $end\n\
$var wire 1 ! clk $end\n\
$var wire 8 # data [7:0] $end\n\
$upscope $end\n\
$enddefinitions $end\n\
$dumpvars\n\
0!\n\
b00000000 #\n\
$end\n\
#0\n\
#10\n\
1!\n\
b00001010 #\n";
        let mut reader = std::io::BufReader::new(vcd.as_bytes());
        let _header = parse_header(&mut reader);
        let mut watched = FxHashSet::default();
        watched.insert("!".to_string());
        watched.insert("#".to_string());
        let mut handler = RecordingHandler::new();
        parse_streaming(&mut reader, &watched, &mut handler);

        // $dumpvars section should capture initial values even for unwatched IDs
        // (in_dumpvars = true bypasses the watched_ids filter)
        assert!(
            handler.scalars.len() >= 2,
            "should have scalars from dumpvars + regular section"
        );
    }

    // ---- Regression: $dumpoff / $dumpon ----

    #[test]
    fn test_dumpoff_sets_all_to_x() {
        let vcd = "$timescale 1ns $end\n\
$scope module tb $end\n\
$var wire 1 ! clk $end\n\
$var wire 8 # data [7:0] $end\n\
$upscope $end\n\
$enddefinitions $end\n\
#0\n\
1!\n\
b00001010 #\n\
#10\n\
$dumpoff\n\
$end\n\
#20\n\
$dumpon\n\
1!\n\
b11111111 #\n\
$end\n\
#30\n\
0!\n\
b00000000 #\n";
        let mut reader = std::io::BufReader::new(vcd.as_bytes());
        let _header = parse_header(&mut reader);
        let mut watched = FxHashSet::default();
        watched.insert("!".to_string());
        watched.insert("#".to_string());
        let mut handler = RecordingHandler::new();
        parse_streaming(&mut reader, &watched, &mut handler);

        // At #10, $dumpoff should emit on_vector("x") for both watched IDs
        // (uses on_vector, not on_scalar, so it works for any signal width)
        let dumpoff_x_count = handler
            .vectors
            .iter()
            .filter(|(_, bits)| bits == "x")
            .count();
        assert_eq!(
            dumpoff_x_count, 2,
            "$dumpoff should emit on_vector(x) for all 2 watched signals, got {}",
            dumpoff_x_count
        );

        // After $dumpon at #20, values should resume (1! and b11111111)
        // $dumpon must NOT set in_dumpvars — only watched IDs pass through
        let post_dumpon_scalars: Vec<_> =
            handler.scalars.iter().filter(|(_, v)| *v == b'0').collect();
        assert!(
            !post_dumpon_scalars.is_empty(),
            "should see scalar changes after $dumpon"
        );

        let post_dumpon_vectors: Vec<_> = handler
            .vectors
            .iter()
            .filter(|(_, bits)| bits == "00000000")
            .collect();
        assert!(
            !post_dumpon_vectors.is_empty(),
            "should see vector changes after $dumpon"
        );
    }

    #[test]
    fn test_dumpoff_suppresses_value_changes() {
        // During $dumpoff, any value change lines should be silently dropped
        let vcd = "$timescale 1ns $end\n\
$scope module tb $end\n\
$var wire 1 ! sig $end\n\
$upscope $end\n\
$enddefinitions $end\n\
#0\n\
0!\n\
#10\n\
$dumpoff\n\
$end\n\
#15\n\
1!\n\
#20\n\
$dumpon\n\
0!\n\
$end\n\
#30\n\
1!\n";
        let mut reader = std::io::BufReader::new(vcd.as_bytes());
        let _header = parse_header(&mut reader);
        let mut watched = FxHashSet::default();
        watched.insert("!".to_string());
        let mut handler = RecordingHandler::new();
        parse_streaming(&mut reader, &watched, &mut handler);

        // Events: #0: 0!(scalar), #10: dumpoff -> x(vector), #15: 1! SUPPRESSED,
        // #20: dumpon -> 0!(scalar), #30: 1!(scalar)
        // Scalars: 0, 0, 1 (the x from dumpoff goes to vectors now)
        let scalar_values: Vec<u8> = handler.scalars.iter().map(|(_, v)| *v).collect();
        assert_eq!(
            scalar_values,
            vec![b'0', b'0', b'1'],
            "1! at #15 during dumpoff should be suppressed"
        );
        // $dumpoff emits one vector "x" for the watched signal
        let dumpoff_vectors: Vec<_> = handler
            .vectors
            .iter()
            .filter(|(_, bits)| bits == "x")
            .collect();
        assert_eq!(
            dumpoff_vectors.len(),
            1,
            "$dumpoff should emit one on_vector(x)"
        );
    }

    #[test]
    fn test_dumpoff_uses_vector_for_multibit() {
        // Regression: $dumpoff must use on_vector (not on_scalar) so multi-bit
        // signals get "x" (all-bits-unknown) instead of a scalar byte.
        let vcd = "$timescale 1ns $end\n\
$scope module tb $end\n\
$var wire 1 ! clk $end\n\
$var wire 16 # bus [15:0] $end\n\
$var wire 32 $ addr [31:0] $end\n\
$upscope $end\n\
$enddefinitions $end\n\
#0\n\
1!\n\
b0000000000001010 #\n\
b00000000000000000000000011111111 $\n\
#10\n\
$dumpoff\n\
$end\n\
#20\n";
        let mut reader = std::io::BufReader::new(vcd.as_bytes());
        let _header = parse_header(&mut reader);
        let mut watched = FxHashSet::default();
        watched.insert("!".to_string());
        watched.insert("#".to_string());
        watched.insert("$".to_string());
        let mut handler = RecordingHandler::new();
        parse_streaming(&mut reader, &watched, &mut handler);

        // All 3 signals should get on_vector("x") from $dumpoff — none via on_scalar
        let dumpoff_x: Vec<_> = handler
            .vectors
            .iter()
            .filter(|(_, bits)| bits == "x")
            .collect();
        assert_eq!(
            dumpoff_x.len(),
            3,
            "all 3 watched signals should get on_vector(x)"
        );
        let scalar_x_count = handler.scalars.iter().filter(|(_, v)| *v == b'x').count();
        assert_eq!(
            scalar_x_count, 0,
            "$dumpoff must not emit on_scalar for any signal"
        );
    }

    #[test]
    fn test_dumpon_does_not_leak_unwatched_ids() {
        // Regression: $dumpon must NOT set in_dumpvars=true, which would bypass
        // the watched_ids filter and leak unwatched signal IDs to the handler.
        let vcd = "$timescale 1ns $end\n\
$scope module tb $end\n\
$var wire 1 ! watched $end\n\
$var wire 1 \" unwatched $end\n\
$upscope $end\n\
$enddefinitions $end\n\
#0\n\
0!\n\
0\"\n\
#10\n\
$dumpoff\n\
$end\n\
#20\n\
$dumpon\n\
1!\n\
1\"\n\
$end\n\
#30\n";
        let mut reader = std::io::BufReader::new(vcd.as_bytes());
        let _header = parse_header(&mut reader);
        let mut watched = FxHashSet::default();
        watched.insert("!".to_string());
        // \" is NOT in watched_ids
        let mut handler = RecordingHandler::new();
        parse_streaming(&mut reader, &watched, &mut handler);

        // Only watched signal "!" should appear in scalars
        let unwatched: Vec<_> = handler
            .scalars
            .iter()
            .filter(|(id, _)| id == "\"")
            .collect();
        assert!(
            unwatched.is_empty(),
            "unwatched signal should not leak through after $dumpon, got {:?}",
            unwatched
        );
        // The watched signal should still get through
        let watched_events: Vec<_> = handler.scalars.iter().filter(|(id, _)| id == "!").collect();
        assert!(
            watched_events.len() >= 2,
            "watched signal should have events before and after dumpon"
        );
    }
}
