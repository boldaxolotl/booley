# Native coverage fixture recipes

These assets back the coverage mission (`qa/missions/coverage/MISSION.md`). The
mission says what to exercise; this file supplies the fixed stimulus, fault
injection and reversal mechanics it points to.

## Fixed inputs and preparation

Use the installed candidate and its issued Linux Sandbox on an Ubuntu or
Windows host. Note package/source/docs/image, Verilator 5.052 commit
`ea338be98e1e838d3518809ce8899f85a009963c`, Yosys, gcc, Python, provider
CLI/SDK and Specialist model identities. Run Codex and Claude separately.
Fixture protocol drift in the provider fixture is a fixture bug, never a
live-model pass. Native compilation happens only in the Sandbox.

Create a fresh disposable Project. Copy `project/` as source, initialize it
through the public Booley route, and install `project/project-data/tests.toml`
in the directory that the product's Project configuration resolves. Keep
initialization's other configuration. The detached-data waiver case uses a
separately initialized project-data repository and its resolved root; never
assume `.booley_project`. Commit the fixture locally before authoring any
Ticket. Keep mutable report and fault roots separate from saved evidence.

The coverage Ticket area replaces only the registry, with `tickets/tests.toml`:
`sim_generated` then has exactly `[gap]`, so its approved `tests: all` is
exactly that acceptance workload. All other areas keep the diagnostic registry,
including the explicitly skipped `full`, `fail`, and native/hook faults. Never
edit the registry or a threshold to make an already sealed Ticket pass.

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
settings to a shell shared with unrelated work. Note all prefixes, suffixes,
executables and control directories before use. No privileges or ptrace needed.

## Named baselines

Create each baseline fresh in the current run, independently of the others;
never reuse one from an earlier run. Note each baseline's exact report paths so
later cases can point at them. `baselines.json` holds the same Target/test/policy
values in machine-readable form. The mission refers to a row as `baseline.<name>`.

| Baseline | Concrete stimulus and required starting truth |
|---|---|
| `native` | Ungated `booley flow sim --target sim_toggle --test half --coverage`: simulation pass, collection complete, not_requested, 4/8 toggle points. |
| `policy` | Publicly author/seal the fixed baseline policy in `baselines.json`: sim_properties3, tests [half], cover_property min_pct 66. Explicit collection yields exactly 2/3 and pass. Snapshot its current accepted Criteria/state and transaction IDs. Separate pristine drafts for invalid-policy cases use this same record. |
| `analysis` | Ungated `sim_hdl --test gap --coverage`: completed Campaign/Simulation, verified RTL and tagged TB source closure, eligible and unscored points. Run the real configured Analyst once on the returned exact path. Also collect sim_properties4 test half for property/source controls; keep both exact pointers distinct. |
| `waiver` | Ungated sim_waiver test run, then formal proof and approved two-point binding below. Seal toggle min_pct 1, exact [run]; recollect with the approved set and keep a passing Campaign with two waived bit-zero points. Also keep the separate excluded-value example on sim_properties4 half, using its two fixed approvals and cover_property min_pct 100. |
| `retention` | Two fresh independent invocation numbers: sim_toggle test half, and sim_properties4 test half. Also one multi-Target invocation selecting those two Targets with their complete normal registry suites. Note each Target pointer separately; their differing suites are not one common test list. Copy all bytes aside before pruning. |
| `custom` | Ungated sim_custom test gap with working start/write hooks; recovery for custom-window and native-data faults reruns this exact harness. |
| `multi` | Ungated sim_multi test run: two coverage_dut instances from one RTL source and waiver_counter from another, plus tagged TB. Independently group all four metric inventories by source path. |
| `scale` | Ungated sim_scale test run; fixed 4096-instance Campaign for scope and evidence-budget cases. No observed-size adaptation. |
| `analysis-fail` | Seal sim_properties3 tests [half], cover_property min_pct 100; collect half: 2/3 fail, simulation pass. |
| `analysis-blocked` | Seal sim_properties3 tests [half, full], cover_property min_pct 66; explicitly collect only half: valid collection but policy blocked by missing required full test. |
| `sim-fail` | Seal sim_custom tests [fail], cover_property min_pct 66; collect fail: completed failing simulation with valid native coverage. |

Large-Campaign cases compile and collect `sim_scale` locally in the current run;
no cached Campaign substitutes. The 4,096-instance workload is fixed and needs
about 8 GiB runtime memory and 20 GiB disposable disk. Without that headroom,
skip the large-Campaign cases; never shrink the workload.

## Campaign paths

The Flow reports `<reports>/sim/<N>/targets/<target>/coverage.json`. That file is
a `booley.coverage-campaign-reference/v1` pointer, not the Campaign. Pass it
unchanged to the Coverage Analyst. The fixture scripts `evaluator/measurements.py`
and `faults/approvals.py` need the nested V3 manifest it points to:

```bash
python3 -c 'import json,sys,pathlib; r=pathlib.Path(sys.argv[1]); print(r.parent / json.loads(r.read_text())["coverage_campaign"]["path"])' <target>/coverage.json
```

`path_base` is `origin_target`: resolve against the Target directory that wrote
the reference. The result is `.../campaign/work-items/<item>/attempts/<attempt>/coverage-campaign/coverage.json`.
That Campaign directory holds `coverage-points.jsonl.gz`, `native/raw/`,
`native/merged/` and `hooks/`. Run ids are the manifest's `tests.runs[].id`
(`run:001:<test>`), not test names. Each run's raw database is the
`artifacts[]` entry whose `id` equals that run's `raw_artifact`, at a path
relative to the Campaign directory (`native/raw/001-<test>.dat`). For example:
`--native run:001:half=<campaign-dir>/native/raw/001-half.dat`.

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

Public authoring negatives change just the one named field. Empty
targets/tests/metrics are literal empty lists/maps; unknown test is `not_registered`.
Threshold values are 0, -1, 100.01, true, .nan, .inf and string "90", separately.
Legacy names are rejected as authored, never translated. Unknown metric tests
use `fsm` and zero-eligible tests use a separate no-property toggle Target with
a requested `cover_property` metric. Save the validator's raw response.

## Approved fixture waivers and proof

`approval-inputs.json` is the literal approval: exact Target/source, two selector
identities, reason, justification, approving-role provenance, timestamp and
approval reference. It is not permission for an operator or Analyst to choose
another exclusion. `excluded` selects properties4 source lines 4/5 (values 2/3).
`unreachable` selects only sim_waiver bit-zero rising/falling points. If either
point is absent, covered, duplicated or optimized away, the fixture is not ready;
do not replace it with a different point.

Run `yosys -s <asset>/proof/parity.ys` from the fixture Project root and save
full stdout/stderr, exit status and tool identity. The command proves
`value[0] == 0` by temporal induction from zero initialization under the
parity-preserving +2 transition. It must exit zero and prove induction. Copy its
log to the owned approved directory's `proof/parity.log` **before** invoking
`faults/approvals.py unreachable <campaign> --project <project> --directory
<approval-dir> --inputs <asset>/approval-inputs.json`. The binder only fills the
exact retained point IDs and byte digests, and refuses to replace existing output
files. Keep the proof log next to its resulting TOML.

Repeat the exact binding for `excluded` with the separate properties4 Campaign.
The directory anchors are explicitly `rtl_repository` or `project_data_repository`;
the relative directory is `qa-approved-waivers`. Gated collection loads the entire
set. Invalid-set cases start from both valid unreachable approvals on sim_waiver
and alter one named field, preserving the second valid approval to prove that no
subset is applied. Make every corruption in a separate owned directory copy.
The excluded-with-proof case changes one reason to excluded while keeping proof.
For ungated bypass, use a non-readable owned approval directory with a malformed
file and confirm its contents/permissions are the same before and after.

## Filesystem publication faults: precise interception and reversal

`faults/filesystem.py` launches only the given public command in a fresh process
group with the shared library in its environment. The library intercepts link,
linkat, rename, renameat, renameat2, unlink, unlinkat and rmdir; *at paths resolve
through their directory descriptors. It matches the declared owned root, operation
and exact suffix. It writes/fsyncs an event containing operation, source and
destination, then waits at most 20 seconds for the controller's answer. A shared
exclusive `consumed` marker permits exactly one EIO across descendants.

| Fault | Operation / destination / controller gate |
|---|---|
| Campaign publication | link to exact Target `/coverage.json`; fail before link. Point store publication has already completed. |
| Simulation publication | rename to exact Target `/simulation.json`; fail before replacement. |
| Acceptance state | rename to the captured Ticket `/booley_state.json`; fail only when temporary JSON adds an acceptance transaction absent from the baseline state. |
| Terminal progress | rename to exact invocation `/progress.json`; fail only when temporary JSON has `complete: true`. Earlier observational writes pass through. |
| Native-prune interruption | unlink of a recorded raw/merged payload beneath that Target's `.native-pruned/`, after native rename and journal. Select a concrete relative suffix from the recorded deletion set. |
| Full-prune interruption | unlink of a recorded payload beneath exact `.pruned-N/`, excluding `.prune.json`, after invocation rename and journal. |

Save the source temporary JSON while the syscall is paused, the consumed event,
exit status and before/after reports/state. A missing handshake is a fixture
failure, not proof of the intended persistence behavior. Shared publication-abort
uses three declared Targets and fails the first Simulation publication. To test
actual producer interruption, use the same paused Campaign-link boundary,
terminate and reap only that producer group before replying, and save the
interruption event; an EIO return alone is not an interruption.

Restoring is a separate step: disable injection by launching a fresh command
without its environment; restore only copied fixture inputs, confirm their bytes
and original permissions, then run the corresponding valid public operation.
Publication retry means a **new invocation number**, never resuming a partial
Campaign. Pruning retry means the **same exact selection**, preserving journals,
quarantine and tombstones. Never delete a lock to bypass active production.
Do not restore acceptance by writing state files; the valid public Flow produces
new authoritative evidence. Keep every failed transaction for inspection.

## Native and input corruption recipes

Native missing/malformed/incompatible cases run `sim_custom` with the exact
registered test `native-missing`, `native-malformed`, or `native-incompatible`.
The fixture main first executes the real simulation and coverage hook, then
deletes its own raw file, writes a malformed record, or writes an incompatible
header, respectively. These are explicit controlled native inputs, not real
EDA-success evidence. There is no stale-native case. Booley reports a raw file as
stale only when it is unchanged from its pre-run snapshot. A raw file created by
this run at its per-attempt path is fresh even with an old mtime, by design.
`COV_RAW_FILE_STALE` is not exercised by this mission.
`native-fsm`, `native-covergroup`, `native-unknown` append a record with that
native class using an existing source/hierarchy identity; judge the expected
unscored or capability/finding behavior independently. They make no claim that
the compiler supports generating those native classes.

For merged equivalence, place a run-owned `verilator_coverage` forwarding wrapper
first on the Flow process PATH. Forward the recorded real executable and all argv.
Only after a successful `--write <merged> ...` call, change the first native count
by +1 in that exact owned merged file, leaving raw databases unchanged. Restore
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
`faults/campaign.py <mode> <copied-target>/coverage.json --owned <copy-root>`, passing
the copied Target reference. The script follows the reference to the nested
Campaign, mutates it, writes point lines and the manifest in Booley's canonical
compact encoding, and rebinds the reference's `bytes`/`sha256`. The original
Campaign is never mutated. Truncation and final-record cases update unrelated
envelope digests/sizes to reach the intended deeper validation. Note the
particular error code/field proving that boundary; an unrelated earlier parse
error does not show the deeper validation works. Restore by discarding only that
copy, recreating it from the original, and successfully analyzing the valid pair.

Analyst path faults use the exact invalid form in their name. Missing-argument
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
reports never stand in for a real-model result. Live closure/advice/source modes
use the configured provider directly; save its genuine transcript and confirm
the Campaign, Project, Criteria and waiver bytes are unchanged. Model instruction
for a gap is:

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

Claude SDK prefers its bundled executable before PATH. Note that exact resolved
path. Set process-only QA_CLAUDE_EXECUTABLE to it and QA_CLAUDE_SHIM to an owned
executable launcher for provider.py, with the library in LD_PRELOAD. The shim
redirects only that exact executable's stream-json call; the real version probe
passes through. The SDK and installed executable bytes are unchanged. The
fixture responds to the received initialize control request ID, reads the user
prompt, and emits the SDK's result envelope. Note the actual SDK and protocol
versions. A PATH-only Claude substitution does not exercise the boundary.

Set QA_PROVIDER_ROOT to a newly created owned evidence directory and
QA_PROVIDER_CASE to the case name. The shim reads the **product-generated**
MCP specification: Codex replaces the environment with the supplied map; Claude
merges its supplied map over inherited environment. The real server is launched
from the installed package. It receives initialize, notifications/initialized,
tools/list and actual tools/call requests. Discovery must expose exactly
coverage_evidence. The fixture never fabricates a point response or writes the
audit; it copies the real server's audit after closing/reaping it. Keep the actual
MCP request/response transcript, including normal-envelope rejection objects.
The fixture code documents both provider envelopes and the bounded JSON-RPC flow.

Candidate cases request the matching disposition: non-RTL/unscored uses tagged
TB points from the `analysis` baseline, waived uses the `waiver` baseline,
observed-hit uses a covered eligible point and the same nonempty proof reference
as the positive unreachable control; unreachable/missing-proof uses an unhit
eligible point. Missing-source first applies the explicit source-stale fixture.
Missing-evidence uses an empty evidence string; invalid-reason uses literal
invalid; duplicate repeats the exact same delivered candidate;
unknown/invented-reference uses point:999999999, which must not have been
delivered. Ready-for-human-review unreachable requests cite proof/parity.log but
never confer approval.

Query cases use overview, metric=branch, source=rtl/coverage_dut.sv, covered
true/false, and disposition eligible/waived/unscored explicitly. Pagination limit
is 2, then follow actual next_cursor. Changed-filter uses toggle then line;
cursor-filter reuses a toggle cursor with line. Undelivered requests use
point:999999999. Cross-Campaign requests instead set QA_FOREIGN_CAMPAIGN to the
actual `native` baseline Campaign and send its path as a prohibited extra
selector while the Analyst is bound to the `analysis` baseline. Note the
rejection and confirm the bound Campaign is unchanged. Budget exhaustion issues
repeated 100-point pages of the fixed large Campaign, bounded at 256 calls; stop
only at the real evidence error and keep the actual terminal audit.
Oversized-query validation and actual response-byte limits are distinct: request
limit 100000 for invalid-limit rejection, and request legal 100-point pages with
long retained identities for response-byte checking. No invented empty success
is accepted.

After every provider fault, restore: remove process-only redirection, restore
the original executable/metadata selection, confirm no nested server survives,
then make a fresh successful **real** Analyst call. Each call has a three-minute
model-work ceiling plus one minute for startup, evidence and cleanup; a timeout
is a failure to report, never a reason to ask a smaller question.

## Restoration and cleanup

Restoration never depends on the fault case having behaved as expected; each
fault uses its own disposable copy. Cleanup always runs: reap simulators,
clients, nested servers and controller groups; release runtimes/attachments,
inventory/grants, fault mounts and scarce storage. Keep only inert review state,
listed in `resources.md` with owner, path and deletion instructions. Confirm the borrowed compiler,
credentials, shared images/cache and unrelated Project state are unchanged.
Pruning cases still perform their exact deletions. No push, PR, issue or
report publication.

## Fixed staging and evaluator controls

For pre-sim staging, merge the project-data/staging.toml section into the
resolved Project configuration. Collect sim_staged tests half and upper. The
public pre-sim command writes each literal vector to BOOLEY_RUN_CWD/qa-vector.txt;
the driver rejects a missing file, wrong test name or wrong values and actually
uses the staged values. Native incidence must match expected.json staged-union:
4/8 per test and complementary union 8/8. Restore the prior config afterward.
The included rtl/constants.svh is declared as an include file in coverage.core;
source drift changes its comment only after a pristine Campaign is saved.

Run evaluator/controls.py separately with positive, hit-count, source-grouping,
digest and rational-verdict. These hand-authored minimal public-field pairs test
the independent evaluator; they are not substitute product Campaigns. Product
numeric cases still require the real canonical pair and every raw per-run file.

For the incomplete-model case the provider emits a partial event then exits
normally without a terminal Codex turn or Claude result. For the
failure-preserves-state case, inspect the earlier failed malformed-json call and
its before/after state; do not run another model. Query response-budget uses
legal limit 100 and the scale Campaign; invalid-limit is its own negative case.
Save the actual MCP text bytes, continuation and real audit. A schema-range
error says nothing about response-byte limits. All pagination/exact-reference/
source sequences must occur within the same fresh Analyst call so the references
were actually delivered in that session.

The stale-source Ticket case uses a separate publicly authored disposable Ticket
with the final improved TB and a fresh passing baseline; restoration reruns
sim_generated gap in that same Ticket. The separate Ticket registry changes only
sim_generated to [gap], preserving all other Target registries. Native/full
retention cases require their exact completed prune operation and never
substitute a fresh, unpruned Campaign. Report the fault result and the
restoration result separately.

The Cocotb pre-sim case uses sim_cocotb_staged gap/full under the same staging
configuration. The command runs once for the selected batch, writes both literal
per-test vectors, and each Cocotb test reads and checks its own named vector.
Keep the batch-stage log and separate native run evidence. The negative control
removes full from the script's written vectors (after validating the same
selected list), then the full test must fail loading it; restore script/config
bytes and rerun both tests. A pre-sim schema error does not show missing-vector
handling.

Intentional context-exhaustion or incomplete-response injection is allowed only
after successful actual MCP initialization and point delivery. A preceding
protocol/setup error remains an error with boundary-error.json and does not show
the intended model-fault handling. Compare response budgets using compact UTF-8
JSON with ensure_ascii=False and comma/colon separators; ordinary pretty-printed
file size is not the product's byte measure. The publish-restart case needs both
the observed interrupted producer and its separately reachable restoration.

## Review regression boundaries

Numeric oracle inputs contain only the pinned, supported native record classes.
The oracle requires equal raw/normalized native-key inventories on every run;
unknown classes or extra records fail this numeric fixture rather than disappearing
from its denominator. Taxonomy injection cases separately inspect unrecognized
record evidence and never reuse numeric oracle success.

The unscored-generated and unscored-foreign policy cases collect sim_custom tests
native-generated and native-foreign, respectively. Each keeps the normal raw
records and appends one supported record with a distinct source key. The first
creates qa-generated/coverage_helper.sv at runtime; the second references the
shipped foreign/coverage_library.sv. Both files are outside the Target's declared
source closure. Capture QA_NATIVE_ORIGIN, the file and exact appended native
record, then require the normalized record to remain unscored and the eligible
RTL denominator to remain unchanged. The public projection need not expose an
internal generated/foreign enum; provenance comes from these fixed producers.
Remove only the generated owned file and rerun the `custom` baseline on restore.

The failed-write-hook case runs as the non-root issued runtime user. It keeps the
exact BOOLEY_COVERAGE_FILE value, creates that new owned destination mode 0400
and calls the real write hook. Note QA_WRITE_PROTECTED, uid/mode, simulator I/O
error and failed collection. A missing environment variable or setup failure
does not exercise the write failure. Restoration sets mode 0600 on that exact
owned file if it survives, moves it aside, and runs a fresh sim_custom gap
Campaign.

The syscall controller captures attempted temporary publication bytes before any
failure reply, including gate=any. Shutdown checks surviving process-group members,
escalates to SIGKILL even after leader exit, and fails if executing members survive.
Campaign mutations reject symlinked or hard-linked input files before any write;
linked outside sentinels must remain byte-identical. Provider pipe transport tests
are Linux-only; Windows hosts exercise it in the Linux runtime.
