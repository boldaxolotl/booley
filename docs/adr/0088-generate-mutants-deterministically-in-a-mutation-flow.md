# Generate Mutants deterministically in a Mutation Flow

Status: accepted (2026-11-07 triage of
[#2856](https://github.com/boldaxolotl/booley/issues/2856))

For mutation testing only, supersedes the `mutation_tester` key of the
`[models]` table in
[ADR 0084](0084-run-each-specialist-on-the-agent-cli-its-model-implies.md)
and the `mutation_tester` Specialist form with its **Start fresh** option in
[ADR 0086](0086-launch-flows-and-specialists-from-the-dashboard.md).

## Context

Today the Mutation Tester is an LLM Specialist. A model proposes each mutation
as an exact text replacement; Booley applies it, builds, simulates, classifies
the result, and stores it in a mutation lock keyed on the scope files' hashes.
Booley gives the model only a prose guide to mutation categories
(`src/booley/data/refs/rtl-mutation-testing.md`); it ships no executable mutation operators.
Two runs on the same RTL can pick different mutations, so scores are not
reproducible or comparable across runs, and every run costs model tokens. The
roadmap called this design temporary. #2856 surveyed existing tools and
proposed a deterministic generator; its triage settled the decisions below.

## Decision

- **Rule-based generator.** A Booley-owned, rule-based Mutant generator
  replaces the LLM creator. Applying, building, simulating, and classifying
  Mutants stay as they are.
- **Parser.** pyslang parses Verilog and SystemVerilog, inside the Sandbox
  only. VHDL is out of scope.
- **Fixed v1 Mutation Operator table**, not configurable by Projects:
  1. relational and equality operator swap;
  2. arithmetic, bitwise, and logical operator swap;
  3. condition negation or forcing;
  4. constant and reset-value change;
  5. bit-select or slice off by one;
  6. deletion of an assignment or a `case` item.
- **Stable Mutant IDs.** A Mutant's ID is a content hash of its operator, file,
  source span, and original text, so IDs do not depend on other Mutants or on
  edits to other files. How the span is expressed, so that edits elsewhere in
  the same file move it as little as possible, is left to
  [#2933](https://github.com/boldaxolotl/booley/issues/2933).
- **Selection.** Candidates are ranked by a hash of (seed, Mutant ID) and taken
  round-robin across operators until the count is reached. `--count N|auto`
  keeps today's default (10) and auto formula,
  `clamp(round(sqrt(source lines)), 3, 25)`. Today `auto` is also capped at
  `max(3, Specialist timeout // 150)`; a Flow has no Specialist timeout, so
  [#2935](https://github.com/boldaxolotl/booley/issues/2935) decides that cap's
  basis. The default seed is 0; `--seed` overrides it.
- **Default scope.** The Target's own RTL, excluding testbench, vendored, and
  generated files.
- **Pass threshold.** By default `ceil(0.8 × count)` Mutants must be detected
  (today's default is all of them), to absorb equivalent Mutants. An explicit
  `min_detected` integer overrides it. There are no waivers and no SAT
  equivalence filter; the report notes that some surviving Mutants may be
  equivalent.
- **Timeout.** Per test, `max(10 × baseline duration, 30 s)`, capped at 300 s.
  A timeout counts as detected and is reported separately as "detected
  (timeout)".
- **Reproducibility identity.** Every result and its criterion detail record
  the scope files' hashes, the generator version, the pyslang version, the
  seed, and the count. The Result Cache may key Mutation Flow results on this
  identity plus the tests' fingerprint; that needs its own triage
  ([#2937](https://github.com/boldaxolotl/booley/issues/2937)). The scope of
  [ADR 0076](0076-reuse-results-at-committed-states-through-a-project-result-cache.md)
  (proposed; graded `sim`, `synth`, and `fpga` passes) does not cover Mutation
  Flow results until #2937 amends it.
- **Removed with no deprecation release.** The LLM creator with its prompt and
  category guide, its steering (`--steer`), the `detectability_argument`, the
  `mutation_tester` model setting, and the mutation lock with `--regen-lock`
  and warm reuse. Generation is a pure function of its inputs, so there is
  nothing to lock.
- **A Booley Flow, not a Specialist.** The Mutation Tester becomes the Booley
  Flow `booley flow mutation`. The `mutation_score_<target>` criterion keeps
  its name. As a Flow it takes no Specialist-only inputs (`--instruction`, a
  model, a transcript).

## Rejected alternatives

- **YosysHQ MCY / Yosys `mutate`.** It mutates the synthesized netlist, so
  tests run on a netlist (slower, with different X and initialization
  behavior) and Mutants map poorly to source lines. Its Mutant list reshuffles
  after any RTL edit or Yosys upgrade, even with the same seed. Its runner and
  database duplicate Booley's.
- **AVL `avl-mutation-testing`.** Its position-based IDs renumber on any edit.
  It owns a cocotb + Verilator + `make` flow, while Booley also supports Icarus
  and classic Targets. It is very young. Its operator list is kept as a
  reference; vendoring its code was also rejected.
- **Regex-based injectors.** Text matching has no syntax awareness: it cannot
  tell an operator from a comment or a string, nor give the source span a
  stable ID rests on. The surveyed ones were hobby projects.
- **Other tools surveyed in #2856** (rtl-buddy-xeno, GateTruth, Verismith, IBM
  Chiffre) were judged there immature, research-only, or fault injection
  rather than mutation scoring. #2856 found Synopsys Certitude to be the only
  commercial testbench mutation tool; it serves only as a feature benchmark.
- **Blocking/nonblocking assignment swap operator.** Its Mutants are mostly
  equivalent or racy.
- **Project-configurable operators.** A fixed table keeps scores comparable
  across Projects and runs.
- **Sorting candidates by (file, offset, operator) and sampling with a seeded
  `random.Random`** (the #2856 proposal). Adding or removing any candidate
  shifts every later pick; ranking by hash(seed, ID) leaves the other Mutants'
  ranks unchanged.
- **SAT equivalence filter (bounded Yosys miter) and per-Mutant waivers.** The
  80 % default threshold absorbs equivalent Mutants without new machinery or a
  waiver workflow.
- **Timeouts counted in simulation cycles.** The flow has no cycle limit
  available; today's timeout is a fixed 300 s wall clock.
- **Keeping the LLM creator behind a flag for one release.** Two generators to
  maintain, and the LLM's results are not reproducible.

## Consequences

- Mutation scores are reproducible and cost no model tokens.
- Existing mutation locks are discarded.
- Specialist Source Isolation no longer concerns mutation testing: no LLM reads
  the sources.
- A Project that still sets `[models] mutation_tester` must remove that key;
  #2935 decides how loading reports it.
- The default pass threshold loosens from all Mutants to 80 %. Goal Mode's
  `mutation` Goal family (`scope`, `min_detected`, `total`, `auto`) is
  affected too: a Goal without `min_detected` moves from all to 80 %, and how
  its fields map onto the Flow's count and seed, and whether Flow evidence
  satisfies it, is decided with the switch-over (#2935).
- QA mission areas that check the mutation lock are rewritten
  ([#2938](https://github.com/boldaxolotl/booley/issues/2938)).
- Delivery order:
  [#2933](https://github.com/boldaxolotl/booley/issues/2933) (generator core,
  operators 1-3, pyslang),
  [#2934](https://github.com/boldaxolotl/booley/issues/2934) (operators 4-6),
  #2935 (switch-over),
  [#2936](https://github.com/boldaxolotl/booley/issues/2936) (rename to
  `booley flow mutation`), #2937 (Result Cache), #2938 (QA mission). User docs
  change as each piece ships.

## Amendments (2026-11-13)

Recorded while implementing
[#3626](https://github.com/boldaxolotl/booley/issues/3626).

The operator names, their order, the selection rule, and the Mutant ID recipe
are unchanged; every Mutant of generator version 2 keeps its ID and text. The
generator version is 3. What each operator now includes:

1. relational and equality operator swap (unchanged);
2. arithmetic, bitwise, and logical operator swap, plus removal of unary `~`,
   `-`, and `!` (`!x` becomes `x != 0`, one bit wide); a reduction swap;
   dropping one operand of `&&`, `||`, `&`, and `|` by putting the operator's
   neutral value there, never for an operand that calls a function or changes
   a variable; `>>` and `>>>` exchanged where the shifted operand is signed;
   `$signed` and `signed'()` turned unsigned;
3. condition negation or forcing (unchanged);
4. constant and reset-value change, plus an enum member replaced by the
   previous or next member of an enum declared in the same file, and numeric
   literals in the initializers of module-level variables (including those in generate blocks);
5. bit-select or slice off by one, plus the swap of two neighbouring
   concatenation operands;
6. deletion of an assignment or a `case` item (unchanged).

Two kinds of site become reachable by every mutation above and by the swaps,
conditions, and constants: the arguments of `$signed` and `$unsigned`, and the
expressions in the port connections of module and interface instances.
Bit-select moves and deletions never apply in a connection. A connection that
is a bare name, and a parameter override, a gate primitive, a `bind` directive,
a checker instance, or a program instance, yield no site. No new mutation applies inside a `while`,
`do`-`while`, `forever`, procedural `for`, or a `case` label.

## Amendments (2026-11-13, #3635)

Recorded while implementing
[#3635](https://github.com/boldaxolotl/booley/issues/3635). "There are no
waivers and no SAT equivalence filter" still holds. Two checks now keep
provably equivalent Mutants from counting against the threshold.

**Build-identity check, on Targets simulated with Verilator only.** After a
Mutant builds, the C++ Verilator generated for it is compared with the C++
generated for the baseline. Only the generated `.cpp` and `.h` files are
compared, by relative path and content, never object code, dependency or
makefile output, or simulation output. The one difference that is ignored is
where the Mutant's private source copy lives: every spelling of that directory
(absolute, resolved, and relative to the Project Root) is replaced by one
token before the comparison. This matters because a design that calls `$error`
or `$stop` carries the absolute path of its sources into the C++. Any other
difference keeps the Mutant, so the check may miss an equivalent Mutant and
never drops another one. A build whose layout is not Verilator's (no
`*__ver.d` in its root) is not compared.

- A Mutant that matches is not tested or scored, and its build directory is
  removed. Candidates are taken in the selection order, so the next one takes
  its number and build directory; the kept Mutants are numbered 1 to the count
  without gaps. When the candidates run out, the run scores the Mutants it has
  and reports the shortfall with the same text as a generator shortfall (the
  report must not mention what was dropped); an unset `--min-detected` follows
  the number scored.
- A Mutant that does not build stays `invalid` and is not replaced.
- The report and `mutation-results.md` never list a dropped Mutant. The
  Record's detail lists their IDs, in drop order, under `equivalent_dropped`
  (an empty list when none were dropped or the Target is not simulated with
  Verilator).
- On a Target that is not simulated with Verilator, such as Icarus, no
  comparison runs and every selected Mutant is simulated.

**Reproducibility identity.** The identity gains the Verilator version (the
first line of `verilator --version`, read in the baseline build's own
environment, `unknown` if it cannot be read), on Verilator Targets only, so
two runs that differ only in that version do not have the same identity. The
Result Cache keys Mutation Flow results on this identity once #2937 builds
that; nothing caches Mutation Flow results today. Target build options (the
`.core`'s defines, tool options and flags) stay outside the identity, as the
scope hashes already leave them out; a change to them can change which
Mutants are dropped, so the same Mutants are guaranteed only for the same
Verilator version and the same build settings.

**Generator filter.** The `+`/`-` swap of the `arithmetic-logical` operator
yields no Mutant where both results are equal at the width the parser reports
for the expression: the width is 1, or the right operand is a known constant
`c` with `2c` a multiple of `2` to the power of the width. An expression whose
width the parser does not report keeps its Mutant. The file is elaborated
alone, so a module that another file instantiates with a parameter override,
or reaches through an interface port, is elaborated here with defaults and
another width elsewhere: the swap is kept in any top-level unit that declares
a parameter (any form but `localparam`, with or without the keyword in a port
list) or a type parameter, or has an interface port or a port of a named type
(a bare interface name is one). A width set by a
macro defined only in another file that is not included is the known blind
spot, the same single-file limit the select-range facts have; it can drop a
Mutant that is not equivalent in the full design. Every other Mutant keeps its
ID. The generator version is 4: the same inputs give the same Mutants only for
the same version.

## Amendments (2026-11-14, #3934)

**Unsized plus one.** The `constant` operator gave an unsized literal only values
that fit the literal's own bit length, so `1`, `3` and `7` never got their plus-one
Mutant. It now also allows values that fit the narrowest width the literal's value
is used at, as the file's elaboration reports it. An unsized literal elaborates as a
32-bit `int` and widens its whole context, so the expression's own width is no
guide. The width is the minimum of: the literal's own width; the width of every
narrowing conversion above it, implicit or an explicit cast, applied to every
unsized literal below it (a misjudgment only withholds a Mutant); and, for the
operands of a context-determined binary operator, the width of the other operand.
So `flag <= 1` on a 1-bit `flag` stays `1 -> 0`, and `a == 255` on an 8-bit `a`
gains no `256`. A literal the elaboration does not reach (from a macro or include,
or in a connection to a module defined in another file) keeps the bit-length
rule. Only plus-one Mutants are added: every Mutant of version 4 keeps its ID and
text. Unlike the `+`/`-` swap filter, a unit that another file may elaborate with
other parameters is trusted here with its default elaboration: an override that
narrows the width can give a Mutant that repeats another variant there, or one
that Verilator rejects for a width warning and the run marks `invalid`. The
generator version is 5.
