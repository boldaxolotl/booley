# Taxi 10G MAC port and evolution

Port pinned upstream Taxi to Booley from a direct clone, then evolve `taxi_eth_mac_10g` through a
verification Goal (exact PFC/FCS/underrun/statistics tests, mutation campaign) and a bugfix
Goal for a seeded PFC fault the upstream tests miss. Also covers Setup with a derived Cocotb
image, Targets, Doctor, lint, logical/physical synthesis, Interactive Mode, B-Wave, a submodule
companion, Goal operations and Simulation Campaigns. Taxi's interfaces, recursive symlink, big
Python stack and five clocks surface stale caches, skipped tests and dropped clocks.

## Pins and prerequisites
- Booley: the candidate under test (set by the run skill; not pinned here).
- Taxi: https://github.com/fpganinja/taxi @ `cc70b270b910d369ab1ad7b3855e76399fd461f1`, fresh direct
  clone on a run-owned branch (no wrapper; no submodules).
- Needs: Docker ≥6 GB free plus run-dir room; Codex or Claude login; Sandbox Image with Verilator,
  Cocotb, Verible, Yosys+slang, OpenROAD; VS Code attached for the area-6 viewer.
- Primary: Ubuntu x86-64 + Codex CLI; rerun on Windows (Docker Desktop, WSL2), VS Code Codex or
  Claude CLI for coverage.
- Budget: 8 h for the primary run: 460 area minutes plus 20 contingency.
  Area 8 is an optional 35-minute follow-up outside this timebox. Log it as
  skipped unless a follow-up is requested. Run primary areas sequentially;
  Interactive children never overlap an active Goal in the same Project.
  Rerun on the other host/client only when asked.

## Mission-specific rules
- Taxi baseline is immutable: only Setup, Goal children and the area-10
  operator seed change it. Preserve the `.f` manifest and `src/eth/lib/taxi`
  symlink. Inject faults on disposable copies/branches, save failures and restore.
- Send `prompts/setup.md` and `prompts/interactive.md` verbatim. The repair
  child gets the failing assertion, expected/observed classes, upstream-test
  contrast, affected file and evidence pointers; hide seed location and fix.
- Follow the shared [Goal-child operating rules](../../shared/GOAL-CHILD.md)
  for every Goal area.

## Areas

### 1. setup — Clone, init and delegated Project Setup (~55 min)
Intent: reach a warning-free Project using only docs, packaged skills, cheat sheets and help.
Try:
- Record `booley --version` and import origin; `booley bootstrap --check-only` reports ready.
- Run `booley init`, then give one Setup agent `prompts/setup.md`; no second approval request
  (`SETUP-PLAN.md` is a record, not a gate). Expect Targets `sim_mac_10g`, `lint_mac_10g`,
  `synth_mac_10g`, `synth_mac_10g_physical`; a derived image from the full pinned `tox.ini` stack;
  explicit `[stealth] enabled = false`; `fixtures/taxi_mac_10g.sdc` as Project-owned config.
- Copy the full text of `prompts/setup.md` into outer `docs/taxi-contract.md`
  and verify the bytes match: the spec reviewer inlines this file, not linked
  documents. Commit it as the immutable visible spec for later Goal review.
- Nothing installs at runtime; the TB import works in the Sandbox; pins (pytest 8.3.4 … scapy 2.6.1)
  match `prompts/setup.md`.
- Compare config/cores/guidance with the plan (every deviation stated). Run the Setup findings
  handoff and native-parity comparison; compare documented selector lists with real ones.
- The `src/eth/lib/taxi -> ../../../` symlink is unchanged; sources resolve without a working host
  symlink (Windows). Rerun plain `booley init`: no drift. Another run with the other provider:
  issued auth/client policy follows.
- Image faults: changed dependency pin → stale image rebuilt; bounded post-setup hook in a new
  linked Goal workspace runs once; status/resume does not rerun it; nonzero hook blocks, fix + documented retry; opted-in
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
- Admission: two read-only Interactive jobs plus Goal child work respect admission caps;
  jobs queued behind an active one are not preempted, peers FIFO; two edits in a disposable
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

### 8. submodules — Disposable submodule companion Project (~35 min, optional follow-up)
Intent: offline pinned-submodule rebuilds; a missing required one breaks simulation.
Try:
- `python3 fixtures/build_submodules.py <new-dir>` (uses `fixtures/fixture.core`) builds outer A/B,
  `deps/data` with nested `deps/data/nested/leaf`, `deps/unselected`, and a paired `.booley_project`
  pinning `deps/control`. Init it at B as a separate Project (same build/image), set `.gitmodules`
  URLs to unreachable `example.invalid` SSH, keep default-deny egress.
- Positive: a child enters a tiny verification Goal in a linked worktree at B,
  naming the sim Target; its base and evidence name B and the paired repo; `sim_submodule_fixture_b` passes fresh (DATA 16'h2468, LEAF 8'h34);
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

### 9. goal-observability — Verification Goal and mutation campaign (~60 min)
Intent: a verification Goal yields exact observability tests and a real mutation score.
Depends on: clean Setup state and committed Project configuration/Goalsets.
Try:
- Create a linked worktree, start a Codex child and send `$booley-goal` with
  `goals/observability.md`. The new files are
  `qa/taxi_eth_mac_10g/test_observability.{py,sv}` and the ordinary Target
  `sim_mac_10g_observability`; existing Taxi source/tests must stay unchanged.
- Tests check bad RX FCS (`tuser`, bad-FCS flag, counter 34); all 8 PFC classes
  with quanta 10…80 (exact TX/RX bitmap and quanta, counters 25/57); four-cycle
  underrun (XGMII error termination, counter 3); 16-bit completion tags and
  timestamp vs SFD; `tuser=0` counters vs `tuser=1` strings. Elab, full sim and
  clean TB-quality evidence must bind to the intended Goal/Target.
- Mutation on `src/eth/rtl/taxi_eth_mac_10g.sv` has explicit total 8/min 7.
  New tests stay hidden from the Mutation Tester; proposals target PFC req/ack,
  RX error, statistics and TX tag/timestamp, and lock before execution. Preserve
  pristine baseline, 8 isolated variants, first killing tests and source restoration.
- Finish with fresh evidence at a clean committed HEAD; inspect package and
  non-Stealth history commit, then the operator integrates the printed branches using the shared rules.
  The new Target stays selectable for the repair Goal.
Look for: weakened upstream tests, mutants seeing new tests, missing restoration,
incorrect evidence binding or a Target lost during integration.

### 10. goal-repair — Seeded PFC fault and bugfix Goal (~60 min)
Intent: the new oracle catches what upstream misses; a blind Goal child restores upstream bytes.
Depends on: area 9 finished and the printed branches integrated. If it failed, log and skip repair;
the operator does not author replacement tests.
Try:
- Rerun both sims on integrated RTL. Apply `fixtures/pfc-fault.md` in a disposable
  copy and commit: upstream PFC still passes, new test fails with class 0 seen
  as class 1 and fresh FST shows the rotation. Save the failure before repair.
  Without that split, restore and skip this Goal.
- Cut a linked worktree from the seed commit; start a new Codex child with
  `$booley-goal` plus `goals/repair.md` and only bounded visible diagnostics.
  Require the child's fresh reproduction before editing and evidence-based diagnosis.
- Repair must be byte-identical to upstream; both full sims and elaborations
  pass, lint is clean, logical cells +0%, physical cells/critical path +0% with
  identical SDC/library/recipe. RTL bugs review is clean; protocol/spec reviews done.
- Finish, save package/history and integrate the printed branches using the shared rules. On failure explicitly
  abandon, keep checkpoint/report/diff in evidence and restore post-observability state.
Look for: child edits to tests/config, unqualified timing changes or stale Goal evidence.

### 11. goal-operations — Concurrent children, Dashboard and warnings (~60 min)
Intent: separate worktrees isolate Goal evidence and expose operator diagnostics without changing policy.
Try:
- Start two Codex children in two clean linked worktrees with distinct slugs
  and `goals/operations.json`. Every MCP call that accepts `work_dir` (listed in
  `qa/shared/GOAL-CHILD.md`) includes it, including independent calls against
  the primary checkout. Evidence and artifacts cannot
  cross records. Check the Sandbox admission cap and cancellation of waiting jobs.
- Run `booley session enter -- booley dashboard` and Goal status short/long
  views; sessions, Goals and Jobs agree with each child's durable record.
- In disposable copies run `booley session enter -- booley doctor` (host Goal
  checks are unavailable). Provoke `goals.worktree-missing` with an owned
  workspace moved aside and restored; provoke quiet-session warnings by waiting
  the reported threshold or setting it before entry. Quiet never clears a record.
- Finish or explicitly abandon both concurrent records. Only then enter a third
  disposable Goal for the Project-wide Protected Input probe: change
  `booley.toml`, observe warnings, discarded evidence and blocked Finish, restore
  byte-exactly and rerun. Never contaminate concurrent Goals with this probe.
- Test wrong worktree identity and occupied-worktree entry refusals, fresh evidence
  after relevant edits, unmet mandatory Goal blocking Finish, and explicit abandon
  preserving branch/worktree. Save diagnostics and resolve all records before cleanup.
Look for: cross-record evidence, missing work_dir accepted during active work
by a call that takes it, auto-abandon or a protected-input edit silently
accepted.

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
  checks. Pre-existing Taxi bytes match the pin; only `qa/taxi_eth_mac_10g/` and the ordinary
  Target changed.

### 14. cleanup — Product cleanup (~20 min)
- Save a git bundle of accepted/seed/repair history and the physical reports in `evidence/`.
- With Booley's own commands, release Sandboxes (`booley session down`), processes, Goal worktrees
  and branches, Project inventory entries, derived images, volumes and mounts; no owned process or
  container may remain.
- Remove the companion Project and clone unless the run skill keeps them for review; release the
  `resources.md` rows. Credentials, base images, caches and remotes are unchanged; nothing pushed.

## Known traps
- Integrate the Goal Branch, and the Project-data branch when `booley worktree new` printed a paired checkout, before starting repair.
- Gearbox off follows the pinned pytest driver, not the Makefile default; RX/TX keep IFG 12 and 0.
- `src/*/lib/taxi` symlinks point to the repo root (infinite walks); use `FUSESOC_IGNORE` or real
  paths, not `.f` hops.
- slang is mandatory (sv2v cannot handle the interfaces). `estimated_fmax_mhz` is not physical
  timing.
