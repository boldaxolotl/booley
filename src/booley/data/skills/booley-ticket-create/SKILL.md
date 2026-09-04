---
name: booley-ticket-create
description: Create a well-formed RTL development ticket from fuzzy requirements through interactive refinement
---

# Create Ticket

All companion files sit in this skill's directory, alongside this file.

**Schema:** `TICKET_TEMPLATE.md` — single source of truth for frontmatter fields and
per-type body structure. Read it before writing a ticket.

**Grilling guide:** `grilling.md` — read **only** in detailed-plan mode (Step 2c).
Lightweight tickets never need it.

**Usage:**
```
booley-ticket-create <fuzzy description>          # human mode (default)
booley-ticket-create --agent <structured input>   # agent mode — no interaction
```

## Output Boundary

Ticket creation authors only the Ticket, any new Target definitions approved at the
Step 2f gate, and empty placeholder files for Scope paths marked `[new]`. A new Target
definition may be added to an existing Target-definition file, but existing Targets
remain unchanged.

The developer who runs the Ticket authors its implementation. A placeholder is a
zero-byte file: do not put declarations, modules, packages, assertions, stimulus,
functions, comments, or any other content in it. Leave existing RTL (including
Verilog/SystemVerilog/VHDL), HDL testbenches, firmware, Python, scripts, constraints,
generators, build hooks, and every other implementation or support-code file unchanged.
If validation or enqueue would require code beyond an approved new Target definition,
stop and report the blocker; creating that code is outside this skill.

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

Run the dependency scan (§A), infer the ticket type and fields (§B), then apply Ticket
Creation Guidance (§E). Two judgement calls §B can't make for you:

- **scope**: don't list generated/compiled artifacts. For bugfix tickets using the top-level TB, suggest including firmware source files. Before accepting `scope: ["*"]`, push back once: *"Are you sure you can't narrow it down to at least a directory (e.g., `rtl/*.sv`)?"*
- **bugfix reproducibility**: if the bug isn't visible in current tests, confirm the feature+bugfix split (§B) with the user before drafting two tickets.

### 2c: Grilling & full draft (detailed mode only)

1. Read `grilling.md` and run the dependency-aware grilling session: map the design as
   a decision tree, ask the entire currently unblocked frontier in each round, recommend
   an answer for every question, and defer decisions whose prerequisites remain open.
   Investigate codebase facts instead of asking for them, and keep the depth proportional.
2. Use the grilling rounds to settle material ticket fields and acceptance criteria as well
   as the design. Start from the §B/§D defaults, apply §E guidance, and let the user edit
   the result in the complete Ticket without creating a separate review step.
3. When the frontier is empty, synthesize the **detailed implementation plan** and complete
   ticket (skeleton and placement: `TICKET_TEMPLATE.md`), then continue directly to the
   draft gate in 2f. The complete ticket and any new Target definitions form the one
   post-grill review artifact; the ticket is the payload the developer plans against.

Detailed mode skips 2d and 2e because those decisions were folded into grilling.

### 2d: Complete lightweight fields (lightweight mode only)

Ask only about **missing** (`???`) and **uncertain** fields, with all questions in one
message. **All fields required** — no silent defaults. Iterate until complete. Continue
directly to 2e without showing an intermediate ticket preview.

### 2e: Criteria selection (lightweight mode only)

Build the shipped defaults from ticket type + Targets (§D), then apply the Project's Ticket
Creation Guidance (§E). Present the resolved selection as a structured menu:

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

### 2f: Approve the ticket

**MANDATORY TICKET APPROVAL.** Show the complete proposed ticket (frontmatter +
body, excluding generated basis fields), followed by a **New Targets** section containing every
Target that ticket creation will author. For each new Target, show its name, destination
file, and complete proposed definition. If creation adds no Targets, show
`New Targets: none`. Ask: *"Create this ticket and these Targets? (yes / edit / cancel)"*

For detailed mode, this is the first review artifact shown after grilling. If the user
chooses `edit`, revise the complete ticket or Target definitions and show the entire review
artifact again; keep the review at this gate rather than falling back to summaries or partial
previews. Approval authorizes the complete creation transaction in Step 4; Acceptance-Basis
mechanics require no further user confirmation.

**Never write the ticket file until the user explicitly approves** — including agent-invoked creation from other skills.

## Step 3: Agent Mode

1. All fields required — return an error listing the missing fields (no interactive questions)
2. Same dependency scan (§A) and validation (§C) as human mode
3. Inference (§B) only for fields marked `"infer"`; missing without `"infer"` → error
4. Explicit `criteria` or `on_success` values win for that field. For each field marked
   `"infer"`, build the §B/§D fallback and apply the relevant §E guidance
5. Ambiguous, conflicting, or unresolvable applicable guidance is a non-interactive error;
   identify the prose that could not be translated
6. Always pass the resolved `--criteria` and `--on-success` values to `create-file`
7. **No grilling** — the calling agent must provide all details upfront
8. Approval gate (2f) applies unless the caller passed `--no-confirm`; validation never does

## Step 4: Author and Enqueue

Follow §C end to end after ticket approval: create the draft and workspace, author only
the approved new Target definitions there, validate, and enqueue. Author the new Targets
exactly as approved at the 2f gate and create only empty placeholders for `[new]` Scope
paths; do not implement any part of the Ticket. Basis publication remains an internal
implementation detail: do not expose its SHAs or pause for another
confirmation. New-Target authoring is part of ticket creation, never deferred to the
developer. If authoring or validation requires changing an approved Target definition,
return to 2f. If it requires implementation code or a mechanical failure cannot be
repaired, report the actionable error without turning basis internals into user choices.

## Step 5: Report

- **Human**: print the enqueued path (`board/queue/`, or `board/waiting/` when it declares unmet dependencies — Step 4's `enqueue` already moved it there), then suggest `booley run`
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
| `on_success` | Start with `{destination: review, merge: true, cleanup: true, triage_report: true, remove_targets: []}`, then apply relevant §E guidance. Set `triage_report: false` to skip the rich HTML explanation. Put a criterion-bound Target in `remove_targets` only when it must exist for execution/review but must not land in the destination; this requires `merge: true`. Benchmark: `{destination: done, merge: false, cleanup: true, triage_report: true, remove_targets: []}` |
| `dependencies` | From scan (§A) + grilling; user confirms |
| `priority` | Default `medium` |
| `criteria` | Start with §D defaults, then apply relevant §E guidance; user confirms/edits. **feature** → from grilling. **refactor** → all `pass -> pass`. **bugfix** → the failing entry `fail -> pass`, rest `pass -> pass`. **verification** → TB-only work |

**Bugfix, not yet reproducible?** Recommend a split: feature ticket (create the failing test) + bugfix ticket (fix the RTL, depends on the feature).

Runtime fields are *not* inferred: `acceptance_basis` and `created` are published
atomically by `enqueue`, and `feature_branch` is written by `init`. Never author
those fields or a SHA. `integration_base`, `target_contract`, and `base_sha` are
unsupported after the hard cutoff.

## §C. CLI Workflow

`create-file` generates the frontmatter (including `criteria` and `on_success`) from its
flags and creates the ordinary Ticket Workspace. Do **not** hand-write runtime YAML or
any SHA. `enqueue` validates and commits authoring state, publishes the Acceptance Basis,
stamps `created`, and moves the Ticket to queue or waiting as one operation.

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
  --on-success "$ON_SUCCESS_JSON" \      # JSON: all five on_success fields
  --body-file "$BODY"

# E4. Add only approved new Target definitions in the workspace printed by create-file.
#     A Scope [new] path may be absent or a zero-byte placeholder. Leave all other
#     implementation/support-code files unchanged. Existing sources may make a new
#     relative-QoR Target fully executable; otherwise report the blocker instead of
#     creating code to make the Target runnable.

# E5. Validate — a path, not a slug. Fix and re-run until clean.
python -m booley.ticket_board validate-ticket \
  .booley_project/tickets/board/drafts/$SLUG.md [--check-git]

# E6. Enqueue. This automatically publishes the immutable Acceptance Basis.
python -m booley.ticket_board enqueue "$SLUG"
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
| Target-bound TB review | `review_tb_quality: {target: <sim-target>}`; use when structured simulation criteria name multiple Targets |
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
forms automatically compare the same Target/test at the Acceptance Basis; consult
`booley cheat --criteria` for the complete signed-bound vocabulary.
Write every percentage value with an explicit `%` suffix (for example,
`cycle_count_reduce_at_least: 8%`); bare numbers are invalid for percentage thresholds.

`synthesis_ok` / `fpga_impl_ok` take threshold **params** in four flavours per metric:
absolute `_max` / `_min`, plus baseline-relative `_increase_at_most` / `_reduce_at_least`
(compared against the Ticket's Acceptance Basis). Common ones: `cell_count_max`, `fmax_mhz_min`,
`cell_count_reduce_at_least` (ASIC); `lut_count_max`, `ff_count_max`, `fmax_mhz_min`
(FPGA). Don't hardcode a subset here — for the full per-metric matrix and which pairs are
mutually exclusive, run `booley cheat --criteria` (the "threshold flavours" table, also in
`docs/user/USAGE.md`); it is generated from the validator, so it never drifts.
The baseline-relative values are percentages and therefore require the `%` suffix.

For a relative threshold, use a plain Target name when baseline and candidate are the same.
When the ticket intentionally needs different frozen Targets, put
`{baseline: <before>, candidate: <after>}` in `targets:`. Author both Targets before enqueue;
the candidate determines the expanded Criterion name.

### Rules

- ≥1 mandatory criterion required
- Every default review criterion uses its bare key, which expands to corrective
  `_clean`, and runs after code-changing work. Use explicit `_done` only for
  user-requested advisory review. Every `_clean` waiver includes a justification
  and is shown to the user regardless of severity.
- Project-defined criterion types are allowed when the live Project catalog registers them
- A criterion may name a new Target only when ticket creation authors it in the
  Ticket Workspace before enqueue. Do not put acceptance controls in Developer
  Scope merely to permit later edits: every `.core`, tests/Target-selection
  configuration, selected constraint, generator, and build hook is immutable
  after enqueue.
- A future non-relative Target may reference missing RTL/TB paths only when every
  path is declared Scope `[new]`. For relative QoR, the baseline Target must resolve and
  dry-run completely at the basis baseline; a distinct frozen candidate may defer only
  its Scope `[new]` RTL/TB paths.
- If a blocked ticket needs different authored inputs, use `return-to-draft`; it
  preserves the old basis and evidence and starts a new authoring generation.
- Decide `on_success.remove_targets` during Ticket creation. Every selector must resolve
  uniquely and name a Target bound by that Ticket's Criteria. The Target remains fixed and
  available through development and review; acceptance removes only its `.core` definition
  and unambiguously-owned `tests.toml` tables from the prepared merge candidate. Shared
  filesets, sources, parameters, constraints, generators, and hooks are retained. Do not use
  this field as general file cleanup.

## §E. Ticket Creation Guidance

Ticket Creation Guidance is Project-owned, free-form Markdown consumed **only here, during
creation**. Its authority is limited to the proposed Ticket's `criteria` and `on_success`.
It cannot change scope, priority, dependencies, ticket depth or body, approval gates,
Acceptance Basis publication, or an existing Ticket.

Resolve the Project directory through Booley rather than assuming its location. Read
`ticket_creation.md` when it exists. For Projects created before that filename was
introduced, read `ticket_defaults.md` only when `ticket_creation.md` is absent:

```bash
PROJECT_DIR=$(python -c 'from booley.runtime.project_dir import resolve_project_dir; print(resolve_project_dir())')
```

Treat the selected file as semantic guidance, not structured data. It may use prose,
headings, lists, tables, examples, or any other Markdown. Start from the shipped §B/§D
inference, then apply every relevant statement to the current Ticket. Guidance can add,
remove, or refine Criteria; select a standard Target or simulation matrix; vary rules by
Ticket type or context; and adjust successful-run disposition. A file containing only the
shipped template's explanatory text and examples adds no guidance.

When reading the legacy filename, disregard the old scaffold's instructions about YAML
activation, required headings, completeness, and full replacement. Treat uncommented
Project-authored mappings as expressions of intent under this guidance contract. An
untouched, comment-only legacy scaffold adds no guidance.

Resolve the guidance against the live Project rather than requiring it to spell serialized
Ticket values. Consult `booley cheat --criteria`, `booley targets`, and registered tests to
translate its intent into concrete Criterion names, value forms, Targets, and tests. Never
invent an unavailable Criterion, Target, test, or threshold. Project guidance overrides
shipped inference; a more specific statement overrides a general one; and explicit
instructions for the current Ticket override the Project file.

In human mode, ask about applicable guidance only when its meaning or mapping remains
materially ambiguous, and show the resolved result at the normal draft gate. In agent mode,
return an error that identifies ambiguous, internally conflicting, or unresolvable
applicable prose. Guidance about another Ticket type or situation is simply inapplicable,
not an error.

Validate the resolved Ticket through §C. The Markdown guidance itself has no schema,
required headings, completeness check, or static validation pass. Simulation entries in
the resolved Ticket retain exact Ticket syntax: Project regressions normally say
`pass -> pass`, while a reproduced bug changes its selected entry to `fail -> pass` for
that Ticket only.
