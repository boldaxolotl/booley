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
     artifacts/toolchains, repo shape, scale, encrypted/PDK, licenses).
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

<!-- Repo-specific rows: continue numbering from 22 (git submodules, generator
     steps, env-var-parameterized TBs, scope exclusions such as a VHDL twin, …).
     The standard list is the floor, not the ceiling.
     Resolution column: `evidence-forced` (the repo determines it — no star, no
     question), `pre-set` (already hand-set on disk; kept verbatim, not starred),
     `user-confirmed`, `inferred`, or `review` (user judgment, or a
     low-confidence inference — starred for the user to audit).
     Confidence column: high/medium/low for `inferred` rows; `—` otherwise.
     Rows 16/17/19/20/21 are never evidence-forced. A row covering several
     independent items (row 1's four flows) resolves per item or splits. -->

### Execution-time checks

<!-- Verifications that need the Session Runtime and therefore run during Steps 2–4.
     A failed check that contradicts a decision triggers the stop-and-ask
     deviation rule. -->

- [ ] <check — e.g. `fusesoc --cores-root <dir> run --setup --work-root "$(mktemp -d)" --target <target> <vlnv>`
      (raw fusesoc: `--cores-root` before `run`, no `<vlnv>#<target>` form)>
- [ ] <check — e.g. compile one firmware file with the project's exact `-march` flags>
- [ ] <check — e.g. the packed-struct toplevel passes the synthesis RTL frontend (sv2v)>
- [ ] <check — e.g. time each smoke candidate and re-pin row 6 to the measured fastest>

## 3. Approval & deviations

- **Approval:** <pending | approved by user DD MMM YYYY | auto-approved (unattended)>

### Deviation log

<!-- Appended by execution steps. Minor deviations: one line each. A
     plan-invalidating contradiction stops execution instead — it lands here
     only together with the user's new decision. -->

| Step | What contradicted the plan | How it was settled |
| --- | --- | --- |
