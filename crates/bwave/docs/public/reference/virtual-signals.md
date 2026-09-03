# Virtual signals

Virtual signals are user-defined 1-bit boolean predicates
over existing signals and earlier virtual definitions. They
behave like query-scoped signals: you can `find` on
them, `sample` on them, plot them in `wave`, snapshot
them in `value`, measure with `distance`. They don't
persist into the store: each invocation re-evaluates
them, but the cost is negligible.

Defined via `--virtual "name = expr"` on a supported query
subcommand. Repeatable; definition order controls composition.

## Scope: where `--virtual` works

Accepted on: `wave`, `find`, `sample`, `distance`,
and `value`.

Rejected on: `list`, `signal`, `diff`, `stats`, `stuck`,
and `build`. These commands reject `--virtual` during
argument parsing.

## Grammar

A Verilog-subset expression syntax:

```
expr := unary | binary | atom | '(' expr ')'

unary  := '~' expr     // bitwise not
        | '!' expr     // logical not

binary := expr op expr
   op  := '&' | '|' | '^'                  // bitwise
        | '&&' | '||'                      // logical
        | '==' | '!=' | '<' | '<=' | '>' | '>='   // comparison

atom := '*' signal_ref
      | verilog_literal
      | integer

signal_ref := name
            | name '[' index ']'
            | name '[' msb ':' lsb ']'
```

`*signame` references an existing signal. The leading
`*` distinguishes signal names from literals: `valid`
would be a bare name (which the parser may treat as a
literal or reject), `*valid` is unambiguous.

## Bit slicing

Two forms inside `[ ]`:

- **Single index**: `*sig[N]`, the Nth bit (0-indexed,
  LSB first). Width: 1.
- **Range**: `*sig[M:N]`, bits M down to N (Verilog
  MSB:LSB convention; M >= N). Width: M-N+1.

### Unpacked-array fallback

A single-index `*sig[N]` first looks for a literal
signal named `sig[N]` (this is how Verilog VCD encodes
unpacked array entries: `mem[0]`, `mem[1]`, etc.). If no
such signal exists, it falls back to bit-slicing the
multi-bit signal `sig`.

Range syntax (`[M:N]`) always slices; it never falls back
to a literal name.

Examples:

- `*mem[3]`: if signal `mem[3]` exists (unpacked array),
  use it. Otherwise, bit 3 of multi-bit signal `mem`.
- `*data[15]`: bit 15 of signal `data`.
- `*data[15:8]`: bits 15:8 (upper byte) of `data`.

## Values

Inside expressions, use Verilog literals:

- `'d255` (decimal 255)
- `'hFF` (hex FF)
- `'b1010` (binary)
- `8'd255`, `4'hF`, `1'b1` (width-prefixed)
- Bare integers (`0`, `1`, `255`) are accepted as decimal.

Bare hex without a base (`FF`) is rejected. See
`reference/verilog-literals`.

## Comparisons

`==`, `!=`, `<`, `<=`, `>`, `>=` produce 1-bit results.

**Signal-to-signal comparisons require matching
widths**. `*sig_a == *sig_b` errors out if the two
signals are different widths; there is no implicit
zero-extension. Slice one side to match: `*sig_a ==
*sig_b[7:0]`.

Signal-to-literal comparisons coerce the literal to the
signal width.

## Boolean operators

- `&` `|` `^` `~` and `&&` `||` `!` combine boolean
  atoms and always produce a 1-bit result.
- A bare multi-bit signal atom is true when non-zero.
- To compare multi-bit values, use a comparison or slice;
  Virtual Signals do not produce multi-bit results.

For a 1-bit handshake predicate, either works:

```
hsk = *valid & *ready          // bitwise (both 1-bit)
hsk = *valid && *ready         // logical (also 1-bit)
```

For multi-bit reductions, prefer comparisons:

```
nonzero = *data != 'd0          // 1 bit, clear semantics
nonzero = | *data               // (reduction or, NOT supported yet)
```

(Reduction operators are not in the grammar.)

## Parentheses

**Required to mix operators**. There is no operator
precedence beyond unary > binary; mixing two binary
operators without parens is a parse error.

```
// OK
v = (*a & *b) | *c
v = (*sig > 'd10) && (*sig < 'd20)

// PARSE ERROR
v = *a & *b | *c
```

## Worked examples

Handshake firing:

```bash
--virtual "hsk = *valid & *ready"
```

Counter above threshold:

```bash
--virtual "hi = *counter > 'd127"
```

A specific FSM state:

```bash
--virtual "in_recovery = *state == 'd5"
```

MSB of a wide bus:

```bash
--virtual "sign = *data[31]"
```

Multi-bit equality with a literal:

```bash
--virtual "match_magic = *header == 32'hDEADBEEF"
```

Equality between two same-width signals:

```bash
--virtual "echoed = *out_data == *in_data"
```

Slice equality:

```bash
--virtual "page_hit = *addr[12:0] == 13'h0C1C"
```

Composite predicate (note parens):

```bash
--virtual "stall = (*valid & ~*ready) | *busy"
```

Using a virtual signal in another query:

```bash
bwave find sim.fst \
    --virtual "hsk = *valid & *ready" \
    hsk 'h1 --first
```

## Errors

Every Virtual Signal definition is parsed and resolved when
the query starts. If any definition fails (unknown signal,
width mismatch, syntax error), B-Wave prints an error to
stderr and exits immediately with code 2 before emitting
query results.

Common failure modes:

- **`unknown signal '*foo'`**: typo, or scope mismatch.
  Verify with `bwave list -s "*foo*"`.
- **`width mismatch: *a is 8 bits, *b is 16 bits`**:
  slice one side: `*a == *b[7:0]`.
- **`expected operand after '&'`**: missing parens
  around a sub-expression.
- **`bare hex literal 'FF' rejected`**: use `'hFF`.
