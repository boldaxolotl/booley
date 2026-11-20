# Step 3 — AGENTS.md

> Part of the `booley-setup` skill. Run in order, or invoke this step alone with `booley-setup 3`. This step runs inside the devcontainer — edit files in the worktree and run `booley doctor` in a container terminal.

Create a minimal Project-level `AGENTS.md` for Developer Agents doing RTL work. The
file is high leverage: do not auto-write, pad, or invent facts.

**Template:** `../AGENTS_TEMPLATE.md`

## Rules

**Location and linking**

- The canonical file lives in the Booley Directory as `<booley_dir>/AGENTS.md`
  (usually `.booley/AGENTS.md`), so it is versioned with the rest of
  the project config. Normally, do not write a real file at the Project Root.
- Exception: when plan row 16 selects the hybrid port/integration footprint,
  write and track a content-identical regular root `AGENTS.md` so a fresh clone
  remains self-describing while `.booley/` stays local. `booley init` preserves
  this matching tracked copy and refuses to overwrite a stale one.
- The Project Root carries only generated `AGENTS.md` and `CLAUDE.md` links to
  the canonical file. `booley init` creates or repairs them after the canonical
  file is written; Doctor only reports `guidance.links-unhealthy` and changes
  no file.

**Content**

- Keep the output concise: only concrete, durable facts that change RTL work,
  using Booley's canonical terms where relevant.
- Follow the template's two sections exactly. Keep its worktree bullet verbatim;
  omit it only when worktree guidance is irrelevant.
- Keep the template's `booley_status` bullet and its two Sandbox bullets
  ("Inside the Sandbox…" and "If `booley_status` is absent…") verbatim. The
  `booley_status` bullet establishes the editor as a user-visible surface for
  files and diffs. The Project Root's guidance links resolve on the host too, where
  none of the Booley Flows are registered; the Sandbox bullets explain why.
  Doctor warns if the "Inside the Sandbox" bullet goes missing.
- When `booley.toml` selects `booley-sandbox-riscv`, keep the template's RISC-V
  reference-docs bullet verbatim so Developer Agents can find the image's offline manuals.
  Remove that conditional bullet for every other sandbox image.
- When `booley.toml` has `[stealth].enabled = true`, keep the template's stealth
  mode bullet verbatim so Developer Agents treat `.booley/` as a separate Git
  repository and do not expose its changes in the main repository.
  Remove that conditional bullet when stealth mode is disabled.
- Keep the template's Metric-Driven Development bullet verbatim. The pointer is
  present when the file already contains `booley cheat --metrics`; add the
  bullet only when the file does not contain that text, so a merge or a re-run
  never adds it twice and a reworded pointer is not duplicated. This applies to
  every row 15 fate that leaves a canonical file (create, merge, overwrite, and
  leave; under leave, the pointer is the one line this step adds). With no
  canonical `AGENTS.md` (row 15 = not wanted), add nothing. The hybrid tracked
  root copy gets the same bytes, as for every Step 3 write.
- Do not read RTL, testbench, or test contents by default. Stable docs and
  config are enough for this task.
- Bad content — omit all of it: repo maps and large directory listings;
  `booley init` or setup instructions; raw simulator command guesses; Developer
  Agent lifecycle
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

- Existing canonical `<booley_dir>/AGENTS.md`, if present (and any alternate
  `AGENTS.md` at the Project Root, to migrate its content into the canonical file)
- `README.md`
- `GLOSSARY-MAP.md`, if present, followed by the relevant context glossaries
- `docs/GLOSSARY.md`, if present when no context map exists
- `.booley/booley.toml`
- the `.core` design-description and `.booley/tests.toml`
- Top-level directory names, only when docs/config mention source/spec roots

If a file is missing, continue. Do not treat missing docs as an error.

### 2. Draft From Template

Fill in `../AGENTS_TEMPLATE.md` with concrete, durable facts. Remove
placeholders and bullets that cannot be filled confidently.

Do not infer Project gotchas from README, source files, or config. Only include
gotchas that came from an existing instruction file or from the user's answers
recorded in the plan (row 15).

### 3. Review Artifact

If the canonical `<booley_dir>/AGENTS.md` already exists:

1. Apply the fate the plan's row 15 decided (merge / overwrite / leave),
   summarizing what is kept, removed, or added.
2. Show the proposed replacement or merge patch in the report.
3. If the plan did not decide the fate, that is a plan gap — deviation rule:
   stop and ask before merging or overwriting.

If it does not exist: show the complete proposed file in the report. No
approval question in either case beyond the plan-gap stop above.

### 4. Write and Report

1. Write the canonical file to `<booley_dir>/AGENTS.md` (resolve
   `<booley_dir>` from `.booley/`; do not hardcode). For the explicit
   hybrid footprint, also write the same bytes to tracked root `AGENTS.md`.
2. Run `booley init` (on the host, or in place in a hand-written Sandbox); it
   creates or repairs the root `AGENTS.md` and `CLAUDE.md` links and adds them
   to `.git/info/exclude`. Doctor then reports whether the links are current.
   If `booley init` cannot run, create the two root symlinks to the canonical
   file by hand and add `/AGENTS.md` and `/CLAUDE.md` to the RTL repo's `.git/info/exclude`.
   If the delegated environment makes the Project Root read-only, do not loop on
   failed `ln` commands: preserve the canonical file, record the two links as
   pending, and have the host run `booley init --seed` after
   the delegated step. A sandbox permission boundary is not evidence that the
   canonical guidance is invalid.

Report whether the canonical file was created, merged, overwritten, or left
unchanged; whether root `AGENTS.md` is a generated link or a durable tracked
copy; which other root links were ensured; and any unresolved facts omitted.
