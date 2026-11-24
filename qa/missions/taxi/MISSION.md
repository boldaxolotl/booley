# Taxi 10G MAC port and evolution

Port pinned upstream Taxi to Booley from a direct clone, then evolve `taxi_eth_mac_10g` through a
verification work (exact PFC/FCS/underrun/statistics tests, mutation campaign)
and a bugfix
for a seeded PFC fault, each accepted as a Measured Commit the upstream tests
miss. Also covers Setup with a derived Cocotb
image, Targets, Doctor, lint, logical/physical synthesis, Developer Agent
sessions, B-Wave, a submodule
companion, Target Metrics and measurement, and Simulation Records. Taxi's
interfaces, recursive
symlink, big
Python stack and five clocks surface stale caches, skipped tests and dropped clocks.
The operator runs it through [`booley-qa-run`](../../booley-qa-run/SKILL.md), which owns the run
directory, findings format, and cleanup bookkeeping; this file says where to look.

## Pins and prerequisites
- Booley: the candidate under test (set by the run skill; not pinned here).
- Taxi: https://github.com/fpganinja/taxi @ `cc70b270b910d369ab1ad7b3855e76399fd461f1`, fresh direct
  clone on a run-owned branch (no wrapper; no submodules).
- Needs: Docker ≥6 GB free plus run-dir room; Codex or Claude login; Sandbox Image with Verilator,
  Cocotb, Verible, Yosys+slang, OpenROAD; VS Code attached for the area-6 viewer.
- Optional: Vivado on x86-64 Linux, plus a paid license and relay, for area 14.
- Primary: Ubuntu x86-64 + Codex CLI; rerun on Windows (Docker Desktop, WSL2), VS Code Codex or
  Claude CLI for coverage.
- Budget: 8 h for the primary run: 460 area minutes plus 20 contingency.
  Areas 8 and 14 are optional follow-up work outside this timebox (75 minutes
  total). Log them as skipped unless a follow-up is requested. Run primary areas sequentially;
  a child's measured work and Interactive children never share a worktree.
  Rerun on the other host/client only when asked.

## Mission-specific rules
- Taxi baseline is immutable: only Setup, the children of areas 10-11 and the
  area-11
  operator seed change it. Preserve the `.f` manifest and `src/eth/lib/taxi`
  symlink. Inject faults on disposable copies/branches, save failures and restore.
- Send `prompts/setup.md` and `prompts/interactive.md` verbatim. The repair
  child gets the failing assertion, expected/observed classes, upstream-test
  contrast, affected file and evidence pointers; hide seed location and fix.
- Follow the shared [Developer Agent child
  rules](../../shared/DEVELOPER-CHILD.md)
  for areas 10 and 11.

## Areas

### 1. setup — Clone, init and delegated Project Setup (~55 min)
Intent: reach a warning-free Project using only docs, packaged skills, cheat sheets and help.
Try:
- Record `booley --version` and import origin; `booley bootstrap --check-only` reports ready.
- Run `booley init`, then give one Setup agent `prompts/setup.md`; no second approval request
  (`SETUP-PLAN.md` is a record, not a gate). Save `.booley/SETUP-PLAN.md` under `evidence/setup/`.
  Expect Targets `sim_mac_10g`, `lint_mac_10g`, `lint_mac_10g_verible`, `synth_mac_10g`,
  `synth_mac_10g_physical`; a derived image from the
  pinned `tox.ini` stack with cocotb at 2.1.0;
  explicit `[stealth] enabled = false`; the prompt's inlined SDC as
  Project-owned config, byte-identical to `fixtures/taxi_mac_10g.sdc`.
- Copy the full text of `prompts/setup.md` into outer `docs/taxi-contract.md`
  and verify the bytes match: the spec reviewer inlines this file, not linked
  documents. Commit it as the immutable visible spec for the later spec reviews.
- Nothing installs at runtime; the TB import works in the Sandbox; pins (pytest 8.3.4 … scapy 2.6.1)
  match `prompts/setup.md`.
- Compare config/cores/guidance with the plan (every deviation stated). Run the Setup findings
  handoff and native-parity comparison; compare documented selector lists with real ones.
- The `src/eth/lib/taxi -> ../../../` symlink is unchanged; sources resolve without a working host
  symlink (Windows). Rerun plain `booley init`: no drift. Another run with the other provider:
  issued auth/client policy follows.
- Image faults: changed dependency pin → stale image rebuilt; bounded post-setup hook in a new
  new worktree (`booley worktree new`) runs once; status/resume does not rerun
  it; nonzero hook blocks, fix + documented retry; opted-in
  disposable host skill is read-only.
Look for: re-approval requests, Taxi edits, missing `cocotb_test`/`pytest` (imported at TB module
scope), implicit stealth, symlink loops, stale Session Runtime after `init` rebuilds the image.

### 2. targets-doctor — Target catalog, selection and Doctor (~25 min)
Intent: discovery shows exactly what will run, and CLI and MCP agree.
Try:
- `sim_mac_10g` uses the upstream `test_taxi_eth_mac_10g.sv` wrapper and `.py` module, the full
  closure of `taxi_eth_mac_10g.f`, and Verilator Cocotb; a traced run yields `trace.fst`. All 7
  tests are registered: rx, tx, tx-alignment, tx-underrun, tx-user-error, lfc and pfc.
- Applicable Targets carry all `prompts/setup.md` parameters; logical and physical synth Targets
  share sources, top, parameters.
- `booley targets` list/filter/JSON agree with MCP on identity, selectors, EDA programs and top.
  Resolved parameters and inputs come from the CLI detail view alone. Use one fully qualified
  selector. Changing run-owned config refreshes discovery without touching Taxi.
- Negatives: a colliding bare name is rejected, the qualified name picks the exact core; an invented
  per-call parameter override is rejected; two Targets with different values report their own; a
  restored valid selector runs.
- Deep Doctor runs only selected Targets; the bad selftest is hidden in listings yet evidenced in
  deep Doctor; authored and vendored names are kept. A many-core fixture lists the exact qualified
  set; an invalid core behind `FUSESOC_IGNORE` is excluded while the valid one resolves.
- Plain, deep, plain Doctor: warning-free, no waivers (`lint_mac_10g_verible` is not
  Doctor-selected). Break config, Doctor, restore: clean.
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
- Verilator `lint_mac_10g` (default warning set, flat wrapper top) is clean. Verible
  `lint_mac_10g_verible` fails with the syntax error at `taxi_axis_xgmii_rx_64.sv:589`, reported
  as a syntax error naming Verible. On `lint_mac_10g`, clean, warning, syntax-error and
  missing-tool inputs give distinct outcomes. A cross-Target duplicate is one finding with provenance; a native waiver waives
  only its finding; nonfatal warnings keep rc and report distinct; `--scope` keeps only findings
  in the named files while the whole Target is still linted; restoring gives a fresh clean result.
- `synth_mac_10g` is logical Yosys with slang; its top is a Project-owned flat wrapper around
  `taxi_eth_mac_10g`. It reports fresh metrics and the real tool provenance, and makes no STA
  claim. Every synthesis-only source copy has its diff against the Taxi file recorded.
- Faults: missing or incompatible frontend → correct failure; latch fixture → critical; timing miss
  without threshold → advisory; violated threshold → design failure; baseline vs candidate → delta
  with both identities; restore → valid fresh metrics.
- `synth_mac_10g_physical` sets explicitly slang/Yosys → OpenROAD, `synth_mode: physical`,
  `ppa_profile: balanced`, `flatten: true`; the SDC puts four 6.4 ns clocks and 8 ns
  `ptp_sample_clk` on exact ports. A fresh baseline run gives timing/structural/PPA artifacts,
  per-clock coverage for each surviving clock (a missing clock proven optimized out) and reported
  unconstrained I/O and paths. Record image/tool/library/recipe identity; areas
  11 and 13 must
  match.
Look for: `estimated_fmax_mhz` as physical timing, dropped clocks, false paths, forced passes.

### 5. interactive — Developer Agent session, MCP jobs and admission (~25 min)
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
- Admission: two read-only Interactive jobs plus a running measurement respect
  admission caps;
  jobs queued behind an active one are not preempted, peers FIFO; two edits in a disposable
  shared-tree fixture collide visibly.
- Compare Flow and Specialist names across help, cheat sheet, MCP and skills; log the public route
  per step. Seed one fault and recover using only the docs.
Look for: child writes, cancels shown as failures, lost `run_id`s, disagreeing catalogs.

### 6. bwave — FST traces, B-Wave queries and the Waveform Viewer (~35 min)
Intent: B-Wave is right on a real trace and a known oracle, and loud on bad input.
Try:
- Real trace: focused-PFC FST is fresh, nonempty, queryable, expected scopes/counts. Register
  alias and `_last`; alias resolves in a new context; stale/missing ones rejected. Create/list/resolve/delete
  markers.
- Run `list`, `signal`, `wave`, `value`, `find`, `sample`, `diff`, `distance`, `stats`, `stuck` on
  PFC request, frame start, XGMII, timestamp, reset and statistics signals, with sync/async views,
  explicit clock/reset, cycle tokens and typed time. Cross-check one request-to-frame latency with
  Cocotb.
- Oracle `fixtures/known-trace.vcd` (truth: `fixtures/known-trace-events.json`): `--format json` on
  `list`, `signal`, `wave`, `value`, `find`, `distance`, `stats` gives the documented envelope
  matching the oracle; `sample`, `diff`, `stuck` reject it with exit 2; markers resolve via
  `value --at`, `diff`, `wave`; alias replace and in-place trace regeneration are picked up.
- Time model: physical, cycle and tick tokens hit the same event; reset include/skip work; literals
  and bit slices work; a bare async time, bad literal, unknown signal and width mismatch each give
  the documented error, then work once fixed.
- `--marker` exits 2 on every subcommand but `wave`. On `wave`: typed in-window marker → right
  column; out-of-window → omitted; negative or bare async → rejected; tick without transition →
  column kept; repeated name → last wins; two names on one tick → comma label.
- `--virtual` exits 2 on build, diff, list, signal, stats, stuck (never silently ignored). On
  distance, find, sample, value, wave a malformed definition exits 2 with a diagnostic and a
  composite one matches the oracle.
- Raw VCD, old `.bwave`, empty and truncated stores → actionable rejection; regenerate after. Also
  try `--version`, `schema`, `docs topics`/`search`/`show`, `skill`, the old v0.1 flag form
  (`--wave`, `--find`), a pattern miss, an ambiguous name, and a `--limit` output cap.
- Viewer (VS Code only): open a scoped `bwave gui` with clock, PFC request, packet start, XGMII,
  statistics, start/end markers and cursor; check the printed readback and screenshot. Append keeps
  rows; a small max-signals cap holds; a dropped signal warns with the discrepancy; no clock → none
  added; no attached window → scoped request fails, bare GUI falls back to the editor; restore →
  exact view. From that client call status, targets, a Flow and a Specialist, and read the MCP
  schemas.
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
  `deps/data` with nested `deps/data/nested/leaf`, `deps/unselected`, and a paired `.booley`
  pinning `deps/control`. Init it at B as a separate Project (same build/image), set `.gitmodules`
  URLs to unreachable `example.invalid` SSH, keep default-deny egress.
- Positive: in a linked worktree at B, `sim_submodule_fixture_b` passes fresh
  (DATA 16'h2468, LEAF 8'h34),
  and `booley measure` of B with a `[sim_submodule_fixture_b.sim]` table runs it
  in a clean checkout at B's pins;
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

### 9. metrics — Target Metrics and measuring the baseline (~60 min)
Intent: Target Metrics declared from the docs alone load, measure the baseline,
and fail closed when a number cannot be produced.
Depends on: areas 3-4.
Try:
- `booley cheat --metrics`; merge `fixtures/metrics.toml` into
  `.booley/metrics.toml`; `booley measure --validate` is clean. Negatives, one
  file listing all at once: an unquoted `rx_clk.fmax_mhz_min` (a `write
  instead:` line), a relative bound, an unknown metric (lists the Flow's
  metrics), a table with no metric; `[defaults.synth]` alone measures nothing.
  Restore.
- Commit, `booley measure --base <the commit that registered the tests>` (the
  default Base is the upstream pin, so the registry would read as "tests
  changed"): one child Flow per table in a clean checkout, a stored Measurement
  Run, green header, exit 0; the Dashboard session line reads `Measured <sha>`.
  Again: the stored run, no Flow, exit 0. `--again` stores a second run;
  `--again --target synth_mac_10g` is never stored or Measured (reports under
  `job_logs/standalone/<run_id>/`); `--json` matches the Markdown
  report.
- Turn the physical tracked numbers into bounds with slack outside the observed
  values, per-clock keys quoted and only for clocks the report lists; commit;
  Measured.
- `error` verdicts, each in a disposable commit: a per-clock bound naming a
  clock the report does not list (misspelled or optimized out) is `error`, exit
  2, never judged against another clock; a Pre-Sim Command or hook that calls
  `git` fails in the measurement checkout (no `.git`); measuring `HEAD` with a
  dirty tracked file notes it and measures only the commit.
Look for: a bound judged on the wrong clock, an `error` row shown as met or
unmet, uncommitted work measured, a `--target` run that makes a commit Measured.

### 10. observability — Verification work as a Measured Commit, and a mutation campaign (~60 min)
Intent: a child's verification work yields exact observability tests, a real
mutation tally and a Measured Commit.
Depends on: area 9's Measured baseline.
Try:
- Start a child per the shared child rules with `prompts/observability.md`. The
  new files are `qa/taxi_eth_mac_10g/test_observability.{py,sv}` and the
  ordinary Target `sim_mac_10g_observability`; existing Taxi source/tests must
  stay unchanged.
- Tests check bad RX FCS (`tuser`, bad-FCS flag, counter 34); each PFC class
  alone (one-hot request) and all 8 together, quanta 10…80 (exact TX/RX bitmap
  and the requested classes' quanta, counters 25/57); four-cycle
  underrun (XGMII error termination, counter 3); 16-bit completion tags and
  timestamp vs SFD; `tuser=0` counters vs `tuser=1` strings. Elab, full sim and
  clean TB-quality evidence must bind to the new Target.
- The child asks before adding the `[sim_mac_10g_observability.sim]` table:
  approve with a reason. The Measured Commit's report, measured with `--base`
  the commit the child started from, shows the new table with every bound met
  and an amber "tests changed" for the approved registry addition.
- Mutation on `src/eth/rtl/taxi_eth_mac_10g.sv` has explicit total 8/min 7. The
  report shows a pristine baseline, 8 Mutants each built and tested in
  isolation, each detected Mutant's first killing test, and an unchanged source
  tree afterward.
- Before accepting the oracle, check that it sees area 11's fault: on a
  disposable copy of the child's commit, apply `fixtures/pfc-fault.md`, run
  the new exact-PFC test, and confirm it fails with a class mismatch. Save the
  result under `evidence/observability/`, then delete the copy. If the test
  passes, do not accept: send the child back to the prompt's Exact PFC
  requirement it misses. Never reveal the seed to the child.
- Integrate the worktree's branch using the shared rules. The new Target stays
  selectable for the repair.
Look for: weakened upstream tests, a source tree changed by mutation, a table
added without approval, a Target lost during integration.

### 11. repair — Seeded PFC fault: unmet bound, base run and measured repair (~60 min)
Intent: the new oracle catches what upstream misses; a blind child restores
upstream bytes and the repair is Measured against its Base.
Depends on: area 10 integrated; if it failed, log and skip repair; the operator
does not author replacement tests.
Try:
- Rerun both sims on integrated RTL. Apply `fixtures/pfc-fault.md` in a
  disposable copy and commit: upstream PFC still passes, new test fails with
  class 0 seen as class 1 and fresh FST shows the rotation. Save the failure
  before repair. `booley measure` on the seed: the observability table's
  `tests_failed` is unmet, exit 1, red header, failing rows first; the upstream
  table stays met. Without that split, restore and skip.
- Base run: the integrated observability commit is the Base and is already
  Measured, so its stored run is the base run. `--base` naming a commit with no
  stored run prints `booley measure --all <base>`; the verdict never depends on
  a base run.
- Cut a linked worktree from the seed commit; start a new child with
  `prompts/repair.md` and only bounded visible diagnostics. Require the child's
  fresh reproduction before editing and evidence-based diagnosis.
- Repair must be byte-identical to upstream and Measured with `--base` at the
  observability commit: both full sims and elaborations pass, `lint_mac_10g` is
  clean, logical cells +0%, physical cells/critical path +0% (Δ 0) with
  identical SDC/library/recipe. RTL bugs review is clean; protocol/spec reviews
  done; keep the review reports.
- Message-only amend: amend the Measured repair commit's message; `booley
  measure` prints the stored run, starts no Flow, exits 0, and the Dashboard
  reads `Measured <new sha>`. Amend with a one-byte content change: measured
  again.
- Loosened rules, each in a disposable commit with and without a `notes` entry,
  measured with `--base <the commit holding area 9's final rules>` (the default
  Base on this run-owned branch is the upstream pin, which has no
  `metrics.toml`, so rules would read as additions): raise a `_max`, lower a
  `_min`, `off = [...]`, move a bound to `track`, delete a table → amber "rules
  loosened" with the note or "(no note)"; a `tests.toml` edit → "tests changed";
  a `booley.toml` edit appears as a protected-set change. Restore byte-exactly.
- Integrate the repair using the shared rules; on failure keep its branch,
  report and diff, and restore the post-observability state.
Look for: Δ against the wrong base, a base run deciding a verdict, a
message-only amend measured again or a content amend kept Measured, a loosening
shown green.

### 12. sim-record — Simulation Records (~25 min)
Intent: one shared build, memory-admitted parallel attempts, isolated attempts, resumable batches.
Try:
- Copy `fixtures/simulation-record/` in, register `campaign.core`, merge
  `tests.toml` into the
  test catalog. `first` + `second` in one Simulation Record → exactly one ready
  `shared_variant` bundle for
  both attempts, bytes unchanged (cross-check `validate_bundle.py`).
- Run each slow item once first: an item with no Resource History runs alone. Apply
  `parallel.toml`, and set a `[sandbox].memory` whose Memory Budget fits several of the items but
  not all three at once (restart the Session as `docs/user/SANDBOX.md` says; read `budget_mb` and
  each item's `reservation_mb` in the claim files to confirm, and adjust if every item fits;
  restore the setting after the area). Run `slow-first`, `slow-fail`, `slow-last` and measure the
  simulator processes: more than one item runs at once, at least one item waits in the queue (copy
  its `w-*.json` claim or queue line) and starts when memory is freed, with no exact admitted
  count; each `qa-shared-name.txt` is in its own attempt, pass/fail/pass in manifest order with strict `fail`.
  Cross-check with `validate_parallel.py`; record the timeline in the format its module docstring
  defines.
- Kill the owned producer mid Taxi Cocotb multi-test batch and copy the interrupted attempt's
  `attempt.json`; resume from the manifest → one new attempt rerunning the whole batch
  (cross-check `validate_phase5.py` with the Simulation Record directory and
  that copy). The same ordered
  `test` array over MCP → authenticated artifact references, ≤32 observations, exact truncation
  metadata, same exit code; keep the MCP run's report invocation directory and pass it to
  `validate_phase5.py`.
Look for: per-test rebuilds, cap overshoot, cross-attempt files, fail-fast omissions.

### 13. final-regression — Final state (~20 min)
Depends on: area 11 merged; else use the latest trustworthy state and say which.
Try:
- Rerun plain/deep Doctor, both sims, `lint_mac_10g` (clean), `lint_mac_10g_verible` (the
  expected syntax error), logical and fresh physical synthesis, B-Wave PFC checks. Pre-existing Taxi bytes match the pin; only `qa/taxi_eth_mac_10g/` and the ordinary
  Target changed.

### 14. vivado-fpga — Provisioned Vivado, Grants, FPGA Flow, licenses (~40 min, Linux, optional follow-up)
Intent: host EDA provisioning is exact, read-only and revocable, and the FPGA Flow grades honestly.
Try:
- Register host Vivado under a run-owned name, then list and show it (canonical paths match;
  symlink spelling may differ). Grant it to this Project and set `[flows.fpga] enabled = true`
  and `[eda.vivado] provisioning = "host"`. After every Grant change, reissue the Session before
  any check (`session down`, `init --seed`, `session validate`, `session up`). The Vivado mount
  is read-only.
- Add a run-owned FPGA Target on the flat `synth_mac_10g` wrapper with `out_of_context: true`
  and a clock-only XDC (the wrapper has more ports than a small part has pins), on any part your
  license covers (WebPACK parts need none), with its Doctor check enabled. Dry run lists part,
  top, XDC and sources. An uncached run gives a fresh PASS with a routed DCP plus timing and
  utilization reports; the same inputs hit the cache and `--no-cache` runs fresh; a bad design
  gives rc 1; a baseline/candidate pair gives a delta.
- Revoke the Grant and grade the Flow before recovering: exit 2, no Vivado run, no mount. Then
  re-grant, reissue, and Doctor is clean. `booley projects forget` on a granted root is refused;
  after revoking, it removes exactly that root.
- With a paid license only: a License Profile attaches through the Grant; a relay fault fails,
  restoring the relay recovers, and unapproved destinations stay blocked.
Look for: a stale Session spec after regrant, borrowed Grants or installations changed, cached
results passed off as fresh.

### 15. cleanup — Product cleanup (~20 min)
- Save a git bundle of accepted/seed/repair history and the physical reports in `evidence/`.
- With Booley's own commands, release Sandboxes (`booley session down`),
  processes, child worktrees
  and branches, Project inventory entries, run-owned Grants, EDA registrations and License
  Profiles, derived images, volumes and mounts; no owned process or
  container may remain.
- Remove the companion Project and clone unless the run skill keeps them for review; release the
  `resources.md` rows. Credentials, base images, caches and remotes are unchanged; nothing pushed.

## Known traps
- Integrate the observability worktree's branch before starting repair.
- The area-11 seed rotates PFC classes, so an all-classes (0xFF) request is
  unchanged by it; only one-hot class checks catch it. Area 10 confirms the
  new test fails on the seed before acceptance.
- Gearbox off follows the pinned pytest driver, not the Makefile default; RX/TX keep IFG 12 and 0.
- `src/*/lib/taxi` symlinks point to the Project Root (infinite walks); use `FUSESOC_IGNORE` or real
  paths, not `.f` hops.
- slang is mandatory (sv2v cannot handle the interfaces). `estimated_fmax_mhz` is not physical
  timing.
- `taxi_eth_mac_10g` has interface ports, so slang cannot take it as a top: synthesis uses a
  Project-owned flat wrapper, and `lint_mac_10g` lints that same top so both check one hierarchy. slang also rejects `$size` of an interface array and some static
  functions; Project-owned synthesis-only copies of those files are allowed, with the diff
  recorded as a stated deviation. Taxi files stay untouched.
- cocotb 2.0.1 from upstream `tox.ini` does not install on the Sandbox's Python 3.14; the
  contract pins 2.1.0.
- No clean Verible run exists on the pinned RTL: Verible cannot parse the concatenation select at
  `taxi_axis_xgmii_rx_64.sv:589`. Verilator with `-Wall` adds about 360 style warnings; the clean
  baseline is Verilator's default warning set on the flat wrapper.
- Cocotb Targets trace through a VCD that Booley converts to FST and reject `--trace-fst`.
- MCP Target discovery has no resolved view; resolved parameters and inputs are CLI-only.
