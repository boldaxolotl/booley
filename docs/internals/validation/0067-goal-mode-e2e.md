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

## Phase 5 finish, summary publication, retry and abandonment

On 7 October 2026, the owner ran six compound checks through the actual modern
MCP server in a new isolated non-Stealth PicoRV32 fixture. Configuration,
authored cores, design sources and consumed firmware were committed before
entry. The fixture used production initialization Git-ignore and scanner
helpers; this check does not claim a complete host initialization CLI run.
Installed Booley, the user Project and earlier retained fixtures were
unchanged.

The runtime used frozen Phase 5 source-v1: **762 files**, aggregate
`04b01fb2c02e67bdb582fa83cbb95ec3d0996078dbedeab734ae5ea91c9f30fb`.
Every Python source and `pyproject.toml` matched the coder's freeze manifest.
Actual Verilator lint passed in **3.325s**; Icarus simulation passed both
required tests in **41.765s**. Actual Astra HIGH RTL code-style review took
**66.163s**, recorded **two MINOR findings**, and met the declared done-review
Goal. Status reported all three Goals met and fresh. Findings remained visible;
no new approval or human UI/authentication claim was made.

Production `goal_finish` completed in **1.903s**. Its immutable JSON, chat and
requested HTML retained the exact selected observations and open findings,
without the inherited Ticket approval banner. Raw Git proof showed commit
`c546f7b83da45b0072e66ce5542b6fee07800edc` has exactly one parent,
the validated pin `fc54360efb94e97d950cc4e72153e0ba3c8228d0`, and changes
only `.booley_project/goals/history/p5-finish-v1-20261007T164714Z.md` at
literal mode `100644`. The rest of the tree and the main checkout's ref/index
were unchanged.

An identical saved-result retry returned the original response and created no
second commit. Re-entry started from that summary commit. Retrying the old
operation after replacement entry left the new occupying record unchanged.
Production abandonment closed the unmet replacement while retaining its branch
and files. All six checks passed. Evidence is retained in
`/tmp/cr-p5/live-finish-v1.{json,log}` and the owner's source-v1 manifest.

Focused production-service regressions also exercise generated build data
authorized by the exact selected campaign manifest, refusal of foreign or
unselected proofs, unavailable bytes, HDL file types and planner programs,
and immutable materialization checks during recovery. Those cases are separate
from this live run's committed-firmware path. Crash boundaries, publication
fences, paired inputs, external-link confinement, and raw Git/index races are
covered by local regression tests; this live check does not imply native
Windows or new human elicitation coverage.

With the preview switch unset, the owner also ran the released Ticket demo's
actual `booley run --ticket <fixture slug> --check-ready` and
`booley.dev_support.demo_contract` verifier against the same frozen source-v1.
Both passed. Logs are retained in `/tmp/cr-p5/live-ticket-{ready,contract}-v1.log`.
The verifier's own preparation is exercised; these calls do not claim another
complete lint/simulation run.

The subsequent source-v2 capture contains the same **762 runtime files**, aggregate
`8dfa61d77e93ed5284baf3294f53af48dba0888c7c98aea6501a2f888cd6462e`.
Its sole difference from v1 is the packaged `booley/data/refs/CHANGELOG.md`
mirror, synchronized with the public changelog using the repository helper.
Every Python source and `pyproject.toml` is byte-identical to the executed v1.
The v1 runtime checks remain applicable through this explicit asset-only delta;
v2 was captured and compared, not re-executed as another live EDA run.
Evidence: `/tmp/cr-p5/live-source-v2.json` and
`/tmp/cr-p5/live-source-v2-delta-proof.json`.

The initial implementation's complete Python profile passed **20,934 tests**, with **152 skips**,
in **328.06s**. Complete main-based patch coverage was **92.41%**
(1,218 of 1,318 changed production lines). Full `src/` and `tests/` Ruff,
whole-repository Ruff and format checks passed. Configured Pyright analyzed
95 files without diagnostics; identical strict scope over all 17 touched
legacy modules introduced no diagnostics (643 baseline, 633 final).

Python 3.11.15 passed 1,504 affected cases before an environment-only
collection error exposed missing declared Hypothesis. After installing that
dependency, the complementary run passed all 15 generated-input cases and
133 existing campaign-manifest codec cases in 12.82s. Together these cover
all **1,519 affected cases**, plus those **133 producer cases**, without any
runtime correction. Both logs preserve the actual invocation outcomes.
The first broad run's packaged-changelog mismatch and failed coverage XML
are also retained; the final broad run followed the official changelog sync.
Final evidence: `/tmp/cr-p5/full-python-final.log`, `coverage-final.xml`,
`patch-coverage-final.{json,html}`, `py311-final.log`,
`py311-generated-final.log`, `pyright-final.json`, and
`strict-comparison.json`.

After the independent first-pass Standards and Spec reviews, the owner repeated
all six compound modern-MCP checks on frozen source-v3: **764 runtime files**,
aggregate `b98d25b63dd1a4f95337fffff94c7560595bbde90cfe0d8ae149915a33a1ce11`.
Every one of the coder's **684 Python/config rows** and every deployed runtime
file matched the immutable capture before execution. Verilator passed in
**3.133s**, Icarus in **51.790s**, and Astra HIGH review in **110.997s** with
two MINOR findings and zero major or critical findings. Actual reviewer stderr
confirmed `gpt-6-astra` at `effort=high` with normal completion.

Record `p5-finish-v3-20261007T174716Z` published summary commit
`33cc37acb30fbe6ad11304c38e7aca43aef9bd91`: exactly one parent equal to
`fc54360efb94e97d950cc4e72153e0ba3c8228d0`, one summary at mode `100644`,
exact frozen blob, and unchanged rest tree. The primary checkout's HEAD and
index bytes were preserved. Exact selected package rows, JSON/chat/HTML, visible
findings, saved retry, replacement entry, old retry leaving the new occupant
untouched, and abandonment retaining branch/files all passed. Released Ticket
readiness and demo-contract checks also passed with the preview switch unset.
Evidence: `/tmp/cr-p5/live-finish-v3.{json,log}`, `live-report-v3.md`, and
`live-ticket-{ready,contract}-v3.log`.

Three independent owner production controls passed on that immutable source:
saved absolute publication-path tampering refused before any external file or
ref write; stat-only tracked-file changes left the index unchanged during
cleanliness inspection; and Stealth finish preserved index bytes, HEAD and
object count. The original source-v1 negative reproduction remains retained.
These temporary real-Git controls are distinct from live EDA evidence:
`/tmp/cr-p5/owner-fix-regressions-v3.{json,log}` and
`owner-fix-negative-v1.log`.

Source-v3's complete Python profile passed **20,996 tests**, with **152 skips**,
in **355.15s**; complete main-based patch coverage was **91.30%**
(1,448 of 1,586 changed production lines). Python 3.11.15 passed **1,747 tests**,
with **six skips**, in **222.29s** across the full affected/complementary scope.
Configured Pyright analyzed 97 files without diagnostics. Identical strict
base/head scope now covers all **20 touched legacy modules**, with 643 baseline
and 633 head diagnostics, zero introduced. These reports remain historical
because a subsequent coalesced Project/control spelling regression required
source-v4. Namespace-spelling fixtures use actual file and Git identities with
modeled mount anchors; native bind-mount execution was unavailable and is not
claimed. Existing internal-symlink and physical-substitution refusal controls
remain exercised.

The final runtime source-v4 was frozen before broad checks and independently
verified across all **684 Python/config rows** and **764 deployed runtime
files**, aggregate
`971813590097936dee5fb9a4e374c0210b2bc70042aa939f79689e72a2848725`.
Its four-file delta from v3 preserves role-specific Target tests, waiver and
D7 labels when distinct entry spellings coalesce onto the same physical roots.
The production namespace regression covers fresh finish, both frozen-attempt
and finishing recovery, exact saved retry, and refusal/revalidation for actual
protected or Target changes. Related compatibility and tamper controls passed
**202 tests**. Two new POSIX namespace fixtures were explicitly marked
POSIX-only after freeze; their Linux predicates are false, and the affected
module was rerun with **41 tests passing**. Runtime bytes remained unchanged.

The owner repeated the complete six-check live exit on a fresh v4 worktree.
Actual Verilator lint passed in **3.613s**, Icarus simulation in **53.411s**,
and the fixture's configured Astra HIGH product Reviewer in **62.378s**, with
two MINOR findings and zero major or critical findings. All three Goals were
met and fresh. Production finish took **3.483s** and retained the exact selected
evidence, open findings and Goal-only JSON/chat/HTML presentation.
Record `p5-finish-v4-20261007T180410Z`, operation
`f4775479-0280-494b-93b9-c1a915ac308c`, published commit
`8d6d677174c92f8f708f68122df6bfcad798b485`. Raw proof confirms one parent
equal to `fc54360efb94e97d950cc4e72153e0ba3c8228d0`, exactly the summary
path at mode `100644` with the exact frozen blob, and unchanged rest tree.
Primary HEAD and index bytes remained unchanged. Saved retry, re-entry, old
retry leaving the replacement untouched, and replacement abandonment retaining
files and branch passed. The three independent path/index/Stealth controls and
released Ticket readiness/demo-contract checks also passed on v4.
Evidence: `/tmp/cr-p5/live-finish-v4.{json,log}`, `live-report-v4.md`,
`owner-fix-regressions-v4.json`, `live-source-v4-delta-proof.json`, and
`live-ticket-{ready,contract}-v4.log`. This product review is distinct from
subsequent external code reviews; it makes no new native Windows, bind-mount,
human UI or authentication claim.

The final v4 complete Python profile passed **21,007 tests**, with **152 skips**,
in **394.51s**. Complete clean committed main-based patch coverage is **90.58%**
(1,597 of 1,763 changed production lines). Python 3.11.15 passed the same full
affected/complementary scope: **1,758 tests**, with **six skips**, in **227.31s**.
Full `src/` and `tests/` Ruff, whole-repository Ruff and the format check
(1,747 files) passed. Configured Pyright analyzed 97 files without diagnostics;
the identical 20-module legacy strict comparison remains 643 baseline versus
633 head diagnostics, with zero introduced. Reports are retained under
`/tmp/cr-p5/full-python-v4.log`, `coverage-v4.xml`,
`patch-coverage-v4.{json,html}`, `py311-v4.log`, `pyright-v4.json`,
`ruff-*-v4-final.log`, and `strict-v4-*.json`. The earlier attempted coverage
command ran before pytest had written its XML; that failed invocation is
retained separately and the final coverage command passed after completion.
The precommit staged-union calculation reported 91.43%; the clean committed
comparison above is the authoritative final patch gate.


## Phase 5 reviewed recovery and pinned Project selection (v6)

The reviewed implementation separates private immutable materialization buffers
from public hash/classification/provenance facts, while preserving exact selected
producer evidence. It retains the D7 `__pycache__` exception, restores lifecycle
replay and abandonment through stable Git Worktree Identity, freezes response and
HTML bytes, and associates canonical completion files with successful attempts.
Abandonment closes pending proposals only after committed-intent recovery and a
durable terminal association. Summary publication uses literal unfiltered blobs,
replacement-free pinned Git operations, non-executable regular-file ownership,
and durable inode ownership before visible staging or index-lock acquisition.

Committed input views recursively reconstruct initialized local submodules at
exact gitlink pins and support built-in Git text/EOL projections. Custom filters,
LFS and unproved external attribute transformations receive an actionable
unsupported-input diagnosis. Separately owned Project configuration is
materialized at its own original/final pin before RTL submodule selection,
including outside-RTL paired layouts; a selected missing submodule still refuses.
Legacy saved terminal responses and proven historical publication replay exactly.
An unpublished legacy attempt containing raw materialization buffers revalidates
to ACTIVE with its artifacts and monotonic Job fence retained, requiring a fresh
hash-only attempt rather than rewriting old bytes.

The owner verified all **692** Python/config freeze rows and all **772** copied
runtime files before repeating production execution on frozen **source-v6**.
Coder freeze digest:
`9860aacfe65232e968162de5dc42df3413fa8f4b5e475b117090353beb197237`.
Runtime aggregate:
`a2bef79e134a51579106593b1b9e97d47393486d9a46be78c7f764b0a98f6e9f`.
V5 to v6 changes exactly `input_view.py` and `committed_export.py`; earlier
versions remain historical evidence for their exact bytes.

The full modern MCP live exit passed all six compound checks: entry **0.150s**,
Verilator lint **3.450s**, Icarus simulation **49.848s**, configured product
Reviewer **51.363s** (2 MINOR, no major/critical), and finish **1.268s**. All three
Goals were fresh and met, with exact selected/original provenance in the frozen
package, chat and requested HTML, and no Ticket approval prompt. The committed
product fixture retained its configured Astra HIGH role; external code review
uses Opus 5.5 HIGH after the user's override.

Record `p5-finish-v6-20261007T202111Z`, operation
`aa29b5f2-ee9b-4375-abd0-b5fb271b04e1`, published summary commit
`53d2352a2ffe58903043fce5bc358ec37100d471` with exactly one raw parent
`fc54360efb94e97d950cc4e72153e0ba3c8228d0`, one summary blob in Git mode `100644`,
and the rest of the tree unchanged. Main HEAD and index bytes remained unchanged.
Saved retry, re-entry from that commit, old retry leaving the replacement occupant
unchanged, and replacement abandonment retaining its branch/files all passed.

All **19 independently authored owner recovery controls** passed. A separate owner
execution of the **19 coder-authored exported-input tests** passed in **3.13s**;
these are not additional independent controls. The controls include actual v4
terminal/pending upgrade fixtures, distinct-filesystem publication, foreign
index-lock file/symlink preservation, raw filter/replace immunity, private buffer
absence, failed abandonment recovery preserving pending proposals, and read-only
index/Stealth checks. Released Ticket readiness and the actual demo contract
passed with Goal preview unset; this does not claim a new full Ticket EDA run.

Evidence: `/tmp/cr-p5/live-report-v6.md`, `live-finish-v6.{json,log}`,
`source-freeze-v6.json`, `live-source-v6-delta-proof.json`, `live-deploy-v6.log`,
`owner-{r1-regressions,raw-publication,index-successor,legacy-upgrade,legacy-pending,fix-regressions}-v6.{json,log}`,
and `live-ticket-{ready,contract}-v6.log`. Native bind mounts remain unavailable;
namespace-spelling units and real distinct-filesystem/link fixtures are disclosed
as such. No native Windows, human UI or authentication execution is claimed.

The final v6 full Python profile passed **21,065 tests**, with **152 skips**, in
**361.99s**. The affected/complement Python 3.11 scope passed **2,338 tests**, with
**6 skips**, in **218.31s**. Complete Ruff for `src/` and `tests/`, whole-repository
Ruff, and whole format verification passed (**1,757 files**). Configured Pyright
analyzed **105 files** with zero errors. The identical expanded **21-module**
legacy strict baseline/head comparison has **877 → 867 existing errors**,
**zero introduced** and **10 removed**; it is a comparison, not a strict-clean
claim. Normalization uses exact scope plus file/rule/message multiplicities.

The complete final main-based production patch covers **2,157/2,393 lines
(90.1379%)**, with **236 missing**. The explicit whole-diff precommit report avoids
staged/committed union duplication; the clean committed main comparison is the
final authority. Evidence: `/tmp/cr-p5/full-python-v6.log`, `coverage-v6.xml`,
`py311-v6.log`, `ruff-*-v6.log`, `pyright-v6.json`,
`strict-v5-corrected-{base,head}.json`, `strict-v5-corrected-base-report.json`,
`strict-v6-head-report.json`, `strict-v6-comparison.json`, and
`patch-coverage-v6-whole-precommit.{json,html}`. Invalid absolute-include strict
setup reports, failed collection/fixture logs and historical v5 whole-patch
coverage below 90% are retained and are not claimed as successful gates. The
Project-name order regression now isolates ambient cache/environment in tests;
production legacy Project fallback remains intact. CI scripts and timeouts were
not changed for the unrelated archive-download infrastructure failure.

Authoritative clean committed coverage artifacts are
`/tmp/cr-p5/patch-coverage-v6.{json,html}`, with the fixed main parent, clean status,
unchanged runtime freeze and single-commit proof in `final-commit-proof-v6.json`.

### Phase 5 final owner corrections and frozen v7

Both Opus 5.5 HIGH review rounds completed. The owner verified the final
findings and implemented the corrections without a third review. New independent
regressions passed **20/20** in **23.20s**; the same tests with an asserted
immutable reviewed-v6 runtime import failed **17/20**, with three compatibility
controls passing. They cover large attribute queries through NUL-separated stdin,
destination-local summary staging, pre-fence publication prerequisites, typed
participant recovery, exact conflicting-index preservation, abandonment capture
changes, retained history and authority-only crashes, and qualified Target binding.
Non-versioned Project snapshots now select original/final submodules before export
and cannot change during capture. Cleanliness reuses one pinned byte projection.

Windows CI exposed fixture commits made with `core.autocrlf=false` while runtime
inherited global `true`. Two representative cases failed under that inherited
configuration and passed after fixtures persisted their intended local policy.
Production conversion semantics, CI scripts and timeouts remain unchanged.

Frozen v7 has **692** source/config rows, SHA-256
`9678d8f5cd766195039ffa96f957be66c9a2eb76f76b671c936561b3a7b497f0`,
and **772** runtime files, aggregate
`8b1873c29c8a42f2bc760336716b6c4290f7634e718987ef58448e7edeae86e0`.
All **238** new/changed runtime functions fit the 50-line limit. Complete Ruff,
whole format (**1,758 files**) and configured Pyright (**105 files**) passed.
The identical 21-module legacy strict comparison remains **877 → 867** errors,
with **zero introduced** and **10 removed**, rather than strict-clean.

All **19** independent earlier owner controls passed again on frozen v7.
The full modern MCP live exit passed all **six** compound checks: entry **0.214s**,
Verilator **2.999s**, Icarus **40.165s**, configured product Reviewer **50.054s**,
and Finish **1.419s**. Product review retains the fixture's Astra HIGH role;
external code reviewers use Opus. Record `p5-finish-v7-20261007T212500Z`, operation
`815ed6d9-f87a-40ab-b0f0-034117a984cb`, published raw summary commit
`55cf5025a59a7d4a9ec358a9de4fdc292eaabd92`: one parent
`fc54360efb94e97d950cc4e72153e0ba3c8228d0`, one exact mode-100644 summary path,
and the rest of the tree unchanged. Root main HEAD/index were preserved.
Exact retry, re-entry, old retry leaving the replacement untouched, and
replacement abandonment retaining branch/files all passed. Released Ticket
readiness and the actual public demo contract passed with preview explicitly unset.

Evidence is under `/tmp/cr-p5/`: `source-freeze-v7.json`, `live-source-v7.json`,
`live-deploy-v7.log`, `owner-static-v7.json`, `live-finish-v7-valid.log`,
`live-finish-v7.json`, `live-report-v7.md`, `owner-controls-v7.log`, dedicated
`owner-*-v7.{json,log}`, and `r2/findings-verification.md`. Setup/model errors and
failed zero-item collection attempts are retained and excluded. Native bind mounts,
native Windows, new human UI/authentication and full host initialization are not
claimed locally. Paths and physical identities remain explicit public provenance;
private materialization bodies are private. Historical exact v1 responses remain
unchanged. Unsupported nonhermetic inputs and unavailable selected gitlinks refuse.

The final v7 full Python profile passed **21,085 tests**, with **152 skips**, in
**339.24s**. The complete main-based production patch initially covers
**2,229/2,459 lines (90.6466%)**, with **230 missing**. This is the explicit
whole-diff precommit result, with no narrowed source or staged/committed union;
the clean committed comparison is the final publication gate. Logs and XML:
`full-python-v7.log`, `coverage-v7.xml`,
`patch-coverage-v7-whole-precommit.{json,log}`. Owner postexecution verification
confirmed every deployed runtime hash and the exact file set remained unchanged.

The full affected/complement Python 3.11 scope passed **2,358 tests**, with **six
skips**, in **246.31s**; all 2,358 headroom checks passed, highest **2.1%** of
the test's budget versus the unchanged 50% limit. All 20 final owner regressions
also passed in **16.04s** under inherited global `core.autocrlf=true`.
Authoritative clean committed whole-patch coverage and single-main-parent proof
are recorded in `patch-coverage-v7.{json,html}` and `final-commit-proof-v7.json`.
No source exclusions, narrowed patch, or test/CI timeout changes were used.

Native CI on `7a5014f` passed Ubuntu full/minimum-version/coverage and package
smoke. Five export-fixture cases failed on Windows: cloned repositories inherited
ambient EOL policy, native `write_text` produced CRLF, and a deliberately replaced
submodule `.git` pointer was read-only. Three Windows shards failed; three were
cancelled. The fixture-only follow-up persists LF policy in the known clones,
writes text with explicit LF, and makes its own pointer writable before the
intentional administration-substitution test. Runtime and CI configuration are
unchanged. All **692** production/config hashes still match frozen v7, so its
broad/minimum-version/type/live/runtime coverage evidence remains applicable.
Both modules using the changed helpers passed **39/39** with inherited global
`autocrlf=true` (**28.61s**) and **39/39** on Python 3.11 (**31.28s**), with all
headroom checks passing (highest **2.3%**). These runs verify the final fixture
bytes; a new complete-profile or live runtime execution is not claimed.
Evidence: `ci-v7-failed-{pr.json,checks.txt,log.txt}`, `r2/owner-ci-export-red.log`
(three reproducible clone-policy failures), `r2/owner-ci-export-{green,py311}.log`,
`ruff-*-v7a.log`, `patch-coverage-v7a.{json,html}` and
`final-commit-proof-v7a.json`. The new commit receives normal CI.

The subsequent normal run on `bb9fc12` passed Ubuntu, coverage and package
smoke. One Windows fixture still failed while truncating the existing submodule
`.git` pointer, after clearing its read-only flag. The fixture now unlinks and
recreates its owned pointer and proves Git resolves the replacement administration
before checking the refusal. This changes no production or CI configuration.
The 39 affected tests passed with inherited `autocrlf=true` in **29.14s** and
on Python 3.11 in **29.13s**. A Python 3.11 run with the unchanged 50% headroom
guard passed **39/39** in **27.95s**, checking all 39 tests (highest **2.0%**).
Native Windows verification remains pending normal CI; immutable v7 runtime
evidence is reused after exact source verification. Evidence:
`ci-v7a-failed-{pr.json,checks.txt,log.txt}`,
`r2/owner-ci-pointer-{green,py311,py311-headroom}.log`, `ruff-*-v7b.log`,
`patch-coverage-v7b.{json,html}` and `final-commit-proof-v7b.json`.

Normal CI on `034f962` passed Ubuntu, coverage, package smoke and Windows
shards 1 and 5, including the pointer-fixture correction. Four paired Finish
cases failed with stale Target-surface evidence; shard 4 was cancelled. Under
inherited `core.autocrlf=true`, the exact four cases reproduced locally
(**4 failures, 4.29s**); one case without a later suite edit failed in **1.31s**.
An actual export of that saved fixture proved live LF versus committed-working
CRLF `tests.toml` bytes: the paired repository lacked the local policy already
used by its commit helper. Persisting `core.autocrlf=false` in that owned fixture
made the exact four cases pass (**6.20s**), without changing runtime conversion.

All **960 Goal tests** then passed with inherited autocrlf enabled in **39.29s**;
all 960 headroom checks passed, highest **1.4%**. The **114 tests** consuming the
paired helper passed on Python 3.11 in **33.82s**, with all headroom checks
passing, highest **1.0%**. Full Ruff/format still pass for **1,758 files**.
Production/config and runtime hashes remain identical to v7; its complete source
gates/live/XML are reused. Native Windows validation awaits the next normal CI.
Evidence: `ci-v7b-failed-*`, `r2/paired-policy-diagnosis.md`,
`r2/owner-ci-paired-{surface-red,surface-minimal,surface-green,policy-probe,all-goals,consumers-py311}.log`,
`ruff-*-v7c.log`, `patch-coverage-v7c.{json,html}` and
`final-commit-proof-v7c.json`. CI scripts and timeouts remain unchanged.


Normal CI on `782290f` passed Ubuntu, coverage, package smoke, minimum-version
Windows and Windows shards 5 and 6. Shards 1–4 were cancelled at the unchanged
15-minute job limit; their partial results do not establish completion. One
existing Reviewer case exceeded the 50% headroom guard (30.5s of its 60s budget).
The owner approved a narrow timing recovery: add **243** previously unmodelled
Phase 5 estimates from measured native Windows calls, preserving every existing
estimate, schema and default, and annotate only that existing parametrized
Reviewer test with **120s**, following the repository 3x/30s sizing rule.
CI scripts, worker counts, the 50% headroom guard and 15-minute job limits remain
unchanged. Partial timing replay is diagnostic and does not predict passing CI.

The 18 CI sharding tests and two Reviewer cases passed (**20 tests, 3.79s**).
Both Reviewer cases passed on Python 3.11 (**3.72s**), with both headroom checks
passing (highest **1.2%**). Full Ruff and formatting pass for **1,758 files**.
All **692** production/config hashes and **772** runtime hashes and the runtime
file set match immutable v7; its complete source/live/XML evidence is reused.
The timing/model and test annotation are separately verified against the exact
approved patch. Evidence: `ci-v7c-failed-*`, `ci-v7c-windows*-cancelled.log`,
`ci-v7c-timing-proposal.json`, `recovery-v7d-{tests,py311}.log`,
`recovery-v7d-proof.json`, `patch-coverage-v7d.{json,html}` and
`final-commit-proof-v7d.json`. Required native CI awaits the new normal run.


### Frozen input compatibility

Both selected versioned participants must be clean as whole repositories.
Pinned working-byte export applies that policy to every versioned file, including
unconsumed documentation. A custom filter, Git LFS filter, or unsupported encoding
anywhere in a selected participant refuses completion with an actionable diagnostic;
Booley neither executes external transformations nor substitutes raw pointers for
design bytes. Configured pinned submodule exclusions can omit an entire participant.

Original baseline gitlinks removed or moved by the Goal Branch can use their
original pinned `.gitmodules` name and contained retained local Git object cache.
This baseline view never fetches or reads replacement working files. Final selected
submodules still require their live physical checkout, exact pin, and clean bytes.
Missing original objects, ambiguous or unsafe mappings, and linked cache paths
refuse completion.


### Requested adversarial review and final runtime v9

The requested Opus 5.5 HIGH Standards, Spec and adversarial reviews captured the
complete PR at `28e7f1d10` against main `9859f2a34`. Their original reports remain
unchanged in the local evidence bundle. Independent real-Git counterexamples
confirmed retained-summary deletion, removed/moved baseline export refusal and
pruned dangling publication objects. Repairs preserve competing staging and
locks, use original pinned contained baseline objects, and reconstruct only the
exact sealed raw publication commit after original physical authority checks.
Typed repository failures, sealed abandonment recovery, read-only derived Job
activity, frozen admission fences, shared Reviewer policy and history-exclusion
Doctor guidance also have regressions. Whole-selected-participant filter/LFS
refusal remains an explicit compatibility limit. Missing historical hash-only
commit objects cannot reconstruct unknown raw metadata and refuse actionably.

Final production/config freeze v9 covers **693 files**, digest
`cc09c71b25b4f454122311c66918669f5efe4cc2316359677d9184a86fb39d40`.
The immutable runtime covers **773 files**; deployed files and hashes were checked
after actual execution. A subsequent test-only Doctor inventory registration
is recorded separately without replacing the freeze. All **264** new or changed
runtime functions in the whole PR are at most 50 lines.

The final broad profile passed **21,131 tests**, with **152 skips**, in
**350.67s**, measuring the complete current repository source at **89.46%**
coverage. Python 3.11 passed **2,604 tests**, with **7 skips**, in **84.23s**;
the unchanged 50% headroom guard passed with maximum **4.2%**. Whole main-based
changed-line coverage on the complete clean committed PR is
**2,460/2,706 = 90.91%**, including the new baseline-object module. The earlier
pre-commit combined-diff report counted 2,509 lines and is superseded for this gate.
Full Ruff and format pass for **1,760 files**. Configured Pyright has zero errors;
the identical corrected 21-file strict scope introduces zero diagnostics
(**877→867**), and the new baseline-object module passes separate strict checking.

Owner execution against immutable v9 passes **53** new recovery/export regressions
and **19** independent publication, ownership and upgrade controls. Actual modern
MCP entry, Verilator lint, Icarus simulation and configured RTL review pass six
compound checks. Finish publishes summary commit
`0ad688fe0629e6c8f90e15368f960aa37c85c0ae`, with sole parent
`fc54360efb94e97d950cc4e72153e0ba3c8228d0` and exactly the intended summary path.
Frozen package/chat/requested HTML, saved retry, re-entry, old-record retry and
abandonment pass; original main HEAD and index remain unchanged. Released Ticket
readiness and its demo contract pass with Goal preview unset.

The timing model preserves **907** existing estimates and default 1, adding
**780** passed historical Windows observations. **749** additions retain exact
final test-file bytes; **31** retain identical individual test bodies with changed
helpers/files. These are historical timings, not final-runtime native measurements.
Two new Goal tests receive individual **150s** budgets from observed **43.475s**
and **40.944s** calls under the 3×/30-second rule. CI scripts, workers, headroom
and 15-minute job limits remain unchanged. Fresh local collection assigns all
**21,283** eligible Linux cases exactly once across six shards. **21,275** Windows
cases are a projection only; exact-head native collection and normal CI are pending.

Authoritative local evidence: `requested-review-20261008/final-fix-report.md`,
`final-test-gates.json`, `broad-v9-final.log`, `py311-v9-final.log`,
`patch-coverage-v9-whole.{json,html}`, `strict-v9-comparison.json`,
`red-counterexamples-v7.{py,log,json}`, `timing-source-v9.json`,
`final-shards-proof.json`; owner `live-finish-v9.{log,json}`,
`live-postexecution-v9.log`, `owner-controls-v9-after-cleanup.{log,json}`,
`owner-review-fixes-v9-after-cleanup.log`, `owner-static-v9.json`,
`live-ticket-{ready,contract}-v9.log`. Invalid intermediate ENOSPC runs, a wrong
old-source pytest replay and incorrectly scoped initial coverage/strict runs are
preserved and excluded from these final gates. Only completed coder-owned
disposable test basetemps were removed to recover exhausted temporary inodes.

Native bind-mount identity stability, new human UI authentication, full host
initialization and two real client sessions are not claimed. Large-repository
lock/export limits, unavailable original caches and unsupported hard links remain
known limits. Prior Git-for-Windows shell failures remain unexplained; timing
rebalancing does not establish their cause or cure. Exact-head green required CI
is mandatory before queueing. Phase 6 has not started.


### Current-main integration and final runtime v10

The initial final-head watcher reported missing `ci-required` after 328s;
Confidential content alone had run. The documented single identical-tree empty
commit recovery was consumed. A subsequent cached-main merge simulation and
GitHub `CONFLICTING`/`DIRTY` result identified the concrete cause: PR 1306 had
merged the same Reviewer 120s annotation with a different explanatory comment.
The watcher was stopped before other GitHub reads. Main fast-forwarded to
`5765840e746baec649a61cb5c895006fff2099c8`; rebase retained its exact Reviewer
file, resolved only that comment and dropped the empty recovery commit. This
is a repaired PR conflict, not evidence of a global GitHub incident.

All Goal production changes are byte-identical to v9. Of its 693 source/config
hashes, 692 remain identical; the only integrated production change is main's
image-inventory fix to omit validated Docker `<none>` repository/tag rows.
A new immutable v10 freeze records all **693 files**, digest
`0a81dc770db618ee979e133040db4a5bb3ad79265d704c632a0d0a18987b069e`.
All **773** deployed runtime hashes and exact fileset were verified after fresh
actual execution. The final validation-document append is hashed separately
without overwriting either freeze.

Fresh full Python profile: **21,140 passed, 152 skipped, 322.41s**, full current
source coverage **89.46%**. Whole clean committed PR against current main:
**2,460/2,706 = 90.91%**. Fresh Python 3.11 integration covers all 170 image tests
and the exact two Reviewer cases: **172 passed, 5.15s**, maximum **1.0%** with
the unchanged 50% headroom guard. The prior v9 **2,604 passed/7 skipped** Goal
complement is explicitly reused for unchanged Goal runtime/test bodies; it was
not rerun as part of this integration. Full Ruff and format pass, **1,760 files**;
configured Pyright **106 files, zero errors**; identical corrected strict 21-file
comparison **877→867, zero introduced**. New baseline reader's strict check and
owner **264 functions ≤50 lines**, **19 controls** and **53 regressions** are
reused by exact unchanged Goal production hashes and identical whole source diff.

Fresh owner immutable-v10 live checks pass all six compound assertions: entry
**0.174s**, Verilator lint **2.755s**, Icarus simulation **50.392s**, configured
RTL review **77.815s**, finish **1.062s**. Exact summary-only commit
`668430fed730fc1c7339ee239e2391a5ae63efdc` has sole parent `fc54360`; the original
main HEAD and index remain unchanged. Frozen package/chat/requested HTML,
retry/re-entry/old-record retry and replacement abandonment pass. Fresh released
Ticket readiness and demo contract pass with Goal preview unset.

Fresh collection assigns **21,292 eligible Linux cases** once across six shards;
ten added and one removed case are all upstream image tests. **21,284 Windows
cases are projected only**, awaiting final-head native confirmation. The model
retains all 907 prior estimates plus the same 780 historical additions and
default 1; CI scripts, workers, headroom and 15-minute job limits are unchanged.

Evidence: `integration-main-20261008/final-report.md`, `final-gates.json`,
`source-integrity-final.json`, `broad.log`, `py311.log`,
`whole-patch-coverage.{json,html}`, `collection-proof.json`; owner
`owner-integration-v10-static-proof.json`, `owner-patch-coverage-v10.*`,
`live-finish-v10.{log,json}`, `live-postexecution-v10.log`,
`live-ticket-{ready,contract}-v10.log` and `rebase-v9-main-proof.json`.
All native/bind-mount/UI/client integration limitations above remain. Exact-head
normal CI is still required before Mergify queueing and Phase 6.


### Phase 5 native CI portability recovery and current-main candidate (2026-10-08)

Normal CI run37754059641 at published322085 failed. The new paired-publication fixture lacked persisted local RTL Git author configuration, the baseline reader serialized Git alternates with native Windows CRLF, a physical-recreation test renamed onto an existing Windows directory, and an unset-configuration test inherited runner global autocrlf. These were confirmed with RED evidence before narrow repairs: persist fixture author configuration, write the alternate path as raw bytes with literal LF, restore the moved directory into an absent destination, and isolate global/system Git configuration only in the unset-config test. A real object-lookup regression also asserts exact alternate bytes under emulated Windows default-newline behavior. Production authorization, object containment and fallback policy are unchanged.

The native run's six Windows manifests select all21286 eligible IDs exactly once. Only four native shard JUnit artifacts exist; two cancelled shards have no JUnit. Available fatal diagnostics are empty. Deadline cancellation and platform completion are therefore unresolved, not repaired claims. CI scripts, workers, six shards, headroom, default estimate and fifteen-minute job limits remain unchanged. The next repaired candidate receives normal exact-head CI before merge.

Main advanced to57eeb7ceb31eb75e67529513181be6055c3b1508 with red-first Goal rules. A conflict-free single-candidate rebase preserved all three narrow repaired files byte-for-byte. Immutable sourcev11 remains historical; sourcev12 freezes693 Python/config files (digest88cca97ee288cf3c5164f56e56bd1ace19ec345830780c8efe7e5d64aaa98f17) plus35 supplemental files. All773 copied/deployed runtime files match aggregatec6030367d7fe0d72d4aa857486d7677f373f7c7e08b1fbdbef4266251b93c061 before and after live execution.

Owner actual frozen-v12 MCP execution passes all six compound checks: entry0.176s, Verilator lint3.714s, Icarus simulation52.945s, configured RTL review76.416s and finish1.518s. Summary663254cce73e1e662b1a40c42bc40db1d586afe2 has sole parentfc54360 and one literal history path. Lost-response retry, re-entry, old-record retry and replacement abandonment pass; original main HEAD and index SHA2543b0e1ab63442c0e90031ed7ec11592b0b4384134cc5dba21b6cd1695a15b5 remain unchanged. Released Ticket check-ready and demo-contract checks pass with preview unset.

An extra full-suite probe changed HOME to an empty directory. It hid the installed user-site FuseSoC and Booley modules used by subprocess tests; the interrupted probe (39failed,7190passed,72skipped,1error) is retained and excluded. Interrupted pre-rebasev11 broad/Python3.11 runs are likewise excluded. Final coverage uses a fresh normal readiness environment, current source and full Python test profile; no unrelated test or dependency-policy fixes were made.

Fresh Python3.11 exercises all affected Goal/cache/pinned/shared Reviewer and paired repository cases, including upstream red-first rules:1230passed43.44s, headroom maximum1.4%. Whole current-main PR AST comparison verifies all264 changed runtime functions at most50 physical lines; the baseline reader remains50. Full Ruff, formatting, configured106-file Pyright and strict one-file baseline reader pass; the same21 strict scope reports867 vs baseline877 diagnostics, with zero introduced and ten removed.

Actual Linux collection is21295. Current Windows21287 is a projection derived from the actual previous21286 native IDs plus the new literal-LF regression, with explicit Linux-only supervisor and QA path-separator differences; it is not final native execution evidence. All907 original timing values plus780 historical additions and default1 remain unchanged. Provenance now distinguishes734 byte-identical final test files,45 identical prior test bodies with changed file/helpers, and one historical body changed by the upstream literal rules assertion. None of these estimates proves final native walltimes.

The requested Opus Standards/Spec and adversarial reports remain tied to captured28e7f1d10. Confirmed findings and later portability repairs are owner/coder verified; no extra external review was invoked and no claim of Opus approval of the repair candidate is made. Prior19+53 owner controls provide unchanged lifecycle-complement evidence; fresh affected cache regressions and the actual v12 live execution cover the changed runtime. Native mounts and new human UI authentication remain unclaimed.

Final normal broad gate passes21143/152 in301.69s with89.46% full-source coverage and fresh coverage-v12-final.xml. The whole current-main PR changed-line gate covers2460/2706=90.9090909%, independently matched by the owner; the final clean committed gate must match after the documentation-only amendment. Source/code/test hashes stay frozen; this validation append is separately hashed.


### Approved additive native timing recovery (2026-10-08)

Normal run37759083174 at e91c3975a5 passes Linux tests, coverage, both Windows compatibility jobs and two full Windows shards. Four full Windows jobs hit the unchanged fifteen-minute deadline. Available JUnit records have no test failures; the cancelled run is incomplete. Its six native manifests select all21,287 eligible IDs exactly once.

The user approved the prepared data-only patch and normal CI. It adds142 previously unmodeled completed passing native call estimates of at least1 second, preserving all1,687 existing values, default1, model settings, budgets, workers, scripts, headroom and job limits. The model now has1,829 entries (SHA256907e8faa085a2d5c9f9762e7297a9f4d267b6c5bb7b112ec4121435b6889b424). Each addition has exact recorded source hashes, native manifest membership and passed JUnit/call-phase timing artifact provenance. Dummy collector, skipped, unfinished and unknown cases are excluded.

Fresh Linux collection remains21,295 eligible cases. Before/after assignments preserve exact once-only coverage of all21,287 previously observed native eligible IDs across six shards. Partial historical call-sum replay reduces the maximum known sum by12.58%;4,236 eligible durations remain unknown or untrusted, including the entire missing3,551-case shard. Allocation arithmetic is not a job-walltime prediction or proof of a deadline cure. New exact-head normal CI remains the merge gate.

All693 frozen runtime/config hashes and all773 runtime files are unchanged from v12. The definitive v12 full-suite, Python3.11, static/type, whole-PR coverage, actual live finish and released Ticket evidence is therefore explicitly reused for unchanged code. This recovery changes only the timing model and this validation record. Source/test scripts, model defaults and test budgets are unchanged; no extra external review cycle or new runtime execution is claimed.
