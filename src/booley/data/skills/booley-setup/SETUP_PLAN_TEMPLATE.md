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

| # | Decision | Value | Resolution | Confidence | Evidence / why | Open question |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Flows: enabled + Target per flow | | | | | |
| 2 | `.core` ownership/placement strategy & target names (must agree with row 16) | | | | | |
| 3 | Toplevel(s); flat-port wrapper? | | | | | |
| 4 | Testbench flavor (sv/cocotb/mixed) | | | | | |
| 5 | Pass/fail/timeout/input-error sentinels (fail wins ties) | | | | | |
| 6 | Test list + smoke test (provisional until timed) | | | | | |
| 7 | Sandbox image | | | | | |
| 8 | Data files / built artifacts | | | | | |
| 9 | Vendored-core quarantine | | | | | |
| 10 | Constraints (SDC/XDC) | | | | | |
| 11 | Style lint opt-in | | | | | |
| 12 | Elaboration Check / standalone need | | | | | |
| 13 | Timeouts, heaviest synth calibration Target, & memory reservation | | | | | |
| 14 | Commercial EDA provisioning and grant | | | | | |
| 15 | AGENTS.md (wanted? merge fate; gotchas) | | | | | |
| 16 | Git footprint: stealth `.booley_project/` or open native cores; ignore repository-native `.core` files? | | | | | |
| 17 | Specialists explicitly disabled from the start (reviewer, …) | | | | | |
| 18 | Parity check (optional): native EDA-tool match per phase → tier, else `none` | | | | | |
| 19 | Agent backend: preserve the `[agent] provider` + `auth` selected by `booley init`; ask only for a legacy missing field | | | | | |
| 20 | `[stealth]`: history scrub plus hidden-core projection; required by row 16 when hidden cores are authored | | | | | |
| 21 | `[feedback] mode`: `ask` (default, public issue) / `email` (private, to the maintainer) / `file-only` / `off` — always ask | | | | | |
| 22 | Tech Cell Replacement: one Project-wide mapping shared by enabled synthesis Targets; `evidence-forced: not applicable` when synthesis is disabled | | | | | |

<!-- Repo-specific rows: continue numbering from 23 (git submodules, generator
     steps, env-var-parameterized TBs, scope exclusions such as a VHDL twin, …).
     The standard list is the floor, not the ceiling.
     Resolution column: `evidence-forced` (the repo determines it — no star, no
     question), `pre-set` (already hand-set on disk; kept verbatim, not starred),
     `user-confirmed`, `inferred`, or `review` (user judgment, or a
     low-confidence inference — starred for the user to audit).
     Confidence column: high/medium/low for `inferred` rows; `—` otherwise.
     Rows 16/17/19/20/21 are never evidence-forced. A row covering several
     independent items (row 1's four flows) resolves per item or splits. -->

### Tech Cell Replacement

<!-- Synthesis-disabled Projects resolve row 22 as evidence-forced: not
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
| <ID> | <intent> | <cell> | preserve direct instantiation \| technology-integration seam \| adapter module \| library-cell model/declaration \| existing post-inference mapping under migration | <path> | <exactly one compatible source> | <docs/RTL/model/Liberty/LEF or focused probe> | semantic; frontend; mapped-netlist; physical-link |

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
