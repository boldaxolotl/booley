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

/// One compiled `-s` pattern: the full-path matchers plus the common scope it
/// is also allowed to match relative to.
#[derive(Debug, Clone)]
pub struct SignalPattern {
    /// Matches the original (bit-select included) name.
    full: GlobMatcher,
    /// Matches the name with its bit-select removed.
    stripped: GlobMatcher,
    /// `common_scope_prefix` of the store (trailing `.`), or empty for none.
    scope: String,
}

impl SignalPattern {
    /// Match the full path, or the path relative to the common scope -- the
    /// names `bwave list` prints, so `dut.*` finds `TOP.tb.dut.*`.
    ///
    /// The relative path is a suffix of the full path, so a pattern that
    /// already matches by suffix (a bare name, or a `*`-led glob) selects the
    /// same signals as before; only anchored globs gain matches. A signal
    /// outside the scope has no relative path and is never reached.
    fn is_match(&self, name: &str) -> bool {
        self.in_full_or_relative(name, |matcher, path| matcher.matches_stripped(path))
    }

    /// Like `is_match`, but tests `name` as given instead of stripping its
    /// bit-select first (callers that already removed a literal `[N]`).
    pub fn is_match_unstripped(&self, name: &str) -> bool {
        self.in_full_or_relative(name, |matcher, path| matcher.matches_as_given(path))
    }

    fn in_full_or_relative(&self, name: &str, test: fn(&Self, &str) -> bool) -> bool {
        test(self, name)
            || (!self.scope.is_empty()
                && name
                    .strip_prefix(self.scope.as_str())
                    .is_some_and(|relative| test(self, relative)))
    }

    /// Test one path against both the bit-select-stripped and original forms.
    fn matches_stripped(&self, path: &str) -> bool {
        self.stripped.is_match(strip_bit_select(path)) || self.full.is_match(path)
    }

    /// Test one path against both matchers without stripping the path.
    fn matches_as_given(&self, path: &str) -> bool {
        self.full.is_match(path) || self.stripped.is_match(path)
    }
}

/// Check if a signal name matches any glob pattern.
/// Strips bit-select brackets before matching (so "*state*" matches "dut.state[3:0]").
/// Matches against both stripped and original forms, in the full path and
/// relative to the common scope.
pub fn match_signal(name: &str, matchers: &[SignalPattern]) -> bool {
    matchers.iter().any(|pattern| pattern.is_match(name))
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

/// Compile glob patterns into matchers that also match relative to `scope`.
/// The stripped matcher also has bit-select removed from the pattern itself.
/// Patterns without glob metacharacters are auto-wrapped with a leading `*` for suffix matching.
///
/// `scope` is the store's `common_scope_prefix` (trailing `.`, or empty). It is
/// a required argument so no call site keeps full-path-only matching by omission.
///
/// Returns Err with the original pattern text and underlying glob error if compilation fails --
/// previously silently fell back to `*`, which matched every signal (typo `--signals "[bad"`
/// would dump the entire VCD; `--sample-at "[oops"` safety check never fired).
pub fn compile_patterns(patterns: &[String], scope: &str) -> Result<Vec<SignalPattern>, String> {
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
            let full = Glob::new(&orig_pat)
                .map_err(|e| format!("invalid glob pattern '{}': {}", p, e))?
                .compile_matcher();
            let stripped_pat = strip_bit_select(&wrapped);
            let stripped = Glob::new(stripped_pat)
                .map_err(|e| format!("invalid glob pattern '{}': {}", p, e))?
                .compile_matcher();
            Ok(SignalPattern {
                full,
                stripped,
                scope: scope.to_string(),
            })
        })
        .collect()
}

/// Escape glob metacharacters of a literal path prefix, each as a one-member
/// character class, so a generate-block name such as `gen[0].` stays literal.
fn escape_glob_literal(prefix: &str) -> String {
    let mut escaped = String::with_capacity(prefix.len());
    for ch in prefix.chars() {
        match ch {
            '[' | '*' | '?' | '{' | '\\' => {
                escaped.push('[');
                escaped.push(ch);
                escaped.push(']');
            }
            _ => escaped.push(ch),
        }
    }
    escaped
}

/// Suggest the full-path form of a glob that matches nothing as written.
///
/// Finds the first name (store order) the glob matches below some scope
/// boundary, then returns that name's shallowest `.`-terminated prefix whose
/// remainder matches, escaped, followed by the glob: `u_core.*` over
/// `TOP.tb.dut.u_core.state` gives `TOP.tb.dut.u_core.*`. None when no
/// signal matches under any boundary (or the glob is invalid).
pub fn anchored_form<'a>(
    pattern: &str,
    names: impl IntoIterator<Item = &'a str>,
) -> Option<String> {
    let any_depth = compile_patterns(&[format!("*.{pattern}")], "").ok()?;
    let anchored = compile_patterns(&[pattern.to_string()], "").ok()?;
    let name = names
        .into_iter()
        .find(|name| match_signal(name, &any_depth))?;
    name.match_indices('.')
        .map(|(dot, _)| name.split_at(dot + 1))
        .find(|(_, remainder)| match_signal(remainder, &anchored))
        .map(|(prefix, _)| format!("{}{}", escape_glob_literal(prefix), pattern))
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

/// Distinct first-level scopes represented by the signal set.
///
/// A trace can be perfectly queryable without one common scope. Verilator's
/// Cocotb main, for example, emits `$rootio.*` beside the DUT hierarchy.
pub fn top_scopes(names: &[String]) -> Vec<String> {
    let mut scopes = std::collections::BTreeSet::new();
    for name in names {
        if let Some((scope, _leaf)) = name.split_once('.') {
            if !scope.is_empty() {
                scopes.insert(scope.to_string());
            }
        }
    }
    scopes.into_iter().collect()
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
    fn test_top_scopes_preserves_multiple_roots() {
        let names = vec![
            "$rootio.clk".into(),
            "uart16550.rx_state".into(),
            "uart16550.rx_data".into(),
        ];
        assert_eq!(top_scopes(&names), vec!["$rootio", "uart16550"]);
    }

    #[test]
    fn test_match_signal() {
        let pats = compile_patterns(&["*data*".into()], "").unwrap();
        assert!(match_signal("tb.dut.data[7:0]", &pats));
        assert!(!match_signal("tb.dut.clk", &pats));

        let pats = compile_patterns(&["*".into()], "").unwrap();
        assert!(match_signal("anything", &pats));
    }

    #[test]
    fn test_auto_wrap_bare_pattern() {
        // Bare name (no glob chars) → suffix match (ADR 0013)
        let pats = compile_patterns(&["input_data".into()], "").unwrap();
        assert!(match_signal("tb.dut.input_data", &pats));
        assert!(match_signal("tb.dut.input_data[7:0]", &pats));
        assert!(match_signal("input_data", &pats));
        assert!(!match_signal("tb.dut.output_data", &pats));

        // Explicit glob should not be auto-wrapped
        let pats = compile_patterns(&["input_data*".into()], "").unwrap();
        assert!(!match_signal("tb.dut.input_data", &pats)); // no leading *
        assert!(match_signal("input_data_bus", &pats));

        // Hierarchical bare name
        let pats = compile_patterns(&["dut.clk".into()], "").unwrap();
        assert!(match_signal("tb.dut.clk", &pats));
    }

    // ---- Suffix-match semantics (ADR 0013) -------------------------------

    #[test]
    fn bare_name_suffix_matches() {
        // Bare name suffix-matches the full hierarchical name.
        let pats = compile_patterns(&["dmem_addr".into()], "").unwrap();
        assert!(match_signal("tb.dut.dmem_addr", &pats));
        // Must NOT cross a token boundary — `dmem_addr_next` doesn't end with `dmem_addr`.
        assert!(!match_signal("tb.dut.dmem_addr_next", &pats));
    }

    #[test]
    fn bare_name_no_substring_match() {
        // `dmem` is not a suffix of `dmem_addr` (which ends in `_addr`).
        let pats = compile_patterns(&["dmem".into()], "").unwrap();
        assert!(!match_signal("tb.dut.dmem_addr", &pats));
        assert!(match_signal("tb.dut.dmem", &pats));
    }

    #[test]
    fn wildcard_substring_still_works() {
        // Explicit `*X*` retains substring semantics.
        let pats = compile_patterns(&["*dmem*".into()], "").unwrap();
        assert!(match_signal("tb.dut.dmem_addr", &pats));
        assert!(match_signal("tb.dut.dmem_addr_next", &pats));
        assert!(match_signal("tb.dut.dmem", &pats));
    }

    #[test]
    fn wildcard_prefix_anchor() {
        // `*dmem_addr` matches `dmem_addr` but NOT `dmem_addr_next`.
        let pats = compile_patterns(&["*dmem_addr".into()], "").unwrap();
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
        let result = compile_patterns(&["[bad".into()], "");
        assert!(result.is_err());
        let msg = result.unwrap_err();
        assert!(
            msg.contains("[bad"),
            "error should name the offending pattern: {}",
            msg
        );
    }

    #[test]
    fn glob_matches_path_relative_to_scope() {
        let scope = "TOP.tb.";
        let pats = compile_patterns(&["dut.*".into()], scope).unwrap();
        assert!(match_signal("TOP.tb.dut.count[7:0]", &pats));
        assert!(match_signal("TOP.tb.dut.u_core.state[3:0]", &pats));
        // Anchored at the scope: not a suffix match, not a sibling, not outside.
        assert!(!match_signal("TOP.tb.other.dut.flag", &pats));
        assert!(!match_signal("TOP.tb.dut_extra.x", &pats));
        assert!(!match_signal("OTHER.dut.e", &pats));

        // Full-path and `*`-led globs are unchanged.
        let full = compile_patterns(&["TOP.tb.dut.*".into()], scope).unwrap();
        assert!(match_signal("TOP.tb.dut.count[7:0]", &full));
        assert!(!match_signal("TOP.tb.other.dut.flag", &full));
        let led = compile_patterns(&["*dut.*".into()], scope).unwrap();
        assert!(match_signal("TOP.tb.other.dut.flag", &led));

        // Bare names stay suffix matches: `bus` still finds `data_bus`.
        let bare = compile_patterns(&["bus".into()], scope).unwrap();
        assert!(match_signal("TOP.tb.data_bus[7:0]", &bare));
        assert!(!match_signal("TOP.tb.bus_en", &bare));

        // No scope: nothing relative to match.
        let none = compile_patterns(&["dut.*".into()], "").unwrap();
        assert!(!match_signal("TOP.tb.dut.count", &none));
    }

    #[test]
    fn anchored_form_names_the_shallowest_matching_boundary() {
        let names = [
            "TOP.tb.clk",
            "TOP.tb.dut.u_core.state[3:0]",
            "TOP.tb.gen[0].leaf.x",
        ];
        let form = |pattern: &str| anchored_form(pattern, names.iter().copied());
        assert_eq!(form("u_core.*").as_deref(), Some("TOP.tb.dut.u_core.*"));
        assert_eq!(form("tb.*").as_deref(), Some("TOP.tb.*"));
        // Metacharacters in the literal prefix are escaped and the result
        // compiles and matches the signal it was derived from.
        let escaped = form("leaf.*").unwrap();
        assert_eq!(escaped, "TOP.tb.gen[[]0].leaf.*");
        let pats = compile_patterns(&[escaped], "").unwrap();
        assert!(match_signal("TOP.tb.gen[0].leaf.x", &pats));
        // Nothing to suggest.
        assert_eq!(form("missing.*"), None);
        assert_eq!(form("[bad"), None);
    }

    #[test]
    fn escape_glob_literal_neutralizes_every_metacharacter() {
        assert_eq!(
            escape_glob_literal("a*b?c{d}e\\f[g]."),
            "a[*]b[?]c[{]d}e[\\]f[[]g]."
        );
    }
}
