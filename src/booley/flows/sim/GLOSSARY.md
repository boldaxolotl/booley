# Simulation Coverage glossary

This is the canonical vocabulary for measuring and evaluating RTL coverage from
Simulation Flow runs.

## Language

**Coverage Campaign**:
One durable coverage record for exactly one Target and one Simulation Flow invocation.
_Avoid_: latest coverage, waveform score

**Coverage Point**:
One losslessly identified native RTL coverage measurement point.
_Avoid_: signal score, source-line identity

**Coverage Window**:
The simulation interval in which native counters contribute to a Coverage Campaign.
_Avoid_: waveform slice

**Coverage Criterion**:
A Target-bound coverage evaluation policy combining native metric thresholds with an exact test suite; in Goal Mode it evaluates a coverage Goal.
_Avoid_: Analyst score, inferred coverage goal

**Approved Waiver Set**:
The immutable project-wide set of human-approved Target-and-Coverage-Point exclusions.
_Avoid_: cached LLM waivers

**Waiver Candidate**:
A proposed exclusion recorded for one Goal Record; it becomes part of the Approved Waiver Set only when a human approves the corresponding Goal Change Proposal.
_Avoid_: approved waiver, automatic exclusion, pending waiver

**Provisional Coverage Verdict**:
A Coverage Criterion evaluation that also counts a Goal Record's Waiver Candidates; it informs human waiver decisions but never satisfies a Goal.
_Avoid_: soft pass, pending verdict

**Coverage Analyst**:
A Specialist that explains one Coverage Campaign and may record Waiver Candidates for its Goal Record; its model only reads evidence.
_Avoid_: coverage scorer, waveform coverage engine
