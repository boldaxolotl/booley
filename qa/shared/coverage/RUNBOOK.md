# Native coverage QA execution recipes

These assets implement the maintainer-approved coverage proposal. Scenario YAML
owns all Checks, memberships, dependencies, budgets and evidence capture paths.
This file supplies shared stimulus and reversal mechanics; it is not a second
coverage index or a qualification runner. Execute only through `booley-qa-run`.

## Fixed inputs and preparation

Use the immutable installed candidate and its issued Linux Session Runtime on the
configured Ubuntu or Windows host. Record package/source/docs/image, Verilator
5.052 commit `ea338be98e1e838d3518809ce8899f85a009963c`, Yosys, gcc, Python,
provider CLI/SDK and exact Specialist model identities. Codex and Claude runs are
separate. Backend fixture protocol drift is a fixture failure, never a live-model
pass. Native compilation occurs only in the Session Runtime.

Create a fresh ledgered Project. Copy `project/` as source, initialize using the
public Booley route, and install `project/project-data/tests.toml` in the directory
resolved by the product's Project configuration. Preserve initialization's other
configuration. The detached-data waiver case uses a separately initialized
project-data repository and its resolved root; never assume `.booley_project`.
Before any Ticket input is sealed, commit this exact fixture locally. Keep
mutable report/fault roots separate from immutable captured evidence.

The Ticket Scenario replaces only its registry with `tickets/tests.toml` at setup:
`sim_generated` then has exactly `[gap]`, so its approved `tests: all` is exactly
that acceptance workload. Other Scenarios keep the diagnostic registry, including
explicitly skipped `full`, `fail`, and native/hook faults. No operator may edit
registry or threshold to make an already sealed Ticket pass.

`sim_generated` deliberately has no HDL `tb` tag and uses generated-main
classification. `sim_hdl` tags the same HDL driver as TB; `sim_custom` uses a
project-authored C++ main; `sim_cocotb` uses the two actual Python test functions.
Check the classification and resolved source closure, not just a Target name.
Only the dedicated `sim_toggle` Target has the eight-point overall toggle
denominator. Its driver settles zero before the start hook. Clock/reset/helper
points in other Targets cannot be silently omitted from their overall totals.

Compile `faults/boundary.c` into a new run-owned tool directory using
`gcc -shared -fPIC -O2 -Wall -Wextra -o <owned>/boundary.so <asset>/boundary.c -ldl`.
Never write `/etc/ld.so.preload`, replace installed binaries, or export injection
settings to a shell shared with unrelated work. All prefixes, suffixes, executables
and control directories are recorded before use. No privileges or ptrace required.

## Named baselines, created independently in every Scenario Run

Each baseline Check depends only on setup; failure of another baseline cannot
prevent it. A baseline is never obtained from another Scenario Run. Retain exact
paths and hashes in that baseline's capture directory and a run-local path ledger.

| Check | Concrete stimulus and required starting truth |
|---|---|
| `baseline.native` | Ungated `booley flow sim --target sim_toggle --test half --coverage`: simulation pass, collection complete, not_requested, 4/8 toggle points. |
| `baseline.policy` | Publicly author/seal the fixed baseline policy in `baselines.json`: sim_properties3, tests [half], cover_property min_pct 66. Explicit collection yields exactly 2/3 and pass. Snapshot its current accepted Criteria/state and transaction IDs. Separate pristine drafts for invalid-policy cases use this same record. |
| `baseline.analysis` | Ungated `sim_hdl --test gap --coverage`: completed Campaign/Simulation, verified RTL and tagged TB source closure, eligible and unscored points. Run the real configured Analyst once on the returned exact path. Also collect sim_properties4 test half for property/source controls; retain both exact pointers distinctly. |
| `baseline.waiver` | Ungated sim_waiver test run, then formal proof and approved two-point binding below. Seal toggle min_pct 1, exact [run]; recollect with the approved set and retain a passing Campaign with two waived bit-zero points. Also retain the separate excluded-value example on sim_properties4 half, using its two fixed approvals and cover_property min_pct 100. |
| `baseline.retention` | Two fresh independent invocation numbers: sim_toggle test half, and sim_properties4 test half. Also one multi-Target invocation selecting those two Targets with their complete normal registry suites. Record each Target pointer separately; do not claim their differing suites are one common test list. Archive all bytes before pruning. |
| `baseline.custom` | Ungated sim_custom test gap with working start/write hooks; recovery for custom-window and native-data faults must rerun this exact affected harness. |
| `baseline.multi` | Ungated sim_multi test run: two coverage_dut instances from one RTL source and waiver_counter from another, plus tagged TB. Independently group all four metric inventories by source path. |
| `baseline.scale` | Ungated sim_scale test run; fixed 4096-instance Campaign for scope and evidence budget cases. No observed-size adaptation. |
| `baseline.analysis-fail` | Seal sim_properties3 tests [half], cover_property min_pct 100; collect half: 2/3 fail, simulation pass. |
| `baseline.analysis-blocked` | Seal sim_properties3 tests [half, full], cover_property min_pct 66; explicitly collect only half: valid retained collection but policy blocked by missing required full test. |
| `baseline.sim-fail` | Seal sim_custom tests [fail], cover_property min_pct 66; collect fail: completed failing simulation with valid native coverage. |


Large-Campaign consumers compile and collect fixed `sim_scale` locally in their
own Scenario Run. Charge the owning Check's 15-minute allowance for this build
and analysis; no cached Campaign from a storage Scenario may substitute. The
4,096-instance workload is fixed and should be admitted with 8 GiB runtime memory
and 20 GiB disposable disk headroom; absence blocks admission, not a smaller run.

## Exact arithmetic and policy fixtures

`expected.json` is authored from literal transitions and equality properties.
The numeric oracle requires exact symbolic point population, per-test hit sets and exact transition/sample counts,
positive native incidence, numerator/denominator, and reported overall percentage.
Its `--native RUN_ID=PATH` inputs must cover every Campaign run. Point identities
must also be present in the actual raw native records. Source rollups apply only
to line, branch, expression and toggle. Property cases verify exact property
points, run incidence and overall `cover_property` totals.

`policies.json` supplies every metric case's Target, exact suite, literal threshold,
expected numerator/denominator and verdict. The `sim_custom` gap stimulus runs 18
post-reset edges alternating choices 0/1: line 5/7, branch 2/4, expression 2/3.
The line points at the unvisited choice-2/default arms are unhit; the reset and
both-bits-true branch outcomes are unhit. The AND expression's native short-circuit
population has three points, not four invented truth-table bins. Its two false
outcomes are hit; its true outcome is not. Thresholds are respectively 70, 50,
66. The AND-policy case uses line 70 and branch 51: line passes, branch fails.
Toggle uses the isolated half stimulus, 4/8 at 50; property uses 2/3 at 66.
Rounding uses exactly 2/3 at 66.67 and fails. These fixed counts are checked against
raw native evidence and the control-flow specification, not automatically updated
from whatever the product emits.

For the four verdict combinations, use sim_custom exact test [gap] or [fail];
both produce 2/3 cover properties. Preauthor thresholds 66 (coverage pass) and
100 (coverage fail). `gap` is simulation pass; `fail` emits the failure sentinel
after valid write and returns nonzero. For blocked evaluation, explicitly invoke
test [gap] against a separate sealed required suite [full], at threshold 100.
Only this mismatch is the seeded fault. Restoration returns to the fixed valid
suite; it cannot lower the failing case's threshold. Multi-Target precedence uses
separate Targets cloned from the fixed recipes at setup, with exact identities
`sim_pass`, `sim_miss`, `sim_collector_error`; registry tests are [gap], [gap],
[native-missing], respectively. Thresholds are 66, 100, 66 cover_property.

Public authoring negatives change just the field named by their Check. Empty
targets/tests/metrics are literal empty lists/maps; unknown test is `not_registered`.
Threshold values are 0, -1, 100.01, true, .nan, .inf and string "90", separately.
Legacy names are rejected as authored, never translated. Unknown metric tests
use `fsm` and zero-eligible tests use a separate no-property toggle Target with
a requested `cover_property` metric. Retain the validator's raw response.

## Approved fixture waivers and proof

`approval-inputs.json` is the literal approval: exact Target/source, two selector
identities, reason, justification, approving-role provenance, timestamp and
approval reference. It is not permission for an operator or Analyst to choose
another exclusion. `excluded` selects properties4 source lines 4/5 (values 2/3).
`unreachable` selects only sim_waiver bit-zero rising/falling points. If either
point is absent, covered, duplicated or optimized away, the fixture is not ready;
do not replace it with a different point.

Run `yosys -s <asset>/proof/parity.ys` from the fixture Project root and retain
full stdout/stderr, exit, tool identity and source/script hashes. The command
proves `value[0] == 0` by temporal induction from zero initialization under the
parity-preserving +2 transition. It must exit zero and prove induction. Copy its
log to the owned approved directory's `proof/parity.log` **before** invoking
`faults/approvals.py unreachable <campaign> --project <project> --directory
<approval-dir> --inputs <asset>/approval-inputs.json`. The binder only fills the
exact retained point IDs and byte digests, and refuses replacement output files.
Retain the proof/script/source identities alongside its resulting TOML.

Repeat the exact binding for `excluded` with the separate properties4 Campaign.
The directory anchors are explicitly `rtl_repository` or `project_data_repository`;
the relative directory is `qa-approved-waivers`. Gated collection loads the entire
set. Invalid-set cases start from both valid unreachable approvals on sim_waiver
and alter one named field, preserving the second valid approval to prove that no
subset is applied. Every corruption is made in a separate owned directory copy.
The excluded-with-proof case changes one reason to excluded while retaining proof.
For ungated bypass, use a non-readable owned approval directory with a malformed
file and preserve its contents/permissions before and after.

## Filesystem publication faults: precise interception and reversal

`faults/filesystem.py` launches only the given public command in a fresh process
group with the shared library in its environment. The library intercepts link,
linkat, rename, renameat, renameat2, unlink, unlinkat and rmdir; *at paths resolve
through their directory descriptors. It matches the declared owned root, operation
and exact suffix. It writes/fsyncs an event containing operation, source and
destination, then waits at most 20 seconds for the controller's answer. A shared
exclusive `consumed` marker permits exactly one EIO across descendants.

| Check family | Operation / destination / controller gate |
|---|---|
| Campaign publication | link to exact Target `/coverage.json`; fail before link. Point store publication has already completed. |
| Simulation publication | rename to exact Target `/simulation.json`; fail before replacement. |
| Acceptance state | rename to the captured Ticket `/booley_state.json`; fail only when temporary JSON adds an acceptance transaction absent from the baseline state. |
| Terminal progress | rename to exact invocation `/progress.json`; fail only when temporary JSON has `complete: true`. Earlier observational writes pass through. |
| Native-prune interruption | unlink of a recorded raw/merged payload beneath that Target's `.native-pruned/`, after native rename and journal. Select a concrete relative suffix from the recorded deletion set. |
| Full-prune interruption | unlink of a recorded payload beneath exact `.pruned-N/`, excluding `.prune.json`, after invocation rename and journal. |

Capture the source temporary JSON while the syscall is paused, the consumed event,
exit status and before/after reports/state. A missing handshake is fixture failure,
not proof of the intended persistence behavior. Shared publication-abort uses
three declared Targets and fails the first Simulation publication. To test actual
producer interruption, use the same paused Campaign-link boundary, terminate and
reap only that producer group before replying, and retain the interruption record;
an EIO return alone does not satisfy the interruption Check.

Restoration is a different Step: disable injection by launching a fresh command
without its environment; restore only copied fixture inputs, verify hashes and
original permissions, then run the corresponding valid public operation.
Publication retry means a **new invocation number**, never resuming a partial
Campaign. Pruning retry means the **same exact selection**, preserving journals,
quarantine and tombstones. Never delete a lock to bypass active production.
Do not restore acceptance by writing state files; the valid public Flow produces
new authoritative evidence. Keep every failed transaction in the archive.

## Native and input corruption recipes

Native missing/stale/malformed/incompatible cases run `sim_custom` with the exact
registered test `native-missing`, `native-stale`, `native-malformed`, or
`native-incompatible`. The fixture main first executes the real simulation and
coverage hook, then deletes its own raw file, sets its mtime to the epoch, writes a
malformed record, or writes an incompatible header, respectively. These are
explicit controlled native inputs, not real EDA-success evidence.
`native-fsm`, `native-covergroup`, `native-unknown` append a record with that
native class using an existing source/hierarchy identity; expected unscored or
capability/finding behavior is judged independently. They make no claim that the
compiler supports generating those native classes.

For merged equivalence, place a run-owned `verilator_coverage` forwarding wrapper
first on the Flow process PATH. Forward the recorded real executable and all argv.
Only after a successful `--write <merged> ...` call, change the first native count
by +1 in that exact owned merged file, retaining raw databases unchanged. Restore
the original executable selection before a fresh invocation.
For tool-version rejection, a process-local `verilator` wrapper forwards every
operation except `--version`, which prints Verilator 5.050. Never edit the real
compiler or source provenance. Pre-sim drift cases use the documented per-test
pre-sim command to append a harmless comment to exactly `rtl/coverage_dut.sv`
or `coverage.core` after planning; capture the before bytes and restore them.
Use a declared included `rtl/constants.svh` with a comment-only change for the
include-source analysis case; it must be in the recorded closure before collection.

Campaign fault cases copy an entire completed numbered invocation into a new
owned reports root, preserving `sim/N/targets/<target>/coverage.json` layout.
Create `.qa-coverage-fault-copy` at that new owned root and invoke
`faults/campaign.py <mode> <copied-campaign> --owned <copy-root>`. The original
Campaign is never mutated. Truncation and final-record cases update unrelated
envelope digests/sizes to reach the intended deeper validation. Retain the
particular error code/field proving that boundary; an unrelated earlier parse
error cannot earn a deep-validation Check. Restore by discarding only that copy,
recreating it from the original capture, and successfully analyzing the valid pair.

Analyst path faults use the exact invalid form in their ID. Missing-argument
omits --campaign; target-name uses sim_hdl; latest uses literal latest;
point-store and waveform use the actual owned gzip and VCD paths; legacy-flat-report
uses a real run-owned JSON file named coverage_report.json. Wrong Target/invocation
cases relocate the valid pair under a different encoded Target or number without
rewriting its identity. Simulation faults remove its projection, set completion to
incomplete, or swap in another Target's projection. Source-mode cases change the
named closure file or replace it with an owned sentinel symlink; restore the full
original closure before the separate verified-source rerun.

## Provider boundary fixture, real evidence MCP and live isolation

The simulated-provider cases are deterministic **external input** tests. Their
reports cannot satisfy a real-model Check. Live closure/advice/source modes use the
configured provider directly and record its genuine transcript and unchanged
Campaign, Project, Criteria and waiver bytes. Model instruction for a gap is:

> Begin with overview of this exact Campaign. Retrieve uncovered eligible points
> for rtl/coverage_dut.sv and verified excerpts for decoder choice 2. Separate
> immutable observations from hypotheses. Recommend a concrete missing test using
> only delivered references. State retrieval limitations. Do not run simulation,
> read waveforms, edit files or Criteria, or approve waivers.

For Codex, create an executable owned launcher named `codex` invoking
`faults/provider.py`, ahead of the original on **only that Analyst process's** PATH.
Keep its configured model and a private copy of exact-model metadata. Booley
creates the private CODEX_HOME/catalog; the fixture reads that generated
`config.toml`'s `mcp_servers.booley` command/args/env. It drains the actual prompt
and emits Codex NDJSON agent_message and turn.completed/turn.failed envelopes.
Malformed-output tests corrupt agent_message text, not the outer event envelope.

Claude SDK prefers its bundled executable before PATH. Record that exact resolved
path and binary digest. Set process-only QA_CLAUDE_EXECUTABLE to it and
QA_CLAUDE_SHIM to a recorded executable launcher for provider.py, with the library
in LD_PRELOAD. The shim redirects only that exact executable's stream-json call;
the real version probe passes through. The SDK and installed executable bytes are
unchanged. The fixture responds to the received initialize control request ID,
reads the user prompt, and emits the SDK's result envelope. Pin actual SDK and
protocol identities in run evidence. A PATH-only Claude substitution earns no
fault credit.

Set QA_PROVIDER_ROOT to a newly created owned evidence directory and
QA_PROVIDER_CASE to the Check's named case. The shim reads the **product-generated**
MCP specification: Codex replaces the environment with the supplied map; Claude
merges its supplied map over inherited environment. The real server is launched
from the installed package. It receives initialize, notifications/initialized,
tools/list and actual tools/call requests. Discovery must expose exactly
coverage_evidence. The fixture never fabricates a point response or writes the
audit; it copies the real server's audit after closing/reaping it. Keep the actual
MCP request/response transcript, including normal-envelope rejection objects.
The fixture code documents both provider envelopes and the bounded JSON-RPC flow.

Candidate cases request the matching disposition: non-RTL/unscored uses tagged
TB points from baseline.analysis, waived uses baseline.waiver, observed-hit uses
a covered eligible point and the same nonempty proof reference as the positive
unreachable control; unreachable/missing-proof uses an unhit eligible point.
Missing-source first applies the explicit source-stale fixture. Missing-evidence
uses an empty evidence string; invalid-reason uses literal invalid; duplicate
repeats the exact same delivered candidate; unknown/invented-reference uses
point:999999999, which must not have been delivered. Ready-for-human-review
unreachable requests cite proof/parity.log but never confer approval.

Query cases use overview, metric=branch, source=rtl/coverage_dut.sv, covered
true/false, and disposition eligible/waived/unscored explicitly. Pagination limit
is 2, then follow actual next_cursor. Changed-filter uses toggle then line;
cursor-filter reuses a toggle cursor with line. Undelivered requests use point:999999999. Cross-Campaign requests instead set
QA_FOREIGN_CAMPAIGN to the actual baseline.native Campaign and send its path as
a prohibited extra selector while the Analyst is bound to baseline.analysis.
Record rejection and prove the bound Campaign is unchanged. Budget exhaustion issues repeated
100-point pages of the fixed large Campaign, bounded at 256 calls; stop only at
the real evidence error and preserve the actual terminal audit. Oversized-query
validation and actual response-byte limits are distinct: request limit 100000
for invalid-limit rejection, and request legal 100-point pages with long retained
identities for response-byte checking. No invented empty success is accepted.

For every provider fault, the restoration Step removes process-only redirection,
restores the original exact executable/metadata selection, verifies no surviving
nested server, then performs a fresh successful **real** Analyst call. No three
clean-run requirement or hidden retry rule is introduced. Each call has a
three-minute model-work ceiling plus one minute for startup, evidence and cleanup;
timeout is retained as failed/blocked work, never a smaller substituted question.

## Schedule, independent recovery and cleanup

The authoring expansion assigns every matrix case and restoration its own ID and
Step. Most slices reserve at most 260 exercise/restoration minutes; the boundary
slice reserves 270 to include a separate restoration and live Analyst rerun after
every provider substitution. All remain within the same 480-minute deadline:
2 minutes per small native/boundary operation, 4 minutes per Analyst operation,
15 minutes for a large compilation/analysis. Every slice independently reserves
52 preparation minutes (including five separately recorded oracle controls and
catalog discovery), 60–96 baseline minutes (75 for the scale boundary slice),
24 final regression/Analyst/archive minutes and 30 cleanup minutes. Each Scenario
declares only the additional local baselines its checks consume. Its remaining
480-minute allocation is explicit contingency for source/tool/provider recovery.
No elapsed allowance proves the Scenario fits on an unmeasured host; exhaustion
records blocked work and enters cleanup. All configured variants remain required.

Restoration references the successful baseline and the detection ID in recovery
metadata, but does not require the detection to pass. Other faults use different
disposable copies. Final cleanup has no success prerequisite: reap simulators,
clients, nested servers and controller groups; release runtimes/attachments,
inventory/grants, fault mounts and scarce storage. Keep only inert eligible
review state with owner/path/deletion instructions. Prove borrowed compiler,
credentials, shared images/cache and unrelated Project state unchanged. Product
pruning Checks still perform their exact deletions before any final retention.
No push, PR, issue submission or report publication is authorized by a Scenario.


## Fixed staging and evaluator controls

For collect.pre-sim, merge the hashed project-data/staging.toml section into the
resolved Project configuration. Collect sim_staged tests half and upper. The
public pre-sim command writes each literal vector to BOOLEY_RUN_CWD/qa-vector.txt;
the driver rejects a missing file, wrong test name or wrong values and actually
uses the staged values. Native incidence must match expected.json staged-union:
4/8 per test and complementary union 8/8. Restore the prior config afterward.
The included rtl/constants.svh is declared as an include file in coverage.core;
source drift changes its comment only after a pristine Campaign is retained.

Run evaluator/controls.py separately with positive, hit-count, source-grouping,
digest and rational-verdict. These hand-authored minimal public-field pairs test
the independent evaluator; they are not substitute product Campaigns. Product
numeric checks still require the real canonical pair and every raw per-run file.

For model.incomplete the provider emits a partial event then exits normally
without a terminal Codex turn or Claude result. For model.failure-preserves,
inspect the recorded failed model.malformed-json call and its before/after state;
do not run another model. Query response-budget uses legal limit 100 and the
scale Campaign; invalid-limit is its own negative Check. Archive actual MCP text
bytes, continuation and real audit. A schema-range error earns no response-byte
credit. All pagination/exact-reference/source sequences must occur within the
same fresh Analyst call so the references were actually delivered in that session.

Ticket consumers require their named local producers. The stale-source case uses
a separate publicly authored disposable Ticket with the final improved TB and a
fresh passing baseline; restoration reruns sim_generated gap in that same Ticket.
The separate Ticket registry changes only sim_generated to [gap], preserving all
other Target registries needed by the Scenario. Native/full retention consumers
require their exact completed prune operation and never substitute a fresh,
unpruned Campaign. Keep detection failure and restoration result separately.


The separate collect.pre-sim-cocotb Check uses sim_cocotb_staged gap/full under
the same staging configuration. The command runs once for the selected batch,
writes both literal per-test vectors, and each Cocotb test reads and checks its
own named vector. Preserve the batch-stage log and separate native run evidence.
The negative control removes full from the script's written vectors (after
validating the same selected list), then the full test must fail loading it;
restore script/config bytes and rerun both tests. Never count a pre-sim schema
error as proof of missing-vector consumption.

Intentional context-exhaustion or incomplete-response injection is allowed only
after successful actual MCP initialization and point delivery. A preceding
protocol/setup error remains an error with boundary-error.json and must not earn
the expected model-fault credit. Compare response budgets using compact UTF-8
JSON with ensure_ascii=False and comma/colon separators; ordinary pretty-printed
file size is not the product's byte measure. publish.restart requires both the
observed interrupted producer and its separately reachable restoration.


## Review regression boundaries

Numeric oracle inputs contain only the pinned, supported native record classes.
The oracle requires equal raw/normalized native-key inventories on every run;
unknown classes or extra records fail this numeric fixture rather than disappearing
from its denominator. Taxonomy injection Checks separately inspect unrecognized
record evidence and never reuse numeric oracle success as taxonomy qualification.

policy.unscored-generated and policy.unscored-foreign collect sim_custom tests
native-generated and native-foreign, respectively. Each retains the normal raw
records and appends one supported record with a distinct source key. The first
creates qa-generated/coverage_helper.sv at runtime; the second references the
shipped foreign/coverage_library.sv. Both files are outside the Target's declared
source closure. Capture QA_NATIVE_ORIGIN, the file/hash and exact appended native
record, then require the normalized record to remain unscored and the eligible
RTL denominator to remain unchanged. The public projection need not expose an
internal generated/foreign enum; provenance comes from these fixed producers.
Archive/remove only the generated owned file and rerun baseline.custom on restore.

window.failed-hook runs as the non-root issued runtime user. It keeps the exact
BOOLEY_COVERAGE_FILE value, creates that new owned destination mode 0400 and calls
the real write hook. Record QA_WRITE_PROTECTED, uid/mode, simulator I/O error and
failed collection. A missing environment variable or setup failure earns no
write-failure credit. The independent restoration restores mode 0600 on that exact
owned file if it survives, archives it, and runs a fresh sim_custom gap Campaign.

The syscall controller captures attempted temporary publication bytes before any
failure reply, including gate=any. Shutdown checks surviving process-group members,
escalates to SIGKILL even after leader exit, and fails if executing members survive.
Campaign mutations reject symlinked or hard-linked input files before any write;
linked outside sentinels must remain byte-identical. Provider pipe transport tests
are explicitly Linux-only; Windows configurations exercise it in the Linux runtime.
