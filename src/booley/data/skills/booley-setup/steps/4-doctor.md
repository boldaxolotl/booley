# Step 4 — Doctor (final audit)

> Part of the `booley-setup` skill. Run in order, or invoke this step alone with `booley-setup 4`. This step is a plain `booley doctor` in a container terminal (doctor is context-aware — it checks whichever side it runs on, but setup runs it in the devcontainer).

Use this as the last Booley setup skill. The goal is a fully clean Doctor:
zero failures and zero active (unwaived) warnings in every required invocation.
An exit code of 0 is necessary but not sufficient because warnings do not change
Doctor's exit code.

Doctor reports six statuses. Only `[XX] FAIL` sets a non-zero exit code. `[!!]
WARN` names a real, actionable failure mode: fix it or justify it with a
project-local waiver before setup is complete. `[ii] WAIVED` is that same
warning matched by a reviewed waiver; it stays visible but is not active.
`[ii] NOTE` is an observation about a healthy setup with nothing to fix (design
scale, for instance). `[--] SKIP` means the check did not apply here.

For `.core` findings, keep modernization scope separate from structural
correctness. Configured/selected Targets and every Target in a hidden adapter
under `.booley_project/cores/` are active setup scope. Modernization-only issues
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
   the layout puts FAIL rows where truncation eats them, and a `[--] SKIP`
   next to a PASS can be the whole story (an enabled Flow whose Target can
   never resolve reads as PASS+SKIP, i.e. it will never actually run).
3. Fix every `[XX] FAIL` and `[!!] WARN`, using its `fix:` hint when present,
   and re-run `booley doctor` until it exits 0 **and reports 0 warnings**. If a
   warning describes a deliberate project constraint that cannot or should not
   be changed, review and record a waiver as described below; do not merely
   ignore it.
4. Run `booley doctor --deep` — never skip it because plain doctor passed.
   Fix all deep-check failures and warnings, then re-run until it exits 0 with
   0 warnings. Confirm the deep
   smoke and self-test lines actually **executed** — a SKIP on the line you
   were counting on (e.g. selftests skipped for a missing runtime) is not a
   pass. `--deep` resolves every Target and runs the smokes, so **minutes to
   tens of minutes is normal and it signals nothing on completion**: start it
   detached and poll it per SKILL.md → "Waiting on long runs" rather than
   standing by.
5. **Settle the git footprint** per the plan's decision row 16 — see below.
   This can create or change `doctor-waivers.toml`, so it precedes the final
   evidence runs.
6. Run plain `booley doctor` **once more** in the container after the footprint
   is settled. Deep and non-deep checks overlap only partially, and deep-side
   fixes have regressed plain checks before.
7. Run `booley doctor --deep` once more in the container. This is the final
   deep evidence over the exact configuration and waiver file being delivered.
8. Run plain Doctor once on the **host** as well (`booley doctor` from the repo
   root there). Each side checks what only it can see — host-side
   Docker/network/image checks never run in the container. Expect a few
   runtime-specific SKIPs on each side; a *FAIL* that exists on one side only
   is real. Resolve or waive host-only warnings too. If this changes the waiver
   file, repeat the final container plain and deep runs.

### Two `--deep` lines specific to setup completeness

- **`elab`**: enabled → it is deep-smoked and must pass; opted out with
  `enabled = false` → a recorded SKIP. A project that
  cannot support a standalone elaboration check (non-FuseSoC, `lint`/`sim` cover it)
  should opt out rather than expose a broken Flow. Note that an exposed
  `elab` with no `[flows.elab].default_target` fails the *plain* `booley
  doctor` too — that Flow can never run, so it is caught before `--deep`.
- **fail-path self-test**: each verification Flow WARNs
  `fail-path unvalidated` until its conventional project-owned bad fixture
  exists. Doctor infers the good case from the configured default Target and
  adds two lines — `good` must pass, `bad` must be *graded a failure*. For lint,
  author a `lint_selftest_bad` Target using an undeclared **RHS** reference (an
  undeclared LHS is only an implicit-net warning). For simulation, mirror the
  broken staged firmware or vectors under
  `.booley_project/selftest/sim/bad-overlay/`; Doctor runs the same smoke test
  normally and with that overlay. Do not add `[flows.<flow>.selftest]`; it is
  retired. A `bad` that FALSE-PASSES or returns an infra error is a hard FAIL:
  fix the Flow or fixture.

### Heaviest synthesis and memory calibration

When synthesis is enabled, the deep synthesis line is not a lightweight smoke:
it must run the row-13 `[flows.synth].calibration_target`, meaning the heaviest
supported configuration selected during planning. For a one-Target project the
sole Target is implicit. For a matrix, do not finish setup while Doctor reports
`synth.calibration-target-unset`.

The run must reach a terminal, completed PPA report. After it passes, Doctor
records the synthesis boundary's process-tree peak RSS and adds 15% rounded-up
headroom to the HEAVY
job reservation. Re-run plain Doctor and use its memory-invariant arithmetic to
settle both `[jobs].heavy_memory` and `[sandbox].memory`, then recreate the
Session Runtime if the container limit changed and repeat the heaviest synthesis.

- `termination = "oom"`: increase the container limit and HEAVY reservation
  when the host has capacity, recreate the Session Runtime, and rerun. If the
  host cannot provide the required memory, stop and report setup blocked; do
  not waive a target that the project claims to support.
- `termination = "resource_killed"`: rc137/SIGKILL was observed without a
  corroborating cgroup OOM counter. Inspect host/container events before calling
  it OOM; do not blindly retry unchanged inputs.
- `termination = "timeout"`: inspect the last completed stage and live progress.
  Raise `timeout_ms` only when the flow is still making credible progress. A
  frontend/optimization explosion is an RTL/recipe scalability defect, not a
  memory-limit calibration.
- Partial area, cell, or latch counts are diagnostic only. They never satisfy
  the calibration and must not be copied into the final PPA table.

## Fixing Failures

- Prefer Booley's own fix hint when present, for example `booley init` for
  stale MCP registration or missing project scaffolding.
- For config parse/schema failures, edit `.booley_project/booley.toml`, the
  `.core` design-description, or `.booley_project/tests.toml` minimally and
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

Waivers live in `.booley_project/doctor-waivers.toml`, next to `booley.toml`.
They are project configuration and follow the same git-footprint decision as
the rest of `.booley_project/`. Never use them for a Doctor bug, a fixable
setup defect, a transient machine problem, or simply to make the output green.
Fix those. A waiver is for a warning whose risk is understood and deliberately
accepted by this project.

Every warning ends with a stable identity such as
`[sim.trace-unavailable:sim_fast]`. Copy the check ID and, when shown, the exact
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
- A match prints `WAIVED` plus the reason and is counted separately. An expired
  entry is ignored and noted; the warning becomes active again. Re-review and
  edit or replace that exact entry if the risk is still accepted, or delete it
  if the risk vanished—never append a duplicate `check`/`subject` pair. Run
  `booley doctor --verbose` to find active waiver entries that no longer match
  anything, then remove them.

After adding or changing a waiver, run the warning's original command again and
read the output. The expected result is the exact finding changing from `WARN`
to `WAIVED`, never disappearing. Log it in the setup plan's deviation record
only when it changes an approved decision; always include every final waiver in
the user report.

## Settle the git footprint

Setup is not finished until `.booley_project/` has a decided home in git.
Leaving it merely untracked-and-unmentioned is wrong in both modes: the config,
hooks, tickets, and authored cores under it are real work, and nothing is
versioning them. The plan's row 16 says which of the two outcomes applies. Do
this once, at the end, when the config has stopped moving.

Row 16 decides the git footprint. If it places authored cores under
`.booley_project/cores/`, row 20 must enable stealth so Booley projects those
cores into the repository root. An open footprint uses tracked native cores.

**Hybrid port/integration footprint.** When row 16 explicitly selected the
hybrid, follow the hidden procedure for `.booley_project/`, but commit only the
row's named repository-native integration artifacts. Keep the selected native
`.core` authoritative, and keep a real tracked root `AGENTS.md` whose content
matches `<project_dir>/AGENTS.md`; current init/Doctor preserves that durable
file and generates only the optional `CLAUDE.md` link. Verify the tracked
allowlist exactly—hybrid does not authorize committing operational state.

**Hidden (default) — exclude it in the *parent repo's* `.git/info/exclude`.**

- The exclusion belongs in the RTL repo's `.git/info/exclude`, **not** its
  `.gitignore`. `.gitignore` is itself a tracked file: a `/.booley_project`
  line in it is committed, shows up in every diff and blame, and advertises
  Booley in exactly the history stealth mode exists to keep clean.
  `info/exclude` is local to the clone and travels nowhere.
- `booley init` already wrote `/.devcontainer`, `/.booley_project`, and
  `/.claude` there, so normally you are only **verifying**: `git check-ignore
  -v .booley_project` should name `.git/info/exclude`, and `git status
  --porcelain` must not list `.booley_project/` as untracked. Repair a missing
  entry by re-running `booley init` (idempotent), not by hand-editing.
  Worktree gotcha: git honors `info/exclude` only from `$GIT_COMMON_DIR`, so
  check there, not in `.git/worktrees/<id>/info/`.
- Hidden is not unversioned. Offer to give `.booley_project/` **its own git
  repo** (`git init` inside it, commit) — this keeps hidden
  authored cores under `.booley_project/cores/`, which are versioned nowhere
  otherwise. Hidden authored cores require `[stealth] enabled = true`, and
  `booley init` creates the inner repo and ignored root-level projections.
  Its `.gitignore` (written by `booley init`) already keeps the
  transient state — `tmp/`, `.runtime/`, `worktrees/`, ticket logs and locks —
  out. The inner repo is invisible to the parent, which sees only an excluded
  directory.
- Also confirm the RTL repo's own `.gitignore` picked up **no** Booley lines
  during setup, and that the root `AGENTS.md`/`CLAUDE.md` links are excluded
  the same way (Step 3; plain `booley doctor` repairs them).

**Open — commit `.booley_project/` to the RTL repo.**

- Drop the `/.booley_project` line from `.git/info/exclude` (leave
  `/.devcontainer` and `/.claude` there unless the user asks otherwise — those
  are machine-specific runtime config, not project config).
- Before `git add`, check whether `.booley_project/.git` exists. `booley init`
  normally creates that inner repository for hidden mode, but an open footprint
  must track the files themselves, not an embedded-repository gitlink. Inspect
  it with `git -C .booley_project rev-list --all --count`, `git -C
  .booley_project remote -v`, and `git -C .booley_project status --short`. If it
  has commits, a remote, or intentional staged history, stop and ask how that
  history should be preserved or migrated; row 16 does not authorize deleting
  it. If it has no history, move its `.git` metadata to a uniquely named backup
  under the parent repo's `$(git rev-parse --git-common-dir)` (verify the
  destination is absent first) and report that recovery path. Do not delete it.
- Then `git add .booley_project && git commit`. Do **not** move the exclusion into
  `.gitignore`; the point is to track the directory, not to re-hide it.
- Verify what landed with `git ls-files .booley_project | head`: the inner
  `.gitignore` should have kept `tmp/`, `.runtime/`, `worktrees/`, and the
  ticket logs/locks out. If transient state slipped in, unstage it and fix the
  inner `.gitignore` before committing — a committed `.runtime/` churns every
  run.
- Expect one **WARN**, not a FAIL, on the next doctor run: `git info/exclude
  missing Booley entry: .booley_project`. That check assumes the hidden
  footprint. For an approved open footprint, append this deliberate exception
  to `doctor-waivers.toml` and confirm it appears as `WAIVED`. If the file does
  not exist, create it using the complete example below. If it exists, keep its
  single `version = 1` header and append only the `[[waiver]]` table—never add a
  second version key or overwrite existing entries.

  ```toml
  version = 1

  [[waiver]]
  check = "project.git-excludes-missing"
  subject = ".booley_project"
  reason = "The approved open footprint tracks .booley_project in the parent repository."
  permanent = true
  ```

  Do not "fix" it by re-excluding the directory you just committed.
- `SETUP-PLAN.md` lives under `.booley_project/` and will be committed with
  it. That is fine — it is the setup audit trail. Say so in the report so
  nobody is surprised to find it in the tracked tree.

Either way, this decides only the fate of `.booley_project/` itself. The
minimal-footprint guardrail still holds: no other Booley-generated file joins
the RTL repo's tracked tree.

## Final Report

Setup is not complete while any doctor invocation exits nonzero **or reports an
active warning**. Report to the
user in the **onboarding voice** (SKILL.md): a newcomer does not know what a
`[!!] WARN`, a waiver, or a `--deep` selftest is, so say in plain English what
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
