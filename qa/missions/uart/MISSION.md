# UART clean-room greenfield

Build a standalone UART from frozen OpenTitan docs only, driving Booley from Host Bootstrap
through `init`, `/booley-setup new` (which runs `init --scaffold`), an Interactive MMIO baseline,
a feature Goal and up to two repair Goals. Then grade the accepted RTL with an independent
evaluator the Goal child never sees: Booley can accept wrong RTL, and this mission measures that
gap. The fresh Project also exercises the host, auth, runtime, policy, Doctor and feedback
surfaces. The `booley-qa-run` skill runs this mission; Goal areas also follow the shared
[Goal-child operating rules](../../shared/GOAL-CHILD.md).

## Pins and prerequisites
- Booley: the candidate under test (set by the run skill; not pinned here).
- OpenTitan corpus: https://github.com/lowRISC/opentitan @
  `615d3c74fadbbf674c8ca05a70f91094989849fb`. Use only the seven files in `spec/corpus/`: UART
  `README.md`, `doc/theory_of_operation.md`, `doc/programmers_guide.md`, `doc/registers.md`,
  `doc/interfaces.md`, `doc/block_diagram.svg`, and the Apache-2.0 `LICENSE`. Before you start,
  check their hashes against `spec/corpus-manifest.json`.
- Scenario inputs: `spec/qa_uart.sv` (interface-only stub), `spec/mmio-addendum.md`,
  `spec/timing-addendum.md` and `spec/contract.md`. The contract inlines only the register
  definitions and the MMIO addendum; the developer reads timing, serial behavior, theory and
  programmer guidance from the other allowed sources. Origin: a new, empty, run-owned Git
  repository.
- Host: Docker with at least 6 GB free, plus artifact space; Verilator, Verible and Yosys come
  from the standard image. The evaluator needs cocotb 2.1.0 and Icarus outside the Sandbox.
- Also: a Codex or Claude client; the previous published Booley release (upgrade and legacy
  cases); a digest-pinned external image with the same Booley build and EDA tools. Optional:
  VS Code with Dev Containers, a run-owned Git server.
- Hosts: written for Ubuntu and Windows (Docker Desktop WSL2) with Codex CLI, Codex VS Code
  and Claude CLI; rerun on another host or client for coverage. Area 2's CRLF cases are
  Windows-only.
- Budget: 8 h for the primary run: 460 area minutes, including 90 for feature
  work and 45 for bounded repair, plus 20 contingency. Areas 12 and 16 are
  optional follow-up work outside this timebox (40 minutes total); log them as
  skipped unless requested. Run primary areas sequentially; Interactive children
  never overlap an active Goal in the same Project.

## Mission-specific rules
- **Clean room.** "Developer" means every agent that writes UART code: the Setup client, the
  Interactive client and the Goal child. It may see only `spec/corpus/`,
  `spec/qa_uart.sv`, both addenda, `spec/contract.md`, `spec/corpus-manifest.json`,
  the `prompts/` and `goals/` payloads, Booley's docs, `--help` and packaged skills, and the
  ordinary Project. No OpenTitan HJSON, regtool output, RTL, tests, UVM, DIFs, models, current
  docs or upstream browsing; if it fetches any, record a finding.
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
  or evidence; `booley auth --status` may name the provider and auth class, never the secret.
  Before closing, search `evidence/` and the Project for token-shaped strings and redact them.
  A leaked token is a `bug`.
- **Host policy** cases run only under a disposable `XDG_CONFIG_HOME` on a dedicated Docker
  daemon. Never touch the real host policy (`fixtures/host-policy/README.md`).

## Areas

### 1. install — Candidate install routes (~15 min)
Intent: the published install routes deliver exactly the declared build.
Try:
- Use the documented install route (`docs/user/INSTALL.md`: pipx; `uv tool install` in
  `docs/user/TROUBLESHOOTING.md`). In a new shell, version, executable, metadata and source
  commit agree, and nothing imports from a checkout. License is Apache-2.0; generated state and
  artifacts stay local.
- On an externally managed distro, plain `pip` gets the documented rejection; the documented
  isolated route then installs the same build.
Look for: a stale `booley` on PATH, version/commit mismatch, docs that don't match the route.

### 2. bootstrap-init — Bootstrap, scaffold and init variants (~30 min)
Intent: onboard a greenfield Project on an empty origin by the documented order: Host Bootstrap,
then `init`.
Try:
- `booley bootstrap --check-only` reports `pending` and exits 1. Plain `booley init` before
  Bootstrap refuses, names `booley bootstrap`, and changes nothing. Run `booley bootstrap`;
  check-only now reports clean. Then run plain `booley init` and choose the provider. Run
  `booley init --scaffold qa_uart` with flags for Verilator sim and lint, a SystemVerilog TB and
  ASIC support: it asks nothing, keeps `[agent]`, and reports its defaults.
- Repeat `init` and Bootstrap: source, config, generated state, images and skills must not
  drift. A repeated scaffold must keep authored files and Target names. Inspect the core,
  counter, skills, image and Stealth state, then set `[stealth] enabled = false`.
- Init flags: `--check-only` on stale disposable state reports work and changes nothing;
  `--force` restores owned generated state and keeps a user sentinel file; `--seed` writes only
  the folder's Interactive Mode devcontainer; `--skip-credentials` touches no credentials; the
  `--provider`/`--auth` choice matches the issued auth and client policy. `init` inside a
  Booley-issued Sandbox gets an actionable refusal.
- Break a run-owned managed resource and force-repair it: caches and sentinel survive, then
  check-only reports ready. Windows CRLF fixture: automatic LF reconciliation, the
  `--fix-line-endings` compatibility flag, dirty/hardlinked/protected files refused with bytes
  unchanged, then fix and retry.
Look for: check-only runs that change state, init overwriting user files, vague refusals.

### 3. setup-interactive — Setup and Interactive MMIO baseline (~45 min)
Intent: packaged Setup plus an Interactive session give a green, committed baseline.
Try:
- On the host, run `/booley-setup new` with `prompts/setup.md` supplied up front, then
  continue in the Sandbox. Plain, deep (`--deep` runs the scaffold counter) and plain Doctor
  again are all clean.
- Copy `spec/` (never `evaluator/`) into the Project and give an Interactive
  child `prompts/interactive.md`: `qa_uart` interface plus minimal CTRL
  reset/read/write, an MMIO smoke test, and `sim_uart`, `lint_uart`, `lint_uart_verible`,
  `synth_uart`. Through MCP Flows the smoke sim self-checks and passes, Verilator and Verible
  lint are clean, Yosys gives a fresh netlist and reports, then Doctor is clean.
- A fresh trace read back through B-Wave matches the stimulus. Commit the clean
  baseline before area 4. Run this Interactive area with no active Goal in the Project.
Look for: supplied choices ignored, stale artifacts called fresh, MCP calls without matching
artifacts, the full UART implemented too early.

### 4. goal-feature — Full UART Goal and review (~90 min child time)
Intent: Goal Mode delivers a full design using the frozen public contract.
Depends on: area 3's committed minimal baseline; if Setup failed, record and skip design work.
Try:
- Commit copied `goals/feature.md` and `goals/repair.md` in Project `goalsets/`. Create a
  linked worktree and send a Codex child `$booley-goal` plus `goals/feature.md` and allowed spec
  files. Review the concrete Goals, Targets and the spec-review binding to `spec/contract.md`.
- The child authors owned `rtl/` and `tb/` assets. Elaboration cannot substitute
  for full sim; sim, lint, synth and clean RTL-bugs/protocol/spec/TB-quality
  reviews must yield fresh Goal-bound evidence and real artifacts.
- Finish requires a clean committed HEAD and a Session Summary; save the record ID,
  Review Package, base, protected-input status and final commit. Inspect the whole diff.
  Integrate the outer Goal Branch per the Goal-child rules (this non-Stealth scaffold gets a
  Snapshot, so there is no paired branch) and preserve the non-Stealth history commit.
Look for: sources outside the clean-room list, corpus conflicts papered over,
stale evidence or Finish claiming completion with dirty files. Conformance is
judged independently in area 5.

### 5. evaluate — Independent conformance evaluation (~30 min)
Intent: grade the accepted RTL against the public contract. Most real design bugs show up here.
Depends on: area 4; if it never finished, evaluate the last commit, labeled unaccepted.
Try:
- Confirm isolation (see the rules). Generate a fresh 32-hex-digit seed and run
  `python cases.py --seed <seed> --output <operator>/manifest.json`. Freeze the manifest, and
  check that every mandatory corner and the 96 seeded supplements are present.
- Run `python controls.py <operator>/controls`. Each of the 45 controls must pass clean, fail
  when corrupted, and pass again once restored. A misbehaving control is an evaluator `qa-bug`.
- Build `candidate-inputs.json` from the accepted commit and run `run.py` per
  `evaluator/README.md`. Its `root` must be a Git checkout whose `HEAD` is that `commit` (the
  Goal worktree or a detached clone): the evaluator reads source bytes from the commit through
  Git, not from uncommitted working-tree edits.
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

### 6. goal-repair — Bounded conformance repair Goals (~45 min)
Intent: check whether a Goal child repairs real mismatches from limited feedback.
Try:
- Only after a real area-5 mismatch on finished work: cut a fresh linked worktree from the
  integrated finished commit and send a new Codex child `$booley-goal` plus `goals/repair.md`,
  for attempt 1/2. Supply at most five failing diagnostic excerpts: case ID, stimulus,
  expected/observed, timing and waveform excerpt. Save the exact excerpts outside the child's
  reach; never reveal evaluator code, complete cases or seed. The original public spec remains
  visible.
- Require a fresh reproduction before fixing, an authored regression and the
  complete original Goals. Finish, inspect the Review Package and history, then integrate
  the outer Goal Branch. Rerun the entire frozen evaluator manifest with the same seed on the
  new commit (`run.py --phase-seconds 2700`); at most two repairs. Unfinished feature work is
  not a repair.
- If a repair cannot finish, explicitly abandon and preserve its checkpoint;
  grade that commit only as unfinished, keeping prior evaluator failures.
Look for: weakened checks, edited Protected Inputs, new regressions or a repair
that succeeds only on the bounded-feedback subset.

### 7. final-regression — Final tree health (~15 min)
Try: on the final accepted tree, run the full developer sim, Verilator and Verible lint, Yosys
synthesis and Doctor. In Git, confirm a clean accepted commit, the local merge, ordinary source
and core paths, and literal commit messages (Stealth is off).
Look for: stale reports, Doctor drift after Goal work.

### 8. reviews-mutation — Specialist reviews, isolation, mutation (~35 min)
Try:
- Seed a real RTL bug on a disposable branch. A `done` review only reports; an unresolved
  MINOR-or-worse finding leaves a `clean` review Goal unmet; an explicit waiver makes it clean;
  a relevant edit makes both stale; a rerun on fixed source gives fresh evidence.
- The `rtl_spec`, `rtl_security` and `tb_quality` review reports each name their focus. A
  conflicting Project `tb_style_guide.md` in the Booley Directory takes precedence over the
  bundled TB guide, which stays present. Specialist workspaces and prompt manifests hold only
  the allowed RTL or TB side; source is restored after success and after a bounded failure.
- Mutation: a small explicit mutation criterion (`total`/`min_detected`) on the real UART, with
  detected and surviving Mutants, shows a pristine baseline, per-Mutant outcomes and first
  killing tests. Sizing, selection and seeds are the
  [mutation mission](../mutation/MISSION.md)'s.
- Disposable Goals from `goals/reviews.json` cover security, spec and
  TB-quality reviews; each Goal is bound and evaluated, and each report is type-specific.
  Before entry, combine that list with an ad-hoc mutation Goal on `sim_uart`
  with `min_detected: 2`, `total: 3` and a `scope` listing actual RTL source files resolved by
  that Target. Finish or explicitly abandon the record before leaving this area.
Look for: stale reviews still counted as clean, TB content leaking into an RTL-side workspace.

### 9. auth — Authentication and secret boundary (~20 min)
Try: `booley auth --clear` on a disposable store, then `booley auth --status` reports no
credential found and exits 1. Store a disposable credential with `--token-stdin`, and for Codex
also through the prompt (for Claude, plain `booley auth` mints a real token through
`claude setup-token`, so skip it). Status shows provider and auth class, never the secret.
Record `subscription`, `api-key` and `auto` in turn (`booley init --auth`); the reported class
must follow the documented precedence. Restore the credential: auth is ready and borrowed
credentials are unchanged. Search the Project, logs and `evidence/` for credential material.
Look for: secrets echoed in errors or logs, precedence that differs from the docs.

### 10. runtime-lifecycle — Session runtime and legacy containers (~30 min)
Try:
- From no runtime: `booley session up`, `status`, `validate`; `down` leaves no owned
  descendants. `session enter -- <cmd>` passes the command's exit code through (0 and
  nonzero). Interrupting a child with a descendant keeps signal semantics and leaves no
  descendants; then `up`/`enter` again.
- `session refresh` after a permitted change gives a new immutable identity; an injected
  replacement failure names the failing step and what is still running, and
  rerunning `session refresh` converges; refreshing a VS Code-owned runtime
  is refused.
- Legacy VS Code containers (`fixtures/legacy-client/README.md`): a running VS Code container
  with an outdated Booley Directory layout is refused, never migrated: nothing stops or is
  removed, and the message names the removal and `booley init --seed` repair. Following that
  repair reopens cleanly. Headless, foreign, ambiguous and multiple candidates are refused.
Look for: orphaned processes, a container stopped or removed, a failure that
names no step or leaves a rerun unable to converge.

### 11. doctor-upgrade — Doctor waivers and upgrade review (~20 min)
Try:
- A benign warning gives an actionable WARN; a matching unexpired waiver waives it; expiring
  the waiver, or separately changing the warning's identity, stops it applying. A waived hard
  failure stays FAIL. A config change reports stale freshness; restoring it gives clean.
- Create a Project with the previous release, then install the candidate: a pending review
  (`booley upgrade status`) names both versions; after `/booley-heal` and
  `booley upgrade acknowledge`, nothing is pending and Doctor is clean.
Look for: waivers that hide failures, a pending review that never clears.

### 12. host-policy — Host policy file (~25 min, optional follow-up)
Follow `fixtures/host-policy/README.md`.
Try:
- With no config file, the documented defaults apply.
- One field at a time: `fixtures/host-policy/idle.toml` (Sandbox expires after 30 s idle),
  `fixtures/host-policy/cap.toml` (a second Project hits the one-Sandbox cap),
  `fixtures/host-policy/egress.toml` (named host allowed; an unlisted name at the same
  endpoint denied).
- Each `invalid-{scheme,path,port,ip,wildcard,key}.toml` is rejected before Bootstrap changes
  anything, with file bytes unchanged. Then `fixtures/host-policy/recovery.toml` succeeds.
- Run `legacy.toml` and `both-tables.toml` through real init and Doctor commands
  as the fixture README specifies; verify migration output, unchanged host
  bytes, canonical-only precedence, and rejection of host keys in Project `[sandbox]`.
- Observed provider, relay and egress routes match the declared policy.
Look for: typos accepted silently, a partial Bootstrap after a rejection.

### 13. sandbox-isolation — Sandbox isolation (~20 min)
Try:
- In the Sandbox: non-root restricted user; no host home, SSH keys or Docker socket mounted;
  PDK writes refused; undeclared routes blocked while provider calls work; interrupting owned
  work leaves no descendants.
- Push-deny: a control client outside the runtime can push to a run-owned Git server. The same
  push from the Sandbox to a separate probe ref must be blocked by the network boundary, not by
  credentials or the server, and the ref stays unchanged.
- Local lifecycle: active, finished and abandoned Goals retain status, logs and Review
  Packages; automatic Doctor persists its report.
Look for: writable mounts that should be read-only, missing local lifecycle reports.

### 14. interactive-surface — MCP, discovery, custom flows, feedback (~25 min)
Try:
- Launch: in the Sandbox, bare `booley` (or `booley chat`) starts the configured provider in the
  current working directory. With a missing client executable (disposable), you get an
  actionable error; restore it. Make a safe edit and commit it, and check the exact local diff
  and history.
- MCP: the Interactive Sandbox exposes the Goal entry, status, proposal and Finish schemas.
  Disabling a Flow or Specialist (`enabled = false`) removes its tool; re-enabling restores its
  schema. Flow and Specialist names agree across help, cheat sheet, MCP and skills; note the
  route you used.
- Custom Flow: install a deterministic Flow with an endpoint. Discovery shows
  its schema; a dry run validates inputs without artifacts; a real run reports success;
  Doctor flags a syntax fault; disabling hides it; restoring brings discovery and runs back.
- Feedback (`booley feedback`): add a defect, friction, impression and win, then list them.
  Concurrent appends keep every entry; test triage and a filed mark (filed entries leave the
  next export); regenerate the report after a restart. A local export with a planted, benign
  private-looking string redacts exactly that string, and ordinary report generation makes no
  redacted companion.
- Seed a fault mid-Flow and resume using only the documented recovery.
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
pinned image with `[sandbox].image`. Start the runtime and confirm the exact Booley version and
image digest, then run Doctor and the smoke test. Stop and recreate the runtime through the
documented lifecycle: the digest is unchanged, the Project persists, and nothing is rebuilt.
Rerun the smoke test, then remove the owned runtime and Project but keep the image.
Look for: hidden downloads or rebuilds, a digest that drifts.

### 17. cleanup — Product cleanup (~20 min)
Exercise Booley's own cleanup: `booley session down`, explicit Goal abandon where needed,
worktree removal per the Goal-child rules, and `booley projects forget`. Record any owned
process or container left behind and any unrelated state touched. Keep the evaluator seed,
manifest and results in the run dir, outside every Project. Release every `resources.md` row
and nothing else. Compare borrowed resources (credentials, caches, host policy, the supplied
external image, pre-existing containers) with their starting state.

## Known traps
- Expected corpus conflicts (not Booley bugs; the developer should report them, not guess):
  RX prose says 32 bytes (normative is 64); an example uses an undocumented TX-overflow
  interrupt; the RX watermark example shifts by three, hidden by a zero threshold; the theory
  text names `STATUS.BREAK`, but only `INTR_STATE.rx_break_err` (bit 5) exists.
- The timing addendum's finite bounds are scenario choices that the developer also receives:
  RX sync ≤ B/4, TX start ≤ 2B, and the timeout IRQ in 30B–34B at NCO=0x4000, VAL=32. Waiting
  past them is an observation timeout, not an RTL bug.
- If the Interactive work overshoots the baseline, area 4 starts from a moved baseline; record it.
- Evaluator controls validate the evaluator's observation paths, not candidate conformance.
- Host policy is daemon-wide; without a dedicated daemon, cap and egress cases disrupt others.
- `session`, `goal`, `feedback` and `upgrade` work but are hidden from top-level
  `booley --help`; their absence there is not a discovery finding.
- The Simulation Campaign fixture is SystemVerilog under Icarus, so its core sets `flow: sim` with
  `flow_options.iverilog_options: [-g2012]`. Without it the build fails on `string`.
- The QA host's canonical install is `pip install --user --break-system-packages`, which is not a
  documented user route. Verify the documented pipx/uv route in an isolated HOME, and don't
  report the missing `pip --user` docs as a gap.
- A candidate whose version string equals the latest release (only the commit differs) can't
  raise a real pending upgrade review; reviews are keyed on the version. Simulate one by editing
  `reviewed_through` and record that you did.
