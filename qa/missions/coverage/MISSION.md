# Native coverage on a fixed fixture Project

Hunt native-coverage bugs end to end: collection, exact arithmetic, Criteria
policy, waivers, Campaign storage, retention, a coverage-gated Ticket, and the
Coverage Analyst. The fixture's counts are known by construction, so every
mismatch is a finding rather than a judgement call.

## Pins and prerequisites
- Booley: the candidate under test (set by the run skill; not pinned here).
- Fixture Project: `../../shared/coverage/project/` in this repository.
- Tools/host needs: pinned Verilator 5.052 in the issued Sandbox, Yosys with
  SAT/induction, gcc, Python, Docker; ~8 GiB RAM and ~20 GiB free disk for the
  large `sim_scale` Campaign; a Codex or Claude client for the Analyst.
- Budget: 8 h total. Areas are in priority order; if time runs out, log the rest
  as skipped. Rerun with the other client or on Windows for host coverage.

These areas hunt native-coverage bugs: collection, exact arithmetic, Criteria policy, waivers, Campaign
storage, retention, a coverage Ticket and the Coverage Analyst. They do **not** use the UART design: they run
on the fixed fixture project `../../shared/coverage/project/`, whose counts are known by construction
(`expected.json`, `policies.json`, `baselines.json` beside it), so any mismatch is a finding. Fault-tool
recipes live in `../../shared/coverage/RUNBOOK.md`; skim it before area 1.

## Staging the coverage fixture
- Create a fresh disposable Project (add it to `resources.md`): copy `../../shared/coverage/project/` as its
  source, initialize it through the documented public route, and install `project-data/tests.toml` into the
  project-data directory Booley actually resolves (never assume `.booley_project`). Keep the rest of
  initialization's config. Commit the fixture locally before authoring any Ticket.
- Tools: pinned Verilator v5.052 (`ea338be98e1e838d3518809ce8899f85a009963c`) in the issued Sandbox, Yosys
  with SAT/induction, gcc, Python. Native builds run only in the Sandbox. `sim_scale` (4,096 instances)
  wants ~8 GiB RAM and ~20 GiB free disk; if absent, log it and skip only the large-Campaign items.
- Build the syscall fault library into a run-owned dir and inject it only into the one command under test
  (never `/etc/ld.so.preload`, never a shared shell):
  `gcc -shared -fPIC -O2 -Wall -Wextra -o <owned>/boundary.so ../../shared/coverage/faults/boundary.c -ldl`
- Sanity-check the independent oracle first: `python3 ../../shared/coverage/evaluator/controls.py <mode>`
  for `positive`, `hit-count`, `source-grouping`, `digest`, `rational-verdict` (positive accepts, each
  negative rejects). A broken oracle is a `qa-bug`; judge numbers by hand from `expected.json`.
- Campaign paths: the Flow's `targets/<t>/coverage.json` is a reference, not the Campaign. The Analyst takes
  it as-is; `measurements.py` and `approvals.py` take the nested V3 manifest it points to, and run ids are
  `run:NNN:<test>`. Resolve both with RUNBOOK.md "Campaign paths".
- Coverage Analyst CLI (there is no `booley flow coverage_analyst`):
  `booley session enter -- python -m booley.specialists.coverage_analyst --campaign <t>/coverage.json [--instruction <q>] [--report-dir <owned>]`.
- Produce the named baselines each area needs fresh, from the RUNBOOK.md table (e.g. `baseline.native` =
  `booley flow sim --target sim_toggle --test half --coverage` → pass, 4/8 toggle, `not_requested`). If one
  fails, record it and build the closest state manually instead of skipping the area.

## Mission-specific rules
- Fixture bytes, registered suites and thresholds are fixed. Never lower a threshold, edit `tests.toml`, or
  pick a different waiver point to make something pass; a mismatch is the finding.
- Every fault works on a run-owned copy or a process-local wrapper/env. Before restoring, save the failed
  output; restoration = remove the injection, restore owned bytes/permissions, rerun the valid public
  operation as a fresh invocation (publication) or the same exact selection (pruning).
- Simulated-provider results (`../../shared/coverage/faults/provider.py`) test Booley's handling of external input only; they
  never count as a live-model result.
- Analyst calls: ~3 min each. Provider pipe fixture is Linux-runtime only. Rerun with the other client
  (Codex vs Claude) for host coverage if time allows.

## Areas

### 1. cov-collect — Collection entry points, harnesses and suite selection (~45 min)
Intent: `--coverage` must produce one complete, correctly attributed native Campaign per selected Target/test,
and reject unsupported combos atomically.
Try:
- Read `booley flow sim --help` and the MCP `sim` schema: `--coverage` and alias `--cov`, default false;
  `coverage_analyst` needs an exact Campaign path.
- `--coverage` vs `--cov` on `sim_generated` (equivalent, fresh invocation numbers); MCP `sim` with
  `coverage: true`, then with string `"true"` (schema rejection before simulation); omit collection while a
  coverage Criterion exists → plain sim, no Campaign, no coverage acceptance.
- Harness kinds: `sim_generated` (two tests), `sim_custom` (C++ main), `sim_hdl` (tagged HDL TB),
  `sim_cocotb` (one process/database per test, verdicts from XML). Reverse the Target/test order → stable
  order, no cross-Target merge.
- Suite choice: no Criterion/selection → full registered suite minus default-skipped; explicit skipped test
  runs; sealed policy suite wins when no filter; explicit different suite → collected but gated result
  blocked, rc2; skipping a required test → visible mismatch, not a smaller denominator.
- Rejections before any build/report dir: `sim_icarus` with coverage; Verilator + Icarus together (valid
  Target rejected too); elab-only and elab-only-standalone; a process-local `verilator` wrapper reporting
  5.050 on `--version` (`../../shared/coverage/faults/native_tool.py`).
- Build cache: plain → coverage, trace → trace+coverage, coverage-only vs trace+coverage must have distinct
  build identities; a repeat reuses the build but runs fresh simulator processes and new native files.
- Pre-sim staging: merge `project-data/staging.toml`, collect `sim_staged` tests `half upper` (per test 4/8,
  union 8/8) and `sim_cocotb_staged` tests `gap full` (one batch stage, each test reads its own vector);
  then drop the `full` vector from the staged file → that test must fail loading it. Restore.
Look for: uninstrumented cache reuse, merged per-test databases, partial side effects after a rejection,
cocotb batch verdicts reused as coverage truth.

### 2. cov-numeric — Exact arithmetic, coverage window and native faults (~50 min)
Intent: numbers must match `expected.json` exactly; windows and hooks must be enforced; broken native input
must never become points.
Try:
- For each `expected.json` case run its Target/tests with `--coverage`, then check the nested V3 manifest
  (one `--native` per run, ids and paths per RUNBOOK.md "Campaign paths"):
  `python3 ../../shared/coverage/evaluator/measurements.py <nested-v3>/coverage.json --expected ../../shared/coverage/expected.json --case <case> --native run:001:<test>=<campaign-dir>/native/raw/001-<test>.dat`
  Cases: zero 0/8,
  quarter 2/8, half 4/8, rise (one direction) 4/8, full 8/8, repeat 4/8 with higher hit counts, union 4+4 →
  8/8, overlap → 4/8, properties4 half 2/4 and full 4/4, properties16 half 2/16 (12.5%), threshold 50 pass
  vs 50.01 fail at 4/8, reset window 4/8 not 8/8.
- Window: default includes reset; `reset_included = false` + one start call excludes it; custom write hook
  fires once on pass and fail paths.
- Config negatives (preflight rejection): non-boolean `reset_included`, unknown hook, duplicate hook,
  missing write declaration, start declared while reset included, `custom_main_hooks` on a non-custom
  harness, window keys in `[flows.sim]`, per-call window override.
- Runtime hook faults (collection invalid): missing/duplicate start, missing/duplicate write, write before
  start, unwritable output file (run as the non-root runtime user, mode 0400).
- Native faults via `sim_custom` tests `native-missing`, `native-malformed`, `native-incompatible`
  (rc2, sim truth kept; `COV_RAW_FILE_STALE` is not exercised here); merged-file tamper via a
  `verilator_coverage` PATH wrapper;
  first Target fails collection → second still runs, rc2; pre-sim edit of `rtl/coverage_dut.sv` or
  `coverage.core` after planning → drift rejected.
Look for: averaged per-test percentages, reset-inflated counts, float rounding, stale or tampered data
accepted, a zero that looks like "blocked".
Depends on: baseline.native, baseline.custom.

### 3. cov-policy — Criteria policy, verdict matrix and eligibility (~45 min)
Intent: sealed coverage Criteria must gate on exact rationals and keep simulation and coverage truths
separate.
Try:
- Seal each `policies.json` case through public Ticket authoring and collect: line 5/7 @70, branch 2/4 @50,
  expression 2/3 @66 (three short-circuit points, not four), toggle 4/8 @50, cover_property 2/3 @66; AND of
  line 70 + branch 51 → fail; 2/3 @66.67 → fail, @66 → pass; one record naming two Targets → two
  Target-bound Criteria.
- Verdicts on `sim_custom` `[gap]`/`[fail]` at 66/100: pass/pass rc0, fail/pass, pass/fail, fail/fail rc1;
  invalid collector → sim pass, gated blocked, rc2; ungated → `not_requested` rc0. Multi-Target `sim_pass`,
  `sim_miss`, `sim_collector_error` → rc2 with each Target's truths distinct.
- Eligibility: only RTL-closure points count. TB points unscored; `sim_custom` tests `native-generated` /
  `native-foreign` add out-of-closure records → unscored, RTL denominator unchanged. `native-fsm`,
  `native-covergroup`, `native-unknown` → kept, never silently scored. Zero-eligible metric and unavailable
  metric → blocked, not vacuous pass.
- Authoring negatives (one field each, via the public validator): empty targets/tests/metrics, unregistered
  test, `min_pct` 0, -1, 100.01, true, NaN, inf, `"90"`; legacy
  `coverage_toggle|fsm|value|branch|expression|mean` → rejected without translation.
Look for: rounding-to-display passing 66.67, AND treated as OR, TB points in the denominator, rc precedence
errors.
Depends on: baseline.policy, baseline.custom.

### 4. cov-waivers — Approved waivers and formal proof (~35 min)
Intent: only exact, pre-approved, Target-bound waivers apply; any bad entry blocks the whole set.
Try:
- Run `yosys -s ../../shared/coverage/proof/parity.ys` from the Project root (must prove induction), copy
  the log to `<approved-dir>/proof/parity.log`, then bind (repeat with `excluded` on the `sim_properties4`
  half Campaign):
  `python3 ../../shared/coverage/faults/approvals.py unreachable <nested-v3>/coverage.json --project <p> --directory <approved-dir> --inputs ../../shared/coverage/approval-inputs.json`
  Gated `sim_waiver` `[run]` toggle @1 → two bit-zero points waived with provenance.
- Anchors `rtl_repository` and `project_data_repository` (use a separately initialized project-data repo);
  approval digest change → new set digest, old Campaign unchanged; approval for one Target never leaks to
  another.
- In separate copies, keep one valid approval and break the other: invalid anchor, absolute or `../`
  directory, symlink, bad schema, duplicate table, zeroed source/proof digest, point from another source,
  wrong Target, TB point, missing proof file, proof path escape, proof on `excluded`, missing `approved_by`,
  Analyst `waiver_candidates` pasted in → blocked, neither applied.
- Ungated run with an unreadable malformed approval dir → no reads, `not_requested`, dir unchanged.
Look for: partial application, symlink/path escapes, hardcoded project-data path.
Depends on: baseline.waiver.

### 5. cov-storage — Campaign layout, integrity and publication faults (~45 min)
Intent: the stored Campaign must be complete, lossless, and never half-published.
Try:
- Two-Target invocation: report, progress, per-Target `coverage.json`, point store, `simulation.json`,
  native/hook files at documented paths; repeat with an explicit report root containing spaces. Decompress
  the point store and recompute totals; `sim_multi` source rollups group by source path, not instance, for
  line/branch/expression/toggle. `sim_scale`: valid, compact sim response (pointer only); note
  bytes/time/RSS.
- Corrupt copies with `../../shared/coverage/faults/campaign.py <mode> <copy>/coverage.json --owned <root>`
  (Target reference; the script rebinds it), then run the Analyst CLI on that reference → rejected before
  the model: v1, v2, missing/changed points, truncated gzip, trailing data, point count, sizes, digest,
  unsafe/absolute path, symlink, invalid final record, duplicate point, wrong rollup/source
  rollup/evaluation, resource ceiling.
- Publication faults with `../../shared/coverage/faults/filesystem.py` (table in RUNBOOK.md): fail `link …/coverage.json`, `rename
  …/simulation.json`, the Ticket `booley_state.json` acceptance write, terminal `progress.json`; shared
  abort across three Targets; `--gate interrupt` then reap the producer → no complete claim, lock released;
  fresh run gets a new number, no resume.
- Rerun and re-analyze while keeping an old Campaign → old bytes identical, no `latest` alias.
Look for: accepted evidence without a Campaign, lost prior acceptance, a corrupt copy reaching the model,
stale lock after interrupt.
Depends on: baseline.analysis, baseline.policy, baseline.multi.

### 6. cov-retention — Pruning and Simulation Campaign recovery (~40 min)
Intent: pruning removes exactly what was asked, is retryable, and resume/recovery never duplicates or loses
evidence.
Try:
- `python -m booley.flows.sim.campaign_retention --reports-root <r> --invocation N --native-target <t>`
  → only that payload gone, normalized evidence byte-identical, pruned Campaign still analyzable; repeat is
  safe. `--full` → empty `.pruned-N` tombstone, analysis now fails cleanly, next run skips N.
- Rejections before deletion (rc2, sentinels unchanged) for native and full: ambiguous/unknown Target,
  invalid number, unsafe path, symlink, changed native, unknown payload, unexplained missing native,
  tampered points.
- Interrupt a prune with `../../shared/coverage/faults/filesystem.py --operation unlink` under `.native-pruned/` or `.pruned-N/` →
  retry same selection completes; prune an active invocation → rc2, lock untouched; reap producer → prune
  succeeds.
- Follow `../../shared/coverage/simulation-campaign/RUNBOOK.md`: interrupt `sim_toggle half --coverage`
  mid-collection and resume from the Manifest (new attempt, whole aggregate rerun, one canonical reference);
  fail the Development State save via a directory at its `.tmp` path, remove, resume (one transaction, no
  new attempt); flip one byte in a terminal `result.json` → rc2 before any EDA launch, restore → resume
  without rerun. Cross-check with `../../shared/coverage/simulation-campaign/validate_aggregate.py`.
Look for: collateral deletion, number reuse, double-committed transactions.
Depends on: baseline.retention.

### 7. cov-ticket — Coverage-gated Ticket closes a test gap (~45 min)
Intent: a Ticket with a coverage Criterion is blocked by real evidence and satisfied only by fresh evidence at
current source.
Try:
- Swap in `../../shared/coverage/tickets/tests.toml` (`sim_generated` = `[gap]` only), create the Ticket
  from `../../shared/coverage/tickets/close-test-gap.md` verbatim. Collect → sim pass, `cover_property`
  miss, acceptance blocked; ask the Analyst about that Campaign; a plain sim adds no coverage acceptance.
- Let the developer add decoder choice 2 in TB only; recollect → pass; read Criteria/Satisfaction record for
  exact Campaign/Simulation pointers.
- In a second disposable Ticket, before handoff (or in an unaccepted review), pass then edit TB → old evidence
  stale; recollect regains it. Separately, an edit after **accepted** review must be refused (`main`
  unchanged) with clear guidance; there is no recollect route from accepted review. Ask for
  exclusions → advisory only. Deliver → clean TB-only committed diff and report.
Look for: RTL or policy edits, stale acceptance honored, Analyst mutating Criteria.
Depends on: area 1 working collection. Read the created Ticket's actual id/slug.

### 8. cov-analyst — Coverage Analyst and its evidence boundary (~50 min)
Intent: the Analyst binds one exact Campaign, sees only `coverage_evidence`, never mutates state, and its
outputs are validated.
Try:
- CLI and MCP on fresh Campaigns; ask the gap question from RUNBOOK.md. Verdict classes: pass
  `coverage_ready`, fail `coverage_not_ready`, blocked `coverage_evidence_blocked`, ungated
  `ungated_no_recommendation`, sim-fail still reports. Ask it to simulate/read waveforms/approve waivers →
  refused, state hashes unchanged. Stealth Project → report-only. Stray `coverage_waivers.json` → ignored.
  Instruction-like source comment → data.
- Source trust: full closure → verified excerpts; missing RTL, changed RTL/TB/`rtl/constants.svh`/core,
  unsafe symlink → whole report downgraded.
- Input rejects before model: no `--campaign`, Target name, `latest`, point store gz, VCD,
  `coverage_report.json`, wrong Target/invocation path, per-call policy or waiver dir,
  missing/incomplete/other-Target `simulation.json`. Completed Target analyzable while sibling still runs.
- Isolation: Codex/Claude sessions expose only `coverage_evidence`, empty cwd, no Project skills; missing
  Codex model metadata fails early, refresh fixes; interrupted Claude Analyst leaves no nested server.
- Via `../../shared/coverage/faults/provider.py` shim: queries (overview, metric, source, covered/uncovered, dispositions, pages
  of 2, changed filter, exact reference, scope on `sim_scale`); rejects (cross-Campaign, bad cursor,
  cursor+new filter, undelivered ref, limit 100000, 64 KiB response, total budget); candidates
  (excluded/unreachable → `not_approved`; missing proof/evidence, observed hit, report-only → investigate;
  TB, unscored, waived, duplicate, bad reason, `point:999999999` → forbidden); model faults (malformed JSON,
  missing fields, incomplete, invented ref, context exhausted) → failed report, state unchanged. Restore the
  real executable and do one real call.
Look for: fabricated pages, cross-Campaign reads, oversized payloads, empty-success reports, surviving MCP
servers.
Depends on: baseline.analysis, baseline.scale, area 4 for waived points.

### 9. cleanup — Coverage cleanup (~20 min)
Reap simulators, Analyst clients, nested MCP servers and fault controllers; remove wrappers, `boundary.so`,
redirection env, altered permissions, approval-dir copies; undo `tests.toml`/config swaps; release the
fixture Project, Tickets and branches in `resources.md`.

## Known traps
- Only `sim_toggle` has the clean 8-point toggle denominator; other Targets include clock/reset/helper
  points that must still be counted.
- `sim_generated` has no HDL `tb` tag on purpose; check classification and resolved source closure, not the
  Target name.
- A schema/setup error never proves a deeper validation; read the error code.
- Compare response sizes as compact UTF-8 JSON, not pretty-printed files.
- Publication retry = new invocation number; prune retry = same selection. Never delete a lock to get past
  an active producer.
- Parallel sub-agents share the host session reaper cap (`[interactive] max_sessions`, default 4). Over the
  cap, it stops the oldest live Sandbox sessions, including other agents' and other Projects'. Before
  fanning out, count live sessions (`docker ps -q --filter label=booley.role=interactive | wc -l`) and
  run at most `max_sessions - live` agents.
- Parallel Claude agents share one 5-hour rate-limit window: four agents reached ~95% in one run. Check
  the remaining allowance before fanning out, and prefer fewer agents over a mid-run stall.
