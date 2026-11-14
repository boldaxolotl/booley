# Mutation testing (`mutation`)

`mutation` checks that your tests catch bugs. It plants small bugs (Mutants) in
the RTL, runs the Target's tests against each one, and reports how many Mutants
the tests detected. A passing simulation only shows the tests accept the
design; a high mutation score shows they would notice if it were wrong. No LLM
is involved: the same RTL, `--count`, and `--seed` give the same Mutants (on a
Verilator Target, with the same Verilator version), so scores are reproducible
and comparable across runs. For what all
Flows share (choosing Targets, common options, logs, exit codes), see
[Running Flows](FLOW_REFERENCE.md).

Mutation drives a [Simulation](FLOW-SIM.md) Target and runs the whole suite of
that Target's Tests, so the Tests must already pass.

## Running mutation testing

1. Check that the Project has a simulation Target (`booley targets --for sim`
   lists them). There is no `--for mutation`: mutation runs on any simulation
   Target.
2. Ask the Developer Agent:

    > *"Check whether the FIFO tests catch injected bugs."*
    >
    > *"Run mutation testing on `sim_fifo` with 20 Mutants."*

3. The agent reports how many Mutants were detected, which survived, and the
   path to the report.

To reproduce a run yourself inside the Sandbox:

```bash
booley flow mutation --target sim_fifo --count 20 --seed 1
```

Mutation takes exactly one Target; `--target` is required and a list is
refused.

| Option | Effect |
|---|---|
| `--scope <file,...>` | RTL files to plant Mutants in; repeat the flag to add more. Default: the Target's own RTL, without testbench, vendored, and generated files. A file you name must be a Verilog or SystemVerilog source of the Target inside the Project: testbench files are refused, while vendored and generated files may be named explicitly. |
| `--count <N\|auto>` | Number of Mutants (default `10`). `auto` derives it from the size of the scope, capped by the time limit (about one Mutant per 150 seconds, at least 3). |
| `--seed <N>` | Non-negative integer that selects the Mutants (default `0`). Change it to try a different sample. |
| `--min-detected <N>` | Detected Mutants needed to pass. Default: 80 % of `--count`, rounded up; when fewer Mutants are scored than `--count` (the candidates ran out or failed to build), 80 % of the Mutants scored, rounded up. |
| `--dry-run` | Validate the Target and scope and print the count, threshold, and seed. Nothing is built or simulated. |

The shared options (`-C/--project`, `--timeout`, `--report-dir`, `--diagnostic`,
`-q/--quiet`) work as for every Flow.

## How a run works

1. Booley selects the Mutants for the seed from the scope files.
2. It builds the unmodified RTL and runs every test of the Target. All must
   pass; otherwise the run ends with `ERROR`, because a failing test could not
   tell a Mutant apart from the original.
3. Each Mutant is applied to a private copy of the sources, built, and the
   Target's tests run on it. Your RTL is never modified.
4. A Mutant is **detected** when a test fails on it, or when a test runs past
   its time limit (reported as `detected (timeout)`): ten times the test's
   baseline duration, at least 30 seconds and at most 300 seconds. A Mutant
   that no test fails on **survives**. A Mutant that does not build is dropped
   and the next candidate takes its place, so it never counts toward the
   threshold, which is 80 % of the Mutants scored.

Every run rebuilds the baseline and every Mutant, so a result never depends on
stale builds. Some survivors may be equivalent Mutants that change no
observable behavior, which is why the default threshold is 80 %, not all.

### Equivalent Mutants on Verilator Targets

On a Verilator Target, Booley compares the C++ Verilator generates for each
Mutant with the baseline's. A Mutant with identical C++ builds the same
simulator as the original, so no test could detect it: Booley skips it and
takes the next candidate instead. The run still scores `--count` Mutants
unless the candidates run out or fail to build; the report then states how many it scored. A
Mutant that does not build is replaced the same way on every Target, and the
report states how many candidates did not build. The run stops replacing after
`--count` candidates failed to build and scores the Mutants it kept; with none
kept it ends with `ERROR`. The report does not list the skipped Mutants; the
run's machine-readable result holds their IDs. On other Targets, such as
Icarus, no Mutant is skipped as equivalent.

## Which Mutants it plants

Booley parses each scope file (Verilog or SystemVerilog) and finds every spot
where one of six operators applies. Most operators make several kinds of
change; the table gives each kind its own row under its operator. Each Mutant
holds one change, so a spot with several possible changes yields one candidate
Mutant per change.

| Operator | What it changes | Example |
|---|---|---|
| `relational` | Swaps a comparison for its neighbour. | `a < b` -> `a <= b`, `a == b` -> `a != b` |
| `arithmetic-logical` | Swaps an arithmetic, bitwise, logical, or shift operator. `/`, `%`, and `**` are never swapped, so Mutants do not divide by zero; `+` and `-` are not swapped where both give the same result at the expression's width, such as a one-bit sum. | `a + b` -> `a - b`, `a & b` -> `a \| b`, `a << 2` -> `a >> 2` |
|  | Removes a unary `~`, `-`, or `!`; `!x` becomes `x != 0`, one bit wide. A `!x` that is a whole `if` or `?:` condition is left to `condition`. | `~a` -> `(a)`, `!b` -> `((b) != 0)` |
|  | Swaps a reduction operator or drops its negation: `&` and `\|` trade places, `^` becomes `\|`, and `~&`, `~\|`, `~^` lose their `~`. | `&a` -> `(\|(a))`, `~^a` -> `(^(a))` |
|  | Drops one operand of `&&`, `\|\|`, `&`, or `\|` by putting the operator's identity there (`1'b1` for `&&`, all ones for `&`), so the other passes through at the same width. An operand that calls a function or changes a variable is never dropped. | `p && q` -> `1'b1 && q`, `a & b` -> `((a) \| ~(a)) & b` |
|  | Exchanges `>>>` and `>>` where the shifted operand is signed. | `s >>> 1` -> `s >> 1` |
|  | Turns a signed cast into an unsigned one, never the reverse. | `$signed(x)` -> `$unsigned(x)`, `signed'(x)` -> `unsigned'(x)` |
| `condition` | Replaces the condition of an `if` or `?:` with its negation, `1'b1`, or `1'b0`. A `while`, `do`-`while`, or `for` condition is only forced to `1'b0`. | `if (valid)` -> `if (!(valid))` |
| `constant` | Changes a numeric literal's value: lowest bit flipped, plus one, or minus one. Size and base stay. The new value must fit a sized literal's size. | `4'd5` -> `4'd4`, `'0` -> `'1` |
|  | Gives an unsized literal its plus-one value only if that fits where the value goes: the target it is assigned to, a cast it passes through, and the other side of a comparison or arithmetic operator. Booley reads the widths from the file's default parameters. A literal whose width Booley cannot tell from the file, such as a connection to a module defined in another file, only gets values that fit its own bits. | `a + 1` -> `a + 2` (8-bit `a`); `flag <= 1` (1-bit `flag`) only becomes `0` |
|  | Changes a literal in the initializer of a module-level variable. | `reg [7:0] cnt = 8'd3;` -> `8'd2` |
|  | Replaces an enum member with the previous or next member of its enum, wrapping around. The enum must be declared in the same file; a two-member enum yields one Mutant. | `state == IDLE` -> `state == DONE` |
| `bit-select` | Moves a literal bit index or slice bound up or down by one, within the declared range. | `data[3]` -> `data[2]`, `data[7:4]` -> `data[6:3]` |
|  | Swaps two neighbouring operands of a concatenation. Equal neighbours, replication counts, streaming concatenations, and assignment patterns are skipped. | `{hi, lo}` -> `{lo, hi}` |
| `deletion` | Removes one assignment, net initializer, or `case` item. | `q <= d;` removed, `case` branch removed |

Mutations also reach the arguments of `$signed` and `$unsigned` and the
expressions in the port connections of module and interface instances.

Only design code is mutated. Booley skips:

- testbench, vendored, and generated files (by default);
- classes, programs, checkers, covergroups, and every assertion, because a
  mutated assertion fires and would count as a detection;
- parameter defaults and overrides, widths, delays, and attributes;
- a loop's condition (`for` stop expression, `while`, `do`-`while`) and any
  assignment inside the loop to a variable that condition reads, from every
  `relational` and `arithmetic-logical` operator swap, because a swapped
  condition or counter can keep the loop from ending; other swaps in the loop
  body, such as an accumulator, are still planted;
- the initializer and step of a `for` loop;
- everything inside a `while`, `do`-`while`, `forever`, or procedural `for`
  loop, body included, for constants, enum members, bit-select moves,
  concatenation swaps, deletions, and every `arithmetic-logical` mutation
  except an operator swap (`repeat` and `foreach` loops are not skipped);
- the arguments of system calls other than `$signed` and `$unsigned`;
- port connections of gate primitives, `bind` directives, and checker and
  program instances, connections that are a bare name, and every bit-select
  move and deletion in a connection;
- code a macro or include supplies, and the inactive branch of an `ifdef`
  (Booley applies the build's defines);
- spots that could drive a variable from two places, and `case` labels (moving
  one can overlap another item).

## How it chooses which Mutants

1. Booley collects the candidates from all scope files. Identical spots count
   once.
2. Within each operator, candidates are ranked by a hash of `--seed` and the
   Mutant's ID. The ID comes from the operator, file, and position inside its
   module, so editing code elsewhere in the file does not change it.
3. The six operators are then taken in turn, one candidate each, in the order
   relational, arithmetic-logical, condition, constant, bit-select, deletion,
   until `--count` Mutants are chosen. A
   small `--count` therefore covers different kinds of bugs instead of many of
   one kind.

If fewer candidates exist than `--count`, all of them that build run and the
report says so. An unset `--min-detected` then becomes 80 % of the Mutants that ran, rounded
up, and the tally labels that base as scored. The knobs are `--scope` (where to
look), `--count` (how many), and `--seed` (which sample); there is no filter by
operator. The same RTL, scope, count, and seed choose the same Mutants (on a
Verilator Target, with the same Verilator version).

## Reading results

The report starts with what reproduces the run, then the tally and the
verdict. The first line records the pyslang version (the parser that finds
the candidate Mutants) and, on a Verilator Target, the Verilator version
(its generated C++ decides which Mutants are equivalent). Example
(Verilator):

```text
[mutation_test] 10 Mutants (seed 0, count 10, generator 6, pyslang <version>, Verilator <version>)
[mutation_test] sim sweep: 10/10 tested              4m
  8 detected (1 timeout), 2 not detected
  detected: 8/10 (required: 8, default 80% of --count)
  NOT DETECTED [arithmetic-logical] rtl/alu.sv:42: 'a + b' -> 'a - b'
…
RESULT: PASS (8/10 detected)
```

Survivors are listed with file, line, and the original and mutated code: each
is a spot your tests do not check. When candidates did not build, a line such
as `[mutation_test] 3 candidate Mutants did not build and were replaced`
appears in the report header, after the shortfall line if there is one; it is absent when every candidate built.

On a `--count auto` run the header reads
`count auto -> N (formula F, budget cap C)`, and `mutation-results.md` shows the
same under Count. F is the count from the scope size, C the cap from the time
limit, and N the smaller of the two.

| Verdict | Meaning | Exit |
|---|---|---:|
| `PASS` | Detected Mutants reach the threshold. | `0` |
| `FAIL` | Fewer Mutants were detected than required. | `1` |
| `ERROR` | No verdict: a failing baseline, a Target that cannot run, an unusable `--scope` entry, an invalid option, or a baseline build failure. | `2` |

Each run's files are in its numbered run directory, under `flow-reports/` in
the Booley Directory or `--report-dir`
([Reports](FLOW_REFERENCE.md#reading-results)):

| File | What it holds |
|---|---|
| `mutation/<N>/campaign/mutation-results.md` | The Mutants and the outcome of each, and each test run's baseline duration and time limit. |
| `mutation/<N>/campaign/mutation-specs.md` | Every Mutant: operator, file and line, original and mutated code. |
| `mutation/<N>/campaign/manifest.json` | The run's identity (seed, count, generator and pyslang versions, the Verilator version on a Verilator Target, scope hashes), each Mutant's status, and each test run's baseline duration and time limit. A `--count auto` run also records how it derived the count. |
| `mutation/<N>/campaign/variants/` | The mutated source of each Mutant. |
| `mutation/<N>/campaign/baseline.log`, `mutant-logs/` | The simulator output of the baseline and of each Mutant. |

## Configuration (`[flows.mutation]`)

```toml
[flows.mutation]
enabled = true         # false turns the Flow off
timeout_ms = 1800000   # default: 30 minutes for the whole run
```

`timeout_ms` (or `--timeout`) sizes `--count auto`: the budget is divided to cap
the number of Mutants, so a larger limit allows more of them. Over MCP it also
raises the wait before the call hands back a run id. The command line does not
stop a run when the budget passes. See
[Flow and Specialist availability](CONFIG.md#flow-and-specialist-availability-enabled).

## Goals

A `MUTATION:{target}` Goal is met by this Flow's result: the Flow records the
`mutation_score` criterion. Your own runs never count as Goal evidence. A Goal
sets the campaign with `total: N`, `min_detected: K`, or `auto: true` in place of
`--count` and `--min-detected`; without them it requires 8 of 10 Mutants.
