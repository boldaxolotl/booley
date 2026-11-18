# Native coverage on a fixed fixture Project

Hunt native-coverage bugs end to end: collection, exact arithmetic, coverage
bounds, waivers, Coverage Record storage, recovery, a measured test-gap closure,
and the Coverage Analyst. The areas don't use a real design: they run on a fixed
fixture Project whose counts are known by construction, so every mismatch is a
finding rather than a judgment call. The `booley-qa-run` skill runs this
mission; fault-tool recipes live in `../../shared/coverage/RUNBOOK.md`, so skim
it before area 1.

## Pins and prerequisites
- Booley: the candidate under test (set by the run skill; not pinned here).
- Fixture Project: `../../shared/coverage/project/` in this repository, with its
  expected values beside it in `expected.json`, `policies.json` and
  `baselines.json`.
- Tools/host needs: pinned Verilator (`VERILATOR_VERSION` and `VERILATOR_REF` in
  `src/booley/data/docker/Dockerfile.base`) in the issued Sandbox, Yosys with
  SAT/induction, gcc, Python, Docker; ~8 GiB RAM and ~20 GiB free disk for the
  large `sim_scale` Coverage Record; a Codex or Claude client for the Analyst.
- Budget: 8 h total: 405 area minutes plus 75 contingency. Areas run in
  priority order; log areas not reached as skipped. Run areas sequentially;
  a child's measured work and Interactive children never share a worktree.
  Rerun with the other client or on Windows only when asked.

## Staging the coverage fixture
- Create a fresh disposable Project (add it to `resources.md`): copy
  `../../shared/coverage/project/` as its source, initialize it through the
  documented public route, and install `project-data/tests.toml` into the
  resolved Booley Directory (never assume `.booley`). Keep the rest of
  initialization's config. Commit fixture and initialized Project files,
  including the managed `.gitignore`, before creating a worktree.
- Native builds run only in the Sandbox. `sim_scale` (4,096 instances) wants ~8 GiB RAM and ~20 GiB
  free disk; if absent, log it and skip only the large-record items.
- Build the syscall fault library into a run-owned dir and inject it only into the one command under test
  (never `/etc/ld.so.preload`, never a shared shell):
  `gcc -shared -fPIC -O2 -Wall -Wextra -o <owned>/boundary.so ../../shared/coverage/faults/boundary.c -ldl`
- Sanity-check the independent oracle first: `python3
  ../../shared/coverage/evaluator/controls.py <mode>` for `positive`,
  `hit-count`, `source-grouping`, `digest` (positive accepts, each negative
  rejects). A broken oracle is a `qa-bug`; judge numbers by hand from
  `expected.json`.
- Coverage Record paths: the Flow's `targets/<t>/coverage.json` is a reference,
  not the Coverage Record. The Analyst takes
  it as-is; `measurements.py` and `approvals.py` take the nested Coverage Record
  manifest it points to, and run ids are
  `run:NNN:<test>`. Resolve both with RUNBOOK.md "Coverage Record paths".
- Coverage Analyst CLI:
  `booley specialist coverage_analyst --record <t>/coverage.json [--instruction <q>] [--report-dir <owned>]`.
- Produce the named baselines each area needs fresh, from the RUNBOOK.md table (e.g. `baseline.native` =
  `booley flow sim --target sim_toggle --test half --coverage` → pass, 4/8 toggle, `not_requested`). If one
  fails, record it and build the closest state manually instead of skipping the area.

## Mission-specific rules
- Fixture bytes, registered suites and thresholds are fixed. Never lower a threshold, edit `tests.toml`, or
  pick a different waiver point to make something pass; a mismatch is the finding.
- Every fault works on a run-owned copy or a process-local wrapper/env. Before restoring, save the failed
  output; restoration = remove the injection, restore owned bytes/permissions, rerun the valid public
  operation as a fresh invocation.
- Simulated-provider results (`../../shared/coverage/faults/provider.py`) test Booley's handling of external input only; they
  never count as a live-model result.
- Analyst calls: ~3 min each. Provider pipe fixture is Linux-runtime only. Rerun with the other client
  (Codex vs Claude) for host coverage if time allows.
- Follow the shared [Developer Agent child
  rules](../../shared/DEVELOPER-CHILD.md) for area 7.

## Areas

### 1. cov-collect — Collection entry points, harnesses and suite selection (~45 min)
Intent: `--coverage` must produce one complete, correctly attributed native
Coverage Record per selected Target/test,
and reject unsupported combos atomically.
Try:
- Read `booley flow sim --help` and the MCP `sim` schema: `--coverage` and alias `--cov`, default false;
  `coverage_analyst` needs an exact Coverage Record path.
- `--coverage` vs `--cov` on `sim_generated` (equivalent, fresh invocation
  numbers); MCP `sim` with `coverage: true`, then with string `"true"` (schema
  rejection before simulation); a `[<t>.coverage]` table does not make a plain
  `booley flow sim` collect: no Coverage Record.
- Harness kinds: `sim_generated` (two selected tests), `sim_custom` (C++ main), `sim_hdl` (tagged HDL TB),
  `sim_cocotb` (one process/database per test, verdicts from XML). Reverse the Target/test order → stable
  order, no cross-Target merge.
- Suite choice: no selection → registered suite minus default-skipped; explicit
  skipped test runs.
- Rejections before any build/report dir: `sim_icarus` with coverage; Verilator
  + Icarus together (valid Target rejected too).
- Rejections before any build: elab-only and elab-only-standalone → rc 2
  "Coverage requires simulation mode" (a numbered report dir holding the
  refusal is allowed, as for other elab-mode refusals); a process-local
  `verilator` wrapper reporting 5.050 on `--version`
  (`../../shared/coverage/faults/native_tool.py`) → rc 2 naming the pinned
  collector before any compile (a Simulation and Coverage Record ending
  `COLLECTOR_ERROR` is allowed).
- Build cache (trace cases on `sim_cocotb`, where Booley drives the dump): plain
  → coverage, trace → trace+coverage, coverage-only vs trace+coverage must have
  distinct build identities; a repeat reuses the build but runs fresh
  simulator processes and new native files.
- Pre-sim staging: merge `project-data/staging.toml`, collect `sim_staged` tests `half upper` (per test 4/8,
  union 8/8) and `sim_cocotb_staged` tests `gap full` (one stage per test,
  since coverage runs each cocotb Test Run in its own process; each test reads
  its own vector);
  then drop the `full` vector from the staged file → that test must fail loading it. Restore.
Look for: uninstrumented cache reuse, merged per-test databases, partial side effects after a rejection,
cocotb batch verdicts reused as coverage truth.

### 2. cov-numeric — Exact arithmetic, coverage window and native faults (~50 min)
Intent: numbers must match `expected.json` exactly; windows and hooks must be enforced; broken native input
must never become points.
Try:
- For each `expected.json` case run its Target/tests with `--coverage`, then
  check the nested Coverage Record manifest (one `--native` per run, ids and
  paths per RUNBOOK.md "Coverage Record paths"): `python3
  ../../shared/coverage/evaluator/measurements.py <nested>/coverage.json
  --expected ../../shared/coverage/expected.json --case <case> --native
  run:001:<test>@1=<record-dir>/native/raw/001-<test>@1.dat` Cases: zero 0/8,
  quarter 2/8, half 4/8, rise (one direction) 4/8, full 8/8, repeat 4/8 with
  higher hit counts, union 4+4 → 8/8, overlap → 4/8, properties4 half 2/4 and
  full 4/4, properties16 half 2/16 (12.5%), threshold-equal and threshold-above
  (4/8 numbers only; their verdicts, bound 50 met and 50.01 unmet, are measured
  in area 3), reset window 4/8 not 8/8.
- Window: default includes reset; `reset_included = false` + one start call excludes it; custom write hook
  fires once on pass and fail paths.
- Config negatives (preflight rejection): non-boolean `reset_included`, unknown hook, duplicate hook,
  missing write declaration, start declared while reset included, `custom_main_hooks` on a non-custom
  harness, window keys in `[flows.sim]`, per-call window override.
- Runtime hook faults (collection invalid): missing/duplicate start, missing/duplicate write, write before
  start, unwritable output file (run as the non-root runtime user, mode 0400).
- Native faults via `sim_custom` tests `native-missing`, `native-unsafe`
  (`COV_RAW_FILE_UNSAFE`), `native-malformed`, `native-incompatible` (rc2, sim
  truth kept; `COV_RAW_FILE_STALE` is not exercised here); merged-file tamper
  and an unsafe merge output (`COV_NATIVE_MERGE_UNSAFE`, no merged artifact
  kept) via a `verilator_coverage` PATH wrapper (RUNBOOK.md "Native and input
  corruption recipes");
  first Target fails collection → second still runs, rc2; pre-sim edit of `rtl/coverage_dut.sv` or
  `coverage.core` after planning → drift rejected.
- `booley coverage uncovered <t>/coverage.json` on the quarter, half and full
  `sim_toggle` records lists exactly the toggle points `expected.json` leaves
  unhit (none for full), sorted by source, line and metric; `--metric` and
  `--source` filter by exact match; waived (area 4) and unscored (area 3)
  points are never listed; a record whose collection failed lists with a
  WARNING that it is not a coverage result. The nested manifest, a Target name
  or `latest` → exit 2 with the `coverage.json` hint.
Look for: averaged per-test percentages, reset-inflated counts, float rounding, stale or tampered data
accepted, a zero that looks like "blocked", an uncovered list that disagrees
with the measured numbers.
Depends on: baseline.native, baseline.custom.

### 3. cov-metrics — Coverage bounds: exact verdicts, `error`s and eligibility (~55 min)
Intent: coverage bounds must judge exact rationals and keep simulation and
coverage truths separate; a number that cannot be produced is an `error`, never
a vacuous met.
Try:
- `booley cheat --metrics` lists the five coverage metrics. Write each
  `policies.json` case as a `[<target>.coverage]` table per RUNBOOK.md "Coverage
  bounds", commit it and measure with `--all`: line 5/7 >=70, branch 2/4 >=50,
  expression 2/3 >=66 (three short-circuit points, not four), toggle 4/8 >=50,
  cover_property 2/3 >=66 are met; 2/3 against 66.67 is unmet; line 70 + branch
  51 → only branch unmet; two Targets' tables in one commit stay two rows.
- Verdict combinations on `sim_custom` `[gap]`/`[fail]` at 66/100 and the
  `sim_pass`, `sim_miss`, `sim_collector_error` precedence per RUNBOOK.md: with
  `--all` the verdicts are met, unmet or `error`, exit 2 for an `error`. Without
  `--all` the first unmet or `error` verdict withdraws only the children still
  waiting for admission, whose tables read "not evaluated", while children
  already running finish and keep their verdicts
  (`src/booley/harness/measure_children.py` module docstring), so a table is
  "not evaluated" only if its child never started.
- Eligibility with plain coverage runs: only RTL-closure points count. TB points
  unscored; `sim_custom` tests `native-generated` / `native-foreign` add
  out-of-closure records → unscored, RTL denominator unchanged. `native-fsm`,
  `native-covergroup`, `native-unknown` → kept, never silently scored. A
  zero-eligible or unavailable metric bound → `error`, never a vacuous met.
- `booley measure --validate` negatives per RUNBOOK.md "Coverage bounds".
Look for: 66.67 rounded to met, AND read as OR, TB points in the denominator, a
collector error read as met or unmet.
Depends on: baseline.policy, baseline.custom.

### 4. cov-waivers — Approved waivers and formal proof (~35 min)
Intent: only exact, pre-approved, Target-bound waivers apply; any bad entry blocks the whole set.
Try:
- Run `yosys -s ../../shared/coverage/proof/parity.ys` from the Project Root
  (must prove induction), copy the log to `<approved-dir>/proof/parity.log`,
  then bind (repeat with `excluded` on the `sim_properties4` half Coverage
  Record): `python3 ../../shared/coverage/faults/approvals.py unreachable
  <nested>/coverage.json --project <p> --directory <approved-dir> --inputs
  ../../shared/coverage/approval-inputs.json` A measured `[sim_waiver.coverage]`
  toggle bound on `[run]` (area 7) → two bit-zero points waived with provenance.
- Anchors `rtl_repository` and `project_data_repository` (use a separately initialized Booley Directory repo);
  approval digest change → new set digest, old Coverage Record unchanged;
  approval for one Target never leaks to
  another.
- In separate copies, keep one valid approval and break the other: invalid anchor, absolute or `../`
  directory, symlink, bad schema, duplicate table, zeroed source/proof digest, point from another source,
  unknown Target identity (`COV_WAIVER_TARGET_UNKNOWN`), TB point, missing
  proof file, proof path escape, proof on `excluded`, missing `approved_by`,
  Analyst `waiver_candidates` pasted in → blocked, neither applied.
- An approval retargeted to another known Target belongs to that Target's set
  (#727): collecting the other Target blocks it (`COV_WAIVER_POINT_STALE`),
  while the original Target alone applies its remaining valid approval.
- Ungated run with an unreadable malformed approval dir → `blocked`, rc2, waiver diagnostics, and a `--no-waivers` hint. The same run with `--no-waivers` → no reads, `not_requested`, dir unchanged.
Look for: partial application, symlink/path escapes, hardcoded Booley Directory path.
Depends on: baseline.waiver.

### 5. cov-storage — Coverage Record layout, integrity and publication faults (~45 min)
Intent: the stored Coverage Record must be complete, lossless, and never
half-published.
Try:
- Two-Target invocation: report, progress, per-Target `coverage.json`, point store, `simulation.json`,
  native/hook files at documented paths; repeat with an explicit report root containing spaces. Decompress
  the point store and recompute totals; `sim_multi` source rollups group by source path, not instance, for
  line/branch/expression/toggle. `sim_scale`: valid, compact sim response (pointer only); note
  bytes/time/RSS.
- Corrupt copies with
  `../../shared/coverage/faults/record.py <mode> <copy>/coverage.json --owned <root>`
  (Target reference; manifest faults rebind it, point faults leave it
  unchanged), then run the Analyst CLI on that reference → rejected before the
  model: v1, v2, v3, v4, v5, missing points, truncated gzip, trailing data,
  symlink, invalid final record, duplicate point, wrong evaluation (forged
  pass), resource ceiling (over-ceiling decompression).
- Older Booley (#3684): `older-layout` (0.4.0's `campaign/` Record
  directory) and `v5` (0.4.0's `booley.coverage-campaign/v5`) copies → the
  Analyst CLI, `booley coverage uncovered` and `booley flow sim --resume-from`
  on the copy's Manifest each say an older Booley wrote it and is no longer
  supported, name the Simulation invocation directory to delete, and never call
  it corrupt. When the 0.4.0 wheel is at hand, a real 0.4.0 `--coverage` run in
  a separate disposable Project beats the copies.
- Publication faults with `../../shared/coverage/faults/filesystem.py` (table in RUNBOOK.md): fail `link …/coverage-record/coverage.json`, `rename
  …/simulation.json` and terminal `progress.json`; shared
  abort across three Targets; `--gate interrupt` then reap the producer → no complete claim, lock released;
  fresh run gets a new number, no resume.
- Rerun and re-analyze while keeping an old Coverage Record → old bytes
  identical, no `latest` alias.
Look for: accepted evidence without a Coverage Record, a lost prior Coverage
Record, a corrupt copy reaching the model, stale lock after interrupt. Depends
on: baseline.analysis, baseline.policy, baseline.multi.

### 6. cov-retention — Simulation Record recovery (~40 min)
Intent: resume and recovery never duplicate or lose evidence.
Try:
- Follow `../../shared/coverage/simulation-record/RUNBOOK.md`: interrupt
  `sim_toggle half --coverage` mid-collection; resume and `--dry-run` from the
  Manifest → rc2 before any EDA launch naming a new `--coverage` run, attempt
  inventory unchanged; the fresh run is the control; break a field resume
  validates in a terminal `result.json` (the grade, `attempt_id` or
  `manifest_sha256`; a well-formed edit to an unvalidated field such as a
  stored digest is accepted by design, ADR 0064) → rc2 before any EDA launch,
  restore → `booley flow sim --resume-from <manifest>` without rerun.
  Cross-check with
  `../../shared/coverage/simulation-record/validate_aggregate.py`.
Look for: number reuse, a restored work item that reruns.
Depends on: baseline.retention.

### 7. cov-closure — Test-gap closure as a Measured Commit; waivers decided by the operator (~65 min)
Intent: only fresh strict coverage evidence meets a bound; an approval cannot
replace collection.
Depends on: area 1's working collection; area 4's proof; read
`docs/user/COVERAGE.md#coverage-waivers` before approvals.
Try:
- Install `../../shared/coverage/closure/tests.toml` (`sim_generated` = `[gap]`
  only) and `../../shared/coverage/closure/metrics.toml`, configure
  `[coverage.waivers]` with an approval directory, and commit. `booley measure`:
  unmet, exit 1, sim passes, `cover_property` below 100, failing rows first.
- Start a child per the [Developer Agent child
  rules](../../shared/DEVELOPER-CHILD.md) with
  `../../shared/coverage/closure/close-test-gap.md`: it asks the Analyst about
  the first Coverage Record, adds decoder choice 2 in the TB only, leaving
  RTL/core/registry/`metrics.toml` intact, and returns a Measured Commit: green
  header, no "tests changed" or "rules loosened", TB-only diff. Integrate.
- Same tree, new message: amend the message only → still Measured; edit a TB
  comment and commit → a new tree, not Measured until measured again.
- Loosened rule: in a disposable commit lower the bound to 50 with a `notes`
  entry and measure with `--base <the commit that installed the closure rules>`
  (the fixture Project's default branch gives its own commits no Base) → met but
  amber "rules loosened" with the note; without the note "(no note)".
- Waivers, in a separate disposable Project: `[sim_waiver.coverage]
  toggle_coverage_min = 100` with area 4's proof; collect, the Analyst screens
  `waiver-candidates.json` (record `sim_waiver`'s observed toggle
  numerator/denominator, not `sim_toggle`'s 8-point figures); measure → unmet.
  `faults/approvals.py` binds both bit-zero points at once and refuses an
  existing output file (`qa/shared/coverage/faults/approvals.py::select`
  requires exactly two, `bind` opens with `"x"`), so stage it: bind into an
  owned staging directory, install a copy of the TOML keeping only the first
  `[[approval]]` table (with its `[approval.proof]`) plus the proof log, commit,
  measure → still unmet; install the full file, commit, measure → Measured.
  Candidates never become waivers by themselves, and never waive the decoder gap
  in the main closure.
Look for: Analyst self-approval, candidates treated as waivers, a stale Measured
verdict on a changed tree, a loosening shown green.

### 8. cov-analyst — Coverage Analyst and its evidence boundary (~50 min)
Intent: the Analyst binds one exact Coverage Record, sees only
`coverage_evidence`, never mutates state, and its
outputs are validated.
Try:
- CLI and MCP on fresh Coverage Records; ask the gap question from RUNBOOK.md.
  Verdict classes: ungated `ungated_no_recommendation`, blocked
  `coverage_evidence_blocked`, sim-fail still reports. Ask it to simulate/read
  waveforms/approve waivers → refused, state hashes unchanged. Stealth Project →
  report-only. Stray `coverage_waivers.json` → ignored. Instruction-like source
  comment → data.
- Source trust: full closure → verified excerpts; missing RTL, changed RTL/TB/`rtl/constants.svh`/core,
  unsafe symlink → whole report downgraded.
- Input rejects before model: no `--record`, Target name, `latest`, point store gz, VCD,
  `coverage_report.json`, wrong Target/invocation path, per-call waiver dir,
  missing/incomplete/other-Target `simulation.json`. Completed Target analyzable while sibling still runs.
- Isolation: Codex/Claude sessions expose only `coverage_evidence`, empty cwd, no Project skills; missing
  a model missing from the Codex models cache → one warning and Booley's
  generated tool-free catalog entry, analysis proceeds (ADR 0080); an
  unreadable, malformed or ambiguous `models_cache.json` fails early;
  interrupted Claude Analyst leaves no nested server.
- Via `../../shared/coverage/faults/provider.py` shim: queries (overview, metric, source, covered/uncovered, dispositions, pages
  of 2, changed filter, exact reference, scope on `sim_scale`); rejects (another
  Coverage Record, bad cursor,
  cursor+new filter, undelivered ref, limit 100000, 64 KiB response, total budget); candidates
  (excluded/unreachable → `not_approved`; an unhit `unreachable` with
  evidence but no proof reference → `ready_for_human_review`, still
  `not_approved` (ADR 0066); missing evidence, observed hit, report-only →
  investigate;
  TB, unscored, waived, duplicate, bad reason, `point:999999999` → forbidden); model faults (malformed JSON,
  missing fields, incomplete, invented ref, context exhausted) → failed report, state unchanged. Restore the
  real executable and do one real call.
Look for: fabricated pages, reads across Coverage Records, oversized payloads,
empty-success reports, surviving MCP
servers.
Depends on: baseline.analysis, baseline.scale, area 4 for waived points.

### 9. cleanup — Coverage cleanup (~20 min)
Reap simulators, Analyst clients, nested MCP servers and fault controllers; remove wrappers, `boundary.so`,
redirection env, altered permissions, approval-dir copies; undo `tests.toml`/config swaps; release the
fixture Project, child worktrees and branches in `resources.md`.

## Known traps
- `sim_toggle` and the `--main` HDL TBs write no waveform, so `--trace` on them
  ends INCONCLUSIVE rc 1 by design; trace cases use `sim_cocotb`.
- Only `sim_toggle` has the clean 8-point toggle denominator; other Targets include clock/reset/helper
  points that must still be counted.
- `sim_generated` has no HDL `tb` tag on purpose; check classification and resolved source closure, not the
  Target name.
- A schema/setup error never proves a deeper validation; read the error code.
- Compare response sizes as compact JSON with ASCII escaping (`ensure_ascii=True`), not pretty-printed files.
- Publication retry = new invocation number. Never delete a lock to get past
  an active producer.
- Parallel sub-agents share the host Sandbox cap (`[sandbox] max_sessions`, default 4). New
  Sandbox starts are refused at capacity; live work is not evicted to make room. Before fanning out,
  count live Sandboxes (`docker ps -q --filter label=booley.role=interactive | wc -l`) and
  allow for pending starts. A capacity refusal lists both live Sandboxes and pending editor starts.
  Reuse existing Sandboxes or reduce parallelism when the host is full.
- Parallel Claude agents share one 5-hour rate-limit window: four agents reached ~95% in one run. Check
  the remaining allowance before fanning out, and prefer fewer agents over a mid-run stall.
