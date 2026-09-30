# Record Waiver Candidates for approval at review

Status: accepted (2026-09-30; amends ADR 0063)

Once the waiver directory became a protected acceptance input (#738), waiving
an unreachable Coverage Point during a Ticket meant a round trip: block, a
human writes approval files, return-to-draft, re-seal, requeue, recollect.
The human already reviews the Ticket, so the waiver decision now belongs in
that review. The Coverage Analyst records its screened Waiver Candidates for
the Ticket; a Coverage Criterion met only by counting them sends the Ticket
to review instead of blocking it; and the human accepts or rejects each
candidate when approving. Accepted candidates become Approved Waivers in the
same change as the RTL they justify.

## Decisions

- **Writer.** The Analyst's non-model code records candidates after
  screening. The model keeps only the read-only `coverage_evidence` tool from
  ADR 0063, which this ADR amends: the Analyst is no longer read-only as a
  whole, but its model still never writes. No Booley interface lets a
  Developer or Specialist create, edit, or remove a candidate.
- **Trust boundary.** The Developer Agent's shell shares the Sandbox with the
  record, so the record is checked, not trusted. At briefing and at approval,
  Booley re-derives every offered candidate from the evidence: Campaign
  manifest and point-store digests, Target, a zero-hit Coverage Point, and
  the source SHA-256 at the approved Ticket head. A candidate that fails is
  shown as invalid and not offered. The justification text cannot be
  verified and is labelled that way; the human decision is the authority.
  Rejections share this limit.
- **Store.** Candidates live in an ignored per-Ticket record,
  `tickets/waiver-candidates/<slug>.json`, beside the ADR 0065 state record
  and outside every Ticket worktree. Nothing tracked changes before approval.
- **Accumulation.** Candidates from every Analyst run on a Campaign add up.
  There is one entry per point: it keeps the latest justification and
  counts how many runs proposed it.
- **Binding.** Each candidate is bound to its Campaign, point-store digest,
  Target, Coverage Point, RTL source path, and source SHA-256. At approval,
  a candidate is promotable only if it comes from the Campaign behind the
  review verdict and its source still matches the approved Ticket head. The
  review shows stale candidates but does not offer them.
- **Two verdicts.** Gated evaluation computes a strict verdict (Approved
  Waiver Set only), which alone satisfies a Criterion, and a Provisional
  Coverage Verdict (Approved Waiver Set plus candidates). The Developer sees
  both and finishes once every mandatory Criterion is met at least
  provisionally. A Ticket met only provisionally goes to `review`. When the
  strict verdict already passes, candidates are listed as not needed and are
  not offered.
- **Approval.** Decisions are per candidate. The triage skill presents each
  one and asks the human; it then runs
  `booley board approve --accept-waivers ... --reject-waivers ...`, which is
  also the CLI surface. There is no interactive prompt. An undecided
  candidate makes approve fail, and nothing defaults to accept.
- **Promotion.** Before any change, approve computes the strict verdict
  with the Approved Waiver Set plus the accepted candidates. If that fails,
  approve records the rejections, promotes nothing, and exits with an error;
  the Ticket stays in review, where the human can fix it, reset it, or
  archive it. There is no review-to-blocked transition. If it passes,
  approve records the accepted approval records in the Acceptance Journal,
  records the strict coverage evidence they produce, and freezes acceptance
  for the unchanged Ticket heads. The Journal then promotes them inside its
  merge candidate: it appends the records to `<source>.toml` under the
  configured `coverage.waivers` anchor, writes one proof file per
  `unreachable` record under `proofs/<waiver-id>.md` (the Analyst's
  argument, cited evidence, binding, and review reference), and commits both
  as `chore(<slug>): approve coverage waivers` before the destination is
  fast-forwarded. The RTL and the waivers that justify it reach the
  destination in one publication, and the Ticket's own branch never
  changes. Accepting candidates requires merge.
- **Stamps.** `approved_by` is the Git identity of the host Project
  checkout. `approval_ref` defaults to `ticket:<slug>@<capture_sha>` when a
  Requested Review captured one, otherwise to
  `ticket:<slug>@<ticket-head-commit>`, and can be overridden by a flag.
- **Review proof.** An `unreachable` approval accepts `proof.kind` of
  `formal` or `review`. The schema (`kind`, `reference`, `sha256` of a file
  in the approval directory) is unchanged. The same rule applies to
  hand-written approval files.
- **Screening.** An `unreachable` candidate needs zero hits but no model
  proof reference; the review proof is written at promotion. A point with
  hits, or one that is missing or ambiguous, stays "investigate" and is not
  recorded.
- **Rejections.** A rejected candidate is recorded against the Ticket by
  Target, point, and source SHA-256, and later proposals matching it are
  filtered out. A changed source reopens the point. Rejections survive
  return-to-draft, which clears only the candidates, and are discarded when
  the Ticket closes. `board reset` clears the candidates and keeps the
  rejections, like return-to-draft.

## Considered options

- **Model-held write tool.** Rejected: it gives authority to an untrusted
  model for no gain over recording the host's screened output.
- **Pending files beside the approval directory.** Rejected: the Developer's
  worktree would see them, so they would need a new protection rule, and they
  would leave tracked churn behind.
- **All-or-nothing approval.** Rejected: rejecting a candidate the verdict
  never needed would fail approval for nothing.
- **Returning to blocked when strict re-evaluation fails.** Rejected:
  approve can predict the strict verdict before it changes anything, and
  review already has fix-here, reset, and archive exits. A new
  review-to-blocked transition would add a lifecycle edge for no gain.
- **Out-of-Sandbox candidate authority.** Rejected for now: no such
  authority exists, and re-deriving candidates from evidence at approval
  keeps the human decision sound without one.
- **Developer withdrawal of candidates.** Rejected: the Developer would be
  editing inputs to its own acceptance.
- **Promoting after merge.** Rejected: the destination would briefly hold RTL
  whose acceptance depends on waivers it does not contain.
- **Committing the waivers on the Ticket branch.** Rejected: the commit
  would move the Ticket heads after review, so the protected-input guards,
  the write-once Criteria Satisfaction Record, and the reviewed package would
  each need an exception for it. Promoting in the Journal's merge candidate
  gives the same atomic destination without any of them.
- **`unreachable` only with formal proof.** Rejected for now: Booley has no
  formal-tool support, so no candidate could ever qualify and every one would
  have to be relabelled.

## Consequences

`unreachable` no longer implies a formal proof; `proof.kind` says which. The
review briefing grows a candidate section, the approve command gains
waiver-decision flags, and the Analyst's output contract is unchanged. The
Ticket Board reconciliation must treat an unpromoted candidate record as
disposable state, just like runtime logs.
