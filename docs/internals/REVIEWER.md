# Reviewer evidence contract

Reviewer runs in both Interactive Mode and Ticket Mode. Ordinary Interactive
reviews have no staged Ticket: findings use the supplied specification,
steering, or concrete code behavior as their scope anchor. Ticket-bound reviews
use the staged Ticket and accepted decisions. The shared Reviewer prompt owns
disposition and output instructions; category guides contain review checklists.

## Validation and dispositions

Reviewer validates the output schema and explicit source membership. Ticket
clauses and Project policy inform the agent; phrase matching and Ticket headings
never discard or rewrite valid dispositions. In-scope `current` findings can
make a Criterion unmet. `advisory`, `deferred`, and `out_of_scope` findings remain
observations. A `_done` Criterion records advisory observations; current
corrective findings require `_clean`. `_clean` requires current findings to be
verified fixed or explicitly waived with user-visible justification.

Filtered source proposals and malformed canonical, `ReportFindings` mirror,
and verification rows are separate non-gating audit evidence. Mixed valid and
invalid initial output keeps valid findings; all-invalid output or missing JSON
is a Specialist error. Invalid verification dispositions keep findings pending.
`FIXED` requires evidence and `WAIVED` requires justification.

## Publication and history

Review results have immutable JSON evidence under `reviewer-evidence/` in the
directory resolved by `booley.runtime.project_dir`, including Interactive Mode
and failures after input validation. Dry runs and input-validation failures do
not publish review evidence. Results include the path in `audit_evidence` and
`artifacts.reviewer_evidence`. Evidence is published atomically before a passing
Criterion is recorded.

Audit rows retain proposal ordinals, attempts, and phases within one contract.
A contract change archives the previous receipt and starts new audit history,
preserving open obligations and explicit disposition provenance. Historical
resolutions remain inspectable, but an active pending finding takes precedence
over any historical resolution with the same identity in normalized views.
Old automatic policy exclusions are historical only.

Live receipts predating the filtering revision require fresh discovery.
Historical accepted packages stay readable; previously discarded proposals
cannot be recovered. Audit evidence appears separately in review packages,
briefings, HTML explanations, and MCP results, without entering severity
counts, dispositions, acceptance predicates, or `review_report_required`.
