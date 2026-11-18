# Native coverage fixture recipes

This runbook gives the QA agent running the coverage mission the fixed stimulus,
fault-injection and restoration mechanics behind each area. The
[mission](../../missions/coverage/MISSION.md) says what to exercise and what
counts as a finding; this file says exactly how. The Simulation Record recovery
cases of area 6 are in
[simulation-record/RUNBOOK.md](simulation-record/RUNBOOK.md).

`<asset>` below is `qa/shared/coverage/` in the
Booley checkout under test.

## Fixed inputs and preparation

Use the installed candidate and its issued Linux Sandbox on an Ubuntu or
Windows host. Record the package, source, docs and image identities; the
Verilator version and commit (`VERILATOR_VERSION` and `VERILATOR_REF` in
`src/booley/data/docker/Dockerfile.base`); and the Yosys, gcc, Python,
provider CLI/SDK and Specialist model identities. Run Codex and Claude
separately. Native compilation happens only in the Sandbox. Sandbox commands
issued from the host go through `booley session enter -- booley ...`.

Create a fresh disposable Project. Copy `<asset>/project/` as its source,
initialize it through the public Booley route, and install
`project/project-data/tests.toml` in the Booley Directory that the Project
configuration resolves; never assume `.booley`. Keep initialization's other
configuration. The detached-data waiver case uses a separately initialized
Booley Directory's repository and its resolved root. Commit the fixture and initialized
Project files before creating a worktree. Keep mutable report and fault
roots separate from saved evidence.

Two registries exist. `project/project-data/tests.toml` is the diagnostic
registry every area uses, including the default-skipped `full`, `fail` and
native/hook fault tests. The test-gap closure (area 7) swaps in
`closure/tests.toml`, which differs only in `sim_generated`: exactly `[gap]`,
so its coverage table measures that workload. Never edit a registry or a bound
to get a met verdict.

Each harness Target tests a different classification, so check the
classification and the resolved source closure, not just the Target name:

| Target | Harness |
|---|---|
| `sim_generated` | Generated main; deliberately no HDL `tb` tag |
| `sim_hdl` | The same HDL driver, tagged `tb` |
| `sim_custom` | Project-authored C++ main with coverage hooks |
| `sim_cocotb` | Cocotb, with two Python test functions |
| `sim_toggle` | A clean eight-point toggle denominator (`rtl/toggle.sv`, which `sim_staged` also uses); its driver settles zero before the start hook |

Clock, reset and helper points in other Targets still count in their overall
totals; they cannot be silently left out.

Build the syscall fault library into a new run-owned tool directory:

```bash
gcc -shared -fPIC -O2 -Wall -Wextra -o <owned>/boundary.so <asset>/faults/boundary.c -ldl
```

Never write `/etc/ld.so.preload`, replace installed binaries, or export
injection settings to a shell shared with unrelated work. Record every
prefix, suffix, executable and control directory before use. No privileges or
ptrace are needed.

## Coverage bounds

Write each bound case as one `[<target>.coverage]` table with
`<metric>_min = N`, where `<metric>` is the coverage metric name
(`line_coverage`, `branch_coverage`, `expression_coverage`,
`toggle_coverage`, `cover_property_coverage`) and `N` is the fixture's
`min_pct`. Commit it on a run-owned branch and run `booley measure --all`. A
measurement runs the registered suite minus default skips, so a case whose
suite differs commits a registry with exactly that suite. These commits sit on
the fixture Project's default branch, which gives them no Base, so no rules
comparison is expected. Plain `booley flow sim --coverage` runs are the
collection route and carry no bounds.

## Named baselines

Create each baseline fresh in the current run, independently of the others;
never reuse one from an earlier run. Record each baseline's exact report paths
so later cases can point at them. `baselines.json` holds the same
Target/test/bound values in machine-readable form; the mission calls a row
`baseline.<name>`.

| Baseline | Concrete stimulus and required starting truth |
|---|---|
| `native` | Ungated `booley flow sim --target sim_toggle --test half --coverage`: simulation pass, collection complete, `not_requested`, 4/8 toggle points. |
| `policy` | Measure `[sim_properties3.coverage] cover_property_coverage_min = 66` (the bound in `baselines.json`) with the registry limited to `[half]`: exactly 2/3, met. Invalid-bound probes use separate disposable commits. |
| `analysis` | Ungated `sim_hdl --test gap --coverage`: completed Coverage Record and Simulation, verified RTL and tagged TB source closure, eligible and unscored points. Run the real configured Analyst once on the returned exact path. Also collect `sim_properties4` test `half` for property and source controls; keep both exact pointers distinct. |
| `waiver` | Ungated `sim_waiver` test `run`, then the formal proof and two-point approval binding in "Approved fixture waivers and proof". Recollect with the approved set and keep a Coverage Record with two waived bit-zero points. Also keep the separate excluded-value example on `sim_properties4` `half`, with its two fixed approvals and `cover_property` 100. |
| `retention` | Two fresh, independent invocation numbers: `sim_toggle` test `half`, and `sim_properties4` test `half`. Also one multi-Target invocation selecting those two Targets with their complete registry suites. Record each Target pointer separately; their suites differ and are not one common test list. |
| `custom` | Ungated `sim_custom` test `gap` with working start and write hooks. Recovery for custom-window and native-data faults reruns this exact harness. |
| `multi` | Ungated `sim_multi` test `run`: two `coverage_dut` instances from one RTL source and `waiver_counter` from another, plus a tagged TB. Group all four metric inventories by source path yourself. |
| `scale` | Ungated `sim_scale` test `run`: the fixed 4,096-instance Coverage Record for scope and evidence-budget cases. Never adapt it to the observed size. |
| `sim-fail` | Ungated `sim_custom` test `fail` with `--coverage`: a completed failing simulation with valid native coverage. |

Large-record cases compile and collect `sim_scale` locally in the current
run; no cached Coverage Record substitutes. The workload is fixed and needs
about
8 GiB of runtime memory and 20 GiB of disposable disk. Without that headroom,
skip the large-record cases; never shrink the workload.

## Coverage Record paths

The Flow reports `<reports>/sim/<N>/targets/<target>/coverage.json`, where
`<reports>` is the report root (`--report-dir`). That file is a
`booley.coverage-record-reference/v1` pointer, not the Coverage Record. Pass it
unchanged to the Coverage Analyst. The fixture scripts
`evaluator/measurements.py` and `faults/approvals.py` need the nested
Coverage Record manifest it points to (`booley.coverage-record/v1`):

```bash
python3 -c 'import json,sys,pathlib; r=pathlib.Path(sys.argv[1]); print(r.parent / json.loads(r.read_text())["coverage_record"]["path"])' <target>/coverage.json
```

`path_base` is `origin_target`: the path resolves against the Target directory
that wrote the reference. The result is
`.../record/work-items/<item>/attempts/<attempt>/coverage-record/coverage.json`.
That Coverage Record directory holds `coverage-points.jsonl.gz`, `native/raw/`,
`native/merged/` and `hooks/`. Run IDs are the manifest's `tests.runs[].id`: the
run ordinal plus the [Test Run](../../../docs/GLOSSARY.md) name, not the bare
test name. Every Verilator Target here uses Default Seed 1, so test `half` is
`run:001:half@1`. Each run's raw database is the `artifacts[]` entry whose
`id` equals that run's `raw_artifact`, at a path relative to the Coverage Record
directory (`native/raw/001-half@1.dat`). Read both from the manifest rather
than guessing. For example:
`--native run:001:half@1=<record-dir>/native/raw/001-half@1.dat`.

## Exact arithmetic and policy fixtures

`expected.json` is written by hand from literal transitions and equality
properties, so the oracle never trusts what the product emits. Run it as:

```bash
python3 <asset>/evaluator/measurements.py <nested>/coverage.json \
  --expected <asset>/expected.json --case <case> --native <run-id>=<raw-path> ...
```

The oracle requires the exact symbolic point population, per-test hit sets,
exact transition and sample counts, positive native incidence, the
numerator/denominator, and the reported overall percentage. Its
`--native RUN_ID=PATH` inputs must cover every Coverage Record run, and every
point
identity must also appear in the actual raw native records. Its inputs contain
only the pinned, supported native record classes, and it requires equal raw and
normalized native-key inventories on every run: an unknown class or an extra
record fails the numeric case rather than dropping out of its denominator.
Taxonomy-injection cases inspect unrecognized records separately and never
reuse numeric-oracle success. Source rollups apply only to line, branch,
expression and toggle. Property cases check exact property points, run
incidence and overall `cover_property` totals.

Before relying on the oracle, run `python3 <asset>/evaluator/controls.py <mode>`
for each of `positive`, `hit-count`, `source-grouping`, `digest`. These
hand-written minimal pairs test the independent evaluator only; they do not
stand in for product Coverage Records, and product numeric cases still need the
real reference/manifest pair and every raw per-run file.

`policies.json` gives every metric case's Target, exact suite, literal
bound, expected numerator/denominator and verdict. The `sim_custom` `gap`
stimulus runs 18 post-reset edges alternating choices 0 and 1, giving line
5/7, branch 2/4 and expression 2/3:

- The line points on the unvisited choice-2 and default arms are unhit.
- The reset and both-bits-true branch outcomes are unhit.
- The AND expression's native short-circuit population has three points, not
  four invented truth-table bins. Its two false outcomes are hit; its true
  outcome is not.

Bounds are 70 (line), 50 (branch) and 66 (expression). The AND case
uses line 70 and branch 51: line passes, branch fails. Toggle uses the isolated
`half` stimulus, 4/8 at 50; property uses 2/3 at 66. Rounding uses exactly 2/3
at 66.67, which is unmet. These counts are checked against raw native evidence
and the control-flow specification, never updated from what the product
emits.

For the verdict combinations, use `sim_custom` with the registry limited to
`[gap]` or `[fail]` (both produce 2/3 cover properties), a bound of 66 or 100,
and the default `tests_failed_max = 0`. `gap` is a simulation pass; `fail`
emits the failure sentinel after a valid write and returns nonzero. Expected:
gap/66 exit 0; gap/100 exit 1; fail/66 and fail/100 exit 1. Restoration
returns to the fixed valid registry; it never lowers the failing case's bound.
Multi-Target precedence uses the Targets `sim_pass`, `sim_miss` and
`sim_collector_error` that ship in `coverage.core` and the registry, with
registry tests `[gap]`, `[gap]` and `[native-missing]` and
`cover_property_coverage_min` bounds 66, 100 and 66.

`booley measure --validate` negatives go in one file listing them all at once:
a string `"90"`, `fsm_coverage_min`, an unquoted dotted key and a relative
bound. Record whether the bounds 0, -1 and 100.01 load (a `_min` above 100
can never be met). A zero-eligible bound is a `cover_property_coverage_min` on
a Target with no properties: it is an `error`, never a vacuous met.

Save the validator's raw response.

## Pre-sim staging

For the pre-sim staging case, merge `project/project-data/staging.toml` into
the resolved Project configuration; it adds a `[flows.sim]`
`pre_run_commands` entry that runs `scripts/stage_vector.py`. Collect
`sim_staged` tests `half` and `upper`. The command writes each literal vector
to `$BOOLEY_RUN_CWD/qa-vector.txt`; the driver rejects a missing file, a wrong
test name or wrong values, and really uses the staged values. Native incidence
must match the `expected.json` case `staged-union`: 4/8 per test and a
complementary union of 8/8. Restore the prior configuration afterward.

The cocotb case uses `sim_cocotb_staged` tests `gap` and `full` under the same
configuration. Under `--coverage` each cocotb Test Run runs in its own
simulator process, so the command runs once per test with that one name in
`BOOLEY_TEST_NAMES`; each cocotb test reads and checks its own named vector.
Keep both stage logs and the separate native run evidence. The
negative control removes `full` from the vectors the script writes (after it
validates the same selected list); the `full` test must then fail loading it.
Restore the script and configuration bytes and rerun both tests. A pre-sim
schema error does not show missing-vector handling.

## Approved fixture waivers and proof

`approval-inputs.json` is the literal approval: the exact Target and source,
two selector identities, reason, justification, approver, timestamp and
approval reference. It does not let an operator or the Analyst choose another
exclusion. `excluded` selects `sim_properties4` source lines 4 and 5 (values 2
and 3); `unreachable` selects only the `sim_waiver` bit-zero rising and
falling points. If either point is absent, covered, duplicated or optimized
away, the fixture is not ready; never replace it with a different point.

Run `yosys -s <asset>/proof/parity.ys` from the fixture Project Root and save
full stdout and stderr, the exit status and the tool identity. The script
proves `value[0] == 0` by temporal induction from zero initialization under
the parity-preserving +2 transition; it must exit zero and prove induction.
Copy its log to the owned approval directory's `proof/parity.log` **before**
binding:

```bash
python3 <asset>/faults/approvals.py unreachable <nested>/coverage.json \
  --project <project> --directory <approval-dir> --inputs <asset>/approval-inputs.json
```

The binder only fills in the exact retained point IDs and byte digests, and
refuses to replace existing output files. Keep the proof log next to the TOML
it produces. Repeat the binding for `excluded` with the separate
`sim_properties4` Coverage Record.

The directory anchors are exactly `rtl_repository` or
`project_data_repository`, and the relative directory is
`qa-approved-waivers`. Collection loads the whole set. Invalid-set cases
start from both valid `unreachable` approvals on `sim_waiver` and change one
named field, keeping the second valid approval to prove that no subset is
applied. Make every corruption in a separate owned copy of the directory. The
excluded-with-proof case changes one reason to `excluded` while keeping the
proof. For the `--no-waivers` bypass, use a non-readable owned approval
directory holding a malformed file, and confirm its contents and permissions
are the same before and after. A plain ungated run over it must block with
rc 2.

## Filesystem publication faults: precise interception and reversal

`faults/filesystem.py` launches only the given public command, in a fresh
process group with `boundary.so` preloaded:

```bash
python3 <asset>/faults/filesystem.py --owned <owned-root> --control <new-control-dir> \
  --library <owned>/boundary.so --operation {link,rename,unlink} --suffix <exact-suffix> \
  [--gate {any,complete,interrupt}] \
  [--timeout <seconds>] -- <command...>
```

The library intercepts `link`, `linkat`, `rename`, `renameat`, `renameat2`,
`unlink`, `unlinkat` and `rmdir` (reported as `unlink`); `*at` paths resolve
through their directory descriptors. `--gate` defaults to `any`, and
`--control` must not exist yet. It matches the owned root, the operation and the exact suffix,
writes and fsyncs an event naming the operation, source and destination, then
waits at most 20 seconds for the controller's answer. An exclusive shared
`consumed` marker allows exactly one EIO across all descendants. The
controller copies the temporary publication bytes before it replies, for every
gate including `any`. On shutdown it signals the whole process group, escalates
to SIGKILL even after the leader exits, and fails if any executing member
survives.

| Fault | Operation / destination / controller gate |
|---|---|
| Coverage Record publication | `link` to the nested Coverage Record manifest (suffix `/coverage-record/coverage.json`); fail before the link. Point-store publication has already completed. The Target `coverage.json` reference is created afterward, by an exclusive open, not a link. |
| Simulation publication | `rename` to the exact Target `/simulation.json`; fail before the replacement. |
| Terminal progress | `rename` to the exact invocation `/progress.json`, gate `complete`; fail only when the temporary JSON has `complete: true`. Earlier writes pass through. |

Save the source temporary JSON copied while the syscall was paused, the
consumed event, the exit status, and the reports and state before and after. A
missing handshake is a fixture failure, not proof of the intended persistence
behavior. The shared publication-abort case uses three declared Targets and
fails the first Simulation publication. To test a real producer interruption,
use the same paused Coverage Record link boundary with `--gate interrupt`: the
controller terminates the producer group before it replies and writes an
`interrupted` record; save it. An EIO return alone is not an interruption. The
publish-restart case needs both the observed interrupted producer and its
separately reachable restoration.

Restoration is a separate step. Disable injection by launching a fresh command
without the injection environment; restore only copied fixture inputs, confirm
their bytes and original permissions, then run the matching valid public
operation:

- A publication retry for an incomplete collection is a **new invocation
  number**; never resume a partial Coverage Record.
Never delete a lock to get past active production, and never restore
by writing state files: the valid public Flow produces new
authoritative evidence. Keep every failed transaction for inspection.

## Native and input corruption recipes

Native missing, unsafe, malformed and incompatible cases run `sim_custom`
with the registered test `native-missing`, `native-unsafe`,
`native-malformed` or `native-incompatible`. The fixture main first runs the
real simulation and coverage hook, then deletes its own raw file, replaces it
with a directory, writes a malformed record, or writes an incompatible header.
These are controlled native inputs, not real EDA-success evidence. Booley
reports `COV_RAW_FILE_UNSAFE` when the raw file is outside the allowed roots or
is not a regular file; `native-unsafe` is the not-a-regular-file case. There is
no stale-native case: Booley reports `COV_RAW_FILE_STALE` only when a raw file
is unchanged from its pre-run snapshot, and a raw file this run created at its
per-attempt path is fresh even with an old mtime.
`native-fsm`, `native-covergroup` and `native-unknown` append a record
of that native class using an existing source and hierarchy identity; judge
the expected unscored or capability/finding behavior independently. They make
no claim that the compiler can generate those classes.

The unscored-generated and unscored-foreign policy cases collect `sim_custom`
tests `native-generated` and `native-foreign`. Each keeps the normal raw
records and appends one supported record with a distinct source key: the first
creates `qa-generated/coverage_helper.sv` at runtime, the second references
the shipped `foreign/coverage_library.sv`. Both files are outside the Target's
declared source closure. Capture the `QA_NATIVE_ORIGIN` line the fixture
main prints, the file and the exact
appended native record, then require the normalized record to stay unscored
and the eligible RTL denominator to stay unchanged. The public projection need
not show an internal generated/foreign value; provenance comes from these
fixed producers. On restore, remove only the generated owned file and rerun
the `custom` baseline.

The failed-write-hook case runs as the non-root issued runtime user. It keeps
the exact `BOOLEY_COVERAGE_FILE` value, creates that new owned destination
with mode 0400, and calls the real write hook. Record the
`QA_WRITE_PROTECTED` line it prints, the
uid and mode, the simulator I/O error and the failed collection. A missing
environment variable or a setup failure does not exercise the write failure.
To restore, set mode 0600 on that exact owned file if it survives, move it
aside, and run a fresh `sim_custom` `gap` Coverage Record.

Tool faults use `faults/native_tool.py` behind a run-owned launcher placed
first on the Flow process's `PATH` only. Set `QA_REAL_EDA` to the recorded real
executable, `QA_EDA_OWNED` to the owned root, and `QA_EDA_FAULT`:

- `merge`, with a launcher named `verilator_coverage`: after a successful
  `--write <merged> ...` call, it adds 1 to the first native count in that
  merged file, which must sit inside `QA_EDA_OWNED`; raw databases stay
  unchanged.
- `merge-unsafe`, with a launcher named `verilator_coverage`: after a
  successful `--write <merged> ...` call, it moves the merged file to
  `<merged>.qa-merged` and leaves a directory at `<merged>`. Booley must report
  `COV_NATIVE_MERGE_UNSAFE` and keep no merged artifact.
- `version`, with a launcher named `verilator`: it forwards every call except
  `--version`, which prints Verilator 5.050.

Never edit the real compiler or its source provenance, and restore the
original executable selection before a fresh invocation.

Pre-sim drift cases use the per-test pre-sim command to append a harmless
comment to exactly `rtl/coverage_dut.sv` or `coverage.core` after planning;
capture the bytes before and restore them. The include-source analysis case
uses the declared include file `rtl/constants.svh` with a comment-only change
made after a pristine Coverage Record is saved; it must be in the recorded
closure
before collection.

Coverage Record faults use a fresh nested Coverage Record; use `baseline.native`
for point
mutations, since the Coverage Record must contain points. Copy an entire
completed
numbered invocation into a new owned reports root, keeping the
`sim/N/targets/<target>/coverage.json` layout. Create
`.qa-coverage-fault-copy` at that new root, then run:

```bash
python3 <asset>/faults/record.py <mode> <copied-target>/coverage.json --owned <copy-root>
```

Modes: `missing-points`, `truncated-gzip`, `trailing-data`,
`duplicate-point`, `invalid-final-record`, `resource-ceiling`, `v1`, `v2`,
`v3`, `v4`, `v5`, `older-layout`, `symlink` and `wrong-evaluation` (the last
needs a blocked or not-requested evaluation). `v1` to `v5` are the
`booley.coverage-campaign/` schemas Booley 0.4.0 and earlier wrote.
`older-layout` renames the Target's `record/` directory to 0.4.0's
`campaign/` and repoints the Target reference, leaving the manifest bytes
unchanged. The script follows the copied reference to the
nested Coverage Record, damages it; manifest faults then rewrite the nested
manifest and the Target
reference (new bytes and sha256) in Booley's own JSON format. Point faults
leave the manifest and the Target reference byte-identical. The
original Coverage Record is never touched. It refuses symlinked or
hard-linked input files before any write; linked sentinels outside the copy
must stay byte-identical. Record the error code, field and message proving the
intended check fired; an unrelated earlier parse error does not show that the
deeper validation works. Restore by discarding only that copy, recreating it
from the original, and analyzing the valid pair successfully.

Analyst path faults use the exact invalid form in their name: missing-argument
omits `--record`; target-name passes `sim_hdl`; latest passes the literal
`latest`; point-store and waveform pass the actual owned gzip and VCD paths;
legacy-flat-report passes a real run-owned JSON file named
`coverage_report.json` that is not a Coverage Record reference. Wrong-Target and
wrong-invocation cases move the valid
pair under a different encoded Target or number without rewriting its
identity. Simulation faults remove its projection, set completion to
incomplete, or swap in another Target's projection. Source-mode cases change
the named closure file or replace it with an owned sentinel symlink; restore
the full original closure before the separate verified-source rerun.

## Provider boundary fixture, real evidence MCP and live isolation

The simulated-provider cases are deterministic **external input** tests; their
reports never stand in for a real-model result, and protocol drift in the
provider fixture is a fixture bug, never a live-model pass. Live closure, advice and
source modes use the configured provider directly: save its real transcript
and confirm the Coverage Record, Project and waiver bytes are unchanged.
The
model instruction for a gap is:

> Begin with overview of this exact Coverage Record. Retrieve uncovered eligible
> points
> for rtl/coverage_dut.sv and verified excerpts for decoder choice 2. Separate
> immutable observations from hypotheses. Recommend a concrete missing test using
> only delivered references. State retrieval limitations. Do not run simulation,
> read waveforms, edit files, or approve waivers.

For Codex, create an executable owned launcher named `codex` that runs
`faults/provider.py`, ahead of the original on **only that Analyst process's**
`PATH`. Keep its configured model and a private copy of the exact-model
metadata. Booley creates the private `CODEX_HOME` and catalog; the fixture
reads that generated `config.toml`'s `mcp_servers.booley` command, args and
env. It drains the actual prompt and emits Codex NDJSON `agent_message` and
`turn.completed`/`turn.failed` envelopes. Malformed-output tests corrupt the
`agent_message` text, not the outer event envelope.

The Claude SDK prefers its bundled executable over `PATH`, so a `PATH`-only
Claude substitution does not exercise the boundary. Record that exact resolved
path. Set process-only `QA_CLAUDE_EXECUTABLE` to it and `QA_CLAUDE_SHIM` to an
owned executable launcher for `provider.py`, with `boundary.so` in
`LD_PRELOAD`. The shim redirects only that executable's stream-json call; the
real version probe passes through, and the SDK and installed executable bytes
stay unchanged. The fixture answers the received initialize control request
ID, reads the user prompt and emits the SDK's result envelope. Record the
actual SDK and protocol versions.

Set QA_PROVIDER_ROOT to a newly created owned evidence directory and
`QA_PROVIDER_CASE` to the case name. The shim reads the **product-generated**
MCP specification: Codex replaces the environment with the supplied map;
Claude merges its supplied map over the inherited environment. The real server
is launched from the installed package and receives `initialize`,
`notifications/initialized`, `tools/list` and actual `tools/call` requests.
Discovery must expose exactly `coverage_evidence`. The fixture never
fabricates a point response or writes the audit; it copies the real server's
audit after closing and reaping it. Keep the actual MCP request/response
transcript, including normal-envelope rejection objects. The fixture code
documents both provider envelopes and the bounded JSON-RPC flow.

Candidate cases request the matching disposition:

- Non-RTL and unscored use tagged TB points from the `analysis` baseline;
  waived uses the `waiver` baseline.
- Observed-hit uses a covered eligible point and the same nonempty proof
  reference as the positive unreachable control.
- Unreachable and missing-proof use an unhit eligible point; both screen
  `ready_for_human_review` and stay `not_approved` (ADR 0066: a candidate
  needs no proof reference). Missing-source first applies the source-stale
  fixture.
- Missing-evidence uses an empty evidence string; invalid-reason uses the
  literal `invalid`; duplicate repeats the exact same delivered candidate.
- Unknown or invented reference uses `point:999999999`, which must not have
  been delivered.
- Ready-for-human-review unreachable requests cite `proof/parity.log` but
  never confer approval.

Query cases use overview, `metric=branch`, `source=rtl/coverage_dut.sv`,
`covered` true and false, and disposition `eligible`, `waived` and `unscored`
explicitly. Pagination uses limit 2, then follows the actual `next_cursor`.
Changed-filter uses toggle then line; cursor-filter reuses a toggle cursor
with line. Undelivered requests use `point:999999999`. Requests across Coverage
Records
set `QA_FOREIGN_RECORD` to the actual `native` baseline Coverage Record and
send
its path as a prohibited extra selector while the Analyst is bound to the
`analysis` baseline; record the rejection and confirm the bound Coverage Record
is
unchanged. Budget exhaustion issues repeated 100-point pages of the `scale`
Coverage Record, at most 256 calls; stop only at the real evidence error and
keep the
actual terminal audit. Query-size validation and response-byte limits are
separate cases: request limit 100000 for the invalid-limit rejection, and
legal 100-point pages with long retained identities for the response-byte
check. A schema-range error says nothing about response-byte limits. Compare
response sizes the way the product measures them: compact JSON with ASCII
escaping (`ensure_ascii=True`, `,` and `:` separators); a pretty-printed
file's size is not that measure. Save
the actual MCP text bytes, continuation and real audit. An invented empty
success is never accepted. All pagination, exact-reference and source
sequences happen within one fresh Analyst call, so the references were really
delivered in that session.

Model-fault cases:

- Incomplete model: the provider emits a partial event, then exits normally
  without a terminal Codex turn or Claude result.
- Failure preserves state: inspect the earlier failed malformed-JSON call and
  its before and after state; do not run another model.
- Context exhaustion and incomplete responses are injected only after a
  successful MCP initialization and point delivery. An earlier protocol or
  setup error stays an error, with `boundary-error.json`, and does not show the
  intended model-fault handling.

Provider pipe transport tests are Linux-only; Windows hosts exercise them in
the Linux runtime. After every provider fault, restore: remove the
process-only redirection, restore the original executable and metadata
selection, confirm no nested server survives, then make a fresh successful
**real** Analyst call. Each call has a three-minute model-work ceiling plus
one minute for startup, evidence and cleanup; a timeout is a failure to
report, never a reason to ask a smaller question.

## Test-gap closure cases

The stale-source case uses a separate disposable worktree with the final
improved TB and a fresh passing collection under `closure/tests.toml`, which
changes only `sim_generated` to `[gap]`. Restoration reruns `sim_generated`
`gap` with `--coverage` there. Report the fault result and the restoration
result separately.

## Restoration and cleanup

Restoration never depends on the fault case having behaved as expected; each
fault uses its own disposable copy. Cleanup always runs: reap simulators,
clients, nested servers and controller groups; release runtimes, attachments,
inventory and grants, fault mounts and scarce storage. Keep only inert review
state, listed in `resources.md` with owner, path and deletion instructions.
Confirm the borrowed compiler, credentials, shared images and caches, and
unrelated Project state are unchanged. No push, PR, issue or report publication.
