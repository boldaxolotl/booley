# RTL Mutation Testing Guide

Mutation testing uses compiler-isolated source variants. The creator is a
read-only design agent: it proposes exact replacements, while Booley validates
and materializes them. Neither side attempts to parse SystemVerilog or Verilog.
The project's configured compiler is the language authority.

## Workflow

1. Read every authorized RTL file and understand the datapath, control logic,
   and externally observable behavior. Do not read testbench sources.
2. Select N single-point mutations that a reasonable testbench should detect.
3. Return each mutation as an exact source slice and replacement. Do not edit
   files, run commands, add selector muxes, or insert marker comments.
4. Booley checks that the exact original bytes occur once on the declared line.
5. Booley builds and runs the untouched source as the baseline.
6. Booley applies one replacement alone, builds it in an independent directory,
   runs the Target's complete test suite, and restores the pristine bytes.

The creator proposes intent; exact byte matching and the compiler enforce the
mechanics. A proposal that does not anchor safely or compile is rejected and a
complete replacement proposal list is requested.

## Good Mutations

Prefer narrow changes with a clear path to an observable output:

- arithmetic or logical operator changes (`+` to `-`, `&` to `|`);
- comparison-boundary changes (`<` to `<=`, `==` to `!=`);
- constant or reset-value changes;
- condition or polarity changes;
- bit-select changes;
- FSM next-state changes;
- signal substitutions or branch swaps.

Structural mutations are allowed only when they remain one exact source
replacement. They do not need to fit a runtime-selection expression because
every variant is compiled independently. Keep the replacement as small and
auditable as possible.

Reject a mutation when:

- error correction or redundancy masks it before any observable output;
- it affects only performance when tests check functional correctness;
- it targets dead or unreachable code;
- it is equivalent for all legal inputs;
- it combines multiple independent faults;
- its exact source slice is needlessly broad.

The `detectability_argument` must explain how the replacement can corrupt an
observable result. Distribute proposals across scope files when the authorized
scope spans several meaningful modules.

## Exact Replacement Contract

`original_code` is not a pattern. Copy it verbatim from the source, preserving
whitespace, punctuation, capitalization, and newlines. `line` is the 1-based
line on which that exact slice begins. `mutated_code` contains only the bytes
that replace it.

Booley rejects a proposal if:

- its file is outside the authorized scope;
- its index is not unique and positive;
- the original or replacement is empty or identical;
- the exact original slice is missing or occurs more than once on the declared
  line;
- the isolated replacement does not compile;
- the simulator cannot produce a trustworthy verdict.

Do not add imports, packages, `MUT_ID`, plusarg readers, conditional muxes,
comments, or testbench changes. Do not edit any project file.

## Output Format

Return only a JSON object with a complete mutation list:

```json
{
  "mutations": [
    {
      "index": 1,
      "category": "operator_change",
      "file": "rtl/mod_a.sv",
      "line": 42,
      "original_code": "a + b",
      "mutated_code": "a - b",
      "detectability_argument": "Subtraction corrupts the output value for unequal operands"
    }
  ]
}
```

Indexes are 1-based and unique. On a retry, return a complete fresh JSON list;
do not edit source in place.

## Campaign Evidence

The completed campaign publishes:

- the pristine baseline log;
- one simulator log per mutation;
- the proposal specification and result report;
- one inspectable mutated source under `variants/mutant_<index>/...`;
- a manifest that links each result to its exact variant and source fingerprint.

A timed-out mutant counts as detected because the mutation can wedge the
design. Missing, malformed, skipped, or otherwise unresolved Cocotb results are
inconclusive and never count as a kill.
