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

Ticket creation authors only the Ticket, Target definitions, their referenced filesets and
local parameter declarations, unambiguously owned `tests.toml` tables approved at the Step
2f gate, and empty placeholder files for Scope paths marked `[new]`. A planned Target may be
added to an existing Target-definition file, while existing definitions remain unchanged.

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
> 1. ✓ `LINT`: Target → `clean` *(feature/refactor)*
> 2. ✓ `SIM`: Target → `{all: pass}` *(all registered tests)*
> 3. ✓ `REVIEW`: `rtl: {bugs: clean}` *(feature/refactor)*
> 4. ✓ `REVIEW`: `tb: {quality: clean}` *(feature/verification)*
>
> **Optional:**
> 5. ☐ `REVIEW`: `rtl: {spec: clean}` *(feature with a detailed spec)*
> 6. ☐ `SYNTH`: Target → threshold mapping *(datapath/timing-critical)*
> 7. ☐ `MUTATION`: Target → policy mapping
>
> Toggle by number, edit thresholds, or add custom. Enter to accept.

If the user deselects every mandatory criterion, confirm explicitly before accepting (§D requires ≥1).

### 2f: Approve the ticket

After Criteria and the derived Target Plan are fully resolved, rerun §A and reconcile the
final `dependencies` before showing the approval artifact. This final pass is
mandatory in both lightweight and detailed modes.

**MANDATORY TICKET APPROVAL.** Show the complete proposed ticket (frontmatter +
body, excluding generated basis fields), followed by a **Target Plan** section. If the
plan is omitted, show `Target Plan: none`. For New and Temporal Target entries, show the
role, canonical name, destination file, acceptance result, complete Target definition,
referenced filesets and local parameter declarations, and complete owned `tests.toml` table.
For a replacement, show its baseline and candidate, destination file, acceptance result, a
focused diff covering those same definitions, and a focused owned-table diff. If either
focused diff cannot be produced unambiguously, stop with an approval blocker. Ask:
*"Create this ticket and Target Plan? (yes / edit / cancel)"*

For detailed mode, this is the first review artifact shown after grilling. If the user
chooses `edit`, revise the complete ticket or Target definitions and show the entire review
artifact again; keep the review at this gate rather than falling back to summaries or partial
previews. Approval authorizes the complete creation transaction in Step 4; Ticket Baseline
mechanics require no further user confirmation.

**Never write the ticket file until the user explicitly approves** — including agent-invoked creation from other skills.

## Step 3: Agent Mode

1. All fields required — return an error listing the missing fields (no interactive questions)
2. Same dependency scan (§A) and validation (§C) as human mode
3. Inference (§B) only for fields marked `"infer"`; missing without `"infer"` → error
4. Explicit `CRITERIA_MANDATORY`, `CRITERIA_OPTIONAL`, Target annotations, and
   `on_success` values win. For each field marked `"infer"`, build the §B/§D
   fallback and apply the relevant §E guidance
5. Ambiguous, conflicting, or unresolvable applicable guidance is a non-interactive error;
   identify the prose that could not be translated
6. Write the complete proposed human-readable Ticket; use the same document
   converter for preview, validation, and enqueue. Never pass a separate
   Criteria, Target Plan, or completion-policy override
7. **No grilling** — the calling agent must provide all details upfront
8. Approval gate (2f) applies unless the caller passed `--no-confirm`; validation never does
9. After all inferred Criteria and Target annotations are resolved, rerun §A and reject
   any missing provider dependency before the approval gate or `--no-confirm` creation
10. Agent mode requires `branch` explicitly and never obtains it from
    `git branch --show-current`. For a standalone paired Project repository, require
    `project_destination_ref` explicitly when it differs from the outer destination.
    In agent mode, treat the supplied pair as authoritative: reject a missing or ambiguous member
    instead of consulting either live checkout.

## Step 4: Author and Enqueue

Follow §C end to end after ticket approval: create the draft and workspace, author only
the approved planned Target definitions, referenced inputs, and owned test tables there,
validate, and enqueue. Author them
exactly as approved at the 2f gate and create only empty placeholders for `[new]` Scope
paths; do not implement any part of the Ticket. Basis publication remains an internal
implementation detail: do not expose its SHAs or pause for another
confirmation. New-Target authoring is part of ticket creation, never deferred to the
developer. If authoring or validation requires changing an approved Target definition,
return to 2f. If it requires implementation code or a mechanical failure cannot be
repaired, report the actionable error without turning basis internals into user choices.
Report which repository rejected which Ticket field. A workspace-materialization
warning is a blocker: retain the diagnostic draft, and do not rewrite either destination
to make validation or enqueue pass.

## Step 5: Report

- **Human**: print the enqueued path (`board/queue/`, or `board/waiting/` when it declares unmet dependencies — Step 4's `enqueue` already moved it there), then suggest `booley run`
- **Agent**: return path

---

# Reference

## §A. Dependency Scan

```bash
CLASSIFIED=$(python -m booley.ticket_board classify)
```

Inspect every non-done Ticket's published Ticket baseline and Target Plan as well as
its Scope. Preserve ordinary scope-overlap and interface-dependency inference for all
non-done Tickets. If an active provider exports a New or Replacement Target selected
by the new Ticket's Criteria, add that provider to `dependencies` in human mode. In
agent mode, reject the request and name every missing provider dependency. Reject
ambiguous active exports (multiple providers offering the same selector) and any
selector that one active provider offers while another removes it. Do not infer a
dependency for Temporal Targets or replacement baselines, because providers do not
export them.

## §B. Field Inference

Field definitions and body forms live in `TICKET_TEMPLATE.md`. Infer values from
conversation and the Project, then show the complete document at the approval gate.

| Field | Inference |
|---|---|
| `summary` | Concise one-line intent; used to generate the slug |
| `type` | `bugfix` for a reproduced bug, `refactor` for restructuring, `verification` for TB/coverage work, otherwise `feature` |
| `branch` | `branch` is a branch name in the outer repository, without `refs/heads/`; human mode may infer it from `git branch --show-current` |
| `project_destination_ref` | `project_destination_ref` is the canonical full local branch ref in the paired Project repository; omit it only for same-name inference when that inferred ref exists |
| `scope` | Files the developer may change; mark a new file `[new]` |
| `spec` | Include an existing architecture spec when relevant |
| `dependencies` | Resolve from §A and the requested work |
| `priority` | `medium` unless urgency is known |
| `CRITERIA_MANDATORY` / `CRITERIA_OPTIONAL` | Start with §D and Project guidance (§E), then edit against the user's desired acceptance conditions |
| Target annotations | Mark every mention of a Ticket-authored Target `(new)`, `(temp)`, or `(replaces <existing Target>)`; the converter derives the Target Plan |
| `on_success` | Start with `[triage_report, review, merge, cleanup]`, then omit any action the user does not want |

A reproduced bug may use `fail -> pass` on its exact registered SIM test; an
ordinary passing test uses `pass`. If the bug has no failing test, recommend a
verification ticket that creates one and a dependent fix ticket.

Only the Ticket Board publishes `machine`, `created`, and `feature_branch`.
Do not author generated metadata, SHAs, `target_plan`, `ticket_format`, or a
`criteria` wrapper. An annotated Target requires `merge` in `on_success`;
`cleanup` is otherwise independent of merge.

## §C. Authoring Workflow

1. Generate a slug with `python -m booley.ticket_board slug "$SUMMARY"`.
2. Compose the **entire** Markdown ticket from `TICKET_TEMPLATE.md`, with YAML
   frontmatter and the type-specific `## Description`. Add `## Implementation
   Plan` only for detailed-plan tickets. Author Target lifecycle suffixes on
   every structured mention, including both Criteria sections and the
   `ELAB_STANDALONE` list.
3. Show the complete proposed document and derived Target Plan at the approval
   gate (Step 2f). After approval, save the approved document at `$TICKET_PATH`
   and run `python -m booley.ticket_board create-file "$SLUG" --document-file "$TICKET_PATH"`.
   The command creates the draft in its Ticket Workspace.
4. Author only approved new Target definitions, referenced filesets and local
   parameter declarations, and owned `tests.toml` tables.
   A Scope `[new]` file may be absent or a zero-byte placeholder. Leave
   implementation and other support code unchanged.
5. Run `python -m booley.ticket_board validate-ticket <draft-path>` and fix
   any diagnostic. Enqueue with `python -m booley.ticket_board enqueue <slug>`.
   Enqueue converts the durable authored file and publishes the Acceptance
   Basis; CLI flags must not override Criteria, Target roles, or completion
   policy.

If the current `create-file` CLI cannot accept the complete v2 document, stop
and report that CLI mismatch; do not translate the Ticket to the retired
`--criteria`, `--target-plan`, or mapping `--on-success` forms. Never hand-write
generated Ticket Board metadata.

## §D. Criteria Catalog

`CRITERIA_MANDATORY` is required and must expand to at least one atomic
Criterion. `CRITERIA_OPTIONAL` may be absent. Each uppercase capability maps
Targets to their checks; `REVIEW` maps a category/focus to an outcome. The
complete frontmatter example is in `TICKET_TEMPLATE.md`.

| Capability | Human form |
|---|---|
| `LINT` | `lint_core: clean` |
| `ELAB` | `sim_core: pass` |
| `ELAB_STANDALONE` | `[sim_core, sim_probe (temp)]` (one sweep over exactly this Target set) |
| `SIM` | `sim_core: {all: pass}` or `sim_core: {smoke: fail -> pass}` |
| `CYCLE_COUNT` | `sim_core: {smoke: {cycle_count_max: 100000}}` |
| `SYNTH` | `synth_core: pass` or `synth_core: {area_um2_max: 10000, fmax_mhz_min: 400}` |
| `FPGA` | `fpga_core: pass` or `fpga_core: {lut_count_max: 100000}` |
| `REVIEW` | `rtl: {bugs: clean}` or `rtl: {bugs: done}` |
| `MUTATION` | `sim_core: {scope: [rtl/core.sv], min_detected: 8, total: 10}` |
| `COVERAGE` | `sim_core: {tests: all, metrics: {line: {min_pct: 90}}}` |
| Project scalar Criterion | `IMPLEMENTATION_DONE: true` when `implementation_done` is registered in Project `criteria.toml` |

Use exact registered Target and test selectors. `SIM.all` requires a nonempty
registered suite; `fail -> pass` requires a named test and matching red/green
evidence. The Target already identifies its top-level TB. `REVIEW.done` and
`REVIEW.clean` are separate outcomes, so one may be mandatory and the other
optional. `SYNTH`, `FPGA`, `CYCLE_COUNT`, and `COVERAGE` produce a separate
atomic Criterion for each metric. Put multiple metrics for one Target in one
mapping, or split them between sections when mandatory/optional status differs.
Project scalar Criteria use their registered name in uppercase and the value
`true`; they produce the lowercase registered Criterion without a Target binding.

For relative thresholds, use the current Target at its Ticket baseline by
default. A replacement defaults to its `(replaces <existing Target>)`
predecessor. A `(new)` or `(temp)` Target needs an explicit existing `baseline`
Target. Percentage thresholds require a `%` suffix. Consult the live
threshold vocabulary through `booley cheat --criteria`; apply its parameter
names under the v2 capability shape, not the retired lowercase Ticket syntax.

Default mandatory choices: feature → LINT, SIM, RTL bugs REVIEW, TB quality
REVIEW; bugfix → SIM; refactor → LINT, SIM, RTL bugs REVIEW; verification →
SIM and TB quality REVIEW. Add `REVIEW.rtl.spec` for a detailed feature spec;
suggest `COVERAGE` and `MUTATION` for verification, `SYNTH` for timing or area,
and `FPGA` for implementation constraints. Include a Project-authored Verible
style-lint Target when selecting lint Targets.

Every new Target must be authored in the Ticket Workspace before enqueue and
must have a mandatory compatible Flow Criterion. Every mention repeats its
lifecycle suffix, even across mandatory and optional sections. The Target
Plan is derived; do not add a `target_plan` field or section. A planned Target
may add a dedicated fileset or local parameter declaration, but cannot edit an
existing definition or attach a new input to an unchanged Target. New
conditional parameter declaration keys are unsupported; use a stable
declaration name and conditional entries in the Target's parameter list.
Acceptance removes only derived Target definitions, unambiguously owned
`tests.toml` tables, and newly authored filesets or parameter declarations
orphaned by Temporal Target removal. Existing and still-shared inputs,
constraints, generators, and hooks remain. If authored inputs change after
enqueue, return the Ticket to draft and re-enqueue.

## §E. Ticket Creation Guidance

Ticket Creation Guidance is Project-owned, free-form Markdown consumed **only here, during
creation**. Its authority is limited to the proposed Ticket's Criteria,
Target annotations, and `on_success`.
It cannot change scope, priority, dependencies, ticket depth or body, approval gates,
Ticket baseline publication, or an existing Ticket.

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
translate its intent into concrete v2 capability forms, Targets, and tests. Never
invent an unavailable Criterion, Target, test, or threshold. Project guidance overrides
shipped inference; a more specific statement overrides a general one; and explicit
instructions for the current Ticket override the Project file.

In human mode, ask about applicable guidance only when its meaning or mapping remains
materially ambiguous, and show the resolved result at the normal draft gate. In agent mode,
return an error that identifies ambiguous, internally conflicting, or unresolvable
applicable prose. Guidance about another Ticket type or situation is simply inapplicable,
not an error.

Validate the resolved Ticket through §C. The Markdown guidance itself has no schema,
required headings, completeness check, or static validation pass. Project regressions
normally use `pass`; a reproduced bug may use `fail -> pass` for its named SIM test.
