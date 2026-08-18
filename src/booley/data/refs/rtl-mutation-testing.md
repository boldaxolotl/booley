# RTL Mutation Testing Guide

Single-point RTL mutation testing with **runtime mutation selection** — all
mutations are baked into one set of muxed RTL files and selected at simulation
time via the `MUT_ID` plusarg.  The design is compiled once and simulated N
times; no per-mutation source edits.

## Workflow

1. Read the RTL files in scope.  Build a complete picture: datapath, control
   logic, output ports.
2. Design N single-point mutations.  Each one wraps the original code in a
   runtime mux gated by `booley_mut_pkg::mut_id == k`.  The original code is
   the default branch; the mutated code activates only when `mut_id == k`.
3. Above each muxed site, place a marker comment:
   `// MUTATION #<index>: <original_code> -> <mutated_code>`.
4. Emit the JSON spec list (see *Output Format* below).

The harness handles all infrastructure:

- generates `package booley_mut_pkg; ... int mut_id = 0; endpackage` and
  prepends it to the DUT top file.  The package mirrors the DUT top's own time
  declarations — a `timeunit`/`timeprecision` pair is copied into the package
  body, a `` `timescale `` directive is re-emitted above it — because a
  timescale-less package on a design that declares time units everywhere trips
  Verilator's TIMESCALEMOD, which `-Wall`/`--Werror` turns into an elaboration
  error before a single mutation runs,
- inserts `import booley_mut_pkg::*;` and a `$value$plusargs("MUT_ID=%d")`
  reader inside the DUT top module.  The reader echoes
  `[booley_mut] MUT_ID=<k> active` on every mutant run (and stays silent on the
  MUT_ID=0 baseline, so a baseline run is byte-identical to an unmutated one) —
  that line is the runtime proof the plusarg actually reached the design, which
  is what separates "the tests don't cover this scope" from "the harness is
  broken" when a sweep kills nothing,
- builds the design once after you finish,
- runs verification sims (baseline + one pinned mutation),
- if scope files do not contain the DUT top, **you** must add
  `import booley_mut_pkg::*;` at the top of every module that references
  `mut_id`.

**Never** modify, remove, or duplicate the harness-generated blocks.

## Mux Templates by Category

Every mutation must follow one of these patterns.  If a candidate site does
not fit a template, **skip it** and pick another site.

The muxed expression and the original expression **must be type- and
width-identical**.  Otherwise the elaborator will warn or silently truncate
and the mutation becomes invalid.

### Expression mutation (operator / comparison / polarity / bit-select)

```systemverilog
// MUTATION #3: a + b -> a - b
assign y = (mut_id == 3) ? (a - b) : (a + b);
```

Works for:
- Arithmetic operator change (`+` ↔ `-`, `*` ↔ `/`, etc.)
- Comparison operator flip (`==` ↔ `!=`, `<` ↔ `<=`, etc.)
- Polarity flip (`x` ↔ `~x`, `cond` ↔ `!cond`)
- Bit-select shift (`a[7:4]` ↔ `a[8:5]`)
- Constant bit flip (`4'b1010` ↔ `4'b1011`)

### Reset value mutation

```systemverilog
always_ff @(posedge clk) begin
  if (rst)
    // MUTATION #5: 4'b0000 -> 4'b0001
    q <= (mut_id == 5) ? 4'b0001 : 4'b0000;
  else
    q <= d;
end
```

### FSM next-state mutation

```systemverilog
case (state)
  S1: begin
    // MUTATION #7: S2 -> S3
    next_state = (mut_id == 7) ? S3 : S2;
  end
  ...
endcase
```

### Stuck-at on enable / valid

```systemverilog
// MUTATION #9: req & ~busy -> 1'b1
assign en = (mut_id == 9) ? 1'b1 : (req & ~busy);
```

### LHS / signal swap (statement-level mutation)

When the mutation gates **which assignment statement runs**, wrap the
statement block, not an expression:

```systemverilog
always_ff @(posedge clk) begin
  // MUTATION #13: a<=x;b<=y -> a<=y;b<=x
  if (mut_id == 13) begin
    a <= y;
    b <= x;
  end else begin
    a <= x;
    b <= y;
  end
end
```

### Mux branch swap

```systemverilog
// MUTATION #15: sel ? a : b -> sel ? b : a
assign out = (mut_id == 15) ? (sel ? b : a) : (sel ? a : b);
```

## Hard Rules

- **One mutation per `always` block.** Multiple muxes interacting in the
  same block at the same `mut_id` create cross-coupled bugs.
- **Type and width must match** between the mutated and original branches.
- **Distribute across files in scope.** Don't concentrate all mutations in
  one module if the scope spans several.
- **Functional logic only** — never mutate comments, parameters, dead /
  unreachable code, or anything outside the simulated path.
- **No structural changes** — port widths, declarations, module
  instantiations are off-limits (they break the compile-once model).
- **Always leave the original code as the default** (`mut_id != k`) branch.
  This guarantees `MUT_ID=0` runs the baseline design unaltered.

## Forbidden Mutation Categories

These categories are unrunnable under runtime selection and must not be used.
The harness rejects them at spec validation as a verification failure:

- `instance_swap` / `module_instantiation_swap`
- `port_width` / `port_declaration` / `declaration_change`
- `sensitivity_list` / `trigger_reorder`
- `code_removal` / `delete_always` / `delete_assign`
- `clock_polarity` / `reset_polarity`

## Quality Criteria — REJECT a mutation if:

- **Error correction absorbs it** — RTL redundancy masks the fault before
  it reaches outputs.
- **Performance-only impact** — affects cycle count / throughput but not
  functional correctness.
- **Dead / unreachable code** — the mutated path is never exercised.
- **Equivalent mutation** — the mutated expression produces the same output
  for all valid inputs (e.g., swapping operands of `+`, `==`).

A good mutation corrupts at least one output-observable value for at least
one legal input stimulus.  The `detectability_argument` field must trace
the mutation to observable output corruption.

## Output Format

After writing all muxes, return a JSON object with the mutation list:

```json
{
  "mutations": [
    {
      "index": 1,
      "mut_id": 1,
      "category": "operator_change",
      "file": "mod_a.sv",
      "line": 42,
      "original_code": "a + b",
      "mutated_code": "a - b",
      "detectability_argument": "Flipping addition to subtraction corrupts every result"
    }
  ]
}
```

`index` and `mut_id` are equal for now (1-based, contiguous).  The harness
drives `+MUT_ID=k` per simulation to activate mutation `k`.  `MUT_ID=0` is
reserved for the unmutated baseline.

## Harness-Side Verification

After you finish writing muxes, the harness will:

1. Build the design once.
2. Run `MUT_ID=0` — must pass (proves your default branches are correct).
3. Run one pinned non-zero `MUT_ID` — must compile and complete without
   crashing (proves your mux scaffolding is sound).

If either fails, the harness resumes this session with the failure log
and asks you to fix the muxed file.  You have up to two retries before
the campaign aborts.  Retry instructions differ by failure type:

- **Forbidden category** → return a fresh JSON spec list with valid
  categories.
- **Elab / sim failure** → only edit the muxed source files; do not
  return a new JSON.
