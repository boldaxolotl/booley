# Step 3 — AGENTS.md

> Part of the `booley-setup` skill. Run in order, or invoke this step alone with `booley-setup 3`. This step runs inside the devcontainer — edit files in the worktree and run `booley doctor` in a container terminal.

Create a minimal Project-level `AGENTS.md` for assistants doing RTL work. The
file is high leverage: do not auto-write, pad, or invent facts.

**Template:** `../AGENTS_TEMPLATE.md`

## Rules

**Location and linking**

- The canonical file lives in the project data dir as `<project_dir>/AGENTS.md`
  (usually `.booley_project/AGENTS.md`), so it is versioned with the rest of
  the project config. Normally, do not write a real file at the repo root.
- Exception: when plan row 16 selects the hybrid port/integration footprint,
  write and track a content-identical regular root `AGENTS.md` so a fresh clone
  remains self-describing while `.booley_project/` stays local. Init and Doctor
  preserve this matching tracked copy and refuse to overwrite a stale one.
- The RTL repo root carries only generated `AGENTS.md` and `CLAUDE.md` links to
  the canonical file. Plain `booley doctor` creates or repairs them after the
  canonical file is written.

**Content**

- Keep the output concise: only concrete, durable facts that change RTL work,
  using Booley's canonical terms where relevant.
- Follow the template's two required project/Booley sections exactly. The
  template's standardized third worktree section is also permitted; keep it
  verbatim when included and omit it only when worktree guidance is irrelevant.
- Keep the template's Interactive Mode / VS Code and Session Runtime scoping
  bullets verbatim. The first establishes the editor as a user-visible surface
  for files and diffs. The repo root's guidance links resolve on the host too,
  where none of the Booley Flows are registered; the second explains why. Doctor
  warns if the latter goes missing.
- When `booley.toml` selects `booley-sandbox-riscv`, keep the template's RISC-V
  reference-docs bullet verbatim so agents can find the image's offline manuals.
  Remove that conditional bullet for every other sandbox image.
- Do not read RTL, testbench, or test contents by default. Stable docs and
  config are enough for this task.
- Bad content — omit all of it: repo maps and large directory listings;
  `booley init` or setup instructions; raw simulator command guesses; Board
  operating instructions; instructions that tell the assistant to choose
  execution mode; generic verification commands sections; generic coding
  advice not grounded in this Project; duplicated README material; detailed
  Booley internals that do not guide day-to-day RTL work.

**Process**

- The plan's decision-sheet row 15 settled whether `AGENTS.md` is wanted, the
  fate of an existing canonical file (merge / overwrite / leave), and any
  user-supplied gotchas; content questions were collected there too (Step 0).
  During execution do not stall — fill what's confident, omit the rest, and
  note the omissions in the report.
- Writes are covered by the plan approval; no approval question here. The one
  exception is the plan-gap stop in workflow step 3 below.

## Workflow

### 1. Inspect Stable Inputs

- Existing canonical `<project_dir>/AGENTS.md`, if present (and any legacy
  `AGENTS.md` at the repo root, to migrate its content into the canonical file)
- `README.md`
- `docs/CONTEXT.md`, if present
- `.booley_project/booley.toml`
- the `.core` design-description and `.booley_project/tests.toml`
- Top-level directory names, only when docs/config mention source/spec roots

If a file is missing, continue. Do not treat missing docs as an error.

### 2. Draft From Template

Fill in `../AGENTS_TEMPLATE.md` with concrete, durable facts. Remove
placeholders and bullets that cannot be filled confidently.

Do not infer Project gotchas from README, source files, or config. Only include
gotchas that came from an existing instruction file or from the user's answers
recorded in the plan (row 15).

### 3. Review Artifact

If the canonical `<project_dir>/AGENTS.md` already exists:

1. Apply the fate the plan's row 15 decided (merge / overwrite / leave),
   summarizing what is kept, removed, or added.
2. Show the proposed replacement or merge patch in the report.
3. If the plan did not decide the fate, that is a plan gap — deviation rule:
   stop and ask before merging or overwriting.

If it does not exist: show the complete proposed file in the report. No
approval question in either case beyond the plan-gap stop above.

### 4. Write and Report

1. Write the canonical file to `<project_dir>/AGENTS.md` (resolve
   `<project_dir>` from `.booley_project/`; do not hardcode). For the explicit
   hybrid footprint, also write the same bytes to tracked root `AGENTS.md`.
2. Run plain `booley doctor`; it creates or repairs the root `AGENTS.md` and
   `CLAUDE.md` links and adds them to `.git/info/exclude`. If Doctor cannot run,
   create the two root symlinks to the canonical file by hand and add
   `/AGENTS.md` and `/CLAUDE.md` to the RTL repo's `.git/info/exclude`.
   If the delegated environment makes the repo root read-only, do not loop on
   failed `ln` commands: preserve the canonical file, record the two links as
   pending, and have the host run `booley init --seed` or plain Doctor after
   the delegated step. A sandbox permission boundary is not evidence that the
   canonical guidance is invalid.

Report whether the canonical file was created, merged, overwritten, or left
unchanged; whether root `AGENTS.md` is a generated link or a durable tracked
copy; which other root links were ensured; and any unresolved facts omitted.
