# UART clean-room greenfield

Build a standalone UART from frozen OpenTitan docs only, driving Booley from host bootstrap
through `init --scaffold`, `/booley-setup new`, an Interactive MMIO baseline, a feature Goal
and up to two repair Goals. Then grade the accepted RTL with an independent evaluator the
Goal child never sees. Booley can accept wrong RTL, and this gap exposes it. The fresh Project
also exercises the host, auth, runtime, policy, Doctor and feedback surfaces.

## Pins and prerequisites
- Booley: the candidate under test (set by the run skill; not pinned here).
- OpenTitan corpus: https://github.com/lowRISC/opentitan @
  `615d3c74fadbbf674c8ca05a70f91094989849fb`. Use only the seven files in `spec/corpus/`: UART
  `README.md`, `doc/theory_of_operation.md`, `doc/programmers_guide.md`, `doc/registers.md`,
  `doc/interfaces.md`, `doc/block_diagram.svg`, and the Apache-2.0 `LICENSE`. Before you start,
  check their hashes against `spec/corpus-manifest.json`.
- Scenario inputs: `spec/qa_uart.sv` (interface-only stub), `spec/mmio-addendum.md`,
  `spec/timing-addendum.md` and `spec/contract.md`. The contract file inlines
  registers/MMIO only; it omits timing, serial behavior, theory and programmer
  guidance, which the child reads from the separate allowed sources. Origin: a new, empty, run-owned Git repository.
- Host: Docker with at least 6 GB free, plus artifact space; Verilator and Yosys come from the
  standard image. The evaluator needs cocotb 2.1.0 and Icarus outside the Sandbox.
- Also: a Codex or Claude client; the previous published Booley release (upgrade and legacy
  cases); a digest-pinned external image with the same Booley build and EDA tools. Optional:
  VS Code with Dev Containers, a run-owned Git server.
- Hosts: written for Ubuntu and Windows (Docker Desktop WSL2) with Codex CLI, Codex VS Code
  and Claude CLI; rerun on another host or client for coverage. Area 2's CRLF cases are
  Windows-only.
- Budget: 8 h for the primary run: 460 area minutes, including 90 for feature
  work and 45 for bounded repair, plus 20 contingency. Areas 12, 16 and 17 are
  optional follow-up work outside this timebox (55 minutes total); log them as
  skipped unless requested. Run primary areas sequentially; Interactive children
  never overlap an active Goal in the same Project.

## Mission-specific rules
- **Clean room.** "Developer" means every agent that writes UART code: the Setup client, the
  Interactive client and the Goal child. It may see only `spec/corpus/`,
  `spec/qa_uart.sv`, both addenda, `spec/contract.md`, `spec/corpus-manifest.json`,
  the `prompts/` and `goals/`
  payloads, Booley's docs, `--help` and packaged skills, and the ordinary Project. No OpenTitan
  HJSON, regtool output, RTL, tests, UVM, DIFs, models, current docs or upstream browsing; if
  it fetches any, record a finding.
- **The evaluator stays independent** (`evaluator/CONTRACT.md`, `evaluator/README.md`). Keep
  `evaluator/`, the seed, generated cases, controls, build logs and raw observations outside the
  Project, the Sandbox, every mount and the developer's network reach. Prove isolation from real
  mounts, filesystem and network routes; separate paths alone prove nothing. The evaluator
  compiles only candidate RTL at the accepted commit, never the candidate TB or its pass
  sentinel. Never paste evaluator content into a prompt or Goal request.
- **Acceptance is not conformance**; only the evaluator decides conformance. Never tune
  oracles, cases or seed to the candidate; one seed and frozen manifest per run; keep failures.
- **Payloads verbatim:** `prompts/*.md`, `goals/*.md`; no hidden coverage, mutation-score or
  relative-PPA Goals.
- **Spec precedence:** the addenda first, then register definitions and precise behavior text,
  then examples. The 64-byte RX FIFO and the 32-byte TX FIFO are normative.
- **Stealth is off** (`[stealth] enabled = false`): ordinary paths and literal commit messages.
- **Credentials:** store, clear and swap only disposable credentials; confirm at the end that
  the borrowed login is unchanged. Tokens must never reach the Project, logs, feedback exports
  or evidence; `auth status` may name the provider and auth class, never the secret. Before
  closing, search `evidence/` and the Project for token-shaped strings and redact them. A
  leaked token is a `bug`.
- **Host policy** cases run only under a disposable `XDG_CONFIG_HOME` on a dedicated Docker
  daemon. Never touch the real host policy (`fixtures/host-policy/README.md`).

- Follow the shared [Goal-child operating rules](../../shared/GOAL-CHILD.md)
  for every Goal area.

## Areas

### 1. install — Candidate install routes (~15 min)
Intent: the published install routes deliver exactly the declared build.
Try:
- Use the documented base-interpreter user install. In a new shell, version, executable,
  metadata and source commit agree, and nothing imports from a checkout. License is Apache-2.0;
  generated state and artifacts stay local.
- On an externally managed distro, plain `pip` gets the documented rejection; the documented
  isolated route then installs the same build.
Look for: a stale `booley` on PATH, version/commit mismatch, docs that don't match the route.

### 2. bootstrap-init — Bootstrap, scaffold and init variants (~30 min)
Intent: onboard a greenfield Project on an empty origin; bootstrap runs implicitly.
Try:
- Bootstrap check-only reports `pending`. Then run `booley init --scaffold` without an explicit
  bootstrap: choose the provider, Verilator for sim and lint, a SystemVerilog TB and ASIC
  support. It should reconcile the host automatically, after which check-only reports clean.
- Repeat `init` and bootstrap: source, config, generated state, images and skills must not
  drift. A repeated scaffold must keep authored files and Target names. Inspect the core,
  counter, skills, image and Stealth state, then set `[stealth] enabled = false`.
- Init flags: check-only on stale disposable state reports work and changes nothing;
  `--force` restores owned generated state and keeps a user sentinel file; a seed shows up in
  config; skip-credentials touches no credentials; the provider choice matches the issued auth
  and client policy. Host-only `init` inside the Sandbox gets an actionable refusal.
- Break a run-owned managed resource and force-repair it: caches and sentinel survive, then
  check-only reports ready. Windows CRLF fixture: auto LF reconciliation, the compatibility
  flag, dirty/hardlinked/protected files refused with bytes unchanged, then fix and retry.
Look for: check-only runs that change state, init overwriting user files, vague refusals.

### 3. setup-interactive — Setup and Interactive MMIO baseline (~45 min)
Intent: packaged Setup plus an Interactive session give a green, committed baseline.
Try:
- In the Sandbox, run `/booley-setup new` with `prompts/setup.md` supplied up front. Plain,
  deep (verifies the counter) and plain Doctor again are all clean.
- Copy `spec/` (never `evaluator/`) into the Project and give an Interactive
  child `prompts/interactive.md`: `qa_uart` interface plus minimal CTRL
  reset/read/write, an MMIO smoke test, and `sim_uart`, `lint_uart`, `synth_uart`.
  Through MCP/Flows the smoke sim self-checks and passes, lint is clean, Yosys
  gives a fresh netlist and reports, then Doctor is clean.
- A fresh trace read back through B-Wave matches the stimulus. Commit the clean
  baseline before area 4. Run this Interactive area with no active Goal in the Project.
Look for: supplied choices ignored, stale artifacts called fresh, MCP calls without matching
artifacts, the full UART implemented too early.

### 4. goal-feature — Full UART Goal and review (~90 min child time)
Intent: Goal Mode delivers a full design using the frozen public contract.
Depends on: area 3's committed minimal baseline; if Setup failed, record and skip design work.
Try:
- Commit copied `goals/feature.md` and `goals/repair.md` in Project `goalsets/`,
  alongside initialization's seeded files and managed ignore rules. Create a
  linked worktree and send a Codex child `$booley-goal` plus `goals/feature.md`
  and allowed spec files. Review concrete Goals, Targets and the spec-review
  binding to `spec/contract.md`, which contains the frozen register definitions
  and MMIO addendum.
- The child authors owned `rtl/` and `tb/` assets. Elaboration cannot substitute
  for full sim; sim, lint, synth and clean RTL-bugs/protocol/spec/TB-quality
  reviews must yield fresh Goal-bound evidence and real artifacts.
- Finish requires clean committed HEAD and Session Summary; save record ID,
  package, base, protected-input status and final commit. Operator inspection
  covers the whole diff. Integrate the outer Goal Branch and preserve the
  non-Stealth history commit. This scaffold has no separate Project Git repo
  and therefore no paired branch; clean up the printed outer checkout.
Look for: sources outside the clean-room list, corpus conflicts papered over,
stale evidence or Finish claiming completion with dirty files. Conformance is
judged independently in area 5.

### 5. evaluate — Independent conformance evaluation (~30 min)
Intent: grade the accepted RTL against the public contract. Most real design bugs show up here.
Try:
- Confirm isolation (see the rules). Generate a fresh 32-hex-digit seed and run
  `python cases.py --seed <seed> --output <operator>/manifest.json`. Freeze the manifest, and
  check that every mandatory corner and the 96 seeded supplements are present.
- Run `python controls.py <operator>/controls`. Each of the 43 controls must pass clean, fail
  when corrupted, and pass again once restored. A misbehaving control is an evaluator `qa-bug`.
- Build `candidate-inputs.json` from the accepted commit and run `run.py` per
  `evaluator/README.md`.
- Families: BUS (handshakes, single outstanding, 4-clock latencies, exactly-once effects,
  strobes, RO/WO, misaligned/unmapped, empty reads), REG (offsets, reset values, reserved bits,
  ALERT_TEST), TXRX (all 256 bytes, back-to-back, full duplex), BAUD (exact-a/b, fractional,
  NCO=0), PARITY, FIFO, WATERMARK, IRQ (INTR_TEST, level vs event, tx_empty vs tx_done), ERROR
  (stop, break, timeout), FILTER, LOOP, OVERRIDE, HISTORY and RESET (idle, active TX/RX,
  occupied FIFOs, pending MMIO).
- Reuse an earlier result only if the commit, evaluator digest and manifest are all identical.
Look for: RTL mismatches that the Goal's reviews and self-checking TB missed. Record each as a
`bug` in Booley's acceptance quality, citing the case ID. Operational timeouts and simulator
failures are blocked observations, not RTL defects.
Depends on: area 4; if it never finished, evaluate the last commit, labeled unaccepted.

### 6. goal-repair — Bounded conformance repair Goals (~45 min)
Intent: check whether a Goal child repairs real mismatches from limited feedback.
Try:
- Only after a real area-5 mismatch on finished work: cut a fresh linked
  worktree from the integrated result and send a new Codex child `$booley-goal`
  plus `goals/repair.md`, for attempt 1/2. Supply at most five failing diagnostic
  excerpts: case ID, stimulus, expected/observed, timing and waveform excerpt.
  Save exact excerpts outside the child's reach; never reveal evaluator code,
  complete cases or seed. The original public spec remains visible.
- Require a fresh reproduction before fixing, an authored regression and the
  complete original Goals. Finish and inspect package/history, then integrate
  the outer Goal Branch. Rerun the entire frozen evaluator manifest with the same seed
  on the new commit; at most two repairs. Unfinished feature work is not a repair.
- If a repair cannot finish, explicitly abandon and preserve its checkpoint;
  grade that commit only as unfinished, keeping prior evaluator failures.
Look for: weakened checks, edited Protected Inputs, new regressions or a repair
that succeeds only on the bounded-feedback subset.

### 7. final-regression — Final tree health (~15 min)
Try: on the final accepted tree, run the full developer sim, Verilator lint, Yosys synthesis and
Doctor. In Git, confirm a clean accepted commit, the local merge, ordinary source and core
paths, and literal commit messages (Stealth is off).
Look for: stale reports, Doctor drift after Goal work.

### 8. reviews-mutation — Specialist reviews, isolation, mutation (~35 min)
Try:
- Seed a real RTL bug on a disposable branch. The review reports `done` (advisory); an
  unresolved MINOR-or-worse finding leaves the clean Goal unmet; an explicit waiver makes
  it clean; a relevant edit makes both stale; a rerun on fixed source gives fresh evidence.
- `review-spec`, `review-security`, `review-tb-quality` reports each name their focus. A
  conflicting Project guide beats the bundled one, which stays present. Specialist workspaces
  and prompt manifests hold only the allowed RTL or TB side; source is restored after success
  and after a bounded failure.
- Mutation: the default dry run gives the documented 10/10 and auto sizing lands in 3–25. A
  locked small N/K campaign with killed and surviving mutants shows a pristine baseline,
  per-mutant outcomes and first killing tests. A rerun reuses the lock; regen writes a new one.
- Disposable Goals from `goals/reviews.json` cover security-, spec- and
  TB-quality reviews; each Goal is bound/evaluated and each report type-specific.
  Before entry, combine that list with an ad-hoc mutation Goal on `sim_uart`
  with `min_detected: 2`, `total: 3`
  and a `scope` listing actual RTL source files resolved by that Target.
  Finish or explicitly abandon the record before leaving this area.
Look for: stale reviews still counted as clean, TB content leaking into an RTL-side workspace.

### 9. auth — Authentication and secret boundary (~20 min)
Try: clearing a disposable store gives `unconfigured`. Store a disposable credential through
each documented form (store, stdin); status shows provider and auth class, never the secret.
Configure subscription, API and automatic selection in turn; the reported class must follow the
documented precedence. Restore the credential: auth is ready and borrowed credentials are
unchanged. Search the Project, logs and `evidence/` for credential material.
Look for: secrets echoed in errors or logs, precedence that differs from the docs.

### 10. runtime-lifecycle — Session runtime and legacy containers (~30 min)
Try:
- From no runtime: `session up`, `status`, `validate`; `down` leaves no owned descendants.
  `session enter` passes a child's exit code through (0 and nonzero). Interrupting a child
  with a descendant keeps signal semantics and leaves no descendants; then `up`/`enter` again.
- `refresh` after a permitted change gives a new immutable identity; an injected replacement
  validation failure restores the old runtime; refreshing a VS Code-owned runtime is refused.
- Legacy VS Code containers (`fixtures/legacy-client/README.md`,
  `fixtures/legacy-client/docker_fixture.py`): exactly one authenticated container is
  replaced; headless, foreign, ambiguous and multiple candidates are refused; a post-stop
  failure prints the exact `docker start` command, which works with the same volumes once the
  bind is restored.
Look for: orphaned processes, the wrong container replaced, rollbacks that leave nothing up.

### 11. doctor-upgrade — Doctor waivers and upgrade review (~20 min)
Try:
- A benign warning gives an actionable WARN; a matching unexpired waiver waives it; expiring
  the waiver, or separately changing the warning's identity, stops it applying. A waived hard
  failure stays FAIL. A config change reports stale freshness; restoring it gives clean.
- Create a Project with the previous release, then install the candidate: a pending review
  names both endpoints; after the documented heal and ack, nothing is pending, Doctor clean.
Look for: waivers that hide failures, a pending review that never clears.

### 12. host-policy — Host policy file (~25 min, optional follow-up)
Follow `fixtures/host-policy/README.md`.
Try:
- With no config file, the documented defaults apply.
- One field at a time: `fixtures/host-policy/idle.toml` (Sandbox expires after 30 s idle),
  `fixtures/host-policy/cap.toml` (a second Project hits the one-Sandbox cap),
  `fixtures/host-policy/egress.toml` (named host allowed; an unlisted name at the same
  endpoint denied).
- Each `invalid-{scheme,path,port,ip,wildcard,key}.toml` is rejected before bootstrap changes
  anything, with file bytes unchanged. Then `fixtures/host-policy/recovery.toml` succeeds.
- Run `legacy.toml` and `both-tables.toml` through real init and Doctor commands
  as specified in the fixture README; verify migration output, unchanged host
  bytes, canonical-only precedence, and rejection of host keys in Project `[sandbox]`.
- Observed provider, relay and egress routes match the declared policy.
Look for: typos accepted silently, a partial bootstrap after a rejection.

### 13. sandbox-isolation — Sandbox isolation (~20 min)
Try:
- In the Sandbox: non-root restricted user; no host home, SSH keys or Docker socket mounted;
  PDK writes refused; undeclared routes blocked while provider calls work; interrupting owned
  work leaves no descendants.
- Push-deny: a control client outside the runtime can push to a run-owned Git server. The same
  push from the Sandbox to a separate probe ref must be blocked by the network boundary, not by
  credentials or the server, and the ref stays unchanged.
- Local lifecycle: active/finished/abandoned Goals retain status, logs and Review Packages;
  automatic Doctor persists its report.
Look for: writable mounts that should be read-only, missing local lifecycle reports.

### 14. interactive-surface — MCP, discovery, custom flows, feedback (~25 min)
Try:
- Launch: bare `booley`/chat starts the configured provider in the Project directory. With a
  missing client executable (disposable), you get an actionable error; restore it. Make a safe
  edit and commit it, and check the exact local diff and history.
- MCP: the Interactive Sandbox exposes Goal entry/status/proposal/Finish schemas. Disabling an
  endpoint removes it with an explaining diagnostic; re-enabling restores its schema. Flow and
  Specialist names agree across help, cheat sheet, MCP and skills; note the route you used.
- Custom Flow: install a deterministic Flow with an endpoint. Discovery shows
  its schema; a dry run validates inputs without artifacts; a real run reports success;
  Doctor flags a syntax fault; disabling hides it; restoring brings discovery and runs back.
- Feedback (`booley feedback`): add a defect, friction, impression and win, then list them.
  Concurrent appends keep every entry; test triage and a filed mark (filed entries leave the
  next report); regenerate after a restart. A local export with a planted, benign
  private-looking string redacts exactly that string, and ordinary report generation makes no
  redacted companion.
- Seed a fault mid-flow and resume using only the documented recovery.
Look for: docs, help and MCP disagreeing; lost feedback entries; incomplete redaction.

### 15. sim-campaign — Simulation Campaign attempt contract (~20 min)
Setup: copy `fixtures/simulation-campaign/` to `qa-campaign/` in the Project, register its
`campaign.core`, and merge its `tests.toml`. Use one TOML fragment per campaign.
Try:
- `runtime-inputs.toml`, tests `alpha` and `beta`: distinct run directories and vectors, owned
  files cleaned after publication, one unchanged shared Simulator Bundle.
- `literal-cwd.toml`, `max_heavy > 1`: attempts on the same literal directory don't overlap,
  but a separate templated campaign started then does (per-directory lock, not global).
- `immutable-presim.toml`: the hook sees no `BOOLEY_BUILD_ROOT` and exits 73. Expect an
  attributed setup failure, no pass, and unchanged source, bundle and snapshot hashes.
- `legacy-presim.toml`: `legacy-per-test` is disclosed, each test gets its own private build,
  and nothing claims a shared bundle.
- Optionally cross-check with `fixtures/simulation-campaign/validate_campaign.py`.
Look for: global serialization, a leaked build root, a failed hook reported as a pass.

### 16. external-image — Externally supplied image (~15 min, optional follow-up)
Try: copy the MMIO smoke sources into a disposable Project with separate state and select the
pinned image through the published config. Start the runtime and confirm the exact Booley
version and image digest, then run Doctor and the smoke test. Stop and recreate the runtime
through the documented lifecycle: the digest is unchanged, the Project persists, and nothing is
rebuilt. Rerun the smoke test, then remove the owned runtime and Project but keep the image.
Look for: hidden downloads or rebuilds, a digest that drifts.

### 17. gui-client — VS Code client (~15 min, only if VS Code is available, optional follow-up)
Try: attach VS Code to the Sandbox and run the baseline prompt; call status, Targets, a Flow
and a Specialist through its MCP (read-only; no Goals entered). Screenshot the fresh MMIO
trace in the Waveform Viewer; headless or B-Wave-only readback doesn't count.
Look for: attachment or MCP failures that appear only in the GUI.

### 18. cleanup — Product cleanup (~20 min)
Exercise Booley's own cleanup: `session down`, explicit Goal abandon where needed, printed Git
worktree removal using the printed USAGE steps, and Project deregistration. Record any owned process or container left behind and any
unrelated state touched. Keep the evaluator seed, manifest and results in the run dir, outside
every Project. Release every `resources.md` row and nothing else. Compare borrowed resources
(credentials, caches, host policy, the supplied external image, pre-existing containers) with
their starting state.

## Known traps
- Expected corpus conflicts (not Booley bugs; the developer should report them, not guess):
  RX prose says 32 bytes (normative is 64); an example uses an undocumented TX-overflow
  interrupt; the RX watermark example shifts by three, hidden by a zero threshold; the theory
  text names `STATUS.BREAK`, but only `INTR_STATE.rx_break_err` (bit 5) exists.
- Cut repairs from the integrated finished outer commit; this scaffold has no paired repository.
- The timing addendum's finite bounds are scenario choices that the developer also receives:
  RX sync ≤ B/4, TX start ≤ 2B, and the timeout IRQ in 30B–34B at NCO=0x4000, VAL=32. Waiting
  past them is an observation timeout, not an RTL bug.
- If the Interactive work overshoots the baseline, area 4 starts from a moved baseline; record it.
- Evaluator controls validate the evaluator's observation paths, not candidate conformance.
- Host policy is daemon-wide; without a dedicated daemon, cap and egress cases disrupt others.
- The Simulation Campaign fixture is SystemVerilog under Icarus, so its core sets `flow: sim` with
  `flow_options.iverilog_options: [-g2012]`. Without it the build fails on `string`.
