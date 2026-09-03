//! Virtual signal definitions: boolean predicates over existing signals.
//! Syntax: a Verilog subset documented in the bundled B-Wave reference.

use crate::format::{hex_slice, hex_to_u128, parse_verilog_literal, slice_bits, values_match};
use crate::signal::{compile_patterns, match_signal};

// -- AST (parsed from --virtual "name = expr") --------------------------------

/// Bit slice range (msb, lsb). Single bit N → (N, N).
/// Used in *resolved* atoms — after resolution, every slice is a range.
pub type BitSlice = (u32, u32);

/// Parsed slice spec — keeps `[N]` and `[N:M]` distinct so the resolver can
/// try a literal-name match for single-index form before falling back to
/// bit slicing (issue 4a).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SliceSpec {
    Single(u32),     // `[N]` — resolver tries literal name `sig[N]` first
    Range(u32, u32), // `[M:N]` — always semantic slice (packed bus)
}

impl SliceSpec {
    pub fn to_bit_slice(self) -> BitSlice {
        match self {
            SliceSpec::Single(n) => (n, n),
            SliceSpec::Range(m, l) => (m, l),
        }
    }
}

/// Right-hand side of a comparison: literal value or signal reference.
#[derive(Debug, Clone)]
pub enum Rhs {
    Value(String),                     // hex literal (from Verilog literal parse)
    Signal(String, Option<SliceSpec>), // signal pattern + optional slice
}

#[derive(Debug, Clone)]
pub enum Atom {
    NonZero(String, Option<SliceSpec>),
    Equal(String, Option<SliceSpec>, Rhs),
    NotEqual(String, Option<SliceSpec>, Rhs),
    Gt(String, Option<SliceSpec>, Rhs),
    Gte(String, Option<SliceSpec>, Rhs),
    Lt(String, Option<SliceSpec>, Rhs),
    Lte(String, Option<SliceSpec>, Rhs),
}

impl Atom {
    fn signal_pattern(&self) -> &str {
        match self {
            Atom::NonZero(s, _)
            | Atom::Equal(s, _, _)
            | Atom::NotEqual(s, _, _)
            | Atom::Gt(s, _, _)
            | Atom::Gte(s, _, _)
            | Atom::Lt(s, _, _)
            | Atom::Lte(s, _, _) => s.as_str(),
        }
    }

    fn lhs_slice(&self) -> Option<SliceSpec> {
        match self {
            Atom::NonZero(_, sl)
            | Atom::Equal(_, sl, _)
            | Atom::NotEqual(_, sl, _)
            | Atom::Gt(_, sl, _)
            | Atom::Gte(_, sl, _)
            | Atom::Lt(_, sl, _)
            | Atom::Lte(_, sl, _) => *sl,
        }
    }
}

/// Tree expression: atoms combined with operators, supporting nesting via parens.
#[derive(Debug, Clone)]
pub enum Expr {
    Atom(Atom),
    Not(Box<Expr>),
    Combine(CombineOp, Vec<Expr>),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CombineOp {
    And,
    Or,
    Xor,
}

#[derive(Debug, Clone)]
pub struct VirtualSignalDef {
    pub name: String,
    pub expr: Expr,
}

// -- Resolved (signal names → cache indices) ----------------------------------

#[derive(Debug)]
pub enum ResolvedRhs {
    Value(String),
    /// (signal index, signal width, optional slice). Width is carried so the
    /// evaluator can call `hex_slice` without re-looking-up the signal table.
    Signal(usize, u32, Option<BitSlice>),
}

#[derive(Debug)]
pub enum ResolvedAtom {
    /// (signal index, signal width, optional slice).
    NonZero(usize, u32, Option<BitSlice>),
    Equal(usize, u32, Option<BitSlice>, ResolvedRhs),
    NotEqual(usize, u32, Option<BitSlice>, ResolvedRhs),
    Gt(usize, u32, Option<BitSlice>, ResolvedRhs),
    Gte(usize, u32, Option<BitSlice>, ResolvedRhs),
    Lt(usize, u32, Option<BitSlice>, ResolvedRhs),
    Lte(usize, u32, Option<BitSlice>, ResolvedRhs),
    VirtualRef(usize),
}

#[derive(Debug)]
pub enum ResolvedExpr {
    Atom(ResolvedAtom),
    Not(Box<ResolvedExpr>),
    Combine(CombineOp, Vec<ResolvedExpr>),
}

#[derive(Debug)]
pub struct ResolvedVirtual {
    pub name: String,
    pub expr: ResolvedExpr,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VirtualDefinitionError {
    pub definition: String,
    pub message: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct VirtualValue {
    pub name: String,
    pub value: String,
}

fn collect_rhs_signal_indices(rhs: &ResolvedRhs, out: &mut std::collections::BTreeSet<usize>) {
    if let ResolvedRhs::Signal(idx, _, _) = rhs {
        out.insert(*idx);
    }
}

fn collect_expr_signal_indices(expr: &ResolvedExpr, out: &mut std::collections::BTreeSet<usize>) {
    match expr {
        ResolvedExpr::Atom(atom) => match atom {
            ResolvedAtom::NonZero(idx, _, _) => {
                out.insert(*idx);
            }
            ResolvedAtom::Equal(idx, _, _, rhs)
            | ResolvedAtom::NotEqual(idx, _, _, rhs)
            | ResolvedAtom::Gt(idx, _, _, rhs)
            | ResolvedAtom::Gte(idx, _, _, rhs)
            | ResolvedAtom::Lt(idx, _, _, rhs)
            | ResolvedAtom::Lte(idx, _, _, rhs) => {
                out.insert(*idx);
                collect_rhs_signal_indices(rhs, out);
            }
            ResolvedAtom::VirtualRef(_) => {}
        },
        ResolvedExpr::Not(inner) => collect_expr_signal_indices(inner, out),
        ResolvedExpr::Combine(_, children) => {
            for child in children {
                collect_expr_signal_indices(child, out);
            }
        }
    }
}

impl ResolvedVirtual {
    /// Store-signal indices referenced directly by this definition.
    pub fn referenced_signal_indices(&self) -> Vec<usize> {
        let mut indices = std::collections::BTreeSet::new();
        collect_expr_signal_indices(&self.expr, &mut indices);
        indices.into_iter().collect()
    }
}

// -- Tokenizer ----------------------------------------------------------------

#[derive(Debug, Clone, PartialEq)]
enum Token {
    Signal(String, Option<SliceSpec>),
    Literal(String),
    CmpEq,
    CmpNeq,
    CmpGt,
    CmpGte,
    CmpLt,
    CmpLte,
    OpAnd,
    OpOr,
    OpXor,
    Not,
    LParen,
    RParen,
}

fn is_signal_char(b: u8) -> bool {
    b.is_ascii_alphanumeric() || b == b'_' || b == b'.' || b == b'*'
}

/// Parse a `[M:N]` or `[N]` bit-slice suffix from `input` at position `pos`.
/// `pos` must point at `[`. Advances `pos` past `]`.
fn tokenize_slice(input: &[u8], pos: &mut usize) -> Result<SliceSpec, String> {
    *pos += 1; // skip '['
    let start = *pos;
    while *pos < input.len() && input[*pos] != b']' {
        *pos += 1;
    }
    if *pos >= input.len() {
        return Err("unclosed '[' in bit slice".into());
    }
    let slice_str =
        std::str::from_utf8(&input[start..*pos]).map_err(|_| "invalid UTF-8 in bit slice")?;
    *pos += 1; // skip ']'
    parse_slice_range(slice_str)
}

/// Parse a SLICE range: "7" → Single(7), "7:4" → Range(7, 4).
fn parse_slice_range(token: &str) -> Result<SliceSpec, String> {
    if let Some(colon) = token.find(':') {
        let msb: u32 = token[..colon]
            .trim()
            .parse()
            .map_err(|_| format!("invalid slice MSB: '{}'", &token[..colon]))?;
        let lsb: u32 = token[colon + 1..]
            .trim()
            .parse()
            .map_err(|_| format!("invalid slice LSB: '{}'", &token[colon + 1..]))?;
        if msb < lsb {
            return Err(format!("slice MSB ({}) must be >= LSB ({})", msb, lsb));
        }
        Ok(SliceSpec::Range(msb, lsb))
    } else {
        let bit: u32 = token
            .trim()
            .parse()
            .map_err(|_| format!("invalid slice bit: '{}'", token))?;
        Ok(SliceSpec::Single(bit))
    }
}

fn tokenize(input: &str) -> Result<Vec<Token>, String> {
    let bytes = input.as_bytes();
    let mut tokens = Vec::new();
    let mut pos = 0;

    while pos < bytes.len() {
        match bytes[pos] {
            b' ' | b'\t' => {
                pos += 1;
            }
            b'(' => {
                tokens.push(Token::LParen);
                pos += 1;
            }
            b')' => {
                tokens.push(Token::RParen);
                pos += 1;
            }
            b'~' => {
                tokens.push(Token::Not);
                pos += 1;
            }
            b'!' => {
                if pos + 1 < bytes.len() && bytes[pos + 1] == b'=' {
                    tokens.push(Token::CmpNeq);
                    pos += 2;
                } else {
                    tokens.push(Token::Not);
                    pos += 1;
                }
            }
            b'&' => {
                if pos + 1 < bytes.len() && bytes[pos + 1] == b'&' {
                    tokens.push(Token::OpAnd);
                    pos += 2;
                } else {
                    tokens.push(Token::OpAnd);
                    pos += 1;
                }
            }
            b'|' => {
                if pos + 1 < bytes.len() && bytes[pos + 1] == b'|' {
                    tokens.push(Token::OpOr);
                    pos += 2;
                } else {
                    tokens.push(Token::OpOr);
                    pos += 1;
                }
            }
            b'^' => {
                tokens.push(Token::OpXor);
                pos += 1;
            }
            b'=' => {
                if pos + 1 < bytes.len() && bytes[pos + 1] == b'=' {
                    tokens.push(Token::CmpEq);
                    pos += 2;
                } else {
                    return Err(format!(
                        "unexpected '=' at position {} (use '==' for comparison)",
                        pos
                    ));
                }
            }
            b'>' => {
                if pos + 1 < bytes.len() && bytes[pos + 1] == b'=' {
                    tokens.push(Token::CmpGte);
                    pos += 2;
                } else {
                    tokens.push(Token::CmpGt);
                    pos += 1;
                }
            }
            b'<' => {
                if pos + 1 < bytes.len() && bytes[pos + 1] == b'=' {
                    tokens.push(Token::CmpLte);
                    pos += 2;
                } else {
                    tokens.push(Token::CmpLt);
                    pos += 1;
                }
            }
            b'\'' => {
                // Verilog literal starting with ' (no width prefix)
                let start = pos;
                pos += 1; // skip '
                if pos >= bytes.len() {
                    return Err("unexpected end after '".into());
                }
                // radix char
                pos += 1;
                // digits
                while pos < bytes.len() && is_signal_char(bytes[pos]) {
                    pos += 1;
                }
                let lit_str = std::str::from_utf8(&bytes[start..pos])
                    .map_err(|_| "invalid UTF-8 in literal")?;
                let hex = parse_verilog_literal(lit_str)?;
                tokens.push(Token::Literal(hex));
            }
            b if b.is_ascii_digit() => {
                // Could be a width-prefixed literal (8'd255) or signal name starting with digit
                let start = pos;
                while pos < bytes.len() && bytes[pos].is_ascii_digit() {
                    pos += 1;
                }
                if pos < bytes.len() && bytes[pos] == b'\'' {
                    // Width-prefixed Verilog literal
                    pos += 1; // skip '
                    if pos >= bytes.len() {
                        return Err("unexpected end after width'".into());
                    }
                    pos += 1; // radix char
                    while pos < bytes.len() && is_signal_char(bytes[pos]) {
                        pos += 1;
                    }
                    let lit_str = std::str::from_utf8(&bytes[start..pos])
                        .map_err(|_| "invalid UTF-8 in literal")?;
                    let hex = parse_verilog_literal(lit_str)?;
                    tokens.push(Token::Literal(hex));
                } else {
                    // Signal name starting with digits (uncommon but possible)
                    while pos < bytes.len() && is_signal_char(bytes[pos]) {
                        pos += 1;
                    }
                    let name = std::str::from_utf8(&bytes[start..pos])
                        .map_err(|_| "invalid UTF-8 in signal")?
                        .to_string();
                    let slice = if pos < bytes.len() && bytes[pos] == b'[' {
                        Some(tokenize_slice(bytes, &mut pos)?)
                    } else {
                        None
                    };
                    tokens.push(Token::Signal(name, slice));
                }
            }
            b if is_signal_char(b) => {
                let start = pos;
                while pos < bytes.len() && is_signal_char(bytes[pos]) {
                    pos += 1;
                }
                let name = std::str::from_utf8(&bytes[start..pos])
                    .map_err(|_| "invalid UTF-8 in signal")?
                    .to_string();
                let slice = if pos < bytes.len() && bytes[pos] == b'[' {
                    Some(tokenize_slice(bytes, &mut pos)?)
                } else {
                    None
                };
                tokens.push(Token::Signal(name, slice));
            }
            other => {
                return Err(format!(
                    "unexpected character '{}' at position {}",
                    other as char, pos
                ));
            }
        }
    }

    Ok(tokens)
}

// -- Parser (recursive descent) -----------------------------------------------

struct Parser {
    tokens: Vec<Token>,
    pos: usize,
}

impl Parser {
    fn new(tokens: Vec<Token>) -> Self {
        Self { tokens, pos: 0 }
    }

    fn peek(&self) -> Option<&Token> {
        self.tokens.get(self.pos)
    }

    fn advance(&mut self) -> Option<Token> {
        if self.pos < self.tokens.len() {
            let tok = self.tokens[self.pos].clone();
            self.pos += 1;
            Some(tok)
        } else {
            None
        }
    }

    fn at_end(&self) -> bool {
        self.pos >= self.tokens.len()
    }

    /// expr := term (combine_op term)*
    /// All combine_ops at the same level must be the same type.
    fn parse_expr(&mut self) -> Result<Expr, String> {
        let first = self.parse_term()?;

        // Check for combine operator
        let op = match self.peek() {
            Some(Token::OpAnd) => CombineOp::And,
            Some(Token::OpOr) => CombineOp::Or,
            Some(Token::OpXor) => CombineOp::Xor,
            _ => return Ok(first),
        };
        self.advance();

        let mut children = vec![first];
        children.push(self.parse_term()?);

        // Continue collecting terms with the same operator
        loop {
            match self.peek() {
                Some(Token::OpAnd) | Some(Token::OpOr) | Some(Token::OpXor) => {
                    let next_op = match self.peek().unwrap() {
                        Token::OpAnd => CombineOp::And,
                        Token::OpOr => CombineOp::Or,
                        Token::OpXor => CombineOp::Xor,
                        _ => unreachable!(),
                    };
                    if next_op != op {
                        return Err(
                            "cannot mix operators without parentheses (e.g. use (*a & *b) | *c)"
                                .into(),
                        );
                    }
                    self.advance();
                    children.push(self.parse_term()?);
                }
                _ => break,
            }
        }

        Ok(Expr::Combine(op, children))
    }

    /// term := '~' primary | '!' primary | primary
    fn parse_term(&mut self) -> Result<Expr, String> {
        if matches!(self.peek(), Some(Token::Not)) {
            self.advance();
            let inner = self.parse_primary()?;
            Ok(Expr::Not(Box::new(inner)))
        } else {
            self.parse_primary()
        }
    }

    /// primary := '(' expr ')' [cmp_op rhs] | signal_ref [cmp_op rhs]
    ///
    /// Issue 4b: a comparison after `(...)` is allowed when the inner
    /// expression is a single bare signal atom; otherwise it is rejected
    /// with an error pointing at the parentheses.
    fn parse_primary(&mut self) -> Result<Expr, String> {
        if matches!(self.peek(), Some(Token::LParen)) {
            self.advance(); // skip '('
            let inner = self.parse_expr()?;
            match self.advance() {
                Some(Token::RParen) => {}
                _ => return Err("expected ')'".into()),
            }
            // Comparison after ')': inner must be a bare-signal NonZero atom.
            if self.peek_cmp().is_some() {
                match inner {
                    Expr::Atom(Atom::NonZero(sig, slice)) => {
                        let atom = self.parse_cmp_tail(sig, slice)?;
                        return Ok(Expr::Atom(atom));
                    }
                    _ => {
                        return Err("comparison LHS must be a single signal atom; \
                             parens enclosing a combine expression cannot be compared"
                            .into());
                    }
                }
            }
            Ok(inner)
        } else {
            self.parse_atom().map(Expr::Atom)
        }
    }

    /// Peek the next token as a comparison operator string, without consuming.
    fn peek_cmp(&self) -> Option<&'static str> {
        match self.peek() {
            Some(Token::CmpEq) => Some("=="),
            Some(Token::CmpNeq) => Some("!="),
            Some(Token::CmpGt) => Some(">"),
            Some(Token::CmpGte) => Some(">="),
            Some(Token::CmpLt) => Some("<"),
            Some(Token::CmpLte) => Some("<="),
            _ => None,
        }
    }

    /// atom := signal_ref [cmp_op rhs]
    fn parse_atom(&mut self) -> Result<Atom, String> {
        let (sig, slice) = match self.advance() {
            Some(Token::Signal(name, sl)) => (name, sl),
            Some(other) => return Err(format!("expected signal, got {:?}", other)),
            None => return Err("expected signal, got end of expression".into()),
        };

        if self.peek_cmp().is_some() {
            self.parse_cmp_tail(sig, slice)
        } else {
            // Bare signal (NonZero / truthy)
            Ok(Atom::NonZero(sig, slice))
        }
    }

    /// Parse the `cmp_op rhs` tail given an already-collected LHS (sig, slice).
    /// Caller must have ensured `peek_cmp()` is Some.
    fn parse_cmp_tail(&mut self, sig: String, slice: Option<SliceSpec>) -> Result<Atom, String> {
        let op_str = self
            .peek_cmp()
            .ok_or_else(|| "internal: parse_cmp_tail called without comparison op".to_string())?;
        self.advance();

        // Parse RHS: literal or signal
        let rhs = match self.peek() {
            Some(Token::Literal(_)) => {
                if let Some(Token::Literal(val)) = self.advance() {
                    Rhs::Value(val)
                } else {
                    unreachable!()
                }
            }
            Some(Token::Signal(_, _)) => {
                if let Some(Token::Signal(rhs_name, rhs_sl)) = self.advance() {
                    Rhs::Signal(rhs_name, rhs_sl)
                } else {
                    unreachable!()
                }
            }
            _ => return Err(format!("expected value or signal after '{}'", op_str)),
        };

        let atom = match op_str {
            "==" => Atom::Equal(sig, slice, rhs),
            "!=" => Atom::NotEqual(sig, slice, rhs),
            ">" => Atom::Gt(sig, slice, rhs),
            ">=" => Atom::Gte(sig, slice, rhs),
            "<" => Atom::Lt(sig, slice, rhs),
            "<=" => Atom::Lte(sig, slice, rhs),
            _ => unreachable!(),
        };
        Ok(atom)
    }
}

// -- Public parse entry point -------------------------------------------------

pub fn parse_virtual_def(s: &str) -> Result<VirtualSignalDef, String> {
    let eq_pos = s
        .find('=')
        .ok_or_else(|| format!("virtual signal definition must contain '=': {}", s))?;

    // Make sure it's not == (comparison inside the name portion)
    if eq_pos + 1 < s.len() && s.as_bytes()[eq_pos + 1] == b'=' {
        return Err(format!(
            "virtual signal definition must contain '=' separator: {}",
            s
        ));
    }

    let name = s[..eq_pos].trim().to_string();
    if name.is_empty() {
        return Err("virtual signal name cannot be empty".into());
    }
    let expr_str = s[eq_pos + 1..].trim();
    if expr_str.is_empty() {
        return Err("virtual signal expression cannot be empty".into());
    }

    let tokens = tokenize(expr_str)?;
    if tokens.is_empty() {
        return Err("virtual signal expression cannot be empty".into());
    }

    let mut parser = Parser::new(tokens);
    let expr = parser.parse_expr()?;

    if !parser.at_end() {
        return Err(format!(
            "unexpected token after expression: {:?}",
            parser.peek()
        ));
    }

    Ok(VirtualSignalDef { name, expr })
}

// -- Resolution ---------------------------------------------------------------

fn resolve_signal_pattern(
    virt_name: &str,
    pattern: &str,
    signal_names: &[String],
) -> Result<usize, String> {
    let matchers = compile_patterns(&[pattern.to_string()])
        .map_err(|e| format!("virtual '{}': bad pattern '{}': {}", virt_name, pattern, e))?;
    let matches: Vec<usize> = signal_names
        .iter()
        .enumerate()
        .filter(|(_, n)| match_signal(n, &matchers))
        .map(|(i, _)| i)
        .collect();

    if matches.is_empty() {
        return Err(format!(
            "virtual '{}': no signal matches '{}'",
            virt_name, pattern
        ));
    }
    if matches.len() > 1 {
        let names: Vec<&str> = matches
            .iter()
            .take(5)
            .map(|&i| signal_names[i].as_str())
            .collect();
        return Err(format!(
            "virtual '{}': pattern '{}' matches {} signals (must be exactly 1): {:?}{}",
            virt_name,
            pattern,
            matches.len(),
            names,
            if matches.len() > 5 { " ..." } else { "" }
        ));
    }
    Ok(matches[0])
}

/// Issue 4a: when a single-index `[N]` slice is used, search for a literal
/// signal name `<pattern>[N]` first. This handles unpacked-array-style VCDs
/// where each bit gets a separate scalar signal (`dmem_wr[0]`, `dmem_wr[1]`).
///
/// Returns:
///   - `Ok(Some(idx))` — exactly one literal match, treat as 1-bit signal.
///   - `Ok(None)` — zero matches, caller should fall back to slice interpretation.
///   - `Err(_)` — multiple literal matches, hard error (never silently switch).
fn try_literal_indexed_match(
    virt_name: &str,
    pattern: &str,
    single_idx: u32,
    signal_names: &[String],
) -> Result<Option<usize>, String> {
    let bracket = format!("[{}]", single_idx);
    // Compile the pattern *with* its suffix-match/glob rules, then test against
    // each signal whose name literally ends in `[N]`. We strip the bracket
    // suffix from the candidate before matching so the pattern need not include
    // brackets (which would be eaten by `strip_bit_select`).
    let matchers = compile_patterns(&[pattern.to_string()])
        .map_err(|e| format!("virtual '{}': bad pattern '{}': {}", virt_name, pattern, e))?;

    let hits: Vec<usize> = signal_names
        .iter()
        .enumerate()
        .filter_map(|(i, n)| {
            let prefix = n.strip_suffix(&bracket)?;
            for (orig, stripped) in &matchers {
                if orig.is_match(prefix) || stripped.is_match(prefix) {
                    return Some(i);
                }
            }
            None
        })
        .collect();

    match hits.len() {
        0 => Ok(None),
        1 => Ok(Some(hits[0])),
        _ => {
            let names: Vec<&str> = hits
                .iter()
                .take(5)
                .map(|&i| signal_names[i].as_str())
                .collect();
            Err(format!(
                "virtual '{}': pattern '{}[{}]' matches {} literal signals (must be exactly 1): {:?}{}",
                virt_name, pattern, single_idx, hits.len(), names,
                if hits.len() > 5 { " ..." } else { "" }
            ))
        }
    }
}

fn effective_width(slice: Option<BitSlice>, full_width: u32) -> u32 {
    match slice {
        Some((msb, lsb)) => msb - lsb + 1,
        None => full_width,
    }
}

fn validate_slice(
    virt_name: &str,
    sig_name: &str,
    slice: Option<BitSlice>,
    sig_width: u32,
) -> Result<(), String> {
    if let Some((msb, _)) = slice {
        if msb >= sig_width {
            return Err(format!(
                "virtual '{}': slice bit {} out of range for signal '{}' (width {})",
                virt_name, msb, sig_name, sig_width
            ));
        }
    }
    Ok(())
}

/// Resolve a `SliceSpec` to a concrete `(idx, width, Option<BitSlice>)`:
///   - Range form → semantic slice on `pattern`.
///   - Single form → try literal-name match first; fall back to slice.
///   - No slice → whole signal.
fn resolve_signal_with_slice(
    virt_name: &str,
    pattern: &str,
    slice: Option<SliceSpec>,
    signal_names: &[String],
    signal_widths: &[u32],
) -> Result<(usize, u32, Option<BitSlice>), String> {
    match slice {
        None => {
            let idx = resolve_signal_pattern(virt_name, pattern, signal_names)?;
            Ok((idx, signal_widths[idx], None))
        }
        Some(SliceSpec::Range(msb, lsb)) => {
            let idx = resolve_signal_pattern(virt_name, pattern, signal_names)?;
            let w = signal_widths[idx];
            validate_slice(virt_name, &signal_names[idx], Some((msb, lsb)), w)?;
            Ok((idx, w, Some((msb, lsb))))
        }
        Some(SliceSpec::Single(n)) => {
            // Issue 4a: try literal name `<pattern>[N]` first.
            if let Some(idx) = try_literal_indexed_match(virt_name, pattern, n, signal_names)? {
                return Ok((idx, signal_widths[idx], None));
            }
            // No literal match → treat as bit slice.
            let idx = resolve_signal_pattern(virt_name, pattern, signal_names)?;
            let w = signal_widths[idx];
            validate_slice(virt_name, &signal_names[idx], Some((n, n)), w)?;
            Ok((idx, w, Some((n, n))))
        }
    }
}

/// Resolve an expression tree: signal patterns → cache indices.
fn resolve_expr(
    virt_name: &str,
    expr: &Expr,
    signal_names: &[String],
    signal_widths: &[u32],
    prior_virtuals: &[String],
) -> Result<ResolvedExpr, String> {
    match expr {
        Expr::Atom(atom) => {
            let ra = resolve_atom(virt_name, atom, signal_names, signal_widths, prior_virtuals)?;
            Ok(ResolvedExpr::Atom(ra))
        }
        Expr::Not(inner) => {
            let resolved = resolve_expr(
                virt_name,
                inner,
                signal_names,
                signal_widths,
                prior_virtuals,
            )?;
            Ok(ResolvedExpr::Not(Box::new(resolved)))
        }
        Expr::Combine(op, children) => {
            let resolved: Result<Vec<_>, _> = children
                .iter()
                .map(|c| resolve_expr(virt_name, c, signal_names, signal_widths, prior_virtuals))
                .collect();
            Ok(ResolvedExpr::Combine(*op, resolved?))
        }
    }
}

fn resolve_atom(
    virt_name: &str,
    atom: &Atom,
    signal_names: &[String],
    signal_widths: &[u32],
    prior_virtuals: &[String],
) -> Result<ResolvedAtom, String> {
    let sig_pattern = atom.signal_pattern();
    let lhs_slice = atom.lhs_slice();

    // Check prior virtuals first (composition)
    if let Some(vi) = prior_virtuals.iter().position(|n| n == sig_pattern) {
        if lhs_slice.is_some() {
            return Err(format!(
                "virtual '{}': bit slice not supported on virtual signal references",
                sig_pattern
            ));
        }
        return match atom {
            Atom::NonZero(_, _) => Ok(ResolvedAtom::VirtualRef(vi)),
            _ => Err(format!(
                "virtual '{}': comparisons not supported on virtual signal references",
                sig_pattern
            )),
        };
    }

    // Resolve LHS signal — may rewrite single-index slice to literal-name match.
    let (lhs_idx, lhs_width, lhs_resolved_slice) = resolve_signal_with_slice(
        virt_name,
        sig_pattern,
        lhs_slice,
        signal_names,
        signal_widths,
    )?;

    let resolve_rhs = |rhs: &Rhs| -> Result<ResolvedRhs, String> {
        match rhs {
            Rhs::Value(v) => Ok(ResolvedRhs::Value(v.clone())),
            Rhs::Signal(pat, rhs_slice) => {
                let (rhs_idx, rhs_width, rhs_resolved_slice) = resolve_signal_with_slice(
                    virt_name,
                    pat,
                    *rhs_slice,
                    signal_names,
                    signal_widths,
                )?;
                let lhs_eff = effective_width(lhs_resolved_slice, lhs_width);
                let rhs_eff = effective_width(rhs_resolved_slice, rhs_width);
                if lhs_eff != rhs_eff {
                    return Err(format!(
                        "virtual '{}': width mismatch: LHS '{}' is {}-bit, RHS '{}' is {}-bit",
                        virt_name, signal_names[lhs_idx], lhs_eff, signal_names[rhs_idx], rhs_eff
                    ));
                }
                Ok(ResolvedRhs::Signal(rhs_idx, rhs_width, rhs_resolved_slice))
            }
        }
    };

    match atom {
        Atom::NonZero(_, _) => Ok(ResolvedAtom::NonZero(
            lhs_idx,
            lhs_width,
            lhs_resolved_slice,
        )),
        Atom::Equal(_, _, rhs) => Ok(ResolvedAtom::Equal(
            lhs_idx,
            lhs_width,
            lhs_resolved_slice,
            resolve_rhs(rhs)?,
        )),
        Atom::NotEqual(_, _, rhs) => Ok(ResolvedAtom::NotEqual(
            lhs_idx,
            lhs_width,
            lhs_resolved_slice,
            resolve_rhs(rhs)?,
        )),
        Atom::Gt(_, _, rhs) => Ok(ResolvedAtom::Gt(
            lhs_idx,
            lhs_width,
            lhs_resolved_slice,
            resolve_rhs(rhs)?,
        )),
        Atom::Gte(_, _, rhs) => Ok(ResolvedAtom::Gte(
            lhs_idx,
            lhs_width,
            lhs_resolved_slice,
            resolve_rhs(rhs)?,
        )),
        Atom::Lt(_, _, rhs) => Ok(ResolvedAtom::Lt(
            lhs_idx,
            lhs_width,
            lhs_resolved_slice,
            resolve_rhs(rhs)?,
        )),
        Atom::Lte(_, _, rhs) => Ok(ResolvedAtom::Lte(
            lhs_idx,
            lhs_width,
            lhs_resolved_slice,
            resolve_rhs(rhs)?,
        )),
    }
}

pub fn resolve_virtual(
    def: &VirtualSignalDef,
    signal_names: &[String],
    signal_widths: &[u32],
    prior_virtuals: &[String],
) -> Result<ResolvedVirtual, String> {
    let resolved = resolve_expr(
        &def.name,
        &def.expr,
        signal_names,
        signal_widths,
        prior_virtuals,
    )?;
    Ok(ResolvedVirtual {
        name: def.name.clone(),
        expr: resolved,
    })
}

/// Parse and resolve an ordered set of virtual definitions.
///
/// Resolution happens for the complete set before callers evaluate or render
/// anything, so a bad later definition cannot leave plausible partial output.
pub fn resolve_virtual_definitions(
    definitions: &[String],
    signal_names: &[String],
    signal_widths: &[u32],
) -> Result<Vec<ResolvedVirtual>, VirtualDefinitionError> {
    let mut prior_names = Vec::new();
    let mut resolved = Vec::with_capacity(definitions.len());

    for definition in definitions {
        let parsed = parse_virtual_def(definition).map_err(|message| VirtualDefinitionError {
            definition: definition.clone(),
            message,
        })?;
        let entry = resolve_virtual(&parsed, signal_names, signal_widths, &prior_names).map_err(
            |message| VirtualDefinitionError {
                definition: definition.clone(),
                message,
            },
        )?;
        prior_names.push(entry.name.clone());
        resolved.push(entry);
    }

    Ok(resolved)
}

// -- Evaluation ---------------------------------------------------------------

fn get_numeric_val(hex: &str, slice: Option<BitSlice>) -> Option<u128> {
    let full = hex_to_u128(hex)?;
    match slice {
        Some((msb, lsb)) => Some(slice_bits(full, msb, lsb)),
        None => Some(full),
    }
}

/// Produce the comparison string for an LHS or RHS signal, optionally sliced.
/// For sliced inputs we route through `hex_slice` which preserves X/Z within
/// the slice range while stripping X/Z bits that sit outside (issue 4c).
fn signal_compare_str(value: String, width: u32, slice: Option<BitSlice>) -> String {
    match slice {
        Some((msb, lsb)) => hex_slice(&value, width, msb, lsb),
        None => value,
    }
}

fn get_rhs_val(rhs: &ResolvedRhs, get_val: &dyn Fn(usize) -> String) -> Option<u128> {
    match rhs {
        ResolvedRhs::Value(v) => hex_to_u128(v),
        ResolvedRhs::Signal(idx, _w, slice) => {
            let hex = get_val(*idx);
            get_numeric_val(&hex, *slice)
        }
    }
}

fn eval_atom(
    atom: &ResolvedAtom,
    get_val: &dyn Fn(usize) -> String,
    get_virtual: &dyn Fn(usize) -> bool,
) -> bool {
    match atom {
        ResolvedAtom::NonZero(idx, _w, slice) => {
            let v = get_val(*idx);
            match slice {
                Some((msb, lsb)) => {
                    get_numeric_val(&v, Some((*msb, *lsb))).map_or(false, |n| n != 0)
                }
                None => v.trim_start_matches('0') != "" && v != "0",
            }
        }
        ResolvedAtom::Equal(idx, lhs_w, slice, rhs) => {
            let v = get_val(*idx);
            let lhs_str = signal_compare_str(v, *lhs_w, *slice);
            let rhs_str = match rhs {
                ResolvedRhs::Value(target) => target.clone(),
                ResolvedRhs::Signal(ri, rw, rs) => signal_compare_str(get_val(*ri), *rw, *rs),
            };
            values_match(&lhs_str, &rhs_str)
        }
        ResolvedAtom::NotEqual(idx, lhs_w, slice, rhs) => {
            let v = get_val(*idx);
            let lhs_str = signal_compare_str(v, *lhs_w, *slice);
            let rhs_str = match rhs {
                ResolvedRhs::Value(target) => target.clone(),
                ResolvedRhs::Signal(ri, rw, rs) => signal_compare_str(get_val(*ri), *rw, *rs),
            };
            !values_match(&lhs_str, &rhs_str)
        }
        ResolvedAtom::Gt(idx, _w, slice, rhs) => {
            let lhs = get_numeric_val(&get_val(*idx), *slice);
            let rhs_val = get_rhs_val(rhs, get_val);
            matches!((lhs, rhs_val), (Some(l), Some(r)) if l > r)
        }
        ResolvedAtom::Gte(idx, _w, slice, rhs) => {
            let lhs = get_numeric_val(&get_val(*idx), *slice);
            let rhs_val = get_rhs_val(rhs, get_val);
            matches!((lhs, rhs_val), (Some(l), Some(r)) if l >= r)
        }
        ResolvedAtom::Lt(idx, _w, slice, rhs) => {
            let lhs = get_numeric_val(&get_val(*idx), *slice);
            let rhs_val = get_rhs_val(rhs, get_val);
            matches!((lhs, rhs_val), (Some(l), Some(r)) if l < r)
        }
        ResolvedAtom::Lte(idx, _w, slice, rhs) => {
            let lhs = get_numeric_val(&get_val(*idx), *slice);
            let rhs_val = get_rhs_val(rhs, get_val);
            matches!((lhs, rhs_val), (Some(l), Some(r)) if l <= r)
        }
        ResolvedAtom::VirtualRef(vi) => get_virtual(*vi),
    }
}

fn eval_expr(
    expr: &ResolvedExpr,
    get_val: &dyn Fn(usize) -> String,
    get_virtual: &dyn Fn(usize) -> bool,
) -> bool {
    match expr {
        ResolvedExpr::Atom(a) => eval_atom(a, get_val, get_virtual),
        ResolvedExpr::Not(inner) => !eval_expr(inner, get_val, get_virtual),
        ResolvedExpr::Combine(op, children) => {
            let vals: Vec<bool> = children
                .iter()
                .map(|c| eval_expr(c, get_val, get_virtual))
                .collect();
            match op {
                CombineOp::And => vals.iter().all(|&v| v),
                CombineOp::Or => vals.iter().any(|&v| v),
                CombineOp::Xor => vals.iter().fold(false, |acc, &v| acc ^ v),
            }
        }
    }
}

pub fn evaluate_virtual(
    rv: &ResolvedVirtual,
    get_val: &dyn Fn(usize) -> String,
    virtual_values: &[bool],
) -> bool {
    let get_virtual = |vi: usize| -> bool { virtual_values[vi] };
    eval_expr(&rv.expr, get_val, &get_virtual)
}

/// Evaluate an ordered resolved set at one timepoint.
pub fn evaluate_virtual_values(
    resolved: &[ResolvedVirtual],
    get_val: &dyn Fn(usize) -> String,
) -> Vec<VirtualValue> {
    let mut prior_values = Vec::with_capacity(resolved.len());
    let mut values = Vec::with_capacity(resolved.len());

    for entry in resolved {
        let value = evaluate_virtual(entry, get_val, &prior_values);
        prior_values.push(value);
        values.push(VirtualValue {
            name: entry.name.clone(),
            value: if value { "1" } else { "0" }.to_string(),
        });
    }

    values
}

// -- Synthetic transition list ------------------------------------------------

fn collect_real_ticks(
    transitions: &[(u64, String)],
    tick_min: u64,
    tick_max: u64,
    out: &mut Vec<u64>,
) {
    for (t, _) in transitions {
        if *t >= tick_min && *t <= tick_max {
            out.push(*t);
        }
    }
}

fn collect_rhs_ticks(
    rhs: &ResolvedRhs,
    real_signal_transitions: &[Vec<(u64, String)>],
    tick_min: u64,
    tick_max: u64,
    out: &mut Vec<u64>,
) {
    if let ResolvedRhs::Signal(idx, _, _) = rhs {
        collect_real_ticks(&real_signal_transitions[*idx], tick_min, tick_max, out);
    }
}

fn collect_expr_ticks(
    expr: &ResolvedExpr,
    prior_virtual_transitions: &[Vec<(u64, String)>],
    real_signal_transitions: &[Vec<(u64, String)>],
    tick_min: u64,
    tick_max: u64,
    out: &mut Vec<u64>,
) {
    match expr {
        ResolvedExpr::Atom(atom) => match atom {
            ResolvedAtom::NonZero(idx, _, _) => {
                collect_real_ticks(&real_signal_transitions[*idx], tick_min, tick_max, out);
            }
            ResolvedAtom::Equal(idx, _, _, rhs)
            | ResolvedAtom::NotEqual(idx, _, _, rhs)
            | ResolvedAtom::Gt(idx, _, _, rhs)
            | ResolvedAtom::Gte(idx, _, _, rhs)
            | ResolvedAtom::Lt(idx, _, _, rhs)
            | ResolvedAtom::Lte(idx, _, _, rhs) => {
                collect_real_ticks(&real_signal_transitions[*idx], tick_min, tick_max, out);
                collect_rhs_ticks(rhs, real_signal_transitions, tick_min, tick_max, out);
            }
            ResolvedAtom::VirtualRef(vi) => {
                for (t, _) in &prior_virtual_transitions[*vi] {
                    if *t >= tick_min && *t <= tick_max {
                        out.push(*t);
                    }
                }
            }
        },
        ResolvedExpr::Not(inner) => {
            collect_expr_ticks(
                inner,
                prior_virtual_transitions,
                real_signal_transitions,
                tick_min,
                tick_max,
                out,
            );
        }
        ResolvedExpr::Combine(_, children) => {
            for child in children {
                collect_expr_ticks(
                    child,
                    prior_virtual_transitions,
                    real_signal_transitions,
                    tick_min,
                    tick_max,
                    out,
                );
            }
        }
    }
}

pub fn build_virtual_transitions(
    rv: &ResolvedVirtual,
    prior_virtual_transitions: &[Vec<(u64, String)>],
    real_signal_transitions: &[Vec<(u64, String)>],
    tick_min: u64,
    tick_max: u64,
) -> Vec<(u64, String)> {
    let mut all_ticks: Vec<u64> = Vec::new();
    collect_expr_ticks(
        &rv.expr,
        prior_virtual_transitions,
        real_signal_transitions,
        tick_min,
        tick_max,
        &mut all_ticks,
    );
    all_ticks.sort_unstable();
    all_ticks.dedup();

    if all_ticks.is_empty() || all_ticks[0] > tick_min {
        all_ticks.insert(0, tick_min);
    }

    let mut result = Vec::new();
    let mut prev_val: Option<bool> = None;

    for &tick in &all_ticks {
        let get_val =
            |idx: usize| -> String { value_at_tick_slice(&real_signal_transitions[idx], tick) };

        let virtual_values: Vec<bool> = prior_virtual_transitions
            .iter()
            .map(|vt| {
                let v = value_at_tick_slice(vt, tick);
                v != "0"
            })
            .collect();

        let val = evaluate_virtual(rv, &get_val, &virtual_values);
        if prev_val != Some(val) {
            result.push((
                tick,
                if val {
                    "1".to_string()
                } else {
                    "0".to_string()
                },
            ));
            prev_val = Some(val);
        }
    }

    result
}

fn value_at_tick_slice(transitions: &[(u64, String)], tick: u64) -> String {
    match transitions.binary_search_by_key(&tick, |(t, _)| *t) {
        Ok(i) => transitions[i].1.clone(),
        Err(0) => "x".to_string(),
        Err(i) => transitions[i - 1].1.clone(),
    }
}

#[cfg(test)]
mod virtual_tests {
    use super::*;

    // ---- Parser tests: Verilog-subset syntax ----

    #[test]
    fn test_parse_simple_nonzero() {
        let def = parse_virtual_def("v = *valid").unwrap();
        assert_eq!(def.name, "v");
        match &def.expr {
            Expr::Atom(Atom::NonZero(s, None)) => assert_eq!(s, "*valid"),
            _ => panic!("expected NonZero atom without slice"),
        }
    }

    #[test]
    fn test_parse_and_expression() {
        let def = parse_virtual_def("hsk = *valid & *ready").unwrap();
        assert_eq!(def.name, "hsk");
        match &def.expr {
            Expr::Combine(CombineOp::And, children) => assert_eq!(children.len(), 2),
            _ => panic!("expected Combine(And, 2)"),
        }
    }

    #[test]
    fn test_parse_logical_and() {
        let def = parse_virtual_def("hsk = *valid && *ready").unwrap();
        match &def.expr {
            Expr::Combine(CombineOp::And, children) => assert_eq!(children.len(), 2),
            _ => panic!("expected Combine(And, 2)"),
        }
    }

    #[test]
    fn test_parse_or_expression() {
        let def = parse_virtual_def("v = *a | *b").unwrap();
        match &def.expr {
            Expr::Combine(CombineOp::Or, children) => assert_eq!(children.len(), 2),
            _ => panic!("expected Combine(Or, 2)"),
        }
    }

    #[test]
    fn test_parse_logical_or() {
        let def = parse_virtual_def("v = *a || *b").unwrap();
        match &def.expr {
            Expr::Combine(CombineOp::Or, children) => assert_eq!(children.len(), 2),
            _ => panic!("expected Combine(Or, 2)"),
        }
    }

    #[test]
    fn test_parse_xor_expression() {
        let def = parse_virtual_def("v = *a ^ *b").unwrap();
        match &def.expr {
            Expr::Combine(CombineOp::Xor, children) => assert_eq!(children.len(), 2),
            _ => panic!("expected Combine(Xor, 2)"),
        }
    }

    #[test]
    fn test_parse_equal_with_verilog_literal() {
        let def = parse_virtual_def("check = sig == 'd255").unwrap();
        match &def.expr {
            Expr::Atom(Atom::Equal(s, None, Rhs::Value(v))) => {
                assert_eq!(s, "sig");
                assert_eq!(v, "FF");
            }
            _ => panic!("expected Equal atom with Value RHS"),
        }
    }

    #[test]
    fn test_parse_not_tilde() {
        let def = parse_virtual_def("idle = ~*busy").unwrap();
        match &def.expr {
            Expr::Not(inner) => match inner.as_ref() {
                Expr::Atom(Atom::NonZero(s, None)) => assert_eq!(s, "*busy"),
                _ => panic!("expected NonZero inside Not"),
            },
            _ => panic!("expected Not expr"),
        }
    }

    #[test]
    fn test_parse_not_bang() {
        let def = parse_virtual_def("idle = !*busy").unwrap();
        match &def.expr {
            Expr::Not(inner) => match inner.as_ref() {
                Expr::Atom(Atom::NonZero(s, None)) => assert_eq!(s, "*busy"),
                _ => panic!("expected NonZero inside Not"),
            },
            _ => panic!("expected Not expr"),
        }
    }

    #[test]
    fn test_mixed_operators_rejected() {
        let res = parse_virtual_def("bad = *a & *b | *c");
        assert!(res.is_err());
    }

    #[test]
    fn test_empty_expression_rejected() {
        assert!(parse_virtual_def("v = ").is_err());
    }

    // ---- Parser tests: comparison operators ----

    #[test]
    fn test_parse_gt() {
        let def = parse_virtual_def("check = *sig > 'd5").unwrap();
        match &def.expr {
            Expr::Atom(Atom::Gt(s, None, Rhs::Value(v))) => {
                assert_eq!(s, "*sig");
                assert_eq!(v, "5");
            }
            _ => panic!("expected Gt atom"),
        }
    }

    #[test]
    fn test_parse_gte() {
        let def = parse_virtual_def("check = *sig >= 'h0").unwrap();
        match &def.expr {
            Expr::Atom(Atom::Gte(s, None, Rhs::Value(v))) => {
                assert_eq!(s, "*sig");
                assert_eq!(v, "0");
            }
            _ => panic!("expected Gte atom"),
        }
    }

    #[test]
    fn test_parse_lt() {
        let def = parse_virtual_def("check = *sig < 'd100").unwrap();
        match &def.expr {
            Expr::Atom(Atom::Lt(s, None, Rhs::Value(v))) => {
                assert_eq!(s, "*sig");
                assert_eq!(v, "64"); // 100 decimal = 0x64
            }
            _ => panic!("expected Lt atom"),
        }
    }

    #[test]
    fn test_parse_lte() {
        let def = parse_virtual_def("check = *sig <= 'hFF").unwrap();
        match &def.expr {
            Expr::Atom(Atom::Lte(s, None, Rhs::Value(v))) => {
                assert_eq!(s, "*sig");
                assert_eq!(v, "FF");
            }
            _ => panic!("expected Lte atom"),
        }
    }

    #[test]
    fn test_parse_neq() {
        let def = parse_virtual_def("v = *sig != 'h0").unwrap();
        match &def.expr {
            Expr::Atom(Atom::NotEqual(s, None, Rhs::Value(v))) => {
                assert_eq!(s, "*sig");
                assert_eq!(v, "0");
            }
            _ => panic!("expected NotEqual atom"),
        }
    }

    // ---- Parser tests: bit slices ----

    #[test]
    fn test_parse_slice_single_bit() {
        let def = parse_virtual_def("msb = *addr[7]").unwrap();
        match &def.expr {
            Expr::Atom(Atom::NonZero(s, Some(SliceSpec::Single(7)))) => assert_eq!(s, "*addr"),
            _ => panic!("expected NonZero with Single(7), got {:?}", def.expr),
        }
    }

    #[test]
    fn test_parse_slice_range() {
        let def = parse_virtual_def("nib = *data[7:4]").unwrap();
        match &def.expr {
            Expr::Atom(Atom::NonZero(s, Some(SliceSpec::Range(7, 4)))) => assert_eq!(s, "*data"),
            _ => panic!("expected NonZero with Range(7,4), got {:?}", def.expr),
        }
    }

    #[test]
    fn test_parse_not_with_slice() {
        let def = parse_virtual_def("bit0 = ~*sig[0]").unwrap();
        match &def.expr {
            Expr::Not(inner) => match inner.as_ref() {
                Expr::Atom(Atom::NonZero(s, Some(SliceSpec::Single(0)))) => assert_eq!(s, "*sig"),
                _ => panic!(
                    "expected NonZero with Single(0) inside Not, got {:?}",
                    inner
                ),
            },
            _ => panic!("expected Not expr"),
        }
    }

    #[test]
    fn test_parse_slice_msb_lt_lsb_rejected() {
        let res = parse_virtual_def("bad = *sig[4:7]");
        assert!(res.is_err());
        assert!(res.unwrap_err().contains("MSB"));
    }

    // ---- Parser tests: slice + comparison ----

    #[test]
    fn test_parse_slice_with_gt() {
        let def = parse_virtual_def("hi = *data[15:8] > 'd0").unwrap();
        match &def.expr {
            Expr::Atom(Atom::Gt(s, Some(SliceSpec::Range(15, 8)), Rhs::Value(v))) => {
                assert_eq!(s, "*data");
                assert_eq!(v, "0");
            }
            _ => panic!("expected Gt with Range(15,8), got {:?}", def.expr),
        }
    }

    #[test]
    fn test_parse_slice_sig_to_sig() {
        let def = parse_virtual_def("eq = *a[3:0] == *b[3:0]").unwrap();
        match &def.expr {
            Expr::Atom(Atom::Equal(
                s,
                Some(SliceSpec::Range(3, 0)),
                Rhs::Signal(rhs_s, Some(SliceSpec::Range(3, 0))),
            )) => {
                assert_eq!(s, "*a");
                assert_eq!(rhs_s, "*b");
            }
            _ => panic!("expected Equal with sliced signal RHS, got {:?}", def.expr),
        }
    }

    // ---- Parser tests: signal-to-signal ----

    #[test]
    fn test_parse_sig_to_sig_equal() {
        let def = parse_virtual_def("match = *exp == *act").unwrap();
        match &def.expr {
            Expr::Atom(Atom::Equal(s, None, Rhs::Signal(rhs, None))) => {
                assert_eq!(s, "*exp");
                assert_eq!(rhs, "*act");
            }
            _ => panic!("expected Equal with Signal RHS, got {:?}", def.expr),
        }
    }

    #[test]
    fn test_parse_sig_to_sig_gt() {
        let def = parse_virtual_def("bigger = *a > *b").unwrap();
        match &def.expr {
            Expr::Atom(Atom::Gt(s, None, Rhs::Signal(rhs, None))) => {
                assert_eq!(s, "*a");
                assert_eq!(rhs, "*b");
            }
            _ => panic!("expected Gt with Signal RHS"),
        }
    }

    #[test]
    fn test_parse_sig_to_sig_sliced_lte() {
        let def = parse_virtual_def("cmp = *a[7:4] <= *b[7:4]").unwrap();
        match &def.expr {
            Expr::Atom(Atom::Lte(
                s,
                Some(SliceSpec::Range(7, 4)),
                Rhs::Signal(rhs, Some(SliceSpec::Range(7, 4))),
            )) => {
                assert_eq!(s, "*a");
                assert_eq!(rhs, "*b");
            }
            _ => panic!("expected Lte with sliced Signal RHS"),
        }
    }

    // ---- Parser tests: Verilog literal variants ----

    #[test]
    fn test_parse_hex_literal() {
        let def = parse_virtual_def("v = *sig == 'hFF").unwrap();
        match &def.expr {
            Expr::Atom(Atom::Equal(_, None, Rhs::Value(v))) => assert_eq!(v, "FF"),
            _ => panic!("expected Value RHS"),
        }
    }

    #[test]
    fn test_parse_decimal_literal() {
        let def = parse_virtual_def("v = *sig == 'd255").unwrap();
        match &def.expr {
            Expr::Atom(Atom::Equal(_, None, Rhs::Value(v))) => assert_eq!(v, "FF"),
            _ => panic!("expected Value RHS"),
        }
    }

    #[test]
    fn test_parse_binary_literal() {
        let def = parse_virtual_def("v = *sig == 'b11111111").unwrap();
        match &def.expr {
            Expr::Atom(Atom::Equal(_, None, Rhs::Value(v))) => assert_eq!(v, "FF"),
            _ => panic!("expected Value RHS"),
        }
    }

    #[test]
    fn test_parse_width_prefixed_literal() {
        let def = parse_virtual_def("v = *sig == 8'd255").unwrap();
        match &def.expr {
            Expr::Atom(Atom::Equal(_, None, Rhs::Value(v))) => assert_eq!(v, "FF"),
            _ => panic!("expected Value RHS"),
        }
    }

    #[test]
    fn test_parse_xz_literal() {
        let def = parse_virtual_def("v = *sig == 'hx").unwrap();
        match &def.expr {
            Expr::Atom(Atom::Equal(_, None, Rhs::Value(v))) => assert_eq!(v, "X"),
            _ => panic!("expected x/z Value RHS"),
        }
    }

    // ---- Parser tests: parentheses ----

    #[test]
    fn test_parse_parens_enable_mixing() {
        let def = parse_virtual_def("v = (*a & *b) | *c").unwrap();
        match &def.expr {
            Expr::Combine(CombineOp::Or, children) => {
                assert_eq!(children.len(), 2);
                match &children[0] {
                    Expr::Combine(CombineOp::And, inner) => assert_eq!(inner.len(), 2),
                    _ => panic!("expected inner And group"),
                }
            }
            _ => panic!("expected outer Or"),
        }
    }

    #[test]
    fn test_parse_nested_parens() {
        let def = parse_virtual_def("v = ((*a & *b) | *c) ^ *d").unwrap();
        match &def.expr {
            Expr::Combine(CombineOp::Xor, children) => {
                assert_eq!(children.len(), 2);
            }
            _ => panic!("expected outer Xor"),
        }
    }

    #[test]
    fn test_parse_not_on_parens_group() {
        let def = parse_virtual_def("v = ~(*a & *b)").unwrap();
        match &def.expr {
            Expr::Not(inner) => match inner.as_ref() {
                Expr::Combine(CombineOp::And, children) => assert_eq!(children.len(), 2),
                _ => panic!("expected And group inside Not"),
            },
            _ => panic!("expected Not expr"),
        }
    }

    // ---- Parser tests: combined with operators ----

    #[test]
    fn test_parse_multi_atom_with_comparison() {
        let def = parse_virtual_def("v = (*a > 'd0) & (*b < 'd10)").unwrap();
        match &def.expr {
            Expr::Combine(CombineOp::And, children) => {
                assert_eq!(children.len(), 2);
                match &children[0] {
                    Expr::Atom(Atom::Gt(s, None, Rhs::Value(v))) => {
                        assert_eq!(s, "*a");
                        assert_eq!(v, "0");
                    }
                    _ => panic!("expected Gt atom"),
                }
                match &children[1] {
                    Expr::Atom(Atom::Lt(s, None, Rhs::Value(v))) => {
                        assert_eq!(s, "*b");
                        assert_eq!(v, "A"); // 10 decimal = 0xA
                    }
                    _ => panic!("expected Lt atom"),
                }
            }
            _ => panic!("expected Combine(And, 2)"),
        }
    }

    #[test]
    fn test_parse_mixed_ops_with_comparison_rejected() {
        let res = parse_virtual_def("v = (*a > 'd0) & (*b < 'd0) | *c");
        assert!(res.is_err());
    }

    // ---- Resolution tests ----

    #[test]
    fn test_resolve_slice_within_width() {
        let def = parse_virtual_def("v = *sig[7:4]").unwrap();
        let names = vec!["tb.dut.sig".to_string()];
        let widths = vec![8u32];
        let res = resolve_virtual(&def, &names, &widths, &[]);
        assert!(res.is_ok());
    }

    #[test]
    fn test_resolve_slice_beyond_width() {
        let def = parse_virtual_def("v = *sig[8]").unwrap();
        let names = vec!["tb.dut.sig".to_string()];
        let widths = vec![8u32];
        let res = resolve_virtual(&def, &names, &widths, &[]);
        assert!(res.is_err());
        assert!(res.unwrap_err().contains("out of range"));
    }

    #[test]
    fn test_resolve_sig_to_sig_same_width() {
        let def = parse_virtual_def("v = *a == *b").unwrap();
        let names = vec!["tb.a".to_string(), "tb.b".to_string()];
        let widths = vec![8u32, 8u32];
        let res = resolve_virtual(&def, &names, &widths, &[]);
        assert!(res.is_ok());
    }

    #[test]
    fn test_resolve_sig_to_sig_width_mismatch() {
        let def = parse_virtual_def("v = *a == *b").unwrap();
        let names = vec!["tb.a".to_string(), "tb.b".to_string()];
        let widths = vec![8u32, 16u32];
        let res = resolve_virtual(&def, &names, &widths, &[]);
        assert!(res.is_err());
        assert!(res.unwrap_err().contains("width mismatch"));
    }

    #[test]
    fn test_resolve_sig_to_sig_sliced_matching() {
        let def = parse_virtual_def("v = *a[3:0] == *b[3:0]").unwrap();
        let names = vec!["tb.a".to_string(), "tb.b".to_string()];
        let widths = vec![8u32, 16u32]; // different widths but same slice width
        let res = resolve_virtual(&def, &names, &widths, &[]);
        assert!(res.is_ok());
    }

    #[test]
    fn test_resolve_slice_on_virtual_ref_rejected() {
        let def = parse_virtual_def("v2 = v1[0]").unwrap();
        let names = vec!["tb.sig".to_string()];
        let widths = vec![8u32];
        let priors = vec!["v1".to_string()];
        let res = resolve_virtual(&def, &names, &widths, &priors);
        assert!(res.is_err());
        assert!(res
            .unwrap_err()
            .contains("bit slice not supported on virtual"));
    }

    // ---- Evaluation tests ----

    fn make_val_fn<'a>(vals: &'a [(usize, &'a str)]) -> impl Fn(usize) -> String + 'a {
        move |idx| -> String {
            vals.iter()
                .find(|(i, _)| *i == idx)
                .map(|(_, v)| v.to_string())
                .unwrap_or_else(|| "0".to_string())
        }
    }

    #[test]
    fn test_eval_gt_true() {
        let atom = ResolvedAtom::Gt(0, 8, None, ResolvedRhs::Value("5".into()));
        let get_val = make_val_fn(&[(0, "6")]);
        assert!(eval_atom(&atom, &get_val, &|_| false));
    }

    #[test]
    fn test_eval_gt_equal_is_false() {
        let atom = ResolvedAtom::Gt(0, 8, None, ResolvedRhs::Value("5".into()));
        let get_val = make_val_fn(&[(0, "5")]);
        assert!(!eval_atom(&atom, &get_val, &|_| false));
    }

    #[test]
    fn test_eval_gte_equal_is_true() {
        let atom = ResolvedAtom::Gte(0, 8, None, ResolvedRhs::Value("5".into()));
        let get_val = make_val_fn(&[(0, "5")]);
        assert!(eval_atom(&atom, &get_val, &|_| false));
    }

    #[test]
    fn test_eval_lt_true() {
        let atom = ResolvedAtom::Lt(0, 8, None, ResolvedRhs::Value("5".into()));
        let get_val = make_val_fn(&[(0, "4")]);
        assert!(eval_atom(&atom, &get_val, &|_| false));
    }

    #[test]
    fn test_eval_lt_equal_is_false() {
        let atom = ResolvedAtom::Lt(0, 8, None, ResolvedRhs::Value("5".into()));
        let get_val = make_val_fn(&[(0, "5")]);
        assert!(!eval_atom(&atom, &get_val, &|_| false));
    }

    #[test]
    fn test_eval_lte_equal_is_true() {
        let atom = ResolvedAtom::Lte(0, 8, None, ResolvedRhs::Value("5".into()));
        let get_val = make_val_fn(&[(0, "5")]);
        assert!(eval_atom(&atom, &get_val, &|_| false));
    }

    // ---- Evaluation tests: slice ----

    #[test]
    fn test_eval_slice_bit7_of_ff() {
        let atom = ResolvedAtom::NonZero(0, 8, Some((7, 7)));
        let get_val = make_val_fn(&[(0, "FF")]);
        assert!(eval_atom(&atom, &get_val, &|_| false));
    }

    #[test]
    fn test_eval_slice_bit7_of_7f() {
        let atom = ResolvedAtom::NonZero(0, 8, Some((7, 7)));
        let get_val = make_val_fn(&[(0, "7F")]);
        assert!(!eval_atom(&atom, &get_val, &|_| false));
    }

    #[test]
    fn test_eval_slice_nibble_of_a3() {
        let atom = ResolvedAtom::Gt(0, 8, Some((7, 4)), ResolvedRhs::Value("9".into()));
        let get_val = make_val_fn(&[(0, "A3")]);
        assert!(eval_atom(&atom, &get_val, &|_| false));
    }

    #[test]
    fn test_eval_not_slice_bit0_of_fe() {
        let expr = ResolvedExpr::Not(Box::new(ResolvedExpr::Atom(ResolvedAtom::NonZero(
            0,
            8,
            Some((0, 0)),
        ))));
        let get_val = make_val_fn(&[(0, "FE")]);
        assert!(eval_expr(&expr, &get_val, &|_| false));
    }

    // ---- Evaluation tests: x/z handling ----

    #[test]
    fn test_eval_xz_comparison_is_false() {
        let atom = ResolvedAtom::Gt(0, 8, None, ResolvedRhs::Value("5".into()));
        let get_val = make_val_fn(&[(0, "x")]);
        assert!(!eval_atom(&atom, &get_val, &|_| false));
    }

    #[test]
    fn test_eval_xz_rhs_is_false() {
        let atom = ResolvedAtom::Gt(0, 8, None, ResolvedRhs::Signal(1, 8, None));
        let get_val = make_val_fn(&[(0, "6"), (1, "xxxx")]);
        assert!(!eval_atom(&atom, &get_val, &|_| false));
    }

    #[test]
    fn test_eval_slice_of_xz_is_false() {
        let atom = ResolvedAtom::NonZero(0, 8, Some((7, 7)));
        let get_val = make_val_fn(&[(0, "xx")]);
        assert!(!eval_atom(&atom, &get_val, &|_| false));
    }

    // ---- Evaluation tests: signal-to-signal ----

    #[test]
    fn test_eval_sig_to_sig_equal_true() {
        let atom = ResolvedAtom::Equal(0, 8, None, ResolvedRhs::Signal(1, 8, None));
        let get_val = make_val_fn(&[(0, "FF"), (1, "FF")]);
        assert!(eval_atom(&atom, &get_val, &|_| false));
    }

    #[test]
    fn test_eval_sig_to_sig_gt() {
        let atom = ResolvedAtom::Gt(0, 8, None, ResolvedRhs::Signal(1, 8, None));
        let get_val = make_val_fn(&[(0, "10"), (1, "F")]);
        assert!(eval_atom(&atom, &get_val, &|_| false));
    }

    #[test]
    fn test_eval_sig_to_sig_sliced_equal() {
        let atom = ResolvedAtom::Equal(0, 8, Some((3, 0)), ResolvedRhs::Signal(1, 8, Some((3, 0))));
        let get_val = make_val_fn(&[(0, "A3"), (1, "B3")]);
        assert!(eval_atom(&atom, &get_val, &|_| false));
    }

    // ---- Evaluation tests: edge cases ----

    #[test]
    fn test_eval_slice_bit0_of_01() {
        let atom = ResolvedAtom::NonZero(0, 8, Some((0, 0)));
        let get_val = make_val_fn(&[(0, "1")]);
        assert!(eval_atom(&atom, &get_val, &|_| false));
    }

    #[test]
    fn test_eval_slice_bit0_of_00() {
        let atom = ResolvedAtom::NonZero(0, 8, Some((0, 0)));
        let get_val = make_val_fn(&[(0, "0")]);
        assert!(!eval_atom(&atom, &get_val, &|_| false));
    }

    #[test]
    fn test_eval_gt_zero_with_zero() {
        let atom = ResolvedAtom::Gt(0, 8, None, ResolvedRhs::Value("0".into()));
        let get_val = make_val_fn(&[(0, "0")]);
        assert!(!eval_atom(&atom, &get_val, &|_| false));
    }

    #[test]
    fn test_eval_gte_zero_with_zero() {
        let atom = ResolvedAtom::Gte(0, 8, None, ResolvedRhs::Value("0".into()));
        let get_val = make_val_fn(&[(0, "0")]);
        assert!(eval_atom(&atom, &get_val, &|_| false));
    }

    #[test]
    fn test_eval_lt_zero_with_zero() {
        let atom = ResolvedAtom::Lt(0, 8, None, ResolvedRhs::Value("0".into()));
        let get_val = make_val_fn(&[(0, "0")]);
        assert!(!eval_atom(&atom, &get_val, &|_| false));
    }

    // ---- Evaluation tests: tree expressions ----

    #[test]
    fn test_eval_not_expr() {
        let expr = ResolvedExpr::Not(Box::new(ResolvedExpr::Atom(ResolvedAtom::NonZero(
            0, 1, None,
        ))));
        let get_val = make_val_fn(&[(0, "0")]);
        assert!(eval_expr(&expr, &get_val, &|_| false));

        let get_val2 = make_val_fn(&[(0, "1")]);
        assert!(!eval_expr(&expr, &get_val2, &|_| false));
    }

    #[test]
    fn test_eval_nested_combine() {
        // (*a & *b) | *c  where a=1,b=0,c=1 → false | true = true
        let expr = ResolvedExpr::Combine(
            CombineOp::Or,
            vec![
                ResolvedExpr::Combine(
                    CombineOp::And,
                    vec![
                        ResolvedExpr::Atom(ResolvedAtom::NonZero(0, 1, None)),
                        ResolvedExpr::Atom(ResolvedAtom::NonZero(1, 1, None)),
                    ],
                ),
                ResolvedExpr::Atom(ResolvedAtom::NonZero(2, 1, None)),
            ],
        );
        let get_val = make_val_fn(&[(0, "1"), (1, "0"), (2, "1")]);
        assert!(eval_expr(&expr, &get_val, &|_| false));
    }

    // ---- Transition builder tests ----

    #[test]
    fn test_virtual_gt_transitions() {
        let real_transitions = vec![vec![
            (0u64, "0".into()),
            (100, "5".into()),
            (200, "A".into()),
            (300, "3".into()),
        ]];
        let resolved = ResolvedVirtual {
            name: "gt5".into(),
            expr: ResolvedExpr::Atom(ResolvedAtom::Gt(0, 8, None, ResolvedRhs::Value("5".into()))),
        };
        let trans = build_virtual_transitions(&resolved, &[], &real_transitions, 0, 400);
        assert_eq!(
            trans,
            vec![(0, "0".into()), (200, "1".into()), (300, "0".into())]
        );
    }

    #[test]
    fn test_virtual_slice_transitions() {
        let real_transitions = vec![vec![
            (0u64, "F".into()),
            (100, "FF".into()),
            (200, "F".into()),
        ]];
        let resolved = ResolvedVirtual {
            name: "msb".into(),
            expr: ResolvedExpr::Atom(ResolvedAtom::NonZero(0, 8, Some((7, 7)))),
        };
        let trans = build_virtual_transitions(&resolved, &[], &real_transitions, 0, 300);
        assert_eq!(
            trans,
            vec![(0, "0".into()), (100, "1".into()), (200, "0".into())]
        );
    }

    #[test]
    fn test_virtual_sig_to_sig_tracks_both() {
        let real_transitions = vec![
            vec![(0u64, "0".into()), (100, "FF".into())],
            vec![(0u64, "0".into()), (200, "FF".into())],
        ];
        let resolved = ResolvedVirtual {
            name: "eq".into(),
            expr: ResolvedExpr::Atom(ResolvedAtom::Equal(
                0,
                8,
                None,
                ResolvedRhs::Signal(1, 8, None),
            )),
        };
        let trans = build_virtual_transitions(&resolved, &[], &real_transitions, 0, 300);
        assert_eq!(
            trans,
            vec![(0, "1".into()), (100, "0".into()), (200, "1".into())]
        );
    }

    // ---- Issue 4a: bit-indexed signal resolution ----

    #[test]
    fn parser_paren_single_signal() {
        // Issue 4b: `(*a) == 'h1` must parse.
        let def = parse_virtual_def("v = (*a) == 'h1").unwrap();
        match &def.expr {
            Expr::Atom(Atom::Equal(s, None, Rhs::Value(v))) => {
                assert_eq!(s, "*a");
                assert_eq!(v, "1");
            }
            _ => panic!(
                "expected Equal atom after paren-wrapped LHS, got {:?}",
                def.expr
            ),
        }
    }

    #[test]
    fn parser_paren_combine_then_cmp_errors() {
        // Issue 4b: `(*a & *b) == 'h0` must reject with a clear error.
        let err = parse_virtual_def("v = (*a & *b) == 'h0").unwrap_err();
        assert!(
            err.to_lowercase().contains("single signal atom") || err.contains("paren"),
            "expected paren-pointing error, got: {}",
            err
        );
    }

    #[test]
    fn parser_cmp_in_combine() {
        // Comparisons can still appear inside combines.
        let def = parse_virtual_def("v = *a == 'h1 & *b == 'h2");
        assert!(def.is_ok(), "expected parse to succeed, got {:?}", def);
    }

    #[test]
    fn tokenizer_records_original_index_text() {
        // Single-index `[0]` is preserved as SliceSpec::Single, not Range.
        let def = parse_virtual_def("v = *sig[0]").unwrap();
        match &def.expr {
            Expr::Atom(Atom::NonZero(_, Some(SliceSpec::Single(0)))) => {}
            _ => panic!("expected Single(0), got {:?}", def.expr),
        }
    }

    #[test]
    fn resolve_single_index_literal_first() {
        // Signals exist as scalar `dmem_wr[0]` and `dmem_wr[1]` (unpacked array).
        // The literal name `dmem_wr[0]` must be picked, NOT a bit slice of `dmem_wr`.
        let def = parse_virtual_def("v = *dmem_wr[0]").unwrap();
        let names = vec![
            "tb.dut.dmem_wr[0]".to_string(),
            "tb.dut.dmem_wr[1]".to_string(),
        ];
        let widths = vec![1u32, 1u32];
        let rv = resolve_virtual(&def, &names, &widths, &[]).unwrap();
        match rv.expr {
            ResolvedExpr::Atom(ResolvedAtom::NonZero(idx, w, slice)) => {
                assert_eq!(idx, 0, "expected dmem_wr[0]");
                assert_eq!(w, 1);
                assert!(
                    slice.is_none(),
                    "expected literal-name match (no slice), got slice={:?}",
                    slice
                );
            }
            _ => panic!("unexpected resolved expr: {:?}", rv.expr),
        }
    }

    #[test]
    fn resolve_range_always_slice() {
        // Even when literals `*addr[0]` etc. exist, a *range* like [12:0] must
        // always be a semantic slice of the wide signal.
        let def = parse_virtual_def("v = *addr[12:0]").unwrap();
        let names = vec!["tb.dut.addr".to_string()];
        let widths = vec![32u32];
        let rv = resolve_virtual(&def, &names, &widths, &[]).unwrap();
        match rv.expr {
            ResolvedExpr::Atom(ResolvedAtom::NonZero(idx, w, Some((12, 0)))) => {
                assert_eq!(idx, 0);
                assert_eq!(w, 32);
            }
            _ => panic!("expected NonZero(0, 32, Some((12,0))), got {:?}", rv.expr),
        }
    }

    #[test]
    fn resolve_single_index_falls_back_to_slice() {
        // No literal `sig[7]` exists → fall back to slicing the 8-bit `sig`.
        let def = parse_virtual_def("v = *sig[7]").unwrap();
        let names = vec!["tb.dut.sig".to_string()];
        let widths = vec![8u32];
        let rv = resolve_virtual(&def, &names, &widths, &[]).unwrap();
        match rv.expr {
            ResolvedExpr::Atom(ResolvedAtom::NonZero(0, 8, Some((7, 7)))) => {}
            _ => panic!("expected slice fallback, got {:?}", rv.expr),
        }
    }

    #[test]
    fn resolve_single_index_multimatch_hard_errors() {
        // Pattern `*wr[0]` matches BOTH `dmem_wr[0]` and `imem_wr[0]` → error.
        let def = parse_virtual_def("v = *wr[0]").unwrap();
        let names = vec![
            "tb.dut.dmem_wr[0]".to_string(),
            "tb.dut.imem_wr[0]".to_string(),
        ];
        let widths = vec![1u32, 1u32];
        let res = resolve_virtual(&def, &names, &widths, &[]);
        assert!(res.is_err(), "expected hard error on multi-match");
        let msg = res.unwrap_err();
        assert!(
            msg.contains("literal") || msg.contains("must be exactly 1"),
            "expected literal-multimatch error, got: {}",
            msg
        );
    }

    // ---- Issue 4c: sliced equality with X/Z outside slice ----

    #[test]
    fn eval_sliced_eq_with_xz_outside_slice() {
        // 32-bit signal with X in upper 8 bits, defined [12:0] == 13'h0C1C.
        // Storage form: VCD raw binary (32 chars) when X/Z is present.
        let mut bin = String::with_capacity(32);
        for _ in 0..8 {
            bin.push('x');
        } // bits 31..24 = X
        for _ in 0..11 {
            bin.push('0');
        } // bits 23..13 = 0
        bin.push_str("0110000011100"); // bits 12..0 = 0x0C1C (13 bits)
        assert_eq!(bin.len(), 32);

        let atom = ResolvedAtom::Equal(0, 32, Some((12, 0)), ResolvedRhs::Value("0C1C".into()));
        let binding = [(0, bin.as_str())];
        let get_val = make_val_fn(&binding);
        assert!(
            eval_atom(&atom, &get_val, &|_| false),
            "slice [12:0] should equal 0x0C1C despite X in upper bits"
        );
    }

    #[test]
    fn eval_sliced_eq_with_xz_inside_slice_false() {
        // X bit *inside* the slice range — comparison must be false.
        let mut bin = String::with_capacity(16);
        for _ in 0..8 {
            bin.push('0');
        } // bits 15..8 = 0
        bin.push('x'); // bit 7 = X (inside slice)
        bin.push_str("0001011"); // bits 6..0 = lower bits of 0x8B
        assert_eq!(bin.len(), 16);

        let atom = ResolvedAtom::Equal(0, 16, Some((7, 0)), ResolvedRhs::Value("8B".into()));
        let binding = [(0, bin.as_str())];
        let get_val = make_val_fn(&binding);
        assert!(
            !eval_atom(&atom, &get_val, &|_| false),
            "slice with X bit inside range cannot match a numeric literal"
        );
    }

    #[test]
    fn eval_sliced_eq_signal_to_signal_xz_handling() {
        // LHS: 16-bit with X in upper byte. RHS: clean 8-bit. Slice [7:0] of LHS == RHS.
        let mut lhs_bin = String::with_capacity(16);
        for _ in 0..8 {
            lhs_bin.push('x');
        }
        lhs_bin.push_str("10101011"); // 0xAB
        let atom = ResolvedAtom::Equal(0, 16, Some((7, 0)), ResolvedRhs::Signal(1, 8, None));
        let binding = [(0, lhs_bin.as_str()), (1, "AB")];
        let get_val = make_val_fn(&binding);
        assert!(
            eval_atom(&atom, &get_val, &|_| false),
            "lower 8 bits should match RHS=0xAB"
        );
    }
}
