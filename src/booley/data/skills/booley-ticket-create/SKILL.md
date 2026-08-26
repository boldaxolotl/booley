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

### 2c: Grilling & full draft (detailed mode only)

1. Read `grilling.md` and run the dependency-aware grilling session: map the design as
   a decision tree, ask the entire currently unblocked frontier in each round, recommend
   an answer for every question, and defer decisions whose prerequisites remain open.
   Investigate codebase facts instead of asking for them, and keep the depth proportional.
2. Use the grilling rounds to settle material ticket fields and acceptance criteria as well
   as the design. Apply clear §B/§D defaults without creating a separate review step; the
   user can edit them in the complete ticket.
3. When the frontier is empty, synthesize the **detailed implementation plan** and complete
   ticket (skeleton and placement: `TICKET_TEMPLATE.md`), then continue directly to the
   draft gate in 2f. The complete ticket is the one post-grill review artifact and the
   payload the developer plans against.

Detailed mode skips 2d and 2e because those decisions were folded into grilling.

### 2d: Complete lightweight fields (lightweight mode only)

Ask only about **missing** (`???`) and **uncertain** fields, with all questions in one
message. **All fields required** — no silent defaults. Iterate until complete. Continue
directly to 2e without showing an intermediate ticket preview.

### 2e: Criteria selection (lightweight mode only)

Build defaults from ticket type + configs (from `@config` segments in sim entries) — catalog and per-type defaults in §D. Present as a structured menu:

> **Criteria** (defaults ✓, edit as needed):
>
> **Mandatory:**
> 1. ✓ `lint_clean`: [configs] *(feature/refactor)*
> 2. ✓ `sim_pass`: [tb @ config @ test @ cur -> exp, ...]
> 3. ✓ `review_rtl_bugs` *(feature/refactor; corrective review)*
> 4. ✓ `review_tb_quality` *(feature/verification; corrective review)*
>
> **Optional:**
> 5. ☐ `review_rtl_spec` *(feature tickets carrying a detailed spec)*
> 6. ☐ `synthesis_ok` *(datapath/timing-critical)*
> 7. ☐ `mutation_score`
>
> Toggle by number, edit thresholds, or add custom. Enter to accept.

If the user deselects every mandatory criterion, confirm explicitly before accepting (§D requires ≥1).

### 2f: Confirm and write

**MANDATORY DRAFT GATE.** Show the complete proposed ticket (frontmatter + body,
excluding seal fields). Ask: *"Create this draft and author its Target contract?
(yes / edit / cancel)"*

For detailed mode, this is the first review artifact shown after grilling. If the user
chooses `edit`, revise the complete ticket and show it again; keep the review at this gate
rather than falling back to summaries or partial previews. Step 4's combined ticket +
Target diff remains the separate seal gate because it reviews newly authored contract data.

**Never write the ticket file until the user explicitly approves** — including agent-invoked creation from other skills.

## Step 3: Agent Mode

1. All fields required — return an error listing the missing fields (no interactive questions)
2. Same dependency scan (§A) and validation (§C) as human mode
3. Inference (§B) only for fields marked `"infer"`; missing without `"infer"` → error
4. No explicit `criteria` dict → auto-build every §D default for the ticket type. Always pass `--criteria` to `create-file`
5. **No grilling** — the calling agent must provide all details upfront
6. Approval gate (2f) applies unless the caller passed `--no-confirm`

## Step 4: Author and Seal

Follow §C: create the approved draft, open its contract worktrees, author every
needed Target/control file there, validate, and show one combined ticket + Target
diff. Get explicit approval to seal, then seal and enqueue. Target authoring is part
of ticket creation, never deferred to the developer. The combined ticket + Target
diff is the separate seal gate.

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

Runtime and seal fields are *not* inferred: `target_contract` and `base_sha` are
stamped from Git by `contract-seal`, `created` by `enqueue`, `feature_branch` by
`init`, and `integration_base` by `enqueue --integration-base`.

## §C. CLI Workflow

`create-file` generates the frontmatter (including `criteria`) from its flags — do **not**
hand-write the YAML or any SHA. `contract-seal` stamps `target_contract` and
`base_sha`; `on_success` and `created` are stamped later by `enqueue`.

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

# E4. Open isolated outer and paired project-data authoring worktrees.
python -m booley.ticket_board contract-open "$SLUG"

# E5. Create or edit all required .core files, constraints, Target-selection
#     configuration, and build hooks in the returned worktree(s). RTL/TB sources
#     may remain absent only when Scope declares each path [new]; relative-QoR
#     Targets must already be fully executable.

# E6. Validate — a path, not a slug. Fix and re-run until clean.
python -m booley.ticket_board validate-ticket \
  .booley_project/tickets/board/drafts/$SLUG.md [--check-git]

# E7. Show the complete ticket plus outer/paired Target diffs and ask for
#     explicit seal approval. Then commit and publish the immutable seal.
python -m booley.ticket_board contract-seal "$SLUG"

# E8. Enqueue (refuses an absent/stale seal; stamps on_success + created)
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
| Per-test Cycle Count | `cycle_count: [{target: sim_coremark, test: coremark, cycle_count_max: 100000}]` |
| Parameterized | `synthesis_ok: {targets: [<target>], cell_count_max: 500}` |
| Parameterized | `fpga_impl_ok: {targets: [<target>], lut_count_max: 100000}` |
| Scalar | Use the bare review key for the corrective default; it expands to `<key>_clean`. Spell `<key>_done` only when the user explicitly wants an advisory review whose findings are reported but do not belong to this ticket's correction loop |

### Defaults by Ticket Type

| Criterion | Feature | Bugfix | Refactor | Verification |
|-----------|:-------:|:------:|:--------:|:------------:|
| `lint_clean` | **M** | — | **M** | — |
| `sim_pass` | **M** | **M** | **M** | **M** |
| `review_rtl_bugs` | **M** | — | **M** | — |
| `review_tb_quality` | **M** | — | — | **M** |

**M** = mandatory, — = not included.

Opt-in suggestions: `review_rtl_spec` for feature tickets carrying a detailed spec (it
checks the RTL against the ticket body, or the external spec the `spec:` field points at);
`coverage_*` and `mutation_score` for verification; `synthesis_ok` for
datapath/timing-critical feature/refactor work; `fpga_impl_ok` for FPGA QoR/timing checks.

When enumerating the project's lint Targets for the `lint_clean` config list, include a
project-authored Verible style-lint Target (a `.core` lint Target with
`flow_options: {tool: verible}`, typically `lint_style`) alongside the Verilator one — a
project that authored it presumably wants it enforced. `lint_clean_<target>`
means "clean under whatever linter that Target names"; there is no separate style criterion.

`cycle_count` is a list of mappings, never a `sim_pass` numeric parameter. Every item must
name one `target` and registered `test`, plus at least one threshold. Absolute
`cycle_count_max` / `cycle_count_min` use the current run. Relative percentage and `_cycles`
forms automatically compare the same Target/test at `base_sha`; consult
`booley cheat --criteria` for the complete signed-bound vocabulary.

`synthesis_ok` / `fpga_impl_ok` take threshold **params** in four flavours per metric:
absolute `_max` / `_min`, plus baseline-relative `_increase_at_most` / `_reduce_at_least`
(compared against the ticket's `base_sha`). Common ones: `cell_count_max`, `fmax_mhz_min`,
`cell_count_reduce_at_least` (ASIC); `lut_count_max`, `ff_count_max`, `fmax_mhz_min`
(FPGA). Don't hardcode a subset here — for the full per-metric matrix and which pairs are
mutually exclusive, run `booley cheat --criteria` (the "threshold flavours" table, also in
`docs/USAGE.md`); it is generated from the validator, so it never drifts.

### Rules

- ≥1 mandatory criterion required
- Every default review criterion uses its bare key, which expands to corrective
  `_clean`, and runs after code-changing work. Use explicit `_done` only for
  user-requested advisory review. Every `_clean` waiver includes a justification
  and is shown to the user regardless of severity.
- Custom criterion types allowed beyond the catalog
- A criterion may name a new Target only when ticket creation authors it in the
  contract worktree before sealing. Do not put contract controls in developer
  Scope merely to permit later edits: every `.core`, tests/Target-selection
  configuration, selected constraint, generator, and build hook is immutable
  after sealing.
- A future non-relative Target may reference missing RTL/TB paths only when every
  path is declared Scope `[new]`. A relative-QoR Target must resolve and dry-run
  completely at the sealed baseline.
- If a blocked ticket needs a different Target recipe, use `revise-contract`; it
  archives the old identity, discards execution evidence, and restarts authoring.
