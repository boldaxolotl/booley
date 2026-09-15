---
summary: <one-line description>
type: feature | bugfix | refactor | verification
branch: <destination branch>
scope:
  - rtl/existing.sv
  - rtl/new_file.sv [new]   # Scope [new] marks a file, not a Target
spec: ""                    # optional architecture-spec path
dependencies: []
priority: medium
on_success: [triage_report, review, merge, cleanup]
# Omit any completion flag to disable it. cleanup is valid without merge.
# Any annotated Target requires merge.
CRITERIA_MANDATORY:
  LINT:
    lint_core: clean
  SIM:
    sim_core: {all: pass}
    # A bugfix may require: sim_core: {smoke: fail -> pass}
  REVIEW:
    rtl: {bugs: clean}
    tb: {quality: clean}
CRITERIA_OPTIONAL:
  SYNTH:
    synth_core: {area_um2_max: 10000, fmax_mhz_min: 400}
  MUTATION:
    sim_core: {scope: [rtl/existing.sv], min_detected: 8, total: 10}
# Other capabilities: ELAB, ELAB_STANDALONE, CYCLE_COUNT, FPGA, COVERAGE.
# A registered Project scalar Criterion uses its uppercase name, e.g.
#   IMPLEMENTATION_DONE: true
# Every Criteria mention of a Ticket-authored Target repeats one suffix:
#   synth_new (new): ...       New Target, retained after merge
#   sim_probe (temp): ...      Temporal Target, removed from accepted destination
#   synth_new (replaces synth_old): ...   candidate replaces predecessor
# The Target Plan is derived from these annotations; do not author target_plan.
# All Criteria parameters and registered Target/test names must resolve at enqueue.
---

## Description

Structure depends on ticket type. For a greenfield module the `spec` field should point at
the architecture spec.

### For `feature`

```markdown
## Description

### Current State
<What the module currently does, relevant interfaces and behavior. For greenfield: "N/A — new module">

### Required Changes
<What needs to change and why — reference spec or issue if applicable>

### Affected Interfaces
<Which ports, signals, or protocols are impacted by the change>
```

### For `bugfix`

```markdown
## Description

### Failing Simulation
<Target/test that fails — the agent uses this as its starting point>

### Observed Symptoms
<What you see: error messages, signal mismatches, assertion failures, unexpected waveform behavior>

### Known-Good Reference (if known)
<Commit SHA or Target where the failing test passes — gives the developer a comparison
to isolate the regression. Leave blank if unknown or if it never passed>

### Suspected Root Cause (if known)
<Best guess — helps the developer focus its investigation. Leave blank if unknown>
```

### For `refactor`

```markdown
## Description

### Current Structure
<How the code is organized now and why it's problematic>

### Target Structure
<How it should be organized after refactoring>

### Invariants
<What must NOT change — behavior, interfaces, timing, area>
```

### For `verification`

```markdown
## Description

### Coverage Gaps
<What behaviors, states, or scenarios are untested>

### Verification Strategy
<Stimulus and checking approach — constrained random, directed, assertions, scoreboard>

### RTL Boundary
<RTL must NOT be modified — list modules or signals the TB targets>
```

### Implementation Plan (detailed-plan tickets only)

Detailed-plan tickets append this section after `## Description`; it carries the plan
distilled from the design-grilling session (how to write it: `grilling.md`). Lightweight
tickets omit it and rely on the developer's inline planning.

```markdown
## Implementation Plan

### Approach
<Chosen design and key decisions (note rejected alternatives where relevant)>

### Implementation Steps
<Ordered, file-by-file breakdown — the sequence a coder would follow>

### Interface Changes
<New/changed ports, signals, widths, handshakes, Target defines>

### Edge Cases & Risks
<Corner cases, reset/CDC, hidden breakage — and how each is handled>

### Verification
<How the change is proven: TBs/tests, new scenarios, definition of done>

### Open Questions
<Anything unresolved after grilling — omit if none>
```

---

<!-- CRITERIA_MANDATORY and CRITERIA_OPTIONAL are the authored acceptance source. -->
