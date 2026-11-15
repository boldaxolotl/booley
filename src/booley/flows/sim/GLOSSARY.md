# Simulation Coverage glossary

This is the canonical vocabulary for measuring and evaluating RTL coverage from
Simulation Flow runs, for contributors and agents working on coverage
collection, waivers, and the Coverage Analyst. Shared terms such as
**Simulation Record**, **Target**, and **Specialist** are
defined in the [shared glossary](../../../../docs/GLOSSARY.md).

## Language

**Coverage Record**:
One durable native RTL coverage record for exactly one Target in one Simulation Flow invocation; a [Simulation Record](../../../../docs/GLOSSARY.md) may contain a Coverage Record. Code, messages, persisted formats, and docs outside the glossaries still say "Coverage Campaign" until the rename in [#2711](https://github.com/boldaxolotl/booley/issues/2711) lands; until then, a lowercase "coverage record" in Criteria or Verilator messages means a Criteria coverage template item or a native counter line, not this term.
_Avoid_: Coverage Campaign, coverage collection, latest coverage, waveform score

**Coverage Point**:
One losslessly identified native RTL coverage measurement point.
_Avoid_: signal score, source-line identity

**Coverage Window**:
The simulation interval in which native counters contribute to a Coverage Record.
_Avoid_: waveform slice

**Coverage Criterion**:
A Target-bound coverage evaluation policy combining native metric thresholds with an exact test suite.
_Avoid_: Analyst score, inferred coverage goal

**Approved Waiver Set**:
The immutable project-wide set of human-approved Target-and-Coverage-Point exclusions.
_Avoid_: cached LLM waivers

**Waiver Candidate**:
A proposed exclusion the Coverage Analyst screens and reports for one Coverage Record; it becomes part of the Approved Waiver Set only when a human approves it.
_Avoid_: approved waiver, automatic exclusion, pending waiver

**Provisional Coverage Verdict**:
A Coverage Criterion evaluation that also counts a Coverage Record's Waiver Candidates; it informs human waiver decisions but never satisfies the Criterion.
_Avoid_: soft pass, pending verdict

**Coverage Analyst**:
A Specialist that explains one Coverage Record and may propose Waiver Candidates; its model only reads evidence.
_Avoid_: coverage scorer, waveform coverage engine
