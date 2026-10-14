# Make the Review Goal the whole review contract

A Reviewer should judge the Developer Agent's work independently. Caller-selected
scope, specification and steering made Goal Mode reviews depend on the Developer
Agent's choices. Contract changes restarted discovery and carried unchecked open
findings into the result (#1432). We replace that implicit state machine with a
Goal-owned review contract and explicit verification (#1507), superseding the
verify-before-restart approach in #1432 and the closed PR #1496.

A Review Goal declares exactly one `target` or `paths`. `target` selects its RTL
or TB filesets and dependency closure, using the same category file lists as
source fingerprinting. `paths` declares category-matching files relative to the
Goal worktree. Category, focus and verdict come from the Goal's review kind and
requirement; a spec review also names `spec`. Goal Mode discovery takes only
category and focus and refuses caller `scope`, `spec` and `steer`. Interactive
Mode retains those controls. The decisions file is no longer Reviewer input or
part of receipt identity in either mode.

`reviewer --verify` checks only the latest review call's open findings against
current source. It performs no discovery and adds no findings. Fixed, waived,
excluded and superseded findings are outside its input set. Every checked finding
is reported as fixed, waived or still present; verification passes when none
remains open and is refused when there are no open findings. The only review
option accepted alongside it is repeatable
`--waive <finding-id> "<justification>"`. The Reviewer decides whether each
justification holds. Unknown IDs and unsolicited Reviewer waivers are refused.
Worktree/report/transcript routing supplied by the execution adapter remains
execution plumbing, outside the review argument contract.

A passing verify meets a `clean` Goal at the verified file contents. Changes to
those files make it stale again. Verification retains the discovery requirements:
a changed spec, TB policy or Target file selection requires new discovery after
open findings are resolved. A `done` Goal is met by discovery completion;
verification updates finding dispositions without refreshing its completion
source evidence. Goal Mode resolves each open review before starting discovery
for another focus; otherwise a later review would make older open findings
unreachable through the latest-review-only verify command. Refusals point to
verify and waiver proposals. Interactive discovery replaces
the previous review's findings.

The restart flag, contract invalidation, finding carry-over and automatic
verification/rediscovery dispatch are removed. Existing verify budgets remain:
two attempts between category resets and three cumulative cycles. The cumulative
limit is reachable because category resets clear the attempt count but preserve
the total. Exhaustion preserves findings and requires a human decision through
the existing Goal Change or abandonment workflow; it never waives automatically.

Existing unscoped Review Goals must be replaced with scoped Goals through the
normal approved Goal Change mechanism. Existing receipts retain their finding
IDs; decisions-file fields are dropped when new review evidence is written.
