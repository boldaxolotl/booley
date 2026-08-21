---
name: booley-setup
description: Set up a Booley project end-to-end after `pip install` + `booley init` — plan first (feasibility + decision grill → SETUP-PLAN.md, Step 0), then execute gate-free (config, AGENTS.md, doctor audit — Steps 2–4). Phase-detects from the plan file; also runs a single step on request, or `new` for a from-scratch (`init --scaffold`) project with a lightweight grill. Post-gate Step 5 optionally cross-checks Booley's results against the repo's native build system; Step 6 always reports the run's findings and offers, once, to send the Booley-side ones upstream.
---

# Booley setup (plan → execute, Steps 0–4, + optional parity)

This skill owns Booley setup from **Step 0 (plan)** through **Step 4 (doctor)**,
plus an **optional post-gate Step 5 (parity)** for projects that want their
Booley results cross-checked against the repo's native build system.
The only parts that live outside it are the mechanical host prefix:
prerequisites and `pip install booley-rtl` in Booley's
[README](https://github.com/boldaxolotl/Booley#installation), then the first
`booley init` in `docs/SETUP.md` (which is what deploys this skill).
None of those steps involves a project decision.

The model is **plan-first**: Step 0 gathers feasibility evidence and every
decision the later steps need, refines them in a grilling session, and writes
them to `.booley_project/SETUP-PLAN.md` for approval. Execution then runs
**gate-free** — no step stops to ask about anything the plan already decided.
In unattended mode the plan file replaces the approval conversation entirely
(see `steps/0-plan.md`).

**Audience — humans and agents alike.** A human can read the `steps/` files and
work through them by hand. Or invoke the skill **from the host** and let the
agent drive: it plans, stops once for plan approval, and executes the rest
without interruption.

## The steps

| # | Step | Where | Always? | File |
| --- | --- | --- | --- | --- |
| 0 | **Plan** — feasibility + decision grill → `SETUP-PLAN.md`. | host | yes (lightweight in `new` mode) | `steps/0-plan.md` |
| 1 | **Environment** — planned image + `booley init` re-run. | host | only if the plan changes the sandbox image | (execution phase, below) |
| 2 | **Project config** — `.core`, `tests.toml`, `booley.toml`. | container; validate via `booley doctor` | yes | `steps/2-project-config.md` |
| 3 | **AGENTS.md** — Project-level guidance for RTL agents. | container; file edits only | yes | `steps/3-agents-md.md` |
| 4 | **Doctor** — final audit; resolve every failure and warning, then `--deep`. | container terminal (+ one host run) | yes | `steps/4-doctor.md` |
| 5 | **Parity** — diff Booley vs the repo's native flow, where EDA tools match. | container | no (only if plan row 18 ≠ `none`) | `steps/5-parity.md` |
| 6 | **Findings** — report, triage, optional bug report to Booley. | container (host to submit) | yes | `steps/6-findings.md` |

Step 4 is the gate: setup is not complete until plain `booley doctor` and
`booley doctor --deep` both exit 0 **and report zero active (unwaived)
warnings**. A deliberate project constraint may use a reviewed
`.booley_project/doctor-waivers.toml` entry following Step 4's rules; an ignored
warning may not. **Steps 5 and 6 are post-gate** — Step 5
validates the finished setup against the native build system, Step 6 reports on
the run; neither blocks completion (see `steps/5-parity.md`,
`steps/6-findings.md`).

## The plan file

`SETUP-PLAN.md` (template: `SETUP_PLAN_TEMPLATE.md`, written to
`.booley_project/`) is the skill's single state file; every section below
refers to its parts:

- **`status:`** — `draft` → `approved` | `auto-approved` → `executing` →
  `complete`. Drives phase detection.
- **§1 Feasibility** — the per-flow verdict table plus determinant evidence.
- **§2 Decision sheet** — numbered decision rows (row 7 is the sandbox image,
  row 16 the git footprint, row 20 the commit-message scrub, …), each with
  value, **resolution mode**, confidence, and evidence (resolution and
  confidence are separate columns — see `steps/0-plan.md`, "How a row
  resolves") — followed by the **execution-time checks** list: planned
  verifications that need the sandbox, run by Steps 2–4 as they are reached.
- **§3 Approval & deviations** — the approval record, and the deviation log
  that execution appends to.

## Invocation and phase detection

Read `$ARGUMENTS`, then `.booley_project/SETUP-PLAN.md` (if present) to find
where setup stands:

- **Empty `$ARGUMENTS`** → phase-detect from the plan's `status`:
  - **No plan file, or `draft`** → run **Step 0 (plan)** — `steps/0-plan.md`.
    Runs on the host; if you find yourself in-container, planning still works,
    but host authority, installation registration, and Project Grant evidence
    must be confirmed by the user instead.
  - **`approved` or `auto-approved`** → set `status: executing` and run the
    **execution phase** (below).
  - **`executing`** → resume execution at the first step not yet reported done
    in the plan's §3.
  - **`complete`** → nothing to do; tell the user and offer the step selector
    for re-runs.
- **`new`** (or `greenfield`) → **greenfield mode** for a from-scratch
  (`init --scaffold`) project — read `steps/new-greenfield.md` and follow it.
- **A step selector** (a number `0`–`6`, or a name like `plan`, `doctor`,
  `project-config`, `agents-md`, `parity`, `findings`) → run **only that step**,
  then stop. Use this
  to re-run or resume one step. If `SETUP-PLAN.md` exists, the step consumes
  it as usual; if not, ask the user for just the decisions that step needs —
  do not force a full plan for a one-step re-run.

## Host or container?

Steps 0–1 run on the **host** — the plan is written before the devcontainer
exists. Steps 2–4 run **inside the Session Runtime**: the per-folder
devcontainer entered via **Reopen in Container** in VS Code, or
`booley session up` headlessly. There, each step edits files in the
worktree and runs the `booley` CLI in a container terminal; the sandbox
toolchain ships in the container, so EDA-tool probes and smoke checks run directly
(`verilator --version`, or `booley flow <name> …` — runs any Booley Flow, passing
the rest of the line to it verbatim). `booley run`, `booley board`, and
`booley doctor` are container commands here.

The CLIs fail fast on the wrong side, each naming the fix: `booley init`
refuses in-container; the workflow CLI (`booley run`/`board`, `bwave`) refuses
on the host. To check explicitly: `/.dockerenv` exists only in the container.
`SETUP-PLAN.md` carries the full context across the boundary — a fresh
in-container session just re-invokes this skill and phase detection resumes
execution.

## Running a step

1. Read the step file under `steps/` (numbered `0-…` … `4-…`). (Step 1 has no
   `steps/` file — it is described in the execution phase below.)
2. **Delegate the step's work to a sub-agent** (Task MCP tool) whenever the step
   is more than a couple of commands — config authoring and the doctor
   fix-loop are both good candidates. Give the sub-agent the step file to
   follow **plus the approved `SETUP-PLAN.md`** — it holds every decision the
   step consumes. The rules a sub-agent must inherit are the **deviation
   rule**, the **onboarding voice**, and **"Waiting on long runs"** (all below);
   a stop-and-ask always surfaces through the main agent, never inside the
   sub-agent. Say the waiting rule out loud in the sub-agent's brief — a
   delegated Step 2 or 4 is exactly where an agent parks on "standing by" and
   waits for a notification that never comes.
3. Run the plan's §2 execution-time checks as the step reaches the artifacts
   they verify.
4. **Log findings as they happen** — see below. This is part of running a step,
   not something Step 6 reconstructs afterwards.

## Logging findings

Every step appends to one log as it goes:

```console
booley feedback add --title "…" --severity blocker|workaround|note \
  --exposed-by "the exact check that surfaced it" --step 4-doctor \
  --repro "booley doctor" --observed "…" --expected "…"
booley feedback win --title "a check that passed first try"   # keeps the sample honest
```

Two rules, and the second is the one that gets skipped:

- **Log at the moment of friction, not at the end.** A report written from memory
  after the gate is a plausible-sounding invention. Anything worth reporting was
  visible the instant it happened, with the command still in scrollback — that is
  when the `--repro` and `--observed` are free. Ten minutes later they are
  guesses.
- **Every sub-agent logs its own findings, into the same log.** A delegated Step 2
  or 4 is where most friction lives, and a sub-agent's context dies when it
  returns — so its brief must say: *run `booley feedback add` for anything that
  surprised you, before you report back.* The log is a file precisely so that a
  finding survives the agent that found it. Do not ask sub-agents to summarize
  findings in their return message and then re-log them yourself; that loses the
  detail and duplicates the entries.

Bucketing (whose problem is it) and the decision about sending anything to
Booley's maintainers both belong to Step 6. While a step is running, just log
what happened.

## Waiting on long runs

Setup's real runs are minutes to hours — a sim suite, `booley doctor --deep`, an
ASIC synth — and **Booley emits no completion notification**. Nothing will nudge
you when one ends. Parking on "standing by" is never a correct state: if you are
waiting, you are polling.

- **Start it detached.** Run the command in the background with stdout
  redirected to a file. A foreground call that outlives your own MCP tool timeout
  loses the output and reads as a hang.
- **Know the bound before you start.** A Booley Flow cannot outlive its
  `[flows.<flow>].timeout_ms` (`sim` applies it per test; `synth`
  defaults to 30 min). Budget that plus a minute of teardown — past it the run
  is wedged, and the answer is to investigate, not to wait longer.
- **Poll on a fixed cadence**: every ~30 s for the first few minutes, then every
  1–2 min. Each poll checks three things — the process is alive (`pgrep -f
  verilator_run`, `pgrep -f yosys`, …), the redirected stdout grew, and for sims
  the work dir's `run.log`, which carries a live tail stamped
  `Ns elapsed, N output line(s), last output Ns ago`. **`last output` is the
  hang-versus-slow signal**: a sim silent for minutes is stuck; a slow one keeps
  talking.
- A `run.log` still headed `(run in progress …)` is a progress tail, **not a
  verdict** — never score a run from it.
- **After any timeout or kill, hunt orphans**: `pgrep -f
  'verilator_run|yosys|V<toplevel>'`. A killed run can leave its supervisor and
  sim child burning a full core; kill them before starting the next run.
- Put the measured wall-clock times in the step's report — the timeout rows of
  the next plan are written from those numbers.

## The deviation rule

Execution consumes the plan; it never re-asks a decided row and never silently
overrules one. When reality contradicts the plan (a probe fails, an EDA tool is
missing from the image, a target doesn't resolve):

- **Plan-invalidating** (the decision or anything built on it must change):
  **stop and ask the user** — always, even mid-step. Unattended: halt, record
  the open question in the plan's §3, surface it in the final report; do not
  improvise a new plan.
- **Minor** (the decision stands, a detail shifts): apply the obvious fix and
  append one line to the §3 deviation log.

## The onboarding voice

Every step that surfaces something to the user — the plan and its grill
(Step 0), the config draft (Step 2), the doctor findings (Step 4) — is
**first-run onboarding: assume the reader is new to Booley.** Lead with a
concise plain-English explanation before the artifact, define each Booley term
the first time it appears instead of assuming fluency, and name the evidence
behind every decision. This is a communication rule, not a decision rule: it
changes how legibly a step explains itself, never *what* it does. Step 0's
grill leans hardest on it — a user who does not yet speak Booley still has to
make every call, so each question carries its recommended answer and the
plain-English reason it matters.

## Execution phase (after an approved plan)

1. **Step 1 — environment · host.** Only if the plan's row 7 calls for it:
   write `.booley_project/docker/Dockerfile` and/or set `[sandbox].image` in
   the placeholder `booley.toml`, then re-run `booley init` (idempotent) so it
   builds/pulls the planned image and re-seeds the devcontainer config. Skip
   entirely when the plan keeps the base image.
2. **Commercial EDA authority · host.** Only if row 14 selects
   host-provisioned EDA: perform Step 2's
   [host-authority bootstrap](steps/2-project-config.md#host-authority-bootstrap)
   before creating the licensed/mounted runtime. This writes the planned
   `[eda.<kind>]` request, registers the installation, adds the exact Project
   Grant, reseeds the issued specification, and runs host Doctor. Skip when
   every selected EDA tool is image-provisioned.
3. **Enter the Session Runtime** (Reopen in Container, or
   `booley session up --rebuild`).
4. **Steps 2 → 4 in order**, per the plan: finish config, AGENTS.md, then the doctor
   gate.
5. After the Step 4 gate is green, set `status: complete` and write the final
   report: what was built, the §3 deviation log, and (unattended) the
   `review`-flagged decisions the user should audit.
6. **Step 5 — parity · optional.** Only if the plan's row 18 selects an oracle
   (≠ `none`): run `steps/5-parity.md` after the gate to diff Booley against the
   repo's native flow where the EDA tools match. It produces
   `.booley_project/PARITY-REPORT.md` and a note in the final report; it never
   blocks completion and does not change `status`. Parity is the richest source
   of real Booley findings in the whole run — a number that differs from the
   native flow's is about as unambiguous as evidence gets — so log every
   discrepancy with `booley feedback add`, not just the ones you can explain.
7. **Step 6 — findings.** Always: `steps/6-findings.md` triages the log, writes
   `.booley_project/SETUP-REPORT.md`, and asks once whether to send the
   Booley-side findings upstream. Post-gate, non-blocking, and a decline is a
   normal outcome.

## Setup guardrails

Two rules that outrank any step's local convenience. A step may look like it
wants you to break these; it doesn't.

- **Stay out of the repo (minimal footprint).** All of Booley's operational
  state lives under `.booley_project/` — config, hooks, tickets, the project
  `docker/` image, logs, and `SETUP-PLAN.md`. Do **not** add or commit
  Booley-generated files into the target repo's tracked tree: no
  `README.booley.md` / integration notes, no logs, no generated artifacts
  explaining the onboarding. **This includes Step 6's own output** — the
  findings log and the one `SETUP-REPORT.md` live in `.booley_project/`; a
  report about the setup is still a Booley-generated file. A redacted
  `BOOLEY-FEEDBACK.md` exists only after an explicit `booley feedback export`.
  The only Booley-touched files that legitimately
  join the tracked tree are genuine **design/verification inputs the project
  owns**. For an **open** footprint, that includes a `.core`
  design-description: reuse/modernize the appropriate native core when one
  exists, and create a tracked core only when none exists. For a **hidden**
  stealth footprint, never edit native tracked cores; its core belongs under
  `.booley_project/cores/` and uses repository-root-relative filesets. Booley
  projects ignored root-level copies; do not create source symlinks. (The
  `booley_vcd_dump.sv` trace
  module is **not** one of them: the trace overlay supplies it from Booley's
  `refs/` at run time.) Notes for the next agent or the user go in
  `.booley_project/`, not the repo. Whether `.booley_project/` **itself** is
  tracked is the plan's row-16 call: **hidden** (default) keeps it out via the
  parent repo's `.git/info/exclude` — never `.gitignore`, which is tracked and
  would advertise Booley — and versions it in its own inner git repo; **open**
  commits it to the RTL repo. (Row 16 selects the footprint; row 20 records
  the required stealth framing and history policy.) Step 4 executes
  that row (mechanics in `steps/4-doctor.md`). Either way, nothing else
  Booley-generated joins the tracked tree.
  **One sanctioned exception:** the maintainer dogfood workflow
  (`booley-port-ip`) may explicitly instruct Step 6 to render the report to the
  *root* of its throwaway public-IP clone (`booley feedback report
  --user-report-path SETUP-REPORT.md`) and commit it — the port *is* the
  deliverable there. When the enclosing workflow gives that instruction, write
  the root report **instead of** an inner copy. A normal setup leaves the report
  in `.booley_project/` and never passes that flag. If you *find* a **tracked**
  `SETUP-REPORT.md`, it is the fingerprint of a port — an earlier one, or **the
  one running right now** (on a dogfood repo it is usually hours old, with
  sections still waiting on the step you are about to run). Either way: record
  it, read it as evidence, leave it in place unless the enclosing port workflow
  explicitly owns and rerenders it, and follow the prior-footprint branch in
  `steps/0-plan.md`. An **untracked**
  `.booley_project/SETUP-REPORT.md` is a different thing entirely — the previous
  setup run's own report, which Step 6 will simply overwrite.
- **Ship the toolchain, not the artifact.** When a design needs a software
  build step to produce a simulation input (CPU-core firmware, a compiled
  ROM/memory image, generated RTL), bake the required toolchain into the
  project sandbox image and build the artifact **on demand inside the
  sandbox** — never compile it once on the host and vendor the prebuilt binary
  into the repo. A frozen `firmware.hex` hides the real dependency and rots on
  the first source change. Step 0's worked example and Step 2's data-files
  guidance give the mechanics (project `Dockerfile` + `post-setup` hook for
  once-per-worktree steps; `[flows.sim].pre_run_commands` for per-test
  builds).

## Configuration reference

The steps author the config files; they do **not** re-explain every field.
When a step needs the meaning of a `booley.toml` section, a `.core`
fileset/target, `tests.toml`, sentinels, or an advanced setup, it links to
`CONFIG.md` in the project docs. Bundled templates live at this skill's root:
`../SETUP_PLAN_TEMPLATE.md`, `../BOOLEY_TEMPLATE.toml`,
`../TESTS_TEMPLATE.toml`, `../CORE_TEMPLATE.yaml`, `../AGENTS_TEMPLATE.md`
(paths relative to the step files in `steps/`).
