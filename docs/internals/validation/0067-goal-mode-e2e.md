# Goal Mode live validation (ADR 0067)

Goal Mode remains gated by `BOOLEY_GOAL_MODE_PREVIEW=1`. These checks use
isolated clones and linked worktrees of the public PicoRV32 example inside a
Booley Sandbox. They do not alter the installed framework or the user Project.
Source snapshots are selected through `PYTHONPATH`.

## Phase 2 entry, repeated during Phase 3c

On 7 October 2026, entry checks passed through a real
`mcp.Client(server, mode="2026-07-28")` against main after PR #1301 and
against the Phase 3c source:

- Entry without `work_dir` is refused.
- Entry in the primary checkout is refused.
- Entry in a dirty linked worktree is refused.
- Entry in a clean linked worktree resolves declared lint and simulation
  Targets, persists an active record, and stores working and HEAD protected
  input digests after final revalidation.
- A second entry in an occupied worktree is refused.

The test archives only its own records between runs. It does not exercise
crash recovery at each durable boundary; those cases are covered by the
entry regression tests.

## Phase 3c evidence and routing

The full live run passed on 7 October 2026 through the modern MCP wire.
The server uses Interactive mode and the Sandbox proxy environment. Checks:

- The preview catalog exposes entry, status, built-in Flows, Reviewer, and a
  Project custom tool through the modern MCP wire.
- Declared Goals initially render unmet without evidence.
- Flow and status calls without `work_dir` are refused while a Goal is active.
- Real Verilator lint and Icarus simulation publish met lint and simulation
  evidence; simulation executes the two named tests.
- A Project custom tool runs Icarus elaboration and publishes a met Goal. Its
  fingerprint uses the declared Target when the tool omits `source_target`.
- Jobs persist under their bound record with `binding` and `work_dir`. A
  separate run forces detached simulation, polls without `work_dir`, checks
  the terminal Job at the binding root, and polls it after reconstructing
  the server.
- Newly produced evidence reads fresh. An RTL edit makes it stale, and
  restoring the exact source bytes restores freshness.
- A separate `python -m booley.mcp.server --transport http` process exposes
  status rules and executes Goal lint successfully over HTTP.
- Public `booley_report` accepts the Goal worktree and recovers its completed
  simulation report over the modern MCP wire. Omitted worktrees are refused;
  regression coverage separates two Goal roots from an older Interactive report
  and scopes the available-endpoint hints to the selected record.

- Astra high Reviewer completes an advisory code-style review with two open
  findings. Its `done` Goal becomes met, and all lint, simulation, and
  elaboration evidence stays met and fresh.
- CLI status renders short and long views inside the Goal worktree and lists
  the active record from outside it. CLI and MCP status from a worktree
  subdirectory also keep fresh evidence met. Status rules return the agent rules text.
- With preview disabled, Goal tools are hidden and uncallable, and ordinary
  lint still passes.

The Reviewer regression tests exercise both `done` and `clean` policy with a
provider stub: Goal freshness is read without rewriting other producers'
evidence. Competing-receipt tests cover a waiting Reviewer with an older
loaded receipt and a receipt replaced after its freshness check. An obsolete
loaded receipt triggers a real review; replay never saves over the newer
Criterion. A real MCP-to-CLI replay additionally preserves all Criterion bytes.
The regressions failed before the fixes and pass afterward. Multi-root ambiguity,
cancellation, reconciliation, discarded evidence, corrupted state, and the
protected-input/specification/checkout checks are additionally covered by the
regression suite.

Phase 3c verification: **20,682 passed, 152 skipped** in the complete Python
profile; complete Ruff check and format gates pass; configured Pyright passes.
New modules pass strict checking. Touched legacy modules introduce zero strict
diagnostics against main (90 pre-existing diagnostics removed). An additional
Python 3.11 compatibility run passes **895 tests**, including Goal, Reviewer,
Job, CLI, and Ticket acceptance-ledger golden coverage. CI-recovery coverage
adds a separate Python 3.11 run of **84 tests** for the runner, formatter,
candidate routing, adapter policy, and MCP routing. Changed-line coverage is
**96%** against the required 90%. The Reviewer freshness regression has a
120-second budget after measuring 31.9 seconds on Windows, following the
repository timeout-headroom sizing rule.

## Phase 3c merged-source exit

After PR #1302 merged at `9d9e861a3aa35aaa930bd0c9f1689a3954fa878f`, the
owner repeated the live Phase 2/3 exit: **18/18 checks passed**. The actual
`gpt-6-astra` Reviewer at high effort completed in 124.5 seconds with two minor
findings; its done Goal was met and all EDA evidence remained fresh. Ticket
readiness and the demo contract passed with preview off. Supplemental checks
passed for CLI/subdirectory replay (4), public reports (2), actual module HTTP
(4), and forced detached simulation/poll/server reconstruction (4).

## Phase 4 approved changes

On 7 October 2026, the owner completed **13 C10 assertions** through the real
preview-gated modern MCP application in the isolated PicoRV32 Sandbox. The
source snapshot aggregate was
`017a6595a92c1a4a3b77cc43d4319cc94158b65f0f8d797ba96039f3044f1e66`.

- A real successful Icarus run measured 481,039 cycles against a maximum of 1.
  Public exact-ID approval relaxed the maximum to 600,000. The cycle Goal and
  unchanged simulation sibling became met/fresh. Derived evidence retained
  original source stamps and ledger producer time and linked the proposal,
  ChangeEntry and original evidence. Only the original simulation invocation
  and Job existed: no simulator rerun or Git commit occurred.
- Real coverage was refreshed once under Phase 4 to capture the newly required
  approval-policy identity, and inert advisory candidates were rebound to that
  exact Campaign. Public exact-ID branch approval installed files in the
  configured RTL anchor; a distinct toggle candidate was rejected. The next
  real Verilator run consumed the approval: branch coverage rose from 50%
  (2 eligible, 0 waived) to 100% (1 eligible, 1 waived). Toggle coverage stayed
  58.33% with 0 waived. The coverage Goal correctly remained unmet because its
  toggle floor was 100%. Files were installed without auto-commit. Only the
  approved proposal produced a ChangeEntry; rejection persisted separately.

These fixture decisions used the agent-recorded fallback and quoted the
standing verification instruction. They prove application and rejection of
exact saved proposals, not a new human-rendered form or authenticated human
identity. Phase 0 human UI evidence remains in the elicitation validation
record. Regression tests separately drive real modern `mcp.Client` forms,
sealed request-state retries, restart/reissue, and fallback through the actual
application and transport boundary.

Initial Phase 4 verification at `b2f755d`: **20,742 passed, 152 skipped** in the complete Python
profile with coverage collection. A supplemental strict numeric-coverage
relaxation regression passed against a reloaded V4 Campaign without installing
waiver files. Complete-patch changed-line coverage is **90.77%** against the
required 90%. Complete source/tests and whole-repository Ruff checks and format
pass, as does configured Pyright with every new module strict. The same 18
legacy Python modules were checked against main: **zero introduced strict
diagnostics**, with one pre-existing diagnostic removed (340 to 339).
Python 3.11 compatibility passed **2,106 tests, 1 skipped**, plus **37**
acceptance-ledger and Ticket golden tests.

After first-pass review fixes, the complete Python profile passed **20,759
tests, 152 skipped** with coverage. The final decision-authority recovery guard
then passed **82 focused tests** with coverage appended; complete-patch
changed-line coverage is **92.04%** (1,243 measured lines, 99 uncovered). These
regressions cover recovery traversal and exact destination authority, rejected
transactions exceeding candidate-storage authority, policy changes before,
during, and after publication/derivation checks, and public relaxation from
18/20 points at a 100% floor to 90%, followed by a waiver yielding 18/19 points
under the retained 90% floor. Campaigns are persisted and reloaded through the
real V4 reference loader.

Complete source/tests and whole-repository Ruff checks and formatting passed.
Configured Pyright includes the new strict semantic policy reader and reports
zero diagnostics across 84 files. The same **21 legacy Python files** were
strictly checked on main and the fixed source: **zero introduced diagnostics**,
one removed (348 to 347). This expands the initial baseline to include the two
Goal adapter composition callers and the shared strict coverage re-evaluation
helper. Python 3.11 compatibility passed **2,123 tests, 1 skipped**, followed by
the same **82** focused recovery tests after the final guard. The earlier live
measurements above describe their frozen source snapshot; these later checks
are local regression and compatibility evidence.

The initial `b2f755d` CI attempt passed coverage, Ubuntu Python 3.11/3.14,
compatibility checks, and five Windows Python 3.14 shards. The remaining
Windows shard hit the job's 15-minute ceiling after **3,387 passed, 93 skipped**;
no test assertion or timeout-headroom failure was reported. All six timing
artifacts measured **544.928 seconds** for the original **62 Phase 4 tests**,
versus 62 seconds in the previous model. Only those measured entries were
added to the Windows timing model; other entries, the default, and budgets
were preserved. Reassignment of the same measured test set reduces the
largest summed test duration from 2,356.3 to 2,174.6 seconds. This is a model
comparison, not a new passing Windows run. The 18 CI sharding/model tests
passed. Later fix regressions retain the model's default until measured.

The owner then confirmed the fixed runtime source in the real Sandbox, using
snapshot aggregate
`fd3e8724e3e07476526a71f8a3f24e942e5d82f0be7df701c864e403e2ebe86e`
(751 source files). Public cycle status remained **2/2 met and fresh**, with
unchanged evidence bytes and the original single Job. One new real Verilator
coverage invocation (`sim/4`) consumed the existing branch approval: **100%
branch coverage, 1 waived point**; toggle stayed **58.33%, 0 waived**. The
producer's semantic policy digest matched the new strict reader, and its
checked raw policy snapshot had no error. Original proposal/audit/approval
files and HEAD were unchanged. No new approval or reset was performed.
Evidence is retained in `/tmp/cr-p4/live-fixed-confirmation-v3.json` and its
log, with the owner report appended to `/tmp/cr-p4/live-report.md`.

On the same frozen Phase 4 source, the owner also repeated the released
PicoRV32 Ticket readiness and pinned demo-contract checks with the preview
switch entirely unset: both passed.

Owner evidence is retained locally in `/tmp/cr-p4/live-report.md`,
`live-changes-v2.json`, `live-changes-v2.log`, and `live-source-v2.json`. The
criterion update time is the decision time; the immutable ledger's
`recorded_at` remains the original producer time.

Round 1 identified that recovery did not bind the complete prepared transaction
to separate durable decision authority. The fix records a canonical digest of
the complete transaction in proposal lifecycle metadata atomically with the
decision. Recovery validates that binding, required decision-specific effects,
the full ChangeEntry, and any existing intent before publishing or finalizing.
The captured transaction body contains no self-referential digest, and recovery
never re-derives evidence from current sources. Unresolved legacy captures
without this authority fail closed; already terminal legacy proposals and
existing ChangeEntry/v1 records remain readable.

Status also compares Criterion verdict, parameters, and detail with its selected
immutable observation, including unmet observations. Parameterless None and
empty maps remain equivalent. While running the affected Reviewer tests, the
coder found a related producer projection discrepancy: later report enrichment
rewrote the Goal receipt's audit path after immutable publication. The narrow
Goal fix detaches result detail before report attachment and keeps the persisted
receipt unchanged, while retaining the result/report artifact link. This was a
discovery during the fix, not an additional finding from the Round 1 reviewer.
The Ticket enrichment path remains covered by compatibility and golden tests.

Round 1 fix verification passed **972 focused tests**, including **46** new
transaction-integrity, legacy-readability, projection-consistency, and Reviewer
aliasing regressions. The complete Python profile passed **20,809 tests,
152 skipped** with coverage collected once. Complete-patch changed-line coverage
is **91.99%** (1,299 measured lines, 104 uncovered) against main, above the
required 90%. Full source/tests and whole-repository Ruff checks and format
passed. Configured Pyright reports zero diagnostics across 84 files. The same
21 legacy files, including Reviewer, remain at **348 to 347 diagnostics**,
with zero introduced. Python 3.11 compatibility passed **2,182 tests, 1 skipped**,
including Reviewer and Ticket acceptance-ledger goldens, while importing the
Phase 4 worktree source. The new regression fixture initially used read-only
endpoint properties incorrectly; those test setup errors were corrected before
the passing focused and broad gates.

The owner confirmed the frozen Round 1 fix source in the real Sandbox using
snapshot aggregate
`87a20ac49ffb1440c0f558fe420d8a6cf1b2faceb459a1415ecfceddcc96b320`
(751 source files). Retained public cycle status remained **2/2 met and fresh**,
with unchanged Goal bytes and only the original Job. One new real Verilator
invocation (`sim/5`) consumed the installed branch approval: **100% branch
coverage, 1 waived point**; toggle remained **58.33%, 0 waived**. The producer
semantic policy digest matched the strict reader and the checked raw snapshot
passed. Policy, proposals, audit files, and HEAD were unchanged. Existing
terminal applied/rejected metadata remained readable; no new decisions were
recorded. Evidence is retained in `/tmp/cr-p4/live-final-confirmation-v4.json`,
its log, and `/tmp/cr-p4/live-source-v4.json`, with the owner report appended to
`/tmp/cr-p4/live-report.md`.

## Released Ticket demo

With the preview switch unset, the existing CI-owned demo Ticket fixture passes
`booley run --ticket <fixture slug> --check-ready` and the PicoRV32 demo
contract validator. The isolated upstream checkout is pinned to
`a473fc8fca393771d83b0ffcf0b14db3393339d8`; its public Project is pinned to
`da79489482a7bed69e275ba2c46358ea6636af4d`, as specified by
`.github/contracts/picorv32-demo.toml`. Real lint reports zero warnings, and
real simulation passes both `main` and `axi`. The verifier's explicit checkout
paths point to the isolated fixture rather than the Sandbox's user checkout.

## Final-round mutation correction and release-source confirmation

The final Astra review identified a supported mutation Goal whose omitted
fixed detection floor was compared with None during relaxation. Fixed counts now
use the producer's effective floor (the requested total, or default10); unresolved
auto-scaled floors raise a clear policy error, while retarget preserves the same
implicit auto policy. A modern MCP regression approves floor8 over the original
8/10 observation without another producer, preserves its immutable source row,
stamps and time, and displays the approved floor8. Stricter implicit/explicit
floors remain refused. Six regression cases pass; affected Python3.11 adds811PASS.
Both review rounds are complete; the owner fixes this final finding without R3.

Frozen source-v5 aggregate
`7a6a47b87e02ca887018c25e780a1ac0e6c4f605a888de5b67af3cf3eb7f8eb7`
(751files) passed retained real modern MCP confirmation: cycle2/2metfresh with
unchanged Goal bytes/original Job; NEW Verilator sim6 consumes the same branch
approval (100%/1waived), leaves toggle58.33%/0waived correctly unmet, and matches
the strict semantic reader/checked raw policy. No proposal, decision, audit,
policy or Git HEAD changes. Terminal legacy applied/rejected metadata is readable.
Earlier v2/v3/v4 proof remains retained. Artifacts
`/tmp/cr-p4/live-release-confirmation-v5.{json,log}` and source-v5 manifest.

Source-v5 Python profile after the mutation correction: **20,815 passed, 152 skipped** (337.46s). Configured Pyright still analyzes84files with0diagnostics; the same21legacy files remain348→347 with zero introduced/one removed. Complete Ruff src/tests, whole-repository check and format1725files pass.

Source-v5 complete main-based patch coverage is **91.89%** (1344 measured production lines; 109 uncovered), passing the90% gate. This final measurement supersedes the earlier pre-final coverage figures.


## Actual Reviewer receipt correction and CI reconciliation

The owner tested clean -> done through the actual Reviewer Specialist and a
modern MCP approval. Real clean receipts use pending/resolved/observations; the
synthetic issue_list fixture had hidden a derivation failure. The changed Goal
now derives terminal completion from that original receipt, preserves its
immutable source observation, receipt ID and timestamp, and supplies a done
replay view of the findings. A fresh selected Goal derivation permits only the
approved clean -> done mode difference; other invocation context and source
freshness checks still apply. Two regressions cover no findings and an unresolved
critical finding, another Reviewer call without an agent run, and a real source
edit requiring a new review.

CI at cbf881526cfed7d942771096f392d75a8645524c exposed an unchanged QA test race:
its event glob observed a snapshot sidecar before the shim had copied the source.
The same first-incomplete-rename failure was reproduced five times each on main
and this branch with a controlled scheduling pause. The test now waits for a
bare, newline-complete event record. A deterministic regression covers snapshot
creation, a partial event, and publication; no fault controller or shim changes.
The corrected native scheduling probe passed ten times.

Ubuntu3.14 completed99% before its15-minute job bound cancelled execution;
there was no final JUnit result or pytest timeout diagnostic. Full-suite jobs
now use the existing20-minute full-python-matrix bound; compatibility and shard
jobs retain15minutes. Test selection, workers and per-test limits are unchanged.
All six Windows shards passed on the previous head after measured timing updates.
Actionlint and122classifier/sharding regressions pass.

Final combined Python profile: **20,818 passed, 152 skipped** (329.78s).
Complete main-based patch coverage: **91.96%**, 1355 measured production lines
and 109 uncovered, passing90%. Full Ruff src/tests and whole-repository check/format
pass1726files. Configured Pyright84files/0diagnostics; same21legacy files
348->347 with zero introduced/one removed. Final affected Python3.11
992PASS30.96s plus pytest policy55PASS4.86s; CI policy/classifier/shard/QA
189PASS8.81s and pinned actionlintPASS. The failed prior policy-guard run is
retained separately and superseded by this passing complete run.

Frozen source-v7, all751runtime files, aggregate
`9a26eb6056a06d1b7de70dd81f5659180802337cfeb5986933a5c6bb99995930`,
passed real modern MCP confirmation: cycle2/2fresh with unchanged Goal bytes
and original Job; real Verilator sim8 consumes branch100%/1waived while
toggle58.33%/0waived stays unmet. Strict semantic/raw policy match;
proposals, decisions, audit, policy and Git HEAD stay unchanged. Evidence:
`/tmp/cr-p4/live-ci-corrections-confirmation-v7.{json,log}`, source-v7 manifest.
Original snapshots and review reports remain preserved.


## Windows deadline and measured new-test balancing

CI at `7a2aff4ea5f3b3e7de7946c9bc51b10962ec9c7d` passed the corrected QA,
global coverage, and both Ubuntu full suites. Windows shard 2 completed
**3,401 tests, 92 skips** in 819.35 seconds, passed per-test headroom, and
completed evidence publication and cleanup. GitHub nevertheless cancelled
the overall job with its explicit 15-minute maximum-time annotation.
Setup, installation, and final job handling left insufficient margin around
the passing test step.

All six shard selections replay exactly against the configured timing model
and cover 20,962 eligible cases once. Of 140 Phase 4 added or changed cases,
77 measured cases still used the one-second default despite 763.405 seconds
of observed call time; one other case has no call measurement. Only those
77 missing estimates are added from this run, rounded to positive
milliseconds. The 62 earlier measured entries, all legacy estimates,
default, selection, workers, and deadlines remain unchanged. Replay using
the exact assignment algorithm and recorded call times lowers the highest
shard sum from 2,533.706 to 2,355.662 seconds (7.03%). This is a measurement
replay; actual final-head CI must verify runner wall time.

Runtime and test bytes are unchanged by this timing-data correction. The
20,818-pass broad gate, 91.96% complete-patch coverage, configured and legacy
type checks, and exact 751-file source-v7 live confirmation remain applicable.
Evidence: `/tmp/cr-p4/ci-7a2-windows-artifacts/` and
`/tmp/cr-p4/ci-7a2-timing-diagnosis.md`.
