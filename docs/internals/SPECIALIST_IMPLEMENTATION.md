# Built-in Specialist implementation

This page records the evidence contracts of the built-in Reviewer and
[Coverage Analyst](../../src/booley/flows/sim/GLOSSARY.md): what each one
validates, publishes, and refuses, and why. It is for contributors and agents
who change `booley.specialists.reviewer` or the Coverage Analyst modules.
The `Specialist` base class, discovery, and the extension contract are in
[MCP-TOOLS.md](MCP-TOOLS.md); how users run Specialists is in
[SPECIALISTS.md](../user/SPECIALISTS.md); the Simulation side of coverage
analysis is in
[FLOW_IMPLEMENTATION_SIM.md](FLOW_IMPLEMENTATION_SIM.md#coverage-analyst-input).

## Reviewer

Code: `booley/specialists/reviewer.py`, with receipts and dispositions in
`booley/evidence/review_receipt.py` and `review_dispositions.py`.

The Reviewer runs in Interactive Mode and Goal Mode. Each finding is anchored
to the supplied specification, steering text, or concrete code behavior; a
spec-review Goal names its specification file explicitly. The shared Reviewer
prompt owns the disposition and output instructions, and the category guides
hold only review checklists, so every category reports in the same shape.

### Validation and dispositions

The Reviewer validates the output schema and checks that each finding cites a
file inside the review's source scope. Specifications and Project policy go
into the prompt; Booley does not filter findings by their wording. Two path
rules do change a finding automatically:

- a finding outside the source scope moves to the audit as filtered (reason
  `source_scope`);
- outside Goal Mode, a finding in a firmware file becomes `out_of_scope`,
  because firmware is read-only context for every Reviewer (see
  [Specialist Source Isolation](../GLOSSARY.md#flows-eda-tools-and-mcp-tools)).

Dispositions decide what a finding blocks. An in-scope `current` finding can
leave a `_clean` Criterion unmet; `advisory`, `deferred`, and `out_of_scope`
findings stay observations. A `_done` Criterion is met once the review runs,
whatever the dispositions, and keeps its findings. In Goal Mode a `done` Goal
may Finish with open findings, which the Review Package shows; they do not
require a human sign-off. A `clean` Goal needs every current finding verified
fixed or waived with a justification the user can read. Merging the Goal
Branch afterwards is ordinary Git.

Malformed rows are kept apart from findings so one bad row cannot erase good
ones. Filtered source proposals and malformed canonical, `ReportFindings`
mirror, and verification rows become separate audit evidence that never
gates. Output that mixes valid and invalid findings keeps the valid ones;
output with no valid finding, or no JSON at all, is a Specialist error (exit
2). An invalid verification disposition keeps its finding pending: `FIXED`
needs evidence and `WAIVED` needs a justification.

### Evidence file

Each review run writes one JSON evidence file,
`reviewer-evidence/<run-id>.json` in the Main Booley Directory, in Interactive
Mode too. The result and the recorded Criterion detail carry the same path in
`audit_evidence` and `artifacts.reviewer_evidence`. Booley writes the file
atomically before it records a passing Criterion, so no Criterion can point at
evidence that does not exist; an error later in the run rewrites the same file
with the final error. A dry run, and a run that fails before the review starts
(invalid input, discarded evidence, missing scope files), writes none.

Filtered and rejected audit rows appear separately in MCP results and in the
recorded Criterion detail; they never enter severity counts, dispositions, or
the checks that decide whether a Criterion is met.

### Receipts and audit history

Audit rows keep their proposal ordinals, attempts, and phases within one
review contract. A contract change starts new audit history but keeps open
findings and the provenance of explicit dispositions. Old resolutions stay
inspectable, but an active pending finding wins over any older resolution
with the same identity.

A stored receipt is valid only for the current filtering rules:
`FILTERING_SEMANTICS_REVISION` in `review_receipt.py`. Bump it whenever you
change which proposals are filtered; older receipts then need a fresh review,
because proposals filtered under the old rules were never kept.

## Coverage Analyst

Code: `booley/specialists/coverage_analyst.py` (the wrapper),
`coverage_analysis.py` (the analysis), `coverage_candidate_recording.py`, and
`booley/flows/sim/coverage_evidence.py` (the evidence session). The design
reasons are in
[ADR 0063](../adr/0063-bind-coverage-analysis-to-budgeted-evidence.md) and
[ADR 0066](../adr/0066-record-waiver-candidates-for-approval-at-review.md).

### Inputs and output

`coverage_analyst` takes a required `campaign`, the exact canonical
`coverage.json` path of one Coverage Campaign, and an optional
`instruction`. For a Campaign stored as `booley.coverage-campaign/v5` it
returns a `booley.coverage-analysis/v2` report that cites the Campaign
manifest and the recorded point-store digest as observed evidence; an older
Campaign gets `booley.coverage-analysis/v1` with the full encoded Campaign.
The report holds the observed evidence, the model's hypotheses and
recommendations, explicit limitations, source-access status, screened
Waiver Candidates, and the exact evidence it was allowed to retrieve.

The Analyst is advisory: it never marks a Goal met or changes Goal evidence.
Invalid input, or model output that is malformed or incomplete, is an
execution error. A valid report succeeds even when its Campaign records a
simulation failure or a coverage miss, because reporting that miss is its
job.

Before calling the model, the wrapper checks the canonical invocation and
Target identity, the complete manifest and point-store pair (schema, format,
size ceiling, every point), and a matching completed Simulation projection.
`analyze_coverage_campaign(campaign, sources, instruction)` is the analysis
itself; `CoverageAnalyzer` takes the model call as a constructor argument, so
tests replace only that external boundary.
`coverage_analyst(campaign: Path, instruction="")` is the default public
composition. Sources reach the model as an immutable, fingerprint-verified
snapshot, never through file tools.

### Waiver Candidates

In a Goal-bound run, the wrapper, not the model, records candidates screened
`ready_for_human_review` in the Goal Record's `waiver-candidates.json` (owned by
`flows.sim.waiver_candidates`). It adds a `waiver_candidate_record` detail of
the form `{status: "recorded", recorded, filtered_by_rejection,
candidate_ids, verdicts: {strict, provisional, candidates}}`, and after the
first report line (`Coverage advisory: <recommendation> (<eligibility>,
<source_access>)`) prints `Waiver Candidates recorded N (filtered by rejection
M); coverage strict X · provisional Y`. A recording failure gives
`{status: "failed", reason}` and the line `Waiver Candidates not recorded:
<reason>` without failing the analysis. Outside Goal Mode no candidates are
recorded.

Stored evaluation maps directly to the closure recommendation: `pass` →
`coverage_ready`, `fail` → `coverage_not_ready`, `blocked` →
`coverage_evidence_blocked`, and `not_requested` →
`ungated_no_recommendation`. Every candidate names exact points and stays
`not_approved`. Screening marks it:

- `forbidden`: a non-RTL or unscored point, or an unknown, duplicate, or
  invalid-reason candidate;
- `investigate`: missing source verification or evidence, or an `unreachable`
  point with observed hits;
- `ready_for_human_review`: everything else. The human accepts or rejects it
  through a coverage-waiver Goal Change Proposal.

An `unreachable` candidate needs no model proof reference; approval writes a
`review` proof. The model never writes the waiver record.

### Evidence access

The model prompt contains only a compact Campaign reference. One isolated read-only
`coverage_evidence` tool exposes `overview`, filtered and cursor-paged `points`,
verified `source` excerpts, and cursor-paged `zero_point_sources` (sorted paths,
`limit` 1–100, optional `cursor`). The latter returns sources with no native
measurement records, independent of hits, point eligibility and waivers. It is
available in report-only mode, charges normal evidence byte budgets, and records
returned source counts with empty point IDs. Overview and its minimal fallback
keep the total source count and a pagination hint. These findings are advisory;
they do not prove a source is dead or uninstantiated. Model-facing point records
use short `point_ref` values (`point:N`) instead of the Campaign's long opaque IDs.
A `point_ref` becomes usable only after its exact point was delivered within the
response and cumulative byte budgets; later `points` or `source` queries may pass
delivered values as `point_refs`. The host resolves them back to exact Campaign
IDs before it publishes the report. Point records include their eligible,
unscored, or waived disposition and Approved Waiver provenance. The host
deep-loads the complete manifest and point store before paging or resolving
references. No other MCP tool is visible to this model. The session keeps
references to the immutable Coverage Points rather than encoded copies, uses an
exact-ID index, derives overview counts once, and caches at most one filtered
match set for consecutive pages.
Changing filters replaces that cache; only returned page records are encoded.

Short references and delivered-only resolution keep the model from inventing
or guessing point IDs, and the byte budgets keep a large Campaign from
flooding its context.

### Capability isolation

The model can only read the bound evidence. A Codex call uses a private
exact-model catalog that removes the model's shell, patch, and search tools,
with startup settings that turn off other tools, apps, plugins, and
subagents. A Claude call gets an empty built-in tool list. Both run without
Project skills, in a private temporary working directory that holds only
Booley's verified-source and audit files (plus Codex's private settings), with
the bound evidence MCP server as their only MCP server.
