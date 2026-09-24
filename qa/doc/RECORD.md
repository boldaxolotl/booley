# Record a Scenario Run

Use the version-2 files and fields in [Format](FORMAT.md). Append every Check attempt
to `check-results.jsonl` immediately:

| Status | Meaning |
|---|---|
| `pass` | Trustworthy evidence satisfies the declared expectation |
| `fail` | Trustworthy evidence contradicts it |
| `blocked` | The Check lacks trustworthy evidence because of a failed prerequisite, timeout, infrastructure/operator error, or invalid execution |
| `unavailable` | A pre-run assessment proved an applicable declared capability absent |

A capability lost after admission is `fail` or `blocked`. Exclusions are fixed by the
Configured Scenario and are not passes. Preserve every trustworthy failed attempt;
later success does not erase it.

For an interrupted attempt, classify the evidence boundary rather than the operator's
expectation:

- A valid stimulus followed by a completed product rejection or other trustworthy
  contradiction may be `fail`, even when later operator work is interrupted.
- An operator stop, loss of control, or declared timeout before a trustworthy behavioral
  result is `blocked`.
- Absence of an expected output after an operator stop is not contradictory evidence.
- A missing prompt or payload hash required by the Check makes the evidence incomplete,
  even when the Scenario Operator believes the full text was supplied.

Retain the last declared milestone and the terminal-boundary category with the Check
evidence. These categories do not change the four statuses above. Apply the execution
continuation rules in [Execute](EXECUTE.md); do not infer interruption semantics from
free-form prose or rewrite an earlier trustworthy failure as blocked.

For the selected borrowed-resource preservation Checks (`cleanup.preserve-borrowed`
and `cleanup-preservation`), a `pass` Check Result also records
`borrowed_preservation.scoped_resource_identities`, `setup_evidence_refs`, and
`end_evidence_refs`. Each list must be nonempty, and both evidence lists must be
included in the Check Result's `evidence_refs`. An `unavailable` result instead
records `pre_run_absence_assessment` and `pre_run_absence_evidence_refs` under
`borrowed_preservation`; those evidence paths must also appear in the Check Result
and in `run.json` admission evidence. Use `blocked` when the comparison or pre-run
absence assessment lacks trustworthy evidence. Older sealed runs remain readable.

Candidate creation, operator-only CLI installation, and Host Bootstrap during
admission are preparation provenance, not Check Results. Retain their verified
pre/post evidence under `evidence/admission/` when admission succeeds and link it from
`run.json`. A later Scenario Check performs and evidences its own declared stimulus.
Before admission mutates state, create the records defined by
[Format](FORMAT.md#admission-attempt-records), ledger planned resources, and update
identities and dispositions atomically.

Append evidence-backed corrections before the run is sealed. Record explicit causes
only when one exact prior Check Result caused the current result. Record integrity
concerns, deviations, and conflicts in their structured fields so triage can enumerate
them deterministically.

Submit each proposed Check Result as a JSON object through the pre-append recorder:

```sh
python3 qa/record_check.py <run-root> <result.json> --suite-root <frozen-suite-root>
```

The recorder checks the schema, run and Check identity, earlier result links,
correction chain, direct Scenario prerequisite direction, and every evidence path
before appending. `evidence_refs`, deviation evidence, borrowed-preservation setup,
end, and pre-run-absence evidence, and `recovery_refs` all name evidence paths. A
rejected candidate remains outside `check-results.jsonl`; fix the candidate and
retry. Do not edit or replace an appended row. To correct an accepted recording
error before sealing, append a new Check Result with a new `check_result_id`, the
same `check_id`, a higher `attempt`, `corrects_result_id` naming the earlier row,
and `correction-chain` in `review_reasons`. Retain the earlier row and any
evidence it cites. A correction may name any earlier compatible row, including a
superseded ancestor. A **surviving head** is a Check Result not named by a later
row's `corrects_result_id`; branches and independent attempts can therefore leave
several surviving heads. Schema, evidence, identity, ordering, and link validation
apply to every historical row. Effective semantic claims, including required
borrowed-preservation claims, apply to every surviving head. A new product execution
is a new attempt, not a clerical rewrite. After sealing, follow [Format](FORMAT.md)
for human triage corrections.

Append unexpected behavior, incidental facts, friction, impressions, and wins to
`observations.jsonl` without classifying them as Findings. Preserve the original text
and link any producing Step, Check Result, cause, correction, and evidence. The Human
Maintainer owns their later Triage Dispositions.

Bind identities created by a Step to that Step and its evidence. Publish every
cleanup-ledger change as a complete candidate document:

```sh
python3 qa/record_cleanup.py <run-root> <candidate.json>
```

The recorder validates the entire candidate and replaces the unsealed run's ledger
under a bounded lock. It does not accept a row patch. Before creating an owned
resource, publish its planned string identity; when the exact identity is unknowable
in advance, publish the exact identity immediately after acquisition and before
dependent work. The only supported resource fields for new writes are `identity`
(required and nonblank), `actual_disposition`, `active_authority_possible`,
`safe_shutdown_evidence_refs`, and `cleanup_reason`. Null, empty, or whitespace-only
`cleanup_reason` is omitted. `updated_at` and every other undeclared resource field
are unsupported. Legacy sealed rows remain readable but are not valid new writes.

An evidence reference is publishable only after its destination exists as a regular
file beneath the Scenario Run's `evidence/` directory. For evidence produced outside
the run, copy the source bytes to a deterministic run-owned evidence path, record the
source and destination digests (or equivalent byte-identity proof), finish the file
under the Scenario's immutable-evidence procedure, and only then submit the Check
Result or cleanup-ledger candidate. The recorders reject outside, traversing, missing,
directory, and escaping-symlink paths and never copy or rewrite external evidence.

Keep immutable evidence outside mutable Project state; retained workspace state cannot
be the only evidence for a claim. The Scenario Operator does not create
`findings.jsonl`, decide a Scenario Run Outcome, or calculate Qualification.
