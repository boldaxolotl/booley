---
name: booley-ticket-create
description: Create a well-formed RTL development ticket from fuzzy requirements through interactive refinement
---

# Create Ticket

Both companion files sit in this skill's directory, alongside this file.

**Schema:** `TICKET_TEMPLATE.md` — single source of truth for frontmatter fields and
per-type body structure. Read it before writing a ticket.

**Grilling guide:** `grilling.md` — read **only** in detailed-plan mode (Step 2c).
Lightweight tickets never need it.

**Usage:**
```
booley-ticket-create <fuzzy description>          # human mode (default)
booley-ticket-create --agent <structured input>   # agent mode — no interaction
```

## Step 1: Parse Input

`--agent` in `$ARGUMENTS` → **agent mode** (Step 3). Otherwise → **human mode** (Step 2).

## Step 2: Interactive Refinement

### 2a: Ticket depth (ask first)

Before anything else, ask the user which kind of ticket they want:

> **How much detail should this ticket carry?**
> 1. **Lightweight** — for simple, well-understood changes. I infer the fields, confirm the few unknowns, and write a concise body. No grilling.
> 2. **Detailed plan** — for complex or risky work. I grill you on the design, then synthesize a full implementation plan and write it into the ticket body.

- **Lightweight** → skip 2c entirely (don't read `grilling.md`); the body carries only `## Description`.
- **Detailed plan** → run 2c; the body also carries `## Implementation Plan`.

(Agent mode never asks — see Step 3.)

### 2b: Dependency scan + field inference

Run the dependency scan (§A) and infer fields (§B). Two judgement calls §B can't make for you:

- **scope**: don't list generated/compiled artifacts. For bugfix tickets using the top-level TB, suggest including firmware source files. Before accepting `scope: ["*"]`, push back once: *"Are you sure you can't narrow it down to at least a directory (e.g., `rtl/*.sv`)?"*
- **bugfix reproducibility**: if the bug isn't visible in current tests, confirm the feature+bugfix split (§B) with the user before drafting two tickets.

### 2c: Grilling & plan (detailed mode only)

1. Read `grilling.md` and run the dependency-aware grilling session: map the design as
   a decision tree, ask the entire currently unblocked frontier in each round, recommend
   an answer for every question, and defer decisions whose prerequisites remain open.
   Investigate codebase facts instead of asking for them, and keep the depth proportional.
2. When the frontier is empty, summarize the resulting shared understanding and ask the
   user to confirm it. Do not advance to ticket drafting while a decision branch remains
   silently assumed or before the user confirms the summary.
3. After confirmation, synthesize the **detailed implementation plan** and write it into
   the ticket body (skeleton and placement: `TICKET_TEMPLATE.md`). This is the payload the
   developer plans against.

### 2d: Draft and iterate

Show inferred values, mark **missing** (`???`) and **uncertain** fields. Ask all questions in one message. **All fields required** — no silent defaults. Iterate until complete, validating each round (§C, E4).

### 2e: Criteria selection

Build defaults from ticket type + configs (from `@config` segments in sim entries) — catalog and per-type defaults in §D. Present as a structured menu:

> **Criteria** (defaults ✓, edit as needed):
>
> **Mandatory:**
> 1. ✓ `lint_clean`: [configs] *(feature/refactor)*
> 2. ✓ `sim_pass`: [tb @ config @ test @ cur -> exp, ...]
> 3. ✓ `review_rtl_bugs_done` *(feature/refactor; terminal advisory review)*
> 4. ✓ `review_tb_quality_done` *(feature/verification; terminal advisory review)*
>
> **Optional:**
> 5. ☐ `review_rtl_spec_done` *(feature tickets carrying a detailed spec)*
> 6. ☐ `synthesis_ok` *(datapath/timing-critical)*
> 7. ☐ `mutation_score`
>
> Toggle by number, edit thresholds, or add custom. Enter to accept.

If the user deselects every mandatory criterion, confirm explicitly before accepting (§D requires ≥1).

### 2f: Confirm and write

**MANDATORY GATE.** Show complete final ticket (frontmatter + body). Ask: *"Write this ticket? (yes / edit / cancel)"*

**Never write the ticket file until the user explicitly approves** — including agent-invoked creation from other skills.

## Step 3: Agent Mode

1. All fields required — return an error listing the missing fields (no interactive questions)
2. Same dependency scan (§A) and validation (§C) as human mode
3. Inference (§B) only for fields marked `"infer"`; missing without `"infer"` → error
4. No explicit `criteria` dict → auto-build every §D default for the ticket type. Always pass `--criteria` to `create-file`
5. **No grilling** — the calling agent must provide all details upfront
6. Approval gate (2f) applies unless the caller passed `--no-confirm`

## Step 4: Write Ticket

Follow §C: slug → body temp file → `create-file --criteria` → validate → enqueue.

## Step 5: Report

- **Human**: print the enqueued path (`board/queue/`, or `board/waiting/` when it declares unmet dependencies — Step 4's `enqueue` already moved it there), then suggest `/booley-run-and-fix`
- **Agent**: return path

---

# Reference

## §A. Dependency Scan

```bash
CLASSIFIED=$(python -m booley.ticket_board classify)
```

Check non-done tickets for scope overlap or interface dependencies.

## §B. Field Inference

Field definitions, defaults, and types live in `TICKET_TEMPLATE.md`. This table covers
only how to *infer* a value from the conversation and the repo.

| Field | Strategy |
|-------|----------|
| `summary` | Concise one-liner from grilling + initial input; becomes the slug |
| `type` | "fix/bug" → `bugfix`, "refactor/clean" → `refactor`, "testbench/coverage/verification/TB" → `verification`, else → `feature` |
| `branch` | `git branch --show-current` |
| `scope` | From grilling results. `[new]` for new files. Unknown bugfix → `["*"]` (prefer narrow) |
| `spec` | Include when an arch spec exists near scope |
| `on_success` | Default `{destination: review, merge: true, cleanup: true, triage_report: true}`. Set `triage_report: false` to skip the rich HTML explanation. Benchmark: `{destination: done, merge: false, cleanup: true}` |
| `dependencies` | From scan (§A) + grilling; user confirms |
| `priority` | Default `medium` |
| `criteria` | §D defaults + grilling; user confirms/edits. **feature** → from grilling. **refactor** → all `pass -> pass`. **bugfix** → the failing entry `fail -> pass`, rest `pass -> pass`. **verification** → TB-only work |

**Bugfix, not yet reproducible?** Recommend a split: feature ticket (create the failing test) + bugfix ticket (fix the RTL, depends on the feature).

Runtime fields are *not* inferred — the ticket-board commands stamp them: `feature_branch` by `init`,
`created` by `enqueue`, `base_sha` by `create-file` (auto-resolved from branch if
omitted), `integration_base` by `enqueue --integration-base` (a relationship field,
typically supplied by `/tickets-from-spec`).

## §C. CLI Workflow

`create-file` generates the frontmatter (including `criteria`) from its flags — do **not**
hand-write the YAML. `on_success` and `created` are stamped later by `enqueue`.

```bash
# E1. Generate slug
SLUG=$(python -m booley.ticket_board slug "$SUMMARY")

# E2. Write the body to a temp file — per-type `## Description` from TICKET_TEMPLATE.md,
#     plus `## Implementation Plan` for detailed-plan tickets
BODY=$(mktemp)

# E3. Create the draft in board/drafts/$SLUG.md (frontmatter built from these flags)
python -m booley.ticket_board create-file "$SLUG" \
  --summary "$SUMMARY" --type "$TYPE" --branch "$BRANCH" \
  --scope rtl/foo.sv tb/foo_tb.sv \      # nargs="*" — space-separated; omit for []
  [--spec "$SPEC"] [--dependencies dep-slug-a dep-slug-b] [--priority "$PRIORITY"] \
  --criteria "$CRITERIA_JSON" \          # JSON: {"mandatory":{...},"optional":{...}}
  --body-file "$BODY"

# E4. Validate — a path, not a slug. Fix and re-run until clean
python -m booley.ticket_board validate-ticket \
  .booley_project/tickets/board/drafts/$SLUG.md [--check-git]

# E5. Enqueue (stamps on_success + created)
python -m booley.ticket_board enqueue $SLUG \
  [--destination done] [--no-merge] [--cleanup] [--integration-base "$INT_BASE"]
```

## §D. Criteria Catalog

Machine-readable acceptance conditions. Immutable after creation. Configs derived from `sim_pass` entries' `@config` segments.

> **Single source of truth — `booley cheat --criteria`.** The authoritative list of criterion
> names, phases, "set by" Flow or Specialist, the `targets:` scoping key, and the valid params for
> the parameterized criteria (`synthesis_ok`/`fpga_impl_ok`) is rendered live by
> `booley cheat --criteria` from `criteria.toml` + the MCP tool registry (that flag prints the
> criteria section alone — the rest of the sheet is not needed here). **Consult it** before
> authoring criteria — the shapes below are structural illustration, not the catalog.
> (This is the same block embedded in USAGE.md; it is why the scoping key is
> `targets`, never `configs`.)

```yaml
criteria:
  mandatory:
    <type>: <value>
  optional:
    <type>: <value>
```

### Value Forms

| Form | Example |
|------|---------|
| Config list | `lint_clean: [<target_a>, <target_b>]` → per-Target expansion |
| Sim-style | `sim_pass: [tb@config@test@cur->exp]` |
| Parameterized | `synthesis_ok: {targets: [<target>], cell_count_max: 500}` |
| Parameterized | `fpga_impl_ok: {targets: [<target>], lut_count_max: 100000}` |
| Scalar | Spell review criteria as `<key>_done` for the default terminal advisory review (report findings, do not fix). Use `<key>_clean` only when the user requests every finding fixed or explicitly waived with user-visible justification |

### Defaults by Ticket Type

| Criterion | Feature | Bugfix | Refactor | Verification |
|-----------|:-------:|:------:|:--------:|:------------:|
| `lint_clean` | **M** | — | **M** | — |
| `sim_pass` | **M** | **M** | **M** | **M** |
| `review_rtl_bugs_done` | **M** | — | **M** | — |
| `review_tb_quality_done` | **M** | — | — | **M** |

**M** = mandatory, — = not included.

Opt-in suggestions: `review_rtl_spec_done` for feature tickets carrying a detailed spec (it
checks the RTL against the ticket body, or the external spec the `spec:` field points at);
`coverage_*` and `mutation_score` for verification; `synthesis_ok` for
datapath/timing-critical feature/refactor work; `fpga_impl_ok` for FPGA QoR/timing checks.

When enumerating the project's lint Targets for the `lint_clean` config list, include a
project-authored Verible style-lint Target (a `.core` lint Target with
`flow_options: {tool: verible}`, typically `lint_style`) alongside the Verilator one — a
project that authored it presumably wants it enforced. `lint_clean_<target>`
means "clean under whatever linter that Target names"; there is no separate style criterion.

`synthesis_ok` / `fpga_impl_ok` take threshold **params** in four flavours per metric:
absolute `_max` / `_min`, plus baseline-relative `_increase_at_most` / `_reduce_at_least`
(compared against the ticket's `base_sha`). Common ones: `cell_count_max`, `fmax_mhz_min`,
`cell_count_reduce_at_least` (ASIC); `lut_count_max`, `ff_count_max`, `fmax_mhz_min`
(FPGA). Don't hardcode a subset here — for the full per-metric matrix and which pairs are
mutually exclusive, run `booley cheat --criteria` (the "threshold flavours" table, also in
`docs/USAGE.md`); it is generated from the validator, so it never drifts.

### Rules

- ≥1 mandatory criterion required
- Every default review criterion uses the explicit `_done` suffix and runs after
  code-changing work. `_clean` is opt-in only; never infer it merely because a
  review is mandatory. Every `_clean` waiver must include a justification and is
  shown to the user regardless of severity.
- Custom criterion types allowed beyond the catalog
- A criterion may name a Target that this ticket will create. In that case,
  include the affected `.core` file in `scope` and state the Target creation
  explicitly in the description or implementation plan. Do not create or edit
  the Target while drafting the ticket; the ticket runner defers validation of
  an unknown Target until the developer has had a chance to author it.
