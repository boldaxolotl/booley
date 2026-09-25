# Taxi 10G MAC port and evolution

Port pinned upstream Taxi to Booley from a direct clone, then evolve `taxi_eth_mac_10g` through a
Verification Ticket (exact PFC/FCS/underrun/statistics tests, mutation campaign) and a Bug Fix
Ticket for a seeded PFC fault the upstream tests miss. Also covers Setup with a derived Cocotb
image, Targets, Doctor, lint, logical/physical synthesis, Interactive Mode, B-Wave, a submodule
companion, Ticket machinery and Simulation Campaigns. Taxi's interfaces, recursive symlink, big
Python stack and five clocks surface stale caches, skipped tests and dropped clocks.

## Pins and prerequisites
- Booley: the candidate under test (set by the run skill; not pinned here).
- Taxi: https://github.com/fpganinja/taxi @ `cc70b270b910d369ab1ad7b3855e76399fd461f1`, fresh direct
  clone on a run-owned branch (no wrapper; no submodules).
- Needs: Docker ≥6 GB free plus run-dir room; Codex or Claude login; Sandbox Image with Verilator,
  Cocotb, Verible, Yosys+slang, OpenROAD; VS Code attached for the area-6 viewer.
- Primary: Ubuntu x86-64 + Codex CLI; rerun on Windows (Docker Desktop, WSL2), VS Code Codex or
  Claude CLI for coverage.
- Budget: 8 h total. Areas are in priority order; if time runs out, log the rest as skipped. Work
  areas 5, 7, 8, 11, 12 while a Ticket runs.

## Mission-specific rules
- Taxi baseline is immutable: only Setup, the two Tickets and the area-10 seed change it. Never edit
  existing RTL, TBs, `.f` manifest, the `src/eth/lib/taxi` symlink or Git metadata.
- Use verbatim, inferring nothing: `prompts/setup.md` (Setup), `prompts/interactive.md` (Interactive
  child), `tickets/observability.md` and `tickets/repair.md` (`booley-ticket-create --agent
  --no-confirm`).
- The repair Developer gets only the failing assertion, expected vs observed classes, the
  upstream-test contrast, Scope and evidence pointers, never the seed location or fix.
- Inject faults only on disposable copies, branches or fixtures; save the failure, then restore.

## Areas

### 1. setup — Clone, init and delegated Project Setup (~55 min)
Intent: reach a warning-free Project using only docs, packaged skills, cheat sheets and help.
Try:
- Record `booley --version` and import origin; `booley bootstrap --check-only` reports ready.
- Run `booley init`, then give one Setup agent `prompts/setup.md`; no second approval request
  (`SETUP-PLAN.md` is a record, not a gate). Expect Targets `sim_mac_10g`, `lint_mac_10g`,
  `synth_mac_10g`, `synth_mac_10g_physical`; a derived image from the full pinned `tox.ini` stack;
  explicit `[stealth] enabled = false`; `fixtures/taxi_mac_10g.sdc` as Project-owned config.
- Nothing installs at runtime; the TB import works in the Sandbox; pins (pytest 8.3.4 … scapy 2.6.1)
  match `prompts/setup.md`.
- Compare config/cores/guidance with the plan (every deviation stated). Run the Setup findings
  handoff and native-parity comparison; compare documented selector lists with real ones.
- The `src/eth/lib/taxi -> ../../../` symlink is unchanged; sources resolve without a working host
  symlink (Windows). Rerun plain `booley init`: no drift. Another run with the other provider:
  issued auth/client policy follows.
- Image faults: changed dependency pin → stale image rebuilt; bounded post-setup hook in a new
  Ticket workspace runs once, not on resume; nonzero hook blocks, fix + documented retry; opted-in
  disposable host skill is read-only.
Look for: re-approval requests, Taxi edits, missing `cocotb_test`/`pytest` (imported at TB module
scope), implicit stealth, symlink loops, stale Session Runtime after `init` rebuilds the image.

### 2. targets-doctor — Target catalog, selection and Doctor (~25 min)
Intent: discovery shows exactly what will run, and CLI and MCP agree.
Try:
- `sim_mac_10g` uses the upstream `test_taxi_eth_mac_10g.sv` wrapper and `.py` module, the full
  closure of `taxi_eth_mac_10g.f`, and Verilator Cocotb with native `trace.fst`. All 7 tests are
  registered: rx, tx, tx-alignment, tx-underrun, tx-user-error, lfc and pfc.
- Applicable Targets carry all `prompts/setup.md` parameters; logical and physical synth Targets
  share sources, top, parameters.
- `booley targets` list/filter/detail/JSON agree with MCP on identity, selectors, EDA programs, top,
  parameters and inputs. Use one fully qualified selector. Changing run-owned config refreshes
  discovery without touching Taxi.
- Negatives: a colliding bare name is rejected, the qualified name picks the exact core; an invented
  per-call parameter override is rejected; two Targets with different values report their own; a
  restored valid selector runs.
- Deep Doctor runs only selected Targets; the bad selftest is hidden in listings yet evidenced in
  deep Doctor; authored and vendored names are kept. A many-core fixture lists the exact qualified
  set; an invalid core behind `FUSESOC_IGNORE` is excluded while the valid one resolves.
- Plain, deep, plain Doctor: warning-free, no waivers. Break config, Doctor, restore: clean.
Look for: CLI/MCP mismatch, dropped tests/params, ambiguous identities, Doctor passing unrun checks.

### 3. baseline — Upstream simulation and Cocotb result handling (~25 min)
Intent: unchanged upstream passes; broken results never count as passes.
Try:
- Run the full seven-function `sim_mac_10g` untraced: normal/jumbo frames, RX/TX at IFG 12 and 0,
  DIC alignment, good FCS, TX underrun and user error, LFC/PFC frames, RX/TX timestamps.
- Full and focused runs execute only registered tests with the version-appropriate selector.
- A deliberate assertion → design failure. A removed or (separately) truncated `results.xml` →
  inconclusive, never pass. Compact and full displays match the XML/JSON verdicts; a rerun writes
  fresh complete XML.
Look for: skips counted as passes, import errors hidden as "results.xml not found", stale XML.

### 4. lint-synth — Lint, logical and physical synthesis (~35 min)
Intent: honest lint/synthesis outcomes; physical timing never faked.
Try:
- Verible `lint_mac_10g` is clean. Clean, warning, syntax-error and missing-tool inputs give
  distinct outcomes. A cross-Target duplicate is one finding with provenance; a native waiver waives
  only its finding; nonfatal warnings keep rc and report distinct; a file scope lints only those
  files; restoring gives a fresh clean result.
- `synth_mac_10g` is logical Yosys with slang, top `taxi_eth_mac_10g`. It reports fresh metrics and
  the real tool provenance, and makes no STA claim.
- Faults: missing or incompatible frontend → correct failure; latch fixture → critical; timing miss
  without threshold → advisory; violated threshold → design failure; baseline vs candidate → delta
  with both identities; restore → valid fresh metrics.
- `synth_mac_10g_physical` sets explicitly slang/Yosys → OpenROAD, `synth_mode: physical`,
  `ppa_profile: balanced`, `flatten: true`; the SDC puts four 6.4 ns clocks and 8 ns
  `ptp_sample_clk` on exact ports. A fresh baseline run gives timing/structural/PPA artifacts,
  per-clock coverage for each surviving clock (a missing clock proven optimized out) and reported
  unconstrained I/O and paths. Record image/tool/library/recipe identity; areas 10 and 13 must
  match.
Look for: `estimated_fmax_mhz` as physical timing, dropped clocks, false paths, forced passes.

### 5. interactive — Interactive Mode, MCP jobs and admission (~25 min)
Intent: a read-only Interactive child drives everything over MCP and changes nothing.
Depends on: areas 2–4; if they failed, use what works and log gaps.
Try:
- Start one long-lived Interactive child in the setup-complete Sandbox (Project cwd, MCP) and send
  `prompts/interactive.md`. It confirms runtime, cleanliness, Doctor and Targets, compares CLI and
  MCP discovery, runs the focused PFC test with a fresh FST (for area 6) and the TB-quality Reviewer
  on `sim_mac_10g`. The tree stays clean.
- A safe Interactive edit + commit gives an exact diff and history; then revert.
- MCP jobs: cancelling queued and running jobs reports "cancelled", not failure, and a new job still
  runs; a detached job returns `run_id` and polling gives its terminal result; after an inline
  client timeout, report lookup returns the latest durable report.
- Admission: two read-only Interactive jobs plus Ticket work respect class limits; jobs queued
  behind an active one are not preempted, Interactive first, peers FIFO; two edits in a disposable
  shared-tree fixture collide visibly.
- Compare Flow and Specialist names across help, cheat sheet, MCP and skills; log the public route
  per step. Seed one fault and recover using only the docs.
Look for: child writes, cancels shown as failures, lost `run_id`s, disagreeing catalogs.

### 6. bwave — FST traces, B-Wave queries and the Waveform Viewer (~35 min)
Intent: B-Wave is right on a real trace and a known oracle, and loud on bad input.
Try:
- Real trace: focused-PFC FST is fresh, nonempty, native, expected scopes/counts. Register alias and
  `_last`; alias resolves in a new context; stale/missing ones rejected. Create/list/resolve/delete
  markers.
- Run `list`, `signal`, `wave`, `value`, `find`, `sample`, `diff`, `distance`, `stats`, `stuck` on
  PFC request, frame start, XGMII, timestamp, reset and statistics signals, with sync/async views,
  explicit clock/reset, cycle tokens and typed time. Cross-check one request-to-frame latency with
  Cocotb.
- Oracle `fixtures/known-trace.vcd` (truth: `fixtures/known-trace-events.json`): `--json` on `list`,
  `find`, `value`, `stats` gives the documented envelope matching the oracle, other queries fall
  back to text; markers resolve via `value --at`, `diff`, `wave`; alias replace and in-place trace
  regeneration are picked up.
- Time model: physical, cycle and tick tokens hit the same event; reset include/skip work; literals
  and bit slices work; a bare async time, bad literal, unknown signal and width mismatch each give
  the documented error, then work once fixed.
- `--marker` exits 2 on every subcommand but `wave`. On `wave`: typed in-window marker → right
  column; out-of-window → omitted; negative or bare async → rejected; tick without transition →
  column kept; repeated name → last wins; two names on one tick → comma label.
- `--virtual` exits 2 on build, diff, list, signal, stats, stuck (never silently ignored). On
  distance, find, sample, value, wave a malformed definition exits 2 with a diagnostic and a
  composite one matches the oracle.
- Raw VCD, legacy, empty and truncated stores → actionable rejection; regenerate after. Also try
  `version`, `schema`, `docs` list/search/show, `skill`, legacy translation, a pattern miss, an
  ambiguous name, a bounded output limit.
- Viewer (VS Code only): open a scoped `bwave gui` with clock, PFC request, packet start, XGMII,
  statistics, start/end markers and cursor; read back over WCP and screenshot. Append keeps rows; a
  small max-signals cap holds; a dropped signal warns with the discrepancy; no clock → none added;
  no WCP → scoped request fails, bare GUI falls back to the editor; restore → exact view. From that
  client call status, targets, a Flow and a Specialist, and read the MCP schemas.
Look for: exit 0 with wrong values, ignored options, off-by-one markers, dropped signals.

### 7. isolation — Sandbox security boundary (~15 min)
Try, inside the Sandbox:
- No host home, SSH or Docker socket mounted; non-root user with restricted capabilities; a PDK
  write is refused and the PDK unchanged.
- An undeclared network route is blocked while provider calls work. A push to a run-owned Git server
  works from outside; from inside, a push to a separate probe ref is blocked by the network (not Git
  or auth) and the ref is unchanged.
- An interrupt leaves no descendant processes.

### 8. submodules — Disposable submodule companion Project (~35 min)
Intent: offline pinned-submodule rebuilds; a missing required one breaks simulation.
Try:
- `python3 fixtures/build_submodules.py <new-dir>` (uses `fixtures/fixture.core`) builds outer A/B,
  `deps/data` with nested `deps/data/nested/leaf`, `deps/unselected`, and a paired `.booley_project`
  pinning `deps/control`. Init it at B as a separate Project (same build/image), set `.gitmodules`
  URLs to unreachable `example.invalid` SSH, keep default-deny egress.
- Positive: a tiny Verification Ticket at B (normal create/run) has gitlinks, Baseline and report
  naming B and the paired repo; `sim_submodule_fixture_b` passes fresh (DATA 16'h2468, LEAF 8'h34);
  baseline-relative `synth_submodule_transport` A→B uses A, leaf A, control A for the baseline,
  never the newer checkout; materialized repos are detached, standalone (own `.git`, no
  remotes/alternates) and offline-built.
- `[submodules].paths`: omitted → all; `["deps/data"]` → data and leaf only; `[]` → nothing, and the
  simulation needing data fails; a non-gitlink path → no invented repo; paired `control` always
  materializes.
- Key proof: after a pass, move `deps/data` outside every search path, change nothing else; a
  warm-cache rerun must fail, then pass after restore. Repeat with only the leaf, and the top-level
  case in a cold fixture.
- In fresh variants, each fails hard with a remedy: missing submodule, dirty tracked file (never
  silently reset), shallow clone, missing historical object (no fetch fallback).
- A failure after one repo was created rolls back (pre-existing sentinel byte-identical); retry into
  a matching destination is accepted. Restore for a fresh pass; the real Taxi Project is unchanged.
Look for: stale-cache passes, silent fetches, newer checkout as baseline, half-created repos.

### 9. ticket1-observability — Verification Ticket and mutation campaign (~60 min)
Intent: a real Verification Ticket merges with strict Criteria and a real mutation score.
Depends on: a clean tree after area 5; if a Setup Target is missing, fix config first and log it.
Try:
- Create from `tickets/observability.md`; record the published Ticket Baseline and Board. Scope:
  only new `qa/taxi_eth_mac_10g/test_observability.{py,sv}` plus persistent
  `sim_mac_10g_observability`. Then `booley run`.
- Tests must check bad RX FCS (`tuser`, bad-FCS flag, counter 34); PFC on all 8 classes with quanta
  10…80 (exact TX/RX bitmap and quanta, counters 25 and 57); four-cycle underrun (XGMII error
  termination, counter 3); 16-bit completion tags and timestamp vs SFD; `tuser=0` counters vs
  `tuser=1` strings. Elaboration, simulation and a clean TB-quality review bind to the new Target.
- Mutation: `mutation_score` on `src/eth/rtl/taxi_eth_mac_10g.sv`, total 8, min 7. New tests hidden
  from the Mutation Tester; proposals steered to PFC req/ack, RX error, statistics, TX tag/timestamp
  and locked before running; pristine baseline passes; 8 isolated variants with first killing test
  each; source restored.
- `destination: done` gives local merge, cleanup and triage report; the Target stays selectable; no
  pre-existing file changes.
- Authoring negatives (disposable drafts): detail mode gives one complete draft; explicit guidance
  beats conflicting Project guidance; invalid Criterion/Scope/Target never enqueues; an approved
  draft seals exactly.
Look for: weakened upstream tests, mutants seeing new tests, missing restore, Target lost at merge.

### 10. seed-ticket2 — Seeded PFC fault and Bug Fix Ticket (~60 min)
Intent: the new oracle catches what upstream misses; a blind Developer restores byte-identical
upstream.
Depends on: area 9 merged; else hand-write an equivalent exact PFC class/quanta test on a run-owned
branch, log the workaround, continue.
Try:
- Rerun upstream and observability sims on clean merged RTL. Apply `fixtures/pfc-fault.md` (rotates
  `tx_pfc_req` at `taxi_mac_pause_ctrl_tx`), commit on a run-owned branch: upstream PFC still
  passes, the new test fails with class 0 seen as class 1, a fresh FST shows the rotation. Without
  that split, restore and skip the repair Ticket.
- Create from `tickets/repair.md` with the dependency slug read from the Ticket area 9 actually
  created; basis = seed commit, Scope = that RTL file; own `booley run`.
- Expect a fresh repro, evidence-based diagnosis and a byte-identical-to-upstream repair; upstream
  pass-to-pass, observability fail-to-pass; both elaborations and `lint_clean`; logical cells +0%;
  physical cells and critical path +0% with the same SDC/library/recipe; clean RTL bugs review,
  terminal protocol and spec reviews; done, merge, cleanup, triage report. On failure keep worktree,
  report and diff, then restore the post-Ticket-1 state.
Look for: Developer touching tests/config, changed unqualified-timing semantics, Criteria met by
stale evidence.

### 11. ticket-machinery — Ticket lifecycle, policy and resilience (~35 min)
Intent: Ticket engine edge cases, on cheap disposable Tickets.
Try:
- Lifecycle: queue → run → review → done; unmet dependency waits then releases; authorized done
  shortcut; human block/unblock; resume keeps commits across a path change. Triage: skill
  discoverable; approve a bounded correction; archive/reset spares; plain review→queue rejected.
- Scope: a bookkeeping edit is blocked; staged, modified, deleted, untracked dirt each reject the
  report; a safe out-of-Scope file is allowed and logged in `.runtime/scope_deviations.json`; every
  changed file needs a rationale; after recovery the report is accepted.
- Baseline: Developer-time control-input edits rejected; baseline-relative Ticket pinned;
  return-to-draft reseals a new basis; persistent/replacement/ephemeral Target plans land exactly.
- Criteria: a diagnostic call doesn't satisfy acceptance; rerun → current evidence, relevant edit →
  stale; fail-fix-pass keeps the failure; unmet mandatory blocks review, unmet optional needs
  justification; undeclared Flow call rejected; code-style and protocol review Criteria each give a
  report naming that focus.
- Integration: briefing HTML renders, JSON is deterministic; a renderer fault leaves work intact and
  regenerates without rerun; `BOOLEY_RUN_RESULT` parses; review and done destinations work.
- Resilience via `fixtures/provider-fault/` (see README): subscription limit → requeue; transient
  stall → bounded retry; crash → no retry; timeout → distinct outcome, no orphans; interrupt after a
  Flow then resume → no duplicate evidence. Remove the shim before a real call.
- Concurrency and operator: two runs claim two Tickets atomically with no cross-artifacts; class cap
  queues excess jobs; waiting jobs cancel; a tiny full queue blocks admission; human and machine
  Board agree; dry-run and check-ready change nothing; Console works; idle drained run exits
  cleanly.
- Policy: tier and role overrides recorded; invalid role or limit rejected before agent work;
  unattended runs get no fake approval; required vs optional report; active vs wall timeout; restore
  policy and relaunch.

### 12. campaign — Simulation Campaigns (~25 min)
Intent: one shared build, capped heavy jobs, isolated attempts, resumable batches.
Try:
- Copy `fixtures/simulation-campaign/` in, register `campaign.core`, merge `tests.toml` into the
  test catalog. `first` + `second` in one campaign → exactly one ready `shared_variant` bundle for
  both attempts, bytes unchanged (cross-check `validate_bundle.py`).
- Apply `parallel.toml` with `max_heavy = 3`; run `slow-first`, `slow-fail`, `slow-last` while
  sampling the SlotStore: overlap >1 and ≤3 counting the outer job, each `qa-shared-name.txt` in its
  own attempt, pass/fail/pass in manifest order with strict `fail`. Cross-check with
  `validate_parallel.py`.
- Kill the owned producer mid Taxi Cocotb multi-test batch; resume from the manifest → one new
  attempt rerunning the whole batch. The same ordered `test` array over MCP → nonempty pointers, ≤32
  observations, exact truncation metadata, same exit code (cross-check `validate_phase5.py`).
Look for: per-test rebuilds, cap overshoot, cross-attempt files, fail-fast omissions.

### 13. final-regression — Final state (~20 min)
Depends on: area 10 merged; else use the latest trustworthy state and say which.
Try:
- Rerun plain/deep Doctor, both sims, Verible, logical and fresh physical synthesis, B-Wave PFC
  checks. Pre-existing Taxi bytes match the pin; only `qa/taxi_eth_mac_10g/` and the persistent
  Target changed.

### 14. cleanup — Product cleanup (~20 min)
- Save a git bundle of accepted/seed/repair history and the physical reports in `evidence/`.
- With Booley's own commands, release Sandboxes (`booley session down`), processes, Ticket worktrees
  and branches, Project inventory entries, derived images, volumes and mounts; no owned process or
  container may remain.
- Remove the companion Project and clone unless the run skill keeps them for review; release the
  `resources.md` rows. Credentials, base images, caches and remotes are unchanged; nothing pushed.

## Known traps
- Ticket 2's dependency slug comes from the Ticket area 9 actually created; a pre-baked slug once
  cost a whole run.
- Gearbox off follows the pinned pytest driver, not the Makefile default; RX/TX keep IFG 12 and 0.
- `src/*/lib/taxi` symlinks point to the repo root (infinite walks); use `FUSESOC_IGNORE` or real
  paths, not `.f` hops.
- slang is mandatory (sv2v cannot handle the interfaces). `estimated_fmax_mhz` is not physical
  timing.
- The provider-fault shim only intercepts `codex exec`; remove it before recovery, never overwrite
  the real binary.
