//! Signal metadata, glob matching, and scope prefix utilities.

use globset::{Glob, GlobMatcher};

/// Metadata for a single VCD signal declaration.
#[derive(Debug, Clone)]
pub struct SignalMeta {
    /// Full hierarchical name (e.g. "tb.dut.data[7:0]")
    pub name: String,
    /// VCD identifier code (short ASCII string like "!" or "#")
    pub id: String,
    /// Bit width
    pub width: u32,
    /// Variable type from $var (wire, reg, etc.)
    pub var_type: String,
}

/// Strip bit-select brackets from a signal name.
/// "dut.state[3:0]" -> "dut.state"
pub fn strip_bit_select(name: &str) -> &str {
    match name.find('[') {
        Some(i) => &name[..i],
        None => name,
    }
}

/// Check if a signal name matches any glob pattern.
/// Strips bit-select brackets before matching (so "*state*" matches "dut.state[3:0]").
/// Matches against both stripped and original forms.
pub fn match_signal(name: &str, matchers: &[(GlobMatcher, GlobMatcher)]) -> bool {
    let stripped = strip_bit_select(name);
    for (orig_matcher, stripped_matcher) in matchers {
        if stripped_matcher.is_match(stripped) || orig_matcher.is_match(name) {
            return true;
        }
    }
    false
}

/// Byte offset of a trailing Verilog index / bit-range suffix — `[56]`,
/// `[7:0]` — or None when the trailing bracket group is a genuine glob
/// character class (`[0-3]`, `[abc]`) or absent.
///
/// This distinction is what makes `-s "words[56]"` work at all: to globset
/// `[56]` reads as "one character, either 5 or 6", so an element-indexed
/// name looked like a wildcard pattern, skipped the bare-name `*` wrap, and
/// matched nothing — array elements appeared not to exist in the trace.
fn index_suffix_start(p: &str) -> Option<usize> {
    let inner_end = p.strip_suffix(']')?.len();
    let open = p[..inner_end].rfind('[')?;
    let inner = &p[open + 1..inner_end];
    let index_like = !inner.is_empty()
        && inner.bytes().all(|b| b.is_ascii_digit() || b == b':')
        && inner.bytes().any(|b| b.is_ascii_digit());
    index_like.then_some(open)
}

/// Escape a literal `[` for globset — `[[]` is the character class whose one
/// member is `[` — so a Verilog index matches itself instead of acting as a
/// class over its own digits.
fn escape_index_bracket(p: &str, open: usize) -> String {
    format!("{}[[]{}", &p[..open], &p[open + 1..])
}

/// Auto-wrap a pattern with a leading `*` if it contains no glob metacharacters.
/// This gives suffix matching for bare signal names — see ADR 0013.
///   "input_data"  -> "*input_data"  (matches "tb.dut.input_data", NOT "input_data_plain")
///   "*input_data" -> "*input_data"  (already has wildcards, keep as-is)
///   "*data*"      -> "*data*"       (explicit substring opt-in)
///   "words[56]"   -> "*words[56]"   (trailing index is a literal, not a class)
fn auto_wrap_pattern(p: &str) -> String {
    let meta_scan = match index_suffix_start(p) {
        Some(open) => &p[..open],
        None => p,
    };
    if meta_scan.contains('*') || meta_scan.contains('?') || meta_scan.contains('[') {
        p.to_string()
    } else {
        format!("*{}", p)
    }
}

/// Compile glob patterns into matchers. Returns (original_matcher, stripped_matcher) pairs.
/// The stripped matcher also has bit-select removed from the pattern itself.
/// Patterns without glob metacharacters are auto-wrapped with `*...*` for substring matching.
///
/// Returns Err with the original pattern text and underlying glob error if compilation fails --
/// previously silently fell back to `*`, which matched every signal (typo `--signals "[bad"`
/// would dump the entire VCD; `--sample-at "[oops"` safety check never fired).
pub fn compile_patterns(patterns: &[String]) -> Result<Vec<(GlobMatcher, GlobMatcher)>, String> {
    patterns
        .iter()
        .map(|p| {
            let wrapped = auto_wrap_pattern(p);
            // A trailing Verilog index is compiled as a literal; the stripped
            // matcher below still covers the case where the simulator dumped
            // the whole vector under its base name.
            let orig_pat = match index_suffix_start(&wrapped) {
                Some(open) => escape_index_bracket(&wrapped, open),
                None => wrapped.clone(),
            };
            let orig = Glob::new(&orig_pat)
                .map_err(|e| format!("invalid glob pattern '{}': {}", p, e))?
                .compile_matcher();
            let stripped_pat = strip_bit_select(&wrapped);
            let stripped = Glob::new(stripped_pat)
                .map_err(|e| format!("invalid glob pattern '{}': {}", p, e))?
                .compile_matcher();
            Ok((orig, stripped))
        })
        .collect()
}

/// Filter signals to those within a hierarchical scope.
/// Auto-appends `.` if missing to prevent "tb.dut" matching "tb.dut_extra.x".
pub fn signals_in_scope(signals: &[SignalMeta], scope: &str) -> Vec<SignalMeta> {
    let scope_dot = if scope.ends_with('.') {
        scope.to_string()
    } else {
        format!("{}.", scope)
    };
    signals
        .iter()
        .filter(|s| s.name.starts_with(&scope_dot))
        .cloned()
        .collect()
}

/// Find the deepest common hierarchical prefix shared by all signal names.
/// Returns prefix with trailing dot (e.g. "tb.dut.") or empty string.
/// Truncated to dot boundary so partial leaf names are never split.
pub fn common_scope_prefix(names: &[String]) -> String {
    if names.is_empty() {
        return String::new();
    }
    if names.len() == 1 {
        return match names[0].rfind('.') {
            Some(dot) if dot > 0 => names[0][..=dot].to_string(),
            _ => String::new(),
        };
    }
    // Longest common character prefix, truncated to last dot boundary
    let first = names.iter().min().unwrap();
    let last = names.iter().max().unwrap();
    let common_len = first
        .bytes()
        .zip(last.bytes())
        .take_while(|(a, b)| a == b)
        .count();
    let prefix = &first[..common_len];
    match prefix.rfind('.') {
        Some(dot) if dot > 0 => prefix[..=dot].to_string(),
        _ => String::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_strip_bit_select() {
        assert_eq!(strip_bit_select("dut.data[7:0]"), "dut.data");
        assert_eq!(strip_bit_select("clk"), "clk");
        assert_eq!(strip_bit_select("a[0]"), "a");
    }

    #[test]
    fn test_common_scope_prefix() {
        let names = vec!["tb.dut.a".into(), "tb.dut.b".into()];
        assert_eq!(common_scope_prefix(&names), "tb.dut.");

        let names = vec!["tb.dut.sub.flag".into()];
        assert_eq!(common_scope_prefix(&names), "tb.dut.sub.");

        let names: Vec<String> = vec![];
        assert_eq!(common_scope_prefix(&names), "");

        let names = vec!["a".into(), "b".into()];
        assert_eq!(common_scope_prefix(&names), "");
    }

    #[test]
    fn test_match_signal() {
        let pats = compile_patterns(&["*data*".into()]).unwrap();
        assert!(match_signal("tb.dut.data[7:0]", &pats));
        assert!(!match_signal("tb.dut.clk", &pats));

        let pats = compile_patterns(&["*".into()]).unwrap();
        assert!(match_signal("anything", &pats));
    }

    #[test]
    fn test_auto_wrap_bare_pattern() {
        // Bare name (no glob chars) → suffix match (ADR 0013)
        let pats = compile_patterns(&["input_data".into()]).unwrap();
        assert!(match_signal("tb.dut.input_data", &pats));
        assert!(match_signal("tb.dut.input_data[7:0]", &pats));
        assert!(match_signal("input_data", &pats));
        assert!(!match_signal("tb.dut.output_data", &pats));

        // Explicit glob should not be auto-wrapped
        let pats = compile_patterns(&["input_data*".into()]).unwrap();
        assert!(!match_signal("tb.dut.input_data", &pats)); // no leading *
        assert!(match_signal("input_data_bus", &pats));

        // Hierarchical bare name
        let pats = compile_patterns(&["dut.clk".into()]).unwrap();
        assert!(match_signal("tb.dut.clk", &pats));
    }

    // ---- Suffix-match semantics (ADR 0013) -------------------------------

    #[test]
    fn bare_name_suffix_matches() {
        // Bare name suffix-matches the full hierarchical name.
        let pats = compile_patterns(&["dmem_addr".into()]).unwrap();
        assert!(match_signal("tb.dut.dmem_addr", &pats));
        // Must NOT cross a token boundary — `dmem_addr_next` doesn't end with `dmem_addr`.
        assert!(!match_signal("tb.dut.dmem_addr_next", &pats));
    }

    #[test]
    fn bare_name_no_substring_match() {
        // `dmem` is not a suffix of `dmem_addr` (which ends in `_addr`).
        let pats = compile_patterns(&["dmem".into()]).unwrap();
        assert!(!match_signal("tb.dut.dmem_addr", &pats));
        assert!(match_signal("tb.dut.dmem", &pats));
    }

    #[test]
    fn wildcard_substring_still_works() {
        // Explicit `*X*` retains substring semantics.
        let pats = compile_patterns(&["*dmem*".into()]).unwrap();
        assert!(match_signal("tb.dut.dmem_addr", &pats));
        assert!(match_signal("tb.dut.dmem_addr_next", &pats));
        assert!(match_signal("tb.dut.dmem", &pats));
    }

    #[test]
    fn wildcard_prefix_anchor() {
        // `*dmem_addr` matches `dmem_addr` but NOT `dmem_addr_next`.
        let pats = compile_patterns(&["*dmem_addr".into()]).unwrap();
        assert!(match_signal("tb.dut.dmem_addr", &pats));
        assert!(!match_signal("tb.dut.dmem_addr_next", &pats));
    }

    #[test]
    fn test_signals_in_scope() {
        let sigs = vec![
            SignalMeta {
                name: "tb.dut.a".into(),
                id: "!".into(),
                width: 1,
                var_type: "wire".into(),
            },
            SignalMeta {
                name: "tb.dut.b[7:0]".into(),
                id: "#".into(),
                width: 8,
                var_type: "reg".into(),
            },
            SignalMeta {
                name: "tb.dut_extra.c".into(),
                id: "$".into(),
                width: 1,
                var_type: "wire".into(),
            },
            SignalMeta {
                name: "tb.clk".into(),
                id: "%".into(),
                width: 1,
                var_type: "wire".into(),
            },
        ];
        let filtered = signals_in_scope(&sigs, "tb.dut");
        assert_eq!(filtered.len(), 2);
        assert_eq!(filtered[0].name, "tb.dut.a");
        assert_eq!(filtered[1].name, "tb.dut.b[7:0]");

        // With trailing dot — same result
        let filtered2 = signals_in_scope(&sigs, "tb.dut.");
        assert_eq!(filtered2.len(), 2);

        // Scope matching nothing
        let filtered3 = signals_in_scope(&sigs, "tb.nonexistent");
        assert!(filtered3.is_empty());
    }

    #[test]
    fn test_invalid_glob_returns_err() {
        // Typo like "[bad" (unterminated bracket) must NOT silently fall back to '*'
        // (which would match every signal in the VCD).
        let result = compile_patterns(&["[bad".into()]);
        assert!(result.is_err());
        let msg = result.unwrap_err();
        assert!(
            msg.contains("[bad"),
            "error should name the offending pattern: {}",
            msg
        );
    }
}
