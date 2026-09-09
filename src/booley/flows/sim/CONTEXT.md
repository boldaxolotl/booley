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
A Target-bound acceptance policy combining native metric thresholds with an exact test suite.
_Avoid_: Analyst score, inferred coverage goal

**Approved Waiver Set**:
The immutable project-wide set of human-approved Target-and-Coverage-Point exclusions.
_Avoid_: cached LLM waivers

**Waiver Candidate**:
An advisory proposed exclusion produced for human investigation or review.
_Avoid_: approved waiver, automatic exclusion

**Coverage Analyst**:
A read-only Specialist that explains one Coverage Campaign and may propose Waiver Candidates.
_Avoid_: coverage scorer, waveform coverage engine
