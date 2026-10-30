# Step 4 — Doctor (final audit)

> Part of the `booley-setup` skill. Run in order, or invoke this step alone with `booley-setup 4`. This step is a plain `booley doctor` in a container terminal (doctor is context-aware — it checks whichever side it runs on, but setup runs it in the devcontainer).

Use this as the last Booley setup skill. The goal is a fully clean Doctor:
zero failures and zero active (unwaived) warnings in every required invocation.
An exit code of 0 is necessary but not sufficient because warnings do not change
Doctor's exit code.

Doctor reports six statuses, each marked by one glyph at the start of its row.
Only `✗` FAIL sets a non-zero exit code. `!` WARN names a real, actionable
failure mode: fix it or justify it with a project-local waiver before setup is
complete. `~` WAIVED is that same warning matched by a reviewed waiver; it stays
visible but is not active. `·` marks both a NOTE, an observation about a healthy
setup with nothing to fix (design scale, for instance), and a SKIP, a check
that did not apply here. Hints sit under their row, led by `→`. The run ends
with a table of the active FAIL and WARN rows, then a count for each status.

For `.core` findings, keep modernization scope separate from structural
correctness. Configured/selected Targets and every Target in a hidden adapter
under `.booley/cores/` are active setup scope. Modernization-only issues
in unselected native Targets are advisory NOTES: do not edit or waive those
Targets merely to clean setup. Parse/schema failures, VLNV collisions, unsafe
path/confinement findings, and failures in a selected Target or its dependency
closure stay active regardless of footprint. If Doctor emits a modernization
WARN for an unselected native Target, record it as a Booley defect; do not paper
over that severity bug with a project waiver.

## Workflow

1. Run `booley doctor` from the repository root.
   When delegating this command to Codex non-interactively, use
   `codex exec --approve-for-me -- <prompt>` so a safe diagnostic command does
   not stall waiting for an approval UI. Do not combine `--approve-for-me`
   with an explicit `--sandbox` flag; the Codex CLI treats those modes as
   mutually exclusive.
2. Read the output **unabridged** — never pipe doctor through `tail`/`head`:
   the closing tables sit at the end, where `head` eats them, and a `·` SKIP
   next to a `✓` PASS can be the whole story (an enabled Flow whose Target can
   never resolve reads as PASS+SKIP, i.e. it will never actually run).
3. Fix every `✗` FAIL and `!` WARN, using its `→` hint when present,
   and re-run `booley doctor` until it exits 0 **and reports 0 warnings**. If a
   warning describes a deliberate project constraint that cannot or should not
   be changed, review and record a waiver as described below; do not merely
   ignore it.
4. During iteration, exercise an affected Flow and Target directly when a live
   check is needed; reserve the full deep matrix for the final gate.
5. **Settle the git footprint** per the plan's decision row 16 — see below.
   This can create or change `doctor-waivers.toml`, so it precedes the final
   evidence runs.
6. Run plain Doctor once on the **host** (`booley doctor` from the Project Root there). Each side checks what only it can see — host-side
   Docker/network/image checks never run in the container. Expect a few
   Sandbox-specific SKIPs on each side; a *FAIL* that exists on one side only
   is real. The host-session reminder is a PASS: Project Setup planning expects a
   Claude Code or Codex session on the host, while Booley Flows and MCP tools live only in the Sandbox. It needs
   no waiver. Resolve or waive other host-only warnings before continuing.
7. Run plain `booley doctor` **once more** in the container after the footprint
   and host findings are settled. Host-side waiver changes must land before this
   point.
8. Run `booley doctor --deep` once in the container. This is the final deep
   evidence over the exact configuration and waiver file being delivered.
   Confirm the deep smoke and self-test lines actually **executed** — a SKIP on
   the line you were counting on (e.g. selftests skipped for a missing Sandbox)
   is not a pass. `--deep` resolves every Target and runs the smokes, so
   **minutes to tens of minutes is normal and it signals nothing on
   completion**: start it detached and poll it per SKILL.md → "Waiting on long
   runs" rather than standing by. If it finds a problem, diagnose and iterate
   with the affected Flow and Target directly, settle the files, repeat the
   applicable plain checks, and rerun deep Doctor only as the final evidence;
   a failed attempt does not count as the one successful final run.

### `--deep` lines specific to setup completeness

- **Simulation**: one full Simulation smoke is the authoritative elaboration
  plus runtime check. Doctor does not run a redundant elab-only smoke.
- **FPGA**: plain Doctor runs the marked FPGA Targets through the real setup and
  source-inspection path and probes Vivado. Deep Doctor does not launch a full
  implementation; each marked Target gets an explicit SKIP with the exact
  `booley flow fpga --target ...` command to run manually.
- **fail-path self-test**: each verification Flow WARNs
  `fail-path unvalidated` until its conventional project-owned bad fixture
  exists. Doctor infers the good case from the first marked Doctor Target and
  adds two lines — `good` must pass, `bad` must be *graded a failure*. For lint,
  author a `lint_selftest_bad` Target in its own `.core`, mark it
  `flow_options.booley.doctor_selftest: true`, and use an undeclared **RHS**
  reference (an undeclared LHS is only an implicit-net warning). The marker
  keeps the bad Target out of public listings and ordinary Flow selection while
  Doctor retains access. For simulation, mirror deliberately faulty staged RTL,
  firmware, or vectors under `.booley/selftest/sim/bad-overlay/`; Doctor runs the
  same smoke Test normally and with that overlay. The overlay must compile and
  then fail behaviorally; a timeout or crash is insufficient. A stale overlay
  that fails to build WARNs: refresh it to match the current design. Do not add
  a Project self-test mapping. A `bad` that FALSE-PASSES or returns
  an infra error is a hard FAIL: fix the Flow or fixture.

### Heaviest synthesis and memory calibration

When synthesis is enabled, the deep synthesis lines are not lightweight smokes:
Doctor runs every Target whose `flow_options.booley.doctor` contains `synth`.
Mark the complete supported synthesis matrix; do not finish setup while an
intended matrix member is unmarked.

The run must reach a terminal, completed PPA report. After it passes, Doctor
records each synthesis boundary's process-tree peak RSS, retains the largest,
and adds 15% rounded-up headroom to the HEAVY
job reservation. Re-run plain Doctor and use its memory-invariant arithmetic to
settle both `[jobs].heavy_memory` and `[sandbox].memory`, then recreate the
Sandbox if the container limit changed and repeat the heaviest synthesis.

- `termination = "oom"`: increase the container limit and HEAVY reservation
  when the host has capacity, recreate the Sandbox, and rerun. If the
  host cannot provide the required memory, stop and report setup blocked; do
  not waive a target that the project claims to support.
- `termination = "resource_killed"`: rc137/SIGKILL was observed without a
  corroborating cgroup OOM counter. Inspect host/container events before calling
  it OOM; do not blindly retry unchanged inputs.
- `termination = "timeout"`: inspect the last completed stage and live progress.
  For Simulation, raise `[flows.sim].build_timeout_ms` for credible compiler
  progress and `timeout_ms` for credible simulator or standalone-sweep
  progress. For other Flows, raise `timeout_ms` only when the Flow is still
  making credible progress. A
  frontend/optimization explosion is an RTL/recipe scalability defect, not a
  memory-limit calibration.
- Partial area, cell, or latch counts are diagnostic only. They never satisfy
  the calibration and must not be copied into the final PPA table.

## Fixing Failures

- Prefer Booley's own fix hint when present, for example `booley init` for
  stale MCP registration or missing project scaffolding.
- For config parse/schema failures, edit `.booley/booley.toml`, the
  `.core` design-description, or `.booley/tests.toml` minimally and
  re-run doctor.
- For Flow dry-run or deep-check failures, inspect the report directory named
  by doctor, especially run.log, reports, stdout, and stderr. Fix the config
  or `.core` before changing RTL or waivers.
- For MCP failures, check both project `.mcp.json` and user-scoped client
  config as reported by doctor. Re-run `booley init` when doctor recommends it.
- For container/image failures, verify the container runtime is running,
  inspect the active `[sandbox].image`, rebuild the project image if
  configured, and re-run doctor.
- Never edit board files directly while fixing setup; use Booley CLIs for
  board operations.
- A dirty git working tree is a NOTE. Settle the intended setup files before the
  final run, but do not manufacture a waiver for ordinary in-progress edits.

## Using Doctor waivers

Waivers live in `.booley/doctor-waivers.toml`, next to `booley.toml`.
They are project configuration and follow the same git-footprint decision as
the rest of `.booley/`. Never use them for a Doctor bug, a fixable
setup defect, a transient machine problem, or simply to make the output green.
Fix those. A waiver is for a warning whose risk is understood and deliberately
accepted by this project.

Every warning ends with a dimmed, stable identity such as
`sim.trace-unavailable:sim_fast`. Copy the check ID and, when shown, the exact
subject into one `[[waiver]]` entry:

```toml
version = 1

[[waiver]]
check = "sim.trace-unavailable"
subject = "sim_fast"
reason = "This target uses an upstream C++ harness with no trace switch."
expires = 2026-11-01
```

- `reason` is mandatory and must explain why accepting the risk is correct,
  not restate the warning.
- Set exactly one of `expires = YYYY-MM-DD` or `permanent = true`. Prefer an
  expiry for environmental and upstream constraints. Reserve permanent waivers
  for stable project policy.
- Include `subject` whenever Doctor prints one. Omitting it waives every subject
  produced by that check and therefore needs evidence that the same reason
  applies to all of them.
- Waivers match exact structured identities, never message text. They cannot
  suppress failures or notes. A malformed waiver file is itself a Doctor FAIL.
- A match prints a `~` row with a `→ reason:` hint and is counted separately. An expired
  entry is ignored and noted; the warning becomes active again. Re-review and
  edit or replace that exact entry if the risk is still accepted, or delete it
  if the risk vanished—never append a duplicate `check`/`subject` pair. Run
  `booley doctor --verbose` to find active waiver entries that do not match
  anything, then remove them.

After adding or changing a waiver, run the warning's original command again and
read the output. The expected result is the exact finding changing from a `!` row
to a `~` row, never disappearing. Log it in the setup plan's deviation record
only when it changes an approved decision; always include every final waiver in
the user report.

## Settle the git footprint

Setup is not finished until `.booley/` has a decided home in git.
Leaving it merely untracked-and-unmentioned is wrong in both modes: the config,
hooks, Goalsets, and authored cores under it are real work, and nothing is
versioning them. The plan's row 16 says which of the two outcomes applies. Do
this once, at the end, when the config has stopped moving.

Row 16 decides the git footprint. If it places authored cores under
`.booley/cores/`, row 20 must enable stealth so Booley projects those
cores into the repository root. An open footprint uses tracked native cores.

**Hybrid port/integration footprint.** When row 16 explicitly selected the
hybrid, follow the hidden procedure for `.booley/`, but commit only the
row's named repository-native integration artifacts. Keep the selected native
`.core` authoritative, and keep a real tracked root `AGENTS.md` whose content
matches `<booley_dir>/AGENTS.md`; current init/Doctor preserves that durable
file and generates only the optional `CLAUDE.md` link. Verify the tracked
allowlist exactly—hybrid does not authorize committing operational state.

**Hidden (default) — exclude it in the *parent repo's* `.git/info/exclude`.**

- The exclusion belongs in the RTL repo's `.git/info/exclude`, **not** its
  `.gitignore`. `.gitignore` is itself a tracked file: a `/.booley`
  line in it is committed, shows up in every diff and blame, and advertises
  Booley in exactly the history stealth mode exists to keep clean.
  `info/exclude` is local to the clone and travels nowhere.
- `booley init` already wrote `/.devcontainer`, `/.booley`, and
  `/.claude` there, so normally you are only **verifying**: `git check-ignore
  -v .booley` should name `.git/info/exclude`, and `git status
  --porcelain` must not list `.booley/` as untracked. Repair a missing
  entry by re-running `booley init` (idempotent), not by hand-editing.
  Worktree gotcha: git honors `info/exclude` only from `$GIT_COMMON_DIR`, so
  check there, not in `.git/worktrees/<id>/info/`.
- Hidden is not unversioned. Offer to give `.booley/` **its own git
  repo** (`git init` inside it, commit) — this keeps hidden
  authored cores under `.booley/cores/`, which are versioned nowhere
  otherwise. Hidden authored cores require `[stealth] enabled = true`, and
  `booley init` creates the inner repo and ignored root-level projections.
  Its `.gitignore` (written by `booley init`) already keeps the
  transient state — `tmp/`, `.runtime/`, `worktrees/`, Goal logs and locks —
  out. The inner repo is invisible to the parent, which sees only an excluded
  directory.
- Also confirm the RTL repo's own `.gitignore` picked up **no** Booley lines
  during setup, and that the root `AGENTS.md`/`CLAUDE.md` links are excluded
  the same way (Step 3; plain `booley doctor` repairs them).

**Open — commit `.booley/` to the RTL repo.**

- Before changing exclusions, inspect `.booley/.git`, if present, with
  `git -C .booley rev-list --all --count`, `git -C .booley remote -v`,
  and `git -C .booley status --short`. Existing commits, remotes, or
  intentional staged history require a human decision about preservation or
  migration; row 16 does not authorize deleting that history.
- When preserving an independently versioned Booley Directory, retain its Git
  metadata and the outer exclusion when present; leave an already absent
  exclusion absent. Read the current checkout's non-concise Doctor result:
  - For an actual `goals.history-ignored:independent-booley-history` **WARN**,
    record the accepted preservation policy using **Using Doctor waivers**:
    copy that exact check and subject into a waiver, explain the independent
    history and local-summary policy, and use `permanent = true` for that stable
    project decision. Commit the waiver in the independent Booley repository so
    future Paired Checkouts receive it and their inner checkout stays clean.
    Rerun the warning's original command and verify the `!` row becomes a `~` row.
  - An unexcluded authenticated Paired Checkout reports **PASS**:
    `Goal summaries remain local; Paired Checkout preserves independent Booley history`.
    That result needs no preservation waiver. A standalone current checkout's
    unchanged `Goal history is eligible for commits` **PASS** also needs no
    waiver; it describes that checkout, not a later Paired Checkout. If the
    preservation warning became a PASS, remove its now-unmatched waiver using
    the existing waiver cleanup and commit that change in independent history.
  Complete the existing final Doctor invocations with zero failures and active
  warnings. Continue below only after the human's chosen history migration into
  the RTL repository is complete.
- If the inner repository has no history, move its `.git` metadata to a uniquely
  named backup under the parent repo's `$(git rev-parse --git-common-dir)` (verify
  the destination is absent first) and report that recovery path. Preserve the
  backup.
- Once the files belong to the RTL repository, drop the `/.booley` line
  from `.git/info/exclude` (leave `/.devcontainer` and `/.claude` there unless the
  user asks otherwise — those are machine-specific runtime config).
- After migration, ensure the inner Git metadata is backed up outside these
  files: the RTL repository must track regular files, not an embedded-repository
  gitlink. Then `git add .booley`; inspect `git ls-files --stage
  .booley` and require regular-file entries rather than mode `160000`
  before committing. Do **not** move the exclusion into `.gitignore`; the point
  is to track the directory, not to re-hide it.
- Verify what landed with `git ls-files .booley | head`: the inner
  `.gitignore` should have kept `tmp/`, `.runtime/`, `worktrees/`, and the
  Goal logs/locks out. If transient state slipped in, unstage it and fix the
  inner `.gitignore` before committing — a committed `.runtime/` churns every
  run.
- Rerun Doctor. With Stealth off, removing the Booley exclusion requires no
  missing-exclusion waiver.
- `SETUP-PLAN.md` lives under `.booley/` and will be committed with
  it. That is fine — it is the setup audit trail. Say so in the report so
  nobody is surprised to find it in the tracked tree.

Either way, this decides only the fate of `.booley/` itself. The
minimal-footprint guardrail still holds: no other Booley-generated file joins
the RTL repo's tracked tree.

## Setup captures

The Doctor command owns its Flow output locations, so do not redirect them with
invented environment variables. Write only setup-authored command captures and
exit-code sidecars beneath the run's scratch root; nothing is registered. Flow
output, Job state, clean stamps, Goal Records, worktrees, and reusable
`.runtime/edalize` slots stay where Doctor writes them and are never removed by
Step 7.

## Final Report

Setup is not complete while any doctor invocation exits nonzero **or reports an
active warning**. Report to the
user in the **onboarding voice** (SKILL.md): a newcomer does not know what a
`!` warning, a waiver, or a `--deep` selftest is, so say in plain English what
passed, which risks were explicitly accepted, and what they mean for using the
project.
Once all three final invocations are green:

- Report the final pass/fail/warn/waived/note/skip counts separately for the
  final container plain, container deep, and host plain commands, plus the files
  changed.
- Report every waiver with its check ID, subject (if any), reason, and expiry or
  permanent status. There must be no warning merely "left for later."
- Set `status: complete` in `SETUP-PLAN.md`, and include the plan's §3
  deviation log plus (unattended mode) any `review`-flagged decision rows the
  user should audit.
