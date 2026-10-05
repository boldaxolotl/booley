<!-- Template for .booley_project/SETUP-PLAN.md — written by the booley-setup
     skill's Step 0 (plan phase). Copy, fill, and delete these comments.
     Statuses: draft → approved | auto-approved → executing → complete. -->

# Booley Setup Plan — <project name>

- **Status:** draft
- **Date:** <DD MMM YYYY>
- **Mode:** interactive | unattended
- **Repo:** <path or URL, commit at plan time>

## 1. Feasibility

| Flow | Verdict | Provisioning | Why |
| --- | --- | --- | --- |
| sim | Green/Yellow/Red | image \| host-provisioned \| — | <one line> |
| lint | | | |
| synth | | | |
| fpga | | | |

<!-- Determinant evidence: one short bullet per determinant that mattered
     (HDL language incl. any VHDL twin, EDA tools in repo, TB style/sentinel
     wording, toplevel port shape — interface vs packed-struct, compiled
     artifacts/toolchains, repo shape, scale, encrypted/PDK, licenses,
     and Tech Cell Replacement's Flow-supplied physical-library family,
     Liberty/LEF inputs, reachable inventory, and Target coverage).
     Cite file paths. Skip determinants with nothing to say. -->

- **<determinant>:** <finding> (<evidence path>)

## 2. Decision sheet

| # | Decision | What it decides | Internal key | Value | Resolution | Confidence | Evidence / why | Open question |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Which jobs should Booley run for you (simulate, lint, synthesize, FPGA build)? | Select the Booley Flows and a build Target for each job so only intended work runs. | Flows: enabled + Target per flow |  |  |  |  |  |
| 2 | Where do Booley's build descriptions (`.core` files) live, and what are the build configurations called? | Choose who owns each build description and its Target names; placement follows the git-history choice. | `.core` ownership/placement strategy & target names (must agree with row 16) |  |  |  |  |  |
| 3 | Which module is the top of the design? Does it need a flat-port wrapper? | Select the entry module for each job and adapt interface ports when the simulator needs flat signals. | Toplevel(s); flat-port wrapper? |  |  |  |  |  |
| 4 | Are your testbenches SystemVerilog, cocotb (Python), or both? | Choose how tests run and how their results are read; this controls the build and test layout. | Testbench flavor (sv/cocotb/mixed) |  |  |  |  |  |
| 5 | Which log lines mean pass, fail, timeout, or bad input? | Recognize test outcomes without false passes; failure wins ties, and cocotb uses its result file. | Pass/fail/timeout/input-error sentinels (fail wins ties) |  |  |  |  |  |
| 6 | Which tests exist, and which quick one proves the setup works? | Register the test list and a provisional smoke test, then measure the fastest useful check. | Test list + smoke test (provisional until timed) |  |  |  |  |  |
| 7 | Which container image holds your EDA tools? | Select the Sandbox image containing the EDA tools and dependencies needed to build and run the design. | Sandbox image |  |  |  |  |  |
| 8 | Which data files or prebuilt artifacts do tests need? | Make vectors and firmware available reproducibly so missing inputs cannot masquerade as a design failure. | Data files / built artifacts |  |  |  |  |  |
| 9 | Which third-party code should Booley leave untouched? | Use vendored-core quarantine to keep upstream cores read-only while integrating your own design. | Vendored-core quarantine |  |  |  |  |  |
| 10 | Where do your timing constraints (SDC/XDC) come from? | Reuse an upstream or user-supplied constraint file; a missing file blocks the affected build. | Constraints (SDC/XDC): upstream path or user-supplied file; never agent-authored (a missing file blocks the Target) |  |  |  |  |  |
| 10a | How should memories be handled in synthesis? | Classify every reachable memory and its implementation, replacement seam, timing behavior, and evidence before synthesis. | Memory implementation: every synthesis-reachable candidate, evidence, disposition, replacement seam, timing shape, and confidence |  |  |  |  |  |
| 11 | Do you also want a code-style lint? | Reuse the repo’s own style rules when available; otherwise keep optional style checking off. | Style lint opt-in |  |  |  |  |  |
| 12 | Should a quick build-without-running check (Elaboration Check) run? | Choose whether to sweep standalone modules as well as compiling the normal simulation build. | Elaboration Check / standalone need |  |  |  |  |  |
| 13 | How long can jobs run, and how much memory does the heaviest synthesis need? | Set time limits and plan measurements over the synthesis matrix so large jobs have enough memory. | Timeouts, heaviest synth calibration Target, & memory reservation |  |  |  |  |  |
| 14 | Which licensed commercial EDA tools may Booley use, and with whose license approval? | Record the approved installation, License Profile, and Project Grant so licensed EDA use has explicit authority. | Commercial EDA provisioning and grant |  |  |  |  |  |
| 15 | Do you want an `AGENTS.md` guide for AI agents working on this repo? | Decide whether to create or merge agent guidance and which documented project gotchas it should retain. | AGENTS.md (wanted? merge fate; gotchas) |  |  |  |  |  |
| 16 | Keep Booley out of your git history? | Hidden keeps `.booley_project/` untracked and enables stealth mode; open commits the config and uses native cores. | Git footprint: stealth `.booley_project/` or open native cores; ignore repository-native `.core` files? |  |  |  |  |  |
| 17 | Turn off any Specialist (AI reviewer, mutation tester) from the start? | Keep optional AI helpers available by default, or record the ones you deliberately disable. | Specialists explicitly disabled from the start (reviewer, …) |  |  |  |  |  |
| 18 | Cross-check Booley's results against your existing scripts (parity check)? | Compare results after setup only when both paths use the same EDA tool; propose sim for an identical runnable script, otherwise none. | Parity check (optional): native EDA-tool match per phase → tier, else `none` |  |  |  |  |  |
| 19 | Which AI provider and account does Booley use? | Preserve the provider and authentication selected during initialization; ask only for missing fields. | Agent backend: preserve the `[agent] provider` + `auth` selected by `booley init`; ask only for a legacy missing field |  |  |  |  |  |
| 20 | Scrub AI/tool names out of commit messages (stealth)? | Record the git-history answer’s commit-message scrub and hidden-core projection policy; existing explicit config wins. | `[stealth]`: history scrub plus hidden-core projection; required by row 16 when hidden cores are authored |  |  |  |  |  |
| 21 | Keep setup's scratch evidence, or clean it up? | Preserve durable configuration and reports; minimal removes only current-run scratch, while diagnostic keeps raw evidence. | Setup artifact retention: `minimal` (recommended) or `diagnostic` |  |  |  |  |  |
| 22 | Keep or clear the build cache (flow cache) after setup? | Preserve reusable build products by default; explicit eviction of setup-touched cache costs a rebuild. | Flow-cache disposition: `preserve` (recommended) or `evict-setup-touched` |  |  |  |  |  |
| 23 | Swap generic cells for your foundry library's cells (Tech Cell Replacement)? | Record one shared mapping and evidence for all synthesis builds; not applicable when synthesis is disabled. | Tech Cell Replacement: one Project-wide mapping shared by enabled synthesis Targets; `evidence-forced: not applicable` when synthesis is disabled |  |  |  |  |  |

<!-- Repo-specific rows: continue numbering from 24 (git submodules, generator
     steps, env-var-parameterized TBs, scope exclusions such as a VHDL twin, …).
     The standard list is the floor, not the ceiling. Each added row also needs
     a plain label, an explanation, and an internal key.
     Resolution column: `evidence-forced` (the repo determines it — no star, no
     question), `pre-set` (already hand-set on disk; kept verbatim, not starred),
     `user-confirmed`, `inferred`, or `review` (user judgment, or a
     low-confidence inference — starred for the user to audit).
     Confidence column: high/medium/low for `inferred` rows; `—` otherwise.
     Rows 16/17/19/20 are never evidence-forced. Rows 17 and 18 are
     defaults-block rows, not grill rows; 16+20 are asked as one question.
     When filling §2, split the canonical table into three sub-tables with the
     same columns and global numbering: ### Decisions you made
     (user-confirmed, and unattended review rows), ### Defaults accepted
     (defaults-block rows, inferred), and
     ### Settled by your repo or existing config (evidence-forced, pre-set).
     A row covering several
     independent items (row 1's four flows) resolves per item or splits. -->

### Tech Cell Replacement

<!-- Synthesis-disabled Projects resolve row 23 as evidence-forced: not
     applicable and omit this subsection. Otherwise, a cell-name list alone is
     not a decision or validation record. Keep one authoritative mapping here;
     Targets reference its sources and record only their covered subset. -->

#### Flow/library and authoritative location

- **Synthesis Flow:** <selected Flow and evidence of its physical-library family>
- **Liberty input:** <path and verification evidence>
- **LEF input (physical mode):** <path and verification evidence, or N/A>
- **Authoritative Project-owned replacement location:** <one tracked Project
  path, or one `.booley_project/` path for a hidden Project>

#### Project inventory

| Finding | Category | Defining file | Hierarchy/dependency core | Governing define/parameter | Applicable Targets | Active or repository-only | Remainder / notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| <finding> | documented technology-integration seam \| direct library-cell instantiation \| behavioral primitive intended for inference or replacement \| existing synthesis-time binding or post-inference mapping \| other library-dependent cell use requiring review | | | | | | |

#### Per-Target coverage matrix

| Synthesis Target | Hierarchy/dependency-core provenance | Mapping entries used | Reached sources/includes/generated inputs | Unhandled discovered findings | Coverage evidence |
| --- | --- | --- | --- | --- | --- |
| <Target> | <top and embedded cores> | <entry IDs> | <paths> | <none or entry IDs> | <check/result> |

#### Replacement table and semantic decisions

| Entry | RTL intent | Library cell | Mechanism | Authoritative source location | Frontend definition | Semantic evidence | Required validation layers |
| --- | --- | --- | --- | --- | --- | --- | --- |
| <ID> | <intent> | <cell> | preserve direct instantiation \| technology-integration seam \| adapter module \| library-cell model/declaration \| existing post-inference mapping under migration | <path> | <exactly one compatible source> | <docs/RTL/model/Liberty/LEF or focused probe> | semantic; frontend; mapped-netlist; physical-link; CDC/synchronizer (when applicable) |

#### Approved Project-owned inputs

- <adapter module, hook, cell model/declaration, fileset, or other approved input>

#### Incomplete/Yellow Targets and open questions

- <Target, missing mechanism or unresolved decision, and why it is Yellow>

#### Execution-time checks

- [ ] Semantic behavior: <polarity, enable/scan, reset, transparency/stage,
      output sense, and power-pin checks for each applicable entry>
- [ ] Frontend: <elaborate every enabled synthesis Target and prove one
      compatible definition with no competing module definition>
- [ ] Mapped netlist: <expected physical cell types/counts, disappearance or
      accounting of replaced forms, compared with the coverage matrix>
- [ ] Physical link: <for physical Targets, OpenROAD resolves every master
      from the Flow-supplied LEF/Liberty inputs>
- [ ] CDC/synchronizer (when applicable): <stage count, reset, preservation or
      `dont_touch`, timing exceptions, and characterized metastability support>

### Execution-time checks

<!-- Verifications that need the Sandbox and therefore run during Steps 2–4.
     A failed check that contradicts a decision triggers the stop-and-ask
     deviation rule. -->

- [ ] <check — e.g. `fusesoc --cores-root <dir> run --setup --work-root "$(mktemp -d)" --target <target> <vlnv>`
      (raw fusesoc: `--cores-root` before `run`, no `<vlnv>#<target>` form)>
- [ ] <check — e.g. compile one firmware file with the project's exact `-march` flags>
- [ ] <check — e.g. the packed-struct toplevel passes the synthesis RTL frontend (sv2v)>
- [ ] <check — e.g. time each smoke candidate and re-pin row 6 to the measured fastest>
- [ ] <check — e.g. compare every enabled synthesis Target's mapped-netlist
      cell types/counts with the per-Target coverage matrix>
- [ ] <check — e.g. for physical Targets, link the mapped netlist through
      OpenROAD and record every Flow-supplied LEF/Liberty master resolution>

## 3. Approval & deviations

- **Approval:** <pending | approved by user DD MMM YYYY | auto-approved (unattended)>

### Deviation log

<!-- Appended by execution steps. Minor deviations: one line each. A
     plan-invalidating contradiction stops execution instead — it lands here
     only together with the user's new decision. -->

| Step | What contradicted the plan | How it was settled |
| --- | --- | --- |
| | | |

### Execution ledger

<!-- One resumable state machine for Steps 1–7 and cleanup. Missing ledger
     metadata on a legacy plan deliberately means inventory-only cleanup. -->

- **Run ID:** <unset>
- **Scratch root:** <unset>
- **Manifest path:** <unset>
- **Step 1 status:** pending
- **Step 2 status:** pending
- **Step 3 status:** pending
- **Step 4 status:** pending
- **Step 5 status:** pending
- **Step 6 status:** pending
- **Step 7 status:** pending
- **Cleanup preview digest:** <unset>
- **Recovery journal:** idle
- **Final disposition:** <unset>
