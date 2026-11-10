# Generate Mutants deterministically in a Mutation Flow

Status: accepted (2026-11-07 triage of
[#2856](https://github.com/boldaxolotl/booley/issues/2856))

For mutation testing only, supersedes the `mutation_tester` key of ADR 0084's
`[models]` table ([ADR 0084](0084-run-each-specialist-on-the-agent-cli-its-model-implies.md))
and the `mutation_tester` Specialist form with its **Start fresh** option in
[ADR 0086](0086-launch-flows-and-specialists-from-the-dashboard.md).

## Context

Today the Mutation Tester is an LLM Specialist. A model proposes each mutation
as an exact text replacement; Booley applies it, builds, simulates, classifies
the result, and stores it in a mutation lock keyed on the scope files' hashes.
Booley gives the model only a prose guide to mutation categories
(`refs/rtl-mutation-testing.md`); it ships no executable mutation operators.
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
  source span, and original text, so IDs survive unrelated edits.
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
  (graded `sim`, `synth`, and `fpga` passes) is unchanged until #2937 amends
  it.
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
  rather than mutation scoring. Synopsys Certitude is the only commercial
  testbench mutation tool; it serves only as a feature benchmark.
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
- The default pass threshold loosens from all Mutants to 80 %.
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
