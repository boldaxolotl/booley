# Generate Mutants deterministically in a Mutation Flow

Status: accepted

For mutation testing only, this replaces the `mutation_tester` model setting of
[ADR 0084](0084-run-each-specialist-on-the-agent-cli-its-model-implies.md).

## Context

Mutation testing used to be an LLM Specialist: a model proposed each mutation as
a text replacement, and a lock stored the result. Two runs on the same RTL could
pick different mutations, so scores were not reproducible or comparable, and
every run cost model tokens. Booley now owns a rule-based generator and runs
mutation testing as a Booley Flow, `booley flow mutation`, with no model and no
agent CLI.

## Decision

### Generation

- **Rule-based, parser-backed.** pyslang parses Verilog and SystemVerilog
  inside the Sandbox. VHDL is out of scope. Regex injection is rejected: text
  matching cannot tell an operator from a comment or a string, nor give the
  source span a stable ID needs.
- **A fixed Mutation Operator table, not configurable by Projects**, so scores
  stay comparable across Projects and runs. The operators are: relational and
  equality swap; arithmetic, bitwise, and logical swap; condition negation or
  forcing; constant and reset-value change; bit-select or slice off by one;
  deletion of an assignment or a `case` item. The blocking/nonblocking swap is
  not an operator: its Mutants are mostly equivalent or racy.
- **Sites a Mutant must not reach.** No change may keep a loop from ending: the
  swap operators skip a loop's stop condition and writes to variables it reads,
  and nothing else mutates inside an unbounded loop or a `case` label. Forcing
  a loop condition to false stays allowed. Bit-select moves and deletions never
  apply in a module or interface port connection.
- **Facts come from the Target's own elaboration.** The generator elaborates all
  of the Target's HDL files as one compilation unit, with the Target's tops,
  parameter overrides, defines, simulator macros and include directories, so
  literal widths, enum members, signed shifts and equal-result swaps are judged
  as the build sees them. Testbench files are context and are never mutated.
  When the Target cannot be elaborated as a whole, each scope file is
  elaborated alone, and the run records and reports why, with a note that
  Mutants may then duplicate each other. A unit the Target does
  not instantiate is elaborated alone with default parameters, and any swap
  whose result depends on a width stays, because another file might elaborate
  the unit differently.
- **Equivalent Mutants are dropped where they can be proven so cheaply.** The
  generator yields no `+`/`-` swap whose two results are equal at the
  expression's width. After a build on a Verilator Target, a Mutant whose
  generated C++ equals the baseline's (apart from where its private source copy
  lives) is dropped as equivalent, and one whose generated C++ equals a Mutant
  already kept is dropped as a duplicate of it. The check may miss an equivalent
  or duplicate Mutant and never drops a Mutant whose C++ differs. There is no
  SAT equivalence filter and there are no waivers: the pass threshold absorbs
  the rest.
- **A Mutant that does not build is replaced**, on every Target, like an
  equivalent or duplicate one. Replacement walks the candidates in selection
  order, so kept Mutants stay numbered 1 to the count without gaps. The run
  stops replacing once as many candidates have failed to build as `--count`, so
  a Target that builds no Mutant cannot walk the whole list; equivalent and
  duplicate drops do not count toward that cap. Shortfalls are reported with one
  text, and a run that keeps no Mutant ends with an error. The report never
  lists equivalent or duplicate drops; it counts build failures; the result
  detail lists the IDs of all three, each duplicate with the ID of the Mutant it
  duplicates.

### Identity and selection

- **Stable Mutant IDs.** A Mutant's ID is a content hash of its operator, file,
  position inside its enclosing design unit, original text and replacement, so
  it depends neither on other Mutants nor on edits elsewhere in the file.
- **Selection is hash ranking, round-robin across operators.** Candidates are
  ranked by a hash of (seed, Mutant ID) and taken one operator at a time until
  the count is reached. Sorting by position and sampling with a seeded RNG was
  rejected: adding or removing any candidate shifts every later pick, while
  hash ranking leaves the other Mutants' ranks unchanged. The default seed is
  0.
- **Count.** `--count N|auto`, default 10. `auto` is
  `clamp(round(sqrt(source lines)), 3, 25)`, capped at `max(3, Flow timeout //
  150 s)` so the run fits the Flow's time budget.
- **Default scope** is the Target's own RTL, excluding testbench, vendored, and
  generated files.
- **Reproducibility identity.** Every result records the scope files' hashes,
  the generator version, the pyslang version, the seed and the count, and on
  Verilator Targets the Verilator version, because it changes which Mutants are
  dropped. The generator version changes whenever the operator table, site
  rules, ID recipe or selection rule changes: the same inputs give the same
  Mutants only for the same version. Target build options stay outside the
  identity, as the scope hashes leave them out.

### Verdicts

- **Pass threshold.** By default `ceil(0.8 x Mutants scored)` must be detected,
  which equals 80 % of `--count` whenever `--count` Mutants were kept. The 80 %
  absorbs equivalent Mutants that the filters above miss. An explicit
  `--min-detected` is never changed, and the report notes that surviving
  Mutants may be equivalent.
- **Timeout.** Per test, `max(10 x baseline duration, 30 s)`, capped at 300 s,
  because the flow has no simulation-cycle limit to count. A timeout counts as
  detected and is reported separately as "detected (timeout)". An inconclusive
  verdict ends the run with an infrastructure error instead of scoring.

## Rejected alternatives

- **YosysHQ MCY / Yosys `mutate`.** It mutates the synthesized netlist, so tests
  run on a netlist (slower, with different X and initialization behavior) and
  Mutants map poorly to source lines. Its Mutant list reshuffles after any RTL
  edit or Yosys upgrade, even with the same seed, and its runner duplicates
  Booley's.
- **AVL `avl-mutation-testing`.** Its position-based IDs renumber on any edit,
  and it owns a cocotb + Verilator flow while Booley also supports Icarus and
  classic Targets. Its operator list stays a reference; vendoring it was
  rejected.
- **Keeping the LLM creator behind a flag.** Two generators to maintain, and the
  LLM's results are not reproducible. Its steering, mutation lock and model
  setting were removed without a deprecation release; generation is a pure
  function of its inputs, so there is nothing to lock.
- **Per-Mutant waivers and a bounded SAT equivalence filter.** The 80 %
  threshold needs no new machinery or waiver workflow.

## Consequences

- Mutation scores are reproducible and cost no model tokens. Specialist Source
  Isolation does not concern mutation testing: no LLM reads the sources.
